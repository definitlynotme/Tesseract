import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime_state import RuntimeStateStore, build_restore_proxy_script, stale_system_proxy_recovery_plan
from system_proxy import ProxySettings, _proxy_transaction


class RuntimeStateTests(unittest.TestCase):
    def test_save_load_and_clear_proxy_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, _proxy_transaction():
            path = Path(temp_dir) / "runtime-state.json"
            store = RuntimeStateStore(path)

            store.save_proxy_snapshot(
                original=ProxySettings(enabled=True, server="old:8080", override="<local>"),
                target="127.0.0.1:7890",
            )
            payload = store.load()

            self.assertEqual(payload["system_proxy"]["target"], "127.0.0.1:7890")
            self.assertEqual(payload["system_proxy"]["original"]["server"], "old:8080")
            store.clear()
            self.assertFalse(path.exists())

    def test_save_proxy_snapshot_cleans_temp_file_when_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, _proxy_transaction():
            path = Path(temp_dir) / "runtime-state.json"
            store = RuntimeStateStore(path)

            with patch("runtime_state.os.replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    store.save_proxy_snapshot(
                        original=ProxySettings(enabled=True, server="old:8080", override="<local>"),
                        target="127.0.0.1:7890",
                    )

            self.assertFalse(path.exists())
            self.assertEqual(list(Path(temp_dir).glob(".runtime-state.json.*.tmp")), [])

    def test_restore_proxy_script_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "restore_proxy.bat"

            build_restore_proxy_script(path)
            text = path.read_text(encoding="utf-8")

            self.assertIn("ProxyEnable", text)
            self.assertIn("Current-user system proxy disabled", text)

    def test_stale_system_proxy_recovery_plan_restores_matching_active_target(self) -> None:
        plan = stale_system_proxy_recovery_plan(
            {
                "system_proxy": {
                    "target": "127.0.0.1:7890",
                    "original": {"enabled": "true", "server": "old:8080", "override": "<local>"},
                }
            },
            current_target="127.0.0.1:7890",
            target_active=True,
        )

        self.assertTrue(plan.should_restore)
        self.assertTrue(plan.should_clear)
        assert plan.original is not None
        self.assertTrue(plan.original.enabled)
        self.assertEqual(plan.original.server, "old:8080")
        self.assertEqual(plan.original.override, "<local>")

    def test_stale_system_proxy_recovery_plan_ignores_mismatched_or_missing_state(self) -> None:
        missing = stale_system_proxy_recovery_plan({}, current_target="127.0.0.1:7890", target_active=True)
        mismatched = stale_system_proxy_recovery_plan(
            {"system_proxy": {"target": "127.0.0.1:7891", "original": {"server": "old:8080"}}},
            current_target="127.0.0.1:7890",
            target_active=True,
        )

        self.assertFalse(missing.should_restore)
        self.assertFalse(missing.should_clear)
        self.assertFalse(mismatched.should_restore)
        self.assertFalse(mismatched.should_clear)

    def test_stale_system_proxy_recovery_plan_clears_matching_inactive_target(self) -> None:
        plan = stale_system_proxy_recovery_plan(
            {"system_proxy": {"target": "127.0.0.1:7890", "original": {"server": "old:8080"}}},
            current_target="127.0.0.1:7890",
            target_active=False,
        )

        self.assertFalse(plan.should_restore)
        self.assertTrue(plan.should_clear)
        self.assertIsNone(plan.original)


if __name__ == "__main__":
    unittest.main()
