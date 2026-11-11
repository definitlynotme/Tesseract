import unittest

from structured_import_validation import (
    first_invalid_port_value,
    missing_structured_field_labels,
    selected_structured_protocols,
    structured_import_validation_message,
    structured_import_ui_state,
    structured_port_values,
    structured_protocol_key,
    structured_required_fields,
)


class StructuredImportValidationTests(unittest.TestCase):
    def test_structured_protocol_key_only_accepts_supported_protocols(self) -> None:
        self.assertEqual(structured_protocol_key("ss", ["ss", "trojan"]), "ss")
        self.assertEqual(structured_protocol_key("subscription", ["ss", "trojan"]), "")

    def test_selected_structured_protocols_preserves_single_and_multiprotocol_ordering(self) -> None:
        self.assertEqual(selected_structured_protocols("tier2_ss", ["ss", "trojan"]), ["ss"])
        self.assertEqual(selected_structured_protocols("tier2_subscription", ["ss", "trojan"]), [])

        selected = selected_structured_protocols(
            "tier1_multiprotocol",
            ["vless_reality", "hysteria2", "argo_xhttp"],
            selected_protocols=["argo_xhttp", "vless_reality", "hysteria2"],
            priority_value=lambda protocol: {"hysteria2": 1, "vless_reality": 2, "argo_xhttp": 2}[protocol],
            protocol_order=["vless_reality", "hysteria2", "argo_xhttp"],
        )

        self.assertEqual(selected, ["hysteria2", "vless_reality", "argo_xhttp"])

        manual = selected_structured_protocols(
            "tier1_manual",
            ["vless_reality", "hysteria2", "argo_xhttp"],
            selected_protocols=["argo_xhttp", "unknown", "hysteria2", "argo_xhttp"],
            protocol_order=["hysteria2", "argo_xhttp"],
        )
        self.assertEqual(manual, ["hysteria2", "argo_xhttp"])

    def test_structured_import_ui_state_preserves_paste_subscription_and_structured_modes(self) -> None:
        supported = ["ss", "trojan"]

        paste = structured_import_ui_state("tier2_nodes", supported)
        self.assertFalse(paste.structured)
        self.assertFalse(paste.subscription)
        self.assertTrue(paste.paste)

        subscription = structured_import_ui_state("tier3_subscription", supported)
        self.assertFalse(subscription.structured)
        self.assertTrue(subscription.subscription)
        self.assertFalse(subscription.paste)

        single = structured_import_ui_state("tier2_ss", supported)
        self.assertTrue(single.structured)
        self.assertEqual(single.protocol, "ss")
        self.assertFalse(single.multiprotocol)
        self.assertIn("普通单协议节点", single.help_text)

        multi = structured_import_ui_state("tier1_multiprotocol", supported)
        self.assertTrue(multi.structured)
        self.assertTrue(multi.multiprotocol)
        self.assertIn("公共信息", multi.help_text)

        manual = structured_import_ui_state("tier1_manual", supported)
        self.assertTrue(manual.structured)
        self.assertFalse(manual.subscription)
        self.assertFalse(manual.paste)
        self.assertFalse(manual.multiprotocol)
        self.assertIn("先添加协议", manual.help_text)

    def test_common_required_fields_apply_to_unknown_protocols(self) -> None:
        self.assertEqual(structured_required_fields(["unknown"]), {"name", "server", "port"})

    def test_vless_reality_required_fields_include_identity_transport_and_reality_keys(self) -> None:
        fields = structured_required_fields(["vless_reality"])

        self.assertTrue(
            {
                "name",
                "server",
                "port",
                "uuid",
                "sni",
                "public_key",
                "short_id",
                "encryption",
                "security",
                "network",
            }.issubset(fields)
        )

    def test_multiprotocol_required_fields_are_union_of_selected_protocols(self) -> None:
        fields = structured_required_fields(["ss", "wireguard", "tuic"])

        self.assertTrue({"method", "password", "private_key", "public_key", "local_address"}.issubset(fields))
        self.assertIn("uuid", fields)
        self.assertNotIn("sni", fields)
        self.assertFalse({"allowed_ips", "reserved", "mtu", "alpn", "skip_cert_verify"} & fields)

    def test_argo_xhttp_required_fields_omit_fixed_network_control(self) -> None:
        fields = structured_required_fields(["argo_xhttp"])

        self.assertEqual(
            fields,
            {"name", "server", "port", "uuid", "sni", "host", "path", "security"},
        )

    def test_transport_required_fields_follow_live_network(self) -> None:
        def required(protocol: str, network: str, security: str = "tls") -> set[str]:
            return structured_required_fields(
                [protocol],
                field_value=lambda _protocol, key: {
                    "network": network,
                    "security": security,
                }.get(key, "value"),
            )

        self.assertFalse({"host", "path", "grpc_service_name"} & required("vless_reality", "tcp", "reality"))
        self.assertTrue({"host", "path"} <= required("vmess", "ws"))
        self.assertTrue({"host", "path"} <= required("vmess", "h2"))
        self.assertTrue({"host", "path"} <= required("argo_xhttp", "xhttp"))
        self.assertIn("grpc_service_name", required("vless_reality", "grpc", "reality"))
        self.assertFalse({"host", "path"} & required("vless_reality", "grpc", "reality"))

    def test_tls_specific_and_optional_fields_are_not_globally_required(self) -> None:
        vmess_plain = structured_required_fields(
            ["vmess"],
            field_value=lambda _protocol, key: {"network": "tcp", "security": "none"}.get(key, ""),
        )
        vmess_tls = structured_required_fields(
            ["vmess"],
            field_value=lambda _protocol, key: {"network": "tcp", "security": "tls"}.get(key, ""),
        )

        self.assertNotIn("sni", vmess_plain)
        self.assertIn("sni", vmess_tls)
        for optional in ("alpn", "fingerprint", "skip_cert_verify"):
            self.assertNotIn(optional, vmess_tls)

        vless_plain = structured_required_fields(
            ["vless_reality"],
            field_value=lambda _protocol, key: {"network": "tcp", "security": "none"}.get(key, ""),
        )
        vless_reality = structured_required_fields(
            ["vless_reality"],
            field_value=lambda _protocol, key: {"network": "tcp", "security": "reality"}.get(key, ""),
        )
        self.assertFalse({"public_key", "short_id", "sni"} & vless_plain)
        self.assertTrue({"public_key", "short_id", "sni"} <= vless_reality)

    def test_obfs_password_is_required_only_when_obfs_is_active(self) -> None:
        inactive = structured_required_fields(
            ["hysteria2"],
            field_value=lambda _protocol, key: {"obfs": ""}.get(key, "value"),
        )
        active = structured_required_fields(
            ["hysteria2"],
            field_value=lambda _protocol, key: {"obfs": "salamander"}.get(key, "value"),
        )

        self.assertNotIn("obfs_password", inactive)
        self.assertIn("obfs_password", active)

    def test_missing_fields_use_conditional_transport_shape(self) -> None:
        values = {
            "name": "Edge",
            "server": "edge.example.com",
            "port": "443",
            "uuid": "id",
            "network": "tcp",
            "security": "none",
        }
        missing = missing_structured_field_labels(
            ["vmess"],
            multiprotocol=False,
            field_value=lambda _protocol, key: values.get(key, ""),
            field_label=lambda key: key,
            protocol_label=lambda protocol: protocol,
        )

        self.assertNotIn("host", missing)
        self.assertNotIn("path", missing)
        self.assertNotIn("sni", missing)

    def test_missing_structured_field_labels_prefixes_protocol_specific_multiprotocol_fields(self) -> None:
        values = {
            ("ss", "name"): "Edge",
            ("ss", "server"): "edge.example.com",
            ("ss", "port"): "443",
            ("ss", "method"): "aes-256-gcm",
            ("wireguard", "name"): "Edge",
            ("wireguard", "server"): "wg.example.com",
            ("wireguard", "port"): "51820",
            ("wireguard", "private_key"): "private",
        }

        missing = missing_structured_field_labels(
            ["ss", "wireguard"],
            multiprotocol=True,
            field_value=lambda protocol, key: values.get((protocol, key), ""),
            field_label=lambda key: {"password": "Password", "public_key": "Public Key"}.get(key, key),
            protocol_label=lambda protocol: {"ss": "Shadowsocks", "wireguard": "WireGuard"}[protocol],
        )

        self.assertIn("Shadowsocks Password", missing)
        self.assertIn("WireGuard Public Key", missing)
        self.assertNotIn("WireGuard name", missing)
        self.assertNotIn("WireGuard server", missing)

    def test_missing_common_fields_are_reported_once_without_protocol_prefixes(self) -> None:
        values = {
            ("ss", "port"): "8388",
            ("ss", "method"): "aes-256-gcm",
            ("ss", "password"): "secret",
            ("wireguard", "port"): "51820",
            ("wireguard", "private_key"): "private",
            ("wireguard", "public_key"): "public",
            ("wireguard", "local_address"): "10.0.0.2/32",
        }

        missing = missing_structured_field_labels(
            ["ss", "wireguard"],
            multiprotocol=True,
            field_value=lambda protocol, key: values.get((protocol, key), ""),
            field_label=lambda key: {"name": "Name", "server": "Server"}.get(key, key),
            protocol_label=lambda protocol: protocol,
        )

        self.assertEqual(missing.count("Name"), 1)
        self.assertEqual(missing.count("Server"), 1)
        self.assertNotIn("ss Name", missing)
        self.assertNotIn("wireguard Server", missing)

    def test_structured_port_values_preserve_single_and_multiprotocol_sources(self) -> None:
        self.assertEqual(
            structured_port_values(
                ["ss", "wireguard"],
                multiprotocol=False,
                shared_port=lambda: "443",
                protocol_port=lambda protocol: {"ss": "8388", "wireguard": "51820"}[protocol],
            ),
            [("ss:port", "8388"), ("wireguard:port", "51820")],
        )
        self.assertEqual(
            structured_port_values(
                ["ss", "wireguard"],
                multiprotocol=True,
                shared_port=lambda: "443",
                protocol_port=lambda protocol: {"ss": "", "wireguard": "51820"}[protocol],
            ),
            [("ss:port", ""), ("wireguard:port", "51820")],
        )

    def test_first_invalid_port_value_ignores_empty_values_and_returns_first_bad_port(self) -> None:
        self.assertIsNone(first_invalid_port_value([("shared", ""), ("ss", "443"), ("wg", "65535")]))
        self.assertEqual(first_invalid_port_value([("shared", ""), ("ss", "bad"), ("wg", "70000")]), ("ss", "bad"))
        self.assertEqual(first_invalid_port_value([("ss", "0")]), ("ss", "0"))

    def test_structured_import_validation_message_preserves_error_order(self) -> None:
        def field_value(protocol: str, key: str) -> str:
            values = {
                ("ss", "name"): "Edge",
                ("ss", "server"): "edge.example",
                ("ss", "port"): "8388",
                ("ss", "method"): "aes-256-gcm",
                ("ss", "password"): "secret",
                ("wireguard", "name"): "Edge",
                ("wireguard", "server"): "wg.example",
                ("wireguard", "port"): "51820",
                ("wireguard", "private_key"): "private",
                ("wireguard", "public_key"): "public",
            }
            return values.get((protocol, key), "")

        common = {
            "priority_value": lambda protocol: {"ss": 1, "wireguard": 2}[protocol],
            "field_value": field_value,
            "field_label": lambda key: {"local_address": "Local Address", "method": "Method"}.get(key, key),
            "protocol_label": lambda protocol: {"ss": "Shadowsocks", "wireguard": "WireGuard"}[protocol],
            "shared_port": lambda: "443",
            "protocol_port": lambda protocol: {"ss": "8388", "wireguard": "bad"}[protocol],
        }

        self.assertEqual(
            structured_import_validation_message([], multiprotocol=False, **common),
            "至少选择一个协议。",
        )
        self.assertEqual(
            structured_import_validation_message(["ss"], multiprotocol=True, **common),
            None,
        )
        self.assertIn(
            "WireGuard Local Address",
            structured_import_validation_message(["ss", "wireguard"], multiprotocol=True, **common) or "",
        )

        def complete_field_value(protocol: str, key: str) -> str:
            if (protocol, key) == ("ss", "method"):
                return "aes-256-gcm"
            if (protocol, key) == ("ss", "password"):
                return "secret"
            return "value"

        complete = {
            **common,
            "field_value": complete_field_value,
        }
        self.assertEqual(
            structured_import_validation_message(["ss", "wireguard"], multiprotocol=True, **complete),
            "端口必须是 1-65535 的整数。",
        )

        duplicate_priority_complete = {
            **complete,
            "priority_value": lambda _protocol: 1,
            "protocol_port": lambda _protocol: "443",
        }
        self.assertIsNone(
            structured_import_validation_message(
                ["ss", "wireguard"],
                multiprotocol=True,
                **duplicate_priority_complete,
            )
        )


if __name__ == "__main__":
    unittest.main()
