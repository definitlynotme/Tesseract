from __future__ import annotations

from collections import deque
from dataclasses import replace
from unittest.mock import patch

import pytest

from tier3_candidate_scheduler import Tier3CandidateScheduler
from tests.test_tier3_candidate_scheduler import candidate, feature


def reference_count(scheduler, grant):
    return sum(
        item.arm_key == grant.preferred_arm_id
        and item.feature_key == grant.preferred_feature_key
        for arm_id in scheduler._arm_ids_by_feedback_key.get(grant.preferred_arm_id, ())
        for arm in (scheduler._arms.get(arm_id),)
        if arm is not None
        for item in arm.items
    )


class ReferenceScheduler(Tier3CandidateScheduler):
    def _grant_exact_inventory_count_unlocked(self, grant):
        return reference_count(self, grant)


def add(scheduler, name, index=0, *, roots=5):
    scheduler.enqueue(
        candidate(name), None, arm_key=f"arm-{index % 9}",
        source_root=f"root-{index % roots}", family=f"family-{index % 3}",
        coarse_key=f"coarse-{index % 19}",
        feature_key=feature(service_profiles=(
            "l0", f"l1-{index % 2}", f"l2-{index % 4}", f"l3-{index % 7}",
        )),
    )


def complete(scheduler, reservation, passed):
    scheduler.commit_handoff(reservation)
    scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
    scheduler.on_fast_observation(
        reservation.ticket, passed=passed, busy_worker_ms=1000,
        outcome="pass" if passed else "fail",
    )
    scheduler.on_terminal(
        reservation.ticket, passed=passed, outcome="pass" if passed else "fail",
        stage="fast_probe",
    )
    if passed:
        scheduler.on_commit_complete(reservation.ticket, net_new=True)


def assert_counts(scheduler):
    for grants in scheduler._exact_rewards.values():
        for grant in grants:
            assert scheduler._grant_exact_inventory_count_unlocked(grant) == reference_count(scheduler, grant)


def test_exact_inventory_does_not_scan_candidate_queues():
    scheduler = Tier3CandidateScheduler()
    add(scheduler, "seed")
    complete(scheduler, scheduler.reserve_handoffs(1)[0], True)
    for index in range(100):
        add(scheduler, f"queued-{index}", index)
    grant = scheduler._exact_rewards["arm-0"][0]
    expected = reference_count(scheduler, grant)
    assert expected > 0

    class NoIteration(deque):
        def __iter__(self):
            raise AssertionError("exact inventory query scanned queued candidates")

    for arm in scheduler._arms.values():
        arm.items = NoIteration(arm.items)
    assert scheduler._grant_exact_inventory_count_unlocked(grant) == expected
    assert scheduler._grant_exact_inventory_count_unlocked(
        replace(grant, preferred_arm_id="absent"),
    ) == 0


@pytest.mark.parametrize("roots", [1, 2, 5, 17])
def test_exact_inventory_matches_scan_through_reservation_cancel_and_feedback(roots):
    traces = []
    for scheduler_type in (ReferenceScheduler, Tier3CandidateScheduler):
        with patch("tier3_candidate_scheduler.time.monotonic", return_value=100.0):
            scheduler = scheduler_type()
            scheduler.set_credit_envelope(16, handoff_window=16)
            add(scheduler, "seed", roots=roots)
            complete(scheduler, scheduler.reserve_handoffs(1)[0], True)
            for index in range(500):
                add(scheduler, f"queued-{index}", index, roots=roots)
            trace = []
            for epoch in range(16):
                reservations = scheduler.reserve_handoffs(16)
                assert_counts(scheduler)
                for offset, item in enumerate(reservations):
                    if (epoch + offset) % 7 == 0:
                        scheduler.cancel_handoff(item, requeue=True)
                    elif (epoch + offset) % 11 == 0:
                        scheduler.cancel_handoff(item, requeue=False)
                    else:
                        trace.append((item.candidate.node.name, item.ticket.selection_reason))
                        complete(scheduler, item, offset % 5 == 0)
                assert_counts(scheduler)
            scheduler.drain_all()
            assert_counts(scheduler)
            add(scheduler, "late-reuse", roots=roots)
            assert_counts(scheduler)
            assert scheduler.release_outstanding() == 0
            traces.append(trace)
    assert traces[0] == traces[1]
