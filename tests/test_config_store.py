import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config_store import ConfigStore
from exit_nodes import exit_nodes_from_entry
from gateway_selection import GatewaySelection, gateway_identity, read_selection

# Two manual Gateway entries that share the name 'JP 01' (different servers),
# B rotated to a new password, and a same-identity copy of B under another
# URI string (the fragment is the name, which is not identity material).
GATEWAY_A_URI = "trojan://pass-a@jp-a.example.invalid:443?sni=jp-a.example.invalid#JP%2001"
GATEWAY_A2_URI = "trojan://pass-a2@jp-a.example.invalid:443?sni=jp-a.example.invalid#JP%2001"
GATEWAY_B_URI = "trojan://pass-b@jp-b.example.invalid:443?sni=jp-b.example.invalid#JP%2001"
GATEWAY_B2_URI = "trojan://pass-b2@jp-b.example.invalid:443?sni=jp-b.example.invalid#JP%2001"
GATEWAY_B_COPY_URI = "trojan://pass-b@jp-b.example.invalid:443?sni=jp-b.example.invalid#JP%2001%20copy"
SELECTION_KEYS = ("selected", "selected_proxy", "selected_node")


def _entry_identity(entry) -> str:
    (node,) = exit_nodes_from_entry(entry, 0)
    return gateway_identity(node)


def _selection_fields(exit_payload: dict) -> dict:
    return {key: exit_payload[key] for key in SELECTION_KEYS if key in exit_payload}


class ConfigStoreTests(unittest.TestCase):
    def test_atomic_save_retries_a_transient_file_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            local = root / 'config.local.yaml'
            local.write_text('tiers: {}\n', encoding='utf-8')
            store = ConfigStore(local_config_path=local, sources_path=root / 'sources.yaml',
                                backup_dir=root / 'backups')
            original = os.replace
            calls = []

            def replace(source, target):
                calls.append((source, target))
                if len(calls) == 1:
                    raise PermissionError('controlled transient file lock')
                return original(source, target)

            with patch('config_store.os.replace', replace):
                store.write_local({'tiers': {}})
            self.assertEqual(len(calls), 2)
            self.assertEqual(local.read_text(encoding='utf-8'), 'tiers: {}\n')
            self.assertEqual(list(root.glob('*.tmp')), [])

    def test_persistent_atomic_save_failure_preserves_the_old_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            local = root / 'config.local.yaml'
            local.write_text('old: value\n', encoding='utf-8')
            store = ConfigStore(local_config_path=local, sources_path=root / 'sources.yaml',
                                backup_dir=root / 'backups')
            with patch('config_store.os.replace', side_effect=PermissionError('locked')) as replace:
                with self.assertRaises(PermissionError):
                    store.write_local({'tiers': {}})
            self.assertEqual(replace.call_count, 4)
            self.assertEqual(local.read_text(encoding='utf-8'), 'old: value\n')
            self.assertEqual(list(root.glob('*.tmp')), [])

    def _store(self, temp_path: Path) -> ConfigStore:
        local = temp_path / "config.local.yaml"
        sources = temp_path / "sources.yaml"
        local.write_text(
            """
tiers:
  tier1:
    nodes:
      - name: Osaka
        address: 203.0.113.10
        port: 443
        root_password: secret
        vless_link: vless://secret@example.com:443#node
        protocols:
          reality:
            port: 443
  tier2:
    nodes:
      - name: Commercial
        uri: vless://commercial@example.com:443#commercial
    subscription_urls:
      - https://example.com/sub?token=secret
  tier3:
    enabled: false
    nodes:
      - name: Paid-Untrusted
        uri: vless://paid@example.com:443#paid
    subscription_urls:
      - https://paid.example.com/sub
    bootstrap_enabled: true
  exit:
    subscription_urls:
      - https://exit.example.com/sub?token=secret
    nodes:
      - ss://YWVzLTI1Ni1nY206cGFzc0BleGl0LmV4YW1wbGUuY29tOjgzODg#Exit-SS
egress_proxy:
  enabled: true
  server: proxy.example.com
  port: 8000
  username: user
  password: pass
killswitch:
  enabled: false
  allow_lan: false
  additional_whitelist:
    - 203.0.113.20
""",
            encoding="utf-8",
        )
        sources.write_text(
            """
github_repos:
  - https://example.com/free.txt
telegram_channels:
  - '@free_proxy_nodes'
subscription_urls: []
local_files: []
github_discovery:
  enabled: true
""",
            encoding="utf-8",
        )
        return ConfigStore(local_config_path=local, sources_path=sources, backup_dir=temp_path / "backups")

    def test_summary_redacts_sensitive_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))

            summary = store.summary()

            self.assertEqual(summary["tier1"]["total"], 1)
            self.assertTrue(summary["tier1"]["nodes"][0]["has_server_login"])
            self.assertNotIn("root_password", str(summary))
            self.assertNotIn("secret", str(summary))
            self.assertEqual(summary["tier2"]["subscriptions"][0]["host"], "example.com")
            self.assertIn("fingerprint", summary["tier2"]["subscriptions"][0])
            self.assertEqual(summary["tier2"]["total"], 1)
            self.assertFalse(summary["tier3"]["enabled"])
            self.assertEqual(summary["tier3"]["total"], 1)
            self.assertEqual(summary["tier3"]["subscription_count"], 1)
            self.assertEqual(summary["tier3"]["tier3b_source_count"], 2)
            self.assertEqual(summary["tier3"]["bootstrap_source_count"], 2)
            self.assertTrue(summary["exit"]["configured"])
            self.assertEqual(summary["exit"]["subscription_count"], 1)
            self.assertFalse(summary["killswitch"]["enabled"])
            self.assertFalse(summary["killswitch"]["allow_lan"])
            self.assertEqual(summary["killswitch"]["additional_whitelist_count"], 1)

    def test_summary_treats_string_false_as_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            local = temp_path / "config.local.yaml"
            sources = temp_path / "sources.yaml"
            local.write_text(
                """
tiers:
  tier3:
    enabled: 'false'
    bootstrap_enabled: 'false'
egress_proxy:
  enabled: 'false'
killswitch:
  enabled: 'false'
  allow_lan: 'false'
""",
                encoding="utf-8",
            )
            sources.write_text(
                """
github_discovery:
  enabled: 'false'
""",
                encoding="utf-8",
            )
            store = ConfigStore(local_config_path=local, sources_path=sources, backup_dir=temp_path / "backups")

            summary = store.summary()

            self.assertFalse(summary["tier3"]["enabled"])
            self.assertFalse(summary["tier3"]["bootstrap_enabled"])
            self.assertFalse(summary["egress_proxy"]["enabled"])
            self.assertFalse(summary["killswitch"]["enabled"])
            self.assertFalse(summary["killswitch"]["allow_lan"])
            self.assertFalse(summary["sources"]["github_discovery_enabled"])

    def test_summary_defaults_for_invalid_nested_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            local = temp_path / "config.local.yaml"
            sources = temp_path / "sources.yaml"
            local.write_text(
                """
tiers: invalid
egress_proxy: invalid
killswitch: invalid
""",
                encoding="utf-8",
            )
            sources.write_text("{}\n", encoding="utf-8")
            store = ConfigStore(local_config_path=local, sources_path=sources, backup_dir=temp_path / "backups")

            summary = store.summary()

            self.assertEqual(summary["tier1"]["total"], 0)
            self.assertEqual(summary["tier2"]["subscription_count"], 0)
            self.assertFalse(summary["tier3"]["enabled"])
            self.assertFalse(summary["exit"]["configured"])
            self.assertFalse(summary["egress_proxy"]["enabled"])
            self.assertFalse(summary["killswitch"]["enabled"])

    def test_write_local_creates_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))

            result = store.write_local({"tiers": {"tier1": {"nodes": []}, "tier2": {"subscription_urls": []}}})

            self.assertTrue(result["backup"])
            self.assertTrue(Path(result["backup"]).exists())
            self.assertEqual(store.read_local()["tiers"]["tier1"]["nodes"], [])

    def test_write_local_trims_old_backups(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            store = ConfigStore(
                local_config_path=temp_path / "config.local.yaml",
                sources_path=temp_path / "sources.yaml",
                backup_dir=temp_path / "backups",
                max_backups_per_file=3,
            )
            store.local_config_path.write_text("tiers: {}\n", encoding="utf-8")
            store.sources_path.write_text("{}\n", encoding="utf-8")

            for index in range(6):
                store.write_local({"tiers": {"tier1": {"nodes": [{"name": f"n{index}", "address": "example.com", "port": 443}]}}})

            backups = list((temp_path / "backups").glob("config.local.yaml.*.bak"))
            self.assertEqual(len(backups), 3)

    def test_write_local_keeps_unique_backups_when_timestamp_collides(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            store = ConfigStore(
                local_config_path=temp_path / "config.local.yaml",
                sources_path=temp_path / "sources.yaml",
                backup_dir=temp_path / "backups",
                max_backups_per_file=10,
            )
            store.local_config_path.write_text("tiers: {}\n", encoding="utf-8")
            store.sources_path.write_text("{}\n", encoding="utf-8")

            with patch("config_store.datetime") as fake_datetime:
                fake_datetime.now.return_value.strftime.return_value = "20260101T000000000000Z"
                for index in range(4):
                    store.write_local(
                        {
                            "tiers": {
                                "tier1": {
                                    "nodes": [
                                        {"name": f"n{index}", "address": "example.com", "port": 443}
                                    ]
                                }
                            }
                        }
                    )

            backup_names = sorted(path.name for path in (temp_path / "backups").glob("config.local.yaml.*.bak"))
            self.assertEqual(
                backup_names,
                [
                    "config.local.yaml.20260101T000000000000Z.1.bak",
                    "config.local.yaml.20260101T000000000000Z.2.bak",
                    "config.local.yaml.20260101T000000000000Z.3.bak",
                    "config.local.yaml.20260101T000000000000Z.bak",
                ],
            )

    def test_tier1_node_crud(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))

            store.add_tier1_node({"name": "Dallas", "address": "198.51.100.2", "port": 8443})
            self.assertEqual(len(store.read_local()["tiers"]["tier1"]["nodes"]), 2)
            store.update_tier1_node("Dallas", {"port": 443})
            nodes = store.read_local()["tiers"]["tier1"]["nodes"]
            self.assertEqual(next(node for node in nodes if node["name"] == "Dallas")["port"], 443)
            store.delete_tier1_node("Dallas")
            self.assertEqual(len(store.read_local()["tiers"]["tier1"]["nodes"]), 1)

    def test_tier_crud_replaces_invalid_tiers_section_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            local = temp_path / "config.local.yaml"
            sources = temp_path / "sources.yaml"
            local.write_text("tiers: invalid\n", encoding="utf-8")
            sources.write_text("{}\n", encoding="utf-8")
            store = ConfigStore(local_config_path=local, sources_path=sources, backup_dir=temp_path / "backups")

            store.add_tier1_node({"name": "Dallas", "address": "198.51.100.2", "port": 8443})

            self.assertEqual(
                store.read_local()["tiers"]["tier1"]["nodes"],
                [{"name": "Dallas", "address": "198.51.100.2", "port": 8443}],
            )

    def test_tier2_subscription_crud(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))

            store.add_tier2_subscription("https://example.org/sub")
            self.assertIn("https://example.org/sub", store.read_local()["tiers"]["tier2"]["subscription_urls"])
            store.delete_tier2_subscription("https://example.org/sub")
            self.assertNotIn("https://example.org/sub", store.read_local()["tiers"]["tier2"]["subscription_urls"])

    def test_tier3_subscription_and_node_crud(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))

            store.add_tier3_subscription("https://paid2.example.org/sub")
            store.add_tier3_node({"name": "Paid-2", "uri": "vless://paid2@example.com:443#paid2"})
            self.assertIn("https://paid2.example.org/sub", store.read_local()["tiers"]["tier3"]["subscription_urls"])
            self.assertEqual(len(store.read_local()["tiers"]["tier3"]["nodes"]), 2)
            store.delete_tier3_subscription("https://paid2.example.org/sub")
            store.delete_tier3_node("vless://paid2@example.com:443#paid2")
            self.assertNotIn("https://paid2.example.org/sub", store.read_local()["tiers"]["tier3"]["subscription_urls"])
            self.assertEqual(len(store.read_local()["tiers"]["tier3"]["nodes"]), 1)

    def test_delete_imported_nodes_and_exit_subscription(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))
            payload = store.read_local()
            payload.setdefault("tiers", {}).setdefault("exit", {})["selected"] = "Exit-SS"
            payload["tiers"]["exit"]["selected_proxy"] = "Exit-SS"
            store.write_local(payload)

            store.delete_tier2_node("vless://commercial@example.com:443#commercial")
            store.delete_exit_node("ss://YWVzLTI1Ni1nY206cGFzc0BleGl0LmV4YW1wbGUuY29tOjgzODg#Exit-SS")
            store.delete_exit_subscription("https://exit.example.com/sub?token=secret")
            payload = store.read_local()

            self.assertEqual(payload["tiers"]["tier2"]["nodes"], [])
            self.assertEqual(payload["tiers"]["exit"]["nodes"], [])
            self.assertEqual(payload["tiers"]["exit"]["subscription_urls"], [])
            self.assertNotIn("selected", payload["tiers"]["exit"])
            self.assertNotIn("selected_proxy", payload["tiers"]["exit"])

    def test_configured_items_and_delete_config_item(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))

            items = store.configured_items()
            tier2_node = next(item for item in items if item["tier"] == "tier2" and item["kind"] == "node")
            tier3_node = next(item for item in items if item["tier"] == "tier3" and item["kind"] == "node")
            exit_subscription = next(item for item in items if item["tier"] == "exit" and item["kind"] == "subscription")
            source = next(item for item in items if item["tier"] == "tier3" and item["kind"] == "source")
            self.assertEqual(tier2_node["detail"], "vless://example.com:443")
            self.assertNotIn("commercial@", tier2_node["detail"])
            store.delete_config_item(tier2_node)
            store.delete_config_item(tier3_node)
            store.delete_config_item(exit_subscription)
            store.delete_config_item(source)

            self.assertEqual(store.read_local()["tiers"]["tier2"]["nodes"], [])
            self.assertEqual(store.read_local()["tiers"]["tier3"]["nodes"], [])
            self.assertEqual(store.read_local()["tiers"]["exit"]["subscription_urls"], [])
            self.assertEqual(store.read_sources()["github_repos"], [])

    def test_configured_items_builds_imported_nodes_and_subscriptions_separately(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            local = temp_path / "config.local.yaml"
            sources = temp_path / "sources.yaml"
            local.write_text(
                """
tiers:
  tier2:
    nodes:
      - ""
      - name: Named
        uri: vless://secret@example.com:443#named
      - vless://free.example.net:8443#free
    subscription_urls:
      - ""
      - https://example.org/sub?token=secret
""",
                encoding="utf-8",
            )
            sources.write_text("{}\n", encoding="utf-8")
            store = ConfigStore(local_config_path=local, sources_path=sources, backup_dir=temp_path / "backups")

            tier2_items = [item for item in store.configured_items() if item["tier"] == "tier2"]
            node_items = [item for item in tier2_items if item["kind"] == "node"]
            subscription_items = [item for item in tier2_items if item["kind"] == "subscription"]

            self.assertEqual(len(node_items), 2)
            self.assertEqual(len(subscription_items), 1)
            self.assertEqual(node_items[0]["label"], "Named")
            self.assertEqual(node_items[0]["detail"], "vless://example.com:443")
            self.assertEqual(node_items[1]["label"], "free.example.net:8443")
            self.assertEqual(subscription_items[0]["detail"], "https://example.org")

    def test_update_config_item_updates_editable_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))

            items = store.configured_items()
            tier1_node = next(item for item in items if item["tier"] == "tier1" and item["kind"] == "node")
            tier2_node = next(item for item in items if item["tier"] == "tier2" and item["kind"] == "node")

            store.update_config_item(
                tier1_node,
                {
                    "name": "Osaka-1",
                    "address": "203.0.113.11",
                    "port": "8443",
                    "fallback_order": ["reality", "hysteria2"],
                    "protocols": {
                        "hysteria2": {"uri": "hy2://osaka.example:8443"},
                        "vless_reality": {"uri": "vless://osaka.example:8443"},
                    },
                },
            )
            store.update_config_item(
                tier2_node,
                {"name": "Commercial-1", "uri": "vless://commercial-1@example.com:8443#commercial-1"},
            )
            payload = store.read_local()

            self.assertEqual(payload["tiers"]["tier1"]["nodes"][0]["name"], "Osaka-1")
            self.assertEqual(payload["tiers"]["tier1"]["nodes"][0]["port"], 8443)
            self.assertEqual(payload["tiers"]["tier1"]["nodes"][0]["fallback_order"], ["reality", "hysteria2"])
            self.assertEqual(
                payload["tiers"]["tier1"]["nodes"][0]["protocols"],
                {
                    "hysteria2": {"uri": "hy2://osaka.example:8443"},
                    "vless_reality": {"uri": "vless://osaka.example:8443"},
                },
            )
            self.assertEqual(payload["tiers"]["tier2"]["nodes"][0]["name"], "Commercial-1")
            self.assertEqual(payload["tiers"]["tier2"]["nodes"][0]["uri"], "vless://commercial-1@example.com:8443#commercial-1")

    def test_node_enablement_updates_all_tiers_without_losing_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))
            payload = store.read_local()
            payload["tiers"]["tier2"]["nodes"][0]["origin"] = "manual-import"
            payload["tiers"]["tier3"]["nodes"][0]["note"] = "keep-me"
            payload["tiers"]["exit"]["selected"] = "Exit-SS"
            payload["tiers"]["exit"]["selected_proxy"] = "Exit-SS"
            store.write_local(payload)

            items = store.configured_items()
            nodes_by_tier = {
                tier: next(item for item in items if item["tier"] == tier and item["kind"] == "node")
                for tier in ("tier1", "tier2", "tier3", "exit")
            }
            store.update_config_item(nodes_by_tier["tier1"], {"enabled": False})
            store.set_config_item_enabled(nodes_by_tier["tier2"], "false")
            store.set_config_item_enabled(nodes_by_tier["tier3"], False)
            store.set_config_item_enabled(nodes_by_tier["exit"], False)

            payload = store.read_local()
            tier1_node = payload["tiers"]["tier1"]["nodes"][0]
            self.assertFalse(tier1_node["enabled"])
            self.assertEqual(tier1_node["root_password"], "secret")
            self.assertEqual(tier1_node["protocols"], {"reality": {"port": 443}})

            tier2_node = payload["tiers"]["tier2"]["nodes"][0]
            self.assertFalse(tier2_node["enabled"])
            self.assertEqual(tier2_node["name"], "Commercial")
            self.assertEqual(tier2_node["origin"], "manual-import")

            tier3_node = payload["tiers"]["tier3"]["nodes"][0]
            self.assertFalse(tier3_node["enabled"])
            self.assertEqual(tier3_node["name"], "Paid-Untrusted")
            self.assertEqual(tier3_node["note"], "keep-me")

            exit_node = payload["tiers"]["exit"]["nodes"][0]
            self.assertEqual(
                exit_node,
                {
                    "uri": "ss://YWVzLTI1Ni1nY206cGFzc0BleGl0LmV4YW1wbGUuY29tOjgzODg#Exit-SS",
                    "enabled": False,
                },
            )
            self.assertNotIn("selected", payload["tiers"]["exit"])
            self.assertNotIn("selected_proxy", payload["tiers"]["exit"])

    def test_disabling_an_unselected_exit_node_keeps_the_current_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))
            payload = store.read_local()
            payload["tiers"]["exit"]["selected"] = "Another-Gateway"
            payload["tiers"]["exit"]["selected_proxy"] = "Another-Gateway"
            store.write_local(payload)
            exit_node = next(
                item
                for item in store.configured_items()
                if item["tier"] == "exit" and item["kind"] == "node"
            )

            store.set_config_item_enabled(exit_node, False)

            exit_payload = store.read_local()["tiers"]["exit"]
            self.assertEqual(exit_payload["selected"], "Another-Gateway")
            self.assertEqual(exit_payload["selected_proxy"], "Another-Gateway")

    def test_disabling_bare_exit_uri_clears_parser_generated_selection_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))
            bare_uri = "ss://YWVzLTI1Ni1nY206cGFzc0BleGl0LmV4YW1wbGUuY29tOjgzODg"
            payload = store.read_local()
            payload["tiers"]["exit"]["nodes"] = [bare_uri]
            payload["tiers"]["exit"]["selected"] = "exit.example.com"
            payload["tiers"]["exit"]["selected_proxy"] = "exit.example.com"
            store.write_local(payload)
            exit_node = next(
                item
                for item in store.configured_items()
                if item["tier"] == "exit" and item["kind"] == "node"
            )

            store.set_config_item_enabled(exit_node, False)

            exit_payload = store.read_local()["tiers"]["exit"]
            self.assertEqual(exit_payload["nodes"], [{"uri": bare_uri, "enabled": False}])
            self.assertNotIn("selected", exit_payload)
            self.assertNotIn("selected_proxy", exit_payload)

    def test_exit_subscription_add(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))

            store.add_exit_subscription("https://exit.example.org/sub")

            self.assertIn("https://exit.example.org/sub", store.read_local()["tiers"]["exit"]["subscription_urls"])

    def test_subscription_enablement_updates_all_supported_tiers_and_preserves_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))
            payload = store.read_local()
            payload["tiers"]["tier3"]["subscription_urls"] = [
                {
                    "url": "https://paid.example.com/sub",
                    "headers": {"Authorization": "Bearer secret"},
                    "label": "Paid provider",
                }
            ]
            payload["tiers"]["exit"]["selected"] = "Manual-Gateway"
            payload["tiers"]["exit"]["selected_proxy"] = "Manual-Gateway"
            store.write_local(payload)

            items = {
                item["tier"]: item
                for item in store.configured_items()
                if item["kind"] == "subscription"
            }
            for tier in ("tier2", "tier3", "exit"):
                self.assertTrue(items[tier]["enabled"])
                self.assertTrue(items[tier]["editable"])
                store.set_config_item_enabled(items[tier], "false")

            payload = store.read_local()
            self.assertEqual(
                payload["tiers"]["tier2"]["subscription_urls"],
                [{"url": "https://example.com/sub?token=secret", "enabled": False}],
            )
            self.assertEqual(
                payload["tiers"]["tier3"]["subscription_urls"][0]["headers"],
                {"Authorization": "Bearer secret"},
            )
            self.assertEqual(payload["tiers"]["tier3"]["subscription_urls"][0]["label"], "Paid provider")
            self.assertFalse(payload["tiers"]["tier3"]["subscription_urls"][0]["enabled"])
            self.assertEqual(payload["tiers"]["exit"]["selected"], "Manual-Gateway")
            self.assertEqual(payload["tiers"]["exit"]["selected_proxy"], "Manual-Gateway")

    def test_subscription_url_and_enabled_are_updated_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))
            payload = store.read_local()
            payload["tiers"]["tier2"]["subscription_urls"].append("https://duplicate.example/sub")
            store.write_local(payload)
            item = next(
                item
                for item in store.configured_items()
                if item["tier"] == "tier2"
                and item["kind"] == "subscription"
                and item["key"].startswith("https://example.com/")
            )

            store.update_config_item(
                item,
                {"url": "https://renamed.example/sub", "enabled": False},
            )

            entries = store.read_local()["tiers"]["tier2"]["subscription_urls"]
            self.assertEqual(
                entries[0],
                {"url": "https://renamed.example/sub", "enabled": False},
            )
            with self.assertRaisesRegex(ValueError, "already exists"):
                store.update_config_item(
                    {**item, "key": "https://renamed.example/sub"},
                    {"url": "https://duplicate.example/sub", "enabled": True},
                )

            store.delete_tier2_subscription("https://renamed.example/sub")
            self.assertEqual(
                store.read_local()["tiers"]["tier2"]["subscription_urls"],
                ["https://duplicate.example/sub"],
            )

    def test_set_exit_selected_proxy_persists_and_clears_choice(self) -> None:
        """A name-only pick (T-GW-ID layout): ``selected_proxy`` holds the
        name; ``selected`` (an older build's key, which would win over every
        other field) is never written and is popped, and so is the record of
        an earlier pick.  Clearing pops ``selected``, ``selected_proxy`` and
        ``selected_node``; the rest of the exit block is kept."""

        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))
            payload = store.read_local()
            exit_nodes = list(payload["tiers"]["exit"]["nodes"])
            exit_subscriptions = list(payload["tiers"]["exit"]["subscription_urls"])
            payload["tiers"]["exit"]["selected"] = "Old"
            payload["tiers"]["exit"]["selected_proxy"] = "Old"
            payload["tiers"]["exit"]["selected_node"] = {"identity": "v1:" + "0" * 64, "label": "Old"}
            store.write_local(payload)

            store.set_exit_selected_proxy("Tokyo")
            exit_payload = store.read_local()["tiers"]["exit"]
            self.assertEqual(_selection_fields(exit_payload), {"selected_proxy": "Tokyo"})
            self.assertEqual(read_selection(exit_payload), GatewaySelection("legacy", "Tokyo"))

            # An older build re-picks (it writes ``selected``) beside a record.
            payload = store.read_local()
            payload["tiers"]["exit"]["selected"] = "Tokyo"
            payload["tiers"]["exit"]["selected_node"] = {"identity": "v1:" + "0" * 64, "label": "Tokyo"}
            store.write_local(payload)

            store.set_exit_selected_proxy("")
            exit_payload = store.read_local()["tiers"]["exit"]
            self.assertEqual(_selection_fields(exit_payload), {})
            self.assertEqual(read_selection(exit_payload), GatewaySelection("none"))
            self.assertEqual(exit_payload["nodes"], exit_nodes)
            self.assertEqual(exit_payload["subscription_urls"], exit_subscriptions)

    def test_set_exit_selection_writes_label_and_record_and_pops_selected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))
            payload = store.read_local()
            exit_nodes = list(payload["tiers"]["exit"]["nodes"])
            payload["tiers"]["exit"]["selected"] = "Old"
            store.write_local(payload)
            identity = _entry_identity({"name": "JP 01", "uri": GATEWAY_B_URI})
            self.assertRegex(identity, r"^v1:[0-9a-f]{64}$")

            store.set_exit_selection("JP 01", identity=identity)
            exit_payload = store.read_local()["tiers"]["exit"]
            self.assertEqual(
                _selection_fields(exit_payload),
                {
                    "selected_proxy": "JP 01",
                    "selected_node": {"identity": identity, "label": "JP 01"},
                },
            )
            self.assertEqual(read_selection(exit_payload), GatewaySelection("bound", "JP 01", identity=identity))
            self.assertEqual(read_selection(exit_payload).key, "id:" + identity)
            self.assertEqual(exit_payload["nodes"], exit_nodes)

            store.set_exit_selected_proxy("X")
            exit_payload = store.read_local()["tiers"]["exit"]
            self.assertEqual(_selection_fields(exit_payload), {"selected_proxy": "X"})
            self.assertEqual(read_selection(exit_payload).key, "name:X")

            # A name proven ambiguous is saved as a record too, and an older
            # build's ``selected`` is popped by every writer.
            payload = store.read_local()
            payload["tiers"]["exit"]["selected"] = "Old"
            store.write_local(payload)
            store.set_exit_selection("JP 01-2", unresolved="ambiguous")
            exit_payload = store.read_local()["tiers"]["exit"]
            self.assertEqual(
                _selection_fields(exit_payload),
                {
                    "selected_proxy": "JP 01-2",
                    "selected_node": {"unresolved": "ambiguous", "label": "JP 01-2"},
                },
            )
            self.assertEqual(read_selection(exit_payload).key, "unresolved:JP 01-2")

            store.set_exit_selected_proxy("")
            exit_payload = store.read_local()["tiers"]["exit"]
            self.assertEqual(_selection_fields(exit_payload), {})
            self.assertEqual(exit_payload["nodes"], exit_nodes)

            # An empty label turns the Gateway off even with an identity.
            store.set_exit_selection("JP 01", identity=identity)
            store.set_exit_selection("", identity=identity)
            self.assertEqual(_selection_fields(store.read_local()["tiers"]["exit"]), {})

    def test_compare_and_set_exit_selection_refuses_a_changed_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            store = self._store(temp_path)
            payload = store.read_local()
            payload["tiers"]["exit"]["selected"] = "Free-2"   # an older build's name-only pick
            store.write_local(payload)
            self.assertEqual(read_selection(store.read_local()["tiers"]["exit"]).key, "name:Free-2")
            identity = _entry_identity({"name": "Free", "uri": GATEWAY_A_URI})

            self.assertIs(store.compare_and_set_exit_selection("name:Free-2", "Free", identity=identity), True)
            exit_payload = store.read_local()["tiers"]["exit"]
            self.assertEqual(
                _selection_fields(exit_payload),
                {
                    "selected_proxy": "Free",
                    "selected_node": {"identity": identity, "label": "Free"},
                },
            )
            self.assertEqual(read_selection(exit_payload).key, "id:" + identity)

            before = store.local_config_path.read_bytes()
            backups = sorted(path.name for path in (temp_path / "backups").iterdir())
            self.assertIs(
                store.compare_and_set_exit_selection("name:Free-2", "Free-2", unresolved="ambiguous"),
                False,
            )
            self.assertEqual(store.local_config_path.read_bytes(), before)
            self.assertEqual(sorted(path.name for path in (temp_path / "backups").iterdir()), backups)

    def _gateway_store(self, temp_path: Path, entries: list) -> ConfigStore:
        temp_path.mkdir(parents=True, exist_ok=True)
        store = self._store(temp_path)
        payload = store.read_local()
        payload["tiers"]["exit"]["nodes"] = entries
        store.write_local(payload)
        return store

    def test_delete_or_disable_clears_only_the_selected_identity(self) -> None:
        entry_a = {"name": "JP 01", "uri": GATEWAY_A_URI}
        entry_b = {"name": "JP 01", "uri": GATEWAY_B_URI}
        entry_b_copy = {"name": "JP 01 copy", "uri": GATEWAY_B_COPY_URI}
        identity_b = _entry_identity(entry_b)
        self.assertNotEqual(_entry_identity(entry_a), identity_b)
        self.assertEqual(_entry_identity(entry_b_copy), identity_b)
        bound = {"selected_proxy": "JP 01", "selected_node": {"identity": identity_b, "label": "JP 01"}}

        def exit_item(uri: str) -> dict:
            return {"tier": "exit", "kind": "node", "key": uri}

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            # The same-named twin A is not the Gateway: disabling or deleting
            # it keeps the selection (a name rule cleared it).
            store = self._gateway_store(root / "twin", [dict(entry_a), dict(entry_b)])
            store.set_exit_selection("JP 01", identity=identity_b)
            store.set_config_item_enabled(exit_item(GATEWAY_A_URI), False)
            exit_payload = store.read_local()["tiers"]["exit"]
            self.assertEqual(exit_payload["nodes"][0], {**entry_a, "enabled": False})
            self.assertEqual(_selection_fields(exit_payload), bound)
            store.delete_exit_node(GATEWAY_A_URI)
            exit_payload = store.read_local()["tiers"]["exit"]
            self.assertEqual(exit_payload["nodes"], [entry_b])
            self.assertEqual(_selection_fields(exit_payload), bound)

            # Deleting B itself turns the Gateway off: all three keys go.
            store.delete_exit_node(GATEWAY_B_URI)
            exit_payload = store.read_local()["tiers"]["exit"]
            self.assertEqual(exit_payload["nodes"], [])
            self.assertEqual(_selection_fields(exit_payload), {})

            # So does disabling B while nothing else carries its identity.
            store = self._gateway_store(root / "disable", [dict(entry_a), dict(entry_b)])
            store.set_exit_selection("JP 01", identity=identity_b)
            store.set_config_item_enabled(exit_item(GATEWAY_B_URI), False)
            self.assertEqual(_selection_fields(store.read_local()["tiers"]["exit"]), {})

            # An enabled manual duplicate of B's identity keeps the selection.
            store = self._gateway_store(root / "copy", [dict(entry_a), dict(entry_b), dict(entry_b_copy)])
            store.set_exit_selection("JP 01", identity=identity_b)
            store.delete_exit_node(GATEWAY_B_URI)
            exit_payload = store.read_local()["tiers"]["exit"]
            self.assertEqual(exit_payload["nodes"], [entry_a, entry_b_copy])
            self.assertEqual(_selection_fields(exit_payload), bound)

            # A disabled duplicate carries nothing: deleting B clears.
            store = self._gateway_store(
                root / "disabled-copy",
                [dict(entry_a), dict(entry_b), {**entry_b_copy, "enabled": False}],
            )
            store.set_exit_selection("JP 01", identity=identity_b)
            store.delete_exit_node(GATEWAY_B_URI)
            self.assertEqual(_selection_fields(store.read_local()["tiers"]["exit"]), {})

    def test_editing_the_selected_entry_rebinds_and_renaming_relabels(self) -> None:
        entry_a = {"name": "JP 01", "uri": GATEWAY_A_URI}
        entry_b = {"name": "JP 01", "uri": GATEWAY_B_URI}
        identity_b = _entry_identity(entry_b)
        identity_b2 = _entry_identity({"name": "JP 01", "uri": GATEWAY_B2_URI})
        self.assertNotEqual(identity_b2, identity_b)

        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._gateway_store(Path(temp_dir) / "edit", [dict(entry_a), dict(entry_b)])
            store.set_exit_selection("JP 01", identity=identity_b)

            # Rotated credentials on B's own entry: re-bound in the same write.
            with patch.object(store, "write_local", wraps=store.write_local) as write_local:
                store.update_config_item({"tier": "exit", "kind": "node", "key": GATEWAY_B_URI}, {"uri": GATEWAY_B2_URI})
            self.assertEqual(write_local.call_count, 1)
            written = write_local.call_args.args[0]["tiers"]["exit"]
            self.assertEqual(written["nodes"][1], {"name": "JP 01", "uri": GATEWAY_B2_URI})
            self.assertEqual(
                _selection_fields(written),
                {"selected_proxy": "JP 01", "selected_node": {"identity": identity_b2, "label": "JP 01"}},
            )
            self.assertEqual(_selection_fields(store.read_local()["tiers"]["exit"]), _selection_fields(written))

            # A rename of that entry relabels both fields; the identity stays.
            store.update_config_item({"tier": "exit", "kind": "node", "key": GATEWAY_B2_URI}, {"name": "JP Tokyo"})
            exit_payload = store.read_local()["tiers"]["exit"]
            self.assertEqual(exit_payload["nodes"][1], {"name": "JP Tokyo", "uri": GATEWAY_B2_URI})
            relabelled = {
                "selected_proxy": "JP Tokyo",
                "selected_node": {"identity": identity_b2, "label": "JP Tokyo"},
            }
            self.assertEqual(_selection_fields(exit_payload), relabelled)
            self.assertEqual(read_selection(exit_payload), GatewaySelection("bound", "JP Tokyo", identity=identity_b2))

            # Editing another entry (the same-named twin A) leaves it alone.
            store.update_config_item({"tier": "exit", "kind": "node", "key": GATEWAY_A_URI}, {"uri": GATEWAY_A2_URI})
            exit_payload = store.read_local()["tiers"]["exit"]
            self.assertEqual(exit_payload["nodes"][0], {"name": "JP 01", "uri": GATEWAY_A2_URI})
            self.assertEqual(_selection_fields(exit_payload), relabelled)

    def test_editing_a_duplicate_carrier_never_moves_the_selection(self) -> None:
        """Review RES-1: only the saved node's sole carrier moves it.  Editing
        a duplicate entry of the picked node, or the picked node's line out of
        a multi-node entry, would otherwise re-bind to a server never picked."""

        entry_b = {"name": "JP 01", "uri": GATEWAY_B_URI}
        identity_b = _entry_identity(entry_b)
        with tempfile.TemporaryDirectory() as temp_dir:
            # A duplicate entry (same server and credentials, another name).
            copy_entry = {"name": "JP 01 copy", "uri": GATEWAY_B_COPY_URI}
            self.assertEqual(_entry_identity(copy_entry), identity_b)
            store = self._gateway_store(Path(temp_dir) / "copy", [dict(entry_b), dict(copy_entry)])
            store.set_exit_selection("JP 01", identity=identity_b)
            store.update_config_item({"tier": "exit", "kind": "node", "key": GATEWAY_B_COPY_URI}, {"uri": GATEWAY_A2_URI})
            store.update_config_item({"tier": "exit", "kind": "node", "key": GATEWAY_A2_URI}, {"name": "Renamed"})
            self.assertEqual(
                read_selection(store.read_local()["tiers"]["exit"]),
                GatewaySelection("bound", "JP 01", identity=identity_b),
            )

            # A multi-node entry edited down to its sibling.
            blob = f"{GATEWAY_B_URI}\n{GATEWAY_A2_URI}"
            store = self._gateway_store(Path(temp_dir) / "blob", [blob])
            store.set_exit_selection("JP 01", identity=identity_b)
            store.update_config_item({"tier": "exit", "kind": "node", "key": blob}, {"uri": GATEWAY_A2_URI})
            self.assertEqual(
                read_selection(store.read_local()["tiers"]["exit"]),
                GatewaySelection("bound", "JP 01", identity=identity_b),
            )

    def test_malformed_record_never_blocks_unrelated_saves(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))
            payload = store.read_local()
            payload["tiers"]["exit"]["selected_node"] = "garbage"
            payload["killswitch"]["allow_lan"] = True     # the unrelated change
            store.write_local(payload)                    # no ValueError
            saved = store.read_local()
            self.assertEqual(saved["tiers"]["exit"]["selected_node"], "garbage")
            self.assertTrue(saved["killswitch"]["allow_lan"])
            self.assertEqual(read_selection(saved["tiers"]["exit"]), GatewaySelection("none"))

            # Store operations that rewrite the file keep working around it.
            store.add_tier2_subscription("https://example.org/sub")
            store.set_exit_selected_proxy("")
            self.assertIn("https://example.org/sub", store.read_local()["tiers"]["tier2"]["subscription_urls"])
            self.assertEqual(_selection_fields(store.read_local()["tiers"]["exit"]), {})

            # A record with the label but an unverifiable identity: invalid.
            payload = store.read_local()
            payload["tiers"]["exit"]["selected_proxy"] = "Exit-SS"
            payload["tiers"]["exit"]["selected_node"] = {"identity": "garbage", "label": "Exit-SS"}
            payload["egress_proxy"]["port"] = 8001
            store.write_local(payload)
            saved = store.read_local()
            self.assertEqual(saved["egress_proxy"]["port"], 8001)
            selection = read_selection(saved["tiers"]["exit"])
            self.assertEqual(
                (selection.state, selection.label, selection.reason, selection.key),
                ("invalid", "Exit-SS", "malformed", "invalid:Exit-SS"),
            )
            store.add_exit_subscription("https://exit.example.org/sub")
            self.assertEqual(
                _selection_fields(store.read_local()["tiers"]["exit"]),
                {"selected_proxy": "Exit-SS", "selected_node": {"identity": "garbage", "label": "Exit-SS"}},
            )

    def test_write_sources_validates_lists_and_urls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(Path(temp_dir))

            store.write_sources({"github_repos": ["https://example.com/a.txt"], "telegram_channels": ["@public"], "subscription_urls": [], "local_files": []})
            self.assertEqual(store.read_sources()["github_repos"], ["https://example.com/a.txt"])
            self.assertEqual(store.read_sources()["telegram_channels"], ["@public"])
            with self.assertRaises(ValueError):
                store.write_sources({"github_repos": "not-a-list"})


if __name__ == "__main__":
    unittest.main()
