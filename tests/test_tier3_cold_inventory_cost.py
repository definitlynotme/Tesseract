from __future__ import annotations

import builtins
from unittest.mock import patch

import pytest

from tier3_candidate_scheduler import Tier3CandidateScheduler
from tests.test_tier3_candidate_scheduler import candidate, feature
from tests.test_tier3_cold_discovery_order import FullSortScheduler
from tests.test_tier3_exact_inventory_index import complete


def inventory(roots, count=600, scheduler_type=Tier3CandidateScheduler):
    scheduler = scheduler_type()
    scheduler.set_credit_envelope(16, handoff_window=16)
    for index in range(count):
        leaf = index % 96
        scheduler.enqueue(
            candidate(f"node-{index}"), None, arm_key=f"arm-{index % 120}",
            source_root=f"root-{index % roots}", family=f"family-{index % 3}",
            coarse_key=f"coarse-{index % 120}",
            feature_key=feature(service_profiles=(
                "l0", f"l1-{leaf % 4}", f"l2-{leaf % 24}", f"l3-{leaf}",
            )),
        )
    return scheduler


def test_cold_discovery_ranks_only_eligible_leaves():
    scheduler = inventory(5)
    initial = scheduler.reserve_handoffs(16)
    for reservation in initial[:8]:
        complete(scheduler, reservation, passed=False)
    reserved = {
        state.ticket.service_cohort_key for state in scheduler._tickets.values()
        if state.reservation_open
    }
    ranked = []

    def observe(items, *, key=None, **kwargs):
        if key is not None and key.__name__ == "cold_leaf_priority":
            items = list(items)
            ranked.extend(item[0] for item in items)
        return builtins.sorted(items, key=key, **kwargs)

    with patch("tier3_candidate_scheduler.sorted", observe, create=True):
        selected, _ = scheduler._pop_cold_leaf_coverage_candidate_unlocked(
            ignore_credits=False, provisionally_selected_arms=set(),
        )
    assert selected is not None and ranked
    assert all(key.startswith("service:") for key in ranked)
    assert not set(ranked) & scheduler._cohort_queued_direct_children.keys()
    assert not set(ranked) & scheduler._cold_leaf_sampled_keys
    assert not set(ranked) & reserved
    scheduler.release_outstanding()


@pytest.mark.parametrize("roots", [1, 2, 5, 17])
def test_filter_before_sort_preserves_full_inventory_reference_trace(roots):
    traces = []
    for reference in (True, False):
        with patch("tier3_candidate_scheduler.time.monotonic", return_value=100.0):
            scheduler = inventory(roots, scheduler_type=FullSortScheduler)

            def sort(items, *, key=None, **kwargs):
                if reference and key is not None and key.__name__ == "cold_leaf_priority":
                    items = scheduler._cohort_queued_inventory.items()
                return builtins.sorted(items, key=key, **kwargs)

            trace = []
            with patch("tier3_candidate_scheduler.sorted", sort, create=True):
                for epoch in range(24):
                    reservations = scheduler.reserve_handoffs(16)
                    for offset, reservation in enumerate(reservations):
                        if (epoch + offset) % 7 == 0:
                            scheduler.cancel_handoff(reservation, requeue=True)
                        elif (epoch + offset) % 11 == 0:
                            scheduler.cancel_handoff(reservation, requeue=False)
                        else:
                            trace.append((reservation.candidate.node.name,
                                          reservation.ticket.selection_reason))
                            complete(scheduler, reservation, passed=offset % 5 == 0)
            assert scheduler.quick_metrics()["selected_total"] == len(trace)
            scheduler.release_outstanding()
            assert scheduler.pending_handoff_count() == 0
            traces.append(trace)
    assert len(traces[0]) > 200
    assert traces[0] == traces[1]
