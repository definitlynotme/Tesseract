import copy
import json
import unittest
from pathlib import Path

import yaml

from clash_formatter import (
    EXIT_QUALITY_SYSTEM_HOSTS,
    LOCAL_NETWORK_SYSTEM_RULES,
    build_bootstrap_clash_config,
    build_capture_disabled_clash_config,
    build_clash_provider_payload,
    build_final_exit_system_rules,
    build_full_clash_config,
    inject_runtime_source_listeners,
    node_to_clash_proxy,
)
from parser import parse_node_uri
from models import Node
from tier1_protocols import tier1_auto_group_name, tier1_proxy_name
from runtime_source_transport import (
    RUNTIME_SOURCE_EGRESS_PROXY_NAME,
    RuntimeSourceListenerCredentials,
    RuntimeSourceListenerPorts,
    RuntimeSourceRouteKind,
    build_runtime_source_transport_generation,
)


def _runtime_source_generation(*, include_egress: bool = True):
    credentials = {
        route: RuntimeSourceListenerCredentials(
            username=f"{route.value}-user",
            password=f"{route.value}-password",
        )
        for route in RuntimeSourceRouteKind
    }
    return build_runtime_source_transport_generation(
        runtime_generation="runtime-test",
        config_generation=4,
        ports=RuntimeSourceListenerPorts(
            final_exit=19101,
            egress=19102 if include_egress else None,
            direct=19103,
        ),
        egress_proxy_name=(
            RUNTIME_SOURCE_EGRESS_PROXY_NAME if include_egress else None
        ),
        credentials=credentials,
    )


class ClashFormatterTests(unittest.TestCase):
    def test_bootstrap_config_disables_capture_and_appends_explicit_egress(self) -> None:
        base = build_full_clash_config(
            {"tier1": [], "tier2": [], "tier3": [], "exit": []},
            network_mode="mixed",
            allow_lan=True,
        )

        parsed = yaml.safe_load(
            build_bootstrap_clash_config(
                base,
                egress_proxy={
                    "enabled": True,
                    "scheme": "https",
                    "server": "bootstrap-proxy.example",
                    "port": 8443,
                    "username": "user",
                    "password": "secret",
                    "use_for": {"collection": True},
                },
            )
        )

        self.assertNotIn("tun", parsed)
        self.assertNotIn("sniffer", parsed)
        self.assertFalse(parsed["allow-lan"])
        explicit = next(
            proxy
            for proxy in parsed["proxies"]
            if proxy["name"] == "BOOTSTRAP-EGRESS-PROXY"
        )
        self.assertEqual(explicit["type"], "http")
        self.assertTrue(explicit["tls"])
        self.assertEqual(explicit["username"], "user")
        fallback = next(
            group
            for group in parsed["proxy-groups"]
            if group["name"] == "PROXY-FALLBACK"
        )
        self.assertEqual(fallback["proxies"], ["BOOTSTRAP-EGRESS-PROXY"])
        self.assertTrue(
            all(
                group.get("lazy") is False
                for group in parsed["proxy-groups"]
                if group.get("type") in {"fallback", "url-test"}
            )
        )
        self.assertTrue(
            all(
                group.get("empty-fallback") == "REJECT"
                for group in parsed["proxy-groups"]
            )
        )
        self.assertFalse(
            any(
                str(candidate).upper() in {"DIRECT", "COMPATIBLE"}
                for group in parsed["proxy-groups"]
                for candidate in group.get("proxies", [])
            )
        )

    def test_bootstrap_egress_disabled_for_collection_is_not_compiled(self) -> None:
        base = build_full_clash_config(
            {"tier1": [], "tier2": [], "tier3": [], "exit": []}
        )

        parsed = yaml.safe_load(
            build_bootstrap_clash_config(
                base,
                egress_proxy={
                    "enabled": True,
                    "scheme": "socks5",
                    "server": "127.0.0.1",
                    "port": 1080,
                    "use_for": {"collection": False},
                },
            )
        )

        self.assertFalse(
            any(
                proxy.get("name") == "BOOTSTRAP-EGRESS-PROXY"
                for proxy in parsed["proxies"]
            )
        )
        fallback = next(
            group
            for group in parsed["proxy-groups"]
            if group["name"] == "PROXY-FALLBACK"
        )
        self.assertEqual(fallback["proxies"], ["REJECT"])

    def test_bootstrap_invalid_explicit_egress_fails_closed(self) -> None:
        base = build_full_clash_config(
            {"tier1": [], "tier2": [], "tier3": [], "exit": []}
        )

        with self.assertRaisesRegex(ValueError, "scheme"):
            build_bootstrap_clash_config(
                base,
                egress_proxy={
                    "enabled": True,
                    "scheme": "ftp",
                    "server": "proxy.example",
                    "port": 21,
                    "use_for": {"collection": True},
                },
            )

    def test_runtime_source_listeners_are_runtime_only_and_do_not_change_mixed_routes(self) -> None:
        generation = _runtime_source_generation()
        tier_nodes = {"tier1": [], "tier2": [], "tier3": [], "exit": []}
        baseline = yaml.safe_load(
            build_full_clash_config(
                tier_nodes,
                mixed_port=17890,
                routing_config={
                    "rules": {
                        "direct": ["suffix:github.com"],
                        "reject": ["blocked.example"],
                    }
                },
            )
        )
        parsed = yaml.safe_load(
            build_full_clash_config(
                tier_nodes,
                mixed_port=17890,
                routing_config={
                    "rules": {
                        "direct": ["suffix:github.com"],
                        "reject": ["blocked.example"],
                    }
                },
                runtime_source_listeners=generation.listeners,
                runtime_source_egress_proxy={
                    "enabled": True,
                    "scheme": "socks5",
                    "server": "127.0.0.1",
                    "port": 1080,
                    "use_for": {"collection": True},
                },
            )
        )

        self.assertEqual(parsed["mixed-port"], baseline["mixed-port"])
        self.assertEqual(parsed["rules"], baseline["rules"])
        self.assertEqual(parsed["proxy-groups"], baseline["proxy-groups"])
        listeners = {listener["name"]: listener for listener in parsed["listeners"]}
        self.assertEqual(
            list(listeners),
            ["source-final-exit", "source-egress", "source-direct"],
        )
        self.assertEqual(listeners["source-final-exit"]["proxy"], "FINAL-EXIT")
        self.assertEqual(
            listeners["source-egress"]["proxy"],
            RUNTIME_SOURCE_EGRESS_PROXY_NAME,
        )
        self.assertEqual(listeners["source-direct"]["proxy"], "DIRECT")
        for listener in listeners.values():
            self.assertEqual(listener["listen"], "127.0.0.1")
            self.assertEqual(listener["type"], "http")
            self.assertEqual(len(listener["users"]), 1)
        runtime_egress = next(
            proxy
            for proxy in parsed["proxies"]
            if proxy.get("name") == RUNTIME_SOURCE_EGRESS_PROXY_NAME
        )
        self.assertEqual(runtime_egress["type"], "socks5")
        self.assertFalse(
            any(
                RUNTIME_SOURCE_EGRESS_PROXY_NAME in group.get("proxies", [])
                for group in parsed["proxy-groups"]
            )
        )

    def test_default_full_config_does_not_export_private_runtime_listeners(self) -> None:
        parsed = yaml.safe_load(
            build_full_clash_config(
                {"tier1": [], "tier2": [], "tier3": [], "exit": []}
            )
        )

        self.assertNotIn("listeners", parsed)
        self.assertFalse(
            any(
                proxy.get("name") == RUNTIME_SOURCE_EGRESS_PROXY_NAME
                for proxy in parsed["proxies"]
            )
        )

    def test_bootstrap_injects_private_listeners_without_exposing_runtime_egress_to_groups(self) -> None:
        generation = _runtime_source_generation()
        base = build_full_clash_config(
            {"tier1": [], "tier2": [], "tier3": [], "exit": []},
            network_mode="mixed",
        )
        egress_proxy = {
            "enabled": True,
            "scheme": "http",
            "server": "bootstrap-proxy.example",
            "port": 8080,
            "use_for": {"collection": True},
        }

        parsed = yaml.safe_load(
            build_bootstrap_clash_config(
                base,
                egress_proxy=egress_proxy,
                runtime_source_listeners=generation.listeners,
            )
        )

        listeners = {listener["name"]: listener for listener in parsed["listeners"]}
        self.assertEqual(
            listeners["source-egress"]["proxy"],
            RUNTIME_SOURCE_EGRESS_PROXY_NAME,
        )
        self.assertTrue(
            any(
                proxy.get("name") == RUNTIME_SOURCE_EGRESS_PROXY_NAME
                for proxy in parsed["proxies"]
            )
        )
        self.assertFalse(
            any(
                RUNTIME_SOURCE_EGRESS_PROXY_NAME in group.get("proxies", [])
                for group in parsed["proxy-groups"]
            )
        )
        fallback = next(
            group
            for group in parsed["proxy-groups"]
            if group["name"] == "PROXY-FALLBACK"
        )
        self.assertEqual(fallback["proxies"], ["BOOTSTRAP-EGRESS-PROXY"])

    def test_capture_disabled_keeps_dns_and_private_listener_contract(self) -> None:
        generation = _runtime_source_generation(include_egress=False)
        formal = yaml.safe_load(
            build_full_clash_config(
                {"tier1": [], "tier2": [], "tier3": [], "exit": []},
                network_mode="tun",
                dns_config={
                    "enable": True,
                    "listen": "127.0.0.1:1053",
                    "nameserver": ["1.1.1.1"],
                },
                runtime_source_listeners=generation.listeners,
            )
        )

        no_capture = yaml.safe_load(build_capture_disabled_clash_config(formal))

        self.assertEqual(no_capture["dns"], formal["dns"])
        self.assertEqual(no_capture["listeners"], formal["listeners"])
        self.assertNotIn("tun", no_capture)
        self.assertNotIn("sniffer", no_capture)

    def test_bootstrap_shares_the_formal_dns_block_verbatim(self) -> None:
        generation = _runtime_source_generation(include_egress=False)
        formal_text = build_full_clash_config(
            {"tier1": [], "tier2": [], "tier3": [], "exit": []},
            network_mode="tun",
            dns_config={
                "enable": True,
                "listen": "127.0.0.1:1053",
                "nameserver": ["https://resolver.example/dns-query"],
                "default_nameserver": ["192.0.2.53"],
            },
            runtime_source_listeners=generation.listeners,
            routing_config={"rules": {"reject": ["blocked.example"]}},
        )
        formal = yaml.safe_load(formal_text)
        bootstrap = yaml.safe_load(build_bootstrap_clash_config(formal))

        # The resolver rung is rendered into the formal payload once per run;
        # the bootstrap Core must resolve exactly like the formal Core.
        self.assertEqual(bootstrap["dns"], formal["dns"])
        self.assertTrue(bootstrap["dns"]["enable"])
        self.assertEqual(formal, yaml.safe_load(formal_text))
        expected_listeners = copy.deepcopy(formal["listeners"])
        for listener in expected_listeners:
            if listener["name"] == "source-final-exit":
                listener["proxy"] = "PROXY-FALLBACK"
        self.assertEqual(bootstrap["listeners"], expected_listeners)
        self.assertEqual(bootstrap["rules"], formal["rules"])
        self.assertNotIn("tun", bootstrap)
        self.assertFalse(bootstrap["allow-lan"])

        # Promotion starts from the formal renderer, never the bootstrap DNS.
        standby = yaml.safe_load(build_capture_disabled_clash_config(formal_text))
        self.assertEqual(standby["dns"], formal["dns"])

    def test_bootstrap_without_dns_does_not_invent_a_dns_listener(self) -> None:
        formal = yaml.safe_load(build_full_clash_config(
            {"tier1": [], "tier2": [], "tier3": [], "exit": []},
        ))
        formal.pop("dns", None)
        bootstrap = yaml.safe_load(build_bootstrap_clash_config(formal))
        self.assertNotIn("dns", bootstrap)

    def test_injection_replaces_only_reserved_private_listener_names(self) -> None:
        generation = _runtime_source_generation(include_egress=False)
        base = yaml.safe_load(
            build_full_clash_config(
                {"tier1": [], "tier2": [], "tier3": [], "exit": []}
            )
        )
        base["listeners"] = [
            {
                "name": "source-custom-user-listener",
                "type": "http",
                "listen": "127.0.0.1",
                "port": 19990,
                "proxy": "DIRECT",
                "users": [],
            },
            {
                "name": "source-direct",
                "type": "http",
                "listen": "127.0.0.1",
                "port": 19991,
                "proxy": "DIRECT",
                "users": [],
            },
        ]

        parsed = yaml.safe_load(
            inject_runtime_source_listeners(base, generation.listeners)
        )

        listeners = {listener["name"]: listener for listener in parsed["listeners"]}
        self.assertIn("source-custom-user-listener", listeners)
        self.assertEqual(listeners["source-direct"]["port"], 19103)
        self.assertEqual(len(listeners), 3)

    def test_egress_listener_without_matching_outbound_fails_closed(self) -> None:
        generation = _runtime_source_generation()
        base = build_full_clash_config(
            {"tier1": [], "tier2": [], "tier3": [], "exit": []}
        )

        with self.assertRaisesRegex(ValueError, "requires an enabled"):
            inject_runtime_source_listeners(base, generation.listeners)

    def test_capture_disabled_config_keeps_rules_and_nodes_unchanged(self) -> None:
        base = yaml.safe_load(
            build_full_clash_config(
                {"tier1": [], "tier2": [], "tier3": [], "exit": []},
                network_mode="tun",
                routing_config={"rules": {"direct": ["example.test"]}},
            )
        )

        parsed = yaml.safe_load(build_capture_disabled_clash_config(base))

        self.assertEqual(parsed["proxies"], base["proxies"])
        expected_groups = copy.deepcopy(base["proxy-groups"])
        for group in expected_groups:
            if str(group.get("type") or "").lower() in {"fallback", "url-test"}:
                group["lazy"] = False
        self.assertEqual(parsed["proxy-groups"], expected_groups)
        self.assertTrue(
            any(
                group.get("type") == "fallback" and group.get("lazy") is True
                for group in base["proxy-groups"]
            )
        )
        self.assertTrue(
            all(
                group.get("lazy") is False
                for group in parsed["proxy-groups"]
                if group.get("type") in {"fallback", "url-test"}
            )
        )
        self.assertEqual(parsed["rules"], base["rules"])
        self.assertNotIn("tun", parsed)
        self.assertNotIn("sniffer", parsed)

    def test_vless_reality_converts_for_mihomo(self) -> None:
        uri = (
            "vless://11111111-1111-1111-1111-111111111111@example.com:443"
            "?encryption=none&flow=xtls-rprx-vision&security=reality"
            "&type=tcp&pbk=test-public-key&sid=abcd&sni=www.microsoft.com&fp=chrome#Example-VPS"
        )
        node = parse_node_uri(uri, source="test", tier="tier1")
        assert node is not None
        proxy = node_to_clash_proxy(node)
        assert proxy is not None
        self.assertEqual(proxy["type"], "vless")
        self.assertEqual(proxy["uuid"], "11111111-1111-1111-1111-111111111111")
        self.assertEqual(proxy["flow"], "xtls-rprx-vision")
        self.assertEqual(proxy["servername"], "www.microsoft.com")
        self.assertEqual(proxy["client-fingerprint"], "chrome")
        self.assertEqual(proxy["reality-opts"]["public-key"], "test-public-key")
        self.assertEqual(proxy["reality-opts"]["short-id"], "abcd")

    def test_vless_allow_insecure_maps_to_mihomo_skip_cert_verify(self) -> None:
        uri = (
            "vless://11111111-1111-1111-1111-111111111111@example.com:443"
            "?encryption=none&security=tls&type=ws&host=edge.example.com"
            "&sni=edge.example.com&allowInsecure=1#Edge"
        )
        node = parse_node_uri(uri, source="test", tier="tier3")
        assert node is not None
        proxy = node_to_clash_proxy(node)
        assert proxy is not None
        self.assertTrue(proxy["skip-cert-verify"])

    def test_string_false_skip_cert_verify_stays_disabled(self) -> None:
        node = Node(
            name="strict-http",
            protocol="http",
            address="example.com",
            port=443,
            password="pass",
            tls=True,
            extra={"username": "user", "skip-cert-verify": "false"},
        )

        proxy = node_to_clash_proxy(node)

        assert proxy is not None
        self.assertFalse(proxy["skip-cert-verify"])

    def test_ssr_proxy_is_emitted(self) -> None:
        uri = "ssr://ZXhhbXBsZS5jb206NDQzOmF1dGhfc2hhMV92NDphZXMtMTI4LWNmYjp0bHMxLjJfdGlja2V0X2F1dGg6Y0dGemN3Lz9yZW1hcmtzPVpHVnRiZw"
        node = parse_node_uri(uri, source="test", tier="tier3")
        assert node is not None
        proxy = node_to_clash_proxy(node)
        assert proxy is not None
        self.assertEqual(proxy["type"], "ssr")
        self.assertEqual(proxy["server"], "example.com")
        self.assertEqual(proxy["cipher"], "aes-128-cfb")
        self.assertEqual(proxy["password"], "pass")
        self.assertEqual(proxy["protocol"], "auth_sha1_v4")
        self.assertEqual(proxy["obfs"], "tls1.2_ticket_auth")

    def test_unknown_protocol_is_skipped(self) -> None:
        node = Node(name="snell", protocol="snell", uri="", address="example.com", port=443, password="pass")
        self.assertIsNone(node_to_clash_proxy(node))

    def test_unreachable_configured_endpoint_reaches_runtime_self_check(self) -> None:
        proxy = node_to_clash_proxy(
            Node(name="1", protocol="socks5", address="1", port=1)
        )

        self.assertIsNotNone(proxy)
        assert proxy is not None
        self.assertEqual(proxy["server"], "1")
        self.assertEqual(proxy["port"], 1)

    def test_full_config_is_valid_yaml(self) -> None:
        tier1_node = parse_node_uri(
            "vless://11111111-1111-1111-1111-111111111111@example.com:443"
            "?encryption=none&flow=xtls-rprx-vision&security=reality"
            "&type=tcp&pbk=test-public-key&sid=abcd&sni=www.microsoft.com&fp=chrome#Example-VPS",
            source="test",
            tier="tier1",
        )
        tier2_node = parse_node_uri(
            "ss://YWVzLTI1Ni1nY206cGFzc0BleGFtcGxlLmNvbTo4Mzg4#JMS-SS",
            source="test",
            tier="tier2",
        )
        assert tier1_node is not None
        assert tier2_node is not None
        payload = build_full_clash_config({"tier1": [tier1_node], "tier2": [tier2_node], "tier3": []})
        parsed = yaml.safe_load(payload)
        self.assertIn("proxies", parsed)
        self.assertIn("proxy-groups", parsed)
        self.assertIn("rules", parsed)
        self.assertTrue(
            all(
                group.get("empty-fallback") == "REJECT"
                for group in parsed["proxy-groups"]
            )
        )
        self.assertTrue(parsed["geodata-mode"])
        self.assertIn("geoip", parsed["geox-url"])
        self.assertEqual(parsed["proxies"][0]["type"], "vless")
        self.assertEqual(parsed["proxies"][1]["type"], "ss")

    def test_tier1_physical_node_fallback_group_uses_saved_order(self) -> None:
        vless = parse_node_uri(
            "vless://11111111-1111-1111-1111-111111111111@edge.example.com:443"
            "?encryption=none&security=reality&pbk=test-key&sid=abcd#vless",
            source="test",
            tier="tier1",
        )
        hysteria2 = parse_node_uri(
            "hysteria2://hy2-pass@edge.example.com:8443#hy2",
            source="test",
            tier="tier1",
        )
        naive = parse_node_uri(
            "naive://user:naive-pass@edge.example.com:443?security=tls#naive",
            source="test",
            tier="tier1",
        )
        assert vless is not None
        assert hysteria2 is not None
        assert naive is not None
        fallback_order = ["hysteria2", "vless_reality", "naive"]
        for node, key, label in (
            (vless, "vless_reality", "VLESS Reality"),
            (naive, "naive", "Naive HTTPS"),
            (hysteria2, "hysteria2", "Hysteria2"),
        ):
            node.extra.update(
                {
                    "physical_node": "Edge VPS",
                    "protocol_key": key,
                    "protocol_label": label,
                    "fallback_order": fallback_order,
                }
            )

        parsed = yaml.safe_load(
            build_full_clash_config(
                {"tier1": [vless, naive, hysteria2], "tier2": [], "tier3": [], "exit": []}
            )
        )
        groups_by_name = {group["name"]: group for group in parsed["proxy-groups"]}

        self.assertEqual(
            groups_by_name[tier1_auto_group_name("Edge VPS")]["proxies"],
            [
                tier1_proxy_name(hysteria2),
                tier1_proxy_name(vless),
                tier1_proxy_name(naive),
            ],
        )
        for group in parsed["proxy-groups"]:
            if group["type"] == "fallback":
                self.assertEqual(group["max-failed-times"], 2)
                # TIER3-AUTO alone re-proves its real-site contract faster.
                self.assertEqual(
                    group["interval"],
                    120 if group["name"] == "TIER3-AUTO" else 300,
                )

    def test_proxy_fallback_does_not_auto_probe_direct_when_proxy_exists(self) -> None:
        tier1_node = parse_node_uri(
            "vless://11111111-1111-1111-1111-111111111111@example.com:443"
            "?encryption=none&security=tls&type=tcp&sni=example.com#Example-VPS",
            source="test",
            tier="tier1",
        )
        assert tier1_node is not None

        parsed = yaml.safe_load(build_full_clash_config({"tier1": [tier1_node], "tier2": [], "tier3": [], "exit": []}))
        groups_by_name = {group["name"]: group for group in parsed["proxy-groups"]}

        self.assertNotIn("DIRECT", groups_by_name["PROXY-FALLBACK"]["proxies"])

    def test_unsupported_shadowsocks_cipher_is_not_exported(self) -> None:
        node = Node(
            name="bad-ss",
            protocol="ss",
            address="example.com",
            port=8388,
            method="bad-cipher",
            password="pass",
            tier="tier3",
        )

        self.assertIsNone(node_to_clash_proxy(node))

    def test_exit_tier_nodes_are_candidates_until_active_exit_is_selected(self) -> None:
        tier1_node = parse_node_uri(
            "vless://11111111-1111-1111-1111-111111111111@relay.example.com:443"
            "?encryption=none&security=tls&type=tcp&sni=relay.example.com#Relay",
            source="test",
            tier="tier1",
        )
        exit_node = parse_node_uri(
            "ss://YWVzLTI1Ni1nY206cGFzc0BleGl0LmV4YW1wbGUuY29tOjgzODg#Exit-SS",
            source="test",
            tier="exit",
        )
        assert tier1_node is not None
        assert exit_node is not None

        parsed = yaml.safe_load(build_full_clash_config({"tier1": [tier1_node], "tier2": [], "tier3": [], "exit": [exit_node]}))
        proxies_by_name = {proxy["name"]: proxy for proxy in parsed["proxies"]}
        groups_by_name = {group["name"]: group for group in parsed["proxy-groups"]}

        self.assertEqual(proxies_by_name["Exit-SS"]["dialer-proxy"], "PROXY-FALLBACK")
        self.assertNotIn("EXIT-AUTO", groups_by_name)
        self.assertNotIn("EXIT-SELECT", groups_by_name)
        self.assertEqual(groups_by_name["FINAL-EXIT"]["proxies"], ["PROXY-FALLBACK", "REJECT"])
        self.assertEqual(groups_by_name["GENERAL"]["proxies"][0], "FINAL-EXIT")

    def test_active_exit_proxy_leads_exit_select_group(self) -> None:
        osaka = parse_node_uri(
            "ss://YWVzLTI1Ni1nY206cGFzc0Bvc2FrYS5leGFtcGxlLmNvbTo4Mzg4#Osaka",
            source="test",
            tier="exit",
        )
        tokyo = parse_node_uri(
            "ss://YWVzLTI1Ni1nY206cGFzc0B0b2t5by5leGFtcGxlLmNvbTo4Mzg4#Tokyo",
            source="test",
            tier="exit",
        )
        assert osaka is not None
        assert tokyo is not None

        parsed = yaml.safe_load(
            build_full_clash_config(
                {"tier1": [], "tier2": [], "tier3": [], "exit": [osaka, tokyo]},
                active_exit_proxy="Tokyo",
            )
        )
        proxies_by_name = {proxy["name"]: proxy for proxy in parsed["proxies"]}
        groups_by_name = {group["name"]: group for group in parsed["proxy-groups"]}

        self.assertEqual(proxies_by_name["Tokyo"]["dialer-proxy"], "PROXY-FALLBACK")
        self.assertNotIn("EXIT-AUTO", groups_by_name)
        self.assertEqual(groups_by_name["EXIT-SELECT"]["proxies"], ["Tokyo"])
        self.assertEqual(groups_by_name["FINAL-EXIT"]["proxies"], ["EXIT-SELECT", "REJECT"])
        self.assertEqual(groups_by_name["GENERAL"]["proxies"], ["FINAL-EXIT"])
        self.assertNotIn("AI-FIXED-IP", groups_by_name)
        self.assertEqual(groups_by_name["GLOBAL"]["proxies"], ["GENERAL", "FINAL-EXIT"])
        for group_name in ("EXIT-SELECT", "FINAL-EXIT", "GENERAL", "GLOBAL"):
            self.assertNotIn("PROXY-FALLBACK", groups_by_name[group_name]["proxies"])
            self.assertNotIn("DIRECT", groups_by_name[group_name]["proxies"])

    def test_selected_exit_missing_from_healthy_export_fails_closed(self) -> None:
        parsed = yaml.safe_load(
            build_full_clash_config(
                {"tier1": [], "tier2": [], "tier3": [], "exit": []},
                active_exit_proxy="Unavailable-Gateway",
            )
        )
        groups_by_name = {group["name"]: group for group in parsed["proxy-groups"]}

        self.assertEqual(groups_by_name["EXIT-SELECT"]["proxies"], ["REJECT"])
        self.assertEqual(groups_by_name["FINAL-EXIT"]["proxies"], ["EXIT-SELECT", "REJECT"])
        self.assertNotIn("PROXY-FALLBACK", groups_by_name["FINAL-EXIT"]["proxies"])

    def test_final_exit_falls_back_to_prior_tiers_without_exit_nodes(self) -> None:
        parsed = yaml.safe_load(build_full_clash_config({"tier1": [], "tier2": [], "tier3": [], "exit": []}))
        groups_by_name = {group["name"]: group for group in parsed["proxy-groups"]}

        self.assertNotIn("EXIT-AUTO", groups_by_name)
        self.assertEqual(groups_by_name["FINAL-EXIT"]["proxies"], ["PROXY-FALLBACK", "REJECT"])
        self.assertEqual(groups_by_name["PROXY-FALLBACK"]["proxies"], ["REJECT"])

    def test_tls_options_do_not_fallback_to_ip_sni(self) -> None:
        node = parse_node_uri(
            "vless://11111111-1111-1111-1111-111111111111@203.0.113.10:443"
            "?encryption=none&security=tls&type=tcp#ip-node",
            source="test",
            tier="tier1",
        )
        assert node is not None

        proxy = node_to_clash_proxy(node)

        assert proxy is not None
        self.assertNotIn("servername", proxy)

    def test_tuic_proxy_is_emitted(self) -> None:
        node = parse_node_uri(
            "tuic://33333333-3333-3333-3333-333333333333:tuic-pass@example.org:10443"
            "?udp_relay_mode=native&congestion_control=bbr&sni=tuic.example.org#tuic",
            source="test",
            tier="tier3",
        )
        assert node is not None

        proxy = node_to_clash_proxy(node)

        assert proxy is not None
        self.assertEqual(proxy["type"], "tuic")
        self.assertEqual(proxy["uuid"], "33333333-3333-3333-3333-333333333333")
        self.assertEqual(proxy["password"], "tuic-pass")
        self.assertEqual(proxy["udp-relay-mode"], "native")
        self.assertEqual(proxy["congestion-controller"], "bbr")
        self.assertEqual(proxy["sni"], "tuic.example.org")

    def test_socks5_proxy_is_emitted(self) -> None:
        node = parse_node_uri("socks5://user:pass@example.net:1080#local-socks", source="test", tier="tier2")
        assert node is not None

        proxy = node_to_clash_proxy(node)

        assert proxy is not None
        self.assertEqual(proxy["type"], "socks5")
        self.assertEqual(proxy["username"], "user")
        self.assertEqual(proxy["password"], "pass")

    def test_custom_routing_rules_and_ipv6_are_emitted(self) -> None:
        parsed = yaml.safe_load(
            build_full_clash_config(
                {"tier1": [], "tier2": [], "tier3": [], "exit": []},
                ipv6=True,
                routing_config={
                    "rules": {
                        "direct": ["example.cn", "geosite:private"],
                        "proxy": ["keyword:openai"],
                        "reject": ["geosite:category-ads-all"],
                        "ai_fixed_ip": ["DOMAIN-SUFFIX,api.openai.com"],
                        "rule_sets": [{"provider": "ads", "target": "reject"}],
                        "raw": ["DOMAIN,legacy-ai.example,AI-FIXED-IP"],
                    },
                    "rule_providers": {
                        "ads": {
                            "type": "http",
                            "behavior": "domain",
                            "url": "https://example.com/ads.yaml",
                            "path": "./rules/ads.yaml",
                            "interval": 86400,
                        },
                        "legacy-direct": {
                            "type": "http",
                            "behavior": "domain",
                            "url": "https://example.com/direct.yaml",
                            "proxy": "COMPATIBLE",
                        },
                        "legacy-ai": {
                            "type": "http",
                            "behavior": "domain",
                            "url": "https://example.com/legacy-ai.yaml",
                            "proxy": "AI-FIXED-IP",
                        },
                    },
                },
            )
        )

        self.assertTrue(parsed["ipv6"])
        system_rules = build_final_exit_system_rules(
            [
                "https://www.gstatic.com/generate_204",
                *parsed["geox-url"].values(),
            ]
        )
        self.assertEqual(
            parsed["rules"][: len(system_rules)],
            system_rules,
        )
        self.assertEqual(
            parsed["rules"][len(system_rules) : len(system_rules) + len(LOCAL_NETWORK_SYSTEM_RULES)],
            list(LOCAL_NETWORK_SYSTEM_RULES),
        )
        self.assertEqual(
            parsed["rules"][len(system_rules) + len(LOCAL_NETWORK_SYSTEM_RULES)],
            "DOMAIN-SUFFIX,example.cn,DIRECT",
        )
        self.assertIn("GEOSITE,category-ads-all,REJECT", parsed["rules"])
        self.assertFalse(any("AI-FIXED-IP" in rule for rule in parsed["rules"]))
        self.assertIn("RULE-SET,ads,REJECT", parsed["rules"])
        self.assertIn("ads", parsed["rule-providers"])
        self.assertEqual(parsed["rule-providers"]["ads"]["proxy"], "FINAL-EXIT")
        self.assertEqual(
            parsed["rule-providers"]["legacy-direct"]["proxy"],
            "FINAL-EXIT",
        )
        self.assertEqual(parsed["rule-providers"]["legacy-ai"]["proxy"], "FINAL-EXIT")

    def test_exit_quality_hosts_are_system_owned_before_custom_routing_rules(self) -> None:
        custom_direct_rules = [f"DOMAIN,{host},DIRECT" for host in EXIT_QUALITY_SYSTEM_HOSTS]
        custom_direct_rules.extend(
            [
                "DOMAIN,sub.api.ipify.org,DIRECT",
                "DOMAIN,unrelated.example,DIRECT",
            ]
        )
        parsed = yaml.safe_load(
            build_full_clash_config(
                {"tier1": [], "tier2": [], "tier3": [], "exit": []},
                routing_config={"rules": {"direct": custom_direct_rules}},
            )
        )

        built_in_rules = [f"DOMAIN,{host},FINAL-EXIT" for host in EXIT_QUALITY_SYSTEM_HOSTS]
        self.assertEqual(parsed["rules"][: len(built_in_rules)], built_in_rules)
        for host, built_in_rule in zip(EXIT_QUALITY_SYSTEM_HOSTS, built_in_rules):
            custom_rule = f"DOMAIN,{host},DIRECT"
            self.assertIn(custom_rule, parsed["rules"])
            self.assertLess(parsed["rules"].index(built_in_rule), parsed["rules"].index(custom_rule))
            self.assertNotIn(f"DOMAIN-SUFFIX,{host},FINAL-EXIT", parsed["rules"])
        self.assertIn("DOMAIN,sub.api.ipify.org,DIRECT", parsed["rules"])
        self.assertIn("DOMAIN,unrelated.example,DIRECT", parsed["rules"])

    def test_all_authoritative_probe_hosts_precede_matching_custom_direct_rules(self) -> None:
        probe_urls = [
            "https://user:secret@probe.example/generate_204?token=private",
            "https://second-probe.example/status/204",
            "https://192.0.2.9/generate_204",
            "https://[2001:db8::9]/generate_204",
        ]
        direct_rules = [
            "DOMAIN,probe.example,DIRECT",
            "DOMAIN,second-probe.example,DIRECT",
            "IP-CIDR,192.0.2.9/32,DIRECT,no-resolve",
            "IP-CIDR6,2001:db8::9/128,DIRECT,no-resolve",
        ]
        parsed = yaml.safe_load(
            build_full_clash_config(
                {"tier1": [], "tier2": [], "tier3": [], "exit": []},
                probe_url="https://probe.example/generate_204",
                probe_urls=probe_urls[1:],
                routing_config={"rules": {"direct": direct_rules}},
            )
        )

        system_rules = build_final_exit_system_rules(
            [*probe_urls, *parsed["geox-url"].values()]
        )
        self.assertEqual(parsed["rules"][: len(system_rules)], system_rules)
        expected_system_rules = [
            "DOMAIN,probe.example,FINAL-EXIT",
            "DOMAIN,second-probe.example,FINAL-EXIT",
            "IP-CIDR,192.0.2.9/32,FINAL-EXIT,no-resolve",
            "IP-CIDR6,2001:db8::9/128,FINAL-EXIT,no-resolve",
        ]
        for system_rule, direct_rule in zip(expected_system_rules, direct_rules):
            self.assertIn(system_rule, parsed["rules"])
            self.assertLess(parsed["rules"].index(system_rule), parsed["rules"].index(direct_rule))
        rendered = "\n".join(parsed["rules"][: len(system_rules)])
        self.assertNotIn("user", rendered)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("token", rendered)

    def test_legacy_single_probe_url_call_still_owns_its_exact_host(self) -> None:
        parsed = yaml.safe_load(
            build_full_clash_config(
                {"tier1": [], "tier2": [], "tier3": [], "exit": []},
                probe_url="https://legacy-probe.example/generate_204",
                routing_config={
                    "rules": {
                        "direct": ["DOMAIN,legacy-probe.example,DIRECT"],
                    }
                },
            )
        )

        system_rule = "DOMAIN,legacy-probe.example,FINAL-EXIT"
        direct_rule = "DOMAIN,legacy-probe.example,DIRECT"
        self.assertLess(parsed["rules"].index(system_rule), parsed["rules"].index(direct_rule))
        groups = {group["name"]: group for group in parsed["proxy-groups"]}
        self.assertEqual(
            groups["PROXY-FALLBACK"]["url"],
            "https://legacy-probe.example/generate_204",
        )

    def test_hidden_compatibility_defaults_field_is_never_emitted(self) -> None:
        # A stale config may still carry the retired hidden stage; the writer
        # must ignore it so the routing UI is the whole truth.  The only DIRECT
        # a user did not authorize comes from the local/LAN system rules.
        parsed = yaml.safe_load(
            build_full_clash_config(
                {"tier1": [], "tier2": [], "tier3": [], "exit": []},
                routing_config={
                    "rules": {
                        "proxy": ["geosite:CN"],
                        "reject": ["geoip:CN"],
                        "compatibility_defaults": [
                            {"rule_type": "direct", "value": "geosite:CN"},
                            {"rule_type": "direct", "value": "geoip:CN"},
                            {"rule_type": "direct", "value": "suffix:github.com"},
                        ],
                    }
                },
            )
        )

        self.assertNotIn("GEOSITE,CN,DIRECT", parsed["rules"])
        self.assertNotIn("GEOIP,CN,DIRECT,no-resolve", parsed["rules"])
        self.assertNotIn("DOMAIN-SUFFIX,github.com,DIRECT", parsed["rules"])
        # The user's own proxy/reject rules for the same targets still apply.
        self.assertIn("GEOSITE,CN,GENERAL", parsed["rules"])
        self.assertIn("GEOIP,CN,REJECT,no-resolve", parsed["rules"])
        self.assertFalse(any("AI-FIXED-IP" in rule for rule in parsed["rules"]))
        self.assertEqual(parsed["rules"][-1], "MATCH,GENERAL")

    def test_example_config_has_no_hidden_direct_tail(self) -> None:
        config = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "config.example.yaml").read_text(
                encoding="utf-8"
            )
        )
        parsed = yaml.safe_load(
            build_full_clash_config(
                {"tier1": [], "tier2": [], "tier3": [], "exit": []},
                routing_config=config["routing"],
                dns_config=config["client"]["dns"],
            )
        )

        # The shipped example carries a regional CN default, and it comes from
        # routing.rules.direct — the list Settings renders — not from a stage
        # behind it. No source host is forced DIRECT.
        self.assertEqual(parsed["rules"][-1], "MATCH,GENERAL")
        self.assertIn("GEOSITE,CN,DIRECT", parsed["rules"])
        self.assertIn("GEOIP,CN,DIRECT,no-resolve", parsed["rules"])
        for rule in parsed["rules"]:
            self.assertNotIn("github.com,DIRECT", rule)
            self.assertNotIn("telegram.me,DIRECT", rule)
            self.assertNotIn("t.me,DIRECT", rule)
        self.assertFalse(any("AI-FIXED-IP" in rule for rule in parsed["rules"]))
        self.assertFalse(parsed["dns"]["respect-rules"])

        # Emptying that list in Settings must remove the CN rules entirely,
        # proving nothing is appended behind the user's choice.
        emptied = yaml.safe_load(json.dumps(config["routing"]))
        emptied["rules"]["direct"] = []
        without = yaml.safe_load(
            build_full_clash_config(
                {"tier1": [], "tier2": [], "tier3": [], "exit": []},
                routing_config=emptied,
                dns_config=config["client"]["dns"],
            )
        )
        for rule in without["rules"]:
            self.assertNotIn(",CN,DIRECT", rule)
        self.assertEqual(without["rules"][-1], "MATCH,GENERAL")

    def test_bucketed_full_rules_use_bucket_target(self) -> None:
        parsed = yaml.safe_load(
            build_full_clash_config(
                {"tier1": [], "tier2": [], "tier3": [], "exit": []},
                routing_config={"rules": {"direct": ["DOMAIN,example.cn,REJECT"]}},
            )
        )

        self.assertIn("DOMAIN,example.cn,DIRECT", parsed["rules"])
        self.assertNotIn("DOMAIN,example.cn,REJECT", parsed["rules"])

    def test_no_hidden_country_direct_rule_and_all_groups_are_fail_closed(self) -> None:
        parsed = yaml.safe_load(
            build_full_clash_config({"tier1": [], "tier2": [], "tier3": [], "exit": []})
        )

        self.assertNotIn("GEOSITE,CN,DIRECT", parsed["rules"])
        self.assertNotIn("GEOIP,CN,DIRECT,no-resolve", parsed["rules"])
        self.assertEqual(parsed["rules"][-1], "MATCH,GENERAL")
        for group in parsed["proxy-groups"]:
            self.assertNotIn("DIRECT", group["proxies"], group["name"])
            self.assertTrue(group["proxies"], group["name"])
        groups = {group["name"]: group for group in parsed["proxy-groups"]}
        self.assertEqual(groups["TIER1-AUTO"]["proxies"], ["REJECT"])
        self.assertEqual(groups["TIER2-AUTO"]["proxies"], ["REJECT"])
        self.assertEqual(groups["TIER3-AUTO"]["proxies"], ["REJECT"])
        self.assertEqual(groups["PROXY-FALLBACK"]["proxies"], ["REJECT"])
        self.assertEqual(groups["FINAL-EXIT"]["proxies"], ["PROXY-FALLBACK", "REJECT"])
        self.assertEqual(groups["GENERAL"]["proxies"], ["FINAL-EXIT"])

    def test_public_direct_rules_require_the_direct_bucket(self) -> None:
        parsed = yaml.safe_load(
            build_full_clash_config(
                {"tier1": [], "tier2": [], "tier3": [], "exit": []},
                routing_config={
                    "rules": {
                        "direct": [
                            "example.cn",
                            "203.0.113.7",
                            "2001:db8::7",
                            "geosite:CN",
                            "geoip:CN",
                        ],
                        "raw": [
                            "DOMAIN,raw-direct.example,DIRECT",
                            "DOMAIN,raw-compatible.example,COMPATIBLE",
                            "DOMAIN,raw-retired-ai.example,AI-FIXED-IP",
                            "DOMAIN,direct,GENERAL",
                            "AND,((DOMAIN,example.net),(NETWORK,TCP)),DIRECT",
                        ],
                        "rule_sets": [
                            {"provider": "hidden-direct", "target": "direct"},
                            "RULE-SET,hidden-raw,DIRECT",
                            "RULE-SET,hidden-compatible,COMPATIBLE",
                            "RULE-SET,hidden-retired-ai,AI-FIXED-IP",
                            {"provider": "hidden-retired-ai-underscore", "target": "AI_FIXED_IP"},
                            {"provider": "ordinary-proxy", "target": "proxy"},
                        ],
                    }
                },
            )
        )

        self.assertIn("DOMAIN-SUFFIX,example.cn,DIRECT", parsed["rules"])
        self.assertIn("IP-CIDR,203.0.113.7/32,DIRECT,no-resolve", parsed["rules"])
        self.assertIn("IP-CIDR6,2001:db8::7/128,DIRECT,no-resolve", parsed["rules"])
        self.assertIn("GEOSITE,CN,DIRECT", parsed["rules"])
        self.assertIn("GEOIP,CN,DIRECT,no-resolve", parsed["rules"])
        self.assertNotIn("DOMAIN,raw-direct.example,DIRECT", parsed["rules"])
        self.assertNotIn("DOMAIN,raw-compatible.example,COMPATIBLE", parsed["rules"])
        self.assertNotIn("DOMAIN,raw-retired-ai.example,AI-FIXED-IP", parsed["rules"])
        self.assertIn("DOMAIN,direct,GENERAL", parsed["rules"])
        self.assertNotIn("AND,((DOMAIN,example.net),(NETWORK,TCP)),DIRECT", parsed["rules"])
        self.assertNotIn("RULE-SET,hidden-direct,DIRECT", parsed["rules"])
        self.assertNotIn("RULE-SET,hidden-raw,DIRECT", parsed["rules"])
        self.assertNotIn("RULE-SET,hidden-compatible,COMPATIBLE", parsed["rules"])
        self.assertNotIn("RULE-SET,hidden-retired-ai,AI-FIXED-IP", parsed["rules"])
        self.assertNotIn("RULE-SET,hidden-retired-ai-underscore,AI_FIXED_IP", parsed["rules"])
        self.assertIn("RULE-SET,ordinary-proxy,GENERAL", parsed["rules"])

    def test_local_network_rules_are_the_only_builtin_direct_rules(self) -> None:
        parsed = yaml.safe_load(
            build_full_clash_config({"tier1": [], "tier2": [], "tier3": [], "exit": []})
        )

        direct_rules = [rule for rule in parsed["rules"] if ",DIRECT" in rule]
        self.assertEqual(direct_rules, list(LOCAL_NETWORK_SYSTEM_RULES))
        self.assertNotIn("DOMAIN-SUFFIX,openai.com,AI-FIXED-IP", parsed["rules"])

    def test_probe_interval_is_clamped(self) -> None:
        parsed = yaml.safe_load(build_full_clash_config({"tier1": [], "tier2": [], "tier3": [], "exit": []}, probe_interval=0))
        groups = {group["name"]: group for group in parsed["proxy-groups"]}

        self.assertEqual(groups["PROXY-FALLBACK"]["interval"], 30)
        self.assertEqual(groups["TIER3-AUTO"]["interval"], 30)

    @staticmethod
    def _tier3_route_nodes() -> list[Node]:
        return [
            Node(
                name=name,
                protocol="ss",
                uri=f"ss://{name}",
                address=f"{name}.example.com",
                port=8388,
                method="aes-128-gcm",
                password="pass",
                tier="tier3",
            )
            for name in ("tier3-first", "tier3-second")
        ]

    def test_tier3_auto_health_checks_real_site_while_other_groups_keep_probe_url(self) -> None:
        tier1_node = parse_node_uri(
            "vless://11111111-1111-1111-1111-111111111111@example.com:443"
            "?encryption=none&security=tls&type=tcp&sni=example.com#Example-VPS",
            source="test",
            tier="tier1",
        )
        tier2_node = parse_node_uri(
            "ss://YWVzLTI1Ni1nY206cGFzc0BleGFtcGxlLmNvbTo4Mzg4#JMS-SS",
            source="test",
            tier="tier2",
        )
        assert tier1_node is not None
        assert tier2_node is not None
        real_site = "https://real-site.example/generate_204"
        parsed = yaml.safe_load(
            build_full_clash_config(
                {
                    "tier1": [tier1_node],
                    "tier2": [tier2_node],
                    "tier3": self._tier3_route_nodes(),
                    "exit": [],
                },
                probe_url="https://neutral.example/generate_204",
                probe_urls=["https://second-neutral.example/generate_204"],
                probe_interval=300,
                tier3_health_check_url=real_site,
                routing_config={
                    "rules": {"direct": ["DOMAIN,real-site.example,DIRECT"]}
                },
            )
        )
        groups = {group["name"]: group for group in parsed["proxy-groups"]}

        tier3 = groups["TIER3-AUTO"]
        self.assertEqual(tier3["url"], real_site)
        self.assertEqual(tier3["expected-status"], 204)
        self.assertEqual(tier3["interval"], 120)
        other_fallbacks = [
            group
            for group in parsed["proxy-groups"]
            if group["type"] == "fallback" and group["name"] != "TIER3-AUTO"
        ]
        self.assertTrue(
            {"PROXY-FALLBACK", "TIER1-AUTO", "TIER2-AUTO"}
            <= {group["name"] for group in other_fallbacks}
        )
        for group in other_fallbacks:
            with self.subTest(group=group["name"]):
                self.assertEqual(group["url"], "https://neutral.example/generate_204")
                self.assertEqual(group["interval"], 300)
        # The route contract is a group health check, not an authoritative
        # probe: no FINAL-EXIT system rule, and the user's rule still applies.
        self.assertFalse(any("real-site.example,FINAL-EXIT" in rule for rule in parsed["rules"]))
        self.assertIn("DOMAIN,real-site.example,DIRECT", parsed["rules"])
        self.assertIn("DOMAIN,neutral.example,FINAL-EXIT", parsed["rules"])

    def test_tier3_auto_defaults_to_google_generate_204_and_keeps_shorter_interval(self) -> None:
        parsed = yaml.safe_load(
            build_full_clash_config(
                {"tier1": [], "tier2": [], "tier3": self._tier3_route_nodes(), "exit": []},
                probe_url="https://cp.cloudflare.com",
                probe_interval=45,
            )
        )
        groups = {group["name"]: group for group in parsed["proxy-groups"]}

        self.assertEqual(groups["TIER3-AUTO"]["url"], "https://www.google.com/generate_204")
        self.assertEqual(groups["TIER3-AUTO"]["interval"], 45)
        self.assertEqual(groups["PROXY-FALLBACK"]["url"], "https://cp.cloudflare.com")
        self.assertFalse(any("www.google.com" in rule for rule in parsed["rules"]))

    def test_tier3_auto_blank_route_url_keeps_real_site_contract(self) -> None:
        # The real-site route check has no off switch (config.example.yaml):
        # a blank or missing URL keeps the default, never probe_url.
        for value in ("", "  ", None):
            with self.subTest(value=value):
                parsed = yaml.safe_load(
                    build_full_clash_config(
                        {"tier1": [], "tier2": [], "tier3": self._tier3_route_nodes(), "exit": []},
                        probe_url="https://cp.cloudflare.com",
                        probe_interval=300,
                        tier3_health_check_url=value,
                    )
                )
                groups = {group["name"]: group for group in parsed["proxy-groups"]}

                self.assertEqual(groups["TIER3-AUTO"]["url"], "https://www.google.com/generate_204")
                self.assertEqual(groups["TIER3-AUTO"]["interval"], 120)
                self.assertEqual(groups["TIER3-AUTO"]["expected-status"], 204)
                self.assertEqual(groups["PROXY-FALLBACK"]["url"], "https://cp.cloudflare.com")
                self.assertEqual(groups["PROXY-FALLBACK"]["interval"], 300)

    def test_tier3_auto_without_real_site_member_still_leads_with_a_node_not_reject(self) -> None:
        # Failure mode when no member passes the real-site check: Mihomo's
        # fallback returns its first loaded member (never REJECT, which is not
        # a member here and is never alive), identical to the old behaviour
        # when no member passed the neutral URL.  REJECT stays reachable only
        # as FINAL-EXIT's explicit member, reserved for a failed fixed Gateway.
        parsed = yaml.safe_load(
            build_full_clash_config(
                {"tier1": [], "tier2": [], "tier3": self._tier3_route_nodes(), "exit": []},
            )
        )
        groups = {group["name"]: group for group in parsed["proxy-groups"]}

        self.assertEqual(groups["TIER3-AUTO"]["proxies"], ["tier3-first", "tier3-second"])
        self.assertEqual(groups["TIER3-AUTO"]["empty-fallback"], "REJECT")
        self.assertEqual(groups["PROXY-FALLBACK"]["proxies"], ["TIER3-AUTO"])
        self.assertEqual(groups["FINAL-EXIT"]["proxies"], ["PROXY-FALLBACK", "REJECT"])

    def test_allow_lan_defaults_false_and_is_configurable(self) -> None:
        default_parsed = yaml.safe_load(build_full_clash_config({"tier1": [], "tier2": [], "tier3": [], "exit": []}))
        self.assertFalse(default_parsed["allow-lan"])

        enabled_parsed = yaml.safe_load(
            build_full_clash_config({"tier1": [], "tier2": [], "tier3": [], "exit": []}, allow_lan=True)
        )
        self.assertTrue(enabled_parsed["allow-lan"])

    def test_string_false_values_do_not_enable_dns_ipv6_or_tun_flags(self) -> None:
        parsed = yaml.safe_load(
            build_full_clash_config(
                {"tier1": [], "tier2": [], "tier3": [], "exit": []},
                allow_lan="false",
                ipv6="false",
                network_mode="tun",
                tun_config={
                    "auto_route": "false",
                    "auto_detect_interface": "false",
                    "strict_route": "false",
                },
                dns_config={"enable": "true", "ipv6": "false"},
            )
        )

        self.assertFalse(parsed["tun"]["auto-route"])
        self.assertFalse(parsed["tun"]["auto-detect-interface"])
        self.assertFalse(parsed["tun"]["strict-route"])
        self.assertFalse(parsed["allow-lan"])
        self.assertFalse(parsed["ipv6"])
        self.assertFalse(parsed["dns"]["ipv6"])
        self.assertFalse(parsed["dns"]["respect-rules"])

    def test_dns_respect_rules_accepts_snake_and_mihomo_keys(self) -> None:
        snake = yaml.safe_load(
            build_full_clash_config(
                {"tier1": [], "tier2": [], "tier3": [], "exit": []},
                dns_config={"respect_rules": "true"},
            )
        )
        mihomo = yaml.safe_load(
            build_full_clash_config(
                {"tier1": [], "tier2": [], "tier3": [], "exit": []},
                dns_config={"respect-rules": "true"},
            )
        )

        self.assertTrue(snake["dns"]["respect-rules"])
        self.assertTrue(mihomo["dns"]["respect-rules"])

    def test_reserved_mihomo_policy_and_group_names_are_not_exported_as_node_names(self) -> None:
        direct = Node(
            name="DIRECT",
            protocol="socks5",
            address="direct-node.example",
            port=1080,
        )
        final_exit = Node(
            name="FINAL-EXIT",
            protocol="socks5",
            address="exit-node.example",
            port=1080,
        )
        retired_ai = Node(
            name="AI-FIXED-IP",
            protocol="socks5",
            address="retired-ai-name.example",
            port=1080,
        )

        parsed = yaml.safe_load(
            build_full_clash_config(
                {
                    "tier1": [direct, final_exit, retired_ai],
                    "tier2": [],
                    "tier3": [],
                    "exit": [],
                }
            )
        )
        proxy_names = [proxy["name"] for proxy in parsed["proxies"]]

        self.assertNotIn("DIRECT", proxy_names)
        self.assertNotIn("FINAL-EXIT", proxy_names)
        self.assertNotIn("AI-FIXED-IP", proxy_names)
        self.assertEqual(proxy_names, ["DIRECT-2", "FINAL-EXIT-2", "AI-FIXED-IP-2"])

    def test_string_false_dns_enable_honors_explicit_disable(self) -> None:
        parsed = yaml.safe_load(
            build_full_clash_config(
                {"tier1": [], "tier2": [], "tier3": [], "exit": []},
                dns_config={"enable": "false"},
            )
        )

        self.assertNotIn("dns", parsed)

    def test_mixed_port_is_configurable(self) -> None:
        parsed = yaml.safe_load(
            build_full_clash_config({"tier1": [], "tier2": [], "tier3": [], "exit": []}, mixed_port=7899)
        )

        self.assertEqual(parsed["mixed-port"], 7899)

    def test_geox_url_override(self) -> None:
        parsed = yaml.safe_load(
            build_full_clash_config(
                {"tier1": [], "tier2": [], "tier3": [], "exit": []},
                geox_url={
                    "geoip": "https://mirror.example.com/geoip.dat",
                    "geosite": "https://mirror.example.com/geosite.dat",
                    "mmdb": "https://mirror.example.com/country.mmdb",
                },
            )
        )

        self.assertEqual(parsed["geox-url"]["geoip"], "https://mirror.example.com/geoip.dat")
        self.assertEqual(parsed["geox-url"]["mmdb"], "https://mirror.example.com/country.mmdb")

    def test_geox_hosts_are_system_owned_before_matching_direct_rules(self) -> None:
        parsed = yaml.safe_load(
            build_full_clash_config(
                {"tier1": [], "tier2": [], "tier3": [], "exit": []},
                geox_url={
                    "geoip": "https://raw.githubusercontent.com/example/geoip.dat",
                    "geosite": "https://assets.example/geosite.dat",
                },
                routing_config={
                    "rules": {
                        "direct": [
                            "suffix:githubusercontent.com",
                            "suffix:assets.example",
                        ]
                    }
                },
            )
        )

        for system_rule, direct_rule in (
            (
                "DOMAIN,raw.githubusercontent.com,FINAL-EXIT",
                "DOMAIN-SUFFIX,githubusercontent.com,DIRECT",
            ),
            (
                "DOMAIN,assets.example,FINAL-EXIT",
                "DOMAIN-SUFFIX,assets.example,DIRECT",
            ),
        ):
            self.assertLess(
                parsed["rules"].index(system_rule),
                parsed["rules"].index(direct_rule),
            )

    def test_wireguard_proxy_is_emitted(self) -> None:
        node = parse_node_uri(
            "wireguard://cHJpdmF0ZQ==@wg.example.com:51820"
            "?publickey=cHVibGlj&allowed_ips=0.0.0.0/0,::/0&address=10.0.0.2/32&mtu=1408&reserved=1,2,3#wg",
            source="test",
            tier="tier1",
        )
        assert node is not None

        proxy = node_to_clash_proxy(node)

        assert proxy is not None
        self.assertEqual(proxy["type"], "wireguard")
        self.assertEqual(proxy["server"], "wg.example.com")
        self.assertEqual(proxy["port"], 51820)
        self.assertEqual(proxy["private-key"], "cHJpdmF0ZQ==")
        self.assertEqual(proxy["public-key"], "cHVibGlj")
        self.assertEqual(proxy["allowed-ips"], ["0.0.0.0/0", "::/0"])
        self.assertEqual(proxy["ip"], "10.0.0.2")
        self.assertEqual(proxy["mtu"], 1408)
        self.assertEqual(proxy["reserved"], [1, 2, 3])

    def test_wireguard_without_public_key_is_skipped(self) -> None:
        node = Node(name="wg", protocol="wireguard", address="wg.example.com", port=51820, password="priv")
        self.assertIsNone(node_to_clash_proxy(node))

    def test_tuic_version_field_is_emitted(self) -> None:
        node = parse_node_uri(
            "tuic://55555555-5555-5555-5555-555555555555:pw@example.org:10443"
            "?version=5&sni=tuic.example.org#tuic5",
            source="test",
            tier="tier3",
        )
        assert node is not None
        proxy = node_to_clash_proxy(node)
        assert proxy is not None
        self.assertEqual(proxy["version"], 5)

    def test_malformed_boolean_transport_fields_do_not_abort_collection(self) -> None:
        node = Node(
            name="malformed-public-node",
            protocol="vless",
            address="example.org",
            port=443,
            uuid="11111111-1111-1111-1111-111111111111",
            network=False,  # type: ignore[arg-type]
            security=False,  # type: ignore[arg-type]
        )

        proxy = node_to_clash_proxy(node)

        assert proxy is not None
        self.assertEqual(proxy["network"], "tcp")


if __name__ == "__main__":
    unittest.main()


def test_bootstrap_forces_respect_rules_off_but_keeps_the_rest_of_the_dns_block():
    """The bootstrap Core has no proxy outbound a rule could route DNS through."""

    formal_text = build_full_clash_config(
        {"tier1": [], "tier2": [], "tier3": [], "exit": []},
        dns_config={
            "enable": True,
            "mode": "custom",
            "respect_rules": True,
            "nameserver": ["tls://9.9.9.9"],
            "default_nameserver": ["system"],
            "proxy_server_nameserver": ["tls://9.9.9.9"],
        },
    )
    formal = yaml.safe_load(formal_text)
    assert formal["dns"]["respect-rules"] is True
    bootstrap = yaml.safe_load(build_bootstrap_clash_config(formal_text))
    assert bootstrap["dns"]["respect-rules"] is False
    assert {k: v for k, v in bootstrap["dns"].items() if k != "respect-rules"} == {
        k: v for k, v in formal["dns"].items() if k != "respect-rules"
    }
    # Promotion re-renders from the formal payload: user policy returns.
    standby = yaml.safe_load(build_capture_disabled_clash_config(formal_text))
    assert standby["dns"]["respect-rules"] is True


def test_routing_rule_order_compiles_the_three_lists_in_the_chosen_order() -> None:
    """routing.rule_order (11-01): which target list matches first; each list
    keeps its own order and raw / rule_sets still follow the three lists."""

    from clash_formatter import build_custom_routing_rules
    from config_utils import normalize_routing_rule_order

    routing = {
        "rules": {
            "direct": ["geosite:CN", "suffix:example.cn"],
            "proxy": ["domain:www.example.cn"],
            "reject": ["keyword:ads"],
            "raw": ["DST-PORT,8080,REJECT"],
        }
    }
    default = build_custom_routing_rules(routing)
    assert default == [
        "GEOSITE,CN,DIRECT",
        "DOMAIN-SUFFIX,example.cn,DIRECT",
        "DOMAIN,www.example.cn,GENERAL",
        "DOMAIN-KEYWORD,ads,REJECT",
        "DST-PORT,8080,REJECT",
    ]
    ordered = build_custom_routing_rules({**routing, "rule_order": ["reject", "proxy", "direct"]})
    assert ordered == [
        "DOMAIN-KEYWORD,ads,REJECT",
        "DOMAIN,www.example.cn,GENERAL",
        "GEOSITE,CN,DIRECT",
        "DOMAIN-SUFFIX,example.cn,DIRECT",
        "DST-PORT,8080,REJECT",
    ]
    # Absent, partial, repeated or unknown names complete to a full order;
    # anything unreadable is the default, so nothing is ever dropped.
    assert normalize_routing_rule_order(None) == ["direct", "proxy", "reject"]
    assert normalize_routing_rule_order(["reject"]) == ["reject", "direct", "proxy"]
    assert normalize_routing_rule_order(["Proxy", "proxy", "raw", "reject"]) == ["proxy", "reject", "direct"]
    assert normalize_routing_rule_order("reject") == ["direct", "proxy", "reject"]
    assert build_custom_routing_rules({**routing, "rule_order": "junk"}) == default


def test_bootstrap_defers_only_geodata_rules_on_a_first_run() -> None:
    formal = (
        "geodata-mode: true\n"
        "rules:\n"
        "- DOMAIN-SUFFIX,local,DIRECT\n"
        "- GEOSITE,CN,DIRECT\n"
        "- AND,((GEOIP,CN),(NETWORK,UDP)),REJECT\n"
        "- GEOIP,CN,DIRECT,no-resolve\n"
        "- DOMAIN,example.com,FINAL-EXIT\n"
        "- MATCH,GENERAL\n"
        "proxy-groups:\n"
        "- name: PROXY-FALLBACK\n"
        "  type: fallback\n"
        "  proxies: [REJECT]\n"
    )
    deferred = yaml.safe_load(build_bootstrap_clash_config(formal, defer_geodata_rules=True))
    assert deferred["rules"] == [
        "DOMAIN-SUFFIX,local,DIRECT",
        "DOMAIN,example.com,FINAL-EXIT",
        "MATCH,GENERAL",
    ]
    kept = yaml.safe_load(build_bootstrap_clash_config(formal))
    assert "GEOSITE,CN,DIRECT" in kept["rules"]
    assert "GEOIP,CN,DIRECT,no-resolve" in kept["rules"]


def _formal_with_pool_provider() -> str:
    health = {"url": "https://www.google.com/generate_204", "interval": 120, "timeout": 5000,
              "expected-status": 204}
    return yaml.safe_dump({
        "geodata-mode": True,
        "proxies": [],
        "proxy-providers": {"TIER3B-RUNTIME": {"type": "file", "path": "./tier3b-runtime.yaml"}},
        "proxy-groups": [
            {"name": "TIER3-AUTO", "type": "fallback", "empty-fallback": "REJECT", **health,
             "lazy": False, "max-failed-times": 2, "use": ["TIER3B-RUNTIME"]},
            {"name": "PROXY-FALLBACK", "type": "fallback", "empty-fallback": "REJECT",
             "proxies": ["TIER3-AUTO"], "url": "https://cp.cloudflare.com", "interval": 300},
            {"name": "FINAL-EXIT", "type": "select", "proxies": ["PROXY-FALLBACK", "REJECT"]},
        ],
        "rules": ["GEOSITE,CN,DIRECT", "MATCH,FINAL-EXIT"],
        "listeners": [
            {"name": "source-final-exit", "type": "http", "listen": "127.0.0.1", "port": 61017,
             "proxy": "FINAL-EXIT", "users": [{"username": "source", "password": "source-secret"}]},
            {"name": "source-direct", "type": "http", "listen": "127.0.0.1", "port": 61018,
             "proxy": "DIRECT", "users": [{"username": "source", "password": "source-secret"}]},
        ],
    }, sort_keys=False)


def test_bootstrap_geodata_listener_rotates_over_the_pool() -> None:
    from clash_formatter import (
        BOOTSTRAP_GEODATA_GROUP_NAME,
        BOOTSTRAP_GEODATA_LISTENER_NAME,
        MIHOMO_RESERVED_PROXY_NAMES,
    )

    formal = _formal_with_pool_provider()
    spec = {"port": 61020, "username": "tesseract-geo", "password": "geo-secret",
            "runtime_generation": 3}
    plain = yaml.safe_load(build_bootstrap_clash_config(formal, defer_geodata_rules=True))
    bootstrap = yaml.safe_load(
        build_bootstrap_clash_config(formal, defer_geodata_rules=True, geodata_listener=spec)
    )

    pool = next(group for group in bootstrap["proxy-groups"] if group["name"] == "TIER3-AUTO")
    group = next(
        group for group in bootstrap["proxy-groups"] if group["name"] == BOOTSTRAP_GEODATA_GROUP_NAME
    )
    # Every connection takes the next pool member; an empty pool is REJECT.
    assert group["type"] == "load-balance" and group["strategy"] == "round-robin"
    assert group["empty-fallback"] == "REJECT"
    # The pool's members and health contract, so Mihomo adds no probe for it.
    for key in ("use", "url", "interval", "lazy", "timeout", "expected-status", "max-failed-times"):
        assert group[key] == pool[key]
    listener = next(
        item for item in bootstrap["listeners"] if item["name"] == BOOTSTRAP_GEODATA_LISTENER_NAME
    )
    assert listener == {
        "name": BOOTSTRAP_GEODATA_LISTENER_NAME, "type": "http", "listen": "127.0.0.1",
        "port": 61020, "proxy": BOOTSTRAP_GEODATA_GROUP_NAME,
        "users": [{"username": "tesseract-geo", "password": "geo-secret"}],
    }
    # Nothing else moves: the source lane still leaves through PROXY-FALLBACK.
    assert [g for g in bootstrap["proxy-groups"] if g is not group] == plain["proxy-groups"]
    assert [item for item in bootstrap["listeners"] if item is not listener] == plain["listeners"]
    assert bootstrap["rules"] == plain["rules"] == ["MATCH,FINAL-EXIT"]
    assert BOOTSTRAP_GEODATA_GROUP_NAME in MIHOMO_RESERVED_PROXY_NAMES
    # Same input, same bytes: the writer and the contract snapshot agree.
    assert build_bootstrap_clash_config(
        formal, defer_geodata_rules=True, geodata_listener=dict(spec)
    ) == build_bootstrap_clash_config(formal, defer_geodata_rules=True, geodata_listener=spec)


def test_bootstrap_geodata_listener_needs_a_pool_group_and_a_whole_spec() -> None:
    import pytest

    from clash_formatter import BOOTSTRAP_GEODATA_LISTENER_NAME

    without_pool = (
        "proxy-groups:\n"
        "- name: PROXY-FALLBACK\n"
        "  type: fallback\n"
        "  proxies: [REJECT]\n"
        "rules:\n"
        "- MATCH,PROXY-FALLBACK\n"
    )
    spec = {"port": 61020, "username": "tesseract-geo", "password": "geo-secret"}
    assert build_bootstrap_clash_config(
        without_pool, geodata_listener=spec
    ) == build_bootstrap_clash_config(without_pool)
    assert BOOTSTRAP_GEODATA_LISTENER_NAME not in build_bootstrap_clash_config(
        _formal_with_pool_provider()
    )
    for broken in ({"port": 0, **{k: spec[k] for k in ("username", "password")}},
                   {"port": 61020, "username": "", "password": "x"},
                   {"port": "x", "username": "u", "password": "p"}):
        with pytest.raises(ValueError):
            build_bootstrap_clash_config(_formal_with_pool_provider(), geodata_listener=broken)
