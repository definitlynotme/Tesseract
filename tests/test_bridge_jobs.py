"""Isolated, event-driven tests for the P1 job executor."""

from __future__ import annotations

import threading
import time

import pytest

import bridge_protocol
from bridge_jobs import JobEventRecorder, JobExecutor, JobFailure, StopIntent


def _executor(*, event_capacity=512, terminal_capacity=1024, monotonic=time.monotonic):
    recorder = JobEventRecorder(event_capacity)
    executor = JobExecutor(
        recorder, epoch="test-epoch", terminal_capacity=terminal_capacity,
        normal_workers=1, normal_queue=1, monotonic=monotonic,
    )
    return executor, recorder


def _register_stop(executor, *, intent=None, execute=None, cancel=None):
    executor.register(
        "service.stop",
        execute or (lambda args, ctx: {
            "startup_phase": "stopped", "shutdown_exit_code": 0,
            "cleanup_pending": False,
        }),
        stop_intent=intent or (lambda args, ctx: StopIntent(True, token="stop-token")),
        cancel=cancel,
    )


def _events(recorder):
    events = recorder.prepare_for_send(123456)
    assert [item["seq"] for item in events] == list(range(1, len(events) + 1))
    assert [item["rev"] for item in events] == list(range(1, len(events) + 1))
    assert all(bridge_protocol.validate_evt(item) == [] for item in events)
    assert all(bridge_protocol.validate_frame(item) == [] for item in events)
    recorder.ack_sent(events)
    assert recorder.pending == 0
    return events


def test_stop_starts_with_change_and_normal_domains_occupied():
    change_entered = threading.Event()
    normal_entered = threading.Event()
    stop_entered = threading.Event()
    release = threading.Event()
    executor, recorder = _executor()
    executor.register("service.start", lambda args, ctx: (
        change_entered.set(), release.wait(),
        {"startup_phase": "running", "failure_code": None},
    )[-1])
    _register_stop(executor, intent=lambda args, ctx: (
        stop_entered.set(), StopIntent(True)
    )[-1])
    try:
        start = executor.submit("service.start", {})
        assert start.accepted and change_entered.wait(1)
        assert executor.submit_normal(lambda: (normal_entered.set(), release.wait()))
        assert normal_entered.wait(1)
        assert executor.submit_normal(lambda: release.wait())
        began = time.perf_counter()
        stop = executor.submit("service.stop", {})
        assert stop.accepted
        assert stop_entered.wait(0.1)
        assert time.perf_counter() - began < 0.1
        assert stop.wait_stop_intent(1).accepted
    finally:
        assert executor.wait(stop.op, 1)["state"] == "done"
        release.set()
        assert executor.wait(start.op, 1)["state"] == "superseded"
        executor.shutdown(time.monotonic() + 1)
    _events(recorder)


def test_change_transactions_are_serial_from_first_to_last_step():
    first_entered = threading.Event()
    second_entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    peak = 0
    executor, recorder = _executor()

    def change(args, ctx):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        if not first_entered.is_set():
            first_entered.set()
            assert release.wait(1)
        else:
            second_entered.set()
        with lock:
            active -= 1
        return {"startup_phase": "running", "failure_code": None}

    executor.register("service.start", change)
    try:
        first = executor.submit("service.start", {})
        assert first_entered.wait(1)
        second = executor.submit("service.start", {})
        assert second.accepted and not second_entered.is_set()
        release.set()
        assert executor.wait(first.op, 1)["state"] == "done"
        assert executor.wait(second.op, 1)["state"] == "done"
        assert second_entered.is_set()
        assert peak == 1
    finally:
        release.set()
        executor.shutdown(time.monotonic() + 1)
    _events(recorder)


def test_active_control_reservation_and_event_capacity_backpressure():
    started = threading.Event()
    release = threading.Event()
    intent_entered = threading.Event()
    release_intent = threading.Event()
    executor, recorder = _executor(event_capacity=512)
    executor.register("service.start", lambda args, ctx: (
        started.set(), release.wait(),
        {"startup_phase": "running", "failure_code": None},
    )[-1])
    _register_stop(executor, intent=lambda args, ctx: (
        intent_entered.set(), release_intent.wait(), StopIntent(True)
    )[-1])
    executor.register("app.quit", lambda args, ctx: {
        "startup_phase": "stopped", "shutdown_exit_code": None,
        "cleanup_pending": False,
    }, stop_intent=lambda args, ctx: StopIntent(True))
    try:
        changes = [executor.submit("service.start", {}) for _ in range(62)]
        assert all(item.accepted for item in changes) and started.wait(1)
        assert executor.submit("service.start", {}).skipped_reason == "job_capacity"
        stop = executor.submit("service.stop", {})
        assert stop.accepted and intent_entered.wait(1)
        quit_job = executor.submit("app.quit", {})
        assert quit_job.accepted
        assert executor._active == 64
        assert executor.submit("service.start", {}).skipped_reason == "not_allowed"
    finally:
        release_intent.set()
        assert executor.wait(stop.op, 1)["state"] == "done"
        assert executor.wait(quit_job.op, 1)["state"] == "done"
        release.set()
        for change in changes:
            assert executor.wait(change.op, 1)["state"] in {"cancelled", "superseded"}
        executor.shutdown(time.monotonic() + 1)
    _events(recorder)

    entered = threading.Event()
    release = threading.Event()
    small, small_recorder = _executor(event_capacity=9)
    small.register("service.start", lambda args, ctx: (
        entered.set(), release.wait(),
        {"startup_phase": "running", "failure_code": None},
    )[-1])
    _register_stop(small)
    small.register("app.quit", lambda args, ctx: {
        "startup_phase": "stopped", "shutdown_exit_code": None,
        "cleanup_pending": False,
    }, stop_intent=lambda args, ctx: StopIntent(True))
    try:
        one = small.submit("service.start", {})
        assert entered.wait(1)
        assert small.submit("service.start", {}).skipped_reason == "job_capacity"
        stop = small.submit("service.stop", {})
        assert stop.accepted and small.wait(stop.op, 1)["state"] == "done"
        quit_job = small.submit("app.quit", {})
        assert quit_job.accepted and small.wait(quit_job.op, 1)["state"] == "done"
    finally:
        release.set()
        assert small.wait(one.op, 1)["state"] == "superseded"
        small.shutdown(time.monotonic() + 1)
    _events(small_recorder)


def test_terminal_retention_holds_ten_minutes_or_most_recent_256():
    now = [0.0]
    executor, recorder = _executor(
        terminal_capacity=259, monotonic=lambda: now[0],
    )
    executor.register("service.start", lambda args, ctx: {
        "startup_phase": "collecting", "failure_code": None,
    })
    try:
        accepted = []
        for _ in range(257):
            ack = executor.submit("service.start", {})
            assert ack.accepted
            accepted.append(ack.op)
            assert executor.wait(ack.op, 1)["state"] == "done"
            outgoing = recorder.prepare_for_send(10)
            assert all(bridge_protocol.validate_evt(item) == [] for item in outgoing)
            recorder.ack_sent(outgoing)
        assert executor.submit("service.start", {}).skipped_reason == "job_capacity"
        assert executor.get_job(accepted[0]) is not None
        now[0] = 601.0
        new = executor.submit("service.start", {})
        assert new.accepted
        assert executor.wait(new.op, 1)["state"] == "done"
        assert executor.get_job(accepted[0]) is None
        assert executor.get_job(accepted[1]) is None
        assert executor.get_job(accepted[2]) is not None
    finally:
        executor.shutdown(time.monotonic() + 1)

    with pytest.raises(ValueError):
        JobExecutor(JobEventRecorder(), terminal_capacity=258)


def test_terminal_reserve_still_admits_stop_and_quit():
    executor, recorder = _executor(terminal_capacity=259)
    executor.register("service.start", lambda args, ctx: {
        "startup_phase": "running", "failure_code": None,
    })
    _register_stop(executor)
    executor.register("app.quit", lambda args, ctx: {
        "startup_phase": "stopped", "shutdown_exit_code": None,
        "cleanup_pending": False,
    }, stop_intent=lambda args, ctx: StopIntent(True))
    try:
        for _ in range(257):
            ack = executor.submit("service.start", {})
            assert ack.accepted and executor.wait(ack.op, 1)["state"] == "done"
            outgoing = recorder.prepare_for_send(10)
            recorder.ack_sent(outgoing)
        assert executor.submit("service.start", {}).skipped_reason == "job_capacity"
        stop = executor.submit("service.stop", {})
        assert stop.accepted and executor.wait(stop.op, 1)["state"] == "done"
        quit_job = executor.submit("app.quit", {})
        assert quit_job.accepted and executor.wait(quit_job.op, 1)["state"] == "done"
        assert all(bridge_protocol.validate_evt(event) == []
                   for event in recorder.prepare_for_send(11))
        recorder.abort_send()
    finally:
        executor.shutdown(time.monotonic() + 1)


def test_queued_cancel_running_no_callback_and_stop_supersession():
    entered = threading.Event()
    release = threading.Event()
    executor, recorder = _executor()
    executor.register("service.start", lambda args, ctx: (
        entered.set(), release.wait(),
        {"startup_phase": "running", "failure_code": None},
    )[-1])
    _register_stop(executor)
    try:
        first = executor.submit("service.start", {}, req_id="request-1", owner="page-1")
        assert entered.wait(1)
        queued = executor.submit("service.start", {})
        assert executor.cancel(queued.op).accepted
        assert executor.wait(queued.op, 1)["state"] == "cancelled"
        assert executor.cancel(first.op).skipped_reason == "not_cancellable"
        stop = executor.submit("service.stop", {})
        assert stop.wait_stop_intent(1).accepted
        release.set()
        assert executor.wait(first.op, 1)["state"] == "superseded"
        assert executor.wait(stop.op, 1)["state"] == "done"
    finally:
        release.set()
        executor.shutdown(time.monotonic() + 1)
    events = _events(recorder)
    by_op = {}
    for event in events:
        by_op.setdefault(event["data"]["op"], []).append(event["data"]["state"])
    assert by_op[queued.op] == ["queued", "cancelled"]
    assert by_op[first.op] == ["queued", "running", "superseded"]
    assert by_op[stop.op] == ["queued", "running", "done"]
    assert events[0]["data"]["req_id"] == "request-1"
    assert events[0]["data"]["owner"] == "page-1"


@pytest.mark.parametrize("name", ["service.stop", "app.quit"])
def test_cancel_after_control_intent_begins_cannot_drop_cleanup(name):
    intent_entered = threading.Event()
    release_intent = threading.Event()
    cleanup_called = threading.Event()
    executor, recorder = _executor()

    def intent(args, ctx):
        intent_entered.set()
        assert release_intent.wait(1)
        return StopIntent(True, token="real-intent")

    executor.register(name, lambda args, ctx: (
        cleanup_called.set(),
        {"startup_phase": "stopped", "shutdown_exit_code": 0,
         "cleanup_pending": False},
    )[-1], stop_intent=intent)
    try:
        ack = executor.submit(name, {})
        assert intent_entered.wait(1)
        assert executor.cancel(ack.op).skipped_reason == "not_cancellable"
        release_intent.set()
        assert ack.wait_stop_intent(1).token == "real-intent"
        assert executor.wait(ack.op, 1)["state"] == "done"
        assert cleanup_called.is_set()
    finally:
        release_intent.set()
        executor.shutdown(time.monotonic() + 1)
    assert [event["data"]["state"] for event in _events(recorder)] == [
        "queued", "running", "done",
    ]


@pytest.mark.parametrize("name", ["service.stop", "app.quit"])
def test_shutdown_keeps_control_cleanup_even_with_cancel_callback(name):
    entered = threading.Event()
    release = threading.Event()
    cancel_called = threading.Event()
    executor, recorder = _executor()
    executor.register(name, lambda args, ctx: (
        entered.set(), release.wait(),
        {"startup_phase": "stopped", "shutdown_exit_code": 0,
         "cleanup_pending": False},
    )[-1], stop_intent=lambda args, ctx: StopIntent(True),
        cancel=lambda args, ctx: (cancel_called.set(), True)[-1])
    try:
        ack = executor.submit(name, {})
        assert entered.wait(1)
        assert not executor.shutdown(time.monotonic() + 0.02)
        assert not cancel_called.is_set()
        release.set()
        assert executor.wait(ack.op, 1)["state"] == "done"
        assert not cancel_called.is_set()
    finally:
        release.set()
    _events(recorder)


@pytest.mark.parametrize("identifier", ["request:1", "页面:结果", "r" * 256])
def test_protocol_identifiers_remain_compatible(identifier):
    executor, recorder = _executor()
    executor.register("service.start", lambda args, ctx: {
        "startup_phase": "running", "failure_code": None,
    })
    try:
        ack = executor.submit("service.start", {}, req_id=identifier, owner=identifier)
        assert ack.accepted
        assert executor.wait(ack.op, 1)["state"] == "done"
    finally:
        executor.shutdown(time.monotonic() + 1)
    events = _events(recorder)
    assert all(event["data"]["req_id"] == identifier and
               event["data"]["owner"] == identifier for event in events)


@pytest.mark.parametrize("identifier", [
    "", "https://example.test/x", "api_key:abc", '{"nested":{"token":"x"}}',
    r"to\u006ben:abc",
])
def test_protocol_identifiers_reject_empty_or_sensitive_values(identifier):
    executor, recorder = _executor()
    executor.register("service.start", lambda args, ctx: {
        "startup_phase": "running", "failure_code": None,
    })
    try:
        assert executor.submit("service.start", {}, req_id=identifier).skipped_reason == "not_allowed"
    finally:
        executor.shutdown(time.monotonic() + 1)
    assert recorder.pending == 0


def test_epoch_uses_protocol_string_rule_too():
    epoch = "运行代:1" + "e" * 256
    recorder = JobEventRecorder()
    executor = JobExecutor(recorder, epoch=epoch)
    executor.register("service.start", lambda args, ctx: {
        "startup_phase": "running", "failure_code": None,
    })
    try:
        ack = executor.submit("service.start", {})
        assert executor.wait(ack.op, 1)["state"] == "done"
    finally:
        executor.shutdown(time.monotonic() + 1)
    assert all(event["epoch"] == epoch for event in _events(recorder))
    with pytest.raises(ValueError):
        JobExecutor(JobEventRecorder(), epoch="")


@pytest.mark.parametrize("name,positive,negative,expected_error", [
    ("service.start", {"startup_phase": "core_standby", "failure_code": None},
     {"startup_phase": "stopping", "failure_code": None}, "conflict"),
    ("service.stop", {"startup_phase": "stopped", "shutdown_exit_code": 0,
                      "cleanup_pending": False},
     {"startup_phase": "stopping", "shutdown_exit_code": 0,
      "cleanup_pending": True}, "timeout"),
    ("service.restart", {"applied": True, "core_running": True,
                         "core_cleanup_pending": False, "core_generation": 6},
     {"applied": False, "core_running": True,
      "core_cleanup_pending": False, "core_generation": 5}, "conflict"),
    ("app.quit", {"startup_phase": "stopped", "shutdown_exit_code": None,
                  "cleanup_pending": False},
     {"startup_phase": "stopped", "shutdown_exit_code": 3,
      "cleanup_pending": False}, "io"),
])
def test_every_p1_result_adapter_positive_and_negative(name, positive, negative, expected_error):
    for raw, expected_state in ((positive, "done"), (negative, "failed")):
        executor, recorder = _executor()
        kwargs = {}
        if name == "service.restart":
            kwargs["before"] = lambda args, ctx: 5
        if name in {"service.stop", "app.quit"}:
            kwargs["stop_intent"] = lambda args, ctx: StopIntent(True)
        executor.register(name, lambda args, ctx: raw, **kwargs)
        try:
            ack = executor.submit(name, {})
            assert ack.accepted
            job = executor.wait(ack.op, 1)
            assert job["state"] == expected_state
            assert job["result"] == raw
            if expected_state == "failed":
                assert job["error"]["code"] == expected_error
        finally:
            executor.shutdown(time.monotonic() + 1)
        _events(recorder)


@pytest.mark.parametrize("name,raw,expected_error", [
    ("service.stop", {"startup_phase": "stopping", "shutdown_exit_code": 0,
                      "cleanup_pending": False}, "timeout"),
    ("service.stop", {"startup_phase": "stopped", "shutdown_exit_code": 0,
                      "cleanup_pending": True}, "timeout"),
    ("app.quit", {"startup_phase": "stopped", "shutdown_exit_code": None,
                  "cleanup_pending": True}, "timeout"),
    ("service.restart", {"applied": True, "core_running": True,
                         "core_cleanup_pending": True, "core_generation": 6}, "conflict"),
])
def test_independent_failure_conditions_keep_partial_facts(name, raw, expected_error):
    executor, recorder = _executor()
    kwargs = ({"before": lambda args, ctx: 5} if name == "service.restart"
              else {"stop_intent": lambda args, ctx: StopIntent(True)})
    executor.register(name, lambda args, ctx: raw, **kwargs)
    try:
        ack = executor.submit(name, {})
        job = executor.wait(ack.op, 1)
        assert job["state"] == "failed"
        assert job["result"] == raw
        assert job["error"]["code"] == expected_error
    finally:
        executor.shutdown(time.monotonic() + 1)
    _events(recorder)


def test_restart_requires_a_real_generation_change_and_failure_keeps_facts():
    executor, recorder = _executor()
    executor.register("service.restart", lambda args, ctx: {
        "applied": True, "core_running": True, "core_cleanup_pending": False,
        "core_generation": 7,
    }, before=lambda args, ctx: 7)
    try:
        ack = executor.submit("service.restart", {})
        job = executor.wait(ack.op, 1)
        assert job["state"] == "failed"
        assert job["result"]["core_generation"] == 7
    finally:
        executor.shutdown(time.monotonic() + 1)
    _events(recorder)

    partial = {"startup_phase": "stopping", "shutdown_exit_code": None,
               "cleanup_pending": True}
    failed, failed_recorder = _executor()
    _register_stop(failed, execute=lambda args, ctx: (_ for _ in ()).throw(
        JobFailure("timeout", partial)
    ))
    try:
        ack = failed.submit("service.stop", {})
        job = failed.wait(ack.op, 1)
        assert job["state"] == "failed" and job["result"] == partial
    finally:
        failed.shutdown(time.monotonic() + 1)
    _events(failed_recorder)


def test_stop_intent_rejection_never_calls_stop_or_supersedes_start():
    start_entered = threading.Event()
    release = threading.Event()
    executed_stop = threading.Event()
    executor, recorder = _executor()
    executor.register("service.start", lambda args, ctx: (
        start_entered.set(), release.wait(),
        {"startup_phase": "running", "failure_code": None},
    )[-1])
    _register_stop(
        executor, intent=lambda args, ctx: StopIntent(False, "not_running"),
        execute=lambda args, ctx: executed_stop.set(),
    )
    try:
        start = executor.submit("service.start", {})
        assert start_entered.wait(1)
        stop = executor.submit("service.stop", {})
        assert not stop.wait_stop_intent(1).accepted
        assert executor.wait(stop.op, 1) is None
        assert not executed_stop.is_set()
        release.set()
        assert executor.wait(start.op, 1)["state"] == "done"
    finally:
        release.set()
        executor.shutdown(time.monotonic() + 1)
    events = _events(recorder)
    assert all(event["data"]["op"] != stop.op for event in events)


def test_running_cancel_callback_only_and_terminal_is_immutable():
    entered = threading.Event()
    released = threading.Event()
    cancelled = threading.Event()
    executor, recorder = _executor()
    executor.register("service.start", lambda args, ctx: (
        entered.set(), released.wait(),
        {"startup_phase": "running", "failure_code": None},
    )[-1], cancel=lambda args, ctx: (cancelled.set(), True)[-1])
    try:
        ack = executor.submit("service.start", {})
        assert entered.wait(1)
        assert executor.cancel(ack.op).accepted
        assert cancelled.wait(1)
        assert executor._jobs[ack.op].context.cancelled.wait(1)
        released.set()
        assert executor.wait(ack.op, 1)["state"] == "cancelled"
        assert not executor.cancel(ack.op).accepted
        assert executor.get_job(ack.op)["state"] == "cancelled"
    finally:
        released.set()
        executor.shutdown(time.monotonic() + 1)
    _events(recorder)


@pytest.mark.parametrize("cancel_result", [False, RuntimeError("refused")])
def test_cancel_callback_refusal_does_not_claim_cancelled(cancel_result):
    entered = threading.Event()
    release = threading.Event()
    called = threading.Event()
    executor, recorder = _executor()

    def on_cancel(args, ctx):
        called.set()
        if isinstance(cancel_result, Exception):
            raise cancel_result
        return cancel_result

    executor.register("service.start", lambda args, ctx: (
        entered.set(), release.wait(),
        {"startup_phase": "running", "failure_code": None},
    )[-1], cancel=on_cancel)
    try:
        ack = executor.submit("service.start", {})
        assert entered.wait(1)
        assert executor.cancel(ack.op).accepted
        assert called.wait(1)
        release.set()
        assert executor.wait(ack.op, 1)["state"] == "done"
    finally:
        release.set()
        executor.shutdown(time.monotonic() + 1)
    _events(recorder)


def test_bad_partial_result_does_not_kill_change_worker():
    executor, recorder = _executor()
    calls = [0]

    def execute(args, ctx):
        calls[0] += 1
        if calls[0] == 1:
            raise JobFailure("io", {"startup_phase": [], "failure_code": None})
        return {"startup_phase": "running", "failure_code": None}

    executor.register("service.start", execute)
    try:
        first = executor.submit("service.start", {})
        second = executor.submit("service.start", {})
        assert executor.wait(first.op, 1)["state"] == "failed"
        assert executor.wait(second.op, 1)["state"] == "done"
    finally:
        executor.shutdown(time.monotonic() + 1)
    _events(recorder)


def test_shutdown_deadline_ignores_stuck_ordinary_worker_and_keeps_control():
    normal_entered = threading.Event()
    release_normal = threading.Event()
    control_entered = threading.Event()
    release_control = threading.Event()
    executor, recorder = _executor()
    _register_stop(executor, execute=lambda args, ctx: (
        control_entered.set(), release_control.wait(),
        {"startup_phase": "stopped", "shutdown_exit_code": 0,
         "cleanup_pending": False},
    )[-1])
    assert executor.submit_normal(lambda: (normal_entered.set(), release_normal.wait()))
    assert normal_entered.wait(1)
    stop = executor.submit("service.stop", {})
    assert control_entered.wait(1)
    try:
        began = time.perf_counter()
        complete = executor.shutdown(time.monotonic() + 0.03)
        assert not complete
        assert time.perf_counter() - began < 0.15
        assert executor.submit("service.stop", {}).accepted is False
        assert executor._normal_threads[0].daemon
    finally:
        release_control.set()
        release_normal.set()
        assert executor.wait(stop.op, 1)["state"] == "done"
    _events(recorder)


def test_failed_quit_keeps_other_domains_closed_and_allows_quit_retry():
    executor, _ = _executor()
    started = []
    attempts = []
    executor.register("service.start", lambda args, ctx: (
        started.append(True),
        {"startup_phase": "running", "failure_code": None},
    )[-1])
    _register_stop(executor)

    def quit_execute(args, ctx):
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("stop failed")
        return {"startup_phase": "stopped", "shutdown_exit_code": 0,
                "cleanup_pending": False}

    executor.register("app.quit", quit_execute,
                      stop_intent=lambda args, ctx: StopIntent(True))
    try:
        first = executor.submit("app.quit", {})
        assert executor.wait(first.op, 1)["state"] == "failed"
        assert not executor.submit("service.start", {}).accepted
        assert not executor.submit("service.stop", {}).accepted
        assert not executor.submit_normal(lambda: started.append(True))
        assert started == []
        retry = executor.submit("app.quit", {})
        assert retry.accepted and retry.op != first.op
        assert executor.wait(retry.op, 1)["state"] == "done"
        assert len(attempts) == 2 and started == []
    finally:
        executor.shutdown(time.monotonic() + 1)


def test_send_ack_preserves_event_prefix_until_submission_confirmed():
    executor, recorder = _executor()
    executor.register("service.start", lambda args, ctx: {
        "startup_phase": "running", "failure_code": None,
    })
    try:
        ack = executor.submit("service.start", {})
        assert executor.wait(ack.op, 1)["state"] == "done"
        first = recorder.prepare_for_send(5)
        with pytest.raises(RuntimeError):
            recorder.prepare_for_send(6)
        recorder.abort_send()
        retry = recorder.prepare_for_send(9)
        assert [item["seq"] for item in first] == [item["seq"] for item in retry]
        assert recorder.pending == 3
        with pytest.raises(ValueError):
            recorder.ack_sent(retry[1:])
        recorder.ack_sent(retry)
        assert recorder.pending == 0
    finally:
        executor.shutdown(time.monotonic() + 1)
