"""Contracts of the batch validator backend (K worker identities per process).

Every test runs the real ``Tier3BatchValidatorSupervisor`` against the
deterministic multi-slot fake helper, so the group/round/slot machinery is
exercised end to end without Mihomo.  The invariants pinned here are the ones
codex asked for before wiring: one lease per identity, results attributed per
identity, cancel and quarantine confined to one identity, a process loss
yielding exactly one facility terminal per staged or in-flight lease, and the
identity ceiling scaling with slots while the process ceiling stays audited.
"""
from __future__ import annotations

import os
import queue
import threading
import time
from typing import Any

import pytest

from models import Node
from test_support.tier3_batch_deterministic_helper import (
    batch_deterministic_helper_bootstrap,
)
from tier3_batch_validator_supervisor import (
    Tier3BatchValidatorSupervisor,
    _BatchWorkerGeneration,
    _HelperGroup,
)
from tier3_defaults import tier3_process_worker_ceiling
from tier3_validation_domain import Tier3ValidationDomain
from tier3_validation_protocol import (
    AdmissionCommitted,
    EndpointAttemptKind,
    EndpointAttemptResult,
    Tier3TerminalKind,
    Tier3WorkerPhase,
    ValidateEndpointCmd,
    canonical_json,
)
from tier3_validator_supervisor import Tier3ValidatorSupervisor
from tier3_windows_job import JobSnapshot


def _wait_until(predicate, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition did not become true")


def _node(index: int, **extra: Any) -> Node:
    return Node(
        name=f"batch-candidate-{index}",
        protocol="http",
        address="127.0.0.1",
        port=10000 + index,
        tier="tier3",
        extra={
            "tier3_source_root_id": "local-root",
            "tier3_source_unit_family": "http",
            **extra,
        },
    )


def _command(
    index: int,
    *,
    epoch: str,
    node: Node | None = None,
    hard_cap: float = 5.75,
    probe_budget: float = 3.0,
) -> ValidateEndpointCmd:
    candidate = node if node is not None else _node(index)
    return ValidateEndpointCmd(
        refresh_epoch=epoch,
        candidate_digest=f"{index:064x}",
        config_digest=f"{index + 1000:064x}",
        lease_id=f"{epoch}-{index}",
        sequence=index,
        node_json=canonical_json(candidate.to_dict()),
        source_root="root",
        source_family="http",
        endpoint="http://127.0.0.1/generate_204",
        facility_budget_seconds=0.5,
        probe_budget_seconds=probe_budget,
        candidate_hard_cap_seconds=hard_cap,
    )


def _supervisor(
    *,
    slots: int,
    workers: int,
    results: "queue.Queue[EndpointAttemptResult]",
    capacities: list[int] | None = None,
    **kwargs: Any,
) -> Tier3BatchValidatorSupervisor:
    capacity_log = capacities if capacities is not None else []
    return Tier3BatchValidatorSupervisor(
        slots_per_process=slots,
        binary_path="unused-by-fixture",
        probe_urls=["http://127.0.0.1/generate_204"],
        expected_statuses={204},
        probe_timeout_seconds=3.0,
        slow_threshold_ms=3000,
        attempt_sink=results.put_nowait,
        capacity_sink=capacity_log.append,
        min_workers=workers,
        max_workers=workers,
        helper_target=batch_deterministic_helper_bootstrap,
        **kwargs,
    )


def _drain(results: "queue.Queue[EndpointAttemptResult]", count: int, timeout: float = 10.0) -> list[EndpointAttemptResult]:
    collected: list[EndpointAttemptResult] = []
    deadline = time.monotonic() + timeout
    while len(collected) < count and time.monotonic() < deadline:
        try:
            collected.append(results.get(timeout=0.1))
        except queue.Empty:
            continue
    assert len(collected) == count, [(item.lease_id, item.kind.value, item.reason) for item in collected]
    return collected


def test_identity_ceiling_scales_with_slots_while_process_ceiling_stays_audited() -> None:
    results: queue.Queue[EndpointAttemptResult] = queue.Queue()
    supervisor = _supervisor(slots=4, workers=64, results=results)

    assert tier3_process_worker_ceiling() == 16
    assert supervisor.max_workers == 64
    assert supervisor.slots_per_process == 4
    assert supervisor.startup_bulkhead == 8
    assert supervisor.mihomo_start_bulkhead == 8

    capped = _supervisor(slots=4, workers=999, results=results)
    assert capped.max_workers == 64


def test_domain_selects_batch_backend_only_when_slots_exceed_one() -> None:
    kwargs = dict(
        refresh_epoch="backend-selection",
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        commit_node=None,
        terminal_observer=None,
        min_workers=4,
        max_workers=64,
        helper_target=batch_deterministic_helper_bootstrap,
    )
    batch = Tier3ValidationDomain(slots_per_process=4, **kwargs)
    assert isinstance(batch.supervisor, Tier3BatchValidatorSupervisor)
    assert batch.supervisor.max_workers == 64

    single = Tier3ValidationDomain(slots_per_process=1, **kwargs)
    assert type(single.supervisor) is Tier3ValidatorSupervisor
    assert single.supervisor.max_workers == 16


def test_groups_share_one_process_and_results_carry_their_identity() -> None:
    results: queue.Queue[EndpointAttemptResult] = queue.Queue()
    capacities: list[int] = []
    supervisor = _supervisor(slots=4, workers=8, results=results, capacities=capacities)
    supervisor.start()
    try:
        _wait_until(lambda: supervisor.snapshot().idle_workers == 8, timeout=15.0)
        snapshot = supervisor.snapshot()
        helper_pids = {int(item["helper_pid"]) for item in snapshot.workers}
        assert len(snapshot.workers) == 8
        assert len(helper_pids) == 2, "eight identities must live in two processes"
        assert sum(capacities) == 8
        assert supervisor.batch_metrics()["groups"] == 2

        for index in range(1, 9):
            assert supervisor.submit(
                _command(index, epoch="identity", node=_node(index, fixture_validation_outcome="pass"))
            )
        collected = _drain(results, 8)
        assert {item.kind for item in collected} == {EndpointAttemptKind.PASS}
        assert sorted(item.worker_id for item in collected) == list(range(1, 9))
        assert {item.lease_id for item in collected} == {f"identity-{index}" for index in range(1, 9)}
        for item in collected:
            assert item.attempt_facts and item.attempt_facts[0].generation_id == f"{item.worker_id}:1"
        _wait_until(lambda: supervisor.snapshot().idle_workers == 8)
        assert supervisor.batch_metrics()["dispatches"] == 8
    finally:
        assert supervisor.stop(timeout_seconds=3.0) == "STOPPED"
    assert supervisor.snapshot().active_workers == 0


def test_a_slow_lease_does_not_block_its_sibling_identities() -> None:
    # The per-slot backend must not couple identities: one slow lease on an
    # identity leaves the other three in the same process free to finish.
    results: queue.Queue[EndpointAttemptResult] = queue.Queue()
    supervisor = _supervisor(slots=4, workers=4, results=results)
    supervisor.start()
    try:
        _wait_until(lambda: supervisor.snapshot().idle_workers == 4, timeout=15.0)
        # One deliberately slow lease.
        assert supervisor.submit(
            _command(
                1,
                epoch="indep",
                node=_node(1, fixture_validation_outcome="pass", fixture_delay_seconds=3.0),
            )
        )
        _wait_until(lambda: supervisor.snapshot().validating_workers >= 1, timeout=5.0)
        # Three fast leases dispatched while the slow one is still in flight.
        for index in range(2, 5):
            assert supervisor.submit(
                _command(
                    index,
                    epoch="indep",
                    node=_node(index, fixture_validation_outcome="pass", fixture_delay_seconds=0.2),
                )
            )
        fast = _drain(results, 3, timeout=8.0)
        assert {item.lease_id for item in fast} == {f"indep-{index}" for index in range(2, 5)}
        # The slow lease is still running: its siblings did not wait for it.
        assert supervisor.snapshot().validating_workers >= 1
        slow = _drain(results, 1, timeout=8.0)[0]
        assert slow.lease_id == "indep-1"
        assert slow.kind is EndpointAttemptKind.PASS
        # Every result came out of the single shared process.
        assert supervisor.batch_metrics()["groups"] == 1
        assert supervisor.batch_metrics()["dispatches"] == 4
    finally:
        assert supervisor.stop(timeout_seconds=3.0) == "STOPPED"


def test_deadline_cancel_seals_one_identity_and_its_siblings_finish() -> None:
    results: queue.Queue[EndpointAttemptResult] = queue.Queue()
    supervisor = _supervisor(slots=4, workers=4, results=results)
    supervisor.start()
    try:
        _wait_until(lambda: supervisor.snapshot().idle_workers == 4, timeout=15.0)
        before = {int(item["helper_pid"]) for item in supervisor.snapshot().workers}
        assert supervisor.submit(
            _command(
                1,
                epoch="seal",
                node=_node(1, fixture_validation_outcome="pass", fixture_delay_seconds=4.0),
                hard_cap=0.5,
                probe_budget=0.2,
            )
        )
        for index in range(2, 5):
            assert supervisor.submit(
                _command(
                    index,
                    epoch="seal",
                    node=_node(index, fixture_validation_outcome="pass", fixture_delay_seconds=0.3),
                )
            )
        collected = _drain(results, 4, timeout=10.0)
        by_lease = {item.lease_id: item for item in collected}
        for index in range(2, 5):
            assert by_lease[f"seal-{index}"].kind is EndpointAttemptKind.PASS
        sealed = by_lease["seal-1"]
        assert sealed.kind is EndpointAttemptKind.RIGHT_CENSORED
        assert sealed.reason == "candidate_probe_deadline"
        assert sealed.clean_confirmed is True
        _wait_until(lambda: supervisor.snapshot().idle_workers == 4)
        after = {int(item["helper_pid"]) for item in supervisor.snapshot().workers}
        assert after == before, "a sealed lease must not replace the shared process"
        assert supervisor.batch_metrics()["slot_quarantines"] == 0
    finally:
        assert supervisor.stop(timeout_seconds=3.0) == "STOPPED"


def test_unclean_cleanup_quarantines_only_that_identity() -> None:
    results: queue.Queue[EndpointAttemptResult] = queue.Queue()
    supervisor = _supervisor(slots=4, workers=4, results=results, cleanup_seconds=0.5)
    supervisor.start()
    try:
        _wait_until(lambda: supervisor.snapshot().idle_workers == 4, timeout=15.0)
        before = {int(item["helper_pid"]) for item in supervisor.snapshot().workers}
        assert supervisor.submit(
            _command(
                1,
                epoch="poison",
                node=_node(
                    1,
                    fixture_validation_outcome="pass",
                    fixture_delay_seconds=4.0,
                    fixture_cancel_unclean=True,
                ),
                hard_cap=0.5,
                probe_budget=0.2,
            )
        )
        for index in range(2, 5):
            assert supervisor.submit(
                _command(
                    index,
                    epoch="poison",
                    node=_node(index, fixture_validation_outcome="pass", fixture_delay_seconds=0.3),
                )
            )
        collected = _drain(results, 4, timeout=10.0)
        by_lease = {item.lease_id: item for item in collected}
        for index in range(2, 5):
            assert by_lease[f"poison-{index}"].kind is EndpointAttemptKind.PASS
        poisoned = by_lease["poison-1"]
        assert poisoned.kind is EndpointAttemptKind.RIGHT_CENSORED
        assert poisoned.clean_confirmed is False
        assert poisoned.reason.endswith("_deadline_cleanup_timeout")

        _wait_until(lambda: supervisor.snapshot().quarantined_workers == 1)
        snapshot = supervisor.snapshot()
        assert snapshot.active_workers == 3
        assert snapshot.idle_workers == 3
        assert {int(item["helper_pid"]) for item in snapshot.workers} == before
        assert supervisor.batch_metrics()["slot_quarantines"] == 1
        assert supervisor.batch_metrics()["group_retirements"] == 0

        # The surviving identities keep serving from the same process.
        for index in range(5, 8):
            assert supervisor.submit(
                _command(index, epoch="poison", node=_node(index, fixture_validation_outcome="pass"))
            )
        later = _drain(results, 3)
        assert {item.kind for item in later} == {EndpointAttemptKind.PASS}
        assert all(item.worker_id != poisoned.worker_id for item in later)
    finally:
        assert supervisor.stop(timeout_seconds=3.0) == "STOPPED"


def test_process_loss_yields_one_facility_terminal_per_lease_and_replaces_the_group() -> None:
    results: queue.Queue[EndpointAttemptResult] = queue.Queue()
    capacities: list[int] = []
    supervisor = _supervisor(slots=4, workers=4, results=results, capacities=capacities)
    supervisor.start()
    try:
        _wait_until(lambda: supervisor.snapshot().idle_workers == 4, timeout=15.0)
        first_generation = {int(item["generation"]) for item in supervisor.snapshot().workers}
        assert first_generation == {1}
        assert supervisor.submit(
            _command(1, epoch="loss", node=_node(1, fixture_crash_helper=True))
        )
        for index in range(2, 5):
            assert supervisor.submit(
                _command(index, epoch="loss", node=_node(index, fixture_validation_outcome="pass"))
            )
        collected = _drain(results, 4, timeout=10.0)
        assert sorted(item.lease_id for item in collected) == [f"loss-{index}" for index in range(1, 5)]
        for item in collected:
            assert item.kind is EndpointAttemptKind.FACILITY_FAIL
            # Staged leases never reached transport: no fabricated attempt fact.
            assert item.attempt_facts == ()
            assert item.worker_generation == 1
        assert results.empty()

        _wait_until(lambda: supervisor.snapshot().idle_workers == 4, timeout=20.0)
        snapshot = supervisor.snapshot()
        assert {int(item["generation"]) for item in snapshot.workers} == {2}
        assert snapshot.replacement_count == 1
        assert snapshot.facility_failure_count == 4
        assert sum(capacities) == 8
        assert supervisor.submit(
            _command(9, epoch="loss", node=_node(9, fixture_validation_outcome="pass"))
        )
        recovered = _drain(results, 1)
        assert recovered[0].kind is EndpointAttemptKind.PASS
        assert recovered[0].worker_generation == 2
    finally:
        assert supervisor.stop(timeout_seconds=3.0) == "STOPPED"


def test_validation_domain_commits_through_the_batch_backend() -> None:
    commits: list[str] = []
    terminals: list[Tier3TerminalKind] = []
    done = threading.Event()
    lock = threading.Lock()
    total = 24

    def commit_node(node, _report, intent):
        with lock:
            commits.append(node.name)
        return AdmissionCommitted(
            refresh_epoch=intent.refresh_epoch,
            candidate_digest=intent.candidate_digest,
            lease_id=intent.lease_id,
            sequence=intent.sequence,
            pool_revision=len(commits),
            net_new_pool_growth=True,
        )

    def observe(_node, terminal):
        with lock:
            terminals.append(terminal.kind)
            if len(terminals) >= total:
                done.set()

    domain = Tier3ValidationDomain(
        refresh_epoch="batch-domain",
        binary_path="fixture",
        probe_urls=["http://127.0.0.1/generate_204"],
        expected_statuses={204},
        probe_timeout_seconds=3.0,
        slow_threshold_ms=3000,
        commit_node=commit_node,
        terminal_observer=observe,
        min_workers=8,
        max_workers=8,
        helper_target=batch_deterministic_helper_bootstrap,
        slots_per_process=4,
    )
    try:
        assert domain.start() is True
        _wait_until(lambda: domain.snapshot().supervisor.active_workers == 8, timeout=15.0)
        assert isinstance(domain.supervisor, Tier3BatchValidatorSupervisor)
        if os.name == "nt":
            # The owner's Pipe I/O keeps the GIL on calls that cannot block
            # (tier3_batch_pipe_io): every live group end uses it.
            from tier3_batch_pipe_io import HoldingPipeConnection

            with domain.supervisor._lock:
                group_connections = [group.connection for group in domain.supervisor._groups.values()]
            assert group_connections
            assert all(type(item) is HoldingPipeConnection for item in group_connections)
        for index in range(total):
            assert domain.submit(
                _node(index, fixture_validation_outcome="pass"),
                {"configured_source_count": 1},
            )
        assert done.wait(30.0), {
            "terminals": len(terminals),
            "commits": len(commits),
            "actor": domain.snapshot().actor,
        }
        assert terminals.count(Tier3TerminalKind.PASS) == total
        assert len(commits) == total
        metrics = domain.supervisor.batch_metrics()
        assert metrics["groups"] == 2
        assert metrics["dispatches"] >= total
    finally:
        assert domain.stop(timeout_seconds=4.0) == "STOPPED"


def test_slot_unusable_report_quarantines_only_that_identity() -> None:
    results: queue.Queue[EndpointAttemptResult] = queue.Queue()
    supervisor = _supervisor(slots=4, workers=4, results=results)
    supervisor.start()
    try:
        _wait_until(lambda: supervisor.snapshot().idle_workers == 4, timeout=15.0)
        before = {int(item["helper_pid"]) for item in supervisor.snapshot().workers}
        assert supervisor.submit(_command(1, epoch="unusable", node=_node(1, fixture_slot_unusable=True)))
        for index in range(2, 5):
            assert supervisor.submit(
                _command(index, epoch="unusable", node=_node(index, fixture_validation_outcome="pass"))
            )
        collected = _drain(results, 4)
        by_lease = {item.lease_id: item for item in collected}
        assert by_lease["unusable-1"].kind is EndpointAttemptKind.FACILITY_FAIL
        for index in range(2, 5):
            assert by_lease[f"unusable-{index}"].kind is EndpointAttemptKind.PASS
        _wait_until(lambda: supervisor.snapshot().quarantined_workers == 1)
        snapshot = supervisor.snapshot()
        assert snapshot.active_workers == 3
        assert {int(item["helper_pid"]) for item in snapshot.workers} == before
        bad = by_lease["unusable-1"].worker_id
        for index in range(5, 9):
            assert supervisor.submit(
                _command(index, epoch="unusable", node=_node(index, fixture_validation_outcome="pass"))
            )
        later = _drain(results, 4)
        assert all(item.worker_id != bad for item in later)
        assert supervisor.batch_metrics()["slot_quarantines"] == 1
    finally:
        assert supervisor.stop(timeout_seconds=3.0) == "STOPPED"


def test_half_quarantined_group_is_recycled_once_idle() -> None:
    results: queue.Queue[EndpointAttemptResult] = queue.Queue()
    supervisor = _supervisor(slots=4, workers=4, results=results)
    supervisor.start()
    try:
        _wait_until(lambda: supervisor.snapshot().idle_workers == 4, timeout=15.0)
        first_pids = {int(item["helper_pid"]) for item in supervisor.snapshot().workers}
        for index in (1, 2):
            assert supervisor.submit(_command(index, epoch="recycle", node=_node(index, fixture_slot_unusable=True)))
        collected = _drain(results, 2)
        assert {item.kind for item in collected} == {EndpointAttemptKind.FACILITY_FAIL}
        # Two of four identities gone: the group is replaced (new generation,
        # new process) once nothing is in flight, and full capacity returns.
        _wait_until(
            lambda: supervisor.snapshot().idle_workers == 4
            and {int(item["generation"]) for item in supervisor.snapshot().workers} == {2},
            timeout=20.0,
        )
        snapshot = supervisor.snapshot()
        assert snapshot.quarantined_workers == 0
        assert {int(item["helper_pid"]) for item in snapshot.workers}.isdisjoint(first_pids)
        assert snapshot.replacement_count == 1
        assert supervisor.batch_metrics()["group_retirements"] == 1
        assert supervisor.submit(_command(9, epoch="recycle", node=_node(9, fixture_validation_outcome="pass")))
        assert _drain(results, 1)[0].kind is EndpointAttemptKind.PASS
    finally:
        assert supervisor.stop(timeout_seconds=3.0) == "STOPPED"


def test_stop_with_in_flight_leases_reaches_stopped_within_budget() -> None:
    import subprocess

    results: queue.Queue[EndpointAttemptResult] = queue.Queue()
    supervisor = _supervisor(slots=4, workers=8, results=results)
    supervisor.start()
    try:
        _wait_until(lambda: supervisor.snapshot().idle_workers == 8, timeout=15.0)
        pids = {int(item["helper_pid"]) for item in supervisor.snapshot().workers}
        for index in range(1, 9):
            assert supervisor.submit(
                _command(index, epoch="stop", node=_node(index, fixture_validation_outcome="pass", fixture_delay_seconds=6.0))
            )
        _wait_until(lambda: supervisor.snapshot().validating_workers == 8, timeout=5.0)
    finally:
        started = time.monotonic()
        status = supervisor.stop(timeout_seconds=3.0)
        elapsed = time.monotonic() - started
    assert status == "STOPPED", status
    assert elapsed <= 3.5, elapsed
    assert supervisor.snapshot().active_workers == 0
    listing = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV"], capture_output=True, text=True,
    ).stdout
    for pid in pids:
        assert f'"{pid}"' not in listing, f"helper {pid} survived stop"


# ------------------------------------------------ bounded stop: owner side
#
# The owner's physical stop runs against in-memory groups whose Job follows a
# schedule, so its verdict can be pinned without timing real processes: a Job
# that empties while the owner waits is empty, a group is never judged on a
# sample it did not get, a Job that keeps a process is a failed stop, and the
# owner returns before the caller's deadline either way.


class _ScheduledJob:
    """A group Job whose last process leaves at ``empty_at`` (never if None).

    ``duplicate`` returns a view with its own handle state, like a duplicated
    Windows handle: closing a caller's copy never closes the owner's.
    """

    def __init__(self, empty_at: float | None, *, root: "_ScheduledJob | None" = None) -> None:
        self.root = root or self
        self.empty_at = empty_at
        self.samples = 0
        self.terminations = 0
        self.closed = False

    def snapshot(self) -> JobSnapshot:
        if self.closed:
            raise OSError("Job handle is closed")
        root = self.root
        root.samples += 1
        live = root.empty_at is None or time.monotonic() < root.empty_at
        return JobSnapshot(1 if live else 0, (4242,) if live else ())

    def terminate(self, exit_code: int = 74) -> None:
        if self.closed:
            raise OSError("Job handle is closed")
        self.root.terminations += 1

    def duplicate(self) -> "_ScheduledJob":
        return _ScheduledJob(None, root=self.root)

    def close(self) -> None:
        self.closed = True


class _ExitedHelper:
    """A helper process that is already gone (its Job may still hold Mihomo)."""

    pid = 4242
    exitcode = 74

    def is_alive(self) -> bool:
        return False

    @property
    def sentinel(self) -> int:
        raise ValueError("process object is closed")

    def join(self, timeout: float | None = None) -> None:
        return None

    def kill(self) -> None:
        return None

    def close(self) -> None:
        return None


class _ScriptedPipe:
    """A group Pipe: ``queued`` says whether a frame is waiting to be read."""

    def __init__(self, *, queued: bool = False) -> None:
        self.closed = False
        self.queued = queued
        self.sent: list[bytes] = []

    def poll(self, _timeout: float = 0.0) -> bool:
        return self.queued

    def send_bytes(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    def close(self) -> None:
        self.closed = True


class _Lines:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def _record(self, level: str, message: str, *args: Any) -> None:
        self.lines.append((level, message % args if args else message))

    def warning(self, message: str, *args: Any) -> None:
        self._record("warning", message, *args)

    def error(self, message: str, *args: Any) -> None:
        self._record("error", message, *args)

    def info(self, message: str, *args: Any) -> None:
        self._record("info", message, *args)


def _stopping_supervisor(
    jobs: list[_ScheduledJob],
    *,
    pipe: _ScriptedPipe | None = None,
    **kwargs: Any,
) -> tuple[Tier3BatchValidatorSupervisor, list[str]]:
    """A supervisor holding one four-identity group per Job, helpers already exited."""

    results: queue.Queue[EndpointAttemptResult] = queue.Queue()
    supervisor = _supervisor(slots=4, workers=4 * len(jobs), results=results, **kwargs)
    for index, job in enumerate(jobs, start=1):
        group = _HelperGroup(
            index, 1, _ExitedHelper(), pipe or _ScriptedPipe(), job, threading.Lock(),
        )
        members = [
            _BatchWorkerGeneration(
                worker_id=worker_id, generation=1, process=group.process,
                connection=group.connection, job=job, send_lock=group.send_lock,
                group=group, phase=Tier3WorkerPhase.VALIDATING,
            )
            for worker_id in supervisor._member_ids(index)
        ]
        group.members = members
        group.lead = members[0]
        supervisor._groups[index] = group
        for member in members:
            supervisor._workers[member.worker_id] = member
    # The starter's threads exist in production; _stop_all joins them.
    supervisor._starter.start()
    stages: list[str] = []
    supervisor.stop_stage_observer = stages.append
    return supervisor, stages


def test_owner_stop_counts_a_job_that_empties_during_the_process_wait() -> None:
    # Mihomo outlives the Job termination by 0.6 s.  The Job-empty sampling
    # used to end after job_empty_wait_seconds and its handles were closed
    # before the process wait, so this group stayed "not empty" for good.
    job = _ScheduledJob(empty_at=time.monotonic() + 0.6)
    supervisor, stages = _stopping_supervisor([job], job_empty_wait_seconds=0.1)
    supervisor._stop_deadline_at = time.monotonic() + 2.0
    supervisor._stopping.set()
    supervisor._stop_all()
    assert supervisor._stop_status == "STOPPED", stages
    assert "owner_job_verification:1/1" in stages
    assert job.terminations == 1
    assert supervisor.snapshot().active_workers == 0


def test_owner_stop_with_no_budget_left_still_samples_every_group() -> None:
    # The caller already killed every Job and its deadline has passed when the
    # owner gets here: one sample per group proves them empty.
    jobs = [_ScheduledJob(empty_at=0.0), _ScheduledJob(empty_at=0.0)]
    supervisor, stages = _stopping_supervisor(jobs)
    supervisor._stop_deadline_at = time.monotonic() - 0.01
    supervisor._stopping.set()
    started = time.monotonic()
    supervisor._stop_all()
    elapsed = time.monotonic() - started
    assert supervisor._stop_status == "STOPPED", stages
    assert all(job.samples >= 1 for job in jobs)
    assert "owner_job_verification:2/2" in stages
    assert elapsed < 0.5, elapsed


def test_owner_stop_quarantines_a_live_job_and_returns_before_the_caller_deadline() -> None:
    gone = _ScheduledJob(empty_at=0.0)
    live = _ScheduledJob(empty_at=None)
    supervisor, stages = _stopping_supervisor([gone, live])
    supervisor._stop_deadline_at = time.monotonic() + 1.0
    supervisor._stopping.set()
    supervisor._stop_all()
    returned = time.monotonic()
    assert supervisor._stop_status == "STOP_FAILED_QUARANTINED"
    assert returned < supervisor._stop_deadline_at, returned - supervisor._stop_deadline_at
    assert live.terminations == 1 and live.samples >= 2
    evidence = [stage for stage in stages if stage.startswith("owner_final_evidence:")]
    assert evidence and "job_failed=1" in evidence[0] and "live_jobs=g2.1" in evidence[0], stages
    assert "owner_job_verification:1/2" in stages


def _owner_thread(supervisor: Tier3BatchValidatorSupervisor, *, late: float = 0.0) -> threading.Thread:
    """Stand-in for the reactor: runs the owner stop once stop is signalled."""

    def run() -> None:
        supervisor._running.set()
        supervisor._stopping.wait(10.0)
        time.sleep(late)
        supervisor._stop_all()

    thread = threading.Thread(target=run, name="owner-stand-in", daemon=True)
    thread.start()
    supervisor._thread = thread
    return thread


def test_a_quarantined_stop_logs_its_stages_once() -> None:
    lines = _Lines()
    supervisor, _stages = _stopping_supervisor([_ScheduledJob(empty_at=None)], logger=lines)
    owner = _owner_thread(supervisor)
    assert supervisor.stop(timeout_seconds=1.0) == "STOP_FAILED_QUARANTINED"
    assert not owner.is_alive(), "the owner must return inside the caller's deadline"
    warnings = [text for level, text in lines.lines if level == "warning"]
    assert len(warnings) == 1, lines.lines
    line = warnings[0]
    assert "stop STOP_FAILED_QUARANTINED" in line and "owner_alive=0" in line
    for stage in ("caller_entered@", "owner_entered@", "owner_final_evidence:job_failed=1", "live_jobs=g1.1"):
        assert stage in line, line
    assert len(line) < 2200
    # A repeated stop reports the same verdict without another line.
    assert supervisor.stop(timeout_seconds=0.2) == "STOP_FAILED_QUARANTINED"
    assert len([text for level, text in lines.lines if level == "warning"]) == 1


def test_a_stopped_stop_logs_nothing() -> None:
    lines = _Lines()
    supervisor, _stages = _stopping_supervisor(
        [_ScheduledJob(empty_at=time.monotonic() + 0.3)], logger=lines,
    )
    _owner_thread(supervisor)
    assert supervisor.stop(timeout_seconds=2.0) == "STOPPED"
    assert [text for level, text in lines.lines if level == "warning"] == []


def test_an_owner_that_overruns_the_deadline_is_named_in_the_stop_line() -> None:
    lines = _Lines()
    supervisor, _stages = _stopping_supervisor([_ScheduledJob(empty_at=0.0)], logger=lines)
    owner = _owner_thread(supervisor, late=1.5)
    try:
        assert supervisor.stop(timeout_seconds=0.6) == "STOP_FAILED_QUARANTINED"
        warnings = [text for level, text in lines.lines if level == "warning"]
        assert len(warnings) == 1 and "owner_alive=1" in warnings[0], lines.lines
        assert "caller_owner_join:alive" in warnings[0] and "owner_entered" not in warnings[0]
    finally:
        owner.join(10.0)


class _UnduplicableJob(_ScheduledJob):
    """A Job whose handle cannot be duplicated for the caller."""

    def duplicate(self) -> "_ScheduledJob":
        raise OSError("DuplicateHandle failed")


@pytest.mark.parametrize("budget", [0.3, 0.5, 0.6])
def test_a_quarantined_group_with_a_live_job_fails_a_short_stop(budget: float) -> None:
    # Group 2 was quarantined earlier because its Job did not empty (its
    # helper is gone, a Mihomo survived).  The owner returns inside its margin,
    # so the caller reaches its own Job readback; it used to read only the
    # Jobs of groups with a usable member and turned the failure into STOPPED.
    gone = _ScheduledJob(empty_at=0.0)
    stuck = _ScheduledJob(empty_at=None)
    supervisor, stages = _stopping_supervisor([gone, stuck])
    for member in supervisor._groups[2].members:
        member.phase = Tier3WorkerPhase.QUARANTINED
    owner = _owner_thread(supervisor)
    try:
        assert supervisor.stop(timeout_seconds=budget) == "STOP_FAILED_QUARANTINED", stages
    finally:
        owner.join(10.0)
    assert "caller_jobs_duplicated:2" in stages
    assert "caller_jobs_empty_confirmed" not in stages
    assert stuck.terminations >= 1


@pytest.mark.parametrize("budget", [0.3, 0.5])
def test_a_live_job_the_caller_never_held_keeps_the_owner_verdict(budget: float) -> None:
    gone = _ScheduledJob(empty_at=0.0)
    stuck = _UnduplicableJob(empty_at=None)
    supervisor, stages = _stopping_supervisor([gone, stuck])
    owner = _owner_thread(supervisor)
    try:
        assert supervisor.stop(timeout_seconds=budget) == "STOP_FAILED_QUARANTINED", stages
    finally:
        owner.join(10.0)
    assert "caller_jobs_duplicated:1" in stages
    assert supervisor.snapshot().stop_status == "STOP_FAILED_QUARANTINED"
    if "caller_jobs_empty_confirmed" in stages:
        # The caller's readback of the Job it held was empty: only the owner's
        # evidence about the other Job keeps the stop failed.
        assert "caller_conversion_refused:uncovered=w5.1" in stages, stages


# ---------------------------------------- bounded stop: owner responsiveness


def test_a_group_pipe_drain_yields_to_a_pending_stop() -> None:
    pipe = _ScriptedPipe(queued=True)
    supervisor, _stages = _stopping_supervisor([_ScheduledJob(empty_at=0.0)], pipe=pipe)
    lead = supervisor._groups[1].lead
    handled: list[int] = []

    def handle(worker) -> None:
        handled.append(worker.worker_id)
        if len(handled) == 3:
            supervisor._stopping.set()

    supervisor._handle_pipe = handle  # type: ignore[method-assign]
    # A caller's stop lands while the owner is working through a burst.
    assert supervisor._drain_worker_pipe(lead, first_message_ready=True) == 3
    assert supervisor._drain_worker_pipe(lead, first_message_ready=True) == 0
    # Without a stop the same burst is drained to its bound, as before.
    supervisor._stopping.clear()
    handled.clear()
    supervisor._handle_pipe = lambda worker: handled.append(worker.worker_id)  # type: ignore[method-assign]
    assert supervisor._drain_worker_pipe(lead, first_message_ready=True, max_messages=32) == 32


def test_deadline_sweep_is_skipped_once_stop_is_signalled() -> None:
    pipe = _ScriptedPipe()
    supervisor, _stages = _stopping_supervisor([_ScheduledJob(empty_at=0.0)], pipe=pipe)
    member = supervisor._groups[1].members[1]
    member.command = _command(2, epoch="sweep")
    member.deadline_at = time.monotonic() - 1.0
    member.deadline_phase = "probe"
    drains: list[int] = []
    supervisor._drain_worker_pipe = (  # type: ignore[method-assign]
        lambda worker, **_kwargs: drains.append(worker.worker_id) or 0
    )
    supervisor._stopping.set()
    supervisor._check_deadlines()
    assert drains == [] and pipe.sent == []
    assert member.phase is Tier3WorkerPhase.VALIDATING
    # Outside a stop the expired lease is drained, sealed and cancelled as before.
    supervisor._stopping.clear()
    supervisor._check_deadlines()
    assert drains == [2] and len(pipe.sent) == 1
    assert member.phase is Tier3WorkerPhase.CANCELLING


def test_stop_returns_stopped_while_the_owner_is_behind_on_busy_group_pipes(monkeypatch) -> None:
    # Per-identity heartbeats make each group Pipe carry K frames every 0.2 s
    # and every frame costs the owner 100 ms (a starved parent), so each Pipe
    # stays full and one drain is 32 frames (3.2 s) per group.  The owner used
    # to finish those drains before it looked at the stop and was still busy
    # at the caller's deadline.
    import subprocess

    monkeypatch.setenv("TIER3_BATCH_FAKE_SLOT_HEARTBEATS", "1")
    results: queue.Queue[EndpointAttemptResult] = queue.Queue()
    supervisor = _supervisor(slots=4, workers=8, results=results)
    supervisor.start()
    try:
        _wait_until(lambda: supervisor.snapshot().idle_workers == 8, timeout=20.0)
        pids = {int(item["helper_pid"]) for item in supervisor.snapshot().workers}
        handle = supervisor._handle_pipe

        def slow(worker) -> None:
            time.sleep(0.1)
            handle(worker)

        supervisor._handle_pipe = slow  # type: ignore[method-assign]
        time.sleep(1.5)
    finally:
        started = time.monotonic()
        status = supervisor.stop(timeout_seconds=1.5)
        elapsed = time.monotonic() - started
    assert status == "STOPPED", (status, elapsed)
    assert elapsed < 2.0, elapsed
    listing = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV"], capture_output=True, text=True,
    ).stdout
    for pid in pids:
        assert f'"{pid}"' not in listing, f"helper {pid} survived stop"
