"""Job-owned facility boot before importing the full validator runtime."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Any, Callable


class _OwnedProcess:
    def __init__(self, process: subprocess.Popen) -> None:
        self._process = process

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def returncode(self) -> int | None:
        return self._process.poll()

    def terminate(self) -> None:
        self._process.terminate()

    def kill(self) -> None:
        self._process.kill()

    async def wait(self) -> int:
        import asyncio

        # A cancelled wait must not leave an executor thread holding the child.
        while (code := self._process.poll()) is None:
            await asyncio.sleep(0.01)
        return code


class ValidatorPrewarmProcess:
    def __init__(self, payload: dict[str, Any], send: Callable[..., None]) -> None:
        self._payload = payload
        self._send = send
        self._closed = False
        self._popen: subprocess.Popen | None = None
        config = payload["prewarm_config"]
        if (
            config.get("mixed-port") != int(payload["mixed_port"])
            or config.get("external-controller") != f"127.0.0.1:{int(payload['controller_port'])}"
            or int(payload["mixed_port"]) <= 0
            or int(payload["controller_port"]) <= 0
            or int(payload["mixed_port"]) == int(payload["controller_port"])
            or config.get("allow-lan") is not False
            or config.get("mode") != "rule"
            or config.get("rules") != ["MATCH,REJECT"]
            or config.get("proxies") or config.get("proxy-groups") or config.get("tun")
        ):
            raise ValueError("prewarm config must match the reject-only facility lease")
        parent = Path(payload.get("temp_parent") or tempfile.gettempdir()).resolve()
        parent.mkdir(parents=True, exist_ok=True)
        self._parent = parent
        self.work_dir = Path(tempfile.mkdtemp(
            prefix=f"tier3-validator-{int(payload['worker_id'])}-{int(payload['worker_generation'])}-",
            dir=parent,
        )).resolve()
        try:
            path = self.work_dir / "validator-prewarm.json"
            path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            self._popen = subprocess.Popen(
                [str(payload["binary_path"]), "-f", str(path), "-d", str(self.work_dir)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
            self.process = _OwnedProcess(self._popen)
            self._publish_progress("prewarm_started")
        except BaseException:
            self.close()
            raise

    def _publish_progress(self, phase: str) -> None:
        payload = self._payload
        self._send("startup_progress", {
            "phase": phase, "worker_id": int(payload["worker_id"]),
            "worker_generation": int(payload["worker_generation"]),
            "helper_pid": os.getpid(), "mihomo_pid": self.process.pid,
            "mixed_port": int(payload["mixed_port"]),
            "controller_port": int(payload["controller_port"]),
            "monotonic_ns": time.monotonic_ns(),
        })

    def close(self) -> None:
        if self._closed:
            return
        if self._popen is not None:
            if self._popen.poll() is None:
                self._popen.kill()
            self._popen.wait(timeout=0.5)
        target = self.work_dir.resolve()
        if target.parent != self._parent or not target.name.startswith("tier3-validator-"):
            raise ValueError("prewarm cleanup escaped its owned directory")
        shutil.rmtree(target, ignore_errors=True)
        self._closed = True
