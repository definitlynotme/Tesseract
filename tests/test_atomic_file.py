from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from atomic_file import replace_with_retry


def test_replace_with_retry_preserves_atomic_replace_after_transient_permission_error() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        source = root / "payload.tmp"
        target = root / "payload.json"
        source.write_text("new", encoding="utf-8")
        target.write_text("old", encoding="utf-8")
        real_replace = os.replace
        calls = 0

        def transient_replace(src, dst):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise PermissionError("transient scanner lock")
            return real_replace(src, dst)

        with patch("atomic_file.os.replace", side_effect=transient_replace):
            replace_with_retry(source, target, initial_delay_seconds=0.001)

        assert calls == 2
        assert target.read_text(encoding="utf-8") == "new"
        assert not source.exists()

