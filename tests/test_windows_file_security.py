from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from windows_file_security import (
    current_user_access_warnings,
    restrict_to_current_user,
)


class WindowsFileSecurityTests(unittest.TestCase):
    def test_existing_file_and_directory_can_be_restricted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            private_dir = Path(temp_dir) / "private"
            private_dir.mkdir()
            private_file = private_dir / "config.local.yaml"
            private_file.write_text("alerts: {}\n", encoding="utf-8")

            directory_result = restrict_to_current_user(private_dir)
            file_result = restrict_to_current_user(private_file)

            self.assertTrue(directory_result.confirmed, directory_result.detail)
            self.assertTrue(file_result.confirmed, file_result.detail)
            warning_paths = {entry.path for entry in current_user_access_warnings()}
            self.assertNotIn(str(private_dir.resolve()), warning_paths)
            self.assertNotIn(str(private_file.resolve()), warning_paths)

    def test_missing_path_is_reported_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing.yaml"
            result = restrict_to_current_user(missing)

            self.assertFalse(result.confirmed)
            self.assertIn("does not exist", result.detail)
            warning_paths = {entry.path for entry in current_user_access_warnings()}
            self.assertIn(str(missing.resolve()), warning_paths)
