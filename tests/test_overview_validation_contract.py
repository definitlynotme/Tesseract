"""Qt-free runtime fixture for the overview identity request tests.

The Qt window half of this contract (and its tests) went with the retired
Qt desktop app; the runtime side is still exercised by the importers.
"""
from __future__ import annotations

import copy
import threading
from types import SimpleNamespace

import pytest

import desktop_runtime as runtime_module
from desktop_runtime import DesktopRuntime


def _route(name: str = "route-a") -> dict[str, object]:
    route = {
        "execution_binding_marker": "1:1:101",
        "selected_chain": ["FINAL-EXIT", name],
        "leaf_proxy": name,
        "confirmed": True,
        "stable_during_check": True,
        "core_running": True,
        "path_kind": "tier2",
    }
    route["binding_marker"] = DesktopRuntime._exit_route_binding_marker(route)
    route["logical_binding_marker"] = route["binding_marker"]
    return route


def _identity(ip: str = "8.8.8.8") -> dict[str, object]:
    return {
        "identity": {
            "ipv4": {"family": 4, "state": "unconfirmed", "ip": ip},
            "ipv6": {"family": 6, "state": "unavailable", "ip": None},
        }
    }


@pytest.fixture
def overview_contract(monkeypatch):
    """A DesktopRuntime whose network proofs are controlled."""
    route_state = {"current": _route()}
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_egress_recovery_profile = "early_single_coordinator"
    runtime._runtime_initial_activation_complete = lambda: True
    runtime._lifecycle_revision = 1
    runtime._runtime_route_epoch = 1
    runtime._runtime_network_context_epoch = 1
    runtime._runtime_selector_intent_revision = 0
    runtime.stop_event = threading.Event()
    runtime.logger = SimpleNamespace(
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        debug=lambda *_args, **_kwargs: None,
    )
    runtime.current_route_context = lambda **_kwargs: copy.deepcopy(route_state["current"])
    runtime._runtime_worker_token = lambda: (1, 1, 1, 101, 1, 0)
    runtime._runtime_worker_token_is_current = lambda token: token == runtime._runtime_worker_token()
    runtime._register_runtime_internal_probe = lambda *_args, **_kwargs: None
    runtime._system_proxy_capture_proof_required = lambda: False
    proof = {
        "schema_version": 2,
        "state": "pending_identity",
        "route": _route(),
        "identity": {},
        "diagnostics": {
            "identity_pending": True,
            "neutral_connectivity": {
                "state": "ok",
                "success_count": 2,
                "execution_binding_current": True,
            },
            "system_proxy_capture": {
                "required": False,
                "target_active": True,
                "host_path_verified": True,
            },
        },
    }
    validation_calls = []

    def check_quality(**kwargs):
        validation_calls.append(kwargs)
        return {**copy.deepcopy(proof), "request_id": kwargs["request_id"]}

    runtime.check_exit_quality = check_quality
    identity_calls = []

    def collect(**kwargs):
        identity_calls.append(kwargs)
        return _identity()

    monkeypatch.setattr(runtime_module, "collect_exit_identity", collect)
    return SimpleNamespace(
        runtime=runtime,
        route_state=route_state,
        proof=proof,
        validation_calls=validation_calls,
        identity_calls=identity_calls,
    )

