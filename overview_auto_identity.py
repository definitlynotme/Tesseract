"""When the overview may look up the exit IP without a click.

The card should show the current exit IP the way whoer.net does, without the
user having to ask - but every lookup is real traffic through a free exit to
third-party IP services. This scheduler keeps that cost explicit and bounded.
It holds no Qt or network code; the window feeds it observations and outcomes.

An automatic lookup only reads identity through a route the runtime already
holds READY (never the canonical validation, never a selector switch).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

AUTO_SETTLE_SECONDS = 5.0
AUTO_MIN_SPACING_SECONDS = 20.0
AUTO_HOURLY_CAP = 12
AUTO_REFRESH_AFTER_SECONDS = 1800.0
AUTO_FAILURE_BACKOFF_SECONDS = (30.0, 60.0, 120.0, 240.0, 600.0)
AUTO_MAX_CONSECUTIVE_FAILURES = 5
AUTO_SUSPEND_SECONDS = 1800.0
AUTO_RATE_LIMIT_PAUSE_SECONDS = 300.0
AUTO_RATE_LIMIT_MAX_PAUSE_SECONDS = 3600.0

# Outcomes that say nothing about the exit: the lookup could not run on a
# settled READY route. They restart the settle window but never back off.
AUTO_SKIPPED_REASONS = frozenset({
    "auto_not_ready", "auto_ready_lost", "route_changed", "route_unconfirmed",
    "validation_cancelled", "manual_validation_execution_changed",
})


@dataclass(frozen=True)
class AutoLookupInputs:
    now: float
    enabled: bool
    visible: bool
    readiness_state: str
    validation_active: bool
    route_confirmed: bool
    binding_marker: str
    core_instance: str
    busy: bool
    # Age in seconds of the displayed usable result for this exact binding,
    # or None when the card shows nothing current for it.
    current_result_age: float | None
    # When that result is due for an automatic refresh.
    current_result_refresh_after: float = AUTO_REFRESH_AFTER_SECONDS


@dataclass(frozen=True)
class AutoLookupDecision:
    start: bool
    reason: str
    retry_at: float | None = None


class OverviewAutoIdentityScheduler:
    def __init__(self) -> None:
        self._settle_key: tuple[str, str, str] | None = None
        self._settle_since = 0.0
        self._starts: deque[float] = deque()
        self._last_start: float | None = None
        self._failures: dict[tuple[str, str], tuple[int, float]] = {}
        self._pause_until = 0.0

    def decide(self, inputs: AutoLookupInputs) -> AutoLookupDecision:
        now = float(inputs.now)
        binding = (str(inputs.binding_marker or ""), str(inputs.core_instance or ""))
        settle_key = (*binding, str(inputs.readiness_state or ""))
        if settle_key != self._settle_key:
            self._settle_key = settle_key
            self._settle_since = now
        if not inputs.enabled:
            return AutoLookupDecision(False, "disabled")
        if not inputs.visible:
            return AutoLookupDecision(False, "not_visible")
        if (
            inputs.readiness_state != "ready"
            or inputs.validation_active
            or not inputs.route_confirmed
            or not binding[0]
        ):
            return AutoLookupDecision(False, "not_ready")
        if inputs.busy:
            return AutoLookupDecision(False, "busy")
        refresh_after = float(inputs.current_result_refresh_after)
        if inputs.current_result_age is not None and inputs.current_result_age < refresh_after:
            return AutoLookupDecision(
                False, "fresh", now + (refresh_after - inputs.current_result_age),
            )
        waits: list[tuple[str, float]] = []
        settled_at = self._settle_since + AUTO_SETTLE_SECONDS
        if now < settled_at:
            waits.append(("wait_settle", settled_at))
        if now < self._pause_until:
            waits.append(("rate_limited", self._pause_until))
        failures, last_failure = self._failures.get(binding, (0, 0.0))
        if failures >= AUTO_MAX_CONSECUTIVE_FAILURES:
            resume = last_failure + AUTO_SUSPEND_SECONDS
            if now < resume:
                return AutoLookupDecision(False, "suspended", resume)
            self._failures.pop(binding, None)
        elif failures:
            step = AUTO_FAILURE_BACKOFF_SECONDS[min(failures, len(AUTO_FAILURE_BACKOFF_SECONDS)) - 1]
            if now < last_failure + step:
                waits.append(("backoff", last_failure + step))
        if self._last_start is not None and now < self._last_start + AUTO_MIN_SPACING_SECONDS:
            waits.append(("spacing", self._last_start + AUTO_MIN_SPACING_SECONDS))
        while self._starts and self._starts[0] <= now - 3600.0:
            self._starts.popleft()
        if len(self._starts) >= AUTO_HOURLY_CAP:
            waits.append(("hourly_cap", self._starts[0] + 3600.0))
        if waits:
            reason, retry_at = max(waits, key=lambda item: item[1])
            return AutoLookupDecision(False, reason, retry_at)
        return AutoLookupDecision(True, "start")

    def record_start(self, now: float) -> None:
        self._last_start = float(now)
        self._starts.append(float(now))

    def record_user_lookup(self, now: float) -> None:
        """A finished click keeps automatic lookups spaced after it.

        It does not use the hourly budget; its outcome is reported through
        record_outcome like an automatic one (back-off, rate-limit pause).
        """
        now = float(now)
        self._last_start = now if self._last_start is None else max(self._last_start, now)

    def record_outcome(
        self,
        *,
        now: float,
        binding_marker: str,
        core_instance: str,
        success: bool,
        skipped_reason: str = "",
        rate_limited: bool = False,
        retry_after: float | None = None,
    ) -> None:
        now = float(now)
        binding = (str(binding_marker or ""), str(core_instance or ""))
        if success:
            self._failures.pop(binding, None)
            return
        if skipped_reason in AUTO_SKIPPED_REASONS:
            # Wait for the route to settle again; not evidence about the exit.
            self._settle_key = None
            return
        if rate_limited:
            pause = AUTO_RATE_LIMIT_PAUSE_SECONDS
            if retry_after is not None and retry_after > 0:
                pause = max(pause, float(retry_after))
            self._pause_until = max(self._pause_until, now + min(pause, AUTO_RATE_LIMIT_MAX_PAUSE_SECONDS))
        failures, _last = self._failures.get(binding, (0, 0.0))
        self._failures[binding] = (failures + 1, now)

    def record_user_request(self, *, binding_marker: str, core_instance: str) -> None:
        """A click lifts the per-binding back-off; the user asked explicitly."""
        self._failures.pop((str(binding_marker or ""), str(core_instance or "")), None)
