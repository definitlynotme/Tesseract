from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path

import pytest

from models import Node
from tier3_candidate_scheduler import (
    Tier3CandidateLookaheadEvidence,
    Tier3CandidateScheduler,
)


def candidate(name: str, protocol: str = "vless") -> Node:
    return Node(
        name=name,
        protocol=protocol,
        address=f"{name}.example.com",
        port=443,
        tier="tier3",
    )


def test_frozen_exact_observation_characterization_is_sanitized() -> None:
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "tier3_exact_observation_baseline_v1.json"
        ).read_text(encoding="utf-8")
    )
    assert fixture["stable_observation_count"] == 244
    assert fixture["tcp_responsive_count"] == 106
    assert fixture["validation_pass_count"] == 26
    assert fixture["all_committed_nodes_exactly_observed_positive"] is True
    serialized = json.dumps(fixture).lower()
    assert "http://" not in serialized
    assert "https://" not in serialized
    assert "uuid" not in serialized
    assert "password" not in serialized


def enqueue(
    scheduler: Tier3CandidateScheduler,
    name: str,
    *,
    root: str,
    family: str = "vless",
    coarse: str | None = None,
) -> None:
    scheduler.enqueue(
        candidate(name, family),
        None,
        arm_key=f"{root}|{family}|{coarse or name}",
        source_root=root,
        family=family,
        coarse_key=coarse or name,
    )


def test_production_structural_lane_requires_current_exact_l3_evidence() -> None:
    scheduler = Tier3CandidateScheduler(
        unknown_exploration_ratio=0.15,
        reward_candidate_limit=12,
    )
    for index in range(32):
        enqueue(
            scheduler,
            f"candidate-{index}",
            root=f"root-{index % 4}",
            coarse="reward-arm" if index < 16 else f"ordinary-{index}",
        )

    scheduler.record_feedback(
        "root-0|vless|reward-arm",
        passed=True,
    )
    reservations = scheduler.reserve_handoffs(16)

    assert len(reservations) == 16
    assert {
        reservation.ticket.selection_lane for reservation in reservations
    } <= {"coverage", "exploration", "balanced"}
    metrics = scheduler.quick_metrics()
    assert metrics["production_structural_routing_enabled"] is True
    assert metrics["production_structural_routing_mode"] == (
        "exact_l3_with_direct_child_discovery_and_similar_shadow"
    )
    assert metrics["lookahead_mode"] == "validated_observation_cursor"
    assert metrics["reward_exact_hit_count"] == 0
    assert metrics["reward_service_hit_count"] == 0


def test_visibility_cursor_finds_late_stable_candidates_without_full_scan() -> None:
    def evidence(item) -> Tier3CandidateLookaheadEvidence:
        return Tier3CandidateLookaheadEvidence(
            stable_tcp_positive=item.node.name.startswith("stable-late"),
            observed=item.node.name.startswith("observed-dead"),
            pre_tcp_score=0.2 if item.node.name.startswith("stable-late") else 0.0,
        )

    scheduler = Tier3CandidateScheduler(
        max_active_arms_per_family=8,
        lookahead_evidence_provider=evidence,
    )
    for index in range(13_984):
        prefix = "observed-dead" if index % 11 == 0 else "unknown"
        enqueue(
            scheduler,
            f"{prefix}-{index}",
            root=f"opaque-root-{index % 17}",
            family=f"family-{index % 5}",
            coarse=f"coarse-{index}",
        )
    for index in range(16):
        enqueue(
            scheduler,
            f"stable-late-{index}",
            root=f"opaque-root-{index % 17}",
            family=f"family-{index % 5}",
            coarse=f"late-coarse-{index}",
        )

    reservations = scheduler.reserve_handoffs(16)
    before_start = scheduler.quick_metrics()

    assert len(reservations) == 16
    assert sum(
        reservation.candidate.node.name.startswith("stable-late")
        for reservation in reservations
    ) == 12
    assert sum(
        reservation.ticket.visibility_class == "unknown"
        for reservation in reservations
    ) >= 3
    assert before_start["selected_total"] == 0
    assert before_start["lookahead_examined_total"] <= 8 * len(reservations)
    assert before_start["lookahead_max_examined_per_reservation"] <= 8 * len(
        reservations
    )
    assert before_start["lookahead_exact_positive_count"] == 4
    assert before_start["lookahead_unknown_count"] > 0
    assert before_start["lookahead_hit_total"] == 16

    committed = scheduler.commit_handoffs(reservations[:8])
    for reservation in reservations[8:]:
        assert scheduler.cancel_handoff(reservation, requeue=True)
    after_start = scheduler.quick_metrics()

    assert len(committed) == 8
    assert after_start["selected_total"] == 8
    assert after_start["cancelled_reservation_total"] == 8
    assert after_start["tcp_outcome_count"] == 0


def test_cancelled_reservation_restores_original_fifo_sequence() -> None:
    scheduler = Tier3CandidateScheduler(unknown_exploration_ratio=0.0)
    enqueue(scheduler, "first", root="root", coarse="same")
    enqueue(scheduler, "second", root="root", coarse="same")

    first_reservation = scheduler.reserve_handoffs(1)[0]
    assert first_reservation.candidate.node.name == "first"
    original_sequence = first_reservation.candidate.lookahead_sequence
    assert scheduler.quick_metrics()["selected_total"] == 0

    assert scheduler.cancel_handoff(first_reservation, requeue=True)
    second_reservation = scheduler.reserve_handoffs(1)[0]

    assert second_reservation.candidate.node.name == "first"
    assert second_reservation.candidate.lookahead_sequence == original_sequence
    assert scheduler.quick_metrics()["selected_total"] == 0


def test_zero_budget_does_not_even_scan_the_lookahead_index() -> None:
    scheduler = Tier3CandidateScheduler()
    for index in range(128):
        enqueue(
            scheduler,
            f"zero-{index}",
            root=f"root-{index % 5}",
            coarse=f"coarse-{index}",
        )

    before = scheduler.quick_metrics()
    assert scheduler.reserve_handoffs(0) == []
    after = scheduler.quick_metrics()

    assert before["lookahead_examined_total"] == 0
    assert after["lookahead_examined_total"] == 0
    assert after["lookahead_hit_total"] == 0
    assert after["selected_total"] == 0


@pytest.mark.parametrize("root_count", [1, 2, 5, 17])
def test_root_renaming_does_not_change_normalized_lookahead_trace(
    root_count: int,
) -> None:
    def run(root_name: Callable[[int], str]) -> list[tuple[int, str, int]]:
        scheduler = Tier3CandidateScheduler()
        for index in range(160):
            root_index = index % root_count
            enqueue(
                scheduler,
                f"node-{index}",
                root=root_name(root_index),
                family=f"family-{index % 3}",
                coarse=f"coarse-{index % 29}",
            )
        reservations = scheduler.reserve_handoffs(32)
        return [
            (
                int(reservation.candidate.node.name.rsplit("-", 1)[1])
                % root_count,
                reservation.candidate.family,
                int(reservation.candidate.node.name.rsplit("-", 1)[1]),
            )
            for reservation in reservations
        ]

    baseline = run(lambda index: f"root-{index}")
    renamed = run(lambda index: f"opaque-{(index * 7 + 3) % 101}")

    assert renamed == baseline


def test_global_lookahead_covers_new_root_family_before_repeating_bucket() -> None:
    scheduler = Tier3CandidateScheduler()
    for root_index in range(5):
        for family_index in range(2):
            for candidate_index in range(8):
                enqueue(
                    scheduler,
                    f"r{root_index}-f{family_index}-{candidate_index}",
                    root=f"root-{root_index}",
                    family=f"family-{family_index}",
                    coarse=f"coarse-{candidate_index}",
                )

    first_epoch = scheduler.reserve_handoffs(16)
    covered = {
        (
            reservation.candidate.source_root,
            reservation.candidate.family,
        )
        for reservation in first_epoch
        if reservation.ticket.coverage_selected
    }

    assert len(covered) == 4
    assert sum(
        1 for reservation in first_epoch if reservation.ticket.unknown_exploration
    ) >= 3
    assert scheduler.quick_metrics()["selected_total"] == 0


def test_same_inventory_ab_exposes_late_quality_with_bounded_exact_lane() -> None:
    def evidence(item) -> Tier3CandidateLookaheadEvidence:
        return Tier3CandidateLookaheadEvidence(
            stable_tcp_positive=item.node.name.startswith("high-yield"),
            observed=False,
            pre_tcp_score=0.2 if item.node.name.startswith("high-yield") else 0.0,
        )

    legacy = Tier3CandidateScheduler(
        max_active_arms_per_family=16,
        unknown_exploration_ratio=0.0,
        lookahead_evidence_provider=evidence,
        _enable_legacy_structural_routing_for_test=True,
    )
    lookahead = Tier3CandidateScheduler(
        max_active_arms_per_family=16,
        unknown_exploration_ratio=0.0,
        lookahead_evidence_provider=evidence,
    )
    for index in range(512):
        for scheduler in (legacy, lookahead):
            enqueue(
                scheduler,
                f"ordinary-{index}",
                root=f"root-{index % 5}",
                family=f"family-{index % 3}",
                coarse=f"ordinary-coarse-{index}",
            )
    for index in range(25):
        for scheduler in (legacy, lookahead):
            enqueue(
                scheduler,
                f"high-yield-{index}",
                root=f"root-{index % 5}",
                family=f"family-{index % 3}",
                coarse=f"high-coarse-{index}",
            )

    legacy_names = {item.node.name for item in legacy.drain(64)}
    lookahead_names: set[str] = set()
    while len(lookahead_names) < 64:
        reservations = lookahead.reserve_handoffs(16)
        assert reservations
        for reservation in reservations:
            lookahead_names.add(reservation.candidate.node.name)
            assert lookahead.commit_handoff(reservation)
            lookahead.on_tcp_outcome(reservation.ticket, responsive=False)
            lookahead.on_terminal(reservation.ticket, outcome="tcp_dead")

    assert not any(name.startswith("high-yield") for name in legacy_names)
    assert sum(name.startswith("high-yield") for name in lookahead_names) == 25
    metrics = lookahead.quick_metrics()
    assert metrics["production_structural_routing_enabled"] is True
    assert metrics["production_structural_routing_mode"] == (
        "exact_l3_with_direct_child_discovery_and_similar_shadow"
    )


def test_current_tcp_result_never_promotes_sibling_candidates_in_same_arm() -> None:
    scheduler = Tier3CandidateScheduler(unknown_exploration_ratio=0.0)
    enqueue(scheduler, "first", root="root", coarse="shared")
    enqueue(scheduler, "second", root="root", coarse="shared")

    first = scheduler.reserve_handoffs(1)[0]
    assert scheduler.commit_handoff(first)
    scheduler.on_tcp_outcome(first.ticket, responsive=True)
    scheduler.on_terminal(first.ticket, outcome="candidate_fail")

    second = scheduler.reserve_handoffs(1)[0]
    assert second.ticket.visibility_class == "unknown"
    assert second.ticket.unknown_exploration is True


def test_exact_visibility_metrics_follow_start_fast_pass_and_commit() -> None:
    scheduler = Tier3CandidateScheduler(
        lookahead_evidence_provider=lambda _item: Tier3CandidateLookaheadEvidence(
            stable_tcp_positive=True,
            observed=True,
        )
    )
    enqueue(scheduler, "known-good", root="root", coarse="known")

    reservation = scheduler.reserve_handoffs(1)[0]
    assert reservation.ticket.visibility_class == "validated_positive"
    assert scheduler.commit_handoff(reservation)
    scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
    scheduler.on_terminal(
        reservation.ticket,
        passed=True,
        outcome="pass",
        busy_worker_ms=500,
        stage="fast_probe",
    )
    scheduler.on_commit_complete(reservation.ticket, net_new=True)

    metrics = scheduler.quick_metrics()
    assert metrics["visibility_exact_positive_reserved_total"] == 1
    assert metrics["visibility_exact_positive_start_total"] == 1
    assert metrics["visibility_exact_positive_responsive_total"] == 1
    assert metrics["visibility_exact_positive_fast_pass_total"] == 1
    assert metrics["visibility_exact_positive_net_new_total"] == 1
    assert metrics["active_ticket_count"] == 0


def test_tcp_only_inventory_never_consumes_validated_replay_quota() -> None:
    def evidence(item) -> Tier3CandidateLookaheadEvidence:
        name = item.node.name
        return Tier3CandidateLookaheadEvidence(
            validated_positive=name.startswith("validated"),
            tcp_only=name.startswith("tcp-only"),
        )

    scheduler = Tier3CandidateScheduler(
        unknown_exploration_ratio=0.15,
        lookahead_evidence_provider=evidence,
    )
    for index in range(512):
        enqueue(
            scheduler,
            f"tcp-only-{index}",
            root=f"root-{index % 17}",
            family=f"family-{index % 5}",
            coarse=f"tcp-{index}",
        )
    for index in range(32):
        enqueue(
            scheduler,
            f"unknown-{index}",
            root=f"root-{index % 17}",
            family=f"family-{index % 5}",
            coarse=f"unknown-{index}",
        )
    for index in range(16):
        enqueue(
            scheduler,
            f"validated-{index}",
            root=f"root-{index % 17}",
            family=f"family-{index % 5}",
            coarse=f"validated-{index}",
        )

    first = scheduler.reserve_handoffs(16)
    assert sum(
        item.ticket.visibility_class == "validated_positive" for item in first
    ) == 12
    assert sum(item.ticket.visibility_class == "unknown" for item in first) >= 3
    assert sum(item.ticket.visibility_class == "tcp_only" for item in first) <= 1
    metrics = scheduler.quick_metrics()
    assert metrics["lookahead_validated_positive_count"] == 4
    assert metrics["lookahead_tcp_only_count"] >= 511
    assert metrics["visibility_exact_positive_reserved_total"] == metrics[
        "visibility_validated_positive_reserved_total"
    ]


def test_validated_replay_inventory_is_redeemed_across_successive_epochs() -> None:
    def evidence(item) -> Tier3CandidateLookaheadEvidence:
        name = item.node.name
        return Tier3CandidateLookaheadEvidence(
            validated_positive=name.startswith("validated"),
            tcp_only=name.startswith("tcp-only"),
        )

    scheduler = Tier3CandidateScheduler(
        unknown_exploration_ratio=0.15,
        lookahead_evidence_provider=evidence,
    )
    for index in range(1000):
        enqueue(
            scheduler,
            f"tcp-only-{index}",
            root=f"root-{index % 17}",
            family=f"family-{index % 5}",
            coarse=f"tcp-{index}",
        )
    for index in range(64):
        enqueue(
            scheduler,
            f"unknown-{index}",
            root=f"root-{index % 17}",
            family=f"family-{index % 5}",
            coarse=f"unknown-{index}",
        )
    for index in range(25):
        enqueue(
            scheduler,
            f"validated-{index}",
            root=f"root-{index % 17}",
            family=f"family-{index % 5}",
            coarse=f"validated-{index}",
        )

    validated_by_epoch: list[int] = []
    for _epoch in range(3):
        reservations = scheduler.reserve_handoffs(16)
        validated_by_epoch.append(
            sum(
                item.ticket.visibility_class == "validated_positive"
                for item in reservations
            )
        )
        for reservation in reservations:
            assert scheduler.commit_handoff(reservation)
            scheduler.on_tcp_outcome(reservation.ticket, responsive=False)
            scheduler.on_terminal(reservation.ticket, outcome="tcp_dead")

    assert validated_by_epoch == [12, 12, 1]


def test_polluted_cache_is_not_twenty_percent_slower_than_cache_disabled() -> None:
    definitions = [
        *(f"fresh-good-{index}" for index in range(8)),
        *(f"unknown-good-{index}" for index in range(25)),
        *(f"historical-dead-{index}" for index in range(8)),
        *(f"tcp-only-dead-{index}" for index in range(128)),
        *(f"nonpositive-dead-{index}" for index in range(16)),
    ]

    def run(*, cache_enabled: bool) -> tuple[int, dict[str, object]]:
        def evidence(item) -> Tier3CandidateLookaheadEvidence:
            if not cache_enabled:
                return Tier3CandidateLookaheadEvidence()
            name = item.node.name
            return Tier3CandidateLookaheadEvidence(
                validated_positive=name.startswith("fresh-good-"),
                validated_reprobe=name.startswith("historical-dead-"),
                tcp_only=name.startswith("tcp-only-dead-"),
                observed_nonpositive=name.startswith("nonpositive-dead-"),
            )

        scheduler = Tier3CandidateScheduler(
            unknown_exploration_ratio=0.15,
            lookahead_evidence_provider=evidence,
        )
        for index, name in enumerate(definitions):
            enqueue(
                scheduler,
                name,
                root=f"opaque-root-{index % 8}",
                family="opaque-family",
                coarse=f"coarse-{index}",
            )

        start_acked = 0
        net_new = 0
        while net_new < 25:
            reservations = scheduler.reserve_handoffs(16)
            assert reservations, scheduler.quick_metrics()
            for reservation in reservations:
                assert scheduler.commit_handoff(reservation)
                start_acked += 1
                name = reservation.candidate.node.name
                passed = bool(
                    name.startswith("fresh-good-")
                    or name.startswith("unknown-good-")
                )
                scheduler.on_tcp_outcome(
                    reservation.ticket,
                    responsive=passed,
                )
                if passed:
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
                    net_new += 1
                else:
                    scheduler.on_terminal(
                        reservation.ticket,
                        outcome="tcp_dead",
                    )
        metrics = scheduler.quick_metrics()
        scheduler.release_outstanding()
        return start_acked, metrics

    cold_start_acked, _cold_metrics = run(cache_enabled=False)
    mixed_start_acked, mixed_metrics = run(cache_enabled=True)

    assert mixed_start_acked <= math.ceil(cold_start_acked * 1.2), (
        cold_start_acked,
        mixed_start_acked,
        {
            key: mixed_metrics[key]
            for key in (
                "visibility_validated_positive_start_total",
                "visibility_validated_reprobe_start_total",
                "visibility_unknown_start_total",
                "visibility_tcp_only_start_total",
                "visibility_observed_nonpositive_start_total",
                "fresh_replay_adaptive_quota",
                "historical_reprobe_adaptive_quota",
            )
        },
    )
    assert mixed_metrics["historical_reprobe_adaptive_quota"] <= 3
    assert mixed_metrics["visibility_tcp_only_start_total"] <= math.ceil(
        mixed_start_acked / 16
    ) * 4
    assert mixed_metrics["visibility_observed_nonpositive_start_total"] == 0


def test_thousand_production_transactions_release_all_lookahead_state() -> None:
    scheduler = Tier3CandidateScheduler(
        max_active_arms_per_family=16,
        unknown_exploration_ratio=0.15,
    )
    for index in range(1000):
        enqueue(
            scheduler,
            f"mixed-{index}",
            root=f"opaque-root-{index % 17}",
            family=f"family-{index % 5}",
            coarse=f"coarse-{index % 97}",
        )

    handled = 0
    while scheduler.size() > 0:
        reservations = scheduler.reserve_handoffs(32)
        assert reservations, scheduler.quick_metrics()
        for reservation in reservations:
            sequence = handled
            handled += 1
            if sequence % 11 == 0:
                assert scheduler.cancel_handoff(reservation, requeue=False)
                continue
            assert scheduler.commit_handoff(reservation)
            ticket = reservation.ticket
            if sequence % 7 == 0:
                scheduler.on_tcp_outcome(ticket, responsive=False)
                scheduler.on_terminal(ticket, outcome="tcp_dead")
            elif sequence % 5 == 0:
                scheduler.on_tcp_outcome(ticket, responsive=True)
                scheduler.on_terminal(ticket, passed=True, outcome="pass")
                scheduler.on_commit_complete(ticket, net_new=sequence % 10 == 0)
                scheduler.on_commit_complete(ticket, net_new=False)
            else:
                scheduler.on_tcp_outcome(ticket, responsive=True)
                scheduler.on_fast_yield(
                    ticket,
                    outcome="candidate_fail",
                    busy_worker_ms=3000,
                )
                scheduler.on_terminal(
                    ticket,
                    outcome="candidate_fail",
                    busy_worker_ms=3000,
                    stage="fast_probe",
                )

    metrics = scheduler.quick_metrics()
    assert handled == 1000
    assert metrics["lookahead_indexed_count"] == 0
    assert metrics["tcp_handoff_lease_count"] == 0
    assert metrics["feedback_only_ticket_count"] == 0
    assert metrics["active_ticket_count"] == 0
    assert metrics["parked_deep_debt"] == 0
    assert scheduler.release_outstanding() == 0


def test_controlled_late_quality_reaches_frozen_performance_gate() -> None:
    def evidence(item) -> Tier3CandidateLookaheadEvidence:
        high_yield = item.node.name.startswith("late-pass")
        return Tier3CandidateLookaheadEvidence(
            stable_tcp_positive=high_yield,
            observed=False,
            pre_tcp_score=0.2 if high_yield else 0.0,
        )

    scheduler = Tier3CandidateScheduler(
        max_active_arms_per_family=16,
        lookahead_evidence_provider=evidence,
    )
    for index in range(4000):
        enqueue(
            scheduler,
            f"ordinary-dead-{index}",
            root=f"root-{index % 17}",
            family=f"family-{index % 5}",
            coarse=f"ordinary-{index}",
        )
    for index in range(25):
        enqueue(
            scheduler,
            f"late-pass-{index}",
            root=f"root-{index % 17}",
            family=f"family-{index % 5}",
            coarse=f"late-pass-{index}",
        )

    committed = 0
    elapsed_seconds = 0.0
    time_to_three = 0.0
    net_new_at_twenty = 0
    while committed < 25 and elapsed_seconds < 61.0:
        reservations = scheduler.reserve_handoffs(16)
        assert reservations
        for reservation in reservations:
            assert scheduler.commit_handoff(reservation)
            ticket = reservation.ticket
            passed = reservation.candidate.node.name.startswith("late-pass")
            if passed:
                scheduler.on_tcp_outcome(ticket, responsive=True)
                scheduler.on_terminal(
                    ticket,
                    passed=True,
                    outcome="pass",
                    busy_worker_ms=750,
                    stage="fast_probe",
                )
                scheduler.on_commit_complete(ticket, net_new=True)
                committed += 1
                if committed == 3:
                    time_to_three = elapsed_seconds + 1.25
            else:
                scheduler.on_tcp_outcome(ticket, responsive=False)
                scheduler.on_terminal(ticket, outcome="tcp_dead")
        elapsed_seconds += 1.25
        if elapsed_seconds <= 20.0:
            net_new_at_twenty = committed

    assert time_to_three <= 10.0
    assert net_new_at_twenty >= 15
    assert committed == 25
    assert elapsed_seconds <= 61.0
    assert scheduler.quick_metrics()["selected_total"] <= 16 * 3
    assert scheduler.release_outstanding() == 0
