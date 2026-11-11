from __future__ import annotations

# Keep this module standard-library only. A frozen child imports the validator
# runtime only after the parent has assigned the helper to its private Job and
# sent the START latch.
import json
import os
import time
import traceback
from typing import Any


BOOTSTRAP_READY_MESSAGE = b"TIER3_VALIDATOR_BOOTSTRAP_READY_V1"


def _send_raw(
    connection: Any,
    kind: str,
    payload: dict[str, Any],
    *,
    schema: int,
) -> None:
    connection.send_bytes(
        json.dumps(
            {"schema": int(schema), "kind": kind, "payload": payload},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def _trace_bootstrap(stage: str, detail: str = "") -> None:
    path = os.environ.get("TESSERACT_VALIDATOR_BOOTSTRAP_TRACE", "").strip()
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "stage": stage,
                        "detail": detail,
                        "monotonic_ns": time.monotonic_ns(),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            )
    except OSError:
        pass


def helper_process_bootstrap(connection: Any) -> None:
    _trace_bootstrap("target_entered")
    bootstrap_schema = 1
    prewarmed = None
    try:
        # This fixed latch contains no candidate data. It lets the parent keep
        # Python spawn/import time separate from the Mihomo prewarm deadline.
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
            raise RuntimeError("validator helper did not receive START latch")
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            raise RuntimeError("validator helper START payload is invalid")

        # START is only sent after Job assignment. Facility boot can overlap
        # imports, but it never grants validation capacity or admission.
        if "prewarm_config" in payload:
            from tier3_validator_prewarm import ValidatorPrewarmProcess

            prewarmed = ValidatorPrewarmProcess(
                payload,
                lambda kind, data: _send_raw(connection, kind, data, schema=bootstrap_schema),
            )

        # Imports below this line may load aiohttp, YAML and the proxy formatter.
        import asyncio

        from tier3_validator_runtime import run_validator_helper

        _trace_bootstrap("runtime_imported")
        _send_raw(
            connection,
            "startup_progress",
            {
                "phase": "runtime_imported",
                "helper_pid": os.getpid(),
                "monotonic_ns": time.monotonic_ns(),
            },
            schema=bootstrap_schema,
        )
        asyncio.run(run_validator_helper(connection, payload, prewarmed=prewarmed))
        _trace_bootstrap("runtime_returned")
    except BaseException as exc:
        _trace_bootstrap("bootstrap_error", f"{type(exc).__name__}: {exc}")
        try:
            _send_raw(
                connection,
                "bootstrap_error",
                {
                    "pid": os.getpid(),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "traceback": traceback.format_exc(limit=24),
                },
                schema=bootstrap_schema,
            )
        except BaseException:
            pass
        raise
    finally:
        if prewarmed is not None:
            prewarmed.close()
        try:
            connection.close()
        except BaseException:
            pass
