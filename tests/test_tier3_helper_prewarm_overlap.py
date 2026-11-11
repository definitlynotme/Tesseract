from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tier3_validation_config import build_validator_prewarm_config
from tier3_validation_protocol import Tier3WorkerPhase
from tier3_validator_prewarm import ValidatorPrewarmProcess
from tier3_validator_runtime import ValidatorHelperRuntime
from tier3_validator_supervisor import Tier3ValidatorSupervisor, _WorkerGeneration


def _supervisor():
    return Tier3ValidatorSupervisor(
        binary_path="fixture", probe_urls=["http://fixture/probe"],
        expected_statuses={204}, probe_timeout_seconds=1.0,
        slow_threshold_ms=3000, attempt_sink=lambda _: None,
        capacity_sink=lambda _: pytest.fail("prewarm is not capacity"),
        min_workers=4, max_workers=16,
    )


def _worker(wid=1):
    return _WorkerGeneration(
        worker_id=wid, generation=1, process=SimpleNamespace(pid=100 + wid),
        connection=None,
        job=SimpleNamespace(snapshot=lambda: SimpleNamespace(process_ids=(100 + wid, 200 + wid))),
        helper_pid=100 + wid, mixed_port=20000 + wid, controller_port=21000 + wid,
        phase=Tier3WorkerPhase.STARTING, bootstrap_ready=True,
    )


def _ready(worker):
    return dict(phase="prewarm_started", worker_id=worker.worker_id,
                worker_generation=worker.generation, helper_pid=worker.helper_pid,
                mihomo_pid=200 + worker.worker_id, mixed_port=worker.mixed_port,
                controller_port=worker.controller_port)


def test_prewarm_config_matches_runtime_and_is_fail_closed():
    for ipv6 in (False, True):
        runtime = SimpleNamespace(mixed_port=20001, controller_port=21001, ipv6=ipv6, _dns_block=None)
        config = build_validator_prewarm_config(20001, 21001, ipv6=ipv6)
        assert config == ValidatorHelperRuntime._base_config(runtime)
        assert config["rules"] == ["MATCH,REJECT"]
        assert not config["allow-lan"]
        assert not config["proxies"]


def test_facility_progress_only_releases_shell_prewarm_not_worker_capacity():
    supervisor = _supervisor()
    supervisor._desired_workers = 16
    supervisor._workers = {wid: _worker(wid) for wid in range(1, 5)}
    scheduled = []
    with patch.object(supervisor, "_schedule_worker_spawn", side_effect=lambda wid: scheduled.append(wid) or True):
        for wid in range(1, 4):
            supervisor._handle_startup_progress(supervisor._workers[wid], _ready(supervisor._workers[wid]))
        supervisor._start_needed_workers()
        assert scheduled == []
        supervisor._handle_startup_progress(supervisor._workers[4], _ready(supervisor._workers[4]))
        supervisor._start_needed_workers()
        assert scheduled == list(range(5, 13))
    assert not supervisor._startup_cohort_ready
    assert all(worker.phase is Tier3WorkerPhase.STARTING for worker in supervisor._workers.values())
    assert supervisor.snapshot().active_workers == 0
    assert supervisor.snapshot().starting_workers == 4
    assert all(worker.ready_at == 0 for worker in supervisor._workers.values())


@pytest.mark.parametrize("field", ["worker_id", "worker_generation", "helper_pid", "mihomo_pid", "mixed_port", "controller_port"])
def test_stale_or_unowned_facility_progress_cannot_release_shells(field):
    supervisor = _supervisor()
    worker = _worker()
    supervisor._workers[1] = worker
    payload = _ready(worker)
    payload[field] += 1
    supervisor._handle_startup_progress(worker, payload)
    assert not worker.prewarm_started
    assert worker.phase is Tier3WorkerPhase.STARTING


def test_retired_facility_progress_cannot_release_shells():
    supervisor = _supervisor()
    worker = _worker()
    supervisor._workers[1] = worker
    worker.phase = Tier3WorkerPhase.RETIRING
    supervisor._handle_startup_progress(worker, _ready(worker))
    assert not worker.prewarm_started


def test_helper_pid_cannot_substitute_for_mihomo_membership():
    supervisor = _supervisor()
    worker = _worker()
    supervisor._workers[1] = worker
    payload = _ready(worker)
    payload["mihomo_pid"] = worker.helper_pid
    supervisor._handle_startup_progress(worker, payload)
    assert not worker.prewarm_started


def test_prewarm_membership_is_retried_on_normal_ticks_without_capacity():
    supervisor = _supervisor()
    worker = _worker()
    supervisor._workers[1] = worker
    with patch.object(worker.job, "snapshot", side_effect=[
        SimpleNamespace(process_ids=(101,)), SimpleNamespace(process_ids=(101, 201)),
    ]) as query, patch("tier3_validator_supervisor.time.monotonic", return_value=100.0) as clock:
        supervisor._handle_startup_progress(worker, _ready(worker))
        assert not worker.prewarm_started
        clock.return_value = 100.1
        supervisor._check_deadlines()
        assert worker.prewarm_started
        assert query.call_count == 2
    assert worker.phase is Tier3WorkerPhase.STARTING
    assert worker.ready_at == 0


@pytest.mark.parametrize("transition", ["retired", "replaced"])
def test_prewarm_membership_readback_cannot_promote_a_stale_worker(transition):
    supervisor = _supervisor()
    worker = _worker()
    supervisor._workers[1] = worker

    def readback():
        if transition == "retired":
            worker.phase = Tier3WorkerPhase.RETIRING
        else:
            supervisor._workers[1] = _worker()
            supervisor._workers[1].generation = 2
        return SimpleNamespace(process_ids=(101, 201))

    with patch.object(worker.job, "snapshot", side_effect=readback):
        supervisor._handle_startup_progress(worker, _ready(worker))
    assert not worker.prewarm_started
    assert not supervisor._workers[1].prewarm_started
    assert supervisor.snapshot().active_workers == 0


def test_prewarm_membership_poll_is_bounded_and_full_ready_clears_pending():
    supervisor = _supervisor()
    worker = _worker()
    supervisor._workers[1] = worker
    with patch.object(worker.job, "snapshot", return_value=SimpleNamespace(process_ids=(101,))) as query, \
            patch("tier3_validator_supervisor.time.monotonic", return_value=100.0):
        supervisor._handle_startup_progress(worker, _ready(worker))
        for _ in range(100):
            supervisor._confirm_prewarm_membership(worker)
        assert query.call_count == 1
    worker.mihomo_pid = 201
    worker.startup_phase = "membership_pending"
    with patch.object(supervisor, "capacity_sink") as capacity:
        assert supervisor._confirm_ready_membership(worker)
    capacity.assert_called_once_with(1)
    assert worker.prewarm_pending_pid == 0
    assert worker.phase is Tier3WorkerPhase.IDLE


def _payload(tmp_path):
    return dict(worker_id=1, worker_generation=2, mixed_port=20001,
                controller_port=21001, binary_path="fixture", temp_parent=str(tmp_path),
                prewarm_config=build_validator_prewarm_config(20001, 21001))


def test_import_failure_cleanup_reaps_prewarm_before_removing_directory(tmp_path):
    events = []
    class Process:
        pid = 200
        dead = False
        def poll(self):
            return 0 if self.dead else None
        def kill(self):
            events.append("kill")
            self.dead = True
        def wait(self, timeout=None):
            events.append("wait")
            return 0

    with patch("tier3_validator_prewarm.subprocess.Popen", return_value=Process()):
        facility = ValidatorPrewarmProcess(_payload(tmp_path), lambda *args: None)
        directory = facility.work_dir
        assert json.loads((directory / "validator-prewarm.json").read_text()) == _payload(tmp_path)["prewarm_config"]
        facility.close()
        facility.close()
    assert events == ["kill", "wait"]
    assert not directory.exists()


def test_runtime_adopts_owned_process_without_second_spawn(tmp_path):
    import sys

    payload = _payload(tmp_path)
    payload.update(binary_path=sys.executable, probe_urls=[], expected_statuses=[204])
    process = SimpleNamespace(pid=42, returncode=None)
    facility = SimpleNamespace(work_dir=tmp_path, process=process)
    sent = []
    runtime = ValidatorHelperRuntime(SimpleNamespace(send_bytes=sent.append), payload, prewarmed=facility)
    assert runtime.process is process
    assert runtime.work_dir == tmp_path
    assert list(tmp_path.iterdir()) == []
    response = AsyncMock()
    response.__aenter__.return_value = SimpleNamespace(status=200)
    session = SimpleNamespace(get=lambda _: response)
    with patch("tier3_validator_runtime.aiohttp.ClientSession", return_value=session), \
            patch("tier3_validator_runtime.aiohttp.TCPConnector"), \
            patch.object(runtime, "_mixed_port_is_ready", new=AsyncMock(return_value=True)), \
            patch("tier3_validator_runtime.asyncio.create_subprocess_exec", side_effect=AssertionError("duplicate Mihomo")):
        asyncio.run(runtime.start())
    assert json.loads(sent[-1])["kind"] == "ready"


@pytest.mark.parametrize("failure", ["spawn", "publish"])
def test_prewarm_creation_failure_cleans_owned_directory(tmp_path, failure):
    process = SimpleNamespace(pid=42, poll=lambda: None, kill=lambda: None, wait=lambda **kwargs: 0)
    popen_effect = OSError("spawn failed") if failure == "spawn" else None
    def publish(*args):
        raise RuntimeError("publication failed")
    with patch("tier3_validator_prewarm.subprocess.Popen", return_value=process, side_effect=popen_effect):
        with pytest.raises((OSError, RuntimeError)):
            ValidatorPrewarmProcess(_payload(tmp_path), publish)
    assert list(tmp_path.iterdir()) == []


def test_prewarm_async_wait_is_cancellable_without_orphan_waiter(tmp_path):
    process = SimpleNamespace(pid=42, poll=lambda: None)
    with patch("tier3_validator_prewarm.subprocess.Popen", return_value=process):
        facility = ValidatorPrewarmProcess(_payload(tmp_path), lambda *args: None)
        async def run():
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(facility.process.wait(), 0.025)
        asyncio.run(run())
        process.poll = lambda: 0
        process.wait = lambda **kwargs: 0
        facility.close()


@pytest.mark.parametrize("mode", ["global", "direct", ""])
def test_prewarm_cannot_override_reject_rule_with_another_mode(tmp_path, mode):
    payload = _payload(tmp_path)
    payload["prewarm_config"]["mode"] = mode
    with patch("tier3_validator_prewarm.subprocess.Popen") as spawn:
        with pytest.raises(ValueError):
            ValidatorPrewarmProcess(payload, lambda *args: None)
    spawn.assert_not_called()
    assert list(tmp_path.iterdir()) == []
