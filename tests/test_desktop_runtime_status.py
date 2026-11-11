import tempfile
import unittest
from pathlib import Path

from desktop_runtime_status import (
    default_core_status,
    desktop_runtime_status_payload,
    runtime_state_status,
    subscription_auth_required,
)


class DesktopRuntimeStatusTests(unittest.TestCase):
    def test_default_core_status_preserves_runtime_contract(self) -> None:
        self.assertEqual(default_core_status(), {"running": False, "pid": None, "recent_logs": []})

    def test_subscription_auth_required_only_uses_dict_token(self) -> None:
        self.assertFalse(subscription_auth_required(None))
        self.assertFalse(subscription_auth_required([]))
        self.assertFalse(subscription_auth_required({}))
        self.assertFalse(subscription_auth_required({"token": ""}))
        self.assertTrue(subscription_auth_required({"token": "secret"}))

    def test_runtime_state_status_preserves_path_and_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runtime-state.json"
            self.assertEqual(runtime_state_status(path), {"path": str(path), "exists": False})
            path.write_text("{}", encoding="utf-8")
            self.assertEqual(runtime_state_status(path), {"path": str(path), "exists": True})

    def test_desktop_runtime_status_payload_preserves_top_level_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_state_path = Path(temp_dir) / "runtime-state.json"
            runtime_state_path.write_text("{}", encoding="utf-8")

            payload = desktop_runtime_status_payload(
                running=True,
                last_error="last",
                clash_config_url="http://127.0.0.1:25500/clash/config",
                subscription_config={"token": "secret"},
                core_status={"running": True},
                system_proxy_state={"active": True},
                network_mode="system",
                is_admin=False,
                runtime_state_path=runtime_state_path,
                geo_update_status={"enabled": True},
                autostart_status={"enabled": False},
                runtime_snapshot={"system": {"mode": "tier1_healthy"}},
            )

            self.assertEqual(payload["running"], True)
            self.assertEqual(payload["last_error"], "last")
            self.assertEqual(payload["clash_config_url"], "http://127.0.0.1:25500/clash/config")
            self.assertTrue(payload["subscription_auth_required"])
            self.assertEqual(payload["core"], {"running": True})
            self.assertEqual(payload["system_proxy"], {"active": True})
            self.assertEqual(payload["network_mode"], "system")
            self.assertFalse(payload["is_admin"])
            self.assertEqual(payload["runtime_state"], {"path": str(runtime_state_path), "exists": True})
            self.assertEqual(payload["geo_update"], {"enabled": True})
            self.assertEqual(payload["autostart"], {"enabled": False})
            self.assertEqual(payload["runtime"], {"system": {"mode": "tier1_healthy"}})


if __name__ == "__main__":
    unittest.main()
