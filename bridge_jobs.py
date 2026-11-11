"""Runtime-independent P1 job scheduling and truthful result adaptation.

The caller supplies a bounded event recorder. Its pending events are compact
records; the transport calls ``prepare_for_send`` when it submits a pipe write so
that ``sent_at_ms`` is never invented by a worker thread.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
import uuid
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import bridge_protocol as protocol
from bridge_commands import COMMAND_SPECS, PYTHON_COMMANDS, CommandSpec

_P1 = {"service.start", "service.stop", "service.restart", "app.quit"}
_COMMANDS = PYTHON_COMMANDS
_CONTROL = {"service.stop", "app.quit"}
_CHANGE = {"service.start", "service.restart"}
_TERMINAL = {"done", "failed", "cancelled", "superseded", "rejected"}
_UNSET = object()   # a cleanup verdict not read yet
_PHASES = {"collecting", "core_standby", "running", "stopping", "failed", "stopped"}
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]*$")
_SENSITIVE_VALUE = re.compile(
    r"\b[a-z][a-z0-9+.-]{0,63}://\S+|"
    r"\bbearer\s+[a-z0-9._~+/=-]+|"
    r"-----begin\s+(?:(?:rsa|ec|dsa|openssh|encrypted)\s+)?private key-----"
)
_JSON_ESCAPE = re.compile(r'\\(?:u[0-9a-fA-F]{4}|["\\/bfnrt])')
_UNICODE_ESCAPE = re.compile(r"\\u[0-9a-fA-F]{4}")
_TEXT_KEY = re.compile(
    r'"(?P<double_key>[^"\r\n]*)"\s*[:=]|'
    r"'(?P<single_key>[^'\r\n]*)'\s*[:=]|"
    r'(?<![a-z0-9])(?P<bare_key>[a-z0-9]+'
    r'(?:[^a-z0-9"\'=:?&,;{}\r\n]+[a-z0-9]+)*'
    r'[^a-z0-9"\'=:?&,;{}\r\n]*)'
    r'(?:\s*["\'])?\s*[:=]'
)
_JSON_TEXT_KEY = re.compile(
    r'"(?P<key>(?:\\(?:u[0-9a-fA-F]{4}|["\\/bfnrt])|[^"\\\r\n])*)"\s*:'
)
_CREDENTIAL_KEYS = {"password", "passwd", "pwd", "secret", "token", "apikey",
                    "key", "authorization", "cookie", "session", "credential",
                    "credentials"}
_SAFE_KEY_SUFFIXES = {"count", "len", "length", "total", "size", "ms", "s", "id"}
_FORBIDDEN_KEYS = {("monotonic",), ("deadline",), ("subscription", "url"),
                   ("subscriptionurl",), ("uri",), ("uuid",), ("url",)}


def _sensitive_key(value: str) -> bool:
    for _ in range(32):
        decoded = _UNICODE_ESCAPE.sub(lambda match: chr(int(match.group()[2:], 16)), value)
        if decoded == value:
            break
        value = decoded
    else:
        if _UNICODE_ESCAPE.search(value):
            return True
    parts = tuple(re.findall(r"[a-z0-9]+", value.lower()))
    return bool(parts and parts[-1] not in _SAFE_KEY_SUFFIXES
                and (parts[-1] in _CREDENTIAL_KEYS or parts in _FORBIDDEN_KEYS))


def _sensitive_json(value: Any, depth: int) -> bool:
    if depth > 64:
        return True
    if isinstance(value, dict):
        return any(not isinstance(key, str) or _sensitive_key(key)
                   or _sensitive_json(child, depth + 1) for key, child in value.items())
    if isinstance(value, list):
        return any(_sensitive_json(child, depth + 1) for child in value)
    if isinstance(value, str):
        return _private_identifier(value, depth + 1)
    return False


def _private_identifier(value: str, depth: int = 0) -> bool:
    """Apply the v1 wire privacy rule to an opaque identifier."""
    if depth > 64:
        return True
    current = value
    for _ in range(33):
        lowered = current.lower()
        try:
            parsed = json.loads(lowered)
        except (ValueError, RecursionError):
            pass
        else:
            return _sensitive_json(parsed, depth + 1)
        for match in _JSON_TEXT_KEY.finditer(lowered):
            try:
                key = json.loads('"' + match.group("key") + '"')
            except (ValueError, RecursionError):
                continue
            if _sensitive_key(key):
                return True
        if _SENSITIVE_VALUE.search(lowered):
            return True
        for match in _TEXT_KEY.finditer(lowered):
            key = next(value for value in match.group(
                "double_key", "single_key", "bare_key") if value is not None)
            if _sensitive_key(key):
                return True
        decoded = _JSON_ESCAPE.sub(
            lambda m: (chr(int(m.group()[2:], 16)) if m.group()[1] == "u"
                       else {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}.get(
                           m.group()[1], m.group()[1])), current)
        if decoded == current:
            return False
        current = decoded
    return True
_ERROR_CODES = {
    "internal", "not_running", "lifecycle_busy", "timeout", "conflict",
    "cancelled", "superseded", "io", "validation", "admin_required",
    "runtime_unavailable", "runtime_listener_conflict",
    "runtime_listener_unavailable", "source_supply_no_egress", "no_sources",
}


def _ms() -> int:
    return time.time_ns() // 1_000_000


def _nonnegative_int(value: Any) -> bool:
    return type(value) is int and value >= 0


def _error(code: str) -> dict[str, Any]:
    safe = code if isinstance(code, str) and code in protocol.ERROR_CODES else "internal"
    return {"code": safe, "message_key": f"errors.{safe}", "params": {}}


@dataclass(frozen=True)
class Ack:
    """Local, immediate executor admission; not a serialized wire ack."""

    accepted: bool
    op: str | None = None
    skipped_reason: str | None = None
    intent_receipt: IntentReceipt | None = field(default=None, repr=False, compare=False)
    cancel_receipt: CancellationReceipt | None = field(default=None, repr=False, compare=False)
    outlet: str = "job"

    def wait_stop_intent(self, timeout: float | None = None) -> StopIntent | None:
        return self.intent_receipt.wait(timeout) if self.intent_receipt else None


@dataclass(frozen=True)
class StopIntent:
    """Decision produced by the injected stop-intent callable on control worker."""

    accepted: bool
    skipped_reason: str | None = None
    token: Any = None


class IntentReceipt:
    """Local one-shot stop intent result retained by the submitter's Ack."""

    def __init__(self):
        self._ready = threading.Event()
        self._value: StopIntent | None = None

    def resolve(self, value: StopIntent) -> None:
        self._value = value
        self._ready.set()

    def wait(self, timeout: float | None = None) -> StopIntent | None:
        return self._value if self._ready.wait(timeout) else None


class JobFailure(Exception):
    """Explicit safe failure with optional complete partial result facts.

    ``error`` is adopted as the terminal Error when it passes the Error DTO
    (wire) validation and its code is registered for the command (business
    or common code); otherwise the executor reports ``errors.<code>``.
    """

    def __init__(self, code: str, partial_result: dict[str, Any] | None = None,
                 *, error: dict[str, Any] | None = None):
        super().__init__(code)
        self.code = code
        self.partial_result = partial_result
        self.error = error


class _RecoveryGateRefusal(JobFailure):
    """The executor's own mutation-gate refusal (T-P2-31a). Its Error is
    adopted as is for every mutation command, service.start included (it is
    not a start diagnostic, so classify_start_failure never relabels it)."""

    def __init__(self) -> None:
        super().__init__("conflict", error=dict(
            code="conflict", message_key="errors.bridge.recovery_pending",
            params={}, reason_code="recovery_pending"))


class Pending:
    """Deferred completion handle (T-P2-31a).

    A handler returns a Pending instead of its result: the job (or one-shot
    request) stays ``running`` but releases its worker. The owner later calls
    ``complete(result)`` or ``fail(error)`` exactly once, from any thread; the
    usual result adapter, error adoption and cancellation rules then apply.
    The executor abandons the handle when the job ends otherwise: owner/session
    cancellation, a new epoch or shutdown (``epoch``), or the timeout. After
    that ``complete``/``fail`` return False and ``abandoned`` is set.

    * ``timeout`` (seconds) is capped by the executor's ``max_pending_seconds``
      (also the default). On expiry ``on_timeout()`` supplies the outcome
      (a result, or raise JobFailure); without it the job fails ``timeout``.
    * ``on_abandon(reason)`` runs once, outside every executor lock, for any
      abandonment (``timeout``/``cancelled``/``epoch``/...); owners release
      their resources there. Both callbacks must not block.
    * Not allowed for mutation-domain or stop/quit jobs (the whole transaction
      must hold its worker and lock): such a job fails ``internal``.
    A handle belongs to one job; returning it again fails ``internal``.
    """

    def __init__(self, *, timeout: float | None = None,
                 on_timeout: Callable[[], Any] | None = None,
                 on_abandon: Callable[[str], Any] | None = None):
        if timeout is not None and (isinstance(timeout, bool)
                                    or not isinstance(timeout, (int, float)) or not timeout > 0):
            raise ValueError("pending timeout must be positive seconds")
        if (on_timeout is not None and not callable(on_timeout)) or (
                on_abandon is not None and not callable(on_abandon)):
            raise ValueError("pending callbacks must be callable")
        self.timeout = None if timeout is None else float(timeout)
        self._on_timeout = on_timeout
        self._on_abandon = on_abandon
        self._lock = threading.Lock()
        self._state = "open"   # open | settled | abandoned
        self._outcome: tuple[str, Any] | None = None
        self._sink: Callable[[tuple[str, Any]], bool] | None = None
        self._bound = False
        self.abandoned = threading.Event()
        self.abandon_reason: str | None = None

    def complete(self, result: Any) -> bool:
        """Finish with a raw handler result; False when already settled/abandoned."""
        return self._settle(("result", result))

    def fail(self, error: JobFailure | str | dict[str, Any]) -> bool:
        """Finish as a failure (JobFailure, a code, or an Error dict)."""
        if isinstance(error, JobFailure):
            failure = error
        elif isinstance(error, str):
            failure = JobFailure(error)
        elif isinstance(error, dict):
            failure = JobFailure(str(error.get("code") or "internal"), error=error)
        else:
            failure = JobFailure("internal")
        return self._settle(("failure", failure))

    @property
    def settled(self) -> bool:
        with self._lock:
            return self._state != "open"

    def _settle(self, outcome: tuple[str, Any]) -> bool:
        with self._lock:
            if self._state != "open":
                return False
            self._state = "settled"
            sink = self._sink
            if sink is None:
                self._outcome = outcome   # delivered when the executor binds
                return True
        try:
            return bool(sink(outcome))
        except Exception:
            return False

    def _bind(self, sink: Callable[[tuple[str, Any]], bool]) -> tuple[str, Any] | None:
        with self._lock:
            if self._bound:
                raise RuntimeError("pending handle already belongs to a job")
            self._bound = True
            if self._state == "settled":
                outcome, self._outcome = self._outcome, None
                return outcome
            self._sink = sink
            return None

    def _abandon(self, reason: str) -> bool:
        with self._lock:
            if self._state != "open":
                return False
            self._state = "abandoned"
            self._sink = None
            self.abandon_reason = reason
        self.abandoned.set()
        return True


@dataclass(frozen=True)
class CancellationReport:
    """Diagnostic callback evidence: requested is distinct from cleanup done.

    An Event must be set ONLY after resource cleanup. A bare True from a
    diagnostic callback is not evidence of cleanup and is treated as refusal.
    Session callbacks may return bool; it acknowledges revocation, not HTTP abort.
    Both explicit cleanup forms release the waiter even if the business
    function has not returned yet; its eventual return cannot replace terminal.

    ``cleanup_failure`` (T-P2-31a, P2-17): the cleanup-failed receipt. A
    JobFailure, or a non-blocking callable returning one or None, read once
    cleanup is complete. A failure ends the job ``failed`` with its partial
    facts and Error instead of ``cancelled``; the local CancellationReceipt
    sets ``cleanup_failed``.
    """

    requested: bool
    cleanup_complete: threading.Event | bool = False
    cleanup_failure: JobFailure | Callable[[], JobFailure | None] | None = None


@dataclass
class CancellationReceipt:
    requested: bool | None = None
    reported: threading.Event = field(default_factory=threading.Event)
    cleanup_complete: threading.Event = field(default_factory=threading.Event)
    cleanup_failed: threading.Event = field(default_factory=threading.Event)


@dataclass(frozen=True)
class Response:
    """One-shot delivery, not a pipe frame or a public-cache entry."""

    outlet: str
    owner: str | None
    req_id: str | None
    name: str
    ok: bool
    data: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    epoch: str | None = None


@dataclass
class RequestReceipt:
    """Local admission/one-shot waiter for res/private; never a wire ack.

    wait()/take_response() consumes the response once. Revocation wins until
    consumption; the transport must deliver synchronously to the stated owner.
    """

    accepted: bool
    outlet: str
    owner: str | None
    op: str | None = None
    skipped_reason: str | None = None
    finished: threading.Event = field(default_factory=threading.Event, repr=False)
    _take: Callable | None = field(default=None, repr=False)

    def wait(self, timeout: float | None = None) -> Response | None:
        if not self.accepted:
            return None
        return self._take(self.op, self.owner) if self.finished.wait(timeout) else None


class JobEventRecorder:
    """Bounded, caller-owned queue with admission reservations and no wakeups."""

    def __init__(self, capacity: int = 512):
        if capacity < 9:
            raise ValueError("capacity must hold change, stop and quit edges")
        self.capacity = capacity
        self._lock = threading.Lock()
        self._events: deque[dict[str, Any]] = deque()
        self._reserved = 0
        self._inflight: tuple[int, ...] | None = None

    def reserve(self, count: int) -> bool:
        with self._lock:
            if len(self._events) + self._reserved + count > self.capacity:
                return False
            self._reserved += count
            return True

    def append_reserved(self, event: dict[str, Any]) -> None:
        with self._lock:
            if self._reserved < 1:
                raise RuntimeError("job edge has no reserved recorder slot")
            self._reserved -= 1
            self._events.append(event)

    def release(self, count: int) -> None:
        with self._lock:
            if count < 0 or count > self._reserved:
                raise ValueError("invalid reservation release")
            self._reserved -= count

    def prepare_for_send(self, sent_at_ms: int, limit: int | None = None) -> list[dict[str, Any]]:
        """Stamp a pending prefix at pipe submission, without losing it on failure."""
        if not _nonnegative_int(sent_at_ms):
            raise ValueError("sent_at_ms must be nonnegative")
        if limit is not None and (type(limit) is not int or limit < 0):
            raise ValueError("limit must be nonnegative")
        with self._lock:
            if self._inflight is not None:
                raise RuntimeError("a jobs prefix is already in flight")
            count = len(self._events) if limit is None else min(limit, len(self._events))
            events = [deepcopy(self._events[index]) for index in range(count)]
            if events:
                self._inflight = tuple(event["seq"] for event in events)
        return [{**event, "sent_at_ms": sent_at_ms} for event in events]

    def abort_send(self) -> None:
        """Allow retry when the transport did not submit the prepared prefix."""
        with self._lock:
            self._inflight = None

    def ack_sent(self, events: list[dict[str, Any]]) -> None:
        """Remove only the prefix that the caller has submitted to the pipe."""
        with self._lock:
            if self._inflight != tuple(event.get("seq") for event in events):
                raise ValueError("jobs prefix was not prepared for submission")
            if len(events) > len(self._events) or any(
                self._events[index]["seq"] != event.get("seq")
                for index, event in enumerate(events)
            ):
                raise ValueError("sent jobs prefix does not match recorder")
            for _ in events:
                self._events.popleft()
            self._inflight = None

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._events)

    @property
    def reserved(self) -> int:
        with self._lock:
            return self._reserved

    @property
    def available(self) -> int:
        with self._lock:
            return self.capacity - len(self._events) - self._reserved


@dataclass
class JobContext:
    op: str
    superseded: threading.Event = field(default_factory=threading.Event)
    cancelled: threading.Event = field(default_factory=threading.Event)
    before_core_generation: int | None = None
    intent: StopIntent | None = None
    owner_revoked: threading.Event = field(default_factory=threading.Event)
    cancel_receipt: CancellationReceipt = field(default_factory=CancellationReceipt)
    # Ingress marker supplied with submit (bridge_main: (epoch, order)).
    ingress: Any = None


@dataclass(frozen=True)
class _Spec:
    execute: Callable[[dict[str, Any], JobContext], Any]
    adapter: Callable[[_Job, Any], tuple[bool, dict[str, Any] | None, str]]
    before: Callable[[dict[str, Any], JobContext], int] | None = None
    stop_intent: Callable[[dict[str, Any], JobContext], StopIntent] | None = None
    cancel: Callable[[dict[str, Any], JobContext], bool] | None = None
    command: CommandSpec | None = None


@dataclass
class _Job:
    op: str
    name: str
    args: dict[str, Any]
    req_id: str | None
    owner: str | None
    spec: _Spec
    context: JobContext
    state: str = "queued"
    started_at_ms: int | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    slots: int = 3
    cancel_pending: bool = False
    intent_started: bool = False
    finished: threading.Event = field(default_factory=threading.Event)
    intent_receipt: IntentReceipt = field(default_factory=IntentReceipt)
    terminal_at: float | None = None
    acceptance_sent: threading.Event = field(default_factory=threading.Event)
    acceptance_deadline: float | None = None
    response: Response | None = None
    request_receipt: RequestReceipt | None = None
    delivery_revoked: bool = False
    cleanup_signal: threading.Event | None = None
    epoch: str | None = None
    pending: Pending | None = None
    pending_deadline: float | None = None
    deferred_outcome: tuple | None = None
    cleanup_failure: Any = None
    cleanup_verdict: Any = _UNSET


class JobExecutor:
    """Three domains: control, whole-transaction change, bounded ordinary work.

    ``deadline`` in ``shutdown`` is an absolute ``time.monotonic()`` value.
    P1 registration/submission remains compatible with bridge_main's existing
    receipt barrier. P2-03 sets require_acceptance=True for absolute job barriers.
    Intent-confirming stop/quit jobs proceed even when their ack cannot be sent.
    Newly introduced jobs always require the receipt.

    T-P2-31a: ``mutation_gate`` (e.g. ``report.gate.require_idle``) is called
    once when every mutation-domain command starts executing, after its ack;
    if it raises, the job fails ``conflict``/``recovery_pending`` without
    calling the handler. ``max_pending_seconds`` caps every deferred
    completion (``Pending``). Sampling-domain commands run on their own
    single worker; ``inline`` control commands run on the admitting thread.
    """

    def __init__(
        self, recorder: JobEventRecorder, *, epoch: str | None = None,
        terminal_capacity: int = 1024, normal_workers: int = 2,
        normal_queue: int = 16, monotonic: Callable[[], float] = time.monotonic,
        wall_ms: Callable[[], int] = _ms,
        require_acceptance: bool = False, acceptance_timeout: float = 5.0,
        owner_limit: int = 16, request_capacity: int = 32,
        mutation_gate: Callable[[], Any] | None = None,
        max_pending_seconds: float = 900.0,
        _mutation_lock: Any = None,
    ):
        if not isinstance(recorder, JobEventRecorder):
            raise TypeError("a reserving JobEventRecorder is required")
        if terminal_capacity < 259 or normal_workers < 1 or normal_queue < 1:
            raise ValueError("invalid executor capacity")
        if require_acceptance and terminal_capacity < 261:
            raise ValueError("P2 terminal capacity must retain 256 plus control reservations")
        if owner_limit < 1 or request_capacity < 1 or acceptance_timeout <= 0:
            raise ValueError("invalid request capacity or acceptance timeout")
        if mutation_gate is not None and not callable(mutation_gate):
            raise ValueError("mutation gate must be callable")
        if isinstance(max_pending_seconds, bool) or not max_pending_seconds > 0:
            raise ValueError("invalid pending ceiling")
        self._mutation_gate = mutation_gate
        self.max_pending_seconds = float(max_pending_seconds)
        # (callback, command names | None); see add_terminal_listener.
        self._terminal_listeners: list[tuple[Callable[[dict[str, Any]], Any],
                                             frozenset[str] | None]] = []
        # Callbacks that must run outside the executor lock (terminal
        # listeners, Pending.on_abandon); drained by the supervisor. The
        # drain lock is re-entrant (31a review): a callback that ends this
        # executor (begin_shutdown/advance_epoch drain again) on the same
        # thread continues the FIFO instead of deadlocking the supervisor.
        self._callbacks: deque[Callable[[], Any]] = deque()
        self._drain_lock = threading.RLock()
        self.require_acceptance = require_acceptance
        self._p2_mode = require_acceptance
        self.acceptance_timeout = acceptance_timeout
        self.owner_limit = owner_limit
        self.request_capacity = request_capacity
        self._normal_workers, self._normal_queue_size = normal_workers, normal_queue
        self.recorder = recorder
        self.epoch = uuid.uuid4().hex if epoch is None else epoch
        if not isinstance(self.epoch, str) or not self.epoch or _private_identifier(self.epoch):
            raise ValueError("invalid epoch")
        self.terminal_capacity = terminal_capacity
        self._monotonic = monotonic
        self._wall_ms = wall_ms
        self._lock = threading.Lock()
        # One runtime keeps whole-transaction serialization across epochs.
        self._mutation_lock = _mutation_lock if _mutation_lock is not None else threading.Lock()
        self._specs: dict[str, _Spec] = {}
        self._jobs: dict[str, _Job] = {}
        self._requests: dict[str, _Job] = {}
        self._revoked_owners: set[str] = set()
        self._revoked_sessions: set[tuple[str | None, str]] = set()
        self._terminals: deque[str] = deque()
        self._active = 0
        self._counter = 0
        self._seq = 0
        self._rev = 0
        self._closed = False
        self._quitting = False
        self._control_pending: set[str] = set()
        self._control: queue.Queue[_Job | None] = queue.Queue()
        # Same control domain, independent emergency execution opportunity:
        # session end cannot wait behind a long lifecycle stop (§13.5).
        self._emergency: queue.Queue[_Job | None] = queue.Queue(maxsize=1)
        self._change: queue.Queue[_Job | None] = queue.Queue()
        self._cancel: queue.Queue[_Job | None] = queue.Queue()
        self._normal: queue.Queue[Callable[[], Any] | None] = queue.Queue(maxsize=normal_queue)
        # Single-owner state domain (ui.presence): never behind ordinary work.
        self._sampling: queue.Queue[_Job | None] = queue.Queue(maxsize=normal_queue)
        self._threads = [
            self._worker("control", self._control, self._run_job),
            self._worker("change", self._change, self._run_change),
            self._worker("cancel", self._cancel, self._run_cancel),
            self._worker("session-end", self._emergency, self._run_job),
            self._worker("sampling", self._sampling, self._run_job),
        ]
        self._normal_threads = [
            self._worker(f"normal-{index}", self._normal, self._run_normal)
            for index in range(normal_workers)
        ]
        self._supervisor_done = threading.Event()
        self._supervisor = threading.Thread(target=self._supervise,
                                            name="bridge-jobs-receipts", daemon=True)
        self._supervisor.start()

    def _worker(self, name: str, source: queue.Queue, action: Callable[[Any], None]) -> threading.Thread:
        def run() -> None:
            while True:
                try:
                    item = source.get(timeout=.05)
                except queue.Empty:
                    if self._closed:
                        return
                    continue
                if item is None:
                    return
                action(item)

        thread = threading.Thread(target=run, name=f"bridge-jobs-{name}", daemon=True)
        thread.start()
        return thread

    def register(
        self, name: str, execute: Callable[[dict[str, Any], JobContext], Any], *,
        before: Callable[[dict[str, Any], JobContext], int] | None = None,
        stop_intent: Callable[[dict[str, Any], JobContext], StopIntent] | None = None,
        cancel: Callable[[dict[str, Any], JobContext], bool] | None = None,
    ) -> None:
        if not isinstance(name, str) or name not in _COMMANDS or not callable(execute):
            raise ValueError("only Python v1 commands may be registered")
        command = COMMAND_SPECS[name]
        if name not in _P1 and self.terminal_capacity < 261:
            raise ValueError("P2 terminal capacity must retain 256 plus control reservations")
        if command.needs_before and not callable(before):
            raise ValueError("restart requires an independent pre-operation generation")
        if name in _CONTROL and not callable(stop_intent):
            raise ValueError("control stop requires a real intent callable")
        if command.cancellation in {"diagnostic", "session"} and not callable(cancel):
            raise ValueError("command requires a real cancellation callable")
        adapter = lambda job, raw: command.adapter(raw, job.context.before_core_generation)
        with self._lock:
            if name in self._specs or self._closed:
                raise ValueError("command already registered or executor closed")
            self._specs[name] = _Spec(execute, adapter, before, stop_intent, cancel, command)
            if name not in _P1:
                self._p2_mode = True

    @staticmethod
    def _valid_args(name: str, args: Any) -> bool:
        return JobExecutor._args_error(name, args) is None

    @staticmethod
    def _args_error(name: str, args: Any) -> str | None:
        if not isinstance(args, dict):
            return "invalid_type"
        errors: list[str] = []
        protocol._schema(args, COMMAND_SPECS[name].args_schema, "args", errors)
        protocol._command_invariants(name, args, errors)
        if not errors:
            return None
        if any(error.startswith("not_allowed:") for error in errors):
            return "not_allowed"
        if any(error.startswith("missing:") for error in errors):
            return "missing_field"
        # _schema reports enums as type errors. Locate the declared field to
        # classify its existing validation result, without revalidating values.
        path = errors[0].split(":", 1)[1].removeprefix("args.")
        schema: Any = COMMAND_SPECS[name].args_schema
        value: Any = args
        for part in re.findall(r"[^.\[\]]+", path):
            if isinstance(value, dict):
                value = value.get(part)
            elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
                value = value[int(part)]
            else:
                value = None
                break
        for key in re.split(r"\.|\[\d+\]", path):
            if not key:
                continue
            if isinstance(schema, str) and schema.startswith("["):
                schema = schema[1:-1]
            if isinstance(schema, str) and schema in protocol.V1_DTOS:
                schema = protocol.V1_DTOS[schema]
            if not isinstance(schema, dict):
                break
            schema = schema.get(key, schema.get(key + "?"))
        if isinstance(schema, str) and ("|" in schema or schema.startswith("=") or schema == "tier"):
            return "invalid_enum" if isinstance(value, str) else "invalid_type"
        return "invalid_type"

    def submit(
        self, name: str, args: dict[str, Any], req_id: str | None = None,
        owner: str | None = None, *, ingress: Any = None,
    ) -> Ack | RequestReceipt:
        """Admit one command. ``ingress`` is the caller's ingress marker,
        stored on ``JobContext.ingress`` atomically with admission (bridge_main
        passes ``(epoch, order)`` for start/restart; see ``acceptance_sent``).
        An ``inline`` command runs on this calling thread after admission."""
        inline: list[_Job] = []
        reply = self._submit(name, args, req_id, owner, ingress, inline)
        for job in inline:
            self._run_job(job)
        return reply

    def _submit(self, name, args, req_id, owner, ingress, inline) -> Ack | RequestReceipt:
        if not isinstance(name, str) or name not in _COMMANDS:
            return Ack(False, skipped_reason="not_allowed")
        command = COMMAND_SPECS[name]

        def reject(code):
            if command.reply == "job":
                return Ack(False, skipped_reason=code)
            return RequestReceipt(False, command.reply, owner, skipped_reason=code)

        if any(value is not None and (not isinstance(value, str)
                                      or not value or _private_identifier(value))
               for value in (req_id, owner)):
            return reject("not_allowed")
        with self._lock:
            self._reap_locked()
            if (self._closed or (self._quitting and name != "app.quit" and command.capacity_class != "session_end")
                    or name not in self._specs or owner in self._revoked_owners):
                return reject("not_allowed")
            code = self._args_error(name, args)
            if code:
                return reject(code)
            if name not in _P1 and (owner is None or req_id is None):
                return reject("missing_field")
            if command.cancellation == "session" and (owner, args.get("session_id", "")) in self._revoked_sessions:
                return reject("not_allowed")
            owned = sum(job.owner == owner and job.state not in _TERMINAL
                        for job in self._jobs.values())
            owned += sum(job.owner == owner for job in self._requests.values())
            # Stop/quit retain their dedicated capacity even if this owner has
            # exhausted ordinary quota. P1's owner-less internal calls survive.
            if (owner is not None and owned >= self.owner_limit and name not in _CONTROL
                    and command.capacity_class != "session_end"):
                return reject("job_capacity")
            controls = sum(job.name == name and job.state not in _TERMINAL
                           for job in self._jobs.values())
            if name in _CONTROL and controls >= (2 if self._p2_mode else 1):
                return reject("lifecycle_busy")
            if name in _CHANGE and "service.stop" in self._control_pending:
                return reject("lifecycle_busy")
            target = (self._control if command.domain == "control" else
                      self._change if command.domain == "mutation" else
                      self._sampling if command.domain == "sampling" else self._normal)
            if command.capacity_class == "session_end":
                target = self._emergency
            if target in (self._normal, self._emergency, self._sampling) and target.full():
                return reject("job_capacity")
            if command.reply != "job":
                reserve_requests = 2 if self.request_capacity >= 3 and target is not self._emergency else 0
                if len(self._requests) >= self.request_capacity - reserve_requests:
                    return reject("job_capacity")
                self._counter += 1
                op = f"request-{self._counter}"
                receipt = RequestReceipt(True, command.reply, owner, op=op, _take=self.take_response)
                job = _Job(op, name, deepcopy(args), req_id, owner, self._specs[name], JobContext(op),
                           slots=0, request_receipt=receipt, epoch=self.epoch)
                job.context.ingress = ingress
                self._requests[op] = job
                if command.inline:
                    inline.append(job)
                else:
                    target.put_nowait(job)
                return receipt
            # Strict P2 reserves two places EACH for stop and quit. P1's
            # existing direct callers retain the historical 62+1+1 budget.
            reserve_control = 4 if self._p2_mode else 2
            if self._active >= 64 or (name not in _CONTROL and self._active >= 64 - reserve_control):
                return reject("job_capacity")
            retained_and_active = len(self._terminals) + self._active
            # Reserve terminal and recorder space for stop and quit while
            # admitting a change. A stop reserves quit; quit closes admission.
            reserve_jobs = (1 + reserve_control if name not in _CONTROL else
                            (3 if self._p2_mode else 2) if name == "service.stop" else 1)
            if retained_and_active + reserve_jobs > self.terminal_capacity:
                return reject("job_capacity")
            if self.recorder.available < 3 * reserve_jobs:
                return reject("job_capacity")
            if not self.recorder.reserve(3):
                return reject("job_capacity")
            self._counter += 1
            op = f"job-{self._counter}"
            job = _Job(op, name, deepcopy(args), req_id, owner, self._specs[name], JobContext(op))
            job.epoch = self.epoch
            job.context.ingress = ingress
            if command.intent_confirming:
                pass  # Intent confirmation does not grant execution authority.
            elif self.require_acceptance or name not in _P1:
                job.acceptance_deadline = self._monotonic() + self.acceptance_timeout
            else:
                job.acceptance_sent.set()
            self._jobs[op] = job
            self._active += 1
            if name in _CONTROL:
                self._control_pending.add(name)
            if name == "app.quit":
                self._quitting = True
            if name not in _CONTROL:
                self._edge_locked(job)
            target.put_nowait(job)
            return Ack(True, op=op, intent_receipt=(job.intent_receipt
                       if name in _CONTROL else None))

    def submit_normal(self, work: Callable[[], Any]) -> bool:
        """Bounded internal ordinary work; P1 exposes no ordinary wire command."""
        if not callable(work):
            return False
        with self._lock:
            if self._closed or self._quitting:
                return False
            try:
                self._normal.put_nowait(work)
            except queue.Full:
                return False
            return True

    # -- public read/integration surface (T-P2-31a) -------------------------
    # bridge_main and business modules use only these; executor internals
    # (_specs, _lock, _jobs, _requests, _revoked_sessions, _mutation_lock)
    # stay private (tests/test_bridge_p2_31a.py static gate).

    def is_registered(self, name: str) -> bool:
        """Whether a handler is registered for the exact command ``name``."""
        with self._lock:
            return isinstance(name, str) and name in self._specs

    @property
    def accepting(self) -> bool:
        """False once admission is closed (shutdown/epoch end) or quitting."""
        with self._lock:
            return not self._closed and not self._quitting

    def _lookup_locked(self, op: Any) -> _Job | None:
        if not isinstance(op, str):
            return None
        return self._requests.get(op) or self._jobs.get(op)

    def owner_of(self, op: str) -> str | None:
        """Wire owner of the job or one-shot request ``op`` (None = unknown)."""
        with self._lock:
            job = self._lookup_locked(op)
            return None if job is None else job.owner

    def request_owner(self, op: str) -> str | None:
        """Same lookup as ``owner_of`` (request table first)."""
        return self.owner_of(op)

    def job_identity(self, op: str) -> tuple[str, str] | None:
        """``(epoch, owner)`` of ``op``; None when unknown or owner-less."""
        with self._lock:
            job = self._lookup_locked(op)
            if job is None or not job.owner or not job.epoch:
                return None
            return job.epoch, job.owner

    def session_revoked(self, owner: str | None, session_id: str) -> bool:
        """Whether ``cancel``/owner revoke has revoked ``(owner, session_id)``."""
        with self._lock:
            return (owner, session_id) in self._revoked_sessions

    def owner_load(self, owner: str | None) -> int:
        """Non-terminal jobs plus undelivered one-shot requests of ``owner``."""
        with self._lock:
            count = sum(job.owner == owner and job.state not in _TERMINAL
                        for job in self._jobs.values())
            return count + sum(job.owner == owner for job in self._requests.values())

    def run_internal_mutation(self, work: Callable[[], Any], *,
                              abandoned: Callable[[], bool] = lambda: False,
                              poll: float = .05) -> tuple[bool, Any]:
        """Run internal ``work`` inside the whole-transaction mutation domain.

        Shares the change worker's (cross-epoch) mutation lock. Returns
        ``(False, None)`` when ``abandoned()`` became true before ``work``
        started, else ``(True, work())``; exceptions from ``work`` propagate.
        The recovery gate is NOT applied here: internal work carries its own
        guard (bridge_settings_apply's session guard).
        """
        if not callable(work):
            raise ValueError("work must be callable")
        while not self._mutation_lock.acquire(timeout=poll):
            if abandoned():
                return False, None
        try:
            if abandoned():
                return False, None
            return True, work()
        finally:
            self._mutation_lock.release()

    def add_terminal_listener(self, callback: Callable[[dict[str, Any]], Any], *,
                              names: Iterable[str] | None = None) -> None:
        """Call ``callback(event)`` once per job (reply ``job``) reaching a
        terminal state, outside the executor lock, on the executor's
        supervisor thread. ``event``: op, name, epoch, owner, state.

        ``names`` (31a review) subscribes to those command names only, and
        only then does ``event`` also carry ``args`` (an owned copy of the
        accepted args). Without ``names`` the listener sees every job but
        never its args: job args may carry secrets (settings.apply edits,
        alerts.test draft credentials, import.apply text). One-shot
        ``res``/``private`` requests are never reported. Listener exceptions
        are swallowed; listeners must not block. Listeners carry over to the
        executor returned by ``advance_epoch``."""
        if not callable(callback):
            raise ValueError("terminal listener must be callable")
        wanted = None
        if names is not None:
            if isinstance(names, str):
                raise ValueError("names must be an iterable of command names")
            wanted = frozenset(names)
            if not wanted or not all(isinstance(name, str) and name for name in wanted):
                raise ValueError("names must be non-empty command names")
        with self._lock:
            self._terminal_listeners.append((callback, wanted))

    def acceptance_sent(self, op: str, *, epoch: str | None = None,
                        preempt: Callable[[Any], bool] | None = None) -> bool:
        """P2-03 calls ONLY after the accepted ack's pipe write completes.

        Resolve a provisional stop intent via Ack.wait_stop_intent() before
        writing its ack. For intent-confirming jobs this only records delivery;
        it may arrive after the true terminal and never gates cleanup.

        ``preempt(ingress_marker)`` (T-P2-31a): evaluated outside the lock
        for a job admitted with an ingress marker; True cancels the job as
        unexecuted instead of releasing it (bridge_main's stop watermark).
        """
        if preempt is not None:
            with self._lock:
                job = self._jobs.get(op)
                marker = job.context.ingress if job is not None else None
                owner = job.owner if job is not None else None
            if marker is not None and preempt(marker):
                self.cancel(op, owner=owner)
                return False
        with self._lock:
            job = self._jobs.get(op)
            if (job is None or (job.state in _TERMINAL and not job.spec.command.intent_confirming) or
                    (epoch is not None and epoch != self.epoch) or
                    (job.acceptance_deadline is not None and
                     self._monotonic() >= job.acceptance_deadline)):
                return False
            job.acceptance_sent.set()
            return True

    def take_response(self, op: str, owner: str | None) -> Response | None:
        """Consume once, with a final owner check; private data has no cache."""
        with self._lock:
            job = self._requests.get(op)
            if job is None or job.owner != owner or not job.finished.is_set():
                return None
            self._requests.pop(op)
            response, job.response = job.response, None
            if job.delivery_revoked or owner in self._revoked_owners:
                return None
            return response

    def _supervise(self) -> None:
        while not self._supervisor_done.wait(.01):
            timeouts: list[tuple[_Job, Pending]] = []
            verdicts: list[_Job] = []
            with self._lock:
                now = self._monotonic()
                for job in list(self._jobs.values()):
                    if job.state in _TERMINAL:
                        continue
                    if (job.state == "queued" and job.acceptance_deadline is not None
                            and not job.acceptance_sent.is_set()
                            and self._monotonic() >= job.acceptance_deadline
                            and not job.intent_started):
                        self._finish_locked(job, "cancelled")
                    elif job.spec.command.cancellation == "diagnostic":
                        receipt = job.context.cancel_receipt
                        if not job.cancel_pending and receipt.requested:
                            if job.cleanup_signal is not None and job.cleanup_signal.is_set():
                                receipt.cleanup_complete.set()
                            if receipt.cleanup_complete.is_set():
                                if self._cleanup_verdict_ready_locked(job):
                                    self._finish_cancelled_locked(job)
                                else:
                                    verdicts.append(job)
                for job in list(self._jobs.values()) + list(self._requests.values()):
                    if job.state != "running":
                        continue
                    if job.deferred_outcome is not None:
                        self._conclude_deferred_locked(job)
                    elif (job.pending is not None and job.pending_deadline is not None
                            and now >= job.pending_deadline):
                        timeouts.append((job, job.pending))
                stop = self._closed and self._active == 0
            for job in verdicts:
                # Outside the lock: the cleanup-failed receipt, the result
                # adapter and the frame validator (31a review).
                verdict = self._cleanup_verdict(job)
                with self._lock:
                    if job.cleanup_verdict is _UNSET:
                        job.cleanup_verdict = verdict
                    if job.state == "running":
                        self._finish_cancelled_locked(job)
            for job, pending in timeouts:
                self._timeout_pending(job, pending)
            self._drain_callbacks()
            if stop:
                return

    def cancel(self, op: str, owner: str | None = None) -> Ack:
        with self._lock:
            job = self._jobs.get(op) or self._requests.get(op)
            if job is None:
                return Ack(False, skipped_reason="not_running")
            if job.owner != owner:
                # The existing P1 bridge passes no owner on its internal cancel
                # calls; preserve only that explicit compatibility path.
                if not (owner is None and job.name in _P1 and not self.require_acceptance):
                    return Ack(False, skipped_reason="not_allowed")
            if job.state in _TERMINAL:
                return Ack(False, skipped_reason="not_running")
            mode = job.spec.command.cancellation
            if mode in {"none", "dialog"}:
                return Ack(False, skipped_reason="not_cancellable")
            if mode == "owner":
                job.delivery_revoked = True
                job.context.owner_revoked.set()
                self._finish_locked(job, "cancelled")
                if job.request_receipt:
                    self._requests.pop(op, None)
                return Ack(True, op=op)
            if mode == "session":
                self._revoke_session_locked(job)
                self._queue_cancel_locked(job)
                # Revocation stops future polls and delivery, not in-flight HTTP.
                if job.request_receipt:
                    job.request_receipt.finished.set()
                if job.state == "queued":
                    self._finish_locked(job, "cancelled")
                return Ack(True, op=op, cancel_receipt=job.context.cancel_receipt)
            if job.state == "queued" and not job.intent_started:
                self._finish_locked(job, "cancelled")
                return Ack(True, op=op)
            if mode == "queued":
                return Ack(False, skipped_reason="not_cancellable")
            if job.spec.cancel is None:
                return Ack(False, skipped_reason="not_cancellable")
            self._queue_cancel_locked(job)
            return Ack(True, op=op, cancel_receipt=job.context.cancel_receipt)

    def _queue_cancel_locked(self, job: _Job) -> None:
        if not job.cancel_pending:
            job.cancel_pending = True
            job.context.cancel_receipt.reported.clear()
            self._cancel.put_nowait(job)

    def _revoke_session_locked(self, job: _Job) -> None:
        job.delivery_revoked = True
        job.response = None
        job.context.owner_revoked.set()
        session = job.args.get("session_id")
        if isinstance(session, str):
            self._revoked_sessions.add((job.owner, session))
            for sibling in list(self._requests.values()):
                if (sibling is not job and sibling.owner == job.owner and
                        sibling.spec.command.cancellation == "session" and
                        sibling.args.get("session_id") == session):
                    sibling.delivery_revoked = True
                    sibling.response = None
                    sibling.context.owner_revoked.set()
                    if sibling.state == "queued" or sibling.pending is not None:
                        # A deferred sibling holds no worker: end it now so
                        # its handle is abandoned (it leaves _requests here).
                        self._finish_locked(sibling, "cancelled")
                    elif sibling.state == "running":
                        self._queue_cancel_locked(sibling)
                    sibling.request_receipt.finished.set()
                    self._requests.pop(sibling.op, None)
        if job.pending is not None:
            self._finish_locked(job, "cancelled")
        if job.request_receipt:
            job.request_receipt.finished.set()
            self._requests.pop(job.op, None)

    def revoke_owner(self, owner: str) -> None:
        """Window-generation destruction; never cancels a started disk transaction."""
        with self._lock:
            self._revoked_owners.add(owner)
            for job in list(self._jobs.values()) + list(self._requests.values()):
                if job.owner != owner:
                    continue
                command = job.spec.command
                if job.request_receipt or command.cancellation in {"owner", "session"}:
                    job.delivery_revoked = True
                    job.response = None
                    job.context.owner_revoked.set()
                    if command.cancellation == "session" and job.state not in _TERMINAL:
                        self._revoke_session_locked(job)
                        self._queue_cancel_locked(job)
                    if not command.persisted_transaction or job.state == "queued":
                        self._finish_locked(job, "cancelled")
                    if job.request_receipt:
                        job.request_receipt.finished.set()
                        if job.state in _TERMINAL:
                            self._requests.pop(job.op, None)

    def _run_cancel(self, job: _Job) -> None:
        with self._lock:
            mode = job.spec.command.cancellation
            if job.state in _TERMINAL and mode != "session":
                job.cancel_pending = False
                job.context.cancel_receipt.requested = False
                job.context.cancel_receipt.reported.set()
                return
        try:
            report = job.spec.cancel(job.args, job.context) if job.spec.cancel else False
        except Exception:
            report = False
        with self._lock:
            receipt = job.context.cancel_receipt
            if mode == "diagnostic":
                receipt.requested = isinstance(report, CancellationReport) and report.requested is True
                if receipt.requested:
                    job.cleanup_failure = report.cleanup_failure
                    if isinstance(report.cleanup_complete, threading.Event):
                        job.cleanup_signal = report.cleanup_complete
                    elif report.cleanup_complete is True:
                        receipt.cleanup_complete.set()
                        job.context.cancelled.set()
                    else:
                        # requested=True with no completed cleanup remains
                        # pending until the handler supplies evidence via ctx.
                        job.cleanup_signal = receipt.cleanup_complete
            else:
                receipt.requested = report is True or isinstance(report, CancellationReport) and report.requested is True
            if mode == "supersede" and receipt.requested and (job.state == "running"
                                     or (job.state == "queued" and job.intent_started)):
                (job.context.superseded if self.require_acceptance else job.context.cancelled).set()
            receipt.reported.set()
            job.cancel_pending = False

    def _run_normal(self, work: Callable[[], Any] | _Job) -> None:
        if isinstance(work, _Job):
            self._run_job(work)
            return
        try:
            work()
        except Exception:
            pass

    def _run_change(self, job: _Job) -> None:
        # Never wait for the old epoch's transaction under an executor lock.
        # A cancelled/expired queued job must remain cancellable while waiting.
        while not self._mutation_lock.acquire(timeout=.01):
            with self._lock:
                if job.state != "queued":
                    return
        try:
            self._run_job(job)
        finally:
            self._mutation_lock.release()

    def _run_job(self, job: _Job) -> None:
        if job.name in _CONTROL:
            with self._lock:
                if job.state != "queued":
                    return
                job.intent_started = True
            try:
                intent = job.spec.stop_intent(job.args, job.context) if job.spec.stop_intent else None
                if (not isinstance(intent, StopIntent) or type(intent.accepted) is not bool
                        or (intent.skipped_reason is not None and
                            (not isinstance(intent.skipped_reason, str)
                             or intent.skipped_reason not in _ERROR_CODES))):
                    intent = StopIntent(False, "internal")
                elif not intent.accepted and intent.skipped_reason is None:
                    intent = StopIntent(False, "lifecycle_busy")
            except Exception:
                intent = StopIntent(False, "internal")
            with self._lock:
                if job.state != "queued":
                    return
                job.context.intent = intent
                if not intent.accepted:
                    self._reject_provisional_locked(job)
                    return
                job.intent_receipt.resolve(intent)
                self._edge_locked(job)
                self._preempt_changes_locked()
        # Absolute jobs cannot call execute/before until ack leaves the pipe.
        # Intent-confirming jobs must finish truthfully despite receipt loss.
        while (not job.spec.command.intent_confirming
               and not job.acceptance_sent.is_set() and job.request_receipt is None):
            with self._lock:
                if job.state in _TERMINAL:
                    return
                if (job.acceptance_deadline is not None and
                        self._monotonic() >= job.acceptance_deadline):
                    self._finish_locked(job, "cancelled")
                    return
            job.acceptance_sent.wait(.01)
        with self._lock:
            if job.state != "queued":
                return
            job.started_at_ms = self._wall_ms()
            job.state = "running"
            if job.request_receipt is None:
                self._edge_locked(job)
            if job.name in _CONTROL:
                self._preempt_changes_locked()
        outcome: tuple[str, Any] | None = None
        try:
            if (job.spec.command.domain == "mutation" and self._mutation_gate is not None
                    and not job.spec.command.self_gated):
                # T-P2-31a: one recovery-gate check at the mutation entry,
                # after the ack and before any business callback. T-P2-31b
                # (plan b): self-gated commands answer the gate themselves.
                try:
                    self._mutation_gate()
                except Exception:
                    raise _RecoveryGateRefusal() from None
            if job.name == "service.restart":
                before = job.spec.before(job.args, job.context) if job.spec.before else None
                if not _nonnegative_int(before):
                    raise JobFailure("internal")
                job.context.before_core_generation = before
            raw = job.spec.execute(job.args, job.context)
            if isinstance(raw, Pending):
                if self._defer_job(job, raw):
                    return   # the worker is released; the owner settles later
            else:
                outcome = ("result", raw)
        except JobFailure as exc:
            outcome = ("failure", exc)
        except Exception:
            outcome = None
        success, result, code, failure_error = (self._evaluate(job, *outcome) if outcome
                                                else (False, None, "internal", None))
        # Make the last cancellation check and terminal decision atomic. Wait
        # for real diagnostic cleanup outside the executor lock, then recheck.
        while True:
            with self._lock:
                if job.state != "running":
                    return
                receipt = job.context.cancel_receipt
                wait_for = self._conclude_locked(job, success, result, code, failure_error)
                if wait_for is None:
                    return
            wait_for.wait()
            if wait_for is job.cleanup_signal:
                receipt.cleanup_complete.set()

    def _evaluate(self, job: _Job, kind: str, value: Any) -> tuple[bool, Any, str, Any]:
        """(success, result, code, adopted Error|None) of a raw outcome."""
        try:
            if kind == "result":
                if isinstance(value, Pending):
                    return False, None, "internal", None
                success, result, code = (self._adapt_response(job, value) if job.request_receipt
                                         else job.spec.adapter(job, value))
                return success, result, code, None
            exc = value if isinstance(value, JobFailure) else JobFailure("internal")
            _, result, _ = (self._adapt_response(job, exc.partial_result) if job.request_receipt
                            else job.spec.adapter(job, exc.partial_result))
            code = self._safe_code(job, exc.code)
            return False, result, code, self._failure_error(job, code, exc)
        except Exception:
            return False, None, "internal", None

    def _failure_error(self, job: _Job, code: str, exc: JobFailure) -> dict[str, Any] | None:
        error = exc.error
        if not isinstance(error, dict):
            return None
        if isinstance(exc, _RecoveryGateRefusal):
            # 31a review: never routed through the start-failure classifier.
            return deepcopy(error) if self._adoptable_error(job, error) else None
        if job.name == "service.start":
            from bridge_messages import classify_start_failure
            candidate = classify_start_failure(diagnostic=error)
            if candidate["code"] == code:
                return candidate
        return deepcopy(error) if self._adoptable_error(job, error) else None

    @staticmethod
    def _adoptable_error(job: _Job, error: dict[str, Any]) -> bool:
        """T-P2-31a: adopt JobFailure.error when it passes the wire Error DTO
        check (shape and privacy, via the frame validator) and its code is a
        business or common code registered for this command."""
        code = error.get("code")
        if not isinstance(code, str) or code not in job.spec.command.errors:
            return False
        if job.request_receipt:
            frame = dict(t="res", epoch="adapter", produced_at_ms=0, sent_at_ms=0, id=1,
                         req_id="adapter", owner="adapter", cmd=job.name, ok=False, error=error)
        else:
            frame = dict(t="evt", epoch="adapter", produced_at_ms=0, sent_at_ms=0,
                         topic="jobs", rev=1, kind="edge", seq=1,
                         data=dict(op="adapter", name=job.name, state="failed",
                                   started_at_ms=0, result=None, error=error))
        try:
            return not protocol.validate_frame(frame)
        except Exception:
            return False

    def _conclude_locked(self, job: _Job, success: bool, result: Any, code: str,
                         failure_error: Any) -> threading.Event | None:
        """Terminal decision; returns an Event to wait for (outside the lock)
        while a diagnostic cancellation is still unreported/uncleaned."""
        receipt = job.context.cancel_receipt
        wait_for = None
        diagnostic = job.spec.command.cancellation == "diagnostic"
        if diagnostic:
            if job.cancel_pending:
                wait_for = receipt.reported
            elif receipt.requested and not receipt.cleanup_complete.is_set():
                wait_for = job.cleanup_signal
            elif receipt.cleanup_complete.is_set():
                job.context.cancelled.set()
        if wait_for is not None:
            return wait_for
        persisted = job.spec.command.persisted_transaction
        if job.context.superseded.is_set() and not persisted:
            self._finish_locked(job, "superseded")
        elif job.context.cancelled.is_set() and not persisted:
            if diagnostic and receipt.requested and receipt.cleanup_complete.is_set():
                if self._cleanup_verdict_ready_locked(job):
                    self._finish_cancelled_locked(job)
                # else: the supervisor reads the cleanup-failed receipt
                # outside the lock and finishes the job on its next tick.
            else:
                self._finish_locked(job, "cancelled")
        elif success:
            self._finish_locked(job, "done", result=result)
        else:
            self._finish_locked(job, "failed", result=result,
                                error=failure_error or _error(code))
        return None

    @staticmethod
    def _cleanup_verdict_ready_locked(job: _Job) -> bool:
        """True when no cleanup-failed receipt is owed or it was already read."""
        if job.cleanup_failure is None and job.cleanup_verdict is _UNSET:
            job.cleanup_verdict = None
        return job.cleanup_verdict is not _UNSET

    def _cleanup_verdict(self, job: _Job) -> tuple[Any, dict[str, Any]] | None:
        """Outside the executor lock: read the CancellationReport's
        cleanup-failed receipt; None = clean, else (partial result, Error)."""
        failure = job.cleanup_failure
        try:
            failure = failure() if callable(failure) else failure
        except Exception:
            failure = None
        if not isinstance(failure, JobFailure):
            return None
        _, result, code, error = self._evaluate(job, "failure", failure)
        return result, error or _error(code)

    def _finish_cancelled_locked(self, job: _Job) -> None:
        """A diagnostic cancellation whose cleanup completed and whose
        cleanup verdict is known (read outside the lock): ``cancelled``, or
        ``failed`` when the CancellationReport's cleanup-failed receipt
        reported a failure (P2-17)."""
        verdict = job.cleanup_verdict
        if verdict is None or verdict is _UNSET:
            job.context.cancelled.set()
            self._finish_locked(job, "cancelled")
            return
        job.context.cancel_receipt.cleanup_failed.set()
        result, error = verdict
        self._finish_locked(job, "failed", result=result, error=error)

    def _conclude_deferred_locked(self, job: _Job) -> None:
        receipt = job.context.cancel_receipt
        for _ in range(2):
            wait_for = self._conclude_locked(job, *job.deferred_outcome)
            if wait_for is None:
                job.deferred_outcome = None
                return
            if wait_for is job.cleanup_signal and wait_for.is_set():
                receipt.cleanup_complete.set()
                continue
            return

    # -- deferred completion ---------------------------------------------------

    def _defer_job(self, job: _Job, pending: Pending) -> bool:
        """Park a running job on its Pending; False = not allowed (internal)."""
        if job.spec.command.domain == "mutation" or job.name in _CONTROL:
            self._abandon_pending(pending, "unsupported")
            return False
        timeout = self.max_pending_seconds if pending.timeout is None else min(
            pending.timeout, self.max_pending_seconds)
        with self._lock:
            parked = job.state == "running" and job.pending is None
            if parked:
                job.pending = pending
                job.pending_deadline = self._monotonic() + timeout
        if not parked:
            # Already terminal (cancelled/epoch) while the handler ran.
            self._abandon_pending(pending, job.state if job.state in _TERMINAL else "invalid")
            return True
        try:
            early = pending._bind(lambda outcome: self._settle_pending(job, pending, outcome))
        except RuntimeError:
            with self._lock:
                if job.pending is pending:
                    job.pending = job.pending_deadline = None
            return False   # a handle reused by a second job
        if early is not None:
            self._settle_pending(job, pending, early)
        return True

    def _settle_pending(self, job: _Job, pending: Pending, outcome: tuple[str, Any]) -> bool:
        with self._lock:
            if job.pending is not pending or job.state != "running":
                return False
        success, result, code, failure_error = self._evaluate(job, *outcome)
        with self._lock:
            if job.pending is not pending or job.state != "running":
                return False
            job.pending = job.pending_deadline = None
            if self._conclude_locked(job, success, result, code, failure_error) is not None:
                # Diagnostic cleanup still owed: the supervisor finishes it.
                job.deferred_outcome = (success, result, code, failure_error)
        return True

    def _timeout_pending(self, job: _Job, pending: Pending) -> None:
        if not pending._abandon("timeout"):
            return   # the owner settled first; that outcome wins
        self._call_abandon(pending, "timeout")
        outcome: tuple[str, Any] = ("failure", JobFailure("timeout"))
        if pending._on_timeout is not None:
            try:
                value = pending._on_timeout()
                outcome = (("failure", JobFailure("internal")) if isinstance(value, Pending)
                           else ("result", value))
            except JobFailure as exc:
                outcome = ("failure", exc)
            except Exception:
                outcome = ("failure", JobFailure("internal"))
        self._settle_pending(job, pending, outcome)

    @staticmethod
    def _call_abandon(pending: Pending, reason: str) -> None:
        if pending._on_abandon is not None:
            try:
                pending._on_abandon(reason)
            except Exception:
                pass

    def _abandon_pending(self, pending: Pending, reason: str) -> None:
        """Outside the executor lock."""
        if pending._abandon(reason):
            self._call_abandon(pending, reason)

    def _drain_callbacks(self) -> None:
        """Run queued callbacks in FIFO order, outside the executor lock.

        Re-entrant on the same thread (a callback may shut this executor
        down); callbacks still must not block, since a blocked callback
        delays the supervisor's timeout/cancel handling."""
        with self._drain_lock:
            while True:
                try:
                    callback = self._callbacks.popleft()
                except IndexError:
                    return
                try:
                    callback()
                except Exception:
                    pass

    def _notify_terminal(self, event: dict[str, Any], args: Any) -> None:
        with self._lock:
            listeners = list(self._terminal_listeners)
        for listener, wanted in listeners:
            if wanted is not None and event["name"] not in wanted:
                continue
            delivered = dict(event)
            if wanted is not None:
                delivered["args"] = deepcopy(args)
            try:
                listener(delivered)
            except Exception:
                pass

    @staticmethod
    def _safe_code(job: _Job, code: str) -> str:
        # P1 stop adapters historically emit timeout as a cleanup fact; the
        # stricter business-code rule applies to explicitly thrown failures.
        if job.name == "service.start":
            from bridge_messages import START_FAILURE_CODES
            if isinstance(code, str) and code in START_FAILURE_CODES:
                return code
        return code if isinstance(code, str) and code in job.spec.command.errors else "internal"

    @staticmethod
    def _adapt_response(job: _Job, raw: Any):
        frame = dict(t="res", epoch=job.epoch, produced_at_ms=0, sent_at_ms=0,
                     id=1, req_id=job.req_id, owner=job.owner, cmd=job.name,
                     ok=True, data=raw)
        if protocol.validate_frame(frame):
            return False, None, "internal"
        return True, deepcopy(raw), "internal"

    def _reject_provisional_locked(self, job: _Job) -> None:
        """An intent rejected before admission has no wire job or edges."""
        job.state = "rejected"
        job.terminal_at = self._monotonic()
        self.recorder.release(job.slots)
        job.slots = 0
        self._active -= 1
        if not any(other.name == job.name and other.state not in _TERMINAL
                   for other in self._jobs.values()):
            self._control_pending.discard(job.name)
        job.intent_receipt.resolve(job.context.intent or StopIntent(False, "internal"))
        self._jobs.pop(job.op)
        job.finished.set()

    def _preempt_changes_locked(self) -> None:
        for other in list(self._jobs.values()):
            if other.spec.command.domain != "mutation":
                continue
            if other.state == "queued":
                self._finish_locked(other, "cancelled")
            elif other.state == "running" and not other.spec.command.persisted_transaction:
                other.context.superseded.set()

    @staticmethod
    def _adapt_start(job: _Job, raw: Any) -> tuple[bool, dict[str, Any] | None, str]:
        if not isinstance(raw, dict):
            return False, None, "internal"
        phase, failure = raw.get("startup_phase"), raw.get("failure_code")
        if (not isinstance(phase, str) or phase not in _PHASES
                or "failure_code" not in raw
                or (failure is not None and (not isinstance(failure, str)
                    or not _SAFE_CODE.fullmatch(failure)))):
            return False, None, "internal"
        result = {"startup_phase": phase, "failure_code": failure}
        good = phase in {"collecting", "core_standby", "running"} and failure is None
        return good, result, failure if failure in _ERROR_CODES else "conflict"

    @staticmethod
    def _adapt_stop(job: _Job, raw: Any) -> tuple[bool, dict[str, Any] | None, str]:
        if not isinstance(raw, dict):
            return False, None, "internal"
        phase, code, pending = (raw.get("startup_phase"),
                                raw.get("shutdown_exit_code"), raw.get("cleanup_pending"))
        if (not isinstance(phase, str) or phase not in _PHASES
                or "shutdown_exit_code" not in raw
                or (code is not None and not _nonnegative_int(code))
                or type(pending) is not bool):
            return False, None, "internal"
        result = {"startup_phase": phase, "shutdown_exit_code": code,
                  "cleanup_pending": pending}
        good = phase == "stopped" and code in (None, 0) and not pending
        reason = "io" if code not in (None, 0) else "timeout"
        return good, result, reason

    @staticmethod
    def _adapt_restart(job: _Job, raw: Any) -> tuple[bool, dict[str, Any] | None, str]:
        if not isinstance(raw, dict):
            return False, None, "internal"
        applied, running, pending, generation = (
            raw.get("applied"), raw.get("core_running"),
            raw.get("core_cleanup_pending"), raw.get("core_generation")
        )
        if (any(type(value) is not bool for value in (applied, running, pending))
                or not _nonnegative_int(generation)):
            return False, None, "internal"
        result = {"applied": applied, "core_running": running,
                  "core_cleanup_pending": pending, "core_generation": generation}
        before = job.context.before_core_generation
        good = (applied and running and not pending and before is not None
                and generation > before)
        return good, result, "conflict"

    def _edge_locked(self, job: _Job) -> None:
        self._seq += 1
        self._rev += 1
        data: dict[str, Any] = {"op": job.op, "name": job.name, "state": job.state,
                                "started_at_ms": job.started_at_ms}
        if job.req_id is not None:
            data["req_id"] = job.req_id
        if job.owner is not None:
            data["owner"] = job.owner
        if job.result is not None:
            data["result"] = deepcopy(job.result)
        if job.error is not None:
            data["error"] = deepcopy(job.error)
        event = {"t": "evt", "epoch": job.epoch, "produced_at_ms": self._wall_ms(),
                 "topic": "jobs", "rev": self._rev, "kind": "edge", "seq": self._seq,
                 "data": data}
        self.recorder.append_reserved(event)
        job.slots -= 1

    def _finish_locked(
        self, job: _Job, state: str, *, result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None, abandon_reason: str | None = None,
    ) -> None:
        if job.state in _TERMINAL:
            return
        if job.pending is not None:
            # Ended otherwise than by its owner: the handle becomes invalid
            # and the owner's on_abandon runs outside this lock.
            pending, job.pending, job.pending_deadline = job.pending, None, None
            reason = abandon_reason or state
            if pending._abandon(reason):
                self._callbacks.append(lambda: self._call_abandon(pending, reason))
        if job.request_receipt:
            job.state = state
            if not job.delivery_revoked and job.owner not in self._revoked_owners:
                job.response = Response(job.spec.command.reply, job.owner, job.req_id, job.name,
                                        state == "done", deepcopy(result) if state == "done" else None,
                                        deepcopy(error) if error else _error("cancelled") if state != "done" else None,
                                        epoch=job.epoch)
            # Private facts never become job.result, a jobs edge, or retention.
            job.finished.set()
            job.request_receipt.finished.set()
            if job.delivery_revoked:
                self._requests.pop(job.op, None)
            return
        if job.name in _CONTROL and job.slots == 3:
            self._edge_locked(job)
            job.intent_receipt.resolve(job.context.intent or StopIntent(False, "cancelled"))
        job.state, job.result, job.error = state, result, error
        job.terminal_at = self._monotonic()
        self._edge_locked(job)
        if job.slots:
            self.recorder.release(job.slots)
            job.slots = 0
        self._active -= 1
        self._terminals.append(job.op)
        if job.name in _CONTROL:
            if not any(other.name == job.name and other.state not in _TERMINAL
                       for other in self._jobs.values()):
                self._control_pending.discard(job.name)
        job.finished.set()
        if self._terminal_listeners:
            event = {"op": job.op, "name": job.name,
                     "epoch": job.epoch, "owner": job.owner, "state": state}
            # Copy args only when a name-filtered listener asked for them.
            args = (deepcopy(job.args) if any(
                wanted is not None and job.name in wanted
                for _, wanted in self._terminal_listeners) else None)
            self._callbacks.append(lambda: self._notify_terminal(event, args))

    def _reap_locked(self) -> None:
        now = self._monotonic()
        while len(self._terminals) > 256:
            oldest = self._jobs[self._terminals[0]]
            if oldest.terminal_at is None or now - oldest.terminal_at < 600:
                break
            self._jobs.pop(self._terminals.popleft())

    def get_job(self, op: str) -> dict[str, Any] | None:
        with self._lock:
            self._reap_locked()
            job = self._jobs.get(op)
            if job is None or job.state == "rejected":
                return None
            return {"op": job.op, "name": job.name, "state": job.state,
                    "started_at_ms": job.started_at_ms, "result": deepcopy(job.result),
                    "error": deepcopy(job.error)}

    def wait(self, op: str, timeout: float | None = None) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(op)
        if job is None or not job.finished.wait(timeout):
            return None
        return self.get_job(op)

    def shutdown(self, deadline: float) -> bool:
        """Reject new work, cancel queued work, and wait only until deadline.

        Already admitted stop/quit jobs stay on their control worker. A stuck
        ordinary worker is daemonized and is never joined here.
        """
        self.begin_shutdown()
        for worker in self._threads:
            worker.join(max(0.0, deadline - self._monotonic()))
        with self._lock:
            pending = any(job.state not in _TERMINAL for job in
                          list(self._jobs.values()) + list(self._requests.values()))
        return all(not worker.is_alive() for worker in self._threads) and not pending

    def begin_shutdown(self) -> None:
        """Close admission and request cancellation without joining a worker.

        A quit job runs on the control worker, so it cannot call shutdown(),
        which joins that same worker. The owner thread may join later.
        """
        with self._lock:
            if not self._closed:
                self._closed = True
                self._close_waiters_locked()
                self._control.put_nowait(None)
                self._change.put_nowait(None)
                self._cancel.put_nowait(None)
                try:
                    self._emergency.put_nowait(None)
                except queue.Full:
                    pass
                while True:
                    try:
                        self._normal.get_nowait()
                    except queue.Empty:
                        break
                for _ in self._normal_threads:
                    try:
                        self._normal.put_nowait(None)
                    except queue.Full:
                        break
                # Queued sampling requests were cancelled above; a full queue
                # still ends its worker through the closed/empty poll.
                try:
                    self._sampling.put_nowait(None)
                except queue.Full:
                    pass
        self._drain_callbacks()

    def prepare_quit(self) -> None:
        """Cancel ordinary queued work before requesting the runtime stop.

        Submit(app.quit) has already closed admission. Leave the control
        worker available so a failed stop-intent request can be retried.
        """
        with self._lock:
            self._close_waiters_locked()
            while True:
                try:
                    self._normal.get_nowait()
                except queue.Empty:
                    break

    def _close_waiters_locked(self) -> None:
        for job in list(self._jobs.values()) + list(self._requests.values()):
            if job.request_receipt and job.state in _TERMINAL:
                # A completed unconsumed reply belongs to this closing epoch.
                # Replace transient private data with an explicit waiter error.
                job.response = (None if job.delivery_revoked else Response(
                    job.spec.command.reply, job.owner, job.req_id, job.name, False,
                    error=_error("runtime_gone"), epoch=job.epoch))
            if job.state in _TERMINAL or job.name in _CONTROL:
                continue
            command = job.spec.command
            if job.pending is not None:
                # A deferred completion never outlives its epoch: the handle
                # is invalidated, its waiters end with runtime_gone.
                self._finish_locked(job, "failed", error=_error("runtime_gone"),
                                    abandon_reason="epoch")
                if command.cancellation == "session":
                    self._revoke_session_locked(job)
                continue
            if job.state == "queued":
                if command.cancellation == "session":
                    self._revoke_session_locked(job)
                    self._queue_cancel_locked(job)
                self._finish_locked(job, "cancelled")
            elif command.persisted_transaction:
                continue  # Preserve the eventual saved/applied facts.
            elif command.cancellation == "diagnostic":
                self._queue_cancel_locked(job)
            elif job.name in _P1 and not self.require_acceptance:
                if job.spec.cancel:
                    self._queue_cancel_locked(job)
            else:
                if command.cancellation == "session":
                    self._revoke_session_locked(job)
                    self._queue_cancel_locked(job)
                    job.request_receipt.finished.set()
                self._finish_locked(job, "failed", error=_error("runtime_gone"))

    def advance_epoch(self, epoch: str, recorder: JobEventRecorder) -> JobExecutor:
        """Close old waiters and return a fresh executor/event stream.

        Started persisted transactions finish on the OLD executor/epoch. Never
        relabel their eventual facts or reset seq beneath pending recorder edges.
        P2-03 retains/drains the old executor until these transactions settle.
        All successors share the runtime's whole-mutation transaction lock.
        """
        replacement = JobExecutor(
            recorder, epoch=epoch, terminal_capacity=self.terminal_capacity,
            normal_workers=self._normal_workers, normal_queue=self._normal_queue_size,
            monotonic=self._monotonic, wall_ms=self._wall_ms,
            require_acceptance=self.require_acceptance, acceptance_timeout=self.acceptance_timeout,
            owner_limit=self.owner_limit, request_capacity=self.request_capacity,
            mutation_gate=self._mutation_gate, max_pending_seconds=self.max_pending_seconds,
            _mutation_lock=self._mutation_lock)
        with self._lock:
            replacement._specs = dict(self._specs)
            replacement._p2_mode = self._p2_mode
            replacement._terminal_listeners = list(self._terminal_listeners)
        self.begin_shutdown()
        return replacement
