import unittest

from runtime_settings import (
    DEFAULT_PRIMARY_GATEWAY_MAX_LATENCY_MS,
    DEFAULT_PROBE_URLS,
    DEFAULT_ROUTE_HEALTH_CHECK_URL,
    as_config_list,
    alerts_block,
    as_string_list,
    client_config_block,
    client_dns_block,
    client_mihomo_controller_block,
    client_system_proxy_block,
    client_tun_block,
    mihomo_client_paths,
    configured_tier3_export_limit,
    default_probe_expected_statuses,
    default_probe_timeout_ms,
    default_probe_url,
    default_probe_urls,
    egress_proxy_block,
    geo_update_block,
    helper_block_from_testing,
    killswitch_block,
    mihomo_controller_block,
    node_test_concurrency_from_testing,
    node_test_tcp_timeout_seconds,
    normalize_network_mode,
    paths_block,
    primary_gateway_max_latency_ms_from_testing,
    primary_gateway_slow_threshold_ms,
    primary_gateway_slow_threshold_ms_from_testing,
    route_health_check_url,
    routing_block,
    startup_core_probe_concurrency,
    startup_core_probe_limit,
    startup_probe_timeout_seconds,
    subscription_server_block,
    system_proxy_block,
    tier_block,
    tiers_block,
    slow_threshold_ms_from_testing,
    validate_primary_gateway_max_latency_ms,
)
from tier3_defaults import (
    DEFAULT_TIER3_STARTUP_EXPORT_CANDIDATE_LIMIT,
    DEFAULT_TIER3_STARTUP_VALIDATION_CONCURRENCY,
)


class RuntimeSettingsTests(unittest.TestCase):
    def test_normalize_network_mode_accepts_expected_modes_only(self) -> None:
        self.assertEqual(normalize_network_mode("system-proxy"), "system_proxy")
        self.assertEqual(normalize_network_mode("TUN"), "tun")
        self.assertEqual(normalize_network_mode("mixed"), "mixed")
        self.assertEqual(normalize_network_mode("invalid"), "system_proxy")
        self.assertEqual(normalize_network_mode(None), "system_proxy")

    def test_as_string_list_splits_strips_deduplicates_and_ignores_scalars(self) -> None:
        self.assertEqual(as_string_list(" a, b\n a \n"), ["a", "b"])
        self.assertEqual(as_string_list(["a", " ", "b", "a", 3]), ["a", "b", "3"])
        self.assertEqual(as_string_list(204), [])

    def test_as_config_list_accepts_only_list_like_config_values(self) -> None:
        self.assertEqual(as_config_list([{"name": "one"}]), [{"name": "one"}])
        self.assertEqual(as_config_list(({"name": "one"},)), [{"name": "one"}])
        self.assertEqual(as_config_list("not-a-list"), [])
        self.assertEqual(as_config_list({"name": "one"}), [])

    def test_runtime_config_blocks_accept_only_dict_sections(self) -> None:
        config = {
            "client": {
                "system_proxy": {"port": 7891},
                "mihomo_controller": {"port": 9091},
            }
        }

        self.assertEqual(client_config_block(config), config["client"])
        self.assertEqual(system_proxy_block(config), {"port": 7891})
        self.assertEqual(mihomo_controller_block(config), {"port": 9091})
        self.assertEqual(client_config_block({"client": False}), {})
        self.assertEqual(client_dns_block({"dns": {"ipv6": True}}), {"ipv6": True})
        self.assertEqual(client_dns_block({"dns": "invalid"}), {})
        self.assertEqual(client_mihomo_controller_block({"mihomo_controller": {"port": 9092}}), {"port": 9092})
        self.assertEqual(client_mihomo_controller_block({"mihomo_controller": "invalid"}), {})
        self.assertEqual(client_system_proxy_block({"system_proxy": {"port": 7892}}), {"port": 7892})
        self.assertEqual(client_system_proxy_block({"system_proxy": "invalid"}), {})
        self.assertEqual(client_tun_block({"tun": {"enable": True}}), {"enable": True})
        self.assertEqual(client_tun_block({"tun": "invalid"}), {})
        self.assertEqual(alerts_block({"alerts": {"level": "DEBUG"}}), {"level": "DEBUG"})
        self.assertEqual(alerts_block({"alerts": "invalid"}), {})
        self.assertEqual(paths_block({"paths": {"data_dir": "data"}}), {"data_dir": "data"})
        self.assertEqual(paths_block({"paths": "invalid"}), {})
        self.assertEqual(tiers_block({"tiers": {"tier3": {"enabled": True}}}), {"tier3": {"enabled": True}})
        self.assertEqual(tiers_block({"tiers": "invalid"}), {})
        self.assertEqual(tier_block({"tiers": {"tier3": {"enabled": True}}}, "tier3"), {"enabled": True})
        self.assertEqual(tier_block({"tiers": {"tier3": "invalid"}}, "tier3"), {})
        self.assertEqual(killswitch_block({"killswitch": {"enabled": False}}), {"enabled": False})
        self.assertEqual(killswitch_block({"killswitch": "invalid"}), {})
        self.assertEqual(geo_update_block({"geo_update": {"enabled": False}}), {"enabled": False})
        self.assertEqual(geo_update_block({"geo_update": "invalid"}), {})
        self.assertEqual(egress_proxy_block({"egress_proxy": {"enabled": True}}), {"enabled": True})
        self.assertEqual(egress_proxy_block({"egress_proxy": "invalid"}), {})
        self.assertEqual(subscription_server_block({"subscription_server": {"port": 25501}}), {"port": 25501})
        self.assertEqual(subscription_server_block({"subscription_server": "invalid"}), {})
        self.assertEqual(routing_block({"routing": {"rules": []}}), {"rules": []})
        self.assertEqual(routing_block({"routing": "invalid"}), {})
        self.assertEqual(system_proxy_block({"client": {"system_proxy": False}}), {})
        self.assertEqual(mihomo_controller_block(None), {})

    def test_mihomo_client_paths_fall_back_for_invalid_or_empty_values(self) -> None:
        self.assertEqual(
            mihomo_client_paths(
                {
                    "mihomo_binary_path": "C:/tools/mihomo.exe",
                    "mihomo_runtime_config": "runtime.yaml",
                    "mihomo_work_dir": "work",
                }
            ).binary_path,
            "C:/tools/mihomo.exe",
        )

        paths = mihomo_client_paths(
            {
                "mihomo_binary_path": "",
                "mihomo_runtime_config": None,
                "mihomo_work_dir": 123,
            }
        )

        self.assertEqual(paths.binary_path, "bin/mihomo.exe")
        self.assertEqual(paths.runtime_config_path, "data/mihomo-runtime.yaml")
        self.assertEqual(paths.work_dir, "data/mihomo")

    def test_default_probe_urls_preserve_priority_and_default_fallbacks(self) -> None:
        config = {
            "testing": {
                "probe_urls": ["https://testing.example/generate_204"],
                "probe_url": "https://legacy-testing.example",
                "mihomo_helper": {
                    "probe_urls": "https://helper.example/a,https://helper.example/b",
                    "probe_url": "https://testing.example/generate_204",
                },
            }
        }

        self.assertEqual(
            default_probe_urls(config),
            [
                "https://helper.example/a",
                "https://helper.example/b",
                "https://testing.example/generate_204",
                "https://legacy-testing.example",
                *DEFAULT_PROBE_URLS,
            ],
        )
        self.assertEqual(default_probe_url(config), "https://helper.example/a")

    def test_default_probe_urls_ignore_unrelated_xray_helper(self) -> None:
        config = {"testing": {"xray_helper": {"probe_url": "https://xray.example/generate_204"}}}

        self.assertEqual(default_probe_urls(config), list(DEFAULT_PROBE_URLS))
        self.assertEqual(default_probe_url(config), DEFAULT_PROBE_URLS[0])

    def test_route_health_check_url_defaults_to_real_site_https_204(self) -> None:
        self.assertEqual(
            DEFAULT_ROUTE_HEALTH_CHECK_URL,
            "https://www.google.com/generate_204",
        )
        self.assertEqual(route_health_check_url({}), DEFAULT_ROUTE_HEALTH_CHECK_URL)
        # The neutral probe list never leaks into the real-site route contract.
        self.assertEqual(
            route_health_check_url(
                {
                    "testing": {
                        "probe_url": "https://testing.example/generate_204",
                        "mihomo_helper": {"probe_urls": ["https://cp.cloudflare.com"]},
                    }
                }
            ),
            DEFAULT_ROUTE_HEALTH_CHECK_URL,
        )
        self.assertEqual(
            route_health_check_url(
                {
                    "testing": {
                        "mihomo_helper": {
                            "route_health_check_url": " https://real.example/generate_204 "
                        }
                    }
                }
            ),
            "https://real.example/generate_204",
        )

    def test_route_health_check_url_rejects_non_https_or_malformed_values(self) -> None:
        for value in (
            "",
            "   ",
            None,
            204,
            ["https://real.example/generate_204"],
            "http://real.example/generate_204",
            "real.example/generate_204",
            "https://",
            "https://[::1/generate_204",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    route_health_check_url(
                        {"testing": {"mihomo_helper": {"route_health_check_url": value}}}
                    ),
                    DEFAULT_ROUTE_HEALTH_CHECK_URL,
                )
        self.assertEqual(
            route_health_check_url({"testing": {"mihomo_helper": "invalid"}}),
            DEFAULT_ROUTE_HEALTH_CHECK_URL,
        )

    def test_default_probe_expected_statuses_use_mihomo_helper_then_default(self) -> None:
        config = {
            "testing": {
                "expected_statuses": [418],
                "mihomo_helper": {"expected_statuses": "200, 204\nbad"},
            }
        }

        self.assertEqual(default_probe_expected_statuses(config), {200, 204})
        self.assertEqual(default_probe_expected_statuses({"testing": {"expected_statuses": ["bad"]}}), {204, 200})

    def test_probe_timeout_preserves_existing_floor_and_fallbacks(self) -> None:
        self.assertEqual(default_probe_timeout_ms({"testing": {"mihomo_helper": {"probe_timeout": "0.5"}}}), 1000)
        self.assertEqual(default_probe_timeout_ms({"testing": {"request_timeout": 2}}), 2000)
        self.assertEqual(default_probe_timeout_ms({"testing": {"mihomo_helper": {"probe_timeout": "bad"}}}), 8000)

    def test_startup_probe_timeout_preserves_implicit_minimum_and_explicit_bounds(self) -> None:
        self.assertEqual(startup_probe_timeout_seconds({"testing": {"request_timeout": 2}}), 8.0)
        self.assertEqual(startup_probe_timeout_seconds({"tiers": {"tier3": {"startup_probe_timeout": "0.5"}}}), 1.0)
        self.assertEqual(startup_probe_timeout_seconds({"testing": {"mihomo_helper": {"startup_probe_timeout": "40"}}}), 30.0)
        self.assertEqual(startup_probe_timeout_seconds({"testing": {"mihomo_helper": {"startup_timeout": 3}}}), 8.0)

    def test_startup_core_probe_limit_preserves_priority_and_bounds(self) -> None:
        self.assertEqual(
            startup_core_probe_limit(
                {
                    "testing": {"mihomo_helper": {"startup_core_probe_limit": 5}},
                    "tiers": {"tier3": {"startup_core_probe_limit": 9, "startup_export_candidate_limit": 11}},
                }
            ),
            5,
        )
        self.assertEqual(
            startup_core_probe_limit({"tiers": {"tier3": {"startup_core_probe_limit": 0, "startup_export_candidate_limit": 2}}}),
            2,
        )
        self.assertEqual(startup_core_probe_limit({"tiers": {"tier3": {"startup_core_probe_limit": "bad"}}}), DEFAULT_TIER3_STARTUP_EXPORT_CANDIDATE_LIMIT)

    def test_startup_core_probe_concurrency_preserves_priority_and_bounds(self) -> None:
        self.assertEqual(
            startup_core_probe_concurrency(
                {
                    "testing": {"mihomo_helper": {"startup_validation_concurrency": 5, "concurrency": 7}},
                    "tiers": {"tier3": {"startup_validation_concurrency": 3}},
                }
            ),
            3,
        )
        self.assertEqual(startup_core_probe_concurrency({"testing": {"mihomo_helper": {"concurrency": 0}}}), DEFAULT_TIER3_STARTUP_VALIDATION_CONCURRENCY)
        self.assertEqual(startup_core_probe_concurrency({"testing": {"mihomo_helper": {"concurrency": "bad"}}}), DEFAULT_TIER3_STARTUP_VALIDATION_CONCURRENCY)

    def test_configured_tier3_export_limit_preserves_subscription_limit_behavior(self) -> None:
        self.assertEqual(configured_tier3_export_limit({"subscription_server": {"tier3_limit": 3}}), 3)
        self.assertEqual(configured_tier3_export_limit({"subscription_server": {"tier3_limit": 0}}), 1)
        self.assertEqual(configured_tier3_export_limit({"subscription_server": {"tier3_limit": "bad"}}), 20)
        self.assertEqual(configured_tier3_export_limit({"subscription_server": False}), 20)

    def test_testing_slow_threshold_uses_default_for_missing_or_invalid_values(self) -> None:
        self.assertEqual(slow_threshold_ms_from_testing({"slow_threshold_ms": "1500"}), 1500)
        self.assertEqual(slow_threshold_ms_from_testing({"slow_threshold_ms": 0}), 1)
        self.assertEqual(slow_threshold_ms_from_testing({"slow_threshold_ms": "bad"}), 2000)
        self.assertEqual(slow_threshold_ms_from_testing(None), 2000)

    def test_primary_gateway_max_latency_uses_strict_bounds_and_defensive_default(self) -> None:
        self.assertEqual(
            primary_gateway_max_latency_ms_from_testing(
                {"primary_gateway_max_latency_ms": 100}
            ),
            100,
        )
        self.assertEqual(
            primary_gateway_max_latency_ms_from_testing(
                {"primary_gateway_max_latency_ms": 5000}
            ),
            5000,
        )
        for testing in (
            None,
            "bad",
            {},
            {"primary_gateway_max_latency_ms": 99},
            {"primary_gateway_max_latency_ms": 5001},
            {"primary_gateway_max_latency_ms": "800"},
            {"primary_gateway_max_latency_ms": 800.0},
            {"primary_gateway_max_latency_ms": True},
        ):
            with self.subTest(testing=testing):
                self.assertEqual(
                    primary_gateway_max_latency_ms_from_testing(testing),
                    DEFAULT_PRIMARY_GATEWAY_MAX_LATENCY_MS,
                )

    def test_primary_gateway_slow_threshold_is_exact_half_of_valid_maximum(self) -> None:
        self.assertEqual(primary_gateway_slow_threshold_ms(800), 400)
        self.assertEqual(primary_gateway_slow_threshold_ms(801), 400.5)
        self.assertEqual(
            primary_gateway_slow_threshold_ms_from_testing(
                {"primary_gateway_max_latency_ms": 801}
            ),
            400.5,
        )
        self.assertEqual(primary_gateway_slow_threshold_ms("bad"), 400)

    def test_primary_gateway_persisted_validator_requires_integer_in_range(self) -> None:
        self.assertEqual(validate_primary_gateway_max_latency_ms(800), 800)
        for value in (99, 5001, "800", 800.0, True, None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "must be an integer between 100 and 5000"):
                    validate_primary_gateway_max_latency_ms(value)

    def test_helper_block_from_testing_accepts_only_dict_helpers(self) -> None:
        self.assertEqual(helper_block_from_testing({"xray_helper": {"enabled": True}}, "xray_helper"), {"enabled": True})
        self.assertEqual(helper_block_from_testing({"xray_helper": False}, "xray_helper"), {})
        self.assertEqual(helper_block_from_testing(None, "xray_helper"), {})

    def test_node_test_tcp_timeout_falls_back_for_invalid_values(self) -> None:
        self.assertEqual(node_test_tcp_timeout_seconds({"tcp_timeout": "1.5"}), 1.5)
        self.assertEqual(node_test_tcp_timeout_seconds({"tcp_timeout": "bad"}), 5.0)
        self.assertEqual(node_test_tcp_timeout_seconds({"tcp_timeout": 0}), 0.1)
        self.assertEqual(node_test_tcp_timeout_seconds(None), 5.0)

    def test_node_test_concurrency_preserves_gateway_and_mihomo_rules(self) -> None:
        self.assertEqual(
            node_test_concurrency_from_testing(
                {"concurrency": "8"},
                full_validation_enabled=False,
                has_tier3_nodes=False,
                has_gateway_nodes=False,
            ),
            8,
        )
        self.assertEqual(
            node_test_concurrency_from_testing(
                {"concurrency": 8},
                full_validation_enabled=False,
                has_tier3_nodes=False,
                has_gateway_nodes=True,
            ),
            3,
        )
        self.assertEqual(
            node_test_concurrency_from_testing(
                {"concurrency": 8, "gateway_chain_concurrency": 2},
                full_validation_enabled=False,
                has_tier3_nodes=False,
                has_gateway_nodes=True,
            ),
            2,
        )
        self.assertEqual(
            node_test_concurrency_from_testing(
                {"concurrency": 50, "mihomo_helper": {"concurrency": 2}},
                full_validation_enabled=True,
                has_tier3_nodes=True,
                has_gateway_nodes=False,
            ),
            2,
        )

    def test_node_test_concurrency_falls_back_for_invalid_values(self) -> None:
        self.assertEqual(
            node_test_concurrency_from_testing(
                {"concurrency": "bad"},
                full_validation_enabled=False,
                has_tier3_nodes=False,
                has_gateway_nodes=False,
            ),
            50,
        )
        self.assertEqual(
            node_test_concurrency_from_testing(
                {"concurrency": 8, "gateway_chain_concurrency": 0},
                full_validation_enabled=False,
                has_tier3_nodes=False,
                has_gateway_nodes=True,
            ),
            1,
        )
        self.assertEqual(
            node_test_concurrency_from_testing(
                {"concurrency": 8, "mihomo_helper": {"concurrency": "bad"}},
                full_validation_enabled=True,
                has_tier3_nodes=True,
                has_gateway_nodes=False,
            ),
            8,
        )


if __name__ == "__main__":
    unittest.main()
