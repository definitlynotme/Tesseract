"""IPC contracts of the batch validator backend.

* The helper's event loop never writes its Pipe: a parent that stops reading
  delays delivery, but neither stalls the K probes nor inflates the latency and
  verdicts they measure, and frames still arrive in the order they were made.
* One ``group_heartbeat`` per process is fanned out to exactly the handling a
  per-identity ``heartbeat`` gets (liveness, CANCELLING completion).
* A group's identities become capacity at ``batch_ready`` (layout installed),
  never at the controller ``ready``; a helper that never gets there is retired
  by the startup deadline.
* The startup cohort is counted in processes: four groups spawn at once.

The first group runs the real ``BatchValidatorHelperRuntime`` in-process over
a real Windows Pipe, with Mihomo replaced by one loopback HTTP proxy per slot.
The supervisor tests run the real ``Tier3BatchValidatorSupervisor`` against the
deterministic multi-slot fake helper.
"""
from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import queue
import socket
import socketserver
import threading
import time
from dataclasses import replace
from typing import Any

import pytest

import tier3_batch_validator_runtime
from models import Node
from test_support.tier3_batch_deterministic_helper import (
    batch_deterministic_helper_bootstrap,
    finish_like_a_finished_helper,
)
from tier3_batch_validator_helper import batch_helper_process_bootstrap
from tier3_batch_validator_runtime import BatchValidatorHelperRuntime, _PipeSender
from tier3_batch_validator_supervisor import (
    Tier3BatchValidatorSupervisor,
    _BatchWorkerGeneration,
    _HelperGroup,
)
from tier3_validation_protocol import (
    PROTOCOL_SCHEMA_VERSION,
    EndpointAttemptKind,
    EndpointAttemptResult,
    Tier3WorkerPhase,
    ValidateEndpointCmd,
    canonical_json,
    dataclass_payload,
    message_bytes,
    parse_message,
)
from tier3_validator_helper import BOOTSTRAP_READY_MESSAGE

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Pipe semantics")


# ---------------------------------------------------------------- fixtures


def _wait_until(predicate, timeout: float = 10.0, interval: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("condition did not become true")


def _node(index: int, **extra: Any) -> Node:
    return Node(
        name=f"ipc-candidate-{index}",
        protocol="http",
        address="127.0.0.1",
        port=10000 + index,
        tier="tier3",
        extra={"tier3_source_root_id": "local-root", "tier3_source_unit_family": "http", **extra},
    )


def _command(
    index: int,
    *,
    epoch: str,
    worker_id: int = 0,
    worker_generation: int = 0,
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
        endpoint="http://probe.invalid/generate_204",
        facility_budget_seconds=0.5,
        probe_budget_seconds=probe_budget,
        candidate_hard_cap_seconds=hard_cap,
        worker_id=worker_id,
        worker_generation=worker_generation,
    )


class _DelayedProxyHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(10.0)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            data += chunk
        time.sleep(self.server.delay)  # type: ignore[attr-defined]
        self.request.sendall(
            b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        )


class _DelayedProxy(socketserver.ThreadingTCPServer):
    """A loopback proxy that answers every request with 204 after ``delay``.

    It stands in for one slot's Mihomo listener and runs on its own threads,
    so its answer time is independent of the helper's event loop.
    """

    daemon_threads = True

    def __init__(self, delay: float) -> None:
        super().__init__(("127.0.0.1", 0), _DelayedProxyHandler)
        self.delay = float(delay)
        self.port = int(self.server_address[1])
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.shutdown()
        self.server_close()


class _LoopbackBatchRuntime(BatchValidatorHelperRuntime):
    """The production batch runtime with its Mihomo replaced by loopback proxies.

    Only the facility seams are stubbed (controller start, layout, provider
    install); dispatch, validate_endpoint, the sweep, heartbeats, cancel and
    the Pipe writer are the production code.
    """

    async def start(self) -> None:
        self._send(
            "ready",
            {
                "worker_id": self.worker_id,
                "worker_generation": self.generation,
                "helper_pid": os.getpid(),
                "mihomo_pid": 0,
                "mixed_port": self.mixed_port,
                "controller_port": self.controller_port,
            },
        )

    async def _widen_controller_session(self) -> None:
        return None

    async def _install_layout(self) -> None:
        return None

    async def _install_candidate(self, slot, node, candidate_digest, facility_budget):  # type: ignore[override]
        slot.bound_node = node
        slot.installed_digest = str(candidate_digest)
        return True, "", 0


def _free_port(excluded: set[int]) -> int:
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if port not in excluded:
            return port


def _runtime_payload(listener_ports: list[int], tmp_path) -> dict[str, Any]:
    excluded = set(listener_ports)
    mixed_port = _free_port(excluded)
    controller_port = _free_port(excluded | {mixed_port})
    return {
        "worker_id": 1,
        "worker_generation": 1,
        "binary_path": "unused-by-loopback-runtime",
        "probe_urls": ["http://probe.invalid/generate_204"],
        "expected_statuses": [204],
        "probe_timeout_seconds": 3.0,
        "slow_threshold_ms": 3000,
        "temp_parent": str(tmp_path),
        "mixed_port": mixed_port,
        "controller_port": controller_port,
        "member_worker_ids": list(range(1, len(listener_ports) + 1)),
        "listener_ports": list(listener_ports),
    }


def _start_runtime(
    child: Any, listener_ports: list[int], tmp_path, *, process: Any = None,
    runtime_class: type = _LoopbackBatchRuntime,
) -> tuple[threading.Thread, list[BaseException]]:
    payload = _runtime_payload(listener_ports, tmp_path)
    errors: list[BaseException] = []

    def target() -> None:
        async def main() -> None:
            runtime = runtime_class(child, payload)
            # Stands in for the shared Mihomo process (pid, returncode).
            runtime.process = process
            try:
                await runtime.run()
            finally:
                await runtime.close()

        try:
            asyncio.run(main())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=target, name="loopback-batch-runtime", daemon=True)
    thread.start()
    return thread, errors


def _start_bootstrap(
    parent: Any, child: Any, listener_ports: list[int], tmp_path, monkeypatch, *, process: Any,
) -> tuple[threading.Thread, list[BaseException]]:
    """Run the production batch bootstrap on ``child`` with the loopback runtime.

    Unlike ``_start_runtime`` this keeps everything the bootstrap does after
    the runtime returns: ``bootstrap_error`` written on the raw connection,
    then the connection closed.  The parent side of the START handshake is
    done here.
    """

    payload = _runtime_payload(listener_ports, tmp_path)

    async def loopback_helper(connection: Any, start_payload: dict[str, Any], *, prewarmed: Any = None) -> None:
        runtime = _LoopbackBatchRuntime(connection, start_payload, prewarmed=prewarmed)
        runtime.process = process
        try:
            await runtime.run()
        finally:
            await runtime.close()

    # The bootstrap imports the runtime entry point only when it runs.
    monkeypatch.setattr(tier3_batch_validator_runtime, "run_batch_validator_helper", loopback_helper)
    errors: list[BaseException] = []

    def target() -> None:
        try:
            batch_helper_process_bootstrap(child)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=target, name="loopback-batch-bootstrap", daemon=True)
    thread.start()
    assert parent.poll(10.0) and parent.recv_bytes() == BOOTSTRAP_READY_MESSAGE
    parent.send_bytes(json.dumps(
        {"schema": PROTOCOL_SCHEMA_VERSION, "kind": "start", "payload": payload},
    ).encode("utf-8"))
    return thread, errors


def _read(parent: Any, *, until, timeout: float) -> list[tuple[int, str, dict[str, Any]]]:
    """Read frames until ``until(frames)`` holds; each frame is (arrival_ns, kind, payload)."""

    frames: list[tuple[int, str, dict[str, Any]]] = []
    deadline = time.monotonic() + timeout
    while not until(frames):
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            raise AssertionError(
                f"helper frames incomplete: {[kind for _, kind, _ in frames]}"
            )
        if parent.poll(min(0.05, remaining)):
            kind, payload = parse_message(parent.recv_bytes())
            frames.append((time.monotonic_ns(), kind, payload))
    return frames


def _count(kind: str, expected: int):
    return lambda frames: sum(1 for _, item, _ in frames if item == kind) >= expected


# ------------------------------------------- helper side: the Pipe writer


class _GatedConnection:
    """A Pipe end whose writes block until ``gate`` opens (a parent not reading)."""

    def __init__(self, *, fail_after: int | None = None) -> None:
        self.gate = threading.Event()
        self.sent: list[bytes] = []
        self.fail_after = fail_after

    def send_bytes(self, data: bytes) -> None:
        self.gate.wait(10.0)
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise BrokenPipeError(32, "parent end closed")
        self.sent.append(bytes(data))


def test_pipe_sender_never_blocks_the_caller_and_keeps_fifo_order() -> None:
    connection = _GatedConnection()
    sender = _PipeSender(connection)
    started = time.monotonic()
    frames = [b"frame-%03d" % index for index in range(200)]
    for frame in frames:
        sender.send(frame)
    enqueue_seconds = time.monotonic() - started
    assert enqueue_seconds < 0.25, enqueue_seconds
    assert connection.sent == [], "nothing can be written while the parent is not reading"
    assert sender.flush(0.05) is False
    connection.gate.set()
    assert sender.flush(5.0) is True
    assert connection.sent == frames
    assert sender.close() is True


def _close_in_thread(sender: _PipeSender) -> tuple[threading.Thread, list[Any]]:
    """Run the unbounded ``close()`` where a regression cannot hang the test."""

    outcome: list[Any] = []

    def target() -> None:
        started = time.monotonic()
        outcome.append(sender.close())
        outcome.append(time.monotonic() - started)

    thread = threading.Thread(target=target, name="pipe-sender-close", daemon=True)
    thread.start()
    return thread, outcome


def test_pipe_sender_close_delivers_every_queued_frame_before_returning() -> None:
    connection = _GatedConnection()
    sender = _PipeSender(connection)
    frames = [b"late-%d" % index for index in range(20)]
    for frame in frames:
        sender.send(frame)
    closer, outcome = _close_in_thread(sender)
    # close() has no deadline of its own: a parent that is alive but slow is
    # waited for, exactly like a synchronous write.
    time.sleep(0.6)
    assert closer.is_alive() and connection.sent == []
    connection.gate.set()
    closer.join(5.0)
    assert outcome and outcome[0] is True, outcome
    assert connection.sent == frames


def test_pipe_sender_close_returns_at_once_when_the_parent_is_gone() -> None:
    # The unbounded close cannot outlive the parent: closing the parent end
    # fails the blocked write, which ends the sender thread.
    parent, child = multiprocessing.Pipe()
    sender = _PipeSender(child)
    for index in range(12):
        sender.send(message_bytes("endpoint_result", {"worker_id": index, "pad": "x" * 2000}))
    time.sleep(0.3)
    assert sender.flush(0.0) is False, "the Pipe must be full for this test to mean anything"
    closer, outcome = _close_in_thread(sender)
    time.sleep(0.3)
    assert closer.is_alive()
    parent.close()
    closer.join(10.0)
    assert not closer.is_alive()
    assert outcome[0] is True
    with pytest.raises(OSError):
        sender.raise_if_failed()
    child.close()


def test_pipe_sender_failure_surfaces_to_the_next_send() -> None:
    connection = _GatedConnection(fail_after=3)
    connection.gate.set()
    sender = _PipeSender(connection)
    for index in range(5):
        sender.send(b"%d" % index)
    assert sender.flush(5.0) is False
    assert connection.sent == [b"0", b"1", b"2"]
    with pytest.raises(BrokenPipeError):
        sender.send(b"after-failure")
    with pytest.raises(BrokenPipeError):
        sender.raise_if_failed()
    assert sender.close() is True


def test_a_parent_that_stops_reading_for_three_seconds_does_not_stall_probes(tmp_path) -> None:
    # Half the candidates answer at 0.3 s, half at 1.2 s, inside a 2.0 s
    # budget.  With Pipe writes on the event loop, the Pipe fills during the
    # stall, the loop freezes, and the 1.2 s answers are only read at ~3 s:
    # four live candidates come back right-censored at 3000 ms.
    slots = 8
    answer_after = [0.3 if index < slots // 2 else 1.2 for index in range(slots)]
    budget = 2.0
    stall_seconds = 3.0
    proxies = [_DelayedProxy(delay) for delay in answer_after]
    parent, child = multiprocessing.Pipe()
    thread, errors = _start_runtime(child, [proxy.port for proxy in proxies], tmp_path)
    try:
        startup = _read(parent, until=_count("batch_ready", 1), timeout=15.0)
        assert [kind for _, kind, _ in startup] == ["ready", "batch_ready"]
        for worker_id in range(1, slots + 1):
            parent.send_bytes(message_bytes("validate_endpoint", dataclass_payload(_command(
                worker_id, epoch="stall", worker_id=worker_id, worker_generation=1,
                probe_budget=budget,
            ))))
        stall_started_ns = time.monotonic_ns()
        # The parent stops reading.  8 slots x (3 phases + 1 result) plus
        # heartbeats is far more than the 8 KiB the Pipe buffers.
        time.sleep(stall_seconds)
        resumed_ns = time.monotonic_ns()

        def drained(frames) -> bool:
            # Every result, plus the first heartbeat made after the stall, so
            # everything produced during the stall has been read.
            return _count("endpoint_result", slots)(frames) and any(
                kind == "group_heartbeat" and int(payload["monotonic_ns"]) >= resumed_ns
                for _, kind, payload in frames
            )

        frames = _read(parent, until=drained, timeout=15.0)
        parent.send_bytes(message_bytes("stop", {}))
        thread.join(10.0)
    finally:
        for proxy in proxies:
            proxy.close()
        parent.close()
    assert not thread.is_alive()
    assert errors == []

    results = [payload for _, kind, payload in frames if kind == "endpoint_result"]
    assert sorted(item["worker_id"] for item in results) == list(range(1, slots + 1))
    for item in results:
        fact = item["attempt_facts"][0]
        # The verdict is the one the candidate earned, measured on time.
        assert item["kind"] == EndpointAttemptKind.PASS.value, (item["kind"], item["reason"])
        answered_ms = answer_after[int(item["worker_id"]) - 1] * 1000
        assert fact["elapsed_ms"] < answered_ms + 500, (fact["elapsed_ms"], answered_ms)
        created_after_transport_ms = (
            int(item["yield_created_monotonic_ns"]) - int(fact["transport_started_monotonic_ns"])
        ) / 1e6
        assert created_after_transport_ms < budget * 1000, created_after_transport_ms
        # ...and it was produced while the parent was not reading.
        assert int(item["yield_created_monotonic_ns"]) < resumed_ns

    # The loop kept its 0.5 s heartbeat cadence through the stall.
    beats = [
        payload for _, kind, payload in frames
        if kind == "group_heartbeat" and stall_started_ns <= int(payload["monotonic_ns"]) < resumed_ns
    ]
    assert len(beats) >= 4, len(beats)
    assert all(len(beat["slots"]) == slots for beat in beats)

    # Frames arrive exactly in the order the loop produced them.
    per_worker: dict[int, list[str]] = {}
    for _, kind, payload in frames:
        if kind == "slice_phase":
            per_worker.setdefault(int(payload["worker_id"]), []).append(payload["phase"])
        elif kind == "endpoint_result":
            per_worker.setdefault(int(payload["worker_id"]), []).append("result")
    assert per_worker == {
        worker_id: ["dispatch_received", "facility_started", "transport_started", "result"]
        for worker_id in range(1, slots + 1)
    }
    stamped_at_send = [
        int(payload["monotonic_ns"]) for _, kind, payload in frames
        if kind in {"slice_phase", "group_heartbeat"}
    ]
    assert stamped_at_send == sorted(stamped_at_send)


def test_stop_delivers_every_cancel_confirmation_before_the_helper_exits(tmp_path) -> None:
    slots = 4
    proxies = [_DelayedProxy(8.0) for _ in range(slots)]
    parent, child = multiprocessing.Pipe()
    thread, errors = _start_runtime(child, [proxy.port for proxy in proxies], tmp_path)
    try:
        _read(parent, until=_count("batch_ready", 1), timeout=15.0)
        for worker_id in range(1, slots + 1):
            parent.send_bytes(message_bytes("validate_endpoint", dataclass_payload(_command(
                worker_id, epoch="stop", worker_id=worker_id, worker_generation=1,
            ))))
        _read(parent, until=_count("slice_phase", 3 * slots), timeout=10.0)
        parent.send_bytes(message_bytes("stop", {}))
        # Nothing is read until the helper thread has fully exited: every
        # frame it produced must already sit in the Pipe or have been
        # written before close() returned.
        thread.join(15.0)
        assert not thread.is_alive()
        frames = []
        while parent.poll(0.2):
            frames.append(parse_message(parent.recv_bytes()))
    finally:
        for proxy in proxies:
            proxy.close()
        parent.close()
    assert errors == []
    confirmed = sorted(
        int(payload["worker_id"]) for kind, payload in frames
        if kind == "clean_confirmed" and payload["reason"] == "helper_stop"
    )
    assert confirmed == list(range(1, slots + 1))



class _StoppableFacility:
    """Stands in for the shared Mihomo: exits a moment after it is asked to."""

    pid = 4343

    def __init__(self, events: list[str], *, exit_after: float = 0.1) -> None:
        self.events = events
        self.exit_after = float(exit_after)
        self.terminations = 0
        self._exit_at: float | None = None

    @property
    def returncode(self) -> int | None:
        if self._exit_at is not None and time.monotonic() >= self._exit_at:
            return 1
        return None

    def terminate(self) -> None:
        self.terminations += 1
        self.events.append("facility_terminate")
        if self._exit_at is None:
            self._exit_at = time.monotonic() + self.exit_after

    kill = terminate

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0.005)
        return int(self.returncode)


class _RecordingBatchRuntime(_LoopbackBatchRuntime):
    """Also records, in the facility's event list, the kind of every frame produced."""

    def _send(self, kind: str, payload: dict[str, Any]) -> None:
        events = getattr(self.process, "events", None)
        if events is not None:
            events.append(kind)
        super()._send(kind, payload)


def test_stop_ends_the_shared_facility_first_and_still_confirms_every_slot(tmp_path) -> None:
    # Nothing validates after stop, so the shared Mihomo is asked to exit before
    # the K leases unwind: its teardown overlaps theirs instead of following
    # it.  Every slot still reports its stop-time clean confirmation, and the
    # facility is asked exactly once (a second TerminateProcess on an exiting
    # process would end close()'s wait for it early).
    slots = 8
    events: list[str] = []
    facility = _StoppableFacility(events)
    proxies = [_DelayedProxy(8.0) for _ in range(slots)]
    parent, child = multiprocessing.Pipe()
    thread, errors = _start_runtime(
        child, [proxy.port for proxy in proxies], tmp_path,
        process=facility, runtime_class=_RecordingBatchRuntime,
    )
    try:
        _read(parent, until=_count("batch_ready", 1), timeout=15.0)
        for worker_id in range(1, slots + 1):
            parent.send_bytes(message_bytes("validate_endpoint", dataclass_payload(_command(
                worker_id, epoch="facility-first", worker_id=worker_id, worker_generation=1,
            ))))
        _read(parent, until=_count("slice_phase", 3 * slots), timeout=10.0)
        stop_sent = time.monotonic()
        parent.send_bytes(message_bytes("stop", {}))
        frames = _read(parent, until=_count("clean_confirmed", slots), timeout=10.0)
        thread.join(10.0)
        stopped_after = time.monotonic() - stop_sent
    finally:
        for proxy in proxies:
            proxy.close()
        parent.close()
    assert not thread.is_alive() and errors == []
    assert sorted(
        int(payload["worker_id"]) for _, kind, payload in frames
        if kind == "clean_confirmed" and payload["reason"] == "helper_stop"
    ) == list(range(1, slots + 1))
    assert facility.terminations == 1
    assert "facility_terminate" in events
    assert events.index("facility_terminate") < events.index("clean_confirmed"), events
    assert stopped_after < 2.0, stopped_after


def test_close_releases_every_slot_under_one_bound(tmp_path) -> None:
    # Eight slots whose detached probe tasks refuse to unwind for 1.5 s.
    # Cleaning them one after another (0.25 s each) kept close() busy for
    # ~1.5 s; one shared bound keeps it near 0.2 s.
    connection = _GatedConnection()
    connection.gate.set()
    ports: set[int] = set()
    for _ in range(8):
        ports.add(_free_port(ports))
    payload = _runtime_payload(sorted(ports), tmp_path)

    async def main() -> tuple[float, int]:
        runtime = _LoopbackBatchRuntime(connection, payload)
        runtime.slots = {
            worker_id: tier3_batch_validator_runtime._BatchProbeSlot(runtime, worker_id, port)
            for worker_id, port in zip(runtime.member_ids, runtime.listener_ports)
        }
        release_at = time.monotonic() + 1.5

        async def stubborn() -> None:
            while True:
                try:
                    await asyncio.sleep(max(0.0, release_at - time.monotonic()))
                    return
                except asyncio.CancelledError:
                    if time.monotonic() >= release_at:
                        raise

        tasks = [asyncio.create_task(stubborn()) for _ in runtime.slots]
        await asyncio.sleep(0.02)  # let every task enter its loop first
        for slot, task in zip(runtime.slots.values(), tasks):
            slot._detach_probe_task(task)
        started = time.monotonic()
        await runtime.close()
        elapsed = time.monotonic() - started
        await asyncio.gather(*tasks, return_exceptions=True)
        return elapsed, sum(1 for slot in runtime.slots.values() if slot.active_task is not None)

    elapsed, active = asyncio.run(main())
    assert active == 0
    assert elapsed < 0.8, elapsed


def test_a_finished_helper_process_skips_interpreter_finalization(tmp_path) -> None:
    from tier3_batch_validator_helper import _exit_finished_helper_process

    marker = tmp_path / "helper-marker.txt"
    process = multiprocessing.get_context("spawn").Process(
        target=finish_like_a_finished_helper, args=(str(marker),),
    )
    process.start()
    process.join(60.0)
    assert process.exitcode == 0
    assert not marker.exists(), marker.read_text(encoding="utf-8")
    # A bootstrap hosted on another thread (as the tests above host it) returns.
    outcome: list[str] = []
    runner = threading.Thread(
        target=lambda: (_exit_finished_helper_process(), outcome.append("returned")),
        daemon=True,
    )
    runner.start()
    runner.join(5.0)
    assert outcome == ["returned"]


class _SharedMihomo:
    pid = 4242

    def __init__(self) -> None:
        self.returncode: int | None = None


def test_a_dead_shared_mihomo_still_delivers_every_earned_verdict_first(tmp_path, monkeypatch) -> None:
    # Four slots earn a verdict while the parent is not reading, so part of
    # what they produced is still queued in the helper when the shared
    # facility dies.  The parent then stays away for 6 s (a stalled reactor),
    # longer than any delivery bound the helper might apply.  Like the
    # synchronous writes did, the helper must write all of it and only then
    # let the bootstrap report bootstrap_error and close the Pipe: the parent
    # gets every earned verdict, then the error, then EOF - never a pipe lost
    # in the middle, which would turn earned verdicts into facility failures.
    slots = 8
    proxies = [_DelayedProxy(0.2 if index < 4 else 8.0) for index in range(slots)]
    mihomo = _SharedMihomo()
    parent, child = multiprocessing.Pipe()
    frames: list[tuple[str, dict[str, Any]]] = []
    try:
        thread, errors = _start_bootstrap(
            parent, child, [proxy.port for proxy in proxies], tmp_path, monkeypatch, process=mihomo,
        )
        _read(parent, until=_count("batch_ready", 1), timeout=15.0)
        for worker_id in range(1, slots + 1):
            parent.send_bytes(message_bytes("validate_endpoint", dataclass_payload(_command(
                worker_id, epoch="death", worker_id=worker_id, worker_generation=1,
            ))))
        time.sleep(1.5)
        mihomo.returncode = 1
        time.sleep(6.0)
        # The Pipe is full: the helper has seen the death and is still
        # delivering what it owes, however long the parent stays away.
        assert thread.is_alive()
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            try:
                if parent.poll(0.05):
                    frames.append(parse_message(parent.recv_bytes()))
                elif not thread.is_alive():
                    break
            except (EOFError, OSError):
                frames.append(("<eof>", {}))
                break
        thread.join(5.0)
        assert not thread.is_alive()
    finally:
        for proxy in proxies:
            proxy.close()
        parent.close()
    assert len(errors) == 1 and "shared Mihomo exited" in str(errors[0]), errors
    results = {
        int(payload["worker_id"]): payload["kind"] for kind, payload in frames if kind == "endpoint_result"
    }
    assert results == {worker_id: EndpointAttemptKind.PASS.value for worker_id in range(1, 5)}
    kinds = [kind for kind, _ in frames]
    # Every earned verdict precedes the bootstrap's report, which is the last
    # frame before the helper's end of the Pipe closes.
    assert kinds[-2:] == ["bootstrap_error", "<eof>"], kinds[-6:]
    assert "shared Mihomo exited" in frames[-2][1]["error"]


def test_a_lost_parent_ends_the_helper_like_a_failed_synchronous_write(tmp_path) -> None:
    proxies = [_DelayedProxy(0.1) for _ in range(2)]
    parent, child = multiprocessing.Pipe()
    thread, errors = _start_runtime(child, [proxy.port for proxy in proxies], tmp_path)
    try:
        _read(parent, until=_count("batch_ready", 1), timeout=15.0)
        parent.close()
        thread.join(10.0)
    finally:
        for proxy in proxies:
            proxy.close()
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], (OSError, EOFError)), errors


# ------------------------------------------ parent side: heartbeat fan-out


def _synthetic_supervisor(results: list, capacities: list) -> tuple[Tier3BatchValidatorSupervisor, _HelperGroup]:
    """A supervisor holding one four-identity group in fixed phases (no processes)."""

    supervisor = Tier3BatchValidatorSupervisor(
        slots_per_process=4,
        binary_path="unused-by-fixture",
        probe_urls=["http://127.0.0.1/generate_204"],
        expected_statuses={204},
        probe_timeout_seconds=3.0,
        slow_threshold_ms=3000,
        attempt_sink=results.append,
        capacity_sink=capacities.append,
        min_workers=4,
        max_workers=4,
        helper_target=batch_deterministic_helper_bootstrap,
    )
    group = _HelperGroup(1, 1, None, None, None, threading.Lock())
    members = [
        _BatchWorkerGeneration(
            worker_id=worker_id, generation=1, process=None, connection=None, job=None,
            send_lock=group.send_lock, group=group, phase=Tier3WorkerPhase.IDLE,
            helper_pid=100, mihomo_pid=200,
        )
        for worker_id in range(1, 5)
    ]
    group.members = members
    group.lead = members[0]
    supervisor._groups[1] = group
    for member in members:
        supervisor._workers[member.worker_id] = member
    # Identity 2: sealed and cancelling; its helper reports it idle.
    # Identity 3: sealed and cancelling; its slot is poisoned and reports 1.
    # Identity 4: sealed and cancelling; the heartbeat names another lease.
    for member in members[1:]:
        command = _command(member.worker_id, epoch="fanout", worker_id=member.worker_id, worker_generation=1)
        member.command = command
        member.phase = Tier3WorkerPhase.CANCELLING
        member.sealed_lease_id = command.lease_id
        member.cancel_reason = "candidate_probe_deadline"
        member.cancel_requested_monotonic_ns = 1_000
        member.transport_started_at = time.monotonic()
        member.cancel_deadline_at = time.monotonic() + 60.0
        member.heartbeat_at = 0.0
    return supervisor, group


_FANOUT_ENTRIES = [
    [1, "", 0],
    [2, "fanout-2", 0],
    [3, "fanout-3", 1],
    [4, "some-other-lease", 0],
]


def _member_state(group: _HelperGroup) -> list[tuple]:
    return [
        (member.worker_id, member.phase, member.command is None, member.mihomo_pid, member.heartbeat_at > 0.0)
        for member in group.members
    ]


def _result_shape(results: list[EndpointAttemptResult]) -> list[tuple]:
    return [
        (item.worker_id, item.lease_id, item.kind, item.reason, item.clean_confirmed, len(item.attempt_facts))
        for item in results
    ]


def test_group_heartbeat_fans_out_exactly_like_per_identity_heartbeats() -> None:
    frame = {"worker_generation": 1, "helper_pid": 100, "mihomo_pid": 201, "monotonic_ns": 2_000}

    slot_results: list = []
    slot_capacities: list = []
    per_slot, per_slot_group = _synthetic_supervisor(slot_results, slot_capacities)
    for worker_id, lease_id, active in _FANOUT_ENTRIES:
        per_slot._handle_heartbeat(
            per_slot._workers[worker_id],
            {**frame, "worker_id": worker_id, "lease_id": lease_id, "active_probe_count": active},
        )

    group_results: list = []
    group_capacities: list = []
    grouped, grouped_group = _synthetic_supervisor(group_results, group_capacities)
    grouped._handle_group_heartbeat(grouped_group, {**frame, "worker_id": 1, "slots": _FANOUT_ENTRIES})

    assert _member_state(grouped_group) == _member_state(per_slot_group)
    assert _result_shape(group_results) == _result_shape(slot_results)
    assert group_capacities == slot_capacities == [1]
    # Only identity 2 completed; the poisoned slot and the foreign lease did not.
    assert [item.worker_id for item in group_results] == [2]
    assert group_results[0].kind is EndpointAttemptKind.RIGHT_CENSORED
    assert group_results[0].reason == "candidate_probe_deadline"
    phases = {member.worker_id: member.phase for member in grouped_group.members}
    assert phases == {
        1: Tier3WorkerPhase.IDLE,
        2: Tier3WorkerPhase.IDLE,
        3: Tier3WorkerPhase.CANCELLING,
        4: Tier3WorkerPhase.CANCELLING,
    }
    assert {member.mihomo_pid for member in grouped_group.members} == {201}


def test_group_heartbeat_naming_a_foreign_identity_retires_the_group() -> None:
    results: list = []
    capacities: list = []
    supervisor, group = _synthetic_supervisor(results, capacities)
    retired: list[str] = []
    supervisor._retire_group = lambda target, reason, **_kwargs: retired.append(reason)  # type: ignore[method-assign]
    supervisor._handle_group_heartbeat(
        group,
        {"worker_generation": 1, "monotonic_ns": 2_000, "slots": [[1, "", 0], [99, "", 0]]},
    )
    assert retired == ["helper_protocol_unknown_worker:heartbeat"]


# ------------------------------------- parent side: real supervisor + fake


def _supervisor(
    *,
    slots: int,
    workers: int,
    results: "queue.Queue[EndpointAttemptResult]",
    capacities: list | None = None,
    **kwargs: Any,
) -> Tier3BatchValidatorSupervisor:
    return Tier3BatchValidatorSupervisor(
        slots_per_process=slots,
        binary_path="unused-by-fixture",
        probe_urls=["http://127.0.0.1/generate_204"],
        expected_statuses={204},
        probe_timeout_seconds=3.0,
        slow_threshold_ms=3000,
        attempt_sink=results.put_nowait,
        capacity_sink=(capacities if capacities is not None else []).append,
        min_workers=workers,
        max_workers=workers,
        helper_target=batch_deterministic_helper_bootstrap,
        **kwargs,
    )


@pytest.mark.parametrize("wire", ["group", "slot"])
def test_a_heartbeat_completes_a_cancelling_identity_without_clean_confirmed(monkeypatch, wire) -> None:
    if wire == "slot":
        monkeypatch.setenv("TIER3_BATCH_FAKE_SLOT_HEARTBEATS", "1")
    else:
        monkeypatch.delenv("TIER3_BATCH_FAKE_SLOT_HEARTBEATS", raising=False)
    results: queue.Queue[EndpointAttemptResult] = queue.Queue()
    # The cancel window is long enough that only a heartbeat can finish it.
    supervisor = _supervisor(slots=4, workers=4, results=results, cleanup_seconds=5.0)
    supervisor.start()
    try:
        _wait_until(lambda: supervisor.snapshot().idle_workers == 4, timeout=20.0)
        before = {int(item["helper_pid"]) for item in supervisor.snapshot().workers}
        started = time.monotonic()
        assert supervisor.submit(_command(
            1, epoch=f"silent-{wire}",
            node=_node(1, fixture_validation_outcome="pass", fixture_delay_seconds=6.0, fixture_cancel_silent=True),
            hard_cap=0.5, probe_budget=0.2,
        ))
        result = results.get(timeout=10.0)
        elapsed = time.monotonic() - started
        assert result.kind is EndpointAttemptKind.RIGHT_CENSORED
        assert result.reason == "candidate_probe_deadline"
        assert result.clean_confirmed is True
        # Completed by a heartbeat, well before the 5 s cancel window.
        assert elapsed < 4.0, elapsed
        _wait_until(lambda: supervisor.snapshot().idle_workers == 4, timeout=5.0)
        snapshot = supervisor.snapshot()
        assert {int(item["helper_pid"]) for item in snapshot.workers} == before
        assert supervisor.batch_metrics()["slot_quarantines"] == 0
        assert supervisor.batch_metrics()["group_retirements"] == 0
    finally:
        assert supervisor.stop(timeout_seconds=3.0) == "STOPPED"


def test_no_capacity_and_no_lease_before_batch_ready(monkeypatch) -> None:
    monkeypatch.setenv("TIER3_BATCH_FAKE_LAYOUT_SECONDS", "2.5")
    results: queue.Queue[EndpointAttemptResult] = queue.Queue()
    grants: list[tuple[float, int]] = []
    supervisor = Tier3BatchValidatorSupervisor(
        slots_per_process=4,
        binary_path="unused-by-fixture",
        probe_urls=["http://127.0.0.1/generate_204"],
        expected_statuses={204},
        probe_timeout_seconds=3.0,
        slow_threshold_ms=3000,
        attempt_sink=results.put_nowait,
        capacity_sink=lambda delta: grants.append((time.monotonic(), delta)),
        min_workers=4,
        max_workers=4,
        helper_target=batch_deterministic_helper_bootstrap,
    )
    supervisor.start()
    try:
        _wait_until(
            lambda: {item["startup_phase"] for item in supervisor.snapshot().workers} == {"layout_pending"}
            and len(supervisor.snapshot().workers) == 4,
            timeout=20.0,
            interval=0.005,
        )
        layout_seen_at = time.monotonic()
        # The helper is installing its layout and not reading its Pipe: no
        # identity may be reserved, so no lease can be dispatched to it.
        while time.monotonic() - layout_seen_at < 1.0:
            assert supervisor.reserve_capacity() is None
            snapshot = supervisor.snapshot()
            assert snapshot.idle_workers == 0 and snapshot.active_workers == 0
            assert snapshot.starting_workers == 4
            time.sleep(0.02)
        assert grants == []
        _wait_until(lambda: supervisor.snapshot().idle_workers == 4, timeout=10.0)
        assert [delta for _, delta in grants] == [1, 1, 1, 1]
        assert grants[0][0] - layout_seen_at >= 1.0
        lease = supervisor.reserve_capacity()
        assert lease is not None
        assert supervisor.submit(replace(
            _command(
                1, epoch="after-layout", worker_id=lease.worker_id,
                worker_generation=lease.worker_generation,
                node=_node(1, fixture_validation_outcome="pass"),
            ),
            capacity_reservation_id=lease.reservation_id,
        ))
        assert results.get(timeout=10.0).kind is EndpointAttemptKind.PASS
    finally:
        assert supervisor.stop(timeout_seconds=3.0) == "STOPPED"


def test_a_helper_that_never_reports_batch_ready_is_retired_by_the_startup_deadline(monkeypatch) -> None:
    monkeypatch.setenv("TIER3_BATCH_FAKE_SKIP_BATCH_READY", "1")
    results: queue.Queue[EndpointAttemptResult] = queue.Queue()
    capacities: list[int] = []
    supervisor = _supervisor(
        slots=4, workers=4, results=results, capacities=capacities, start_timeout_seconds=3.0,
    )
    supervisor.start()
    try:
        _wait_until(
            lambda: supervisor.snapshot().last_failure_reason == "helper_start_timeout:layout_pending",
            timeout=45.0,
        )
        assert "helper_start_timeout" in supervisor.batch_metrics()["group_retirement_reasons"]
        assert capacities == []
        assert results.empty()
    finally:
        supervisor.stop(timeout_seconds=3.0)


def test_the_startup_cohort_spawns_four_groups_at_once() -> None:
    results: queue.Queue[EndpointAttemptResult] = queue.Queue()
    grants: list[float] = []
    supervisor = Tier3BatchValidatorSupervisor(
        slots_per_process=4,
        binary_path="unused-by-fixture",
        probe_urls=["http://127.0.0.1/generate_204"],
        expected_statuses={204},
        probe_timeout_seconds=3.0,
        slow_threshold_ms=3000,
        attempt_sink=results.put_nowait,
        capacity_sink=lambda _delta: grants.append(time.monotonic()),
        # Six groups desired: four form the startup cohort.
        min_workers=24,
        max_workers=24,
        helper_target=batch_deterministic_helper_bootstrap,
    )
    scheduled: list[tuple[float, int]] = []
    original = supervisor._schedule_group_spawn

    def record(group_index: int) -> bool:
        accepted = original(group_index)
        if accepted:
            scheduled.append((time.monotonic(), int(group_index)))
        return accepted

    supervisor._schedule_group_spawn = record  # type: ignore[method-assign]
    started = time.monotonic()
    supervisor.start()
    try:
        _wait_until(lambda: supervisor.snapshot().idle_workers == 24, timeout=60.0)
        before_first_grant = sorted(index for at, index in scheduled if at < grants[0])
        assert before_first_grant == [1, 2, 3, 4], scheduled
        # All four were handed to the starter in the first reactor passes.
        assert max(at for at, index in scheduled if index <= 4) - started < 1.0
        assert sorted(index for _, index in scheduled) == list(range(1, 7))
        assert len({int(item["helper_pid"]) for item in supervisor.snapshot().workers}) == 6
    finally:
        assert supervisor.stop(timeout_seconds=6.0) == "STOPPED"
