from __future__ import annotations

from typing import Any, Callable

from models import Node, NodeStatus, count_healthy_nodes
from tier1_protocols import group_tier1_nodes


StatusSnapshotFn = Callable[[Node], dict[str, Any]]


def tier1_group_stats(nodes: list[Node]) -> dict[str, int]:
    groups = group_tier1_nodes(nodes)
    alive_groups = 0
    for _, group_nodes in groups:
        if count_healthy_nodes(group_nodes) > 0:
            alive_groups += 1
    return {
        "total": len(groups),
        "alive": alive_groups,
        "variant_total": len(nodes),
        "variant_alive": count_healthy_nodes(nodes),
    }


def tier1_protocol_groups_snapshot(
    nodes: list[Node],
    status_snapshot: StatusSnapshotFn,
) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for physical_name, group_nodes in group_tier1_nodes(nodes):
        groups.append(
            {
                "name": physical_name,
                "total": len(group_nodes),
                "healthy": count_healthy_nodes(group_nodes),
                "fallback_order": list(group_nodes[0].extra.get("fallback_order", []) or []) if group_nodes else [],
                "protocols": [status_snapshot(node) for node in group_nodes],
            }
        )
    return groups


def best_tier1_status_node(nodes: list[Node]) -> Node:
    status_rank = {
        NodeStatus.ALIVE: 0,
        NodeStatus.SLOW: 1,
        NodeStatus.UNKNOWN: 2,
        NodeStatus.SUSPICIOUS: 3,
        NodeStatus.DEAD: 4,
    }

    def latency(node: Node) -> float:
        try:
            return float(node.latency_ms)
        except (TypeError, ValueError):
            return 999999.0

    return min(nodes, key=lambda node: (status_rank.get(node.status, 2), latency(node)))


def latest_node_check(nodes: list[Node]) -> str | None:
    values = [str(node.last_checked_at) for node in nodes if node.last_checked_at]
    return max(values) if values else None


def tier1_status_nodes_snapshot(
    nodes: list[Node],
    status_snapshot: StatusSnapshotFn,
) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for physical_name, group_nodes in group_tier1_nodes(nodes):
        if not group_nodes:
            continue
        best_node = best_tier1_status_node(group_nodes)
        protocols = [status_snapshot(node) for node in group_nodes]
        protocol_labels = [
            str(item.get("protocol_label") or item.get("protocol") or "")
            for item in protocols
            if item.get("protocol_label") or item.get("protocol")
        ]
        payload = status_snapshot(best_node)
        payload.update(
            {
                "aggregate": True,
                "name": physical_name,
                "physical_node": physical_name,
                "protocol_label": " -> ".join(protocol_labels)
                if len(protocol_labels) > 1
                else (protocol_labels[0] if protocol_labels else payload.get("protocol_label")),
                "fallback_order": list(group_nodes[0].extra.get("fallback_order", []) or []),
                "last_checked_at": latest_node_check(group_nodes),
                "variant_total": len(group_nodes),
                "variant_healthy": count_healthy_nodes(group_nodes),
                "protocols": protocols,
            }
        )
        snapshots.append(payload)
    return snapshots
