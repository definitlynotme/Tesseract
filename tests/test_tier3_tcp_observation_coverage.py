"""Scheduling causality tests, not real-network performance acceptance."""

import pytest

from tests.test_tier3_candidate_lookahead import enqueue
from tests.test_tier3_candidate_scheduler import candidate, feature
from tier3_candidate_scheduler import Tier3CandidateLookaheadEvidence, Tier3CandidateScheduler


def populated(roots=1, *, cache=True, separate_roots=False):
    scheduler = Tier3CandidateScheduler(
        unknown_exploration_ratio=0.15,
        lookahead_evidence_provider=lambda item: Tier3CandidateLookaheadEvidence(
            tcp_only=cache and item.node.name.startswith("tcp-"),
        ),
    )
    scheduler.set_credit_envelope(16, handoff_window=16, tcp_execution_limit=16)
    for prefix, count in (("unknown", 600), ("tcp", 128)):
        for index in range(count):
            root = f"{prefix}-opaque-{index % roots}" if separate_roots else f"opaque-{index % roots}"
            enqueue(scheduler, f"{prefix}-{index}", root=root,
                    family="opaque-family", coarse=f"{prefix}-{index}")
    return scheduler


@pytest.mark.parametrize("roots", [1, 2, 5, 17])
def test_tcp_history_root_receives_bounded_coverage_before_unknown_exhaustion(roots):
    scheduler = populated(roots, separate_roots=True)
    weak_starts = 0
    weak_roots = set()
    for _epoch in range(8):
        batch = scheduler.reserve_handoffs(16)
        assert len(batch) == 16
        weak = [r for r in batch if r.ticket.visibility_class == "tcp_only"]
        assert len(weak) <= 4
        weak_roots.update(r.ticket.source_root for r in weak)
        assert all(r.ticket.coverage_selected for r in weak)
        assert all(not r.ticket.unknown_exploration for r in weak)
        assert all(r.ticket.reward_scope == "none" for r in weak)
        assert sum(r.ticket.visibility_class == "unknown" for r in batch) >= 3
        for reservation in batch:
            assert scheduler.commit_handoff(reservation)
            scheduler.on_tcp_outcome(reservation.ticket, responsive=False)
        weak_starts += len(weak)
    assert weak_starts > 0
    assert weak_roots == {f"tcp-opaque-{index}" for index in range(roots)}
    scheduler.release_outstanding()


def test_prestart_cancel_returns_coverage_without_selection_or_replay_evidence():
    scheduler = populated(separate_roots=True)
    first = scheduler.reserve_handoffs(16)
    weak_names = [r.candidate.node.name for r in first if r.ticket.visibility_class == "tcp_only"]
    assert weak_names
    for reservation in first:
        scheduler.cancel_handoff(reservation, requeue=True)
    metrics = scheduler.quick_metrics()
    assert metrics["selected_total"] == 0
    assert metrics["visibility_tcp_only_start_total"] == 0
    again = scheduler.reserve_handoffs(16)
    assert [r.candidate.node.name for r in again if r.ticket.visibility_class == "tcp_only"] == weak_names
    scheduler.release_outstanding()


def test_tcp_history_is_a_candidate_lane_not_a_pass_or_an_admission():
    scheduler = populated(separate_roots=True)
    batch = scheduler.reserve_handoffs(16)
    weak = next(r for r in batch if r.ticket.visibility_class == "tcp_only")
    scheduler.commit_handoff(weak)
    scheduler.on_tcp_outcome(weak.ticket, responsive=True)
    metrics = scheduler.quick_metrics()
    assert metrics["visibility_tcp_only_fast_pass_total"] == 0
    assert metrics["visibility_tcp_only_net_new_total"] == 0
    scheduler.on_terminal(weak.ticket, outcome="right_censored", stage="fast_probe")
    assert scheduler.quick_metrics()["visibility_tcp_only_net_new_total"] == 0
    scheduler.release_outstanding()


def test_same_inventory_tcp_history_cannot_claim_unneeded_coverage():
    def starts(cache):
        scheduler = populated(cache=cache)
        names = []
        for _ in range(8):
            batch = scheduler.reserve_handoffs(16)
            for reservation in batch:
                scheduler.commit_handoff(reservation)
                names.append(reservation.candidate.node.name)
                scheduler.on_tcp_outcome(reservation.ticket, responsive=False)
        metrics = scheduler.quick_metrics()
        assert metrics["selected_total"] == len(names) == 128
        assert metrics["visibility_tcp_only_fast_pass_total"] == 0
        assert metrics["visibility_tcp_only_net_new_total"] == 0
        scheduler.release_outstanding()
        return names

    cold = starts(False)
    observed = starts(True)
    assert not any(name.startswith("tcp-") for name in cold)
    assert not any(name.startswith("tcp-") for name in observed)


def test_coverage_pass_can_open_existing_leaf_confirmation_without_manual_promotion():
    scheduler = Tier3CandidateScheduler(
        lookahead_evidence_provider=lambda item: Tier3CandidateLookaheadEvidence(
            tcp_only=item.node.name.startswith("tcp-"),
        ),
    )
    scheduler.set_credit_envelope(16, handoff_window=16, tcp_execution_limit=16)
    for prefix, count in (("ordinary", 128), ("tcp", 12)):
        for index in range(count):
            profiles = ("zero", "one", "two", "leaf") if prefix == "tcp" else (
                "zero", f"one-{index}", f"two-{index}", f"leaf-{index}",
            )
            name = f"{prefix}-{index}"
            scheduler.enqueue(
                candidate(name), None, arm_key=name, coarse_key=name,
                source_root=f"{prefix}-opaque-root", family="opaque-family",
                feature_key=feature(family="opaque-family", service_profiles=profiles),
            )
    first = scheduler.reserve_handoffs(16)
    weak = next(r for r in first if r.ticket.visibility_class == "tcp_only")
    for reservation in first:
        if reservation is not weak:
            scheduler.cancel_handoff(reservation, requeue=True)
    assert weak.ticket.reward_scope == "none"
    assert weak.ticket.coverage_selected
    assert not weak.ticket.unknown_exploration
    scheduler.commit_handoff(weak)
    scheduler.on_tcp_outcome(weak.ticket, responsive=True)
    assert scheduler.quick_metrics()["reward_service_probation_cohort_count"] == 0
    scheduler.on_terminal(weak.ticket, passed=True, outcome="pass",
                          busy_worker_ms=200, stage="fast_probe")
    scheduler.on_commit_complete(weak.ticket, net_new=True)
    second = scheduler.reserve_handoffs(16)
    confirmations = [r for r in second
                     if r.ticket.reward_cohort_state == "leaf_confirmation"]
    # Existing confirmation policy permits two flights for >=8 same-leaf peers.
    assert len(confirmations) == 2
    assert all(r.ticket.service_cohort_keys[-1] == "service:leaf" for r in confirmations)
    assert scheduler.quick_metrics()["visibility_tcp_only_net_new_total"] == 1
    scheduler.release_outstanding()


@pytest.mark.parametrize("cancel_reversed", [False, True])
def test_cancelled_tcp_history_returns_in_original_sequence(cancel_reversed):
    scheduler = populated()
    first = scheduler.reserve_handoffs(16)
    order = reversed(first) if cancel_reversed else first
    for reservation in order:
        assert scheduler.cancel_handoff(reservation, requeue=True)
        assert not scheduler.cancel_handoff(reservation, requeue=True)
    again = scheduler.reserve_handoffs(16)
    assert [r.candidate.node.name for r in again] == [r.candidate.node.name for r in first]
    assert scheduler.quick_metrics()["selected_total"] == 0
    scheduler.release_outstanding()


def test_one_thousand_async_coverage_transactions_do_not_leak_or_overspend():
    transactions = 0
    for epoch in range(64):
        scheduler = populated(roots=(1, 2, 5, 17)[epoch % 4])
        batch = scheduler.reserve_handoffs(16)
        assert sum(r.ticket.coverage_selected for r in batch) <= 4
        for offset, reservation in enumerate(reversed(batch)):
            transactions += 1
            ticket = reservation.ticket
            outcome = (epoch + offset) % 8
            if outcome == 0:
                scheduler.cancel_handoff(reservation, requeue=True)
                scheduler.cancel_handoff(reservation, requeue=True)
                continue
            assert scheduler.commit_handoff(reservation)
            assert not scheduler.commit_handoff(reservation)
            if outcome <= 2:
                scheduler.on_tcp_outcome(ticket, responsive=False,
                                         hard_censored=outcome == 2)
            else:
                scheduler.on_tcp_outcome(ticket, responsive=True)
                if outcome == 3:
                    scheduler.on_fast_yield(ticket, right_censored=True)
                    scheduler.on_terminal(ticket, outcome="right_censored")
                elif outcome in (4, 5):
                    scheduler.on_terminal(ticket, passed=True, outcome="pass",
                                          stage="fast_probe", busy_worker_ms=200)
                    scheduler.on_commit_complete(ticket, net_new=outcome == 4)
                else:
                    scheduler.on_terminal(ticket, outcome="cancelled")
            scheduler.on_terminal(ticket, outcome="cancelled")
        scheduler.release_outstanding()
        metrics = scheduler.quick_metrics()
        assert metrics["active_ticket_count"] == 0
        assert metrics["feedback_only_ticket_count"] == 0
        assert scheduler.pending_handoff_count() == 0
        scheduler.drain_all()
        assert not scheduler._lookahead_index._returned_heaps
    assert transactions == 1024
