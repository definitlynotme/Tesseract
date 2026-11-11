import tempfile
import unittest
from pathlib import Path

from runtime_config_file import read_existing_runtime_config_text, restore_runtime_config_text


class RuntimeConfigFileTests(unittest.TestCase):
    def test_read_existing_runtime_config_text_returns_none_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            self.assertIsNone(read_existing_runtime_config_text(Path(temp_dir) / "missing.yaml"))

    def test_read_existing_runtime_config_text_reads_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "mihomo-runtime.yaml"
            path.write_text("mixed-port: 7890\n", encoding="utf-8")

            self.assertEqual(read_existing_runtime_config_text(path), "mixed-port: 7890\n")

    def test_restore_runtime_config_text_writes_previous_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "data" / "mihomo-runtime.yaml"

            self.assertTrue(restore_runtime_config_text(path, "old-config"))
            self.assertEqual(path.read_text(encoding="utf-8"), "old-config")

    def test_restore_runtime_config_text_skips_missing_previous_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "data" / "mihomo-runtime.yaml"

            self.assertFalse(restore_runtime_config_text(path, None))
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
