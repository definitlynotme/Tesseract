from __future__ import annotations

from copy import deepcopy
from unittest.mock import patch

import pytest

from tier3_candidate_scheduler import _CohortYieldState
from tests.test_tier3_cold_inventory_cost import inventory


def diagnostic_inventory(count=512):
    scheduler = inventory(5)
    for reservation in scheduler.reserve_handoffs(16)[:8]:
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
    # Synthetic diagnostic states exercise ranking, not learning/promotion.
    for scope in ("service", "similar"):
        for index in range(count):
            key = f"{scope}:diagnostic-{index:05d}"
            state = _CohortYieldState(scope=scope)
            state.samples.extend(
                (100.0 + offset, offset % 3 == 0, offset % 5 == 0, 750)
                for offset in range(index % 65)
            )
            state.production_samples.extend(
                (150.0 + offset, offset % 2 == 0, offset % 3 == 0, 250)
                for offset in range(index % 17)
            )
            scheduler._cohort_yield_states[key] = state
            scheduler._cohort_queued_inventory[key] = index % 19
    return scheduler


@pytest.mark.parametrize("scope", ["service", "similar", "absent"])
@pytest.mark.parametrize("limit", [-1, 1, 8, 1000])
def test_top_diagnostics_matches_full_projection_without_state_changes(scope, limit):
    scheduler = diagnostic_inventory(80)
    before = deepcopy(scheduler._cohort_yield_states)
    full = scheduler._top_cohort_diagnostics_unlocked(scope, limit=10000)
    expected = sorted(
        full,
        key=lambda row: (
            row["window_pass_count"], row["window_completion_count"],
            row["window_sample_count"], row["cohort_id"],
        ),
        reverse=True,
    )[:max(1, limit)]
    actual = scheduler._top_cohort_diagnostics_unlocked(scope, limit=limit)
    assert actual == expected
    assert scheduler._cohort_yield_states == before
    assert scheduler._selected_total == 8
    scheduler.release_outstanding()


def test_top_diagnostics_expands_inventory_only_for_reported_rows():
    scheduler = diagnostic_inventory()
    with patch.object(
        scheduler, "_cohort_inventory_unlocked",
        wraps=scheduler._cohort_inventory_unlocked,
    ) as expand:
        rows = scheduler._top_cohort_diagnostics_unlocked("service", limit=8)
    assert len(rows) == 8
    assert expand.call_count == len(rows)
    assert [call.args[0].split(":", 1)[-1] for call in expand.call_args_list] == [
        row["cohort_id"] for row in rows
    ]
    scheduler.release_outstanding()


def test_reported_inventory_still_includes_reserved_and_active_tickets():
    scheduler = inventory(5)
    reservations = scheduler.reserve_handoffs(16)
    for reservation in reservations[:8]:
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
    for key in scheduler._cohort_queued_inventory:
        if key.startswith("service:"):
            scheduler._cohort_yield_states[key] = _CohortYieldState(scope="service")
    rows = scheduler._top_cohort_diagnostics_unlocked("service", limit=10000)
    assert rows
    assert sum(row["inventory_reserved"] for row in rows) > 0
    assert sum(row["inventory_active"] for row in rows) > 0
    for row in rows:
        expected = scheduler._cohort_inventory_unlocked(f"service:{row['cohort_id']}")
        assert row["inventory_reservable"] == expected["reservable"]
        assert row["inventory_reserved"] == expected["reserved"]
        assert row["inventory_active"] == expected["active"]
        assert row["inventory_root_family_count"] == expected["root_family_count"]
    scheduler.release_outstanding()
