"""Real Core errors retain observation authority across native route changes."""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core_runner import CoreOutputEvent
from desktop_runtime import (
    DesktopRuntime,
    _RuntimeMonitoringLease,
    _RuntimeRecoveryArm,
    _RuntimeRecoveryTicket,
)
from runtime_egress_recovery import BoundRuntimeFailure, RuntimeEgressCoordinator
from tests.test_runtime_ready_arm_regressions import _loaded_authority


def _event(**changes):
    event = CoreOutputEvent(
        raw_line='level=warning msg="[TCP] dial FINAL-EXIT (match Match/) '
        '127.0.0.1:55001 --> example.invalid:443 error: context deadline exceeded"',
        pid=4312, process_generation=7, received_at_monotonic=time.monotonic(),
    )
    return replace(event, **changes)


def _runtime_case(*, gateway=False):
    runtime = object.__new__(DesktopRuntime)
    snapshot, payload, binding = _loaded_authority(
        pool_kind="gateway" if gateway else "tier1_variant",
    )
    runtime.config = {}
    runtime.stop_event = threading.Event()
    runtime._runtime_generation = 4
    runtime._core_generation = 3
    runtime._lifecycle_revision = 9
    runtime._runtime_initial_activation_generation = 3
    runtime._runtime_selector_intent_revision = 2
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_failure_monitor_thread = None
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_monitoring_lease = _RuntimeMonitoringLease(
        4, 3, 4312, 7, 9, time.monotonic() - 1,
    )
    runtime._runtime_passive_refresh_key = None
    runtime._runtime_passive_refresh_until = 0.0
    runtime._runtime_failure_probe_bindings = set()
    runtime._runtime_failure_recovery_bindings = set()
    runtime._runtime_failure_recovery_threads = set()
    runtime._runtime_incident_bindings = {}
    runtime._runtime_incident_route_payloads = {}
    runtime._runtime_recovery_is_armed = Mock(return_value=False)
    runtime._runtime_initial_activation_complete = Mock(return_value=False)
    runtime.core_runner = SimpleNamespace(
        is_running=lambda: True,
        status=lambda: {"pid": 4312, "process_generation": 7},
    )
    runtime.manager = SimpleNamespace(enqueue_node_health_check=Mock())
    runtime._runtime_egress_recovery_profile = "early_single_coordinator"
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
    runtime._runtime_route_snapshot = snapshot
    runtime._runtime_route_snapshot_payload = payload
    runtime._runtime_route_snapshot_state = lambda: (
        runtime._runtime_route_snapshot, runtime._runtime_route_snapshot_payload,
    )
    runtime._runtime_worker_token = lambda: (4, 1, 3, 4312, 7, 0)
    runtime._runtime_worker_token_is_current = Mock(return_value=True)
    runtime._runtime_transport_failure_is_internal_probe = Mock(return_value=False)
    runtime._runtime_local_listener_fault_from_core_output = Mock(return_value=None)
    runtime._runtime_route_attribution_barrier_active = Mock(return_value=False)
    runtime._start_runtime_early_confirmation = Mock()
    runtime._probe_mihomo_mixed_port_once = Mock()

    def refresh(**_kwargs):
        runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
        return snapshot, payload, binding

    runtime._refresh_loaded_runtime_authority = Mock(side_effect=refresh)
    return runtime, snapshot, payload, binding


def _consume(runtime, snapshot, payload, event=None):
    runtime._handle_runtime_output_event_early(
        event or _event(), snapshot, payload, ingress_selector_intent_revision=2,
    )


def test_monitoring_lease_survives_unhealthy_or_native_unarmed_route():
    runtime, _, _, _ = _runtime_case()
    assert runtime._runtime_monitoring_is_active()
    runtime._runtime_recovery_is_armed.assert_not_called()
    runtime._runtime_initial_activation_complete.assert_not_called()


def test_monitoring_permission_is_committed_after_ready_and_not_renewed_by_rearm():
    runtime, snapshot, _, binding = _runtime_case()
    runtime._runtime_monitoring_lease = None
    runtime._runtime_recovery_arm = _RuntimeRecoveryArm(
        worker_token=runtime._runtime_worker_token(), snapshot_key=snapshot.snapshot_key,
        binding_key=binding.node.key, route_epoch=snapshot.route_epoch,
        facility_epoch=0, selector_intent_revision=2, lifecycle_revision=9,
        armed_at_monotonic=time.monotonic(),
    )
    assert not runtime._commit_runtime_monitoring_lease()
    assert runtime._runtime_monitoring_lease is None
    runtime._runtime_initial_activation_complete.return_value = True
    runtime._runtime_recovery_is_armed.return_value = True
    assert runtime._commit_runtime_monitoring_lease()
    lease = runtime._runtime_monitoring_lease
    assert runtime._commit_runtime_monitoring_lease()
    assert runtime._runtime_monitoring_lease is lease
    runtime._refresh_loaded_runtime_authority.assert_not_called()
    runtime._probe_mihomo_mixed_port_once.assert_not_called()


@pytest.mark.parametrize("change", ["lease", "core", "runtime", "lifecycle", "stop", "disabled", "admission"])
def test_monitoring_rejects_unadmitted_or_replaced_core(change):
    runtime, _, _, _ = _runtime_case()
    if change == "lease":
        runtime._runtime_monitoring_lease = None
    elif change == "stop":
        runtime.stop_event.set()
    elif change == "disabled":
        runtime.config = {"client": {"egress_recovery": {"enabled": False}}}
    else:
        attribute = {
            "core": "_core_generation", "runtime": "_runtime_generation",
            "lifecycle": "_lifecycle_revision", "admission": "_runtime_initial_activation_generation",
        }[change]
        setattr(runtime, attribute, -1)
    assert not runtime._runtime_monitoring_is_active()


@pytest.mark.parametrize("missing_snapshot", [False, True])
def test_reader_enqueues_unarmed_error_without_controller_or_network_io(missing_snapshot):
    runtime, _, _, _ = _runtime_case()
    if missing_snapshot:
        runtime._runtime_route_snapshot = None
        runtime._runtime_route_snapshot_payload = None
    runtime._on_core_output_event(_event())
    assert runtime._runtime_egress_coordinator.next_ingress() is not None
    runtime._refresh_loaded_runtime_authority.assert_not_called()
    runtime._probe_mihomo_mixed_port_once.assert_not_called()


def test_first_error_enters_confirmation_without_health_pass_and_burst_shares_refresh():
    runtime, snapshot, payload, binding = _runtime_case()
    for _ in range(8):
        _consume(runtime, snapshot, payload)
    assert runtime._refresh_loaded_runtime_authority.call_count == 1
    runtime._start_runtime_early_confirmation.assert_called_once()
    assert runtime._start_runtime_early_confirmation.call_args.args[0].binding.node == binding.node
    runtime._probe_mihomo_mixed_port_once.assert_not_called()
    runtime.manager.enqueue_node_health_check.assert_not_called()


@pytest.mark.parametrize("change", ["intent", "expired", "pre_admission", "pid", "controller", "worker"])
def test_stale_or_uncertain_event_cannot_accuse_or_switch(change):
    runtime, snapshot, payload, _ = _runtime_case()
    event = _event()
    if change == "intent":
        runtime._runtime_selector_intent_revision = 3
    elif change == "expired":
        event = replace(event, received_at_monotonic=time.monotonic() - 7)
    elif change == "pre_admission":
        runtime._runtime_monitoring_lease = replace(
            runtime._runtime_monitoring_lease, admitted_at_monotonic=time.monotonic() + 1,
        )
    elif change == "pid":
        event = replace(event, pid=99)
    elif change == "controller":
        runtime._refresh_loaded_runtime_authority.side_effect = TimeoutError("controller unavailable")
    else:
        runtime._runtime_worker_token_is_current.return_value = False
    _consume(runtime, snapshot, payload, event)
    runtime._start_runtime_early_confirmation.assert_not_called()
    runtime.manager.enqueue_node_health_check.assert_not_called()


def test_native_switch_rechecks_retired_identity_without_transferring_its_failure():
    runtime, old, old_payload, old_binding = _runtime_case()
    fresh, payload, binding = _loaded_authority(identity="leaf-b-id", proxy_name="leaf-b")
    fresh = replace(fresh, route_epoch=old.route_epoch + 1, proxy_nodes=(*fresh.proxy_nodes, old_binding.node))
    binding = replace(binding, route_epoch=fresh.route_epoch)

    def refresh(**_kwargs):
        runtime._runtime_route_snapshot = fresh
        runtime._runtime_route_snapshot_payload = payload
        runtime._runtime_egress_coordinator.set_current_snapshot(fresh)
        return fresh, payload, binding

    runtime._refresh_loaded_runtime_authority.side_effect = refresh
    for _ in range(5):
        _consume(runtime, old, old_payload)
    runtime.manager.enqueue_node_health_check.assert_called_once_with(
        old_binding.node, reason="native_route_drift", source="runtime_transport",
    )
    signal = runtime._start_runtime_early_confirmation.call_args.args[0]
    assert signal.binding.node == binding.node
    assert signal.error_type == "route_changed_after_transport_failure"
    assert signal.protocol == "PROBE"
    assert not signal.deterministic_proxy_endpoint
    runtime._start_runtime_early_confirmation.assert_called_once()


def test_fixed_gateway_without_underlay_gets_own_authoritative_check():
    runtime, snapshot, payload, binding = _runtime_case(gateway=True)
    runtime._runtime_gateway_underlay_binding = Mock(return_value=None)
    _consume(runtime, snapshot, payload)
    runtime.manager.enqueue_node_health_check.assert_called_once_with(
        binding.node, reason="gateway_runtime_error", source="runtime_transport",
    )
    runtime._start_runtime_early_confirmation.assert_not_called()


def _idle_core_lines():
    # Idle Core output: app-origin opens (a private source lane with no
    # ``match``, a system-rule probe host), a DIRECT open and non-TCP noise.
    # None of them is user traffic on a TIER3 leaf.
    now = datetime.now(timezone.utc).isoformat()
    return [
        f'time="{now}" level=info msg="[TCP] 127.0.0.1:61001 --> 198.51.100.7:443 '
        'using FINAL-EXIT[leaf-a]"',
        f'time="{now}" level=info msg="[TCP] 127.0.0.1:61002 --> www.gstatic.com:443 '
        'match Domain(www.gstatic.com) using FINAL-EXIT[leaf-a]"',
        f'time="{now}" level=info msg="[TCP] 127.0.0.1:61003 --> lan.test:80 '
        'match Match using DIRECT"',
        f'time="{now}" level=debug msg="[Sniffer] sniffed nothing"',
    ]


@pytest.mark.parametrize("observer", [False, True], ids=["no-observer", "post-dial-observer"])
def test_idle_consumer_does_not_poll_controller_or_probe_egress(observer):
    runtime, _, _, _ = _runtime_case()
    runtime._maybe_watch_runtime_path = Mock()
    runtime._maybe_probe_runtime_failure_guard = Mock()
    controller = Mock()  # any attribute call on it would be a controller call
    runner = None
    if observer:
        # Node judgement P1-3 (default post_dial_judge: shadow): with the
        # observer running, an idle Core still costs 0 controller calls.
        runtime._runtime_post_dial_observer_lock = threading.Lock()
        runtime._runtime_post_dial_observer = None
        runtime._runtime_post_dial_observer_lease = None
        runtime._runtime_post_dial_tap = None
        runtime._runtime_post_dial_controller = Mock(return_value=controller)
        assert runtime._ensure_runtime_post_dial_observer(runtime._runtime_monitoring_lease)
        runner = runtime._runtime_post_dial_observer
        lines = _idle_core_lines()
        for raw in lines:
            runtime._on_core_output_event(_event(raw_line=raw))
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not (
            runner.metrics["lines_drained"] == 3 and runner.tap._wake_armed
        ):
            time.sleep(0.005)
        assert runner.metrics["lines_drained"] == 3  # the debug line is never tapped
        time.sleep(0.3)

    def idle(**_kwargs):
        runtime.stop_event.set()
        return None

    runtime._runtime_egress_coordinator.next_ingress = idle
    runtime._runtime_failure_monitor_loop()
    runtime._maybe_watch_runtime_path.assert_not_called()
    runtime._maybe_probe_runtime_failure_guard.assert_not_called()
    runtime._refresh_loaded_runtime_authority.assert_not_called()
    if observer:
        runtime._runtime_post_dial_controller.assert_not_called()
        assert controller.mock_calls == []
        assert runner.metrics["controller_calls"] == 0
        assert runtime._stop_runtime_post_dial_observer("test", join_timeout=2.0)
        assert not runner.thread.is_alive()


@pytest.mark.parametrize("gateway,underlay_passed", [(False, False), (True, True), (True, False)])
def test_no_backup_still_requests_correct_authoritative_node_check(gateway, underlay_passed):
    runtime, snapshot, payload, binding = _runtime_case()
    _, _, gateway_binding = _loaded_authority(pool_kind="gateway", identity="gateway", proxy_name="gateway")
    observed = time.monotonic()
    failure = BoundRuntimeFailure(
        binding=binding, protocol="TCP", observed_outbound="FINAL-EXIT",
        destination_token="destination", error_type="timeout", observed_at_monotonic=observed,
    )
    ticket = _RuntimeRecoveryTicket(
        generation=1, incident_id="incident", observed_at_monotonic=observed,
        deadline_monotonic=observed + 6, snapshot_key=snapshot.snapshot_key,
        route_epoch=snapshot.route_epoch, facility_epoch=0,
        selector_intent_revision=2, worker_token=runtime._runtime_worker_token(), from_ref=binding.node,
    )
    runtime._ensure_runtime_recovery_ticket = Mock(return_value=ticket)
    runtime._runtime_early_differential_plans = Mock(return_value=[])
    runtime._complete_runtime_early_indeterminate = Mock()
    runtime._probe_runtime_leaf = Mock(return_value={"delay": 9} if underlay_passed else None)
    DesktopRuntime._start_runtime_early_confirmation(
        runtime, failure, snapshot, payload, probe_token="probe", incident_id="incident",
        gateway_ref=gateway_binding.node if gateway else None,
    )
    for worker in tuple(runtime._runtime_failure_recovery_threads):
        worker.join(2)
        assert not worker.is_alive()
    wanted = gateway_binding.node if gateway and underlay_passed else binding.node
    assert runtime.manager.enqueue_node_health_check.call_args.args == (wanted,)
    if not gateway:
        runtime._probe_runtime_leaf.assert_not_called()


def test_gateway_failed_chain_with_healthy_underlay_checks_gateway_only():
    runtime, snapshot, payload, binding = _runtime_case()
    _, _, gateway = _loaded_authority(pool_kind="gateway", identity="gateway", proxy_name="gateway")
    alternative = replace(binding.node, identity="other", proxy_name="other")
    observed = time.monotonic()
    failure = BoundRuntimeFailure(
        binding=binding, protocol="TCP", observed_outbound="FINAL-EXIT",
        destination_token="destination", error_type="timeout", observed_at_monotonic=observed,
    )
    ticket = _RuntimeRecoveryTicket(
        generation=1, incident_id="incident", observed_at_monotonic=observed,
        deadline_monotonic=observed + 6, snapshot_key=snapshot.snapshot_key,
        route_epoch=snapshot.route_epoch, facility_epoch=0,
        selector_intent_revision=2, worker_token=runtime._runtime_worker_token(), from_ref=binding.node,
    )
    runtime._ensure_runtime_recovery_ticket = Mock(return_value=ticket)
    runtime._runtime_early_differential_plans = Mock(return_value=[{
        "ref": alternative, "action": {"group": "TIER1-AUTO", "target": "other"},
        "probe_url": "https://probe.invalid/generate_204", "expected_status": "204",
    }])
    runtime._complete_runtime_early_indeterminate = Mock()
    runtime._runtime_confirmation_timeout_ms = Mock(return_value=500)
    runtime._probe_runtime_leaf = Mock(return_value={"delay": 9})
    runtime._run_runtime_early_confirmed_recovery = Mock()
    DesktopRuntime._start_runtime_early_confirmation(
        runtime, failure, snapshot, payload, probe_token="probe", incident_id="incident",
        gateway_ref=gateway.node,
    )
    for worker in tuple(runtime._runtime_failure_recovery_threads):
        worker.join(2)
        assert not worker.is_alive()
    runtime.manager.enqueue_node_health_check.assert_called_once_with(
        gateway.node, reason="gateway_runtime_error", source="runtime_transport",
    )
    runtime._run_runtime_early_confirmed_recovery.assert_not_called()
    assert runtime._complete_runtime_early_indeterminate.call_args.kwargs["reason_code"] == "current_leaf_healthy"
