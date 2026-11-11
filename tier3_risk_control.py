from __future__ import annotations

import hashlib
import json
import math
import os
import random
import threading
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from tier3_validation_protocol import TIER3_VALIDATION_CONTRACT_HASH


TIER3_RISK_CONTROL_SCHEMA_VERSION = 7
TIER3_RISK_AGE_BUCKETS = (3.0, 8.0, 12.0, math.inf)


class Tier3OutcomeKind(str, Enum):
    PASS = "pass"
    REAL_DEAD = "real_dead"
    TRANSIENT = "transient"
    RIGHT_CENSORED = "right_censored"


class Tier3ReachabilityClass(str, Enum):
    FEASIBLE = "feasible"
    RESOURCE_LIMITED = "resource_limited"
    SUPPLY_LIMITED = "supply_limited"
    FACILITY_LIMITED = "facility_limited"
    UNCERTAIN = "uncertain"


class Tier3SourceRiskState(str, Enum):
    FRESH_HIGH_YIELD = "fresh_high_yield"
    FRESH_UNKNOWN = "fresh_unknown"
    STALE = "stale"
    FAILED_RETRYABLE = "failed_retryable"
    COOLDOWN = "cooldown"
    FACILITY_BLOCKED = "facility_blocked"


@dataclass(frozen=True, slots=True)
class Tier3SourceControlSnapshot:
    root_id: str
    state: str = Tier3SourceRiskState.FRESH_UNKNOWN.value
    pending: bool = False
    retry_due: bool = False
    age_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class Tier3ControlSnapshot:
    now_monotonic: float
    alive_count: int
    min_alive_nodes: int
    target_alive: int
    pi_desired_workers: int
    active_workers: int
    primary_depth: int = 0
    canonical_depth: int = 0
    retry_depth: int = 0
    primary_oldest_wait_ms: float = 0.0
    canonical_oldest_wait_ms: float = 0.0
    retry_oldest_wait_ms: float = 0.0
    supported_backlog_count: int = 0
    source_remaining_upper: int | None = None
    source_states: tuple[Tier3SourceControlSnapshot, ...] = ()
    recent_admissions_per_second: float = 0.0
    recent_candidate_pass_rate: float = 0.0
    facility_failure_rate: float = 0.0
    warm_reload_p95_ms: float = 0.0
    process_reset_rate: float = 0.0
    facility_validation_count: int = 0
    primary_pass_throughput: float = 0.0
    canonical_pass_throughput: float = 0.0
    retry_pass_throughput: float = 0.0
    probe_endpoint_limited: bool = False
    session_elapsed_seconds: float = 0.0
    validation_completion_count: int = 0
    validation_completion_throughput: float = 0.0
    deadline_violation_count: int = 0
    hard_facility_failure_count: int = 0
    primary_completed_count: int = 0
    primary_definitive_count: int = 0
    primary_pass_count: int = 0
    primary_occupied_ms: float = 0.0
    canonical_completed_count: int = 0
    canonical_definitive_count: int = 0
    canonical_pass_count: int = 0
    canonical_occupied_ms: float = 0.0
    retry_completed_count: int = 0
    retry_definitive_count: int = 0
    retry_pass_count: int = 0
    retry_occupied_ms: float = 0.0
    session_committed_count: int = 0
    capacity_epoch_id: int = 0
    capacity_epoch_dispatched_count: int = 0
    capacity_epoch_completed_count: int = 0
    capacity_epoch_occupied_ms: float = 0.0
    capacity_epoch_committed_count: int = 0
    endpoint_throughput_lcb: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Tier3LaneAllocation:
    primary_tokens: int
    canonical_tokens: int
    retry_tokens: int

    @property
    def total(self) -> int:
        return self.primary_tokens + self.canonical_tokens + self.retry_tokens


@dataclass(frozen=True, slots=True)
class Tier3RiskDecision:
    rollout_stage: str
    calibration_lambda: float
    calibration_ece: float
    drift_detected: bool
    desired_workers: int
    safe_worker_cap: int
    bounded_worker_delta: int
    lane_allocation: Tier3LaneAllocation
    candidate_correction_limit: float
    source_order: tuple[str, ...]
    source_tokens: int
    reachability_class: Tier3ReachabilityClass
    reachability_confidence: float
    cvar95_time_to_gate_ms: int | None
    expected_target_gap_20s: float | None
    canary_state: str
    policy_replay_sample_count: int
    policy_replay_qualified: bool
    primary_queue_debt: float
    canonical_queue_debt: float
    retry_queue_debt: float
    primary_oldest_wait_ms: float
    canonical_oldest_wait_ms: float
    retry_oldest_wait_ms: float
    primary_hazard_lcb: float
    canonical_hazard_lcb: float
    retry_hazard_lcb: float
    primary_pass_throughput: float
    canonical_pass_throughput: float
    retry_pass_throughput: float
    canary_reason: str = ""

    def to_report(self) -> dict[str, Any]:
        return {
            "risk_control_stage": self.rollout_stage,
            "calibration_lambda": round(self.calibration_lambda, 4),
            "calibration_ece": round(self.calibration_ece, 4),
            "drift_detected": self.drift_detected,
            "risk_desired_worker_count": self.desired_workers,
            "risk_safe_worker_cap": self.safe_worker_cap,
            "risk_bounded_worker_delta": self.bounded_worker_delta,
            "risk_primary_tokens": self.lane_allocation.primary_tokens,
            "risk_canonical_tokens": self.lane_allocation.canonical_tokens,
            "risk_retry_tokens": self.lane_allocation.retry_tokens,
            "risk_candidate_correction_limit": round(
                self.candidate_correction_limit,
                4,
            ),
            "risk_source_order": list(self.source_order),
            "risk_source_tokens": self.source_tokens,
            "reachability_class": self.reachability_class.value,
            "reachability_confidence": round(self.reachability_confidence, 4),
            "cvar95_time_to_gate_ms": self.cvar95_time_to_gate_ms,
            "expected_target_gap_20s": self.expected_target_gap_20s,
            "risk_worker_canary_state": self.canary_state,
            "risk_worker_canary_reason": self.canary_reason,
            "risk_policy_replay_sample_count": self.policy_replay_sample_count,
            "risk_policy_replay_qualified": self.policy_replay_qualified,
            "risk_primary_queue_debt": round(self.primary_queue_debt, 4),
            "risk_canonical_queue_debt": round(self.canonical_queue_debt, 4),
            "risk_retry_queue_debt": round(self.retry_queue_debt, 4),
            "risk_primary_oldest_wait_ms": round(self.primary_oldest_wait_ms, 3),
            "risk_canonical_oldest_wait_ms": round(self.canonical_oldest_wait_ms, 3),
            "risk_retry_oldest_wait_ms": round(self.retry_oldest_wait_ms, 3),
            "risk_primary_hazard_lcb": round(self.primary_hazard_lcb, 6),
            "risk_canonical_hazard_lcb": round(self.canonical_hazard_lcb, 6),
            "risk_retry_hazard_lcb": round(self.retry_hazard_lcb, 6),
            "risk_primary_pass_throughput": round(self.primary_pass_throughput, 6),
            "risk_canonical_pass_throughput": round(self.canonical_pass_throughput, 6),
            "risk_retry_pass_throughput": round(self.retry_pass_throughput, 6),
        }


@dataclass(frozen=True, slots=True)
class Tier3RiskControlConfig:
    enabled: bool = True
    rollout_mode: str = "auto_bounded"
    fast_allocation_interval_ms: int = 250
    envelope_interval_ms: int = 2000
    model_refresh_interval_seconds: int = 15
    max_learning_correction: float = 0.20
    initial_safe_worker_cap: int = 16
    hard_worker_max: int = 24
    canary_step: int = 2
    canary_cooldown_seconds: int = 60
    baseline_capacity_canary_enabled: bool = True
    min_candidate_observations: int = 500
    min_candidate_passes: int = 20
    mature_candidate_observations: int = 2000
    mature_candidate_passes: int = 50
    min_cvar_sessions: int = 200

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "Tier3RiskControlConfig":
        payload = value if isinstance(value, Mapping) else {}

        def as_int(key: str, default: int, minimum: int = 0) -> int:
            try:
                return max(minimum, int(payload.get(key, default)))
            except (TypeError, ValueError):
                return default

        def as_float(key: str, default: float, minimum: float, maximum: float) -> float:
            try:
                return max(minimum, min(maximum, float(payload.get(key, default))))
            except (TypeError, ValueError):
                return default

        enabled_value = payload.get("enabled", True)
        enabled = enabled_value if isinstance(enabled_value, bool) else str(enabled_value).lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        baseline_canary_value = payload.get("baseline_capacity_canary_enabled", True)
        baseline_capacity_canary_enabled = (
            baseline_canary_value
            if isinstance(baseline_canary_value, bool)
            else str(baseline_canary_value).lower() not in {"0", "false", "no", "off"}
        )
        hard_cap = as_int("hard_worker_max", 24, 1)
        initial_cap = min(
            hard_cap,
            as_int("initial_safe_worker_cap", 16, 1),
        )
        return cls(
            enabled=enabled,
            rollout_mode=str(payload.get("rollout_mode") or "auto_bounded").strip().lower(),
            fast_allocation_interval_ms=as_int("fast_allocation_interval_ms", 250, 50),
            envelope_interval_ms=as_int("envelope_interval_ms", 2000, 250),
            model_refresh_interval_seconds=as_int("model_refresh_interval_seconds", 15, 1),
            max_learning_correction=as_float("max_learning_correction", 0.20, 0.0, 0.20),
            initial_safe_worker_cap=initial_cap,
            hard_worker_max=hard_cap,
            canary_step=as_int("canary_step", 2, 1),
            canary_cooldown_seconds=as_int("canary_cooldown_seconds", 60, 1),
            baseline_capacity_canary_enabled=baseline_capacity_canary_enabled,
            min_candidate_observations=as_int("min_candidate_observations", 500, 1),
            min_candidate_passes=as_int("min_candidate_passes", 20, 1),
            mature_candidate_observations=as_int("mature_candidate_observations", 2000, 1),
            mature_candidate_passes=as_int("mature_candidate_passes", 50, 1),
            min_cvar_sessions=as_int("min_cvar_sessions", 200, 1),
        )


@dataclass(slots=True)
class _HazardCell:
    exposures: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    passes: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    real_dead: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    transient: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    censored: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    elapsed_sum_seconds: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])

    def observe(self, outcome: Tier3OutcomeKind, elapsed_seconds: float) -> None:
        bucket = _age_bucket(elapsed_seconds)
        self.exposures[bucket] += 1
        self.elapsed_sum_seconds[bucket] += max(0.001, float(elapsed_seconds))
        if outcome is Tier3OutcomeKind.PASS:
            self.passes[bucket] += 1
        elif outcome is Tier3OutcomeKind.REAL_DEAD:
            self.real_dead[bucket] += 1
        elif outcome is Tier3OutcomeKind.TRANSIENT:
            self.transient[bucket] += 1
        else:
            self.censored[bucket] += 1

    @property
    def sample_count(self) -> int:
        return sum(self.exposures)

    @property
    def pass_count(self) -> int:
        return sum(self.passes)

    def pass_probability(self) -> float:
        return self.pass_count / float(max(1, self.sample_count))

    def pass_lcb(self) -> float:
        return wilson_lower_bound(self.pass_count, self.sample_count)

    def pass_lcb_from_age(self, age_seconds: float) -> float:
        start = _age_bucket(age_seconds)
        exposures = sum(self.exposures[start:])
        passes = sum(self.passes[start:])
        return wilson_lower_bound(passes, exposures)

    def mean_elapsed_seconds(self) -> float:
        return sum(self.elapsed_sum_seconds) / float(max(1, self.sample_count))

    def to_dict(self) -> dict[str, Any]:
        return {
            "exposures": list(self.exposures),
            "passes": list(self.passes),
            "real_dead": list(self.real_dead),
            "transient": list(self.transient),
            "censored": list(self.censored),
            "elapsed_sum_seconds": [round(value, 6) for value in self.elapsed_sum_seconds],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "_HazardCell":
        cell = cls()
        for name in ("exposures", "passes", "real_dead", "transient", "censored"):
            raw = value.get(name)
            if isinstance(raw, list) and len(raw) == 4:
                setattr(cell, name, [max(0, int(item or 0)) for item in raw])
        elapsed = value.get("elapsed_sum_seconds")
        if isinstance(elapsed, list) and len(elapsed) == 4:
            cell.elapsed_sum_seconds = [max(0.0, float(item or 0.0)) for item in elapsed]
        return cell


@dataclass(slots=True)
class _SourcePosterior:
    alpha: float = 1.0
    beta: float = 1.0
    actions: int = 0
    admissions: int = 0
    token_seconds: float = 0.0
    last_selected_round: int = 0
    state: str = Tier3SourceRiskState.FRESH_UNKNOWN.value
    whittle_index: float = 0.0

    def discount(self, factor: float = 0.98) -> None:
        self.alpha = 1.0 + (self.alpha - 1.0) * factor
        self.beta = 1.0 + (self.beta - 1.0) * factor

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "_SourcePosterior":
        return cls(
            alpha=max(1.0, float(value.get("alpha", 1.0) or 1.0)),
            beta=max(1.0, float(value.get("beta", 1.0) or 1.0)),
            actions=max(0, int(value.get("actions", 0) or 0)),
            admissions=max(0, int(value.get("admissions", 0) or 0)),
            token_seconds=max(0.0, float(value.get("token_seconds", 0.0) or 0.0)),
            last_selected_round=max(0, int(value.get("last_selected_round", 0) or 0)),
            state=str(value.get("state") or Tier3SourceRiskState.FRESH_UNKNOWN.value),
            whittle_index=float(value.get("whittle_index", 0.0) or 0.0),
        )


def _age_bucket(elapsed_seconds: float) -> int:
    elapsed = max(0.0, float(elapsed_seconds))
    for index, upper in enumerate(TIER3_RISK_AGE_BUCKETS):
        if elapsed <= upper:
            return index
    return len(TIER3_RISK_AGE_BUCKETS) - 1


def wilson_lower_bound(successes: int, total: int, *, z: float = 1.6448536269514722) -> float:
    total = max(0, int(total))
    successes = max(0, min(total, int(successes)))
    if total <= 0:
        return 0.0
    proportion = successes / float(total)
    denominator = 1.0 + (z * z) / total
    centre = proportion + (z * z) / (2.0 * total)
    margin = z * math.sqrt(
        (proportion * (1.0 - proportion) + (z * z) / (4.0 * total)) / total
    )
    return max(0.0, min(1.0, (centre - margin) / denominator))


def wilson_upper_bound(successes: int, total: int, *, z: float = 1.6448536269514722) -> float:
    total = max(0, int(total))
    successes = max(0, min(total, int(successes)))
    if total <= 0:
        return 1.0
    proportion = successes / float(total)
    denominator = 1.0 + (z * z) / total
    centre = proportion + (z * z) / (2.0 * total)
    margin = z * math.sqrt(
        (proportion * (1.0 - proportion) + (z * z) / (4.0 * total)) / total
    )
    return max(0.0, min(1.0, (centre + margin) / denominator))


def empirical_cvar95(values: Sequence[float], *, minimum_samples: int = 200) -> float | None:
    cleaned = sorted(float(value) for value in values if math.isfinite(float(value)))
    if len(cleaned) < max(1, int(minimum_samples)):
        return None
    tail_count = max(1, int(math.ceil(len(cleaned) * 0.05)))
    tail = cleaned[-tail_count:]
    return sum(tail) / float(len(tail))


def expected_gap(values: Sequence[int | float]) -> float | None:
    cleaned = [max(0.0, float(value)) for value in values]
    if not cleaned:
        return None
    return sum(cleaned) / float(len(cleaned))


def lane_allocation(
    worker_count: int,
    *,
    primary_depth: int,
    canonical_depth: int,
    retry_depth: int,
    primary_wait_ms: float = 0.0,
    canonical_wait_ms: float = 0.0,
    retry_wait_ms: float = 0.0,
    previous: Tier3LaneAllocation | None = None,
) -> Tier3LaneAllocation:
    workers = max(1, int(worker_count))
    depths = (
        max(0, int(primary_depth)),
        max(0, int(canonical_depth)),
        max(0, int(retry_depth)),
    )
    if not any(depths):
        return Tier3LaneAllocation(workers, 0, 0)

    primary_min = (workers + 1) // 2 if depths[0] > 0 else 0
    canonical_min = max(1, workers // 4) if depths[1] > 0 else 0
    retry_max = workers // 4 if depths[2] > 0 else 0
    if depths[0] > 0:
        retry_max = min(retry_max, max(0, workers - 1))

    weights = (
        depths[0] + max(0.0, float(primary_wait_ms)) / 250.0,
        depths[1] + max(0.0, float(canonical_wait_ms)) / 500.0,
        depths[2] + max(0.0, float(retry_wait_ms)) / 1000.0,
    )
    best: Tier3LaneAllocation | None = None
    best_score = -math.inf
    for primary in range(workers + 1):
        for canonical in range(workers - primary + 1):
            retry = workers - primary - canonical
            if primary < primary_min or canonical < canonical_min or retry > retry_max:
                continue
            if depths[0] <= 0 and primary > 0:
                continue
            if depths[1] <= 0 and canonical > 0:
                continue
            if depths[2] <= 0 and retry > 0:
                continue
            score = primary * weights[0] + canonical * weights[1] + retry * weights[2]
            if previous is not None:
                score -= 0.1 * (
                    abs(primary - previous.primary_tokens)
                    + abs(canonical - previous.canonical_tokens)
                    + abs(retry - previous.retry_tokens)
                )
            if score > best_score:
                best_score = score
                best = Tier3LaneAllocation(primary, canonical, retry)
    if best is not None:
        return best

    # Very small worker counts cannot satisfy all percentage constraints at once.
    if depths[0] > 0:
        return Tier3LaneAllocation(workers, 0, 0)
    if depths[1] > 0:
        return Tier3LaneAllocation(0, workers, 0)
    return Tier3LaneAllocation(0, 0, workers)


class Tier3RiskController:
    """Read-only learning controller layered on top of the deterministic PI path."""

    def __init__(
        self,
        *,
        config: Mapping[str, Any] | None = None,
        state_path: str | Path | None = None,
        random_seed: int | None = None,
    ) -> None:
        self.config = Tier3RiskControlConfig.from_mapping(config)
        self.state_path = Path(state_path) if state_path else None
        self._lock = threading.RLock()
        self._random = random.Random(random_seed)
        self._hazards: dict[str, _HazardCell] = {}
        self._published_hazards: dict[str, _HazardCell] = {}
        self._last_model_refresh_at = 0.0
        self._sources: dict[str, _SourcePosterior] = {}
        # Raw root labels are process-local diagnostics only. Persisted model
        # keys are irreversible digests so source URLs and credentials never
        # enter tier3-control-state.json.
        self._source_labels: dict[str, str] = {}
        self._calibration: deque[tuple[float, int, int]] = deque(maxlen=4096)
        self._sessions: deque[dict[str, Any]] = deque(maxlen=256)
        self._shadow_replay_sessions: deque[dict[str, float]] = deque(maxlen=256)
        self._current_replay_events: list[dict[str, Any]] = []
        self._facility_samples: dict[int, deque[dict[str, float]]] = defaultdict(
            lambda: deque(maxlen=256)
        )
        self._facility_history: deque[int] = deque(maxlen=4096)
        self._candidate_observations = 0
        self._candidate_passes = 0
        self._source_round = 0
        self._source_policy_replay_improvement: float | None = None
        self._policy_replay: dict[str, Any] = {
            "sample_count": 0,
            "baseline_cvar95_ms": None,
            "bounded_cvar95_ms": None,
            "baseline_gap_20s": None,
            "bounded_gap_20s": None,
            "qualified": False,
        }
        self._safe_worker_cap = self.config.initial_safe_worker_cap
        self._canary_state = "stable"
        self._canary_reason = "initial_safe_cap"
        self._canary_started_at = 0.0
        self._canary_validation_start = 0
        self._canary_pass_start = 0
        self._canary_commit_start = 0
        self._canary_capacity_epoch_id = 0
        self._canary_baseline_throughput = 0.0
        self._canary_baseline_failure = 0.0
        self._canary_baseline_reload_ms = 0.0
        self._canary_cooldown_until = 0.0
        self._canary_cooldown_until_epoch = 0.0
        self._saturated_samples = 0
        self._last_envelope_evaluation_at = 0.0
        self._last_fast_canary_evaluation_at = 0.0
        self._canary_baseline_completion_throughput = 0.0
        self._canary_baseline_busy_admission_lcb = 0.0
        self._canary_regression_streak = 0
        self._fast_canary_target_active = False
        self._last_deadline_violation_count = 0
        self._last_hard_facility_failure_count = 0
        self._drift_recovery_remaining = 0
        self._last_allocation: Tier3LaneAllocation | None = None
        self._state_migration_reason = ""
        self._load_state()

    @property
    def safe_worker_cap(self) -> int:
        with self._lock:
            return self._safe_worker_cap

    @staticmethod
    def _stable_digest(*parts: str) -> str:
        payload = "\x1f".join(str(part or "unknown") for part in parts)
        return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()[:24]

    def _source_storage_key(self, root: str) -> str:
        label = str(root or "unknown")
        key = f"source:{self._stable_digest(label)}"
        with self._lock:
            self._source_labels[key] = label
        return key

    def _hazard_keys(self, root: str, family: str, coarse: str) -> tuple[str, ...]:
        clean_root = str(root or "unknown")
        clean_family = str(family or "other")
        clean_coarse = str(coarse or "unknown")
        return (
            f"rfc:{self._stable_digest(clean_root, clean_family, clean_coarse)}",
            f"rf:{self._stable_digest(clean_root, clean_family)}",
            f"f:{clean_family}",
            "global",
        )

    def _selected_hazard(self, root: str, family: str, coarse: str) -> _HazardCell:
        return self._selected_hazard_from(
            self._hazards,
            root=root,
            family=family,
            coarse=coarse,
        )

    def _selected_hazard_from(
        self,
        hazards: Mapping[str, _HazardCell],
        *,
        root: str,
        family: str,
        coarse: str,
    ) -> _HazardCell:
        keys = self._hazard_keys(root, family, coarse)
        for key in keys[:-1]:
            cell = hazards.get(key)
            if cell is not None and cell.sample_count >= 20:
                return cell
        return hazards.get("global", _HazardCell())

    def _refresh_published_model(self, now_monotonic: float, *, force: bool = False) -> None:
        interval = max(1.0, float(self.config.model_refresh_interval_seconds))
        if (
            not force
            and self._published_hazards
            and now_monotonic - self._last_model_refresh_at < interval
        ):
            return
        self._published_hazards = {
            key: _HazardCell.from_dict(cell.to_dict())
            for key, cell in self._hazards.items()
        }
        self._last_model_refresh_at = float(now_monotonic)

    def candidate_pass_lcb(self, *, root: str, family: str, coarse: str) -> float:
        with self._lock:
            return self._selected_hazard(root, family, coarse).pass_lcb()

    def candidate_correction(self, *, root: str, family: str, coarse: str) -> float:
        with self._lock:
            stage, calibration_lambda, _ece, _drift = self._rollout_state()
            if stage == "shadow" or calibration_lambda <= 0.0:
                return 0.0
            local = self._selected_hazard_from(
                self._published_hazards,
                root=root,
                family=family,
                coarse=coarse,
            )
            global_cell = self._published_hazards.get("global", _HazardCell())
            local_efficiency = local.pass_lcb() / max(0.25, local.mean_elapsed_seconds())
            global_efficiency = global_cell.pass_lcb() / max(
                0.25,
                global_cell.mean_elapsed_seconds(),
            )
            denominator = max(0.01, local_efficiency + global_efficiency)
            normalized = max(-1.0, min(1.0, (local_efficiency - global_efficiency) / denominator))
            return normalized * self.config.max_learning_correction * calibration_lambda

    def observe_candidate(
        self,
        *,
        outcome: Tier3OutcomeKind | str,
        elapsed_seconds: float,
        root: str,
        family: str,
        coarse: str,
        facility_failure: bool = False,
        predicted_pass_probability: float | None = None,
    ) -> None:
        if facility_failure:
            return
        try:
            normalized = outcome if isinstance(outcome, Tier3OutcomeKind) else Tier3OutcomeKind(str(outcome))
        except ValueError:
            return
        with self._lock:
            selected_before_update = self._selected_hazard(root, family, coarse)
            prediction = (
                selected_before_update.pass_probability()
                if predicted_pass_probability is None
                else max(0.0, min(1.0, float(predicted_pass_probability)))
            )
            replay_score = selected_before_update.pass_lcb() / max(
                0.25,
                selected_before_update.mean_elapsed_seconds(),
            )
            self._current_replay_events.append(
                {
                    "group": self._stable_digest(root, family),
                    "score": max(0.0, float(replay_score)),
                    "elapsed": max(0.001, float(elapsed_seconds)),
                    "passed": normalized is Tier3OutcomeKind.PASS,
                }
            )
            for key in self._hazard_keys(root, family, coarse):
                self._hazards.setdefault(key, _HazardCell()).observe(normalized, elapsed_seconds)
            self._candidate_observations += 1
            if normalized is Tier3OutcomeKind.PASS:
                self._candidate_passes += 1
            if normalized is not Tier3OutcomeKind.RIGHT_CENSORED:
                self._calibration.append(
                    (
                        prediction,
                        1 if normalized is Tier3OutcomeKind.PASS else 0,
                        1 if normalized is Tier3OutcomeKind.TRANSIENT else 0,
                    )
                )
            if self._drift_recovery_remaining > 0:
                self._drift_recovery_remaining -= 1

    def observe_source_action(
        self,
        root_id: str,
        *,
        admissions: int,
        token_seconds: float,
        facility_healthy: bool,
        informative: bool = True,
        state: str | Tier3SourceRiskState | None = None,
    ) -> None:
        root = self._source_storage_key(str(root_id or "unknown"))
        with self._lock:
            posterior = self._sources.setdefault(root, _SourcePosterior())
            if state is not None:
                posterior.state = state.value if isinstance(state, Tier3SourceRiskState) else str(state)
            if not facility_healthy:
                posterior.state = Tier3SourceRiskState.FACILITY_BLOCKED.value
                return
            if not informative:
                return
            posterior.discount()
            delivered = max(0, int(admissions))
            elapsed = max(0.001, float(token_seconds))
            posterior.actions += 1
            posterior.admissions += delivered
            posterior.token_seconds += elapsed
            if delivered > 0:
                posterior.alpha += min(
                    4.0,
                    max(0.25, float(delivered) / elapsed),
                )
                posterior.state = Tier3SourceRiskState.FRESH_HIGH_YIELD.value
            else:
                posterior.beta += 1.0

    def rank_sources(
        self,
        sources: Iterable[Tier3SourceControlSnapshot],
        *,
        advance_round: bool = True,
        selected_count: int = 1,
    ) -> tuple[str, ...]:
        snapshots = list(sources)
        with self._lock:
            round_number = self._source_round + (1 if advance_round else 0)
            scored: list[tuple[bool, float, str, str]] = []
            for snapshot in snapshots:
                root_label = str(snapshot.root_id or "unknown")
                root = self._source_storage_key(root_label)
                posterior = self._sources.setdefault(root, _SourcePosterior(state=snapshot.state))
                posterior.state = str(snapshot.state or posterior.state)
                overdue = (
                    (snapshot.pending or snapshot.retry_due)
                    and round_number - posterior.last_selected_round >= 2
                )
                sample = (
                    self._random.betavariate(posterior.alpha, posterior.beta)
                    if advance_round
                    else posterior.alpha / max(1.0, posterior.alpha + posterior.beta)
                )
                age_bonus = min(0.25, max(0.0, snapshot.age_seconds) / 3600.0 * 0.05)
                retry_bonus = 0.15 if snapshot.retry_due else 0.0
                cooldown_penalty = 0.5 if snapshot.state == Tier3SourceRiskState.COOLDOWN.value else 0.0
                score = sample + age_bonus + retry_bonus - cooldown_penalty
                posterior.whittle_index = self._whittle_shadow_index(snapshot, posterior)
                scored.append((overdue, score, root_label, root))
            scored.sort(key=lambda item: (not item[0], -item[1], item[2]))
            ordered = tuple(item[2] for item in scored)
            if advance_round:
                self._source_round = round_number
                for _overdue, _score, _label, storage_key in scored[
                    : max(0, int(selected_count))
                ]:
                    self._sources[storage_key].last_selected_round = self._source_round
            return ordered

    def select_sources(
        self,
        sources: Iterable[Tier3SourceControlSnapshot],
        *,
        token_count: int,
    ) -> tuple[str, ...]:
        ordered = self.rank_sources(
            sources,
            advance_round=True,
            selected_count=max(0, int(token_count)),
        )
        return ordered[: max(0, int(token_count))]

    @staticmethod
    def _whittle_shadow_index(
        snapshot: Tier3SourceControlSnapshot,
        posterior: _SourcePosterior,
    ) -> float:
        mean = posterior.alpha / max(1.0, posterior.alpha + posterior.beta)
        freshness = min(1.0, max(0.0, snapshot.age_seconds) / 3600.0)
        retry = 0.25 if snapshot.retry_due else 0.0
        blocked = 1.0 if snapshot.state == Tier3SourceRiskState.FACILITY_BLOCKED.value else 0.0
        return mean + 0.2 * freshness + retry - blocked

    @staticmethod
    def _source_indexability_ratio(posterior: _SourcePosterior) -> float:
        states = tuple(Tier3SourceRiskState)
        subsidies = tuple(value / 10.0 for value in range(-10, 21))
        monotone_states = 0
        posterior_mean = posterior.alpha / max(1.0, posterior.alpha + posterior.beta)
        state_adjustment = {
            Tier3SourceRiskState.FRESH_HIGH_YIELD: 0.30,
            Tier3SourceRiskState.FRESH_UNKNOWN: 0.05,
            Tier3SourceRiskState.STALE: 0.15,
            Tier3SourceRiskState.FAILED_RETRYABLE: 0.20,
            Tier3SourceRiskState.COOLDOWN: -0.25,
            Tier3SourceRiskState.FACILITY_BLOCKED: -1.0,
        }
        for state in states:
            active_value = posterior_mean + state_adjustment[state]
            passive_seen = False
            monotone = True
            for subsidy in subsidies:
                passive = subsidy >= active_value
                if passive_seen and not passive:
                    monotone = False
                    break
                passive_seen = passive_seen or passive
            monotone_states += 1 if monotone else 0
        return monotone_states / float(len(states))

    def record_source_policy_replay(self, *, relative_improvement: float) -> None:
        with self._lock:
            self._source_policy_replay_improvement = float(relative_improvement)

    @staticmethod
    def _metric_not_worse(
        baseline: float | None,
        bounded: float | None,
        *,
        tolerance: float = 0.05,
    ) -> bool:
        if baseline is None or bounded is None:
            return False
        if baseline <= 0.0:
            return bounded <= baseline
        return bounded <= baseline * (1.0 + tolerance)

    @staticmethod
    def _metric_relative_improvement(
        baseline: float | None,
        bounded: float | None,
    ) -> float:
        if baseline is None or bounded is None or baseline <= 0.0:
            return 0.0
        return (baseline - bounded) / baseline

    def record_policy_replay(
        self,
        *,
        sample_count: int,
        baseline_cvar95_ms: float,
        bounded_cvar95_ms: float,
        baseline_gap_20s: float,
        bounded_gap_20s: float,
    ) -> bool:
        samples = max(0, int(sample_count))
        baseline_cvar = max(0.0, float(baseline_cvar95_ms))
        bounded_cvar = max(0.0, float(bounded_cvar95_ms))
        baseline_gap = max(0.0, float(baseline_gap_20s))
        bounded_gap = max(0.0, float(bounded_gap_20s))
        qualified = bool(
            samples >= 200
            and self._metric_not_worse(baseline_cvar, bounded_cvar)
            and self._metric_not_worse(baseline_gap, bounded_gap)
            and max(
                self._metric_relative_improvement(baseline_cvar, bounded_cvar),
                self._metric_relative_improvement(baseline_gap, bounded_gap),
            )
            >= 0.10
        )
        with self._lock:
            self._policy_replay = {
                "sample_count": samples,
                "baseline_cvar95_ms": baseline_cvar,
                "bounded_cvar95_ms": bounded_cvar,
                "baseline_gap_20s": baseline_gap,
                "bounded_gap_20s": bounded_gap,
                "qualified": qualified,
            }
        return qualified

    def source_diagnostics(self) -> dict[str, Any]:
        with self._lock:
            actions = sum(item.actions for item in self._sources.values())
            ratios: list[float] = []
            for posterior in self._sources.values():
                if posterior.actions <= 0:
                    continue
                ratios.append(self._source_indexability_ratio(posterior))
            ratio = sum(ratios) / float(len(ratios)) if ratios else 0.0
            replay_improvement = self._source_policy_replay_improvement
            whittle_ready = (
                actions >= 100
                and bool(ratios)
                and ratio >= 0.95
                and replay_improvement is not None
                and replay_improvement >= 0.10
            )
            return {
                "policy": "whittle_bounded" if whittle_ready else "discounted_thompson_drr",
                "action_count": actions,
                "indexability_ratio": round(ratio, 4),
                "replay_relative_improvement": (
                    round(replay_improvement, 4)
                    if replay_improvement is not None
                    else None
                ),
                "whittle_ready": whittle_ready,
                "roots": {
                    self._source_labels.get(root, root): {
                        "state": posterior.state,
                        "actions": posterior.actions,
                        "posterior_samples": posterior.actions,
                        "thompson_mean": round(
                            posterior.alpha / max(1.0, posterior.alpha + posterior.beta),
                            4,
                        ),
                        "whittle_shadow_index": round(posterior.whittle_index, 4),
                        "fairness_debt": max(0, self._source_round - posterior.last_selected_round),
                    }
                    for root, posterior in self._sources.items()
                },
            }

    def observe_facility(
        self,
        *,
        worker_count: int,
        reload_elapsed_ms: float,
        failed: bool,
        process_reset: bool = False,
        admissions_per_second: float = 0.0,
    ) -> None:
        with self._lock:
            self._facility_history.append(1 if failed else 0)
            self._facility_samples[max(1, int(worker_count))].append(
                {
                    "reload_ms": max(0.0, float(reload_elapsed_ms)),
                    "failed": 1.0 if failed else 0.0,
                    "reset": 1.0 if process_reset else 0.0,
                    "throughput": max(0.0, float(admissions_per_second)),
                }
            )

    def observe_session(
        self,
        *,
        time_to_gate_ms: int | None,
        alive_at_20s: int,
        target_alive: int,
        min_alive_nodes: int = 3,
        initial_alive: int = 0,
        observation_horizon_ms: int = 60_000,
    ) -> None:
        gate_loss = (
            max(0, int(time_to_gate_ms))
            if time_to_gate_ms is not None
            else max(1, int(observation_horizon_ms)) * 2
        )
        with self._lock:
            self._sessions.append(
                {
                    "gate_loss_ms": gate_loss,
                    "target_gap_20s": max(0, int(target_alive) - max(0, int(alive_at_20s))),
                    "censored": time_to_gate_ms is None,
                }
            )
            self._finalize_shadow_replay(
                gate_loss_ms=gate_loss,
                alive_at_20s=max(0, int(alive_at_20s)),
                target_alive=max(1, int(target_alive)),
                min_alive_nodes=max(1, int(min_alive_nodes)),
                initial_alive=max(0, int(initial_alive)),
            )

    @staticmethod
    def _replay_bounded_order(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        ordered: list[dict[str, Any]] = []
        for offset in range(0, len(events), 8):
            chunk = [dict(item) for item in events[offset : offset + 8]]
            positions_by_group: dict[str, list[int]] = defaultdict(list)
            for index, item in enumerate(chunk):
                # Keep roughly 15% of positions untouched as unknown exploration.
                if (offset + index) % 7 == 0:
                    continue
                positions_by_group[str(item.get("group") or "global")].append(index)
            for positions in positions_by_group.values():
                candidates = [chunk[index] for index in positions]
                candidates.sort(key=lambda item: -float(item.get("score", 0.0) or 0.0))
                for index, candidate in zip(positions, candidates):
                    chunk[index] = candidate
            ordered.extend(chunk)
        return ordered

    @staticmethod
    def _replay_service_until_passes(
        events: Sequence[Mapping[str, Any]],
        required_passes: int,
    ) -> float | None:
        if required_passes <= 0:
            return 0.0
        elapsed = 0.0
        passes = 0
        for event in events:
            elapsed += max(0.001, float(event.get("elapsed", 0.001) or 0.001))
            if bool(event.get("passed")):
                passes += 1
                if passes >= required_passes:
                    return elapsed
        return None

    @staticmethod
    def _replay_passes_within_service(
        events: Sequence[Mapping[str, Any]],
        service_budget: float,
    ) -> int:
        elapsed = 0.0
        passes = 0
        for event in events:
            next_elapsed = elapsed + max(
                0.001,
                float(event.get("elapsed", 0.001) or 0.001),
            )
            if next_elapsed > service_budget:
                break
            elapsed = next_elapsed
            passes += 1 if bool(event.get("passed")) else 0
        return passes

    def _finalize_shadow_replay(
        self,
        *,
        gate_loss_ms: int,
        alive_at_20s: int,
        target_alive: int,
        min_alive_nodes: int,
        initial_alive: int,
    ) -> None:
        events = self._current_replay_events
        self._current_replay_events = []
        if not events:
            return
        bounded_events = self._replay_bounded_order(events)
        gate_passes = max(0, min_alive_nodes - initial_alive)
        baseline_service = self._replay_service_until_passes(events, gate_passes)
        bounded_service = self._replay_service_until_passes(bounded_events, gate_passes)
        bounded_gate_loss = float(gate_loss_ms)
        if baseline_service is not None and bounded_service is not None:
            bounded_gate_loss = float(gate_loss_ms) * (
                bounded_service / max(0.001, baseline_service)
            )
        elif bounded_service is None:
            bounded_gate_loss = max(float(gate_loss_ms), 120_000.0)

        actual_new_passes = max(0, alive_at_20s - initial_alive)
        baseline_horizon = self._replay_service_until_passes(
            events,
            actual_new_passes,
        )
        bounded_alive_at_20s = alive_at_20s
        if baseline_horizon is not None and actual_new_passes > 0:
            bounded_alive_at_20s = min(
                target_alive,
                initial_alive
                + self._replay_passes_within_service(
                    bounded_events,
                    baseline_horizon,
                ),
            )
        self._shadow_replay_sessions.append(
            {
                "baseline_gate_ms": float(gate_loss_ms),
                "bounded_gate_ms": max(0.0, bounded_gate_loss),
                "baseline_gap_20s": float(max(0, target_alive - alive_at_20s)),
                "bounded_gap_20s": float(
                    max(0, target_alive - bounded_alive_at_20s)
                ),
            }
        )
        if len(self._shadow_replay_sessions) < 200:
            return
        replay = list(self._shadow_replay_sessions)
        baseline_cvar = empirical_cvar95(
            [item["baseline_gate_ms"] for item in replay],
            minimum_samples=200,
        )
        bounded_cvar = empirical_cvar95(
            [item["bounded_gate_ms"] for item in replay],
            minimum_samples=200,
        )
        baseline_gap = expected_gap([item["baseline_gap_20s"] for item in replay])
        bounded_gap = expected_gap([item["bounded_gap_20s"] for item in replay])
        if None not in (baseline_cvar, bounded_cvar, baseline_gap, bounded_gap):
            self.record_policy_replay(
                sample_count=len(replay),
                baseline_cvar95_ms=float(baseline_cvar),
                bounded_cvar95_ms=float(bounded_cvar),
                baseline_gap_20s=float(baseline_gap),
                bounded_gap_20s=float(bounded_gap),
            )

    def objective_metrics(self) -> dict[str, Any]:
        with self._lock:
            cvar = empirical_cvar95(
                [item["gate_loss_ms"] for item in self._sessions],
                minimum_samples=self.config.min_cvar_sessions,
            )
            gap = expected_gap([item["target_gap_20s"] for item in self._sessions])
            return {
                "cvar95_time_to_gate_ms": int(round(cvar)) if cvar is not None else None,
                "expected_target_gap_20s": round(gap, 4) if gap is not None else None,
                "session_sample_count": len(self._sessions),
                "session_censored_count": sum(1 for item in self._sessions if item["censored"]),
            }

    def _calibration_ece(self) -> float:
        if not self._calibration:
            return 1.0
        bins: dict[int, list[tuple[float, int]]] = defaultdict(list)
        for prediction, actual, _transient in self._calibration:
            bins[min(9, max(0, int(prediction * 10.0)))].append((prediction, actual))
        total = len(self._calibration)
        ece = 0.0
        for values in bins.values():
            predicted = sum(item[0] for item in values) / len(values)
            actual = sum(item[1] for item in values) / len(values)
            ece += len(values) / float(total) * abs(predicted - actual)
        return max(0.0, min(1.0, ece))

    @staticmethod
    def _binary_regime_shift(previous: Sequence[int], recent: Sequence[int]) -> bool:
        if not previous or not recent:
            return False
        previous_successes = sum(1 for value in previous if int(value))
        recent_successes = sum(1 for value in recent if int(value))
        previous_rate = previous_successes / float(len(previous))
        recent_rate = recent_successes / float(len(recent))
        if abs(previous_rate - recent_rate) <= 0.15:
            return False
        previous_interval = (
            wilson_lower_bound(previous_successes, len(previous)),
            wilson_upper_bound(previous_successes, len(previous)),
        )
        recent_interval = (
            wilson_lower_bound(recent_successes, len(recent)),
            wilson_upper_bound(recent_successes, len(recent)),
        )
        return (
            previous_interval[1] < recent_interval[0]
            or recent_interval[1] < previous_interval[0]
        )

    def _drift_detected(self) -> bool:
        if self._drift_recovery_remaining > 0:
            return True
        if len(self._calibration) < 200:
            return False
        values = list(self._calibration)
        previous = values[-200:-50]
        recent = values[-50:]
        shifted = self._binary_regime_shift(
            [item[1] for item in previous],
            [item[1] for item in recent],
        )
        if len(self._facility_history) >= 200:
            facility_values = list(self._facility_history)
            shifted = shifted or self._binary_regime_shift(
                facility_values[-200:-50],
                facility_values[-50:],
            )
        if shifted:
            self._drift_recovery_remaining = 50
        return shifted

    def _rollout_state(self) -> tuple[str, float, float, bool]:
        if not self.config.enabled or self.config.rollout_mode in {"off", "disabled"}:
            return "disabled", 0.0, 1.0, False
        ece = self._calibration_ece()
        drift = self._drift_detected()
        if self.config.rollout_mode == "shadow" or drift:
            return "shadow", 0.0, ece, drift
        if (
            self.config.rollout_mode == "auto_bounded"
            and not bool(self._policy_replay.get("qualified"))
        ):
            return "shadow", 0.0, ece, drift
        if (
            self._candidate_observations < self.config.min_candidate_observations
            or self._candidate_passes < self.config.min_candidate_passes
            or ece > 0.10
        ):
            return "shadow", 0.0, ece, drift
        if (
            self._candidate_observations < self.config.mature_candidate_observations
            or self._candidate_passes < self.config.mature_candidate_passes
            or ece > 0.05
        ):
            return "bounded_10", 0.5, ece, drift
        return "bounded_20", 1.0, ece, drift

    def _facility_bounds(self, worker_count: int) -> tuple[float, float, float, int]:
        samples = list(self._facility_samples.get(max(1, int(worker_count)), ()))
        if not samples:
            return 1.0, math.inf, 1.0, 0
        failures = sum(1 for item in samples if item["failed"] > 0)
        resets = sum(1 for item in samples if item["reset"] > 0)
        latencies = sorted(item["reload_ms"] for item in samples)
        rank = min(len(latencies) - 1, max(0, int(math.ceil((len(latencies) + 1) * 0.95)) - 1))
        return (
            wilson_upper_bound(failures, len(samples)),
            latencies[rank],
            wilson_upper_bound(resets, len(samples)),
            len(samples),
        )

    def facility_diagnostics(self, worker_count: int) -> dict[str, float | int]:
        with self._lock:
            failure_upper, reload_p95, reset_upper, sample_count = self._facility_bounds(
                worker_count
            )
            samples = list(self._facility_samples.get(max(1, int(worker_count)), ()))
            return {
                "sample_count": sample_count,
                "failure_upper": failure_upper,
                "reload_p95_ms": reload_p95 if sample_count > 0 else 0.0,
                "reset_upper": reset_upper,
                "failure_rate": (
                    sum(item["failed"] for item in samples) / len(samples)
                    if samples
                    else 0.0
                ),
                "reset_rate": (
                    sum(item["reset"] for item in samples) / len(samples)
                    if samples
                    else 0.0
                ),
            }

    def _reachability(
        self,
        snapshot: Tier3ControlSnapshot,
        *,
        pass_lcb: float,
        pass_sample_count: int,
        facility_failure_upper: float,
    ) -> tuple[Tier3ReachabilityClass, float]:
        deficit = max(0, snapshot.target_alive - snapshot.alive_count)
        if deficit <= 0:
            return Tier3ReachabilityClass.FEASIBLE, 1.0
        if snapshot.probe_endpoint_limited:
            return Tier3ReachabilityClass.FACILITY_LIMITED, 0.95
        backlog = max(
            0,
            snapshot.primary_depth
            + snapshot.canonical_depth
            + snapshot.retry_depth
            + snapshot.supported_backlog_count,
        )
        if facility_failure_upper > 0.05 and snapshot.facility_validation_count >= 20:
            return Tier3ReachabilityClass.FACILITY_LIMITED, min(1.0, snapshot.facility_validation_count / 64.0)
        predicted_lcb = backlog * pass_lcb
        if pass_sample_count >= 20 and predicted_lcb >= deficit:
            return Tier3ReachabilityClass.FEASIBLE, min(1.0, backlog / float(max(1, deficit * 4)))
        source_upper = snapshot.source_remaining_upper
        if source_upper is not None and backlog + max(0, int(source_upper)) < deficit:
            return Tier3ReachabilityClass.SUPPLY_LIMITED, 0.9
        if (
            snapshot.active_workers >= self._safe_worker_cap
            and backlog > snapshot.active_workers * 2
            and pass_sample_count >= 20
            and snapshot.facility_validation_count >= 20
            and facility_failure_upper <= 0.05
        ):
            return Tier3ReachabilityClass.RESOURCE_LIMITED, 0.75
        return Tier3ReachabilityClass.UNCERTAIN, 0.25

    @staticmethod
    def _canary_saturated(snapshot: Tier3ControlSnapshot, cap: int) -> bool:
        return bool(
            snapshot.active_workers >= max(1, int(cap))
            and (
                snapshot.primary_depth + snapshot.canonical_depth + snapshot.retry_depth
                > snapshot.active_workers * 2
                or max(
                    snapshot.primary_oldest_wait_ms,
                    snapshot.canonical_oldest_wait_ms,
                    snapshot.retry_oldest_wait_ms,
                )
                > 1000.0
            )
        )

    def _enter_canary_cooldown(self, snapshot: Tier3ControlSnapshot, *, rollback: bool) -> None:
        testing = self._canary_state.startswith("testing_")
        if rollback and not testing:
            self._safe_worker_cap = max(
                min(self.config.initial_safe_worker_cap, self.config.hard_worker_max),
                self._safe_worker_cap - self.config.canary_step,
            )
        self._canary_state = "cooldown"
        self._canary_reason = "canary_guard_regression"
        self._canary_cooldown_until = (
            float(snapshot.now_monotonic) + self.config.canary_cooldown_seconds
        )
        self._canary_cooldown_until_epoch = time.time() + self.config.canary_cooldown_seconds
        self._saturated_samples = 0
        self._canary_regression_streak = 0
        self._fast_canary_target_active = False

    def _begin_canary(self, snapshot: Tier3ControlSnapshot, target: int) -> None:
        self._canary_state = f"testing_{int(target)}"
        self._canary_reason = f"healthy_saturated_canary_{int(target)}"
        self._canary_started_at = float(snapshot.now_monotonic)
        self._canary_validation_start = int(snapshot.validation_completion_count)
        self._canary_pass_start = max(
            0,
            int(snapshot.primary_pass_count)
            + int(snapshot.canonical_pass_count)
            + int(snapshot.retry_pass_count),
        )
        self._canary_capacity_epoch_id = max(
            0,
            int(snapshot.capacity_epoch_id),
        )
        self._canary_commit_start = max(
            0,
            int(snapshot.capacity_epoch_committed_count),
        )
        self._canary_baseline_throughput = float(snapshot.recent_admissions_per_second)
        self._canary_baseline_completion_throughput = float(
            snapshot.validation_completion_throughput
        )
        self._canary_baseline_busy_admission_lcb = (
            self._busy_worker_admission_lcb(snapshot)
        )
        self._canary_baseline_failure = float(snapshot.facility_failure_rate)
        self._canary_baseline_reload_ms = float(snapshot.warm_reload_p95_ms)
        self._canary_regression_streak = 0
        self._fast_canary_target_active = False

    @staticmethod
    def _busy_worker_admission_lcb(snapshot: Tier3ControlSnapshot) -> float:
        if int(snapshot.capacity_epoch_id) > 0:
            completed = max(0, int(snapshot.capacity_epoch_completed_count))
            passed = max(0, int(snapshot.capacity_epoch_committed_count))
            occupied_seconds = max(
                0.0,
                float(snapshot.capacity_epoch_occupied_ms) / 1000.0,
            )
        else:
            completed = max(
                0,
                int(snapshot.primary_completed_count)
                + int(snapshot.canonical_completed_count)
                + int(snapshot.retry_completed_count),
            )
            passed = max(
                0,
                int(snapshot.primary_pass_count)
                + int(snapshot.canonical_pass_count)
                + int(snapshot.retry_pass_count),
            )
            occupied_seconds = max(
                0.0,
                (
                    float(snapshot.primary_occupied_ms)
                    + float(snapshot.canonical_occupied_ms)
                    + float(snapshot.retry_occupied_ms)
                )
                / 1000.0,
            )
        if completed <= 0 or occupied_seconds <= 0.0:
            return 0.0
        return wilson_lower_bound(passed, completed) * completed / occupied_seconds

    def _update_fast_canary(self, snapshot: Tier3ControlSnapshot) -> None:
        now = float(snapshot.now_monotonic)
        hard_cap = max(1, int(self.config.hard_worker_max))
        fast_cap = min(20, hard_cap)
        if self._safe_worker_cap > hard_cap:
            self._safe_worker_cap = hard_cap

        if not self.config.baseline_capacity_canary_enabled:
            self._safe_worker_cap = min(
                hard_cap,
                max(1, int(self.config.initial_safe_worker_cap)),
            )
            self._canary_state = "baseline_locked"
            self._canary_reason = "validator_baseline_recovery"
            self._saturated_samples = 0
            self._canary_regression_streak = 0
            self._fast_canary_target_active = False
            return

        new_deadline_violation = (
            snapshot.deadline_violation_count > self._last_deadline_violation_count
        )
        new_hard_failure = (
            snapshot.hard_facility_failure_count
            > self._last_hard_facility_failure_count
        )
        self._last_deadline_violation_count = max(
            self._last_deadline_violation_count,
            int(snapshot.deadline_violation_count),
        )
        self._last_hard_facility_failure_count = max(
            self._last_hard_facility_failure_count,
            int(snapshot.hard_facility_failure_count),
        )
        if new_deadline_violation or new_hard_failure:
            self._enter_canary_cooldown(snapshot, rollback=self._safe_worker_cap > 16)
            self._canary_reason = (
                "deadline_violation" if new_deadline_violation else "hard_facility_failure"
            )
            return

        if snapshot.probe_endpoint_limited:
            self._safe_worker_cap = min(
                self._safe_worker_cap,
                self.config.initial_safe_worker_cap,
                16,
            )
            self._canary_state = "probe_endpoint_limited"
            self._canary_reason = "probe_endpoint_limited"
            self._saturated_samples = 0
            return
        if self._canary_state == "probe_endpoint_limited":
            self._canary_state = "stable"
        if now < self._canary_cooldown_until:
            self._canary_state = "cooldown"
            return
        if self._canary_state == "cooldown":
            self._canary_state = "stable"

        testing_target = 0
        if self._canary_state.startswith("testing_"):
            testing_target = int(self._canary_state.rsplit("_", 1)[-1])
        if testing_target and testing_target <= fast_cap:
            if int(snapshot.active_workers) < testing_target:
                return
            if not self._fast_canary_target_active:
                self._fast_canary_target_active = True
                self._canary_started_at = now
                self._canary_validation_start = int(
                    snapshot.validation_completion_count
                )
                self._canary_pass_start = max(
                    0,
                    int(snapshot.primary_pass_count)
                    + int(snapshot.canonical_pass_count)
                    + int(snapshot.retry_pass_count),
                )
                self._canary_capacity_epoch_id = max(
                    0,
                    int(snapshot.capacity_epoch_id),
                )
                self._canary_commit_start = max(
                    0,
                    int(snapshot.capacity_epoch_committed_count),
                )
                return
            if int(snapshot.capacity_epoch_id) != self._canary_capacity_epoch_id:
                self._canary_capacity_epoch_id = max(
                    0,
                    int(snapshot.capacity_epoch_id),
                )
                self._canary_started_at = now
                self._canary_validation_start = int(
                    snapshot.validation_completion_count
                )
                self._canary_commit_start = max(
                    0,
                    int(snapshot.capacity_epoch_committed_count),
                )
                return
            elapsed = max(0.0, now - self._canary_started_at)
            validations = max(
                0,
                int(snapshot.validation_completion_count) - self._canary_validation_start,
            )
            completion_ratio = (
                1.0
                if self._canary_baseline_completion_throughput <= 0.0
                else float(snapshot.validation_completion_throughput)
                / self._canary_baseline_completion_throughput
            )
            current_busy_admission_lcb = self._busy_worker_admission_lcb(snapshot)
            if self._canary_baseline_busy_admission_lcb <= 0.0:
                busy_admission_ratio = (
                    float("inf") if current_busy_admission_lcb > 0.0 else 0.0
                )
            else:
                busy_admission_ratio = (
                    current_busy_admission_lcb
                    / self._canary_baseline_busy_admission_lcb
                )
            current_passes = max(
                0,
                int(snapshot.primary_pass_count)
                + int(snapshot.canonical_pass_count)
                + int(snapshot.retry_pass_count),
            )
            new_admissions = max(
                0,
                int(snapshot.capacity_epoch_committed_count)
                - self._canary_commit_start,
            )
            reload_limit = max(
                1250.0,
                self._canary_baseline_reload_ms * 1.25,
            )
            regressed = bool(
                completion_ratio < 0.80
                or (
                    self._canary_baseline_busy_admission_lcb > 0.0
                    and busy_admission_ratio < 0.80
                )
                or snapshot.facility_failure_rate > self._canary_baseline_failure + 0.03
                or (
                    snapshot.warm_reload_p95_ms > 0.0
                    and snapshot.warm_reload_p95_ms > reload_limit
                )
            )
            self._canary_regression_streak = (
                self._canary_regression_streak + 1 if regressed else 0
            )
            if self._canary_regression_streak >= 2:
                self._enter_canary_cooldown(snapshot, rollback=True)
                return
            if elapsed < 3.0 or validations < 16:
                return
            throughput_ok = bool(
                new_admissions > 0
                and current_busy_admission_lcb > 0.0
                and busy_admission_ratio >= 0.95
            )
            completion_guard_ok = completion_ratio >= 0.80
            failure_ok = (
                snapshot.facility_failure_rate
                <= self._canary_baseline_failure + 0.03
            )
            reload_ok = bool(
                snapshot.warm_reload_p95_ms <= 0.0
                or snapshot.warm_reload_p95_ms <= reload_limit
            )
            if throughput_ok and completion_guard_ok and failure_ok and reload_ok:
                self._safe_worker_cap = min(fast_cap, testing_target)
                self._canary_state = "stable"
                self._canary_reason = f"fast_canary_{testing_target}_promoted"
                self._saturated_samples = 0
                self._canary_regression_streak = 0
                self._fast_canary_target_active = False
            else:
                self._enter_canary_cooldown(snapshot, rollback=True)
            return

        if fast_cap <= 16 or self._safe_worker_cap < 16 or self._safe_worker_cap >= fast_cap:
            return
        saturated = self._canary_saturated(snapshot, self._safe_worker_cap)
        self._saturated_samples = self._saturated_samples + 1 if saturated else 0
        qualified = bool(
            self._saturated_samples >= 2
            and snapshot.session_elapsed_seconds >= 3.0
            # Facility work proves that another process is safe to start, but
            # promotion still requires positive admission yield per busy
            # worker-second. Completion throughput alone is not capacity value.
            and snapshot.facility_validation_count >= 16
            and (
                int(snapshot.primary_completed_count)
                + int(snapshot.canonical_completed_count)
                + int(snapshot.retry_completed_count)
            )
            >= 16
            and snapshot.hard_facility_failure_count <= 0
            and snapshot.deadline_violation_count <= 0
            and snapshot.facility_failure_rate <= 0.0
            and int(snapshot.capacity_epoch_committed_count) > 0
            and self._busy_worker_admission_lcb(snapshot) > 0.0
            and (
                snapshot.warm_reload_p95_ms <= 0.0
                or snapshot.warm_reload_p95_ms <= 1000.0
            )
        )
        if qualified:
            target = min(fast_cap, self._safe_worker_cap + self.config.canary_step)
            self._begin_canary(snapshot, target)

    def _update_strict_canary(self, snapshot: Tier3ControlSnapshot, *, stage: str) -> None:
        now = float(snapshot.now_monotonic)
        hard_cap = max(1, self.config.hard_worker_max)
        if self._safe_worker_cap > hard_cap:
            self._safe_worker_cap = hard_cap
        if self._safe_worker_cap < 20:
            return
        if stage != "bounded_20" or self._safe_worker_cap >= hard_cap:
            return
        if now < self._canary_cooldown_until:
            self._canary_state = "cooldown"
            return

        failure_upper, reload_upper, reset_upper, sample_count = self._facility_bounds(
            self._safe_worker_cap
        )
        saturated = self._canary_saturated(snapshot, self._safe_worker_cap)
        if saturated:
            self._saturated_samples += 1
        else:
            self._saturated_samples = 0

        if self._canary_state.startswith("testing_"):
            target = int(self._canary_state.rsplit("_", 1)[-1])
            validations = max(0, snapshot.facility_validation_count - self._canary_validation_start)
            elapsed = max(0.0, now - self._canary_started_at)
            if elapsed < 15.0 and validations < 64:
                return
            throughput_ok = (
                snapshot.recent_admissions_per_second > 0.0
                if self._canary_baseline_throughput <= 0.0
                else snapshot.recent_admissions_per_second
                >= self._canary_baseline_throughput * 1.05
            )
            failure_ok = snapshot.facility_failure_rate <= self._canary_baseline_failure + 0.03
            reload_ok = (
                self._canary_baseline_reload_ms <= 0
                or snapshot.warm_reload_p95_ms <= self._canary_baseline_reload_ms * 1.25
            )
            if throughput_ok and failure_ok and reload_ok:
                self._safe_worker_cap = min(hard_cap, target)
                self._canary_state = "stable"
                self._canary_reason = f"strict_canary_{target}_promoted"
            else:
                self._canary_state = "cooldown"
                self._canary_cooldown_until = now + self.config.canary_cooldown_seconds
                self._canary_cooldown_until_epoch = (
                    time.time() + self.config.canary_cooldown_seconds
                )
            return

        marginal_lower = (
            self.config.canary_step / float(max(1, self._safe_worker_cap))
        ) * max(0.0, 1.0 - failure_upper)
        qualified = (
            self._saturated_samples >= 3
            and sample_count >= 64
            and failure_upper <= 0.05
            and reload_upper <= 1000.0
            and marginal_lower >= 0.08
        )
        if not qualified:
            self._canary_state = "qualified_pending" if self._saturated_samples >= 3 else "stable"
            return
        target = min(hard_cap, self._safe_worker_cap + self.config.canary_step)
        self._canary_state = f"testing_{target}"
        self._canary_reason = f"strict_canary_{target}_started"
        self._canary_started_at = now
        self._canary_validation_start = snapshot.facility_validation_count
        self._canary_baseline_throughput = snapshot.recent_admissions_per_second
        self._canary_baseline_failure = snapshot.facility_failure_rate
        self._canary_baseline_reload_ms = snapshot.warm_reload_p95_ms

    def evaluate(self, snapshot: Tier3ControlSnapshot) -> Tier3RiskDecision:
        with self._lock:
            self._refresh_published_model(snapshot.now_monotonic)
            stage, calibration_lambda, ece, drift = self._rollout_state()
            if stage == "disabled":
                safe_cap = min(
                    self.config.initial_safe_worker_cap,
                    self.config.hard_worker_max,
                )
            else:
                if (
                    self._last_fast_canary_evaluation_at <= 0.0
                    or snapshot.now_monotonic - self._last_fast_canary_evaluation_at
                    >= 0.5
                ):
                    self._update_fast_canary(snapshot)
                    self._last_fast_canary_evaluation_at = snapshot.now_monotonic
                envelope_interval = self.config.envelope_interval_ms / 1000.0
                if (
                    self._last_envelope_evaluation_at <= 0.0
                    or snapshot.now_monotonic - self._last_envelope_evaluation_at
                    >= envelope_interval
                ):
                    self._update_strict_canary(snapshot, stage=stage)
                    self._last_envelope_evaluation_at = snapshot.now_monotonic
                if self._canary_state.startswith("testing_"):
                    canary_target = int(self._canary_state.rsplit("_", 1)[-1])
                    safe_cap = min(canary_target, self.config.hard_worker_max)
                else:
                    safe_cap = min(self._safe_worker_cap, self.config.hard_worker_max)

            global_cell = self._published_hazards.get("global", _HazardCell())
            pass_lcb = global_cell.pass_lcb()
            facility_upper, _reload_upper, _reset_upper, facility_samples = self._facility_bounds(
                max(1, snapshot.active_workers)
            )
            if facility_samples <= 0:
                facility_upper = max(0.0, min(1.0, snapshot.facility_failure_rate))
            reachability, confidence = self._reachability(
                snapshot,
                pass_lcb=pass_lcb,
                pass_sample_count=global_cell.sample_count,
                facility_failure_upper=facility_upper,
            )
            bounded_delta = 0
            if stage in {"bounded_10", "bounded_20"}:
                if reachability is Tier3ReachabilityClass.RESOURCE_LIMITED:
                    bounded_delta = min(2, self.config.canary_step)
                elif reachability is Tier3ReachabilityClass.FACILITY_LIMITED:
                    bounded_delta = -min(2, self.config.canary_step)
            desired = max(
                1,
                min(
                    safe_cap,
                    int(snapshot.pi_desired_workers) + bounded_delta,
                ),
            )
            if self._canary_state.startswith("testing_"):
                # A canary that never allocates the tested +2 workers cannot
                # produce counterfactual facility or throughput evidence.
                desired = safe_cap
            allocation = lane_allocation(
                max(1, snapshot.active_workers or desired),
                primary_depth=snapshot.primary_depth,
                canonical_depth=snapshot.canonical_depth,
                retry_depth=snapshot.retry_depth,
                primary_wait_ms=snapshot.primary_oldest_wait_ms,
                canonical_wait_ms=snapshot.canonical_oldest_wait_ms,
                retry_wait_ms=snapshot.retry_oldest_wait_ms,
                previous=self._last_allocation,
            )
            self._last_allocation = allocation
            source_order = self.rank_sources(snapshot.source_states, advance_round=False)
            metrics = self.objective_metrics()
            decision = Tier3RiskDecision(
                rollout_stage=stage,
                calibration_lambda=calibration_lambda,
                calibration_ece=ece,
                drift_detected=drift,
                desired_workers=desired,
                safe_worker_cap=safe_cap,
                bounded_worker_delta=bounded_delta,
                lane_allocation=allocation,
                candidate_correction_limit=(
                    self.config.max_learning_correction * calibration_lambda
                ),
                source_order=source_order,
                source_tokens=max(1, min(len(source_order) or 1, desired)),
                reachability_class=reachability,
                reachability_confidence=confidence,
                cvar95_time_to_gate_ms=metrics["cvar95_time_to_gate_ms"],
                expected_target_gap_20s=metrics["expected_target_gap_20s"],
                canary_state=self._canary_state,
                policy_replay_sample_count=int(
                    self._policy_replay.get("sample_count", 0) or 0
                ),
                policy_replay_qualified=bool(
                    self._policy_replay.get("qualified")
                ),
                primary_queue_debt=(
                    snapshot.primary_depth
                    + max(0.0, snapshot.primary_oldest_wait_ms) / 250.0
                ),
                canonical_queue_debt=(
                    snapshot.canonical_depth
                    + max(0.0, snapshot.canonical_oldest_wait_ms) / 500.0
                ),
                retry_queue_debt=(
                    snapshot.retry_depth
                    + max(0.0, snapshot.retry_oldest_wait_ms) / 1000.0
                ),
                primary_oldest_wait_ms=snapshot.primary_oldest_wait_ms,
                canonical_oldest_wait_ms=snapshot.canonical_oldest_wait_ms,
                retry_oldest_wait_ms=snapshot.retry_oldest_wait_ms,
                primary_hazard_lcb=global_cell.pass_lcb_from_age(0.0),
                canonical_hazard_lcb=global_cell.pass_lcb_from_age(3.001),
                retry_hazard_lcb=global_cell.pass_lcb_from_age(8.001),
                primary_pass_throughput=snapshot.primary_pass_throughput,
                canonical_pass_throughput=snapshot.canonical_pass_throughput,
                retry_pass_throughput=snapshot.retry_pass_throughput,
                canary_reason=self._canary_reason,
            )
            return decision

    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            stage, calibration_lambda, ece, drift = self._rollout_state()
            return {
                "schema_version": TIER3_RISK_CONTROL_SCHEMA_VERSION,
                "validation_contract_hash": TIER3_VALIDATION_CONTRACT_HASH,
                "state_migration_reason": self._state_migration_reason,
                "risk_control_stage": stage,
                "calibration_lambda": round(calibration_lambda, 4),
                "calibration_ece": round(ece, 4),
                "drift_detected": drift,
                "candidate_observation_count": self._candidate_observations,
                "candidate_pass_count": self._candidate_passes,
                "safe_worker_cap": self._safe_worker_cap,
                "source_policy_replay_improvement": self._source_policy_replay_improvement,
                "worker_canary_state": self._canary_state,
                "worker_canary_reason": self._canary_reason,
                "risk_model_last_refresh_monotonic": self._last_model_refresh_at,
                "policy_replay": dict(self._policy_replay),
                "canary_cooldown_until_epoch": self._canary_cooldown_until_epoch,
                "shadow_replay_session_count": len(self._shadow_replay_sessions),
                **self.objective_metrics(),
                "source": self.source_diagnostics(),
            }

    def save(self) -> None:
        if self.state_path is None:
            return
        with self._lock:
            payload = {
                "schema_version": TIER3_RISK_CONTROL_SCHEMA_VERSION,
                "validation_contract_hash": TIER3_VALIDATION_CONTRACT_HASH,
                "generated_at": time.time(),
                "candidate_observations": self._candidate_observations,
                "candidate_passes": self._candidate_passes,
                "safe_worker_cap": self._safe_worker_cap,
                "source_policy_replay_improvement": self._source_policy_replay_improvement,
                "policy_replay": dict(self._policy_replay),
                "canary_cooldown_until_epoch": self._canary_cooldown_until_epoch,
                "hazards": {key: cell.to_dict() for key, cell in self._hazards.items()},
                "sources": {key: value.to_dict() for key, value in self._sources.items()},
                "calibration": list(self._calibration),
                "sessions": list(self._sessions),
                "facility": {
                    str(key): list(values) for key, values in self._facility_samples.items()
                },
                "facility_history": list(self._facility_history),
                "shadow_replay_sessions": list(self._shadow_replay_sessions),
            }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=True, separators=(",", ":")), encoding="utf-8")
        os.replace(temp_path, self.state_path)

    def _load_state(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            schema_changed = (
                int(payload.get("schema_version", 0) or 0)
                != TIER3_RISK_CONTROL_SCHEMA_VERSION
            )
            contract_changed = (
                str(payload.get("validation_contract_hash") or "")
                != TIER3_VALIDATION_CONTRACT_HASH
            )
            if schema_changed or contract_changed:
                sources = payload.get("sources")
                if isinstance(sources, dict):
                    self._sources = {
                        str(key): _SourcePosterior.from_dict(value)
                        for key, value in sources.items()
                        if isinstance(value, Mapping)
                    }
                self._state_migration_reason = (
                    "schema_version_changed"
                    if schema_changed
                    else "validation_contract_changed"
                )
                self._safe_worker_cap = min(
                    16,
                    self.config.initial_safe_worker_cap,
                    self.config.hard_worker_max,
                )
                return
            hazards = payload.get("hazards")
            sources = payload.get("sources")
            calibration = payload.get("calibration")
            sessions = payload.get("sessions")
            facility = payload.get("facility")
            facility_history = payload.get("facility_history")
            shadow_replay_sessions = payload.get("shadow_replay_sessions")
            if isinstance(hazards, dict):
                self._hazards = {
                    str(key): _HazardCell.from_dict(value)
                    for key, value in hazards.items()
                    if isinstance(value, Mapping)
                }
            if isinstance(sources, dict):
                self._sources = {
                    str(key): _SourcePosterior.from_dict(value)
                    for key, value in sources.items()
                    if isinstance(value, Mapping)
                }
            if isinstance(calibration, list):
                self._calibration.extend(
                    (
                        max(0.0, min(1.0, float(item[0]))),
                        1 if int(item[1]) else 0,
                        1 if int(item[2]) else 0,
                    )
                    for item in calibration[-4096:]
                    if isinstance(item, (list, tuple)) and len(item) == 3
                )
            if isinstance(sessions, list):
                self._sessions.extend(
                    dict(item) for item in sessions[-256:] if isinstance(item, Mapping)
                )
            if isinstance(facility, dict):
                for key, values in facility.items():
                    if not isinstance(values, list):
                        continue
                    target = self._facility_samples[max(1, int(key))]
                    target.extend(dict(item) for item in values[-256:] if isinstance(item, Mapping))
            if isinstance(facility_history, list):
                self._facility_history.extend(
                    1 if int(item) else 0 for item in facility_history[-4096:]
                )
            if isinstance(shadow_replay_sessions, list):
                self._shadow_replay_sessions.extend(
                    {
                        "baseline_gate_ms": max(
                            0.0,
                            float(item.get("baseline_gate_ms", 0.0) or 0.0),
                        ),
                        "bounded_gate_ms": max(
                            0.0,
                            float(item.get("bounded_gate_ms", 0.0) or 0.0),
                        ),
                        "baseline_gap_20s": max(
                            0.0,
                            float(item.get("baseline_gap_20s", 0.0) or 0.0),
                        ),
                        "bounded_gap_20s": max(
                            0.0,
                            float(item.get("bounded_gap_20s", 0.0) or 0.0),
                        ),
                    }
                    for item in shadow_replay_sessions[-256:]
                    if isinstance(item, Mapping)
                )
            self._candidate_observations = max(
                0,
                int(payload.get("candidate_observations", 0) or 0),
            )
            self._candidate_passes = max(0, int(payload.get("candidate_passes", 0) or 0))
            self._safe_worker_cap = max(
                1,
                min(
                    self.config.hard_worker_max,
                    int(payload.get("safe_worker_cap", self.config.initial_safe_worker_cap) or 0),
                ),
            )
            self._refresh_published_model(time.monotonic(), force=True)
            replay_improvement = payload.get("source_policy_replay_improvement")
            self._source_policy_replay_improvement = (
                float(replay_improvement)
                if replay_improvement is not None
                else None
            )
            replay_payload = payload.get("policy_replay")
            if isinstance(replay_payload, Mapping):
                self._policy_replay = {
                    "sample_count": max(
                        0,
                        int(replay_payload.get("sample_count", 0) or 0),
                    ),
                    "baseline_cvar95_ms": replay_payload.get("baseline_cvar95_ms"),
                    "bounded_cvar95_ms": replay_payload.get("bounded_cvar95_ms"),
                    "baseline_gap_20s": replay_payload.get("baseline_gap_20s"),
                    "bounded_gap_20s": replay_payload.get("bounded_gap_20s"),
                    "qualified": bool(replay_payload.get("qualified")),
                }
            cooldown_epoch = max(
                0.0,
                float(payload.get("canary_cooldown_until_epoch", 0.0) or 0.0),
            )
            self._canary_cooldown_until_epoch = cooldown_epoch
            cooldown_remaining = max(0.0, cooldown_epoch - time.time())
            if cooldown_remaining > 0.0:
                self._canary_cooldown_until = time.monotonic() + cooldown_remaining
                self._canary_state = "cooldown"
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            # Corrupt or incompatible learning state is never a startup blocker.
            self._state_migration_reason = "state_corrupt"
            self._hazards = {}
            self._published_hazards = {}
            self._last_model_refresh_at = 0.0
            self._sources = {}
            self._calibration.clear()
            self._sessions.clear()
            self._facility_samples.clear()
            self._facility_history.clear()
            self._shadow_replay_sessions.clear()
            self._current_replay_events = []
            self._candidate_observations = 0
            self._candidate_passes = 0
            self._safe_worker_cap = min(
                self.config.initial_safe_worker_cap,
                self.config.hard_worker_max,
            )
            self._policy_replay = {
                "sample_count": 0,
                "baseline_cvar95_ms": None,
                "bounded_cvar95_ms": None,
                "baseline_gap_20s": None,
                "bounded_gap_20s": None,
                "qualified": False,
            }
            self._canary_cooldown_until = 0.0
            self._canary_cooldown_until_epoch = 0.0
