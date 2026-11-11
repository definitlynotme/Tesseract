import base64
import logging
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import yaml

from alerts import AlertManager
from manager import ProxyTierManager
from models import Node
from subscription import SubscriptionServer, encode_subscription, render_subscription_route


class SubscriptionTests(unittest.TestCase):
    def test_encode_subscription(self) -> None:
        encoded = encode_subscription(["vless://one", "ss://two"])
        decoded = base64.b64decode(encoded).decode("utf-8")
        self.assertEqual(decoded, "vless://one\nss://two")

    def test_plain_subscription_exports_structured_tier1_nodes(self) -> None:
        class FakeManager:
            logger = logging.getLogger("subscription-fake")

            def get_nodes_for_subscription(self, tier_id, limit=None):
                return [
                    Node(
                        name="structured",
                        protocol="vless",
                        uri="",
                        address="example.com",
                        port=443,
                        tier="tier1",
                        uuid="11111111-1111-1111-1111-111111111111",
                        encryption="none",
                        security="tls",
                        network="xhttp",
                        path="/xhttp",
                        sni="example.com",
                    )
                ]

        content_type, payload = render_subscription_route(FakeManager(), "/sub/tier1")
        decoded = base64.b64decode(payload).decode("utf-8")

        self.assertEqual(content_type, "text/plain; charset=utf-8")
        self.assertIn("vless://11111111-1111-1111-1111-111111111111@example.com:443", decoded)
        self.assertIn("type=xhttp", decoded)

    def test_plain_subscription_exports_hysteria2_and_anytls_without_empty_path(self) -> None:
        class FakeManager:
            logger = logging.getLogger("subscription-fake-modern")

            def get_nodes_for_subscription(self, tier_id, limit=None):
                return [
                    Node(name="hy2", protocol="hysteria2", uri="", address="hy2.example", port=443, password="hy2-pass"),
                    Node(name="any", protocol="anytls", uri="", address="any.example", port=8443, password="any-pass"),
                ]

        _content_type, payload = render_subscription_route(FakeManager(), "/sub/tier1")
        decoded = base64.b64decode(payload).decode("utf-8")

        self.assertIn("hysteria2://hy2-pass@hy2.example:443#hy2", decoded)
        self.assertIn("anytls://any-pass@any.example:8443#any", decoded)
        self.assertNotIn(":443/#", decoded)
        self.assertNotIn(":8443/#", decoded)

    def test_plain_subscription_does_not_turn_string_false_into_insecure(self) -> None:
        class FakeManager:
            logger = logging.getLogger("subscription-fake-strict")

            def get_nodes_for_subscription(self, tier_id, limit=None):
                return [
                    Node(
                        name="hy2",
                        protocol="hysteria2",
                        uri="",
                        address="hy2.example",
                        port=443,
                        password="hy2-pass",
                        extra={"skip-cert-verify": "false"},
                    ),
                    Node(
                        name="any",
                        protocol="anytls",
                        uri="",
                        address="any.example",
                        port=8443,
                        password="any-pass",
                        extra={"insecure": "false"},
                    ),
                ]

        _content_type, payload = render_subscription_route(FakeManager(), "/sub/tier1")
        decoded = base64.b64decode(payload).decode("utf-8")

        self.assertNotIn("insecure=1", decoded)

    def test_plain_subscription_exports_http_proxy_auth_uri(self) -> None:
        class FakeManager:
            logger = logging.getLogger("subscription-fake-http")

            def get_nodes_for_subscription(self, tier_id, limit=None):
                return [
                    Node(
                        name="naive",
                        protocol="http",
                        uri="",
                        address="naive.example",
                        port=2443,
                        password="p@ss:word",
                        tls=True,
                        extra={"username": "user name"},
                    )
                ]

        _content_type, payload = render_subscription_route(FakeManager(), "/sub/tier1")
        decoded = base64.b64decode(payload).decode("utf-8")

        self.assertEqual(decoded, "https://user%20name:p%40ss%3Aword@naive.example:2443#naive")

    def test_stop_does_not_close_server_before_serve_starts(self) -> None:
        class FakeHttpd:
            def __init__(self) -> None:
                self.shutdown_called = False
                self.server_close_called = False

            def shutdown(self) -> None:
                self.shutdown_called = True

            def server_close(self) -> None:
                self.server_close_called = True

        class FakeThread:
            def is_alive(self) -> bool:
                return True

            def join(self, timeout=None) -> None:
                return None

        class FakeEvent:
            def wait(self, timeout=None) -> bool:
                return False

            def clear(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            server = SubscriptionServer(self._manager(Path(temp_dir)), "127.0.0.1", 0)
            httpd = FakeHttpd()
            server.httpd = httpd
            server.thread = FakeThread()
            server._serve_started = FakeEvent()

            server.stop()

            self.assertFalse(httpd.shutdown_called)
            self.assertFalse(httpd.server_close_called)
            self.assertIs(server.httpd, httpd)

    def test_clash_route_outputs_valid_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
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
                    "tier1": {
                        "dns": {"proxy_server_nameserver": ["9.9.9.9"]},
                        "nodes": [
                            {
                                "name": "Example-VPS",
                                "address": "192.0.2.156",
                                "port": 443,
                                "vless_link": (
                                    "vless://11111111-1111-1111-1111-111111111111@192.0.2.156:443"
                                    "?encryption=none&flow=xtls-rprx-vision&security=reality"
                                    "&type=tcp&pbk=test-public-key&sid=abcd&sni=www.microsoft.com&fp=chrome#Example-VPS"
                                ),
                            }
                        ],
                        "check_interval": 30,
                    },
                    "tier2": {"subscription_urls": [], "cache_file": str(cache_file), "check_interval": 60},
                    "tier3": {"check_interval": 120, "auto_collect_interval": 3600, "max_pool_size": 100},
                },
                "testing": {
                    "tcp_timeout": 1,
                    "concurrency": 5,
                    "slow_threshold_ms": 1000,
                    "validation_mode": "tcp_only",
                    "mihomo_helper": {"probe_urls": ["https://cp.cloudflare.com", "https://www.gstatic.com/generate_204"]},
                },
                "subscription_server": {"host": "127.0.0.1", "port": 25500, "tier3_limit": 20, "probe_interval": 123},
                "client": {
                    "mode": "clash",
                    "clash_config_path": "",
                    "dns": {
                        "nameserver": ["https://dns.example/dns-query"],
                        "default_nameserver": ["223.5.5.5"],
                        "proxy_server_nameserver": ["119.29.29.29"],
                    },
                },
            }

            alerts = AlertManager(
                {"desktop": {"enabled": False}, "telegram": {"enabled": False}, "sound": {"enabled": False}},
                logging.getLogger("subscription-alerts"),
            )
            manager = ProxyTierManager(config=config, logger=logging.getLogger("subscription-manager"), alerts=alerts)
            manager.runtime_nodes["tier1"][0].extra["dns"] = {"proxy_server_nameserver": ["8.8.4.4"]}

            content_type, payload = render_subscription_route(manager, "/clash/tier1", {"tier3_limit": 20})
            self.assertEqual(content_type, "application/yaml; charset=utf-8")
            parsed = yaml.safe_load(payload)
            self.assertIn("proxies", parsed)
            self.assertEqual(parsed["proxies"][0]["type"], "vless")

            config_type, config_payload = render_subscription_route(manager, "/clash/config", {"tier3_limit": 20})
            self.assertEqual(config_type, "application/yaml; charset=utf-8")
            parsed_config = yaml.safe_load(config_payload)
            self.assertIn("proxy-groups", parsed_config)
            self.assertIn("rules", parsed_config)
            groups = {group["name"]: group for group in parsed_config["proxy-groups"]}
            self.assertEqual(groups["PROXY-FALLBACK"]["interval"], 123)
            self.assertEqual(groups["PROXY-FALLBACK"]["url"], "https://cp.cloudflare.com")
            self.assertEqual(parsed_config["dns"]["nameserver"], ["https://dns.example/dns-query"])
            self.assertEqual(parsed_config["dns"]["default-nameserver"], ["223.5.5.5"])
            self.assertEqual(parsed_config["dns"]["proxy-server-nameserver"], ["119.29.29.29", "9.9.9.9", "8.8.4.4"])

    def test_clash_config_uses_mihomo_probe_default_even_with_legacy_xray_probe(self) -> None:
        class FakeManager:
            logger = logging.getLogger("subscription-fake-mihomo-probe")
            testing_config = {"xray_helper": {"probe_url": "https://www.google.com/generate_204"}}
            client_config = {"mihomo_controller": {"enabled": False}, "system_proxy": {"port": 7890}}
            config = {"subscription_server": {}, "routing": {}, "geo_update": {}}

            def get_nodes_for_subscription(self, tier_id, limit=None):
                return []

        _content_type, payload = render_subscription_route(FakeManager(), "/clash/config")
        parsed = yaml.safe_load(payload)
        groups = {group["name"]: group for group in parsed["proxy-groups"]}

        self.assertEqual(groups["PROXY-FALLBACK"]["url"], "https://cp.cloudflare.com")
        # User traffic over the free pool is health-checked on a real site.
        self.assertEqual(groups["TIER3-AUTO"]["url"], "https://www.google.com/generate_204")
        self.assertEqual(groups["TIER3-AUTO"]["expected-status"], 204)
        self.assertEqual(groups["TIER3-AUTO"]["interval"], 120)
        self.assertFalse(any("www.google.com" in rule for rule in parsed["rules"]))

    def test_clash_config_takes_tier3_route_health_url_from_complete_config(self) -> None:
        class FakeManager:
            logger = logging.getLogger("subscription-fake-route-health")
            config = {
                "testing": {
                    "mihomo_helper": {
                        "probe_urls": ["https://neutral.example/generate_204"],
                        "route_health_check_url": "https://real-site.example/generate_204",
                    }
                },
                "subscription_server": {"probe_interval": 60},
                "routing": {},
                "geo_update": {},
            }
            # Deliberately stale derived snapshot: the complete config wins,
            # as it does for the probe URLs shared with DesktopRuntime.
            testing_config = {
                "mihomo_helper": {
                    "route_health_check_url": "https://stale.example/generate_204"
                }
            }
            client_config = {"mihomo_controller": {"enabled": False}, "system_proxy": {"port": 7890}}

            def get_nodes_for_subscription(self, tier_id, limit=None):
                if tier_id != "tier3":
                    return []
                return [
                    Node(
                        name="tier3-route",
                        protocol="ss",
                        uri="ss://tier3-route",
                        address="tier3-route.example.com",
                        port=8388,
                        method="aes-128-gcm",
                        password="pass",
                        tier="tier3",
                    )
                ]

        _content_type, payload = render_subscription_route(FakeManager(), "/clash/config")
        parsed = yaml.safe_load(payload)
        groups = {group["name"]: group for group in parsed["proxy-groups"]}

        self.assertEqual(groups["TIER3-AUTO"]["proxies"], ["tier3-route"])
        self.assertEqual(groups["TIER3-AUTO"]["url"], "https://real-site.example/generate_204")
        self.assertEqual(groups["TIER3-AUTO"]["interval"], 60)
        self.assertEqual(groups["PROXY-FALLBACK"]["url"], "https://neutral.example/generate_204")
        self.assertEqual(groups["PROXY-FALLBACK"]["interval"], 60)
        self.assertIn("DOMAIN,neutral.example,FINAL-EXIT", parsed["rules"])
        rule_text = "\n".join(parsed["rules"])
        self.assertNotIn("real-site.example", rule_text)
        self.assertNotIn("stale.example", rule_text)

    def test_clash_config_blank_route_health_url_keeps_real_site_default(self) -> None:
        # route_health_check_url cannot be turned off (config.example.yaml):
        # '' keeps TIER3-AUTO on the real-site default, not on probe_url.
        class FakeManager:
            logger = logging.getLogger("subscription-fake-route-health-blank")
            config = {
                "testing": {
                    "mihomo_helper": {
                        "probe_urls": ["https://neutral.example/generate_204"],
                        "route_health_check_url": "",
                    }
                },
                "subscription_server": {"probe_interval": 240},
                "routing": {},
                "geo_update": {},
            }
            testing_config = {}
            client_config = {"mihomo_controller": {"enabled": False}, "system_proxy": {"port": 7890}}

            def get_nodes_for_subscription(self, tier_id, limit=None):
                return []

        _content_type, payload = render_subscription_route(FakeManager(), "/clash/config")
        parsed = yaml.safe_load(payload)
        groups = {group["name"]: group for group in parsed["proxy-groups"]}

        self.assertEqual(groups["TIER3-AUTO"]["url"], "https://www.google.com/generate_204")
        self.assertEqual(groups["TIER3-AUTO"]["interval"], 120)
        self.assertEqual(groups["TIER3-AUTO"]["expected-status"], 204)
        self.assertEqual(groups["PROXY-FALLBACK"]["url"], "https://neutral.example/generate_204")
        self.assertEqual(groups["PROXY-FALLBACK"]["interval"], 240)
        self.assertFalse(any("www.google.com" in rule for rule in parsed["rules"]))

    def test_clash_config_routes_every_configured_probe_authority_before_direct(self) -> None:
        class FakeManager:
            logger = logging.getLogger("subscription-fake-all-mihomo-probes")
            config = {
                "testing": {
                    "mihomo_helper": {
                        "probe_urls": [
                            "https://first-probe.example/generate_204",
                            "https://user:secret@second-probe.example/status?token=private",
                            "https://192.0.2.44/generate_204",
                        ]
                    }
                },
                "subscription_server": {},
                "routing": {
                    "rules": {
                        "direct": [
                            "DOMAIN,first-probe.example,DIRECT",
                            "DOMAIN,second-probe.example,DIRECT",
                            "IP-CIDR,192.0.2.44/32,DIRECT,no-resolve",
                        ]
                    }
                },
                "geo_update": {},
            }
            # Deliberately stale: rendering must use the complete manager.config
            # source shared with DesktopRuntime, not this derived snapshot.
            testing_config = {
                "mihomo_helper": {"probe_urls": ["https://stale-probe.example"]}
            }
            client_config = {
                "mihomo_controller": {"enabled": False},
                "system_proxy": {"port": 7890},
            }

            def get_nodes_for_subscription(self, tier_id, limit=None):
                return []

        _content_type, payload = render_subscription_route(FakeManager(), "/clash/config")
        parsed = yaml.safe_load(payload)

        expected_pairs = [
            (
                "DOMAIN,first-probe.example,FINAL-EXIT",
                "DOMAIN,first-probe.example,DIRECT",
            ),
            (
                "DOMAIN,second-probe.example,FINAL-EXIT",
                "DOMAIN,second-probe.example,DIRECT",
            ),
            (
                "IP-CIDR,192.0.2.44/32,FINAL-EXIT,no-resolve",
                "IP-CIDR,192.0.2.44/32,DIRECT,no-resolve",
            ),
        ]
        for system_rule, direct_rule in expected_pairs:
            self.assertLess(
                parsed["rules"].index(system_rule),
                parsed["rules"].index(direct_rule),
            )
        self.assertFalse(any("stale-probe.example" in rule for rule in parsed["rules"]))
        rule_text = "\n".join(parsed["rules"])
        self.assertNotIn("user", rule_text)
        self.assertNotIn("secret", rule_text)
        self.assertNotIn("token", rule_text)

    def test_clash_config_normalizes_subscription_render_settings(self) -> None:
        class FakeManager:
            logger = logging.getLogger("subscription-fake-render-settings")
            testing_config = {"clash_probe_interval": "45"}
            client_config = {"mihomo_controller": {"enabled": False}, "system_proxy": {"port": 70000}}
            config = {
                "subscription_server": {"tier3_limit": "12", "probe_interval": "bad"},
                "routing": {},
                "geo_update": {},
            }

            def __init__(self) -> None:
                self.calls: list[tuple[str, int | None]] = []

            def get_nodes_for_subscription(self, tier_id, limit=None):
                self.calls.append((tier_id, limit))
                return []

        manager = FakeManager()
        _content_type, payload = render_subscription_route(manager, "/clash/config", {"tier3_limit": "bad"})
        parsed = yaml.safe_load(payload)
        groups = {group["name"]: group for group in parsed["proxy-groups"]}

        self.assertEqual(groups["PROXY-FALLBACK"]["interval"], 45)
        self.assertEqual(parsed["mixed-port"], 7890)
        self.assertIn(("tier3", 12), manager.calls)

    def test_clash_config_tolerates_invalid_global_config_blocks(self) -> None:
        class FakeManager:
            logger = logging.getLogger("subscription-fake-invalid-config")
            testing_config = {}
            client_config = {"mihomo_controller": {"enabled": False}, "system_proxy": {"port": 7890}}
            config = {
                "subscription_server": "invalid",
                "tiers": "invalid",
                "routing": "invalid",
                "geo_update": "invalid",
            }

            def get_nodes_for_subscription(self, tier_id, limit=None):
                return []

        _content_type, payload = render_subscription_route(FakeManager(), "/clash/config")
        parsed = yaml.safe_load(payload)

        self.assertEqual(parsed["mixed-port"], 7890)
        self.assertIn("proxy-groups", parsed)

    def test_clash_config_tolerates_invalid_client_nested_blocks(self) -> None:
        class FakeManager:
            logger = logging.getLogger("subscription-fake-invalid-client")
            testing_config = {}
            client_config = {
                "dns": "invalid",
                "mihomo_controller": "invalid",
                "tun": "invalid",
                "system_proxy": {"port": 7890},
            }
            config = {"subscription_server": {}, "routing": {}, "geo_update": {}}

            def get_nodes_for_subscription(self, tier_id, limit=None):
                return []

        _content_type, payload = render_subscription_route(FakeManager(), "/clash/config")
        parsed = yaml.safe_load(payload)

        self.assertEqual(parsed["mixed-port"], 7890)
        self.assertEqual(parsed["external-controller"], "127.0.0.1:9090")

    def test_string_false_disables_controller_lan_and_ipv6_in_generated_config(self) -> None:
        class FakeManager:
            logger = logging.getLogger("subscription-fake-bool")
            testing_config = {}
            client_config = {
                "mihomo_controller": {"enabled": "false"},
                "allow_lan": "false",
                "ipv6": "false",
                "system_proxy": {"port": 7890},
            }
            config = {"subscription_server": {}, "routing": {}, "geo_update": {}}

            def get_nodes_for_subscription(self, tier_id, limit=None):
                return []

        _content_type, payload = render_subscription_route(FakeManager(), "/clash/config")
        parsed = yaml.safe_load(payload)

        self.assertNotIn("external-controller", parsed)
        self.assertFalse(parsed["allow-lan"])
        self.assertFalse(parsed["dns"]["ipv6"])

    def test_clash_config_uses_controller_address_and_secret(self) -> None:
        class FakeManager:
            logger = logging.getLogger("subscription-fake-controller")
            testing_config = {}
            client_config = {
                "mihomo_controller": {
                    "enabled": True,
                    "host": "10.0.0.2",
                    "port": "19090",
                    "secret": 1234,
                },
                "system_proxy": {"port": 7890},
            }
            config = {"subscription_server": {}, "routing": {}, "geo_update": {}}

            def get_nodes_for_subscription(self, tier_id, limit=None):
                return []

        _content_type, payload = render_subscription_route(FakeManager(), "/clash/config")
        parsed = yaml.safe_load(payload)

        self.assertEqual(parsed["external-controller"], "10.0.0.2:19090")
        self.assertEqual(parsed["secret"], "1234")

    def test_subscription_server_requires_token_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self._manager(Path(temp_dir))
            server = SubscriptionServer(manager, "127.0.0.1", 0, {"tier3_limit": 20}, token="secret")
            try:
                server.start()
                assert server.httpd is not None
                host, port = server.httpd.server_address[:2]
                with self.assertRaises(HTTPError) as raised:
                    urlopen(f"http://{host}:{port}/clash/config", timeout=3)
                self.assertEqual(raised.exception.code, 401)

                with urlopen(f"http://{host}:{port}/clash/config?token=secret", timeout=3) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn("application/yaml", response.headers["Content-Type"])
            finally:
                server.stop()

    def test_subscription_server_accepts_header_token_and_sets_allowed_cors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self._manager(Path(temp_dir))
            server = SubscriptionServer(
                manager,
                "127.0.0.1",
                0,
                {"tier3_limit": 20},
                token="secret",
                cors_allowed_origins=["https://app.example"],
            )
            try:
                server.start()
                assert server.httpd is not None
                host, port = server.httpd.server_address[:2]
                request = Request(
                    f"http://{host}:{port}/clash/config",
                    headers={"Authorization": "Bearer secret", "Origin": "https://app.example"},
                )

                with urlopen(request, timeout=3) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers["Access-Control-Allow-Origin"], "https://app.example")
                    self.assertEqual(response.headers["Vary"], "Origin")
            finally:
                server.stop()

    def test_subscription_server_rejects_lan_bind_without_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self._manager(Path(temp_dir))
            server = SubscriptionServer(manager, "0.0.0.0", 0, {"tier3_limit": 20})
            with self.assertRaises(ValueError):
                server.start()

    def _manager(self, temp_path: Path) -> ProxyTierManager:
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
            logging.getLogger("subscription-alerts-helper"),
        )
        return ProxyTierManager(config=config, logger=logging.getLogger("subscription-manager-helper"), alerts=alerts)


if __name__ == "__main__":
    unittest.main()
