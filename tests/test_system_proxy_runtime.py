import unittest

from system_proxy_runtime import (
    EffectiveEgressState,
    DEFAULT_SYSTEM_PROXY_BYPASS,
    HealthFailureProgress,
    SystemProxyCapturePlan,
    SystemProxyRuntimeConfig,
    client_mixed_port,
    effective_egress_state,
    health_failure_progress,
    health_failure_restart_due,
    health_failure_restart_threshold,
    missing_core_egress_state,
    normalize_system_proxy_bypass,
    normalize_system_proxy_bypass_entry,
    should_attempt_core_restart,
    system_proxy_capture_active,
    system_proxy_capture_plan,
    system_proxy_capture_wanted,
    system_proxy_runtime_config,
    tun_mode_core_health_monitor_active,
)


class SystemProxyRuntimeTests(unittest.TestCase):
    def test_client_mixed_port_preserves_default_proxy_and_client_override_priority(self) -> None:
        self.assertEqual(client_mixed_port({}), 7890)
        self.assertEqual(client_mixed_port({"system_proxy": {"port": 7891}}), 7891)
        self.assertEqual(
            client_mixed_port({"mihomo_mixed_port": 7892, "system_proxy": {"port": 7891}}),
            7892,
        )
        self.assertEqual(client_mixed_port({"system_proxy": False}), 7890)

    def test_client_mixed_port_falls_back_for_invalid_values(self) -> None:
        self.assertEqual(client_mixed_port({"system_proxy": {"port": "bad"}}), 7890)
        self.assertEqual(client_mixed_port({"mihomo_mixed_port": 70000, "system_proxy": {"port": 7891}}), 7890)

    def test_system_proxy_runtime_config_preserves_existing_sync_semantics(self) -> None:
        proxy_config = {
            "enabled": "false",
            "host": "0.0.0.0",
            "port": 7891,
            "bypass": ["localhost", 42, "example.com", "10.1.2.3"],
        }
        config = system_proxy_runtime_config({"system_proxy": proxy_config})

        self.assertEqual(
            config,
            SystemProxyRuntimeConfig(
                raw_config=proxy_config,
                enabled=False,
                host="0.0.0.0",
                port=7891,
                bypass=["localhost", "10.1.2.3"],
            ),
        )
        self.assertIs(config.raw_config, proxy_config)

    def test_system_proxy_runtime_config_defaults(self) -> None:
        config = system_proxy_runtime_config(None)

        self.assertTrue(config.enabled)
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 7890)
        self.assertEqual(config.bypass, DEFAULT_SYSTEM_PROXY_BYPASS)

    def test_system_proxy_bypass_normalization_handles_strings_and_lists(self) -> None:
        self.assertEqual(normalize_system_proxy_bypass("<local>"), ["<local>"])
        self.assertEqual(
            normalize_system_proxy_bypass("localhost\n 127.* ; <local> \nlocalhost"),
            ["localhost", "127.*", "<local>"],
        )
        self.assertEqual(normalize_system_proxy_bypass([" localhost ", "", None, "<local>"]), ["localhost", "<local>"])
        self.assertEqual(normalize_system_proxy_bypass(False), DEFAULT_SYSTEM_PROXY_BYPASS)

    def test_system_proxy_bypass_entry_allows_only_local_hosts_and_addresses(self) -> None:
        allowed = {
            "LOCALHOST": "localhost",
            "<LOCAL>": "<local>",
            "Printer-01": "printer-01",
            "*.LOCAL": "*.local",
            "*.lan": "*.lan",
            "*.home.arpa": "*.home.arpa",
            "printer.local": "printer.local",
            "Router.LAN": "router.lan",
            "host.home.arpa": "host.home.arpa",
            "home.arpa": "home.arpa",
            "10.1.2.3": "10.1.2.3",
            "127.0.0.1": "127.0.0.1",
            "169.254.10.20": "169.254.10.20",
            "172.31.255.254": "172.31.255.254",
            "192.168.1.2": "192.168.1.2",
            "10.*.*.*": "10.*",
            "172.16.*": "172.16.*",
            "192.168.1.*": "192.168.1.*",
            "::1": "::1",
            "[::1]": "[::1]",
            "fd12:3456::1": "fd12:3456::1",
            "FE80::1": "fe80::1",
        }

        for value, expected in allowed.items():
            with self.subTest(value=value):
                self.assertEqual(normalize_system_proxy_bypass_entry(value), expected)

    def test_system_proxy_bypass_entry_rejects_public_and_unsupported_values(self) -> None:
        rejected = (
            "example.com",
            "*.example.com",
            "printer.evillocal",
            "*.evillocal",
            "*.local.example",
            "8.8.8.8",
            "198.51.100.10",
            "100.64.0.1",
            "224.0.0.1",
            "2001:4860:4860::8888",
            "2001:db8::1",
            "ff02::1",
            "10.0.0.0/8",
            "192.168.1.0/24",
            "fe80::/10",
            "172.*",
            "192.*",
            "192.168.*.10",
            "*",
            "42",
            "[127.0.0.1]",
        )

        for value in rejected:
            with self.subTest(value=value):
                self.assertIsNone(normalize_system_proxy_bypass_entry(value))
        self.assertIsNone(normalize_system_proxy_bypass_entry(42))
        self.assertIsNone(normalize_system_proxy_bypass_entry(None))

    def test_system_proxy_bypass_filters_legacy_public_entries_without_mutating_config(self) -> None:
        configured_bypass = ["localhost", "example.com", "8.8.8.8", "printer.local"]
        proxy_config = {"bypass": configured_bypass.copy()}

        config = system_proxy_runtime_config({"system_proxy": proxy_config})

        self.assertEqual(config.bypass, ["localhost", "printer.local"])
        self.assertEqual(proxy_config, {"bypass": configured_bypass})
        self.assertIs(config.raw_config, proxy_config)

    def test_system_proxy_runtime_config_does_not_split_string_bypass_into_characters(self) -> None:
        config = system_proxy_runtime_config({"system_proxy": {"bypass": "<local>"}})

        self.assertEqual(config.bypass, ["<local>"])

    def test_system_proxy_capture_wanted_matches_network_mode_contract(self) -> None:
        self.assertTrue(system_proxy_capture_wanted("system_proxy", True))
        self.assertTrue(system_proxy_capture_wanted("mixed", True))
        self.assertFalse(system_proxy_capture_wanted("tun", True))
        self.assertFalse(system_proxy_capture_wanted("system_proxy", False))

    def test_system_proxy_capture_plan_only_probes_when_needed(self) -> None:
        self.assertEqual(
            system_proxy_capture_plan(network_mode="system_proxy", system_proxy_enabled=True, active=False),
            SystemProxyCapturePlan(wanted=True, probe_target_active=True),
        )
        self.assertEqual(
            system_proxy_capture_plan(network_mode="system_proxy", system_proxy_enabled=True, active=True),
            SystemProxyCapturePlan(wanted=True, probe_target_active=False),
        )
        self.assertEqual(
            system_proxy_capture_plan(network_mode="tun", system_proxy_enabled=True, active=False),
            SystemProxyCapturePlan(wanted=False, probe_target_active=False),
        )
        self.assertEqual(
            system_proxy_capture_plan(network_mode="mixed", system_proxy_enabled=False, active=False),
            SystemProxyCapturePlan(wanted=False, probe_target_active=False),
        )

    def test_tun_mode_core_health_monitor_active_preserves_tun_guard(self) -> None:
        self.assertTrue(tun_mode_core_health_monitor_active(network_mode="tun", system_proxy_active=False))
        self.assertFalse(tun_mode_core_health_monitor_active(network_mode="tun", system_proxy_active=True))
        self.assertFalse(tun_mode_core_health_monitor_active(network_mode="mixed", system_proxy_active=False))

    def test_system_proxy_capture_active_preserves_active_or_wanted_target_semantics(self) -> None:
        self.assertTrue(
            system_proxy_capture_active(
                network_mode="tun",
                system_proxy_enabled=False,
                active=True,
                target_active=False,
            )
        )
        self.assertTrue(
            system_proxy_capture_active(
                network_mode="mixed",
                system_proxy_enabled=True,
                active=False,
                target_active=True,
            )
        )
        self.assertFalse(
            system_proxy_capture_active(
                network_mode="tun",
                system_proxy_enabled=True,
                active=False,
                target_active=True,
            )
        )

    def test_effective_egress_state_preserves_reason_priority(self) -> None:
        self.assertEqual(
            effective_egress_state(
                running=False,
                core_present=False,
                core_running=False,
                capture_wanted=True,
                capture_active=False,
            ),
            EffectiveEgressState(None, ""),
        )
        self.assertEqual(
            effective_egress_state(
                running=True,
                core_present=False,
                core_running=False,
                capture_wanted=False,
                capture_active=False,
            ),
            EffectiveEgressState(False, "mihomo core is not running"),
        )
        self.assertEqual(
            effective_egress_state(
                running=True,
                core_present=True,
                core_running=False,
                capture_wanted=True,
                capture_active=False,
            ),
            EffectiveEgressState(False, "system proxy capture is inactive"),
        )
        self.assertEqual(
            effective_egress_state(
                running=True,
                core_present=True,
                core_running=False,
                capture_wanted=False,
                capture_active=False,
            ),
            EffectiveEgressState(False, "mihomo core is not running"),
        )
        self.assertEqual(
            effective_egress_state(
                running=True,
                core_present=True,
                core_running=True,
                capture_wanted=True,
                capture_active=True,
            ),
            EffectiveEgressState(True, ""),
        )

    def test_missing_core_egress_state_preserves_running_reason(self) -> None:
        self.assertEqual(missing_core_egress_state(running=False), EffectiveEgressState(None, ""))
        self.assertEqual(
            missing_core_egress_state(running=True),
            EffectiveEgressState(False, "mihomo core is not running"),
        )

    def test_health_failure_restart_threshold_preserves_floor_and_default(self) -> None:
        self.assertEqual(health_failure_restart_threshold({}), 3)
        self.assertEqual(health_failure_restart_threshold({"health_failures_before_restore": 5}), 5)
        self.assertEqual(health_failure_restart_threshold({"health_failures_before_restore": 0}), 1)
        self.assertEqual(health_failure_restart_threshold(None), 3)

    def test_health_failure_restart_due_preserves_counter_contract(self) -> None:
        proxy_config = {"health_failures_before_restore": 2}

        self.assertFalse(
            health_failure_restart_due(
                failure_count=1,
                system_proxy_config=proxy_config,
            )
        )
        self.assertTrue(
            health_failure_restart_due(
                failure_count=2,
                system_proxy_config=proxy_config,
            )
        )

    def test_health_failure_progress_increments_before_restart_decision(self) -> None:
        proxy_config = {"health_failures_before_restore": 2}

        self.assertEqual(
            health_failure_progress(failure_count=0, system_proxy_config=proxy_config),
            HealthFailureProgress(failure_count=1, restart_due=False),
        )
        self.assertEqual(
            health_failure_progress(failure_count=1, system_proxy_config=proxy_config),
            HealthFailureProgress(failure_count=2, restart_due=True),
        )

    def test_health_failure_restart_threshold_falls_back_for_invalid_values(self) -> None:
        self.assertEqual(health_failure_restart_threshold({"health_failures_before_restore": "bad"}), 3)

    def test_should_attempt_core_restart_preserves_running_and_stopping_gate(self) -> None:
        self.assertTrue(should_attempt_core_restart(running=True, stopping=False))
        self.assertFalse(should_attempt_core_restart(running=True, stopping=True))
        self.assertFalse(should_attempt_core_restart(running=False, stopping=False))


if __name__ == "__main__":
    unittest.main()
