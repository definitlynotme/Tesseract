from __future__ import annotations

import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from desktop_runtime import DesktopRuntime, _RuntimeProbeBudgetUnavailable
from runtime_egress_recovery import CoreLoadedGroupState
from runtime_source_transport import RUNTIME_SOURCE_BOOTSTRAP_POOL_PROXY
from tests.test_runtime_ready_arm_regressions import _loaded_authority


def _runtime():
    runtime = object.__new__(DesktopRuntime)
    snapshot, payload, binding = _loaded_authority()
    pool = CoreLoadedGroupState(
        name=RUNTIME_SOURCE_BOOTSTRAP_POOL_PROXY, type_name="Fallback",
        members=(binding.node.proxy_name, "REJECT"), now=binding.node.proxy_name,
    )
    snapshot = replace(snapshot, group_graph=(*snapshot.group_graph, (pool.name, pool.members)),
                       group_states=(*snapshot.group_states, pool))
    payload["proxies"][pool.name] = {"type": pool.type_name, "all": list(pool.members), "now": pool.now}
    runtime.stop_event = threading.Event()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._core_lifecycle_lock = threading.RLock()
    runtime._runtime_failure_recovery_threads = set()
    runtime._runtime_generation = 2
    runtime._core_generation = snapshot.core_generation
    runtime._runtime_selector_intent_revision = 0
    runtime._runtime_route_snapshot = snapshot
    runtime._runtime_route_snapshot_payload = payload
    runtime._tier3_source_control_qualification = None
    runtime._core_route_mutation_is_pending = lambda: False
    runtime._current_core_runtime_phase = lambda: "formal_capture"
    runtime._runtime_worker_token = lambda: (2, 3)
    runtime._runtime_worker_token_is_current = Mock(return_value=True)
    runtime._refresh_loaded_runtime_authority = Mock(return_value=(snapshot, payload, binding))
    runtime._probe_mihomo_mixed_port_once = Mock(return_value={"status": 204})
    runtime._probe_runtime_source_pool_once = Mock()
    runtime.manager = SimpleNamespace(notify_tier3_source_supply_route_changed=Mock())
    runtime.core_runner = SimpleNamespace(
        is_running=lambda: True,
        status=lambda: {"pid": snapshot.process_pid, "process_generation": snapshot.process_generation},
    )
    return runtime, snapshot, payload, binding


def _finish(runtime):
    deadline = time.monotonic() + 2
    while getattr(runtime, "_runtime_source_demand_worker", None) is not None:
        assert time.monotonic() < deadline, "source-demand worker did not finish"
        time.sleep(0.005)


def test_readiness_queries_do_not_create_network_work():
    runtime, _, _, _ = _runtime()
    for _ in range(20):
        assert not runtime._source_control_final_exit_ready(runtime.manager)
    runtime._refresh_loaded_runtime_authority.assert_not_called()
    runtime._probe_mihomo_mixed_port_once.assert_not_called()


@pytest.mark.parametrize("phase", ["bootstrap", "formal_capture", "formal_no_capture"])
def test_source_demand_qualifies_changed_route_once(phase):
    runtime, snapshot, _, _ = _runtime()
    runtime._current_core_runtime_phase = lambda: phase
    runtime.manager.effective_egress_ok = False
    runtime._runtime_initial_activation_generation = -1
    runtime._request_runtime_source_qualification("bootstrap" if phase == "bootstrap" else "core")
    _finish(runtime)
    assert runtime._source_control_final_exit_ready(runtime.manager)
    assert runtime._tier3_source_control_qualification.snapshot_key == snapshot.snapshot_key
    if phase == "bootstrap":
        runtime._probe_runtime_source_pool_once.assert_called_once()
        runtime._probe_mihomo_mixed_port_once.assert_not_called()
    else:
        runtime._probe_mihomo_mixed_port_once.assert_called_once()
    runtime.manager.notify_tier3_source_supply_route_changed.assert_called_once_with(
        "verified_source_pool_ready" if phase == "bootstrap" else "verified_final_exit_ready"
    )
    assert runtime.manager.effective_egress_ok is False
    assert runtime._runtime_initial_activation_generation == -1


def test_bootstrap_source_demand_cannot_publish_across_formal_transition():
    runtime, _, _, _ = _runtime()
    runtime._current_core_runtime_phase = Mock(return_value="bootstrap")

    def probe(**_kwargs):
        runtime._current_core_runtime_phase.return_value = "formal_no_capture"
        return {"status": 204}

    runtime._probe_runtime_source_pool_once.side_effect = probe
    runtime._request_runtime_source_qualification("bootstrap")
    _finish(runtime)
    runtime._probe_runtime_source_pool_once.assert_called_once()
    assert runtime._tier3_source_control_qualification is None


def test_unchanged_old_proof_never_requires_another_external_probe():
    runtime, snapshot, _, binding = _runtime()
    assert runtime._set_tier3_source_control_qualification(snapshot, binding)
    proof = replace(runtime._tier3_source_control_qualification, qualified_at_monotonic=0)
    runtime._tier3_source_control_qualification = proof
    for _ in range(3):
        runtime._runtime_source_demand_state = {}
        runtime._request_runtime_source_qualification("core")
        _finish(runtime)
    assert runtime._tier3_source_control_qualification is proof
    runtime._probe_mihomo_mixed_port_once.assert_not_called()


def test_final_transaction_reuses_its_proof_without_network_io():
    runtime, snapshot, _, _ = _runtime()
    assert runtime._publish_verified_runtime_source_proof(expected_route_epoch=snapshot.route_epoch)
    assert runtime._source_control_final_exit_ready(runtime.manager)
    runtime._probe_mihomo_mixed_port_once.assert_not_called()
    runtime._refresh_loaded_runtime_authority.assert_not_called()


def test_final_transaction_cannot_publish_for_another_epoch():
    runtime, snapshot, _, _ = _runtime()
    assert not runtime._publish_verified_runtime_source_proof(expected_route_epoch=snapshot.route_epoch - 1)
    assert runtime._tier3_source_control_qualification is None


@pytest.mark.parametrize("changed", ["epoch", "identity", "intent", "generation", "stop"])
def test_demand_proof_rejects_changes_during_the_request(changed):
    runtime, snapshot, payload, binding = _runtime()

    def probe(**_kwargs):
        if changed == "epoch":
            new_snapshot = replace(snapshot, route_epoch=snapshot.route_epoch + 1)
            runtime._runtime_route_snapshot = new_snapshot
            runtime._refresh_loaded_runtime_authority.return_value = (new_snapshot, payload, binding)
        elif changed == "identity":
            runtime._refresh_loaded_runtime_authority.return_value = (
                snapshot, payload, replace(binding, node=replace(binding.node, identity="new-node")),
            )
        elif changed == "intent":
            runtime._runtime_selector_intent_revision += 1
        elif changed == "generation":
            runtime._runtime_worker_token_is_current.return_value = False
        else:
            runtime.stop_event.set()
        return {"status": 204}

    runtime._probe_mihomo_mixed_port_once.side_effect = probe
    runtime._request_runtime_source_qualification("core")
    _finish(runtime)
    assert runtime._tier3_source_control_qualification is None


def test_failed_source_demand_backs_off_without_scheduling_a_retry():
    runtime, _, _, _ = _runtime()
    runtime._probe_mihomo_mixed_port_once.side_effect = TimeoutError("source route uncertain")
    runtime._request_runtime_source_qualification("core")
    _finish(runtime)
    state = runtime._runtime_source_demand_state
    assert 4 < state["not_before"] - time.monotonic() <= 5
    assert not runtime._runtime_failure_recovery_threads
    for _ in range(10):
        runtime._request_runtime_source_qualification("core")
    runtime._probe_mihomo_mixed_port_once.assert_called_once()
    assert runtime._tier3_source_control_qualification is None


def test_missing_loaded_authority_does_not_consume_transport_retry_budget():
    runtime, snapshot, payload, binding = _runtime()
    runtime._refresh_loaded_runtime_authority.return_value = None
    runtime._request_runtime_source_qualification("core")
    _finish(runtime)
    state = runtime._runtime_source_demand_state
    assert state["attempts"] == 0
    assert state["not_before"] - time.monotonic() <= .25
    runtime._probe_mihomo_mixed_port_once.assert_not_called()
    runtime._refresh_loaded_runtime_authority.return_value = (snapshot, payload, binding)
    time.sleep(.26)
    runtime._request_runtime_source_qualification("core")
    _finish(runtime)
    assert runtime._source_control_final_exit_ready(runtime.manager)


@pytest.mark.parametrize("phase", ["bootstrap", "core"])
def test_probe_budget_pending_does_not_consume_transport_retry_budget(phase):
    runtime, _, _, _ = _runtime()
    if phase == "bootstrap":
        runtime._current_core_runtime_phase = lambda: "bootstrap"
    probe = (runtime._probe_runtime_source_pool_once if phase == "bootstrap"
             else runtime._probe_mihomo_mixed_port_once)
    probe.side_effect = _RuntimeProbeBudgetUnavailable("probe slot unavailable")
    runtime._request_runtime_source_qualification(phase)
    _finish(runtime)
    state = runtime._runtime_source_demand_state
    assert state["attempts"] == 0
    assert state["outcome"] == "probe_budget_pending"
    assert state["not_before"] - time.monotonic() <= .25
    assert not runtime._source_control_final_exit_ready(runtime.manager)
    assert not runtime._runtime_failure_recovery_threads


def test_bootstrap_proof_cannot_be_reused_after_phase_changes_without_reload():
    runtime, _, _, _ = _runtime()
    runtime._current_core_runtime_phase = Mock(return_value="bootstrap")
    runtime._request_runtime_source_qualification("bootstrap")
    _finish(runtime)
    assert runtime._source_control_final_exit_ready(runtime.manager)
    runtime._current_core_runtime_phase.return_value = "formal_no_capture"
    assert not runtime._source_control_final_exit_ready(runtime.manager)
    runtime._request_runtime_source_qualification("core")
    _finish(runtime)
    assert runtime._source_control_final_exit_ready(runtime.manager)
    runtime._probe_mihomo_mixed_port_once.assert_called_once()


def test_concurrent_source_batches_share_one_verification():
    runtime, _, _, _ = _runtime()
    started, release = threading.Event(), threading.Event()

    def probe(**_kwargs):
        started.set()
        assert release.wait(2)
        return {"status": 204}

    runtime._probe_mihomo_mixed_port_once.side_effect = probe
    runtime._request_runtime_source_qualification("core")
    try:
        assert started.wait(1)
        for _ in range(20):
            runtime._request_runtime_source_qualification("core")
        runtime._probe_mihomo_mixed_port_once.assert_called_once()
    finally:
        release.set()
        _finish(runtime)
