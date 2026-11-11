from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from clash_formatter import node_to_clash_proxy
from models import Node, NodeStatus, utc_now_iso
from runtime_network_context import RuntimeNetworkContextObserver
from tier3_defaults import (
    tier3_process_worker_ceiling,
    tier3_validation_slot_ceiling,
    tier3_validation_slots_per_process,
)
from tier3_endpoint_controller import Tier3EndpointController
from tier3_expansion_actor import ExpansionActorSnapshot, Tier3ExpansionActor
from tier3_admission_commit_queue import AdmissionResult, Tier3AdmissionCommitQueue
from tier3_validation_feedback import (
    Tier3CandidateDispatchContext,
    Tier3ValidationFeedbackEvent,
    Tier3ValidationFeedbackKind,
)
from tier3_validation_protocol import (
    AdmissionCommitted,
    AdmissionIntent,
    AdmissionRejected,
    CandidateEnvelope,
    EndpointAttemptKind,
    EndpointAttemptResult,
    GenerationGone,
    ProbeAttemptFact,
    Tier3DeepRetentionDecision,
    Tier3TerminalKind,
    Tier3ValidationPurpose,
    TIER3_VALIDATION_CONTRACT_HASH,
    ValidationStage,
    ValidationTerminal,
    canonical_json,
    confirm_https_budget_seconds,
    digest_json,
)
from tier3_validator_supervisor import (
    SupervisorSnapshot,
    Tier3ValidatorSupervisor,
    Tier3ValidatorWatchdog,
)


DEFAULT_PROCESS_PROBE_URLS = (
    "https://cp.cloudflare.com",
    "https://www.gstatic.com/generate_204",
    "http://connectivitycheck.gstatic.com/generate_204",
    "http://www.gstatic.com/generate_204",
)
# Admission gate: a node is admitted only if it can load Google over HTTPS
# (``testing.mihomo_helper.confirm_urls``; ``[]`` disables it).
DEFAULT_PROCESS_CONFIRM_URLS = ("https://www.google.com/generate_204",)


def _process_confirm_urls(helper: dict[str, Any]) -> list[str]:
    raw = helper.get("confirm_urls")
    if raw is None:
        raw = DEFAULT_PROCESS_CONFIRM_URLS
    values = raw if isinstance(raw, (list, tuple)) else [raw]
    urls: list[str] = []
    for item in values:
        url = str(item or "").strip()
        parsed = urlsplit(url)
        # A malformed URL would fail every passing candidate as a facility
        # error; drop it rather than silently stop all admission.
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        if url not in urls:
            urls.append(url)
    return urls


def tier3_candidate_config_payload(node: Node) -> dict[str, Any]:
    """Return the exact rendered proxy semantics without mutable ownership."""

    try:
        rendered = node_to_clash_proxy(node, seen_names=set())
    except Exception:
        rendered = None
    if isinstance(rendered, dict) and rendered:
        proxy = dict(rendered)
        proxy.pop("name", None)
        return {"proxy": proxy}
    return {
        "unsupported": True,
        "protocol": node.protocol,
        "address": node.address,
        "port": int(node.port),
        "uuid": node.uuid,
        "password": node.password,
        "method": node.method,
        "security": node.security,
        "network": node.network,
        "host": node.host,
        "path": node.path,
        "sni": node.sni,
        "plugin": node.plugin,
        "encryption": node.encryption,
        "tls": node.tls,
    }


def tier3_candidate_config_digest(node: Node) -> str:
    return digest_json(tier3_candidate_config_payload(node))


def process_validator_settings(testing_config: dict[str, Any]) -> dict[str, Any]:
    helper = testing_config.get("mihomo_helper")
    helper = dict(helper) if isinstance(helper, dict) else {}
    raw_urls = (
        helper.get("probe_urls")
        or helper.get("probe_url")
        or testing_config.get("probe_urls")
        or DEFAULT_PROCESS_PROBE_URLS
    )
    urls = (
        [str(item).strip() for item in raw_urls if str(item).strip()]
        if isinstance(raw_urls, (list, tuple, set))
        else [str(raw_urls).strip()]
    )
    for fallback in DEFAULT_PROCESS_PROBE_URLS:
        if fallback not in urls:
            urls.append(fallback)
    raw_statuses = helper.get("expected_statuses") or helper.get("expected_status") or [204, 200]
    if not isinstance(raw_statuses, (list, tuple, set)):
        raw_statuses = [raw_statuses]
    statuses: set[int] = set()
    for value in raw_statuses:
        try:
            statuses.add(int(value))
        except (TypeError, ValueError):
            continue
    return {
        "binary_path": str(
            helper.get("binary_path")
            or testing_config.get("mihomo_binary_path")
            or testing_config.get("_mihomo_binary_path")
            or ""
        ),
        "probe_urls": urls,
        "confirm_urls": _process_confirm_urls(helper),
        "expected_statuses": statuses or {200, 204},
        "probe_timeout_seconds": max(
            0.25,
            float(helper.get("probe_timeout") or testing_config.get("request_timeout") or 5.0),
        ),
        "temp_parent": str(testing_config.get("mihomo_validation_temp_dir") or ""),
        "ipv6": bool(testing_config.get("ipv6", False)),
    }


@dataclass(frozen=True, slots=True)
class ValidationDomainSnapshot:
    actor: ExpansionActorSnapshot
    supervisor: SupervisorSnapshot
    operational_supervisor: SupervisorSnapshot
    peak_helper_count: int
    commit_pending: int
    result_pending: int
    desired_workers: int
    pressure: float
    deficit: int
    stop_status: str
    endpoint_health: dict[str, dict[str, float | int | str | bool]]
    terminal_aggregates: dict[str, dict[str, int]]
    terminal_samples: tuple[dict[str, Any], ...]
    stop_stages: tuple[str, ...] = ()
    ingress_depth: int = 0
    ingress_transfer_count: int = 0
    transaction_window_limit: int = 0
    transaction_window_used: int = 0
    accepted_transactions: int = 0
    actor_submitted_transactions: int = 0
    ingress_cancelled_transactions: int = 0
    transaction_conservation_ok: bool = True
    runtime_network_context: dict[str, object] | None = None
    runtime_network_context_history: tuple[dict[str, object], ...] = ()
    dispatch_samples: tuple[dict[str, Any], ...] = ()
    attempt_samples: tuple[dict[str, Any], ...] = ()
    controller_dispatch_consistency_ok: bool = True
    actor_owner_limit: int = 0
    actor_owner_used: int = 0
    ready_work_limit: int = 0
    ready_work_used: int = 0
    dispatch_context_count: int = 0
    fresh_transaction_pending_count: int = 0
    duplicate_submission_count: int = 0
    missing_dispatch_context_count: int = 0
    duplicate_feedback_count: int = 0
    result_reporter_active: bool = False
    observer_pending: int = 0
    result_queue_high_water: int = 0
    observer_queue_high_water: int = 0
    actor_result_max_ms: float = 0.0
    observer_callback_max_ms: float = 0.0


@dataclass(slots=True)
class _DispatchContextState:
    context: Tier3CandidateDispatchContext
    attempt_seen: bool = False
    continuation_seen: bool = False
    terminal_seen: bool = False
    admission_seen: bool = False


def validation_fresh_supply_depth(snapshot: ValidationDomainSnapshot) -> int:
    actor = snapshot.actor
    published = (
        int(snapshot.ingress_depth)
        + int(getattr(snapshot, "ingress_transfer_count", 0))
        + sum(
            int(getattr(actor, name, 0))
            for name in (
                "candidate_inbox_depth", "ready_fast", "parked_fast", "active_fast"
            )
        )
    )
    # Actor inbox removal precedes publication of ready/active work. Context
    # ownership bridges that gap, but a retained DEEP transaction is not FAST.
    return max(0, published, int(getattr(snapshot, "fresh_transaction_pending_count", 0)))


@dataclass(frozen=True, slots=True)
class ValidationFastIngressWindow:
    """Bounded worker-ready credit over an unbounded durable ledger."""

    limit: int
    used: int
    credit: int
    tcp_pending: int
    actor_fast_pending: int
    actor_deep_pending: int
    actor_transaction_pending: int
    actor_ingress_pending: int
    saturated: bool


class _TransactionIngress:
    """Durable root/family FIFO before actor ownership begins."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._roots: OrderedDict[
            str, OrderedDict[str, deque[CandidateEnvelope]]
        ] = OrderedDict()
        self._root_cursor: deque[str] = deque()
        self._family_cursor: dict[str, deque[str]] = {}
        self._depth = 0

    def push(self, envelope: CandidateEnvelope, *, front: bool = False) -> None:
        root = envelope.source_root or "unknown"
        family = envelope.source_family or "unknown"
        with self._lock:
            families = self._roots.get(root)
            if families is None:
                families = OrderedDict()
                self._roots[root] = families
                self._root_cursor.append(root)
            candidates = families.get(family)
            if candidates is None:
                candidates = deque()
                families[family] = candidates
                self._family_cursor.setdefault(root, deque()).append(family)
            if front:
                candidates.appendleft(envelope)
            else:
                candidates.append(envelope)
            self._depth += 1

    def pop(self) -> CandidateEnvelope | None:
        with self._lock:
            root_attempts = len(self._root_cursor)
            while root_attempts > 0 and self._root_cursor:
                root_attempts -= 1
                root = self._root_cursor.popleft()
                families = self._roots.get(root)
                family_cursor = self._family_cursor.get(root)
                if not families or not family_cursor:
                    self._roots.pop(root, None)
                    self._family_cursor.pop(root, None)
                    continue
                family_attempts = len(family_cursor)
                while family_attempts > 0 and family_cursor:
                    family_attempts -= 1
                    family = family_cursor.popleft()
                    candidates = families.get(family)
                    if not candidates:
                        families.pop(family, None)
                        continue
                    envelope = candidates.popleft()
                    self._depth = max(0, self._depth - 1)
                    if candidates:
                        family_cursor.append(family)
                    else:
                        families.pop(family, None)
                    if families:
                        self._root_cursor.append(root)
                    else:
                        self._roots.pop(root, None)
                        self._family_cursor.pop(root, None)
                    return envelope
                if families:
                    self._root_cursor.append(root)
                else:
                    self._roots.pop(root, None)
                    self._family_cursor.pop(root, None)
            return None

    def drain(self) -> list[CandidateEnvelope]:
        drained: list[CandidateEnvelope] = []
        while True:
            envelope = self.pop()
            if envelope is None:
                return drained
            drained.append(envelope)

    def depth(self) -> int:
        with self._lock:
            return self._depth


def calculate_validation_fast_ingress_window(
    *,
    active_workers: int,
    pressure: float,
    tcp_selected: int,
    tcp_completed: int,
    validation_accepted: int,
    actor_submitted: int,
    actor_fast_queued: int,
    actor_fast_active: int,
    actor_deep_queued: int = 0,
    actor_deep_active: int = 0,
    actor_inbox_depth: int = 0,
) -> ValidationFastIngressWindow:
    """Describe ready reservations without throttling TCP or durable work."""

    workers = max(1, int(active_workers))
    clamped_pressure = max(0.0, min(1.0, float(pressure)))
    limit = max(
        workers,
        int(round(workers + workers * clamped_pressure)),
    )
    tcp_pending = max(0, int(tcp_selected) - int(tcp_completed))
    actor_ingress_pending = max(
        max(0, int(validation_accepted) - int(actor_submitted)),
        max(0, int(actor_inbox_depth)),
    )
    actor_fast_pending = max(0, int(actor_fast_queued)) + max(
        0, int(actor_fast_active)
    )
    actor_deep_pending = max(0, int(actor_deep_queued)) + max(
        0, int(actor_deep_active)
    )
    actor_transaction_pending = (
        actor_fast_pending + actor_deep_pending + actor_ingress_pending
    )
    active = max(0, int(actor_fast_active)) + max(0, int(actor_deep_active))
    durable_waiting = max(
        0,
        int(actor_fast_queued)
        + int(actor_deep_queued)
        + int(actor_ingress_pending),
    )
    reserved = min(max(0, limit - active), durable_waiting)
    used = min(limit, active + reserved)
    credit = max(0, limit - used)
    return ValidationFastIngressWindow(
        limit=limit,
        used=used,
        credit=credit,
        tcp_pending=tcp_pending,
        actor_fast_pending=actor_fast_pending,
        actor_deep_pending=actor_deep_pending,
        actor_transaction_pending=actor_transaction_pending,
        actor_ingress_pending=actor_ingress_pending,
        saturated=credit <= 0,
    )


def _safe_json_dict(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _attempt_facts_payload(facts: tuple[ProbeAttemptFact, ...]) -> list[dict[str, Any]]:
    return [
        {
            "endpoint": fact.endpoint,
            "elapsed_ms": fact.elapsed_ms,
            "full_budget": fact.full_budget,
            "completed": fact.completed,
            "entitlement_exhausted": fact.entitlement_exhausted,
            "status_code": fact.status_code,
            "outcome": fact.outcome,
            "failure_domain": fact.failure_domain,
            "detail": fact.detail,
            "attempt_id": fact.attempt_id,
            "generation_id": fact.generation_id,
            "candidate_fingerprint": fact.candidate_fingerprint,
            "session_lease_id": fact.session_lease_id,
            "stage": fact.stage,
            "transport_started_monotonic_ns": fact.transport_started_monotonic_ns,
            "granted_budget_ms": fact.granted_budget_ms,
            "lifecycle": fact.lifecycle,
        }
        for fact in facts
    ]


def _ledger_exhausted_without_response(terminal: ValidationTerminal) -> bool:
    """True when every attempt in the ledger was right-censored with no response.

    Only the whole-ledger terminal qualifies (``endpoint_ledger_transient_or_censored``);
    a candidate censored for deep-retention overflow or cut by a wave deadline
    still owes an attempt.  A single TRANSIENT attempt (any HTTP status) means
    the node did answer and keeps the deferred classification.
    """

    if terminal.kind is not Tier3TerminalKind.CENSORED:
        return False
    if str(terminal.reason or "") != "endpoint_ledger_transient_or_censored":
        return False
    facts = tuple(terminal.attempt_facts or ())
    if not facts:
        return False
    exhausted = False
    for fact in facts:
        if fact.status_code is not None:
            return False
        if str(fact.outcome or "") != "right_censored":
            return False
        if fact.entitlement_exhausted:
            exhausted = True
    return exhausted


def _attempt_is_right_censored(result: EndpointAttemptResult) -> bool:
    """True when the attempt produced no answer we can hold against the node.

    TRANSIENT covers two different things.  A dial that never came back owes
    the candidate another attempt.  But a dial that failed at the far end is
    reported by the local mixed port as HTTP 5xx, and that fact is completed:
    it is the node answering "no".  Folding both into censored made
    CANDIDATE_FAIL unreachable for a dead proxy, so validation never wrote a
    negative record and every session re-tested nodes it had already
    disproved (2026-09-20: endpoint controller 38 completed FAST facts vs 4
    counted by the scheduler; candidate_fail 0 in all 16 buckets).
    """

    if result.kind is EndpointAttemptKind.RIGHT_CENSORED:
        return True
    if result.kind is not EndpointAttemptKind.TRANSIENT:
        return False
    return not any(fact.completed for fact in result.attempt_facts)


def node_from_terminal(terminal: ValidationTerminal) -> Node:
    node = Node.from_dict(_safe_json_dict(terminal.node_json))
    checked_at = utc_now_iso()
    node.last_checked_at = checked_at
    node.extra["validation_profile"] = "endpoint_process_helper"
    node.extra["validated_by"] = "tier3_validator_helper"
    node.extra["tier3_validation_contract_hash"] = TIER3_VALIDATION_CONTRACT_HASH
    node.extra["tier3_validator_refresh_epoch"] = terminal.refresh_epoch
    node.extra["tier3_validator_lease_id"] = terminal.lease_id
    node.extra["tier3_validator_worker_id"] = terminal.worker_id
    node.extra["tier3_validator_worker_generation"] = terminal.worker_generation
    node.extra["tier3_validator_transaction_id"] = terminal.transaction_id
    node.extra["tier3_validator_slice_id"] = terminal.slice_id
    node.extra["tier3_validator_stage"] = terminal.stage.value
    node.extra["tier3_validation_purpose"] = terminal.purpose.value
    node.extra["tier3_validation_lane"] = terminal.stage.value
    node.extra["tier3_candidate_digest"] = terminal.candidate_digest
    node.extra["tier3_config_digest"] = terminal.config_digest
    node.extra["tier3_probe_policy_revision"] = terminal.probe_policy_revision
    node.extra["tier3_probe_portfolio_digest"] = terminal.portfolio_digest
    node.extra["tier3_probe_attempt_facts"] = _attempt_facts_payload(terminal.attempt_facts)
    node.extra["validation_probe_urls_attempted"] = [
        fact.endpoint for fact in terminal.attempt_facts
    ]
    node.extra["tier3_probe_urls_completed_all"] = [
        fact.endpoint for fact in terminal.attempt_facts if fact.completed
    ]
    node.extra["tier3_probe_urls_censored"] = [
        fact.endpoint
        for fact in terminal.attempt_facts
        if not fact.completed or fact.outcome == "right_censored"
    ]
    node.extra["tier3_probe_urls_remaining"] = []
    node.extra["validation_probe_failures_by_url"] = {
        fact.endpoint: fact.detail or fact.outcome
        for fact in terminal.attempt_facts
        if fact.outcome != "pass"
    }
    node.extra["session_validator_facility_elapsed_ms"] = terminal.facility_elapsed_ms
    node.extra["real_probe_elapsed_ms"] = terminal.portfolio_elapsed_ms
    node.extra["tier3_full_validation_executor_elapsed_ms"] = terminal.total_elapsed_ms
    node.extra["tier3_validator_clean_confirmed"] = terminal.clean_confirmed
    node.extra["tier3_validator_submitted_monotonic_ns"] = (
        terminal.submitted_monotonic_ns
    )
    node.extra["tier3_validator_dispatch_monotonic_ns"] = (
        terminal.dispatch_monotonic_ns
    )
    node.extra["tier3_validator_supervisor_assigned_monotonic_ns"] = (
        terminal.supervisor_assigned_monotonic_ns
    )
    node.extra["tier3_validator_supervisor_sent_monotonic_ns"] = (
        terminal.supervisor_sent_monotonic_ns
    )
    node.extra["tier3_validator_helper_started_monotonic_ns"] = (
        terminal.helper_started_monotonic_ns
    )
    node.extra["tier3_validator_portfolio_started_monotonic_ns"] = (
        terminal.portfolio_started_monotonic_ns
    )
    if terminal.probe_url:
        node.extra["validation_probe_url"] = terminal.probe_url

    if terminal.kind is Tier3TerminalKind.PASS:
        latency = float(terminal.latency_ms or 0.0)
        node.latency_ms = latency
        node.status = (
            NodeStatus.SLOW
            if terminal.slow
            else NodeStatus.ALIVE
        )
        node.suspicious_reason = None
        for key in (
            "tier3_failure_stage",
            "tier3_failure_reason",
            "tier3_full_validation_deadline_hit",
        ):
            node.extra.pop(key, None)
        return node

    node.status = NodeStatus.SUSPICIOUS
    node.latency_ms = None
    reason = str(terminal.reason or terminal.kind.value)
    if terminal.kind is Tier3TerminalKind.CANDIDATE_FAIL:
        stage = "full_validation_candidate_failed"
        failure_reason = f"mihomo_validation_failed: {reason}"
    elif terminal.kind is Tier3TerminalKind.FACILITY_FAIL:
        stage = "validator_facility_failure"
        failure_reason = f"validator_facility_failure: {reason}"
    elif terminal.kind is Tier3TerminalKind.CANCELLED:
        stage = "validation_cancelled"
        failure_reason = f"validation_cancelled: {reason}"
    elif _ledger_exhausted_without_response(terminal):
        # The candidate accepted TCP, then answered nothing on any endpoint of
        # its FAST and canonical ledger while every attempt ran its full
        # entitlement.  That is a verdict about the node, not a scheduling
        # shortfall: record it as a real (short-cooldown) failure so the
        # negative cache learns it and the next session does not spend a
        # full ledger on it again.  Deep-retention censors and wave-deadline
        # cuts stay deferred because those candidates were never judged.
        stage = "full_validation_probe_deadline"
        total_ms = max(0, int(terminal.total_elapsed_ms or 0))
        failure_reason = (
            "mihomo_batch_validation_failed: full_validation_probe_deadline "
            f"exhausted {total_ms / 1000.0:.1f}s probe entitlement without a response"
        )
        node.extra["tier3_full_validation_deadline_hit"] = True
        node.extra["tier3_probe_entitlement_exhausted"] = True
    else:
        stage = "validation_deferred"
        failure_reason = f"validation_deferred: {reason}"
        node.extra["tier3_full_validation_deadline_hit"] = True
    node.suspicious_reason = failure_reason
    node.extra["tier3_failure_stage"] = stage
    node.extra["tier3_failure_reason"] = failure_reason
    return node


class Tier3ValidationDomain:
    """Process-isolated canonical validator execution domain for one expansion."""

    # Worker identities per helper process (batch backend); 1 is the
    # single-slot backend.  Class default so partially built doubles keep
    # the single-slot bounds.
    slots_per_process: int = 1

    def __init__(
        self,
        *,
        refresh_epoch: str,
        binary_path: str,
        probe_urls: list[str],
        expected_statuses: set[int],
        probe_timeout_seconds: float,
        slow_threshold_ms: int,
        commit_node: Callable[[Node, dict[str, Any], AdmissionIntent], AdmissionResult]
        | None,
        terminal_observer: Callable[[Node, ValidationTerminal], None] | None,
        attempt_observer: Callable[[Node, EndpointAttemptResult], None] | None = None,
        admission_observer: Callable[[AdmissionResult], None] | None = None,
        generation_observer: Callable[[GenerationGone], None] | None = None,
        feedback_observer: Callable[[Tier3ValidationFeedbackEvent], None] | None = None,
        logger: Any | None = None,
        min_workers: int = 4,
        max_workers: int = 16,
        startup_bulkhead: int = 8,
        mihomo_start_bulkhead: int = 8,
        temp_parent: str = "",
        ipv6: bool = False,
        helper_target: Callable[[Any], None] | None = None,
        slots_per_process: int = 1,
        confirm_urls: list[str] | tuple[str, ...] = (),
    ) -> None:
        self.refresh_epoch = str(refresh_epoch)
        self.slots_per_process = tier3_validation_slots_per_process(slots_per_process)
        # Mainstream HTTPS confirm (production passes the configured list;
        # the empty default keeps fixtures on the bare portfolio contract).
        self._confirm_urls = tuple(
            dict.fromkeys(str(item) for item in confirm_urls if str(item))
        )
        self._commit_node = commit_node
        self._attempt_observer = attempt_observer
        self._terminal_observer = terminal_observer
        self._admission_observer = admission_observer
        self._generation_observer = generation_observer
        self._feedback_observer = feedback_observer
        self._callbacks_lock = threading.Lock()
        self._callbacks_bound = bool(
            commit_node is not None and terminal_observer is not None
        )
        self._logger = logger
        # The supervisor is the sole owner of all helper Pipes.  Its sinks
        # therefore must remain O(1): manager feedback, diagnostics and actor
        # transitions run on the result reporter instead of the Pipe reader.
        # The producer rate is bounded by the helper envelope, so an unbounded
        # in-process queue is safer than falling back to synchronous business
        # callbacks when a bounded queue is briefly full.
        self._terminal_queue: queue.Queue[
            EndpointAttemptResult | GenerationGone | ValidationTerminal | None
        ] = queue.Queue()
        self._result_thread: threading.Thread | None = None
        self._result_sentinel_sent = False
        self._observer_queue: queue.Queue[
            tuple[str, object] | None
        ] = queue.Queue()
        self._observer_thread: threading.Thread | None = None
        self._observer_sentinel_sent = False
        self._result_metrics_lock = threading.Lock()
        self._result_queue_high_water = 0
        self._observer_queue_high_water = 0
        self._actor_result_max_ms = 0.0
        self._observer_callback_max_ms = 0.0
        self._resource_thread: threading.Thread | None = None
        self._resource_stop = threading.Event()
        self._pressure = 1.0
        self._deficit = 1
        self._desired_workers = max(1, int(min_workers))
        self._stop_status = "not_started"
        self._hard_stall_sealed = False
        self._stop_lock = threading.Lock()
        self._stop_owner_lock = threading.Lock()
        self._stop_owner = ""
        self._stopped = threading.Event()
        self._endpoint_controller = Tier3EndpointController(probe_urls)
        self._probe_urls = tuple(
            dict.fromkeys(str(item) for item in probe_urls if str(item))
        )
        self._diagnostics_lock = threading.Lock()
        self._terminal_aggregates: dict[str, dict[str, int]] = {}
        self._terminal_samples: deque[dict[str, Any]] = deque(maxlen=64)
        self._dispatch_samples: deque[dict[str, Any]] = deque(maxlen=64)
        self._attempt_samples: deque[dict[str, Any]] = deque(maxlen=64)
        self._peak_helper_count = 0
        self._last_operational_supervisor: SupervisorSnapshot | None = None
        self._stop_stages: list[str] = []
        self._ingress = _TransactionIngress()
        self._ingress_event = threading.Event()
        self._ingress_stop = threading.Event()
        self._ingress_accepting = threading.Event()
        self._ingress_accepting.set()
        self._ingress_thread: threading.Thread | None = None
        self._ingress_metrics_lock = threading.Lock()
        self._accepted_candidate_digests: set[str] = set()
        self._dispatch_contexts: dict[str, _DispatchContextState] = {}
        self._duplicate_submission_count = 0
        self._missing_dispatch_context_count = 0
        self._duplicate_feedback_count = 0
        self._accepted_transaction_count = 0
        self._actor_submitted_transaction_count = 0
        self._ingress_cancelled_transaction_count = 0
        self._ingress_transfer_count = 0
        self._transaction_window_limit = 0
        self._transaction_window_used = 0
        self._actor_owner_limit = min(
            256 * self.slots_per_process,
            max(64, 10 * self._desired_workers),
        )
        self._actor_owner_used = 0
        self._ready_work_limit = max(8, 2 * self._desired_workers)
        self._ready_work_used = 0
        self._resident_limit_override: int | None = None
        self._ready_limit_override: int | None = None
        self._dispatch_enabled = True
        self._network_context = RuntimeNetworkContextObserver(self.refresh_epoch)

        validation_temp_parent = str(temp_parent or "")
        if validation_temp_parent:
            Path(validation_temp_parent).mkdir(parents=True, exist_ok=True)

        self.committer = Tier3AdmissionCommitQueue(
            self._commit_intent,
            self._admission_result,
        )
        self.actor = Tier3ExpansionActor(
            self.refresh_epoch,
            dispatch_sink=self._dispatch,
            admission_sink=self.committer.put_nowait,
            terminal_sink=self._terminal_queue,  # type: ignore[arg-type]
            initial_worker_envelope=max(1, int(min_workers)),
            transaction_released_sink=self._ingress_event.set,
            continuation_sink=self._continuation_decision,
            fast_observation_sink=self._accepted_fast_observation,
            endpoint_controller=self._endpoint_controller,
            confirm_budget_seconds=confirm_https_budget_seconds(
                self._confirm_urls,
                probe_timeout_seconds,
            ),
        )
        self._actor_worker_envelope = max(1, int(min_workers))
        supervisor_kwargs: dict[str, Any] = {}
        if helper_target is not None:
            supervisor_kwargs["helper_target"] = helper_target
        supervisor_class: type[Tier3ValidatorSupervisor] = Tier3ValidatorSupervisor
        identity_ceiling = tier3_process_worker_ceiling()
        if self.slots_per_process > 1:
            # Batch backend: one helper process carries several worker
            # identities.  The process ceiling is unchanged; identities scale.
            from tier3_batch_validator_supervisor import Tier3BatchValidatorSupervisor

            supervisor_class = Tier3BatchValidatorSupervisor
            supervisor_kwargs["slots_per_process"] = self.slots_per_process
            identity_ceiling = tier3_validation_slot_ceiling(self.slots_per_process)
        self.supervisor = supervisor_class(
            binary_path=binary_path,
            probe_urls=probe_urls,
            expected_statuses=expected_statuses,
            probe_timeout_seconds=probe_timeout_seconds,
            slow_threshold_ms=slow_threshold_ms,
            capacity_sink=self.actor.capacity_available,
            attempt_sink=self._queue_attempt_received,
            generation_sink=self._queue_generation_gone,
            logger=logger,
            min_workers=max(1, int(min_workers)),
            max_workers=min(
                identity_ceiling,
                max(int(min_workers), int(max_workers)),
            ),
            startup_bulkhead=startup_bulkhead,
            mihomo_start_bulkhead=mihomo_start_bulkhead,
            temp_parent=validation_temp_parent,
            ipv6=ipv6,
            confirm_urls=self._confirm_urls,
            **supervisor_kwargs,
        )
        self.actor.bind_capacity_reservation_owner(
            self.supervisor.reserve_capacity,
            self.supervisor.release_capacity,
        )
        self.supervisor.stop_stage_observer = lambda stage: self._log_stop_stage(
            f"supervisor:{stage}"
        )
        self.watchdog = Tier3ValidatorWatchdog(
            self.supervisor,
            backlog_depth=self.actor.backlog_depth,
            logger=logger,
        )

    def bind_callbacks(
        self,
        *,
        commit_node: Callable[
            [Node, dict[str, Any], AdmissionIntent], AdmissionResult
        ],
        terminal_observer: Callable[[Node, ValidationTerminal], None],
        attempt_observer: Callable[[Node, EndpointAttemptResult], None] | None = None,
        admission_observer: Callable[[AdmissionResult], None] | None = None,
        generation_observer: Callable[[GenerationGone], None] | None = None,
        feedback_observer: Callable[[Tier3ValidationFeedbackEvent], None] | None = None,
    ) -> None:
        """Bind business callbacks after helper-only prewarm completes.

        Prewarm may create helper processes, but it must not accept a candidate
        before the current refresh has installed its admission and feedback
        authority.  Binding is therefore a one-way transition that is only
        legal before the full domain starts.
        """

        with self._stop_lock:
            if self._stop_claimed():
                raise RuntimeError("validation domain is stopping")
            if self._stop_status not in {"not_started", "prewarmed"}:
                raise RuntimeError("validation callbacks must bind before start")
            with self._callbacks_lock:
                if self._callbacks_bound:
                    raise RuntimeError("validation callbacks are already bound")
                self._commit_node = commit_node
                self._terminal_observer = terminal_observer
                self._attempt_observer = attempt_observer
                self._admission_observer = admission_observer
                self._generation_observer = generation_observer
                self._feedback_observer = feedback_observer
                self._callbacks_bound = True

    def prewarm(self) -> bool:
        """Start only the helper supervisor before business callbacks exist."""

        with self._stop_lock:
            if self._stop_claimed():
                return False
            if self._stop_status in {"starting", "running"}:
                return True
            if self._stop_status == "prewarmed":
                return True
            if self._stop_status != "not_started":
                return False
            self.supervisor.start()
            if self._stop_claimed():
                return False
            self._stop_status = "prewarmed"
            return True

    def _stop_claimed(self) -> bool:
        if self._stopped.is_set():
            return True
        with self._stop_owner_lock:
            return bool(self._stop_owner)

    def start(self) -> bool:
        # Serialize the complete component launch against stop().  In
        # particular, stop-before-start is terminal: a manager cancellation
        # that lands after registration must never be undone by a later
        # unconditional start.
        with self._stop_lock:
            if self._stop_claimed():
                return False
            if self._stop_status == "running":
                return True
            if self._stop_status not in {"not_started", "prewarmed"}:
                return False
            with self._callbacks_lock:
                if not self._callbacks_bound:
                    raise RuntimeError("validation callbacks are not bound")
            self._stop_status = "starting"
            self._network_context.start()
            if self._stop_claimed():
                return False
            self.committer.start()
            if self._stop_claimed():
                return False
            self.actor.start()
            if self._stop_claimed():
                return False
            self.actor.set_credit_limits(
                self._actor_owner_limit,
                self._ready_work_limit,
            )
            if self._stop_claimed():
                return False
            self._observer_thread = threading.Thread(
                target=self._observer_loop,
                name="tier3-validation-business-observer",
                daemon=False,
            )
            self._observer_sentinel_sent = False
            self._observer_thread.start()
            if self._stop_claimed():
                return False
            self._result_thread = threading.Thread(
                target=self._result_loop,
                name="tier3-validation-result-reporter",
                daemon=False,
            )
            self._result_sentinel_sent = False
            self._result_thread.start()
            if self._stop_claimed():
                return False
            self.supervisor.start()
            if self._stop_claimed():
                return False
            self.watchdog.start()
            if self._stop_claimed():
                return False
            self._ingress_thread = threading.Thread(
                target=self._ingress_loop,
                name="tier3-validation-transaction-ingress",
                daemon=False,
            )
            self._ingress_thread.start()
            self._ingress_event.set()
            if self._stop_claimed():
                return False
            self._resource_thread = threading.Thread(
                target=self._resource_loop,
                name="tier3-validator-resource-envelope",
                daemon=False,
            )
            self._resource_thread.start()
            if self._stop_claimed():
                return False
            self._stop_status = "running"
            return True

    @classmethod
    def candidate_digest_for_node(cls, node: Node) -> str:
        return tier3_candidate_config_digest(node)

    @classmethod
    def candidate_digest_for_submission(
        cls,
        node: Node,
        *,
        purpose: Tier3ValidationPurpose,
        transaction_nonce: str = "",
    ) -> str:
        identity_digest = cls.candidate_digest_for_node(node)
        if purpose is Tier3ValidationPurpose.AUTO_ADMISSION:
            return identity_digest
        return digest_json(
            {
                "identity_digest": identity_digest,
                "purpose": purpose.value,
                "transaction_nonce": str(transaction_nonce or "health-check"),
            }
        )

    def submit(
        self,
        node: Node,
        report: dict[str, Any],
        *,
        capacity_epoch_id: int = 0,
        dispatch_context: Tier3CandidateDispatchContext | None = None,
        purpose: Tier3ValidationPurpose = Tier3ValidationPurpose.AUTO_ADMISSION,
        transaction_nonce: str = "",
    ) -> bool:
        with self._callbacks_lock:
            if not self._callbacks_bound:
                return False
        node_payload = node.to_dict()
        identity_payload = tier3_candidate_config_payload(node)
        candidate_digest = self.candidate_digest_for_submission(
            node,
            purpose=purpose,
            transaction_nonce=transaction_nonce,
        )
        config_digest = hashlib.sha256(
            canonical_json(identity_payload).encode("utf-8")
        ).hexdigest()
        source_root = (
            "__tier3_user_pool_health__"
            if purpose is Tier3ValidationPurpose.USER_POOL_HEALTH
            else dispatch_context.ticket.source_root
            if dispatch_context is not None
            else str(
                node.extra.get("tier3_source_root_id")
                or node.extra.get("tier3_source_root_key")
                or node.source
                or "unknown"
            )
        )
        # Preserve immutable TCP handoff attribution through validation fairness.
        # Aggregate source-unit labels describe provenance, not protocol lanes.
        source_family = (
            dispatch_context.ticket.family
            if dispatch_context is not None
            else str(node.protocol or "unknown")
        )
        source_coarse = (
            dispatch_context.ticket.coarse_key
            if dispatch_context is not None
            else str(
                node.extra.get("tier3_source_coarse_key")
                or node.extra.get("tier3_source_arm")
                or node.extra.get("tier3_source_unit_id")
                or node.protocol
                or "unknown"
            )
        )
        compact_report = {
            key: report[key]
            for key in (
                "collection_source_count",
                "source_count",
                "configured_source_count",
                "fetched_sources",
                "failed_sources",
                "node_count",
                "discovered_source_units",
                "fetched_source_units",
                "failed_source_units",
                "pending_source_units",
                "retry_due_source_units",
            )
            if key in report
        }
        envelope = CandidateEnvelope(
            refresh_epoch=self.refresh_epoch,
            candidate_digest=candidate_digest,
            config_digest=config_digest,
            node_json=canonical_json(node_payload),
            report_json=canonical_json(compact_report),
            source_root=source_root,
            source_family=source_family,
            source_coarse=source_coarse,
            dispatch_selection_reason=(
                str(dispatch_context.ticket.selection_reason)
                if dispatch_context is not None
                else ""
            ),
            dispatch_evidence_origin=(
                str(dispatch_context.ticket.evidence_origin)
                if dispatch_context is not None
                else "cold_unknown"
            ),
            probe_urls=self._probe_urls,
            capacity_epoch_id=int(capacity_epoch_id),
            purpose=purpose,
        )
        with self._ingress_metrics_lock:
            if not self._ingress_accepting.is_set():
                return False
            if candidate_digest in self._accepted_candidate_digests:
                self._duplicate_submission_count += 1
                return False
            self._accepted_candidate_digests.add(candidate_digest)
            if dispatch_context is not None:
                self._dispatch_contexts[candidate_digest] = _DispatchContextState(
                    context=dispatch_context,
                )
            self._accepted_transaction_count += 1
            self._ingress.push(envelope)
        self._ingress_event.set()
        return True

    def dispatch_context_for_attempt(
        self,
        candidate_digest: str,
    ) -> Tier3CandidateDispatchContext | None:
        with self._ingress_metrics_lock:
            state = self._dispatch_contexts.get(str(candidate_digest))
            if state is None:
                self._missing_dispatch_context_count += 1
                return None
            if state.attempt_seen:
                self._duplicate_feedback_count += 1
                return None
            state.attempt_seen = True
            return state.context

    def dispatch_context_for_terminal(
        self,
        candidate_digest: str,
        *,
        passed: bool,
    ) -> Tier3CandidateDispatchContext | None:
        digest = str(candidate_digest)
        with self._ingress_metrics_lock:
            state = self._dispatch_contexts.get(digest)
            if state is None:
                self._missing_dispatch_context_count += 1
                return None
            if state.terminal_seen:
                self._duplicate_feedback_count += 1
                return None
            state.terminal_seen = True
            context = state.context
            if not passed or state.admission_seen:
                self._dispatch_contexts.pop(digest, None)
            return context

    def dispatch_context_for_continuation(
        self,
        candidate_digest: str,
    ) -> Tier3CandidateDispatchContext | None:
        with self._ingress_metrics_lock:
            state = self._dispatch_contexts.get(str(candidate_digest))
            if state is None:
                self._missing_dispatch_context_count += 1
                return None
            if state.continuation_seen:
                self._duplicate_feedback_count += 1
                return None
            state.continuation_seen = True
            return state.context

    def dispatch_context_for_admission(
        self,
        candidate_digest: str,
    ) -> Tier3CandidateDispatchContext | None:
        digest = str(candidate_digest)
        with self._ingress_metrics_lock:
            state = self._dispatch_contexts.get(digest)
            if state is None:
                self._missing_dispatch_context_count += 1
                return None
            if state.admission_seen:
                self._duplicate_feedback_count += 1
                return None
            state.admission_seen = True
            context = state.context
            if state.terminal_seen:
                self._dispatch_contexts.pop(digest, None)
            return context

    def dispatch_context_count(self) -> int:
        with self._ingress_metrics_lock:
            return len(self._dispatch_contexts)

    def release_dispatch_contexts(self) -> int:
        with self._ingress_metrics_lock:
            released = len(self._dispatch_contexts)
            self._dispatch_contexts.clear()
            return released

    def set_pressure(self, pressure: float, deficit: int) -> None:
        """Compatibility-only telemetry update; manager owns worker count."""
        self._pressure = max(0.0, min(1.0, float(pressure)))
        self._deficit = max(0, int(deficit))
        self._ingress_event.set()

    def set_shell_prewarm_workers(self, count: int) -> None:
        """Prepare process shells without changing active validation capacity."""

        self.supervisor.set_shell_prewarm_workers(count)

    def set_envelope(
        self,
        desired_workers: int,
        pressure: float,
        deficit: int,
        *,
        resident_limit: int | None = None,
        ready_work_limit: int | None = None,
        dispatch_enabled: bool = True,
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
        """Apply the manager's authoritative process envelope."""
        self._pressure = max(0.0, min(1.0, float(pressure)))
        self._deficit = max(0, int(deficit))
        desired = max(
            self.supervisor.min_workers,
            min(self.supervisor.max_workers, int(desired_workers)),
        )
        self._desired_workers = desired
        self._resident_limit_override = (
            max(1, int(resident_limit))
            if resident_limit is not None
            else None
        )
        self._ready_limit_override = (
            max(1, int(ready_work_limit))
            if ready_work_limit is not None
            else None
        )
        self._dispatch_enabled = bool(dispatch_enabled)
        self.supervisor.set_desired_workers(desired)
        self._sync_active_worker_limits(self.supervisor.snapshot())
        self.actor.set_expansion_control(
            pressure=self._pressure,
            dispatch_enabled=self._dispatch_enabled,
            external_fresh_backlog=max(0, int(external_fresh_backlog)),
            expected_fast_supply=bool(expected_fast_supply),
            fast_supply_state=fast_supply_state,
            actionable_deep=max(0, int(actionable_deep)),
            normal_reservable_fast_count=max(
                0, int(normal_reservable_fast_count)
            ),
            reward_reservable_fast_count=max(
                0, int(reward_reservable_fast_count)
            ),
            escape_eligible_bucket_count=max(
                0, int(escape_eligible_bucket_count)
            ),
            tcp_handoff_lease_count=max(0, int(tcp_handoff_lease_count)),
            expected_responsive_arrivals=max(
                0, int(expected_responsive_arrivals)
            ),
            source_retry_due_count=max(0, int(source_retry_due_count)),
        )
        self._ingress_event.set()

    def _sync_active_worker_limits(
        self,
        supervisor_snapshot: SupervisorSnapshot,
        *,
        actor_used: int | None = None,
    ) -> tuple[int, int]:
        active_workers = max(0, int(supervisor_snapshot.active_workers))
        workers = max(
            1,
            active_workers,
            min(int(self._desired_workers), int(self.supervisor.max_workers)),
        )
        pressure = max(0.0, min(1.0, float(self._pressure)))
        owner_floor = 2 * workers
        owner_cap = min(256 * self.slots_per_process, max(owner_floor, 10 * workers))
        base_owner_limit = int(
            round(owner_floor + (owner_cap - owner_floor) * pressure)
        )
        base_ready_limit = int(round(workers + workers * pressure))
        requested_owner_limit = (
            base_owner_limit
            if self._resident_limit_override is None
            else self._resident_limit_override
        )
        ready_work_limit = (
            base_ready_limit
            if self._ready_limit_override is None
            else self._ready_limit_override
        )
        ready_work_limit = max(workers, int(ready_work_limit))
        owner_limit = max(
            ready_work_limit,
            int(requested_owner_limit),
            max(0, int(actor_used or 0)),
        )
        owner_limit = min(256 * self.slots_per_process, owner_limit)
        with self._ingress_metrics_lock:
            credit_limits_changed = (
                self._actor_owner_limit != owner_limit
                or self._ready_work_limit != ready_work_limit
            )
            worker_envelope_changed = self._actor_worker_envelope != workers
            self._actor_owner_limit = owner_limit
            self._ready_work_limit = ready_work_limit
            self._actor_worker_envelope = workers
        if worker_envelope_changed:
            self.actor.set_worker_envelope(workers)
        if credit_limits_changed:
            self.actor.set_credit_limits(owner_limit, ready_work_limit)
        if worker_envelope_changed or credit_limits_changed:
            self._ingress_event.set()
        return owner_limit, ready_work_limit

    def pending_work(self) -> int:
        with self._ingress_metrics_lock:
            ingress_work = (
                self._ingress.depth() + self._ingress_transfer_count
            )
            dispatch_context_work = len(self._dispatch_contexts)
        published_work = (
            ingress_work
            + self.actor.pending_work()
            + self.supervisor.pending_count()
            + self.committer.pending_count()
        )
        return max(published_work, dispatch_context_work)

    def wait_idle(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        while time.monotonic() < deadline:
            actor = self.actor.snapshot()
            supervisor = self.supervisor.snapshot()
            with self._ingress_metrics_lock:
                ingress_depth = self._ingress.depth()
                ingress_transfer_count = self._ingress_transfer_count
                dispatch_context_count = len(self._dispatch_contexts)
            if (
                ingress_depth == 0
                and ingress_transfer_count == 0
                and dispatch_context_count == 0
                and actor.inbox_depth == 0
                and actor.queued == 0
                and actor.leased == 0
                and actor.outbox_depth == 0
                and supervisor.pending_commands == 0
                and supervisor.validating_workers == 0
                and self.committer.pending_count() == 0
            ):
                return True
            time.sleep(0.025)
        return False

    def claim_stop_owner(self, owner: str) -> bool:
        requested = str(owner or "caller")
        with self._stop_owner_lock:
            if self._stopped.is_set():
                return True
            if not self._stop_owner:
                self._stop_owner = requested
            return self._stop_owner == requested

    def stop(
        self,
        timeout_seconds: float = 3.0,
        *,
        owner: str = "caller",
    ) -> str:
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        requested_owner = str(owner or "caller")
        if self._stopped.is_set():
            result_thread = self._result_thread
            observer_thread = self._observer_thread
            if (
                self._stop_status != "STOP_FAILED_RESULT_REPORTER_ACTIVE"
                and result_thread is None
                and observer_thread is None
            ):
                return self._stop_status
            with self._stop_owner_lock:
                current_owner = self._stop_owner or requested_owner
            if current_owner != requested_owner:
                return f"STOP_DEFERRED_TO_OWNER:{current_owner}"
            acquired = self._stop_lock.acquire(timeout=remaining())
            if not acquired:
                return self._stop_status
            try:
                result_thread = self._result_thread
                observer_thread = self._observer_thread
                if (
                    self._stop_status != "STOP_FAILED_RESULT_REPORTER_ACTIVE"
                    and result_thread is None
                    and observer_thread is None
                ):
                    return self._stop_status
                reporter_only_failure = (
                    self._stop_status
                    == "STOP_FAILED_RESULT_REPORTER_ACTIVE"
                )
                if self._stop_result_reporter(remaining):
                    self._log_stop_stage("result_reporter_reaped")
                    if reporter_only_failure:
                        self._stop_status = "STOPPED"
                return self._stop_status
            finally:
                self._stop_lock.release()
        if not self.claim_stop_owner(requested_owner):
            self._stopped.wait(remaining())
            if self._stopped.is_set():
                return self._stop_status
            with self._stop_owner_lock:
                current_owner = self._stop_owner or "unknown"
            return f"STOP_DEFERRED_TO_OWNER:{current_owner}"

        self._log_stop_stage(f"owner:{requested_owner}")
        self._log_stop_stage("waiting_for_stop_lock")
        acquired = self._stop_lock.acquire(timeout=remaining())
        if not acquired:
            self._log_stop_stage("stop_lock_timeout")
            self._stopped.wait(remaining())
            return (
                self._stop_status
                if self._stopped.is_set()
                else "STOP_TIMEOUT_IN_PROGRESS"
            )
        try:
            if self._stopped.is_set():
                return self._stop_status
            try:
                self._log_stop_stage("acquired")
                self._network_context.stop(timeout_seconds=0.0)
                # The 500ms resource reporter continuously preserves the last
                # operational snapshot. Stop must not wait on a diagnostic
                # snapshot lock before it seals physical execution.
                self._ingress_accepting.clear()
                self._ingress_stop.set()
                self._ingress_event.set()
                if self._ingress_thread is not None:
                    self._ingress_thread.join(timeout=min(0.15, remaining()))
                self._cancel_unsubmitted_ingress()
                self._log_stop_stage("ingress_sealed")
                self.actor.seal()
                self._log_stop_stage("actor_sealed")
                self._resource_stop.set()
                if self._resource_thread is not None:
                    self._resource_thread.join(timeout=min(0.15, remaining()))
                self.watchdog.stop(timeout_seconds=min(0.15, remaining()))
                self._log_stop_stage("reporters_stopped")
                supervisor_status = self.supervisor.stop(
                    timeout_seconds=remaining()
                )
                if supervisor_status == "not_started":
                    # A stop intent published before start is already a fully
                    # confirmed physical stop: no helper process or owner
                    # thread ever existed. Keep the terminal lifecycle state
                    # authoritative so a later start cannot appear pending.
                    supervisor_status = "STOPPED"
                self._desired_workers = 0
                self._pressure = 0.0
                self._deficit = 0
                self._log_stop_stage(f"supervisor:{supervisor_status}")
                actor_deadline = time.monotonic() + min(0.15, remaining())
                while time.monotonic() < actor_deadline:
                    actor_snapshot = self.actor.snapshot()
                    if (
                        actor_snapshot.leased <= 0
                        and actor_snapshot.outbox_depth <= 0
                    ):
                        break
                    time.sleep(0.01)
                self.committer.stop(timeout_seconds=remaining())
                self._log_stop_stage("committer_stopped")
                self.actor.stop(timeout_seconds=remaining())
                self._log_stop_stage("actor_stopped")
                result_reporter_stopped = self._stop_result_reporter(remaining)
                result_reporter_active = not result_reporter_stopped
                if result_reporter_active:
                    self._log_stop_stage("result_reporter_active")
                    if supervisor_status == "STOPPED":
                        supervisor_status = (
                            "STOP_FAILED_RESULT_REPORTER_ACTIVE"
                        )
                self.release_dispatch_contexts()
                self._stop_status = supervisor_status
                self._network_context.stop(timeout_seconds=min(0.5, remaining()))
                return supervisor_status
            finally:
                self._stopped.set()
        finally:
            self._stop_lock.release()

    def _stop_result_reporter(
        self,
        remaining: Callable[[], float],
    ) -> bool:
        result_thread = self._result_thread
        if result_thread is not None:
            if result_thread.is_alive():
                while result_thread.is_alive() and not self._result_sentinel_sent:
                    sentinel_wait = min(0.05, remaining())
                    try:
                        self._terminal_queue.put(None, timeout=max(0.0, sentinel_wait))
                        self._result_sentinel_sent = True
                    except queue.Full:
                        if sentinel_wait <= 0:
                            break
                        continue
                if result_thread.is_alive():
                    result_thread.join(timeout=remaining())
            if result_thread.is_alive():
                return False
            if self._result_thread is result_thread:
                self._result_thread = None

        observer_thread = self._observer_thread
        if observer_thread is None:
            return True
        if observer_thread.is_alive():
            while observer_thread.is_alive() and not self._observer_sentinel_sent:
                sentinel_wait = min(0.05, remaining())
                try:
                    self._observer_queue.put(None, timeout=max(0.0, sentinel_wait))
                    self._observer_sentinel_sent = True
                except queue.Full:
                    if sentinel_wait <= 0:
                        break
                    continue
            if observer_thread.is_alive():
                observer_thread.join(timeout=remaining())
        if observer_thread.is_alive():
            return False
        if self._observer_thread is observer_thread:
            self._observer_thread = None
        return True

    def _log_stop_stage(self, stage: str) -> None:
        # Stop is a hard lifecycle boundary. Synchronous logging can wait on a
        # handler lock held by an unrelated reporter, so publish the trace in
        # the domain snapshot and let the normal report path log it later.
        self._stop_stages.append(str(stage))
        observer = getattr(self, "stop_stage_observer", None)
        if callable(observer):
            try:
                observer(str(stage))
            except Exception:
                pass

    def snapshot(self) -> ValidationDomainSnapshot:
        actor_snapshot = self.actor.snapshot()
        supervisor_snapshot = self.supervisor.snapshot()
        transaction_window_limit, transaction_window_used = self._transaction_window(
            actor_snapshot=actor_snapshot,
            supervisor_snapshot=supervisor_snapshot,
        )
        result_thread = self._result_thread
        observer_thread = self._observer_thread
        self._capture_supervisor_snapshot(supervisor_snapshot)
        with self._diagnostics_lock:
            operational = self._last_operational_supervisor or supervisor_snapshot
            terminal_aggregates = {
                key: dict(value)
                for key, value in self._terminal_aggregates.items()
            }
            terminal_samples = tuple(dict(item) for item in self._terminal_samples)
            dispatch_samples = tuple(dict(item) for item in self._dispatch_samples)
            attempt_samples = tuple(dict(item) for item in self._attempt_samples)
            peak_helper_count = self._peak_helper_count
            stop_stages = tuple(self._stop_stages)
        with self._ingress_metrics_lock:
            accepted_transactions = self._accepted_transaction_count
            actor_submitted_transactions = (
                self._actor_submitted_transaction_count
            )
            ingress_cancelled_transactions = (
                self._ingress_cancelled_transaction_count
            )
            ingress_depth = self._ingress.depth()
            ingress_transfer_count = self._ingress_transfer_count
            dispatch_context_count = len(self._dispatch_contexts)
            fresh_transaction_pending_count = sum(
                not (
                    state.attempt_seen
                    or state.continuation_seen
                    or state.terminal_seen
                    or state.admission_seen
                )
                for state in self._dispatch_contexts.values()
            )
            duplicate_submission_count = self._duplicate_submission_count
            missing_dispatch_context_count = self._missing_dispatch_context_count
            duplicate_feedback_count = self._duplicate_feedback_count
        with self._result_metrics_lock:
            result_queue_high_water = self._result_queue_high_water
            observer_queue_high_water = self._observer_queue_high_water
            actor_result_max_ms = self._actor_result_max_ms
            observer_callback_max_ms = self._observer_callback_max_ms
        network_latest = self._network_context.latest()
        network_history = self._network_context.history()
        return ValidationDomainSnapshot(
            actor=actor_snapshot,
            supervisor=supervisor_snapshot,
            operational_supervisor=operational,
            peak_helper_count=peak_helper_count,
            commit_pending=self.committer.pending_count(),
            result_pending=max(0, int(self._terminal_queue.qsize())),
            desired_workers=self._desired_workers,
            pressure=self._pressure,
            deficit=self._deficit,
            stop_status=self._stop_status,
            endpoint_health=self._endpoint_controller.snapshot(),
            terminal_aggregates=terminal_aggregates,
            terminal_samples=terminal_samples,
            stop_stages=stop_stages,
            ingress_depth=ingress_depth,
            ingress_transfer_count=ingress_transfer_count,
            transaction_window_limit=transaction_window_limit,
            transaction_window_used=transaction_window_used,
            accepted_transactions=accepted_transactions,
            actor_submitted_transactions=actor_submitted_transactions,
            ingress_cancelled_transactions=ingress_cancelled_transactions,
            transaction_conservation_ok=(
                accepted_transactions
                == actor_submitted_transactions
                + ingress_depth
                + ingress_transfer_count
                + ingress_cancelled_transactions
                and actor_snapshot.conservation_ok
            ),
            runtime_network_context=(
                network_latest.to_dict() if network_latest is not None else None
            ),
            runtime_network_context_history=tuple(
                item.to_dict() for item in network_history
            ),
            dispatch_samples=dispatch_samples,
            attempt_samples=attempt_samples,
            controller_dispatch_consistency_ok=(
                actor_snapshot.controller_dispatch_mismatch_count == 0
            ),
            actor_owner_limit=transaction_window_limit,
            actor_owner_used=transaction_window_used,
            ready_work_limit=actor_snapshot.ready_work_limit,
            ready_work_used=actor_snapshot.ready_work_used,
            dispatch_context_count=dispatch_context_count,
            fresh_transaction_pending_count=fresh_transaction_pending_count,
            duplicate_submission_count=duplicate_submission_count,
            missing_dispatch_context_count=missing_dispatch_context_count,
            duplicate_feedback_count=duplicate_feedback_count,
            result_reporter_active=bool(
                (result_thread is not None and result_thread.is_alive())
                or (observer_thread is not None and observer_thread.is_alive())
            ),
            observer_pending=max(0, int(self._observer_queue.qsize())),
            result_queue_high_water=result_queue_high_water,
            observer_queue_high_water=observer_queue_high_water,
            actor_result_max_ms=actor_result_max_ms,
            observer_callback_max_ms=observer_callback_max_ms,
        )

    def _transaction_window(
        self,
        *,
        actor_snapshot: ExpansionActorSnapshot | None = None,
        supervisor_snapshot: SupervisorSnapshot | None = None,
    ) -> tuple[int, int]:
        actor = actor_snapshot or self.actor.snapshot()
        supervisor = supervisor_snapshot or self.supervisor.snapshot()
        # Parked transactions retain their endpoint ledger and terminal right,
        # but they are durable backlog rather than resident actor ownership.
        used = max(0, int(actor.actor_owner_used))
        limit, ready_limit = self._sync_active_worker_limits(
            supervisor,
            actor_used=used,
        )
        with self._ingress_metrics_lock:
            self._transaction_window_limit = limit
            self._transaction_window_used = used
            self._actor_owner_limit = limit
            self._actor_owner_used = used
            self._ready_work_limit = ready_limit
            self._ready_work_used = actor.ready_work_used
        return limit, used

    def _ingress_loop(self) -> None:
        while not self._ingress_stop.is_set():
            self._ingress_event.wait(0.05)
            self._ingress_event.clear()
            while (
                self._ingress_accepting.is_set()
                and not self._ingress_stop.is_set()
                and self._ingress.depth() > 0
            ):
                limit, used = self._transaction_window()
                if used >= limit:
                    break
                with self._ingress_metrics_lock:
                    envelope = self._ingress.pop()
                    if envelope is not None:
                        self._ingress_transfer_count += 1
                if envelope is None:
                    break
                try:
                    accepted = self.actor.submit(envelope)
                except BaseException:
                    with self._ingress_metrics_lock:
                        self._ingress.push(envelope, front=True)
                        self._ingress_transfer_count = max(
                            0,
                            self._ingress_transfer_count - 1,
                        )
                    raise
                if not accepted:
                    with self._ingress_metrics_lock:
                        self._ingress.push(envelope, front=True)
                        self._ingress_transfer_count = max(
                            0,
                            self._ingress_transfer_count - 1,
                        )
                    break
                with self._ingress_metrics_lock:
                    self._actor_submitted_transaction_count += 1
                    self._ingress_transfer_count = max(
                        0,
                        self._ingress_transfer_count - 1,
                    )

    def _cancel_unsubmitted_ingress(self) -> None:
        with self._ingress_metrics_lock:
            drained = self._ingress.drain()
            if not drained:
                return
            self._ingress_cancelled_transaction_count += len(drained)
        for envelope in drained:
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
                transaction_id=(
                    f"{self.refresh_epoch}:{envelope.candidate_digest[:24]}"
                ),
                slice_id=0,
                stage=ValidationStage.FAST_PROBE,
                purpose=envelope.purpose,
                report_json=envelope.report_json,
                reason="expansion_sealed_before_dispatch",
                clean_confirmed=True,
            )
            try:
                self._terminal_queue.put(terminal, timeout=0.05)
            except queue.Full:
                try:
                    self._terminal_observer(node_from_terminal(terminal), terminal)
                except Exception:
                    target = getattr(self._logger, "exception", None)
                    if callable(target):
                        target("Tier3 ingress cancellation observer failed")

    def _dispatch(self, command: Any) -> bool:
        accepted = self.supervisor.submit(command)
        if not accepted:
            return False
        with self._diagnostics_lock:
            self._dispatch_samples.append(
                {
                    "candidate_hash": str(command.candidate_digest)[:16],
                    "root_hash": hashlib.sha256(
                        str(command.source_root or "unknown").encode("utf-8")
                    ).hexdigest()[:16],
                    "family": str(command.source_family or "unknown"),
                    "stage": command.stage.value,
                    "endpoint": self._endpoint_diagnostic_label(command.endpoint),
                    "policy_revision": int(command.probe_policy_revision),
                    "portfolio_digest": str(command.portfolio_digest),
                    "selection_reason": str(command.endpoint_selection_reason),
                    "coverage_epoch": int(command.endpoint_coverage_epoch),
                    "canonical_position": int(
                        command.endpoint_canonical_position
                    ),
                    "dispatch_monotonic_ns": int(command.dispatch_monotonic_ns),
                    "worker_id": int(command.worker_id),
                    "worker_generation": int(command.worker_generation),
                }
            )
        return True

    def _attempt_received(self, result: EndpointAttemptResult) -> None:
        received_monotonic_ns = time.monotonic_ns()
        actor_started = time.perf_counter()
        self.actor.endpoint_result(result)
        actor_elapsed_ms = (time.perf_counter() - actor_started) * 1000.0
        with self._result_metrics_lock:
            self._actor_result_max_ms = max(
                self._actor_result_max_ms,
                actor_elapsed_ms,
            )
        fact = result.attempt_facts[-1] if result.attempt_facts else None
        with self._diagnostics_lock:
            self._attempt_samples.append(
                {
                    "candidate_hash": str(result.candidate_digest)[:16],
                    "slice_id": int(result.slice_id),
                    "stage": result.stage.value,
                    "dispatch_monotonic_ns": int(result.dispatch_monotonic_ns),
                    "transport_started_monotonic_ns": int(result.portfolio_started_monotonic_ns),
                    "yield_created_monotonic_ns": int(result.yield_created_monotonic_ns),
                    "received_monotonic_ns": received_monotonic_ns,
                    "endpoint": self._endpoint_diagnostic_label(
                        fact.endpoint if fact is not None else result.probe_url
                    ),
                    "kind": result.kind.value,
                    "completed": bool(fact.completed) if fact is not None else False,
                    "full_budget": bool(fact.full_budget) if fact is not None else False,
                    "entitlement_exhausted": (
                        bool(fact.entitlement_exhausted)
                        if fact is not None
                        else False
                    ),
                    "elapsed_ms": int(fact.elapsed_ms) if fact is not None else 0,
                    "failure_type": (
                        self._normalized_attempt_failure(fact)
                        if fact is not None
                        else "missing_attempt_fact"
                    ),
                    "policy_revision": int(result.probe_policy_revision),
                    "portfolio_digest": str(result.portfolio_digest),
                }
            )
        self._publish_observer_event("attempt", result)

    def _accepted_fast_observation(
        self,
        result: EndpointAttemptResult,
    ) -> None:
        """Publish one scheduler observation after actor lease validation."""

        if (
            self._feedback_observer is None
            or result.purpose is not Tier3ValidationPurpose.AUTO_ADMISSION
            or result.stage is not ValidationStage.FAST_PROBE
        ):
            return
        dispatch_context = self.dispatch_context_for_attempt(
            result.candidate_digest
        )
        self._publish_feedback_event(
            Tier3ValidationFeedbackEvent(
                kind=Tier3ValidationFeedbackKind.FAST_OBSERVATION,
                refresh_epoch=result.refresh_epoch,
                candidate_digest=result.candidate_digest,
                worker_generation=int(result.worker_generation),
                stage=result.stage,
                outcome=result.kind.value,
                busy_worker_ms=max(
                    int(result.total_elapsed_ms or 0),
                    int(result.facility_elapsed_ms or 0)
                    + int(result.portfolio_elapsed_ms or 0),
                ),
                facility_elapsed_ms=int(result.facility_elapsed_ms or 0),
                terminal=False,
                passed=result.kind is EndpointAttemptKind.PASS,
                ticket=(
                    dispatch_context.ticket
                    if dispatch_context is not None
                    else None
                ),
                facility_failure=False,
                right_censored=_attempt_is_right_censored(result),
            )
        )

    def _queue_attempt_received(self, result: EndpointAttemptResult) -> None:
        """Publish a helper result without blocking the sole Pipe reader."""

        self._terminal_queue.put_nowait(result)
        with self._result_metrics_lock:
            self._result_queue_high_water = max(
                self._result_queue_high_water,
                max(0, int(self._terminal_queue.qsize())),
            )

    def _queue_generation_gone(self, event: GenerationGone) -> None:
        """Keep generation observers outside the supervisor lifecycle thread."""

        self._terminal_queue.put_nowait(event)
        with self._result_metrics_lock:
            self._result_queue_high_water = max(
                self._result_queue_high_water,
                max(0, int(self._terminal_queue.qsize())),
            )

    def _publish_observer_event(self, kind: str, payload: object) -> None:
        observer_thread = self._observer_thread
        if observer_thread is None or not observer_thread.is_alive():
            self._observe_event(kind, payload)
            return
        self._observer_queue.put_nowait((str(kind), payload))
        with self._result_metrics_lock:
            self._observer_queue_high_water = max(
                self._observer_queue_high_water,
                max(0, int(self._observer_queue.qsize())),
            )

    def _publish_feedback_event(
        self,
        event: Tier3ValidationFeedbackEvent,
    ) -> None:
        """Publish scheduler ownership feedback before slow business observers.

        This callback is deliberately synchronous and must remain O(1). The
        result thread has already advanced the actor, so releasing the immutable
        candidate ticket here prevents report/cache work from starving future
        FAST supply.
        """

        observer = self._feedback_observer
        if observer is None:
            return
        try:
            observer(event)
        except Exception:
            target = getattr(self._logger, "exception", None)
            if callable(target):
                target("Tier3 validation credit feedback failed (%s)", event.kind.value)

    def _continuation_decision(
        self,
        candidate_digest: str,
        decision: Tier3DeepRetentionDecision,
        result: EndpointAttemptResult,
    ) -> None:
        if self._feedback_observer is None:
            return
        probe_elapsed_ms = max(
            [
                max(0, int(fact.elapsed_ms or 0))
                for fact in result.attempt_facts
            ]
            + [max(0, int(result.portfolio_elapsed_ms or 0))]
        )
        busy_worker_ms = max(
            max(0, int(result.total_elapsed_ms or 0)),
            max(0, int(result.facility_elapsed_ms or 0))
            + probe_elapsed_ms,
        )
        dispatch_context = self.dispatch_context_for_continuation(candidate_digest)
        self._publish_feedback_event(
            Tier3ValidationFeedbackEvent(
                kind=Tier3ValidationFeedbackKind.CONTINUATION,
                refresh_epoch=self.refresh_epoch,
                candidate_digest=str(candidate_digest),
                worker_generation=0,
                stage=ValidationStage.FAST_PROBE,
                outcome=result.kind.value,
                busy_worker_ms=busy_worker_ms,
                facility_elapsed_ms=max(
                    0,
                    int(result.facility_elapsed_ms or 0),
                ),
                terminal=False,
                passed=False,
                ticket=(
                    dispatch_context.ticket
                    if dispatch_context is not None
                    else None
                ),
                facility_failure=(
                    result.kind is EndpointAttemptKind.FACILITY_FAIL
                ),
                right_censored=_attempt_is_right_censored(result),
            )
        )

    def _observe_event(self, kind: str, payload: object) -> None:
        started = time.perf_counter()
        try:
            if kind == "attempt":
                result = payload
                if not isinstance(result, EndpointAttemptResult):
                    return
                if self._attempt_observer is not None:
                    self._attempt_observer(
                        Node.from_dict(_safe_json_dict(result.node_json)),
                        result,
                    )
                return
            if kind == "terminal":
                terminal = payload
                if not isinstance(terminal, ValidationTerminal):
                    return
                terminal_observer = self._terminal_observer
                if terminal_observer is not None:
                    terminal_observer(node_from_terminal(terminal), terminal)
                return
            if kind == "generation":
                event = payload
                if isinstance(event, GenerationGone) and self._generation_observer is not None:
                    self._generation_observer(event)
                return
            if kind == "admission":
                result = payload
                if self._admission_observer is not None and isinstance(
                    result,
                    (AdmissionCommitted, AdmissionRejected),
                ):
                    self._admission_observer(result)
        except Exception:
            target = getattr(self._logger, "exception", None)
            if callable(target):
                target("Tier3 validation business observer failed (%s)", kind)
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            with self._result_metrics_lock:
                self._observer_callback_max_ms = max(
                    self._observer_callback_max_ms,
                    elapsed_ms,
                )

    def _observer_loop(self) -> None:
        while True:
            item = self._observer_queue.get()
            try:
                if item is None:
                    return
                kind, payload = item
                self._observe_event(kind, payload)
            finally:
                self._observer_queue.task_done()

    @staticmethod
    def _endpoint_diagnostic_label(endpoint: str) -> str:
        value = str(endpoint or "")
        if not value:
            return ""
        parsed = urlsplit(value)
        authority = parsed.hostname or "unknown"
        if parsed.port:
            authority = f"{authority}:{parsed.port}"
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        return f"{parsed.scheme or 'unknown'}://{authority}#{digest}"

    @staticmethod
    def _normalized_attempt_failure(fact: ProbeAttemptFact) -> str:
        if fact.outcome == "pass":
            return "pass"
        if fact.status_code is not None:
            return f"http_{int(fact.status_code)}"
        detail = str(fact.detail or "").lower()
        if "tls" in detail and "eof" in detail:
            return "tls_eof"
        if "tls" in detail:
            return "tls_error"
        if "connect" in detail and "timeout" in detail:
            return "connect_timeout"
        if "timeout" in detail or fact.failure_domain == "endpoint_deadline":
            return "probe_timeout"
        if "body" in detail:
            return "body_mismatch"
        if fact.outcome:
            return str(fact.outcome)
        return str(fact.failure_domain or "unknown")

    def _capture_supervisor_snapshot(
        self,
        snapshot: SupervisorSnapshot | None = None,
    ) -> None:
        current = snapshot or self.supervisor.snapshot()
        with self._diagnostics_lock:
            if current.active_workers > 0 or self._last_operational_supervisor is None:
                self._last_operational_supervisor = current
            self._peak_helper_count = max(
                self._peak_helper_count,
                int(current.active_workers),
            )

    def _record_terminal_diagnostic(
        self,
        terminal: ValidationTerminal,
        node_payload: dict[str, Any],
        extra: dict[str, Any],
    ) -> None:
        root = str(
            extra.get("tier3_source_root_id")
            or extra.get("tier3_source_root_key")
            or node_payload.get("source")
            or "unknown"
        )
        family = str(
            extra.get("tier3_source_unit_family")
            or extra.get("tier3_protocol_family")
            or node_payload.get("protocol")
            or "unknown"
        )
        protocol = str(node_payload.get("protocol") or "unknown")
        root_hash = hashlib.sha256(root.encode("utf-8")).hexdigest()[:16]
        aggregate_key = f"{root_hash}:{family}:{protocol}"
        timings = {
            "queue_wait_ms": max(
                0,
                int(
                    (
                        (terminal.helper_started_monotonic_ns or terminal.monotonic_ns)
                        - terminal.submitted_monotonic_ns
                    )
                    / 1_000_000
                ),
            )
            if terminal.submitted_monotonic_ns > 0
            else 0,
            "dispatch_to_helper_ms": max(
                0,
                int(
                    (
                        (terminal.helper_started_monotonic_ns or terminal.monotonic_ns)
                        - terminal.dispatch_monotonic_ns
                    )
                    / 1_000_000
                ),
            )
            if terminal.dispatch_monotonic_ns > 0
            else 0,
            "dispatch_to_assignment_ms": max(
                0,
                int(
                    (
                        terminal.supervisor_assigned_monotonic_ns
                        - terminal.dispatch_monotonic_ns
                    )
                    / 1_000_000
                ),
            )
            if (
                terminal.dispatch_monotonic_ns > 0
                and terminal.supervisor_assigned_monotonic_ns > 0
            )
            else 0,
            "assignment_to_helper_ms": max(
                0,
                int(
                    (
                        terminal.helper_started_monotonic_ns
                        - terminal.supervisor_assigned_monotonic_ns
                    )
                    / 1_000_000
                ),
            )
            if (
                terminal.supervisor_assigned_monotonic_ns > 0
                and terminal.helper_started_monotonic_ns > 0
            )
            else 0,
            "assignment_to_send_ms": max(
                0,
                int(
                    (
                        terminal.supervisor_sent_monotonic_ns
                        - terminal.supervisor_assigned_monotonic_ns
                    )
                    / 1_000_000
                ),
            )
            if (
                terminal.supervisor_assigned_monotonic_ns > 0
                and terminal.supervisor_sent_monotonic_ns > 0
            )
            else 0,
            "send_to_helper_ms": max(
                0,
                int(
                    (
                        terminal.helper_started_monotonic_ns
                        - terminal.supervisor_sent_monotonic_ns
                    )
                    / 1_000_000
                ),
            )
            if (
                terminal.supervisor_sent_monotonic_ns > 0
                and terminal.helper_started_monotonic_ns > 0
            )
            else 0,
            "facility_ms": int(terminal.facility_elapsed_ms),
            "portfolio_ms": int(terminal.portfolio_elapsed_ms),
            "terminal_delivery_ms": max(
                0,
                int(
                    (time.monotonic_ns() - terminal.terminal_created_monotonic_ns)
                    / 1_000_000
                ),
            ),
        }
        sample = {
            "candidate_hash": terminal.candidate_digest[:16],
            "transaction_hash": hashlib.sha256(
                terminal.transaction_id.encode("utf-8")
            ).hexdigest()[:16]
            if terminal.transaction_id
            else "",
            "slice_id": int(terminal.slice_id),
            "terminal_created_monotonic_ns": int(terminal.terminal_created_monotonic_ns),
            "stage": terminal.stage.value,
            "root_hash": root_hash,
            "family": family,
            "protocol": protocol,
            "kind": terminal.kind.value,
            "reason": terminal.reason[:160],
            "probe_policy_revision": terminal.probe_policy_revision,
            "portfolio_digest": terminal.portfolio_digest,
            **timings,
            "attempts": [
                {
                    "endpoint": Tier3EndpointController._diagnostic_label(
                        fact.endpoint
                    ),
                    "elapsed_ms": int(fact.elapsed_ms),
                    "full_budget": bool(fact.full_budget),
                    "entitlement_exhausted": bool(
                        fact.entitlement_exhausted
                    ),
                    "outcome": fact.outcome,
                    "failure_domain": fact.failure_domain,
                    "failure_type": self._normalized_attempt_failure(fact),
                }
                for fact in terminal.attempt_facts
            ],
        }
        with self._diagnostics_lock:
            aggregate = self._terminal_aggregates.setdefault(
                aggregate_key,
                {
                    "attempted": 0,
                    "pass": 0,
                    "candidate_fail": 0,
                    "censored": 0,
                    "facility_fail": 0,
                    "cancelled": 0,
                },
            )
            aggregate["attempted"] += 1
            aggregate[terminal.kind.value] += 1
            self._terminal_samples.append(sample)

    def _commit_intent(self, intent: AdmissionIntent) -> AdmissionResult:
        node = node_from_terminal(
            ValidationTerminal(
                refresh_epoch=intent.refresh_epoch,
                candidate_digest=intent.candidate_digest,
                config_digest=intent.config_digest,
                lease_id=intent.lease_id,
                sequence=intent.sequence,
                worker_id=intent.worker_id,
                worker_generation=intent.worker_generation,
                kind=Tier3TerminalKind.PASS,
                node_json=intent.node_json,
                transaction_id=intent.transaction_id,
                slice_id=intent.slice_id,
                stage=intent.stage,
                purpose=intent.purpose,
                report_json=intent.report_json,
                reason=intent.reason,
                latency_ms=intent.latency_ms,
                slow=intent.slow,
                probe_url=intent.probe_url,
                attempt_facts=intent.attempt_facts,
                probe_policy_revision=intent.probe_policy_revision,
                portfolio_digest=intent.portfolio_digest,
                facility_elapsed_ms=intent.facility_elapsed_ms,
                portfolio_elapsed_ms=intent.portfolio_elapsed_ms,
                total_elapsed_ms=intent.total_elapsed_ms,
                clean_confirmed=intent.clean_confirmed,
                submitted_monotonic_ns=intent.submitted_monotonic_ns,
                dispatch_monotonic_ns=intent.dispatch_monotonic_ns,
                supervisor_assigned_monotonic_ns=(
                    intent.supervisor_assigned_monotonic_ns
                ),
                supervisor_sent_monotonic_ns=(
                    intent.supervisor_sent_monotonic_ns
                ),
                helper_started_monotonic_ns=intent.helper_started_monotonic_ns,
                portfolio_started_monotonic_ns=(
                    intent.portfolio_started_monotonic_ns
                ),
                terminal_created_monotonic_ns=(
                    intent.terminal_created_monotonic_ns
                ),
            )
        )
        commit_node = self._commit_node
        if commit_node is None:
            raise RuntimeError("validation callbacks are not bound")
        return commit_node(node, _safe_json_dict(intent.report_json), intent)

    def _admission_result(
        self,
        intent: AdmissionIntent,
        result: AdmissionResult,
    ) -> None:
        self.actor.admission_result(result)
        if self._feedback_observer is not None:
            dispatch_context = self.dispatch_context_for_admission(
                result.candidate_digest
            )
            self._publish_feedback_event(
                Tier3ValidationFeedbackEvent(
                    kind=Tier3ValidationFeedbackKind.ADMISSION,
                    refresh_epoch=result.refresh_epoch,
                    candidate_digest=result.candidate_digest,
                    worker_generation=int(intent.worker_generation),
                    stage=intent.stage,
                    outcome=(
                        "committed"
                        if isinstance(result, AdmissionCommitted)
                        else str(result.reason or "rejected")
                    ),
                    busy_worker_ms=0,
                    facility_elapsed_ms=0,
                    terminal=True,
                    passed=isinstance(result, AdmissionCommitted),
                    net_new_pool_growth=bool(
                        result.net_new_pool_growth
                        if isinstance(result, AdmissionCommitted)
                        else False
                    ),
                    ticket=(
                        dispatch_context.ticket
                        if dispatch_context is not None
                        else None
                    ),
                )
            )
        self._publish_observer_event("admission", result)

    def _generation_gone(self, event: GenerationGone) -> None:
        self._publish_observer_event("generation", event)

    def _result_loop(self) -> None:
        while True:
            result = self._terminal_queue.get()
            try:
                if result is None:
                    # Stop may have no join budget left. This sole producer
                    # must still close its unbounded observer queue in order.
                    if self._observer_thread is not None:
                        self._observer_queue.put_nowait(None)
                        self._observer_sentinel_sent = True
                    return
                if isinstance(result, EndpointAttemptResult):
                    self._attempt_received(result)
                    continue
                if isinstance(result, GenerationGone):
                    self._generation_gone(result)
                    continue
                terminal = result
                node_payload = _safe_json_dict(terminal.node_json)
                extra = node_payload.get("extra")
                extra = dict(extra) if isinstance(extra, dict) else {}
                self._record_terminal_diagnostic(terminal, node_payload, extra)
                if (
                    self._feedback_observer is not None
                    and terminal.purpose is Tier3ValidationPurpose.AUTO_ADMISSION
                ):
                    passed = terminal.kind is Tier3TerminalKind.PASS
                    dispatch_context = self.dispatch_context_for_terminal(
                        terminal.candidate_digest,
                        passed=passed,
                    )
                    self._publish_feedback_event(
                        Tier3ValidationFeedbackEvent(
                            kind=Tier3ValidationFeedbackKind.TERMINAL,
                            refresh_epoch=terminal.refresh_epoch,
                            candidate_digest=terminal.candidate_digest,
                            worker_generation=int(terminal.worker_generation),
                            stage=terminal.stage,
                            outcome=terminal.kind.value,
                            busy_worker_ms=max(
                                int(terminal.total_elapsed_ms or 0),
                                int(terminal.facility_elapsed_ms or 0)
                                + int(terminal.portfolio_elapsed_ms or 0),
                            ),
                            facility_elapsed_ms=int(
                                terminal.facility_elapsed_ms or 0
                            ),
                            terminal=True,
                            passed=passed,
                            ticket=(
                                dispatch_context.ticket
                                if dispatch_context is not None
                                else None
                            ),
                            facility_failure=(
                                terminal.kind is Tier3TerminalKind.FACILITY_FAIL
                            ),
                            right_censored=(
                                terminal.kind is Tier3TerminalKind.CENSORED
                            ),
                            monotonic_ns=int(
                                terminal.terminal_created_monotonic_ns
                                or terminal.monotonic_ns
                                or time.monotonic_ns()
                            ),
                        )
                    )
                self._publish_observer_event("terminal", terminal)
            finally:
                self._terminal_queue.task_done()

    def _resource_loop(self) -> None:
        while not self._resource_stop.wait(0.5):
            self.supervisor.refresh_resource_metrics()
            snapshot = self.supervisor.snapshot()
            self._sync_active_worker_limits(snapshot)
            self._capture_supervisor_snapshot(snapshot)
            if snapshot.hard_stall and not self._hard_stall_sealed:
                self._hard_stall_sealed = True
                self._ingress_accepting.clear()
                self._ingress_stop.set()
                self._ingress_event.set()
                self._cancel_unsubmitted_ingress()
                self.actor.seal()
                target = getattr(self._logger, "error", None)
                if callable(target):
                    target(
                        "Tier3 validator expansion sealed after hard stall: %s",
                        snapshot.last_failure_reason,
                    )
