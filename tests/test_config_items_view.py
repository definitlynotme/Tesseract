import unittest

from config_items_view import (
    config_item_table_values,
    config_items_signature,
    config_items_table_state,
    config_kind_label,
    configured_config_items,
    filtered_config_items,
)


class ConfigItemsViewTests(unittest.TestCase):
    def test_filtered_config_items_preserves_all_or_selected_tier(self) -> None:
        tier2 = {"tier": "tier2", "kind": "node", "label": "Gateway"}
        tier3 = {"tier": "tier3", "kind": "subscription", "label": "Provider"}
        exit_subscription = {"tier": "exit", "kind": "subscription", "label": "Exit Provider"}

        self.assertEqual(filtered_config_items([tier2, "bad", tier3, exit_subscription], "all"), [tier2, exit_subscription])
        self.assertEqual(filtered_config_items([tier2, "bad", tier3], "tier3"), [])
        self.assertEqual(filtered_config_items([tier2, exit_subscription], "exit"), [exit_subscription])

    def test_config_items_signature_preserves_refresh_contract(self) -> None:
        item = {
            "tier": "tier3",
            "kind": "subscription",
            "section": "sources",
            "key": "sub-1",
            "label": "Provider",
            "detail": "https://sub.example/list",
            "enabled": False,
        }

        self.assertEqual(
            config_items_signature("tier3", [item]),
            (("tier3", "tier3", "subscription", "sources", "sub-1", "Provider", "https://sub.example/list", "False"),),
        )

        enabled_item = {**item, "enabled": True}
        self.assertNotEqual(
            config_items_signature("tier3", [item]),
            config_items_signature("tier3", [enabled_item]),
        )

    def test_config_items_table_state_combines_filter_title_and_signature(self) -> None:
        tier2 = {"tier": "tier2", "kind": "node", "section": "", "key": "node-1", "label": "Gateway", "detail": "198.51.100.1:443"}
        tier3 = {"tier": "tier3", "kind": "subscription", "section": "", "key": "sub-1", "label": "Provider", "detail": "https://sub.example/list"}

        state = config_items_table_state(
            [tier2, tier3, "bad"],
            "tier3",
            tier_label=lambda tier: {"tier3": "Bastion"}.get(str(tier), str(tier)),
            translate=lambda text: {"已配置项目": "Configured items"}.get(text, text),
        )

        self.assertEqual(state.title, "Bastion 已配置项目")
        self.assertEqual(state.items, [])
        self.assertEqual(state.signature, config_items_signature("tier3", []))

        all_state = config_items_table_state([tier2], "all", tier_label=str, translate=lambda text: "Configured items")

        self.assertEqual(all_state.title, "Configured items")
        self.assertEqual(all_state.items, [tier2])

    def test_config_kind_label_matches_desktop_labels(self) -> None:
        translate = lambda text: {"订阅": "Subscription", "节点": "Node", "sources": "Sources"}.get(text, text)

        self.assertEqual(config_kind_label({"kind": "subscription"}, translate=translate), "Subscription")
        self.assertEqual(config_kind_label({"kind": "source", "section": "sources"}, translate=translate), "Sources")
        self.assertEqual(config_kind_label({"kind": "node"}, translate=translate), "Node")

    def test_config_item_table_values_preserves_desktop_columns(self) -> None:
        item = {
            "tier": "tier2",
            "kind": "node",
            "label": "Gateway",
            "detail": "198.51.100.1:443",
        }

        self.assertEqual(
            config_item_table_values(
                item,
                tier_label=lambda tier: {"tier2": "Tier2"}.get(str(tier), str(tier)),
                translate=lambda text: {"节点": "Node"}.get(text, text),
            ),
            ["Tier2", "Node", "Gateway", "198.51.100.1:443"],
        )

    def test_configured_config_items_builds_nodes_subscriptions_and_sources(self) -> None:
        local_config = {
            "tiers": {
                "tier1": {
                    "nodes": [
                        {
                            "name": "Tokyo",
                            "address": "203.0.113.10",
                            "port": 443,
                            "vless_link": "vless://secret@example.com:443#tokyo",
                        }
                    ]
                },
                "tier2": {
                    "nodes": [
                        {
                            "name": "Commercial",
                            "uri": "vless://commercial@example.com:443#secret",
                            "enabled": False,
                        },
                        "vless://free.example.net:8443#free",
                    ],
                    "subscription_urls": ["https://example.org/sub?token=secret"],
                },
                "tier3": {
                    "subscription_urls": [
                        {
                            "url": "https://tier3.example/sub?token=hidden",
                            "enabled": "false",
                            "label": "Paused provider",
                        }
                    ],
                },
            }
        }
        sources = {
            "github_repos": [{"url": "https://raw.githubusercontent.com/owner/repo/main/sub.txt"}],
            "telegram_channels": ["@free_proxy_nodes"],
        }

        items = configured_config_items(local_config, sources)

        tier1 = next(item for item in items if item["tier"] == "tier1")
        tier2_nodes = [item for item in items if item["tier"] == "tier2" and item["kind"] == "node"]
        subscription = next(item for item in items if item["tier"] == "tier2" and item["kind"] == "subscription")
        tier3_subscription = next(item for item in items if item["tier"] == "tier3" and item["kind"] == "subscription")
        source = next(item for item in items if item["kind"] == "source" and item["section"] == "github_repos")
        self.assertEqual(tier1["detail"], "203.0.113.10:443")
        self.assertEqual(tier1["protocol"], "vless")
        self.assertTrue(tier1["enabled"])
        self.assertEqual(tier2_nodes[0]["label"], "Commercial")
        self.assertEqual(tier2_nodes[0]["detail"], "vless://example.com:443")
        self.assertFalse(tier2_nodes[0]["enabled"])
        self.assertEqual(tier2_nodes[1]["label"], "free.example.net:8443")
        self.assertTrue(tier2_nodes[1]["enabled"])
        self.assertEqual(subscription["detail"], "https://example.org")
        self.assertEqual(subscription["url"], "https://example.org/sub?token=secret")
        self.assertEqual(subscription["uri"], "https://example.org/sub?token=secret")
        self.assertTrue(subscription["enabled"])
        self.assertTrue(subscription["editable"])
        self.assertEqual(tier3_subscription["url"], "https://tier3.example/sub?token=hidden")
        self.assertEqual(tier3_subscription["uri"], "https://tier3.example/sub?token=hidden")
        self.assertEqual(tier3_subscription["label"], "Paused provider")
        self.assertFalse(tier3_subscription["enabled"])
        self.assertTrue(tier3_subscription["editable"])
        self.assertEqual(source["detail"], "https://raw.githubusercontent.com")
        self.assertNotIn("token=secret", subscription["detail"])

    def test_configured_config_items_skips_empty_or_malformed_entries(self) -> None:
        items = configured_config_items(
            {
                "tiers": {
                    "tier1": {"nodes": [{"address": "missing-name"}, "bad"]},
                    "tier2": {"nodes": ["", {"name": "missing-uri"}], "subscription_urls": [""]},
                }
            },
            {"github_repos": [{"enabled": True}], "local_files": ["C:/subs/free.txt"]},
        )

        self.assertEqual(
            items,
            [
                {
                    "tier": "tier3",
                    "kind": "source",
                    "section": "local_files",
                    "key": "C:/subs/free.txt",
                    "label": "local_files",
                    "detail": "free.txt",
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
