"""Revalidated replay may request a trial, never supply cold PASS evidence."""

import pytest

from tests.test_tier3_candidate_scheduler import candidate, feature
from tests.test_tier3_exact_inventory_index import complete
from tier3_candidate_scheduler import Tier3CandidateLookaheadEvidence, Tier3CandidateScheduler


PROFILES = ("l0", "l1", "l2", "precise-leaf")


def setup(*, root="opaque", roots=1):
    scheduler = Tier3CandidateScheduler(
        max_active_arms_per_family=1,
        lookahead_evidence_provider=lambda item: Tier3CandidateLookaheadEvidence(
            validated_positive=item.node.name.startswith("replay"),
            tcp_only=item.node.name.startswith("peer"),
        ),
    )
    scheduler.set_credit_envelope(16, handoff_window=16)

    def add(name, *, source=root, profiles=PROFILES):
        scheduler.enqueue(
            candidate(name), None, arm_key=name, coarse_key=name,
            source_root=source, family="opaque-family",
            feature_key=feature(family="opaque-family", service_profiles=profiles),
        )

    add("replay-donor")
    donor = scheduler.reserve_handoffs(1)[0]
    complete(scheduler, donor, passed=True)
    for index in range(128):
        add(f"ordinary-{index}", source=root if roots == 1 else f"{root}-{index % roots}",
            profiles=("l0", "l1", "l2", f"other-{index}"))
    for index in range(8):
        add(f"peer-{index}")
    return scheduler, add


@pytest.mark.parametrize("roots", [1, 2, 5, 17])
def test_replay_trial_preserves_cold_evidence_and_is_actually_scheduled(roots):
    scheduler, _ = setup(root=f"renamed-{roots}", roots=roots)
    trials = []
    for _ in range(3):
        batch = scheduler.reserve_handoffs(16)
        assert sum(item.ticket.unknown_exploration for item in batch) >= 3
        assert sum(item.ticket.coverage_selected for item in batch) <= 4
        for item in batch:
            scheduler.commit_handoff(item)
            if item.ticket.selection_reason == "replay_leaf_discovery":
                trials.append(item)
            else:
                scheduler.on_tcp_outcome(item.ticket, responsive=False)
    assert len(trials) == 1
    # Coverage is entitled to take earlier peers before this discovery slot.
    assert trials[0].candidate.node.name.startswith("peer-")
    metrics = scheduler.quick_metrics()
    assert metrics["replay_fast_observation_pass_count"] == 1
    assert metrics["cold_fast_observation_pass_count"] == 0
    assert metrics["reward_service_probation_cohort_count"] == 0
    assert metrics["replay_leaf_discovery"]["outstanding"] == 1
    complete(scheduler, trials[0], passed=True)
    metrics = scheduler.quick_metrics()
    assert metrics["cold_fast_observation_pass_count"] == 1
    assert metrics["reward_service_probation_cohort_count"] == 0
    assert metrics["replay_leaf_discovery"]["net_new"] == 1
    scheduler.release_outstanding()


def test_replay_trial_cancel_is_fifo_and_duplicate_failure_cannot_reopen_it():
    scheduler, _ = setup()
    trial, _ = scheduler._pop_replay_leaf_discovery_candidate_unlocked(ignore_credits=False)
    assert trial.node.name == "peer-0"
    before = scheduler.quick_metrics()["selected_total"]
    assert scheduler.cancel_handoff(trial.dispatch_ticket, requeue=True)
    assert not scheduler.cancel_handoff(trial.dispatch_ticket, requeue=True)
    assert scheduler.quick_metrics()["selected_total"] == before
    retry, _ = scheduler._pop_replay_leaf_discovery_candidate_unlocked(ignore_credits=False)
    assert retry.node.name == trial.node.name
    scheduler.commit_handoff(retry.dispatch_ticket)
    scheduler.on_tcp_outcome(retry.dispatch_ticket, responsive=False)
    scheduler.on_tcp_outcome(retry.dispatch_ticket, responsive=False)
    scheduler.on_commit_complete(retry.dispatch_ticket, net_new=True)
    again, _ = scheduler._pop_replay_leaf_discovery_candidate_unlocked(ignore_credits=False)
    assert again is None
    metrics = scheduler.quick_metrics()["replay_leaf_discovery"]
    assert metrics["selected"] == metrics["resolved"] == 1
    assert metrics["outstanding"] == metrics["net_new"] == 0
    scheduler.release_outstanding()


def test_replay_trial_never_matches_sibling_or_different_source():
    scheduler, add = setup()
    # Remove only the same-leaf, same-source peers before testing boundaries.
    while True:
        item = next((item for arm in scheduler._arms.values() for item in arm.items
                     if item.node.name.startswith("peer")), None)
        if item is None:
            break
        arm = next(arm for arm in scheduler._arms.values() if item in arm.items)
        selected = scheduler._dequeue_from_arm(arm, selection_reason="balanced", preferred_candidate=item)
        scheduler.cancel_handoff(selected.dispatch_ticket, requeue=False)
    add("peer-sibling", profiles=(*PROFILES[:-1], "sibling"))
    add("peer-other-root", source="other-root")
    item, _ = scheduler._pop_replay_leaf_discovery_candidate_unlocked(ignore_credits=False)
    assert item is None
    scheduler.release_outstanding()


def test_current_fast_pass_requires_commit_and_trial_expires(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("tier3_candidate_scheduler.time.monotonic", lambda: clock[0])
    scheduler, add = setup()
    clock[0] += 61.0
    item, _ = scheduler._pop_replay_leaf_discovery_candidate_unlocked(ignore_credits=False)
    assert item is None
    add("replay-new", profiles=("a", "b", "c", "new-leaf"))
    add("peer-new", profiles=("a", "b", "c", "new-leaf"))
    batch = scheduler.reserve_handoffs(16)
    donor = next(item for item in batch if item.candidate.node.name == "replay-new")
    for item in batch:
        if item is not donor:
            scheduler.cancel_handoff(item, requeue=False)
    assert donor.candidate.node.name == "replay-new"
    scheduler.commit_handoff(donor)
    scheduler.on_tcp_outcome(donor.ticket, responsive=True)
    scheduler.on_terminal(donor.ticket, passed=True, outcome="pass", stage="fast_probe")
    assert scheduler._pop_replay_leaf_discovery_candidate_unlocked(ignore_credits=False)[0] is None
    scheduler.on_commit_complete(donor.ticket, net_new=False)
    assert scheduler._pop_replay_leaf_discovery_candidate_unlocked(ignore_credits=False)[0] is None
    scheduler.release_outstanding()


def test_two_global_trials_and_one_per_leaf_even_across_sources():
    scheduler, add = setup()
    for index in range(5):
        profiles = ("l0", "l1", "l2", f"leaf-{index}")
        add(f"replay-extra-{index}", profiles=profiles)
        add(f"peer-extra-{index}", profiles=profiles)
    for item in scheduler.reserve_handoffs(16):
        if item.candidate.node.name.startswith("replay"):
            complete(scheduler, item, passed=True)
        else:
            scheduler.cancel_handoff(item, requeue=True)
    trials = [scheduler._pop_replay_leaf_discovery_candidate_unlocked(ignore_credits=False)[0]
              for _ in range(3)]
    assert all(trials[:2]) and trials[2] is None
    for item in trials[:2]:
        scheduler.commit_handoff(item.dispatch_ticket)
        scheduler.on_tcp_outcome(item.dispatch_ticket, responsive=True)
        scheduler.on_fast_yield(item.dispatch_ticket, right_censored=True)
    assert scheduler._pop_replay_leaf_discovery_candidate_unlocked(ignore_credits=False)[0] is None
    scheduler.release_outstanding()
    assert scheduler.quick_metrics()["replay_leaf_discovery"]["outstanding"] == 0


def test_one_thousand_trial_outcomes_and_late_feedback_conserve_ownership():
    transactions = 0
    for cycle in range(64):
        scheduler, add = setup()
        for index in range(24):
            add(f"replay-extra-{index}", profiles=("l0", "l1", "l2", f"leaf-{index}"))
            add(f"peer-extra-{index}", profiles=("l0", "l1", "l2", f"leaf-{index}"))
        batch = scheduler.reserve_handoffs(16)
        transactions += len(batch)
        for item in batch:
            if item.candidate.node.name.startswith("replay"):
                complete(scheduler, item, passed=True)
            else:
                scheduler.cancel_handoff(item, requeue=True)
        for index in range(16):
            item, _ = scheduler._pop_replay_leaf_discovery_candidate_unlocked(ignore_credits=False)
            if item is None:
                break
            transactions += 1
            ticket = item.dispatch_ticket
            outcome = (cycle + index) % 8
            if outcome == 0:
                scheduler.cancel_handoff(ticket, requeue=True)
                continue
            scheduler.commit_handoff(ticket)
            if outcome <= 2:
                scheduler.on_tcp_outcome(ticket, responsive=False, hard_censored=outcome == 2)
            else:
                scheduler.on_tcp_outcome(ticket, responsive=True)
                if outcome == 3:
                    scheduler.on_fast_yield(ticket, right_censored=True)
                    scheduler.on_terminal(ticket, outcome="right_censored", stage="canonical_deep")
                elif outcome == 4:
                    scheduler.on_terminal(ticket, outcome="facility_failure")
                else:
                    scheduler.on_terminal(ticket, passed=True, outcome="pass", stage="fast_probe")
                    if outcome != 7:
                        scheduler.on_commit_complete(ticket, net_new=outcome == 5)
            scheduler.on_tcp_outcome(ticket, responsive=False)
            # PASS awaiting commit is cancelled by seal, not double-committed.
            if outcome != 7:
                scheduler.on_terminal(ticket, outcome="cancelled")
        scheduler.release_outstanding()
        metrics = scheduler.quick_metrics()
        assert metrics["replay_leaf_discovery"]["outstanding"] == 0
        assert metrics["replay_leaf_discovery"]["resolved"] == metrics["replay_leaf_discovery"]["selected"]
        assert scheduler.pending_handoff_count() == 0
        assert scheduler._tickets == {}
    assert transactions >= 1024


def test_same_input_selection_ab_does_not_hide_replay_peer_behind_large_leaves():
    class NoReplayTrial(Tier3CandidateScheduler):
        def _pop_replay_leaf_discovery_candidate_unlocked(self, *, ignore_credits):
            return None, 0

    # This is a causal selection comparison, not a wall-clock throughput test.
    # Both schedulers see exactly the same completed feedback, delayed until
    # after the entire real-handoff batch. No promotion is assigned directly.
    results = []
    for scheduler_type in (NoReplayTrial, Tier3CandidateScheduler):
        scheduler = scheduler_type(lookahead_evidence_provider=lambda item:
            Tier3CandidateLookaheadEvidence(validated_positive=item.node.name == "donor"))
        scheduler.set_credit_envelope(16, handoff_window=16)

        def add(name, leaf):
            scheduler.enqueue(candidate(name), None, arm_key=name, coarse_key=name,
                              source_root="opaque", family="opaque-family",
                              feature_key=feature(service_profiles=("l0", "l1", "l2", leaf)))

        add("donor", "precise")
        complete(scheduler, scheduler.reserve_handoffs(1)[0], passed=True)
        for index in range(4096):
            add(f"ordinary-{index}", f"other-{index // 64}")
        for index in range(48):
            add(f"target-{index}", "precise")
        found = 0
        for _ in range(4):
            batch = scheduler.reserve_handoffs(16)
            assert len(batch) == 16
            assert sum(item.ticket.unknown_exploration for item in batch) >= 3
            for item in batch:
                scheduler.commit_handoff(item)
            for item in reversed(batch):
                if item.candidate.node.name.startswith("target"):
                    complete(scheduler, item, passed=True)
                    found += 1
                else:
                    scheduler.on_tcp_outcome(item.ticket, responsive=False)
        assert scheduler.quick_metrics()["selected_total"] == 65
        results.append(found)
        scheduler.release_outstanding()
    assert results[0] == 0
    assert results[1] >= 3
