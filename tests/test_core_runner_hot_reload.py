"""Tests for the hot_reload method on MihomoCoreRunner."""
from __future__ import annotations

import io
import unittest
from unittest.mock import MagicMock, patch

from core_runner import MihomoCoreRunner


class DummyProcess:
    def __init__(self, alive: bool = True) -> None:
        self._alive = alive
        self.stdout = io.StringIO("")

    def poll(self):
        return None if self._alive else 0

    @property
    def pid(self):
        return 4242


class HotReloadTests(unittest.TestCase):
    def _runner(self, *, project_root: str, alive: bool = True) -> MihomoCoreRunner:
        runner = MihomoCoreRunner(
            binary_path="bin/mihomo.exe",
            runtime_config_path="data/mihomo-runtime.yaml",
            work_dir="data/mihomo",
            project_root=project_root,
        )
        runner.process = DummyProcess(alive=alive)
        return runner

    def test_hot_reload_returns_false_when_core_not_running(self, tmp_path=None) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            runner = self._runner(project_root=tmp, alive=False)
            result = runner.hot_reload(controller_host="127.0.0.1", controller_port=9090)
            self.assertFalse(result["reloaded"])
            self.assertEqual(result["reason"], "core not running")

    def test_hot_reload_issues_put_with_bearer_token(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            runner = self._runner(project_root=tmp, alive=True)

            captured = {}

            class FakeResp:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return b""

            def fake_urlopen(request, timeout=None):
                captured["method"] = request.get_method()
                captured["url"] = request.full_url
                captured["body"] = request.data
                captured["headers"] = dict(request.header_items())
                captured["timeout"] = timeout
                return FakeResp()

            with patch("core_runner.urlopen", fake_urlopen):
                result = runner.hot_reload(
                    controller_host="127.0.0.1",
                    controller_port=9090,
                    controller_secret="abc",
                    timeout=2.5,
                )
            self.assertTrue(result["reloaded"])
            self.assertEqual(captured["method"], "PUT")
            self.assertIn("/configs?force=true", captured["url"])
            normalized = {k.lower(): v for k, v in captured["headers"].items()}
            self.assertEqual(normalized["authorization"], "Bearer abc")
            self.assertEqual(captured["timeout"], 2.5)

    def test_hot_reload_captures_http_failure(self) -> None:
        import tempfile
        from urllib.error import URLError

        with tempfile.TemporaryDirectory() as tmp:
            runner = self._runner(project_root=tmp, alive=True)

            def failing_urlopen(request, timeout=None):
                raise URLError("connection refused")

            with patch("core_runner.urlopen", failing_urlopen):
                result = runner.hot_reload(controller_host="127.0.0.1", controller_port=9090)
            self.assertFalse(result["reloaded"])
            self.assertIn("connection refused", str(result.get("reason")))

    def test_hot_reload_includes_http_error_body(self) -> None:
        import tempfile
        from urllib.error import HTTPError

        with tempfile.TemporaryDirectory() as tmp:
            runner = self._runner(project_root=tmp, alive=True)

            def failing_urlopen(request, timeout=None):
                raise HTTPError(
                    request.full_url,
                    400,
                    "Bad Request",
                    hdrs=None,
                    fp=io.BytesIO(b"proxy 3: unsupported cipher"),
                )

            with patch("core_runner.urlopen", failing_urlopen):
                result = runner.hot_reload(controller_host="127.0.0.1", controller_port=9090)

            self.assertFalse(result["reloaded"])
            self.assertIn("HTTP Error 400: Bad Request", str(result.get("reason")))
            self.assertIn("unsupported cipher", str(result.get("reason")))


if __name__ == "__main__":
    unittest.main()
