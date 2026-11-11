"""T-P2-31a: executor public surface, deferred completion, layered secret
registry, mutation recovery gate (plus the P2-17 cleanup-failed receipt,
the sampling domain and alerts.desktopDelivered's control/inline domain)."""
from __future__ import annotations

from copy import deepcopy
import json
import logging
from pathlib import Path
import re
import threading
import time
from types import SimpleNamespace

import pytest

import bridge_alerts
import bridge_exit as bx
import bridge_protocol as protocol
from bridge_alerts_desktop import DesktopAlertOwner
from bridge_commands import COMMAND_SPECS
from bridge_jobs import (CancellationReport, JobEventRecorder, JobExecutor, JobFailure,
                         Pending)
from bridge_secrets import SecretRegistry, session_registry
from bridge_state import PublicationState


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "bridge_schema/fixtures/v1/valid"
EPOCH = "epoch-31a"


def fixture(name, prefix="result"):
    frame = json.loads((FIXTURES / f"{prefix}_{name}.json").read_bytes())
    if prefix == "command":
        return frame["args"]
    return frame["data"].get("result", frame["data"])


def make_executor(**kwargs):
    return JobExecutor(JobEventRecorder(4096), epoch=EPOCH, require_acceptance=True, **kwargs)


def start(executor, name, args=None, owner="window-1", req_id="r-1"):
    ack = executor.submit(name, fixture(name, "command") if args is None else args,
                          req_id=req_id, owner=owner)
    assert ack.accepted, ack
    if ack.outlet == "job":
        assert executor.acceptance_sent(ack.op, epoch=EPOCH)
    return ack


def wait_for(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(.005)
    return predicate()


@pytest.fixture
def executor():
    ex = make_executor()
    yield ex
    ex.shutdown(time.monotonic() + 1)


# ---------------------------------------------------------------- static gate

# 31a review: ``executor\w*`` without a leading word boundary also covers
# ``self._executor._specs`` and ``self._executor()._jobs``.
PRIVATE = re.compile(
    r"executor\w*(?:\(\))?\._[a-z]"
    r"|getattr\(\s*[\w.()]*executor[\w.()]*\s*,\s*[\"']_"
    r"|\.executor\._[a-z]")


def test_no_bridge_module_reads_executor_private_members():
    offenders = []
    for path in sorted(ROOT.glob("bridge_*.py")):
        if path.name == "bridge_jobs.py":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if PRIVATE.search(line):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert offenders == []


def test_static_gate_pattern_catches_the_old_access_forms():
    for line in ('with executor._lock:', 'job = self.executor._jobs.get(op)',
                 'table = getattr(executor, "_revoked_sessions", None)',
                 'lock = executor._mutation_lock', 'executor()._specs',
                 'specs = self._executor._specs', 'jobs = self._executor()._jobs'):
        assert PRIVATE.search(line), line
    for line in ('executor.owner_of(op)', 'self.executor.is_registered(name)',
                 'getattr(executor, "submit_normal", None)'):
        assert not PRIVATE.search(line), line


# ---------------------------------------------------------------- public surface

def test_public_lookups_registration_and_load(executor):
    gate = threading.Event()
    executor.register("app.update.check", lambda args, ctx: (gate.wait(5), fixture("app.update.check"))[1])
    assert executor.is_registered("app.update.check") and not executor.is_registered("mode.set")
    assert executor.is_registered(object()) is False
    ack = start(executor, "app.update.check", owner="w1")
    assert executor.owner_of(ack.op) == "w1"
    assert executor.job_identity(ack.op) == (EPOCH, "w1")
    assert executor.owner_of("job-missing") is None and executor.job_identity("x") is None
    assert executor.owner_load("w1") == 1 and executor.owner_load("w2") == 0
    assert executor.accepting
    gate.set()
    assert executor.wait(ack.op, 5)["state"] == "done"
    executor.begin_shutdown()
    assert executor.accepting is False


def test_run_internal_mutation_shares_the_change_lock_and_honours_abandon(executor):
    entered, release = threading.Event(), threading.Event()
    executor.register("mode.set", lambda args, ctx: (entered.set(), release.wait(5),
                                                      fixture("mode.set"))[2])
    ack = start(executor, "mode.set")
    assert entered.wait(5)
    abandoned = threading.Event()
    box = {}
    thread = threading.Thread(target=lambda: box.update(value=executor.run_internal_mutation(
        lambda: "ran", abandoned=abandoned.is_set, poll=.01)))
    thread.start()
    time.sleep(.1)
    assert "value" not in box          # waits behind the running mutation
    abandoned.set()
    thread.join(5)
    assert box["value"] == (False, None)
    release.set()
    assert executor.wait(ack.op, 5)["state"] == "done"
    assert executor.run_internal_mutation(lambda: 7) == (True, 7)
    with pytest.raises(RuntimeError):
        executor.run_internal_mutation(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert executor.run_internal_mutation(lambda: 8) == (True, 8)   # lock released


def test_terminal_listener_gets_owned_job_facts_outside_the_lock(executor):
    seen = []
    unfiltered = []

    def listener(event):
        # Outside the executor lock: a public read must not deadlock.
        executor.owner_of(event["op"])
        seen.append(event)
        event["args"]["mutated"] = True
    executor.add_terminal_listener(listener, names={"app.update.check"})
    executor.add_terminal_listener(unfiltered.append)
    with pytest.raises(ValueError):
        executor.add_terminal_listener("not callable")
    for bad in ("app.update.check", (), [""]):
        with pytest.raises(ValueError):
            executor.add_terminal_listener(seen.append, names=bad)
    executor.register("app.update.check", lambda args, ctx: fixture("app.update.check"))
    executor.register("settings.validate", lambda args, ctx: {"valid": True, "errors": []})
    ack = start(executor, "app.update.check")
    receipt = executor.submit("settings.validate", fixture("settings.validate", "command"),
                              req_id="r-v", owner="window-1")
    assert receipt.wait(5).ok
    assert wait_for(lambda: len(seen) == 1)
    time.sleep(.05)
    assert [event["state"] for event in seen] == ["done"]   # one-shot res never reported
    assert seen[0]["op"] == ack.op and seen[0]["name"] == "app.update.check"
    assert seen[0]["owner"] == "window-1" and seen[0]["epoch"] == EPOCH
    assert seen[0]["args"] == {"mutated": True}      # owned copy of {} args
    # Without a name filter: every job, never its args (they may be secret).
    assert wait_for(lambda: len(unfiltered) == 1)
    assert unfiltered[0]["op"] == ack.op and "args" not in unfiltered[0]
    assert executor.get_job(ack.op)["state"] == "done"
    replacement = executor.advance_epoch("epoch-31a-next", JobEventRecorder())
    try:
        ack2 = replacement.submit("app.update.check", {}, req_id="r-9", owner="window-1")
        assert replacement.acceptance_sent(ack2.op, epoch="epoch-31a-next")
        assert wait_for(lambda: len(seen) == 2)
        assert seen[1]["epoch"] == "epoch-31a-next"
    finally:
        replacement.shutdown(time.monotonic() + 1)


def test_terminal_listener_args_only_for_subscribed_names(executor):
    seen = []
    executor.add_terminal_listener(seen.append, names={"logs.exportBundle"})
    executor.register("app.update.check", lambda args, ctx: fixture("app.update.check"))
    executor.register("logs.exportBundle", lambda args, ctx: fixture("logs.exportBundle"))
    other = start(executor, "app.update.check")
    assert executor.wait(other.op, 5)["state"] == "done"
    args = fixture("logs.exportBundle", "command")
    ack = start(executor, "logs.exportBundle", args, req_id="r-2")
    assert executor.wait(ack.op, 5) is not None
    assert wait_for(lambda: len(seen) == 1)
    time.sleep(.05)
    assert [event["name"] for event in seen] == ["logs.exportBundle"]
    assert seen[0]["args"] == args and seen[0]["args"] is not args


def test_a_listener_may_end_its_executor_without_deadlocking_the_supervisor(executor):
    """31a review: the callback drain is re-entrant on the supervisor."""
    returned = threading.Event()
    later = []

    def closing(event):
        executor.begin_shutdown()      # drains callbacks again on this thread
        returned.set()
    executor.add_terminal_listener(closing, names={"app.update.check"})
    executor.add_terminal_listener(later.append)
    executor.register("app.update.check", lambda args, ctx: fixture("app.update.check"))
    ack = start(executor, "app.update.check")
    assert executor.wait(ack.op, 5)["state"] == "done"
    assert returned.wait(3)
    assert wait_for(lambda: len(later) == 1)


def test_ingress_marker_is_stamped_at_admission_and_preempt_cancels(executor):
    ran = threading.Event()
    executor.register("app.update.check", lambda args, ctx: (ran.set(), fixture("app.update.check"))[1])
    ack = executor.submit("app.update.check", {}, req_id="r-1", owner="w1", ingress=("e", 3))
    markers = []
    assert executor.acceptance_sent(ack.op, epoch=EPOCH,
                                    preempt=lambda marker: markers.append(marker) or True) is False
    assert markers == [("e", 3)]
    assert executor.wait(ack.op, 5)["state"] == "cancelled" and not ran.is_set()
    ack = executor.submit("app.update.check", {}, req_id="r-2", owner="w1", ingress=("e", 9))
    assert executor.acceptance_sent(ack.op, epoch=EPOCH, preempt=lambda marker: False)
    assert executor.wait(ack.op, 5)["state"] == "done"


# ---------------------------------------------------------------- deferred completion

def test_three_deferred_jobs_hold_no_worker_while_queries_still_run(executor):
    handles = []
    executor.register("app.update.check", lambda args, ctx: handles.append(Pending()) or handles[-1])
    executor.register("settings.validate", lambda args, ctx: {"valid": True, "errors": []})
    acks = [start(executor, "app.update.check", req_id=f"r-{i}") for i in range(3)]
    assert wait_for(lambda: len(handles) == 3)
    assert all(executor.get_job(ack.op)["state"] == "running" for ack in acks)
    # Two normal workers only, yet a query and plain ordinary work still run.
    receipt = executor.submit("settings.validate", fixture("settings.validate", "command"),
                              req_id="r-q", owner="window-2")
    response = receipt.wait(3)
    assert response is not None and response.ok
    done = threading.Event()
    assert executor.submit_normal(done.set) and done.wait(3)
    for handle in handles:
        assert handle.complete(fixture("app.update.check")) is True
        assert handle.complete(fixture("app.update.check")) is False   # exactly once
    for ack in acks:
        job = executor.wait(ack.op, 5)
        assert job["state"] == "done" and job["result"] == fixture("app.update.check")


def test_deferred_result_goes_through_the_adapter_and_error_adoption(executor):
    handles = []
    executor.register("app.update.check", lambda args, ctx: handles.append(Pending()) or handles[-1])
    bad, failed, adopted, foreign = (start(executor, "app.update.check", req_id=f"r-{i}")
                                     for i in range(4))
    assert wait_for(lambda: len(handles) == 4)
    handles[0].complete({"state": "available"})                  # incomplete result
    handles[1].fail("timeout")
    error = {"code": "io", "message_key": "errors.update.download_failed", "params": {},
             "reason_code": "download_failed"}
    handles[2].fail(JobFailure("io", error=error))
    handles[3].fail(JobFailure("conflict", error={"code": "needs_route",  # unregistered here
                                                  "message_key": "errors.x.y", "params": {}}))
    assert executor.wait(bad.op, 5)["error"]["code"] == "internal"
    assert executor.wait(failed.op, 5)["error"] == {
        "code": "timeout", "message_key": "errors.timeout", "params": {}}
    assert executor.wait(adopted.op, 5)["error"] == error
    assert executor.wait(foreign.op, 5)["error"]["message_key"] == "errors.conflict"


def test_job_failure_error_is_adopted_only_when_valid_and_registered(executor):
    errors = iter([
        {"code": "timeout", "message_key": "errors.update.slow", "params": {}, "reason_code": "slow"},
        {"code": "timeout", "message_key": "errors.update.slow", "params": {"url": "https://x/y"}},
        {"code": "timeout", "message_key": "not a key", "params": {}},
        {"code": "not_running", "message_key": "errors.update.slow", "params": {}},
    ])

    def handler(args, ctx):
        error = next(errors)
        raise JobFailure(error["code"], error=error)
    executor.register("app.update.check", handler)
    jobs = [executor.wait(start(executor, "app.update.check", req_id=f"r-{i}").op, 5)
            for i in range(4)]
    assert jobs[0]["error"]["message_key"] == "errors.update.slow"
    assert jobs[0]["error"]["reason_code"] == "slow"
    assert [job["error"]["message_key"] for job in jobs[1:]] == [
        "errors.timeout", "errors.timeout", "errors.internal"]


def test_deferred_owner_cancel_abandons_the_handle(executor):
    handles, abandoned = [], []
    executor.register("exit.verify", lambda args, ctx: handles.append(
        Pending(on_abandon=abandoned.append)) or handles[-1])
    ack = start(executor, "exit.verify")
    assert wait_for(lambda: len(handles) == 1)
    assert executor.cancel(ack.op, owner="window-1").accepted
    assert executor.wait(ack.op, 5)["state"] == "cancelled"
    assert wait_for(lambda: abandoned == ["cancelled"])
    assert handles[0].abandoned.is_set() and handles[0].abandon_reason == "cancelled"
    assert handles[0].complete(fixture("exit.verify")) is False
    assert executor.get_job(ack.op)["state"] == "cancelled"


def test_deferred_queued_mode_job_cannot_be_cancelled_once_running(executor):
    handles = []
    executor.register("app.update.check", lambda args, ctx: handles.append(Pending()) or handles[-1])
    ack = start(executor, "app.update.check")
    assert wait_for(lambda: len(handles) == 1)
    refused = executor.cancel(ack.op, owner="window-1")
    assert not refused.accepted and refused.skipped_reason == "not_cancellable"
    handles[0].complete(fixture("app.update.check"))
    assert executor.wait(ack.op, 5)["state"] == "done"


def test_deferred_timeout_is_capped_and_uses_on_timeout():
    ex = make_executor(max_pending_seconds=.2)
    try:
        handles, abandoned = [], []
        results = iter([Pending(timeout=60),                      # capped to 0.2 s
                        Pending(timeout=.05, on_abandon=abandoned.append,
                                on_timeout=lambda: {"ok": False, "delivery_id": None,
                                                    "error": {"code": "timeout",
                                                              "message_key": "errors.timeout",
                                                              "params": {}}})])

        def handler(args, ctx):
            handles.append(next(results))
            return handles[-1]
        ex.register("exit.verify", handler)
        ex.register("alerts.test", handler)
        first = start(ex, "exit.verify")
        second = start(ex, "alerts.test", req_id="r-2")
        started = time.monotonic()
        job = ex.wait(first.op, 5)
        assert job["state"] == "failed" and job["error"]["code"] == "timeout"
        assert time.monotonic() - started < 2
        job = ex.wait(second.op, 5)
        assert job["state"] == "failed" and job["error"]["code"] == "timeout"
        assert job["result"]["ok"] is False
        assert wait_for(lambda: abandoned == ["timeout"])
        assert handles[0].complete(fixture("exit.verify")) is False
    finally:
        ex.shutdown(time.monotonic() + 1)


def test_epoch_change_invalidates_handles_and_ends_waiters(executor):
    handles, abandoned = [], []
    executor.register("alerts.test", lambda args, ctx: handles.append(
        Pending(on_abandon=abandoned.append)) or handles[-1])
    executor.register("alerts.desktopDelivered", lambda args, ctx: handles.append(
        Pending(on_abandon=abandoned.append)) or handles[-1])
    ack = start(executor, "alerts.test")
    receipt = executor.submit("alerts.desktopDelivered",
                              fixture("alerts.desktopDelivered", "command"),
                              req_id="r-d", owner="shell-1")
    assert wait_for(lambda: len(handles) == 2)
    replacement = executor.advance_epoch("epoch-31a-b", JobEventRecorder())
    try:
        job = executor.wait(ack.op, 5)
        assert job["state"] == "failed" and job["error"]["code"] == "runtime_gone"
        response = receipt.wait(5)
        assert response is not None and not response.ok
        assert response.error["code"] == "runtime_gone"
        assert wait_for(lambda: sorted(abandoned) == ["epoch", "epoch"])
        assert all(handle.complete({}) is False for handle in handles)
    finally:
        replacement.shutdown(time.monotonic() + 1)


def test_settled_before_return_and_reused_or_forbidden_handles(executor):
    early = Pending()
    assert early.complete(fixture("app.update.check")) is True   # before the executor binds
    shared = Pending()
    executor.register("app.update.check", lambda args, ctx: early)
    executor.register("geo.update", lambda args, ctx: shared)
    executor.register("mode.set", lambda args, ctx: Pending())
    assert executor.wait(start(executor, "app.update.check").op, 5)["state"] == "done"
    assert executor.wait(start(executor, "app.update.check", req_id="r-2").op, 5)[
        "error"]["code"] == "internal"                              # reused handle
    first = start(executor, "geo.update", req_id="r-3")
    second = start(executor, "geo.update", req_id="r-4")
    assert executor.wait(second.op, 5)["error"]["code"] == "internal"
    shared.complete(fixture("geo.update"))
    assert executor.wait(first.op, 5)["state"] == "done"
    job = executor.wait(start(executor, "mode.set", req_id="r-5").op, 5)
    assert job["state"] == "failed" and job["error"]["code"] == "internal"   # mutation


def test_deferred_one_shot_request_answers_after_completion(executor):
    handles = []
    executor.register("settings.validate", lambda args, ctx: handles.append(Pending()) or handles[-1])
    receipt = executor.submit("settings.validate", fixture("settings.validate", "command"),
                              req_id="r-v", owner="w1")
    assert wait_for(lambda: len(handles) == 1)
    assert receipt.wait(.1) is None
    handles[0].complete({"valid": "yes"})                      # adapted: invalid shape
    response = receipt.wait(5)
    assert response is not None and not response.ok and response.error["code"] == "internal"


# ---------------------------------------------------------------- domains

def test_desktop_delivered_is_control_domain_and_runs_inline():
    spec = COMMAND_SPECS["alerts.desktopDelivered"]
    # T-P2-31b: the contract tuple itself is control now (override table removed).
    assert spec.domain == "control" and spec.inline and spec.contract[0] == "control"
    assert not any(other.inline for name, other in COMMAND_SPECS.items()
                   if name != "alerts.desktopDelivered")
    assert COMMAND_SPECS["ui.presence"].domain == "sampling"
    ex = make_executor(normal_queue=1)
    try:
        threads = []
        ex.register("alerts.desktopDelivered", lambda args, ctx: threads.append(
            threading.current_thread()) or {"recorded": True, "duplicate": False})
        hold = threading.Event()
        for _ in range(3):   # both normal workers busy and the queue full
            ex.submit_normal(lambda: hold.wait(5))
        receipt = ex.submit("alerts.desktopDelivered",
                            fixture("alerts.desktopDelivered", "command"),
                            req_id="r-d", owner="shell-1")
        assert threads == [threading.current_thread()]     # ran during submit
        assert receipt.wait(1).data == {"recorded": True, "duplicate": False}
        hold.set()
    finally:
        ex.shutdown(time.monotonic() + 1)


def test_ui_presence_runs_on_the_sampling_worker_beside_full_normal_domain():
    ex = make_executor(normal_queue=1)
    try:
        names = []
        ex.register("ui.presence", lambda args, ctx: names.append(
            threading.current_thread().name) or {"recorded": True})
        hold = threading.Event()
        for _ in range(3):
            ex.submit_normal(lambda: hold.wait(5))
        receipt = ex.submit("ui.presence", fixture("ui.presence", "command"),
                            req_id="r-p", owner="shell-1")
        response = receipt.wait(2)
        assert response is not None and response.ok
        assert names == ["bridge-jobs-sampling"]
        hold.set()
    finally:
        ex.shutdown(time.monotonic() + 1)


# ---------------------------------------------------------------- business wiring

def _preview_owner(ex):
    from tests.test_bridge_notice_owner import Port, Publication
    pub = Publication()
    owner = DesktopAlertOwner(Port(), pub, submit_normal=ex.submit_normal,
                              shell_subscribed=lambda: True)
    return owner, pub


def test_alerts_test_waits_for_the_desktop_receipt_without_a_worker():
    ex = make_executor()
    try:
        owner, pub = _preview_owner(ex)
        tester = bridge_alerts.AlertTester(desktop=owner, deferred=True)
        ex.register("alerts.test", tester.execute)
        ex.register("alerts.desktopDelivered", owner.desktop_delivered_handler)
        ack = start(ex, "alerts.test", {})
        assert wait_for(lambda: pub.topic("alerts.desktop"))
        assert ex.get_job(ack.op)["state"] == "running"
        # Neither normal worker is held: two blocking ordinary works both start.
        started, release = [threading.Event(), threading.Event()], threading.Event()
        for event in started:
            assert ex.submit_normal(lambda event=event: (event.set(), release.wait(5)))
        assert all(event.wait(3) for event in started)
        delivery_id = pub.topic("alerts.desktop")[-1]["delivery_id"]
        receipt = ex.submit("alerts.desktopDelivered",
                            {"delivery_id": delivery_id, "outcome": "delivered",
                             "surface": "windows"}, req_id="r-d", owner="shell-1")
        assert receipt.accepted
        time.sleep(.1)
        assert ex.get_job(ack.op)["state"] == "running"   # write still queued
        release.set()
        response = receipt.wait(5)
        # A preview leaves no record (提醒 redesign 10-30): only update notices persist.
        assert response.ok and response.data == {"recorded": False, "duplicate": False}
        job = ex.wait(ack.op, 5)
        assert job["state"] == "done" and job["result"]["delivery_id"] == delivery_id
        # The preview slot is free again.
        owner.release_desktop_test("unknown")
        assert owner.force_desktop_test() != delivery_id
    finally:
        ex.shutdown(time.monotonic() + 1)


def test_alerts_test_receipt_timeout_frees_the_forced_test_slot():
    ex = make_executor()
    try:
        owner, _pub = _preview_owner(ex)
        tester = bridge_alerts.AlertTester(desktop=owner, deferred=True, receipt_timeout=.1)
        ex.register("alerts.test", tester.execute)
        job = ex.wait(start(ex, "alerts.test", {}).op, 5)
        assert job["state"] == "failed" and job["error"]["code"] == "timeout"
        assert job["result"]["error"]["reason_code"] == "alerts_desktop_receipt_timeout"
        assert wait_for(lambda: owner.force_desktop_test() is not None)   # no conflict
    finally:
        ex.shutdown(time.monotonic() + 1)


def test_exit_verify_deferral_holds_no_normal_worker():
    from tests.test_bridge_p2_16 import FakePort, lifecycle, payload
    port = FakePort(lifecycle(phase="collecting", core_running=False, readiness="activation_hold"))
    port.results.append(payload())
    ex = make_executor()
    session = SimpleNamespace(executor=ex, state=PublicationState(EPOCH),
                              register_owner_revoke_hook=lambda hook: None)
    owner = bx.register(session, port=port, owner_options=dict(
        attempt_seconds=2.0, poll_seconds=.01, lifecycle_poll_seconds=.02, grace_seconds=.05,
        defer_max_wait=5.0))
    try:
        ack = start(ex, "exit.verify")
        assert wait_for(lambda: owner.snapshot()["current"]["reason_code"] == "deferred")
        started, release = [threading.Event(), threading.Event()], threading.Event()
        for event in started:
            assert ex.submit_normal(lambda event=event: (event.set(), release.wait(5)))
        assert all(event.wait(3) for event in started)   # both workers free
        release.set()
        port.life = lifecycle(phase="running", generation=2)
        owner.wake()
        job = ex.wait(ack.op, 5)
        assert job["state"] == "done" and job["result"]["generation"] == 2
        assert port.calls[0]["trigger"] == "deferred"
    finally:
        ex.shutdown(time.monotonic() + 1)


def test_exit_verify_owner_cancel_stops_the_driver():
    from tests.test_bridge_p2_16 import FakePort, lifecycle
    port = FakePort(lifecycle(phase="collecting", core_running=False, readiness="activation_hold"))
    ex = make_executor()
    session = SimpleNamespace(executor=ex, state=PublicationState(EPOCH),
                              register_owner_revoke_hook=lambda hook: None)
    owner = bx.register(session, port=port, owner_options=dict(
        poll_seconds=.01, lifecycle_poll_seconds=.02, defer_max_wait=5.0))
    try:
        ack = start(ex, "exit.verify")
        assert wait_for(lambda: owner.progress() is not None)
        assert ex.cancel(ack.op, owner="window-1").accepted
        assert ex.wait(ack.op, 5)["state"] == "cancelled"
        assert wait_for(lambda: owner.progress() is None)   # the driver let go
        assert port.calls == []
    finally:
        ex.shutdown(time.monotonic() + 1)


def test_exit_verify_driver_ownership_requires_a_live_handle():
    """31a review: op ids restart per executor, so ``alive()`` alone (an op
    lookup) may name another job; an abandoned handle ends the driver."""
    from tests.test_bridge_p2_16 import FakePort, lifecycle
    port = FakePort(lifecycle(phase="collecting", core_running=False, readiness="activation_hold"))
    ex = make_executor()
    session = SimpleNamespace(executor=ex, state=PublicationState(EPOCH),
                              register_owner_revoke_hook=lambda hook: None)
    owner = bx.register(session, port=port, owner_options=dict(
        poll_seconds=.01, lifecycle_poll_seconds=.02, defer_max_wait=30.0))
    try:
        pending = owner.verify_deferred({"trigger": "manual"}, SimpleNamespace(op="job-1"),
                                        alive=lambda: True)   # a stale op lookup
        assert wait_for(lambda: owner.progress() is not None)
        assert pending._abandon("epoch")
        assert wait_for(lambda: owner.progress() is None)   # the driver let go
        assert port.calls == [] and pending.complete({}) is False
    finally:
        ex.shutdown(time.monotonic() + 1)


def test_automatic_lookup_is_refused_once_the_executor_stops_admitting():
    from tests.test_bridge_p2_16 import FakePort, lifecycle
    ex = make_executor()
    session = SimpleNamespace(executor=ex, state=PublicationState(EPOCH),
                              register_owner_revoke_hook=lambda hook: None)
    owner = bx.register(session, port=FakePort(lifecycle()))
    try:
        ran = threading.Event()
        assert owner._auto_submit(ran.set) is True
        assert ran.wait(3)
        ex.begin_shutdown()
        late = threading.Event()
        assert owner._auto_submit(late.set) is False
        time.sleep(.05)
        assert not late.is_set()
    finally:
        ex.shutdown(time.monotonic() + 1)


def test_cancellation_report_carries_the_cleanup_failed_receipt(executor):
    import bridge_diagnostics as bd
    from tests.test_bridge_p2_17 import DiagPort, NODE

    class CleanupFailPort(DiagPort):
        def diagnose_node(self, *args, **kwargs):
            raw = super().diagnose_node(*args, **kwargs)
            # The runtime's cancel ran, but its temporary Core cleanup failed.
            return ({"state": "cancelled", "reason": "cleanup_failed", "status_applied": False}
                    if self.cancelled else raw)
    port = CleanupFailPort()
    port.blocking = True
    owner = bd.register(SimpleNamespace(executor=executor), port=port)
    ack = start(executor, "nodes.diagnose", {"node_id": NODE, "probe_mode": "direct",
                                            "request_id": "diag-31a"})
    assert port.started.wait(5)
    cancel = executor.cancel(ack.op, owner="window-1")
    assert cancel.accepted          # the real runtime cancel releases the call
    job = executor.wait(ack.op, 5)
    assert job["state"] == "failed", job
    assert job["error"]["code"] == "runtime_unavailable"
    assert job["error"]["reason_code"] == "cleanup_failed"
    assert job["result"]["cleanup_pending"] is True
    assert cancel.cancel_receipt.cleanup_failed.is_set()
    assert owner.active_request() is None


def test_clean_cancellation_still_ends_cancelled(executor):
    import bridge_diagnostics as bd
    from tests.test_bridge_p2_17 import DiagPort, NODE
    port = DiagPort()
    port.blocking = True
    bd.register(SimpleNamespace(executor=executor), port=port)
    ack = start(executor, "nodes.diagnose", {"node_id": NODE, "probe_mode": "direct",
                                            "request_id": "diag-31b"})
    assert port.started.wait(5)
    cancel = executor.cancel(ack.op, owner="window-1")
    assert executor.wait(ack.op, 5)["state"] == "cancelled"
    assert not cancel.cancel_receipt.cleanup_failed.is_set()


# ---------------------------------------------------------------- secret registry

def test_concurrent_leases_merge_and_release_without_overwriting():
    installed = []
    registry = SecretRegistry(lambda: {"saved-secret-1": "password"},
                              sink=lambda table: installed.append(dict(table)))
    barrier = threading.Barrier(8)
    handles = {}

    def worker(index):
        barrier.wait()
        handles[index] = registry.lease(f"owner-{index % 3}", {
            f"lease-{index}-value": "token", "shared-value-xyz": "secret"})
    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    merged = registry.merged()
    assert {f"lease-{index}-value" for index in range(8)} <= set(merged)
    assert merged["saved-secret-1"] == "password" and "shared-value-xyz" in merged
    assert installed[-1] == merged and registry.lease_count() == 8
    assert registry.lease_count("owner-0") == 3
    releasers = [threading.Thread(target=registry.release, args=(handles[index],),
                                  kwargs={"refresh": "now"}) for index in range(0, 8, 2)]
    for thread in releasers:
        thread.start()
    for thread in releasers:
        thread.join(5)
    table = installed[-1]
    assert not {f"lease-{index}-value" for index in range(0, 8, 2)} & set(table)
    assert {f"lease-{index}-value" for index in range(1, 8, 2)} <= set(table)
    assert "shared-value-xyz" in table          # still held by the odd leases
    for index in range(1, 8, 2):
        registry.release(handles[index], refresh="now")
    assert "shared-value-xyz" not in installed[-1] and "saved-secret-1" in installed[-1]
    assert registry.release(handles[1]) is False and registry.lease("o", {}) is None


def test_release_defers_refresh_and_external_layer_survives_until_reset():
    installed, deferred = [], []
    registry = SecretRegistry(lambda: {}, sink=lambda table: installed.append(dict(table)),
                              defer=lambda work: deferred.append(work) or True)
    handle = registry.lease("onboarding", {"pairing-token-1": "link"})
    registry.push_external({"settings-draft-1": "password"})
    assert {"pairing-token-1", "settings-draft-1"} <= set(installed[-1])
    other = registry.lease("import", {"import-draft-1": "secret"})
    assert {"pairing-token-1", "settings-draft-1", "import-draft-1"} <= set(installed[-1])
    registry.release(handle)                        # control/hook thread: deferred
    assert "pairing-token-1" in installed[-1] and len(deferred) == 1
    deferred.pop()()
    assert "pairing-token-1" not in installed[-1] and "settings-draft-1" in installed[-1]
    registry.refresh(reset_external=True)
    assert "settings-draft-1" not in installed[-1] and "import-draft-1" in installed[-1]
    registry.release(other, refresh="none")
    assert "import-draft-1" in installed[-1]       # nothing pushed yet
    registry.refresh()
    assert installed[-1] == {}


def test_failing_redactor_withdraws_the_lease():
    def sink(table):
        raise OSError("sink failed")
    registry = SecretRegistry(lambda: {}, sink=sink)
    with pytest.raises(OSError):
        registry.lease("import", {"draft-value-77": "secret"})
    assert registry.lease_count() == 0
    assert SecretRegistry(lambda: {}).lease("x", {"no-sink-value": "secret"}) is not None


def test_released_values_disappear_from_the_real_log_redactor():
    from bridge_logs import LogCollector
    state = PublicationState("p2-31a-logs")
    state.subscribe(["logs.batch"])
    logs = LogCollector(state, logger=logging.Logger("p2-31a-logs"))
    registry = SecretRegistry(lambda: {})
    registry.attach(logs.refresh_known_secret_values)
    logs.refresh_known_secret_values = registry.push_external
    cursor = [0]

    def emit(text):
        cursor[0] += 1
        assert logs.ingest_source({"source": "TESSERACT", "level": "warning", "message_raw": text},
                                  source="runtime", generation=1, cursor=cursor[0])
        return json.dumps(logs.tail({"n": 100, "since_seq": 0})["records"][-1])

    first = registry.lease("onboarding", {"Pairing-Secret-4411": "secret"})
    second = registry.lease("import", {"Import-Secret-9922": "secret"})
    line = emit("pair=Pairing-Secret-4411 import=Import-Secret-9922")
    assert "Pairing-Secret-4411" not in line and "Import-Secret-9922" not in line
    # A legacy wholesale push lands in the external layer, leases stay.
    logs.refresh_known_secret_values({"Settings-Secret-5533": "password"})
    line = emit("pair=Pairing-Secret-4411 settings=Settings-Secret-5533")
    assert "Pairing-Secret-4411" not in line and "Settings-Secret-5533" not in line
    registry.release(first, refresh="now")
    line = emit("pair=Pairing-Secret-4411 import=Import-Secret-9922")
    assert "Pairing-Secret-4411" in line and "Import-Secret-9922" not in line
    registry.release(second, refresh="now")
    assert "Import-Secret-9922" in emit("import=Import-Secret-9922")


def test_modules_lease_into_the_session_registry():
    import bridge_import
    import bridge_sources
    installed = []
    registry = SecretRegistry(lambda: {"saved-1": "password"},
                              sink=lambda table: installed.append(dict(table)))
    refreshed = []
    session = SimpleNamespace(secrets=registry, logs=object(), access=None,
                              refresh_secrets=lambda: (refreshed.append(1),
                                                       registry.refresh(reload_base=True,
                                                                        reset_external=True)))
    assert session_registry(session) is registry
    imports = bridge_import.SessionSecretRegistry(session)
    imports.register_draft({"import-value-1": "secret"})
    register_sources, refresh_sources = bridge_sources._session_secret_hooks(session)
    register_sources({"source-value-1": "secret"})
    assert {"saved-1", "import-value-1", "source-value-1"} <= set(installed[-1])
    imports.restore()                       # releases only the import lease
    assert "import-value-1" not in installed[-1]
    assert "source-value-1" in installed[-1]
    refresh_sources()
    assert "source-value-1" not in installed[-1] and refreshed == [1, 1]


# ---------------------------------------------------------------- recovery gate

class Gate:
    def __init__(self):
        self.busy = True
        self.calls = 0

    def require_idle(self):
        from runtime_network_recovery import RecoveryStillRunning
        self.calls += 1
        if self.busy:
            raise RecoveryStillRunning("startup proxy recovery is still running")


def test_mutation_commands_are_refused_while_the_recovery_gate_is_busy():
    from bridge_main import BridgeSession
    gate = Gate()
    session = BridgeSession(None, SimpleNamespace(gate=SimpleNamespace(require_idle=lambda: None)),
                            None, EPOCH, runtime_factory=lambda *_: SimpleNamespace())
    try:
        session.report = SimpleNamespace(gate=gate)   # resolved at call time
        calls = []
        session.executor.register("mode.set", lambda args, ctx: calls.append("mode") or
                                  fixture("mode.set"))
        session.executor.register("app.update.check", lambda args, ctx: calls.append("check") or
                                  fixture("app.update.check"))
        job = session.executor.wait(start(session.executor, "mode.set").op, 5)
        assert job["state"] == "failed" and job["result"] is None
        assert job["error"] == {"code": "conflict", "message_key": "errors.bridge.recovery_pending",
                                "params": {}, "reason_code": "recovery_pending"}
        frame = dict(t="evt", epoch=EPOCH, produced_at_ms=1, sent_at_ms=1, topic="jobs", rev=1,
                     kind="edge", seq=1, data=dict(op="o", name="mode.set", state="failed",
                                                   started_at_ms=1, result=None, error=job["error"]))
        assert protocol.validate_frame(frame) == []
        assert calls == [] and gate.calls == 1
        # Ordinary commands are not gated.
        assert session.executor.wait(start(session.executor, "app.update.check",
                                           req_id="r-2").op, 5)["state"] == "done"
        gate.busy = False
        assert session.executor.wait(start(session.executor, "mode.set", req_id="r-3").op,
                                     5)["state"] == "done"
        assert calls == ["check", "mode"] and gate.calls == 2
    finally:
        session.executor.shutdown(time.monotonic() + 1)


@pytest.mark.parametrize("name", ["service.start", "service.restart"])
def test_service_start_and_restart_keep_the_recovery_pending_error(name):
    """31a review: the gate's own refusal is never relabelled by the start
    failure classifier (it was errors.start.unexpected for service.start)."""
    gate = Gate()
    ex = make_executor(mutation_gate=gate.require_idle)
    try:
        calls = []
        kwargs = ({"before": lambda args, ctx: calls.append("before") or 1}
                  if name == "service.restart" else {})
        ex.register(name, lambda args, ctx: calls.append("handler") or fixture(name), **kwargs)
        job = ex.wait(start(ex, name).op, 5)
        assert job["state"] == "failed" and job["result"] is None
        assert job["error"] == {"code": "conflict", "message_key": "errors.bridge.recovery_pending",
                                "params": {}, "reason_code": "recovery_pending"}
        assert calls == [] and gate.calls == 1
    finally:
        ex.shutdown(time.monotonic() + 1)


def test_internal_mutations_are_not_gated_by_the_executor():
    gate = Gate()
    ex = make_executor(mutation_gate=gate.require_idle)
    try:
        assert ex.run_internal_mutation(lambda: "ran") == (True, "ran")
        assert gate.calls == 0
        ex.register("mode.set", lambda args, ctx: fixture("mode.set"))
        replacement = ex.advance_epoch("epoch-31a-c", JobEventRecorder())
        try:
            ack = replacement.submit("mode.set", fixture("mode.set", "command"), req_id="r",
                                     owner="w")
            replacement.acceptance_sent(ack.op, epoch="epoch-31a-c")
            assert replacement.wait(ack.op, 5)["error"]["reason_code"] == "recovery_pending"
        finally:
            replacement.shutdown(time.monotonic() + 1)
    finally:
        ex.shutdown(time.monotonic() + 1)
