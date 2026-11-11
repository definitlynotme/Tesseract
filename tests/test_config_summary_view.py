import unittest

from config_summary_view import config_summary, config_summary_with_runtime_status, sources_summary


class ConfigSummaryViewTests(unittest.TestCase):
    def test_config_summary_builds_masked_status_payload(self) -> None:
        summary = config_summary(
            {
                "tiers": {
                    "tier1": {
                        "nodes": [
                            {
                                "name": "Tokyo",
                                "address": "203.0.113.10",
                                "port": 443,
                                "root_password": "secret",
                                "protocols": {"vless": {}, "hysteria2": {}},
                            }
                        ]
                    },
                    "tier2": {
                        "nodes": [{"name": "Commercial", "uri": "vless://secret@example.com:443#node"}],
                        "subscription_urls": ["https://user:token@example.org/sub"],
                    },
                    "tier3": {
                        "enabled": True,
                    },
                },
                "egress_proxy": {
                    "enabled": True,
                    "server": "proxy.internal.example.com",
                    "port": 8080,
                    "username": "operator",
                    "password": "secret",
                },
            },
            {"github_discovery": {"enabled": True}, "github_repos": ["https://example.com/free.txt"]},
        )

        self.assertEqual(summary["tier1"]["total"], 1)
        self.assertTrue(summary["tier1"]["nodes"][0]["has_server_login"])
        self.assertEqual(summary["tier1"]["nodes"][0]["protocols"], ["hysteria2", "vless"])
        self.assertEqual(summary["tier2"]["subscriptions"][0]["host"], "example.org")
        self.assertIn("fingerprint", summary["tier2"]["subscriptions"][0])
        self.assertTrue(summary["tier3"]["enabled"])
        self.assertEqual(summary["tier3"]["tier3b_source_count"], 1)
        self.assertEqual(summary["tier3"]["bootstrap_source_count"], 1)
        self.assertEqual(summary["egress_proxy"]["server"], "p***.example.com")
        self.assertTrue(summary["egress_proxy"]["has_username"])
        self.assertTrue(summary["sources"]["github_discovery_enabled"])
        self.assertNotIn("token", str(summary["tier2"]["subscriptions"][0]))

    def test_config_summary_treats_invalid_sections_as_empty(self) -> None:
        summary = config_summary(
            {"tiers": "invalid", "egress_proxy": "invalid", "killswitch": "invalid"},
            {"github_discovery": "invalid"},
        )

        self.assertEqual(summary["tier1"]["total"], 0)
        self.assertFalse(summary["egress_proxy"]["enabled"])
        self.assertFalse(summary["killswitch"]["enabled"])
        self.assertFalse(summary["sources"]["github_discovery_enabled"])

    def test_config_summary_separates_saved_enabled_and_disabled_nodes(self) -> None:
        summary = config_summary(
            {
                "tiers": {
                    "tier1": {
                        "nodes": [
                            {"name": "Active", "address": "active.example", "port": 443},
                            {
                                "name": "Draft",
                                "address": "draft.example",
                                "port": 8443,
                                "enabled": False,
                            },
                        ]
                    },
                    "tier2": {
                        "nodes": [
                            "vless://legacy.example:443#legacy",
                            {"uri": "vless://draft.example:443#draft", "enabled": "false"},
                        ],
                        "subscription_urls": [
                            "https://active.example/sub",
                            {"url": "https://draft.example/sub", "enabled": "false"},
                        ],
                    },
                    "tier3": {"nodes": [{"uri": "ss://active", "enabled": True}]},
                    "exit": {
                        "nodes": [{"uri": "ss://draft", "enabled": False}],
                        "subscription_urls": ["https://exit.example/sub"],
                    },
                }
            },
            {},
        )

        self.assertEqual(
            {
                tier: (
                    summary[tier]["total"],
                    summary[tier]["enabled_total"],
                    summary[tier]["disabled_total"],
                )
                for tier in ("tier1", "tier2", "tier3", "exit")
            },
            {
                "tier1": (2, 1, 1),
                "tier2": (2, 1, 1),
                "tier3": (1, 1, 0),
                "exit": (1, 0, 1),
            },
        )
        self.assertTrue(summary["tier1"]["nodes"][0]["enabled"])
        self.assertFalse(summary["tier1"]["nodes"][1]["enabled"])
        self.assertTrue(summary["tier2"]["nodes"][0]["enabled"])
        self.assertFalse(summary["tier2"]["nodes"][1]["enabled"])
        self.assertEqual(summary["tier2"]["subscription_count"], 2)
        self.assertEqual(summary["tier2"]["enabled_subscription_count"], 1)
        self.assertEqual(summary["tier2"]["disabled_subscription_count"], 1)
        self.assertTrue(summary["tier2"]["subscriptions"][0]["enabled"])
        self.assertFalse(summary["tier2"]["subscriptions"][1]["enabled"])
        self.assertTrue(summary["exit"]["configured"])

    def test_config_summary_with_runtime_status_adds_tier_runtime_counts(self) -> None:
        summary = {"tier1": {}, "tier2": {}, "tier3": {}, "exit": {}}

        result = config_summary_with_runtime_status(
            summary,
            {"tiers": {"tier3": {"total": 10, "healthy": 3, "last_check_at": "now"}}},
        )

        self.assertEqual(result["tier3"]["runtime"], {"total": 10, "healthy": 3, "last_check_at": "now"})
        self.assertNotIn("runtime", result["tier1"])

    def test_sources_summary_counts_sections(self) -> None:
        self.assertEqual(
            sources_summary(
                {
                    "github_repos": ["a", "b"],
                    "telegram_channels": ["@c"],
                    "subscription_urls": ["https://example.com/sub"],
                    "local_files": ["C:/subs/free.txt"],
                    "github_discovery": {"enabled": True},
                }
            ),
            {
                "github_repos": 2,
                "telegram_channels": 1,
                "subscription_urls": 1,
                "local_files": 1,
                "github_discovery_enabled": True,
            },
        )


if __name__ == "__main__":
    unittest.main()
