import unittest

from models import Node, NodeStatus
from tier3_pool_order import select_tier3_startup_export_nodes, sort_tier3_nodes, tier3_node_score


def node(
    name: str,
    *,
    status: NodeStatus = NodeStatus.ALIVE,
    latency: float = 100,
    source: str = "",
    protocol: str = "vless",
    extra: dict | None = None,
) -> Node:
    return Node(
        name=name,
        protocol=protocol,
        uri=f"{protocol}://{name}",
        address=f"{name}.example.com",
        port=443,
        source=source,
        tier="tier3",
        status=status,
        latency_ms=latency,
        extra=dict(extra or {}),
    )


class Tier3PoolOrderTests(unittest.TestCase):
    def test_node_score_preserves_startup_export_rank_priority(self) -> None:
        ranked = node(
            "ranked",
            latency=9000,
            extra={"startup_prelaunch_validation": "tcp", "startup_export_rank": 1},
        )
        fast = node("fast", latency=1)

        self.assertGreater(tier3_node_score(ranked), tier3_node_score(fast))
        self.assertEqual([item.name for item in sort_tier3_nodes([fast, ranked])], ["ranked", "fast"])

    def test_tier3a_usable_nodes_are_prioritized_for_fallback(self) -> None:
        tier3a = node("tier3a", latency=2000, extra={"tier3_origin": "tier3a"})
        tier3b = node("tier3b", latency=1, extra={"tier3_origin": "tier3b"})
        dead_tier3a = node("dead-tier3a", status=NodeStatus.DEAD, latency=1, extra={"tier3_origin": "tier3a"})

        self.assertEqual([item.name for item in sort_tier3_nodes([tier3b, tier3a])], ["tier3a", "tier3b"])
        self.assertEqual([item.name for item in sort_tier3_nodes([tier3b, dead_tier3a])], ["tier3b", "dead-tier3a"])

    def test_startup_export_selection_round_robins_source_protocol_buckets(self) -> None:
        selected = select_tier3_startup_export_nodes(
            [
                node("fast-a", source="source-a", protocol="vless", latency=10),
                node("second-a", source="source-a", protocol="vless", latency=20),
                node("slow-b", source="source-b", protocol="trojan", latency=800),
            ],
            3,
        )

        self.assertEqual([item.name for item in selected], ["fast-a", "slow-b", "second-a"])
        self.assertEqual([item.extra["startup_export_rank"] for item in selected], [0, 1, 2])

    def test_startup_export_selection_skips_duplicate_runtime_identity(self) -> None:
        duplicate = node("same-a", source="source-a")
        selected = select_tier3_startup_export_nodes(
            [
                duplicate,
                duplicate.clone(),
                node("other", source="source-b"),
            ],
            3,
        )

        self.assertEqual([item.name for item in selected], ["same-a", "other"])
        self.assertEqual([item.extra["startup_export_rank"] for item in selected], [0, 1])


if __name__ == "__main__":
    unittest.main()
