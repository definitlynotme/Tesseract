"""Spawn target for the batch prototype helper.

Mirrors ``tier3_validator_helper`` deliberately: the control arm's child pays a
particular import ladder before validation starts, so the batch arm's child must
pay the same one or the comparison is biased.  Keep this module standard-library
only, and import the runtime only after the parent's START latch arrives.
"""
from __future__ import annotations

import json
import os
import time
import traceback
from typing import Any

from tier3_validator_helper import BOOTSTRAP_READY_MESSAGE


def _send_raw(connection: Any, kind: str, payload: dict[str, Any], *, schema: int) -> None:
    connection.send_bytes(
        json.dumps(
            {"schema": int(schema), "kind": kind, "payload": payload},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def _trace_bootstrap(stage: str, detail: str = "") -> None:
    path = os.environ.get("TESSERACT_BATCH_VALIDATOR_BOOTSTRAP_TRACE", "").strip()
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"stage": stage, "detail": detail,
                                     "pid": os.getpid(),
                                     "monotonic_ns": time.monotonic_ns()}) + "\n")
    except BaseException:  # noqa: BLE001
        pass


def batch_helper_bootstrap(connection: Any) -> None:
    bootstrap_schema = 1
    prewarmed = None
    try:
        connection.send_bytes(BOOTSTRAP_READY_MESSAGE)
        raw = connection.recv_bytes()
        _trace_bootstrap("start_received")
        envelope = json.loads(raw.decode("utf-8"))
        if isinstance(envelope, dict):
            try:
                received_schema = int(envelope.get("schema", 0) or 0)
            except (TypeError, ValueError):
                received_schema = 0
            if received_schema > 0:
                bootstrap_schema = received_schema
        if not isinstance(envelope, dict) or envelope.get("kind") != "start":
            raise RuntimeError("batch validator helper did not receive START latch")
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            raise RuntimeError("batch validator helper START payload is invalid")

        if "prewarm_config" in payload:
            from tier3_validator_prewarm import ValidatorPrewarmProcess

            prewarmed = ValidatorPrewarmProcess(
                payload,
                lambda kind, data: _send_raw(connection, kind, data, schema=bootstrap_schema),
            )

        import asyncio

        from test_support.tier3_batch_validator_runtime import run_batch_validator_helper

        _trace_bootstrap("runtime_imported")
        _send_raw(connection, "startup_progress", {
            "phase": "runtime_imported",
            "helper_pid": os.getpid(),
            "monotonic_ns": time.monotonic_ns(),
        }, schema=bootstrap_schema)
        asyncio.run(run_batch_validator_helper(connection, payload, prewarmed=prewarmed))
        _trace_bootstrap("runtime_returned")
    except BaseException as exc:
        _trace_bootstrap("bootstrap_error", f"{type(exc).__name__}: {exc}")
        try:
            _send_raw(connection, "bootstrap_error", {
                "pid": os.getpid(),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(limit=24),
            }, schema=bootstrap_schema)
        except BaseException:  # noqa: BLE001
            pass
        raise
    finally:
        if prewarmed is not None:
            prewarmed.close()
        try:
            connection.close()
        except BaseException:  # noqa: BLE001
            pass
