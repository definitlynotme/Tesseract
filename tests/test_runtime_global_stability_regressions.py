from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace

import pytest

from core_runner import CoreOutputEvent
from desktop_runtime import DesktopRuntime, _HotStandbyEvidence
from runtime_egress_recovery import (
    BoundRuntimeFailure,
    CoreLoadedRouteSnapshot,
    RuntimeEgressCoordinator,
    RuntimeNodeRef,
    RuntimeRouteBinding,
)
from runtime_reload_gate import RuntimeReloadGate


def _wait_for_runtime_workers(runtime: DesktopRuntime) -> None:
    deadline = time.monotonic() + 1.0
    while runtime._runtime_failure_recovery_threads and time.monotonic() < deadline:
        time.sleep(0.005)
    assert runtime._runtime_failure_recovery_threads == set()


def _snapshot(*refs: RuntimeNodeRef) -> CoreLoadedRouteSnapshot:
    return CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        proxy_nodes=refs,
    )


def _binding(
    snapshot: CoreLoadedRouteSnapshot,
    *,
    outbound: str,
    chain: tuple[str, ...],
    node: RuntimeNodeRef,
) -> RuntimeRouteBinding:
    return RuntimeRouteBinding(
        process_pid=snapshot.process_pid,
        process_generation=snapshot.process_generation,
        core_generation=snapshot.core_generation,
        semantic_digest=snapshot.semantic_digest,
        route_epoch=snapshot.route_epoch,
        observed_outbound=outbound,
        route_chain=chain,
        node=node,
    )


def test_fixed_selection_unknown_commit_is_rejected_before_snapshot_revocation() -> None:
    runtime = object.__new__(DesktopRuntime)

    class Client:
        @staticmethod
        def select_proxy_with_readback(*_args, **_kwargs):
            return {
                "group": "PROXY-FALLBACK",
                "target": "standby",
                "mode": "fixed",
                "commit_state": "unknown",
                "now": "standby",
                "fixed": None,
            }

    runtime._require_api_client = lambda: Client()
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_route_epoch = 9
    marker = object()
    runtime._runtime_route_snapshot = marker
    runtime._runtime_route_snapshot_payload = {"loaded": True}
    runtime._runtime_route_selection_signature = ("loaded",)
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()

    with pytest.raises(RuntimeError, match="not authoritatively committed"):
        runtime._apply_runtime_automatic_selection(
            {
                "group": "PROXY-FALLBACK",
                "target": "standby",
                "mode": "fixed",
            },
            deadline_monotonic=time.monotonic() + 1.0,
        )

    assert runtime._runtime_route_epoch == 9
    assert runtime._runtime_route_snapshot is marker
    assert runtime._runtime_route_selection_signature == ("loaded",)


def test_fixed_release_without_fixed_field_remains_unknown() -> None:
    runtime = object.__new__(DesktopRuntime)

    class Client:
        @staticmethod
        def unfix_proxy(_group: str, **_kwargs):
            return {"fixed": False}

        @staticmethod
        def proxy(_group: str, **_kwargs):
            return {"name": "PROXY-FALLBACK", "now": "original"}

    runtime._require_api_client = lambda: Client()
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_route_epoch = 9
    runtime._runtime_route_snapshot = object()
    runtime._runtime_route_snapshot_payload = {"loaded": True}
    runtime._runtime_route_selection_signature = ("loaded",)
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()

    result = runtime._release_runtime_automatic_fixed_selection(
        {"mode": "fixed", "group": "PROXY-FALLBACK"},
        deadline_monotonic=time.monotonic() + 1.0,
    )

    assert result["commit_state"] == "unknown"
    assert "fixed" not in result["readback"]


def test_gateway_dialer_outbound_binds_to_managed_underlay() -> None:
    runtime = object.__new__(DesktopRuntime)
    gateway = RuntimeNodeRef("exit", "gateway", "gateway-id", "gateway")
    underlay = RuntimeNodeRef(
        "tier1", "tier1_variant", "underlay-id", "underlay"
    )
    snapshot = _snapshot(gateway, underlay)
    payload = {
        "proxies": {
            "proxies": {
                "PROXY-FALLBACK": {
                    "type": "Fallback",
                    "now": "underlay",
                    "all": ["underlay"],
                },
                "underlay": {"type": "Shadowsocks"},
            }
        },
        "proxy_map": {
            "gateway": {
                "tier": "exit",
                "pool_kind": "gateway",
                "identity": "gateway-id",
                "proxy": {
                    "name": "gateway",
                    "type": "ss",
                    "dialer-proxy": "PROXY-FALLBACK",
                },
            },
            "underlay": {
                "tier": "tier1",
                "pool_kind": "tier1_variant",
                "identity": "underlay-id",
            },
        },
    }

    bound = runtime._runtime_gateway_underlay_binding(snapshot, payload, gateway)

    assert bound is not None
    assert bound.observed_outbound == "PROXY-FALLBACK"
    assert bound.route_chain == ("PROXY-FALLBACK", "underlay")
    assert bound.node.key == underlay.key


def test_gateway_underlay_rollback_rejects_a_different_gateway_identity() -> None:
    runtime = object.__new__(DesktopRuntime)
    gateway_a = RuntimeNodeRef("exit", "gateway", "gateway-a", "gateway-a")
    gateway_b = RuntimeNodeRef("exit", "gateway", "gateway-b", "gateway-b")
    underlay = RuntimeNodeRef(
        "tier1", "tier1_variant", "underlay-id", "underlay"
    )
    snapshot = _snapshot(gateway_a, gateway_b, underlay)
    payload = {
        "proxies": {
            "proxies": {
                "FINAL-EXIT": {
                    "type": "Selector",
                    "now": "gateway-b",
                    "all": ["gateway-a", "gateway-b", "REJECT"],
                },
                "gateway-a": {"type": "Shadowsocks"},
                "gateway-b": {"type": "Shadowsocks"},
                "PROXY-FALLBACK": {
                    "type": "Fallback",
                    "now": "underlay",
                    "all": ["underlay"],
                },
                "underlay": {"type": "Shadowsocks"},
            }
        },
        "proxy_map": {
            "gateway-a": {
                "tier": "exit",
                "pool_kind": "gateway",
                "identity": "gateway-a",
            },
            "gateway-b": {
                "tier": "exit",
                "pool_kind": "gateway",
                "identity": "gateway-b",
            },
            "underlay": {
                "tier": "tier1",
                "pool_kind": "tier1_variant",
                "identity": "underlay-id",
            },
        },
    }
    incident_binding = _binding(
        snapshot,
        outbound="PROXY-FALLBACK",
        chain=("PROXY-FALLBACK", "underlay"),
        node=underlay,
    )
    runtime._runtime_route_snapshot_state = lambda: (snapshot, payload)
    runtime._refresh_runtime_route_snapshot = lambda **_kwargs: snapshot
    runtime._restore_runtime_selector_path = lambda *_args, **_kwargs: []

    with pytest.raises(RuntimeError, match="Gateway identity changed"):
        runtime._restore_runtime_automatic_route(
            incident_binding=incident_binding,
            previous_binding=incident_binding,
            previous_payload=payload,
            lease_record={"mode": "selector", "to": "underlay"},
            incident_id="incident",
            deadline_monotonic=time.monotonic() + 1.0,
            expected_gateway_key=gateway_a.key,
        )


def test_superseded_failback_is_parked_only_after_old_owner_releases() -> None:
    class ImmediateEvent:
        @staticmethod
        def wait(_seconds: float) -> bool:
            return False

        @staticmethod
        def is_set() -> bool:
            return False

    runtime = object.__new__(DesktopRuntime)
    runtime.stop_event = ImmediateEvent()
    runtime.running = True
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_failure_recovery_bindings = set()
    runtime._runtime_failure_recovery_threads = set()
    runtime._runtime_deferred_failbacks = {}
    runtime._runtime_selector_intent_revision = 0
    runtime._runtime_failback_last_attempt = {}
    runtime._runtime_failback_failures = {}
    runtime._runtime_flap_history = {}
    runtime._runtime_worker_token = lambda: (1,)
    runtime._runtime_worker_token_is_current = lambda _token: False
    runtime._runtime_facility_hold_reason = lambda: "network rebaseline"
    runtime._current_core_runtime_phase = lambda: "formal_capture"
    ref = RuntimeNodeRef("tier2", "tier2", "node-id", "node")
    snapshot = _snapshot(ref)
    binding = _binding(
        snapshot,
        outbound="FINAL-EXIT",
        chain=("FINAL-EXIT", "node"),
        node=ref,
    )
    runtime._runtime_incident_bindings = {"incident": binding}
    runtime._runtime_incident_route_payloads = {"incident": {}}
    now = time.monotonic()
    runtime._runtime_route_mutation_lease = {
        "incident_id": "incident",
        "selector_intent_revision": 0,
        "verified_at_monotonic": now - 31.0,
    }
    runtime._runtime_failback_pass_history = {ref.key: [now - 6.0]}
    original_defer = DesktopRuntime._defer_runtime_failback

    def defer_after_release(payload, recovered_ref):
        assert ("failback", *ref.key) not in (
            runtime._runtime_failure_recovery_bindings
        )
        original_defer(runtime, payload, recovered_ref)

    runtime._defer_runtime_failback = defer_after_release

    DesktopRuntime._start_runtime_failback(
        runtime,
        {"incident_id": "incident"},
        ref,
    )
    _wait_for_runtime_workers(runtime)

    assert "incident" in runtime._runtime_deferred_failbacks
    assert runtime._runtime_failure_recovery_bindings == set()


def test_provisional_verification_failure_rolls_back_without_reject_or_health_commit() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime.logger = logging.getLogger("test-provisional-route-change-fail-closed")
    runtime.stop_event = threading.Event()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._core_transition_lock = threading.RLock()
    runtime._runtime_failure_recovery_bindings = set()
    runtime._runtime_failure_recovery_threads = set()
    runtime._runtime_origin_diagnostic_bindings = {}
    runtime._runtime_pending_confirmed_incidents = {}
    runtime._runtime_route_mutation_lease = None
    runtime._runtime_incident_bindings = {}
    runtime._runtime_incident_route_payloads = {}
    runtime._runtime_incident_recovery_state = {}
    runtime._runtime_selector_intent_revision = 0
    runtime._runtime_network_context_epoch = 0
    runtime._runtime_route_epoch = 10
    runtime._runtime_route_stable_since = 0.0
    runtime._runtime_last_verified_route = None
    runtime._runtime_reload_gate = RuntimeReloadGate()
    runtime._runtime_worker_token = lambda: (1,)
    runtime._runtime_worker_token_is_current = lambda _token: True
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()

    manager_health: list[object] = []

    class Manager:
        @staticmethod
        def apply_runtime_health_observation(observation):
            manager_health.append(observation)

    runtime.manager = Manager()
    bad = RuntimeNodeRef("tier1", "tier1_variant", "bad-id", "bad")
    good = RuntimeNodeRef("tier1", "tier1_variant", "good-id", "good")
    snapshot = _snapshot(bad, good)
    origin = _binding(
        snapshot,
        outbound="GENERAL",
        chain=("GENERAL", "FINAL-EXIT", "TIER1-AUTO", "bad"),
        node=bad,
    )
    changed = _binding(
        snapshot,
        outbound="GENERAL",
        chain=("GENERAL", "FINAL-EXIT", "TIER1-AUTO", "good"),
        node=good,
    )
    failure = BoundRuntimeFailure(
        binding=origin,
        protocol="TCP",
        observed_outbound="GENERAL",
        destination_token="destination",
        error_type="connection_refused",
        observed_at_monotonic=time.monotonic(),
    )
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
    decision = runtime._runtime_egress_coordinator.observe(
        failure,
        current_snapshot=snapshot,
    )
    runtime._runtime_alternative_switch_action = lambda *_args: {
        "group": "TIER1-AUTO",
        "target": "good",
        "previous": "bad",
        "mode": "fixed",
    }

    def route_changed_then_verification_failed(*_args, **_kwargs):
        raise RuntimeError("neutral 204 verification failed after selector PUT")

    runtime._fast_recover_confirmed_alternative = (
        route_changed_then_verification_failed
    )
    runtime._current_runtime_failure_binding_state = lambda **_kwargs: (
        changed,
        snapshot,
        {},
    )
    rollbacks: list[dict[str, object]] = []
    runtime._rollback_runtime_provisional_selection = (
        lambda action, **_kwargs: (
            rollbacks.append(dict(action)) or origin,
            "committed",
        )
    )
    facility_degradation: list[str] = []
    runtime._record_runtime_facility_degradation = (
        lambda reason: facility_degradation.append(str(reason)) or {}
    )
    fail_closed: list[tuple[str, dict[str, object]]] = []

    def enforce(reason: str, **kwargs):
        fail_closed.append((reason, dict(kwargs)))
        return {"enforced": True, "stale": False}

    runtime._enforce_runtime_fail_closed = enforce
    effective: list[tuple[object, str]] = []
    runtime._set_effective_egress_state = (
        lambda ok, reason="": effective.append((ok, str(reason)))
    )
    runtime._set_effective_egress_readiness_state = (
        lambda ok, reason="": effective.append((ok, str(reason)))
    )
    reconciles: list[tuple[str, str]] = []
    runtime._submit_current_route_probe_failure = (
        lambda reason, *, source: reconciles.append((str(reason), str(source)))
        or True
    )
    evidence = _HotStandbyEvidence(
        ref=good,
        action={"group": "TIER1-AUTO", "target": "good"},
        snapshot_key=snapshot.snapshot_key,
        network_context_epoch=0,
        verified_at_monotonic=time.monotonic(),
        latency_ms=8.0,
        probe_result={"delay": 8},
        selection_group="TIER1-AUTO",
        selection_target="good",
        selection_mode="fixed",
        probe_url="https://www.gstatic.com/generate_204",
        expected_status="204",
    )

    runtime._start_runtime_provisional_diversion(
        failure,
        snapshot,
        {},
        incident_id=decision.incident_id,
        probe_token=decision.probe_token,
        evidence=evidence,
    )
    _wait_for_runtime_workers(runtime)

    assert fail_closed == []
    assert facility_degradation == []
    assert rollbacks == [
        {
            "group": "TIER1-AUTO",
            "target": "good",
            "previous": "bad",
            "mode": "fixed",
        }
    ]
    assert manager_health == []
    assert runtime._runtime_route_mutation_lease is None
    assert effective[-1] == (
        None,
        "Provisional route was restored after inconclusive verification",
    )
    assert reconciles == [
        ("provisional diversion was inconclusive", "provisional_reconcile")
    ]


def test_provisional_diversion_does_not_race_an_active_confirmation_pair() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_failure_recovery_bindings = set()
    runtime._runtime_route_mutation_lease = None
    runtime._runtime_network_context_epoch = 0
    runtime._runtime_worker_token = lambda: (1,)
    bad = RuntimeNodeRef("tier1", "tier1_variant", "bad-id", "bad")
    good = RuntimeNodeRef("tier1", "tier1_variant", "good-id", "good")
    snapshot = _snapshot(bad, good)
    binding = _binding(
        snapshot,
        outbound="GENERAL",
        chain=("GENERAL", "FINAL-EXIT", "TIER1-AUTO", "bad"),
        node=bad,
    )
    failure = BoundRuntimeFailure(
        binding=binding,
        protocol="TCP",
        observed_outbound="GENERAL",
        destination_token="destination",
        error_type="timeout",
        observed_at_monotonic=time.monotonic(),
    )
    runtime._runtime_failure_probe_bindings = {failure.circuit_key}
    runtime._runtime_alternative_switch_action = lambda *_args: {
        "group": "TIER1-AUTO",
        "target": "good",
        "previous": "bad",
        "mode": "fixed",
    }
    runtime._runtime_group_probe_contract = lambda *_args: (
        "https://www.gstatic.com/generate_204",
        "204",
    )
    evidence = _HotStandbyEvidence(
        ref=good,
        action={"group": "TIER1-AUTO", "target": "good", "mode": "fixed"},
        snapshot_key=snapshot.snapshot_key,
        network_context_epoch=0,
        verified_at_monotonic=time.monotonic(),
        latency_ms=8.0,
        probe_result={"delay": 8},
        selection_group="TIER1-AUTO",
        selection_target="good",
        selection_mode="fixed",
        probe_url="https://www.gstatic.com/generate_204",
        expected_status="204",
    )

    assert not runtime._start_runtime_provisional_diversion(
        failure,
        snapshot,
        {},
        incident_id="incident",
        probe_token="probe",
        evidence=evidence,
    )
    assert runtime._runtime_failure_recovery_bindings == set()


def test_gateway_underlay_failback_promotes_underlay_without_replacing_gateway() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime.logger = logging.getLogger("test-gateway-underlay-failback")
    runtime.stop_event = threading.Event()
    runtime.running = True
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._core_transition_lock = threading.RLock()
    runtime._runtime_failure_recovery_bindings = set()
    runtime._runtime_failure_recovery_threads = set()
    runtime._runtime_selector_intent_revision = 0
    runtime._runtime_failback_last_attempt = {}
    runtime._runtime_failback_failures = {}
    runtime._runtime_flap_history = {}
    runtime._runtime_deferred_failbacks = {}
    runtime._runtime_origin_diagnostic_bindings = {"incident": object()}
    runtime._runtime_incident_recovery_state = {"incident": "diverted"}
    runtime._runtime_failback_export_holds = set()
    runtime._runtime_worker_token = lambda: (1,)
    runtime._runtime_worker_token_is_current = lambda _token: True
    runtime._current_core_runtime_phase = lambda: "formal_capture"
    runtime._revalidation_capture_active = lambda _phase: True

    class Runner:
        @staticmethod
        def is_running() -> bool:
            return True

    runtime.core_runner = Runner()
    gateway = RuntimeNodeRef("exit", "gateway", "gateway-id", "gateway")
    recovered = RuntimeNodeRef(
        "tier1", "tier1_variant", "recovered-id", "recovered"
    )
    standby = RuntimeNodeRef(
        "tier1", "tier1_variant", "standby-id", "standby"
    )
    snapshot = _snapshot(gateway, recovered, standby)
    underlay_binding = _binding(
        snapshot,
        outbound="PROXY-FALLBACK",
        chain=("PROXY-FALLBACK", "recovered"),
        node=recovered,
    )
    runtime._runtime_incident_bindings = {"incident": underlay_binding}
    runtime._runtime_incident_route_payloads = {"incident": {}}
    now = time.monotonic()
    runtime._runtime_route_mutation_lease = {
        "incident_id": "incident",
        "selector_intent_revision": 0,
        "verified_at_monotonic": now - 31.0,
        "mode": "fixed",
        "group": "PROXY-FALLBACK",
        "to": "standby",
    }
    runtime._runtime_failback_pass_history = {recovered.key: [now - 6.0]}
    runtime._runtime_failback_export_holds.add(
        runtime._runtime_leaf_export_hold_key(recovered)
    )

    def route_payload(underlay: str) -> dict[str, object]:
        return {
            "proxies": {
                "proxies": {
                    "FINAL-EXIT": {
                        "type": "Selector",
                        "now": "EXIT-SELECT",
                        "all": ["EXIT-SELECT", "REJECT"],
                    },
                    "EXIT-SELECT": {
                        "type": "Selector",
                        "now": "gateway",
                        "all": ["gateway"],
                    },
                    "gateway": {"type": "Shadowsocks"},
                    "PROXY-FALLBACK": {
                        "type": "Fallback",
                        "now": underlay,
                        "all": ["recovered", "standby"],
                    },
                    "recovered": {"type": "Shadowsocks"},
                    "standby": {"type": "Shadowsocks"},
                }
            }
        }

    runtime._runtime_route_snapshot = snapshot
    runtime._runtime_route_snapshot_payload = route_payload("standby")
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
    runtime._refresh_runtime_route_snapshot = lambda **_kwargs: snapshot
    probed: list[RuntimeNodeRef] = []
    runtime._probe_runtime_leaf = (
        lambda ref, _payload, **_kwargs: probed.append(ref) or {"delay": 8}
    )
    prewarmed: list[dict[str, object]] = []
    runtime._prewarm_runtime_switch_target = (
        lambda _snapshot, action, **_kwargs: (
            prewarmed.append(dict(action)) or {"delay": 8}
        )
    )
    releases: list[dict[str, object]] = []

    def release(lease_record, **_kwargs):
        releases.append(dict(lease_record))
        runtime._runtime_route_snapshot_payload = route_payload("recovered")
        return {"commit_state": "committed"}

    runtime._release_runtime_automatic_fixed_selection = release
    verified: list[CoreLoadedRouteSnapshot] = []
    runtime._verify_runtime_final_exit = (
        lambda *, expected_snapshot, **_kwargs: verified.append(expected_snapshot)
        or {"status": 204}
    )
    effective: list[tuple[object, str]] = []
    runtime._set_effective_egress_state = (
        lambda ok, reason="": effective.append((ok, str(reason)))
    )
    retries: list[RuntimeNodeRef] = []
    runtime._start_runtime_failback = (
        lambda _payload, ref: retries.append(ref)
    )
    runtime._start_runtime_core_reconcile_for_failback = (
        lambda *_args, **_kwargs: pytest.fail("unexpected Core reconcile")
    )

    DesktopRuntime._start_runtime_failback(
        runtime,
        {"incident_id": "incident"},
        recovered,
    )
    _wait_for_runtime_workers(runtime)

    final_binding = runtime._runtime_route_binding_for_outbound(
        snapshot,
        runtime._runtime_route_snapshot_payload,
        "FINAL-EXIT",
    )
    underlay_after = runtime._runtime_route_binding_for_outbound(
        snapshot,
        runtime._runtime_route_snapshot_payload,
        "PROXY-FALLBACK",
        require_final_exit=False,
    )
    assert probed == []
    assert prewarmed == [
        {
            "group": "PROXY-FALLBACK",
            "target": "recovered",
            "previous": "standby",
            "mode": "fixed",
            "alternative_leaf": "recovered",
        }
    ]
    assert len(releases) == 1
    assert verified == [snapshot]
    assert final_binding is not None and final_binding.node.key == gateway.key
    assert underlay_after is not None and underlay_after.node.key == recovered.key
    assert runtime._runtime_route_mutation_lease is None
    assert runtime._runtime_incident_recovery_state["incident"] == "promoted"
    assert effective[-1] == (True, "")
    assert retries == []


def test_released_fixed_lease_accepts_natural_higher_tier_but_never_other_gateway() -> None:
    runtime = object.__new__(DesktopRuntime)
    recovered_tier3 = RuntimeNodeRef(
        "tier3", "tier3b", "tier3-id", "tier3-leaf"
    )
    natural_tier2 = RuntimeNodeRef(
        "tier2", "tier2", "tier2-id", "tier2-leaf"
    )
    same_layer_member = RuntimeNodeRef(
        "tier3", "tier3b", "other-tier3-id", "other-tier3-leaf"
    )
    gateway_a = RuntimeNodeRef("exit", "gateway", "gateway-a", "gateway-a")
    gateway_b = RuntimeNodeRef("exit", "gateway", "gateway-b", "gateway-b")

    assert runtime._runtime_natural_winner_is_acceptable(
        natural_tier2,
        recovered_tier3,
    )
    assert not runtime._runtime_natural_winner_is_acceptable(
        same_layer_member,
        recovered_tier3,
    )
    assert not runtime._runtime_natural_winner_is_acceptable(gateway_b, gateway_a)


def test_late_old_flow_error_inside_route_barrier_never_reaches_node_attribution() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_hot_standby_lock = threading.RLock()
    runtime._runtime_route_attribution_barrier_epoch = 9
    runtime._runtime_route_attribution_barrier_until = time.monotonic() + 2.0
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    leaf = RuntimeNodeRef("tier1", "tier1_variant", "leaf-id", "leaf")
    snapshot = _snapshot(leaf)
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
    runtime._runtime_transport_failure_is_internal_probe = lambda _parsed: False
    runtime._bind_runtime_transport_failure = lambda *_args: pytest.fail(
        "old-flow line must not bind to the new leaf"
    )
    confirmations: list[str] = []
    runtime._start_runtime_facility_confirmation = (
        lambda reason: confirmations.append(str(reason))
    )
    observed = time.monotonic()
    event = CoreOutputEvent(
        raw_line=(
            "[TCP] dial GENERAL (match Match/) 127.0.0.1:51001 "
            "--> example.com:443 error: connection reset by peer"
        ),
        pid=snapshot.process_pid,
        process_generation=snapshot.process_generation,
        received_at_monotonic=observed,
    )

    runtime._handle_runtime_output_event(event, snapshot, {"proxies": {}})

    assert confirmations == [
        "Transport error arrived inside the route-mutation attribution barrier"
    ]
    assert runtime._runtime_egress_coordinator._circuits == {}


def test_runtime_probe_facility_never_exceeds_two_concurrent_requests() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_probe_semaphore = threading.BoundedSemaphore(2)
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_internal_probe_windows = {}
    active = 0
    maximum = 0
    active_lock = threading.Lock()
    two_active = threading.Event()
    release = threading.Event()

    class Client:
        @staticmethod
        def proxy_delay(*_args, **_kwargs):
            nonlocal active, maximum
            with active_lock:
                active += 1
                maximum = max(maximum, active)
                if active == 2:
                    two_active.set()
            release.wait(0.5)
            with active_lock:
                active -= 1
            return {"delay": 8}

    runtime._require_api_client = lambda: Client()
    results: list[object] = []

    def worker(index: int) -> None:
        results.append(
            runtime._probe_proxy_delay_with_urls(
                f"leaf-{index}",
                ["https://www.gstatic.com/generate_204"],
                timeout_ms=700,
                request_timeout_seconds=0.7,
                expected_status="204",
            )
        )

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(3)]
    for thread in threads:
        thread.start()
    assert two_active.wait(0.5)
    time.sleep(0.03)
    assert maximum == 2
    release.set()
    for thread in threads:
        thread.join(1.0)

    assert maximum == 2
    assert len(results) == 3


def test_mixed_port_manual_probes_share_the_global_two_request_budget() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_probe_semaphore = threading.BoundedSemaphore(2)
    active = 0
    maximum = 0
    active_lock = threading.Lock()
    two_active = threading.Event()
    release = threading.Event()

    def leased_probe(*, timeout_seconds: float, excluded_destination: str = ""):
        del timeout_seconds, excluded_destination
        nonlocal active, maximum
        with active_lock:
            active += 1
            maximum = max(maximum, active)
            if active == 2:
                two_active.set()
        release.wait(0.5)
        with active_lock:
            active -= 1
        return {"status": 204}

    runtime._probe_mihomo_mixed_port_once_leased = leased_probe
    results: list[object] = []

    def worker() -> None:
        results.append(runtime._probe_mihomo_mixed_port_once(timeout_seconds=0.7))

    threads = [threading.Thread(target=worker) for _index in range(3)]
    for thread in threads:
        thread.start()
    assert two_active.wait(0.5)
    time.sleep(0.03)
    assert maximum == 2
    release.set()
    for thread in threads:
        thread.join(1.0)

    assert maximum == 2
    assert len(results) == 3


def test_gateway_hot_standby_refreshes_its_underlay_without_changing_gateway() -> None:
    runtime = object.__new__(DesktopRuntime)
    gateway = RuntimeNodeRef("exit", "gateway", "gateway-id", "gateway")
    underlay = RuntimeNodeRef("tier1", "tier1_variant", "underlay-a", "leaf-a")
    backup = RuntimeNodeRef("tier1", "tier1_variant", "underlay-b", "leaf-b")
    snapshot = _snapshot(gateway, underlay, backup)
    payload: dict[str, object] = {}
    gateway_binding = _binding(
        snapshot,
        outbound="FINAL-EXIT",
        chain=("FINAL-EXIT", "gateway"),
        node=gateway,
    )
    underlay_binding = _binding(
        snapshot,
        outbound="PROXY-FALLBACK",
        chain=("PROXY-FALLBACK", "leaf-a"),
        node=underlay,
    )
    runtime._runtime_route_snapshot_state = lambda: (snapshot, payload)
    runtime._runtime_route_binding_for_outbound = (
        lambda *_args, **_kwargs: gateway_binding
    )
    runtime._runtime_gateway_underlay_binding = (
        lambda _snapshot, _payload, ref: (
            underlay_binding if ref.key == gateway.key else None
        )
    )

    def alternatives(failure, *_args):
        assert failure.binding.node.key == underlay.key
        return [backup]

    runtime._runtime_ordered_alternative_refs = alternatives
    runtime._runtime_alternative_switch_action = (
        lambda failure, ref, _payload: {
            "group": failure.binding.observed_outbound,
            "target": ref.proxy_name,
            "mode": "selector",
        }
    )
    runtime._runtime_confirmation_timeout_ms = lambda *_args: 500
    runtime._probe_runtime_leaf = lambda *_args, **_kwargs: {"delay": 7}
    runtime._runtime_hot_standby_lock = threading.RLock()
    runtime._runtime_hot_standby_next_index = 0
    runtime._runtime_hot_standby_evidence = {}
    runtime._runtime_network_context_epoch = snapshot.network_context_epoch
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)

    runtime._refresh_one_runtime_hot_standby()

    evidence = runtime._runtime_hot_standby_evidence[backup.key]
    assert evidence.ref.key == backup.key
    assert evidence.action == {
        "group": "PROXY-FALLBACK",
        "target": "leaf-b",
        "mode": "selector",
    }
    assert gateway_binding.node.key == gateway.key


def test_gateway_nested_underlay_hot_standby_prewarms_exact_group_target() -> None:
    runtime = object.__new__(DesktopRuntime)
    gateway = RuntimeNodeRef("exit", "gateway", "gateway-id", "gateway")
    underlay = RuntimeNodeRef("tier1", "tier1_variant", "underlay-a", "leaf-a")
    backup = RuntimeNodeRef("tier1", "tier1_variant", "underlay-b", "leaf-b")
    snapshot = _snapshot(gateway, underlay, backup)
    payload: dict[str, object] = {}
    gateway_binding = _binding(
        snapshot,
        outbound="FINAL-EXIT",
        chain=("FINAL-EXIT", "gateway"),
        node=gateway,
    )
    underlay_binding = _binding(
        snapshot,
        outbound="PROXY-FALLBACK",
        chain=("PROXY-FALLBACK", "leaf-a"),
        node=underlay,
    )
    runtime._runtime_route_snapshot_state = lambda: (snapshot, payload)
    runtime._runtime_route_binding_for_outbound = (
        lambda *_args, **_kwargs: gateway_binding
    )
    runtime._runtime_gateway_underlay_binding = (
        lambda *_args, **_kwargs: underlay_binding
    )
    runtime._runtime_ordered_alternative_refs = lambda *_args, **_kwargs: [backup]
    action = {
        "group": "PROXY-FALLBACK",
        "target": "NESTED-BACKUP",
        "previous": "leaf-a",
        "mode": "fixed",
        "alternative_leaf": "leaf-b",
    }
    runtime._runtime_alternative_switch_action = lambda *_args, **_kwargs: action
    runtime._runtime_confirmation_timeout_ms = lambda *_args: 500
    runtime._runtime_group_probe_contract = (
        lambda *_args: ("https://www.gstatic.com/generate_204", "204")
    )
    prewarmed: list[dict[str, object]] = []
    runtime._prewarm_runtime_switch_target = (
        lambda _snapshot, observed, **_kwargs: (
            prewarmed.append(dict(observed)) or {"delay": 6}
        )
    )
    runtime._probe_runtime_leaf = lambda *_args, **_kwargs: pytest.fail(
        "nested fixed target must use exact group prewarm"
    )
    runtime._runtime_hot_standby_lock = threading.RLock()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_route_mutation_lease = None
    runtime._runtime_hot_standby_next_index = 0
    runtime._runtime_hot_standby_evidence = {}
    runtime._runtime_network_context_epoch = snapshot.network_context_epoch
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)

    runtime._refresh_one_runtime_hot_standby()

    evidence = runtime._runtime_hot_standby_evidence[backup.key]
    assert prewarmed == [action]
    assert evidence.probed_proxy_name == "NESTED-BACKUP"
    assert runtime._runtime_hot_standby_contract_matches(
        evidence,
        snapshot,
        action,
    )


def test_gateway_underlay_failure_consumes_hot_standby_without_cold_probe() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_failure_probe_bindings = set()
    runtime._runtime_hot_standby_lock = threading.RLock()
    runtime._runtime_route_mutation_lease = None
    runtime._runtime_facility_hold_reason = lambda: ""
    runtime._runtime_worker_token = lambda: (1,)
    runtime._runtime_worker_token_is_current = lambda _token: True
    runtime._runtime_origin_diagnostic_binding_for_probe = (
        lambda *_args, **_kwargs: None
    )

    gateway = RuntimeNodeRef("exit", "gateway", "gateway-id", "gateway-g")
    underlay = RuntimeNodeRef("tier1", "tier1_variant", "underlay-a", "leaf-a")
    backup = RuntimeNodeRef("tier1", "tier1_variant", "underlay-b", "leaf-b")
    snapshot = _snapshot(gateway, underlay, backup)
    gateway_binding = _binding(
        snapshot,
        outbound="FINAL-EXIT",
        chain=("FINAL-EXIT", "gateway-g"),
        node=gateway,
    )
    underlay_binding = _binding(
        snapshot,
        outbound="PROXY-FALLBACK",
        chain=("PROXY-FALLBACK", "leaf-a"),
        node=underlay,
    )
    payload = {
        "proxies": {
            "proxies": {
                "FINAL-EXIT": {
                    "type": "Selector",
                    "now": "gateway-g",
                    "all": ["gateway-g", "REJECT"],
                },
                "gateway-g": {"type": "Shadowsocks"},
                "PROXY-FALLBACK": {
                    "type": "Fallback",
                    "now": "leaf-a",
                    "all": ["leaf-a", "leaf-b", "REJECT"],
                },
                "leaf-a": {"type": "Shadowsocks"},
                "leaf-b": {"type": "Shadowsocks"},
            }
        },
        "proxy_map": {
            "gateway-g": {
                "tier": "exit",
                "pool_kind": "gateway",
                "identity": "gateway-id",
                "proxy": {
                    "name": "gateway-g",
                    "type": "ss",
                    "dialer-proxy": "PROXY-FALLBACK",
                },
            },
            "leaf-a": {
                "tier": "tier1",
                "pool_kind": "tier1_variant",
                "identity": "underlay-a",
            },
            "leaf-b": {
                "tier": "tier1",
                "pool_kind": "tier1_variant",
                "identity": "underlay-b",
            },
        },
    }
    runtime._runtime_gateway_underlay_binding = (
        lambda _snapshot, _payload, ref: (
            underlay_binding if ref.key == gateway.key else None
        )
    )
    runtime._runtime_group_probe_contract = (
        lambda _snapshot, _group: (
            "https://www.gstatic.com/generate_204",
            "204",
        )
    )
    runtime._runtime_confirmation_timeout_ms = lambda *_args, **_kwargs: 500
    runtime._runtime_network_context_epoch = snapshot.network_context_epoch
    runtime._runtime_hot_standby_evidence = {
        backup.key: _HotStandbyEvidence(
            ref=backup,
            action={
                "group": "PROXY-FALLBACK",
                "target": "leaf-b",
                "mode": "fixed",
            },
            snapshot_key=snapshot.snapshot_key,
            network_context_epoch=snapshot.network_context_epoch,
            verified_at_monotonic=time.monotonic(),
            latency_ms=7.0,
            probe_result={"delay": 7},
            selection_group="PROXY-FALLBACK",
            selection_target="leaf-b",
            selection_mode="fixed",
            probe_url="https://www.gstatic.com/generate_204",
            expected_status="204",
        )
    }

    def alternatives(failure, *_args):
        assert failure.binding.node.key == underlay.key
        return [backup]

    runtime._runtime_ordered_alternative_refs = alternatives
    probed: list[str] = []

    def fail_gateway_and_underlay(ref, *_args, **_kwargs):
        probed.append(ref.proxy_name)
        if ref.key in {gateway.key, underlay.key}:
            raise RuntimeError("selected Gateway route is unavailable")
        raise AssertionError("prewarmed backup must not be probed again")

    runtime._probe_runtime_leaf = fail_gateway_and_underlay
    cold_probes: list[str] = []
    runtime._probe_managed_alternative = (
        lambda *_args, **_kwargs: (
            cold_probes.append("cold")
            or (None, None)
        )
    )
    facility_events: list[str] = []
    runtime._record_runtime_facility_degradation = facility_events.append
    runtime._start_runtime_facility_confirmation = facility_events.append
    recovered = threading.Event()
    recoveries: list[
        tuple[
            BoundRuntimeFailure,
            RuntimeNodeRef | None,
            _HotStandbyEvidence | None,
        ]
    ] = []

    def start_recovery(
        bound_failure,
        *_args,
        confirmed_alternative=None,
        confirmed_alternative_evidence=None,
        **_kwargs,
    ):
        recoveries.append(
            (
                bound_failure,
                confirmed_alternative,
                confirmed_alternative_evidence,
            )
        )
        recovered.set()

    runtime._start_runtime_recovery = start_recovery
    coordinator = RuntimeEgressCoordinator()
    coordinator.set_current_snapshot(snapshot)
    runtime._runtime_egress_coordinator = coordinator
    failure = BoundRuntimeFailure(
        binding=gateway_binding,
        protocol="TCP",
        observed_outbound="FINAL-EXIT",
        destination_token="business-destination",
        error_type="timeout",
        observed_at_monotonic=time.monotonic(),
    )
    first = coordinator.observe(failure, current_snapshot=snapshot)

    runtime._start_runtime_independent_probe(
        failure,
        snapshot,
        payload,
        probe_token=first.probe_token,
    )
    assert recovered.wait(1.0)
    deadline = time.monotonic() + 1.0
    while failure.circuit_key in runtime._runtime_failure_probe_bindings:
        assert time.monotonic() < deadline
        time.sleep(0.005)

    assert cold_probes == []
    assert facility_events == []
    assert len(recoveries) == 1
    recovered_failure, recovered_alternative, recovered_evidence = recoveries[0]
    assert recovered_failure.binding.node.key == underlay.key
    assert recovered_alternative is not None
    assert recovered_alternative.key == backup.key
    assert recovered_evidence is runtime._runtime_hot_standby_evidence[backup.key]
    assert gateway_binding.node.key == gateway.key
    assert payload["proxies"]["proxies"]["FINAL-EXIT"]["now"] == "gateway-g"


def test_targeted_recheck_enqueue_failure_logs_only_identity_hash(caplog) -> None:
    runtime = object.__new__(DesktopRuntime)
    identity = "tier3|secret.example|443|uuid-secret"

    class Manager:
        @staticmethod
        def enqueue_tier3_targeted_recheck(*_args, **_kwargs):
            raise RuntimeError(f"recheck failed for {identity}")

    runtime.manager = Manager()
    runtime.logger = logging.getLogger("runtime-privacy-regression")
    plan = SimpleNamespace(failed_identities=(identity,))

    with caplog.at_level(logging.WARNING, logger=runtime.logger.name):
        runtime._queue_tier3_core_repair_targeted_rechecks(plan)

    assert identity not in caplog.text
    assert "identity_hash=hmac:" in caplog.text


def test_failback_selector_restore_is_inner_first_bounded_and_never_queues_refresh() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_route_mutation_lease = {"incident_id": "incident"}
    runtime.select_mihomo_proxy = lambda *_args, **_kwargs: pytest.fail(
        "failback must not use the generic queued-refresh selector helper"
    )
    leaf = RuntimeNodeRef("tier1", "tier1_variant", "leaf-id", "leaf")
    snapshot = _snapshot(leaf)
    binding = _binding(
        snapshot,
        outbound="GENERAL",
        chain=("GENERAL", "FINAL-EXIT", "TIER1", "leaf"),
        node=leaf,
    )
    desired = {
        "proxies": {
            "proxies": {
                "GENERAL": {"type": "Selector", "now": "FINAL-EXIT", "all": ["FINAL-EXIT"]},
                "FINAL-EXIT": {"type": "Selector", "now": "TIER1", "all": ["TIER1"]},
                "TIER1": {"type": "Selector", "now": "leaf", "all": ["leaf"]},
                "leaf": {"type": "Shadowsocks"},
            }
        }
    }
    current = {
        "proxies": {
            "proxies": {
                "GENERAL": {"type": "Selector", "now": "FINAL-EXIT", "all": ["FINAL-EXIT"]},
                "FINAL-EXIT": {"type": "Selector", "now": "standby-tier", "all": ["TIER1", "standby-tier"]},
                "TIER1": {"type": "Selector", "now": "standby", "all": ["leaf", "standby"]},
            }
        }
    }
    calls: list[tuple[str, str]] = []
    runtime._apply_runtime_automatic_selection = (
        lambda action, **_kwargs: calls.append(
            (str(action["group"]), str(action["target"]))
        )
        or {"commit_state": "committed"}
    )

    actions = runtime._restore_runtime_selector_path(
        binding,
        desired,
        incident_id="incident",
        deadline_monotonic=time.monotonic() + 1.0,
        current_snapshot_payload=current,
    )

    assert calls == [("TIER1", "leaf"), ("FINAL-EXIT", "TIER1")]
    assert actions == [
        {"group": "TIER1", "proxy": "leaf"},
        {"group": "FINAL-EXIT", "proxy": "TIER1"},
    ]


def test_bounded_route_snapshot_refresh_includes_lock_wait_in_deadline() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_route_snapshot_refresh_lock = threading.Lock()
    runtime._runtime_route_snapshot_refresh_lock.acquire()
    runtime._refresh_runtime_route_snapshot_serialized = lambda **_kwargs: pytest.fail(
        "serialized controller read must not start after lock deadline"
    )
    started = time.monotonic()
    try:
        result = runtime._refresh_runtime_route_snapshot(timeout_seconds=0.05)
    finally:
        runtime._runtime_route_snapshot_refresh_lock.release()

    assert result is None
    assert time.monotonic() - started < 0.2
