from __future__ import annotations

import math
import threading
import time

import pytest

from models import Node
from tier3_candidate_scheduler import (
    Tier3CandidateDispatchTicket,
    Tier3CandidateFeatureKey,
    Tier3CandidateFeedbackTicket,
    Tier3CandidateLookaheadEvidence,
    Tier3CandidateScheduler,
    Tier3ServiceCohortEligibility,
)
from tier3_defaults import tier3_tcp_breadth_scaled_limit


def test_feedback_ticket_mapping_round_trip_is_lossless() -> None:
    ticket = Tier3CandidateFeedbackTicket(
        scheduler_generation=3,
        ticket_id=7,
        arm_key="arm",
        source_root="root",
        family="family",
        coarse_key="coarse",
        selection_reason="ordinary",
        dispatched_monotonic_ns=11,
    )

    assert Tier3CandidateFeedbackTicket.from_mapping(ticket.to_dict()) == ticket


def test_datagram_tcp_refusal_keeps_feedback_without_positive_tcp_evidence() -> None:
    scheduler = Tier3CandidateScheduler()
    scheduler.set_credit_envelope(16, handoff_window=16)
    for index in range(1000):
        scheduler.enqueue(candidate(f"datagram-{index}"), None, arm_key="opaque",
                          source_root="root", family="family", coarse_key="coarse")
        reservation = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoffs([reservation])
        scheduler.on_tcp_outcome(reservation.ticket, responsive=False, validation_required=True)
        metrics = scheduler.quick_metrics()
        assert metrics["feedback_only_ticket_count"] == 1
        assert metrics["active_ticket_count"] == 1
        assert scheduler.pending_handoff_count() == 0
        scheduler.on_tcp_outcome(reservation.ticket, responsive=False, validation_required=True)
        scheduler.on_terminal(reservation.ticket, outcome="validation_cancelled")
        scheduler.on_terminal(reservation.ticket, outcome="validation_cancelled")
    metrics = scheduler.quick_metrics()
    assert metrics["tcp_outcome_count"] == 1000
    assert metrics["active_ticket_count"] == 0
    assert metrics["feedback_only_ticket_count"] == 0
    assert metrics["tcp_start_acked_responsive_rate"] == 0


def test_validated_replay_and_reprobe_share_a_bounded_epoch_budget() -> None:
    scheduler = Tier3CandidateScheduler(unknown_exploration_ratio=0.15)
    scheduler.set_credit_envelope(16, handoff_window=16)

    def evidence(item: object) -> Tier3CandidateLookaheadEvidence:
        name = str(getattr(getattr(item, "node", None), "name", ""))
        return Tier3CandidateLookaheadEvidence(
            validated_positive=name.startswith("positive-"),
            validated_reprobe=name.startswith("reprobe-"),
        )

    scheduler.set_lookahead_evidence_provider(evidence)
    for prefix, count in (("positive", 20), ("reprobe", 12), ("unknown", 24)):
        for index in range(count):
            scheduler.enqueue(
                candidate(f"{prefix}-{index}"),
                None,
                arm_key=f"{prefix}-arm-{index}",
                source_root=f"root-{index % 5}",
                family=f"family-{index % 3}",
                coarse_key=f"coarse-{index}",
            )

    reservations = scheduler.reserve_handoffs(16)
    visibility = [item.candidate.visibility_class for item in reservations]

    assert len(reservations) == 16
    assert visibility.count("unknown") >= 3
    assert visibility.count("validated_reprobe") <= 4
    assert (
        visibility.count("validated_positive")
        + visibility.count("validated_reprobe")
        <= 12
    )
    metrics = scheduler.quick_metrics()
    assert metrics["handoff_validated_replay_limit"] == 12
    assert metrics["handoff_validated_reprobe_limit"] == 3
    assert metrics["lookahead_validated_reprobe_count"] > 0
    assert metrics["reserved_handoff_count"] == 16
    assert metrics["saturation_state"] == "handoff_window_saturated"

    for reservation in reservations:
        scheduler.cancel_handoff(reservation, requeue=False)
    assert scheduler.pending_handoff_count() == 0
    assert scheduler.release_outstanding() == 0


def test_fresh_replay_is_not_displaced_by_tcp_only_exact_reward() -> None:
    scheduler = Tier3CandidateScheduler(unknown_exploration_ratio=0.15)
    scheduler.set_credit_envelope(16, handoff_window=16)
    scheduler.set_lookahead_evidence_provider(lambda item: Tier3CandidateLookaheadEvidence(
        validated_positive=item.node.name.startswith("positive-"),
        tcp_only=item.node.name.startswith("weak-"),
    ))
    exact_feature = feature(service_profile="shared-shape")

    def enqueue(name, *, same_arm=True):
        scheduler.enqueue(candidate(name), None,
            arm_key="shared-arm" if same_arm else name,
            source_root="opaque-root", family="opaque-family",
            coarse_key="shared-coarse" if same_arm else name,
            feature_key=exact_feature if same_arm else feature(service_profile="other-shape"))

    enqueue("positive-seed")
    seed = scheduler.reserve_handoffs(1)[0]
    scheduler.commit_handoff(seed)
    scheduler.on_tcp_outcome(seed.ticket, responsive=True)
    scheduler.on_terminal(seed.ticket, passed=True, stage="fast_probe")
    scheduler.on_commit_complete(seed.ticket, net_new=True)
    for index in range(32):
        enqueue(f"weak-{index}")
        enqueue(f"positive-{index}")
        enqueue(f"unknown-{index}", same_arm=False)

    reservations = scheduler.reserve_handoffs(16)
    try:
        lanes = [item.ticket.visibility_class for item in reservations]
        assert len(reservations) == 16
        assert lanes.count("validated_positive") == 12
        assert lanes.count("unknown") >= 3
        assert lanes.count("tcp_only") <= 1  # Existing bounded discovery may use an unknown slot.
        assert not any(item.ticket.reward_scope == "exact" for item in reservations)
        assert scheduler.quick_metrics()["selected_total"] == 1
        scheduler.commit_handoffs(reservations)
        assert scheduler.quick_metrics()["selected_total"] == 17
    finally:
        scheduler.release_outstanding()


def test_historical_only_inventory_reclaims_empty_fresh_and_unknown_slots() -> None:
    scheduler = Tier3CandidateScheduler(unknown_exploration_ratio=0.15)
    scheduler.set_credit_envelope(16, handoff_window=16)
    scheduler.set_lookahead_evidence_provider(
        lambda _item: Tier3CandidateLookaheadEvidence(validated_reprobe=True)
    )
    for index in range(25):
        scheduler.enqueue(
            candidate(f"historical-only-{index}"),
            None,
            arm_key=f"historical-only-arm-{index}",
            source_root=f"root-{index % 5}",
            family=f"family-{index % 3}",
            coarse_key=f"historical-only-coarse-{index}",
        )

    reservations = scheduler.reserve_handoffs(16)

    assert len(reservations) == 16
    assert all(
        item.ticket.visibility_class == "validated_reprobe"
        for item in reservations
    )
    assert sum(
        item.ticket.selection_reason == "ordinary_spillover"
        for item in reservations
    ) >= 12
    for reservation in reservations:
        scheduler.cancel_handoff(reservation, requeue=False)
    assert scheduler.release_outstanding() == 0


def test_tcp_execution_capacity_is_independent_from_rolling_handoff_window() -> None:
    scheduler = Tier3CandidateScheduler(unknown_exploration_ratio=0.15)
    scheduler.set_credit_envelope(
        16,
        handoff_window=64,
        tcp_execution_limit=16,
    )
    for index in range(80):
        scheduler.enqueue(
            candidate(f"execution-{index}"),
            None,
            arm_key=f"arm-{index}",
            source_root=f"root-{index % 5}",
            family=f"family-{index % 3}",
            coarse_key=f"coarse-{index}",
        )

    first = scheduler.reserve_handoffs(64)
    assert len(first) == 16
    supply = scheduler.supply_snapshot()
    assert supply.handoff_window_limit == 64
    assert supply.tcp_execution_limit == 16
    assert supply.tcp_handoff_lease_count == 16
    assert scheduler.reserve_handoffs(1) == []

    # Keep margin above the scheduler's 50 ms protocol floor on Windows,
    # where the test thread may resume on the same coarse timer tick.
    time.sleep(0.12)
    reclaimed, closed = scheduler.reconcile_tcp_execution_leases(
        frozenset(),
        stale_after_seconds=0.05,
    )
    assert len(reclaimed) == 16
    assert closed == ()
    assert scheduler.pending_handoff_count() == 0
    assert len(scheduler.reserve_handoffs(16)) == 16


def test_fresh_and_historical_replay_quotas_demote_from_resolved_outcomes() -> None:
    def run_wave(
        scheduler: Tier3CandidateScheduler,
        *,
        unknown_succeeds: bool,
    ) -> None:
        reservations = scheduler.reserve_handoffs(16)
        assert len(reservations) == 16
        for reservation in reservations:
            scheduler.commit_handoff(reservation)
            scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
            succeeds = bool(
                unknown_succeeds
                and reservation.ticket.visibility_class == "unknown"
            )
            scheduler.on_terminal(
                reservation.ticket,
                passed=succeeds,
                outcome="pass" if succeeds else "candidate_fail",
                busy_worker_ms=200,
                stage="fast_probe",
            )
            if succeeds:
                scheduler.on_commit_complete(
                    reservation.ticket,
                    net_new=True,
                )

    fresh = Tier3CandidateScheduler(unknown_exploration_ratio=0.15)
    fresh.set_lookahead_evidence_provider(
        lambda item: Tier3CandidateLookaheadEvidence(
            validated_positive=item.node.name.startswith("fresh-"),
        )
    )
    for prefix in ("fresh", "unknown"):
        for index in range(40):
            fresh.enqueue(
                candidate(f"{prefix}-{index}"),
                None,
                arm_key=f"{prefix}-{index}",
                source_root=f"root-{index % 5}",
                family="opaque-family",
                coarse_key=f"{prefix}-{index}",
            )

    run_wave(fresh, unknown_succeeds=True)
    run_wave(fresh, unknown_succeeds=True)
    fresh_metrics = fresh.quick_metrics()
    assert fresh_metrics["visibility_validated_positive_resolved_total"] >= 16
    assert fresh_metrics["visibility_validated_positive_net_new_total"] == 0
    assert fresh_metrics["fresh_replay_adaptive_quota"] == 4

    historical = Tier3CandidateScheduler(unknown_exploration_ratio=0.15)
    historical.set_lookahead_evidence_provider(
        lambda item: Tier3CandidateLookaheadEvidence(
            validated_reprobe=item.node.name.startswith("historical-"),
        )
    )
    for prefix, count in (("historical", 40), ("unknown", 80)):
        for index in range(count):
            historical.enqueue(
                candidate(f"{prefix}-{index}"),
                None,
                arm_key=f"{prefix}-{index}",
                source_root=f"root-{index % 5}",
                family="opaque-family",
                coarse_key=f"{prefix}-{index}",
            )

    run_wave(historical, unknown_succeeds=False)
    run_wave(historical, unknown_succeeds=False)
    run_wave(historical, unknown_succeeds=False)
    historical_metrics = historical.quick_metrics()
    assert historical_metrics["visibility_validated_reprobe_resolved_total"] >= 8
    assert historical_metrics["visibility_validated_reprobe_net_new_total"] == 0
    assert historical_metrics["historical_reprobe_adaptive_quota"] == 1
    after_demotion = historical.reserve_handoffs(16)
    assert sum(
        item.ticket.visibility_class == "validated_reprobe"
        for item in after_demotion
    ) <= 1
    for reservation in after_demotion:
        historical.cancel_handoff(reservation, requeue=False)
    assert historical.release_outstanding() == 0


def test_fresh_replay_uses_recent_windows_after_early_success() -> None:
    scheduler = Tier3CandidateScheduler(unknown_exploration_ratio=0.15)
    scheduler.set_lookahead_evidence_provider(
        lambda _item: Tier3CandidateLookaheadEvidence(
            validated_positive=True,
        )
    )
    for index in range(32):
        scheduler.enqueue(
            candidate(f"fresh-window-{index}"),
            None,
            arm_key=f"fresh-window-{index}",
            source_root=f"root-{index % 4}",
            family="opaque-family",
            coarse_key=f"fresh-window-{index}",
        )

    def resolve(count: int, *, succeeds: bool) -> None:
        remaining = count
        while remaining > 0:
            reservations = scheduler.reserve_handoffs(min(8, remaining))
            assert reservations
            remaining -= len(reservations)
            for reservation in reservations:
                scheduler.commit_handoff(reservation)
                scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
                scheduler.on_terminal(
                    reservation.ticket,
                    passed=succeeds,
                    outcome="pass" if succeeds else "candidate_fail",
                    busy_worker_ms=200,
                    stage="fast_probe",
                )
                if succeeds:
                    scheduler.on_commit_complete(
                        reservation.ticket,
                        net_new=True,
                    )

    resolve(8, succeeds=True)
    assert scheduler.quick_metrics()["fresh_replay_adaptive_quota"] == 12
    resolve(8, succeeds=False)
    resolve(8, succeeds=False)
    metrics = scheduler.quick_metrics()
    assert metrics["visibility_validated_positive_net_new_total"] == 8
    assert metrics["visibility_validated_positive_resolved_total"] == 24
    assert metrics["fresh_replay_adaptive_quota"] == 4
    assert scheduler.release_outstanding() == 0


def test_replay_censor_is_exposure_and_requeue_clears_false_exhaustion() -> None:
    scheduler = Tier3CandidateScheduler(unknown_exploration_ratio=0.15)
    scheduler.set_lookahead_evidence_provider(
        lambda _item: Tier3CandidateLookaheadEvidence(
            validated_positive=True,
        )
    )
    scheduler.enqueue(
        candidate("fresh-censored"),
        None,
        arm_key="fresh-censored",
        source_root="opaque-root",
        family="opaque-family",
        coarse_key="fresh-censored",
    )

    censored = scheduler.reserve_handoffs(1)[0]
    assert scheduler.commit_handoff(censored)
    scheduler.on_tcp_outcome(censored.ticket, responsive=True)
    scheduler.on_terminal(
        censored.ticket,
        outcome="right_censored",
        busy_worker_ms=3000,
        stage="fast_probe",
    )
    metrics = scheduler.quick_metrics()
    assert metrics["visibility_validated_positive_resolved_total"] == 0
    assert metrics["visibility_validated_positive_censored_total"] == 1
    assert metrics["fresh_replay_adaptive_quota"] == 12
    assert metrics["active_ticket_count"] == 0
    scheduler.enqueue(
        candidate("fresh-after-censor"),
        None,
        arm_key="fresh-after-censor",
        source_root="opaque-root",
        family="opaque-family",
        coarse_key="fresh-after-censor",
    )
    after_censor = scheduler.reserve_handoffs(1)
    post_censor_metrics = scheduler.quick_metrics()
    assert after_censor, ", ".join(
        f"{key}={post_censor_metrics[key]!r}"
        for key in (
            "backlog_count",
            "active_root_count",
            "active_arm_count",
            "normal_reservable_count",
            "lookahead_indexed_count",
            "lookahead_validated_positive_count",
            "fast_supply_state",
            "tcp_handoff_lease_count",
            "feedback_only_ticket_count",
            "parked_deep_debt",
        )
    )
    assert scheduler.cancel_handoff(after_censor[0], requeue=False)

    requeue_scheduler = Tier3CandidateScheduler(unknown_exploration_ratio=0.15)
    requeue_scheduler.set_lookahead_evidence_provider(
        lambda _item: Tier3CandidateLookaheadEvidence(
            validated_positive=True,
        )
    )
    requeue_scheduler.enqueue(
        candidate("fresh-requeued"),
        None,
        arm_key="fresh-requeued",
        source_root="opaque-root",
        family="opaque-family",
        coarse_key="fresh-requeued",
    )
    reservation = requeue_scheduler.reserve_handoffs(1)[0]
    assert requeue_scheduler.cancel_handoff(reservation, requeue=True)
    metrics = requeue_scheduler.quick_metrics()
    assert metrics["lookahead_validated_positive_count"] == 1
    assert metrics["visibility_validated_positive_exhausted_at_ms"] is None

    reservation = requeue_scheduler.reserve_handoffs(1)[0]
    assert requeue_scheduler.cancel_handoff(reservation, requeue=False)
    metrics = requeue_scheduler.quick_metrics()
    assert metrics["lookahead_validated_positive_count"] == 0
    assert isinstance(
        metrics["visibility_validated_positive_exhausted_at_ms"],
        int,
    )
    assert requeue_scheduler.release_outstanding() == 0


def test_replay_passes_do_not_promote_cold_l3_learning() -> None:
    scheduler = Tier3CandidateScheduler(
        unknown_exploration_ratio=0.15,
        exploit_scan_share=0.75,
    )
    scheduler.set_lookahead_evidence_provider(
        lambda item: Tier3CandidateLookaheadEvidence(
            validated_positive=item.node.name.startswith("replay-"),
        )
    )
    profiles = ("isolated-l0", "isolated-l1", "isolated-l2", "isolated-l3")
    for index in range(2):
        scheduler.enqueue(
            candidate(f"replay-{index}"),
            None,
            arm_key=f"replay-arm-{index}",
            source_root=f"replay-root-{index}",
            family="opaque-family",
            coarse_key=f"replay-coarse-{index}",
            feature_key=feature(
                family="opaque-family",
                service_profiles=profiles,
            ),
        )
    for reservation in scheduler.reserve_handoffs(2):
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
        scheduler.on_terminal(
            reservation.ticket,
            passed=True,
            outcome="pass",
            busy_worker_ms=200,
            stage="fast_probe",
        )
        scheduler.on_commit_complete(reservation.ticket, net_new=True)

    metrics = scheduler.quick_metrics()
    assert metrics["replay_fast_observation_pass_count"] == 2
    assert metrics["cold_fast_observation_pass_count"] == 0
    assert not any(
        row["cohort_id"] == "isolated-l3"
        for row in metrics["reward_service_top_cohorts"]
    )

    scheduler.enqueue(
        candidate("cold-seed-after-replay"),
        None,
        arm_key="cold-seed-after-replay",
        source_root="cold-root",
        family="opaque-family",
        coarse_key="cold-seed-after-replay",
        feature_key=feature(
            family="opaque-family",
            service_profiles=profiles,
        ),
    )
    seed = scheduler.reserve_handoffs(1)[0]
    scheduler.commit_handoff(seed)
    scheduler.on_tcp_outcome(seed.ticket, responsive=True)
    scheduler.on_terminal(
        seed.ticket,
        passed=True,
        outcome="pass",
        busy_worker_ms=200,
        stage="fast_probe",
    )
    scheduler.on_commit_complete(seed.ticket, net_new=True)
    for index in range(15):
        scheduler.enqueue(
            candidate(f"cold-filler-{index}"),
            None,
            arm_key=f"cold-filler-{index}",
            source_root=f"cold-filler-root-{index % 3}",
            family="opaque-family",
            coarse_key=f"cold-filler-{index}",
            feature_key=feature(
                family="opaque-family",
                service_profiles=(
                    "filler-l0",
                    f"filler-l1-{index}",
                    f"filler-l2-{index}",
                    f"filler-l3-{index}",
                ),
            ),
        )
    scheduler.enqueue(
        candidate("cold-confirm-after-replay"),
        None,
        arm_key="cold-confirm-after-replay",
        source_root="cold-root-2",
        family="opaque-family",
        coarse_key="cold-confirm-after-replay",
        feature_key=feature(
            family="opaque-family",
            service_profiles=profiles,
        ),
    )

    reservations = scheduler.reserve_handoffs(16)
    confirmation_candidates = [
        item
        for item in reservations
        if item.candidate.node.name == "cold-confirm-after-replay"
    ]
    assert len(confirmation_candidates) == 1
    target_row = next(
        row
        for row in scheduler.quick_metrics()["reward_service_top_cohorts"]
        if row["cohort_id"] == "isolated-l3"
    )
    assert target_row["eligibility"] == "leaf_confirmation"
    assert scheduler.quick_metrics()["cold_fast_observation_pass_count"] == 1
    for reservation in reservations:
        if reservation not in confirmation_candidates:
            scheduler.cancel_handoff(reservation, requeue=False)
    confirmation = confirmation_candidates[0]
    scheduler.commit_handoff(confirmation)
    scheduler.on_tcp_outcome(confirmation.ticket, responsive=True)
    scheduler.on_terminal(
        confirmation.ticket,
        passed=True,
        outcome="pass",
        busy_worker_ms=200,
        stage="fast_probe",
    )
    scheduler.on_commit_complete(confirmation.ticket, net_new=True)
    target_row = next(
        row
        for row in scheduler.quick_metrics()["reward_service_top_cohorts"]
        if row["cohort_id"] == "isolated-l3"
    )
    assert target_row["eligibility"] == "leaf_probation"
    assert scheduler.quick_metrics()["cold_fast_observation_pass_count"] == 2
    assert scheduler.release_outstanding() == 0


@pytest.mark.parametrize("late_confirmation_failure", [False, True])
def test_default_cold_path_uses_bounded_leaf_holdout_then_exact_l3(
    late_confirmation_failure: bool,
) -> None:
    scheduler = Tier3CandidateScheduler(
        unknown_exploration_ratio=0.15,
        exploit_scan_share=0.75,
    )
    target_profiles = ("cold-l0", "cold-l1", "cold-l2", "cold-l3")
    sibling_profiles = (
        "cold-l0",
        "cold-l1",
        "cold-l2",
        "cold-l3-sibling",
    )
    scheduler.enqueue(
        candidate("cold-seed"),
        None,
        arm_key="cold-seed",
        source_root="root-seed",
        family="opaque-family",
        coarse_key="cold-seed",
        feature_key=feature(
            family="opaque-family",
            service_profiles=target_profiles,
        ),
    )
    seed = scheduler.reserve_handoffs(1)[0]
    scheduler.commit_handoff(seed)
    scheduler.on_tcp_outcome(seed.ticket, responsive=True)
    scheduler.on_terminal(
        seed.ticket,
        passed=True,
        outcome="pass",
        busy_worker_ms=200,
        stage="fast_probe",
    )
    scheduler.on_commit_complete(seed.ticket, net_new=True)

    for index in range(8):
        scheduler.enqueue(
            candidate(f"cold-target-{index}"),
            None,
            arm_key=f"cold-target-{index}",
            source_root=f"root-target-{index % 3}",
            family="opaque-family",
            coarse_key=f"cold-target-{index}",
            feature_key=feature(
                family="opaque-family",
                service_profiles=target_profiles,
            ),
        )
        scheduler.enqueue(
            candidate(f"cold-sibling-{index}"),
            None,
            arm_key=f"cold-sibling-{index}",
            source_root=f"root-sibling-{index % 3}",
            family="opaque-family",
            coarse_key=f"cold-sibling-{index}",
            feature_key=feature(
                family="opaque-family",
                service_profiles=sibling_profiles,
            ),
        )
    for index in range(24):
        scheduler.enqueue(
            candidate(f"cold-ordinary-{index}"),
            None,
            arm_key=f"cold-ordinary-{index}",
            source_root=f"root-ordinary-{index % 5}",
            family="opaque-family",
            coarse_key=f"cold-ordinary-{index}",
            feature_key=feature(
                family="opaque-family",
                service_profiles=(
                    "ordinary-l0",
                    f"ordinary-l1-{index}",
                    f"ordinary-l2-{index}",
                    f"ordinary-l3-{index}",
                ),
            ),
        )

    first = scheduler.reserve_handoffs(16)
    confirmations = [
        item
        for item in first
        if item.ticket.reward_cohort_state == "leaf_confirmation"
    ]
    assert len(confirmations) == 2
    confirmation = confirmations[0]
    assert confirmation.ticket.reward_cohort_key == "service:cold-l3"
    assert "cold-target-" in confirmation.candidate.node.name
    for item in first:
        if item is not confirmation:
            if late_confirmation_failure and item is confirmations[1]:
                scheduler.commit_handoff(item)
            else:
                scheduler.cancel_handoff(item, requeue=True)
    scheduler.commit_handoff(confirmation)
    scheduler.on_tcp_outcome(confirmation.ticket, responsive=True)
    scheduler.on_terminal(
        confirmation.ticket,
        passed=True,
        outcome="pass",
        busy_worker_ms=200,
        stage="fast_probe",
    )
    scheduler.on_commit_complete(confirmation.ticket, net_new=True)

    probation_wave = scheduler.reserve_handoffs(16)
    probation = [
        item
        for item in probation_wave
        if item.ticket.reward_cohort_state == "leaf_probation"
    ]
    assert len(probation) == 4
    assert {
        item.ticket.reward_cohort_key for item in probation
    } == {"service:cold-l3"}
    assert all("cold-target-" in item.candidate.node.name for item in probation)
    assert all(
        item.ticket.reward_cohort_state == "parent_discovery"
        for item in probation_wave
        if item.ticket.reward_cohort_key == "service:cold-l2"
    )

    # A second epoch cannot over-issue this cohort before holdout feedback.
    concurrent = scheduler.reserve_handoffs(16)
    assert all(
        item.ticket.reward_cohort_state != "leaf_probation"
        or item.ticket.reward_cohort_key != "service:cold-l3"
        for item in concurrent
    )
    for item in concurrent:
        scheduler.cancel_handoff(item, requeue=True)

    probation_passes = 0
    for item in probation_wave:
        if item not in probation:
            scheduler.cancel_handoff(item, requeue=True)
            continue
        scheduler.commit_handoff(item)
        scheduler.on_tcp_outcome(item.ticket, responsive=True)
        net_new = probation_passes < 2
        scheduler.on_terminal(
            item.ticket,
            passed=net_new,
            outcome="pass" if net_new else "candidate_fail",
            busy_worker_ms=200,
            stage="fast_probe",
        )
        if net_new:
            probation_passes += 1
            scheduler.on_commit_complete(item.ticket, net_new=True)
            if probation_passes == 2 and item is probation[1]:
                leaf = next(
                    row
                    for row in scheduler.quick_metrics()["reward_service_top_cohorts"]
                    if row["cohort_id"] == "cold-l3"
                )
                assert leaf["eligibility"] == "proven_50"

    if late_confirmation_failure:
        # This flight belongs to the earlier 1/1 confirmation state, not the
        # independently established production state that now has two net-new.
        scheduler.on_tcp_outcome(confirmations[1].ticket, responsive=False)

    leaf = next(
        row
        for row in scheduler.quick_metrics()["reward_service_top_cohorts"]
        if row["cohort_id"] == "cold-l3"
    )
    assert leaf["eligibility"] == "proven_50"
    assert scheduler.release_outstanding() == 0


def test_default_cold_leaf_requires_one_confirmation_in_same_stratum() -> None:
    scheduler = Tier3CandidateScheduler(
        unknown_exploration_ratio=0.15,
        exploit_scan_share=0.75,
    )
    profiles = ("confirm-l0", "confirm-l1", "confirm-l2", "confirm-l3")
    scheduler.enqueue(
        candidate("confirm-seed"),
        None,
        arm_key="confirm-seed",
        source_root="same-root",
        family="opaque-family",
        coarse_key="confirm-seed",
        feature_key=feature(
            family="opaque-family",
            service_profiles=profiles,
        ),
    )
    seed = scheduler.reserve_handoffs(1)[0]
    scheduler.commit_handoff(seed)
    scheduler.on_tcp_outcome(seed.ticket, responsive=True)
    scheduler.on_terminal(
        seed.ticket,
        passed=True,
        outcome="pass",
        busy_worker_ms=200,
        stage="fast_probe",
    )
    scheduler.on_commit_complete(seed.ticket, net_new=False)

    for index in range(6):
        scheduler.enqueue(
            candidate(f"confirm-candidate-{index}"),
            None,
            arm_key=f"confirm-candidate-{index}",
            source_root="same-root",
            family="opaque-family",
            coarse_key=f"confirm-candidate-{index}",
            feature_key=feature(
                family="opaque-family",
                service_profiles=profiles,
            ),
        )

    first = scheduler.reserve_handoffs(16)
    confirmations = [
        item
        for item in first
        if item.ticket.reward_cohort_state == "leaf_confirmation"
    ]
    assert len(confirmations) == 1
    confirmation = confirmations[0]
    for item in first:
        if item is not confirmation:
            scheduler.cancel_handoff(item, requeue=True)

    concurrent = scheduler.reserve_handoffs(16)
    assert all(
        item.ticket.reward_cohort_state != "leaf_confirmation"
        for item in concurrent
    )
    for item in concurrent:
        scheduler.cancel_handoff(item, requeue=True)

    scheduler.commit_handoff(confirmation)
    scheduler.on_tcp_outcome(confirmation.ticket, responsive=True)
    scheduler.on_terminal(
        confirmation.ticket,
        passed=True,
        outcome="pass",
        busy_worker_ms=200,
        stage="fast_probe",
    )
    scheduler.on_commit_complete(confirmation.ticket, net_new=False)

    leaf = next(
        row
        for row in scheduler.quick_metrics()["reward_service_top_cohorts"]
        if row["cohort_id"] == "confirm-l3"
    )
    assert leaf["eligibility"] == "leaf_probation"
    assert leaf["confirmation_selection_total"] == 1
    assert scheduler.release_outstanding() == 0


def test_default_cross_strata_leaf_requires_confirmation_before_probation() -> None:
    scheduler = Tier3CandidateScheduler(
        unknown_exploration_ratio=0.15,
        exploit_scan_share=0.75,
    )
    profiles = ("strata-l0", "strata-l1", "strata-l2", "strata-l3")
    scheduler.enqueue(
        candidate("strata-seed"),
        None,
        arm_key="strata-seed",
        source_root="seed-root",
        family="opaque-family",
        coarse_key="strata-seed",
        feature_key=feature(
            family="opaque-family",
            service_profiles=profiles,
        ),
    )
    seed = scheduler.reserve_handoffs(1)[0]
    scheduler.commit_handoff(seed)
    scheduler.on_tcp_outcome(seed.ticket, responsive=True)
    scheduler.on_terminal(
        seed.ticket,
        passed=True,
        outcome="pass",
        busy_worker_ms=200,
        stage="fast_probe",
    )
    scheduler.on_commit_complete(seed.ticket, net_new=False)

    for index in range(6):
        scheduler.enqueue(
            candidate(f"strata-candidate-{index}"),
            None,
            arm_key=f"strata-candidate-{index}",
            source_root="independent-root",
            family="opaque-family",
            coarse_key=f"strata-candidate-{index}",
            feature_key=feature(
                family="opaque-family",
                service_profiles=profiles,
            ),
        )

    with scheduler._lock:
        state = scheduler._cohort_yield_states["service:strata-l3"]
        assert state.eligibility is (
            Tier3ServiceCohortEligibility.LEAF_CONFIRMATION
        )
        assert state.last_decision_reason == "sparse_1_of_1_confirmation"

    reservations = scheduler.reserve_handoffs(16)
    confirmations = [
        item
        for item in reservations
        if item.ticket.reward_cohort_state == "leaf_confirmation"
    ]
    assert len(confirmations) == 2
    for item in reservations:
        if item not in confirmations:
            scheduler.cancel_handoff(item, requeue=True)

    censored_confirmation, passing_confirmation = confirmations
    scheduler.commit_handoff(censored_confirmation)
    scheduler.on_tcp_outcome(censored_confirmation.ticket, responsive=True)
    scheduler.on_terminal(
        censored_confirmation.ticket,
        passed=False,
        outcome="validation_timeout",
        busy_worker_ms=200,
        stage="fast_probe",
    )
    scheduler.commit_handoff(passing_confirmation)
    scheduler.on_tcp_outcome(passing_confirmation.ticket, responsive=True)
    scheduler.on_terminal(
        passing_confirmation.ticket,
        passed=True,
        outcome="pass",
        busy_worker_ms=200,
        stage="fast_probe",
    )
    scheduler.on_commit_complete(passing_confirmation.ticket, net_new=False)

    with scheduler._lock:
        state = scheduler._cohort_yield_states["service:strata-l3"]
        assert state.eligibility is (
            Tier3ServiceCohortEligibility.LEAF_PROBATION
        )
        assert state.confirmation_selection_total == 2
        assert state.confirmation_censored_total == 1
        assert state.confirmation_outstanding_total == 0
    assert scheduler.release_outstanding() == 0


@pytest.mark.parametrize("include_unseen", [False, True])
def test_default_confirmation_spreads_across_qualified_leaves(include_unseen: bool) -> None:
    scheduler = Tier3CandidateScheduler(
        unknown_exploration_ratio=0.15,
        exploit_scan_share=0.75,
    )
    strong = ("shared-l0", "strong-l1", "strong-l2", "strong-l3")
    weak = ("shared-l0", "weak-l1", "weak-l2", "weak-l3")

    for label, profiles, root in (
        ("strong", strong, "strong-seed-root"),
        ("weak", weak, "weak-seed-root"),
    ):
        scheduler.enqueue(
            candidate(f"{label}-seed"),
            None,
            arm_key=f"{label}-seed",
            source_root=root,
            family="opaque-family",
            coarse_key=f"{label}-seed",
            feature_key=feature(
                family="opaque-family",
                service_profiles=profiles,
            ),
        )
        seed = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(seed)
        scheduler.on_tcp_outcome(seed.ticket, responsive=True)
        scheduler.on_terminal(
            seed.ticket,
            passed=True,
            outcome="pass",
            busy_worker_ms=200,
            stage="fast_probe",
        )
        scheduler.on_commit_complete(seed.ticket, net_new=False)

    for index in range(8):
        scheduler.enqueue(
            candidate(f"strong-candidate-{index}"),
            None,
            arm_key=f"strong-candidate-{index}",
            source_root="strong-inventory-root",
            family="opaque-family",
            coarse_key=f"strong-candidate-{index}",
            feature_key=feature(
                family="opaque-family",
                service_profiles=strong,
            ),
        )
        scheduler.enqueue(
            candidate(f"weak-candidate-{index}"),
            None,
            arm_key=f"weak-candidate-{index}",
            source_root=f"weak-root-{index % 3}",
            family="opaque-family",
            coarse_key=f"weak-candidate-{index}",
            feature_key=feature(
                family="opaque-family",
                service_profiles=weak,
            ),
        )

    if include_unseen:
        for index in range(12):
            scheduler.enqueue(
                candidate(f"novel-{index}"), None,
                arm_key=f"novel-{index}", source_root=f"novel-root-{index % 3}",
                family="opaque-family", coarse_key=f"novel-{index}",
                feature_key=feature(
                    family="opaque-family",
                    service_profiles=("novel-l0", "novel-l1", "novel-l2", f"novel-{index}"),
                ),
            )

    reservations = scheduler.reserve_handoffs(16)
    confirmations = [
        item
        for item in reservations
        if item.ticket.reward_cohort_state == "leaf_confirmation"
    ]
    # Cold idle share expands sampling, not the four global FAST permits.
    assert len(confirmations) == 4
    assert {
        item.ticket.reward_cohort_key for item in confirmations
    } == {"service:strong-l3", "service:weak-l3"}
    assert all(
        sum(
            item.ticket.reward_cohort_key == cohort_key
            for item in confirmations
        )
        == 2
        for cohort_key in ("service:strong-l3", "service:weak-l3")
    )
    if include_unseen:
        discovery = [item for item in reservations
                     if item.ticket.selection_reason == "cold_leaf_coverage"]
        assert len(discovery) == 4
        assert all(item.ticket.service_cohort_key.startswith("service:novel-")
                   for item in discovery)
    for item in reservations:
        scheduler.cancel_handoff(item, requeue=False)
    assert scheduler.release_outstanding() == 0


def test_confirmation_first_attempt_precedes_an_overdue_retry() -> None:
    scheduler = Tier3CandidateScheduler()

    def enqueue(name: str, group: str) -> None:
        scheduler.enqueue(
            candidate(name), None, arm_key=name,
            source_root=f"seed-{group}" if name.endswith("-seed") else "opaque-root",
            family="family", coarse_key=name,
            feature_key=feature(
                family="family", service_profiles=("l0", "l1", group, f"{group}-leaf"),
            ),
        )

    def seed(group: str) -> None:
        enqueue(f"{group}-seed", group)
        batch = scheduler.reserve_handoffs(16)
        item = next(item for item in batch if item.candidate.node.name == f"{group}-seed")
        for other in batch:
            if other is not item:
                scheduler.cancel_handoff(other, requeue=True)
        scheduler.commit_handoff(item)
        scheduler.on_tcp_outcome(item.ticket, responsive=True)
        scheduler.on_terminal(item.ticket, passed=True, outcome="pass",
                              stage="fast_probe", busy_worker_ms=200)
        scheduler.on_commit_complete(item.ticket, net_new=False)
        for index in range(12):
            enqueue(f"{group}-{index}", group)

    seed("early")
    with scheduler._lock:
        first, _ = scheduler._pop_leaf_confirmation_candidate_unlocked(ignore_credits=False)
    assert first is not None
    scheduler.commit_handoff(first.dispatch_ticket)
    scheduler.on_tcp_outcome(first.dispatch_ticket, responsive=True)
    for index in range(32):
        enqueue(f"ordinary-{index}", f"ordinary-{index}")
    batch = scheduler.reserve_handoffs(16)
    for item in batch:
        if item.ticket.reward_cohort_state == "leaf_confirmation":
            scheduler.cancel_handoff(item, requeue=True)
            continue
        scheduler.commit_handoff(item)
        scheduler.on_tcp_outcome(item.ticket, responsive=False)
    seed("later")
    with scheduler._lock:
        chosen, _ = scheduler._pop_leaf_confirmation_candidate_unlocked(ignore_credits=False)
    assert chosen is not None
    assert chosen.dispatch_ticket.reward_cohort_key == "service:later-leaf"
    scheduler.release_outstanding()
    assert scheduler.quick_metrics()["active_ticket_count"] == 0


def test_confirmation_exhaustion_cannot_reuse_the_same_single_pass() -> None:
    scheduler = Tier3CandidateScheduler()
    profiles = ("retry-l0", "retry-l1", "retry-l2", "retry-l3")

    def enqueue(name: str) -> None:
        scheduler.enqueue(
            candidate(name), None, arm_key=name, source_root="opaque-root",
            family="opaque-family", coarse_key=name,
            feature_key=feature(family="opaque-family", service_profiles=profiles),
        )

    enqueue("seed")
    seed = scheduler.reserve_handoffs(1)[0]
    scheduler.commit_handoff(seed)
    scheduler.on_tcp_outcome(seed.ticket, responsive=True)
    scheduler.on_terminal(seed.ticket, passed=True, outcome="pass",
                          busy_worker_ms=200, stage="fast_probe")
    scheduler.on_commit_complete(seed.ticket, net_new=False)
    for index in range(20):
        enqueue(f"pending-{index}")
    wave = scheduler.reserve_handoffs(16)
    confirmations = [item for item in wave
                     if item.ticket.reward_cohort_state == "leaf_confirmation"]
    assert len(confirmations) == 2
    for item in wave:
        if item not in confirmations:
            scheduler.cancel_handoff(item, requeue=True)
    for item in confirmations:
        scheduler.commit_handoff(item)
        scheduler.on_tcp_outcome(item.ticket, responsive=True)
        scheduler.on_terminal(item.ticket, passed=False, outcome="validation_timeout",
                              busy_worker_ms=3000, stage="fast_probe")

    scheduler.enqueue(
        candidate("other-seed"), None, arm_key="other", source_root="other-root",
        family="other-family", coarse_key="other",
        feature_key=feature(service_profiles=("other-0", "other-1", "other-2", "other-3")),
    )
    unrelated_wave = scheduler.reserve_handoffs(16)
    other = next(item for item in unrelated_wave if item.candidate.node.name == "other-seed")
    for item in unrelated_wave:
        if item is not other:
            scheduler.cancel_handoff(item, requeue=True)
    scheduler.commit_handoff(other)
    scheduler.on_tcp_outcome(other.ticket, responsive=True)
    scheduler.on_terminal(other.ticket, passed=True, outcome="pass",
                          busy_worker_ms=200, stage="fast_probe")
    scheduler.on_commit_complete(other.ticket, net_new=False)

    next_wave = scheduler.reserve_handoffs(16)
    try:
        assert not any(item.ticket.reward_cohort_state == "leaf_confirmation"
                       for item in next_wave)
        # Ordinary exploration is still allowed to supply genuinely new evidence.
        ordinary = next_wave[0]
        scheduler.commit_handoff(ordinary)
        scheduler.on_tcp_outcome(ordinary.ticket, responsive=True)
        scheduler.on_terminal(ordinary.ticket, passed=True, outcome="pass",
                              busy_worker_ms=200, stage="fast_probe")
        scheduler.on_commit_complete(ordinary.ticket, net_new=False)
        assert scheduler._cohort_yield_states["service:retry-l3"].eligibility is (
            Tier3ServiceCohortEligibility.LEAF_PROBATION
        )
    finally:
        for item in next_wave:
            scheduler.cancel_handoff(item, requeue=False)
        assert scheduler.release_outstanding() == 0


def test_failed_probation_cannot_reuse_unchanged_training_on_global_feedback() -> None:
    scheduler = Tier3CandidateScheduler()
    profiles = ("failure-l0", "failure-l1", "failure-l2", "failure-l3")

    def enqueue(name: str, keys: tuple[str, ...] = profiles) -> None:
        scheduler.enqueue(
            candidate(name), None, arm_key=name, source_root="opaque-root",
            family="opaque-family", coarse_key=name,
            feature_key=feature(family="opaque-family", service_profiles=keys),
        )

    def pass_result(item) -> None:
        scheduler.commit_handoff(item)
        scheduler.on_tcp_outcome(item.ticket, responsive=True)
        scheduler.on_terminal(item.ticket, passed=True, outcome="pass",
                              busy_worker_ms=200, stage="fast_probe")
        scheduler.on_commit_complete(item.ticket, net_new=False)

    for index in range(2):
        enqueue(f"seed-{index}")
        pass_result(scheduler.reserve_handoffs(1)[0])
    for index in range(20):
        enqueue(f"inventory-{index}")
        enqueue(f"ordinary-{index}", ("ordinary-l0", "ordinary-l1",
                                       f"ordinary-l2-{index}", f"ordinary-l3-{index}"))
    wave = scheduler.reserve_handoffs(16)
    probation = [item for item in wave
                 if item.ticket.reward_cohort_state == "leaf_probation"]
    assert len(probation) == 4
    for item in wave:
        if item not in probation:
            scheduler.cancel_handoff(item, requeue=True)
    for item in probation:
        scheduler.commit_handoff(item)
        scheduler.on_tcp_outcome(item.ticket, responsive=False)
    state = scheduler._cohort_yield_states["service:failure-l3"]
    assert state.eligibility is Tier3ServiceCohortEligibility.DEMOTED
    assert (state.completed_total, state.pass_total) == (2, 2)

    unrelated = scheduler.reserve_handoffs(16)
    ordinary = next(item for item in unrelated
                    if item.candidate.node.name.startswith("ordinary-"))
    censored = next(item for item in unrelated
                    if item.candidate.node.name.startswith("inventory-"))
    for item in unrelated:
        if item is not ordinary and item is not censored:
            scheduler.cancel_handoff(item, requeue=True)
    scheduler.commit_handoff(censored)
    scheduler.on_tcp_outcome(censored.ticket, responsive=True)
    scheduler.on_terminal(censored.ticket, passed=False, outcome="validation_timeout",
                          busy_worker_ms=3000, stage="fast_probe")
    pass_result(ordinary)
    assert state.eligibility is Tier3ServiceCohortEligibility.DEMOTED
    assert state.production_consecutive_nonpass == 4
    assert scheduler.release_outstanding() == 0


def test_four_probation_passes_can_wait_for_commit_and_all_resolve() -> None:
    scheduler = Tier3CandidateScheduler(
        unknown_exploration_ratio=0.15,
        exploit_scan_share=0.75,
    )
    profiles = ("burst-l0", "burst-l1", "burst-l2", "burst-l3")
    scheduler.enqueue(
        candidate("burst-seed"),
        None,
        arm_key="burst-seed",
        source_root="seed-root",
        family="opaque-family",
        coarse_key="burst-seed",
        feature_key=feature(
            family="opaque-family",
            service_profiles=profiles,
        ),
    )
    seed = scheduler.reserve_handoffs(1)[0]
    scheduler.commit_handoff(seed)
    scheduler.on_tcp_outcome(seed.ticket, responsive=True)
    scheduler.on_terminal(
        seed.ticket,
        passed=True,
        outcome="pass",
        busy_worker_ms=200,
        stage="fast_probe",
    )
    scheduler.on_commit_complete(seed.ticket, net_new=False)

    for index in range(8):
        scheduler.enqueue(
            candidate(f"burst-candidate-{index}"),
            None,
            arm_key=f"burst-candidate-{index}",
            source_root="inventory-root",
            family="opaque-family",
            coarse_key=f"burst-candidate-{index}",
            feature_key=feature(
                family="opaque-family",
                service_profiles=profiles,
            ),
        )

    confirmation_wave = scheduler.reserve_handoffs(16)
    confirmations = [
        item
        for item in confirmation_wave
        if item.ticket.reward_cohort_state == "leaf_confirmation"
    ]
    assert len(confirmations) == 2
    confirmation = confirmations[0]
    for item in confirmation_wave:
        if item is not confirmation:
            scheduler.cancel_handoff(item, requeue=True)
    scheduler.commit_handoff(confirmation)
    scheduler.on_tcp_outcome(confirmation.ticket, responsive=True)
    scheduler.on_terminal(
        confirmation.ticket,
        passed=True,
        outcome="pass",
        busy_worker_ms=200,
        stage="fast_probe",
    )
    scheduler.on_commit_complete(confirmation.ticket, net_new=False)

    reservations = scheduler.reserve_handoffs(16)
    probation = [
        item
        for item in reservations
        if item.ticket.reward_cohort_state == "leaf_probation"
    ]
    assert len(probation) == 4
    for item in reservations:
        if item not in probation:
            scheduler.cancel_handoff(item, requeue=False)

    for item in probation:
        scheduler.commit_handoff(item)
        scheduler.on_tcp_outcome(item.ticket, responsive=True)
        scheduler.on_terminal(
            item.ticket,
            passed=True,
            outcome="pass",
            busy_worker_ms=200,
            stage="fast_probe",
        )

    pending = scheduler.quick_metrics()
    assert pending["reward_service_outstanding_count"] == 4
    assert pending["active_ticket_count"] == 4

    for item in reversed(probation):
        scheduler.on_commit_complete(item.ticket, net_new=True)

    resolved = scheduler.quick_metrics()
    assert resolved["reward_service_outstanding_count"] == 0
    assert resolved["reward_service_resolved_count"] >= 4
    assert resolved["reward_service_net_new_count"] >= 4
    assert resolved["active_ticket_count"] == 0
    assert scheduler.release_outstanding() == 0


def test_default_l2_discovery_samples_one_unverified_direct_child() -> None:
    scheduler = Tier3CandidateScheduler(unknown_exploration_ratio=0.15)
    successful = ("discover-l0", "discover-l1", "discover-l2", "discover-seed")
    scheduler.enqueue(
        candidate("discover-seed"),
        None,
        arm_key="discover-seed",
        source_root="discover-root",
        family="opaque-family",
        coarse_key="discover-seed",
        feature_key=feature(
            family="opaque-family",
            service_profiles=successful,
        ),
    )
    seed = scheduler.reserve_handoffs(1)[0]
    scheduler.commit_handoff(seed)
    scheduler.on_tcp_outcome(seed.ticket, responsive=True)
    scheduler.on_terminal(
        seed.ticket,
        passed=True,
        outcome="pass",
        busy_worker_ms=200,
        stage="fast_probe",
    )
    scheduler.on_commit_complete(seed.ticket, net_new=True)

    for index in range(3):
        scheduler.enqueue(
            candidate(f"discover-child-{index}"),
            None,
            arm_key=f"discover-child-{index}",
            source_root="discover-root",
            family="opaque-family",
            coarse_key=f"discover-child-{index}",
            feature_key=feature(
                family="opaque-family",
                service_profiles=(
                    "discover-l0",
                    "discover-l1",
                    "discover-l2",
                    f"discover-child-l3-{index}",
                ),
            ),
        )
    for index in range(24):
        scheduler.enqueue(
            candidate(f"discover-ordinary-{index}"),
            None,
            arm_key=f"discover-ordinary-{index}",
            source_root=f"ordinary-root-{index % 6}",
            family="opaque-family",
            coarse_key=f"discover-ordinary-{index}",
            feature_key=feature(
                family="opaque-family",
                service_profiles=(
                    "ordinary-l0",
                    f"ordinary-l1-{index}",
                    f"ordinary-l2-{index}",
                    f"ordinary-l3-{index}",
                ),
            ),
        )

    discovered_children: set[str] = set()
    for _round in range(3):
        reservations = scheduler.reserve_handoffs(16)
        discovery = [
            item
            for item in reservations
            if item.ticket.reward_cohort_state == "parent_discovery"
        ]

        assert len(discovery) == 1
        selected = discovery[0]
        assert selected.ticket.reward_cohort_key == "service:discover-l2"
        assert selected.ticket.reward_scope == "none"
        assert selected.ticket.unknown_exploration is True
        assert "discover-child-" in selected.candidate.node.name
        assert selected.candidate.node.name not in discovered_children
        discovered_children.add(selected.candidate.node.name)
        assert all(
            item.ticket.reward_cohort_key
            not in {"service:discover-l0", "service:discover-l1"}
            for item in reservations
        )
        for item in reservations:
            if item is selected:
                scheduler.commit_handoff(item)
                scheduler.on_tcp_outcome(item.ticket, responsive=True)
                scheduler.on_terminal(
                    item.ticket,
                    passed=False,
                    outcome="candidate_fail",
                    busy_worker_ms=200,
                    stage="fast_probe",
                )
            else:
                scheduler.cancel_handoff(item, requeue=True)

    assert len(discovered_children) == 3
    assert scheduler.release_outstanding() == 0


def test_confirmation_flights_remain_bounded_across_handoff_epochs() -> None:
    scheduler = Tier3CandidateScheduler(unknown_exploration_ratio=0.15)
    profiles = [
        ("flight-l0", f"flight-l1-{i}", f"flight-l2-{i}", f"flight-l3-{i}")
        for i in range(3)
    ]
    for index, profile in enumerate(profiles):
        scheduler.enqueue(
            candidate(f"flight-seed-{index}"), None,
            arm_key=f"flight-seed-{index}", source_root=f"root-{index}",
            family="opaque", coarse_key=f"seed-{index}",
            feature_key=feature(service_profiles=profile),
        )
        seed = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(seed)
        scheduler.on_tcp_outcome(seed.ticket, responsive=True)
        scheduler.on_terminal(seed.ticket, passed=True, outcome="pass",
                              busy_worker_ms=200, stage="fast_probe")
        scheduler.on_commit_complete(seed.ticket, net_new=False)

    for index in range(120):
        scheduler.enqueue(
            candidate(f"flight-backlog-{index}"), None,
            arm_key=f"flight-arm-{index}", source_root=f"root-{index % 3}",
            family="opaque", coarse_key=f"coarse-{index}",
            feature_key=feature(service_profiles=profiles[index % 3]),
        )
    held = []
    for revision in range(4):
        for reservation in scheduler.reserve_handoffs(16, control_revision=revision + 1):
            scheduler.commit_handoff(reservation)
            scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
            if reservation.ticket.reward_cohort_state == "leaf_confirmation":
                held.append(reservation)
            else:
                scheduler.on_terminal(reservation.ticket, outcome="cancelled")
        assert len(held) == 4
        assert all(
            sum(item.ticket.reward_cohort_key == f"service:{profile[-1]}"
                for item in held) <= 2
            for profile in profiles
        )
    for reservation in held:
        scheduler.on_terminal(reservation.ticket, outcome="cancelled")
    assert scheduler.quick_metrics()["leaf_confirmation_outstanding_count"] == 0
    assert scheduler.release_outstanding() == 0


@pytest.mark.parametrize("outcome,explicit_censor", [
    ("right_censored", True), ("transient", False),
])
def test_retained_confirmation_deep_does_not_hold_fast_sampling_permits(
    outcome: str, explicit_censor: bool,
) -> None:
    scheduler = Tier3CandidateScheduler(unknown_exploration_ratio=0.15)
    profiles = [
        ("permit-l0", f"permit-l1-{i}", f"permit-l2-{i}", f"permit-l3-{i}")
        for i in range(6)
    ]
    for index, profile in enumerate(profiles):
        scheduler.enqueue(
            candidate(f"permit-seed-{index}"), None,
            arm_key=f"permit-seed-{index}", source_root=f"root-{index}",
            family="opaque", coarse_key=f"seed-{index}",
            feature_key=feature(service_profiles=profile),
        )
        seed = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(seed)
        scheduler.on_tcp_outcome(seed.ticket, responsive=True)
        scheduler.on_terminal(seed.ticket, passed=True, outcome="pass",
                              busy_worker_ms=200, stage="fast_probe")
        scheduler.on_commit_complete(seed.ticket, net_new=False)
    for index in range(120):
        scheduler.enqueue(
            candidate(f"permit-backlog-{index}"), None,
            arm_key=f"permit-arm-{index}", source_root=f"root-{index % 6}",
            family="opaque", coarse_key=f"coarse-{index}",
            feature_key=feature(service_profiles=profiles[index % 6]),
        )

    held = []
    for item in scheduler.reserve_handoffs(16):
        scheduler.commit_handoff(item)
        scheduler.on_tcp_outcome(item.ticket, responsive=True)
        if item.ticket.reward_cohort_state == "leaf_confirmation":
            held.append(item)
        else:
            scheduler.on_terminal(item.ticket, outcome="cancelled")
    assert len(held) == 4
    unserved = {f"service:{profile[-1]}" for profile in profiles} - {
        item.ticket.reward_cohort_key for item in held
    }
    assert unserved
    for item in held:
        scheduler.on_fast_observation(item.ticket, outcome=outcome,
                                      right_censored=explicit_censor, busy_worker_ms=3000)
    with scheduler._lock:
        assert not any(scheduler._leaf_confirmation_quota_open_unlocked(
            key, scheduler._cohort_yield_states[key]) for key in unserved)

    for item in held:
        scheduler.on_fast_yield(item.ticket, outcome=outcome,
                                right_censored=explicit_censor, busy_worker_ms=3000)
    with scheduler._lock:
        assert all(scheduler._leaf_confirmation_quota_open_unlocked(
            key, scheduler._cohort_yield_states[key]) for key in unserved)
    metrics = scheduler.quick_metrics()
    assert metrics["leaf_confirmation_outstanding_count"] == 4
    assert metrics["leaf_confirmation_fast_pending_count"] == 0
    assert metrics["leaf_confirmation_deep_pending_count"] == 4
    assert metrics["parked_deep_debt"] == 4

    fresh = []
    for item in scheduler.reserve_handoffs(16):
        scheduler.commit_handoff(item)
        scheduler.on_tcp_outcome(item.ticket, responsive=True)
        if item.ticket.reward_cohort_state == "leaf_confirmation":
            fresh.append(item)
        else:
            scheduler.on_terminal(item.ticket, outcome="cancelled")
    assert 0 < len(fresh) <= 4
    assert any(item.ticket.reward_cohort_key in unserved for item in fresh)
    metrics = scheduler.quick_metrics()
    assert metrics["leaf_confirmation_fast_pending_count"] == len(fresh)
    assert metrics["leaf_confirmation_outstanding_count"] == 4 + len(fresh)
    for item in held + fresh:
        scheduler.on_terminal(item.ticket, outcome="cancelled")
        scheduler.on_terminal(item.ticket, outcome="cancelled")
    assert scheduler.release_outstanding() == 0
    metrics = scheduler.quick_metrics()
    assert metrics["leaf_confirmation_fast_pending_count"] == 0
    assert metrics["leaf_confirmation_deep_pending_count"] == 0
    assert metrics["leaf_confirmation_outstanding_count"] == 0
    assert metrics["parked_deep_debt"] == 0


def test_parent_discovery_waits_while_validated_leaf_has_live_inventory() -> None:
    scheduler = Tier3CandidateScheduler(unknown_exploration_ratio=0.15)
    profiles = ("live-l0", "live-l1", "live-l2", "live-good-leaf")
    for index in range(2):
        scheduler.enqueue(
            candidate(f"live-seed-{index}"), None,
            arm_key=f"live-seed-{index}", source_root=f"root-{index}",
            family="opaque", coarse_key=f"seed-{index}",
            feature_key=feature(service_profiles=profiles),
        )
        seed = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(seed)
        scheduler.on_tcp_outcome(seed.ticket, responsive=True)
        scheduler.on_terminal(seed.ticket, passed=True, outcome="pass",
                              busy_worker_ms=200, stage="fast_probe")
        scheduler.on_commit_complete(seed.ticket, net_new=False)
    for index in range(32):
        for label, leaf in (("good", profiles[-1]), ("sibling", "live-unseen-leaf")):
            scheduler.enqueue(
                candidate(f"{label}-{index}"), None,
                arm_key=f"{label}-{index}", source_root=f"root-{index % 3}",
                family="opaque", coarse_key=f"{label}-{index}",
                feature_key=feature(service_profiles=(*profiles[:3], leaf)),
            )
    wave = scheduler.reserve_handoffs(16)
    assert any(item.ticket.reward_scope == "service" for item in wave)
    assert not any(item.ticket.reward_cohort_state == "parent_discovery" for item in wave)
    for item in wave:
        scheduler.cancel_handoff(item, requeue=True)
    assert scheduler.release_outstanding() == 0


def legacy_candidate_scheduler(**kwargs: object) -> Tier3CandidateScheduler:
    """Exercise the retained shadow model without enabling it in production."""

    kwargs["_enable_legacy_structural_routing_for_test"] = True
    return Tier3CandidateScheduler(**kwargs)


def candidate(name: str, protocol: str = "vless") -> Node:
    return Node(
        name=name,
        protocol=protocol,
        address=f"{name}.example.com",
        port=443,
        tier="tier3",
    )


def feature(
    family: str = "vless",
    *,
    transport: str = "ws",
    security: str = "tls",
    port: str = "443",
    address: str = "host:example.com",
    host: str = "example.com",
    service_profile: str = "none",
    service_profiles: tuple[str, ...] = (),
) -> Tier3CandidateFeatureKey:
    return Tier3CandidateFeatureKey.build(
        service_family=family,
        transport=transport,
        security=security,
        port_bucket=port,
        address_bucket=address,
        host_suffix=host,
        service_profile=service_profile,
        service_profiles=service_profiles,
    )


def promote_cohort(
    scheduler: Tier3CandidateScheduler,
    *,
    scope: str,
    cohort_key: str,
) -> None:
    with scheduler._lock:
        for _index in range(16):
            scheduler._record_fast_yield_sample_unlocked(
                passed=False,
                censored=False,
                busy_worker_ms=1000,
                scope="ordinary",
                cohort_key="ordinary",
            )
            scheduler._record_fast_yield_sample_unlocked(
                passed=True,
                censored=False,
                busy_worker_ms=1000,
                scope=scope,
                cohort_key=cohort_key,
                leaf_eligible=scope == "service",
            )


def test_cold_leaf_coverage_samples_distinct_unknown_structures() -> None:
    scheduler = Tier3CandidateScheduler(unknown_exploration_ratio=0.15)
    scheduler.set_credit_envelope(16, handoff_window=16, tcp_execution_limit=16)
    for index in range(24):
        leaf = "leaf-head" if index < 16 else f"leaf-{index}"
        scheduler.enqueue(
            candidate(f"cold-{index}"),
            None,
            arm_key=f"cold-{index}",
            source_root=f"root-{index % 3}",
            family="opaque-family",
            coarse_key=f"coarse-{index}",
            feature_key=feature(
                family="opaque-family",
                service_profiles=("l0", "l1", "l2", leaf),
            ),
        )

    first = scheduler.reserve_handoffs(16)
    cold = [
        item
        for item in first
        if item.ticket.selection_reason == "cold_leaf_coverage"
    ]
    assert len(cold) == 8
    assert len({item.ticket.service_cohort_key for item in cold}) == 8
    assert all(item.ticket.unknown_exploration for item in cold)

    for item in first:
        scheduler.cancel_handoff(item, requeue=True)
    repeated = scheduler.reserve_handoffs(16)
    repeated_cold = [
        item
        for item in repeated
        if item.ticket.selection_reason == "cold_leaf_coverage"
    ]
    assert len(repeated_cold) == 8
    assert {
        item.ticket.service_cohort_key for item in repeated_cold
    } == {item.ticket.service_cohort_key for item in cold}
    for item in repeated:
        scheduler.cancel_handoff(item, requeue=False)
    assert scheduler.release_outstanding() == 0


def test_cold_leaf_coverage_never_repeats_an_unresolved_started_leaf() -> None:
    scheduler = Tier3CandidateScheduler(unknown_exploration_ratio=0.15)
    scheduler.set_credit_envelope(16, handoff_window=16, tcp_execution_limit=16)
    leaves = ("leaf-a", "leaf-b")
    for leaf_index, leaf in enumerate(leaves):
        for index in range(3):
            scheduler.enqueue(
                candidate(f"{leaf}-{index}"),
                None,
                arm_key=f"{leaf}-arm-{index}",
                source_root=f"root-{index}",
                family="opaque-family",
                coarse_key=f"{leaf}-coarse-{index}",
                feature_key=feature(
                    family="opaque-family",
                    service_profiles=(
                        "l0",
                        "l1",
                        f"l2-{leaf_index}",
                        leaf,
                    ),
                ),
            )

    def take_one() -> Tier3CandidateFeedbackTicket | None:
        with scheduler._lock:
            item, _examined = scheduler._pop_cold_leaf_coverage_candidate_unlocked(
                ignore_credits=False,
                provisionally_selected_arms=set(),
            )
        if item is None or item.dispatch_ticket is None:
            return None
        scheduler.commit_handoff(item.dispatch_ticket)
        scheduler.on_tcp_outcome(item.dispatch_ticket, responsive=True)
        return item.dispatch_ticket

    first = [take_one(), take_one()]
    assert all(ticket is not None for ticket in first)
    first_keys = {ticket.service_cohort_key for ticket in first if ticket is not None}
    assert first_keys == {"service:leaf-a", "service:leaf-b"}

    second = [take_one(), take_one()]
    assert second == [None, None]
    with scheduler._lock:
        assert scheduler._cold_leaf_started_keys == first_keys
    assert take_one() is None

    scheduler.release_outstanding()
    assert scheduler.quick_metrics()["active_ticket_count"] == 0


@pytest.mark.parametrize("phase", ["reserved", "started", "censored", "cancelled"])
def test_cold_discovery_does_not_repeat_an_ordinary_lane_sample(phase: str) -> None:
    scheduler = Tier3CandidateScheduler(unknown_exploration_ratio=0.15)
    scheduler.set_credit_envelope(16, handoff_window=16, tcp_execution_limit=16)
    for leaf, count in (("early", 20), ("unseen", 4)):
        for index in range(count):
            scheduler.enqueue(
                candidate(f"{leaf}-{index}"), None,
                arm_key=f"{leaf}-{index}", source_root="opaque-root",
                family="opaque-family", coarse_key=f"{leaf}-{index}",
                feature_key=feature(
                    family="opaque-family",
                    service_profiles=("l0", "l1", f"l2-{leaf}", leaf),
                ),
            )
    first = scheduler.reserve_handoffs(1)[0]
    assert first.ticket.selection_reason == "coverage"
    assert first.ticket.service_cohort_key == "service:early"
    if phase == "cancelled":
        scheduler.cancel_handoff(first, requeue=True)
    elif phase != "reserved":
        scheduler.commit_handoff(first)
        scheduler.on_tcp_outcome(first.ticket, responsive=True)
    if phase == "censored":
        scheduler.on_fast_observation(first.ticket, right_censored=True, busy_worker_ms=3000)
        scheduler.on_terminal(first.ticket, outcome="censored", stage="fast_probe")
    with scheduler._lock:
        discovered, _ = scheduler._pop_cold_leaf_coverage_candidate_unlocked(
            ignore_credits=False, provisionally_selected_arms=set(),
        )
    assert discovered is not None
    expected = "service:early" if phase == "cancelled" else "service:unseen"
    assert discovered.dispatch_ticket.service_cohort_key == expected
    scheduler.release_outstanding()
    assert scheduler.quick_metrics()["active_ticket_count"] == 0


@pytest.mark.parametrize("root_count", [1, 2, 5, 17])
def test_cold_discovery_covers_unseen_parent_before_repeating_large_parent(
    root_count: int,
) -> None:
    scheduler = Tier3CandidateScheduler()
    scheduler.set_credit_envelope(16, handoff_window=16, tcp_execution_limit=16)
    for leaf in range(12):
        for index in range(max(8, root_count)):
            name = f"head-{leaf}-{index}"
            scheduler.enqueue(
                candidate(name), None, arm_key=name,
                source_root=f"opaque-{index % root_count}", family="family",
                coarse_key=name,
                feature_key=feature(
                    family="family",
                    service_profiles=("l0", "l1", "large-parent", f"leaf-{leaf}"),
                ),
            )
    first = scheduler.reserve_handoffs(1)[0]
    scheduler.commit_handoff(first)
    scheduler.on_tcp_outcome(first.ticket, responsive=True)
    scheduler.enqueue(
        candidate("later-parent"), None, arm_key="later", source_root="opaque-0",
        family="family", coarse_key="later",
        feature_key=feature(
            family="family", service_profiles=("l0", "l1", "unseen-parent", "later-leaf"),
        ),
    )
    for _ in range(2):
        with scheduler._lock:
            discovered, _ = scheduler._pop_cold_leaf_coverage_candidate_unlocked(
                ignore_credits=False, provisionally_selected_arms=set(),
            )
        assert discovered is not None
        assert discovered.node.name == "later-parent"
        scheduler.cancel_handoff(discovered.dispatch_ticket, requeue=True)
    # Peeking and cancelled reservations do not consume a real discovery.
    assert scheduler.quick_metrics()["selected_total"] == 1
    assert scheduler.quick_metrics()["cold_leaf_sampled_distinct_started"] == 1
    assert scheduler.quick_metrics()["cold_l2_sampled_distinct_started"] == 1
    scheduler.release_outstanding()
    assert scheduler.quick_metrics()["active_ticket_count"] == 0


def test_cold_leaf_coverage_prefers_independent_strata_before_arrival_order() -> None:
    scheduler = Tier3CandidateScheduler(unknown_exploration_ratio=0.15)
    scheduler.set_credit_envelope(16, handoff_window=16, tcp_execution_limit=16)
    early_leaf = ("l0", "l1", "l2", "leaf-early")
    consensus_leaf = ("l0", "l1", "l2", "leaf-consensus")

    for index in range(8):
        scheduler.enqueue(
            candidate(f"early-{index}"),
            None,
            arm_key=f"early-{index}",
            source_root="root-early",
            family="opaque-family",
            coarse_key=f"early-{index}",
            feature_key=feature(
                family="opaque-family",
                service_profiles=early_leaf,
            ),
        )
    for index in range(6):
        scheduler.enqueue(
            candidate(f"consensus-{index}"),
            None,
            arm_key=f"consensus-{index}",
            source_root=f"root-{index % 3}",
            family="opaque-family",
            coarse_key=f"consensus-{index}",
            feature_key=feature(
                family="opaque-family",
                service_profiles=consensus_leaf,
            ),
        )

    for _ in range(2):
        with scheduler._lock:
            item, _ = scheduler._pop_cold_leaf_coverage_candidate_unlocked(
                ignore_credits=False, provisionally_selected_arms=set(),
            )
        assert item is not None
        assert item.dispatch_ticket.service_cohort_key == "service:leaf-consensus"
        scheduler.cancel_handoff(item.dispatch_ticket, requeue=True)

    reservations = scheduler.reserve_handoffs(16)
    # Root coverage already samples both leaves; discovery must not sample
    # them again merely because they entered through a different lane.
    assert not any(
        item.ticket.selection_reason == "cold_leaf_coverage"
        for item in reservations
    )
    for item in reservations:
        scheduler.cancel_handoff(item, requeue=False)
    assert scheduler.release_outstanding() == 0


def test_cold_l2_training_preserves_inventory_until_fast_feedback() -> None:
    scheduler = Tier3CandidateScheduler(unknown_exploration_ratio=0.15)
    scheduler.set_credit_envelope(16, handoff_window=16, tcp_execution_limit=16)

    for l2_index in range(4):
        for candidate_index in range(20):
            scheduler.enqueue(
                candidate(f"l2-{l2_index}-{candidate_index}"),
                None,
                arm_key=f"arm-{l2_index}-{candidate_index}",
                source_root="opaque-root",
                family="opaque-family",
                coarse_key=f"coarse-{l2_index}-{candidate_index}",
                feature_key=feature(
                    family="opaque-family",
                    service_profiles=(
                        "shared-l0",
                        "shared-l1",
                        f"l2-{l2_index}",
                        f"leaf-{l2_index}",
                    ),
                ),
            )

    first_wave = scheduler.reserve_handoffs(16)
    assert len(first_wave) == 16
    for reservation in first_wave:
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=True)

    unresolved_by_l2: dict[str, int] = {}
    for reservation in first_wave:
        l2_key = reservation.ticket.service_cohort_keys[2]
        unresolved_by_l2[l2_key] = unresolved_by_l2.get(l2_key, 0) + 1
    assert sorted(unresolved_by_l2.values()) == [4, 4, 4, 4]
    assert scheduler.reserve_handoffs(16) == []

    released = first_wave[0]
    released_l2 = released.ticket.service_cohort_keys[2]
    scheduler.on_terminal(
        released.ticket,
        passed=False,
        outcome="candidate_fail",
        busy_worker_ms=250,
        stage="fast_probe",
    )
    replacement = scheduler.reserve_handoffs(1)
    assert len(replacement) == 1
    assert replacement[0].ticket.service_cohort_keys[2] == released_l2

    metrics = scheduler.quick_metrics()
    assert metrics["cold_l2_training_outstanding_high_water"] == 16
    assert metrics["cold_l2_training_deferred_total"] > 0
    scheduler.cancel_handoff(replacement[0], requeue=False)
    assert scheduler.release_outstanding() == 15
    assert scheduler.pending_handoff_count() == 0


def test_single_cold_l2_remains_work_conserving_and_cancel_requeues() -> None:
    scheduler = Tier3CandidateScheduler(unknown_exploration_ratio=0.15)
    scheduler.set_credit_envelope(16, handoff_window=16, tcp_execution_limit=16)
    profiles = ("only-l0", "only-l1", "only-l2", "only-leaf")
    for index in range(32):
        scheduler.enqueue(
            candidate(f"single-{index}"),
            None,
            arm_key=f"single-arm-{index}",
            source_root="opaque-root",
            family="opaque-family",
            coarse_key=f"single-coarse-{index}",
            feature_key=feature(
                family="opaque-family",
                service_profiles=profiles,
            ),
        )

    first_wave = scheduler.reserve_handoffs(16)
    assert len(first_wave) == 16
    first_sequences = {
        item.candidate.node.name: item.candidate.lookahead_sequence
        for item in first_wave
    }
    assert all(item.ticket.service_cohort_keys[2] == "service:only-l2" for item in first_wave)
    assert scheduler.quick_metrics()["cold_l2_training_borrowed_total"] >= 12

    for reservation in first_wave:
        assert scheduler.cancel_handoff(reservation, requeue=True) is True
        assert (
            reservation.candidate.lookahead_sequence
            == first_sequences[reservation.candidate.node.name]
        )
    repeated = scheduler.reserve_handoffs(16)
    assert len(repeated) == 16
    for reservation in repeated:
        scheduler.cancel_handoff(reservation, requeue=False)
    assert scheduler.release_outstanding() == 0


# --- opt-in TCP breadth and the cold-L2 training limit ------------------------


def _cold_l2_inventory(
    scheduler: Tier3CandidateScheduler,
    sizes: tuple[int, ...],
) -> None:
    """Queue ``sizes[i]`` cold candidates under L2 cohort ``l2-i``, in order."""

    for l2_index, size in enumerate(sizes):
        for candidate_index in range(size):
            scheduler.enqueue(
                candidate(f"l2-{l2_index}-{candidate_index}"),
                None,
                arm_key=f"arm-{l2_index}-{candidate_index}",
                source_root="opaque-root",
                family="opaque-family",
                coarse_key=f"coarse-{l2_index}-{candidate_index}",
                feature_key=feature(
                    family="opaque-family",
                    service_profiles=(
                        "shared-l0",
                        "shared-l1",
                        f"l2-{l2_index}",
                        f"leaf-{l2_index}",
                    ),
                ),
            )


def _responsive_wave(
    scheduler: Tier3CandidateScheduler,
    limit: int,
) -> tuple[list, dict[str, int]]:
    """Reserve, start and TCP-pass one wave; it then waits for FAST feedback."""

    wave = scheduler.reserve_handoffs(limit)
    per_l2: dict[str, int] = {}
    for reservation in wave:
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
        l2_key = reservation.ticket.service_cohort_keys[2]
        per_l2[l2_key] = per_l2.get(l2_key, 0) + 1
    return wave, per_l2


def test_cold_l2_training_breadth_two_keeps_each_share_until_fast_feedback() -> None:
    # The pinned four-L2 case above with opt-in TCP breadth 2.0: the owner
    # doubles the TCP pipeline (32 leases for 16 identities).
    baseline = Tier3CandidateScheduler(unknown_exploration_ratio=0.15)
    baseline.set_credit_envelope(16, handoff_window=32, tcp_execution_limit=32)
    _cold_l2_inventory(baseline, (20, 20, 20, 20))
    baseline_wave, baseline_per_l2 = _responsive_wave(baseline, 32)
    # Unscaled, the identity-sized limit holds the doubled pipeline at 16.
    assert len(baseline_wave) == 16
    assert sorted(baseline_per_l2.values()) == [4, 4, 4, 4]
    assert baseline.reserve_handoffs(32) == []
    baseline.release_outstanding()

    scheduler = Tier3CandidateScheduler(unknown_exploration_ratio=0.15)
    scheduler.set_credit_envelope(16, handoff_window=32, tcp_execution_limit=32)
    scheduler.set_cold_l2_training_breadth(2.0)
    _cold_l2_inventory(scheduler, (20, 20, 20, 20))
    first_wave, per_l2 = _responsive_wave(scheduler, 32)
    # Each L2 keeps its quarter of the widened pipeline: 8 of 32, not 4.
    assert len(first_wave) == 32
    assert sorted(per_l2.values()) == [8, 8, 8, 8]
    # Diversity still binds: a capped L2 gets no new sample before FAST
    # feedback, although 48 candidates are queued and TCP is idle.
    assert scheduler.reserve_handoffs(32) == []

    released = first_wave[0]
    released_l2 = released.ticket.service_cohort_keys[2]
    scheduler.on_terminal(
        released.ticket,
        passed=False,
        outcome="candidate_fail",
        busy_worker_ms=250,
        stage="fast_probe",
    )
    replacement = scheduler.reserve_handoffs(1)
    assert len(replacement) == 1
    assert replacement[0].ticket.service_cohort_keys[2] == released_l2

    metrics = scheduler.quick_metrics()
    assert metrics["cold_l2_training_current_limit"] == 8
    assert metrics["cold_l2_training_breadth"] == 2.0
    assert metrics["cold_l2_training_capacity"] == 32
    assert metrics["cold_l2_training_outstanding_high_water"] == 32
    assert metrics["cold_l2_training_deferred_total"] > 0
    snapshot = scheduler.cold_l2_training_snapshot()
    assert (snapshot["capacity"], snapshot["current_limit"]) == (32, 8)
    assert snapshot["outstanding_count"] == 32
    scheduler.cancel_handoff(replacement[0], requeue=False)
    assert scheduler.release_outstanding() == 31
    assert scheduler.pending_handoff_count() == 0


@pytest.mark.parametrize(
    ("sizes", "limits", "first_l2_counts"),
    [
        # One L2 may use every identity, then the whole widened pipeline.
        ((48,), (16, 32), (16, 32)),
        ((48, 48), (8, 16), (8, 16)),
        # A large first-arriving family still gets only its fair share of the
        # samples: a quarter of 16 identities, then a quarter of 32 leases.
        ((80, 10, 10, 10), (4, 8), (4, 8)),
        # Eight L2 cohorts: the absolute floor of four binds at both
        # breadths, and 8 x 4 already fills the 32 leases.
        ((12,) * 8, (4, 4), (4, 4)),
    ],
)
def test_cold_l2_training_breadth_scales_the_fair_share(
    sizes: tuple[int, ...],
    limits: tuple[int, int],
    first_l2_counts: tuple[int, int],
) -> None:
    for breadth, limit, first_l2_count in zip((1.0, 2.0), limits, first_l2_counts):
        scheduler = Tier3CandidateScheduler(unknown_exploration_ratio=0.15)
        scheduler.set_credit_envelope(16, handoff_window=32, tcp_execution_limit=32)
        scheduler.set_cold_l2_training_breadth(breadth)
        _cold_l2_inventory(scheduler, sizes)
        for _ in range(3):
            _responsive_wave(scheduler, 32)
        with scheduler._lock:
            outstanding = dict(scheduler._cold_l2_training_outstanding_unlocked())
        case = (sizes[:2], breadth)
        assert scheduler.cold_l2_training_snapshot()["current_limit"] == limit, case
        assert outstanding["service:l2-0"] == first_l2_count, case
        # No cohort holds more unresolved samples than the limit.
        assert max(outstanding.values()) <= limit, case
        assert scheduler.reserve_handoffs(32) == [], case
        scheduler.release_outstanding()


def test_cold_l2_training_limit_formula_by_breadth() -> None:
    scheduler = Tier3CandidateScheduler()
    for helpers in (1, 3, 4, 7, 16, 24, 64, 96, 128):
        for breadth in (1.0, 1.25, 1.5, 2.0, 3.0, 4.0):
            scheduler.set_credit_envelope(helpers, active_helper_ceiling=256)
            scheduler.set_cold_l2_training_breadth(breadth)
            capacity = tier3_tcp_breadth_scaled_limit(helpers, breadth)
            with scheduler._lock:
                assert scheduler._cold_l2_training_capacity_unlocked() == capacity
                for visible in range(80):
                    limit = scheduler._cold_l2_training_limit_for_cohorts_unlocked(
                        visible
                    )
                    cohorts = max(1, visible)
                    # Verbatim audited default formula.
                    audited = min(helpers, max(4, math.ceil(helpers / cohorts)))
                    case = (helpers, breadth, visible)
                    assert limit == min(
                        capacity, max(4, math.ceil(capacity / cohorts))
                    ), case
                    if breadth == 1.0:
                        assert limit == audited, case
                    # Never tighter than the default, and a cohort's share of
                    # the widened pipeline never exceeds its default share by
                    # more than one sample of rounding.
                    assert limit >= audited, case
                    assert limit * helpers <= audited * capacity + helpers, case
                    if (
                        breadth == 2.0
                        and helpers % cohorts == 0
                        and helpers // cohorts >= 4
                    ):
                        assert limit == 2 * audited, case


def test_cold_l2_training_breadth_change_takes_effect_at_once() -> None:
    scheduler = Tier3CandidateScheduler()
    scheduler.set_credit_envelope(16)
    _cold_l2_inventory(scheduler, (5, 5))

    def limit_and_capacity() -> tuple[int, int]:
        snapshot = scheduler.cold_l2_training_snapshot()
        return snapshot["current_limit"], snapshot["capacity"]

    assert limit_and_capacity() == (8, 16)
    scheduler.set_cold_l2_training_breadth(2.0)
    assert limit_and_capacity() == (16, 32)
    scheduler.set_cold_l2_training_breadth(1.5)
    assert limit_and_capacity() == (12, 24)
    # The transport ceiling of 256 bounds the widened capacity.
    scheduler.set_credit_envelope(96, active_helper_ceiling=128)
    scheduler.set_cold_l2_training_breadth(4.0)
    assert limit_and_capacity() == (128, 256)
    scheduler.set_cold_l2_training_breadth(1.0)
    assert limit_and_capacity() == (48, 96)


@pytest.mark.parametrize("breadth", [1.0, 0.5, 0, float("nan"), None, "wide"])
def test_cold_l2_training_default_breadth_is_the_audited_limit(breadth) -> None:
    import tier3_candidate_scheduler as module

    runs = []
    original_monotonic = module.time.monotonic
    original_monotonic_ns = module.time.monotonic_ns
    module.time.monotonic = lambda: 7_000.0
    module.time.monotonic_ns = lambda: 7_000_000_000_000
    try:
        for configure in (False, True):
            scheduler = Tier3CandidateScheduler(unknown_exploration_ratio=0.15)
            scheduler.set_credit_envelope(16, handoff_window=32, tcp_execution_limit=32)
            if configure:
                scheduler.set_cold_l2_training_breadth(breadth)
            _cold_l2_inventory(scheduler, (40, 12, 6, 3, 1))
            waves = []
            for _ in range(3):
                wave, _per_l2 = _responsive_wave(scheduler, 32)
                waves.append([item.candidate.node.name for item in wave])
                if wave:
                    scheduler.on_terminal(
                        wave[0].ticket,
                        passed=False,
                        outcome="candidate_fail",
                        busy_worker_ms=250,
                        stage="fast_probe",
                    )
            metrics = {
                key: value
                for key, value in scheduler.quick_metrics().items()
                if key != "last_tick_ms"
            }
            runs.append((waves, metrics, scheduler.cold_l2_training_snapshot()))
            scheduler.release_outstanding()
    finally:
        module.time.monotonic = original_monotonic
        module.time.monotonic_ns = original_monotonic_ns
    assert runs[0] == runs[1]
    assert "cold_l2_training_breadth" not in runs[1][1]
    assert "cold_l2_training_capacity" not in runs[1][1]
    assert runs[1][2]["breadth"] == 1.0
    assert runs[1][2]["capacity"] == 16


def test_cold_l2_training_report_fields_come_from_the_lock_free_memo() -> None:
    # The owner's report builders call quick_metrics(max_age_seconds=0.25),
    # which returns its memo without the scheduler lock while it is fresh.
    # At breadth > 1 that memo must carry every cold-L2 report field, with
    # the values the locked snapshot reports, so the builders need no
    # locked call of their own.
    scheduler = Tier3CandidateScheduler(unknown_exploration_ratio=0.15)
    scheduler.set_credit_envelope(16, handoff_window=32, tcp_execution_limit=32)
    scheduler.set_cold_l2_training_breadth(2.0)
    _cold_l2_inventory(scheduler, (20, 20, 20, 20))
    _responsive_wave(scheduler, 32)
    assert scheduler.reserve_handoffs(32) == []

    snapshot = scheduler.cold_l2_training_snapshot()
    primed = scheduler.quick_metrics(max_age_seconds=60.0)
    assert snapshot["breadth"] == primed["cold_l2_training_breadth"] == 2.0
    for key, value in snapshot.items():
        assert primed[f"cold_l2_training_{key}"] == value, key
    assert (snapshot["capacity"], snapshot["current_limit"]) == (32, 8)
    assert snapshot["deferred_total"] > 0

    held = threading.Event()
    release = threading.Event()
    released = threading.Event()

    def hold_lock() -> None:
        with scheduler._lock:
            held.set()
            release.wait(5.0)
        released.set()

    holder = threading.Thread(target=hold_lock)
    holder.start()
    try:
        assert held.wait(5.0)
        memo = scheduler.quick_metrics(max_age_seconds=60.0)
        # Returned while another thread still holds the scheduler lock.
        assert not released.is_set()
    finally:
        release.set()
        holder.join(5.0)
    assert memo == primed
    scheduler.release_outstanding()


def test_production_leaf_uses_guarded_two_net_new_proven_50() -> None:
    scheduler = Tier3CandidateScheduler(unknown_exploration_ratio=0.0)
    scheduler.set_credit_envelope(16, handoff_window=16, tcp_execution_limit=16)
    profiles = ("l0", "l1", "l2", "l3")

    scheduler.enqueue(
        candidate("training"),
        None,
        arm_key="training",
        source_root="root",
        family="opaque-family",
        coarse_key="training",
        feature_key=feature(
            family="opaque-family",
            service_profiles=profiles,
        ),
    )
    training = scheduler.reserve_handoffs(1)[0]
    scheduler.commit_handoff(training)
    scheduler.on_tcp_outcome(training.ticket, responsive=True)
    scheduler.on_terminal(
        training.ticket,
        passed=True,
        outcome="pass",
        busy_worker_ms=200,
        stage="fast_probe",
    )
    scheduler.on_commit_complete(training.ticket, net_new=False)

    for index in range(24):
        scheduler.enqueue(
            candidate(f"holdout-{index}"),
            None,
            arm_key=f"holdout-{index}",
            source_root="root",
            family="opaque-family",
            coarse_key=f"holdout-{index}",
            feature_key=feature(
                family="opaque-family",
                service_profiles=profiles,
            ),
        )
    confirmation_wave = scheduler.reserve_handoffs(16)
    confirmation = next(
        item
        for item in confirmation_wave
        if item.ticket.reward_cohort_state == "leaf_confirmation"
    )
    for item in confirmation_wave:
        if item is not confirmation:
            scheduler.cancel_handoff(item, requeue=True)
    scheduler.commit_handoff(confirmation)
    scheduler.on_tcp_outcome(confirmation.ticket, responsive=True)
    scheduler.on_terminal(
        confirmation.ticket,
        passed=True,
        outcome="pass",
        busy_worker_ms=200,
        stage="fast_probe",
    )
    scheduler.on_commit_complete(confirmation.ticket, net_new=False)

    wave = scheduler.reserve_handoffs(16)
    probation = [
        item
        for item in wave
        if item.ticket.reward_cohort_state == "leaf_probation"
    ]
    assert len(probation) == 4
    for index, item in enumerate(probation[:2]):
        scheduler.commit_handoff(item)
        scheduler.on_tcp_outcome(item.ticket, responsive=True)
        scheduler.on_terminal(
            item.ticket,
            passed=True,
            outcome="pass",
            busy_worker_ms=200,
            stage="fast_probe",
        )
        scheduler.on_commit_complete(item.ticket, net_new=True)
        with scheduler._lock:
            state = scheduler._cohort_yield_states["service:l3"]
            if index == 0:
                assert state.eligibility.value == "leaf_probation"
    with scheduler._lock:
        state = scheduler._cohort_yield_states["service:l3"]
        assert state.eligibility.value == "proven_50"
        assert state.last_decision_reason == (
            "probation_holdout_2_net_new_guarded_proven"
        )
    for item in wave:
        if item not in probation[:2]:
            scheduler.cancel_handoff(item, requeue=False)

    confirmation_wave = scheduler.reserve_handoffs(16)
    proven = [
        item
        for item in confirmation_wave
        if item.ticket.reward_cohort_state == "proven_50"
    ]
    assert len(proven) == 4
    for item in proven[:2]:
        scheduler.commit_handoff(item)
        scheduler.on_tcp_outcome(item.ticket, responsive=True)
        scheduler.on_terminal(
            item.ticket,
            passed=False,
            outcome="candidate_fail",
            busy_worker_ms=200,
            stage="fast_probe",
        )
    with scheduler._lock:
        state = scheduler._cohort_yield_states["service:l3"]
        assert state.eligibility.value == "demoted"
        assert state.last_decision_reason == "proven_50_confirmation_failed"
    for item in confirmation_wave:
        if item not in proven[:2]:
            scheduler.cancel_handoff(item, requeue=False)
    assert scheduler.release_outstanding() == 0


def test_equal_yield_cohorts_use_evidence_order_not_cohort_key() -> None:
    scheduler = Tier3CandidateScheduler(unknown_exploration_ratio=0.0)
    for profile in ("z-first", "a-second"):
        for index in range(2):
            name = f"{profile}-{index}"
            scheduler.enqueue(
                candidate(name), None, arm_key=name, coarse_key=name,
                source_root="opaque", family="opaque",
                feature_key=feature(service_profiles=("l0", "l1", "l2", profile)),
            )
            sample = scheduler.reserve_handoffs(1)[0]
            scheduler.commit_handoff(sample)
            scheduler.on_tcp_outcome(sample.ticket, responsive=True)
            scheduler.on_terminal(sample.ticket, passed=True, outcome="pass",
                                  stage="fast_probe", busy_worker_ms=200)
            scheduler.on_commit_complete(sample.ticket, net_new=False)
    for profile in ("z-first", "a-second"):
        scheduler.enqueue(
            candidate(f"{profile}-inventory"), None, arm_key=profile, coarse_key=profile,
            source_root="opaque", family="opaque",
            feature_key=feature(service_profiles=("l0", "l1", "l2", profile)),
        )
    with scheduler._lock:
        ordered = scheduler._production_service_cohorts_with_inventory_unlocked()
    assert [key for key, _state in ordered] == [
        "service:z-first",
        "service:a-second",
    ]


def test_large_supported_source_uses_bounded_linear_scheduler_tick() -> None:
    scheduler = legacy_candidate_scheduler(max_active_arms_per_family=16)
    for index in range(3000):
        scheduler.enqueue(
            candidate(f"large-{index}"),
            {"source": "large"},
            arm_key=f"large-arm-{index}",
            source_root="large-root",
            family="vless",
            coarse_key=f"port-bucket-{index}",
        )

    selected = scheduler.drain(32)
    metrics = scheduler.quick_metrics()

    # Thousands of coarse arms do not create extra root/family shares, but a
    # single ready bucket may borrow the complete bounded TCP window.
    assert len(selected) == 32
    assert metrics["active_arm_count"] <= 16
    assert metrics["overflow_arm_count"] > 0
    assert metrics["last_tick_keys_examined"] <= 32 * 40
    assert metrics["backlog_count"] == 2968
    assert metrics["normal_reservable_count"] == 32


def test_unknown_coverage_rotates_unseen_overflow_before_active_arms_drain() -> None:
    scheduler = legacy_candidate_scheduler(
        max_active_arms_per_family=2,
        unknown_exploration_ratio=1.0,
    )
    for arm_index in range(6):
        for candidate_index in range(8):
            scheduler.enqueue(
                candidate(f"arm-{arm_index}-{candidate_index}"),
                None,
                arm_key=f"arm-{arm_index}",
                source_root="root",
                family="vless",
                coarse_key=f"coarse-{arm_index}",
            )

    first_wave = scheduler.drain(2)
    assert {item.coarse_key for item in first_wave} == {"coarse-0", "coarse-1"}
    for item in first_wave:
        scheduler.on_tcp_outcome(item.dispatch_ticket, responsive=False)

    second_wave = scheduler.drain(2)

    assert {item.coarse_key for item in second_wave} == {"coarse-2", "coarse-3"}
    metrics = scheduler.quick_metrics()
    assert metrics["active_arm_count"] == 2
    assert metrics["overflow_arm_count"] == 4
    assert metrics["overflow_rotation_count"] == 2
    assert metrics["unknown_overflow_rotation_count"] == 2
    assert metrics["last_tick_keys_examined"] <= 8


def test_hot_supply_metrics_never_scan_all_coarse_arms() -> None:
    scheduler = legacy_candidate_scheduler(max_active_arms_per_family=16)
    for index in range(5000):
        scheduler.enqueue(
            candidate(f"hot-{index}"),
            None,
            arm_key=f"hot-arm-{index}",
            source_root=f"root-{index % 5}",
            family=("vless", "vmess", "trojan", "ss")[index % 4],
            coarse_key=f"coarse-{index}",
        )

    class NoWholeArmTraversal(dict[str, object]):
        def values(self):  # type: ignore[override]
            raise AssertionError("hot metrics traversed every coarse arm")

        def items(self):  # type: ignore[override]
            raise AssertionError("hot metrics traversed every coarse arm")

        def __iter__(self):
            raise AssertionError("hot metrics traversed every coarse arm")

    scheduler._arms = NoWholeArmTraversal(scheduler._arms)  # type: ignore[assignment]

    supply = scheduler.supply_snapshot()
    metrics = scheduler.quick_metrics()

    assert supply.raw_backlog == 5000
    assert supply.unique_backlog == 5000
    assert supply.normal_reservable_count == 64
    assert metrics["backlog_count"] == 5000
    assert metrics["ready_arm_count"] == 5000
    assert metrics["normal_dispatchable_arm_count"] == 5000


def test_zero_dispatch_budget_does_not_select_or_consume_scheduler_state() -> None:
    scheduler = legacy_candidate_scheduler(max_active_arms_per_family=16)
    for index in range(256):
        scheduler.enqueue(
            candidate(f"token-zero-{index}"),
            None,
            arm_key=f"arm-{index % 16}",
            source_root="root",
            family="vless",
            coarse_key=f"coarse-{index % 16}",
        )

    before = scheduler.quick_metrics()
    assert scheduler.drain(0) == []
    after = scheduler.quick_metrics()
    assert after["selected_total"] == before["selected_total"] == 0
    assert after["backlog_count"] == before["backlog_count"] == 256
    assert after["tick_count"] == before["tick_count"] == 0


def test_fast_observation_survives_deep_retention_overflow_terminal() -> None:
    scheduler = legacy_candidate_scheduler(unknown_exploration_ratio=0.0)
    scheduler.enqueue(
        candidate("accepted-fast-failure"),
        None,
        arm_key="accepted-fast-failure-arm",
        source_root="root-observation",
        family="vless",
        coarse_key="coarse-observation",
        feature_key=feature(
            service_profile="leaf-observation",
            service_profiles=("l0", "l1", "l2", "leaf-observation"),
        ),
    )
    selected = scheduler.drain(1)
    assert len(selected) == 1
    ticket = selected[0].dispatch_ticket
    assert ticket is not None
    scheduler.on_tcp_outcome(ticket, responsive=True)

    scheduler.on_fast_observation(
        ticket,
        outcome="candidate_fail",
        busy_worker_ms=3000,
        passed=False,
    )
    scheduler.on_fast_yield(
        ticket,
        outcome="candidate_fail",
        busy_worker_ms=3000,
    )
    scheduler.on_terminal(
        ticket,
        outcome="deep_retention_window_full",
        busy_worker_ms=3000,
        stage="fast_probe",
    )

    metrics = scheduler.quick_metrics()
    assert metrics["fast_observation_sample_count"] == 1
    assert metrics["fast_observation_completion_count"] == 1
    assert metrics["fast_observation_pass_count"] == 0


def test_transient_fast_observation_remains_exposure_after_overflow() -> None:
    scheduler = legacy_candidate_scheduler(unknown_exploration_ratio=0.0)
    scheduler.enqueue(
        candidate("accepted-fast-censor"),
        None,
        arm_key="accepted-fast-censor-arm",
        source_root="root-censor",
        family="vless",
        coarse_key="coarse-censor",
    )
    selected = scheduler.drain(1)
    ticket = selected[0].dispatch_ticket
    assert ticket is not None
    scheduler.on_tcp_outcome(ticket, responsive=True)

    scheduler.on_fast_observation(
        ticket,
        outcome="transient",
        busy_worker_ms=3000,
        right_censored=True,
    )
    scheduler.on_fast_yield(
        ticket,
        outcome="transient",
        busy_worker_ms=3000,
        right_censored=True,
    )
    scheduler.on_terminal(
        ticket,
        outcome="deep_retention_window_full",
        busy_worker_ms=3000,
        stage="fast_probe",
    )

    metrics = scheduler.quick_metrics()
    assert metrics["fast_observation_sample_count"] == 1
    assert metrics["fast_observation_completion_count"] == 0
    assert metrics["fast_observation_pass_count"] == 0


def test_overflow_tail_preserves_full_fast_denominator_and_does_not_promote() -> None:
    scheduler = legacy_candidate_scheduler(unknown_exploration_ratio=0.0)
    total = 512
    passed_total = 5
    shared_feature = feature(
        service_profile="low-yield-leaf",
        service_profiles=("low-yield-l0", "low-yield-l1", "low-yield-leaf"),
    )

    for index in range(total):
        scheduler.enqueue(
            candidate(f"overflow-tail-{index}"),
            None,
            arm_key=f"overflow-tail-arm-{index}",
            source_root=f"opaque-root-{index % 3}",
            family="opaque-family",
            coarse_key=f"overflow-tail-coarse-{index}",
            feature_key=shared_feature,
        )
        reservation = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
        passed = index < passed_total
        scheduler.on_fast_observation(
            reservation.ticket,
            outcome="pass" if passed else "candidate_fail",
            busy_worker_ms=3000,
            passed=passed,
        )
        if passed:
            scheduler.on_terminal(
                reservation.ticket,
                outcome="pass",
                busy_worker_ms=3000,
                passed=True,
                stage="fast_probe",
            )
            scheduler.on_commit_complete(reservation.ticket, net_new=True)
        else:
            scheduler.on_fast_yield(
                reservation.ticket,
                outcome="candidate_fail",
                busy_worker_ms=3000,
            )
            scheduler.on_terminal(
                reservation.ticket,
                outcome="deep_retention_window_full",
                busy_worker_ms=3000,
                stage="fast_probe",
            )

    metrics = scheduler.quick_metrics()
    assert metrics["fast_observation_completion_count"] == total
    assert metrics["fast_observation_pass_count"] == passed_total
    assert metrics["reward_service_sample_completion_count"] == total
    assert metrics["reward_service_sample_pass_count"] == passed_total
    assert metrics["reward_service_promoted_cohort_count"] == 0
    assert metrics["feedback_only_ticket_count"] == 0
    assert scheduler.release_outstanding() == 0


def test_delayed_root_is_scheduled_within_two_release_cycles() -> None:
    scheduler = legacy_candidate_scheduler(max_active_arms_per_family=16)
    for index in range(64):
        scheduler.enqueue(
            candidate(f"early-{index}"),
            None,
            arm_key=f"early-{index % 8}",
            source_root="early-root",
            family="ss",
            coarse_key=f"early-coarse-{index % 8}",
        )
    first_wave = scheduler.drain(4)
    assert len(first_wave) == 4
    for item in first_wave:
        scheduler.on_tcp_outcome(item.dispatch_ticket, responsive=False)

    scheduler.enqueue(
        candidate("late-good"),
        None,
        arm_key="late-good",
        source_root="late-root",
        family="vless",
        coarse_key="late-coarse",
    )

    next_two_cycles = [*scheduler.drain(4), *scheduler.drain(4)]
    assert "late-good" in {item.node.name for item in next_two_cycles}


def test_first_round_covers_protocol_families_deterministically() -> None:
    scheduler = legacy_candidate_scheduler()
    families = ["vless", "vmess", "trojan", "ss", "hysteria2", "unknown"]
    for family in reversed(families):
        scheduler.enqueue(
            candidate(family, family),
            None,
            arm_key=family,
            source_root="root",
            family=family,
            coarse_key=family,
        )

    selected = scheduler.drain(6)
    selected_families = [item.family for item in selected]
    assert set(selected_families) == {
        "vless",
        "vmess",
        "trojan",
        "ss",
        "hysteria2",
        "other",
    }
    assert "modern" not in selected_families


def test_tcp_responsive_feedback_does_not_earn_full_validation_reward() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.15,
        reward_wave_limit=2,
        reward_candidate_limit=8,
    )
    for index in range(24):
        arm = "reward" if index < 16 else f"unknown-{index}"
        coarse = "reward" if index < 16 else arm
        source_root = "root" if index < 16 else f"unknown-root-{index}"
        scheduler.enqueue(
            candidate(f"node-{index}"),
            None,
            arm_key=arm,
            source_root=source_root,
            family="vless" if index < 16 else "trojan",
            coarse_key=coarse,
        )
    scheduler.record_feedback("reward", responsive=True)

    selected = scheduler.drain(16)
    reward_selected = sum(item.selection_lane == "reward" for item in selected)
    unknown_selected = sum(item.arm_key.startswith("unknown-") for item in selected)

    assert reward_selected == 0
    assert unknown_selected >= 2


def test_cancelled_tcp_outcome_releases_credit_without_marking_arm_explored() -> None:
    scheduler = legacy_candidate_scheduler(unknown_exploration_ratio=1.0)
    for index in range(2):
        scheduler.enqueue(
            candidate(f"deferred-{index}"),
            None,
            arm_key="deferred-arm",
            source_root="root",
            family="vless",
            coarse_key="deferred-arm",
        )

    first = scheduler.drain(1)[0]
    assert first.selection_lane == "coverage"
    assert first.dispatch_ticket is not None
    assert first.dispatch_ticket.unknown_exploration is True

    scheduler.on_tcp_outcome(
        first.dispatch_ticket,
        responsive=False,
        cancelled=True,
    )

    after_cancel = scheduler.quick_metrics()
    assert after_cancel["tcp_outcome_count"] == 0
    assert after_cancel["fast_credit_outstanding"] == 0
    assert after_cancel["active_ticket_count"] == 0

    second = scheduler.drain(1)[0]
    assert second.dispatch_ticket is not None
    assert second.dispatch_ticket.unknown_exploration is True


def test_hard_tcp_failure_does_not_replace_missing_fast_evidence() -> None:
    scheduler = legacy_candidate_scheduler(unknown_exploration_ratio=1.0)
    for index in range(2):
        scheduler.enqueue(
            candidate(f"hard-failure-{index}"),
            None,
            arm_key="hard-failure-arm",
            source_root="root",
            family="vless",
            coarse_key="hard-failure-arm",
        )

    first = scheduler.drain(1)[0]
    scheduler.on_tcp_outcome(first.dispatch_ticket, responsive=False)

    second = scheduler.drain(1)[0]
    metrics = scheduler.quick_metrics()
    assert second.selection_lane == "exploration"
    assert metrics["tcp_outcome_count"] == 1


def test_full_validation_pass_earns_bounded_reward_without_removing_exploration() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.15,
        reward_wave_limit=2,
        reward_candidate_limit=8,
    )
    for index in range(24):
        arm = "reward" if index < 16 else f"unknown-{index}"
        coarse = "reward" if index < 16 else arm
        scheduler.enqueue(
            candidate(f"node-{index}"),
            None,
            arm_key=arm,
            source_root=("root" if index < 16 else f"unknown-root-{index}"),
            family=("vless" if index < 16 else "trojan"),
            coarse_key=coarse,
        )
    scheduler.record_feedback("reward", passed=True)

    selected = scheduler.drain(16)
    reward_selected = sum(item.selection_lane == "reward" for item in selected)
    unknown_selected = sum(item.arm_key.startswith("unknown-") for item in selected)

    assert 0 < reward_selected <= 8
    assert unknown_selected >= 2


def test_thousand_mixed_feedback_transactions_release_all_credit() -> None:
    scheduler = legacy_candidate_scheduler(
        max_active_arms_per_family=16,
        unknown_exploration_ratio=0.15,
    )
    for index in range(1000):
        scheduler.enqueue(
            candidate(f"mixed-{index}", "vless" if index % 2 == 0 else "trojan"),
            None,
            arm_key=f"arm-{index % 32}",
            source_root=f"root-{index % 5}",
            family="vless" if index % 2 == 0 else "trojan",
            coarse_key=f"coarse-{index % 32}",
        )

    handled = 0
    while scheduler.size() > 0:
        selected = scheduler.drain(32)
        assert selected, scheduler.quick_metrics()
        for item in selected:
            ticket = item.dispatch_ticket
            assert ticket is not None
            sequence = handled
            handled += 1
            if sequence % 11 == 0:
                scheduler.on_tcp_outcome(ticket, responsive=False, cancelled=True)
                scheduler.on_terminal(ticket, outcome="cancelled")
            elif sequence % 5 == 0:
                scheduler.on_tcp_outcome(ticket, responsive=True)
                scheduler.on_terminal(ticket, passed=True, outcome="pass")
                scheduler.on_commit_complete(ticket, net_new=sequence % 10 == 0)
                scheduler.on_commit_complete(ticket, net_new=False)
            elif sequence % 3 == 0:
                scheduler.on_tcp_outcome(ticket, responsive=True)
                scheduler.on_fast_yield(ticket)
                scheduler.on_fast_yield(ticket)
                scheduler.on_terminal(ticket, outcome="right_censored")
                scheduler.on_terminal(ticket, outcome="right_censored")
            else:
                scheduler.on_tcp_outcome(ticket, responsive=True)
                scheduler.on_terminal(ticket, outcome="candidate_fail")

    assert handled == 1000
    scheduler.release_outstanding()
    metrics = scheduler.quick_metrics()
    assert metrics["active_ticket_count"] == 0
    assert metrics["fast_credit_outstanding"] == 0
    assert metrics["parked_deep_debt"] == 0
    assert metrics["reward_exact_outstanding_count"] == 0
    assert metrics["reward_service_outstanding_count"] == 0
    assert metrics["reward_probation_outstanding_count"] == 0
    assert metrics["reward_discovery_outstanding_count"] == 0
    assert metrics["reservation_total"] == 1000
    assert metrics["selected_total"] == 909
    assert metrics["cancelled_reservation_total"] == 91


def test_zero_learning_correction_keeps_baseline_trace_identical() -> None:
    baseline = legacy_candidate_scheduler(unknown_exploration_ratio=0.0)
    shadow = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        score_provider=lambda _candidate: 0.0,
    )
    for index in range(24):
        for scheduler in (baseline, shadow):
            scheduler.enqueue(
                candidate(f"node-{index}"),
                None,
                arm_key=f"arm-{index}",
                source_root="root",
                family="vless",
                coarse_key=f"coarse-{index}",
            )

    baseline_trace = [item.node.name for item in baseline.drain(16)]
    shadow_trace = [item.node.name for item in shadow.drain(16)]
    assert shadow_trace == baseline_trace


def test_bounded_learning_tie_break_cannot_permanently_starve_lower_scored_arms() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        learning_window=8,
        score_provider=lambda item: 0.2 if item.arm_key == "favored" else -0.2,
    )
    for arm in ("favored", "ordinary-a", "ordinary-b"):
        for index in range(4):
            scheduler.enqueue(
                candidate(f"{arm}-{index}"),
                None,
                arm_key=arm,
                source_root="root",
                family="vless",
                coarse_key=arm,
            )

    first_four: list[str] = []
    for _index in range(4):
        item = scheduler.drain(1)[0]
        first_four.append(item.arm_key)
        scheduler.commit_handoff(item.dispatch_ticket)
        scheduler.on_terminal(item.dispatch_ticket, passed=False)
    assert first_four[0] == "favored"
    assert "ordinary-a" in first_four
    assert "ordinary-b" in first_four


def test_tcp_responsive_releases_handoff_capacity_before_fast_terminal() -> None:
    assert Tier3CandidateDispatchTicket is Tier3CandidateFeedbackTicket
    scheduler = legacy_candidate_scheduler(unknown_exploration_ratio=0.0)
    scheduler.set_credit_envelope(16, handoff_window=2)
    for index in range(12):
        scheduler.enqueue(
            candidate(f"same-arm-{index}"),
            None,
            arm_key=f"arm-{index}",
            source_root="root-a",
            family="vless",
            coarse_key=f"coarse-{index}",
        )

    first = scheduler.drain(8)
    assert len(first) == 2
    for item in first:
        scheduler.on_tcp_outcome(item.dispatch_ticket, responsive=True)

    second = scheduler.drain(8)
    assert len(second) == 2
    supply = scheduler.supply_snapshot()
    assert supply.tcp_handoff_lease_count == 2
    assert supply.feedback_only_ticket_count == 2
    for item in second:
        scheduler.on_tcp_outcome(item.dispatch_ticket, responsive=True)

    third = scheduler.drain(8)
    assert len(third) == 2
    assert all(item.selection_lane != "credit_escape" for item in third)

    metrics = scheduler.quick_metrics()
    assert metrics["fast_credit_outstanding"] == 2
    assert metrics["fast_credit_outstanding"] == metrics["tcp_handoff_lease_count"]
    assert metrics["feedback_only_ticket_count"] == 4
    assert metrics["root_family_fast_credit_limit"] == 0


def test_feedback_only_ticket_never_reduces_shared_handoff_window() -> None:
    scheduler = legacy_candidate_scheduler(unknown_exploration_ratio=0.0)
    scheduler.set_credit_envelope(16, handoff_window=4)
    for index in range(8):
        scheduler.enqueue(
            candidate(f"future-deep-{index}"),
            None,
            arm_key=f"future-deep-arm-{index}",
            source_root="root-a",
            family="vless",
            coarse_key=f"future-deep-coarse-{index}",
        )

    first = scheduler.drain(2)
    assert len(first) == 2
    for item in first:
        scheduler.on_tcp_outcome(item.dispatch_ticket, responsive=True)
        scheduler.on_fast_yield(item.dispatch_ticket)

    supply = scheduler.supply_snapshot()
    assert supply.raw_backlog == 6
    assert supply.normal_reservable_count == 4
    assert supply.fast_supply_state == "ready"
    assert supply.tcp_handoff_lease_count == 0
    assert supply.feedback_only_ticket_count == 2
    assert supply.deep_retained_count == 2

    second = scheduler.drain(8)
    assert len(second) == 4


def test_fast_censor_is_exposure_not_completed_arm_learning() -> None:
    scheduler = legacy_candidate_scheduler(unknown_exploration_ratio=0.0)
    scheduler.enqueue(
        candidate("censored"),
        None,
        arm_key="censored-arm",
        source_root="root-a",
        family="vless",
        coarse_key="censored",
    )
    item = scheduler.drain(1)[0]
    scheduler.on_tcp_outcome(item.dispatch_ticket, responsive=True)
    scheduler.on_fast_yield(
        item.dispatch_ticket,
        right_censored=True,
        busy_worker_ms=3_000,
    )

    with scheduler._lock:
        ticket_state = scheduler._tickets[item.dispatch_ticket.ticket_id]
        assert not scheduler._arms[ticket_state.arm_id].validation_observed

    completed = legacy_candidate_scheduler(unknown_exploration_ratio=0.0)
    completed.enqueue(
        candidate("completed"),
        None,
        arm_key="completed-arm",
        source_root="root-a",
        family="vless",
        coarse_key="completed",
    )
    item = completed.drain(1)[0]
    completed.on_tcp_outcome(item.dispatch_ticket, responsive=True)
    completed.on_fast_yield(
        item.dispatch_ticket,
        right_censored=False,
        busy_worker_ms=1_000,
    )
    with completed._lock:
        ticket_state = completed._tickets[item.dispatch_ticket.ticket_id]
        assert completed._arms[ticket_state.arm_id].validation_observed


def test_root_family_deep_debt_does_not_close_fast_breadth() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        work_conserving_parked_deep_limit=16,
    )
    scheduler.set_credit_envelope(16)
    for index in range(160):
        scheduler.enqueue(
            candidate(f"borrow-{index}"),
            None,
            arm_key=f"borrow-arm-{index}",
            source_root="root-a",
            family="vless",
            coarse_key=f"coarse-{index}",
        )

    handed_off = 0
    for revision in range(1, 100):
        wave = scheduler.drain(8, control_revision=revision)
        if not wave:
            break
        handed_off += len(wave)
        for item in wave:
            scheduler.on_fast_yield(item.dispatch_ticket)

    metrics = scheduler.quick_metrics()
    assert handed_off == 160
    assert metrics["parked_deep_debt"] == 160
    assert metrics["fast_credit_outstanding"] == 0
    supply = scheduler.supply_snapshot()
    assert supply.fast_supply_state == "exhausted"
    assert supply.raw_backlog == 0
    assert metrics["deep_credit_blocks_fast"] is False


def test_few_root_families_keep_fast_breadth_past_deep_diagnostic_window() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        work_conserving_parked_deep_limit=16,
    )
    scheduler.set_credit_envelope(16)
    buckets = [
        (f"root-{index // 3}", ("vless", "vmess", "trojan")[index % 3])
        for index in range(9)
    ]
    for bucket_index, (root, family) in enumerate(buckets):
        for index in range(40):
            scheduler.enqueue(
                candidate(f"window-{bucket_index}-{index}"),
                None,
                arm_key=f"window-arm-{bucket_index}-{index}",
                source_root=root,
                family=family,
                coarse_key=f"coarse-{index}",
            )

    normal_handoffs = 0
    for revision in range(1, 80):
        wave = scheduler.drain(32, control_revision=revision)
        if not wave:
            break
        normal_handoffs += len(wave)
        for item in wave:
            scheduler.on_fast_yield(item.dispatch_ticket)

    metrics = scheduler.quick_metrics()
    assert normal_handoffs == 360
    assert metrics["parked_deep_debt"] == 360
    assert metrics["root_family_deep_credit_floor"] == 4
    assert metrics["root_family_deep_credit_borrow_limit"] == 16
    assert metrics["root_family_deep_credit_enforced"] is False
    assert metrics["global_deep_credit_limit"] == 128

    assert metrics["deep_credit_blocks_fast"] is False
    assert scheduler.supply_snapshot().raw_backlog == 0


def test_one_ready_bucket_borrows_the_complete_shared_tcp_window() -> None:
    scheduler = legacy_candidate_scheduler(unknown_exploration_ratio=0.0)
    scheduler.set_credit_envelope(16, handoff_window=32)
    for index in range(80):
        scheduler.enqueue(
            candidate(f"only-{index}"),
            None,
            arm_key=f"arm-{index}",
            source_root="opaque-root",
            family="vless",
            coarse_key=f"coarse-{index}",
        )

    wave = scheduler.reserve_handoffs(64, control_revision=1)
    assert len(wave) == 32
    scheduler.commit_handoffs(wave)
    for lease in wave:
        scheduler.on_tcp_outcome(lease.ticket, responsive=True)

    supply = scheduler.supply_snapshot()
    assert supply.tcp_handoff_lease_count == 0
    assert supply.feedback_only_ticket_count == 32
    assert supply.normal_reservable_count == 32
    assert supply.escape_eligible_bucket_count == 0


def test_root_family_rounds_are_source_identity_agnostic() -> None:
    def normalized_trace(root_names: list[str]) -> list[tuple[int, str]]:
        scheduler = legacy_candidate_scheduler(unknown_exploration_ratio=0.0)
        root_positions = {root: index for index, root in enumerate(root_names)}
        for root in root_names:
            for family in ("vless", "ss"):
                for index in range(4):
                    scheduler.enqueue(
                        candidate(f"{root}-{family}-{index}"),
                        None,
                        arm_key=f"arm-{index}",
                        source_root=root,
                        family=family,
                        coarse_key=f"coarse-{index}",
                    )
        wave = scheduler.reserve_handoffs(16, control_revision=7)
        return [
            (root_positions[item.candidate.source_root], item.candidate.family)
            for item in wave
        ]

    for root_count in (1, 2, 5, 17):
        baseline = [f"root-{index}" for index in range(root_count)]
        renamed = [f"opaque-{(index * 37 + 11) % 101}" for index in range(root_count)]
        assert normalized_trace(baseline) == normalized_trace(renamed)


def test_first_two_tcp_release_cycles_fill_32_real_handoffs_at_low_yield() -> None:
    scheduler = legacy_candidate_scheduler(unknown_exploration_ratio=0.15)
    scheduler.set_credit_envelope(16, handoff_window=64, free_fast_slots=12)
    for index in range(256):
        scheduler.enqueue(
            candidate(f"low-yield-{index}"),
            {"route": f"https://opaque.invalid/{index % 9}"},
            arm_key=f"arm-{index % 32}",
            source_root=f"root-{index % 5}",
            family=("vless", "vmess", "trojan", "ss")[index % 4],
            coarse_key=f"coarse-{index % 32}",
        )

    handed_off = 0
    for revision in (1, 2):
        wave = scheduler.reserve_handoffs(16, control_revision=revision)
        assert len(wave) == 16
        scheduler.commit_handoffs(wave)
        handed_off += len(wave)
        for index, lease in enumerate(wave):
            scheduler.on_tcp_outcome(
                lease.ticket,
                responsive=index % 4 == 0,
            )
            if index % 4 == 0:
                scheduler.on_terminal(
                    lease.ticket,
                    passed=index % 28 == 0,
                )
                scheduler.on_commit_complete(
                    lease.ticket,
                    net_new=index % 28 == 0,
                )

    assert handed_off == 32
    metrics = scheduler.quick_metrics()
    assert metrics["tcp_handoff_total"] == 32
    assert metrics["tcp_handoff_lease_count"] == 0


def test_tcp_responsive_lcb_uses_only_the_latest_64_outcomes() -> None:
    scheduler = legacy_candidate_scheduler(unknown_exploration_ratio=0.0)
    scheduler.set_credit_envelope(16, handoff_window=64)

    for phase, responsive in (("dead", False), ("live", True)):
        for index in range(64):
            scheduler.enqueue(
                candidate(f"{phase}-{index}"),
                None,
                arm_key=f"{phase}-{index}",
                source_root=f"root-{index % 4}",
                family="vless",
                coarse_key=f"coarse-{index}",
            )
        wave = scheduler.reserve_handoffs(64)
        scheduler.commit_handoffs(wave)
        for lease in wave:
            scheduler.on_tcp_outcome(lease.ticket, responsive=responsive)
            if responsive:
                scheduler.on_terminal(lease.ticket, passed=False)
        if phase == "dead":
            assert scheduler.supply_snapshot().tcp_responsive_probability_lcb == 0.0

    assert scheduler.supply_snapshot().tcp_responsive_probability_lcb > 0.9


def test_fast_supply_imminent_uses_observed_lcb_without_prior_floor() -> None:
    scheduler = legacy_candidate_scheduler(unknown_exploration_ratio=0.0)
    scheduler.set_credit_envelope(16, handoff_window=64, free_fast_slots=1)

    for index in range(64):
        scheduler.enqueue(
            candidate(f"dead-{index}"),
            None,
            arm_key=f"dead-{index}",
            source_root=f"root-{index % 4}",
            family="vless",
            coarse_key=f"dead-{index}",
        )
    dead_wave = scheduler.reserve_handoffs(64)
    scheduler.commit_handoffs(dead_wave)
    for lease in dead_wave:
        scheduler.on_tcp_outcome(lease.ticket, responsive=False)

    for index in range(16):
        scheduler.enqueue(
            candidate(f"pending-{index}"),
            None,
            arm_key=f"pending-{index}",
            source_root=f"root-{index % 4}",
            family="vless",
            coarse_key=f"pending-{index}",
        )
    pending_wave = scheduler.reserve_handoffs(16)
    scheduler.commit_handoffs(pending_wave)

    supply = scheduler.supply_snapshot()
    assert supply.tcp_responsive_probability_lcb == 0.0
    assert supply.expected_responsive_arrivals == 0
    assert supply.fast_supply_state == "blocked"
    assert scheduler.release_outstanding() == 16


def test_hard_censored_tcp_starts_remain_in_supply_denominator() -> None:
    scheduler = legacy_candidate_scheduler(unknown_exploration_ratio=0.0)
    scheduler.set_credit_envelope(16, handoff_window=64, free_fast_slots=1)
    for index in range(64):
        scheduler.enqueue(
            candidate(f"sample-{index}"),
            None,
            arm_key=f"sample-{index}",
            source_root=f"root-{index % 4}",
            family="vless",
            coarse_key=f"sample-{index}",
        )
    wave = scheduler.reserve_handoffs(64)
    scheduler.commit_handoffs(wave)
    for index, lease in enumerate(wave):
        scheduler.on_tcp_outcome(
            lease.ticket,
            responsive=index < 32,
            hard_censored=index >= 32,
        )
        if index < 32:
            scheduler.on_terminal(lease.ticket, passed=False)

    supply = scheduler.supply_snapshot()
    assert supply.tcp_start_acked_sample_count == 64
    assert supply.tcp_completed_sample_count == 32
    assert supply.tcp_hard_censored_sample_count == 32
    assert supply.tcp_completed_result_rate == 0.5
    assert supply.tcp_start_acked_responsive_rate == 0.5
    assert 0.3 < supply.tcp_responsive_probability_lcb < 0.5

    for index in range(16):
        scheduler.enqueue(
            candidate(f"pending-after-sample-{index}"),
            None,
            arm_key=f"pending-after-sample-{index}",
            source_root=f"pending-root-{index % 4}",
            family="vless",
            coarse_key=f"pending-after-sample-{index}",
        )
    pending_wave = scheduler.reserve_handoffs(16)
    scheduler.commit_handoffs(pending_wave)
    supply = scheduler.supply_snapshot()
    assert supply.tcp_start_acked_sample_count == 64
    assert supply.tcp_start_acked_responsive_rate == 0.25
    assert supply.expected_redeemable_fast_arrivals == 4
    assert supply.expected_responsive_arrivals == 4
    assert scheduler.release_outstanding() == 16


def test_late_root_and_family_join_the_next_hierarchical_round() -> None:
    scheduler = legacy_candidate_scheduler(unknown_exploration_ratio=0.0)
    for index in range(64):
        scheduler.enqueue(
            candidate(f"large-{index}"),
            None,
            arm_key=f"large-{index}",
            source_root="large-root",
            family="vless",
            coarse_key=f"coarse-{index}",
        )
    first = scheduler.reserve_handoffs(4, control_revision=1)
    scheduler.commit_handoffs(first)
    for lease in first:
        scheduler.on_tcp_outcome(lease.ticket, responsive=False)

    scheduler.enqueue(
        candidate("late-vmess"),
        None,
        arm_key="late-vmess",
        source_root="large-root",
        family="vmess",
        coarse_key="late-vmess",
    )
    scheduler.enqueue(
        candidate("late-root"),
        None,
        arm_key="late-root",
        source_root="new-root",
        family="trojan",
        coarse_key="late-root",
    )

    next_two = [
        *scheduler.reserve_handoffs(4, control_revision=2),
        *scheduler.reserve_handoffs(4, control_revision=3),
    ]
    selected_names = {lease.candidate.node.name for lease in next_two}
    assert {"late-vmess", "late-root"} <= selected_names
    assert all(
        lease.candidate.selection_lane != "credit_escape" for lease in next_two
    )


def test_reward_share_is_bounded_without_a_second_capacity_lane() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.15,
        exploit_scan_share=0.25,
    )
    scheduler.set_credit_envelope(16, handoff_window=64)
    for index in range(80):
        scheduler.enqueue(
            candidate(f"productive-{index}"),
            None,
            arm_key="productive",
            source_root="root-a",
            family="ss",
            coarse_key="productive",
        )
    seed = scheduler.reserve_handoffs(1)[0]
    scheduler.commit_handoff(seed)
    scheduler.on_terminal(seed.ticket, passed=True)
    scheduler.on_commit_complete(seed.ticket, net_new=True)

    wave = scheduler.reserve_handoffs(16, control_revision=9)
    assert len(wave) == 16
    assert sum(
        lease.candidate.selection_lane == "reward" for lease in wave
    ) <= 4
    assert all(
        lease.candidate.selection_lane != "credit_escape" for lease in wave
    )


def test_reward_and_coverage_share_real_handoff_budget() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.25,
    )
    for root_index in range(5):
        root = f"root-{root_index}"
        for index in range(12):
            scheduler.enqueue(
                candidate(f"{root}-{index}"),
                None,
                arm_key=("reward" if root_index == 0 else f"arm-{index}"),
                source_root=root,
                family="vless",
                coarse_key=("reward" if root_index == 0 else f"coarse-{index}"),
            )
    scheduler.record_feedback("reward", passed=True)

    first_wave = scheduler.reserve_handoffs(16)
    scheduler.commit_handoffs(first_wave)
    for lease in first_wave:
        scheduler.on_tcp_outcome(lease.ticket, responsive=False)
    second_wave = scheduler.reserve_handoffs(16)

    combined = [*first_wave, *second_wave]
    coverage = [item for item in combined if item.ticket.coverage_selected]
    reward = [
        item
        for item in combined
        if item.ticket.selection_lane == "reward"
    ]
    assert len(coverage) <= math.ceil(len(combined) * 0.25)
    assert len(reward) <= math.ceil(len(combined) * 0.25)
    assert {item.candidate.source_root for item in combined} == {
        f"root-{index}" for index in range(5)
    }


def test_unknown_quota_debt_cannot_idle_dispatchable_reward_credit() -> None:
    """Unknown quota accounting cannot hide an active reward grant."""

    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.15,
        reward_handoff_opportunity_limit=64,
    )
    scheduler.set_credit_envelope(16)
    for index in range(100):
        scheduler.enqueue(
            candidate(f"productive-{index}"),
            None,
            arm_key="productive",
            source_root="productive-root",
            family="ss",
            coarse_key="productive",
        )

    admitted = scheduler.drain(1)[0]
    scheduler.on_terminal(admitted.dispatch_ticket, passed=True)
    scheduler.on_commit_complete(admitted.dispatch_ticket, net_new=True)

    # Saturate the ordinary root/family window while retaining global room.
    for _index in range(16):
        item = scheduler.drain(1)[0]
        scheduler.on_fast_yield(item.dispatch_ticket)

    with scheduler._lock:
        scheduler._tcp_handoff_total = 137
        scheduler._selected_total = 137
        scheduler._unknown_selected_total = 1
        scheduler._credit_escape_selected_total = 21
        scheduler._exploit_selected_total = 11
        grant = scheduler._family_rewards[("productive-root", "ss")]
        grant.eligible_handoff_opportunities = 0
        grant.exact_handoffs_remaining = 16

    supply = scheduler.supply_snapshot()
    assert supply.normal_reservable_count > 0
    # The supply snapshot reports what the current 16-handoff lane budget can
    # actually redeem, not the larger per-tick candidate cap.
    assert supply.reward_reservable_count == 4
    assert supply.escape_eligible_bucket_count == 0
    assert supply.fast_supply_state == "ready"

    selected = scheduler.drain(1, control_revision=200)
    assert len(selected) == 1
    assert selected[0].source_root == "productive-root"


def test_handoff_reservation_only_counts_after_commit_and_cancel_requeues() -> None:
    scheduler = legacy_candidate_scheduler(unknown_exploration_ratio=0.0)
    scheduler.enqueue(
        candidate("reserved"),
        None,
        arm_key="arm-a",
        source_root="root-a",
        family="vless",
        coarse_key="coarse-a",
    )

    reservations = scheduler.reserve_handoffs(1, control_revision=301)
    assert len(reservations) == 1
    assert scheduler.pending_handoff_count() == 1
    assert scheduler.quick_metrics()["reserved_handoff_count"] == 1
    assert scheduler.quick_metrics()["selected_total"] == 0
    assert scheduler.quick_metrics()["tcp_handoff_total"] == 0

    scheduler.cancel_handoff(reservations[0], requeue=True)
    assert scheduler.pending_handoff_count() == 0
    assert scheduler.quick_metrics()["reserved_handoff_count"] == 0
    assert scheduler.size() == 1

    reservation = scheduler.reserve_handoffs(1, control_revision=302)[0]
    scheduler.commit_handoff(reservation)
    scheduler.commit_handoff(reservation)
    metrics = scheduler.quick_metrics()
    assert scheduler.pending_handoff_count() == 1
    assert metrics["reserved_handoff_count"] == 0
    assert metrics["selected_total"] == 1
    assert metrics["tcp_handoff_total"] == 1
    scheduler.on_terminal(reservation.candidate.dispatch_ticket, passed=False)
    assert scheduler.pending_handoff_count() == 0


def test_mass_prestart_cancellation_requeues_every_candidate_without_false_handoff() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
    )
    candidate_count = 1024
    for index in range(candidate_count):
        scheduler.enqueue(
            candidate(f"prestart-requeue-{index}"),
            None,
            arm_key=f"arm-{index % 64}",
            source_root=f"root-{index % 17}",
            family=f"family-{index % 5}",
            coarse_key=f"coarse-{index % 128}",
        )

    cancelled = 0
    for revision in range(32):
        reservations = scheduler.reserve_handoffs(64, control_revision=400 + revision)
        assert len(reservations) == 64
        for reservation in reservations:
            assert scheduler.cancel_handoff(reservation, requeue=True)
            cancelled += 1

    metrics = scheduler.quick_metrics()
    assert cancelled == 2048
    assert scheduler.size() == candidate_count
    assert scheduler.pending_handoff_count() == 0
    assert metrics["selected_total"] == 0
    assert metrics["tcp_handoff_total"] == 0

    committed = 0
    while scheduler.size() > 0:
        reservations = scheduler.reserve_handoffs(64, control_revision=500 + committed)
        assert reservations
        for reservation in reservations:
            assert scheduler.commit_handoff(reservation)
            scheduler.on_terminal(reservation.ticket, passed=False)
            committed += 1

    metrics = scheduler.quick_metrics()
    assert committed == candidate_count
    assert metrics["selected_total"] == candidate_count
    assert metrics["tcp_handoff_total"] == candidate_count
    assert scheduler.pending_handoff_count() == 0
    assert metrics["active_ticket_count"] == 0


def test_credit_exempt_wave_still_obeys_global_limit_and_cancels_cleanly() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        work_conserving_parked_deep_limit=256,
    )
    for index in range(256):
        scheduler.enqueue(
            candidate(f"shutdown-reserved-{index}"),
            None,
            arm_key=f"arm-{index}",
            source_root=f"root-{index}",
            family="vless",
            coarse_key=f"coarse-{index}",
            credit_exempt=True,
        )

    reservations = scheduler.reserve_handoffs(256, control_revision=303)
    assert len(reservations) == 64
    assert scheduler.quick_metrics()["reserved_handoff_count"] == 64

    for reservation in reservations:
        assert scheduler.cancel_handoff(reservation, requeue=False)

    metrics = scheduler.quick_metrics()
    assert metrics["reserved_handoff_count"] == 0
    assert metrics["fast_credit_outstanding"] == 0
    assert metrics["active_ticket_count"] == 0
    assert metrics["selected_total"] == 0
    assert metrics["tcp_handoff_total"] == 0


def test_drain_all_moves_only_queued_work_without_reservation_evidence() -> None:
    scheduler = legacy_candidate_scheduler(unknown_exploration_ratio=0.0)
    for index in range(600):
        scheduler.enqueue(
            candidate(f"shutdown-backlog-{index}"),
            None,
            arm_key=f"arm-{index % 32}",
            source_root=f"root-{index % 5}",
            family="vless",
            coarse_key=f"coarse-{index % 32}",
        )
    before = scheduler.quick_metrics()

    drained = scheduler.drain_all()

    after = scheduler.quick_metrics()
    assert len(drained) == 600
    assert after["backlog_count"] == 0
    assert after["reservation_total"] == before["reservation_total"] == 0
    assert after["reserved_handoff_count"] == 0
    assert after["cancelled_reservation_total"] == 0
    assert after["selected_total"] == 0
    assert after["fast_credit_outstanding"] == 0
    assert all(item.dispatch_ticket is None for item in drained)


def test_cancelled_reservations_do_not_advance_handoff_or_feedback_evidence() -> None:
    scheduler = legacy_candidate_scheduler(unknown_exploration_ratio=0.0)
    for index in range(8):
        scheduler.enqueue(
            candidate(f"blocked-{index}"),
            None,
            arm_key=f"blocked-{index}",
            source_root="blocked-root",
            family="vless",
            coarse_key=f"blocked-{index}",
        )
    blocked_tickets = [
        item.dispatch_ticket for item in scheduler.drain(2)
    ]
    scheduler.commit_handoffs(blocked_tickets)

    for index in range(4):
        scheduler.enqueue(
            candidate(f"normal-{index}"),
            None,
            arm_key=f"normal-{index}",
            source_root="normal-root",
            family="ss",
            coarse_key=f"normal-{index}",
        )

    for revision in (401, 402):
        reservations = scheduler.reserve_handoffs(
            1,
            escape_budget=1,
            control_revision=revision,
        )
        assert reservations
        scheduler.cancel_handoff(reservations[0], requeue=True)

    cancelled = scheduler.quick_metrics()
    assert cancelled["selected_total"] == 2
    assert cancelled["credit_escape_selected_total"] == 0
    assert cancelled["cancelled_reservation_total"] == 2

    for revision in (403, 404):
        reservation = scheduler.reserve_handoffs(
            1,
            escape_budget=1,
            control_revision=revision,
        )[0]
        scheduler.commit_handoffs([reservation])
        scheduler.on_terminal(reservation.ticket, passed=False)

    committed = scheduler.quick_metrics()
    assert committed["selected_total"] == 4
    assert committed["credit_escape_selected_total"] == 0
    for ticket in blocked_tickets:
        scheduler.on_terminal(ticket, passed=False)


def test_tcp_handoff_metric_counts_only_real_handoff_once() -> None:
    scheduler = legacy_candidate_scheduler(unknown_exploration_ratio=0.0)
    scheduler.enqueue(
        candidate("handoff"),
        None,
        arm_key="arm-a",
        source_root="root-a",
        family="vless",
        coarse_key="coarse-a",
    )

    selected = scheduler.drain(1)
    ticket = selected[0].dispatch_ticket
    assert scheduler.quick_metrics()["tcp_handoff_total"] == 0

    scheduler.on_tcp_handoff(ticket)
    scheduler.on_tcp_handoff(ticket)

    assert scheduler.quick_metrics()["tcp_handoff_total"] == 1


def test_net_new_reward_is_capped_at_quarter_and_keeps_unknown_exploration() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.15,
        exploit_scan_share=0.25,
    )
    scheduler.enqueue(
        candidate("reward-seed"),
        None,
        arm_key="reward",
        source_root="root-a",
        family="vless",
        coarse_key="reward",
    )
    seed = scheduler.drain(1)[0]
    scheduler.on_terminal(seed.dispatch_ticket, passed=True)
    scheduler.on_commit_complete(seed.dispatch_ticket, net_new=True)

    for index in range(16):
        scheduler.enqueue(
            candidate(f"reward-{index}"),
            None,
            arm_key="reward",
            source_root="root-a",
            family="vless",
            coarse_key="reward",
        )
    for index in range(16):
        scheduler.enqueue(
            candidate(f"unknown-{index}"),
            None,
            arm_key=f"unknown-{index}",
            source_root=f"unknown-root-{index}",
            family="trojan",
            coarse_key=f"unknown-{index}",
        )

    first = scheduler.drain(16)
    for item in first:
        scheduler.on_tcp_outcome(item.dispatch_ticket, responsive=False)
    selected = scheduler.drain(16)
    combined = first + selected
    exploit = sum(item.selection_lane == "reward" for item in combined)
    unknown = sum(
        bool(item.dispatch_ticket and item.dispatch_ticket.unknown_exploration)
        for item in combined
    )

    assert 0 < exploit <= 8
    assert unknown >= math.ceil(32 * 0.15)


def test_exact_reward_lifetime_ignores_unrelated_handoffs() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.0,
        reward_handoff_opportunity_limit=4,
    )
    scheduler.enqueue(
        candidate("unused-reward"),
        None,
        arm_key="unused-reward",
        source_root="root-a",
        family="vless",
        coarse_key="unused-reward",
        feature_key=feature(),
    )

    seed = scheduler.drain(1)[0]
    scheduler.on_tcp_outcome(seed.dispatch_ticket, responsive=False)
    scheduler.record_feedback("unused-reward", passed=True)
    assert scheduler.quick_metrics()["reward_created_count"] == 1

    for index in range(64):
        scheduler.enqueue(
            candidate(f"handoff-{index}"),
            None,
            arm_key="other",
            source_root="root-b",
            family="trojan",
            coarse_key="other",
        )
        item = scheduler.drain(1)[0]
        scheduler.on_tcp_outcome(item.dispatch_ticket, responsive=False)

    metrics = scheduler.quick_metrics()
    # A grant without matching inventory remains alive for its origin-bucket
    # lifetime, but it cannot advertise reservable reward work.
    assert metrics["rewarded_arm_count"] == 0
    with scheduler._lock:
        assert ("root-a", "vless") in scheduler._family_rewards
    assert metrics["reward_expired_unused_count"] == 0
    assert metrics["reward_hit_count"] == 0

    for index in range(64):
        scheduler.enqueue(
            candidate(f"same-bucket-{index}"),
            None,
            arm_key=f"same-bucket-{index}",
            source_root="root-a",
            family="vless",
            coarse_key=f"same-bucket-{index}",
            feature_key=feature(address="host:example.com", host="example.com"),
        )
        item = scheduler.drain(1)[0]
        scheduler.on_tcp_outcome(item.dispatch_ticket, responsive=False)

    metrics = scheduler.quick_metrics()
    assert metrics["rewarded_arm_count"] == 0
    assert metrics["reward_expired_credit_count"] == 0
    # An exact grant is consumed only by a matching exact selection. Other
    # traffic in its origin bucket cannot erase the short exploitation chance.
    # Inventory-backed credit is intentionally zero while the exact arm is
    # absent, but the grant itself remains intact for a later exact candidate.
    assert metrics["reward_handoff_credit"] == 0
    with scheduler._lock:
        grant = scheduler._family_rewards[("root-a", "vless")]
        assert grant.exact_handoffs_remaining == 16
    assert metrics["reward_expiry_scope"] == (
        "exact_selection_credit_or_15s_wall_time"
    )


def test_exact_reward_lifetime_ignores_nonmatching_origin_bucket_handoffs() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.0,
        reward_handoff_opportunity_limit=4,
    )
    scheduler.enqueue(
        candidate("seed"),
        None,
        arm_key="seed",
        source_root="root-a",
        family="vless",
        coarse_key="seed",
        feature_key=feature(),
    )
    seed = scheduler.drain(1)[0]
    scheduler.on_terminal(seed.dispatch_ticket, passed=True)
    scheduler.on_commit_complete(seed.dispatch_ticket, net_new=True)

    for index in range(4):
        scheduler.enqueue(
            candidate(f"broad-{index}"),
            None,
            arm_key=f"broad-{index}",
            source_root="root-a",
            family="vless",
            coarse_key=f"broad-{index}",
            feature_key=feature(
                transport="grpc",
                security="reality",
                address=f"host:other-{index}.invalid",
                host=f"other-{index}.invalid",
                service_profile=f"other-profile-{index}",
            ),
        )
        item = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(item)
        scheduler.on_tcp_outcome(item.ticket, responsive=False)

    metrics = scheduler.quick_metrics()
    assert metrics["rewarded_arm_count"] == 0
    assert metrics["reward_expired_credit_count"] == 0
    assert metrics["reward_handoff_credit"] == 0
    with scheduler._lock:
        grant = scheduler._family_rewards[("root-a", "vless")]
        assert grant.exact_handoffs_remaining == 16
    assert metrics["reward_expiry_scope"] == (
        "exact_selection_credit_or_15s_wall_time"
    )


def test_exact_reward_redeems_after_unrelated_origin_handoffs() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.25,
        reward_handoff_opportunity_limit=4,
    )
    scheduler.enqueue(
        candidate("seed"),
        None,
        arm_key="exact",
        source_root="root-a",
        family="opaque-family",
        coarse_key="exact",
        feature_key=feature(),
    )
    seed = scheduler.reserve_handoffs(1)[0]
    scheduler.commit_handoff(seed)
    scheduler.on_terminal(seed.ticket, passed=True, stage="fast_probe")
    scheduler.on_commit_complete(seed.ticket, net_new=True)

    for index in range(3):
        scheduler.enqueue(
            candidate(f"ordinary-{index}"),
            None,
            arm_key=f"ordinary-{index}",
            source_root="root-a",
            family="opaque-family",
            coarse_key=f"ordinary-{index}",
            feature_key=feature(service_profile=f"other-{index}"),
        )
        ordinary = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(ordinary)
        scheduler.on_tcp_outcome(ordinary.ticket, responsive=False)

    scheduler.enqueue(
        candidate("exact-final-opportunity"),
        None,
        arm_key="exact",
        source_root="root-a",
        family="opaque-family",
        coarse_key="exact",
        feature_key=feature(),
    )
    rewarded = scheduler.reserve_handoffs(1)[0]
    assert rewarded.ticket.selection_lane == "reward"
    assert rewarded.ticket.reward_scope == "exact"
    scheduler.commit_handoff(rewarded)
    scheduler.on_tcp_outcome(rewarded.ticket, responsive=False)

    metrics = scheduler.quick_metrics()
    assert metrics["reward_hit_count"] == 1
    assert metrics["reward_exact_hit_count"] == 1
    assert metrics["reward_expired_unused_count"] == 0
    assert metrics["reward_expired_partial_count"] == 0
    assert metrics["reward_exact_resolved_count"] == 1
    assert metrics["reward_handoff_credit"] == 0
    with scheduler._lock:
        grant = scheduler._family_rewards[("root-a", "opaque-family")]
        assert grant.exact_handoffs_remaining == 15


def test_default_scheduler_redeems_exact_inventory_before_structural_work() -> None:
    scheduler = Tier3CandidateScheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.75,
    )
    exact_feature = feature(
        family="opaque-family",
        service_profiles=("l0", "l1", "l2", "l3-exact"),
    )
    scheduler.enqueue(
        candidate("exact-seed"),
        None,
        arm_key="exact-arm",
        source_root="opaque-root",
        family="opaque-family",
        coarse_key="exact-seed",
        feature_key=exact_feature,
    )
    seed = scheduler.reserve_handoffs(1)[0]
    scheduler.commit_handoff(seed)
    scheduler.on_tcp_outcome(seed.ticket, responsive=True)
    scheduler.on_terminal(
        seed.ticket,
        passed=True,
        outcome="pass",
        busy_worker_ms=200,
        stage="fast_probe",
    )
    scheduler.on_commit_complete(seed.ticket, net_new=True)

    scheduler.enqueue(
        candidate("exact-replay"),
        None,
        arm_key="exact-arm",
        source_root="opaque-root",
        family="opaque-family",
        coarse_key="exact-replay",
        feature_key=exact_feature,
    )
    selected = scheduler.reserve_handoffs(1)[0]
    assert selected.ticket.reward_scope == "exact"
    assert selected.ticket.selection_reason == "reward"
    scheduler.cancel_handoff(selected, requeue=False)
    assert scheduler.release_outstanding() == 0


def test_exact_reward_uses_default_sixteen_opportunities_and_fifteen_seconds() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.0,
    )
    assert scheduler.reward_handoff_opportunity_limit == 16
    scheduler.enqueue(
        candidate("wall-expiry-seed"),
        None,
        arm_key="wall-expiry-seed",
        source_root="opaque-root",
        family="opaque-family",
        coarse_key="wall-expiry-seed",
        feature_key=feature(),
    )
    seed = scheduler.reserve_handoffs(1)[0]
    scheduler.commit_handoff(seed)
    scheduler.on_terminal(seed.ticket, passed=True, stage="fast_probe")
    scheduler.on_commit_complete(seed.ticket, net_new=True)

    with scheduler._lock:
        grant = scheduler._family_rewards[("opaque-root", "opaque-family")]
        grant.created_monotonic_ns -= 16_000_000_000

    metrics = scheduler.quick_metrics()
    assert metrics["reward_handoff_credit"] == 0
    assert metrics["reward_expired_wall_time_count"] == 0
    assert grant.exact_handoffs_remaining == 16
    assert grant.dormant

    scheduler.enqueue(
        candidate("wall-expiry-inventory"),
        None,
        arm_key="wall-expiry-seed",
        source_root="opaque-root",
        family="opaque-family",
        coarse_key="wall-expiry-seed",
        feature_key=feature(),
    )
    with scheduler._lock:
        scheduler._refresh_reward_grant_lifetime_unlocked(grant)
        assert grant.inventory_activated_monotonic_ns > 0
        grant.inventory_activated_monotonic_ns -= 16_000_000_000

    metrics = scheduler.quick_metrics()
    assert metrics["reward_handoff_credit"] == 0
    assert metrics["reward_expired_wall_time_count"] == 1
    assert metrics["reward_expired_unused_count"] == 1
    assert metrics["reward_grant_expiry_reason_counts"] == {
        "active_inventory_wall_time_limit": 1
    }


def test_network_similar_remains_shadow_even_after_positive_evidence() -> None:
    scheduler = legacy_candidate_scheduler(unknown_exploration_ratio=0.0)
    scheduler.enqueue(
        candidate("seed"),
        None,
        arm_key="exact",
        source_root="root-a",
        family="vless",
        coarse_key="exact",
        feature_key=feature(),
    )
    seed = scheduler.drain(1)[0]
    scheduler.on_terminal(seed.dispatch_ticket, passed=True)
    scheduler.on_commit_complete(seed.dispatch_ticket, net_new=True)
    promote_cohort(
        scheduler,
        scope="similar",
        cohort_key=f"similar:{scheduler._feature_signature(feature())}",
    )

    scheduler.enqueue(
        candidate("family-fallback"),
        None,
        arm_key="sibling",
        source_root="renamed-root-b",
        family="vless",
        coarse_key="sibling",
        feature_key=feature(address="host:example.com", host="example.com"),
    )

    selected = scheduler.drain(1)[0]
    assert selected.node.name == "family-fallback"
    assert selected.selection_lane != "reward"
    scheduler.on_tcp_outcome(selected.dispatch_ticket, responsive=False)
    metrics = scheduler.quick_metrics()
    assert metrics["reward_exact_hit_count"] == 0
    assert metrics["reward_similar_hit_count"] == 0
    assert metrics["reward_family_fallback_hit_count"] == 0
    assert metrics["exploit_scan_share_limit"] == 0.25


def test_default_network_similar_stays_shadow_after_sparse_promotion() -> None:
    scheduler = Tier3CandidateScheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.25,
    )
    target = feature(
        family="opaque-family",
        transport="opaque-transport",
        security="opaque-security",
        port="opaque-port",
        address="opaque-address-a",
        host="opaque-host-a",
        service_profiles=("unrelated-service-leaf",),
    )

    for index in range(2):
        scheduler.enqueue(
            candidate(f"network-sparse-train-{index}", protocol="opaque"),
            None,
            arm_key=f"network-sparse-train-{index}",
            source_root=f"opaque-root-{index}",
            family="opaque-family",
            coarse_key=f"network-sparse-train-{index}",
            feature_key=Tier3CandidateFeatureKey.build(
                service_family=target.service_family,
                transport=target.transport,
                security=target.security,
                port_bucket=target.port_bucket,
                address_bucket=target.address_bucket,
                host_suffix=target.host_suffix,
                service_profiles=(f"training-leaf-{index}",),
            ),
        )
        reservation = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
        scheduler.on_fast_observation(
            reservation.ticket,
            outcome="pass",
            busy_worker_ms=500,
            passed=True,
        )
        scheduler.on_terminal(
            reservation.ticket,
            passed=True,
            outcome="pass",
            busy_worker_ms=500,
            stage="fast_probe",
        )
        scheduler.on_commit_complete(reservation.ticket, net_new=False)

    similar_key = f"similar:{scheduler._feature_signature(target)}"
    with scheduler._lock:
        state = scheduler._cohort_yield_states[similar_key]
        assert state.lifecycle_state == "similar_probation"
        assert state.promoted
        assert not state.advantage_confirmed

    for index in range(8):
        scheduler.enqueue(
            candidate(f"network-sparse-match-{index}", protocol="opaque"),
            None,
            arm_key=f"network-sparse-match-{index}",
            source_root=f"match-root-{index % 2}",
            family="opaque-family",
            coarse_key=f"network-sparse-match-{index}",
            feature_key=Tier3CandidateFeatureKey.build(
                service_family=target.service_family,
                transport=target.transport,
                security=target.security,
                port_bucket=target.port_bucket,
                address_bucket=target.address_bucket,
                host_suffix=target.host_suffix,
                service_profiles=(f"match-leaf-{index}",),
            ),
        )
        scheduler.enqueue(
            candidate(f"network-sparse-sibling-{index}", protocol="opaque"),
            None,
            arm_key=f"network-sparse-sibling-{index}",
            source_root=f"sibling-root-{index % 2}",
            family="opaque-family",
            coarse_key=f"network-sparse-sibling-{index}",
            feature_key=feature(
                family="opaque-family",
                transport="opaque-transport",
                security="opaque-security",
                port="opaque-port",
                address="opaque-address-b",
                host="opaque-host-b",
                service_profiles=(f"sibling-leaf-{index}",),
            ),
        )

    first_wave = scheduler.reserve_handoffs(16)
    assert all(item.ticket.reward_scope != "similar" for item in first_wave)
    metrics = scheduler.quick_metrics()
    assert metrics["reward_similar_selected_count"] == 0
    assert metrics["production_structural_routing_mode"] == (
        "exact_l3_with_direct_child_discovery_and_similar_shadow"
    )
    for item in first_wave:
        scheduler.cancel_handoff(item, requeue=False)
    assert scheduler.release_outstanding() == 0


def test_network_cohort_requires_multistrata_training_and_independent_holdout() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.25,
    )
    target_feature = feature(
        family="opaque-family",
        transport="opaque-transport",
        security="opaque-security",
        port="opaque-port",
        address="opaque-address",
        host="opaque-host",
    )

    def observe(
        index: int,
        *,
        target: bool,
        passed: bool,
    ) -> None:
        item_feature = (
            Tier3CandidateFeatureKey.build(
                service_family=target_feature.service_family,
                transport=target_feature.transport,
                security=target_feature.security,
                port_bucket=target_feature.port_bucket,
                address_bucket=target_feature.address_bucket,
                host_suffix=target_feature.host_suffix,
                service_profiles=(f"unique-{index}",),
            )
            if target
            else feature(
                family="ordinary-family",
                transport=f"ordinary-transport-{index}",
                address=f"ordinary-address-{index}",
                host=f"ordinary-host-{index}",
                service_profiles=(f"ordinary-{index}",),
            )
        )
        scheduler.enqueue(
            candidate(f"network-train-{index}", protocol="opaque"),
            None,
            arm_key=f"network-train-{index}",
            source_root=f"opaque-root-{index % 2}",
            family="opaque-family",
            coarse_key=f"network-train-{index}",
            feature_key=item_feature,
        )
        reservation = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
        scheduler.on_fast_observation(
            reservation.ticket,
            outcome="pass" if passed else "candidate_fail",
            busy_worker_ms=500,
            passed=passed,
        )
        scheduler.on_terminal(
            reservation.ticket,
            passed=passed,
            outcome="pass" if passed else "candidate_fail",
            busy_worker_ms=500,
            stage="fast_probe",
        )
        if passed:
            scheduler.on_commit_complete(reservation.ticket, net_new=False)

    for index in range(16):
        observe(index, target=False, passed=False)
    for index in range(9):
        observe(100 + index, target=True, passed=index < 3)

    similar_key = f"similar:{scheduler._feature_signature(target_feature)}"
    with scheduler._lock:
        state = scheduler._cohort_yield_states[similar_key]
        assert state.lifecycle_state == "similar_probation"
        assert state.promoted
        assert len(state.current_pass_root_families) == 2

    for index in range(12):
        scheduler.enqueue(
            candidate(f"network-holdout-{index}", protocol="opaque"),
            None,
            arm_key=f"network-holdout-{index}",
            source_root=f"opaque-root-{index % 2}",
            family="opaque-family",
            coarse_key=f"network-holdout-{index}",
            feature_key=Tier3CandidateFeatureKey.build(
                service_family=target_feature.service_family,
                transport=target_feature.transport,
                security=target_feature.security,
                port_bucket=target_feature.port_bucket,
                address_bucket=target_feature.address_bucket,
                host_suffix=target_feature.host_suffix,
                service_profiles=(f"holdout-{index}",),
            ),
        )

    with scheduler._lock:
        assert scheduler._cohort_queued_inventory[similar_key] == 12
        assert scheduler._production_similar_cohorts_with_inventory_unlocked()
        assert scheduler._candidate_reward_grant_and_scope(
            next(
                arm.items[0]
                for arm in scheduler._arms.values()
                if arm.items
            )
        )[1] == "similar"

    first_wave = scheduler.reserve_handoffs(16)
    probation = [
        item
        for item in first_wave
        if item.ticket.reward_scope == "similar"
    ]
    assert len(probation) == 2, scheduler.quick_metrics()
    for item in probation:
        scheduler.commit_handoff(item)
        scheduler.on_tcp_outcome(item.ticket, responsive=True)
    assert scheduler.quick_metrics()["reward_similar_outstanding_count"] == 2
    for item in reversed(probation):
        scheduler.on_fast_observation(
            item.ticket,
            outcome="pass",
            busy_worker_ms=500,
            passed=True,
        )
        scheduler.on_terminal(
            item.ticket,
            passed=True,
            outcome="pass",
            busy_worker_ms=500,
            stage="fast_probe",
        )
        scheduler.on_commit_complete(item.ticket, net_new=True)
    for item in first_wave:
        if item not in probation:
            scheduler.cancel_handoff(item, requeue=True)

    with scheduler._lock:
        state = scheduler._cohort_yield_states[similar_key]
        assert state.lifecycle_state == "similar_proven"
    second_wave = scheduler.reserve_handoffs(16)
    assert sum(
        item.ticket.reward_scope == "similar" for item in second_wave
    ) <= 4
    for item in second_wave:
        scheduler.cancel_handoff(item, requeue=False)
    metrics = scheduler.quick_metrics()
    assert metrics["reward_similar_selected_count"] >= 2
    assert metrics["reward_similar_resolved_count"] == 2
    assert metrics["reward_similar_outstanding_count"] == 0
    assert metrics["reward_similar_outstanding_high_water"] == 2
    assert metrics["reward_similar_pass_count"] == 2
    assert metrics["reward_similar_net_new_count"] == 2
    assert metrics["reward_family_fallback_hit_count"] == 0
    assert scheduler.release_outstanding() == 0


def test_service_reward_pass_still_trains_independent_similar_cohort() -> None:
    scheduler = Tier3CandidateScheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.25,
    )
    target = feature(
        family="opaque-family",
        transport="opaque-transport",
        security="opaque-security",
        port="opaque-port",
        address="opaque-address-a",
        host="opaque-host-a",
        service_profiles=("ordinary-leaf",),
    )
    scheduler.enqueue(
        candidate("ordinary-similar-seed", protocol="opaque"),
        None,
        arm_key="ordinary-similar-seed",
        source_root="opaque-root",
        family="opaque-family",
        coarse_key="ordinary-similar-seed",
        feature_key=target,
    )
    first = scheduler.reserve_handoffs(1)[0]
    scheduler.commit_handoff(first)
    scheduler.on_tcp_outcome(first.ticket, responsive=True)
    scheduler.on_fast_observation(
        first.ticket,
        outcome="pass",
        busy_worker_ms=500,
        passed=True,
    )
    scheduler.on_terminal(
        first.ticket,
        passed=True,
        outcome="pass",
        busy_worker_ms=500,
        stage="fast_probe",
    )
    scheduler.on_commit_complete(first.ticket, net_new=True)

    promote_cohort(
        scheduler,
        scope="service",
        cohort_key="service:reward-leaf",
    )
    scheduler.enqueue(
        candidate("service-reward-similar-confirmation", protocol="opaque"),
        None,
        arm_key="service-reward-similar-confirmation",
        source_root="opaque-root",
        family="opaque-family",
        coarse_key="service-reward-similar-confirmation",
        feature_key=Tier3CandidateFeatureKey.build(
            service_family=target.service_family,
            transport=target.transport,
            security=target.security,
            port_bucket=target.port_bucket,
            address_bucket=target.address_bucket,
            host_suffix=target.host_suffix,
            service_profiles=("reward-leaf",),
        ),
    )
    second = scheduler.reserve_handoffs(1)[0]
    assert second.ticket.selection_reason == "reward"
    assert second.ticket.reward_scope == "service"
    scheduler.commit_handoff(second)
    scheduler.on_tcp_outcome(second.ticket, responsive=True)
    scheduler.on_fast_observation(
        second.ticket,
        outcome="pass",
        busy_worker_ms=500,
        passed=True,
    )

    similar_key = f"similar:{scheduler._feature_signature(target)}"
    with scheduler._lock:
        state = scheduler._cohort_yield_states[similar_key]
        assert state.completed_total == 2
        assert state.pass_total == 2
        assert state.lifecycle_state == "similar_probation"
        assert state.promoted

    scheduler.on_terminal(
        second.ticket,
        passed=True,
        outcome="pass",
        busy_worker_ms=500,
        stage="fast_probe",
    )
    scheduler.on_commit_complete(second.ticket, net_new=True)
    assert scheduler.release_outstanding() == 0


def test_service_profile_reward_crosses_source_without_using_source_identity() -> None:
    scheduler = legacy_candidate_scheduler(unknown_exploration_ratio=0.0)
    scheduler.enqueue(
        candidate("seed"),
        None,
        arm_key="seed",
        source_root="random-root-a",
        family="vless",
        coarse_key="seed",
        feature_key=feature(service_profile="session-hmac-a"),
    )
    seed = scheduler.drain(1)[0]
    scheduler.on_terminal(seed.dispatch_ticket, passed=True)
    scheduler.on_commit_complete(seed.dispatch_ticket, net_new=True)
    promote_cohort(
        scheduler,
        scope="service",
        cohort_key="service:session-hmac-a",
    )
    scheduler.enqueue(
        candidate("new-root-coverage"),
        None,
        arm_key="new-root-coverage",
        source_root="renamed-root-b",
        family="vless",
        coarse_key="new-root-coverage",
        feature_key=feature(service_profile="unrelated-profile"),
    )
    coverage = scheduler.drain(1)[0]
    scheduler.on_tcp_outcome(coverage.dispatch_ticket, responsive=False)
    scheduler.enqueue(
        candidate("service-peer"),
        None,
        arm_key="different-arm",
        source_root="renamed-root-b",
        family="vless",
        coarse_key="different-arm",
        feature_key=feature(
            address="host:unrelated.example",
            host="unrelated.example",
            service_profile="session-hmac-a",
        ),
    )

    selected = scheduler.drain(1)[0]
    assert selected.selection_lane == "reward"
    assert selected.dispatch_ticket is not None
    assert selected.dispatch_ticket.reward_scope == "service"
    scheduler.on_tcp_outcome(selected.dispatch_ticket, responsive=True)
    with scheduler._lock:
        grant = next(iter(scheduler._family_rewards.values()))
        assert grant.exact_handoffs_remaining == 16
        assert grant.service_handoffs_remaining == 0
    scheduler.on_terminal(
        selected.dispatch_ticket,
        passed=True,
        outcome="pass",
        busy_worker_ms=1000,
        stage="fast_probe",
    )
    metrics = scheduler.quick_metrics()
    assert metrics["reward_service_hit_count"] == 1
    assert metrics["reward_service_fast_pass_count"] == 1


def test_net_new_exact_grant_does_not_make_service_profile_production_ready() -> None:
    scheduler = legacy_candidate_scheduler(unknown_exploration_ratio=0.0)
    scheduler.enqueue(
        candidate("seed-only"),
        None,
        arm_key="seed-only",
        source_root="opaque-a",
        family="vless",
        coarse_key="seed-only",
        feature_key=feature(service_profile="shared-structure"),
    )
    seed = scheduler.reserve_handoffs(1)[0]
    scheduler.commit_handoff(seed)
    scheduler.on_tcp_outcome(seed.ticket, responsive=True)
    scheduler.on_terminal(
        seed.ticket,
        passed=True,
        outcome="pass",
        busy_worker_ms=500,
        stage="fast_probe",
    )
    scheduler.on_commit_complete(seed.ticket, net_new=True)

    scheduler.enqueue(
        candidate("peer-before-evidence"),
        None,
        arm_key="different-arm",
        source_root="opaque-b",
        family="vless",
        coarse_key="different-arm",
        feature_key=feature(service_profile="shared-structure"),
    )
    peer = scheduler.reserve_handoffs(1)[0]

    assert peer.ticket.reward_scope == "none"
    assert peer.candidate.selection_lane != "reward"
    with scheduler._lock:
        grant = scheduler._family_rewards[("opaque-a", "vless")]
        assert grant.exact_handoffs_remaining == 16
        assert grant.service_handoffs_remaining == 0


def test_dirty_service_cohort_rechecks_when_ordinary_reference_arrives_later() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.25,
    )

    def complete(name: str, *, profile: str, passed: bool) -> None:
        scheduler.enqueue(
            candidate(name),
            None,
            arm_key=name,
            source_root=f"root-{name}",
            family="opaque-protocol",
            coarse_key=name,
            feature_key=feature(
                family="opaque-protocol",
                service_profile=profile,
            ),
        )
        reservation = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
        scheduler.on_terminal(
            reservation.ticket,
            passed=passed,
            outcome="pass" if passed else "candidate_fail",
            busy_worker_ms=500,
            stage="fast_probe",
        )
        scheduler.on_commit_complete(reservation.ticket, net_new=passed)

    for index in range(8):
        complete(
            f"service-first-{index}",
            profile="late-reference",
            passed=True,
        )
    with scheduler._lock:
        state = scheduler._cohort_yield_states["service:late-reference"]
        assert state.probation is False
        assert state.promoted is True
        assert state.lifecycle_state == "proven_50"
        assert state.production_completion_total >= 2
        assert state.production_pass_total >= 2

    for index in range(16):
        complete(f"ordinary-later-{index}", profile="none", passed=False)

    with scheduler._lock:
        state = scheduler._cohort_yield_states["service:late-reference"]
        assert state.promoted is True
        assert state.probation is False
        assert state.exploit_share >= 0.50
        assert state.lifecycle_state in {"proven_50", "proven_75"}
        assert state.production_completion_total >= 2


def test_family_normalization_preserves_service_profile_observation_key() -> None:
    scheduler = legacy_candidate_scheduler(unknown_exploration_ratio=0.0)
    scheduler.enqueue(
        candidate("seed", protocol="vless"),
        None,
        arm_key="seed",
        source_root="root-a",
        family="vless",
        coarse_key="seed",
        feature_key=feature(service_profile="session-profile"),
    )
    seed = scheduler.drain(1)[0]
    scheduler.on_terminal(seed.dispatch_ticket, passed=True)
    scheduler.on_commit_complete(seed.dispatch_ticket, net_new=True)

    scheduler.enqueue(
        candidate("normalized", protocol="vless"),
        None,
        arm_key="normalized",
        source_root="root-b",
        family="VLESS ",
        coarse_key="normalized",
        feature_key=feature(
            family="other",
            service_profile="session-profile",
        ),
    )
    selected = scheduler.drain(1)[0]

    assert selected.dispatch_ticket is not None
    assert selected.dispatch_ticket.feature_key.service_family == "vless"
    assert selected.dispatch_ticket.feature_key.service_profile == "session-profile"
    assert selected.dispatch_ticket.service_cohort_key == "service:session-profile"
    assert selected.dispatch_ticket.reward_scope == "none"


def test_adaptive_exploit_requires_two_evidence_windows_and_keeps_floor() -> None:
    scheduler = legacy_candidate_scheduler(exploit_scan_share=0.25)
    promote_cohort(scheduler, scope="exact", cohort_key="exact:winning")
    metrics = scheduler.quick_metrics()
    assert 0.25 < metrics["exploit_scan_share_limit"] <= 0.75
    assert metrics["exploit_scan_share_baseline"] == 0.25


def test_adaptive_exploit_does_not_pool_losing_service_with_winning_exact() -> None:
    scheduler = legacy_candidate_scheduler(exploit_scan_share=0.25)
    with scheduler._lock:
        for index in range(32):
            scheduler._record_fast_yield_sample_unlocked(
                passed=index % 2 == 0,
                censored=False,
                busy_worker_ms=1000,
                scope="exact",
                cohort_key="exact:winning",
            )
            scheduler._record_fast_yield_sample_unlocked(
                passed=False,
                censored=False,
                busy_worker_ms=1000,
                scope="service",
                cohort_key="service:losing",
            )
            scheduler._record_fast_yield_sample_unlocked(
                passed=index % 16 == 0,
                censored=False,
                busy_worker_ms=1000,
                scope="ordinary",
                cohort_key="ordinary",
            )

    metrics = scheduler.quick_metrics()
    assert metrics["reward_exact_promoted_cohort_count"] == 1
    assert metrics["reward_service_promoted_cohort_count"] == 0
    assert metrics["reward_service_zero_pass_demoted_count"] >= 1


def test_reward_rejects_broad_root_family_fallback_and_keeps_shadow_count() -> None:
    scheduler = legacy_candidate_scheduler(unknown_exploration_ratio=0.0)
    scheduler.enqueue(
        candidate("seed"),
        None,
        arm_key="exact",
        source_root="root-a",
        family="vless",
        coarse_key="exact",
        feature_key=feature(),
    )
    seed = scheduler.drain(1)[0]
    scheduler.on_terminal(seed.dispatch_ticket, passed=True)
    scheduler.on_commit_complete(seed.dispatch_ticket, net_new=True)

    scheduler.enqueue(
        candidate("broad"),
        None,
        arm_key="broad",
        source_root="root-a",
        family="vless",
        coarse_key="broad",
        feature_key=feature(
            transport="grpc",
            address="host:other.example",
            host="other.example",
        ),
    )
    selected = scheduler.reserve_handoffs(1)[0]
    assert selected.candidate.selection_lane != "reward"
    scheduler.commit_handoff(selected)
    scheduler.on_tcp_outcome(selected.ticket, responsive=True)
    scheduler.on_terminal(selected.ticket, outcome="candidate_fail")

    metrics = scheduler.quick_metrics()
    assert metrics["reward_family_fallback_hit_count"] == 0
    assert metrics["reward_shadow_broad_handoff_count"] == 1


def test_similar_reward_requires_two_useful_feature_matches() -> None:
    base = feature()
    assert base.is_similar_to(
        feature(address="host:example.com", host="example.com")
    )
    assert not base.is_similar_to(
        feature(address="unknown", host="none")
    )
    assert not base.is_similar_to(feature(transport="grpc"))
    assert not base.is_similar_to(feature(family="vmess"))


def test_similar_reward_never_enters_production_handoffs() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.25,
    )
    for index in range(32):
        scheduler.enqueue(
            candidate(f"quota-history-{index}"),
            None,
            arm_key=f"quota-history-{index}",
            source_root="history-root",
            family="trojan",
            coarse_key=f"quota-history-{index}",
        )
    history = scheduler.reserve_handoffs(32)
    scheduler.commit_handoffs(history)
    for reservation in history:
        scheduler.on_tcp_outcome(reservation.ticket, responsive=False)

    scheduler.enqueue(
        candidate("similar-seed"),
        None,
        arm_key="similar-seed",
        source_root="reward-root",
        family="vless",
        coarse_key="similar-seed",
        feature_key=feature(),
    )
    seed = scheduler.reserve_handoffs(1)[0]
    scheduler.commit_handoff(seed)
    scheduler.on_terminal(seed.ticket, passed=True)
    scheduler.on_commit_complete(seed.ticket, net_new=True)
    promote_cohort(
        scheduler,
        scope="similar",
        cohort_key=f"similar:{scheduler._feature_signature(feature())}",
    )

    for index in range(16):
        scheduler.enqueue(
            candidate(f"similar-{index}"),
            None,
            arm_key=f"similar-{index}",
            source_root="reward-root",
            family="vless",
            coarse_key=f"similar-{index}",
            feature_key=feature(
                address="host:example.com",
                host="example.com",
            ),
        )

    first_wave = scheduler.reserve_handoffs(16)
    scheduler.commit_handoffs(first_wave)
    assert sum(
        item.candidate.selection_lane == "reward" for item in first_wave
    ) == 0
    for item in first_wave:
        scheduler.on_tcp_outcome(item.ticket, responsive=False)

    metrics = scheduler.quick_metrics()
    assert metrics["reward_similar_hit_count"] == 0
    assert metrics["reward_family_fallback_hit_count"] == 0


def test_later_same_bucket_commit_does_not_overwrite_exact_reward() -> None:
    scheduler = legacy_candidate_scheduler(unknown_exploration_ratio=0.0)
    exact_keys: list[str] = []
    for arm_name in ("first-arm", "second-arm"):
        scheduler.enqueue(
            candidate(f"{arm_name}-seed"),
            None,
            arm_key=arm_name,
            source_root="opaque-root",
            family="vless",
            coarse_key=arm_name,
            feature_key=feature(service_profile=f"profile-{arm_name}"),
        )
        seed = scheduler.reserve_handoffs(1)[0]
        exact_keys.append(seed.ticket.exact_cohort_key.removeprefix("exact:"))
        scheduler.commit_handoff(seed)
        scheduler.on_terminal(seed.ticket, passed=True, stage="fast_probe")
        scheduler.on_commit_complete(seed.ticket, net_new=True)

    with scheduler._lock:
        assert len(scheduler._family_reward_grants[("opaque-root", "vless")]) == 2
        assert len(scheduler._exact_rewards[exact_keys[0]]) == 1
        assert len(scheduler._exact_rewards[exact_keys[1]]) == 1


def test_global_reward_uses_post_coverage_capacity_without_starving_roots() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.25,
    )
    scheduler.enqueue(
        candidate("seed"),
        None,
        arm_key="seed-arm",
        source_root="reward-root",
        family="vless",
        coarse_key="seed-arm",
        feature_key=feature(service_profile="profile"),
    )
    seed = scheduler.reserve_handoffs(1)[0]
    scheduler.commit_handoff(seed)
    scheduler.on_terminal(seed.ticket, passed=True, stage="fast_probe")
    scheduler.on_commit_complete(seed.ticket, net_new=True)

    for index in range(32):
        scheduler.enqueue(
            candidate(f"reward-{index}"),
            None,
            arm_key="seed-arm",
            source_root="reward-root",
            family="vless",
            coarse_key="seed-arm",
            feature_key=feature(service_profile="profile"),
        )
    for root_index in range(8):
        for index in range(8):
            scheduler.enqueue(
                candidate(f"ordinary-{root_index}-{index}"),
                None,
                arm_key=f"ordinary-{root_index}-{index}",
                source_root=f"ordinary-root-{root_index}",
                family="trojan",
                coarse_key=f"ordinary-{root_index}-{index}",
            )

    wave = scheduler.reserve_handoffs(16)
    roots = {item.candidate.source_root for item in wave}
    reward_count = sum(
        item.candidate.selection_lane == "reward" for item in wave
    )
    for item in wave:
        scheduler.commit_handoff(item)
        scheduler.on_tcp_outcome(item.ticket, responsive=False)
    second_wave = scheduler.reserve_handoffs(16)
    roots.update(item.candidate.source_root for item in second_wave)

    assert all(f"ordinary-root-{index}" in roots for index in range(8))
    assert "reward-root" in roots
    assert 3 <= reward_count <= 4
    for item in second_wave:
        scheduler.cancel_handoff(item, requeue=False)
    assert scheduler.release_outstanding() == 0


def test_real_fast_feedback_promotes_service_without_manual_state_mutation() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.25,
    )

    def complete_fast(name: str, *, profile: str, passed: bool) -> None:
        scheduler.enqueue(
            candidate(name),
            None,
            arm_key=name,
            source_root=f"root-{name}",
            family="vless",
            coarse_key=name,
            feature_key=feature(service_profile=profile),
        )
        reservation = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
        scheduler.on_terminal(
            reservation.ticket,
            passed=passed,
            outcome="pass" if passed else "candidate_fail",
            busy_worker_ms=500,
            stage="fast_probe",
        )
        scheduler.on_commit_complete(reservation.ticket, net_new=passed)

    for index in range(32):
        complete_fast(
            f"ordinary-{index}",
            profile="none",
            passed=False,
        )
    for index in range(16):
        complete_fast(
            f"service-{index}",
            profile="structural-profile",
            passed=True,
        )

    metrics = scheduler.quick_metrics()
    assert metrics["reward_service_promoted_cohort_count"] == 1
    assert metrics["reward_service_sample_completion_count"] >= 16
    assert metrics["reward_service_sample_pass_count"] >= 16
    top = metrics["reward_service_top_cohorts"]
    assert top[0]["cohort_id"] == "structural-profile"
    assert (
        top[0]["window_completion_count"]
        + top[0]["production_completion_total"]
        == 16
    )
    assert top[0]["window_pass_count"] + top[0]["production_pass_total"] == 16
    assert top[0]["promoted"] is True


def test_censored_exposure_does_not_consume_service_evaluation_watermark() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.25,
    )

    def complete_fast(
        name: str,
        *,
        profile: str,
        passed: bool = False,
        censored: bool = False,
    ) -> None:
        scheduler.enqueue(
            candidate(name),
            None,
            arm_key=name,
            source_root=f"root-{name}",
            family="vless",
            coarse_key=name,
            feature_key=feature(service_profile=profile),
        )
        reservation = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
        scheduler.on_terminal(
            reservation.ticket,
            passed=passed,
            outcome=(
                "pass"
                if passed
                else "right_censored_timeout"
                if censored
                else "candidate_fail"
            ),
            busy_worker_ms=500,
            stage="fast_probe",
        )
        scheduler.on_commit_complete(reservation.ticket, net_new=passed)

    for index in range(32):
        complete_fast(f"ordinary-{index}", profile="none")
    for index in range(8):
        complete_fast(
            f"censored-{index}",
            profile="censored-then-strong",
            censored=True,
        )
    for index in range(8):
        complete_fast(
            f"strong-{index}",
            profile="censored-then-strong",
            passed=True,
        )

    top = scheduler.quick_metrics()["reward_service_top_cohorts"][0]
    assert top["window_sample_count"] + top["production_selection_total"] == 16
    assert (
        top["window_completion_count"] + top["production_completion_total"]
        == 8
    )
    assert top["window_pass_count"] + top["production_pass_total"] == 8
    assert top["production_completion_total"] >= 2


def test_service_promotion_uses_consecutive_rolling_evidence_windows() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.25,
    )

    def complete_fast(name: str, *, profile: str, passed: bool) -> None:
        scheduler.enqueue(
            candidate(name),
            None,
            arm_key=name,
            source_root=f"root-{name}",
            family="vless",
            coarse_key=name,
            feature_key=feature(service_profile=profile),
        )
        reservation = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
        scheduler.on_terminal(
            reservation.ticket,
            passed=passed,
            outcome="pass" if passed else "candidate_fail",
            busy_worker_ms=500,
            stage="fast_probe",
        )
        scheduler.on_commit_complete(reservation.ticket, net_new=passed)

    for index in range(32):
        complete_fast(f"ordinary-rolling-{index}", profile="none", passed=False)
    for index in range(64):
        complete_fast(
            f"service-rolling-{index}",
            profile="rolling-winner",
            passed=True,
        )

    metrics = scheduler.quick_metrics()
    top = metrics["reward_service_top_cohorts"][0]
    assert (
        top["window_completion_count"]
        + top["production_completion_total"]
        == 64
    )
    assert top["promoted"] is True
    assert top["lifecycle_state"] == "proven_75"
    assert top["probation_pass_total"] >= 2
    assert top["proven_pass_total"] >= 8
    level = metrics["reward_service_level_metrics"]["0"]
    assert level["probation_pass_count"] >= 2
    assert level["proven_pass_count"] >= 8
    assert metrics["reward_service_promoted_cohort_count"] == 1


def test_fast_feedback_updates_all_service_profile_ancestors() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.25,
    )

    def complete(name: str, profiles: tuple[str, ...], passed: bool) -> None:
        scheduler.enqueue(
            candidate(name),
            None,
            arm_key=name,
            source_root=f"root-{name}",
            family="opaque-protocol",
            coarse_key=name,
            feature_key=feature(
                family="opaque-protocol",
                service_profiles=profiles,
            ),
        )
        reservation = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
        scheduler.on_terminal(
            reservation.ticket,
            passed=passed,
            outcome="pass" if passed else "candidate_fail",
            busy_worker_ms=500,
            stage="fast_probe",
        )
        scheduler.on_commit_complete(reservation.ticket, net_new=passed)

    for index in range(32):
        complete(f"ordinary-hierarchy-{index}", (), False)
    levels = ("l0", "l1", "l2", "l3")
    for index in range(16):
        complete(f"hierarchy-{index}", levels, True)

    with scheduler._lock:
        states = [
            scheduler._cohort_yield_states[f"service:{profile}"]
            for profile in levels
        ]
    assert len({state.completed_total for state in states}) == 1
    assert states[-1].completed_total + states[-1].production_completion_total == 16
    assert [state.hierarchy_level for state in states] == [0, 1, 2, 3]
    assert all(state.promoted is False for state in states[:-1])
    assert states[-1].promoted is True
    assert all(
        state.last_decision_reason == "ancestor_discovery_evidence_pending"
        for state in states[:-1]
    )


def test_pool_prior_only_opens_bounded_discovery_after_current_pass() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.15,
        exploit_scan_share=0.25,
    )
    scheduler.seed_service_pool_prior(
        [
            feature(service_profiles=("l0-prior", "l1-prior-a")),
            feature(service_profiles=("l0-prior", "l1-prior-b")),
        ]
    )

    assert "service:l0-prior" not in scheduler._cohort_yield_states

    scheduler.enqueue(
        candidate("current-pass"),
        None,
        arm_key="current-pass",
        source_root="random-root-current",
        family="opaque-protocol",
        coarse_key="current-pass",
        feature_key=feature(
            family="opaque-protocol",
            service_profiles=("l0-prior", "l1-current", "l2-current"),
        ),
    )
    reservation = scheduler.reserve_handoffs(1)[0]
    scheduler.commit_handoff(reservation)
    scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
    scheduler.on_terminal(
        reservation.ticket,
        passed=True,
        outcome="pass",
        busy_worker_ms=500,
        stage="fast_probe",
    )
    scheduler.on_commit_complete(reservation.ticket, net_new=False)

    with scheduler._lock:
        state = scheduler._cohort_yield_states["service:l0-prior"]
        assert state.sample_total == 1
        assert state.completed_total == 1
        assert state.pass_total == 1
        assert state.discovery is True
        assert state.probation is False
        assert state.promoted is False

    for index in range(12):
        scheduler.enqueue(
            candidate(f"discovery-ready-{index}"),
            None,
            arm_key=f"discovery-ready-{index}",
                source_root="random-root-ready",
            family="opaque-protocol",
            coarse_key=f"discovery-ready-{index}",
            feature_key=feature(
                family="opaque-protocol",
                service_profiles=(
                    "l0-prior",
                    f"unseen-child-{index}",
                ),
            ),
        )
    reservations = scheduler.reserve_handoffs(16)
    discovery = [
        item
        for item in reservations
        if item.ticket.reward_cohort_state == "parent_discovery"
    ]
    assert len(discovery) == 1
    assert all(item.ticket.reward_cohort_key == "service:l0-prior" for item in discovery)
    assert all(item.ticket.selection_lane != "reward" for item in discovery)
    assert all(item.ticket.unknown_exploration for item in discovery)


def test_parent_discovery_targets_an_unseen_direct_child_behind_coarse_prefix() -> None:
    scheduler = legacy_candidate_scheduler(
        max_active_arms_per_family=1,
        unknown_exploration_ratio=0.15,
        exploit_scan_share=0.25,
    )
    scheduler.seed_service_pool_prior(
        [
            feature(service_profiles=("parent", "prior-a")),
            feature(service_profiles=("parent", "prior-b")),
        ]
    )

    scheduler.enqueue(
        candidate("parent-pass"),
        None,
        arm_key="parent-pass",
        source_root="opaque-root",
        family="opaque-family",
        coarse_key="coarse-pass",
        feature_key=feature(service_profiles=("parent", "observed-child")),
    )
    evidence = scheduler.reserve_handoffs(1)[0]
    scheduler.commit_handoff(evidence)
    scheduler.on_tcp_outcome(evidence.ticket, responsive=True)
    scheduler.on_terminal(
        evidence.ticket,
        passed=True,
        outcome="pass",
        busy_worker_ms=300,
        stage="fast_probe",
    )
    scheduler.on_commit_complete(evidence.ticket, net_new=False)

    for index in range(8):
        scheduler.enqueue(
            candidate(f"prefix-{index}"),
            None,
            arm_key=f"prefix-{index}",
            source_root="opaque-root",
            family="opaque-family",
            coarse_key="coarse-prefix",
            feature_key=feature(
                service_profiles=("other-parent", f"prefix-{index}"),
            ),
        )
    scheduler.enqueue(
        candidate("hidden-direct-child"),
        None,
        arm_key="hidden-direct-child",
        source_root="opaque-root",
        family="opaque-family",
        coarse_key="coarse-hidden",
        feature_key=feature(service_profiles=("parent", "unseen-child")),
    )

    reservations = scheduler.reserve_handoffs(16)
    discovery = [
        item
        for item in reservations
        if item.ticket.reward_cohort_state == "parent_discovery"
    ]
    assert len(discovery) == 1
    assert discovery[0].candidate.node.name == "hidden-direct-child"
    assert discovery[0].ticket.selection_lane == "exploration"
    assert discovery[0].ticket.reward_cohort_key == "service:parent"
    assert discovery[0].ticket.unknown_exploration is True


def test_two_current_child_passes_open_parent_discovery_without_source_identity() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.25,
    )
    for index, child in enumerate(("child-a", "child-b")):
        scheduler.enqueue(
            candidate(f"parent-pass-{index}"),
            None,
            arm_key=f"parent-pass-{index}",
            source_root=f"opaque-root-{index}",
            family="opaque-protocol",
            coarse_key=f"parent-pass-{index}",
            feature_key=feature(
                family="opaque-protocol",
                service_profiles=("l0", "l1-parent", child, f"leaf-{index}"),
            ),
        )
        reservation = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
        scheduler.on_terminal(
            reservation.ticket,
            passed=True,
            outcome="pass",
            busy_worker_ms=500,
            stage="fast_probe",
        )
        scheduler.on_commit_complete(reservation.ticket, net_new=True)

    with scheduler._lock:
        state = scheduler._cohort_yield_states["service:l1-parent"]
        assert state.pass_total == 2
        assert len(state.current_pass_child_keys) == 2
        assert state.discovery is True
        assert state.lifecycle_state == "discovery"


def test_current_pass_and_live_child_inventory_open_bounded_cold_discovery() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.15,
        exploit_scan_share=0.25,
    )
    known_profiles = (
        "cold-l0",
        "cold-l1",
        "cold-l2-known",
        "cold-l3-known",
    )
    scheduler.enqueue(
        candidate("cold-evidence"),
        None,
        arm_key="cold-evidence",
        source_root="opaque-root",
        family="opaque-family",
        coarse_key="cold-evidence",
        feature_key=feature(
            family="opaque-family",
            service_profiles=known_profiles,
        ),
    )
    evidence = scheduler.reserve_handoffs(1)[0]
    scheduler.commit_handoff(evidence)
    scheduler.on_tcp_outcome(evidence.ticket, responsive=True)
    scheduler.on_terminal(
        evidence.ticket,
        passed=True,
        outcome="pass",
        busy_worker_ms=300,
        stage="fast_probe",
    )
    scheduler.on_commit_complete(evidence.ticket, net_new=False)

    with scheduler._lock:
        parent = scheduler._cohort_yield_states["service:cold-l1"]
        assert parent.eligibility is Tier3ServiceCohortEligibility.OBSERVE

    child_profiles = (
        known_profiles,
        ("cold-l0", "cold-l1", "cold-l2-b", "cold-l3-b"),
        ("cold-l0", "cold-l1", "cold-l2-c", "cold-l3-c"),
    )
    for child_index, profiles in enumerate(child_profiles):
        for item_index in range(3):
            name = f"cold-ready-{child_index}-{item_index}"
            scheduler.enqueue(
                candidate(name),
                None,
                arm_key=name,
                source_root="opaque-root",
                family="opaque-family",
                coarse_key=name,
                feature_key=feature(
                    family="opaque-family",
                    service_profiles=profiles,
                ),
            )

    with scheduler._lock:
        parent = scheduler._cohort_yield_states["service:cold-l1"]
        assert (
            parent.eligibility
            is Tier3ServiceCohortEligibility.PARENT_DISCOVERY
        )
        assert len(
            scheduler._cohort_queued_direct_children["service:cold-l1"]
        ) == 3

    reservations = scheduler.reserve_handoffs(16)
    discovery = [
        item
        for item in reservations
        if item.ticket.reward_cohort_state == "parent_discovery"
        and item.ticket.reward_cohort_key == "service:cold-l1"
    ]
    assert len(discovery) == 2
    assert all(item.ticket.selection_lane == "exploration" for item in discovery)
    assert all(item.ticket.unknown_exploration is True for item in discovery)
    selected_children = {
        item.ticket.feature_key.ordered_service_profiles()[2]
        for item in discovery
    }
    assert selected_children == {"cold-l2-b", "cold-l2-c"}


def test_current_pass_without_distinct_live_child_inventory_stays_observe() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.15,
        exploit_scan_share=0.25,
    )
    profiles = ("single-l0", "single-l1", "single-l2", "single-l3")
    scheduler.enqueue(
        candidate("single-evidence"),
        None,
        arm_key="single-evidence",
        source_root="opaque-root",
        family="opaque-family",
        coarse_key="single-evidence",
        feature_key=feature(
            family="opaque-family",
            service_profiles=profiles,
        ),
    )
    evidence = scheduler.reserve_handoffs(1)[0]
    scheduler.commit_handoff(evidence)
    scheduler.on_tcp_outcome(evidence.ticket, responsive=True)
    scheduler.on_terminal(
        evidence.ticket,
        passed=True,
        outcome="pass",
        busy_worker_ms=300,
        stage="fast_probe",
    )
    scheduler.on_commit_complete(evidence.ticket, net_new=False)

    for index in range(12):
        name = f"single-ready-{index}"
        scheduler.enqueue(
            candidate(name),
            None,
            arm_key=name,
            source_root="opaque-root",
            family="opaque-family",
            coarse_key=name,
            feature_key=feature(
                family="opaque-family",
                service_profiles=profiles,
            ),
        )

    with scheduler._lock:
        parent = scheduler._cohort_yield_states["service:single-l1"]
        assert parent.eligibility is Tier3ServiceCohortEligibility.OBSERVE
        assert len(
            scheduler._cohort_queued_direct_children["service:single-l1"]
        ) == 1

    reservations = scheduler.reserve_handoffs(16)
    assert all(
        item.ticket.reward_cohort_state != "parent_discovery"
        for item in reservations
    )


def test_sparse_child_uses_parent_evidence_for_bounded_probation() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.25,
    )

    def complete(name: str, profiles: tuple[str, ...], passed: bool) -> None:
        scheduler.enqueue(
            candidate(name),
            None,
            arm_key=name,
            source_root=f"root-{name}",
            family="opaque-protocol",
            coarse_key=name,
            feature_key=feature(
                family="opaque-protocol",
                service_profiles=profiles,
            ),
        )
        reservation = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
        scheduler.on_terminal(
            reservation.ticket,
            passed=passed,
            outcome="pass" if passed else "candidate_fail",
            busy_worker_ms=500,
            stage="fast_probe",
        )
        scheduler.on_commit_complete(reservation.ticket, net_new=True)

    for index in range(32):
        complete(f"ordinary-parent-{index}", (), False)
    for index in range(8):
        complete(f"parent-fail-{index}", ("parent",), False)
    for index in range(2):
        complete(f"parent-pass-{index}", ("parent",), True)
    for index in range(2):
        complete(f"child-{index}", ("parent", "child"), True)

    with scheduler._lock:
        child = scheduler._cohort_yield_states["service:child"]
    assert child.completed_total == 2
    assert child.probation is True
    assert child.promoted is False
    assert child.exploit_share == 0.25


def test_sparse_child_probation_reservations_are_bounded_to_four_per_sixteen() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.25,
    )

    def complete(name: str, profiles: tuple[str, ...], passed: bool) -> None:
        scheduler.enqueue(
            candidate(name),
            None,
            arm_key=name,
            source_root=f"evidence-{name}",
            family="opaque-protocol",
            coarse_key=name,
            feature_key=feature(
                family="opaque-protocol",
                service_profiles=profiles,
            ),
        )
        reservation = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
        scheduler.on_terminal(
            reservation.ticket,
            passed=passed,
            outcome="pass" if passed else "candidate_fail",
            busy_worker_ms=500,
            stage="fast_probe",
        )
        scheduler.on_commit_complete(reservation.ticket, net_new=False)

    for index in range(32):
        complete(f"ordinary-quota-{index}", (), False)
    for index in range(8):
        complete(f"parent-quota-fail-{index}", ("parent-quota",), False)
    for index in range(2):
        complete(f"parent-quota-pass-{index}", ("parent-quota",), True)
    for index in range(2):
        complete(
            f"child-quota-{index}",
            ("parent-quota", "child-quota"),
            True,
        )

    for index in range(24):
        scheduler.enqueue(
            candidate(f"child-ready-{index}"),
            None,
            arm_key=f"child-ready-{index}",
            source_root="quota-root",
            family="opaque-protocol",
            coarse_key=f"child-ready-{index}",
            feature_key=feature(
                family="opaque-protocol",
                service_profiles=("parent-quota", "child-quota"),
            ),
        )
    reservations = scheduler.reserve_handoffs(16)

    child_probation = [
        item
        for item in reservations
        if item.ticket.reward_probation
        and item.ticket.reward_cohort_key == "service:child-quota"
    ]
    assert len(reservations) == 16
    assert len(child_probation) == 4
    assert scheduler.quick_metrics()["reserved_probation_handoff_count"] == 4

    for item in reservations:
        scheduler.cancel_handoff(item, requeue=True)
    assert scheduler.quick_metrics()["reserved_probation_handoff_count"] == 0


def test_parent_probation_cannot_reward_an_unrelated_child_cohort() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.25,
    )

    def complete(
        name: str,
        profiles: tuple[str, ...],
        *,
        passed: bool,
    ) -> None:
        scheduler.enqueue(
            candidate(name),
            None,
            arm_key=name,
            source_root="opaque-root",
            family="opaque-protocol",
            coarse_key=name,
            feature_key=feature(
                family="opaque-protocol",
                service_profiles=profiles,
            ),
        )
        reservation = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
        scheduler.on_terminal(
            reservation.ticket,
            passed=passed,
            outcome="pass" if passed else "candidate_fail",
            busy_worker_ms=500,
            stage="fast_probe",
        )
        scheduler.on_commit_complete(reservation.ticket, net_new=False)

    # Mixed children make the broad ancestor look profitable. Two observations
    # of one leaf independently earn only that leaf a probation entitlement.
    for index in range(3):
        complete(
            f"ancestor-pass-{index}",
            (
                "l0-broad",
                f"l1-seen-{index}",
                f"l2-seen-{index}",
                f"l3-seen-{index}",
            ),
            passed=True,
        )
    for index in range(2):
        complete(
            f"leaf-evidence-{index}",
            ("l0-broad", "l1-good", "l2-good", "l3-good"),
            passed=True,
        )

    for index in range(8):
        scheduler.enqueue(
            candidate(f"unrelated-{index}"),
            None,
            arm_key=f"unrelated-{index}",
            source_root="opaque-root",
            family="opaque-protocol",
            coarse_key=f"unrelated-{index}",
            feature_key=feature(
                family="opaque-protocol",
                service_profiles=(
                    "l0-broad",
                    "l1-unrelated",
                    "l2-unrelated",
                    "l3-unrelated",
                ),
            ),
        )
    for index in range(8):
        scheduler.enqueue(
            candidate(f"matching-{index}"),
            None,
            arm_key=f"matching-{index}",
            source_root="opaque-root",
            family="opaque-protocol",
            coarse_key=f"matching-{index}",
            feature_key=feature(
                family="opaque-protocol",
                service_profiles=(
                    "l0-broad",
                    "l1-good",
                    "l2-good",
                    "l3-good",
                ),
            ),
        )

    reservations = scheduler.reserve_handoffs(16)
    rewarded = [
        item for item in reservations if item.ticket.selection_lane == "reward"
    ]

    assert rewarded
    assert all(
        item.ticket.reward_cohort_key == "service:l3-good"
        for item in rewarded
    )
    assert all(
        item.candidate.node.name.startswith("matching-") for item in rewarded
    )


def test_multistrata_parent_evidence_uses_only_verified_direct_children() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.25,
    )

    def observe(index: int, *, passed: bool) -> None:
        child = f"verified-child-{index}" if passed else f"failed-child-{index}"
        scheduler.enqueue(
            candidate(f"parent-evidence-{index}"),
            None,
            arm_key=f"parent-evidence-{index}",
            source_root=f"opaque-root-{index % 2}",
            family="opaque-family",
            coarse_key=f"parent-evidence-{index}",
            feature_key=feature(
                family="opaque-family",
                service_profiles=("l0", "l1", "l2-parent", child),
            ),
        )
        reservation = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
        scheduler.on_fast_observation(
            reservation.ticket,
            outcome="pass" if passed else "candidate_fail",
            busy_worker_ms=500,
            passed=passed,
        )
        scheduler.on_terminal(
            reservation.ticket,
            passed=passed,
            outcome="pass" if passed else "candidate_fail",
            busy_worker_ms=500,
            stage="fast_probe",
        )
        if passed:
            scheduler.on_commit_complete(reservation.ticket, net_new=True)

    for index in range(8):
        observe(index, passed=index < 3)

    with scheduler._lock:
        parent = scheduler._cohort_yield_states["service:l2-parent"]
        assert parent.eligibility is Tier3ServiceCohortEligibility.PARENT_PROBATION
        assert len(parent.current_pass_child_keys) == 3
        assert len(parent.current_pass_root_families) == 2

    for index in range(8):
        scheduler.enqueue(
            candidate(f"verified-ready-{index}"),
            None,
            arm_key=f"verified-ready-{index}",
            source_root=f"opaque-root-{index % 2}",
            family="opaque-family",
            coarse_key=f"verified-ready-{index}",
            feature_key=feature(
                family="opaque-family",
                service_profiles=(
                    "l0",
                    "l1",
                    "l2-parent",
                    "verified-child-0",
                ),
            ),
        )
        scheduler.enqueue(
            candidate(f"unseen-ready-{index}"),
            None,
            arm_key=f"unseen-ready-{index}",
            source_root=f"opaque-root-{index % 2}",
            family="opaque-family",
            coarse_key=f"unseen-ready-{index}",
            feature_key=feature(
                family="opaque-family",
                service_profiles=(
                    "l0",
                    "l1",
                    "l2-parent",
                    "unseen-child",
                ),
            ),
        )

    reservations = scheduler.reserve_handoffs(16)
    parent_reward = [
        item
        for item in reservations
        if item.ticket.reward_cohort_key == "service:l2-parent"
        and item.ticket.selection_lane == "reward"
    ]
    assert len(parent_reward) == 4
    assert all(
        item.candidate.node.name.startswith("verified-ready-")
        for item in parent_reward
    )

    for item in parent_reward[:2]:
        scheduler.commit_handoff(item)
        scheduler.on_tcp_outcome(item.ticket, responsive=True)
        scheduler.on_fast_observation(
            item.ticket,
            outcome="pass",
            busy_worker_ms=500,
            passed=True,
        )
        scheduler.on_terminal(
            item.ticket,
            passed=True,
            outcome="pass",
            busy_worker_ms=500,
            stage="fast_probe",
        )
        scheduler.on_commit_complete(item.ticket, net_new=True)
    for item in reservations:
        if item not in parent_reward[:2]:
            scheduler.cancel_handoff(item, requeue=True)

    with scheduler._lock:
        parent = scheduler._cohort_yield_states["service:l2-parent"]
        assert parent.eligibility is Tier3ServiceCohortEligibility.PARENT_PROVEN
        assert parent.exploit_share == 0.50


def test_executable_tail_replay_redeems_sparse_leaf_without_internal_stall() -> None:
    """Exercise the complete scheduler feedback order used by production.

    A small, source-independent structural cohort is mixed into a much larger
    low-yield population.  Two early ordinary observations must open bounded
    holdout, then durable net-new feedback must make the remaining inventory
    reachable without broad root/family fallback.
    """

    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.15,
        exploit_scan_share=0.25,
    )
    winning_profiles = (
        "opaque-l0",
        "opaque-l1",
        "opaque-l2",
        "opaque-l3-winning",
    )
    winning_names = {f"winning-{index}" for index in range(25)}

    # Two source-independent training candidates arrive early.  The remaining
    # winning candidates are deliberately placed behind ordinary inventory.
    for index in range(2):
        scheduler.enqueue(
            candidate(f"winning-{index}", protocol="opaque"),
            None,
            arm_key=f"winning-arm-{index}",
            source_root=f"opaque-root-{index}",
            family="opaque-family",
            coarse_key=f"winning-{index}",
            feature_key=feature(
                family="opaque-family",
                service_profiles=winning_profiles,
            ),
        )
    for index in range(600):
        scheduler.enqueue(
            candidate(f"ordinary-{index}", protocol="opaque"),
            None,
            arm_key=f"ordinary-arm-{index}",
            source_root=f"opaque-root-{index % 5}",
            family="opaque-family",
            coarse_key=f"ordinary-{index}",
            feature_key=feature(
                family="opaque-family",
                service_profiles=(
                    "ordinary-l0",
                    f"ordinary-l1-{index % 17}",
                    f"ordinary-l2-{index % 61}",
                    f"ordinary-l3-{index}",
                ),
            ),
        )
        if index < 23:
            winner_index = index + 2
            scheduler.enqueue(
                candidate(f"winning-{winner_index}", protocol="opaque"),
                None,
                arm_key=f"winning-arm-{winner_index}",
                source_root=f"opaque-root-{winner_index % 5}",
                family="opaque-family",
                coarse_key=f"winning-{winner_index}",
                feature_key=feature(
                    family="opaque-family",
                    service_profiles=winning_profiles,
                ),
            )

    committed = 0
    handoffs_at_15 = 0
    handoffs_at_25 = 0
    handoff_total = 0
    empty_waves = 0
    while committed < 25 and handoff_total < 768:
        wave = scheduler.reserve_handoffs(16)
        if not wave:
            empty_waves += 1
            assert empty_waves <= 1
            continue
        empty_waves = 0
        for reservation in wave:
            scheduler.commit_handoff(reservation)
            handoff_total += 1
            name = reservation.candidate.node.name
            if name in winning_names:
                scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
                scheduler.on_fast_observation(
                    reservation.ticket,
                    outcome="pass",
                    busy_worker_ms=500,
                    passed=True,
                )
                scheduler.on_terminal(
                    reservation.ticket,
                    passed=True,
                    outcome="pass",
                    busy_worker_ms=500,
                    stage="fast_probe",
                )
                scheduler.on_commit_complete(
                    reservation.ticket,
                    net_new=True,
                )
                committed += 1
                if committed == 15:
                    handoffs_at_15 = handoff_total
                if committed == 25:
                    handoffs_at_25 = handoff_total
            elif handoff_total % 3:
                scheduler.on_tcp_outcome(reservation.ticket, responsive=False)
            else:
                scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
                scheduler.on_fast_observation(
                    reservation.ticket,
                    outcome="candidate_fail",
                    busy_worker_ms=500,
                )
                scheduler.on_fast_yield(
                    reservation.ticket,
                    outcome="candidate_fail",
                    busy_worker_ms=500,
                )
                scheduler.on_terminal(
                    reservation.ticket,
                    passed=False,
                    outcome="candidate_fail",
                    busy_worker_ms=500,
                    stage="canonical_deep",
                )

    # Sixteen start acknowledgements per 1.25 second executor cycle means
    # 256 handoffs represent 20 seconds and 768 represent 60 seconds.
    assert committed == 25
    assert handoffs_at_15 <= 256
    assert handoffs_at_25 <= 768
    metrics = scheduler.quick_metrics()
    assert metrics["fast_observation_pass_count"] == 25
    assert metrics["reward_service_selected_count"] >= 4
    assert metrics["reward_service_net_new_count"] >= 2
    assert metrics["reward_family_fallback_hit_count"] == 0
    assert metrics["feedback_only_ticket_count"] == 0
    assert scheduler.release_outstanding() == 0


def test_reward_uses_the_selected_candidate_feature_inside_a_mixed_coarse_arm() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.25,
    )
    good = ("l0", "l1-good", "l2-good", "l3-good")
    bad = ("l0", "l1-bad", "l2-bad", "l3-bad")

    for index in range(2):
        scheduler.enqueue(
            candidate(f"train-{index}"),
            None,
            arm_key=f"train-{index}",
            source_root="opaque-root",
            family="opaque-protocol",
            coarse_key=f"train-{index}",
            feature_key=feature(
                family="opaque-protocol",
                service_profiles=good,
            ),
        )
        reservation = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
        scheduler.on_terminal(
            reservation.ticket,
            passed=True,
            outcome="pass",
            busy_worker_ms=500,
            stage="fast_probe",
        )
        scheduler.on_commit_complete(reservation.ticket, net_new=False)

    for name, profiles in (("bad-head", bad), ("good-behind", good)):
        scheduler.enqueue(
            candidate(name),
            None,
            arm_key=name,
            source_root="opaque-root",
            family="opaque-protocol",
            coarse_key="shared-coarse",
            feature_key=feature(
                family="opaque-protocol",
                service_profiles=profiles,
            ),
        )

    selected = scheduler.reserve_handoffs(1)[0]
    assert selected.ticket.selection_lane == "reward"
    assert selected.ticket.reward_cohort_key == "service:l3-good"
    assert selected.candidate.node.name == "good-behind"
    assert selected.ticket.feature_key.ordered_service_profiles() == good


def test_sparse_leaf_confirmation_has_one_global_flight_until_feedback() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.0,
    )
    profiles = ("l0-confirm", "l1-confirm", "l2-confirm", "l3-confirm")
    scheduler.enqueue(
        candidate("seed-confirm"),
        None,
        arm_key="seed-confirm",
        source_root="root-confirm",
        family="opaque-family",
        coarse_key="seed",
        feature_key=feature(
            family="opaque-family",
            service_profiles=profiles,
        ),
    )
    seed = scheduler.reserve_handoffs(1)[0]
    scheduler.commit_handoff(seed)
    scheduler.on_tcp_outcome(seed.ticket, responsive=True)
    scheduler.on_terminal(
        seed.ticket,
        passed=True,
        outcome="pass",
        busy_worker_ms=200,
        stage="fast_probe",
    )
    scheduler.on_commit_complete(seed.ticket, net_new=False)

    for index in range(2):
        scheduler.enqueue(
            candidate(f"confirm-{index}"),
            None,
            arm_key=f"confirm-{index}",
            source_root="root-confirm",
            family="opaque-family",
            coarse_key=f"confirm-{index}",
            feature_key=feature(
                family="opaque-family",
                service_profiles=profiles,
            ),
        )

    confirmation = scheduler.reserve_handoffs(1)[0]
    assert confirmation.ticket.reward_cohort_state == "leaf_confirmation"
    concurrent = scheduler.reserve_handoffs(1)
    assert all(
        item.ticket.reward_cohort_state != "leaf_confirmation"
        for item in concurrent
    )
    metrics = scheduler.quick_metrics()
    assert metrics["reserved_leaf_confirmation_handoff_count"] == 1
    assert metrics["leaf_confirmation_outstanding_count"] == 1
    for item in concurrent:
        scheduler.cancel_handoff(item, requeue=False)

    scheduler.commit_handoff(confirmation)
    scheduler.on_tcp_outcome(confirmation.ticket, responsive=True)
    scheduler.on_terminal(
        confirmation.ticket,
        passed=True,
        outcome="pass",
        busy_worker_ms=200,
        stage="fast_probe",
    )
    scheduler.on_commit_complete(confirmation.ticket, net_new=False)

    leaf = next(
        row
        for row in scheduler.quick_metrics()["reward_service_top_cohorts"]
        if row["cohort_id"] == "l3-confirm"
    )
    assert leaf["eligibility"] == "leaf_probation"
    assert leaf["confirmation_selection_total"] == 1
    assert leaf["confirmation_pass_total"] == 1
    assert scheduler.release_outstanding() == 0


def test_parent_discovery_prioritizes_strong_evidence_with_live_children() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.0,
    )

    def record_pass(name: str, profiles: tuple[str, ...]) -> None:
        scheduler.enqueue(
            candidate(name),
            None,
            arm_key=name,
            source_root="root-training",
            family="opaque-family",
            coarse_key=name,
            feature_key=feature(
                family="opaque-family",
                service_profiles=profiles,
            ),
        )
        item = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(item)
        scheduler.on_tcp_outcome(item.ticket, responsive=True)
        scheduler.on_terminal(
            item.ticket,
            passed=True,
            outcome="pass",
            busy_worker_ms=200,
            stage="fast_probe",
        )
        scheduler.on_commit_complete(item.ticket, net_new=False)

    strong = ("l0-shared", "l1-shared", "l2-strong", "l3-strong")
    weak = ("l0-shared", "l1-shared", "l2-weak", "l3-weak")
    for index in range(4):
        record_pass(f"strong-train-{index}", strong)
    record_pass("weak-train", weak)

    for parent_name in ("strong", "weak"):
        for child_index in range(2):
            profiles = (
                "l0-shared",
                "l1-shared",
                f"l2-{parent_name}",
                f"l3-{parent_name}-child-{child_index}",
            )
            scheduler.enqueue(
                candidate(f"{parent_name}-child-{child_index}"),
                None,
                arm_key=f"{parent_name}-child-{child_index}",
                source_root=f"root-{parent_name}-{child_index}",
                family="opaque-family",
                coarse_key=f"{parent_name}-child-{child_index}",
                feature_key=feature(
                    family="opaque-family",
                    service_profiles=profiles,
                ),
            )

    with scheduler._lock:
        selected, _examined = (
            scheduler._pop_parent_discovery_candidate_unlocked(
                ignore_credits=False,
            )
        )
    assert selected is not None
    assert selected.dispatch_ticket is not None
    assert selected.dispatch_ticket.reward_cohort_key == "service:l2-strong"
    assert selected.dispatch_ticket.reward_cohort_state == "parent_discovery"
    scheduler.cancel_handoff(selected.dispatch_ticket, requeue=False)
    assert scheduler.release_outstanding() == 0


def test_default_lane_serves_exact_leaf_confirmation_before_parent_discovery() -> None:
    scheduler = Tier3CandidateScheduler(
        unknown_exploration_ratio=0.15,
        exploit_scan_share=0.75,
    )

    def record_pass(name: str, profiles: tuple[str, ...]) -> None:
        scheduler.enqueue(
            candidate(name),
            None,
            arm_key=name,
            source_root="opaque-root",
            family="opaque-family",
            coarse_key=name,
            feature_key=feature(
                family="opaque-family",
                service_profiles=profiles,
            ),
        )
        item = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(item)
        scheduler.on_tcp_outcome(item.ticket, responsive=True)
        scheduler.on_terminal(
            item.ticket,
            passed=True,
            outcome="pass",
            busy_worker_ms=200,
            stage="fast_probe",
        )
        scheduler.on_commit_complete(item.ticket, net_new=False)

    strong = ("l0-shared", "l1-shared", "l2-strong", "l3-strong")
    weak = ("l0-shared", "l1-shared", "l2-weak", "l3-weak")
    for index in range(4):
        record_pass(f"strong-train-{index}", strong)
    record_pass("weak-train", weak)

    queued_profiles = (
        ("strong-child-a", (*strong[:3], "l3-strong-child-a")),
        ("strong-child-b", (*strong[:3], "l3-strong-child-b")),
        ("weak-confirm", weak),
        ("cold-unrelated", ("l0-cold", "l1-cold", "l2-cold", "l3-cold")),
    )
    for name, profiles in queued_profiles:
        scheduler.enqueue(
            candidate(name),
            None,
            arm_key=name,
            source_root="opaque-root",
            family="opaque-family",
            coarse_key=name,
            feature_key=feature(
                family="opaque-family",
                service_profiles=profiles,
            ),
        )

    reservations = scheduler.reserve_handoffs(4)
    discovery = [
        item
        for item in reservations
        if item.ticket.reward_cohort_state
        in {"parent_discovery", "leaf_confirmation"}
        or item.ticket.selection_reason == "cold_leaf_coverage"
    ]
    assert discovery
    assert discovery[0].ticket.reward_cohort_state == "leaf_confirmation"
    assert discovery[0].ticket.reward_cohort_key == "service:l3-weak"
    assert all(item.ticket.reward_scope != "similar" for item in reservations)
    for item in reservations:
        scheduler.cancel_handoff(item, requeue=False)
    assert scheduler.release_outstanding() == 0


def test_cohort_inventory_tracks_reserve_cancel_and_drain_without_scanning_arms() -> None:
    scheduler = legacy_candidate_scheduler(unknown_exploration_ratio=0.0)
    profiles = ("l0", "l1", "l2", "l3")
    for index, root in enumerate(("root-a", "root-b")):
        scheduler.enqueue(
            candidate(f"inventory-{index}"),
            None,
            arm_key=f"inventory-{index}",
            source_root=root,
            family="opaque-protocol",
            coarse_key="shared",
            feature_key=feature(
                family="opaque-protocol",
                service_profiles=profiles,
            ),
        )

    with scheduler._lock:
        inventory = scheduler._cohort_inventory_unlocked("service:l3")
        direct_children = dict(
            scheduler._cohort_queued_direct_children["service:l1"]
        )
    assert inventory == {
        "reservable": 2,
        "reserved": 0,
        "active": 0,
        "root_family_count": 2,
    }
    assert direct_children == {"service:l2": 2}

    reservation = scheduler.reserve_handoffs(1)[0]
    with scheduler._lock:
        reserved = scheduler._cohort_inventory_unlocked("service:l3")
        reserved_children = dict(
            scheduler._cohort_queued_direct_children["service:l1"]
        )
    assert reserved["reservable"] == 1
    assert reserved["reserved"] == 1
    assert reserved["root_family_count"] == 2
    assert reserved_children == {"service:l2": 1}

    assert scheduler.cancel_handoff(reservation, requeue=True)
    with scheduler._lock:
        restored = scheduler._cohort_inventory_unlocked("service:l3")
        restored_children = dict(
            scheduler._cohort_queued_direct_children["service:l1"]
        )
    assert restored == inventory
    assert restored_children == direct_children

    assert len(scheduler.drain_all()) == 2
    with scheduler._lock:
        drained = scheduler._cohort_inventory_unlocked("service:l3")
        assert not scheduler._cohort_queued_direct_children
    assert drained == {
        "reservable": 0,
        "reserved": 0,
        "active": 0,
        "root_family_count": 0,
    }


def test_service_decision_ring_records_only_opaque_cohort_truth() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.25,
    )
    profiles = ("opaque-l0", "opaque-l1", "opaque-l2", "opaque-l3")
    for index in range(8):
        scheduler.enqueue(
            candidate(f"decision-{index}"),
            None,
            arm_key=f"decision-{index}",
            source_root="secret-root-name",
            family="opaque-protocol",
            coarse_key=f"decision-{index}",
            feature_key=feature(
                family="opaque-protocol",
                service_profiles=profiles,
            ),
        )
        reservation = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
        scheduler.on_terminal(
            reservation.ticket,
            passed=True,
            outcome="pass",
            busy_worker_ms=500,
            stage="fast_probe",
        )
        scheduler.on_commit_complete(reservation.ticket, net_new=True)

    decisions = scheduler.quick_metrics()["reward_service_recent_decisions"]
    assert decisions
    assert any(item["outcome"] == "committed" for item in decisions)
    serialized = repr(decisions)
    assert "secret-root-name" not in serialized
    assert "decision-" not in serialized


def test_service_decision_ring_preserves_typed_tcp_terminal_outcome() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.25,
    )
    profiles = ("opaque-l0", "opaque-l1", "opaque-l2", "opaque-l3")
    for index in range(2):
        scheduler.enqueue(
            candidate(f"training-{index}"),
            None,
            arm_key=f"training-{index}",
            source_root="opaque-root",
            family="opaque-protocol",
            coarse_key=f"training-{index}",
            feature_key=feature(
                family="opaque-protocol",
                service_profiles=profiles,
            ),
        )
        training = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(training)
        scheduler.on_tcp_outcome(training.ticket, responsive=True)
        scheduler.on_terminal(
            training.ticket,
            passed=True,
            outcome="pass",
            busy_worker_ms=500,
            stage="fast_probe",
        )
        scheduler.on_commit_complete(training.ticket, net_new=False)

    scheduler.enqueue(
        candidate("tcp-dead-holdout"),
        None,
        arm_key="tcp-dead-holdout",
        source_root="opaque-root",
        family="opaque-protocol",
        coarse_key="tcp-dead-holdout",
        feature_key=feature(
            family="opaque-protocol",
            service_profiles=profiles,
        ),
    )
    selected = scheduler.reserve_handoffs(1)[0]
    assert selected.ticket.selection_lane == "reward"
    scheduler.commit_handoff(selected)
    scheduler.on_tcp_outcome(selected.ticket, responsive=False)
    scheduler.on_terminal(
        selected.ticket,
        outcome="tcp_dead",
        stage="tcp",
    )

    decisions = scheduler.quick_metrics()["reward_service_recent_decisions"]
    assert decisions[-1]["outcome"] == "tcp_dead"
    assert decisions[-1]["passed"] is False


def test_sealed_release_closes_pending_service_decision() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.25,
    )
    profiles = ("l0", "l1", "l2", "l3-leaf")
    for index in range(2):
        scheduler.enqueue(
            candidate(f"training-{index}"),
            None,
            arm_key=f"training-{index}",
            source_root="opaque-root",
            family="opaque-protocol",
            coarse_key=f"training-{index}",
            feature_key=feature(
                family="opaque-protocol",
                service_profiles=profiles,
            ),
        )
        training = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(training)
        scheduler.on_tcp_outcome(training.ticket, responsive=True)
        scheduler.on_terminal(
            training.ticket,
            passed=True,
            outcome="pass",
            busy_worker_ms=500,
            stage="fast_probe",
        )
        scheduler.on_commit_complete(training.ticket, net_new=False)

    scheduler.enqueue(
        candidate("pending-holdout"),
        None,
        arm_key="pending-holdout",
        source_root="opaque-root",
        family="opaque-protocol",
        coarse_key="pending-holdout",
        feature_key=feature(
            family="opaque-protocol",
            service_profiles=profiles,
        ),
    )
    pending = scheduler.reserve_handoffs(1)[0]
    assert pending.ticket.selection_lane == "reward"
    assert scheduler.quick_metrics()["reward_service_pending_decision_count"] == 1
    assert scheduler.quick_metrics()["reserved_service_handoff_count"] == 1

    assert scheduler.release_outstanding() == 1
    metrics = scheduler.quick_metrics()
    assert metrics["active_ticket_count"] == 0
    assert metrics["reward_service_pending_decision_count"] == 0
    assert metrics["reserved_service_handoff_count"] == 0
    assert metrics["reward_service_recent_decisions"][-1]["outcome"] == "sealed"


def test_leaf_probation_promotes_from_two_independent_reward_passes() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.25,
    )
    profiles = ("l0", "l1", "l2", "l3-leaf")

    def complete(name: str, *, passed: bool) -> None:
        scheduler.enqueue(
            candidate(name),
            None,
            arm_key=name,
            source_root="opaque-root",
            family="opaque-protocol",
            coarse_key=name,
            feature_key=feature(
                family="opaque-protocol",
                service_profiles=profiles,
            ),
        )
        reservation = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
        scheduler.on_terminal(
            reservation.ticket,
            passed=passed,
            outcome="pass" if passed else "candidate_fail",
            busy_worker_ms=500,
            stage="fast_probe",
        )
        scheduler.on_commit_complete(reservation.ticket, net_new=passed)

    complete("training-pass-0", passed=True)
    complete("training-pass-1", passed=True)
    # Probation owns at most four of every sixteen handoffs. Feed enough
    # ordinary siblings for two independent probation selections.
    for index in range(8):
        complete(f"holdout-pass-{index}", passed=True)

    with scheduler._lock:
        state = scheduler._cohort_yield_states["service:l3-leaf"]
        assert state.probation is False
        assert state.promoted is True
        assert state.lifecycle_state == "proven_50"
        assert state.exploit_share == 0.50


def test_leaf_probation_demotes_after_four_reward_nonpasses() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.25,
    )
    profiles = ("l0", "l1", "l2", "l3-leaf")

    def complete(name: str, *, passed: bool) -> None:
        scheduler.enqueue(
            candidate(name),
            None,
            arm_key=name,
            source_root="opaque-root",
            family="opaque-protocol",
            coarse_key=name,
            feature_key=feature(
                family="opaque-protocol",
                service_profiles=profiles,
            ),
        )
        reservation = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
        scheduler.on_terminal(
            reservation.ticket,
            passed=passed,
            outcome="pass" if passed else "candidate_fail",
            busy_worker_ms=500,
            stage="fast_probe",
        )
        scheduler.on_commit_complete(reservation.ticket, net_new=False)

    complete("training-pass-0", passed=True)
    complete("training-pass-1", passed=True)
    # Four production non-passes require sixteen total handoff opportunities
    # at the probation lane's 4/16 cap.
    for index in range(16):
        complete(f"holdout-fail-{index}", passed=False)

    with scheduler._lock:
        state = scheduler._cohort_yield_states["service:l3-leaf"]
        assert state.probation is False
        assert state.promoted is False
        assert state.lifecycle_state == "demoted"
        assert state.last_decision_reason == (
            "probation_four_consecutive_nonpass"
        )


def test_leaf_probation_demotes_after_pass_then_four_reward_nonpasses() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.25,
    )
    profiles = ("l0", "l1", "l2", "l3-leaf")

    def complete(name: str, *, passed: bool) -> None:
        scheduler.enqueue(
            candidate(name),
            None,
            arm_key=name,
            source_root="opaque-root",
            family="opaque-protocol",
            coarse_key=name,
            feature_key=feature(
                family="opaque-protocol",
                service_profiles=profiles,
            ),
        )
        reservation = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
        scheduler.on_terminal(
            reservation.ticket,
            passed=passed,
            outcome="pass" if passed else "candidate_fail",
            busy_worker_ms=500,
            stage="fast_probe",
        )
        scheduler.on_commit_complete(reservation.ticket, net_new=False)

    complete("training-pass-0", passed=True)
    complete("training-pass-1", passed=True)
    complete("holdout-pass", passed=True)
    for index in range(32):
        complete(f"holdout-fail-{index}", passed=False)

    with scheduler._lock:
        state = scheduler._cohort_yield_states["service:l3-leaf"]
        assert state.eligibility == Tier3ServiceCohortEligibility.DEMOTED
        assert state.production_consecutive_nonpass == 4

    metrics = scheduler.quick_metrics()
    assert metrics["reward_service_probation_cohort_count"] == 0
    assert metrics["reward_service_demotion_count"] >= 1


def test_executable_tail_replay_redeems_precise_leaf_without_sibling_fallback() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.15,
        exploit_scan_share=0.25,
    )
    good = ("l0-shared", "l1-shared", "l2-good", "l3-good")
    sibling = ("l0-shared", "l1-shared", "l2-sibling", "l3-sibling")

    def finish(reservation, *, passed: bool) -> None:
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
        scheduler.on_terminal(
            reservation.ticket,
            passed=passed,
            outcome="pass" if passed else "candidate_fail",
            busy_worker_ms=500,
            stage="fast_probe",
        )
        scheduler.on_commit_complete(reservation.ticket, net_new=passed)

    committed = 0
    for index in range(2):
        scheduler.enqueue(
            candidate(f"seed-good-{index}"),
            None,
            arm_key=f"seed-good-{index}",
            source_root="opaque-root",
            family="opaque-protocol",
            coarse_key=f"seed-good-{index}",
            feature_key=feature(
                family="opaque-protocol",
                service_profiles=good,
            ),
        )
        reservation = scheduler.reserve_handoffs(1)[0]
        finish(reservation, passed=True)
        committed += 1

    # Low-yield siblings occupy the active-arm front. Precise leaf inventory
    # must still be pulled from overflow without authorizing its ancestors.
    for index in range(128):
        scheduler.enqueue(
            candidate(f"sibling-{index}"),
            None,
            arm_key=f"sibling-{index}",
            source_root="opaque-root",
            family="opaque-protocol",
            coarse_key=f"sibling-{index}",
            feature_key=feature(
                family="opaque-protocol",
                service_profiles=sibling,
            ),
        )
    for index in range(23):
        scheduler.enqueue(
            candidate(f"tail-good-{index}"),
            None,
            arm_key=f"tail-good-{index}",
            source_root="opaque-root",
            family="opaque-protocol",
            coarse_key=f"tail-good-{index}",
            feature_key=feature(
                family="opaque-protocol",
                service_profiles=good,
            ),
        )

    replay_handoffs = 0
    for _epoch in range(8):
        reservations = scheduler.reserve_handoffs(16)
        assert reservations
        replay_handoffs += len(reservations)
        for reservation in reservations:
            passed = reservation.candidate.node.name.startswith("tail-good-")
            if passed:
                committed += 1
            finish(reservation, passed=passed)
        if committed >= 25:
            break

    assert committed == 25
    metrics = scheduler.quick_metrics()
    relevant_cohorts = [
        (
            item["cohort_id"],
            item["eligibility"],
            item["production_selection_total"],
            item["production_completion_total"],
            item["production_pass_total"],
            item["inventory_reservable"],
            item["decision_reason"],
        )
        for item in metrics["reward_service_top_cohorts"]
        if item["cohort_id"] in {"l3-good", "l3-sibling"}
    ]
    assert replay_handoffs <= 64, (
        replay_handoffs,
        metrics["reward_service_hit_count"],
        metrics["reward_service_fast_pass_count"],
        metrics["reward_service_promotion_count"],
        metrics["reward_service_probation_cohort_count"],
        metrics["reward_service_promoted_cohort_count"],
        relevant_cohorts,
    )
    decisions = metrics["reward_service_recent_decisions"]
    assert decisions
    production_decisions = [
        item
        for item in decisions
        if item["eligibility"] != "parent_discovery"
    ]
    discovery_decisions = [
        item
        for item in decisions
        if item["eligibility"] == "parent_discovery"
    ]
    assert production_decisions
    assert all(
        item["cohort_id"] == "l3-good" for item in production_decisions
    )
    assert all(
        item["selection_reason"] == "direct_child_discovery"
        for item in discovery_decisions
    )
    assert sum(item["outcome"] == "committed" for item in decisions) >= 2
    assert not any(item["cohort_id"] == "l0-shared" for item in decisions)


def test_sparse_probation_budget_is_shared_across_service_cohorts() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.25,
    )

    def complete(name: str, profiles: tuple[str, ...], passed: bool) -> None:
        scheduler.enqueue(
            candidate(name),
            None,
            arm_key=name,
            source_root=f"evidence-{name}",
            family="opaque-protocol",
            coarse_key=name,
            feature_key=feature(
                family="opaque-protocol",
                service_profiles=profiles,
            ),
        )
        reservation = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
        scheduler.on_terminal(
            reservation.ticket,
            passed=passed,
            outcome="pass" if passed else "candidate_fail",
            busy_worker_ms=500,
            stage="fast_probe",
        )
        scheduler.on_commit_complete(reservation.ticket, net_new=False)

    for index in range(32):
        complete(f"ordinary-shared-{index}", (), False)
    for cohort in ("a", "b"):
        parent = f"parent-{cohort}"
        child = f"child-{cohort}"
        for index in range(8):
            complete(f"{parent}-fail-{index}", (parent,), False)
        for index in range(2):
            complete(f"{parent}-pass-{index}", (parent,), True)
        for index in range(2):
            complete(f"{child}-pass-{index}", (parent, child), True)

    for cohort in ("a", "b"):
        parent = f"parent-{cohort}"
        child = f"child-{cohort}"
        for index in range(12):
            scheduler.enqueue(
                candidate(f"{child}-ready-{index}"),
                None,
                arm_key=f"{child}-ready-{index}",
                source_root=f"ready-root-{cohort}",
                family="opaque-protocol",
                coarse_key=f"{child}-ready-{index}",
                feature_key=feature(
                    family="opaque-protocol",
                    service_profiles=(parent, child),
                ),
            )

    with scheduler._lock:
        probation = {
            key: state.probation
            for key, state in scheduler._cohort_yield_states.items()
            if key in {"service:child-a", "service:child-b"}
        }
    assert probation == {
        "service:child-a": True,
        "service:child-b": True,
    }

    reservations = scheduler.reserve_handoffs(16)
    probation = [item for item in reservations if item.ticket.reward_probation]

    assert len(reservations) == 16
    assert len(probation) == 4
    assert {item.ticket.reward_cohort_key for item in probation} == {
        "service:child-a",
        "service:child-b",
    }


def test_single_slot_handoffs_consume_reward_across_root_drr() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.15,
        exploit_scan_share=0.25,
    )
    scheduler.enqueue(
        candidate("seed"),
        None,
        arm_key="seed-arm",
        source_root="reward-root",
        family="vless",
        coarse_key="seed-arm",
        feature_key=feature(),
    )
    seed = scheduler.drain(1)[0]
    scheduler.on_terminal(seed.dispatch_ticket, passed=True)
    scheduler.on_commit_complete(seed.dispatch_ticket, net_new=True)

    for index in range(24):
        scheduler.enqueue(
            candidate(f"ordinary-{index}"),
            None,
            arm_key=f"ordinary-{index}",
            source_root="ordinary-root",
            family="trojan",
            coarse_key=f"ordinary-{index}",
        )
    for index in range(8):
        scheduler.enqueue(
            candidate(f"reward-family-{index}"),
            None,
            arm_key="seed-arm",
            source_root="reward-root",
            family="vless",
            coarse_key="seed-arm",
            feature_key=feature(),
        )

    reasons: list[str] = []
    for _index in range(12):
        item = scheduler.drain(1)[0]
        reasons.append(item.selection_lane)
        scheduler.on_tcp_outcome(item.dispatch_ticket, responsive=False)

    metrics = scheduler.quick_metrics()
    assert "reward" in reasons[:4]
    assert metrics["reward_hit_count"] >= 1
    assert metrics["unknown_selected_total"] >= 2
    assert metrics["exploit_selected_total"] <= 3


def test_reward_two_waves_are_real_handoff_credit_not_two_feeder_calls() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.15,
        exploit_scan_share=0.25,
        reward_wave_limit=2,
        reward_candidate_limit=8,
    )
    scheduler.enqueue(
        candidate("seed"),
        None,
        arm_key="seed-arm",
        source_root="reward-root",
        family="vless",
        coarse_key="seed-arm",
        feature_key=feature(),
    )
    seed = scheduler.drain(1)[0]
    scheduler.on_terminal(seed.dispatch_ticket, passed=True)
    scheduler.on_commit_complete(seed.dispatch_ticket, net_new=True)

    for index in range(32):
        scheduler.enqueue(
            candidate(f"reward-{index}"),
            None,
            arm_key="seed-arm",
            source_root="reward-root",
            family="vless",
            coarse_key="seed-arm",
            feature_key=feature(),
        )
        scheduler.enqueue(
            candidate(f"ordinary-{index}"),
            None,
            arm_key=f"ordinary-{index}",
            source_root="ordinary-root",
            family="trojan",
            coarse_key=f"ordinary-{index}",
        )

    reasons: list[str] = []
    for _index in range(20):
        item = scheduler.drain(1)[0]
        reasons.append(item.selection_lane)
        scheduler.on_tcp_outcome(item.dispatch_ticket, responsive=False)

    reward_handoffs = reasons.count("reward")
    metrics = scheduler.quick_metrics()
    assert 3 <= reward_handoffs <= 5
    assert metrics["reward_handoff_credit"] > 0
    assert metrics["unknown_selected_total"] >= 3


def test_reward_quota_does_not_hide_family_from_ordinary_drr() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        reward_wave_limit=8,
        exploit_scan_share=0.25,
    )
    scheduler.enqueue(
        candidate("seed"),
        None,
        arm_key="rewarded",
        source_root="root-a",
        family="vless",
        coarse_key="rewarded",
    )
    seed = scheduler.drain(1)[0]
    scheduler.on_terminal(seed.dispatch_ticket, passed=True)
    scheduler.on_commit_complete(seed.dispatch_ticket, net_new=True)
    for index in range(8):
        scheduler.enqueue(
            candidate(f"same-family-{index}"),
            None,
            arm_key="rewarded",
            source_root="root-a",
            family="vless",
            coarse_key="rewarded",
        )

    first = scheduler.drain(1)[0]
    assert first.selection_lane == "reward"
    scheduler.on_tcp_outcome(first.dispatch_ticket, responsive=False)

    second = scheduler.drain(1)[0]
    assert second.source_root == "root-a"
    assert second.selection_lane != "reward"
    scheduler.on_tcp_outcome(second.dispatch_ticket, responsive=False)


def test_reward_uses_only_bounded_share_of_shared_handoff_window() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.25,
        work_conserving_parked_deep_limit=16,
    )
    scheduler.set_credit_envelope(16)
    for index in range(8):
        scheduler.enqueue(
            candidate(f"ordinary-seed-{index}"),
            None,
            arm_key=f"ordinary-seed-{index}",
            source_root="ordinary-root",
            family="trojan",
            coarse_key=f"ordinary-seed-{index}",
        )
        reservation = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(
            reservation.ticket,
            responsive=False,
        )
    scheduler.enqueue(
        candidate("seed"),
        None,
        arm_key="seed",
        source_root="reward-root",
        family="vless",
        coarse_key="seed",
    )
    seed = scheduler.reserve_handoffs(1)[0]
    scheduler.commit_handoff(seed)
    scheduler.on_terminal(seed.ticket, passed=True)
    scheduler.on_commit_complete(seed.ticket, net_new=True)

    for index in range(64):
        scheduler.enqueue(
            candidate(f"reward-{index}"),
            None,
            arm_key=f"reward-{index}",
            source_root="reward-root",
            family="vless",
            coarse_key=f"reward-{index}",
        )

    first_wave = scheduler.reserve_handoffs(8)
    scheduler.commit_handoffs(first_wave)
    assert len(first_wave) == 8
    assert all(
        reservation.candidate.source_root == "reward-root"
        for reservation in first_wave
    )
    assert sum(
        reservation.candidate.selection_lane == "reward"
        for reservation in first_wave
    ) <= math.ceil((9 + len(first_wave)) * 0.25)

    # Reward changes ordering only. It never creates handoff capacity beyond
    # the shared window and never exceeds the bounded exploit share.
    metrics = scheduler.quick_metrics()
    assert metrics["fast_credit_outstanding"] == 8
    assert metrics["root_family_fast_credit_limit"] == 0
    assert metrics["rewarded_root_family_fast_credit_limit"] == 0
    assert metrics["fast_credit_outstanding"] <= metrics["global_fast_credit_limit"]


def test_global_deep_saturation_keeps_bounded_fast_breadth_for_new_buckets() -> None:
    scheduler = legacy_candidate_scheduler(unknown_exploration_ratio=0.0)
    scheduler.set_credit_envelope(16)
    for index in range(128):
        scheduler.enqueue(
            candidate(f"early-{index}"),
            None,
            arm_key=f"early-arm-{index}",
            source_root=f"early-root-{index // 2}",
            family="vless",
            coarse_key=f"early-{index}",
        )

    for revision in range(1, 5):
        wave = scheduler.drain(32, control_revision=revision)
        assert len(wave) == 32
        for item in wave:
            scheduler.on_fast_yield(item.dispatch_ticket)

    assert scheduler.quick_metrics()["parked_deep_debt"] == 128
    for index in range(3):
        scheduler.enqueue(
            candidate(f"late-pass-{index}"),
            None,
            arm_key=f"late-pass-arm-{index}",
            source_root=f"late-root-{index}",
            family="trojan",
            coarse_key=f"late-pass-{index}",
        )

    selected = scheduler.drain(8, escape_budget=8, control_revision=5)
    assert len(selected) == 3
    assert all(item.selection_lane != "credit_escape" for item in selected)
    for item in selected:
        scheduler.on_terminal(item.dispatch_ticket, passed=True)
        scheduler.on_commit_complete(item.dispatch_ticket, net_new=True)
    assert {item.node.name for item in selected} == {
        "late-pass-0",
        "late-pass-1",
        "late-pass-2",
    }

    metrics = scheduler.quick_metrics()
    assert metrics["net_new_feedback_count"] == 3
    assert metrics["active_escape_lease_count"] == 0


def test_tcp_quality_is_only_a_bounded_within_family_tie_break() -> None:
    scheduler = legacy_candidate_scheduler(unknown_exploration_ratio=0.0)
    for arm_key in ("low", "high"):
        for index in range(9):
            scheduler.enqueue(
                candidate(f"{arm_key}-{index}"),
                None,
                arm_key=arm_key,
                source_root="root-a",
                family="vless",
                coarse_key=arm_key,
            )

    for _index in range(16):
        item = scheduler.drain(1)[0]
        responsive = item.arm_key == "high"
        scheduler.on_tcp_outcome(item.dispatch_ticket, responsive=responsive)
        if responsive:
            scheduler.on_terminal(item.dispatch_ticket, passed=False)

    selected = scheduler.drain(1)[0]
    assert selected.arm_key == "high"
    assert selected.selection_lane == "balanced"


def test_commit_is_a_defensive_final_credit_boundary() -> None:
    scheduler = legacy_candidate_scheduler(unknown_exploration_ratio=0.0)
    scheduler.enqueue(
        candidate("commit-boundary"),
        None,
        arm_key="commit-boundary",
        source_root="root-a",
        family="vless",
        coarse_key="commit-boundary",
    )
    item = scheduler.drain(1)[0]
    scheduler.on_fast_yield(item.dispatch_ticket)

    before = scheduler.quick_metrics()
    assert before["fast_credit_outstanding"] == 0
    assert before["parked_deep_debt"] == 1
    assert before["active_ticket_count"] == 1

    # A durable commit may race ahead of the terminal observer. The commit
    # boundary must still release every pre-TCP credit exactly once.
    scheduler.on_commit_complete(item.dispatch_ticket, net_new=True)
    scheduler.on_terminal(item.dispatch_ticket, passed=True)

    after = scheduler.quick_metrics()
    assert after["fast_credit_outstanding"] == 0
    assert after["parked_deep_debt"] == 0
    assert after["active_ticket_count"] == 0


def test_rejected_commit_without_terminal_releases_fast_credit() -> None:
    scheduler = legacy_candidate_scheduler(unknown_exploration_ratio=0.0)
    scheduler.enqueue(
        candidate("rejected"),
        None,
        arm_key="rejected",
        source_root="root-a",
        family="vless",
        coarse_key="rejected",
    )
    item = scheduler.drain(1)[0]

    scheduler.on_commit_complete(item.dispatch_ticket, net_new=False)

    metrics = scheduler.quick_metrics()
    assert metrics["fast_credit_outstanding"] == 0
    assert metrics["parked_deep_debt"] == 0
    assert metrics["active_ticket_count"] == 0
    assert metrics["credit_release_total"] == 1


def test_release_outstanding_clears_fast_and_parked_deep_debt() -> None:
    scheduler = legacy_candidate_scheduler(unknown_exploration_ratio=0.0)
    for index in range(4):
        scheduler.enqueue(
            candidate(f"sealed-{index}"),
            None,
            arm_key=f"sealed-{index}",
            source_root="root-a",
            family="vless" if index < 2 else "vmess",
            coarse_key=f"sealed-{index}",
        )
    selected = scheduler.drain(4)
    scheduler.on_fast_yield(selected[0].dispatch_ticket)
    scheduler.on_fast_yield(selected[1].dispatch_ticket)

    assert scheduler.release_outstanding() == 4
    metrics = scheduler.quick_metrics()
    assert metrics["fast_credit_outstanding"] == 0
    assert metrics["parked_deep_debt"] == 0
    assert metrics["active_ticket_count"] == 0
    assert metrics["credit_release_total"] == 4
    assert metrics["reward_service_pending_decision_count"] == 0


def test_coarse_arms_do_not_turn_deep_retention_into_a_fast_gate() -> None:
    scheduler = legacy_candidate_scheduler(unknown_exploration_ratio=0.0)
    scheduler.set_credit_envelope(16)
    for index in range(2400):
        root_index = index // 8
        scheduler.enqueue(
            candidate(f"bulk-{index}"),
            None,
            arm_key=f"bulk-{index}",
            source_root=f"root-{root_index}",
            family="vless",
            coarse_key=f"coarse-{index}",
        )

    for revision in range(1, 5):
        selected = scheduler.drain(32, control_revision=revision)
        assert len(selected) == 32
        for item in selected:
            scheduler.on_fast_yield(item.dispatch_ticket)

    metrics = scheduler.quick_metrics()
    assert metrics["parked_deep_debt"] == 128
    selected_after_window = []
    for revision in range(5, 9):
        wave = scheduler.drain(
            8,
            escape_budget=8,
            control_revision=revision,
        )
        assert len(wave) == 8
        assert all(item.selection_lane != "credit_escape" for item in wave)
        selected_after_window.extend(wave)
        for item in wave:
            scheduler.on_fast_yield(item.dispatch_ticket)

    metrics = scheduler.quick_metrics()
    assert metrics["parked_deep_debt"] == 160
    assert metrics["active_escape_lease_count"] == 0
    assert scheduler.drain(8, escape_budget=8, control_revision=9)

    scheduler.release_outstanding()
    metrics = scheduler.quick_metrics()
    assert metrics["fast_credit_outstanding"] == 0
    assert metrics["parked_deep_debt"] == 0
    assert metrics["active_escape_lease_count"] == 0


def test_released_fast_credit_ignores_deep_retention_diagnostic_limit() -> None:
    scheduler = legacy_candidate_scheduler(unknown_exploration_ratio=0.0)
    scheduler.set_credit_envelope(1)
    for index in range(24):
        scheduler.enqueue(
            candidate(f"reserve-deep-{index}"),
            None,
            arm_key=f"reserve-deep-{index}",
            source_root=f"root-{index}",
            family="vless",
            coarse_key=f"coarse-{index}",
        )

    for revision in range(1, 5):
        selected = scheduler.drain(2, control_revision=revision)
        assert len(selected) == 2
        for item in selected:
            scheduler.on_fast_yield(item.dispatch_ticket)

    metrics = scheduler.quick_metrics()
    assert metrics["parked_deep_debt"] == 8
    selected = scheduler.drain(2, control_revision=5)
    assert len(selected) == 2
    assert all(item.selection_lane != "credit_escape" for item in selected)
    for item in selected:
        scheduler.on_fast_yield(item.dispatch_ticket)
    metrics = scheduler.quick_metrics()
    assert metrics["parked_deep_debt"] == 10
    assert metrics["active_escape_lease_count"] == 0


@pytest.mark.parametrize("ending", ["terminal", "sealed_continuation"])
def test_async_leaf_probation_never_exceeds_four_unresolved_flights(ending: str) -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.25,
    )
    profiles = ("opaque-l0", "opaque-l1", "opaque-l2", "opaque-l3")

    # Two ordinary PASS observations open L3 probation. They are deliberately
    # duplicate commits: training may establish eligibility, but it is not the
    # end-to-end reward success signal.
    for index in range(2):
        scheduler.enqueue(
            candidate(f"training-{index}"),
            None,
            arm_key=f"training-{index}",
            source_root="opaque-root",
            family="opaque-family",
            coarse_key=f"training-{index}",
            feature_key=feature(
                family="opaque-family",
                service_profiles=profiles,
            ),
        )
        training = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(training)
        scheduler.on_tcp_outcome(training.ticket, responsive=True)
        scheduler.on_terminal(
            training.ticket,
            passed=True,
            outcome="pass",
            busy_worker_ms=500,
            stage="fast_probe",
        )
        scheduler.on_commit_complete(training.ticket, net_new=False)

    for index in range(96):
        scheduler.enqueue(
            candidate(f"async-{index}"),
            None,
            arm_key=f"async-{index}",
            source_root="opaque-root",
            family="opaque-family",
            coarse_key=f"async-{index}",
            feature_key=feature(
                family="opaque-family",
                service_profiles=profiles,
            ),
        )

    selected = []
    # Deliver TCP responses but intentionally withhold every FAST result. This
    # reproduces the production race that previously selected 75 candidates
    # from one 2/2 cohort before the first holdout result came back.
    for _epoch in range(6):
        wave = scheduler.reserve_handoffs(16)
        assert len(wave) == 16
        selected.extend(wave)
        for reservation in wave:
            scheduler.commit_handoff(reservation)
            scheduler.on_tcp_outcome(reservation.ticket, responsive=True)

        metrics = scheduler.quick_metrics()
        assert metrics["reward_probation_outstanding_count"] <= 4
        assert metrics["reward_service_outstanding_count"] <= 4
        assert metrics["reward_service_selected_count"] <= 4

    metrics = scheduler.quick_metrics()
    assert metrics["reward_probation_outstanding_count"] == 4
    assert metrics["reward_probation_outstanding_high_water"] == 4
    assert metrics["reward_service_outstanding_high_water"] == 4

    for reservation in selected:
        if ending == "sealed_continuation":
            scheduler.on_fast_yield(reservation.ticket, right_censored=True)
        else:
            scheduler.on_terminal(
                reservation.ticket,
                passed=False,
                outcome="candidate_fail",
                busy_worker_ms=500,
                stage="fast_probe",
            )
    if ending == "sealed_continuation":
        assert scheduler.release_outstanding() == len(selected)

    metrics = scheduler.quick_metrics()
    assert metrics["reward_service_outstanding_count"] == 0
    assert metrics["reward_probation_outstanding_count"] == 0
    assert metrics["active_ticket_count"] == 0
    assert metrics["parked_deep_debt"] == 0
    assert metrics["reward_service_selected_count"] == metrics["reward_service_resolved_count"]
    if ending == "terminal":
        assert metrics["reward_service_probation_cohort_count"] == 0
        assert metrics["scheduling_outcome_counts"]["validation_fail"] >= 4
    else:
        assert metrics["scheduling_outcome_counts"].get("validation_fail", 0) == 0
    assert scheduler.release_outstanding() == 0


def test_exact_grant_redeems_after_unrelated_traffic_with_four_flight_cap() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.25,
    )
    exact_feature = feature(
        family="opaque-family",
        service_profiles=("l0", "l1", "l2", "l3-exact"),
    )
    scheduler.enqueue(
        candidate("seed"),
        None,
        arm_key="exact-winner",
        source_root="opaque-root",
        family="opaque-family",
        coarse_key="seed",
        feature_key=exact_feature,
    )
    seed = scheduler.reserve_handoffs(1)[0]
    scheduler.commit_handoff(seed)
    scheduler.on_tcp_outcome(seed.ticket, responsive=True)
    scheduler.on_terminal(
        seed.ticket,
        passed=True,
        outcome="pass",
        busy_worker_ms=500,
        stage="fast_probe",
    )
    scheduler.on_commit_complete(seed.ticket, net_new=True)

    for index in range(64):
        scheduler.enqueue(
            candidate(f"unrelated-{index}"),
            None,
            arm_key=f"unrelated-{index}",
            source_root="opaque-root",
            family="opaque-family",
            coarse_key=f"unrelated-{index}",
            feature_key=feature(
                family="opaque-family",
                service_profiles=("other-l0", f"other-{index}"),
            ),
        )
        unrelated = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(unrelated)
        scheduler.on_tcp_outcome(unrelated.ticket, responsive=False)

    with scheduler._lock:
        grant = scheduler._family_rewards[("opaque-root", "opaque-family")]
        assert grant.exact_handoffs_remaining == 16

    for index in range(32):
        scheduler.enqueue(
            candidate(f"exact-{index}"),
            None,
            arm_key="exact-winner",
            source_root="opaque-root",
            family="opaque-family",
            coarse_key=f"exact-{index}",
            feature_key=exact_feature,
        )

    unresolved = []
    first_wave = scheduler.reserve_handoffs(16)
    assert len(first_wave) == 16
    for reservation in first_wave:
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
        unresolved.append(reservation)

    first_metrics = scheduler.quick_metrics()
    assert first_metrics["reward_exact_selected_count"] == 4
    assert first_metrics["reward_exact_outstanding_count"] == 4
    assert first_metrics["reward_exact_outstanding_high_water"] == 4

    second_wave = scheduler.reserve_handoffs(16)
    assert len(second_wave) == 16
    for reservation in second_wave:
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
        unresolved.append(reservation)

    blocked_metrics = scheduler.quick_metrics()
    assert blocked_metrics["reward_exact_selected_count"] == 4
    assert blocked_metrics["reward_exact_outstanding_count"] == 4

    for reservation in unresolved:
        scheduler.on_terminal(
            reservation.ticket,
            passed=False,
            outcome="candidate_fail",
            busy_worker_ms=500,
            stage="fast_probe",
        )

    final_metrics = scheduler.quick_metrics()
    assert final_metrics["reward_exact_outstanding_count"] == 0
    assert final_metrics["reward_exact_resolved_count"] == 4
    assert final_metrics["active_ticket_count"] == 0


def test_origin_structural_leaf_redeems_without_rewarding_global_siblings() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.25,
    )
    global_profiles = ("global-l0", "global-l1", "global-l2", "global-l3")

    # Two ordinary confirmations qualify only the private origin leaf.  The
    # globally identical L3 structure remains discovery evidence.
    for index in range(2):
        scheduler.enqueue(
            candidate(f"origin-training-{index}"),
            None,
            arm_key=f"training-arm-{index}",
            source_root="opaque-root",
            family="opaque-family",
            coarse_key=f"training-{index}",
            feature_key=feature(
                family="opaque-family",
                service_profiles=global_profiles,
            ),
            origin_service_profile="origin-unit-a",
        )
        training = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(training)
        scheduler.on_tcp_outcome(training.ticket, responsive=True)
        scheduler.on_terminal(
            training.ticket,
            passed=True,
            outcome="pass",
            busy_worker_ms=500,
            stage="fast_probe",
        )
        scheduler.on_commit_complete(training.ticket, net_new=True)

    with scheduler._lock:
        origin_state = scheduler._cohort_yield_states[
            "service:origin-unit-a"
        ]
        global_state = scheduler._cohort_yield_states["service:global-l3"]
        assert origin_state.eligibility is (
            Tier3ServiceCohortEligibility.LEAF_PROBATION
        )
        assert global_state.eligibility is (
            Tier3ServiceCohortEligibility.OBSERVE
        )

    for index in range(64):
        for unit in ("a", "b"):
            scheduler.enqueue(
                candidate(f"inventory-{unit}-{index}"),
                None,
                arm_key=f"inventory-{unit}-{index}",
                source_root="opaque-root",
                family="opaque-family",
                coarse_key=f"inventory-{unit}-{index}",
                feature_key=feature(
                    family="opaque-family",
                    service_profiles=global_profiles,
                ),
                origin_service_profile=f"origin-unit-{unit}",
            )

    first_wave = scheduler.reserve_handoffs(16)
    assert len(first_wave) == 16
    service = [
        item for item in first_wave if item.ticket.reward_scope == "service"
    ]
    assert 1 <= len(service) <= 4
    assert {
        item.ticket.reward_cohort_key for item in service
    } == {"service:origin-unit-a"}
    assert all("inventory-a-" in item.candidate.node.name for item in service)

    reward_passes = 0
    for item in first_wave:
        scheduler.commit_handoff(item)
        scheduler.on_tcp_outcome(item.ticket, responsive=True)
        passed = item.ticket.reward_scope == "service" and reward_passes < 2
        scheduler.on_terminal(
            item.ticket,
            passed=passed,
            outcome="pass" if passed else "candidate_fail",
            busy_worker_ms=500,
            stage="fast_probe",
        )
        if passed:
            reward_passes += 1
            scheduler.on_commit_complete(item.ticket, net_new=True)

    assert reward_passes == 2
    with scheduler._lock:
        assert scheduler._cohort_yield_states[
            "service:origin-unit-a"
        ].eligibility is Tier3ServiceCohortEligibility.PROVEN_50
        assert scheduler._cohort_yield_states[
            "service:origin-unit-b"
        ].eligibility not in {
            Tier3ServiceCohortEligibility.LEAF_PROBATION,
            Tier3ServiceCohortEligibility.PROVEN_50,
            Tier3ServiceCohortEligibility.PROVEN_75,
        }

    second_wave = scheduler.reserve_handoffs(16)
    second_service = [
        item for item in second_wave if item.ticket.reward_scope == "service"
    ]
    assert second_service
    assert all(
        item.ticket.reward_cohort_key == "service:origin-unit-a"
        and "inventory-a-" in item.candidate.node.name
        for item in second_service
    )
    metrics = scheduler.quick_metrics()
    assert metrics["reward_origin_structural_cohort_count"] >= 2
    assert metrics["reward_origin_structural_eligible_count"] == 1
    assert metrics["reward_origin_structural_selected_count"] >= len(service)
    assert metrics["reward_origin_structural_pass_count"] == 2
    assert metrics["reward_origin_structural_net_new_count"] == 2
    assert metrics["reward_origin_structural_inventory_count"] > 0
    scheduler.release_outstanding()


def test_origin_structural_metrics_do_not_depend_on_unfolded_hierarchy_depth() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.0,
        exploit_scan_share=0.25,
    )
    for index in range(2):
        scheduler.enqueue(
            candidate(f"collapsed-origin-{index}"),
            None,
            arm_key=f"collapsed-origin-{index}",
            source_root="opaque-root",
            family="opaque-family",
            coarse_key=f"collapsed-origin-{index}",
            feature_key=feature(
                family="opaque-family",
                service_profiles=("collapsed-global",),
            ),
            origin_service_profile="collapsed-origin-leaf",
        )
        reservation = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
        scheduler.on_terminal(
            reservation.ticket,
            passed=True,
            outcome="pass",
            busy_worker_ms=500,
            stage="fast_probe",
        )
        scheduler.on_commit_complete(reservation.ticket, net_new=True)

    with scheduler._lock:
        state = scheduler._cohort_yield_states[
            "service:collapsed-origin-leaf"
        ]
        assert state.hierarchy_level < 4
    metrics = scheduler.quick_metrics()
    assert metrics["reward_origin_structural_cohort_count"] == 1
    assert metrics["reward_origin_structural_eligible_count"] == 1
    assert metrics["fast_observation_sample_count"] == 2
    assert metrics["fast_observation_exposure_worker_seconds"] == 1.0
    scheduler.release_outstanding()


def test_origin_structural_tail_replay_reaches_target_without_global_sibling_fallback() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.15,
        exploit_scan_share=0.25,
    )
    global_profiles = ("shared-l0", "shared-l1", "shared-l2", "shared-l3")
    winning_names = {f"origin-good-{index}" for index in range(25)}

    def finish(reservation, *, passed: bool) -> None:
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
        scheduler.on_terminal(
            reservation.ticket,
            passed=passed,
            outcome="pass" if passed else "candidate_fail",
            busy_worker_ms=500,
            stage="fast_probe",
        )
        scheduler.on_commit_complete(reservation.ticket, net_new=passed)

    committed = 0
    for index in range(2):
        scheduler.enqueue(
            candidate(f"origin-good-{index}"),
            None,
            arm_key=f"origin-good-{index}",
            source_root="opaque-root",
            family="opaque-family",
            coarse_key=f"origin-good-{index}",
            feature_key=feature(
                family="opaque-family",
                service_profiles=global_profiles,
            ),
            origin_service_profile="origin-structural-good",
        )
        training = scheduler.reserve_handoffs(1)[0]
        finish(training, passed=True)
        committed += 1

    # The low-yield sibling has the same global L0-L3 structure. Only the
    # source-local structural leaf may receive production reward.
    for index in range(512):
        scheduler.enqueue(
            candidate(f"sibling-low-{index}"),
            None,
            arm_key=f"sibling-low-{index}",
            source_root="opaque-root",
            family="opaque-family",
            coarse_key=f"sibling-low-{index}",
            feature_key=feature(
                family="opaque-family",
                service_profiles=global_profiles,
            ),
            origin_service_profile="origin-structural-sibling",
        )
        if index < 23:
            winner_index = index + 2
            scheduler.enqueue(
                candidate(f"origin-good-{winner_index}"),
                None,
                arm_key=f"origin-good-{winner_index}",
                source_root="opaque-root",
                family="opaque-family",
                coarse_key=f"origin-good-{winner_index}",
                feature_key=feature(
                    family="opaque-family",
                    service_profiles=global_profiles,
                ),
                origin_service_profile="origin-structural-good",
            )

    handoff_total = 0
    while committed < 25 and handoff_total < 96:
        wave = scheduler.reserve_handoffs(16)
        assert wave
        for reservation in wave:
            name = reservation.candidate.node.name
            passed = name in winning_names
            finish(reservation, passed=passed)
            handoff_total += 1
            if passed:
                committed += 1

    assert committed == 25
    assert handoff_total <= 64
    metrics = scheduler.quick_metrics()
    assert metrics["reward_origin_structural_net_new_count"] >= 2
    assert metrics["reward_origin_structural_selected_count"] > 0
    assert all(
        item["cohort_id"] != "origin-structural-sibling"
        for item in metrics["reward_service_recent_decisions"]
    )
    scheduler.release_outstanding()


def test_origin_structural_tail_replay_survives_low_tcp_responsive_supply() -> None:
    scheduler = legacy_candidate_scheduler(
        unknown_exploration_ratio=0.15,
        exploit_scan_share=0.25,
    )
    global_profiles = ("shared-l0", "shared-l1", "shared-l2", "shared-l3")

    def complete_fast(reservation, *, passed: bool) -> None:
        scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
        scheduler.on_terminal(
            reservation.ticket,
            passed=passed,
            outcome="pass" if passed else "candidate_fail",
            busy_worker_ms=500,
            stage="fast_probe",
        )
        scheduler.on_commit_complete(reservation.ticket, net_new=passed)

    committed = 0
    for index in range(2):
        scheduler.enqueue(
            candidate(f"responsive-good-{index}"),
            None,
            arm_key=f"responsive-good-{index}",
            source_root="opaque-root",
            family="opaque-family",
            coarse_key=f"responsive-good-{index}",
            feature_key=feature(
                family="opaque-family",
                service_profiles=global_profiles,
            ),
            origin_service_profile="responsive-origin-good",
        )
        training = scheduler.reserve_handoffs(1)[0]
        scheduler.commit_handoff(training)
        complete_fast(training, passed=True)
        committed += 1

    # Both origins share the same global L0-L3 structure. The useful origin
    # has only one TCP-responsive candidate in four; the sibling has the same
    # transport response rate but never passes the real FAST probe.
    for index in range(1024):
        scheduler.enqueue(
            candidate(f"responsive-sibling-{index}"),
            None,
            arm_key=f"responsive-sibling-{index}",
            source_root="opaque-root",
            family="opaque-family",
            coarse_key=f"responsive-sibling-{index}",
            feature_key=feature(
                family="opaque-family",
                service_profiles=global_profiles,
            ),
            origin_service_profile="responsive-origin-sibling",
        )
        if index < 160:
            scheduler.enqueue(
                candidate(f"responsive-good-{index + 2}"),
                None,
                arm_key=f"responsive-good-{index + 2}",
                source_root="opaque-root",
                family="opaque-family",
                coarse_key=f"responsive-good-{index + 2}",
                feature_key=feature(
                    family="opaque-family",
                    service_profiles=global_profiles,
                ),
                origin_service_profile="responsive-origin-good",
            )

    handoff_total = 0
    target_handoff_total: int | None = None
    while committed < 25 and handoff_total < 384:
        wave = scheduler.reserve_handoffs(16)
        assert wave
        for reservation in wave:
            scheduler.commit_handoff(reservation)
            handoff_total += 1
            name = reservation.candidate.node.name
            if name.startswith("responsive-good-"):
                candidate_index = int(name.rsplit("-", 1)[-1])
                responsive = candidate_index % 4 == 0
                if responsive:
                    complete_fast(reservation, passed=True)
                else:
                    scheduler.on_tcp_outcome(
                        reservation.ticket,
                        responsive=False,
                    )
            else:
                candidate_index = int(name.rsplit("-", 1)[-1])
                responsive = candidate_index % 4 == 0
                if responsive:
                    complete_fast(reservation, passed=False)
                else:
                    scheduler.on_tcp_outcome(
                        reservation.ticket,
                        responsive=False,
                    )
            if responsive and name.startswith("responsive-good-"):
                committed += 1
                if committed >= 25 and target_handoff_total is None:
                    target_handoff_total = handoff_total

    assert committed >= 25
    assert target_handoff_total is not None
    assert target_handoff_total <= 320
    metrics = scheduler.quick_metrics()
    assert metrics["reward_origin_structural_net_new_count"] >= 2
    assert metrics["reward_origin_structural_selected_count"] > 0
    assert all(
        item["cohort_id"] != "responsive-origin-sibling"
        for item in metrics["reward_service_recent_decisions"]
    )
    scheduler.release_outstanding()
