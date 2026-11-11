from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app_update_state import (
    completed_update_notice,
    mark_update_notice_shown,
    record_pending_update,
    update_state_path,
)


def _downloaded_update(*, verified: bool = True):
    update = SimpleNamespace(
        current_version="0.1.0",
        latest_version="0.2.0",
        release_url="https://github.com/definitlynotme/Tessract/releases/tag/v0.2.0",
        release_notes="Fixed privacy leaks.\n\nSee the release page for details.",
    )
    return SimpleNamespace(
        update=update,
        sha256="a" * 64,
        manifest_key_id="sha256:" + "b" * 64,
        manifest_verified=verified,
    )


class AppUpdateStateTests(unittest.TestCase):
    def test_pending_receipt_is_visible_only_after_target_version_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = update_state_path(Path(temp_dir))
            record_pending_update(path, _downloaded_update())

            self.assertIsNone(completed_update_notice(path, "0.1.0"))
            notice = completed_update_notice(path, "0.2.0")

            self.assertIsNotNone(notice)
            assert notice is not None
            self.assertEqual(notice.previous_version, "0.1.0")
            self.assertEqual(notice.installed_version, "0.2.0")
            self.assertTrue(notice.manifest_verified)
            self.assertEqual(notice.installer_sha256, "a" * 64)

    def test_notice_is_marked_once_after_dialog_closes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = update_state_path(Path(temp_dir))
            record_pending_update(path, _downloaded_update())
            mark_update_notice_shown(path, "0.2.0")

            self.assertIsNone(completed_update_notice(path, "0.2.0"))
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["shown_versions"], ["0.2.0"])
            self.assertNotIn("pending", payload)

    def test_unverified_download_is_never_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = update_state_path(Path(temp_dir))
            with self.assertRaisesRegex(ValueError, "verified release manifest"):
                record_pending_update(path, _downloaded_update(verified=False))
            self.assertFalse(path.exists())

    def test_corrupt_state_fails_closed_without_startup_notice(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = update_state_path(Path(temp_dir))
            path.write_text("{", encoding="utf-8")
            self.assertIsNone(completed_update_notice(path, "0.2.0"))

    def test_state_write_finishes_with_atomic_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = update_state_path(Path(temp_dir))
            with patch("app_update_state.os.replace", wraps=__import__("os").replace) as replace:
                record_pending_update(path, _downloaded_update())
            replace.assert_called_once()


if __name__ == "__main__":
    unittest.main()
