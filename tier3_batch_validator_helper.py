from __future__ import annotations

# Keep this module standard-library only.  It is the spawn target of the batch
# validator backend: one helper process whose single Mihomo hosts several
# worker identities (one listener per identity).  Like the single-slot
# bootstrap it imports the real runtime only after the parent has assigned the
# helper to its private Job and sent the START latch.
import json
import multiprocessing
import os
import sys
import threading
import time
import traceback
from typing import Any

from tier3_validator_helper import BOOTSTRAP_READY_MESSAGE, _send_raw, _trace_bootstrap


def _exit_finished_helper_process() -> None:
    """End a helper process that finished cleanly, skipping interpreter finalization.

    Measured with the bundled Mihomo (one batch helper, 8 slots mid-probe):
    stop -> runtime returned in ~200 ms, but the process only exited ~200 ms
    later, all of it spent finalizing an interpreter that has loaded aiohttp,
    YAML and the formatter.  That alone pushed a stopping helper past the
    parent's 0.5 s cooperative window and turned every stop into a Job kill.
    Nothing is left to finalize here: the runtime wrote every frame and closed
    the Pipe, the facility is gone and its directory removed, and no
    non-daemon thread remains.  Only the spawned helper's own main thread
    exits this way; a bootstrap hosted on another thread (tests) returns.
    """

    if multiprocessing.parent_process() is None:
        return
    if threading.current_thread() is not threading.main_thread():
        return
    _trace_bootstrap("process_exit")
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.flush()
        except BaseException:  # noqa: BLE001
            pass
    os._exit(0)


def batch_helper_process_bootstrap(connection: Any) -> None:
    _trace_bootstrap("batch_target_entered")
    bootstrap_schema = 1
    prewarmed = None
    finished = False
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

        from tier3_batch_validator_runtime import run_batch_validator_helper

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
        asyncio.run(run_batch_validator_helper(connection, payload, prewarmed=prewarmed))
        _trace_bootstrap("runtime_returned")
        finished = True
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
    if finished:
        _exit_finished_helper_process()
