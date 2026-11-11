from __future__ import annotations

import logging
import threading
from types import SimpleNamespace

import pytest

import manager as manager_module
from manager import ProxyTierManager
from models import Node, NodeStatus


def node(tier="tier2", name="leaf", **extra):
    return Node(tier=tier, name=name, protocol="ss", address=f"{name}.example", port=443, method="aes-128-gcm", password="test-only",
                status=NodeStatus.ALIVE, latency_ms=12, last_checked_at="2026-09-12T00:00:00Z", extra=extra)


def manager(nodes):
    result = ProxyTierManager.__new__(ProxyTierManager)
    result.lock = threading.RLock()
    result.operation_lock = threading.RLock()
    result._tier3_active_stream_lock = threading.RLock()
    result._tier3_cancel_event = threading.Event()
    result.runtime_generation = 7
    result.config = {}
    result.runtime_nodes = {tier: [] for tier in ("tier1", "tier2", "tier3", "exit")}
    for value in nodes:
        result.runtime_nodes[value.tier].append(value)
    result.closed = False
    result.logger = logging.getLogger(__name__)
    result._runtime_health_ledger = {}
    result._runtime_health_completed_incidents = {}
    result._runtime_leaf_recheck_inflight = set()
    result.tier3_last_collection_report = {}
    result.tier3_refresh_progress = {}
    result.tier3_runtime_overlay_unavailable_count = 0
    result.last_check_at = {tier: None for tier in result.runtime_nodes}
    result.last_check_monotonic = {tier: 0.0 for tier in result.runtime_nodes}
    result.last_urgent_check_monotonic = {tier: 0.0 for tier in result.runtime_nodes}
    result.events = []
    result.observations = []
    result.on_runtime_leaf_recheck_result = result.events.append
    result.observation_cache = SimpleNamespace(observe=lambda tier, values: result.observations.extend((tier, value.clone()) for value in values))
    result._tier3_enabled = lambda: True
    result._testing_config_for_node_checks = lambda: {}
    result._full_validation_enabled = lambda: False
    result._gateway_nodes_for_testing = lambda tier: []
    result._tier_interval = lambda tier: 300
    result._run_async = lambda value, **kwargs: value
    return result


def confirm(instance, value, incident="incident"):
    instance.apply_runtime_health_observation({
        "tier": value.tier, "pool_kind": instance._runtime_pool_kind_locked(value.tier, value),
        "identity": instance._node_identity(value), "proxy_name": value.name,
        "runtime_generation": instance.runtime_generation, "incident_id": incident,
        "quarantine_token": incident, "source": "runtime", "verdict": "confirmed",
        "failure_domain": "node",
    })


def failed(value):
    tested = value.clone()
    tested.status = NodeStatus.DEAD
    tested.latency_ms = None
    tested.last_checked_at = "2026-09-12T00:00:01Z"
    tested.suspicious_reason = "tcp refused"
    tested.extra["tier3_failure_stage"] = "tcp_dead"
    return tested


@pytest.mark.parametrize("tier", ["tier1", "tier2", "exit"])
def test_periodic_and_requested_results_share_observation_and_health(tier):
    snapshots = []
    for origin in ("scheduled", "incident_initial"):
        value = node(tier)
        instance = manager([value])
        context = instance._capture_health_check_context(tier, value, origin=origin)
        transition = instance._commit_health_check_result(context, failed(value))
        assert transition["action"] == "transferred"
        assert len(instance.observations) == 1
        assert instance.last_check_at[tier] is None
        snapshots.append((instance.runtime_nodes[tier][0].status, instance.observations[0][1].extra))
    assert snapshots[0] == snapshots[1]


@pytest.mark.parametrize("tier", ["tier1", "tier2", "exit"])
def test_actual_periodic_pass_resolves_current_incident(tier, monkeypatch):
    value = node(tier)
    instance = manager([value])
    confirm(instance, value)
    context = instance._capture_health_check_context(tier, value, origin="incident_initial")
    instance._commit_health_check_result(context, failed(value))
    assert not instance.effective_node_usable(tier, instance.runtime_nodes[tier][0])
    healthy = value.clone()
    healthy.last_checked_at = "2026-09-12T00:05:00Z"
    monkeypatch.setattr(manager_module, "test_nodes", lambda *args, **kwargs: [healthy])
    instance._manager_stop_requested = lambda: False
    instance._tier2_tier_configured = lambda: True
    instance._exit_tier_configured = lambda: True
    instance._exit_max_pool_size = lambda: 10
    instance._subscription_sources_in_nodes = lambda *args: []
    instance._mark_subscription_sources_checked = lambda *args: None
    if tier == "tier1":
        instance.check_tier1()
    elif tier == "tier2":
        ProxyTierManager.check_tier2.__wrapped__(instance)
    else:
        ProxyTierManager.check_exit.__wrapped__(instance)
    assert instance.runtime_health_snapshot()["unavailable_count"] == 0
    assert instance.events[-1]["transition"]["action"] == "recovered"
    assert instance.events[-1]["check_origin"] == "scheduled"
    assert instance.last_check_at[tier] is not None


@pytest.mark.parametrize("change", ["generation", "config", "incident"])
def test_stale_check_cannot_overwrite_node_or_publish(change):
    value = node()
    instance = manager([value])
    context = instance._capture_health_check_context("tier2", value, origin="scheduled")
    if change == "generation":
        instance.runtime_generation += 1
    elif change == "config":
        instance.runtime_nodes["tier2"][0].port += 1
    else:
        confirm(instance, value)
    assert instance._commit_health_check_result(context, failed(value))["action"] == "stale"
    assert instance.runtime_nodes["tier2"][0].status == NodeStatus.ALIVE
    assert not instance.observations
    assert not instance.events


def test_duplicate_delivery_and_older_probe_are_not_committed_twice():
    value = node()
    instance = manager([value])
    older = instance._capture_health_check_context("tier2", value, origin="scheduled")
    newer = instance._capture_health_check_context("tier2", value, origin="incident_initial")
    instance._commit_health_check_result(newer, failed(value))
    assert instance._commit_health_check_result(newer, failed(value))["action"] == "duplicate"
    assert instance._commit_health_check_result(older, value)["action"] == "stale"
    assert len(instance.observations) == len(instance.events) == 1


def test_neutral_cancellation_does_not_change_health_or_observation_cache():
    value = node()
    instance = manager([value])
    context = instance._capture_health_check_context("tier2", value, origin="incident_initial")
    cancelled = failed(value)
    cancelled.extra["tier3_failure_stage"] = "tcp_cancelled"
    assert instance._commit_health_check_result(context, cancelled)["action"] == "pending"
    assert instance.runtime_nodes["tier2"][0].status == NodeStatus.ALIVE
    assert not instance.observations


def test_suspicion_checks_without_prewriting_failure_ledger(monkeypatch):
    value = node()
    instance = manager([value])
    class InlineThread:
        def __init__(self, *, target, **kwargs):
            self.target = target
        def start(self):
            self.target()
    monkeypatch.setattr(manager_module.threading, "Thread", InlineThread)
    def test(*args, **kwargs):
        assert not instance._runtime_health_ledger
        assert instance.runtime_nodes["tier2"][0].status == NodeStatus.ALIVE
        return failed(value)
    instance._test_runtime_node_by_identity = test
    result = instance.enqueue_node_health_check({"tier": "tier2", "identity": instance._node_identity(value), "proxy_name": value.name})
    assert result["accepted"]
    assert instance.events[-1]["verdict"] == "fail"
    assert instance.events[-1]["authoritative"] is True
    assert instance.runtime_nodes["tier2"][0].status == NodeStatus.DEAD


def tier3_manager(value, threshold=3):
    instance = manager([value])
    instance.tier3_pool_revision = 1
    instance.tier3_user_pool_health_revision = 1
    instance.tier3_pool_mutation_ledger = {}
    instance.tier3_last_collection_at = None
    instance.last_collect_tier3 = 0.0
    instance.tier3_source_count = 0
    instance._tier3_pre_gate_side_effects_suppressed = lambda: False
    instance._tier3_pool_eviction_failure_threshold = lambda: threshold
    instance._sort_tier3_nodes = lambda values: list(values)
    instance._save_tier3_snapshot = lambda *args, **kwargs: None
    instance._notify_tier3_pool_committed = lambda *args, **kwargs: None
    instance._tier3_pool_change_signature = lambda values: tuple((v.name, v.status) for v in values)
    instance._tier3_background_target_alive_nodes = lambda: 1
    def replace(values, **kwargs):
        instance.runtime_nodes["tier3"] = list(values)
        return True
    instance._replace_tier3_pool_locked = replace
    return instance


def test_tier3b_independent_scheduled_checks_reach_original_eviction_threshold():
    value = node("tier3", tier3_origin="tier3b")
    instance = tier3_manager(value)
    confirm(instance, value)
    for strike in range(1, 4):
        current = instance.runtime_nodes["tier3"][0]
        context = instance._capture_health_check_context("tier3", current, origin="scheduled")
        instance._commit_tier3_health_check_result(context, failed(current))
        assert instance._commit_tier3_health_check_result(context, failed(current))["action"] == "duplicate"
        if strike < 3:
            retained = instance.runtime_nodes["tier3"][0]
            assert retained.status == NodeStatus.DEAD
            assert retained.extra["tier3_failure_stage"] == "tcp_dead"
            assert retained.extra["tier3_consecutive_failures"] == strike
    assert not instance.runtime_nodes["tier3"]
    assert instance.observations[-1][1].extra["tier3_evicted_from_pool"] is True


def test_tier3b_requested_check_uses_scheduled_full_validation_contract(monkeypatch):
    value = node("tier3", tier3_origin="tier3b")
    gateway = node("exit", name="gateway")
    instance = tier3_manager(value)
    instance._gateway_nodes_for_testing = lambda tier: [gateway]
    calls = []
    def test(values, **kwargs):
        calls.append(kwargs)
        return [failed(value)]
    monkeypatch.setattr(manager_module, "test_nodes", test)
    context = instance._capture_health_check_context("tier3", value, origin="incident_initial")
    instance._run_unbound_tier3_health_check(context, value)
    assert calls[0]["full_validation_enabled"] is True
    assert calls[0]["gateway_nodes"] == [gateway]


def test_tier1_partial_check_alert_evaluates_entire_current_physical_group():
    first, second = node("tier1", "first"), node("tier1", "second")
    instance = manager([first, second])
    instance.tier1_reported_down_nodes = set()
    groups = []
    instance._tier1_failure_alert_events = lambda values: groups.append(values) or []
    context = instance._capture_health_check_context("tier1", first, origin="incident_initial")
    instance._commit_health_check_result(context, failed(first))
    assert {value.name for value in groups[0]} == {"first", "second"}


def test_source_demand_only_when_actual_source_entries_are_dispatched():
    instance = manager([])
    demands = []
    instance.runtime_source_route_demand = demands.append
    instance._tier3_source_entry_authority = lambda entry: "source"
    instance._runtime_source_fetch_endpoints = lambda *args, **kwargs: ()
    instance._runtime_source_endpoint_map("core", [])
    instance._runtime_source_endpoint_map("bootstrap", [])
    assert not demands
    instance._runtime_source_endpoint_map("bootstrap", [{}])
    assert demands == ["bootstrap"]
    instance._runtime_source_endpoint_map("core", [{}])
    assert demands == ["bootstrap", "core"]


def test_ordinary_deferred_result_is_neutral_without_tier3_annotations():
    value = node()
    instance = manager([value])
    context = instance._capture_health_check_context("tier2", value, origin="incident_initial")
    deferred = value.clone()
    deferred.status = NodeStatus.SUSPICIOUS
    deferred.suspicious_reason = "tcp_deferred: tcp_dns_facility_saturated"
    assert instance._commit_health_check_result(context, deferred)["action"] == "pending"
    assert instance.runtime_nodes["tier2"][0].status == NodeStatus.ALIVE


def test_thread_start_failure_releases_neutral_singleflight(monkeypatch):
    value = node()
    instance = manager([value])
    class FailedThread:
        def __init__(self, **kwargs):
            pass
        def start(self):
            raise RuntimeError("thread capacity")
    monkeypatch.setattr(manager_module.threading, "Thread", FailedThread)
    with pytest.raises(RuntimeError, match="thread capacity"):
        instance.enqueue_node_health_check({"tier": "tier2", "identity": instance._node_identity(value), "proxy_name": value.name})
    assert not instance._node_health_check_inflight


def test_observation_write_holds_commit_lock_until_health_is_durable():
    value = node()
    instance = manager([value])
    context = instance._capture_health_check_context("tier2", value, origin="incident_initial")
    entered, release, reader_done = threading.Event(), threading.Event(), threading.Event()
    def observe(tier, values):
        entered.set()
        assert release.wait(2)
    instance.observation_cache.observe = observe
    worker = threading.Thread(target=lambda: instance._commit_health_check_result(context, failed(value)))
    worker.start()
    assert entered.wait(2)
    def read_committed_health():
        with instance.lock:
            reader_done.set()
    reader = threading.Thread(target=read_committed_health)
    reader.start()
    try:
        assert not reader_done.wait(0.05)
    finally:
        release.set()
        worker.join(2)
        reader.join(2)
    assert reader_done.is_set()


def test_periodic_raw_health_change_invalidates_an_older_targeted_result():
    value = node("tier3", tier3_origin="tier3b")
    instance = tier3_manager(value)
    context = instance._capture_health_check_context("tier3", value, origin="incident_initial")
    periodic = value.clone()
    periodic.last_checked_at = "2026-09-12T00:05:00Z"
    instance.runtime_nodes["tier3"] = [periodic]
    assert instance._commit_tier3_health_check_result(context, failed(value))["action"] == "stale"
    assert not instance.observations


def test_tier3a_periodic_pass_publishes_authority_and_preserves_user_pool_policy():
    from tier3_validation_protocol import Tier3TerminalKind
    value = node("tier3", tier3_origin="tier3a")
    instance = tier3_manager(value)
    confirm(instance, value)
    context = instance._capture_health_check_context("tier3", value, origin="scheduled")
    tested = value.clone()
    tested.last_checked_at = "2026-09-12T00:05:00Z"
    result = instance._apply_tier3a_health_result(
        identity=context.identity, config_digest=manager_module.tier3_candidate_config_digest(value),
        tested_node=tested, outcome=Tier3TerminalKind.PASS, failure_class="", check_context=context,
    )
    assert result.status == NodeStatus.ALIVE
    assert "tier3_consecutive_failures" not in instance.runtime_nodes["tier3"][0].extra
    assert instance.events[-1]["transition"]["action"] == "recovered"
    assert instance.runtime_health_snapshot()["unavailable_count"] == 0
