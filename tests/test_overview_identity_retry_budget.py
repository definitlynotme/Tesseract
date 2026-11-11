"""Identity retries use only the original proof's budget and observations."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import desktop_runtime as runtime_module
from desktop_runtime import DesktopRuntime


@pytest.fixture
def identity_runtime(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(runtime_module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_route_epoch = 1
    runtime._runtime_selector_intent_revision = 1
    runtime.logger = SimpleNamespace(info=lambda *_args, **_kwargs: None)
    runtime._register_runtime_internal_probe = lambda *_args, **_kwargs: None
    route = {"confirmed": True, "binding_marker": "same-route", "core_instance": "same-core"}
    route_reads = []

    def read_route(**kwargs):
        route_reads.append(kwargs["timeout_seconds"])
        clock.now += 0.02
        return dict(route)

    runtime.current_route_context = read_route
    runtime._submit_current_route_probe_failure = lambda *_args, **_kwargs: pytest.fail(
        "identity providers cannot initiate node recovery"
    )
    return SimpleNamespace(runtime=runtime, clock=clock, route=route, route_reads=route_reads)


def _identity(status="error", *, ip=None):
    return {"identity": {"ipv4": {
        "ip": ip, "state": "confirmed" if ip else "unavailable",
        "evidence": [{"attempts": [{"status": status, "ip": ip}]}],
    }}}


def _check(state, *, budget=6.0):
    return state.runtime._collect_current_exit_identity(
        proxy_url="http://127.0.0.1:7890", request_id="same-request",
        timeout_seconds=budget, proven_route=dict(state.route),
    )


def test_healthy_identity_can_use_remaining_budget_beyond_old_short_cap(identity_runtime, monkeypatch):
    state = identity_runtime
    budgets = []

    def collect(**kwargs):
        budgets.append(kwargs["total_timeout_seconds"])
        state.clock.now += 2.8
        return _identity("ok", ip="8.8.8.8")

    monkeypatch.setattr(runtime_module, "collect_exit_identity", collect)
    result = _check(state)
    assert result["state"] == "complete"
    assert 5 < budgets[0] < 6
    assert state.clock.now < 106
    assert len(state.route_reads) == 2


def test_provider_retry_evidence_is_counted_without_another_collection(identity_runtime, monkeypatch):
    state = identity_runtime
    budgets = []

    def collect(**kwargs):
        budgets.append(kwargs["total_timeout_seconds"])
        state.clock.now += 0.3
        assert kwargs["retry_transient_errors"] is True
        result = _identity("ok", ip="8.8.8.8")
        result["identity"]["ipv4"]["evidence"][0]["attempts"][0]["transport_retries"] = [
            {"status": "error"}
        ]
        return result

    monkeypatch.setattr(runtime_module, "collect_exit_identity", collect)
    result = _check(state)
    assert result["state"] == "complete"
    assert len(budgets) == 1
    assert result["diagnostics"]["identity_collection_attempts"] == 1
    assert result["diagnostics"]["identity_provider_status"] == {"error": 1, "ok": 1}


def test_persistent_provider_error_cannot_restart_whole_collection(identity_runtime, monkeypatch):
    calls = []
    monkeypatch.setattr(runtime_module, "collect_exit_identity", lambda **kwargs: (
        calls.append(kwargs) or _identity()
    ))
    result = _check(identity_runtime)
    assert len(calls) == 1
    assert result["state"] == "unavailable"
    assert result["error"] == "identity_unavailable"
    assert result["route"]["confirmed"] is True


@pytest.mark.parametrize("observation", [
    _identity("invalid"),
    _identity("ok", ip="8.8.8.8"),
    {"identity": {"ipv4": {"state": "conflict", "ip": None, "evidence": [
        {"attempts": [{"status": "ok", "ip": "8.8.8.8"}]},
        {"attempts": [{"status": "ok", "ip": "1.1.1.1"}]},
    ]}}},
])
def test_observations_are_not_erased_to_retry_for_a_success(identity_runtime, monkeypatch, observation):
    calls = []
    monkeypatch.setattr(runtime_module, "collect_exit_identity", lambda **kwargs: (
        calls.append(kwargs) or observation
    ))
    result = _check(identity_runtime)
    assert len(calls) == 1
    assert result["identity"] == observation["identity"]


def test_exhausted_collection_still_reserves_final_route_readback(identity_runtime, monkeypatch):
    state = identity_runtime
    calls = []

    def collect(**kwargs):
        calls.append(kwargs)
        state.clock.now += kwargs["total_timeout_seconds"]
        return _identity("timeout")

    monkeypatch.setattr(runtime_module, "collect_exit_identity", collect)
    result = _check(state, budget=0.7)
    assert len(calls) == 1
    assert result["route"]["confirmed"] is True
    assert len(state.route_reads) == 2
    assert state.clock.now < 100.7


@pytest.mark.parametrize("changed_field", ["_runtime_route_epoch", "_runtime_selector_intent_revision"])
def test_route_or_intent_change_discards_provider_result(identity_runtime, monkeypatch, changed_field):
    state = identity_runtime
    calls = []

    def collect(**kwargs):
        calls.append(kwargs)
        setattr(state.runtime, changed_field, 2)
        return _identity()

    monkeypatch.setattr(runtime_module, "collect_exit_identity", collect)
    result = _check(state)
    assert len(calls) == 1
    assert result["identity"] == {}
    assert result["error"] == "manual_validation_execution_changed"


def test_user_intent_during_successful_lookup_invalidates_unchanged_route(identity_runtime, monkeypatch):
    state = identity_runtime

    def collect(**_kwargs):
        state.runtime._runtime_selector_intent_revision += 1
        return _identity("ok", ip="8.8.8.8")

    monkeypatch.setattr(runtime_module, "collect_exit_identity", collect)
    result = _check(state)
    assert result["state"] == "unavailable"
    assert result["identity"] == {}
    assert result["route"]["confirmed"] is False
