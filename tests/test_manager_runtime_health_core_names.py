"""Runtime-health incidents keep the Core leaf name they were recorded under.

The runtime records a confirmed failure under the Core-loaded proxy name.  A
stable Tier3 name ("base·digest6", verified provider writes) or a positional
"-2" suffix differs from the Manager's node.name, yet every authoritative
recheck must resolve that incident, release its single-flight key and report
back under the same Core name.
"""

from __future__ import annotations

import pytest

import manager as manager_module
from clash_formatter import build_clash_proxy_list
from models import NodeStatus
from node_status_view import node_identity, node_status_snapshot
from runtime_health_check import CheckContext
from tests.test_runtime_health_check_commit import failed, manager, node, tier3_manager


def _core_names(values, *, stable):
    # One shared name set, as build_full_clash_config renders every tier.
    pairs = []
    build_clash_proxy_list(values, seen_names=set(), rendered_pairs=pairs, stable_names=stable)
    return [str(proxy["name"]) for _value, proxy in pairs]


def _confirm(instance, value, core_name, *, incident="incident"):
    return instance.apply_runtime_health_observation({
        "tier": value.tier, "pool_kind": instance._runtime_pool_kind_locked(value.tier, value),
        "identity": instance._node_identity(value), "proxy_name": core_name,
        "runtime_generation": instance.runtime_generation, "incident_id": incident,
        "quarantine_token": f"token-{incident}", "core_generation": 3,
        "semantic_digest": "digest-a", "source": "business_transport",
        "verdict": "confirmed", "failure_domain": "node",
    })


def _runtime_ref(instance, value, core_name):
    # RuntimeNodeRef shape: the runtime addresses the Core-loaded leaf.
    return {
        "tier": value.tier, "pool_kind": instance._runtime_pool_kind_locked(value.tier, value),
        "identity": instance._node_identity(value), "proxy_name": core_name,
    }


def _tested(value, *, healthy=True):
    tested = value.clone() if healthy else failed(value)
    tested.last_checked_at = "2026-11-02T06:00:00Z"
    return tested


def _tier3b(values):
    instance = tier3_manager(values[0])
    instance.runtime_nodes["tier3"] = list(values)
    instance.tier3_targeted_recheck_queue = []
    # The queued targeted recheck is run inline by the test, not on a thread.
    instance.trigger_tier3_targeted_recheck = lambda: {"accepted": True}
    return instance


class _InlineThread:
    def __init__(self, *, target, args=(), **_kwargs):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


def _tier3b_nodes(case):
    if case.endswith("duplicate"):
        # Two servers that share a display name: positional names give the
        # second one "dup-2".
        first = node("tier3", "dup", tier3_origin="tier3b")
        second = node("tier3", "dup", tier3_origin="tier3b")
        second.port = 8443
        return [first, second], 1
    return [node("tier3", "auto-a", tier3_origin="tier3b")], 0


@pytest.mark.parametrize("case", ["positional", "stable", "positional-duplicate", "stable-duplicate"])
def test_tier3b_incident_under_core_name_resolves_through_targeted_recheck(case):
    values, index = _tier3b_nodes(case)
    instance = _tier3b(values)
    target = instance.runtime_nodes["tier3"][index]
    core_name = _core_names(values, stable=case.startswith("stable"))[index]
    assert (core_name == target.name) is (case == "positional")
    assert _confirm(instance, target, core_name)["action"] == "quarantined"

    delegated = instance.enqueue_runtime_leaf_recheck(
        _runtime_ref(instance, target, core_name),
        reason="runtime transport failure",
        source="runtime_coordinator",
        incident_id="incident",
        quarantine_token="token-incident",
        runtime_generation=instance.runtime_generation,
        semantic_digest="digest-a",
    )
    assert delegated["status"] == "delegated"
    assert len(instance._runtime_leaf_recheck_inflight) == 1
    instance._test_tier3_candidate_slice = lambda nodes: [_tested(item) for item in nodes]

    run = instance._run_tier3_targeted_rechecks()

    assert (run["healthy"], run["skipped"]) == (1, 0)
    assert instance.runtime_health_snapshot()["entries"] == []
    assert not instance._runtime_leaf_recheck_inflight
    recovered = [event for event in instance.events if event["transition"].get("action") == "recovered"]
    assert len(recovered) == 1
    assert recovered[0]["proxy_name"] == core_name
    current = next(
        value for value in instance.runtime_nodes["tier3"]
        if instance._node_identity(value) == instance._node_identity(target)
    )
    assert current.name == target.name
    assert instance.effective_node_usable("tier3", current)


def test_tier3b_fail_under_stable_core_name_keeps_incident_and_releases_single_flight():
    value = node("tier3", "auto-a", tier3_origin="tier3b")
    instance = _tier3b([value])
    core_name = _core_names([value], stable=True)[0]
    _confirm(instance, value, core_name)
    instance.enqueue_runtime_leaf_recheck(
        _runtime_ref(instance, value, core_name), source="runtime_coordinator",
    )
    instance._test_tier3_candidate_slice = lambda nodes: [_tested(item, healthy=False) for item in nodes]

    run = instance._run_tier3_targeted_rechecks()

    assert run["failed"] == 1
    assert instance.events[-1]["transition"]["action"] == "transferred"
    assert instance.events[-1]["proxy_name"] == core_name
    assert not instance._runtime_leaf_recheck_inflight
    entry = instance.runtime_health_snapshot()["entries"][0]
    # The 300 s Tier3 cycle delegates the retained incident again instead of
    # finding it permanently in flight.
    periodic = instance.enqueue_runtime_leaf_recheck(
        entry, reason="periodic", source="tier3_periodic_recheck",
    )
    assert periodic["status"] == "delegated"


def test_neutral_check_request_joins_incident_recorded_under_stable_core_name():
    value = node("tier3", "auto-a", tier3_origin="tier3b")
    instance = _tier3b([value])
    core_name = _core_names([value], stable=True)[0]
    _confirm(instance, value, core_name)

    result = instance.enqueue_node_health_check(
        _runtime_ref(instance, value, core_name), reason="passive",
    )

    assert result["status"] == "delegated"
    assert instance.tier3_targeted_recheck_queue[0]["runtime_recheck"]["proxy_name"] == core_name


def test_ordinary_leaf_recheck_tests_the_manager_node_behind_a_suffixed_core_name(monkeypatch):
    first = node("tier1", "edge")
    second = node("tier2", "edge")
    instance = manager([first, second])
    # Core names are unique across tiers, so the Tier2 leaf loads as "edge-2".
    core_name = _core_names([first, second], stable=False)[1]
    assert core_name == "edge-2"
    _confirm(instance, second, core_name)
    healthy = _tested(second)
    monkeypatch.setattr(manager_module, "test_nodes", lambda *args, **kwargs: [healthy])
    monkeypatch.setattr(manager_module.threading, "Thread", _InlineThread)

    started = instance.enqueue_runtime_leaf_recheck(
        _runtime_ref(instance, second, core_name), source="runtime_coordinator",
    )

    assert started["status"] == "started"
    assert instance.events[-1]["transition"]["action"] == "recovered"
    assert instance.events[-1]["proxy_name"] == core_name
    assert instance.runtime_health_snapshot()["unavailable_count"] == 0
    assert not instance._runtime_leaf_recheck_inflight


@pytest.mark.parametrize("tier", ["tier1", "tier2", "exit"])
def test_scheduled_layer_check_resolves_incident_recorded_under_suffixed_core_name(tier):
    value = node(tier, "edge")
    instance = manager([value])
    _confirm(instance, value, "edge-2")
    context = instance._capture_health_check_context(tier, value, origin="scheduled")

    transition = instance._commit_health_check_result(context, _tested(value))

    assert transition["action"] == "recovered"
    assert instance.events[-1]["proxy_name"] == "edge-2"
    assert instance.runtime_health_snapshot()["unavailable_count"] == 0


def test_tier3a_incident_under_stable_core_name_commits_user_pool_health(monkeypatch):
    from tier3_validation_protocol import Tier3TerminalKind

    value = node("tier3", "user-a", tier3_origin="tier3a")
    instance = tier3_manager(value)
    core_name = _core_names([value], stable=True)[0]
    _confirm(instance, value, core_name)

    def user_pool_check(target, *, runtime_recheck=None, check_context=None):
        # The process check's own commit step after a passing validation.
        if check_context is None:
            check_context = instance._capture_health_check_context(
                "tier3", target, origin="incident_initial", request=runtime_recheck,
            )
        return instance._apply_tier3a_health_result(
            identity=check_context.identity,
            config_digest=manager_module.tier3_candidate_config_digest(target),
            tested_node=_tested(target), outcome=Tier3TerminalKind.PASS, failure_class="",
            runtime_recheck=runtime_recheck, check_context=check_context,
        )

    instance._run_tier3a_process_health_check = user_pool_check
    monkeypatch.setattr(manager_module.threading, "Thread", _InlineThread)

    started = instance.enqueue_runtime_leaf_recheck(
        _runtime_ref(instance, value, core_name), source="runtime_coordinator",
    )

    assert started["status"] == "started"
    assert instance.events[-1]["transition"]["action"] == "recovered"
    assert instance.events[-1]["proxy_name"] == core_name
    assert instance.runtime_health_snapshot()["unavailable_count"] == 0


def test_bound_check_request_names_the_incident_leaf_and_unbound_names_the_node():
    common = dict(
        check_run_id="run", origin="incident_initial", tier="tier3", pool_kind="tier3b",
        identity="tier3|ss|auto.example|443", proxy_name="auto-a", runtime_generation=7,
        node_config_digest="digest", started_monotonic=1.0,
    )
    bound = CheckContext(**common, incident={"incident_id": "incident", "proxy_name": "auto-a·53e84f"})
    unbound = CheckContext(**common, incident={"proxy_name": "no-incident"})

    assert bound.to_request()["proxy_name"] == "auto-a·53e84f"
    assert bound.node_key == ("tier3", "tier3b", "tier3|ss|auto.example|443", "auto-a")
    assert unbound.to_request()["proxy_name"] == "auto-a"


class _HealthOwner:
    def __init__(self, entries):
        self.entries = entries

    @staticmethod
    def effective_node_status(_tier, _node):
        return NodeStatus.SUSPICIOUS

    @staticmethod
    def effective_node_usable(_tier, _node):
        return False

    def runtime_health_snapshot(self):
        return {"entries": [dict(entry) for entry in self.entries]}


def _entry(value, pool_kind, core_name, observed_at, *, pending=True):
    return {
        "tier": value.tier, "pool_kind": pool_kind, "identity": node_identity(value),
        "proxy_name": core_name, "source": "business_transport",
        "observed_at": observed_at, "pending_recheck": pending,
    }


def test_status_row_projects_incident_recorded_under_stable_core_name():
    value = node("tier3", "auto-a", tier3_origin="tier3b")
    core_name = _core_names([value], stable=True)[0]
    owner = _HealthOwner([_entry(value, "tier3b", core_name, "2026-11-02T06:00:01+00:00")])

    row = node_status_snapshot(value, health_owner=owner)

    assert (row["health_origin"], row["health_observed_at"]) == (
        "business_transport", "2026-11-02T06:00:01+00:00",
    )
    assert row["pending_recheck"] is True
    assert row["latency_ms"] is None


def test_status_row_keeps_one_server_in_both_tier3_pools_apart():
    user = node("tier3", "shared-user", tier3_origin="tier3a")
    auto = node("tier3", "shared-auto", tier3_origin="tier3b")
    auto.address = user.address
    assert node_identity(user) == node_identity(auto)
    user_core, auto_core = _core_names([user, auto], stable=True)
    owner = _HealthOwner([
        _entry(user, "tier3a", user_core, "2026-11-02T06:00:01+00:00", pending=True),
        _entry(auto, "tier3b", auto_core, "2026-11-02T06:00:02+00:00", pending=False),
    ])

    user_row = node_status_snapshot(user, health_owner=owner)
    auto_row = node_status_snapshot(auto, health_owner=owner)

    assert (user_row["health_observed_at"], user_row["pending_recheck"]) == ("2026-11-02T06:00:01+00:00", True)
    assert (auto_row["health_observed_at"], auto_row["pending_recheck"]) == ("2026-11-02T06:00:02+00:00", False)


@pytest.mark.parametrize("stable", [False, True])
def test_stale_tier3b_incident_check_releases_single_flight(stable):
    value = node("tier3", "auto-a", tier3_origin="tier3b")
    instance = _tier3b([value])
    core_name = _core_names([value], stable=stable)[0]
    _confirm(instance, value, core_name)
    instance.enqueue_runtime_leaf_recheck(
        _runtime_ref(instance, value, core_name), source="runtime_coordinator",
    )

    def moved_while_testing(nodes):
        # Another writer commits this member while the validation runs.
        instance.runtime_nodes["tier3"][0].last_checked_at = "2026-11-02T06:00:09Z"
        return [_tested(item) for item in nodes]

    instance._test_tier3_candidate_slice = moved_while_testing

    run = instance._run_tier3_targeted_rechecks()

    assert run["healthy"] == 0
    assert not instance._runtime_leaf_recheck_inflight
    entry = instance.runtime_health_snapshot()["entries"][0]
    periodic = instance.enqueue_runtime_leaf_recheck(
        entry, reason="periodic", source="tier3_periodic_recheck",
    )
    assert periodic["status"] == "delegated"


def test_scheduled_tier3a_user_pool_check_resolves_incident_under_stable_core_name():
    from tier3_validation_protocol import Tier3TerminalKind

    value = node("tier3", "user-a", tier3_origin="tier3a")
    instance = tier3_manager(value)
    core_name = _core_names([value], stable=True)[0]
    _confirm(instance, value, core_name)
    # The scheduled user-pool health pass captures its context without a request.
    context = instance._capture_health_check_context("tier3", value, origin="scheduled")

    result = instance._apply_tier3a_health_result(
        identity=context.identity, config_digest=manager_module.tier3_candidate_config_digest(value),
        tested_node=_tested(value), outcome=Tier3TerminalKind.PASS, failure_class="", check_context=context,
    )

    assert result is not None
    assert instance.events[-1]["transition"]["action"] == "recovered"
    assert instance.events[-1]["proxy_name"] == core_name
    assert instance.runtime_health_snapshot()["unavailable_count"] == 0
