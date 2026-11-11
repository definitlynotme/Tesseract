import unittest

from models import Node, NodeStatus
from runtime_snapshot_view import (
    disabled_killswitch_state,
    exit_stats_snapshot,
    runtime_snapshot_payload,
    runtime_tiers_snapshot,
    subscription_urls,
)


def node(name: str, status: NodeStatus = NodeStatus.ALIVE) -> Node:
    return Node(
        name=name,
        protocol="ss",
        uri=f"ss://{name}",
        address=f"{name}.example.com",
        port=8388,
        status=status,
    )


class RuntimeSnapshotViewTests(unittest.TestCase):
    def test_disabled_killswitch_state_preserves_manager_default(self) -> None:
        self.assertEqual(
            disabled_killswitch_state(),
            {
                "state": "disabled",
                "manual_override": "none",
                "active": False,
                "admin_available": False,
                "enabled": False,
                "supported": False,
                "whitelist": {},
            },
        )

    def test_subscription_urls_preserve_formats_and_preference(self) -> None:
        urls = subscription_urls("127.0.0.1", 25500, client_mode="clash")

        self.assertEqual(urls["preferred_format"], "clash")
        self.assertEqual(urls["v2ray"]["tier1"], "http://127.0.0.1:25500/sub/tier1")
        self.assertEqual(urls["clash"]["tier3"], "http://127.0.0.1:25500/clash/tier3")
        self.assertEqual(urls["clash"]["config"], "http://127.0.0.1:25500/clash/config")
        self.assertEqual(subscription_urls("host", 1, client_mode="v2ray")["preferred_format"], "v2ray")

    def test_exit_stats_snapshot_preserves_manager_counts(self) -> None:
        stats = exit_stats_snapshot(
            configured=False,
            nodes=[
                node("alive", NodeStatus.ALIVE),
                node("slow", NodeStatus.SLOW),
                node("dead", NodeStatus.DEAD),
                node("suspicious", NodeStatus.SUSPICIOUS),
            ],
        )

        self.assertEqual(
            stats,
            {
                "configured": True,
                "total": 4,
                "alive": 1,
                "slow": 1,
                "dead": 1,
                "suspicious": 1,
            },
        )
        self.assertEqual(exit_stats_snapshot(configured=True, nodes=[])["configured"], True)
        self.assertEqual(exit_stats_snapshot(configured=False, nodes=[])["configured"], False)

    def test_runtime_tiers_snapshot_preserves_include_nodes_contract(self) -> None:
        tiers = runtime_tiers_snapshot(
            metrics={
                "tier1_alive": 1,
                "tier1_total": 1,
                "tier1_variant_alive": 2,
                "tier1_variant_total": 3,
                "tier2_alive": 1,
                "tier2_total": 2,
            },
            tier3_stats={"enabled": True, "total": 1, "alive": 1},
            exit_stats={"configured": True, "total": 1, "alive": 1},
            last_check_at={
                "tier1": "check-1",
                "tier2": "check-2",
                "tier3": "check-3",
                "exit": "check-exit",
            },
            tier2_last_refresh_at="refresh-2",
            tier3_last_collection_at="collect-3",
            tier3_source_count=7,
            exit_last_refresh_at="refresh-exit",
            include_nodes=True,
            tier1_nodes=[node("tier1")],
            tier1_protocol_groups=[{"name": "tier1", "healthy": 1}],
            tier2_nodes=[node("tier2-a"), node("tier2-b", NodeStatus.DEAD)],
            tier3_nodes=[node("tier3")],
            tier3_enabled=True,
            exit_nodes=[node("exit")],
        )

        self.assertEqual(tiers["tier1"]["alive"], 1)
        self.assertEqual(tiers["tier1"]["variant_total"], 3)
        self.assertEqual(tiers["tier1"]["protocol_groups"], [{"name": "tier1", "healthy": 1}])
        self.assertEqual(tiers["tier2"]["last_refresh_at"], "refresh-2")
        self.assertEqual(tiers["tier3"]["last_collection_at"], "collect-3")
        self.assertEqual(tiers["tier3"]["source_count"], 7)
        self.assertEqual(tiers["exit"]["last_refresh_at"], "refresh-exit")
        self.assertEqual(tiers["tier3"]["nodes"][0]["name"], "tier3")

    def test_runtime_tiers_snapshot_hides_disabled_tier3_nodes(self) -> None:
        tiers = runtime_tiers_snapshot(
            metrics={
                "tier1_alive": 0,
                "tier1_total": 0,
                "tier1_variant_alive": 0,
                "tier1_variant_total": 0,
                "tier2_alive": 0,
                "tier2_total": 0,
            },
            tier3_stats={"enabled": False, "total": 0},
            exit_stats={"configured": False, "total": 0},
            last_check_at={"tier1": None, "tier2": None, "tier3": None, "exit": None},
            tier2_last_refresh_at=None,
            tier3_last_collection_at=None,
            tier3_source_count=0,
            exit_last_refresh_at=None,
            include_nodes=True,
            tier1_nodes=[],
            tier1_protocol_groups=[],
            tier2_nodes=[],
            tier3_nodes=[node("hidden")],
            tier3_enabled=False,
            exit_nodes=[],
        )

        self.assertEqual(tiers["tier3"]["nodes"], [])

    def test_runtime_snapshot_payload_preserves_top_level_contract(self) -> None:
        snapshot = runtime_snapshot_payload(
            metrics={
                "mode": "tier2_active",
                "active_tier": "tier2",
                "final_exit": "exit",
                "exit_configured": True,
                "effective_egress_ok": False,
                "effective_egress_reason": "dial failed",
            },
            uptime_seconds=12,
            started_at="started",
            last_overall_check_at="overall",
            last_check_at={"tier1": "check"},
            client_mode="clash",
            clash_config_path="config.yaml",
            kill_switch_state={"state": "disabled"},
            tiers={"tier1": {"total": 0}},
            background_actions={"tier3_collect": {"running": False}},
            subscriptions={"preferred_format": "clash"},
        )

        self.assertEqual(snapshot["system"]["mode"], "tier2_active")
        self.assertEqual(snapshot["system"]["final_exit"], "exit")
        self.assertEqual(snapshot["system"]["uptime_seconds"], 12)
        self.assertEqual(snapshot["client"], {"mode": "clash", "clash_config_path": "config.yaml"})
        self.assertEqual(snapshot["killswitch"], {"state": "disabled"})
        self.assertEqual(snapshot["tiers"], {"tier1": {"total": 0}})
        self.assertEqual(snapshot["actions"], {"tier3_collect": {"running": False}})
        self.assertEqual(snapshot["subscriptions"], {"preferred_format": "clash"})


if __name__ == "__main__":
    unittest.main()
