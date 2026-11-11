from __future__ import annotations

import json
import os
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


def slow_progress_helper_bootstrap(connection: Any) -> None:
    connection.send_bytes(BOOTSTRAP_READY_MESSAGE)
    start = json.loads(connection.recv_bytes().decode("utf-8"))["payload"]
    worker_id = int(start["worker_id"])
    generation = int(start["worker_generation"])
    try:
        for phase in ("runtime_imported", "mihomo_starting"):
            time.sleep(0.7)
            _send(
                connection,
                "startup_progress",
                {
                    "worker_id": worker_id,
                    "worker_generation": generation,
                    "phase": phase,
                    "helper_pid": os.getpid(),
                },
            )
        time.sleep(0.7)
        _send(
            connection,
            "startup_progress",
            {
                "worker_id": worker_id,
                "worker_generation": generation,
                "phase": "mihomo_started",
                "helper_pid": os.getpid(),
                "mihomo_pid": os.getpid(),
            },
        )
        time.sleep(0.7)
        _send(
            connection,
            "ready",
            {
                "worker_id": worker_id,
                "worker_generation": generation,
                "helper_pid": os.getpid(),
                "mihomo_pid": os.getpid(),
                "mixed_port": int(start.get("mixed_port", 0) or 0),
                "controller_port": int(start.get("controller_port", 0) or 0),
            },
        )
        while True:
            if not connection.poll(0.05):
                continue
            envelope = json.loads(connection.recv_bytes().decode("utf-8"))
            if str(envelope.get("kind") or "") == "stop":
                return
    finally:
        connection.close()
