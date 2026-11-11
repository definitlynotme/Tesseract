from __future__ import annotations

from typing import Any

from models import NodeStatus
from tier3_origin import is_tier3_user_pool_node


def tier3_node_score(node: Any) -> tuple[float, float, str]:
    startup_rank = node.extra.get("startup_export_rank")
    if node.extra.get("startup_prelaunch_validation") == "tcp" and startup_rank is not None:
        try:
            rank_value = max(0, int(startup_rank))
        except (TypeError, ValueError):
            rank_value = 999999
        rank_score = max(0.0, 1000.0 - float(rank_value))
    else:
        rank_score = 0.0
    status_score = {
        NodeStatus.ALIVE: 1.0,
        NodeStatus.SLOW: 0.75,
        NodeStatus.UNKNOWN: 0.45,
        NodeStatus.SUSPICIOUS: 0.2,
        NodeStatus.DEAD: 0.0,
    }.get(node.status, 0.0)
    origin_bonus = 0.4 if is_tier3_user_pool_node(node) and node.status in {NodeStatus.ALIVE, NodeStatus.SLOW, NodeStatus.UNKNOWN} else 0.0
    bonus = origin_bonus
    bonus += float(node.extra.get("tier3_score_bonus", 0) or 0)
    latency_penalty = min(float(node.latency_ms or 9999), 9999.0) / 10000.0
    return (rank_score + status_score + bonus - latency_penalty, -latency_penalty, node.name or node.address)


def sort_tier3_nodes(nodes: list[Any]) -> list[Any]:
    return sorted(nodes, key=tier3_node_score, reverse=True)


def select_tier3_startup_export_nodes(nodes: list[Any], limit: int) -> list[Any]:
    sorted_nodes = sort_tier3_nodes(nodes)
    buckets: dict[tuple[str, str], list[Any]] = {}
    for node in sorted_nodes:
        key = (str(node.source or node.extra.get("source_type") or ""), str(node.protocol or ""))
        buckets.setdefault(key, []).append(node)
    selected: list[Any] = []
    seen: set[tuple[str, ...]] = set()
    while len(selected) < limit and buckets:
        progressed = False
        for key in list(buckets):
            bucket = buckets.get(key) or []
            if not bucket:
                buckets.pop(key, None)
                continue
            node = bucket.pop(0)
            if node.dedupe_key in seen:
                progressed = True
                continue
            node.extra["startup_export_rank"] = len(selected)
            selected.append(node)
            seen.add(node.dedupe_key)
            progressed = True
            if len(selected) >= limit:
                break
        if not progressed:
            break
    return selected
