from __future__ import annotations

import threading
import time
import weakref
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Callable


MAX_QUIESCE_SECONDS = 0.5


class RuntimeRecoveryPressurePriority(IntEnum):
    """Closed priority order for runtime network work.

    The numeric values are intentionally spaced so callers can compare values,
    while all admission decisions remain owned by the coordinator.
    """

    BACKGROUND = 10
    HOT_STANDBY = 20
    MANUAL = 30
    MANUAL_VALIDATION = 30
    ACTIVATION = 40
    COLD_ACTIVATION = 40
    RUNTIME_RECOVERY = 50


_PRIORITY_ALIASES: dict[str, RuntimeRecoveryPressurePriority] = {
    "background": RuntimeRecoveryPressurePriority.BACKGROUND,
    "hot": RuntimeRecoveryPressurePriority.HOT_STANDBY,
    "hot_standby": RuntimeRecoveryPressurePriority.HOT_STANDBY,
    "manual": RuntimeRecoveryPressurePriority.MANUAL,
    "manual_validation": RuntimeRecoveryPressurePriority.MANUAL,
    "activation": RuntimeRecoveryPressurePriority.ACTIVATION,
    "cold_activation": RuntimeRecoveryPressurePriority.ACTIVATION,
    "recovery": RuntimeRecoveryPressurePriority.RUNTIME_RECOVERY,
    "runtime_recovery": RuntimeRecoveryPressurePriority.RUNTIME_RECOVERY,
}


def normalise_recovery_pressure_priority(
    value: RuntimeRecoveryPressurePriority | str | int,
) -> RuntimeRecoveryPressurePriority:
    if isinstance(value, RuntimeRecoveryPressurePriority):
        return value
    if isinstance(value, str):
        key = value.strip().casefold().replace("-", "_").replace(" ", "_")
        priority = _PRIORITY_ALIASES.get(key)
        if priority is not None:
            return priority
    try:
        return RuntimeRecoveryPressurePriority(int(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unknown runtime recovery priority: {value!r}") from exc


class RuntimeRecoveryPressureQuiesceState(str, Enum):
    IDLE = "idle"
    IN_PROGRESS = "in_progress"
    QUIESCED = "quiesced"
    TIMED_OUT = "timed_out"
    FAILED = "failed"
    RESUMING = "resuming"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class RuntimeRecoveryPressureSnapshot:
    revision: int
    lifecycle_generation: int
    stopped: bool
    transitioning: bool
    active_lease_count: int
    active_owner_ids: tuple[str, ...]
    active_priorities: tuple[RuntimeRecoveryPressurePriority, ...]
    highest_priority: RuntimeRecoveryPressurePriority | None
    quiesce_state: RuntimeRecoveryPressureQuiesceState
    quiesce_episode: int
    quiesce_deadline_monotonic: float | None
    quiesce_callback_count: int
    resume_callback_count: int
    last_callback_error: str

    @property
    def pressure_active(self) -> bool:
        return bool(
            self.highest_priority is not None
            and self.highest_priority
            >= RuntimeRecoveryPressurePriority.ACTIVATION
        )


class RuntimeRecoveryPressureLease:
    """Generation-bound pressure lease with idempotent local release."""

    __slots__ = (
        "token",
        "owner_id",
        "priority",
        "lifecycle_generation",
        "revision",
        "acquired_at_monotonic",
        "_coordinator_ref",
        "_release_lock",
        "_released",
    )

    def __init__(
        self,
        *,
        token: str,
        owner_id: str,
        priority: RuntimeRecoveryPressurePriority,
        lifecycle_generation: int,
        revision: int,
        acquired_at_monotonic: float,
        coordinator: RuntimeRecoveryPressureCoordinator,
    ) -> None:
        self.token = token
        self.owner_id = owner_id
        self.priority = priority
        self.lifecycle_generation = lifecycle_generation
        self.revision = revision
        self.acquired_at_monotonic = acquired_at_monotonic
        self._coordinator_ref = weakref.ref(coordinator)
        self._release_lock = threading.Lock()
        self._released = False

    @property
    def released(self) -> bool:
        with self._release_lock:
            return self._released

    def _mark_released(self) -> None:
        with self._release_lock:
            self._released = True

    def release(self) -> bool:
        """Release once; repeated calls remain successful and side-effect free."""

        with self._release_lock:
            if self._released:
                return True
        coordinator = self._coordinator_ref()
        if coordinator is None:
            return False
        released = coordinator.release(self)
        if released:
            self._mark_released()
        return released

    def __enter__(self) -> RuntimeRecoveryPressureLease:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.release()

    def __repr__(self) -> str:
        return (
            "RuntimeRecoveryPressureLease("
            f"owner_id={self.owner_id!r}, priority={self.priority.name}, "
            f"lifecycle_generation={self.lifecycle_generation}, "
            f"released={self.released})"
        )


class RuntimeRecoveryPressureCoordinator:
    """Coordinate recovery pressure without owning any worker or socket.

    Lower-priority work may already be in flight when higher-priority work
    arrives.  The high-priority lease is published first (so no new lower work
    can enter), then the generic ``quiesce_callback`` is given at most 500 ms
    to drain external work.  Activation and runtime recovery open a quiesce
    episode; manual, hot-standby, and background leases do not.

    Callbacks run outside the state lock.  A callback exception or timeout is
    reflected in :meth:`snapshot` but never removes or strands the lease.
    """

    def __init__(
        self,
        *,
        quiesce_callback: Callable[[], object] | None = None,
        resume_callback: Callable[[], object] | None = None,
        lifecycle_generation: int = 1,
        quiesce_timeout_seconds: float = MAX_QUIESCE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[str, int, int], str] | None = None,
    ) -> None:
        generation = int(lifecycle_generation)
        if generation < 0:
            raise ValueError("lifecycle_generation must be non-negative")
        timeout = max(0.0, min(MAX_QUIESCE_SECONDS, float(quiesce_timeout_seconds)))

        self._lock = threading.RLock()
        self._clock = clock
        self._quiesce_callback = quiesce_callback
        self._resume_callback = resume_callback
        self._quiesce_timeout_seconds = timeout
        self._token_factory = token_factory or (
            lambda owner, generation, revision: (
                f"pressure-{generation}-{revision}-{owner}"
            )
        )

        self._revision = 0
        self._lifecycle_generation = generation
        self._stopped = False
        self._transitioning = False
        self._active_by_owner: OrderedDict[str, RuntimeRecoveryPressureLease] = (
            OrderedDict()
        )
        self._active_by_token: dict[str, RuntimeRecoveryPressureLease] = {}
        self._released_tokens: OrderedDict[str, None] = OrderedDict()

        self._quiesce_state = RuntimeRecoveryPressureQuiesceState.IDLE
        self._quiesce_episode = 0
        self._quiesce_deadline_monotonic: float | None = None
        self._quiesce_completion: threading.Event | None = None
        self._quiesce_callback_count = 0
        self._resume_callback_count = 0
        self._last_callback_error = ""
        self._callback_threads: set[threading.Thread] = set()

    @property
    def lifecycle_generation(self) -> int:
        with self._lock:
            return self._lifecycle_generation

    def _snapshot_locked(self) -> RuntimeRecoveryPressureSnapshot:
        leases = tuple(self._active_by_owner.values())
        priorities = tuple(lease.priority for lease in leases)
        return RuntimeRecoveryPressureSnapshot(
            revision=self._revision,
            lifecycle_generation=self._lifecycle_generation,
            stopped=self._stopped,
            transitioning=self._transitioning,
            active_lease_count=len(leases),
            active_owner_ids=tuple(lease.owner_id for lease in leases),
            active_priorities=priorities,
            highest_priority=max(priorities, default=None),
            quiesce_state=self._quiesce_state,
            quiesce_episode=self._quiesce_episode,
            quiesce_deadline_monotonic=self._quiesce_deadline_monotonic,
            quiesce_callback_count=self._quiesce_callback_count,
            resume_callback_count=self._resume_callback_count,
            last_callback_error=self._last_callback_error,
        )

    def snapshot(self) -> RuntimeRecoveryPressureSnapshot:
        with self._lock:
            return self._snapshot_locked()

    def _remember_released_locked(self, lease: RuntimeRecoveryPressureLease) -> None:
        lease._mark_released()
        self._released_tokens[lease.token] = None
        self._released_tokens.move_to_end(lease.token)
        while len(self._released_tokens) > 256:
            self._released_tokens.popitem(last=False)

    def _begin_quiesce_locked(self) -> tuple[int, int, threading.Event]:
        self._quiesce_episode += 1
        self._quiesce_state = RuntimeRecoveryPressureQuiesceState.IN_PROGRESS
        self._quiesce_deadline_monotonic = (
            self._clock() + self._quiesce_timeout_seconds
        )
        completion = threading.Event()
        self._quiesce_completion = completion
        self._quiesce_callback_count += 1
        self._last_callback_error = ""
        self._revision += 1
        return self._lifecycle_generation, self._quiesce_episode, completion

    def _start_quiesce_callback(
        self,
        generation: int,
        episode: int,
        completion: threading.Event,
    ) -> None:
        callback = self._quiesce_callback
        if callback is None:
            with self._lock:
                if (
                    generation == self._lifecycle_generation
                    and episode == self._quiesce_episode
                    and self._quiesce_state
                    is RuntimeRecoveryPressureQuiesceState.IN_PROGRESS
                ):
                    self._quiesce_state = (
                        RuntimeRecoveryPressureQuiesceState.QUIESCED
                    )
                    self._revision += 1
            completion.set()
            return

        def invoke() -> None:
            error = ""
            try:
                callback()
            except BaseException as exc:  # callback failures are state, not control flow
                error = f"quiesce_callback:{type(exc).__name__}"
            finally:
                with self._lock:
                    if (
                        generation == self._lifecycle_generation
                        and episode == self._quiesce_episode
                        and self._quiesce_state
                        is RuntimeRecoveryPressureQuiesceState.IN_PROGRESS
                    ):
                        if error:
                            self._quiesce_state = (
                                RuntimeRecoveryPressureQuiesceState.FAILED
                            )
                            self._last_callback_error = error
                        else:
                            self._quiesce_state = (
                                RuntimeRecoveryPressureQuiesceState.QUIESCED
                            )
                        self._revision += 1
                    self._callback_threads.discard(threading.current_thread())
                completion.set()

        worker = threading.Thread(
            target=invoke,
            name=f"runtime-pressure-quiesce-{generation}-{episode}",
            daemon=True,
        )
        with self._lock:
            self._callback_threads.add(worker)
        worker.start()

    def _wait_for_quiesce(
        self,
        generation: int,
        episode: int,
        completion: threading.Event,
    ) -> None:
        with self._lock:
            deadline = self._quiesce_deadline_monotonic
        remaining = self._quiesce_timeout_seconds
        if deadline is not None:
            remaining = max(0.0, min(remaining, deadline - self._clock()))
        if completion.wait(remaining):
            return
        with self._lock:
            if (
                generation == self._lifecycle_generation
                and episode == self._quiesce_episode
                and self._quiesce_state
                is RuntimeRecoveryPressureQuiesceState.IN_PROGRESS
            ):
                self._quiesce_state = RuntimeRecoveryPressureQuiesceState.TIMED_OUT
                self._last_callback_error = "quiesce_callback:deadline_expired"
                self._revision += 1
        completion.set()

    def acquire(
        self,
        owner_id: str,
        priority: RuntimeRecoveryPressurePriority | str | int,
        *,
        lifecycle_generation: int | None = None,
    ) -> RuntimeRecoveryPressureLease | None:
        """Acquire pressure authority, or ``None`` when a higher owner wins.

        The same owner and priority always receive the same live lease.  Reuse
        of one owner with a different priority is an integration error.
        """

        owner = str(owner_id or "").strip()
        if not owner:
            raise ValueError("owner_id must not be empty")
        normalised_priority = normalise_recovery_pressure_priority(priority)

        start_quiesce: tuple[int, int, threading.Event] | None = None
        wait_for: tuple[int, int, threading.Event] | None = None
        with self._lock:
            if self._stopped or self._transitioning:
                return None
            if (
                lifecycle_generation is not None
                and int(lifecycle_generation) != self._lifecycle_generation
            ):
                return None

            existing = self._active_by_owner.get(owner)
            if existing is not None:
                if existing.priority != normalised_priority:
                    raise ValueError(
                        "an active owner cannot change pressure priority"
                    )
                if (
                    existing.priority
                    >= RuntimeRecoveryPressurePriority.ACTIVATION
                    and self._quiesce_state
                    is RuntimeRecoveryPressureQuiesceState.IN_PROGRESS
                    and self._quiesce_completion is not None
                ):
                    wait_for = (
                        self._lifecycle_generation,
                        self._quiesce_episode,
                        self._quiesce_completion,
                    )
                lease = existing
            else:
                active_priorities = (
                    lease.priority for lease in self._active_by_owner.values()
                )
                highest = max(active_priorities, default=None)
                if highest is not None and normalised_priority < highest:
                    return None

                self._revision += 1
                token = str(
                    self._token_factory(
                        owner,
                        self._lifecycle_generation,
                        self._revision,
                    )
                )
                if not token or token in self._active_by_token:
                    raise ValueError("token_factory must return a unique token")
                lease = RuntimeRecoveryPressureLease(
                    token=token,
                    owner_id=owner,
                    priority=normalised_priority,
                    lifecycle_generation=self._lifecycle_generation,
                    revision=self._revision,
                    acquired_at_monotonic=self._clock(),
                    coordinator=self,
                )
                self._active_by_owner[owner] = lease
                self._active_by_token[token] = lease

                if (
                    normalised_priority
                    >= RuntimeRecoveryPressurePriority.ACTIVATION
                    and self._quiesce_state
                    in {
                        RuntimeRecoveryPressureQuiesceState.IDLE,
                        RuntimeRecoveryPressureQuiesceState.STOPPED,
                    }
                ):
                    start_quiesce = self._begin_quiesce_locked()
                    wait_for = start_quiesce

        if start_quiesce is not None:
            self._start_quiesce_callback(*start_quiesce)
        if wait_for is not None:
            self._wait_for_quiesce(*wait_for)
        return lease

    def _finish_resume(self, generation: int, episode: int) -> None:
        error = ""
        callback = self._resume_callback
        if callback is not None:
            try:
                callback()
            except BaseException as exc:  # callback failures must not strand pressure
                error = f"resume_callback:{type(exc).__name__}"
        with self._lock:
            if (
                generation == self._lifecycle_generation
                and episode == self._quiesce_episode
            ):
                self._transitioning = False
                self._quiesce_state = (
                    RuntimeRecoveryPressureQuiesceState.STOPPED
                    if self._stopped
                    else RuntimeRecoveryPressureQuiesceState.IDLE
                )
                self._quiesce_deadline_monotonic = None
                self._quiesce_completion = None
                if error:
                    self._last_callback_error = error
                self._revision += 1

    def release(self, lease_or_token: RuntimeRecoveryPressureLease | str) -> bool:
        """Release a lease; already released known tokens are harmless."""

        token = (
            lease_or_token.token
            if isinstance(lease_or_token, RuntimeRecoveryPressureLease)
            else str(lease_or_token or "")
        )
        resume: tuple[int, int] | None = None
        with self._lock:
            lease = self._active_by_token.get(token)
            if lease is None:
                return token in self._released_tokens
            if (
                isinstance(lease_or_token, RuntimeRecoveryPressureLease)
                and lease_or_token is not lease
            ):
                return False

            self._active_by_token.pop(token, None)
            self._active_by_owner.pop(lease.owner_id, None)
            self._remember_released_locked(lease)
            self._revision += 1

            pressure_remains = any(
                current.priority >= RuntimeRecoveryPressurePriority.ACTIVATION
                for current in self._active_by_owner.values()
            )
            if (
                not pressure_remains
                and self._quiesce_state
                in {
                    RuntimeRecoveryPressureQuiesceState.IN_PROGRESS,
                    RuntimeRecoveryPressureQuiesceState.QUIESCED,
                    RuntimeRecoveryPressureQuiesceState.TIMED_OUT,
                    RuntimeRecoveryPressureQuiesceState.FAILED,
                }
            ):
                self._transitioning = True
                self._quiesce_state = RuntimeRecoveryPressureQuiesceState.RESUMING
                if self._quiesce_completion is not None:
                    self._quiesce_completion.set()
                self._resume_callback_count += 1
                self._revision += 1
                resume = (self._lifecycle_generation, self._quiesce_episode)

        if resume is not None:
            self._finish_resume(*resume)
        return True

    def _invalidate_all_locked(self) -> None:
        for lease in tuple(self._active_by_owner.values()):
            self._remember_released_locked(lease)
        self._active_by_owner.clear()
        self._active_by_token.clear()
        if self._quiesce_completion is not None:
            self._quiesce_completion.set()

    def reset_for_lifecycle(
        self,
        lifecycle_generation: int | None = None,
    ) -> RuntimeRecoveryPressureSnapshot:
        """Invalidate old leases and begin a strictly newer lifecycle."""

        with self._lock:
            requested = (
                self._lifecycle_generation + 1
                if lifecycle_generation is None
                else int(lifecycle_generation)
            )
            if requested <= self._lifecycle_generation:
                raise ValueError("new lifecycle generation must be greater")
            needs_resume = self._quiesce_state in {
                RuntimeRecoveryPressureQuiesceState.IN_PROGRESS,
                RuntimeRecoveryPressureQuiesceState.QUIESCED,
                RuntimeRecoveryPressureQuiesceState.TIMED_OUT,
                RuntimeRecoveryPressureQuiesceState.FAILED,
            }
            self._invalidate_all_locked()
            self._lifecycle_generation = requested
            self._stopped = False
            self._transitioning = needs_resume
            self._quiesce_state = (
                RuntimeRecoveryPressureQuiesceState.RESUMING
                if needs_resume
                else RuntimeRecoveryPressureQuiesceState.IDLE
            )
            self._quiesce_deadline_monotonic = None
            self._quiesce_completion = None
            self._revision += 1
            generation = self._lifecycle_generation
            episode = self._quiesce_episode
            if needs_resume:
                self._resume_callback_count += 1

        if needs_resume:
            self._finish_resume(generation, episode)
        return self.snapshot()

    def stop(
        self,
        *,
        timeout_seconds: float = MAX_QUIESCE_SECONDS,
    ) -> RuntimeRecoveryPressureSnapshot:
        """Invalidate the lifecycle and return within 500 ms.

        Potentially uncooperative callbacks are daemon-owned integration work;
        this coordinator never performs an unbounded join.
        """

        timeout = max(0.0, min(MAX_QUIESCE_SECONDS, float(timeout_seconds)))
        started = time.monotonic()
        with self._lock:
            if self._stopped:
                return self._snapshot_locked()
            needs_resume = self._quiesce_state in {
                RuntimeRecoveryPressureQuiesceState.IN_PROGRESS,
                RuntimeRecoveryPressureQuiesceState.QUIESCED,
                RuntimeRecoveryPressureQuiesceState.TIMED_OUT,
                RuntimeRecoveryPressureQuiesceState.FAILED,
            }
            self._invalidate_all_locked()
            self._lifecycle_generation += 1
            self._stopped = True
            self._transitioning = False
            self._quiesce_state = RuntimeRecoveryPressureQuiesceState.STOPPED
            self._quiesce_deadline_monotonic = None
            self._quiesce_completion = None
            self._revision += 1
            if needs_resume:
                self._resume_callback_count += 1
            callback = self._resume_callback if needs_resume else None

        # Stop cannot trust an integration callback to return. Dispatch it as
        # a daemon and wait only inside the caller's bounded stop budget.
        if callback is not None:
            done = threading.Event()

            def resume_on_stop() -> None:
                error = ""
                try:
                    callback()
                except BaseException as exc:
                    error = f"resume_callback:{type(exc).__name__}"
                finally:
                    with self._lock:
                        if error and self._stopped:
                            self._last_callback_error = error
                            self._revision += 1
                        self._callback_threads.discard(threading.current_thread())
                    done.set()

            worker = threading.Thread(
                target=resume_on_stop,
                name=f"runtime-pressure-stop-{self._lifecycle_generation}",
                daemon=True,
            )
            with self._lock:
                self._callback_threads.add(worker)
            worker.start()
            done.wait(max(0.0, timeout - (time.monotonic() - started)))

        deadline = started + timeout
        with self._lock:
            threads = tuple(self._callback_threads)
        for worker in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                break
            worker.join(remaining)
        return self.snapshot()


__all__ = [
    "MAX_QUIESCE_SECONDS",
    "RuntimeRecoveryPressureCoordinator",
    "RuntimeRecoveryPressureLease",
    "RuntimeRecoveryPressurePriority",
    "RuntimeRecoveryPressureQuiesceState",
    "RuntimeRecoveryPressureSnapshot",
    "normalise_recovery_pressure_priority",
]
