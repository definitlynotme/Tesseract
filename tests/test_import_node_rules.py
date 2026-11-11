import unittest

from clash_formatter import node_to_clash_proxy
from import_node_rules import (
    existing_tier1_duplicate_keys,
    import_warnings_for_tier,
    multiprotocol_import_warnings,
    node_duplicate_key,
    node_to_tier1_config,
    summarize_import_node,
    summarize_tier1_import_entry,
    tier1_import_entries_from_nodes,
    unique_import_name,
)
from models import Node
from parser import parse_node_uri
from tier1_protocols import expand_tier1_entry


class ImportNodeRulesTests(unittest.TestCase):
    def test_summarize_import_node_uses_dedupe_key_when_uri_is_empty(self) -> None:
        first = Node(name="one", protocol="vless", uri="", address="one.example", port=443, uuid="one")
        second = Node(name="two", protocol="vless", uri="", address="two.example", port=443, uuid="two")

        first_summary = summarize_import_node(first)
        second_summary = summarize_import_node(second)

        self.assertNotEqual(first_summary["fingerprint"], "e3b0c44298")
        self.assertNotEqual(first_summary["fingerprint"], second_summary["fingerprint"])

    def test_import_warnings_do_not_repeat_runtime_filtering(self) -> None:
        warnings = import_warnings_for_tier(
            "tier3",
            [Node(name="old", protocol="ssr", uri="ssr://node", address="example.com", port=443)],
        )

        self.assertEqual(warnings, [])

    def test_multiprotocol_warnings_report_unsupported_and_structured_only_imports(self) -> None:
        warnings = multiprotocol_import_warnings(
            {"protocols": {"vless": {}, "unknown": {}}},
            [Node(name="structured", protocol="vless", uri="", address="example.com", port=443)],
        )

        by_code = {warning["code"]: warning for warning in warnings}
        self.assertEqual(by_code["unsupported_sections_ignored"]["params"], {"sections": ["unknown"]})
        self.assertEqual(by_code["unsupported_sections_ignored"]["message"], "unsupported sections ignored: unknown")
        self.assertTrue(by_code["unsupported_sections_ignored"]["display"])
        self.assertEqual(
            by_code["structured_export_incomplete"]["message"],
            "this import uses structured protocol fields; v2ray plain subscription export may not include every variant",
        )
        self.assertFalse(by_code["structured_export_incomplete"]["display"])

    def test_tier1_payload_and_summary_preserve_import_contract(self) -> None:
        node = Node(
            name="Tokyo",
            protocol="trojan",
            uri="trojan://secret@example.com:443#tokyo",
            address="example.com",
            port=443,
        )

        self.assertEqual(
            node_to_tier1_config(node),
            {
                "name": "Tokyo",
                "address": "example.com",
                "port": 443,
                "imported_link": "trojan://secret@example.com:443#tokyo",
                "protocol": "trojan",
            },
        )
        self.assertEqual(
            summarize_tier1_import_entry(
                {
                    "name": "Tokyo",
                    "address": "example.com",
                    "port": 443,
                    "protocols": {"trojan": {}, "vless": {}},
                    "fallback_order": ["trojan", "vless"],
                }
            ),
            {
                "name": "Tokyo",
                "address": "example.com",
                "port": 443,
                "protocols": ["trojan", "vless"],
                "fallback_order": ["trojan", "vless"],
            },
        )

    def test_supported_uri_roundtrip_preserves_credentials_and_tls(self) -> None:
        uris = {
            "naive": "naive://user:password@naive.example:443?security=tls&sni=naive.example#naive",
            "hy2": "hy2://hy-password@hy.example:443?sni=hy.example#hy2",
            "hysteria2-query-password": (
                "hysteria2://hy-query.example:443?password=query-password&sni=hy-query.example#hy-query"
            ),
            "anytls-password-userinfo": (
                "anytls://:any-password@any.example:443?sni=any.example#anytls"
            ),
        }

        for label, uri in uris.items():
            with self.subTest(label=label):
                parsed = parse_node_uri(uri, source="paste", tier="tier1")
                self.assertIsNotNone(parsed)
                assert parsed is not None
                entries = tier1_import_entries_from_nodes([parsed])
                self.assertEqual(len(entries), 1)
                expanded = expand_tier1_entry(entries[0])
                self.assertEqual(len(expanded), 1)
                restored = expanded[0]
                self.assertEqual(restored.protocol, parsed.protocol)
                self.assertEqual(restored.address, parsed.address)
                self.assertEqual(restored.port, parsed.port)
                self.assertEqual(restored.password, parsed.password)
                self.assertEqual(restored.tls, parsed.tls)
                self.assertEqual(restored.extra.get("username"), parsed.extra.get("username"))
                self.assertIsNotNone(node_to_clash_proxy(restored))

    def test_unique_import_name_adds_numeric_suffix(self) -> None:
        self.assertEqual(unique_import_name("Tokyo", {"Tokyo", "Tokyo-2"}), "Tokyo-3")
        self.assertEqual(unique_import_name("", set()), "imported-node")

    def test_tier1_clipboard_nodes_on_same_host_merge_into_physical_entry(self) -> None:
        uris = [
            "https://main:naive-password@example.com:2443#sing-box-naive-tcp",
            (
                "vless://11111111-1111-1111-1111-111111111111@example.com:8443"
                "?type=tcp&security=reality&encryption=none&flow=xtls-rprx-vision"
                "&sni=www.cloudflare.com&fp=chrome&pbk=test-public-key&sid=abcd#sing-box-vless-reality-vision"
            ),
            "hysteria2://hy2-password@example.com:443/?obfs=salamander&sni=hy2.local#sing-box-hysteria2-udp443",
        ]
        nodes = [parse_node_uri(uri, tier="tier1") for uri in uris]
        assert all(node is not None for node in nodes)

        entries = tier1_import_entries_from_nodes([node for node in nodes if node is not None])

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["name"], "sing-box")
        self.assertEqual(entries[0]["address"], "example.com")
        self.assertEqual(entries[0]["port"], 443)
        self.assertEqual(set(entries[0]["protocols"]), {"naive", "vless_reality", "hysteria2"})
        self.assertEqual(entries[0]["fallback_order"], ["naive", "vless_reality", "hysteria2"])

    def test_duplicate_key_ignores_uri_fragment_and_query_order(self) -> None:
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

        self.assertEqual(node_duplicate_key(node_a), node_duplicate_key(node_b))
        self.assertIn(node_duplicate_key(node_a), existing_tier1_duplicate_keys([{"vless_link": uri_a}]))


if __name__ == "__main__":
    unittest.main()
