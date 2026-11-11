import pytest

from tests.test_tier3_candidate_scheduler import candidate
from tier3_candidate_scheduler import Tier3CandidateScheduler


@pytest.mark.parametrize("outcome", [
    "cancelled", "closed", "facility_unavailable", "validation_rejected",
    "right_censored", "fail",
])
def test_terminal_before_transport_start_never_commits_selection(outcome):
    scheduler = Tier3CandidateScheduler()
    scheduler.set_credit_envelope(16, handoff_window=16, tcp_execution_limit=16)
    for index in range(16):
        scheduler.enqueue(candidate(str(index)), None, arm_key=str(index),
                          source_root="opaque", family="opaque", coarse_key=str(index))
    reservations = scheduler.reserve_handoffs(16)
    assert len(reservations) == 16
    for item in reversed(reservations):
        scheduler.on_terminal(item.ticket, outcome=outcome, stage="fast_probe", busy_worker_ms=3000)
        scheduler.on_terminal(item.ticket, outcome=outcome)
    metrics = scheduler.quick_metrics()
    assert metrics["selected_total"] == 0
    assert metrics["active_ticket_count"] == 0
    assert metrics["tcp_handoff_lease_count"] == 0
    assert metrics["fast_observation_sample_count"] == 0
    assert metrics["tcp_start_acked_sample_count"] == 0
    assert scheduler.pending_handoff_count() == 0


def test_cancel_after_start_keeps_one_real_handoff_without_validation_evidence():
    scheduler = Tier3CandidateScheduler()
    scheduler.enqueue(candidate("started"), None, arm_key="arm", source_root="root",
                      family="family", coarse_key="coarse")
    reservation = scheduler.reserve_handoffs(1)[0]
    scheduler.commit_handoff(reservation)
    scheduler.on_terminal(reservation.ticket, outcome="cancelled")
    scheduler.on_terminal(reservation.ticket, outcome="cancelled")
    metrics = scheduler.quick_metrics()
    assert metrics["selected_total"] == 1
    assert metrics["active_ticket_count"] == 0
    assert metrics["fast_observation_sample_count"] == 0
