from __future__ import annotations

import inspect
import io
import math
import socket
from urllib.error import HTTPError

import pytest

import mihomo_api
import runtime_manual_validation
from mihomo_api import (
    MihomoAPIClient,
    MihomoAPIError,
    MihomoDelayStatusMismatch,
    MihomoDelayTestFailed,
    MihomoDelayTimeout,
    MihomoDelayTransportFailed,
)
from runtime_manual_validation import (
    MANUAL_ROUTE_CONTRACT_ID,
    MAX_FAST_DIVERT_SECONDS,
    MAX_MANUAL_PROBE_SECONDS,
    MAX_MANUAL_VALIDATION_SECONDS,
    ManualDataPlaneProbeResult,
    ManualProbeFailureKind,
    ManualValidationBudget,
    ManualValidationOutcome,
    RuntimeManualValidation,
    adaptive_manual_probe_timeout,
    clamp_manual_probe_timeout,
    evaluate_manual_validation,
    manual_failure_kind_from_delay_error,
    manual_result_from_delay_error,
    normalise_manual_probe_failure,
)
from runtime_probe_broker import ProbeIndeterminateReason

ROUTE = MANUAL_ROUTE_CONTRACT_ID
NEUTRAL = "neutral-1"


def success(contract_id: str, **kwargs: object) -> ManualDataPlaneProbeResult:
    values: dict[str, object] = {"status_code": 204}
    values.update(kwargs)
    return ManualDataPlaneProbeResult(
        contract_id=contract_id,
        succeeded=True,
        **values,
    )


def failure(
    contract_id: str,
    kind: ManualProbeFailureKind | str = ManualProbeFailureKind.TIMEOUT,
    **kwargs: object,
) -> ManualDataPlaneProbeResult:
    values: dict[str, object] = {
        "status_code": None,
        "failure_kind": kind,
    }
    values.update(kwargs)
    return ManualDataPlaneProbeResult(
        contract_id=contract_id,
        succeeded=False,
        **values,
    )


def evaluate(
    current: list[ManualDataPlaneProbeResult],
    backup: list[ManualDataPlaneProbeResult] | None = None,
    **kwargs: object,
):
    return evaluate_manual_validation(
        current,
        backup or [],
        started_at_monotonic=10.0,
        now_monotonic=10.5,
        **kwargs,
    )


def test_total_and_per_probe_timeouts_are_clamped_to_health_ceiling() -> None:
    budget = ManualValidationBudget.create(
        started_at_monotonic=10.0,
        total_timeout_seconds=30.0,
        per_probe_timeout_seconds=20.0,
    )

    assert budget.total_timeout_seconds == MAX_MANUAL_VALIDATION_SECONDS
    assert budget.per_probe_timeout_seconds == MAX_MANUAL_PROBE_SECONDS
    assert budget.deadline_monotonic == 15.0
    assert budget.probe_timeout(now_monotonic=10.5) == 4.5
    assert clamp_manual_probe_timeout(8.0) == 5.0
    assert clamp_manual_probe_timeout(1.0, remaining_seconds=0.25) == 0.25


def test_invalid_timeout_inputs_remain_bounded() -> None:
    assert clamp_manual_probe_timeout(-1.0) == 0.0
    assert clamp_manual_probe_timeout(math.inf) == 5.0
    assert clamp_manual_probe_timeout(math.nan) == 5.0


def test_adaptive_probe_timeout_preserves_fast_target_without_misclassifying_slow_leaf() -> None:
    assert MAX_FAST_DIVERT_SECONDS == 1.5
    assert adaptive_manual_probe_timeout(
        100,
        configured_timeout_seconds=8,
    ) == 0.5
    assert adaptive_manual_probe_timeout(
        400,
        configured_timeout_seconds=8,
    ) == 1.4
    assert adaptive_manual_probe_timeout(
        900,
        configured_timeout_seconds=8,
    ) == 2.9
    assert adaptive_manual_probe_timeout(
        2500,
        configured_timeout_seconds=8,
    ) == 5.0
    assert adaptive_manual_probe_timeout(
        None,
        configured_timeout_seconds=4,
    ) == 2.0
    with pytest.raises(ValueError, match="finite"):
        ManualValidationBudget.create(started_at_monotonic=math.nan)


def test_two_current_https_204_results_are_healthy_without_switch() -> None:
    decision = evaluate([success("a"), success("b")])

    assert decision.outcome is ManualValidationOutcome.CURRENT_HEALTHY
    assert decision.current_success_count == 2
    assert not decision.switch_before_return
    assert not decision.leaf_failure_confirmed


@pytest.mark.parametrize(
    "failed_kind",
    [
        ManualProbeFailureKind.TIMEOUT,
        ManualProbeFailureKind.CONNECT,
        ManualProbeFailureKind.TLS_TRANSPORT,
    ],
)
def test_one_success_and_one_failure_never_switches(
    failed_kind: ManualProbeFailureKind,
) -> None:
    decision = evaluate(
        [success("a"), failure("b", failed_kind)],
        [success("a"), success("b")],
    )

    assert decision.outcome is ManualValidationOutcome.CURRENT_MIXED
    assert decision.current_success_count == 1
    assert not decision.switch_before_return


def test_double_transport_failure_requires_backup_evidence() -> None:
    decision = evaluate([failure("a"), failure("b")])

    assert decision.outcome is ManualValidationOutcome.BACKUP_REQUIRED
    assert decision.backup_required
    assert not decision.switch_before_return


def test_double_transport_failure_and_double_backup_success_switches_before_return() -> None:
    decision = evaluate(
        [
            failure("a", ManualProbeFailureKind.CONNECT),
            failure("b", ManualProbeFailureKind.RESET),
        ],
        [success("a"), success("b")],
    )

    assert decision.outcome is ManualValidationOutcome.SWITCH_BEFORE_RETURN
    assert decision.switch_before_return
    assert decision.leaf_failure_confirmed
    assert decision.backup_success_count == 2


@pytest.mark.parametrize(
    "backup",
    [
        [success("a"), failure("b")],
        [failure("a"), failure("b")],
        [success("a")],
    ],
)
def test_backup_must_pass_both_contracts(
    backup: list[ManualDataPlaneProbeResult],
) -> None:
    decision = evaluate([failure("a"), failure("b")], backup)

    assert not decision.switch_before_return
    assert not decision.leaf_failure_confirmed


def test_backup_contracts_must_match_current_contracts() -> None:
    decision = evaluate(
        [failure("a"), failure("b")],
        [success("c"), success("d")],
    )

    assert decision.outcome is ManualValidationOutcome.INVALID_CONTRACT
    assert not decision.switch_before_return


@pytest.mark.parametrize(
    "current",
    [
        [
            ManualDataPlaneProbeResult("a", False, status_code=503),
            ManualDataPlaneProbeResult("b", False, status_code=500),
        ],
        [
            failure("a", ManualProbeFailureKind.CERTIFICATE_TARGET),
            failure("b", ManualProbeFailureKind.CERTIFICATE_TARGET),
        ],
        [
            failure("a", ManualProbeFailureKind.TLS_TRANSPORT, certificate_target_error=True),
            failure("b", ManualProbeFailureKind.TLS_TRANSPORT, certificate_target_error=True),
        ],
    ],
)
def test_http_status_and_certificate_target_errors_never_confirm_leaf_failure(
    current: list[ManualDataPlaneProbeResult],
) -> None:
    decision = evaluate(current, [success("a"), success("b")])

    assert decision.outcome is ManualValidationOutcome.NON_NODE_FAILURE
    assert decision.current_leaf_failure_count == 0
    assert not decision.switch_before_return


@pytest.mark.parametrize(
    "invalid_success",
    [
        ManualDataPlaneProbeResult("a", True),
        success("a", forced_new_connection=False),
        success("a", scheme="http"),
        success("a", status_code=200),
        success("a", failure_kind=ManualProbeFailureKind.CONTENT),
    ],
)
def test_success_requires_forced_new_connection_https_204_contract(
    invalid_success: ManualDataPlaneProbeResult,
) -> None:
    decision = evaluate([invalid_success, success("b")])

    assert not decision.switch_before_return
    assert decision.current_success_count == 1


def test_duplicate_or_missing_contract_ids_are_invalid() -> None:
    duplicate = evaluate([success("a"), success("a")])
    missing = evaluate([success(""), success("b")])

    assert duplicate.outcome is ManualValidationOutcome.INVALID_CONTRACT
    assert missing.outcome is ManualValidationOutcome.INVALID_CONTRACT


def test_explicit_factories_produce_auditable_contract_results() -> None:
    passed = ManualDataPlaneProbeResult.https_204("a")
    failed = ManualDataPlaneProbeResult.transport_failure(
        "b",
        ManualProbeFailureKind.CONNECT,
    )
    budget = ManualValidationBudget.create(started_at_monotonic=10.0)

    assert passed.is_https_204_success(budget)
    assert failed.is_leaf_transport_failure(budget)


def test_late_results_and_late_decision_cannot_authorize_switch() -> None:
    late_result = evaluate(
        [
            failure("a", completed_at_monotonic=15.1),
            failure("b", completed_at_monotonic=15.1),
        ],
        [success("a"), success("b")],
    )
    late_decision = evaluate_manual_validation(
        [failure("a"), failure("b")],
        [success("a"), success("b")],
        started_at_monotonic=10.0,
        now_monotonic=15.1,
    )

    assert late_result.outcome is ManualValidationOutcome.NON_NODE_FAILURE
    assert not late_result.switch_before_return
    assert late_decision.outcome is ManualValidationOutcome.DEADLINE_EXPIRED
    assert not late_decision.switch_before_return


def test_zero_budget_or_pre_session_decision_cannot_authorize_switch() -> None:
    current = [failure("a"), failure("b")]
    backup = [success("a"), success("b")]

    zero_budget = evaluate_manual_validation(
        current,
        backup,
        started_at_monotonic=10.0,
        now_monotonic=10.0,
        total_timeout_seconds=0.0,
    )
    before_start = evaluate_manual_validation(
        current,
        backup,
        started_at_monotonic=10.0,
        now_monotonic=9.9,
    )

    assert zero_budget.outcome is ManualValidationOutcome.DEADLINE_EXPIRED
    assert before_start.outcome is ManualValidationOutcome.DEADLINE_EXPIRED


def test_identity_diagnostic_is_ignored_by_the_authority_path() -> None:
    current = [failure("a"), failure("b")]
    backup = [success("a"), success("b")]

    without_identity = evaluate(current, backup)
    with_identity_success = evaluate(
        current,
        backup,
        identity_diagnostic={"succeeded": True, "country": "ignored"},
    )
    with_identity_failure = evaluate(
        current,
        backup,
        identity_diagnostic=RuntimeError("blocked for ten seconds"),
    )

    assert with_identity_success == without_identity
    assert with_identity_failure == without_identity
    assert not without_identity.identity_diagnostic_considered


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("certificate verify failed: hostname mismatch", ManualProbeFailureKind.CERTIFICATE_TARGET),
        ("HTTP status 503", ManualProbeFailureKind.HTTP_STATUS),
        ("context deadline exceeded", ManualProbeFailureKind.TIMEOUT),
        ("connection reset by peer", ManualProbeFailureKind.RESET),
        ("TLS handshake timeout", ManualProbeFailureKind.TIMEOUT),
        ("TLS handshake failed", ManualProbeFailureKind.TLS_TRANSPORT),
    ],
)
def test_failure_normalisation_keeps_certificate_and_http_out_of_transport(
    text: str,
    expected: ManualProbeFailureKind,
) -> None:
    assert normalise_manual_probe_failure(text) is expected


def test_session_exposes_only_remaining_clamped_probe_budget() -> None:
    session = RuntimeManualValidation(
        started_at_monotonic=10.0,
        total_timeout_seconds=99.0,
        per_probe_timeout_seconds=99.0,
        clock=lambda: 10.75,
    )

    assert session.probe_timeout_seconds() == 4.25
    assert session.probe_timeout_seconds(requested_seconds=99.0) == 4.25
    assert session.probe_timeout_seconds(now_monotonic=15.0) == 0.0


# --- Route contract (contract 0 = owning fallback group's contract) --------


def route_failure(
    exc: BaseException,
    **kwargs: object,
) -> ManualDataPlaneProbeResult:
    return manual_result_from_delay_error(exc, contract_id=ROUTE, **kwargs)


def route_evaluate(
    current: list[ManualDataPlaneProbeResult],
    backup: list[ManualDataPlaneProbeResult] | None = None,
    **kwargs: object,
):
    kwargs.setdefault("route_contract_enabled", True)
    return evaluate(current, backup, **kwargs)


def zombie_current() -> list[ManualDataPlaneProbeResult]:
    # 09-25 shape: the neutral endpoint answers 204 while the owning group's
    # contract fails at transport level on the same leaf.
    return [
        route_failure(MihomoDelayTransportFailed("delay test failed")),
        success(NEUTRAL),
    ]


def test_route_contract_flag_off_keeps_single_route_failure_mixed() -> None:
    backup = [success(ROUTE), success(NEUTRAL)]

    default = evaluate(zombie_current(), backup)
    explicit_off = evaluate(
        zombie_current(),
        backup,
        route_contract_enabled=False,
    )

    assert default == explicit_off
    assert default.outcome is ManualValidationOutcome.CURRENT_MIXED
    assert default.reason == "one_current_contract_succeeded"
    assert not default.switch_before_return
    assert not default.backup_required
    assert not default.route_contract_leaf_failure


_LEGACY_SCENARIOS = (
    ([success("a"), success("b")], []),
    ([success("a"), failure("b")], [success("a"), success("b")]),
    ([success("a"), failure("b", ManualProbeFailureKind.CONNECT)], []),
    ([failure("a"), failure("b")], []),
    ([failure("a", ManualProbeFailureKind.CONNECT), failure("b", ManualProbeFailureKind.RESET)], [success("a"), success("b")]),
    ([failure("a"), failure("b")], [success("a"), failure("b")]),
    ([failure("a"), failure("b")], [success("a")]),
    ([failure("a"), failure("b")], [success("c"), success("d")]),
    ([ManualDataPlaneProbeResult("a", False, status_code=503), ManualDataPlaneProbeResult("b", False, status_code=500)], [success("a"), success("b")]),
    ([failure("a", ManualProbeFailureKind.CERTIFICATE_TARGET), failure("b")], [success("a"), success("b")]),
    ([failure("a", completed_at_monotonic=15.1), failure("b", completed_at_monotonic=15.1)], [success("a"), success("b")]),
    ([success("a"), success("a")], []),
    ([success(""), success("b")], []),
    ([failure("a", ManualProbeFailureKind.FACILITY), success("b")], []),
)


@pytest.mark.parametrize(("current", "backup"), _LEGACY_SCENARIOS)
def test_route_contract_flag_is_inert_without_a_route_contract_result(
    current: list[ManualDataPlaneProbeResult],
    backup: list[ManualDataPlaneProbeResult],
) -> None:
    legacy = evaluate(current, backup)

    assert evaluate(current, backup, route_contract_enabled=False) == legacy
    # The flag only reads the result carrying the route-contract identifier.
    assert route_evaluate(current, backup) == legacy
    assert not legacy.route_contract_leaf_failure


def test_zombie_route_contract_transport_failure_requires_backup() -> None:
    decision = route_evaluate(zombie_current())

    assert decision.outcome is ManualValidationOutcome.BACKUP_REQUIRED
    assert decision.reason == "route_contract_failed_backup_required"
    assert decision.backup_required
    assert decision.route_contract_leaf_failure
    assert decision.current_success_count == 1
    assert decision.current_leaf_failure_count == 1
    assert not decision.switch_before_return
    assert not decision.leaf_failure_confirmed


def test_zombie_switches_before_return_when_backup_passes_both_contracts() -> None:
    decision = route_evaluate(
        zombie_current(),
        [success(ROUTE), success(NEUTRAL)],
    )

    assert decision.outcome is ManualValidationOutcome.SWITCH_BEFORE_RETURN
    assert decision.reason == "route_contract_failed_and_backup_contract_passed"
    assert decision.switch_before_return
    assert decision.leaf_failure_confirmed
    assert decision.backup_success_count == 2
    assert decision.current_success_count == 1
    assert decision.route_contract_leaf_failure


@pytest.mark.parametrize(
    ("backup", "expected"),
    [
        (
            [success(ROUTE), failure(NEUTRAL)],
            ManualValidationOutcome.BACKUP_UNAVAILABLE,
        ),
        (
            [route_failure(MihomoDelayTransportFailed("x")), success(NEUTRAL)],
            ManualValidationOutcome.BACKUP_UNAVAILABLE,
        ),
        (
            [route_failure(MihomoDelayStatusMismatch("x")), success(NEUTRAL)],
            ManualValidationOutcome.BACKUP_UNAVAILABLE,
        ),
        ([success(ROUTE)], ManualValidationOutcome.BACKUP_REQUIRED),
        (
            [success("neutral-0"), success(NEUTRAL)],
            ManualValidationOutcome.INVALID_CONTRACT,
        ),
    ],
)
def test_zombie_backup_must_pass_both_route_and_neutral_contracts(
    backup: list[ManualDataPlaneProbeResult],
    expected: ManualValidationOutcome,
) -> None:
    decision = route_evaluate(zombie_current(), backup)

    assert decision.outcome is expected
    assert not decision.switch_before_return
    assert not decision.leaf_failure_confirmed


@pytest.mark.parametrize(
    "neutral",
    [
        success(NEUTRAL),
        failure(NEUTRAL, ManualProbeFailureKind.CONNECT),
        failure(NEUTRAL, ManualProbeFailureKind.TIMEOUT),
    ],
)
def test_route_contract_status_mismatch_429_never_switches(
    neutral: ManualDataPlaneProbeResult,
) -> None:
    decision = route_evaluate(
        [route_failure(MihomoDelayStatusMismatch("expected status")), neutral],
        [success(ROUTE), success(NEUTRAL)],
    )

    assert decision.outcome in {
        ManualValidationOutcome.CURRENT_MIXED,
        ManualValidationOutcome.NON_NODE_FAILURE,
    }
    assert not decision.switch_before_return
    assert not decision.backup_required
    assert not decision.leaf_failure_confirmed
    assert not decision.route_contract_leaf_failure


@pytest.mark.parametrize(
    "exc",
    [
        MihomoAPIError("GET /proxies/x/delay failed: timed out"),
        MihomoAPIError("GET /proxies/x/delay failed: HTTP 503 Service Unavailable"),
        RuntimeError("proxy delay deadline expired"),
        TimeoutError("client timed out"),
        MihomoDelayTestFailed("unattributed negative verdict"),
    ],
)
@pytest.mark.parametrize(
    "neutral",
    [success(NEUTRAL), failure(NEUTRAL, ManualProbeFailureKind.CONNECT)],
)
def test_route_contract_facility_or_unattributed_error_never_switches(
    exc: BaseException,
    neutral: ManualDataPlaneProbeResult,
) -> None:
    decision = route_evaluate(
        [route_failure(exc), neutral],
        [success(ROUTE), success(NEUTRAL)],
    )

    assert decision.outcome in {
        ManualValidationOutcome.CURRENT_MIXED,
        ManualValidationOutcome.NON_NODE_FAILURE,
    }
    assert not decision.switch_before_return
    assert not decision.backup_required
    assert not decision.route_contract_leaf_failure


def test_route_contract_pass_with_neutral_failure_stays_mixed() -> None:
    decision = route_evaluate(
        [success(ROUTE), failure(NEUTRAL, ManualProbeFailureKind.CONNECT)],
        [success(ROUTE), success(NEUTRAL)],
    )

    assert decision.outcome is ManualValidationOutcome.CURRENT_MIXED
    assert decision.reason == "one_current_contract_succeeded"
    assert not decision.switch_before_return


def test_route_contract_healthy_pair_is_healthy() -> None:
    decision = route_evaluate([success(ROUTE), success(NEUTRAL)])

    assert decision.outcome is ManualValidationOutcome.CURRENT_HEALTHY
    assert not decision.route_contract_leaf_failure


def test_route_contract_double_failure_keeps_existing_backup_path() -> None:
    current = [
        route_failure(MihomoDelayTimeout("Timeout")),
        failure(NEUTRAL, ManualProbeFailureKind.CONNECT),
    ]

    required = route_evaluate(current)
    switched = route_evaluate(current, [success(ROUTE), success(NEUTRAL)])

    assert required.outcome is ManualValidationOutcome.BACKUP_REQUIRED
    assert required.reason == "backup_requires_two_distinct_contract_results"
    assert required.current_leaf_failure_count == 2
    assert switched.outcome is ManualValidationOutcome.SWITCH_BEFORE_RETURN
    assert switched.reason == "current_leaf_failed_and_backup_contract_passed"
    assert switched.route_contract_leaf_failure


@pytest.mark.parametrize(
    "neutral",
    [
        failure(NEUTRAL, ManualProbeFailureKind.HTTP_STATUS),
        failure(NEUTRAL, ManualProbeFailureKind.FACILITY),
        ManualDataPlaneProbeResult(NEUTRAL, False, status_code=503),
    ],
)
def test_route_failure_with_non_transport_neutral_failure_is_not_node_evidence(
    neutral: ManualDataPlaneProbeResult,
) -> None:
    decision = route_evaluate(
        [route_failure(MihomoDelayTransportFailed("x")), neutral],
        [success(ROUTE), success(NEUTRAL)],
    )

    assert decision.outcome is ManualValidationOutcome.NON_NODE_FAILURE
    assert decision.route_contract_leaf_failure
    assert not decision.switch_before_return


def test_late_route_contract_failure_cannot_require_backup() -> None:
    decision = route_evaluate(
        [
            route_failure(
                MihomoDelayTransportFailed("x"),
                completed_at_monotonic=15.1,
            ),
            success(NEUTRAL),
        ],
        [success(ROUTE), success(NEUTRAL)],
    )

    assert decision.outcome is ManualValidationOutcome.CURRENT_MIXED
    assert not decision.backup_required
    assert not decision.route_contract_leaf_failure


def test_route_contract_identifier_is_explicit() -> None:
    current = [
        manual_result_from_delay_error(
            MihomoDelayTransportFailed("x"),
            contract_id="group-contract",
        ),
        success(NEUTRAL),
    ]

    other_id = route_evaluate(current)
    matched = route_evaluate(current, route_contract_id=" group-contract ")
    blank_id = route_evaluate(current, route_contract_id="")

    assert other_id.outcome is ManualValidationOutcome.CURRENT_MIXED
    assert matched.outcome is ManualValidationOutcome.BACKUP_REQUIRED
    assert blank_id.outcome is ManualValidationOutcome.CURRENT_MIXED


@pytest.mark.parametrize("route_only", [False, True])
def test_black_hole_current_with_slow_backup_respects_deadline(
    route_only: bool,
) -> None:
    now = [10.0]
    session = RuntimeManualValidation(
        started_at_monotonic=10.0,
        total_timeout_seconds=5.0,
        per_probe_timeout_seconds=2.5,
        clock=lambda: now[0],
        route_contract_enabled=True,
    )
    # The route contract gets at most the per-probe share, and never more
    # than what is left of the click's budget.
    assert session.probe_timeout_seconds() == 2.5
    assert session.probe_timeout_seconds(
        requested_seconds=5.0,
        now_monotonic=13.9,
    ) == pytest.approx(1.1)

    # Black hole: Mihomo's URL test answers 504 at its own timeout.
    now[0] = 12.5
    current = [
        route_failure(
            MihomoDelayTimeout("Timeout"),
            completed_at_monotonic=12.5,
        ),
        (
            success(NEUTRAL, completed_at_monotonic=11.0)
            if route_only
            else failure(NEUTRAL, completed_at_monotonic=12.5)
        ),
    ]
    required = session.evaluate(current)
    assert required.outcome is ManualValidationOutcome.BACKUP_REQUIRED
    assert session.probe_timeout_seconds(requested_seconds=5.0) == 2.5

    late_backup = [
        success(ROUTE, completed_at_monotonic=15.2),
        success(NEUTRAL, completed_at_monotonic=15.2),
    ]
    # A slow backup whose sockets finished after the deadline is no evidence,
    # and a decision taken after the deadline cannot authorise a switch.
    now[0] = 14.9
    stamped_late = session.evaluate(current, late_backup)
    now[0] = 15.2
    decided_late = session.evaluate(current, late_backup)
    assert session.probe_timeout_seconds() == 0.0

    assert stamped_late.outcome is ManualValidationOutcome.BACKUP_UNAVAILABLE
    assert not stamped_late.switch_before_return
    assert decided_late.outcome is ManualValidationOutcome.DEADLINE_EXPIRED
    assert not decided_late.switch_before_return

    now[0] = 14.8
    timely = session.evaluate(
        current,
        [
            success(ROUTE, completed_at_monotonic=14.7),
            success(NEUTRAL, completed_at_monotonic=14.7),
        ],
    )
    assert timely.outcome is ManualValidationOutcome.SWITCH_BEFORE_RETURN


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (MihomoDelayStatusMismatch("x"), ManualProbeFailureKind.HTTP_STATUS),
        (MihomoDelayTimeout("x"), ManualProbeFailureKind.TIMEOUT),
        (MihomoDelayTransportFailed("x"), ManualProbeFailureKind.CONNECT),
        (MihomoDelayTestFailed("x"), ManualProbeFailureKind.FACILITY),
        (MihomoAPIError("x"), ManualProbeFailureKind.FACILITY),
        (type("NestedTimeout", (MihomoDelayTimeout,), {})("x"), ManualProbeFailureKind.TIMEOUT),
        (RuntimeError("x"), ManualProbeFailureKind.FACILITY),
        (TimeoutError("timed out"), ManualProbeFailureKind.FACILITY),
        (None, ManualProbeFailureKind.FACILITY),
    ],
)
def test_delay_errors_map_by_exception_class(
    exc: BaseException | None,
    expected: ManualProbeFailureKind,
) -> None:
    result = manual_result_from_delay_error(exc, completed_at_monotonic=10.2)
    budget = ManualValidationBudget.create(started_at_monotonic=10.0)

    assert manual_failure_kind_from_delay_error(exc) is expected
    assert result.contract_id == MANUAL_ROUTE_CONTRACT_ID
    assert result.normalised_failure_kind is expected
    assert not result.is_https_204_success(budget)
    assert result.is_leaf_transport_failure(budget) is (
        expected in {ManualProbeFailureKind.CONNECT, ManualProbeFailureKind.TIMEOUT}
    )
    assert (result.probe_indeterminate_reason is not None) is (
        expected is ManualProbeFailureKind.FACILITY
    )


def test_delay_error_mapping_does_not_read_the_error_text() -> None:
    text = "GET /proxies/node/delay failed: HTTP 503 Service Unavailable"

    # The text normaliser's HTTP-status reading is why the typed map exists.
    assert normalise_manual_probe_failure(text) is ManualProbeFailureKind.HTTP_STATUS
    assert (
        manual_failure_kind_from_delay_error(MihomoDelayTransportFailed(text))
        is ManualProbeFailureKind.CONNECT
    )
    assert normalise_manual_probe_failure("timed out") is ManualProbeFailureKind.TIMEOUT
    assert (
        manual_failure_kind_from_delay_error(MihomoAPIError("timed out"))
        is ManualProbeFailureKind.FACILITY
    )


def test_delay_error_result_retains_no_raw_destination_or_leaf_text() -> None:
    secret = "zombie-leaf.example.net"
    results = [
        manual_result_from_delay_error(
            error_type(f"GET /proxies/{secret}/delay failed: HTTP 503 via https://{secret}/"),
        )
        for error_type in (
            MihomoDelayTransportFailed,
            MihomoDelayTimeout,
            MihomoDelayStatusMismatch,
            MihomoDelayTestFailed,
            MihomoAPIError,
            RuntimeError,
        )
    ]

    for result in results:
        assert secret not in repr(result)
        assert secret not in str(result)
        assert result.probe_indeterminate_reason in {
            None,
            "controller_error",
            "probe_error",
        }


@pytest.mark.parametrize(
    ("status", "body", "error_type", "expected"),
    [
        (503, b"{}", MihomoDelayTransportFailed, ManualProbeFailureKind.CONNECT),
        (
            503,
            b'{"message":"An error occurred in the delay test"}',
            MihomoDelayTransportFailed,
            ManualProbeFailureKind.CONNECT,
        ),
        (504, b'{"message":"Timeout"}', MihomoDelayTimeout, ManualProbeFailureKind.TIMEOUT),
        (503, b"<html></html>", MihomoAPIError, ManualProbeFailureKind.FACILITY),
        (504, b"<html></html>", MihomoAPIError, ManualProbeFailureKind.FACILITY),
        (500, b"{}", MihomoAPIError, ManualProbeFailureKind.FACILITY),
    ],
)
def test_controller_http_failures_map_by_class(
    status: int,
    body: bytes,
    error_type: type[MihomoAPIError],
    expected: ManualProbeFailureKind,
) -> None:
    exc = mihomo_api._http_failure("GET", "/proxies/n/delay", status, "failure", body)

    assert type(exc) is error_type
    assert manual_failure_kind_from_delay_error(exc) is expected
    assert manual_result_from_delay_error(exc).normalised_failure_kind is expected


def test_delay_error_table_names_real_controller_classes() -> None:
    # The module dispatches by class name only; a rename in the controller
    # client must fail here instead of silently mapping a 503 to FACILITY.
    table = runtime_manual_validation._DELAY_ERROR_FAILURE_KINDS

    for name, kind in table.items():
        klass = getattr(mihomo_api, name, None)
        assert isinstance(klass, type), name
        assert klass.__name__ == name
        assert issubclass(klass, MihomoAPIError)
        # Most-specific first: each class resolves to its own entry.
        assert manual_failure_kind_from_delay_error(klass("x")) is kind

    verdict_classes = {
        name
        for name, value in vars(mihomo_api).items()
        if inspect.isclass(value)
        and issubclass(value, MihomoDelayTestFailed)
        and value.__module__ == mihomo_api.__name__
    }
    # Every native URL-test verdict the client can raise has a decided kind.
    assert verdict_classes <= set(table)
    assert {
        name for name, kind in table.items()
        if kind in {ManualProbeFailureKind.CONNECT, ManualProbeFailureKind.TIMEOUT}
    } == {"MihomoDelayTransportFailed", "MihomoDelayTimeout"}


class _DelayOpener:
    """urlopen stand-in: queued bodies are returned, exceptions raised."""

    def __init__(self, *responses: bytes | BaseException) -> None:
        self.responses = list(responses)

    def __call__(self, request: object, timeout: float | None = None) -> io.BytesIO:
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return io.BytesIO(response)


def _delay_http_error(status: int, body: bytes) -> HTTPError:
    return HTTPError(
        "http://127.0.0.1:9090/proxies/zombie-leaf/delay",
        status,
        "failure",
        {},  # type: ignore[arg-type]
        io.BytesIO(body),
    )


@pytest.mark.parametrize(
    ("responses", "expected"),
    [
        ((_delay_http_error(503, b"{}"),), ManualProbeFailureKind.CONNECT),
        (
            (_delay_http_error(504, b'{"message":"Timeout"}'),),
            ManualProbeFailureKind.TIMEOUT,
        ),
        (
            (
                b'{"delay":120}',
                b'{"extra":{"https://contract.invalid/generate_204":{"alive":false}}}',
            ),
            ManualProbeFailureKind.HTTP_STATUS,
        ),
        ((_delay_http_error(503, b"<html></html>"),), ManualProbeFailureKind.FACILITY),
        ((socket.timeout("timed out"),), ManualProbeFailureKind.FACILITY),
        ((b'{"delay":120}', b"{}"), ManualProbeFailureKind.FACILITY),
    ],
)
def test_real_controller_client_errors_map_to_route_contract_results(
    responses: tuple[bytes | BaseException, ...],
    expected: ManualProbeFailureKind,
) -> None:
    client = MihomoAPIClient(
        base_url="http://127.0.0.1:9090",
        opener=_DelayOpener(*responses),
    )

    with pytest.raises(MihomoAPIError) as caught:
        client.proxy_delay(
            "zombie-leaf",
            url="https://contract.invalid/generate_204",
            timeout_ms=2500,
            expected_status=204,
            request_timeout=2.9,
            verify_expected_status=True,
        )
    result = manual_result_from_delay_error(caught.value, completed_at_monotonic=10.3)
    decision = route_evaluate([result, success(NEUTRAL)], [success(ROUTE), success(NEUTRAL)])

    assert result.normalised_failure_kind is expected
    assert "zombie-leaf" not in repr(result)
    # Only the controller's own transport verdicts reach the switch path.
    assert decision.switch_before_return is (
        expected in {ManualProbeFailureKind.CONNECT, ManualProbeFailureKind.TIMEOUT}
    )


@pytest.mark.parametrize("reason", list(ProbeIndeterminateReason))
def test_broker_indeterminate_reason_survives_the_delay_error_mapping(
    reason: ProbeIndeterminateReason,
) -> None:
    exc = RuntimeError("runtime data-plane probe is indeterminate")
    exc.probe_indeterminate_reason = reason.value  # type: ignore[attr-defined]
    enum_carrier = MihomoAPIError("controller")
    enum_carrier.probe_indeterminate_reason = reason  # type: ignore[attr-defined]

    for carrier in (exc, enum_carrier):
        result = manual_result_from_delay_error(carrier)
        assert result.normalised_failure_kind is ManualProbeFailureKind.FACILITY
        assert result.probe_indeterminate_reason == reason.value
    assert runtime_manual_validation._BROKER_INDETERMINATE_REASONS == {
        value.value for value in ProbeIndeterminateReason
    }


def test_unknown_carried_reason_is_replaced_by_a_fixed_token() -> None:
    leaky = RuntimeError("x")
    leaky.probe_indeterminate_reason = "zombie-leaf.example.net"  # type: ignore[attr-defined]
    typed = MihomoDelayTransportFailed("x")
    typed.probe_indeterminate_reason = "queue_timeout"  # type: ignore[attr-defined]

    assert manual_result_from_delay_error(leaky).probe_indeterminate_reason == "probe_error"
    # A leaf verdict is never downgraded by a carried broker reason.
    typed_result = manual_result_from_delay_error(typed)
    assert typed_result.normalised_failure_kind is ManualProbeFailureKind.CONNECT
    assert typed_result.probe_indeterminate_reason is None


def test_session_exposes_route_contract_configuration() -> None:
    default = RuntimeManualValidation(started_at_monotonic=10.0, clock=lambda: 10.0)
    enabled = RuntimeManualValidation(
        started_at_monotonic=10.0,
        clock=lambda: 10.0,
        route_contract_enabled=True,
    )

    assert not default.route_contract_enabled
    assert enabled.route_contract_enabled
    assert enabled.route_contract_id == MANUAL_ROUTE_CONTRACT_ID == "route-0"
