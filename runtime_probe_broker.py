from __future__ import annotations

import heapq
import itertools
import math
import threading
import time
from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Callable, Generic, TypeVar


MAX_ACTIVE_SOCKET_SLOTS = 2
_CANCELLATION_POLL_SECONDS = 0.05
_WORKER_CONTROL_POLL_SECONDS = 0.01
_EXECUTION_ABORT_GRACE_SECONDS = 0.1
# Leave a little scheduler headroom so stop() itself stays below its 500 ms
# public bound even on a busy Windows host.
_STOP_ABORT_GRACE_SECONDS = 0.45

T = TypeVar("T")


class ProbePriority(IntEnum):
    """Dispatch order for operations that can open probe sockets."""

    RUNTIME_MANUAL = 0
    ACTIVATION = 1
    HOT_STANDBY = 2
    BACKGROUND = 3


class ProbeDisposition(str, Enum):
    """Whether a caller obtained authoritative socket-level evidence."""

    EXECUTED = "executed"
    INDETERMINATE = "indeterminate"


class ProbeIndeterminateReason(str, Enum):
    QUEUE_TIMEOUT = "queue_timeout"
    STARTED_TIMEOUT = "started_timeout"
    EXECUTION_TIMEOUT = "execution_timeout"
    CANCELLED = "cancelled"
    BROKER_STOPPED = "broker_stopped"
    BROKER_POISONED = "broker_poisoned"
    SOCKET_NOT_STARTED = "socket_not_started"


class ProbeRequestState(str, Enum):
    QUEUED = "queued"
    GRANTED = "granted"
    RELEASED = "released"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class ProbeTiming:
    """Queue and socket timing kept in separate, non-overlapping fields."""

    queued_at_monotonic: float
    granted_at_monotonic: float | None
    socket_started_at_monotonic: float | None
    completed_at_monotonic: float
    queue_wait_seconds: float
    socket_elapsed_seconds: float | None


@dataclass(frozen=True, slots=True)
class ProbeExecutionResult(Generic[T]):
    """One brokered probe result.

    The broker deliberately has no ``transport_failure`` disposition.  An
    exception is exposed to the adapter only when the probe marked that its
    real socket attempt had started.  The adapter can then apply its existing
    transport classification.  Queue/cancellation/facility outcomes remain
    explicitly indeterminate and must not penalise a node.
    """

    disposition: ProbeDisposition
    timing: ProbeTiming
    value: T | None = None
    error: Exception | None = None
    indeterminate_reason: ProbeIndeterminateReason | None = None

    @property
    def is_indeterminate(self) -> bool:
        return self.disposition is ProbeDisposition.INDETERMINATE

    @property
    def socket_attempted(self) -> bool:
        return self.timing.socket_started_at_monotonic is not None


@dataclass(frozen=True, slots=True)
class ProbeBrokerSnapshot:
    max_active_sockets: int
    active_socket_slots: int
    queued_runtime_manual: int
    queued_activation: int
    queued_hot_standby: int
    queued_background: int
    stopped: bool
    poisoned: bool = False
    poison_reason: str | None = None

    @property
    def queued_total(self) -> int:
        return (
            self.queued_runtime_manual
            + self.queued_activation
            + self.queued_hot_standby
            + self.queued_background
        )


@dataclass(slots=True)
class _Waiter:
    request_id: int
    sequence: int
    priority: ProbePriority
    slots: int
    enqueued_at_monotonic: float
    state: ProbeRequestState = ProbeRequestState.QUEUED
    granted_at_monotonic: float | None = None
    terminal_at_monotonic: float | None = None
    indeterminate_reason: ProbeIndeterminateReason | None = None
    ticket: ProbeTicket | None = None
    claimed: bool = False
    cancellation_event: threading.Event | None = None


@dataclass(frozen=True, slots=True)
class ProbeAdmission:
    """Admission result for one request (one slot or one atomic pair)."""

    priority: ProbePriority
    slots: int
    timing: ProbeTiming
    ticket: ProbeTicket | None = None
    indeterminate_reason: ProbeIndeterminateReason | None = None

    @property
    def admitted(self) -> bool:
        return self.ticket is not None


class ProbeRequest:
    """A cancellable queued request returned before admission is known."""

    def __init__(self, broker: RuntimeProbeBroker, waiter: _Waiter) -> None:
        self._broker = broker
        self._waiter = waiter

    @property
    def request_id(self) -> int:
        return self._waiter.request_id

    @property
    def priority(self) -> ProbePriority:
        return self._waiter.priority

    @property
    def slots(self) -> int:
        return self._waiter.slots

    @property
    def state(self) -> ProbeRequestState:
        return self._broker._request_state(self._waiter)

    def wait(
        self,
        timeout_seconds: float | None = None,
        *,
        cancel_event: threading.Event | None = None,
    ) -> ProbeAdmission:
        return self._broker._wait_for_request(
            self._waiter,
            timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
        )

    def cancel(self) -> bool:
        """Cancel a queued request or signal a claimed ticket to stop."""

        return self._broker._cancel_request(self._waiter)


class ProbeTicket:
    """Reservation for one socket slot or an indivisible two-slot pair."""

    def __init__(self, broker: RuntimeProbeBroker, waiter: _Waiter) -> None:
        self._broker = broker
        self._waiter = waiter
        self._release_lock = threading.Lock()
        self._released = False
        self._released_slots: set[int] = set()

    @property
    def request_id(self) -> int:
        return self._waiter.request_id

    @property
    def priority(self) -> ProbePriority:
        return self._waiter.priority

    @property
    def slots(self) -> int:
        return self._waiter.slots

    @property
    def queue_wait_seconds(self) -> float:
        granted = self._waiter.granted_at_monotonic
        if granted is None:
            return 0.0
        return _elapsed(self._waiter.enqueued_at_monotonic, granted)

    @property
    def cancelled(self) -> bool:
        event = self._waiter.cancellation_event
        return bool(event is not None and event.is_set())

    def execution(
        self,
        slot_index: int = 0,
        *,
        cancel_event: threading.Event | None = None,
        socket_start_timeout_seconds: float | None = None,
        execution_deadline_monotonic: float | None = None,
    ) -> ProbeExecutionContext:
        if not 0 <= int(slot_index) < self.slots:
            raise IndexError("slot_index is outside this ticket")
        with self._release_lock:
            if self._released:
                raise RuntimeError("probe ticket has already been released")
        return ProbeExecutionContext(
            ticket=self,
            slot_index=int(slot_index),
            cancel_event=cancel_event,
            socket_start_timeout_seconds=socket_start_timeout_seconds,
            execution_deadline_monotonic=execution_deadline_monotonic,
        )

    def release(self) -> None:
        with self._release_lock:
            if self._released:
                return
            released_count = self.slots - len(self._released_slots)
            self._released_slots.update(range(self.slots))
            self._released = True
        self._broker._release_ticket_slots(
            self._waiter,
            released_count,
            fully_released=True,
        )

    def release_slot(self, slot_index: int) -> None:
        """Release one worker's slot only after that worker has really exited."""

        selected = int(slot_index)
        if not 0 <= selected < self.slots:
            raise IndexError("slot_index is outside this ticket")
        with self._release_lock:
            if selected in self._released_slots:
                return
            self._released_slots.add(selected)
            fully_released = len(self._released_slots) == self.slots
            if fully_released:
                self._released = True
        self._broker._release_ticket_slots(
            self._waiter,
            1,
            fully_released=fully_released,
        )

    def cancel(self) -> bool:
        """Cooperatively cancel executions using this claimed ticket."""

        return self._broker._signal_ticket_cancel(self._waiter)

    def __enter__(self) -> ProbeTicket:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


class ProbeExecutionContext:
    """Context passed to a probe callable after a slot is reserved.

    ``mark_socket_started`` must be called immediately before the real dial,
    connect, handshake, or controller delay request begins.  Preparation and
    executor queueing belong before that marker and are never counted as
    socket time.
    """

    def __init__(
        self,
        *,
        ticket: ProbeTicket,
        slot_index: int,
        cancel_event: threading.Event | None,
        socket_start_timeout_seconds: float | None,
        execution_deadline_monotonic: float | None = None,
    ) -> None:
        self._ticket = ticket
        self._slot_index = int(slot_index)
        self._cancel_event = cancel_event
        self._state_lock = threading.Lock()
        self._socket_started_at_monotonic: float | None = None
        self._execution_deadline_monotonic = _normalise_deadline(
            execution_deadline_monotonic
        )
        self._abort_callback: Callable[[], None] | None = None
        self._abort_invoked = False
        self._contract_violated = False
        self._execution_timed_out = False
        start_timeout = _normalise_timeout(socket_start_timeout_seconds)
        granted = ticket._waiter.granted_at_monotonic
        self._socket_start_deadline_monotonic = (
            None
            if start_timeout is None or granted is None
            else granted + start_timeout
        )
        self._started_timeout = False

    @property
    def request_id(self) -> int:
        return self._ticket.request_id

    @property
    def priority(self) -> ProbePriority:
        return self._ticket.priority

    @property
    def slot_index(self) -> int:
        return self._slot_index

    @property
    def cancelled(self) -> bool:
        return bool(
            self._ticket.cancelled
            or (self._cancel_event is not None and self._cancel_event.is_set())
        )

    @property
    def socket_started_at_monotonic(self) -> float | None:
        with self._state_lock:
            return self._socket_started_at_monotonic

    @property
    def started_timeout(self) -> bool:
        with self._state_lock:
            return self._started_timeout

    @property
    def execution_deadline_monotonic(self) -> float | None:
        return self._execution_deadline_monotonic

    @property
    def abort_registered(self) -> bool:
        with self._state_lock:
            return self._abort_callback is not None

    @property
    def contract_violated(self) -> bool:
        with self._state_lock:
            return self._contract_violated

    @property
    def execution_timed_out(self) -> bool:
        with self._state_lock:
            return self._execution_timed_out

    def register_abort_callback(self, callback: Callable[[], None]) -> bool:
        """Register the close/abort action before marking a real socket start.

        Deadline-controlled adapters must register exactly one non-blocking
        callback (for example ``socket.close`` or ``connection.close``) before
        ``mark_socket_started``.  If cancellation won the race, the callback is
        scheduled immediately and ``False`` is returned.
        """

        if not callable(callback):
            raise TypeError("abort callback must be callable")
        with self._state_lock:
            if self._socket_started_at_monotonic is not None:
                raise RuntimeError("abort callback must be registered before socket start")
            if self._abort_callback is not None:
                raise RuntimeError("abort callback was already registered")
            self._abort_callback = callback
        cancelled = self.cancelled or self._deadline_has_expired()
        if cancelled:
            self._schedule_abort()
        return not cancelled

    # Concise alias for transport adapters; the explicit name remains the
    # canonical API and documents what is registered.
    register_abort = register_abort_callback

    def mark_socket_started(self) -> bool:
        """Mark an actual socket attempt.

        The adapter must not dial when this returns false: cancellation or the
        post-admission start deadline has already made the attempt stale.
        """

        if self.cancelled:
            return False
        contract_violated = False
        with self._state_lock:
            if self._socket_started_at_monotonic is not None:
                raise RuntimeError("socket attempt was already marked as started")
            if (
                self._execution_deadline_monotonic is not None
                and self._abort_callback is None
            ):
                self._contract_violated = True
                contract_violated = True
            if self._deadline_has_expired_locked():
                self._execution_timed_out = True
                return False
            now = self._ticket._broker._now()
            if (
                self._socket_start_deadline_monotonic is not None
                and now > self._socket_start_deadline_monotonic
            ):
                self._started_timeout = True
                return False
            if self.cancelled:
                return False
            if not contract_violated:
                self._socket_started_at_monotonic = now
        if contract_violated:
            self._ticket._broker._mark_poisoned(
                "deadline-controlled probe started without an abort callback"
            )
            return False
        return True

    def _deadline_has_expired(self) -> bool:
        with self._state_lock:
            return self._deadline_has_expired_locked()

    def _deadline_has_expired_locked(self) -> bool:
        deadline = self._execution_deadline_monotonic
        return deadline is not None and time.monotonic() >= deadline

    def _mark_execution_timed_out(self) -> None:
        with self._state_lock:
            self._execution_timed_out = True

    def _schedule_abort(self) -> threading.Thread | None:
        with self._state_lock:
            callback = self._abort_callback
            if callback is None or self._abort_invoked:
                return None
            self._abort_invoked = True

        def invoke() -> None:
            try:
                callback()
            except Exception:
                # Closing a half-torn-down socket can itself fail.  The worker
                # exit/poison decision is authoritative; abort errors are not
                # node transport evidence.
                return

        thread = threading.Thread(
            target=invoke,
            name=f"runtime-probe-abort-{self.request_id}-{self.slot_index}",
            daemon=True,
        )
        thread.start()
        return thread


class RuntimeProbeBroker:
    """Strict-priority, process-local budget for runtime probe sockets.

    Admission is centralised under one condition lock.  A two-slot request is
    granted only when both slots are free; it never consumes one slot while it
    waits for the other.  The oldest request at the highest queued priority is
    the only eligible head, so later background work cannot pass a queued
    runtime/manual request.
    """

    def __init__(
        self,
        *,
        max_active_sockets: int = MAX_ACTIVE_SOCKET_SLOTS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        capacity = int(max_active_sockets)
        if not 1 <= capacity <= MAX_ACTIVE_SOCKET_SLOTS:
            raise ValueError(
                f"max_active_sockets must be between 1 and {MAX_ACTIVE_SOCKET_SLOTS}"
            )
        self._capacity = capacity
        self._clock = clock
        self._condition = threading.Condition(threading.RLock())
        self._sequence = itertools.count()
        self._request_ids = itertools.count(1)
        self._queue: list[tuple[int, int, _Waiter]] = []
        self._waiters: dict[int, _Waiter] = {}
        self._active_socket_slots = 0
        self._active_contexts: dict[int, ProbeExecutionContext] = {}
        self._stopped = False
        self._poisoned = False
        self._poison_reason: str | None = None

    @property
    def max_active_sockets(self) -> int:
        return self._capacity

    @property
    def stopped(self) -> bool:
        with self._condition:
            return self._stopped

    @property
    def poisoned(self) -> bool:
        with self._condition:
            return self._poisoned

    @property
    def poison_reason(self) -> str | None:
        with self._condition:
            return self._poison_reason

    def request(
        self,
        priority: ProbePriority | int,
        *,
        slots: int = 1,
    ) -> ProbeRequest:
        selected_priority = _coerce_priority(priority)
        requested_slots = int(slots)
        if requested_slots not in (1, 2):
            raise ValueError("slots must be 1 or 2")
        if requested_slots > self._capacity:
            raise ValueError("requested slots exceed broker capacity")

        with self._condition:
            now = self._now()
            waiter = _Waiter(
                request_id=next(self._request_ids),
                sequence=next(self._sequence),
                priority=selected_priority,
                slots=requested_slots,
                enqueued_at_monotonic=now,
                cancellation_event=threading.Event(),
            )
            self._waiters[waiter.request_id] = waiter
            if self._stopped:
                self._make_indeterminate_locked(
                    waiter,
                    ProbeIndeterminateReason.BROKER_STOPPED,
                    now=now,
                )
            elif self._poisoned:
                self._make_indeterminate_locked(
                    waiter,
                    ProbeIndeterminateReason.BROKER_POISONED,
                    now=now,
                )
            else:
                heapq.heappush(
                    self._queue,
                    (int(waiter.priority), waiter.sequence, waiter),
                )
                self._dispatch_locked()
            return ProbeRequest(self, waiter)

    def request_pair(self, priority: ProbePriority | int) -> ProbeRequest:
        return self.request(priority, slots=2)

    def acquire(
        self,
        priority: ProbePriority | int,
        *,
        slots: int = 1,
        timeout_seconds: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> ProbeAdmission:
        return self.request(priority, slots=slots).wait(
            timeout_seconds,
            cancel_event=cancel_event,
        )

    def acquire_pair(
        self,
        priority: ProbePriority | int,
        *,
        timeout_seconds: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> ProbeAdmission:
        return self.acquire(
            priority,
            slots=2,
            timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
        )

    def run(
        self,
        priority: ProbePriority | int,
        probe: Callable[[ProbeExecutionContext], T],
        *,
        queue_timeout_seconds: float | None = None,
        socket_start_timeout_seconds: float | None = None,
        cancel_event: threading.Event | None = None,
        execution_deadline_monotonic: float | None = None,
    ) -> ProbeExecutionResult[T]:
        execution_deadline = _normalise_deadline(execution_deadline_monotonic)
        queue_timeout, deadline_limited_queue = _queue_timeout_for_deadline(
            queue_timeout_seconds,
            execution_deadline,
        )
        admission = self.acquire(
            priority,
            timeout_seconds=queue_timeout,
            cancel_event=cancel_event,
        )
        if admission.ticket is None:
            result = _indeterminate_from_admission(admission)
            if (
                deadline_limited_queue
                and result.indeterminate_reason
                is ProbeIndeterminateReason.QUEUE_TIMEOUT
            ):
                return _with_indeterminate_reason(
                    result,
                    ProbeIndeterminateReason.EXECUTION_TIMEOUT,
                )
            return result

        ticket = admission.ticket
        context = ticket.execution(
            0,
            cancel_event=cancel_event,
            socket_start_timeout_seconds=socket_start_timeout_seconds,
            execution_deadline_monotonic=execution_deadline,
        )
        if execution_deadline is None:
            with ticket:
                return self._execute_probe(
                    context,
                    probe,
                    admission=admission,
                )

        if time.monotonic() >= execution_deadline:
            context._mark_execution_timed_out()
            ticket.cancel()
            ticket.release()
            return self._indeterminate_after_grant(
                admission,
                ProbeIndeterminateReason.EXECUTION_TIMEOUT,
                socket_started=context.socket_started_at_monotonic,
            )

        result_holder: list[ProbeExecutionResult[T] | None] = [None]
        finished_at_monotonic: list[float | None] = [None]

        def worker() -> None:
            try:
                result_holder[0] = self._execute_probe(
                    context,
                    probe,
                    admission=admission,
                )
            finally:
                finished_at_monotonic[0] = time.monotonic()

        thread = threading.Thread(
            target=worker,
            name=f"runtime-probe-{ticket.request_id}-0",
            daemon=True,
        )
        try:
            thread.start()
        except BaseException:
            ticket.cancel()
            ticket.release()
            raise

        control_reason = self._wait_for_workers(
            (thread,),
            execution_deadline_monotonic=execution_deadline,
            cancel_event=cancel_event,
            ticket=ticket,
        )
        if control_reason is None:
            finished_at = finished_at_monotonic[0]
            if finished_at is None or finished_at > execution_deadline:
                context._mark_execution_timed_out()
                ticket.release()
                return self._indeterminate_after_grant(
                    admission,
                    ProbeIndeterminateReason.EXECUTION_TIMEOUT,
                    socket_started=context.socket_started_at_monotonic,
                )
            ticket.release()
            result = result_holder[0]
            if result is not None:
                return result
            return self._indeterminate_after_grant(
                admission,
                ProbeIndeterminateReason.SOCKET_NOT_STARTED,
            )

        if control_reason is ProbeIndeterminateReason.EXECUTION_TIMEOUT:
            context._mark_execution_timed_out()
        ticket.cancel()
        abort_threads = self._abort_contexts((context,))
        cooperative = self._wait_for_abort(
            (thread,),
            abort_threads,
            _EXECUTION_ABORT_GRACE_SECONDS,
        )
        if cooperative:
            ticket.release()
        else:
            self._mark_poisoned(
                "probe worker or abort callback did not exit after cancellation"
            )
            self._release_slots_after_workers(ticket, ((0, thread),))
        return self._indeterminate_after_grant(
            admission,
            control_reason,
            socket_started=context.socket_started_at_monotonic,
        )

    def run_pair(
        self,
        priority: ProbePriority | int,
        first_probe: Callable[[ProbeExecutionContext], T],
        second_probe: Callable[[ProbeExecutionContext], T],
        *,
        queue_timeout_seconds: float | None = None,
        socket_start_timeout_seconds: float | None = None,
        cancel_event: threading.Event | None = None,
        execution_deadline_monotonic: float | None = None,
        execution_timeout_seconds: float | None = None,
    ) -> tuple[ProbeExecutionResult[T], ProbeExecutionResult[T]]:
        """Reserve both slots, then run a pair inside its execution window.

        An optional relative window begins after admission, so waiting behind
        an already active socket does not consume the pair's transport budget.
        The caller's absolute deadline continues to bound the entire request.
        """

        execution_deadline = _normalise_deadline(execution_deadline_monotonic)
        execution_timeout = _normalise_timeout(execution_timeout_seconds)
        queue_timeout, deadline_limited_queue = _queue_timeout_for_deadline(
            queue_timeout_seconds,
            execution_deadline,
        )
        admission = self.acquire_pair(
            priority,
            timeout_seconds=queue_timeout,
            cancel_event=cancel_event,
        )
        if admission.ticket is None:
            result: ProbeExecutionResult[T] = _indeterminate_from_admission(
                admission
            )
            if (
                deadline_limited_queue
                and result.indeterminate_reason
                is ProbeIndeterminateReason.QUEUE_TIMEOUT
            ):
                result = _with_indeterminate_reason(
                    result,
                    ProbeIndeterminateReason.EXECUTION_TIMEOUT,
                )
            return result, result

        ticket = admission.ticket
        if execution_timeout is not None:
            phase_deadline = time.monotonic() + execution_timeout
            execution_deadline = (
                phase_deadline
                if execution_deadline is None
                else min(execution_deadline, phase_deadline)
            )
        results: list[ProbeExecutionResult[T] | None] = [None, None]
        finished_at_monotonic: list[float | None] = [None, None]
        callables = (first_probe, second_probe)

        contexts = tuple(
            ticket.execution(
                index,
                cancel_event=cancel_event,
                socket_start_timeout_seconds=socket_start_timeout_seconds,
                execution_deadline_monotonic=execution_deadline,
            )
            for index in range(2)
        )

        if execution_deadline is not None and time.monotonic() >= execution_deadline:
            for context in contexts:
                context._mark_execution_timed_out()
            ticket.cancel()
            ticket.release()
            result = self._indeterminate_after_grant(
                admission,
                ProbeIndeterminateReason.EXECUTION_TIMEOUT,
            )
            return result, result

        def worker(index: int) -> None:
            try:
                results[index] = self._execute_probe(
                    contexts[index],
                    callables[index],
                    admission=admission,
                )
            finally:
                finished_at_monotonic[index] = time.monotonic()

        threads = tuple(
            threading.Thread(
                target=worker,
                args=(index,),
                name=f"runtime-probe-{ticket.request_id}-{index}",
                daemon=True,
            )
            for index in range(2)
        )
        started_threads: list[threading.Thread] = []
        try:
            for thread in threads:
                thread.start()
                started_threads.append(thread)
            control_reason = self._wait_for_workers(
                threads,
                execution_deadline_monotonic=execution_deadline,
                cancel_event=cancel_event,
                ticket=ticket,
            )
        except BaseException:
            ticket.cancel()
            abort_threads = self._abort_contexts(contexts)
            cooperative = self._wait_for_abort(
                tuple(started_threads),
                abort_threads,
                _EXECUTION_ABORT_GRACE_SECONDS,
            )
            if cooperative:
                ticket.release()
            else:
                self._mark_poisoned(
                    "probe worker or abort callback did not exit after cancellation"
                )
                self._release_slots_after_workers(
                    ticket,
                    tuple(enumerate(started_threads)),
                )
            raise

        if control_reason is None:
            ticket.release()
            if execution_deadline is not None:
                late = tuple(
                    finished_at is None or finished_at > execution_deadline
                    for finished_at in finished_at_monotonic
                )
                if any(late):
                    for index, is_late in enumerate(late):
                        if is_late:
                            contexts[index]._mark_execution_timed_out()
                    return tuple(
                        (
                            self._indeterminate_after_grant(
                                admission,
                                ProbeIndeterminateReason.EXECUTION_TIMEOUT,
                                socket_started=(
                                    contexts[index].socket_started_at_monotonic
                                ),
                            )
                            if late[index]
                            else results[index]
                            or self._indeterminate_after_grant(
                                admission,
                                ProbeIndeterminateReason.SOCKET_NOT_STARTED,
                            )
                        )
                        for index in range(2)
                    )  # type: ignore[return-value]
        else:
            unfinished = tuple(thread.is_alive() for thread in threads)
            if control_reason is ProbeIndeterminateReason.EXECUTION_TIMEOUT:
                for index, context in enumerate(contexts):
                    if unfinished[index]:
                        context._mark_execution_timed_out()
            ticket.cancel()
            abort_threads = self._abort_contexts(contexts)
            cooperative = self._wait_for_abort(
                threads,
                abort_threads,
                _EXECUTION_ABORT_GRACE_SECONDS,
            )
            if cooperative:
                ticket.release()
            else:
                self._mark_poisoned(
                    "probe worker or abort callback did not exit after cancellation"
                )
                self._release_slots_after_workers(
                    ticket,
                    tuple(enumerate(threads)),
                )
            return tuple(
                (
                    self._indeterminate_after_grant(
                        admission,
                        control_reason,
                        socket_started=contexts[index].socket_started_at_monotonic,
                    )
                    if unfinished[index]
                    else results[index]
                    or self._indeterminate_after_grant(
                        admission,
                        control_reason,
                        socket_started=contexts[index].socket_started_at_monotonic,
                    )
                )
                for index in range(2)
            )  # type: ignore[return-value]

        first_result, second_result = results
        if first_result is None or second_result is None:
            # The worker wrapper catches ordinary probe exceptions, so this is
            # reachable only for process-level thread failure.  It is not node
            # evidence and must remain indeterminate.
            fallback = self._indeterminate_after_grant(
                admission,
                ProbeIndeterminateReason.SOCKET_NOT_STARTED,
            )
            return first_result or fallback, second_result or fallback
        return first_result, second_result

    def stop(self, *, cancel_active: bool = True) -> None:
        """Permanently stop admission and abort active probes within 500 ms."""

        with self._condition:
            if self._stopped:
                return
            self._stopped = True
            now = self._now()
            for waiter in tuple(self._waiters.values()):
                if waiter.state is ProbeRequestState.QUEUED:
                    self._make_indeterminate_locked(
                        waiter,
                        ProbeIndeterminateReason.BROKER_STOPPED,
                        now=now,
                    )
                    continue
                if (
                    cancel_active
                    and waiter.state is ProbeRequestState.GRANTED
                    and waiter.cancellation_event is not None
                ):
                    waiter.cancellation_event.set()
                    if not waiter.claimed:
                        self._release_unclaimed_locked(
                            waiter,
                            ProbeIndeterminateReason.BROKER_STOPPED,
                            now=now,
                        )
            self._condition.notify_all()

            contexts = tuple(self._active_contexts.values())

        if not cancel_active:
            return

        abort_threads = self._abort_contexts(contexts)
        deadline = time.monotonic() + _STOP_ABORT_GRACE_SECONDS
        with self._condition:
            while self._active_contexts:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    break
                self._condition.wait(remaining)
            contexts_exited = not self._active_contexts

        callbacks_exited = _join_threads_until(abort_threads, deadline)
        if not contexts_exited or not callbacks_exited:
            self._mark_poisoned(
                "probe worker or abort callback did not exit while stopping"
            )

    close = stop

    def wait_until_idle(self, timeout_seconds: float | None = None) -> bool:
        timeout = _normalise_timeout(timeout_seconds)
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while self._active_socket_slots:
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return False
                self._condition.wait(remaining)
            return True

    def snapshot(self) -> ProbeBrokerSnapshot:
        with self._condition:
            queued = {priority: 0 for priority in ProbePriority}
            for waiter in self._waiters.values():
                if waiter.state is ProbeRequestState.QUEUED:
                    queued[waiter.priority] += 1
            return ProbeBrokerSnapshot(
                max_active_sockets=self._capacity,
                active_socket_slots=self._active_socket_slots,
                queued_runtime_manual=queued[ProbePriority.RUNTIME_MANUAL],
                queued_activation=queued[ProbePriority.ACTIVATION],
                queued_hot_standby=queued[ProbePriority.HOT_STANDBY],
                queued_background=queued[ProbePriority.BACKGROUND],
                stopped=self._stopped,
                poisoned=self._poisoned,
                poison_reason=self._poison_reason,
            )

    def _wait_for_workers(
        self,
        threads: tuple[threading.Thread, ...],
        *,
        execution_deadline_monotonic: float | None,
        cancel_event: threading.Event | None,
        ticket: ProbeTicket,
    ) -> ProbeIndeterminateReason | None:
        """Wait using bounded joins so stop/cancel/deadline always stay observable."""

        while any(thread.is_alive() for thread in threads):
            with self._condition:
                stopped = self._stopped
                poisoned = self._poisoned
            if poisoned:
                return ProbeIndeterminateReason.BROKER_POISONED
            if stopped:
                return ProbeIndeterminateReason.BROKER_STOPPED
            if cancel_event is not None and cancel_event.is_set():
                return ProbeIndeterminateReason.CANCELLED
            if ticket.cancelled:
                return ProbeIndeterminateReason.CANCELLED
            wait_seconds = _WORKER_CONTROL_POLL_SECONDS
            if execution_deadline_monotonic is not None:
                remaining = execution_deadline_monotonic - time.monotonic()
                if remaining <= 0.0:
                    return ProbeIndeterminateReason.EXECUTION_TIMEOUT
                wait_seconds = min(wait_seconds, remaining)
            for thread in threads:
                if thread.is_alive():
                    thread.join(wait_seconds)
                    break
        return None

    def _abort_contexts(
        self,
        contexts: tuple[ProbeExecutionContext, ...],
    ) -> tuple[threading.Thread, ...]:
        threads: list[threading.Thread] = []
        for context in contexts:
            thread = context._schedule_abort()
            if thread is not None:
                threads.append(thread)
        return tuple(threads)

    def _wait_for_abort(
        self,
        workers: tuple[threading.Thread, ...],
        abort_threads: tuple[threading.Thread, ...],
        grace_seconds: float,
    ) -> bool:
        deadline = time.monotonic() + grace_seconds
        workers_exited = _join_threads_until(workers, deadline)
        callbacks_exited = _join_threads_until(abort_threads, deadline)
        return workers_exited and callbacks_exited

    def _release_slots_after_workers(
        self,
        ticket: ProbeTicket,
        indexed_workers: tuple[tuple[int, threading.Thread], ...],
    ) -> None:
        """Release only slots whose corresponding real worker has exited."""

        workers_by_slot = dict(indexed_workers)
        lingering: list[tuple[int, threading.Thread]] = []
        for slot_index in range(ticket.slots):
            worker = workers_by_slot.get(slot_index)
            if worker is None or not worker.is_alive():
                ticket.release_slot(slot_index)
            else:
                lingering.append((slot_index, worker))

        if not lingering:
            return

        def release_when_exited() -> None:
            for slot_index, worker in lingering:
                while not _join_threads_until(
                    (worker,),
                    time.monotonic() + _CANCELLATION_POLL_SECONDS,
                ):
                    pass
                ticket.release_slot(slot_index)

        threading.Thread(
            target=release_when_exited,
            name=f"runtime-probe-release-{ticket.request_id}",
            daemon=True,
        ).start()

    def _register_execution_context(self, context: ProbeExecutionContext) -> None:
        with self._condition:
            self._active_contexts[id(context)] = context
            self._condition.notify_all()

    def _unregister_execution_context(self, context: ProbeExecutionContext) -> None:
        with self._condition:
            self._active_contexts.pop(id(context), None)
            self._condition.notify_all()

    def _mark_poisoned(self, reason: str) -> None:
        with self._condition:
            if not self._poisoned:
                self._poisoned = True
                self._poison_reason = str(reason)
            now = self._now()
            for waiter in tuple(self._waiters.values()):
                if waiter.state is ProbeRequestState.QUEUED:
                    self._make_indeterminate_locked(
                        waiter,
                        ProbeIndeterminateReason.BROKER_POISONED,
                        now=now,
                    )
                elif (
                    waiter.state is ProbeRequestState.GRANTED
                    and waiter.cancellation_event is not None
                ):
                    waiter.cancellation_event.set()
                    if not waiter.claimed:
                        self._release_unclaimed_locked(
                            waiter,
                            ProbeIndeterminateReason.BROKER_POISONED,
                            now=now,
                        )
            contexts = tuple(self._active_contexts.values())
            self._condition.notify_all()

        self._abort_contexts(contexts)

    def _now(self) -> float:
        value = float(self._clock())
        if not math.isfinite(value):
            raise ValueError("probe broker clock must return a finite value")
        return value

    def _request_state(self, waiter: _Waiter) -> ProbeRequestState:
        with self._condition:
            return waiter.state

    def _wait_for_request(
        self,
        waiter: _Waiter,
        *,
        timeout_seconds: float | None,
        cancel_event: threading.Event | None,
    ) -> ProbeAdmission:
        timeout = _normalise_timeout(timeout_seconds)
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while True:
                if waiter.state is ProbeRequestState.GRANTED:
                    if waiter.claimed:
                        raise RuntimeError("probe request ticket was already claimed")
                    waiter.claimed = True
                    return self._admission_locked(waiter)
                if waiter.state is ProbeRequestState.INDETERMINATE:
                    admission = self._admission_locked(waiter)
                    self._waiters.pop(waiter.request_id, None)
                    return admission
                if waiter.state is ProbeRequestState.RELEASED:
                    raise RuntimeError("probe request ticket was already released")

                if cancel_event is not None and cancel_event.is_set():
                    self._make_indeterminate_locked(
                        waiter,
                        ProbeIndeterminateReason.CANCELLED,
                    )
                    self._dispatch_locked()
                    continue

                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0.0:
                        self._make_indeterminate_locked(
                            waiter,
                            ProbeIndeterminateReason.QUEUE_TIMEOUT,
                        )
                        self._dispatch_locked()
                        continue
                    wait_seconds = min(remaining, _CANCELLATION_POLL_SECONDS)
                elif cancel_event is not None:
                    wait_seconds = _CANCELLATION_POLL_SECONDS
                else:
                    wait_seconds = None
                self._condition.wait(wait_seconds)

    def _cancel_request(self, waiter: _Waiter) -> bool:
        with self._condition:
            if waiter.state is ProbeRequestState.QUEUED:
                self._make_indeterminate_locked(
                    waiter,
                    ProbeIndeterminateReason.CANCELLED,
                )
                self._dispatch_locked()
                return True
            if waiter.state is ProbeRequestState.GRANTED:
                assert waiter.cancellation_event is not None
                waiter.cancellation_event.set()
                if not waiter.claimed:
                    self._release_unclaimed_locked(
                        waiter,
                        ProbeIndeterminateReason.CANCELLED,
                    )
                    self._dispatch_locked()
                self._condition.notify_all()
                return True
            return False

    def _signal_ticket_cancel(self, waiter: _Waiter) -> bool:
        with self._condition:
            if waiter.state is not ProbeRequestState.GRANTED:
                return False
            assert waiter.cancellation_event is not None
            waiter.cancellation_event.set()
            self._condition.notify_all()
            return True

    def _release_ticket_slots(
        self,
        waiter: _Waiter,
        released_slots: int,
        *,
        fully_released: bool,
    ) -> None:
        with self._condition:
            if waiter.state is not ProbeRequestState.GRANTED:
                return
            self._active_socket_slots -= int(released_slots)
            if self._active_socket_slots < 0:
                raise RuntimeError("probe broker active-slot accounting underflow")
            if fully_released:
                waiter.state = ProbeRequestState.RELEASED
                waiter.terminal_at_monotonic = self._now()
                self._waiters.pop(waiter.request_id, None)
            self._dispatch_locked()
            self._condition.notify_all()

    def _release_unclaimed_locked(
        self,
        waiter: _Waiter,
        reason: ProbeIndeterminateReason,
        *,
        now: float | None = None,
    ) -> None:
        if waiter.state is not ProbeRequestState.GRANTED or waiter.claimed:
            return
        self._active_socket_slots -= waiter.slots
        if self._active_socket_slots < 0:
            raise RuntimeError("probe broker active-slot accounting underflow")
        waiter.state = ProbeRequestState.INDETERMINATE
        waiter.indeterminate_reason = reason
        waiter.terminal_at_monotonic = self._now() if now is None else now
        waiter.ticket = None
        self._waiters.pop(waiter.request_id, None)

    def _dispatch_locked(self) -> None:
        while not self._stopped and not self._poisoned:
            while self._queue and self._queue[0][2].state is not ProbeRequestState.QUEUED:
                heapq.heappop(self._queue)
            if not self._queue:
                return
            _, _, waiter = self._queue[0]
            available = self._capacity - self._active_socket_slots
            # Do not bypass a head pair when only one slot is free.  This is
            # the atomicity/fairness rule that prevents a stream of singles
            # from starving a high-priority pair.
            if waiter.slots > available:
                return
            heapq.heappop(self._queue)
            waiter.state = ProbeRequestState.GRANTED
            waiter.granted_at_monotonic = self._now()
            waiter.ticket = ProbeTicket(self, waiter)
            self._active_socket_slots += waiter.slots
            self._condition.notify_all()

    def _make_indeterminate_locked(
        self,
        waiter: _Waiter,
        reason: ProbeIndeterminateReason,
        *,
        now: float | None = None,
    ) -> None:
        if waiter.state is not ProbeRequestState.QUEUED:
            return
        waiter.state = ProbeRequestState.INDETERMINATE
        waiter.indeterminate_reason = reason
        waiter.terminal_at_monotonic = self._now() if now is None else now
        waiter.ticket = None
        self._waiters.pop(waiter.request_id, None)
        self._condition.notify_all()

    def _admission_locked(self, waiter: _Waiter) -> ProbeAdmission:
        completed = (
            waiter.granted_at_monotonic
            if waiter.state is ProbeRequestState.GRANTED
            else waiter.terminal_at_monotonic
        )
        if completed is None:
            completed = self._now()
        granted = waiter.granted_at_monotonic
        queue_completed = granted if granted is not None else completed
        timing = ProbeTiming(
            queued_at_monotonic=waiter.enqueued_at_monotonic,
            granted_at_monotonic=granted,
            socket_started_at_monotonic=None,
            completed_at_monotonic=completed,
            queue_wait_seconds=_elapsed(
                waiter.enqueued_at_monotonic,
                queue_completed,
            ),
            socket_elapsed_seconds=None,
        )
        return ProbeAdmission(
            priority=waiter.priority,
            slots=waiter.slots,
            timing=timing,
            ticket=waiter.ticket,
            indeterminate_reason=waiter.indeterminate_reason,
        )

    def _execute_probe(
        self,
        context: ProbeExecutionContext,
        probe: Callable[[ProbeExecutionContext], T],
        *,
        admission: ProbeAdmission,
    ) -> ProbeExecutionResult[T]:
        value: T | None = None
        error: Exception | None = None
        self._register_execution_context(context)
        try:
            if context.cancelled:
                return self._execution_result(
                    admission,
                    completed=self._now(),
                    socket_started=None,
                    reason=self._cancellation_reason(context),
                )
            try:
                value = probe(context)
            except Exception as exc:  # Adapter owns transport classification.
                error = exc
            completed = self._now()
            socket_started = context.socket_started_at_monotonic

            if context.contract_violated or self.poisoned:
                reason = ProbeIndeterminateReason.BROKER_POISONED
            elif context.execution_timed_out:
                reason = ProbeIndeterminateReason.EXECUTION_TIMEOUT
            elif context.cancelled:
                reason = self._cancellation_reason(context)
            else:
                reason = None
        finally:
            self._unregister_execution_context(context)

        if reason is not None:
            return self._execution_result(
                admission,
                completed=completed,
                socket_started=socket_started,
                reason=reason,
            )
        if socket_started is None:
            return self._execution_result(
                admission,
                completed=completed,
                socket_started=None,
                reason=(
                    ProbeIndeterminateReason.STARTED_TIMEOUT
                    if context.started_timeout
                    else ProbeIndeterminateReason.SOCKET_NOT_STARTED
                ),
            )
        return self._execution_result(
            admission,
            completed=completed,
            socket_started=socket_started,
            value=value,
            error=error,
        )

    def _cancellation_reason(
        self,
        context: ProbeExecutionContext,
    ) -> ProbeIndeterminateReason:
        if context.contract_violated or self.poisoned:
            return ProbeIndeterminateReason.BROKER_POISONED
        if context.execution_timed_out:
            return ProbeIndeterminateReason.EXECUTION_TIMEOUT
        if self.stopped:
            return ProbeIndeterminateReason.BROKER_STOPPED
        return ProbeIndeterminateReason.CANCELLED

    def _execution_result(
        self,
        admission: ProbeAdmission,
        *,
        completed: float,
        socket_started: float | None,
        reason: ProbeIndeterminateReason | None = None,
        value: T | None = None,
        error: Exception | None = None,
    ) -> ProbeExecutionResult[T]:
        timing = ProbeTiming(
            queued_at_monotonic=admission.timing.queued_at_monotonic,
            granted_at_monotonic=admission.timing.granted_at_monotonic,
            socket_started_at_monotonic=socket_started,
            completed_at_monotonic=completed,
            queue_wait_seconds=admission.timing.queue_wait_seconds,
            socket_elapsed_seconds=(
                None if socket_started is None else _elapsed(socket_started, completed)
            ),
        )
        if reason is not None:
            return ProbeExecutionResult(
                disposition=ProbeDisposition.INDETERMINATE,
                timing=timing,
                indeterminate_reason=reason,
            )
        return ProbeExecutionResult(
            disposition=ProbeDisposition.EXECUTED,
            timing=timing,
            value=value,
            error=error,
        )

    def _indeterminate_after_grant(
        self,
        admission: ProbeAdmission,
        reason: ProbeIndeterminateReason,
        *,
        socket_started: float | None = None,
    ) -> ProbeExecutionResult[T]:
        return self._execution_result(
            admission,
            completed=self._now(),
            socket_started=socket_started,
            reason=reason,
        )


def _coerce_priority(value: ProbePriority | int) -> ProbePriority:
    if isinstance(value, ProbePriority):
        return value
    try:
        return ProbePriority(int(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid probe priority") from exc


def _normalise_timeout(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout must be a finite non-negative number") from exc
    if not math.isfinite(timeout) or timeout < 0.0:
        raise ValueError("timeout must be a finite non-negative number")
    return timeout


def _normalise_deadline(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        deadline = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("execution deadline must be a finite monotonic value") from exc
    if not math.isfinite(deadline):
        raise ValueError("execution deadline must be a finite monotonic value")
    return deadline


def _queue_timeout_for_deadline(
    queue_timeout_seconds: float | None,
    execution_deadline_monotonic: float | None,
) -> tuple[float | None, bool]:
    queue_timeout = _normalise_timeout(queue_timeout_seconds)
    if execution_deadline_monotonic is None:
        return queue_timeout, False
    remaining = max(0.0, execution_deadline_monotonic - time.monotonic())
    if queue_timeout is None or remaining <= queue_timeout:
        return remaining, True
    return queue_timeout, False


def _elapsed(start: float, end: float) -> float:
    return max(0.0, float(end) - float(start))


def _indeterminate_from_admission(
    admission: ProbeAdmission,
) -> ProbeExecutionResult[T]:
    reason = admission.indeterminate_reason
    if reason is None:
        reason = ProbeIndeterminateReason.SOCKET_NOT_STARTED
    return ProbeExecutionResult(
        disposition=ProbeDisposition.INDETERMINATE,
        timing=admission.timing,
        indeterminate_reason=reason,
    )


def _with_indeterminate_reason(
    result: ProbeExecutionResult[T],
    reason: ProbeIndeterminateReason,
) -> ProbeExecutionResult[T]:
    return ProbeExecutionResult(
        disposition=ProbeDisposition.INDETERMINATE,
        timing=result.timing,
        indeterminate_reason=reason,
    )


def _join_threads_until(
    threads: tuple[threading.Thread, ...],
    deadline_monotonic: float,
) -> bool:
    for thread in threads:
        while thread.is_alive():
            remaining = deadline_monotonic - time.monotonic()
            if remaining <= 0.0:
                return False
            thread.join(min(remaining, _WORKER_CONTROL_POLL_SECONDS))
    return True


# DesktopRuntime should share this process-wide instance across runtime/manual,
# activation, hot-standby, and background adapters.  Tests should instantiate a
# fresh RuntimeProbeBroker so stopping one test broker is isolated.
GLOBAL_RUNTIME_PROBE_BROKER = RuntimeProbeBroker()


def get_global_runtime_probe_broker() -> RuntimeProbeBroker:
    return GLOBAL_RUNTIME_PROBE_BROKER
