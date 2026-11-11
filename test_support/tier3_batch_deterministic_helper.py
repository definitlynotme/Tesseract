"""Deterministic fake for the batch validator helper (K identities per process).

Speaks the batch supervisor's wire protocol by hand, exactly like
``tier3_deterministic_helper`` does for the single-slot supervisor, so the
supervisor's group/slot machinery can be tested without Mihomo.  There are no
rounds: each ``validate_endpoint`` is answered per slot after a simulated
hot-install.

Verdicts follow the same digest buckets as the single-slot fake.  Per-node
fixtures in ``node.extra``:

* ``fixture_validation_outcome``: pass | candidate_fail | censored | facility_fail
* ``fixture_delay_seconds``: answer after this delay (slots run concurrently)
* ``fixture_cancel_unclean``: never confirm a cancel clean (slot poison)
* ``fixture_crash_helper``: the helper process dies when this candidate is
  dispatched (process-level loss with in-flight siblings)
* ``fixture_slot_unusable``: the lease fails facility and the helper reports
  the identity unusable (``slot_unusable``)
* ``fixture_cancel_silent``: a cancel drops the lease without a
  ``clean_confirmed``; only the next heartbeat (active_probe_count 0) can
  complete the parent's CANCELLING identity

Liveness is one ``group_heartbeat`` frame per process, like the production
helper.  Process-wide switches, read from the environment the spawned helper
inherits:

* ``TIER3_BATCH_FAKE_SLOT_HEARTBEATS=1``: send one ``heartbeat`` per identity
  instead (the older wire form the parent still accepts)
* ``TIER3_BATCH_FAKE_LAYOUT_SECONDS=<s>``: between ``ready`` and
  ``batch_ready``, sleep without reading the Pipe (a slow listener layout)
* ``TIER3_BATCH_FAKE_SKIP_BATCH_READY=1``: never send ``batch_ready``
"""
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


def _node_extra(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        node_payload = json.loads(str(payload.get("node_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    extra = node_payload.get("extra") if isinstance(node_payload, dict) else None
    return dict(extra) if isinstance(extra, dict) else {}


def _result_payload(
    payload: dict[str, Any],
    *,
    worker_id: int,
    generation: int,
    mihomo_pid: int,
    attempt_kind: str,
) -> dict[str, Any]:
    stage = str(payload.get("stage") or "fast_probe")
    fact = {
        "endpoint": str(payload["endpoint"]),
        "elapsed_ms": 1,
        "full_budget": attempt_kind not in {"censored", "facility_fail"},
        "completed": attempt_kind != "censored",
        "entitlement_exhausted": attempt_kind == "censored",
        "status_code": 204 if attempt_kind == "pass" else None,
        "outcome": "right_censored" if attempt_kind == "censored" else attempt_kind,
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
        "granted_budget_ms": int(float(payload.get("probe_budget_seconds", 1.0)) * 1000),
        "lifecycle": "censored" if attempt_kind == "censored" else "completed",
    }
    return {
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
        "node_json": "",
        "report_json": "{}",
        "kind": "right_censored" if attempt_kind == "censored" else attempt_kind,
        "reason": f"deterministic_{attempt_kind}",
        "latency_ms": 25.0 if attempt_kind == "pass" else None,
        "slow": False,
        "probe_url": str(payload.get("endpoint") or "") if attempt_kind == "pass" else "",
        "attempt_facts": [fact],
        "facility_elapsed_ms": 1,
        "portfolio_elapsed_ms": 1,
        "total_elapsed_ms": 2,
        "clean_confirmed": True,
        "helper_pid": os.getpid(),
        "mihomo_pid": mihomo_pid,
        "probe_policy_revision": int(payload.get("probe_policy_revision", 1) or 1),
        "portfolio_digest": str(payload.get("portfolio_digest") or ""),
        "submitted_monotonic_ns": int(payload.get("submitted_monotonic_ns", 0) or 0),
        "dispatch_monotonic_ns": int(payload.get("dispatch_monotonic_ns", 0) or 0),
        "supervisor_assigned_monotonic_ns": int(
            payload.get("supervisor_assigned_monotonic_ns", 0) or 0
        ),
        "supervisor_sent_monotonic_ns": int(payload.get("supervisor_sent_monotonic_ns", 0) or 0),
        "helper_started_monotonic_ns": time.monotonic_ns(),
        "yield_created_monotonic_ns": time.monotonic_ns(),
        "monotonic_ns": time.monotonic_ns(),
    }


def batch_deterministic_helper_bootstrap(connection: Any) -> None:  # noqa: C901
    connection.send_bytes(BOOTSTRAP_READY_MESSAGE)
    start = json.loads(connection.recv_bytes().decode("utf-8"))["payload"]
    lead_id = int(start["worker_id"])
    generation = int(start["worker_generation"])
    member_ids = [int(item) for item in start.get("member_worker_ids", [lead_id])]
    mixed_port = int(start.get("mixed_port", 0) or 0)
    controller_port = int(start.get("controller_port", 0) or 0)
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
            "worker_id": lead_id,
            "worker_generation": generation,
            "helper_pid": os.getpid(),
            "mihomo_pid": child.pid,
            "mixed_port": mixed_port,
            "controller_port": controller_port,
        },
    )
    slot_heartbeats = os.environ.get("TIER3_BATCH_FAKE_SLOT_HEARTBEATS", "") == "1"
    layout_seconds = max(0.0, float(os.environ.get("TIER3_BATCH_FAKE_LAYOUT_SECONDS", "0") or 0.0))
    if os.environ.get("TIER3_BATCH_FAKE_SKIP_BATCH_READY", "") == "1":
        # A layout that never finishes: the helper neither reads nor reports.
        try:
            time.sleep(120.0)
        finally:
            if child.poll() is None:
                child.kill()
            connection.close()
        return
    if layout_seconds:
        time.sleep(layout_seconds)
    _send(
        connection,
        "batch_ready",
        {
            "worker_id": lead_id,
            "worker_generation": generation,
            "member_worker_ids": member_ids,
            "monotonic_ns": time.monotonic_ns(),
        },
    )
    # worker_id -> (due_at, result payload, lease_id, unclean_on_cancel)
    pending: dict[int, tuple[float, dict[str, Any], str, bool]] = {}
    unclean: set[int] = set()
    silent: set[int] = set()
    last_heartbeat = 0.0
    try:
        while True:
            now = time.monotonic()
            if now - last_heartbeat >= 0.2:
                last_heartbeat = now
                now_ns = time.monotonic_ns()
                entries = []
                for worker_id in member_ids:
                    active = pending.get(worker_id)
                    entries.append([
                        worker_id,
                        active[2] if active else "",
                        1 if (active or worker_id in unclean) else 0,
                    ])
                if slot_heartbeats:
                    for worker_id, lease_id, active_count in entries:
                        _send(
                            connection,
                            "heartbeat",
                            {
                                "worker_id": worker_id,
                                "worker_generation": generation,
                                "helper_pid": os.getpid(),
                                "mihomo_pid": child.pid,
                                "lease_id": lease_id,
                                "active_probe_count": active_count,
                                "monotonic_ns": now_ns,
                            },
                        )
                else:
                    _send(
                        connection,
                        "group_heartbeat",
                        {
                            "worker_id": lead_id,
                            "worker_generation": generation,
                            "helper_pid": os.getpid(),
                            "mihomo_pid": child.pid,
                            "monotonic_ns": now_ns,
                            "slots": entries,
                        },
                    )
            for worker_id, (due_at, result, _lease, _unclean) in list(pending.items()):
                if now >= due_at:
                    pending.pop(worker_id, None)
                    _send(connection, "endpoint_result", result)
            if not connection.poll(0.02):
                continue
            envelope = json.loads(connection.recv_bytes().decode("utf-8"))
            kind = str(envelope["kind"])
            payload = dict(envelope.get("payload") or {})
            if kind == "stop":
                return
            if kind == "cancel":
                worker_id = int(payload.get("worker_id", 0) or 0)
                active = pending.pop(worker_id, None)
                if active is not None and active[3]:
                    unclean.add(worker_id)
                    continue
                if worker_id in silent:
                    silent.discard(worker_id)
                    continue
                _send(
                    connection,
                    "clean_confirmed",
                    {
                        "worker_id": worker_id,
                        "worker_generation": generation,
                        "lease_id": payload.get("lease_id", ""),
                        "sequence": int(payload.get("sequence", 0) or 0),
                        "reason": str(payload.get("reason") or ""),
                        "active_probe_count": 0,
                    },
                )
                continue
            if kind != "validate_endpoint":
                continue
            worker_id = int(payload.get("worker_id", 0) or 0)
            if worker_id not in member_ids:
                raise RuntimeError("batch fake received a lease for a foreign identity")
            if worker_id in pending:
                raise RuntimeError("helper received overlapping validation lease")
            extra = _node_extra(payload)
            if extra.get("fixture_crash_helper"):
                # Process-level loss with in-flight siblings: die without a word.
                os._exit(3)
            if extra.get("fixture_slot_unusable"):
                # The production helper answers the lease with a facility
                # failure and then asks the parent to retire this identity.
                _send(connection, "endpoint_result", _result_payload(
                    payload, worker_id=worker_id, generation=generation,
                    mihomo_pid=child.pid, attempt_kind="facility_fail",
                ))
                _send(connection, "slot_unusable", {
                    "worker_id": worker_id, "worker_generation": generation,
                    "reason": "fixture_unusable", "monotonic_ns": time.monotonic_ns(),
                })
                continue
            _send(
                connection,
                "slice_phase",
                {
                    "worker_id": worker_id,
                    "worker_generation": generation,
                    "lease_id": payload["lease_id"],
                    "slice_id": int(payload.get("slice_id", 1) or 1),
                    "stage": str(payload.get("stage") or "fast_probe"),
                    "phase": "dispatch_received",
                    "monotonic_ns": time.monotonic_ns(),
                },
            )
            fixture_kind = str(extra.get("fixture_validation_outcome") or "").strip().lower()
            if fixture_kind not in {"pass", "candidate_fail", "censored", "facility_fail"}:
                fixture_kind = ""
            bucket = int(str(payload["candidate_digest"])[:8], 16) % 5
            attempt_kind = fixture_kind or (
                "pass"
                if bucket == 0
                else "candidate_fail"
                if bucket == 1
                else "censored"
                if bucket in {2, 3}
                else "facility_fail"
            )
            delay = max(0.0, min(8.0, float(extra.get("fixture_delay_seconds", 0.0) or 0.0)))
            for phase in ("facility_started", "transport_started"):
                _send(
                    connection,
                    "slice_phase",
                    {
                        "worker_id": worker_id,
                        "worker_generation": generation,
                        "lease_id": payload["lease_id"],
                        "slice_id": int(payload.get("slice_id", 1) or 1),
                        "stage": str(payload.get("stage") or "fast_probe"),
                        "phase": phase,
                        "monotonic_ns": time.monotonic_ns(),
                    },
                )
            if extra.get("fixture_cancel_silent"):
                silent.add(worker_id)
            else:
                silent.discard(worker_id)
            pending[worker_id] = (
                time.monotonic() + delay,
                _result_payload(
                    payload,
                    worker_id=worker_id,
                    generation=generation,
                    mihomo_pid=child.pid,
                    attempt_kind=attempt_kind,
                ),
                str(payload["lease_id"]),
                bool(extra.get("fixture_cancel_unclean")),
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


def finish_like_a_finished_helper(marker: str) -> None:
    """Spawn target: queue finalization work, then end like a helper that finished.

    The production bootstrap's fast exit must leave with code 0 without running
    interpreter finalization, so the atexit write below never happens.  Kept
    here (standard library plus the bootstrap) so the spawned child stays cheap.
    """

    import atexit
    from pathlib import Path

    from tier3_batch_validator_helper import _exit_finished_helper_process

    atexit.register(Path(marker).write_text, "finalized", encoding="utf-8")
    _exit_finished_helper_process()
    Path(marker).write_text("returned", encoding="utf-8")
