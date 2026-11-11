"""FAST cost is transport evidence, not responsive count or a selection prior."""

import pytest

from tests.test_tier3_tcp_hint_exposure import add, make_scheduler
from tier3_candidate_scheduler import Tier3CandidateLookaheadEvidence, Tier3CandidateScheduler


def costs(scheduler):
    return scheduler.quick_metrics()["visibility_fast_cost"]


@pytest.mark.parametrize("visibility", [
    "unknown", "tcp_only", "validated_positive", "validated_reprobe",
    "observed_nonpositive",
])
def test_cost_counts_one_fast_fact_not_terminal_or_ancestor_copies(visibility):
    scheduler = Tier3CandidateScheduler(
        lookahead_evidence_provider=lambda item: Tier3CandidateLookaheadEvidence(
            **({visibility: True} if visibility != "unknown" else {}),
        ),
    )
    add(scheduler, "candidate")
    reservation = scheduler.reserve_handoffs(1)[0]
    assert reservation.ticket.visibility_class == visibility
    scheduler.commit_handoff(reservation)
    scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
    scheduler.on_fast_observation(reservation.ticket, right_censored=True, busy_worker_ms=2750)
    before = costs(scheduler)
    assert before[visibility] == {
        "samples": 1, "completed": 0, "censored": 1, "passed": 0,
        "worker_ms": 2750, "completed_worker_ms": 0, "censored_worker_ms": 2750,
    }
    scheduler.on_fast_observation(reservation.ticket, right_censored=True, busy_worker_ms=2750)
    scheduler.on_fast_yield(reservation.ticket, right_censored=True)
    scheduler.on_terminal(reservation.ticket, passed=True, outcome="pass",
                          stage="canonical_deep", busy_worker_ms=5000)
    scheduler.on_commit_complete(reservation.ticket, net_new=True)
    scheduler.release_outstanding()
    assert costs(scheduler) == before
    before[visibility]["worker_ms"] = -1
    assert costs(scheduler)[visibility]["worker_ms"] == 2750


def test_cost_ignores_tcp_only_facility_and_prestart_cancel():
    scheduler = make_scheduler()
    for index in range(4):
        add(scheduler, f"tcp-{index}")
    pending = scheduler.reserve_handoffs(4)
    scheduler.cancel_handoff(pending[0], requeue=False)
    scheduler.commit_handoff(pending[1])
    scheduler.on_tcp_outcome(pending[1].ticket, responsive=False)
    scheduler.commit_handoff(pending[2])
    scheduler.on_tcp_outcome(pending[2].ticket, responsive=True)
    scheduler.on_fast_observation(pending[2].ticket, facility_failure=True, busy_worker_ms=5000)
    scheduler.on_terminal(pending[2].ticket, outcome="facility", stage="fast", busy_worker_ms=5000)
    scheduler.commit_handoff(pending[3])
    scheduler.on_tcp_outcome(pending[3].ticket, responsive=True)
    scheduler.on_terminal(pending[3].ticket, outcome="cancelled", stage="fast", busy_worker_ms=5000)
    assert costs(scheduler) == {}
    scheduler.release_outstanding()


def test_thousand_mixed_fast_costs_reconcile_and_do_not_change_selection():
    scheduler = make_scheduler()
    control = make_scheduler()
    control._record_fast_visibility_cost_unlocked = lambda *_args, **_kwargs: None
    expected_samples = expected_passes = completed_ms = censored_ms = 0
    for index in range(1000):
        for owner in (scheduler, control):
            add(owner, f"unknown-{index}", leaf=f"leaf-{index % 31}")
        batch = [owner.reserve_handoffs(1)[0] for owner in (scheduler, control)]
        assert [(r.candidate.node.name, r.ticket.selection_reason) for r in batch] == [
            (batch[0].candidate.node.name, batch[0].ticket.selection_reason),
        ] * 2
        for owner, reservation in zip((scheduler, control), batch):
            if index % 10 == 0:
                owner.cancel_handoff(reservation, requeue=False)
                continue
            owner.commit_handoff(reservation)
            owner.on_tcp_outcome(reservation.ticket, responsive=True)
            if index % 10 == 1:
                owner.on_terminal(reservation.ticket, outcome="facility", stage="fast")
                continue
            censored = index % 3 == 0
            passed = not censored and index % 5 == 0
            for _ in range(2):
                owner.on_fast_observation(reservation.ticket, passed=passed,
                                          right_censored=censored, busy_worker_ms=index)
            owner.on_terminal(reservation.ticket, passed=passed,
                              outcome="pass" if passed else "right_censored" if censored else "fail",
                              stage="fast", busy_worker_ms=index)
            if passed:
                owner.on_commit_complete(reservation.ticket, net_new=index % 4 != 0)
                owner.on_commit_complete(reservation.ticket, net_new=True)
        if index % 10 not in {0, 1}:
            expected_samples += 1
            expected_passes += int(index % 3 != 0 and index % 5 == 0)
            if index % 3 == 0:
                censored_ms += index
            else:
                completed_ms += index
    row = costs(scheduler)["unknown"]
    assert row["samples"] == expected_samples
    assert row["passed"] == expected_passes
    assert row["samples"] == row["completed"] + row["censored"]
    assert row["worker_ms"] == completed_ms + censored_ms
    assert row["completed_worker_ms"] == completed_ms
    assert row["censored_worker_ms"] == censored_ms
    for owner in (scheduler, control):
        owner.release_outstanding()
        assert owner.quick_metrics()["active_ticket_count"] == 0
        assert owner.pending_handoff_count() == 0
