"""T-P2-11 bridge side of B-26(1)(2): deferred apply re-enters the mutation domain.

``settings.apply`` (P2-10) saves inside the mutation domain and, for a
startup-deferred Core plan, calls ``SettingsApplyCoordinator.request``. That
returns the PendingApply fact immediately: the save job never waits for ready.

A single waiter thread (alive only while an intent is pending) samples the
public lifecycle phase. Once the phase leaves collecting/core_standby it
re-enters the mutation domain -- the same whole-transaction lock as every
external mutation command -- takes the session's network guard, and calls
``runtime.apply_settings_intent`` once. The runtime rechecks generation, config
revision, stop intent and Core generation under the existing Core transition
lock; it decides applied/failed/cancelled/superseded or "still waiting".

Stop keeps its own priority: the control worker publishes the runtime stop
intent without this module, and the runtime recheck turns the intent into
``cancelled``. The waiter never holds the mutation lock while waiting for ready.
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from copy import deepcopy
from typing import Any, Callable, ContextManager, Protocol


TERMINAL = frozenset({"applied", "failed", "superseded", "cancelled"})
WAITING_PHASES = frozenset({"collecting", "core_standby"})
MAX_BACKOFF = 5.0
LOGGER = logging.getLogger("tesseract.bridge.settings_apply")


class Abandoned(Exception):
    """The coordinator closed while the transaction waited for its guard."""


def _expected_retry(exc: BaseException) -> bool:
    """RT-1 startup recovery still running: an expected, silent retry."""
    try:
        from runtime_network_recovery import RecoveryStillRunning
    except Exception:  # pragma: no cover - import failure is itself unexpected
        return False
    return isinstance(exc, RecoveryStillRunning)


class SettingsApplyRuntime(Protocol):
    """Runtime facts/entries used by this module (all public)."""

    def request_settings_apply(self, plan: Any, config_rev: int,
                               user_commit_id: str) -> dict | None: ...

    def pending(self) -> dict | None: ...

    def apply_intent(self, intent_id: str, *, config_rev: int | None) -> dict | None:
        """One apply attempt. An intent that owes a latency recheck comes back
        ``applying`` (parked) after its Core half: see ``probe_latency`` /
        ``finish_latency`` (T-P2-RT2)."""
        ...

    def probe_latency(self) -> Any:
        """Latency recheck probe, run with no bridge lock held (T-P2-RT2)."""
        ...

    def finish_latency(self, intent_id: str, probe: Any) -> dict | None:
        """Reload half of a parked intent, under the network guard; finishes it."""
        ...

    def startup_phase(self) -> str: ...

    def config_revision(self) -> int | None:
        """Bridge config write revision (P2-10 owner); None when unknown."""
        ...


class MutationDomain(Protocol):
    def run(self, work: Callable[[], Any], *, abandoned: Callable[[], bool]) -> tuple[bool, Any]:
        """Run ``work`` serialized with every mutation-domain transaction.

        Returns (False, None) when ``abandoned()`` became true while waiting.
        """
        ...


class RuntimeSettingsApplyAdapter:
    """Adapter over DesktopRuntime public methods only."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def request_settings_apply(self, plan, config_rev, user_commit_id):
        return self.runtime.request_settings_apply(plan, config_rev, user_commit_id)

    def pending(self):
        return self.runtime.bridge_config_pending()

    def apply_intent(self, intent_id, *, config_rev):
        # T-P2-RT2 (决定_第二波裁定 P2-10 #4): the latency probe never runs
        # under the network lock; the runtime parks the intent instead.
        return self.runtime.apply_settings_intent(intent_id, config_rev=config_rev,
                                                  split_latency=True)

    def probe_latency(self):
        return self.runtime.probe_latency_policy()

    def finish_latency(self, intent_id, probe):
        return self.runtime.finish_settings_intent_latency(intent_id, probe)

    def startup_phase(self):
        return str(self.runtime.lifecycle_status()["runtime"]["startup_phase"])

    def config_revision(self):
        # Needed runtime getter (P2-10 config write owner). Until it exists the
        # revision is unknown: the runtime ledger still supersedes on every
        # newer request, it only cannot see saves that bypass request().
        try:
            getter = self.runtime.bridge_config_revision
        except AttributeError:
            return None
        value = getter() if callable(getter) else None
        return value if type(value) is int and value >= 0 else None


class ExecutorMutationDomain:
    """Whole-transaction serialization shared with JobExecutor's change worker.

    Resolves the session's current executor on each run (epoch advance keeps
    one mutation lock) and enters the domain through the executor's public
    ``run_internal_mutation`` (T-P2-31a); executor internals are never read.
    """

    def __init__(self, executor: Callable[[], Any],
                 guard: Callable[[Callable[[], bool]], ContextManager],
                 *, poll: float = .05) -> None:
        self._executor = executor
        self._guard = guard
        self._poll = poll

    def run(self, work, *, abandoned):
        executor = self._executor()
        try:
            return executor.run_internal_mutation(
                lambda: self._guarded(work, abandoned), abandoned=abandoned, poll=self._poll)
        except Abandoned:
            return False, None

    def _guarded(self, work, abandoned):
        # The guard rechecks ``abandoned`` after its own (network) lock: a
        # close() that happened while waiting there runs no work and no
        # guard side effects.
        with self._guard(abandoned):
            return work()


class SettingsApplyCoordinator:
    """Owns the waiter; no lock of its own is held across runtime calls."""

    def __init__(self, port: SettingsApplyRuntime, mutation: MutationDomain, *,
                 interval: float = .5, max_backoff: float = MAX_BACKOFF,
                 ready_check: Callable[[], None] | None = None) -> None:
        if interval <= 0:
            raise ValueError("interval must be positive")
        self.port = port
        self.mutation = mutation
        self.interval = interval
        self.max_backoff = max(float(interval), float(max_backoff))
        self._lock = threading.Lock()
        # Serializes read-latest-then-deliver, so listeners see pending facts
        # in ledger order (never an older state after a newer one).
        self._deliver_lock = threading.RLock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._listeners: list[Callable[[dict | None], None]] = []
        self._last_published: Any = object()
        self._logged_failures: set[str] = set()
        # T-P2-RT2: intents this coordinator parked ``applying`` (Core half
        # done, latency reload owed). Kept until their finish ran in the
        # mutation domain, so a refused finish (RT-1 gate busy) is retried
        # instead of leaving the intent ``applying`` forever.
        self._parked: dict[str, None] = {}
        # Cheap precheck before a latency probe (the RT-1 recovery gate):
        # raises while the finish would be refused anyway.
        self._ready_check = ready_check or (lambda: None)

    # -- surface for P2-10 and the integration ticket -----------------------
    def request(self, plan: Any, config_rev: int, user_commit_id: str) -> dict | None:
        """Record deferred Core work; returns PendingApply now, never waits.

        Pass the save's ApplyPlan whenever an intent may be live: only its
        ``deferred`` Core effect is new work; a plan without deferred Core work
        (including a live restart/reload already executed) just advances the
        live intent to the newer revision.
        """
        dto = self.port.request_settings_apply(plan, config_rev, user_commit_id)
        self._publish_current()
        if dto is not None and dto.get("state") not in TERMINAL:
            self.wake()
        return deepcopy(dto)

    def pending(self) -> dict | None:
        """config.changed / gateway.current ``pending`` fact (PendingApply?)."""
        return deepcopy(self.port.pending())

    def add_listener(self, listener: Callable[[dict | None], None]) -> None:
        """Called whenever the pending fact changes, in ledger order.

        Delivery is serialized by the coordinator's own delivery lock; the
        coordinator holds no runtime lock, but a delivery from request() runs
        on the save thread inside its mutation transaction. A listener must
        only hand the fact to the publisher and must not wait on bridge locks.
        """
        if not callable(listener):
            raise ValueError("listener must be callable")
        with self._lock:
            self._listeners.append(listener)

    def wake(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._wake.set()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="bridge-settings-apply",
                                                daemon=True)
                self._thread.start()

    def close(self, timeout: float = 1.0) -> bool:
        with self._lock:
            self._closed = True
            thread = self._thread
            self._wake.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0.0, timeout))
            return not thread.is_alive()
        return True

    @property
    def closed(self) -> bool:
        return self._closed

    # -- waiter ---------------------------------------------------------------
    def _publish_current(self) -> dict | None:
        """Deliver the latest pending fact, serialized and deduplicated.

        The fact is re-read under the delivery lock instead of publishing a
        caller's (possibly older) result, so concurrent save/waiter deliveries
        cannot reorder or flash back to an old intent's state.
        """
        with self._deliver_lock:
            dto = self.port.pending()
            key = None if dto is None else (dto.get("intent_id"), dto.get("state"),
                                            dto.get("config_rev"), dto.get("effect"))
            with self._lock:
                if key == self._last_published:
                    return dto
                self._last_published = key
                listeners = list(self._listeners)
            for listener in listeners:
                try:
                    listener(deepcopy(dto))
                except Exception:
                    self._diagnose("listener", None)
            return dto

    def _diagnose(self, where: str, exc: BaseException | None) -> None:
        """One raw diagnostic per (place, exception type); never the pending fact."""
        key = where + ":" + (type(exc).__name__ if exc is not None else "-")
        with self._lock:
            if key in self._logged_failures:
                return
            self._logged_failures.add(key)
        try:
            from log_event_reports import log_raw_diagnostic
            log_raw_diagnostic(LOGGER, "warning", "Deferred settings apply %s failed (%s)",
                               where, key.split(":", 1)[1], exc_info=exc is not None)
        except Exception:
            pass

    def _apply(self, intent_id: str) -> dict | None:
        def work():
            # Read the revision inside the domain: no save can interleave.
            return self.port.apply_intent(intent_id, config_rev=self.port.config_revision())
        ran, result = self.mutation.run(work, abandoned=lambda: self._closed)
        if not ran:
            return None
        if (isinstance(result, dict) and result.get("state") == "applying"
                and result.get("intent_id") == intent_id):
            with self._lock:
                self._parked[intent_id] = None
            return self._finish_latency(intent_id)
        return result

    def _finish_latency(self, intent_id: str) -> dict | None:
        """Parked latency recheck (决定_第二波裁定 P2-10 #4, T-P2-RT2).

        The probe runs here with no mutation/network lock held; only the Core
        reload re-enters the domain and the network guard. A stop in between
        makes the runtime abandon the reload (the intent ends cancelled). If
        the coordinator closes meanwhile, the intent is finished without any
        Core work (probe None) so it never stays ``applying``.

        The ready check (RT-1 gate) runs before the probe; it and the guard
        may refuse (raise). The intent then stays in ``_parked`` and the
        waiter retries this whole step (a fresh probe) with backoff, like a
        refused pending intent -- it never stays ``applying`` for good.
        """
        self._ready_check()
        try:
            probe = self.port.probe_latency()
        except Exception as exc:
            self._diagnose("latency_probe", exc)
            probe = None
        ran, result = self.mutation.run(lambda: self.port.finish_latency(intent_id, probe),
                                        abandoned=lambda: self._closed)
        if ran:
            self._unpark(intent_id)
            return result
        return self._finish_unguarded(intent_id)

    def _unpark(self, intent_id: str) -> None:
        with self._lock:
            self._parked.pop(intent_id, None)

    def _parked_intents(self) -> list[str]:
        with self._lock:
            return list(self._parked)

    def _finish_unguarded(self, intent_id: str) -> dict | None:
        """Close path: finish a parked intent without any Core work."""
        self._unpark(intent_id)
        try:
            return self.port.finish_latency(intent_id, None)
        except Exception as exc:
            self._diagnose("latency_finish", exc)
            return None

    def _run(self) -> None:
        delay = self.interval
        while True:
            with self._lock:
                closed = self._closed
                if closed and not self._parked:
                    self._thread = None
                    return
                woken = self._wake.is_set()
                self._wake.clear()
            if closed:
                # Never leave a parked intent ``applying``: finish it with no
                # Core work (probe None), then exit on the next pass.
                for intent_id in self._parked_intents():
                    self._finish_unguarded(intent_id)
                self._publish_current()
                continue
            if woken:
                delay = self.interval  # a new save: retry promptly
            try:
                parked = self._parked_intents()
                if parked:
                    # A finish refused earlier (RT-1 gate busy): retry it
                    # before anything newer; raises again while still busy.
                    for intent_id in parked:
                        self._finish_latency(intent_id)
                    self._publish_current()
                    delay = self.interval
                    continue
                dto = self._publish_current()
                if dto is not None and dto.get("state") == "pending":
                    if self.port.startup_phase() in WAITING_PHASES:
                        delay = self.interval  # cheap phase poll, no domain entry
                    else:
                        result = self._apply(dto["intent_id"])
                        self._publish_current()
                        if result is not None and result.get("state") in TERMINAL:
                            delay = self.interval
                            continue  # a successor may already be pending
                        # Still waiting (Core down, recovery window): back off,
                        # each attempt takes the domain and network locks.
                        delay = min(delay * 2, self.max_backoff)
                elif dto is None or dto.get("state") in TERMINAL:
                    with self._lock:
                        if not self._wake.is_set() or self._closed:
                            self._thread = None
                            return
                    continue
            except Exception as exc:
                # RT-1 gate busy is an expected retry; anything else is logged
                # once per type. Either way the intent is kept and retried.
                if not _expected_retry(exc):
                    self._diagnose("waiter", exc)
                delay = min(delay * 2, self.max_backoff)
            self._wake.wait(delay)


@contextmanager
def _session_guard(session, abandoned: Callable[[], bool] = lambda: False):
    """Same discipline as BridgeSession.restart: network lock, idle RT-1 gate.

    Like restart, service.state is recorded in the outer finally (after the
    network lock), also when the RT-1 gate refuses. A transaction abandoned
    while waiting for the network lock touches nothing.
    """
    entered = False
    try:
        with session.network_lock:
            if abandoned():
                raise Abandoned()
            entered = True
            session.report.gate.require_idle()
            session.refresh_secrets()
            try:
                yield
            finally:
                session.refresh_secrets()
    finally:
        if entered:
            session.record_service()


def _session_gate_idle(session) -> None:
    """report.gate.require_idle() without the network lock (cheap precheck)."""
    gate = getattr(getattr(session, "report", None), "gate", None)
    if gate is not None:
        gate.require_idle()


def register(session, *, interval: float = .5,
             max_backoff: float = MAX_BACKOFF) -> SettingsApplyCoordinator:
    """Wire into a BridgeSession after connect(); registers no command/query.

    settings.apply itself is P2-10's command; it calls
    ``session.settings_apply.request(plan, rev, user_commit_id)``. The
    ``pending`` fact reaches config.snapshot through the runtime getter
    ``bridge_config_pending`` already resolved by config_query_owners().
    Integration publishes config.changed/gateway.current via add_listener().
    """
    coordinator = SettingsApplyCoordinator(
        RuntimeSettingsApplyAdapter(session.runtime),
        ExecutorMutationDomain(lambda: session.executor,
                               lambda abandoned: _session_guard(session, abandoned)),
        interval=interval, max_backoff=max_backoff,
        ready_check=lambda: _session_gate_idle(session))
    session.settings_apply = coordinator
    return coordinator
