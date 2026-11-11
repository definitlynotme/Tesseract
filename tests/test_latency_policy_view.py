import unittest

from latency_policy_view import (
    apply_latency_policy_config_payload,
    latency_policy_boundaries,
    latency_policy_form_state,
    validate_latency_policy_max_latency_ms,
)


class LatencyPolicyViewTests(unittest.TestCase):
    def test_form_state_uses_local_override_and_exposes_display_boundaries(self) -> None:
        state = latency_policy_form_state(
            {"testing": {"primary_gateway_max_latency_ms": 1000}},
            {"testing": {"primary_gateway_max_latency_ms": 801}},
        )

        self.assertEqual(state.max_latency_ms, 801)
        self.assertEqual(state.boundaries.normal_max_ms, 400.5)
        self.assertEqual(state.boundaries.slow_max_ms, 801)

    def test_form_state_uses_default_for_missing_or_invalid_effective_value(self) -> None:
        self.assertEqual(latency_policy_form_state({}, {}).max_latency_ms, 800)
        self.assertEqual(
            latency_policy_form_state(
                {"testing": {"primary_gateway_max_latency_ms": 1200}},
                {"testing": {"primary_gateway_max_latency_ms": "bad"}},
            ).max_latency_ms,
            800,
        )

    def test_boundaries_match_inclusive_normal_and_slow_contract(self) -> None:
        boundaries = latency_policy_boundaries(800)

        self.assertEqual(boundaries.normal_max_ms, 400)
        self.assertEqual(boundaries.slow_max_ms, 800)

    def test_form_parser_accepts_integer_text_and_rejects_other_values(self) -> None:
        self.assertEqual(validate_latency_policy_max_latency_ms(" 800 "), 800)
        self.assertEqual(validate_latency_policy_max_latency_ms(5000), 5000)
        for value in ("", "800.0", 99, 5001, 800.0, True, None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "integer between 100 and 5000 ms"):
                    validate_latency_policy_max_latency_ms(value)

    def test_apply_updates_only_latency_key_and_preserves_other_testing_fields(self) -> None:
        payload = {
            "testing": {
                "slow_threshold_ms": 2000,
                "request_timeout": 15,
                "custom": {"keep": True},
            },
            "tiers": {"tier3": {"enabled": True}},
        }

        result = apply_latency_policy_config_payload(payload, "1200")

        self.assertIs(result, payload)
        self.assertEqual(
            result["testing"],
            {
                "slow_threshold_ms": 2000,
                "request_timeout": 15,
                "custom": {"keep": True},
                "primary_gateway_max_latency_ms": 1200,
            },
        )
        self.assertEqual(result["tiers"], {"tier3": {"enabled": True}})

    def test_apply_replaces_invalid_testing_section_without_touching_other_sections(self) -> None:
        payload = {"testing": "bad", "unrelated": {"keep": True}}

        apply_latency_policy_config_payload(payload, 800)

        self.assertEqual(
            payload,
            {
                "testing": {"primary_gateway_max_latency_ms": 800},
                "unrelated": {"keep": True},
            },
        )


if __name__ == "__main__":
    unittest.main()
