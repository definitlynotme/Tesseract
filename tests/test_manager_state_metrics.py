import unittest

from manager_state_metrics import state_metrics, state_mode, state_summary
from models import Node, NodeStatus


def node(name: str, status: NodeStatus) -> Node:
    return Node(name=name, address=f"{name}.example.com", port=443, status=status)


class ManagerStateMetricsTests(unittest.TestCase):
    def test_state_mode_preserves_tier_priority(self) -> None:
        self.assertEqual(
            state_mode(tier1_total=2, tier1_alive=2, tier2_alive=1, tier3_alive=1),
            ("tier1_healthy", "tier1"),
        )
        self.assertEqual(
            state_mode(tier1_total=2, tier1_alive=1, tier2_alive=1, tier3_alive=1),
            ("tier1_degraded", "tier1"),
        )
        self.assertEqual(
            state_mode(tier1_total=2, tier1_alive=0, tier2_alive=1, tier3_alive=1),
            ("tier2_active", "tier2"),
        )
        self.assertEqual(
            state_mode(tier1_total=0, tier1_alive=0, tier2_alive=0, tier3_alive=1),
            ("tier3_active", "tier3"),
        )
        self.assertEqual(
            state_mode(tier1_total=0, tier1_alive=0, tier2_alive=0, tier3_alive=0),
            ("offline", "offline"),
        )

    def test_state_metrics_preserve_snapshot_contract(self) -> None:
        metrics = state_metrics(
            tier1_group_stats={
                "total": 2,
                "alive": 1,
                "variant_total": 3,
                "variant_alive": 2,
            },
            tier2_nodes=[node("tier2-alive", NodeStatus.ALIVE), node("tier2-dead", NodeStatus.DEAD)],
            tier3_display_nodes=[node("tier3-slow", NodeStatus.SLOW)],
            tier3_enabled=True,
            exit_nodes=[node("exit", NodeStatus.ALIVE)],
            exit_configured=False,
            exit_active=True,
            effective_egress_ok=False,
            effective_egress_reason="dial failed",
        )

        self.assertEqual(
            metrics,
            {
                "mode": "tier1_degraded",
                "active_tier": "tier1",
                "tier1_alive": 1,
                "tier1_total": 2,
                "tier1_variant_alive": 2,
                "tier1_variant_total": 3,
                "tier2_alive": 1,
                "tier2_total": 2,
                "tier3_alive": 1,
                "tier3_total": 1,
                "exit_alive": 1,
                "exit_total": 1,
                "exit_configured": True,
                "final_exit": "exit",
                "effective_egress_ok": False,
                "effective_egress_reason": "dial failed",
            },
        )

    def test_state_metrics_hide_disabled_tier3_display_nodes(self) -> None:
        metrics = state_metrics(
            tier1_group_stats={
                "total": 0,
                "alive": 0,
                "variant_total": 0,
                "variant_alive": 0,
            },
            tier2_nodes=[],
            tier3_display_nodes=[node("tier3-alive", NodeStatus.ALIVE)],
            tier3_enabled=False,
            exit_nodes=[],
            exit_configured=False,
            exit_active=False,
            effective_egress_ok=None,
            effective_egress_reason="",
        )

        self.assertEqual(metrics["mode"], "offline")
        self.assertEqual(metrics["active_tier"], "offline")
        self.assertEqual(metrics["tier3_alive"], 0)
        self.assertEqual(metrics["tier3_total"], 0)
        self.assertEqual(metrics["final_exit"], "offline")

    def test_state_metrics_do_not_promote_inactive_exit_candidates(self) -> None:
        metrics = state_metrics(
            tier1_group_stats={
                "total": 1,
                "alive": 1,
                "variant_total": 1,
                "variant_alive": 1,
            },
            tier2_nodes=[],
            tier3_display_nodes=[],
            tier3_enabled=True,
            exit_nodes=[node("exit", NodeStatus.ALIVE)],
            exit_configured=True,
            exit_active=False,
            effective_egress_ok=None,
            effective_egress_reason="",
        )

        self.assertTrue(metrics["exit_configured"])
        self.assertEqual(metrics["final_exit"], "tier1")

    def test_state_summary_preserves_evaluate_state_contract(self) -> None:
        summary = state_summary(
            {
                "active_tier": "tier2",
                "mode": "tier2_active",
                "tier1_alive": 0,
                "tier2_alive": 2,
                "tier3_alive": 1,
                "exit_alive": 1,
                "final_exit": "exit",
                "tier1_total": 3,
                "effective_egress_ok": True,
            }
        )

        self.assertEqual(
            summary,
            {
                "active_tier": "tier2",
                "mode": "tier2_active",
                "tier1_alive": 0,
                "tier2_alive": 2,
                "tier3_alive": 1,
                "exit_alive": 1,
                "final_exit": "exit",
            },
        )


if __name__ == "__main__":
    unittest.main()
