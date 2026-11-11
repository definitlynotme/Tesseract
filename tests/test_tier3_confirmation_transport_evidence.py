"""Transport evidence can order a bounded confirmation, never admit it."""

from collections import Counter

import pytest

from tests.test_tier3_candidate_scheduler import candidate, feature
from tests.test_tier3_exact_inventory_index import complete
from tier3_candidate_scheduler import Tier3CandidateLookaheadEvidence, Tier3CandidateScheduler


PROFILES = ("opaque-l0", "opaque-l1", "opaque-l2", "opaque-leaf")


def seeded(*, shared_arm=False, tcp_history=True, peer_root="root"):
    scheduler = Tier3CandidateScheduler(
        max_active_arms_per_family=1,
        lookahead_evidence_provider=lambda item: Tier3CandidateLookaheadEvidence(
            tcp_only=tcp_history and item.node.name.startswith("responsive-"),
        ),
    )
    scheduler.set_credit_envelope(16, handoff_window=16)

    def add(name, root="root", profiles=PROFILES):
        scheduler.enqueue(
            candidate(name), None,
            arm_key="shared" if shared_arm else name,
            coarse_key="shared" if shared_arm else name,
            source_root=root, family="opaque-family",
            feature_key=feature(family="opaque-family", service_profiles=profiles),
        )

    add("seed")
    complete(scheduler, scheduler.reserve_handoffs(1)[0], passed=True)
    for index in range(8):
        add(f"unknown-{index}")
    for index in range(8):
        add(f"responsive-{index}", root=peer_root)
    return scheduler, add


@pytest.mark.parametrize("shared_arm", [False, True])
def test_confirmation_prefers_same_bucket_same_leaf_tcp_evidence(shared_arm):
    scheduler, _ = seeded(shared_arm=shared_arm)
    selected, _ = scheduler._pop_leaf_confirmation_candidate_unlocked(ignore_credits=False)
    assert selected is not None
    assert selected.node.name == "responsive-0"
    assert selected.dispatch_ticket.visibility_class == "tcp_only"
    assert not selected.dispatch_ticket.unknown_exploration
    assert scheduler.quick_metrics()["selected_total"] == 1
    assert scheduler.quick_metrics()["reward_service_probation_cohort_count"] == 0
    decision = scheduler.quick_metrics()["reward_service_recent_decisions"][-1]
    assert decision["visibility_class"] == "tcp_only"
    assert decision["remaining_tcp_only"] == 7
    assert decision["remaining_unknown"] == 8
    scheduler.release_outstanding()


def test_confirmation_evidence_does_not_cross_root_fairness_or_leaf():
    scheduler, add = seeded(peer_root="later-root")
    add("responsive-sibling", profiles=(*PROFILES[:-1], "other-leaf"))
    # Keep the root/family cursor fixed: evidence must not buy an earlier turn.
    scheduler._root_order.clear()
    scheduler._root_order.extend(["root", "later-root"])
    selected, _ = scheduler._pop_leaf_confirmation_candidate_unlocked(ignore_credits=False)
    assert selected.node.name == "unknown-0"
    assert selected.source_root == "root"
    assert selected.dispatch_ticket.service_cohort_keys[-1] == "service:opaque-leaf"
    scheduler.release_outstanding()


def test_confirmation_cancel_keeps_fifo_and_never_creates_a_validation_sample():
    scheduler, _ = seeded()
    first, _ = scheduler._pop_leaf_confirmation_candidate_unlocked(ignore_credits=False)
    assert first.node.name == "responsive-0"
    assert scheduler.cancel_handoff(first.dispatch_ticket, requeue=True)
    assert not scheduler.cancel_handoff(first.dispatch_ticket, requeue=True)
    second, _ = scheduler._pop_leaf_confirmation_candidate_unlocked(ignore_credits=False)
    assert second.node.name == first.node.name
    assert scheduler.quick_metrics()["selected_total"] == 1
    assert scheduler.quick_metrics()["visibility_tcp_only_fast_pass_total"] == 0
    scheduler.release_outstanding()


def test_same_input_confirmation_reaches_validation_instead_of_spending_both_on_dead_tcp():
    outcomes = []
    for history in (False, True):
        scheduler, _ = seeded(tcp_history=history)
        selected = []
        for _ in range(2):
            item, _ = scheduler._pop_leaf_confirmation_candidate_unlocked(ignore_credits=False)
            assert item is not None
            scheduler.commit_handoff(item.dispatch_ticket)
            selected.append(item)
        extra, _ = scheduler._pop_leaf_confirmation_candidate_unlocked(ignore_credits=False)
        assert extra is None
        for item in selected:
            ticket = item.dispatch_ticket
            alive = item.node.name.startswith("responsive-")
            previous_passes = scheduler.quick_metrics()["visibility_tcp_only_fast_pass_total"]
            scheduler.on_tcp_outcome(ticket, responsive=alive)
            assert scheduler.quick_metrics()["visibility_tcp_only_fast_pass_total"] == previous_passes
            if alive:
                # History alone cannot generate PASS or promotion.
                scheduler.on_terminal(ticket, passed=True, outcome="pass", stage="fast_probe")
                scheduler.on_commit_complete(ticket, net_new=True)
        outcomes.append(scheduler.quick_metrics()["reward_service_probation_cohort_count"])
        scheduler.release_outstanding()
        assert scheduler.pending_handoff_count() == 0
        assert scheduler._tickets == {}
    assert outcomes == [0, 1]


@pytest.mark.parametrize("shared_arm", [False, True])
def test_probation_keeps_transport_evidence_after_confirmation(shared_arm, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("tier3_candidate_scheduler.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("tier3_candidate_scheduler.time.monotonic_ns", lambda: int(clock[0] * 1e9))
    scheduler, _ = seeded(shared_arm=shared_arm)
    confirmation, _ = scheduler._pop_leaf_confirmation_candidate_unlocked(ignore_credits=False)
    ticket = confirmation.dispatch_ticket
    scheduler.commit_handoff(ticket)
    scheduler.on_tcp_outcome(ticket, responsive=True)
    scheduler.on_terminal(ticket, passed=True, outcome="pass", stage="fast_probe")
    scheduler.on_commit_complete(ticket, net_new=True)
    assert scheduler.quick_metrics()["reward_service_probation_cohort_count"] == 1
    # The shared-arm case also has an exact grant; inspect service once that
    # independent, higher-priority grant expires normally.
    clock[0] += 16.0
    selected, _ = scheduler._pop_service_reward_candidate_unlocked(ignore_credits=False)
    assert selected is not None
    assert selected.node.name == "responsive-1"
    assert selected.dispatch_ticket.visibility_class == "tcp_only"
    assert selected.dispatch_ticket.reward_scope == "service"
    # Reservation itself is still neither selection nor positive evidence.
    assert scheduler.quick_metrics()["reward_service_selected_count"] == 0
    scheduler.cancel_handoff(selected.dispatch_ticket, requeue=True)
    retried, _ = scheduler._pop_service_reward_candidate_unlocked(ignore_credits=False)
    assert retried.node.name == selected.node.name
    scheduler.release_outstanding()


def test_one_thousand_mixed_transactions_keep_confirmation_flights_and_index_exact():
    starts = 0
    for cycle in range(64):
        scheduler, add = seeded(shared_arm=cycle % 2 == 0)
        for index in range(32):
            add(f"extra-{index}", profiles=("l0-extra", "l1-extra", "l2-extra", f"leaf-{index}"))
        batch = scheduler.reserve_handoffs(16)
        cycle_starts = 0
        assert len(batch) == 16
        assert sum(r.ticket.unknown_exploration for r in batch) >= 3
        assert sum(r.ticket.reward_cohort_state == "leaf_confirmation" for r in batch) <= 2
        for offset, reservation in enumerate(batch):
            if (cycle + offset) % 9 == 0:
                assert scheduler.cancel_handoff(reservation, requeue=True)
                assert not scheduler.cancel_handoff(reservation, requeue=True)
            else:
                scheduler.commit_handoff(reservation)
                starts += 1
                cycle_starts += 1
        # All starts precede the asynchronous feedback; no extra flights may
        # be created by a duplicate result or a terminal following TCP failure.
        for offset, reservation in enumerate(reversed(batch)):
            ticket = reservation.ticket
            outcome = (cycle + offset) % 6
            if outcome < 2:
                scheduler.on_tcp_outcome(ticket, responsive=False, hard_censored=outcome == 1)
            else:
                scheduler.on_tcp_outcome(ticket, responsive=True)
                if outcome == 2:
                    scheduler.on_fast_yield(ticket, right_censored=True)
                    scheduler.on_terminal(ticket, outcome="right_censored", stage="deep_probe")
                elif outcome == 3:
                    scheduler.on_terminal(ticket, outcome="facility_failure")
                else:
                    scheduler.on_terminal(ticket, passed=True, outcome="pass", stage="fast_probe")
                    scheduler.on_commit_complete(ticket, net_new=outcome == 4)
                    scheduler.on_commit_complete(ticket, net_new=True)
            scheduler.on_terminal(ticket, outcome="cancelled")
        expected = {}
        for arm_id, arm in scheduler._arms.items():
            for item in arm.items:
                if item.visibility_class != "tcp_only":
                    continue
                for key in (*scheduler._candidate_service_cohort_keys(item),
                            scheduler._candidate_similar_cohort_key(item)):
                    expected.setdefault(key, Counter())[arm_id] += 1
        assert scheduler._cohort_tcp_only_queued_arms == expected
        assert scheduler.quick_metrics()["selected_total"] == 1 + cycle_starts
        scheduler.release_outstanding()
        assert scheduler.pending_handoff_count() == 0
        assert scheduler._tickets == {}
    assert starts >= 900


def test_service_transport_hint_cannot_take_another_roots_turn():
    scheduler, _ = seeded(peer_root="later-root")
    first, _ = scheduler._pop_leaf_confirmation_candidate_unlocked(ignore_credits=False)
    scheduler.commit_handoff(first.dispatch_ticket)
    scheduler.on_tcp_outcome(first.dispatch_ticket, responsive=True)
    scheduler.on_terminal(first.dispatch_ticket, passed=True, stage="fast_probe")
    scheduler.on_commit_complete(first.dispatch_ticket, net_new=True)
    scheduler._root_order.clear()
    scheduler._root_order.extend(["root", "later-root"])
    scheduler._credit_escape_epoch_debt[("root", "opaque-family")] = 2
    scheduler._credit_escape_epoch_debt[("later-root", "opaque-family")] = 0
    selected, _ = scheduler._pop_service_reward_candidate_unlocked(ignore_credits=False)
    assert selected is not None
    assert selected.source_root == "root"
    assert selected.dispatch_ticket.visibility_class == "unknown"
    scheduler.release_outstanding()


def test_service_tcp_history_does_not_prevent_current_failure_demotion():
    scheduler, _ = seeded()
    first, _ = scheduler._pop_leaf_confirmation_candidate_unlocked(ignore_credits=False)
    scheduler.commit_handoff(first.dispatch_ticket)
    scheduler.on_tcp_outcome(first.dispatch_ticket, responsive=True)
    scheduler.on_terminal(first.dispatch_ticket, passed=True, stage="fast_probe")
    scheduler.on_commit_complete(first.dispatch_ticket, net_new=True)
    flights = []
    for _ in range(4):
        item, _ = scheduler._pop_service_reward_candidate_unlocked(ignore_credits=False)
        assert item is not None
        assert item.dispatch_ticket.visibility_class == "tcp_only"
        scheduler.commit_handoff(item.dispatch_ticket)
        flights.append(item.dispatch_ticket)
    extra, _ = scheduler._pop_service_reward_candidate_unlocked(ignore_credits=False)
    assert extra is None
    for ticket in reversed(flights):
        scheduler.on_tcp_outcome(ticket, responsive=False)
        scheduler.on_tcp_outcome(ticket, responsive=False)
    metrics = scheduler.quick_metrics()
    assert metrics["reward_service_selected_count"] == 4
    assert metrics["reward_service_net_new_count"] == 0
    assert metrics["reward_service_probation_cohort_count"] == 0
    assert metrics["reward_service_outstanding_count"] == 0
    scheduler.release_outstanding()
