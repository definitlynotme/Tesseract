import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alerts import flush_logging_handlers, setup_logging
from autostart import AutostartManager, default_autostart_manager
from config_store import ConfigStore


class LogRotationTests(unittest.TestCase):
    def test_setup_logging_uses_rotating_handler(self) -> None:
        import logging.handlers as handlers

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "monitor.log"
            logger = setup_logging(str(log_path), level="INFO")
            try:
                file_handlers = [
                    handler for handler in logger.handlers if isinstance(handler, logging.FileHandler)
                ]
                self.assertTrue(
                    any(isinstance(handler, handlers.RotatingFileHandler) for handler in file_handlers),
                    "expected a RotatingFileHandler in proxy_manager logger",
                )
            finally:
                for handler in list(logger.handlers):
                    handler.close()
                    logger.removeHandler(handler)

    def test_flush_logging_handlers_reaches_proxy_manager_sink(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "monitor.log"
            runtime_logger = setup_logging(str(log_path), level="INFO")
            child_logger = logging.getLogger("proxy_manager.tier3-live")
            child_logger.handlers.clear()
            child_logger.propagate = True
            try:
                child_logger.info("rolling expansion active")
                self.assertGreaterEqual(flush_logging_handlers(child_logger), 1)
                self.assertIn(
                    "rolling expansion active",
                    log_path.read_text(encoding="utf-8"),
                )
            finally:
                for handler in list(runtime_logger.handlers):
                    handler.close()
                    runtime_logger.removeHandler(handler)


class ConfigStoreUrlRedactionTests(unittest.TestCase):
    def test_summarize_url_strips_userinfo(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ConfigStore(
                local_config_path=Path(temp_dir) / "local.yaml",
                sources_path=Path(temp_dir) / "sources.yaml",
                backup_dir=Path(temp_dir) / "backups",
            )
            summary = store._summarize_url("https://secret_token@sub.example.com:8443/feed?token=xyz")
            self.assertEqual(summary["host"], "sub.example.com:8443")
            self.assertNotIn("secret_token", summary["host"])
            self.assertNotIn("secret_token", summary["fingerprint"])

    def test_summarize_url_missing_port(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ConfigStore(
                local_config_path=Path(temp_dir) / "local.yaml",
                sources_path=Path(temp_dir) / "sources.yaml",
                backup_dir=Path(temp_dir) / "backups",
            )
            summary = store._summarize_url("https://user:pass@example.com/sub")
            self.assertEqual(summary["host"], "example.com")


class AutostartManagerTests(unittest.TestCase):
    def test_launch_command_quotes_paths_with_spaces(self) -> None:
        manager = AutostartManager(
            executable=r"C:\\Program Files\\Tesseract\\Tesseract.exe",
        )
        command = manager.launch_command()
        self.assertTrue(command.startswith('"'))
        self.assertIn("Tesseract.exe", command)

    def test_non_windows_status_is_disabled(self) -> None:
        manager = AutostartManager(executable=sys.executable)
        with patch("autostart.platform.system", return_value="Linux"):
            status = manager.status()
        self.assertFalse(status["supported"])
        self.assertFalse(status["enabled"])

    def test_default_manager_uses_current_interpreter(self) -> None:
        manager = default_autostart_manager()
        command = manager.launch_command()
        self.assertTrue(command)
        # The executable portion should always be quoted.
        self.assertTrue(command.startswith('"'))


if __name__ == "__main__":
    unittest.main()
