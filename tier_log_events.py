"""Tier state-transition events for the operational log.

``ProxyTierManager.evaluate_state`` runs about once a second and is the only
writer of the current mode.  :class:`TierLogTracker` compares each evaluation
with the previous one and returns registered log events for the transitions a
reader cares about: which tier carries traffic, Bastion pool milestones, and
the verified proxy egress.  An unchanged evaluation returns nothing, so the
log never repeats a steady state.

Alert events for the same mode changes are notifications (with their own
wording, cooldowns and flap suppression); these events are the record.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from log_event_reports import log_event


TIER_PRODUCT_NAMES: Mapping[str, str] = {
    "tier1": "Vanguard",
    "tier2": "Sentinel",
    "tier3": "Bastion",
    "exit": "Gateway",
}
MODE_EVENT_KEYS: Mapping[str, str] = {
    "tier1_healthy": "tier.vanguard_healthy",
    "tier1_degraded": "tier.vanguard_degraded",
    "tier2_active": "tier.sentinel_active",
    "tier3_active": "tier.bastion_active",
    "offline": "tier.offline",
}
_MODE_ACTIVE_TIER: Mapping[str, str] = {
    "tier1_healthy": "tier1",
    "tier1_degraded": "tier1",
    "tier2_active": "tier2",
    "tier3_active": "tier3",
}
# "Target reached" re-arms only after the pool falls well below the target,
# so a pool hovering at the threshold does not log every admission.
TARGET_REARM_RATIO = 0.6


@dataclass(frozen=True, slots=True)
class TierLogEvent:
    event_key: str
    level: str
    message: str
    params: Mapping[str, Any] = field(default_factory=dict)


def _count(metrics: Mapping[str, Any], name: str) -> int:
    try:
        return max(0, int(metrics.get(name) or 0))
    except (TypeError, ValueError):
        return 0


@dataclass(slots=True)
class TierLogTracker:
    """Previous-evaluation state; one tracker lives as long as its manager."""

    mode: str | None = None
    tier3_alive: int | None = None
    tier3_target_reached: bool = False
    egress_ok: bool | None = None
    egress_confirmed: bool = False
    egress_failed: bool = False

    def observe(
        self,
        metrics: Mapping[str, Any],
        *,
        tier3_enabled: bool,
        tier3_target: int,
    ) -> list[TierLogEvent]:
        events: list[TierLogEvent] = []
        mode_event = self._mode_event(metrics)
        if mode_event is not None:
            events.append(mode_event)
        events.extend(
            self._pool_events(
                metrics,
                tier3_enabled=tier3_enabled,
                tier3_target=tier3_target,
                mode_event=mode_event,
            )
        )
        events.extend(self._egress_events(metrics))
        return events

    def _mode_event(self, metrics: Mapping[str, Any]) -> TierLogEvent | None:
        mode = str(metrics.get("mode") or "")
        previous = self.mode
        if not mode or mode == previous:
            return None
        self.mode = mode
        if previous is None and mode == "offline":
            # Every cold start begins with empty pools; that baseline is not
            # a loss of service.  The first tier that carries traffic is.
            return None
        event_key = MODE_EVENT_KEYS.get(mode)
        if event_key is None:
            return None
        params: dict[str, Any] = {
            name: _count(metrics, name)
            for name in ("tier1_alive", "tier1_total", "tier2_alive", "tier3_alive")
        }
        previous_tier = _MODE_ACTIVE_TIER.get(previous or "")
        if previous_tier and previous_tier != _MODE_ACTIVE_TIER.get(mode):
            params["previous"] = TIER_PRODUCT_NAMES[previous_tier]
        level = {
            "tier1_healthy": "INFO",
            "offline": "CRITICAL",
        }.get(mode, "WARNING")
        message = (
            f"Tier mode changed: {previous or 'start'} -> {mode} "
            f"(tier1={params['tier1_alive']}/{params['tier1_total']} "
            f"tier2={params['tier2_alive']} tier3={params['tier3_alive']})"
        )
        return TierLogEvent(event_key, level, message, params)

    def _pool_events(
        self,
        metrics: Mapping[str, Any],
        *,
        tier3_enabled: bool,
        tier3_target: int,
        mode_event: TierLogEvent | None,
    ) -> list[TierLogEvent]:
        if not tier3_enabled:
            self.tier3_alive = None
            self.tier3_target_reached = False
            return []
        alive = _count(metrics, "tier3_alive")
        previous = self.tier3_alive
        self.tier3_alive = alive
        try:
            target = max(0, int(tier3_target or 0))
        except (TypeError, ValueError):
            target = 0
        mode_key = mode_event.event_key if mode_event is not None else ""
        events: list[TierLogEvent] = []
        if alive > 0 and not previous:
            # Entering Bastion already states its count; don't say it twice.
            if mode_key != "tier.bastion_active":
                params: dict[str, Any] = {"alive_count": alive}
                if target > 0:
                    params["target_count"] = target
                events.append(
                    TierLogEvent(
                        "tier3.pool_ready",
                        "INFO",
                        f"Bastion pool has usable nodes (alive={alive} target={target})",
                        params,
                    )
                )
        elif alive == 0 and previous:
            if mode_key != "tier.offline":
                events.append(
                    TierLogEvent(
                        "tier3.pool_empty",
                        "WARNING",
                        f"Bastion pool is empty (previous_alive={previous})",
                        {"previous_count": previous},
                    )
                )
        if target > 0:
            if not self.tier3_target_reached and alive >= target:
                self.tier3_target_reached = True
                events.append(
                    TierLogEvent(
                        "tier3.pool_target_reached",
                        "INFO",
                        f"Bastion pool reached its target (alive={alive} target={target})",
                        {"alive_count": alive, "target_count": target},
                    )
                )
            elif self.tier3_target_reached and alive < max(1, int(target * TARGET_REARM_RATIO)):
                self.tier3_target_reached = False
        return events

    def _egress_events(self, metrics: Mapping[str, Any]) -> list[TierLogEvent]:
        value = metrics.get("effective_egress_ok")
        ok = None if value is None else bool(value)
        previous = self.egress_ok
        self.egress_ok = ok
        if ok == previous or ok is None:
            return []
        exit_name = TIER_PRODUCT_NAMES.get(str(metrics.get("final_exit") or ""), "")
        exit_params = {"exit_name": exit_name} if exit_name else {}
        if ok:
            if self.egress_failed:
                self.egress_failed = False
                return [
                    TierLogEvent(
                        "tier.egress_restored",
                        "INFO",
                        f"Proxy egress verified again (exit={exit_name or 'unknown'})",
                        exit_params,
                    )
                ]
            if not self.egress_confirmed:
                self.egress_confirmed = True
                return [
                    TierLogEvent(
                        "tier.egress_ready",
                        "INFO",
                        f"Proxy egress verified (exit={exit_name or 'unknown'})",
                        exit_params,
                    )
                ]
            return []
        # Before the first confirmation a failing check is start-up, not a
        # regression; only a verified egress can be lost.
        if not self.egress_confirmed or self.egress_failed:
            return []
        self.egress_failed = True
        reason = str(metrics.get("effective_egress_reason") or "").strip()
        return [
            TierLogEvent(
                "tier.egress_failed",
                "ERROR",
                f"Proxy egress check failed: {reason or 'no reason reported'}",
                {"reason": reason} if reason else {},
            )
        ]


def emit_tier_log_events(logger: logging.Logger, events: Iterable[TierLogEvent]) -> None:
    for event in events:
        log_event(logger, event.level, event.event_key, event.message, params=event.params)


__all__ = [
    "MODE_EVENT_KEYS",
    "TARGET_REARM_RATIO",
    "TIER_PRODUCT_NAMES",
    "TierLogEvent",
    "TierLogTracker",
    "emit_tier_log_events",
]
