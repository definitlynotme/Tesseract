import threading
import time
from types import SimpleNamespace

from bridge_main import read_loop
from bridge_jobs import JobEventRecorder, JobExecutor
from bridge_runtime_access import RuntimeAccess
from bridge_shutdown import ShutdownCoordinator


class Gate:
    def __init__(self, ready=True):
        self.ready = ready

    def require_idle(self):
        if not self.ready:
            raise RuntimeError("still running")


class Report(dict):
    def __init__(self, gate, state="restored"):
        super().__init__({name: {"state": state, "error_code": None,
                                "remaining_count": None}
                          for name in ("proxy", "firewall")})
        self.gate = gate


class Runtime:
    def __init__(self):
        self.calls = []
        self.intent = SimpleNamespace(accepted=True, generation=1,
                                      lifecycle_revision=2, skipped_reason="")
        self.stop_gate = None
        self.exit_code = 0

    def request_stop(self, **kwargs):
        self.calls.append(("request", kwargs))
        return self.intent

    def stop(self, *, stop_intent):
        assert stop_intent is self.intent
        self.calls.append(("stop", stop_intent))
        if self.stop_gate is not None:
            self.stop_gate.wait()
        return {"shutdown_exit_code": self.exit_code}

    def lifecycle_status(self):
        return {"intent": {}, "runtime": {"startup_phase": "stopped",
                "shutdown_cleanup_pending": False},
                "core": {"cleanup_pending": False}, "readiness": {},
                "freshness": {}}


def setup(*, startup_gate=None, recovered=None, send=None, poll_write=None,
          normal_workers=2, normal_queue=2):
    runtime = Runtime()
    events = []
    lease = SimpleNamespace(close=lambda: events.append("lease"))
    executor = JobExecutor(JobEventRecorder(), epoch="test-epoch",
                           normal_workers=normal_workers, normal_queue=normal_queue)
    if startup_gate is None:
        startup_gate = Gate()
    if recovered is None:
        recovered = Report(Gate())

    def recover(_remaining):
        events.append("recover")
        return recovered

    shutdown = ShutdownCoordinator(
        executor, RuntimeAccess(runtime), lease=lease,
        startup_recovery=Report(startup_gate), recover_network=recover,
        send=send or (lambda frame: (events.append(frame), "complete")[1]),
        poll_write=poll_write,
        exit_process=lambda code: events.append(("exit", code)),
        close_diagnostics=lambda remaining: events.append("diagnostics"),
    )
    return shutdown, runtime, events


def test_stop_uses_exact_intent_without_exit_diagnostics():
    shutdown, runtime, events = setup()
    try:
        assert not shutdown.quitting
        ack = shutdown.request_stop(expected_generation=1)
        assert ack.wait_stop_intent(1).accepted
        assert shutdown.executor.wait(ack.op, 1)["state"] == "done"
        assert runtime.calls == [("request", {"expected_generation": 1}),
                                 ("stop", runtime.intent)]
        assert events == []
        assert not shutdown.quitting
    finally:
        shutdown.executor.shutdown(time.monotonic() + 1)


def test_stop_waits_for_lifecycle_lock_and_job_remains_running():
    shutdown, runtime, _ = setup()
    runtime.stop_gate = threading.Event()
    try:
        ack = shutdown.request_stop()
        assert ack.wait_stop_intent(1).accepted
        assert shutdown.executor.wait(ack.op, .03) is None
        assert shutdown.executor.get_job(ack.op)["state"] == "running"
        runtime.stop_gate.set()
        assert shutdown.executor.wait(ack.op, 1)["state"] == "done"
    finally:
        runtime.stop_gate.set()
        shutdown.executor.shutdown(time.monotonic() + 1)


def test_blocked_intent_does_not_block_ping_read_loop():
    entered, release = threading.Event(), threading.Event()
    shutdown, runtime, _ = setup()
    original = runtime.request_stop

    def blocked_request(**kwargs):
        entered.set()
        release.wait()
        return original(**kwargs)

    runtime.request_stop = blocked_request
    ack = shutdown.request_stop()
    assert entered.wait(1)
    now = 1
    class Transport:
        frames = [{"t": "ping", "epoch": "test-epoch", "produced_at_ms": now,
                   "sent_at_ms": now, "n": 7}, None]
        replies = []
        def receive(self):
            return self.frames.pop(0)
        def send(self, frame):
            self.replies.append(frame)
    transport = Transport()
    try:
        read_loop(transport, "test-epoch")
        assert transport.replies[0]["t"] == "pong"
        assert transport.replies[0]["n"] == 7
        assert ack.wait_stop_intent(.01) is None
    finally:
        release.set()
        shutdown.executor.wait(ack.op, 1)
        shutdown.executor.shutdown(time.monotonic() + 1)


def test_quit_keeps_control_available_with_stuck_ordinary_work():
    shutdown, runtime, events = setup()
    entered, release = threading.Event(), threading.Event()
    assert shutdown.executor.submit_normal(lambda: (entered.set(), release.wait()))
    assert entered.wait(1)
    try:
        ack = shutdown.request_quit()
        assert ack.wait_stop_intent(1).accepted
        assert shutdown.executor.wait(ack.op, 1)["state"] == "done"
        assert runtime.calls[0] == ("request", {"actor": "app_exit"})
        assert runtime.calls[1] == ("stop", runtime.intent)
        assert shutdown.finish_quit(deadline_seconds=.1)
        assert [item for item in events if isinstance(item, str)] == [
            "recover", "diagnostics", "lease"]
        assert events[-1] == ("exit", 0)
        assert [item["t"] for item in events if isinstance(item, dict)] == ["bye"]
        assert shutdown.executor.submit_normal(lambda: None) is False
    finally:
        release.set()


def test_disconnect_has_no_bye_and_reports_actual_failure():
    recovered = Report(Gate(), state="failed")
    shutdown, _, events = setup(recovered=recovered)
    ack = shutdown.on_disconnect()
    assert shutdown.executor.wait(ack.op, 1)["state"] == "done"
    assert shutdown.finish_quit()
    assert not any(isinstance(item, dict) for item in events)
    assert events[-1] == ("exit", 0)
    assert recovered["proxy"]["state"] == "failed"


def test_closed_startup_gate_forbids_second_recovery_and_lease_release():
    gate = Gate(False)
    shutdown, runtime, events = setup(startup_gate=gate)
    ack = shutdown.request_quit()
    assert ack.accepted
    assert ack.wait_stop_intent(.03) is None
    assert shutdown.executor.get_job(ack.op)["state"] == "queued"
    assert not shutdown.finish_quit(deadline_seconds=.01)
    assert events == []
    assert runtime.calls == []
    gate.ready = True
    assert shutdown.executor.wait(ack.op, 1)["state"] == "done"
    assert shutdown.finish_quit()
    assert events[0] == "recover"


def test_closed_gate_blocks_stop_and_disconnect_until_original_writer_idle():
    gate = Gate(False)
    shutdown, runtime, events = setup(startup_gate=gate)
    stop = shutdown.request_stop()
    assert stop.accepted and stop.wait_stop_intent(.03) is None
    assert runtime.calls == []
    quit_ack = shutdown.on_disconnect()
    assert quit_ack.accepted
    assert not shutdown.finish_quit(deadline_seconds=.02)
    assert runtime.calls == [] and events == []
    gate.ready = True
    assert shutdown.executor.wait(stop.op, 1)["state"] == "done"
    assert shutdown.executor.wait(quit_ack.op, 1)["state"] == "done"
    assert shutdown.finish_quit()
    assert not any(isinstance(item, dict) for item in events)


def test_closed_gate_blocks_fatal_before_runtime_side_effects():
    gate = Gate(False)
    shutdown, runtime, events = setup(startup_gate=gate)
    ack = shutdown.on_fatal()
    assert ack.accepted and ack.wait_stop_intent(.03) is None
    assert runtime.calls == [] and events == []
    gate.ready = True
    assert shutdown.executor.wait(ack.op, 1)["state"] == "done"
    assert shutdown.finish_quit()
    assert events[-1] == ("exit", 1)


def test_failed_quit_intent_can_retry_from_fatal_or_disconnect():
    for trigger in ("on_fatal", "on_disconnect"):
        shutdown, runtime, events = setup()
        started = []
        shutdown.executor.register("service.start", lambda args, ctx: (
            started.append(True),
            {"startup_phase": "running", "failure_code": None},
        )[-1])
        original = runtime.request_stop
        calls = 0
        def fail_once(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("request failed")
            return original(**kwargs)
        runtime.request_stop = fail_once
        assert not shutdown.quitting
        first = getattr(shutdown, trigger)()
        assert shutdown.quitting
        assert first.wait_stop_intent(1).accepted is False
        assert shutdown.executor.get_job(first.op) is None
        assert not shutdown.executor.submit("service.start", {}).accepted
        assert not shutdown.executor.submit_normal(lambda: started.append(True))
        assert started == []
        assert not shutdown.finish_quit(deadline_seconds=0)
        assert shutdown.quitting
        second = getattr(shutdown, trigger)()
        assert second.op != first.op
        assert shutdown.executor.wait(second.op, 1)["state"] == "done"
        assert not shutdown.executor.submit("service.start", {}).accepted
        assert started == []
        assert shutdown.finish_quit()
        assert shutdown.quitting
        assert calls == 2 and events[-1][0] == "exit"


def test_bye_busy_then_pending_must_complete_before_lease_release():
    events = []
    sends = iter(["busy", "pending"])
    polls = iter(["pending", "complete"])
    def send(frame):
        events.append("send")
        return next(sends)
    def poll():
        events.append("poll")
        return next(polls)
    shutdown, _, cleanup = setup(send=send, poll_write=poll)
    ack = shutdown.request_quit()
    assert shutdown.executor.wait(ack.op, 1)["state"] == "done"
    assert shutdown.finish_quit(deadline_seconds=1)
    assert events == ["send", "send", "poll", "poll"]
    assert cleanup[-2:] == ["lease", ("exit", 0)]


def test_bye_pending_timeout_keeps_lease_and_retries_poll_only():
    sent = []
    def send(frame):
        sent.append(frame)
        return "pending"
    shutdown, _, events = setup(send=send, poll_write=lambda: "pending")
    ack = shutdown.request_quit()
    assert shutdown.executor.wait(ack.op, 1)["state"] == "done"
    assert not shutdown.finish_quit(deadline_seconds=.02)
    assert len(sent) == 1 and "lease" not in events
    assert shutdown.bye_submitted
    # Submitted bye retries must never reenter diagnostics or ordinary publishing.
    shutdown.close_diagnostics = lambda _: (_ for _ in ()).throw(AssertionError("late close"))
    shutdown.drain_publications = lambda: (_ for _ in ()).throw(AssertionError("late publish"))
    shutdown.poll_write = lambda: "complete"
    assert shutdown.finish_quit(deadline_seconds=.1)
    assert len(sent) == 1


def test_disconnect_after_bye_pending_only_finishes_owned_cleanup():
    sent = []
    shutdown, runtime, events = setup(
        send=lambda frame: sent.append(frame) or "pending", poll_write=lambda: "pending")
    ack = shutdown.request_quit()
    assert shutdown.executor.wait(ack.op, 1)["state"] == "done"
    assert not shutdown.finish_quit(deadline_seconds=.02)
    assert shutdown.bye_submitted and "lease" not in events
    shutdown.close_diagnostics = lambda _: (_ for _ in ()).throw(AssertionError("late close"))
    shutdown.drain_publications = lambda: (_ for _ in ()).throw(AssertionError("late publish"))
    shutdown.on_disconnect()
    assert shutdown.finish_quit(deadline_seconds=0)
    assert shutdown.finish_quit(deadline_seconds=0)
    assert len(sent) == 1
    assert events.count("recover") == events.count("lease") == 1
    assert runtime.calls == [("request", {"actor": "app_exit"}), ("stop", runtime.intent)]
    assert events[-1] == ("exit", 0)


def test_bye_oserror_requires_disconnect_before_release():
    shutdown, _, events = setup(send=lambda frame: (_ for _ in ()).throw(OSError(5, "write")))
    ack = shutdown.request_quit()
    assert shutdown.executor.wait(ack.op, 1)["state"] == "done"
    assert not shutdown.finish_quit(deadline_seconds=.02)
    assert "lease" not in events
    shutdown.on_disconnect()
    assert shutdown.finish_quit(deadline_seconds=.1)
    assert events[-2:] == ["lease", ("exit", 0)]


def test_old_api_intent_cleanup_failure_preserves_exit_74():
    shutdown, runtime, events = setup()
    entered, release = threading.Event(), threading.Event()
    def incomplete(*, stop_intent):
        assert stop_intent is runtime.intent
        entered.set()
        release.wait()
        return {"cleanup_pending": True, "startup_phase": "failed"}
    runtime.stop = incomplete
    old = shutdown.request_stop()
    assert entered.wait(1)
    ack = shutdown.request_quit()
    assert shutdown.executor.get_job(old.op)["state"] == "running"
    release.set()
    assert shutdown.executor.wait(old.op, 1)["state"] == "failed"
    assert shutdown.executor.wait(ack.op, 1)["state"] == "failed"
    assert shutdown.finish_quit()
    assert events[-1] == ("exit", 74)
    assert runtime.calls == [("request", {"expected_generation": None}),
                             ("request", {"actor": "app_exit"})]


def test_full_normal_pool_and_blocked_manager_boundary_keep_dispatch_responsive():
    shutdown, runtime, events = setup(normal_workers=2, normal_queue=2)
    entered = [threading.Event(), threading.Event()]
    release = threading.Event()
    for marker in entered:
        assert shutdown.executor.submit_normal(
            lambda marker=marker: (marker.set(), release.wait()))
    assert all(marker.wait(1) for marker in entered)
    queued_work = []
    assert shutdown.executor.submit_normal(lambda: queued_work.append(1))
    assert shutdown.executor.submit_normal(lambda: queued_work.append(2))
    assert not shutdown.executor.submit_normal(lambda: None)
    manager_entered = threading.Event()
    original = runtime.request_stop
    def manager_blocked(**kwargs):
        manager_entered.set()
        release.wait()
        return original(**kwargs)
    runtime.request_stop = manager_blocked
    replies = []
    def dispatch(frame):
        if frame["t"] == "ping":
            replies.append(("pong", frame["n"]))
        elif frame["name"] == "service.stop":
            return shutdown.request_stop()
        elif frame["name"] == "app.quit":
            return shutdown.request_quit()
    try:
        stop = dispatch({"t": "req", "name": "service.stop"})
        assert manager_entered.wait(1)
        dispatch({"t": "ping", "n": 9})
        quit_ack = dispatch({"t": "req", "name": "app.quit"})
        dispatch({"t": "ping", "n": 10})
        assert replies == [("pong", 9), ("pong", 10)]
        assert stop.accepted and quit_ack.accepted
        started = time.monotonic()
        assert not shutdown.finish_quit(deadline_seconds=.03)
        assert time.monotonic() - started < .2
        assert "lease" not in events
    finally:
        release.set()
    assert shutdown.executor.wait(stop.op, 1)["state"] == "done"
    assert shutdown.executor.wait(quit_ack.op, 1)["state"] == "done"
    assert queued_work == []
    started = time.monotonic()
    assert shutdown.finish_quit(deadline_seconds=.2)
    assert time.monotonic() - started < .2


def test_lifecycle_wait_keeps_same_dispatch_path_responsive():
    shutdown, runtime, events = setup()
    runtime.stop_gate = threading.Event()
    replies = []
    def dispatch(frame):
        if frame["t"] == "ping":
            replies.append(("pong", frame["n"]))
        elif frame["name"] == "service.stop":
            return shutdown.request_stop()
        else:
            return shutdown.request_quit()
    stop = dispatch({"t": "req", "name": "service.stop"})
    assert stop.wait_stop_intent(1).accepted
    assert shutdown.executor.get_job(stop.op)["state"] == "running"
    quit_ack = dispatch({"t": "req", "name": "app.quit"})
    dispatch({"t": "ping", "n": 11})
    assert replies == [("pong", 11)]
    started = time.monotonic()
    assert not shutdown.finish_quit(deadline_seconds=.02)
    assert time.monotonic() - started < .2
    assert "lease" not in events
    runtime.stop_gate.set()
    assert shutdown.executor.wait(stop.op, 1)["state"] == "done"
    assert shutdown.executor.wait(quit_ack.op, 1)["state"] == "done"
    assert shutdown.finish_quit(deadline_seconds=.2)


def test_recovery_gate_preserved_until_idle_and_failure_not_relabelled():
    gate = Gate(False)
    shutdown, _, events = setup(recovered=Report(gate, state="unknown"))
    ack = shutdown.request_quit()
    assert shutdown.executor.wait(ack.op, 1)["state"] == "done"
    assert not shutdown.finish_quit(deadline_seconds=.01)
    assert events == ["recover"]
    gate.ready = True
    assert shutdown.finish_quit()
    bye = next(item for item in events if isinstance(item, dict))
    assert bye["network_recovery"]["proxy"]["state"] == "unknown"


def test_blocked_recovery_keeps_lease_and_is_not_started_twice():
    shutdown, _, events = setup()
    entered, release = threading.Event(), threading.Event()
    calls = []
    def recover(_remaining):
        calls.append(True)
        entered.set()
        release.wait()
        return Report(Gate())
    shutdown.recover_network = recover
    ack = shutdown.request_quit()
    assert shutdown.executor.wait(ack.op, 1)["state"] == "done"
    try:
        assert not shutdown.finish_quit(deadline_seconds=.02)
        assert entered.is_set()
        assert events == []
        assert not shutdown.finish_quit(deadline_seconds=.02)
        assert len(calls) == 1
    finally:
        release.set()
    assert shutdown.finish_quit(deadline_seconds=1)
    assert events[-1] == ("exit", 0)


def test_fatal_uses_same_quit_and_exit_74():
    shutdown, runtime, events = setup()
    runtime.exit_code = 74
    ack = shutdown.on_fatal()
    assert shutdown.executor.wait(ack.op, 1)["state"] == "failed"
    assert shutdown.finish_quit()
    assert events[-1] == ("exit", 74)
    bye = next(item for item in events if isinstance(item, dict))
    assert bye["exit_code"] == 74


def test_fatal_after_successful_stop_exits_nonzero_only_after_cleanup():
    shutdown, _, events = setup()
    ack = shutdown.on_fatal()
    assert shutdown.executor.wait(ack.op, 1)["state"] == "done"
    assert shutdown.finish_quit()
    assert events[-1] == ("exit", 1)
    assert events.index("lease") < len(events) - 1


def test_stop_rejected_intent_never_calls_stop():
    shutdown, runtime, _ = setup()
    runtime.intent = SimpleNamespace(accepted=False, generation=2,
                                     lifecycle_revision=2,
                                     skipped_reason="stale_runtime_generation")
    ack = shutdown.request_stop(expected_generation=1)
    intent = ack.wait_stop_intent(1)
    assert not intent.accepted and intent.skipped_reason == "conflict"
    assert shutdown.executor.wait(ack.op, .01) is None
    assert not any(name == "stop" for name, _ in runtime.calls)
    shutdown.executor.shutdown(time.monotonic() + 1)
