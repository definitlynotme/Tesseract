from __future__ import annotations

from typing import Any

from models import Node, count_healthy_nodes


def state_mode(
    *,
    tier1_total: int,
    tier1_alive: int,
    tier2_alive: int,
    tier3_alive: int,
) -> tuple[str, str]:
    if tier1_total and tier1_alive == tier1_total:
        return "tier1_healthy", "tier1"
    if tier1_alive > 0:
        return "tier1_degraded", "tier1"
    if tier2_alive > 0:
        return "tier2_active", "tier2"
    if tier3_alive > 0:
        return "tier3_active", "tier3"
    return "offline", "offline"


def state_metrics(
    *,
    tier1_group_stats: dict[str, int],
    tier2_nodes: list[Node],
    tier3_display_nodes: list[Node],
    tier3_enabled: bool,
    exit_nodes: list[Node],
    exit_configured: bool,
    exit_active: bool | None,
    effective_egress_ok: bool | None,
    effective_egress_reason: str,
) -> dict[str, Any]:
    tier1_alive = tier1_group_stats["alive"]
    tier2_alive = count_healthy_nodes(tier2_nodes)
    tier3_alive = count_healthy_nodes(tier3_display_nodes)
    if not tier3_enabled:
        tier3_alive = 0
    exit_alive = count_healthy_nodes(exit_nodes)
    tier1_total = tier1_group_stats["total"]
    mode, active_tier = state_mode(
        tier1_total=tier1_total,
        tier1_alive=tier1_alive,
        tier2_alive=tier2_alive,
        tier3_alive=tier3_alive,
    )
    exit_available = exit_configured or bool(exit_nodes)
    final_exit_active = exit_available if exit_active is None else bool(exit_active and exit_available)

    return {
        "mode": mode,
        "active_tier": active_tier,
        "tier1_alive": tier1_alive,
        "tier1_total": tier1_total,
        "tier1_variant_alive": tier1_group_stats["variant_alive"],
        "tier1_variant_total": tier1_group_stats["variant_total"],
        "tier2_alive": tier2_alive,
        "tier2_total": len(tier2_nodes),
        "tier3_alive": tier3_alive,
        "tier3_total": len(tier3_display_nodes) if tier3_enabled else 0,
        "exit_alive": exit_alive,
        "exit_total": len(exit_nodes),
        "exit_configured": exit_available,
        "final_exit": "exit" if final_exit_active else active_tier,
        "effective_egress_ok": effective_egress_ok,
        "effective_egress_reason": effective_egress_reason,
    }


def state_summary(metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "active_tier": metrics["active_tier"],
        "mode": metrics["mode"],
        "tier1_alive": metrics["tier1_alive"],
        "tier2_alive": metrics["tier2_alive"],
        "tier3_alive": metrics["tier3_alive"],
        "exit_alive": metrics["exit_alive"],
        "final_exit": metrics["final_exit"],
    }
