from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Tier3StartRecoveryAction(Enum):
    NONE = "none"
    UNAVAILABLE = "unavailable"
    REVALIDATE_CACHED_POOL = "revalidate_cached_pool"
    COLLECT_POOL = "collect_pool"


@dataclass(frozen=True)
class Tier3StartRecoveryPlan:
    recovery_required: bool
    action: Tier3StartRecoveryAction
    target_healthy_count: int
    reason: str


def tier3_start_recovery_plan(
    *,
    primary_ready: bool,
    tier3_enabled: bool,
    cached_healthy_count: int,
    target_healthy_count: int,
    primary_failure: str | None,
    cached_revalidation_count: int = 0,
) -> Tier3StartRecoveryPlan:
    target = max(1, int(target_healthy_count or 1))
    if primary_ready:
        return Tier3StartRecoveryPlan(
            recovery_required=False,
            action=Tier3StartRecoveryAction.NONE,
            target_healthy_count=target,
            reason="primary layers are ready",
        )
    if not tier3_enabled:
        reason = (primary_failure or "primary layers are unavailable") + "; Bastion Layer is disabled"
        return Tier3StartRecoveryPlan(
            recovery_required=True,
            action=Tier3StartRecoveryAction.UNAVAILABLE,
            target_healthy_count=target,
            reason=reason,
        )
    if cached_healthy_count > 0 or cached_revalidation_count > 0:
        return Tier3StartRecoveryPlan(
            recovery_required=True,
            action=Tier3StartRecoveryAction.REVALIDATE_CACHED_POOL,
            target_healthy_count=target,
            reason="Bastion Layer cached pool must be revalidated",
        )
    return Tier3StartRecoveryPlan(
        recovery_required=True,
        action=Tier3StartRecoveryAction.COLLECT_POOL,
        target_healthy_count=target,
        reason="Bastion Layer pool must be collected",
    )
