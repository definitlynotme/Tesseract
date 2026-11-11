"""A lost READY state must not trap an admitted Core in startup admission."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from desktop_runtime import DesktopRuntime
from runtime_egress_recovery import RuntimeEgressReadiness, RuntimeEgressReadinessState


@pytest.fixture
def admitted_runtime():
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_egress_recovery_profile = "early_single_coordinator"
    runtime._core_lifecycle_lock = threading.RLock()
    runtime._tier3_egress_activation_lock = threading.RLock()
    runtime._core_generation = 4
    runtime._runtime_initial_activation_generation = 4
    runtime._core_runtime_phase = "formal_capture"
    runtime._tier3_egress_activation_pending = False
    runtime._tier3_egress_activation_pending_mode = ""
    runtime._tier3_egress_activation_worker_running = False
    runtime._tier3_egress_activation_active_mode = ""
    runtime._tier3_egress_activation_active_generation = 0
    runtime._runtime_egress_readiness = RuntimeEgressReadiness(
        state=RuntimeEgressReadinessState.DATA_PLANE_FAILED,
        validation_active=False,
        reason="runtime route failed after READY",
    )
    runtime.manager = SimpleNamespace(effective_egress_ok=False)
    runtime.core_runner = SimpleNamespace(
        is_running=lambda: True,
        status=lambda: {"running": True, "pid": 101, "process_generation": 1},
    )
    runtime.stop_event = threading.Event()
    proof = {
        "state": "pending_identity",
        "route": {"binding_marker": "current-route", "confirmed": True},
        "identity": {},
        "diagnostics": {"neutral_connectivity": {"success_count": 2}},
    }
    validations = []
    identities = []

    def validate(**kwargs):
        validations.append(kwargs)
        return dict(proof)

    def identity(**kwargs):
        identities.append(kwargs)
        return {
            "request_id": kwargs["request_id"],
            "state": "complete",
            "route": kwargs["proven_route"],
            "identity": {"ipv4": {"state": "confirmed", "ip": "8.8.8.8"}},
        }

    runtime.check_exit_quality = validate
    runtime._collect_current_exit_identity = identity
    return SimpleNamespace(
        runtime=runtime, proof=proof, validations=validations, identities=identities,
    )


def _check(runtime, request_id="degraded-overview"):
    return runtime.check_current_exit_identity(
        proxy_url="http://127.0.0.1:7890", request_id=request_id, timeout_seconds=8,
    )


@pytest.mark.parametrize("phase", ["formal_capture", "formal_no_capture"])
@pytest.mark.parametrize("readiness,effective_ok", [
    (RuntimeEgressReadinessState.DATA_PLANE_FAILED, False),
    (RuntimeEgressReadinessState.CONTROLLER_HOLD, None),
    (RuntimeEgressReadinessState.SERVING, True),
    (RuntimeEgressReadinessState.READY, False),
])
def test_admitted_core_reproves_path_even_when_not_ready(
    admitted_runtime, phase, readiness, effective_ok,
):
    state = admitted_runtime
    runtime = state.runtime
    runtime._core_runtime_phase = phase
    runtime._runtime_egress_readiness = RuntimeEgressReadiness(
        state=readiness, validation_active=False,
    )
    runtime.manager.effective_egress_ok = effective_ok
    assert not runtime._runtime_initial_activation_complete()

    def unexpected_admission(**_kwargs):
        raise AssertionError("an admitted Core must use fresh manual validation")

    runtime._wait_for_manual_initial_egress_admission = unexpected_admission
    for index in range(2):
        result = _check(runtime, f"retry-{index}")
        assert result["state"] == "complete", result
        assert result["identity"]["ipv4"]["ip"] == "8.8.8.8"
        assert result["request_id"] == f"retry-{index}"
    assert len(state.validations) == len(state.identities) == 2
    assert all(call["queue_identity_diagnostic"] is False for call in state.validations)
    # The historical generation only selects a verifier; it is not READY proof.
    assert not runtime._runtime_initial_activation_complete()


def test_admitted_core_with_failed_current_proof_does_not_collect_identity(admitted_runtime):
    state = admitted_runtime
    state.proof.update(state="data_plane_failed", identity={"ipv4": {"ip": "1.1.1.1"}})
    result = _check(state.runtime)
    assert result["state"] == "data_plane_failed", result
    assert len(state.validations) == 1
    assert state.identities == []
    assert result["identity"] == {}


@pytest.mark.parametrize("admitted_generation,active", [(-1, False), (3, False), (4, True)])
def test_unadmitted_or_active_startup_still_uses_sole_admission(
    admitted_runtime, admitted_generation, active,
):
    state = admitted_runtime
    runtime = state.runtime
    runtime._runtime_initial_activation_generation = admitted_generation
    runtime._tier3_egress_activation_active_mode = "activation" if active else ""
    attached = []

    def wait(**kwargs):
        attached.append(kwargs)
        return False, "initial_egress_admission_pending"

    runtime._wait_for_manual_initial_egress_admission = wait
    result = _check(runtime)
    assert result["error"] == "initial_egress_admission_pending"
    assert result["identity"] == {}
    assert len(attached) == 1
    assert not state.validations and not state.identities


def test_overview_attaches_during_startup_capture_commit(admitted_runtime):
    runtime = admitted_runtime.runtime
    runtime._tier3_egress_activation_active_mode = "activation"
    runtime._tier3_egress_activation_active_generation = 1
    runtime._runtime_egress_readiness = RuntimeEgressReadiness(
        state=RuntimeEgressReadinessState.ACTIVATION_HOLD, validation_active=True,
    )
    queued = []
    runtime._request_initial_egress_admission = lambda: queued.append(True)

    def finish_startup(_timeout):
        runtime._runtime_egress_readiness = RuntimeEgressReadiness(
            state=RuntimeEgressReadinessState.READY, validation_active=False,
        )
        runtime.manager.effective_egress_ok = True
        return False

    runtime.stop_event.wait = finish_startup
    ready, reason = runtime._wait_for_manual_initial_egress_admission(
        deadline_monotonic=time.monotonic() + 0.5,
    )
    assert (ready, reason) == (True, "")
    assert not queued
