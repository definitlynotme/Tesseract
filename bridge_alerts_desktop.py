"""Desktop-notice owner for one sidecar epoch (T-P2-22, ASK-02 M-06; 提醒 redesign 10-30).

``alert_policy.IncidentTracker`` decides which runtime facts become a notice
(plan .build/提醒模块重设计_20261030.md §2–§3, §10). This owner applies the
user's switches (``alerts.notify``), turns policy notices, update offers and
the settings page's preview into ``alerts.desktop`` edges, and owns their
delivery ids and receipts. P3 (shell/page) owns the actual surface. No Qt,
runtime construction, threads or transport I/O.

Threading model:
- ``observe(facts)`` runs on the 1 Hz SamplingWorker (``Wiring.sample``): it
  runs the pure policy under the owner lock, queues its notices and logs
  them. Every incident reaches the run log (category alert); the switches
  only decide whether it is shown.
- ``poll()`` runs on ONE existing bridge tick thread: it issues the queued
  notices and update offers. It never touches disk; the config read runs on
  an ordinary worker through ``submit_normal``.
- ``alerts.desktopDelivered`` (T-P2-31a) runs in the control domain directly
  on the admitting ingress thread: it records the receipt in memory and
  answers through a deferred completion (``Pending``) once the receipt's
  write (an update notice's version) ran on an ordinary worker.
- ``self._lock`` guards only in-memory tables and is never held while calling
  the update owner, the publisher or the logger.
"""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
import logging
from threading import Event, Lock
from time import monotonic
from typing import Any, Callable, Hashable, Protocol
from uuid import uuid4

import alert_policy
import bridge_protocol as protocol
from alert_config import effective_alerts_config
from bridge_jobs import JobFailure, Pending
from log_event_reports import log_event

_LOGGER = logging.getLogger(__name__)

DESKTOP_TOPIC = "alerts.desktop"
OWNER = "alert_owner"

PENDING_CAPACITY = 64
PENDING_TTL_SECONDS = 300.0
TOMBSTONE_CAPACITY = 256
# Policy notices waiting for the next tick (the sampler runs at 1 Hz).
NOTICE_QUEUE_CAPACITY = 64
REFRESH_CHECK_SECONDS = 1.0
# Without bridge_config_revision the effective config is re-read on a worker
# at this cadence, so a config.local.yaml-only save is still picked up.
CONFIG_REREAD_SECONDS = 8.0
CONFIG_RETRY_MIN_SECONDS = 1.0
CONFIG_RETRY_MAX_SECONDS = 60.0
# Upper bound of a desktopDelivered answer waiting for its receipt write.
RECEIPT_PERSIST_TIMEOUT_SECONDS = 30.0

# Prefix contract for the shell: a forced notice (the settings page's
# preview) bypasses foreground routing and is always shown as a card.
TEST_DELIVERY_PREFIX = "desktop-test-"
ALERT_DELIVERY_PREFIX = "desktop-"

# Run-log event per notice (log_event_reports, category alert, source ALERT).
_LOG_KEYS = {"error": "alert.notice.error", "warn": "alert.notice.warn", "info": "alert.notice.info"}
_LOG_ALERT_TYPES = {"error": "CRITICAL", "warn": "WARNING", "info": "INFO"}


# --------------------------------------------------------------------- ports


class DesktopAlertRuntimePort(Protocol):
    """Runtime facts this owner needs. Blocking methods say so explicitly."""

    def effective_alerts_config(self) -> dict[str, Any]:
        """BLOCKING (reads config.local.yaml): ordinary worker only."""

    def alert_language(self) -> str: ...

    def notice_logger(self) -> logging.Logger | None:
        """The runtime's alert logger (its records reach the run log)."""

    def config_token(self) -> Hashable:
        """O(1) in-memory token that changes when alert config may change."""

    def config_revision_known(self) -> bool:
        """True when config_token carries a real config revision (getter wired).

        False makes the owner also re-read the config periodically on a worker.
        """


class Publication(Protocol):
    """``bridge_state.PublicationState`` publication API."""

    def record_state(self, topic: str, dto: dict[str, Any]) -> None: ...

    def record_edge(self, topic: str, dto: dict[str, Any]) -> int: ...


class UpdateNoticeSource(Protocol):
    """``bridge_update.UpdateOwner`` notice-candidate interface (P2-26)."""

    current_version: str

    def take_notice_candidate(self) -> dict[str, str] | None: ...

    def record_notice_delivered(self, version: str, *, delivery_id: str) -> None: ...

    def notice_not_delivered(self, delivery_id: str) -> None: ...


class _ConfigToken:
    """Identity token holding the config objects themselves.

    A bare ``id()`` can be reused once ``reload_config`` drops the old object,
    which would hide a replacement; holding the reference rules that out.
    """

    __slots__ = ("revision", "objects")

    def __init__(self, revision: Any, objects: tuple[Any, ...]) -> None:
        self.revision = revision
        self.objects = objects

    def __eq__(self, other: object) -> bool:
        return (isinstance(other, _ConfigToken) and self.revision == other.revision
                and len(self.objects) == len(other.objects)
                and all(a is b for a, b in zip(self.objects, other.objects)))

    def __hash__(self) -> int:
        return hash((self.revision, tuple(id(item) for item in self.objects)))


class RuntimeDesktopAlertPort:
    """Adapter over existing public DesktopRuntime members only.

    Note: ``runtime.config`` forces ``alerts.desktop.enabled=false`` for the
    runtime's own AlertManager (``_disable_external_desktop_notifications``);
    the user's switches live in config.local.yaml, so the effective config is
    the runtime config merged with the local file, exactly like Qt.
    """

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    # Optional public runtime members are probed with literal attribute access
    # (the bridge private-access gate forbids dynamic getattr on the runtime).
    def _config_revision_getter(self):
        try:
            getter = self.runtime.bridge_config_revision
        except AttributeError:
            return None
        return getter if callable(getter) else None

    def effective_alerts_config(self) -> dict[str, Any]:
        # Same merge as Qt's _effective_alerts_config(read_local_config()).
        return effective_alerts_config(self.runtime.config, self.runtime.read_local_config())

    def alert_language(self) -> str:
        return str(getattr(self.runtime.alerts, "language", "") or "")

    def notice_logger(self) -> logging.Logger | None:
        try:
            return self.runtime.alerts.logger
        except AttributeError:
            return None

    def config_token(self) -> Hashable:
        getter = self._config_revision_getter()
        revision = getter() if getter is not None else None
        try:
            config = self.runtime.config
        except AttributeError:
            config = None
        try:
            alerts_config = self.runtime.alerts_config
        except AttributeError:
            alerts_config = None
        return _ConfigToken(revision, (config, alerts_config))

    def config_revision_known(self) -> bool:
        # Needed runtime getter (report): bridge_config_revision().
        return self._config_revision_getter() is not None


# --------------------------------------------------------------------- owner


@dataclass
class _Pending:
    delivery_id: str
    kind: str  # alert | test | update
    created: float
    version: str | None = None
    done: Event = field(default_factory=Event)
    result: dict[str, Any] | None = None
    # T-P2-31a: deferred-completion watchers, called once (outside the owner
    # lock) with the receipt result, or None when the entry expired.
    watchers: list[Callable[[dict[str, Any] | None], Any]] = field(default_factory=list)


@dataclass
class _Tomb:
    state: str  # received | expired
    entry: _Pending | None = None
    recorded: bool = False
    late_seen: bool = False  # expired: a late receipt already answered
    # T-P2-31b (决定_第三波裁定 T-P2-31a 第 1 轮 #1): the first receipt's write is
    # still running; duplicates attach here and are answered after it.
    persisting: bool = False
    waiters: list[Callable[[bool], Any]] = field(default_factory=list)


# T-P2-31b: a receipt write that outlasts RECEIPT_PERSIST_TIMEOUT_SECONDS is
# answered ok:false timeout/receipt_persist_pending (never the late/expired
# shape); the shell only logs it and the write still completes once.
def _persist_pending_failure() -> JobFailure:
    return JobFailure("timeout", error={
        "code": "timeout", "message_key": "errors.alerts.receipt_persist_pending",
        "params": {}, "reason_code": "receipt_persist_pending"})


def _raise_persist_pending() -> Any:
    raise _persist_pending_failure()


class DesktopAlertOwner:
    """One owner per sidecar epoch. Not reusable across epochs."""

    def __init__(
        self, port: DesktopAlertRuntimePort, publication: Publication, *,
        submit_normal: Callable[[Callable[[], Any]], bool],
        shell_subscribed: Callable[[], bool],
        update_owner: UpdateNoticeSource | None = None,
        tracker: alert_policy.IncidentTracker | None = None,
        clock: Callable[[], float] = monotonic,
        pending_capacity: int = PENDING_CAPACITY,
        pending_ttl: float = PENDING_TTL_SECONDS,
        id_factory: Callable[[], str] = lambda: uuid4().hex,
    ) -> None:
        self.port = port
        self.publication = publication
        self.submit_normal = submit_normal
        self.shell_subscribed = shell_subscribed
        self.update_owner = update_owner
        self._tracker = tracker if tracker is not None else alert_policy.IncidentTracker()
        self._clock = clock
        self._pending_capacity = max(1, int(pending_capacity))
        self._pending_ttl = float(pending_ttl)
        self._new_id = id_factory
        self._lock = Lock()
        self._attached = False
        # Policy notices waiting for the tick, and the incidents whose open
        # notice was issued (their update / resolved / closed follow it).
        self._notices: list[alert_policy.Notice] = []
        self._issued: set[str] = set()
        # The switches (refreshed on ordinary workers); None until first read.
        self._notify: dict[str, Any] | None = None
        self._config_token_seen: Any = None
        self._config_read_at: float | None = None
        self._config_retry_at = 0.0
        self._config_backoff = 0.0
        self._refresh_inflight = False
        self._next_refresh_check = 0.0
        # Receipts.
        self._pending: OrderedDict[str, _Pending] = OrderedDict()
        self._tombs: OrderedDict[str, _Tomb] = OrderedDict()
        self._test_inflight: str | None = None
        self._closed = False
        # Receipt writes refused by a full normal queue; re-submitted by poll().
        self._persist_backlog: list[Callable[[], Any]] = []
        # Diagnostic counters only (unsynchronized increments are acceptable).
        self.stats = {"suppressed": 0, "dropped": 0, "issued": 0, "expired": 0,
                      "publish_failed": 0, "duplicates": 0, "late": 0,
                      "persist_failed": 0, "config_failed": 0, "log_failed": 0}

    # ------------------------------------------------------------ policy

    def close(self) -> None:
        """Full exit (T-P2-31b): expire every notice still waiting for its
        receipt (no write, update notices are offered again at the next
        start) and stop issuing. Waiting alerts.test watchers get None."""
        with self._lock:
            self._closed = True
            entries = list(self._pending.values())
            self._pending.clear()
            for entry in entries:
                self._tomb(entry.delivery_id, _Tomb("expired", entry))
        self._release_expired(entries)

    def observe(self, facts: alert_policy.Facts | None) -> int:
        """SamplingWorker entry (1 Hz): run the policy, queue and log its notices."""
        if self._closed or facts is None:
            return 0
        now = self._clock()
        with self._lock:
            notices = self._tracker.observe(facts, now)
            room = max(0, NOTICE_QUEUE_CAPACITY - len(self._notices))
            self._notices.extend(notices[:room])
            self.stats["dropped"] += len(notices) - min(room, len(notices))
        self._log(notices)
        return len(notices)

    def _log(self, notices: list[alert_policy.Notice]) -> None:
        records = [notice for notice in notices if notice.state != "closed"]
        if not records:
            return
        try:
            logger = self.port.notice_logger()
            language = self.port.alert_language()
        except Exception:
            logger = None
        if logger is None:
            return
        for notice in records:
            title, detail = alert_policy.fallback_text(notice, language)
            resolved = notice.state == "resolved"
            try:
                log_event(
                    logger, logging.INFO,
                    "alert.notice.resolved" if resolved else _LOG_KEYS.get(notice.severity, "alert.notice.info"),
                    f"[{notice.kind}] {title}" + (f" - {detail}" if detail else ""),
                    params={"title": title, "message": detail, "event_group": notice.group,
                            "scope": notice.incident,
                            "alert_type": "RECOVERY" if resolved else _LOG_ALERT_TYPES.get(notice.severity, "INFO")},
                )
            except Exception as error:
                self.stats["log_failed"] += 1
                _LOGGER.warning("Desktop notice log failed: %s", type(error).__name__)

    def poll(self) -> int:
        """Tick entry; returns the number of alerts.desktop edges recorded."""
        if self._closed:
            return 0
        now = self._clock()
        self._drain_persist_backlog()
        self._maybe_refresh(now)
        expired = self._expire(now)
        self._release_expired(expired)
        if not self._attached:
            if not self.shell_subscribed():
                # No shell surface exists: the notices stay history (run log).
                with self._lock:
                    self.stats["dropped"] += len(self._notices)
                    self._notices.clear()
                return 0
            with self._lock:
                self._attached = True
        notify = self._notify
        issued = 0
        if notify is not None:
            with self._lock:
                notices, self._notices = self._notices, []
            for notice in notices:
                if self._wanted(notice, notify):
                    issued += self._issue(self._notice_dto(notice, notify), kind="alert")
                else:
                    self.stats["suppressed"] += 1
        issued += self._issue_update_notices(notify)
        return issued

    def _wanted(self, notice: alert_policy.Notice, notify: dict[str, Any]) -> bool:
        """An open notice follows the switches; its follow-ups follow the open."""
        if notice.state == "open":
            if not (notify["enabled"] and notice.group in notify["events"]):
                return False
            if notice.kind != "unstable":  # fire-and-forget: never updated
                self._issued.add(notice.incident)
            return True
        if notice.incident not in self._issued:
            return False
        if notice.state in ("resolved", "closed"):
            self._issued.discard(notice.incident)
        return True

    def _notice_dto(self, notice: alert_policy.Notice, notify: dict[str, Any] | None,
                    *, version: str | None = None) -> dict[str, Any]:
        title, detail = alert_policy.fallback_text(notice, self.port.alert_language())
        return {
            "event_ids": [f"notice.{notice.kind}"], "severity": notice.severity,
            "title": title or "Tesseract", "detail": detail, "members": [],
            "target": notice.target, "version": version,
            "notice": notice.wire(sound=bool(notify and notify["sound"])),
        }

    def _issue_update_notices(self, notify: dict[str, Any] | None) -> int:
        if self.update_owner is None or notify is None:
            return 0
        if not (notify["enabled"] and alert_policy.GROUP_UPDATE in notify["events"]):
            return 0  # the offers stay with the update owner
        issued = 0
        while True:
            candidate = self.update_owner.take_notice_candidate()
            if candidate is None:
                return issued
            version = str(candidate["version"])
            current = str(getattr(self.update_owner, "current_version", "") or "")
            notice = alert_policy.update_notice(version, current, f"update-{version}")
            issued += self._issue(self._notice_dto(notice, notify, version=version), kind="update",
                                  version=version, delivery_id=str(candidate["delivery_id"]))

    # ------------------------------------------------------------ issuing

    def _issue(self, notice: dict[str, Any], *, kind: str, version: str | None = None,
               delivery_id: str | None = None) -> int:
        if delivery_id is None:
            prefix = TEST_DELIVERY_PREFIX if kind == "test" else ALERT_DELIVERY_PREFIX
            delivery_id = prefix + self._new_id()
        # T-P2-31b (决定_第一波裁定 T-P2-22 #1): the explicit force flag marks a
        # forced notice (always a card, whatever the foreground window); the
        # shell keeps reading the delivery-id prefix for one version.
        dto = {"delivery_id": delivery_id, **notice, "force": kind == "test"}
        entry = _Pending(delivery_id, kind, self._clock(), version)
        evicted: list[_Pending] = []
        with self._lock:
            # Insert before publishing so an immediate receipt finds the row.
            self._pending[delivery_id] = entry
            while len(self._pending) > self._pending_capacity:
                _, old = self._pending.popitem(last=False)
                self._tomb(old.delivery_id, _Tomb("expired", old))
                evicted.append(old)
        self._release_expired(evicted)
        try:
            if protocol.validate_topic_payload(DESKTOP_TOPIC, dto):
                raise ValueError("invalid alerts.desktop DTO")
            self.publication.record_edge(DESKTOP_TOPIC, dto)
        except Exception:
            self.stats["publish_failed"] += 1
            with self._lock:
                self._pending.pop(delivery_id, None)
                self._tomb(delivery_id, _Tomb("expired", entry))
            self._release_expired([entry])
            return 0
        self.stats["issued"] += 1
        return 1

    def _tomb(self, delivery_id: str, tomb: _Tomb) -> None:
        self._tombs[delivery_id] = tomb
        self._tombs.move_to_end(delivery_id)
        while len(self._tombs) > TOMBSTONE_CAPACITY:
            self._tombs.popitem(last=False)

    def _expire(self, now: float) -> list[_Pending]:
        expired: list[_Pending] = []
        with self._lock:
            while self._pending:
                entry = next(iter(self._pending.values()))
                if now - entry.created < self._pending_ttl:
                    break
                self._pending.popitem(last=False)
                self._tomb(entry.delivery_id, _Tomb("expired", entry))
                expired.append(entry)
        return expired

    def _release_expired(self, entries: list[_Pending]) -> None:
        """No receipt: nothing is persisted; an update notice is re-offered next epoch."""
        for entry in entries:
            self.stats["expired"] += 1
            with self._lock:
                if self._test_inflight == entry.delivery_id:
                    self._test_inflight = None
            self._resolve(entry)
            if entry.kind == "update" and self.update_owner is not None:
                try:
                    self.update_owner.notice_not_delivered(entry.delivery_id)
                except Exception:
                    pass

    # ------------------------------------------------------------ receipts

    def _resolve(self, entry: _Pending) -> None:
        """Mark ``entry`` done and run its watchers once, outside the lock."""
        with self._lock:
            watchers, entry.watchers = entry.watchers, []
            entry.done.set()
        result = deepcopy(entry.result) if entry.result is not None else None
        for watcher in watchers:
            try:
                watcher(deepcopy(result))
            except Exception:
                pass

    def desktop_delivered(self, args: dict[str, Any], context: Any = None) -> dict[str, Any]:
        """Synchronous receipt entry (memory registration and write here).

        ``delivered`` + ``surface:none`` is refused at admission by the
        PROTOCOL invariant (bridge_protocol), so it never reaches this handler.
        A duplicate that arrives while the first receipt's write runs waits
        for it (bounded) and answers with its outcome. The executor uses
        ``desktop_delivered_handler`` instead.
        """
        step = self._register_receipt(args)
        if isinstance(step, dict):
            return step
        if isinstance(step, _Tomb):
            done = Event()
            outcome: list[bool] = []
            if self._watch_write(step, lambda recorded: (outcome.append(recorded), done.set())):
                if not done.wait(RECEIPT_PERSIST_TIMEOUT_SECONDS):
                    raise _persist_pending_failure()
            else:
                outcome.append(step.recorded)
            return {"recorded": bool(outcome and outcome[0]), "duplicate": True}
        return self._persist_receipt(*step)

    def _register_receipt(self, args: dict[str, Any]):
        """In-memory part: a final answer dict (late/duplicate), or the
        (entry, tomb, outcome, surface) still to persist. Never touches disk."""
        delivery_id = args["delivery_id"]
        outcome = args["outcome"]
        surface = args["surface"]
        first_late = False
        recorded_now = False
        with self._lock:
            tomb = self._tombs.get(delivery_id)
            entry = None if tomb is not None else self._pending.pop(delivery_id, None)
            if entry is not None:
                tomb = _Tomb("received", entry, persisting=True)
                self._tomb(delivery_id, tomb)
                if self._test_inflight == delivery_id:
                    self._test_inflight = None
            elif tomb is not None:
                recorded_now = tomb.recorded
                if tomb.persisting:
                    # T-P2-31b: attach to the first receipt's write.
                    self.stats["duplicates"] += 1
                    return tomb
                if tomb.state == "expired":
                    first_late = not tomb.late_seen
                    tomb.late_seen = True
        if entry is None:
            if tomb is None:
                raise JobFailure("conflict")  # never issued in this epoch
            if first_late:
                self.stats["late"] += 1
                return {"recorded": False, "duplicate": False}
            # Duplicate (of a receipt or of a late receipt): never write again.
            self.stats["duplicates"] += 1
            return {"recorded": recorded_now, "duplicate": True}
        return entry, tomb, outcome, surface

    def _persist_receipt(self, entry: _Pending, tomb: _Tomb, outcome: str,
                         surface: str) -> dict[str, Any]:
        """Disk part of a first receipt (ordinary worker or direct caller)."""
        recorded = self._persist(entry, outcome, surface)
        with self._lock:
            tomb.recorded = recorded
            tomb.persisting = False
            waiters, tomb.waiters = tomb.waiters, []
        for waiter in waiters:
            try:
                waiter(recorded)
            except Exception:
                pass
        entry.result = {"outcome": outcome, "surface": surface, "recorded": recorded}
        self._resolve(entry)
        return {"recorded": recorded, "duplicate": False}

    def _watch_write(self, tomb: _Tomb, callback: Callable[[bool], Any]) -> bool:
        """Attach ``callback(recorded)`` to a running first-receipt write;
        False (callback not attached) when that write already finished."""
        with self._lock:
            if not tomb.persisting:
                return False
            tomb.waiters.append(callback)
            return True

    def _submit_persist(self, work: Callable[[], Any]) -> None:
        try:
            submitted = bool(self.submit_normal(work))
        except Exception:
            submitted = False
        if not submitted:
            with self._lock:
                self._persist_backlog.append(work)

    def _drain_persist_backlog(self) -> None:
        """Tick side: re-submit refused receipt writes (no disk work here)."""
        while True:
            with self._lock:
                if not self._persist_backlog:
                    return
                work = self._persist_backlog.pop(0)
            try:
                submitted = bool(self.submit_normal(work))
            except Exception:
                submitted = False
            if not submitted:
                with self._lock:
                    self._persist_backlog.insert(0, work)
                return

    def _persist(self, entry: _Pending, outcome: str, surface: str) -> bool:
        """Runs on the receipt's ordinary worker, holding no owner lock.

        Only an update notice has something to keep (B-30b: its version is
        remembered once a notice for it was really shown); policy notices and
        previews leave their trace in the run log instead.
        """
        if entry.kind != "update" or self.update_owner is None:
            return False
        try:
            if outcome == "delivered":
                self.update_owner.record_notice_delivered(entry.version or "",
                                                          delivery_id=entry.delivery_id)
                return True
            # failed/dropped never persist; next start offers it again.
            self.update_owner.notice_not_delivered(entry.delivery_id)
        except Exception:
            self.stats["persist_failed"] += 1
        return False

    def desktop_delivered_handler(self, args: dict[str, Any], context: Any) -> Any:
        """``alerts.desktopDelivered`` executor handler (T-P2-31a).

        Control domain, run inline by the admitting ingress thread: records
        the receipt in memory (so a waiting alerts.test can never starve it)
        and hands the write to an ordinary worker; the one-shot answer is a
        deferred completion settled by that write, ``{recorded, duplicate}``
        as before. A duplicate that arrives during that write is attached to
        it and answered afterwards with the first receipt's ``recorded``
        (T-P2-31b). A refused submission is retried from ``poll()``; after
        RECEIPT_PERSIST_TIMEOUT_SECONDS the answer is ``ok:false``
        ``timeout``/``receipt_persist_pending`` (the write still happens once).
        """
        step = self._register_receipt(args)
        if isinstance(step, dict):
            return step
        if isinstance(step, _Tomb):
            # Duplicate during the first write: answered after that write,
            # {recorded, duplicate:true} with the first receipt's outcome.
            duplicate = Pending(timeout=RECEIPT_PERSIST_TIMEOUT_SECONDS,
                                on_timeout=_raise_persist_pending)
            if not self._watch_write(step, lambda recorded: duplicate.complete(
                    {"recorded": bool(recorded), "duplicate": True})):
                return {"recorded": step.recorded, "duplicate": True}
            return duplicate

        pending = Pending(timeout=RECEIPT_PERSIST_TIMEOUT_SECONDS,
                          on_timeout=_raise_persist_pending)

        def write() -> None:
            try:
                result = self._persist_receipt(*step)
            except Exception:
                self.stats["persist_failed"] += 1
                result = {"recorded": False, "duplicate": False}
            pending.complete(result)

        self._submit_persist(write)
        return pending

    # ------------------------------------------------------------ preview

    def force_desktop_test(self, draft: Any = None) -> str:
        """alerts.test entry: the settings page's preview, always shown as a card.

        Bypasses the switches. ``draft`` is accepted for compatibility and
        ignored. Returns the delivery_id; wait with wait_desktop_receipt() or
        watch_desktop_receipt(). At most one preview is in flight; the slot is
        released by the receipt, by expiry, or when a wait for it gives up.
        """
        notice = alert_policy.preview_notice(f"preview-{self._new_id()[:16]}")
        dto = self._notice_dto(notice, self._notify)
        delivery_id = TEST_DELIVERY_PREFIX + self._new_id()
        with self._lock:
            if self._test_inflight is not None:
                raise JobFailure("conflict")
            self._test_inflight = delivery_id
            attached = self._attached
        if not attached and not self.shell_subscribed():
            # No shell surface exists at all: resolve locally as dropped.
            entry = _Pending(delivery_id, "test", self._clock())
            with self._lock:
                self._tomb(delivery_id, _Tomb("received", entry))
                self._test_inflight = None
            entry.result = {"outcome": "dropped", "surface": "none", "recorded": False}
            self._resolve(entry)
            return delivery_id
        if not self._issue(dto, kind="test", delivery_id=delivery_id):
            with self._lock:
                if self._test_inflight == delivery_id:
                    self._test_inflight = None
            raise JobFailure("io")
        return delivery_id

    def _receipt_entry_locked(self, delivery_id: str) -> _Pending | None:
        entry = self._pending.get(delivery_id)
        tomb = self._tombs.get(delivery_id)
        if entry is None and tomb is not None:
            entry = tomb.entry
        return entry

    def watch_desktop_receipt(self, delivery_id: str,
                              callback: Callable[[dict[str, Any] | None], Any]) -> bool:
        """Deferred-completion form of wait_desktop_receipt (T-P2-31a).

        ``callback(result)`` runs once, never under the owner lock: with the
        receipt result (as wait_desktop_receipt returns it) or None when the
        notice expired unanswered; immediately when already resolved. Returns
        False for an unknown id (callback never called). Nothing waits; the
        caller gives up with ``release_desktop_test``.
        """
        with self._lock:
            entry = self._receipt_entry_locked(delivery_id)
            if entry is None:
                return False
            resolved = entry.done.is_set()
            if not resolved:
                entry.watchers.append(callback)
        if resolved:
            try:
                callback(deepcopy(entry.result) if entry.result is not None else None)
            except Exception:
                pass
        return True

    def release_desktop_test(self, delivery_id: str) -> None:
        """The caller gave up waiting (timeout / job ended): free the preview
        slot. The pending row stays, so a late receipt is still answered once
        (same as a wait_desktop_receipt timeout)."""
        with self._lock:
            if self._test_inflight == delivery_id:
                self._test_inflight = None
            entry = self._receipt_entry_locked(delivery_id)
            if entry is not None:
                entry.watchers.clear()

    def wait_desktop_receipt(self, delivery_id: str, timeout: float) -> dict[str, Any] | None:
        """Block (ordinary worker) for the real receipt; None on timeout/expiry.

        A timeout releases the preview slot (the caller has given up), but the
        pending row stays: a late receipt is still answered once and later
        duplicates follow the normal idempotency rules.
        """
        with self._lock:
            entry = self._pending.get(delivery_id)
            tomb = self._tombs.get(delivery_id)
            if entry is None and tomb is not None:
                entry = tomb.entry
        if entry is None or not entry.done.wait(max(0.0, timeout)):
            with self._lock:
                if self._test_inflight == delivery_id:
                    self._test_inflight = None
            return None
        return deepcopy(entry.result)

    # ------------------------------------------------------------ config

    def _maybe_refresh(self, now: float) -> None:
        """Tick side: O(1) in-memory signals only; the read goes to a worker."""
        if now < self._next_refresh_check:
            return
        self._next_refresh_check = now + REFRESH_CHECK_SECONDS
        try:
            token = self.port.config_token()
            revision_known = bool(self.port.config_revision_known())
        except Exception:
            token, revision_known = object(), False
        with self._lock:
            due = now >= self._config_retry_at and (
                self._notify is None or token != self._config_token_seen
                or (not revision_known and (self._config_read_at is None
                                            or now - self._config_read_at >= CONFIG_REREAD_SECONDS)))
            if self._refresh_inflight or not due:
                return
            self._refresh_inflight = True
        if not self.submit_normal(lambda: self._run_refresh(token)):
            with self._lock:
                self._refresh_inflight = False

    def _run_refresh(self, token: Any) -> None:
        """Ordinary worker: config read (with backoff)."""
        try:
            try:
                self.refresh_config(token)
            except Exception as error:
                self._config_read_failed(error)
        finally:
            with self._lock:
                self._refresh_inflight = False

    def _config_read_failed(self, error: BaseException) -> None:
        """Exponential backoff 1 s -> 60 s; the last known config is kept."""
        with self._lock:
            self._config_backoff = min(CONFIG_RETRY_MAX_SECONDS,
                                       max(CONFIG_RETRY_MIN_SECONDS, self._config_backoff * 2))
            delay = self._config_backoff
            self._config_retry_at = self._clock() + delay
        self.stats["config_failed"] += 1
        # Exception type only: YAML errors can quote config text (credentials).
        _LOGGER.warning("Desktop notice config read failed (%s); retrying in %.0f s",
                        type(error).__name__, delay)

    def refresh_config(self, token: Any = None) -> None:
        """BLOCKING: ordinary worker only. Failure keeps the last known config."""
        settings = alert_policy.notify_settings(self.port.effective_alerts_config())
        notify = {"enabled": settings["enabled"], "events": frozenset(settings["events"]),
                  "sound": settings["sound"]}
        read_at = self._clock()
        with self._lock:
            self._notify = notify
            self._config_token_seen = token
            self._config_read_at = read_at
            self._config_backoff = 0.0
            self._config_retry_at = 0.0


def register(session: Any, *, update_owner: UpdateNoticeSource | None = None,
             port: DesktopAlertRuntimePort | None = None) -> DesktopAlertOwner:
    """Create the epoch owner and register its receipt command on ``session``.

    Integration must additionally call ``owner.poll()`` from one existing
    bridge tick (100 ms - 1 s), ``owner.observe(facts)`` from the 1 Hz
    sampler, and pass the P2-26 UpdateOwner when wired. The command goes
    through ``session.register_command`` (the business-ticket entry,
    bridge_main) when the session has it, else ``executor.register``.
    """
    owner = DesktopAlertOwner(
        port or RuntimeDesktopAlertPort(session.runtime), session.state,
        submit_normal=session.executor.submit_normal,
        shell_subscribed=lambda: DESKTOP_TOPIC in session.subscribed_topics,
        update_owner=update_owner,
    )
    register_command = getattr(session, "register_command", None)
    if not callable(register_command):
        register_command = session.executor.register
    register_command("alerts.desktopDelivered", owner.desktop_delivered_handler)
    return owner
