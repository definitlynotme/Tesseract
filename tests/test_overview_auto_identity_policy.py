"""Automatic exit-IP lookups stay rare, settled, visible-only and bounded."""
from __future__ import annotations

from dataclasses import replace

import pytest

from overview_auto_identity import (
    AUTO_FAILURE_BACKOFF_SECONDS,
    AUTO_HOURLY_CAP,
    AUTO_MIN_SPACING_SECONDS,
    AUTO_RATE_LIMIT_PAUSE_SECONDS,
    AUTO_REFRESH_AFTER_SECONDS,
    AUTO_SETTLE_SECONDS,
    AUTO_SUSPEND_SECONDS,
    AutoLookupInputs,
    OverviewAutoIdentityScheduler,
)


def _inputs(now, **overrides):
    base = AutoLookupInputs(
        now=now, enabled=True, visible=True, readiness_state="ready",
        validation_active=False, route_confirmed=True, binding_marker="route-a",
        core_instance="1:1:101", busy=False, current_result_age=None,
    )
    return replace(base, **overrides)


def _settled(scheduler, now=100.0, **overrides):
    scheduler.decide(_inputs(now, **overrides))
    return now + AUTO_SETTLE_SECONDS


def test_waits_for_the_ready_binding_to_settle():
    scheduler = OverviewAutoIdentityScheduler()
    decision = scheduler.decide(_inputs(100.0))
    assert not decision.start and decision.reason == "wait_settle"
    assert decision.retry_at == pytest.approx(100.0 + AUTO_SETTLE_SECONDS)
    assert scheduler.decide(_inputs(100.0 + AUTO_SETTLE_SECONDS)).start


@pytest.mark.parametrize("override,reason", [
    ({"enabled": False}, "disabled"),
    ({"visible": False}, "not_visible"),
    ({"readiness_state": "serving"}, "not_ready"),
    ({"readiness_state": "data_plane_failed"}, "not_ready"),
    ({"validation_active": True}, "not_ready"),
    ({"route_confirmed": False}, "not_ready"),
    ({"binding_marker": ""}, "not_ready"),
    ({"busy": True}, "busy"),
])
def test_only_a_visible_enabled_idle_ready_route_is_eligible(override, reason):
    scheduler = OverviewAutoIdentityScheduler()
    now = _settled(scheduler, **override)
    decision = scheduler.decide(_inputs(now, **override))
    assert not decision.start
    assert decision.reason == reason


def test_one_lookup_per_binding_until_the_result_is_old():
    scheduler = OverviewAutoIdentityScheduler()
    now = _settled(scheduler)
    fresh = scheduler.decide(_inputs(now, current_result_age=60.0))
    assert not fresh.start and fresh.reason == "fresh"
    assert scheduler.decide(_inputs(now, current_result_age=AUTO_REFRESH_AFTER_SECONDS)).start


def test_binding_change_restarts_the_settle_window():
    scheduler = OverviewAutoIdentityScheduler()
    now = _settled(scheduler)
    assert scheduler.decide(_inputs(now)).start
    moved = scheduler.decide(_inputs(now + 1, binding_marker="route-b"))
    assert not moved.start and moved.reason == "wait_settle"


def test_spacing_and_hourly_cap_bound_total_starts():
    scheduler = OverviewAutoIdentityScheduler()
    now = _settled(scheduler)
    starts = 0
    t = now
    while t < now + 3600:
        if scheduler.decide(_inputs(t)).start:
            scheduler.record_start(t)
            starts += 1
            scheduler.record_outcome(now=t, binding_marker="route-a", core_instance="1:1:101",
                                     success=False, skipped_reason="auto_ready_lost")
        t += 1.0
    assert starts == AUTO_HOURLY_CAP
    scheduler2 = OverviewAutoIdentityScheduler()
    now2 = _settled(scheduler2)
    scheduler2.record_start(now2)
    spaced = scheduler2.decide(_inputs(now2 + 1))
    assert not spaced.start and spaced.reason in {"spacing", "wait_settle"}
    assert spaced.retry_at >= now2 + AUTO_MIN_SPACING_SECONDS - 1e-9 or spaced.reason == "wait_settle"


def test_failures_back_off_then_suspend_until_the_binding_changes():
    scheduler = OverviewAutoIdentityScheduler()
    now = _settled(scheduler)
    for index, step in enumerate(AUTO_FAILURE_BACKOFF_SECONDS, start=1):
        scheduler.record_start(now)
        scheduler.record_outcome(now=now, binding_marker="route-a", core_instance="1:1:101",
                                 success=False, skipped_reason="identity_unavailable")
        decision = scheduler.decide(_inputs(now + 1))
        assert not decision.start
        if index < len(AUTO_FAILURE_BACKOFF_SECONDS):
            assert decision.reason in {"backoff", "spacing"}
            now = now + max(step, AUTO_MIN_SPACING_SECONDS)
        else:
            assert decision.reason == "suspended"
            assert decision.retry_at == pytest.approx(now + AUTO_SUSPEND_SECONDS)
    other = OverviewAutoIdentityScheduler()
    other._failures = dict(scheduler._failures)
    later = _settled(other, now + 100, binding_marker="route-b")
    assert other.decide(_inputs(later, binding_marker="route-b")).start


def test_skipped_outcomes_never_count_as_failures():
    scheduler = OverviewAutoIdentityScheduler()
    now = _settled(scheduler)
    for reason in ("auto_not_ready", "auto_ready_lost", "route_changed", "route_unconfirmed"):
        scheduler.record_outcome(now=now, binding_marker="route-a", core_instance="1:1:101",
                                 success=False, skipped_reason=reason)
    assert ("route-a", "1:1:101") not in scheduler._failures


def test_rate_limit_pauses_every_automatic_lookup():
    scheduler = OverviewAutoIdentityScheduler()
    now = _settled(scheduler)
    scheduler.record_outcome(now=now, binding_marker="route-a", core_instance="1:1:101",
                             success=False, skipped_reason="identity_rate_limited",
                             rate_limited=True, retry_after=900)
    later = _settled(scheduler, now + 30, binding_marker="route-b")
    decision = scheduler.decide(_inputs(later, binding_marker="route-b"))
    assert not decision.start and decision.reason == "rate_limited"
    assert decision.retry_at == pytest.approx(now + 900)
    assert AUTO_RATE_LIMIT_PAUSE_SECONDS <= 900


def test_a_click_lifts_the_binding_backoff():
    scheduler = OverviewAutoIdentityScheduler()
    now = _settled(scheduler)
    scheduler.record_outcome(now=now, binding_marker="route-a", core_instance="1:1:101",
                             success=False, skipped_reason="identity_timeout")
    scheduler.record_user_request(binding_marker="route-a", core_instance="1:1:101")
    assert scheduler.decide(_inputs(now + 1)).start
