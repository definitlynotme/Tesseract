from __future__ import annotations

from typing import Any

from models import NodeStatus


TIER_IDS = ("tier1", "tier2", "tier3", "exit", "all")


def disabled_killswitch_state() -> dict[str, Any]:
    return {
        "state": "disabled",
        "manual_override": "none",
        "active": False,
        "admin_available": False,
        "enabled": False,
        "supported": False,
        "whitelist": {},
    }


def subscription_urls(host: str, port: int, *, client_mode: str) -> dict[str, Any]:
    return {
        "preferred_format": "clash" if client_mode == "clash" else "v2ray",
        "v2ray": {
            tier_name: f"http://{host}:{port}/sub/{tier_name}"
            for tier_name in TIER_IDS
        },
        "clash": {
            **{
                tier_name: f"http://{host}:{port}/clash/{tier_name}"
                for tier_name in TIER_IDS
            },
            "config": f"http://{host}:{port}/clash/config",
        },
    }


def exit_stats_snapshot(*, configured: bool, nodes: list[Any]) -> dict[str, Any]:
    return {
        "configured": bool(configured or nodes),
        "total": len(nodes),
        "alive": sum(1 for node in nodes if getattr(node, "status", None) == NodeStatus.ALIVE),
        "slow": sum(1 for node in nodes if getattr(node, "status", None) == NodeStatus.SLOW),
        "dead": sum(1 for node in nodes if getattr(node, "status", None) == NodeStatus.DEAD),
        "suspicious": sum(1 for node in nodes if getattr(node, "status", None) == NodeStatus.SUSPICIOUS),
    }


def runtime_tiers_snapshot(
    *,
    metrics: dict[str, Any],
    tier3_stats: dict[str, Any],
    exit_stats: dict[str, Any],
    last_check_at: dict[str, Any],
    tier2_last_refresh_at: str | None,
    tier3_last_collection_at: str | None,
    tier3_source_count: int,
    exit_last_refresh_at: str | None,
    include_nodes: bool,
    tier1_nodes: list[Any],
    tier1_protocol_groups: list[dict[str, Any]],
    tier2_nodes: list[Any],
    tier3_nodes: list[Any],
    tier3_enabled: bool,
    exit_nodes: list[Any],
) -> dict[str, Any]:
    tier1 = {
        "alive": metrics["tier1_alive"],
        "total": metrics["tier1_total"],
        "variant_alive": metrics["tier1_variant_alive"],
        "variant_total": metrics["tier1_variant_total"],
        "last_check_at": last_check_at["tier1"],
    }
    tier2 = {
        "alive": metrics["tier2_alive"],
        "total": metrics["tier2_total"],
        "last_check_at": last_check_at["tier2"],
        "last_refresh_at": tier2_last_refresh_at,
    }
    tier3 = {
        **tier3_stats,
        "last_check_at": last_check_at["tier3"],
        "last_collection_at": tier3_last_collection_at,
        "source_count": tier3_source_count,
    }
    exit_tier = {
        **exit_stats,
        "last_check_at": last_check_at["exit"],
        "last_refresh_at": exit_last_refresh_at,
    }
    if include_nodes:
        tier1["nodes"] = [node.to_dict() for node in tier1_nodes]
        tier1["protocol_groups"] = tier1_protocol_groups
        tier2["nodes"] = [node.to_dict() for node in tier2_nodes]
        tier3["nodes"] = [node.to_dict() for node in tier3_nodes] if tier3_enabled else []
        exit_tier["nodes"] = [node.to_dict() for node in exit_nodes]
    return {
        "tier1": tier1,
        "tier2": tier2,
        "tier3": tier3,
        "exit": exit_tier,
    }


def runtime_snapshot_payload(
    *,
    metrics: dict[str, Any],
    uptime_seconds: int,
    started_at: str,
    last_overall_check_at: str | None,
    last_check_at: dict[str, Any],
    client_mode: str,
    clash_config_path: str,
    kill_switch_state: dict[str, Any],
    tiers: dict[str, Any],
    background_actions: dict[str, Any],
    subscriptions: dict[str, Any],
) -> dict[str, Any]:
    return {
        "system": {
            "mode": metrics["mode"],
            "active_tier": metrics["active_tier"],
            "final_exit": metrics["final_exit"],
            "exit_configured": metrics["exit_configured"],
            "effective_egress_ok": metrics["effective_egress_ok"],
            "effective_egress_reason": metrics["effective_egress_reason"],
            "uptime_seconds": uptime_seconds,
            "started_at": started_at,
            "last_overall_check_at": last_overall_check_at,
            "last_check_at": last_check_at,
        },
        "client": {
            "mode": client_mode,
            "clash_config_path": clash_config_path,
        },
        "killswitch": kill_switch_state,
        "tiers": tiers,
        "actions": dict(background_actions),
        "subscriptions": subscriptions,
    }
