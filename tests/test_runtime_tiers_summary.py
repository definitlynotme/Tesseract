"""Compact, passive bridge sampling; no runtime construction or host I/O."""

import copy
import threading
import time
from unittest.mock import Mock

import pytest

from bridge_protocol import validate_frame
from bridge_runtime_access import RuntimeAccess
from desktop_runtime import DesktopRuntime
from manager import ProxyTierManager
from models import Node, NodeStatus


def node(tier, name, **changes):
    return Node(tier=tier, name=name, address=name, protocol="ss", port=443,
                status=changes.pop("status", NodeStatus.ALIVE),
                latency_ms=changes.pop("latency_ms", 50), **changes)


def manager_case():
    manager = object.__new__(ProxyTierManager)
    manager.lock = threading.RLock()
    manager.config = {"testing": {"primary_gateway_max_latency_ms": 800},
                      "tiers": {"tier3": {"enabled": True}}}
    manager.runtime_generation = 4
    manager.runtime_nodes = {tier: [] for tier in ("tier1", "tier2", "tier3", "exit")}
    manager._runtime_health_ledger = {}
    return manager


def runtime_case(manager=None):
    runtime = object.__new__(DesktopRuntime)
    runtime.manager = manager or manager_case()
    runtime.config = runtime.manager.config
    runtime._runtime_intent_lock = threading.RLock()
    runtime._runtime_generation = 4
    runtime._lifecycle_revision = 2
    runtime.running = True
    runtime._stop_request_pending = False
    runtime.core_fact = Mock(return_value={"core_generation": 2, "running": True})
    runtime.published_route_status = Mock(return_value={
        "publication_revision": 1, "managed_tier": "vanguard", "valid": True,
        "invalid_reason": None, "fail_closed_confirmed": False})
    runtime._refresh_runtime_route_snapshot = Mock(side_effect=AssertionError("refresh"))
    return runtime


def assert_wire(revision, data):
    assert validate_frame({"t": "evt", "epoch": "test", "produced_at_ms": 0,
                           "sent_at_ms": 0, "topic": "tiers.summary",
                           "kind": "snapshot", "rev": revision, "data": data}) == []


def test_four_tiers_pool_latency_and_no_clones_or_ledger_mutation(monkeypatch):
    m = manager_case()
    m.runtime_nodes = {
        "tier1": [node("tier1", "a", extra={"physical_node": "one"}),
                  node("tier1", "b", latency_ms=900, extra={"physical_node": "one"}),
                  node("tier1", "c", status=NodeStatus.DEAD)],
        "tier2": [node("tier2", "d", latency_ms=800, status=NodeStatus.SLOW),
                  node("tier2", "e", latency_ms=801)],
        "tier3": [node("tier3", "auto", latency_ms=3000, status=NodeStatus.SLOW),
                  node("tier3", "user", extra={"tier3_origin": "tier3a"}),
                  node("tier3", "tcp", extra={"startup_prelaunch_validation": "tcp"}),
                  node("tier3", "cache", extra={"tier3_cache_revalidation_pending": True})],
        "exit": [node("exit", "gate"), node("exit", "limited", extra={
            "latency_policy_state": "over_limit"})]}
    gate = m.runtime_nodes["exit"][0]
    m._runtime_health_ledger["legacy"] = {
        "tier": "exit", "identity": m._node_identity(gate), "pool_kind": "gateway"}
    before = copy.deepcopy(m._runtime_health_ledger)
    forbidden = Mock(side_effect=AssertionError("mutating/cloning helper"))
    monkeypatch.setattr(Node, "clone", forbidden)
    for method in ("_effective_node_clone_locked", "_ensure_runtime_health_state_locked",
                   "_prune_runtime_health_ledger_locked", "runtime_effective_nodes"):
        monkeypatch.setattr(m, method, forbidden)
    counts = m.compact_tiers_count_status()
    assert counts["tiers"] == {
        "tier1": {"total": 2, "usable": 1}, "tier2": {"total": 2, "usable": 1},
        "tier3": {"total": 1, "usable": 1}, "exit": {"total": 2, "usable": 0}}
    # §8u: Tier1 counts per physical node in the pool too (a, b = one node "one"; c)
    assert counts["pool"] == {"total": 7, "usable": 3}
    assert m._runtime_health_ledger == before
    forbidden.assert_not_called()
    r = runtime_case(m)
    revision, data = r.tiers_summary_status()
    assert data["degraded"] and data["carrier"] == "vanguard"
    assert_wire(revision, data)
    r._refresh_runtime_route_snapshot.assert_not_called()


def test_ledger_exact_and_ambiguous_fallback_follow_existing_lookup():
    m = manager_case()
    n = node("tier2", "same")
    m.runtime_nodes["tier2"] = [n]
    entry = {"tier": "tier2", "identity": m._node_identity(n), "pool_kind": "tier2"}
    m._runtime_health_ledger = {"a": dict(entry), "b": dict(entry)}
    assert m.compact_tiers_count_status()["tiers"]["tier2"]["usable"] == 1
    key = m._runtime_health_ledger_key("tier2", "tier2", entry["identity"], n.name)
    m._runtime_health_ledger[key] = entry
    assert m.compact_tiers_count_status()["tiers"]["tier2"]["usable"] == 0


@pytest.mark.parametrize("change", ["runtime", "lifecycle", "manager", "config",
                                    "manager_config", "manager_generation", "route", "core"])
def test_cross_domain_change_discards_sample(change):
    r = runtime_case()
    original = r.manager.compact_tiers_count_status

    def racing_count():
        result = original()
        if change == "runtime":
            r._runtime_generation += 1
        elif change == "lifecycle":
            r._lifecycle_revision += 1
        elif change == "manager":
            r.manager = manager_case()
        elif change == "config":
            r.config = copy.deepcopy(r.config)
        elif change == "manager_config":
            r.manager.config = copy.deepcopy(r.config)
        elif change == "manager_generation":
            r.manager.runtime_generation += 1
        elif change == "route":
            r.published_route_status.return_value = {
                **r.published_route_status.return_value, "publication_revision": 2}
        elif change == "core":
            r.core_fact.return_value = {"core_generation": 3, "running": True}
        return result

    r.manager.compact_tiers_count_status = racing_count
    assert r.tiers_summary_status() is None
    assert not hasattr(r, "_bridge_tiers_summary_revision")


def test_semantic_revision_and_caller_ownership():
    r = runtime_case()
    first = RuntimeAccess(r).tiers_summary_status()
    raw = r.tiers_summary_status()
    # RuntimeAccess only adds the per-tier check_interval_s / pending fields (T-P4-C2).
    additions = {"check_interval_s", "pending"}
    assert first[0] == raw[0]
    assert {k: v for k, v in first[1].items() if k != "tiers"} == {
        k: v for k, v in raw[1].items() if k != "tiers"}
    assert {name: {k: v for k, v in row.items() if k not in additions}
            for name, row in first[1]["tiers"].items()} == raw[1]["tiers"]
    first[1]["tiers"]["vanguard"]["total"] = 900
    assert r.tiers_summary_status()[0] == first[0]
    r.manager.runtime_nodes["tier2"].append(node("tier2", "new"))
    assert r.tiers_summary_status()[0] == first[0] + 1
    r.published_route_status.return_value["publication_revision"] += 1
    assert r.tiers_summary_status()[0] == first[0] + 1


@pytest.mark.parametrize("carrier", ["vanguard", "sentinel", "bastion", "gateway"])
def test_carrier_order_and_fallback_are_published_route_facts(carrier):
    r = runtime_case()
    r.published_route_status.return_value["managed_tier"] = carrier
    revision, data = r.tiers_summary_status()
    assert data["carrier"] == carrier and data["order"][0] == carrier
    assert data["fallback"] == (carrier in {"sentinel", "bastion"})
    assert data["offline_reason"] is None
    assert_wire(revision, data)


@pytest.mark.parametrize("state,reason", [("stopped", "stopped"), ("stopping", "stopping"),
    ("invalid", "route_owner_changed"), ("reject", "fail_closed"), ("direct", "unmanaged_route")])
def test_offline_stopped_and_disabled_projection(state, reason):
    r = runtime_case()
    r.config["tiers"] = {"tier3": {"enabled": False}, "exit": {
        "nodes": [{"enabled": False}], "subscription_urls": []}}
    if state == "stopped":
        r.running = False
        r.manager = None
    elif state == "stopping":
        r._stop_request_pending = True
    elif state == "invalid":
        r.published_route_status.return_value.update(valid=False, invalid_reason=reason)
    else:
        r.published_route_status.return_value.update(managed_tier=None,
                                                    fail_closed_confirmed=state == "reject")
    result = r.tiers_summary_status()
    assert result == r.tiers_summary_status()
    revision, data = result
    assert data["carrier"] is None and data["offline_reason"] == reason
    assert not data["tiers"]["bastion"]["enabled"]
    assert not data["tiers"]["gateway"]["enabled"]
    assert_wire(revision, data)


def test_route_and_core_reads_happen_outside_manager_lock():
    r = runtime_case()
    original = r.published_route_status

    def read_route():
        assert not r.manager.lock._is_owned()
        return original()

    r.published_route_status = read_route
    assert r.tiers_summary_status() is not None


def test_real_published_route_getter_is_passive_and_rejects_epoch_race():
    from tests.test_runtime_published_route import runtime_with_snapshot

    r = runtime_with_snapshot()
    r.manager = manager_case()
    r.config = r.manager.config
    r._runtime_intent_lock = threading.RLock()
    r._runtime_generation = 4
    r._lifecycle_revision = 2
    r.running = True
    r._stop_request_pending = False
    revision, data = r.tiers_summary_status()
    assert data["carrier"] == "vanguard"
    assert_wire(revision, data)
    original = r.manager.compact_tiers_count_status

    def racing_count():
        result = original()
        r._runtime_network_context_epoch += 1
        return result

    r.manager.compact_tiers_count_status = racing_count
    assert r.tiers_summary_status() is None


def test_missing_health_ledger_is_not_initialized():
    m = manager_case()
    del m._runtime_health_ledger
    m.runtime_nodes["tier2"] = [node("tier2", "one")]
    assert m.compact_tiers_count_status()["tiers"]["tier2"]["usable"] == 1
    assert not hasattr(m, "_runtime_health_ledger")


def test_800_node_lock_hold_measurement(capsys):
    m = manager_case()
    for tier in m.runtime_nodes:
        m.runtime_nodes[tier] = [node(tier, f"{tier}-{i}") for i in range(200)]

    class MeasuredLock:
        def __init__(self):
            self.lock = threading.RLock()

        def __enter__(self):
            self.lock.acquire()
            self.started = time.perf_counter_ns()

        def __exit__(self, *args):
            self.held_ns = time.perf_counter_ns() - self.started
            self.lock.release()

    m.lock = MeasuredLock()
    result = m.compact_tiers_count_status()
    assert all(row == {"total": 200, "usable": 200} for row in result["tiers"].values())
    assert result["pool"] == {"total": 600, "usable": 600}
    with capsys.disabled():
        print(f"\n800 nodes, one manager lock hold: {m.lock.held_ns / 1e6:.6f} ms")
