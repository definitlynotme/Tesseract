"""Overview request deadlines, retry ownership and presentation diagnostics."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import desktop_runtime as runtime_module
from desktop_runtime import DesktopRuntime
from runtime_manual_validation import ManualDataPlaneProbeResult, RuntimeManualValidation
from runtime_probe_broker import (
    ProbeDisposition, ProbeExecutionResult, ProbeIndeterminateReason, ProbeTiming,
)
from tests.test_manual_identity_epoch import _runtime as identity_runtime
from tests.test_manual_validation_execution_budget import _manual_runtime


@pytest.fixture
def request_runtime(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(runtime_module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_uses_early_single_coordinator = lambda: True
    runtime.logger = None
    return runtime, clock


def test_expired_click_deadline_never_starts_validation_or_extends_request(request_runtime):
    runtime, clock = request_runtime
    runtime._check_current_exit_identity_early_once = lambda **kwargs: pytest.fail("expired work started")
    result = runtime.check_current_exit_identity(
        proxy_url=None, request_id="expired", timeout_seconds=8, deadline_monotonic=99.9,
    )
    assert result["error"] == "validation_timeout"
    assert result["diagnostics"]["overview_failure_reason"] == "validation_timeout"
    assert result["completed_monotonic"] == clock.now


@pytest.mark.parametrize("reason,retried,expected", [
    ("queue_timeout", True, "probe_busy"),
    ("started_timeout", True, "probe_busy"),
    ("execution_timeout", False, "manual_probe_not_completed"),
    ("cancelled", False, "validation_cancelled"),
    ("broker_stopped", False, "validation_cancelled"),
    ("broker_poisoned", False, "probe_unavailable"),
])
def test_only_short_lived_facility_contention_retries_same_deadline(
    request_runtime, reason, retried, expected,
):
    runtime, clock = request_runtime
    calls, progress = [], []

    def attempt(**kwargs):
        calls.append(kwargs)
        clock.now += 0.2
        return {
            "state": "data_plane_indeterminate", "identity": {},
            "freshness": {"reason": "manual_probe_not_completed"},
            "diagnostics": {"probe_indeterminate_reasons": [reason]},
        }

    runtime._check_current_exit_identity_early_once = attempt
    result = runtime.check_current_exit_identity(
        proxy_url=None, request_id="busy", deadline_monotonic=104.0,
        progress_callback=progress.append,
    )
    assert len(calls) == (3 if retried else 1)
    assert {call["deadline_monotonic"] for call in calls} == {104.0}
    assert result["diagnostics"]["overview_failure_reason"] == expected
    assert result["completed_monotonic"] < 104.0
    assert [item["attempt"] for item in progress] == ([2, 3] if retried else [])


@pytest.mark.parametrize("change", ["intent", "stop", "epoch"])
def test_changed_request_owner_never_retries_even_when_broker_was_busy(request_runtime, change):
    runtime, _clock = request_runtime
    calls = []
    stop = SimpleNamespace(is_set=lambda: False)
    runtime.stop_event = stop

    def attempt(**kwargs):
        calls.append(kwargs)
        if change == "intent":
            runtime._runtime_selector_intent_revision = 1
        elif change == "epoch":
            runtime._core_generation = 1
        else:
            stop.is_set = lambda: True
        return {"state": "data_plane_indeterminate", "diagnostics": {
            "probe_indeterminate_reasons": ["queue_timeout"],
        }}

    runtime._check_current_exit_identity_early_once = attempt
    runtime.check_current_exit_identity(proxy_url=None, request_id="superseded")
    assert len(calls) == 1


def test_inadequate_remaining_budget_does_not_launch_another_attempt(request_runtime):
    runtime, clock = request_runtime
    calls = []

    def attempt(**kwargs):
        calls.append(kwargs)
        clock.now = 107.2
        return {"state": "data_plane_indeterminate", "diagnostics": {
            "probe_indeterminate_reasons": ["queue_timeout"],
        }}

    runtime._check_current_exit_identity_early_once = attempt
    result = runtime.check_current_exit_identity(proxy_url=None, request_id="no-retry")
    assert len(calls) == 1
    assert result["completed_monotonic"] == 107.2


def test_temporary_queue_contention_can_finish_on_the_second_attempt(request_runtime):
    runtime, clock = request_runtime
    attempts = []

    def attempt(**kwargs):
        attempts.append(kwargs["deadline_monotonic"])
        clock.now += 0.3
        if len(attempts) == 1:
            return {"state": "data_plane_indeterminate", "diagnostics": {
                "probe_indeterminate_reasons": ["queue_timeout"],
            }}
        return {"state": "complete", "identity": {"ipv4": {"ip": "8.8.8.8"}}}

    runtime._check_current_exit_identity_early_once = attempt
    result = runtime.check_current_exit_identity(
        proxy_url=None, request_id="recovered", deadline_monotonic=102.0,
    )
    assert attempts == [102.0, 102.0]
    assert result["state"] == "complete"
    assert result["diagnostics"]["overview_validation_attempts"] == 2
    assert "overview_failure_reason" not in result["diagnostics"]


def test_finished_timestamp_is_recorded_before_delivery_and_late_success_is_not_accepted(request_runtime):
    runtime, clock = request_runtime

    def late(**_kwargs):
        clock.now = 108.1
        return {"state": "complete", "identity": {"ipv4": {"ip": "8.8.8.8"}}}

    runtime._check_current_exit_identity_early_once = late
    result = runtime.check_current_exit_identity(proxy_url=None, request_id="late")
    assert result["completed_monotonic"] == 108.1
    assert result["identity"] == {}
    assert result["error"] == "validation_timeout"


@pytest.mark.parametrize("broker_reason", list(ProbeIndeterminateReason))
def test_broker_reason_survives_contract_and_runtime_diagnostics(broker_reason):
    runtime, _requests = _manual_runtime()
    now = runtime_module.time.monotonic()
    execution = ProbeExecutionResult(
        disposition=ProbeDisposition.INDETERMINATE,
        timing=ProbeTiming(now, None, None, now, 0.0, None),
        indeterminate_reason=broker_reason,
    )
    runtime._runtime_probe_broker = SimpleNamespace(run_pair=lambda *args, **kwargs: (execution, execution))
    contracts = runtime._run_manual_exit_quality_contracts(
        RuntimeManualValidation(), probe=lambda *args: pytest.fail("unexpected socket"),
        phase_timeout_seconds=1.0,
        contract_urls=("https://one.test/204", "https://two.test/204"),
    )
    assert [item.probe_indeterminate_reason for item in contracts] == [broker_reason.value] * 2
    runtime._manual_current_exit_quality_contracts = lambda session: contracts
    result = runtime.check_exit_quality(proxy_url=None, request_id="facility")
    assert result["diagnostics"]["probe_indeterminate_reasons"] == [broker_reason.value]


def test_overview_only_adapts_mixed_result_without_changing_shared_validation(request_runtime):
    runtime, _clock = request_runtime
    shared = {"state": "data_plane_failed", "identity": {}, "diagnostics": {
        "neutral_connectivity": {"outcome": "current_mixed", "success_count": 1},
    }}
    runtime._check_current_exit_identity_early_once = lambda **kwargs: shared
    result = runtime.check_current_exit_identity(proxy_url=None, request_id="mixed")
    assert result["state"] == "partial"
    assert result["diagnostics"]["overview_failure_reason"] == "partial_validation"
    assert shared["state"] == "data_plane_failed"


def test_host_probe_keeps_its_structured_cancellation_reason():
    runtime, _requests = _manual_runtime()
    runtime._manual_current_exit_quality_contracts = lambda session: tuple(
        ManualDataPlaneProbeResult.https_204(f"neutral-{index}") for index in range(2)
    )
    runtime._system_proxy_capture_proof_required = lambda: True
    runtime.system_proxy = SimpleNamespace(is_target_active=lambda: True)

    def cancelled(**kwargs):
        raise runtime_module._RuntimeProbeBudgetUnavailable(
            "cancelled host probe", probe_indeterminate_reason="cancelled",
        )

    runtime._verify_system_proxy_host_capture = cancelled
    result = runtime.check_exit_quality(proxy_url=None, request_id="host-cancelled")
    assert result["state"] == "data_plane_indeterminate"
    assert result["diagnostics"]["probe_indeterminate_reasons"] == ["cancelled"]


def test_progress_reports_validation_and_identity_without_weakening_execution_checks(monkeypatch):
    runtime, _route = identity_runtime()
    progress = []
    monkeypatch.setattr(runtime_module, "collect_exit_identity", lambda **kwargs: {
        "identity": {"ipv4": {"state": "unconfirmed", "ip": "8.8.8.8"}},
    })
    result = runtime.check_current_exit_identity(
        proxy_url="http://127.0.0.1:7890", request_id="progress", progress_callback=progress.append,
    )
    assert result["state"] == "complete"
    assert [item["phase"] for item in progress] == ["validating_route", "querying_identity"]
    assert all(item["request_id"] == "progress" and item["attempt"] == 1 for item in progress)


@pytest.mark.parametrize("family_state", ["conflict", "rotating"])
@pytest.mark.parametrize("other_family_ok", [False, True])
def test_identity_ambiguity_retains_family_evidence_and_usable_other_family(
    monkeypatch, family_state, other_family_ok,
):
    runtime, route = identity_runtime()
    identity = {"ipv4": {"state": family_state, "ip": None, "evidence": []}}
    if other_family_ok:
        identity["ipv6"] = {"state": "unconfirmed", "ip": "2001:4860:4860::8888"}
    monkeypatch.setattr(runtime_module, "collect_exit_identity", lambda **kwargs: {"identity": identity})
    result = runtime._collect_current_exit_identity(
        proxy_url="http://127.0.0.1:7890", request_id="ambiguous", timeout_seconds=2, proven_route=route,
    )
    assert result["identity"] == identity
    assert result["state"] == ("complete" if other_family_ok else "unavailable")
    assert result["diagnostics"]["overview_failure_reason"] == f"identity_{family_state}"
    assert result.get("error") != "identity_unavailable"


@pytest.mark.parametrize("attempt,expected", [
    ({"status": "timeout"}, "identity_timeout"),
    ({"status": "error", "error_code": "rate_limited", "http_status": 429}, "identity_rate_limited"),
    ({"status": "timeout", "transport_retries": [{"error_code": "rate_limited", "http_status": 429}]},
     "identity_rate_limited"),
])
def test_identity_timeout_and_rate_limit_are_distinct(monkeypatch, attempt, expected):
    runtime, route = identity_runtime()
    monkeypatch.setattr(runtime_module, "collect_exit_identity", lambda **kwargs: {"identity": {
        "ipv4": {"state": "unavailable", "ip": None, "evidence": [{"attempts": [attempt]}]},
    }})
    result = runtime._collect_current_exit_identity(
        proxy_url="http://127.0.0.1:7890", request_id="provider-failed", timeout_seconds=2, proven_route=route,
    )
    assert result["error"] == expected
