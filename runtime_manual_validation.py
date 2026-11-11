from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterable


MAX_FAST_DIVERT_SECONDS = 1.5
MAX_MANUAL_VALIDATION_SECONDS = 5.0
MAX_MANUAL_PROBE_SECONDS = 5.0
# Opaque identifier of the owning fallback group's contract, measured via the
# controller URL test on the current non-Gateway leaf.  Callers pair it with
# one neutral contract; a backup must report both identifiers.
MANUAL_ROUTE_CONTRACT_ID = "route-0"


class ManualProbeFailureKind(str, Enum):
    NONE = "none"
    CONNECT = "connect"
    TIMEOUT = "timeout"
    RESET = "reset"
    EOF = "eof"
    TLS_TRANSPORT = "tls_transport"

    HTTP_STATUS = "http_status"
    CERTIFICATE_TARGET = "certificate_target"
    CONTENT = "content"
    CANCELLED = "cancelled"
    FACILITY = "facility"
    UNKNOWN = "unknown"


class ManualValidationOutcome(str, Enum):
    CURRENT_HEALTHY = "current_healthy"
    CURRENT_MIXED = "current_mixed"
    SWITCH_BEFORE_RETURN = "switch_before_return"
    BACKUP_REQUIRED = "backup_required"
    BACKUP_UNAVAILABLE = "backup_unavailable"
    NON_NODE_FAILURE = "non_node_failure"
    INVALID_CONTRACT = "invalid_contract"
    DEADLINE_EXPIRED = "deadline_expired"


_LEAF_TRANSPORT_FAILURES = frozenset(
    {
        ManualProbeFailureKind.CONNECT,
        ManualProbeFailureKind.TIMEOUT,
        ManualProbeFailureKind.RESET,
        ManualProbeFailureKind.EOF,
        ManualProbeFailureKind.TLS_TRANSPORT,
    }
)

_CERTIFICATE_TARGET_PATTERN = re.compile(
    r"(?:certificate verify failed|certificate_verify_failed|hostname mismatch|"
    r"doesn['’]?t match|not valid for|ip address mismatch|unknown ca|"
    r"certificate (?:has )?expired|self[- ]signed certificate)",
    re.IGNORECASE,
)
_HTTP_STATUS_PATTERN = re.compile(
    r"(?:http(?:/\d(?:\.\d)?)?\s+(?:status\s*)?[1-5]\d\d|"
    r"status(?:\s+code)?\s*[:=]?\s*[1-5]\d\d)",
    re.IGNORECASE,
)


def _bounded_seconds(value: Any, *, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(maximum)
    if not math.isfinite(parsed):
        return float(maximum)
    return max(0.0, min(float(maximum), parsed))


def clamp_manual_probe_timeout(
    requested_seconds: float | None = None,
    *,
    remaining_seconds: float | None = None,
) -> float:
    """Return a probe timeout bounded by the authoritative health contract."""

    requested = _bounded_seconds(
        MAX_MANUAL_PROBE_SECONDS
        if requested_seconds is None
        else requested_seconds,
        maximum=MAX_MANUAL_PROBE_SECONDS,
    )
    if remaining_seconds is None:
        return requested
    remaining = _bounded_seconds(
        remaining_seconds,
        maximum=MAX_MANUAL_VALIDATION_SECONDS,
    )
    return min(requested, remaining)


def adaptive_manual_probe_timeout(
    recent_verified_rtt_ms: float | int | None,
    *,
    configured_timeout_seconds: float | int | None,
    default_seconds: float = 2.0,
) -> float:
    """Budget a fresh HTTPS handshake without turning slow healthy leaves dead.

    The 1.5 second fast-divert target is a latency objective, not a health
    definition. A leaf with a slower, recently verified RTT receives enough
    time to repeat that contract, up to the configured (and hard five-second)
    health ceiling.
    """

    configured = _bounded_seconds(
        MAX_MANUAL_PROBE_SECONDS
        if configured_timeout_seconds is None
        else configured_timeout_seconds,
        maximum=MAX_MANUAL_PROBE_SECONDS,
    )
    if configured <= 0.0:
        configured = MAX_MANUAL_PROBE_SECONDS
    fallback = max(0.5, min(configured, float(default_seconds)))
    try:
        rtt_ms = float(recent_verified_rtt_ms)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(rtt_ms) or rtt_ms <= 0.0:
        return fallback
    requested = (3.0 * rtt_ms + 200.0) / 1000.0
    return max(0.5, min(configured, requested))


def normalise_manual_probe_failure(
    value: ManualProbeFailureKind | str | BaseException | None,
) -> ManualProbeFailureKind:
    """Classify a caller-supplied failure without retaining its raw text."""

    if isinstance(value, ManualProbeFailureKind):
        return value
    text = str(value or "").strip().casefold()
    if not text:
        return ManualProbeFailureKind.NONE
    canonical = text.replace("-", "_").replace(" ", "_")
    try:
        return ManualProbeFailureKind(canonical)
    except ValueError:
        pass
    if _CERTIFICATE_TARGET_PATTERN.search(text):
        return ManualProbeFailureKind.CERTIFICATE_TARGET
    if _HTTP_STATUS_PATTERN.search(text):
        return ManualProbeFailureKind.HTTP_STATUS
    if "cancel" in text or "closed network connection" in text:
        return ManualProbeFailureKind.CANCELLED
    if "timeout" in text or "timed out" in text or "deadline exceeded" in text:
        return ManualProbeFailureKind.TIMEOUT
    if "reset" in text or "broken pipe" in text or "forcibly closed" in text:
        return ManualProbeFailureKind.RESET
    if "unexpected eof" in text or text == "eof" or " eof" in text:
        return ManualProbeFailureKind.EOF
    if "tls" in text or "ssl" in text:
        return ManualProbeFailureKind.TLS_TRANSPORT
    if (
        "connect" in text
        or "refused" in text
        or "no route to host" in text
        or "network is unreachable" in text
    ):
        return ManualProbeFailureKind.CONNECT
    return ManualProbeFailureKind.UNKNOWN


@dataclass(frozen=True, slots=True)
class ManualValidationBudget:
    started_at_monotonic: float
    deadline_monotonic: float
    total_timeout_seconds: float
    per_probe_timeout_seconds: float

    @classmethod
    def create(
        cls,
        *,
        started_at_monotonic: float | None = None,
        total_timeout_seconds: float = MAX_MANUAL_VALIDATION_SECONDS,
        per_probe_timeout_seconds: float = MAX_MANUAL_PROBE_SECONDS,
    ) -> ManualValidationBudget:
        started = (
            time.monotonic()
            if started_at_monotonic is None
            else float(started_at_monotonic)
        )
        if not math.isfinite(started):
            raise ValueError("started_at_monotonic must be finite")
        total = _bounded_seconds(
            total_timeout_seconds,
            maximum=MAX_MANUAL_VALIDATION_SECONDS,
        )
        per_probe = min(
            total,
            _bounded_seconds(
                per_probe_timeout_seconds,
                maximum=MAX_MANUAL_PROBE_SECONDS,
            ),
        )
        return cls(
            started_at_monotonic=started,
            deadline_monotonic=started + total,
            total_timeout_seconds=total,
            per_probe_timeout_seconds=per_probe,
        )

    def remaining(self, now_monotonic: float | None = None) -> float:
        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        if not math.isfinite(now):
            return 0.0
        return max(0.0, self.deadline_monotonic - now)

    def probe_timeout(
        self,
        *,
        now_monotonic: float | None = None,
        requested_seconds: float | None = None,
    ) -> float:
        requested = (
            self.per_probe_timeout_seconds
            if requested_seconds is None
            else requested_seconds
        )
        return clamp_manual_probe_timeout(
            requested,
            remaining_seconds=self.remaining(now_monotonic),
        )


@dataclass(frozen=True, slots=True)
class ManualDataPlaneProbeResult:
    """Result for one opaque HTTPS-204 contract endpoint.

    ``contract_id`` should be an internal identifier or process-local digest,
    never the raw destination.  A declared success is accepted only when the
    caller also proves HTTPS, status 204, and a forced fresh connection.
    """

    contract_id: str
    succeeded: bool
    forced_new_connection: bool = True
    scheme: str = "https"
    status_code: int | None = None
    failure_kind: ManualProbeFailureKind | str = ManualProbeFailureKind.NONE
    certificate_target_error: bool = False
    completed_at_monotonic: float | None = None
    # Diagnostic metadata only. It never contributes to node-health decisions.
    probe_indeterminate_reason: str | None = None

    @classmethod
    def https_204(
        cls,
        contract_id: str,
        *,
        completed_at_monotonic: float | None = None,
    ) -> ManualDataPlaneProbeResult:
        return cls(
            contract_id=contract_id,
            succeeded=True,
            forced_new_connection=True,
            scheme="https",
            status_code=204,
            failure_kind=ManualProbeFailureKind.NONE,
            completed_at_monotonic=completed_at_monotonic,
        )

    @classmethod
    def transport_failure(
        cls,
        contract_id: str,
        failure_kind: ManualProbeFailureKind | str,
        *,
        completed_at_monotonic: float | None = None,
    ) -> ManualDataPlaneProbeResult:
        return cls(
            contract_id=contract_id,
            succeeded=False,
            forced_new_connection=True,
            scheme="https",
            status_code=None,
            failure_kind=failure_kind,
            completed_at_monotonic=completed_at_monotonic,
        )

    @property
    def normalised_failure_kind(self) -> ManualProbeFailureKind:
        if self.certificate_target_error:
            return ManualProbeFailureKind.CERTIFICATE_TARGET
        if self.status_code is not None:
            try:
                status_code = int(self.status_code)
            except (TypeError, ValueError):
                return ManualProbeFailureKind.HTTP_STATUS
            if status_code != 204:
                return ManualProbeFailureKind.HTTP_STATUS
        return normalise_manual_probe_failure(self.failure_kind)

    def completed_within(self, budget: ManualValidationBudget) -> bool:
        completed = self.completed_at_monotonic
        if completed is None:
            return True
        try:
            value = float(completed)
        except (TypeError, ValueError):
            return False
        return bool(
            math.isfinite(value)
            and budget.started_at_monotonic <= value <= budget.deadline_monotonic
        )

    def is_https_204_success(self, budget: ManualValidationBudget) -> bool:
        return bool(
            self.completed_within(budget)
            and self.succeeded
            and self.forced_new_connection
            and str(self.scheme or "").strip().casefold() == "https"
            and self.status_code == 204
            and not self.certificate_target_error
            and self.normalised_failure_kind is ManualProbeFailureKind.NONE
        )

    def is_leaf_transport_failure(self, budget: ManualValidationBudget) -> bool:
        return bool(
            self.completed_within(budget)
            and not self.succeeded
            and self.forced_new_connection
            and str(self.scheme or "").strip().casefold() == "https"
            # Receiving any HTTP status proves that the transport reached an
            # HTTP peer. It is an endpoint/contract result, not leaf evidence.
            and self.status_code is None
            and not self.certificate_target_error
            and self.normalised_failure_kind in _LEAF_TRANSPORT_FAILURES
        )


# Controller URL-test verdicts, keyed by exception class name so this module
# needs no import of the controller client (the pure-module convention); the
# tests pin every key to a real ``mihomo_api`` class.  The MRO is walked
# most-specific first: the delay subclasses derive from MihomoDelayTestFailed,
# which derives from MihomoAPIError.
_DELAY_ERROR_FAILURE_KINDS: dict[str, ManualProbeFailureKind] = {
    # 200 plus a failed expected-status readback: the leaf transported and an
    # HTTP peer answered (reputation, captcha, redirect).  Never leaf evidence.
    "MihomoDelayStatusMismatch": ManualProbeFailureKind.HTTP_STATUS,
    # 504 {"message": "Timeout"}: the URL test found no answer through the leaf.
    "MihomoDelayTimeout": ManualProbeFailureKind.TIMEOUT,
    # 503: dial, TLS, reset or EOF through the leaf.
    "MihomoDelayTransportFailed": ManualProbeFailureKind.CONNECT,
    # A negative verdict whose kind was not preserved is an "other API error"
    # (design v3 §5.2): indeterminate, never leaf evidence.
    "MihomoDelayTestFailed": ManualProbeFailureKind.FACILITY,
    # Client timeout, HTML upstream error or controller failure: says nothing
    # about the leaf.
    "MihomoAPIError": ManualProbeFailureKind.FACILITY,
}
# Fixed probe-broker vocabulary (runtime_probe_broker.ProbeIndeterminateReason
# values, pinned by the tests).  An exception carrying one of these keeps it,
# so the overview's busy/cancelled mapping survives; any other value is
# replaced by a fixed token and never retained.
_BROKER_INDETERMINATE_REASONS = frozenset(
    {
        "queue_timeout",
        "started_timeout",
        "execution_timeout",
        "cancelled",
        "broker_stopped",
        "broker_poisoned",
        "socket_not_started",
    }
)


def _delay_error_class_name(exc: BaseException | None) -> str | None:
    for klass in type(exc).__mro__:
        if klass.__name__ in _DELAY_ERROR_FAILURE_KINDS:
            return klass.__name__
    return None


def manual_failure_kind_from_delay_error(
    exc: BaseException | None,
) -> ManualProbeFailureKind:
    """Classify a controller URL-test exception by type, never by its text.

    The text normaliser reads ``"HTTP 503"`` as an HTTP status, which is the
    wrong class for a controller delay test: there a 503 is Mihomo's own
    transport verdict about the leaf.  Unrecognised exceptions are facility
    results and never penalise the leaf.
    """

    name = _delay_error_class_name(exc)
    if name is None:
        return ManualProbeFailureKind.FACILITY
    return _DELAY_ERROR_FAILURE_KINDS[name]


def manual_result_from_delay_error(
    exc: BaseException | None,
    *,
    contract_id: str = MANUAL_ROUTE_CONTRACT_ID,
    completed_at_monotonic: float | None = None,
) -> ManualDataPlaneProbeResult:
    """Build the contract result for a failed controller URL test.

    The exception text is not retained: it can carry the leaf name and the
    contract URL.  Facility results carry a fixed diagnostic token only: the
    probe broker's own reason when the exception carries one, otherwise
    ``controller_error`` or ``probe_error``.
    """

    name = _delay_error_class_name(exc)
    kind = (
        ManualProbeFailureKind.FACILITY
        if name is None
        else _DELAY_ERROR_FAILURE_KINDS[name]
    )
    reason: str | None = None
    if kind is ManualProbeFailureKind.FACILITY:
        carried = getattr(exc, "probe_indeterminate_reason", None)
        carried = carried.value if isinstance(carried, Enum) else carried
        reason = (
            carried
            if isinstance(carried, str)
            and carried in _BROKER_INDETERMINATE_REASONS
            else "controller_error"
            if name is not None
            else "probe_error"
        )
    return ManualDataPlaneProbeResult(
        contract_id=contract_id,
        succeeded=False,
        forced_new_connection=True,
        scheme="https",
        status_code=None,
        failure_kind=kind,
        completed_at_monotonic=completed_at_monotonic,
        probe_indeterminate_reason=reason,
    )


@dataclass(frozen=True, slots=True)
class ManualValidationDecision:
    outcome: ManualValidationOutcome
    switch_before_return: bool
    leaf_failure_confirmed: bool
    backup_required: bool
    current_success_count: int
    current_leaf_failure_count: int
    backup_success_count: int
    reason: str
    identity_diagnostic_considered: bool = False
    # True only while the route contract is enabled and the current leaf's
    # route-contract result is a leaf-transport failure.
    route_contract_leaf_failure: bool = False


def _two_distinct_results(
    values: Iterable[ManualDataPlaneProbeResult],
) -> tuple[ManualDataPlaneProbeResult, ManualDataPlaneProbeResult] | None:
    results = tuple(values)
    if len(results) != 2:
        return None
    if not all(isinstance(value, ManualDataPlaneProbeResult) for value in results):
        return None
    identifiers = tuple(str(value.contract_id or "").strip() for value in results)
    if not all(identifiers) or len(set(identifiers)) != 2:
        return None
    return results[0], results[1]


class RuntimeManualValidation:
    """Pure decision session for one manual FINAL-EXIT data-plane check.

    With ``route_contract_enabled`` the current pair may carry the owning
    fallback group's contract under ``route_contract_id``.  A leaf-transport
    failure of that contract alone then requires backup evidence, as a double
    failure does.  Without the flag, or without that identifier among the
    current results, every decision is the neutral-contract one.
    """

    def __init__(
        self,
        *,
        started_at_monotonic: float | None = None,
        total_timeout_seconds: float = MAX_MANUAL_VALIDATION_SECONDS,
        per_probe_timeout_seconds: float = MAX_MANUAL_PROBE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        route_contract_enabled: bool = False,
        route_contract_id: str = MANUAL_ROUTE_CONTRACT_ID,
    ) -> None:
        self._clock = clock
        self.route_contract_enabled = bool(route_contract_enabled)
        self.route_contract_id = str(route_contract_id or "").strip()
        self.budget = ManualValidationBudget.create(
            started_at_monotonic=(
                clock()
                if started_at_monotonic is None
                else started_at_monotonic
            ),
            total_timeout_seconds=total_timeout_seconds,
            per_probe_timeout_seconds=per_probe_timeout_seconds,
        )

    def probe_timeout_seconds(
        self,
        *,
        requested_seconds: float | None = None,
        now_monotonic: float | None = None,
    ) -> float:
        return self.budget.probe_timeout(
            now_monotonic=(
                self._clock() if now_monotonic is None else now_monotonic
            ),
            requested_seconds=requested_seconds,
        )

    @staticmethod
    def _decision(
        outcome: ManualValidationOutcome,
        *,
        reason: str,
        current_success_count: int = 0,
        current_leaf_failure_count: int = 0,
        backup_success_count: int = 0,
        switch_before_return: bool = False,
        leaf_failure_confirmed: bool = False,
        backup_required: bool = False,
        route_contract_leaf_failure: bool = False,
    ) -> ManualValidationDecision:
        return ManualValidationDecision(
            outcome=outcome,
            switch_before_return=switch_before_return,
            leaf_failure_confirmed=leaf_failure_confirmed,
            backup_required=backup_required,
            current_success_count=current_success_count,
            current_leaf_failure_count=current_leaf_failure_count,
            backup_success_count=backup_success_count,
            reason=reason,
            identity_diagnostic_considered=False,
            route_contract_leaf_failure=route_contract_leaf_failure,
        )

    def _route_contract_result(
        self,
        current: tuple[ManualDataPlaneProbeResult, ManualDataPlaneProbeResult],
    ) -> ManualDataPlaneProbeResult | None:
        if not self.route_contract_enabled or not self.route_contract_id:
            return None
        for result in current:
            if str(result.contract_id or "").strip() == self.route_contract_id:
                return result
        return None

    def _backup_decision(
        self,
        current: tuple[ManualDataPlaneProbeResult, ManualDataPlaneProbeResult],
        backup_results: Iterable[ManualDataPlaneProbeResult],
        *,
        current_success_count: int,
        current_leaf_failure_count: int,
        route_contract_leaf_failure: bool,
        backup_required_reason: str,
        switch_reason: str,
    ) -> ManualValidationDecision:
        backup = _two_distinct_results(backup_results)
        if backup is None:
            return self._decision(
                ManualValidationOutcome.BACKUP_REQUIRED,
                reason=backup_required_reason,
                current_success_count=current_success_count,
                current_leaf_failure_count=current_leaf_failure_count,
                backup_required=True,
                route_contract_leaf_failure=route_contract_leaf_failure,
            )
        current_contracts = {
            str(result.contract_id or "").strip() for result in current
        }
        backup_contracts = {
            str(result.contract_id or "").strip() for result in backup
        }
        if current_contracts != backup_contracts:
            return self._decision(
                ManualValidationOutcome.INVALID_CONTRACT,
                reason="backup_contracts_do_not_match_current_contracts",
                current_success_count=current_success_count,
                current_leaf_failure_count=current_leaf_failure_count,
                route_contract_leaf_failure=route_contract_leaf_failure,
            )
        backup_successes = sum(
            result.is_https_204_success(self.budget) for result in backup
        )
        if backup_successes != 2:
            return self._decision(
                ManualValidationOutcome.BACKUP_UNAVAILABLE,
                reason="backup_did_not_pass_both_https_204_contracts",
                current_success_count=current_success_count,
                current_leaf_failure_count=current_leaf_failure_count,
                backup_success_count=backup_successes,
                route_contract_leaf_failure=route_contract_leaf_failure,
            )
        return self._decision(
            ManualValidationOutcome.SWITCH_BEFORE_RETURN,
            reason=switch_reason,
            current_success_count=current_success_count,
            current_leaf_failure_count=current_leaf_failure_count,
            backup_success_count=2,
            switch_before_return=True,
            leaf_failure_confirmed=True,
            route_contract_leaf_failure=route_contract_leaf_failure,
        )

    def evaluate(
        self,
        current_results: Iterable[ManualDataPlaneProbeResult],
        backup_results: Iterable[ManualDataPlaneProbeResult] = (),
        *,
        now_monotonic: float | None = None,
        identity_diagnostic: Any = None,
    ) -> ManualValidationDecision:
        # Identity/IP collection is intentionally outside the authority path.
        # Accepting the value makes integration explicit while never reading it.
        del identity_diagnostic

        now = self._clock() if now_monotonic is None else float(now_monotonic)
        if (
            not math.isfinite(now)
            or self.budget.total_timeout_seconds <= 0.0
            or now < self.budget.started_at_monotonic
            or now > self.budget.deadline_monotonic
        ):
            return self._decision(
                ManualValidationOutcome.DEADLINE_EXPIRED,
                reason="manual_validation_deadline_expired",
            )

        current = _two_distinct_results(current_results)
        if current is None:
            return self._decision(
                ManualValidationOutcome.INVALID_CONTRACT,
                reason="current_contract_requires_two_distinct_results",
            )

        current_successes = sum(
            result.is_https_204_success(self.budget) for result in current
        )
        current_leaf_failures = sum(
            result.is_leaf_transport_failure(self.budget) for result in current
        )
        route_result = self._route_contract_result(current)
        route_leaf_failure = bool(
            route_result is not None
            and route_result.is_leaf_transport_failure(self.budget)
        )
        if current_successes == 2:
            return self._decision(
                ManualValidationOutcome.CURRENT_HEALTHY,
                reason="current_https_204_contract_passed",
                current_success_count=2,
            )
        if current_successes == 1 and route_leaf_failure:
            # The owning group's own contract failed at transport level on the
            # current leaf: the failure Mihomo demotes on, even though the
            # neutral endpoint still answers.  It is leaf evidence, not proof;
            # a backup must still pass both contracts before any switch.
            return self._backup_decision(
                current,
                backup_results,
                current_success_count=1,
                current_leaf_failure_count=current_leaf_failures,
                route_contract_leaf_failure=True,
                backup_required_reason="route_contract_failed_backup_required",
                switch_reason="route_contract_failed_and_backup_contract_passed",
            )
        if current_successes == 1:
            # One independent success disproves a complete current-route
            # outage. Never switch on the other endpoint's isolated failure.
            return self._decision(
                ManualValidationOutcome.CURRENT_MIXED,
                reason="one_current_contract_succeeded",
                current_success_count=1,
                current_leaf_failure_count=current_leaf_failures,
            )
        if current_leaf_failures != 2:
            return self._decision(
                ManualValidationOutcome.NON_NODE_FAILURE,
                reason="current_failures_are_not_two_leaf_transport_failures",
                current_success_count=current_successes,
                current_leaf_failure_count=current_leaf_failures,
                route_contract_leaf_failure=route_leaf_failure,
            )
        return self._backup_decision(
            current,
            backup_results,
            current_success_count=0,
            current_leaf_failure_count=2,
            route_contract_leaf_failure=route_leaf_failure,
            backup_required_reason="backup_requires_two_distinct_contract_results",
            switch_reason="current_leaf_failed_and_backup_contract_passed",
        )


def evaluate_manual_validation(
    current_results: Iterable[ManualDataPlaneProbeResult],
    backup_results: Iterable[ManualDataPlaneProbeResult] = (),
    *,
    started_at_monotonic: float = 0.0,
    now_monotonic: float = 0.0,
    total_timeout_seconds: float = MAX_MANUAL_VALIDATION_SECONDS,
    per_probe_timeout_seconds: float = MAX_MANUAL_PROBE_SECONDS,
    identity_diagnostic: Any = None,
    route_contract_enabled: bool = False,
    route_contract_id: str = MANUAL_ROUTE_CONTRACT_ID,
) -> ManualValidationDecision:
    """One-shot convenience wrapper with an explicit deterministic clock."""

    session = RuntimeManualValidation(
        started_at_monotonic=started_at_monotonic,
        total_timeout_seconds=total_timeout_seconds,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        clock=lambda: now_monotonic,
        route_contract_enabled=route_contract_enabled,
        route_contract_id=route_contract_id,
    )
    return session.evaluate(
        current_results,
        backup_results,
        now_monotonic=now_monotonic,
        identity_diagnostic=identity_diagnostic,
    )


__all__ = [
    "MANUAL_ROUTE_CONTRACT_ID",
    "MAX_FAST_DIVERT_SECONDS",
    "MAX_MANUAL_PROBE_SECONDS",
    "MAX_MANUAL_VALIDATION_SECONDS",
    "ManualDataPlaneProbeResult",
    "ManualProbeFailureKind",
    "ManualValidationBudget",
    "ManualValidationDecision",
    "ManualValidationOutcome",
    "RuntimeManualValidation",
    "adaptive_manual_probe_timeout",
    "clamp_manual_probe_timeout",
    "evaluate_manual_validation",
    "manual_failure_kind_from_delay_error",
    "manual_result_from_delay_error",
    "normalise_manual_probe_failure",
]
