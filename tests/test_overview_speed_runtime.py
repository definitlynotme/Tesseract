"""Runtime side of the 09-23 overview speed work: auto lookups, budgets, speculation, logs."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

import desktop_runtime as runtime_module
from desktop_runtime import DesktopRuntime
from tests.test_overview_validation_contract import (
    _identity,
    overview_contract,
)


def _ready(runtime):
    runtime.core_runner = SimpleNamespace(is_running=lambda: True)
    runtime._runtime_egress_readiness = SimpleNamespace(
        lifecycle_revision=1, route_epoch=1, network_context_epoch=1,
    )


def _auto(state, **kwargs):
    return state.runtime.check_current_exit_identity(
        proxy_url="http://127.0.0.1:7890", request_id="auto-1",
        timeout_seconds=8.0, trigger="auto", **kwargs,
    )


# ---- automatic identity-only path -----------------------------------------


def test_auto_lookup_reads_identity_without_canonical_validation(overview_contract):
    state = overview_contract
    _ready(state.runtime)
    state.runtime.check_exit_quality = lambda **_kwargs: pytest.fail("auto ran the canonical validation")
    result = _auto(state)

    assert result["state"] == "complete"
    assert result["identity"]["ipv4"]["ip"] == "8.8.8.8"
    assert result["freshness"]["reason"] == "auto_lookup"
    assert result["diagnostics"]["overview_trigger"] == "auto"
    assert result["diagnostics"]["overview_validation_source"] == "readiness"
    assert "overview_failure_reason" not in result["diagnostics"]
    assert len(state.identity_calls) == 1


def test_auto_lookup_without_ready_token_makes_no_request(overview_contract):
    state = overview_contract
    state.runtime._runtime_initial_activation_complete = lambda: False
    _ready(state.runtime)
    result = _auto(state)

    assert result["error"] == "auto_not_ready"
    assert result["diagnostics"]["overview_failure_reason"] == "auto_not_ready"
    assert not state.identity_calls


def test_auto_lookup_for_a_route_that_already_changed_is_skipped(overview_contract):
    state = overview_contract
    _ready(state.runtime)
    result = _auto(state, expected_binding_marker="some-other-route")

    assert result["state"] == "stale"
    assert result["diagnostics"]["overview_failure_reason"] == "route_changed"
    assert not state.identity_calls


def test_readiness_lost_during_auto_lookup_discards_the_identity(overview_contract, monkeypatch):
    state = overview_contract
    _ready(state.runtime)

    def collect(**_kwargs):
        state.runtime._runtime_egress_readiness = SimpleNamespace(
            lifecycle_revision=1, route_epoch=2, network_context_epoch=1,
        )
        return _identity()

    monkeypatch.setattr(runtime_module, "collect_exit_identity", collect)
    result = _auto(state)
    assert result["identity"] == {}
    assert result["diagnostics"]["overview_failure_reason"] == "auto_ready_lost"


def test_auto_lookup_reports_a_foreign_system_proxy_as_a_warning(overview_contract):
    state = overview_contract
    _ready(state.runtime)
    state.runtime._system_proxy_capture_proof_required = lambda: True
    state.runtime.system_proxy = SimpleNamespace(is_target_active=lambda: False)
    result = _auto(state)

    assert result["state"] == "partial"
    assert result["diagnostics"]["overview_warning_reason"] == "system_proxy_capture_inactive"
    assert result["identity"]["ipv4"]["ip"] == "8.8.8.8"


# ---- contract budget floor -------------------------------------------------


def _budget_runtime(configured_ms=8000, recent_ms=None):
    runtime = object.__new__(DesktopRuntime)
    runtime._default_probe_timeout_ms = lambda: configured_ms
    if recent_ms is not None:
        runtime._runtime_last_verified_route = {"result": {"latency_ms": recent_ms}}
    return runtime


def test_contract_floor_raises_a_stale_fast_rtt():
    runtime = _budget_runtime(recent_ms=150)
    per_probe, _total = runtime._manual_exit_quality_probe_budget()
    assert per_probe < 1.0
    assert runtime._manual_exit_quality_probe_budget(min_per_probe_seconds=2.75) == (2.75, 5.0)


def test_contract_floor_respects_the_configured_ceiling_and_default_is_unchanged():
    assert _budget_runtime(configured_ms=2000)._manual_exit_quality_probe_budget(
        min_per_probe_seconds=2.75,
    ) == (2.0, 4.75)
    assert _budget_runtime()._manual_exit_quality_probe_budget() == (2.0, 4.75)


def test_only_the_overview_lease_requests_the_contract_floor():
    runtime = object.__new__(DesktopRuntime)
    seen = []

    class Stop(Exception):
        pass

    def budget(**kwargs):
        seen.append(kwargs.get("min_per_probe_seconds"))
        raise Stop

    runtime._manual_exit_quality_probe_budget = budget
    with pytest.raises(Stop):
        runtime._check_exit_quality_impl(proxy_url=None, request_id="x", manual_validation_lease=object())
    with pytest.raises(Stop):
        runtime._check_exit_quality_impl(proxy_url=None, request_id="y")
    assert seen == [2.75, 0.0]


# ---- startup attach reserve ---------------------------------------------------


def test_admission_attach_leaves_identity_a_reserve(overview_contract):
    state = overview_contract
    runtime = state.runtime
    runtime._manual_identity_needs_initial_admission = lambda: True
    observed = {}

    def wait(**kwargs):
        observed["deadline"] = kwargs["deadline_monotonic"]
        return False, "initial_egress_admission_pending"

    runtime._wait_for_manual_initial_egress_admission = wait
    deadline = time.monotonic() + 8.0
    runtime.check_current_exit_identity(
        proxy_url="http://127.0.0.1:7890", request_id="attach",
        timeout_seconds=8.0, deadline_monotonic=deadline,
    )
    assert observed["deadline"] == pytest.approx(deadline - 3.0, abs=0.01)


# ---- status hints ------------------------------------------------------------


def test_deferral_status_fields_report_phase_and_admission_without_locks():
    runtime = object.__new__(DesktopRuntime)
    runtime._core_runtime_phase = "formal_no_capture"
    runtime._tier3_egress_activation_pending = True
    assert runtime._overview_deferral_status_fields() == {
        "core_runtime_phase": "formal_no_capture",
        "initial_admission_active": True,
    }
    empty = object.__new__(DesktopRuntime)
    assert empty._overview_deferral_status_fields() == {
        "core_runtime_phase": "", "initial_admission_active": False,
    }


# ---- test hygiene ------------------------------------------------------------------


def test_tests_that_pin_runtime_providers_also_pin_overview_providers():
    """The overview reads OVERVIEW_IDENTITY_PROVIDERS; pinning only the legacy
    tuple would silently send a local test to the real IP services."""
    import re
    from pathlib import Path

    pattern = re.compile(r"setattr\(\s*runtime_module\s*,\s*['\"](\w+)['\"]")
    offenders = []
    for path in Path(__file__).parent.glob("test_*.py"):
        names = set(pattern.findall(path.read_text(encoding="utf-8")))
        if "EXIT_IDENTITY_PROVIDERS" in names and "OVERVIEW_IDENTITY_PROVIDERS" not in names:
            offenders.append(path.name)
    assert not offenders
