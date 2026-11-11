from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping


PROTOCOL_SCHEMA_VERSION = 16
VALIDATION_CONTRACT_MATERIAL = (
    b"tier3-process-endpoint-work-v2|one-endpoint-per-helper-command|"
    b"one-candidate-transaction|one-terminal|job-bound|"
    b"fast-one-3s-endpoint|canonical-one-5s-endpoint|"
    b"fast-censor-retains-canonical-entitlement|ordered-endpoint-ledger|"
    b"one-facility-redispatch|deterministic-fast-deep-stage-recovery|"
    b"actor-owned-endpoint-controller|stratified-fast-coverage|"
    b"root-family-drr|canonical-continuation-cohort|"
    b"real-transaction-window-backpressure|"
    b"endpoint-learning-shadow-only|priority-control-events|"
    b"latched-helper-shell-buffer-4|mihomo-start-bulkhead-4|"
    b"nonblocking-parallel-job-retirement|parallel-job-terminate-bulkhead-v2|"
    b"caller-watchdog-job-termination-early|nonblocking-lifecycle-stop-trace|"
    b"off-owner-resource-sampling|explicit-domain-stop-owner|global-stop-deadline|"
    b"two-level-credit-owner-8x-ready-2x|durable-root-family-ingress|"
    b"endpoint-entitlement-fact-separated-from-completion|"
    b"shared-single-proxy-config-builder|"
    b"rolling-pi-scan-token-no-lifetime-cap|"
    b"deep-endpoint-reparks-resident-credit|"
    b"candidate-dispatch-ticket-tcp-lease-feedback-only-v2|"
    b"actor-owned-bounded-deep-retention-v1|"
    b"continuation-feedback-after-retention-decision-v1|"
    b"actor-accepted-fast-observation-before-retention-v1|"
    b"escape-lease-deep-continuation-priority-v1|"
    b"unified-validation-feedback-and-durable-commit-timeline-v1|"
    b"one-command-current-attempt-facts-only-v1|"
    b"single-250ms-pi-envelope-and-actor-actual-slots-v1|"
    b"rolling-lane-yield-60s-or-64-completions|"
    b"fixed-fast-entitlement-3000ms|async-worker-starter-bulkhead-2|"
    b"controller-and-mixed-port-prewarm-readiness|"
    b"supervisor-generation-owned-distinct-ports-v1|"
    b"bounded-helper-startup-progress-phases-v1|"
    b"dispatch-ack-facility-transport-deadline-phases|"
    b"validation-purpose-auto-admission-or-user-pool-health|"
    b"concurrent-global-bounded-stop|"
    b"generation-bound-idle-helper-capacity-lease-v2|"
    b"supervisor-owner-send-latency-v1|"
    b"facility-2s|fast-3s|canonical-endpoint-5s|delivery-slack-750ms|"
    b"readonly-network-context-no-policy-input"
    # No bump for the mainstream HTTPS confirm (2026-09-25): replayed PASS
    # records are admitted only through a live validation, which now includes
    # the confirm, so a stale PASS cannot re-enter the pool.  Bumping would
    # only demote the whole cache to slow re-probe scheduling and reset the
    # learned worker cap, costing the first session after the upgrade.
)
TIER3_VALIDATION_CONTRACT_HASH = hashlib.sha256(
    VALIDATION_CONTRACT_MATERIAL
).hexdigest()[:24]

# Mainstream HTTPS confirm (2026-09-25).  A portfolio endpoint PASS proves the
# node relays a connectivity check, yet about half of the nodes that passed one
# SSL-EOF on every real HTTPS site (fresh pool: 11/14 pass gstatic, 5/14 load
# Google).  After the scheduled endpoint PASSES, the helper loads each
# configured confirm URL once through the same node with TLS verification.
# Its fact trails the portfolio fact and carries this stage, so endpoint
# ledgers and the endpoint controller can tell the two apart.
CONFIRM_HTTPS_STAGE = "confirm_https"
CONFIRM_HTTPS_FAILED_REASON = "confirm_https_failed"
CONFIRM_HTTPS_CENSORED_REASON = "confirm_https_censored"
# Per-URL ceiling; the helper also caps it at its probe timeout.
CONFIRM_HTTPS_BUDGET_SECONDS = 5.0
# A transport-class confirm failure (reset/EOF, 5xx) is sampled once more
# before it becomes a verdict, so one hiccup cannot cost a good node its 6 h
# negative-cache cooldown.  An SSL-EOF node fails the repeat in about one RTT.
# An explicit answer (4xx, a 200 page, a 204 with a body) needs no repeat.
CONFIRM_HTTPS_ATTEMPTS_PER_URL = 2


def is_confirm_https_fact(fact: Any) -> bool:
    return str(getattr(fact, "stage", "") or "") == CONFIRM_HTTPS_STAGE


def confirm_https_budget_seconds(
    confirm_urls: Any,
    probe_timeout_seconds: float,
) -> float:
    """Worst-case confirm time one PASSING slice adds to its lease."""

    count = sum(1 for item in (confirm_urls or ()) if str(item or "").strip())
    if count <= 0:
        return 0.0
    per_url = min(
        CONFIRM_HTTPS_BUDGET_SECONDS,
        max(0.25, float(probe_timeout_seconds)),
    )
    return float(count) * per_url

# Cold helper startup is a process-facility concern, separate from candidate
# facility and probe entitlements. Windows can take several seconds to make a
# newly started Mihomo controller reachable when multiple helpers prewarm.
TIER3_HELPER_PREWARM_READY_TIMEOUT_SECONDS = 8.0
TIER3_HELPER_START_TIMEOUT_SECONDS = 10.0
TIER3_HELPER_BOOTSTRAP_TIMEOUT_SECONDS = 30.0


class Tier3TerminalKind(str, Enum):
    PASS = "pass"
    CANDIDATE_FAIL = "candidate_fail"
    CENSORED = "censored"
    FACILITY_FAIL = "facility_fail"
    CANCELLED = "cancelled"


class ValidationStage(str, Enum):
    FAST_PROBE = "fast_probe"
    CANONICAL_DEEP = "canonical_deep"


class Tier3ValidationPurpose(str, Enum):
    AUTO_ADMISSION = "auto_admission"
    USER_POOL_HEALTH = "user_pool_health"


@dataclass(frozen=True, slots=True)
class Tier3AdmissionAuthorityDecision:
    """Commit-time authority for one validated Tier3 candidate."""

    allowed: bool
    reason: str = ""


class Tier3DeepRetentionDecision(str, Enum):
    RETAINED = "retained"
    OVERFLOW_CENSORED = "overflow_censored"
    FAIR_REPLACED = "fair_replaced"


class EndpointAttemptKind(str, Enum):
    PASS = "pass"
    CANDIDATE_FAIL = "candidate_fail"
    TRANSIENT = "transient"
    RIGHT_CENSORED = "right_censored"
    FACILITY_FAIL = "facility_fail"
    CANCELLED = "cancelled"


class Tier3WorkerPhase(str, Enum):
    WAITING_START = "waiting_start"
    STARTING = "starting"
    IDLE = "idle"
    RESERVED = "reserved"
    VALIDATING = "validating"
    CANCELLING = "cancelling"
    RETIRING = "retiring"
    QUARANTINED = "quarantined"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class Tier3ValidatorCapacityLease:
    worker_id: int
    worker_generation: int
    reservation_id: int


@dataclass(frozen=True, slots=True)
class ValidateEndpointCmd:
    refresh_epoch: str
    candidate_digest: str
    config_digest: str
    lease_id: str
    sequence: int
    node_json: str
    source_root: str
    source_family: str
    transaction_id: str = ""
    slice_id: int = 1
    stage: ValidationStage = ValidationStage.FAST_PROBE
    purpose: Tier3ValidationPurpose = Tier3ValidationPurpose.AUTO_ADMISSION
    report_json: str = "{}"
    worker_id: int = 0
    worker_generation: int = 0
    capacity_reservation_id: int = 0
    capacity_epoch_id: int = 0
    facility_budget_seconds: float = 2.0
    endpoint: str = ""
    probe_budget_seconds: float = 3.0
    candidate_hard_cap_seconds: float = 5.75
    attempt_facts: tuple["ProbeAttemptFact", ...] = ()
    probe_policy_revision: int = 1
    portfolio_digest: str = ""
    endpoint_selection_reason: str = ""
    endpoint_coverage_epoch: int = 0
    endpoint_canonical_position: int = -1
    submitted_monotonic_ns: int = 0
    dispatch_monotonic_ns: int = field(default_factory=time.monotonic_ns)
    supervisor_assigned_monotonic_ns: int = 0
    supervisor_sent_monotonic_ns: int = 0


@dataclass(frozen=True, slots=True)
class CancelCmd:
    refresh_epoch: str
    lease_id: str
    worker_id: int
    worker_generation: int
    sequence: int
    reason: str


@dataclass(frozen=True, slots=True)
class ProbeAttemptFact:
    endpoint: str
    elapsed_ms: int
    full_budget: bool
    completed: bool
    entitlement_exhausted: bool = False
    status_code: int | None = None
    outcome: str = ""
    failure_domain: str = ""
    detail: str = ""
    attempt_id: str = ""
    generation_id: str = ""
    candidate_fingerprint: str = ""
    session_lease_id: str = ""
    stage: str = ValidationStage.CANONICAL_DEEP.value
    transport_started_monotonic_ns: int = 0
    granted_budget_ms: int = 0
    lifecycle: str = "completed"


@dataclass(frozen=True, slots=True)
class EndpointAttemptResult:
    refresh_epoch: str
    transaction_id: str
    candidate_digest: str
    config_digest: str
    lease_id: str
    slice_id: int
    sequence: int
    stage: ValidationStage
    worker_id: int
    worker_generation: int
    node_json: str
    purpose: Tier3ValidationPurpose = Tier3ValidationPurpose.AUTO_ADMISSION
    kind: EndpointAttemptKind = EndpointAttemptKind.RIGHT_CENSORED
    report_json: str = "{}"
    reason: str = ""
    latency_ms: float | None = None
    slow: bool = False
    probe_url: str = ""
    attempt_facts: tuple[ProbeAttemptFact, ...] = ()
    facility_elapsed_ms: int = 0
    portfolio_elapsed_ms: int = 0
    total_elapsed_ms: int = 0
    clean_confirmed: bool = False
    helper_pid: int = 0
    mihomo_pid: int = 0
    probe_policy_revision: int = 1
    portfolio_digest: str = ""
    submitted_monotonic_ns: int = 0
    dispatch_monotonic_ns: int = 0
    supervisor_assigned_monotonic_ns: int = 0
    supervisor_sent_monotonic_ns: int = 0
    helper_started_monotonic_ns: int = 0
    portfolio_started_monotonic_ns: int = 0
    yield_created_monotonic_ns: int = field(default_factory=time.monotonic_ns)
    monotonic_ns: int = field(default_factory=time.monotonic_ns)


@dataclass(frozen=True, slots=True)
class ValidationTerminal:
    refresh_epoch: str
    candidate_digest: str
    config_digest: str
    lease_id: str
    sequence: int
    worker_id: int
    worker_generation: int
    kind: Tier3TerminalKind
    node_json: str
    transaction_id: str = ""
    slice_id: int = 1
    stage: ValidationStage = ValidationStage.CANONICAL_DEEP
    purpose: Tier3ValidationPurpose = Tier3ValidationPurpose.AUTO_ADMISSION
    report_json: str = "{}"
    reason: str = ""
    latency_ms: float | None = None
    slow: bool = False
    probe_url: str = ""
    attempt_facts: tuple[ProbeAttemptFact, ...] = ()
    facility_elapsed_ms: int = 0
    portfolio_elapsed_ms: int = 0
    total_elapsed_ms: int = 0
    clean_confirmed: bool = False
    helper_pid: int = 0
    mihomo_pid: int = 0
    probe_policy_revision: int = 1
    portfolio_digest: str = ""
    submitted_monotonic_ns: int = 0
    dispatch_monotonic_ns: int = 0
    supervisor_assigned_monotonic_ns: int = 0
    supervisor_sent_monotonic_ns: int = 0
    helper_started_monotonic_ns: int = 0
    portfolio_started_monotonic_ns: int = 0
    terminal_created_monotonic_ns: int = field(default_factory=time.monotonic_ns)
    monotonic_ns: int = field(default_factory=time.monotonic_ns)


@dataclass(frozen=True, slots=True)
class GenerationGone:
    worker_id: int
    worker_generation: int
    helper_pid: int
    mihomo_pid: int
    reason: str
    job_empty: bool
    ports_released: bool


@dataclass(frozen=True, slots=True)
class AdmissionIntent:
    refresh_epoch: str
    candidate_digest: str
    config_digest: str
    lease_id: str
    sequence: int
    worker_id: int
    worker_generation: int
    node_json: str
    report_json: str
    capacity_epoch_id: int = 0
    latency_ms: float | None = None
    slow: bool = False
    probe_url: str = ""
    reason: str = ""
    attempt_facts: tuple[ProbeAttemptFact, ...] = ()
    probe_policy_revision: int = 1
    portfolio_digest: str = ""
    facility_elapsed_ms: int = 0
    portfolio_elapsed_ms: int = 0
    total_elapsed_ms: int = 0
    clean_confirmed: bool = False
    submitted_monotonic_ns: int = 0
    dispatch_monotonic_ns: int = 0
    supervisor_assigned_monotonic_ns: int = 0
    supervisor_sent_monotonic_ns: int = 0
    helper_started_monotonic_ns: int = 0
    portfolio_started_monotonic_ns: int = 0
    terminal_created_monotonic_ns: int = 0
    transaction_id: str = ""
    slice_id: int = 1
    stage: ValidationStage = ValidationStage.CANONICAL_DEEP
    purpose: Tier3ValidationPurpose = Tier3ValidationPurpose.AUTO_ADMISSION


@dataclass(frozen=True, slots=True)
class AdmissionCommitted:
    refresh_epoch: str
    candidate_digest: str
    lease_id: str
    sequence: int
    pool_revision: int
    net_new_pool_growth: bool
    duplicate_identity: bool = False


@dataclass(frozen=True, slots=True)
class AdmissionRejected:
    refresh_epoch: str
    candidate_digest: str
    lease_id: str
    sequence: int
    reason: str


@dataclass(frozen=True, slots=True)
class CandidateEnvelope:
    refresh_epoch: str
    candidate_digest: str
    config_digest: str
    node_json: str
    report_json: str
    source_root: str
    source_family: str
    source_coarse: str = "unknown"
    dispatch_selection_reason: str = ""
    dispatch_evidence_origin: str = "cold_unknown"
    probe_urls: tuple[str, ...] = ()
    capacity_epoch_id: int = 0
    purpose: Tier3ValidationPurpose = Tier3ValidationPurpose.AUTO_ADMISSION
    submitted_monotonic_ns: int = field(default_factory=time.monotonic_ns)


def canonical_json(value: Mapping[str, Any] | list[Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def digest_json(value: Mapping[str, Any] | list[Any]) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def message_bytes(kind: str, payload: Mapping[str, Any]) -> bytes:
    return canonical_json(
        {
            "schema": PROTOCOL_SCHEMA_VERSION,
            "kind": str(kind),
            "payload": dict(payload),
        }
    ).encode("utf-8")


def parse_message(raw: bytes) -> tuple[str, dict[str, Any]]:
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("validator IPC message must be an object")
    if int(value.get("schema", 0) or 0) != PROTOCOL_SCHEMA_VERSION:
        raise ValueError("validator IPC schema mismatch")
    kind = str(value.get("kind") or "")
    payload = value.get("payload")
    if not kind or not isinstance(payload, dict):
        raise ValueError("validator IPC message is incomplete")
    return kind, payload


def dataclass_payload(value: Any) -> dict[str, Any]:
    payload = asdict(value)
    for key, item in list(payload.items()):
        if isinstance(item, Enum):
            payload[key] = item.value
    return payload


def endpoint_result_wire_payload(result: EndpointAttemptResult) -> dict[str, Any]:
    """Return evidence without echoing the parent's immutable lease payload."""

    payload = dataclass_payload(result)
    payload["node_json"] = ""
    payload["report_json"] = "{}"
    return payload


def validate_endpoint_cmd_from_payload(payload: Mapping[str, Any]) -> ValidateEndpointCmd:
    data = dict(payload)
    stage_value = str(data.get("stage") or "")
    if not stage_value:
        raise ValueError("validator command stage is required")
    data["stage"] = ValidationStage(stage_value)
    data["purpose"] = Tier3ValidationPurpose(
        str(data.get("purpose") or Tier3ValidationPurpose.AUTO_ADMISSION.value)
    )
    data["attempt_facts"] = tuple(
        ProbeAttemptFact(**dict(item))
        for item in data.get("attempt_facts", ())
        if isinstance(item, dict)
    )
    endpoint = str(data.get("endpoint") or "")
    if not endpoint:
        raise ValueError("validator command endpoint is required")
    data["endpoint"] = endpoint
    return ValidateEndpointCmd(**data)


def cancel_cmd_from_payload(payload: Mapping[str, Any]) -> CancelCmd:
    return CancelCmd(**dict(payload))


def terminal_from_payload(payload: Mapping[str, Any]) -> ValidationTerminal:
    data = dict(payload)
    data["kind"] = Tier3TerminalKind(str(data.get("kind") or "censored"))
    stage_value = str(data.get("stage") or "")
    if not stage_value:
        raise ValueError("validator terminal stage is required")
    data["stage"] = ValidationStage(stage_value)
    data["purpose"] = Tier3ValidationPurpose(
        str(data.get("purpose") or Tier3ValidationPurpose.AUTO_ADMISSION.value)
    )
    data["attempt_facts"] = tuple(
        ProbeAttemptFact(**dict(item))
        for item in data.get("attempt_facts", ())
        if isinstance(item, dict)
    )
    return ValidationTerminal(**data)


def endpoint_attempt_result_from_payload(
    payload: Mapping[str, Any],
) -> EndpointAttemptResult:
    data = dict(payload)
    stage_value = str(data.get("stage") or "")
    if not stage_value:
        raise ValueError("validator endpoint-result stage is required")
    data["stage"] = ValidationStage(stage_value)
    data["purpose"] = Tier3ValidationPurpose(
        str(data.get("purpose") or Tier3ValidationPurpose.AUTO_ADMISSION.value)
    )
    data["kind"] = EndpointAttemptKind(
        str(data.get("kind") or EndpointAttemptKind.RIGHT_CENSORED.value)
    )
    data["attempt_facts"] = tuple(
        ProbeAttemptFact(**dict(item))
        for item in data.get("attempt_facts", ())
        if isinstance(item, dict)
    )
    return EndpointAttemptResult(**data)
