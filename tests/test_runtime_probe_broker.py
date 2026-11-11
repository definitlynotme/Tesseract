from __future__ import annotations

import threading
import time

import pytest

from runtime_probe_broker import (
    MAX_ACTIVE_SOCKET_SLOTS,
    ProbeDisposition,
    ProbeIndeterminateReason,
    ProbePriority,
    ProbeRequestState,
    RuntimeProbeBroker,
)


def test_capacity_is_hard_capped_at_two_global_socket_slots() -> None:
    assert RuntimeProbeBroker().max_active_sockets == MAX_ACTIVE_SOCKET_SLOTS == 2
    assert RuntimeProbeBroker(max_active_sockets=1).max_active_sockets == 1
    with pytest.raises(ValueError):
        RuntimeProbeBroker(max_active_sockets=3)


def test_atomic_pair_never_partially_acquires_or_gets_bypassed() -> None:
    broker = RuntimeProbeBroker()
    first = broker.acquire(ProbePriority.BACKGROUND)
    assert first.ticket is not None

    high_pair = broker.request_pair(ProbePriority.RUNTIME_MANUAL)
    lower_single = broker.request(ProbePriority.BACKGROUND)

    snapshot = broker.snapshot()
    assert snapshot.active_socket_slots == 1
    assert snapshot.queued_runtime_manual == 1
    assert snapshot.queued_background == 1
    assert high_pair.state is ProbeRequestState.QUEUED
    assert lower_single.state is ProbeRequestState.QUEUED

    first.ticket.release()
    pair_admission = high_pair.wait(0.2)
    assert pair_admission.ticket is not None
    assert pair_admission.ticket.slots == 2
    assert broker.snapshot().active_socket_slots == 2
    assert lower_single.state is ProbeRequestState.QUEUED

    pair_admission.ticket.release()
    lower_admission = lower_single.wait(0.2)
    assert lower_admission.ticket is not None
    lower_admission.ticket.release()
    assert broker.wait_until_idle(0.2)


def test_strict_priority_prevents_background_work_starving_high_priority() -> None:
    broker = RuntimeProbeBroker()
    occupied = broker.acquire_pair(ProbePriority.BACKGROUND)
    assert occupied.ticket is not None

    background = broker.request(ProbePriority.BACKGROUND)
    hot = broker.request(ProbePriority.HOT_STANDBY)
    activation = broker.request(ProbePriority.ACTIVATION)
    runtime = broker.request(ProbePriority.RUNTIME_MANUAL)

    occupied.ticket.release()
    admissions = []
    for request in (runtime, activation, hot, background):
        admission = request.wait(0.2)
        assert admission.ticket is not None
        admissions.append(admission)
        admission.ticket.release()

    assert [item.priority for item in admissions] == [
        ProbePriority.RUNTIME_MANUAL,
        ProbePriority.ACTIVATION,
        ProbePriority.HOT_STANDBY,
        ProbePriority.BACKGROUND,
    ]


def test_no_available_slot_is_indeterminate_and_does_not_run_probe() -> None:
    broker = RuntimeProbeBroker()
    occupied = broker.acquire_pair(ProbePriority.BACKGROUND)
    assert occupied.ticket is not None
    called = False

    def probe(context):
        nonlocal called
        called = True
        context.mark_socket_started()
        return "unexpected"

    result = broker.run(
        ProbePriority.RUNTIME_MANUAL,
        probe,
        queue_timeout_seconds=0.0,
    )

    assert result.disposition is ProbeDisposition.INDETERMINATE
    assert result.indeterminate_reason is ProbeIndeterminateReason.QUEUE_TIMEOUT
    assert not result.socket_attempted
    assert result.error is None
    assert not called
    occupied.ticket.release()


@pytest.mark.parametrize("raise_before_return", [False, True])
def test_callback_without_real_socket_marker_is_indeterminate(
    raise_before_return: bool,
) -> None:
    broker = RuntimeProbeBroker()

    def probe(_context):
        if raise_before_return:
            raise TimeoutError("preflight did not reach a socket")
        return "preflight-only"

    result = broker.run(ProbePriority.RUNTIME_MANUAL, probe)

    assert result.disposition is ProbeDisposition.INDETERMINATE
    assert result.indeterminate_reason is ProbeIndeterminateReason.SOCKET_NOT_STARTED
    assert result.value is None
    assert result.error is None
    assert result.timing.socket_elapsed_seconds is None


def test_queue_and_socket_timings_are_separate() -> None:
    now = [10.0]
    broker = RuntimeProbeBroker(clock=lambda: now[0])

    def probe(context):
        now[0] = 12.0  # Adapter preparation: deliberately not socket time.
        assert context.mark_socket_started()
        now[0] = 15.5
        return "204"

    result = broker.run(ProbePriority.RUNTIME_MANUAL, probe)

    assert result.disposition is ProbeDisposition.EXECUTED
    assert result.value == "204"
    assert result.timing.queue_wait_seconds == 0.0
    assert result.timing.socket_started_at_monotonic == 12.0
    assert result.timing.socket_elapsed_seconds == 3.5
    assert result.timing.completed_at_monotonic == 15.5


def test_socket_start_deadline_is_distinct_from_queue_timeout() -> None:
    now = [20.0]
    broker = RuntimeProbeBroker(clock=lambda: now[0])
    dial_was_attempted = False

    def probe(context):
        nonlocal dial_was_attempted
        now[0] = 20.6
        if context.mark_socket_started():
            dial_was_attempted = True
        return "too-late"

    result = broker.run(
        ProbePriority.RUNTIME_MANUAL,
        probe,
        socket_start_timeout_seconds=0.5,
    )

    assert result.disposition is ProbeDisposition.INDETERMINATE
    assert result.indeterminate_reason is ProbeIndeterminateReason.STARTED_TIMEOUT
    assert result.timing.queue_wait_seconds == 0.0
    assert result.timing.socket_elapsed_seconds is None
    assert not result.socket_attempted
    assert not dial_was_attempted


def test_error_after_socket_start_is_executed_for_adapter_classification() -> None:
    broker = RuntimeProbeBroker()

    def probe(context):
        assert context.mark_socket_started()
        raise ConnectionResetError("real attempted socket reset")

    result = broker.run(ProbePriority.RUNTIME_MANUAL, probe)

    assert result.disposition is ProbeDisposition.EXECUTED
    assert isinstance(result.error, ConnectionResetError)
    assert result.indeterminate_reason is None
    assert result.socket_attempted


def test_run_pair_reserves_both_slots_and_executes_concurrently() -> None:
    broker = RuntimeProbeBroker()
    both_started = threading.Barrier(2, timeout=1.0)
    observed_active: list[int] = []
    observed_request_ids: list[int] = []
    lock = threading.Lock()

    def probe(context):
        assert context.mark_socket_started()
        with lock:
            observed_active.append(broker.snapshot().active_socket_slots)
            observed_request_ids.append(context.request_id)
        both_started.wait()
        return context.slot_index

    first, second = broker.run_pair(
        ProbePriority.RUNTIME_MANUAL,
        probe,
        probe,
    )

    assert first.disposition is ProbeDisposition.EXECUTED
    assert second.disposition is ProbeDisposition.EXECUTED
    assert {first.value, second.value} == {0, 1}
    assert observed_active == [2, 2]
    assert len(set(observed_request_ids)) == 1
    assert broker.snapshot().active_socket_slots == 0


def test_concurrent_callers_never_exceed_two_active_socket_slots() -> None:
    broker = RuntimeProbeBroker()
    release = threading.Event()
    lock = threading.Lock()
    observed_active: list[int] = []
    results = []

    def probe(context):
        assert context.mark_socket_started()
        with lock:
            observed_active.append(broker.snapshot().active_socket_slots)
        assert release.wait(1.0)
        return context.request_id

    def caller() -> None:
        result = broker.run(ProbePriority.BACKGROUND, probe)
        with lock:
            results.append(result)

    threads = [threading.Thread(target=caller, daemon=True) for _ in range(8)]
    for thread in threads:
        thread.start()

    deadline = time.monotonic() + 1.0
    while True:
        snapshot = broker.snapshot()
        with lock:
            observed_count = len(observed_active)
        if (
            snapshot.active_socket_slots == 2
            and snapshot.queued_background == 6
            and observed_count >= 2
        ):
            break
        assert time.monotonic() < deadline
        time.sleep(0.005)
    assert max(observed_active) == 2

    release.set()
    for thread in threads:
        thread.join(1.0)
        assert not thread.is_alive()

    assert len(results) == 8
    assert all(result.disposition is ProbeDisposition.EXECUTED for result in results)
    assert max(observed_active) == 2
    assert broker.snapshot().active_socket_slots == 0


def test_one_request_cannot_claim_the_same_ticket_twice() -> None:
    broker = RuntimeProbeBroker()
    request = broker.request(ProbePriority.RUNTIME_MANUAL)
    admission = request.wait(0.2)
    assert admission.ticket is not None

    with pytest.raises(RuntimeError, match="already claimed"):
        request.wait(0.2)

    admission.ticket.release()


def test_cancelling_queued_request_releases_it_as_indeterminate() -> None:
    broker = RuntimeProbeBroker()
    occupied = broker.acquire_pair(ProbePriority.BACKGROUND)
    assert occupied.ticket is not None
    request = broker.request(ProbePriority.RUNTIME_MANUAL)

    assert request.cancel()
    admission = request.wait(0.2)

    assert not admission.admitted
    assert admission.indeterminate_reason is ProbeIndeterminateReason.CANCELLED
    assert request.state is ProbeRequestState.INDETERMINATE
    assert broker.snapshot().queued_total == 0
    occupied.ticket.release()


def test_external_cancel_event_interrupts_wait_without_transport_failure() -> None:
    broker = RuntimeProbeBroker()
    occupied = broker.acquire_pair(ProbePriority.BACKGROUND)
    assert occupied.ticket is not None
    cancel_event = threading.Event()
    finished = threading.Event()
    result_holder = []

    def run_waiter() -> None:
        result_holder.append(
            broker.run(
                ProbePriority.RUNTIME_MANUAL,
                lambda context: context.mark_socket_started(),
                cancel_event=cancel_event,
            )
        )
        finished.set()

    thread = threading.Thread(target=run_waiter, daemon=True)
    thread.start()
    deadline = time.monotonic() + 1.0
    while broker.snapshot().queued_runtime_manual != 1:
        assert time.monotonic() < deadline
        time.sleep(0.005)
    cancel_event.set()

    assert finished.wait(0.5)
    result = result_holder[0]
    assert result.disposition is ProbeDisposition.INDETERMINATE
    assert result.indeterminate_reason is ProbeIndeterminateReason.CANCELLED
    assert not result.socket_attempted
    occupied.ticket.release()
    thread.join(0.2)


def test_stop_rejects_queued_work_and_cooperatively_cancels_active_probe() -> None:
    broker = RuntimeProbeBroker()
    socket_started = threading.Event()
    finished = threading.Event()
    result_holder = []

    def probe(context):
        assert context.mark_socket_started()
        socket_started.set()
        deadline = time.monotonic() + 1.0
        while not context.cancelled:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        return "abandoned"

    def run_probe() -> None:
        result_holder.append(broker.run(ProbePriority.BACKGROUND, probe))
        finished.set()

    thread = threading.Thread(target=run_probe, daemon=True)
    thread.start()
    assert socket_started.wait(0.5)
    queued = broker.request(ProbePriority.ACTIVATION)

    broker.stop()

    queued_admission = queued.wait(0.2)
    assert not queued_admission.admitted
    assert (
        queued_admission.indeterminate_reason
        is ProbeIndeterminateReason.BROKER_STOPPED
    )
    assert finished.wait(0.5)
    active_result = result_holder[0]
    assert active_result.disposition is ProbeDisposition.INDETERMINATE
    assert active_result.indeterminate_reason is ProbeIndeterminateReason.BROKER_STOPPED
    assert broker.wait_until_idle(0.2)
    assert broker.snapshot().stopped

    rejected = broker.run(
        ProbePriority.RUNTIME_MANUAL,
        lambda context: context.mark_socket_started(),
    )
    assert rejected.indeterminate_reason is ProbeIndeterminateReason.BROKER_STOPPED
    thread.join(0.2)


def test_execution_deadline_aborts_closable_worker_and_releases_slot() -> None:
    broker = RuntimeProbeBroker()
    socket_started = threading.Event()
    connection_closed = threading.Event()

    def blocking_probe(context):
        assert context.register_abort_callback(connection_closed.set)
        assert context.mark_socket_started()
        socket_started.set()
        assert connection_closed.wait(1.0)
        return "closed"

    started_at = time.monotonic()
    result = broker.run(
        ProbePriority.RUNTIME_MANUAL,
        blocking_probe,
        execution_deadline_monotonic=started_at + 0.05,
    )
    elapsed = time.monotonic() - started_at

    assert socket_started.is_set()
    assert connection_closed.is_set()
    assert elapsed < 0.3
    assert result.disposition is ProbeDisposition.INDETERMINATE
    assert result.indeterminate_reason is ProbeIndeterminateReason.EXECUTION_TIMEOUT
    assert result.socket_attempted
    assert broker.wait_until_idle(0.2)
    assert broker.snapshot().active_socket_slots == 0
    assert not broker.poisoned

    next_result = broker.run(
        ProbePriority.RUNTIME_MANUAL,
        lambda context: context.mark_socket_started(),
    )
    assert next_result.disposition is ProbeDisposition.EXECUTED


def test_pair_deadline_aborts_both_workers_and_releases_both_slots() -> None:
    broker = RuntimeProbeBroker()
    closed = (threading.Event(), threading.Event())
    both_started = threading.Barrier(2, timeout=1.0)

    def blocking_probe(context):
        close_event = closed[context.slot_index]
        assert context.register_abort(close_event.set)
        assert context.mark_socket_started()
        both_started.wait()
        assert close_event.wait(1.0)
        return context.slot_index

    deadline = time.monotonic() + 0.06
    first, second = broker.run_pair(
        ProbePriority.RUNTIME_MANUAL,
        blocking_probe,
        blocking_probe,
        execution_deadline_monotonic=deadline,
    )

    assert closed[0].is_set() and closed[1].is_set()
    assert {
        first.indeterminate_reason,
        second.indeterminate_reason,
    } == {ProbeIndeterminateReason.EXECUTION_TIMEOUT}
    assert first.socket_attempted and second.socket_attempted
    assert broker.wait_until_idle(0.2)
    assert broker.snapshot().active_socket_slots == 0
    assert not broker.poisoned


def test_deadline_expiring_while_queued_is_execution_timeout() -> None:
    broker = RuntimeProbeBroker()
    occupied = broker.acquire_pair(ProbePriority.BACKGROUND)
    assert occupied.ticket is not None
    called = False

    def probe(context):
        nonlocal called
        called = True
        return context.mark_socket_started()

    result = broker.run(
        ProbePriority.RUNTIME_MANUAL,
        probe,
        execution_deadline_monotonic=time.monotonic() + 0.04,
    )

    assert result.indeterminate_reason is ProbeIndeterminateReason.EXECUTION_TIMEOUT
    assert not called
    assert not broker.poisoned
    occupied.ticket.release()


def test_missing_abort_registration_poisons_deadline_controlled_broker() -> None:
    broker = RuntimeProbeBroker()

    result = broker.run(
        ProbePriority.RUNTIME_MANUAL,
        lambda context: context.mark_socket_started(),
        execution_deadline_monotonic=time.monotonic() + 0.5,
    )

    assert result.disposition is ProbeDisposition.INDETERMINATE
    assert result.indeterminate_reason is ProbeIndeterminateReason.BROKER_POISONED
    snapshot = broker.snapshot()
    assert snapshot.poisoned
    assert snapshot.poison_reason
    assert snapshot.active_socket_slots == 0

    rejected = broker.run(
        ProbePriority.RUNTIME_MANUAL,
        lambda context: context.mark_socket_started(),
    )
    assert rejected.indeterminate_reason is ProbeIndeterminateReason.BROKER_POISONED


def test_noncooperative_worker_poisons_without_faking_free_capacity() -> None:
    broker = RuntimeProbeBroker()
    abort_called = threading.Event()
    release_worker = threading.Event()

    def noncooperative_probe(context):
        assert context.register_abort_callback(abort_called.set)
        assert context.mark_socket_started()
        assert release_worker.wait(2.0)
        return "late"

    started_at = time.monotonic()
    result = broker.run(
        ProbePriority.RUNTIME_MANUAL,
        noncooperative_probe,
        execution_deadline_monotonic=started_at + 0.04,
    )
    elapsed = time.monotonic() - started_at

    assert result.indeterminate_reason is ProbeIndeterminateReason.EXECUTION_TIMEOUT
    assert abort_called.is_set()
    assert elapsed < 0.3
    assert broker.poisoned
    assert broker.snapshot().active_socket_slots == 1

    rejected = broker.run(
        ProbePriority.RUNTIME_MANUAL,
        lambda context: context.mark_socket_started(),
    )
    assert rejected.indeterminate_reason is ProbeIndeterminateReason.BROKER_POISONED
    assert broker.snapshot().active_socket_slots == 1

    release_worker.set()
    assert broker.wait_until_idle(0.5)
    assert broker.snapshot().active_socket_slots == 0
    assert broker.poisoned


def test_pair_releases_only_exited_worker_slot_when_peer_is_noncooperative() -> None:
    broker = RuntimeProbeBroker()
    cooperative_closed = threading.Event()
    stubborn_abort_called = threading.Event()
    release_stubborn = threading.Event()
    both_started = threading.Barrier(2, timeout=1.0)

    def cooperative_probe(context):
        assert context.register_abort(cooperative_closed.set)
        assert context.mark_socket_started()
        both_started.wait()
        assert cooperative_closed.wait(1.0)
        return "closed"

    def stubborn_probe(context):
        assert context.register_abort(stubborn_abort_called.set)
        assert context.mark_socket_started()
        both_started.wait()
        assert release_stubborn.wait(2.0)
        return "late"

    first, second = broker.run_pair(
        ProbePriority.RUNTIME_MANUAL,
        cooperative_probe,
        stubborn_probe,
        execution_deadline_monotonic=time.monotonic() + 0.05,
    )

    assert first.indeterminate_reason is ProbeIndeterminateReason.EXECUTION_TIMEOUT
    assert second.indeterminate_reason is ProbeIndeterminateReason.EXECUTION_TIMEOUT
    assert cooperative_closed.is_set()
    assert stubborn_abort_called.is_set()
    assert broker.poisoned
    assert broker.snapshot().active_socket_slots == 1

    release_stubborn.set()
    assert broker.wait_until_idle(0.5)
    assert broker.snapshot().active_socket_slots == 0


def test_stop_is_bounded_and_aborts_then_poisons_noncooperative_worker() -> None:
    broker = RuntimeProbeBroker()
    socket_started = threading.Event()
    abort_called = threading.Event()
    release_worker = threading.Event()
    finished = threading.Event()

    def noncooperative_probe(context):
        assert context.register_abort_callback(abort_called.set)
        assert context.mark_socket_started()
        socket_started.set()
        assert release_worker.wait(2.0)
        return "late"

    def run_probe() -> None:
        broker.run(ProbePriority.BACKGROUND, noncooperative_probe)
        finished.set()

    thread = threading.Thread(target=run_probe, daemon=True)
    thread.start()
    assert socket_started.wait(0.5)

    started_at = time.monotonic()
    broker.stop()
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.5
    assert abort_called.is_set()
    assert broker.stopped
    assert broker.poisoned
    assert broker.snapshot().active_socket_slots == 1

    release_worker.set()
    assert finished.wait(0.5)
    assert broker.wait_until_idle(0.5)
    thread.join(0.2)
