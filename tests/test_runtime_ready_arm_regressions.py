from __future__ import annotations

import copy

import logging
import threading
import time
from types import SimpleNamespace

import pytest

import desktop_runtime as desktop_runtime_module
from core_runner import CoreOutputEvent
from desktop_runtime import (
    DesktopRuntime,
    _InitialEgressAdmissionLease,
    _RuntimeActivationRecoveryOwner,
    _RuntimeRecoveryArm,
    _RuntimeRecoveryTicket,
)
from runtime_egress_recovery import (
    BoundRuntimeFailure,
    CoreInventorySnapshot,
    CoreLoadedGroupState,
    CoreLoadedRouteSnapshot,
    RuntimeEgressCoordinator,
    RuntimeEgressReadiness,
    RuntimeEgressReadinessState,
    RuntimeNodeRef,
    RuntimeRouteBinding,
)


class _RunningCore:
    def __init__(self) -> None:
        self.running = True
        self.stop_calls: list[dict[str, object]] = []

    def is_running(self) -> bool:
        return self.running

    def status(self) -> dict[str, object]:
        return {
            "running": self.running,
            "pid": 4312 if self.running else 0,
            "process_generation": 7,
        }

    def stop(self, **kwargs: object) -> dict[str, object]:
        self.stop_calls.append(dict(kwargs))
        self.running = False
        return {"stopped": True}


class _IngressRecorder:
    def __init__(self, snapshot: CoreLoadedRouteSnapshot | None = None) -> None:
        self.snapshot = snapshot
        self.guards: list[str] = []
        self.ingress: list[object] = []

    def current_route_snapshot(self) -> CoreLoadedRouteSnapshot | None:
        return self.snapshot

    def request_guard(self, reason: str) -> None:
        self.guards.append(str(reason))

    def submit_ingress(self, payload: object) -> None:
        self.ingress.append(payload)


def _loaded_authority(
    *,
    pool_kind: str = "tier1_variant",
    identity: str = "leaf-a-id",
    proxy_name: str = "leaf-a",
) -> tuple[CoreLoadedRouteSnapshot, dict[str, object], RuntimeRouteBinding]:
    ref = RuntimeNodeRef(
        tier="tier1" if pool_kind != "gateway" else "exit",
        pool_kind=pool_kind,
        identity=identity,
        proxy_name=proxy_name,
    )
    snapshot = CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded-digest",
        route_epoch=11,
        network_context_epoch=5,
        group_graph=(("FINAL-EXIT", (proxy_name, "REJECT")),),
        group_states=(
            CoreLoadedGroupState(
                name="FINAL-EXIT",
                type_name="Selector",
                members=(proxy_name, "REJECT"),
                now=proxy_name,
            ),
        ),
        proxy_nodes=(ref,),
    )
    payload: dict[str, object] = {
        "proxies": {
            "FINAL-EXIT": {
                "type": "Selector",
                "now": proxy_name,
                "all": [proxy_name, "REJECT"],
            },
            proxy_name: {"type": "Shadowsocks"},
            "REJECT": {"type": "Reject"},
        },
        "proxy_map": {
            proxy_name: {
                "tier": ref.tier,
                "pool_kind": ref.pool_kind,
                "identity": ref.identity,
            }
        },
    }
    binding = RuntimeRouteBinding(
        process_pid=snapshot.process_pid,
        process_generation=snapshot.process_generation,
        core_generation=snapshot.core_generation,
        semantic_digest=snapshot.semantic_digest,
        route_epoch=snapshot.route_epoch,
        network_context_epoch=snapshot.network_context_epoch,
        observed_outbound="FINAL-EXIT",
        route_chain=("FINAL-EXIT", proxy_name),
        node=ref,
    )
    return snapshot, payload, binding


def _unbound_loaded_authority() -> tuple[
    CoreLoadedRouteSnapshot,
    dict[str, object],
    tuple[RuntimeNodeRef, RuntimeNodeRef],
]:
    bad = RuntimeNodeRef(
        tier="tier3",
        pool_kind="tier3b",
        identity="bad-id",
        proxy_name="bad-leaf",
    )
    good = RuntimeNodeRef(
        tier="tier3",
        pool_kind="tier3b",
        identity="good-id",
        proxy_name="good-leaf",
    )
    groups = (
        CoreLoadedGroupState(
            name="FINAL-EXIT",
            type_name="Selector",
            members=("PROXY-FALLBACK", "REJECT"),
            now="PROXY-FALLBACK",
        ),
        CoreLoadedGroupState(
            name="PROXY-FALLBACK",
            type_name="Fallback",
            members=("TIER3-AUTO", "REJECT"),
            now="TIER3-AUTO",
        ),
        CoreLoadedGroupState(
            name="TIER3-AUTO",
            type_name="Fallback",
            members=("TIER3B-RUNTIME", "REJECT"),
            now="TIER3B-RUNTIME",
        ),
        CoreLoadedGroupState(
            name="TIER3B-RUNTIME",
            type_name="Fallback",
            members=(bad.proxy_name, good.proxy_name, "REJECT"),
            now="REJECT",
            test_url="https://probe.invalid/generate_204",
            expected_status="204",
        ),
    )
    snapshot = CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="unbound-loaded-digest",
        route_epoch=11,
        network_context_epoch=5,
        group_graph=tuple((state.name, state.members) for state in groups),
        group_states=groups,
        proxy_nodes=(bad, good),
    )
    payload: dict[str, object] = {
        "proxies": {
            "proxies": {
                state.name: {
                    "type": state.type_name,
                    "now": state.now,
                    "all": list(state.members),
                    "testUrl": state.test_url,
                    "expectedStatus": state.expected_status,
                }
                for state in groups
            }
            | {
                bad.proxy_name: {"type": "Shadowsocks"},
                good.proxy_name: {"type": "Shadowsocks"},
                "REJECT": {"type": "Reject"},
            }
        },
        "proxy_map": {
            bad.proxy_name: {
                "tier": bad.tier,
                "pool_kind": bad.pool_kind,
                "identity": bad.identity,
            },
            good.proxy_name: {
                "tier": good.tier,
                "pool_kind": good.pool_kind,
                "identity": good.identity,
            },
        },
    }
    return snapshot, payload, (bad, good)


def _selected_unbound_authority(
    base: CoreLoadedRouteSnapshot,
    base_payload: dict[str, object],
    ref: RuntimeNodeRef,
    *,
    route_epoch: int,
) -> tuple[CoreLoadedRouteSnapshot, dict[str, object], RuntimeRouteBinding]:
    groups = tuple(
        CoreLoadedGroupState(
            name=state.name,
            type_name=state.type_name,
            members=state.members,
            now=(
                ref.proxy_name
                if state.name == "TIER3B-RUNTIME"
                else state.now
            ),
            fixed=(
                ref.proxy_name
                if state.name == "TIER3B-RUNTIME"
                else state.fixed
            ),
            test_url=state.test_url,
            expected_status=state.expected_status,
        )
        for state in base.group_states
    )
    snapshot = CoreLoadedRouteSnapshot(
        process_pid=base.process_pid,
        process_generation=base.process_generation,
        core_generation=base.core_generation,
        semantic_digest=base.semantic_digest,
        route_epoch=route_epoch,
        network_context_epoch=base.network_context_epoch,
        group_graph=base.group_graph,
        group_states=groups,
        proxy_nodes=base.proxy_nodes,
    )
    payload = copy.deepcopy(base_payload)
    entries = payload["proxies"]["proxies"]
    entries["TIER3B-RUNTIME"]["now"] = ref.proxy_name
    entries["TIER3B-RUNTIME"]["fixed"] = ref.proxy_name
    binding = RuntimeRouteBinding(
        process_pid=snapshot.process_pid,
        process_generation=snapshot.process_generation,
        core_generation=snapshot.core_generation,
        semantic_digest=snapshot.semantic_digest,
        route_epoch=snapshot.route_epoch,
        network_context_epoch=snapshot.network_context_epoch,
        observed_outbound="FINAL-EXIT",
        route_chain=(
            "FINAL-EXIT",
            "PROXY-FALLBACK",
            "TIER3-AUTO",
            "TIER3B-RUNTIME",
            ref.proxy_name,
        ),
        node=ref,
    )
    return snapshot, payload, binding


def _dial_failure_event() -> CoreOutputEvent:
    return CoreOutputEvent(
        raw_line=(
            'level=warning msg="[TCP] dial GENERAL (match Match/) '
            "127.0.0.1:55001 --> example.invalid:443 error: "
            'context deadline exceeded"'
        ),
        pid=4312,
        process_generation=7,
        received_at_monotonic=10.0,
    )


def _initial_activation_fixture(
    *,
    capture_wanted: bool,
) -> tuple[DesktopRuntime, _RunningCore, list[tuple[object, ...]]]:
    runtime = object.__new__(DesktopRuntime)
    runner = _RunningCore()
    events: list[tuple[object, ...]] = []
    snapshot, payload, binding = _loaded_authority()

    runtime.manager = None
    runtime.core_runner = runner
    runtime.running = True
    runtime.stop_event = threading.Event()
    runtime._runtime_generation = 7
    runtime._core_generation = 3
    runtime._core_lifecycle_lock = threading.RLock()
    runtime._runtime_egress_recovery_profile = "early_single_coordinator"
    runtime.traffic_monitor = SimpleNamespace(
        start=lambda: events.append(("traffic-start",)),
        stop=lambda: events.append(("traffic-stop",)),
    )
    runtime._runtime_intent_lock = threading.RLock()
    runtime._core_transition_lock = threading.RLock()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_route_snapshot = snapshot
    runtime._runtime_route_snapshot_payload = payload
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
    runtime._runtime_facility_epoch = 2
    runtime._runtime_selector_intent_revision = 4
    runtime._lifecycle_revision = 9
    runtime._runtime_route_mutation_lease = None
    runtime._runtime_traffic_capture_wanted = lambda: capture_wanted
    runtime._runtime_facility_hold_reason = lambda: ""
    runtime._tier3_egress_activation_is_current = (
        lambda *_args, **_kwargs: True
    )
    runtime._runtime_worker_token_is_current = lambda _token: True
    runtime._disarm_runtime_recovery = (
        lambda reason="": events.append(("disarm", str(reason)))
    )
    def publish_readiness(
        state: RuntimeEgressReadinessState,
        **kwargs: object,
    ) -> RuntimeEgressReadiness:
        readiness = RuntimeEgressReadiness(
            state=state,
            validation_active=bool(kwargs.get("validation_active", False)),
            reason=str(kwargs.get("reason") or ""),
            lifecycle_revision=runtime._lifecycle_revision,
            updated_at_monotonic=time.monotonic(),
        )
        runtime._runtime_egress_readiness = readiness
        events.append(("readiness", state))
        return readiness

    runtime._publish_runtime_egress_readiness = publish_readiness
    runtime._publish_non_gateway_serving = (
        lambda reason="Automatic switching is preparing": events.append(
            ("serving", str(reason))
        )
    )
    runtime._verify_runtime_final_exit = (
        lambda **kwargs: events.append(
            (
                "probe",
                kwargs.get("expected_snapshot") is snapshot,
                kwargs.get("expected_worker_token"),
                kwargs.get("required_successes"),
            )
        )
        or {"status": 204, "url": "local-204"}
    )
    runtime._probe_runtime_leaf = (
        lambda ref, _payload, **kwargs: events.append(
            (
                "leaf-probe",
                ref.proxy_name,
                kwargs.get("timeout_ms"),
                kwargs.get("probe_priority"),
            )
        )
        or {"delay": 40}
    )
    runtime._verify_capture_route_after_reload = (
        lambda **kwargs: events.append(
            (
                "capture-probe",
                kwargs.get("lease_record"),
                kwargs.get("expected_gateway_key"),
                kwargs.get("route_intent"),
            )
        )
        or {"status": 204, "url": "local-204"}
    )
    runtime._hot_reload_runtime_config_phase = (
        lambda *_args, **kwargs: events.append(
            ("reload", kwargs.get("capture_enabled"))
        )
    )
    runtime._apply_system_proxy_capture_after_core_start = (
        lambda _runner: events.append(("capture-published",))
    )
    runtime._mark_tier3_core_sync_success = (
        lambda **kwargs: events.append(
            ("sync", kwargs.get("traffic_capture_active"))
        )
    )
    runtime._set_effective_egress_state = (
        lambda value, reason: events.append(("effective", value, reason))
    )
    runtime._reset_tier3_standby_activation_failure = (
        lambda: events.append(("failure-reset",))
    )
    runtime._queue_runtime_route_snapshot_refresh = (
        lambda: events.append(("snapshot-refresh",))
    )
    runtime._wake_parked_tier3_pool_sync_if_causal_key_changed = (
        lambda: events.append(("provider-wake",)) or True
    )
    runtime._try_arm_runtime_recovery = (
        lambda: events.append(("arm",)) or True
    )
    runtime._start_runtime_failure_monitor = (
        lambda: events.append(("recovery-monitor",))
    )
    runtime._resume_deferred_runtime_failbacks = (
        lambda: events.append(("failback-resume",))
    )
    runtime._current_core_runtime_phase = (
        lambda: "formal_capture" if capture_wanted else "formal_no_capture"
    )
    runtime._rollback_to_formal_no_capture = (
        lambda *_args, **_kwargs: events.append(("rollback",))
    )
    runtime._tier3_activation_authority_state = (
        lambda **kwargs: events.append(
            ("authority", kwargs.get("worker_token"))
        )
        or (snapshot, payload, binding)
    )
    runtime._refresh_loaded_runtime_authority = (
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("initial activation must not require controller readback")
        )
    )
    return runtime, runner, events


def test_initial_no_capture_requires_authority_and_dual_204_before_ready() -> None:
    runtime, runner, events = _initial_activation_fixture(capture_wanted=False)

    result = runtime._run_initial_egress_activation(
        runner=runner,
        activation_generation=8,
        core_generation=3,
        selector_intent_revision=4,
        worker_token=(9, 11),
    )

    assert result["status"] == 204
    probe_events = [event for event in events if event[0] == "probe"]
    assert len(probe_events) == 1
    assert probe_events[0][2:] == ((9, 11), 2)
    assert not [event for event in events if event[0] == "serving"]
    assert events.index(("authority", (9, 11))) < events.index(
        ("probe", True, (9, 11), 2)
    )
    assert ("reload", True) not in events
    assert ("traffic-start",) not in events
    assert events.index(("probe", True, (9, 11), 2)) < events.index(
        ("readiness", RuntimeEgressReadinessState.READY)
    )
    assert [event for event in events if event[0] == "arm"] == [("arm",)]
    assert events.index(("arm",)) < events.index(
        ("readiness", RuntimeEgressReadinessState.READY)
    )
    assert events.index(
        ("readiness", RuntimeEgressReadinessState.READY)
    ) < events.index(("recovery-monitor",))
    assert runtime._runtime_initial_activation_generation == 3


def test_initial_capture_verifies_native_fallback_before_and_after_reload() -> None:
    runtime, runner, events = _initial_activation_fixture(capture_wanted=True)

    runtime._run_initial_egress_activation(
        runner=runner,
        activation_generation=8,
        core_generation=3,
        selector_intent_revision=4,
        worker_token=(9, 11),
    )

    probe_events = [event for event in events if event[0] == "probe"]
    assert len(probe_events) == 1
    assert probe_events[0][2:] == ((9, 11), 2)
    assert [event for event in events if event[0] == "leaf-probe"] == []
    assert len([event for event in events if event[0] == "capture-probe"]) == 1
    assert not [event for event in events if event[0] == "serving"]
    capture_probe = next(event for event in events if event[0] == "capture-probe")
    assert capture_probe[1] is None
    assert capture_probe[3] is not None
    assert events.index(("authority", (9, 11))) < events.index(probe_events[0])
    assert events.index(probe_events[0]) < events.index(("reload", True))
    assert events.index(("reload", True)) < events.index(("capture-published",))
    assert events.index(("reload", True)) < events.index(capture_probe)
    assert events.index(capture_probe) < events.index(("capture-published",))
    assert events.index(("capture-published",)) < events.index(("traffic-start",))
    assert events.index(("traffic-start",)) < events.index(
        ("readiness", RuntimeEgressReadinessState.READY)
    )
    assert [event for event in events if event[0] == "arm"] == [("arm",)]
    assert events.index(("arm",)) < events.index(
        ("readiness", RuntimeEgressReadinessState.READY)
    )
    assert events.index(
        ("readiness", RuntimeEgressReadinessState.READY)
    ) < events.index(("recovery-monitor",))
    assert ("traffic-start",) in events
    assert ("rollback",) not in events


def test_system_proxy_uses_verified_no_capture_core_without_apply_config() -> None:
    runtime, runner, events = _initial_activation_fixture(capture_wanted=True)
    runtime.network_mode = "system_proxy"

    runtime._run_initial_egress_activation(
        runner=runner,
        activation_generation=8,
        core_generation=3,
        selector_intent_revision=4,
        worker_token=(9, 11),
    )

    assert ("reload", True) not in events
    assert not [event for event in events if event[0] == "capture-probe"]
    assert ("capture-published",) in events
    assert ("traffic-start",) in events
    assert ("readiness", RuntimeEgressReadinessState.READY) in events
    assert ("rollback",) not in events


def test_system_proxy_host_failure_advances_same_native_fallback_before_ready() -> None:
    runtime, runner, events = _initial_activation_fixture(capture_wanted=True)
    runtime.network_mode = "system_proxy"
    unbound, unbound_payload, (first, second) = _unbound_loaded_authority()
    first_authority = _selected_unbound_authority(
        unbound,
        unbound_payload,
        first,
        route_epoch=12,
    )
    second_authority = _selected_unbound_authority(
        unbound,
        unbound_payload,
        second,
        route_epoch=13,
    )
    runtime._tier3_activation_authority_state = (
        lambda **_kwargs: (unbound, unbound_payload, None)
    )

    def publish(authority):
        snapshot, payload, binding = authority
        with runtime._runtime_route_snapshot_lock:
            runtime._runtime_route_snapshot = snapshot
            runtime._runtime_route_snapshot_payload = payload
            runtime._runtime_route_epoch = snapshot.route_epoch
        runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
        return binding, snapshot, payload, 0

    runtime._activate_startup_native_fallback = (
        lambda **_kwargs: publish(first_authority)
    )
    readbacks: list[str] = []

    def readback(**_kwargs):
        readbacks.append("advance")
        binding, snapshot, payload, _commits = publish(second_authority)
        return snapshot, payload, binding

    runtime._refresh_loaded_runtime_authority = readback
    full_chain: list[str] = []

    def verify_chain(*, expected_snapshot, **_kwargs):
        binding = runtime._runtime_route_binding_for_outbound(
            expected_snapshot,
            runtime._runtime_route_snapshot_payload,
            "FINAL-EXIT",
        )
        assert binding is not None
        full_chain.append(binding.node.proxy_name)
        return {"status": 204, "url": "local-204"}

    runtime._verify_runtime_final_exit = verify_chain
    member_probes: list[str] = []

    def fail_first_member(ref, _payload, **_kwargs):
        member_probes.append(ref.proxy_name)
        raise TimeoutError("first member failed after host-path failure")

    runtime._probe_runtime_leaf = fail_first_member
    runtime.system_proxy = SimpleNamespace(active=False)

    def apply_capture(_runner):
        runtime.system_proxy.active = True
        events.append(("capture-published",))

    restores: list[str] = []

    def restore_capture():
        restores.append("restore")
        runtime.system_proxy.active = False
        return {"active": False}

    runtime._apply_system_proxy_capture_after_core_start = apply_capture
    runtime.restore_system_proxy = restore_capture
    host_proofs: list[str] = []

    def verify_host(**_kwargs):
        snapshot, payload = runtime._runtime_route_snapshot_state()
        assert snapshot is not None
        binding = runtime._runtime_route_binding_for_outbound(
            snapshot,
            payload,
            "FINAL-EXIT",
        )
        assert binding is not None
        host_proofs.append(binding.node.proxy_name)
        if binding.node.key == first.key:
            raise TimeoutError("first host path failed")
        return {"status": 204, "verified": True}

    runtime._verify_system_proxy_host_capture = verify_host

    result = runtime._run_initial_egress_activation(
        runner=runner,
        activation_generation=8,
        core_generation=3,
        selector_intent_revision=4,
        worker_token=(9, 11),
    )

    assert result["status"] == 204
    assert full_chain == [first.proxy_name, second.proxy_name]
    assert host_proofs == [first.proxy_name, second.proxy_name]
    assert member_probes == [first.proxy_name]
    assert readbacks == ["advance"]
    assert restores == ["restore"]
    assert runtime.system_proxy.active is True
    assert ("reload", True) not in events
    assert ("readiness", RuntimeEgressReadinessState.READY) in events


def test_rollback_admission_delegates_unbound_route_to_native_fallback() -> None:
    runtime, runner, events = _initial_activation_fixture(capture_wanted=False)
    snapshot, payload, (_bad, good) = _unbound_loaded_authority()
    activated: list[dict[str, object]] = []
    runtime._tier3_activation_authority_state = (
        lambda **_kwargs: (snapshot, payload, None)
    )
    good_binding = RuntimeRouteBinding(
        process_pid=snapshot.process_pid,
        process_generation=snapshot.process_generation,
        core_generation=snapshot.core_generation,
        semantic_digest=snapshot.semantic_digest,
        route_epoch=snapshot.route_epoch,
        network_context_epoch=snapshot.network_context_epoch,
        observed_outbound="FINAL-EXIT",
        route_chain=("FINAL-EXIT", "TIER3B-RUNTIME", good.proxy_name),
        node=good,
    )

    def activate(**kwargs: object):
        activated.append(dict(kwargs))
        return good_binding, snapshot, payload, 0

    runtime._activate_startup_native_fallback = activate
    runtime._activate_initial_unbound_candidate = lambda **_kwargs: (
        (_ for _ in ()).throw(
            AssertionError("startup must not pin a concrete fallback leaf")
        )
    )

    result = runtime._run_initial_egress_activation(
        runner=runner,
        activation_generation=8,
        core_generation=3,
        selector_intent_revision=4,
        worker_token=(9, 11),
    )

    assert result["status"] == 204
    assert len(activated) == 1
    assert activated[0]["intent"].top_level_target == "PROXY-FALLBACK"
    assert "excluded_probe_keys" not in activated[0]
    probe_events = [event for event in events if event[0] == "probe"]
    assert len(probe_events) == 1
    assert probe_events[0][2:] == ((9, 11), 2)


def test_rollback_admission_never_turns_failed_current_leaf_into_startup_fixed() -> None:
    runtime, runner, events = _initial_activation_fixture(capture_wanted=False)
    snapshot, payload, (bad, good) = _unbound_loaded_authority()
    bad_binding = RuntimeRouteBinding(
        process_pid=snapshot.process_pid,
        process_generation=snapshot.process_generation,
        core_generation=snapshot.core_generation,
        semantic_digest=snapshot.semantic_digest,
        route_epoch=snapshot.route_epoch,
        network_context_epoch=snapshot.network_context_epoch,
        observed_outbound="FINAL-EXIT",
        route_chain=("FINAL-EXIT", "TIER3B-RUNTIME", bad.proxy_name),
        node=bad,
    )
    good_binding = RuntimeRouteBinding(
        process_pid=snapshot.process_pid,
        process_generation=snapshot.process_generation,
        core_generation=snapshot.core_generation,
        semantic_digest=snapshot.semantic_digest,
        route_epoch=snapshot.route_epoch,
        network_context_epoch=snapshot.network_context_epoch,
        observed_outbound="FINAL-EXIT",
        route_chain=("FINAL-EXIT", "TIER3B-RUNTIME", good.proxy_name),
        node=good,
    )
    runtime._tier3_activation_authority_state = (
        lambda **_kwargs: (snapshot, payload, bad_binding)
    )
    probe_calls = 0

    def verify(**kwargs: object) -> dict[str, object]:
        nonlocal probe_calls
        probe_calls += 1
        events.append(("probe-attempt", probe_calls))
        return {"status": 204, "url": "local-204"}

    activated: list[dict[str, object]] = []

    def activate(**kwargs: object):
        activated.append(dict(kwargs))
        return good_binding, snapshot, payload, 0

    runtime._verify_runtime_final_exit = verify
    runtime._activate_startup_native_fallback = activate
    runtime._activate_initial_unbound_candidate = lambda **_kwargs: (
        (_ for _ in ()).throw(
            AssertionError("startup must not pin a concrete fallback leaf")
        )
    )

    result = runtime._run_initial_egress_activation(
        runner=runner,
        activation_generation=8,
        core_generation=3,
        selector_intent_revision=4,
        worker_token=(9, 11),
    )

    assert result["status"] == 204
    assert probe_calls == 1
    assert len(activated) == 1
    assert activated[0]["intent"].loaded_order == (
        bad.proxy_name,
        good.proxy_name,
    )
    assert "excluded_probe_keys" not in activated[0]


def test_initial_admission_is_deferred_until_runtime_commit() -> None:
    runtime = object.__new__(DesktopRuntime)
    queued: list[str] = []
    runtime.running = False
    runtime._desired_running = True
    runtime._stop_request_pending = False
    runtime.stop_event = threading.Event()
    runtime._runtime_intent_lock = threading.RLock()
    runtime._core_lifecycle_lock = threading.RLock()
    runtime._core_generation = 3
    runtime._lifecycle_revision = 9
    runtime._initial_egress_activation_deferred = False
    runtime._initial_egress_activation_deferred_core_generation = -1
    runner = _RunningCore()
    runtime.core_runner = runner
    runtime._current_core_runtime_phase = lambda: "formal_no_capture"
    runtime._runtime_initial_activation_was_completed = lambda: False
    runtime._queue_tier3_egress_activation = (
        lambda: queued.append("activation") if not queued else None
    )

    assert runtime._request_initial_egress_admission() is False
    assert queued == []
    assert runtime._initial_egress_activation_deferred is False

    runtime.running = True
    assert runtime._start_deferred_initial_egress_admission(
        lifecycle_revision=9
    ) is True
    assert queued == ["activation"]
    assert runtime._initial_egress_activation_deferred is False
    assert runtime._start_deferred_initial_egress_admission(
        lifecycle_revision=9
    ) is True
    assert queued == ["activation"]


def test_deferred_admission_transfers_to_current_core_generation() -> None:
    runtime = object.__new__(DesktopRuntime)
    queued: list[str] = []
    runtime.running = True
    runtime._desired_running = True
    runtime._stop_request_pending = False
    runtime.stop_event = threading.Event()
    runtime._runtime_intent_lock = threading.RLock()
    runtime._core_lifecycle_lock = threading.RLock()
    runtime._core_generation = 4
    runtime._lifecycle_revision = 9
    runtime._initial_egress_activation_deferred = True
    runtime._initial_egress_activation_deferred_core_generation = 3
    runtime.core_runner = _RunningCore()
    runtime._current_core_runtime_phase = lambda: "formal_no_capture"
    runtime._runtime_initial_activation_was_completed = lambda: False
    runtime._queue_tier3_egress_activation = (
        lambda: queued.append("activation") if not queued else None
    )

    assert runtime._start_deferred_initial_egress_admission(
        lifecycle_revision=9
    ) is True
    assert queued == ["activation"]
    assert runtime._initial_egress_activation_deferred is False


def test_standby_activation_key_includes_core_provider_inventory() -> None:
    runtime = object.__new__(DesktopRuntime)
    snapshot, payload, _binding = _loaded_authority()
    runner = _RunningCore()
    leaf_b = RuntimeNodeRef(
        tier="tier3",
        pool_kind="tier3b",
        identity="leaf-b-id",
        proxy_name="leaf-b",
    )
    runtime.core_runner = runner
    runtime._core_lifecycle_lock = threading.RLock()
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._core_generation = snapshot.core_generation
    runtime._runtime_route_snapshot = snapshot
    runtime._runtime_route_snapshot_payload = payload
    runtime._runtime_loaded_export_contract = {
        "semantic_digest": snapshot.semantic_digest,
    }
    runtime._runtime_network_context_epoch = snapshot.network_context_epoch
    runtime._runtime_selector_intent_revision = 4
    runtime._runtime_core_inventory_snapshot = CoreInventorySnapshot(
        process_pid=snapshot.process_pid,
        process_generation=snapshot.process_generation,
        core_generation=snapshot.core_generation,
        semantic_digest=snapshot.semantic_digest,
        route_epoch=snapshot.route_epoch,
        provider_version="provider-v1",
        members=(snapshot.proxy_nodes[0],),
    )

    first = runtime._tier3_standby_activation_key()
    runtime._runtime_core_inventory_snapshot = CoreInventorySnapshot(
        process_pid=snapshot.process_pid,
        process_generation=snapshot.process_generation,
        core_generation=snapshot.core_generation,
        semantic_digest=snapshot.semantic_digest,
        route_epoch=snapshot.route_epoch,
        provider_version="provider-v2",
        members=(*snapshot.proxy_nodes, leaf_b),
    )

    assert runtime._tier3_standby_activation_key() != first


def test_cancelled_stale_admission_cannot_overwrite_new_lifecycle() -> None:
    runtime = object.__new__(DesktopRuntime)
    runner = _RunningCore()
    published: list[object] = []
    runtime.stop_event = threading.Event()
    runtime.core_runner = runner
    runtime._tier3_egress_activation_lock = threading.RLock()
    runtime._core_lifecycle_lock = threading.RLock()
    runtime._tier3_egress_activation_pending = False
    runtime._tier3_egress_activation_generation = 8
    runtime._tier3_egress_activation_active_generation = 8
    runtime._lifecycle_revision = 10
    runtime._runtime_generation = 3
    runtime._core_generation = 3
    runtime._runtime_selector_intent_revision = 4
    runtime._record_tier3_initial_activation_stage = lambda *_args, **_kwargs: None
    runtime._set_effective_egress_readiness_state = (
        lambda *_args, **_kwargs: published.append("effective")
    )
    runtime._publish_runtime_egress_readiness = (
        lambda *_args, **_kwargs: published.append("readiness")
    )
    lease = _InitialEgressAdmissionLease(
        activation_generation=8,
        lifecycle_revision=9,
        runtime_generation=3,
        core_generation=3,
        process_pid=4312,
        process_generation=7,
        selector_intent_revision=4,
        worker_token=(3, 1, 3, 4312, 7, 0),
        deadline_monotonic=time.monotonic() + 6.0,
    )

    assert runtime._finish_cancelled_initial_egress_admission(
        admission_lease=lease,
        runner=runner,
        reason="old lifecycle cancelled",
    ) is False
    assert published == []


def test_cancelled_current_busy_owner_clears_after_selector_supersession() -> None:
    runtime = object.__new__(DesktopRuntime)
    runner = _RunningCore()
    published: list[tuple[object, ...]] = []
    runtime.stop_event = threading.Event()
    runtime.core_runner = runner
    runtime._tier3_egress_activation_lock = threading.RLock()
    runtime._core_lifecycle_lock = threading.RLock()
    runtime._tier3_egress_activation_pending = False
    runtime._tier3_egress_activation_generation = 8
    runtime._tier3_egress_activation_active_generation = 8
    runtime._lifecycle_revision = 9
    runtime._runtime_generation = 3
    runtime._core_generation = 3
    runtime._runtime_selector_intent_revision = 5
    runtime._runtime_egress_readiness = RuntimeEgressReadiness(
        state=RuntimeEgressReadinessState.ACTIVATION_HOLD,
        validation_active=True,
        lifecycle_revision=9,
        deadline_monotonic=time.monotonic() + 6.0,
    )
    runtime._record_tier3_initial_activation_stage = lambda *_args, **_kwargs: None
    runtime._set_effective_egress_readiness_state = (
        lambda value, reason: published.append(("effective", value, reason))
    )
    runtime._publish_runtime_egress_readiness = (
        lambda state, **kwargs: published.append(
            (
                "readiness",
                state,
                kwargs.get("validation_active"),
                kwargs.get("effective_ok"),
            )
        )
    )
    lease = _InitialEgressAdmissionLease(
        activation_generation=8,
        lifecycle_revision=9,
        runtime_generation=3,
        core_generation=3,
        process_pid=4312,
        process_generation=7,
        selector_intent_revision=4,
        worker_token=(3, 1, 3, 4312, 7, 0),
        deadline_monotonic=time.monotonic() + 6.0,
    )

    assert runtime._finish_cancelled_initial_egress_admission(
        admission_lease=lease,
        runner=runner,
        reason="user selection superseded admission",
    ) is False
    assert published[0] == (
        "readiness",
        RuntimeEgressReadinessState.DATA_PLANE_FAILED,
        False,
        False,
    )


def test_tier3_final_exit_qualification_cannot_cross_core_generation() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._core_lifecycle_lock = threading.RLock()
    runtime._tier3_runtime_qualification_lock = threading.RLock()
    runtime._core_generation = 12
    runtime._runtime_initial_activation_generation = 12
    runtime._tier3_runtime_final_exit_ok = True
    runtime._tier3_runtime_final_exit_core_generation = 11
    runtime._tier3_selected_leaf_evidence = (
        "leaf-old",
        "identity-old",
        ("signature-old",),
    )

    assert runtime._tier3_runtime_qualification_stable() is False

    runtime._tier3_runtime_final_exit_core_generation = 12
    runtime._runtime_egress_readiness = RuntimeEgressReadiness(
        state=RuntimeEgressReadinessState.READY,
        validation_active=False,
    )
    runtime.manager = SimpleNamespace(effective_egress_ok=True)
    assert runtime._tier3_runtime_qualification_stable() is True

    runtime._runtime_initial_activation_generation = -1
    runtime._runtime_egress_readiness = RuntimeEgressReadiness(
        state=RuntimeEgressReadinessState.ACTIVATION_HOLD,
        validation_active=True,
    )
    assert runtime._tier3_runtime_qualification_stable() is False


def test_capture_reload_does_not_start_without_verification_tail_budget() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._core_transition_lock = threading.RLock()
    runner = _RunningCore()
    runner.hot_reload = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("ApplyConfig must not start")
    )

    with pytest.raises(TimeoutError, match="insufficient budget"):
        runtime._hot_reload_runtime_config_phase(
            runner,
            capture_enabled=True,
            deadline_monotonic=time.monotonic() + 0.25,
        )


def test_capture_reload_expires_while_another_core_mutation_owns_the_lock() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._core_transition_lock = threading.RLock()
    runner = _RunningCore()
    entered = threading.Event()
    release = threading.Event()

    def hold_core_mutation() -> None:
        with runtime._core_transition_lock:
            entered.set()
            release.wait(timeout=2.0)

    holder = threading.Thread(target=hold_core_mutation, daemon=True)
    holder.start()
    assert entered.wait(timeout=1.0)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="Core mutation ownership"):
            runtime._hot_reload_runtime_config_phase(
                runner,
                capture_enabled=True,
                deadline_monotonic=time.monotonic() + 0.05,
            )
    finally:
        release.set()
        holder.join(timeout=1.0)

    assert time.monotonic() - started < 0.5
    assert holder.is_alive() is False


def test_initial_admission_watchdog_ends_busy_and_rejects_old_ready_callback() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime.stop_event = threading.Event()
    runtime.manager = None
    runtime._tier3_egress_activation_lock = threading.RLock()
    runtime._core_lifecycle_lock = threading.RLock()
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._tier3_egress_activation_generation = 5
    runtime._tier3_egress_activation_active_generation = 5
    runtime._tier3_egress_activation_active_mode = "activation"
    runtime._tier3_egress_activation_active_key = ("activation", "fixture")
    runtime._tier3_egress_activation_pending = False
    runtime._tier3_egress_activation_worker_running = True
    runtime._runtime_initial_activation_generation = -1
    runtime._lifecycle_revision = 7
    runtime._runtime_generation = 2
    runtime._core_generation = 3
    runtime._core_route_mutation_pending = False
    runtime._runtime_route_epoch = 0
    runtime._runtime_network_context_epoch = 0
    runtime._source_control_final_exit_was_ready = False
    runtime._runtime_egress_readiness = RuntimeEgressReadiness(
        state=RuntimeEgressReadinessState.ACTIVATION_HOLD,
        validation_active=True,
        lifecycle_revision=7,
        deadline_monotonic=time.monotonic() + 0.05,
    )
    stages: list[str] = []
    runtime._record_tier3_initial_activation_stage = (
        lambda stage, **_kwargs: stages.append(str(stage))
    )
    lease = _InitialEgressAdmissionLease(
        activation_generation=5,
        lifecycle_revision=7,
        runtime_generation=2,
        core_generation=3,
        process_pid=4312,
        process_generation=7,
        selector_intent_revision=4,
        worker_token=(2, 1, 3, 4312, 7, 0),
        deadline_monotonic=time.monotonic() + 0.05,
    )

    runtime._start_initial_egress_admission_watchdog(lease)
    deadline = time.monotonic() + 1.0
    while (
        runtime._runtime_egress_readiness.validation_active
        and time.monotonic() < deadline
    ):
        time.sleep(0.005)

    assert runtime._runtime_egress_readiness.state is (
        RuntimeEgressReadinessState.DATA_PLANE_FAILED
    )
    assert runtime._runtime_egress_readiness.validation_active is False
    assert runtime._tier3_egress_activation_generation == 6
    assert runtime._tier3_egress_activation_active_generation == 0
    assert runtime._tier3_egress_activation_worker_running is False
    assert stages == ["deadline_terminal"]

    # A callback from the expired owner cannot re-open READY or busy.
    runtime._publish_runtime_egress_readiness(
        RuntimeEgressReadinessState.READY,
        validation_active=False,
        expected_activation_generation=5,
        expected_lifecycle_revision=7,
    )
    assert runtime._runtime_egress_readiness.state is (
        RuntimeEgressReadinessState.DATA_PLANE_FAILED
    )


def test_failed_capture_rollback_never_claims_stopped_while_core_is_running() -> None:
    class StubbornCore(_RunningCore):
        def stop(self, **kwargs: object) -> dict[str, object]:
            self.stop_calls.append(dict(kwargs))
            return {
                "stopped": False,
                "still_running": True,
                "running": True,
            }

    runtime = object.__new__(DesktopRuntime)
    runner = StubbornCore()
    phases = ["formal_capture"]
    runtime.logger = logging.getLogger("runtime-ready-arm-rollback")
    runtime.logger.handlers.clear()
    runtime.logger.addHandler(logging.NullHandler())
    runtime.restore_system_proxy = lambda: None
    runtime.traffic_monitor = SimpleNamespace(stop=lambda: None)
    runtime._current_core_runtime_phase = lambda: phases[-1]
    runtime._set_core_runtime_phase = lambda phase: phases.append(str(phase))
    runtime._hot_reload_runtime_config_phase = (
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("rollback ApplyConfig failed")
        )
    )

    runtime._rollback_to_formal_no_capture(
        runner,
        "capture verification failed",
        deadline_monotonic=time.monotonic() + 0.2,
    )

    assert runner.stop_calls
    assert runner.is_running() is True
    assert phases[-1] == "formal_capture"
    assert "stopped" not in phases


def test_incremental_revalidation_without_a_managed_binding_is_terminal() -> None:
    runtime = object.__new__(DesktopRuntime)
    effective: list[tuple[object, str]] = []
    readiness: list[tuple[object, bool, str]] = []
    forbidden: list[str] = []
    runtime.stop_event = threading.Event()
    runtime.core_runner = _RunningCore()
    runtime._tier3_egress_activation_lock = threading.RLock()
    runtime._core_lifecycle_lock = threading.RLock()
    runtime._tier3_egress_activation_pending = True
    runtime._tier3_egress_activation_pending_mode = "incremental"
    runtime._tier3_egress_activation_pending_key = ("fixture",)
    runtime._tier3_egress_activation_worker_running = True
    runtime._tier3_egress_activation_generation = 1
    runtime._tier3_egress_activation_active_generation = 0
    runtime._tier3_egress_activation_active_mode = ""
    runtime._tier3_egress_activation_active_key = None
    runtime._tier3_egress_activation_thread = threading.current_thread()
    runtime._core_generation = 3
    runtime._runtime_selector_intent_revision = 2
    runtime._revalidation_mode_for_runtime_state = (
        lambda mode, **_kwargs: mode
    )
    runtime._runtime_worker_token = lambda: (1, 2, 3)
    runtime._current_runtime_failure_binding_state = lambda **_kwargs: None
    runtime._refresh_loaded_runtime_authority = lambda **_kwargs: None
    runtime._verify_runtime_final_exit = lambda **_kwargs: forbidden.append("probe")
    runtime._set_effective_egress_readiness_state = (
        lambda value, reason: effective.append((value, str(reason)))
    )
    runtime._publish_runtime_egress_readiness = (
        lambda state, **kwargs: readiness.append(
            (state, bool(kwargs.get("validation_active")), str(kwargs.get("reason") or ""))
        )
    )

    runtime._tier3_egress_activation_worker_early()

    assert forbidden == []
    assert effective[-1][0] is False
    assert readiness[-1][0] is RuntimeEgressReadinessState.CONTROLLER_HOLD
    assert readiness[-1][1] is False
    assert runtime._tier3_egress_activation_worker_running is False


def test_readiness_revocation_notifies_source_supply() -> None:
    runtime = object.__new__(DesktopRuntime)
    events: list[str] = []
    manager = SimpleNamespace(
        lock=threading.RLock(),
        tier3_last_collection_report={},
        notify_tier3_source_supply_route_changed=lambda event: events.append(str(event)),
    )
    runtime.manager = manager
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_route_epoch = 4
    runtime._runtime_network_context_epoch = 2
    runtime._lifecycle_revision = 7
    runtime._source_control_final_exit_was_ready = True
    runtime._runtime_egress_readiness = RuntimeEgressReadiness(
        state=RuntimeEgressReadinessState.READY,
        validation_active=False,
        lifecycle_revision=7,
        route_epoch=4,
        network_context_epoch=2,
    )
    runtime._source_control_final_exit_ready = lambda _manager: (
        runtime._runtime_egress_readiness.state
        is RuntimeEgressReadinessState.READY
    )

    runtime._publish_runtime_egress_readiness(
        RuntimeEgressReadinessState.DATA_PLANE_FAILED,
        validation_active=False,
        reason="current-generation proof was revoked",
    )

    assert events == ["verified_final_exit_revoked"]
    assert manager.tier3_last_collection_report["validation_active"] is False
    assert manager.tier3_last_collection_report["readiness_state"] == (
        RuntimeEgressReadinessState.DATA_PLANE_FAILED.value
    )


def test_unbound_reject_route_adopts_native_fallback_leaf_before_ready() -> None:
    runtime, runner, events = _initial_activation_fixture(capture_wanted=False)
    unbound, unbound_payload, (bad, good) = _unbound_loaded_authority()
    selected_groups = tuple(
        CoreLoadedGroupState(
            name=state.name,
            type_name=state.type_name,
            members=state.members,
            now=(good.proxy_name if state.name == "TIER3B-RUNTIME" else state.now),
            fixed=(good.proxy_name if state.name == "TIER3B-RUNTIME" else state.fixed),
            test_url=state.test_url,
            expected_status=state.expected_status,
        )
        for state in unbound.group_states
    )
    selected_snapshot = CoreLoadedRouteSnapshot(
        process_pid=unbound.process_pid,
        process_generation=unbound.process_generation,
        core_generation=unbound.core_generation,
        semantic_digest=unbound.semantic_digest,
        route_epoch=12,
        network_context_epoch=unbound.network_context_epoch,
        group_graph=unbound.group_graph,
        group_states=selected_groups,
        proxy_nodes=unbound.proxy_nodes,
    )
    selected_payload = copy.deepcopy(unbound_payload)
    selected_entries = selected_payload["proxies"]["proxies"]
    selected_entries["TIER3B-RUNTIME"]["now"] = good.proxy_name
    selected_entries["TIER3B-RUNTIME"]["fixed"] = good.proxy_name
    selected_binding = RuntimeRouteBinding(
        process_pid=selected_snapshot.process_pid,
        process_generation=selected_snapshot.process_generation,
        core_generation=selected_snapshot.core_generation,
        semantic_digest=selected_snapshot.semantic_digest,
        route_epoch=selected_snapshot.route_epoch,
        network_context_epoch=selected_snapshot.network_context_epoch,
        observed_outbound="FINAL-EXIT",
        route_chain=(
            "FINAL-EXIT",
            "PROXY-FALLBACK",
            "TIER3-AUTO",
            "TIER3B-RUNTIME",
            good.proxy_name,
        ),
        node=good,
    )
    runtime._active_gateway_policy_enabled = lambda: False
    runtime._tier3_activation_authority_state = (
        lambda **_kwargs: (unbound, unbound_payload, None)
    )
    leaf_probes: list[str] = []

    def probe_leaf(ref, _payload, **_kwargs):
        leaf_probes.append(ref.proxy_name)
        if ref.key == bad.key:
            raise TimeoutError("bad first leaf")
        return {"delay": 17}

    runtime._probe_runtime_leaf = probe_leaf
    prewarms: list[tuple[str, str]] = []
    runtime._prewarm_runtime_switch_target = (
        lambda _snapshot, action, **_kwargs: (
            prewarms.append((action["group"], action["target"]))
            or {"delay": 17}
        )
    )
    commits: list[tuple[str, str]] = []
    runtime._apply_runtime_automatic_selection = (
        lambda action, **_kwargs: (
            commits.append((action["group"], action["target"]))
            or {"commit_state": "committed"}
        )
    )
    readbacks: list[str] = []
    def publish_selected_authority(**_kwargs):
        readbacks.append("readback")
        with runtime._runtime_route_snapshot_lock:
            runtime._runtime_route_snapshot = selected_snapshot
            runtime._runtime_route_snapshot_payload = selected_payload
        runtime._runtime_egress_coordinator.set_current_snapshot(
            selected_snapshot
        )
        return selected_snapshot, selected_payload, selected_binding

    runtime._refresh_loaded_runtime_authority = publish_selected_authority
    final_probes: list[tuple[object, ...]] = []
    runtime._verify_runtime_final_exit = (
        lambda **kwargs: (
            final_probes.append(
                (
                    kwargs.get("expected_snapshot"),
                    kwargs.get("required_successes"),
                )
            )
            or {"status": 204, "url": "local-204"}
        )
    )
    native_calls: list[object] = []
    runtime._activate_startup_native_fallback = (
        lambda **kwargs: (
            native_calls.append(kwargs["intent"])
            or (selected_binding, selected_snapshot, selected_payload, 0)
        )
    )
    runtime._activate_initial_unbound_candidate = lambda **_kwargs: (
        (_ for _ in ()).throw(
            AssertionError("startup must not pin a concrete fallback leaf")
        )
    )

    result = runtime._run_initial_egress_activation(
        runner=runner,
        activation_generation=8,
        core_generation=3,
        selector_intent_revision=4,
        worker_token=(9, 11),
    )

    assert result["status"] == 204
    assert leaf_probes == []
    assert prewarms == []
    assert commits == []
    assert readbacks == []
    assert final_probes == [(selected_snapshot, 2)]
    assert len(native_calls) == 1
    assert native_calls[0].loaded_order == (bad.proxy_name, good.proxy_name)
    assert runtime._runtime_route_mutation_lease is None
    assert not [event for event in events if event[0] == "serving"]
    assert ("readiness", RuntimeEgressReadinessState.READY) in events


def test_native_fallback_owns_leaf_retry_without_application_selector_loop() -> None:
    runtime, runner, events = _initial_activation_fixture(capture_wanted=False)
    unbound, unbound_payload, (first, second) = _unbound_loaded_authority()
    runtime._active_gateway_policy_enabled = lambda: False
    runtime._tier3_activation_authority_state = (
        lambda **_kwargs: (unbound, unbound_payload, None)
    )

    def selected_authority(
        ref: RuntimeNodeRef,
        route_epoch: int,
    ) -> tuple[CoreLoadedRouteSnapshot, dict[str, object], RuntimeRouteBinding]:
        groups = tuple(
            CoreLoadedGroupState(
                name=state.name,
                type_name=state.type_name,
                members=state.members,
                now=(ref.proxy_name if state.name == "TIER3B-RUNTIME" else state.now),
                fixed=(
                    ref.proxy_name
                    if state.name == "TIER3B-RUNTIME"
                    else state.fixed
                ),
                test_url=state.test_url,
                expected_status=state.expected_status,
            )
            for state in unbound.group_states
        )
        snapshot = CoreLoadedRouteSnapshot(
            process_pid=unbound.process_pid,
            process_generation=unbound.process_generation,
            core_generation=unbound.core_generation,
            semantic_digest=unbound.semantic_digest,
            route_epoch=route_epoch,
            network_context_epoch=unbound.network_context_epoch,
            group_graph=unbound.group_graph,
            group_states=groups,
            proxy_nodes=unbound.proxy_nodes,
        )
        payload = copy.deepcopy(unbound_payload)
        entries = payload["proxies"]["proxies"]
        entries["TIER3B-RUNTIME"]["now"] = ref.proxy_name
        entries["TIER3B-RUNTIME"]["fixed"] = ref.proxy_name
        binding = RuntimeRouteBinding(
            process_pid=snapshot.process_pid,
            process_generation=snapshot.process_generation,
            core_generation=snapshot.core_generation,
            semantic_digest=snapshot.semantic_digest,
            route_epoch=snapshot.route_epoch,
            network_context_epoch=snapshot.network_context_epoch,
            observed_outbound="FINAL-EXIT",
            route_chain=(
                "FINAL-EXIT",
                "PROXY-FALLBACK",
                "TIER3-AUTO",
                "TIER3B-RUNTIME",
                ref.proxy_name,
            ),
            node=ref,
        )
        return snapshot, payload, binding

    selected = {
        first.proxy_name: selected_authority(first, 12),
        second.proxy_name: selected_authority(second, 13),
    }
    leaf_probes: list[str] = []

    def probe_selected_member(ref, _payload, **_kwargs):
        leaf_probes.append(ref.proxy_name)
        if ref.key == first.key:
            raise TimeoutError("native fallback selected a dead first member")
        return {"delay": 9}

    runtime._probe_runtime_leaf = probe_selected_member
    prewarms: list[str] = []
    runtime._prewarm_runtime_switch_target = (
        lambda _snapshot, action, **_kwargs: (
            prewarms.append(str(action["target"])) or {"delay": 9}
        )
    )
    commits: list[str] = []
    selected_target = [""]

    def commit(action, **_kwargs):
        selected_target[0] = str(action["target"])
        commits.append(selected_target[0])
        return {"commit_state": "committed"}

    runtime._apply_runtime_automatic_selection = commit

    readbacks: list[str] = []

    def readback(**_kwargs):
        readbacks.append("native-advance")
        snapshot, payload, binding = selected[second.proxy_name]
        with runtime._runtime_route_snapshot_lock:
            runtime._runtime_route_snapshot = snapshot
            runtime._runtime_route_snapshot_payload = payload
            runtime._runtime_route_epoch = snapshot.route_epoch
        runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
        return snapshot, payload, binding

    runtime._refresh_loaded_runtime_authority = readback
    final_probes: list[str] = []

    def verify_full_chain(*, expected_snapshot, required_successes, **_kwargs):
        binding = runtime._runtime_route_binding_for_outbound(
            expected_snapshot,
            runtime._runtime_route_snapshot_payload,
            "FINAL-EXIT",
        )
        assert binding is not None
        assert required_successes == 2
        final_probes.append(binding.node.proxy_name)
        if binding.node.key == first.key:
            raise TimeoutError("first candidate failed the complete FINAL-EXIT contract")
        return {"status": 204, "url": "local-204"}

    runtime._verify_runtime_final_exit = verify_full_chain
    native_calls: list[object] = []
    first_snapshot, first_payload, first_binding = selected[first.proxy_name]

    def activate_native(**kwargs):
        native_calls.append(kwargs["intent"])
        with runtime._runtime_route_snapshot_lock:
            runtime._runtime_route_snapshot = first_snapshot
            runtime._runtime_route_snapshot_payload = first_payload
        runtime._runtime_egress_coordinator.set_current_snapshot(first_snapshot)
        return first_binding, first_snapshot, first_payload, 0

    runtime._activate_startup_native_fallback = activate_native
    runtime._activate_initial_unbound_candidate = lambda **_kwargs: (
        (_ for _ in ()).throw(
            AssertionError("startup must not loop over fixed fallback leaves")
        )
    )

    result = runtime._run_initial_egress_activation(
        runner=runner,
        activation_generation=8,
        core_generation=3,
        selector_intent_revision=4,
        worker_token=(9, 11),
    )

    assert result["status"] == 204
    assert leaf_probes == [first.proxy_name]
    assert prewarms == []
    assert commits == []
    assert readbacks == ["native-advance"]
    assert final_probes == [first.proxy_name, second.proxy_name]
    assert len(native_calls) == 1
    assert native_calls[0].loaded_order == (first.proxy_name, second.proxy_name)
    assert runtime._runtime_route_mutation_lease is None
    assert not [event for event in events if event[0] == "serving"]
    assert ("readiness", RuntimeEgressReadinessState.READY) in events


def _gateway_candidate_authority() -> tuple[
    CoreLoadedRouteSnapshot,
    dict[str, object],
]:
    gateway_a = RuntimeNodeRef(
        tier="exit",
        pool_kind="gateway",
        identity="gateway-a-id",
        proxy_name="gateway-a",
    )
    gateway_b = RuntimeNodeRef(
        tier="exit",
        pool_kind="gateway",
        identity="gateway-b-id",
        proxy_name="gateway-b",
    )
    underlay = RuntimeNodeRef(
        tier="tier3",
        pool_kind="tier3b",
        identity="underlay-id",
        proxy_name="underlay-leaf",
    )
    states = (
        CoreLoadedGroupState(
            name="FINAL-EXIT",
            type_name="Selector",
            members=("EXIT-SELECT", "REJECT"),
            now="REJECT",
        ),
        CoreLoadedGroupState(
            name="EXIT-SELECT",
            type_name="Selector",
            members=(gateway_a.proxy_name, gateway_b.proxy_name, "REJECT"),
            now=gateway_b.proxy_name,
        ),
        CoreLoadedGroupState(
            name="PROXY-FALLBACK",
            type_name="Fallback",
            members=(underlay.proxy_name, "REJECT"),
            now="REJECT",
            test_url="https://probe.invalid/generate_204",
            expected_status="204",
        ),
    )
    snapshot = CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="gateway-loaded-digest",
        route_epoch=11,
        network_context_epoch=5,
        group_graph=tuple((state.name, state.members) for state in states),
        group_states=states,
        proxy_nodes=(gateway_a, gateway_b, underlay),
    )
    payload: dict[str, object] = {
        "proxies": {"proxies": {}},
        "proxy_map": {
            gateway_a.proxy_name: {
                "tier": gateway_a.tier,
                "pool_kind": gateway_a.pool_kind,
                "identity": gateway_a.identity,
                "proxy": {"dialer-proxy": "PROXY-FALLBACK"},
            },
            gateway_b.proxy_name: {
                "tier": gateway_b.tier,
                "pool_kind": gateway_b.pool_kind,
                "identity": gateway_b.identity,
                "proxy": {"dialer-proxy": "PROXY-FALLBACK"},
            },
            underlay.proxy_name: {
                "tier": underlay.tier,
                "pool_kind": underlay.pool_kind,
                "identity": underlay.identity,
            },
        },
    }
    return snapshot, payload


def test_gateway_initial_candidate_keeps_identity_and_only_switches_underlay() -> None:
    runtime = object.__new__(DesktopRuntime)
    snapshot, payload = _gateway_candidate_authority()
    runtime._active_gateway_policy_enabled = lambda: True
    runtime.manager = SimpleNamespace(
        active_exit_proxy_name=lambda: "gateway-b",
    )

    plans = runtime._runtime_initial_candidate_plans(snapshot, payload)

    assert len(plans) == 1
    assert plans[0]["ref"].proxy_name == "gateway-b"
    assert plans[0]["probe_ref"].proxy_name == "underlay-leaf"
    assert plans[0]["expected_underlay_key"] == plans[0]["probe_ref"].key
    assert [
        (action["group"], action["target"])
        for action in plans[0]["actions"]
    ] == [
        ("PROXY-FALLBACK", "underlay-leaf"),
        ("FINAL-EXIT", "EXIT-SELECT"),
    ]


def test_gateway_authority_exception_produces_no_candidate_or_commit() -> None:
    runtime = object.__new__(DesktopRuntime)
    snapshot, payload = _gateway_candidate_authority()
    runtime._active_gateway_policy_enabled = lambda: True
    runtime.manager = SimpleNamespace(
        active_exit_proxy_name=lambda: (_ for _ in ()).throw(
            RuntimeError("manager authority unavailable")
        ),
    )

    assert runtime._runtime_initial_candidate_plans(snapshot, payload) == []


def test_bootstrap_promotion_does_not_gate_activation_on_controller_readback() -> None:
    """A usable no-capture Core must reach its 204 activation path first."""

    runtime = object.__new__(DesktopRuntime)
    runner = _RunningCore()
    phase = ["bootstrap"]
    queued: list[str] = []
    ready: list[tuple[bool, str]] = []
    holds: list[str] = []

    runtime.manager = SimpleNamespace()
    runtime.logger = logging.getLogger("test-bootstrap-promotion-controller-delay")
    runtime._get_core_runner = lambda: runner
    runtime._fail_terminal_tier3_bootstrap = lambda _manager: False
    runtime._current_core_runtime_phase = lambda: phase[0]
    runtime._hot_reload_runtime_config_phase = (
        lambda *_args, **_kwargs: phase.__setitem__(0, "formal_no_capture")
    )
    runtime._core_lifecycle_lock = threading.RLock()
    runtime._core_generation = 3
    runtime.running = False
    runtime._desired_running = True
    runtime._stop_request_pending = False
    runtime.stop_event = threading.Event()
    runtime._runtime_intent_lock = threading.RLock()
    runtime.core_runner = runner
    runtime._runtime_initial_activation_was_completed = lambda: False
    runtime._mark_tier3_core_process_ready = lambda **_kwargs: None
    runtime._refresh_loaded_runtime_authority = lambda **_kwargs: None
    runtime._publish_tier3_controller_hold = lambda reason, **_kwargs: holds.append(
        str(reason)
    )

    def queue_activation() -> None:
        queued.append("activation")
        # The actual activation worker owns the mixed-port 204.  Keep the
        # fixture local and deterministic while proving promotion did not
        # suppress that successful data-plane path.
        ready.append((True, "full mixed-port 204"))

    runtime._queue_tier3_egress_activation = queue_activation

    result = runtime._promote_bootstrap_core_once()

    assert result["running"] is True
    assert phase == ["formal_no_capture"]
    assert queued == []
    assert ready == []
    runtime.running = True
    assert runtime._request_initial_egress_admission() is True
    assert queued == ["activation"]
    assert ready == [(True, "full mixed-port 204")]
    assert holds == []


def test_concurrent_bootstrap_promoters_hot_reload_once_and_share_admission() -> None:
    runtime = object.__new__(DesktopRuntime)
    runner = _RunningCore()
    phase = ["bootstrap"]
    reloads: list[str] = []
    queued: list[str] = []
    errors: list[BaseException] = []
    start = threading.Barrier(3)

    runtime.manager = SimpleNamespace()
    runtime.logger = logging.getLogger("test-concurrent-bootstrap-promoters")
    runtime.core_runner = runner
    runtime._get_core_runner = lambda: runner
    runtime._fail_terminal_tier3_bootstrap = lambda _manager: False
    runtime._primary_runtime_healthy_count = lambda: 1
    runtime._require_tier3_ready_for_core_start = lambda: None
    runtime._core_transition_lock = threading.RLock()
    runtime._core_lifecycle_lock = threading.RLock()
    runtime._core_generation = 3
    runtime._runtime_intent_lock = threading.RLock()
    runtime._desired_running = True
    runtime._stop_request_pending = False
    runtime._lifecycle_revision = 9
    runtime.running = True
    runtime.stop_event = threading.Event()
    runtime._current_core_runtime_phase = lambda: phase[0]
    runtime._runtime_initial_activation_was_completed = lambda: False
    runtime._mark_tier3_core_process_ready = lambda **_kwargs: None

    def reload_once(*_args: object, **_kwargs: object) -> None:
        reloads.append("reload")
        phase[0] = "formal_no_capture"

    runtime._hot_reload_runtime_config_phase = reload_once
    runtime._queue_tier3_egress_activation = (
        lambda: queued.append("activation") if not queued else None
    )

    def promote() -> None:
        start.wait()
        try:
            runtime._start_core_from_committed_tier3_pool()
        except BaseException as exc:  # pragma: no cover - assertion records it
            errors.append(exc)

    threads = [threading.Thread(target=promote) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=2.0)

    assert errors == []
    assert reloads == ["reload"]
    assert phase == ["formal_no_capture"]
    assert queued == ["activation"]


def test_unready_unarmed_core_output_cannot_enter_runtime_recovery() -> None:
    """Startup log noise must not create an incident or a selector mutation."""

    runtime = object.__new__(DesktopRuntime)
    snapshot, payload, _binding = _loaded_authority()
    recorder = _IngressRecorder(snapshot)
    runtime.stop_event = threading.Event()
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_route_snapshot = snapshot
    runtime._runtime_route_snapshot_payload = payload
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_facility_epoch = 0
    runtime._runtime_egress_coordinator = recorder
    runtime._runtime_recovery_arm = None
    runtime._runtime_recovery_is_armed = lambda *_args, **_kwargs: False
    runtime._runtime_facility_hold_reason = lambda: ""
    runtime._runtime_node_attribution_hold_reason = lambda: ""
    forbidden: list[str] = []
    runtime._start_runtime_independent_probe = (
        lambda *_args, **_kwargs: forbidden.append("probe")
    )
    runtime._start_runtime_provisional_diversion = (
        lambda *_args, **_kwargs: forbidden.append("selector") or False
    )
    runtime._start_runtime_recovery = (
        lambda *_args, **_kwargs: forbidden.append("recovery")
    )

    runtime._on_core_output_event(_dial_failure_event())

    assert recorder.ingress == []
    assert recorder.guards == []
    assert forbidden == []


def test_pre_ready_output_queued_before_arm_cannot_become_live_incident(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = object.__new__(DesktopRuntime)
    snapshot, payload, binding = _loaded_authority()
    parsed: list[str] = []
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_recovery_arm = _RuntimeRecoveryArm(
        worker_token=(9, 11),
        snapshot_key=tuple(snapshot.snapshot_key),
        binding_key=tuple(binding.node.key),
        route_epoch=snapshot.route_epoch,
        facility_epoch=2,
        selector_intent_revision=4,
        lifecycle_revision=9,
        armed_at_monotonic=20.0,
    )
    runtime._runtime_recovery_is_armed = lambda *_args, **_kwargs: True
    monkeypatch.setattr(
        desktop_runtime_module,
        "runtime_transport_failure_from_log",
        lambda *_args, **_kwargs: parsed.append("parsed"),
    )

    runtime._handle_runtime_output_event_early(
        _dial_failure_event(),
        snapshot,
        payload,
    )

    assert parsed == []


def test_only_exact_activation_owner_can_admit_pre_ready_recovery() -> None:
    """The startup exception is one narrow owner, not a second live arm."""

    runtime = object.__new__(DesktopRuntime)
    snapshot, payload, binding = _loaded_authority()
    coordinator = RuntimeEgressCoordinator()
    coordinator.set_current_snapshot(snapshot)
    now = time.monotonic()

    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_route_snapshot = snapshot
    runtime._runtime_route_snapshot_payload = payload
    runtime._runtime_egress_coordinator = coordinator
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_recovery_arm = None
    runtime._runtime_recovery_tickets = {}
    runtime._runtime_recovery_outcomes = {}
    runtime._runtime_recovery_outcome_events = {}
    runtime._runtime_incident_recovery_state = {}
    runtime._runtime_recovery_ticket_revision = 0
    runtime._runtime_facility_epoch = 2
    runtime._runtime_selector_intent_revision = 4
    runtime._lifecycle_revision = 9
    runtime._tier3_egress_activation_generation = 7
    runtime._runtime_initial_activation_complete = lambda: False
    runtime._runtime_worker_token = lambda: (9, 11)
    runtime._runtime_worker_token_is_current = (
        lambda token: tuple(token) == (9, 11)
    )
    runtime._runtime_recovery_monotonic = lambda: now
    runtime._runtime_facility_hold_reason = lambda: ""
    runtime._runtime_node_attribution_hold_reason = lambda: ""
    runtime._runtime_recovery_authority_key = (
        lambda _snapshot: ("activation", "stable")
    )
    runtime._fresh_runtime_hot_standby = lambda *_args, **_kwargs: None
    runtime._start_runtime_provisional_diversion = (
        lambda *_args, **_kwargs: False
    )
    probes: list[str] = []
    runtime._start_runtime_independent_probe = (
        lambda *_args, **_kwargs: probes.append("probe")
    )
    runtime._queue_runtime_route_snapshot_refresh = lambda: None
    runtime._log_runtime_recovery_phase = lambda *_args, **_kwargs: None

    owner = _RuntimeActivationRecoveryOwner(
        activation_generation=7,
        worker_token=(9, 11),
        snapshot_key=tuple(snapshot.snapshot_key),
        binding_key=tuple(binding.node.key),
        facility_epoch=2,
        selector_intent_revision=4,
        lifecycle_revision=9,
        deadline_monotonic=now + 6.0,
    )
    stale_owner = _RuntimeActivationRecoveryOwner(
        activation_generation=7,
        worker_token=(9, 11),
        snapshot_key=tuple(snapshot.snapshot_key),
        binding_key=tuple(binding.node.key),
        facility_epoch=2,
        selector_intent_revision=3,
        lifecycle_revision=9,
        deadline_monotonic=now + 6.0,
    )
    binding_state = (binding, snapshot, payload)

    assert runtime._submit_current_route_probe_failure(
        "activation failed without owner",
        source="activation_final_exit",
        binding_state=binding_state,
        binding_captured=True,
    ) is False
    assert runtime._submit_current_route_probe_failure(
        "live probe cannot borrow activation owner",
        source="runtime_probe",
        binding_state=binding_state,
        binding_captured=True,
        activation_owner=owner,
    ) is False
    assert runtime._submit_current_route_probe_failure(
        "stale activation owner",
        source="activation_final_exit",
        binding_state=binding_state,
        binding_captured=True,
        activation_owner=stale_owner,
    ) is False

    ticket = runtime._submit_current_route_probe_failure(
        "first FINAL-EXIT 204 failed",
        source="activation_final_exit",
        binding_state=binding_state,
        binding_captured=True,
        activation_owner=owner,
    )

    assert ticket is not False
    assert ticket.deadline_monotonic <= owner.deadline_monotonic
    assert probes == ["probe"]
    assert runtime._runtime_recovery_arm is None


def test_queued_output_is_rechecked_against_arm_before_incident_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An event queued before disarm cannot be replayed into a later route."""

    runtime = object.__new__(DesktopRuntime)
    snapshot, payload, _binding = _loaded_authority()
    runtime._runtime_recovery_arm = None
    runtime._runtime_recovery_is_armed = lambda *_args, **_kwargs: False
    forbidden: list[str] = []
    runtime._runtime_facility_epoch = 0
    runtime._runtime_facility_hold_reason = lambda: ""
    runtime._runtime_node_attribution_hold_reason = lambda: ""
    runtime._runtime_transport_failure_is_internal_probe = lambda _failure: False
    runtime._runtime_egress_coordinator = _IngressRecorder(snapshot)
    runtime._bind_runtime_transport_failure = (
        lambda *_args, **_kwargs: forbidden.append("bind")
    )
    runtime._start_runtime_facility_confirmation = (
        lambda *_args, **_kwargs: forbidden.append("facility")
    )
    runtime._start_runtime_independent_probe = (
        lambda *_args, **_kwargs: forbidden.append("probe")
    )
    runtime._start_runtime_recovery = (
        lambda *_args, **_kwargs: forbidden.append("recovery")
    )
    monkeypatch.setattr(
        desktop_runtime_module,
        "runtime_transport_failure_from_log",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )

    runtime._handle_runtime_output_event(
        _dial_failure_event(),
        snapshot,
        payload,
    )

    assert forbidden == []


def test_runtime_recovery_arms_only_after_ready_with_coherent_authority() -> None:
    runtime = object.__new__(DesktopRuntime)
    snapshot, payload, binding = _loaded_authority()
    coordinator = RuntimeEgressCoordinator()
    coordinator.set_current_snapshot(snapshot)
    phase = ["formal_capture"]
    runner = _RunningCore()

    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_route_snapshot = snapshot
    runtime._runtime_route_snapshot_payload = payload
    runtime._runtime_egress_coordinator = coordinator
    runtime._runtime_recovery_arm = None
    runtime._runtime_route_epoch = snapshot.route_epoch
    runtime._runtime_network_context_epoch = snapshot.network_context_epoch
    runtime._runtime_facility_epoch = 2
    runtime._runtime_selector_intent_revision = 4
    runtime._lifecycle_revision = 9
    runtime._core_generation = snapshot.core_generation
    runtime._runtime_initial_activation_generation = -1
    runtime.core_runner = runner
    runtime._current_core_runtime_phase = lambda: phase[0]
    runtime._runtime_traffic_capture_wanted = lambda: True
    runtime._runtime_worker_token = lambda: (9, 11, 5, 2)
    runtime._runtime_worker_token_is_current = lambda _token: True
    runtime._tier3_activation_authority_state = (
        lambda **_kwargs: (snapshot, payload, binding)
    )
    runtime._refresh_loaded_runtime_authority = (
        lambda **_kwargs: (snapshot, payload, binding)
    )
    runtime.manager = SimpleNamespace(effective_egress_ok=None)
    runtime._runtime_egress_readiness = RuntimeEgressReadiness(
        state=RuntimeEgressReadinessState.ACTIVATION_HOLD,
        validation_active=True,
        lifecycle_revision=runtime._lifecycle_revision,
        route_epoch=snapshot.route_epoch,
        network_context_epoch=snapshot.network_context_epoch,
    )

    runtime._try_arm_runtime_recovery()

    assert runtime._runtime_recovery_arm is None
    assert runtime._runtime_recovery_is_armed() is False

    runtime.manager.effective_egress_ok = True
    runtime._runtime_initial_activation_generation = snapshot.core_generation
    runtime._runtime_egress_readiness = RuntimeEgressReadiness(
        state=RuntimeEgressReadinessState.READY,
        validation_active=False,
        lifecycle_revision=runtime._lifecycle_revision,
        route_epoch=snapshot.route_epoch,
        network_context_epoch=snapshot.network_context_epoch,
    )

    runtime._try_arm_runtime_recovery()

    assert runtime._runtime_recovery_arm is not None
    assert runtime._runtime_recovery_is_armed() is True

    runtime._runtime_route_epoch += 1
    assert runtime._runtime_recovery_is_armed() is False
    runtime._disarm_runtime_recovery("route generation changed")
    assert runtime._runtime_recovery_arm is None


def test_early_confirmation_probes_current_and_alternative_with_same_group_contract() -> None:
    runtime = object.__new__(DesktopRuntime)
    snapshot, payload, binding = _loaded_authority()
    alternative = RuntimeNodeRef(
        tier="tier1",
        pool_kind="tier1_variant",
        identity="leaf-b-id",
        proxy_name="leaf-b",
    )
    failure = BoundRuntimeFailure(
        binding=binding,
        protocol="TCP",
        observed_outbound="FINAL-EXIT",
        destination_token="contract-test",
        error_type="timeout",
        observed_at_monotonic=time.monotonic(),
    )
    ticket = _RuntimeRecoveryTicket(
        generation=1,
        incident_id="contract-incident",
        observed_at_monotonic=time.monotonic(),
        deadline_monotonic=time.monotonic() + 1.0,
        snapshot_key=tuple(snapshot.snapshot_key),
        route_epoch=snapshot.route_epoch,
        facility_epoch=0,
        selector_intent_revision=0,
        worker_token=(1,),
        from_ref=binding.node,
    )
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_failure_probe_bindings = set()
    runtime._runtime_failure_recovery_threads = set()
    runtime._runtime_incident_bindings = {}
    runtime._runtime_incident_route_payloads = {}
    runtime._runtime_recovery_now = time.monotonic
    runtime._ensure_runtime_recovery_ticket = lambda *_args, **_kwargs: ticket
    runtime._runtime_candidate_health_snapshot = lambda: None
    runtime._runtime_ordered_alternative_refs = (
        lambda *_args, **_kwargs: [alternative]
    )
    runtime._runtime_alternative_switch_action = (
        lambda *_args, **_kwargs: {
            "group": "TIER1-AUTO",
            "target": alternative.proxy_name,
            "mode": "fixed",
        }
    )
    contracts: list[str] = []

    def group_contract(_snapshot, group):
        contracts.append(str(group))
        return "https://group.invalid/generate_204", "204"

    runtime._runtime_group_probe_contract = group_contract
    runtime._runtime_confirmation_timeout_ms = lambda *_args, **_kwargs: 500
    runtime._proxy_delay_failure_is_transient = lambda _exc: False
    observed: list[tuple[str, str, str]] = []

    def probe(ref, _payload, *, probe_url, expected_status, **_kwargs):
        observed.append((ref.proxy_name, probe_url, expected_status))
        if ref.key == binding.node.key:
            raise RuntimeError("current leaf failed")
        return {"delay": 8}

    runtime._probe_runtime_leaf = probe
    confirmed = threading.Event()
    recovery_calls: list[dict[str, object]] = []

    def recover(*_args, **kwargs):
        recovery_calls.append(dict(kwargs))
        confirmed.set()

    runtime._run_runtime_early_confirmed_recovery = recover
    runtime._complete_runtime_early_indeterminate = (
        lambda *_args, **_kwargs: pytest.fail(
            "a proven differential must not finish indeterminate"
        )
    )

    returned = runtime._start_runtime_early_confirmation(
        failure,
        snapshot,
        payload,
        probe_token="probe-token",
        incident_id=ticket.incident_id,
    )

    assert returned is ticket
    assert confirmed.wait(timeout=1.0)
    assert contracts == ["TIER1-AUTO"]
    assert sorted(observed) == sorted(
        [
            (
                binding.node.proxy_name,
                "https://group.invalid/generate_204",
                "204",
            ),
            (
                alternative.proxy_name,
                "https://group.invalid/generate_204",
                "204",
            ),
        ]
    )
    evidence = recovery_calls[0]["alternative_probe_evidence"]
    assert evidence.ref == alternative
    assert evidence.selection_group == "TIER1-AUTO"
    assert evidence.selection_target == alternative.proxy_name
    assert evidence.probe_url == "https://group.invalid/generate_204"
    assert evidence.expected_status == "204"


@pytest.mark.parametrize(
    "pool_kind,gateway_failure_confirmed,expected",
    [
        ("tier1_variant", False, False),
        ("tier1_variant", True, False),
        ("tier2", True, False),
        ("tier3a", True, False),
        ("tier3b", True, False),
        ("exit", True, False),
        ("gateway_underlay", True, False),
        ("gateway", False, False),
        ("gateway", True, True),
    ],
)
def test_physical_fail_closed_is_reserved_for_explicit_fixed_gateway_failure(
    pool_kind: str,
    gateway_failure_confirmed: bool,
    expected: bool,
) -> None:
    _snapshot, _payload, binding = _loaded_authority(pool_kind=pool_kind)

    allowed = DesktopRuntime._runtime_physical_fail_closed_allowed(
        binding,
        gateway_failure_confirmed=gateway_failure_confirmed,
    )

    assert allowed is expected


@pytest.mark.parametrize(
    "terminal_reason",
    [
        "controller unavailable",
        "selector readback missing",
        "recovery deadline expired",
        "route epoch became stale",
    ],
)
def test_non_gateway_recovery_uncertainty_cannot_reach_reject_or_core_stop(
    terminal_reason: str,
) -> None:
    """Defense in depth: even a misrouted hold cannot physically close Core."""

    runtime = object.__new__(DesktopRuntime)
    _snapshot, _payload, binding = _loaded_authority(pool_kind="tier1_variant")
    runner = _RunningCore()
    forbidden: list[str] = []
    runtime.core_runner = runner
    runtime._runtime_facility_epoch = 2
    runtime._runtime_route_epoch = binding.route_epoch
    runtime._runtime_selector_intent_revision = 1
    runtime._core_transition_lock = threading.RLock()
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._activate_killswitch_for_egress_failure = (
        lambda _reason: forbidden.append("killswitch") or True
    )
    runtime._require_api_client = lambda: forbidden.append("REJECT")
    runtime.stop_core = lambda **_kwargs: forbidden.append("stop")

    result = runtime._enforce_runtime_fail_closed(
        terminal_reason,
        failure_binding=binding,
        gateway_failure_confirmed=False,
        expected_facility_epoch=2,
    )

    assert result["enforced"] is False
    assert result["selected_reject"] is False
    assert forbidden == []
    assert runner.is_running() is True


def test_old_gateway_failure_cannot_cross_a_facility_epoch() -> None:
    runtime = object.__new__(DesktopRuntime)
    _snapshot, _payload, binding = _loaded_authority(pool_kind="gateway")
    forbidden: list[str] = []
    runtime.core_runner = _RunningCore()
    runtime._runtime_facility_epoch = 3
    runtime._runtime_route_epoch = binding.route_epoch
    runtime._runtime_selector_intent_revision = 1
    runtime._core_transition_lock = threading.RLock()
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._activate_killswitch_for_egress_failure = (
        lambda _reason: forbidden.append("killswitch") or True
    )
    runtime._require_api_client = lambda: forbidden.append("REJECT")
    runtime.stop_core = lambda **_kwargs: forbidden.append("stop")

    result = runtime._enforce_runtime_fail_closed(
        "old fixed Gateway failure",
        failure_binding=binding,
        gateway_failure_confirmed=True,
        expected_facility_epoch=2,
    )

    assert result["attempted"] is False
    assert result["enforced"] is False
    assert result["selected_reject"] is False
    assert forbidden == []
    assert runtime.core_runner.is_running() is True


def test_explicit_fixed_gateway_failure_retains_physical_fail_closed_path() -> None:
    """The new admission gate must not weaken the existing Gateway contract."""

    runtime = object.__new__(DesktopRuntime)
    _snapshot, _payload, binding = _loaded_authority(pool_kind="gateway")

    assert runtime._runtime_physical_fail_closed_allowed(
        binding,
        gateway_failure_confirmed=True,
    ) is True


def test_startup_dual_204_runs_in_parallel_for_healthy_slow_leaf() -> None:
    runtime = object.__new__(DesktopRuntime)
    snapshot, _payload, _binding = _loaded_authority()
    runtime.config = {}
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
    runtime._current_mihomo_runtime_config_path = lambda: "runtime.yaml"
    runtime._wait_for_mihomo_route_ready = lambda **_kwargs: None
    runtime._runtime_worker_token_is_current = lambda _token: True
    calls: list[tuple[str, float]] = []

    def slow_healthy_probe(*, timeout_seconds, excluded_destination, **_kwargs):
        calls.append((str(excluded_destination), float(timeout_seconds)))
        time.sleep(0.12)
        return {"status": 204, "url": excluded_destination or "primary"}

    runtime._probe_mihomo_mixed_port_once = slow_healthy_probe

    started = time.monotonic()
    result = DesktopRuntime._verify_runtime_final_exit(
        runtime,
        expected_worker_token=(1,),
        expected_snapshot=snapshot,
        timeout_seconds=0.5,
        require_controller_ready=True,
        required_successes=2,
        parallel_probes=True,
        total_timeout_cap_seconds=0.5,
        per_probe_timeout_cap_seconds=0.4,
    )
    elapsed = time.monotonic() - started

    assert result["status"] == 204
    assert result["successful_destinations"] == 2
    assert len(calls) == 2
    assert calls[0][0] != calls[1][0]
    assert elapsed < 0.22


def test_startup_reuses_only_current_core_exact_group_alive_evidence() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime.config = {}
    snapshot, payload, (_bad, good) = _unbound_loaded_authority()
    entries = payload["proxies"]["proxies"]
    group_url = "https://probe.invalid/generate_204"
    entries[good.proxy_name]["alive"] = True
    entries[good.proxy_name]["extra"] = {group_url: {"alive": True, "history": []}}

    evidence = runtime._initial_core_loaded_probe_evidence(
        snapshot,
        payload,
        good,
        "TIER3B-RUNTIME",
    )

    assert evidence is not None
    assert evidence["evidence_source"] == "current_core_group_urltest"
    assert evidence["probe_url"] == group_url
    entries[good.proxy_name]["extra"][group_url]["alive"] = False
    assert (
        runtime._initial_core_loaded_probe_evidence(
            snapshot,
            payload,
            good,
            "TIER3B-RUNTIME",
        )
        is None
    )


def test_startup_evidence_ignores_top_level_alive_from_another_url() -> None:
    """A member alive only for a neutral 204 URL is not group evidence.

    Mihomo's top-level ``alive`` is the latest URLTest for any URL; the
    fallback group selects by the per-URL verdict for its own test URL.
    """

    runtime = object.__new__(DesktopRuntime)
    runtime.config = {}
    snapshot, payload, (_bad, good) = _unbound_loaded_authority()
    entry = payload["proxies"]["proxies"][good.proxy_name]
    entry["alive"] = True
    entry["extra"] = {"https://cp.cloudflare.com": {"alive": True, "history": []}}

    assert (
        runtime._initial_core_loaded_probe_evidence(
            snapshot,
            payload,
            good,
            "TIER3B-RUNTIME",
        )
        is None
    )
    entry.pop("extra")
    assert (
        runtime._initial_core_loaded_probe_evidence(
            snapshot,
            payload,
            good,
            "TIER3B-RUNTIME",
        )
        is None
    )


def _formatter_tier3_route_authority(
    *,
    neutral_url: str,
    real_site_url: str,
    leaf_states: dict[str, dict[str, object]],
) -> tuple[
    CoreLoadedRouteSnapshot,
    dict[str, object],
    tuple[RuntimeNodeRef, ...],
]:
    """Mirror Mihomo's /proxies readback of the canonical formatter output."""

    import yaml

    from clash_formatter import build_full_clash_config
    from models import Node

    names = ("tier3-first", "tier3-second")
    rendered = yaml.safe_load(
        build_full_clash_config(
            {
                "tier1": [],
                "tier2": [],
                "tier3": [
                    Node(
                        name=name,
                        protocol="ss",
                        uri=f"ss://{name}",
                        address=f"{name}.example.com",
                        port=8388,
                        method="aes-128-gcm",
                        password="pass",
                        tier="tier3",
                    )
                    for name in names
                ],
                "exit": [],
            },
            probe_url=neutral_url,
            tier3_health_check_url=real_site_url,
        )
    )
    entries: dict[str, object] = {}
    for group in rendered["proxy-groups"]:
        members = list(group.get("proxies") or [])
        entry: dict[str, object] = {
            "type": "Fallback" if group["type"] == "fallback" else "Selector",
            "all": members,
            "now": members[0] if members else "",
        }
        if group["type"] == "fallback":
            # Mihomo publishes the group's own URL/status contract.
            entry["testUrl"] = group["url"]
            entry["expectedStatus"] = str(group["expected-status"])
        entries[group["name"]] = entry
    # Unbound startup route: FINAL-EXIT still holds its REJECT member.
    entries["FINAL-EXIT"]["now"] = "REJECT"
    for proxy in rendered["proxies"]:
        entries[proxy["name"]] = {
            "type": "Shadowsocks",
            **copy.deepcopy(leaf_states.get(proxy["name"], {})),
        }
    entries["REJECT"] = {"type": "Reject"}
    payload: dict[str, object] = {"proxies": {"proxies": entries}}
    graph, _selection, states = DesktopRuntime._runtime_group_graph_and_selection(
        payload["proxies"]
    )
    refs = tuple(
        RuntimeNodeRef(
            tier="tier3",
            pool_kind="tier3b",
            identity=f"{name}-id",
            proxy_name=name,
        )
        for name in names
    )
    snapshot = CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="formatter-route-digest",
        route_epoch=11,
        network_context_epoch=5,
        group_graph=graph,
        group_states=states,
        proxy_nodes=refs,
    )
    return snapshot, payload, refs


def test_tier3_route_probes_share_the_loaded_real_site_group_contract() -> None:
    neutral = "https://neutral.invalid/generate_204"
    real_site = "https://real-site.invalid/generate_204"
    # Both members answer the neutral 204; neither has proven the real site.
    snapshot, payload, (first, second) = _formatter_tier3_route_authority(
        neutral_url=neutral,
        real_site_url=real_site,
        leaf_states={
            "tier3-first": {
                "alive": True,
                "extra": {
                    neutral: {"alive": True, "history": []},
                    real_site: {"alive": False, "history": []},
                },
            },
            "tier3-second": {
                "alive": True,
                "extra": {neutral: {"alive": True, "history": []}},
            },
        },
    )
    runtime = object.__new__(DesktopRuntime)
    runtime.config = {"testing": {"mihomo_helper": {"probe_urls": [neutral]}}}
    runtime._active_gateway_policy_enabled = lambda: False
    runtime._require_api_client = lambda: pytest.fail(
        "an unproven startup route must not commit any selector"
    )

    assert runtime._runtime_group_probe_contract(snapshot, "TIER3-AUTO") == (
        real_site,
        "204",
    )
    assert runtime._runtime_group_probe_contract(snapshot, "PROXY-FALLBACK") == (
        neutral,
        "204",
    )
    plans = runtime._runtime_initial_candidate_plans(snapshot, payload)
    assert [plan["probe_ref"].proxy_name for plan in plans] == [
        first.proxy_name,
        second.proxy_name,
    ]
    assert {plan["probe_group"] for plan in plans} == {"TIER3-AUTO"}
    for ref in (first, second):
        assert (
            runtime._initial_core_loaded_probe_evidence(
                snapshot,
                payload,
                ref,
                "TIER3-AUTO",
            )
            is None
        )

    observed: list[tuple[str, str, str]] = []
    observed_lock = threading.Lock()

    def real_site_fails(ref, _payload, *, probe_url, expected_status, **_kwargs):
        with observed_lock:
            observed.append((ref.proxy_name, str(probe_url), str(expected_status)))
        raise RuntimeError("SSL EOF on the real site")

    runtime._probe_runtime_leaf = real_site_fails
    # Failure mode: no member passes the real-site check.  Startup adopts no
    # candidate and selects nothing; it never falls back to the neutral URL.
    with pytest.raises(RuntimeError, match="no verified Core-loaded candidate"):
        runtime._activate_initial_unbound_candidate(
            snapshot=snapshot,
            snapshot_payload=payload,
            admission_lease=SimpleNamespace(
                deadline_monotonic=time.monotonic() + 2.0,
            ),
            ensure_current=lambda _stage: None,
        )
    assert sorted(observed) == [
        (first.proxy_name, real_site, "204"),
        (second.proxy_name, real_site, "204"),
    ]

    # Once Mihomo's own real-site URLTest proves a member, that exact per-URL
    # verdict is reused without another probe.
    entries = payload["proxies"]["proxies"]
    entries[second.proxy_name]["extra"][real_site] = {"alive": True, "history": []}
    observed.clear()
    accepted = runtime._probe_initial_candidate_plans(
        plans,
        snapshot,
        payload,
        deadline_monotonic=time.monotonic() + 2.0,
    )
    assert accepted is not None
    assert accepted["probe_ref"] == second
    assert accepted["probe_result"]["evidence_source"] == "current_core_group_urltest"
    assert accepted["probe_result"]["probe_url"] == real_site
    assert observed == [(first.proxy_name, real_site, "204")]


def _unfixed_tier3_route(
    members: tuple[str, ...],
    *,
    now: str,
    fixed: str = "",
    final_now: str = "TIER3-AUTO",
) -> dict[str, object]:
    """Mihomo /proxies readback with an unfixed native TIER3-AUTO pick."""

    entries: dict[str, object] = {
        "FINAL-EXIT": {
            "type": "Selector",
            "now": final_now,
            "all": ["TIER3-AUTO", "REJECT"],
        },
        "TIER3-AUTO": {
            "type": "Fallback",
            "now": now,
            "fixed": fixed,
            "all": list(members),
        },
        "REJECT": {"type": "Reject"},
    }
    for name in members:
        if name != "REJECT":
            entries[name] = {"type": "Shadowsocks"}
    return {"proxies": entries}


def _unfixed_provider_growth_case(tmp_path):
    """Append-only Tier3B provider growth under a real-site-dead first member.

    node-00 answers only the neutral probes, so before the update Mihomo's
    unfixed TIER3-AUTO natively serves node-01.
    """

    from unittest.mock import MagicMock

    from clash_formatter import build_capture_disabled_clash_config
    from tests.test_desktop_runtime_tier3_provider_contract import (
        _Coordinator,
        _RunningCore as _ProviderRunningCore,
        _SequencedProviderClient,
        _numbered_projection,
        _projection_proxy_map,
        _runtime_shell,
        _write_loaded_main_file,
    )

    runtime = _runtime_shell(tmp_path)
    old_projection = _numbered_projection(3)
    candidate_projection = _numbered_projection(4)
    old_map = _projection_proxy_map(old_projection)
    candidate_map = _projection_proxy_map(candidate_projection)
    loaded_main = build_capture_disabled_clash_config(old_projection.main_yaml)
    runtime._runtime_proxy_name_map = lambda: old_map
    old_contract = runtime._runtime_export_contract_for_payload(
        loaded_main,
        provider_payload=old_projection.provider_yaml,
    )
    old_contract["runtime_config_path"] = str(
        _write_loaded_main_file(runtime, loaded_main)
    )
    old_contract = {**old_contract, "revision": 7}
    runtime.stop_event = threading.Event()
    runtime.core_runner = _ProviderRunningCore()
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_route_mutation_lease = None
    runtime._runtime_loaded_export_contract = old_contract
    runtime._runtime_loaded_export_revision = 7
    runtime._runtime_pending_export_contract = None
    runtime._runtime_route_epoch = 11
    runtime._runtime_network_context_epoch = 3
    runtime._runtime_selector_intent_revision = 5
    runtime._runtime_route_snapshot = None
    runtime._runtime_route_snapshot_payload = None
    runtime._runtime_route_selection_signature = ()
    runtime._runtime_hot_standby_lock = threading.RLock()
    runtime._runtime_local_facility_fault_reason = ""
    runtime._runtime_egress_coordinator = _Coordinator()
    runtime._tier3_core_provider_last_projection = old_projection
    runtime._runtime_proxy_name_map = lambda: candidate_map
    runtime._tier3_egress_activation_lock = threading.RLock()
    runtime._tier3_egress_activation_pending = False
    runtime._tier3_egress_activation_worker_running = False
    runtime._tier3_egress_activation_active_mode = ""
    runtime._tier3_standby_activation_hold_kind = ""
    runtime._reset_tier3_standby_activation_failure = MagicMock()
    runtime._queue_tier3_egress_activation = MagicMock()
    runtime.manager = SimpleNamespace(
        lock=threading.RLock(),
        tier3_pool_revision=41,
    )
    provider_path = runtime._tier3_core_provider_file_path()
    provider_path.parent.mkdir(parents=True, exist_ok=True)
    provider_path.write_bytes(old_projection.provider_yaml.encode("utf-8"))
    old_members = tuple(old_projection.provider_member_names)
    candidate_members = tuple(candidate_projection.provider_member_names)
    assert old_members == ("node-00", "node-01", "node-02", "REJECT")
    assert candidate_members[: len(old_members) - 1] == old_members[:-1]
    client = _SequencedProviderClient(provider_path, [candidate_members, old_members])
    pool_marker = ("pool", 4)
    binding = {
        "route_guard": runtime._tier3_provider_route_guard(
            {"proxies": _unfixed_tier3_route(old_members, now="node-01")}
        ),
        "tier3_pool_revision": 41,
        "tier3_pool_marker": pool_marker,
        "contract_revision": 7,
        "contract_identity": id(old_contract),
        "semantic_digest": str(old_contract["semantic_digest"]),
        "route_epoch": 11,
        "network_context_epoch": 3,
        "selector_intent_revision": 5,
    }
    return SimpleNamespace(
        runtime=runtime,
        client=client,
        binding=binding,
        pool_marker=pool_marker,
        provider_path=provider_path,
        old_contract=old_contract,
        old_bytes=old_projection.provider_yaml.encode("utf-8"),
        candidate_bytes=candidate_projection.provider_yaml.encode("utf-8"),
        candidate_projection=candidate_projection,
        old_members=old_members,
        candidate_members=candidate_members,
    )


def _run_unfixed_provider_growth(case, proxies_side_effect):
    from unittest.mock import patch

    runtime = case.runtime
    with (
        patch.object(runtime, "_current_core_runtime_phase", return_value="formal_no_capture"),
        patch.object(runtime, "_runtime_recovery_transaction_active", return_value=False),
        patch.object(runtime, "_runtime_initial_activation_complete", return_value=False),
        patch.object(
            runtime,
            "_render_mihomo_runtime_payload_base_with_provider",
            return_value=(
                case.candidate_projection.main_yaml,
                case.candidate_projection,
            ),
        ),
        patch.object(runtime, "_tier3_provider_mutation_binding", return_value=case.binding),
        patch.object(runtime, "_tier3_provider_mutation_binding_is_current", return_value=True),
        patch.object(runtime, "_tier3_core_sync_marker", return_value=case.pool_marker),
        patch.object(runtime, "_require_api_client", return_value=case.client),
        patch.object(runtime, "mihomo_proxies", side_effect=proxies_side_effect),
        patch.object(runtime, "_refresh_runtime_route_snapshot", return_value=None),
    ):
        return runtime._update_running_tier3_core_provider_leased(
            marker=case.pool_marker
        )


def test_provider_growth_judges_unfixed_tier3_pick_after_mihomo_resettles(
    tmp_path,
) -> None:
    # Mihomo's provider PUT rebuilds members with fresh health, so the unfixed
    # fallback transiently serves node-00 (dead for the real-site URL) until
    # its post-update URLTest round restores node-01.  The route guard must
    # judge that settled pick instead of rolling the growth back.
    case = _unfixed_provider_growth_case(tmp_path)
    readbacks = [
        _unfixed_tier3_route(case.candidate_members, now="node-00"),
        _unfixed_tier3_route(case.candidate_members, now="node-00"),
        _unfixed_tier3_route(case.candidate_members, now="node-01"),
    ]

    result = _run_unfixed_provider_growth(
        case,
        lambda **_kwargs: readbacks.pop(0),
    )

    assert result["updated"] is True, result
    assert case.client.update_file_snapshots == [case.candidate_bytes]
    assert case.provider_path.read_bytes() == case.candidate_bytes
    published = case.runtime._runtime_route_snapshot_payload["proxies"]["proxies"]
    assert published["TIER3-AUTO"]["now"] == "node-01"
    assert case.runtime._runtime_route_epoch == 11
    assert readbacks == []


def test_provider_rollback_also_waits_for_the_unfixed_tier3_pick(
    tmp_path,
    monkeypatch,
) -> None:
    # A pick that never returns is still drift and rolls back; the restoring
    # PUT resets member health too, so rollback verification must also judge
    # the pick Mihomo re-settles on rather than report an unknown state.
    monkeypatch.setattr(
        desktop_runtime_module,
        "_TIER3_PROVIDER_ROUTE_SETTLE_SECONDS",
        0.3,
    )
    case = _unfixed_provider_growth_case(tmp_path)
    rollback_reads: list[int] = []

    def proxies(**_kwargs):
        if len(case.client.update_file_snapshots) < 2:
            return _unfixed_tier3_route(case.candidate_members, now="node-00")
        rollback_reads.append(1)
        return _unfixed_tier3_route(
            case.old_members,
            now="node-00" if len(rollback_reads) == 1 else "node-01",
        )

    started = time.monotonic()
    result = _run_unfixed_provider_growth(case, proxies)

    assert time.monotonic() - started < 3.0
    assert result["updated"] is False
    assert result["rolled_back"] is True, result
    assert result["rollback_verified"] is True
    assert result["unknown"] is False
    assert "changed fixed/selected route state" in result["reason"]
    assert case.client.update_file_snapshots == [case.candidate_bytes, case.old_bytes]
    assert case.provider_path.read_bytes() == case.old_bytes
    assert case.runtime._runtime_loaded_export_contract is case.old_contract
    assert len(rollback_reads) >= 2


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        (
            _unfixed_tier3_route(("node-00", "node-01", "REJECT"), now="node-01", fixed="node-01"),
            _unfixed_tier3_route(("node-00", "node-01", "REJECT"), now="node-00", fixed="node-01"),
        ),
        (
            _unfixed_tier3_route(("node-00", "node-01", "REJECT"), now="node-01"),
            _unfixed_tier3_route(("node-00", "node-01", "REJECT"), now="node-00", fixed="node-00"),
        ),
        (
            _unfixed_tier3_route(("REJECT",), now="REJECT"),
            _unfixed_tier3_route(("node-00", "REJECT"), now="node-00"),
        ),
        (
            _unfixed_tier3_route(("node-00", "node-01", "REJECT"), now="node-01"),
            _unfixed_tier3_route(
                ("node-00", "node-01", "REJECT"),
                now="node-00",
                final_now="REJECT",
            ),
        ),
    ],
    ids=["fixed-pick", "newly-fixed", "reject-pick", "final-exit-changed"],
)
def test_provider_route_guard_never_waits_on_non_native_drift(previous, current) -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime.stop_event = threading.Event()

    def unexpected_readback(**_kwargs):
        pytest.fail("only an unfixed native TIER3-AUTO pick may be re-read")

    runtime.mihomo_proxies = unexpected_readback
    previous_guard = runtime._tier3_provider_route_guard({"proxies": previous})
    current_guard = runtime._tier3_provider_route_guard({"proxies": current})
    members = current["proxies"]["TIER3-AUTO"]["all"]

    assert current_guard != previous_guard
    assert runtime._settle_unfixed_tier3_provider_pick(
        previous_guard,
        current_guard,
        current,
        expected_group_members=members,
    ) == (current_guard, current)
