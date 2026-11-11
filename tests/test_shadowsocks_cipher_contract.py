from __future__ import annotations

import base64
import subprocess
import unittest
from pathlib import Path

import yaml

from clash_formatter import SUPPORTED_CLASH_PROTOCOLS, node_to_clash_proxy
from import_protocol_view import (
    PROTOCOL_FIELD_DEFAULTS,
    PROTOCOL_SPECS,
    PROTOCOL_TEMPLATE_SPECS,
    protocol_field_is_required,
    protocol_field_is_visible,
    protocol_condition_field_keys,
    protocol_required_field_keys,
    protocol_visible_notice_codes,
)
from parser import SUPPORTED_URI_SCHEMES, analyze_content_blob, parse_node_uri
from shadowsocks_capabilities import (
    DEFAULT_SHADOWSOCKS_CIPHER,
    MIHOMO_SHADOWSOCKS_VERSION,
    SHADOWSOCKS_CIPHER_CHOICES,
    SUPPORTED_SHADOWSOCKS_CIPHERS,
    shadowsocks_cipher_capability,
    validate_shadowsocks_credential,
)
from structured_node_uri import build_structured_node_uri


OFFICIAL_MIHOMO_1_19_24_CIPHERS = (
    "aes-128-ctr",
    "aes-192-ctr",
    "aes-256-ctr",
    "aes-128-cfb",
    "aes-192-cfb",
    "aes-256-cfb",
    "aes-128-gcm",
    "aes-192-gcm",
    "aes-256-gcm",
    "aes-128-ccm",
    "aes-192-ccm",
    "aes-256-ccm",
    "aes-128-gcm-siv",
    "aes-256-gcm-siv",
    "chacha20-ietf",
    "chacha20",
    "xchacha20",
    "chacha20-ietf-poly1305",
    "xchacha20-ietf-poly1305",
    "chacha8-ietf-poly1305",
    "xchacha8-ietf-poly1305",
    "2022-blake3-aes-128-gcm",
    "2022-blake3-aes-256-gcm",
    "2022-blake3-chacha20-poly1305",
    "lea-128-gcm",
    "lea-192-gcm",
    "lea-256-gcm",
    "rabbit128-poly1305",
    "aegis-128l",
    "aegis-256",
    "aez-384",
    "deoxys-ii-256-128",
    "rc4-md5",
    "none",
)


def _credential_for(cipher: str) -> str:
    capability = shadowsocks_cipher_capability(cipher)
    assert capability is not None
    if capability.decoded_key_bytes:
        return base64.b64encode(b"k" * capability.decoded_key_bytes).decode("ascii")
    if not capability.password_required:
        return ""
    return "test-password"


class ShadowsocksCipherContractTests(unittest.TestCase):
    def test_capability_is_the_exact_mihomo_1_19_24_set(self) -> None:
        self.assertEqual(MIHOMO_SHADOWSOCKS_VERSION, "1.19.27")
        self.assertEqual(SUPPORTED_SHADOWSOCKS_CIPHERS, frozenset(OFFICIAL_MIHOMO_1_19_24_CIPHERS))
        self.assertEqual(len(SHADOWSOCKS_CIPHER_CHOICES), 34)
        self.assertEqual(SHADOWSOCKS_CIPHER_CHOICES[0], DEFAULT_SHADOWSOCKS_CIPHER)
        self.assertEqual(SHADOWSOCKS_CIPHER_CHOICES[-1], "none")
        self.assertEqual(set(SHADOWSOCKS_CIPHER_CHOICES), SUPPORTED_SHADOWSOCKS_CIPHERS)

    def test_bundled_mihomo_accepts_every_declared_cipher(self) -> None:
        executable = Path(__file__).resolve().parents[1] / "bin" / "mihomo.exe"
        if not executable.is_file():
            self.skipTest("bundled Mihomo is not available")

        version = subprocess.run(
            [str(executable), "-v"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        version_output = f"{version.stdout}\n{version.stderr}"
        self.assertEqual(version.returncode, 0, version_output)
        self.assertIn(f"v{MIHOMO_SHADOWSOCKS_VERSION}", version_output)

        for index, cipher in enumerate(OFFICIAL_MIHOMO_1_19_24_CIPHERS, start=1):
            with self.subTest(cipher=cipher):
                config = {
                    "mixed-port": 17890,
                    "mode": "rule",
                    "log-level": "silent",
                    "proxies": [
                        {
                            "name": f"ss-contract-{index}",
                            "type": "ss",
                            "server": "127.0.0.1",
                            "port": 8388,
                            "cipher": cipher,
                            "password": _credential_for(cipher),
                        }
                    ],
                }
                encoded_config = base64.b64encode(
                    yaml.safe_dump(config, sort_keys=False).encode("utf-8")
                ).decode("ascii")
                result = subprocess.run(
                    [str(executable), "-t", "-config", encoded_config],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
                )

    def test_every_supported_cipher_roundtrips_parser_to_formatter(self) -> None:
        for cipher in OFFICIAL_MIHOMO_1_19_24_CIPHERS:
            with self.subTest(cipher=cipher):
                password = _credential_for(cipher)
                uri = f"ss://{cipher}:{password}@ss.example:8388#{cipher}"
                node = parse_node_uri(uri, source="test", tier="tier2")
                self.assertIsNotNone(node)
                assert node is not None
                proxy = node_to_clash_proxy(node)
                self.assertIsNotNone(proxy)
                assert proxy is not None
                self.assertEqual(proxy["type"], "ss")
                self.assertEqual(proxy["cipher"], cipher)
                self.assertEqual(proxy["password"], password)

    def test_unknown_cipher_is_rejected_with_a_structured_code(self) -> None:
        report = analyze_content_blob(
            "ss://future-cipher:password@ss.example:8388#unknown",
            source="paste",
            tier="tier2",
        )

        self.assertTrue(report.blocked)
        self.assertEqual(report.accepted_count, 0)
        issue = next(item for item in report.rejected if item.code == "unsupported_cipher")
        self.assertEqual(issue.params.get("cipher"), "future-cipher")

    def test_2022_credentials_enforce_base64_and_decoded_key_lengths(self) -> None:
        cases = (
            ("2022-blake3-aes-128-gcm", 16),
            ("2022-blake3-aes-256-gcm", 32),
            ("2022-blake3-chacha20-poly1305", 32),
        )
        for cipher, expected_bytes in cases:
            valid = base64.b64encode(b"v" * expected_bytes).decode("ascii")
            with self.subTest(cipher=cipher, case="valid"):
                self.assertIsNone(validate_shadowsocks_credential(cipher, valid))
            with self.subTest(cipher=cipher, case="invalid_base64"):
                issue = validate_shadowsocks_credential(cipher, "not-base64!")
                self.assertIsNotNone(issue)
                assert issue is not None
                self.assertEqual(issue.code, "invalid_ss_2022_key_base64")
                self.assertEqual(issue.params["expected_bytes"], expected_bytes)
            with self.subTest(cipher=cipher, case="wrong_length"):
                issue = validate_shadowsocks_credential(
                    cipher,
                    base64.b64encode(b"x" * (expected_bytes - 1)).decode("ascii"),
                )
                self.assertIsNotNone(issue)
                assert issue is not None
                self.assertEqual(issue.code, "invalid_ss_2022_key_length")
                self.assertEqual(issue.params["expected_bytes"], expected_bytes)
                self.assertEqual(issue.params["actual_bytes"], expected_bytes - 1)

    def test_2022_key_chains_validate_each_component(self) -> None:
        key = base64.b64encode(b"k" * 32).decode("ascii")
        self.assertIsNone(validate_shadowsocks_credential("2022-blake3-aes-256-gcm", f"{key}:{key}"))

        issue = validate_shadowsocks_credential(
            "2022-blake3-aes-256-gcm",
            f"{key}:{base64.b64encode(b'short').decode('ascii')}",
        )

        self.assertIsNotNone(issue)
        assert issue is not None
        self.assertEqual(issue.code, "invalid_ss_2022_key_length")
        self.assertEqual(issue.params["key_index"], 2)

    def test_none_allows_empty_password_and_only_emits_the_short_notice(self) -> None:
        capability = shadowsocks_cipher_capability("none")
        self.assertIsNotNone(capability)
        assert capability is not None
        self.assertFalse(capability.password_required)
        self.assertIsNone(validate_shadowsocks_credential("none", ""))

        values = {("ss", "method"): "none"}
        reader = lambda protocol, field: values.get((protocol, field), "")
        self.assertFalse(protocol_field_is_visible("ss", "password", reader))
        self.assertFalse(protocol_field_is_required("ss", "password", reader))
        self.assertEqual(protocol_visible_notice_codes("ss", reader), ("ss_none_unencrypted",))

    def test_ss_template_uses_the_canonical_nonfreeform_cipher_choices(self) -> None:
        ss_spec = next(spec for spec in PROTOCOL_SPECS if spec.key == "ss")
        method = next(field for field in ss_spec.fields if field.key == "method")

        self.assertEqual(method.input_type, "enum")
        self.assertEqual(method.choices, SHADOWSOCKS_CIPHER_CHOICES)
        self.assertEqual(PROTOCOL_FIELD_DEFAULTS["ss"]["method"], DEFAULT_SHADOWSOCKS_CIPHER)


class CrossProtocolTemplateCapabilityTests(unittest.TestCase):
    RUNTIME_PROTOCOL_BY_TEMPLATE = {
        "vless_reality": "vless",
        "vmess": "vmess",
        "trojan": "trojan",
        "ss": "ss",
        "anytls": "anytls",
        "naive": "http",
        "socks5": "socks5",
        "hysteria2": "hysteria2",
        "tuic": "tuic",
        "wireguard": "wireguard",
        "argo_xhttp": "vless",
    }
    PARSER_SCHEME_BY_TEMPLATE = {
        **RUNTIME_PROTOCOL_BY_TEMPLATE,
        "vless_reality": "vless",
        "naive": "naive",
        "argo_xhttp": "vless",
    }

    def test_every_template_has_parser_and_formatter_runtime_support(self) -> None:
        template_keys = {template.protocol_key for template in PROTOCOL_TEMPLATE_SPECS}
        spec_keys = {spec.key for spec in PROTOCOL_SPECS}
        self.assertEqual(template_keys, spec_keys)
        self.assertEqual(template_keys, set(self.RUNTIME_PROTOCOL_BY_TEMPLATE))

        for protocol_key in sorted(template_keys):
            with self.subTest(protocol=protocol_key):
                self.assertIn(self.RUNTIME_PROTOCOL_BY_TEMPLATE[protocol_key], SUPPORTED_CLASH_PROTOCOLS)
                self.assertIn(self.PARSER_SCHEME_BY_TEMPLATE[protocol_key], SUPPORTED_URI_SCHEMES)

    def test_enum_defaults_and_required_visibility_never_drift(self) -> None:
        for spec in PROTOCOL_SPECS:
            defaults = PROTOCOL_FIELD_DEFAULTS.get(spec.key, {})
            fields = {field.key: field for field in spec.fields}
            for field in fields.values():
                if field.input_type == "enum":
                    self.assertTrue(field.choices, (spec.key, field.key))
                    if field.key in defaults:
                        self.assertIn(defaults[field.key], field.choices, (spec.key, field.key))
            for field_key in protocol_required_field_keys(spec.key):
                self.assertIn(field_key, fields, (spec.key, field_key))
                self.assertTrue(protocol_field_is_visible(spec.key, field_key), (spec.key, field_key))

    def test_condition_drivers_cover_every_dynamic_protocol_field(self) -> None:
        expected = {
            "vless_reality": {"network", "security"},
            "vmess": {"network", "security"},
            "hysteria2": {"obfs"},
            "ss": {"method"},
            "socks5": {"username", "password"},
        }
        for protocol, drivers in expected.items():
            with self.subTest(protocol=protocol):
                self.assertTrue(drivers <= set(protocol_condition_field_keys(protocol)))

    def test_default_and_enum_template_variants_reach_parser_and_formatter(self) -> None:
        for spec in PROTOCOL_SPECS:
            if spec.key == "argo_xhttp":
                continue
            defaults = dict(PROTOCOL_FIELD_DEFAULTS.get(spec.key, {}))
            cases: list[tuple[str, dict[str, str]]] = [("defaults", dict(defaults))]
            for field in spec.fields:
                if field.input_type == "enum":
                    cases.extend(
                        (f"{field.key}={choice}", {**defaults, field.key: choice})
                        for choice in field.choices
                    )
            for label, values in cases:
                if spec.key == "ss":
                    cipher = values.get("method", DEFAULT_SHADOWSOCKS_CIPHER)
                    values["password"] = _credential_for(cipher)
                if spec.key == "vless_reality" and values.get("security", "reality") == "reality":
                    values["short_id"] = "abcd"
                with self.subTest(protocol=spec.key, case=label):
                    uri = build_structured_node_uri(
                        spec.key,
                        lambda key, default="", current=values: current.get(key, default),
                    )
                    report = analyze_content_blob(uri, source="template-test", tier="tier1")
                    self.assertTrue(report.ok, report.to_dict())
                    self.assertEqual(report.accepted_count, 1)
                    self.assertIsNotNone(node_to_clash_proxy(report.nodes[0]))

    def test_argo_xhttp_stays_on_its_dedicated_multiprotocol_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported structured protocol: argo_xhttp"):
            build_structured_node_uri("argo_xhttp", lambda _key, default="": default)

    def test_socks5_authentication_is_optional_but_never_half_configured(self) -> None:
        anonymous_uri = build_structured_node_uri(
            "socks5",
            lambda key, default="": "" if key in {"username", "password"} else default,
        )
        anonymous = analyze_content_blob(anonymous_uri, source="template-test", tier="tier1")
        self.assertTrue(anonymous.ok, anonymous.to_dict())
        self.assertIsNotNone(node_to_clash_proxy(anonymous.nodes[0]))

        incomplete_uri = build_structured_node_uri(
            "socks5",
            lambda key, default="": {"username": "user", "password": ""}.get(key, default),
        )
        incomplete = analyze_content_blob(incomplete_uri, source="template-test", tier="tier1")
        self.assertTrue(incomplete.blocked)
        self.assertIn("incomplete_socks5_credentials", {issue.code for issue in incomplete.rejected})


if __name__ == "__main__":
    unittest.main()
