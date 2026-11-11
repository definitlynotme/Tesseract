from __future__ import annotations

from collections.abc import Mapping

from models import Node


def tier3_subscription_nodes(nodes: list[Node], *, tier3_enabled: bool) -> list[Node]:
    if not tier3_enabled:
        return []
    # This is the public inventory/subscription view. Runtime eligibility is
    # deliberately enforced only by DesktopRuntime's private Core export view,
    # so failed nodes stay visible here and remain available for periodic
    # health checks and automatic promotion after recovery.
    return list(nodes)


def subscription_nodes_for_tier(
    runtime_nodes: Mapping[str, list[Node]],
    tier_id: str,
    *,
    tier3_enabled: bool,
) -> list[Node]:
    if tier_id == "tier1":
        return list(runtime_nodes["tier1"])
    if tier_id == "tier2":
        return list(runtime_nodes["tier2"])
    if tier_id == "tier3":
        return tier3_subscription_nodes(list(runtime_nodes["tier3"]), tier3_enabled=tier3_enabled)
    if tier_id == "exit":
        return list(runtime_nodes["exit"])
    if tier_id == "all":
        return [
            *runtime_nodes["tier1"],
            *runtime_nodes["tier2"],
            *tier3_subscription_nodes(list(runtime_nodes["tier3"]), tier3_enabled=tier3_enabled),
            *runtime_nodes["exit"],
        ]
    return []
