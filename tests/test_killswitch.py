import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from killswitch import KILL_SWITCH_RULE_NAMES, KillSwitchManager, firewall_remote_token


class KillSwitchTests(unittest.TestCase):
    def test_close_skips_netsh_when_session_never_activated(self) -> None:
        commands: list[list[str]] = []
        manager = KillSwitchManager(
            config={"enabled": False},
            tier1_nodes=[],
            command_runner=lambda command: commands.append(command),
        )
        manager.platform_name = "windows"
        manager.state = "disabled"

        manager.close(timeout=0.1)

        self.assertEqual(commands, [])

    def test_close_cleans_rules_when_session_is_in_error_state(self) -> None:
        commands: list[list[str]] = []

        def runner(
            command: list[str],
            *,
            timeout: float | None = None,
        ) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        manager = KillSwitchManager(
            config={"enabled": False},
            tier1_nodes=[],
            command_runner=runner,
        )
        manager.platform_name = "windows"
        manager.state = "error"

        manager.close(timeout=0.1)

        joined = [" ".join(command) for command in commands]
        self.assertTrue(any("delete rule" in command for command in joined))

    def test_activate_and_deactivate_manage_rules(self) -> None:
        commands: list[list[str]] = []

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        original_detect_admin = KillSwitchManager._detect_admin
        try:
            KillSwitchManager._detect_admin = lambda self: True
            manager = KillSwitchManager(
                config={"enabled": True, "allow_lan": True, "additional_whitelist": ["8.8.8.8"]},
                tier1_nodes=[{"address": "192.0.2.156"}, {"address": "198.51.100.138"}],
                command_runner=runner,
            )
        finally:
            KillSwitchManager._detect_admin = original_detect_admin
        manager.platform_name = "windows"
        manager.state = "armed"

        self.assertTrue(manager.activate())
        self.assertTrue(manager.get_state()["active"])
        joined = [" ".join(command) for command in commands]
        tier1_rule = next(
            command for command in joined
            if "firewall add rule" in command and "ProxyManager-KillSwitch-Allow-Tier1" in command
        )
        self.assertIn("192.0.2.156", tier1_rule)
        self.assertIn("198.51.100.138", tier1_rule)
        self.assertTrue(any("profile=private,public" in command for command in joined if "firewall add rule" in command))

        commands.clear()
        self.assertTrue(manager.deactivate())
        self.assertFalse(manager.get_state()["active"])
        self.assertTrue(any("delete rule" in " ".join(command) for command in commands))

    def test_dns_is_not_globally_allowed_by_default(self) -> None:
        commands: list[list[str]] = []

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        original_detect_admin = KillSwitchManager._detect_admin
        try:
            KillSwitchManager._detect_admin = lambda self: True
            manager = KillSwitchManager(
                config={"enabled": True, "allow_lan": True},
                tier1_nodes=[{"address": "192.0.2.156"}],
                command_runner=runner,
            )
        finally:
            KillSwitchManager._detect_admin = original_detect_admin
        manager.platform_name = "windows"
        manager.state = "armed"

        manager.activate()

        joined = [" ".join(command) for command in commands]
        self.assertFalse(any("ProxyManager-KillSwitch-Allow-DNS" in command and "add rule" in command for command in joined))
        self.assertFalse(manager.get_state()["whitelist"]["dns_udp_53"])

    def test_reconfigure_preserves_explicit_empty_core_program_path(self) -> None:
        original_detect_admin = KillSwitchManager._detect_admin
        try:
            KillSwitchManager._detect_admin = lambda self: True
            manager = KillSwitchManager(
                config={"enabled": True},
                tier1_nodes=[],
                core_program_path="bin/mihomo.exe",
                command_runner=lambda command: subprocess.CompletedProcess(command, 0, "", ""),
            )
        finally:
            KillSwitchManager._detect_admin = original_detect_admin

        manager.reconfigure({"enabled": True}, [], core_program_path="")

        self.assertEqual(manager.core_program_path, "")

    def test_dns_allow_rule_requires_explicit_resolver_ips(self) -> None:
        commands: list[list[str]] = []

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        original_detect_admin = KillSwitchManager._detect_admin
        try:
            KillSwitchManager._detect_admin = lambda self: True
            manager = KillSwitchManager(
                config={"enabled": True, "allow_dns": True, "dns_whitelist": ["1.1.1.1"]},
                tier1_nodes=[],
                command_runner=runner,
            )
        finally:
            KillSwitchManager._detect_admin = original_detect_admin
        manager.platform_name = "windows"
        manager.state = "armed"

        manager.activate()

        dns_rule = next(
            " ".join(command)
            for command in commands
            if "firewall add rule" in " ".join(command) and "ProxyManager-KillSwitch-Allow-DNS" in " ".join(command)
        )
        self.assertIn("remoteip=1.1.1.1", dns_rule)
        self.assertIn("remoteport=53", dns_rule)

    def test_string_false_does_not_enable_lan_or_dns_allow_rules(self) -> None:
        commands: list[list[str]] = []

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        original_detect_admin = KillSwitchManager._detect_admin
        try:
            KillSwitchManager._detect_admin = lambda self: True
            manager = KillSwitchManager(
                config={
                    "enabled": "true",
                    "allow_lan": "false",
                    "allow_dns": "false",
                    "dns_whitelist": ["1.1.1.1"],
                },
                tier1_nodes=[],
                command_runner=runner,
            )
        finally:
            KillSwitchManager._detect_admin = original_detect_admin
        manager.platform_name = "windows"
        manager.state = "armed"

        self.assertTrue(manager.activate())

        joined = [" ".join(command) for command in commands]
        self.assertFalse(any("ProxyManager-KillSwitch-Allow-LAN" in command and "add rule" in command for command in joined))
        self.assertFalse(any("ProxyManager-KillSwitch-Allow-DNS" in command and "add rule" in command for command in joined))
        state = manager.get_state()
        self.assertEqual(state["whitelist"]["lan"], [])
        self.assertEqual(state["whitelist"]["dns"], [])

    def test_string_false_disables_reconfigured_killswitch(self) -> None:
        commands: list[list[str]] = []

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        original_detect_admin = KillSwitchManager._detect_admin
        try:
            KillSwitchManager._detect_admin = lambda self: True
            manager = KillSwitchManager(
                config={"enabled": "true"},
                tier1_nodes=[],
                command_runner=runner,
            )
        finally:
            KillSwitchManager._detect_admin = original_detect_admin
        manager.platform_name = "windows"
        manager.state = "armed"
        self.assertTrue(manager.activate())

        manager.reconfigure({"enabled": "false"}, [])

        self.assertEqual(manager.get_state()["state"], "disabled")
        self.assertTrue(any("delete rule" in " ".join(command) for command in commands))

    def test_invalid_interval_config_falls_back_without_crashing(self) -> None:
        manager = KillSwitchManager(
            config={
                "enabled": True,
                "activation_retry_interval": "bad",
                "dynamic_whitelist_refresh_interval": "bad",
            },
            tier1_nodes=[],
            command_runner=lambda command: subprocess.CompletedProcess(command, 0, "", ""),
        )

        self.assertEqual(manager.activation_retry_interval, 30.0)
        self.assertEqual(manager.dynamic_whitelist_refresh_interval, 300.0)

        manager.reconfigure(
            {
                "enabled": True,
                "activation_retry_interval": "bad",
                "dynamic_whitelist_refresh_interval": "bad",
            },
            [],
        )

        self.assertEqual(manager.activation_retry_interval, 30.0)
        self.assertEqual(manager.dynamic_whitelist_refresh_interval, 300.0)

    def test_scalar_whitelists_are_not_split_into_characters(self) -> None:
        manager = KillSwitchManager(
            config={
                "enabled": True,
                "additional_whitelist": "8.8.8.8",
                "dns_whitelist": "1.1.1.1",
            },
            tier1_nodes=[],
            command_runner=lambda command: subprocess.CompletedProcess(command, 0, "", ""),
        )

        self.assertEqual(manager.additional_whitelist, ["8.8.8.8"])
        self.assertEqual(manager.dns_whitelist, ["1.1.1.1"])
        self.assertTrue(manager.allow_dns)

    def test_firewall_remote_token_accepts_netsh_remoteip_forms_only(self) -> None:
        accepted = {
            "203.0.113.5": "203.0.113.5",
            " 2001:db8::1 ": "2001:db8::1",
            "10.0.0.5/8": "10.0.0.0/8",
            "192.168.1.0/255.255.255.0": "192.168.1.0/24",
            "2001:db8::/32": "2001:db8::/32",
            "192.0.2.10-192.0.2.20": "192.0.2.10-192.0.2.20",
            "2001:db8::1-2001:db8::ff": "2001:db8::1-2001:db8::ff",
            "localsubnet": "LocalSubnet",
            "DNS": "DNS",
            "dhcp": "DHCP",
            "Wins": "WINS",
            "defaultgateway": "DefaultGateway",
        }
        for value, expected in accepted.items():
            with self.subTest(value=value):
                self.assertEqual(firewall_remote_token(value), expected)
        for value in (
            "",
            "any",
            "nas.local",
            "1.1.1.1、8.8.8.8",
            "1.1.1.1,8.8.8.8",
            "192.0.2.20-192.0.2.10",
            "192.0.2.1-2001:db8::1",
            "10.0.0.0/8-10.0.0.9",
            "fe80::1%12",
            "1.1.1.1/33",
        ):
            with self.subTest(value=value):
                self.assertIsNone(firewall_remote_token(value))

    def test_invalid_additional_whitelist_entries_cannot_block_activation(self) -> None:
        commands: list[list[str]] = []

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            if any(part.startswith("remoteip=") and ("nas.local" in part or "%" in part) for part in command):
                raise subprocess.CalledProcessError(1, command, "", "invalid remoteip")
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        original_detect_admin = KillSwitchManager._detect_admin
        try:
            KillSwitchManager._detect_admin = lambda self: True
            with self.assertLogs("proxy_manager.killswitch", level="WARNING") as logs:
                manager = KillSwitchManager(
                    config={
                        "enabled": True,
                        "allow_lan": False,
                        "allow_dns": True,
                        "additional_whitelist": [
                            "1.1.1.1、8.8.8.8",
                            "nas.local",
                            "any",
                            "fe80::1%12",
                            "203.0.113.5",
                            "10.0.0.5/8",
                            "localsubnet",
                            "192.0.2.10-192.0.2.20",
                            "2001:db8::/32",
                        ],
                        "dns_whitelist": ["9.9.9.9，149.112.112.112", "fe80::1%12"],
                    },
                    tier1_nodes=[],
                    command_runner=runner,
                )
        finally:
            KillSwitchManager._detect_admin = original_detect_admin
        manager.platform_name = "windows"
        manager.state = "armed"

        self.assertEqual(len(logs.records), 1)
        self.assertIn("nas.local, any, fe80::1%12", logs.output[0])
        self.assertEqual(
            manager.additional_whitelist,
            [
                "1.1.1.1",
                "8.8.8.8",
                "203.0.113.5",
                "10.0.0.0/8",
                "LocalSubnet",
                "192.0.2.10-192.0.2.20",
                "2001:db8::/32",
            ],
        )

        self.assertTrue(manager.activate())

        joined = [" ".join(command) for command in commands]
        additional_rule = next(
            command for command in joined
            if "firewall add rule" in command and KILL_SWITCH_RULE_NAMES["additional"] in command
        )
        self.assertIn(
            "remoteip=1.1.1.1,10.0.0.0/8,192.0.2.10-192.0.2.20,2001:db8::/32,203.0.113.5,8.8.8.8,LocalSubnet",
            additional_rule,
        )
        dns_rule = next(
            command for command in joined
            if "firewall add rule" in command and KILL_SWITCH_RULE_NAMES["dns"] in command
        )
        self.assertIn("remoteip=149.112.112.112,9.9.9.9", dns_rule)
        self.assertTrue(
            any(
                "firewall add rule" in command and KILL_SWITCH_RULE_NAMES["block"] in command
                for command in joined
            )
        )
        self.assertEqual(manager.get_state()["state"], "triggered")

    def test_reconfigure_revalidates_additional_whitelist(self) -> None:
        manager = KillSwitchManager(
            config={"enabled": True, "additional_whitelist": ["203.0.113.5"]},
            tier1_nodes=[],
            command_runner=lambda command: subprocess.CompletedProcess(command, 0, "", ""),
        )

        with self.assertLogs("proxy_manager.killswitch", level="WARNING"):
            manager.reconfigure({"enabled": True, "additional_whitelist": ["nas.local", "198.51.100.7"]}, [])

        self.assertEqual(manager.additional_whitelist, ["198.51.100.7"])
        self.assertEqual(manager.get_state()["whitelist"]["additional"], ["198.51.100.7"])

    def test_tier1_hostnames_are_resolved_for_firewall_rules(self) -> None:
        commands: list[list[str]] = []

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        original_detect_admin = KillSwitchManager._detect_admin
        try:
            KillSwitchManager._detect_admin = lambda self: True
            with patch(
                "killswitch.socket.getaddrinfo",
                return_value=[(None, None, None, None, ("203.0.113.10", 0))],
            ):
                manager = KillSwitchManager(
                    config={"enabled": True, "allow_lan": False},
                    tier1_nodes=[{"address": "cdn.example.com"}],
                    command_runner=runner,
                )
                manager.platform_name = "windows"
                manager.state = "armed"
                manager.activate()
        finally:
            KillSwitchManager._detect_admin = original_detect_admin

        tier1_rule = next(
            " ".join(command)
            for command in commands
            if "firewall add rule" in " ".join(command) and "ProxyManager-KillSwitch-Allow-Tier1" in " ".join(command)
        )
        self.assertIn("203.0.113.10", tier1_rule)

    def test_cleanup_stale_rules_removes_managed_rules_while_inactive(self) -> None:
        commands: list[list[str]] = []

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        original_detect_admin = KillSwitchManager._detect_admin
        try:
            KillSwitchManager._detect_admin = lambda self: True
            manager = KillSwitchManager(
                config={"enabled": True},
                tier1_nodes=[],
                command_runner=runner,
            )
        finally:
            KillSwitchManager._detect_admin = original_detect_admin
        manager.platform_name = "windows"
        manager.state = "armed"

        self.assertTrue(manager.cleanup_stale_rules())

        joined = [" ".join(command) for command in commands]
        self.assertTrue(any("delete rule" in command and "ProxyManager-KillSwitch-Block" in command for command in joined))

    def test_force_on_sets_manual_override(self) -> None:
        original_detect_admin = KillSwitchManager._detect_admin
        try:
            KillSwitchManager._detect_admin = lambda self: True
            manager = KillSwitchManager(
                config={"enabled": True, "allow_lan": True, "additional_whitelist": []},
                tier1_nodes=[],
                command_runner=lambda command: subprocess.CompletedProcess(command, 0, "", ""),
            )
        finally:
            KillSwitchManager._detect_admin = original_detect_admin
        manager.platform_name = "windows"
        manager.state = "armed"

        manager.force_on()
        self.assertEqual(manager.get_state()["manual_override"], "forced_on")
        self.assertTrue(manager.get_state()["active"])

        manager.force_off()
        self.assertEqual(manager.get_state()["manual_override"], "none")

    def test_command_failure_disables_killswitch_without_raising(self) -> None:
        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            if "add" in command:
                raise subprocess.CalledProcessError(1, command, "", "access denied")
            return subprocess.CompletedProcess(command, 0, "", "")

        original_detect_admin = KillSwitchManager._detect_admin
        try:
            KillSwitchManager._detect_admin = lambda self: True
            manager = KillSwitchManager(
                config={"enabled": True, "allow_lan": True, "additional_whitelist": []},
                tier1_nodes=[],
                command_runner=runner,
            )
        finally:
            KillSwitchManager._detect_admin = original_detect_admin
        manager.platform_name = "windows"
        manager.state = "armed"

        self.assertFalse(manager.activate())
        state = manager.get_state()
        self.assertEqual(state["state"], "error")
        self.assertFalse(state["active"])
        self.assertIn("returned non-zero exit status", state["last_error"])

        manager._last_command_error_monotonic = 0.0
        self.assertFalse(manager.activate())

    def test_reconfigure_disable_clears_active_rules(self) -> None:
        commands: list[list[str]] = []

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        original_detect_admin = KillSwitchManager._detect_admin
        try:
            KillSwitchManager._detect_admin = lambda self: True
            manager = KillSwitchManager(
                config={"enabled": True, "allow_lan": True, "additional_whitelist": []},
                tier1_nodes=[],
                command_runner=runner,
            )
        finally:
            KillSwitchManager._detect_admin = original_detect_admin
        manager.platform_name = "windows"
        manager.state = "armed"

        manager.activate()
        commands.clear()
        manager.reconfigure({"enabled": False}, [])

        state = manager.get_state()
        self.assertEqual(state["state"], "disabled")
        self.assertFalse(state["active"])
        self.assertTrue(any("delete rule" in " ".join(command) for command in commands))

    def test_default_runner_hides_windows_console(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows-specific subprocess window flags")
        original_detect_admin = KillSwitchManager._detect_admin
        try:
            KillSwitchManager._detect_admin = lambda self: True
            manager = KillSwitchManager(config={"enabled": True}, tier1_nodes=[])
        finally:
            KillSwitchManager._detect_admin = original_detect_admin

        with patch("killswitch.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(["netsh"], 0, "", "")
            manager._default_command_runner(["netsh"])
        manager.platform_name = "test"

        kwargs = run.call_args.kwargs
        self.assertIn("creationflags", kwargs)
        self.assertIn("startupinfo", kwargs)

    def test_cleanup_uses_one_shared_deadline_for_firewall_rules(self) -> None:
        observed_timeouts: list[float] = []

        def runner(command: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
            observed_timeouts.append(float(timeout or 0.0))
            time.sleep(0.03)
            return subprocess.CompletedProcess(command, 0, "", "")

        original_detect_admin = KillSwitchManager._detect_admin
        try:
            KillSwitchManager._detect_admin = lambda self: True
            manager = KillSwitchManager(
                config={"enabled": True},
                tier1_nodes=[],
                command_runner=runner,
            )
        finally:
            KillSwitchManager._detect_admin = original_detect_admin
        manager.platform_name = "windows"

        started_at = time.monotonic()
        manager.cleanup(timeout=0.07)
        elapsed = time.monotonic() - started_at
        manager.platform_name = "test"

        self.assertLess(elapsed, 0.15)
        self.assertGreaterEqual(len(observed_timeouts), 1)
        self.assertLess(len(observed_timeouts), len(KILL_SWITCH_RULE_NAMES))
        self.assertGreater(observed_timeouts[0], observed_timeouts[-1])

    def test_close_unregisters_duplicate_atexit_cleanup(self) -> None:
        manager = KillSwitchManager(
            config={"enabled": False},
            tier1_nodes=[],
            command_runner=lambda command: subprocess.CompletedProcess(command, 0, "", ""),
        )
        with patch("killswitch.atexit.unregister") as unregister:
            manager.close(timeout=0.1)

        unregister.assert_called_once()


if __name__ == "__main__":
    unittest.main()
