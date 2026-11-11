from __future__ import annotations

import multiprocessing
import multiprocessing.connection
import os
from types import SimpleNamespace

import pytest

from tier3_validation_protocol import Tier3WorkerPhase
from tier3_validator_supervisor import Tier3ValidatorSupervisor
import tier3_validator_supervisor as supervisor_module


def make_supervisor():
    return Tier3ValidatorSupervisor(
        binary_path="fixture", probe_urls=["http://fixture/probe"],
        expected_statuses={204}, probe_timeout_seconds=5.0,
        slow_threshold_ms=3000, attempt_sink=lambda _: None,
        capacity_sink=lambda _: None, min_workers=1, max_workers=16,
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows overlapped Pipe contract")
@pytest.mark.parametrize("count", [1, 16])
def test_empty_pipe_sweep_does_not_create_and_cancel_overlapped_reads(monkeypatch, count):
    supervisor = make_supervisor()
    pairs = [multiprocessing.Pipe() for _ in range(count)]

    def reject_empty_wait(*args, **kwargs):
        raise AssertionError("empty sweep must not create a second overlapped wait")

    monkeypatch.setattr(multiprocessing.connection, "wait", reject_empty_wait)
    try:
        for worker_id, (parent, _) in enumerate(pairs, 1):
            worker = SimpleNamespace(worker_id=worker_id, connection=parent,
                                     phase=Tier3WorkerPhase.IDLE)
            supervisor._workers[worker_id] = worker
            assert supervisor._drain_worker_pipe(worker) == 0
    finally:
        supervisor._workers.clear()
        for parent, child in pairs:
            parent.close()
            child.close()


def test_pipe_sweep_preserves_message_order_and_leaves_empty_pipe(monkeypatch):
    supervisor = make_supervisor()
    parent, child = multiprocessing.Pipe()
    worker = SimpleNamespace(worker_id=1, connection=parent, phase=Tier3WorkerPhase.IDLE)
    supervisor._workers[1] = worker
    received = []
    monkeypatch.setattr(supervisor, "_handle_pipe", lambda w: received.append(w.connection.recv_bytes()))
    try:
        for message in (b"heartbeat", b"facility_started", b"transport_started", b"result"):
            child.send_bytes(message)
        assert supervisor._drain_worker_pipe(worker, max_messages=2) == 2
        assert supervisor._drain_worker_pipe(worker) == 2
        assert supervisor._drain_worker_pipe(worker) == 0
        assert received == [b"heartbeat", b"facility_started", b"transport_started", b"result"]
    finally:
        supervisor._workers.clear()
        parent.close()
        child.close()


def test_closed_peer_still_flows_through_single_pipe_failure(monkeypatch):
    supervisor = make_supervisor()
    parent, child = multiprocessing.Pipe()
    worker = SimpleNamespace(worker_id=1, connection=parent, phase=Tier3WorkerPhase.IDLE,
                             stop_requested=False)
    supervisor._workers[1] = worker
    failures = []

    def retire(w, reason, **kwargs):
        failures.append(reason)
        w.phase = Tier3WorkerPhase.STOPPED

    monkeypatch.setattr(supervisor, "_retire_worker", retire)
    try:
        child.close()
        assert supervisor._drain_worker_pipe(worker) == 1
        assert supervisor._drain_worker_pipe(worker) == 0
        assert len(failures) == 1
        assert failures[0].startswith("helper_pipe_lost:")
    finally:
        supervisor._workers.clear()
        parent.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows consumed empty message contract")
def test_wait_consumed_empty_message_is_not_hidden(monkeypatch):
    supervisor = make_supervisor()
    parent, child = multiprocessing.Pipe()
    worker = SimpleNamespace(worker_id=1, connection=parent, phase=Tier3WorkerPhase.IDLE)
    supervisor._workers[1] = worker
    received = []
    monkeypatch.setattr(supervisor, "_handle_pipe", lambda w: received.append(w.connection.recv_bytes()))
    try:
        child.send_bytes(b"")
        assert multiprocessing.connection.wait([parent], timeout=1.0) == [parent]
        assert supervisor._drain_worker_pipe(worker) == 1
        assert received == [b""]
    finally:
        supervisor._workers.clear()
        parent.close()
        child.close()


def test_owner_does_not_resweep_idle_pipes_after_batch_wait(monkeypatch):
    supervisor = make_supervisor()
    pairs = [multiprocessing.Pipe() for _ in range(16)]
    try:
        for worker_id, (parent, _) in enumerate(pairs, 1):
            supervisor._workers[worker_id] = SimpleNamespace(
                worker_id=worker_id, connection=parent, process=None,
                phase=Tier3WorkerPhase.IDLE,
            )
        for name in ("_process_controls", "_reap_retiring_workers", "_assign_pending",
                     "_start_needed_workers", "_stop_all"):
            monkeypatch.setattr(supervisor, name, lambda: None)
        monkeypatch.setattr(supervisor_module, "_process_sentinel", lambda _: None)
        waits = []

        def wait(items, timeout):
            waits.append((len(items), timeout))
            return []

        def reject_resweep(*args, **kwargs):
            raise AssertionError("batch wait already found every idle Pipe empty")

        monkeypatch.setattr(supervisor._pipe_wait, "wait", wait)
        monkeypatch.setattr(supervisor_module, "_pipe_has_queued_message", reject_resweep)
        monkeypatch.setattr(supervisor, "_check_deadlines", supervisor._stopping.set)
        supervisor._run()
        assert waits == [(16, 0.05)]
    finally:
        supervisor._workers.clear()
        for parent, child in pairs:
            parent.close()
            child.close()


def test_batch_ready_message_is_consumed_without_second_readiness_probe(monkeypatch):
    supervisor = make_supervisor()
    parent, child = multiprocessing.Pipe()
    worker = SimpleNamespace(worker_id=1, connection=parent, phase=Tier3WorkerPhase.IDLE)
    supervisor._workers[1] = worker
    received = []
    monkeypatch.setattr(supervisor, "_handle_pipe", lambda w: received.append(w.connection.recv_bytes()))

    def reject_second_probe(*args, **kwargs):
        raise AssertionError("the sole reader already owns this ready message")

    try:
        child.send_bytes(b"result")
        assert multiprocessing.connection.wait([parent], timeout=1.0) == [parent]
        monkeypatch.setattr(supervisor_module, "_pipe_has_queued_message", reject_second_probe)
        assert supervisor._drain_worker_pipe(worker, max_messages=1, first_message_ready=True) == 1
        assert received == [b"result"]
    finally:
        supervisor._workers.clear()
        parent.close()
        child.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows pending readiness ownership")
def test_deadline_drain_releases_pending_wait_before_receiving_late_phase(monkeypatch):
    supervisor = make_supervisor()
    parent, child = multiprocessing.Pipe()
    worker = SimpleNamespace(worker_id=1, connection=parent, phase=Tier3WorkerPhase.IDLE)
    supervisor._workers[1] = worker
    received = []

    def receive(w):
        assert w.connection not in supervisor._pipe_wait._pending
        received.append(w.connection.recv_bytes())

    monkeypatch.setattr(supervisor, "_handle_pipe", receive)
    try:
        assert supervisor._pipe_wait.wait([parent], 0) == []
        child.send_bytes(b"transport_started")
        child.send_bytes(b"result")
        assert supervisor._drain_worker_pipe(worker) == 2
        assert received == [b"transport_started", b"result"]
    finally:
        supervisor._pipe_wait.close()
        supervisor._workers.clear()
        parent.close()
        child.close()


def test_owner_exit_releases_pending_native_operations_even_when_stop_is_replaced(monkeypatch):
    supervisor = make_supervisor()
    parent, child = multiprocessing.Pipe()
    supervisor._workers[1] = SimpleNamespace(
        worker_id=1, connection=parent, process=None, phase=Tier3WorkerPhase.IDLE,
    )
    for name in ("_process_controls", "_reap_retiring_workers", "_assign_pending",
                 "_start_needed_workers", "_stop_all"):
        monkeypatch.setattr(supervisor, name, lambda: None)
    monkeypatch.setattr(supervisor_module, "_process_sentinel", lambda _: None)
    monkeypatch.setattr(supervisor, "_check_deadlines", supervisor._stopping.set)
    try:
        supervisor._run()
        assert not supervisor._pipe_wait._pending
        assert not supervisor._running.is_set()
        child.send_bytes(b"no-overlapping-reader")
        assert parent.recv_bytes() == b"no-overlapping-reader"
    finally:
        supervisor._pipe_wait.close()
        supervisor._workers.clear()
        parent.close()
        child.close()


@pytest.mark.parametrize("spawn_completed", [False, True])
def test_owner_waits_for_initial_spawn_without_losing_its_wakeup(monkeypatch, spawn_completed):
    supervisor = make_supervisor()
    for name in ("_process_controls", "_reap_retiring_workers", "_assign_pending", "_stop_all"):
        monkeypatch.setattr(supervisor, name, lambda: None)
    supervisor._wake.set()

    def start_workers():
        if spawn_completed:
            supervisor._wake.set()

    waits = []

    def wait(timeout):
        waits.append((timeout, supervisor._wake.is_set()))
        return supervisor._wake.is_set()

    monkeypatch.setattr(supervisor, "_start_needed_workers", start_workers)
    monkeypatch.setattr(supervisor._wake, "wait", wait)
    monkeypatch.setattr(supervisor, "_check_deadlines", supervisor._stopping.set)
    supervisor._run()
    assert waits == [(0.05, spawn_completed)]


@pytest.mark.parametrize("phase", [
    Tier3WorkerPhase.WAITING_START, Tier3WorkerPhase.STARTING,
    Tier3WorkerPhase.VALIDATING, Tier3WorkerPhase.CANCELLING,
])
def test_due_deadline_checks_late_pipe_message_before_retirement(monkeypatch, phase):
    supervisor = make_supervisor()
    worker = SimpleNamespace(
        worker_id=1, generation=1, phase=phase, bootstrap_ready=False, started_at=0.01,
        startup_phase="", startup_deadline_at=99.0,
        command=SimpleNamespace(refresh_epoch="fixture", lease_id="lease", sequence=1),
        deadline_at=99.0, deadline_phase="transport", transport_started_at=98.0,
        cancel_deadline_at=99.0,
    )
    supervisor._workers[1] = worker
    monkeypatch.setattr(supervisor_module.time, "monotonic", lambda: 100.0)
    drained = []

    def drain(w, **kwargs):
        drained.append(w.worker_id)
        # A result arriving just after batch wait releases the lease.
        w.phase = Tier3WorkerPhase.IDLE
        w.command = None
        return 1

    def reject_retire(*args, **kwargs):
        raise AssertionError("late queued result must precede deadline enforcement")

    monkeypatch.setattr(supervisor, "_drain_worker_pipe", drain)
    monkeypatch.setattr(supervisor, "_retire_worker", reject_retire)
    monkeypatch.setattr(supervisor, "_send", reject_retire)
    supervisor._check_deadlines()
    assert drained == [1]
