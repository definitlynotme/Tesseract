"""Separate bounded GUI setup from the requested Tier3 observation window."""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True, slots=True)
class Tier3AuditDeadline:
    launched_at: float
    observation_seconds: float
    startup_seconds: float = 180.0
    cleanup_seconds: float = 30.0

    def __post_init__(self) -> None:
        for value in (self.launched_at, self.observation_seconds,
                      self.startup_seconds, self.cleanup_seconds):
            if not math.isfinite(value) or value < 0:
                raise ValueError("audit deadline requires finite non-negative times")

    def limit(self, *, now: float, window_started_at: float | None = None) -> tuple[str, float]:
        startup_end = self.launched_at + self.startup_seconds
        if window_started_at is None:
            return "startup", startup_end
        if (not math.isfinite(window_started_at)
                or not self.launched_at <= window_started_at <= min(now, startup_end)):
            raise ValueError("audit observation marker is stale, late or in the future")
        return "observation", window_started_at + self.observation_seconds + self.cleanup_seconds
