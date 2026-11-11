import base64
import json
import logging
import tempfile
import unittest
from pathlib import Path

import yaml

from alerts import AlertManager
from clash_formatter import build_full_clash_config
from config_store import ConfigStore
from import_service import ImportService
from manager import ProxyTierManager
from models import NodeStatus
from tier1_protocols import (
    expand_tier1_entry,
    ordered_tier1_protocol_keys,
    parse_tier1_multiprotocol_block,
)


MULTI_PROTOCOL_BLOCK = """
VLESS Reality:
vless://11111111-1111-1111-1111-111111111111@example.com:8443?type=tcp&security=reality&encryption=none&flow=xtls-rprx-vision&sni=www.cloudflare.com&fp=chrome&pbk=test-public-key&sid=abcd#vless

Hysteria2:
hysteria2://hy2-password@example.com:443/?obfs=salamander&obfs-password=obfs-pass&sni=hy2.local&insecure=1#hy2

AnyTLS:
server=example.com
port=3443
password=anytls-password
server_name=anytls.local
insecure=true

Naive:
https://main:naive-password@example.com:2443#naive

Argo local:
127.0.0.1:2080
path=/xhttp
uuid=22222222-2222-2222-2222-222222222222

Fallback:
hysteria2 -> vless_reality -> anytls -> naive -> argo_xhttp
""".strip()


class Tier1ProtocolTests(unittest.TestCase):
    def _base_files(self, temp_path: Path) -> tuple[Path, Path, Path]:
        nodes_file = temp_path / "nodes.json"
        nodes_file.write_text('{"generated_at":"","metadata":{"tier":"tier3","source_count":0},"nodes":[]}', encoding="utf-8")
        cache_file = temp_path / "tier2_cache.json"
        cache_file.write_text('{"generated_at":"","metadata":{"tier":"tier2","source_urls":[]},"nodes":[]}', encoding="utf-8")
        sources_file = temp_path / "sources.yaml"
        sources_file.write_text("github_repos: []\nsubscription_urls: []\nlocal_files: []\n", encoding="utf-8")
        return nodes_file, cache_file, sources_file

    def test_multiprotocol_block_expands_to_variants(self) -> None:
        entry = parse_tier1_multiprotocol_block(MULTI_PROTOCOL_BLOCK, name="Edge VPS")
        nodes = expand_tier1_entry(entry)

        self.assertEqual(entry["name"], "Edge VPS")
        self.assertEqual(entry["address"], "example.com")
        self.assertEqual(len(nodes), 5)
        self.assertEqual(
            [node.extra["protocol_key"] for node in nodes],
            ["hysteria2", "vless_reality", "anytls", "naive", "argo_xhttp"],
        )
        self.assertEqual(nodes[-1].network, "xhttp")
        self.assertEqual(nodes[-1].address, "127.0.0.1")
        self.assertEqual(entry["fallback_order"][0], "hysteria2")

    def test_configured_protocol_order_resolves_legacy_fallback_aliases(self) -> None:
        protocols = {
            "hysteria2": {"password": "hy-pass"},
            "vless_reality": {"uri": "vless://edge"},
            "naive": {"uri": "https://edge"},
        }

        self.assertEqual(
            ordered_tier1_protocol_keys(
                protocols,
                ["reality", "hy2", "naiveproxy"],
            ),
            ["vless_reality", "hysteria2", "naive"],
        )

    def test_plain_xhttp_section_is_not_classified_as_argo(self) -> None:
        entry = parse_tier1_multiprotocol_block(
            """
            XHTTP:
            server=example.com
            port=443
            uuid=22222222-2222-2222-2222-222222222222
            path=/xhttp
            """.strip(),
            name="XHTTP VPS",
        )
        nodes = expand_tier1_entry(entry)

        self.assertIn("xhttp", entry["protocols"])
        self.assertNotIn("argo_xhttp", entry["protocols"])
        self.assertEqual(nodes[0].extra["protocol_key"], "xhttp")

    def test_multiprotocol_block_rejects_missing_address(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing a server/address"):
            parse_tier1_multiprotocol_block(
                """
                AnyTLS:
                password=anytls-password
                """.strip(),
                name="Broken VPS",
            )

    def test_unreachable_multiprotocol_entry_remains_available_to_self_check(self) -> None:
        entry = parse_tier1_multiprotocol_block(
            """
            VLESS:
            server=1
            port=1
            uuid=11111111-1111-1111-1111-111111111111

            Hysteria2:
            server=1
            port=1
            password=secret
            """.strip(),
            name="1",
        )
        nodes = expand_tier1_entry(entry)

        self.assertEqual(entry["name"], "1")
        self.assertEqual(entry["address"], "1")
        self.assertEqual(entry["port"], 1)
        self.assertEqual(len(nodes), 2)
        self.assertTrue(all(node.address == "1" and node.port == 1 for node in nodes))

    def test_string_false_insecure_does_not_enable_skip_cert_verify(self) -> None:
        entry = parse_tier1_multiprotocol_block(
            """
            AnyTLS:
            server=example.com
            port=3443
            password=anytls-password
            insecure=false
            """.strip(),
            name="Strict TLS VPS",
        )
        nodes = expand_tier1_entry(entry)

        self.assertEqual(nodes[0].extra.get("skip-cert-verify"), False)

    def test_string_false_protocol_enabled_skips_variant(self) -> None:
        nodes = expand_tier1_entry(
            {
                "name": "Disabled Variant VPS",
                "protocols": {
                    "anytls": {
                        "enabled": "false",
                        "server": "example.com",
                        "port": 3443,
                        "password": "anytls-password",
                    }
                },
            }
        )

        self.assertEqual(nodes, [])

    def test_imported_link_uses_uri_scheme_for_protocol_label(self) -> None:
        payload = {
            "v": "2",
            "ps": "vmess-node",
            "add": "example.com",
            "port": "443",
            "id": "22222222-2222-2222-2222-222222222222",
            "aid": "0",
            "net": "ws",
            "path": "/ws",
            "tls": "tls",
        }
        uri = "vmess://" + base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")

        nodes = expand_tier1_entry({"name": "VMess VPS", "address": "example.com", "port": 443, "imported_link": uri})

        self.assertEqual(nodes[0].extra["protocol_key"], "vmess")
        self.assertEqual(nodes[0].extra["protocol_label"], "Vmess")

    def test_mihomo_config_groups_protocols_under_physical_node(self) -> None:
        nodes = expand_tier1_entry(parse_tier1_multiprotocol_block(MULTI_PROTOCOL_BLOCK, name="Edge VPS"))

        parsed = yaml.safe_load(build_full_clash_config({"tier1": nodes, "tier2": [], "tier3": []}))
        groups = {group["name"]: group for group in parsed["proxy-groups"]}

        self.assertIn("TIER1-Edge-VPS-AUTO", groups)
        self.assertIn("TIER1-Edge-VPS-SELECT", groups)
        self.assertEqual(groups["TIER1-AUTO"]["proxies"], ["TIER1-Edge-VPS-SELECT"])
        self.assertEqual(groups["TIER1-Edge-VPS-AUTO"]["proxies"][0], "Edge VPS :: Hysteria2")
        self.assertEqual(groups["TIER1-Edge-VPS-AUTO"]["proxies"][1], "Edge VPS :: VLESS Reality")
        self.assertIn("Edge VPS :: AnyTLS", groups["TIER1-Edge-VPS-AUTO"]["proxies"])
        proxies = {proxy["name"]: proxy for proxy in parsed["proxies"]}
        self.assertEqual(proxies["Edge VPS :: Hysteria2"]["type"], "hysteria2")
        self.assertEqual(proxies["Edge VPS :: AnyTLS"]["type"], "anytls")

    def test_manager_counts_physical_tier1_node_alive_if_any_protocol_alive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            nodes_file, cache_file, sources_file = self._base_files(temp_path)
            entry = parse_tier1_multiprotocol_block(MULTI_PROTOCOL_BLOCK, name="Edge VPS")
            config = {
                "paths": {
                    "sources_file": str(sources_file),
                    "tier3_nodes_file": str(nodes_file),
                    "tier2_cache_file": str(cache_file),
                },
                "tiers": {
                    "tier1": {"nodes": [entry], "check_interval": 30},
                    "tier2": {"subscription_urls": [], "cache_file": str(cache_file), "check_interval": 60},
                    "tier3": {"check_interval": 120, "auto_collect_interval": 3600, "max_pool_size": 100},
                },
                "testing": {"tcp_timeout": 1, "concurrency": 5, "slow_threshold_ms": 1000, "validation_mode": "tcp_only"},
                "subscription_server": {"host": "127.0.0.1", "port": 25500, "tier3_limit": 20},
            }
            manager = ProxyTierManager(
                config=config,
                logger=logging.getLogger("test-tier1-protocols"),
                alerts=AlertManager({"desktop": {"enabled": False}}, logging.getLogger("test-tier1-alerts")),
            )
            for index, node in enumerate(manager.runtime_nodes["tier1"]):
                node.status = NodeStatus.DEAD
                if index == 1:
                    node.status = NodeStatus.ALIVE

            snapshot = manager.get_runtime_snapshot()

            self.assertEqual(snapshot["tiers"]["tier1"]["alive"], 1)
            self.assertEqual(snapshot["tiers"]["tier1"]["total"], 1)
            self.assertEqual(snapshot["tiers"]["tier1"]["variant_alive"], 1)
            self.assertEqual(snapshot["tiers"]["tier1"]["variant_total"], 5)
            self.assertEqual(snapshot["tiers"]["tier1"]["protocol_groups"][0]["healthy"], 1)

            node_snapshot = manager.get_node_status_snapshot("tier1")
            tier1_nodes = node_snapshot["tiers"]["tier1"]["nodes"]
            self.assertEqual(node_snapshot["tiers"]["tier1"]["total"], 1)
            self.assertEqual(node_snapshot["tiers"]["tier1"]["variant_total"], 5)
            self.assertEqual(len(tier1_nodes), 1)
            self.assertEqual(tier1_nodes[0]["name"], "Edge VPS")
            self.assertTrue(tier1_nodes[0]["aggregate"])
            self.assertEqual(tier1_nodes[0]["status"], "alive")
            self.assertEqual(len(tier1_nodes[0]["protocols"]), 5)

    def test_import_service_imports_multiprotocol_vps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            nodes_file, cache_file, sources_file = self._base_files(temp_path)
            local = temp_path / "config.local.yaml"
            local.write_text("tiers:\n  tier1:\n    nodes: []\n  tier2:\n    subscription_urls: []\n", encoding="utf-8")
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
            }
            alerts = AlertManager({"desktop": {"enabled": False}}, logging.getLogger("test-import-alerts"))
            ProxyTierManager(config=config, logger=logging.getLogger("test-import"), alerts=alerts)
            store = ConfigStore(local_config_path=local, sources_path=sources_file, backup_dir=temp_path / "backups")
            importer = ImportService(store)

            preview = importer.preview_tier1_multiprotocol({"name": "Edge VPS", "text": MULTI_PROTOCOL_BLOCK})
            self.assertEqual(preview["variant_count"], 5)

            saved = importer.import_tier1_multiprotocol({"name": "Edge VPS", "text": MULTI_PROTOCOL_BLOCK})
            self.assertEqual(saved["added"], 1)
            stored_node = store.read_local()["tiers"]["tier1"]["nodes"][0]
            self.assertEqual(stored_node["name"], "Edge VPS")
            self.assertIn("anytls", stored_node["protocols"])
            self.assertEqual(stored_node["fallback_order"][0], "hysteria2")

    def test_import_payload_overrides_fallback_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            nodes_file, cache_file, sources_file = self._base_files(temp_path)
            local = temp_path / "config.local.yaml"
            local.write_text("tiers:\n  tier1:\n    nodes: []\n  tier2:\n    subscription_urls: []\n", encoding="utf-8")
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
            }
            alerts = AlertManager({"desktop": {"enabled": False}}, logging.getLogger("test-import-order-alerts"))
            ProxyTierManager(config=config, logger=logging.getLogger("test-import-order"), alerts=alerts)
            store = ConfigStore(local_config_path=local, sources_path=sources_file, backup_dir=temp_path / "backups")
            importer = ImportService(store)

            saved = importer.import_tier1_multiprotocol(
                {"name": "Edge VPS", "text": MULTI_PROTOCOL_BLOCK, "fallback_order": "anytls -> naive"}
            )

            self.assertEqual(saved["added"], 1)
            stored_node = store.read_local()["tiers"]["tier1"]["nodes"][0]
            self.assertEqual(stored_node["fallback_order"], ["anytls", "naive"])


class Tier1WireGuardTests(unittest.TestCase):
    def test_structured_wireguard_block_produces_node(self) -> None:
        entry = {
            "name": "WG VPS",
            "address": "wg.example.com",
            "port": 51820,
            "protocols": {
                "wireguard": {
                    "private_key": "priv",
                    "peer_public_key": "pub",
                    "allowed_ips": ["0.0.0.0/0", "::/0"],
                    "address": "10.0.0.2/32,fd00::2/128",
                    "mtu": 1408,
                    "reserved": [1, 2, 3],
                    "jc": 4,
                }
            },
        }
        nodes = expand_tier1_entry(entry)

        self.assertEqual(len(nodes), 1)
        node = nodes[0]
        self.assertEqual(node.protocol, "wireguard")
        self.assertEqual(node.extra["protocol_key"], "wireguard")
        self.assertEqual(node.extra["public-key"], "pub")
        self.assertEqual(node.extra["allowed-ips"], ["0.0.0.0/0", "::/0"])
        self.assertEqual(node.extra["ip"], "10.0.0.2")
        self.assertEqual(node.extra["ipv6"], "fd00::2")
        self.assertEqual(node.extra["reserved"], [1, 2, 3])
        self.assertEqual(node.extra["jc"], 4)

    def test_multiprotocol_block_parses_wireguard_section(self) -> None:
        block = """
        VLESS Reality:
        vless://11111111-1111-1111-1111-111111111111@example.com:8443?type=tcp&security=reality&encryption=none&flow=xtls-rprx-vision&sni=www.cloudflare.com&fp=chrome&pbk=test-public-key&sid=abcd#v

        WireGuard:
        server=wg.example.com
        port=51820
        private_key=priv
        peer_public_key=pub
        allowed_ips=0.0.0.0/0
        address=10.0.0.2/32
        mtu=1408
        """.strip()
        entry = parse_tier1_multiprotocol_block(block, name="Edge VPS")
        nodes = expand_tier1_entry(entry)

        self.assertIn("wireguard", entry["protocols"])
        keys = [node.extra["protocol_key"] for node in nodes]
        self.assertIn("wireguard", keys)


if __name__ == "__main__":
    unittest.main()
