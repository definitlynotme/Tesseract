"""Transport evidence must not become cross-session validation authority."""

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from manager import ProxyTierManager
from models import NodeStatus
from tests.test_tier_cache import make_node
from tier_cache import Tier3ObservationFact, Tier3ObservationKind, TierObservationCache
from tier3_candidate_scheduler import Tier3CandidateLookaheadEvidence, Tier3CandidateScheduler


def identity(node):
    return "|".join(["tier3", *node.dedupe_key])


def snapshot(cache):
    return cache.stable_tier3_observation_snapshot(
        days=7, config_digest_provider=identity,
    )


@pytest.mark.parametrize("stage", ["tcp_dead", "tcp_dns_failed", "tcp_timeout"])
def test_previous_transport_failure_remains_eligible_for_current_source(tmp_path, stage):
    cache = TierObservationCache(tmp_path / "observations.json")
    node = make_node("current-source-node", NodeStatus.DEAD)
    node.extra["tier3_failure_stage"] = stage
    cache.observe_tier3_facts([
        Tier3ObservationFact(node=node, kind=Tier3ObservationKind.TCP_DEAD),
    ])
    reloaded = TierObservationCache(cache.path)
    assert reloaded.filter_recently_failed_tier3([node], cooldown_hours=6) == ([node], 0)
    evidence = snapshot(reloaded)
    assert evidence.classification_for(identity(node)) == "unknown"
    assert not evidence.exact_positive_config_digests


def test_legacy_tcp_negative_without_full_node_cannot_exclude_source_node(tmp_path):
    node = make_node("old-transport-only", NodeStatus.DEAD)
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps({
        "schema_version": 3, "nodes": [], "negative": [{
            "identity": identity(node), "tier": "tier3", "status": "dead",
            "failure_stage": "tcp_dns_failed",
            "last_failed_at": datetime.now(timezone.utc).isoformat(),
        }],
    }), encoding="utf-8")
    assert TierObservationCache(path).filter_recently_failed_tier3(
        [node], cooldown_hours=6,
    ) == ([node], 0)


@pytest.mark.parametrize("kind", [
    Tier3ObservationKind.DETERMINISTIC_VALIDATION_FAIL,
    Tier3ObservationKind.POOL_HEALTH_FAIL,
])
@pytest.mark.parametrize("status", [NodeStatus.DEAD, NodeStatus.UNKNOWN, NodeStatus.SUSPICIOUS])
def test_stronger_failure_survives_later_tcp_failure_and_history_pruning(tmp_path, kind, status):
    cache = TierObservationCache(tmp_path / "observations.json", history_limit=3)
    node = make_node("verified-failure", status)
    base = datetime.now(timezone.utc) - timedelta(minutes=1)
    cache.observe_tier3_facts([
        Tier3ObservationFact(node=node, kind=kind, observed_at=base.isoformat()),
        Tier3ObservationFact(node=node, kind=Tier3ObservationKind.TCP_DEAD,
                            observed_at=(base + timedelta(seconds=1)).isoformat()),
    ])
    cache.observe_tier3_facts([
        Tier3ObservationFact(node=make_node(f"unrelated-{index}", NodeStatus.DEAD),
                            kind=Tier3ObservationKind.TCP_DEAD)
        for index in range(16)
    ])
    assert TierObservationCache(cache.path).filter_recently_failed_tier3(
        [node], cooldown_hours=6,
    ) == ([], 1)


def test_old_tcp_entry_cannot_mask_authoritative_failure_still_in_observation(tmp_path):
    cache = TierObservationCache(tmp_path / "observations.json")
    node = make_node("ordered-facts", NodeStatus.DEAD)
    cache.observe_tier3_facts([
        Tier3ObservationFact(node=node, kind=Tier3ObservationKind.POOL_HEALTH_FAIL),
    ])
    payload = cache.load()
    for entry in payload["negative"]:
        entry["failure_stage"] = "tcp_dead"
    cache.path.write_text(json.dumps(payload), encoding="utf-8")
    reloaded = TierObservationCache(cache.path)
    assert reloaded.filter_recently_failed_tier3([node], cooldown_hours=6) == ([], 1)
    assert snapshot(reloaded).classification_for(identity(node)) == "observed_nonpositive"


def test_pass_then_tcp_failure_requires_reprobe_not_direct_admission(tmp_path):
    cache = TierObservationCache(tmp_path / "observations.json")
    node = make_node("trusted-node", NodeStatus.ALIVE)
    cache.observe_tier3_facts([
        Tier3ObservationFact(node=node, kind=Tier3ObservationKind.MIXED_PORT_PASS,
                            observed_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()),
        Tier3ObservationFact(node=node, kind=Tier3ObservationKind.TCP_DEAD),
    ])
    assert cache.filter_recently_failed_tier3([node], cooldown_hours=6) == ([node], 0)
    assert snapshot(cache).classification_for(identity(node)) == "validated_reprobe"


@pytest.mark.parametrize("stale_stage", ["deterministic_validation_fail", "pool_health_fail"])
def test_typed_tcp_fact_cannot_inherit_stale_validation_authority(tmp_path, stale_stage):
    cache = TierObservationCache(tmp_path / "observations.json")
    node = make_node("stale-metadata", NodeStatus.SUSPICIOUS)
    node.extra["tier3_failure_stage"] = stale_stage
    node.suspicious_reason = "mihomo_validation_failed:previous-attempt"
    cache.observe_tier3_facts([
        Tier3ObservationFact(node=node, kind=Tier3ObservationKind.TCP_DEAD),
    ])
    reloaded = TierObservationCache(cache.path)
    assert reloaded.filter_recently_failed_tier3([node], cooldown_hours=6) == ([node], 0)
    assert snapshot(reloaded).classification_for(identity(node)) == "unknown"
    negative = reloaded.load()["negative"][0]
    assert negative["failure_stage"] == "tcp_dead"
    assert negative["entry_cooldown_authority"] == "transport_only"


def test_manager_entry_gate_recovers_transport_filtered_inventory(tmp_path):
    cache = TierObservationCache(tmp_path / "observations.json")
    nodes = [make_node(f"source-{index}", NodeStatus.DEAD) for index in range(3479)]
    cache.observe_tier3_facts([
        Tier3ObservationFact(node=node, kind=Tier3ObservationKind.TCP_DEAD)
        for node in nodes
    ])
    manager = ProxyTierManager.__new__(ProxyTierManager)
    manager.observation_cache = TierObservationCache(cache.path)
    manager.logger = logging.getLogger(__name__)
    manager.config = {"tiers": {"tier3": {"negative_cache_cooldown_hours": 6}}}
    kept, skipped = manager._filter_tier3_entry_candidates(nodes, [])
    assert len(kept) == 3479
    assert skipped == 0
    evidence = snapshot(manager.observation_cache)
    assert all(evidence.classification_for(identity(node)) == "unknown" for node in kept)


@pytest.mark.parametrize("roots", [1, 2, 5, 17])
def test_reprobe_reservation_cancellation_does_not_create_extra_candidates(tmp_path, roots):
    cache = TierObservationCache(tmp_path / "observations.json")
    nodes = [make_node(f"reprobe-{index}", NodeStatus.DEAD) for index in range(64)]
    cache.observe_tier3_facts([
        Tier3ObservationFact(node=node, kind=Tier3ObservationKind.TCP_DEAD)
        for node in nodes
    ])
    evidence = snapshot(cache)

    def session():
        scheduler = Tier3CandidateScheduler(
            max_active_arms_per_family=1,
            lookahead_evidence_provider=lambda item: Tier3CandidateLookaheadEvidence(
                observed_nonpositive=evidence.classification_for(identity(item.node)) == "observed_nonpositive",
            ),
        )
        scheduler.set_credit_envelope(16, handoff_window=16, tcp_execution_limit=16)
        return scheduler

    def deliver(scheduler):
        kept, skipped = cache.filter_recently_failed_tier3(nodes, cooldown_hours=6)
        assert skipped == 0
        for index, node in enumerate(kept):
            scheduler.enqueue(node.clone(), None, arm_key=str(index), coarse_key=str(index),
                              source_root=f"opaque-{index % roots}", family="opaque")

    scheduler = session()
    deliver(scheduler)
    batch = scheduler.reserve_handoffs(16)
    assert len(batch) == 16
    assert all(item.ticket.visibility_class == "unknown" for item in batch)
    for item in reversed(batch):
        assert scheduler.cancel_handoff(item, requeue=True)
        assert not scheduler.cancel_handoff(item, requeue=True)
    assert scheduler.quick_metrics()["selected_total"] == 0
    assert scheduler.quick_metrics()["backlog_count"] == 64
    starts = 0
    for _ in range(4):
        batch = scheduler.reserve_handoffs(16)
        assert len(batch) == 16
        for item in batch:
            scheduler.commit_handoff(item)
            scheduler.on_tcp_outcome(item.ticket, responsive=False)
            starts += 1
    assert scheduler.reserve_handoffs(16) == []
    scheduler.release_outstanding()
    assert scheduler.quick_metrics()["selected_total"] == starts == 64
    assert scheduler.quick_metrics()["active_ticket_count"] == 0
    assert scheduler.quick_metrics()["tcp_handoff_lease_count"] == 0
    next_session = session()
    deliver(next_session)
    assert len(next_session.reserve_handoffs(16)) == 16
    next_session.release_outstanding()
