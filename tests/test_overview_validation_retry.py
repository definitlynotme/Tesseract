"""One click revalidates automatic settling without reusing invalid evidence."""
from __future__ import annotations

import copy
import threading
from types import SimpleNamespace

import pytest

import desktop_runtime as runtime_module
from desktop_runtime import DesktopRuntime


def _route(leaf):
    return {
        "confirmed": True,
        "binding_marker": leaf,
        "leaf_proxy": leaf,
    }


def _proof(leaf="route-a"):
    return {
        "state": "pending_identity",
        "route": _route(leaf),
        "identity": {},
        "diagnostics": {
            "identity_pending": True,
            "neutral_connectivity": {"success_count": 2, "state": "ok"},
        },
    }


def _settled(reason="route_settled_during_check"):
    return {
        "state": "data_plane_indeterminate",
        "route": _route("route-b"),
        "freshness": {"reason": reason},
        # A service cache must never become evidence for the retry.
        "identity": {"ipv4": {"ip": "1.1.1.1"}},
    }


@pytest.fixture
def request_runtime(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(runtime_module.time, "monotonic", lambda: clock.now)
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_egress_recovery_profile = "early_single_coordinator"
    runtime._runtime_initial_activation_complete = lambda: True
    runtime._lifecycle_revision = 1
    runtime._runtime_generation = 1
    runtime._core_generation = 1
    runtime._runtime_route_epoch = 1
    runtime._runtime_network_context_epoch = 1
    runtime._runtime_selector_intent_revision = 1
    runtime.stop_event = threading.Event()
    identity_calls = []

    def identity(**kwargs):
        identity_calls.append(kwargs)
        return {
            "request_id": kwargs["request_id"],
            "state": "complete",
            "route": copy.deepcopy(kwargs["proven_route"]),
            "identity": {"ipv4": {"ip": "8.8.8.8"}},
        }

    runtime._collect_current_exit_identity = identity
    return SimpleNamespace(runtime=runtime, clock=clock, identity_calls=identity_calls)


def _check(runtime):
    return runtime.check_current_exit_identity(
        proxy_url="http://127.0.0.1:7890", request_id="this-click", timeout_seconds=8,
    )


@pytest.mark.parametrize("reason", [
    "route_settled_during_check", "manual_execution_binding_unverified",
])
def test_automatic_settling_reproves_new_binding_with_original_deadline(request_runtime, reason):
    state = request_runtime
    calls = []

    def validate(**kwargs):
        calls.append(kwargs)
        state.clock.now += 1.8
        if len(calls) == 1:
            state.runtime._runtime_route_epoch += 1
            return _settled(reason)
        return _proof("route-b")

    state.runtime.check_exit_quality = validate
    result = _check(state.runtime)

    assert result["state"] == "complete"
    assert result["identity"]["ipv4"]["ip"] == "8.8.8.8"
    assert len(calls) == 2
    assert [call["manual_validation_lease"].deadline_monotonic for call in calls] == [108, 108]
    assert [call["manual_validation_lease"].route_epoch for call in calls] == [1, 2]
    assert all(call["queue_identity_diagnostic"] is False for call in calls)
    assert len(state.identity_calls) == 1
    assert state.identity_calls[0]["proven_route"]["leaf_proxy"] == "route-b"
    assert result["diagnostics"]["overview_validation_attempts"] == 2
    assert result["diagnostics"]["overview_validation_retry_reasons"] == [reason]


def test_route_switch_during_identity_discards_both_ip_and_old_data_plane_proof(request_runtime):
    state = request_runtime
    validation_routes = []
    identity_routes = []

    def validate(**kwargs):
        leaf = "route-a" if not validation_routes else "route-b"
        validation_routes.append(leaf)
        return _proof(leaf)

    def identity(**kwargs):
        identity_routes.append(kwargs["proven_route"]["leaf_proxy"])
        if len(identity_routes) == 1:
            state.runtime._runtime_route_epoch += 1
            return {
                "state": "route_changed", "route_changed": True,
                "freshness": {"reason": "route_changed"}, "identity": {},
            }
        return {
            "state": "complete", "route": kwargs["proven_route"],
            "identity": {"ipv4": {"ip": "8.8.8.8"}},
        }

    state.runtime.check_exit_quality = validate
    state.runtime._collect_current_exit_identity = identity
    result = _check(state.runtime)
    assert validation_routes == identity_routes == ["route-a", "route-b"]
    assert result["state"] == "complete"
    assert result["route"]["leaf_proxy"] == "route-b"
    assert "route_changed" not in result


@pytest.mark.parametrize("changed_attr", [
    "_lifecycle_revision", "_runtime_generation", "_core_generation",
    "_runtime_network_context_epoch", "_runtime_selector_intent_revision",
])
def test_request_owner_change_cannot_trigger_new_route_proof(request_runtime, changed_attr):
    state = request_runtime
    calls = []

    def validate(**kwargs):
        calls.append(kwargs)
        setattr(state.runtime, changed_attr, getattr(state.runtime, changed_attr) + 1)
        return _settled()

    state.runtime.check_exit_quality = validate
    result = _check(state.runtime)
    assert result["state"] == "data_plane_indeterminate"
    assert result["identity"] == {}
    assert len(calls) == 1
    assert state.identity_calls == []


def test_repeated_native_churn_stops_after_three_attempts(request_runtime):
    state = request_runtime
    calls = []

    def validate(**kwargs):
        calls.append(kwargs)
        state.runtime._runtime_route_epoch += 1
        return _settled()

    state.runtime.check_exit_quality = validate
    result = _check(state.runtime)
    assert result["state"] == "data_plane_indeterminate"
    assert result["identity"] == {}
    assert len(calls) == 3
    assert state.identity_calls == []
    assert result["diagnostics"]["overview_validation_attempts"] == 3


def test_nearly_exhausted_original_budget_does_not_restart_validation(request_runtime):
    state = request_runtime
    calls = []

    def validate(**kwargs):
        calls.append(kwargs)
        state.clock.now = 107.6
        return _settled()

    state.runtime.check_exit_quality = validate
    result = _check(state.runtime)
    assert result["state"] == "data_plane_indeterminate"
    assert len(calls) == 1
    assert state.identity_calls == []


@pytest.mark.parametrize("failure", [
    {"state": "data_plane_failed", "freshness": {"reason": "transport_failed"}},
    {"state": "data_plane_indeterminate", "freshness": {"reason": "manual_probe_not_completed"}},
    {"state": "controller_hold", "freshness": {"reason": "manual_validation_lease_stale"}},
])
def test_retry_is_limited_to_route_ownership_transitions(request_runtime, failure):
    state = request_runtime
    calls = []

    def validate(**kwargs):
        calls.append(kwargs)
        return copy.deepcopy(failure)

    state.runtime.check_exit_quality = validate
    result = _check(state.runtime)
    assert result["state"] == failure["state"]
    assert len(calls) == 1
    assert state.identity_calls == []


def test_user_selector_change_during_identity_is_terminal(request_runtime):
    state = request_runtime
    validations = []

    def validate(**kwargs):
        validations.append(kwargs)
        return _proof()

    def identity(**kwargs):
        state.runtime._runtime_selector_intent_revision += 1
        state.runtime._runtime_route_epoch += 1
        return {"state": "route_changed", "freshness": {"reason": "route_changed"}, "identity": {}}

    state.runtime.check_exit_quality = validate
    state.runtime._collect_current_exit_identity = identity
    result = _check(state.runtime)
    assert result["state"] == "route_changed"
    assert result["identity"] == {}
    assert len(validations) == 1


def test_user_intent_after_proof_cannot_be_adopted_for_identity(request_runtime):
    state = request_runtime
    validations = []

    def validate(**kwargs):
        validations.append(kwargs)
        # The selector write is still queued: the route has not changed yet,
        # but the proof belongs to the previous user intent.
        state.runtime._runtime_selector_intent_revision += 1
        return _proof()

    state.runtime.check_exit_quality = validate
    result = _check(state.runtime)
    assert result["state"] == "unavailable"
    assert result["error"] == "manual_validation_execution_changed"
    assert result["identity"] == {}
    assert len(validations) == 1
    assert state.identity_calls == []


def test_core_pid_change_is_terminal_even_before_generation_counter_advances(request_runtime):
    state = request_runtime
    process = {"running": True, "pid": 101, "process_generation": 1}
    state.runtime.core_runner = SimpleNamespace(status=lambda: dict(process))
    calls = []

    def validate(**kwargs):
        calls.append(kwargs)
        process["pid"] = 202
        return _settled("manual_execution_binding_unverified")

    state.runtime.check_exit_quality = validate
    result = _check(state.runtime)
    assert result["state"] == "data_plane_indeterminate"
    assert len(calls) == 1


def test_collector_core_owner_change_cannot_rebind_request_to_replacement_process(request_runtime):
    state = request_runtime
    validations = []

    def validate(**kwargs):
        validations.append(kwargs)
        return _proof()

    state.runtime.check_exit_quality = validate
    state.runtime._collect_current_exit_identity = lambda **_kwargs: {
        "state": "unavailable", "error": "manual_validation_execution_changed",
        "identity": {}, "diagnostics": {"identity_execution_owner_changed": True},
    }
    result = _check(state.runtime)
    assert result["state"] == "unavailable"
    assert result["identity"] == {}
    assert len(validations) == 1


def test_collector_diagnostics_remain_bound_to_final_attempt(request_runtime):
    state = request_runtime
    state.runtime.check_exit_quality = lambda **_kwargs: _proof()
    state.runtime._collect_current_exit_identity = lambda **_kwargs: {
        "state": "complete", "identity": {"ipv4": {"ip": "8.8.8.8"}},
        "diagnostics": {"identity_collection_attempts": 2, "identity_provider_status": {"ok": 1}},
    }
    result = _check(state.runtime)
    assert result["diagnostics"]["identity_collection_attempts"] == 2
    assert result["diagnostics"]["identity_provider_status"] == {"ok": 1}
    assert result["diagnostics"]["neutral_connectivity"]["success_count"] == 2
