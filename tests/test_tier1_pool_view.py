import unittest

from models import Node, NodeStatus
from tier1_pool_view import (
    best_tier1_status_node,
    latest_node_check,
    tier1_group_stats,
    tier1_protocol_groups_snapshot,
    tier1_status_nodes_snapshot,
)


def node(
    name: str,
    *,
    physical: str,
    protocol: str,
    protocol_label: str,
    status: NodeStatus,
    latency_ms: float | None = None,
    last_checked_at: str | None = None,
    fallback_order: list[str] | None = None,
) -> Node:
    return Node(
        name=name,
        protocol=protocol,
        uri=f"{protocol}://{name}",
        address=f"{name}.example.com",
        port=443,
        tier="tier1",
        status=status,
        latency_ms=latency_ms,
        last_checked_at=last_checked_at,
        extra={
            "physical_node": physical,
            "protocol_key": protocol,
            "protocol_label": protocol_label,
            "fallback_order": list(fallback_order or []),
        },
    )


def status_snapshot(item: Node) -> dict:
    return {
        "name": item.name,
        "protocol": item.protocol,
        "protocol_label": item.extra.get("protocol_label"),
        "status": item.status.value,
        "last_checked_at": item.last_checked_at,
    }


class Tier1PoolViewTests(unittest.TestCase):
    def test_group_stats_count_physical_nodes_and_protocol_variants(self) -> None:
        nodes = [
            node(
                "alpha-vless",
                physical="alpha",
                protocol="vless",
                protocol_label="VLESS",
                status=NodeStatus.DEAD,
            ),
            node(
                "alpha-hy2",
                physical="alpha",
                protocol="hysteria2",
                protocol_label="Hysteria2",
                status=NodeStatus.SLOW,
            ),
            node(
                "beta-vless",
                physical="beta",
                protocol="vless",
                protocol_label="VLESS",
                status=NodeStatus.DEAD,
            ),
        ]

        self.assertEqual(
            tier1_group_stats(nodes),
            {"total": 2, "alive": 1, "variant_total": 3, "variant_alive": 1},
        )

    def test_protocol_group_snapshot_preserves_existing_contract(self) -> None:
        nodes = [
            node(
                "alpha-hy2",
                physical="alpha",
                protocol="hysteria2",
                protocol_label="Hysteria2",
                status=NodeStatus.SLOW,
                fallback_order=["vless", "hysteria2"],
            ),
            node(
                "alpha-vless",
                physical="alpha",
                protocol="vless",
                protocol_label="VLESS",
                status=NodeStatus.DEAD,
                fallback_order=["vless", "hysteria2"],
            ),
        ]

        self.assertEqual(
            tier1_protocol_groups_snapshot(nodes, status_snapshot),
            [
                {
                    "name": "alpha",
                    "total": 2,
                    "healthy": 1,
                    "fallback_order": ["vless", "hysteria2"],
                    "protocols": [
                        {
                            "name": "alpha-vless",
                            "protocol": "vless",
                            "protocol_label": "VLESS",
                            "status": "dead",
                            "last_checked_at": None,
                        },
                        {
                            "name": "alpha-hy2",
                            "protocol": "hysteria2",
                            "protocol_label": "Hysteria2",
                            "status": "slow",
                            "last_checked_at": None,
                        },
                    ],
                }
            ],
        )

    def test_status_snapshot_uses_best_variant_but_keeps_group_protocol_details(self) -> None:
        nodes = [
            node(
                "alpha-vless",
                physical="alpha",
                protocol="vless",
                protocol_label="VLESS",
                status=NodeStatus.DEAD,
                latency_ms=1,
                last_checked_at="2026-05-30T10:00:00+00:00",
                fallback_order=["vless", "hysteria2"],
            ),
            node(
                "alpha-hy2",
                physical="alpha",
                protocol="hysteria2",
                protocol_label="Hysteria2",
                status=NodeStatus.SLOW,
                latency_ms=300,
                last_checked_at="2026-05-30T10:01:00+00:00",
                fallback_order=["vless", "hysteria2"],
            ),
        ]

        self.assertIs(best_tier1_status_node(nodes), nodes[1])
        self.assertEqual(latest_node_check(nodes), "2026-05-30T10:01:00+00:00")
        self.assertEqual(
            tier1_status_nodes_snapshot(nodes, status_snapshot),
            [
                {
                    "name": "alpha",
                    "protocol": "hysteria2",
                    "protocol_label": "VLESS -> Hysteria2",
                    "status": "slow",
                    "last_checked_at": "2026-05-30T10:01:00+00:00",
                    "aggregate": True,
                    "physical_node": "alpha",
                    "fallback_order": ["vless", "hysteria2"],
                    "variant_total": 2,
                    "variant_healthy": 1,
                    "protocols": [
                        {
                            "name": "alpha-vless",
                            "protocol": "vless",
                            "protocol_label": "VLESS",
                            "status": "dead",
                            "last_checked_at": "2026-05-30T10:00:00+00:00",
                        },
                        {
                            "name": "alpha-hy2",
                            "protocol": "hysteria2",
                            "protocol_label": "Hysteria2",
                            "status": "slow",
                            "last_checked_at": "2026-05-30T10:01:00+00:00",
                        },
                    ],
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
