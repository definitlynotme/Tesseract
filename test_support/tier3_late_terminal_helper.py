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


def late_terminal_helper_bootstrap(connection: Any) -> None:
    connection.send_bytes(BOOTSTRAP_READY_MESSAGE)
    start = json.loads(connection.recv_bytes().decode("utf-8"))["payload"]
    worker_id = int(start["worker_id"])
    generation = int(start["worker_generation"])
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
    )
    _send(
        connection,
        "ready",
        {
            "worker_id": worker_id,
            "worker_generation": generation,
            "helper_pid": os.getpid(),
            "mihomo_pid": child.pid,
            "mixed_port": int(start.get("mixed_port", 0) or 0),
            "controller_port": int(start.get("controller_port", 0) or 0),
        },
    )
    active: dict[str, Any] | None = None
    try:
        while True:
            if not connection.poll(0.02):
                continue
            envelope = json.loads(connection.recv_bytes().decode("utf-8"))
            kind = str(envelope["kind"])
            payload = dict(envelope.get("payload") or {})
            if kind == "stop":
                return
            if kind == "validate_endpoint":
                active = payload
                continue
            if kind != "cancel" or active is None:
                continue
            _send(
                connection,
                "endpoint_result",
                {
                    "refresh_epoch": active["refresh_epoch"],
                    "transaction_id": str(active.get("transaction_id") or ""),
                    "candidate_digest": active["candidate_digest"],
                    "config_digest": active["config_digest"],
                    "lease_id": active["lease_id"],
                    "slice_id": int(active.get("slice_id", 1) or 1),
                    "sequence": int(active["sequence"]),
                    "stage": str(active.get("stage") or "fast_probe"),
                    "purpose": str(active.get("purpose") or "auto_admission"),
                    "worker_id": worker_id,
                    "worker_generation": generation,
                    "kind": "pass",
                    "node_json": active["node_json"],
                    "report_json": active.get("report_json", "{}"),
                    "reason": "late_pass_after_parent_seal",
                    "latency_ms": 1.0,
                    "slow": False,
                    "probe_url": "http://fixture/late",
                    "attempt_facts": [],
                    "clean_confirmed": True,
                    "helper_pid": os.getpid(),
                    "mihomo_pid": child.pid,
                    "probe_policy_revision": int(
                        active.get("probe_policy_revision", 1) or 1
                    ),
                    "portfolio_digest": str(active.get("portfolio_digest") or ""),
                    "submitted_monotonic_ns": int(
                        active.get("submitted_monotonic_ns", 0) or 0
                    ),
                    "dispatch_monotonic_ns": int(
                        active.get("dispatch_monotonic_ns", 0) or 0
                    ),
                    "supervisor_assigned_monotonic_ns": int(
                        active.get("supervisor_assigned_monotonic_ns", 0) or 0
                    ),
                    "helper_started_monotonic_ns": time.monotonic_ns(),
                    "yield_created_monotonic_ns": time.monotonic_ns(),
                    "monotonic_ns": time.monotonic_ns(),
                },
            )
            # Deliberately omit the separate clean-confirmed message. The
            # endpoint result is late and must never regain PASS authority,
            # but its exact generation/lease clean flag is sufficient physical
            # cleanup evidence for the parent to reuse this helper.
            active = None
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=0.25)
        connection.close()
