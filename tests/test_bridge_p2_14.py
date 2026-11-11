"""P2-14 node/subscription/Gateway/mode commands: substitute runtimes only.

No app start, Core, registry, firewall or network access. Results run through
the real JobExecutor so the PROTOCOL section-15 adapter and jobs frames apply.
"""

from __future__ import annotations

from copy import deepcopy
import json
import threading
import time
from types import SimpleNamespace

import pytest

import bridge_commands as commands
import bridge_node_commands as node_commands
import bridge_protocol as protocol
from bridge_jobs import JobEventRecorder, JobExecutor
from bridge_node_commands import (
    NodeCommands, RuntimeNodeCommandAdapter, StaleItemFacts, node_item_id,
    subscription_item_id,
)
from models import Node, NodeStatus
from tests.test_bridge_p2_04 import runtime  # noqa: F401  (P2-04 isolated runtime fixture)


SUB_URL = "https://sub.example.invalid/private-token-ABC"
MANUAL_URI = "trojan://ManualPass123@manual.example.invalid:443#Manual"
NEW_URI = "trojan://BrandNewSecret987@new.example.invalid:443#New"


def node(name, tier, **fields):
    defaults = dict(protocol="trojan", address=f"{name}.example.invalid", port=443,
                    password=f"{name}-pw", status=NodeStatus.ALIVE, tier=tier)
    defaults.update(fields)
    return Node(name=name, **defaults)


class FakePort:
    """Port substitute: records calls, exposes every fact the handler reads."""

    def __init__(self):
        self.calls = []
        self.generation = 7
        self.core = {"running": True, "pid": 42, "process_generation": 3,
                     "cleanup_pending": False, "core_generation": 10}
        self.loaded_mode = "system_proxy"
        self.persisted = "system_proxy"
        # Mode the running Core was started with; exposed only when the
        # (future) runtime getter is modelled.
        self.core_mode = "system_proxy"
        self.expose_core_mode = False
        # Runtime _restart_core_serialized: an unclean stop returns its
        # stop_result without raising and the old Core keeps running.
        self.restart_fails = False
        self.readback = None
        self.saved_exit = None  # persisted exit selection; None = unreadable
        self.stop_requested = False
        self.phase = "running"
        self.busy = None
        self.refresh_state = None
        self.nodes = [
            ("tier2", node("Manual", "tier2", uri=MANUAL_URI, password="ManualPass123")),
            ("exit", node("Member", "exit", uri="vless://member", uuid="MemberUuid-1",
                          protocol="vless", password=None, source=SUB_URL, sources=[SUB_URL])),
            ("tier1", node("Edge :: VLESS", "tier1", uri="vless://edge", uuid="EdgeUuid-1",
                           protocol="vless", password=None, extra={"physical_node": "Edge"})),
            ("tier2", node("Wg", "tier2", protocol="wireguard", uri="wireguard://wg",
                           password="WgPrivateKey=")),
        ]
        self.subscriptions = [("exit", 0, SUB_URL)]
        self.items = [
            {"tier": "tier2", "kind": "node", "key": MANUAL_URI, "uri": MANUAL_URI, "enabled": True},
            {"tier": "tier1", "kind": "node", "key": "Edge", "name": "Edge", "enabled": True},
            {"tier": "exit", "kind": "subscription", "key": SUB_URL, "url": SUB_URL, "enabled": True},
        ]
        self.set_mode = self._set_mode
        self.update = lambda item, updates: {"node_config_transaction": True}
        self.delete = self._delete
        self.reload = lambda **kw: {"reloaded": True, "running": True}
        self.refresh = lambda item: {"total": 3, "healthy": 2, "stored": True, "applied": True,
                                     "retained_previous": False, "core_running": True,
                                     "apply_error": None}
        self.select = lambda name: {"selected": name, "applied": True, "core_running": True,
                                    "selection": {"group": "EXIT-SELECT", "selected": name}}

    def _set_mode(self, mode):
        if mode == self.loaded_mode:
            return {"mode": mode, "changed": False}
        self.persisted = self.loaded_mode = mode  # update_local_config -> reload_config
        running = self.core["running"]
        if running and not self.restart_fails:
            self.core = dict(self.core, core_generation=self.core["core_generation"] + 1)
            self.core_mode = mode
        return {"mode": mode, "changed": True, "restarted": running}

    def service_restart(self):
        """Simulates a later successful Core restart under the loaded mode."""
        self.core = dict(self.core, core_generation=self.core["core_generation"] + 1,
                         running=True, cleanup_pending=False)
        self.core_mode = self.loaded_mode

    def _delete(self, item):
        self.items = [row for row in self.items if row["key"] != item["key"]]
        return {"path": "synthetic"}

    def core_fact(self):
        return dict(self.core)

    def lifecycle_status(self):
        return {"intent": {"runtime_generation": self.generation,
                           "stop_requested": self.stop_requested},
                "runtime": {"startup_phase": self.phase}}

    def network_mode(self):
        return self.loaded_mode

    def persisted_network_mode(self):
        return self.persisted

    def set_network_mode(self, mode):
        self.calls.append(("set_network_mode", mode))
        return self.set_mode(mode)

    def core_loaded_network_mode(self):
        return self.core_mode if self.expose_core_mode and self.core["running"] else None

    def loaded_node_index(self):
        if isinstance(self.nodes, Exception):
            raise self.nodes
        self.calls.append(("loaded_node_index",))
        return self.generation, tuple((tier, node_commands.node_identity(value), value)
                                      for tier, value in self.nodes)

    def detach_node(self, generation, tier, handle):
        self.calls.append(("detach_node", tier, handle.name))
        if generation != self.generation or not any(
                t == tier and n is handle for t, n in self.nodes):
            raise StaleItemFacts("gone")
        return deepcopy(handle)

    def subscription_entries(self):
        return tuple(self.subscriptions)

    def configured_items(self):
        return deepcopy(self.items)

    def update_config_item(self, item, updates):
        self.calls.append(("update_config_item", deepcopy(item), deepcopy(updates)))
        return self.update(item, updates)

    def delete_config_item(self, item):
        self.calls.append(("delete_config_item", deepcopy(item)))
        return self.delete(item)

    def reload_core_config(self, **kwargs):
        self.calls.append(("reload_core_config", kwargs))
        return self.reload(**kwargs)

    def refresh_subscription(self, item):
        self.calls.append(("refresh_subscription", deepcopy(item)))
        return self.refresh(item)

    def set_exit_selected_proxy(self, name):
        self.calls.append(("set_exit_selected_proxy", name))
        return self.select(name)

    def exit_selection_readback(self):
        return self.readback

    def persisted_exit_selection(self):
        if isinstance(self.saved_exit, Exception):
            raise self.saved_exit
        return self.saved_exit

    def subscription_refresh_state(self):
        return self.refresh_state

    def subscription_refresh_busy(self):
        return self.busy

    def called(self, name):
        return [call for call in self.calls if call[0] == name]


def nid(port, tier, name):
    value = next(n for t, n in port.nodes if t == tier and n.name == name)
    return node_item_id(port.generation, tier, value)


def sid(port):
    return subscription_item_id("exit", 0, SUB_URL)


@pytest.fixture
def port():
    return FakePort()


@pytest.fixture
def executor():
    instance = JobExecutor(JobEventRecorder(8192), epoch="p2-14", require_acceptance=True)
    yield instance
    instance.shutdown(time.monotonic() + 1)


@pytest.fixture
def handler(port, executor):
    log = []
    instance = NodeCommands(port, register_secrets=lambda values: log.append(("register", values)),
                            refresh_secrets=lambda: log.append(("refresh",)))
    instance.secret_log = log
    for name, method in (("mode.set", instance.mode_set), ("nodes.update", instance.nodes_update),
                         ("nodes.delete", instance.nodes_delete), ("nodes.reveal", instance.nodes_reveal),
                         ("subscriptions.refresh", instance.subscriptions_refresh),
                         ("gateway.select", instance.gateway_select)):
        executor.register(name, method)
    return instance


def run(executor, name, args, owner="window-a"):
    ack = executor.submit(name, args, req_id="request-a", owner=owner)
    assert ack.accepted, ack
    assert executor.acceptance_sent(ack.op, epoch=executor.epoch)
    job = executor.wait(ack.op, 5)
    assert job is not None
    events = executor.recorder.prepare_for_send(1)
    assert events and all(not protocol.validate_frame(event) for event in events)
    executor.recorder.ack_sent(events)
    return job


def ask(executor, name, args, owner="window-a"):
    receipt = executor.submit(name, args, req_id="request-a", owner=owner)
    assert receipt.accepted
    return receipt.wait(5)


def wire_text(value):
    return json.dumps(value, sort_keys=True)


# ---------------------------------------------------------------------------
# IDs and resolution
# ---------------------------------------------------------------------------

def test_p204_runtime_parity(runtime):
    rt = runtime
    rt.lifecycle_status = lambda: {"intent": {"runtime_generation": rt._runtime_generation}}
    rt.manager.runtime_nodes["tier2"] = [node("Manual", "tier2", uri=MANUAL_URI)]
    records = rt.bridge_node_records()
    subscriptions = rt.bridge_subscription_records()
    adapter = RuntimeNodeCommandAdapter(rt)
    generation, index = adapter.loaded_node_index()
    assert {node_commands.identity_item_id(generation, tier, identity)
            for tier, identity, _ in index} == {
        row["id"] for row in records["items"]}
    assert {subscription_item_id(*row) for row in adapter.subscription_entries()} == {
        row["id"] for row in subscriptions["items"]}
    resolver = NodeCommands(adapter)
    for row in records["items"]:
        assert resolver.resolve(row["id"], need_config=False).kind == "node"
    for row in subscriptions["items"]:
        assert resolver.resolve(row["id"], need_config=False).url == "https://example.invalid/private-sub"


@pytest.mark.parametrize("item_id", ["", "not-an-id", "0" * 24, "A" * 24])
def test_unknown_or_malformed_ids_are_conflict(executor, handler, item_id, port):
    job = run(executor, "nodes.delete", {"item_id": item_id or "x"})
    assert job["state"] == "failed" and job["error"]["code"] == "conflict"
    assert job["result"] == {"saved": False, "applied": False, "apply_error": None, "pending": None}
    assert not port.called("delete_config_item")


def test_node_ids_are_bound_to_runtime_generation(executor, handler, port):
    old = nid(port, "tier2", "Manual")
    port.generation = 8
    job = run(executor, "nodes.delete", {"item_id": old})
    assert job["error"]["code"] == "conflict" and not port.called("delete_config_item")
    port.nodes = StaleItemFacts("raced")
    job = run(executor, "nodes.delete", {"item_id": nid(FakePort(), "tier2", "Manual")})
    assert job["error"]["code"] == "conflict"


def test_subscription_members_are_not_editable_config(executor, handler, port):
    job = run(executor, "nodes.update", {"item_id": nid(port, "exit", "Member"),
                                         "updates": {"name": "Renamed"}})
    assert job["error"]["code"] == "conflict" and not port.called("update_config_item")


# ---------------------------------------------------------------------------
# mode.set
# ---------------------------------------------------------------------------

def test_mode_set_restart_verified_by_core_generation(executor, handler, port):
    port.loaded_mode = port.persisted = "tun"
    job = run(executor, "mode.set", {"mode": "system"})
    assert job["state"] == "done"
    assert job["result"] == {"mode": "system", "changed": True, "saved": True,
                             "restarted": True, "applied": True, "apply_error": None}
    assert port.called("set_network_mode") == [("set_network_mode", "system_proxy")]


def test_mode_set_runtime_restart_claim_without_new_generation_is_not_restart(executor, handler, port):
    def claim_only(mode):
        port.persisted = port.loaded_mode = mode
        return {"mode": mode, "changed": True, "restarted": True}  # pre-op running flag
    port.set_mode = claim_only
    job = run(executor, "mode.set", {"mode": "mixed"})
    assert job["state"] == "failed"
    result = job["result"]
    assert result["saved"] is True and result["changed"] is True
    assert result["restarted"] is False and result["applied"] is False
    assert result["apply_error"]["reason_code"] == "restart_unconfirmed"
    assert job["error"]["code"] == result["apply_error"]["code"] == "io"


def test_mode_set_restart_leaves_cleanup_pending_is_not_applied(executor, handler, port):
    def restart_dirty(mode):
        port.persisted = port.loaded_mode = mode
        port.core = dict(port.core, core_generation=11, cleanup_pending=True)
        return {"mode": mode, "changed": True, "restarted": True}
    port.set_mode = restart_dirty
    job = run(executor, "mode.set", {"mode": "tun"})
    assert job["state"] == "failed"
    assert job["result"]["restarted"] is True and job["result"]["applied"] is False


def test_mode_set_with_stopped_core_saves_and_applies_without_restart(executor, handler, port):
    port.core["running"] = False
    job = run(executor, "mode.set", {"mode": "tun"})
    assert job["state"] == "done"
    assert job["result"]["restarted"] is False and job["result"]["applied"] is True


def test_mode_set_saved_but_restart_failed_keeps_apply_error(executor, handler, port):
    def save_then_fail(mode):
        port.persisted = port.loaded_mode = mode
        port.core = dict(port.core, core_generation=11, running=False)
        raise OSError("restart failed with C:/private/path")
    port.set_mode = save_then_fail
    job = run(executor, "mode.set", {"mode": "tun"})
    assert job["state"] == "failed"
    assert job["result"]["saved"] is True and job["result"]["changed"] is True
    assert job["result"]["restarted"] is True and job["result"]["applied"] is False
    assert job["result"]["apply_error"]["code"] == "io"
    assert "private" not in wire_text(job)


def test_mode_set_admin_required_saves_nothing(executor, handler, port):
    def refuse(mode):
        raise PermissionError("TUN mode requires Administrator")
    port.set_mode = refuse
    job = run(executor, "mode.set", {"mode": "tun"})
    assert job["state"] == "failed" and job["error"]["code"] == "admin_required"
    assert job["result"] == {"mode": "tun", "changed": False, "saved": False,
                             "restarted": False, "applied": False, "apply_error": None}


def test_mode_set_permission_error_on_config_file_is_io(executor, handler, port):
    def locked(mode):
        raise PermissionError(13, "Access is denied", "C:/private/config.local.yaml")
    port.set_mode = locked
    job = run(executor, "mode.set", {"mode": "tun"})
    assert job["state"] == "failed" and job["error"]["code"] == "io"
    assert job["result"]["saved"] is False and "private" not in wire_text(job)


def test_mode_set_retry_after_unclean_restart_is_not_applied(executor, handler, port):
    """M1: the loaded config already holds tun while the old Core still runs."""
    port.restart_fails = True
    first = run(executor, "mode.set", {"mode": "tun"})
    assert first["state"] == "failed"
    assert first["result"]["saved"] is True and first["result"]["restarted"] is False
    assert first["result"]["apply_error"]["reason_code"] == "restart_unconfirmed"
    assert port.core["core_generation"] == 10 and port.core_mode == "system_proxy"
    retry = run(executor, "mode.set", {"mode": "tun"})
    assert retry["state"] == "failed" and retry["error"]["code"] == "io"
    assert retry["result"] == {
        "mode": "tun", "changed": False, "saved": True, "restarted": False, "applied": False,
        "apply_error": {"code": "io", "message_key": "errors.mode.restart_unconfirmed",
                        "params": {}, "reason_code": "restart_unconfirmed"}}


def test_mode_set_unchanged_with_cleanup_pending_is_not_applied(executor, handler, port):
    port.core["cleanup_pending"] = True
    job = run(executor, "mode.set", {"mode": "system"})
    assert job["state"] == "failed" and job["error"]["code"] == "io"
    assert job["result"]["saved"] is True and job["result"]["applied"] is False
    assert job["result"]["apply_error"]["reason_code"] == "restart_unconfirmed"


def test_mode_set_retry_after_core_generation_advanced_is_applied(executor, handler, port):
    port.restart_fails = True
    assert run(executor, "mode.set", {"mode": "tun"})["state"] == "failed"
    port.service_restart()  # a later restart under the loaded mode
    retry = run(executor, "mode.set", {"mode": "tun"})
    assert retry["state"] == "done"
    assert retry["result"]["applied"] is True and retry["result"]["changed"] is False
    # The record is cleared once applied; later unchanged requests stay done.
    assert run(executor, "mode.set", {"mode": "tun"})["state"] == "done"


def test_mode_set_prefers_runtime_core_mode_getter(executor, port):
    port.expose_core_mode = True
    port.loaded_mode = port.persisted = "tun"  # Core still runs system_proxy
    fresh = NodeCommands(port)  # no remembered failure in this bridge
    executor.register("mode.set", fresh.mode_set)
    job = run(executor, "mode.set", {"mode": "tun"})
    assert job["state"] == "failed" and job["result"]["applied"] is False
    port.core_mode = "tun"
    assert run(executor, "mode.set", {"mode": "tun"})["state"] == "done"


def test_mode_set_readback_failure_keeps_confirmed_restart(executor, handler, port):
    def switch_then_lose_config(mode):
        result = port._set_mode(mode)
        port.persisted_network_mode = lambda: (_ for _ in ()).throw(OSError("C:/private"))
        return result
    port.set_mode = switch_then_lose_config
    job = run(executor, "mode.set", {"mode": "tun"})
    assert job["state"] == "failed" and job["error"]["code"] == "io"
    result = job["result"]
    assert result["restarted"] is True
    assert result["saved"] is False and result["applied"] is False
    assert result["apply_error"]["reason_code"] == "readback_failed"
    assert "private" not in wire_text(job)


def test_mode_set_core_fact_failure_after_switch_is_io_with_result(executor, handler, port):
    calls = []
    original = port.core_fact
    def flaky():
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("controller gone")
        return original()
    port.core_fact = flaky
    job = run(executor, "mode.set", {"mode": "tun"})
    assert job["state"] == "failed" and job["error"]["code"] == "io"
    assert job["result"]["restarted"] is False and job["result"]["saved"] is False
    assert job["result"]["apply_error"]["reason_code"] == "readback_failed"


RESTART_UNCONFIRMED = {"code": "io", "message_key": "errors.mode.restart_unconfirmed",
                       "params": {}, "reason_code": "restart_unconfirmed"}


def assert_retry_not_applied(executor, port, mode="tun"):
    """The runtime already holds the mode while the pre-switch Core still runs."""
    assert port.loaded_mode == RUNTIME_MODE[mode]
    assert port.core["core_generation"] == 10 and port.core_mode == "system_proxy"
    retry = run(executor, "mode.set", {"mode": mode})
    assert retry["state"] == "failed" and retry["error"]["code"] == "io"
    assert retry["result"] == {"mode": mode, "changed": False, "saved": True,
                               "restarted": False, "applied": False,
                               "apply_error": RESTART_UNCONFIRMED}
    return retry


RUNTIME_MODE = {"tun": "tun", "system": "system_proxy", "mixed": "mixed"}


def test_mode_set_retry_after_readback_failure_is_not_applied(executor, handler, port):
    """Continued review MUST-1 path A: readback_failed exit records the target."""
    port.restart_fails = True
    original = port.persisted_network_mode
    calls = []
    def flaky():
        calls.append(1)
        if len(calls) == 2:  # the readback after set_network_mode
            raise OSError("C:/private/config.local.yaml locked")
        return original()
    port.persisted_network_mode = flaky
    first = run(executor, "mode.set", {"mode": "tun"})
    assert first["state"] == "failed"
    assert first["result"]["apply_error"]["reason_code"] == "readback_failed"
    assert_retry_not_applied(executor, port)


def test_mode_set_retry_after_core_fact_readback_failure_is_not_applied(executor, handler, port):
    """Path A with core_fact itself failing: the pre-switch generation is recorded."""
    port.restart_fails = True
    original = port.core_fact
    calls = []
    def flaky():
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("controller gone")
        return original()
    port.core_fact = flaky
    first = run(executor, "mode.set", {"mode": "tun"})
    assert first["result"]["apply_error"]["reason_code"] == "readback_failed"
    assert_retry_not_applied(executor, port)


def test_mode_set_retry_after_save_unconfirmed_is_not_applied(executor, handler, port):
    """Path B: changed but the persisted readback does not hold the mode."""
    port.restart_fails = True
    def reload_without_persist(mode):
        result = port._set_mode(mode)
        port.persisted = "system_proxy"
        return result
    port.set_mode = reload_without_persist
    first = run(executor, "mode.set", {"mode": "tun"})
    assert first["state"] == "failed" and first["result"]["saved"] is False
    assert first["result"]["apply_error"]["reason_code"] == "save_unconfirmed"
    port.persisted = "tun"  # the write lands later
    port.set_mode = port._set_mode
    assert_retry_not_applied(executor, port)


def test_mode_set_retry_after_unsaved_exception_is_not_applied(executor, handler, port):
    """Path C: set_network_mode reloaded the mode, then raised; nothing confirmed saved."""
    def reload_then_raise(mode):
        port.loaded_mode = mode
        raise OSError("restart blew up at C:/private")
    port.set_mode = reload_then_raise
    first = run(executor, "mode.set", {"mode": "tun"})
    assert first["state"] == "failed" and first["result"]["saved"] is False
    assert "private" not in wire_text(first)
    port.set_mode = port._set_mode
    assert_retry_not_applied(executor, port)


def test_mode_set_retry_after_readback_failure_with_real_restart_is_applied(executor, handler, port):
    """The record keeps the pre-switch generation: a Core started after the
    reload (generation advanced) is not mistaken for the previous-mode Core."""
    def switch_then_lose_config(mode):
        result = port._set_mode(mode)
        port.persisted_network_mode = lambda: (_ for _ in ()).throw(OSError("locked"))
        return result
    port.set_mode = switch_then_lose_config
    first = run(executor, "mode.set", {"mode": "tun"})
    assert first["state"] == "failed" and first["result"]["restarted"] is True
    port.persisted_network_mode = lambda: port.persisted
    port.set_mode = port._set_mode
    retry = run(executor, "mode.set", {"mode": "tun"})
    assert retry["state"] == "done" and retry["result"]["applied"] is True
    assert port.core["core_generation"] == 11 and port.core_mode == "tun"


def test_mode_set_retry_after_restart_with_pending_cleanup_resolves_once_clean(executor, handler, port):
    def restart_dirty(mode):
        port.persisted = port.loaded_mode = mode
        port.core = dict(port.core, core_generation=11, cleanup_pending=True)
        port.core_mode = mode
        return {"mode": mode, "changed": True, "restarted": True}
    port.set_mode = restart_dirty
    assert run(executor, "mode.set", {"mode": "tun"})["state"] == "failed"
    port.set_mode = port._set_mode
    port.core["cleanup_pending"] = False
    retry = run(executor, "mode.set", {"mode": "tun"})
    assert retry["state"] == "done" and retry["result"]["applied"] is True


def test_mode_set_core_not_restarted_branch(executor, handler, port):
    def no_restart_claim(mode):
        port.persisted = port.loaded_mode = mode
        return {"mode": mode, "changed": True, "restarted": False}  # Core was "not running"
    port.set_mode = no_restart_claim  # ...yet a Core runs afterwards (old generation)
    job = run(executor, "mode.set", {"mode": "mixed"})
    assert job["state"] == "failed" and job["error"]["code"] == "conflict"
    assert job["result"]["saved"] is True and job["result"]["applied"] is False
    assert job["result"]["apply_error"]["reason_code"] == "core_not_restarted"


def test_mode_set_unchanged_is_saved_and_applied(executor, handler, port):
    job = run(executor, "mode.set", {"mode": "system"})
    assert job["state"] == "done"
    assert job["result"] == {"mode": "system", "changed": False, "saved": True,
                             "restarted": False, "applied": True, "apply_error": None}


def test_mode_set_respects_network_guard(executor, port):
    class Gate:
        def require_idle(self):
            raise RuntimeError("startup recovery still running")
    session = SimpleNamespace(network_lock=threading.RLock(), report=SimpleNamespace(gate=Gate()))
    instance = NodeCommands(port, network_guard=node_commands._session_network_guard(session))
    executor.register("mode.set", instance.mode_set)
    job = run(executor, "mode.set", {"mode": "tun"})
    assert job["state"] == "failed" and job["error"]["code"] == "conflict"
    assert job["result"]["saved"] is False and not port.called("set_network_mode")


# ---------------------------------------------------------------------------
# nodes.update
# ---------------------------------------------------------------------------

def test_nodes_update_reuses_transaction_without_extra_reload(executor, handler, port):
    job = run(executor, "nodes.update", {
        "item_id": nid(port, "tier2", "Manual"), "updates": {"name": "Renamed"},
        "secrets": [{"field": "uri", "action": "set", "value": NEW_URI}]})
    assert job["state"] == "done"
    assert job["result"] == {"saved": True, "applied": True, "apply_error": None, "pending": None}
    [(_, item, updates)] = port.called("update_config_item")
    assert item["key"] == MANUAL_URI
    assert updates == {"name": "Renamed", "uri": NEW_URI}
    assert not port.called("reload_core_config")
    # Section 12: new values registered before the transaction, table replaced after.
    assert handler.secret_log[0][0] == "register"
    assert NEW_URI in handler.secret_log[0][1] and "BrandNewSecret987" in handler.secret_log[0][1]
    assert handler.secret_log[-1] == ("refresh",)
    assert NEW_URI not in wire_text(job)


@pytest.mark.parametrize("args", [
    {"updates": {"uri": NEW_URI}},
    {"updates": {"password": "x"}},
    {"updates": {"enabled": "yes"}},
    # T-P2-31b: {"enabled": False} on a node is now allowed (ConfigItem IDs
    # address disabled nodes; see tests/test_bridge_p2_31b.py).
    {"updates": {"name": "http://leak"}},
    {"updates": {}},
    {"updates": {}, "secrets": [{"field": "uri", "action": "clear"}]},
    {"updates": {}, "secrets": [{"field": "uri", "action": "keep"}]},
    {"updates": {}, "secrets": [{"field": "uuid", "action": "set", "value": "u"}]},
    {"updates": {}, "secrets": [{"field": "uri", "action": "set", "value": NEW_URI,
                                 "target_id": "0" * 24}]},
])
def test_nodes_update_rejects_non_whitelisted_input(executor, handler, port, args):
    job = run(executor, "nodes.update", {"item_id": nid(port, "tier2", "Manual"), **args})
    assert job["state"] == "failed" and job["error"]["code"] == "validation"
    assert job["result"]["saved"] is False and not port.called("update_config_item")
    assert not [entry for entry in handler.secret_log if entry[0] == "register"]


def test_nodes_update_tier1_and_subscription_shapes(executor, handler, port):
    job = run(executor, "nodes.update", {
        "item_id": nid(port, "tier1", "Edge :: VLESS"), "updates": {"port": 8443},
        "secrets": [{"field": "naive_link", "action": "clear"}]})
    assert job["state"] == "done"
    assert port.called("update_config_item")[-1][1]["key"] == "Edge"
    assert port.called("update_config_item")[-1][2] == {"port": 8443, "naive_link": ""}
    job = run(executor, "nodes.update", {
        "item_id": sid(port), "updates": {"enabled": False},
        "secrets": [{"field": "subscription_url", "action": "set", "value": SUB_URL + "-v2"}]})
    assert job["state"] == "done"
    assert port.called("update_config_item")[-1][2] == {"enabled": False, "url": SUB_URL + "-v2"}


def test_nodes_update_rolled_back_transaction_reports_unsaved(executor, handler, port):
    def rolled_back(item, updates):
        try:
            raise RuntimeError("strict core reload did not confirm")
        except RuntimeError as cause:
            raise RuntimeError("Node configuration update was rolled back") from cause
    port.update = rolled_back
    job = run(executor, "nodes.update", {"item_id": nid(port, "tier2", "Manual"),
                                         "updates": {"name": "Renamed"}})
    assert job["state"] == "failed" and job["error"]["code"] == "io"
    assert job["result"]["saved"] is False and job["result"]["applied"] is False
    assert job["result"]["apply_error"]["reason_code"] == "rolled_back"
    assert len(port.called("update_config_item")) == 1 and not port.called("reload_core_config")


def test_nodes_update_unconfirmed_rollback_keeps_saved_fact(executor, handler, port):
    def partially(item, updates):
        port.items[0] = dict(port.items[0], name="Renamed")
        try:
            raise RuntimeError("reload")
        except RuntimeError as cause:
            raise RuntimeError("rolled back") from cause
    port.update = partially
    job = run(executor, "nodes.update", {"item_id": nid(port, "tier2", "Manual"),
                                         "updates": {"name": "Renamed"}})
    assert job["state"] == "failed"
    assert job["result"]["saved"] is True and job["result"]["applied"] is False
    assert job["result"]["apply_error"]["reason_code"] == "rollback_unconfirmed"


def test_nodes_update_missing_config_entry_is_conflict(executor, handler, port):
    def vanished(item, updates):
        raise KeyError("tier2 node not found")
    port.update = vanished
    job = run(executor, "nodes.update", {"item_id": nid(port, "tier2", "Manual"),
                                         "updates": {"name": "Renamed"}})
    assert job["error"]["code"] == "conflict" and job["result"]["saved"] is False


# ---------------------------------------------------------------------------
# nodes.delete
# ---------------------------------------------------------------------------

def test_nodes_delete_applies_once_to_running_core(executor, handler, port):
    job = run(executor, "nodes.delete", {"item_id": nid(port, "tier2", "Manual")})
    assert job["state"] == "done"
    assert job["result"] == {"saved": True, "applied": True, "apply_error": None, "pending": None}
    [(_, kwargs)] = port.called("reload_core_config")
    assert kwargs["user_commit_id"].startswith("node-delete:")


def test_nodes_delete_with_stopped_core_needs_no_reload(executor, handler, port):
    port.core["running"] = False
    job = run(executor, "nodes.delete", {"item_id": nid(port, "tier2", "Manual")})
    assert job["state"] == "done" and not port.called("reload_core_config")


@pytest.mark.parametrize("reload", [
    lambda **kw: {"reloaded": False, "kept_existing_core": True},
    lambda **kw: (_ for _ in ()).throw(TimeoutError("controller")),
])
def test_nodes_delete_saved_but_apply_failed_is_reported(executor, handler, port, reload):
    port.reload = reload
    job = run(executor, "nodes.delete", {"item_id": nid(port, "tier2", "Manual")})
    assert job["state"] == "failed" and job["error"]["code"] == "io"
    assert job["result"]["saved"] is True and job["result"]["applied"] is False
    assert job["result"]["apply_error"]["code"] == "io"
    assert MANUAL_URI not in [row["key"] for row in port.items]


def test_nodes_delete_written_but_runtime_reload_raised(executor, handler, port):
    def write_then_fail(item):
        port._delete(item)
        raise RuntimeError("reload_config failed")
    port.delete = write_then_fail
    job = run(executor, "nodes.delete", {"item_id": nid(port, "tier2", "Manual")})
    assert job["state"] == "failed"
    assert job["result"]["saved"] is True and job["result"]["applied"] is False
    assert not port.called("reload_core_config")


def test_nodes_delete_not_found_is_conflict_and_unsaved(executor, handler, port):
    port.delete = lambda item: (_ for _ in ()).throw(KeyError("gone"))
    job = run(executor, "nodes.delete", {"item_id": nid(port, "tier2", "Manual")})
    assert job["error"]["code"] == "conflict" and job["result"]["saved"] is False


def test_nodes_delete_subscription_notifies_subscriptions_and_gateway(executor, handler, port):
    families = []
    handler.add_listener(families.append)
    job = run(executor, "nodes.delete", {"item_id": sid(port)})
    assert job["state"] == "done" and job["result"]["saved"] is True
    [(_, item)] = port.called("delete_config_item")
    assert item["kind"] == "subscription" and item["key"] == SUB_URL
    assert len(port.called("reload_core_config")) == 1
    assert families == ["nodes", "subscriptions", "gateway"]  # exit tier
    families.clear()
    run(executor, "nodes.update", {"item_id": nid(port, "tier2", "Manual"),
                                   "updates": {"name": "Renamed"}})
    assert families == ["nodes"]


@pytest.mark.parametrize("name, args", [
    ("nodes.update", lambda p: {"item_id": nid(p, "tier2", "Manual"), "updates": {"name": "R"}}),
    ("nodes.delete", lambda p: {"item_id": nid(p, "tier2", "Manual")}),
    ("subscriptions.refresh", lambda p: {"item_id": sid(p)}),
    ("gateway.select", lambda p: {"proxy_name": "Gateway B"}),
])
def test_mutations_wait_for_recovery_gate(executor, port, name, args):
    def pending():
        raise RuntimeError("startup recovery still running")
    instance = NodeCommands(port, recovery_gate=pending)
    executor.register(name, {"nodes.update": instance.nodes_update,
                             "nodes.delete": instance.nodes_delete,
                             "subscriptions.refresh": instance.subscriptions_refresh,
                             "gateway.select": instance.gateway_select}[name])
    job = run(executor, name, args(port))
    assert job["state"] == "failed" and job["error"]["code"] == "conflict"
    assert [call for call in port.calls if call[0] not in {"loaded_node_index"}] == []
    if name == "subscriptions.refresh":
        assert job["result"]["error"]["reason_code"] == "recovery_pending"
    elif name == "gateway.select":
        assert job["result"]["applied"] is False and job["result"]["selection"] is None
    else:
        assert job["result"]["saved"] is False


def test_secret_edit_registration_uses_canonical_walker(executor, handler, port):
    from bridge_import import draft_secret_values
    uri = "trojan://WalkerSecret42@walker.example.invalid:443?sni=x#W"
    job = run(executor, "nodes.update", {"item_id": nid(port, "tier2", "Manual"), "updates": {},
                                         "secrets": [{"field": "uri", "action": "set", "value": uri}]})
    assert job["state"] == "done"
    registered = handler.secret_log[0][1]
    assert set(draft_secret_values(uris=[uri])) <= set(registered)
    assert registered[uri] == "node" and "WalkerSecret42" in registered


# ---------------------------------------------------------------------------
# nodes.reveal
# ---------------------------------------------------------------------------

def test_reveal_is_private_owner_only_and_never_a_job(executor, handler, port):
    receipt = executor.submit("nodes.reveal", {"item_id": nid(port, "tier2", "Manual")},
                              req_id="request-a", owner="window-a")
    assert receipt.accepted
    response = receipt.wait(5)
    assert response.ok and response.outlet == "private" and response.owner == "window-a"
    assert response.data == {"item_id": nid(port, "tier2", "Manual"),
                             "fields": {"password": "ManualPass123", "uri": MANUAL_URI}}
    # Never a job: no jobs event, no job record for the op.
    assert not executor.recorder.pending and executor.get_job(receipt.op) is None
    # Only the matched node is detached (deep-copied).
    assert port.called("detach_node") == [("detach_node", "tier2", "Manual")]
    frame = dict(t="res", epoch="p2-14", produced_at_ms=0, sent_at_ms=0, id=1,
                 req_id="request-a", owner="window-a", cmd="nodes.reveal", ok=True,
                 data=response.data)
    assert not protocol.validate_frame(frame)
    assert "ManualPass123" not in repr(vars(handler))


def test_reveal_field_whitelist_and_shapes(executor, handler, port):
    response = ask(executor, "nodes.reveal", {"item_id": nid(port, "tier2", "Manual"),
                                              "fields": ["uri"]})
    assert response.data["fields"] == {"uri": MANUAL_URI}
    response = ask(executor, "nodes.reveal", {"item_id": nid(port, "tier2", "Wg")})
    assert response.data["fields"]["private_key"] == "WgPrivateKey="
    response = ask(executor, "nodes.reveal", {"item_id": nid(port, "exit", "Member")})
    assert response.data["fields"] == {"uuid": "MemberUuid-1", "uri": "vless://member"}
    response = ask(executor, "nodes.reveal", {"item_id": sid(port)})
    assert response.data["fields"] == {"subscription_url": SUB_URL}
    rejected = executor.submit("nodes.reveal", {"item_id": sid(port), "fields": ["method"]},
                               req_id="r", owner="window-a")
    assert not rejected.accepted and rejected.skipped_reason == "not_allowed"


def test_reveal_unknown_item_is_conflict(executor, handler):
    response = ask(executor, "nodes.reveal", {"item_id": "f" * 24})
    assert not response.ok and response.error["code"] == "conflict" and response.data is None


def test_reveal_owner_revoked_during_read_drops_result(executor, port):
    entered, release = threading.Event(), threading.Event()
    instance = NodeCommands(port)
    original = port.loaded_node_index
    def slow():
        entered.set()
        release.wait(5)
        return original()
    port.loaded_node_index = slow
    executor.register("nodes.reveal", instance.nodes_reveal)
    receipt = executor.submit("nodes.reveal", {"item_id": nid(port, "tier2", "Manual")},
                              req_id="r", owner="window-a")
    assert entered.wait(2)
    executor.revoke_owner("window-a")
    release.set()
    assert receipt.wait(2) is None
    assert executor.take_response(receipt.op, "window-a") is None
    assert not executor.recorder.pending
    later = executor.submit("nodes.reveal", {"item_id": sid(port)}, req_id="r2", owner="window-a")
    assert not later.accepted


def test_reveal_revoked_context_reads_no_secrets(port):
    instance = NodeCommands(port)
    port.loaded_node_index = lambda: pytest.fail("node read after revocation")
    port.detach_node = lambda *a: pytest.fail("secret read after revocation")
    context = SimpleNamespace(owner_revoked=threading.Event())
    context.owner_revoked.set()
    with pytest.raises(node_commands.JobFailure) as failure:
        instance.nodes_reveal({"item_id": nid(FakePort(), "tier2", "Manual")}, context)
    assert failure.value.code == "not_allowed"


# ---------------------------------------------------------------------------
# subscriptions.refresh
# ---------------------------------------------------------------------------

def test_refresh_success(executor, handler, port):
    job = run(executor, "subscriptions.refresh", {"item_id": sid(port)})
    assert job["state"] == "done"
    assert job["result"] == {"total": 3, "healthy": 2, "stored": True, "applied": True,
                             "retained_previous": False, "apply_error": None, "error": None}
    assert port.called("refresh_subscription")[0][1]["key"] == SUB_URL
    assert handler.bridge_refresh_activity()[sid(port)] == {"refreshing": False, "job_generation": 1}


def test_refresh_stored_but_not_applied_stays_distinct(executor, handler, port):
    port.refresh = lambda item: {"total": 4, "healthy": 1, "stored": True, "applied": False,
                                 "retained_previous": False, "core_running": True,
                                 "apply_error": f"reload failed for {SUB_URL}"}
    job = run(executor, "subscriptions.refresh", {"item_id": sid(port)})
    assert job["state"] == "failed" and job["error"]["code"] == "io"
    result = job["result"]
    assert result["stored"] is True and result["applied"] is False
    assert result["retained_previous"] is False and result["error"] is None
    assert result["apply_error"]["reason_code"] == "core_reload_failed"
    assert SUB_URL not in wire_text(job) and "private-token" not in wire_text(job)


def test_refresh_stored_with_stopped_core_is_done_and_applied(executor, handler, port):
    # Cross-ticket ruling 1: no Core means nothing to apply now; next start loads it.
    port.refresh = lambda item: {"total": 4, "healthy": 1, "stored": True, "applied": False,
                                 "retained_previous": False, "core_running": False,
                                 "apply_error": None}
    job = run(executor, "subscriptions.refresh", {"item_id": sid(port)})
    assert job["state"] == "done"
    assert job["result"] == {"total": 4, "healthy": 1, "stored": True, "applied": True,
                             "retained_previous": False, "apply_error": None, "error": None}


def test_refresh_with_stopped_core_and_error_is_not_applied(executor, handler, port):
    # A raw error excludes the stopped-Core shortcut even if stored is claimed.
    port.refresh = lambda item: {"total": 1, "healthy": 1, "stored": True, "applied": False,
                                 "retained_previous": False, "core_running": False,
                                 "error": f"HTTP 403 for {SUB_URL}"}
    job = run(executor, "subscriptions.refresh", {"item_id": sid(port)})
    assert job["state"] == "failed"
    result = job["result"]
    assert result["applied"] is False and result["error"]["reason_code"] == "refresh_failed"
    assert SUB_URL not in wire_text(job)


def test_refresh_with_stopped_core_but_not_stored_still_fails(executor, handler, port):
    port.refresh = lambda item: {"total": 0, "healthy": 0, "stored": False, "applied": False,
                                 "retained_previous": True, "core_running": False}
    job = run(executor, "subscriptions.refresh", {"item_id": sid(port)})
    assert job["state"] == "failed" and job["result"]["applied"] is False
    assert job["result"]["error"]["reason_code"] == "refresh_unconfirmed"


@pytest.mark.parametrize("raw_error, reason", [
    ("subscription refresh returned no nodes", "empty_subscription"),
    (f"HTTP 403 for {SUB_URL}", "refresh_failed"),
])
def test_refresh_failure_retains_previous(executor, handler, port, raw_error, reason):
    port.refresh = lambda item: {"total": 0, "healthy": 0, "stored": False, "applied": False,
                                 "retained_previous": True, "error": raw_error}
    job = run(executor, "subscriptions.refresh", {"item_id": sid(port)})
    assert job["state"] == "failed" and job["error"]["code"] == "io"
    assert job["result"]["retained_previous"] is True and job["result"]["stored"] is False
    assert job["result"]["error"]["reason_code"] == reason
    assert SUB_URL not in wire_text(job)


# T-P2-RT2 (决定_第二波裁定 RG #7): the busy getter is no longer consulted
# (check-then-call); the runtime's bounded acquire reports busy instead, see
# test_refresh_runtime_lock_busy_is_conflict_lifecycle_busy and
# tests/test_bridge_p2_rt2.py.
@pytest.mark.parametrize("state", ["stop_requested", "stopping"])
def test_refresh_lock_contention_reports_lifecycle_busy(executor, handler, port, state):
    if state == "stop_requested":
        port.stop_requested = True
    else:
        port.phase = "stopping"
    job = run(executor, "subscriptions.refresh", {"item_id": sid(port)})
    # Section 10 codes are closed: conflict on the wire, lifecycle_busy as reason.
    assert job["state"] == "failed" and job["error"]["code"] == "conflict"
    assert job["result"]["error"]["code"] == "conflict"
    assert job["result"]["error"]["reason_code"] == "lifecycle_busy"
    assert job["error"]["code"] in commands.COMMAND_SPECS["subscriptions.refresh"].errors
    assert job["result"]["stored"] is False and not port.called("refresh_subscription")


def test_refresh_runtime_lock_busy_is_conflict_lifecycle_busy(executor, handler, port):
    def busy(item):
        raise node_commands.RefreshLockBusy("lock")
    port.refresh = busy
    job = run(executor, "subscriptions.refresh", {"item_id": sid(port)})
    assert job["state"] == "failed" and job["error"]["code"] == "conflict"
    assert job["result"]["error"]["reason_code"] == "lifecycle_busy"
    assert job["result"]["stored"] is False and job["result"]["retained_previous"] is True


def test_adapter_maps_typed_runtime_busy_to_refresh_lock_busy():
    class Busy(RuntimeError):
        bridge_code = "lifecycle_busy"

    seen = []

    class Runtime(PublicOnlyRuntime):
        def refresh_subscription(self, item, *, acquire_timeout=None):
            seen.append(acquire_timeout)  # T-P2-RT2: bounded acquire is passed
            raise Busy("refresh lock held by stop")
    with pytest.raises(node_commands.RefreshLockBusy):
        RuntimeNodeCommandAdapter(Runtime()).refresh_subscription({})
    assert seen == [node_commands.REFRESH_ACQUIRE_TIMEOUT]


@pytest.mark.parametrize("exc, code", [
    (OSError("disk C:/private"), "io"),
    (TimeoutError("fetch"), "timeout"),
    (ValueError("subscription item is required"), "conflict"),
])
def test_refresh_runtime_exception_is_refresh_rejected(executor, handler, port, exc, code):
    def raise_(item):
        raise exc
    port.refresh = raise_
    job = run(executor, "subscriptions.refresh", {"item_id": sid(port)})
    assert job["state"] == "failed" and job["error"]["code"] == code
    assert job["result"]["error"]["reason_code"] == "refresh_rejected"
    assert job["result"]["stored"] is False and "private" not in wire_text(job)
    assert handler.bridge_refresh_activity()[sid(port)]["refreshing"] is False


def test_unregistered_business_code_cannot_be_built():
    with pytest.raises(AssertionError):
        node_commands._error("lifecycle_busy", "subscriptions", "lifecycle_busy")
    with pytest.raises(AssertionError):
        node_commands._error("admin_required", "nodes")
    assert node_commands._error("admin_required", "mode")["code"] == "admin_required"


def test_refresh_of_same_item_in_flight_is_conflict(port):
    instance = NodeCommands(port)
    entered, release = threading.Event(), threading.Event()
    port.refresh = lambda item: (entered.set(), release.wait(5),
                                 {"total": 1, "healthy": 1, "stored": True, "applied": True,
                                  "retained_previous": False})[-1]
    first = []
    worker = threading.Thread(target=lambda: first.append(
        instance.subscriptions_refresh({"item_id": sid(port)}, None)))
    worker.start()
    assert entered.wait(2)
    assert instance.bridge_refresh_activity()[sid(port)]["refreshing"] is True
    with pytest.raises(node_commands.JobFailure) as failure:
        instance.subscriptions_refresh({"item_id": sid(port)}, None)
    release.set()
    worker.join(2)
    second = failure.value.partial_result
    assert failure.value.code == "conflict"
    assert second["error"]["reason_code"] == "refresh_in_progress" and second["stored"] is False
    assert first[0]["applied"] is True
    assert len(port.called("refresh_subscription")) == 1


def test_refresh_rejects_node_ids(executor, handler, port):
    job = run(executor, "subscriptions.refresh", {"item_id": nid(port, "tier2", "Manual")})
    assert job["error"]["code"] == "conflict" and not port.called("refresh_subscription")


def test_refresh_owner_snapshot_is_unknown_until_runtime_getter(port):
    instance = NodeCommands(port)
    assert instance.refresh_owner_snapshot() is None
    port.refresh_state = {"generation": 7, "stale": False,
                          "items": [{"id": sid(port), "refreshing": False}]}
    assert instance.refresh_owner_snapshot() is None  # no job_generation: incomplete
    port.refresh_state = {"generation": 7, "stale": False, "items": [
        {"id": sid(port), "refreshing": True, "job_generation": 2,
         "membership_version": 1, "member_ids": [], "check_receipt": None}]}
    snapshot = instance.refresh_owner_snapshot()
    assert snapshot["items"][0]["refreshing"] is True
    with pytest.raises(TypeError):
        snapshot["generation"] = 9
    from bridge_nodes import NodeProjection
    sub = {"id": sid(port), "tier": "gateway", "source_id": "a" * 24, "enabled": True}
    rows = NodeProjection()._refresh_facts([sub], 7, snapshot)
    assert rows[sid(port)]["job_generation"] == 2


# ---------------------------------------------------------------------------
# gateway.select
# ---------------------------------------------------------------------------

def test_gateway_select_applied_with_actual_selection(executor, handler, port):
    job = run(executor, "gateway.select", {"proxy_name": "Gateway B"})
    assert job["state"] == "done"
    assert job["result"] == {"selected": "Gateway B", "applied": True, "apply_error": None,
                             "selection": "Gateway B", "pending": None}


@pytest.mark.parametrize("raw, reason", [
    ({"applied": False, "core_running": True,
      "apply_error": "exit selection was superseded by newer user intent"}, "superseded"),
    ({"applied": False, "core_running": False,
      "apply_error": "exit selection was superseded by newer user intent"}, "superseded"),
    ({"applied": False, "core_running": True, "apply_error": "reload not confirmed"}, "reload_unconfirmed"),
    ({"applied": True, "core_running": True}, "selection_unconfirmed"),
])
def test_gateway_select_not_applied_never_pretends(executor, handler, port, raw, reason):
    port.select = lambda name: dict(raw, selected=name)
    job = run(executor, "gateway.select", {"proxy_name": "Gateway B"})
    assert job["state"] == "failed"
    result = job["result"]
    assert result["selected"] == "Gateway B" and result["applied"] is False
    assert result["selection"] is None
    assert result["apply_error"]["reason_code"] == reason


@pytest.mark.parametrize("saved", ["Gateway B", None, OSError("locked")])
def test_gateway_select_with_stopped_core_is_done_and_applied(executor, handler, port, saved):
    # Saved + runtime config reloaded, no Core: effective at next start.
    port.saved_exit = saved
    port.select = lambda name: {"selected": name, "applied": False, "core_running": False}
    job = run(executor, "gateway.select", {"proxy_name": "Gateway B"})
    assert job["state"] == "done"
    assert job["result"] == {"selected": "Gateway B", "applied": True, "apply_error": None,
                             "selection": "Gateway B", "pending": None}


def test_gateway_select_with_stopped_core_and_other_saved_name_is_unconfirmed(executor, handler, port):
    port.saved_exit = "Gateway A"
    port.select = lambda name: {"selected": name, "applied": False, "core_running": False}
    job = run(executor, "gateway.select", {"proxy_name": "Gateway B"})
    assert job["state"] == "failed" and job["error"]["code"] == "io"
    assert job["result"]["applied"] is False and job["result"]["selection"] is None
    assert job["result"]["apply_error"]["reason_code"] == "selection_unconfirmed"


def test_core_stopped_every_mutation_is_done_and_applied(executor, handler, port):
    """Cross-ticket ruling 1: with Core stopped a successful save is done, applied=true."""
    port.core = dict(port.core, running=False)

    def select_stopped(name):
        port.saved_exit = name
        return {"selected": name, "applied": False, "core_running": False}

    port.select = select_stopped
    port.refresh = lambda item: {"total": 2, "healthy": 2, "stored": True, "applied": False,
                                 "retained_previous": False, "core_running": False,
                                 "apply_error": None}
    cases = [
        ("mode.set", {"mode": "tun"}),
        ("nodes.update", {"item_id": nid(port, "tier2", "Manual"), "updates": {"name": "Renamed"}}),
        ("subscriptions.refresh", {"item_id": sid(port)}),
        ("gateway.select", {"proxy_name": "Gateway B"}),
        ("nodes.delete", {"item_id": nid(port, "tier2", "Manual")}),
    ]
    for name, args in cases:
        job = run(executor, name, args)
        assert job["state"] == "done", (name, job)
        assert job["result"]["applied"] is True and job["result"]["apply_error"] is None, name
    assert not port.called("reload_core_config")


def test_gateway_select_exception_and_validation(executor, handler, port):
    port.select = lambda name: (_ for _ in ()).throw(ConnectionError("controller down"))
    job = run(executor, "gateway.select", {"proxy_name": "Gateway B"})
    assert job["state"] == "failed" and job["result"]["apply_error"]["code"] == "io"
    job = run(executor, "gateway.select", {"proxy_name": " padded"})
    assert job["error"]["code"] == "validation"
    assert len(port.called("set_exit_selected_proxy")) == 1


def test_gateway_select_runtime_value_error_after_write_is_io(executor, handler, port):
    def written_then_reload_rejected(name):
        raise ValueError("reload_config rejected C:/private/config.local.yaml")
    port.select = written_then_reload_rejected
    job = run(executor, "gateway.select", {"proxy_name": "Gateway B"})
    assert job["state"] == "failed" and job["error"]["code"] == "io"
    assert job["result"]["apply_error"]["reason_code"] == "apply_failed"
    assert job["result"]["applied"] is False and "private" not in wire_text(job)


def test_gateway_select_prefers_side_effect_free_readback(executor, handler, port):
    port.readback = "Gateway B"
    job = run(executor, "gateway.select", {"proxy_name": "Gateway B"})
    assert job["state"] == "done" and job["result"]["selection"] == "Gateway B"
    port.readback = "Gateway A"  # Mihomo accepted the PUT but reads back another member
    job = run(executor, "gateway.select", {"proxy_name": "Gateway B"})
    assert job["state"] == "failed"
    assert job["result"]["selection"] == "Gateway A" and job["result"]["applied"] is False
    assert job["result"]["apply_error"]["reason_code"] == "selection_unconfirmed"


# ---------------------------------------------------------------------------
# Adapter and register()
# ---------------------------------------------------------------------------

class PublicOnlyRuntime:
    """Fails on any private attribute access from the adapter."""

    def __init__(self):
        self.manager = SimpleNamespace(lock=threading.RLock(), runtime_generation=5,
            runtime_nodes={"tier2": [node("Manual", "tier2", uri=MANUAL_URI)]})
        self.config = {"tiers": {"exit": {"subscription_urls": [SUB_URL, {"url": "", "enabled": False}]}}}
        self.network_mode = "system_proxy"

    def __getattribute__(self, name):
        if name.startswith("_") and not name.startswith("__"):
            raise AssertionError(f"private runtime access: {name}")
        return object.__getattribute__(self, name)

    def lifecycle_status(self):
        return {"intent": {"runtime_generation": 5}}

    def read_local_config(self):
        return {"client": {"network_mode": "tun"}}

    def configured_items(self):
        return []

    def reload_core_config(self, **kwargs):
        return dict(kwargs)


def test_adapter_uses_public_surfaces_and_unknown_new_getters():
    runtime = PublicOnlyRuntime()
    adapter = RuntimeNodeCommandAdapter(runtime)
    live = runtime.manager.runtime_nodes["tier2"][0]
    generation, index = adapter.loaded_node_index()
    assert generation == 5 and index == (("tier2", node_commands.node_identity(live), live),)
    detached = adapter.detach_node(5, "tier2", live)
    assert detached is not live and detached.uri == live.uri
    with pytest.raises(StaleItemFacts):
        adapter.detach_node(5, "tier2", node("Other", "tier2"))  # left the tier
    assert adapter.subscription_entries() == (("exit", 0, SUB_URL), ("exit", 1, ""))
    assert adapter.persisted_network_mode() == "tun"
    assert adapter.subscription_refresh_state() is None
    assert adapter.subscription_refresh_busy() is None
    assert adapter.core_loaded_network_mode() is None
    assert adapter.exit_selection_readback() is None
    assert adapter.persisted_exit_selection() == ""
    runtime.read_local_config = lambda: {"tiers": {"exit": {"selected_proxy": " Legacy "}}}
    assert adapter.persisted_exit_selection() == "Legacy"
    runtime.read_local_config = lambda: {"tiers": {"exit": {"selected": "G", "selected_proxy": "L"}}}
    assert adapter.persisted_exit_selection() == "G"
    from runtime_reload_gate import RuntimeReloadReason
    reload = adapter.reload_core_config(reason=RuntimeReloadReason.USER_GRAPH_CONFIG,
                                        user_commit_id="node-delete:1")
    assert reload["mode"] == "strict" and reload["reason"].value == "user_graph_config"
    runtime.manager.runtime_generation = 6
    with pytest.raises(StaleItemFacts):
        adapter.loaded_node_index()
    with pytest.raises(StaleItemFacts):
        adapter.detach_node(5, "tier2", live)


def test_adapter_forwards_future_runtime_getter():
    class Future(PublicOnlyRuntime):
        def bridge_subscription_refresh_state(self):
            return {"generation": 5, "stale": False, "items": []}

        def bridge_subscription_refresh_busy(self):
            return False

        def bridge_core_network_mode(self):
            return "tun"

        def bridge_exit_selection_readback(self):
            return "Gateway B"
    adapter = RuntimeNodeCommandAdapter(Future())
    assert adapter.subscription_refresh_state()["generation"] == 5
    assert adapter.subscription_refresh_busy() is False
    assert adapter.core_loaded_network_mode() == "tun"
    assert adapter.exit_selection_readback() == "Gateway B"


def test_register_uses_contract_specs_and_session_hooks(port):
    executor = JobExecutor(JobEventRecorder(8192), epoch="p2-14-reg", require_acceptance=True)
    services, refreshed, tables = [], [], []
    logs = SimpleNamespace(refresh_known_secret_values=lambda values: tables.append(dict(values)))
    session = SimpleNamespace(
        executor=executor, runtime=object(), network_lock=threading.RLock(),
        report=SimpleNamespace(gate=SimpleNamespace(require_idle=lambda: None)),
        access=SimpleNamespace(known_secret_values=lambda: {"old-secret": "node"}),
        logs=logs, refresh_secrets=lambda: refreshed.append(True),
        record_service=lambda: services.append(True))
    try:
        instance = node_commands.register(session, port=port)
        for name in node_commands.COMMANDS:
            # Public evidence of registration: a second register is refused.
            with pytest.raises(ValueError):
                executor.register(name, lambda args, context: None)
        assert {name: (commands.COMMAND_SPECS[name].domain, commands.COMMAND_SPECS[name].reply,
                       commands.COMMAND_SPECS[name].cancellation)
                for name in node_commands.COMMANDS} == {
            "mode.set": ("mutation", "job", "queued"),
            "nodes.update": ("mutation", "job", "queued"),
            "nodes.delete": ("mutation", "job", "queued"),
            "nodes.reveal": ("normal", "private", "owner"),
            "subscriptions.refresh": ("mutation", "job", "queued"),
            "gateway.select": ("mutation", "job", "queued")}
        reveal = executor.submit("nodes.reveal", {"item_id": sid(port)}, req_id="v", owner="window-a")
        assert reveal.accepted and reveal.wait(5).outlet == "private"
        job = run(executor, "mode.set", {"mode": "tun"})
        assert job["state"] == "done" and services and refreshed
        job = run(executor, "nodes.update", {"item_id": nid(port, "tier2", "Manual"),
            "updates": {}, "secrets": [{"field": "uri", "action": "set", "value": NEW_URI}]})
        assert job["state"] == "done"
        assert tables[-1]["old-secret"] == "node" and NEW_URI in tables[-1]
        # Queued mutation is cancellable before start; running one is not.
        ack = executor.submit("gateway.select", {"proxy_name": "G"}, req_id="q", owner="window-a")
        assert executor.cancel(ack.op, owner="window-a").accepted
        assert executor.wait(ack.op, 2)["state"] == "cancelled"
        assert not port.called("set_exit_selected_proxy")
        assert isinstance(instance, NodeCommands)
    finally:
        executor.shutdown(time.monotonic() + 1)


def test_new_files_are_lf_utf8():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for path in (root / "bridge_node_commands.py", Path(__file__)):
        data = path.read_bytes()
        data.decode("utf-8")
        assert b"\r\n" not in data
