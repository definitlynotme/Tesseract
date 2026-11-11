from __future__ import annotations

import queue
import math
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field, replace
from typing import Callable, Deque, Mapping

from tier3_endpoint_controller import (
    EndpointWorkDecision,
    Tier3EndpointController,
)

from tier3_validation_protocol import (
    CONFIRM_HTTPS_CENSORED_REASON,
    CONFIRM_HTTPS_FAILED_REASON,
    AdmissionCommitted,
    AdmissionIntent,
    AdmissionRejected,
    CandidateEnvelope,
    EndpointAttemptKind,
    EndpointAttemptResult,
    ProbeAttemptFact,
    Tier3DeepRetentionDecision,
    Tier3ValidationPurpose,
    Tier3ValidatorCapacityLease,
    Tier3TerminalKind,
    ValidateEndpointCmd,
    ValidationStage,
    ValidationTerminal,
    is_confirm_https_fact,
)


# The FAST lane is a triage budget, not a verdict on the node.  Measured on
# real candidates through the packaged Core: a candidate with no prior PASS
# answers well inside three seconds or never answers at all (identical results
# at 3s, 6s and 12s), so spending longer on it buys nothing and costs the lane
# its turnover.  A candidate that has passed before is different: those answer
# at four to six seconds on a slow path and a three-second budget discards them.
# The budget is therefore constant for unknown candidates and longer only for
# the replay lanes, where the extra time is known to convert.
_FAST_SOFT_ENTITLEMENT_DEFAULT_SECONDS = 3.0


def _wilson_lower(successes: int, attempts: int, z: float = 1.96) -> float:
    if attempts <= 0:
        return 0.0
    n = float(attempts)
    p = max(0.0, min(1.0, float(successes) / n))
    denominator = 1.0 + (z * z) / n
    center = p + (z * z) / (2.0 * n)
    radius = z * math.sqrt((p * (1.0 - p) + (z * z) / (4.0 * n)) / n)
    return max(0.0, (center - radius) / denominator)


def _wilson_upper(successes: int, attempts: int, z: float = 1.96) -> float:
    if attempts <= 0:
        return 1.0
    n = float(attempts)
    p = max(0.0, min(1.0, float(successes) / n))
    denominator = 1.0 + (z * z) / n
    center = p + (z * z) / (2.0 * n)
    radius = z * math.sqrt((p * (1.0 - p) + (z * z) / (4.0 * n)) / n)
    return min(1.0, (center + radius) / denominator)


def _pass_throughput_lcb(
    successes: int,
    completions: int,
    occupied_ms: int,
) -> float:
    """Conservative admissions per busy worker-second for one stage."""
    if completions <= 0 or occupied_ms <= 0:
        return 0.0
    mean_worker_seconds = (float(occupied_ms) / 1000.0) / float(completions)
    return _wilson_lower(successes, completions) / max(0.001, mean_worker_seconds)


def _pass_throughput_ucb(
    successes: int,
    completions: int,
    occupied_ms: int,
) -> float:
    """Optimistic admissions per busy worker-second for dominance checks."""
    if completions <= 0 or occupied_ms <= 0:
        return float("inf")
    mean_worker_seconds = (float(occupied_ms) / 1000.0) / float(completions)
    return _wilson_upper(successes, completions) / max(0.001, mean_worker_seconds)


@dataclass(frozen=True, slots=True)
class ExpansionActorSnapshot:
    refresh_epoch: str
    inbox_depth: int
    submitted: int
    dispatched: int
    queued: int
    leased: int
    active: int
    completed: int
    cancelled_before_dispatch: int
    terminal: int
    passed: int
    candidate_failed: int
    censored: int
    facility_failed: int
    cancelled: int
    admission_intents: int
    admission_committed: int
    admission_rejected: int
    outbox_depth: int
    available_capacity: int
    sealed: bool
    conservation_ok: bool
    first_dispatch_monotonic_ns: int
    first_terminal_monotonic_ns: int
    candidate_inbox_depth: int = 0
    queued_fast: int = 0
    ready_fast: int = 0
    parked_fast: int = 0
    active_fast: int = 0
    queued_deep: int = 0
    active_deep: int = 0
    slice_dispatched: int = 0
    slice_completed: int = 0
    slice_cancelled: int = 0
    fast_completed: int = 0
    fast_passed: int = 0
    fast_continued: int = 0
    deep_completed: int = 0
    deep_passed: int = 0
    worker_envelope: int = 0
    first_fast_dispatch_monotonic_ns: int = 0
    first_deep_dispatch_monotonic_ns: int = 0
    first_fast_pass_monotonic_ns: int = 0
    first_deep_pass_monotonic_ns: int = 0
    release_epoch: int = 0
    fairness_violation_count: int = 0
    max_newcomer_wait_epochs: int = 0
    fast_slots: int = 0
    deep_slots: int = 0
    allocation_reason: str = ""
    desired_fast_slots: int = 0
    desired_deep_slots: int = 0
    borrowed_fast_slots: int = 0
    borrowed_deep_slots: int = 0
    fast_occupied_ms: int = 0
    deep_occupied_ms: int = 0
    deep_full_sample_count: int = 0
    net_new_admission_committed: int = 0
    deep_ready: int = 0
    deep_parked: int = 0
    deep_cohort_limit: int = 0
    blocked_escape_deep_priority_count: int = 0
    controller_decision_count: int = 0
    controller_dispatch_mismatch_count: int = 0
    actor_owner_limit: int = 0
    actor_owner_used: int = 0
    ready_work_limit: int = 0
    ready_work_used: int = 0
    unique_fast_candidates_10s: int = 0
    fast_rolling_completions: int = 0
    fast_rolling_terminals: int = 0
    fast_rolling_net_new: int = 0
    fast_rolling_occupied_ms: int = 0
    fast_rolling_yield_lcb: float = 0.0
    deep_rolling_completions: int = 0
    deep_rolling_terminals: int = 0
    deep_rolling_net_new: int = 0
    deep_rolling_occupied_ms: int = 0
    deep_rolling_yield_lcb: float = 0.0
    fast_soft_entitlement_seconds: float = 3.0
    external_fresh_backlog: int = 0
    expected_fast_supply: bool = False
    fast_supply_state: str = "exhausted"
    actionable_deep: int = 0
    normal_reservable_fast_count: int = 0
    reward_reservable_fast_count: int = 0
    escape_eligible_bucket_count: int = 0
    tcp_handoff_lease_count: int = 0
    expected_responsive_arrivals: int = 0
    source_retry_due_count: int = 0
    capacity_reservation_rejected_count: int = 0
    deep_retention_limit: int = 0
    deep_retention_used: int = 0
    deep_retention_overflow_censored: int = 0
    deep_retention_fair_replaced: int = 0
    deep_zero_yield_latched: bool = False
    deep_zero_yield_latched_monotonic_ns: int = 0
    deep_low_yield_evidence_streak: int = 0
    deep_probation_active: bool = False
    deep_recovery_level: int = 0
    deep_probation_completion_delta: int = 0
    deep_probation_net_new_delta: int = 0
    fast_observation_count: int = 0
    fast_observation_publish_failure_count: int = 0


@dataclass(slots=True)
class _FamilyQueue:
    candidates: Deque[str]
    continuations: Deque[str]
    proven_continuations: Deque[str]
    trusted_continuations: Deque[str]

    @classmethod
    def empty(cls) -> "_FamilyQueue":
        return cls(
            candidates=deque(),
            continuations=deque(),
            proven_continuations=deque(),
            trusted_continuations=deque(),
        )

    def has_candidates(self) -> bool:
        return bool(
            self.trusted_continuations
            or self.proven_continuations
            or self.continuations
            or self.candidates
        )

    def append(
        self,
        digest: str,
        *,
        continuation: bool,
        priority: bool = False,
        priority_tier: int = 0,
    ) -> None:
        if continuation and int(priority_tier) >= 2:
            target = self.trusted_continuations
        elif continuation and int(priority_tier) >= 1:
            target = self.proven_continuations
        else:
            target = self.continuations if continuation else self.candidates
        if priority:
            target.appendleft(digest)
        else:
            target.append(digest)

    def popleft(self) -> str | None:
        if self.trusted_continuations:
            return self.trusted_continuations.popleft()
        if self.proven_continuations:
            return self.proven_continuations.popleft()
        if self.continuations:
            return self.continuations.popleft()
        if self.candidates:
            return self.candidates.popleft()
        return None

    def remove(self, digest: str) -> bool:
        for candidates in (
            self.trusted_continuations,
            self.proven_continuations,
            self.continuations,
            self.candidates,
        ):
            try:
                candidates.remove(digest)
            except ValueError:
                continue
            return True
        return False


@dataclass(slots=True)
class _CandidateState:
    envelope: CandidateEnvelope
    transaction_id: str
    state: str = "parked_fast"
    lease_id: str = ""
    sequence: int = 0
    slice_id: int = 0
    active_stage: ValidationStage | None = None
    attempt_facts: tuple[ProbeAttemptFact, ...] = ()
    endpoints: tuple[str, ...] = ()
    fast_endpoint: str = ""
    canonical_pending: Deque[str] = field(default_factory=deque)
    endpoint_outcomes: dict[str, EndpointAttemptKind] = field(default_factory=dict)
    active_endpoint: str = ""
    facility_redispatch_used: bool = False
    terminal: ValidationTerminal | None = None
    admission_state: str = "none"
    deep_queued_monotonic_ns: int = 0
    active_decision: EndpointWorkDecision | None = None
    deep_dispatch_count: int = 0
    deep_retained: bool = False


@dataclass(slots=True)
class _StageRollingSample:
    completed_at: float
    candidate_digest: str
    stage: ValidationStage
    occupied_ms: int
    passed: bool = False
    terminal: bool = False
    net_new: bool = False
    facility_failed: bool = False
    pass_elapsed_ms: int = 0
    probe_elapsed_ms: int = 0
    right_censored: bool = False
    transport_completed: bool = False


class Tier3ExpansionActor:
    """Single-thread owner of staged validation transactions and admissions."""

    # Class default so partially built doubles keep the bare hard cap.
    _confirm_budget_seconds: float = 0.0

    def __init__(
        self,
        refresh_epoch: str,
        *,
        dispatch_sink: Callable[[ValidateEndpointCmd], bool | None],
        admission_sink: Callable[[AdmissionIntent], bool],
        terminal_sink: queue.Queue[ValidationTerminal],
        initial_worker_envelope: int = 4,
        transaction_released_sink: Callable[[], None] | None = None,
        continuation_sink: (
            Callable[
                [str, Tier3DeepRetentionDecision, EndpointAttemptResult],
                None,
            ]
            | None
        ) = None,
        fast_observation_sink: (
            Callable[[EndpointAttemptResult], None] | None
        ) = None,
        endpoint_controller: Tier3EndpointController | None = None,
        capacity_reservation_sink: (
            Callable[[], Tier3ValidatorCapacityLease | None] | None
        ) = None,
        capacity_release_sink: (
            Callable[[Tier3ValidatorCapacityLease], None] | None
        ) = None,
        confirm_budget_seconds: float = 0.0,
    ) -> None:
        self.refresh_epoch = str(refresh_epoch)
        # Worst-case mainstream HTTPS confirm time a passing slice adds; the
        # lease hard cap (and the supervisor guard it feeds) must cover it.
        self._confirm_budget_seconds = max(0.0, float(confirm_budget_seconds))
        self.dispatch_sink = dispatch_sink
        self.admission_sink = admission_sink
        self.terminal_sink = terminal_sink
        self.transaction_released_sink = transaction_released_sink
        self.continuation_sink = continuation_sink
        self.fast_observation_sink = fast_observation_sink
        self._endpoint_controller = endpoint_controller
        self._capacity_reservation_sink = capacity_reservation_sink
        self._capacity_release_sink = capacity_release_sink
        self._events: queue.PriorityQueue[tuple[int, int, str, object]] = (
            queue.PriorityQueue()
        )
        self._lock = threading.Lock()
        self._ingress_lock = threading.Lock()
        self._event_sequence = 0
        self._inbox_depth = 0
        self._candidate_inbox_depth = 0
        self._accepting = threading.Event()
        self._accepting.set()
        self._accepted_candidate_digests: set[str] = set()
        self._owner_live_digests: set[str] = set()
        self._owner_reserved_count = 0
        self._states: dict[str, _CandidateState] = {}
        self._root_families: dict[
            ValidationStage,
            OrderedDict[str, OrderedDict[str, _FamilyQueue]],
        ] = {
            ValidationStage.FAST_PROBE: OrderedDict(),
            ValidationStage.CANONICAL_DEEP: OrderedDict(),
        }
        self._fast_parked_families: OrderedDict[
            str, OrderedDict[str, _FamilyQueue]
        ] = OrderedDict()
        self._fast_parked_root_cursor: Deque[str] = deque()
        self._fast_parked_family_cursor: dict[str, Deque[str]] = {}
        self._deep_parked_families: OrderedDict[
            str, OrderedDict[str, _FamilyQueue]
        ] = OrderedDict()
        self._deep_parked_root_cursor: Deque[str] = deque()
        self._deep_parked_family_cursor: dict[str, Deque[str]] = {}
        self._deep_escape_parked: Deque[str] = deque()
        self._deep_retained_count = 0
        self._deep_retained_by_bucket: dict[tuple[str, str], int] = {}
        self._deep_retention_overflow_censored_count = 0
        self._deep_retention_fair_replaced_count = 0
        self._root_cursor: dict[ValidationStage, Deque[str]] = {
            ValidationStage.FAST_PROBE: deque(),
            ValidationStage.CANONICAL_DEEP: deque(),
        }
        self._family_cursor: dict[ValidationStage, dict[str, Deque[str]]] = {
            ValidationStage.FAST_PROBE: {},
            ValidationStage.CANONICAL_DEEP: {},
        }
        self._active_stage_families: dict[
            ValidationStage, dict[tuple[str, str], int]
        ] = {
            ValidationStage.FAST_PROBE: {},
            ValidationStage.CANONICAL_DEEP: {},
        }
        self._release_epoch = 0
        self._release_credit = 0
        self._stage_release_epoch: dict[ValidationStage, int] = {
            ValidationStage.FAST_PROBE: 0,
            ValidationStage.CANONICAL_DEEP: 0,
        }
        self._stage_release_credit: dict[ValidationStage, int] = {
            ValidationStage.FAST_PROBE: 0,
            ValidationStage.CANONICAL_DEEP: 0,
        }
        self._newcomer_enqueued_epoch: dict[
            tuple[ValidationStage, str, str], int
        ] = {}
        self._newcomer_dispatched: set[tuple[ValidationStage, str, str]] = set()
        self._stage_seen_roots: dict[ValidationStage, set[str]] = {
            ValidationStage.FAST_PROBE: set(),
            ValidationStage.CANONICAL_DEEP: set(),
        }
        self._fairness_violation_count = 0
        self._max_newcomer_wait_epochs = 0
        self._outbox: Deque[AdmissionIntent] = deque()
        self._available_capacity = 0
        self._capacity_reservation_rejected_count = 0
        self._worker_envelope = max(1, int(initial_worker_envelope))
        self._control_pressure = 1.0
        self._external_fresh_backlog = 0
        self._expected_fast_supply = False
        self._fast_supply_state = "exhausted"
        self._fast_supply_nonactionable_revisions = 0
        self._actionable_deep = 0
        self._normal_reservable_fast_count = 0
        self._reward_reservable_fast_count = 0
        self._escape_eligible_bucket_count = 0
        self._tcp_handoff_lease_count = 0
        self._expected_responsive_arrivals = 0
        self._source_retry_due_count = 0
        self._dispatch_enabled = True
        self._fast_completions_since_deep = 0
        self._actor_owner_limit = min(
            256,
            max(64, 10 * self._worker_envelope),
        )
        self._ready_work_limit = max(8, 2 * self._worker_envelope)
        self._sequence = 0
        self._sealed = False
        self._stopping = False
        self._thread: threading.Thread | None = None
        self._stopped = threading.Event()
        self._terminal_counts = {kind: 0 for kind in Tier3TerminalKind}
        self._submitted_count = 0
        self._terminal_state_count = 0
        self._queued_fast_count = 0
        self._fast_ready_count = 0
        self._fast_parked_count = 0
        self._active_fast_count = 0
        self._queued_deep_count = 0
        self._deep_ready_count = 0
        self._deep_parked_count = 0
        self._active_deep_count = 0
        self._admission_intents = 0
        self._admission_committed = 0
        self._net_new_admission_committed = 0
        self._admission_rejected = 0
        self._candidate_dispatched_count = 0
        self._candidate_completed_count = 0
        self._cancelled_before_dispatch_count = 0
        self._slice_dispatched_count = 0
        self._slice_completed_count = 0
        self._slice_cancelled_count = 0
        self._fast_completed_count = 0
        self._fast_observation_count = 0
        self._fast_observation_publish_failure_count = 0
        self._fast_passed_count = 0
        self._fast_continued_count = 0
        self._deep_completed_count = 0
        self._deep_passed_count = 0
        self._fast_net_new_count = 0
        self._deep_net_new_count = 0
        self._deep_zero_yield_latched = False
        self._deep_zero_yield_latch_net_new_count = 0
        self._deep_zero_yield_latched_monotonic_ns = 0
        self._deep_low_yield_latch_reason = ""
        self._deep_low_yield_evidence_streak = 0
        self._deep_low_yield_last_evaluated_at = 0.0
        self._deep_probation_active = False
        self._deep_recovery_level = 0
        self._deep_probation_started_at = 0.0
        self._deep_probation_start_completion_count = 0
        self._deep_probation_start_net_new_count = 0
        self._fast_occupied_ms = 0
        self._deep_occupied_ms = 0
        self._deep_full_sample_count = 0
        self._blocked_escape_deep_priority_count = 0
        self._controller_decision_count = 0
        self._controller_dispatch_mismatch_count = 0
        self._started_monotonic_ns = time.monotonic_ns()
        self._fast_candidates_first_10s: set[str] = set()
        self._fast_slot_limit, self._deep_slot_limit = (
            self._baseline_stage_slot_limits(self._worker_envelope)
        )
        self._allocation_reason = "endpoint_discovery_75_25"
        self._allocation_candidate_reason = self._allocation_reason
        self._allocation_candidate_streak = 0
        self._allocation_last_evaluated_at = time.monotonic()
        self._allocation_last_moved_at = 0.0
        self._rolling_stage_samples: Deque[_StageRollingSample] = deque(maxlen=512)
        self._latest_stage_sample_by_candidate: OrderedDict[
            tuple[str, ValidationStage],
            _StageRollingSample,
        ] = OrderedDict()
        self._fast_soft_entitlement_seconds = _FAST_SOFT_ENTITLEMENT_DEFAULT_SECONDS
        self._fast_soft_completion_counter = 0
        self._fast_soft_last_adjust_completion = 0
        self._fast_soft_reset_count = 0
        self._first_dispatch_monotonic_ns = 0
        self._first_terminal_monotonic_ns = 0
        self._first_fast_dispatch_monotonic_ns = 0
        self._first_deep_dispatch_monotonic_ns = 0
        self._first_fast_pass_monotonic_ns = 0
        self._first_deep_pass_monotonic_ns = 0
        self._snapshot_cache = self._build_snapshot()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="tier3-expansion-actor",
            daemon=False,
        )
        self._thread.start()

    def submit(self, envelope: CandidateEnvelope) -> bool:
        with self._ingress_lock:
            if not self._accepting.is_set():
                return False
            digest = str(envelope.candidate_digest)
            if digest in self._accepted_candidate_digests:
                return True
            if self._owner_reserved_count >= self._actor_owner_limit:
                return False
            self._accepted_candidate_digests.add(digest)
            self._owner_live_digests.add(digest)
            self._owner_reserved_count += 1
            try:
                self._enqueue_event("candidate", envelope)
            except BaseException:
                self._accepted_candidate_digests.discard(digest)
                self._owner_live_digests.discard(digest)
                self._owner_reserved_count = max(
                    0,
                    self._owner_reserved_count - 1,
                )
                raise
            return True

    def _try_acquire_owner_credit(self, candidate_digest: str) -> bool:
        digest = str(candidate_digest)
        with self._ingress_lock:
            if digest in self._owner_live_digests:
                return True
            if self._owner_reserved_count >= self._actor_owner_limit:
                return False
            self._owner_live_digests.add(digest)
            self._owner_reserved_count += 1
            return True

    def _owner_credit_available(self) -> bool:
        with self._ingress_lock:
            return self._owner_reserved_count < self._actor_owner_limit

    def _release_owner_credit(self, candidate_digest: str) -> None:
        digest = str(candidate_digest)
        released = False
        with self._ingress_lock:
            if digest not in self._owner_live_digests:
                return
            self._owner_live_digests.discard(digest)
            self._owner_reserved_count = max(0, self._owner_reserved_count - 1)
            released = True
        if released and self.transaction_released_sink is not None:
            try:
                self.transaction_released_sink()
            except Exception:
                pass

    def capacity_available(self, count: int = 1) -> None:
        if count:
            self._enqueue_event("capacity", int(count))

    def bind_capacity_reservation_owner(
        self,
        reserve: Callable[[], Tier3ValidatorCapacityLease | None],
        release: Callable[[Tier3ValidatorCapacityLease], None],
    ) -> None:
        if self._thread is not None:
            raise RuntimeError("capacity reservation owner must bind before actor start")
        self._capacity_reservation_sink = reserve
        self._capacity_release_sink = release

    def set_worker_envelope(self, count: int) -> None:
        self._enqueue_event("envelope", max(1, int(count)))

    def set_credit_limits(self, owner_limit: int, ready_work_limit: int) -> None:
        self._enqueue_event(
            "credits",
            (
                max(1, int(owner_limit)),
                max(1, int(ready_work_limit)),
            ),
        )

    def set_expansion_control(
        self,
        *,
        pressure: float,
        dispatch_enabled: bool,
        external_fresh_backlog: int = 0,
        expected_fast_supply: bool = False,
        fast_supply_state: str | None = None,
        actionable_deep: int = 0,
        normal_reservable_fast_count: int = 0,
        reward_reservable_fast_count: int = 0,
        escape_eligible_bucket_count: int = 0,
        tcp_handoff_lease_count: int = 0,
        expected_responsive_arrivals: int = 0,
        source_retry_due_count: int = 0,
    ) -> None:
        normalized_supply_state = str(fast_supply_state or "").strip().lower()
        if normalized_supply_state not in {"ready", "imminent", "blocked", "exhausted"}:
            normalized_supply_state = (
                "imminent" if bool(expected_fast_supply) else "exhausted"
            )
        self._enqueue_event(
            "control",
            (
                max(0.0, min(1.0, float(pressure))),
                bool(dispatch_enabled),
                max(0, int(external_fresh_backlog)),
                normalized_supply_state,
                max(0, int(actionable_deep)),
                max(0, int(normal_reservable_fast_count)),
                max(0, int(reward_reservable_fast_count)),
                max(0, int(escape_eligible_bucket_count)),
                max(0, int(tcp_handoff_lease_count)),
                max(0, int(expected_responsive_arrivals)),
                max(0, int(source_retry_due_count)),
            ),
        )

    def endpoint_result(self, result: EndpointAttemptResult) -> None:
        self._enqueue_event("attempt", result)

    def terminal(self, terminal: ValidationTerminal) -> None:
        self._enqueue_event("terminal", terminal)

    def admission_result(self, result: AdmissionCommitted | AdmissionRejected) -> None:
        self._enqueue_event("admission", result)

    def seal(self) -> None:
        with self._ingress_lock:
            self._accepting.clear()
            self._enqueue_event("seal", None)

    def stop(self, timeout_seconds: float = 2.0) -> bool:
        with self._ingress_lock:
            self._accepting.clear()
            self._enqueue_event("stop", None)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, float(timeout_seconds)))
            return not thread.is_alive()
        return True

    def backlog_depth(self) -> int:
        with self._lock:
            return self._inbox_depth + self._snapshot_cache.queued

    def active_depth(self) -> int:
        with self._lock:
            return self._snapshot_cache.active

    def pending_work(self) -> int:
        with self._lock:
            snapshot = self._snapshot_cache
            published_work = (
                self._inbox_depth
                + snapshot.queued
                + snapshot.active
                + snapshot.outbox_depth
            )
        # submit() reserves ownership before its candidate event is processed,
        # while the actor publishes the resulting queued/active snapshot only
        # after dispatch.  Quiescence readers must not see a false zero in that
        # transfer window or close a one-candidate validation session early.
        with self._ingress_lock:
            owner_work = self._owner_reserved_count
        return max(published_work, owner_work)

    def snapshot(self) -> ExpansionActorSnapshot:
        with self._lock:
            snapshot = self._snapshot_cache
            if (
                snapshot.inbox_depth != self._inbox_depth
                or snapshot.candidate_inbox_depth != self._candidate_inbox_depth
            ):
                return replace(
                    snapshot,
                    inbox_depth=self._inbox_depth,
                    candidate_inbox_depth=self._candidate_inbox_depth,
                )
            return snapshot

    def _build_snapshot(self) -> ExpansionActorSnapshot:
        fast_recent = self._rolling_stage_summary(ValidationStage.FAST_PROBE)
        deep_recent = self._rolling_stage_summary(ValidationStage.CANONICAL_DEEP)
        effective_fast_slots, effective_deep_slots = (
            self._effective_stage_slot_limits(
                self._fast_slot_limit,
                self._deep_slot_limit,
            )
        )
        submitted = self._submitted_count
        queued_fast = self._queued_fast_count
        active_fast = self._active_fast_count
        queued_deep = self._queued_deep_count
        active_deep = self._active_deep_count
        terminal = self._terminal_state_count
        queued = queued_fast + queued_deep
        active = active_fast + active_deep
        ready_work_used = (
            self._fast_ready_count
            + active_fast
            + self._deep_ready_count
            + active_deep
        )
        actor_owner_used = max(0, int(self._owner_reserved_count))
        candidate_conservation = submitted == queued + active + terminal
        dispatched_live = sum(
            1
            for state in self._states.values()
            if state.slice_id > 0 and state.state != "terminal"
        )
        dispatched_conservation = self._candidate_dispatched_count == (
            dispatched_live + self._candidate_completed_count
        )
        slice_conservation = self._slice_dispatched_count == (
            self._slice_completed_count + self._slice_cancelled_count + active
        )
        return ExpansionActorSnapshot(
            refresh_epoch=self.refresh_epoch,
            inbox_depth=self._inbox_depth,
            candidate_inbox_depth=self._candidate_inbox_depth,
            submitted=submitted,
            dispatched=self._candidate_dispatched_count,
            queued=queued,
            leased=active,
            active=active,
            completed=self._candidate_completed_count,
            cancelled_before_dispatch=self._cancelled_before_dispatch_count,
            terminal=terminal,
            passed=self._terminal_counts[Tier3TerminalKind.PASS],
            candidate_failed=self._terminal_counts[Tier3TerminalKind.CANDIDATE_FAIL],
            censored=self._terminal_counts[Tier3TerminalKind.CENSORED],
            facility_failed=self._terminal_counts[Tier3TerminalKind.FACILITY_FAIL],
            cancelled=self._terminal_counts[Tier3TerminalKind.CANCELLED],
            admission_intents=self._admission_intents,
            admission_committed=self._admission_committed,
            admission_rejected=self._admission_rejected,
            outbox_depth=len(self._outbox),
            available_capacity=self._available_capacity,
            sealed=self._sealed,
            conservation_ok=(
                candidate_conservation
                and dispatched_conservation
                and slice_conservation
                and queued_fast
                == self._fast_ready_count + self._fast_parked_count
                and queued_deep
                == self._deep_ready_count + self._deep_parked_count
                and ready_work_used <= self._ready_work_limit
                and actor_owner_used <= self._actor_owner_limit
                and terminal
                == self._candidate_completed_count + self._cancelled_before_dispatch_count
            ),
            first_dispatch_monotonic_ns=self._first_dispatch_monotonic_ns,
            first_terminal_monotonic_ns=self._first_terminal_monotonic_ns,
            queued_fast=queued_fast,
            ready_fast=self._fast_ready_count,
            parked_fast=self._fast_parked_count,
            active_fast=active_fast,
            queued_deep=queued_deep,
            active_deep=active_deep,
            slice_dispatched=self._slice_dispatched_count,
            slice_completed=self._slice_completed_count,
            slice_cancelled=self._slice_cancelled_count,
            fast_completed=self._fast_completed_count,
            fast_observation_count=self._fast_observation_count,
            fast_observation_publish_failure_count=(
                self._fast_observation_publish_failure_count
            ),
            fast_passed=self._fast_passed_count,
            fast_continued=self._fast_continued_count,
            deep_completed=self._deep_completed_count,
            deep_passed=self._deep_passed_count,
            worker_envelope=self._worker_envelope,
            first_fast_dispatch_monotonic_ns=self._first_fast_dispatch_monotonic_ns,
            first_deep_dispatch_monotonic_ns=self._first_deep_dispatch_monotonic_ns,
            first_fast_pass_monotonic_ns=self._first_fast_pass_monotonic_ns,
            first_deep_pass_monotonic_ns=self._first_deep_pass_monotonic_ns,
            release_epoch=self._release_epoch,
            fairness_violation_count=self._fairness_violation_count,
            max_newcomer_wait_epochs=self._max_newcomer_wait_epochs,
            fast_slots=effective_fast_slots,
            deep_slots=effective_deep_slots,
            allocation_reason=self._allocation_reason,
            desired_fast_slots=max(0, int(self._fast_slot_limit)),
            desired_deep_slots=max(0, int(self._deep_slot_limit)),
            borrowed_fast_slots=max(
                0,
                int(effective_fast_slots) - int(self._fast_slot_limit),
            ),
            borrowed_deep_slots=max(
                0,
                int(effective_deep_slots) - int(self._deep_slot_limit),
            ),
            fast_occupied_ms=self._fast_occupied_ms,
            deep_occupied_ms=self._deep_occupied_ms,
            deep_full_sample_count=self._deep_full_sample_count,
            net_new_admission_committed=self._net_new_admission_committed,
            deep_ready=self._deep_ready_count,
            deep_parked=self._deep_parked_count,
            deep_cohort_limit=self._deep_cohort_limit(),
            blocked_escape_deep_priority_count=(
                self._blocked_escape_deep_priority_count
            ),
            controller_decision_count=self._controller_decision_count,
            controller_dispatch_mismatch_count=(
                self._controller_dispatch_mismatch_count
            ),
            actor_owner_limit=self._actor_owner_limit,
            actor_owner_used=actor_owner_used,
            ready_work_limit=self._ready_work_limit,
            ready_work_used=ready_work_used,
            unique_fast_candidates_10s=len(
                self._fast_candidates_first_10s
            ),
            fast_rolling_completions=int(fast_recent["completions"]),
            fast_rolling_terminals=int(fast_recent["terminals"]),
            fast_rolling_net_new=int(fast_recent["net_new"]),
            fast_rolling_occupied_ms=int(fast_recent["occupied_ms"]),
            fast_rolling_yield_lcb=float(fast_recent["yield_lcb"]),
            deep_rolling_completions=int(deep_recent["completions"]),
            deep_rolling_terminals=int(deep_recent["terminals"]),
            deep_rolling_net_new=int(deep_recent["net_new"]),
            deep_rolling_occupied_ms=int(deep_recent["occupied_ms"]),
            deep_rolling_yield_lcb=float(deep_recent["yield_lcb"]),
            fast_soft_entitlement_seconds=float(
                self._fast_soft_entitlement_seconds
            ),
            external_fresh_backlog=int(self._external_fresh_backlog),
            expected_fast_supply=bool(self._expected_fast_supply),
            fast_supply_state=str(self._fast_supply_state),
            actionable_deep=int(self._actionable_deep),
            normal_reservable_fast_count=int(
                self._normal_reservable_fast_count
            ),
            reward_reservable_fast_count=int(
                self._reward_reservable_fast_count
            ),
            escape_eligible_bucket_count=int(
                self._escape_eligible_bucket_count
            ),
            tcp_handoff_lease_count=int(self._tcp_handoff_lease_count),
            expected_responsive_arrivals=int(
                self._expected_responsive_arrivals
            ),
            source_retry_due_count=int(self._source_retry_due_count),
            capacity_reservation_rejected_count=(
                self._capacity_reservation_rejected_count
            ),
            deep_retention_limit=self._deep_retention_limit(),
            deep_retention_used=max(0, int(self._deep_retained_count)),
            deep_retention_overflow_censored=(
                self._deep_retention_overflow_censored_count
            ),
            deep_retention_fair_replaced=(
                self._deep_retention_fair_replaced_count
            ),
            deep_zero_yield_latched=bool(self._deep_zero_yield_latched),
            deep_zero_yield_latched_monotonic_ns=int(
                self._deep_zero_yield_latched_monotonic_ns
            ),
            deep_low_yield_evidence_streak=int(
                self._deep_low_yield_evidence_streak
            ),
            deep_probation_active=bool(self._deep_probation_active),
            deep_recovery_level=max(0, int(self._deep_recovery_level)),
            deep_probation_completion_delta=max(
                0,
                int(self._deep_completed_count)
                - int(self._deep_probation_start_completion_count),
            ),
            deep_probation_net_new_delta=max(
                0,
                int(self._deep_net_new_count)
                - int(self._deep_probation_start_net_new_count),
            ),
        )

    def _publish_snapshot(self) -> None:
        with self._lock:
            self._snapshot_cache = self._build_snapshot()

    def _enqueue_event(self, kind: str, payload: object) -> None:
        with self._lock:
            self._inbox_depth += 1
            if kind == "candidate":
                self._candidate_inbox_depth += 1
            self._event_sequence += 1
            event_sequence = self._event_sequence
        priority = 10 if kind == "candidate" else 1
        if kind in {"seal", "stop"}:
            priority = 0
        try:
            self._events.put_nowait((priority, event_sequence, kind, payload))
        except BaseException:
            with self._lock:
                self._inbox_depth = max(0, self._inbox_depth - 1)
                if kind == "candidate":
                    self._candidate_inbox_depth = max(
                        0, self._candidate_inbox_depth - 1
                    )
            raise

    def _event_processed(self, kind: str) -> None:
        with self._lock:
            self._inbox_depth = max(0, self._inbox_depth - 1)
            if kind == "candidate":
                self._candidate_inbox_depth = max(0, self._candidate_inbox_depth - 1)

    def _drain_candidate_event_batch(self, limit: int = 63) -> None:
        """Admit a bounded ingress cohort before scheduling its first slot."""
        for _ in range(max(0, int(limit))):
            with self._events.mutex:
                if not self._events.queue or self._events.queue[0][2] != "candidate":
                    return
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                return
            _priority, _sequence, kind, payload = event
            if kind != "candidate":
                # A producer may insert a higher-priority seal/stop between
                # the heap peek above and this pop. Preserve that control
                # event for the main loop instead of interpreting its empty
                # payload as a candidate.
                self._events.put_nowait(event)
                return
            try:
                self._add_candidate(payload)  # type: ignore[arg-type]
            finally:
                self._event_processed(kind)

    @staticmethod
    def _state_for_stage(stage: ValidationStage, active: bool) -> str:
        suffix = "active" if active else "queued"
        return f"{suffix}_{'fast' if stage is ValidationStage.FAST_PROBE else 'deep'}"

    @staticmethod
    def _deep_continuation_priority(
        envelope: CandidateEnvelope,
        *,
        continuation: bool,
    ) -> int:
        if not continuation:
            return 0
        if envelope.purpose is Tier3ValidationPurpose.USER_POOL_HEALTH:
            return 2
        origin = str(envelope.dispatch_evidence_origin or "cold_unknown")
        if origin == "fresh_replay":
            return 2
        if origin == "cold_discovery":
            return 1
        return 0

    def _enqueue_stage(
        self,
        state: _CandidateState,
        stage: ValidationStage,
        *,
        continuation: bool = False,
    ) -> bool:
        if self._ready_work_used() >= self._ready_work_limit:
            if stage is ValidationStage.FAST_PROBE:
                self._park_fast(state)
            else:
                self._park_deep(state, continuation=continuation)
            return False
        if (
            stage is ValidationStage.CANONICAL_DEEP
            and self._deep_ready_count + self._active_deep_count
            >= self._deep_cohort_limit()
        ):
            self._park_deep(state, continuation=continuation)
            return False
        if not self._try_acquire_owner_credit(state.envelope.candidate_digest):
            if stage is ValidationStage.FAST_PROBE:
                self._park_fast(state)
            else:
                self._park_deep(state, continuation=continuation)
            return False
        state.state = self._state_for_stage(stage, False)
        root = state.envelope.source_root or "unknown"
        family = state.envelope.source_family or "unknown"
        roots = self._root_families[stage]
        families = roots.setdefault(root, OrderedDict())
        newcomer_key = (stage, root, family)
        # A completed endpoint may empty a DEEP ready bucket too. Returning
        # work must join the rotation, not claim newcomer priority repeatedly.
        root_is_new = root not in self._stage_seen_roots[stage]
        family_is_new = newcomer_key not in self._newcomer_enqueued_epoch
        self._stage_seen_roots[stage].add(root)
        family_queue = families.get(family)
        if family_queue is None:
            family_queue = _FamilyQueue.empty()
            families[family] = family_queue
        # Candidate quality and coarse-arm exploration are resolved before
        # TCP. Once responsive, preserve that ingress order within the fair
        # root/family bucket; coarse keys must not reshuffle validation work.
        family_queue.append(
            state.envelope.candidate_digest,
            continuation=(
                stage is ValidationStage.CANONICAL_DEEP and continuation
            ),
            priority=(
                state.envelope.purpose
                is Tier3ValidationPurpose.USER_POOL_HEALTH
                or (
                    stage is ValidationStage.CANONICAL_DEEP
                    and state.envelope.dispatch_selection_reason
                    == "credit_escape"
                )
            ),
            priority_tier=(
                self._deep_continuation_priority(
                    state.envelope,
                    continuation=continuation,
                )
                if stage is ValidationStage.CANONICAL_DEEP
                else 0
            ),
        )
        root_cursor = self._root_cursor[stage]
        health_priority = (
            state.envelope.purpose is Tier3ValidationPurpose.USER_POOL_HEALTH
        )
        # Continuation preference is local to _FamilyQueue. Rewinding either
        # cursor here lets a returning ledger bypass other runnable buckets.
        if root not in root_cursor:
            if root_is_new or health_priority:
                root_cursor.appendleft(root)
            else:
                root_cursor.append(root)
        family_cursor = self._family_cursor[stage].setdefault(root, deque())
        if family not in family_cursor:
            if family_is_new or health_priority:
                family_cursor.appendleft(family)
            else:
                family_cursor.append(family)
        self._newcomer_enqueued_epoch.setdefault(
            newcomer_key,
            self._stage_release_epoch[stage],
        )
        if stage is ValidationStage.CANONICAL_DEEP:
            self._queued_deep_count += 1
            self._deep_ready_count += 1
            state.deep_queued_monotonic_ns = time.monotonic_ns()
        else:
            self._queued_fast_count += 1
            self._fast_ready_count += 1
        return True

    def _ready_work_used(self) -> int:
        return (
            self._fast_ready_count
            + self._active_fast_count
            + self._deep_ready_count
            + self._active_deep_count
        )

    def _park_fast(self, state: _CandidateState) -> None:
        self._release_owner_credit(state.envelope.candidate_digest)
        state.state = "parked_fast"
        root = state.envelope.source_root or "unknown"
        family = state.envelope.source_family or "unknown"
        families = self._fast_parked_families.setdefault(root, OrderedDict())
        family_queue = families.get(family)
        if family_queue is None:
            family_queue = _FamilyQueue.empty()
            families[family] = family_queue
        health_priority = (
            state.envelope.purpose is Tier3ValidationPurpose.USER_POOL_HEALTH
        )
        family_queue.append(
            state.envelope.candidate_digest,
            continuation=False,
            priority=health_priority,
        )
        if root not in self._fast_parked_root_cursor:
            if health_priority:
                self._fast_parked_root_cursor.appendleft(root)
            else:
                self._fast_parked_root_cursor.append(root)
        family_cursor = self._fast_parked_family_cursor.setdefault(root, deque())
        if family not in family_cursor:
            if health_priority:
                family_cursor.appendleft(family)
            else:
                family_cursor.append(family)
        self._queued_fast_count += 1
        self._fast_parked_count += 1

    def _pop_parked_fast(self) -> _CandidateState | None:
        root_attempts = len(self._fast_parked_root_cursor)
        while root_attempts > 0 and self._fast_parked_root_cursor:
            root_attempts -= 1
            root = self._fast_parked_root_cursor.popleft()
            families = self._fast_parked_families.get(root)
            family_cursor = self._fast_parked_family_cursor.get(root)
            if not families or not family_cursor:
                continue
            family_attempts = len(family_cursor)
            selected: _CandidateState | None = None
            while family_attempts > 0 and family_cursor:
                family_attempts -= 1
                family = family_cursor.popleft()
                family_queue = families.get(family)
                digest = family_queue.popleft() if family_queue is not None else None
                if digest:
                    candidate = self._states.get(digest)
                    if candidate is not None and candidate.state == "parked_fast":
                        selected = candidate
                if family_queue is not None and family_queue.has_candidates():
                    family_cursor.append(family)
                else:
                    families.pop(family, None)
                if selected is not None:
                    break
            if families:
                self._fast_parked_root_cursor.append(root)
            else:
                self._fast_parked_families.pop(root, None)
                self._fast_parked_family_cursor.pop(root, None)
            if selected is not None:
                self._queued_fast_count = max(0, self._queued_fast_count - 1)
                self._fast_parked_count = max(0, self._fast_parked_count - 1)
                return selected
        return None

    def _deep_cohort_limit(self) -> int:
        _fast_slots, effective_deep_slots = self._effective_stage_slot_limits(
            self._fast_slot_limit,
            self._deep_slot_limit,
        )
        if self._fresh_upstream_truly_exhausted():
            return max(1, int(self._worker_envelope))
        return max(2, 2 * max(1, int(effective_deep_slots)))

    def _fresh_upstream_actionable_count(self) -> int:
        return max(
            0,
            int(self._external_fresh_backlog)
            + int(self._normal_reservable_fast_count)
            + int(self._reward_reservable_fast_count)
            + int(self._escape_eligible_bucket_count)
            + int(self._tcp_handoff_lease_count)
            + int(self._expected_responsive_arrivals)
            + int(self._source_retry_due_count),
        )

    def _fresh_upstream_truly_exhausted(self) -> bool:
        return bool(
            self._fast_supply_state == "exhausted"
            and self._fresh_upstream_actionable_count() <= 0
            and self._fast_ready_count <= 0
            and self._fast_parked_count <= 0
            and self._active_fast_count <= 0
            and self._fast_supply_nonactionable_revisions >= 2
        )

    def _deep_retention_limit(self) -> int:
        # Parked DEEP transactions release resident owner credit, so this
        # bounded durable window is independent of the FAST resident window.
        return max(8, 8 * max(1, int(self._worker_envelope)))

    @staticmethod
    def _deep_bucket(state: _CandidateState) -> tuple[str, str]:
        return (
            state.envelope.source_root or "unknown",
            state.envelope.source_family or "unknown",
        )

    @staticmethod
    def _deep_bucket_limit(state: _CandidateState) -> int:
        # Deliberately a constant: it is the diversity guarantee that stops one
        # (source_root, family) owning the DEEP window, and
        # tests/test_tier3_validator_process_domain.py pins the overflow it
        # produces for a single bucket.  Raising it to a share of the window
        # was tried on 2026-09-20 and reverted for breaking that contract; the
        # real cost is that overflow is terminal, which belongs in the
        # retention decision, not in this limit.
        return 16 if state.envelope.dispatch_selection_reason == "reward" else 4

    def _register_deep_retention(self, state: _CandidateState) -> None:
        if state.deep_retained:
            return
        bucket = self._deep_bucket(state)
        state.deep_retained = True
        self._deep_retained_count += 1
        self._deep_retained_by_bucket[bucket] = (
            self._deep_retained_by_bucket.get(bucket, 0) + 1
        )

    def _release_deep_retention(self, state: _CandidateState) -> None:
        if not state.deep_retained:
            return
        bucket = self._deep_bucket(state)
        state.deep_retained = False
        self._deep_retained_count = max(0, self._deep_retained_count - 1)
        remaining = max(0, self._deep_retained_by_bucket.get(bucket, 0) - 1)
        if remaining:
            self._deep_retained_by_bucket[bucket] = remaining
        else:
            self._deep_retained_by_bucket.pop(bucket, None)

    def _remove_parked_deep_membership(self, state: _CandidateState) -> bool:
        if state.state != "parked_deep":
            return False
        digest = state.envelope.candidate_digest
        root, family = self._deep_bucket(state)
        families = self._deep_parked_families.get(root)
        family_queue = families.get(family) if families is not None else None
        if family_queue is None or not family_queue.remove(digest):
            return False
        if not family_queue.has_candidates():
            families.pop(family, None)
            family_cursor = self._deep_parked_family_cursor.get(root)
            if family_cursor is not None:
                try:
                    family_cursor.remove(family)
                except ValueError:
                    pass
        if not families:
            self._deep_parked_families.pop(root, None)
            self._deep_parked_family_cursor.pop(root, None)
            try:
                self._deep_parked_root_cursor.remove(root)
            except ValueError:
                pass
        try:
            self._deep_escape_parked.remove(digest)
        except ValueError:
            pass
        self._queued_deep_count = max(0, self._queued_deep_count - 1)
        self._deep_parked_count = max(0, self._deep_parked_count - 1)
        return True

    def _deep_retention_terminal(
        self,
        state: _CandidateState,
        *,
        reason: str,
    ) -> ValidationTerminal:
        return ValidationTerminal(
            refresh_epoch=self.refresh_epoch,
            candidate_digest=state.envelope.candidate_digest,
            config_digest=state.envelope.config_digest,
            lease_id=state.lease_id,
            sequence=state.sequence,
            worker_id=0,
            worker_generation=0,
            kind=Tier3TerminalKind.CENSORED,
            node_json=state.envelope.node_json,
            transaction_id=state.transaction_id,
            slice_id=state.slice_id,
            stage=ValidationStage.CANONICAL_DEEP,
            purpose=state.envelope.purpose,
            report_json=state.envelope.report_json,
            reason=reason,
            attempt_facts=tuple(state.attempt_facts),
            clean_confirmed=True,
        )

    def _fair_deep_retention_victim(self) -> _CandidateState | None:
        candidates = [
            state
            for state in self._states.values()
            if state.deep_retained
            and state.state == "parked_deep"
            and state.envelope.purpose is Tier3ValidationPurpose.AUTO_ADMISSION
            and self._deep_retained_by_bucket.get(self._deep_bucket(state), 0) > 1
        ]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda state: (
                self._deep_retained_by_bucket.get(self._deep_bucket(state), 0)
                / float(max(1, self._deep_bucket_limit(state))),
                int(state.deep_queued_monotonic_ns),
            ),
        )

    def _retain_deep(
        self,
        state: _CandidateState,
    ) -> Tier3DeepRetentionDecision:
        if state.envelope.purpose is Tier3ValidationPurpose.USER_POOL_HEALTH:
            return Tier3DeepRetentionDecision.RETAINED
        if state.deep_retained:
            return Tier3DeepRetentionDecision.RETAINED
        bucket = self._deep_bucket(state)
        bucket_count = self._deep_retained_by_bucket.get(bucket, 0)
        if bucket_count >= self._deep_bucket_limit(state):
            self._deep_retention_overflow_censored_count += 1
            return Tier3DeepRetentionDecision.OVERFLOW_CENSORED
        if self._deep_retained_count < self._deep_retention_limit():
            self._register_deep_retention(state)
            return Tier3DeepRetentionDecision.RETAINED
        if bucket_count <= 0:
            victim = self._fair_deep_retention_victim()
            if victim is not None and self._remove_parked_deep_membership(victim):
                self._deep_retention_fair_replaced_count += 1
                self._finalize_terminal(
                    victim,
                    self._deep_retention_terminal(
                        victim,
                        reason="deep_retention_fair_replaced",
                    ),
                )
                self._register_deep_retention(state)
                return Tier3DeepRetentionDecision.FAIR_REPLACED
        self._deep_retention_overflow_censored_count += 1
        return Tier3DeepRetentionDecision.OVERFLOW_CENSORED

    def _publish_continuation(
        self,
        state: _CandidateState,
        decision: Tier3DeepRetentionDecision,
        result: EndpointAttemptResult,
    ) -> None:
        sink = self.continuation_sink
        if sink is None:
            return
        try:
            sink(state.envelope.candidate_digest, decision, result)
        except Exception:
            pass

    def _park_deep(
        self,
        state: _CandidateState,
        *,
        continuation: bool = False,
    ) -> None:
        self._release_owner_credit(state.envelope.candidate_digest)
        state.state = "parked_deep"
        root = state.envelope.source_root or "unknown"
        family = state.envelope.source_family or "unknown"
        families = self._deep_parked_families.setdefault(root, OrderedDict())
        family_queue = families.get(family)
        if family_queue is None:
            family_queue = _FamilyQueue.empty()
            families[family] = family_queue
        escape_continuation = bool(
            state.envelope.dispatch_selection_reason == "credit_escape"
        )
        continuation_priority = self._deep_continuation_priority(
            state.envelope,
            continuation=continuation,
        )
        family_queue.append(
            state.envelope.candidate_digest,
            continuation=continuation,
            priority=(
                state.envelope.purpose
                is Tier3ValidationPurpose.USER_POOL_HEALTH
                or escape_continuation
                or continuation
            ),
            priority_tier=max(
                continuation_priority,
                2 if escape_continuation and continuation else 0,
            ),
        )
        if root not in self._deep_parked_root_cursor:
            if state.envelope.purpose is Tier3ValidationPurpose.USER_POOL_HEALTH:
                self._deep_parked_root_cursor.appendleft(root)
            else:
                self._deep_parked_root_cursor.append(root)
        family_cursor = self._deep_parked_family_cursor.setdefault(root, deque())
        if family not in family_cursor:
            if state.envelope.purpose is Tier3ValidationPurpose.USER_POOL_HEALTH:
                family_cursor.appendleft(family)
            else:
                family_cursor.append(family)
        self._queued_deep_count += 1
        self._deep_parked_count += 1
        if escape_continuation:
            # Escape credit remains leased until the transaction terminates.
            # When normal FAST credit is blocked, finishing these bounded
            # transactions is the shortest path to making fresh handoff
            # credit available again.  Keep a separate O(1) recovery queue;
            # ordinary ready/imminent operation still uses root/family DRR.
            if continuation:
                # Lexicographically prefer remaining work once an escape
                # transaction has begun canonical validation. Round-robin at
                # endpoint granularity advances every lease but delays the
                # first terminal, which is the only event that releases escape
                # credit. The queue is globally bounded by the scheduler's
                # escape lease cap, so this cannot create unbounded starvation.
                self._deep_escape_parked.appendleft(
                    state.envelope.candidate_digest
                )
            else:
                self._deep_escape_parked.append(
                    state.envelope.candidate_digest
                )
        state.deep_queued_monotonic_ns = time.monotonic_ns()

    def _pop_blocked_escape_deep(self) -> _CandidateState | None:
        if self._fast_supply_state != "blocked":
            return None
        while self._deep_escape_parked:
            digest = self._deep_escape_parked.popleft()
            state = self._states.get(digest)
            if (
                state is None
                or state.state != "parked_deep"
                or state.envelope.dispatch_selection_reason != "credit_escape"
            ):
                continue
            root = state.envelope.source_root or "unknown"
            family = state.envelope.source_family or "unknown"
            families = self._deep_parked_families.get(root)
            family_queue = families.get(family) if families is not None else None
            if family_queue is None or not family_queue.remove(digest):
                continue
            family_cursor = self._deep_parked_family_cursor.get(root)
            if not family_queue.has_candidates():
                families.pop(family, None)
                if family_cursor is not None:
                    try:
                        family_cursor.remove(family)
                    except ValueError:
                        pass
            if not families:
                self._deep_parked_families.pop(root, None)
                self._deep_parked_family_cursor.pop(root, None)
                try:
                    self._deep_parked_root_cursor.remove(root)
                except ValueError:
                    pass
            self._queued_deep_count = max(0, self._queued_deep_count - 1)
            self._deep_parked_count = max(0, self._deep_parked_count - 1)
            self._blocked_escape_deep_priority_count += 1
            return state
        return None

    def _pop_parked_deep(self) -> _CandidateState | None:
        escape_state = self._pop_blocked_escape_deep()
        if escape_state is not None:
            return escape_state
        root_attempts = len(self._deep_parked_root_cursor)
        while root_attempts > 0 and self._deep_parked_root_cursor:
            root_attempts -= 1
            root = self._deep_parked_root_cursor.popleft()
            families = self._deep_parked_families.get(root)
            family_cursor = self._deep_parked_family_cursor.get(root)
            if not families or not family_cursor:
                continue
            family_attempts = len(family_cursor)
            selected: _CandidateState | None = None
            while family_attempts > 0 and family_cursor:
                family_attempts -= 1
                family = family_cursor.popleft()
                family_queue = families.get(family)
                digest = family_queue.popleft() if family_queue is not None else None
                if digest:
                    candidate = self._states.get(digest)
                    if candidate is not None and candidate.state == "parked_deep":
                        selected = candidate
                if family_queue is not None and family_queue.has_candidates():
                    family_cursor.append(family)
                else:
                    families.pop(family, None)
                if selected is not None:
                    break
            if families:
                self._deep_parked_root_cursor.append(root)
            else:
                self._deep_parked_families.pop(root, None)
                self._deep_parked_family_cursor.pop(root, None)
            if selected is not None:
                self._queued_deep_count = max(0, self._queued_deep_count - 1)
                self._deep_parked_count = max(0, self._deep_parked_count - 1)
                return selected
        return None

    def _refill_deep_cohort(self) -> None:
        self._refill_ready_work()

    def _refill_ready_work(self) -> None:
        if self._sealed:
            return
        while self._ready_work_used() < self._ready_work_limit:
            if not self._owner_credit_available():
                return
            fast_available = self._fast_parked_count > 0
            deep_available = bool(
                self._deep_parked_count > 0
                and self._deep_ready_count + self._active_deep_count
                < self._deep_cohort_limit()
            )
            if not fast_available and not deep_available:
                return
            if fast_available and not deep_available:
                stage = ValidationStage.FAST_PROBE
            elif deep_available and not fast_available:
                stage = ValidationStage.CANONICAL_DEEP
            else:
                effective_fast_slots, effective_deep_slots = (
                    self._effective_stage_slot_limits(
                        self._fast_slot_limit,
                        self._deep_slot_limit,
                    )
                )
                fast_target = max(1, 2 * max(1, effective_fast_slots))
                deep_target = max(1, 2 * max(1, effective_deep_slots))
                fast_load = (
                    self._fast_ready_count + self._active_fast_count
                ) / fast_target
                deep_load = (
                    self._deep_ready_count + self._active_deep_count
                ) / deep_target
                # FAST is only the equal-debt tie-break. Letting it override
                # normalized lane load lets a stream of one-at-a-time FAST
                # completions immediately reclaim every released ready credit,
                # so the advertised DEEP slots never become dispatchable.
                if fast_load < deep_load:
                    stage = ValidationStage.FAST_PROBE
                elif deep_load < fast_load:
                    stage = ValidationStage.CANONICAL_DEEP
                else:
                    # FAST remains the deterministic equal-debt tie-break.
                    stage = ValidationStage.FAST_PROBE
            state = (
                self._pop_parked_fast()
                if stage is ValidationStage.FAST_PROBE
                else self._pop_parked_deep()
            )
            if state is None:
                if stage is ValidationStage.FAST_PROBE and deep_available:
                    state = self._pop_parked_deep()
                    stage = ValidationStage.CANONICAL_DEEP
                elif stage is ValidationStage.CANONICAL_DEEP and fast_available:
                    state = self._pop_parked_fast()
                    stage = ValidationStage.FAST_PROBE
            if state is None:
                return
            self._enqueue_stage(
                state,
                stage,
                continuation=(
                    stage is ValidationStage.CANONICAL_DEEP
                    and state.deep_dispatch_count > 0
                ),
            )

    def _trim_deep_cohort(self) -> None:
        limit = self._deep_cohort_limit()
        while self._deep_ready_count + self._active_deep_count > limit:
            selected: _CandidateState | None = None
            # Return fresh work before continuations. A transaction that has
            # already started canonical validation keeps its continuation
            # debt whenever any fresh reservation can be revoked instead.
            for prefer_continuation in (False, True):
                for families in reversed(tuple(self._root_families[
                    ValidationStage.CANONICAL_DEEP
                ].values())):
                    for family_queue in reversed(tuple(families.values())):
                        target = (
                            None
                            if prefer_continuation
                            else family_queue.candidates
                        )
                        targets = (
                            (
                                family_queue.continuations,
                                family_queue.proven_continuations,
                                family_queue.trusted_continuations,
                            )
                            if prefer_continuation
                            else (target,)
                        )
                        for candidate_queue in targets:
                            if candidate_queue is None:
                                continue
                            while candidate_queue:
                                digest = candidate_queue.pop()
                                state = self._states.get(digest)
                                if (
                                    state is not None
                                    and state.state == "queued_deep"
                                ):
                                    selected = state
                                    break
                            if selected is not None:
                                break
                        if selected is not None:
                            break
                    if selected is not None:
                        break
                if selected is not None:
                    break
            if selected is None:
                return
            self._queued_deep_count = max(0, self._queued_deep_count - 1)
            self._deep_ready_count = max(0, self._deep_ready_count - 1)
            self._park_deep(
                selected,
                continuation=selected.deep_dispatch_count > 0,
            )

    def _pop_ready_tail(
        self,
        stage: ValidationStage,
    ) -> _CandidateState | None:
        expected_state = self._state_for_stage(stage, False)
        for families in reversed(tuple(self._root_families[stage].values())):
            for family_queue in reversed(tuple(families.values())):
                targets = (
                    (
                        family_queue.candidates,
                        family_queue.continuations,
                        family_queue.proven_continuations,
                        family_queue.trusted_continuations,
                    )
                    if stage is ValidationStage.CANONICAL_DEEP
                    else (family_queue.candidates,)
                )
                for target in targets:
                    while target:
                        digest = target.pop()
                        state = self._states.get(digest)
                        if state is not None and state.state == expected_state:
                            return state
        return None

    def _trim_ready_window(self) -> None:
        while self._ready_work_used() > self._ready_work_limit:
            fast_excess = max(
                0,
                self._fast_ready_count - max(1, 2 * self._fast_slot_limit),
            )
            deep_excess = max(
                0,
                self._deep_ready_count - max(1, 2 * self._deep_slot_limit),
            )
            stage = (
                ValidationStage.FAST_PROBE
                if fast_excess >= deep_excess and self._fast_ready_count > 0
                else ValidationStage.CANONICAL_DEEP
            )
            selected = self._pop_ready_tail(stage)
            if selected is None:
                alternate = (
                    ValidationStage.CANONICAL_DEEP
                    if stage is ValidationStage.FAST_PROBE
                    else ValidationStage.FAST_PROBE
                )
                selected = self._pop_ready_tail(alternate)
                stage = alternate
            if selected is None:
                return
            if stage is ValidationStage.FAST_PROBE:
                self._queued_fast_count = max(0, self._queued_fast_count - 1)
                self._fast_ready_count = max(0, self._fast_ready_count - 1)
                self._park_fast(selected)
            else:
                self._queued_deep_count = max(0, self._queued_deep_count - 1)
                self._deep_ready_count = max(0, self._deep_ready_count - 1)
                self._park_deep(
                    selected,
                    continuation=selected.deep_dispatch_count > 0,
                )

    def _add_candidate(self, envelope: CandidateEnvelope) -> None:
        if envelope.refresh_epoch != self.refresh_epoch:
            self._release_owner_credit(envelope.candidate_digest)
            return
        if envelope.candidate_digest in self._states:
            self._release_owner_credit(envelope.candidate_digest)
            return
        endpoints = tuple(dict.fromkeys(str(item) for item in envelope.probe_urls if str(item)))
        if not endpoints:
            # Compatibility-only endpoint for actor unit tests that inject
            # results directly. Production CandidateEnvelope always carries
            # the configured portfolio.
            endpoints = ("__legacy_endpoint__",)
        if self._endpoint_controller is None:
            self._endpoint_controller = Tier3EndpointController(endpoints)
        state = _CandidateState(
            envelope=envelope,
            transaction_id=f"{self.refresh_epoch}:{envelope.candidate_digest[:24]}",
            endpoints=endpoints,
            fast_endpoint="",
        )
        self._states[envelope.candidate_digest] = state
        self._submitted_count += 1
        if self._sealed:
            state.state = "terminal"
            terminal = ValidationTerminal(
                refresh_epoch=self.refresh_epoch,
                candidate_digest=envelope.candidate_digest,
                config_digest=envelope.config_digest,
                lease_id="",
                sequence=0,
                worker_id=0,
                worker_generation=0,
                kind=Tier3TerminalKind.CANCELLED,
                node_json=envelope.node_json,
                transaction_id=state.transaction_id,
                slice_id=0,
                stage=ValidationStage.FAST_PROBE,
                purpose=envelope.purpose,
                report_json=envelope.report_json,
                reason="expansion_sealed_before_dispatch",
                clean_confirmed=True,
            )
            state.terminal = terminal
            self._terminal_state_count += 1
            self._cancelled_before_dispatch_count += 1
            self._terminal_counts[Tier3TerminalKind.CANCELLED] += 1
            try:
                self.terminal_sink.put_nowait(terminal)
            except queue.Full:
                pass
            self._release_owner_credit(envelope.candidate_digest)
            return
        self._park_fast(state)
        self._refill_ready_work()

    def _next_candidate_digest(self, stage: ValidationStage) -> str | None:
        roots = self._root_families[stage]
        root_cursor = self._root_cursor[stage]
        family_cursors = self._family_cursor[stage]
        expected_state = self._state_for_stage(stage, False)
        root_attempts = len(root_cursor)
        while root_attempts > 0 and root_cursor:
            root_attempts -= 1
            root = root_cursor.popleft()
            families = roots.get(root)
            family_cursor = family_cursors.get(root)
            if not families or not family_cursor:
                continue
            family_attempts = len(family_cursor)
            selected: str | None = None
            while family_attempts > 0 and family_cursor:
                family_attempts -= 1
                family = family_cursor.popleft()
                family_queue = families.get(family)
                if family_queue is None:
                    continue
                while family_queue.has_candidates():
                    digest = family_queue.popleft()
                    if digest is None:
                        break
                    state = self._states.get(digest)
                    if state is not None and state.state == expected_state:
                        selected = digest
                        break
                if family_queue.has_candidates():
                    family_cursor.append(family)
                else:
                    families.pop(family, None)
                if selected:
                    break
            if families:
                root_cursor.append(root)
            else:
                roots.pop(root, None)
                family_cursors.pop(root, None)
            if selected:
                if stage is ValidationStage.FAST_PROBE:
                    self._queued_fast_count = max(0, self._queued_fast_count - 1)
                    self._fast_ready_count = max(0, self._fast_ready_count - 1)
                else:
                    self._queued_deep_count = max(0, self._queued_deep_count - 1)
                    self._deep_ready_count = max(0, self._deep_ready_count - 1)
                newcomer_key = (stage, root, family)
                if newcomer_key not in self._newcomer_dispatched:
                    stage_epoch = self._stage_release_epoch[stage]
                    queued_epoch = self._newcomer_enqueued_epoch.get(
                        newcomer_key, stage_epoch
                    )
                    wait_epochs = max(0, stage_epoch - queued_epoch)
                    self._max_newcomer_wait_epochs = max(
                        self._max_newcomer_wait_epochs, wait_epochs
                    )
                    if wait_epochs > 2:
                        self._fairness_violation_count += 1
                    self._newcomer_dispatched.add(newcomer_key)
                return selected
        return None

    def _stage_depth(self, stage: ValidationStage) -> int:
        return (
            self._fast_ready_count
            if stage is ValidationStage.FAST_PROBE
            else self._deep_ready_count
        )

    def _active_stage_count(self, stage: ValidationStage) -> int:
        return (
            self._active_fast_count
            if stage is ValidationStage.FAST_PROBE
            else self._active_deep_count
        )

    @staticmethod
    def _baseline_stage_slot_limits(total: int) -> tuple[int, int]:
        total = max(1, int(total))
        if total <= 1:
            return 1, 0
        deep = max(1, min(total - 1, int(round(total * 0.25))))
        return total - deep, deep

    def _controlled_stage_slot_limits(self) -> tuple[int, int]:
        return self._baseline_stage_slot_limits(self._worker_envelope)

    def _rolling_stage_summary(
        self,
        stage: ValidationStage,
        *,
        now: float | None = None,
    ) -> dict[str, int | float]:
        current = time.monotonic() if now is None else float(now)
        samples = [
            sample
            for sample in self._rolling_stage_samples
            if sample.stage is stage and current - sample.completed_at <= 60.0
        ][-64:]
        completions = len(samples)
        occupied_ms = sum(max(0, sample.occupied_ms) for sample in samples)
        net_new = sum(1 for sample in samples if sample.net_new)
        terminals = sum(1 for sample in samples if sample.terminal)
        passed = sum(1 for sample in samples if sample.passed)
        facility_failed = sum(1 for sample in samples if sample.facility_failed)
        transport_completed = sum(
            1 for sample in samples if sample.transport_completed
        )
        return {
            "completions": completions,
            "occupied_ms": occupied_ms,
            "net_new": net_new,
            "terminals": terminals,
            "passed": passed,
            "facility_failed": facility_failed,
            "transport_completed": transport_completed,
            "yield_lcb": _pass_throughput_lcb(
                net_new,
                completions,
                occupied_ms,
            ),
        }

    def _session_stage_summary(
        self,
        stage: ValidationStage,
    ) -> dict[str, int | float]:
        """Return transport-backed evidence for the current expansion session.

        The rolling window controls rapid recovery. This longer session view
        also keeps established zero-yield probation from disappearing merely
        because a reduced DEEP lane produced no new samples for 60 seconds.
        """

        samples = [
            sample
            for sample in self._rolling_stage_samples
            if sample.stage is stage and sample.transport_completed
        ]
        completions = len(samples)
        occupied_ms = sum(max(0, sample.occupied_ms) for sample in samples)
        net_new = sum(1 for sample in samples if sample.net_new)
        return {
            "completions": completions,
            "occupied_ms": occupied_ms,
            "net_new": net_new,
            "yield_lcb": _pass_throughput_lcb(
                net_new,
                completions,
                occupied_ms,
            ),
            "yield_ucb": _pass_throughput_ucb(
                net_new,
                completions,
                occupied_ms,
            ),
        }

    def _priority_deep_waiting(self) -> bool:
        """Keep Tier3A health continuations serviceable during FAST discovery."""

        return any(
            state.envelope.purpose is Tier3ValidationPurpose.USER_POOL_HEALTH
            and state.state in {"parked_deep", "queued_deep", "active_deep"}
            for state in self._states.values()
        )

    def _record_rolling_stage_sample(
        self,
        result: EndpointAttemptResult | ValidationTerminal,
    ) -> None:
        transport_observed = any(
            bool(
                fact.transport_started_monotonic_ns
                or fact.completed
                or fact.entitlement_exhausted
            )
            for fact in result.attempt_facts
        )
        if not transport_observed:
            # Dispatch/facility failures before the mixed-port request starts
            # are worker health evidence, not lane-yield evidence.
            return
        kind_value = str(getattr(result.kind, "value", result.kind))
        pass_elapsed_ms = max(
            [
                max(0, int(fact.elapsed_ms))
                for fact in result.attempt_facts
                if str(fact.outcome) == "pass"
            ]
            or [max(0, int(result.portfolio_elapsed_ms or 0))]
        )
        sample = _StageRollingSample(
            completed_at=time.monotonic(),
            candidate_digest=str(result.candidate_digest),
            stage=result.stage,
            occupied_ms=max(0, int(result.total_elapsed_ms or 0)),
            passed=kind_value == Tier3TerminalKind.PASS.value,
            facility_failed=kind_value == Tier3TerminalKind.FACILITY_FAIL.value,
            pass_elapsed_ms=pass_elapsed_ms,
            probe_elapsed_ms=max(
                [max(0, int(fact.elapsed_ms)) for fact in result.attempt_facts]
                or [max(0, int(result.portfolio_elapsed_ms or 0))]
            ),
            right_censored=(
                kind_value == EndpointAttemptKind.RIGHT_CENSORED.value
            ),
            transport_completed=any(
                bool(fact.completed or fact.entitlement_exhausted)
                for fact in result.attempt_facts
            ),
        )
        self._rolling_stage_samples.append(sample)
        sample_key = (sample.candidate_digest, sample.stage)
        self._latest_stage_sample_by_candidate.pop(sample_key, None)
        self._latest_stage_sample_by_candidate[sample_key] = sample
        while len(self._latest_stage_sample_by_candidate) > 1024:
            self._latest_stage_sample_by_candidate.popitem(last=False)

    def _forget_rolling_candidate(self, candidate_digest: str) -> None:
        digest = str(candidate_digest)
        for stage in (ValidationStage.FAST_PROBE, ValidationStage.CANONICAL_DEEP):
            self._latest_stage_sample_by_candidate.pop((digest, stage), None)

    def _mark_rolling_terminal(
        self,
        candidate_digest: str,
        stage: ValidationStage,
    ) -> None:
        sample = self._latest_stage_sample_by_candidate.get(
            (str(candidate_digest), stage)
        )
        if sample is not None:
            sample.terminal = True

    def _mark_rolling_net_new(
        self,
        candidate_digest: str,
        stage: ValidationStage,
    ) -> None:
        sample = self._latest_stage_sample_by_candidate.get(
            (str(candidate_digest), stage)
        )
        if sample is not None:
            sample.net_new = True
        if stage is ValidationStage.CANONICAL_DEEP:
            fast_sample = self._latest_stage_sample_by_candidate.get(
                (str(candidate_digest), ValidationStage.FAST_PROBE)
            )
            if (
                fast_sample is not None
                and fast_sample.right_censored
                and fast_sample.probe_elapsed_ms
                >= int(self._fast_soft_entitlement_seconds * 900.0)
                and self._fast_soft_entitlement_seconds
                < _FAST_SOFT_ENTITLEMENT_DEFAULT_SECONDS
            ):
                # A durable DEEP admission immediately after a FAST slice that
                # exhausted the shortened entitlement is direct evidence that
                # the recent-latency model is under-budgeting the tail.
                self._fast_soft_entitlement_seconds = (
                    _FAST_SOFT_ENTITLEMENT_DEFAULT_SECONDS
                )
                self._fast_soft_reset_count += 1
                self._fast_soft_last_adjust_completion = int(
                    self._fast_soft_completion_counter
                )

    def _fast_probe_budget_seconds(self, envelope: CandidateEnvelope) -> float:
        """Replay changes ordering, never the validation entitlement."""
        del envelope
        return _FAST_SOFT_ENTITLEMENT_DEFAULT_SECONDS

    def _refresh_fast_soft_entitlement(self) -> None:
        if (
            self._fast_soft_entitlement_seconds
            != _FAST_SOFT_ENTITLEMENT_DEFAULT_SECONDS
        ):
            self._fast_soft_reset_count += 1
        self._fast_soft_entitlement_seconds = _FAST_SOFT_ENTITLEMENT_DEFAULT_SECONDS
        self._fast_soft_last_adjust_completion = int(
            self._fast_soft_completion_counter
        )

    def _start_deep_recovery_probation(self, now: float) -> None:
        """Open the first bounded recovery window after a latched DEEP PASS."""

        self._deep_probation_active = True
        self._deep_recovery_level = 1
        self._deep_probation_started_at = float(now)
        # The admission that opens probation is evidence to inspect the lane,
        # not evidence produced inside the probation window itself.  Starting
        # from the current counters keeps both recovery stages independent.
        self._deep_probation_start_completion_count = int(
            self._deep_completed_count
        )
        self._deep_probation_start_net_new_count = int(
            self._deep_net_new_count
        )
        self._deep_zero_yield_latch_net_new_count = int(
            self._deep_net_new_count
        )

    def _advance_deep_recovery_probation(self, now: float) -> None:
        """Require a second independent window before clearing the latch."""

        self._deep_recovery_level = 2
        self._deep_probation_started_at = float(now)
        self._deep_probation_start_completion_count = int(
            self._deep_completed_count
        )
        self._deep_probation_start_net_new_count = int(
            self._deep_net_new_count
        )
        self._deep_zero_yield_latch_net_new_count = int(
            self._deep_net_new_count
        )

    def _desired_stage_slot_limits(self) -> tuple[int, int, str]:
        total = max(1, int(self._worker_envelope))
        fast_backlog = (
            self._fast_ready_count
            + self._fast_parked_count
            + max(0, int(self._external_fresh_backlog))
        )
        fast_recent = self._rolling_stage_summary(ValidationStage.FAST_PROBE)
        deep_recent = self._rolling_stage_summary(ValidationStage.CANONICAL_DEEP)
        fast_session = self._session_stage_summary(ValidationStage.FAST_PROBE)
        deep_session = self._session_stage_summary(ValidationStage.CANONICAL_DEEP)
        now = time.monotonic()

        fast_session_lcb = float(fast_session["yield_lcb"])
        deep_session_lcb = float(deep_session["yield_lcb"])
        deep_session_ucb = float(deep_session["yield_ucb"])
        fast_session_net_new = int(fast_session["net_new"])
        deep_session_net_new = int(deep_session["net_new"])
        fast_session_occupied = int(fast_session["occupied_ms"])
        deep_session_occupied = int(deep_session["occupied_ms"])
        total_session_net_new = fast_session_net_new + deep_session_net_new
        total_session_occupied = fast_session_occupied + deep_session_occupied

        fast_evidence_ready_for_latch = bool(
            int(fast_session["completions"]) >= 32
            and fast_session_occupied >= 30_000
            and fast_session_net_new >= 3
            and fast_session_lcb > 0.0
        )
        deep_evidence_ready_for_latch = bool(
            int(deep_session["completions"]) >= 16
            and deep_session_occupied >= 60_000
        )
        deep_throughput_dominated = bool(
            fast_session_lcb > 0.0
            and deep_session_ucb < fast_session_lcb
        )
        deep_cost_share = (
            deep_session_occupied / float(total_session_occupied)
            if total_session_occupied > 0
            else 0.0
        )
        deep_admission_share = (
            deep_session_net_new / float(total_session_net_new)
            if total_session_net_new > 0
            else 0.0
        )
        fast_worker_efficiency = (
            fast_session_net_new / float(fast_session_occupied)
            if fast_session_occupied > 0
            else 0.0
        )
        deep_worker_efficiency = (
            deep_session_net_new / float(deep_session_occupied)
            if deep_session_occupied > 0
            else 0.0
        )
        deep_worker_efficiency_dominated = bool(
            fast_worker_efficiency > 0.0
            and deep_worker_efficiency <= 0.5 * fast_worker_efficiency
        )
        deep_cost_inefficient = bool(
            total_session_net_new > 0
            and deep_cost_share >= 0.25
            and deep_admission_share <= 0.20
        )
        deep_low_yield_evidence = bool(
            fast_backlog > 0
            and fast_evidence_ready_for_latch
            and deep_evidence_ready_for_latch
            and (
                deep_throughput_dominated
                or deep_cost_inefficient
                or deep_worker_efficiency_dominated
            )
        )

        hard_deep_zero_yield = bool(
            fast_backlog > 0
            and (
                (
                    int(deep_recent["completions"]) >= 32
                    and int(deep_recent["occupied_ms"]) >= 60_000
                    and int(deep_recent["net_new"]) <= 0
                )
                or (
                    int(deep_session["completions"]) >= 32
                    and deep_session_occupied >= 60_000
                    and deep_session_net_new <= 0
                )
            )
        )
        if hard_deep_zero_yield and not self._deep_zero_yield_latched:
            # This is already a complete deterministic evidence window, not a
            # noisy relative comparison. Latch immediately so another second
            # of allocator smoothing cannot spend four more worker slots on a
            # lane with no durable yield.
            self._deep_zero_yield_latched = True
            self._deep_zero_yield_latch_net_new_count = int(
                self._deep_net_new_count
            )
            self._deep_zero_yield_latched_monotonic_ns = time.monotonic_ns()
            self._deep_low_yield_latch_reason = "deep_zero_yield_debt"
            self._deep_probation_active = False
            self._deep_recovery_level = 0

        if not self._deep_zero_yield_latched:
            if now - self._deep_low_yield_last_evaluated_at >= 0.5:
                self._deep_low_yield_last_evaluated_at = now
                if deep_low_yield_evidence:
                    self._deep_low_yield_evidence_streak += 1
                else:
                    self._deep_low_yield_evidence_streak = 0
            if self._deep_low_yield_evidence_streak >= 2:
                self._deep_zero_yield_latched = True
                self._deep_zero_yield_latch_net_new_count = int(
                    self._deep_net_new_count
                )
                self._deep_zero_yield_latched_monotonic_ns = time.monotonic_ns()
                self._deep_low_yield_latch_reason = "deep_low_yield_latched"
                self._deep_probation_active = False
                self._deep_recovery_level = 0

        if (
            self._deep_zero_yield_latched
            and not self._deep_probation_active
            and int(self._deep_net_new_count)
            > int(self._deep_zero_yield_latch_net_new_count)
        ):
            # A post-latch DEEP admission grants a bounded probation window;
            # it never clears the latch by itself. Keep this transition in
            # the allocation owner as well as the commit path so reordered
            # feedback cannot leave policy and evidence out of sync.
            self._start_deep_recovery_probation(now)

        if self._deep_zero_yield_latched:
            probation_completion_delta = max(
                0,
                int(deep_session["completions"])
                - int(self._deep_probation_start_completion_count),
            )
            probation_net_new_delta = max(
                0,
                deep_session_net_new
                - int(self._deep_probation_start_net_new_count),
            )
            probation_expired = bool(
                self._deep_probation_active
                and (
                    probation_completion_delta >= 8
                    or now - self._deep_probation_started_at >= 30.0
                )
            )
            probation_succeeded = bool(
                self._deep_probation_active
                and probation_net_new_delta >= 2
                and deep_session_lcb >= 0.5 * fast_session_lcb
            )
            if probation_succeeded:
                if self._deep_recovery_level <= 1:
                    self._advance_deep_recovery_probation(now)
                else:
                    self._deep_zero_yield_latched = False
                    self._deep_zero_yield_latched_monotonic_ns = 0
                    self._deep_probation_active = False
                    self._deep_recovery_level = 0
                    self._deep_low_yield_evidence_streak = 0
                    self._deep_low_yield_latch_reason = ""
            elif probation_expired:
                self._deep_probation_active = False
                self._deep_recovery_level = 0
                self._deep_zero_yield_latch_net_new_count = int(
                    self._deep_net_new_count
                )
            if self._deep_zero_yield_latched and fast_backlog > 0 and total > 1:
                if self._deep_probation_active and self._deep_recovery_level >= 2:
                    deep_slots = 3 if total > 3 else max(1, total - 1)
                elif self._deep_probation_active:
                    deep_slots = 2 if total > 2 else 1
                else:
                    deep_slots = 1
                return (
                    total - deep_slots,
                    deep_slots,
                    (
                        "deep_low_yield_probation_stage_2"
                        if self._deep_recovery_level >= 2
                        else "deep_low_yield_probation_stage_1"
                        if self._deep_probation_active
                        else (
                            self._deep_low_yield_latch_reason
                            or "deep_low_yield_latched"
                        )
                    ),
                )

        if fast_backlog > 0:
            baseline_fast, baseline_deep = self._baseline_stage_slot_limits(total)
            return baseline_fast, baseline_deep, "fresh_supply_hard_baseline"

        def lane_evidence_ready(summary: Mapping[str, int | float]) -> bool:
            return bool(
                int(summary["transport_completed"]) >= 16
                and int(summary["occupied_ms"]) >= 30_000
                and int(summary["net_new"]) >= 3
                and float(summary["yield_lcb"]) > 0.0
            )

        fast_evidence_ready = lane_evidence_ready(fast_recent)
        deep_evidence_ready = lane_evidence_ready(deep_recent)
        if not fast_evidence_ready and not deep_evidence_ready:
            baseline_fast, baseline_deep = self._baseline_stage_slot_limits(total)
            return baseline_fast, baseline_deep, "transport_evidence_baseline"
        if deep_evidence_ready and not fast_evidence_ready and total > 1:
            fast_slots = max(1, int(math.ceil(total * 0.5)))
            return fast_slots, total - fast_slots, "deep_recent_yield_recovery"
        if fast_evidence_ready and deep_evidence_ready and total > 1:
            fast_lcb = float(fast_recent["yield_lcb"])
            deep_lcb = float(deep_recent["yield_lcb"])
            if fast_lcb > 0.0 or deep_lcb > 0.0:
                fast_share = fast_lcb / max(0.000001, fast_lcb + deep_lcb)
                fast_slots = int(round(total * fast_share))
                fast_slots = max(int(math.ceil(total * 0.5)), fast_slots)
                fast_slots = min(total - 1, fast_slots)
                return fast_slots, total - fast_slots, "rolling_yield_lcb"
        baseline_fast, baseline_deep = self._baseline_stage_slot_limits(total)
        return baseline_fast, baseline_deep, "transport_evidence_baseline"

    def _refresh_stage_slot_limits(self) -> None:
        target_fast, target_deep, reason = self._desired_stage_slot_limits()
        now = time.monotonic()
        if now - self._allocation_last_evaluated_at < 0.5:
            return
        self._allocation_last_evaluated_at = now
        if reason == self._allocation_candidate_reason:
            self._allocation_candidate_streak += 1
        else:
            self._allocation_candidate_reason = reason
            self._allocation_candidate_streak = 1
        if self._allocation_candidate_streak < 2:
            return
        if target_fast == self._fast_slot_limit:
            self._allocation_reason = reason
            return
        if now - self._allocation_last_moved_at < 1.0:
            return
        delta = max(-1, min(1, target_fast - self._fast_slot_limit))
        self._fast_slot_limit += delta
        self._deep_slot_limit = self._worker_envelope - self._fast_slot_limit
        self._deep_slot_limit = max(
            0,
            min(max(0, self._worker_envelope - 1), self._deep_slot_limit),
        )
        self._fast_slot_limit = max(
            1,
            self._worker_envelope - self._deep_slot_limit,
        )
        self._allocation_reason = reason
        self._allocation_last_moved_at = now
        self._trim_deep_cohort()
        self._refill_ready_work()

    def _reset_stage_slot_limits_for_envelope(self) -> None:
        (
            self._fast_slot_limit,
            self._deep_slot_limit,
            self._allocation_reason,
        ) = self._desired_stage_slot_limits()
        self._allocation_candidate_reason = self._allocation_reason
        self._allocation_candidate_streak = 0
        now = time.monotonic()
        self._allocation_last_evaluated_at = now
        self._allocation_last_moved_at = now

    def _stage_slot_limits(self) -> tuple[int, int]:
        self._refresh_stage_slot_limits()
        return self._effective_stage_slot_limits(
            self._fast_slot_limit,
            self._deep_slot_limit,
        )

    def _effective_stage_slot_limits(
        self,
        fast_slots: int,
        deep_slots: int,
    ) -> tuple[int, int]:
        """Lend capacity that FAST cannot consume to durable DEEP work.

        Desired allocation is unchanged. Pending FAST blocks new loans;
        running FAST and predicted arrivals reserve only their actual slots.
        Upstream work permits a bounded loan, not full DEEP work stealing.
        """

        total = max(1, int(self._worker_envelope))
        normalized_fast = max(0, min(total, int(fast_slots)))
        normalized_deep = max(0, min(total, int(deep_slots)))
        if normalized_fast + normalized_deep != total:
            normalized_deep = max(0, total - normalized_fast)
        deep_work = max(
            0,
            int(self._deep_ready_count)
            + int(self._deep_parked_count)
            + int(self._active_deep_count),
            int(self._actionable_deep),
        )
        if deep_work <= 0:
            return normalized_fast, normalized_deep
        pending_fast = max(
            0,
            int(self._fast_ready_count)
            + int(self._fast_parked_count),
        )
        if pending_fast > 0:
            return normalized_fast, normalized_deep
        if self._fresh_upstream_truly_exhausted():
            return 0, total
        # An active FAST request does not make every other FAST slot busy.
        # Keep room for predicted TCP arrivals before lending idle capacity;
        # existing bounded DEEP requests may carry over when FAST returns.
        projected_fast = max(0, int(self._active_fast_count)) + max(
            0, int(self._expected_responsive_arrivals)
        )
        idle_fast = max(0, normalized_fast - projected_fast)
        borrow_limit = 1 if self._deep_zero_yield_latched else 2
        borrowed = min(borrow_limit, idle_fast)
        return normalized_fast - borrowed, normalized_deep + borrowed

    def _choose_stage(self) -> ValidationStage | None:
        fast_ready = self._stage_depth(ValidationStage.FAST_PROBE) > 0
        deep_ready = self._stage_depth(ValidationStage.CANONICAL_DEEP) > 0
        fast_limit, deep_limit = self._stage_slot_limits()
        if not fast_ready and not deep_ready:
            return None
        if fast_ready and not deep_ready:
            return ValidationStage.FAST_PROBE
        if deep_ready and not fast_ready:
            active_deep = self._active_stage_count(
                ValidationStage.CANONICAL_DEEP
            )
            return (
                ValidationStage.CANONICAL_DEEP
                if active_deep < deep_limit
                else None
            )
        # Fast breadth is the deterministic baseline for every expansion, not
        # an empty-pool special case. Deep retains one service-debt slot while
        # either empty stage can still lend all capacity to the other.
        active_fast = self._active_stage_count(ValidationStage.FAST_PROBE)
        active_deep = self._active_stage_count(ValidationStage.CANONICAL_DEEP)
        if (
            self._fast_completions_since_deep >= 16
            and deep_limit > 0
            and active_deep < deep_limit
        ):
            return ValidationStage.CANONICAL_DEEP
        if fast_limit <= 0:
            return (
                ValidationStage.CANONICAL_DEEP
                if active_deep < deep_limit
                else None
            )
        fast_debt = max(0.0, (fast_limit - active_fast) / fast_limit)
        deep_debt = (
            max(0.0, (deep_limit - active_deep) / deep_limit)
            if deep_limit > 0
            else 0.0
        )
        # Equal service debt goes to fresh breadth. Deep already has an
        # explicit 16-completion debt guard above, so tie-breaking toward it
        # here recreates the head-of-line behavior the parked ledger avoids.
        if active_deep < deep_limit and deep_debt > fast_debt:
            return ValidationStage.CANONICAL_DEEP
        if active_fast < fast_limit:
            return ValidationStage.FAST_PROBE
        if active_deep < deep_limit:
            return ValidationStage.CANONICAL_DEEP
        return None

    def _dispatch(self) -> None:
        while (
            self._available_capacity > 0
            and not self._sealed
            and self._dispatch_enabled
        ):
            stage = self._choose_stage()
            if stage is None:
                return
            digest = self._next_candidate_digest(stage)
            if digest is None:
                alternate = (
                    ValidationStage.CANONICAL_DEEP
                    if stage is ValidationStage.FAST_PROBE
                    else ValidationStage.FAST_PROBE
                )
                digest = self._next_candidate_digest(alternate)
                if digest is None:
                    return
                stage = alternate
            state = self._states[digest]
            capacity_lease: Tier3ValidatorCapacityLease | None = None
            if self._capacity_reservation_sink is not None:
                capacity_lease = self._capacity_reservation_sink()
                if capacity_lease is None:
                    # Integer capacity events are wake hints. Only a concrete
                    # idle worker generation may authorize a dispatch.
                    self._available_capacity = 0
                    self._capacity_reservation_rejected_count += 1
                    self._enqueue_stage(
                        state,
                        stage,
                        continuation=(
                            stage is ValidationStage.CANONICAL_DEEP
                            and state.deep_dispatch_count > 0
                        ),
                    )
                    return
            self._sequence += 1
            state.slice_id += 1
            lease_id = (
                f"{state.transaction_id}:{state.slice_id}:{stage.value}:"
                f"{self._sequence}"
            )
            state.state = self._state_for_stage(stage, True)
            if stage is ValidationStage.FAST_PROBE:
                self._active_fast_count += 1
            else:
                self._active_deep_count += 1
                state.deep_dispatch_count += 1
                self._fast_completions_since_deep = 0
            active_key = (
                state.envelope.source_root or "unknown",
                state.envelope.source_family or "unknown",
            )
            stage_active = self._active_stage_families[stage]
            stage_active[active_key] = stage_active.get(active_key, 0) + 1
            state.active_stage = stage
            controller = self._endpoint_controller
            if controller is None:
                controller = Tier3EndpointController(state.endpoints)
                self._endpoint_controller = controller
            decision = controller.work_decision(
                digest,
                stage,
                source_root=state.envelope.source_root,
                source_family=state.envelope.source_family,
                attempt_facts=tuple(state.attempt_facts),
                remaining_urls=(
                    tuple(state.canonical_pending)
                    if stage is ValidationStage.CANONICAL_DEEP
                    else state.endpoints
                ),
            )
            endpoint = decision.endpoint
            state.active_decision = decision
            if stage is ValidationStage.FAST_PROBE:
                state.fast_endpoint = endpoint
            state.active_endpoint = endpoint
            state.lease_id = lease_id
            state.sequence = self._sequence
            self._available_capacity = max(0, self._available_capacity - 1)
            envelope = state.envelope
            endpoint_budget = (
                self._fast_probe_budget_seconds(envelope)
                if stage is ValidationStage.FAST_PROBE
                else 5.0
            )
            command = ValidateEndpointCmd(
                refresh_epoch=self.refresh_epoch,
                candidate_digest=digest,
                config_digest=envelope.config_digest,
                lease_id=lease_id,
                sequence=self._sequence,
                node_json=envelope.node_json,
                source_root=envelope.source_root,
                source_family=envelope.source_family,
                transaction_id=state.transaction_id,
                slice_id=state.slice_id,
                stage=stage,
                purpose=envelope.purpose,
                report_json=envelope.report_json,
                capacity_epoch_id=envelope.capacity_epoch_id,
                endpoint=endpoint,
                probe_budget_seconds=endpoint_budget,
                candidate_hard_cap_seconds=(
                    2.0 + endpoint_budget + self._confirm_budget_seconds + 0.75
                ),
                attempt_facts=tuple(state.attempt_facts),
                probe_policy_revision=decision.policy_revision,
                portfolio_digest=decision.portfolio_digest,
                endpoint_selection_reason=decision.selection_reason,
                endpoint_coverage_epoch=decision.coverage_epoch,
                endpoint_canonical_position=decision.canonical_position,
                submitted_monotonic_ns=envelope.submitted_monotonic_ns,
                worker_id=(capacity_lease.worker_id if capacity_lease else 0),
                worker_generation=(
                    capacity_lease.worker_generation if capacity_lease else 0
                ),
                capacity_reservation_id=(
                    capacity_lease.reservation_id if capacity_lease else 0
                ),
            )
            if command.endpoint != decision.endpoint:
                self._controller_dispatch_mismatch_count += 1
            try:
                accepted = self.dispatch_sink(command)
            except Exception as exc:
                accepted = False
                dispatch_error = exc
            else:
                dispatch_error = None
            if accepted is False and capacity_lease is not None:
                if self._capacity_release_sink is not None:
                    self._capacity_release_sink(capacity_lease)
                self._capacity_reservation_rejected_count += 1
                if stage is ValidationStage.FAST_PROBE:
                    self._active_fast_count = max(0, self._active_fast_count - 1)
                else:
                    self._active_deep_count = max(0, self._active_deep_count - 1)
                    state.deep_dispatch_count = max(0, state.deep_dispatch_count - 1)
                remaining = max(0, stage_active.get(active_key, 0) - 1)
                if remaining:
                    stage_active[active_key] = remaining
                else:
                    stage_active.pop(active_key, None)
                state.state = self._state_for_stage(stage, False)
                state.slice_id = max(0, state.slice_id - 1)
                state.active_stage = None
                state.active_decision = None
                state.active_endpoint = ""
                state.lease_id = ""
                state.sequence = 0
                self._available_capacity = 0
                self._enqueue_stage(
                    state,
                    stage,
                    continuation=(
                        stage is ValidationStage.CANONICAL_DEEP
                        and state.deep_dispatch_count > 0
                    ),
                )
                return
            self._controller_decision_count += 1
            self._slice_dispatched_count += 1
            if stage is ValidationStage.FAST_PROBE and state.slice_id == 1:
                self._candidate_dispatched_count += 1
                if (
                    time.monotonic_ns() - self._started_monotonic_ns
                    <= 10_000_000_000
                ):
                    self._fast_candidates_first_10s.add(digest)
            if self._first_dispatch_monotonic_ns <= 0:
                self._first_dispatch_monotonic_ns = time.monotonic_ns()
            if stage is ValidationStage.FAST_PROBE:
                if self._first_fast_dispatch_monotonic_ns <= 0:
                    self._first_fast_dispatch_monotonic_ns = time.monotonic_ns()
            elif self._first_deep_dispatch_monotonic_ns <= 0:
                self._first_deep_dispatch_monotonic_ns = time.monotonic_ns()
            if dispatch_error is not None:
                self._accept_endpoint_result(
                    EndpointAttemptResult(
                        refresh_epoch=self.refresh_epoch,
                        transaction_id=state.transaction_id,
                        candidate_digest=digest,
                        config_digest=envelope.config_digest,
                        lease_id=lease_id,
                        slice_id=state.slice_id,
                        sequence=self._sequence,
                        stage=stage,
                        purpose=envelope.purpose,
                        worker_id=0,
                        worker_generation=0,
                        node_json=envelope.node_json,
                        kind=EndpointAttemptKind.FACILITY_FAIL,
                        report_json=envelope.report_json,
                        reason=(
                            "dispatch_rejected:"
                            f"{type(dispatch_error).__name__}:{dispatch_error}"
                        ),
                        clean_confirmed=True,
                    )
                )

    def _matches_active(self, state: _CandidateState, result: object) -> bool:
        return bool(
            state.state in {"active_fast", "active_deep"}
            and state.lease_id == getattr(result, "lease_id", "")
            and state.sequence == getattr(result, "sequence", -1)
            and state.slice_id == getattr(result, "slice_id", -1)
            and state.active_stage is getattr(result, "stage", None)
            and (
                not getattr(result, "transaction_id", "")
                or state.transaction_id == getattr(result, "transaction_id", "")
            )
        )

    def _release_active_stage(
        self,
        state: _CandidateState,
        stage: ValidationStage,
    ) -> None:
        if stage is ValidationStage.FAST_PROBE:
            self._active_fast_count = max(0, self._active_fast_count - 1)
        else:
            self._active_deep_count = max(0, self._active_deep_count - 1)
        active_key = (
            state.envelope.source_root or "unknown",
            state.envelope.source_family or "unknown",
        )
        stage_active = self._active_stage_families[stage]
        remaining = max(0, stage_active.get(active_key, 0) - 1)
        if remaining:
            stage_active[active_key] = remaining
        else:
            stage_active.pop(active_key, None)
        # One fairness epoch is a feasible worker cohort, not one candidate
        # completion. Counting every release as an epoch falsely reported
        # starvation whenever more than two root/family buckets were ready.
        self._release_credit += 1
        total_slots = max(1, self._worker_envelope)
        while self._release_credit >= total_slots:
            self._release_credit -= total_slots
            self._release_epoch += 1
        fast_slots, deep_slots = self._stage_slot_limits()
        stage_slots = max(1, (
            fast_slots
            if stage is ValidationStage.FAST_PROBE
            else deep_slots
        ))
        self._stage_release_credit[stage] += 1
        while self._stage_release_credit[stage] >= stage_slots:
            self._stage_release_credit[stage] -= stage_slots
            self._stage_release_epoch[stage] += 1

    def _record_stage_runtime(
        self,
        result: EndpointAttemptResult | ValidationTerminal,
    ) -> None:
        elapsed_ms = max(0, int(result.total_elapsed_ms or 0))
        self._record_rolling_stage_sample(result)
        if result.stage is ValidationStage.FAST_PROBE:
            self._fast_soft_completion_counter += 1
            self._fast_occupied_ms += elapsed_ms
            self._refresh_fast_soft_entitlement()
            return
        self._deep_occupied_ms += elapsed_ms
        self._deep_full_sample_count += sum(
            1
            for fact in result.attempt_facts
            if fact.stage == ValidationStage.CANONICAL_DEEP.value
            and fact.completed
            and fact.full_budget
        )

    def _cancel_active_result(
        self,
        state: _CandidateState,
        result: EndpointAttemptResult | ValidationTerminal,
    ) -> None:
        stage = result.stage
        self._release_active_stage(state, stage)
        self._slice_cancelled_count += 1
        self._finalize_terminal(
            state,
            ValidationTerminal(
                refresh_epoch=result.refresh_epoch,
                candidate_digest=result.candidate_digest,
                config_digest=result.config_digest,
                lease_id=result.lease_id,
                sequence=result.sequence,
                worker_id=result.worker_id,
                worker_generation=result.worker_generation,
                kind=Tier3TerminalKind.CANCELLED,
                node_json=result.node_json,
                transaction_id=result.transaction_id,
                slice_id=result.slice_id,
                stage=stage,
                purpose=state.envelope.purpose,
                report_json=result.report_json,
                reason="expansion_sealed_active_slice",
                attempt_facts=result.attempt_facts,
                clean_confirmed=result.clean_confirmed,
                helper_pid=result.helper_pid,
                mihomo_pid=result.mihomo_pid,
                probe_policy_revision=result.probe_policy_revision,
                portfolio_digest=result.portfolio_digest,
                submitted_monotonic_ns=result.submitted_monotonic_ns,
                dispatch_monotonic_ns=result.dispatch_monotonic_ns,
                supervisor_assigned_monotonic_ns=(
                    result.supervisor_assigned_monotonic_ns
                ),
                supervisor_sent_monotonic_ns=(
                    result.supervisor_sent_monotonic_ns
                ),
                helper_started_monotonic_ns=result.helper_started_monotonic_ns,
                portfolio_started_monotonic_ns=result.portfolio_started_monotonic_ns,
            ),
        )

    def _cancel_active_on_stop(self) -> None:
        for state in self._states.values():
            if state.state not in {"active_fast", "active_deep"}:
                continue
            stage = state.active_stage or (
                ValidationStage.FAST_PROBE
                if state.state == "active_fast"
                else ValidationStage.CANONICAL_DEEP
            )
            self._release_active_stage(state, stage)
            self._slice_cancelled_count += 1
            self._finalize_terminal(
                state,
                ValidationTerminal(
                    refresh_epoch=self.refresh_epoch,
                    candidate_digest=state.envelope.candidate_digest,
                    config_digest=state.envelope.config_digest,
                    lease_id=state.lease_id,
                    sequence=state.sequence,
                    worker_id=0,
                    worker_generation=0,
                    kind=Tier3TerminalKind.CANCELLED,
                    node_json=state.envelope.node_json,
                    transaction_id=state.transaction_id,
                    slice_id=state.slice_id,
                    stage=stage,
                    purpose=state.envelope.purpose,
                    report_json=state.envelope.report_json,
                    reason="expansion_stopped_active_slice",
                    attempt_facts=tuple(state.attempt_facts),
                    clean_confirmed=True,
                ),
            )

    @staticmethod
    def _merge_attempt_facts(
        existing: tuple[ProbeAttemptFact, ...],
        incoming: tuple[ProbeAttemptFact, ...],
    ) -> tuple[ProbeAttemptFact, ...]:
        merged = list(existing)
        seen = {
            fact.attempt_id
            or (
                fact.endpoint,
                fact.stage,
                fact.transport_started_monotonic_ns,
                fact.outcome,
            )
            for fact in merged
        }
        for fact in incoming:
            key = fact.attempt_id or (
                fact.endpoint,
                fact.stage,
                fact.transport_started_monotonic_ns,
                fact.outcome,
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(fact)
        return tuple(merged)

    @staticmethod
    def _confirm_https_terminal_kind(
        result: EndpointAttemptResult,
        *,
        has_confirm_fact: bool,
    ) -> Tier3TerminalKind | None:
        """Terminal kind for a slice decided by its mainstream HTTPS confirm."""

        if not has_confirm_fact:
            return None
        reason = str(result.reason or "")
        if (
            result.kind is EndpointAttemptKind.CANDIDATE_FAIL
            and reason.startswith(CONFIRM_HTTPS_FAILED_REASON)
        ):
            return Tier3TerminalKind.CANDIDATE_FAIL
        if (
            result.kind is EndpointAttemptKind.RIGHT_CENSORED
            and reason == CONFIRM_HTTPS_CENSORED_REASON
        ):
            return Tier3TerminalKind.CENSORED
        return None

    def _terminal_from_attempt(
        self,
        state: _CandidateState,
        result: EndpointAttemptResult,
        *,
        kind: Tier3TerminalKind,
        reason: str,
    ) -> ValidationTerminal:
        return ValidationTerminal(
            refresh_epoch=result.refresh_epoch,
            candidate_digest=result.candidate_digest,
            config_digest=result.config_digest,
            lease_id=result.lease_id,
            sequence=result.sequence,
            worker_id=result.worker_id,
            worker_generation=result.worker_generation,
            kind=kind,
            node_json=result.node_json,
            transaction_id=result.transaction_id,
            slice_id=result.slice_id,
            stage=result.stage,
            purpose=state.envelope.purpose,
            report_json=result.report_json,
            reason=reason,
            latency_ms=result.latency_ms,
            slow=result.slow,
            probe_url=result.probe_url,
            attempt_facts=tuple(state.attempt_facts),
            facility_elapsed_ms=result.facility_elapsed_ms,
            portfolio_elapsed_ms=result.portfolio_elapsed_ms,
            total_elapsed_ms=sum(
                max(0, int(fact.elapsed_ms)) for fact in state.attempt_facts
            )
            + max(0, int(result.facility_elapsed_ms)),
            clean_confirmed=result.clean_confirmed,
            helper_pid=result.helper_pid,
            mihomo_pid=result.mihomo_pid,
            probe_policy_revision=result.probe_policy_revision,
            portfolio_digest=result.portfolio_digest,
            submitted_monotonic_ns=result.submitted_monotonic_ns,
            dispatch_monotonic_ns=result.dispatch_monotonic_ns,
            supervisor_assigned_monotonic_ns=(
                result.supervisor_assigned_monotonic_ns
            ),
            supervisor_sent_monotonic_ns=result.supervisor_sent_monotonic_ns,
            helper_started_monotonic_ns=result.helper_started_monotonic_ns,
            portfolio_started_monotonic_ns=result.portfolio_started_monotonic_ns,
        )

    def _accept_endpoint_result(self, result: EndpointAttemptResult) -> None:
        if result.refresh_epoch != self.refresh_epoch:
            return
        state = self._states.get(result.candidate_digest)
        if state is None or not self._matches_active(state, result):
            return
        if self._sealed:
            self._cancel_active_result(state, result)
            return

        is_user_health = (
            state.envelope.purpose
            is Tier3ValidationPurpose.USER_POOL_HEALTH
        )
        stage = result.stage
        endpoint = state.active_endpoint
        decision = state.active_decision
        if decision is not None:
            if (
                result.portfolio_digest
                and result.portfolio_digest != decision.portfolio_digest
            ) or (
                result.probe_policy_revision
                and result.probe_policy_revision != decision.policy_revision
            ):
                self._controller_dispatch_mismatch_count += 1
        # A passing slice may carry mainstream HTTPS confirm facts behind its
        # portfolio fact.  FAST/DEEP ledgers and the endpoint controller belong
        # to the portfolio endpoint only, so select by stage (a confirm URL
        # may coincide with a portfolio URL) and keep confirm facts out of the
        # controller's endpoint statistics.
        portfolio_facts = tuple(
            fact for fact in result.attempt_facts if not is_confirm_https_fact(fact)
        )
        if portfolio_facts:
            endpoint = str(portfolio_facts[-1].endpoint or endpoint)
        self._slice_completed_count += 1
        transport_observed = bool(result.attempt_facts) or bool(
            int(result.portfolio_started_monotonic_ns or 0)
        )
        if transport_observed and not is_user_health:
            self._record_stage_runtime(result)
        self._release_active_stage(state, stage)
        state.active_stage = None
        state.active_decision = None
        state.active_endpoint = ""
        state.attempt_facts = self._merge_attempt_facts(
            state.attempt_facts,
            tuple(result.attempt_facts),
        )
        controller = self._endpoint_controller
        if controller is not None and not is_user_health:
            controller.observe_attempts(
                portfolio_facts,
                passed_endpoint=(
                    endpoint if result.kind is EndpointAttemptKind.PASS else ""
                ),
                root=state.envelope.source_root,
                protocol=state.envelope.source_family,
                fast_tilt_budget=(
                    2 * self._worker_envelope
                    if result.kind is EndpointAttemptKind.PASS
                    and stage is ValidationStage.CANONICAL_DEEP
                    else 0
                ),
            )

        if result.kind is EndpointAttemptKind.FACILITY_FAIL:
            if not state.facility_redispatch_used:
                state.facility_redispatch_used = True
                if stage is ValidationStage.CANONICAL_DEEP:
                    self._park_deep(state, continuation=True)
                else:
                    self._park_fast(state)
                self._refill_ready_work()
                return
            self._finalize_terminal(
                state,
                self._terminal_from_attempt(
                    state,
                    result,
                    kind=Tier3TerminalKind.FACILITY_FAIL,
                    reason=result.reason or "endpoint_facility_redispatch_exhausted",
                ),
            )
            return

        if (
            not is_user_health
            and stage is ValidationStage.FAST_PROBE
            and result.kind
            not in {
                EndpointAttemptKind.FACILITY_FAIL,
                EndpointAttemptKind.CANCELLED,
            }
        ):
            sink = self.fast_observation_sink
            if sink is not None:
                try:
                    sink(result)
                    self._fast_observation_count += 1
                except Exception:
                    self._fast_observation_publish_failure_count += 1

        if not is_user_health:
            if stage is ValidationStage.FAST_PROBE:
                self._fast_completed_count += 1
                self._fast_completions_since_deep += 1
            else:
                self._deep_completed_count += 1

        confirm_terminal = self._confirm_https_terminal_kind(
            result,
            has_confirm_fact=len(portfolio_facts) < len(result.attempt_facts),
        )
        if confirm_terminal is not None:
            # The portfolio endpoint already PASSED in this slice and the
            # confirm ran with its full canonical-length entitlement, so the
            # canonical ledger has nothing left to learn: replaying it would
            # only re-run the same confirm per endpoint (up to ~5 s each).
            # A completed confirm failure is a verdict (CANDIDATE_FAIL, which
            # the negative cache learns); a confirm timeout stays deferred.
            if endpoint:
                state.endpoint_outcomes[endpoint] = result.kind
            self._finalize_terminal(
                state,
                self._terminal_from_attempt(
                    state,
                    result,
                    kind=confirm_terminal,
                    reason=result.reason,
                ),
            )
            return

        if result.kind is EndpointAttemptKind.PASS:
            if not is_user_health:
                if stage is ValidationStage.FAST_PROBE:
                    self._fast_passed_count += 1
                    if self._first_fast_pass_monotonic_ns <= 0:
                        self._first_fast_pass_monotonic_ns = time.monotonic_ns()
                else:
                    self._deep_passed_count += 1
                    if self._first_deep_pass_monotonic_ns <= 0:
                        self._first_deep_pass_monotonic_ns = time.monotonic_ns()
            if endpoint:
                state.endpoint_outcomes[endpoint] = result.kind
            self._finalize_terminal(
                state,
                self._terminal_from_attempt(
                    state,
                    result,
                    kind=Tier3TerminalKind.PASS,
                    reason=result.reason or "endpoint_probe_passed",
                ),
            )
            return

        if result.kind is EndpointAttemptKind.CANCELLED:
            self._finalize_terminal(
                state,
                self._terminal_from_attempt(
                    state,
                    result,
                    kind=Tier3TerminalKind.CANCELLED,
                    reason=result.reason or "endpoint_attempt_cancelled",
                ),
            )
            return

        if stage is ValidationStage.FAST_PROBE:
            if not is_user_health:
                self._fast_continued_count += 1
            # A three-second FAST observation never consumes the canonical
            # five-second entitlement, regardless of whether it ended in an
            # explicit transient or a right-censor. Endpoint choice for the
            # first canonical item belongs to the shared controller.
            ordered: list[str] = []
            for configured in state.endpoints:
                if configured not in ordered:
                    ordered.append(configured)
            state.canonical_pending = deque(ordered)
            if state.canonical_pending:
                retention = self._retain_deep(state)
                if retention is Tier3DeepRetentionDecision.OVERFLOW_CENSORED:
                    self._finalize_terminal(
                        state,
                        self._terminal_from_attempt(
                            state,
                            result,
                            kind=Tier3TerminalKind.CENSORED,
                            reason="deep_retention_window_full",
                        ),
                    )
                    return
                self._park_deep(state)
                self._publish_continuation(state, retention, result)
                self._refill_ready_work()
                return
        else:
            if endpoint:
                state.endpoint_outcomes[endpoint] = result.kind
            if state.canonical_pending:
                if state.canonical_pending[0] == endpoint:
                    state.canonical_pending.popleft()
                else:
                    try:
                        state.canonical_pending.remove(endpoint)
                    except ValueError:
                        pass
            if state.canonical_pending:
                # Each endpoint is an independent worker entitlement. Keeping
                # the transaction resident here lets a low-yield candidate
                # monopolize a validator through its entire endpoint ledger.
                # Re-park it so fresh FAST work and overdue root/family debt can
                # compete before the next canonical endpoint is dispatched.
                self._park_deep(state, continuation=True)
                self._refill_ready_work()
                return

        all_failed = bool(state.endpoints) and all(
            state.endpoint_outcomes.get(configured)
            is EndpointAttemptKind.CANDIDATE_FAIL
            for configured in state.endpoints
        )
        terminal_kind = (
            Tier3TerminalKind.CANDIDATE_FAIL
            if all_failed
            else Tier3TerminalKind.CENSORED
        )
        self._finalize_terminal(
            state,
            self._terminal_from_attempt(
                state,
                result,
                kind=terminal_kind,
                reason=(
                    "endpoint_ledger_candidate_failed"
                    if all_failed
                    else "endpoint_ledger_transient_or_censored"
                ),
            ),
        )

    def _accept_terminal(self, terminal: ValidationTerminal) -> None:
        if terminal.refresh_epoch != self.refresh_epoch:
            return
        state = self._states.get(terminal.candidate_digest)
        if state is None or not self._matches_active(state, terminal):
            return
        if self._sealed:
            self._cancel_active_result(state, terminal)
            return
        is_user_health = (
            state.envelope.purpose
            is Tier3ValidationPurpose.USER_POOL_HEALTH
        )
        controller = self._endpoint_controller
        if controller is not None and not is_user_health:
            controller.observe(
                terminal,
                root=state.envelope.source_root,
                protocol=state.envelope.source_family,
            )
        self._slice_completed_count += 1
        if not is_user_health:
            self._record_stage_runtime(terminal)
        if terminal.stage is ValidationStage.FAST_PROBE:
            self._release_active_stage(state, terminal.stage)
            if not is_user_health:
                self._fast_completed_count += 1
                self._fast_completions_since_deep += 1
                if terminal.kind is Tier3TerminalKind.PASS:
                    self._fast_passed_count += 1
                    if self._first_fast_pass_monotonic_ns <= 0:
                        self._first_fast_pass_monotonic_ns = time.monotonic_ns()
        else:
            self._release_active_stage(state, terminal.stage)
            if not is_user_health:
                self._deep_completed_count += 1
                if terminal.kind is Tier3TerminalKind.PASS:
                    self._deep_passed_count += 1
                    if self._first_deep_pass_monotonic_ns <= 0:
                        self._first_deep_pass_monotonic_ns = time.monotonic_ns()
        self._finalize_terminal(state, terminal)

    def _finalize_terminal(
        self,
        state: _CandidateState,
        terminal: ValidationTerminal,
    ) -> None:
        self._release_deep_retention(state)
        state.state = "terminal"
        state.active_stage = None
        state.terminal = terminal
        state.active_decision = None
        self._terminal_state_count += 1
        self._release_owner_credit(state.envelope.candidate_digest)
        self._candidate_completed_count += 1
        if self._first_terminal_monotonic_ns <= 0:
            self._first_terminal_monotonic_ns = time.monotonic_ns()
        self._terminal_counts[terminal.kind] += 1
        if state.envelope.purpose is not Tier3ValidationPurpose.USER_POOL_HEALTH:
            self._mark_rolling_terminal(
                terminal.candidate_digest,
                terminal.stage,
            )
        self._refill_ready_work()
        try:
            self.terminal_sink.put_nowait(terminal)
        except queue.Full:
            pass
        if self.transaction_released_sink is not None:
            try:
                self.transaction_released_sink()
            except Exception:
                pass
        if terminal.kind is not Tier3TerminalKind.PASS:
            self._forget_rolling_candidate(terminal.candidate_digest)
            return
        if state.envelope.purpose is Tier3ValidationPurpose.USER_POOL_HEALTH:
            self._forget_rolling_candidate(terminal.candidate_digest)
            return
        intent = AdmissionIntent(
            refresh_epoch=terminal.refresh_epoch,
            candidate_digest=terminal.candidate_digest,
            config_digest=terminal.config_digest,
            lease_id=terminal.lease_id,
            sequence=terminal.sequence,
            worker_id=terminal.worker_id,
            worker_generation=terminal.worker_generation,
            node_json=terminal.node_json,
            report_json=terminal.report_json,
            capacity_epoch_id=state.envelope.capacity_epoch_id,
            latency_ms=terminal.latency_ms,
            slow=terminal.slow,
            probe_url=terminal.probe_url,
            reason=terminal.reason,
            attempt_facts=terminal.attempt_facts,
            probe_policy_revision=terminal.probe_policy_revision,
            portfolio_digest=terminal.portfolio_digest,
            facility_elapsed_ms=terminal.facility_elapsed_ms,
            portfolio_elapsed_ms=terminal.portfolio_elapsed_ms,
            total_elapsed_ms=terminal.total_elapsed_ms,
            clean_confirmed=terminal.clean_confirmed,
            submitted_monotonic_ns=terminal.submitted_monotonic_ns,
            dispatch_monotonic_ns=terminal.dispatch_monotonic_ns,
            supervisor_assigned_monotonic_ns=(
                terminal.supervisor_assigned_monotonic_ns
            ),
            supervisor_sent_monotonic_ns=(
                terminal.supervisor_sent_monotonic_ns
            ),
            helper_started_monotonic_ns=terminal.helper_started_monotonic_ns,
            portfolio_started_monotonic_ns=terminal.portfolio_started_monotonic_ns,
            terminal_created_monotonic_ns=terminal.terminal_created_monotonic_ns,
            transaction_id=terminal.transaction_id,
            slice_id=terminal.slice_id,
            stage=terminal.stage,
            purpose=state.envelope.purpose,
        )
        state.admission_state = "pending"
        self._admission_intents += 1
        if not self.admission_sink(intent):
            self._outbox.append(intent)

    def _flush_outbox(self) -> None:
        if self._sealed:
            self._outbox.clear()
            return
        while self._outbox:
            intent = self._outbox[0]
            if not self.admission_sink(intent):
                return
            self._outbox.popleft()

    def _accept_admission_result(
        self,
        result: AdmissionCommitted | AdmissionRejected,
    ) -> None:
        if result.refresh_epoch != self.refresh_epoch:
            return
        state = self._states.get(result.candidate_digest)
        if state is None or state.lease_id != result.lease_id:
            return
        if state.admission_state not in {"pending", "queued"}:
            return
        if isinstance(result, AdmissionCommitted):
            state.admission_state = "committed"
            self._admission_committed += 1
            if result.net_new_pool_growth:
                self._net_new_admission_committed += 1
                if state.terminal is not None:
                    self._mark_rolling_net_new(
                        state.terminal.candidate_digest,
                        state.terminal.stage,
                    )
                    if state.terminal.stage is ValidationStage.FAST_PROBE:
                        self._fast_net_new_count += 1
                    else:
                        self._deep_net_new_count += 1
                        if (
                            self._deep_zero_yield_latched
                            and not self._deep_probation_active
                        ):
                            self._start_deep_recovery_probation(
                                time.monotonic()
                            )
                    self._refresh_fast_soft_entitlement()
        else:
            state.admission_state = "rejected"
            self._admission_rejected += 1
        self._forget_rolling_candidate(result.candidate_digest)

    def _cancel_queued(self) -> None:
        released = 0
        for state in self._states.values():
            if state.state not in {
                "queued_fast",
                "parked_fast",
                "queued_deep",
                "parked_deep",
            }:
                continue
            was_dispatched = state.slice_id > 0
            if state.state in {"queued_fast", "parked_fast"}:
                self._queued_fast_count = max(0, self._queued_fast_count - 1)
                if state.state == "queued_fast":
                    self._fast_ready_count = max(0, self._fast_ready_count - 1)
                else:
                    self._fast_parked_count = max(0, self._fast_parked_count - 1)
            else:
                self._queued_deep_count = max(0, self._queued_deep_count - 1)
                if state.state == "queued_deep":
                    self._deep_ready_count = max(0, self._deep_ready_count - 1)
                else:
                    self._deep_parked_count = max(0, self._deep_parked_count - 1)
            self._release_deep_retention(state)
            state.state = "terminal"
            terminal = ValidationTerminal(
                refresh_epoch=self.refresh_epoch,
                candidate_digest=state.envelope.candidate_digest,
                config_digest=state.envelope.config_digest,
                lease_id=state.lease_id,
                sequence=state.sequence,
                worker_id=0,
                worker_generation=0,
                kind=Tier3TerminalKind.CANCELLED,
                node_json=state.envelope.node_json,
                transaction_id=state.transaction_id,
                slice_id=state.slice_id,
                stage=(
                    ValidationStage.CANONICAL_DEEP
                    if was_dispatched
                    else ValidationStage.FAST_PROBE
                ),
                purpose=state.envelope.purpose,
                report_json=state.envelope.report_json,
                reason="expansion_sealed_before_dispatch",
                attempt_facts=tuple(state.attempt_facts),
                clean_confirmed=True,
            )
            state.terminal = terminal
            self._terminal_state_count += 1
            self._release_owner_credit(state.envelope.candidate_digest)
            if was_dispatched:
                self._candidate_completed_count += 1
            else:
                self._cancelled_before_dispatch_count += 1
            self._terminal_counts[Tier3TerminalKind.CANCELLED] += 1
            try:
                self.terminal_sink.put_nowait(terminal)
            except queue.Full:
                pass
            released += 1
        for stage in ValidationStage:
            self._root_families[stage].clear()
            self._root_cursor[stage].clear()
            self._family_cursor[stage].clear()
        self._deep_parked_families.clear()
        self._deep_parked_root_cursor.clear()
        self._deep_parked_family_cursor.clear()
        self._deep_escape_parked.clear()
        self._fast_parked_families.clear()
        self._fast_parked_root_cursor.clear()
        self._fast_parked_family_cursor.clear()
        if released and self.transaction_released_sink is not None:
            try:
                self.transaction_released_sink()
            except Exception:
                pass

    def _run(self) -> None:
        try:
            while not self._stopping:
                self._flush_outbox()
                try:
                    _priority, _sequence, kind, payload = self._events.get(
                        timeout=0.05
                    )
                except queue.Empty:
                    self._dispatch()
                    self._publish_snapshot()
                    continue
                try:
                    if kind == "candidate":
                        self._add_candidate(payload)  # type: ignore[arg-type]
                        self._drain_candidate_event_batch()
                    elif kind == "capacity":
                        self._available_capacity = max(
                            0,
                            self._available_capacity + int(payload),
                        )
                    elif kind == "envelope":
                        worker_envelope = max(1, int(payload))
                        if worker_envelope != self._worker_envelope:
                            self._worker_envelope = worker_envelope
                            self._reset_stage_slot_limits_for_envelope()
                            self._actor_owner_limit = max(
                                self._actor_owner_limit,
                                min(256, max(64, 10 * worker_envelope)),
                            )
                            self._ready_work_limit = max(
                                self._active_fast_count + self._active_deep_count,
                                max(8, 2 * worker_envelope),
                            )
                            self._trim_deep_cohort()
                            self._trim_ready_window()
                            self._refill_ready_work()
                    elif kind == "credits":
                        owner_limit, ready_work_limit = payload  # type: ignore[misc]
                        self._actor_owner_limit = max(
                            self._owner_reserved_count,
                            int(owner_limit),
                        )
                        self._ready_work_limit = max(
                            self._active_fast_count + self._active_deep_count,
                            int(ready_work_limit),
                        )
                        self._trim_deep_cohort()
                        self._trim_ready_window()
                        self._refill_ready_work()
                    elif kind == "control":
                        (
                            pressure,
                            dispatch_enabled,
                            external_fresh_backlog,
                            fast_supply_state,
                            actionable_deep,
                            normal_reservable_fast_count,
                            reward_reservable_fast_count,
                            escape_eligible_bucket_count,
                            tcp_handoff_lease_count,
                            expected_responsive_arrivals,
                            source_retry_due_count,
                        ) = payload  # type: ignore[misc]
                        self._control_pressure = max(
                            0.0,
                            min(1.0, float(pressure)),
                        )
                        self._external_fresh_backlog = max(
                            0, int(external_fresh_backlog)
                        )
                        normalized_supply_state = str(fast_supply_state).strip().lower()
                        if normalized_supply_state not in {
                            "ready",
                            "imminent",
                            "blocked",
                            "exhausted",
                        }:
                            normalized_supply_state = "exhausted"
                        self._fast_supply_state = normalized_supply_state
                        self._normal_reservable_fast_count = max(
                            0, int(normal_reservable_fast_count)
                        )
                        self._reward_reservable_fast_count = max(
                            0, int(reward_reservable_fast_count)
                        )
                        self._escape_eligible_bucket_count = max(
                            0, int(escape_eligible_bucket_count)
                        )
                        self._tcp_handoff_lease_count = max(
                            0, int(tcp_handoff_lease_count)
                        )
                        self._expected_responsive_arrivals = max(
                            0, int(expected_responsive_arrivals)
                        )
                        self._source_retry_due_count = max(
                            0, int(source_retry_due_count)
                        )
                        if (
                            normalized_supply_state in {"ready", "imminent"}
                            or self._fresh_upstream_actionable_count() > 0
                        ):
                            self._fast_supply_nonactionable_revisions = 0
                        else:
                            self._fast_supply_nonactionable_revisions += 1
                        self._expected_fast_supply = normalized_supply_state in {
                            "ready",
                            "imminent",
                        }
                        self._actionable_deep = max(0, int(actionable_deep))
                        self._dispatch_enabled = bool(dispatch_enabled)
                        self._refresh_stage_slot_limits()
                        self._trim_deep_cohort()
                        self._refill_ready_work()
                    elif kind == "attempt":
                        self._accept_endpoint_result(payload)  # type: ignore[arg-type]
                    elif kind == "terminal":
                        self._accept_terminal(payload)  # type: ignore[arg-type]
                    elif kind == "admission":
                        self._accept_admission_result(payload)  # type: ignore[arg-type]
                    elif kind == "seal":
                        self._accepting.clear()
                        self._sealed = True
                        self._cancel_queued()
                    elif kind == "stop":
                        self._accepting.clear()
                        self._sealed = True
                        self._cancel_active_on_stop()
                        self._cancel_queued()
                        self._stopping = True
                finally:
                    self._event_processed(kind)
                self._dispatch()
                self._publish_snapshot()
        finally:
            self._accepting.clear()
            self._publish_snapshot()
            self._stopped.set()
