"""T-P2-RG runtime getters on real DesktopRuntime shapes.

Every getter runs on ``minimal_runtime()`` (or the P2-04 real-shape fixture)
with the real DesktopRuntime methods; only Manager/AlertManager stand-ins,
never a Mihomo client: the controller is a ForbiddenAPI and route refresh
fails the test. No app start, registry, firewall or network access.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import inspect
import json
import threading
import time
from types import SimpleNamespace

import pytest

import desktop_runtime as dr
from bridge_diagnostics import RuntimeDiagnosticPort, TargetUnavailable
from bridge_exit import RuntimeExitPort
from bridge_node_commands import NodeCommands, RuntimeNodeCommandAdapter, node_item_id
from bridge_nodes import NodeProjection
from bridge_runtime_access import RuntimeAccess, config_item_id, opaque_id
from desktop_runtime import DesktopRuntime
from models import Node, NodeStatus
from node_diagnostic import diagnostic_node_identity
from runtime_egress_recovery import CoreLoadedGroupState, CoreLoadedRouteSnapshot, RuntimeNodeRef
from runtime_listener_preflight import (RuntimeListenerPreflightResult,
                                        RuntimeListenerPreflightState)
from runtime_route_audit import inspect_runtime_route
from subscription_operations import SubscriptionOperationCoordinator
from tests.test_bridge_p2_04 import ForbiddenAPI, TrackingLock
from tests.test_runtime_lifecycle_status import minimal_runtime


SUB = "https://example.invalid/private-sub"
SUB_OFF = "https://example.invalid/disabled-sub"
NOW_MS = time.time_ns() // 1_000_000
LAUNCH_MS = NOW_MS - 600_000
PAYLOAD = {"proxies": {"proxies": {
    "FINAL-EXIT": {"type": "Selector", "now": "EXIT-SELECT", "all": ["EXIT-SELECT", "PROXY-FALLBACK"]},
    "EXIT-SELECT": {"type": "Selector", "now": "Gateway A", "all": ["Gateway A", "Gateway B"]},
    "Gateway A": {"type": "Trojan"}, "Gateway B": {"type": "Trojan"}}}}


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def gateway_node(name="Gateway A", *, source=SUB, address="gw.example.invalid", extra=None):
    return Node(name=name, protocol="trojan", tier="exit", address=address, port=443,
                password="LoadedPasswordOnly", source=source, status=NodeStatus.ALIVE,
                latency_ms=12, sources=[source] if source else [], extra=dict(extra or {}))


class Manager:
    """Manager stand-in exposing exactly the members the getters read."""

    def __init__(self, config, generation=2):
        self.config = config
        self.runtime_generation = generation
        self.lock = TrackingLock()
        self.runtime_nodes = {tier: [] for tier in ("tier1", "tier2", "tier3", "exit")}
        self.runtime_nodes["exit"] = [gateway_node()]
        self.evidence = {"tier2": {}, "exit": {}}

    def subscription_source_check_snapshot(self, tier):
        with self.lock:
            return deepcopy(self.evidence[tier])


class Runner:
    def __init__(self):
        self.fact = {"running": True, "pid": 42, "process_generation": 3, "cleanup_pending": False}

    def lightweight_fact(self):
        return dict(self.fact)

    def status(self):
        return {"running": self.fact["running"], "pid": self.fact["pid"]}

    def is_running(self):
        return self.fact["running"]


def build_runtime():
    """minimal_runtime() plus the config/manager owners the P2 getters read."""
    rt = minimal_runtime()
    rt._config_change_lock = threading.RLock()
    rt._runtime_lifecycle_lock = threading.RLock()
    rt.config = {
        "client": {"network_mode": "system_proxy", "mihomo_mixed_port": 7890,
                   "system_proxy": {"host": "127.0.0.1", "port": 7890}},
        "tiers": {"tier1": {}, "tier2": {"subscription_urls": [{"url": SUB_OFF, "enabled": False}]},
                  "tier3": {}, "exit": {"subscription_urls": [SUB]}},
        "alerts": {"desktop": {"enabled": False, "disabled_in_native_ui": True},
                   "telegram": {"enabled": True, "bot_token": "TelegramTokenOnly", "chat_id": "42"},
                   "channels": [{"id": "ops", "type": "generic_webhook", "label": "Ops",
                                 "config": {"url": "https://hook.example.invalid/private"}}]},
        "ui": {"language": "en_US"}}
    rt.manager = Manager(rt.config)
    rt.local = {"alerts": {"desktop": {"enabled": True}}}
    rt.config_store = SimpleNamespace(read_sources=lambda: {}, read_local=lambda: deepcopy(rt.local))
    rt.read_local_config = lambda: deepcopy(rt.local)
    rt.core_runner = Runner()
    rt.network_mode = "system_proxy"
    rt.subscription_operation_coordinator = SubscriptionOperationCoordinator()
    rt._bridge_core_launch_fact = (7, "system_proxy", LAUNCH_MS)
    rt._core_loaded_network_mode = "system_proxy"  # T-P2-RT2 Core-loaded mode
    rt.api_client = ForbiddenAPI()
    rt.mihomo_proxies = lambda *a, **k: pytest.fail("getter read GET /proxies")
    rt._refresh_runtime_route_snapshot_serialized = lambda *a, **k: pytest.fail("route refresh")
    rt._queue_runtime_route_snapshot_refresh = lambda *a, **k: pytest.fail("route refresh queued")
    rt._runtime_confirmed_fail_closed_snapshot_key = None
    rt._runtime_route_snapshot = CoreLoadedRouteSnapshot(
        process_pid=42, process_generation=3, core_generation=7, semantic_digest="a" * 64,
        route_epoch=9, network_context_epoch=5,
        group_states=(CoreLoadedGroupState("FINAL-EXIT", "Selector", ("EXIT-SELECT",), "EXIT-SELECT"),
                      CoreLoadedGroupState("EXIT-SELECT", "Selector", ("Gateway A", "Gateway B"), "Gateway A")),
        proxy_nodes=(RuntimeNodeRef("exit", "gateway", "opaque", "Gateway A"),),
        loaded_at_monotonic=time.monotonic() - 2.0)
    rt._runtime_route_snapshot_payload = deepcopy(PAYLOAD)
    return rt


def select_happened(rt):
    """What select_mihomo_proxy does to the publication after its PUT."""
    with rt._runtime_route_snapshot_lock:
        rt._runtime_route_epoch += 1
        rt._runtime_route_snapshot = None
        rt._runtime_route_snapshot_payload = None


# --------------------------------------------------------------------------
# bridge_core_network_mode (P2-14 core_loaded_network_mode)
# --------------------------------------------------------------------------

def test_core_network_mode_is_the_mode_the_core_actually_loaded():
    # T-P2-RT2 (Fallback 10-06): the getter reads _core_loaded_network_mode,
    # written at the cold start and at every successful staged hot reload.
    rt = build_runtime()
    assert rt.bridge_core_network_mode() == "system_proxy"
    # A failed restart to TUN reloads config but leaves generation 7 running
    # with the old graph: nothing new was loaded yet.
    rt.network_mode = "tun"
    assert rt.bridge_core_network_mode() == "system_proxy"
    adapter = RuntimeNodeCommandAdapter(rt)
    assert adapter.core_loaded_network_mode() == "system_proxy"
    # A same-generation hot reload that rendered TUN is reported at once.
    rt._core_loaded_network_mode = "tun"
    assert rt._core_generation == 7 and adapter.core_loaded_network_mode() == "tun"
    # No record (e.g. a bootstrap Core not yet promoted) is unknown.
    rt._core_loaded_network_mode = None
    assert rt.bridge_core_network_mode() is None


@pytest.mark.parametrize("change", ["stopped", "cleanup", "owner"])
def test_core_network_mode_unknown_without_a_clean_current_core(change):
    rt = build_runtime()
    if change == "stopped":
        rt.core_runner.fact["running"] = False
    elif change == "cleanup":
        rt.core_runner.fact["cleanup_pending"] = True
    else:
        rt.manager.runtime_generation = 3  # rebuilt manager of another generation
    assert rt.bridge_core_network_mode() is None


def test_launch_mode_is_recorded_at_both_core_generation_bumps():
    for method in (DesktopRuntime._start_core_once, DesktopRuntime._ensure_bootstrap_core_proxy):
        source = inspect.getsource(method)
        bump = source.index("self._core_generation += 1")
        assert "self._bridge_core_launch_fact = (int(self._core_generation)" in source[bump:bump + 300]


# --------------------------------------------------------------------------
# EXIT-SELECT readback and exit binding (no Mihomo read)
# --------------------------------------------------------------------------

def test_exit_selection_readback_comes_from_the_current_publication_only():
    rt = build_runtime()
    assert rt.bridge_exit_selection_readback() == "Gateway A"
    assert RuntimeNodeCommandAdapter(rt).exit_selection_readback() == "Gateway A"
    select_happened(rt)
    assert rt.bridge_exit_selection_readback() is None  # unknown, not the request


@pytest.mark.parametrize("change", ["epoch", "core_generation", "pid", "stopped", "cleanup"])
def test_publication_of_another_core_or_epoch_is_not_a_readback(change):
    rt = build_runtime()
    if change == "epoch":
        rt._runtime_route_epoch += 1
    elif change == "core_generation":
        rt._core_generation = 8
    elif change == "pid":
        rt.core_runner.fact["pid"] = 43
    elif change == "stopped":
        rt.core_runner.fact["running"] = False
    else:
        rt.core_runner.fact["cleanup_pending"] = True
    assert rt.bridge_exit_selection_readback() is None
    assert rt.bridge_exit_binding() is None


def test_exit_binding_equals_the_qt_route_context_marker_without_get_proxies():
    # T-P2-RT2: no module-import-time clock (NOW_MS made this order dependent
    # in a long single-process run); the publication is stamped right here.
    before_ms = time.time_ns() // 1_000_000
    rt = build_runtime()  # publishes a snapshot loaded 2 s before now
    binding = rt.bridge_exit_binding()
    after_ms = time.time_ns() // 1_000_000
    assert binding["confirmed"] is True
    assert binding["core_instance"] == "2:7:42"
    assert before_ms - 2_000 - 1_000 <= binding["checked_at_ms"] <= after_ms - 2_000 + 1_000
    # Qt's current_route_context computes the same marker from GET /proxies.
    rt._exit_quality_route_lock = threading.Lock()
    rt._exit_quality_route_cache, rt._exit_quality_route_cache_at = {}, 0.0
    rt._exit_quality_route_cache_route_epoch = -1
    rt.mihomo_proxies = lambda *a, **k: deepcopy(PAYLOAD["proxies"])
    route = rt.current_route_context(force=True)
    assert route["binding_marker"] == binding["binding_marker"]
    assert route["core_instance"] == binding["core_instance"]
    assert route["confirmed"] is True
    rt.mihomo_proxies = lambda *a, **k: pytest.fail("getter read GET /proxies")
    port = RuntimeExitPort(rt)
    # checked_at_ms is re-derived per call (wall clock minus age): compare the
    # rest exactly and that field within a tolerance (no millisecond race).
    via_port = dict(port.exit_binding())
    assert abs(via_port.pop("checked_at_ms") - binding["checked_at_ms"]) <= 1_000
    assert via_port == {key: value for key, value in dict(binding).items()
                        if key != "checked_at_ms"}


def test_exit_binding_is_unknown_for_a_broken_chain_and_after_a_selection():
    # T-P2-RT2 (Fallback 10-06): a published chain without a managed leaf is
    # unknown (None), so an automatic lookup skips; never a GET /proxies.
    rt = build_runtime()
    broken = deepcopy(PAYLOAD)
    broken["proxies"]["proxies"]["EXIT-SELECT"]["now"] = ""
    rt._runtime_route_snapshot_payload = broken
    assert rt.bridge_exit_binding() is None
    rt._runtime_route_snapshot_payload = deepcopy(PAYLOAD)
    assert rt.bridge_exit_binding()["confirmed"] is True
    select_happened(rt)
    assert rt.bridge_exit_binding() is None


def test_exit_proxy_url_follows_qt_host_rules():
    rt = build_runtime()
    rt.system_proxy = SimpleNamespace(host="0.0.0.0", port=7891)
    assert rt.bridge_exit_proxy_url() == "http://127.0.0.1:7891"
    assert RuntimeExitPort(rt).proxy_url() == "http://127.0.0.1:7891"
    rt.system_proxy = SimpleNamespace(host="::1", port=None)
    assert rt.bridge_exit_proxy_url() == "http://[::1]:7890"
    rt.core_runner.fact["running"] = False
    assert rt.bridge_exit_proxy_url() is None


def test_exit_admission_phase_reads_the_qt_deferral_fields():
    rt = build_runtime()
    rt._core_runtime_phase = "formal_no_capture"
    rt._tier3_egress_activation_pending = True
    assert dict(rt.bridge_exit_admission_phase()) == {
        "core_runtime_phase": "formal_no_capture", "initial_admission_active": True, "stale": False}
    rt._tier3_egress_activation_pending = False
    assert rt.bridge_exit_admission_phase()["initial_admission_active"] is False


# --------------------------------------------------------------------------
# Subscription refresh owner (P2-14 / P2-06)
# --------------------------------------------------------------------------

def test_refresh_state_covers_every_subscription_and_projection_members():
    rt = build_runtime()
    state = rt.bridge_subscription_refresh_state()
    records = rt.bridge_subscription_records()
    nodes = rt.bridge_node_records()
    assert state["generation"] == 2 and state["stale"] is False
    assert [row["id"] for row in state["items"]] == [row["id"] for row in records["items"]]
    assert len(state["items"]) == 2  # the disabled tier2 subscription is covered too
    by_id = {row["id"]: row for row in state["items"]}
    gateway = next(row for row in records["items"] if row["tier"] == "gateway")
    assert by_id[gateway["id"]]["member_ids"] == (nodes["items"][0]["id"],)
    assert all(row["refreshing"] is False and row["job_generation"] == 0
               and row["membership_version"] == 0 and row["check_receipt"] is None
               for row in state["items"])
    snapshot = NodeCommands(RuntimeNodeCommandAdapter(rt)).refresh_owner_snapshot()
    assert snapshot is not None and list(snapshot["items"]) == state["items"]


def test_refreshing_follows_the_shared_coordinator_and_advances_the_job():
    rt = build_runtime()
    sub_id = opaque_id("exit", 0, SUB)
    rows = lambda: {row["id"]: row for row in rt.bridge_subscription_refresh_state()["items"]}
    assert rows()[sub_id]["job_generation"] == 0
    with rt.subscription_operation_coordinator.system_scope("exit", [SUB]):
        during = rows()[sub_id]
        assert during["refreshing"] is True and during["job_generation"] == 1
        assert rows()[sub_id]["job_generation"] == 1  # same job while in flight
    after = rows()[sub_id]
    assert after["refreshing"] is False and after["job_generation"] == 1


def test_runtime_refresh_entry_counts_a_job_even_when_never_seen_in_flight():
    rt = build_runtime()
    sub_id = opaque_id("exit", 0, SUB)
    rt.bridge_subscription_refresh_state()
    rt._bridge_note_subscription_refresh("exit", " https://EXAMPLE.invalid/private-sub")
    row = {r["id"]: r for r in rt.bridge_subscription_refresh_state()["items"]}[sub_id]
    assert row["job_generation"] == 1 and row["refreshing"] is False
    for method in (DesktopRuntime.refresh_subscription,
                   DesktopRuntime._import_subscription_with_initial_refresh):
        assert "self._bridge_note_subscription_refresh(tier, url)" in inspect.getsource(method)


def receipt_of(rt):
    sub_id = opaque_id("exit", 0, SUB)
    record = {row["id"]: row for row in rt.bridge_subscription_records()["items"]}[sub_id]
    state = {row["id"]: row for row in rt.bridge_subscription_refresh_state()["items"]}[sub_id]
    assert record["check_receipt"] == state["check_receipt"]
    assert record["membership_version"] == state["membership_version"]
    return state["check_receipt"]


def test_receipt_needs_this_generation_evidence_after_the_core_launch():
    rt = build_runtime()
    rt.manager.evidence["exit"][SUB] = {"runtime_generation": 2, "checked_at": iso(LAUNCH_MS - 1000)}
    assert receipt_of(rt) is None  # checked before this Core was launched
    rt.manager.evidence["exit"][SUB] = {"runtime_generation": 1, "checked_at": iso(LAUNCH_MS + 1000)}
    assert receipt_of(rt) is None  # another runtime generation
    rt.manager.evidence["exit"][SUB] = {"runtime_generation": 2, "checked_at": iso(LAUNCH_MS + 1000)}
    receipt = receipt_of(rt)
    nodes = rt.bridge_node_records()
    assert receipt == {"runtime_generation": 2, "core_generation": 7, "core_pid": 42,
                       "membership_version": 0, "member_ids": (nodes["items"][0]["id"],),
                       "refresh_job_generation": 0, "checked_at_ms": LAUNCH_MS + 1000}


def test_owner_events_require_newer_evidence_never_relabel_old_checks():
    rt = build_runtime()
    rt.manager.evidence["exit"][SUB] = {"runtime_generation": 2, "checked_at": iso(LAUNCH_MS + 1000)}
    assert receipt_of(rt) is not None
    # Member change observed after that check: the old check is not relabelled.
    rt.manager.runtime_nodes["exit"].append(gateway_node("Gateway B", address="b.example.invalid"))
    assert receipt_of(rt) is None
    rt.manager.evidence["exit"][SUB]["checked_at"] = iso(time.time_ns() // 1_000_000 + 5_000)
    receipt = receipt_of(rt)
    assert receipt["membership_version"] == 1 and len(receipt["member_ids"]) == 2
    # A runtime refresh job starting later invalidates it again.
    time.sleep(0.01)
    rt._bridge_note_subscription_refresh("exit", SUB)
    rt.manager.evidence["exit"][SUB]["checked_at"] = iso(time.time_ns() // 1_000_000 - 60_000)
    assert receipt_of(rt) is None
    rt.manager.evidence["exit"][SUB]["checked_at"] = iso(time.time_ns() // 1_000_000 + 10_000)
    assert receipt_of(rt)["refresh_job_generation"] == 1
    # While a refresh is in flight there is no receipt at all.
    with rt.subscription_operation_coordinator.system_scope("exit", [SUB]):
        assert receipt_of(rt) is None


def test_real_projection_publishes_current_subscription_health_from_the_getters():
    rt = build_runtime()
    access = RuntimeAccess(rt)
    owner = NodeCommands(RuntimeNodeCommandAdapter(rt)).refresh_owner_snapshot
    projector = NodeProjection(access, refresh_owner=owner)
    before = projector.sample()
    gateway = next(row for row in before["subscriptions"]["items"] if row["tier"] == "gateway")
    assert gateway["visibility"] == "pending" and before["nodes"]["items"][0]["status"] == "pending"
    rt.manager.evidence["exit"][SUB] = {"runtime_generation": 2, "checked_at": iso(LAUNCH_MS + 1000)}
    after = projector.sample()
    gateway = next(row for row in after["subscriptions"]["items"] if row["tier"] == "gateway")
    assert (gateway["visibility"], gateway["total"], gateway["healthy"]) == ("current", 1, 1)
    assert after["nodes"]["items"][0]["status"] == "healthy"
    assert "check_receipt" not in json.dumps(after) and SUB not in json.dumps(after)


def _two_observed_refreshes(rt, sample=None):
    for _ in range(2):
        with rt.subscription_operation_coordinator.system_scope("exit", [SUB]):
            rt.bridge_subscription_refresh_state()
        rt.bridge_subscription_refresh_state()  # idle edge between the two jobs
        if sample is not None:
            sample()


def test_job_generation_never_drops_after_a_call_without_the_subscription():
    """Review M1: a stale owner or an absent entry must not reset the ledger."""
    rt = build_runtime()
    sub_id = opaque_id("exit", 0, SUB)
    rows = lambda: {row["id"]: row for row in rt.bridge_subscription_refresh_state()["items"]}
    _two_observed_refreshes(rt)
    assert rows()[sub_id]["job_generation"] == 2
    # 1. A caller whose (older) owner lacks the subscription: computes, never commits.
    stale = list(rt._bridge_read_owner())
    stale[3] = deepcopy(stale[3])
    stale[3]["tiers"]["exit"]["subscription_urls"] = []
    ledger = deepcopy(rt._bridge_refresh_ledger)
    rt._bridge_subscription_refresh_facts(tuple(stale))
    assert rt._bridge_refresh_ledger == ledger
    assert rows()[sub_id]["job_generation"] == 2
    # 2. A current owner really without it (deleted in place), then re-added.
    version = rows()[sub_id]["membership_version"]
    saved = rt.config["tiers"]["exit"]["subscription_urls"]
    rt.config["tiers"]["exit"]["subscription_urls"] = []
    assert sub_id not in rows()
    rt.config["tiers"]["exit"]["subscription_urls"] = saved
    again = rows()[sub_id]
    assert again["job_generation"] == 2
    assert again["membership_version"] > version  # re-add is a member event


def test_readded_subscription_never_hits_retired_job_facts_in_the_real_projection():
    """Review M1: delete + re-add with one failed sample() in between."""
    from bridge_nodes import StaleNodeSample
    rt = build_runtime()
    sub_id = opaque_id("exit", 0, SUB)
    owner = NodeCommands(RuntimeNodeCommandAdapter(rt)).refresh_owner_snapshot
    saved = rt.config["tiers"]["exit"]["subscription_urls"]
    flip = {"armed": False}

    def refresh_owner():
        snapshot = owner()
        if flip["armed"]:  # the subscription disappears mid-sample
            flip["armed"] = False
            rt.config["tiers"]["exit"]["subscription_urls"] = []
        return snapshot

    projector = NodeProjection(RuntimeAccess(rt), refresh_owner=refresh_owner)
    _two_observed_refreshes(rt, projector.sample)
    assert projector._refresh_jobs[sub_id] == 2
    flip["armed"] = True
    with pytest.raises(StaleNodeSample, match="node owner changed"):
        projector.sample()  # committed the absence, then failed its bracket
    rt.bridge_subscription_records()  # another caller also sees the absence
    rt.config["tiers"]["exit"]["subscription_urls"] = saved
    for _ in range(3):
        sample = projector.sample()  # never "retired refresh job facts"
        assert any(row["id"] == sub_id for row in sample["subscriptions"]["items"])
    assert projector._refresh_jobs[sub_id] == 2


def test_refresh_state_unknown_without_a_coordinator():
    rt = build_runtime()
    del rt.subscription_operation_coordinator
    assert rt.bridge_subscription_refresh_state() is None
    assert RuntimeNodeCommandAdapter(rt).subscription_refresh_state() is None
    row = rt.bridge_subscription_records()["items"][0]
    assert row["membership_version"] is None and row["check_receipt"] is None


def test_refresh_busy_probes_the_lifecycle_lock_without_blocking():
    rt = build_runtime()
    adapter = RuntimeNodeCommandAdapter(rt)
    assert rt.bridge_subscription_refresh_busy() is False
    held, release = threading.Event(), threading.Event()

    def holder():
        with rt._runtime_lifecycle_lock:
            held.set()
            release.wait(5)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert held.wait(5)
        started = time.monotonic()
        assert adapter.subscription_refresh_busy() is True
        assert time.monotonic() - started < 1.0
    finally:
        release.set()
        thread.join(5)
    assert rt.bridge_subscription_refresh_busy() is False
    with rt._runtime_lifecycle_lock:  # the caller's own hold is not "busy"
        assert rt.bridge_subscription_refresh_busy() is False


# --------------------------------------------------------------------------
# Node diagnostic target (P2-17)
# --------------------------------------------------------------------------

def test_diagnostic_target_resolves_running_node_ids_and_auto_pool():
    rt = build_runtime()
    auto = Node(name="Auto", protocol="vless", tier="tier3", address="auto.example.invalid",
                port=443, uuid="11111111-2222-3333-4444-555555555555", source="collector",
                extra={"tier3_origin": "tier3b"})
    rt.manager.runtime_nodes["tier3"] = [auto]
    ids = {row["tier"]: row["id"] for row in rt.bridge_node_records()["items"]}
    target = rt.bridge_node_diagnostic_target(ids["gateway"])
    assert dict(target) == {"node_identity": diagnostic_node_identity(rt.manager.runtime_nodes["exit"][0]),
                            "tier": "exit", "auto_pool": False, "stale": False}
    assert rt.bridge_node_diagnostic_target(ids["bastion"])["auto_pool"] is True
    assert rt.bridge_node_diagnostic_target("0" * 24) is None
    resolved = RuntimeDiagnosticPort(rt).resolve_node(ids["gateway"])
    assert (resolved.tier, resolved.gateway, resolved.auto_pool) == ("exit", True, False)
    rt.manager.runtime_generation = 3
    assert rt.bridge_node_diagnostic_target(ids["gateway"])["stale"] is True
    with pytest.raises(TargetUnavailable):
        RuntimeDiagnosticPort(rt).resolve_node(ids["gateway"])


def test_lock_bodies_copy_facts_and_project_outside(monkeypatch):
    """Review S3/S5: evidence+members in one manager.lock hold; projection after it."""
    import node_diagnostic
    rt = build_runtime()
    depths = []
    original = Manager.subscription_source_check_snapshot

    def snapshot(self, tier):
        depths.append(self.lock.depth)
        return original(self, tier)

    monkeypatch.setattr(Manager, "subscription_source_check_snapshot", snapshot)
    rt.bridge_subscription_refresh_state()
    assert depths and all(depth >= 1 for depth in depths)  # nested in the members hold
    projected = []
    real = node_diagnostic.diagnostic_node_identity

    def identity(node):
        projected.append(rt.manager.lock.depth)
        return real(node)

    monkeypatch.setattr(node_diagnostic, "diagnostic_node_identity", identity)
    node_id = rt.bridge_node_records()["items"][0]["id"]
    target = rt.bridge_node_diagnostic_target(node_id)
    assert target["node_identity"] == real(rt.manager.runtime_nodes["exit"][0])
    assert projected and set(projected) == {0}


def test_diagnostic_target_resolves_the_persisted_inventory_while_stopped(tmp_path):
    rt = build_runtime()
    uri = "trojan://StoppedSecret@stopped.example.invalid:443#Stopped"
    rt.config["tiers"]["exit"] = {"nodes": [uri], "cache_file": str(tmp_path / "exit_cache.json")}
    rt.config["tiers"]["tier2"] = {"cache_file": str(tmp_path / "tier2_cache.json")}
    rt.manager = None
    rt.running = False
    inventory = rt._persisted_node_diagnostic_inventory()
    node = inventory["exit"][0]
    node_id = node_item_id(2, "exit", node)
    target = rt.bridge_node_diagnostic_target(node_id)
    assert dict(target) == {"node_identity": diagnostic_node_identity(node), "tier": "exit",
                            "auto_pool": False, "stale": False}
    assert RuntimeDiagnosticPort(rt).resolve_node(node_id).node_identity == diagnostic_node_identity(node)


# --------------------------------------------------------------------------
# Listener preflight (P2-12)
# --------------------------------------------------------------------------

def test_listener_preflight_checks_only_endpoints_the_core_does_not_hold(monkeypatch):
    rt = build_runtime()
    rt.config["client"]["mihomo_controller"] = {"enabled": False}
    calls = []

    def fake_preflight(specs):
        calls.append(tuple(specs))
        return RuntimeListenerPreflightResult(state=RuntimeListenerPreflightState.OCCUPIED,
                                              specs=tuple(specs), conflicts=tuple(specs))

    monkeypatch.setattr(dr, "preflight_runtime_listeners", fake_preflight)
    unchanged = deepcopy(rt.config["client"])
    result = rt.bridge_listener_preflight(unchanged)
    assert result.state is RuntimeListenerPreflightState.AVAILABLE and calls == []
    moved = deepcopy(unchanged) | {"mihomo_mixed_port": 7999}
    moved["system_proxy"] = {"host": "0.0.0.0", "port": 7999}
    result = rt.bridge_listener_preflight(moved)
    assert result.state is RuntimeListenerPreflightState.OCCUPIED
    assert [(s.kind, s.transport, s.host, s.port) for s in calls[-1]] == [
        ("mixed", "tcp", "127.0.0.1", 7999)]
    rt.core_runner.fact["running"] = False  # nothing held: the unchanged port is checked
    rt.bridge_listener_preflight(unchanged)
    assert [(s.kind, s.port) for s in calls[-1]] == [("mixed", 7890)]
    with pytest.raises(ValueError):
        rt.bridge_listener_preflight(None)


def test_listener_preflight_controller_branch(monkeypatch):
    rt = build_runtime()
    rt.config["client"]["mihomo_controller"] = {"enabled": True, "host": "127.0.0.1", "port": 9097}
    calls = []

    def fake_preflight(specs):
        calls.append(tuple(specs))
        return RuntimeListenerPreflightResult(state=RuntimeListenerPreflightState.AVAILABLE,
                                              specs=tuple(specs))

    monkeypatch.setattr(dr, "preflight_runtime_listeners", fake_preflight)
    unchanged = deepcopy(rt.config["client"])
    # The running Core holds mixed and the enabled controller: nothing to probe.
    assert rt.bridge_listener_preflight(unchanged).state is RuntimeListenerPreflightState.AVAILABLE
    assert calls == []
    rt.core_runner.fact["running"] = False
    rt.bridge_listener_preflight(unchanged)
    assert [(s.kind, s.transport, s.host, s.port) for s in calls[-1]] == [
        ("mixed", "tcp", "127.0.0.1", 7890), ("controller", "tcp", "127.0.0.1", 9097)]


def test_listener_preflight_is_indeterminate_when_the_owner_moves(monkeypatch):
    rt = build_runtime()
    rt.config["client"]["mihomo_controller"] = {"enabled": False}

    def moving_preflight(specs):
        rt._lifecycle_revision += 1  # a lifecycle change during the socket probe
        return RuntimeListenerPreflightResult(state=RuntimeListenerPreflightState.OCCUPIED,
                                              specs=tuple(specs), conflicts=tuple(specs))

    monkeypatch.setattr(dr, "preflight_runtime_listeners", moving_preflight)
    moved = deepcopy(rt.config["client"]) | {"mihomo_mixed_port": 7999}
    moved["system_proxy"] = {"host": "127.0.0.1", "port": 7999}
    result = rt.bridge_listener_preflight(moved)
    assert result.state is RuntimeListenerPreflightState.INDETERMINATE  # never holds a save


# --------------------------------------------------------------------------
# ConfigItem ID (决定_第一波裁定2 P2-14 #3) and the no-routing contract
# --------------------------------------------------------------------------

def test_config_item_id_is_stable_and_opaque():
    item = {"tier": "exit", "kind": "node", "key": "trojan://Secret@x.invalid:443", "enabled": True}
    base = config_item_id(item)
    assert config_item_id(item | {"enabled": False, "label": "renamed"}) == base
    assert config_item_id(item | {"tier": "tier2"}) != base
    assert config_item_id(item | {"kind": "subscription"}) != base
    assert config_item_id(item | {"id": "persisted"}) == config_item_id(
        {"tier": "exit", "kind": "node", "key": "other", "id": "persisted"})
    assert config_item_id(item | {"key": " trojan://Secret@x.invalid:443 "}) == base
    assert len(base) == 24 and "Secret" not in base


GETTERS = ("bridge_subscription_refresh_state", "bridge_subscription_refresh_busy",
           "bridge_core_network_mode", "bridge_exit_selection_readback", "bridge_exit_binding",
           "bridge_exit_proxy_url", "bridge_exit_admission_phase", "bridge_node_diagnostic_target",
           "bridge_listener_preflight", "bridge_alert_delivery_revision",
           "bridge_alert_channel_entry", "_bridge_subscription_refresh_facts",
           "_bridge_published_route", "_bridge_route_unchanged", "_bridge_effective_desktop_alerts")


def test_new_getters_never_touch_routing_reads_or_refreshes():
    banned = ("mihomo_proxies", "providers_proxies", "current_route_context", "select_proxy",
              "_queue_runtime_route_snapshot_refresh", "_refresh_runtime_route_snapshot",
              "api_client", "proxy_group", "reload_core_config", "restart_core")
    import ast
    import textwrap
    for name in GETTERS:
        tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(DesktopRuntime, name))))
        body = tree.body[0].body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]  # docstrings may name what the getter avoids
        code = "\n".join(ast.unparse(statement) for statement in body)
        assert not [word for word in banned if word in code], name
