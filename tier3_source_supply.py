from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable


@dataclass(frozen=True, slots=True)
class Tier3SourceRetryLease:
    lease_id: int
    root_key: str
    runtime_generation: int
    refresh_epoch: int
    attempt: int
    claimed_at: float
    refresh_existing: bool = False


@dataclass(frozen=True, slots=True)
class Tier3SourceSupplySnapshot:
    runtime_generation: int
    refresh_epoch: int
    configured_roots: int
    pending_roots: int
    retry_due_roots: int
    active_leases: int
    succeeded_roots: int
    terminal_roots: int
    dispatch_count: int
    deferred_count: int
    retry_completion_count: int
    revision: int
    ready_at_monotonic: float
    last_dispatch_at_monotonic: float
    last_wake_reason: str
    conditional_refresh_due_roots: int = 0

    def report_fields(self) -> dict[str, int]:
        fields = {"source_supply_supervisor_revision": self.revision}
        for canonical, alias, value in (
            ("dispatch_count", "dispatch_count", self.dispatch_count),
            ("active_leases", "active_lease_count", self.active_leases),
            ("retry_due_roots", "retry_due_root_count", self.retry_due_roots),
            ("conditional_refresh_due_roots", "conditional_refresh_due_root_count",
             self.conditional_refresh_due_roots),
            ("deferred_count", "deferred_count", self.deferred_count),
            ("retry_completion_count", "retry_completion_count", self.retry_completion_count),
        ):
            fields[f"source_supply_supervisor_{canonical}"] = int(value)
            fields[f"source_supply_{alias}"] = int(value)
        return fields


@dataclass(slots=True)
class _RootSupplyState:
    status: str = "pending"
    retry_due: bool = True
    refresh_due: bool = False
    attempt: int = 0
    next_due_at: float = 0.0
    active_lease_id: int = 0
    last_success_at: float = 0.0


class Tier3SourceSupplySupervisor:
    """Session-owned retry leases and backoff for configured Tier3 roots.

    The supervisor deliberately owns no network or pool authority. The manager
    claims a bounded lease immediately before collector dispatch and returns
    the typed result afterwards. A lease is never a refresh-wide tombstone.
    """

    def __init__(
        self,
        *,
        runtime_generation: int,
        refresh_epoch: int,
        root_keys: Iterable[str],
        control_interval_seconds: float = 0.25,
        retry_backoff_seconds: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0, 8.0),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.runtime_generation = max(0, int(runtime_generation))
        self.refresh_epoch = max(0, int(refresh_epoch))
        self.control_interval_seconds = max(
            0.01, float(control_interval_seconds)
        )
        normalized_backoff = tuple(
            max(self.control_interval_seconds, float(value))
            for value in retry_backoff_seconds
            if float(value) > 0.0
        )
        self.retry_backoff_seconds = normalized_backoff or (
            self.control_interval_seconds,
        )
        self._clock = clock
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._roots = {
            str(key): _RootSupplyState()
            for key in root_keys
            if str(key).strip()
        }
        self._next_lease_id = 1
        self._revision = 1
        self._dispatch_count = 0
        self._deferred_count = 0
        self._retry_completion_count = 0
        self._ready_at = self._clock()
        self._last_dispatch_at = 0.0
        self._last_wake_reason = "session_started"

    def observe_root(
        self,
        root_key: str,
        *,
        ok: bool,
        pending: bool,
        retryable: bool,
    ) -> None:
        key = str(root_key or "").strip()
        if not key:
            return
        now = self._clock()
        with self._lock:
            state = self._roots.setdefault(key, _RootSupplyState())
            if state.active_lease_id:
                return
            # Aggregate/progress reports can arrive out of order. A completed
            # dispatch lease is the terminal authority for this generation;
            # stale pending snapshots must not reopen a successful or terminal
            # root and create a retry storm. A new refresh gets a new
            # supervisor, so this does not suppress future-session retries.
            if state.status in {"ok", "terminal"}:
                return
            if ok and not retryable:
                state.status = "ok"
                state.retry_due = False
                state.refresh_due = False
                state.next_due_at = 0.0
                state.attempt = 0
                state.last_success_at = now
            elif pending or retryable:
                state.status = "pending" if pending else "retry_due"
                state.retry_due = True
                state.refresh_due = False
                if state.next_due_at <= 0.0:
                    state.next_due_at = now
            else:
                state.status = "terminal"
                state.retry_due = False
                state.refresh_due = False
                state.next_due_at = 0.0
            self._revision += 1
        self._wake.set()

    def claim_due(
        self,
        root_keys: Iterable[str],
        *,
        limit: int,
        force: bool = False,
    ) -> tuple[Tier3SourceRetryLease, ...]:
        now = self._clock()
        leases: list[Tier3SourceRetryLease] = []
        with self._lock:
            for raw_key in root_keys:
                if len(leases) >= max(0, int(limit)):
                    break
                key = str(raw_key or "").strip()
                state = self._roots.get(key)
                if (
                    state is None
                    or not (state.retry_due or state.refresh_due)
                    or state.active_lease_id
                    or (not force and state.next_due_at > now)
                ):
                    continue
                lease_id = self._next_lease_id
                self._next_lease_id += 1
                refresh_existing = bool(
                    state.refresh_due
                    or (state.status == "ok" and state.last_success_at > 0.0)
                )
                state.active_lease_id = lease_id
                lease = Tier3SourceRetryLease(
                    lease_id=lease_id,
                    root_key=key,
                    runtime_generation=self.runtime_generation,
                    refresh_epoch=self.refresh_epoch,
                    attempt=state.attempt + 1,
                    claimed_at=now,
                    refresh_existing=refresh_existing,
                )
                leases.append(lease)
            if leases:
                self._dispatch_count += len(leases)
                self._last_dispatch_at = now
                self._revision += 1
                # Clear while holding the same lock used by ``wake`` so a
                # route/config/admission signal arriving after this claim
                # cannot be erased by the claimant.
                self._wake.clear()
        return tuple(leases)

    def finish(
        self,
        lease: Tier3SourceRetryLease,
        *,
        ok: bool,
        retryable: bool,
        deferred: bool = False,
    ) -> bool:
        if (
            lease.runtime_generation != self.runtime_generation
            or lease.refresh_epoch != self.refresh_epoch
        ):
            return False
        now = self._clock()
        with self._lock:
            state = self._roots.get(lease.root_key)
            if state is None or state.active_lease_id != lease.lease_id:
                return False
            state.active_lease_id = 0
            if ok and not retryable:
                state.status = "ok"
                state.retry_due = False
                state.refresh_due = False
                state.attempt = 0
                state.next_due_at = 0.0
                state.last_success_at = now
            elif retryable or deferred:
                state.status = (
                    "ok"
                    if lease.refresh_existing
                    else ("pending" if deferred else "retry_due")
                )
                state.retry_due = not lease.refresh_existing
                state.refresh_due = bool(lease.refresh_existing)
                if deferred:
                    self._deferred_count += 1
                    delay = self.control_interval_seconds
                else:
                    state.attempt += 1
                    self._retry_completion_count += 1
                    delay = self.retry_backoff_seconds[
                        min(state.attempt - 1, len(self.retry_backoff_seconds) - 1)
                    ]
                state.next_due_at = now + delay
            else:
                if lease.refresh_existing:
                    state.status = "ok"
                    state.retry_due = False
                    state.refresh_due = False
                    state.next_due_at = 0.0
                else:
                    state.status = "terminal"
                    state.retry_due = False
                    state.refresh_due = False
                    state.next_due_at = 0.0
            self._revision += 1
        self._wake.set()
        return True

    def wake(self, reason: str, *, reset_due: bool = True) -> None:
        now = self._clock()
        with self._lock:
            self._last_wake_reason = str(reason or "external_wake")
            if reset_due:
                for state in self._roots.values():
                    if (state.retry_due or state.refresh_due) and not state.active_lease_id:
                        state.next_due_at = now
            self._revision += 1
        self._wake.set()

    def rearm_succeeded(
        self,
        root_keys: Iterable[str] | None = None,
        *,
        min_age_seconds: float = 30.0,
        reason: str = "conditional_refresh",
    ) -> tuple[str, ...]:
        """Make stale successful roots eligible for one conditional refresh."""

        now = self._clock()
        minimum_age = max(0.0, float(min_age_seconds))
        requested = (
            tuple(str(key or "").strip() for key in root_keys)
            if root_keys is not None
            else tuple(self._roots)
        )
        rearmed: list[str] = []
        with self._lock:
            for key in requested:
                state = self._roots.get(key)
                if (
                    state is None
                    or state.status != "ok"
                    or state.retry_due
                    or state.refresh_due
                    or state.active_lease_id
                    or state.last_success_at <= 0.0
                    or now - state.last_success_at < minimum_age
                ):
                    continue
                state.refresh_due = True
                state.next_due_at = now
                rearmed.append(key)
            if rearmed:
                self._last_wake_reason = str(reason or "conditional_refresh")
                self._revision += 1
        if rearmed:
            self._wake.set()
        return tuple(rearmed)

    def due_root_keys(self, *, limit: int | None = None) -> tuple[str, ...]:
        now = self._clock()
        with self._lock:
            keys = tuple(
                key
                for key, state in self._roots.items()
                if (state.retry_due or state.refresh_due)
                and not state.active_lease_id
                and state.next_due_at <= now
            )
        if limit is None:
            return keys
        return keys[: max(0, int(limit))]

    def postpone_due(
        self,
        root_keys: Iterable[str],
        *,
        reason: str,
        delay_seconds: float | None = None,
    ) -> None:
        delay = (
            self.control_interval_seconds
            if delay_seconds is None
            else max(self.control_interval_seconds, float(delay_seconds))
        )
        now = self._clock()
        with self._lock:
            for raw_key in root_keys:
                state = self._roots.get(str(raw_key or "").strip())
                if (
                    state is None
                    or not (state.retry_due or state.refresh_due)
                    or state.active_lease_id
                ):
                    continue
                state.next_due_at = max(state.next_due_at, now + delay)
            self._last_wake_reason = str(reason or "dispatch_postponed")
            self._revision += 1
            self._wake.clear()

    def actionable(self) -> bool:
        with self._lock:
            return any(
                state.retry_due or state.refresh_due or state.active_lease_id
                for state in self._roots.values()
            )

    def due_now(self) -> bool:
        now = self._clock()
        with self._lock:
            return any(
                (state.retry_due or state.refresh_due)
                and not state.active_lease_id
                and state.next_due_at <= now
                for state in self._roots.values()
            )

    def wait(self, timeout: float | None = None) -> bool:
        signaled = self._wake.wait(
            self.control_interval_seconds if timeout is None else max(0.0, timeout)
        )
        if signaled:
            self._wake.clear()
        return signaled

    def snapshot(self) -> Tier3SourceSupplySnapshot:
        with self._lock:
            values = tuple(self._roots.values())
            return Tier3SourceSupplySnapshot(
                runtime_generation=self.runtime_generation,
                refresh_epoch=self.refresh_epoch,
                configured_roots=len(values),
                pending_roots=sum(
                    1 for state in values if state.status == "pending"
                ),
                retry_due_roots=sum(1 for state in values if state.retry_due),
                active_leases=sum(
                    1 for state in values if state.active_lease_id
                ),
                succeeded_roots=sum(1 for state in values if state.status == "ok"),
                terminal_roots=sum(
                    1 for state in values if state.status == "terminal"
                ),
                dispatch_count=self._dispatch_count,
                deferred_count=self._deferred_count,
                retry_completion_count=self._retry_completion_count,
                revision=self._revision,
                ready_at_monotonic=self._ready_at,
                last_dispatch_at_monotonic=self._last_dispatch_at,
                last_wake_reason=self._last_wake_reason,
                conditional_refresh_due_roots=sum(
                    1 for state in values if state.refresh_due
                ),
            )
