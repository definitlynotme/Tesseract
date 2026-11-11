"""Post-dial observer tap and thread (node-judgement design v3 P1-1 and P1-3).

The stdout reader thread only offers raw lines to ``PostDialTap``: attribute
checks, one bounded ``deque.append`` and, when the observer sleeps, one
``Event.set``.  Parsing, joining, ``GET /connections`` and trigger evaluation
all run on the ``runtime-post-dial-observer`` thread owned by
``PostDialObserverRunner`` (§18.4).

With no user traffic the observer is dormant: it waits on the tap's Event with
no timeout and makes no controller call (invariant 10).  It never calls
``/proxies`` and it never probes, closes or switches anything itself: act-rule
triggers, accepted snapshot digests and demotion-watch steps are handed to
injected hooks (the P2 judge, ``runtime_post_dial_judge``).  During a
demotion watch it polls at 1 Hz only while a user connection on the demoted
leaf exists (§6).

Privacy: raw lines exist only in the tap's bounded deque until they are
drained; every record this module emits carries tokens and hashed leaves only
(``runtime_post_dial``), and ``format_observer_record`` keeps only a safe
character set.
"""

from __future__ import annotations

import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from runtime_post_dial import (
    DemotionWatch,
    OriginRules,
    PostDialCpuThrottle,
    PostDialObserverCore,
    RawBodyError,
    SnapshotDigest,
    StuckConnections,
    Trigger,
    activity_record,
    parse_open_line,
    trigger_record,
)

OBSERVER_THREAD_NAME = "runtime-post-dial-observer"
# §18.8: one summary at most every 5 min, only while the observer is active.
SUMMARY_INTERVAL_S = 300.0
TAP_MAXLEN = 512
# ``mode()`` parses the policy from the config.  A loop pass that only sleeps
# one fast period may reuse a reading at most this old; a pass that polls,
# waits longer or goes dormant always reads it afresh, so no poll, trigger or
# act-rule decision uses an older policy than before (one reading per poll).
MODE_REFRESH_S = 0.25
# Records the observer core emits per drained line; ``open`` is never logged.
_DEBUG_RECORDS = frozenset({"outcome", "active", "dormant", "epoch"})
_SAFE_VALUE = re.compile(r"[^A-Za-z0-9_.:+-]")

Emit = Callable[[str, Mapping[str, Any]], None]


class PostDialTap:
    """The reader-thread side of the observer (P1-1).

    ``offer`` never parses, logs or blocks: it compares the line's process
    with the lease's, appends ``(received_at_monotonic, raw_line)`` to a
    ``deque(maxlen=512)`` and sets the Event only when the observer has armed
    a wake-up (dormant, or asleep for longer than one fast poll period).
    Nothing is appended once the observer has stopped.
    """

    __slots__ = (
        "_lines",
        "_event",
        "_alive",
        "_wake_armed",
        "_pid",
        "_process_generation",
        "maxlen",
        "offered",
        "overflow",
    )

    def __init__(self, *, pid: int, process_generation: int, maxlen: int = TAP_MAXLEN) -> None:
        self.maxlen = max(1, int(maxlen))
        self._lines: deque[tuple[float, str]] = deque(maxlen=self.maxlen)
        self._event = threading.Event()
        self._alive = True
        self._wake_armed = False
        self._pid = int(pid)
        self._process_generation = int(process_generation)
        self.offered = 0
        self.overflow = 0

    def __repr__(self) -> str:
        return f"PostDialTap(alive={self._alive}, pending={len(self._lines)})"

    @property
    def alive(self) -> bool:
        return self._alive

    @property
    def event(self) -> threading.Event:
        return self._event

    def offer(self, event: Any) -> bool:
        """Reader thread: enqueue one raw Core line of the lease's process."""

        if (
            not self._alive
            or event.pid != self._pid
            or event.process_generation != self._process_generation
        ):
            return False
        lines = self._lines
        if len(lines) >= self.maxlen:
            self.overflow += 1
        lines.append((event.received_at_monotonic, event.raw_line))
        self.offered += 1
        if self._wake_armed:
            # The dormant -> active transition: one Event.set per sleep.
            self._wake_armed = False
            self._event.set()
        return True

    def drain(self) -> list[tuple[float, str]]:
        """Observer thread: take every queued line, oldest first."""

        drained: list[tuple[float, str]] = []
        lines = self._lines
        while True:
            try:
                drained.append(lines.popleft())
            except IndexError:
                return drained

    def arm_wake(self) -> bool:
        """Ask the reader to set the Event on the next line.

        Returns True when lines are already queued, in which case the caller
        must not sleep (the append raced the arming).
        """

        self._event.clear()
        self._wake_armed = True
        if self._lines or not self._alive:
            self._wake_armed = False
            return True
        return False

    def disarm(self) -> None:
        self._wake_armed = False

    def close(self) -> None:
        """Stop accepting lines, drop the queued ones and wake the observer."""

        self._alive = False
        self._wake_armed = False
        self._lines.clear()
        self._event.set()


@dataclass(frozen=True, slots=True)
class PostDialRouteContext:
    """TIER3-AUTO membership the observer judges (``all`` minus Reject)."""

    tier3_members: frozenset[str]
    gateway_groups: frozenset[str] = frozenset()

    def __repr__(self) -> str:
        return (
            f"PostDialRouteContext(tier3_members=<{len(self.tier3_members)}>, "
            f"gateway_groups=<{len(self.gateway_groups)}>)"
        )


def format_observer_record(record: Mapping[str, Any]) -> str:
    """``key=value`` text of one record; values reduced to a safe charset."""

    parts: list[str] = []
    for key, value in record.items():
        name = _SAFE_VALUE.sub("_", str(key))[:48]
        parts.append(f"{name}={_format_value(value)}")
    return " ".join(parts)


def format_observer_value(value: Any) -> str:
    """One record value as ``format_observer_record`` renders it."""

    return _format_value(value)


def thread_cpu_clock() -> Callable[[], float] | None:
    """A reader of the calling thread's CPU seconds; call it on that thread only.

    Windows accounts ``time.thread_time()`` (GetThreadTimes) in whole timer
    ticks, and a thread woken by a timer runs just after a tick, so short
    bursts are mostly never charged (a 1 ms burst per second reads as 0,
    1 ms per 20 ms as 4.9 % against 3.6 %).  There the reader counts the
    thread's cycles (QueryThreadCycleTime on the current-thread
    pseudo-handle, through ``ctypes.PyDLL`` so the GIL is held: a
    non-blocking query must not pay a contended GIL handoff) at the
    processor's rated clock, and when that is unavailable there is no reader
    (None: the budget stays off rather than act on a wrong share).
    Elsewhere ``time.thread_time``.
    """

    global _CYCLE_QUERY
    if sys.platform == "win32":
        if _CYCLE_QUERY is False:
            return None
        try:
            if _CYCLE_QUERY is None:
                # Once per process: the registry read releases the GIL.
                _CYCLE_QUERY = _windows_cycle_query()
            return _windows_cycle_clock(*_CYCLE_QUERY)
        except Exception:
            _CYCLE_QUERY = False
            return None
    return time.thread_time


# ``(QueryThreadCycleTime, current-thread pseudo-handle, rated Hz)``; False
# once it proved unavailable.
_CYCLE_QUERY: Any = None


def _windows_cycle_query() -> tuple[Any, Any, float]:  # pragma: no cover - Windows only
    import ctypes
    import winreg
    from ctypes import wintypes

    key_path = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
        mhz, _kind = winreg.QueryValueEx(key, "~MHz")
    hz = float(mhz) * 1e6
    if not 1e8 <= hz <= 2e10:
        raise ValueError("processor clock out of range")
    kernel32 = ctypes.PyDLL("kernel32")
    query = kernel32.QueryThreadCycleTime
    query.restype = wintypes.BOOL
    query.argtypes = (wintypes.HANDLE, ctypes.POINTER(ctypes.c_ulonglong))
    current = kernel32.GetCurrentThread
    current.restype = wintypes.HANDLE
    current.argtypes = ()
    return query, current(), hz  # the pseudo-handle means whichever thread calls


def _windows_cycle_clock(query: Any, handle: Any, hz: float) -> Callable[[], float]:  # pragma: no cover
    import ctypes

    cycles = ctypes.c_ulonglong(0)
    ref = ctypes.byref(cycles)
    if not query(handle, ref):
        raise OSError("QueryThreadCycleTime failed")

    def clock() -> float:
        query(handle, ref)
        return cycles.value / hz

    return clock


def _format_value(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(round(value, 4))
    if isinstance(value, (list, tuple, set, frozenset)):
        return ",".join(_format_value(item) for item in value) or "-"
    return _SAFE_VALUE.sub("_", str(value))[:64] or "-"


class PostDialObserverRunner:
    """The ``runtime-post-dial-observer`` thread (§18.4) around the pure core.

    Every dependency is injected so the runtime keeps its own locks and the
    tests can drive the real thread with a fake controller:

    - ``controller_factory()`` returns an object with ``connections(*,
      timeout_seconds)`` and ``close()`` (``MihomoControllerKeepAlive``), or
      None when no controller is configured.  It is called lazily, on this
      thread, before the first poll.  When the object also has
      ``connections_raw(*, timeout_seconds)``, polls take the undecoded body
      and ``PostDialObserverCore.ingest_raw`` decodes only what changed.
    - ``is_current()`` is an in-memory check that the monitoring lease this
      observer was started for is still the committed one.
    - ``mode()`` returns ``(post_dial_judge, act_rule_ids)``; ``off`` stops
      the observer.
    - ``route_snapshot()`` returns the published route snapshot (compared by
      identity) and ``route_context_for(snapshot)`` its TIER3 context.
    - ``network_epoch()`` returns ``(epoch, changed_at_wall or None)``.  An
      epoch change whose time is missing or not newer than the last one seen
      (a torn read, or an advance that recorded no time) starts the quiet
      window at the current time instead.
    - ``on_trigger(trigger, mode)`` receives every act-rule trigger, outside
      the core lock.
    - ``on_snapshot(digest, previous_poll_wall)``, when given, receives every
      accepted poll's digest (the judge's hold-until-answered), outside the
      core lock, before the triggers of the same poll.
    - ``on_watch(runner, watch, stuck)``, when given, receives each active
      demotion watch whose leaf has stuck connections after a poll (§6),
      outside the core lock, after the triggers.
    - ``emit(level, record)`` logs one record.
    - ``origin_rules_factory()``, when given, builds the origin rules on this
      thread before the first line is parsed, keeping that work (its first
      call imports the IDNA codec) off the caller's path.
    - ``cpu_clock()``, when given, returns this thread's CPU seconds; it is
      only called on this thread.  By default the thread builds
      ``thread_cpu_clock()`` itself, unless ``perf`` was substituted: a CPU
      share needs both readings on one timeline, so the budget then stays
      off (as it does when there is no reliable clock or the clock fails).

    CPU budget (``core.constants.cpu_budget_pct``, percent of one core; 0
    turns it off): every loop pass hands ``PostDialCpuThrottle`` one CPU
    reading and the CPU spent inside polls so far (read around each poll),
    and the stretch it returns goes to ``core.set_poll_scale``.
    ``observer_throttled`` (an episode starts) and
    ``observer_budget_unreachable`` are logged at most once per summary
    interval each; the summary carries the shares, episodes, changes and
    scales.

    The core has no locks of its own; ``with_core`` and the demotion-watch
    methods serialise other threads (the judge) with this one.
    """

    def __init__(
        self,
        *,
        tap: PostDialTap,
        core: PostDialObserverCore,
        controller_factory: Callable[[], Any],
        is_current: Callable[[], bool],
        mode: Callable[[], tuple[str, frozenset[str]]],
        route_snapshot: Callable[[], Any],
        route_context_for: Callable[[Any], PostDialRouteContext | None],
        network_epoch: Callable[[], tuple[int, float | None]],
        on_trigger: Callable[[Trigger, str], None],
        emit: Emit,
        origin_rules: OriginRules | None = None,
        origin_rules_factory: Callable[[], OriginRules] | None = None,
        is_exonerated: Callable[[str, str], bool] | None = None,
        on_snapshot: Callable[[SnapshotDigest, float | None], None] | None = None,
        on_watch: Callable[
            ["PostDialObserverRunner", DemotionWatch, StuckConnections], None
        ] | None = None,
        summary_interval_s: float = SUMMARY_INTERVAL_S,
        wall: Callable[[], float] = time.time,
        perf: Callable[[], float] = time.perf_counter,
        monotonic: Callable[[], float] = time.monotonic,
        cpu_clock: Callable[[], float] | None = None,
        name: str = OBSERVER_THREAD_NAME,
        on_diagnostics_retired: Callable[[], None] | None = None,
        diagnostics_emit: Emit | None = None,
    ) -> None:
        self.tap = tap
        self.core = core
        self._origin_rules = origin_rules or OriginRules()
        self._origin_rules_factory = origin_rules_factory
        self._controller_factory = controller_factory
        self._is_current = is_current
        self._mode = mode
        self._route_snapshot = route_snapshot
        self._route_context_for = route_context_for
        self._network_epoch = network_epoch
        self._on_trigger = on_trigger
        self._on_diagnostics_retired = on_diagnostics_retired
        self._diagnostics_emit = diagnostics_emit
        self._diagnostics_policy_revision = 0
        self._diagnostics_policy_value = None
        self._emit = emit
        self._is_exonerated = is_exonerated
        self._on_snapshot = on_snapshot
        self._on_watch = on_watch
        # Set by ``wake`` before its Event.set, consumed before every sleep
        # that follows an ``arm_wake``: a wake-up between the schedule
        # computation and the Event clear is never lost.
        self._wake_requested = False
        self._last_accepted_poll_wall: float | None = None
        self._summary_interval_s = max(1.0, float(summary_interval_s))
        self._wall = wall
        self._perf = perf
        self._monotonic = monotonic
        self._name = str(name)
        self._core_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stopping = threading.Event()
        # Held until ``stop``: a fast-window sleep is one timed acquire of it
        # (the same OS wait ``Event.wait`` ends in, without its Condition).
        self._sleep_lock = threading.Lock()
        self._sleep_lock.acquire()
        self._sleep_released = False
        self._stop_reason = ""
        self._thread: threading.Thread | None = None
        self._client: Any = None
        self._fetch_raw: Callable[..., Any] | None = None
        self._last_snapshot: Any = None
        self._context: PostDialRouteContext | None = None
        self._epoch: int | None = None
        self._epoch_changed_at: float | None = None
        self._act_rules: frozenset[str] | None = None
        # The last ``mode()`` reading and its perf time (``_loop``).
        self._mode_value: tuple[str, frozenset[str]] | None = None
        self._mode_read_at = 0.0
        self._active = False
        self._last_summary_perf: float | None = None
        # CPU budget: the clock is built on this thread (``_note_cpu``).
        self._cpu_clock_given = cpu_clock
        self._measure_cpu = cpu_clock is not None or perf is time.perf_counter
        self._cpu_clock: Callable[[], float] | None = None
        self._throttle = PostDialCpuThrottle.from_constants(
            core.constants,
            record_interval_s=self._summary_interval_s,
        )
        self._scale = 1.0
        # CPU seconds spent inside ``_poll`` (the share a stretch reduces).
        self._poll_cpu = 0.0
        self.metrics: dict[str, int] = {
            "controller_calls": 0,
            "poll_errors": 0,
            "poll_oversize": 0,
            "lines_drained": 0,
            "lines_rejected": 0,
            "dormant_waits": 0,
            "raw_polls": 0,
            "throttle_episodes": 0,
            "throttled_polls": 0,
        }
        self._calls_since_summary = 0

    def __repr__(self) -> str:
        return f"PostDialObserverRunner(alive={self.is_alive()}, active={self._active})"

    # -- lifecycle ----------------------------------------------------------

    @property
    def thread(self) -> threading.Thread | None:
        return self._thread

    @property
    def stop_reason(self) -> str:
        return self._stop_reason

    def start(self) -> threading.Thread:
        with self._state_lock:
            if self._thread is not None:
                return self._thread
            thread = threading.Thread(target=self._run, name=self._name, daemon=True)
            self._thread = thread
        thread.start()
        return thread

    def is_alive(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive() and not self._stopping.is_set())

    def stop(self, reason: str = "stopped") -> None:
        """Signal the thread; safe from any thread and idempotent."""

        with self._state_lock:
            if not self._stop_reason:
                self._stop_reason = str(reason or "stopped")
        self._stopping.set()
        self._release_sleep()
        self.tap.close()
        client = self._client
        if client is not None:
            self._close_client(client)

    def _release_sleep(self) -> None:
        with self._state_lock:
            if self._sleep_released:
                return
            self._sleep_released = True
        self._sleep_lock.release()

    def _sleep(self, delay: float) -> None:
        """Sleep ``delay`` seconds or until ``stop``."""

        if self._sleep_lock.acquire(True, delay):
            self._sleep_lock.release()

    def join(self, timeout: float) -> bool:
        thread = self._thread
        if thread is None or thread is threading.current_thread():
            return not (thread is not None and thread.is_alive())
        thread.join(max(0.0, float(timeout)))
        return not thread.is_alive()

    def with_core(self, callback: Callable[[PostDialObserverCore], Any]) -> Any:
        """Run ``callback(core)`` serialised with the observer thread."""

        with self._core_lock:
            return callback(self.core)

    # -- thread body --------------------------------------------------------

    def _run(self) -> None:
        reason = ""
        try:
            initial_value = None
            try:
                initial_value = self._mode()
                mode = initial_value[0]
            except Exception:
                mode = "unknown"
            self._safe_emit("info", {"ev": "observer_started", "mode": mode})
            if self._diagnostics_emit is not None:
                try:
                    initial_rules = tuple(sorted(initial_value[1]))
                except Exception:
                    initial_rules = "unknown"
                self._safe_diagnostics_emit("info", {"ev": "observer_started_context",
                    "mode": mode, "act_rules": initial_rules})
            if not self._load_origin_rules():
                reason = "origin_rules_unavailable"
            else:
                reason = self._loop()
        except Exception as exc:  # pragma: no cover - defensive
            reason = f"error_{type(exc).__name__}"
        finally:
            self._stopping.set()
            self.tap.close()
            client, self._client = self._client, None
            if client is not None:
                self._close_client(client)
            with self._state_lock:
                if not self._stop_reason:
                    self._stop_reason = reason or "stopped"
                final_reason = self._stop_reason
            if self._calls_since_summary:
                self._emit_summary(self._wall())
            self._safe_emit(
                "info",
                {
                    "ev": "observer_stopped",
                    "reason": final_reason,
                    "controller_calls": self.metrics["controller_calls"],
                },
            )
            # This source will submit no more triggers. A diagnostic scope
            # separately waits for already registered producers and its drain.
            if self._on_diagnostics_retired is not None:
                try:
                    self._on_diagnostics_retired()
                except Exception:
                    pass

    def _loop(self) -> str:
        tap = self.tap
        core = self.core
        fast_period = float(core.constants.poll_fast_s)
        self._mode_value = None
        while not self._stopping.is_set():
            if not self._is_current():
                return "lease_revoked"
            reused = self._mode_value is not None and self._perf() - self._mode_read_at < MODE_REFRESH_S
            if not reused and not self._read_mode():
                return "disabled"
            now_wall = self._wall()
            now_perf = self._perf()
            self._note_cpu(now_wall, now_perf)
            self._refresh_context(now_wall)
            drained = tap.drain()
            with self._core_lock:
                if drained:
                    self._ingest_lines(drained, now_wall, now_perf)
                due = core.next_poll_time(now_perf, now_wall)
                records = core.drain_records()
            self._emit_records(records)
            if due is None:
                if self._active:
                    self._active = False
                    self._safe_emit("debug", activity_record(False, now_wall=now_wall))
                    self._maybe_summary(now_wall, now_perf)
                if reused and not self._read_mode():
                    return "disabled"  # never dormant on a reused policy reading
                if tap.arm_wake():
                    continue
                if self._consume_wake_request():
                    # A demotion watch started after this schedule was taken.
                    tap.disarm()
                    continue
                # DORMANT: no timeout, no controller call until a line arrives.
                self.metrics["dormant_waits"] += 1
                tap.event.wait()
                tap.disarm()
                self._mode_value = None  # a config reload while dormant applies now
                continue
            if not self._active:
                self._active = True
                if self._last_summary_perf is None:
                    self._last_summary_perf = now_perf
                self._safe_emit("debug", activity_record(True, now_wall=now_wall))
            delay = due - now_perf
            if delay > 0.0:
                if delay > fast_period and reused and not self._read_mode():
                    return "disabled"
                if delay > fast_period * self._scale:
                    # A new open during a slow (1 Hz) sleep must be drained
                    # promptly for its fast window.
                    if not tap.arm_wake() and not self._consume_wake_request():
                        tap.event.wait(delay)
                    tap.disarm()
                else:
                    # At most one fast period (stretched by the CPU budget):
                    # a new open could bring the next poll forward by less
                    # than that, so its line waits instead of costing a wake-up.
                    self._sleep(delay)
                continue
            # A poll (and the triggers it hands on) always follows a reading
            # taken in the same pass.
            if reused and not self._read_mode():
                return "disabled"
            clock = self._cpu_clock
            if clock is None:
                self._poll(self._mode_value[0])
            else:
                before = self._read_cpu(clock)
                self._poll(self._mode_value[0])
                if before is not None:
                    after = self._read_cpu(clock)
                    if after is not None and after > before:
                        self._poll_cpu += after - before
            self._maybe_summary(self._wall(), self._perf())
        return self._stop_reason or "stopped"

    def _read_mode(self) -> bool:
        """Read ``mode()`` now and apply its act rules; False when it is off."""

        value = self._mode()
        self._mode_value = value
        self._mode_read_at = self._perf()
        mode, act_rules = value
        if mode not in {"shadow", "act"}:
            return False
        if act_rules != self._act_rules:
            self._act_rules = frozenset(act_rules)
            with self._core_lock:
                self.core.set_act_rules(self._act_rules)
        diagnostic_value = (mode, self._act_rules)
        if self._diagnostics_emit is not None and diagnostic_value != self._diagnostics_policy_value:
            self._diagnostics_policy_value = diagnostic_value
            self._diagnostics_policy_revision += 1
            self._safe_diagnostics_emit("info", {"ev": "observer_policy", "mode": mode,
                "actual_rules": tuple(sorted(self._act_rules or ())),
                "policy_revision": self._diagnostics_policy_revision})
        return True

    def _note_cpu(self, now_wall: float, now_perf: float) -> None:
        """One CPU reading for the budget; applies the stretch it yields."""

        clock = self._cpu_clock
        if clock is None:
            if not self._measure_cpu:
                return
            # Built on this thread, after the first pass's lease check.
            clock = self._cpu_clock = self._cpu_clock_given or thread_cpu_clock()
            if clock is None:  # no reliable clock here: no budget
                self._cpu_failed()
                return
        cpu = self._read_cpu(clock)
        if cpu is None:
            return
        throttle = self._throttle
        record = throttle.note(now_perf, cpu, self._poll_cpu)
        self.metrics["throttle_episodes"] = throttle.episodes_total
        self._apply_scale()
        if record is not None:
            self._safe_emit("info", {"ev": record.pop("ev"), "t": round(float(now_wall), 3), **record})

    def _read_cpu(self, clock: Callable[[], float]) -> float | None:
        try:
            return float(clock())
        except Exception:
            self._cpu_failed()
            return None

    def _cpu_failed(self) -> None:
        """No (more) CPU readings: the budget is off and the stretch undone."""

        self._cpu_clock = None
        self._measure_cpu = False
        self._throttle.disable()
        self._apply_scale()

    def _apply_scale(self) -> None:
        scale = self._throttle.scale
        if scale != self._scale:
            with self._core_lock:
                self.core.set_poll_scale(scale)
            self._scale = scale

    def _load_origin_rules(self) -> bool:
        factory = self._origin_rules_factory
        if factory is None:
            return True
        try:
            rules = factory()
        except Exception:
            return False
        if not isinstance(rules, OriginRules):
            return False
        self._origin_rules = rules
        with self._core_lock:
            self.core.set_origin_rules(rules)
        return True

    def _ingest_lines(self, drained: list[tuple[float, str]], now_wall: float, now_perf: float) -> None:
        core = self.core
        tokenizer = core.tokenizer
        rules = self._origin_rules
        now_monotonic = self._monotonic()
        self.metrics["lines_drained"] += len(drained)
        for received_at, raw_line in drained:
            record = parse_open_line(
                raw_line,
                received_at,
                tokenizer=tokenizer,
                origin_rules=rules,
            )
            if record is None:
                self.metrics["lines_rejected"] += 1
                continue
            core.ingest_open(
                record,
                now_wall=now_wall,
                now_perf=now_perf,
                now_monotonic=now_monotonic,
            )

    def _refresh_context(self, now_wall: float) -> None:
        try:
            snapshot = self._route_snapshot()
        except Exception:
            snapshot = None
        if snapshot is not None and snapshot is not self._last_snapshot:
            self._last_snapshot = snapshot
            try:
                context = self._route_context_for(snapshot)
            except Exception:
                context = None
            if context is not None and context != self._context:
                self._context = context
                with self._core_lock:
                    self.core.set_route_context(
                        tier3_members=context.tier3_members,
                        gateway_groups=context.gateway_groups,
                    )
        try:
            epoch, changed_at = self._network_epoch()
        except Exception:
            return
        epoch = int(epoch)
        changed = float(changed_at) if changed_at is not None else None
        if self._epoch is None:
            self._epoch = epoch
            self._epoch_changed_at = changed
            if changed is not None:
                with self._core_lock:
                    self.core.note_network_epoch(changed)
            return
        if epoch != self._epoch:
            self._epoch = epoch
            last = self._epoch_changed_at
            if changed is None or (last is not None and changed <= last):
                # The time of this change is not visible (yet): its quiet
                # window cannot have started later than now.
                at = float(now_wall)
            else:
                at = changed
            self._epoch_changed_at = max(at, last) if last is not None else at
            with self._core_lock:
                self.core.note_network_epoch(at)
            self._safe_emit("debug", {"ev": "epoch", "t": round(at, 3)})

    def _poll(self, mode: str) -> None:
        core = self.core
        client = self._client
        if client is None:
            try:
                client = self._controller_factory()
            except Exception:
                client = None
            if client is None:
                self.stop("controller_unavailable")
                return
            self._client = client
            fetch_raw = getattr(client, "connections_raw", None)
            self._fetch_raw = fetch_raw if callable(fetch_raw) else None
            if self._stopping.is_set():
                self._close_client(client)
                return
        t_req_wall = self._wall()
        t_req_perf = self._perf()
        body: Any = None
        failed = False
        try:
            if self._fetch_raw is not None:
                body = self._fetch_raw(timeout_seconds=core.constants.ctrl_timeout_s)
            else:
                body = client.connections(timeout_seconds=core.constants.ctrl_timeout_s)
        except Exception:
            failed = True
        cost = max(0.0, self._perf() - t_req_perf)
        self.metrics["controller_calls"] += 1
        if self._scale > 1.0:
            self.metrics["throttled_polls"] += 1
        self._calls_since_summary += 1
        if self._stopping.is_set():
            return
        digest: SnapshotDigest | None = None
        watch_items: list[tuple[DemotionWatch, StuckConnections]] = []
        with self._core_lock:
            if not failed and isinstance(body, (bytes, bytearray)):
                self.metrics["raw_polls"] += 1
                try:
                    digest = core.ingest_raw(body, t_req_wall, t_req_perf, cost_s=cost)
                except RawBodyError:
                    # Not a JSON object (raised before the core changed): the
                    # failure the client reports for ``connections()``.
                    failed = True
            if failed:
                self.metrics["poll_errors"] += 1
                core.note_poll_failed(t_req_wall, t_req_perf, cost_s=cost, reason="controller_error")
                evaluation = None
            elif body is None:
                self.metrics["poll_oversize"] += 1
                core.note_poll_failed(t_req_wall, t_req_perf, cost_s=cost, reason="oversize")
                evaluation = None
            else:
                if digest is None:
                    digest = core.ingest_snapshot(body, t_req_wall, t_req_perf, cost_s=cost)
                evaluation = core.evaluate_triggers(
                    self._wall(),
                    is_exonerated=self._is_exonerated,
                )
                if self._on_watch is not None and digest.accepted:
                    for watch in core.active_demotion_watches(t_req_perf):
                        stuck = core.stuck_connection_ids(watch.leaf, watch.t_demote_wall, t_req_wall)
                        if stuck.ids:
                            watch_items.append((watch, stuck))
                if digest.accepted:
                    # The back-off reacts to the whole poll (request, decode
                    # and this ingest), not only the request it was given.
                    core.note_poll_cost(max(0.0, self._perf() - t_req_perf))
            records = core.drain_records()
        self._emit_records(records)
        if digest is not None and digest.accepted:
            previous = self._last_accepted_poll_wall
            self._last_accepted_poll_wall = t_req_wall
            if self._on_snapshot is not None:
                try:
                    self._on_snapshot(digest, previous)
                except Exception:
                    self._safe_emit("warning", {"ev": "snapshot_hook_failed"})
        if evaluation is None:
            return
        tokenizer = core.tokenizer
        for trigger in evaluation.triggers:
            try:
                self._on_trigger(trigger, mode)
            except Exception:
                self._safe_emit("warning", {"ev": "trigger_hook_failed", "rule": trigger.kind})
        for shadow in evaluation.shadow:
            record = dict(trigger_record(shadow, tokenizer, mode=mode))
            record["mode"] = mode
            self._safe_emit("info", record)
        for watch, stuck in watch_items:
            try:
                self._on_watch(self, watch, stuck)  # type: ignore[misc]
            except Exception:
                self._safe_emit("warning", {"ev": "watch_hook_failed"})

    # -- demotion watch (§6), serialised with the observer thread -------------

    def wake(self) -> None:
        """Re-evaluate the schedule now (a demotion watch changes it)."""

        self._wake_requested = True
        self.tap.event.set()

    def _consume_wake_request(self) -> bool:
        if not self._wake_requested:
            return False
        self._wake_requested = False
        return True

    def start_demotion_watch(self, leaf: str, *, t_demote_wall: float) -> DemotionWatch | None:
        """Watch L for POST_DEMOTION_WATCH_S: 1 Hz polls while a user
        connection on L exists, each handed to ``on_watch`` with its stuck ids."""

        if self._stopping.is_set():
            return None
        with self._core_lock:
            watch = self.core.start_demotion_watch(
                leaf,
                t_demote_wall=float(t_demote_wall),
                now_perf=self._perf(),
            )
        self.wake()
        return watch

    def end_demotion_watch(self, leaf: str) -> None:
        with self._core_lock:
            self.core.end_demotion_watch(leaf)

    def stuck_now(
        self,
        leaf: str,
        *,
        t_demote_wall: float,
        max_age_s: float,
    ) -> StuckConnections | None:
        """§6 rules 1, 2, 4, 5 on the latest snapshot, or None when it is older
        than ``max_age_s`` (the next poll then decides)."""

        with self._core_lock:
            age = self.core.latest_snapshot_age(self._perf())
            if age is None or age > float(max_age_s):
                return None
            return self.core.stuck_connection_ids(leaf, float(t_demote_wall), self._wall())

    def mark_closed(self, conn_ids: Any) -> None:
        with self._core_lock:
            self.core.mark_closed(tuple(conn_ids))

    # -- output -------------------------------------------------------------

    def _emit_records(self, records: list[dict[str, Any]]) -> None:
        for record in records:
            kind = record.get("ev")
            if kind == "open":
                continue
            self._safe_emit("debug" if kind in _DEBUG_RECORDS else "info", record)

    def _maybe_summary(self, now_wall: float, now_perf: float) -> None:
        last = self._last_summary_perf
        if last is None or now_perf - last < self._summary_interval_s:
            return
        self._emit_summary(now_wall)
        self._last_summary_perf = now_perf

    def _emit_summary(self, now_wall: float) -> None:
        with self._core_lock:
            record = self.core.summary(now_wall)  # counters since the last summary
            raw_counts = dict(getattr(self.core, "raw_counts", None) or {})
        # The CPU share, throttle episodes and scales since the last summary.
        record.update(self._throttle.summary())
        # The observer's own counters are totals since it started.
        record.update({f"total_{key}": value for key, value in self.metrics.items()})
        record.update({f"total_raw_{key}": value for key, value in raw_counts.items()})
        record["total_tap_offered"] = int(self.tap.offered)
        record["total_tap_overflow"] = int(self.tap.overflow)
        self._calls_since_summary = 0
        self._safe_emit("info", record)

    def _safe_emit(self, level: str, record: Mapping[str, Any]) -> None:
        try:
            self._emit(level, record)
        except Exception:
            pass

    def _safe_diagnostics_emit(self, level: str, record: Mapping[str, Any]) -> None:
        if self._diagnostics_emit is not None:
            try:
                self._diagnostics_emit(level, record)
            except Exception:
                pass

    @staticmethod
    def _close_client(client: Any) -> None:
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
