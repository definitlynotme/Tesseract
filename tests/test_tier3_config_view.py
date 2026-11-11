import unittest

from config_validation import validate_local_config
from tier3_config_view import (
    apply_tier3_config_payload,
    effective_tier3_config,
    tier3_config_form_state,
    tier3_default_min_alive,
)


class Tier3ConfigViewTests(unittest.TestCase):
    def test_effective_tier3_config_merges_runtime_and_local_overrides(self) -> None:
        self.assertEqual(
            effective_tier3_config(
                {"tiers": {"tier3": {"enabled": False, "min_alive_nodes": 3}}},
                {"tiers": {"tier3": {"enabled": True}}},
            ),
            {"enabled": True, "min_alive_nodes": 3},
        )
        self.assertEqual(effective_tier3_config({"tiers": []}, {"tiers": []}), {})

    def test_tier3_default_min_alive_uses_runtime_default_and_floor(self) -> None:
        self.assertEqual(tier3_default_min_alive({"tiers": {"tier3": {"min_alive_nodes": 5}}}), 5)
        self.assertEqual(tier3_default_min_alive({"tiers": {"tier3": {"min_alive_nodes": 0}}}), 1)
        self.assertEqual(tier3_default_min_alive({}), 3)

    def test_tier3_config_form_state_normalizes_display_values(self) -> None:
        state = tier3_config_form_state(
            {"tiers": {"tier3": {"enabled": False, "min_alive_nodes": 3}}},
            {
                "tiers": {
                    "tier3": {
                        "enabled": True,
                        "min_alive_nodes": "4",
                    }
                }
            },
        )

        self.assertTrue(state.enabled)
        self.assertEqual(state.min_alive_nodes, 4)
        self.assertEqual(state.max_pool_size, 100)

    def test_tier3_config_form_state_clamps_minimum_to_max_pool(self) -> None:
        state = tier3_config_form_state(
            {"tiers": {"tier3": {"min_alive_nodes": 3, "max_pool_size": 6}}},
            {"tiers": {"tier3": {"min_alive_nodes": 20}}},
        )

        self.assertEqual(state.min_alive_nodes, 6)
        self.assertEqual(state.max_pool_size, 6)

    def test_apply_tier3_config_payload_clamps_minimum_and_preserves_pi_controller(self) -> None:
        controller = {"target_alive": 41, "kp": 0.8, "ki": 0.1}
        payload = apply_tier3_config_payload(
            {"tiers": {"tier3": {"max_pool_size": 20, "pi_controller": controller}}},
            enabled=True,
            min_alive_text="30",
            default_min_alive=3,
            max_pool_size=20,
        )

        tier3 = payload["tiers"]["tier3"]
        self.assertEqual(tier3["min_alive_nodes"], 20)
        self.assertIs(tier3["pi_controller"], controller)
        self.assertEqual(tier3["pi_controller"], {"target_alive": 41, "kp": 0.8, "ki": 0.1})

    def test_apply_tier3_config_payload_does_not_create_pi_controller(self) -> None:
        payload = apply_tier3_config_payload(
            {"tiers": {"tier3": {}}},
            enabled=True,
            min_alive_text="3",
            default_min_alive=3,
        )

        self.assertNotIn("pi_controller", payload["tiers"]["tier3"])

    def test_apply_tier3_config_payload_preserves_non_mapping_pi_controller(self) -> None:
        payload = apply_tier3_config_payload(
            {"tiers": {"tier3": {"pi_controller": "advanced-value"}}},
            enabled=True,
            min_alive_text="3",
            default_min_alive=3,
        )

        self.assertEqual(payload["tiers"]["tier3"]["pi_controller"], "advanced-value")

    def test_apply_tier3_config_payload_replaces_invalid_sections_and_coerces_min_alive(self) -> None:
        payload = apply_tier3_config_payload(
            {"tiers": []},
            enabled=True,
            min_alive_text="bad",
            default_min_alive=3,
        )

        self.assertEqual(
            payload,
            {
                "tiers": {
                    "tier3": {
                        "enabled": True,
                        "min_alive_nodes": 3,
                    }
                }
            },
        )

    def test_apply_tier3_config_payload_removes_legacy_bootstrap_fields(self) -> None:
        payload = apply_tier3_config_payload(
            {
                "tiers": {
                    "tier3": {
                        "bootstrap_sources": [{"url": "https://example.com/sub"}],
                        "bootstrap_defaults_initialized": True,
                    }
                }
            },
            enabled=True,
            min_alive_text="3",
            default_min_alive=3,
        )

        tier3 = payload["tiers"]["tier3"]
        self.assertNotIn("bootstrap_sources", tier3)
        self.assertNotIn("bootstrap_defaults_initialized", tier3)


class Tier3RetiredSwitchTests(unittest.TestCase):
    """The fast-expansion and fallback-source switches are gone from the card."""

    def _apply(self, tier3: dict):
        return apply_tier3_config_payload(
            {"tiers": {"tier3": tier3}},
            enabled=True,
            min_alive_text="3",
            default_min_alive=3,
        )["tiers"]["tier3"]

    def test_form_state_has_only_the_card_fields(self) -> None:
        state = tier3_config_form_state(
            {"tiers": {"tier3": {"validation_slots_per_process": 8}}},
            {"tiers": {"tier3": {"bootstrap_enabled": False}}},
        )

        self.assertFalse(hasattr(state, "bootstrap_enabled"))
        self.assertFalse(hasattr(state, "fast_expansion"))

    def test_save_drops_the_retired_bootstrap_switch(self) -> None:
        for value in (True, False, "false"):
            with self.subTest(value=value):
                self.assertNotIn("bootstrap_enabled", self._apply({"bootstrap_enabled": value}))

    def test_save_never_writes_validation_capacity_keys(self) -> None:
        tier3 = self._apply({})

        for key in (
            "validation_slots_per_process",
            "validation_worker_max",
            "full_validation_process_max",
            "validation_concurrency_max",
            "tcp_breadth_per_identity",
            "tcp_dns_capacity",
        ):
            self.assertNotIn(key, tier3)

    def test_save_keeps_hand_written_capacity_values(self) -> None:
        custom = {
            "validation_slots_per_process": 1,
            "validation_worker_max": 24,
            "full_validation_process_max": 24,
            "validation_concurrency_max": 16,
            "tcp_breadth_per_identity": 1.0,
        }
        tier3 = self._apply(dict(custom))

        for key, value in custom.items():
            self.assertEqual(tier3[key], value, key)
        self.assertNotIn("tcp_dns_capacity", tier3)
        validate_local_config({"tiers": {"tier3": tier3}})


if __name__ == "__main__":
    unittest.main()
