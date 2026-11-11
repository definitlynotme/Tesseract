"""Bounded ordinary-thread log producers for PublicationState (no Qt)."""

from __future__ import annotations

from collections import deque, OrderedDict
from functools import wraps
from datetime import datetime
from pathlib import Path
import json
import logging
import re
import uuid
from threading import Condition, Event, Lock, RLock, Thread, current_thread, local
import time
from typing import Any, Callable
from urllib.request import Request

import bridge_protocol as protocol
from bridge_redact import KnownSecrets, RedactionBudgetExceeded, redact_record, validate_record
from log_capture import LogQueueHandler, captured_record_from_text_line
from log_store import classify_log_category, serialize_log_records
from log_run_boundary import is_run_start
from bridge_log_view import LogView, SnapshotRequired
from log_event_reports import build_log_event_report, event_spec, sanitize_event_params

# T-P4-C1 #2 (R1/R2): the history key of an unclassified line; batch and
# history use the same value (PROTOCOL §16 Log.key).
RAW_KEY = "log.raw"
# Optional Log facts added after the +64 allowance was sized (is_key_event,
# run_id and their JSON punctuation): a conservative byte bound.
_LOG_FACTS_ALLOWANCE = 96
_LANGUAGES = frozenset({"zh_CN", "en_US", "ja_JP", "ko_KR", "fr_FR", "de_DE",
                        "es_ES", "pt_BR", "ru_RU"})


def log_translator(language: Any) -> Callable[[str], str] | None:
    """Template translator for ``build_log_event_report`` in one UI language.

    Same lookup as Qt's ``translated_text_or_source``: zh_CN (the source
    language), an unknown language or a missing catalog keep the source
    template; en_US also falls back to ``TRANSLATIONS_EN``. Never raises.
    """
    if language not in _LANGUAGES or language == "zh_CN":
        return None
    try:
        from ui_i18n import TRANSLATIONS_BY_LANGUAGE, TRANSLATIONS_EN
    except Exception:
        return None
    catalog = TRANSLATIONS_BY_LANGUAGE.get(language) or {}
    fallback = TRANSLATIONS_EN if language == "en_US" else {}

    def translate(text: str) -> str:
        found = catalog.get(text)
        if found is None:
            found = fallback.get(text, text)
        return found
    return translate


def is_key_event(key: str, record: Any) -> bool:
    """``LogEventSpec.is_key_event`` of the record's event key (catalog owned);
    an unregistered key keeps the capture's explicit flag; raw lines are not."""
    if not key or key == RAW_KEY:
        return False
    spec = event_spec(key)
    if spec is not None:
        return bool(spec.is_key_event)
    metadata = record.get("metadata") if isinstance(record, dict) else None
    flag = record.get("is_key_event") if isinstance(record, dict) else None
    if not isinstance(flag, bool) and isinstance(metadata, dict):
        flag = metadata.get("is_key_event")
    return flag if isinstance(flag, bool) else False


def ms() -> int:
    return time.time_ns() // 1_000_000


def log_level(value) -> str:
    level = str(value or "info").lower()
    level = {"warning": "warn", "fatal": "critical"}.get(level, level)
    return level if level in {"debug", "info", "warn", "error", "critical"} else "info"


def stdout_level(line: str) -> str:
    """Read only the severity header before CPU admission, without event parsing."""
    if line.startswith(("time=", "level=")):
        header = line.partition(" msg=")[0]
        match = re.search(r'(?:^|\s)level="?([A-Za-z]+)', header)
    else:
        match = re.match(r'^\d{4}-\d{2}-\d{2}\s+\S+\s+\[([A-Za-z]+)\]', line)
    return log_level(match.group(1)) if match else "info"


def timestamp(value: Any, fallback: int) -> int:
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str):
        try:
            return max(0, int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000))
        except (ValueError, OverflowError, OSError):
            pass
    return fallback


class LogNotRunning(RuntimeError):
    code = "not_running"


class _StreamBindingExpired(RuntimeError):
    """The response's Core epoch expired; discard it instead of relabelling it."""


class BoundedMihomoStream:
    """Only /logs and /traffic; bounded reads, owner-local close and backoff.

    Uses public MihomoAPIClient connection settings, never private HTTP helpers.
    The bridge owns the response; stopping cannot close another consumer's stream.
    """
    def __init__(self, client_factory: Callable[[], Any], endpoint: str,
                 consume: Callable[[dict], Any], *, note_dropped: Callable[[], Any] = lambda: None,
                 level: str = "info", backoff: float = 0.5, max_backoff: float = 30,
                 on_state: Callable[[str], Any] = lambda _state: None,
                 line_limit: int = 64 * 1024,
                 bind_consume: Callable[[], Callable[[dict], Any]] | None = None) -> None:
        if (endpoint not in {"/logs", "/traffic"} or not 0 < backoff <= max_backoff < float("inf")
                or type(line_limit) is not int or line_limit < 1):
            raise ValueError("invalid stream bounds")
        if level not in {"debug", "info", "warning", "error", "silent"}:
            raise ValueError("invalid log level")
        self.factory, self.endpoint, self.consume = client_factory, endpoint, consume
        self.note_dropped, self.level = note_dropped, level
        self.on_state, self.bind_consume = on_state, bind_consume
        self.backoff, self.max_backoff, self.line_limit = backoff, max_backoff, line_limit
        self.stop_event = Event()
        self._lock = Lock()
        self._response = None
        self.thread = None
        self._close_thread = None
        self._closing_response = None
        self.worker_thread_cpu_ns = 0
        self._cpu_started_ns = None
        self.reconnects = 0
        self.error_code = None
        self.backoff_seconds = 0.0

    def _consume(self, client: Any) -> None:
        response = None
        try:
            consume = self.bind_consume() if self.bind_consume else self.consume
            headers = {"Accept": "application/json"}
            if client.secret:
                headers["Authorization"] = f"Bearer {client.secret}"
            with self._lock:
                level = self.level
            suffix = "?level=" + level if self.endpoint == "/logs" else ""
            response = client.opener(Request(client.base_url + self.endpoint + suffix, headers=headers),
                                     timeout=min(3.0, max(0.1, client.timeout)))
            with self._lock:
                self._response = response
            self.on_state("connected")
            while not self.stop_event.is_set():
                # Refresh on each bounded read even while the connection stays
                # open; this also accounts for malformed/oversized records.
                self._refresh_cpu()
                line = response.readline(self.line_limit + 1)
                if not line:
                    self.error_code = "stream_disconnected"
                    return
                if len(line) > self.line_limit:
                    self.note_dropped()
                    while line and not line.endswith(b"\n") and not self.stop_event.is_set():
                        self._refresh_cpu()
                        line = response.readline(self.line_limit + 1)
                    if not line:
                        self.error_code = "stream_disconnected"
                        return
                    continue
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    record = json.loads(text)
                except ValueError:
                    if self.endpoint == "/logs":
                        record = {"type": level, "payload": text}
                    else:
                        self.note_dropped()
                        continue
                if isinstance(record, dict):
                    consume(record)
                    self.error_code = None
                else:
                    self.note_dropped()
                self._refresh_cpu()
        except _StreamBindingExpired:
            self.error_code = "stream_disconnected"
        except Exception:
            self.error_code = "stream_unavailable"
            raise
        finally:
            # Complete the boundary/feed handoff at detection, before close can
            # block. run() must not send a second notification for this attempt.
            try:
                self.on_state("reconnecting")
            finally:
                if response is not None:
                    self._begin_close(response).join(0.1)

    def _begin_close(self, response: Any) -> Thread:
        """One owner-local closer, shared by disconnect cleanup and stop()."""
        with self._lock:
            if response is self._response:
                self._response = None
            if response is self._closing_response:
                return self._close_thread
            def close():
                try:
                    response.close()
                except Exception:
                    pass
            self._closing_response = response
            self._close_thread = Thread(target=close, name="bridge-stream-close", daemon=True)
            self._close_thread.start()
            return self._close_thread

    def _refresh_cpu(self) -> None:
        if self._cpu_started_ns is not None:
            self.worker_thread_cpu_ns = time.thread_time_ns() - self._cpu_started_ns

    def run(self) -> None:
        started = time.thread_time_ns()
        self._cpu_started_ns = started
        delay = self.backoff
        try:
            while not self.stop_event.is_set():
                # A stuck close keeps stdout active without accumulating open
                # responses or helper threads. Waiting remains interruptible.
                if self._close_thread and self._close_thread.is_alive():
                    if self.stop_event.wait(0.1):
                        break
                    continue
                opened = time.monotonic()
                consuming = False
                try:
                    client = self.factory()
                    if client is None:
                        self.error_code = "controller_disabled"
                    else:
                        consuming = True
                        self._consume(client)
                except Exception:
                    self.error_code = "stream_unavailable"
                if not consuming:
                    self.on_state("reconnecting")
                if time.monotonic() - opened >= 30:
                    delay = self.backoff
                self.backoff_seconds = delay
                self.worker_thread_cpu_ns = time.thread_time_ns() - started
                if self.stop_event.wait(delay):
                    break
                self.reconnects += 1
                delay = min(self.max_backoff, delay * 2)
        finally:
            self.worker_thread_cpu_ns = time.thread_time_ns() - started
            self.on_state("stopped")

    def set_level(self, level: str) -> dict:
        """Configure only future /logs connections; never restart Core."""
        if level not in {"info", "debug"}:
            raise ValueError("invalid Mihomo log level")
        with self._lock:
            self.level = level
        return {"level": level, "applied": True}

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        if self._close_thread and self._close_thread.is_alive():
            return
        self._close_thread = None
        self._closing_response = None
        self.stop_event.clear()
        self.thread = Thread(target=self.run, name="bridge" + self.endpoint, daemon=True)
        self.thread.start()

    def stop(self, timeout: float = 2) -> bool:
        deadline = time.monotonic() + max(0, timeout)
        self.stop_event.set()
        with self._lock:
            response = self._response
        if response is not None:
            self._begin_close(response)
        if self._close_thread:
            self._close_thread.join(max(0, deadline - time.monotonic()))
        if self.thread and self.thread is not current_thread():
            self.thread.join(max(0, deadline - time.monotonic()))
        return ((self.thread is None or not self.thread.is_alive())
                and (self._close_thread is None or not self._close_thread.is_alive()))


def _measured_log_work(action):
    """Charge nested producer/capture/flush work once, using thread CPU only."""
    @wraps(action)
    def measured(self, *args, **kwargs):
        depth = getattr(self._work_local, "depth", 0)
        started = time.thread_time_ns() if depth == 0 else 0
        self._work_local.depth = depth + 1
        try:
            return action(self, *args, **kwargs)
        finally:
            self._work_local.depth = depth
            if depth == 0:
                self._charge_work(time.thread_time_ns() - started)
    return measured


class _AdmittedCaptureQueue:
    """Only the handler's final enqueue consumes its prior admission."""
    def __init__(self, owner):
        self.owner = owner

    def put_nowait(self, record):
        return self.owner.put_nowait(record, _admitted=True)

    def note_dropped(self, count=1):
        self.owner.note_dropped(count)


class _MeasuredLogHandler(LogQueueHandler):
    def __init__(self, capture_queue):
        super().__init__(_AdmittedCaptureQueue(capture_queue))
        self.collector = capture_queue
        self._emits = Condition()
        self._accepting = True
        self._active_emits = 0

    def close_intake(self) -> None:
        # Independent of Handler.lock: formatting may hold that lock forever.
        with self._emits:
            self._accepting = False

    def wait_emits(self, timeout: float) -> bool:
        with self._emits:
            return self._emits.wait_for(lambda: self._active_emits == 0, timeout)

    def emit(self, record: logging.LogRecord) -> None:
        with self._emits:
            if not self._accepting:
                return
            self._active_emits += 1
        try:
            self._emit(record)
        finally:
            with self._emits:
                self._active_emits -= 1
                self._emits.notify_all()

    def _emit(self, record: logging.LogRecord) -> None:
        queue = self.collector
        started = time.thread_time_ns()
        depth = getattr(queue._work_local, "depth", 0)
        try:
            # Admission precedes formatting/capture. Nested put_nowait is
            # measured as part of this handler, with no second admission.
            if not queue._admit(log_level(record.levelname)):
                return
            queue._work_local.depth = depth + 1
            try:
                super().emit(record)
            finally:
                queue._work_local.depth = depth
        finally:
            cpu = time.thread_time_ns() - started
            if depth == 0:
                queue._charge_work(cpu)
            with queue._lock:
                queue.logging_handler_cpu_ns += cpu



class LogCollector:
    def __init__(self, state: Any, *, client_factory: Callable[[], Any] | None = None,
                 logger: logging.Logger | None = None, capacity: int = 10_000,
                 period: float = 0.25, batch_limit: int = 1000, batch_bytes: int = 160 * 1024,
                 work_budget_ns: int = 20_000_000, known_secret_values=(),
                 history_items: int = 10_000, history_bytes: int = 16 * 1024 * 1024,
                 core_origin: Callable[[dict], tuple[int, int] | None] | None = None) -> None:
        if (type(capacity) is not int or capacity < 1 or not 0.1 <= period <= 0.25
                or type(batch_limit) is not int or not 1 <= batch_limit <= 1000
                or type(batch_bytes) is not int or batch_bytes < 4096
                or type(work_budget_ns) is not int or work_budget_ns < 1):
            raise ValueError("invalid log bounds")
        self.state, self.capacity, self.period = state, capacity, period
        self.batch_limit, self.batch_bytes = batch_limit, batch_bytes
        self.logger = logger if logger is not None else logging.getLogger("proxy_manager")
        self._ring = deque()
        self._lock, self._flush_lock = Lock(), Lock()
        self._seq, self._next_from = 0, 1
        self.dropped_total, self._reported_total = 0, 0
        self._state_drop_counts = dict.fromkeys(("debug", "info", "warn", "error", "critical"), 0)
        self._stop = Event()
        self._thread = None
        self._handler = None
        self._handler_closer = None
        self.producer_cpu_ns = 0
        self.logging_handler_cpu_ns = 0
        self.worker_thread_cpu_ns = 0
        self.serialization_failures = 0
        self.budget_dropped = 0
        self.work_budget_ns = work_budget_ns
        self._budget_active = False
        # A two-second burst absorbs Windows GetThreadTimes quantization;
        # refill stays 20ms/s by default and every observed CPU ns is debited.
        self.work_burst_ns = 2 * work_budget_ns
        self._work_tokens = float(self.work_burst_ns)
        self._budget_at = time.monotonic()
        self._work_local = local()
        self.work_cpu_ns = 0
        # Separate publication safety ceiling, measured by PublicationState's
        # pending->ack CPU intervals (including retry attempts). This protects
        # the <=0.1-core loaded log path without predicting a per-record cost.
        self.publication_budget_ns = 40_000_000
        self._publication_tokens = float(2 * self.publication_budget_ns)
        self._publication_at = self._budget_at
        self._publication_cpu_seen = state.log_publication_cpu_ns
        self._known_secrets = KnownSecrets(known_secret_values)
        self.view = LogView(max_items=history_items, max_bytes=history_bytes)
        self._ingress_lock = RLock()
        self._source_cursors = {}
        # Legacy identity arguments are accepted but never used across sources.
        self._core_epoch = 0
        self._stdout_retired_generation = -1
        self._active_source = None
        self._stream_cursor = 0
        self._feed_state, self._feed_at = "stopped", ms()
        self._core_generation = None
        self._stdout_generation = None
        self._run_id = None
        # T-P4-C1 #2: current UI language for key-event titles (batch, tail,
        # query). None until the integration attaches a source: the catalog's
        # source templates (zh_CN), exactly as logs.tail/query rendered so far.
        self._language_source: Callable[[], Any] | None = None
        # T-P4-C2 #12 (logs R6): why the feed is not on /logs (register()).
        self._reason_source: Callable[[], Any] | None = None
        self._translator: tuple[Any, Callable[[str], str] | None] = (None, None)
        self._drop_counts = dict.fromkeys(("debug", "info", "warn", "error", "critical"), 0)
        # Repeated literal lines need the same pure capture/classification;
        # timestamps, severity, filtering and sequence are still applied anew.
        self._capture_cache = OrderedDict()
        self.diagnostics = deque(maxlen=64)
        self.stream = (BoundedMihomoStream(client_factory, "/logs", self._ingest_stream,
                                         note_dropped=self.note_dropped,
                                         on_state=self.stream_state,
                                         bind_consume=self._bind_stream) if client_factory else None)

    def _append_record(self, dto, length, *, report=None, run_id=None, source_cursor=None,
                       rendered=None, rendered_length=None):
        """Caller owns _lock; boundaries use the same bounded history and batch queue.

        ``rendered`` (T-P4-C1 #2) is the key-event (title, detail) in the
        current language for the live batch only; history keeps the stored row
        and renders on each query.
        """
        dto["key"] = dto["key"] or RAW_KEY
        dto["is_key_event"] = bool(dto.get("is_key_event", False))
        if run_id:
            dto["run_id"] = run_id
        dto["seq"] = self.view.append(dto, report=report, run_id=run_id,
                                      payload_bytes=length, source_cursor=source_cursor)
        self._run_id, self._seq = run_id, dto["seq"]
        if len(self._ring) == self.capacity:
            removed, _ = self._ring.popleft()
            self.dropped_total += 1
            self._drop_counts[removed["level"]] += 1
        if rendered is not None:
            row = dict(dto, title=rendered[0], detail=rendered[1])
            self._ring.append((row, rendered_length or length))
        else:
            self._ring.append((dto, length))
        self._feed_at = ms()

    # -- T-P4-C1 #2: current-language key-event titles --------------------------
    def set_language_source(self, source: Callable[[], Any] | None) -> None:
        """Attach the configured UI language reader (integration, ``register``).

        Called on producer and query threads: it must be a cheap, lock-free
        read (no deep copy, no I/O). Each new record and every tail/query page
        follows the language it returns.
        """
        with self._lock:
            self._language_source = source

    def set_reason_source(self, source: Callable[[], Any] | None) -> None:
        """Attach the runtime facts behind ``logs.feed.reason`` (``register``).

        ``source()`` returns ``{"core_running": bool, "controller_enabled":
        bool}``; it runs on the sampling thread, so it must be a cheap read
        (no Mihomo request, no I/O).
        """
        with self._lock:
            self._reason_source = source

    def _feed_reason(self, state: str) -> str | None:
        """T-P4-C2 #12: ``controller_disabled|core_not_running|stream_error``.

        Only for a feed that is not ``connected``. Runtime facts first (no
        Core; Core without an external controller, i.e. ``api_client`` is
        None), then the /logs stream's own last error (``controller_disabled``
        when the client factory had no controller; ``stream_disconnected`` /
        ``stream_unavailable`` -> ``stream_error``). No fact -> None.
        """
        if state == "connected":
            return None
        source = self._reason_source
        facts = None
        if source is not None:
            try:
                facts = source()
            except Exception:
                facts = None
        if isinstance(facts, dict):
            if facts.get("core_running") is False:
                return "core_not_running"
            if facts.get("controller_enabled") is False:
                return "controller_disabled"
        code = getattr(self.stream, "error_code", None) if self.stream else None
        if code == "controller_disabled":
            return "controller_disabled"
        if code in {"stream_disconnected", "stream_unavailable"}:
            return "stream_error"
        return None

    def _translate(self) -> Callable[[str], str] | None:
        return self._language_translate()[1]

    def _language_translate(self) -> tuple[Any, Callable[[str], str] | None]:
        source = self._language_source
        try:
            language = source() if source is not None else None
        except Exception:
            language = None
        cached = self._translator
        if cached[0] == language:
            return cached
        translate = log_translator(language)
        self._translator = cached = (language, translate)
        return cached

    def _render(self, report_facts) -> tuple[str, str] | None:
        if not report_facts:
            return None
        try:
            report = build_log_event_report(report_facts, translate=self._translate())
        except Exception:
            return None
        return str(report.headline), str(report.detail)

    def _transition(self, state: str, reason: str, *, force=False) -> None:
        # Caller owns _ingress_lock; feed readers cannot observe a new state
        # until its boundary has been appended. Control records bypass data
        # admission so an exhausted CPU budget cannot silently erase the seam.
        target = {"connected": "mihomo_api", "stdout": "stdout"}.get(state)
        with self._lock:
            now = ms()
            if force or target != self._active_source:
                facts = {"from": self._active_source, "to": target,
                         "at_ms": now, "reason": reason, "continuity": "unknown"}
                text = json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
                dto = {"ts_ms": now, "rx_ms": now, "source": "TESSERACT",
                       "level": "info", "category": "other", "key": "source_boundary",
                       "title": "Core log source boundary", "detail": text, "raw": text,
                       "is_key_event": False}
                length = (len(json.dumps(dto, ensure_ascii=False).encode("utf-8")) + 64
                          + _LOG_FACTS_ALLOWANCE)
                self._append_record(dto, length, run_id=self._run_id)
            self._active_source = target
            self._feed_state, self._feed_at = state, now

    def stream_state(self, state: str) -> None:
        if state not in {"connected", "reconnecting", "stdout", "stopped"}:
            raise ValueError("invalid log feed state")
        with self._ingress_lock:
            if state == "connected":
                # An open HTTP response alone does not restore /logs authority.
                # The first received event performs the transition in _ingest_stream.
                return
            if state == "stopped":
                if self._stdout_generation is not None:
                    self._stdout_retired_generation = max(self._stdout_retired_generation,
                                                          self._stdout_generation)
                self._stdout_generation = None
                self._core_epoch += 1
            if state == "reconnecting" and self._stdout_generation is not None:
                state = "stdout"
            if state == "stdout" and self._stdout_generation is None:
                state = "reconnecting" if self.stream else "stopped"
            self._transition(state, "stopped" if state == "stopped" else "disconnected")

    def stdout_started(self, generation: int) -> bool:
        """Reader owner calls before intake; Core generation increases on restart."""
        if type(generation) is not int or generation < 0:
            raise ValueError("invalid Core generation")
        with self._ingress_lock:
            if (generation <= self._stdout_retired_generation or
                    (self._core_generation is not None and generation < self._core_generation)):
                return False
            changed = self._core_generation is not None and generation != self._core_generation
            if changed:
                self._stdout_retired_generation = max(self._stdout_retired_generation,
                                                      self._core_generation)
                self._core_epoch += 1
            self._core_generation = self._stdout_generation = generation
            if changed or self._active_source != "mihomo_api":
                self._transition("stdout", "core_generation" if changed else "started", force=changed)
            return True

    def stdout_stopped(self, generation: int) -> None:
        """Reader EOF/exit revokes this generation, including late stream input."""
        with self._ingress_lock:
            if generation == self._stdout_generation:
                self._stdout_generation = None
                self._stdout_retired_generation = max(self._stdout_retired_generation, generation)
                self._core_epoch += 1
                self._transition("reconnecting" if self.stream else "stopped", "stopped", force=True)

    def _settle_publication_loss(self) -> None:
        # Caller owns _lock. Only irreversible receipts can affect the feed.
        counts = self.state.settled_log_drop_counts
        changed = counts != self._state_drop_counts
        for level, count in counts.items():
            delta = count - self._state_drop_counts[level]
            self._drop_counts[level] += delta
            self.dropped_total += delta
        self._state_drop_counts = counts
        if changed:
            self._feed_at = ms()

    def feed(self) -> dict:
        with self._lock:
            self._settle_publication_loss()
            counts = dict(self._drop_counts)
            counts["total"] = sum(counts.values())
            dto = dict(state=self._feed_state, level=self.stream.level if self.stream else "info",
                       updated_at_ms=self._feed_at, dropped=counts, run_id=self._run_id)
        # Outside the lock: the reason source reads runtime facts.
        dto["reason"] = self._feed_reason(dto["state"])
        return dto

    def set_mihomo_level(self, args: dict, _context=None) -> dict:
        if not self.stream:
            raise LogNotRunning("Mihomo stream is not configured")
        result = self.stream.set_level(args["level"])
        with self._lock:
            self._feed_at = ms()
        return result

    @_measured_log_work
    def ingest_source(self, record: dict, *, source: str, generation: int, cursor: int,
                      core_event: tuple[int, int] | None = None, _admitted: bool = False) -> bool:
        """Deduplicate ordered offsets only within one source and generation.

        Core transports obey the single active-source rule. Legacy core_event
        is ignored: no common identity or text comparison can prove continuity.
        """
        if source not in {"runtime", "stdout", "logs"} or any(
                type(v) is not int or v < 0 for v in (generation, cursor)):
            raise ValueError("invalid source cursor")
        with self._ingress_lock:
            if source == "stdout" and (generation != self._stdout_generation or
                                        self._active_source != "stdout"):
                return False
            if source == "logs" and self._active_source != "mihomo_api":
                return False
            previous = self._source_cursors.get(source)
            offset = (generation, cursor)
            if previous is not None and offset <= previous:
                return False
            origin = ("mihomo_api" if source == "logs" else source, generation, cursor)
            accepted = (self.ingest_mihomo(record, _admitted=_admitted, _source_cursor=origin)
                        if source == "logs" else
                        self.put_nowait(record, _admitted=_admitted, _source_cursor=origin))
            if accepted:
                self._source_cursors[source] = offset
            return accepted  # A failed admission may be retried at the same cursor.

    @_measured_log_work
    def ingest_stdout(self, line: str, *, generation: int, cursor: int,
                      core_event: tuple[int, int] | None = None) -> bool:
        if any(type(v) is not int or v < 0 for v in (generation, cursor)):
            raise ValueError("invalid source cursor")
        with self._ingress_lock:
            if generation != self._stdout_generation or self._active_source != "stdout":
                return False
            if (generation, cursor) <= self._source_cursors.get("stdout", (-1, -1)):
                return False
            # Admission must precede the event parser, independently of measurement depth.
            if not self._admit(stdout_level(line)):
                return False
            captured = captured_record_from_text_line(line, source="MIHOMO")
            if captured is None:
                return False
            return self.ingest_source(captured, source="stdout", generation=generation,
                                      cursor=cursor, _admitted=True)

    def _bind_stream(self):
        # Freeze before opener/readline: a response belonging to a replaced or
        # stopped Core must not regain authority with a late buffered event.
        with self._ingress_lock:
            epoch = self._core_epoch
        def consume(record):
            with self._ingress_lock:
                if epoch != self._core_epoch:
                    # This differs from ordinary admission rejection (budget,
                    # severity, etc.), which must not tear down a valid stream.
                    raise _StreamBindingExpired("Core log stream binding expired")
                return self._ingest_stream(record, epoch=epoch)
        return consume

    @_measured_log_work
    def _ingest_stream(self, record: dict, *, epoch=None) -> bool:
        with self._ingress_lock:
            if epoch is not None and epoch != self._core_epoch:
                return False
            self._stream_cursor += 1
            self._transition("connected", "recovered")
            return self.ingest_source(record, source="logs", generation=self._core_epoch,
                                      cursor=self._stream_cursor)

    def seed(self, store, *, generation: int = 0) -> int:
        """Read-only LogStore tail adapter; keep the store's source sequence.

        Call before live intake, under the integration's capture barrier, then
        replay live records with source=runtime and this same generation/seq.
        LogStore already owns retention; do not append via its text deduper.
        """
        records = store.snapshot(tab="raw", limit=self.view.max_items)
        count = 0
        for record in records:
            entry = json.loads(serialize_log_records([record]))
            entry["timestamp"], entry["received_at"] = entry["occurred_at"], entry["received_at"]
            count += self.ingest_source(entry, source="runtime", generation=generation,
                                        cursor=record.sequence)
        return count

    @_measured_log_work
    def seed_file(self, path, *, generation: int, limit: int = 500) -> int:
        """Bounded read of a trusted persistent JSONL/monitor tail, on a worker.

        Cursor = byte offset AFTER the line, scoped by file generation. The
        live file follower must use precisely those offsets and generation.
        No LogStore.append_many: its fingerprint deduper can merge equal text.
        A capture barrier must cover opening this tail and attaching its live
        follower. Paths/raw metadata never become public DTO fields.
        """
        if type(limit) is not int or not 1 <= limit <= self.view.max_items:
            raise ValueError("invalid seed limit")
        with Path(path).open("rb") as handle:
            handle.seek(0, 2)
            end = handle.tell()
            start = max(0, end - self.view.max_bytes)
            handle.seek(start)
            data = handle.read(self.view.max_bytes)
        # Split from the right with a count bound as well as a byte bound:
        # millions of tiny lines must not create millions of Python objects.
        lines = data.rsplit(b"\n", limit + 1)
        position = end - len(lines[-1])  # Skip the unfinished final line.
        offsets = []
        for index in range(len(lines) - 2, -1, -1):
            if len(offsets) == limit or (index == 0 and start):
                break
            line = lines[index]
            offsets.append((position, line))
            position -= len(line) + 1
        offsets.reverse()
        count = 0
        for offset, line in offsets:
            if len(line) > 64 * 1024:
                self.note_dropped()
                continue
            text = line.decode("utf-8", errors="replace").strip()
            try:
                entry = json.loads(text)
            except ValueError:
                entry = captured_record_from_text_line(text, source="TESSERACT")
            if not isinstance(entry, dict):
                self.note_dropped()
                continue
            if "occurred_at" in entry:
                entry["timestamp"] = entry["occurred_at"]
            count += self.ingest_source(entry, source="runtime", generation=generation, cursor=offset)
        return count

    def note_dropped(self, count: int = 1, level: str = "info") -> None:
        with self._lock:
            self.dropped_total += max(0, count)
            self._drop_counts[level if level in self._drop_counts else "info"] += max(0, count)
            self._feed_at = ms()

    def _admit(self, level: str = "info") -> bool:
        with self._lock:
            if not self._budget_active:
                return True
            now = time.monotonic()
            self._work_tokens = min(self.work_burst_ns,
                self._work_tokens + max(0, now - self._budget_at) * self.work_budget_ns)
            self._budget_at = now
            publication_cpu = self.state.log_publication_cpu_ns
            self._publication_tokens = min(2 * self.publication_budget_ns,
                self._publication_tokens + max(0, now - self._publication_at) * self.publication_budget_ns)
            self._publication_tokens -= max(0, publication_cpu - self._publication_cpu_seen)
            self._publication_cpu_seen = publication_cpu
            self._publication_at = now
            if self._work_tokens <= 0 or self._publication_tokens <= 0:
                self.budget_dropped += 1
                self.dropped_total += 1
                self._drop_counts[log_level(level)] += 1
                self._feed_at = ms()
                return False
            return True

    def _charge_work(self, cpu_ns: int) -> None:
        with self._lock:
            self.work_cpu_ns += max(0, cpu_ns)
            if self._budget_active:
                # Refill BEFORE subtracting: time spent doing work also earns
                # tokens. Keep debt after a costly record/batch; no reservation.
                now = time.monotonic()
                self._work_tokens = min(self.work_burst_ns, self._work_tokens +
                    max(0, now - self._budget_at) * self.work_budget_ns) - max(0, cpu_ns)
                self._budget_at = now

    def refresh_known_secret_values(self, values) -> None:
        """Compile an owned replacement snapshot off-path; P1-16 calls on reload."""
        replacement = KnownSecrets(values)
        with self._lock:
            self._known_secrets = replacement

    @_measured_log_work
    def put_nowait(self, record: dict, *, _admitted: bool = False,
                   _capture_validated: bool = False, _source_cursor=None) -> bool:
        started = time.thread_time_ns()
        try:
            if not _admitted and not self._admit(
                    log_level(record.get("level", record.get("type")))):
                return False
            if not _capture_validated:
                validate_record(record)
            # Project only transmitted fields, then filter that small owned DTO.
            # Discarded metadata was still validated/bounded above.
            safe = record
            now = ms()
            source = str(safe.get("source", "UNKNOWN")).upper()
            level = str(safe.get("level", safe.get("type", "info"))).lower()
            level = {"warning": "warn", "fatal": "critical"}.get(level, level)
            category = classify_log_category(safe.get("message", safe.get("payload", "")),
                                             source=source, category=safe.get("category"))
            category = {"general": "other", "validation": "health", "tier": "health"}.get(category, category)
            categories = {"runtime", "routing", "health", "circuit_breaker", "update",
                          "configuration", "connection", "alert", "other"}
            raw = safe.get("raw_message", safe.get("message_raw", safe.get("payload", safe.get("message", ""))))
            key = safe.get("reason_code", safe.get("key", safe.get("event_key", ""))) or ""
            metadata = record.get("metadata", {})
            classified = bool(record.get("suspicious_reason") or
                              (isinstance(metadata, dict) and metadata.get("suspicious_reason")) or
                              (isinstance(key, str) and any(word in key.lower() for word in
                                                           ("failed", "failure", "timeout", "rejected"))) or
                              (key and level in {"warn", "error", "critical"}))
            dto = {"ts_ms": timestamp(safe.get("ts_ms", safe.get("timestamp")), now),
                   "rx_ms": timestamp(safe.get("rx_ms", safe.get("received_at")), now),
                   "source": source if source in {"TESSERACT", "MIHOMO", "ALERT"} else "UNKNOWN",
                   "level": level if level in {"debug", "info", "warn", "error", "critical"} else "info",
                   "category": category if category in categories else "other",
                   "key": key, "title": "" if classified else safe.get("title", safe.get("message", raw)),
                   "detail": "" if classified else safe.get("detail", ""),
                   "raw": "" if classified else raw}
            if not all(isinstance(dto[k], str) for k in ("key", "title", "detail", "raw")):
                raise TypeError("invalid log text")
            with self._lock:
                secrets = self._known_secrets
            dto = redact_record(dto, known_secrets=secrets)  # Before queue or sequence.
            length = len(json.dumps(dto, ensure_ascii=False, allow_nan=False).encode("utf-8")) + 64
            if length > min(self.batch_bytes - 512, 16 * 1024):
                raise ValueError("record too large")
            report_facts = {}
            if dto["key"]:
                params = metadata.get("event_params", {}) if isinstance(metadata, dict) else {}
                report_facts = redact_record({"event_key": dto["key"], "source": dto["source"],
                    "level": dto["level"], "message_raw": dto["raw"],
                    "metadata": {"event_params": dict(sanitize_event_params(dto["key"], params)),
                                 "event_match_kind": "event_key"}}, known_secrets=secrets)
            dto["is_key_event"] = is_key_event(dto["key"], record)
            # Live key-event title/detail in the current language, as
            # logs.tail/query render the same report (outside the lock).
            rendered = self._render(report_facts)
            rendered_length = None
            if rendered is not None:
                rendered_length = (length + _LOG_FACTS_ALLOWANCE
                                   + len(json.dumps(list(rendered), ensure_ascii=False).encode("utf-8")))
                if rendered_length > min(self.batch_bytes - 512, 16 * 1024):
                    rendered = rendered_length = None  # Keep the stored text.
            with self._lock:
                run_id = uuid.uuid4().hex if is_run_start(record) else self._run_id
                # P1 already serialized the DTO above. Its +64 allowance
                # covers seq and the log.raw key; the optional Log facts
                # have their own conservative allowance.
                self._append_record(dto, length + _LOG_FACTS_ALLOWANCE, report=report_facts,
                                    run_id=run_id, source_cursor=_source_cursor,
                                    rendered=rendered, rendered_length=rendered_length)
            return True
        except RedactionBudgetExceeded:
            with self._lock:
                self.budget_dropped += 1
                self.dropped_total += 1
                self._drop_counts[log_level(record.get("level", record.get("type")))] += 1
                self._feed_at = ms()
            return False
        except Exception:
            with self._lock:
                self.serialization_failures += 1
                self.dropped_total += 1
                self._drop_counts[log_level(record.get("level", record.get("type")))] += 1
                self._feed_at = ms()
            return False
        finally:
            with self._lock:
                self.producer_cpu_ns += time.thread_time_ns() - started

    @_measured_log_work
    def ingest_mihomo(self, record: dict, *, _admitted: bool = False, _source_cursor=None) -> bool:
        if not _admitted and not self._admit(log_level(record.get("type"))):
            return False
        text = record.get("payload", "")
        if not isinstance(text, str):
            self.note_dropped(level=log_level(record.get("type")))
            return False
        if len(text) > 16_384:
            with self._lock:
                self.budget_dropped += 1
                self.dropped_total += 1
                self._drop_counts[log_level(record.get("type"))] += 1
                self._feed_at = ms()
            return False
        with self._lock:
            template = self._capture_cache.get(text)
            if template is not None:
                self._capture_cache.move_to_end(text)
        if template is None:
            captured = captured_record_from_text_line(text, source="MIHOMO")
            if captured is None:
                return False
            captured["category"] = classify_log_category(captured.get("message", text),
                source="MIHOMO", category=captured.get("category"))
            # Bound retained text and metadata; large lines always take full path.
            cacheable = len(text) <= 2048
            if cacheable:
                try:
                    validate_record(captured)  # Only bounded JSON-compatible captures.
                except (TypeError, ValueError):
                    cacheable = False  # put_nowait owns per-record loss accounting.
            if cacheable:
                with self._lock:
                    self._capture_cache[text] = dict(captured)
                    if len(self._capture_cache) > 128:
                        self._capture_cache.popitem(last=False)
        else:
            captured = dict(template)
            now = ms()
            captured["rx_ms"] = now
            if captured.get("timestamp") == captured.get("received_at"):
                captured["ts_ms"] = now
        captured["level"] = record.get("type", captured["level"])
        # Every cached capture passed validate_record before retention. Its
        # private metadata is unchanged; only two ints and severity are replaced.
        # Reuse that validation for classified captures as well as raw lines.
        validated = (template is not None and
                     isinstance(captured["level"], str) and len(captured["level"]) <= 128)
        return self.put_nowait(captured, _admitted=True, _capture_validated=validated,
                               _source_cursor=_source_cursor)

    @_measured_log_work
    def flush(self) -> dict | None:
        with self._flush_lock:
            with self._lock:
                self._settle_publication_loss()
                dropped = self.dropped_total - self._reported_total
                if not self._ring and not dropped:
                    return None
                rows, size = [], 512
                while self._ring and len(rows) < self.batch_limit:
                    row, length = self._ring[0]
                    if size + length > self.batch_bytes:
                        break
                    self._ring.popleft()
                    rows.append(row)
                    size += length
                dto = {"from_seq": self._next_from, "records": rows, "dropped": dropped,
                       "dropped_total": self.dropped_total, "gap": bool(dropped)}
                if rows:
                    self._next_from = rows[-1]["seq"] + 1
                self._reported_total = self.dropped_total
            try:
                self.state.record_edge("logs.batch", dto)
            except Exception:
                for row in rows:
                    self.note_dropped(level=row["level"])
                errors = protocol.validate_logs_batch(dto)
                code = ("redaction_scan_hit" if any(e.startswith("sensitive:") for e in errors)
                        else "publication_rejected")
                self.diagnostics.append({"source": "logs.batch", "error": code})
            return dto

    def run(self) -> None:
        started = time.thread_time_ns()
        try:
            while not self._stop.wait(self.period):
                self.flush()
                self.worker_thread_cpu_ns = time.thread_time_ns() - started
        finally:
            self.worker_thread_cpu_ns = time.thread_time_ns() - started

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.stream_state("reconnecting" if self.stream else
                          ("stdout" if self._stdout_generation is not None else "stopped"))
        with self._lock:
            self._budget_active = True
            self._work_tokens = float(self.work_burst_ns)
            self._budget_at = time.monotonic()
            self._publication_at = self._budget_at
            self._publication_cpu_seen = self.state.log_publication_cpu_ns
            self._publication_tokens = float(2 * self.publication_budget_ns)
        self._handler = _MeasuredLogHandler(self)
        self.logger.addHandler(self._handler)
        self._thread = Thread(target=self.run, name="bridge-log-batches", daemon=True)
        self._thread.start()
        if self.stream:
            self.stream.start()

    def stop(self, timeout: float = 2) -> bool:
        deadline = time.monotonic() + max(0, timeout)
        handler = self._handler
        if handler:
            handler.close_intake()
        self._stop.set()
        if handler and self._handler_closer is None:
            # Handler close can wait for logging locks; retain its task across
            # retries rather than blocking the owner or claiming completion.
            def close_handler():
                self.logger.removeHandler(handler)
                handler.close()
            self._handler_closer = Thread(target=close_handler,
                name="bridge-log-handler-close", daemon=True)
            self._handler_closer.start()
        stream_closed = True
        if self.stream:
            stream_closed = self.stream.stop(max(0, deadline - time.monotonic()))
        if self._thread:
            self._thread.join(max(0, deadline - time.monotonic()))
        if self._handler_closer:
            self._handler_closer.join(max(0, deadline - time.monotonic()))
        emits_closed = handler is None or handler.wait_emits(max(0, deadline - time.monotonic()))
        closed = (stream_closed and (self._thread is None or not self._thread.is_alive())
                  and (self._handler_closer is None or not self._handler_closer.is_alive())
                  and emits_closed)
        if not closed:
            return False
        with self._lock:
            self._budget_active = False
        self._handler = None
        self._handler_closer = None
        self.stream_state("stopped")
        return True

    @_measured_log_work
    def tail(self, args: dict, _context=None, *, translate=None) -> dict:
        return self.view.tail(**args, **self._page_language(translate))

    @_measured_log_work
    def query(self, args: dict, _context=None, *, translate=None) -> dict:
        return self.view.query(**args, **self._page_language(translate))

    def _page_language(self, translate) -> dict:
        """Renderer and cursor-binding language of one tail/query page (fix S2)."""
        if translate is not None:
            return {"translate": translate, "language": None}
        language, translate = self._language_translate()
        return {"translate": translate, "language": language if isinstance(language, str) else None}

    def clear_view(self) -> int:
        return self.view.clear_view()

    def metrics(self) -> dict:
        with self._lock:
            return {**self.view.metrics(), "producer_cpu_ns": self.producer_cpu_ns,
                    "logging_handler_cpu_ns": self.logging_handler_cpu_ns,
                    "worker_thread_cpu_ns": self.worker_thread_cpu_ns,
                    "stream_thread_cpu_ns": self.stream.worker_thread_cpu_ns if self.stream else 0,
                    "queue_depth": len(self._ring), "dropped_total": self.dropped_total,
                    "serialization_failures": self.serialization_failures,
                    "budget_dropped": self.budget_dropped,
                    "work_cpu_ns": self.work_cpu_ns,
                    "work_budget_ns": self.work_budget_ns,
                    "work_burst_ns": self.work_burst_ns,
                    "publication_budget_ns": self.publication_budget_ns,
                    "publication_cpu_ns": self.state.log_publication_cpu_ns,
                    "diagnostics": list(self.diagnostics)}


def register(session: Any) -> LogCollector:
    """Integration entry (T-P4-C1 #2): key-event titles follow ``ui.language``.

    T-P4-C2 #12: also attaches the ``logs.feed.reason`` facts (Core running,
    external controller enabled) from ``session.access`` / ``session.runtime``.

    Attaches a lock-free reader of the runtime's configured language
    (``runtime.config['ui']['language']``: the dict the settings save and
    every reload replace or update in place) to ``session.logs``. No queries,
    commands or topics change: logs.tail/query stay registered by the wiring.
    """
    logs = session.logs

    def language() -> Any:
        config = getattr(session.runtime, "config", None)
        ui = config.get("ui") if isinstance(config, dict) else None
        return ui.get("language") if isinstance(ui, dict) else None

    logs.set_language_source(language)

    def reasons() -> dict:
        # T-P4-C2 #12: cheap facts only (Core fact record, client attribute).
        core = session.access.core_fact()
        return {"core_running": bool(core["running"]),
                "controller_enabled": getattr(session.runtime, "api_client", None) is not None}

    logs.set_reason_source(reasons)
    return logs
