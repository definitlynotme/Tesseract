"""A fresh evidence window must remain usable late in an expansion."""

import pytest

from tests.test_tier3_candidate_scheduler import candidate, feature
from tests.test_tier3_exact_inventory_index import complete
from tier3_candidate_scheduler import Tier3CandidateScheduler


PROFILES = ("opaque-l0", "opaque-l1", "opaque-l2", "opaque-leaf")
KEY = "service:opaque-leaf"


def add(scheduler, name, *, root="root", profiles=PROFILES):
    scheduler.enqueue(
        candidate(name), None, arm_key=name, source_root=root,
        family="opaque-family", coarse_key=name,
        feature_key=feature(family="opaque-family", service_profiles=profiles),
    )


def training(scheduler, name, *, passed=True):
    add(scheduler, name)
    batch = scheduler.reserve_handoffs(16)
    selected = next(item for item in batch if item.candidate.node.name == name)
    for item in batch:
        if item is not selected:
            scheduler.cancel_handoff(item, requeue=True)
    assert selected.ticket.selection_reason != "reward"
    complete(scheduler, selected, passed)


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("tier3_candidate_scheduler.time.monotonic", lambda: now[0])
    return now


def test_new_one_pass_window_can_confirm_after_old_pass_expires(clock):
    scheduler = Tier3CandidateScheduler()
    training(scheduler, "old-pass")
    clock[0] += 61.0
    training(scheduler, "current-pass")
    for index in range(8):
        add(scheduler, f"peer-{index}")
    assert scheduler._cohort_yield_states[KEY].eligibility.value == "leaf_confirmation"
    selected, _ = scheduler._pop_leaf_confirmation_candidate_unlocked(ignore_credits=False)
    assert selected is not None
    assert selected.dispatch_ticket.service_cohort_key == KEY
    scheduler.release_outstanding()


def test_two_fresh_passes_can_probation_after_old_nonpass_expires(clock):
    scheduler = Tier3CandidateScheduler()
    training(scheduler, "old-nonpass", passed=False)
    clock[0] += 61.0
    training(scheduler, "current-pass-1")
    training(scheduler, "current-pass-2")
    for index in range(8):
        add(scheduler, f"peer-{index}")
    state = scheduler._cohort_yield_states[KEY]
    assert (state.completed_total, state.pass_total) == (3, 2)
    assert state.eligibility.value == "leaf_probation"
    scheduler.release_outstanding()


def test_recent_nonpass_is_not_erased_by_new_passes(clock):
    scheduler = Tier3CandidateScheduler()
    training(scheduler, "recent-nonpass", passed=False)
    training(scheduler, "current-pass-1")
    training(scheduler, "current-pass-2")
    add(scheduler, "peer")
    assert scheduler._cohort_yield_states[KEY].eligibility.value == "observe"
    scheduler.release_outstanding()


def test_old_confirmation_failure_cannot_reset_a_renewed_window(clock):
    scheduler = Tier3CandidateScheduler()
    scheduler.set_credit_envelope(16, handoff_window=16)
    training(scheduler, "old-pass")
    for index in range(32):
        add(scheduler, f"peer-{index}")
    old = []
    for _ in range(2):
        item, _ = scheduler._pop_leaf_confirmation_candidate_unlocked(ignore_credits=False)
        assert item is not None
        scheduler.commit_handoff(item.dispatch_ticket)
        old.append(item.dispatch_ticket)
    scheduler.on_tcp_outcome(old[0], responsive=False)
    assert scheduler._cohort_yield_states[KEY].eligibility.value == "observe"
    clock[0] += 61.0
    # Ordinary exploration supplies new current evidence; no promotion is set.
    batch = scheduler.reserve_handoffs(16)
    current = batch[0]
    assert current.ticket.selection_reason != "reward"
    for item in batch[1:]:
        scheduler.cancel_handoff(item, requeue=True)
    complete(scheduler, current, True)
    state = scheduler._cohort_yield_states[KEY]
    assert state.eligibility.value == "leaf_confirmation"
    selected, _ = scheduler._pop_leaf_confirmation_candidate_unlocked(ignore_credits=False)
    assert selected is not None
    scheduler.commit_handoff(selected.dispatch_ticket)
    scheduler.on_tcp_outcome(old[1], responsive=False)
    assert state.eligibility.value == "leaf_confirmation"
    assert state.confirmation_completion_total == 0
    assert state.confirmation_stale_transition_ignored_total == 1
    assert state.confirmation_outstanding_total == 1
    scheduler.on_tcp_outcome(selected.dispatch_ticket, responsive=True)
    scheduler.on_terminal(selected.dispatch_ticket, passed=True, stage="fast_probe")
    scheduler.on_commit_complete(selected.dispatch_ticket, net_new=True)
    assert state.eligibility.value == "leaf_probation"
    scheduler.release_outstanding()
    assert scheduler._tickets == {}
    assert scheduler.quick_metrics()["leaf_confirmation_outstanding_count"] == 0
