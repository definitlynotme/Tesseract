import tempfile
import unittest
from pathlib import Path

import yaml

from alert_config import RETIRED_ALERT_CHANNEL_TYPES, SUPPORTED_ALERT_CHANNEL_TYPES
from clash_formatter import build_custom_routing_rules
from config_validation import validate_local_config
from main import load_config


class ConfigLoadingTests(unittest.TestCase):
    def test_example_configs_do_not_advertise_custom_exit_check_providers(self) -> None:
        project_root = Path(__file__).resolve().parents[1]

        for filename in ("config.example.yaml", "config.local.example.yaml"):
            path = project_root / filename
            text = path.read_text(encoding="utf-8")
            parsed = yaml.safe_load(text)

            self.assertIsInstance(parsed, dict)
            self.assertNotIn("exit_check", parsed, filename)
            self.assertNotIn("ip_providers:", text, filename)
            self.assertNotIn("profile_providers:", text, filename)
            self.assertNotIn("dns_probes:", text, filename)
            self.assertNotIn("Custom providers", text, filename)
            routing = parsed.get("routing", {})
            self.assertIsInstance(routing, dict)
            routing_rules = routing.get("rules", {})
            self.assertIsInstance(routing_rules, dict)
            # The regional DIRECT default ships in the user-editable list the
            # Settings UI renders, never behind it. config.local.example.yaml
            # stays empty so the local layer does not duplicate the base.
            expected_direct = (
                ["geosite:CN", "geoip:CN"]
                if filename == "config.example.yaml"
                else []
            )
            self.assertEqual(
                routing_rules.get("direct"),
                expected_direct,
                filename,
            )
            self.assertNotIn("ai_fixed_ip", routing_rules, filename)
            # The hidden compatibility stage is retired: no shipped config
            # carries country- or source-host DIRECT defaults any more.
            self.assertNotIn("compatibility_defaults", routing_rules, filename)
            self.assertEqual(routing_rules.get("raw"), [], filename)
            self.assertEqual(routing_rules.get("rule_sets"), [], filename)
            self.assertIn(
                "routing.rules.direct is the ONLY authorization source for public DIRECT",
                text,
                filename,
            )
            self.assertIn(
                "raw and rule_sets must not target DIRECT or COMPATIBLE.",
                text,
                filename,
            )
            self.assertIn("HTTP rule-provider updates default to FINAL-EXIT", text, filename)
            self.assertFalse(parsed.get("client", {}).get("dns", {}).get("respect_rules"))
            self.assertEqual(
                parsed.get("client", {}).get("egress_recovery"),
                {
                    "enabled": True,
                    "max_rotations": 2,
                    "exclusion_ttl_seconds": 120,
                    "retry_source_refresh": True,
                },
                filename,
            )
            alerts = parsed.get("alerts", {})
            self.assertIsInstance(alerts, dict)
            self.assertNotIn("experimental_bridges", alerts, filename)
            for retired_type in RETIRED_ALERT_CHANNEL_TYPES:
                self.assertNotIn(retired_type, text, filename)
            for channel in alerts.get("channels", []):
                self.assertIn(channel["type"], SUPPORTED_ALERT_CHANNEL_TYPES)
            validate_local_config(parsed)

    def test_alert_validation_accepts_legacy_formal_sections_and_retired_read_compatibility(self) -> None:
        payload = {
            "alerts": {
                "slack": {
                    "enabled": True,
                    "url": "https://hooks.slack.test/services/release",
                },
                "channels": [
                    {
                        "id": "mail",
                        "type": "email_sendgrid",
                        "enabled": True,
                        "config": {
                            "enabled": True,
                            "api_key": "secret",
                            "from_addr": "from@example.test",
                            "to_addrs": ["to@example.test"],
                        },
                    },
                    {
                        "id": "old-apprise",
                        "type": "apprise",
                        "enabled": True,
                        "config": {"enabled": True, "urls": ["json://legacy"]},
                    },
                ],
            }
        }

        validate_local_config(payload)

    def test_alert_validation_rejects_unknown_or_incomplete_enabled_channels(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported type"):
            validate_local_config(
                {
                    "alerts": {
                        "channels": [
                            {"id": "unknown", "type": "not-a-provider", "config": {}}
                        ]
                    }
                }
            )

        with self.assertRaisesRegex(ValueError, "server and topic"):
            validate_local_config(
                {
                    "alerts": {
                        "channels": [
                            {
                                "id": "phone",
                                "type": "ntfy",
                                "enabled": True,
                                "config": {"enabled": True, "server": "https://ntfy.sh"},
                            }
                        ]
                    }
                }
            )

    def test_local_config_deep_merges_over_base(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            base = temp_path / "config.yaml"
            local = temp_path / "config.local.yaml"
            base.write_text(
                """
client:
  mode: clash
  system_proxy:
    enabled: false
    host: 127.0.0.1
    port: 7890
tiers:
  tier2:
    subscription_urls: []
""",
                encoding="utf-8",
            )
            local.write_text(
                """
client:
  system_proxy:
    enabled: true
tiers:
  tier2:
    subscription_urls:
      - https://example.com/sub
""",
                encoding="utf-8",
            )

            config = load_config(str(base), local_path=str(local))

            self.assertEqual(config["client"]["mode"], "clash")
            self.assertTrue(config["client"]["system_proxy"]["enabled"])
            self.assertEqual(config["client"]["system_proxy"]["host"], "127.0.0.1")
            self.assertEqual(config["tiers"]["tier2"]["subscription_urls"], ["https://example.com/sub"])

    def test_missing_local_config_keeps_base(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir) / "config.yaml"
            base.write_text("client:\n  mode: clash\n", encoding="utf-8")

            config = load_config(str(base), local_path="missing.yaml")

            self.assertEqual(config["client"]["mode"], "clash")

    def test_explicit_empty_local_direct_rules_remove_base_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            base = temp_path / "config.yaml"
            local = temp_path / "config.local.yaml"
            base.write_text(
                """
routing:
  rules:
    direct:
      - geosite:CN
      - geoip:CN
    proxy:
      - example.com
""",
                encoding="utf-8",
            )
            local.write_text(
                """
routing:
  rules:
    direct: []
""",
                encoding="utf-8",
            )

            config = load_config(str(base), local_path=str(local))

            self.assertEqual(config["routing"]["rules"]["direct"], [])
            self.assertEqual(config["routing"]["rules"]["proxy"], ["example.com"])

    def test_legacy_flat_empty_direct_override_matches_core_routing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            base = temp_path / "config.yaml"
            local = temp_path / "config.local.yaml"
            base.write_text(
                """
routing:
  rules:
    direct:
      - geosite:CN
      - geoip:CN
""",
                encoding="utf-8",
            )
            local.write_text("routing:\n  direct: []\n", encoding="utf-8")

            config = load_config(str(base), local_path=str(local))

            self.assertEqual(config["routing"]["rules"]["direct"], [])
            self.assertEqual(build_custom_routing_rules(config["routing"]), [])

    def test_nested_local_rules_override_legacy_flat_base_rules(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            base = temp_path / "config.yaml"
            local = temp_path / "config.local.yaml"
            base.write_text("routing:\n  direct: []\n", encoding="utf-8")
            local.write_text(
                """
routing:
  rules:
    direct:
      - geosite:CN
      - geoip:CN
""",
                encoding="utf-8",
            )

            config = load_config(str(base), local_path=str(local))

            self.assertNotIn("direct", config["routing"])
            self.assertEqual(
                config["routing"]["rules"]["direct"],
                ["geosite:CN", "geoip:CN"],
            )
            self.assertEqual(
                build_custom_routing_rules(config["routing"]),
                ["GEOSITE,CN,DIRECT", "GEOIP,CN,DIRECT,no-resolve"],
            )


if __name__ == "__main__":
    unittest.main()
