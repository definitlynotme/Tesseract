from __future__ import annotations

import unittest

from manager_subscription_nodes import subscription_nodes_for_tier, tier3_subscription_nodes
from models import Node, NodeStatus


def node(name: str, tier: str, status: NodeStatus = NodeStatus.ALIVE) -> Node:
    return Node(name=name, address=f"{name}.example.com", port=443, tier=tier, status=status)


class ManagerSubscriptionNodesTests(unittest.TestCase):
    def test_tier3_subscription_nodes_preserve_public_inventory_statuses(self) -> None:
        nodes = [
            node("alive", "tier3", NodeStatus.ALIVE),
            node("slow", "tier3", NodeStatus.SLOW),
            node("dead", "tier3", NodeStatus.DEAD),
            node("unknown", "tier3", NodeStatus.UNKNOWN),
        ]

        self.assertEqual(
            [item.name for item in tier3_subscription_nodes(nodes, tier3_enabled=True)],
            ["alive", "slow", "dead", "unknown"],
        )
        self.assertEqual(tier3_subscription_nodes(nodes, tier3_enabled=False), [])

    def test_subscription_nodes_for_tier_preserves_layer_contract(self) -> None:
        runtime_nodes = {
            "tier1": [node("tier1", "tier1", NodeStatus.DEAD)],
            "tier2": [node("tier2", "tier2", NodeStatus.UNKNOWN)],
            "tier3": [
                node("tier3-alive", "tier3", NodeStatus.ALIVE),
                node("tier3-dead", "tier3", NodeStatus.DEAD),
            ],
            "exit": [node("exit", "exit", NodeStatus.DEAD)],
        }

        self.assertEqual(
            [item.name for item in subscription_nodes_for_tier(runtime_nodes, "tier1", tier3_enabled=True)],
            ["tier1"],
        )
        self.assertEqual(
            [item.name for item in subscription_nodes_for_tier(runtime_nodes, "tier2", tier3_enabled=True)],
            ["tier2"],
        )
        self.assertEqual(
            [item.name for item in subscription_nodes_for_tier(runtime_nodes, "tier3", tier3_enabled=True)],
            ["tier3-alive", "tier3-dead"],
        )
        self.assertEqual(
            [item.name for item in subscription_nodes_for_tier(runtime_nodes, "exit", tier3_enabled=True)],
            ["exit"],
        )
        self.assertEqual(
            [item.name for item in subscription_nodes_for_tier(runtime_nodes, "all", tier3_enabled=True)],
            ["tier1", "tier2", "tier3-alive", "tier3-dead", "exit"],
        )

    def test_subscription_nodes_for_tier_hides_disabled_tier3_from_all_scope(self) -> None:
        runtime_nodes = {
            "tier1": [],
            "tier2": [],
            "tier3": [node("tier3-alive", "tier3", NodeStatus.ALIVE)],
            "exit": [],
        }

        self.assertEqual(subscription_nodes_for_tier(runtime_nodes, "tier3", tier3_enabled=False), [])
        self.assertEqual(subscription_nodes_for_tier(runtime_nodes, "all", tier3_enabled=False), [])
        self.assertEqual(subscription_nodes_for_tier(runtime_nodes, "unknown", tier3_enabled=True), [])


if __name__ == "__main__":
    unittest.main()
