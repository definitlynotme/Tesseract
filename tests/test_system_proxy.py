import unittest

from system_proxy import ProxySettings, SystemProxyManager


class MemoryProxyBackend:
    def __init__(self, settings: ProxySettings | None = None, fail_write: bool = False) -> None:
        self.settings = settings or ProxySettings(enabled=False, server="", override="")
        self.fail_write = fail_write
        self.writes: list[ProxySettings] = []

    def read(self) -> ProxySettings:
        return ProxySettings(
            enabled=self.settings.enabled,
            server=self.settings.server,
            override=self.settings.override,
        )

    def write(self, settings: ProxySettings) -> None:
        if self.fail_write:
            raise RuntimeError("write failed")
        self.settings = settings
        self.writes.append(settings)


class SystemProxyTests(unittest.TestCase):
    def test_enable_records_original_and_sets_proxy(self) -> None:
        backend = MemoryProxyBackend(ProxySettings(enabled=True, server="old:8080", override="<local>"))
        manager = SystemProxyManager(
            {"enabled": True, "host": "127.0.0.1", "port": 7890, "bypass": ["localhost", "<local>"]},
            backend=backend,
            platform_name="windows",
        )

        state = manager.enable()

        self.assertTrue(state["active"])
        self.assertTrue(state["restorable"])
        self.assertEqual(backend.settings.server, "127.0.0.1:7890")
        self.assertEqual(backend.settings.override, "localhost;<local>")

    def test_enable_filters_public_bypass_before_writing_windows_override(self) -> None:
        backend = MemoryProxyBackend()
        manager = SystemProxyManager(
            {
                "enabled": True,
                "bypass": [
                    "localhost",
                    "example.com",
                    "8.8.8.8",
                    "printer.local",
                    "10.*",
                    "10.0.0.0/8",
                ],
            },
            backend=backend,
            platform_name="windows",
        )

        manager.enable()

        self.assertEqual(
            backend.settings.override,
            "localhost;printer.local;10.*",
        )

    def test_restore_returns_original_settings(self) -> None:
        backend = MemoryProxyBackend(ProxySettings(enabled=True, server="old:8080", override="<local>"))
        manager = SystemProxyManager({"enabled": True}, backend=backend, platform_name="windows")

        manager.enable()
        state = manager.restore()

        self.assertFalse(state["active"])
        self.assertFalse(state["restorable"])
        self.assertTrue(backend.settings.enabled)
        self.assertEqual(backend.settings.server, "old:8080")
        self.assertEqual(backend.settings.override, "<local>")

    def test_disable_preserves_unmanaged_proxy_without_original_snapshot(self) -> None:
        backend = MemoryProxyBackend(ProxySettings(enabled=True, server="old:8080", override="<local>"))
        manager = SystemProxyManager({"enabled": True}, backend=backend, platform_name="windows")

        manager.disable()

        self.assertTrue(backend.settings.enabled)
        self.assertEqual(backend.settings.server, "old:8080")
        self.assertEqual(backend.settings.override, "<local>")

    def test_disable_restores_original_proxy_snapshot(self) -> None:
        backend = MemoryProxyBackend(ProxySettings(enabled=True, server="old:8080", override="<local>"))
        manager = SystemProxyManager({"enabled": True}, backend=backend, platform_name="windows")

        manager.enable()
        state = manager.disable()

        self.assertFalse(state["active"])
        self.assertFalse(state["restorable"])
        self.assertTrue(backend.settings.enabled)
        self.assertEqual(backend.settings.server, "old:8080")
        self.assertEqual(backend.settings.override, "<local>")

    def test_disable_clears_managed_target_without_original_snapshot(self) -> None:
        backend = MemoryProxyBackend(ProxySettings(enabled=True, server="127.0.0.1:7890", override="<local>"))
        manager = SystemProxyManager({"enabled": True, "host": "127.0.0.1", "port": 7890}, backend=backend, platform_name="windows")

        manager.disable()

        self.assertFalse(backend.settings.enabled)
        self.assertEqual(backend.settings.server, "")
        self.assertEqual(backend.settings.override, "")

    def test_write_failure_keeps_original_snapshot(self) -> None:
        backend = MemoryProxyBackend(ProxySettings(enabled=True, server="old:8080", override="<local>"), fail_write=True)
        manager = SystemProxyManager({"enabled": True}, backend=backend, platform_name="windows")

        with self.assertRaises(RuntimeError):
            manager.enable()

        self.assertTrue(manager.get_state()["restorable"])
        self.assertIn("write failed", manager.get_state()["last_error"])

    def test_non_windows_is_unsupported(self) -> None:
        manager = SystemProxyManager({"enabled": True}, backend=None, platform_name="linux")

        state = manager.enable()

        self.assertFalse(state["supported"])
        self.assertFalse(state["active"])

    def test_string_false_disables_system_proxy_manager(self) -> None:
        backend = MemoryProxyBackend(ProxySettings(enabled=False, server="", override=""))
        manager = SystemProxyManager({"enabled": "false"}, backend=backend, platform_name="windows")

        state = manager.enable()

        self.assertFalse(state["enabled"])
        self.assertFalse(state["active"])
        self.assertEqual(backend.writes, [])

    def test_invalid_port_falls_back_without_crashing(self) -> None:
        manager = SystemProxyManager({"enabled": True, "port": "bad"}, backend=None, platform_name="linux")

        self.assertEqual(manager.port, 7890)
        self.assertEqual(manager.proxy_server(), "127.0.0.1:7890")

    def test_scalar_bypass_is_not_split_into_characters(self) -> None:
        manager = SystemProxyManager({"enabled": True, "bypass": "<local>"}, backend=None, platform_name="linux")

        self.assertEqual(manager.bypass, ["<local>"])
        self.assertEqual(manager.proxy_override(), "<local>")

    def test_is_target_active(self) -> None:
        backend = MemoryProxyBackend(ProxySettings(enabled=True, server="127.0.0.1:7890", override="<local>"))
        manager = SystemProxyManager(
            {"enabled": True, "host": "127.0.0.1", "port": 7890},
            backend=backend,
            platform_name="windows",
        )

        self.assertTrue(manager.is_target_active())

    def test_restore_settings_writes_explicit_snapshot(self) -> None:
        backend = MemoryProxyBackend(ProxySettings(enabled=True, server="127.0.0.1:7890", override="<local>"))
        manager = SystemProxyManager({"enabled": True}, backend=backend, platform_name="windows")

        manager.restore_settings(ProxySettings(enabled=True, server="old:8080", override="old"))

        self.assertEqual(backend.settings.server, "old:8080")
        self.assertEqual(backend.settings.override, "old")
        self.assertFalse(manager.get_state()["restorable"])

    def test_apply_proxy_override_rewrites_only_override_while_target_is_live(self) -> None:
        backend = MemoryProxyBackend(ProxySettings(enabled=True, server="old:8080", override="<local>"))
        manager = SystemProxyManager(
            {"enabled": True, "host": "127.0.0.1", "port": 7890, "bypass": ["localhost"]},
            backend=backend,
            platform_name="windows",
        )
        manager.enable()
        manager.bypass = ["localhost", "printer.local"]

        result = manager.apply_proxy_override()

        self.assertEqual(result, {"applied": True, "reason": "written"})
        self.assertEqual(
            backend.settings,
            ProxySettings(enabled=True, server="127.0.0.1:7890", override="localhost;printer.local"),
        )
        self.assertEqual(len(backend.writes), 2)
        state = manager.get_state()
        self.assertTrue(state["active"])
        self.assertTrue(state["restorable"])
        self.assertIsNone(state["last_error"])

        manager.restore()

        self.assertEqual(
            backend.settings,
            ProxySettings(enabled=True, server="old:8080", override="<local>"),
        )

    def test_apply_proxy_override_skips_write_when_windows_already_matches(self) -> None:
        backend = MemoryProxyBackend()
        manager = SystemProxyManager({"enabled": True, "bypass": ["localhost"]}, backend=backend, platform_name="windows")
        manager.enable()

        self.assertEqual(manager.apply_proxy_override(), {"applied": True, "reason": "unchanged"})
        self.assertEqual(len(backend.writes), 1)

    def test_apply_proxy_override_never_takes_over_another_clients_proxy(self) -> None:
        backend = MemoryProxyBackend()
        manager = SystemProxyManager(
            {"enabled": True, "host": "127.0.0.1", "port": 7890, "bypass": ["localhost"]},
            backend=backend,
            platform_name="windows",
        )
        manager.enable()
        other_client = ProxySettings(enabled=True, server="127.0.0.1:10808", override="<local>")
        backend.settings = other_client
        manager.bypass = ["localhost", "printer.local"]

        result = manager.apply_proxy_override()

        self.assertEqual(result, {"applied": False, "reason": "not_target"})
        self.assertEqual(backend.settings, other_client)
        self.assertEqual(len(backend.writes), 1)
        self.assertEqual(manager.bypass, ["localhost", "printer.local"])

    def test_apply_proxy_override_waits_for_next_enable_without_live_capture(self) -> None:
        # HKCU happens to name our target, but this process has not captured it.
        backend = MemoryProxyBackend(ProxySettings(enabled=True, server="127.0.0.1:7890", override="<local>"))
        idle = SystemProxyManager(
            {"enabled": True, "host": "127.0.0.1", "port": 7890, "bypass": ["localhost"]},
            backend=backend,
            platform_name="windows",
        )
        disabled = SystemProxyManager({"enabled": False}, backend=backend, platform_name="windows")
        disabled.active = True
        unsupported = SystemProxyManager({"enabled": True}, backend=None, platform_name="linux")

        self.assertEqual(idle.apply_proxy_override(), {"applied": False, "reason": "inactive"})
        self.assertEqual(disabled.apply_proxy_override(), {"applied": False, "reason": "disabled"})
        self.assertEqual(unsupported.apply_proxy_override(), {"applied": False, "reason": "unsupported"})
        self.assertEqual(backend.writes, [])
        self.assertEqual(backend.settings.override, "<local>")

        idle.enable()

        self.assertEqual(backend.settings.override, "localhost")

    def test_apply_proxy_override_write_failure_records_error(self) -> None:
        backend = MemoryProxyBackend()
        manager = SystemProxyManager({"enabled": True, "bypass": ["localhost"]}, backend=backend, platform_name="windows")
        manager.enable()
        manager.bypass = ["printer.local"]
        backend.fail_write = True

        with self.assertRaises(RuntimeError):
            manager.apply_proxy_override()

        self.assertIn("write failed", manager.get_state()["last_error"])
        self.assertEqual(backend.settings.override, "localhost")
        self.assertTrue(manager.active)


if __name__ == "__main__":
    unittest.main()
