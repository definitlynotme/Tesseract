"""Non-blocking capture primitives for the desktop log center.

This module deliberately has no Qt dependency.  Runtime logging threads only
format a record and attempt a bounded ``put_nowait``; the UI owns draining and
turning these capture dictionaries into its immutable ``LogRecord`` objects.
"""

from __future__ import annotations

import logging
import logging.handlers
import json
import queue
import re
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from log_event_reports import (
    event_spec,
    match_core_runner_event,
    match_mihomo_event,
    reclassify_mihomo_fallback,
    safe_event_metadata,
    sanitize_event_params,
)


DEFAULT_CAPTURE_CAPACITY = 10_000
MAX_DRAIN_BATCH = 500
DEFAULT_MONITOR_SEED_LINES = 500
DEFAULT_TRACKED_GENERATIONS = 64
DEFAULT_EVENT_LOG_NAME = "events.jsonl"
DEFAULT_EVENT_LOG_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_EVENT_LOG_BACKUP_COUNT = 5

_MONITOR_LINE_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:,\d{1,6})?)"
    r"\s+\[(?P<level>[A-Za-z]+)\]\s*(?P<message>.*)$"
)
_MIHOMO_LOGFMT_FIELD = re.compile(
    r'(?:^|\s)(time|level|msg)=("(?:\\.|[^"\\])*"|[^\s"]+)'
)
_CAPTURE_HANDLER_MARKER = "_tesseract_log_capture_handler"
_ALERT_TYPES = frozenset(
    {
        "WARNING",
        "CRITICAL",
        "EMERGENCY",
        "RECOVERY",
        "POOL_UPDATE",
        "BACKUP_REMINDER",
        "KILL_SWITCH_ON",
        "KILL_SWITCH_OFF",
        "KILL_SWITCH_DISABLED",
        "KILL_SWITCH_OVERRIDE",
    }
)


def _local_iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _record_time_iso(created: float) -> str:
    return datetime.fromtimestamp(created).astimezone().isoformat(timespec="milliseconds")


def _parsed_monitor_time(value: str, fallback: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace(",", "."))
        return parsed.astimezone().isoformat(timespec="milliseconds")
    except (TypeError, ValueError, OSError):
        return fallback


def _normalize_level(value: Any) -> str:
    level = str(value or "INFO").strip().upper()
    if level == "WARN":
        return "WARNING"
    if level == "FATAL":
        return "CRITICAL"
    if level in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        return level
    return "INFO"


def _boolean_flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _enrich_captured_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Attach only registered event facts to a neutral capture mapping."""

    captured = dict(record)
    source = str(captured.get("source") or "UNKNOWN").strip().upper() or "UNKNOWN"
    level = _normalize_level(captured.get("level") or captured.get("type"))
    message = str(
        captured.get("message_raw")
        or captured.get("raw_message")
        or captured.get("message")
        or captured.get("payload")
        or ""
    )
    event_key = str(captured.get("event_key") or "").strip()
    match_kind = str(captured.get("event_match_kind") or "").strip()
    params = captured.get("event_params")
    if not isinstance(params, Mapping):
        metadata_value = captured.get("metadata")
        if isinstance(metadata_value, Mapping) and isinstance(metadata_value.get("event_params"), Mapping):
            params = metadata_value.get("event_params")
        else:
            params = {}

    if not event_key and source in {"MIHOMO", "TESSERACT"} and message:
        match = (
            match_core_runner_event(message)
            if source == "TESSERACT"
            else match_mihomo_event(message, level)
        )
        if match is not None:
            event_key = match.event_key
            params = match.params
            match_kind = match.match_kind

    metadata = dict(captured.get("metadata") or {})
    if source == "MIHOMO" and event_key:
        refreshed = reclassify_mihomo_fallback(event_key, message, level, metadata)
        if refreshed is not None:
            metadata.setdefault("observed_event_key", event_key)
            event_key, params, match_kind = refreshed.event_key, refreshed.params, refreshed.match_kind
    spec = event_spec(event_key)
    explicit_key_event = captured.get("is_key_event")
    if explicit_key_event is None:
        metadata_value = captured.get("metadata")
        if isinstance(metadata_value, Mapping):
            explicit_key_event = metadata_value.get("is_key_event")
    if spec is None and explicit_key_event is None:
        return captured

    sanitized = sanitize_event_params(event_key, params)
    effective_level = spec.level if spec is not None else level
    if spec is not None and effective_level != level:
        metadata.setdefault("observed_level", level)
    metadata["event_params"] = dict(sanitized)
    metadata["event_match_kind"] = match_kind or "event_key"
    if spec is not None:
        observed_category = str(captured.get("category") or "").strip()
        if observed_category and observed_category != spec.category:
            metadata["observed_category"] = observed_category
        if explicit_key_event is not None and _boolean_flag(explicit_key_event) != spec.is_key_event:
            metadata["observed_is_key_event"] = _boolean_flag(explicit_key_event)
        is_key = bool(spec.is_key_event)
        category = spec.category
    else:
        is_key = _boolean_flag(explicit_key_event)
        category = str(captured.get("category") or "general")
    metadata["is_key_event"] = is_key
    captured["event_key"] = event_key
    captured["event_params"] = dict(sanitized)
    captured["event_match_kind"] = metadata["event_match_kind"]
    captured["is_key_event"] = is_key
    captured["category"] = category
    captured["level"] = effective_level
    captured["metadata"] = metadata
    return captured


class LogCaptureQueue:
    """Thread-safe bounded FIFO used between logging threads and the UI."""

    def __init__(self, capacity: int = DEFAULT_CAPTURE_CAPACITY) -> None:
        self.capacity = max(1, int(capacity))
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=self.capacity)
        self._ingress_lock = threading.Lock()
        self._drop_lock = threading.Lock()
        self._dropped_count = 0
        self._sidecar_failure_count = 0
        self._external_event_logger: logging.Logger | None = None

    def bind_external_event_logger(self, logger: logging.Logger | None) -> None:
        """Bind the durable sidecar owner used by live Mihomo ingestion."""

        self._external_event_logger = logger

    def put_nowait(self, record: Mapping[str, Any]) -> bool:
        captured = _enrich_captured_record(record)
        metadata = captured.get("metadata")
        capture_kind = (
            str(metadata.get("capture_kind") or "")
            if isinstance(metadata, Mapping)
            else ""
        )
        spec = event_spec(captured.get("event_key"))
        should_persist = (
            self._external_event_logger is not None
            and str(captured.get("source") or "").upper() == "MIHOMO"
            and capture_kind in {"mihomo_stream", "stdout_fallback"}
            and spec is not None
            and (spec.is_key_event or spec.persist_raw)
        )
        if should_persist:
            try:
                persisted = persist_structured_event_record(
                    self._external_event_logger,
                    captured,
                )
            except Exception:
                persisted = False
            if not persisted:
                with self._drop_lock:
                    self._sidecar_failure_count += 1
        with self._ingress_lock:
            try:
                self._queue.put_nowait(captured)
                return True
            except queue.Full:
                self.note_dropped()
                return False

    def note_dropped(self, count: int = 1) -> None:
        with self._drop_lock:
            self._dropped_count += max(0, int(count))

    @property
    def dropped_count(self) -> int:
        with self._drop_lock:
            return self._dropped_count

    @property
    def sidecar_failure_count(self) -> int:
        with self._drop_lock:
            return self._sidecar_failure_count

    def pending_count(self) -> int:
        with self._ingress_lock:
            return self._queue.qsize()

    def drain(self, limit: int = MAX_DRAIN_BATCH) -> list[dict[str, Any]]:
        """Return up to 500 records in FIFO order without waiting."""

        size = min(MAX_DRAIN_BATCH, max(0, int(limit)))
        records: list[dict[str, Any]] = []
        with self._ingress_lock:
            for _ in range(size):
                try:
                    records.append(self._queue.get_nowait())
                except queue.Empty:
                    break
        return records

    def drain_all(self) -> list[dict[str, Any]]:
        """Atomically take every record present at this clear-view barrier."""

        with self._ingress_lock:
            fenced_queue = self._queue
            self._queue = queue.Queue(maxsize=self.capacity)
        records: list[dict[str, Any]] = []
        while True:
            try:
                records.append(fenced_queue.get_nowait())
            except queue.Empty:
                return records


class LogQueueHandler(logging.Handler):
    """A logging handler that only formats and attempts a bounded enqueue.

    The handler never logs its own failures, which prevents recursion when it
    is attached to ``proxy_manager``.  Formatter failures and queue overflow
    are represented by ``LogCaptureQueue.dropped_count``.
    """

    def __init__(self, capture_queue: LogCaptureQueue) -> None:
        super().__init__(level=logging.DEBUG)
        self.capture_queue = capture_queue
        self.setFormatter(logging.Formatter("%(message)s"))
        setattr(self, _CAPTURE_HANDLER_MARKER, True)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # Formatter.format preserves formatted exception and stack output
            # in the one raw message presented by the log detail panel.
            raw_message = self.format(record)
            received_at = _local_iso_now()
            metadata: dict[str, Any] = {
                "logger_name": record.name,
                "module": record.module,
                "thread_name": record.threadName,
            }
            if record.exc_info:
                exc_type, exc_value, _traceback = record.exc_info
                metadata["exception_type"] = getattr(exc_type, "__name__", str(exc_type or ""))
                metadata["exception_message"] = str(exc_value or "")
            event_params = getattr(record, "event_params", {})
            if isinstance(event_params, Mapping):
                metadata["event_params"] = dict(safe_event_metadata(event_params))
            event_match_kind = str(getattr(record, "event_match_kind", "") or "").strip()
            explicit_key_event = getattr(record, "is_key_event", None)
            event_key = str(getattr(record, "event_key", "") or "").strip()
            normalized_level = _normalize_level(record.levelname)
            if (
                explicit_key_event is None
                and not event_key
                and normalized_level in {"WARNING", "ERROR", "CRITICAL"}
            ):
                explicit_key_event = False
                event_match_kind = event_match_kind or "raw_only"
            if event_match_kind:
                metadata["event_match_kind"] = event_match_kind
            if explicit_key_event is not None:
                metadata["is_key_event"] = bool(explicit_key_event)
            alert_type = str(getattr(record, "alert_type", "") or "").strip().upper()
            captured = {
                "timestamp": _record_time_iso(record.created),
                "received_at": received_at,
                "source": "ALERT" if alert_type else "TESSERACT",
                "level": normalized_level,
                "type": normalized_level.lower(),
                "message": raw_message,
                "raw_message": raw_message,
                "payload": raw_message,
                "event_key": event_key,
                "event_group": str(getattr(record, "event_group", "") or ""),
                "event_params": dict(event_params) if isinstance(event_params, Mapping) else {},
                "event_match_kind": event_match_kind,
                "is_key_event": explicit_key_event,
                "alert_type": alert_type,
                "category": str(getattr(record, "category", "") or ("alert" if alert_type else "")),
                "metadata": metadata,
            }
            self.capture_queue.put_nowait(captured)
        except Exception:
            # Calling Handler.handleError can write to stderr.  Keep this sink
            # side-effect free and account for the lost record instead.
            self.capture_queue.note_dropped()


def structured_event_log_path(monitor_log_path: str | Path) -> Path:
    return Path(monitor_log_path).with_name(DEFAULT_EVENT_LOG_NAME)


def _structured_event_payload(
    record: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Normalize one captured record and build its durable event payload."""

    captured = _enrich_captured_record(record)
    event_key = str(captured.get("event_key") or "").strip()
    spec = event_spec(event_key)
    if not event_key or spec is None or not (spec.is_key_event or spec.persist_raw):
        return None
    raw_message = str(
        captured.get("message_raw")
        or captured.get("raw_message")
        or captured.get("message")
        or captured.get("payload")
        or ""
    )
    metadata = dict(safe_event_metadata(captured.get("metadata") or {}))
    metadata["event_params"] = dict(captured.get("event_params") or {})
    metadata["event_match_kind"] = str(captured.get("event_match_kind") or "event_key")
    metadata["is_key_event"] = spec.is_key_event
    if captured.get("event_group"):
        metadata["event_group"] = str(captured.get("event_group"))
    if captured.get("alert_type"):
        metadata["alert_type"] = str(captured.get("alert_type"))
    payload = {
        "timestamp": captured.get("timestamp") or _local_iso_now(),
        "received_at": captured.get("received_at") or _local_iso_now(),
        "source": captured.get("source") or "UNKNOWN",
        "level": spec.level,
        "category": spec.category,
        "event_key": event_key,
        "event_group": captured.get("event_group") or "",
        "alert_type": captured.get("alert_type") or "",
        "message_raw": raw_message,
        "metadata": metadata,
    }
    return captured, payload


def _structured_event_fingerprint(payload: Mapping[str, Any]) -> tuple[str, str, str, str]:
    raw_timestamp = str(payload.get("timestamp") or "").strip()
    try:
        parsed_timestamp = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
        if parsed_timestamp.tzinfo is None:
            parsed_timestamp = parsed_timestamp.astimezone()
        timestamp = parsed_timestamp.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    except (TypeError, ValueError, OSError):
        timestamp = raw_timestamp
    return (
        str(payload.get("source") or "").strip().upper(),
        timestamp,
        _normalize_level(payload.get("level")),
        str(payload.get("message_raw") or ""),
    )


class _PropagatingRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Let the owning sidecar writer account for real sink failures."""

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 - logging API
        error = sys.exc_info()[1]
        if error is None:
            raise OSError(f"structured event sidecar write failed: {record.name}")
        raise error


class StructuredEventJsonHandler(logging.Handler):
    """Rotating JSONL sidecar for key events and registered raw diagnostics."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_bytes: int = DEFAULT_EVENT_LOG_MAX_BYTES,
        backup_count: int = DEFAULT_EVENT_LOG_BACKUP_COUNT,
    ) -> None:
        super().__init__(level=logging.DEBUG)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._sink = _PropagatingRotatingFileHandler(
            self.path,
            maxBytes=max(1, int(max_bytes)),
            backupCount=max(0, int(backup_count)),
            encoding="utf-8",
            delay=False,
        )
        self._sink.setFormatter(logging.Formatter("%(message)s"))
        self.setFormatter(logging.Formatter("%(message)s"))
        self._sink_lock = threading.RLock()
        self._fingerprint_limit = 4_000
        self._fingerprints: set[tuple[str, str, str, str]] = set()
        self._fingerprint_order: deque[tuple[str, str, str, str]] = deque()
        self._external_queue: queue.Queue[tuple[dict[str, Any], str, int, str]] = queue.Queue(
            maxsize=DEFAULT_CAPTURE_CAPACITY
        )
        self._external_state = threading.Condition(threading.Lock())
        self._external_pending = 0
        self._external_failure_count = 0
        self._external_stopping = False
        self._external_thread: threading.Thread | None = None
        self._seed_fingerprints()
        setattr(self, "_tesseract_structured_event_handler", True)
        setattr(self, "_tesseract_structured_event_path", str(self.path.resolve()))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            received_at = _local_iso_now()
            raw_message = self.format(record)
            alert_type = str(getattr(record, "alert_type", "") or "").strip().upper()
            event_params = getattr(record, "event_params", {})
            captured = _enrich_captured_record(
                {
                    "timestamp": _record_time_iso(record.created),
                    "received_at": received_at,
                    "source": str(getattr(record, "log_source", "") or ("ALERT" if alert_type else "TESSERACT")),
                    "level": _normalize_level(record.levelname),
                    "message_raw": raw_message,
                    "event_key": str(getattr(record, "event_key", "") or ""),
                    "event_group": str(getattr(record, "event_group", "") or ""),
                    "event_params": dict(event_params) if isinstance(event_params, Mapping) else {},
                    "event_match_kind": str(getattr(record, "event_match_kind", "") or ""),
                    "is_key_event": getattr(record, "is_key_event", None),
                    "alert_type": alert_type,
                    "category": str(getattr(record, "category", "") or ""),
                    "metadata": {
                        "logger_name": record.name,
                        "module": record.module,
                        "thread_name": record.threadName,
                    },
                }
            )
            prepared = _structured_event_payload(captured)
            if prepared is None:
                return
            _captured, payload = prepared
            self._write_payload(
                payload,
                logger_name=record.name,
                levelno=record.levelno,
                levelname=record.levelname,
            )
        except Exception:
            self.handleError(record)

    def _write_payload(
        self,
        payload: Mapping[str, Any],
        *,
        logger_name: str,
        levelno: int,
        levelname: str,
    ) -> bool:
        with self._sink_lock:
            sink = getattr(self, "_sink", None)
            if self._closed or sink is None or bool(getattr(sink, "_closed", False)):
                return False
            fingerprint = _structured_event_fingerprint(payload)
            if fingerprint in self._fingerprints:
                return False
            line = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True)
            sidecar_record = logging.makeLogRecord(
                {
                    "name": logger_name,
                    "levelno": levelno,
                    "levelname": levelname,
                    "msg": line,
                    "args": (),
                }
            )
            sink.emit(sidecar_record)
            self._fingerprints.add(fingerprint)
            self._fingerprint_order.append(fingerprint)
            while len(self._fingerprint_order) > self._fingerprint_limit:
                expired = self._fingerprint_order.popleft()
                self._fingerprints.discard(expired)
            return True

    def _seed_fingerprints(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        for line in lines[-self._fingerprint_limit :]:
            try:
                payload = json.loads(line)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(payload, Mapping):
                continue
            fingerprint = _structured_event_fingerprint(payload)
            if fingerprint in self._fingerprints:
                continue
            self._fingerprints.add(fingerprint)
            self._fingerprint_order.append(fingerprint)

    def append_captured(self, record: Mapping[str, Any]) -> bool:
        """Synchronously persist one external record for tests and shutdown."""

        try:
            prepared = _structured_event_payload(record)
            if prepared is None:
                return False
            captured, payload = prepared
            levelname = _normalize_level(captured.get("level"))
            metadata = captured.get("metadata")
            logger_name = (
                str(metadata.get("logger_name") or "mihomo")
                if isinstance(metadata, Mapping)
                else "mihomo"
            )
            sink = getattr(self, "_sink", None)
            if self._closed or sink is None or bool(getattr(sink, "_closed", False)):
                return False
            self._write_payload(
                payload,
                logger_name=logger_name,
                levelno=int(getattr(logging, levelname, logging.INFO)),
                levelname=levelname,
            )
            return True
        except Exception:
            # The sidecar is supplementary; a disk or rotation failure must
            # never take down the live UI ingress path.
            return False

    def enqueue_captured(self, record: Mapping[str, Any]) -> bool:
        """Queue one external event without performing file I/O in the caller."""

        try:
            prepared = _structured_event_payload(record)
            if prepared is None:
                return False
            captured, payload = prepared
            levelname = _normalize_level(captured.get("level"))
            metadata = captured.get("metadata")
            logger_name = (
                str(metadata.get("logger_name") or "mihomo")
                if isinstance(metadata, Mapping)
                else "mihomo"
            )
            item = (
                payload,
                logger_name,
                int(getattr(logging, levelname, logging.INFO)),
                levelname,
            )
            with self._external_state:
                if self._closed or self._external_stopping:
                    return False
                if self._external_thread is None or not self._external_thread.is_alive():
                    self._external_thread = threading.Thread(
                        target=self._run_external_writer,
                        name="tesseract-event-sidecar",
                        daemon=True,
                    )
                    self._external_thread.start()
                self._external_pending += 1
                try:
                    self._external_queue.put_nowait(item)
                except queue.Full:
                    self._external_pending -= 1
                    self._external_failure_count += 1
                    self._external_state.notify_all()
                    return False
            return True
        except Exception:
            return False

    def _run_external_writer(self) -> None:
        while True:
            try:
                payload, logger_name, levelno, levelname = self._external_queue.get(timeout=0.1)
            except queue.Empty:
                with self._external_state:
                    if self._external_stopping and self._external_pending == 0:
                        return
                continue
            failed = False
            try:
                self._write_payload(
                    payload,
                    logger_name=logger_name,
                    levelno=levelno,
                    levelname=levelname,
                )
            except Exception:
                failed = True
            finally:
                self._external_queue.task_done()
                with self._external_state:
                    if failed:
                        self._external_failure_count += 1
                    self._external_pending = max(0, self._external_pending - 1)
                    self._external_state.notify_all()

    @property
    def external_failure_count(self) -> int:
        with self._external_state:
            return self._external_failure_count

    def flush(self) -> None:
        deadline = time.monotonic() + 2.0
        with self._external_state:
            while self._external_pending > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._external_state.wait(timeout=min(0.1, remaining))
        with self._sink_lock:
            sink = getattr(self, "_sink", None)
            if sink is not None and not bool(getattr(sink, "_closed", False)):
                sink.flush()

    def close(self) -> None:
        with self._external_state:
            self._external_stopping = True
            self._external_state.notify_all()
            thread = self._external_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self.flush()
        with self._sink_lock:
            sink = getattr(self, "_sink", None)
            if sink is not None and not bool(getattr(sink, "_closed", False)):
                sink.close()
        super().close()


def install_structured_event_sidecar(
    logger: logging.Logger,
    monitor_log_path: str | Path,
    *,
    max_bytes: int = DEFAULT_EVENT_LOG_MAX_BYTES,
    backup_count: int = DEFAULT_EVENT_LOG_BACKUP_COUNT,
    owner_handler: logging.Handler | None = None,
) -> StructuredEventJsonHandler:
    """Install one sidecar handler next to the authoritative monitor log."""

    expected = str(structured_event_log_path(monitor_log_path).resolve())
    for handler in tuple(logger.handlers):
        if not bool(getattr(handler, "_tesseract_structured_event_handler", False)):
            continue
        if str(getattr(handler, "_tesseract_structured_event_path", "")) == expected:
            if owner_handler is not None:
                _bind_sidecar_lifecycle(logger, owner_handler, handler)
            return handler  # type: ignore[return-value]
    handler = StructuredEventJsonHandler(
        expected,
        max_bytes=max_bytes,
        backup_count=backup_count,
    )
    logger.addHandler(handler)
    if owner_handler is not None:
        _bind_sidecar_lifecycle(logger, owner_handler, handler)
    return handler


def persist_structured_event_record(
    logger: logging.Logger,
    record: Mapping[str, Any],
) -> bool:
    """Write one external structured event through the logger's sidecar."""

    current: logging.Logger | None = logger
    while current is not None:
        for handler in tuple(current.handlers):
            if isinstance(handler, StructuredEventJsonHandler):
                try:
                    return handler.enqueue_captured(record)
                except Exception:
                    return False
        if not current.propagate:
            break
        current = current.parent
    return False


def _bind_sidecar_lifecycle(
    logger: logging.Logger,
    owner_handler: logging.Handler,
    sidecar: logging.Handler,
) -> None:
    """Close/remove the sidecar whenever the authoritative sink is closed."""

    companions = getattr(owner_handler, "_tesseract_companion_handlers", None)
    if isinstance(companions, list):
        if sidecar not in companions:
            companions.append(sidecar)
        return

    companion_handlers: list[logging.Handler] = [sidecar]
    setattr(owner_handler, "_tesseract_companion_handlers", companion_handlers)
    original_close = owner_handler.close
    close_lock = threading.Lock()
    companions_closed = False

    def close_with_companions() -> None:
        nonlocal companions_closed
        with close_lock:
            if not companions_closed:
                companions_closed = True
                for companion in tuple(companion_handlers):
                    companion.acquire()
                    try:
                        try:
                            companion.flush()
                        except Exception:
                            pass
                        try:
                            companion.close()
                        except Exception:
                            pass
                    finally:
                        companion.release()
                        logger.removeHandler(companion)
        original_close()

    owner_handler.close = close_with_companions  # type: ignore[method-assign]


def install_proxy_manager_log_capture(
    capture_queue: LogCaptureQueue | None = None,
    *,
    logger: logging.Logger | None = None,
) -> LogQueueHandler:
    """Attach one idempotent capture handler to ``proxy_manager``."""

    target = logger or logging.getLogger("proxy_manager")
    for handler in tuple(target.handlers):
        if isinstance(handler, LogQueueHandler) or bool(getattr(handler, _CAPTURE_HANDLER_MARKER, False)):
            handler.capture_queue.bind_external_event_logger(target)
            return handler  # type: ignore[return-value]
    handler = LogQueueHandler(capture_queue or LogCaptureQueue())
    handler.capture_queue.bind_external_event_logger(target)
    target.addHandler(handler)
    return handler


def remove_proxy_manager_log_capture(
    handler: LogQueueHandler | None = None,
    *,
    logger: logging.Logger | None = None,
) -> int:
    """Remove the selected capture handler, or all capture handlers."""

    target = logger or logging.getLogger("proxy_manager")
    removed = 0
    for candidate in tuple(target.handlers):
        if handler is not None and candidate is not handler:
            continue
        if not isinstance(candidate, LogQueueHandler) and not bool(
            getattr(candidate, _CAPTURE_HANDLER_MARKER, False)
        ):
            continue
        target.removeHandler(candidate)
        candidate.close()
        removed += 1
    return removed


def captured_record_from_text_line(
    line: str,
    *,
    source: str,
    received_at: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Parse a file/stdout line into the neutral capture dictionary shape."""

    raw_line = str(line or "").rstrip("\r\n")
    if not raw_line.strip():
        return None
    received = received_at or _local_iso_now()
    match = _MONITOR_LINE_PATTERN.match(raw_line)
    if match:
        level = _normalize_level(match.group("level"))
        message = match.group("message")
        timestamp = _parsed_monitor_time(match.group("timestamp"), received)
    else:
        level = "INFO"
        message = raw_line
        timestamp = received
        if str(source).strip().upper() == "MIHOMO" and re.match(r"^(?:time|level)=", raw_line):
            fields = dict(_MIHOMO_LOGFMT_FIELD.findall(raw_line))
            if "level" in fields and "msg" in fields:
                # Core stdout uses logfmt, unlike the monitor file. Preserve
                # severity even when an unfamiliar quoted payload cannot decode.
                level = _normalize_level(fields["level"].strip('"'))
                try:
                    encoded = fields["msg"]
                    message = json.loads(encoded) if encoded.startswith('"') else encoded
                except (ValueError, TypeError):
                    message = raw_line
                timestamp = _parsed_monitor_time(fields.get("time", "").strip('"'), received)
    details = dict(metadata or {})
    details.setdefault("raw_line", raw_line)
    normalized_source = str(source or "UNKNOWN").strip().upper() or "UNKNOWN"
    alert_match = re.match(r"^\[(?P<type>[A-Z][A-Z0-9_]+)\]\s*", message)
    alert_type = alert_match.group("type") if alert_match and alert_match.group("type") in _ALERT_TYPES else ""
    if alert_type and normalized_source == "TESSERACT":
        normalized_source = "ALERT"
    return _enrich_captured_record({
        "timestamp": timestamp,
        "received_at": received,
        "source": normalized_source,
        "level": level,
        "type": level.lower(),
        "message": message,
        "raw_message": message,
        "payload": message,
        "event_key": "",
        "event_group": "",
        "alert_type": alert_type,
        "category": "alert" if alert_type else "",
        "metadata": details,
    })


def seed_monitor_log(
    log_path: str | Path,
    *,
    limit: int = DEFAULT_MONITOR_SEED_LINES,
) -> list[dict[str, Any]]:
    """Read only the current monitor log's final lines as Tesseract records.

    Rotated siblings such as ``monitor.log.1`` are intentionally ignored.
    """

    path = Path(log_path)
    line_limit = max(0, int(limit))
    if line_limit == 0 or not path.is_file():
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            position = handle.tell()
            chunks: list[bytes] = []
            newline_count = 0
            while position > 0 and newline_count <= line_limit:
                read_size = min(64 * 1024, position)
                position -= read_size
                handle.seek(position)
                chunk = handle.read(read_size)
                chunks.append(chunk)
                newline_count += chunk.count(b"\n")
        tail = b"".join(reversed(chunks)).decode("utf-8", errors="replace").splitlines()[-line_limit:]
    except OSError:
        return []
    received_at = _local_iso_now()
    records: list[dict[str, Any]] = []
    for entry in _monitor_entries(tail):
        record = captured_record_from_text_line(
            entry[0],
            source="TESSERACT",
            received_at=received_at,
            metadata={"seeded_from": str(path), "capture_kind": "monitor_tail"},
        )
        if record is None:
            continue
        if len(entry) > 1:
            # A traceback (or any multi-line message) belongs to the record
            # whose header precedes it.  Split apart, each continuation line
            # became an INFO record stamped with the seed time, and the entry
            # no longer matched its structured sidecar copy for dedupe.
            continuation = "\n".join(line.rstrip("\r\n") for line in entry[1:]).rstrip()
            message = f"{record.get('raw_message') or record.get('message') or ''}\n{continuation}"
            for key in ("message", "raw_message", "payload"):
                record[key] = message
            metadata = record.get("metadata")
            if isinstance(metadata, dict):
                metadata["raw_line"] = "\n".join(line.rstrip("\r\n") for line in entry)
        records.append(record)
    return records


def _monitor_entries(lines: list[str]) -> list[list[str]]:
    """Group monitor.log lines into entries: a timestamped header plus the
    untimestamped lines that follow it.  Lines before the first header in the
    tail belong to an entry whose header is outside the window; drop them."""

    entries: list[list[str]] = []
    for line in lines:
        if _MONITOR_LINE_PATTERN.match(line.rstrip("\r\n")):
            entries.append([line])
        elif entries:
            entries[-1].append(line)
    return entries


def merge_seed_records(*batches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge seed batches into one time-ordered stream.

    The structured sidecar tail spans days of sparse events while the monitor
    tail spans minutes of dense lines; enqueued one after the other, the raw
    view jumped back in time at the seam.  The sort is stable and earlier
    batches win ties, so a sidecar record still precedes (and dedupes) its
    identical monitor.log copy.
    """

    merged: list[tuple[float, int, dict[str, Any]]] = []
    for batch in batches:
        for record in batch:
            merged.append((_seed_sort_key(record), len(merged), record))
    merged.sort(key=lambda item: (item[0], item[1]))
    return [record for _key, _order, record in merged]


def _seed_sort_key(record: Mapping[str, Any]) -> float:
    raw = str(record.get("timestamp") or record.get("occurred_at") or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return float("inf")
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.timestamp()


def seed_structured_event_log(
    event_log_path: str | Path,
    *,
    limit: int = DEFAULT_MONITOR_SEED_LINES,
) -> list[dict[str, Any]]:
    """Load the current structured sidecar tail; rotated backups stay cold."""

    path = Path(event_log_path)
    line_limit = max(0, int(limit))
    if line_limit == 0 or not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-line_limit:]
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            payload = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        event_key = str(payload.get("event_key") or "").strip()
        metadata = dict(payload.get("metadata") or {})
        spec = event_spec(event_key)
        if not event_key or spec is None or not (spec.is_key_event or spec.persist_raw):
            continue
        metadata["seeded_from"] = str(path)
        metadata["capture_kind"] = "structured_event_tail"
        record = dict(payload)
        record["metadata"] = metadata
        record["event_params"] = dict(metadata.get("event_params") or {})
        record["event_match_kind"] = str(metadata.get("event_match_kind") or "event_key")
        record["is_key_event"] = bool(metadata.get("is_key_event", True))
        records.append(_enrich_captured_record(record))
    return records


# Concise compatibility name for callers that already know the sidecar path.
seed_event_log = seed_structured_event_log


class MihomoStdoutFallbackTracker:
    """Allows stdout fallback ingestion once for each runtime generation."""

    def __init__(self, *, max_generations: int = DEFAULT_TRACKED_GENERATIONS) -> None:
        self.max_generations = max(1, int(max_generations))
        self._lock = threading.Lock()
        self._seen: set[str] = set()
        self._order: deque[str] = deque()

    @staticmethod
    def _generation_key(generation: Any) -> str:
        value = str(generation if generation is not None else "unknown").strip()
        return value or "unknown"

    def was_consumed(self, generation: Any) -> bool:
        key = self._generation_key(generation)
        with self._lock:
            return key in self._seen

    def consume_once(
        self,
        generation: Any,
        text: str,
        *,
        parse_line: Callable[[str], Mapping[str, Any] | None] | None = None,
    ) -> list[dict[str, Any]]:
        """Parse meaningful stdout at most once for this generation.

        Empty output is not consumed so a later startup-failure line from the
        same process generation can still be collected.
        """

        raw_text = str(text or "")
        if not raw_text.strip():
            return []
        key = self._generation_key(generation)
        with self._lock:
            if key in self._seen:
                return []
            self._seen.add(key)
            self._order.append(key)
            while len(self._order) > self.max_generations:
                self._seen.discard(self._order.popleft())

        records: list[dict[str, Any]] = []
        received_at = _local_iso_now()
        for line in raw_text.splitlines():
            parsed = parse_line(line) if parse_line is not None else captured_record_from_text_line(
                line,
                source="MIHOMO",
                received_at=received_at,
            )
            if not parsed:
                continue
            record = dict(parsed)
            record["source"] = "MIHOMO"
            message = str(record.get("raw_message") or record.get("message") or record.get("payload") or "")
            record.setdefault("message", message)
            record.setdefault("raw_message", message)
            record.setdefault("payload", message)
            record.setdefault("timestamp", received_at)
            record.setdefault("received_at", received_at)
            record["level"] = _normalize_level(record.get("level") or record.get("type"))
            record.setdefault("type", record["level"].lower())
            details = dict(record.get("metadata") or {})
            details["runtime_generation"] = key
            details["capture_kind"] = "stdout_fallback"
            record["metadata"] = details
            records.append(_enrich_captured_record(record))
        return records
