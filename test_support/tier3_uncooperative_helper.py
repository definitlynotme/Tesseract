from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any

from tier3_validation_protocol import PROTOCOL_SCHEMA_VERSION
from tier3_validator_helper import BOOTSTRAP_READY_MESSAGE


def _send(connection: Any, kind: str, payload: dict[str, Any]) -> None:
    connection.send_bytes(
        json.dumps(
            {"schema": PROTOCOL_SCHEMA_VERSION, "kind": kind, "payload": payload},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def uncooperative_helper_bootstrap(connection: Any) -> None:
    """Test fixture that ignores cancellation until its Job is terminated."""

    connection.send_bytes(BOOTSTRAP_READY_MESSAGE)
    raw = json.loads(connection.recv_bytes().decode("utf-8"))
    payload = dict(raw["payload"])
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
    )
    worker_id = int(payload["worker_id"])
    generation = int(payload["worker_generation"])
    _send(
        connection,
        "ready",
        {
            "worker_id": worker_id,
            "worker_generation": generation,
            "helper_pid": os.getpid(),
            "mihomo_pid": int(child.pid),
            "mixed_port": int(payload.get("mixed_port", 0) or 0),
            "controller_port": int(payload.get("controller_port", 0) or 0),
        },
    )
    last_heartbeat = 0.0
    try:
        while True:
            now = time.monotonic()
            if now - last_heartbeat >= 0.1:
                last_heartbeat = now
                _send(
                    connection,
                    "heartbeat",
                    {
                        "worker_id": worker_id,
                        "worker_generation": generation,
                        "helper_pid": os.getpid(),
                        "mihomo_pid": int(child.pid),
                    },
                )
            if connection.poll(0.02):
                kind = str(json.loads(connection.recv_bytes().decode("utf-8"))["kind"])
                # validate, cancel and stop are deliberately ignored.  The
                # fixture exits only when its Job is physically terminated.
    finally:
        try:
            child.terminate()
        except OSError:
            pass
        try:
            connection.close()
        except OSError:
            pass
