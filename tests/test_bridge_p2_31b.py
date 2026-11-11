"""T-P2-31b: full module wiring, v1 capabilities, contract additions, full exit.

In-process tests use doubles only; the end-to-end test runs the real source
sidecar (runtime_main.pyw) over a private pipe in a temporary custom scope,
like tests/test_bridge_integration.py: no live profile, no host network
takeover, no HKCU proxy or firewall writes, no network access.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest

import bridge_alerts
import bridge_main
import bridge_protocol as protocol
import bridge_wiring
from bridge_commands import COMMAND_SPECS, SELF_GATED_COMMANDS
from bridge_jobs import JobEventRecorder, JobExecutor, JobFailure, Pending
from bridge_state import PublicationState


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "bridge_schema/fixtures/v1/valid"
EPOCH = "epoch-31b"


def fixture(name, prefix="result"):
    frame = json.loads((FIXTURES / f"{prefix}_{name}.json").read_bytes())
    if prefix == "command":
        return frame["args"]
    return frame["data"].get("result", frame["data"])


def make_executor(**kwargs):
    return JobExecutor(JobEventRecorder(4096), epoch=EPOCH, require_acceptance=True, **kwargs)


def submit(executor, name, args, owner="window-1", req_id="r-1"):
    ack = executor.submit(name, args, req_id=req_id, owner=owner)
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


# ------------------------------------------------------------------ contract

def test_hello_caps_accept_the_v1_set_and_refuse_host_commands():
    hello = json.loads((FIXTURES / "hello_v1_caps.json").read_bytes())
    assert protocol.validate_frame(hello) == []
    assert set(hello["caps"]["commands"]) == protocol.HELLO_CAP_COMMANDS
    assert not any(name.startswith("host.") for name in protocol.HELLO_CAP_COMMANDS)
    assert protocol.V1_QUERIES <= protocol.HELLO_CAP_COMMANDS
    bad = deepcopy(hello)
    bad["caps"]["topics"] = ["service.state", "service.state"]
    assert "invariant:caps.topics" in protocol.validate_frame(bad)


def test_desktop_delivered_is_control_in_the_contract_and_answers_timeout():
    spec = protocol.V1_COMMAND_SPECS["alerts.desktopDelivered"]
    assert spec[0] == "control" and "timeout" in spec[6].split()
    assert COMMAND_SPECS["alerts.desktopDelivered"].inline
    assert SELF_GATED_COMMANDS == {"settings.apply", "subscriptions.refresh"}
    assert all(COMMAND_SPECS[name].self_gated for name in SELF_GATED_COMMANDS)
    assert not any(spec.self_gated for name, spec in COMMAND_SPECS.items()
                   if name not in SELF_GATED_COMMANDS)


def test_new_optional_fields_validate():
    # alerts.test target_id / secrets were retired with the 提醒 redesign (10-30).
    for name in ("variant_shell.saveGrant_for_owner", "variant_alerts.desktop_force"):
        assert protocol.validate_frame(json.loads((FIXTURES / f"{name}.json").read_bytes())) == []


def test_malformed_request_with_a_usable_id_gets_a_structured_error():
    session = bridge_main.BridgeSession(
        None, SimpleNamespace(gate=SimpleNamespace(require_idle=lambda: None)), None, EPOCH,
        runtime_factory=lambda *_: SimpleNamespace())
    try:
        frame = session.envelope("req", id=7, cmd="mode.set", args={"mode": "warp"},
                                 req_id="bad", owner="window-1", **{"from": "page"})
        assert protocol.validate_frame(frame)
        session.dispatch(frame)
        replies = [reply for reply, _ in session.outgoing if reply.get("id") == 7]
        assert len(replies) == 1 and replies[0]["t"] == "error"
        assert replies[0]["error"]["code"] == "invalid_type"
        assert replies[0]["req_id"] == "bad" and protocol.validate_frame(replies[0]) == []
    finally:
        session.executor.shutdown(time.monotonic() + 1)
    assert bridge_main.request_error_code(["missing:args.mode"]) == "missing_field"
    assert bridge_main.request_error_code(["enum:args.mode"]) == "invalid_enum"
    assert bridge_main.request_error_code(["type:args.mode"]) == "invalid_type"
    assert bridge_main.request_error_code(["not_allowed:cmd"]) == "not_allowed"
    assert bridge_main.request_error_code(["invariant:args.target_id"]) == "validation"


# ------------------------------------------------------- state barrier fix

def test_never_recorded_topic_invalidated_is_unavailable_so_barriers_finish():
    state = PublicationState(EPOCH)
    state.subscribe(["gateway.current"])
    assert not state.baseline_unavailable("gateway.current")
    state.invalidate_state("gateway.current", notify=True)
    assert state.baseline_unavailable("gateway.current")
    gap = [event for event in state.pending(16) if event["topic"] == "gateway.current"]
    assert gap and gap[0]["kind"] == "gap" and gap[0]["data"]["dropped"] == 1
    state.invalidate_state("gateway.current", notify=True)   # repeated: no second gap
    assert len([event for event in state.pending(16) if event["kind"] == "gap"]) == 1


# ------------------------------------------------------------ revision store

def test_config_revision_is_persisted_across_instances(tmp_path):
    path = tmp_path / bridge_main.CONFIG_REVISION_FILE
    store = bridge_main.ConfigRevisionStore(path)
    assert store.current() == 0
    assert store.commit() == 1 and store.commit() == 2
    assert json.loads(path.read_text(encoding="utf-8")) == {"rev": 2}
    again = bridge_main.ConfigRevisionStore(path)
    assert again.current() == 2 and again.commit() == 3
    path.write_text("not json", encoding="utf-8")
    assert bridge_main.ConfigRevisionStore(path).current() == 0


class _WriteSession:
    def __init__(self):
        self.state = PublicationState(EPOCH)
        self.state.subscribe(["config.changed", "config.items"])
        self.revision = 0

        def commit():
            self.revision += 1
            return self.revision
        self.runtime = SimpleNamespace(bridge_commit_config_revision=commit,
                                       bridge_config_revision=lambda: self.revision,
                                       configured_items=lambda: [])
        self.executor = SimpleNamespace(submit_normal=lambda work: (work(), True)[1])
        self.subscribed_topics = set()


@pytest.mark.parametrize("name,result,writes", [
    ("mode.set", {"mode": "tun", "changed": True, "saved": True, "restarted": True,
                  "applied": True, "apply_error": None}, True),
    ("mode.set", {"mode": "tun", "changed": False, "saved": True, "restarted": False,
                  "applied": True, "apply_error": None}, False),
    ("nodes.update", {"saved": True, "applied": False, "pending": None,
                      "apply_error": {"code": "io", "message_key": "errors.io", "params": {}}}, True),
    ("nodes.delete", {"saved": False, "applied": False, "apply_error": None, "pending": None}, False),
    ("subscriptions.refresh", {"total": 1, "healthy": 1, "stored": True, "applied": True,
                               "retained_previous": False, "apply_error": None, "error": None}, True),
    ("gateway.select", {"selected": "x", "applied": True, "apply_error": None,
                        "selection": "x", "pending": None}, True),
    ("import.apply", {"saved": True, "applied": True, "items": [], "subscriptions": [],
                      "apply_error": None}, True),
    ("mode.set", None, True),     # unknown outcome: conservatively a write
])
def test_controlled_writes_commit_the_revision_and_record_config_changed(name, result, writes):
    session = _WriteSession()
    wiring = bridge_wiring.Wiring(session)
    calls = []

    def handler(args, context):
        calls.append(name)
        if result is None:
            raise RuntimeError("boom")
        return deepcopy(result)
    wrapped = wiring._write_wrap(name, handler)
    if result is None:
        with pytest.raises(RuntimeError):
            wrapped({}, None)
    else:
        assert wrapped({}, None) == result
    events = [event for event in session.state.pending(32) if event["topic"] == "config.changed"]
    assert session.revision == (1 if writes else 0)
    assert len(events) == (1 if writes else 0)
    if writes:
        data = events[0]["data"]
        assert data["rev"] == 1 and data["external_detection"] == "bridge_writes_only"
        assert protocol.validate_topic_payload("config.changed", data) == []
        if result is None:
            # Unknown outcome: the revision advances, but nothing claims saved=true.
            assert data["sections"] == []
        else:
            assert data["sections"][0]["saved"] is True
    assert wiring._write_wrap("nodes.reveal", handler) is handler


def test_failed_write_with_partial_result_still_reports_the_saved_fact():
    session = _WriteSession()
    wiring = bridge_wiring.Wiring(session)

    def handler(args, context):
        raise JobFailure("io", {"saved": True, "applied": False, "pending": None,
                                "apply_error": {"code": "io", "message_key": "errors.io",
                                                "params": {}}})
    with pytest.raises(JobFailure):
        wiring._write_wrap("nodes.update", handler)({}, None)
    data = [e for e in session.state.pending(8) if e["topic"] == "config.changed"][0]["data"]
    assert data["sections"][0] == {"section": "nodes", "saved": True, "applied": False,
                                   "effect": "none", "error": {"code": "io",
                                   "message_key": "errors.io", "params": {}}}


# -------------------------------------------------------- recovery gate (b)

class Gate:
    def __init__(self):
        self.busy = True
        self.calls = 0

    def require_idle(self):
        from runtime_network_recovery import RecoveryStillRunning
        self.calls += 1
        if self.busy:
            raise RecoveryStillRunning("startup proxy recovery is still running")


def test_bridge_session_gate_lets_self_gated_commands_answer_with_partial_results():
    """决定_第三波裁定 plan b, real BridgeSession path: settings.apply and
    subscriptions.refresh give their own richer answers; other mutations get
    the executor's uniform conflict/recovery_pending."""
    import bridge_node_commands
    import bridge_settings
    from tests.test_bridge_p2_14 import FakePort, sid
    session = bridge_main.BridgeSession(
        None, SimpleNamespace(gate=SimpleNamespace(require_idle=lambda: None)), None, EPOCH,
        runtime_factory=lambda *_: SimpleNamespace())
    try:
        gate = Gate()
        session.report = SimpleNamespace(gate=gate)
        port = FakePort()
        bridge_node_commands.register(session, port=port)
        settings_port = SimpleNamespace(config_revision=lambda: 4)
        bridge_settings.register(session, port=settings_port, sources=SimpleNamespace())
        apply = session.executor.wait(submit(session.executor, "settings.apply", {
            "base_rev": 4, "changes": [{"section": "ui", "values": {}}]}).op, 5)
        assert apply["state"] == "failed"
        assert apply["error"]["code"] == "lifecycle_busy" and apply["error"]["hold"] is True
        assert apply["error"]["reason_code"] == "recovery_in_progress"
        assert apply["result"]["rev"] == 4 and apply["result"]["sections"][0]["section"] == "ui"
        refresh = session.executor.wait(submit(session.executor, "subscriptions.refresh",
                                               {"item_id": sid(port)}, req_id="r-2").op, 5)
        assert refresh["state"] == "failed" and refresh["result"]["stored"] is False
        assert refresh["result"]["error"]["reason_code"] == "recovery_pending"
        mode = session.executor.wait(submit(session.executor, "mode.set", {"mode": "tun"},
                                            req_id="r-3").op, 5)
        assert mode["state"] == "failed" and mode["result"] is None
        assert mode["error"] == {"code": "conflict", "message_key": "errors.bridge.recovery_pending",
                                 "params": {}, "reason_code": "recovery_pending"}
        assert not port.called("set_network_mode") and not port.called("refresh_subscription")
    finally:
        session.executor.shutdown(time.monotonic() + 1)


# ----------------------------------------------------------- inline lane

def test_inline_lane_overflow_answers_job_capacity_without_a_session_fault():
    session = bridge_main.BridgeSession(
        None, SimpleNamespace(gate=SimpleNamespace(require_idle=lambda: None)), None, EPOCH,
        runtime_factory=lambda *_: SimpleNamespace())
    try:
        session.shutdown = SimpleNamespace(session_end=None)
        frame = lambda ident: session.envelope("req", id=ident, cmd="alerts.desktopDelivered",
            args={"delivery_id": f"d{ident}", "outcome": "delivered", "surface": "windows"},
            req_id=f"shell-{ident}", owner="shell", **{"from": "shell"})
        for ident in range(1, bridge_main.INLINE_QUEUE_CAPACITY + 1):
            session.enqueue_input(frame(ident))
        assert [queue.qsize() for queue in session._ingress_queues] == [
            0, 0, 0, bridge_main.INLINE_QUEUE_CAPACITY]
        session.enqueue_input(frame(999))     # no StartupError("control_capacity")
        replies = [reply for reply, _ in session.outgoing if reply.get("id") == 999]
        assert len(replies) == 1 and replies[0]["t"] == "res" and replies[0]["ok"] is False
        assert replies[0]["error"]["code"] == "job_capacity"
        assert protocol.validate_frame(replies[0]) == []
        # The control lane is untouched by the inline backlog.
        session.enqueue_input(session.envelope("owner_revoke", id=5, owner="other"))
        assert session._ingress_queues[0].qsize() == 1
    finally:
        session.executor.shutdown(time.monotonic() + 1)


# ------------------------------------------------- desktop receipt answers

def test_receipt_write_timeout_answers_receipt_persist_pending():
    from bridge_alerts_desktop import _raise_persist_pending
    with pytest.raises(JobFailure) as error:
        _raise_persist_pending()
    assert error.value.code == "timeout"
    assert error.value.error["reason_code"] == "receipt_persist_pending"
    frame = dict(t="res", epoch=EPOCH, produced_at_ms=1, sent_at_ms=1, id=1, req_id="r",
                 owner="shell", cmd="alerts.desktopDelivered", ok=False, error=error.value.error)
    assert protocol.validate_frame(frame) == []


# --------------------------------------------- node diagnostic cancellation

@pytest.mark.parametrize("revoke,state", [(False, "failed"), (True, "cancelled")])
def test_cancel_diagnose_with_failed_cleanup_ends_failed_unless_revoked(revoke, state):
    from bridge_diagnostics import DiagnosticsOwner
    release = threading.Event()

    class Port:
        def resolve_node(self, node_id):
            return SimpleNamespace(node_identity=("n",), auto_pool=False, gateway=False)

        def diagnose_node(self, identity, *, probe_mode, request_id):
            release.wait(5)
            return {"state": "failed", "reason": "cleanup_failed", "status_applied": False}

        def cancel_node_diagnostic(self, request_id):
            release.set()
            return True

    executor = make_executor()
    owner = DiagnosticsOwner(Port(), owner_of=lambda context: "window-1")
    executor.register("nodes.diagnose", owner.diagnose, cancel=owner.cancel_job)
    executor.register("nodes.cancelDiagnose", owner.cancel_request)
    try:
        ack = submit(executor, "nodes.diagnose", {"node_id": "n" * 24, "probe_mode": "direct",
                                                  "request_id": "req-1"})
        assert wait_for(lambda: executor.get_job(ack.op)["state"] == "running")
        assert wait_for(lambda: owner._flight is not None and owner._flight.state == "running")
        if revoke:
            owner.revoke_owner("window-1")
        else:
            receipt = submit(executor, "nodes.cancelDiagnose", {"request_id": "req-1"},
                             req_id="r-2")
            assert wait_for(lambda: receipt.finished.is_set())
        job = executor.wait(ack.op, 5)
        assert job["state"] == state
        if state == "failed":
            assert job["error"]["code"] == "runtime_unavailable"
            assert job["error"]["reason_code"] == "cleanup_failed"
            assert job["result"]["cleanup_pending"] is True
    finally:
        release.set()
        executor.shutdown(time.monotonic() + 1)


# ---------------------------------------------------------- ConfigItem IDs

def test_disabled_node_is_addressed_by_its_config_item_id_and_can_be_enabled():
    import bridge_node_commands
    from bridge_runtime_access import config_item_id
    from tests.test_bridge_p2_14 import FakePort
    port = FakePort()
    disabled = {"tier": "tier2", "kind": "node", "key": "trojan://Off@off.invalid:443#Off",
                "uri": "trojan://Off@off.invalid:443#Off", "name": "Off", "enabled": False}
    port.items.append(disabled)
    commands = bridge_node_commands.NodeCommands(port)
    item_id = config_item_id(disabled)
    resolved = commands.resolve(item_id, need_config=True)
    assert resolved.config_item["key"] == disabled["key"] and resolved.node is None
    result = commands.nodes_update({"item_id": item_id, "updates": {"enabled": True}},
                                   SimpleNamespace(op="o"))
    assert result["saved"] is True
    assert port.called("update_config_item")[0][2] == {"enabled": True}
    with pytest.raises(JobFailure):
        commands.resolve(item_id, need_config=False)   # reveal needs a loaded node
    port.items.append(dict(disabled))                  # duplicate entry: ambiguous
    with pytest.raises(JobFailure) as error:
        commands.resolve(item_id, need_config=True)
    assert error.value.code == "conflict"


def test_config_items_projection_is_safe_and_unique():
    items = [
        {"tier": "tier1", "kind": "node", "key": "Edge", "name": "Edge", "enabled": True,
         "vless_link": "vless://secret@edge:443"},
        {"tier": "exit", "kind": "subscription", "key": "https://sub.invalid/token",
         "url": "https://sub.invalid/token", "label": "https://sub.invalid/token", "enabled": False},
        {"tier": "tier3", "kind": "source", "section": "github_repos", "key": "o/r"},
        {"tier": "tier1", "kind": "node", "key": "Edge", "name": "Edge", "enabled": True},
    ]
    projected = bridge_wiring.config_items_projection(items)
    assert [row["section"] for row in projected] == ["vanguard", "gateway"]
    assert projected[0]["secret_set"] is True and projected[1]["name"] == "subscription"
    assert projected[1]["enabled"] is False
    assert protocol.validate_topic_payload("config.items", {"items": projected}) == []
    assert "sub.invalid" not in json.dumps(projected)


# --------------------------------------------------------------- exports

def test_save_grant_binds_to_for_owner():
    from bridge_exports import ExportService, UnavailableSaveTargets

    class Targets(UnavailableSaveTargets):
        def verify(self, handle_id, *, purpose, format):
            return True

    identity = SimpleNamespace(job_identity=lambda op: (EPOCH, "shell"))
    service = ExportService(targets=Targets(), identity=identity,
                            sources=SimpleNamespace(), log_model=SimpleNamespace(),
                            timer_factory=None)
    args = {"ref": "save-1", "purpose": "bundle", "format": "zip",
            "expires_at_ms": int(time.time() * 1000) + 60_000, "handle_id": 7,
            "for_owner": "window-9"}
    assert service.save_grant(args, SimpleNamespace(op="request-1")) == {"registered": True}
    grant = service.grants._grants["save-1"]
    assert grant.owner == "window-9" and grant.epoch == EPOCH


@pytest.mark.skipif(os.name != "nt", reason="Windows handles")
def test_windows_handle_targets_write_from_zero_and_truncate_only_on_commit(tmp_path):
    import ctypes
    import msvcrt
    from bridge_exports import WindowsHandleSaveTargets

    def handle_for(path):
        descriptor = os.open(path, os.O_RDWR | os.O_BINARY)
        raw = msvcrt.get_osfhandle(descriptor)
        duplicate = ctypes.c_void_p()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        process = kernel32.GetCurrentProcess()
        assert kernel32.DuplicateHandle(ctypes.c_void_p(process), ctypes.c_void_p(raw),
                                        ctypes.c_void_p(process), ctypes.byref(duplicate),
                                        0, False, 2)
        os.close(descriptor)
        return int(duplicate.value)

    path = tmp_path / "export.zip"
    path.write_bytes(b"0123456789-old-tail")
    targets = WindowsHandleSaveTargets()
    handle = handle_for(path)
    assert targets.verify(handle, purpose="bundle", format="zip")
    target = targets.open(handle)
    assert not targets.verify(handle, purpose="bundle", format="zip")   # consumed
    target.write(b"new")
    assert target.commit() == 3
    assert path.read_bytes() == b"new"                 # no stale tail
    path.write_bytes(b"keep-this-content")
    handle = handle_for(path)
    target = targets.open(handle)
    target.write(b"XY")
    target.discard()
    assert path.read_bytes() == b"XYep-this-content"   # discard never truncates
    assert not targets.verify(123457, purpose="bundle", format="zip")
    targets.release(handle_for(path))                  # unused: closed once


# ------------------------------------------------------------- log view

def test_log_view_clear_seq_is_public_and_filters_exports():
    from bridge_exports import LogViewModel
    from bridge_log_view import LogView
    view = LogView()
    for index in range(3):
        view.append({"ts_ms": 1, "rx_ms": 1, "source": "TESSERACT", "level": "info",
                     "category": "other", "key": "k", "title": f"t{index}", "detail": "",
                     "raw": ""})
    assert view.clear_seq == 0
    view.clear_view()
    assert view.clear_seq == 3
    snapshot = LogViewModel(lambda: view).export_snapshot()
    assert snapshot.clear_seq == 3


# --------------------------------------------------- secret registry usage

def test_settings_and_node_commands_lease_instead_of_pushing_whole_tables():
    import bridge_node_commands
    import bridge_settings
    from bridge_secrets import SecretRegistry
    pushed = []
    registry = SecretRegistry(lambda: {})
    registry.attach(lambda table: pushed.append(dict(table)))
    refreshed = []
    session = SimpleNamespace(secrets=registry, logs=SimpleNamespace(
        refresh_known_secret_values=lambda table: (_ for _ in ()).throw(AssertionError("push"))),
        refresh_secrets=lambda: refreshed.append(True),
        access=SimpleNamespace(known_secret_values=lambda: {}))
    for module, owner in ((bridge_settings, "settings"), (bridge_node_commands, "node_commands")):
        register, refresh = module._session_secret_hooks(session)
        register({"Secret-" + owner: "node"})
        assert registry.lease_count(owner) == 1
        assert "Secret-" + owner in pushed[-1]
        refresh()
        assert registry.lease_count(owner) == 0
    assert refreshed == [True, True]


# ---------------------------------------------------------- shutdown order

def test_full_exit_closes_owners_before_recovery_and_bye_is_last():
    from tests.test_bridge_shutdown import setup
    shutdown, runtime, ownership = setup()
    order = []
    phases = []
    shutdown.close_owners = lambda budget: order.append(("owners", budget > 0)) or True
    original_recover = shutdown.recover_network

    def recover(budget):
        order.append("recovery")
        return original_recover(budget)
    shutdown.recover_network = recover
    shutdown.on_phase = lambda phase, recovery, workers_pending=True: phases.append(phase)
    shutdown._service_running_at_quit = False
    closed = []
    runtime.alerts = SimpleNamespace(close=lambda timeout: closed.append(timeout) or True)
    sent = []
    original_send = shutdown.send
    shutdown.send = lambda frame: (sent.append(frame["t"]), original_send(frame))[1]
    ack = shutdown.request_quit()
    assert shutdown.executor.wait(ack.op, 2)["state"] in {"done", "failed"}
    deadline = time.monotonic() + 10
    while not shutdown.finish_quit(deadline_seconds=.2):
        assert time.monotonic() < deadline
    assert order[:2] == [("owners", True), "recovery"]
    assert closed == [3.0]                 # no service at quit: Qt's alerts.close(3)
    assert sent[-1] == "bye"
    assert phases[0] == "draining" and phases[-1] == "finished"
    assert phases.index("recovering") < phases.index("waiting_workers")


def test_full_exit_completes_when_an_owner_close_returns_false_at_its_budget():
    from tests.test_bridge_shutdown import setup
    shutdown, runtime, ownership = setup()
    release = threading.Event()
    shutdown.owner_close_seconds = .2

    def slow(budget):
        release.wait(budget)   # an owner that honours its budget (see the next test for one that does not)
        return False
    shutdown.close_owners = slow
    ack = shutdown.request_quit()
    assert shutdown.executor.wait(ack.op, 2)["state"] in {"done", "failed"}
    deadline = time.monotonic() + 10
    while not shutdown.finish_quit(deadline_seconds=.2):
        assert time.monotonic() < deadline
    assert shutdown.bye_submitted or shutdown._finished


def test_wiring_close_runs_every_owner_and_reports_failures():
    calls = []
    session = SimpleNamespace(runtime=SimpleNamespace(
        close_node_diagnostics=lambda timeout: calls.append("diagnostics") or True,
        cancel_subscription_diagnostics=lambda: calls.append("subscriptions") or 0))
    wiring = bridge_wiring.Wiring(session)
    wiring.owners = {
        "exit": SimpleNamespace(observe_presence=lambda value: calls.append(("exit", value["visible"]))),
        "alerts_desktop": SimpleNamespace(close=lambda: (_ for _ in ()).throw(RuntimeError("x"))),
        "exports": SimpleNamespace(close=lambda: calls.append("exports")),
        "settings_apply": SimpleNamespace(close=lambda timeout: calls.append("apply") or True),
    }
    assert wiring.close(1.0) is False   # the desktop owner raised
    assert calls == ["diagnostics", "subscriptions", ("exit", False), "exports", "apply"]
    assert wiring._closed
    wiring.tick()                       # closed: no polling


def test_shutdown_phases_only_move_forward():
    recorded = []
    state = SimpleNamespace(record_state=lambda topic, dto: recorded.append(("snapshot", dto["phase"])),
                            record_edge=lambda topic, dto: recorded.append(("edge", dto["phase"])))
    wiring = bridge_wiring.Wiring(SimpleNamespace(state=state))
    # finish_quit retries report draining before it learns the stop still runs.
    for phase in ("draining", "stopping", "draining", "stopping", "recovering",
                  "draining", "waiting_workers", "finished", "finished", "bogus"):
        wiring.shutdown_phase(phase, None)
    assert recorded == [("snapshot", "draining"), ("edge", "stopping"), ("edge", "recovering"),
                        ("edge", "waiting_workers"), ("edge", "finished")]


def test_wiring_close_closes_the_update_handoff_entrance(tmp_path):
    import app_update_state
    import bridge_update
    published = []
    owner = bridge_update.UpdateOwner(app_update_state.update_state_path(tmp_path),
                                      tmp_path / "updates", updater=bridge_wiring.IsolatedUpdater())
    owner.attach_handoff(SimpleNamespace(publish_prepare_exit=published.append),
                         bridge_update.ShutdownExitAdapter(lambda: SimpleNamespace(quitting=False)))
    session = SimpleNamespace(runtime=SimpleNamespace(close_node_diagnostics=lambda timeout: True,
                                                      cancel_subscription_diagnostics=lambda: 0),
                              shutdown=SimpleNamespace(quitting=True))
    wiring = bridge_wiring.Wiring(session)
    wiring.owners = {"update": owner}
    assert wiring.close(1.0) is True
    receipt = {"receipt_ref": "receipt-1", "target_version": "9.9.9",
               "installer_path": str(tmp_path / "Tesseract-Setup-9.9.9-win-x64.exe"),
               "installer_sha256": "0" * 64}
    with pytest.raises(bridge_update._HandoffUnpublished):
        owner._start_handoff(receipt)    # the closed entrance: nothing published
    assert published == [] and owner.snapshot()["data"]["state"] == "verified"


# ---------------------------------------------------------- full wiring e2e

pytestmark_windows = pytest.mark.skipif(os.name != "nt", reason="Windows integration")

SAFE_ARGS = {
    "service.restart": {}, "service.stop": {}, "mode.set": {"mode": "system"},
    "exit.verify": {"trigger": "manual"},
    "settings.apply": {"base_rev": 999999, "changes": [{"section": "ui", "values": {}}]},
    "settings.validate": {"section": "network", "field": "network.mihomo_mixed_port", "value": 7890},
    "nodes.diagnose": {"node_id": "0" * 24, "probe_mode": "direct", "request_id": "diag-1"},
    "nodes.cancelDiagnose": {"request_id": "diag-unknown"},
    "nodes.update": {"item_id": "0" * 24, "updates": {"name": "x"}},
    "nodes.delete": {"item_id": "0" * 24},
    "nodes.reveal": {"item_id": "0" * 24},
    "subscriptions.refresh": {"item_id": "0" * 24},
    "import.preview": {"tier": "sentinel", "text": "not a node"},
    "import.apply": {"tier": "sentinel", "mode": "paste", "plan_id": "plan-unknown"},
    "gateway.select": {"proxy_name": "Fixture-Exit"},
    "geo.update": {"force": True},
    "app.update.check": {}, "app.update.install": {},
    "logs.setMihomoLevel": {"level": "info"},
    "logs.exportBundle": {"save_ref": "save-unknown"},
    "logs.exportFiltered": {"save_ref": "save-unknown", "format": "json", "seqs": [1]},
    "alerts.test": {},
    "alerts.desktopDelivered": {"delivery_id": "desktop-never", "outcome": "delivered",
                                "surface": "windows"},
    "ui.presence": {"visible": False, "focused": False, "page": "none"},
    "shell.saveGrant": {"ref": "save-x", "purpose": "bundle", "format": "zip",
                        "expires_at_ms": 0, "handle_id": 1234567, "for_owner": "window-1"},
    "update.exitResponse": {"handoff_id": "handoff-unknown", "accepted": False,
                            "reason_code": "declined"},
    "update.installResult": {"receipt_ref": "receipt-unknown", "launched": False,
                             "reason_code": "launch_failed"},
    "host.saveFile": {"purpose": "bundle", "format": "zip"},
    "host.openFolder": {"kind": "logs"}, "host.openUrl": {"url": "https://github.com"},
    "host.reveal": {"ref": "save-x"},
}
QUERY_ARGS = {
    "app.info": {}, "config.snapshot": {}, "nodes.detail": {"node_id": "0" * 24},
    "import.specs": {},
    "logs.tail": {"n": 5}, "logs.query": {"before_seq": 10, "filter": {}, "n": 5},
    "autostart.state": {}, "geo.status": {},
}


@pytest.mark.skipif(os.name != "nt", reason="Windows integration")
def test_real_sidecar_announces_v1_and_answers_every_command_and_query(tmp_path):
    """minimal runtime e2e: real source sidecar, temporary custom scope."""
    from tests.test_bridge_integration import Shell, events, isolated_env
    env = isolated_env(tmp_path)
    shell = Shell(env)
    try:
        hello = shell.hello()
        caps = hello["caps"]
        assert set(caps["commands"]) == protocol.HELLO_CAP_COMMANDS
        assert set(caps["topics"]) == protocol.V1_TOPICS
        # Undeclared/partial subscriptions are refused as a whole.
        shell.send("subscribe", id=1, topics=sorted(caps["topics"]))
        assert shell.until(lambda frame: frame["t"] == "subscribed")["topics"] == sorted(caps["topics"])
        ident = 100
        terminal = {"done", "failed", "cancelled", "superseded"}
        for name in sorted(protocol.V1_COMMANDS - {"app.quit", "app.sessionEnd", "service.start"}):
            ident += 1
            args = deepcopy(SAFE_ARGS[name])
            if name == "shell.saveGrant":
                args["expires_at_ms"] = int(time.time() * 1000) + 60_000
            origin = "shell" if name in protocol.SHELL_COMMANDS else "page"
            fields = dict(id=ident, cmd=name, args=args, req_id=f"req-{ident}",
                          owner="shell" if origin == "shell" else "window-1", **{"from": origin})
            shell.send("req", **fields)
            reply = shell.until(lambda frame: frame.get("id") == ident and frame["t"] in {"ack", "res", "error"},
                                timeout=30)
            assert protocol.validate_frame(reply) == [], (name, reply)
            if name.startswith("host."):
                assert reply["t"] == "error" and reply["error"]["code"] == "not_allowed", name
                continue
            if reply["t"] == "ack" and reply["accepted"]:
                op = reply["op"]
                edge = shell.until(lambda frame: any(
                    item["topic"] == "jobs" and item["data"]["op"] == op
                    and item["data"]["state"] in terminal for item in events(frame)), timeout=60)
                job = next(item for item in events(edge) if item["topic"] == "jobs"
                           and item["data"]["op"] == op and item["data"]["state"] in terminal)
                assert protocol.validate_frame(job) == [], (name, job)
            else:
                assert reply["t"] in {"res", "ack", "error"}, (name, reply)
        for name in sorted(protocol.V1_QUERIES):
            ident += 1
            fields = dict(id=ident, cmd=name, args=deepcopy(QUERY_ARGS[name]),
                          req_id=f"req-{ident}", owner="window-1", **{"from": "page"})
            shell.send("req", **fields)
            reply = shell.until(lambda frame: frame.get("id") == ident and frame["t"] in {"res", "error"},
                                timeout=30)
            request = dict(t="req", epoch=shell.epoch, produced_at_ms=0, sent_at_ms=0, **fields)
            assert protocol.validate_response(request, reply) == [], (name, reply)
            if name in {"app.info", "config.snapshot", "import.specs", "alerts.catalog",
                        "autostart.state", "geo.status", "logs.tail", "alerts.deliveries"}:
                assert reply["ok"] is True, (name, reply)
            if name == "app.info":
                assert set(reply["data"]["caps"]) == protocol.HELLO_CAP_COMMANDS
        # Full exit: bye is the last frame and exits 0.
        shell.send("req", id=9000, cmd="app.quit", args={}, **{"from": "shell"})
        bye = shell.until(lambda frame: frame["t"] == "bye", timeout=60)
        assert bye["exit_code"] == 0
        assert shell.process.wait(timeout=30) == 0
    finally:
        shell.close()


# ------------------------------------------------- fix round 1 (review A/B)

def _dup_handle(path):
    import ctypes
    import msvcrt
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.DuplicateHandle.argtypes = [ctypes.c_void_p] * 4 + [ctypes.c_ulong, ctypes.c_bool,
                                                                 ctypes.c_ulong]
    descriptor = os.open(path, os.O_RDWR | os.O_BINARY | os.O_CREAT)
    raw = msvcrt.get_osfhandle(descriptor)
    duplicate = ctypes.c_void_p()
    process = kernel32.GetCurrentProcess()
    assert kernel32.DuplicateHandle(ctypes.c_void_p(process), ctypes.c_void_p(raw),
                                    ctypes.c_void_p(process), ctypes.byref(duplicate), 0, False, 2)
    os.close(descriptor)
    return int(duplicate.value)


def _handle_open(handle):
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetHandleInformation.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    flags = wintypes.DWORD(0)
    return bool(kernel32.GetHandleInformation(handle, ctypes.byref(flags)))


@pytest.mark.skipif(os.name != "nt", reason="Windows handles")
def test_windows_handle_targets_accept_a_recycled_handle_value(tmp_path):
    from bridge_exports import WindowsHandleSaveTargets
    targets = WindowsHandleSaveTargets()
    committed = set()
    recycled = None
    for index in range(200):
        handle = _dup_handle(tmp_path / f"export-{index}.zip")
        if handle in committed:
            recycled = handle
        assert targets.verify(handle, purpose="bundle", format="zip"), (index, handle)
        target = targets.open(handle)
        assert not targets.verify(handle, purpose="bundle", format="zip")   # held while open
        target.write(b"x")
        assert target.commit() == 1
        committed.add(handle)
        if recycled is not None:
            break
    assert recycled is not None, "Windows did not recycle a handle value in 200 rounds"
    assert targets.open_count() == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows handles")
def test_refused_save_grants_really_close_their_handle_and_spare_non_files(tmp_path):
    import ctypes
    from bridge_exports import SaveGrantTable, WindowsHandleSaveTargets
    targets = WindowsHandleSaveTargets()
    table = SaveGrantTable(targets, timer_factory=None)
    handle = _dup_handle(tmp_path / "late.zip")
    with pytest.raises(JobFailure) as error:
        table.register({"ref": "save-late", "purpose": "bundle", "format": "zip",
                        "expires_at_ms": int(time.time() * 1000) - 1, "handle_id": handle},
                       (EPOCH, "window-1"))
    assert error.value.code == "validation"
    assert not _handle_open(handle), "a refused grant closes the duplicated file handle"
    # A stale value that is not a disk file (here: an event) is never closed.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateEventW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    event = int(kernel32.CreateEventW(None, True, False, None))
    try:
        with pytest.raises(JobFailure):
            table.register({"ref": "save-event", "purpose": "bundle", "format": "zip",
                            "expires_at_ms": int(time.time() * 1000) + 60_000, "handle_id": event},
                           (EPOCH, "window-1"))
        assert _handle_open(event)
    finally:
        kernel32.CloseHandle(event)


def test_save_grant_for_a_revoked_owner_is_refused_and_its_handle_released():
    from bridge_exports import ExportService, UnavailableSaveTargets
    released = []

    class Targets(UnavailableSaveTargets):
        def verify(self, handle_id, *, purpose, format):
            return True

        def release(self, handle_id):
            released.append(handle_id)

    service = ExportService(targets=Targets(),
                            identity=SimpleNamespace(job_identity=lambda op: (EPOCH, "shell")),
                            sources=SimpleNamespace(), log_model=SimpleNamespace(),
                            timer_factory=None)
    service.revoke_owner("window-9")      # owner_revoke arrived before the shell's saveGrant
    args = {"ref": "save-1", "purpose": "bundle", "format": "zip",
            "expires_at_ms": int(time.time() * 1000) + 60_000, "handle_id": 11,
            "for_owner": "window-9"}
    with pytest.raises(JobFailure) as error:
        service.save_grant(args, SimpleNamespace(op="request-1"))
    assert error.value.code == "not_allowed" and released == [11]
    assert len(service.grants) == 0
    args.update(ref="save-2", handle_id=12, for_owner="window-10")
    assert service.save_grant(args, SimpleNamespace(op="request-2")) == {"registered": True}


def test_geo_update_that_did_not_complete_fails_timeout():
    recorded = []
    session = SimpleNamespace(
        runtime=SimpleNamespace(trigger_geo_update=lambda force: None),
        state=SimpleNamespace(record_state=lambda topic, dto: recorded.append(topic),
                              record_edge=lambda topic, dto: recorded.append(topic)))
    wiring = bridge_wiring.Wiring(session)
    wiring.owners["queries"] = SimpleNamespace(
        geo_status=lambda args, context: {"running": True, "updated_at_ms": None, "errors": []})
    pending = wiring._geo_update({"force": True}, None)
    assert wait_for(lambda: pending.settled)
    kind, failure = pending._outcome
    assert kind == "failure" and failure.code == "timeout"
    assert failure.partial_result["completed"] is False


def test_named_flights_are_created_once_under_concurrency():
    wiring = bridge_wiring.Wiring(SimpleNamespace())
    barrier = threading.Barrier(8)
    flights = []

    def create():
        barrier.wait()
        flights.append(wiring._flight("nodes", lambda: None))
    threads = [threading.Thread(target=create) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len({id(flight) for flight in flights}) == 1


def test_full_exit_abandons_an_owner_close_that_never_returns():
    from tests.test_bridge_shutdown import setup
    import bridge_shutdown
    shutdown, runtime, ownership = setup()
    never = threading.Event()
    shutdown.owner_close_seconds = .1
    shutdown.close_owners = lambda budget: never.wait()   # ignores its budget entirely
    ack = shutdown.request_quit()
    assert shutdown.executor.wait(ack.op, 2)["state"] in {"done", "failed"}
    started = time.monotonic()
    cap = shutdown.owner_close_seconds + bridge_shutdown.OWNER_CLOSE_EXTRA_SECONDS
    try:
        while not shutdown.finish_quit(deadline_seconds=.2):
            assert time.monotonic() - started < cap + 5
        assert shutdown.bye_submitted or shutdown._finished
        assert shutdown._owners_abandoned and not shutdown._owners_done.is_set()
        assert time.monotonic() - started >= cap - .3
    finally:
        never.set()
        shutdown.executor.shutdown(time.monotonic() + 1)


def test_session_end_hurries_the_owner_close():
    from tests.test_bridge_shutdown import setup
    import bridge_shutdown
    shutdown, runtime, ownership = setup()
    budgets, timeouts = [], []
    shutdown.close_owners = lambda budget: budgets.append(budget) or True
    runtime.alerts = SimpleNamespace(close=lambda timeout: timeouts.append(timeout) or True)
    shutdown._service_running_at_quit = False
    try:
        assert shutdown._owner_close_cap() == shutdown.owner_close_seconds + 3.5
        shutdown.session_end.started = True
        assert shutdown._owner_close_cap() == bridge_shutdown.SESSION_END_OWNER_GRACE
        shutdown._run_owner_close()
        assert budgets == [0.0] and timeouts == [0.0]
    finally:
        shutdown.executor.shutdown(time.monotonic() + 1)   # no worker threads outlive the test


def test_scope_without_kind_uses_the_isolated_updater(tmp_path, monkeypatch):
    import bridge_update
    registered = []
    session = SimpleNamespace(scope=SimpleNamespace(data_dir=tmp_path))
    wiring = bridge_wiring.Wiring(session)
    assert wiring.scope_kind is None
    monkeypatch.setattr(bridge_update, "register", lambda session, owner: registered.append(owner))
    wiring._update()
    assert isinstance(registered[0]._updater, bridge_wiring.IsolatedUpdater)


def test_isolated_autostart_manager_never_writes_the_run_value(monkeypatch):
    import autostart
    import bridge_autostart
    writes = []

    class Recorder:
        def __init__(self, *args, **kwargs):
            self.value_name = "Tesseract"

        def current_command(self):
            return None

        def disable(self):
            writes.append("disable")
            return {"enabled": False}

        def enable(self):
            writes.append("enable")
            return {"enabled": True}
    monkeypatch.setattr(autostart, "AutostartManager", Recorder)
    monkeypatch.setattr(autostart, "shell_autostart_manager", lambda shell: Recorder())
    base = bridge_autostart.SidecarAutostartManager(Path(sys.executable), Path(sys.executable), None)
    isolated = bridge_autostart.isolated_autostart_manager(base)
    assert isinstance(isolated, bridge_autostart.SidecarAutostartManager)
    assert bridge_autostart.isolated_autostart_manager(isolated) is isolated
    for enabled in (False, True):
        result = isolated.set_enabled(enabled)
        assert result["supported"] is False and result["error"]
    assert isolated._layout_error() is not None
    assert writes == []
    sentinel = object()
    assert bridge_autostart.isolated_autostart_manager(sentinel) is sentinel


def _until_terminal(shell, ident, timeout=60):
    """The reply to request ``ident``: a res/error frame, or the terminal job of its ack."""
    from tests.test_bridge_integration import events
    reply = shell.until(lambda frame: frame.get("id") == ident and frame["t"] in {"ack", "res", "error"},
                        timeout=30)
    if reply["t"] != "ack":
        return reply
    assert reply["accepted"], reply
    op = reply["op"]
    terminal = {"done", "failed", "cancelled", "superseded"}
    edge = shell.until(lambda frame: any(
        item["topic"] == "jobs" and item["data"]["op"] == op and item["data"]["state"] in terminal
        for item in events(frame)), timeout=timeout)
    return next(item["data"] for item in events(edge) if item["topic"] == "jobs"
                and item["data"]["op"] == op and item["data"]["state"] in terminal)


@pytest.mark.skipif(os.name != "nt", reason="Windows integration")
def test_real_sidecar_exports_through_a_real_handle_and_saves_settings(tmp_path):
    """Review B (fix round 1): success paths end to end -- a real duplicated
    file handle granted by the shell receives the bundle, and a controlled
    settings write commits the persisted revision."""
    import ctypes
    import msvcrt
    import zipfile
    from tests.test_bridge_integration import Shell, events, isolated_env
    env = isolated_env(tmp_path)
    shell = Shell(env)
    target = tmp_path / "bundle.zip"
    try:
        caps = shell.hello()["caps"]
        shell.send("subscribe", id=1, topics=sorted(caps["topics"]))
        shell.until(lambda frame: frame["t"] == "subscribed")
        # 1. The shell duplicates a real handle into the sidecar and grants it to window-1.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.DuplicateHandle.argtypes = [ctypes.c_void_p] * 4 + [ctypes.c_ulong, ctypes.c_bool,
                                                                     ctypes.c_ulong]
        descriptor = os.open(target, os.O_RDWR | os.O_BINARY | os.O_CREAT)
        remote = ctypes.c_void_p()
        try:
            assert kernel32.DuplicateHandle(ctypes.c_void_p(kernel32.GetCurrentProcess()),
                                            ctypes.c_void_p(msvcrt.get_osfhandle(descriptor)),
                                            ctypes.c_void_p(int(shell.process._handle)),
                                            ctypes.byref(remote), 0, False, 2)
        finally:
            os.close(descriptor)
        grant = {"ref": "save-e2e-1", "purpose": "bundle", "format": "zip",
                 "expires_at_ms": int(time.time() * 1000) + 60_000,
                 "handle_id": int(remote.value), "for_owner": "window-1"}
        shell.send("req", id=10, cmd="shell.saveGrant", args=grant, req_id="req-10", owner="shell",
                   **{"from": "shell"})
        reply = _until_terminal(shell, 10)
        assert (reply.get("data") or reply.get("result")) == {"registered": True}, reply
        shell.send("req", id=11, cmd="logs.exportBundle", args={"save_ref": "save-e2e-1"},
                   req_id="req-11", owner="window-1", **{"from": "page"})
        job = _until_terminal(shell, 11)
        assert job["state"] == "done", job
        assert job["result"]["ref"] == "save-e2e-1" and job["result"]["size"] > 0
        assert "bundle.zip" not in json.dumps(job), "no path reaches the page"
        # The sidecar committed and closed its handle: the file is a complete zip of that size.
        assert target.stat().st_size == job["result"]["size"]
        with zipfile.ZipFile(target) as archive:
            assert archive.testzip() is None and archive.namelist()
        # 2. A controlled settings write succeeds and advances the persisted revision.
        shell.send("req", id=20, cmd="config.snapshot", args={}, req_id="req-20", owner="window-1",
                   **{"from": "page"})
        before = shell.until(lambda frame: frame.get("id") == 20 and frame["t"] == "res")["data"]["rev"]
        shell.send("req", id=21, cmd="settings.apply", req_id="req-21", owner="window-1",
                   args={"base_rev": before,
                         "changes": [{"section": "ui", "values": {"language": "zh_CN"}}]},
                   **{"from": "page"})
        job = _until_terminal(shell, 21)
        assert job["state"] == "done", job
        assert any(section["section"] == "ui" and section["saved"] is True
                   for section in job["result"]["sections"]), job
        changed = [item["data"] for frame in shell.frames for item in events(frame)
                   if item["topic"] == "config.changed"]
        if not changed:
            changed = [item["data"] for item in events(shell.until(lambda frame: any(
                item["topic"] == "config.changed" for item in events(frame)), timeout=20))
                if item["topic"] == "config.changed"]
        assert changed[-1]["rev"] == before + 1
        shell.send("req", id=22, cmd="config.snapshot", args={}, req_id="req-22", owner="window-1",
                   **{"from": "page"})
        after = shell.until(lambda frame: frame.get("id") == 22 and frame["t"] == "res")["data"]
        assert after["rev"] == before + 1 and after["values"]["ui"]["language"] == "zh_CN"
        # The ui section is local (bridge_settings.LOCAL_SECTIONS): it lands in config.local.yaml.
        local_yaml = (tmp_path / "config" / "config.local.yaml").read_text(encoding="utf-8")
        assert "language: zh_CN" in local_yaml
        main_yaml = tmp_path / "config" / "config.yaml"
        if main_yaml.exists():
            assert "language: zh_CN" not in main_yaml.read_text(encoding="utf-8")
        shell.send("req", id=9000, cmd="app.quit", args={}, **{"from": "shell"})
        assert shell.until(lambda frame: frame["t"] == "bye", timeout=60)["exit_code"] == 0
        assert shell.process.wait(timeout=30) == 0
    finally:
        shell.close()
