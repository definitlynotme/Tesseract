from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

import desktop_runtime as runtime_module
from desktop_runtime import DesktopRuntime


def _identity() -> dict[str, object]:
    return {"identity": {"ipv4": {"state": "unconfirmed", "ip": "8.8.8.8"}}}


def _runtime() -> tuple[DesktopRuntime, dict[str, object]]:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_egress_recovery_profile = "early_single_coordinator"
    runtime._runtime_initial_activation_complete = lambda: True
    runtime._lifecycle_revision = 1
    runtime._runtime_generation = 1
    runtime._core_generation = 1
    runtime._runtime_network_context_epoch = 1
    runtime._runtime_route_epoch = 1
    runtime._register_runtime_internal_probe = lambda *_args, **_kwargs: None
    runtime._system_proxy_capture_proof_required = lambda: False
    runtime.logger = SimpleNamespace(info=lambda *_args, **_kwargs: None)
    route = {
        "confirmed": True,
        "binding_marker": "stable-route",
        "core_instance": "core-1",
    }
    runtime.current_route_context = lambda **_kwargs: copy.deepcopy(route)
    runtime.check_exit_quality = lambda **kwargs: {
        "request_id": kwargs["request_id"],
        "state": "pending_identity",
        "route": copy.deepcopy(route),
        "identity": {},
        "diagnostics": {
            "neutral_connectivity": {
                "state": "ok",
                "success_count": 2,
                "execution_binding_current": True,
            },
        },
    }
    return runtime, route


@pytest.mark.parametrize("field", [
    "_core_generation",
    "_runtime_generation",
    "_lifecycle_revision",
    "_runtime_network_context_epoch",
])
def test_startup_route_readback_cannot_rebind_admission_proof_to_new_epoch(monkeypatch, field):
    runtime, route = _runtime()
    ready = {"value": False}
    runtime._runtime_initial_activation_complete = lambda: ready["value"]

    def admit(**_kwargs):
        ready["value"] = True
        return True, ""

    runtime._wait_for_manual_initial_egress_admission = admit

    def read_route(**_kwargs):
        setattr(runtime, field, getattr(runtime, field) + 1)
        return copy.deepcopy(route)

    runtime.current_route_context = read_route
    monkeypatch.setattr(runtime_module, "collect_exit_identity", lambda **_kwargs: pytest.fail(
        "identity must not use an admission proof from another epoch"
    ))

    result = runtime.check_current_exit_identity(
        proxy_url="http://127.0.0.1:7890", request_id="startup-changed", timeout_seconds=8.0,
    )

    assert result["state"] == "unavailable"
    assert result["error"] == "manual_validation_execution_changed"
    assert result["identity"] == {}


def test_startup_cannot_use_admission_if_ready_is_lost_during_route_readback(monkeypatch):
    runtime, route = _runtime()
    ready = {"value": False}
    runtime._runtime_initial_activation_complete = lambda: ready["value"]

    def admit(**_kwargs):
        ready["value"] = True
        return True, ""

    def read_route(**_kwargs):
        ready["value"] = False
        return copy.deepcopy(route)

    runtime._wait_for_manual_initial_egress_admission = admit
    runtime.current_route_context = read_route
    monkeypatch.setattr(runtime_module, "collect_exit_identity", lambda **_kwargs: pytest.fail(
        "identity must not use an admission that is no longer ready"
    ))

    result = runtime.check_current_exit_identity(
        proxy_url="http://127.0.0.1:7890", request_id="startup-unready",
    )

    assert result["state"] == "unavailable"
    assert result["identity"] == {}


def test_startup_accepts_the_generation_that_completed_admission(monkeypatch):
    runtime, route = _runtime()
    ready = {"value": False}
    runtime._runtime_initial_activation_complete = lambda: ready["value"]

    def admit(**_kwargs):
        runtime._core_generation += 1
        route["core_instance"] = "admitted-core"
        ready["value"] = True
        return True, ""

    runtime._wait_for_manual_initial_egress_admission = admit
    monkeypatch.setattr(runtime_module, "collect_exit_identity", lambda **_kwargs: _identity())

    result = runtime.check_current_exit_identity(
        proxy_url="http://127.0.0.1:7890", request_id="startup-admitted",
    )

    assert result["state"] == "complete"
    assert result["identity"]["ipv4"]["ip"] == "8.8.8.8"


@pytest.mark.parametrize("field", [
    "_core_generation",
    "_runtime_generation",
    "_lifecycle_revision",
    "_runtime_network_context_epoch",
    "_runtime_route_epoch",
    "core_instance",
])
def test_identity_cannot_survive_execution_change_on_same_logical_route(monkeypatch, field):
    runtime, route = _runtime()

    def collect(**_kwargs):
        if field == "core_instance":
            route[field] = "replacement-core"
        else:
            setattr(runtime, field, getattr(runtime, field) + 1)
        return _identity()

    monkeypatch.setattr(runtime_module, "collect_exit_identity", collect)

    result = runtime.check_current_exit_identity(
        proxy_url="http://127.0.0.1:7890", request_id="identity-changed",
    )

    assert result["state"] == "unavailable"
    assert result["error"] == "manual_validation_execution_changed"
    assert result["identity"] == {}
    assert result["route"]["confirmed"] is False
