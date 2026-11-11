from __future__ import annotations

import threading
import time
import logging
import socket
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

import desktop_runtime as desktop_runtime_module
from core_runner import CoreOutputEvent
from desktop_runtime import (
    RUNTIME_LISTENER_CONFLICT_MESSAGE,
    RUNTIME_LISTENER_SETUP_MESSAGE,
    DesktopRuntime,
    RuntimeListenerPreflightError,
    RuntimeListenerSetupError,
)
from runtime_listener_preflight import (
    RuntimeListenerPreflightResult,
    RuntimeListenerPreflightState,
    RuntimeListenerSpec,
)
from runtime_egress_recovery import (
    BoundRuntimeFailure,
    CoreLoadedGroupState,
    CoreLoadedRouteSnapshot,
    RuntimeEgressCoordinator,
    RuntimeNodeRef,
    RuntimeRouteBinding,
)


def _activation_runtime() -> DesktopRuntime:
    runtime = object.__new__(DesktopRuntime)
    runtime._tier3_egress_activation_lock = threading.RLock()
    runtime._tier3_standby_activation_failure_key = None
    runtime._tier3_standby_activation_retry_stage = 0
    runtime._tier3_standby_activation_retry_at = 0.0
    runtime._tier3_standby_activation_facility_hold = False
    runtime._tier3_standby_activation_last_reason = ""
    runtime._tier3_runtime_qualification_retry_at = 0.0
    runtime._runtime_local_facility_fault_reason = ""
    runtime.manager = SimpleNamespace(
        tier3_startup_gate_satisfied=True,
        effective_egress_ok=None,
    )
    runtime.core_runner = SimpleNamespace(is_running=lambda: True)
    runtime._current_core_runtime_phase = lambda: "formal_no_capture"
    runtime._runtime_traffic_capture_wanted = lambda: True
    runtime._egress_revalidation_active = lambda: False
    return runtime


def _unused_loopback_port(excluded: set[int]) -> int:
    while True:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        finally:
            probe.close()
        if port not in excluded:
            excluded.add(port)
            return port


def _occupy_loopback_port(*transports: str) -> tuple[int, list[socket.socket]]:
    listeners: list[socket.socket] = []
    port = 0
    try:
        for transport in transports:
            socket_type = (
                socket.SOCK_DGRAM if transport == "udp" else socket.SOCK_STREAM
            )
            listener = socket.socket(socket.AF_INET, socket_type)
            listeners.append(listener)
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive is not None:
                listener.setsockopt(socket.SOL_SOCKET, exclusive, 1)
            listener.bind(("127.0.0.1", port))
            if socket_type == socket.SOCK_STREAM:
                listener.listen(1)
            if port == 0:
                port = int(listener.getsockname()[1])
    except Exception:
        for listener in listeners:
            listener.close()
        raise
    return port, listeners


def _listener_preflight_runtime(
    *,
    network_mode: str,
    mixed_port: int,
    controller_port: int,
    dns_port: int,
) -> DesktopRuntime:
    runtime = object.__new__(DesktopRuntime)
    runtime.config = {
        "client": {
            "network_mode": network_mode,
            "mihomo_mixed_port": mixed_port,
            "system_proxy": {
                "enabled": True,
                "host": "127.0.0.1",
                "port": mixed_port,
            },
            "mihomo_controller": {
                "enabled": True,
                "host": "127.0.0.1",
                "port": controller_port,
            },
            "dns": {
                "enable": True,
                "listen": f"127.0.0.1:{dns_port}",
            },
        }
    }
    runtime.network_mode = network_mode
    runtime.core_runner = None
    runtime.logger = logging.getLogger(
        f"listener-preflight-{time.monotonic_ns()}"
    )
    runtime.logger.handlers = [logging.NullHandler()]
    runtime.logger.propagate = False
    return runtime


@pytest.mark.parametrize("network_mode", ("system_proxy", "tun", "mixed"))
@pytest.mark.parametrize("listener_kind", ("mixed", "controller"))
def test_listener_preflight_blocks_mixed_and_controller_in_every_network_mode(
    network_mode: str,
    listener_kind: str,
) -> None:
    occupied_port, occupied = _occupy_loopback_port("tcp")
    excluded = {occupied_port}
    ports = {
        "mixed": _unused_loopback_port(excluded),
        "controller": _unused_loopback_port(excluded),
        "dns": _unused_loopback_port(excluded),
    }
    ports[listener_kind] = occupied_port
    runtime = _listener_preflight_runtime(
        network_mode=network_mode,
        mixed_port=ports["mixed"],
        controller_port=ports["controller"],
        dns_port=ports["dns"],
    )
    try:
        with pytest.raises(RuntimeListenerPreflightError) as caught:
            runtime._preflight_runtime_listeners()
    finally:
        for listener in occupied:
            listener.close()

    assert (listener_kind, "tcp", "127.0.0.1", occupied_port) in (
        caught.value.conflicts
    )
    # T-P2-12 (B-26(3)): params carries the whitelisted conflicts only.
    assert caught.value.diagnostic == {
        "code": "runtime_listener_conflict",
        "params": {
            "conflicts": [
                {
                    "kind": kind,
                    "transport": transport,
                    "host": host,
                    "port": port,
                }
                for kind, transport, host, port in caught.value.conflicts
            ]
        },
        "message": RUNTIME_LISTENER_CONFLICT_MESSAGE,
    }
    assert str(occupied_port) not in str(caught.value)


@pytest.mark.parametrize(
    "transports",
    (("tcp",), ("udp",), ("tcp", "udp")),
    ids=("tcp", "udp", "tcp-and-udp"),
)
def test_listener_preflight_system_proxy_does_not_block_on_dns_conflicts(
    transports: tuple[str, ...],
) -> None:
    occupied_port, occupied = _occupy_loopback_port(*transports)
    excluded = {occupied_port}
    runtime = _listener_preflight_runtime(
        network_mode="system_proxy",
        mixed_port=_unused_loopback_port(excluded),
        controller_port=_unused_loopback_port(excluded),
        dns_port=occupied_port,
    )
    try:
        result = runtime._preflight_runtime_listeners()
    finally:
        for listener in occupied:
            listener.close()

    assert result.state is RuntimeListenerPreflightState.AVAILABLE
    assert all(spec.kind != "dns" for spec in result.specs)


@pytest.mark.parametrize("network_mode", ("tun", "mixed"))
@pytest.mark.parametrize("transport", ("tcp", "udp"))
def test_listener_preflight_tun_and_mixed_block_on_dns_conflicts(
    network_mode: str,
    transport: str,
) -> None:
    occupied_port, occupied = _occupy_loopback_port(transport)
    excluded = {occupied_port}
    runtime = _listener_preflight_runtime(
        network_mode=network_mode,
        mixed_port=_unused_loopback_port(excluded),
        controller_port=_unused_loopback_port(excluded),
        dns_port=occupied_port,
    )
    try:
        with pytest.raises(RuntimeListenerPreflightError) as caught:
            runtime._preflight_runtime_listeners()
    finally:
        for listener in occupied:
            listener.close()

    assert ("dns", transport, "127.0.0.1", occupied_port) in (
        caught.value.conflicts
    )


def test_listener_preflight_exempts_the_current_runtime_core() -> None:
    runtime = _listener_preflight_runtime(
        network_mode="system_proxy",
        mixed_port=7890,
        controller_port=9090,
        dns_port=1053,
    )
    runtime.core_runner = SimpleNamespace(is_running=lambda: True)

    result = runtime._preflight_runtime_listeners()

    assert result.state is RuntimeListenerPreflightState.AVAILABLE
    assert result.specs == ()


def test_released_listener_is_rechecked_on_the_next_start_attempt() -> None:
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    if exclusive is not None:
        occupied.setsockopt(socket.SOL_SOCKET, exclusive, 1)
    occupied.bind(("127.0.0.1", 0))
    occupied.listen(1)
    port = int(occupied.getsockname()[1])
    excluded = {port}
    runtime = _listener_preflight_runtime(
        network_mode="system_proxy",
        mixed_port=port,
        controller_port=_unused_loopback_port(excluded),
        dns_port=_unused_loopback_port(excluded),
    )

    with pytest.raises(RuntimeListenerPreflightError):
        runtime._preflight_runtime_listeners()
    occupied.close()

    result = runtime._preflight_runtime_listeners()

    assert result.state is RuntimeListenerPreflightState.AVAILABLE
    assert result.conflicts == ()


def test_listener_setup_error_has_a_distinct_non_conflict_diagnostic() -> None:
    error = RuntimeListenerSetupError()

    assert error.diagnostic == {
        "code": "runtime_listener_unavailable",
        "params": {},
        "message": RUNTIME_LISTENER_SETUP_MESSAGE,
    }
    assert RUNTIME_LISTENER_CONFLICT_MESSAGE not in str(error)


def test_authoritative_core_bind_failure_uses_confirmed_occupancy() -> None:
    runtime = _listener_preflight_runtime(
        network_mode="system_proxy",
        mixed_port=7890,
        controller_port=9090,
        dns_port=1053,
    )
    spec = RuntimeListenerSpec("mixed", "tcp", "127.0.0.1", 7890)
    runtime._runtime_listener_preflight_report = lambda: RuntimeListenerPreflightResult(
        state=RuntimeListenerPreflightState.OCCUPIED,
        specs=(spec,),
        conflicts=(spec,),
        endpoint_snapshot_available=True,
    )
    runner = SimpleNamespace(
        get_recent_logs=lambda limit=200: [
            "Started mihomo: bundled.exe -f runtime.yaml",
            "listen tcp 127.0.0.1:7890: bind: permission denied",
        ]
    )

    error = runtime._authoritative_runtime_listener_start_error(runner)

    assert isinstance(error, RuntimeListenerPreflightError)
    assert error.diagnostic["code"] == "runtime_listener_conflict"


def test_authoritative_core_bind_failure_without_owner_is_network_setup_error() -> None:
    runtime = _listener_preflight_runtime(
        network_mode="system_proxy",
        mixed_port=7890,
        controller_port=9090,
        dns_port=1053,
    )
    spec = RuntimeListenerSpec("mixed", "tcp", "127.0.0.1", 7890)
    runtime._runtime_listener_preflight_report = lambda: RuntimeListenerPreflightResult(
        state=RuntimeListenerPreflightState.INDETERMINATE,
        specs=(spec,),
        endpoint_snapshot_available=True,
    )
    runner = SimpleNamespace(
        get_recent_logs=lambda limit=200: [
            "Started mihomo: bundled.exe -f runtime.yaml",
            "listen tcp 127.0.0.1:7890: bind: permission denied",
        ]
    )

    error = runtime._authoritative_runtime_listener_start_error(runner)

    assert isinstance(error, RuntimeListenerSetupError)
    assert error.diagnostic["code"] == "runtime_listener_unavailable"


@pytest.mark.parametrize("transport", ("tcp", "udp"))
def test_authoritative_system_proxy_dns_bind_failure_is_non_blocking(
    transport: str,
) -> None:
    runtime = _listener_preflight_runtime(
        network_mode="system_proxy",
        mixed_port=7890,
        controller_port=9090,
        dns_port=1053,
    )
    runtime._runtime_listener_preflight_report = lambda: pytest.fail(
        "an optional DNS bind fault must not trigger required-listener classification"
    )
    runner = SimpleNamespace(
        get_recent_logs=lambda limit=200: [
            "Started mihomo: bundled.exe -f runtime.yaml",
            f"listen {transport} 127.0.0.1:1053: bind: permission denied",
        ]
    )

    assert runtime._required_runtime_listener_bind_fault(runner) is None
    assert runtime._authoritative_runtime_listener_start_error(runner) is None


@pytest.mark.parametrize(
    ("listener_kind", "port"),
    (("mixed", 7890), ("controller", 9090)),
)
def test_authoritative_system_proxy_scanner_skips_dns_and_finds_required_fault(
    listener_kind: str,
    port: int,
) -> None:
    runtime = _listener_preflight_runtime(
        network_mode="system_proxy",
        mixed_port=7890,
        controller_port=9090,
        dns_port=1053,
    )
    spec = RuntimeListenerSpec(listener_kind, "tcp", "127.0.0.1", port)
    runtime._runtime_listener_preflight_report = lambda: RuntimeListenerPreflightResult(
        state=RuntimeListenerPreflightState.OCCUPIED,
        specs=(spec,),
        conflicts=(spec,),
        endpoint_snapshot_available=True,
    )
    runner = SimpleNamespace(
        get_recent_logs=lambda limit=200: [
            "Started mihomo: bundled.exe -f runtime.yaml",
            f"listen tcp 127.0.0.1:{port}: bind: address already in use",
            # This is newest, so the reverse scanner must explicitly skip it
            # before it can find the startup-blocking listener above.
            "listen udp 127.0.0.1:1053: bind: permission denied",
        ]
    )

    fault = runtime._required_runtime_listener_bind_fault(runner)
    error = runtime._authoritative_runtime_listener_start_error(runner)

    assert fault is not None
    assert fault.kind.value == listener_kind
    assert isinstance(error, RuntimeListenerPreflightError)
    assert error.conflicts == (
        (listener_kind, "tcp", "127.0.0.1", port),
    )


def test_old_core_bind_log_cannot_poison_a_new_start_attempt() -> None:
    runtime = _listener_preflight_runtime(
        network_mode="system_proxy",
        mixed_port=7890,
        controller_port=9090,
        dns_port=1053,
    )
    runtime._runtime_listener_preflight_report = lambda: pytest.fail(
        "an old launch must not trigger a new endpoint ownership query"
    )
    runner = SimpleNamespace(
        get_recent_logs=lambda limit=200: [
            "Started mihomo: old.exe -f old.yaml",
            "listen tcp 127.0.0.1:7890: bind: address already in use",
            "Started mihomo: bundled.exe -f runtime.yaml",
            "unrelated startup output",
        ]
    )

    assert runtime._authoritative_runtime_listener_start_error(runner) is None


def test_initial_start_preflight_precedes_runtime_owner_creation() -> None:
    source = inspect.getsource(DesktopRuntime._start_locked)
    initial_start = source.split("core_start_error: str | None = None", 1)[1]

    assert initial_start.index("recover_stale_system_proxy") < initial_start.index(
        "_preflight_runtime_listeners"
    )
    assert initial_start.index("_preflight_runtime_listeners") < initial_start.index(
        "KillSwitchManager"
    )
    assert initial_start.index("_preflight_runtime_listeners") < initial_start.index(
        "_new_proxy_tier_manager"
    )


def test_standby_activation_failure_key_requires_a_material_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _activation_runtime()
    now = [100.0]
    key = [(7, "digest-a", "route-a", 2)]
    queued: list[tuple[object, ...]] = []
    monkeypatch.setattr(desktop_runtime_module.time, "monotonic", lambda: now[0])
    runtime._tier3_standby_activation_key = lambda: key[0]
    runtime._queue_tier3_egress_activation = lambda: queued.append(key[0])

    state = runtime._record_tier3_standby_activation_failure("probe failed")
    assert state["facility_hold"] is True
    assert state["retry_at"] == 0.0
    assert state["retry_stage"] == 0
    assert state["hold_kind"] == "activation_hold"

    runtime._maybe_retry_tier3_standby_activation()
    assert queued == []

    # The immutable failure is event-gated. Advancing arbitrary amounts of
    # time must not recreate the same activation transaction.
    now[0] = max(now[0] + 600.0, float(state["retry_at"]) + 600.0)
    runtime._maybe_retry_tier3_standby_activation()
    runtime._maybe_retry_tier3_standby_activation()
    assert queued == []

    # A real binding/key event may wake activation immediately.
    key[0] = (7, "digest-a", "route-b", 2)
    runtime._maybe_retry_tier3_standby_activation()
    assert queued == [key[0]]
    assert runtime._tier3_standby_activation_failure_key is None
    assert runtime._tier3_standby_activation_retry_stage == 0
    assert runtime._tier3_standby_activation_facility_hold is False

    # The manual dual-204 success path uses this explicit reset before waking
    # the same unchanged route; that is also an authorized event.
    queued.clear()
    runtime._tier3_standby_activation_failure_key = key[0]
    runtime._tier3_standby_activation_retry_stage = 3
    runtime._tier3_standby_activation_retry_at = 0.0
    runtime._tier3_standby_activation_facility_hold = True
    runtime._reset_tier3_standby_activation_failure()
    runtime._maybe_retry_tier3_standby_activation()
    assert queued == [key[0]]


def test_facility_hold_suppresses_repeated_stdout_and_manual_probe_attribution() -> None:
    runtime = object.__new__(DesktopRuntime)
    facility_events: list[str] = []
    snapshot_refreshes: list[str] = []
    forbidden: list[str] = []
    runtime._runtime_facility_hold_reason = lambda: "network context stabilizing"
    runtime._record_runtime_facility_degradation = facility_events.append
    runtime._queue_runtime_route_snapshot_refresh = lambda: snapshot_refreshes.append(
        "refresh"
    )
    runtime._start_runtime_independent_probe = lambda *_args, **_kwargs: forbidden.append(
        "probe"
    )
    runtime._start_runtime_recovery = lambda *_args, **_kwargs: forbidden.append(
        "mutation"
    )
    runtime.manager = SimpleNamespace(
        apply_runtime_health_observation=lambda *_args, **_kwargs: forbidden.append(
            "strike"
        )
    )

    class Coordinator:
        @staticmethod
        def observe(*_args, **_kwargs):
            forbidden.append("observe")
            return SimpleNamespace(accepted=True, probe_token="probe")

    runtime._runtime_egress_coordinator = Coordinator()
    snapshot = CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="digest-a",
        route_epoch=11,
        network_context_epoch=4,
    )
    event = CoreOutputEvent(
        raw_line=(
            "level=warning msg=\"[TCP] dial GENERAL (match Match/) "
            "127.0.0.1:55001 --> example.com:443 error: "
            "context deadline exceeded\""
        ),
        pid=4312,
        process_generation=7,
        received_at_monotonic=10.0,
    )

    for _index in range(4):
        runtime._handle_runtime_output_event(event, snapshot, {})
        assert runtime._submit_current_route_probe_failure(
            "manual dual probe failed",
            source="manual_exit_quality",
        ) is False

    assert forbidden == []
    assert len(facility_events) == 8
    assert snapshot_refreshes == ["refresh"] * 4


def test_stdout_queued_during_facility_hold_is_not_replayed_after_release() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime.stop_event = threading.Event()
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_hot_standby_lock = threading.RLock()
    runtime._runtime_local_facility_fault_reason = ""
    runtime._runtime_facility_epoch = 7
    runtime._runtime_network_context_epoch = 4
    runtime._runtime_network_context_hold_active = True
    runtime._runtime_network_context_hold_pending_candidate = False
    runtime._runtime_network_context_hold_reason = "WAN baseline unavailable"
    runtime._runtime_network_context_hold_epoch = 4
    forbidden: list[str] = []

    snapshot = CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="digest-a",
        route_epoch=11,
        network_context_epoch=4,
    )
    runtime._runtime_route_snapshot = snapshot
    runtime._runtime_route_snapshot_payload = {"proxies": {}}
    coordinator = RuntimeEgressCoordinator()
    coordinator.set_current_snapshot(snapshot)
    coordinator.observe = lambda *_args, **_kwargs: forbidden.append("observe")
    runtime._runtime_egress_coordinator = coordinator
    runtime._record_runtime_facility_degradation = (
        lambda *_args, **_kwargs: forbidden.append("facility")
    )
    runtime._start_runtime_independent_probe = (
        lambda *_args, **_kwargs: forbidden.append("probe")
    )
    runtime._start_runtime_recovery = (
        lambda *_args, **_kwargs: forbidden.append("mutation")
    )
    runtime.manager = SimpleNamespace(
        apply_runtime_health_observation=(
            lambda *_args, **_kwargs: forbidden.append("strike")
        )
    )
    readiness: list[tuple[bool | None, str]] = []
    runtime._set_effective_egress_readiness_state = (
        lambda ok, reason: readiness.append((ok, reason))
    )
    event = CoreOutputEvent(
        raw_line=(
            "level=warning msg=\"[TCP] dial GENERAL (match Match/) "
            "127.0.0.1:55001 --> example.com:443 error: "
            "context deadline exceeded\""
        ),
        pid=4312,
        process_generation=7,
        received_at_monotonic=10.0,
    )

    runtime._on_core_output_event(event)
    queued = coordinator.next_ingress()
    assert queued.facility_epoch == 7
    assert queued.facility_hold_active is True

    # Release the hold through the production baseline gate before the bounded
    # ingress queue is drained. The old WAN burst must not become a fresh leaf
    # incident after the facility epoch advances.
    assert runtime._release_runtime_network_context_hold_after_verified_probe()
    assert runtime._runtime_facility_epoch == 8
    assert readiness == [(True, "")]
    runtime._handle_runtime_output_event(
        queued.event,
        queued.snapshot,
        queued.snapshot_payload,
        ingress_facility_epoch=queued.facility_epoch,
        ingress_facility_hold_active=queued.facility_hold_active,
    )

    assert forbidden == []


def test_invalid_network_context_enters_hold_without_route_or_leaf_side_effects() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_hot_standby_lock = threading.RLock()
    runtime._runtime_local_facility_fault_reason = ""
    runtime._runtime_facility_epoch = 7
    runtime._runtime_network_context_epoch = 4
    runtime._runtime_route_epoch = 11
    runtime._runtime_network_context_hold_active = False
    runtime._runtime_network_context_hold_pending_candidate = False
    runtime._runtime_network_context_hold_reason = ""
    runtime._runtime_network_context_hold_epoch = 4
    runtime._runtime_network_context_rebase_stage = 0
    runtime._runtime_network_context_rebase_at = 0.0
    forbidden: list[str] = []
    readiness: list[tuple[bool | None, str]] = []
    runtime._set_effective_egress_readiness_state = (
        lambda ok, reason: readiness.append((ok, reason))
    )
    runtime._start_runtime_recovery = (
        lambda *_args, **_kwargs: forbidden.append("mutation")
    )
    runtime.manager = SimpleNamespace(
        apply_runtime_health_observation=(
            lambda *_args, **_kwargs: forbidden.append("strike")
        )
    )

    changed = runtime._observe_runtime_network_context(
        desktop_runtime_module._RuntimeNetworkContextObservation(
            signature="",
            valid=False,
            components=(("default_route", False), ("dns", False)),
        )
    )

    assert changed is False
    assert runtime._runtime_network_context_epoch == 4
    assert runtime._runtime_route_epoch == 11
    assert runtime._runtime_facility_epoch == 8
    assert runtime._runtime_network_context_hold_active is True
    assert runtime._runtime_network_context_hold_pending_candidate is False
    assert runtime._runtime_network_context_hold_epoch == 4
    assert "unavailable" in runtime._runtime_network_context_hold_reason
    assert forbidden == []
    assert readiness == [(None, "runtime network context is unavailable")]


def test_inflight_leaf_probe_cannot_commit_after_facility_hold_begins() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_failure_probe_bindings = set()
    runtime._runtime_route_mutation_lease = None
    runtime._runtime_selector_intent_revision = 0
    entered = threading.Event()
    release = threading.Event()
    hold = [False]
    node_records: list[dict[str, object]] = []
    recoveries: list[str] = []
    facility_events: list[str] = []

    ref = RuntimeNodeRef(
        tier="tier3",
        pool_kind="tier3b",
        identity="identity-a",
        proxy_name="leaf-a",
    )
    alternative = RuntimeNodeRef(
        tier="tier3",
        pool_kind="tier3b",
        identity="identity-b",
        proxy_name="leaf-b",
    )
    snapshot = CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="digest-a",
        route_epoch=11,
        network_context_epoch=4,
    )
    binding = RuntimeRouteBinding(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="digest-a",
        route_epoch=11,
        network_context_epoch=4,
        observed_outbound="GENERAL",
        route_chain=("GENERAL", "FINAL-EXIT", "leaf-a"),
        node=ref,
    )
    failure = BoundRuntimeFailure(
        binding=binding,
        protocol="TCP",
        observed_outbound="GENERAL",
        destination_token="destination-token",
        error_type="timeout",
        # The production recovery budget is bound to the actual incident
        # observation.  Keep this concurrency fixture current so it exercises
        # the facility-hold race instead of correctly expiring as a stale
        # six-second incident.
        observed_at_monotonic=time.monotonic(),
    )

    class Coordinator:
        @staticmethod
        def current_snapshot_for(_binding):
            return SimpleNamespace(incident_id="incident-a")

        @staticmethod
        def snapshot_ownership_matches(*_args, **_kwargs):
            return True

        @staticmethod
        def record_probe(*_args, **kwargs):
            if not bool(kwargs.get("facility_failure", False)):
                node_records.append(dict(kwargs))
            return SimpleNamespace(
                cleared=False,
                confirmed=True,
                incident_id="incident-a",
                probe_token="probe-a",
            )

    runtime._runtime_egress_coordinator = Coordinator()
    runtime._runtime_worker_token = lambda: (7, 3, 11, 4)
    runtime._runtime_worker_token_is_current = lambda _token: True
    runtime._runtime_origin_diagnostic_binding_for_probe = (
        lambda *_args, **_kwargs: None
    )
    runtime._runtime_facility_hold_reason = (
        lambda: "network context stabilizing" if hold[0] else ""
    )
    runtime._record_runtime_facility_degradation = facility_events.append
    runtime._runtime_confirmation_timeout_ms = lambda *_args, **_kwargs: 500
    runtime._probe_managed_alternative = (
        lambda *_args, **_kwargs: (alternative, {"status": 204})
    )

    def probe_leaf(*_args, **_kwargs):
        entered.set()
        assert release.wait(1.0)
        return {"status": 204}

    runtime._probe_runtime_leaf = probe_leaf
    runtime._start_runtime_recovery = (
        lambda *_args, **_kwargs: recoveries.append("recovery")
    )
    runtime._start_runtime_failback = lambda *_args, **_kwargs: recoveries.append(
        "failback"
    )

    runtime._start_runtime_independent_probe(
        failure,
        snapshot,
        {"proxy_map": {}},
        probe_token="probe-a",
    )
    assert entered.wait(1.0)
    hold[0] = True
    release.set()
    deadline = time.monotonic() + 1.0
    while failure.circuit_key in runtime._runtime_failure_probe_bindings:
        assert time.monotonic() < deadline
        time.sleep(0.005)

    assert node_records == []
    assert recoveries == []


def _egress_revalidation_worker_runtime(
    *,
    mode: str,
    phase: str,
) -> tuple[DesktopRuntime, CoreLoadedRouteSnapshot, list[tuple[object, ...]]]:
    runtime = object.__new__(DesktopRuntime)
    runtime._tier3_egress_activation_lock = threading.RLock()
    runtime._core_lifecycle_lock = threading.RLock()
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_hot_standby_lock = threading.RLock()
    runtime.stop_event = threading.Event()
    runtime._tier3_egress_activation_pending = True
    runtime._tier3_egress_activation_pending_mode = mode
    runtime._tier3_egress_activation_generation = 3
    runtime._tier3_egress_activation_worker_running = True
    runtime._tier3_egress_activation_active_mode = ""
    runtime._core_generation = 9
    runtime._runtime_route_epoch = 11
    runtime._runtime_network_context_epoch = 4
    runtime._runtime_facility_epoch = 7
    runtime._runtime_selector_intent_revision = 2
    runtime._runtime_local_facility_fault_reason = ""
    runtime._runtime_network_context_hold_active = True
    runtime._runtime_network_context_hold_pending_candidate = False
    runtime._runtime_network_context_hold_reason = "WAN baseline unavailable"
    runtime._runtime_network_context_hold_epoch = 4
    runtime._runtime_network_context_rebase_stage = 3
    runtime._runtime_network_context_rebase_at = float("inf")
    runtime._incremental_egress_recheck_max_attempts = 1
    runtime._incremental_egress_recheck_retry_delay_seconds = 0.0
    runtime.manager = None
    runtime._tier3_standby_activation_failure_key = None
    runtime._tier3_standby_activation_retry_stage = 0
    runtime._tier3_standby_activation_retry_at = 0.0
    runtime._tier3_standby_activation_facility_hold = False
    runtime._tier3_standby_activation_last_reason = ""
    runtime._tier3_runtime_qualification_retry_at = 0.0

    runner = SimpleNamespace(is_running=lambda: True)
    runtime.core_runner = runner
    runtime._current_core_runtime_phase = lambda: phase
    runtime._runtime_traffic_capture_wanted = lambda: True
    runtime._tier3_is_primary_export = lambda: False
    runtime._runtime_worker_token = lambda: (9, 11, 4, 7)
    runtime._reset_tier3_standby_activation_failure = lambda: None
    runtime._resume_deferred_runtime_failbacks = lambda: None
    runtime._queue_final_exit_identity_diagnostic = lambda **_kwargs: None
    runtime.logger = SimpleNamespace(
        debug=lambda *_args, **_kwargs: None,
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        error=lambda *_args, **_kwargs: None,
    )
    runtime.last_error = ""

    ref = RuntimeNodeRef(
        tier="tier3",
        pool_kind="tier3a",
        identity="activation-fixture-leaf-id",
        proxy_name="activation-fixture-leaf",
    )
    snapshot = CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=7,
        core_generation=9,
        semantic_digest="digest-a",
        route_epoch=11,
        network_context_epoch=4,
        proxy_nodes=(ref,),
        group_graph=(("FINAL-EXIT", (ref.proxy_name, "REJECT")),),
        group_states=(
            CoreLoadedGroupState(
                name="FINAL-EXIT",
                type_name="Selector",
                members=(ref.proxy_name, "REJECT"),
                now=ref.proxy_name,
            ),
        ),
    )
    runtime._runtime_route_snapshot = snapshot
    runtime._runtime_route_snapshot_payload = {
        "proxies": {
            "FINAL-EXIT": {
                "type": "Selector",
                "now": ref.proxy_name,
                "all": [ref.proxy_name, "REJECT"],
            },
            ref.proxy_name: {"type": "Shadowsocks"},
        },
        "proxy_map": {
            ref.proxy_name: {
                "tier": ref.tier,
                "pool_kind": ref.pool_kind,
                "identity": ref.identity,
            }
        },
    }
    runtime._verify_capture_route_after_reload = lambda **_kwargs: {
        "status": 204,
        "fixture": True,
    }
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
    events: list[tuple[object, ...]] = []
    runtime._set_effective_egress_readiness_state = (
        lambda ok, reason: events.append(
            ("readiness", ok, reason, runtime._runtime_facility_epoch)
        )
    )
    runtime._set_effective_egress_state = lambda ok, reason: events.append(
        ("effective", ok, reason, runtime._runtime_facility_epoch)
    )
    return runtime, snapshot, events


def test_incremental_revalidation_releases_matching_facility_hold_before_ready() -> None:
    runtime, snapshot, events = _egress_revalidation_worker_runtime(
        mode="incremental",
        phase="formal_capture",
    )
    verified: list[tuple[object, ...]] = []

    def verify(**kwargs):
        verified.append(
            (
                kwargs.get("expected_worker_token"),
                kwargs.get("expected_snapshot"),
                runtime._runtime_facility_epoch,
            )
        )
        return {"status": 204, "url": "https://example.invalid/generate_204"}

    runtime._verify_runtime_final_exit = verify

    runtime._tier3_egress_activation_worker()

    assert verified == [((9, 11, 4, 7), snapshot, 7)]
    assert runtime._runtime_network_context_hold_active is False
    assert runtime._runtime_network_context_hold_reason == ""
    assert runtime._runtime_facility_epoch == 8
    assert events == [
        ("readiness", True, "", 8),
        ("effective", True, "", 8),
    ]


def test_incremental_probe_budget_wait_never_publishes_or_advances_failure() -> None:
    runtime, _snapshot, events = _egress_revalidation_worker_runtime(
        mode="incremental",
        phase="formal_capture",
    )
    runtime._verify_runtime_final_exit = lambda **_kwargs: (_ for _ in ()).throw(
        desktop_runtime_module._RuntimeProbeBudgetUnavailable(
            "both runtime probe slots are occupied"
        )
    )
    indeterminate: list[str] = []
    runtime._record_tier3_standby_activation_indeterminate = (
        lambda reason, **_kwargs: indeterminate.append(str(reason))
        or {"reason": str(reason)}
    )
    runtime._publish_tier3_standby_activation_failure = (
        lambda _exc, **_kwargs: pytest.fail(
            "probe capacity cannot advance the activation failure state"
        )
    )

    runtime._tier3_egress_activation_worker()

    assert len(indeterminate) == 1
    assert runtime._runtime_network_context_hold_active is True
    assert runtime._runtime_facility_epoch == 7
    assert events == [
        (
            "readiness",
            None,
            "Bastion Layer verification is waiting for probe capacity",
            7,
        )
    ]


@pytest.mark.parametrize("invalidated_epoch", ["route", "facility"])
def test_activation_revalidation_with_stale_cas_keeps_hold_and_capture_closed(
    invalidated_epoch: str,
) -> None:
    runtime, _snapshot, events = _egress_revalidation_worker_runtime(
        mode="activation",
        phase="formal_no_capture",
    )
    forbidden: list[str] = []
    failures: list[BaseException] = []

    def verify_activation() -> None:
        if invalidated_epoch == "route":
            runtime._runtime_route_epoch += 1
        else:
            runtime._runtime_facility_epoch += 1

    runtime._verify_tier3_final_exit_for_activation = verify_activation
    runtime._hot_reload_runtime_config_phase = (
        lambda *_args, **_kwargs: forbidden.append("capture_reload")
    )
    runtime._apply_system_proxy_capture_after_core_start = (
        lambda *_args, **_kwargs: forbidden.append("capture_enable")
    )
    runtime.traffic_monitor = SimpleNamespace(
        start=lambda: forbidden.append("traffic_start"),
        stop=lambda: forbidden.append("traffic_stop"),
    )
    runtime._rollback_to_formal_no_capture = lambda *_args, **_kwargs: None

    def publish_failure(
        exc: BaseException,
        **_kwargs,
    ) -> dict[str, object]:
        failures.append(exc)
        return {"retry_stage": 1, "facility_hold": True}

    runtime._publish_tier3_standby_activation_failure = publish_failure

    runtime._tier3_egress_activation_worker()

    assert forbidden == []
    assert len(failures) == 1
    assert "facility hold changed" in str(failures[0])
    assert runtime._runtime_network_context_hold_active is True
    assert runtime._runtime_network_context_hold_reason == "WAN baseline unavailable"
    assert events == []


def test_runtime_start_commit_queues_admission_without_failure_monitor() -> None:
    runtime, _snapshot, _events = _egress_revalidation_worker_runtime(
        mode="activation",
        phase="formal_no_capture",
    )
    runtime._runtime_intent_lock = threading.RLock()
    runtime._desired_running = True
    runtime._stop_request_pending = False
    runtime._lifecycle_revision = 12
    runtime.running = False
    runtime._monitor_loop = lambda: None
    failure_monitor_calls: list[str] = []
    admissions: list[int] = []
    runtime._start_runtime_failure_monitor = lambda: failure_monitor_calls.append(
        "started"
    )
    runtime._request_initial_egress_admission = (
        lambda: admissions.append(12) or True
    )

    runtime._commit_runtime_started(12)
    runtime.monitor_thread.join(timeout=0.5)

    assert runtime.running is True
    assert failure_monitor_calls == []
    assert admissions == [12]


def test_network_rebaseline_enters_slow_backoff_after_failed_1_2_5_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_hot_standby_lock = threading.RLock()
    runtime._runtime_local_facility_fault_reason = ""
    runtime._runtime_network_context_hold_active = True
    runtime._runtime_network_context_hold_pending_candidate = False
    runtime._runtime_network_context_hold_epoch = 4
    runtime._runtime_network_context_epoch = 4
    runtime._runtime_network_context_rebase_stage = 0
    runtime._runtime_network_context_rebase_at = 100.0
    now = [100.0]
    attempts: list[float] = []
    monkeypatch.setattr(desktop_runtime_module.time, "monotonic", lambda: now[0])

    def fail_probe(*_args, **_kwargs):
        attempts.append(now[0])
        raise TimeoutError("facility baseline unavailable")

    runtime._probe_mihomo_mixed_port_once = fail_probe

    runtime._maybe_rebaseline_runtime_network_context()
    now[0] = runtime._runtime_network_context_rebase_at
    runtime._maybe_rebaseline_runtime_network_context()
    now[0] = runtime._runtime_network_context_rebase_at
    runtime._maybe_rebaseline_runtime_network_context()
    assert len(attempts) == 3

    slow_retry_at = runtime._runtime_network_context_rebase_at
    assert slow_retry_at == now[0] + 30.0
    now[0] = slow_retry_at - 0.01
    runtime._maybe_rebaseline_runtime_network_context()
    assert len(attempts) == 3

    now[0] = slow_retry_at
    runtime._maybe_rebaseline_runtime_network_context()
    assert len(attempts) == 4
    assert runtime._runtime_network_context_rebase_at == now[0] + 60.0


def test_network_context_epoch_requires_two_matching_changed_signatures() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_hot_standby_lock = threading.RLock()
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_network_context_signature_value = "context-a"
    runtime._runtime_network_context_candidate_signature = ""
    runtime._runtime_network_context_candidate_count = 0
    runtime._runtime_network_context_hold_active = False
    runtime._runtime_network_context_hold_pending_candidate = False
    runtime._runtime_network_context_hold_reason = ""
    runtime._runtime_network_context_hold_epoch = 4
    runtime._runtime_network_context_epoch = 4
    runtime._runtime_route_epoch = 11
    runtime._runtime_route_attribution_barrier_epoch = 11
    runtime._runtime_route_attribution_barrier_until = 999.0
    runtime._runtime_route_snapshot = object()
    runtime._runtime_route_snapshot_payload = {"proxies": {}}
    runtime._runtime_route_selection_signature = (("FINAL-EXIT", "leaf-a"),)
    runtime._runtime_hot_standby_evidence = {"leaf-b": object()}
    runtime._runtime_hot_standby_last_probe_at = 50.0
    runtime._runtime_origin_diagnostic_bindings = {"incident": object()}
    snapshot_resets: list[object | None] = []
    refreshes: list[str] = []
    runtime._runtime_egress_coordinator = SimpleNamespace(
        set_current_snapshot=snapshot_resets.append
    )
    runtime.logger = SimpleNamespace(info=lambda *_args, **_kwargs: None)
    readiness: list[tuple[bool | None, str]] = []
    runtime._set_effective_egress_readiness_state = (
        lambda ok, reason: readiness.append((ok, reason))
    )
    runtime._queue_runtime_route_snapshot_refresh = lambda: refreshes.append(
        "refresh"
    )

    assert runtime._observe_runtime_network_context("context-b") is False
    assert runtime._runtime_network_context_epoch == 4
    assert runtime._runtime_network_context_hold_active is True
    assert readiness == [
        (None, "runtime network context change is stabilizing")
    ]

    # A single changed sample followed by the committed value is noise, not
    # a new context epoch.
    assert runtime._observe_runtime_network_context("context-a") is False
    assert runtime._runtime_network_context_epoch == 4
    assert snapshot_resets == []

    assert runtime._observe_runtime_network_context("context-b") is False
    assert runtime._runtime_network_context_epoch == 4
    assert runtime._observe_runtime_network_context("context-b") is True
    assert runtime._runtime_network_context_epoch == 5
    assert runtime._runtime_route_epoch == 12
    assert snapshot_resets == [None]
    assert refreshes == ["refresh"]


def test_same_network_signature_wakes_event_gated_hold_after_invalid_sample() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_hot_standby_lock = threading.RLock()
    runtime._runtime_network_context_signature_value = "context-a"
    runtime._runtime_network_context_candidate_signature = ""
    runtime._runtime_network_context_candidate_count = 0
    runtime._runtime_network_context_invalid_observed = False
    runtime._runtime_network_context_hold_active = False
    runtime._runtime_network_context_hold_pending_candidate = False
    runtime._runtime_network_context_rebase_stage = 3
    runtime._runtime_network_context_rebase_at = float("inf")

    def enter_hold(_reason: str) -> None:
        runtime._runtime_network_context_hold_active = True
        runtime._runtime_network_context_hold_pending_candidate = False
        runtime._runtime_network_context_rebase_stage = 3
        runtime._runtime_network_context_rebase_at = float("inf")

    runtime._enter_runtime_facility_hold = enter_hold

    assert runtime._observe_runtime_network_context("") is False
    assert runtime._runtime_network_context_invalid_observed is True
    assert runtime._runtime_network_context_rebase_at == float("inf")

    before = time.monotonic()
    assert runtime._observe_runtime_network_context("context-a") is False
    assert runtime._runtime_network_context_invalid_observed is False
    assert runtime._runtime_network_context_rebase_stage == 0
    assert before <= runtime._runtime_network_context_rebase_at < float("inf")


def test_empty_hot_standby_worker_establishes_baseline_without_resume_or_probe() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_hot_standby_lock = threading.RLock()
    runtime._runtime_hot_standby_thread = None
    runtime._runtime_hot_standby_last_loop_at = time.monotonic() - 30.0
    runtime._runtime_hot_standby_last_probe_at = 0.0
    runtime._runtime_hot_standby_last_traffic_totals = (99, 101)
    runtime._runtime_hot_standby_last_active_at = time.monotonic() - 1.0
    runtime._runtime_hot_standby_was_active = True
    runtime.stop_event = threading.Event()
    runtime.stop_event.set()
    runtime._runtime_hot_standby_traffic_totals = lambda: (0, 0)
    resumed: list[str] = []
    probes: list[str] = []
    runtime._advance_runtime_network_context_epoch = resumed.append
    runtime._refresh_one_runtime_hot_standby = lambda: probes.append("probe")

    runtime._start_runtime_hot_standby_maintenance()
    deadline = time.monotonic() + 1.0
    while runtime._runtime_hot_standby_thread is not None and time.monotonic() < deadline:
        time.sleep(0.005)

    assert runtime._runtime_hot_standby_thread is None
    assert resumed == []
    assert probes == []
    assert runtime._runtime_hot_standby_last_traffic_totals == (0, 0)
    assert runtime._runtime_hot_standby_last_active_at == 0.0
    assert runtime._runtime_hot_standby_last_loop_at > 0.0
    assert runtime._runtime_hot_standby_last_probe_at > 0.0


def test_internal_probe_bytes_do_not_mark_empty_runtime_as_business_active() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_hot_standby_lock = threading.RLock()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_hot_standby_last_traffic_totals = (0, 0)
    runtime._runtime_hot_standby_last_active_at = 0.0
    runtime._runtime_internal_probe_windows = {
        "probe-token": (99.5, 101.0),
    }
    totals = [(10, 20), (20, 30)]
    runtime._runtime_hot_standby_traffic_totals = lambda: totals.pop(0)

    assert runtime._runtime_traffic_active_for_hot_standby(100.0) is False
    assert runtime._runtime_hot_standby_last_traffic_totals == (10, 20)
    assert runtime._runtime_hot_standby_last_active_at == 0.0

    assert runtime._runtime_traffic_active_for_hot_standby(102.0) is True
    assert runtime._runtime_hot_standby_last_traffic_totals == (20, 30)
    assert runtime._runtime_hot_standby_last_active_at == 102.0


def test_local_listener_bind_error_requests_facility_guard_not_transport_ingress() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime.stop_event = threading.Event()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_local_facility_fault_reason = ""

    class Coordinator:
        def __init__(self) -> None:
            self.guards: list[str] = []
            self.ingress: list[object] = []

        def request_guard(self, reason: str) -> None:
            self.guards.append(reason)

        def submit_ingress(self, payload: object) -> None:
            self.ingress.append(payload)

    coordinator = Coordinator()
    runtime._runtime_egress_coordinator = coordinator
    event = CoreOutputEvent(
        raw_line=(
            "level=error msg=\"Start DNS server error: listen udp "
            "127.0.0.1:1053: bind: Only one usage of each socket address\""
        ),
        pid=4312,
        process_generation=7,
        received_at_monotonic=10.0,
    )

    runtime._on_core_output_event(event)

    # In system-proxy mode Mihomo's private DNS listener is capture-only: the
    # already-loaded mixed-port FINAL-EXIT path remains usable and must not be
    # pushed into the global recovery guard/Offline loop.
    assert coordinator.guards == []
    assert coordinator.ingress == []
    assert runtime._runtime_local_facility_fault_reason == ""
    assert runtime._runtime_facility_hold_reason() == ""


def _route_payload(*, selected_leaf: str, unrelated: str = "x") -> dict[str, object]:
    return {
        "proxies": {
            "FINAL-EXIT": {
                "type": "Selector",
                "now": "PROXY-FALLBACK",
                "all": ["PROXY-FALLBACK", "REJECT"],
            },
            "PROXY-FALLBACK": {
                "type": "Fallback",
                "now": "TIER3-AUTO",
                "all": ["TIER3-AUTO", "REJECT"],
            },
            "TIER3-AUTO": {
                "type": "Selector",
                "now": selected_leaf,
                "all": ["leaf-a", "leaf-b", "REJECT"],
            },
            "leaf-a": {"type": "Shadowsocks"},
            "leaf-b": {"type": "Shadowsocks"},
            "UNRELATED": {
                "type": "Selector",
                "now": unrelated,
                "all": ["x", "y"],
            },
            "x": {"type": "Shadowsocks"},
            "y": {"type": "Shadowsocks"},
        }
    }


def test_final_exit_selection_signature_ignores_unrelated_group_selection() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_selector_intent_revision = 3

    baseline = runtime._runtime_final_exit_selection_signature(
        _route_payload(selected_leaf="leaf-a", unrelated="x"),
        {},
    )
    unrelated_changed = runtime._runtime_final_exit_selection_signature(
        _route_payload(selected_leaf="leaf-a", unrelated="y"),
        {},
    )
    current_chain_changed = runtime._runtime_final_exit_selection_signature(
        _route_payload(selected_leaf="leaf-b", unrelated="y"),
        {},
    )

    assert unrelated_changed == baseline
    assert current_chain_changed != baseline


def test_final_exit_selection_signature_includes_fixed_gateway_underlay() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_selector_intent_revision = 0

    def payload(underlay: str) -> dict[str, object]:
        return {
            "proxies": {
                "FINAL-EXIT": {
                    "type": "Selector",
                    "now": "gateway-fixed",
                    "all": ["gateway-fixed", "REJECT"],
                },
                "gateway-fixed": {"type": "Shadowsocks"},
                "PROXY-FALLBACK": {
                    "type": "Fallback",
                    "now": underlay,
                    "all": ["leaf-a", "leaf-b", "REJECT"],
                },
                "leaf-a": {"type": "Shadowsocks"},
                "leaf-b": {"type": "Shadowsocks"},
            }
        }

    proxy_map = {
        "gateway-fixed": {
            "proxy": {
                "name": "gateway-fixed",
                "type": "ss",
                "dialer-proxy": "PROXY-FALLBACK",
            }
        }
    }
    first = runtime._runtime_final_exit_selection_signature(
        payload("leaf-a"),
        proxy_map,
    )
    second = runtime._runtime_final_exit_selection_signature(
        payload("leaf-b"),
        proxy_map,
    )

    assert first != second
    assert first[2][0] == "gateway_underlay"
    assert first[2][1]


def test_formal_no_capture_sync_preserves_pending_readiness_without_mutation() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._tier3_egress_activation_lock = threading.RLock()
    runtime._tier3_egress_activation_pending = False
    runtime._tier3_egress_activation_active_mode = ""
    runtime._core_lifecycle_lock = threading.RLock()
    runtime._core_route_mutation_pending = True
    runtime.running = True
    runtime.core_runner = SimpleNamespace(is_running=lambda: True)
    runtime._current_core_runtime_phase = lambda: "formal_no_capture"
    runtime._runtime_traffic_capture_wanted = lambda: True
    published: list[tuple[bool | None, str]] = []
    runtime.manager = SimpleNamespace(
        effective_egress_ok=None,
        set_effective_egress_state=lambda ok, reason: published.append((ok, reason)),
    )

    runtime._sync_effective_egress_state_from_core()

    assert len(published) == 1
    assert published[0][0] is None
    assert "pending" in published[0][1]
    assert runtime._core_route_mutation_pending is False


def test_complete_mixed_port_once_and_proxy_delay_share_global_two_probe_budget() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime.config = {}
    runtime._runtime_probe_semaphore = threading.BoundedSemaphore(2)
    runtime._startup_probe_timeout_seconds = lambda: 0.8
    runtime._default_probe_timeout_ms = lambda: 800
    runtime._register_runtime_internal_probe = lambda *_args, **_kwargs: None
    active = 0
    maximum = 0
    active_lock = threading.Lock()
    two_active = threading.Event()
    release = threading.Event()

    runtime._wait_for_mihomo_route_ready = lambda **_kwargs: {
        "ready": True,
    }
    runtime._current_mihomo_runtime_config_path = lambda: Path(
        "fixture-runtime.yaml"
    )

    def leased_result(**_kwargs) -> dict[str, int]:
        nonlocal active, maximum
        with active_lock:
            active += 1
            maximum = max(maximum, active)
            if active == 2:
                two_active.set()
        release.wait(0.6)
        with active_lock:
            active -= 1
        return {"status": 204}

    runtime._probe_mihomo_mixed_port_leased = leased_result
    runtime._probe_mihomo_mixed_port_once_leased = (
        lambda **_kwargs: leased_result()
    )

    class Client:
        @staticmethod
        def proxy_delay(*_args, **_kwargs):
            return leased_result()

    runtime._require_api_client = lambda: Client()
    results: list[object] = []
    failures: list[BaseException] = []

    def run(callable_) -> None:
        try:
            results.append(callable_())
        except BaseException as exc:  # pragma: no cover - assertion reports it
            failures.append(exc)

    calls = (
        runtime._probe_mihomo_mixed_port,
        lambda: runtime._probe_mihomo_mixed_port_once(timeout_seconds=0.8),
        lambda: runtime._probe_proxy_delay_with_urls(
            "leaf-a",
            ["https://www.gstatic.com/generate_204"],
            timeout_ms=800,
            request_timeout_seconds=0.8,
            expected_status="204",
        ),
    )
    threads = [threading.Thread(target=run, args=(call,)) for call in calls]
    for thread in threads:
        thread.start()
    assert two_active.wait(0.5)
    time.sleep(0.03)
    assert maximum == 2
    release.set()
    for thread in threads:
        thread.join(1.0)

    assert failures == []
    assert len(results) == 3
    assert maximum == 2
