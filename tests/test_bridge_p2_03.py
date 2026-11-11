"""P2-03 wire dispatch with in-memory runtime, workers and pipe backpressure."""
from collections import deque
from copy import deepcopy
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from bridge_main import BridgeSession, StartupError
from bridge_jobs import JobEventRecorder, StopIntent
import bridge_protocol as protocol


FIXTURES = Path(__file__).resolve().parents[1] / "bridge_schema/fixtures/v1/valid"


def fixture(name, prefix="result"):
    frame = json.loads((FIXTURES / f"{prefix}_{name}.json").read_bytes())
    if prefix == "command":
        return frame["args"]
    return frame["data"].get("result", frame["data"])


class Pipe:
    def __init__(self):
        self.sent = []
        self.mode = "complete"
        self.poll = "complete"

    def send(self, frame):
        assert protocol.validate_frame(frame) == []
        if self.mode != "busy":
            self.sent.append(deepcopy(frame))
        return self.mode

    def poll_write(self):
        return self.poll


@pytest.fixture
def session():
    gate = SimpleNamespace(require_idle=lambda: None)
    s = BridgeSession(None, SimpleNamespace(gate=gate), None, "epoch-03",
        runtime_factory=lambda *_: SimpleNamespace())
    s.transport = Pipe()
    s.publisher = SimpleNamespace(pending_write=False, next_tick=time.monotonic() + 100,
                                  tick=lambda: None)
    s.shutdown = SimpleNamespace(quitting=False, _quitting=threading.Event(),
                                 _lock=threading.Lock(), _quit_ack=None)
    yield s
    s._ingress_stop.set()
    for worker in s._ingress_threads:
        worker.join(1)
    s.executor.shutdown(time.monotonic() + 1)


def request(s, name, ident=101, *, origin="page", owner="window-52", req_id="page-request-9"):
    args = fixture(name, "command") if name in protocol.V1_COMMANDS else {}
    return s.envelope("req", id=ident, cmd=name, args=args,
                      req_id=req_id, owner=owner, **{"from": origin})


def spin(s, predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s.pump()
        if predicate():
            return
        time.sleep(.005)
    assert predicate(), "worker/pipe did not reach expected state"


def register(s, name, handler=None):
    options = {}
    if name == "service.restart":
        options["before"] = lambda *_: 0
    if name in {"service.stop", "app.quit"}:
        options["stop_intent"] = lambda *_: StopIntent(True)
    if protocol.V1_COMMAND_SPECS[name][3] in {"session", "diagnostic"}:
        options["cancel"] = lambda *_: False
    s.register_command(name, handler or (lambda *_: fixture(name)), **options)


@pytest.mark.parametrize("name", ["geo.update", "ui.presence", "nodes.reveal"])
def test_reply_modes_and_distinct_identities(session, name):
    s = session
    register(s, name)
    frame = request(s, name, origin="shell" if name == "ui.presence" else "page")
    calls = []
    original = s.executor.submit
    def submit(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)
    s.executor.submit = submit
    s.dispatch(frame)
    spin(s, lambda: bool(s.transport.sent))
    reply = s.transport.sent[0]
    assert calls == [dict(req_id="page-request-9", owner="window-52")]
    assert protocol.validate_response(frame, reply) == []
    assert reply["id"] == 101 and reply["req_id"] == "page-request-9" and reply["owner"] == "window-52"
    assert "from" not in reply
    if name == "geo.update":
        assert reply["t"] == "ack"
        assert s.executor.wait(reply["op"], 1)["state"] == "done"
        events = s.recorder.prepare_for_send(123)
        assert [event["seq"] for event in events] == [1, 2, 3]
        for event in events:
            assert event["data"]["req_id"] == "page-request-9"
            assert event["data"]["owner"] == "window-52"
            assert "id" not in event["data"] and "from" not in event["data"]
            assert protocol.validate_frame(event) == []
    else:
        assert reply["t"] == "res" and reply["ok"]
        assert len(s.transport.sent) == 1 and s.recorder.pending == 0
        assert not s.state.pending(1)


@pytest.mark.parametrize("name", sorted(protocol.P1_COMMANDS))
def test_p1_missing_business_ids_are_not_filled(session, name):
    s = session
    if name in {"service.start", "service.restart"}:
        # Replace only the test instance's existing runtime callbacks.
        s.executor._specs.pop(name)
    register(s, name)
    frame = request(s, name, origin="shell")
    frame.pop("owner")
    frame.pop("req_id")
    s.dispatch(frame)
    spin(s, lambda: bool(s.transport.sent))
    ack = s.transport.sent[0]
    assert ack["accepted"] and "req_id" not in ack and "owner" not in ack
    assert s.executor.wait(ack["op"], 1)["state"] == "done"
    for event in s.recorder.prepare_for_send(123):
        assert "req_id" not in event["data"] and "owner" not in event["data"]


@pytest.mark.parametrize("stage", ["running", "complete", "outgoing"])
def test_revoke_discards_private_result_at_every_unsent_stage(session, stage):
    s = session
    entered, release = threading.Event(), threading.Event()
    register(s, "nodes.reveal", lambda *_: (entered.set(), release.wait(2),
        dict(item_id="example", fields=dict(password="PRIVATE-CANARY")))[-1])
    s.dispatch(request(s, "nodes.reveal"))
    assert entered.wait(1)
    receipt = s.pending_responses[0][1]
    if stage != "running":
        release.set()
        assert receipt.finished.wait(1)
    if stage == "outgoing":
        s.transport.mode = "busy"
        s.pump()
        assert s.outgoing
    s.dispatch(s.envelope("owner_revoke", id=777, owner="window-52"))
    release.set()
    s.transport.mode = "complete"
    spin(s, lambda: not s.pending_responses and not s.pending_acks and not s.outgoing)
    assert [frame["t"] for frame in s.transport.sent] == ["ack"]
    assert s.transport.sent[0]["id"] == 777
    assert s.recorder.pending == 0 and not s.state.pending(1)
    assert "PRIVATE-CANARY" not in json.dumps(s.transport.sent)
    s.dispatch(request(s, "nodes.reveal", ident=102))
    s.pump()
    assert s.transport.sent[-1]["error"]["code"] == "not_allowed"


@pytest.mark.parametrize("name,origin", [
    ("app.quit", "page"), ("shell.saveGrant", "page"),
    ("host.openFolder", "page"), ("host.openFolder", "shell"),
    ("host.unknown", "shell"), ("runtime.getattr", "page"),
    ("*", "shell"), ("service.*", "page"),
    ("geo.update", "page"), ("ui.presence", "page"), ("app.info", "page"),
])
def test_not_allowed_has_pipe_and_business_correlation(session, name, origin):
    s = session
    frame = request(s, name, origin=origin)
    s.dispatch(frame)
    s.pump()
    assert len(s.transport.sent) == 1
    reply = s.transport.sent[0]
    assert reply["t"] == "error" and reply["error"]["code"] == "not_allowed"
    assert (reply["id"], reply["req_id"], reply["owner"]) == (101, "page-request-9", "window-52")
    assert "from" not in reply and not s.recorder.pending


@pytest.mark.parametrize("name", ["geo.update", "mode.set", "service.start", "service.restart"])
@pytest.mark.parametrize("mode", ["busy", "pending"])
def test_acceptance_requires_completed_pipe_write(session, name, mode):
    s = session
    if name in {"service.start", "service.restart"}:
        s.executor._specs.pop(name)
    effect = threading.Event()
    register(s, name, lambda *_: (effect.set(), fixture(name))[-1])
    s.transport.mode = mode
    s.transport.poll = "pending"
    s.dispatch(request(s, name))
    s.pump()
    assert not effect.wait(.04)
    s.pump()
    assert not effect.is_set()
    s.transport.mode = s.transport.poll = "complete"
    s.pump()
    assert effect.wait(1)
    assert len(s.transport.sent) == 1


@pytest.mark.parametrize("intent_confirming", [False, True])
def test_epoch_and_lost_receipt_follow_p2_02_semantics(session, intent_confirming):
    s = session
    name = "service.stop" if intent_confirming else "geo.update"
    effect = threading.Event()
    register(s, name, lambda *_: (effect.set(), fixture(name))[-1])
    s.transport.mode = "busy"
    s.dispatch(request(s, name))
    ack = s.pending_acks[0][1]
    if intent_confirming:
        assert ack.wait_stop_intent(1).accepted
    s.pump()
    old = s.executor
    s.executor = old.advance_epoch("epoch-04", JobEventRecorder())
    s.epoch = "epoch-04"
    s.transport.mode = "complete"
    s.pump()
    assert old.wait(ack.op, 1)["state"] == ("done" if intent_confirming else "cancelled")
    assert effect.is_set() is intent_confirming
    assert s.transport.sent == []
    old.shutdown(time.monotonic() + 1)


def test_owner_revoke_hooks_are_ordered_and_fail_independently(session):
    s = session
    calls = []
    revoke = s.executor.revoke_owner
    def executor_revoke(owner):
        calls.append(("executor", owner))
        revoke(owner)
    s.executor.revoke_owner = executor_revoke
    s.register_owner_revoke_hook(lambda owner: calls.append(("first", owner)))
    def broken(owner):
        calls.append(("broken", owner))
        raise RuntimeError("test hook")
    s.register_owner_revoke_hook(broken)
    s.register_owner_revoke_hook(lambda owner: calls.append(("last", owner)))
    frame = s.envelope("owner_revoke", id=78, owner="window-52")
    s.dispatch(frame)
    s.pump()
    assert calls == [(label, "window-52") for label in ("executor", "first", "broken", "last")]
    assert protocol.validate_control_reply(frame, s.transport.sent[0]) == []
    assert s.transport.sent[0]["accepted"] and "op" not in s.transport.sent[0]
    with pytest.raises(ValueError):
        s.register_owner_revoke_hook(None)


@pytest.mark.parametrize("changes", [dict(id=0), dict(id=True), dict(owner=4),
    dict(owner=""), {"from": "page"}, {"from": "other"}])
def test_owner_revoke_invalid_frames_are_rejected(session, changes):
    s = session
    frame = s.envelope("owner_revoke", id=78, owner="window-52")
    frame.update(changes)
    assert protocol.validate_frame(frame)
    s.dispatch(frame)
    s.pump()
    assert not s.transport.sent and not s._revoked_owners


def test_ten_queries_and_two_jobs_preserve_ingress_order_and_capacity(session):
    s = session
    entered = [threading.Event(), threading.Event()]
    release = threading.Event()
    # Occupy both workers so the whole burst is in the queue at admission.
    for event in entered:
        assert s.executor.submit_normal(lambda event=event: (event.set(), release.wait(2)))
    assert all(event.wait(1) for event in entered)
    admitted = []
    completed = []
    def finish(ident, data):
        completed.append(ident)
        return data
    s.register_query("app.info", lambda _, ctx: finish(int(ctx.op.removeprefix("query-")),
        dict(version="test", language="en_US", caps=[], listeners=[])))
    register(s, "geo.update", lambda _, ctx: finish(ctx.op, fixture("geo.update")))
    original = s.dispatch
    def dispatch(frame):
        original(frame)
        admitted.append(frame["id"])
    s.dispatch = dispatch
    s._start_ingress()
    for index in range(10):
        s.enqueue_input(request(s, "app.info", ident=200 + index))
    for index in range(2):
        s.enqueue_input(request(s, "geo.update", ident=210 + index))
    spin(s, lambda: len(admitted) == 12)
    assert admitted == list(range(200, 212))
    # pump may already have sent the acks; check wire acceptance below.
    assert len(s.pending_responses) == 10
    s.pump()
    release.set()
    spin(s, lambda: len(completed) == 12 and not s.pending_responses)
    assert set(completed[:]) >= set(range(200, 210))
    assert sorted(frame["id"] for frame in s.transport.sent) == list(range(200, 212))
    assert all(frame.get("accepted", frame.get("ok")) for frame in s.transport.sent)
    assert s.executor._normal_workers == 2 and s.executor._normal_queue_size == 16
    assert s.executor.owner_limit == 16


def test_queries_execute_concurrently_and_reply_in_completion_order(session):
    s = session
    slow_entered, slow_release = threading.Event(), threading.Event()
    completed = []
    def query(_, context):
        ident = int(context.op.removeprefix("query-"))
        if ident == 200:
            slow_entered.set()
            assert slow_release.wait(2)
        completed.append(ident)
        return dict(version="test", language="en_US", caps=[], listeners=[])
    s.register_query("app.info", query)
    s.dispatch(request(s, "app.info", ident=200))
    assert slow_entered.wait(1)
    s.dispatch(request(s, "app.info", ident=201))
    spin(s, lambda: bool(s.transport.sent))
    assert completed == [201]
    assert [frame["id"] for frame in s.transport.sent] == [201]
    slow_release.set()
    spin(s, lambda: not s.pending_responses)
    assert completed == [201, 200]
    assert [frame["id"] for frame in s.transport.sent] == [201, 200]


def test_reader_handoff_keeps_ping_and_revoke_independent(session):
    s = session
    blocked, release = threading.Event(), threading.Event()
    original = s.dispatch
    def dispatch(frame):
        if frame.get("cmd") == "app.info":
            blocked.set()
            release.wait(2)
        original(frame)
    s.dispatch = dispatch
    s._start_ingress()
    s.enqueue_input(request(s, "app.info"))
    assert blocked.wait(1)
    revoked = threading.Event()
    s.register_owner_revoke_hook(lambda _: revoked.set())
    s.enqueue_input(s.envelope("owner_revoke", id=80, owner="window-52"))
    s.enqueue_input(s.envelope("ping", n=42))
    assert revoked.wait(1)
    s.pump()
    assert any(frame.get("n") == 42 for frame in s.transport.sent)
    assert any(frame.get("id") == 80 for frame in s.transport.sent)
    release.set()


def test_registration_is_closed_to_unknown_names(session):
    for name in ("*", "runtime.getattr", "host.openFolder"):
        with pytest.raises(ValueError):
            session.register_command(name, lambda *_: {})
    with pytest.raises(ValueError):
        session.register_query("*", lambda *_: {})
    session.register_query("app.info", lambda *_: {})
    with pytest.raises(ValueError):
        session.register_query("app.info", lambda *_: {})


@pytest.mark.parametrize("loss", ["quit", "shutdown", "epoch", "revoke"])
@pytest.mark.parametrize("stage", ["queued", "running"])
def test_query_waiters_finish_even_when_normal_queue_is_discarded(session, loss, stage):
    s = session
    release = threading.Event()
    occupied = [threading.Event(), threading.Event()]
    if stage == "queued":
        for event in occupied:
            assert s.executor.submit_normal(lambda event=event: (event.set(), release.wait(2)))
        assert all(event.wait(1) for event in occupied)
    entered = threading.Event()
    s.register_query("app.info", lambda *_: (entered.set(), release.wait(2),
        dict(version="test", language="en_US", caps=[], listeners=[]))[-1])
    s.dispatch(request(s, "app.info"))
    receipt = s.pending_responses[0][1]
    if stage == "running":
        assert entered.wait(1)
    old = s.executor
    if loss == "quit":
        old._quitting = True
        old.prepare_quit()
    elif loss == "shutdown":
        old.begin_shutdown()
    elif loss == "epoch":
        s.executor = old.advance_epoch("epoch-04", JobEventRecorder())
        s.epoch = "epoch-04"
    else:
        s.dispatch(s.envelope("owner_revoke", id=80, owner="window-52"))
    s.publisher.metrics = lambda: dict(control_depth=0)
    assert s.drain_publications()
    assert receipt.finished.is_set() and not s.pending_responses
    if loss in {"quit", "shutdown"}:
        assert s.transport.sent[-1]["t"] == "res"
        assert s.transport.sent[-1]["error"]["code"] == "runtime_gone"
    else:
        assert not any(frame["t"] == "res" for frame in s.transport.sent)
    release.set()
    old.shutdown(time.monotonic() + 1)


@pytest.mark.parametrize("mode", ["busy", "pending"])
def test_absolute_job_ack_timeout_cannot_release_side_effect(session, mode):
    s = session
    now = [0.0]
    s.executor._monotonic = lambda: now[0]
    effect = threading.Event()
    register(s, "geo.update", lambda *_: (effect.set(), fixture("geo.update"))[-1])
    s.transport.mode, s.transport.poll = mode, "pending"
    s.dispatch(request(s, "geo.update"))
    ack = s.pending_acks[0][1]
    s.pump()
    now[0] = 10.0
    assert s.executor.wait(ack.op, 1)["state"] == "cancelled"
    s.transport.mode = s.transport.poll = "complete"
    s.pump()
    assert not effect.is_set()


def test_unknown_command_error_backlog_is_bounded(session):
    s = session
    s.transport.mode = "busy"
    for index in range(64):
        s.dispatch(request(s, "*", ident=300 + index))
    with pytest.raises(StartupError, match="control_capacity"):
        s.dispatch(request(s, "*", ident=400))
    assert len(s.outgoing) == 64


def test_owner_quota_combines_queries_jobs_and_private_waiters(session):
    s = session
    release = threading.Event()
    occupied = [threading.Event(), threading.Event()]
    for event in occupied:
        assert s.executor.submit_normal(lambda event=event: (event.set(), release.wait(2)))
    assert all(event.wait(1) for event in occupied)
    s.register_query("app.info", lambda *_: dict(version="test", language="en_US", caps=[], listeners=[]))
    register(s, "mode.set")
    register(s, "nodes.reveal")
    for index in range(10):
        s.dispatch(request(s, "app.info", ident=200 + index))
    for index in range(5):
        s.dispatch(request(s, "mode.set", ident=210 + index))
    s.dispatch(request(s, "nodes.reveal", ident=215))
    s.dispatch(request(s, "app.info", ident=216))
    s.dispatch(request(s, "mode.set", ident=217))
    s.dispatch(request(s, "nodes.reveal", ident=218))
    assert len(s.pending_responses) == 11 and len(s.pending_acks) == 5
    assert [frame["error"]["code"] for frame, _ in s.outgoing] == ["job_capacity"] * 3
    release.set()


@pytest.mark.parametrize("name", ["service.start", "service.restart"])
@pytest.mark.parametrize("stage", ["queued", "before_submit", "after_submit"])
def test_stop_supersedes_old_ingress_start_but_allows_later_start(session, name, stage):
    s = session
    blocked, release = threading.Event(), threading.Event()
    effects = []
    s.executor._specs.pop(name)
    register(s, name, lambda *_: (effects.append(name), fixture(name))[-1])
    register(s, "service.stop")
    original_dispatch, original_submit = s.dispatch, s.executor.submit
    if stage == "queued":
        def dispatch(frame):
            if frame.get("cmd") == "app.info":
                blocked.set()
                assert release.wait(2)
            original_dispatch(frame)
        s.dispatch = dispatch
    else:
        def submit(command, *args, **kwargs):
            if command != name:
                return original_submit(command, *args, **kwargs)
            ack = original_submit(command, *args, **kwargs) if stage == "after_submit" else None
            blocked.set()
            assert release.wait(2)
            return ack or original_submit(command, *args, **kwargs)
        s.executor.submit = submit
    s._start_ingress()
    try:
        if stage == "queued":
            s.enqueue_input(request(s, "app.info", ident=90))
            assert blocked.wait(1)
        s.enqueue_input(request(s, name, ident=101))
        assert blocked.wait(1)
        s.enqueue_input(request(s, "service.stop", ident=102))
        spin(s, lambda: any(frame.get("id") == 102 for frame in s.transport.sent))
        stop_ack = next(frame for frame in s.transport.sent if frame.get("id") == 102)
        assert stop_ack["accepted"]
        assert s.executor.wait(stop_ack["op"], 1)["state"] == "done"
        assert not effects
        release.set()
        spin(s, lambda: any(frame.get("id") == 101 for frame in s.transport.sent))
        old_reply = next(frame for frame in s.transport.sent if frame.get("id") == 101)
        if old_reply["t"] == "ack":
            assert s.executor.wait(old_reply["op"], 1)["state"] == "cancelled"
        else:
            assert old_reply["error"]["code"] == "superseded"
        assert not effects
        s.executor.submit = original_submit
        s.enqueue_input(request(s, name, ident=103))
        spin(s, lambda: any(frame.get("id") == 103 for frame in s.transport.sent))
        new_ack = next(frame for frame in s.transport.sent if frame.get("id") == 103)
        assert new_ack["accepted"]
        assert s.executor.wait(new_ack["op"], 1)["state"] == "done"
        assert effects == [name]
    finally:
        release.set()


@pytest.mark.parametrize("name", ["service.start", "service.restart"])
@pytest.mark.parametrize("mode", ["busy", "pending"])
def test_stop_watermark_covers_start_waiting_for_ack_write(session, name, mode):
    s = session
    effect = threading.Event()
    s.executor._specs.pop(name)
    register(s, name, lambda *_: (effect.set(), fixture(name))[-1])
    register(s, "service.stop")
    s._start_ingress()
    s.transport.mode, s.transport.poll = mode, "pending"
    s.enqueue_input(request(s, name, ident=101))
    spin(s, lambda: bool(s.outgoing) or s.publisher.pending_write)
    old_op = next(job.op for job in s.executor._jobs.values() if job.name == name)
    s.enqueue_input(request(s, "service.stop", ident=102))
    spin(s, lambda: any(job.name == "service.stop" and job.state == "done"
                        for job in list(s.executor._jobs.values())))
    s.transport.mode = s.transport.poll = "complete"
    spin(s, lambda: not s.outgoing and not s.pending_acks and not s.publisher.pending_write)
    assert s.executor.wait(old_op, 1)["state"] == "cancelled"
    assert not effect.is_set()


@pytest.mark.parametrize("change", [dict(epoch="old-epoch"), {"from": "other"}, dict(id=0)])
def test_invalid_stop_does_not_supersede_valid_start(session, change):
    s = session
    s.executor._specs.pop("service.start")
    register(s, "service.start")
    register(s, "service.stop")
    s.enqueue_input(request(s, "service.start", ident=101))
    stop = request(s, "service.stop", ident=102)
    stop.update(change)
    s.enqueue_input(stop)
    s._start_ingress()
    # T-P2-31b: an invalid request with a usable id now gets an error reply
    # (never executed); the start's ack is the frame carrying an op.
    spin(s, lambda: any(frame["t"] == "ack" for frame in s.transport.sent))
    ack = next(frame for frame in s.transport.sent if frame["t"] == "ack")
    assert s.executor.wait(ack["op"], 1)["state"] == "done"
    assert all(frame["t"] != "ack" or frame["id"] == 101 for frame in s.transport.sent)


@pytest.mark.parametrize("name", ["service.start", "service.restart"])
def test_stop_received_during_ack_release_is_checked_before_execution(session, name):
    s = session
    effect = threading.Event()
    s.executor._specs.pop(name)
    register(s, name, lambda *_: (effect.set(), fixture(name))[-1])
    register(s, "service.stop")
    original = s.executor.acceptance_sent
    def acceptance_sent(op, **kwargs):
        # Receive stop in the last gap between checking the ack and releasing
        # the executor. The control handoff has deliberately not run yet.
        # T-P2-31a: the check is the executor's preempt callback now, so the
        # stop lands right after it returned "not stale".
        preempt = kwargs["preempt"]
        def late(marker):
            stale = preempt(marker)
            s.enqueue_input(request(s, "service.stop", ident=102))
            return stale
        return original(op, **dict(kwargs, preempt=late))
    s.executor.acceptance_sent = acceptance_sent
    s.dispatch(request(s, name, ident=101))
    s.pump()
    ack = s.transport.sent[0]
    assert ack["accepted"]
    assert s.executor.wait(ack["op"], 1)["state"] == "superseded"
    assert not effect.is_set()


def test_session_end_bypasses_blocked_normal_ingress_and_revoke_hook(session):
    s = session
    normal_blocked, hook_blocked, release, executed = [threading.Event() for _ in range(4)]
    original = s.dispatch
    def dispatch(frame):
        if frame.get("cmd") == "app.info":
            normal_blocked.set()
            assert release.wait(2)
        original(frame)
    s.dispatch = dispatch
    def hook(_):
        hook_blocked.set()
        assert release.wait(2)
    s.register_owner_revoke_hook(hook)
    register(s, "app.sessionEnd", lambda *_: (executed.set(), fixture("app.sessionEnd"))[-1])
    s._start_ingress()
    try:
        s.enqueue_input(request(s, "app.info"))
        s.enqueue_input(s.envelope("owner_revoke", id=80, owner="other-owner"))
        assert normal_blocked.wait(1) and hook_blocked.wait(1)
        # Fill both other ingress lanes while their handlers are blocked.
        for ident in range(200, 264):
            s.enqueue_input(request(s, "app.info", ident=ident))
        for ident in range(300, 316):
            s.enqueue_input(s.envelope("owner_revoke", id=ident, owner="other-owner"))
        s.enqueue_input(request(s, "app.sessionEnd", ident=400, origin="shell"))
        assert executed.wait(1)
        spin(s, lambda: any(frame.get("id") == 400 for frame in s.transport.sent))
        reply = next(frame for frame in s.transport.sent if frame.get("id") == 400)
        assert reply["t"] == "res" and reply["ok"]
        assert not s.recorder.pending
    finally:
        # Discard intentionally saturated test queues before fixture teardown.
        s._ingress_stop.set()
        release.set()


@pytest.mark.parametrize("name", [name for name, spec in protocol.V1_COMMAND_SPECS.items()
    if spec[0] == "control" and name != "app.sessionEnd"])
def test_all_spec_control_commands_use_control_ingress(session, name):
    s = session
    s.enqueue_input(request(s, name, origin="shell"))
    # T-P2-31b: the reviewed inline command has its own small lane (index 3).
    expected = [0, 0, 0, 1] if name == "alerts.desktopDelivered" else [1, 0, 0, 0]
    assert [source.qsize() for source in s._ingress_queues] == expected


def test_session_end_ingress_capacity_is_independent_and_bounded(session):
    s = session
    s.enqueue_input(request(s, "app.sessionEnd", ident=400, origin="shell"))
    # Overflow rejects the duplicate on the wire instead of faulting the session.
    s.enqueue_input(request(s, "app.sessionEnd", ident=401, origin="shell"))
    assert [source.qsize() for source in s._ingress_queues] == [0, 0, 1, 0]
    rejected = [frame for frame, _ in s.outgoing if frame.get("id") == 401]
    assert len(rejected) == 1 and rejected[0]["t"] == "error"
    assert rejected[0]["error"]["code"] == "conflict"
    assert rejected[0]["error"]["message_key"] == "errors.bridge.session_end_in_progress"
    s.enqueue_input(request(s, "app.info"))
    s.enqueue_input(s.envelope("owner_revoke", id=80, owner="other-owner"))
    assert [source.qsize() for source in s._ingress_queues] == [1, 1, 1, 0]


def test_session_end_overflow_keeps_emergency_work_and_session_alive(session):
    # Regression for T-P2-03-R MUST-1: duplicate session-end requests arriving
    # while the first is still queued must not fault the session (which would
    # submit the ordinary quit and cancel the queued emergency request).
    s = session
    calls = []
    executed = threading.Event()
    def execute(*_):
        calls.append(1)
        executed.set()
        return fixture("app.sessionEnd")
    register(s, "app.sessionEnd", execute)
    s.enqueue_input(request(s, "app.sessionEnd", ident=400, origin="shell"))
    for ident in (401, 402):
        s.enqueue_input(request(s, "app.sessionEnd", ident=ident, origin="shell"))
    assert not s.executor._closed and not s.executor._quitting
    s._start_ingress()
    try:
        assert executed.wait(2)
        spin(s, lambda: any(frame.get("id") == 400 for frame in s.transport.sent))
        assert calls == [1]
        replies = {frame["id"]: frame for frame in s.transport.sent if frame.get("id") in {400, 401, 402}}
        assert replies[400]["t"] == "res" and replies[400]["ok"]
        assert {replies[i]["error"]["code"] for i in (401, 402)} == {"conflict"}
        assert not s.executor._closed
    finally:
        s._ingress_stop.set()


def test_session_end_executor_saturation_is_reported_on_wire(session):
    s = session
    entered, release = threading.Event(), threading.Event()
    def execute(*_):
        entered.set()
        assert release.wait(2)
        return fixture("app.sessionEnd")
    register(s, "app.sessionEnd", execute)
    s._start_ingress()
    try:
        s.enqueue_input(request(s, "app.sessionEnd", ident=400, origin="shell"))
        assert entered.wait(1)
        s.enqueue_input(request(s, "app.sessionEnd", ident=401, origin="shell"))
        spin(s, lambda: len(s.pending_responses) == 2)
        s.enqueue_input(request(s, "app.sessionEnd", ident=402, origin="shell"))
        spin(s, lambda: any(frame.get("id") == 402 for frame in s.transport.sent))
        reply = next(frame for frame in s.transport.sent if frame.get("id") == 402)
        assert reply["t"] == "res" and not reply["ok"]
        assert reply["error"]["code"] == "job_capacity"
        release.set()
        spin(s, lambda: not s.pending_responses)
        assert [frame["id"] for frame in s.transport.sent if frame.get("ok")] == [400, 401]
    finally:
        release.set()
