from __future__ import annotations

import queue

import pytest

from tier3_expansion_actor import Tier3ExpansionActor
from tier3_validation_protocol import ValidationStage


def make_actor(*, latched: bool = True) -> Tier3ExpansionActor:
    actor = Tier3ExpansionActor(
        "idle-loan", dispatch_sink=lambda _command: True,
        admission_sink=lambda _intent: True, terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    actor._fast_slot_limit = 15 if latched else 12
    actor._deep_slot_limit = 1 if latched else 4
    actor._deep_zero_yield_latched = latched
    actor._external_fresh_backlog = 7_000
    actor._tcp_handoff_lease_count = 16
    actor._expected_responsive_arrivals = 3
    actor._active_fast_count = 8
    actor._deep_ready_count = 8
    actor._deep_parked_count = 64
    actor._active_deep_count = actor._deep_slot_limit
    return actor


@pytest.mark.parametrize("supply", ["ready", "imminent", "blocked"])
def test_active_fast_does_not_hide_one_idle_latched_loan(supply):
    actor = make_actor()
    actor._fast_supply_state = supply

    assert actor._stage_slot_limits() == (14, 2)
    assert actor._choose_stage() is ValidationStage.CANONICAL_DEEP
    actor._active_deep_count = 2
    assert actor._choose_stage() is None
    snapshot = actor._build_snapshot()
    assert (snapshot.desired_fast_slots, snapshot.desired_deep_slots) == (15, 1)
    assert snapshot.borrowed_deep_slots == 1
    assert actor._deep_zero_yield_latched


@pytest.mark.parametrize("active, arrivals, expected", [
    (8, 7, (15, 1)), (15, 0, (15, 1)), (14, 0, (14, 2)),
    (8, 100, (15, 1)),
])
def test_latched_loan_respects_actual_and_predicted_fast_capacity(active, arrivals, expected):
    actor = make_actor()
    actor._active_fast_count = active
    actor._expected_responsive_arrivals = arrivals
    assert actor._stage_slot_limits() == expected


@pytest.mark.parametrize("queue_field", ["_fast_ready_count", "_fast_parked_count"])
def test_new_fast_work_revokes_new_loan_without_preempting_running_deep(queue_field):
    actor = make_actor()
    assert actor._stage_slot_limits() == (14, 2)
    actor._active_deep_count = 2
    setattr(actor, queue_field, 1)

    assert actor._stage_slot_limits() == (15, 1)
    assert actor._active_deep_count == 2
    snapshot = actor._build_snapshot()
    assert snapshot.borrowed_deep_slots == 0


def test_unlatched_loan_never_exceeds_two_or_idle_capacity():
    actor = make_actor(latched=False)
    actor._expected_responsive_arrivals = 0
    assert actor._stage_slot_limits() == (10, 6)
    actor._active_fast_count = 11
    assert actor._stage_slot_limits() == (11, 5)
    actor._active_fast_count = 12
    assert actor._stage_slot_limits() == (12, 4)


def test_full_steal_still_requires_two_exhausted_revisions():
    actor = make_actor()
    actor._external_fresh_backlog = 0
    actor._tcp_handoff_lease_count = 0
    actor._expected_responsive_arrivals = 0
    actor._active_fast_count = 0
    actor._fast_supply_state = "exhausted"
    actor._fast_supply_nonactionable_revisions = 1
    assert actor._stage_slot_limits() == (14, 2)
    actor._fast_supply_nonactionable_revisions = 2
    assert actor._stage_slot_limits() == (0, 16)
    assert actor._deep_zero_yield_latched
