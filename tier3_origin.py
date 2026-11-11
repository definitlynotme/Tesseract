from __future__ import annotations

from typing import Any


TIER3_USER_ORIGIN = "tier3a"


def tier3_origin(node: Any) -> str:
    extra = getattr(node, "extra", {})
    if not isinstance(extra, dict):
        return ""
    return str(extra.get("tier3_origin") or extra.get("source_origin") or "").strip()


def is_tier3_user_pool_node(node: Any) -> bool:
    return str(getattr(node, "tier", "") or "") == "tier3" and tier3_origin(node) == TIER3_USER_ORIGIN


def is_tier3_auto_pool_node(node: Any) -> bool:
    return str(getattr(node, "tier", "") or "") == "tier3" and not is_tier3_user_pool_node(node)


def tier3_auto_pool_nodes(nodes: list[Any]) -> list[Any]:
    return [node for node in nodes if is_tier3_auto_pool_node(node)]


def tier3_user_pool_nodes(nodes: list[Any]) -> list[Any]:
    return [node for node in nodes if is_tier3_user_pool_node(node)]
