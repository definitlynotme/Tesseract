import base64
import json
import logging
import threading
import unittest
from unittest.mock import patch

from clash_formatter import (
    SUPPORTED_SHADOWSOCKS_CIPHERS as FORMATTER_SHADOWSOCKS_CIPHERS,
    node_to_clash_proxy,
)
from parser import (
    SUPPORTED_SHADOWSOCKS_CIPHERS,
    analyze_content_blob,
    parse_content_blob,
    parse_node_uri,
)


class ParserTests(unittest.TestCase):
    def test_explicit_uri_lines_do_not_probe_repaired_base64(self) -> None:
        content = "\n".join((
            "vless://11111111-1111-1111-1111-111111111111@node.invalid:443?security=tls",
            "trojan://fixture-password@node.invalid:443?sni=example.invalid",
            "unknown://fixture-password@node.invalid:443",
            "https://source.invalid/subscription",
        ))
        original = parse_content_blob.__globals__["_strict_b64decode_text"]
        with patch("parser._strict_b64decode_text", wraps=original) as decoder:
            report = analyze_content_blob(content, source="fixture", tier="tier3")
        self.assertEqual([node.protocol for node in report.nodes], ["vless", "trojan"])
        self.assertEqual([issue.code for issue in report.rejected], [
            "unsupported_uri_scheme", "subscription_url_not_node",
        ])
        decoder.assert_not_called()

    def test_source_parse_template_cache_reuses_exact_uri_without_sharing_state(self) -> None:
        uri = (
            "vless://11111111-1111-1111-1111-111111111111@example.com:443"
            "?encryption=none&security=tls&type=tcp&sni=example.com#cached"
        )
        cache = {}
        cache_lock = threading.Lock()

        with patch(
            "parser._parse_node_uri_or_raise",
            wraps=parse_node_uri.__globals__["_parse_node_uri_or_raise"],
        ) as parse_mock:
            first = parse_content_blob(
                uri,
                source="source-a",
                tier="tier3",
                node_template_cache=cache,
                node_template_cache_lock=cache_lock,
            )
            second = parse_content_blob(
                uri,
                source="source-b",
                tier="tier3",
                node_template_cache=cache,
                node_template_cache_lock=cache_lock,
            )

        self.assertEqual(parse_mock.call_count, 1)
        self.assertEqual(first[0].source, "source-a")
        self.assertEqual(second[0].source, "source-b")
        first[0].extra["mutated"] = True
        self.assertNotIn("mutated", second[0].extra)

    def test_preexisting_cancel_returns_before_line_scanning(self) -> None:
        cancel_event = threading.Event()
        cancel_event.set()

        with patch(
            "parser._line_records",
            side_effect=AssertionError("line scanning should not start"),
        ):
            nodes = parse_content_blob(
                "vless://id@example.com:443?encryption=none",
                source="cancelled-before-scan",
                tier="tier3",
                cancel_event=cancel_event,
            )

        self.assertEqual(nodes, [])

    def test_large_content_parse_honors_cancellation(self) -> None:
        cancel_event = threading.Event()
        cancel_event.set()
        content = "\n".join(
            f"ss://YWVzLTEyOC1nY206cGFzc0Bub2Rl{index}.example:8388#node-{index}"
            for index in range(10_000)
        )

        self.assertEqual(
            parse_content_blob(
                content,
                source="cancelled-large-content",
                tier="tier3",
                cancel_event=cancel_event,
            ),
            [],
        )

    def test_large_content_parse_checks_cancel_during_scan(self) -> None:
        class CancelDuringScan:
            def __init__(self) -> None:
                self.checks = 0

            def is_set(self) -> bool:
                self.checks += 1
                return self.checks >= 64

        cancel_event = CancelDuringScan()
        content = "\n".join(
            f"ss://YWVzLTEyOC1nY206cGFzc0Bub2Rl{index}.example:8388#node-{index}"
            for index in range(10_000)
        )

        self.assertEqual(
            parse_content_blob(
                content,
                source="cancelled-mid-scan",
                tier="tier3",
                cancel_event=cancel_event,
            ),
            [],
        )
        self.assertGreaterEqual(cancel_event.checks, 64)

    def test_shadowsocks_cipher_validation_matches_formatter(self) -> None:
        self.assertEqual(SUPPORTED_SHADOWSOCKS_CIPHERS, FORMATTER_SHADOWSOCKS_CIPHERS)

    def test_parse_vless(self) -> None:
        uri = (
            "vless://11111111-1111-1111-1111-111111111111@example.com:443"
            "?encryption=none&security=reality&type=tcp&pbk=testkey&sid=abcd&sni=www.microsoft.com#demo"
        )
        node = parse_node_uri(uri, source="test", tier="tier1")
        self.assertIsNotNone(node)
        assert node is not None
        self.assertEqual(node.protocol, "vless")
        self.assertEqual(node.address, "example.com")
        self.assertEqual(node.port, 443)
        self.assertEqual(node.uuid, "11111111-1111-1111-1111-111111111111")

    def test_parse_vmess(self) -> None:
        payload = {
            "v": "2",
            "ps": "demo-vmess",
            "add": "vmess.example.com",
            "port": "443",
            "id": "22222222-2222-2222-2222-222222222222",
            "aid": "0",
            "net": "ws",
            "path": "/ws",
            "host": "demo.example.com",
            "tls": "tls",
        }
        uri = "vmess://" + base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")
        node = parse_node_uri(uri, source="test", tier="tier2")
        self.assertIsNotNone(node)
        assert node is not None
        self.assertEqual(node.protocol, "vmess")
        self.assertEqual(node.path, "/ws")
        self.assertTrue(node.tls)
        self.assertEqual(node.encryption, "auto")

    def test_parse_base64_blob(self) -> None:
        content = "vless://id@example.com:443?encryption=none#one\nss://YWVzLTI1Ni1nY206cGFzc0BleGFtcGxlLmNvbTo4Mzg4#two"
        encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        nodes = parse_content_blob(encoded, source="blob", tier="tier3")
        self.assertEqual(len(nodes), 2)

    def test_parse_ss_ipv6_strips_address_brackets(self) -> None:
        node = parse_node_uri("ss://aes-128-gcm:pass@[2001:db8::1]:8388#v6", source="test", tier="tier3")

        self.assertIsNotNone(node)
        assert node is not None
        self.assertEqual(node.address, "2001:db8::1")
        self.assertEqual(node.port, 8388)

    def test_parse_modern_subscription_protocols(self) -> None:
        content = "\n".join(
            [
                "hysteria2://hy2-pass@example.com:443/?sni=hy2.example.com&insecure=1#hy2",
                "anytls://any-pass@example.net:8443/?sni=any.example.net#any",
                "tuic://33333333-3333-3333-3333-333333333333:tuic-pass@example.org:10443?udp_relay_mode=native&congestion_control=bbr&sni=tuic.example.org#tuic",
            ]
        )

        nodes = parse_content_blob(content, source="blob", tier="tier3")

        self.assertEqual([node.protocol for node in nodes], ["hysteria2", "anytls", "tuic"])
        self.assertEqual(nodes[0].password, "hy2-pass")
        self.assertTrue(nodes[0].extra["skip-cert-verify"])
        self.assertEqual(nodes[2].uuid, "33333333-3333-3333-3333-333333333333")
        self.assertEqual(nodes[2].extra["congestion_control"], "bbr")

    def test_parse_naive_links_from_subscription_content(self) -> None:
        nodes = parse_content_blob("https://user:pass@naive.example:443#naive", source="blob", tier="tier1")

        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].protocol, "http")
        self.assertEqual(nodes[0].address, "naive.example")
        self.assertEqual(nodes[0].extra["username"], "user")
        self.assertEqual(nodes[0].password, "pass")
        self.assertTrue(nodes[0].tls)

    def test_ordinary_https_links_are_ignored_without_warning(self) -> None:
        records = []

        class ListHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        logger = logging.getLogger("parser-ordinary-https-test")
        logger.handlers.clear()
        logger.setLevel(logging.WARNING)
        logger.propagate = False
        logger.addHandler(ListHandler())

        nodes = parse_content_blob("Read https://t.me/s/channel for updates", source="html", tier="tier3", logger=logger)

        self.assertEqual(nodes, [])
        self.assertEqual(records, [])

    def test_malformed_vless_and_trojan_without_userinfo_are_skipped(self) -> None:
        self.assertIsNone(parse_node_uri("vless://example.com:443?encryption=none", source="test", tier="tier3"))
        self.assertIsNone(parse_node_uri("trojan://example.com:443", source="test", tier="tier3"))

    def test_parse_tuic_v5_version_field(self) -> None:
        node = parse_node_uri(
            "tuic://44444444-4444-4444-4444-444444444444:pw@example.org:10443"
            "?version=5&udp_relay_mode=native&congestion_control=bbr&sni=tuic.example.org#tuic5",
            source="test",
            tier="tier3",
        )

        self.assertIsNotNone(node)
        assert node is not None
        self.assertEqual(node.extra.get("version"), 5)
        self.assertEqual(node.extra.get("udp-relay-mode"), "native")
        self.assertEqual(node.extra.get("congestion-controller"), "bbr")

    def test_parse_wireguard_v2rayn_uri(self) -> None:
        uri = (
            "wireguard://cHJpdmF0ZWtleWJhc2U2NA==@wg.example.com:51820"
            "?publickey=cGVlcnB1YmJhc2U2NA%3D%3D&allowed_ips=0.0.0.0/0,::/0"
            "&address=10.0.0.2/32,fd00::2/128&mtu=1408&reserved=1,2,3#wg-demo"
        )
        node = parse_node_uri(uri, source="test", tier="tier1")

        self.assertIsNotNone(node)
        assert node is not None
        self.assertEqual(node.protocol, "wireguard")
        self.assertEqual(node.address, "wg.example.com")
        self.assertEqual(node.port, 51820)
        self.assertEqual(node.password, "cHJpdmF0ZWtleWJhc2U2NA==")
        self.assertEqual(node.extra["public-key"], "cGVlcnB1YmJhc2U2NA==")
        self.assertEqual(node.extra["allowed-ips"], ["0.0.0.0/0", "::/0"])
        self.assertEqual(node.extra["ip"], "10.0.0.2")
        self.assertEqual(node.extra["ipv6"], "fd00::2")
        self.assertEqual(node.extra["reserved"], [1, 2, 3])
        self.assertEqual(node.extra["mtu"], 1408)

    def test_parse_wireguard_amnezia_fields(self) -> None:
        uri = (
            "wireguard://k@host:51820?publickey=p&jc=5&jmin=50&jmax=100"
            "&s1=0&s2=0&h1=1&h2=2&h3=3&h4=4&address=10.0.0.2/32"
        )
        node = parse_node_uri(uri, source="test", tier="tier1")
        assert node is not None
        for key in ("jc", "jmin", "jmax", "s1", "s2", "h1", "h2", "h3", "h4"):
            self.assertIn(key, node.extra)
        self.assertEqual(node.extra["jc"], 5)

    def test_wireguard_uri_is_extracted_from_subscription_blob(self) -> None:
        content = (
            "wireguard://pk@wg.example.com:51820?publickey=pub&allowed_ips=0.0.0.0/0&address=10.0.0.2/32\n"
            "vless://id@example.com:443?encryption=none\n"
        )
        nodes = parse_content_blob(content, source="blob", tier="tier1")
        self.assertEqual([node.protocol for node in nodes], ["wireguard", "vless"])

    def test_analyze_report_is_structured_serializable_and_redacted(self) -> None:
        content = (
            "vless://id@example.com:443?encryption=none#accepted\n"
            "trojan://super-secret@bad.example:99999#rejected"
        )

        report = analyze_content_blob(content, source="paste", tier="tier1")
        serialized = json.dumps(report.to_dict(), ensure_ascii=False)

        self.assertEqual(report.candidate_count, 2)
        self.assertEqual(report.accepted_count, 1)
        self.assertEqual(report.rejected_count, 1)
        self.assertTrue(report.blocked)
        self.assertEqual(report.nodes, report.accepted_nodes)
        self.assertEqual(report.rejected[0].code, "invalid_port")
        self.assertNotIn("super-secret", serialized)
        self.assertNotIn("trojan://", serialized)
        self.assertIn("fingerprint", serialized)

    def test_wrapped_base64_is_decoded_once_at_64_and_76_columns(self) -> None:
        content = "\n".join(
            [
                "vless://id@one.example:443?encryption=none#one",
                "hysteria2://password@two.example:8443#two",
                "socks5://user:password@three.example:1080#three",
            ]
        )
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")

        for width in (64, 76):
            with self.subTest(width=width):
                wrapped = "\n".join(encoded[index : index + width] for index in range(0, len(encoded), width))
                report = analyze_content_blob(wrapped, source="paste", tier="tier1")

                self.assertFalse(report.blocked)
                self.assertEqual(report.base64_sources, ["wrapped"])
                self.assertEqual(report.candidate_count, 3)
                self.assertEqual(report.duplicate_count, 0)
                self.assertEqual([node.address for node in report.nodes], ["one.example", "two.example", "three.example"])

    def test_wrapped_base64_with_short_tail_never_accepts_truncated_nodes(self) -> None:
        expected_addresses = ["h0.example", "h1.example"]
        content = "\n".join(
            f"socks5://u:p@{address}:1080#{index}"
            for index, address in enumerate(expected_addresses)
        )
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")

        for width in (64, 76):
            with self.subTest(width=width):
                wrapped_lines = [encoded[index : index + width] for index in range(0, len(encoded), width)]
                self.assertLess(len(wrapped_lines[-1]), 24)

                report = analyze_content_blob("\n".join(wrapped_lines), source="paste", tier="tier1")

                self.assertTrue(report.ok)
                self.assertEqual(report.base64_sources, ["wrapped"])
                self.assertEqual([node.address for node in report.nodes], expected_addresses)

    def test_corrupted_wrapped_base64_blocks_the_entire_wrapped_candidate(self) -> None:
        content = "\n".join(
            f"socks5://u:p@h{index}.example:1080#{index}"
            for index in range(6)
        )
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")

        for width in (64, 76):
            original_lines = [encoded[index : index + width] for index in range(0, len(encoded), width)]
            for corrupt_index in (0, 1):
                for mutation in ("replace", "delete", "early_padding"):
                    with self.subTest(width=width, corrupt_index=corrupt_index, mutation=mutation):
                        wrapped_lines = list(original_lines)
                        line = wrapped_lines[corrupt_index]
                        if mutation == "replace":
                            wrapped_lines[corrupt_index] = f"{line[:5]}*{line[6:]}"
                        elif mutation == "early_padding":
                            wrapped_lines[corrupt_index] = f"{line[:5]}={line[6:]}"
                        else:
                            wrapped_lines[corrupt_index] = f"{line[:5]}{line[6:]}"

                        report = analyze_content_blob(
                            "\n".join(wrapped_lines),
                            source="paste",
                            tier="tier1",
                        )

                        self.assertTrue(report.blocked)
                        self.assertEqual(report.accepted_count, 0)
                        self.assertEqual(report.base64_sources, ["wrapped"])
                        self.assertIn("invalid_base64", [issue.code for issue in report.rejected])

    def test_wrapped_base64_with_blank_lines_is_decoded_once(self) -> None:
        content = "\n".join(
            [
                "vless://id@alpha.example:443?encryption=none#alpha",
                "hysteria2://pw@beta.example:8443#beta",
                "socks5://gamma.example:1080#gamma",
            ]
        )
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")

        for width in (64, 76):
            original_lines = [encoded[index : index + width] for index in range(0, len(encoded), width)]
            for boundary in range(1, len(original_lines)):
                with self.subTest(width=width, boundary=boundary):
                    wrapped_lines = list(original_lines)
                    wrapped_lines.insert(boundary, "")
                    report = analyze_content_blob(
                        "\n".join(wrapped_lines),
                        source="paste",
                        tier="tier1",
                    )

                    self.assertFalse(report.blocked)
                    self.assertEqual(report.base64_sources, ["wrapped"])
                    self.assertEqual(
                        [node.address for node in report.nodes],
                        ["alpha.example", "beta.example", "gamma.example"],
                    )

    def test_blank_line_separates_two_complete_wrapped_base64_exports(self) -> None:
        first_content = (
            "vless://id@one.example:443?encryption=none#one\n"
            "hysteria2://pw@one.example:443#hy"
        )
        second_content = (
            "trojan://pw@two.example:443#tr\n"
            "socks5://u:p@two.example:1080#so"
        )

        for width in (64, 76):
            encoded_exports = []
            for content in (first_content, second_content):
                encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
                encoded_exports.append(
                    "\n".join(encoded[index : index + width] for index in range(0, len(encoded), width))
                )
            with self.subTest(width=width):
                report = analyze_content_blob(
                    "\n\n".join(encoded_exports),
                    source="paste",
                    tier="tier1",
                )

                self.assertFalse(report.blocked)
                self.assertEqual(
                    [(node.protocol, node.address) for node in report.nodes],
                    [
                        ("vless", "one.example"),
                        ("hysteria2", "one.example"),
                        ("trojan", "two.example"),
                        ("socks5", "two.example"),
                    ],
                )

    def test_ambiguous_full_width_base64_boundary_is_blocking(self) -> None:
        def encoded_with_length(prefix: str, target_length: int) -> str:
            for padding_length in range(256):
                encoded = base64.b64encode(f"{prefix}{'x' * padding_length}".encode("utf-8")).decode("ascii")
                if len(encoded) == target_length and not encoded.endswith("="):
                    return encoded
            self.fail(f"could not build Base64 fixture of length {target_length}")

        first = encoded_with_length(
            "vless://id@one.example:443?encryption=none#one\n"
            "hysteria2://pw@one.example:443#hy",
            152,
        )
        second = encoded_with_length(
            "trojan://pw@two.example:443#tr\n"
            "socks5://u:p@two.example:1080#so",
            84,
        )
        self.assertEqual([len(first[:76]), len(first[76:])], [76, 76])
        self.assertEqual([len(second[:76]), len(second[76:])], [76, 8])

        report = analyze_content_blob(
            "\n".join(
                [
                    first[:76],
                    first[76:],
                    "",
                    second[:76],
                    second[76:],
                    "anytls://pw@plain.example:443#plain",
                ]
            ),
            source="paste",
            tier="tier1",
        )

        self.assertTrue(report.blocked)
        self.assertEqual([(node.protocol, node.address) for node in report.nodes], [("anytls", "plain.example")])
        self.assertIn("invalid_base64", [issue.code for issue in report.rejected])

    def test_standard_wrapped_base64_rejects_wider_line_damage(self) -> None:
        content = "\n".join(
            f"socks5://u:p@host-{index}.example:1080#{index}"
            for index in range(10)
        )
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")

        for width in (64, 76):
            original_lines = [encoded[index : index + width] for index in range(0, len(encoded), width)]
            self.assertGreaterEqual(len(original_lines), 4)
            for deleted in (1, 2, 3, 5, 10):
                with self.subTest(width=width, deleted=deleted):
                    wrapped_lines = list(original_lines)
                    wrapped_lines[1] = wrapped_lines[1][:-deleted]
                    report = analyze_content_blob(
                        "\n".join(wrapped_lines),
                        source="paste",
                        tier="tier1",
                    )

                    self.assertTrue(report.blocked)
                    self.assertEqual(report.accepted_count, 0)
                    self.assertEqual(report.base64_sources, ["wrapped"])
                    self.assertIn("invalid_base64", [issue.code for issue in report.rejected])

    def test_single_base64_with_early_padding_is_blocked(self) -> None:
        contents = (
            "vless://id@alpha.example:443?encryption=none#alpha\n"
            "socks5://beta.example:1080#beta",
            "provider export\n"
            "vless://id@alpha.example:443?encryption=none#alpha\n"
            "socks5://beta.example:1080#beta",
        )

        for content in contents:
            encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
            corrupt_at = max(12, len(encoded) // 3)
            self.assertNotEqual(encoded[corrupt_at], "=")
            corrupted = f"{encoded[:corrupt_at]}={encoded[corrupt_at + 1:]}"
            with self.subTest(label_first=content.startswith("provider")):
                report = analyze_content_blob(corrupted, source="paste", tier="tier1")

                self.assertTrue(report.blocked)
                self.assertEqual(report.accepted_count, 0)
                self.assertIn("invalid_base64", [issue.code for issue in report.rejected])

    def test_label_first_whole_base64_with_one_extra_character_is_blocking(self) -> None:
        prefix = (
            "provider export metadataxx\n"
            "vless://id@alpha.example:443?encryption=none#alpha"
        )
        encoded = ""
        for padding_length in range(256):
            candidate = base64.b64encode(f"{prefix}{'x' * padding_length}".encode("utf-8")).decode("ascii")
            if len(candidate) == 148 and not candidate.endswith("="):
                encoded = candidate
                break
        self.assertTrue(encoded)

        for suffix in ("A", "B", "_", "="):
            with self.subTest(suffix=suffix):
                report = analyze_content_blob(
                    f"{encoded}{suffix}\nhysteria2://pw@backup.example:443#backup",
                    source="paste",
                    tier="tier1",
                )

                self.assertTrue(report.blocked)
                self.assertEqual(
                    [(node.protocol, node.address) for node in report.nodes],
                    [("hysteria2", "backup.example")],
                )
                self.assertIn("invalid_base64", [issue.code for issue in report.rejected])

    def test_two_line_label_first_wrapper_damage_blocks_a_partial_batch(self) -> None:
        content = (
            "provider export metadata\n"
            "vless://11111111-1111-1111-1111-111111111111@alpha.example:443"
            "?encryption=none#alpha"
        )
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        original_lines = [encoded[index : index + 76] for index in range(0, len(encoded), 76)]
        self.assertEqual(len(original_lines), 2)
        plain_node = "hysteria2://pw@backup.example:443#backup"

        for delta in (-1, -2, -3, -5, 1, 2, 3, 5):
            with self.subTest(delta=delta):
                wrapped_lines = list(original_lines)
                if delta < 0:
                    wrapped_lines[0] = wrapped_lines[0][:delta]
                else:
                    wrapped_lines[0] = f"{wrapped_lines[0]}{'A' * delta}"
                report = analyze_content_blob(
                    "\n".join([*wrapped_lines, plain_node]),
                    source="paste",
                    tier="tier1",
                )

                self.assertTrue(report.blocked)
                self.assertEqual(report.accepted_count, 1)
                self.assertEqual(report.nodes[0].address, "backup.example")
                self.assertIn("invalid_base64", [issue.code for issue in report.rejected])

    def test_full_width_final_base64_line_growth_is_blocking(self) -> None:
        prefix = (
            "vless://id@alpha.example:443?encryption=none#alpha\n"
            "hysteria2://pw@beta.example:443#beta\n"
            "socks5://gamma.example:1080#gamma"
        )
        encoded = ""
        for padding_length in range(512):
            candidate = base64.b64encode(f"{prefix}{'x' * padding_length}".encode("utf-8")).decode("ascii")
            if len(candidate) == 228 and not candidate.endswith("="):
                encoded = candidate
                break
        self.assertTrue(encoded)
        original_lines = [encoded[index : index + 76] for index in range(0, len(encoded), 76)]
        self.assertEqual([len(line) for line in original_lines], [76, 76, 76])

        for suffix in ("A", "B", "_", "="):
            with self.subTest(suffix=suffix):
                wrapped_lines = list(original_lines)
                wrapped_lines[-1] += suffix
                report = analyze_content_blob(
                    "\n".join(wrapped_lines),
                    source="paste",
                    tier="tier1",
                )

                self.assertTrue(report.blocked)
                self.assertEqual(report.accepted_count, 0)
                self.assertIn("invalid_base64", [issue.code for issue in report.rejected])

    def test_legacy_parse_content_blob_keeps_exact_uri_deduplication(self) -> None:
        uri = "socks5://user:password@duplicate.example:1080#same"

        nodes = parse_content_blob(f"{uri}\n{uri}", source="legacy", tier="tier3")

        self.assertEqual(len(nodes), 1)

    def test_independent_base64_lines_and_mixed_plaintext_keep_visual_order(self) -> None:
        first = "vless://id@first.example:443?encryption=none#first"
        second = "hysteria2://pw@second.example:8443#second"
        third = "socks5://third.example:1080#third"
        encoded_second = base64.b64encode(second.encode("utf-8")).decode("ascii")
        encoded_third = base64.b64encode(third.encode("utf-8")).decode("ascii")

        report = analyze_content_blob(
            "\n".join([first, encoded_second, encoded_third]),
            source="paste",
            tier="tier1",
        )

        self.assertFalse(report.blocked)
        self.assertEqual([node.address for node in report.nodes], ["first.example", "second.example", "third.example"])
        self.assertEqual(report.base64_sources, ["line:2", "line:3"])

    def test_equal_width_unpadded_base64_uri_lines_are_not_joined_as_wrapped(self) -> None:
        first = "socks5://one.example:1080#first"
        second = "socks5://two.example:1080#second"
        while len(first.encode("utf-8")) % 3:
            first += "x"
        while len(second.encode("utf-8")) % 3:
            second += "x"
        self.assertEqual(len(first), len(second))
        encoded_first = base64.b64encode(first.encode("utf-8")).decode("ascii")
        encoded_second = base64.b64encode(second.encode("utf-8")).decode("ascii")
        self.assertNotIn("=", encoded_first + encoded_second)
        self.assertEqual(len(encoded_first), len(encoded_second))

        report = analyze_content_blob(
            f"{encoded_first}\n{encoded_second}",
            source="paste",
            tier="tier1",
        )

        self.assertFalse(report.blocked)
        self.assertEqual(report.base64_sources, ["line:1", "line:2"])
        self.assertEqual([node.address for node in report.nodes], ["one.example", "two.example"])

    def test_duplicate_nodes_are_reported_and_first_seen_order_is_preserved(self) -> None:
        report = analyze_content_blob(
            "\n".join(
                [
                    "vless://same@one.example:443?encryption=none#first-name",
                    "vless://same@one.example:443?encryption=none#second-name",
                    "trojan://pw@two.example:443#two",
                ]
            ),
            source="paste",
            tier="tier1",
        )

        self.assertEqual(report.candidate_count, 3)
        self.assertEqual(report.duplicate_count, 1)
        self.assertEqual([node.name for node in report.nodes], ["first-name", "two"])

    def test_invalid_base64_unknown_uri_and_structured_formats_block(self) -> None:
        encoded = base64.b64encode(b"vless://id@example.com:443?encryption=none").decode("ascii")
        invalid_base64 = encoded[:-1] + "*"
        cases = {
            "base64": (invalid_base64, "invalid_base64"),
            "unknown": ("customproxy://credential@example.com:443", "unsupported_uri_scheme"),
            "yaml": ("proxies:\n  - name: demo\n    type: vless\n    server: example.com", "unsupported_structured_format"),
            "json": ('{"outbounds": [{"type": "vless", "server": "example.com"}]}', "unsupported_structured_format"),
        }

        for label, (content, expected_code) in cases.items():
            with self.subTest(label=label):
                report = analyze_content_blob(content, source="paste", tier="tier1")
                self.assertTrue(report.blocked)
                self.assertIn(expected_code, [issue.code for issue in report.rejected])

    def test_protocol_internal_base64_is_strict(self) -> None:
        vmess_payload = {
            "v": "2",
            "ps": "strict-vmess",
            "add": "vmess.example.com",
            "port": "443",
            "id": "22222222-2222-2222-2222-222222222222",
            "aid": "0",
        }
        valid_vmess = base64.b64encode(json.dumps(vmess_payload).encode("utf-8")).decode("ascii")
        valid_ssr = (
            "ZXhhbXBsZS5jb206NDQzOmF1dGhfc2hhMV92NDphZXMtMTI4LWNmYjp0bHMxLjJfdGlja2V0X2F1dGg6"
            "Y0dGemN3Lz9yZW1hcmtzPVpHVnRiZw"
        )

        for scheme, payload in (("vmess", valid_vmess), ("ssr", valid_ssr)):
            with self.subTest(scheme=scheme):
                corrupt = f"{payload[:12]}*{payload[13:]}"
                report = analyze_content_blob(f"{scheme}://{corrupt}", source="paste", tier="tier1")

                self.assertTrue(report.blocked)
                self.assertEqual(report.accepted_count, 0)
                self.assertIn("invalid_base64", [issue.code for issue in report.rejected])

    def test_ordinary_json_is_explanatory_text_not_a_proxy_candidate(self) -> None:
        for content in ('{"note": "ordinary explanatory text"}', "[]"):
            with self.subTest(content=content):
                report = analyze_content_blob(content, source="paste", tier="tier1")

                self.assertFalse(report.blocked)
                self.assertEqual(report.candidate_count, 0)
                self.assertEqual(report.nodes, [])

    def test_corrupt_base64_with_leading_label_blocks_a_partial_batch(self) -> None:
        encoded = base64.b64encode(
            b"provider export\nhysteria2://password@backup.example:443#backup"
        ).decode("ascii")
        content = "\n".join(
            [
                "vless://id@primary.example:443?encryption=none#primary",
                encoded[:-1] + "*",
            ]
        )

        report = analyze_content_blob(content, source="paste", tier="tier1")

        self.assertEqual(report.accepted_count, 1)
        self.assertTrue(report.blocked)
        self.assertIn("invalid_base64", [issue.code for issue in report.rejected])

    def test_plain_labels_are_not_blocking_candidates(self) -> None:
        report = analyze_content_blob(
            "ReleaseCandidateNodeIdentifier20260715\nThis is an ordinary label",
            source="paste",
            tier="tier1",
        )

        self.assertFalse(report.blocked)
        self.assertEqual(report.candidate_count, 0)
        self.assertEqual(report.nodes, [])

    def test_http_url_without_auth_is_redirected_to_subscription_import(self) -> None:
        report = analyze_content_blob(
            "https://provider.example/api/v1/client/subscribe?token=secret-token",
            source="paste",
            tier="tier1",
        )
        serialized = json.dumps(report.to_dict())

        self.assertTrue(report.blocked)
        self.assertEqual(report.candidate_count, 1)
        self.assertEqual(report.rejected[0].code, "subscription_url_not_node")
        self.assertNotIn("secret-token", serialized)

    def test_authenticated_http_is_ambiguous_only_for_strict_analysis(self) -> None:
        uri = "https://user:password@naive.example:443#naive"

        report = analyze_content_blob(uri, source="paste", tier="tier1")
        compatible_nodes = parse_content_blob(uri, source="legacy", tier="tier1")

        self.assertTrue(report.blocked)
        self.assertEqual(report.rejected[0].code, "ambiguous_authenticated_http")
        self.assertEqual(len(compatible_nodes), 1)
        self.assertEqual(compatible_nodes[0].protocol, "http")

    def test_explicit_naive_scheme_controls_tls_with_security_query(self) -> None:
        plain = parse_node_uri(
            "naive://user:password@naive.example:80?security=none#plain",
            source="manual",
            tier="tier1",
        )
        secure = parse_node_uri(
            "naive://user:password@naive.example:443?security=tls&sni=edge.example#secure",
            source="manual",
            tier="tier1",
        )

        assert plain is not None and secure is not None
        self.assertFalse(plain.tls)
        self.assertTrue(secure.tls)
        self.assertEqual(secure.sni, "edge.example")

    def test_runtime_required_fields_are_rejected_during_analysis(self) -> None:
        vmess_without_uuid = "vmess://" + base64.b64encode(
            json.dumps({"add": "vmess.example", "port": 443}).encode("utf-8")
        ).decode("ascii")
        invalid_cases = {
            "vless_uuid": ("vless://@example.com:443?encryption=none", "missing_uuid"),
            "vless_reality_keys": (
                "vless://11111111-1111-1111-1111-111111111111@example.com:443"
                "?encryption=none&security=reality&sid=abcd",
                "missing_reality_keys",
            ),
            "vmess_uuid": (vmess_without_uuid, "missing_uuid"),
            "vmess_alter_id": (
                "vmess://"
                + base64.b64encode(
                    json.dumps(
                        {
                            "add": "vmess.example",
                            "port": 443,
                            "id": "22222222-2222-2222-2222-222222222222",
                            "aid": "not-a-number",
                        }
                    ).encode("utf-8")
                ).decode("ascii"),
                "invalid_vmess_alter_id",
            ),
            "trojan_password": ("trojan://@example.com:443", "missing_password"),
            "hysteria_password": ("hysteria2://example.com:443", "missing_password"),
            "anytls_password": ("anytls://example.com:443", "missing_password"),
            "ss_cipher": ("ss://unsupported-cipher:password@example.com:8388", "unsupported_cipher"),
            "tuic_credentials": ("tuic://example.com:443", "missing_tuic_credentials"),
            "wireguard_keys": ("wireguard://private@example.com:51820", "missing_wireguard_keys"),
            "wireguard_address": (
                "wireguard://private@example.com:51820?publickey=public",
                "missing_wireguard_address",
            ),
            "port": ("socks5://example.com:70000", "invalid_port"),
        }

        for label, (uri, expected_code) in invalid_cases.items():
            with self.subTest(label=label):
                report = analyze_content_blob(uri, source="paste", tier="tier1")
                self.assertTrue(report.blocked)
                self.assertEqual(report.accepted_count, 0)
                self.assertIn(expected_code, [issue.code for issue in report.rejected])

    def test_unreachable_one_endpoint_remains_editable_and_self_checkable(self) -> None:
        report = analyze_content_blob(
            "socks5://1:1#1",
            source="paste",
            tier="tier1",
        )

        self.assertTrue(report.ok, report.to_dict())
        self.assertEqual(report.accepted_count, 1)
        self.assertEqual(report.nodes[0].name, "1")
        self.assertEqual(report.nodes[0].address, "1")
        self.assertEqual(report.nodes[0].port, 1)

    def test_strictly_accepted_runtime_sensitive_nodes_generate_complete_clash_proxies(self) -> None:
        vmess = "vmess://" + base64.b64encode(
            json.dumps(
                {
                    "add": "vmess.example",
                    "port": 443,
                    "id": "22222222-2222-2222-2222-222222222222",
                    "aid": "0",
                    "tls": "tls",
                }
            ).encode("utf-8")
        ).decode("ascii")
        cases = {
            "reality": (
                "vless://11111111-1111-1111-1111-111111111111@reality.example:443"
                "?encryption=none&security=reality&pbk=public-key&sid=abcd&sni=reality.example",
                "reality-opts",
            ),
            "vmess": (vmess, "alterId"),
            "wireguard": (
                "wireguard://private@wg.example:51820?publickey=public&address=10.0.0.2/32",
                "ip",
            ),
        }

        for label, (uri, required_key) in cases.items():
            with self.subTest(label=label):
                report = analyze_content_blob(uri, source="paste", tier="tier1")
                self.assertTrue(report.ok, report.to_dict())
                proxy = node_to_clash_proxy(report.nodes[0])
                self.assertIsNotNone(proxy)
                assert proxy is not None
                self.assertIn(required_key, proxy)


if __name__ == "__main__":
    unittest.main()
