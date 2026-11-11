from __future__ import annotations

import pytest

from tier3_candidate_scheduler import Tier3CandidateLookaheadEvidence, Tier3CandidateScheduler
from tests.test_tier3_candidate_scheduler import candidate, feature


def _inventory(root_count: int, prefix: str = "opaque") -> Tier3CandidateScheduler:
    scheduler = Tier3CandidateScheduler(unknown_exploration_ratio=0.15)
    scheduler.set_credit_envelope(16, handoff_window=16)
    for index in range(160):
        scheduler.enqueue(
            candidate(f"item-{index}"), None, arm_key=f"arm-{index}",
            source_root=f"{prefix}-{index % root_count}", family=f"family-{index % 3}",
            coarse_key=f"coarse-{index}",
            feature_key=feature(service_profiles=(
                "l0", f"l1-{index % 4}", f"l2-{index % 40}", f"l3-{index % 80}",
            )),
        )
    return scheduler


@pytest.mark.parametrize("roots", [1, 2, 5, 17])
def test_idle_priority_share_samples_cold_inventory_without_reward(roots):
    traces = []
    for prefix in ("opaque", "renamed-in-reverse-lexical-order"):
        scheduler = _inventory(roots, prefix)
        reservations = scheduler.reserve_handoffs(16)
        traces.append([item.candidate.node.name for item in reservations])
        assert sum(r.ticket.selection_reason == "cold_leaf_coverage" for r in reservations) == 8
        assert sum(r.ticket.selection_reason == "coverage" for r in reservations) <= 4
        assert all(r.ticket.selection_reason != "reward" for r in reservations)
        assert scheduler.quick_metrics()["selected_total"] == 0
        for reservation in reservations:
            scheduler.cancel_handoff(reservation, requeue=True)
        assert scheduler.quick_metrics()["selected_total"] == 0
        assert scheduler.pending_handoff_count() == 0
        assert scheduler._handoff_lane_budget_unlocked().discovery_remaining == 8
    assert traces[0] == traces[1]


@pytest.mark.parametrize("historical", [False, True])
def test_replay_inventory_keeps_existing_discovery_budget(historical):
    scheduler = Tier3CandidateScheduler()
    scheduler.set_lookahead_evidence_provider(
        lambda _item: Tier3CandidateLookaheadEvidence(
            validated_positive=not historical, validated_reprobe=historical,
        )
    )
    scheduler.enqueue(candidate("cached"), None, arm_key="cached", source_root="root",
                      family="opaque", coarse_key="coarse")
    assert scheduler._handoff_lane_budget_unlocked().discovery_limit == 4


def test_exact_inventory_ends_idle_borrow_after_real_feedback():
    scheduler = Tier3CandidateScheduler()
    key = feature(service_profiles=("l0", "l1", "l2", "l3"))
    scheduler.enqueue(candidate("seed"), None, arm_key="shared", source_root="root",
                      family="opaque", coarse_key="shared", feature_key=key)
    selected = scheduler.reserve_handoffs(1)[0]
    scheduler.commit_handoff(selected)
    scheduler.on_tcp_outcome(selected.ticket, responsive=True)
    scheduler.on_terminal(selected.ticket, passed=True, outcome="pass", stage="fast_probe")
    scheduler.on_commit_complete(selected.ticket, net_new=True)
    scheduler.enqueue(candidate("next"), None, arm_key="shared", source_root="root",
                      family="opaque", coarse_key="shared", feature_key=key)
    assert scheduler._has_production_reward_unlocked()
    assert scheduler._handoff_lane_budget_unlocked().discovery_limit == 4


def test_idle_borrow_counts_reserved_and_started_in_the_same_window():
    scheduler = _inventory(5)
    reservations = scheduler.reserve_handoffs(16)
    for reservation in reservations[:8]:
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=False)
    assert scheduler.quick_metrics()["selected_total"] == 8
    budget = scheduler._handoff_lane_budget_unlocked()
    assert budget.discovery_limit == 8
    assert budget.discovery_remaining == 0
    for reservation in reservations[8:]:
        scheduler.cancel_handoff(reservation, requeue=True)
    assert scheduler.quick_metrics()["selected_total"] == 8
    assert scheduler.release_outstanding() == 0


def test_pending_exact_flights_do_not_hide_idle_priority_capacity():
    scheduler = Tier3CandidateScheduler(unknown_exploration_ratio=0.0)
    scheduler.set_credit_envelope(16, handoff_window=16)
    key = feature(service_profiles=("l0", "l1", "l2", "l3"))
    for index in range(10):
        scheduler.enqueue(candidate(f"exact-{index}"), None, arm_key="exact",
                          source_root="root", family="opaque", coarse_key="exact",
                          feature_key=key)
    seed = scheduler.reserve_handoffs(1)[0]
    scheduler.commit_handoff(seed)
    scheduler.on_tcp_outcome(seed.ticket, responsive=True)
    scheduler.on_terminal(seed.ticket, passed=True, outcome="pass", stage="fast_probe")
    scheduler.on_commit_complete(seed.ticket, net_new=True)

    pending = scheduler.reserve_handoffs(4)
    assert len(pending) == 4
    assert all(item.ticket.reward_scope == "exact" for item in pending)
    assert scheduler._handoff_lane_budget_unlocked().discovery_limit == 8
    for item in pending:
        scheduler.commit_handoff(item)
        scheduler.on_tcp_outcome(item.ticket, responsive=True)
    assert scheduler._handoff_lane_budget_unlocked().discovery_limit == 8
    scheduler.on_terminal(pending[0].ticket, passed=False, outcome="cancelled")
    assert scheduler._handoff_lane_budget_unlocked().discovery_limit == 4
    scheduler.release_outstanding()
