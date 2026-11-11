import base64
import json
import unittest
from urllib.parse import parse_qs, urlparse

from structured_node_uri import build_structured_node_uri


class StructuredNodeUriTests(unittest.TestCase):
    def _build(self, protocol: str, values: dict[str, object] | None = None) -> str:
        field_values = values or {}
        return build_structured_node_uri(protocol, lambda key, default="": str(field_values.get(key, default)))

    def test_vless_reality_uri_uses_protocol_specific_transport_fields(self) -> None:
        uri = self._build(
            "vless_reality",
            {
                "name": "Node One",
                "server": "edge.example.com",
                "port": "8443",
                "uuid": "11111111-1111-1111-1111-111111111111",
                "security": "reality",
                "network": "ws",
                "public_key": "PUBLIC",
                "short_id": "abcd",
                "sni": "www.example.com",
                "host": "cdn.example.com",
                "path": "/ray",
                "skip_cert_verify": "true",
            },
        )

        parsed = urlparse(uri)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, "vless")
        self.assertEqual(parsed.hostname, "edge.example.com")
        self.assertEqual(parsed.port, 8443)
        self.assertEqual(query["security"], ["reality"])
        self.assertEqual(query["type"], ["ws"])
        self.assertEqual(query["pbk"], ["PUBLIC"])
        self.assertEqual(query["sid"], ["abcd"])
        self.assertEqual(query["host"], ["cdn.example.com"])
        self.assertEqual(query["path"], ["/ray"])
        self.assertEqual(query["allowInsecure"], ["1"])
        self.assertEqual(parsed.fragment, "Node%20One")

    def test_vmess_uri_encodes_payload_without_ui_dependency(self) -> None:
        uri = self._build(
            "vmess",
            {
                "name": "VMess",
                "server": "vmess.example.com",
                "port": "443",
                "uuid": "22222222-2222-2222-2222-222222222222",
                "network": "ws",
                "host": "host.example.com",
                "path": "/ws",
                "sni": "sni.example.com",
                "alpn": "h2",
            },
        )

        payload = json.loads(base64.b64decode(uri.removeprefix("vmess://")).decode("utf-8"))
        self.assertEqual(payload["ps"], "VMess")
        self.assertEqual(payload["add"], "vmess.example.com")
        self.assertEqual(payload["id"], "22222222-2222-2222-2222-222222222222")
        self.assertEqual(payload["net"], "ws")
        self.assertEqual(payload["host"], "host.example.com")
        self.assertEqual(payload["path"], "/ws")
        self.assertEqual(payload["sni"], "sni.example.com")
        self.assertEqual(payload["alpn"], "h2")

    def test_vmess_grpc_maps_service_name_and_ignores_inactive_transport_drafts(self) -> None:
        uri = self._build(
            "vmess",
            {
                "network": "grpc",
                "grpc_service_name": "edge-service",
                "host": "stale-host.example",
                "path": "/stale-path",
            },
        )

        payload = json.loads(base64.b64decode(uri.removeprefix("vmess://")).decode("utf-8"))
        self.assertEqual(payload["net"], "grpc")
        self.assertEqual(payload["path"], "edge-service")
        self.assertEqual(payload["serviceName"], "edge-service")
        self.assertNotIn("host", payload)

        plain_payload = json.loads(
            base64.b64decode(
                self._build(
                    "vmess",
                    {
                        "network": "tcp",
                        "security": "none",
                        "sni": "stale-sni.example",
                        "host": "stale-host.example",
                        "path": "/stale-path",
                    },
                ).removeprefix("vmess://")
            ).decode("utf-8")
        )
        self.assertNotIn("sni", plain_payload)
        self.assertNotIn("host", plain_payload)
        self.assertNotIn("path", plain_payload)

    def test_inactive_vless_security_and_transport_fields_are_not_serialized(self) -> None:
        uri = self._build(
            "vless_reality",
            {
                "security": "tls",
                "network": "tcp",
                "public_key": "STALE-PUBLIC",
                "short_id": "STALE-SID",
                "host": "stale.example",
                "path": "/stale",
            },
        )

        query = parse_qs(urlparse(uri).query)
        self.assertNotIn("pbk", query)
        self.assertNotIn("sid", query)
        self.assertNotIn("host", query)
        self.assertNotIn("path", query)

    def test_common_protocol_builders_preserve_existing_defaults(self) -> None:
        self.assertEqual(self._build("ss"), "ss://aes-256-gcm:PASSWORD@example.com:443#My-Node")
        self.assertTrue(self._build("trojan").startswith("trojan://PASSWORD@example.com:443?"))
        self.assertTrue(self._build("anytls").startswith("anytls://PASSWORD@example.com:443?"))
        self.assertEqual(
            self._build("naive"),
            "naive://user:PASSWORD@example.com:443?security=tls#My-Node",
        )
        self.assertEqual(self._build("socks5"), "socks5://example.com:443#My-Node")
        self.assertTrue(self._build("hysteria2").startswith("hysteria2://PASSWORD@example.com:443?"))
        self.assertTrue(self._build("tuic").startswith("tuic://11111111-1111-1111-1111-111111111111:PASSWORD@example.com:443?"))
        self.assertTrue(self._build("wireguard").startswith("wireguard://PRIVATE_KEY@example.com:443?"))

    def test_build_naive_preserves_plain_http_security_choice(self) -> None:
        self.assertEqual(
            self._build("naive", {"security": "none"}),
            "naive://user:PASSWORD@example.com:443?security=none#My-Node",
        )

    def test_naive_tls_serializes_sni_but_plain_security_ignores_stale_sni(self) -> None:
        tls_query = parse_qs(
            urlparse(self._build("naive", {"security": "tls", "sni": "tls.example"})).query
        )
        plain_query = parse_qs(
            urlparse(self._build("naive", {"security": "none", "sni": "stale.example"})).query
        )

        self.assertEqual(tls_query["sni"], ["tls.example"])
        self.assertNotIn("sni", plain_query)

    def test_hysteria2_ignores_obfs_password_when_obfs_is_inactive(self) -> None:
        query = parse_qs(
            urlparse(self._build("hysteria2", {"obfs": "", "obfs_password": "stale"})).query
        )

        self.assertNotIn("obfs-password", query)

    def test_unsupported_protocol_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._build("unknown")


if __name__ == "__main__":
    unittest.main()
