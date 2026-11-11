import math

import pytest

from scripts.tier3_audit_deadline import Tier3AuditDeadline


def test_slow_gui_setup_cannot_consume_the_requested_observation_window():
    guard = Tier3AuditDeadline(100, 90)
    assert guard.limit(now=240) == ("startup", 280)
    assert guard.limit(now=270, window_started_at=250) == ("observation", 370)
    assert guard.limit(now=371, window_started_at=250) == ("observation", 370)


def test_missing_window_does_not_produce_an_unbounded_wait():
    assert Tier3AuditDeadline(100, 90).limit(now=300) == ("startup", 280)


@pytest.mark.parametrize("started", [99, 281, 400, math.inf, math.nan])
def test_stale_or_invalid_window_markers_cannot_extend_the_guard(started):
    with pytest.raises(ValueError):
        Tier3AuditDeadline(100, 90).limit(now=300, window_started_at=started)


@pytest.mark.parametrize("seconds", [-1, math.inf, math.nan])
def test_invalid_durations_are_rejected(seconds):
    with pytest.raises(ValueError):
        Tier3AuditDeadline(100, seconds)
