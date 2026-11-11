"""Ordinary-thread traffic producer; history is copied once outside I/O ticks."""

from __future__ import annotations

from collections import deque
import math
from threading import Event, Lock, Thread
import time
from typing import Any, Callable

from bridge_logs import ms, timestamp
from bridge_runtime_access import RuntimeAccess, traffic_monitor


def _rate(value: Any) -> float:
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError("invalid traffic rate")
    return value


def history_data(snapshot: dict, sampled_at_ms: int, *, gap: bool = False) -> dict:
    """Project the monitor's bounded means, omitting empty buckets, never zero filling."""
    def project(name, maximum):
        result = []
        for row in snapshot.get(name, [])[-maximum:]:
            if not isinstance(row, dict) or type(row.get("n")) is not int or row["n"] <= 0:
                continue
            t = timestamp(row.get("t_ms"), -1)
            if t < 0:
                continue
            try:
                item = {"t_ms": t, "up_bps": _rate(row["up_bps"]),
                        "down_bps": _rate(row["down_bps"])}
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if name == "h24":
                item["sample_count"] = row["n"]
            result.append(item)
        return sorted(result, key=lambda row: row["t_ms"])
    dto = {"sampled_at_ms": sampled_at_ms, "source": "bucketed",
           "h24": project("h24", 288), "live": project("live", 300)}
    if gap:
        # An optional snapshot-data continuity marker, not a P1 gap-kind frame.
        dto["gap"] = True
    return dto


class TrafficCollector:
    """Read the current runtime monitor on the ordinary 1 Hz worker.

    No controller connection, history scan or byte integration occurs on ticks.
    History is seeded once per monitor identity after the first subscription.
    """
    def __init__(self, state: Any, runtime: Any, *,
                 period: float = 1.0, clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], int] = ms) -> None:
        if period < 1 or not math.isfinite(period):
            raise ValueError("traffic sampling exceeds 1 Hz")
        self.state = state
        self.runtime = runtime if isinstance(runtime, RuntimeAccess) else RuntimeAccess(runtime)
        self.period, self.clock, self.wall_clock = period, clock, wall_clock
        self._history_lock = Lock()
        self._history_requested, self._stop = Event(), Event()
        self._history_attempted = False
        self._history_cache = None
        self._monitor = None
        self._monitor_seen = False
        self._history_gap = False
        self._last_sample = None
        self._running = False
        self._waiting_since = self.wall_clock()
        self._last_tick = None
        self._next_sample = 0.0
        self._thread = None
        self.worker_thread_cpu_ns = 0
        self.history_copies = 0
        self.source_costs = {}
        self.diagnostics = deque(maxlen=64)

    def request_history(self) -> None:
        """A wakeup-free first-subscription flag; safe to call from I/O control."""
        self._history_requested.set()

    def _observe(self, name: str, action: Callable) -> Any:
        cpu, wall = time.thread_time_ns(), time.perf_counter_ns()
        costs = self.source_costs.setdefault(name, {"cpu_ns": 0, "elapsed_ns": 0,
                                                   "successes": 0, "failures": 0})
        try:
            result = action()
            costs["successes"] += 1
            return result
        except Exception:
            if not self._stop.is_set():
                costs["failures"] += 1
                self.diagnostics.append({"source": name, "error": "sample_unavailable"})
            return None
        finally:
            costs["cpu_ns"] += time.thread_time_ns() - cpu
            costs["elapsed_ns"] += time.perf_counter_ns() - wall

    def _tick(self) -> None:
        monitor = traffic_monitor(self.runtime)
        if not self._monitor_seen or monitor is not self._monitor:
            # Keep the old history seed until its explicit replacement is ready.
            self._history_gap = self._history_gap or self._history_attempted
            self._history_attempted = False
            self._monitor_seen = True
            self._monitor = monitor
            self._last_sample = None
            self._last_tick = None
            self._waiting_since = self.wall_clock()
        # Monitor.running is the stream worker's liveness, not Core lifecycle.
        # The compact Core getter is local and does no controller/history I/O.
        running = bool(self.runtime.core_fact()["running"])
        if running and not self._running:
            self._waiting_since = self.wall_clock()
        self._running = running
        if monitor is None:
            return
        raw = monitor.tick_snapshot()  # Exactly one atomic monitor read per tick.
        latest = raw["latest"]
        sample = dict(latest) if latest is not None else None
        totals = raw["session"]
        session = {"up_bytes": totals["upload_bytes"], "down_bytes": totals["download_bytes"]}
        if running:
            if sample is None or sample == self._last_sample:
                return  # No fabricated fresh timestamp, even if the stream fails.
            t = timestamp(sample["timestamp"], -1)
            if t < 0:
                raise ValueError("invalid traffic sample time")
            dto = {"t_ms": t, "up_bps": _rate(sample["up"]),
                   "down_bps": _rate(sample["down"]), "session": session,
                   "running": True, "error_code": "io" if raw["error"] else None}
        else:
            dto = {"t_ms": self.wall_clock(), "up_bps": 0, "down_bps": 0,
                   "session": session, "running": False,
                   "error_code": None}
            semantic = {k: v for k, v in dto.items() if k != "t_ms"}
            prior = {k: v for k, v in (self._last_tick or {}).items() if k != "t_ms"}
            if semantic == prior:
                return
        if self._stop.is_set():
            return
        self.state.record_state("traffic.tick", dto)
        self._last_tick = dto
        self._last_sample = sample if running else None

    def stale(self, now_ms: int | None = None) -> bool:
        """P1 silence timeout; only an observed Core stop exempts sample aging."""
        if self._stop.is_set() or not self._running:
            return False
        observed = (self._last_tick["t_ms"] if self._last_tick and self._last_tick["running"]
                    else self._waiting_since)
        return (self.wall_clock() if now_ms is None else now_ms) - observed > 3000

    def _history(self) -> None:
        # Serialized once per monitor, including failed attempts. Resolve again:
        # reload may replace the monitor between the tick and this seed copy.
        with self._history_lock:
            if self._stop.is_set() or self._history_attempted or self._monitor is None:
                return
            monitor = self._monitor
            self._history_attempted = True
            self.history_copies += 1
            snapshot = monitor.history_buckets()
            if self._stop.is_set() or traffic_monitor(self.runtime) is not monitor:
                return  # The next tick resolves and seeds the new identity.
            self._history_cache = history_data(snapshot, self.wall_clock(), gap=self._history_gap)
            if self._history_gap:
                self.state.record_state("traffic.history", self._history_cache, history_reseed=True)
            else:
                self.state.record_state("traffic.history", self._history_cache)
            self._history_gap = False

    def poll(self) -> None:
        """Worker entry point; never registered as a Publisher.tick callback."""
        if self._stop.is_set():
            return
        started = time.thread_time_ns()
        try:
            now = self.clock()
            if now >= self._next_sample:
                self._next_sample = now + self.period
                self._observe("traffic.tick", self._tick)
            if not self._history_attempted and (self._history_requested.is_set() or self.state.history_sent):
                self._observe("traffic.history", self._history)
        finally:
            self.worker_thread_cpu_ns += time.thread_time_ns() - started

    def run(self) -> None:
        started = time.thread_time_ns()
        prior = self.worker_thread_cpu_ns
        try:
            while not self._stop.is_set():
                self.poll()
                self.worker_thread_cpu_ns = prior + time.thread_time_ns() - started
                self._stop.wait(0.2)
        finally:
            self.worker_thread_cpu_ns = prior + time.thread_time_ns() - started

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self.run, name="bridge-traffic-sampler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2) -> bool:
        deadline = time.monotonic() + max(0, timeout)
        self._stop.set()
        if self._thread:
            self._thread.join(max(0, deadline - time.monotonic()))
        return self._thread is None or not self._thread.is_alive()

    def metrics(self) -> dict:
        return {"worker_thread_cpu_ns": self.worker_thread_cpu_ns,
                "stream_thread_cpu_ns": 0, "stale": self.stale(),
                "history_copies": self.history_copies,
                "source_costs": {key: dict(value) for key, value in self.source_costs.items()},
                "diagnostics": list(self.diagnostics)}


def collection_metrics(publisher: Any, sampler: Any, logs: Any, traffic: Any) -> dict:
    """Extend P1-12's metrics without changing its accepted publisher module.

    Thread CPU and callback CPU describe costs, not exact GIL ownership time.
    Callback CPU can overlap a measured worker lifetime and must not be summed
    blindly; acceptance sums disjoint thread lifetimes / elapsed time. In the
    log stream path, producer callbacks are already included in stream thread CPU.
    """
    return {**publisher.metrics(), "state_worker_thread_cpu_ns": sampler.worker_thread_cpu_ns,
            "state_source_costs": {key: dict(value) for key, value in sampler.source_costs.items()},
            "logs": logs.metrics(), "traffic": traffic.metrics()}
