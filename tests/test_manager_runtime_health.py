from __future__ import annotations

import logging
import threading
import time
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

import manager as manager_module
from desktop_runtime import DesktopRuntime
from manager import ProxyTierManager
from models import Node, NodeStatus
from node_status_view import node_identity, node_status_snapshot
from runtime_egress_recovery import (
    BoundRuntimeFailure,
    CoreLoadedRouteSnapshot,
    RuntimeEgressCoordinator,
    RuntimeNodeRef,
    RuntimeRouteBinding,
)


def _node(
    tier: str,
    name: str,
    *,
    protocol: str = "ss",
    address: str | None = None,
    port: int = 443,
    origin: str = "",
    physical: str = "",
) -> Node:
    extra = {}
    if origin:
        extra["tier3_origin"] = origin
    if physical:
        extra.update(
            {
                "physical_node": physical,
                "protocol_label": protocol,
                "fallback_order": ["vless", "hysteria2"],
            }
        )
    return Node(
        tier=tier,
        name=name,
        protocol=protocol,
        address=address or f"{name}.example",
        port=port,
        status=NodeStatus.ALIVE,
        latency_ms=25.0,
        last_checked_at="2026-08-23T00:00:00+00:00",
        extra=extra,
    )


def _manager(nodes: list[Node]) -> ProxyTierManager:
    instance = ProxyTierManager.__new__(ProxyTierManager)
    instance.lock = threading.RLock()
    instance.operation_lock = threading.RLock()
    instance.runtime_generation = 7
    instance.runtime_nodes = {tier: [] for tier in ("tier1", "tier2", "tier3", "exit")}
    for node in nodes:
        instance.runtime_nodes[node.tier].append(node)
    instance._runtime_health_ledger = {}
    instance._runtime_health_completed_incidents = {}
    instance._runtime_leaf_recheck_inflight = set()
    instance.on_runtime_leaf_recheck_result = None
    instance.tier3_runtime_overlay_unavailable_count = 0
    instance.tier3_last_collection_report = {}
    instance.tier3_refresh_progress = {}
    instance.last_check_at = {tier: None for tier in instance.runtime_nodes}
    instance.closed = False
    instance.logger = SimpleNamespace()
    instance._tier3_enabled = lambda: True
    return instance


def _bind_core_inventory_truth(
    instance: ProxyTierManager,
    members: list[dict[str, str]],
) -> None:
    instance.tier3_core_running = True
    instance.tier3_startup_gate_satisfied = True
    instance.tier3_traffic_capture_active = True
    instance.tier3_runtime_qualified_count = len(members)
    instance.tier3_runtime_exported_count = len(members)
    instance.effective_egress_ok = True
    instance.effective_egress_reason = ""
    instance._tier3_min_alive_nodes = lambda: 1
    instance.tier3_runtime_truth_provider = lambda: {
        "runtime_generation": instance.runtime_generation,
        "runtime_service_running": True,
        "core_process_running": True,
        "startup_gate_satisfied": True,
        "effective_egress_ok": True,
        "traffic_capture_active": True,
        "core_inventory_verified": True,
        "core_inventory_members": list(members),
        "core_inventory_semantic_digest": "loaded-digest",
        "core_inventory_route_epoch": 11,
    }


def _configure_tier3_pool_mutation(
    instance: ProxyTierManager,
    *,
    eviction_threshold: int = 1,
) -> None:
    instance.tier3_source_count = 0
    instance.last_collect_tier3 = 0.0
    instance._tier3_pre_gate_side_effects_suppressed = lambda: False
    instance._tier3_pool_eviction_failure_threshold = lambda: eviction_threshold
    instance._sort_tier3_nodes = lambda nodes: list(nodes)

    def replace(nodes, **_kwargs):
        instance.runtime_nodes["tier3"] = list(nodes)
        return True

    instance._replace_tier3_pool_locked = replace
    instance._save_tier3_snapshot = lambda *_args, **_kwargs: None
    instance._observe_nodes = lambda *_args, **_kwargs: None
    instance._notify_tier3_pool_committed = lambda *_args, **_kwargs: None


def _tier3_provider_runtime(instance: ProxyTierManager) -> DesktopRuntime:
    runtime = object.__new__(DesktopRuntime)
    runtime.manager = instance
    runtime._last_synced_tier3_nodes = list(instance.runtime_nodes["tier3"])
    runtime._node_identity_text = instance._node_identity
    runtime._tier3_core_export_override_snapshot = lambda: []
    return runtime


def _confirmed(
    instance: ProxyTierManager,
    node: Node,
    *,
    incident: str = "incident-1",
    pool_kind: str = "",
    proxy_name: str = "",
    observed_monotonic: float | None = None,
    required_passes: int = 1,
) -> dict:
    return instance.apply_runtime_health_observation(
        {
            "tier": node.tier,
            "pool_kind": pool_kind,
            "identity": instance._node_identity(node),
            "proxy_name": proxy_name or node.name,
            "source": "mihomo_stdout",
            "verdict": "confirmed",
            "failure_domain": "node",
            "incident_id": incident,
            "failure_epoch": 11,
            "required_passes": required_passes,
            "quarantine_token": f"token-{incident}",
            "runtime_generation": 7,
            "core_generation": 3,
            "semantic_digest": "digest-a",
            "observed_monotonic": (
                time.monotonic() - 0.1
                if observed_monotonic is None
                else observed_monotonic
            ),
        }
    )


def _authoritative_result(
    entry: dict,
    verdict: str,
    **overrides,
) -> dict:
    payload = dict(entry)
    payload.update(
        {
            "source": "runtime_leaf_recheck",
            "verdict": verdict,
            "authoritative": True,
            "recheck_started_monotonic": time.monotonic(),
            "observed_monotonic": time.monotonic(),
        }
    )
    payload.update(overrides)
    return payload


def test_confirmed_failure_is_effective_only_and_snapshot_uses_one_health_truth() -> None:
    node = _node("tier2", "tier2-a")
    instance = _manager([node])

    transition = _confirmed(instance, node)

    assert transition["action"] == "quarantined"
    assert node.status is NodeStatus.ALIVE
    assert node.last_checked_at == "2026-08-23T00:00:00+00:00"
    assert instance.effective_node_status("tier2", node) is NodeStatus.SUSPICIOUS
    snapshot = instance.get_node_status_snapshot("tier2")["tiers"]["tier2"]
    assert (snapshot["healthy"], snapshot["total"]) == (0, 1)
    assert snapshot["nodes"][0]["status"] == "suspicious"
    assert snapshot["nodes"][0]["self_check_status"] == "alive"
    assert snapshot["nodes"][0]["pending_recheck"] is True
    assert snapshot["nodes"][0]["last_checked_at"] == node.last_checked_at


def test_only_matching_authoritative_result_clears_and_elapsed_time_never_does() -> None:
    node = _node("tier2", "tier2-a")
    instance = _manager([node])
    _confirmed(instance, node, observed_monotonic=time.monotonic() - 1000.0)
    entry = instance.runtime_health_snapshot()["entries"][0]

    assert instance.runtime_health_snapshot()["unavailable_count"] == 1
    stale = instance.apply_runtime_health_observation(
        _authoritative_result(entry, "pass", quarantine_token="wrong-token")
    )
    assert stale["action"] == "stale"
    assert instance.effective_node_usable("tier2", node) is False

    recovered = instance.apply_runtime_health_observation(
        _authoritative_result(entry, "pass")
    )
    assert recovered["action"] == "recovered"
    assert instance.effective_node_usable("tier2", node) is True


def test_flap_damping_requires_two_authoritative_passes_two_seconds_apart() -> None:
    node = _node("tier2", "tier2-a")
    instance = _manager([node])
    _confirmed(instance, node, required_passes=2)
    entry = instance.runtime_health_snapshot()["entries"][0]
    first_started = time.monotonic()

    first = instance.apply_runtime_health_observation(
        _authoritative_result(
            entry,
            "pass",
            recheck_started_monotonic=first_started,
        )
    )
    too_soon = instance.apply_runtime_health_observation(
        _authoritative_result(
            entry,
            "pass",
            recheck_started_monotonic=first_started + 1.0,
        )
    )
    assert instance.effective_node_usable("tier2", node) is False
    recovered = instance.apply_runtime_health_observation(
        _authoritative_result(
            entry,
            "pass",
            recheck_started_monotonic=first_started + 2.1,
        )
    )

    assert first["action"] == "pass_confirmation_pending"
    assert too_soon["action"] == "pass_confirmation_pending"
    assert recovered["action"] == "recovered"
    assert instance.effective_node_usable("tier2", node) is True
    assert instance.runtime_health_snapshot()["unavailable_count"] == 0


def test_authoritative_failure_transfers_to_raw_layer_status() -> None:
    node = _node("exit", "gateway-a")
    instance = _manager([node])
    _confirmed(instance, node)
    entry = instance.runtime_health_snapshot()["entries"][0]
    node.status = NodeStatus.DEAD
    node.last_checked_at = "2026-08-23T00:01:00+00:00"

    transferred = instance.apply_runtime_health_observation(
        _authoritative_result(entry, "fail")
    )

    assert transferred["action"] == "transferred"
    assert instance.runtime_health_snapshot()["unavailable_count"] == 1
    assert instance.runtime_health_snapshot()["entries"][0]["pending_recheck"] is True
    assert instance.effective_node_status("exit", node) is NodeStatus.DEAD

    node.status = NodeStatus.ALIVE
    recovered = instance.apply_runtime_health_observation(
        _authoritative_result(
            entry,
            "pass",
            recheck_started_monotonic=time.monotonic() + 5.0,
        )
    )
    assert recovered["action"] == "recovered"
    assert instance.runtime_health_snapshot()["unavailable_count"] == 0


def test_tier1_physical_health_survives_one_quarantined_protocol_variant() -> None:
    vless = _node("tier1", "home-vless", protocol="vless", physical="home")
    hy2 = _node(
        "tier1",
        "home-hy2",
        protocol="hysteria2",
        port=8443,
        physical="home",
    )
    instance = _manager([vless, hy2])
    _confirmed(instance, vless)

    tier = instance.get_node_status_snapshot("tier1")["tiers"]["tier1"]

    assert (tier["healthy"], tier["total"]) == (1, 1)
    assert (tier["variant_healthy"], tier["variant_total"]) == (1, 2)


def test_tier1_physical_health_drops_only_after_every_variant_is_quarantined() -> None:
    vless = _node("tier1", "home-vless", protocol="vless", physical="home")
    hy2 = _node(
        "tier1",
        "home-hy2",
        protocol="hysteria2",
        port=8443,
        physical="home",
    )
    instance = _manager([vless, hy2])

    _confirmed(instance, vless, incident="vless")
    _confirmed(instance, hy2, incident="hy2")
    unavailable = instance.get_node_status_snapshot("tier1")["tiers"]["tier1"]
    assert (unavailable["healthy"], unavailable["total"]) == (0, 1)
    assert (unavailable["variant_healthy"], unavailable["variant_total"]) == (0, 2)

    entries = {
        entry["proxy_name"]: entry
        for entry in instance.runtime_health_snapshot()["entries"]
    }
    recovered = instance.apply_runtime_health_observation(
        _authoritative_result(entries["home-vless"], "pass")
    )

    assert recovered["action"] == "recovered"
    restored = instance.get_node_status_snapshot("tier1")["tiers"]["tier1"]
    assert (restored["healthy"], restored["total"]) == (1, 1)
    assert (restored["variant_healthy"], restored["variant_total"]) == (1, 2)
    assert [
        entry["proxy_name"]
        for entry in instance.runtime_health_snapshot()["entries"]
    ] == ["home-hy2"]


def test_tier2_failure_and_recovery_preserve_static_inventory() -> None:
    node = _node("tier2", "tier2-a")
    instance = _manager([node])
    _confirmed(instance, node)
    entry = instance.runtime_health_snapshot()["entries"][0]

    node.status = NodeStatus.DEAD
    failed = instance.apply_runtime_health_observation(
        _authoritative_result(entry, "fail")
    )

    assert failed["action"] == "transferred"
    assert instance.runtime_nodes["tier2"] == [node]
    assert instance.get_node_status_snapshot("tier2")["tiers"]["tier2"]["total"] == 1

    node.status = NodeStatus.ALIVE
    recovered = instance.apply_runtime_health_observation(
        _authoritative_result(
            entry,
            "pass",
            recheck_started_monotonic=time.monotonic() + 5.0,
        )
    )

    assert recovered["action"] == "recovered"
    snapshot = instance.get_node_status_snapshot("tier2")["tiers"]["tier2"]
    assert (snapshot["healthy"], snapshot["total"]) == (1, 1)


@pytest.mark.parametrize("tier,pool_kind", [("tier1", "tier1_variant"), ("tier2", "tier2"), ("exit", "gateway")])
def test_dispatcher_uses_exact_ordinary_layer_check_without_layer_clock(
    monkeypatch: pytest.MonkeyPatch,
    tier: str,
    pool_kind: str,
) -> None:
    node = _node(tier, f"{tier}-a")
    instance = _manager([node])
    _confirmed(instance, node, pool_kind=pool_kind)
    before_clocks = dict(instance.last_check_at)
    calls: list[tuple[str, dict]] = []
    callbacks: list[dict] = []

    class InlineThread:
        def __init__(self, *, target, args, **_kwargs):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    def fake_test(tier_id, identity, **kwargs):
        calls.append(("test", {"tier": tier_id, "identity": identity, **kwargs}))
        tested = node.clone()
        tested.status = NodeStatus.DEAD
        tested.last_checked_at = "2026-08-23T00:02:00+00:00"
        return tested

    def fake_replace(tier_id, identity, tested, **kwargs):
        calls.append(("replace", {"tier": tier_id, "identity": identity, **kwargs}))
        instance.runtime_nodes[tier_id][0] = tested

    monkeypatch.setattr(manager_module.threading, "Thread", InlineThread)
    instance._test_runtime_node_by_identity = fake_test
    instance._replace_tested_runtime_node = fake_replace
    instance.on_runtime_leaf_recheck_result = callbacks.append
    ref = instance.runtime_health_snapshot()["entries"][0]

    accepted = instance.enqueue_runtime_leaf_recheck(ref)

    assert accepted["accepted"] is True
    assert [name for name, _payload in calls] == ["test", "replace"]
    assert calls[0][1]["mark_layer_check"] is False
    assert calls[0][1]["persist_result"] is False
    assert calls[1][1]["mark_layer_check"] is False
    assert instance.last_check_at == before_clocks
    assert callbacks[-1]["verdict"] == "fail"
    assert callbacks[-1]["transition"]["action"] == "transferred"


def test_tier3a_dispatches_user_pool_health_and_tier3b_delegates() -> None:
    user_node = _node("tier3", "user-a", origin="tier3a")
    auto_node = _node("tier3", "auto-a", address="auto.example", origin="tier3b")
    instance = _manager([user_node, auto_node])
    _confirmed(instance, user_node, incident="user", pool_kind="tier3a")
    _confirmed(instance, auto_node, incident="auto", pool_kind="tier3b")
    calls: list[tuple[str, str]] = []

    class InlineThread:
        def __init__(self, *, target, args, **_kwargs):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    def user_check(target, *, runtime_recheck=None):
        assert runtime_recheck["proxy_name"] == "user-a"
        calls.append(("tier3a", target.name))
        return SimpleNamespace(status=NodeStatus.ALIVE, pending_recheck=False)

    def auto_check(identity, **kwargs):
        calls.append(("tier3b", identity))
        assert kwargs["_runtime_recheck"]["proxy_name"] == "auto-a"
        return {"accepted": True}

    original_thread = manager_module.threading.Thread
    manager_module.threading.Thread = InlineThread
    try:
        instance._run_tier3a_process_health_check = user_check
        instance.enqueue_tier3_targeted_recheck = auto_check
        entries = {
            entry["pool_kind"]: entry
            for entry in instance.runtime_health_snapshot()["entries"]
        }
        user_result = instance.enqueue_runtime_leaf_recheck(entries["tier3a"])
        auto_result = instance.enqueue_runtime_leaf_recheck(entries["tier3b"])
    finally:
        manager_module.threading.Thread = original_thread

    assert user_result["status"] == "started"
    assert auto_result["status"] == "delegated"
    assert [name for name, _target in calls] == ["tier3a", "tier3b"]
    assert instance.last_check_at["tier3"] is None


def test_tier3a_runtime_failure_uses_user_health_without_auto_pool_strikes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = _node("tier3", "user-a", origin="tier3a")
    instance = _manager([node])
    _confirmed(instance, node, incident="user-fail", pool_kind="tier3a")
    callbacks: list[dict] = []

    class InlineThread:
        def __init__(self, *, target, args, **_kwargs):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    def user_check(target, *, runtime_recheck=None):
        assert runtime_recheck["pool_kind"] == "tier3a"
        tested = target.clone()
        tested.status = NodeStatus.DEAD
        tested.latency_ms = None
        instance.runtime_nodes["tier3"] = [tested]
        return SimpleNamespace(status=NodeStatus.DEAD, pending_recheck=False)

    def forbidden_auto_rule(*_args, **_kwargs):
        pytest.fail("Tier3A runtime health must not use Tier3B strike/eviction rules")

    monkeypatch.setattr(manager_module.threading, "Thread", InlineThread)
    instance._run_tier3a_process_health_check = user_check
    instance.enqueue_tier3_targeted_recheck = forbidden_auto_rule
    instance.mark_runtime_audit_node_failure = forbidden_auto_rule
    instance.on_runtime_leaf_recheck_result = callbacks.append
    entry = instance.runtime_health_snapshot()["entries"][0]

    accepted = instance.enqueue_runtime_leaf_recheck(entry)

    assert accepted["status"] == "started"
    assert len(instance.runtime_nodes["tier3"]) == 1
    assert instance.runtime_nodes["tier3"][0].status is NodeStatus.DEAD
    assert "tier3_consecutive_failures" not in instance.runtime_nodes["tier3"][0].extra
    assert callbacks[-1]["transition"]["action"] == "transferred"


def test_direct_runtime_audit_failure_can_never_strike_or_evict_tier3a() -> None:
    node = _node("tier3", "user-a", origin="tier3a")
    instance = _manager([node])
    _configure_tier3_pool_mutation(instance, eviction_threshold=1)

    try:
        instance.mark_runtime_audit_node_failure(
            "tier3",
            instance._node_identity(node),
            reason="runtime transport failure",
            marker="runtime:test-tier3a",
            pool_kind="tier3a",
            proxy_name=node.name,
        )
    except ValueError:
        # Rejecting this Tier3B-only mutation API is also a valid contract.
        pass

    assert len(instance.runtime_nodes["tier3"]) == 1
    retained = instance.runtime_nodes["tier3"][0]
    assert instance._node_identity(retained) == instance._node_identity(node)
    assert "tier3_consecutive_failures" not in retained.extra
    assert "tier3_evicted_from_pool" not in retained.extra


def test_tier3b_manager_ratio_follows_quarantine_recheck_and_eviction() -> None:
    nodes = [
        _node("tier3", "auto-a", origin="tier3b"),
        _node("tier3", "auto-b", origin="tier3b"),
        _node("tier3", "auto-c", origin="tier3b"),
    ]
    instance = _manager(nodes)

    def ratio() -> tuple[int, int]:
        snapshot = instance.get_node_status_snapshot("tier3")["tiers"]["tier3"]
        return snapshot["healthy"], snapshot["total"]

    assert ratio() == (3, 3)
    _confirmed(instance, nodes[0], incident="first", pool_kind="tier3b")
    assert ratio() == (2, 3)

    first_entry = instance.runtime_health_snapshot()["entries"][0]
    recovered = instance.apply_runtime_health_observation(
        _authoritative_result(first_entry, "pass")
    )
    assert recovered["action"] == "recovered"
    assert ratio() == (3, 3)

    _confirmed(instance, nodes[0], incident="second", pool_kind="tier3b")
    assert ratio() == (2, 3)

    # The Tier3B retention owner performs the durable removal only after its
    # configured strike threshold.  Once that lawful eviction is committed,
    # the same Manager truth changes the denominator instead of restoring m.
    instance.runtime_nodes["tier3"] = list(instance.runtime_nodes["tier3"][1:])
    assert ratio() == (2, 2)


def test_tier3b_health_overlay_preserves_provider_until_legal_eviction() -> None:
    nodes = [
        _node("tier3", "auto-a", origin="tier3b"),
        _node("tier3", "auto-b", origin="tier3b"),
        _node("tier3", "auto-c", origin="tier3b"),
    ]
    instance = _manager(nodes)
    instance.enqueue_runtime_leaf_recheck = lambda *_args, **_kwargs: {
        "accepted": True,
        "status": "delegated",
    }
    runtime = _tier3_provider_runtime(instance)
    core_api_mutations: list[str] = []
    runtime._update_running_tier3_core_provider = (
        lambda *_args, **_kwargs: core_api_mutations.append("provider")
    )
    runtime.reload_core_config = (
        lambda *_args, **_kwargs: core_api_mutations.append("reload")
    )
    runtime.select_mihomo_proxy = (
        lambda *_args, **_kwargs: core_api_mutations.append("selector")
    )
    runtime._runtime_flap_history = {}
    runtime._runtime_generation = instance.runtime_generation
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_hot_standby_lock = threading.RLock()
    runtime._runtime_network_context_hold_active = False
    runtime._runtime_local_facility_fault_reason = ""
    runtime._runtime_incident_bindings = {}
    runtime._runtime_incident_route_payloads = {}
    runtime._revoke_tier3_runtime_leaf_qualification = lambda _identity: None

    bad_ref = RuntimeNodeRef(
        "tier3",
        "tier3b",
        instance._node_identity(nodes[0]),
        nodes[0].name,
    )
    snapshot = CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        proxy_nodes=(bad_ref,),
    )
    binding = RuntimeRouteBinding(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        observed_outbound="GENERAL",
        route_chain=("GENERAL", "FINAL-EXIT", "TIER3-AUTO", nodes[0].name),
        node=bad_ref,
    )
    runtime._runtime_route_snapshot = snapshot
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
    provider_before = [
        node.name for node in runtime._tier3_nodes_for_core_export()
    ]

    transition = runtime._apply_confirmed_runtime_health(
        BoundRuntimeFailure(
            binding=binding,
            protocol="TCP",
            observed_outbound="GENERAL",
            destination_token="destination-token",
            error_type="timeout",
            observed_at_monotonic=time.monotonic(),
        ),
        snapshot,
        {"proxy_map": {}},
        incident_id="incident-provider-overlay",
        quarantine_token="token-provider-overlay",
    )

    assert transition["action"] == "quarantined"
    ratio = instance.get_node_status_snapshot("tier3")["tiers"]["tier3"]
    assert (ratio["healthy"], ratio["total"]) == (2, 3)
    assert [node.name for node in runtime._tier3_nodes_for_core_export()] == provider_before
    assert core_api_mutations == []

    _configure_tier3_pool_mutation(instance, eviction_threshold=1)
    evicted = instance.mark_runtime_audit_node_failure(
        "tier3",
        instance._node_identity(nodes[0]),
        reason="matching Tier3B targeted recheck failed",
        marker="runtime:tier3b-eviction",
        pool_kind="tier3b",
        proxy_name=nodes[0].name,
    )

    assert evicted.extra["tier3_evicted_from_pool"] is True
    after_eviction = instance.get_node_status_snapshot("tier3")["tiers"]["tier3"]
    assert (after_eviction["healthy"], after_eviction["total"]) == (2, 2)
    assert [node.name for node in runtime._tier3_nodes_for_core_export()] == [
        "auto-b",
        "auto-c",
    ]


def test_replacement_provider_commit_cannot_clear_bad_leaf_runtime_health() -> None:
    bad = _node("tier3", "auto-bad", origin="tier3b")
    good = _node("tier3", "auto-good", origin="tier3b")
    instance = _manager([bad, good])
    _confirmed(
        instance,
        bad,
        incident="bad-leaf",
        pool_kind="tier3b",
    )
    runtime = _tier3_provider_runtime(instance)
    runtime.stop_event = threading.Event()
    runtime.logger = SimpleNamespace(info=lambda *_args, **_kwargs: None)
    runtime.core_runner = None
    runtime._fail_terminal_tier3_bootstrap = lambda *_args, **_kwargs: False
    runtime._tier3_core_gate_check = lambda _nodes: (
        False,
        2,
        25,
        "below startup floor",
    )
    runtime._queue_tier3_core_gate_attempt = lambda: pytest.fail(
        "replacement below the floor must not start Core"
    )

    replacement = _node("tier3", "auto-replacement", origin="tier3b")
    instance.runtime_nodes["tier3"].append(replacement)
    runtime._on_tier3_pool_committed(
        list(instance.runtime_nodes["tier3"]),
        {
            "alive_count": 3,
            "total_count": 3,
            "admission_identity": instance._node_identity(replacement),
        },
    )

    remaining = instance.runtime_health_snapshot()["entries"]
    assert len(remaining) == 1
    assert remaining[0]["identity"] == instance._node_identity(bad)
    assert instance.effective_node_status("tier3", bad) is NodeStatus.SUSPICIOUS
    ratio = instance.get_node_status_snapshot("tier3")["tiers"]["tier3"]
    assert (ratio["healthy"], ratio["total"]) == (2, 3)
    assert [node.name for node in runtime._tier3_nodes_for_core_export()] == [
        "auto-bad",
        "auto-good",
        "auto-replacement",
    ]


def test_full_leaf_reference_keeps_tier3a_and_tier3b_incidents_separate() -> None:
    user_node = _node(
        "tier3",
        "user-alias",
        address="shared.example",
        origin="tier3a",
    )
    auto_node = _node(
        "tier3",
        "auto-alias",
        address="shared.example",
        origin="tier3b",
    )
    instance = _manager([user_node, auto_node])
    assert instance._node_identity(user_node) == instance._node_identity(auto_node)

    _confirmed(instance, user_node, incident="user", pool_kind="tier3a")
    _confirmed(instance, auto_node, incident="auto", pool_kind="tier3b")
    entries = {
        entry["pool_kind"]: entry
        for entry in instance.runtime_health_snapshot()["entries"]
    }
    recovered = instance.apply_runtime_health_observation(
        _authoritative_result(entries["tier3a"], "pass")
    )

    assert recovered["action"] == "recovered"
    remaining = instance.runtime_health_snapshot()["entries"]
    assert [(entry["pool_kind"], entry["proxy_name"]) for entry in remaining] == [
        ("tier3b", "auto-alias")
    ]
    request = dict(remaining[0])
    request["recheck_started_monotonic"] = time.monotonic()
    assert instance._reserve_runtime_layer_failure(request) is True
    assert instance._reserve_runtime_layer_failure(request) is False


def test_core_inventory_truth_counts_only_loaded_members_and_runtime_health() -> None:
    nodes = [
        _node("tier3", f"auto-{index:02d}", origin="tier3b")
        for index in range(16)
    ]
    instance = _manager(nodes)
    instance.tier3_pool_revision = 9
    loaded = nodes[:7]
    _bind_core_inventory_truth(
        instance,
        [
            {
                "tier": "tier3",
                "pool_kind": "tier3b",
                "identity": instance._node_identity(node),
                "proxy_name": node.name,
            }
            for node in loaded
        ],
    )

    _confirmed(instance, loaded[0], pool_kind="tier3b")

    truth = instance._tier3_live_runtime_truth_fields()
    durable = instance._tier3_durable_pool_count_snapshot_locked()
    assert (durable.healthy, durable.total) == (16, 16)
    assert all(node.status is NodeStatus.ALIVE for node in nodes)
    assert truth["core_inventory_verified"] is True
    assert (
        truth["core_inventory_healthy"],
        truth["core_inventory_total"],
    ) == (6, 7)
    assert (
        truth["core_inventory_auto_healthy"],
        truth["core_inventory_auto_total"],
    ) == (6, 7)


def test_core_inventory_truth_matches_same_identity_by_pool_kind() -> None:
    user_node = _node(
        "tier3",
        "user-shared",
        address="shared.example",
        origin="tier3a",
    )
    auto_node = _node(
        "tier3",
        "auto-shared",
        address="shared.example",
        origin="tier3b",
    )
    instance = _manager([user_node, auto_node])
    shared_identity = instance._node_identity(user_node)
    assert shared_identity == instance._node_identity(auto_node)
    _bind_core_inventory_truth(
        instance,
        [
            {
                "tier": "tier3",
                "pool_kind": "tier3a",
                "identity": shared_identity,
                "proxy_name": user_node.name,
            },
            {
                "tier": "tier3",
                "pool_kind": "tier3b",
                "identity": shared_identity,
                "proxy_name": auto_node.name,
            },
        ],
    )

    _confirmed(instance, auto_node, pool_kind="tier3b")

    truth = instance._tier3_live_runtime_truth_fields()
    assert (
        truth["core_inventory_user_healthy"],
        truth["core_inventory_user_total"],
    ) == (1, 1)
    assert (
        truth["core_inventory_auto_healthy"],
        truth["core_inventory_auto_total"],
    ) == (0, 1)
    assert (
        truth["core_inventory_healthy"],
        truth["core_inventory_total"],
    ) == (1, 2)


def test_normalized_core_proxy_name_still_projects_to_unique_manager_node() -> None:
    node = _node("tier2", "raw display name")
    instance = _manager([node])

    _confirmed(
        instance,
        node,
        pool_kind="tier2",
        proxy_name="TIER2-normalized-8d92",
    )
    entry = instance.runtime_health_snapshot()["entries"][0]

    assert entry["proxy_name"] == "TIER2-normalized-8d92"
    assert instance.effective_node_status("tier2", node) is NodeStatus.SUSPICIOUS
    assert instance.runtime_health_snapshot()["unavailable_count"] == 1
    recovered = instance.apply_runtime_health_observation(
        _authoritative_result(entry, "pass")
    )
    assert recovered["action"] == "recovered"


def test_generation_change_clears_volatile_ledger() -> None:
    node = _node("tier2", "tier2-a")
    instance = _manager([node])
    _confirmed(instance, node)
    instance.subscription_source_check_evidence = {"tier2": {}, "exit": {}}
    instance.tier3_report_revision = 0
    instance.tier3_runtime_truth_provider = None
    instance.tier3_startup_gate_satisfied = True
    instance.tier3_core_running = True
    instance.tier3_traffic_capture_active = True
    instance.tier3_runtime_qualified_count = 0
    instance.tier3_runtime_exported_count = 0
    instance.tier3_runtime_qualification_generation = 0
    instance.tier3_runtime_qualification = {}
    instance.effective_egress_ok = True
    instance.effective_egress_reason = ""
    instance._tier3_persisted_collection_history = lambda report: dict(report or {})
    instance._apply_tier3_runtime_truth = lambda report: report

    instance.bind_runtime_generation(8)

    assert instance.runtime_health_snapshot()["unavailable_count"] == 0


def test_reconfigured_node_clears_runtime_overlay_and_inflight_token() -> None:
    node = _node("tier2", "tier2-a")
    instance = _manager([node])
    _confirmed(instance, node)
    entry = instance.runtime_health_snapshot()["entries"][0]
    incident_key = instance._runtime_health_incident_key(entry)
    instance._runtime_leaf_recheck_inflight.add(incident_key)

    node.encryption = "aes-128-gcm"

    assert instance.runtime_health_snapshot()["unavailable_count"] == 0
    assert incident_key not in instance._runtime_leaf_recheck_inflight
    assert instance._runtime_health_completed_incidents[incident_key] == "node_reconfigured"


def test_gateway_recovery_requires_the_same_identity_and_incident_binding() -> None:
    selected = _node("exit", "gateway-selected")
    other = _node("exit", "gateway-other", port=8443)
    instance = _manager([selected, other])
    _confirmed(instance, selected, pool_kind="gateway")
    entry = instance.runtime_health_snapshot()["entries"][0]

    wrong_gateway = _authoritative_result(
        entry,
        "pass",
        identity=instance._node_identity(other),
        proxy_name=other.name,
        node_config_digest=instance._runtime_node_config_digest(other),
    )
    stale = instance.apply_runtime_health_observation(wrong_gateway)

    assert stale["action"] == "stale"
    assert instance.effective_node_usable("exit", selected) is False
    assert instance.effective_node_usable("exit", other) is True

    recovered = instance.apply_runtime_health_observation(
        _authoritative_result(entry, "pass")
    )
    assert recovered["action"] == "recovered"
    assert instance.effective_node_usable("exit", selected) is True


def test_old_periodic_alive_result_and_elapsed_time_cannot_clear_new_incident() -> None:
    node = _node("tier2", "tier2-a")
    instance = _manager([node])
    incident_at = time.monotonic()
    _confirmed(instance, node, observed_monotonic=incident_at)
    entry = instance.runtime_health_snapshot()["entries"][0]

    # Simulate a periodic batch that started before the incident and later
    # merged an ALIVE raw node.  Effective health must remain ledger-owned.
    stale_alive = node.clone()
    stale_alive.status = NodeStatus.ALIVE
    stale_alive.last_checked_at = "2026-08-23T00:03:00+00:00"
    instance.runtime_nodes["tier2"] = [stale_alive]
    old_result = instance.apply_runtime_health_observation(
        _authoritative_result(
            entry,
            "alive",
            source="self_check",
            authoritative=False,
            recheck_started_monotonic=incident_at - 1.0,
        )
    )

    assert old_result["action"] == "stale"
    assert instance.effective_node_usable("tier2", stale_alive) is False

    # A wall-clock/TTL-sized delay alone is likewise not a recovery signal.
    next(iter(instance._runtime_health_ledger.values()))["observed_monotonic"] -= 1000.0
    assert instance.runtime_health_snapshot()["unavailable_count"] == 1
    assert instance.effective_node_usable("tier2", stale_alive) is False


def test_empty_tier3_pool_clears_runtime_report_counts() -> None:
    instance = _manager([])
    instance.tier3_runtime_overlay_unavailable_count = 2
    instance.tier3_last_collection_report.update(
        {
            "runtime_health_unavailable_count": 2,
            "runtime_overlay_unavailable_count": 2,
            "runtime_auto_pool_display_healthy_count": 1,
            "runtime_display_healthy_count": 1,
        }
    )
    instance.tier3_refresh_progress.update(instance.tier3_last_collection_report)

    instance._refresh_runtime_health_report_locked()

    for payload in (
        instance.tier3_last_collection_report,
        instance.tier3_refresh_progress,
    ):
        assert payload["runtime_health_unavailable_count"] == 0
        assert payload["runtime_overlay_unavailable_count"] == 0
        assert payload["runtime_auto_pool_display_healthy_count"] == 0
        assert payload["runtime_display_healthy_count"] == 0
    assert instance.tier3_runtime_overlay_unavailable_count == 0


def test_pending_recheck_projection_uses_ledger_field() -> None:
    node = _node("tier2", "tier2-a")
    instance = _manager([node])
    _confirmed(instance, node)
    ledger_entry = next(iter(instance._runtime_health_ledger.values()))
    ledger_entry["pending_recheck"] = False

    manager_snapshot = instance.get_node_status_snapshot("tier2")["tiers"]["tier2"]
    assert manager_snapshot["nodes"][0]["pending_recheck"] is False

    class HealthOwner:
        @staticmethod
        def effective_node_status(_tier, _node):
            return NodeStatus.SUSPICIOUS

        @staticmethod
        def effective_node_usable(_tier, _node):
            return False

        @staticmethod
        def runtime_health_snapshot():
            return {
                "entries": [
                    {
                        "tier": "tier2",
                        "identity": node_identity(node),
                        "proxy_name": node.name,
                        "pending_recheck": False,
                    }
                ]
            }

    view_snapshot = node_status_snapshot(node, health_owner=HealthOwner())
    assert view_snapshot["pending_recheck"] is False


def test_superseding_tier3_runtime_request_discards_old_inflight_key() -> None:
    node = _node("tier3", "auto-a", origin="tier3b")
    instance = _manager([node])
    instance.tier3_targeted_recheck_queue = []
    identity = instance._node_identity(node)
    old_request = {
        "runtime_generation": 7,
        "tier": "tier3",
        "pool_kind": "tier3b",
        "identity": identity,
        "proxy_name": node.name,
        "incident_id": "incident-old",
        "quarantine_token": "token-old",
    }
    new_request = {
        **old_request,
        "incident_id": "incident-new",
        "quarantine_token": "token-new",
    }
    old_key = instance._runtime_leaf_recheck_key(old_request)
    new_key = instance._runtime_leaf_recheck_key(new_request)
    instance._runtime_leaf_recheck_inflight.update({old_key, new_key})
    instance.tier3_targeted_recheck_queue.append(
        {
            "identity": identity,
            "reason": "old",
            "source": "runtime",
            "runtime_recheck": old_request,
        }
    )
    instance.trigger_tier3_targeted_recheck = lambda: {
        "accepted": False,
        "status": "running",
    }

    instance.enqueue_tier3_targeted_recheck(
        identity,
        reason="new",
        _runtime_recheck=new_request,
    )

    assert old_key not in instance._runtime_leaf_recheck_inflight
    assert new_key in instance._runtime_leaf_recheck_inflight
    assert instance.tier3_targeted_recheck_queue[0]["runtime_recheck"] == new_request


def test_runtime_candidate_snapshot_is_exact_immutable_and_revisioned() -> None:
    first = _node("tier2", "shared", address="first.example")
    second = _node("tier2", "shared", address="second.example", port=8443)
    second.status = NodeStatus.SLOW
    dead = _node("exit", "gateway-dead")
    dead.status = NodeStatus.DEAD
    for node in (first, second, dead):
        node.method = "aes-128-gcm"
        node.password = "test-password"
    instance = _manager([first, second, dead])

    initial = instance.runtime_candidate_snapshot()
    repeated = instance.runtime_candidate_snapshot()
    by_identity = {candidate.identity: candidate for candidate in initial.candidates}
    first_state = by_identity[instance._node_identity(first)]
    second_state = by_identity[instance._node_identity(second)]
    dead_state = by_identity[instance._node_identity(dead)]

    assert initial.runtime_generation == 7
    assert initial.revision > 0
    assert initial.health_revision == initial.revision
    assert repeated.revision == initial.revision
    assert first_state.key == (
        "tier2",
        "tier2",
        instance._node_identity(first),
    )
    assert first_state.proxy_name == ""
    assert first_state.eligible is True
    assert second_state.raw_status == NodeStatus.SLOW.value
    assert second_state.effective_status == NodeStatus.SLOW.value
    assert second_state.usable is True
    assert dead_state.eligible is False
    assert initial.candidate_for(second_state.key) is second_state
    assert initial.candidate_for((*second_state.key, "core-owned-alias")) is second_state
    rendered = repr(initial)
    assert instance._node_identity(first) not in rendered
    assert "first.example" not in rendered
    assert "identity_hash='hmac:" in rendered
    with pytest.raises(FrozenInstanceError):
        first_state.raw_status = NodeStatus.DEAD.value
    with pytest.raises(FrozenInstanceError):
        initial.candidates = ()

    quarantined = _confirmed(
        instance,
        second,
        incident="second-failed",
        pool_kind="tier2",
        proxy_name="shared-2",
    )
    assert quarantined["action"] == "quarantined"
    updated = instance.runtime_candidate_snapshot()
    current = updated.candidate_for(
        ("tier2", "tier2", instance._node_identity(second))
    )

    assert updated.revision == initial.revision + 1
    assert current is not None
    assert current.raw_status == NodeStatus.SLOW.value
    assert current.effective_status == NodeStatus.SUSPICIOUS.value
    assert current.effective_usable is False
    assert current.incident_active is True
    assert current.overlay_active is True
    assert current.pending_recheck is True
    assert current.incident_id == "second-failed"
    assert current.eligible is False
    # Previously returned values remain a frozen statement of the old health
    # revision rather than observing the live ledger through shared dicts.
    assert second_state.eligible is True


def test_runtime_candidate_snapshot_has_no_fixed_candidate_limit() -> None:
    nodes = [_node("tier2", f"candidate-{index}") for index in range(7)]
    for node in nodes:
        node.method = "aes-128-gcm"
        node.password = "test-password"
    instance = _manager(nodes)

    snapshot = instance.runtime_candidate_snapshot()

    assert len(snapshot.candidates) == 7
    assert all(candidate.eligible for candidate in snapshot.candidates)


def test_runtime_candidate_revision_tracks_raw_health_and_generation() -> None:
    node = _node("tier2", "candidate-a")
    node.method = "aes-128-gcm"
    node.password = "test-password"
    instance = _manager([node])

    initial = instance.runtime_candidate_snapshot()
    node.status = NodeStatus.SLOW
    raw_changed = instance.runtime_candidate_snapshot()
    instance.runtime_generation = 8
    generation_changed = instance.runtime_candidate_snapshot()

    assert raw_changed.revision == initial.revision + 1
    assert raw_changed.candidates[0].raw_status == NodeStatus.SLOW.value
    assert generation_changed.revision == raw_changed.revision + 1
    assert generation_changed.runtime_generation == 8


def test_runtime_candidate_snapshot_does_not_derive_core_proxy_names(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_secret = "raw-secret-node.example"
    malformed = _node(
        "tier2",
        "raw-secret-node",
        protocol="vless",
        address=raw_secret,
    )
    instance = _manager([malformed])
    monkeypatch.setattr(
        manager_module,
        "node_to_clash_proxy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("candidate health must not invoke the Core formatter")
        ),
    )

    with caplog.at_level(logging.WARNING):
        snapshot = instance.runtime_candidate_snapshot()
        logging.getLogger("candidate-snapshot-test").warning("%r", snapshot)

    assert len(snapshot.candidates) == 1
    assert snapshot.candidates[0].proxy_name == ""
    assert raw_secret not in caplog.text
    assert "raw-secret-node" not in caplog.text


def test_runtime_candidate_snapshot_rejects_ambiguous_stable_identity() -> None:
    first = _node("tier2", "duplicate", address="same.example")
    second = _node("tier2", "duplicate", address="same.example")
    instance = _manager([first, second])

    ambiguous = instance.runtime_candidate_snapshot()

    assert ambiguous.candidates == ()
    assert ambiguous.candidate_for(
        ("tier2", "tier2", instance._node_identity(first), "either-core-alias")
    ) is None

    instance.runtime_nodes["tier2"] = [first]
    resolved = instance.runtime_candidate_snapshot()

    assert resolved.revision == ambiguous.revision + 1
    assert len(resolved.candidates) == 1
    assert resolved.candidates[0].key == (
        "tier2",
        "tier2",
        instance._node_identity(first),
    )
