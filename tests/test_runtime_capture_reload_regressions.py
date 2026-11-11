from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from desktop_runtime import (
    DesktopRuntime,
    StartupRouteIntent,
    _InitialEgressAdmissionLease,
    _RuntimeProbeBudgetUnavailable,
)
from runtime_egress_recovery import (
    BoundRuntimeFailure,
    CoreLoadedGroupState,
    CoreLoadedRouteSnapshot,
    RuntimeNodeRef,
    RuntimeRouteBinding,
)
from runtime_probe_broker import RuntimeProbeBroker


def _snapshot(
    *,
    route_epoch: int,
    group: str,
    members: tuple[str, ...],
    now: str,
    fixed: str = "",
    refs: tuple[RuntimeNodeRef, ...],
) -> CoreLoadedRouteSnapshot:
    return CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=8,
        core_generation=12,
        semantic_digest="capture-graph",
        route_epoch=route_epoch,
        network_context_epoch=3,
        group_graph=(("FINAL-EXIT", (group,)), (group, members)),
        group_states=(
            CoreLoadedGroupState(
                name="FINAL-EXIT",
                type_name="Selector",
                members=(group,),
                now=group,
            ),
            CoreLoadedGroupState(
                name=group,
                type_name="Fallback",
                members=members,
                now=now,
                fixed=fixed,
                test_url="https://www.gstatic.com/generate_204",
                expected_status="204",
            ),
        ),
        proxy_nodes=refs,
    )


def _binding(
    snapshot: CoreLoadedRouteSnapshot,
    ref: RuntimeNodeRef,
    *,
    chain: tuple[str, ...],
) -> RuntimeRouteBinding:
    return RuntimeRouteBinding(
        process_pid=snapshot.process_pid,
        process_generation=snapshot.process_generation,
        core_generation=snapshot.core_generation,
        semantic_digest=snapshot.semantic_digest,
        route_epoch=snapshot.route_epoch,
        network_context_epoch=snapshot.network_context_epoch,
        observed_outbound="FINAL-EXIT",
        route_chain=chain,
        node=ref,
    )


def _native_fallback_snapshot(
    *,
    route_epoch: int,
    final_now: str,
    fallback_now: str,
    members: tuple[str, ...],
    refs: tuple[RuntimeNodeRef, ...],
) -> CoreLoadedRouteSnapshot:
    return CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=8,
        core_generation=12,
        semantic_digest="native-fallback-graph",
        route_epoch=route_epoch,
        network_context_epoch=3,
        group_graph=(
            ("FINAL-EXIT", ("PROXY-FALLBACK", "REJECT")),
            ("PROXY-FALLBACK", ("TIER3-AUTO",)),
            ("TIER3-AUTO", members),
        ),
        group_states=(
            CoreLoadedGroupState(
                name="FINAL-EXIT",
                type_name="Selector",
                members=("PROXY-FALLBACK", "REJECT"),
                now=final_now,
            ),
            CoreLoadedGroupState(
                name="PROXY-FALLBACK",
                type_name="Fallback",
                members=("TIER3-AUTO",),
                now="TIER3-AUTO",
                test_url="https://www.gstatic.com/generate_204",
                expected_status="204",
            ),
            CoreLoadedGroupState(
                name="TIER3-AUTO",
                type_name="Fallback",
                members=members,
                now=fallback_now,
                test_url="https://www.gstatic.com/generate_204",
                expected_status="204",
            ),
        ),
        proxy_nodes=refs,
    )


def _lease(*, group: str, target: str, to_proxy: str) -> dict[str, Any]:
    return {
        "incident_id": "activation-incident",
        "group": group,
        "from": "leaf-a",
        "to": to_proxy,
        "target_member": target,
        "mode": "fixed",
        "route_epoch": 10,
        "owner_token": "activation-owner",
        "minimum_dwell_until": 0.0,
        "selector_intent_revision": 7,
        "verified_at_monotonic": 0.0,
        "provisional": True,
    }


def test_startup_native_fallback_uses_full_loaded_order_and_never_pins_leaf() -> None:
    refs = tuple(
        RuntimeNodeRef("tier3", "tier3b", f"id-{index}", f"leaf-{index:02d}")
        for index in range(1, 7)
    )
    names = tuple(ref.proxy_name for ref in refs)
    before = _native_fallback_snapshot(
        route_epoch=50,
        final_now="REJECT",
        fallback_now=names[0],
        members=names,
        refs=refs,
    )
    selected = _native_fallback_snapshot(
        route_epoch=51,
        final_now="PROXY-FALLBACK",
        fallback_now=names[0],
        members=names,
        refs=refs,
    )
    healthy = _native_fallback_snapshot(
        route_epoch=52,
        final_now="PROXY-FALLBACK",
        fallback_now=names[4],
        members=names,
        refs=refs,
    )
    payload = {"proxies": {"phase": "native-fallback"}}
    runtime = object.__new__(DesktopRuntime)
    runtime.logger = logging.getLogger("test-native-startup-fallback")
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_core_inventory_snapshot = None
    runtime._runtime_loaded_export_contract = {
        "provider_semantic_digest": "provider-v7"
    }
    runtime._runtime_selector_intent_revision = 4
    runtime._tier3_core_provider_name = "TIER3B-RUNTIME"
    runtime._active_gateway_policy_enabled = lambda: False
    runtime._runtime_traffic_capture_wanted = lambda: True
    stages: list[tuple[str, dict[str, Any]]] = []
    runtime._record_tier3_initial_activation_stage = (
        lambda stage, **details: stages.append((stage, details)) or {}
    )
    writes: list[tuple[str, str]] = []
    runtime._apply_runtime_automatic_selection = (
        lambda action, **_kwargs: writes.append(
            (str(action["group"]), str(action["target"]))
        )
        or {"commit_state": "committed"}
    )
    healthchecks: list[tuple[str, str, int]] = []
    runtime.test_group_delay = (
        lambda group, *, url, timeout_ms: healthchecks.append(
            (str(group), str(url), int(timeout_ms))
        )
        or {"delay": 18}
    )
    authority = iter(
        (
            (selected, payload, _binding(
                selected,
                refs[0],
                chain=("FINAL-EXIT", "PROXY-FALLBACK", "TIER3-AUTO", names[0]),
            )),
            (healthy, payload, _binding(
                healthy,
                refs[4],
                chain=("FINAL-EXIT", "PROXY-FALLBACK", "TIER3-AUTO", names[4]),
            )),
        )
    )
    runtime._refresh_loaded_runtime_authority = lambda **_kwargs: next(authority)
    lease = _InitialEgressAdmissionLease(
        activation_generation=9,
        lifecycle_revision=1,
        runtime_generation=2,
        core_generation=12,
        process_pid=4312,
        process_generation=8,
        selector_intent_revision=4,
        worker_token=(2, 12, 4312, 8),
        deadline_monotonic=time.monotonic() + 14.0,
    )

    intent = runtime._startup_route_intent_from_snapshot(before)
    assert intent is not None
    assert intent.loaded_order == names
    assert intent.actions == (("FINAL-EXIT", "PROXY-FALLBACK", "selector"),)

    binding, snapshot, _payload, commits = (
        runtime._activate_startup_native_fallback(
            snapshot=before,
            snapshot_payload=payload,
            intent=intent,
            admission_lease=lease,
            ensure_current=lambda _stage: None,
        )
    )

    assert writes == [("FINAL-EXIT", "PROXY-FALLBACK")]
    assert all(target not in names for _group, target in writes)
    assert healthchecks and healthchecks[0][0] == "TIER3-AUTO"
    assert commits == 1
    assert binding.node == refs[4]
    assert snapshot is healthy
    assert stages[-1][0] == "native_fallback_selected"


def test_capture_reload_replays_policy_intent_when_startup_mutated_no_leaf() -> None:
    leaf_a = RuntimeNodeRef("tier3", "tier3b", "id-a", "leaf-a")
    leaf_b = RuntimeNodeRef("tier3", "tier3b", "id-b", "leaf-b")
    before = _native_fallback_snapshot(
        route_epoch=60,
        final_now="REJECT",
        fallback_now="leaf-a",
        members=("leaf-a", "leaf-b"),
        refs=(leaf_a, leaf_b),
    )
    after = _native_fallback_snapshot(
        route_epoch=61,
        final_now="PROXY-FALLBACK",
        fallback_now="leaf-b",
        members=("leaf-a", "leaf-b"),
        refs=(leaf_a, leaf_b),
    )
    intent = StartupRouteIntent(
        actions=(("FINAL-EXIT", "PROXY-FALLBACK", "selector"),),
        top_level_group="FINAL-EXIT",
        top_level_target="PROXY-FALLBACK",
        expected_gateway_key=None,
        selector_intent_revision=7,
        provider_version="provider-v8",
        loaded_order=("leaf-a", "leaf-b"),
        snapshot_key=before.snapshot_key,
    )
    payload_before = {"proxies": {"phase": "capture-reset"}}
    payload_after = {"proxies": {"phase": "capture-replayed"}}
    runtime, state = _runtime_for_capture_reload(
        initial_snapshot=before,
        initial_payload=payload_before,
        lease={},
    )
    runtime._refresh_runtime_route_snapshot = lambda **_kwargs: state["snapshot"]
    writes: list[tuple[str, str]] = []
    runtime._apply_runtime_automatic_selection = (
        lambda action, **_kwargs: writes.append(
            (str(action["group"]), str(action["target"]))
        )
        or {"commit_state": "committed"}
    )

    def authority(**_kwargs: Any):
        state["snapshot"] = after
        state["payload"] = payload_after
        return after, payload_after, _binding(
            after,
            leaf_b,
            chain=("FINAL-EXIT", "PROXY-FALLBACK", "TIER3-AUTO", "leaf-b"),
        )

    runtime._refresh_loaded_runtime_authority = authority
    prewarmed: list[tuple[RuntimeNodeRef, str, object]] = []
    runtime._probe_runtime_leaf = (
        lambda ref, _payload, **kwargs: prewarmed.append(
            (ref, str(kwargs.get("probe_url") or ""), kwargs.get("probe_priority"))
        )
        or {"delay": 22}
    )
    group_health: list[tuple[str, str, int]] = []
    runtime.test_group_delay = (
        lambda group, *, url, timeout_ms: group_health.append(
            (str(group), str(url), int(timeout_ms))
        )
        or {"delay": 22}
    )
    runtime._runtime_route_binding_for_outbound = lambda *_args, **_kwargs: _binding(
        after,
        leaf_b,
        chain=("FINAL-EXIT", "PROXY-FALLBACK", "TIER3-AUTO", "leaf-b"),
    )
    runtime._verify_runtime_final_exit = lambda **_kwargs: {"status": 204}

    result = runtime._verify_capture_route_after_reload(
        lease_record=None,
        route_intent=intent,
        preferred_leaf_ref=leaf_a,
        preferred_probe_group="TIER3-AUTO",
        timeout_seconds=5.0,
    )

    assert result == {"status": 204}
    assert writes == [("FINAL-EXIT", "PROXY-FALLBACK")]
    assert prewarmed == []
    assert len(group_health) == 1
    assert group_health[0][0:2] == (
        "TIER3-AUTO",
        "https://www.gstatic.com/generate_204",
    )
    # Capture may select another healthy leaf in the same frozen branch.
    assert state["snapshot"] is after


def test_tier3_qualification_uses_one_probe_slot_per_real_urltest() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime.stop_event = threading.Event()
    runtime._runtime_probe_broker = RuntimeProbeBroker(max_active_sockets=2)
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_internal_probe_windows = {}
    runtime._probe_urls_for_node = lambda _node: [
        "https://www.gstatic.com/generate_204"
    ]

    active = 0
    peak = 0
    entered = 0
    lock = threading.Lock()
    both_entered = threading.Event()

    class Client:
        @staticmethod
        def proxy_delay(
            name: str,
            *,
            url: str,
            timeout_ms: int,
            expected_status: object = None,
            request_timeout: float | None = None,
        ) -> dict[str, Any]:
            del url, timeout_ms, expected_status, request_timeout
            nonlocal active, peak, entered
            with lock:
                active += 1
                entered += 1
                peak = max(peak, active)
                if entered == 2:
                    both_entered.set()
            try:
                assert both_entered.wait(0.75), (
                    "the two real URLTests must reach the controller without "
                    "a nested broker self-deadlock"
                )
                return {"delay": 9, "name": name}
            finally:
                with lock:
                    active -= 1

    runtime._require_api_client = lambda: Client()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                runtime._probe_tier3_proxy_delay_candidate,
                f"leaf-{index}",
                object(),
                1000,
            )
            for index in range(2)
        ]
        results = [future.result(timeout=2.0) for future in futures]

    assert all(result[0] is True for result in results)
    assert entered == 2
    assert peak == 2
    assert runtime._runtime_probe_broker.snapshot().active_socket_slots == 0


def _runtime_for_capture_reload(
    *,
    initial_snapshot: CoreLoadedRouteSnapshot,
    initial_payload: dict[str, Any],
    lease: dict[str, Any],
) -> tuple[DesktopRuntime, dict[str, Any]]:
    runtime = object.__new__(DesktopRuntime)
    runtime._core_lifecycle_lock = threading.RLock()
    runtime._core_generation = initial_snapshot.core_generation
    runtime._runtime_generation = 0
    runtime._lifecycle_revision = 0
    runtime._tier3_egress_activation_generation = 0
    runtime._runtime_selector_intent_revision = 7
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_route_mutation_lease = dict(lease)
    state = {
        "snapshot": initial_snapshot,
        "payload": initial_payload,
        "refreshes": 0,
    }
    runtime._runtime_route_snapshot_state = lambda: (
        state["snapshot"],
        state["payload"],
    )
    return runtime, state


def test_capture_reload_replays_valid_fixed_lease_then_readbacks_and_verifies() -> None:
    group = "TIER3B-RUNTIME"
    leaf_a = RuntimeNodeRef("tier3", "tier3b", "id-a", "leaf-a")
    leaf_b = RuntimeNodeRef("tier3", "tier3b", "id-b", "leaf-b")
    before = _snapshot(
        route_epoch=20,
        group=group,
        members=("leaf-a", "leaf-b"),
        now="leaf-a",
        refs=(leaf_a, leaf_b),
    )
    after = _snapshot(
        route_epoch=21,
        group=group,
        members=("leaf-a", "leaf-b"),
        now="leaf-b",
        fixed="leaf-b",
        refs=(leaf_a, leaf_b),
    )
    payload_before = {"proxies": {"phase": "after-reload-before-replay"}}
    payload_after = {"proxies": {"phase": "after-replay-readback"}}
    lease = _lease(group=group, target="leaf-b", to_proxy="leaf-b")
    runtime, state = _runtime_for_capture_reload(
        initial_snapshot=before,
        initial_payload=payload_before,
        lease=lease,
    )
    order: list[str] = []

    def refresh(*, timeout_seconds: float) -> CoreLoadedRouteSnapshot:
        assert timeout_seconds > 0.0
        state["refreshes"] += 1
        order.append(f"refresh-{state['refreshes']}")
        if state["refreshes"] >= 2:
            state["snapshot"] = after
            state["payload"] = payload_after
        return state["snapshot"]

    runtime._refresh_runtime_route_snapshot = refresh
    runtime._prewarm_runtime_switch_target = (
        lambda snapshot, action, *, deadline_monotonic, probe_priority=None: (
            order.append("prewarm")
            or {
                "snapshot_key": snapshot.snapshot_key,
                "target": action["target"],
                "deadline": deadline_monotonic,
                "probe_priority": probe_priority,
            }
        )
    )

    def select(action: dict[str, Any], *, deadline_monotonic: float) -> dict[str, Any]:
        assert action == {
            "group": group,
            "target": "leaf-b",
            "previous": "",
            "mode": "fixed",
            "alternative_leaf": "leaf-b",
        }
        assert deadline_monotonic > 0.0
        order.append("select")
        return {"commit_state": "committed"}

    runtime._apply_runtime_automatic_selection = select

    def bind(
        snapshot: CoreLoadedRouteSnapshot,
        payload: dict[str, Any],
        outbound: str,
    ) -> RuntimeRouteBinding:
        assert snapshot is after
        assert payload is payload_after
        assert outbound == "FINAL-EXIT"
        order.append("bind")
        return _binding(after, leaf_b, chain=("FINAL-EXIT", group, "leaf-b"))

    runtime._runtime_route_binding_for_outbound = bind

    def verify(
        *,
        expected_snapshot: CoreLoadedRouteSnapshot,
        timeout_seconds: float,
        required_successes: int = 1,
        parallel_probes: bool = False,
        total_timeout_cap_seconds: float = 0.0,
        per_probe_timeout_cap_seconds: float = 0.0,
    ) -> dict[str, Any]:
        assert expected_snapshot is after
        assert timeout_seconds > 0.0
        assert required_successes == 2
        assert parallel_probes is True
        assert total_timeout_cap_seconds > 0.0
        assert per_probe_timeout_cap_seconds > 0.0
        # The full-chain 204 must not start until selection has been replayed
        # and the post-write route has been read back and rebound.
        assert order == ["refresh-1", "prewarm", "select", "refresh-2", "bind"]
        order.append("verify-204")
        return {"status": 204, "url": "https://probe.invalid/generate_204"}

    runtime._verify_runtime_final_exit = verify

    result = runtime._verify_capture_route_after_reload(
        lease_record=lease,
        timeout_seconds=3.0,
    )

    assert result["status"] == 204
    assert order == [
        "refresh-1",
        "prewarm",
        "select",
        "refresh-2",
        "bind",
        "verify-204",
    ]
    assert runtime._runtime_route_mutation_lease["route_epoch"] == 21
    assert runtime._runtime_route_mutation_lease["verified_at_monotonic"] > 0.0


def test_capture_reload_rejects_gateway_identity_change_before_final_204() -> None:
    group = "PROXY-FALLBACK"
    gateway_before = RuntimeNodeRef("exit", "gateway", "gateway-a", "Gateway A")
    gateway_after = RuntimeNodeRef("exit", "gateway", "gateway-b", "Gateway B")
    underlay_b = RuntimeNodeRef("tier3", "tier3b", "id-b", "underlay-b")
    snapshot = _snapshot(
        route_epoch=31,
        group=group,
        members=("underlay-a", "underlay-b"),
        now="underlay-b",
        fixed="underlay-b",
        refs=(gateway_after, underlay_b),
    )
    payload = {"proxies": {"phase": "gateway-changed"}}
    lease = _lease(group=group, target="underlay-b", to_proxy="underlay-b")
    runtime, _state = _runtime_for_capture_reload(
        initial_snapshot=snapshot,
        initial_payload=payload,
        lease=lease,
    )
    runtime._refresh_runtime_route_snapshot = lambda **_kwargs: snapshot
    runtime._prewarm_runtime_switch_target = lambda *_args, **_kwargs: pytest.fail(
        "already-selected underlay must not be replayed"
    )
    runtime._apply_runtime_automatic_selection = lambda *_args, **_kwargs: pytest.fail(
        "already-selected underlay must not be rewritten"
    )
    runtime._runtime_route_binding_for_outbound = (
        lambda *_args, **_kwargs: _binding(
            snapshot,
            gateway_after,
            chain=("FINAL-EXIT", "Gateway B"),
        )
    )
    runtime._verify_runtime_final_exit = lambda **_kwargs: pytest.fail(
        "Gateway identity must be checked before FINAL-EXIT 204"
    )

    with pytest.raises(RuntimeError, match="Gateway identity changed"):
        runtime._verify_capture_route_after_reload(
            lease_record=lease,
            expected_gateway_key=gateway_before.key,
            timeout_seconds=3.0,
        )


def test_capture_reload_missing_lease_target_cannot_reach_capture_publication() -> None:
    group = "TIER3B-RUNTIME"
    leaf_a = RuntimeNodeRef("tier3", "tier3b", "id-a", "leaf-a")
    leaf_b = RuntimeNodeRef("tier3", "tier3b", "id-b", "leaf-b")
    snapshot = _snapshot(
        route_epoch=40,
        group=group,
        members=("leaf-a",),
        now="leaf-a",
        refs=(leaf_a, leaf_b),
    )
    payload = {"proxies": {"phase": "lease-target-removed"}}
    lease = _lease(group=group, target="leaf-b", to_proxy="leaf-b")
    runtime, _state = _runtime_for_capture_reload(
        initial_snapshot=snapshot,
        initial_payload=payload,
        lease=lease,
    )
    events: list[str] = []
    runtime._refresh_runtime_route_snapshot = lambda **_kwargs: snapshot
    runtime._prewarm_runtime_switch_target = (
        lambda *_args, **_kwargs: events.append("prewarm") or {"delay": 8}
    )
    runtime._apply_runtime_automatic_selection = (
        lambda *_args, **_kwargs: events.append("select")
        or {"commit_state": "unknown"}
    )
    runtime._runtime_route_binding_for_outbound = (
        lambda *_args, **_kwargs: events.append("bind")
        or _binding(snapshot, leaf_a, chain=("FINAL-EXIT", group, "leaf-a"))
    )
    runtime._verify_runtime_final_exit = lambda **_kwargs: events.append(
        "verify-204"
    )
    runtime._apply_system_proxy_capture_after_core_start = (
        lambda *_args, **_kwargs: events.append("publish-capture")
    )

    with pytest.raises(RuntimeError, match="target was not selected"):
        runtime._verify_capture_route_after_reload(
            lease_record=lease,
            timeout_seconds=3.0,
        )

    assert events == ["prewarm", "select"]
    assert "verify-204" not in events
    assert "publish-capture" not in events


class _SnapshotCoordinator:
    def __init__(self, snapshot: CoreLoadedRouteSnapshot | None = None) -> None:
        self.snapshot = snapshot
        self.mutation = object()
        self.ended = threading.Event()

    def current_route_snapshot(self) -> CoreLoadedRouteSnapshot | None:
        return self.snapshot

    def set_current_snapshot(
        self,
        snapshot: CoreLoadedRouteSnapshot | None,
    ) -> None:
        self.snapshot = snapshot

    def begin_core_mutation(self, *_args, **_kwargs) -> object:
        return self.mutation

    def core_mutation_lease_is_current(self, lease: object) -> bool:
        return lease is self.mutation

    def end_core_mutation(self, lease: object) -> bool:
        assert lease is self.mutation
        self.ended.set()
        return True


class _ImmediateStopEvent:
    @staticmethod
    def wait(_seconds: float) -> bool:
        return False

    @staticmethod
    def is_set() -> bool:
        return False


def _wait_for_runtime_workers(
    runtime: DesktopRuntime,
    *,
    timeout_seconds: float = 1.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with runtime._runtime_failure_monitor_lock:
            threads = list(runtime._runtime_failure_recovery_threads)
            bindings = set(runtime._runtime_failure_recovery_bindings)
        for thread in threads:
            thread.join(timeout=0.01)
        if not bindings and not any(thread.is_alive() for thread in threads):
            return
        time.sleep(0.005)
    raise AssertionError(
        "runtime worker did not release its thread and admission binding"
    )


def test_network_hold_release_reconciles_provisional_without_origin_binding() -> None:
    """A network epoch must not strand a verified A->B provisional lease.

    The epoch transition intentionally revokes the old origin binding.  Once
    the new network context has a verified route, release must either enqueue
    fresh current-route reconciliation or retire provisional ownership.  It
    cannot leave a lease that no future origin probe is capable of completing.
    """

    group = "TIER3B-RUNTIME"
    leaf_b = RuntimeNodeRef("tier3", "tier3b", "id-b", "leaf-b")
    old_snapshot = _snapshot(
        route_epoch=50,
        group=group,
        members=("leaf-a", "leaf-b"),
        now="leaf-b",
        fixed="leaf-b",
        refs=(leaf_b,),
    )
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_hot_standby_lock = threading.RLock()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_network_context_epoch = 4
    runtime._runtime_route_epoch = old_snapshot.route_epoch
    runtime._runtime_route_snapshot = old_snapshot
    runtime._runtime_route_snapshot_payload = {"proxies": {}}
    runtime._runtime_route_selection_signature = ("leaf-b",)
    runtime._runtime_route_attribution_barrier_epoch = 0
    runtime._runtime_route_attribution_barrier_until = 0.0
    runtime._runtime_hot_standby_evidence = {leaf_b.key: object()}
    runtime._runtime_hot_standby_last_probe_at = 1.0
    runtime._runtime_network_context_hold_active = False
    runtime._runtime_network_context_hold_pending_candidate = False
    runtime._runtime_network_context_hold_reason = ""
    runtime._runtime_network_context_hold_epoch = 4
    runtime._runtime_network_context_rebase_stage = 0
    runtime._runtime_network_context_rebase_at = 0.0
    runtime._runtime_network_context_invalid_observed = False
    runtime._runtime_facility_epoch = 9
    runtime._runtime_origin_diagnostic_bindings = {
        "incident-a": SimpleNamespace(binding="origin-a")
    }
    runtime._runtime_route_mutation_lease = {
        **_lease(group=group, target="leaf-b", to_proxy="leaf-b"),
        "incident_id": "incident-a",
        "provisional": True,
    }
    runtime._runtime_deferred_failbacks = {}
    coordinator = _SnapshotCoordinator(old_snapshot)
    runtime._runtime_egress_coordinator = coordinator
    runtime.logger = SimpleNamespace(info=lambda *_args, **_kwargs: None)
    runtime._enter_runtime_facility_hold = lambda _reason: setattr(
        runtime,
        "_runtime_facility_epoch",
        runtime._runtime_facility_epoch + 1,
    )
    runtime._queue_runtime_route_snapshot_refresh = lambda: None
    runtime._wake_parked_tier3_pool_sync_if_causal_key_changed = lambda: None
    runtime._set_effective_egress_readiness_state = lambda *_args: None
    reconcile: list[tuple[str, str]] = []
    runtime._submit_current_route_probe_failure = (
        lambda reason, *, source: reconcile.append((reason, source)) or True
    )

    new_epoch = runtime._advance_runtime_network_context_epoch("Wi-Fi changed")

    assert new_epoch == 5
    assert runtime._runtime_origin_diagnostic_bindings == {}
    current_snapshot = _snapshot(
        route_epoch=runtime._runtime_route_epoch,
        group=group,
        members=("leaf-a", "leaf-b"),
        now="leaf-b",
        fixed="leaf-b",
        refs=(leaf_b,),
    )
    current_snapshot = CoreLoadedRouteSnapshot(
        process_pid=current_snapshot.process_pid,
        process_generation=current_snapshot.process_generation,
        core_generation=current_snapshot.core_generation,
        semantic_digest=current_snapshot.semantic_digest,
        route_epoch=current_snapshot.route_epoch,
        network_context_epoch=new_epoch,
        group_graph=current_snapshot.group_graph,
        group_states=current_snapshot.group_states,
        proxy_nodes=current_snapshot.proxy_nodes,
    )
    coordinator.set_current_snapshot(current_snapshot)

    assert runtime._release_runtime_network_context_hold_after_verified_probe()
    current_lease = runtime._runtime_route_mutation_lease
    reconciled_or_retired = bool(
        reconcile
        or not isinstance(current_lease, dict)
        or not bool(current_lease.get("provisional"))
    )
    assert reconciled_or_retired, (
        "network hold release left a provisional lease whose origin binding "
        "was revoked and queued no fresh reconciliation"
    )


def test_failback_facility_epoch_change_after_selection_is_deferred_without_ready() -> None:
    """A late facility transition must revoke failback publication.

    The exact leaf probe and selector release can both complete before the
    facility epoch changes.  Even if the final verifier returns, the caller
    must recheck its execution token before clearing the incident lease or
    publishing effective-egress ready, then park the PASS for a fresh worker.
    """

    group = "TIER3B-RUNTIME"
    recovered = RuntimeNodeRef("tier3", "tier3b", "id-a", "leaf-a")
    diverted = RuntimeNodeRef("tier3", "tier3b", "id-b", "leaf-b")
    snapshot = _snapshot(
        route_epoch=61,
        group=group,
        members=("leaf-a", "leaf-b"),
        now="leaf-b",
        fixed="leaf-b",
        refs=(recovered, diverted),
    )
    payload = {"proxies": {"phase": "failback"}}
    origin = _binding(
        snapshot,
        recovered,
        chain=("FINAL-EXIT", group, "leaf-a"),
    )
    runtime = object.__new__(DesktopRuntime)
    runtime.stop_event = threading.Event()
    runtime.running = True
    runtime.core_runner = SimpleNamespace(is_running=lambda: True)
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._core_transition_lock = threading.RLock()
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_failure_recovery_bindings = set()
    runtime._runtime_failure_recovery_threads = set()
    runtime._runtime_selector_intent_revision = 7
    runtime._runtime_facility_epoch = 3
    runtime._runtime_network_context_epoch = snapshot.network_context_epoch
    runtime._runtime_route_epoch = snapshot.route_epoch
    runtime._runtime_route_mutation_lease = {
        **_lease(group=group, target="leaf-b", to_proxy="leaf-b"),
        "incident_id": "incident-failback",
        "verified_at_monotonic": time.monotonic() - 120.0,
        "provisional": False,
    }
    runtime._runtime_incident_bindings = {"incident-failback": origin}
    runtime._runtime_incident_route_payloads = {
        "incident-failback": payload
    }
    runtime._runtime_incident_recovery_state = {
        "incident-failback": "recovered"
    }
    runtime._runtime_origin_diagnostic_bindings = {}
    runtime._runtime_deferred_failbacks = {}
    runtime._runtime_failback_last_attempt = {recovered.key: time.monotonic() - 20.0}
    runtime._runtime_failback_pass_history = {
        recovered.key: [time.monotonic() - 10.0]
    }
    runtime._runtime_flap_history = {}
    runtime._runtime_failback_failures = {}
    runtime._runtime_failback_export_holds = set()
    runtime._runtime_last_verified_route = {}
    runtime._runtime_reload_gate = None
    coordinator = _SnapshotCoordinator(snapshot)
    runtime._runtime_egress_coordinator = coordinator
    initial_token = (runtime._runtime_facility_epoch,)
    runtime._runtime_worker_token = lambda: initial_token
    runtime._runtime_worker_token_is_current = (
        lambda token: token == (runtime._runtime_facility_epoch,)
    )
    runtime._current_core_runtime_phase = lambda: "formal_capture"
    runtime._revalidation_capture_active = lambda _phase: True
    runtime._runtime_facility_hold_reason = lambda: (
        "network context changed"
        if runtime._runtime_facility_epoch != initial_token[0]
        else ""
    )
    runtime._runtime_route_snapshot_state = lambda: (snapshot, payload)
    runtime._refresh_runtime_route_snapshot = lambda **_kwargs: snapshot
    promoted = {"value": False}

    def binding_for_outbound(
        _snapshot_value: CoreLoadedRouteSnapshot,
        _payload_value: dict[str, Any],
        _outbound: str,
        **_kwargs,
    ) -> RuntimeRouteBinding:
        ref = recovered if promoted["value"] else diverted
        return _binding(
            snapshot,
            ref,
            chain=("FINAL-EXIT", group, ref.proxy_name),
        )

    runtime._runtime_route_binding_for_outbound = binding_for_outbound
    runtime._runtime_alternative_switch_action = lambda *_args, **_kwargs: {
        "group": group,
        "target": "leaf-a",
        "previous": "leaf-b",
        "mode": "fixed",
        "alternative_leaf": "leaf-a",
    }
    events: list[str] = []
    runtime._prewarm_runtime_switch_target = (
        lambda *_args, **_kwargs: events.append("exact-probe") or {"delay": 7}
    )

    def release_fixed(*_args, **_kwargs) -> dict[str, Any]:
        events.append("selection")
        promoted["value"] = True
        return {"commit_state": "committed"}

    runtime._release_runtime_automatic_fixed_selection = release_fixed
    runtime._runtime_natural_winner_is_acceptable = lambda *_args: True

    def verify_final(**_kwargs) -> dict[str, Any]:
        assert events == ["exact-probe", "selection"]
        events.append("verify-204")
        # The transition occurs after exact evidence and route selection.  A
        # stale callback that happens to return 204 still has no publication
        # authority in the new facility epoch.
        runtime._runtime_facility_epoch += 1
        return {"status": 204}

    runtime._verify_runtime_final_exit = verify_final
    ready: list[tuple[object, str]] = []
    runtime._set_effective_egress_state = (
        lambda value, reason: ready.append((value, reason))
    )
    runtime._set_effective_egress_readiness_state = (
        lambda value, reason: ready.append((value, reason))
    )
    runtime._runtime_leaf_export_hold_key = lambda ref: ref.key
    runtime._queue_runtime_route_snapshot_refresh = lambda: None
    runtime._enforce_runtime_fail_closed = lambda *_args, **_kwargs: pytest.fail(
        "facility CAS loss must defer rather than fail-close"
    )
    runtime._start_runtime_core_reconcile_for_failback = (
        lambda *_args, **_kwargs: pytest.fail("Core reconcile is not due")
    )

    runtime._start_runtime_failback(
        {"incident_id": "incident-failback"},
        recovered,
    )

    assert coordinator.ended.wait(1.0), "failback worker did not finish mutation"
    deadline = time.monotonic() + 1.0
    key = ("failback", *recovered.key)
    while key in runtime._runtime_failure_recovery_bindings and time.monotonic() < deadline:
        time.sleep(0.005)

    assert events == ["exact-probe", "selection", "verify-204"]
    assert runtime._runtime_route_mutation_lease is not None
    assert "incident-failback" in runtime._runtime_deferred_failbacks
    assert ready == []


def _fail_closed_failback_runtime(
    *,
    lease_incident_id: str,
    lease_verified: bool,
) -> tuple[DesktopRuntime, RuntimeNodeRef, list[dict[str, Any]], list[dict[str, Any]]]:
    incident_id = "incident-pass"
    group = "TIER3B-RUNTIME"
    recovered = RuntimeNodeRef("tier3", "tier3b", "id-a", "leaf-a")
    snapshot = _snapshot(
        route_epoch=72,
        group=group,
        members=("leaf-a",),
        now="leaf-a",
        refs=(recovered,),
    )
    origin = _binding(
        snapshot,
        recovered,
        chain=("FINAL-EXIT", group, "leaf-a"),
    )

    def route_payload(final_target: str) -> dict[str, Any]:
        return {
            "proxies": {
                "proxies": {
                    "FINAL-EXIT": {
                        "type": "Selector",
                        "now": final_target,
                        "all": [group, "REJECT"],
                    },
                    group: {
                        "type": "Fallback",
                        "now": "leaf-a",
                        "all": ["leaf-a"],
                    },
                    "leaf-a": {"type": "Shadowsocks"},
                    "REJECT": {"type": "Reject"},
                }
            }
        }

    original_payload = route_payload(group)
    state = {"payload": route_payload("REJECT")}
    runtime = object.__new__(DesktopRuntime)
    runtime.logger = SimpleNamespace(
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
    )
    runtime.stop_event = _ImmediateStopEvent()
    runtime.running = True
    runtime.core_runner = SimpleNamespace(is_running=lambda: True)
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._core_transition_lock = threading.RLock()
    runtime._runtime_failure_recovery_bindings = set()
    runtime._runtime_failure_recovery_threads = set()
    runtime._runtime_selector_intent_revision = 11
    runtime._runtime_facility_epoch = 4
    runtime._runtime_route_epoch = snapshot.route_epoch
    runtime._runtime_confirmed_fail_closed_snapshot_key = snapshot.snapshot_key
    runtime._runtime_worker_token = lambda: (4,)
    runtime._runtime_worker_token_is_current = (
        lambda token: tuple(token) == (runtime._runtime_facility_epoch,)
    )
    runtime._runtime_facility_hold_reason = lambda: ""
    runtime._current_core_runtime_phase = lambda: "formal_capture"
    runtime._revalidation_capture_active = lambda _phase: True
    runtime._runtime_incident_bindings = {incident_id: origin}
    runtime._runtime_incident_route_payloads = {
        incident_id: original_payload,
    }
    runtime._runtime_incident_recovery_state = {incident_id: "ready"}
    runtime._runtime_origin_diagnostic_bindings = {incident_id: object()}
    runtime._runtime_deferred_failbacks = {}
    runtime._runtime_failback_last_attempt = {
        recovered.key: time.monotonic() - 20.0,
    }
    runtime._runtime_failback_pass_history = {
        recovered.key: [time.monotonic() - 6.0],
    }
    runtime._runtime_flap_history = {}
    runtime._runtime_failback_failures = {}
    runtime._runtime_failback_export_holds = {
        runtime._runtime_leaf_export_hold_key(recovered)
    }
    runtime._runtime_last_verified_route = {}
    runtime._runtime_reload_gate = None
    runtime._runtime_route_mutation_lease = {
        "group": "FINAL-EXIT",
        "from": "leaf-a",
        "to": "REJECT",
        "target_member": "REJECT",
        "mode": "selector",
        "incident_id": lease_incident_id,
        "route_epoch": snapshot.route_epoch,
        "selector_intent_revision": 11,
        "verified_at_monotonic": (
            time.monotonic() - 31.0 if lease_verified else 0.0
        ),
        "provisional": False,
    }
    coordinator = _SnapshotCoordinator(snapshot)
    runtime._runtime_egress_coordinator = coordinator
    runtime._runtime_route_snapshot_state = lambda: (
        snapshot,
        state["payload"],
    )
    runtime._refresh_runtime_route_snapshot = lambda **_kwargs: snapshot
    runtime._probe_runtime_leaf = lambda *_args, **_kwargs: {"delay": 7}
    selections: list[dict[str, Any]] = []

    def select(action: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        selections.append(dict(action))
        state["payload"] = route_payload(group)
        return {"commit_state": "committed"}

    runtime._apply_runtime_automatic_selection = select
    verifications: list[dict[str, Any]] = []
    runtime._verify_runtime_final_exit = (
        lambda **_kwargs: verifications.append(dict(_kwargs))
        or {"status": 204}
    )
    runtime._set_effective_egress_state = lambda *_args, **_kwargs: None
    runtime._set_effective_egress_readiness_state = (
        lambda *_args, **_kwargs: None
    )
    runtime._queue_runtime_route_snapshot_refresh = lambda: None
    runtime._start_runtime_core_reconcile_for_failback = (
        lambda *_args, **_kwargs: pytest.fail("unexpected Core reconcile")
    )
    runtime._enforce_runtime_fail_closed = lambda *_args, **_kwargs: pytest.fail(
        "an incident-owned verified REJECT must be recoverable"
    )
    return runtime, recovered, selections, verifications


def test_authoritative_pass_restores_incident_owned_verified_reject() -> None:
    runtime, recovered, selections, verifications = (
        _fail_closed_failback_runtime(
            lease_incident_id="incident-pass",
            lease_verified=True,
        )
    )

    DesktopRuntime._start_runtime_failback(
        runtime,
        {"incident_id": "incident-pass"},
        recovered,
    )
    _wait_for_runtime_workers(runtime)

    assert selections == [
        {
            "group": "FINAL-EXIT",
            "target": "TIER3B-RUNTIME",
            "previous": "REJECT",
            "mode": "selector",
            "alternative_leaf": "leaf-a",
        }
    ]
    assert len(verifications) == 1
    assert runtime._runtime_route_mutation_lease is None
    assert runtime._runtime_incident_recovery_state["incident-pass"] == "promoted"
    assert runtime._runtime_last_verified_route["leaf_proxy"] == "leaf-a"


@pytest.mark.parametrize(
    ("lease_incident_id", "lease_verified"),
    [
        pytest.param("incident-pass", False, id="unverified-reject"),
        pytest.param("different-incident", True, id="foreign-incident-reject"),
    ],
)
def test_unverified_or_foreign_reject_is_not_restored_by_pass(
    lease_incident_id: str,
    lease_verified: bool,
) -> None:
    runtime, recovered, selections, verifications = (
        _fail_closed_failback_runtime(
            lease_incident_id=lease_incident_id,
            lease_verified=lease_verified,
        )
    )
    retries: list[RuntimeNodeRef] = []
    runtime._start_runtime_failback = (
        lambda _payload, ref: retries.append(ref)
    )

    DesktopRuntime._start_runtime_failback(
        runtime,
        {"incident_id": "incident-pass"},
        recovered,
    )
    _wait_for_runtime_workers(runtime)

    assert selections == []
    assert verifications == []
    assert runtime._runtime_route_mutation_lease is not None
    assert runtime._runtime_route_mutation_lease["incident_id"] == lease_incident_id
    assert runtime._runtime_incident_recovery_state["incident-pass"] == "ready"


class _RecoveryCoordinator(_SnapshotCoordinator):
    def __init__(self, snapshot: CoreLoadedRouteSnapshot) -> None:
        super().__init__(snapshot)
        self.confirmed = SimpleNamespace(
            confirmed_monotonic=time.monotonic(),
        )
        self.recovering = threading.Event()

    def current_snapshot_for(self, _binding: RuntimeRouteBinding) -> object:
        return self.confirmed

    def mark_recovering(self, *_args: Any, **_kwargs: Any) -> None:
        self.recovering.set()

    def mark_quarantined(self, *_args: Any, **_kwargs: Any) -> None:
        pytest.fail("a facility-superseded result must not publish quarantine")


def test_recovery_facility_epoch_change_after_204_cannot_publish_verified_route() -> None:
    group = "TIER3B-RUNTIME"
    failed = RuntimeNodeRef("tier3", "tier3b", "id-a", "leaf-a")
    alternative = RuntimeNodeRef("tier3", "tier3b", "id-b", "leaf-b")
    snapshot = _snapshot(
        route_epoch=81,
        group=group,
        members=("leaf-a", "leaf-b"),
        now="leaf-a",
        refs=(failed, alternative),
    )
    binding = _binding(
        snapshot,
        failed,
        chain=("FINAL-EXIT", group, "leaf-a"),
    )
    failure = BoundRuntimeFailure(
        binding=binding,
        protocol="TCP",
        observed_outbound="FINAL-EXIT",
        destination_token="destination",
        error_type="connection_refused",
        observed_at_monotonic=time.monotonic(),
    )
    runtime = object.__new__(DesktopRuntime)
    runtime.stop_event = threading.Event()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._core_transition_lock = threading.RLock()
    runtime._runtime_failure_recovery_bindings = set()
    runtime._runtime_failure_recovery_threads = set()
    runtime._runtime_pending_confirmed_incidents = {}
    runtime._runtime_pending_recovered_payloads = {}
    runtime._runtime_incident_recovery_state = {}
    runtime._runtime_route_mutation_lease = None
    runtime._runtime_last_verified_route = {}
    runtime._runtime_selector_intent_revision = 5
    runtime._runtime_facility_epoch = 9
    runtime._runtime_route_epoch = snapshot.route_epoch
    runtime._runtime_reload_gate = None
    runtime._runtime_worker_token = lambda: (runtime._runtime_facility_epoch,)
    runtime._runtime_worker_token_is_current = (
        lambda token: tuple(token) == (runtime._runtime_facility_epoch,)
    )
    runtime._runtime_facility_hold_reason = lambda: ""
    coordinator = _RecoveryCoordinator(snapshot)
    runtime._runtime_egress_coordinator = coordinator
    runtime._apply_confirmed_runtime_health = (
        lambda *_args, **_kwargs: {"accepted": True, "changed": True}
    )
    runtime._activate_killswitch_for_egress_failure = lambda *_args: None

    def verified_alternative(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        # This models the exact late window: selector application and the
        # replacement's neutral 204 have succeeded, but the incident lease has
        # not yet been published under the Core lock.
        runtime._runtime_facility_epoch += 1
        return {
            "fast_path_available": True,
            "post_switch_connectivity_verified": True,
            "effective_egress_ok": True,
            "alternative_leaf": "leaf-b",
            "leaf_proxy": "leaf-b",
            "selection_action": {
                "group": group,
                "target": "leaf-b",
                "mode": "fixed",
            },
            "selected_chain": ["FINAL-EXIT", group, "leaf-b"],
            "post_switch_result": {"status": 204},
            "loaded_route_epoch": snapshot.route_epoch,
        }

    runtime._fast_recover_confirmed_alternative = verified_alternative
    runtime._enforce_runtime_fail_closed = lambda *_args, **_kwargs: pytest.fail(
        "facility CAS loss must stay indeterminate, not manufacture REJECT"
    )
    readiness: list[tuple[object, str]] = []
    runtime._set_effective_egress_readiness_state = (
        lambda value, reason: readiness.append((value, str(reason)))
    )
    runtime._set_effective_egress_state = lambda *_args, **_kwargs: pytest.fail(
        "a stale 204 cannot publish effective-egress ready"
    )
    refreshes: list[str] = []
    runtime._queue_runtime_route_snapshot_refresh = (
        lambda: refreshes.append("snapshot")
    )
    reconciles: list[tuple[str, str]] = []
    runtime._submit_current_route_probe_failure = (
        lambda reason, *, source: reconciles.append((str(reason), str(source)))
        or True
    )
    runtime._start_runtime_connection_cleanup = lambda *_args: pytest.fail(
        "a stale 204 cannot advance to connection cleanup"
    )
    runtime._mark_runtime_incident_recovery_ready = lambda *_args: pytest.fail(
        "a stale 204 cannot mark the incident ready"
    )

    runtime._start_runtime_recovery(
        failure,
        snapshot,
        {"proxies": {}},
        incident_id="incident-facility-race",
        quarantine_token="quarantine-token",
        confirmed_alternative=alternative,
    )
    assert coordinator.ended.wait(1.0), "recovery worker did not end mutation"
    _wait_for_runtime_workers(runtime)

    assert runtime._runtime_route_mutation_lease is None
    assert runtime._runtime_last_verified_route == {}
    assert (
        runtime._runtime_incident_recovery_state["incident-facility-race"]
        == "indeterminate"
    )
    assert readiness and readiness[-1][0] is None
    assert refreshes
    assert reconciles == [
        (
            "runtime recovery ended with indeterminate route ownership",
            "recovery_indeterminate_reconcile",
        )
    ]


def test_alternative_probe_budget_saturation_is_not_all_candidates_failed() -> None:
    runtime = object.__new__(DesktopRuntime)
    failed = RuntimeNodeRef("tier1", "tier1_variant", "id-a", "leaf-a")
    alternative = RuntimeNodeRef("tier1", "tier1_variant", "id-b", "leaf-b")
    snapshot = _snapshot(
        route_epoch=91,
        group="TIER1-AUTO",
        members=("leaf-a", "leaf-b"),
        now="leaf-a",
        refs=(failed, alternative),
    )
    failure = BoundRuntimeFailure(
        binding=_binding(
            snapshot,
            failed,
            chain=("FINAL-EXIT", "TIER1-AUTO", "leaf-a"),
        ),
        protocol="TCP",
        observed_outbound="FINAL-EXIT",
        destination_token="destination",
        error_type="timeout",
        observed_at_monotonic=time.monotonic(),
    )
    runtime._runtime_ordered_alternative_refs = (
        lambda *_args, **_kwargs: [alternative]
    )
    runtime._probe_runtime_leaf = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        _RuntimeProbeBudgetUnavailable("probe slots busy")
    )

    ref, result = runtime._probe_managed_alternative(
        failure,
        snapshot,
        {"proxies": {}},
        max_parallel=1,
    )

    assert ref is None
    assert result == {"probe_indeterminate": True}


def test_facility_confirmation_budget_saturation_does_not_open_hold() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_facility_confirmation_tokens = set()
    runtime._runtime_failure_recovery_threads = set()
    runtime._runtime_failure_recovery_bindings = set()
    runtime._runtime_worker_token = lambda: (1, 2, 3)
    runtime._runtime_worker_token_is_current = lambda _token: True
    runtime._probe_mihomo_mixed_port_once = (
        lambda **_kwargs: (_ for _ in ()).throw(
            _RuntimeProbeBudgetUnavailable("probe slots busy")
        )
    )
    runtime._record_runtime_facility_degradation = lambda *_args: pytest.fail(
        "budget saturation cannot become facility evidence"
    )

    runtime._start_runtime_facility_confirmation("unbound stdout signal")
    _wait_for_runtime_workers(runtime)

    assert runtime._runtime_facility_confirmation_tokens == set()
    assert runtime._runtime_failure_recovery_threads == set()


def test_internal_probe_only_bytes_do_not_mark_business_traffic_active() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_hot_standby_lock = threading.RLock()
    runtime._runtime_internal_probe_windows = {"probe": (99.0, 101.0)}
    runtime._runtime_hot_standby_last_traffic_totals = (0, 0)
    runtime._runtime_hot_standby_last_active_at = 0.0
    runtime._runtime_hot_standby_pending_unknown_activity = False
    runtime._runtime_hot_standby_traffic_totals = lambda: (64, 128)

    assert runtime._runtime_traffic_active_for_hot_standby(100.0) is False
    assert runtime._runtime_hot_standby_pending_unknown_activity is True
    assert runtime._runtime_traffic_active_for_hot_standby(102.0) is False
    assert runtime._runtime_hot_standby_pending_unknown_activity is False
    assert runtime._runtime_hot_standby_last_active_at == 0.0


def test_stopped_core_fail_closed_without_gateway_ticket_is_ignored() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_selector_intent_revision = 4
    runtime._runtime_route_epoch = 12
    runtime._runtime_route_mutation_lease = {
        "incident_id": "stale",
        "to": "REJECT",
    }
    runtime._runtime_core_stopped_recovery_owners = {}
    runtime._runtime_incident_recovery_state = {}

    disposition = runtime._commit_runtime_fail_closed_outcome(
        physical={
            "enforced": True,
            "core_stopped": True,
            "route_verified": False,
        },
        incident_id="incident-stop",
        from_proxy="leaf-a",
        selector_intent_revision=4,
    )

    assert disposition == "indeterminate"
    assert runtime._runtime_route_mutation_lease == {
        "incident_id": "stale",
        "to": "REJECT",
    }
    assert runtime._runtime_core_stopped_recovery_owners == {}
    assert runtime._runtime_incident_recovery_state == {}


def test_killswitch_activation_alone_does_not_publish_route_offline() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime.kill_switch = SimpleNamespace(
        active=False,
        activate=lambda: True,
    )
    runtime._set_effective_egress_state = lambda *_args: pytest.fail(
        "a leak guard is not REJECT/Core-stop route evidence"
    )

    assert runtime._activate_killswitch_for_egress_failure("confirmed leaf")


def test_verified_incident_reject_releases_attribution_without_external_204() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_hot_standby_lock = threading.RLock()
    runtime._runtime_local_facility_fault_reason = ""
    runtime._runtime_network_context_epoch = 3
    runtime._runtime_network_context_hold_active = True
    runtime._runtime_network_context_hold_pending_candidate = False
    runtime._runtime_network_context_hold_epoch = 3
    runtime._runtime_network_context_hold_reason = "network changed"
    runtime._runtime_network_context_invalid_observed = False
    runtime._runtime_network_context_rebase_stage = 2
    runtime._runtime_network_context_rebase_at = float("inf")
    runtime._runtime_facility_epoch = 9
    runtime._runtime_selector_intent_revision = 7
    runtime._runtime_route_mutation_lease = {
        "incident_id": "incident-reject",
        "from": "leaf-a",
        "to": "REJECT",
        "selector_intent_revision": 7,
        "verified_at_monotonic": time.monotonic(),
    }
    snapshot = CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=8,
        core_generation=12,
        semantic_digest="capture-graph",
        route_epoch=44,
        network_context_epoch=3,
        group_graph=(("FINAL-EXIT", ("REJECT",)),),
        group_states=(
            CoreLoadedGroupState(
                name="FINAL-EXIT",
                type_name="Selector",
                members=("REJECT",),
                now="REJECT",
            ),
        ),
    )
    payload = {
        "proxies": {
            "proxies": {
                "FINAL-EXIT": {
                    "type": "Selector",
                    "now": "REJECT",
                    "all": ["REJECT"],
                },
                "REJECT": {"type": "Reject"},
            }
        }
    }
    runtime._runtime_route_snapshot = snapshot
    runtime._runtime_route_snapshot_payload = payload
    runtime._runtime_confirmed_fail_closed_snapshot_key = ("previous",)
    runtime._runtime_egress_coordinator = SimpleNamespace(
        current_route_snapshot=lambda: snapshot
    )
    runtime._refresh_runtime_route_snapshot = lambda **_kwargs: snapshot
    runtime._runtime_route_snapshot_state = lambda: (snapshot, payload)
    publications: list[tuple[object, str]] = []
    runtime._set_effective_egress_readiness_state = (
        lambda value, reason: publications.append((value, str(reason)))
    )
    runtime._wake_parked_tier3_pool_sync_if_causal_key_changed = lambda: None
    runtime._resume_runtime_deferred_after_facility_release = lambda: None

    rebound = runtime._runtime_incident_owned_reject_rebaseline()
    assert rebound is snapshot
    assert runtime._release_runtime_network_context_hold_after_verified_probe(
        readiness=False,
        readiness_reason="safe REJECT is still active",
        preserve_fail_closed_snapshot_key=rebound.snapshot_key,
    )

    assert runtime._runtime_network_context_hold_active is False
    assert runtime._runtime_confirmed_fail_closed_snapshot_key == snapshot.snapshot_key
    assert publications == [(False, "safe REJECT is still active")]


def test_old_network_baseline_cannot_release_new_facility_hold_same_network_epoch() -> None:
    ref = RuntimeNodeRef("tier2", "tier2", "leaf-id", "leaf-a")
    snapshot = _snapshot(
        route_epoch=45,
        group="TIER2-AUTO",
        members=("leaf-a",),
        now="leaf-a",
        refs=(ref,),
    )
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_hot_standby_lock = threading.RLock()
    runtime._runtime_local_facility_fault_reason = ""
    runtime._runtime_network_context_epoch = 3
    runtime._runtime_network_context_hold_active = True
    runtime._runtime_network_context_hold_pending_candidate = False
    runtime._runtime_network_context_hold_epoch = 3
    runtime._runtime_network_context_hold_reason = "new facility interval"
    runtime._runtime_network_context_invalid_observed = False
    runtime._runtime_network_context_rebase_stage = 0
    runtime._runtime_network_context_rebase_at = 0.0
    # The old probe started at facility epoch 9. A new hold opened without a
    # network epoch change and advanced the facility epoch to 10.
    runtime._runtime_facility_epoch = 10
    runtime._runtime_egress_coordinator = SimpleNamespace(
        current_route_snapshot=lambda: snapshot
    )
    publications: list[tuple[object, str]] = []
    runtime._set_effective_egress_readiness_state = (
        lambda value, reason: publications.append((value, str(reason)))
    )

    assert not runtime._release_runtime_network_context_hold_after_verified_probe(
        expected_hold_epoch=3,
        expected_facility_epoch=9,
    )
    assert runtime._runtime_network_context_hold_active is True
    assert runtime._runtime_network_context_hold_reason == "new facility interval"
    assert publications == []


def test_route_context_cache_is_revoked_by_runtime_route_epoch() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_generation = 2
    runtime._core_generation = 5
    runtime._runtime_route_epoch = 31
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._exit_quality_route_lock = threading.RLock()
    runtime._exit_quality_route_cache = {}
    runtime._exit_quality_route_cache_at = 0.0
    runtime._exit_quality_route_cache_route_epoch = -1
    runtime._runtime_confirmed_fail_closed_snapshot_key = None
    runtime.network_mode = "system"
    runtime.core_runner = SimpleNamespace(
        status=lambda: {
            "running": True,
            "pid": 4312,
            "process_generation": 8,
        }
    )
    leaf_a = RuntimeNodeRef("tier1", "tier1_variant", "id-a", "leaf-a")
    leaf_b = RuntimeNodeRef("tier1", "tier1_variant", "id-b", "leaf-b")
    runtime._runtime_route_snapshot = _snapshot(
        route_epoch=31,
        group="TIER1-AUTO",
        members=("leaf-a", "leaf-b"),
        now="leaf-a",
        refs=(leaf_a, leaf_b),
    )
    selected = {"leaf": "leaf-a"}
    reads: list[str] = []

    def proxies(**_kwargs: Any) -> dict[str, Any]:
        reads.append(selected["leaf"])
        return {
            "proxies": {
                "FINAL-EXIT": {
                    "type": "Selector",
                    "now": "TIER1-AUTO",
                    "all": ["TIER1-AUTO", "REJECT"],
                },
                "TIER1-AUTO": {
                    "type": "Fallback",
                    "now": selected["leaf"],
                    "all": ["leaf-a", "leaf-b"],
                },
                "leaf-a": {"type": "Shadowsocks"},
                "leaf-b": {"type": "Shadowsocks"},
                "REJECT": {"type": "Reject"},
            }
        }

    runtime.mihomo_proxies = proxies
    first = runtime.current_route_context()
    cached = runtime.current_route_context()
    selected["leaf"] = "leaf-b"
    runtime._runtime_route_epoch += 1
    changed = runtime.current_route_context()

    assert first["leaf_proxy"] == "leaf-a"
    assert cached["leaf_proxy"] == "leaf-a"
    assert changed["leaf_proxy"] == "leaf-b"
    assert reads == ["leaf-a", "leaf-b"]
