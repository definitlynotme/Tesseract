import logging
import tempfile
import unittest
from pathlib import Path

from alerts import AlertEvent, AlertManager
from config_store import ConfigStore
from import_service import ImportService
from manager import ProxyTierManager
from models import AlertType


class NativeServicesTests(unittest.TestCase):
    def _build_manager_and_alerts(self, temp_path: Path):
        nodes_file = temp_path / "nodes.json"
        nodes_file.write_text('{"generated_at":"","metadata":{"tier":"tier3","source_count":0},"nodes":[]}', encoding="utf-8")
        cache_file = temp_path / "tier2_cache.json"
        cache_file.write_text('{"generated_at":"","metadata":{"tier":"tier2","source_urls":[]},"nodes":[]}', encoding="utf-8")
        sources_file = temp_path / "sources.yaml"
        sources_file.write_text("github_repos: []\nsubscription_urls: []\nlocal_files: []\n", encoding="utf-8")

        config = {
            "paths": {
                "sources_file": str(sources_file),
                "tier3_nodes_file": str(nodes_file),
                "tier2_cache_file": str(cache_file),
            },
            "tiers": {
                "tier1": {"nodes": [], "check_interval": 30},
                "tier2": {"subscription_urls": [], "cache_file": str(cache_file), "check_interval": 60},
                "tier3": {"check_interval": 120, "auto_collect_interval": 3600, "max_pool_size": 100},
            },
            "testing": {"tcp_timeout": 1, "concurrency": 5, "slow_threshold_ms": 1000, "validation_mode": "tcp_only"},
            "subscription_server": {"host": "127.0.0.1", "port": 25500, "tier3_limit": 20},
            "client": {"mode": "clash", "clash_config_path": ""},
        }

        alerts = AlertManager(
            {"desktop": {"enabled": False}, "telegram": {"enabled": False}, "sound": {"enabled": False}},
            logging.getLogger("native-alerts"),
        )
        manager = ProxyTierManager(config=config, logger=logging.getLogger("native-manager"), alerts=alerts)
        return manager, alerts

    def test_manager_exposes_native_runtime_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager, alerts = self._build_manager_and_alerts(Path(temp_dir))
            alerts.send_event(
                AlertEvent(
                    event_key="test.recovery",
                    event_group="route_health",
                    alert_type=AlertType.RECOVERY,
                    title="Recovered",
                    message="Test recovery message",
                    source="test",
                )
            )

            snapshot = manager.get_runtime_snapshot()

            self.assertIn("system", snapshot)
            self.assertIn("tiers", snapshot)
            self.assertEqual(snapshot["client"]["mode"], "clash")
            self.assertIn("clash", snapshot["subscriptions"])
            self.assertIn("v2ray", snapshot["subscriptions"])
            self.assertEqual(alerts.recent()[-1]["title"], "Recovered")
            self.assertIn("total", manager.get_tier3_nodes_snapshot())
            self.assertIn("tiers", manager.get_node_status_snapshot())

    def test_desktop_notification_payload_is_truncated_for_windows_limits(self) -> None:
        title, message = AlertManager._desktop_payload("t" * 100, "m" * 400)

        self.assertLessEqual(len(title), 63)
        self.assertLessEqual(len(message), 240)
        self.assertTrue(title.endswith("..."))
        self.assertTrue(message.endswith("..."))

    def test_config_store_manages_local_config_and_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            manager, _ = self._build_manager_and_alerts(temp_path)
            local = temp_path / "config.local.yaml"
            sources = temp_path / "sources.yaml"
            local.write_text("tiers:\n  tier1:\n    nodes: []\n  tier2:\n    subscription_urls: []\n", encoding="utf-8")
            sources.write_text("github_repos: []\nsubscription_urls: []\nlocal_files: []\n", encoding="utf-8")
            store = ConfigStore(local_config_path=local, sources_path=sources, backup_dir=temp_path / "backups")

            summary = store.summary_with_status(manager.get_node_status_snapshot())
            self.assertEqual(summary["tier1"]["total"], 0)
            self.assertIn("runtime", summary["tier1"])

            store.add_tier1_node({"name": "Osaka", "address": "203.0.113.10", "port": 443})
            store.update_tier1_node("Osaka", {"port": 8443})
            self.assertEqual(store.read_local()["tiers"]["tier1"]["nodes"][0]["port"], 8443)

            store.add_tier2_subscription("https://example.com/sub")
            self.assertIn("https://example.com/sub", store.read_local()["tiers"]["tier2"]["subscription_urls"])
            store.delete_tier2_subscription("https://example.com/sub")

            store.write_sources({"github_repos": ["https://example.com/a"], "subscription_urls": [], "local_files": []})
            self.assertEqual(store.read_sources()["github_repos"], ["https://example.com/a"])

            store.delete_tier1_node("Osaka")
            self.assertEqual(store.read_local()["tiers"]["tier1"]["nodes"], [])

    def test_import_service_previews_and_saves_by_tier(self) -> None:
        uri = (
            "vless://11111111-1111-1111-1111-111111111111@example.com:443"
            "?encryption=none&security=reality&type=tcp&pbk=test&sid=abcd#Imported"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            self._build_manager_and_alerts(temp_path)
            local = temp_path / "config.local.yaml"
            sources = temp_path / "sources.yaml"
            local.write_text("tiers:\n  tier1:\n    nodes: []\n  tier2:\n    subscription_urls: []\n", encoding="utf-8")
            sources.write_text("github_repos: []\nsubscription_urls: []\nlocal_files: []\n", encoding="utf-8")
            store = ConfigStore(local_config_path=local, sources_path=sources, backup_dir=temp_path / "backups")
            importer = ImportService(store)

            preview = importer.preview({"text": uri, "tier": "tier1"})
            self.assertEqual(preview["node_count"], 1)

            tier1 = importer.import_tier1({"text": uri})
            self.assertEqual(tier1["added"], 1)
            self.assertEqual(store.read_local()["tiers"]["tier1"]["nodes"][0]["name"], "Imported")

            importer.import_tier2_subscription({"url": "https://example.com/sub"})
            self.assertEqual(store.read_local()["tiers"]["tier2"]["subscription_urls"], ["https://example.com/sub"])

            importer.import_tier3_source({"url": "https://example.com/free.txt"})
            self.assertEqual(store.read_sources()["github_repos"], ["https://example.com/free.txt"])


if __name__ == "__main__":
    unittest.main()
