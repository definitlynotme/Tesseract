from __future__ import annotations

import hashlib
import hmac
import ipaddress
import math
import queue
import re
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Iterable, Iterator, Mapping

from config_utils import coerce_bool
from diagnostic_privacy import (
    redact_diagnostic_text,
    runtime_identity_hash,
    runtime_scoped_hmac,
)


DEFAULT_MAX_ROTATIONS = 2
DEFAULT_EXCLUSION_TTL_SECONDS = 120.0
MAX_ROTATIONS_LIMIT = 4
MIN_EXCLUSION_TTL_SECONDS = 30.0
MAX_EXCLUSION_TTL_SECONDS = 900.0
RUNTIME_FAILURE_CORROBORATION_SECONDS = 5.0
RUNTIME_FAILURE_INTERMITTENT_SECONDS = 30.0
RUNTIME_FAILURE_PROBE_DEDUP_SECONDS = 2.0
RUNTIME_FAILURE_QUEUE_CAPACITY = 512


class RouteStabilityState(str, Enum):
    """Connection-stability state independent from durable node health."""

    STABLE = "stable"
    SUSPECT_FLOW = "suspect_flow"
    DIVERTED = "diverted"
    CONFIRMED_LEAF = "confirmed_leaf"
    FACILITY_HOLD = "facility_hold"
    RECOVERY_ELIGIBLE = "recovery_eligible"


class RuntimeEgressReadinessState(str, Enum):
    """User-visible data-plane readiness, separate from route ownership.

    A controller readback failure is not a failed leaf, and an activation
    hold is not a network-context facility transition.  Keeping these states
    explicit prevents a bounded verifier from turning into an endless busy
    UI or from decrementing node health without an attributable route.
    """

    # User traffic is attached to a healthy Core-loaded non-Gateway route.
    # Controller attribution and the live-fallback arm may still be catching
    # up; losing either must not tear down this already usable data plane.
    SERVING = "serving"
    READY = "ready"
    ACTIVATION_HOLD = "activation_hold"
    DATA_PLANE_FAILED = "data_plane_failed"
    CONTROLLER_HOLD = "controller_hold"
    FACILITY_HOLD = "facility_hold"
    FAIL_CLOSED = "fail_closed"


class RuntimeRecoveryStatus(str, Enum):
    """Terminal result of one incident-bound route recovery transaction."""

    RECOVERED = "recovered"
    FAIL_CLOSED = "fail_closed"
    FACILITY_HOLD = "facility_hold"
    CONTROLLER_HOLD = "controller_hold"
    STALE = "stale"
    MANUAL_SUPERSEDED = "manual_superseded"


@dataclass(frozen=True, slots=True)
class RuntimeEgressReadiness:
    state: RuntimeEgressReadinessState
    validation_active: bool
    reason: str = ""
    lifecycle_revision: int = 0
    route_epoch: int = 0
    network_context_epoch: int = 0
    deadline_monotonic: float = 0.0
    updated_at_monotonic: float = 0.0
    # (pid, process_generation, core_generation) of the route snapshot
    # published at route_epoch when this record was published; () when
    # unknown.  Decision D: a READY is carried to a newer route epoch only
    # while that same Core owner still serves (T-P1-10).
    route_owner: tuple[int, ...] = ()
    # The user route intent revision current at publication; -1 unknown.
    selector_intent_revision: int = -1
    # The physical FINAL-EXIT selection (the route selection signature
    # without its user-intent element) this record was proved or carried on;
    # () when unknown.  An epoch move that leaves it unchanged (a user-intent
    # bump alone) moved no traffic.
    route_physical: tuple[Any, ...] = ()


@dataclass(frozen=True, slots=True)
class CoreInventorySnapshot:
    """Exact managed inventory read back from one loaded Core generation."""

    process_pid: int
    process_generation: int
    core_generation: int
    semantic_digest: str
    route_epoch: int
    provider_version: str = ""
    members: tuple[RuntimeNodeRef, ...] = ()
    captured_at_monotonic: float = 0.0


@dataclass(frozen=True, slots=True)
class ManualValidationLease:
    """CAS token for one UI request attached to the canonical coordinator."""

    request_id: str
    lifecycle_revision: int
    route_epoch: int
    network_context_epoch: int
    deadline_monotonic: float


@dataclass(frozen=True, slots=True)
class RuntimeNodeRef:
    """Stable identity of one Core-loadable runtime leaf."""

    tier: str
    pool_kind: str
    identity: str
    proxy_name: str

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (
            str(self.tier),
            str(self.pool_kind),
            str(self.identity),
            str(self.proxy_name),
        )


def _runtime_recovery_hash(value: Any, *, namespace: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    return f"hmac:{runtime_scoped_hmac(text, namespace=namespace)}"


def _serialized_runtime_recovery_ref(
    ref: RuntimeNodeRef | None,
    *,
    namespace: str,
) -> dict[str, str] | None:
    if ref is None:
        return None
    return {
        "tier": str(ref.tier),
        "pool_kind": str(ref.pool_kind),
        "identity_hash": runtime_identity_hash(ref.identity),
        "proxy_name_hash": _runtime_recovery_hash(
            ref.proxy_name,
            namespace=f"runtime-recovery:{namespace}:proxy-name",
        ),
    }


@dataclass(frozen=True, slots=True)
class RuntimeRecoveryOutcome:
    """Immutable control result with a privacy-safe diagnostic projection.

    The full ``from_ref`` and ``to_ref`` remain available to the in-process
    control plane.  ``to_dict`` is deliberately one-way: it never exposes the
    raw incident token, node identity, proxy name, or those values when they
    are repeated inside the reason text.
    """

    status: RuntimeRecoveryStatus
    incident_id: str
    route_epoch: int
    budget_ms: int
    reason: str
    from_ref: RuntimeNodeRef | None = None
    to_ref: RuntimeNodeRef | None = None

    def __post_init__(self) -> None:
        try:
            status = (
                self.status
                if isinstance(self.status, RuntimeRecoveryStatus)
                else RuntimeRecoveryStatus(str(self.status))
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"unsupported runtime recovery status: {self.status!r}"
            ) from exc
        route_epoch = int(self.route_epoch)
        budget_ms = int(self.budget_ms)
        if route_epoch < 0:
            raise ValueError("route_epoch must be non-negative")
        if budget_ms < 0:
            raise ValueError("budget_ms must be non-negative")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "incident_id", str(self.incident_id or ""))
        object.__setattr__(self, "route_epoch", route_epoch)
        object.__setattr__(self, "budget_ms", budget_ms)
        object.__setattr__(self, "reason", str(self.reason or ""))

    def _serialized_reason(self) -> str:
        reason = self.reason
        sensitive_values = {
            self.incident_id,
            *(
                value
                for ref in (self.from_ref, self.to_ref)
                if ref is not None
                for value in (str(ref.identity), str(ref.proxy_name))
            ),
        }
        for raw_value in sorted(
            (value for value in sensitive_values if value),
            key=len,
            reverse=True,
        ):
            reason = reason.replace(
                raw_value,
                _runtime_recovery_hash(
                    raw_value,
                    namespace="runtime-recovery:reason-material",
                ),
            )
        return redact_diagnostic_text(reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "incident_hash": _runtime_recovery_hash(
                self.incident_id,
                namespace="runtime-recovery:incident",
            ),
            "route_epoch": self.route_epoch,
            "budget_ms": self.budget_ms,
            "reason": self._serialized_reason(),
            "from_ref": _serialized_runtime_recovery_ref(
                self.from_ref,
                namespace="from",
            ),
            "to_ref": _serialized_runtime_recovery_ref(
                self.to_ref,
                namespace="to",
            ),
        }


@dataclass(frozen=True, slots=True)
class RuntimeIncidentProbeContract:
    """One immutable data-plane test shared by every step of an incident.

    The URL is intentionally retained only for in-process execution.  The
    diagnostic projection exposes a process-scoped digest so logs cannot leak
    a user-configured endpoint.  A circuit that was opened with a contract
    accepts probe results only for that exact contract; callers therefore
    cannot confirm the origin with one target and candidates with another.
    """

    probe_url: str
    expected_status: str = "204"
    method: str = "GET"

    def __post_init__(self) -> None:
        probe_url = str(self.probe_url or "").strip()
        expected_status = str(self.expected_status or "").strip()
        method = str(self.method or "GET").strip().upper()
        if not probe_url:
            raise ValueError("probe_url must not be empty")
        if not expected_status:
            raise ValueError("expected_status must not be empty")
        if not method:
            raise ValueError("method must not be empty")
        object.__setattr__(self, "probe_url", probe_url)
        object.__setattr__(self, "expected_status", expected_status)
        object.__setattr__(self, "method", method)

    @property
    def contract_key(self) -> tuple[str, str, str]:
        return (self.method, self.probe_url, self.expected_status)

    def to_dict(self) -> dict[str, str]:
        return {
            "method": self.method,
            "probe_url_hash": _runtime_recovery_hash(
                self.probe_url,
                namespace="runtime-recovery:probe-url",
            ),
            "expected_status": self.expected_status,
        }


@dataclass(frozen=True, slots=True)
class ParsedTransportFailure:
    """Privacy-safe data extracted from one Mihomo data-plane log record.

    ``destination_token`` is a process-local keyed digest.  The raw business
    destination is deliberately not retained in this object.
    """

    protocol: str
    observed_outbound: str
    destination_token: str
    error_type: str
    observed_at_monotonic: float
    flow_token: str = ""
    mux_token: str = ""
    failure_phase: str = ""
    deterministic_proxy_endpoint: bool = False
    facility_hint: bool = False


@dataclass(frozen=True, slots=True)
class CoreLoadedGroupState:
    """Selection-relevant state published by one loaded Mihomo group."""

    name: str
    type_name: str
    members: tuple[str, ...] = ()
    now: str = ""
    fixed: str = ""
    test_url: str = ""
    expected_status: str = ""

    @property
    def signature(self) -> tuple[Any, ...]:
        return (
            str(self.name),
            str(self.type_name),
            tuple(str(member) for member in self.members),
            str(self.now),
            str(self.fixed),
            str(self.test_url),
            str(self.expected_status),
        )


@dataclass(frozen=True, slots=True)
class CoreLoadedRouteSnapshot:
    """Immutable description of the route graph accepted by the running Core."""

    process_pid: int
    process_generation: int
    core_generation: int
    semantic_digest: str
    route_epoch: int
    network_context_epoch: int = 0
    group_graph: tuple[tuple[str, tuple[str, ...]], ...] = ()
    group_states: tuple[CoreLoadedGroupState, ...] = ()
    proxy_nodes: tuple[RuntimeNodeRef, ...] = ()
    loaded_at_monotonic: float = 0.0

    def __post_init__(self) -> None:
        graph: list[tuple[str, tuple[str, ...]]] = []
        source_graph: Any = self.group_graph
        if isinstance(source_graph, Mapping):
            source_items: Iterable[tuple[Any, Any]] = source_graph.items()
        else:
            source_items = source_graph
        for name, members in source_items:
            graph.append(
                (
                    str(name),
                    tuple(str(member) for member in (members or ())),
                )
            )
        object.__setattr__(self, "group_graph", tuple(graph))
        object.__setattr__(
            self,
            "group_states",
            tuple(
                state
                for state in (self.group_states or ())
                if isinstance(state, CoreLoadedGroupState)
            ),
        )
        object.__setattr__(self, "proxy_nodes", tuple(self.proxy_nodes or ()))

    def group_state(self, name: str) -> CoreLoadedGroupState | None:
        expected = str(name or "")
        matches = [state for state in self.group_states if state.name == expected]
        return matches[0] if len(matches) == 1 else None

    @property
    def snapshot_key(self) -> tuple[int, int, int, str, int, int]:
        return (
            int(self.process_pid),
            int(self.process_generation),
            int(self.core_generation),
            str(self.semantic_digest),
            int(self.route_epoch),
            int(self.network_context_epoch),
        )

    def owns(self, binding: RuntimeRouteBinding) -> bool:
        return self.snapshot_key == binding.snapshot_key


@dataclass(frozen=True, slots=True)
class RuntimeRouteBinding:
    """The exact Core-loaded route and leaf observed when a failure occurred."""

    process_pid: int
    process_generation: int
    core_generation: int
    semantic_digest: str
    route_epoch: int
    observed_outbound: str
    route_chain: tuple[str, ...]
    node: RuntimeNodeRef
    network_context_epoch: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "route_chain",
            tuple(str(part) for part in (self.route_chain or ())),
        )

    @property
    def snapshot_key(self) -> tuple[int, int, int, str, int, int]:
        return (
            int(self.process_pid),
            int(self.process_generation),
            int(self.core_generation),
            str(self.semantic_digest),
            int(self.route_epoch),
            int(self.network_context_epoch),
        )

    @property
    def circuit_key(self) -> tuple[Any, ...]:
        # Keep all ownership coordinates in the key.  In particular, identity
        # alone is insufficient because the same endpoint may exist in more
        # than one tier or Core proxy variant.
        return (
            *self.snapshot_key,
            *self.node.key,
            _route_name(self.observed_outbound),
            tuple(_route_name(item) for item in self.route_chain),
        )


@dataclass(frozen=True, slots=True)
class BoundRuntimeFailure:
    """A parsed transport failure attributed to one immutable route binding."""

    binding: RuntimeRouteBinding
    protocol: str
    observed_outbound: str
    destination_token: str
    error_type: str
    observed_at_monotonic: float
    flow_token: str = ""
    mux_token: str = ""
    failure_phase: str = ""
    deterministic_proxy_endpoint: bool = False
    facility_hint: bool = False

    @property
    def circuit_key(self) -> tuple[Any, ...]:
        return self.binding.circuit_key

    @classmethod
    def from_parsed(
        cls,
        parsed: ParsedTransportFailure,
        binding: RuntimeRouteBinding,
    ) -> BoundRuntimeFailure | None:
        if _route_name(parsed.observed_outbound) != _route_name(
            binding.observed_outbound
        ):
            return None
        return cls(
            binding=binding,
            protocol=parsed.protocol,
            observed_outbound=parsed.observed_outbound,
            destination_token=parsed.destination_token,
            error_type=parsed.error_type,
            observed_at_monotonic=parsed.observed_at_monotonic,
            flow_token=parsed.flow_token,
            mux_token=parsed.mux_token,
            failure_phase=parsed.failure_phase,
            deterministic_proxy_endpoint=parsed.deterministic_proxy_endpoint,
            facility_hint=parsed.facility_hint,
        )


@dataclass(frozen=True, slots=True)
class FlowFailureEvidence:
    """One privacy-safe flow observation used for independence checks."""

    flow_token: str
    destination_token: str
    mux_token: str
    protocol: str
    error_type: str
    failure_phase: str
    observed_at_monotonic: float
    deterministic_proxy_endpoint: bool = False
    facility_hint: bool = False

    @property
    def cluster_token(self) -> str:
        # A Mihomo mux/session identifier is stronger than the local source
        # socket: multiple application sockets may share one broken upstream
        # session and must not be counted as independent node failures.
        return self.mux_token or self.flow_token

    @classmethod
    def from_failure(cls, failure: BoundRuntimeFailure) -> FlowFailureEvidence:
        return cls(
            flow_token=str(failure.flow_token or ""),
            destination_token=str(failure.destination_token or ""),
            mux_token=str(failure.mux_token or ""),
            protocol=str(failure.protocol or "UNKNOWN"),
            error_type=str(failure.error_type or ""),
            failure_phase=str(failure.failure_phase or ""),
            observed_at_monotonic=float(failure.observed_at_monotonic or 0.0),
            deterministic_proxy_endpoint=bool(
                failure.deterministic_proxy_endpoint
            ),
            facility_hint=bool(failure.facility_hint),
        )


@dataclass(frozen=True, slots=True)
class FailureCluster:
    """Failures sharing one flow or mux session; tokens are never serialized."""

    cluster_token: str
    flow_tokens: tuple[str, ...]
    destination_tokens: tuple[str, ...]
    error_types: tuple[str, ...]
    first_observed_monotonic: float
    last_observed_monotonic: float
    observation_count: int = 1
    deterministic_proxy_endpoint: bool = False

    @classmethod
    def from_evidence(cls, evidence: FlowFailureEvidence) -> FailureCluster:
        return cls(
            # Empty means attribution was unavailable. All anonymous records
            # in one incident intentionally collapse into one cluster.
            cluster_token=str(evidence.cluster_token or ""),
            flow_tokens=((evidence.flow_token,) if evidence.flow_token else ()),
            destination_tokens=(evidence.destination_token,),
            error_types=((evidence.error_type,) if evidence.error_type else ()),
            first_observed_monotonic=evidence.observed_at_monotonic,
            last_observed_monotonic=evidence.observed_at_monotonic,
            deterministic_proxy_endpoint=bool(
                evidence.deterministic_proxy_endpoint
            ),
        )

    def merge(self, evidence: FlowFailureEvidence) -> FailureCluster:
        return replace(
            self,
            flow_tokens=tuple(
                dict.fromkeys(
                    (
                        *self.flow_tokens,
                        *((evidence.flow_token,) if evidence.flow_token else ()),
                    )
                )
            ),
            destination_tokens=tuple(
                dict.fromkeys((*self.destination_tokens, evidence.destination_token))
            ),
            error_types=tuple(
                dict.fromkeys(
                    (
                        *self.error_types,
                        *((evidence.error_type,) if evidence.error_type else ()),
                    )
                )
            ),
            last_observed_monotonic=max(
                self.last_observed_monotonic,
                evidence.observed_at_monotonic,
            ),
            observation_count=self.observation_count + 1,
            deterministic_proxy_endpoint=bool(
                self.deterministic_proxy_endpoint
                or evidence.deterministic_proxy_endpoint
            ),
        )


@dataclass(frozen=True, slots=True)
class NodeHealthObservation:
    node: RuntimeNodeRef
    source: str
    verdict: str
    failure_domain: str
    incident_id: str
    failure_epoch: int
    quarantine_token: str
    observed_at_monotonic: float
    started_at_monotonic: float = 0.0
    completed_at_monotonic: float = 0.0


@dataclass(frozen=True, slots=True)
class NodeHealthTransition:
    node: RuntimeNodeRef
    previous: NodeHealthObservation | None
    current: NodeHealthObservation
    usable_before: bool
    usable_after: bool
    accepted: bool = True
    reason: str = ""


@dataclass(frozen=True, slots=True)
class LayerRecoveryDirective:
    node: RuntimeNodeRef
    action: str
    incident_id: str
    quarantine_token: str
    require_health_recheck: bool = True
    max_candidates: int = MAX_ROTATIONS_LIMIT
    deadline_monotonic: float = 0.0


@dataclass(frozen=True, slots=True)
class RuntimeFailureCircuitDecision:
    accepted: bool
    start_probe: bool = False
    divert_recommended: bool = False
    confirmed: bool = False
    cleared: bool = False
    deduplicated: bool = False
    stale: bool = False
    facility: bool = False
    deterministic: bool = False
    reason: str = ""
    incident_id: str = ""
    probe_token: str = ""
    probe_contract: RuntimeIncidentProbeContract | None = None
    terminal_status: RuntimeRecoveryStatus | None = None
    stability_state: RouteStabilityState = RouteStabilityState.STABLE


@dataclass(frozen=True, slots=True)
class RuntimeFailureCircuitSnapshot:
    state: str = "closed"
    stability_state: RouteStabilityState = RouteStabilityState.STABLE
    binding: RuntimeRouteBinding | None = None
    destination_tokens: tuple[str, ...] = ()
    failure_clusters: tuple[FailureCluster, ...] = ()
    first_failure_monotonic: float = 0.0
    last_observed_monotonic: float = 0.0
    last_probe_intent_monotonic: float = 0.0
    confirmed_monotonic: float = 0.0
    recovered_monotonic: float = 0.0
    quarantine_until_monotonic: float = 0.0
    failure_count: int = 0
    probe_intent_count: int = 0
    facility_evidence_count: int = 0
    confirmation_reason: str = ""
    incident_id: str = ""
    probe_token: str = ""
    probe_contract: RuntimeIncidentProbeContract | None = None
    terminal_status: RuntimeRecoveryStatus | None = None
    recovery_latency_ms: int | None = None

    @property
    def circuit_key(
        self,
    ) -> tuple[Any, ...] | None:
        return None if self.binding is None else self.binding.circuit_key

    def to_dict(self) -> dict[str, Any]:
        binding = self.binding
        node = binding.node if binding is not None else None
        return {
            "state": self.state,
            "process_pid": int(binding.process_pid) if binding else 0,
            "process_generation": int(binding.process_generation) if binding else 0,
            "core_generation": int(binding.core_generation) if binding else 0,
            "semantic_digest": binding.semantic_digest if binding else "",
            "route_epoch": int(binding.route_epoch) if binding else 0,
            "tier": node.tier if node else "",
            "pool_kind": node.pool_kind if node else "",
            "identity_hash": (
                runtime_identity_hash(node.identity) if node else ""
            ),
            "proxy_name": node.proxy_name if node else "",
            "stability_state": self.stability_state.value,
            # Never serialize even the keyed destination tokens.  Count is the
            # only operational information consumers need.
            "destination_count": len(self.destination_tokens),
            "flow_count": len(
                {
                    token
                    for cluster in self.failure_clusters
                    for token in cluster.flow_tokens
                    if token
                }
            ),
            "cluster_count": len(
                [cluster for cluster in self.failure_clusters if cluster.cluster_token]
            ),
            "first_failure_monotonic": float(self.first_failure_monotonic),
            "last_observed_monotonic": float(self.last_observed_monotonic),
            "confirmed_monotonic": float(self.confirmed_monotonic),
            "recovered_monotonic": float(self.recovered_monotonic),
            "quarantine_until_monotonic": float(
                self.quarantine_until_monotonic
            ),
            "failure_count": int(self.failure_count),
            "probe_intent_count": int(self.probe_intent_count),
            "facility_evidence_count": int(self.facility_evidence_count),
            "confirmation_reason": self.confirmation_reason,
            "incident_id": self.incident_id,
            "probe_contract": (
                self.probe_contract.to_dict()
                if self.probe_contract is not None
                else None
            ),
            "terminal_status": (
                self.terminal_status.value
                if self.terminal_status is not None
                else ""
            ),
            "recovery_latency_ms": self.recovery_latency_ms,
        }


class RuntimeFailureCircuit:
    """Generation-bound corroboration circuit for one active runtime leaf.

    The caller activates a route binding when it becomes current.  Results for
    any other Core generation, semantic digest, route epoch, tier, pool,
    identity, or proxy name are rejected as stale instead of being allowed to
    reset or clear the active incident. Business-flow correlation may recommend
    a reversible new-flow diversion, but only a failed independent leaf probe
    confirms node health. This keeps request bursts and multiplexed sessions
    from turning one ordinary fluctuation into a node-wide quarantine.
    """

    def __init__(
        self,
        *,
        corroboration_seconds: float = RUNTIME_FAILURE_CORROBORATION_SECONDS,
        intermittent_seconds: float = RUNTIME_FAILURE_INTERMITTENT_SECONDS,
        probe_dedup_seconds: float = RUNTIME_FAILURE_PROBE_DEDUP_SECONDS,
        active_binding: RuntimeRouteBinding | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._corroboration_seconds = max(0.1, float(corroboration_seconds))
        self._intermittent_seconds = max(
            self._corroboration_seconds,
            float(intermittent_seconds),
        )
        self._probe_dedup_seconds = max(0.1, float(probe_dedup_seconds))
        self._snapshot = RuntimeFailureCircuitSnapshot(binding=active_binding)

    def snapshot(self) -> RuntimeFailureCircuitSnapshot:
        with self._lock:
            return self._snapshot

    def activate(
        self,
        binding: RuntimeRouteBinding,
    ) -> RuntimeFailureCircuitSnapshot:
        with self._lock:
            if self._snapshot.circuit_key == binding.circuit_key:
                return self._snapshot
            self._snapshot = RuntimeFailureCircuitSnapshot(binding=binding)
            return self._snapshot

    def reset(
        self,
        binding: RuntimeRouteBinding | None = None,
    ) -> RuntimeFailureCircuitSnapshot:
        with self._lock:
            self._snapshot = RuntimeFailureCircuitSnapshot(binding=binding)
            return self._snapshot

    @staticmethod
    def _binding_is_current(
        event: BoundRuntimeFailure,
        *,
        active_binding: RuntimeRouteBinding | None,
        current_snapshot: CoreLoadedRouteSnapshot | None,
    ) -> bool:
        if (
            active_binding is not None
            and event.circuit_key != active_binding.circuit_key
        ):
            return False
        if (
            current_snapshot is not None
            and not current_snapshot.owns(event.binding)
        ):
            return False
        return True

    def observe(
        self,
        event: BoundRuntimeFailure,
        *,
        active_binding: RuntimeRouteBinding | None = None,
        current_snapshot: CoreLoadedRouteSnapshot | None = None,
        probe_contract: RuntimeIncidentProbeContract | None = None,
    ) -> RuntimeFailureCircuitDecision:
        if not event.destination_token or not event.binding.node.proxy_name:
            return RuntimeFailureCircuitDecision(
                accepted=False,
                reason="missing_destination_or_leaf",
            )
        if not self._binding_is_current(
            event,
            active_binding=active_binding,
            current_snapshot=current_snapshot,
        ):
            return RuntimeFailureCircuitDecision(
                accepted=False,
                stale=True,
                reason="stale_route_binding",
            )
        now = float(event.observed_at_monotonic or time.monotonic())
        if not math.isfinite(now) or now < 0.0:
            return RuntimeFailureCircuitDecision(
                accepted=False,
                stale=True,
                reason="invalid_observation_time",
            )
        evidence = FlowFailureEvidence.from_failure(event)
        with self._lock:
            current = self._snapshot
            if current.binding is None:
                current = RuntimeFailureCircuitSnapshot(binding=event.binding)
                self._snapshot = current
            if current.circuit_key != event.circuit_key:
                return RuntimeFailureCircuitDecision(
                    accepted=False,
                    stale=True,
                    reason="stale_route_binding",
                )
            terminal_status = current.terminal_status
            if terminal_status is None and current.state == "recovered":
                terminal_status = RuntimeRecoveryStatus.RECOVERED
            if terminal_status is not None:
                # A monitor/heartbeat replay under the same immutable route
                # authority cannot reopen a completed incident.  A material
                # authority change must explicitly activate/reset the circuit.
                return RuntimeFailureCircuitDecision(
                    accepted=True,
                    cleared=terminal_status is RuntimeRecoveryStatus.RECOVERED,
                    deduplicated=True,
                    reason=f"terminal_{terminal_status.value}_held",
                    incident_id=current.incident_id,
                    probe_token=current.probe_token,
                    probe_contract=current.probe_contract,
                    terminal_status=terminal_status,
                    stability_state=current.stability_state,
                )
            if (
                probe_contract is not None
                and current.incident_id
                and (
                    current.probe_contract is None
                    or current.probe_contract.contract_key
                    != probe_contract.contract_key
                )
            ):
                return RuntimeFailureCircuitDecision(
                    accepted=False,
                    stale=True,
                    deduplicated=True,
                    reason="incident_probe_contract_mismatch",
                    incident_id=current.incident_id,
                    probe_token=current.probe_token,
                    probe_contract=current.probe_contract,
                    stability_state=current.stability_state,
                )
            observation_watermark = max(
                float(current.first_failure_monotonic or 0.0),
                float(current.last_observed_monotonic or 0.0),
                float(current.confirmed_monotonic or 0.0),
                float(current.recovered_monotonic or 0.0),
            )
            if observation_watermark > 0.0 and now < observation_watermark:
                return RuntimeFailureCircuitDecision(
                    accepted=False,
                    stale=True,
                    reason="out_of_order_observation",
                    incident_id=current.incident_id,
                    probe_token=current.probe_token,
                    stability_state=current.stability_state,
                )
            failure_delta = (
                now - float(current.first_failure_monotonic)
                if current.incident_id
                else 0.0
            )
            expired_suspect = bool(
                current.state == "suspect"
                and current.incident_id
                and failure_delta > self._intermittent_seconds
            )
            if current.state == "indeterminate":
                # An attribution-neutral result must not become a sticky hold,
                # but a burst of lines from the same failed socket must still
                # collapse into one transaction.  A later business error may
                # open a fresh incident after this short, in-process quiet
                # window; no timer or guard probe reopens it by itself.
                if now - float(current.last_observed_monotonic or 0.0) < 2.0:
                    return RuntimeFailureCircuitDecision(
                        accepted=True,
                        deduplicated=True,
                        reason="indeterminate_burst_held",
                        stability_state=RouteStabilityState.STABLE,
                    )
            if current.state in {"closed", "indeterminate"} or expired_suspect:
                incident_id = _incident_token(event.circuit_key, now)
                probe_token = _probe_token(
                    incident_id,
                    f"{event.destination_token}:1",
                )
                stability_state = (
                    RouteStabilityState.FACILITY_HOLD
                    if evidence.facility_hint
                    else (
                        RouteStabilityState.DIVERTED
                        if evidence.deterministic_proxy_endpoint
                        else RouteStabilityState.SUSPECT_FLOW
                    )
                )
                self._snapshot = RuntimeFailureCircuitSnapshot(
                    state="suspect",
                    stability_state=stability_state,
                    binding=event.binding,
                    destination_tokens=(event.destination_token,),
                    failure_clusters=(FailureCluster.from_evidence(evidence),),
                    first_failure_monotonic=now,
                    last_observed_monotonic=now,
                    last_probe_intent_monotonic=(
                        0.0 if evidence.facility_hint else now
                    ),
                    failure_count=1,
                    probe_intent_count=(0 if evidence.facility_hint else 1),
                    facility_evidence_count=(1 if evidence.facility_hint else 0),
                    confirmation_reason=(
                        "facility_evidence"
                        if evidence.facility_hint
                        else (
                            "deterministic_proxy_endpoint_failure"
                            if evidence.deterministic_proxy_endpoint
                            else "first_transport_failure"
                        )
                    ),
                    incident_id=incident_id,
                    probe_token=probe_token,
                    probe_contract=probe_contract,
                )
                return RuntimeFailureCircuitDecision(
                    accepted=True,
                    start_probe=not evidence.facility_hint,
                    divert_recommended=evidence.deterministic_proxy_endpoint,
                    facility=evidence.facility_hint,
                    deterministic=evidence.deterministic_proxy_endpoint,
                    reason=self._snapshot.confirmation_reason,
                    incident_id=incident_id,
                    probe_token=probe_token,
                    probe_contract=probe_contract,
                    stability_state=stability_state,
                )
            if current.state in {"confirmed", "recovering", "quarantined"}:
                self._snapshot = replace(
                    current,
                    last_observed_monotonic=now,
                )
                return RuntimeFailureCircuitDecision(
                    accepted=True,
                    confirmed=True,
                    deduplicated=True,
                    reason="recovery_already_active",
                    incident_id=current.incident_id,
                    probe_token=current.probe_token,
                    probe_contract=current.probe_contract,
                    stability_state=current.stability_state,
                )

            clusters = list(current.failure_clusters)
            cluster_key = evidence.cluster_token
            matching_index = next(
                (
                    index
                    for index, cluster in enumerate(clusters)
                    if cluster.cluster_token == cluster_key
                ),
                None,
            )
            independent_cluster = bool(cluster_key and matching_index is None)
            if matching_index is None:
                clusters.append(FailureCluster.from_evidence(evidence))
            else:
                clusters[matching_index] = clusters[matching_index].merge(evidence)

            destination_tokens = tuple(
                dict.fromkeys((*current.destination_tokens, event.destination_token))
            )
            flow_tokens = {
                token
                for cluster in clusters
                for token in cluster.flow_tokens
                if token
            }
            independent_clusters = [
                cluster for cluster in clusters if cluster.cluster_token
            ]
            fast_divert = bool(
                0.0 <= failure_delta <= self._corroboration_seconds
                and len(independent_clusters) >= 2
                and len(flow_tokens) >= 2
                and len(destination_tokens) >= 2
            )
            intermittent_divert = bool(
                0.0 <= failure_delta <= self._intermittent_seconds
                and len(independent_clusters) >= 3
                and len(flow_tokens) >= 3
                and len(destination_tokens) >= 2
            )
            deterministic = bool(
                evidence.deterministic_proxy_endpoint
                or any(
                    cluster.deterministic_proxy_endpoint for cluster in clusters
                )
            )
            divert_recommended = bool(
                deterministic or fast_divert or intermittent_divert
            )
            facility = bool(
                evidence.facility_hint
                or current.stability_state == RouteStabilityState.FACILITY_HOLD
            )
            stability_state = (
                RouteStabilityState.FACILITY_HOLD
                if facility
                else (
                    RouteStabilityState.DIVERTED
                    if divert_recommended
                    else RouteStabilityState.SUSPECT_FLOW
                )
            )
            # The first observation owns the only probe lease for this
            # incident. Repeated monitor ticks may add corroborating evidence,
            # but they cannot rotate the token or launch a second probe. A new
            # authority revision must reset/activate the circuit first.
            probe_due = False
            probe_intent_count = current.probe_intent_count
            probe_token = current.probe_token
            reason = (
                "facility_evidence"
                if facility
                else (
                    "deterministic_proxy_endpoint_failure"
                    if deterministic
                    else (
                        "independent_flow_destination_failures"
                        if fast_divert
                        else (
                            "intermittent_failure_clusters"
                            if intermittent_divert
                            else (
                                "independent_failure_cluster_observed"
                                if independent_cluster
                                else "duplicate_flow_or_mux_failure"
                            )
                        )
                    )
                )
            )
            self._snapshot = replace(
                current,
                stability_state=stability_state,
                destination_tokens=destination_tokens,
                failure_clusters=tuple(clusters),
                last_observed_monotonic=now,
                last_probe_intent_monotonic=(
                    now if probe_due else current.last_probe_intent_monotonic
                ),
                # Repeated errors from one flow/mux are one operational
                # failure cluster, not a vote proportional to request volume.
                failure_count=max(1, len(independent_clusters)),
                probe_intent_count=probe_intent_count,
                facility_evidence_count=(
                    current.facility_evidence_count + (1 if evidence.facility_hint else 0)
                ),
                confirmation_reason=reason,
                probe_token=probe_token,
            )
            return RuntimeFailureCircuitDecision(
                accepted=True,
                start_probe=probe_due,
                divert_recommended=divert_recommended,
                deduplicated=not independent_cluster,
                facility=facility,
                deterministic=deterministic,
                reason=reason,
                incident_id=current.incident_id,
                probe_token=probe_token,
                probe_contract=current.probe_contract,
                stability_state=stability_state,
            )

    def finish_indeterminate(
        self,
        *,
        incident_id: str,
        quarantine_token: str,
        binding: RuntimeRouteBinding | None = None,
    ) -> RuntimeFailureCircuitSnapshot:
        """End one unproven incident without creating a retrying hold.

        The short-lived ``indeterminate`` state deduplicates the originating
        stdout burst.  It has no timer, quarantine, health, or route mutation
        authority; only a later external observation can open another incident.
        """

        with self._lock:
            current = self._snapshot
            if binding is not None and current.circuit_key != binding.circuit_key:
                return current
            if not self._transition_cas_matches(
                current,
                incident_id=incident_id,
                quarantine_token=quarantine_token,
            ):
                return current
            now = time.monotonic()
            self._snapshot = RuntimeFailureCircuitSnapshot(
                state="indeterminate",
                stability_state=RouteStabilityState.STABLE,
                binding=current.binding,
                last_observed_monotonic=now,
                confirmation_reason="indeterminate",
            )
            return self._snapshot

    def record_probe(
        self,
        event: BoundRuntimeFailure,
        *,
        succeeded: bool,
        facility_failure: bool = False,
        probe_token: str = "",
        probe_contract: RuntimeIncidentProbeContract | None = None,
        active_binding: RuntimeRouteBinding | None = None,
        current_snapshot: CoreLoadedRouteSnapshot | None = None,
    ) -> RuntimeFailureCircuitDecision:
        probe_token = str(probe_token or "").strip()
        if not probe_token:
            return RuntimeFailureCircuitDecision(
                accepted=False,
                reason="missing_probe_token",
            )
        if not self._binding_is_current(
            event,
            active_binding=active_binding,
            current_snapshot=current_snapshot,
        ):
            return RuntimeFailureCircuitDecision(
                accepted=False,
                stale=True,
                reason="stale_probe_result",
            )
        now = time.monotonic()
        with self._lock:
            current = self._snapshot
            if (
                current.circuit_key != event.circuit_key
                or current.state not in {"suspect", "confirmed"}
                or not hmac.compare_digest(probe_token, current.probe_token)
            ):
                return RuntimeFailureCircuitDecision(
                    accepted=False,
                    stale=True,
                    reason="stale_probe_result",
                )
            if (
                (current.probe_contract is None) != (probe_contract is None)
                or (
                    current.probe_contract is not None
                    and probe_contract is not None
                    and current.probe_contract.contract_key
                    != probe_contract.contract_key
                )
            ):
                return RuntimeFailureCircuitDecision(
                    accepted=False,
                    stale=True,
                    reason="incident_probe_contract_mismatch",
                    incident_id=current.incident_id,
                    probe_token=current.probe_token,
                    probe_contract=current.probe_contract,
                    stability_state=current.stability_state,
                )
            if facility_failure:
                self._snapshot = replace(
                    current,
                    stability_state=RouteStabilityState.FACILITY_HOLD,
                    facility_evidence_count=current.facility_evidence_count + 1,
                    confirmation_reason="probe_facility_unavailable",
                )
                return RuntimeFailureCircuitDecision(
                    accepted=True,
                    facility=True,
                    reason="probe_facility_unavailable",
                    incident_id=current.incident_id,
                    probe_token=current.probe_token,
                    probe_contract=current.probe_contract,
                    stability_state=RouteStabilityState.FACILITY_HOLD,
                )
            if (
                current.stability_state == RouteStabilityState.FACILITY_HOLD
                and not succeeded
            ):
                return RuntimeFailureCircuitDecision(
                    accepted=True,
                    facility=True,
                    reason="facility_hold_withholds_leaf_confirmation",
                    incident_id=current.incident_id,
                    probe_token=current.probe_token,
                    probe_contract=current.probe_contract,
                    stability_state=RouteStabilityState.FACILITY_HOLD,
                )
            if succeeded:
                if current.state == "confirmed":
                    # Two independent business destinations already confirmed
                    # this binding. A later one-off delay/204 success is useful
                    # recovery evidence, but it cannot erase that incident
                    # before layer attribution and quarantine are committed.
                    return RuntimeFailureCircuitDecision(
                        accepted=True,
                        confirmed=True,
                        deduplicated=True,
                        reason="confirmed_incident_requires_recovery",
                        incident_id=current.incident_id,
                        probe_token=current.probe_token,
                        probe_contract=current.probe_contract,
                        stability_state=current.stability_state,
                    )
                self._snapshot = RuntimeFailureCircuitSnapshot(
                    state="closed",
                    stability_state=RouteStabilityState.STABLE,
                    binding=current.binding,
                    last_observed_monotonic=current.last_observed_monotonic,
                    recovered_monotonic=now,
                )
                return RuntimeFailureCircuitDecision(
                    accepted=True,
                    cleared=True,
                    reason="independent_probe_succeeded",
                    incident_id=current.incident_id,
                    probe_contract=current.probe_contract,
                    stability_state=RouteStabilityState.STABLE,
                )
            self._snapshot = replace(
                current,
                state="confirmed",
                stability_state=RouteStabilityState.CONFIRMED_LEAF,
                confirmed_monotonic=now,
                confirmation_reason="independent_probe_failed",
            )
            return RuntimeFailureCircuitDecision(
                accepted=True,
                confirmed=True,
                reason="independent_probe_failed",
                incident_id=current.incident_id,
                probe_token=current.probe_token,
                probe_contract=current.probe_contract,
                stability_state=RouteStabilityState.CONFIRMED_LEAF,
            )

    def mark_recovering(
        self,
        *,
        incident_id: str,
        quarantine_token: str,
        binding: RuntimeRouteBinding | None = None,
    ) -> RuntimeFailureCircuitSnapshot:
        with self._lock:
            current = self._snapshot
            if binding is not None and current.circuit_key != binding.circuit_key:
                return current
            if not self._transition_cas_matches(
                current,
                incident_id=incident_id,
                quarantine_token=quarantine_token,
            ):
                return current
            if current.state not in {"confirmed", "recovering"}:
                return current
            self._snapshot = replace(
                current,
                state="recovering",
                stability_state=RouteStabilityState.DIVERTED,
            )
            return self._snapshot

    def mark_diverted(
        self,
        *,
        incident_id: str,
        quarantine_token: str,
        binding: RuntimeRouteBinding | None = None,
    ) -> RuntimeFailureCircuitSnapshot:
        """Record a reversible new-flow diversion without node quarantine."""

        with self._lock:
            current = self._snapshot
            if binding is not None and current.circuit_key != binding.circuit_key:
                return current
            if not self._transition_cas_matches(
                current,
                incident_id=incident_id,
                quarantine_token=quarantine_token,
            ):
                return current
            if current.state != "suspect":
                return current
            self._snapshot = replace(
                current,
                stability_state=RouteStabilityState.DIVERTED,
            )
            return self._snapshot

    def mark_quarantined(
        self,
        *,
        ttl_seconds: float,
        incident_id: str,
        quarantine_token: str,
        binding: RuntimeRouteBinding | None = None,
    ) -> RuntimeFailureCircuitSnapshot:
        with self._lock:
            current = self._snapshot
            if binding is not None and current.circuit_key != binding.circuit_key:
                return current
            if not self._transition_cas_matches(
                current,
                incident_id=incident_id,
                quarantine_token=quarantine_token,
            ):
                return current
            if current.binding is None or current.state not in {
                "confirmed",
                "recovering",
                "quarantined",
            }:
                return current
            self._snapshot = replace(
                current,
                state="quarantined",
                stability_state=RouteStabilityState.CONFIRMED_LEAF,
                quarantine_until_monotonic=(
                    time.monotonic() + max(0.0, float(ttl_seconds))
                ),
            )
            return self._snapshot

    def mark_recovered(
        self,
        *,
        incident_id: str,
        quarantine_token: str,
        binding: RuntimeRouteBinding | None = None,
    ) -> RuntimeFailureCircuitSnapshot:
        with self._lock:
            current = self._snapshot
            if binding is not None and current.circuit_key != binding.circuit_key:
                return current
            if not self._transition_cas_matches(
                current,
                incident_id=incident_id,
                quarantine_token=quarantine_token,
            ):
                return current
            if current.binding is None or current.state not in {
                "confirmed",
                "recovering",
                "quarantined",
            }:
                return current
            now = time.monotonic()
            confirmed_at = float(current.confirmed_monotonic or 0.0)
            self._snapshot = replace(
                current,
                state="recovered",
                stability_state=RouteStabilityState.RECOVERY_ELIGIBLE,
                recovered_monotonic=now,
                terminal_status=RuntimeRecoveryStatus.RECOVERED,
                recovery_latency_ms=(
                    int(round((now - confirmed_at) * 1000.0))
                    if confirmed_at > 0.0
                    else None
                ),
            )
            return self._snapshot

    def mark_terminal_outcome(
        self,
        outcome: RuntimeRecoveryOutcome,
        *,
        quarantine_token: str,
        binding: RuntimeRouteBinding | None = None,
    ) -> RuntimeFailureCircuitSnapshot:
        """Freeze any of the six recovery outcomes on its owning incident.

        This method records control-plane state only.  In particular,
        ``FAIL_CLOSED`` does not imply or perform a selector mutation, REJECT,
        or Core stop; those physical actions remain the caller's responsibility
        under its fixed-Gateway authority checks.
        """

        with self._lock:
            current = self._snapshot
            if binding is not None and current.circuit_key != binding.circuit_key:
                return current
            if not self._transition_cas_matches(
                current,
                incident_id=outcome.incident_id,
                quarantine_token=quarantine_token,
            ):
                return current
            if current.binding is None:
                return current
            if int(outcome.route_epoch) != int(current.binding.route_epoch):
                return current
            if current.terminal_status is not None:
                return current
            status = outcome.status
            stability_state = current.stability_state
            if status is RuntimeRecoveryStatus.RECOVERED:
                stability_state = RouteStabilityState.RECOVERY_ELIGIBLE
            elif status is RuntimeRecoveryStatus.FACILITY_HOLD:
                stability_state = RouteStabilityState.FACILITY_HOLD
            elif status is RuntimeRecoveryStatus.FAIL_CLOSED:
                stability_state = RouteStabilityState.CONFIRMED_LEAF
            now = time.monotonic()
            confirmed_at = float(current.confirmed_monotonic or 0.0)
            self._snapshot = replace(
                current,
                state=(
                    "recovered"
                    if status is RuntimeRecoveryStatus.RECOVERED
                    else "terminal"
                ),
                stability_state=stability_state,
                recovered_monotonic=(
                    now
                    if status is RuntimeRecoveryStatus.RECOVERED
                    else current.recovered_monotonic
                ),
                terminal_status=status,
                recovery_latency_ms=(
                    int(round((now - confirmed_at) * 1000.0))
                    if status is RuntimeRecoveryStatus.RECOVERED
                    and confirmed_at > 0.0
                    else current.recovery_latency_ms
                ),
            )
            return self._snapshot

    @staticmethod
    def _transition_cas_matches(
        snapshot: RuntimeFailureCircuitSnapshot,
        *,
        incident_id: str,
        quarantine_token: str,
    ) -> bool:
        expected_incident = str(incident_id or "").strip()
        expected_quarantine = str(quarantine_token or "").strip()
        return bool(
            snapshot.incident_id
            and snapshot.probe_token
            and expected_incident
            and expected_quarantine
            and hmac.compare_digest(expected_incident, snapshot.incident_id)
            and hmac.compare_digest(expected_quarantine, snapshot.probe_token)
        )


@dataclass(frozen=True, slots=True)
class QueuedRuntimeFailure:
    sequence: int
    coordinator_epoch: int
    failure: BoundRuntimeFailure


@dataclass(frozen=True, slots=True)
class QueuedRuntimeIngress:
    """One raw, immutable reader event owned by the coordinator queue."""

    sequence: int
    coordinator_epoch: int
    payload: Any


@dataclass(frozen=True, slots=True)
class RuntimeFailureSubmitDecision:
    accepted: bool
    sequence: int = 0
    overflowed: bool = False
    guard_required: bool = False
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RuntimeFailureQueueSnapshot:
    running: bool
    coordinator_epoch: int
    capacity: int
    pending_count: int
    submitted_count: int
    dropped_count: int
    overflow_guard_required: bool


@dataclass(frozen=True, slots=True)
class RuntimeFailureGuardToken:
    coordinator_epoch: int
    revision: int
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CoreMutationLease:
    token: str
    coordinator_epoch: int
    binding_key: tuple[Any, ...]
    snapshot_key: tuple[int, int, int, str, int, int]
    acquired_at_monotonic: float


class RuntimeEgressCoordinator:
    """Pure coordinator for runtime-failure ordering and ownership.

    This object performs no network, Core, or Manager I/O.  Producers submit
    already-bound failures without blocking.  The DesktopRuntime owner drains
    them in FIFO order, launches probes asynchronously, and uses the global
    mutation lease only around a Core route transaction.
    """

    def __init__(
        self,
        *,
        queue_capacity: int = RUNTIME_FAILURE_QUEUE_CAPACITY,
        corroboration_seconds: float = RUNTIME_FAILURE_CORROBORATION_SECONDS,
        automatic_guard_enabled: bool = True,
    ) -> None:
        self._capacity = max(1, int(queue_capacity))
        self._corroboration_seconds = max(0.1, float(corroboration_seconds))
        self._automatic_guard_enabled = bool(automatic_guard_enabled)
        self._state_lock = threading.RLock()
        self._mutation_lock = threading.Lock()
        self._queue: queue.Queue[QueuedRuntimeFailure | QueuedRuntimeIngress] = queue.Queue(
            maxsize=self._capacity
        )
        self._circuits: dict[tuple[Any, ...], RuntimeFailureCircuit] = {}
        self._running = True
        self._coordinator_epoch = 1
        self._sequence = 0
        self._submitted_count = 0
        self._dropped_count = 0
        self._guard_revision = 0
        self._guard_acked_revision = 0
        self._guard_inflight_revision = 0
        self._guard_reasons: set[str] = set()
        self._route_snapshot: CoreLoadedRouteSnapshot | None = None
        self._active_mutation_lease: CoreMutationLease | None = None

    def set_current_snapshot(
        self,
        snapshot: CoreLoadedRouteSnapshot | None,
    ) -> None:
        with self._state_lock:
            self._route_snapshot = snapshot

    # Naming alias for call sites that treat this as the loaded route graph.
    set_route_snapshot = set_current_snapshot

    def current_route_snapshot(self) -> CoreLoadedRouteSnapshot | None:
        with self._state_lock:
            return self._route_snapshot

    def snapshot_ownership_matches(
        self,
        binding: RuntimeRouteBinding,
        *,
        expected_snapshot: CoreLoadedRouteSnapshot | None = None,
    ) -> bool:
        """CAS helper: both expected and currently loaded snapshots must own it."""

        with self._state_lock:
            current = self._route_snapshot
            return bool(
                self._running
                and current is not None
                and current.owns(binding)
                and (
                    expected_snapshot is None
                    or expected_snapshot.snapshot_key == current.snapshot_key
                )
            )

    def submit(
        self,
        failure: BoundRuntimeFailure,
    ) -> RuntimeFailureSubmitDecision:
        """Enqueue without blocking; legacy profiles may request a guard."""

        with self._state_lock:
            if not self._running:
                return RuntimeFailureSubmitDecision(
                    accepted=False,
                    reason="coordinator_stopped",
                )
            self._sequence += 1
            sequence = self._sequence
            item = QueuedRuntimeFailure(
                sequence=sequence,
                coordinator_epoch=self._coordinator_epoch,
                failure=failure,
            )
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                self._dropped_count += 1
                if self._automatic_guard_enabled:
                    self._request_guard_locked("queue_overflow")
                return RuntimeFailureSubmitDecision(
                    accepted=False,
                    sequence=sequence,
                    overflowed=True,
                    guard_required=self._automatic_guard_enabled,
                    reason="failure_queue_full",
                )
            self._submitted_count += 1
            return RuntimeFailureSubmitDecision(
                accepted=True,
                sequence=sequence,
                guard_required=self._guard_revision > self._guard_acked_revision,
                reason="queued",
            )

    def submit_ingress(self, payload: Any) -> RuntimeFailureSubmitDecision:
        """Enqueue one stdout-owned immutable event without a second queue."""

        with self._state_lock:
            if not self._running:
                return RuntimeFailureSubmitDecision(
                    accepted=False,
                    reason="coordinator_stopped",
                )
            self._sequence += 1
            sequence = self._sequence
            item = QueuedRuntimeIngress(
                sequence=sequence,
                coordinator_epoch=self._coordinator_epoch,
                payload=payload,
            )
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                self._dropped_count += 1
                if self._automatic_guard_enabled:
                    self._request_guard_locked("queue_overflow")
                return RuntimeFailureSubmitDecision(
                    accepted=False,
                    sequence=sequence,
                    overflowed=True,
                    guard_required=self._automatic_guard_enabled,
                    reason="failure_queue_full",
                )
            self._submitted_count += 1
            return RuntimeFailureSubmitDecision(
                accepted=True,
                sequence=sequence,
                guard_required=self._guard_revision > self._guard_acked_revision,
                reason="queued",
            )

    def _request_guard_locked(self, reason: str) -> None:
        self._guard_revision += 1
        normalized = str(reason or "runtime_guard").strip() or "runtime_guard"
        self._guard_reasons.add(normalized)

    def request_guard(self, reason: str = "runtime_guard") -> None:
        """Request one bounded data-plane guard when attribution was unavailable."""

        with self._state_lock:
            if self._running and self._automatic_guard_enabled:
                self._request_guard_locked(reason)

    def _next_queue_item(
        self,
        *,
        timeout_seconds: float = 0.0,
    ) -> QueuedRuntimeFailure | QueuedRuntimeIngress | None:
        with self._state_lock:
            if not self._running:
                return None
            coordinator_epoch = self._coordinator_epoch
            work_queue = self._queue
        try:
            if float(timeout_seconds) > 0.0:
                item = work_queue.get(timeout=float(timeout_seconds))
            else:
                item = work_queue.get_nowait()
        except queue.Empty:
            return None
        with self._state_lock:
            if (
                not self._running
                or work_queue is not self._queue
                or item.coordinator_epoch != coordinator_epoch
                or item.coordinator_epoch != self._coordinator_epoch
            ):
                return None
        return item

    def next_queued(self) -> QueuedRuntimeFailure | None:
        item = self._next_queue_item()
        return item if isinstance(item, QueuedRuntimeFailure) else None

    def next_ingress(self, *, timeout_seconds: float = 0.0) -> Any | None:
        item = self._next_queue_item(timeout_seconds=timeout_seconds)
        if isinstance(item, QueuedRuntimeIngress):
            return item.payload
        if isinstance(item, QueuedRuntimeFailure):
            return item.failure
        return None

    def next_failure(self) -> BoundRuntimeFailure | None:
        item = self.next_queued()
        return None if item is None else item.failure

    def queue_snapshot(self) -> RuntimeFailureQueueSnapshot:
        with self._state_lock:
            return RuntimeFailureQueueSnapshot(
                running=self._running,
                coordinator_epoch=self._coordinator_epoch,
                capacity=self._capacity,
                pending_count=self._queue.qsize(),
                submitted_count=self._submitted_count,
                dropped_count=self._dropped_count,
                overflow_guard_required=(
                    self._guard_revision > self._guard_acked_revision
                ),
            )

    def claim_guard(self) -> RuntimeFailureGuardToken | None:
        with self._state_lock:
            if (
                not self._running
                or self._guard_inflight_revision > 0
                or self._guard_revision <= self._guard_acked_revision
            ):
                return None
            revision = self._guard_revision
            self._guard_inflight_revision = revision
            return RuntimeFailureGuardToken(
                coordinator_epoch=self._coordinator_epoch,
                revision=revision,
                reasons=tuple(sorted(self._guard_reasons)),
            )

    def acknowledge_guard(
        self,
        token: RuntimeFailureGuardToken,
        *,
        completed: bool,
    ) -> bool:
        with self._state_lock:
            if (
                token.coordinator_epoch != self._coordinator_epoch
                or token.revision != self._guard_inflight_revision
            ):
                return False
            self._guard_inflight_revision = 0
            if completed:
                self._guard_acked_revision = max(
                    self._guard_acked_revision,
                    token.revision,
                )
                if self._guard_acked_revision >= self._guard_revision:
                    self._guard_reasons.clear()
            return True

    def consume_overflow_guard(self) -> bool:
        token = self.claim_guard()
        if token is None:
            return False
        self.acknowledge_guard(token, completed=True)
        return True

    def observe(
        self,
        failure: BoundRuntimeFailure,
        *,
        current_snapshot: CoreLoadedRouteSnapshot | None = None,
        probe_contract: RuntimeIncidentProbeContract | None = None,
    ) -> RuntimeFailureCircuitDecision:
        with self._state_lock:
            if not self._running:
                return RuntimeFailureCircuitDecision(
                    accepted=False,
                    stale=True,
                    reason="coordinator_stopped",
                )
            loaded = self._route_snapshot
            if (
                loaded is None
                or not loaded.owns(failure.binding)
                or (
                    current_snapshot is not None
                    and current_snapshot.snapshot_key != loaded.snapshot_key
                )
            ):
                return RuntimeFailureCircuitDecision(
                    accepted=False,
                    stale=True,
                    reason="stale_route_binding",
                )
            circuit = self._circuits.get(failure.circuit_key)
            if circuit is None:
                circuit = RuntimeFailureCircuit(
                    corroboration_seconds=self._corroboration_seconds,
                    active_binding=failure.binding,
                )
                self._circuits[failure.circuit_key] = circuit
            # The state lock is deliberately held through the circuit write.
            # This makes the loaded-snapshot ownership check and the incident
            # mutation one linearizable operation with set_current_snapshot().
            # Lock order is always coordinator state -> circuit; circuit code
            # never calls back into the coordinator.
            return circuit.observe(
                failure,
                current_snapshot=loaded,
                probe_contract=probe_contract,
            )

    def record_probe(
        self,
        failure: BoundRuntimeFailure,
        *,
        succeeded: bool,
        facility_failure: bool = False,
        probe_token: str = "",
        probe_contract: RuntimeIncidentProbeContract | None = None,
        current_snapshot: CoreLoadedRouteSnapshot | None = None,
        allow_origin_binding: bool = False,
    ) -> RuntimeFailureCircuitDecision:
        with self._state_lock:
            if not self._running:
                return RuntimeFailureCircuitDecision(
                    accepted=False,
                    stale=True,
                    reason="coordinator_stopped",
                )
            loaded = self._route_snapshot
            loaded_matches = bool(
                loaded is not None
                and loaded.owns(failure.binding)
                and (
                    current_snapshot is None
                    or current_snapshot.snapshot_key == loaded.snapshot_key
                )
            )
            if not loaded_matches and not allow_origin_binding:
                return RuntimeFailureCircuitDecision(
                    accepted=False,
                    stale=True,
                    reason="stale_probe_result",
                )
            circuit = self._circuits.get(failure.circuit_key)
            if circuit is None:
                return RuntimeFailureCircuitDecision(
                    accepted=False,
                    stale=True,
                    reason="stale_probe_result",
                )
            return circuit.record_probe(
                failure,
                succeeded=succeeded,
                facility_failure=facility_failure,
                probe_token=probe_token,
                probe_contract=probe_contract,
                current_snapshot=(loaded if loaded_matches else None),
            )

    def current_snapshot_for(
        self,
        binding: RuntimeRouteBinding,
    ) -> RuntimeFailureCircuitSnapshot | None:
        with self._state_lock:
            circuit = self._circuits.get(binding.circuit_key)
            return None if circuit is None else circuit.snapshot()

    def current_snapshots(self) -> tuple[RuntimeFailureCircuitSnapshot, ...]:
        with self._state_lock:
            circuits = tuple(self._circuits.values())
            return tuple(circuit.snapshot() for circuit in circuits)

    def mark_recovering(
        self,
        binding: RuntimeRouteBinding,
        *,
        incident_id: str,
        quarantine_token: str,
    ) -> RuntimeFailureCircuitSnapshot | None:
        with self._state_lock:
            circuit = self._circuits.get(binding.circuit_key)
            return (
                None
                if circuit is None
                else circuit.mark_recovering(
                    binding=binding,
                    incident_id=incident_id,
                    quarantine_token=quarantine_token,
                )
            )

    def mark_diverted(
        self,
        binding: RuntimeRouteBinding,
        *,
        incident_id: str,
        quarantine_token: str,
    ) -> RuntimeFailureCircuitSnapshot | None:
        with self._state_lock:
            circuit = self._circuits.get(binding.circuit_key)
            return (
                None
                if circuit is None
                else circuit.mark_diverted(
                    binding=binding,
                    incident_id=incident_id,
                    quarantine_token=quarantine_token,
                )
            )

    def mark_quarantined(
        self,
        binding: RuntimeRouteBinding,
        *,
        ttl_seconds: float,
        incident_id: str,
        quarantine_token: str,
    ) -> RuntimeFailureCircuitSnapshot | None:
        with self._state_lock:
            circuit = self._circuits.get(binding.circuit_key)
            return (
                None
                if circuit is None
                else circuit.mark_quarantined(
                    binding=binding,
                    ttl_seconds=ttl_seconds,
                    incident_id=incident_id,
                    quarantine_token=quarantine_token,
                )
            )

    def mark_recovered(
        self,
        binding: RuntimeRouteBinding,
        *,
        incident_id: str,
        quarantine_token: str,
    ) -> RuntimeFailureCircuitSnapshot | None:
        with self._state_lock:
            circuit = self._circuits.get(binding.circuit_key)
            return (
                None
                if circuit is None
                else circuit.mark_recovered(
                    binding=binding,
                    incident_id=incident_id,
                    quarantine_token=quarantine_token,
                )
            )

    def finish_indeterminate(
        self,
        binding: RuntimeRouteBinding,
        *,
        incident_id: str,
        quarantine_token: str,
    ) -> RuntimeFailureCircuitSnapshot | None:
        with self._state_lock:
            circuit = self._circuits.get(binding.circuit_key)
            return (
                None
                if circuit is None
                else circuit.finish_indeterminate(
                    binding=binding,
                    incident_id=incident_id,
                    quarantine_token=quarantine_token,
                )
            )

    def mark_terminal_outcome(
        self,
        binding: RuntimeRouteBinding,
        outcome: RuntimeRecoveryOutcome,
        *,
        quarantine_token: str,
    ) -> RuntimeFailureCircuitSnapshot | None:
        """Atomically freeze a terminal result on the incident that owns it."""

        with self._state_lock:
            circuit = self._circuits.get(binding.circuit_key)
            return (
                None
                if circuit is None
                else circuit.mark_terminal_outcome(
                    outcome,
                    binding=binding,
                    quarantine_token=quarantine_token,
                )
            )

    def begin_core_mutation(
        self,
        binding: RuntimeRouteBinding,
        *,
        expected_snapshot: CoreLoadedRouteSnapshot | None = None,
        timeout_seconds: float | None = 0.0,
    ) -> CoreMutationLease | None:
        """Acquire the one global Core transaction lease and recheck CAS."""

        if timeout_seconds is None:
            acquired = self._mutation_lock.acquire()
        elif float(timeout_seconds) <= 0.0:
            acquired = self._mutation_lock.acquire(blocking=False)
        else:
            acquired = self._mutation_lock.acquire(timeout=float(timeout_seconds))
        if not acquired:
            return None
        with self._state_lock:
            current = self._route_snapshot
            valid = bool(
                self._running
                and self._active_mutation_lease is None
                and current is not None
                and current.owns(binding)
                and (
                    expected_snapshot is None
                    or expected_snapshot.snapshot_key == current.snapshot_key
                )
            )
            if not valid:
                self._mutation_lock.release()
                return None
            token = _keyed_token(
                "runtime-core-mutation-v1",
                (
                    f"{self._coordinator_epoch}\0{binding.circuit_key!r}\0"
                    f"{time.monotonic():.9f}\0{secrets.token_hex(8)}"
                ),
            )
            lease = CoreMutationLease(
                token=token,
                coordinator_epoch=self._coordinator_epoch,
                binding_key=binding.circuit_key,
                snapshot_key=current.snapshot_key,
                acquired_at_monotonic=time.monotonic(),
            )
            self._active_mutation_lease = lease
            return lease

    def core_mutation_lease_is_current(self, lease: CoreMutationLease) -> bool:
        with self._state_lock:
            current = self._route_snapshot
            return bool(
                self._running
                and self._active_mutation_lease == lease
                and lease.coordinator_epoch == self._coordinator_epoch
                and current is not None
                and current.snapshot_key == lease.snapshot_key
            )

    def core_mutation_active(self) -> bool:
        with self._state_lock:
            return bool(
                self._running and self._active_mutation_lease is not None
            )

    def end_core_mutation(self, lease: CoreMutationLease) -> bool:
        with self._state_lock:
            if self._active_mutation_lease != lease:
                return False
            self._active_mutation_lease = None
        self._mutation_lock.release()
        return True

    @contextmanager
    def core_mutation_transaction(
        self,
        binding: RuntimeRouteBinding,
        *,
        expected_snapshot: CoreLoadedRouteSnapshot | None = None,
        timeout_seconds: float | None = 0.0,
    ) -> Iterator[CoreMutationLease | None]:
        lease = self.begin_core_mutation(
            binding,
            expected_snapshot=expected_snapshot,
            timeout_seconds=timeout_seconds,
        )
        try:
            yield lease
        finally:
            if lease is not None:
                self.end_core_mutation(lease)

    def stop(self) -> RuntimeFailureQueueSnapshot:
        with self._state_lock:
            self._running = False
            self._coordinator_epoch += 1
            self._queue = queue.Queue(maxsize=self._capacity)
            self._circuits.clear()
            self._route_snapshot = None
            self._guard_revision = 0
            self._guard_acked_revision = 0
            self._guard_inflight_revision = 0
            self._guard_reasons.clear()
            return self.queue_snapshot()

    def restart(
        self,
        *,
        current_snapshot: CoreLoadedRouteSnapshot | None = None,
    ) -> RuntimeFailureQueueSnapshot:
        with self._state_lock:
            self._coordinator_epoch += 1
            self._queue = queue.Queue(maxsize=self._capacity)
            self._circuits.clear()
            self._running = True
            self._sequence = 0
            self._submitted_count = 0
            self._dropped_count = 0
            self._guard_revision = 0
            self._guard_acked_revision = 0
            self._guard_inflight_revision = 0
            self._guard_reasons.clear()
            self._route_snapshot = current_snapshot
            return self.queue_snapshot()


@dataclass(frozen=True, slots=True)
class Tier3RuntimeFailureEvent:
    """One generation-bound FINAL-EXIT transport failure from Mihomo."""

    core_generation: int
    semantic_digest: str
    selected_leaf: str
    selected_identity: str
    destination: str
    error_type: str
    observed_at_monotonic: float

    @property
    def binding(self) -> tuple[int, str, str, str]:
        return (
            int(self.core_generation),
            str(self.semantic_digest),
            str(self.selected_leaf),
            str(self.selected_identity),
        )


@dataclass(frozen=True, slots=True)
class Tier3RuntimeCircuitDecision:
    accepted: bool
    start_probe: bool = False
    confirmed: bool = False
    cleared: bool = False
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Tier3RuntimeCircuitSnapshot:
    state: str = "closed"
    core_generation: int = 0
    semantic_digest: str = ""
    selected_leaf: str = ""
    selected_identity: str = ""
    destinations: tuple[str, ...] = ()
    first_failure_monotonic: float = 0.0
    last_observed_monotonic: float = 0.0
    confirmed_monotonic: float = 0.0
    recovered_monotonic: float = 0.0
    quarantine_until_monotonic: float = 0.0
    failure_count: int = 0
    confirmation_reason: str = ""
    recovery_latency_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "core_generation": int(self.core_generation),
            "semantic_digest": self.semantic_digest,
            "selected_leaf": self.selected_leaf,
            "selected_identity_hash": (
                runtime_identity_hash(self.selected_identity)
                if self.selected_identity
                else ""
            ),
            "destination_count": len(self.destinations),
            "first_failure_monotonic": float(self.first_failure_monotonic),
            "last_observed_monotonic": float(self.last_observed_monotonic),
            "confirmed_monotonic": float(self.confirmed_monotonic),
            "recovered_monotonic": float(self.recovered_monotonic),
            "quarantine_until_monotonic": float(
                self.quarantine_until_monotonic
            ),
            "failure_count": int(self.failure_count),
            "confirmation_reason": self.confirmation_reason,
            "recovery_latency_ms": self.recovery_latency_ms,
        }


class Tier3RuntimeFailureCircuit:
    """Cross-confirm a bad selected leaf without touching durable pool state."""

    def __init__(self, *, corroboration_seconds: float = 5.0) -> None:
        self._lock = threading.Lock()
        self._corroboration_seconds = max(0.1, float(corroboration_seconds))
        self._snapshot = Tier3RuntimeCircuitSnapshot()

    def snapshot(self) -> Tier3RuntimeCircuitSnapshot:
        with self._lock:
            return self._snapshot

    def reset(self) -> Tier3RuntimeCircuitSnapshot:
        with self._lock:
            self._snapshot = Tier3RuntimeCircuitSnapshot()
            return self._snapshot

    def observe(
        self,
        event: Tier3RuntimeFailureEvent,
    ) -> Tier3RuntimeCircuitDecision:
        destination = str(event.destination or "").strip().lower()
        if not destination or not event.selected_leaf:
            return Tier3RuntimeCircuitDecision(
                accepted=False,
                reason="missing_destination_or_leaf",
            )
        now = float(event.observed_at_monotonic or time.monotonic())
        if not math.isfinite(now) or now < 0.0:
            return Tier3RuntimeCircuitDecision(
                accepted=False,
                reason="invalid_observation_time",
            )
        with self._lock:
            current = self._snapshot
            observation_watermark = max(
                float(current.first_failure_monotonic or 0.0),
                float(current.last_observed_monotonic or 0.0),
                float(current.confirmed_monotonic or 0.0),
                float(current.recovered_monotonic or 0.0),
            )
            if observation_watermark > 0.0 and now < observation_watermark:
                return Tier3RuntimeCircuitDecision(
                    accepted=False,
                    reason="out_of_order_observation",
                )
            same_binding = (
                int(current.core_generation),
                current.semantic_digest,
                current.selected_leaf,
                current.selected_identity,
            ) == event.binding
            failure_delta = now - float(current.first_failure_monotonic or now)
            expired_suspect = bool(
                current.state == "suspect"
                and current.first_failure_monotonic > 0.0
                and failure_delta > self._corroboration_seconds
            )
            if not same_binding or expired_suspect or current.state in {
                "closed",
                "recovered",
            }:
                self._snapshot = Tier3RuntimeCircuitSnapshot(
                    state="suspect",
                    core_generation=int(event.core_generation),
                    semantic_digest=str(event.semantic_digest),
                    selected_leaf=str(event.selected_leaf),
                    selected_identity=str(event.selected_identity),
                    destinations=(destination,),
                    first_failure_monotonic=now,
                    last_observed_monotonic=now,
                    failure_count=1,
                )
                return Tier3RuntimeCircuitDecision(
                    accepted=True,
                    start_probe=True,
                    reason="first_transport_failure",
                )
            if current.state in {"confirmed", "recovering", "quarantined"}:
                self._snapshot = replace(
                    current,
                    last_observed_monotonic=now,
                )
                return Tier3RuntimeCircuitDecision(
                    accepted=True,
                    confirmed=True,
                    reason="recovery_already_active",
                )
            destinations = tuple(dict.fromkeys((*current.destinations, destination)))
            confirmed = bool(
                current.first_failure_monotonic > 0.0
                and 0.0 <= failure_delta <= self._corroboration_seconds
                and len(destinations) >= 2
            )
            self._snapshot = Tier3RuntimeCircuitSnapshot(
                state="confirmed" if confirmed else "suspect",
                core_generation=current.core_generation,
                semantic_digest=current.semantic_digest,
                selected_leaf=current.selected_leaf,
                selected_identity=current.selected_identity,
                destinations=destinations,
                first_failure_monotonic=current.first_failure_monotonic,
                last_observed_monotonic=now,
                confirmed_monotonic=now if confirmed else 0.0,
                failure_count=current.failure_count + 1,
                confirmation_reason=(
                    "different_destination_transport_failure"
                    if confirmed
                    else "same_destination_transport_failure"
                ),
            )
            return Tier3RuntimeCircuitDecision(
                accepted=True,
                confirmed=confirmed,
                reason=self._snapshot.confirmation_reason,
            )

    def record_probe(
        self,
        event: Tier3RuntimeFailureEvent,
        *,
        succeeded: bool,
        facility_failure: bool = False,
    ) -> Tier3RuntimeCircuitDecision:
        now = time.monotonic()
        event_time = float(event.observed_at_monotonic or now)
        if not math.isfinite(event_time) or event_time < 0.0:
            return Tier3RuntimeCircuitDecision(
                accepted=False,
                reason="invalid_observation_time",
            )
        with self._lock:
            current = self._snapshot
            binding = (
                int(current.core_generation),
                current.semantic_digest,
                current.selected_leaf,
                current.selected_identity,
            )
            if (
                binding != event.binding
                or current.state != "suspect"
                or (
                    current.last_observed_monotonic > 0.0
                    and event_time < current.last_observed_monotonic
                )
            ):
                return Tier3RuntimeCircuitDecision(
                    accepted=False,
                    reason="stale_probe_result",
                )
            if facility_failure:
                return Tier3RuntimeCircuitDecision(
                    accepted=True,
                    reason="probe_facility_unavailable",
                )
            if succeeded:
                self._snapshot = Tier3RuntimeCircuitSnapshot(
                    state="closed",
                    last_observed_monotonic=current.last_observed_monotonic,
                    recovered_monotonic=now,
                )
                return Tier3RuntimeCircuitDecision(
                    accepted=True,
                    cleared=True,
                    reason="independent_probe_succeeded",
                )
            self._snapshot = Tier3RuntimeCircuitSnapshot(
                state="confirmed",
                core_generation=current.core_generation,
                semantic_digest=current.semantic_digest,
                selected_leaf=current.selected_leaf,
                selected_identity=current.selected_identity,
                destinations=current.destinations,
                first_failure_monotonic=current.first_failure_monotonic,
                last_observed_monotonic=current.last_observed_monotonic,
                confirmed_monotonic=now,
                failure_count=current.failure_count,
                confirmation_reason="independent_probe_failed",
            )
            return Tier3RuntimeCircuitDecision(
                accepted=True,
                confirmed=True,
                reason="independent_probe_failed",
            )

    def mark_recovering(self) -> Tier3RuntimeCircuitSnapshot:
        with self._lock:
            current = self._snapshot
            if current.state not in {"confirmed", "recovering"}:
                return current
            self._snapshot = replace(current, state="recovering")
            return self._snapshot

    def mark_quarantined(
        self,
        *,
        ttl_seconds: float,
    ) -> Tier3RuntimeCircuitSnapshot:
        with self._lock:
            current = self._snapshot
            self._snapshot = Tier3RuntimeCircuitSnapshot(
                state="quarantined",
                core_generation=current.core_generation,
                semantic_digest=current.semantic_digest,
                selected_leaf=current.selected_leaf,
                selected_identity=current.selected_identity,
                destinations=current.destinations,
                first_failure_monotonic=current.first_failure_monotonic,
                last_observed_monotonic=current.last_observed_monotonic,
                confirmed_monotonic=current.confirmed_monotonic,
                quarantine_until_monotonic=(
                    time.monotonic() + max(0.0, float(ttl_seconds))
                ),
                failure_count=current.failure_count,
                confirmation_reason=current.confirmation_reason,
            )
            return self._snapshot

    def mark_recovered(self) -> Tier3RuntimeCircuitSnapshot:
        with self._lock:
            current = self._snapshot
            now = time.monotonic()
            confirmed_at = float(current.confirmed_monotonic or 0.0)
            self._snapshot = Tier3RuntimeCircuitSnapshot(
                state="recovered",
                core_generation=current.core_generation,
                semantic_digest=current.semantic_digest,
                selected_leaf=current.selected_leaf,
                selected_identity=current.selected_identity,
                destinations=current.destinations,
                first_failure_monotonic=current.first_failure_monotonic,
                last_observed_monotonic=current.last_observed_monotonic,
                confirmed_monotonic=confirmed_at,
                recovered_monotonic=now,
                quarantine_until_monotonic=current.quarantine_until_monotonic,
                failure_count=current.failure_count,
                confirmation_reason=current.confirmation_reason,
                recovery_latency_ms=(
                    int(round((now - confirmed_at) * 1000.0))
                    if confirmed_at > 0.0
                    else None
                ),
            )
            return self._snapshot


@dataclass(frozen=True)
class EgressRecoveryPolicy:
    enabled: bool = True
    max_rotations: int = DEFAULT_MAX_ROTATIONS
    exclusion_ttl_seconds: float = DEFAULT_EXCLUSION_TTL_SECONDS
    retry_source_refresh: bool = True


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def egress_recovery_policy(config: Any) -> EgressRecoveryPolicy:
    root = _mapping(config)
    client = _mapping(root.get("client"))
    block = _mapping(client.get("egress_recovery"))
    try:
        max_rotations = int(block.get("max_rotations", DEFAULT_MAX_ROTATIONS))
    except (TypeError, ValueError):
        max_rotations = DEFAULT_MAX_ROTATIONS
    try:
        exclusion_ttl_seconds = float(
            block.get("exclusion_ttl_seconds", DEFAULT_EXCLUSION_TTL_SECONDS)
        )
    except (TypeError, ValueError):
        exclusion_ttl_seconds = DEFAULT_EXCLUSION_TTL_SECONDS
    return EgressRecoveryPolicy(
        enabled=coerce_bool(block.get("enabled", True), True),
        max_rotations=max(0, min(MAX_ROTATIONS_LIMIT, max_rotations)),
        exclusion_ttl_seconds=max(
            MIN_EXCLUSION_TTL_SECONDS,
            min(MAX_EXCLUSION_TTL_SECONDS, exclusion_ttl_seconds),
        ),
        retry_source_refresh=coerce_bool(
            block.get("retry_source_refresh", True),
            True,
        ),
    )


_HTTP_STATUS_PATTERN = re.compile(
    r"\bhttp(?:\s+error|\s+status)?\s*[:=]?\s*([45]\d\d)\b",
    re.IGNORECASE,
)

_HTTP_APPLICATION_FAILURE_PATTERN = re.compile(
    r"(?:\bhttp(?:/\d(?:\.\d)?)?\s+(?:status|error|code)?\s*[:=]?\s*[45]\d\d\b"
    r"|\b(?:unexpected\s+)?status(?:\s+code)?\s*[:=]?\s*[45]\d\d\b)",
    re.IGNORECASE,
)

_TRANSPORT_ERROR_MARKERS = (
    "context deadline exceeded",
    "i/o timeout",
    "unexpected_eof",
    "unexpected eof",
    " eof",
    "eof occurred in violation of protocol",
    "ssleoferror",
    "tls handshake",
    "remote error: tls",
    "tlsv1 alert internal error",
    "timed out",
    "timeout",
    "getaddrinfo",
    "name resolution",
    "temporary failure in name resolution",
    "nodename nor servname",
    "dns",
    "cannot connect",
    "connection refused",
    "connection reset",
    "wsarecv",
    "wsasend",
    "forcibly closed by the remote host",
    "connection aborted",
    "remote end closed connection",
    "remote disconnected",
    "network is unreachable",
    "no route to host",
    "proxy connection failed",
    "proxyerror",
    "broken pipe",
    "no such host",
    "server misbehaving",
)

_SHUTDOWN_CANCELLATION_MARKERS = (
    "context canceled",
    "context cancelled",
    "operation canceled",
    "operation cancelled",
    "request canceled",
    "request cancelled",
    "use of closed network connection",
    "application is shutting down",
    "core is shutting down",
    "process exited",
    "closed by user",
)

_INTERNAL_OR_CONTROL_MARKERS = (
    "internal probe",
    "guard probe",
    "mixed-port probe",
    "neutral probe",
    "health-check",
    "health check",
    "proxy-delay",
    "proxy delay",
    "url-test",
    "external-controller",
    "external controller",
    "controller request",
    "/proxies/",
)

_CONTENT_FAILURE_MARKERS = (
    "invalid json",
    "invalid response body",
    "unexpected content-type",
    "unexpected content type",
    "response did not contain",
    "content validation failed",
    "malformed response",
)

_PROTOCOL_PATTERN = re.compile(r"\[(TCP|UDP)\]", re.IGNORECASE)
_OBSERVED_OUTBOUND_PATTERN = re.compile(
    r"\bdial\s+(?P<outbound>[^\s(]+)",
    re.IGNORECASE,
)
_ARROW_DESTINATION_PATTERN = re.compile(
    r"--?>\s*(?P<destination>\[[^\]]+\](?::\d+)?|[^\s,;]+)",
    re.IGNORECASE,
)
_FLOW_SOURCE_PATTERN = re.compile(
    r"(?P<source>\[[^\]]+\]:\d+|[a-z0-9_.-]+:\d+)\s+--?>\s*",
    re.IGNORECASE,
)
_ERROR_DETAIL_PATTERN = re.compile(r"\berror\s*:\s*(?P<error>.+)$", re.IGNORECASE)
_RAW_LEVEL_FIELD_PATTERN = re.compile(
    r"\blevel\s*=\s*[\"']?(?P<level>debug|info|warn|warning|error|fatal)\b",
    re.IGNORECASE,
)
_RAW_LEVEL_PREFIX_PATTERN = re.compile(
    r"^\s*\[(?P<level>debug|info|warn|warning|error|fatal)\]",
    re.IGNORECASE,
)

_PROCESS_DESTINATION_HMAC_KEY = secrets.token_bytes(32)

_DETERMINISTIC_PROXY_PHASES = {
    "dialer_proxy",
    "proxy_auth",
    "proxy_authentication",
    "proxy_connect",
    "proxy_handshake",
    "proxy_protocol",
    "proxy_tls",
    "upstream_proxy",
}

_DETERMINISTIC_PROXY_ENDPOINT_MARKERS = (
    "failed to connect to proxy",
    "proxy authentication failed",
    "proxy certificate",
    "proxy handshake failed",
    "proxy protocol error",
    "proxy server refused",
    "upstream proxy refused",
)

_FACILITY_FAILURE_PHASES = {
    "controller",
    "core",
    "facility",
    "local_listener",
    "local_network",
    "probe_facility",
    "wan",
}


def _route_name(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _keyed_token(namespace: str, value: str) -> str:
    digest = hmac.new(
        _PROCESS_DESTINATION_HMAC_KEY,
        f"{namespace}\0{value}".encode("utf-8", "surrogatepass"),
        hashlib.sha256,
    ).hexdigest()
    return digest[:32]


def _destination_token(destination: str) -> str:
    return _keyed_token("runtime-destination-v1", destination)


def _flow_token(source_endpoint: str) -> str:
    value = " ".join(str(source_endpoint or "").strip().casefold().split())
    return _keyed_token("runtime-flow-v1", value) if value else ""


def _mux_token(value: str) -> str:
    normalized = " ".join(str(value or "").strip().casefold().split())
    return _keyed_token("runtime-mux-v1", normalized) if normalized else ""


def runtime_destination_token(destination: str) -> str:
    """Return the same private token used by parsed runtime destinations."""

    canonical = _canonical_destination(destination)
    return _destination_token(canonical) if canonical else ""


def runtime_process_subkey(namespace: str) -> bytes:
    """32 bytes derived from the per-process destination-token key.

    For consumers that keep their own keyed hashes (the node-judgement
    tokenizer) without a second random key.  Like the key itself, the value
    changes every process and must never be logged.
    """

    return hmac.new(
        _PROCESS_DESTINATION_HMAC_KEY,
        f"runtime-subkey-v1\0{namespace}".encode("utf-8", "surrogatepass"),
        hashlib.sha256,
    ).digest()


def _incident_token(circuit_key: tuple[Any, ...], observed_at: float) -> str:
    return _keyed_token(
        "runtime-incident-v1",
        f"{circuit_key!r}\0{observed_at:.9f}\0{secrets.token_hex(8)}",
    )


def _probe_token(incident_id: str, destination_token: str) -> str:
    return _keyed_token(
        "runtime-probe-v1",
        f"{incident_id}\0{destination_token}",
    )


def _record_text(record: Mapping[str, Any] | Any) -> tuple[str, str]:
    if not isinstance(record, Mapping):
        text = str(record or "").strip()
        match = _RAW_LEVEL_FIELD_PATTERN.search(text)
        if match is None:
            match = _RAW_LEVEL_PREFIX_PATTERN.search(text)
        severity = str(match.group("level") or "").casefold() if match else ""
        return text, severity
    severity = str(
        record.get("type")
        or record.get("level")
        or record.get("severity")
        or ""
    ).strip().casefold()
    parts: list[str] = []
    for key in ("payload", "message", "msg", "error"):
        value = str(record.get(key) or "").strip()
        if value and value not in parts:
            parts.append(value)
    return " ".join(parts), severity


def _structured_record_value(
    record: Mapping[str, Any] | Any,
    *keys: str,
) -> Any:
    if not isinstance(record, Mapping):
        return None
    wanted = {str(key).casefold() for key in keys}
    pending: list[Mapping[str, Any]] = [record]
    seen: set[int] = set()
    while pending:
        current = pending.pop(0)
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        for key, value in current.items():
            if (
                str(key).casefold() in wanted
                and value is not None
                and value != ""
            ):
                return value
        for nested_key in ("metadata", "context", "fields"):
            nested = current.get(nested_key)
            if isinstance(nested, Mapping):
                pending.append(nested)
    return None


def _canonical_destination(endpoint: str) -> str:
    value = str(endpoint or "").strip().rstrip(".,;)").casefold()
    if not value:
        return ""
    if value.startswith("["):
        closing = value.find("]")
        if closing > 1:
            value = value[1:closing]
    elif value.count(":") == 1:
        host, port = value.rsplit(":", 1)
        if port.isdigit():
            value = host
    elif value.count(":") > 1:
        host, possible_port = value.rsplit(":", 1)
        if possible_port.isdigit():
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                value = host
    return value.strip("[]")


def _destination_is_local(destination: str) -> bool:
    normalized = destination.rstrip(".").casefold()
    if normalized in {"localhost", "localhost.localdomain"}:
        return True
    if normalized.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return bool(address.is_loopback or address.is_link_local or address.is_unspecified)


def _classify_transport_error(error_detail: str) -> str:
    normalized = " ".join(str(error_detail or "").casefold().split())
    if not normalized:
        return ""
    if any(marker in normalized for marker in _SHUTDOWN_CANCELLATION_MARKERS):
        return ""
    if _HTTP_APPLICATION_FAILURE_PATTERN.search(normalized):
        return ""
    if any(marker in normalized for marker in _CONTENT_FAILURE_MARKERS):
        return ""
    if any(
        marker in normalized
        for marker in (
            "tls handshake",
            "remote error: tls",
            "tlsv1 alert",
            "ssleoferror",
            "violation of protocol",
        )
    ):
        return "tls"
    if any(
        marker in normalized
        for marker in (
            "getaddrinfo",
            "name resolution",
            "no such host",
            "server misbehaving",
            "dns lookup",
            "lookup ",
        )
    ):
        return "dns"
    if "connection refused" in normalized or "connectex" in normalized:
        return "connection_refused"
    if any(
        marker in normalized
        for marker in (
            "connection reset",
            "connection aborted",
            "wsarecv",
            "wsasend",
            "forcibly closed by the remote host",
            "remote end closed connection",
            "remote disconnected",
            "broken pipe",
        )
    ):
        return "connection_reset"
    if "network is unreachable" in normalized or "no route to host" in normalized:
        return "network_unreachable"
    if "eof" in normalized:
        return "eof"
    if any(
        marker in normalized
        for marker in (
            "context deadline exceeded",
            "i/o timeout",
            "timed out",
            "timeout",
        )
    ):
        return "timeout"
    if any(
        marker in normalized
        for marker in (
            "cannot connect",
            "connect failed",
            "connection failed",
            "proxy connection failed",
            "proxyerror",
        )
    ):
        return "connect"
    return ""


def runtime_transport_failure_from_log(
    record: Mapping[str, Any] | Any,
    *,
    observed_at_monotonic: float | None = None,
) -> ParsedTransportFailure | None:
    """Parse one real Mihomo data-plane warning without retaining its target.

    Only warning/error data-plane dial records are accepted.  DIRECT traffic,
    local/controller activity, internal probes, shutdown cancellation and HTTP
    application/content failures are intentionally outside this failure
    domain.
    """

    text, severity = _record_text(record)
    normalized = " ".join(text.casefold().split())
    if not normalized:
        return None
    if severity and severity not in {"warn", "warning", "error", "fatal"}:
        return None
    if any(marker in normalized for marker in _INTERNAL_OR_CONTROL_MARKERS):
        return None
    if any(marker in normalized for marker in _SHUTDOWN_CANCELLATION_MARKERS):
        return None
    outbound_match = _OBSERVED_OUTBOUND_PATTERN.search(text)
    if not outbound_match:
        return None
    observed_outbound = str(outbound_match.group("outbound") or "").strip()
    if _route_name(observed_outbound) in {"direct", "reject", "compatible"}:
        return None
    destination_match = _ARROW_DESTINATION_PATTERN.search(text)
    if not destination_match:
        return None
    destination = _canonical_destination(destination_match.group("destination"))
    if not destination or _destination_is_local(destination):
        return None
    error_match = _ERROR_DETAIL_PATTERN.search(text)
    if not error_match:
        return None
    error_detail = str(error_match.group("error") or "")
    failure_phase = "_".join(
        re.findall(
            r"[a-z0-9]+",
            str(
                _structured_record_value(
                    record,
                    "failure_phase",
                    "failurePhase",
                    "transport_phase",
                    "transportPhase",
                )
                or ""
            ).casefold(),
        )
    )
    deterministic_proxy_endpoint = bool(
        failure_phase in _DETERMINISTIC_PROXY_PHASES
        or any(
            marker in error_detail.casefold()
            for marker in _DETERMINISTIC_PROXY_ENDPOINT_MARKERS
        )
        or coerce_bool(
            _structured_record_value(
                record,
                "deterministic_proxy_endpoint",
                "deterministicProxyEndpoint",
            ),
            False,
        )
    )
    facility_hint = bool(
        failure_phase in _FACILITY_FAILURE_PHASES
        or coerce_bool(
            _structured_record_value(
                record,
                "facility_failure",
                "facilityFailure",
            ),
            False,
        )
        or str(
            _structured_record_value(record, "failure_domain", "failureDomain")
            or ""
        ).strip().casefold()
        == "facility"
    )
    error_type = _classify_transport_error(error_detail)
    if not error_type and deterministic_proxy_endpoint:
        error_type = "proxy_endpoint"
    if not error_type:
        return None
    protocol_match = _PROTOCOL_PATTERN.search(text)
    protocol = str(protocol_match.group(1)).upper() if protocol_match else "UNKNOWN"
    flow_match = _FLOW_SOURCE_PATTERN.search(text)
    source_endpoint = str(flow_match.group("source") or "") if flow_match else ""
    explicit_mux = str(
        _structured_record_value(
            record,
            "mux_id",
            "muxId",
            "session_id",
            "sessionId",
            "connection_id",
            "connectionId",
        )
        or ""
    )
    return ParsedTransportFailure(
        protocol=protocol,
        observed_outbound=observed_outbound,
        destination_token=_destination_token(destination),
        error_type=error_type,
        observed_at_monotonic=(
            time.monotonic()
            if observed_at_monotonic is None
            else float(observed_at_monotonic)
        ),
        flow_token=_flow_token(source_endpoint),
        mux_token=_mux_token(explicit_mux),
        failure_phase=failure_phase,
        deterministic_proxy_endpoint=deterministic_proxy_endpoint,
        facility_hint=facility_hint,
    )


# Short integration-friendly alias while the DesktopRuntime owner migrates its
# reader to the global domain.
transport_failure_from_log = runtime_transport_failure_from_log

_FINAL_EXIT_ROUTE_PATTERN = re.compile(
    r"(?:\bFINAL-EXIT\b|\bFINAL[_ -]?EXIT\b)",
    re.IGNORECASE,
)
def final_exit_transport_failure_from_log(
    record: Mapping[str, Any] | Any,
    *,
    core_generation: int,
    semantic_digest: str,
    selected_leaf: str,
    selected_identity: str = "",
    observed_at_monotonic: float | None = None,
) -> Tier3RuntimeFailureEvent | None:
    """Compatibility adapter for the former Tier3-only monitor.

    New code should parse once with :func:`runtime_transport_failure_from_log`
    and bind the result to a :class:`RuntimeRouteBinding`.  The compatibility
    event receives the keyed destination token, never the raw business host.
    """

    text, _severity = _record_text(record)
    if not _FINAL_EXIT_ROUTE_PATTERN.search(text):
        return None
    parsed = runtime_transport_failure_from_log(
        record,
        observed_at_monotonic=observed_at_monotonic,
    )
    if parsed is None:
        return None
    return Tier3RuntimeFailureEvent(
        core_generation=max(0, int(core_generation)),
        semantic_digest=str(semantic_digest or ""),
        selected_leaf=str(selected_leaf or ""),
        selected_identity=str(selected_identity or ""),
        destination=parsed.destination_token,
        error_type=parsed.error_type,
        observed_at_monotonic=parsed.observed_at_monotonic,
    )


def identity_attempt_error_is_transport(error: Any) -> bool:
    text = " ".join(str(error or "").strip().lower().split())
    if not text:
        return False
    statuses = [int(match) for match in _HTTP_STATUS_PATTERN.findall(text)]
    if statuses:
        return all(status in {502, 503, 504} for status in statuses)
    return any(marker in text for marker in _TRANSPORT_ERROR_MARKERS)


def _identity_attempts(payload: Any) -> list[Mapping[str, Any]]:
    root = _mapping(payload)
    identity = _mapping(root.get("identity"))
    attempts: list[Mapping[str, Any]] = []
    for family_name in ("ipv4", "ipv6"):
        family = _mapping(identity.get(family_name))
        evidence = family.get("evidence")
        if not isinstance(evidence, (list, tuple)):
            continue
        for source in evidence:
            source_attempts = _mapping(source).get("attempts")
            if not isinstance(source_attempts, (list, tuple)):
                continue
            attempts.extend(
                attempt
                for attempt in source_attempts
                if isinstance(attempt, Mapping)
            )
    return attempts


def all_identity_attempts_transport_failed(payload: Any) -> bool:
    attempts = _identity_attempts(payload)
    return bool(attempts) and all(
        str(attempt.get("status") or "").strip().lower() == "error"
        and identity_attempt_error_is_transport(attempt.get("error"))
        for attempt in attempts
    )


def identity_attempts_were_recorded(payload: Any) -> bool:
    return bool(_identity_attempts(payload))


def exit_quality_route_is_stable_and_confirmed(payload: Any) -> bool:
    route = _mapping(_mapping(payload).get("route"))
    confirmed = bool(
        route.get("confirmed")
        or (
            route.get("confirmed_before")
            and route.get("confirmed_after")
        )
    )
    return bool(confirmed and route.get("stable_during_check"))


def should_rotate_after_identity_failure(payload: Any, policy: EgressRecoveryPolicy) -> bool:
    return bool(
        policy.enabled
        and policy.max_rotations > 0
        and exit_quality_route_is_stable_and_confirmed(payload)
        and all_identity_attempts_transport_failed(payload)
    )
