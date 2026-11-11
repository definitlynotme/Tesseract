from __future__ import annotations

import random
from unittest.mock import patch

import pytest

from tier3_candidate_scheduler import Tier3CandidateScheduler, _ColdLeafReservationOrder
from tests.test_tier3_candidate_scheduler import candidate, feature


class FullSortScheduler(Tier3CandidateScheduler):
    @property
    def _cold_leaf_reservation_order(self):
        return None

    @_cold_leaf_reservation_order.setter
    def _cold_leaf_reservation_order(self, value):
        pass


def populate(scheduler, roots, start=0, count=1200, *, ambiguous=False):
    rng = random.Random(start + 721)
    root_names = [f"opaque-{rng.getrandbits(64):016x}" for _ in range(roots)]
    for index in range(start, start + count):
        leaf = rng.randrange(240)
        parent = leaf // 4
        if ambiguous and index % 3 == 0:
            parent = (parent + 1) % 60
        arm = rng.randrange(500)
        scheduler.enqueue(
            candidate(f"node-{index}"), None,
            arm_key=f"arm-{arm}", source_root=root_names[index % roots],
            family=f"family-{index % 3}", coarse_key=f"coarse-{arm}",
            feature_key=feature(service_profiles=(
                "l0", f"l1-{parent // 5}", f"l2-{parent}", f"l3-{leaf}",
            )),
        )


def resolve(scheduler, reservation):
    ticket = reservation.ticket
    number = int(reservation.candidate.node.name.split("-")[-1])
    if number % 7 == 0:
        scheduler.on_tcp_outcome(ticket, responsive=False)
        scheduler.on_tcp_outcome(ticket, responsive=False)
        return
    scheduler.on_tcp_outcome(ticket, responsive=True)
    if number % 11 == 0:
        scheduler.on_terminal(ticket, outcome="facility_failure")
        return
    if number % 13 == 0:
        scheduler.on_fast_observation(ticket, right_censored=True, busy_worker_ms=3000)
        scheduler.on_fast_yield(ticket, right_censored=True, busy_worker_ms=3000)
        scheduler.on_fast_yield(ticket, right_censored=True, busy_worker_ms=3000)
        scheduler.on_terminal(ticket, outcome="right_censored", stage="deep_probe")
        return
    passed = number % 5 in (0, 1)
    scheduler.on_fast_observation(
        ticket, passed=passed, outcome="pass" if passed else "fail", busy_worker_ms=1200,
    )
    scheduler.on_terminal(ticket, passed=passed, stage="fast_probe")
    scheduler.on_terminal(ticket, passed=passed, stage="fast_probe")
    if passed:
        scheduler.on_commit_complete(ticket, net_new=number % 17 != 0)
        scheduler.on_commit_complete(ticket, net_new=number % 17 != 0)


def trace(scheduler_type, roots, *, ambiguous=False):
    clock = 100.0
    with patch("tier3_candidate_scheduler.time.monotonic", side_effect=lambda: clock):
        scheduler = scheduler_type(max_active_arms_per_family=3)
        scheduler.set_credit_envelope(16, handoff_window=16)
        populate(scheduler, roots, ambiguous=ambiguous)
        results = []
        selected = 0
        pending = []
        for epoch in range(96):
            clock += 0.25
            if epoch == 16:
                populate(scheduler, roots, start=1200, count=600, ambiguous=ambiguous)
            reserved = scheduler.reserve_handoffs(16, control_revision=epoch + 1)
            assert scheduler._cold_leaf_reservation_order is None
            assert scheduler.quick_metrics()["selected_total"] == selected
            for offset, item in enumerate(reserved):
                ticket = item.ticket
                results.append((
                    item.candidate.node.name, ticket.selection_reason,
                    ticket.selection_lane, ticket.visibility_class,
                    ticket.coverage_selected, ticket.unknown_exploration,
                    ticket.reward_scope, ticket.reward_cohort_key,
                ))
                if (epoch + offset) % 9 == 0:
                    scheduler.cancel_handoff(item, requeue=True)
                elif (epoch + offset) % 19 == 0:
                    scheduler.cancel_handoff(item, requeue=False)
                else:
                    scheduler.commit_handoff(item)
                    selected += 1
                    pending.append(item)
            # Feedback arrives after all reservations, in a different order.
            if epoch % 2 == 1:
                for item in reversed(pending):
                    resolve(scheduler, item)
                pending.clear()
        for item in pending:
            resolve(scheduler, item)
        assert scheduler.quick_metrics()["selected_total"] == selected
        assert selected >= 500
        scheduler.release_outstanding()
        assert scheduler.pending_handoff_count() == 0
        assert scheduler._tickets == {}
        metrics = scheduler.quick_metrics()
        for key in ("fast_credit_outstanding", "parked_deep_debt",
                    "reward_exact_outstanding_count", "reward_service_outstanding_count"):
            assert metrics[key] == 0
        return results


@pytest.mark.parametrize("roots", [1, 2, 5, 17])
@pytest.mark.parametrize("ambiguous", [False, True])
def test_reservation_heap_preserves_full_sort_trace_with_async_feedback(roots, ambiguous):
    assert trace(Tier3CandidateScheduler, roots, ambiguous=ambiguous) == trace(
        FullSortScheduler, roots, ambiguous=ambiguous,
    )


def test_lazy_order_refreshes_decreased_inventory_and_retries_suspended_items():
    counts = {"first": 5, "second": 4, "third": 4}
    sampled = set()
    order = _ColdLeafReservationOrder()

    def priority(item):
        return (int(item[0] in sampled), 0, -item[1], 0)

    def ranked():
        return order.ordered(counts.items(), counts, lambda key: counts[key] > 0, priority)

    first_call = iter(ranked())
    assert next(first_call) == ("first", 5)
    assert next(first_call) == ("second", 4)
    first_call.close()
    counts["first"] = 1
    sampled.add("second")
    assert list(ranked()) == [("third", 4), ("first", 1), ("second", 4)]


def test_exception_does_not_retain_reservation_order():
    scheduler = Tier3CandidateScheduler()
    populate(scheduler, 5, count=100)
    with patch.object(scheduler, "_prune_terminal_tickets", side_effect=RuntimeError("injected")):
        with pytest.raises(RuntimeError, match="injected"):
            scheduler.reserve_handoffs(16)
    assert scheduler._cold_leaf_reservation_order is None
    assert scheduler.quick_metrics()["selected_total"] == 0
    reservations = scheduler.reserve_handoffs(16)
    assert reservations
    for item in reservations:
        scheduler.cancel_handoff(item, requeue=True)
    assert scheduler._cold_leaf_reservation_order is None
    assert scheduler.quick_metrics()["selected_total"] == 0


def test_one_reservation_does_not_rerank_the_inventory_for_every_pick():
    scheduler = Tier3CandidateScheduler()
    scheduler.set_credit_envelope(16, handoff_window=16)
    populate(scheduler, 17, count=3000)
    calls = 0
    original = _ColdLeafReservationOrder.ordered

    def measured(self, initial, inventory, eligible, priority):
        def counted(item):
            nonlocal calls
            calls += 1
            return priority(item)
        return original(self, initial, inventory, eligible, counted)

    with patch.object(_ColdLeafReservationOrder, "ordered", measured):
        reserved = scheduler.reserve_handoffs(16)
    assert len(reserved) == 16
    assert sum(item.ticket.selection_reason == "cold_leaf_coverage" for item in reserved) == 8
    assert 0 < calls <= 240 + 4 * len(reserved)
    for item in reserved:
        scheduler.cancel_handoff(item, requeue=True)
