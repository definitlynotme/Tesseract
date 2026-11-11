import unittest

from config_utils import (
    canonicalize_routing_rule_layout,
    coerce_bool,
    coerce_float,
    coerce_port,
    coerce_positive_int,
    config_entry_enabled,
    enabled_subscription_urls,
    ensure_list_section,
    ensure_mapping_section,
    normalize_routing_compatibility_defaults,
    routing_rule_mapping,
    subscription_entry_url,
)


class ConfigUtilsTests(unittest.TestCase):
    def test_coerce_bool_accepts_common_text_values(self) -> None:
        self.assertTrue(coerce_bool("yes"))
        self.assertFalse(coerce_bool("off", True))
        self.assertTrue(coerce_bool("unknown", True))

    def test_config_entry_enabled_keeps_legacy_entries_active(self) -> None:
        self.assertTrue(config_entry_enabled("vless://legacy.example:443#node"))
        self.assertTrue(config_entry_enabled({"uri": "vless://legacy.example:443#node"}))
        self.assertTrue(config_entry_enabled(None))
        self.assertFalse(config_entry_enabled({"enabled": False}))
        self.assertFalse(config_entry_enabled({"enabled": "false"}))

    def test_subscription_url_normalization_filters_disabled_mappings(self) -> None:
        self.assertEqual(subscription_entry_url(" https://legacy.example/sub "), "https://legacy.example/sub")
        self.assertEqual(subscription_entry_url({"url": "https://mapped.example/sub", "enabled": False}), "https://mapped.example/sub")
        self.assertEqual(
            enabled_subscription_urls(
                [
                    "https://legacy.example/sub",
                    {"url": "https://draft.example/sub", "enabled": "false"},
                    {"value": "https://active.example/sub", "enabled": "true"},
                    "https://legacy.example/sub",
                ]
            ),
            ["https://legacy.example/sub", "https://active.example/sub"],
        )

    def test_coerce_positive_int_uses_default_for_invalid_values(self) -> None:
        self.assertEqual(coerce_positive_int("5", 3), 5)
        self.assertEqual(coerce_positive_int("bad", 3), 3)
        self.assertEqual(coerce_positive_int(0, 3), 1)

    def test_coerce_float_uses_default_and_optional_minimum(self) -> None:
        self.assertEqual(coerce_float("1.5", 3.0), 1.5)
        self.assertEqual(coerce_float("bad", 3.0), 3.0)
        self.assertEqual(coerce_float(0, 3.0, minimum=0.1), 0.1)

    def test_coerce_port_preserves_valid_range_and_falls_back(self) -> None:
        self.assertEqual(coerce_port("65535", 9090), 65535)
        self.assertEqual(coerce_port("0", 9090), 9090)
        self.assertEqual(coerce_port("70000", 9090), 9090)
        self.assertEqual(coerce_port("bad", 9090), 9090)

    def test_ensure_mapping_section_reuses_or_replaces_invalid_section(self) -> None:
        payload = {"client": {"port": 7890}, "alerts": "bad"}

        self.assertIs(ensure_mapping_section(payload, "client"), payload["client"])
        alerts = ensure_mapping_section(payload, "alerts")

        self.assertEqual(alerts, {})
        self.assertIs(payload["alerts"], alerts)

    def test_ensure_list_section_reuses_or_replaces_invalid_section(self) -> None:
        payload = {"nodes": [{"name": "one"}], "subscription_urls": "bad"}

        self.assertIs(ensure_list_section(payload, "nodes"), payload["nodes"])
        urls = ensure_list_section(payload, "subscription_urls")

        self.assertEqual(urls, [])
        self.assertIs(payload["subscription_urls"], urls)

    def test_nested_routing_rules_win_over_stale_flat_values_in_one_file(self) -> None:
        payload = {
            "routing": {
                "rules": {"direct": []},
                "direct": ["public.example"],
            }
        }

        self.assertEqual(routing_rule_mapping(payload["routing"])["direct"], [])
        self.assertTrue(canonicalize_routing_rule_layout(payload))
        self.assertNotIn("direct", payload["routing"])
        self.assertEqual(payload["routing"]["rules"]["direct"], [])

    def test_compatibility_defaults_are_canonicalized_and_normalized_in_order(self) -> None:
        payload = {
            "routing": {
                "compatibility_defaults": [
                    {"type": "ai-fixed-ip", "value": "openai.com"},
                    {"rule_type": "direct", "rule": "geosite:CN"},
                    {"rule_type": "direct", "rule": "geosite:CN"},
                    {"rule_type": "unknown", "value": "ignored.example"},
                ]
            }
        }

        self.assertTrue(canonicalize_routing_rule_layout(payload))
        self.assertEqual(
            normalize_routing_compatibility_defaults(
                payload["routing"]["rules"]["compatibility_defaults"]
            ),
            [
                {"rule_type": "direct", "value": "geosite:CN"},
            ],
        )

    def test_retired_ai_bucket_is_not_migrated_into_active_rules(self) -> None:
        payload = {
            "routing": {
                "ai_fixed_ip": ["openai.com"],
            }
        }

        self.assertEqual(routing_rule_mapping(payload["routing"]), {})
        self.assertFalse(canonicalize_routing_rule_layout(payload))
        self.assertEqual(payload["routing"]["ai_fixed_ip"], ["openai.com"])


if __name__ == "__main__":
    unittest.main()
