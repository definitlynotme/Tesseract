from datetime import datetime, timedelta, timezone

import pytest

from tests.test_tier3_candidate_scheduler import candidate, feature
from tier_cache import Tier3ObservationFact, Tier3ObservationKind, TierObservationCache
from tier3_candidate_scheduler import (
    Tier3CandidateLookaheadEvidence,
    Tier3CandidateScheduler,
)


@pytest.mark.parametrize("tcp_responsive", [False, True])
def test_censor_without_validation_failure_is_not_negative(tmp_path, tcp_responsive):
    cache = TierObservationCache(tmp_path / "observations.json")
    node = candidate("neutral-history")
    node.extra["tier3_origin"] = "tier3b"
    now = datetime(2026, 9, 7, tzinfo=timezone.utc)
    facts = []
    if tcp_responsive:
        facts.append(Tier3ObservationFact(
            node=node, kind=Tier3ObservationKind.TCP_RESPONSIVE,
            observed_at=(now - timedelta(seconds=1)).isoformat(),
        ))
    facts.append(Tier3ObservationFact(
        node=node, kind=Tier3ObservationKind.RIGHT_CENSORED,
        observed_at=now.isoformat(),
    ))
    cache.observe_tier3_facts(facts)
    snapshot = cache.stable_tier3_observation_snapshot(
        days=7, config_digest_provider=lambda item: item.address, now=now,
    )
    assert snapshot.classification_for(node.address) == (
        "tcp_only" if tcp_responsive else "unknown"
    )
    assert snapshot.validated_positive_record_count == 0


def seeded_scheduler(visibility):
    scheduler = Tier3CandidateScheduler(max_active_arms_per_family=1)
    scheduler.set_credit_envelope(16, handoff_window=16, tcp_execution_limit=16)
    scheduler.set_lookahead_evidence_provider(
        lambda item: Tier3CandidateLookaheadEvidence(
            tcp_only=visibility == "tcp_only" and item.node.name.startswith("peer-"),
            observed_nonpositive=(visibility == "observed_nonpositive"
                                  and item.node.name.startswith("peer-")),
        )
    )
    profiles = ("test-l0", "test-l1", "test-l2", "test-l3")

    def enqueue(name, leaf):
        scheduler.enqueue(
            candidate(name), None, arm_key=name, coarse_key=name,
            source_root="opaque-root", family="opaque-family",
            feature_key=feature(family="opaque-family", service_profiles=leaf),
        )

    enqueue("seed", profiles)
    seed = scheduler.reserve_handoffs(1)[0]
    scheduler.commit_handoff(seed)
    scheduler.on_tcp_outcome(seed.ticket, responsive=True)
    scheduler.on_terminal(seed.ticket, passed=True, outcome="pass",
                          busy_worker_ms=200, stage="fast_probe")
    scheduler.on_commit_complete(seed.ticket, net_new=False)
    for index in range(64):
        enqueue(f"ordinary-{index}", ("other-0", "other-1", f"other-2-{index}",
                                      f"other-3-{index}"))
    for index in range(9):
        enqueue(f"peer-{index}", profiles)
    return scheduler


def test_current_pass_can_confirm_tcp_only_peer_without_claiming_unknown_credit():
    scheduler = seeded_scheduler("tcp_only")
    reservations = scheduler.reserve_handoffs(16)
    confirmations = [r for r in reservations
                     if r.ticket.reward_cohort_state == "leaf_confirmation"]
    assert 1 <= len(confirmations) <= 2
    assert all(r.ticket.visibility_class == "tcp_only" for r in confirmations)
    assert all(not r.ticket.unknown_exploration for r in confirmations)
    assert sum(r.ticket.visibility_class == "unknown" for r in reservations) >= 3
    # Pre-start cancellation must not spend the confirmation flight or FIFO.
    first_peer = confirmations[0].candidate.node.name
    for reservation in reservations:
        scheduler.cancel_handoff(reservation, requeue=True)
    assert scheduler.quick_metrics()["selected_total"] == 1
    retry = scheduler.reserve_handoffs(16)
    assert first_peer in [r.candidate.node.name for r in retry
                          if r.ticket.reward_cohort_state == "leaf_confirmation"]
    scheduler.release_outstanding()
    assert scheduler.pending_handoff_count() == 0


def test_known_negative_peer_is_neither_confirmation_inventory_nor_selected():
    scheduler = seeded_scheduler("observed_nonpositive")
    assert scheduler.supply_snapshot().leaf_confirmation_reservable_count == 0
    reservations = scheduler.reserve_handoffs(16)
    assert all(r.ticket.reward_cohort_state != "leaf_confirmation" for r in reservations)
    scheduler.release_outstanding()


def test_tcp_only_confirmation_advances_only_from_real_feedback():
    scheduler = seeded_scheduler("tcp_only")
    reservations = scheduler.reserve_handoffs(16)
    confirmations = [r for r in reservations
                     if r.ticket.reward_cohort_state == "leaf_confirmation"]
    for reservation in reservations:
        if reservation not in confirmations:
            scheduler.cancel_handoff(reservation, requeue=True)
    first = confirmations[0]
    scheduler.commit_handoff(first)
    scheduler.on_tcp_outcome(first.ticket, responsive=True)
    assert scheduler.quick_metrics()["reward_service_probation_cohort_count"] == 0
    scheduler.on_terminal(first.ticket, passed=True, outcome="pass",
                          busy_worker_ms=300, stage="fast_probe")
    scheduler.on_commit_complete(first.ticket, net_new=True)
    for reservation in confirmations[1:]:
        scheduler.cancel_handoff(reservation, requeue=True)
    assert scheduler.quick_metrics()["reward_service_probation_cohort_count"] == 1
    followup = scheduler.reserve_handoffs(16)
    service = [r for r in followup if r.ticket.reward_scope == "service"]
    assert service
    assert all(r.ticket.service_cohort_keys[-1] == "service:test-l3" for r in service)
    assert len(service) <= 4
    scheduler.release_outstanding()


def test_one_thousand_mixed_confirmation_transactions_conserve_credits():
    transactions = 0
    for cycle in range(63):
        scheduler = seeded_scheduler("tcp_only")
        reservations = scheduler.reserve_handoffs(16)
        for offset, reservation in enumerate(reversed(reservations)):
            transactions += 1
            outcome = (cycle + offset) % 6
            ticket = reservation.ticket
            if outcome == 0:
                scheduler.cancel_handoff(reservation, requeue=True)
                scheduler.cancel_handoff(reservation, requeue=True)
                continue
            scheduler.commit_handoff(reservation)
            scheduler.commit_handoff(reservation)
            if outcome == 1:
                scheduler.on_tcp_outcome(ticket, responsive=False)
                scheduler.on_tcp_outcome(ticket, responsive=False)
            elif outcome == 2:
                scheduler.on_tcp_outcome(ticket, responsive=False, hard_censored=True)
            else:
                scheduler.on_tcp_outcome(ticket, responsive=True)
                if outcome == 3:
                    scheduler.on_fast_yield(ticket, right_censored=True)
                    scheduler.on_terminal(ticket, outcome="right_censored")
                elif outcome == 4:
                    scheduler.on_terminal(ticket, passed=True, outcome="pass",
                                          stage="fast_probe", busy_worker_ms=300)
                    scheduler.on_commit_complete(ticket, net_new=False)
                else:
                    scheduler.on_terminal(ticket, outcome="cancelled")
                scheduler.on_terminal(ticket, outcome="cancelled")
        scheduler.release_outstanding()
        metrics = scheduler.quick_metrics()
        assert metrics["active_ticket_count"] == 0
        assert metrics["feedback_only_ticket_count"] == 0
        assert scheduler.pending_handoff_count() == 0
        scheduler.drain_all()
        assert not scheduler._cohort_cold_queued_inventory
    assert transactions >= 1000
