from __future__ import annotations

import copy
import unittest

from manager import ProxyTierManager
from tier3_refresh_controller import Tier3PIController
from tier3_threshold_policy import resolve_tier3_thresholds


class Tier3ThresholdPolicyTests(unittest.TestCase):
    def test_defaults_preserve_three_to_twenty_five_baseline(self) -> None:
        thresholds = resolve_tier3_thresholds({})

        self.assertEqual(thresholds.max_pool_size, 100)
        self.assertEqual(thresholds.min_alive_nodes, 3)
        self.assertEqual(thresholds.configured_target_alive, 25)
        self.assertEqual(thresholds.automatic_target_alive, 25)
        self.assertEqual(thresholds.effective_target_alive, 25)

    def test_hidden_target_mapping_uses_hybrid_reserve(self) -> None:
        expected_targets = {
            1: 25,
            3: 25,
            5: 27,
            10: 32,
            20: 42,
            40: 80,
            60: 100,
        }

        for minimum, expected in expected_targets.items():
            with self.subTest(minimum=minimum):
                thresholds = resolve_tier3_thresholds(
                    {"max_pool_size": 100, "min_alive_nodes": minimum}
                )
                self.assertEqual(thresholds.effective_target_alive, expected)

    def test_advanced_target_can_raise_but_not_lower_automatic_target(self) -> None:
        raised = resolve_tier3_thresholds(
            {
                "max_pool_size": 100,
                "min_alive_nodes": 5,
                "pi_controller": {"target_alive": 40},
            }
        )
        lowered = resolve_tier3_thresholds(
            {
                "max_pool_size": 100,
                "min_alive_nodes": 20,
                "pi_controller": {"target_alive": 5},
            }
        )

        self.assertEqual(raised.effective_target_alive, 40)
        self.assertEqual(lowered.configured_target_alive, 5)
        self.assertEqual(lowered.automatic_target_alive, 42)
        self.assertEqual(lowered.effective_target_alive, 42)

    def test_invalid_values_and_capacity_saturation_are_normalized(self) -> None:
        invalid = resolve_tier3_thresholds(
            {
                "max_pool_size": "bad",
                "min_alive_nodes": "bad",
                "pi_controller": {"target_alive": "bad"},
            }
        )
        saturated = resolve_tier3_thresholds(
            {
                "max_pool_size": 10,
                "min_alive_nodes": 60,
                "pi_controller": {"target_alive": 250},
            }
        )
        non_positive_target = resolve_tier3_thresholds(
            {"pi_controller": {"target_alive": 0}}
        )

        self.assertEqual(invalid.max_pool_size, 100)
        self.assertEqual(invalid.min_alive_nodes, 3)
        self.assertEqual(invalid.configured_target_alive, 25)
        self.assertEqual(invalid.effective_target_alive, 25)
        self.assertEqual(saturated.max_pool_size, 10)
        self.assertEqual(saturated.min_alive_nodes, 10)
        self.assertEqual(saturated.configured_target_alive, 250)
        self.assertEqual(saturated.effective_target_alive, 10)
        self.assertEqual(non_positive_target.configured_target_alive, 25)

    def test_resolver_and_manager_controller_copy_do_not_mutate_raw_config(self) -> None:
        tier3 = {
            "max_pool_size": 100,
            "min_alive_nodes": 20,
            "pi_controller": {
                "target_alive": 5,
                "kp": 0.7,
                "ki": 0.2,
            },
        }
        original = copy.deepcopy(tier3)
        manager = ProxyTierManager.__new__(ProxyTierManager)
        manager.config = {"tiers": {"tier3": tier3}}

        thresholds = resolve_tier3_thresholds(tier3)
        controller_config = manager._tier3_controller_config()

        self.assertEqual(thresholds.effective_target_alive, 42)
        self.assertEqual(manager._tier3_min_alive_nodes(), 20)
        self.assertEqual(manager._tier3_background_target_alive_nodes(), 42)
        self.assertEqual(controller_config["target_alive"], 42)
        self.assertEqual(controller_config["kp"], 0.7)
        self.assertEqual(controller_config["ki"], 0.2)
        self.assertEqual(tier3, original)
        self.assertIsNot(controller_config, tier3["pi_controller"])

    def test_pi_uses_max_pressure_at_floor_and_stops_at_effective_target(self) -> None:
        thresholds = resolve_tier3_thresholds({})

        for healthy_count in (0, thresholds.min_alive_nodes):
            with self.subTest(healthy_count=healthy_count, state="critical"):
                decision = Tier3PIController(
                    {"target_alive": thresholds.effective_target_alive}
                ).evaluate(
                    alive_count=healthy_count,
                    min_alive_nodes=thresholds.min_alive_nodes,
                    target_alive=thresholds.effective_target_alive,
                    min_interval=120,
                    max_interval=3600,
                    now_monotonic=1.0,
                )
                self.assertEqual(decision.intensity, 1.0)

        for healthy_count in (
            thresholds.effective_target_alive,
            thresholds.effective_target_alive + 5,
        ):
            with self.subTest(healthy_count=healthy_count, state="maintenance"):
                decision = Tier3PIController(
                    {"target_alive": thresholds.effective_target_alive}
                ).evaluate(
                    alive_count=healthy_count,
                    min_alive_nodes=thresholds.min_alive_nodes,
                    target_alive=thresholds.effective_target_alive,
                    min_interval=120,
                    max_interval=3600,
                    now_monotonic=1.0,
                )
                self.assertEqual(decision.intensity, 0.0)


if __name__ == "__main__":
    unittest.main()
