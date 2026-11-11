from __future__ import annotations

import queue
import threading
import time
from typing import Callable

from tier3_validation_protocol import (
    AdmissionCommitted,
    AdmissionIntent,
    AdmissionRejected,
)


AdmissionResult = AdmissionCommitted | AdmissionRejected


class Tier3AdmissionCommitQueue:
    """Serializes admission intents before the central pool mutation gateway."""

    def __init__(
        self,
        commit: Callable[[AdmissionIntent], AdmissionResult],
        result_sink: Callable[[AdmissionIntent, AdmissionResult], None],
        *,
        capacity: int = 256,
    ) -> None:
        self._commit = commit
        self._result_sink = result_sink
        self._queue: queue.Queue[AdmissionIntent | None] = queue.Queue(
            maxsize=max(1, int(capacity))
        )
        self._thread: threading.Thread | None = None
        self._accepting = threading.Event()
        self._accepting.set()
        self._stopped = threading.Event()
        self._pending_condition = threading.Condition()
        self._pending = 0
        self._seen_intents: set[tuple[str, str, str, int]] = set()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="tier3-admission-commit-queue",
            daemon=False,
        )
        self._thread.start()

    def put_nowait(self, intent: AdmissionIntent) -> bool:
        if not self._accepting.is_set():
            return False
        try:
            with self._pending_condition:
                self._queue.put_nowait(intent)
                self._pending += 1
        except queue.Full:
            return False
        return True

    def pending_count(self) -> int:
        with self._pending_condition:
            return max(0, int(self._pending))

    def wait_empty(self, timeout_seconds: float) -> bool:
        timeout = max(0.0, float(timeout_seconds))
        with self._pending_condition:
            return self._pending_condition.wait_for(
                lambda: self._pending <= 0,
                timeout=timeout,
            )

    def stop(self, timeout_seconds: float = 2.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        self._accepting.clear()
        self.wait_empty(max(0.0, deadline - time.monotonic()))
        try:
            self._queue.put(
                None,
                timeout=max(0.0, deadline - time.monotonic()),
            )
        except queue.Full:
            return False
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
            return not thread.is_alive()
        return True

    def _run(self) -> None:
        try:
            while True:
                intent = self._queue.get()
                try:
                    if intent is None:
                        return
                    intent_key = (
                        intent.refresh_epoch,
                        intent.candidate_digest,
                        intent.lease_id,
                        int(intent.sequence),
                    )
                    if intent_key in self._seen_intents:
                        result = AdmissionRejected(
                            refresh_epoch=intent.refresh_epoch,
                            candidate_digest=intent.candidate_digest,
                            lease_id=intent.lease_id,
                            sequence=intent.sequence,
                            reason="duplicate_admission_intent",
                        )
                    else:
                        self._seen_intents.add(intent_key)
                        try:
                            result = self._commit(intent)
                        except BaseException as exc:
                            result = AdmissionRejected(
                                refresh_epoch=intent.refresh_epoch,
                                candidate_digest=intent.candidate_digest,
                                lease_id=intent.lease_id,
                                sequence=intent.sequence,
                                reason=(
                                    f"committer_exception:{type(exc).__name__}:{exc}"
                                ),
                            )
                    self._result_sink(intent, result)
                finally:
                    self._queue.task_done()
                    if intent is not None:
                        with self._pending_condition:
                            self._pending = max(0, self._pending - 1)
                            if self._pending <= 0:
                                self._pending_condition.notify_all()
        finally:
            self._stopped.set()
