"""Tests for TUN / DNS block emission in build_full_clash_config."""
from __future__ import annotations

import unittest

import yaml

from clash_formatter import build_full_clash_config
from dns_resolver_policy import rendered_dns_uses_hostnames
from models import Node
from parser import parse_node_uri


def _parse(config_yaml: str) -> dict:
    return yaml.safe_load(config_yaml)


def _sample_tier1_node():
    node = parse_node_uri(
        "vless://11111111-1111-1111-1111-111111111111@example.com:443"
        "?encryption=none&flow=xtls-rprx-vision&security=reality"
        "&type=tcp&pbk=test-public-key&sid=abcd&sni=www.microsoft.com&fp=chrome#VPS",
        source="test",
        tier="tier1",
    )
    assert node is not None
    return node


def _sample_tier3_domain_node() -> Node:
    return Node(
        name="Tier3-Domain",
        protocol="ss",
        uri="ss://tier3-domain",
        address="tier3.example.com",
        port=8388,
        method="aes-128-gcm",
        password="pass",
        tier="tier3",
    )


class TunAndDnsEmissionTests(unittest.TestCase):
    def test_system_proxy_mode_emits_no_tun_block(self) -> None:
        parsed = _parse(
            build_full_clash_config(
                {"tier1": [_sample_tier1_node()], "tier2": [], "tier3": []},
                network_mode="system_proxy",
            )
        )
        self.assertNotIn("tun", parsed)

    def test_system_proxy_mode_emits_dns_for_proxy_server_resolution(self) -> None:
        parsed = _parse(
            build_full_clash_config(
                {"tier1": [_sample_tier1_node()], "tier2": [], "tier3": []},
                network_mode="system_proxy",
            )
        )
        # DNS is always emitted so mihomo can resolve proxy-server hostnames
        # through explicit startup-safe resolvers.
        self.assertIn("dns", parsed)
        self.assertTrue(parsed["dns"]["enable"])
        # The static default is a region-neutral pool of IP-literal encrypted
        # resolvers, so no plaintext bootstrap lookup is ever needed.
        self.assertEqual(parsed["dns"]["default-nameserver"], ["system"])
        self.assertEqual(len(parsed["dns"]["nameserver"]), 5)
        self.assertEqual(parsed["dns"]["proxy-server-nameserver"], parsed["dns"]["nameserver"])
        self.assertFalse(rendered_dns_uses_hostnames(parsed["dns"]))
        self.assertNotIn("223.5.5.5", parsed["dns"]["default-nameserver"])

    def test_tier3_only_system_proxy_routes_through_tier3_with_dns(self) -> None:
        parsed = _parse(
            build_full_clash_config(
                {"tier1": [], "tier2": [], "tier3": [_sample_tier3_domain_node()], "exit": []},
                network_mode="system_proxy",
            )
        )

        groups = {group["name"]: group for group in parsed["proxy-groups"]}
        self.assertEqual(groups["TIER3-AUTO"]["proxies"], ["Tier3-Domain"])
        self.assertEqual(groups["PROXY-FALLBACK"]["proxies"], ["TIER3-AUTO"])
        self.assertFalse(groups["TIER3-AUTO"]["lazy"])
        self.assertFalse(groups["PROXY-FALLBACK"]["lazy"])
        self.assertEqual(parsed["proxies"][0]["server"], "tier3.example.com")
        self.assertEqual(parsed["dns"]["proxy-server-nameserver"], parsed["dns"]["nameserver"])
        self.assertTrue(all(url.startswith("https://") for url in parsed["dns"]["proxy-server-nameserver"]))
        self.assertFalse(rendered_dns_uses_hostnames(parsed["dns"]))

    def test_tier3_health_check_stays_lazy_when_primary_layers_exist(self) -> None:
        parsed = _parse(
            build_full_clash_config(
                {"tier1": [_sample_tier1_node()], "tier2": [], "tier3": [_sample_tier3_domain_node()], "exit": []},
                network_mode="system_proxy",
            )
        )

        groups = {group["name"]: group for group in parsed["proxy-groups"]}
        self.assertTrue(groups["TIER3-AUTO"]["lazy"])
        self.assertTrue(groups["PROXY-FALLBACK"]["lazy"])

    def test_explicit_proxy_server_nameservers_are_honoured_verbatim(self) -> None:
        parsed = _parse(
            build_full_clash_config(
                {"tier1": [_sample_tier1_node()], "tier2": [], "tier3": []},
                network_mode="system_proxy",
                dns_config={
                    "enable": True,
                    "proxy_server_nameserver": [
                        "tls://9.9.9.9",
                        "https://1.0.0.1/dns-query",
                    ],
                },
            )
        )

        # Explicit lists are user policy (custom mode); nothing rewrites them.
        self.assertEqual(
            parsed["dns"]["proxy-server-nameserver"],
            ["tls://9.9.9.9", "https://1.0.0.1/dns-query"],
        )

    def test_historical_shipped_proxy_server_pair_is_a_default_not_user_policy(self) -> None:
        # The Cloudflare/Google DoH pair shipped in early configs and was
        # silently rewritten by the old renderer, so it never was a choice.
        parsed = _parse(
            build_full_clash_config(
                {"tier1": [_sample_tier1_node()], "tier2": [], "tier3": []},
                network_mode="system_proxy",
                dns_config={
                    "enable": True,
                    "nameserver": ["https://doh.pub/dns-query", "https://dns.alidns.com/dns-query"],
                    "default_nameserver": ["223.5.5.5", "119.29.29.29", "114.114.114.114"],
                    "proxy_server_nameserver": [
                        "https://1.1.1.1/dns-query",
                        "https://8.8.8.8/dns-query",
                    ],
                },
            )
        )
        self.assertEqual(parsed["dns"]["default-nameserver"], ["system"])
        self.assertNotIn("https://doh.pub/dns-query", parsed["dns"]["nameserver"])
        self.assertEqual(
            parsed["dns"]["proxy-server-nameserver"], parsed["dns"]["nameserver"],
        )

    def test_system_proxy_mode_still_honors_explicit_dns(self) -> None:
        parsed = _parse(
            build_full_clash_config(
                {"tier1": [_sample_tier1_node()], "tier2": [], "tier3": []},
                network_mode="system_proxy",
                dns_config={"enable": True, "nameserver": ["https://doh.pub/dns-query"]},
            )
        )
        self.assertIn("dns", parsed)
        self.assertEqual(parsed["dns"]["nameserver"], ["https://doh.pub/dns-query"])

    def test_tun_mode_emits_tun_block_and_auto_enables_dns(self) -> None:
        parsed = _parse(
            build_full_clash_config(
                {"tier1": [_sample_tier1_node()], "tier2": [], "tier3": []},
                network_mode="tun",
                tun_config={"stack": "mixed", "device": "Tesseract"},
            )
        )
        self.assertIn("tun", parsed)
        self.assertTrue(parsed["tun"]["enable"])
        self.assertEqual(parsed["tun"]["stack"], "mixed")
        self.assertEqual(parsed["tun"]["device"], "Tesseract")
        self.assertTrue(parsed["tun"]["auto-route"])
        self.assertTrue(parsed["tun"]["auto-detect-interface"])
        # DNS must be present in TUN mode even without explicit dns_config.
        self.assertIn("dns", parsed)
        self.assertEqual(parsed["dns"]["enhanced-mode"], "fake-ip")
        self.assertIn("fake-ip-range", parsed["dns"])
        # Sniffer is required for TLS/HTTP rules to work under fake-ip.
        self.assertIn("sniffer", parsed)
        self.assertTrue(parsed["sniffer"]["enable"])

    def test_mixed_mode_emits_tun_block(self) -> None:
        parsed = _parse(
            build_full_clash_config(
                {"tier1": [_sample_tier1_node()], "tier2": [], "tier3": []},
                network_mode="mixed",
            )
        )
        self.assertIn("tun", parsed)
        self.assertTrue(parsed["tun"]["enable"])

    def test_tun_mode_respects_custom_dns_upstream(self) -> None:
        parsed = _parse(
            build_full_clash_config(
                {"tier1": [_sample_tier1_node()], "tier2": [], "tier3": []},
                network_mode="tun",
                dns_config={
                    "enable": True,
                    "nameserver": ["https://1.12.12.12/dns-query"],
                    "proxy_server_nameserver": ["https://1.1.1.1/dns-query"],
                    "fake_ip_range": "28.0.0.1/8",
                },
            )
        )
        self.assertEqual(parsed["dns"]["nameserver"], ["https://1.12.12.12/dns-query"])
        self.assertEqual(parsed["dns"]["proxy-server-nameserver"], ["https://1.1.1.1/dns-query"])
        self.assertEqual(parsed["dns"]["fake-ip-range"], "28.0.0.1/8")

    def test_dns_disabled_flag_is_honored(self) -> None:
        parsed = _parse(
            build_full_clash_config(
                {"tier1": [_sample_tier1_node()], "tier2": [], "tier3": []},
                network_mode="system_proxy",
                dns_config={"enable": False, "nameserver": ["https://doh.pub/dns-query"]},
            )
        )
        self.assertNotIn("dns", parsed)

    def test_redir_host_mode_omits_fake_ip_range(self) -> None:
        parsed = _parse(
            build_full_clash_config(
                {"tier1": [_sample_tier1_node()], "tier2": [], "tier3": []},
                network_mode="tun",
                dns_config={"enable": True, "enhanced_mode": "redir-host"},
            )
        )
        self.assertEqual(parsed["dns"]["enhanced-mode"], "redir-host")
        self.assertNotIn("fake-ip-range", parsed["dns"])


if __name__ == "__main__":
    unittest.main()
