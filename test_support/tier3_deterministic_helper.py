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


def deterministic_helper_bootstrap(connection: Any) -> None:
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
    last_heartbeat = 0.0
    try:
        while True:
            now = time.monotonic()
            if now - last_heartbeat >= 0.2:
                last_heartbeat = now
                _send(
                    connection,
                    "heartbeat",
                    {
                        "worker_id": worker_id,
                        "worker_generation": generation,
                        "helper_pid": os.getpid(),
                        "mihomo_pid": child.pid,
                    },
                )
            if not connection.poll(0.02):
                continue
            envelope = json.loads(connection.recv_bytes().decode("utf-8"))
            kind = str(envelope["kind"])
            payload = dict(envelope.get("payload") or {})
            if kind == "stop":
                return
            if kind == "cancel":
                _send(
                    connection,
                    "clean_confirmed",
                    {
                        "worker_id": worker_id,
                        "worker_generation": generation,
                        "lease_id": payload.get("lease_id", ""),
                        "sequence": int(payload.get("sequence", 0) or 0),
                        "active_probe_count": 0,
                    },
                )
                continue
            if kind != "validate_endpoint":
                continue
            try:
                report = json.loads(str(payload.get("report_json") or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                report = {}
            try:
                node_payload = json.loads(str(payload.get("node_json") or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                node_payload = {}
            fixture_delay = max(
                0.0,
                min(1.0, float(report.get("fixture_delay_seconds", 0.0) or 0.0)),
            )
            if fixture_delay > 0.0:
                time.sleep(fixture_delay)
            fixture_attempt_kind = str(
                dict(node_payload.get("extra") or {}).get(
                    "fixture_validation_outcome",
                    "",
                )
                or report.get("fixture_validation_outcome")
                or ""
            ).strip().lower()
            if fixture_attempt_kind not in {
                "pass",
                "candidate_fail",
                "censored",
                "facility_fail",
            }:
                fixture_attempt_kind = ""
            bucket = int(str(payload["candidate_digest"])[:8], 16) % 5
            stage = str(payload.get("stage") or "fast_probe")
            attempt_kind = fixture_attempt_kind or (
                "pass"
                if bucket == 0
                else "candidate_fail"
                if bucket == 1
                else "censored"
                if bucket in {2, 3}
                else "facility_fail"
            )
            common = {
                    "refresh_epoch": payload["refresh_epoch"],
                    "transaction_id": str(payload.get("transaction_id") or ""),
                    "candidate_digest": payload["candidate_digest"],
                    "config_digest": payload["config_digest"],
                    "lease_id": payload["lease_id"],
                    "slice_id": int(payload.get("slice_id", 1) or 1),
                    "sequence": int(payload["sequence"]),
                    "stage": stage,
                    "purpose": str(payload.get("purpose") or "auto_admission"),
                    "worker_id": worker_id,
                    "worker_generation": generation,
                    "node_json": payload["node_json"],
                    "report_json": payload.get("report_json", "{}"),
                    "attempt_facts": [
                        {
                            "endpoint": str(payload["endpoint"]),
                            "elapsed_ms": 1,
                            "full_budget": attempt_kind
                            not in {"censored", "facility_fail"},
                            "completed": attempt_kind != "censored",
                            "entitlement_exhausted": attempt_kind == "censored",
                            "status_code": 204 if attempt_kind == "pass" else None,
                            "outcome": (
                                "right_censored"
                                if attempt_kind == "censored"
                                else attempt_kind
                            ),
                            "failure_domain": (
                                "facility"
                                if attempt_kind == "facility_fail"
                                else "transient"
                                if attempt_kind == "censored"
                                else "candidate"
                                if attempt_kind == "candidate_fail"
                                else "none"
                            ),
                            "detail": f"deterministic_{attempt_kind}",
                            "attempt_id": f"{payload['lease_id']}:1",
                            "generation_id": f"{worker_id}:{generation}",
                            "candidate_fingerprint": payload["candidate_digest"],
                            "session_lease_id": payload["lease_id"],
                            "stage": stage,
                            "transport_started_monotonic_ns": time.monotonic_ns(),
                            "granted_budget_ms": int(
                                float(payload.get("probe_budget_seconds", 1.0))
                                * 1000
                            ),
                            "lifecycle": (
                                "censored" if attempt_kind == "censored" else "completed"
                            ),
                        }
                    ],
                    "facility_elapsed_ms": 1,
                    "portfolio_elapsed_ms": 1,
                    "total_elapsed_ms": 2,
                    "clean_confirmed": True,
                    "helper_pid": os.getpid(),
                    "mihomo_pid": child.pid,
                    "probe_policy_revision": int(
                        payload.get("probe_policy_revision", 1) or 1
                    ),
                    "portfolio_digest": str(payload.get("portfolio_digest") or ""),
                    "submitted_monotonic_ns": int(
                        payload.get("submitted_monotonic_ns", 0) or 0
                    ),
                    "dispatch_monotonic_ns": int(
                        payload.get("dispatch_monotonic_ns", 0) or 0
                    ),
                    "supervisor_assigned_monotonic_ns": int(
                        payload.get("supervisor_assigned_monotonic_ns", 0) or 0
                    ),
                    "supervisor_sent_monotonic_ns": int(
                        payload.get("supervisor_sent_monotonic_ns", 0) or 0
                    ),
                    "helper_started_monotonic_ns": time.monotonic_ns(),
                    "monotonic_ns": time.monotonic_ns(),
            }
            _send(
                connection,
                "endpoint_result",
                {
                    **common,
                    "kind": (
                        "right_censored"
                        if attempt_kind == "censored"
                        else attempt_kind
                    ),
                    "reason": f"deterministic_{attempt_kind}",
                    "latency_ms": 25.0 if attempt_kind == "pass" else None,
                    "slow": False,
                    "probe_url": (
                        str(payload.get("endpoint") or "")
                        if attempt_kind == "pass"
                        else ""
                    ),
                    "yield_created_monotonic_ns": time.monotonic_ns(),
                },
            )
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=0.25)
        connection.close()


def deterministic_no_child_bootstrap(connection: Any) -> None:
    """Minimal ready helper for port/startup tests that do not need a child."""

    connection.send_bytes(BOOTSTRAP_READY_MESSAGE)
    start = json.loads(connection.recv_bytes().decode("utf-8"))["payload"]
    worker_id = int(start["worker_id"])
    generation = int(start["worker_generation"])
    process_id = os.getpid()
    _send(
        connection,
        "ready",
        {
            "worker_id": worker_id,
            "worker_generation": generation,
            "helper_pid": process_id,
            "mihomo_pid": process_id,
            "mixed_port": int(start.get("mixed_port", 0) or 0),
            "controller_port": int(start.get("controller_port", 0) or 0),
        },
    )
    last_heartbeat = 0.0
    try:
        while True:
            now = time.monotonic()
            if now - last_heartbeat >= 0.2:
                last_heartbeat = now
                _send(
                    connection,
                    "heartbeat",
                    {
                        "worker_id": worker_id,
                        "worker_generation": generation,
                        "helper_pid": process_id,
                        "mihomo_pid": process_id,
                    },
                )
            if not connection.poll(0.02):
                continue
            envelope = json.loads(connection.recv_bytes().decode("utf-8"))
            if str(envelope.get("kind") or "") == "stop":
                return
    finally:
        connection.close()
