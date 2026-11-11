from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from desktop_runtime import (
    DeferredProviderSyncKey,
    RuntimeActivationRecoveryError,
    _RuntimeRecoveryTicket,
)
from manager import RuntimeCandidateSnapshot, RuntimeCandidateState
from runtime_egress_recovery import (
    BoundRuntimeFailure,
    CoreLoadedGroupState,
    CoreLoadedRouteSnapshot,
    RuntimeEgressReadinessState,
    RuntimeNodeRef,
    RuntimeRecoveryOutcome,
    RuntimeRecoveryStatus,
    RuntimeRouteBinding,
)
from system_proxy import ProxySettings
from tests import test_desktop_runtime_recovery as recovery_fixtures
from tests.test_desktop_runtime_recovery import (
    FakeLifecycleCoreRunner,
    FakeTier3Manager,
    FakeTrafficMonitor,
    MemoryProxyBackend,
)


@pytest.fixture
def tmp_path(request):
    # These tests stub every disk-mutating lifecycle edge.  A distinct logical
    # path is sufficient and avoids Windows ACL noise from pytest/tempfile.
    return Path(__file__).resolve().parent / (
        ".runtime-serving-fallback-" + request.node.name
    )


def _quiet_runtime_logger() -> logging.Logger:
    logger = logging.getLogger(
        f"runtime-serving-fallback-{time.monotonic_ns()}"
    )
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    return logger


def _harness_runtime(tmp_path: Path):
    harness = recovery_fixtures.DesktopRuntimeRecoveryTests()
    with patch("desktop_runtime.setup_logging", return_value=_quiet_runtime_logger()):
        runtime = harness._runtime(
            tmp_path,
            MemoryProxyBackend(
                ProxySettings(enabled=False, server="", override="")
            ),
        )
    manager = FakeTier3Manager()
    manager.tier3_startup_gate_satisfied = True
    manager.tier3_traffic_capture_active = False

    def mark_capture_active(active: bool) -> None:
        manager.tier3_traffic_capture_active = bool(active)

    manager.mark_tier3_traffic_capture_active = mark_capture_active
    runtime.manager = manager
    runtime.network_mode = "system_proxy"
    runtime._runtime_traffic_capture_wanted = lambda: True
    runner = FakeLifecycleCoreRunner()
    runner.start()
    runner.status_payload.update({"pid": 4242, "process_generation": 1})
    runtime.core_runner = runner
    runtime._core_generation = 1
    runtime.traffic_monitor = FakeTrafficMonitor()
    runtime._set_core_runtime_phase("formal_no_capture")
    return harness, runtime, manager, runner


def _readiness_name(runtime) -> str:
    state = runtime._runtime_egress_readiness.state
    return str(getattr(state, "value", state))


def _assert_runtime_is_serving(runtime, manager: FakeTier3Manager) -> None:
    assert runtime._current_core_runtime_phase() == "formal_capture"
    assert manager.tier3_traffic_capture_active is True
    assert manager.effective_egress_ok is True
    assert _readiness_name(runtime) in {"ready", "serving"}


def _publish_injected_non_gateway_authority(harness, runtime):
    harness._install_injected_runtime_authority(runtime)
    return runtime._refresh_loaded_runtime_authority()


@pytest.mark.parametrize("broken_authority", ["controller", "binding"])
def test_non_gateway_readback_failure_keeps_no_capture_standby(
    tmp_path: Path,
    broken_authority: str,
) -> None:
    harness, runtime, manager, runner = _harness_runtime(tmp_path)
    snapshot, payload, _binding = _publish_injected_non_gateway_authority(
        harness,
        runtime,
    )
    assert snapshot is not None
    assert isinstance(payload, dict)
    original_proxy_map = dict(payload["proxy_map"])
    runtime._runtime_loaded_export_contract = {
        "semantic_digest": snapshot.semantic_digest,
        "proxy_map": original_proxy_map,
    }

    if broken_authority == "controller":
        runtime._runtime_egress_coordinator.set_current_snapshot(None)
    else:
        broken_controller = dict(payload["proxies"])
        broken_proxies = dict(broken_controller["proxies"])
        broken_auto = dict(broken_proxies["TIER3-AUTO"])
        broken_auto["now"] = "REJECT"
        broken_proxies["TIER3-AUTO"] = broken_auto
        broken_controller["proxies"] = broken_proxies
        with runtime._runtime_route_snapshot_lock:
            runtime._runtime_route_snapshot_payload = {
                **payload,
                "proxies": broken_controller,
            }

    # A mixed-port response without a current-generation route binding cannot
    # prove which managed leaf would receive user traffic.  Startup must stay
    # fail-closed until controller authority can be read back.
    runtime._refresh_loaded_runtime_authority = MagicMock(return_value=None)
    runtime._verify_runtime_final_exit = MagicMock(
        return_value={"status": 204, "fixture": True}
    )
    capture_reloads: list[bool] = []

    def enable_capture(_runner, *, capture_enabled: bool, **_kwargs):
        capture_reloads.append(bool(capture_enabled))
        runtime._set_core_runtime_phase("formal_capture")
        return tmp_path / "mihomo-runtime.yaml"

    runtime._hot_reload_runtime_config_phase = enable_capture
    apply_capture = MagicMock()
    runtime._apply_system_proxy_capture_after_core_start = apply_capture

    runtime._queue_tier3_egress_activation()
    assert runtime._wait_for_tier3_egress_activation(timeout=1.0)

    assert capture_reloads == []
    apply_capture.assert_not_called()
    assert runtime.traffic_monitor.started == 0
    assert runtime._current_core_runtime_phase() == "formal_no_capture"
    assert manager.tier3_traffic_capture_active is False
    assert manager.effective_egress_ok is False
    assert _readiness_name(runtime) == "controller_hold"
    runtime._refresh_loaded_runtime_authority.assert_called_once()


def test_initial_activation_requires_authoritative_binding_before_capture(
    tmp_path: Path,
) -> None:
    harness, runtime, manager, runner = _harness_runtime(tmp_path)
    snapshot, payload, binding = _publish_injected_non_gateway_authority(
        harness,
        runtime,
    )
    with runtime._runtime_route_snapshot_lock:
        runtime._runtime_route_snapshot = None
        runtime._runtime_route_snapshot_payload = None
    runtime._runtime_egress_coordinator.set_current_snapshot(None)

    order: list[str] = []
    runtime._refresh_loaded_runtime_authority = MagicMock(return_value=None)
    verification_count = 0

    def verify_without_controller(**kwargs):
        nonlocal verification_count
        assert kwargs["require_controller_ready"] is False
        verification_count += 1
        order.append(
            "no_capture_204" if verification_count == 1 else "capture_204"
        )
        return {"status": 204}

    runtime._verify_runtime_final_exit = MagicMock(
        side_effect=verify_without_controller
    )

    def enable_capture(_runner, *, capture_enabled: bool, **_kwargs):
        assert capture_enabled is True
        order.append("capture_reload")
        runtime._set_core_runtime_phase("formal_capture")
        return tmp_path / "mihomo-runtime.yaml"

    runtime._hot_reload_runtime_config_phase = enable_capture
    capture_publications: list[str] = []
    runtime._apply_system_proxy_capture_after_core_start = (
        lambda _runner: capture_publications.append("capture")
    )
    source_wakes: list[str] = []
    manager.notify_tier3_source_supply_route_changed = source_wakes.append

    runtime._queue_tier3_egress_activation()
    assert runtime._wait_for_tier3_egress_activation(timeout=1.0)
    assert order == []
    assert capture_publications == []
    assert manager.effective_egress_ok is False
    assert manager.tier3_traffic_capture_active is False
    assert runtime.traffic_monitor.started == 0
    assert runtime._current_core_runtime_phase() == "formal_no_capture"
    assert _readiness_name(runtime) == "controller_hold"
    assert "verified_final_exit_ready" not in source_wakes
    runtime._refresh_loaded_runtime_authority.assert_called_once()
    assert runtime._tier3_initial_activation_trace_snapshot()["stage"] == "binding_pending"


def test_missing_initial_binding_cannot_publish_capture_from_unattributed_204(
    tmp_path: Path,
) -> None:
    _harness, runtime, manager, runner = _harness_runtime(tmp_path)
    runtime._refresh_loaded_runtime_authority = MagicMock(return_value=None)
    def capture_reload(_runner, *, capture_enabled: bool, **_kwargs):
        assert capture_enabled is True
        runtime._set_core_runtime_phase("formal_capture")
        return tmp_path / "mihomo-runtime.yaml"

    runtime._hot_reload_runtime_config_phase = MagicMock(
        side_effect=capture_reload
    )
    runtime._verify_runtime_final_exit = MagicMock(
        return_value={"status": 204, "fixture": True}
    )
    runtime._apply_system_proxy_capture_after_core_start = MagicMock()

    runtime._queue_tier3_egress_activation()
    assert runtime._wait_for_tier3_egress_activation(timeout=1.0)

    runtime._hot_reload_runtime_config_phase.assert_not_called()
    runtime._verify_runtime_final_exit.assert_not_called()
    runtime._apply_system_proxy_capture_after_core_start.assert_not_called()
    assert runtime._current_core_runtime_phase() == "formal_no_capture"
    assert manager.effective_egress_ok is False
    assert manager.tier3_traffic_capture_active is False
    assert runtime.traffic_monitor.started == 0
    assert _readiness_name(runtime) == "controller_hold"


def _install_fixed_gateway_authority(runtime):
    gateway_ref = RuntimeNodeRef(
        tier="exit",
        pool_kind="gateway",
        identity="fixed-gateway-id",
        proxy_name="fixed-gateway",
    )
    snapshot = CoreLoadedRouteSnapshot(
        process_pid=4242,
        process_generation=1,
        core_generation=runtime._core_generation,
        semantic_digest="fixed-gateway-authority",
        route_epoch=1,
        network_context_epoch=runtime._runtime_network_context_epoch,
        group_graph=(("FINAL-EXIT", (gateway_ref.proxy_name, "REJECT")),),
        group_states=(
            CoreLoadedGroupState(
                name="FINAL-EXIT",
                type_name="Selector",
                members=(gateway_ref.proxy_name, "REJECT"),
                now=gateway_ref.proxy_name,
            ),
        ),
        proxy_nodes=(gateway_ref,),
        loaded_at_monotonic=time.monotonic(),
    )
    controller_payload = {
        "proxies": {
            "FINAL-EXIT": {
                "type": "Selector",
                "now": gateway_ref.proxy_name,
                "all": [gateway_ref.proxy_name, "REJECT"],
            },
            gateway_ref.proxy_name: {"type": "Shadowsocks"},
            "REJECT": {"type": "Reject"},
        }
    }
    payload = {
        "proxies": controller_payload,
        "proxy_map": {
            gateway_ref.proxy_name: {
                "tier": gateway_ref.tier,
                "pool_kind": gateway_ref.pool_kind,
                "identity": gateway_ref.identity,
            }
        },
    }
    with runtime._runtime_route_snapshot_lock:
        runtime._runtime_route_epoch = snapshot.route_epoch
        runtime._runtime_route_snapshot = snapshot
        runtime._runtime_route_snapshot_payload = payload
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
    binding = runtime._runtime_route_binding_for_outbound(
        snapshot,
        payload,
        "FINAL-EXIT",
    )
    assert binding is not None
    assert binding.node.pool_kind == "gateway"
    runtime._refresh_loaded_runtime_authority = MagicMock(
        return_value=(snapshot, payload, binding)
    )
    return snapshot, payload, binding


def test_confirmed_fixed_gateway_failure_keeps_the_failed_chain_without_reject(
    tmp_path: Path,
) -> None:
    _harness, runtime, manager, _runner = _harness_runtime(tmp_path)
    snapshot, _payload, binding = _install_fixed_gateway_authority(runtime)
    runtime._verify_runtime_final_exit = MagicMock(
        side_effect=RuntimeError("confirmed fixed Gateway data-plane failure")
    )
    runtime.test_group_delay = MagicMock(
        side_effect=RuntimeError("gateway chain remains unavailable")
    )
    outcome = RuntimeRecoveryOutcome(
        status=RuntimeRecoveryStatus.FAIL_CLOSED,
        incident_id="fixed-gateway-incident",
        route_epoch=snapshot.route_epoch,
        budget_ms=250,
        reason="confirmed_gateway_failure",
        from_ref=binding.node,
    )
    runtime._recover_initial_final_exit = MagicMock(
        side_effect=RuntimeActivationRecoveryError(outcome)
    )
    runtime._hot_reload_runtime_config_phase = MagicMock()
    runtime._apply_system_proxy_capture_after_core_start = MagicMock()

    runtime._queue_tier3_egress_activation()
    assert runtime._wait_for_tier3_egress_activation(timeout=1.0)

    assert runtime._current_core_runtime_phase() == "formal_no_capture"
    assert manager.tier3_traffic_capture_active is False
    assert _readiness_name(runtime) == "data_plane_failed"
    runtime._recover_initial_final_exit.assert_not_called()
    runtime._hot_reload_runtime_config_phase.assert_not_called()
    runtime._apply_system_proxy_capture_after_core_start.assert_not_called()


def test_unknown_provider_snapshot_does_not_revoke_or_requeue_serving(
    tmp_path: Path,
) -> None:
    harness, runtime, manager, _runner = _harness_runtime(tmp_path)
    _publish_injected_non_gateway_authority(harness, runtime)
    runtime._set_core_runtime_phase("formal_capture")
    manager.tier3_traffic_capture_active = True
    runtime._runtime_initial_activation_generation = runtime._core_generation
    runtime._set_effective_egress_state(True, "")
    runtime._publish_runtime_egress_readiness(
        RuntimeEgressReadinessState.READY,
        validation_active=False,
    )
    snapshot_refresh = MagicMock()
    revalidation = MagicMock()
    runtime._queue_runtime_route_snapshot_refresh = snapshot_refresh
    runtime._queue_egress_revalidation = revalidation

    runtime._handle_unknown_tier3_provider_state("provider snapshot returned None")
    runtime._handle_unknown_tier3_provider_state("provider snapshot returned None")

    assert runtime._current_core_runtime_phase() == "formal_capture"
    assert manager.tier3_traffic_capture_active is True
    assert manager.effective_egress_ok is True
    assert _readiness_name(runtime) in {"ready", "serving"}
    assert snapshot_refresh.call_count <= 1
    assert revalidation.call_count <= 1


def test_same_runtime_signature_reuses_one_background_recovery_arm(
    tmp_path: Path,
) -> None:
    harness, runtime, manager, _runner = _harness_runtime(tmp_path)
    _publish_injected_non_gateway_authority(harness, runtime)
    runtime._set_core_runtime_phase("formal_capture")
    manager.tier3_traffic_capture_active = True
    runtime._runtime_initial_activation_generation = runtime._core_generation
    runtime._set_effective_egress_state(True, "")
    runtime._publish_runtime_egress_readiness(
        RuntimeEgressReadinessState.READY,
        validation_active=False,
    )

    assert runtime._try_arm_runtime_recovery() is True
    first_arm = runtime._runtime_recovery_arm
    assert first_arm is not None
    assert runtime._try_arm_runtime_recovery() is True

    assert runtime._runtime_recovery_arm is first_arm


def test_non_gateway_recovery_never_activates_killswitch_or_physical_fail_closed(
    tmp_path: Path,
) -> None:
    harness = recovery_fixtures.DesktopRuntimeRecoveryTests()
    with patch("desktop_runtime.setup_logging", return_value=_quiet_runtime_logger()):
        runtime, _manager, _bad, _good, snapshot, payload = (
            harness._global_runtime_failure_fixture(tmp_path)
        )
    binding = runtime._runtime_route_binding_for_outbound(
        snapshot,
        payload,
        "GENERAL",
    )
    assert binding is not None
    assert binding.node.pool_kind != "gateway"
    failure = BoundRuntimeFailure(
        binding=binding,
        protocol="TCP",
        observed_outbound="GENERAL",
        destination_token="non-gateway-regression",
        error_type="timeout",
        observed_at_monotonic=time.monotonic(),
    )
    first = runtime._runtime_egress_coordinator.observe(
        failure,
        current_snapshot=snapshot,
    )
    confirmed = runtime._runtime_egress_coordinator.record_probe(
        failure,
        succeeded=False,
        probe_token=first.probe_token,
        current_snapshot=snapshot,
    )
    assert confirmed.confirmed is True
    alternative = next(
        ref for ref in snapshot.proxy_nodes if ref.proxy_name == "good-next"
    )
    runtime._activate_killswitch_for_egress_failure = MagicMock(
        side_effect=AssertionError("non-Gateway recovery must not activate killswitch")
    )
    runtime._enforce_runtime_fail_closed = MagicMock(
        side_effect=AssertionError(
            "non-Gateway recovery must not physically fail closed"
        )
    )
    runtime._fast_recover_confirmed_alternative = MagicMock(
        return_value={
            "fast_path_available": True,
            "post_switch_connectivity_verified": True,
            "effective_egress_ok": True,
            "selected_chain": ["FINAL-EXIT", alternative.proxy_name],
            "alternative_leaf": alternative.proxy_name,
            "post_switch_result": {"status": 204},
            "loaded_route_epoch": snapshot.route_epoch + 1,
            "selection_action": {
                "group": "TIER1-AUTO",
                "target": alternative.proxy_name,
                "mode": "fixed",
            },
        }
    )
    runtime._start_runtime_connection_cleanup = lambda _binding: None

    runtime._start_runtime_recovery(
        failure,
        snapshot,
        payload,
        incident_id=confirmed.incident_id,
        quarantine_token=confirmed.probe_token,
        confirmed_alternative=alternative,
    )

    assert harness._wait_until(
        lambda: not runtime._runtime_failure_recovery_threads,
        timeout=1.0,
    )
    runtime._activate_killswitch_for_egress_failure.assert_not_called()
    runtime._enforce_runtime_fail_closed.assert_not_called()


def test_non_gateway_serving_waits_for_the_formal_listener(tmp_path: Path) -> None:
    _harness, runtime, manager, runner = _harness_runtime(tmp_path)
    reloads: list[bool] = []

    def reload_phase(_runner, *, capture_enabled: bool, **_kwargs):
        reloads.append(bool(capture_enabled))
        runtime._core_runtime_phase = (
            "formal_capture" if capture_enabled else "formal_no_capture"
        )
        return tmp_path / "mihomo-runtime.yaml"

    runtime._hot_reload_runtime_config_phase = reload_phase
    runtime._verify_runtime_final_exit = MagicMock(
        side_effect=RuntimeError("formal listener is unavailable")
    )
    runtime.test_group_delay = MagicMock(
        side_effect=RuntimeError("formal listener is unavailable")
    )
    runner.port_ok = False
    runner.wait_for_proxy = MagicMock(return_value=False)
    apply_capture = MagicMock()
    runtime._apply_system_proxy_capture_after_core_start = apply_capture

    runtime._queue_tier3_egress_activation()
    assert runtime._wait_for_tier3_egress_activation(timeout=3.0)

    assert reloads == []
    runner.wait_for_proxy.assert_not_called()
    apply_capture.assert_not_called()
    assert manager.tier3_traffic_capture_active is False
    assert runtime.traffic_monitor.started == 0
    assert _readiness_name(runtime) not in {"serving", "ready"}


def test_core_health_facility_faults_do_not_activate_the_global_killswitch(
    tmp_path: Path,
) -> None:
    _harness, runtime, _manager, runner = _harness_runtime(tmp_path)
    activate = MagicMock(
        side_effect=AssertionError(
            "listener/process health is not a confirmed fixed Gateway failure"
        )
    )
    runtime._activate_killswitch_for_egress_failure = activate
    runtime._system_proxy_capture_state = lambda: (True, True)

    runtime.check_proxy_port = MagicMock(return_value=False)
    runtime._monitor_core_health()

    runner.running = False
    runner.status_payload["running"] = False
    runtime.running = False
    runtime._monitor_core_health()

    runtime.network_mode = "tun"
    runtime.system_proxy.active = False
    runtime._monitor_core_health()

    activate.assert_not_called()


def test_monitor_cycle_routes_provider_work_through_the_keyed_scheduler(
    tmp_path: Path,
) -> None:
    _harness, runtime, _manager, _runner = _harness_runtime(tmp_path)

    class OneCycleStopEvent:
        def __init__(self) -> None:
            self.stopped = False

        def is_set(self) -> bool:
            return self.stopped

        def wait(self, _timeout: float = 0.0) -> bool:
            self.stopped = True
            return True

    runtime.stop_event = OneCycleStopEvent()
    runtime.manager = MagicMock()
    runtime.manager.run_cycle = MagicMock()
    runtime._write_runtime_lifecycle_heartbeat_if_due = MagicMock()
    runtime._sync_effective_egress_state_from_core = MagicMock()
    runtime._maybe_probe_runtime_failure_guard = MagicMock()
    runtime._wake_parked_tier3_pool_sync_if_causal_key_changed = MagicMock()
    runtime._queue_tier3_pool_sync_after_commit = MagicMock()
    runtime._maybe_retry_tier3_standby_activation = MagicMock()
    runtime._maybe_revalidate_tier3_runtime_qualification = MagicMock()
    runtime._monitor_core_health = MagicMock()
    runtime._remote_refresh_egress_proxy_config = MagicMock(return_value=None)
    runtime._clear_last_error_if_runtime_healthy = MagicMock()
    direct_sync = MagicMock(
        side_effect=AssertionError("monitor loop bypassed keyed provider sync")
    )
    runtime._sync_core_config_after_pool_update = direct_sync

    runtime._monitor_loop()

    runtime.manager.run_cycle.assert_called_once()
    runtime._wake_parked_tier3_pool_sync_if_causal_key_changed.assert_called_once()
    runtime._queue_tier3_pool_sync_after_commit.assert_called_once()
    direct_sync.assert_not_called()


def test_serving_pool_growth_only_records_deferred_capacity(
    tmp_path: Path,
) -> None:
    _harness, runtime, _manager, _runner = _harness_runtime(tmp_path)
    runtime._set_core_runtime_phase("formal_capture")
    runtime._record_tier3_core_sync_ledger = MagicMock()
    runtime._tier3_pool_target_reached = lambda: False
    runtime._update_running_tier3_core_provider = MagicMock(
        side_effect=AssertionError(
            "background pool growth must not mutate the online provider"
        )
    )
    route_epoch = runtime._runtime_route_epoch
    activation_key = runtime._tier3_standby_activation_key()
    latest_key = None

    for revision in range(8, 26):
        latest_key = DeferredProviderSyncKey(
            runtime_generation=runtime._runtime_generation,
            core_generation=runtime._core_generation,
            pool_revision=revision,
            provider_digest=f"candidate-{revision}",
            loaded_digest="loaded-cohort",
            network_epoch=runtime._runtime_network_context_epoch,
            facility_epoch=runtime._runtime_facility_epoch,
            serving=True,
        )
        runtime._tier3_provider_sync_key = lambda value=latest_key: value
        assert runtime._queue_tier3_pool_sync_after_commit() is False

    assert runtime._tier3_pool_sync_pending is False
    assert runtime._tier3_pool_sync_worker_running is False
    assert runtime._tier3_pool_sync_parked_key == latest_key
    assert runtime._runtime_route_epoch == route_epoch
    assert runtime._tier3_standby_activation_key() == activation_key
    runtime._update_running_tier3_core_provider.assert_not_called()


def test_exhausted_loaded_candidates_admit_one_incident_owned_capacity_rebase(
    tmp_path: Path,
) -> None:
    _harness, runtime, manager, _runner = _harness_runtime(tmp_path)
    current = RuntimeNodeRef("tier3", "tier3b", "identity-a", "A")
    added = RuntimeNodeRef("tier3", "tier3b", "identity-b", "B")
    now = time.monotonic()
    old_snapshot = CoreLoadedRouteSnapshot(
        process_pid=4242,
        process_generation=1,
        core_generation=runtime._core_generation,
        semantic_digest="frozen-route-authority",
        route_epoch=7,
        network_context_epoch=runtime._runtime_network_context_epoch,
        group_graph=(
            ("FINAL-EXIT", ("TIER3-AUTO", "REJECT")),
            ("TIER3-AUTO", ("A",)),
        ),
        proxy_nodes=(current,),
        loaded_at_monotonic=now,
    )
    new_snapshot = CoreLoadedRouteSnapshot(
        process_pid=old_snapshot.process_pid,
        process_generation=old_snapshot.process_generation,
        core_generation=old_snapshot.core_generation,
        semantic_digest=old_snapshot.semantic_digest,
        route_epoch=old_snapshot.route_epoch,
        network_context_epoch=old_snapshot.network_context_epoch,
        group_graph=(
            ("FINAL-EXIT", ("TIER3-AUTO", "REJECT")),
            ("TIER3-AUTO", ("A", "B")),
        ),
        proxy_nodes=(current, added),
        loaded_at_monotonic=now,
    )
    old_payload = {
        "proxies": {
            "proxies": {
                "FINAL-EXIT": {
                    "type": "Selector",
                    "now": "TIER3-AUTO",
                    "all": ["TIER3-AUTO", "REJECT"],
                },
                "TIER3-AUTO": {
                    "type": "Fallback",
                    "now": "A",
                    "fixed": "A",
                    "all": ["A"],
                },
                "A": {"type": "Shadowsocks"},
            }
        },
        "proxy_map": {
            "A": {
                "tier": current.tier,
                "pool_kind": current.pool_kind,
                "identity": current.identity,
            }
        },
    }
    new_payload = {
        "proxies": {
            "proxies": {
                **old_payload["proxies"]["proxies"],
                "TIER3-AUTO": {
                    "type": "Fallback",
                    "now": "A",
                    "fixed": "A",
                    "all": ["A", "B"],
                },
                "B": {"type": "Shadowsocks"},
            }
        },
        "proxy_map": {
            **old_payload["proxy_map"],
            "B": {
                "tier": added.tier,
                "pool_kind": added.pool_kind,
                "identity": added.identity,
            },
        },
    }
    failure = BoundRuntimeFailure(
        binding=RuntimeRouteBinding(
            process_pid=old_snapshot.process_pid,
            process_generation=old_snapshot.process_generation,
            core_generation=old_snapshot.core_generation,
            semantic_digest=old_snapshot.semantic_digest,
            route_epoch=old_snapshot.route_epoch,
            network_context_epoch=old_snapshot.network_context_epoch,
            observed_outbound="FINAL-EXIT",
            route_chain=("FINAL-EXIT", "TIER3-AUTO", "A"),
            node=current,
        ),
        protocol="TCP",
        observed_outbound="FINAL-EXIT",
        destination_token="opaque-capacity-incident",
        error_type="timeout",
        observed_at_monotonic=now,
    )
    ticket = _RuntimeRecoveryTicket(
        generation=1,
        incident_id="capacity-incident",
        observed_at_monotonic=now,
        deadline_monotonic=now + 6.0,
        snapshot_key=old_snapshot.snapshot_key,
        route_epoch=old_snapshot.route_epoch,
        facility_epoch=runtime._runtime_facility_epoch,
        selector_intent_revision=runtime._runtime_selector_intent_revision,
        worker_token=tuple(runtime._runtime_worker_token()),
        from_ref=current,
    )
    runtime._runtime_recovery_tickets[ticket.incident_id] = ticket
    runtime._runtime_recovery_outcomes.pop(ticket.incident_id, None)
    runtime._runtime_incident_provider_rebases.clear()
    manager.runtime_candidate_snapshot = MagicMock(
        return_value=RuntimeCandidateSnapshot(
            revision=25,
            runtime_generation=runtime._runtime_generation,
            candidates=(
                RuntimeCandidateState(
                    tier=current.tier,
                    pool_kind=current.pool_kind,
                    identity=current.identity,
                    raw_status="alive",
                    effective_status="alive",
                    effective_usable=True,
                    incident_active=False,
                    overlay_active=False,
                    pending_recheck=False,
                ),
                RuntimeCandidateState(
                    tier=added.tier,
                    pool_kind=added.pool_kind,
                    identity=added.identity,
                    raw_status="alive",
                    effective_status="alive",
                    effective_usable=True,
                    incident_active=False,
                    overlay_active=False,
                    pending_recheck=False,
                ),
            ),
        )
    )
    runtime._tier3_core_sync_marker = MagicMock(return_value=("pool", 25))

    def commit_rebase(_marker, **kwargs):
        assert kwargs["recovery_ticket"] is ticket
        assert kwargs["preserve_route_authority"] is True
        with runtime._runtime_route_snapshot_lock:
            runtime._runtime_route_snapshot = new_snapshot
            runtime._runtime_route_snapshot_payload = new_payload
        runtime._runtime_egress_coordinator.set_current_snapshot(new_snapshot)
        return {"updated": True, "reason": "append-only capacity committed"}

    runtime._update_running_tier3_core_provider = MagicMock(
        side_effect=commit_rebase
    )
    with runtime._runtime_route_snapshot_lock:
        runtime._runtime_route_snapshot = old_snapshot
        runtime._runtime_route_snapshot_payload = old_payload
    runtime._runtime_egress_coordinator.set_current_snapshot(old_snapshot)

    refreshed, payload, result = runtime._try_runtime_incident_provider_rebase(
        failure,
        old_snapshot,
        old_payload,
        recovery_ticket=ticket,
        deadline_monotonic=now + 4.0,
    )
    duplicate_snapshot, duplicate_payload, duplicate = (
        runtime._try_runtime_incident_provider_rebase(
            failure,
            old_snapshot,
            old_payload,
            recovery_ticket=ticket,
            deadline_monotonic=now + 4.0,
        )
    )

    assert refreshed is new_snapshot
    assert payload == new_payload
    assert refreshed.snapshot_key == old_snapshot.snapshot_key
    assert result["attempted"] is True
    assert result["updated"] is True
    assert duplicate_snapshot is None
    assert duplicate_payload is None
    assert duplicate["attempted"] is False
    runtime._update_running_tier3_core_provider.assert_called_once()


def test_background_route_snapshot_refresh_is_bounded(tmp_path: Path) -> None:
    _harness, runtime, _manager, _runner = _harness_runtime(tmp_path)
    runtime._runtime_worker_token_is_current = lambda _token: True
    refresh = MagicMock(return_value=None)
    runtime._refresh_runtime_route_snapshot = refresh

    runtime._queue_runtime_route_snapshot_refresh()
    deadline = time.monotonic() + 1.0
    while runtime._runtime_route_snapshot_refresh_running:
        assert time.monotonic() < deadline
        time.sleep(0.005)

    refresh.assert_called_once()
    assert refresh.call_args.kwargs["timeout_seconds"] == pytest.approx(0.75)
