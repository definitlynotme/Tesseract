import unittest

from tier3_collection_plan import (
    should_collect_tier3_bootstrap,
    should_refresh_tier3_during_check,
    should_urgent_recheck_tier3,
    tier3_bootstrap_threshold,
    tier3_collect_bounds,
    tier3_collect_interval_for_pool,
    tier3_collection_entry_groups,
)


class Tier3CollectionPlanTests(unittest.TestCase):
    def test_collection_entry_groups_keeps_tier3a_first_and_limits_tier3b(self) -> None:
        tier3a = [{"type": "tier3a", "value": "a"}]
        tier3b = [
            {"type": "tier3b", "value": "b1"},
            {"type": "tier3b", "value": "b2"},
            {"type": "tier3b", "value": "b3"},
        ]

        self.assertEqual(
            tier3_collection_entry_groups(tier3a, tier3b, source_budget=2),
            [tier3a, tier3b[:2]],
        )

    def test_collection_entry_groups_skips_empty_groups(self) -> None:
        self.assertEqual(
            tier3_collection_entry_groups([], [{"value": "b"}], source_budget=1),
            [[{"value": "b"}]],
        )
        self.assertEqual(tier3_collection_entry_groups([], [], source_budget=1), [])

    def test_collect_bounds_use_defaults_and_clamp_invalid_intervals(self) -> None:
        self.assertEqual(
            tier3_collect_bounds(
                {
                    "min_collect_interval": "bad",
                    "max_collect_interval": "bad",
                    "auto_collect_interval": "bad",
                },
                default_min_interval=120,
                default_max_interval=3600,
            ),
            (120, 3600),
        )
        self.assertEqual(
            tier3_collect_bounds(
                {
                    "min_collect_interval": 300,
                    "max_collect_interval": 60,
                },
                default_min_interval=120,
                default_max_interval=3600,
            ),
            (300, 300),
        )
        self.assertEqual(
            tier3_collect_bounds(
                {
                    "min_collect_interval": 30,
                    "auto_collect_interval": 900,
                },
                default_min_interval=120,
                default_max_interval=3600,
            ),
            (30, 900),
        )

    def test_bootstrap_threshold_tracks_background_target_after_floor_is_met(self) -> None:
        self.assertEqual(
            tier3_bootstrap_threshold(
                target_alive=10,
                min_alive_nodes=3,
                intensive_expansion=True,
            ),
            10,
        )
        self.assertEqual(
            tier3_bootstrap_threshold(
                target_alive=10,
                min_alive_nodes=3,
                intensive_expansion=False,
            ),
            10,
        )
        self.assertEqual(
            tier3_bootstrap_threshold(
                target_alive=3,
                min_alive_nodes=3,
                intensive_expansion=False,
            ),
            3,
        )

    def test_bootstrap_collection_policy_matches_refresh_conditions(self) -> None:
        self.assertFalse(
            should_collect_tier3_bootstrap(
                bootstrap_enabled=False,
                previous_alive_count=0,
                bootstrap_threshold=3,
                collected_node_count=0,
                startup_mode=True,
            )
        )
        self.assertFalse(
            should_collect_tier3_bootstrap(
                bootstrap_enabled=True,
                previous_alive_count=3,
                bootstrap_threshold=3,
                collected_node_count=0,
                startup_mode=True,
            )
        )
        self.assertTrue(
            should_collect_tier3_bootstrap(
                bootstrap_enabled=True,
                previous_alive_count=3,
                bootstrap_threshold=25,
                collected_node_count=0,
                startup_mode=False,
            )
        )
        self.assertTrue(
            should_collect_tier3_bootstrap(
                bootstrap_enabled=True,
                previous_alive_count=0,
                bootstrap_threshold=3,
                collected_node_count=0,
                startup_mode=False,
            )
        )
        self.assertTrue(
            should_collect_tier3_bootstrap(
                bootstrap_enabled=True,
                previous_alive_count=0,
                bootstrap_threshold=3,
                collected_node_count=2,
                startup_mode=False,
            )
        )
        self.assertTrue(
            should_collect_tier3_bootstrap(
                bootstrap_enabled=True,
                previous_alive_count=0,
                bootstrap_threshold=3,
                collected_node_count=2,
                startup_mode=True,
            )
        )

    def test_check_refresh_policy_respects_existing_pool_and_cooldown(self) -> None:
        self.assertFalse(
            should_refresh_tier3_during_check(
                force_collect=False,
                has_runtime_nodes=True,
                elapsed_seconds=3600,
                min_collect_interval=60,
            )
        )
        self.assertFalse(
            should_refresh_tier3_during_check(
                force_collect=False,
                has_runtime_nodes=False,
                elapsed_seconds=59,
                min_collect_interval=60,
            )
        )
        self.assertTrue(
            should_refresh_tier3_during_check(
                force_collect=False,
                has_runtime_nodes=False,
                elapsed_seconds=60,
                min_collect_interval=60,
            )
        )
        self.assertTrue(
            should_refresh_tier3_during_check(
                force_collect=True,
                has_runtime_nodes=True,
                elapsed_seconds=0,
                min_collect_interval=60,
            )
        )

    def test_tier3_urgent_recheck_requires_primary_outage_nonempty_dead_pool_and_cooldown(self) -> None:
        base = {
            "tier1_alive_count": 0,
            "tier2_alive_count": 0,
            "tier3_enabled": True,
            "tier3_node_count": 1,
            "tier3_alive_count": 0,
            "urgent_due": True,
        }
        self.assertTrue(should_urgent_recheck_tier3(**base))
        for key, value in [
            ("tier1_alive_count", 1),
            ("tier2_alive_count", 1),
            ("tier3_enabled", False),
            ("tier3_node_count", 0),
            ("tier3_alive_count", 1),
            ("urgent_due", False),
        ]:
            with self.subTest(key=key):
                case = dict(base)
                case[key] = value
                self.assertFalse(should_urgent_recheck_tier3(**case))

    def test_collect_interval_for_pool_uses_underfilled_interval_only_below_target(self) -> None:
        self.assertEqual(
            tier3_collect_interval_for_pool(
                collect_interval=300,
                alive_count=2,
                min_alive_nodes=3,
                underfilled_collect_interval=30,
            ),
            30,
        )
        self.assertEqual(
            tier3_collect_interval_for_pool(
                collect_interval=20,
                alive_count=2,
                min_alive_nodes=3,
                underfilled_collect_interval=30,
            ),
            20,
        )
        self.assertEqual(
            tier3_collect_interval_for_pool(
                collect_interval=300,
                alive_count=3,
                min_alive_nodes=3,
                underfilled_collect_interval=30,
            ),
            300,
        )


if __name__ == "__main__":
    unittest.main()
