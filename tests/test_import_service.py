import base64
import tempfile
import unittest
from pathlib import Path

from clash_formatter import node_to_clash_proxy
from config_store import ConfigStore
from import_service import ImportService
from models import Node
from parser import parse_node_uri
from tier1_protocols import expand_tier1_entry


VLESS_URI = (
    "vless://11111111-1111-1111-1111-111111111111@example.com:443"
    "?encryption=none&security=reality&type=tcp&pbk=test&sid=abcd&sni=www.microsoft.com#Osaka"
)
TROJAN_URI = "trojan://password@backup.example.com:443?security=tls&sni=backup.example.com#Backup"
TIER1_MULTIPROTOCOL_CLIPBOARD = "\n".join(
    [
        "naive://main:naive-password@example.com:2443#sing-box-naive-tcp",
        (
            "vless://11111111-1111-1111-1111-111111111111@example.com:8443"
            "?type=tcp&security=reality&encryption=none&flow=xtls-rprx-vision"
            "&sni=www.cloudflare.com&fp=chrome&pbk=test-public-key&sid=abcd#sing-box-vless-reality-vision"
        ),
        "hysteria2://hy2-password@example.com:443/?obfs=salamander&sni=hy2.local#sing-box-hysteria2-udp443",
    ]
)


class ImportServiceTests(unittest.TestCase):
    def _service(self, temp_path: Path) -> ImportService:
        local = temp_path / "config.local.yaml"
        sources = temp_path / "sources.yaml"
        local.write_text("tiers:\n  tier1:\n    nodes: []\n  tier2:\n    nodes: []\n    subscription_urls: []\n  tier3:\n    enabled: false\n    nodes: []\n    subscription_urls: []\n  exit:\n    nodes: []\n    subscription_urls: []\n", encoding="utf-8")
        sources.write_text("github_repos: []\ntelegram_channels: []\nsubscription_urls: []\nlocal_files: []\n", encoding="utf-8")
        store = ConfigStore(local_config_path=local, sources_path=sources, backup_dir=temp_path / "backups")
        return ImportService(store)

    def test_preview_parses_nodes_without_leaking_uri(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))

            preview = service.preview({"text": VLESS_URI, "tier": "tier1"})

            self.assertEqual(preview["node_count"], 1)
            self.assertEqual(preview["nodes"][0]["protocol"], "vless")
            self.assertTrue(preview["can_import"])
            self.assertEqual(preview["candidate_count"], 1)
            self.assertEqual(preview["duplicate_count"], 0)
            self.assertEqual(preview["rejected_count"], 0)
            self.assertEqual(preview["blocking_errors"], [])
            self.assertEqual(preview["physical_nodes"][0]["protocols"], ["vless_reality"])
            self.assertEqual(len(preview["plan_id"]), 64)
            self.assertNotIn("11111111-1111-1111-1111-111111111111", str(preview))

    def test_import_tier1_saves_parsed_node(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))

            result = service.import_tier1({"text": VLESS_URI})
            local = service.config_store.read_local()

            self.assertEqual(result["added"], 1)
            self.assertEqual(local["tiers"]["tier1"]["nodes"][0]["name"], "Osaka")
            self.assertIn("vless_link", local["tiers"]["tier1"]["nodes"][0])

    def test_import_tier1_single_naive_roundtrip_preserves_auth_and_tls(self) -> None:
        uri = (
            "naive://user:password@naive.example:443"
            "?security=tls&sni=naive.example#Naive"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))

            preview = service.preview({"text": uri, "tier": "tier1"})
            service.import_tier1(
                {"text": uri, "expected_plan_id": preview["plan_id"]}
            )
            stored = service.config_store.read_local()["tiers"]["tier1"]["nodes"][0]
            expanded = expand_tier1_entry(stored)

            self.assertEqual(preview["physical_nodes"][0]["protocols"], ["naive"])
            self.assertEqual(stored["protocol"], "naive")
            self.assertEqual(len(expanded), 1)
            self.assertEqual(expanded[0].extra.get("username"), "user")
            self.assertEqual(expanded[0].password, "password")
            self.assertTrue(expanded[0].tls)
            proxy = node_to_clash_proxy(expanded[0])
            self.assertIsNotNone(proxy)
            assert proxy is not None
            self.assertEqual(proxy.get("username"), "user")
            self.assertEqual(proxy.get("password"), "password")
            self.assertTrue(proxy.get("tls"))

    def test_import_tier1_merges_clipboard_multiprotocol_vps_into_one_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))

            preview = service.preview({"text": TIER1_MULTIPROTOCOL_CLIPBOARD, "tier": "tier1"})
            result = service.import_tier1({"text": TIER1_MULTIPROTOCOL_CLIPBOARD})
            local = service.config_store.read_local()

            self.assertEqual(preview["node_count"], 3)
            self.assertEqual(preview["physical_node_count"], 1)
            self.assertEqual(len(preview["physical_nodes"]), 1)
            self.assertEqual(preview["physical_nodes"][0]["address"], "example.com")
            self.assertEqual(
                preview["physical_nodes"][0]["fallback_order"],
                ["naive", "vless_reality", "hysteria2"],
            )
            self.assertEqual(result["added"], 1)
            self.assertEqual(result["variant_count"], 3)
            stored_node = local["tiers"]["tier1"]["nodes"][0]
            self.assertEqual(stored_node["name"], "sing-box")
            self.assertEqual(set(stored_node["protocols"]), {"naive", "vless_reality", "hysteria2"})
            self.assertEqual(len(local["tiers"]["tier1"]["nodes"]), 1)

    def test_multiprotocol_service_accepts_one_selected_protocol(self) -> None:
        text = "\n".join(
            [
                "fallback:",
                "  trojan",
                "",
                "trojan:",
                f"  uri={TROJAN_URI}",
            ]
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))

            result = service.import_tier1_multiprotocol(
                {
                    "name": "One protocol group",
                    "text": text,
                    "fallback_order": "trojan",
                }
            )
            stored = service.config_store.read_local()["tiers"]["tier1"]["nodes"][0]

            self.assertEqual(result["added"], 1)
            self.assertEqual(result["variant_count"], 1)
            self.assertEqual(list(stored["protocols"]), ["trojan"])
            self.assertEqual(stored["fallback_order"], ["trojan"])

    def test_tier1_mixed_base64_and_plaintext_fallback_follows_visual_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))
            hysteria_uri = "hysteria2://password@example.com:443#first"
            encoded_hysteria = base64.b64encode(hysteria_uri.encode("utf-8")).decode("ascii")
            text = f"{encoded_hysteria}\n{VLESS_URI}"

            preview = service.preview({"text": text, "tier": "tier1"})
            service.import_tier1({"text": text, "expected_plan_id": preview["plan_id"]})
            stored = service.config_store.read_local()["tiers"]["tier1"]["nodes"][0]

            self.assertEqual(
                preview["physical_nodes"][0]["fallback_order"],
                ["hysteria2", "vless_reality"],
            )
            self.assertEqual(stored["fallback_order"], ["hysteria2", "vless_reality"])

    def test_import_tier1_accepts_matching_preview_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))
            preview = service.preview({"text": VLESS_URI, "tier": "tier1", "source": "preview-source"})

            result = service.import_tier1(
                {
                    "text": VLESS_URI,
                    "source": "apply-source",
                    "expected_plan_id": preview["plan_id"],
                }
            )

            self.assertEqual(result["added"], 1)

    def test_import_tier1_rejects_stale_plan_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))
            config_path = service.config_store.local_config_path
            before = config_path.read_text(encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "run preview again"):
                service.import_tier1({"text": VLESS_URI, "expected_plan_id": "0" * 64})

            self.assertEqual(config_path.read_text(encoding="utf-8"), before)

    def test_import_tier1_blocks_partial_recognition_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))
            config_path = service.config_store.local_config_path
            text = f"{VLESS_URI}\nvmess://not-valid-base64"
            before = config_path.read_text(encoding="utf-8")

            preview = service.preview({"text": text, "tier": "tier1"})

            self.assertEqual(preview["node_count"], 1)
            self.assertEqual(preview["rejected_count"], 1)
            self.assertFalse(preview["can_import"])
            with self.assertRaises(ValueError):
                service.import_tier1({"text": text})
            self.assertEqual(config_path.read_text(encoding="utf-8"), before)

    def test_import_tier1_allows_duplicate_input_as_non_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))

            preview = service.preview({"text": f"{VLESS_URI}\n{VLESS_URI}", "tier": "tier1"})

            self.assertEqual(preview["node_count"], 1)
            self.assertEqual(preview["candidate_count"], 2)
            self.assertEqual(preview["duplicate_count"], 1)
            self.assertTrue(preview["can_import"])

    def test_import_tier1_repairs_invalid_tiers_section_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))
            service.config_store.local_config_path.write_text("tiers: invalid\n", encoding="utf-8")

            service.import_tier1({"text": VLESS_URI})

            self.assertEqual(
                len(service.config_store.read_local()["tiers"]["tier1"]["nodes"]),
                1,
            )

    def test_import_tier1_rejects_exact_duplicate_node(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))

            service.import_tier1({"text": VLESS_URI})

            with self.assertRaisesRegex(ValueError, "duplicate node already exists"):
                service.import_tier1({"text": VLESS_URI})
            self.assertEqual(len(service.config_store.read_local()["tiers"]["tier1"]["nodes"]), 1)

    def test_import_tier2_subscription_adds_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))

            service.import_tier2_subscription({"url": "https://example.com/sub"})

            self.assertEqual(service.config_store.read_local()["tiers"]["tier2"]["subscription_urls"], ["https://example.com/sub"])

    def test_import_tier2_node_saves_single_protocol_node(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))

            result = service.import_tier2_node({"text": VLESS_URI})
            local = service.config_store.read_local()

            self.assertEqual(result["added"], 1)
            self.assertEqual(local["tiers"]["tier2"]["nodes"][0]["name"], "Osaka")
            self.assertIn("uri", local["tiers"]["tier2"]["nodes"][0])

    def test_import_tier2_node_rejects_multiple_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))

            with self.assertRaisesRegex(ValueError, "exactly one"):
                service.import_tier2_node({"text": f"{VLESS_URI}\n{TROJAN_URI}"})

            self.assertEqual(service.config_store.read_local()["tiers"]["tier2"]["nodes"], [])

    def test_preview_tier2_marks_multiple_nodes_as_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))

            preview = service.preview({"text": f"{VLESS_URI}\n{TROJAN_URI}", "tier": "tier2"})

            self.assertFalse(preview["can_import"])
            self.assertIn("exactly_one_node_required", [issue["code"] for issue in preview["blocking_errors"]])

    def test_import_tier3_subscription_adds_3a_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))

            service.import_tier3_subscription({"url": "https://paid.example.com/sub"})

            self.assertEqual(service.config_store.read_local()["tiers"]["tier3"]["subscription_urls"], ["https://paid.example.com/sub"])

    def test_import_tier3_node_saves_single_3a_node(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))

            result = service.import_tier3_node({"text": VLESS_URI})
            local = service.config_store.read_local()

            self.assertEqual(result["added"], 1)
            self.assertEqual(local["tiers"]["tier3"]["nodes"][0]["name"], "Osaka")
            self.assertEqual(local["tiers"]["tier3"]["nodes"][0]["origin"], "tier3a")

    def test_import_tier3_node_rejects_multiple_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))

            with self.assertRaisesRegex(ValueError, "exactly one"):
                service.import_tier3_node({"text": f"{VLESS_URI}\n{TROJAN_URI}"})

            self.assertEqual(service.config_store.read_local()["tiers"]["tier3"]["nodes"], [])

    def test_import_exit_nodes_saves_to_exit_layer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))

            result = service.import_exit_nodes({"text": VLESS_URI})
            local = service.config_store.read_local()

            self.assertEqual(result["added"], 1)
            self.assertEqual(local["tiers"]["exit"]["nodes"][0]["name"], "Osaka")
            self.assertIn("uri", local["tiers"]["exit"]["nodes"][0])

    def test_import_exit_nodes_accepts_mixed_protocol_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))

            result = service.import_exit_nodes({"text": f"{VLESS_URI}\n{TROJAN_URI}"})

            self.assertEqual(result["added"], 2)
            self.assertEqual(len(service.config_store.read_local()["tiers"]["exit"]["nodes"]), 2)

    def test_preview_unsupported_content_finds_no_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))

            preview = service.preview({"text": "https://example.com/subscription", "tier": "tier1"})

            self.assertEqual(preview["node_count"], 0)
            self.assertFalse(preview["can_import"])

    def test_authenticated_https_is_blocked_as_ambiguous_in_automatic_import(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))
            text = "https://user:password@example.com:443#ambiguous"

            preview = service.preview({"text": text, "tier": "tier1"})

            self.assertFalse(preview["can_import"])
            self.assertGreaterEqual(preview["rejected_count"], 1)
            self.assertEqual(service.config_store.read_local()["tiers"]["tier1"]["nodes"], [])

    def test_import_exit_nodes_rejects_exact_duplicate_node(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))

            service.import_exit_nodes({"text": VLESS_URI})

            with self.assertRaisesRegex(ValueError, "duplicate node already exists"):
                service.import_exit_nodes({"text": VLESS_URI})
            self.assertEqual(len(service.config_store.read_local()["tiers"]["exit"]["nodes"]), 1)

    def test_import_exit_subscription_adds_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))

            service.import_exit_subscription({"url": "https://exit.example.com/sub"})

            self.assertEqual(service.config_store.read_local()["tiers"]["exit"]["subscription_urls"], ["https://exit.example.com/sub"])

    def test_import_tier3_source_adds_source_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))

            service.import_tier3_source({"url": "https://example.com/free.txt", "section": "github_repos"})

            self.assertEqual(service.config_store.read_sources()["github_repos"], ["https://example.com/free.txt"])

    def test_import_tier3_source_repairs_invalid_section_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))
            service.config_store.sources_path.write_text("github_repos: invalid\n", encoding="utf-8")

            service.import_tier3_source({"url": "https://example.com/free.txt", "section": "github_repos"})

            self.assertEqual(service.config_store.read_sources()["github_repos"], ["https://example.com/free.txt"])

    def test_import_tier3_telegram_source_adds_channel(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))

            service.import_tier3_source({"url": "@free_proxy_nodes", "section": "telegram_channels"})

            self.assertEqual(service.config_store.read_sources()["telegram_channels"], ["@free_proxy_nodes"])

    def test_upsert_tier3_source_replaces_type_and_preserves_unrelated_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))
            service.config_store.write_sources(
                {
                    "github_repos": [
                        {"url": "https://example.com/old.txt", "enabled": False, "custom": "kept"},
                        "https://example.com/unrelated.txt",
                    ],
                    "telegram_channels": ["@existing"],
                    "subscription_urls": [],
                    "local_files": ["C:/private/nodes.txt"],
                    "future_section": {"enabled": True},
                }
            )

            result = service.upsert_tier3_source(
                {
                    "section": "subscription_urls",
                    "value": "https://sub.example.com/new",
                    "previous_section": "github_repos",
                    "previous_value": "https://example.com/old.txt",
                }
            )

            sources = service.config_store.read_sources()
            self.assertEqual(result["action"], "updated")
            self.assertEqual(sources["github_repos"], ["https://example.com/unrelated.txt"])
            self.assertEqual(
                sources["subscription_urls"],
                [{"url": "https://sub.example.com/new", "enabled": False, "custom": "kept"}],
            )
            self.assertEqual(sources["telegram_channels"], ["@existing"])
            self.assertEqual(sources["local_files"], ["C:/private/nodes.txt"])
            self.assertEqual(sources["future_section"], {"enabled": True})
            local = service.config_store.read_local()
            self.assertEqual(local["tiers"]["tier3"]["subscription_urls"], [])
            self.assertNotIn("bootstrap_sources", local["tiers"]["tier3"])

    def test_upsert_tier3_source_rejects_duplicate_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))
            service.config_store.write_sources(
                {
                    "github_repos": ["https://example.com/old.txt"],
                    "telegram_channels": [],
                    "subscription_urls": ["https://sub.example.com/existing"],
                    "local_files": [],
                }
            )
            before = service.config_store.read_sources()

            with self.assertRaisesRegex(ValueError, "already exists"):
                service.upsert_tier3_source(
                    {
                        "section": "subscription_urls",
                        "value": "https://sub.example.com/existing",
                        "previous_section": "github_repos",
                        "previous_value": "https://example.com/old.txt",
                    }
                )

            self.assertEqual(service.config_store.read_sources(), before)

    def test_delete_tier3_source_preserves_other_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))
            service.config_store.write_sources(
                {
                    "github_repos": ["https://example.com/delete.txt", "https://example.com/keep.txt"],
                    "telegram_channels": ["@keep"],
                    "subscription_urls": [],
                    "local_files": ["C:/keep.txt"],
                    "unknown": ["keep"],
                }
            )

            result = service.delete_tier3_source(
                {"section": "github_repos", "value": "https://example.com/delete.txt"}
            )

            sources = service.config_store.read_sources()
            self.assertEqual(result["action"], "deleted")
            self.assertEqual(sources["github_repos"], ["https://example.com/keep.txt"])
            self.assertEqual(sources["telegram_channels"], ["@keep"])
            self.assertEqual(sources["local_files"], ["C:/keep.txt"])
            self.assertEqual(sources["unknown"], ["keep"])

    def test_tier3_source_validation_is_shared_by_add_upsert_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))
            invalid_payloads = [
                {"section": "local_files", "value": "C:/not-allowed.txt"},
                {"section": "subscription_urls", "value": "ftp://example.com/sub"},
                {"section": "telegram_channels", "value": "https://example.com/not-telegram"},
                {"section": "telegram_channels", "value": "@bad channel"},
            ]
            for payload in invalid_payloads:
                with self.subTest(payload=payload):
                    with self.assertRaises(ValueError):
                        service.upsert_tier3_source(payload)
                    with self.assertRaises(ValueError):
                        service.delete_tier3_source(payload)

    def test_preview_does_not_repeat_runtime_filtering(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))

            preview = service.preview({"text": VLESS_URI, "tier": "tier3"})

            self.assertEqual(preview["node_count"], 1)
            self.assertEqual(preview["warnings"], [])

    def test_empty_uri_nodes_get_distinct_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))
            first = Node(name="one", protocol="vless", uri="", address="one.example", port=443, uuid="one")
            second = Node(name="two", protocol="vless", uri="", address="two.example", port=443, uuid="two")

            first_summary = service._summarize_node(first)
            second_summary = service._summarize_node(second)

            self.assertNotEqual(first_summary["fingerprint"], "e3b0c44298")
            self.assertNotEqual(first_summary["fingerprint"], second_summary["fingerprint"])

    def test_duplicate_key_keeps_distinct_transports(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))
            reality = Node(
                name="reality",
                protocol="vless",
                uri="",
                address="example.com",
                port=443,
                uuid="same-uuid",
                security="reality",
                network="tcp",
            )
            xhttp = Node(
                name="xhttp",
                protocol="vless",
                uri="",
                address="example.com",
                port=443,
                uuid="same-uuid",
                security="tls",
                network="xhttp",
                path="/xhttp",
            )

            self.assertNotEqual(service._node_duplicate_key(reality), service._node_duplicate_key(xhttp))

    def test_duplicate_key_ignores_uri_fragment_and_query_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))
            uri_a = (
                "vless://11111111-1111-1111-1111-111111111111@example.com:443"
                "?encryption=none&security=reality&type=tcp&pbk=test&sid=abcd&sni=www.microsoft.com#One"
            )
            uri_b = (
                "vless://11111111-1111-1111-1111-111111111111@example.com:443"
                "?sni=www.microsoft.com&sid=abcd&pbk=test&type=tcp&security=reality&encryption=none#Two"
            )
            node_a = parse_node_uri(uri_a, tier="tier1")
            node_b = parse_node_uri(uri_b, tier="tier1")
            assert node_a is not None
            assert node_b is not None

            self.assertEqual(service._node_duplicate_key(node_a), service._node_duplicate_key(node_b))


if __name__ == "__main__":
    unittest.main()
