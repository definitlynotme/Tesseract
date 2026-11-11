from __future__ import annotations

import hashlib
import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping, Protocol


@dataclass
class Tier3ControllerDecision:
    intensity: float
    source_budget: int
    collect_interval: int
    error: float
    integral: float


@dataclass(frozen=True)
class Tier3RefreshPlan:
    decision: Tier3ControllerDecision
    target_alive: int
    intensive_expansion: bool
    previous_alive_count: int = 0
    reserve_deficit: int = 0
    reserve_deficit_ratio: float = 0.0
    floor_deficit: bool = False
    validation_budget: Tier3ValidationBudget | None = None


@dataclass(frozen=True)
class Tier3ValidationBudget:
    pi_intensity: float
    expansion_pressure: float
    tier3_throughput_pressure: float
    reserve_deficit: int
    reserve_deficit_ratio: float
    backlog_pressure: float
    recent_pass_rate: float
    pass_rate_pressure: float
    validator_health: float
    recent_negative_skip_rate: float
    negative_cache_pressure: float
    source_budget: int
    validation_budget: int
    validation_session_wave_count: int
    validation_session_budget: int
    validation_batch_size: int
    validation_concurrency: int
    validation_worker_count: int
    mihomo_process_count: int
    proxy_delay_concurrency_total: int
    responsive_queue_drain_budget: int
    source_stream_window: int


@dataclass(frozen=True, slots=True)
class Tier3PipelineWorkSnapshot:
    """One control-revision view of every Tier3 expansion work owner."""

    sampled_monotonic: float
    healthy_count: int
    active_helpers: int
    source_pending_count: int = 0
    source_retry_due_count: int = 0
    source_backlog_depth: int = 0
    source_ingress_depth: int = 0
    source_unit_launch_debt: int = 0
    source_unit_parse_valid_supply: int = 0
    candidate_fresh_backlog: int = 0
    candidate_ready_arm_count: int = 0
    candidate_normal_dispatchable_arm_count: int = 0
    candidate_credit_blocked_arm_count: int = 0
    candidate_escape_overdue_bucket_count: int = 0
    candidate_normal_reservable_count: int = 0
    candidate_reward_reservable_count: int = 0
    candidate_escape_eligible_bucket_count: int = 0
    candidate_reserved_handoff_count: int = 0
    candidate_handoff_window_limit: int = 0
    candidate_tcp_handoff_lease_count: int = 0
    candidate_feedback_only_ticket_count: int = 0
    candidate_expected_responsive_arrivals: int = 0
    candidate_tcp_responsive_probability_lcb: float = 0.25
    candidate_last_real_wave_fill: int = 0
    candidate_active_escape_lease_count: int = 0
    candidate_leaf_confirmation_reservable_count: int = 0
    candidate_leaf_confirmation_outstanding_count: int = 0
    candidate_parent_discovery_reservable_count: int = 0
    candidate_parent_discovery_outstanding_count: int = 0
    candidate_parent_discovery_overdue_rounds: int = 0
    fast_supply_state: str = "exhausted"
    candidate_credit_deadlock: bool = False
    tcp_pending_depth: int = 0
    tcp_scanned_total: int = 0
    tcp_responsive_total: int = 0
    validation_ingress_depth: int = 0
    actor_submitted: int = 0
    actor_terminal: int = 0
    fast_ready: int = 0
    fast_active: int = 0
    deep_ready: int = 0
    deep_parked: int = 0
    deep_active: int = 0
    actor_fast_slots: int = 0
    actor_deep_slots: int = 0
    scheduler_saturation_state: str = "unsaturated"
    exploit_scan_share: float = 0.25
    fast_soft_entitlement_seconds: float = 3.0
    fast_rolling_completions: int = 0
    fast_rolling_net_new: int = 0
    fast_rolling_occupied_ms: int = 0
    fast_rolling_yield_lcb: float = 0.0
    deep_rolling_completions: int = 0
    deep_rolling_net_new: int = 0
    deep_rolling_occupied_ms: int = 0
    deep_rolling_yield_lcb: float = 0.0
    commit_pending: int = 0
    runtime_qualified_count: int | None = None
    hard_resource_guardrail: bool = False

    @property
    def process_work_depth(self) -> int:
        return max(
            0,
            int(self.validation_ingress_depth)
            + int(self.actor_submitted)
            - int(self.actor_terminal),
        )

    @property
    def actionable_deep(self) -> int:
        return max(
            0,
            int(self.deep_ready)
            + int(self.deep_parked)
            + int(self.deep_active),
        )

    @property
    def actionable_work(self) -> bool:
        return any(
            int(value) > 0
            for value in (
                self.source_pending_count,
                self.source_retry_due_count,
                self.source_backlog_depth,
                self.source_ingress_depth,
                self.source_unit_launch_debt,
                self.candidate_fresh_backlog,
                self.tcp_pending_depth,
                self.process_work_depth,
                self.fast_ready,
                self.fast_active,
                self.actionable_deep,
                self.commit_pending,
            )
        )

    @property
    def expected_fast_supply(self) -> bool:
        return str(self.fast_supply_state) in {"ready", "imminent"}


def tier3_pipeline_fast_supply_state(
    scheduler_state: str,
    *,
    downstream_imminent_count: int = 0,
    expected_responsive_arrivals: int = 0,
    free_fast_slots: int = 1,
    actor_fast_ready: int = 0,
) -> str:
    """Merge scheduler reservability with already-dispatched future work.

    Actor-owned FAST work is deliberately excluded: it is current work, not
    evidence that the candidate scheduler can replenish the lane.
    """

    normalized = str(scheduler_state or "").strip().lower()
    if normalized not in {"ready", "imminent", "blocked", "exhausted"}:
        normalized = "exhausted"
    if normalized == "ready" or max(0, int(actor_fast_ready)) > 0:
        return "ready"
    required = max(1, int(free_fast_slots))
    expected = max(0, int(downstream_imminent_count)) + max(
        0,
        int(expected_responsive_arrivals),
    )
    if normalized == "imminent" and expected <= 0:
        expected = required
    if expected >= required:
        return "imminent"
    return normalized


@dataclass(frozen=True)
class Tier3ExpansionEnvelopeDecision:
    revision: int
    healthy_count: int
    min_alive_nodes: int
    target_alive: int
    target_error: float
    critical_error: float
    effective_error: float
    integral: float
    stock_pressure: float
    delivery_debt: float
    service_pressure_floor: float
    pressure: float
    actionable_work: bool
    helper_hold_reason: str
    state: str
    critical: bool
    expansion_active: bool
    maintenance: bool
    source_refill_tokens: int
    source_refill_interval_seconds: int
    tcp_inflight_limit: int
    tcp_transport_timeout_seconds: float
    responsive_backlog_low_water: int
    responsive_backlog_high_water: int
    fresh_supply_low_water: int
    fresh_supply_high_water: int
    scan_token_capacity: int
    scan_token_refill: int
    scan_token_interval_ms: int
    candidate_scan_lifetime_cap_enabled: bool
    resident_transaction_limit: int
    ready_work_limit: int
    desired_helpers: int
    fast_slots: int
    deep_slots: int
    saturation_state: str = "unsaturated"
    exploit_scan_share: float = 0.25
    fast_soft_entitlement_seconds: float = 3.0
    fast_rolling_completions: int = 0
    fast_rolling_net_new: int = 0
    fast_rolling_occupied_ms: int = 0
    fast_rolling_yield_lcb: float = 0.0
    deep_rolling_completions: int = 0
    deep_rolling_net_new: int = 0
    deep_rolling_occupied_ms: int = 0
    deep_rolling_yield_lcb: float = 0.0
    runtime_qualified_count: int = 0
    runtime_qualification_error: float = 0.0
    fresh_candidate_backlog: int = 0
    normal_credit_blocked_arm_count: int = 0
    credit_escape_budget: int = 0
    expected_fast_supply: bool = False
    fast_supply_state: str = "exhausted"
    normal_reservable_fast_count: int = 0
    reward_reservable_fast_count: int = 0
    escape_eligible_bucket_count: int = 0
    active_escape_lease_count: int = 0
    candidate_credit_deadlock: bool = False
    actionable_deep: int = 0
    tcp_responsive_reservation_ratio: float = 1.0
    handoff_window_limit: int = 0
    tcp_responsive_probability_lcb: float = 0.25
    expected_responsive_arrivals: int = 0
    resource_hold_reason: str = ""
    source_unit_budget: int = 6
    source_unit_concurrency: int = 6
    source_unit_transport_limit: int = 24


def tier3_dual_threshold_errors(
    *,
    healthy_count: int,
    min_alive_nodes: int,
    target_alive: int,
) -> tuple[float, float, float]:
    """Return target, critical and combined errors for Tier3 expansion."""

    healthy = max(0, int(healthy_count))
    safety_floor = max(1, int(min_alive_nodes))
    target = max(safety_floor, int(target_alive))
    target_error = max(
        0.0,
        min(1.0, (target - healthy) / float(target)),
    )
    if healthy <= safety_floor:
        critical_error = 1.0
    elif healthy < 2 * safety_floor:
        critical_error = max(
            0.0,
            min(
                1.0,
                (2 * safety_floor - healthy) / float(safety_floor),
            ),
        )
    else:
        critical_error = 0.0
    effective_error = 1.0 - (1.0 - target_error) * (1.0 - critical_error)
    return target_error, critical_error, max(0.0, min(1.0, effective_error))


def tier3_validation_supply_pause_state(
    *,
    currently_paused: bool,
    supply_depth: int,
    low_water: int,
    high_water: int,
) -> bool:
    """Apply hysteresis to the TCP-to-validation supply boundary."""

    high = max(1, int(high_water))
    low = max(0, min(high, int(low_water)))
    depth = max(0, int(supply_depth))
    if currently_paused:
        return depth > low
    return depth >= high


def tier3_operational_validation_supply_watermarks(
    *,
    configured_low_water: int,
    configured_high_water: int,
    operational_helpers: int,
    wave_capacity: int,
    desired_fast_slots: int | None = None,
    active_fast: int = 0,
    active_deep: int = 0,
    tcp_prefetch_ratio: float = 0.0,
) -> tuple[int, int]:
    """Bound fresh supply by validation capacity that can run now.

    ``tcp_prefetch_ratio`` is only non-zero with opt-in TCP breadth. It adds
    ``ceil(operational * ratio)`` of speculative TCP supply above the
    operational FAST slots, to both watermarks, so the TCP needed to keep
    those slots busy is not paused. When the configured high water binds,
    the low water sits one wave below it rather than collapsing onto it, but
    never below the default low water. So the band is one wave wide whenever
    ``configured_high >= operational + wave``, and otherwise equals the
    default band. Both watermarks are always at least the default ones.
    At 0.0 both watermarks are exactly the default formula below.
    """

    configured_high = max(1, int(configured_high_water))
    wave = max(1, int(wave_capacity))
    operational = max(0, int(operational_helpers))
    if desired_fast_slots is not None:
        # DEEP ownership is not usable FAST capacity. Retain already-running
        # FAST carry-over, but do not prefetch for slots borrowed by DEEP.
        operational = min(
            operational,
            max(
                max(0, int(active_fast)),
                min(
                    max(0, int(desired_fast_slots)),
                    max(0, operational - max(0, int(active_deep))),
                ),
            ),
        )
    # One active item per operational FAST slot plus one complete TCP wave keeps
    # helpers fed without hiding newly learned high-yield candidates behind a
    # second full helper cohort. Refill that wave as soon as only the active
    # cohort remains.
    prefetch = 0
    ratio = float(tcp_prefetch_ratio or 0.0)
    if ratio > 0.0 and math.isfinite(ratio):
        prefetch = int(math.ceil(round(operational * ratio, 6)))
    high = min(configured_high, max(wave, operational + prefetch + wave))
    low = min(high, max(0, operational))
    if prefetch > 0:
        # Keep a one-wave band below a configured high that clips the
        # prefetch; low == high would flip the pause state on every call.
        low = max(low, min(high - wave, operational + prefetch))
    return low, high


TIER3_TCP_PREFETCH_RATIO_MAX = 2.0
# Evidence threshold of tier3_tcp_responsive_reservation_ratio at the
# controller's wave capacity of 4; below it the reservation ratio is 1.0.
TIER3_TCP_PREFETCH_MIN_TRIALS = 8


def tier3_tcp_breadth_prefetch_ratio(
    *,
    responsive_reservation_ratio: float,
    tcp_scanned: int,
    tcp_responsive: int,
    tcp_timeout_seconds: float,
    fast_budget_seconds: float,
) -> tuple[float, bool]:
    """Speculative TCP prefetch per operational FAST slot, for opt-in breadth.

    The supply gate counts ``ceil(I * r)`` of ``I`` in-flight TCP candidates
    as future FAST work (``r``: the Wilson upper reservation ratio). Keeping
    ``A`` FAST leases busy needs ``I = A * T_s / (p * T_L)`` in flight, so the
    gate must admit ``A * (r / p) * (T_s / T_L)`` above ``A``. ``p`` is the
    observed TCP responsive share, ``T_s`` the TCP timeout and ``T_L`` the
    FAST budget. Before ``TIER3_TCP_PREFETCH_MIN_TRIALS`` outcomes ``p`` is
    unavailable and 1.0 is used; an observed ``p`` of zero gives the upper
    clamp. The result is clamped to [0, 2]. Returns ``(ratio, p_available)``.
    """

    trials = max(0, int(tcp_scanned))
    successes = max(0, min(trials, int(tcp_responsive)))
    p_available = trials >= TIER3_TCP_PREFETCH_MIN_TRIALS
    fast_budget = float(fast_budget_seconds)
    reservation = float(responsive_reservation_ratio)
    timeout = float(tcp_timeout_seconds)
    if not (
        fast_budget > 0.0
        and math.isfinite(fast_budget)
        and math.isfinite(reservation)
        and math.isfinite(timeout)
    ):
        return 0.0, p_available
    reservation = max(0.0, min(1.0, reservation))
    latency_ratio = max(0.0, timeout) / fast_budget
    if not p_available:
        ratio = reservation * latency_ratio
    elif successes <= 0:
        ratio = TIER3_TCP_PREFETCH_RATIO_MAX if reservation * latency_ratio > 0.0 else 0.0
    else:
        ratio = (reservation / (successes / float(trials))) * latency_ratio
    return max(0.0, min(TIER3_TCP_PREFETCH_RATIO_MAX, ratio)), p_available


def tier3_initial_candidate_coalesce_pending(
    *,
    first_candidate_at: float | None,
    now: float,
    first_tcp_enqueued: bool,
    trusted_replay_inventory: int,
    ready_bucket_count: int,
    wave_capacity: int,
    coalesce_seconds: float = 0.25,
) -> bool:
    """Hold only the first cold TCP fill for bounded structural diversity.

    A fast first source unit must not consume the complete 16-item execution
    window before sibling root/family buckets reach the scheduler. Trusted
    replay is already ordered evidence and bypasses this hold. Single-bucket
    configurations wait at most ``coalesce_seconds`` and then proceed with all
    available capacity.
    """

    if first_tcp_enqueued or int(trusted_replay_inventory) > 0:
        return False
    if first_candidate_at is None:
        return False
    target_buckets = min(4, max(1, int(wave_capacity)))
    if max(0, int(ready_bucket_count)) >= target_buckets:
        return False
    return max(0.0, float(now) - float(first_candidate_at)) < max(
        0.0,
        float(coalesce_seconds),
    )


def tier3_validation_fresh_pipeline_headroom(
    *,
    actor_fresh_depth: int,
    tcp_pending_batches: int,
    tcp_active_batches: int,
    wave_capacity: int,
    high_water: int,
    responsive_reservation_ratio: float | None = None,
    tcp_inflight_nodes: int | None = None,
) -> int:
    """Return real fresh-work headroom across TCP and validation.

    Counting every in-flight TCP candidate as future responsive work starves
    later source families when the real TCP conversion rate is low. Counting
    none of them can overrun the validator ingress when every candidate is
    responsive. Reserve the current session's bounded responsive-rate upper
    estimate instead. Before two waves resolve, reserve the full possible
    arrival count. Completed responsive candidates transfer to FAST ownership.
    """

    high = max(1, int(high_water))
    wave = max(1, int(wave_capacity))
    ratio = (
        1.0
        if responsive_reservation_ratio is None
        else max(1.0 / float(wave), min(1.0, float(responsive_reservation_ratio)))
    )
    inflight_nodes = (
        wave
        * (
            max(0, int(tcp_pending_batches))
            + max(0, int(tcp_active_batches))
        )
        if tcp_inflight_nodes is None
        else max(0, int(tcp_inflight_nodes))
    )
    speculative_responsive = int(math.ceil(inflight_nodes * ratio))
    pipeline_depth = max(0, int(actor_fresh_depth)) + speculative_responsive
    return max(0, high - pipeline_depth)


def tier3_tcp_responsive_reservation_ratio(
    *,
    tcp_scanned: int,
    tcp_responsive: int,
    wave_capacity: int,
) -> float:
    """Return a bounded upper estimate for in-flight TCP conversion.

    Until two waves resolve, every outstanding candidate can still become
    FAST work. A low startup prior would admit several cohorts before the next
    envelope revision can observe the first results. Thereafter a Wilson upper
    bound releases speculative capacity for low-response environments. This is
    a capacity bound, not the separate arrival-probability lower estimate.
    """

    wave = max(1, int(wave_capacity))
    floor = 1.0 / float(wave)
    trials = max(0, int(tcp_scanned))
    successes = max(0, min(trials, int(tcp_responsive)))
    if trials < max(8, 2 * wave):
        return 1.0
    z = 1.96
    z2 = z * z
    proportion = successes / float(trials)
    denominator = 1.0 + z2 / trials
    center = proportion + z2 / (2.0 * trials)
    margin = z * math.sqrt(
        proportion * (1.0 - proportion) / trials
        + z2 / (4.0 * trials * trials)
    )
    upper = (center + margin) / denominator
    return max(floor, min(1.0, upper))


def tier3_tcp_responsive_probability_lcb(
    *,
    tcp_scanned: int,
    tcp_responsive: int,
) -> float:
    """Conservative TCP conversion estimate used only for supply breadth."""

    trials = max(0, int(tcp_scanned))
    successes = max(0, min(trials, int(tcp_responsive)))
    if trials < 8:
        return 0.25
    z = 1.96
    z2 = z * z
    proportion = successes / float(trials)
    denominator = 1.0 + z2 / trials
    center = proportion + z2 / (2.0 * trials)
    margin = z * math.sqrt(
        proportion * (1.0 - proportion) / trials
        + z2 / (4.0 * trials * trials)
    )
    return max(0.0, min(1.0, (center - margin) / denominator))


def tier3_candidate_handoff_window(
    *,
    tcp_inflight_limit: int,
    free_fast_slots: int,
    pressure: float,
    responsive_probability_lcb: float,
) -> int:
    """Size bounded TCP breadth to replenish FAST under low conversion."""

    inflight = max(1, int(tcp_inflight_limit))
    demand = max(inflight, int(free_fast_slots))
    # The caller supplies the 25% prior only while evidence is sparse. Once a
    # rolling Wilson lower bound exists, values below 25% must expand breadth
    # toward the bounded 4N ceiling instead of being hidden by a second floor.
    effective_probability = max(
        1e-6,
        min(1.0, float(responsive_probability_lcb)),
    )
    evidence_target = min(
        4 * inflight,
        max(inflight, int(math.ceil(demand / effective_probability))),
    )
    bounded_pressure = max(0.0, min(1.0, float(pressure)))
    return max(
        inflight,
        min(
            4 * inflight,
            int(round(inflight + (evidence_target - inflight) * bounded_pressure)),
        ),
    )


def tier3_candidate_credit_escape_budget(
    *,
    healthy_count: int,
    target_alive: int,
    fast_slots: int,
    active_fast: int,
    ready_fast: int,
    fresh_candidate_backlog: int,
    ready_arm_count: int,
    normal_dispatchable_arm_count: int,
    credit_blocked_arm_count: int,
    overdue_credit_bucket_count: int = 0,
) -> int:
    """Return a bounded FAST escape budget for overdue credit-blocked strata."""

    # ``active_fast`` and ``ready_fast`` describe the actor after candidates
    # have crossed TCP. They cannot decide whether a newly available TCP
    # handoff opportunity reserves fairness for an overdue source bucket. The
    # caller applies this budget only when a real handoff budget exists.
    del active_fast, ready_fast

    if max(0, int(healthy_count)) >= max(1, int(target_alive)):
        return 0
    backlog = max(0, int(fresh_candidate_backlog))
    ready_arms = max(0, int(ready_arm_count))
    blocked_arms = max(0, int(credit_blocked_arm_count))
    overdue_buckets = max(0, int(overdue_credit_bucket_count))
    saturated = bool(
        ready_arms > 0
        and int(normal_dispatchable_arm_count) <= 0
        and blocked_arms > 0
    )
    if backlog <= 0 or ready_arms <= 0 or blocked_arms <= 0:
        return 0
    if not saturated and overdue_buckets <= 0:
        return 0
    slot_budget = max(1, int(fast_slots))
    return min(
        8,
        max(1, overdue_buckets if overdue_buckets > 0 else blocked_arms),
        max(1, int(math.ceil(slot_budget * 0.15))),
    )


@dataclass(frozen=True)
class Tier3ValidatorEnvelopeDecision:
    desired_count: int
    active_count: int
    primary_slots: int
    canonical_slots: int
    scale_down_streak: int
    retry_slots: int = 0
    canonical_fresh_slots: int = 0
    canonical_resume_slots: int = 0
    allocation_reason: str = "deterministic"
    canary_level: int = 0


@dataclass(frozen=True)
class Tier3LaneServiceMetrics:
    completed_count: int = 0
    definitive_count: int = 0
    pass_count: int = 0
    occupied_ms: float = 0.0
    net_new_count: int | None = None
    lifetime_completed_count: int | None = None

    @property
    def pass_throughput_lcb(self) -> float:
        completed = max(0, int(self.completed_count))
        occupied_seconds = max(0.001, float(self.occupied_ms) / 1000.0)
        if completed <= 0:
            return 0.0
        passed = max(0, min(completed, int(self.pass_count)))
        # Censored/deferred outcomes add occupied exposure but are not entered
        # into any failure counter. This estimates conservative pass events per
        # worker-second, which is the quantity needed for lane allocation.
        return (
            _wilson_lower_bound(passed, completed)
            * completed
            / occupied_seconds
        )

    @property
    def allocation_throughput_lcb(self) -> float:
        completed = max(0, int(self.completed_count))
        occupied_seconds = max(0.001, float(self.occupied_ms) / 1000.0)
        if completed <= 0:
            return 0.0
        yielded = (
            int(self.pass_count)
            if self.net_new_count is None
            else int(self.net_new_count)
        )
        yielded = max(0, min(completed, yielded))
        return _wilson_lower_bound(yielded, completed) * completed / occupied_seconds


def _wilson_lower_bound(successes: int, total: int, *, z: float = 1.96) -> float:
    count = max(0, int(total))
    if count <= 0:
        return 0.0
    success = max(0, min(count, int(successes)))
    rate = success / float(count)
    denominator = 1.0 + (z * z) / count
    centre = rate + (z * z) / (2.0 * count)
    margin = z * math.sqrt(
        (rate * (1.0 - rate) + (z * z) / (4.0 * count)) / count
    )
    return max(0.0, (centre - margin) / denominator)


def tier3_validator_worker_lane(
    decision: Tier3ValidatorEnvelopeDecision,
    worker_index: int,
) -> str | None:
    index = max(1, int(worker_index))
    if index > max(1, int(decision.active_count)):
        return None
    primary_end = max(0, int(decision.primary_slots))
    canonical_end = primary_end + max(0, int(decision.canonical_slots))
    retry_end = canonical_end + max(0, int(decision.retry_slots))
    if index <= primary_end:
        return "primary"
    if index <= canonical_end:
        return "canonical"
    if index <= retry_end:
        return "retry"
    return "primary"


def tier3_validator_worker_dispatch_lane(
    decision: Tier3ValidatorEnvelopeDecision,
    worker_index: int,
) -> str | None:
    """Map a worker to the exact physical validation queue allocation."""

    lane = tier3_validator_worker_lane(decision, worker_index)
    if lane != "canonical":
        return lane
    canonical_offset = max(1, int(worker_index)) - max(
        0,
        int(decision.primary_slots),
    )
    fresh_slots = max(0, int(decision.canonical_fresh_slots))
    resume_slots = max(0, int(decision.canonical_resume_slots))
    if fresh_slots + resume_slots <= 0:
        return "canonical_fresh"
    if canonical_offset <= fresh_slots:
        return "canonical_fresh"
    return "canonical_resume"


def tier3_initial_validation_lane(
    *,
    primary_dispatched: int,
    canonical_dispatched: int,
    canonical_share: float,
) -> str:
    """Keep every first validation attempt on the shared primary ingress."""

    del primary_dispatched, canonical_dispatched, canonical_share
    return "primary"


def tier3_process_envelope_target(
    *,
    cached_target: int,
    risk_desired: int,
    active_count: int,
    interval_due: bool,
    canary_state: str,
) -> tuple[int, bool]:
    """Keep one process target sticky between envelope sampling intervals."""

    active = max(1, int(active_count))
    current_target = max(1, int(cached_target or active))
    desired = max(1, int(risk_desired))
    canary_transition = bool(
        str(canary_state or "").startswith("testing_") and desired != active
    )
    rollback = desired < current_target
    update_target = bool(interval_due or canary_transition or rollback)
    return (desired if update_target else current_target), update_target


def tier3_primary_probe_endpoint(
    probe_urls: list[str],
    *,
    identity: str,
    endpoint_stats: Mapping[str, Mapping[str, Any]],
) -> tuple[str, bool]:
    http_urls = [
        url
        for url in probe_urls
        if str(url).lower().startswith("http://")
    ]
    if not http_urls:
        return "", False
    if len(http_urls) == 1:
        return http_urls[0], False

    sampled = [
        url
        for url in http_urls
        if int(endpoint_stats.get(url, {}).get("attempted", 0) or 0) >= 8
        and int(endpoint_stats.get(url, {}).get("root_count", 0) or 0) >= 2
        and int(endpoint_stats.get(url, {}).get("protocol_count", 0) or 0) >= 2
    ]
    # Until every HTTP endpoint has representative evidence, stable hashing
    # provides deterministic exploration without letting arrival order decide.
    if len(sampled) < len(http_urls):
        digest = hashlib.sha256(
            str(identity or "unknown").encode("utf-8", errors="replace")
        ).digest()
        return http_urls[int.from_bytes(digest[:8], "big") % len(http_urls)], True

    non_degraded = [
        url for url in sampled if not bool(endpoint_stats.get(url, {}).get("limited"))
    ]
    candidates = non_degraded or sampled
    ordered = sorted(
        candidates,
        key=lambda url: float(
            endpoint_stats.get(url, {}).get("success_throughput_lcb", 0.0)
            or 0.0
        ),
        reverse=True,
    )
    digest = hashlib.sha256(
        str(identity or "unknown").encode("utf-8", errors="replace")
    ).digest()
    best_score = float(
        endpoint_stats.get(ordered[0], {}).get("success_throughput_lcb", 0.0)
        or 0.0
    )
    zero_yield_alternatives = [
        url
        for url in http_urls
        if url != ordered[0]
        and int(endpoint_stats.get(url, {}).get("attempted", 0) or 0) >= 32
        and int(endpoint_stats.get(url, {}).get("root_count", 0) or 0) >= 2
        and int(endpoint_stats.get(url, {}).get("protocol_count", 0) or 0) >= 2
        and int(endpoint_stats.get(url, {}).get("success", 0) or 0) <= 0
    ]
    exploration_divisor = (
        8 if best_score > 0.0 and zero_yield_alternatives else 4
    )
    if int.from_bytes(digest[:8], "big") % exploration_divisor != 0:
        return ordered[0], False
    # Keep a bounded 25% exploration share. When only one endpoint is currently
    # non-degraded, half-open traffic goes to the degraded alternatives instead
    # of disabling endpoint preference altogether.
    alternatives = [url for url in http_urls if url != ordered[0]]
    if not alternatives:
        return ordered[0], False
    return (
        alternatives[int.from_bytes(digest[8:16], "big") % len(alternatives)],
        True,
    )


def tier3_probe_portfolio_limited(
    endpoint_stats: Mapping[str, Mapping[str, object]],
) -> bool:
    endpoints = [
        endpoint
        for endpoint in endpoint_stats.values()
        if isinstance(endpoint, Mapping)
    ]
    return bool(endpoints) and all(
        bool(endpoint.get("limited"))
        for endpoint in endpoints
    )


def tier3_canonical_probe_priority(
    probe_urls: list[str],
    *,
    attempted_urls: set[str] | None = None,
    completed_urls: set[str] | None = None,
    exhausted_urls: set[str] | None = None,
    censored_urls: set[str] | None = None,
    endpoint_stats: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    attempted = set(attempted_urls or ())
    completed = set(completed_urls or ())
    exhausted = set(exhausted_urls or ())
    censored = set(censored_urls or ()) - exhausted
    if attempted and not (completed or exhausted or censored):
        # Compatibility for callers created before the attempt-fact contract.
        exhausted = set(attempted)
    consumed = completed | exhausted
    remaining = [url for url in probe_urls if url not in consumed]

    def score(url: str) -> float:
        return float(
            endpoint_stats.get(url, {}).get("success_throughput_lcb", 0.0)
            or 0.0
        )

    http_urls = [url for url in remaining if url.lower().startswith("http://")]
    censored_http = sorted(
        [url for url in http_urls if url in censored],
        key=lambda url: (
            bool(endpoint_stats.get(url, {}).get("limited")),
            -score(url),
            probe_urls.index(url),
        ),
    )
    untried_http = sorted(
        [url for url in http_urls if url not in attempted and url not in censored],
        key=lambda url: (
            bool(endpoint_stats.get(url, {}).get("limited")),
            -score(url),
            probe_urls.index(url),
        ),
    )
    other_http = sorted(
        [url for url in http_urls if url not in censored_http and url not in untried_http],
        key=lambda url: (
            bool(endpoint_stats.get(url, {}).get("limited")),
            -score(url),
            probe_urls.index(url),
        ),
    )
    https_urls = sorted(
        [url for url in remaining if url.lower().startswith("https://")],
        key=lambda url: (
            bool(endpoint_stats.get(url, {}).get("limited")),
            -score(url),
            probe_urls.index(url),
        ),
    )
    ordered: list[str] = []
    positive_censored = [url for url in censored_http if score(url) > 0.0]
    if positive_censored:
        # A primary timeout is right-censoring. When session-wide evidence says
        # that endpoint can pass, give it a full canonical entitlement before
        # spending the slice on a zero-yield alternate or slow HTTPS probe.
        ordered.append(positive_censored[0])
    elif untried_http:
        ordered.append(untried_http[0])
    elif censored_http:
        ordered.append(censored_http[0])
    ordered.extend(url for url in untried_http if url not in ordered)
    ordered.extend(url for url in censored_http if url not in ordered)
    ordered.extend(url for url in other_http if url not in ordered)
    ordered.extend(url for url in https_urls if url not in ordered)
    ordered.extend(url for url in remaining if url not in ordered)
    return ordered


def apply_tier3_bounded_lane_correction(
    baseline: Tier3ValidatorEnvelopeDecision,
    *,
    suggested_primary: int,
    suggested_canonical: int,
    suggested_retry: int,
    correction_fraction: float,
    canonical_fresh_depth: int = 0,
    canonical_resume_depth: int = 0,
) -> Tier3ValidatorEnvelopeDecision:
    active = max(1, int(baseline.active_count))
    fraction = max(0.0, min(0.20, float(correction_fraction)))
    max_moves = int(math.floor(active * fraction))
    if max_moves <= 0:
        return baseline
    primary_floor = int(math.ceil(active * 0.25)) if baseline.primary_slots > 0 else 0
    canonical_floor = int(math.ceil(active * 0.25)) if baseline.canonical_slots > 0 else 0
    retry_cap = (
        active // 8
        if baseline.primary_slots > 0 and baseline.canonical_slots > 0
        else active // 4
    )
    suggested = (
        max(0, int(suggested_primary)),
        max(0, int(suggested_canonical)),
        max(0, int(suggested_retry)),
    )
    best: tuple[int, int, int] | None = None
    best_distance = math.inf
    for primary in range(active + 1):
        for canonical in range(active - primary + 1):
            retry = active - primary - canonical
            if primary < primary_floor or canonical < canonical_floor:
                continue
            if retry > retry_cap:
                continue
            movement = (
                abs(primary - baseline.primary_slots)
                + abs(canonical - baseline.canonical_slots)
                + abs(retry - baseline.retry_slots)
            ) // 2
            if movement > max_moves:
                continue
            distance = (
                abs(primary - suggested[0])
                + abs(canonical - suggested[1])
                + abs(retry - suggested[2])
            )
            if distance < best_distance:
                best_distance = distance
                best = (primary, canonical, retry)
    if best is None:
        return baseline
    primary_slots, canonical_slots, retry_slots = best
    if canonical_slots <= 0:
        fresh_slots = 0
        resume_slots = 0
    elif canonical_fresh_depth > 0 and canonical_resume_depth > 0:
        resume_slots = max(1, int(math.ceil(canonical_slots * 0.25)))
        fresh_slots = canonical_slots - resume_slots
    elif canonical_resume_depth > 0:
        fresh_slots = 0
        resume_slots = canonical_slots
    else:
        fresh_slots = canonical_slots
        resume_slots = 0
    return Tier3ValidatorEnvelopeDecision(
        desired_count=baseline.desired_count,
        active_count=baseline.active_count,
        primary_slots=primary_slots,
        canonical_slots=canonical_slots,
        scale_down_streak=baseline.scale_down_streak,
        retry_slots=retry_slots,
        canonical_fresh_slots=fresh_slots,
        canonical_resume_slots=resume_slots,
        allocation_reason=f"{baseline.allocation_reason}+bounded_learning",
        canary_level=baseline.canary_level,
    )


class Tier3ValidatorEnvelopeController:
    """Apply bounded hysteresis to the PI validator-process target."""

    def __init__(
        self,
        *,
        minimum_count: int = 1,
        maximum_count: int = 16,
        initial_count: int | None = None,
        scale_up_step: int | None = None,
        scale_down_samples: int = 3,
        scale_down_step: int = 2,
    ) -> None:
        self.minimum_count = max(1, int(minimum_count))
        self.maximum_count = max(self.minimum_count, int(maximum_count))
        self.scale_down_samples = max(1, int(scale_down_samples))
        self.scale_up_step = (
            max(1, int(scale_up_step)) if scale_up_step is not None else None
        )
        self.scale_down_step = max(1, int(scale_down_step))
        self.active_count = max(
            self.minimum_count,
            min(
                self.maximum_count,
                int(initial_count) if initial_count is not None else self.minimum_count,
            ),
        )
        self.scale_down_streak = 0
        self._last_decision: Tier3ValidatorEnvelopeDecision | None = None
        self._primary_advantage_streak = 0
        self._canonical_advantage_streak = 0
        self._retry_last_main_completion = 0
        self._last_lane_move_at = float("-inf")
        self._canonical_zero_yield_mode = False
        self._canonical_recovery_slots = 1
        self._last_canonical_recovery_at = float("-inf")
        self._canonical_last_net_new_count = 0
        self._canonical_last_growth_completion = 0

    def update_process_domain(
        self,
        desired_count: int,
        *,
        canary_level: int | None = None,
    ) -> Tier3ValidatorEnvelopeDecision:
        """Update only process capacity; the expansion actor owns all lanes."""
        desired = max(
            self.minimum_count,
            min(self.maximum_count, int(desired_count)),
        )
        if desired > self.active_count:
            self.active_count = (
                min(desired, self.active_count + self.scale_up_step)
                if self.scale_up_step is not None
                else desired
            )
            self.scale_down_streak = 0
        elif desired <= self.active_count - 2:
            self.scale_down_streak += 1
            if self.scale_down_streak >= self.scale_down_samples:
                self.active_count = max(
                    desired,
                    self.active_count - self.scale_down_step,
                )
                self.scale_down_streak = 0
        else:
            self.scale_down_streak = 0
        decision = Tier3ValidatorEnvelopeDecision(
            desired_count=desired,
            active_count=self.active_count,
            primary_slots=self.active_count,
            canonical_slots=0,
            scale_down_streak=self.scale_down_streak,
            retry_slots=0,
            canonical_fresh_slots=0,
            canonical_resume_slots=0,
            allocation_reason="process_domain_actor_drr",
            canary_level=(
                int(canary_level)
                if canary_level is not None
                else self.active_count
            ),
        )
        self._last_decision = decision
        return decision

    def update(
        self,
        desired_count: int,
        *,
        primary_backlog: bool,
        canonical_backlog: bool,
        primary_depth: int | None = None,
        canonical_depth: int | None = None,
        canonical_fresh_depth: int = 0,
        canonical_resume_depth: int = 0,
        canonical_fresh_oldest_wait_ms: float = 0.0,
        canonical_resume_oldest_wait_ms: float = 0.0,
        retry_depth: int = 0,
        primary_oldest_wait_ms: float = 0.0,
        canonical_oldest_wait_ms: float = 0.0,
        retry_oldest_wait_ms: float = 0.0,
        primary_service: Tier3LaneServiceMetrics | None = None,
        canonical_service: Tier3LaneServiceMetrics | None = None,
        canonical_fresh_service: Tier3LaneServiceMetrics | None = None,
        canonical_resume_service: Tier3LaneServiceMetrics | None = None,
        retry_service: Tier3LaneServiceMetrics | None = None,
        main_completion_count: int = 0,
        canary_level: int | None = None,
        now_monotonic: float | None = None,
    ) -> Tier3ValidatorEnvelopeDecision:
        current_time = (
            time.monotonic()
            if now_monotonic is None
            else float(now_monotonic)
        )
        desired = max(
            self.minimum_count,
            min(self.maximum_count, int(desired_count)),
        )
        if desired > self.active_count:
            self.active_count = min(
                desired,
                self.active_count + self.scale_up_step,
            ) if self.scale_up_step is not None else desired
            self.scale_down_streak = 0
        elif desired <= self.active_count - 2:
            self.scale_down_streak += 1
            if self.scale_down_streak >= self.scale_down_samples:
                self.active_count = max(
                    desired,
                    self.active_count - self.scale_down_step,
                )
                self.scale_down_streak = 0
        else:
            self.scale_down_streak = 0

        active = self.active_count
        primary_depth = max(
            0,
            int(primary_depth if primary_depth is not None else int(primary_backlog)),
        )
        canonical_depth = max(
            0,
            int(canonical_depth if canonical_depth is not None else int(canonical_backlog)),
        )
        retry_depth = max(0, int(retry_depth))
        primary_backlog = bool(primary_backlog or primary_depth > 0)
        canonical_backlog = bool(canonical_backlog or canonical_depth > 0)

        primary_slots = 0
        canonical_slots = 0
        retry_slots = 0
        allocation_reason = "work_steal_primary"
        if primary_backlog and not canonical_backlog:
            primary_slots = active
            allocation_reason = "primary_only"
        elif canonical_backlog and not primary_backlog:
            canonical_slots = active
            allocation_reason = "canonical_only"
        elif primary_backlog and canonical_backlog and active > 1:
            retry_cap = min(
                max(0, active - 2),
                active // 8 if retry_depth > 0 else 0,
            )
            retry_metrics = retry_service or Tier3LaneServiceMetrics()
            main_completed = max(0, int(main_completion_count))
            retry_evidence = bool(
                retry_metrics.completed_count >= 8
                and retry_metrics.pass_throughput_lcb > 0.0
            )
            retry_service_due = bool(
                retry_depth > 0
                and (
                    retry_oldest_wait_ms >= 12_000.0
                    or main_completed - self._retry_last_main_completion >= 16
                )
            )
            if retry_depth > 0 and active >= 8 and retry_evidence:
                retry_slots = max(1, retry_cap)
            elif retry_depth > 0 and active >= 2 and retry_service_due:
                retry_slots = 1
                self._retry_last_main_completion = main_completed
            main_slots = max(2, active - retry_slots)
            primary_metrics = primary_service or Tier3LaneServiceMetrics()
            canonical_metrics = canonical_service or Tier3LaneServiceMetrics()
            evidence_poor = bool(
                primary_metrics.pass_count < 3
                or canonical_metrics.pass_count < 3
            )
            if evidence_poor:
                # Fresh candidates are always primary work. Before both lanes
                # have reliable pass evidence, preserve at least half of the
                # workers for new candidates and a quarter for continuation.
                primary_floor = max(1, int(math.ceil(main_slots * 0.50)))
                canonical_floor = max(1, int(math.ceil(main_slots * 0.25)))
            else:
                primary_floor = max(1, int(math.ceil(main_slots * 0.25)))
                canonical_floor = max(1, int(math.ceil(main_slots * 0.25)))
            primary_ceiling = max(primary_floor, main_slots - canonical_floor)
            initial_primary = max(
                primary_floor,
                min(primary_ceiling, int(round(main_slots * 0.60))),
            )
            if evidence_poor:
                primary_floor = max(primary_floor, initial_primary - 2)
                primary_ceiling = min(primary_ceiling, initial_primary + 2)
            primary_slots = initial_primary
            allocation_reason = "dual_lane_60_40"

            previous = self._last_decision
            previous_had_both_main_lanes = bool(
                previous is not None
                and previous.active_count == active
                and previous.primary_slots > 0
                and previous.canonical_slots > 0
            )
            if previous_had_both_main_lanes:
                previous_primary = max(
                    primary_floor,
                    min(primary_ceiling, int(previous.primary_slots)),
                )
                primary_slots = previous_primary

            enough_service_evidence = (
                primary_metrics.completed_count >= 16
                and canonical_metrics.completed_count >= 16
                and primary_metrics.occupied_ms >= 30_000.0
                and canonical_metrics.occupied_ms >= 30_000.0
            )
            primary_score = primary_metrics.allocation_throughput_lcb
            canonical_score = canonical_metrics.allocation_throughput_lcb
            wait_advantage = ""
            if primary_oldest_wait_ms >= canonical_oldest_wait_ms + 1000.0:
                wait_advantage = "primary"
            elif canonical_oldest_wait_ms >= primary_oldest_wait_ms + 1000.0:
                wait_advantage = "canonical"

            if enough_service_evidence and canonical_score > primary_score * 1.25:
                self._canonical_advantage_streak += 1
                self._primary_advantage_streak = 0
            elif enough_service_evidence and primary_score > canonical_score * 1.25:
                self._primary_advantage_streak += 1
                self._canonical_advantage_streak = 0
            elif wait_advantage == "canonical":
                self._canonical_advantage_streak += 1
                self._primary_advantage_streak = 0
            elif wait_advantage == "primary":
                self._primary_advantage_streak += 1
                self._canonical_advantage_streak = 0
            else:
                self._primary_advantage_streak = 0
                self._canonical_advantage_streak = 0

            lane_move_allowed = current_time - self._last_lane_move_at >= 1.0
            if (
                lane_move_allowed
                and self._canonical_advantage_streak >= 2
                and primary_slots > primary_floor
            ):
                primary_slots -= 1
                self._canonical_advantage_streak = 0
                self._last_lane_move_at = current_time
                allocation_reason = "canonical_yield_or_wait_advantage"
            elif (
                lane_move_allowed
                and self._primary_advantage_streak >= 2
                and primary_slots < primary_ceiling
            ):
                primary_slots += 1
                self._primary_advantage_streak = 0
                self._last_lane_move_at = current_time
                allocation_reason = "primary_yield_or_wait_advantage"
            canonical_slots = max(0, main_slots - primary_slots)
            canonical_net_new = (
                int(canonical_metrics.pass_count)
                if canonical_metrics.net_new_count is None
                else int(canonical_metrics.net_new_count)
            )
            canonical_lifetime_completed = (
                int(canonical_metrics.completed_count)
                if canonical_metrics.lifetime_completed_count is None
                else int(canonical_metrics.lifetime_completed_count)
            )
            if canonical_net_new > self._canonical_last_net_new_count:
                self._canonical_last_net_new_count = canonical_net_new
                self._canonical_last_growth_completion = canonical_lifetime_completed
            primary_net_new = (
                int(primary_metrics.pass_count)
                if primary_metrics.net_new_count is None
                else int(primary_metrics.net_new_count)
            )
            canonical_probation = bool(
                primary_net_new >= 1
                and primary_score > 0.0
                and canonical_net_new <= 0
                and canonical_metrics.completed_count >= 4
                and canonical_metrics.occupied_ms >= 20_000.0
            )
            canonical_zero_yield = bool(
                canonical_lifetime_completed
                - self._canonical_last_growth_completion
                >= 32
                and canonical_metrics.occupied_ms >= 60_000.0
            )
            if canonical_zero_yield or canonical_probation:
                self._canonical_zero_yield_mode = True
                self._canonical_recovery_slots = 1
            if self._canonical_zero_yield_mode:
                if canonical_net_new >= 3 and canonical_score > 0.0:
                    self._canonical_zero_yield_mode = False
                else:
                    if (
                        canonical_net_new > 0
                        and current_time - self._last_canonical_recovery_at >= 1.0
                    ):
                        self._canonical_recovery_slots = min(
                            canonical_slots,
                            self._canonical_recovery_slots + 1,
                        )
                        self._last_canonical_recovery_at = current_time
                    debt_slots = max(1, self._canonical_recovery_slots)
                    shifted = max(0, canonical_slots - debt_slots)
                    canonical_slots -= shifted
                    primary_slots += shifted
                    allocation_reason = (
                        "canonical_zero_yield_debt"
                        if canonical_zero_yield
                        else "canonical_yield_probation"
                        if canonical_net_new <= 0
                        else "canonical_yield_recovery"
                    )
            if retry_slots > 0:
                allocation_reason += "+retry_evidence" if retry_evidence else "+retry_service_debt"
        elif retry_depth > 0:
            # Keep the deterministic retry quota bounded. Idle main-lane
            # workers may still work-steal retry work.
            retry_slots = max(1, min(active, active // 4 or 1))
            primary_slots = max(0, active - retry_slots)
            allocation_reason = "retry_only_work_steal"
        else:
            primary_slots = active

        fresh_depth = max(0, int(canonical_fresh_depth))
        resume_depth = max(0, int(canonical_resume_depth))
        resume_metrics = canonical_resume_service or Tier3LaneServiceMetrics()
        resume_has_yield_evidence = bool(
            resume_metrics.completed_count >= 8
            and resume_metrics.pass_throughput_lcb > 0.0
        )
        resume_zero_yield = bool(
            resume_metrics.completed_count >= 32
            and resume_metrics.occupied_ms >= 60_000.0
            and resume_metrics.pass_count <= 0
        )
        if (
            primary_backlog
            and resume_depth > 0
            and fresh_depth <= 0
            and not resume_has_yield_evidence
            and canonical_slots > 0
        ):
            resume_cap = max(1, int(math.floor(active * 0.25)))
            shifted = max(0, canonical_slots - resume_cap)
            canonical_slots -= shifted
            primary_slots += shifted
            if shifted:
                allocation_reason = "canonical_deep_service_debt"
        if canonical_slots <= 0:
            canonical_fresh_slots = 0
            canonical_resume_slots = 0
        elif fresh_depth > 0 and resume_depth > 0:
            resume_overdue = bool(
                canonical_resume_oldest_wait_ms >= 12_000.0
                and canonical_resume_oldest_wait_ms
                >= canonical_fresh_oldest_wait_ms + 1_000.0
            )
            resume_share = 0.50 if resume_overdue and resume_has_yield_evidence else 0.25
            canonical_resume_slots = max(
                1,
                int(math.ceil(canonical_slots * resume_share)),
            )
            if not resume_has_yield_evidence or resume_zero_yield:
                canonical_resume_slots = min(
                    canonical_resume_slots,
                    max(1, int(math.floor(active * 0.25))),
                )
            canonical_fresh_slots = canonical_slots - canonical_resume_slots
        elif resume_depth > 0:
            canonical_resume_slots = canonical_slots
            canonical_fresh_slots = 0
        else:
            canonical_fresh_slots = canonical_slots
            canonical_resume_slots = 0

        decision = Tier3ValidatorEnvelopeDecision(
            desired_count=desired,
            active_count=active,
            primary_slots=primary_slots,
            canonical_slots=canonical_slots,
            scale_down_streak=self.scale_down_streak,
            retry_slots=retry_slots,
            canonical_fresh_slots=canonical_fresh_slots,
            canonical_resume_slots=canonical_resume_slots,
            allocation_reason=allocation_reason,
            canary_level=max(1, int(canary_level or active)),
        )
        self._last_decision = decision
        return decision


class Tier3RefreshController(Protocol):
    config: dict[str, Any]
    last_decision: Tier3ControllerDecision

    def evaluate(
        self,
        *,
        alive_count: int,
        target_alive: int,
        min_interval: int,
        max_interval: int,
        min_alive_nodes: int | None = None,
        now_monotonic: float | None = None,
    ) -> Tier3ControllerDecision:
        ...

    def to_dict(self) -> dict[str, Any]:
        ...


def _coerce_int(value: Any, default: int, *, minimum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = int(default)
    if minimum is not None:
        result = max(minimum, result)
    return result


def _coerce_float(
    value: Any,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = float(default)
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def _interpolated_int(
    *,
    minimum_value: Any,
    maximum_value: Any,
    pressure: float,
    default_minimum: int,
    default_maximum: int,
) -> int:
    lower = _coerce_int(minimum_value, default_minimum, minimum=1)
    upper = max(lower, _coerce_int(maximum_value, default_maximum, minimum=lower))
    clamped_pressure = max(0.0, min(1.0, pressure))
    return max(lower, min(upper, int(round(lower + (upper - lower) * clamped_pressure))))


def tier3_validation_budget(
    *,
    decision: Tier3ControllerDecision,
    alive_count: int,
    target_alive: int,
    backlog_count: int = 0,
    recent_pass_rate: float | None = None,
    recent_validator_health: float | None = None,
    recent_negative_skip_rate: float | None = None,
    source_budget_min: int | None = None,
    source_budget_max: int | None = None,
    validation_candidate_limit: int,
    validation_empty_retry_limit: int,
    validation_budget_multiplier: float,
    validation_batch_size_min: int,
    validation_batch_size_max: int,
    validation_concurrency_min: int,
    validation_concurrency_max: int,
    validation_worker_min: int,
    validation_worker_max: int,
    source_stream_window_min: int,
    source_stream_window_max: int,
    validation_session_wave_min: int = 1,
    validation_session_wave_max: int = 1,
    mihomo_process_min: int = 1,
    mihomo_process_max: int = 4,
    proxy_delay_concurrency_min: int | None = None,
    proxy_delay_concurrency_max: int = 96,
) -> Tier3ValidationBudget:
    alive = _coerce_int(alive_count, 0, minimum=0)
    target = _coerce_int(target_alive, 1, minimum=1)
    reserve_deficit = max(0, target - alive)
    reserve_deficit_ratio = round(reserve_deficit / float(target), 4)
    intensity = max(0.0, min(1.0, _coerce_float(getattr(decision, "intensity", 0.0), 0.0)))
    positive_error = max(0.0, min(1.0, _coerce_float(getattr(decision, "error", reserve_deficit_ratio), reserve_deficit_ratio)))
    pi_pressure = intensity * positive_error
    backlog = _coerce_int(backlog_count, 0, minimum=0)
    backlog_pressure = reserve_deficit_ratio * (backlog / float(backlog + target))
    if recent_pass_rate is None:
        pass_rate = 1.0
        pass_rate_pressure = 0.0
    else:
        pass_rate = max(0.0, min(1.0, _coerce_float(recent_pass_rate, 1.0)))
        pass_rate_pressure = reserve_deficit_ratio * (1.0 - pass_rate)
    if recent_validator_health is None:
        validator_health = 1.0
    else:
        validator_health = max(0.0, min(1.0, _coerce_float(recent_validator_health, 1.0)))
    if recent_negative_skip_rate is None:
        negative_skip_rate = 0.0
        negative_cache_pressure = 0.0
    else:
        negative_skip_rate = max(0.0, min(1.0, _coerce_float(recent_negative_skip_rate, 0.0)))
        negative_cache_pressure = reserve_deficit_ratio * negative_skip_rate
    pressure_inputs = (
        max(0.0, min(1.0, pi_pressure)),
        max(0.0, min(1.0, reserve_deficit_ratio)),
        max(0.0, min(1.0, backlog_pressure)),
        max(0.0, min(1.0, pass_rate_pressure)),
        max(0.0, min(1.0, negative_cache_pressure)),
    )
    remaining_headroom = 1.0
    for input_pressure in pressure_inputs:
        remaining_headroom *= 1.0 - input_pressure
    pressure = max(0.0, min(1.0, 1.0 - remaining_headroom))
    source_floor = _coerce_int(
        source_budget_min if source_budget_min is not None else getattr(decision, "source_budget", 1),
        getattr(decision, "source_budget", 1),
        minimum=1,
    )
    source_ceiling = max(
        source_floor,
        _coerce_int(
            source_budget_max if source_budget_max is not None else getattr(decision, "source_budget", source_floor),
            getattr(decision, "source_budget", source_floor),
            minimum=source_floor,
        ),
    )
    source_budget = _interpolated_int(
        minimum_value=source_floor,
        maximum_value=source_ceiling,
        pressure=pressure,
        default_minimum=source_floor,
        default_maximum=source_ceiling,
    )
    candidate_floor = _coerce_int(validation_candidate_limit, 1, minimum=1)
    multiplier = _coerce_float(validation_budget_multiplier, 1.0, minimum=1.0)
    target_scaled_limit = int(round(target * multiplier))
    hard_limit = max(
        candidate_floor,
        _coerce_int(validation_empty_retry_limit, candidate_floor, minimum=candidate_floor),
        target_scaled_limit,
    )
    budget = _interpolated_int(
        minimum_value=candidate_floor,
        maximum_value=hard_limit,
        pressure=pressure,
        default_minimum=candidate_floor,
        default_maximum=hard_limit,
    )
    wave_count = _interpolated_int(
        minimum_value=validation_session_wave_min,
        maximum_value=validation_session_wave_max,
        pressure=pressure,
        default_minimum=1,
        default_maximum=max(1, validation_session_wave_max),
    )
    session_budget = max(budget, budget * wave_count)
    concurrency = _interpolated_int(
        minimum_value=validation_concurrency_min,
        maximum_value=validation_concurrency_max,
        pressure=pressure,
        default_minimum=1,
        default_maximum=max(1, validation_concurrency_max),
    )
    batch_size = _interpolated_int(
        minimum_value=max(validation_batch_size_min, concurrency),
        maximum_value=max(validation_batch_size_max, concurrency),
        pressure=pressure,
        default_minimum=max(1, concurrency),
        default_maximum=max(1, validation_batch_size_max, concurrency),
    )
    worker_count = _interpolated_int(
        minimum_value=validation_worker_min,
        maximum_value=validation_worker_max,
        pressure=pressure,
        default_minimum=1,
        default_maximum=max(1, validation_worker_max),
    )
    mihomo_process_count = _interpolated_int(
        minimum_value=mihomo_process_min,
        maximum_value=mihomo_process_max,
        pressure=pressure,
        default_minimum=1,
        default_maximum=max(1, mihomo_process_max),
    )
    proxy_delay_concurrency_floor = (
        validation_concurrency_min
        if proxy_delay_concurrency_min is None
        else proxy_delay_concurrency_min
    )
    proxy_delay_concurrency_total = _interpolated_int(
        minimum_value=proxy_delay_concurrency_floor,
        maximum_value=max(proxy_delay_concurrency_floor, proxy_delay_concurrency_max),
        pressure=pressure,
        default_minimum=max(1, validation_concurrency_min),
        default_maximum=max(1, proxy_delay_concurrency_max),
    )
    responsive_queue_drain_budget = _interpolated_int(
        minimum_value=batch_size,
        maximum_value=max(batch_size, session_budget),
        pressure=pressure,
        default_minimum=max(1, batch_size),
        default_maximum=max(1, session_budget),
    )
    stream_window = _interpolated_int(
        minimum_value=source_stream_window_min,
        maximum_value=source_stream_window_max,
        pressure=pressure,
        default_minimum=max(1, source_stream_window_min),
        default_maximum=max(1, source_stream_window_max),
    )
    return Tier3ValidationBudget(
        pi_intensity=round(intensity, 4),
        expansion_pressure=round(pressure, 4),
        tier3_throughput_pressure=round(pressure, 4),
        reserve_deficit=reserve_deficit,
        reserve_deficit_ratio=reserve_deficit_ratio,
        backlog_pressure=round(backlog_pressure, 4),
        recent_pass_rate=round(pass_rate, 4),
        pass_rate_pressure=round(pass_rate_pressure, 4),
        validator_health=round(validator_health, 4),
        recent_negative_skip_rate=round(negative_skip_rate, 4),
        negative_cache_pressure=round(negative_cache_pressure, 4),
        source_budget=source_budget,
        validation_budget=budget,
        validation_session_wave_count=wave_count,
        validation_session_budget=session_budget,
        validation_batch_size=batch_size,
        validation_concurrency=concurrency,
        validation_worker_count=worker_count,
        mihomo_process_count=mihomo_process_count,
        proxy_delay_concurrency_total=proxy_delay_concurrency_total,
        responsive_queue_drain_budget=responsive_queue_drain_budget,
        source_stream_window=stream_window,
    )


def tier3_refresh_plan(
    *,
    previous_alive_count: int,
    target_alive: int,
    min_alive_nodes: int | None = None,
    base_decision: Tier3ControllerDecision,
) -> Tier3RefreshPlan:
    previous_alive = _coerce_int(previous_alive_count, 0, minimum=0)
    target = _coerce_int(target_alive, 1, minimum=1)
    floor_threshold = _coerce_int(
        min_alive_nodes,
        target,
        minimum=1,
    ) if min_alive_nodes is not None else target
    floor_deficit = previous_alive < floor_threshold
    reserve_deficit = max(0, target - previous_alive)
    reserve_deficit_ratio = round(reserve_deficit / float(target), 4)
    intensive_expansion = reserve_deficit > 0
    return Tier3RefreshPlan(
        decision=base_decision,
        target_alive=target,
        intensive_expansion=intensive_expansion,
        previous_alive_count=previous_alive,
        reserve_deficit=reserve_deficit,
        reserve_deficit_ratio=reserve_deficit_ratio,
        floor_deficit=floor_deficit,
    )


class Tier3PIController:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self._lock = threading.Lock()
        self.integral = 0.0
        self._last_evaluated_at: float | None = None
        self._last_target_error = 0.0
        self._last_critical_error = 0.0
        self._last_effective_error = 0.0
        self._last_critical_state = "maintenance"
        self._envelope_revision = 0
        self._last_envelope_helpers = 0
        self._last_actionable_envelope_pressure = 0.0
        self._envelope_scale_down_streak = 0
        self._last_evaluation_key: tuple[Any, ...] | None = None
        self._last_evaluation_result_at: float | None = None
        self._last_envelope_key: tuple[Any, ...] | None = None
        self._last_envelope_result_at: float | None = None
        self._last_envelope_decision: Tier3ExpansionEnvelopeDecision | None = None
        self.last_decision = Tier3ControllerDecision(
            intensity=0.0,
            source_budget=_coerce_int(config.get("source_budget_min", 1), 1, minimum=1),
            collect_interval=3600,
            error=0.0,
            integral=0.0,
        )

    def evaluate(
        self,
        *,
        alive_count: int,
        target_alive: int,
        min_interval: int,
        max_interval: int,
        min_alive_nodes: int | None = None,
        now_monotonic: float | None = None,
    ) -> Tier3ControllerDecision:
        with self._lock:
            return self._evaluate_locked(
                alive_count=alive_count,
                target_alive=target_alive,
                min_interval=min_interval,
                max_interval=max_interval,
                min_alive_nodes=min_alive_nodes,
                now_monotonic=now_monotonic,
            )

    def _evaluate_locked(
        self,
        *,
        alive_count: int,
        target_alive: int,
        min_interval: int,
        max_interval: int,
        min_alive_nodes: int | None,
        now_monotonic: float | None,
    ) -> Tier3ControllerDecision:
        configured_target = _coerce_int(
            self.config.get("target_alive", target_alive),
            target_alive,
            minimum=1,
        )
        safety_floor = (
            max(1, _coerce_int(min_alive_nodes, 1, minimum=1))
            if min_alive_nodes is not None
            else 1
        )
        target = max(safety_floor, configured_target)
        healthy = max(0, int(alive_count))
        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        evaluation_key = (
            healthy,
            target,
            safety_floor if min_alive_nodes is not None else None,
            max(1, int(min_interval)),
            max(1, int(max_interval)),
            repr(sorted(self.config.items())),
        )
        if (
            self._last_evaluation_key == evaluation_key
            and self._last_evaluation_result_at is not None
            and 0.0 <= now - self._last_evaluation_result_at < 0.25
        ):
            return self.last_decision
        if min_alive_nodes is None:
            target_error = max(
                0.0,
                min(1.0, (target - healthy) / float(target)),
            )
            critical_error = 0.0
            effective_error = target_error
        else:
            target_error, critical_error, effective_error = tier3_dual_threshold_errors(
                healthy_count=healthy,
                min_alive_nodes=safety_floor,
                target_alive=target,
            )
        if self._last_evaluated_at is None:
            dt = 1.0
        else:
            dt = max(0.0, min(5.0, now - self._last_evaluated_at))
        self._last_evaluated_at = now
        integral_decay = _coerce_float(
            self.config.get("integral_decay", 0.85),
            0.85,
            minimum=0.0,
            maximum=1.0,
        )
        if effective_error <= 0.0:
            self.integral *= integral_decay ** max(0.001, dt)
        else:
            self.integral = min(1.0, self.integral + effective_error * dt)
        self.integral = max(0.0, min(1.0, self.integral))
        if abs(self.integral) < 0.0001:
            self.integral = 0.0
        kp = _coerce_float(self.config.get("kp", 0.85), 0.85)
        ki = _coerce_float(self.config.get("ki", 0.15), 0.15)
        min_intensity = _coerce_float(
            self.config.get("min_intensity", 0.12),
            0.12,
            minimum=0.0,
            maximum=1.0,
        )
        max_intensity = _coerce_float(
            self.config.get("max_intensity", 1.0),
            1.0,
            minimum=0.0,
            maximum=1.0,
        )
        intensity_signal = kp * effective_error + ki * self.integral
        if healthy >= target:
            intensity = 0.0
        elif min_alive_nodes is not None and healthy <= safety_floor:
            intensity = max_intensity
        else:
            intensity = max(
                min_intensity,
                min(max_intensity, intensity_signal),
            )
        budget_min = _coerce_int(self.config.get("source_budget_min", 1), 1, minimum=1)
        budget_max = max(budget_min, _coerce_int(self.config.get("source_budget_max", 12), 12))
        source_budget = int(round(budget_min + (budget_max - budget_min) * intensity))
        lower_interval = _coerce_int(min_interval, 1, minimum=1)
        upper_interval = _coerce_int(max_interval, lower_interval, minimum=1)
        interval_span = max(0, upper_interval - lower_interval)
        collect_interval = int(upper_interval - interval_span * intensity)
        decision = Tier3ControllerDecision(
            intensity=round(intensity, 4),
            source_budget=max(budget_min, min(budget_max, source_budget)),
            collect_interval=max(1, collect_interval),
            error=round(effective_error, 4),
            integral=round(self.integral, 4),
        )
        self._last_target_error = target_error
        self._last_critical_error = critical_error
        self._last_effective_error = effective_error
        if healthy >= target:
            self._last_critical_state = "maintenance"
        elif healthy < 2 * safety_floor and min_alive_nodes is not None:
            self._last_critical_state = "critical"
        else:
            self._last_critical_state = "expansion"
        self.last_decision = decision
        self._last_evaluation_key = evaluation_key
        self._last_evaluation_result_at = now
        return decision

    def expansion_envelope(
        self,
        *,
        healthy_count: int,
        min_alive_nodes: int,
        target_alive: int,
        source_budget_min: int,
        source_budget_max: int,
        collect_interval_min: int,
        collect_interval_max: int,
        helper_min: int,
        helper_max: int,
        active_helpers: int,
        tcp_inflight_min: int,
        tcp_inflight_max: int,
        actionable_work: bool = False,
        supply_exhausted: bool = False,
        hard_resource_guardrail: bool = False,
        last_net_new_commit_monotonic: float | None = None,
        scan_session_budget: int | None = None,
        facility_deadline_seconds: float = 2.0,
        fast_probe_entitlement_seconds: float = 3.0,
        delivery_slack_seconds: float = 0.75,
        tcp_transport_base_timeout_seconds: float = 5.0,
        tcp_transport_timeout_floor_seconds: float = 1.25,
        now_monotonic: float | None = None,
        work_snapshot: Tier3PipelineWorkSnapshot | None = None,
    ) -> Tier3ExpansionEnvelopeDecision:
        with self._lock:
            if work_snapshot is not None:
                healthy_count = int(work_snapshot.healthy_count)
                active_helpers = max(1, int(work_snapshot.active_helpers))
                actionable_work = bool(work_snapshot.actionable_work)
                supply_exhausted = not actionable_work
                hard_resource_guardrail = bool(
                    work_snapshot.hard_resource_guardrail
                )
                if now_monotonic is None:
                    now_monotonic = float(work_snapshot.sampled_monotonic)
            now = time.monotonic() if now_monotonic is None else float(now_monotonic)
            envelope_key = (
                int(healthy_count),
                int(min_alive_nodes),
                int(target_alive),
                int(source_budget_min),
                int(source_budget_max),
                int(collect_interval_min),
                int(collect_interval_max),
                int(helper_min),
                int(helper_max),
                int(active_helpers),
                int(tcp_inflight_min),
                int(tcp_inflight_max),
                bool(actionable_work),
                bool(supply_exhausted),
                bool(hard_resource_guardrail),
                (
                    None
                    if last_net_new_commit_monotonic is None
                    else round(float(last_net_new_commit_monotonic), 6)
                ),
                None if scan_session_budget is None else int(scan_session_budget),
                round(float(facility_deadline_seconds), 6),
                round(float(fast_probe_entitlement_seconds), 6),
                round(float(delivery_slack_seconds), 6),
                round(float(tcp_transport_base_timeout_seconds), 6),
                round(float(tcp_transport_timeout_floor_seconds), 6),
                work_snapshot,
                repr(sorted(self.config.items())),
            )
            if (
                self._last_envelope_key == envelope_key
                and self._last_envelope_result_at is not None
                and self._last_envelope_decision is not None
                and 0.0 <= now - self._last_envelope_result_at < 0.25
            ):
                return self._last_envelope_decision
            decision = self._evaluate_locked(
                alive_count=healthy_count,
                target_alive=target_alive,
                min_interval=collect_interval_min,
                max_interval=collect_interval_max,
                min_alive_nodes=min_alive_nodes,
                now_monotonic=now_monotonic,
            )
            healthy = max(0, int(healthy_count))
            safety_floor = max(1, int(min_alive_nodes))
            target = max(safety_floor, int(target_alive))
            stock_pressure = max(0.0, min(1.0, float(decision.intensity)))
            runtime_qualified_count = (
                None
                if work_snapshot is None
                else work_snapshot.runtime_qualified_count
            )
            runtime_qualification_error = 0.0
            if runtime_qualified_count is not None:
                runtime_qualification_error = max(
                    0.0,
                    min(
                        1.0,
                        (safety_floor - max(0, int(runtime_qualified_count)))
                        / float(safety_floor),
                    ),
                )
                stock_pressure = max(
                    stock_pressure,
                    runtime_qualification_error,
                )
            maintenance = healthy >= target
            expansion_active = not maintenance
            critical = expansion_active and healthy < 2 * safety_floor
            service_horizon = max(
                0.001,
                float(facility_deadline_seconds)
                + float(fast_probe_entitlement_seconds)
                + float(delivery_slack_seconds),
            )
            delivery_debt = 0.0
            service_pressure_floor = 0.0
            has_actionable_work = bool(actionable_work and not supply_exhausted)
            if expansion_active and has_actionable_work:
                last_growth = (
                    now
                    if last_net_new_commit_monotonic is None
                    else min(now, float(last_net_new_commit_monotonic))
                )
                delivery_debt = max(
                    0.0,
                    min(1.0, (now - last_growth) / service_horizon),
                )
                service_pressure_floor = self._last_target_error + (
                    1.0 - self._last_target_error
                ) * delivery_debt
            pressure = max(stock_pressure, service_pressure_floor)
            if (
                expansion_active
                and has_actionable_work
                and not hard_resource_guardrail
            ):
                # A durable commit reduces inventory error, but it must not
                # collapse an already-provisioned source/TCP/validation path
                # while the user target remains unmet.
                pressure = max(
                    pressure,
                    self._last_actionable_envelope_pressure,
                )
                self._last_actionable_envelope_pressure = pressure
            else:
                self._last_actionable_envelope_pressure = 0.0
            tcp_timeout_base = max(
                0.1,
                float(tcp_transport_base_timeout_seconds),
            )
            tcp_timeout_floor = max(
                0.1,
                min(
                    tcp_timeout_base,
                    float(tcp_transport_timeout_floor_seconds),
                ),
            )
            tcp_transport_timeout_seconds = (
                tcp_timeout_base
                - (tcp_timeout_base - tcp_timeout_floor) * pressure
            )

            worker_floor = max(1, int(helper_min))
            worker_cap = max(worker_floor, int(helper_max))
            raw_helpers = _interpolated_int(
                minimum_value=worker_floor,
                maximum_value=worker_cap,
                pressure=pressure,
                default_minimum=worker_floor,
                default_maximum=worker_cap,
            )
            if critical and healthy <= safety_floor:
                raw_helpers = worker_cap
            previous_helpers = self._last_envelope_helpers or max(
                worker_floor,
                min(worker_cap, int(active_helpers or worker_floor)),
            )
            helper_hold_reason = ""
            if raw_helpers > previous_helpers:
                desired_helpers = raw_helpers
                self._envelope_scale_down_streak = 0
            elif raw_helpers < previous_helpers:
                if (
                    expansion_active
                    and has_actionable_work
                    and not hard_resource_guardrail
                ):
                    desired_helpers = previous_helpers
                    helper_hold_reason = "below_target_actionable_work"
                    self._envelope_scale_down_streak = 0
                else:
                    self._envelope_scale_down_streak += 1
                    desired_helpers = previous_helpers
                    if self._envelope_scale_down_streak >= 3:
                        desired_helpers = max(raw_helpers, previous_helpers - 2)
                        self._envelope_scale_down_streak = 0
                        helper_hold_reason = (
                            "hard_resource_guardrail"
                            if hard_resource_guardrail
                            else "supply_exhausted"
                            if supply_exhausted
                            else "maintenance_scale_down"
                        )
            else:
                desired_helpers = raw_helpers
                self._envelope_scale_down_streak = 0
                if (
                    expansion_active
                    and has_actionable_work
                    and not hard_resource_guardrail
                    and desired_helpers >= previous_helpers
                ):
                    helper_hold_reason = "below_target_actionable_work"
            desired_helpers = max(worker_floor, min(worker_cap, desired_helpers))
            target_unmet_capacity_hold = bool(
                expansion_active
                and has_actionable_work
                and not hard_resource_guardrail
                and previous_helpers >= worker_cap
            )
            resource_hold_reason = ""
            if target_unmet_capacity_hold:
                desired_helpers = worker_cap
                helper_hold_reason = "below_target_actionable_work"
                resource_hold_reason = "target_unmet_actionable"
            self._last_envelope_helpers = desired_helpers

            # Resident ownership contains two independent populations: up to
            # 8N retained DEEP continuations and a 2N FAST ingress reserve.
            # Collapsing both into the old 8N cap lets a full retention window
            # close FAST before the actor can apply its overflow policy.
            resident_floor = max(2 * desired_helpers, desired_helpers)
            # 10 resident owners per identity; the 256 floor is the audited
            # single-slot ceiling and only grows when the owner allows more
            # identities (batch backend).
            resident_ceiling = max(256, 10 * int(helper_max))
            resident_cap = min(resident_ceiling, max(resident_floor, 10 * desired_helpers))
            resident_limit = int(
                round(resident_floor + (resident_cap - resident_floor) * pressure)
            )
            ready_floor = max(1, desired_helpers)
            ready_cap = max(ready_floor, 2 * desired_helpers)
            ready_limit = int(
                round(ready_floor + (ready_cap - ready_floor) * pressure)
            )
            resident_limit = max(ready_limit, min(resident_ceiling, resident_limit))

            tcp_floor = max(1, int(tcp_inflight_min))
            tcp_cap = max(tcp_floor, int(tcp_inflight_max))
            tcp_inflight = _interpolated_int(
                minimum_value=tcp_floor,
                maximum_value=tcp_cap,
                pressure=pressure,
                default_minimum=tcp_floor,
                default_maximum=tcp_cap,
            )
            if target_unmet_capacity_hold:
                tcp_inflight = tcp_cap
            snapshot_fast_slots = (
                max(0, int(work_snapshot.actor_fast_slots))
                if work_snapshot is not None
                else 0
            )
            free_fast_slots = max(
                0,
                snapshot_fast_slots
                - (
                    max(0, int(work_snapshot.fast_ready))
                    + max(0, int(work_snapshot.fast_active))
                    if work_snapshot is not None
                    else 0
                ),
            )
            responsive_probability_lcb = (
                max(
                    0.0,
                    min(
                        1.0,
                        float(
                            work_snapshot.candidate_tcp_responsive_probability_lcb
                        ),
                    ),
                )
                if work_snapshot is not None
                else 0.25
            )
            handoff_window_limit = tier3_candidate_handoff_window(
                tcp_inflight_limit=tcp_inflight,
                free_fast_slots=free_fast_slots,
                pressure=pressure,
                responsive_probability_lcb=responsive_probability_lcb,
            )
            responsive_high = max(
                desired_helpers,
                int(round(desired_helpers * (1.0 + 3.0 * pressure))),
            )
            responsive_low = max(desired_helpers, responsive_high // 2)

            configured_scan_budget = max(
                tcp_inflight,
                int(scan_session_budget or tcp_inflight),
            )
            scan_token_capacity = max(
                tcp_inflight,
                handoff_window_limit,
                min(configured_scan_budget, 2 * tcp_inflight),
            )
            scan_token_refill = max(
                1,
                int(math.ceil(scan_token_capacity * (0.25 + 0.75 * pressure))),
            )

            source_floor = max(1, int(source_budget_min))
            source_cap = max(source_floor, int(source_budget_max))
            source_tokens = _interpolated_int(
                minimum_value=source_floor,
                maximum_value=source_cap,
                pressure=pressure,
                default_minimum=source_floor,
                default_maximum=source_cap,
            )
            source_unit_budget = max(
                6,
                min(12, int(round(6 + 6 * pressure))),
            )
            # Selection and launch width are one PI decision. A fixed launch
            # width of six strands alternate layouts behind the first slow
            # transport tranche after the portfolio has widened to twelve.
            source_unit_concurrency = source_unit_budget
            source_unit_transport_limit = min(48, 4 * source_unit_budget)
            interval_floor = max(1, int(collect_interval_min))
            interval_cap = max(interval_floor, int(collect_interval_max))
            refill_interval = int(
                round(interval_cap - (interval_cap - interval_floor) * pressure)
            )

            if desired_helpers <= 1:
                fast_slots, deep_slots = 1, 0
            else:
                fast_share = 0.50 + 0.25 * pressure
                fast_slots = max(
                    1,
                    min(
                        desired_helpers - 1,
                        int(round(desired_helpers * fast_share)),
                    ),
                )
                deep_slots = max(1, desired_helpers - fast_slots)

            actual_fast_slots = fast_slots
            actual_deep_slots = deep_slots
            if (
                work_snapshot is not None
                and int(work_snapshot.actor_fast_slots)
                + int(work_snapshot.actor_deep_slots)
                > 0
            ):
                actual_fast_slots = max(
                    0,
                    int(work_snapshot.actor_fast_slots),
                )
                actual_deep_slots = max(
                    0,
                    int(work_snapshot.actor_deep_slots),
                )

            fresh_candidate_backlog = (
                max(0, int(work_snapshot.candidate_fresh_backlog))
                if work_snapshot is not None
                else 0
            )
            normal_credit_blocked_arm_count = (
                max(0, int(work_snapshot.candidate_credit_blocked_arm_count))
                if work_snapshot is not None
                else 0
            )
            credit_escape_budget = 0
            expected_fast_supply = False
            fast_supply_state = "exhausted"
            normal_reservable_fast_count = 0
            reward_reservable_fast_count = 0
            escape_eligible_bucket_count = 0
            active_escape_lease_count = 0
            candidate_credit_deadlock = False
            actionable_deep = 0
            if work_snapshot is not None:
                credit_escape_budget = tier3_candidate_credit_escape_budget(
                    healthy_count=healthy,
                    target_alive=target,
                    fast_slots=max(1, actual_fast_slots),
                    active_fast=work_snapshot.fast_active,
                    ready_fast=work_snapshot.fast_ready,
                    fresh_candidate_backlog=fresh_candidate_backlog,
                    ready_arm_count=work_snapshot.candidate_ready_arm_count,
                    normal_dispatchable_arm_count=(
                        work_snapshot.candidate_normal_dispatchable_arm_count
                    ),
                    credit_blocked_arm_count=(
                        work_snapshot.candidate_credit_blocked_arm_count
                    ),
                    overdue_credit_bucket_count=(
                        work_snapshot.candidate_escape_overdue_bucket_count
                    ),
                )
                expected_fast_supply = work_snapshot.expected_fast_supply
                fast_supply_state = str(work_snapshot.fast_supply_state)
                normal_reservable_fast_count = max(
                    0,
                    int(work_snapshot.candidate_normal_reservable_count),
                )
                reward_reservable_fast_count = max(
                    0,
                    int(work_snapshot.candidate_reward_reservable_count),
                )
                escape_eligible_bucket_count = max(
                    0,
                    int(work_snapshot.candidate_escape_eligible_bucket_count),
                )
                active_escape_lease_count = max(
                    0,
                    int(work_snapshot.candidate_active_escape_lease_count),
                )
                candidate_credit_deadlock = bool(
                    work_snapshot.candidate_credit_deadlock
                )
                actionable_deep = work_snapshot.actionable_deep

            tcp_responsive_reservation_ratio = (
                tier3_tcp_responsive_reservation_ratio(
                    tcp_scanned=(
                        work_snapshot.tcp_scanned_total
                        if work_snapshot is not None
                        else 0
                    ),
                    tcp_responsive=(
                        work_snapshot.tcp_responsive_total
                        if work_snapshot is not None
                        else 0
                    ),
                    wave_capacity=4,
                )
            )

            self._envelope_revision += 1
            envelope_decision = Tier3ExpansionEnvelopeDecision(
                revision=self._envelope_revision,
                healthy_count=healthy,
                min_alive_nodes=safety_floor,
                target_alive=target,
                target_error=round(self._last_target_error, 4),
                critical_error=round(self._last_critical_error, 4),
                effective_error=round(self._last_effective_error, 4),
                integral=round(self.integral, 4),
                stock_pressure=round(stock_pressure, 4),
                delivery_debt=round(delivery_debt, 4),
                service_pressure_floor=round(service_pressure_floor, 4),
                pressure=round(pressure, 4),
                actionable_work=has_actionable_work,
                helper_hold_reason=helper_hold_reason,
                state=(
                    "maintenance"
                    if maintenance
                    else "critical"
                    if critical
                    else "expansion"
                ),
                critical=critical,
                expansion_active=expansion_active,
                maintenance=maintenance,
                source_refill_tokens=source_tokens,
                source_refill_interval_seconds=max(1, refill_interval),
                tcp_inflight_limit=tcp_inflight,
                tcp_transport_timeout_seconds=round(
                    tcp_transport_timeout_seconds,
                    2,
                ),
                responsive_backlog_low_water=responsive_low,
                responsive_backlog_high_water=responsive_high,
                fresh_supply_low_water=responsive_low,
                fresh_supply_high_water=responsive_high,
                scan_token_capacity=scan_token_capacity,
                scan_token_refill=scan_token_refill,
                scan_token_interval_ms=250,
                candidate_scan_lifetime_cap_enabled=False,
                resident_transaction_limit=resident_limit,
                ready_work_limit=ready_limit,
                desired_helpers=desired_helpers,
                fast_slots=actual_fast_slots,
                deep_slots=actual_deep_slots,
                saturation_state=(
                    "candidate_credit_recovering"
                    if credit_escape_budget > 0
                    else "candidate_feedback_saturated"
                    if work_snapshot is not None
                    and work_snapshot.scheduler_saturation_state
                    == "arm_credit_saturated"
                    else
                    "resource_saturated"
                    if expansion_active
                    and has_actionable_work
                    and desired_helpers >= worker_cap
                    else "supply_idle"
                    if not has_actionable_work
                    else "scaling"
                ),
                exploit_scan_share=(
                    max(0.0, min(0.25, work_snapshot.exploit_scan_share))
                    if work_snapshot is not None
                    else 0.25
                ),
                fast_soft_entitlement_seconds=max(
                    2.25,
                    min(
                        3.0,
                        (
                            work_snapshot.fast_soft_entitlement_seconds
                            if work_snapshot is not None
                            else float(fast_probe_entitlement_seconds)
                        ),
                    ),
                ),
                fast_rolling_completions=(
                    int(work_snapshot.fast_rolling_completions)
                    if work_snapshot is not None
                    else 0
                ),
                fast_rolling_net_new=(
                    int(work_snapshot.fast_rolling_net_new)
                    if work_snapshot is not None
                    else 0
                ),
                fast_rolling_occupied_ms=(
                    int(work_snapshot.fast_rolling_occupied_ms)
                    if work_snapshot is not None
                    else 0
                ),
                fast_rolling_yield_lcb=(
                    float(work_snapshot.fast_rolling_yield_lcb)
                    if work_snapshot is not None
                    else 0.0
                ),
                deep_rolling_completions=(
                    int(work_snapshot.deep_rolling_completions)
                    if work_snapshot is not None
                    else 0
                ),
                deep_rolling_net_new=(
                    int(work_snapshot.deep_rolling_net_new)
                    if work_snapshot is not None
                    else 0
                ),
                deep_rolling_occupied_ms=(
                    int(work_snapshot.deep_rolling_occupied_ms)
                    if work_snapshot is not None
                    else 0
                ),
                deep_rolling_yield_lcb=(
                    float(work_snapshot.deep_rolling_yield_lcb)
                    if work_snapshot is not None
                    else 0.0
                ),
                runtime_qualified_count=max(
                    0,
                    int(runtime_qualified_count or 0),
                ),
                runtime_qualification_error=round(
                    runtime_qualification_error,
                    4,
                ),
                fresh_candidate_backlog=fresh_candidate_backlog,
                normal_credit_blocked_arm_count=(
                    normal_credit_blocked_arm_count
                ),
                credit_escape_budget=credit_escape_budget,
                expected_fast_supply=expected_fast_supply,
                fast_supply_state=fast_supply_state,
                normal_reservable_fast_count=normal_reservable_fast_count,
                reward_reservable_fast_count=reward_reservable_fast_count,
                escape_eligible_bucket_count=escape_eligible_bucket_count,
                active_escape_lease_count=active_escape_lease_count,
                candidate_credit_deadlock=candidate_credit_deadlock,
                actionable_deep=actionable_deep,
                tcp_responsive_reservation_ratio=round(
                    tcp_responsive_reservation_ratio,
                    4,
                ),
                handoff_window_limit=handoff_window_limit,
                tcp_responsive_probability_lcb=round(
                    responsive_probability_lcb,
                    6,
                ),
                expected_responsive_arrivals=(
                    max(
                        0,
                        int(work_snapshot.candidate_expected_responsive_arrivals),
                    )
                    if work_snapshot is not None
                    else 0
                ),
                resource_hold_reason=resource_hold_reason,
                source_unit_budget=source_unit_budget,
                source_unit_concurrency=source_unit_concurrency,
                source_unit_transport_limit=source_unit_transport_limit,
            )
            self._last_envelope_key = envelope_key
            self._last_envelope_result_at = now
            self._last_envelope_decision = envelope_decision
            return envelope_decision

    def to_dict(self) -> dict[str, Any]:
        decision = self.last_decision
        return {
            "intensity": decision.intensity,
            "source_budget": decision.source_budget,
            "collect_interval": decision.collect_interval,
            "error": decision.error,
            "integral": decision.integral,
            "target_error": round(self._last_target_error, 4),
            "critical_error": round(self._last_critical_error, 4),
            "effective_error": round(self._last_effective_error, 4),
            "expansion_state": self._last_critical_state,
        }
