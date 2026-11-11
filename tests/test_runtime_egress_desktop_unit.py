from __future__ import annotations

import ast
import inspect
import json
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from core_runner import CoreOutputEvent
from desktop_runtime import (
    DesktopRuntime,
    OriginDiagnosticBinding,
    RuntimeControllerReadbackError,
    _HotStandbyEvidence,
    _InitialEgressAdmissionLease,
    _RuntimeProbeBudgetUnavailable,
)
from runtime_egress_recovery import (
    BoundRuntimeFailure,
    CoreLoadedGroupState,
    CoreLoadedRouteSnapshot,
    RuntimeEgressCoordinator,
    RuntimeEgressReadinessState,
    RuntimeNodeRef,
    RuntimeRecoveryOutcome,
    RuntimeRecoveryStatus,
    RuntimeRouteBinding,
    runtime_transport_failure_from_log,
)
from runtime_reload_gate import (
    RuntimeReloadGate,
    RuntimeReloadReason,
    RuntimeReloadRequest,
)


class _Runner:
    def is_running(self) -> bool:
        return True

    def status(self) -> dict[str, int]:
        return {"pid": 4312, "process_generation": 7}


class _KillSwitch:
    def __init__(self) -> None:
        self.activate_calls = 0

    def activate(self) -> bool:
        self.activate_calls += 1
        return True


class _SelectorClient:
    def select_proxy(self, group_name: str, proxy_name: str) -> dict[str, str]:
        return {"group": group_name, "proxy": proxy_name}


def test_formal_no_capture_qualification_failure_never_reloads_core() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime.logger = logging.getLogger("test-no-capture-no-reload")
    runtime._current_core_runtime_phase = lambda: "formal_no_capture"
    runtime.restore_system_proxy = lambda: (_ for _ in ()).throw(
        AssertionError("no-capture rollback must not touch system proxy")
    )
    runtime.traffic_monitor = object()
    runtime._hot_reload_runtime_config_phase = lambda *_args, **_kwargs: (
        (_ for _ in ()).throw(
            AssertionError("same no-capture semantics must not reload Mihomo")
        )
    )

    runtime._rollback_to_formal_no_capture(_Runner(), "qualification failed")


def test_all_production_reload_core_config_callers_declare_a_reason() -> None:
    root = Path(__file__).resolve().parents[1]
    callers: list[tuple[str, int]] = []
    missing: list[tuple[str, int]] = []
    explicit_none: list[tuple[str, int]] = []

    # Production modules in this repository live at the project root.  Keep
    # test helpers and generated build trees out of this integration contract.
    for source_path in root.glob("*.py"):
        tree = ast.parse(
            source_path.read_text(encoding="utf-8"),
            filename=str(source_path),
        )
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "reload_core_config"
            ):
                continue
            location = (source_path.name, int(node.lineno))
            callers.append(location)
            reason_keywords = [
                keyword
                for keyword in node.keywords
                if keyword.arg == "reason"
            ]
            if not reason_keywords:
                missing.append(location)
            elif isinstance(reason_keywords[0].value, ast.Constant) and (
                reason_keywords[0].value.value is None
            ):
                explicit_none.append(location)

    assert callers, "the production reload boundary unexpectedly has no callers"
    assert missing == []
    assert explicit_none == []


def test_runtime_reload_reason_is_blocked_before_unchanged_shortcut() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._core_transition_lock = threading.RLock()
    runtime._runtime_reload_gate = RuntimeReloadGate("loaded")
    runtime._runtime_loaded_semantic_digest = lambda: "loaded"
    runtime._render_mihomo_runtime_payload_snapshot = lambda: "changed-payload"
    runtime._runtime_export_contract_for_payload = lambda _payload: {
        "semantic_digest": "changed"
    }
    runtime._reload_core_config_serialized = lambda **_kwargs: (
        (_ for _ in ()).throw(
            AssertionError("runtime hot paths must not reach Core apply")
        )
    )
    marker_calls: list[str] = []
    runtime._last_core_export_sync_marker = ("same",)
    runtime._core_export_sync_marker = lambda: (
        marker_calls.append("marker") or ("same",)
    )

    result = runtime.reload_core_config(
        mode="incremental_export",
        reason=RuntimeReloadReason.RUNTIME_FAILBACK,
    )

    assert result["reloaded"] is False
    assert result["reload_blocked"] is True
    assert result["reason"] == "runtime_hot_path_reload_forbidden"
    assert marker_calls == []


def test_loaded_contract_publication_defers_gate_digest_to_active_reload_lease() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_loaded_export_revision = 3
    runtime._runtime_loaded_export_contract = {
        "semantic_digest": "loaded",
        "revision": 3,
    }
    runtime._runtime_pending_export_contract = {
        "semantic_digest": "changed",
        "main_semantic_digest": "changed-main",
        "provider_semantic_digest": "changed-provider",
        "proxy_map": {},
        "runtime_config_path": "mihomo-runtime.yaml",
    }
    runtime._tier3_core_provider_last_projection = None
    runtime._tier3_core_provider_pending_projection = None
    gate = RuntimeReloadGate("loaded")
    runtime._runtime_reload_gate = gate
    decision = gate.request_reload(
        RuntimeReloadRequest(
            semantic_digest="changed",
            reason=RuntimeReloadReason.CAPTURE_LIFECYCLE,
        )
    )
    assert decision.lease is not None

    runtime._mark_loaded_runtime_export_contract()

    assert runtime._runtime_loaded_export_contract["semantic_digest"] == "changed"
    assert gate.loaded_semantic_digest == "loaded"
    assert gate.complete_reload(decision.lease, applied=True)
    assert gate.loaded_semantic_digest == "changed"


def test_blocked_user_reload_does_not_leave_a_deferred_gate_token() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._core_transition_lock = threading.RLock()
    runtime._core_lifecycle_lock = threading.RLock()
    runtime._core_runtime_phase = "formal_capture"
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_route_mutation_lease = None
    runtime._runtime_origin_diagnostic_bindings = {}
    runtime._runtime_selector_intent_revision = 0
    gate = RuntimeReloadGate("loaded")
    recovery = gate.begin_recovery("incident-active")
    assert recovery is not None
    runtime._runtime_reload_gate = gate
    runtime._runtime_loaded_semantic_digest = lambda: "loaded"
    runtime._render_mihomo_runtime_payload_snapshot = lambda: "changed-payload"
    runtime._runtime_export_contract_for_payload = lambda _payload: {
        "semantic_digest": "changed"
    }
    runtime._reload_core_config_serialized = lambda **_kwargs: (
        (_ for _ in ()).throw(
            AssertionError("a deferred user commit must not reach Core apply")
        )
    )

    result = runtime.reload_core_config(
        mode="strict",
        reason=RuntimeReloadReason.USER_GRAPH_CONFIG,
        user_commit_id="gateway-commit-a",
    )

    assert result["reloaded"] is False
    assert result["deferred"] is True
    assert result["reason"] == "user_commit_waiting_for_recovery"
    assert gate.snapshot().pending_user_commit_id == ""

    assert gate.end_recovery(recovery)
    later = gate.request_reload(
        RuntimeReloadRequest(
            semantic_digest="changed-later",
            reason=RuntimeReloadReason.USER_GRAPH_CONFIG,
            user_commit_id="gateway-commit-b",
        )
    )
    assert later.allowed and later.lease is not None
    assert gate.cancel_reload(later.lease)


def test_blocked_gateway_reload_never_touches_live_selectors() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._reload_after = lambda _action, *, target_tier: {
        "stored": True,
        "target_tier": target_tier,
    }
    runtime.core_runner = _Runner()
    runtime._core_transition_lock = threading.RLock()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_selector_intent_revision = 4
    runtime.manager = None
    runtime._runtime_route_mutation_lease = {
        "incident_id": "automatic-route",
    }
    reload_calls: list[dict[str, object]] = []

    def blocked_reload(**kwargs: object) -> dict[str, object]:
        reload_calls.append(dict(kwargs))
        return {
            "reloaded": False,
            "reload_blocked": True,
            "deferred": True,
            "reason": "recovery_transaction_active",
        }

    runtime.reload_core_config = blocked_reload
    selector_calls: list[tuple[str, str]] = []
    runtime.select_mihomo_proxy = (
        lambda group, target, **_kwargs: selector_calls.append((group, target))
    )

    result = runtime.set_exit_selected_proxy("fixed-gateway")

    assert result["stored"] is True
    assert result["applied"] is False
    assert result["apply_error"] == "recovery_transaction_active"
    assert selector_calls == []
    assert len(reload_calls) == 1
    assert reload_calls[0]["reason"] is RuntimeReloadReason.USER_GRAPH_CONFIG
    assert runtime._runtime_selector_intent_revision == 5
    assert runtime._runtime_route_mutation_lease is None


def test_legacy_runtime_audit_overlay_cannot_reload_or_restart_core() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime.restart_core = lambda: (_ for _ in ()).throw(
        AssertionError("runtime health must not restart Core")
    )
    runtime.core_runner = type(
        "ReloadForbiddenRunner",
        (),
        {
            "is_running": lambda self: True,
            "hot_reload": lambda self, **_kwargs: (
                (_ for _ in ()).throw(
                    AssertionError("runtime health must not hot-reload Core")
                )
            ),
        },
    )()

    result = runtime._hot_reload_runtime_audit_overlay()

    assert result == {
        "reloaded": False,
        "reload_blocked": True,
        "reason": "runtime health hot-path Core reload is forbidden",
    }


def test_formal_provider_failure_never_falls_back_to_core_reload() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime.logger = logging.getLogger("test-provider-no-reload-fallback")
    runtime.stop_event = threading.Event()
    runtime.running = True
    runtime.manager = object()
    runtime.core_runner = _Runner()
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_incident_recovery_state = {}
    runtime._current_core_runtime_phase = lambda: "formal_capture"
    runtime._tier3_core_sync_marker = lambda: ("marker",)
    provider_calls: list[tuple[object, ...]] = []
    runtime._update_running_tier3_core_provider = lambda marker: (
        provider_calls.append(marker)
        or {"updated": False, "reason": "provider readback failed"}
    )
    sync_failures: list[tuple[object, ...]] = []
    runtime._mark_tier3_core_sync_failure = lambda marker: sync_failures.append(
        marker
    )
    runtime.reload_core_config = lambda **_kwargs: (
        (_ for _ in ()).throw(
            AssertionError("provider failure must retain the loaded inventory")
        )
    )

    runtime._sync_core_config_after_pool_update(allow_live_rebase=True)

    assert provider_calls == [("marker",)]
    assert sync_failures == [("marker",)]


class _MutableRunner(_Runner):
    def __init__(self) -> None:
        self.running = True

    def is_running(self) -> bool:
        return self.running

    def status(self) -> dict[str, int]:
        return (
            {"pid": 4312, "process_generation": 7}
            if self.running
            else {"pid": 0, "process_generation": 7}
        )


class _FailClosedClient:
    def __init__(self, selected: dict[str, str], *, fail: bool = False) -> None:
        self.selected = selected
        self.fail = fail
        self.calls: list[tuple[str, str, float | None]] = []

    def select_proxy(
        self,
        group_name: str,
        proxy_name: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, str]:
        self.calls.append((group_name, proxy_name, timeout))
        if self.fail:
            raise RuntimeError("controller unavailable")
        self.selected["leaf"] = proxy_name
        return {"group": group_name, "selected": proxy_name}


def _blocking_route_refresh_runtime():
    runtime = object.__new__(DesktopRuntime)
    runtime.core_runner = _Runner()
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_route_snapshot_refresh_lock = threading.Lock()
    runtime._core_lifecycle_lock = threading.Lock()
    runtime._core_transition_lock = threading.RLock()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._core_generation = 3
    runtime._runtime_route_epoch = 9
    runtime._runtime_route_selection_signature = ()
    runtime._runtime_selector_intent_revision = 0
    runtime._runtime_route_mutation_lease = None
    runtime._runtime_loaded_export_contract = {
        "revision": 1,
        "semantic_digest": "loaded",
        "proxy_map": {
            "bad-leaf": {
                "tier": "tier1",
                "pool_kind": "tier1_variant",
                "identity": "bad-id",
            }
        },
    }
    stale = CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
    )
    runtime._runtime_route_snapshot = stale
    runtime._runtime_route_snapshot_payload = {"proxies": {}}
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_egress_coordinator.set_current_snapshot(stale)
    started = threading.Event()
    release = threading.Event()

    def blocked_proxies():
        started.set()
        assert release.wait(1.0)
        return {
            "proxies": {
                "FINAL-EXIT": {
                    "type": "Selector",
                    "now": "bad-leaf",
                    "all": ["bad-leaf", "REJECT"],
                },
                "bad-leaf": {"type": "Shadowsocks"},
            }
        }

    runtime.mihomo_proxies = blocked_proxies
    return runtime, started, release


def _physical_fail_closed_runtime(*, controller_fails: bool = False):
    runtime = object.__new__(DesktopRuntime)
    runner = _MutableRunner()
    selected = {"leaf": "bad-leaf"}
    client = _FailClosedClient(selected, fail=controller_fails)
    runtime.core_runner = runner
    runtime.kill_switch = _KillSwitch()
    runtime.stop_event = threading.Event()
    runtime._runtime_generation = 1
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_hot_standby_lock = threading.RLock()
    runtime._runtime_network_context_epoch = 0
    runtime._runtime_network_context_hold_active = False
    runtime._runtime_network_context_hold_pending_candidate = False
    runtime._runtime_network_context_hold_reason = ""
    runtime._runtime_network_context_hold_epoch = 0
    runtime._runtime_network_context_rebase_stage = 0
    runtime._runtime_network_context_rebase_at = 0.0
    runtime._runtime_route_snapshot_refresh_lock = threading.Lock()
    runtime._core_lifecycle_lock = threading.Lock()
    runtime._core_transition_lock = threading.RLock()
    runtime._core_generation = 3
    runtime._runtime_route_epoch = 9
    runtime._runtime_selector_intent_revision = 0
    runtime._runtime_route_selection_signature = ()
    runtime._runtime_loaded_export_contract = {
        "revision": 1,
        "semantic_digest": "loaded",
        "proxy_map": {
            "bad-leaf": {
                "tier": "tier1",
                "pool_kind": "tier1_variant",
                "identity": "bad-id",
            },
            "good-leaf": {
                "tier": "tier1",
                "pool_kind": "tier1_variant",
                "identity": "good-id",
            },
        },
    }
    runtime._runtime_route_snapshot = None
    runtime._runtime_route_snapshot_payload = None
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._require_api_client = lambda: client
    effective: list[tuple[object, str]] = []
    runtime._set_effective_egress_state = (
        lambda ok, reason="": effective.append((ok, str(reason)))
    )

    def proxies():
        return {
            "proxies": {
                "FINAL-EXIT": {
                    "type": "Selector",
                    "now": selected["leaf"],
                    "all": ["bad-leaf", "good-leaf", "REJECT"],
                },
                "bad-leaf": {"type": "Shadowsocks"},
                "good-leaf": {"type": "Shadowsocks"},
                "REJECT": {"type": "Reject"},
            }
        }

    runtime.mihomo_proxies = proxies
    stops: list[dict[str, object]] = []

    def stop_core(**kwargs):
        runner.running = False
        runtime._runtime_egress_coordinator.set_current_snapshot(None)
        result = {"stopped": True, **kwargs}
        stops.append(result)
        return result

    runtime.stop_core = stop_core
    return runtime, runner, client, selected, stops, effective


def _fixed_gateway_failure_fixture(
    *,
    route_epoch: int = 9,
) -> tuple[
    CoreLoadedRouteSnapshot,
    RuntimeRouteBinding,
    BoundRuntimeFailure,
]:
    ref = RuntimeNodeRef(
        tier="exit",
        pool_kind="gateway",
        identity="fixed-gateway-id",
        proxy_name="fixed-gateway",
    )
    snapshot = CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=route_epoch,
        proxy_nodes=(ref,),
    )
    binding = RuntimeRouteBinding(
        process_pid=snapshot.process_pid,
        process_generation=snapshot.process_generation,
        core_generation=snapshot.core_generation,
        semantic_digest=snapshot.semantic_digest,
        route_epoch=snapshot.route_epoch,
        observed_outbound="FINAL-EXIT",
        route_chain=("FINAL-EXIT", ref.proxy_name),
        node=ref,
    )
    failure = BoundRuntimeFailure(
        binding=binding,
        protocol="PROBE",
        observed_outbound="FINAL-EXIT",
        destination_token="fixed-gateway-probe",
        error_type="activation_final_exit",
        observed_at_monotonic=time.monotonic(),
        deterministic_proxy_endpoint=True,
    )
    return snapshot, binding, failure


def _failed_recovery_worker_result(
    *,
    audit_error: str,
    raises: bool = False,
    lease_unavailable: bool = False,
    binding_state_unavailable: bool = False,
    replacement_before_recovery: bool = False,
    replacement_ownership_lost: bool = False,
    probe_budget_indeterminate: bool = False,
):
    runtime, _runner, _client, selected, stops, _effective = (
        _physical_fail_closed_runtime()
    )
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_failure_recovery_bindings = set()
    runtime._runtime_failure_recovery_threads = set()
    runtime._runtime_incident_recovery_state = {}
    runtime._runtime_pending_recovered_payloads = {}
    runtime._runtime_pending_confirmed_incidents = {}
    runtime._runtime_origin_diagnostic_bindings = {}
    runtime._runtime_deferred_provisional_confirmations = {}
    runtime._runtime_route_mutation_lease = None
    runtime._runtime_selector_intent_revision = 2
    runtime._runtime_last_verified_route = None
    runtime._runtime_worker_token = lambda: (1,)
    runtime._runtime_worker_token_is_current = lambda _token: True
    runtime._activate_killswitch_for_egress_failure = lambda _reason: None
    runtime._apply_confirmed_runtime_health = (
        lambda *_args, **_kwargs: {"accepted": True, "changed": True}
    )
    runtime._start_runtime_connection_cleanup = lambda _binding: None
    runtime.manager = None
    runtime._set_effective_egress_readiness_state = (
        lambda ok, reason="": runtime._set_effective_egress_state(
            ok,
            reason,
        )
    )
    runtime._queue_runtime_route_snapshot_refresh = lambda: None
    runtime._submit_current_route_probe_failure = (
        lambda *_args, **_kwargs: False
    )
    published: list[dict[str, object]] = []
    runtime._set_runtime_audit_result = (
        lambda result, **_kwargs: published.append(dict(result)) or result
    )

    def failed_audit(**_kwargs):
        if raises:
            raise RuntimeError(audit_error)
        return {
            "post_switch_connectivity_verified": False,
            "effective_egress_ok": False,
            "reason": audit_error,
            "leaf_proxy": "good-leaf",
        }

    runtime.audit_runtime_egress = failed_audit
    snapshot = runtime._refresh_runtime_route_snapshot()
    assert snapshot is not None
    bad = runtime._runtime_snapshot_node(snapshot, "bad-leaf")
    good = runtime._runtime_snapshot_node(snapshot, "good-leaf")
    assert bad is not None
    assert good is not None
    binding = RuntimeRouteBinding(
        process_pid=snapshot.process_pid,
        process_generation=snapshot.process_generation,
        core_generation=snapshot.core_generation,
        semantic_digest=snapshot.semantic_digest,
        route_epoch=snapshot.route_epoch,
        observed_outbound="FINAL-EXIT",
        route_chain=("FINAL-EXIT", "bad-leaf"),
        node=bad,
    )
    failure = BoundRuntimeFailure(
        binding=binding,
        protocol="TCP",
        observed_outbound="FINAL-EXIT",
        destination_token="business-destination",
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
    if lease_unavailable:
        runtime._runtime_egress_coordinator.begin_core_mutation = (
            lambda *_args, **_kwargs: None
        )
    if binding_state_unavailable:
        runtime._current_runtime_failure_binding_state = (
            lambda **_kwargs: None
        )
    if replacement_before_recovery:
        selected["leaf"] = "good-leaf"

        def replacement_probe(**_kwargs):
            if replacement_ownership_lost:
                runtime._runtime_egress_coordinator.snapshot_ownership_matches = (
                    lambda *_args, **_kwargs: False
                )
            return {"status": 204, "latency_ms": 7}

        runtime._probe_mihomo_mixed_port_once = replacement_probe
    if probe_budget_indeterminate:
        runtime._fast_recover_confirmed_alternative = (
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                _RuntimeProbeBudgetUnavailable("probe slots busy")
            )
        )
        runtime._enforce_runtime_fail_closed = (
            lambda *_args, **_kwargs: pytest.fail(
                "probe capacity cannot be upgraded to REJECT/Core stop"
            )
        )
    runtime._start_runtime_recovery(
        failure,
        snapshot,
        runtime._runtime_route_snapshot_state()[1] or {},
        incident_id=confirmed.incident_id,
        quarantine_token=confirmed.probe_token,
        confirmed_alternative=good,
    )
    deadline = time.monotonic() + 1.0
    while runtime._runtime_failure_recovery_threads and time.monotonic() < deadline:
        time.sleep(0.005)
    assert runtime._runtime_failure_recovery_threads == set()
    return runtime, binding, selected, stops, published


def test_application_probe_window_suppresses_real_format_core_line() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_failure_monitor_lock = threading.Lock()
    runtime._runtime_internal_probe_windows = {}

    runtime._register_runtime_internal_probe(
        "https://cp.cloudflare.com/generate_204",
        timeout_seconds=1.5,
    )
    parsed = runtime_transport_failure_from_log(
        "[TCP] dial GENERAL (match Match/) 127.0.0.1:51001 "
        "--> cp.cloudflare.com:443 error: context deadline exceeded",
        observed_at_monotonic=time.monotonic(),
    )
    unrelated = runtime_transport_failure_from_log(
        "[TCP] dial GENERAL (match Match/) 127.0.0.1:51002 "
        "--> example.net:443 error: context deadline exceeded",
        observed_at_monotonic=time.monotonic(),
    )

    assert parsed is not None
    assert unrelated is not None
    assert runtime._runtime_transport_failure_is_internal_probe(parsed) is True
    assert runtime._runtime_transport_failure_is_internal_probe(unrelated) is False


def test_route_snapshot_rejects_mixed_loaded_contract_revision() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime.core_runner = _Runner()
    runtime._runtime_route_snapshot_lock = threading.Lock()
    runtime._runtime_route_snapshot_refresh_lock = threading.Lock()
    runtime._runtime_route_snapshot = None
    runtime._runtime_route_snapshot_payload = None
    runtime._runtime_route_epoch = 0
    runtime._runtime_route_selection_signature = ()
    runtime._runtime_loaded_export_contract = {
        "revision": 1,
        "semantic_digest": "old",
        "proxy_map": {},
    }
    runtime._core_lifecycle_lock = threading.Lock()
    runtime._core_generation = 3
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()

    def concurrent_controller_read(*, timeout_seconds=None):
        del timeout_seconds
        with runtime._runtime_route_snapshot_lock:
            runtime._runtime_loaded_export_contract = {
                "revision": 2,
                "semantic_digest": "new",
                "proxy_map": {},
            }
        return {"proxies": {"FINAL-EXIT": {"all": ["REJECT"], "now": "REJECT"}}}

    runtime.mihomo_proxies = concurrent_controller_read

    assert runtime._refresh_runtime_route_snapshot() is None
    assert runtime._runtime_egress_coordinator.current_route_snapshot() is None


def test_blocked_route_refresh_cannot_republish_after_manual_selector() -> None:
    runtime, started, release = _blocking_route_refresh_runtime()
    runtime._begin_core_route_mutation = lambda _reason: None
    runtime._require_api_client = lambda: _SelectorClient()
    runtime._queue_runtime_route_snapshot_refresh = lambda: None
    runtime._queue_incremental_egress_recheck = lambda: None
    results: list[CoreLoadedRouteSnapshot | None] = []
    worker = threading.Thread(
        target=lambda: results.append(runtime._refresh_runtime_route_snapshot())
    )
    worker.start()
    assert started.wait(0.5)

    runtime.select_mihomo_proxy("FINAL-EXIT", "other-leaf")
    assert runtime._runtime_egress_coordinator.current_route_snapshot() is None
    release.set()
    worker.join(1.0)

    assert not worker.is_alive()
    assert results == [None]
    assert runtime._runtime_route_snapshot is None
    assert runtime._runtime_egress_coordinator.current_route_snapshot() is None


def test_blocked_route_refresh_cannot_republish_after_core_authority_revoke() -> None:
    runtime, started, release = _blocking_route_refresh_runtime()
    runtime._set_core_runtime_phase = lambda _phase: None
    runtime._tier3_egress_activation_lock = threading.Lock()
    runtime._tier3_egress_activation_pending = False
    runtime._tier3_egress_activation_pending_mode = ""
    runtime._tier3_egress_activation_generation = 1
    runtime.manager = None
    runtime._set_effective_egress_state = lambda *_args: None
    results: list[CoreLoadedRouteSnapshot | None] = []
    worker = threading.Thread(
        target=lambda: results.append(runtime._refresh_runtime_route_snapshot())
    )
    worker.start()
    assert started.wait(0.5)

    runtime._revoke_core_provider_authority("test stop")
    assert runtime._runtime_egress_coordinator.current_route_snapshot() is None
    release.set()
    worker.join(1.0)

    assert not worker.is_alive()
    assert results == [None]
    assert runtime._runtime_route_snapshot is None
    assert runtime._runtime_egress_coordinator.current_route_snapshot() is None


def test_legacy_partial_fail_closed_without_gateway_binding_is_blocked() -> None:
    runtime, runner, client, selected, stops, effective = (
        _physical_fail_closed_runtime()
    )

    result = runtime._enforce_runtime_fail_closed(
        "legacy partial runtime failure",
        deadline_monotonic=time.monotonic() + 1.0,
    )

    assert result["attempted"] is False
    assert result["blocked"] is True
    assert result["enforced"] is False
    assert client.calls == []
    assert selected["leaf"] == "bad-leaf"
    assert stops == []
    assert effective == []
    assert runner.running is True


def test_fixed_gateway_fail_closed_selects_and_verifies_loaded_reject_route() -> None:
    runtime, runner, client, selected, stops, effective = (
        _physical_fail_closed_runtime()
    )
    _snapshot, binding, _failure = _fixed_gateway_failure_fixture()

    result = runtime._enforce_runtime_fail_closed(
        "confirmed replacement failed",
        failure_binding=binding,
        gateway_failure_confirmed=True,
        deadline_monotonic=time.monotonic() + 1.0,
    )

    assert result["enforced"] is True
    assert result["selected_reject"] is True
    assert result["route_verified"] is True
    assert result["core_stopped"] is False
    assert runner.running is True
    assert selected["leaf"] == "REJECT"
    assert client.calls[0][0:2] == ("FINAL-EXIT", "REJECT")
    assert 0.0 < float(client.calls[0][2] or 0.0) <= 0.35
    assert stops == []
    assert effective[-1][0] is False


def test_emergency_fail_closed_stops_core_when_controller_cannot_select() -> None:
    runtime, runner, client, _selected, stops, effective = (
        _physical_fail_closed_runtime(controller_fails=True)
    )
    _snapshot, binding, _failure = _fixed_gateway_failure_fixture()

    result = runtime._enforce_runtime_fail_closed(
        "controller facility failure",
        failure_binding=binding,
        gateway_failure_confirmed=True,
        deadline_monotonic=time.monotonic() + 1.0,
    )

    assert result["enforced"] is True
    assert result["route_verified"] is False
    assert result["core_stopped"] is True
    assert runner.running is False
    assert client.calls[0][0:2] == ("FINAL-EXIT", "REJECT")
    assert len(stops) == 1
    assert effective[-1][0] is False


def test_failed_non_gateway_recovery_preserves_route_and_quarantines_leaf() -> None:
    cases = (
        ("Runtime failover reload failed", False),
        ("runtime hot reload raised", True),
        ("replacement route failed 204 verification", False),
    )
    for reason, raises in cases:
        runtime, binding, selected, stops, published = (
            _failed_recovery_worker_result(
                audit_error=reason,
                raises=raises,
            )
        )
        assert selected["leaf"] != "REJECT", reason
        assert stops == [], reason
        assert runtime._runtime_route_mutation_lease is None, reason
        assert published[-1]["physical_route_preserved"] is True, reason
        assert published[-1]["effective_egress_ok"] is None, reason
        circuit = runtime._runtime_egress_coordinator.current_snapshot_for(binding)
        assert circuit.state == "quarantined", reason


def test_non_gateway_recovery_lease_contention_preserves_current_route() -> None:
    runtime, binding, selected, stops, published = (
        _failed_recovery_worker_result(
            audit_error="audit must not run after lease contention",
            lease_unavailable=True,
        )
    )

    assert selected["leaf"] == "bad-leaf"
    assert stops == []
    assert published == []
    circuit = runtime._runtime_egress_coordinator.current_snapshot_for(binding)
    assert circuit is not None
    assert circuit.state == "quarantined"


def test_non_gateway_lease_contention_without_snapshot_preserves_current_route() -> None:
    runtime, binding, selected, stops, published = (
        _failed_recovery_worker_result(
            audit_error="audit must not run after unavailable binding",
            lease_unavailable=True,
            binding_state_unavailable=True,
        )
    )

    assert selected["leaf"] == "bad-leaf"
    assert stops == []
    assert published == []
    circuit = runtime._runtime_egress_coordinator.current_snapshot_for(binding)
    assert circuit is not None
    assert circuit.state == "quarantined"


def test_core_fallback_during_lease_contention_keeps_bad_leaf_quarantined() -> None:
    runtime, binding, selected, stops, _published = (
        _failed_recovery_worker_result(
            audit_error="audit must not run after Core fallback",
            lease_unavailable=True,
            replacement_before_recovery=True,
        )
    )

    assert selected["leaf"] == "good-leaf"
    assert stops == []
    assert runtime._runtime_route_mutation_lease["incident_id"]
    assert runtime._runtime_route_mutation_lease["from"] == "bad-leaf"
    assert runtime._runtime_route_mutation_lease["to"] == "good-leaf"
    circuit = runtime._runtime_egress_coordinator.current_snapshot_for(binding)
    assert circuit is not None
    assert circuit.state == "quarantined"


def test_core_fallback_cas_loss_after_204_never_publishes_unowned_route_ready() -> None:
    runtime, binding, selected, stops, published = (
        _failed_recovery_worker_result(
            audit_error="audit must not run after replacement ownership loss",
            lease_unavailable=True,
            replacement_before_recovery=True,
            replacement_ownership_lost=True,
        )
    )

    # A newer route already selected and verified the healthy leaf, but this
    # callback lost publication authority.  It must leave the physical route
    # alone and keep the failed origin quarantined; forcing REJECT here would
    # let a stale incident interrupt healthy new and existing connections.
    assert selected["leaf"] == "good-leaf"
    assert stops == []
    assert runtime._runtime_last_verified_route is None
    assert runtime._runtime_route_mutation_lease is None
    assert published == []
    assert set(runtime._runtime_incident_recovery_state.values()) == {
        "indeterminate"
    }
    circuit = runtime._runtime_egress_coordinator.current_snapshot_for(binding)
    assert circuit is not None
    assert circuit.state == "quarantined"


def test_confirmed_alternative_probe_budget_never_fail_closes() -> None:
    runtime, binding, selected, stops, published = (
        _failed_recovery_worker_result(
            audit_error="probe budget must stay indeterminate",
            probe_budget_indeterminate=True,
        )
    )

    assert selected["leaf"] == "bad-leaf"
    assert stops == []
    assert published == []
    assert runtime._runtime_route_mutation_lease is None
    assert set(runtime._runtime_incident_recovery_state.values()) == {
        "indeterminate"
    }
    circuit = runtime._runtime_egress_coordinator.current_snapshot_for(binding)
    assert circuit is not None
    assert circuit.state == "quarantined"


def test_facility_degradation_holds_managed_route_without_stopping_core() -> None:
    runtime, runner, _client, _selected, stops, _effective = (
        _physical_fail_closed_runtime(controller_fails=True)
    )
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_failure_recovery_threads = set()
    runtime._runtime_facility_fail_closed_running = False
    runtime._runtime_facility_fail_closed_pending = None
    runtime.manager = None
    runtime.last_runtime_route_audit = None
    runtime._set_runtime_audit_result = (
        lambda result, **_kwargs: setattr(
            runtime, "last_runtime_route_audit", dict(result)
        )
        or result
    )

    runtime._record_runtime_facility_degradation(
        "mihomo controller /proxies failed"
    )
    deadline = time.monotonic() + 1.0
    while runtime._runtime_failure_recovery_threads and time.monotonic() < deadline:
        time.sleep(0.005)

    assert runtime._runtime_failure_recovery_threads == set()
    assert runner.running is True
    assert stops == []
    assert runtime.last_runtime_route_audit["facility_hold"] is True
    assert runtime.last_runtime_route_audit["effective_egress_ok"] is None


def _prepare_facility_worker_runtime(*, controller_fails: bool = False):
    runtime, runner, client, selected, stops, effective = (
        _physical_fail_closed_runtime(controller_fails=controller_fails)
    )
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_failure_recovery_threads = set()
    runtime._runtime_facility_fail_closed_running = False
    runtime._runtime_facility_fail_closed_pending = None
    runtime.manager = None
    runtime.last_runtime_route_audit = {
        "reason": "facility failure",
        "trigger": "runtime_facility",
    }
    runtime._set_runtime_audit_result = (
        lambda result: setattr(runtime, "last_runtime_route_audit", dict(result))
        or result
    )
    return runtime, runner, client, selected, stops, effective


def test_unbound_single_log_needs_neutral_failure_before_facility_degradation() -> None:
    runtime, runner, client, selected, stops, effective = (
        _prepare_facility_worker_runtime()
    )
    runtime._runtime_facility_confirmation_tokens = set()
    runtime._probe_mihomo_mixed_port_once = (
        lambda **_kwargs: {"status": 204, "latency_ms": 5}
    )
    facility: list[str] = []
    runtime._record_runtime_facility_degradation = (
        lambda reason: facility.append(str(reason)) or {}
    )

    runtime._start_runtime_facility_confirmation(
        "unbound single business transport failure"
    )

    deadline = time.monotonic() + 1.0
    while runtime._runtime_failure_recovery_threads and time.monotonic() < deadline:
        time.sleep(0.005)
    assert runtime._runtime_failure_recovery_threads == set()
    assert facility == []
    assert runner.running is True
    assert client.calls == []
    assert selected["leaf"] == "bad-leaf"
    assert stops == []
    assert effective == []


def test_unbound_signal_becomes_facility_only_after_neutral_failure() -> None:
    runtime, _runner, _client, _selected, _stops, _effective = (
        _prepare_facility_worker_runtime()
    )
    runtime._runtime_facility_confirmation_tokens = set()
    runtime._probe_mihomo_mixed_port_once = (
        lambda **_kwargs: (_ for _ in ()).throw(
            TimeoutError("neutral timeout")
        )
    )
    facility: list[str] = []
    runtime._record_runtime_facility_degradation = (
        lambda reason: facility.append(str(reason)) or {}
    )

    runtime._start_runtime_facility_confirmation(
        "unbound single business transport failure"
    )

    deadline = time.monotonic() + 1.0
    while runtime._runtime_failure_recovery_threads and time.monotonic() < deadline:
        time.sleep(0.005)
    assert runtime._runtime_failure_recovery_threads == set()
    assert len(facility) == 1
    assert "neutral FINAL-EXIT confirmation failed" in facility[0]


def test_stale_facility_worker_cannot_close_restarted_core() -> None:
    runtime, runner, client, selected, stops, effective = (
        _prepare_facility_worker_runtime()
    )
    runtime._core_transition_lock.acquire()
    try:
        runtime._start_runtime_facility_fail_closed("old facility incident")
        deadline = time.monotonic() + 0.5
        while (
            runtime._runtime_facility_fail_closed_pending is not None
            and time.monotonic() < deadline
        ):
            time.sleep(0.005)
        runtime._runtime_generation += 1
        runtime._core_generation += 1
    finally:
        runtime._core_transition_lock.release()

    deadline = time.monotonic() + 1.0
    while runtime._runtime_failure_recovery_threads and time.monotonic() < deadline:
        time.sleep(0.005)

    assert runtime._runtime_failure_recovery_threads == set()
    assert runner.running is True
    assert client.calls == []
    assert selected["leaf"] == "bad-leaf"
    assert stops == []
    assert effective == []


def test_facility_mailbox_never_upgrades_unbound_event_to_physical_fail_closed() -> None:
    runtime, runner, client, selected, stops, _effective = (
        _prepare_facility_worker_runtime()
    )
    runtime._core_transition_lock.acquire()
    try:
        runtime._start_runtime_facility_fail_closed("old facility incident")
        deadline = time.monotonic() + 0.5
        while (
            runtime._runtime_facility_fail_closed_pending is not None
            and time.monotonic() < deadline
        ):
            time.sleep(0.005)
        runtime._runtime_generation += 1
        runtime._core_generation += 1
        runtime._start_runtime_facility_fail_closed("new facility incident")
    finally:
        runtime._core_transition_lock.release()

    deadline = time.monotonic() + 1.0
    while runtime._runtime_failure_recovery_threads and time.monotonic() < deadline:
        time.sleep(0.005)

    assert runtime._runtime_failure_recovery_threads == set()
    assert runner.running is True
    assert client.calls == []
    assert selected["leaf"] == "bad-leaf"
    assert stops == []
    assert runtime.last_runtime_route_audit["fail_closed_terminal"] is False
    assert runtime.last_runtime_route_audit["fail_closed_apply"]["attempted"] is False
    assert runtime.last_runtime_route_audit["fail_closed_apply"]["blocked"] is True


def test_runtime_failure_authority_has_no_controller_log_reader() -> None:
    monitor_source = inspect.getsource(DesktopRuntime._runtime_failure_monitor_loop)
    tier3_start_source = inspect.getsource(
        DesktopRuntime._start_tier3_runtime_failure_monitor
    )
    tier3_loop_source = inspect.getsource(
        DesktopRuntime._tier3_runtime_failure_monitor_loop
    )

    assert "stream_logs" not in monitor_source
    assert "_start_runtime_failure_monitor()" in tier3_start_source
    assert "_runtime_failure_monitor_loop()" in tier3_loop_source


def test_runtime_audit_hard_deadline_stops_before_controller_io() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime.stop_event = threading.Event()
    runtime.manager = None
    runtime._tier3_required_for_start = False
    runtime._runtime_route_audit_lock = threading.Lock()
    runtime._runtime_route_audit_cooldowns = {}
    runtime._core_lifecycle_lock = threading.Lock()
    runtime.mihomo_proxies = lambda: (_ for _ in ()).throw(
        AssertionError("controller I/O ran after deadline")
    )

    result = runtime.audit_runtime_egress(
        confirmed_transport_failure=True,
        deadline_monotonic=time.monotonic() - 0.01,
    )

    assert result["deadline_exceeded"] is True
    assert result["effective_egress_ok"] is False


def test_connection_cleanup_is_a_noop_without_exact_terminal_flow_ids() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_failure_recovery_threads = set()
    runtime._runtime_worker_token = lambda: (1, 2, 3, 4, 5)
    runtime._runtime_worker_token_is_current = lambda token: tuple(token) == (
        1,
        2,
        3,
        4,
        5,
    )
    called: list[object] = []

    def blocked_cleanup(_binding, **_kwargs):
        called.append(_kwargs)
        return 1

    runtime._close_runtime_connections_for_binding = blocked_cleanup
    ref = RuntimeNodeRef("tier2", "tier2", "bad-id", "bad-leaf")
    binding = RuntimeRouteBinding(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        observed_outbound="FINAL-EXIT",
        route_chain=("FINAL-EXIT", "bad-leaf"),
        node=ref,
    )

    runtime._start_runtime_connection_cleanup(binding)

    assert called == []
    assert runtime._runtime_failure_recovery_threads == set()


def test_connection_cleanup_never_closes_healthy_connection_on_same_leaf() -> None:
    runtime = object.__new__(DesktopRuntime)
    closed: list[str] = []
    runtime.mihomo_connections = lambda **_kwargs: {
        "connections": [
            {
                "id": "terminal-flow",
                "chains": ["bad-leaf", "FINAL-EXIT"],
            },
            {
                "id": "healthy-download",
                "chains": ["bad-leaf", "FINAL-EXIT"],
            },
        ]
    }
    runtime.close_mihomo_connection = (
        lambda connection_id, **_kwargs: closed.append(connection_id)
    )
    binding = RuntimeRouteBinding(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        observed_outbound="FINAL-EXIT",
        route_chain=("FINAL-EXIT", "bad-leaf"),
        node=RuntimeNodeRef("tier2", "tier2", "bad-id", "bad-leaf"),
    )

    count = runtime._close_runtime_connections_for_binding(
        binding,
        terminal_connection_ids={"terminal-flow"},
    )

    assert count == 1
    assert closed == ["terminal-flow"]


def test_connection_cleanup_stops_when_core_generation_changes_after_get() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_failure_recovery_threads = set()
    current_token = {"value": (1, 2, 3, 4, 5)}
    runtime._runtime_worker_token = lambda: current_token["value"]
    runtime._runtime_worker_token_is_current = (
        lambda token: tuple(token) == current_token["value"]
    )
    started = threading.Event()
    release = threading.Event()
    closed: list[str] = []

    def connections(**_kwargs):
        started.set()
        assert release.wait(1.0)
        return {
            "connections": [
                {
                    "id": "new-core-connection",
                    "chains": ["bad-leaf", "FINAL-EXIT"],
                }
            ]
        }

    runtime.mihomo_connections = connections
    runtime.close_mihomo_connection = (
        lambda connection_id, **_kwargs: closed.append(connection_id)
    )
    binding = RuntimeRouteBinding(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        observed_outbound="FINAL-EXIT",
        route_chain=("FINAL-EXIT", "bad-leaf"),
        node=RuntimeNodeRef("tier2", "tier2", "bad-id", "bad-leaf"),
    )

    runtime._start_runtime_connection_cleanup(
        binding,
        terminal_connection_ids={"new-core-connection"},
    )
    assert started.wait(0.5)
    current_token["value"] = (2, 3, 4, 5, 6)
    release.set()
    deadline = time.monotonic() + 1.0
    while runtime._runtime_failure_recovery_threads and time.monotonic() < deadline:
        time.sleep(0.005)

    assert runtime._runtime_failure_recovery_threads == set()
    assert closed == []


def test_matching_pass_restarts_fail_closed_core_and_reenters_failback() -> None:
    runtime, runner, _client, _selected, _stops, _effective = (
        _physical_fail_closed_runtime()
    )
    runner.running = False
    runtime.running = True
    runtime.manager = object()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_failure_recovery_bindings = set()
    runtime._runtime_failure_recovery_threads = set()
    runtime._runtime_failback_failures = {}
    runtime._runtime_route_mutation_lease = None
    runtime._runtime_core_stopped_recovery_owners = {
        "incident-pass": {
            "incident_id": "incident-pass",
            "from": "bad-leaf",
            "selector_intent_revision": 0,
            "fail_closed_at_monotonic": time.monotonic(),
        }
    }
    start_calls: list[str] = []
    reentered = threading.Event()

    def start_core():
        start_calls.append("start")
        runner.running = True
        runtime._core_generation += 1
        return {"running": True, "pid": 4312}

    runtime._start_core_for_runtime_recovery = start_core
    runtime._verify_runtime_final_exit = lambda **_kwargs: {"status": 204}
    runtime.logger = SimpleNamespace(
        warning=lambda *_args, **_kwargs: None,
    )
    runtime._start_runtime_failback = (
        lambda _payload, _ref: reentered.set()
    )
    ref = RuntimeNodeRef("tier1", "tier1_variant", "bad-id", "bad-leaf")
    payload = {"incident_id": "incident-pass"}

    runtime._start_runtime_core_reconcile_for_failback(payload, ref)

    assert reentered.wait(1.0)
    deadline = time.monotonic() + 1.0
    while runtime._runtime_failure_recovery_threads and time.monotonic() < deadline:
        time.sleep(0.005)
    assert runtime._runtime_failure_recovery_threads == set()
    assert start_calls == ["start"]
    assert runner.running is True
    assert runtime._runtime_route_mutation_lease["incident_id"] == "incident-pass"
    assert runtime._runtime_core_stopped_recovery_owners == {}


def test_matching_pass_restarts_core_owned_by_verified_reject_lease() -> None:
    runtime, runner, _client, _selected, _stops, _effective = (
        _physical_fail_closed_runtime()
    )
    runner.running = False
    runtime.running = True
    runtime.manager = object()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_failure_recovery_bindings = set()
    runtime._runtime_failure_recovery_threads = set()
    runtime._runtime_failback_failures = {}
    runtime._runtime_facility_epoch = 0
    runtime._runtime_route_mutation_lease = {
        "incident_id": "incident-reject",
        "from": "bad-leaf",
        "to": "REJECT",
        "owner_token": "verified-reject-owner",
        "selector_intent_revision": 0,
        "verified_at_monotonic": time.monotonic(),
    }
    runtime._runtime_core_stopped_recovery_owners = {}
    start_calls: list[str] = []
    reentered = threading.Event()

    def start_core():
        start_calls.append("start")
        runner.running = True
        runtime._core_generation += 1
        return {"running": True, "pid": 4312}

    runtime._start_core_for_runtime_recovery = start_core
    runtime._verify_runtime_final_exit = lambda **_kwargs: {"status": 204}
    runtime.logger = SimpleNamespace(
        warning=lambda *_args, **_kwargs: None,
    )
    runtime._start_runtime_failback = (
        lambda _payload, _ref: reentered.set()
    )
    ref = RuntimeNodeRef("tier1", "tier1_variant", "bad-id", "bad-leaf")

    runtime._start_runtime_core_reconcile_for_failback(
        {"incident_id": "incident-reject"},
        ref,
    )

    assert reentered.wait(1.0)
    deadline = time.monotonic() + 1.0
    while runtime._runtime_failure_recovery_threads and time.monotonic() < deadline:
        time.sleep(0.005)
    assert start_calls == ["start"]
    assert runtime._runtime_route_mutation_lease["incident_id"] == "incident-reject"
    assert runtime._runtime_route_mutation_lease["to"] == "bad-leaf"


def test_core_reconcile_old_204_cannot_publish_across_facility_epoch() -> None:
    runtime, runner, _client, _selected, _stops, effective = (
        _physical_fail_closed_runtime()
    )
    runner.running = False
    runtime.running = True
    runtime.manager = object()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_failure_recovery_bindings = set()
    runtime._runtime_failure_recovery_threads = set()
    runtime._runtime_failback_failures = {}
    runtime._runtime_facility_epoch = 4
    runtime._runtime_route_mutation_lease = None
    runtime._runtime_core_stopped_recovery_owners = {
        "incident-race": {
            "incident_id": "incident-race",
            "from": "bad-leaf",
            "selector_intent_revision": 0,
            "fail_closed_at_monotonic": time.monotonic(),
        }
    }
    reentered = threading.Event()

    def start_core():
        runner.running = True
        runtime._core_generation += 1
        return {"running": True, "pid": 4312}

    def verify(**_kwargs):
        runtime._runtime_facility_epoch += 1
        return {"status": 204}

    runtime._start_core_for_runtime_recovery = start_core
    runtime._verify_runtime_final_exit = verify
    runtime.logger = SimpleNamespace(
        warning=lambda *_args, **_kwargs: None,
    )
    runtime._start_runtime_failback = (
        lambda _payload, _ref: reentered.set()
    )
    ref = RuntimeNodeRef("tier1", "tier1_variant", "bad-id", "bad-leaf")

    runtime._start_runtime_core_reconcile_for_failback(
        {"incident_id": "incident-race"},
        ref,
    )

    assert reentered.wait(1.0)
    deadline = time.monotonic() + 1.0
    while runtime._runtime_failure_recovery_threads and time.monotonic() < deadline:
        time.sleep(0.005)
    assert runtime._runtime_route_mutation_lease is None
    assert "incident-race" in runtime._runtime_core_stopped_recovery_owners
    assert not any(value is True for value, _reason in effective)


def test_matching_pass_holds_exact_leaf_during_failback_stability_window() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime.stop_event = threading.Event()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_recheck_retry_attempts = {}
    runtime._runtime_incident_recovery_state = {"incident-pass": "ready"}
    runtime._runtime_pending_recovered_payloads = {}
    runtime._runtime_failback_export_holds = set()
    ref = RuntimeNodeRef(
        "tier1",
        "tier1_variant",
        "tier1|vless|edge.example|443|uuid|tls",
        "edge-vless",
    )
    binding = RuntimeRouteBinding(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        observed_outbound="FINAL-EXIT",
        route_chain=("FINAL-EXIT", "TIER1-AUTO", "edge-vless"),
        node=ref,
    )
    runtime._runtime_incident_bindings = {"incident-pass": binding}
    runtime._runtime_incident_route_payloads = {"incident-pass": {}}
    cleared: list[str] = []
    started: list[tuple[str, tuple[str, str, str]]] = []
    runtime._clear_runtime_audit_export_identity = cleared.append

    def start_failback(payload, recovered_ref):
        hold_key = runtime._runtime_leaf_export_hold_key(recovered_ref)
        assert hold_key not in runtime._runtime_failback_export_holds
        started.append((str(payload["incident_id"]), hold_key))

    runtime._start_runtime_failback = start_failback
    payload = {
        "incident_id": "incident-pass",
        "tier": ref.tier,
        "pool_kind": ref.pool_kind,
        "identity": ref.identity,
        "proxy_name": ref.proxy_name,
        "transition": {"action": "recovered"},
    }

    runtime._on_runtime_leaf_recheck_result(payload)

    hold_key = runtime._runtime_leaf_export_hold_key(ref)
    assert started == [("incident-pass", hold_key)]
    assert runtime._runtime_failback_export_holds == set()
    assert cleared == [ref.identity]


def test_early_profile_matching_pass_releases_once_after_verified_restoration() -> None:
    from tests.test_runtime_passive_failback import event, runtime as fixture, wait_done

    case = fixture.__wrapped__()
    runtime = next(case)
    runtime._start_runtime_failback = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("the passive profile must not start legacy standby failback")
    )
    payload = event(runtime.test.a, check_run_id="one-pass")
    try:
        runtime._on_runtime_leaf_recheck_result(payload)
        runtime._on_runtime_leaf_recheck_result(payload)
        wait_done(runtime)
        assert runtime.test.calls.count("DELETE") == 1
        assert runtime.test.calls.count("204") == 1
        assert runtime._runtime_route_mutation_lease is None
    finally:
        next(case, None)


def test_early_profile_starts_only_passive_runtime_failure_monitor() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_egress_recovery_profile = "early_single_coordinator"
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_failure_monitor_thread = None
    runtime._runtime_route_snapshot = None
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator(
        automatic_guard_enabled=False,
    )
    monitor_runs: list[str] = []
    runtime._runtime_failure_monitor_loop = lambda: monitor_runs.append("run")
    snapshots: list[str] = []
    hot_standby: list[str] = []
    runtime._queue_runtime_route_snapshot_refresh = (
        lambda: snapshots.append("snapshot")
    )
    runtime._start_runtime_hot_standby_maintenance = (
        lambda: hot_standby.append("started")
    )

    runtime._start_runtime_failure_monitor()
    thread = runtime._runtime_failure_monitor_thread
    assert thread is not None
    thread.join(1.0)
    assert monitor_runs == ["run"]
    runtime._runtime_recovery_is_armed = lambda *_args, **_kwargs: (
        (_ for _ in ()).throw(
            AssertionError("guard heartbeat must return before arm checks")
        )
    )
    runtime._maybe_probe_runtime_failure_guard()

    assert snapshots == []
    assert hot_standby == []


def test_failback_promotion_gate_defers_provider_update_until_lease_release() -> None:
    class ImmediateStopEvent:
        def __init__(self) -> None:
            self.stopped = False

        def wait(self, _seconds: float) -> bool:
            return self.stopped

        def is_set(self) -> bool:
            return self.stopped

        def set(self) -> None:
            self.stopped = True

    mutation_entered = threading.Event()
    release_mutation = threading.Event()

    class BlockingCoordinator:
        @staticmethod
        def begin_core_mutation(*_args, **_kwargs):
            mutation_entered.set()
            release_mutation.wait(1.0)
            return None

    runtime = object.__new__(DesktopRuntime)
    runtime.stop_event = ImmediateStopEvent()
    runtime.running = True
    runtime.core_runner = _Runner()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_failure_recovery_bindings = set()
    runtime._runtime_failure_recovery_threads = set()
    runtime._runtime_selector_intent_revision = 0
    runtime._runtime_worker_token = lambda: (1,)
    runtime._runtime_worker_token_is_current = lambda _token: True
    runtime._runtime_failback_last_attempt = {}
    runtime._runtime_failback_failures = {}
    runtime._runtime_flap_history = {}
    runtime._current_core_runtime_phase = lambda: "formal_capture"
    runtime._revalidation_capture_active = lambda _phase: True

    recovered = RuntimeNodeRef(
        "tier1",
        "tier1_variant",
        "recovered-id",
        "recovered-leaf",
    )
    alternative = RuntimeNodeRef(
        "tier2",
        "tier2",
        "alternative-id",
        "alternative-leaf",
    )
    snapshot = CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        proxy_nodes=(recovered, alternative),
    )
    incident_binding = RuntimeRouteBinding(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        observed_outbound="FINAL-EXIT",
        route_chain=("FINAL-EXIT", "TIER1-AUTO", "recovered-leaf"),
        node=recovered,
    )
    current_binding = RuntimeRouteBinding(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        observed_outbound="FINAL-EXIT",
        route_chain=("FINAL-EXIT", "TIER2-AUTO", "alternative-leaf"),
        node=alternative,
    )
    now = time.monotonic()
    runtime._runtime_incident_bindings = {"incident-pass": incident_binding}
    runtime._runtime_incident_route_payloads = {
        "incident-pass": {"proxies": {}}
    }
    runtime._runtime_failback_pass_history = {recovered.key: [now]}
    runtime._runtime_route_mutation_lease = {
        "incident_id": "incident-pass",
        "selector_intent_revision": 0,
        "verified_at_monotonic": now,
        "group": "TIER2-AUTO",
        "from": "recovered-leaf",
        "to": "alternative-leaf",
        "mode": "fixed",
    }
    runtime._runtime_route_snapshot_state = lambda: (
        snapshot,
        {"proxies": {}},
    )
    runtime._runtime_route_binding_for_outbound = (
        lambda *_args, **_kwargs: current_binding
    )
    runtime._runtime_egress_coordinator = BlockingCoordinator()
    gate = RuntimeReloadGate("loaded")
    runtime._runtime_reload_gate = gate
    runtime._update_running_tier3_core_provider_leased = (
        lambda _marker: (_ for _ in ()).throw(
            AssertionError("provider mutation must defer behind failback")
        )
    )

    runtime._start_runtime_failback(
        {"incident_id": "incident-pass"},
        recovered,
    )

    assert mutation_entered.wait(0.5)
    assert (
        gate.snapshot().active_recovery_transaction_id
        == "failback:incident-pass"
    )
    provider_result = runtime._update_running_tier3_core_provider(("marker",))
    assert provider_result["updated"] is False
    assert provider_result["deferred"] is True
    assert "another Core recovery" in provider_result["reason"]

    runtime.stop_event.set()
    release_mutation.set()
    deadline = time.monotonic() + 1.0
    while runtime._runtime_failure_recovery_threads and time.monotonic() < deadline:
        time.sleep(0.005)

    assert runtime._runtime_failure_recovery_threads == set()
    assert gate.snapshot().active_recovery_transaction_id == ""


def test_stdout_sink_enqueues_snapshot_owner_without_copying_route_graph() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime.stop_event = threading.Event()
    runtime._runtime_route_snapshot_lock = threading.Lock()
    runtime._runtime_hot_standby_lock = threading.RLock()
    runtime._runtime_network_context_hold_active = False
    runtime._runtime_local_facility_fault_reason = ""
    runtime._runtime_facility_epoch = 0
    snapshot = CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
    )
    # A lock is intentionally not deepcopy-able. Its presence proves the Core
    # pipe callback only takes the append/replace-owned payload reference.
    payload = {"uncopyable": threading.Lock()}
    runtime._runtime_route_snapshot = snapshot
    runtime._runtime_route_snapshot_payload = payload
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
    event = CoreOutputEvent(
        raw_line=(
            "[TCP] dial GENERAL (match Match/) 127.0.0.1:51001 "
            "--> example.net:443 error: context deadline exceeded"
        ),
        pid=4312,
        process_generation=7,
        received_at_monotonic=time.monotonic(),
    )

    runtime._on_core_output_event(event)

    queued = runtime._runtime_egress_coordinator.next_ingress()
    assert queued.event is event
    assert queued.snapshot is snapshot
    assert queued.snapshot_payload is payload
    assert queued.facility_epoch == 0
    assert queued.facility_hold_active is False


def test_real_stdout_failure_uses_queued_snapshot_without_controller_refresh() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_hot_standby_lock = threading.RLock()
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_transport_failure_is_internal_probe = lambda _parsed: False
    runtime._refresh_runtime_route_snapshot = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("real stdout attribution must not wait for /proxies")
    )
    runtime._start_runtime_facility_confirmation = lambda _reason: (_ for _ in ()).throw(
        AssertionError("the immutable loaded route is sufficient for attribution")
    )
    runtime._submit_current_route_probe_failure = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("the route-change guard must not run for the same snapshot")
    )
    started: list[BoundRuntimeFailure] = []
    runtime._start_runtime_independent_probe = (
        lambda failure, *_args, **_kwargs: started.append(failure)
    )

    bad = RuntimeNodeRef("tier1", "tier1_variant", "bad-id", "bad-leaf")
    snapshot = CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        proxy_nodes=(bad,),
    )
    payload = {
        "proxies": {
            "proxies": {
                "GENERAL": {
                    "type": "Selector",
                    "now": "FINAL-EXIT",
                    "all": ["FINAL-EXIT"],
                },
                "FINAL-EXIT": {
                    "type": "Selector",
                    "now": "TIER1-AUTO",
                    "all": ["TIER1-AUTO", "REJECT"],
                },
                "TIER1-AUTO": {
                    "type": "Fallback",
                    "now": "bad-leaf",
                    "all": ["bad-leaf"],
                },
                "bad-leaf": {"type": "Shadowsocks"},
            }
        },
        "proxy_map": {
            "bad-leaf": {
                "tier": "tier1",
                "pool_kind": "tier1_variant",
                "identity": "bad-id",
            }
        },
    }
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
    event = CoreOutputEvent(
        raw_line=(
            "[TCP] dial GENERAL (match Match/) 127.0.0.1:51001 "
            "--> example.net:443 error: context deadline exceeded"
        ),
        pid=4312,
        process_generation=7,
        received_at_monotonic=time.monotonic(),
    )

    runtime._handle_runtime_output_event(event, snapshot, payload)

    assert len(started) == 1
    assert started[0].binding.node == bad
    assert started[0].binding.route_chain == (
        "GENERAL",
        "FINAL-EXIT",
        "TIER1-AUTO",
        "bad-leaf",
    )


def test_independent_flow_evidence_diverts_to_fresh_hot_standby() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_hot_standby_lock = threading.RLock()
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_transport_failure_is_internal_probe = lambda _parsed: False
    runtime._start_runtime_facility_confirmation = lambda _reason: None
    runtime._submit_current_route_probe_failure = lambda *_args, **_kwargs: True
    bad = RuntimeNodeRef("tier1", "tier1_variant", "bad-id", "bad-leaf")
    good = RuntimeNodeRef("tier1", "tier1_variant", "good-id", "good-leaf")
    snapshot = CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        proxy_nodes=(bad, good),
    )
    payload = {
        "proxies": {
            "proxies": {
                "GENERAL": {"type": "Selector", "now": "FINAL-EXIT", "all": ["FINAL-EXIT"]},
                "FINAL-EXIT": {"type": "Selector", "now": "TIER1-AUTO", "all": ["TIER1-AUTO", "REJECT"]},
                "TIER1-AUTO": {"type": "Fallback", "now": "bad-leaf", "all": ["bad-leaf", "good-leaf"]},
                "bad-leaf": {"type": "Shadowsocks"},
                "good-leaf": {"type": "Shadowsocks"},
            }
        },
        "proxy_map": {
            "bad-leaf": {"tier": "tier1", "pool_kind": "tier1_variant", "identity": "bad-id"},
            "good-leaf": {"tier": "tier1", "pool_kind": "tier1_variant", "identity": "good-id"},
        },
    }
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
    evidence = object()
    runtime._fresh_runtime_hot_standby = lambda *_args: evidence
    diversions: list[tuple[str, object]] = []
    runtime._start_runtime_provisional_diversion = (
        lambda _failure, _snapshot, _payload, *, incident_id, evidence, **_kwargs: diversions.append(
            (str(incident_id), evidence)
        )
        or True
    )
    probes: list[str] = []
    runtime._start_runtime_independent_probe = (
        lambda _failure, *_args, probe_token="", **_kwargs: probes.append(
            str(probe_token)
        )
    )
    now = time.monotonic()
    events = [
        CoreOutputEvent(
            raw_line=(
                "[TCP] dial GENERAL (match Match/) 127.0.0.1:51001 "
                "--> one.example:443 error: context deadline exceeded"
            ),
            pid=4312,
            process_generation=7,
            received_at_monotonic=now,
        ),
        CoreOutputEvent(
            raw_line=(
                "[TCP] dial GENERAL (match Match/) 127.0.0.1:51002 "
                "--> two.example:443 error: context deadline exceeded"
            ),
            pid=4312,
            process_generation=7,
            received_at_monotonic=now + 0.1,
        ),
    ]

    for event in events:
        runtime._handle_runtime_output_event(event, snapshot, payload)

    assert len(probes) == 1
    assert len(diversions) == 1
    assert diversions[0][1] is evidence


def test_fixed_gateway_deterministic_failure_never_enters_provisional_diversion() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_hot_standby_lock = threading.RLock()
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_transport_failure_is_internal_probe = lambda _parsed: False
    runtime._start_runtime_facility_confirmation = lambda _reason: None
    runtime._submit_current_route_probe_failure = lambda *_args, **_kwargs: True
    gateway = RuntimeNodeRef("exit", "gateway", "gateway-id", "fixed-gateway")
    snapshot = CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        proxy_nodes=(gateway,),
    )
    payload = {
        "proxies": {
            "proxies": {
                "GENERAL": {"type": "Selector", "now": "FINAL-EXIT", "all": ["FINAL-EXIT"]},
                "FINAL-EXIT": {"type": "Selector", "now": "EXIT-SELECT", "all": ["EXIT-SELECT", "REJECT"]},
                "EXIT-SELECT": {"type": "Selector", "now": "fixed-gateway", "all": ["fixed-gateway"]},
                "fixed-gateway": {"type": "Shadowsocks"},
            }
        },
        "proxy_map": {
            "fixed-gateway": {"tier": "exit", "pool_kind": "gateway", "identity": "gateway-id"},
        },
    }
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
    runtime._fresh_runtime_hot_standby = lambda *_args: (_ for _ in ()).throw(
        AssertionError("fixed Gateway must not seek an alternate Gateway")
    )
    runtime._start_runtime_provisional_diversion = lambda *_args, **_kwargs: (
        (_ for _ in ()).throw(AssertionError("fixed Gateway must not divert"))
    )
    probes: list[str] = []
    runtime._start_runtime_independent_probe = (
        lambda _failure, *_args, probe_token="", **_kwargs: probes.append(
            str(probe_token)
        )
    )
    event = CoreOutputEvent(
        raw_line=(
            "[TCP] dial GENERAL (match Match/) 127.0.0.1:51001 "
            "--> one.example:443 error: proxy authentication failed"
        ),
        pid=4312,
        process_generation=7,
        received_at_monotonic=time.monotonic(),
    )

    runtime._handle_runtime_output_event(event, snapshot, payload)

    assert len(probes) == 1


def test_provisional_diversion_changes_route_without_manager_health() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime.logger = logging.getLogger("test-provisional-diversion")
    runtime.stop_event = threading.Event()
    runtime._runtime_failure_monitor_lock = threading.RLock()
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
    runtime._runtime_route_stable_since = 0.0
    runtime._runtime_last_verified_route = None
    runtime._core_transition_lock = threading.RLock()
    runtime._runtime_reload_gate = RuntimeReloadGate()
    runtime._runtime_worker_token = lambda: (1,)
    runtime._runtime_worker_token_is_current = lambda _token: True
    runtime._set_effective_egress_state = lambda *_args: None
    runtime._start_runtime_independent_probe = lambda *_args, **_kwargs: None
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()

    class Manager:
        @staticmethod
        def apply_runtime_health_observation(_observation):
            raise AssertionError("provisional diversion changed Manager health")

    runtime.manager = Manager()
    bad = RuntimeNodeRef("tier1", "tier1_variant", "bad-id", "bad-leaf")
    good = RuntimeNodeRef("tier1", "tier1_variant", "good-id", "good-leaf")
    snapshot = CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        proxy_nodes=(bad, good),
    )
    binding = RuntimeRouteBinding(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        observed_outbound="GENERAL",
        route_chain=("GENERAL", "FINAL-EXIT", "TIER1-AUTO", "bad-leaf"),
        node=bad,
    )
    failure = BoundRuntimeFailure(
        binding=binding,
        protocol="TCP",
        observed_outbound="GENERAL",
        destination_token="destination",
        error_type="connection_refused",
        observed_at_monotonic=time.monotonic(),
    )
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
    first = runtime._runtime_egress_coordinator.observe(
        failure,
        current_snapshot=snapshot,
    )
    runtime._runtime_alternative_switch_action = lambda *_args: {
        "group": "TIER1-AUTO",
        "target": "good-leaf",
        "previous": "bad-leaf",
        "mode": "fixed",
    }
    runtime._fast_recover_confirmed_alternative = lambda *_args, **_kwargs: {
        "post_switch_connectivity_verified": True,
        "effective_egress_ok": True,
        "selection_action": {
            "group": "TIER1-AUTO",
            "target": "good-leaf",
            "mode": "fixed",
        },
        "alternative_leaf": "good-leaf",
        "selected_chain": ["FINAL-EXIT", "TIER1-AUTO", "good-leaf"],
        "post_switch_result": {"status": 204},
        "loaded_route_epoch": 10,
    }
    evidence = _HotStandbyEvidence(
        ref=good,
        action={"group": "TIER1-AUTO", "target": "good-leaf"},
        snapshot_key=snapshot.snapshot_key,
        network_context_epoch=0,
        verified_at_monotonic=time.monotonic(),
        latency_ms=8.0,
        probe_result={"delay": 8},
        selection_group="TIER1-AUTO",
        selection_target="good-leaf",
        selection_mode="fixed",
        probe_url="https://www.gstatic.com/generate_204",
        expected_status="204",
    )

    runtime._start_runtime_provisional_diversion(
        failure,
        snapshot,
        {},
        incident_id=first.incident_id,
        probe_token=first.probe_token,
        evidence=evidence,
    )
    deadline = time.monotonic() + 1.0
    while runtime._runtime_failure_recovery_threads and time.monotonic() < deadline:
        time.sleep(0.005)

    assert runtime._runtime_failure_recovery_threads == set()
    assert runtime._runtime_route_mutation_lease["provisional"] is True
    assert runtime._runtime_route_mutation_lease["to"] == "good-leaf"
    circuit = runtime._runtime_egress_coordinator.current_snapshot_for(binding)
    assert circuit is not None
    assert circuit.state == "suspect"
    assert circuit.stability_state.value == "diverted"


def test_origin_binding_has_no_authority_before_verified_lease() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_selector_intent_revision = 0
    runtime._runtime_network_context_epoch = 0
    runtime._runtime_worker_token_is_current = lambda _token: True
    bad = RuntimeNodeRef("tier1", "tier1_variant", "bad-id", "bad-leaf")
    snapshot = CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        proxy_nodes=(bad,),
    )
    binding = RuntimeRouteBinding(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        observed_outbound="GENERAL",
        route_chain=("GENERAL", "FINAL-EXIT", "bad-leaf"),
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
    incident_id = "incident-prepared"
    probe_token = "probe-prepared"
    record = OriginDiagnosticBinding(
        incident_id=incident_id,
        binding=binding,
        snapshot_key=snapshot.snapshot_key,
        selector_intent_revision=0,
        owner_token=probe_token,
        expires_at_monotonic=time.monotonic() + 30.0,
        committed_route_epoch=10,
        target_proxy="good-leaf",
    )
    runtime._runtime_origin_diagnostic_bindings = {incident_id: record}
    runtime._runtime_route_mutation_lease = None

    assert (
        runtime._runtime_origin_diagnostic_binding_for_probe(
            failure,
            snapshot,
            incident_id=incident_id,
            probe_token=probe_token,
            worker_token=(1,),
        )
        is None
    )

    runtime._runtime_route_mutation_lease = {
        "incident_id": incident_id,
        "owner_token": probe_token,
        "from": "bad-leaf",
        "to": "good-leaf",
        "route_epoch": 10,
        "verified_at_monotonic": 0.0,
        "provisional": True,
    }
    assert (
        runtime._runtime_origin_diagnostic_binding_for_probe(
            failure,
            snapshot,
            incident_id=incident_id,
            probe_token=probe_token,
            worker_token=(1,),
        )
        is None
    )

    runtime._runtime_route_mutation_lease["verified_at_monotonic"] = (
        time.monotonic()
    )
    assert (
        runtime._runtime_origin_diagnostic_binding_for_probe(
            failure,
            snapshot,
            incident_id=incident_id,
            probe_token=probe_token,
            worker_token=(1,),
        )
        is record
    )


def test_confirmed_incident_queues_behind_provisional_and_consumes_once() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime.logger = logging.getLogger("test-provisional-confirmation-mailbox")
    runtime.stop_event = threading.Event()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_hot_standby_lock = threading.RLock()
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
    runtime._runtime_route_stable_since = 0.0
    runtime._runtime_last_verified_route = None
    runtime._core_transition_lock = threading.RLock()
    runtime._runtime_reload_gate = None
    runtime._runtime_worker_token = lambda: (1,)
    runtime._runtime_worker_token_is_current = lambda _token: True
    runtime._set_effective_egress_state = lambda *_args: None
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    bad = RuntimeNodeRef("tier1", "tier1_variant", "bad-id", "bad-leaf")
    good = RuntimeNodeRef("tier1", "tier1_variant", "good-id", "good-leaf")
    snapshot = CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        proxy_nodes=(bad, good),
    )
    binding = RuntimeRouteBinding(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        observed_outbound="GENERAL",
        route_chain=("GENERAL", "FINAL-EXIT", "TIER1-AUTO", "bad-leaf"),
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
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
    opened = runtime._runtime_egress_coordinator.observe(
        failure,
        current_snapshot=snapshot,
    )
    runtime._runtime_alternative_switch_action = lambda *_args: {
        "group": "TIER1-AUTO",
        "target": "good-leaf",
        "previous": "bad-leaf",
        "mode": "fixed",
    }
    entered = threading.Event()
    release = threading.Event()

    def blocked_commit(*_args, **_kwargs):
        entered.set()
        assert release.wait(1.0)
        return {
            "post_switch_connectivity_verified": True,
            "effective_egress_ok": True,
            "selection_action": {
                "group": "TIER1-AUTO",
                "target": "good-leaf",
                "mode": "fixed",
            },
            "alternative_leaf": "good-leaf",
            "selected_chain": ["FINAL-EXIT", "TIER1-AUTO", "good-leaf"],
            "post_switch_result": {"status": 204},
            "loaded_route_epoch": 10,
        }

    runtime._fast_recover_confirmed_alternative = blocked_commit
    evidence = _HotStandbyEvidence(
        ref=good,
        action={"group": "TIER1-AUTO", "target": "good-leaf"},
        snapshot_key=snapshot.snapshot_key,
        network_context_epoch=0,
        verified_at_monotonic=time.monotonic(),
        latency_ms=8.0,
        probe_result={"delay": 8},
        selection_group="TIER1-AUTO",
        selection_target="good-leaf",
        selection_mode="fixed",
        probe_url="https://www.gstatic.com/generate_204",
        expected_status="204",
    )

    runtime._start_runtime_provisional_diversion(
        failure,
        snapshot,
        {},
        incident_id=opened.incident_id,
        probe_token=opened.probe_token,
        evidence=evidence,
    )
    assert entered.wait(0.5)
    assert runtime._runtime_origin_diagnostic_bindings == {}
    assert runtime._runtime_route_mutation_lease is None

    DesktopRuntime._start_runtime_recovery(
        runtime,
        failure,
        snapshot,
        {},
        incident_id=opened.incident_id,
        quarantine_token=opened.probe_token,
        confirmed_alternative=good,
    )
    provisional_key = ("provisional", *failure.circuit_key)
    assert runtime._runtime_failure_recovery_bindings == {provisional_key}
    assert list(runtime._runtime_pending_confirmed_incidents) == [
        opened.incident_id
    ]
    assert len(runtime._runtime_failure_recovery_threads) == 1

    consumed: list[dict[str, object]] = []

    def consume_once(_failure, _snapshot, _payload, **kwargs):
        consumed.append(dict(kwargs))

    runtime._start_runtime_recovery = consume_once
    release.set()
    deadline = time.monotonic() + 1.0
    while runtime._runtime_failure_recovery_threads and time.monotonic() < deadline:
        time.sleep(0.005)

    assert runtime._runtime_failure_recovery_threads == set()
    assert runtime._runtime_pending_confirmed_incidents == {}
    assert runtime._runtime_failure_recovery_bindings == set()
    assert len(consumed) == 1
    assert consumed[0]["incident_id"] == opened.incident_id
    assert consumed[0]["already_diverted"] is True
    assert opened.incident_id in runtime._runtime_origin_diagnostic_bindings
    assert runtime._runtime_route_mutation_lease["verified_at_monotonic"] > 0.0


def test_origin_probe_can_commit_health_after_own_route_diversion() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_hot_standby_lock = threading.RLock()
    runtime._runtime_selector_intent_revision = 0
    runtime._runtime_network_context_epoch = 0
    runtime._runtime_generation = 1
    runtime._runtime_flap_history = {}
    runtime._runtime_incident_bindings = {}
    runtime._runtime_incident_route_payloads = {}
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    bad = RuntimeNodeRef("tier1", "tier1_variant", "bad-id", "bad-leaf")
    good = RuntimeNodeRef("tier1", "tier1_variant", "good-id", "good-leaf")
    original = CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        proxy_nodes=(bad, good),
    )
    current = CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=10,
        proxy_nodes=(bad, good),
    )
    binding = RuntimeRouteBinding(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        observed_outbound="GENERAL",
        route_chain=("GENERAL", "FINAL-EXIT", "bad-leaf"),
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
    runtime._runtime_egress_coordinator.set_current_snapshot(original)
    opened = runtime._runtime_egress_coordinator.observe(
        failure,
        current_snapshot=original,
    )
    runtime._runtime_route_snapshot = current
    runtime._runtime_egress_coordinator.set_current_snapshot(current)
    runtime._runtime_worker_token_is_current = lambda _token: True
    incident_id = opened.incident_id
    probe_token = opened.probe_token
    runtime._runtime_origin_diagnostic_bindings = {
        incident_id: OriginDiagnosticBinding(
            incident_id=incident_id,
            binding=binding,
            snapshot_key=original.snapshot_key,
            selector_intent_revision=0,
            owner_token=probe_token,
            expires_at_monotonic=time.monotonic() + 30.0,
            committed_route_epoch=current.route_epoch,
            target_proxy=good.proxy_name,
        )
    }
    runtime._runtime_route_mutation_lease = {
        "incident_id": incident_id,
        "owner_token": probe_token,
        "from": bad.proxy_name,
        "to": good.proxy_name,
        "route_epoch": current.route_epoch,
        "verified_at_monotonic": time.monotonic(),
        "provisional": True,
    }
    observations: list[dict[str, object]] = []

    class Manager:
        runtime_generation = 1

        @staticmethod
        def apply_runtime_health_observation(observation):
            observations.append(dict(observation))
            return {"accepted": True, "changed": True}

    runtime.manager = Manager()

    origin = runtime._runtime_origin_diagnostic_binding_for_probe(
        failure,
        original,
        incident_id=incident_id,
        probe_token=probe_token,
        worker_token=(1,),
    )
    assert origin is not None
    confirmed = runtime._runtime_egress_coordinator.record_probe(
        failure,
        succeeded=False,
        probe_token=probe_token,
        current_snapshot=original,
        allow_origin_binding=True,
    )
    assert confirmed.confirmed is True

    transition = runtime._apply_confirmed_runtime_health(
        failure,
        original,
        {},
        incident_id=incident_id,
        quarantine_token=probe_token,
        allow_origin_binding=True,
    )

    assert transition["accepted"] is True
    assert observations[0]["identity"] == "bad-id"


def test_confirmed_alternative_action_chooses_deepest_selector() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._active_gateway_policy_enabled = lambda: False
    bad = RuntimeNodeRef("tier1", "tier1_variant", "bad-id", "bad-leaf")
    good = RuntimeNodeRef("tier1", "tier1_variant", "good-id", "good-leaf")
    binding = RuntimeRouteBinding(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        observed_outbound="GENERAL",
        route_chain=(
            "GENERAL",
            "FINAL-EXIT",
            "TIER1-SELECT",
            "EDGE-SELECT",
            "bad-leaf",
        ),
        node=bad,
    )
    failure = BoundRuntimeFailure(
        binding=binding,
        protocol="TCP",
        observed_outbound="GENERAL",
        destination_token="business-destination",
        error_type="timeout",
        observed_at_monotonic=time.monotonic(),
    )
    payload = {
        "proxies": {
            "proxies": {
                "GENERAL": {"type": "Selector", "now": "FINAL-EXIT", "all": ["FINAL-EXIT"]},
                "FINAL-EXIT": {
                    "type": "Selector",
                    "now": "TIER1-SELECT",
                    "all": ["TIER1-SELECT", "REJECT"],
                },
                "TIER1-SELECT": {
                    "type": "Selector",
                    "now": "EDGE-SELECT",
                    "all": ["EDGE-SELECT", "good-leaf"],
                },
                "EDGE-SELECT": {
                    "type": "Selector",
                    "now": "bad-leaf",
                    "all": ["bad-leaf", "good-leaf"],
                },
                "bad-leaf": {"type": "Shadowsocks"},
                "good-leaf": {"type": "Shadowsocks"},
            }
        }
    }

    action = runtime._runtime_alternative_switch_action(failure, good, payload)

    assert action == {
        "group": "EDGE-SELECT",
        "target": "good-leaf",
        "previous": "bad-leaf",
        "mode": "selector",
        "alternative_leaf": "good-leaf",
    }


def test_confirmed_alternative_fast_path_prewarms_exact_group_before_put_readback_and_204() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_route_epoch = 9
    runtime._runtime_route_selection_signature = ()
    runtime._runtime_route_snapshot = None
    runtime._runtime_route_snapshot_payload = None
    runtime._runtime_selector_intent_revision = 7
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._active_gateway_policy_enabled = lambda: False
    runtime._register_runtime_internal_probe = lambda *_args, **_kwargs: None

    bad = RuntimeNodeRef("tier1", "tier1_variant", "bad-id", "bad-leaf")
    good = RuntimeNodeRef("tier1", "tier1_variant", "good-id", "good-leaf")
    snapshot = CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        group_states=(
            CoreLoadedGroupState(
                name="EDGE-AUTO",
                type_name="Fallback",
                members=("bad-leaf", "good-leaf"),
                now="bad-leaf",
                test_url="https://group-probe.example/generate_204",
                expected_status="204",
            ),
        ),
        proxy_nodes=(bad, good),
    )
    binding = RuntimeRouteBinding(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        observed_outbound="GENERAL",
        route_chain=("GENERAL", "FINAL-EXIT", "EDGE-AUTO", "bad-leaf"),
        node=bad,
    )
    failure = BoundRuntimeFailure(
        binding=binding,
        protocol="TCP",
        observed_outbound="GENERAL",
        destination_token="business-destination",
        error_type="timeout",
        observed_at_monotonic=time.monotonic(),
    )

    def route_payload(selected: str) -> dict[str, object]:
        return {
            "proxies": {
                "proxies": {
                    "GENERAL": {"type": "Selector", "now": "FINAL-EXIT", "all": ["FINAL-EXIT"]},
                    "FINAL-EXIT": {
                        "type": "Selector",
                        "now": "EDGE-AUTO",
                        "all": ["EDGE-AUTO", "REJECT"],
                    },
                    "EDGE-AUTO": {
                        "type": "Fallback",
                        "now": selected,
                        "all": ["bad-leaf", "good-leaf"],
                    },
                    "bad-leaf": {"type": "Shadowsocks"},
                    "good-leaf": {"type": "Shadowsocks"},
                }
            },
            "proxy_map": {},
        }

    before_payload = route_payload("bad-leaf")
    after_payload = route_payload("good-leaf")
    refreshed = CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=10,
        group_states=(
            CoreLoadedGroupState(
                name="EDGE-AUTO",
                type_name="Fallback",
                members=("bad-leaf", "good-leaf"),
                now="good-leaf",
                fixed="good-leaf",
                test_url="https://group-probe.example/generate_204",
                expected_status="204",
            ),
        ),
        proxy_nodes=(bad, good),
    )
    calls: list[tuple[object, ...]] = []

    class Client:
        def proxy_delay(
            self,
            name,
            *,
            url,
            timeout_ms=5000,
            expected_status=None,
            request_timeout=None,
        ):
            calls.append(
                (
                    "prewarm",
                    name,
                    url,
                    expected_status,
                    timeout_ms,
                    request_timeout,
                )
            )
            return {"delay": 8}

        def select_proxy(self, group_name, proxy_name, *, timeout=None):
            calls.append(("put", group_name, proxy_name, timeout))
            return {"group": group_name, "selected": proxy_name}

    runtime._require_api_client = lambda: Client()

    def refresh(**_kwargs):
        calls.append(("readback",))
        with runtime._runtime_route_snapshot_lock:
            runtime._runtime_route_snapshot = refreshed
            runtime._runtime_route_snapshot_payload = after_payload
        runtime._runtime_egress_coordinator.set_current_snapshot(refreshed)
        return refreshed

    runtime._refresh_runtime_route_snapshot = refresh

    def verify(*, expected_snapshot, timeout_seconds, required_successes=1):
        assert required_successes == 1
        calls.append(("204", expected_snapshot.route_epoch, timeout_seconds))
        return {"status": 204, "latency_ms": 9}

    runtime._verify_runtime_final_exit = verify
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)

    result = runtime._fast_recover_confirmed_alternative(
        failure,
        snapshot,
        before_payload,
        good,
        incident_id="incident-fast",
        selector_intent_revision=7,
        deadline_monotonic=time.monotonic() + 3.0,
        # A raw member-delay result is not the fallback group's exact
        # URL/expected-status evidence and therefore cannot skip prewarm.
        prewarmed_result={"delay": 1},
    )

    assert [call[0] for call in calls] == ["prewarm", "put", "readback", "204"]
    assert calls[0][1:4] == (
        "good-leaf",
        "https://group-probe.example/generate_204",
        "204",
    )
    assert calls[1][1:3] == ("EDGE-AUTO", "good-leaf")
    assert result["effective_egress_ok"] is True
    assert result["leaf_proxy"] == "good-leaf"
    assert result["selection_action"]["group"] == "EDGE-AUTO"


def test_fixed_gateway_body_failure_never_selects_another_gateway() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._active_gateway_policy_enabled = lambda: True
    current = RuntimeNodeRef("exit", "gateway", "gateway-id", "fixed-gateway")
    other = RuntimeNodeRef("exit", "gateway", "other-gateway-id", "other-gateway")
    binding = RuntimeRouteBinding(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        observed_outbound="GENERAL",
        route_chain=("GENERAL", "FINAL-EXIT", "EXIT-SELECT", "fixed-gateway"),
        node=current,
    )
    failure = BoundRuntimeFailure(
        binding=binding,
        protocol="TCP",
        observed_outbound="GENERAL",
        destination_token="business-destination",
        error_type="timeout",
        observed_at_monotonic=time.monotonic(),
    )
    payload = {
        "proxies": {
            "proxies": {
                "GENERAL": {"type": "Selector", "now": "FINAL-EXIT", "all": ["FINAL-EXIT"]},
                "FINAL-EXIT": {
                    "type": "Selector",
                    "now": "EXIT-SELECT",
                    "all": ["EXIT-SELECT", "REJECT"],
                },
                "EXIT-SELECT": {
                    "type": "Selector",
                    "now": "fixed-gateway",
                    "all": ["fixed-gateway", "other-gateway"],
                },
                "fixed-gateway": {"type": "Shadowsocks"},
                "other-gateway": {"type": "Shadowsocks"},
            }
        }
    }

    assert runtime._runtime_alternative_switch_action(failure, other, payload) is None


def test_gateway_body_failure_is_attributed_only_after_healthy_underlay() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_failure_monitor_lock = threading.Lock()
    runtime._runtime_hot_standby_lock = threading.RLock()
    runtime._runtime_failure_probe_bindings = set()
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_worker_token = lambda: (1,)
    runtime._runtime_worker_token_is_current = lambda _token: True
    gateway = RuntimeNodeRef("exit", "gateway", "gateway-id", "fixed-gateway")
    underlay = RuntimeNodeRef(
        "tier1", "tier1_variant", "underlay-id", "underlay"
    )
    snapshot = CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        proxy_nodes=(gateway, underlay),
    )
    gateway_binding = RuntimeRouteBinding(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        observed_outbound="GENERAL",
        route_chain=("GENERAL", "FINAL-EXIT", "EXIT-SELECT", "fixed-gateway"),
        node=gateway,
    )
    underlay_binding = RuntimeRouteBinding(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        observed_outbound="PROXY-FALLBACK",
        route_chain=("PROXY-FALLBACK", "underlay"),
        node=underlay,
    )
    failure = BoundRuntimeFailure(
        binding=gateway_binding,
        protocol="TCP",
        observed_outbound="GENERAL",
        destination_token="business-destination",
        error_type="timeout",
        observed_at_monotonic=time.monotonic(),
    )
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
    runtime._runtime_gateway_underlay_binding = (
        lambda _snapshot, _payload, _gateway: underlay_binding
    )

    probe_contracts: list[tuple[str, str, str]] = []

    def probe(ref, _payload, **kwargs):
        probe_contracts.append(
            (
                ref.proxy_name,
                str(kwargs.get("probe_url") or ""),
                str(kwargs.get("expected_status") or ""),
            )
        )
        if ref.key == gateway.key:
            raise RuntimeError("Gateway body failed")
        return {"delay": 8}

    runtime._probe_runtime_leaf = probe
    runtime._probe_managed_alternative = lambda *_args: (None, None)
    recovered = threading.Event()
    captured: list[RuntimeNodeRef] = []

    def start_recovery(bound_failure, *_args, **_kwargs):
        captured.append(bound_failure.binding.node)
        recovered.set()

    runtime._start_runtime_recovery = start_recovery
    runtime._record_runtime_facility_degradation = lambda _reason: {}
    first = runtime._runtime_egress_coordinator.observe(
        failure,
        current_snapshot=snapshot,
    )

    runtime._start_runtime_independent_probe(
        failure,
        snapshot,
        {},
        probe_token=first.probe_token,
    )

    assert recovered.wait(1.8)
    assert captured == [gateway]
    assert sorted(name for name, _url, _expected in probe_contracts) == [
        "fixed-gateway",
        "underlay",
    ]
    assert {url for _name, url, _expected in probe_contracts} == {
        "https://www.gstatic.com/generate_204"
    }
    assert {expected for _name, _url, expected in probe_contracts} == {"204"}


def test_gateway_underlay_recovery_keeps_outer_activation_ticket() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime.logger = logging.getLogger("test-gateway-underlay-ticket-bridge")
    runtime.stop_event = threading.Event()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_hot_standby_lock = threading.RLock()
    runtime._runtime_failure_probe_bindings = set()
    runtime._runtime_recovery_tickets = {}
    runtime._runtime_recovery_outcomes = {}
    runtime._runtime_recovery_outcome_events = {}
    runtime._runtime_recovery_ticket_revision = 0
    runtime._runtime_incident_recovery_state = {}
    runtime._runtime_facility_epoch = 0
    runtime._runtime_selector_intent_revision = 0
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_worker_token = lambda: (1,)
    runtime._runtime_worker_token_is_current = lambda _token: True
    runtime._runtime_facility_hold_reason = lambda: ""
    gateway = RuntimeNodeRef("exit", "gateway", "gateway-id", "fixed-gateway")
    underlay = RuntimeNodeRef(
        "tier1", "tier1_variant", "underlay-a-id", "underlay-a"
    )
    alternative = RuntimeNodeRef(
        "tier1", "tier1_variant", "underlay-b-id", "underlay-b"
    )
    snapshot = CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        proxy_nodes=(gateway, underlay, alternative),
    )
    gateway_binding = RuntimeRouteBinding(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        observed_outbound="GENERAL",
        route_chain=("GENERAL", "FINAL-EXIT", "EXIT-SELECT", "fixed-gateway"),
        node=gateway,
    )
    underlay_binding = RuntimeRouteBinding(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        observed_outbound="PROXY-FALLBACK",
        route_chain=("PROXY-FALLBACK", "underlay-a"),
        node=underlay,
    )
    failure = BoundRuntimeFailure(
        binding=gateway_binding,
        protocol="TCP",
        observed_outbound="GENERAL",
        destination_token="business-destination",
        error_type="timeout",
        observed_at_monotonic=time.monotonic(),
    )
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
    runtime._runtime_gateway_underlay_binding = (
        lambda _snapshot, _payload, _gateway: underlay_binding
    )
    runtime._probe_runtime_leaf = lambda *_args, **_kwargs: (
        (_ for _ in ()).throw(RuntimeError("bound route failed"))
    )
    runtime._fresh_runtime_hot_standby = lambda *_args, **_kwargs: None
    runtime._probe_managed_alternative = lambda *_args, **_kwargs: (
        alternative,
        {"delay": 8},
    )
    runtime._record_runtime_facility_degradation = lambda _reason: {}
    captured: list[tuple[BoundRuntimeFailure, dict[str, object]]] = []
    recovered = threading.Event()

    def start_recovery(bound_failure, *_args, **kwargs):
        captured.append((bound_failure, dict(kwargs)))
        recovered.set()

    runtime._start_runtime_recovery = start_recovery
    outer = runtime._runtime_egress_coordinator.observe(
        failure,
        current_snapshot=snapshot,
    )

    runtime._start_runtime_independent_probe(
        failure,
        snapshot,
        {},
        probe_token=outer.probe_token,
    )

    assert recovered.wait(1.8)
    assert len(captured) == 1
    rebound_failure, kwargs = captured[0]
    assert rebound_failure.binding.node == underlay
    assert kwargs["incident_id"] == outer.incident_id
    ticket = kwargs["recovery_ticket_override"]
    assert ticket.incident_id == outer.incident_id
    assert kwargs["circuit_incident_id"]
    assert kwargs["circuit_incident_id"] != outer.incident_id
    assert kwargs["circuit_quarantine_token"]


def test_fixed_gateway_underlay_switches_a_b_then_b_c_and_keeps_gateway_g() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_selector_intent_revision = 4
    runtime._runtime_recovery_monotonic = time.monotonic
    runtime._runtime_worker_token_is_current = lambda _token: True
    runtime._runtime_facility_hold_reason = lambda: ""
    runtime._active_gateway_policy_enabled = lambda: True
    runtime._prewarm_runtime_switch_target = (
        lambda _snapshot, action, **_kwargs: {
            "group": action["group"],
            "target": action["target"],
        }
    )
    forbidden: list[str] = []
    runtime._enforce_runtime_fail_closed = (
        lambda *_args, **_kwargs: forbidden.append("reject")
    )
    runtime._activate_killswitch_for_egress_failure = (
        lambda *_args, **_kwargs: forbidden.append("killswitch")
    )
    runtime.stop_core = lambda *_args, **_kwargs: forbidden.append("core-stop")

    gateway = RuntimeNodeRef("exit", "gateway", "gateway-id", "G")
    underlays = {
        name: RuntimeNodeRef(
            "tier1",
            "tier1_variant",
            f"underlay-{name.lower()}-id",
            name,
        )
        for name in ("A", "B", "C")
    }
    state: dict[str, object] = {
        "selected": "A",
        "route_epoch": 40,
    }

    def authority() -> tuple[CoreLoadedRouteSnapshot, dict[str, object]]:
        selected = str(state["selected"])
        entries: dict[str, object] = {
            "FINAL-EXIT": {
                "type": "Selector",
                "now": "EXIT-SELECT",
                "fixed": "EXIT-SELECT",
                "all": ["EXIT-SELECT", "REJECT"],
            },
            "EXIT-SELECT": {
                "type": "Selector",
                "now": gateway.proxy_name,
                "fixed": gateway.proxy_name,
                "all": [gateway.proxy_name],
            },
            gateway.proxy_name: {"type": "Shadowsocks"},
            "PROXY-FALLBACK": {
                "type": "Fallback",
                "now": selected,
                "fixed": selected,
                "all": ["A", "B", "C"],
            },
            **{
                name: {"type": "Shadowsocks"}
                for name in underlays
            },
        }
        controller = {"proxies": entries}
        group_graph, _selection, group_states = (
            DesktopRuntime._runtime_group_graph_and_selection(controller)
        )
        snapshot = CoreLoadedRouteSnapshot(
            process_pid=4312,
            process_generation=7,
            core_generation=3,
            semantic_digest="gateway-loaded",
            route_epoch=int(state["route_epoch"]),
            group_graph=group_graph,
            group_states=group_states,
            proxy_nodes=(gateway, *underlays.values()),
        )
        payload: dict[str, object] = {
            "proxies": controller,
            "proxy_map": {
                gateway.proxy_name: {
                    "tier": gateway.tier,
                    "pool_kind": gateway.pool_kind,
                    "identity": gateway.identity,
                    "proxy": {
                        "name": gateway.proxy_name,
                        "dialer-proxy": "PROXY-FALLBACK",
                    },
                },
                **{
                    name: {
                        "tier": ref.tier,
                        "pool_kind": ref.pool_kind,
                        "identity": ref.identity,
                    }
                    for name, ref in underlays.items()
                },
            },
        }
        return snapshot, payload

    current_snapshot, current_payload = authority()

    def apply_selection(action, **_kwargs):
        assert action["group"] == "PROXY-FALLBACK"
        state["selected"] = str(action["target"])
        state["route_epoch"] = int(state["route_epoch"]) + 1
        nonlocal current_snapshot, current_payload
        current_snapshot, current_payload = authority()
        return {
            "group": action["group"],
            "selected": action["target"],
            "fixed": action["target"],
        }

    runtime._apply_runtime_automatic_selection = apply_selection
    runtime._refresh_runtime_route_snapshot = lambda **_kwargs: current_snapshot
    runtime._runtime_route_snapshot_state = lambda: (
        current_snapshot,
        current_payload,
    )
    verified_gateways: list[tuple[str, str, int]] = []

    def verify_final_exit(**_kwargs):
        binding = runtime._runtime_route_binding_for_outbound(
            current_snapshot,
            current_payload,
            "FINAL-EXIT",
        )
        assert binding is not None
        verified_gateways.append(
            (
                binding.node.proxy_name,
                str(state["selected"]),
                current_snapshot.route_epoch,
            )
        )
        return {"status": 204}

    runtime._verify_runtime_final_exit = verify_final_exit

    def underlay_failure(name: str) -> BoundRuntimeFailure:
        binding = runtime._runtime_route_binding_for_outbound(
            current_snapshot,
            current_payload,
            "PROXY-FALLBACK",
            require_final_exit=False,
        )
        assert binding is not None
        assert binding.node == underlays[name]
        return BoundRuntimeFailure(
            binding=binding,
            protocol="TCP",
            observed_outbound="PROXY-FALLBACK",
            destination_token=f"opaque-{name}",
            error_type="timeout",
            observed_at_monotonic=time.monotonic(),
        )

    first = runtime._fast_recover_confirmed_alternative(
        underlay_failure("A"),
        current_snapshot,
        current_payload,
        underlays["B"],
        incident_id="underlay-a-b",
        selector_intent_revision=4,
        deadline_monotonic=time.monotonic() + 2.0,
    )
    second = runtime._fast_recover_confirmed_alternative(
        underlay_failure("B"),
        current_snapshot,
        current_payload,
        underlays["C"],
        incident_id="underlay-b-c",
        selector_intent_revision=4,
        deadline_monotonic=time.monotonic() + 2.0,
    )

    assert first["alternative_leaf"] == "B"
    assert second["alternative_leaf"] == "C"
    assert first["leaf_proxy"] == gateway.proxy_name
    assert second["leaf_proxy"] == gateway.proxy_name
    assert verified_gateways == [("G", "B", 41), ("G", "C", 42)]
    assert state["selected"] == "C"
    assert forbidden == []


def test_confirmed_recovery_worker_consumes_proven_alternative_without_audit() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_hot_standby_lock = threading.RLock()
    runtime._core_transition_lock = threading.RLock()
    runtime._runtime_failure_recovery_bindings = set()
    runtime._runtime_failure_recovery_threads = set()
    runtime._runtime_incident_recovery_state = {"previous-a-b": "ready"}
    runtime._runtime_pending_recovered_payloads = {}
    runtime._runtime_pending_confirmed_incidents = {}
    runtime._runtime_origin_diagnostic_bindings = {}
    runtime._runtime_deferred_provisional_confirmations = {}
    runtime._runtime_route_mutation_lease = {
        "automatic": True,
        "incident_id": "previous-a-b",
        "owner_token": "previous-owner",
        "from": "first-leaf",
        "to": "bad-leaf",
        "route_epoch": 9,
        "selector_intent_revision": 4,
        "verified_at_monotonic": time.monotonic(),
    }
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_route_epoch = 9
    runtime._runtime_selector_intent_revision = 4
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_worker_token = lambda: (1,)
    runtime._runtime_worker_token_is_current = lambda _token: True
    runtime._activate_killswitch_for_egress_failure = lambda _reason: None
    health_calls: list[str] = []
    runtime._apply_confirmed_runtime_health = (
        lambda *_args, incident_id, **_kwargs: (
            health_calls.append(str(incident_id))
            or {"accepted": True, "changed": True}
        )
    )
    fast_switches: list[RuntimeNodeRef] = []

    def fast_switch(_failure, _snapshot, _payload, alternative, **_kwargs):
        fast_switches.append(alternative)
        return {
            "fast_path_available": True,
            "selection_action": {
                "group": "TIER1-AUTO",
                "target": "good-leaf",
                "mode": "fixed",
            },
            "post_switch_connectivity_verified": True,
            "effective_egress_ok": True,
            "selected_chain": ["FINAL-EXIT", "good-leaf"],
            "leaf_proxy": "good-leaf",
            "alternative_leaf": "good-leaf",
            "post_switch_result": {"status": 204},
            "loaded_route_epoch": 10,
        }

    runtime._fast_recover_confirmed_alternative = fast_switch
    runtime.audit_runtime_egress = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("confirmed alternative must not re-enter route audit")
    )
    runtime._refresh_runtime_route_snapshot = lambda **_kwargs: None
    runtime._set_effective_egress_state = lambda *_args: None
    runtime._start_runtime_connection_cleanup = lambda _binding: None
    rearm_states: list[str] = []
    runtime._try_arm_runtime_recovery = lambda: (
        rearm_states.append(
            str(
                runtime._runtime_incident_recovery_state.get(
                    confirmed.incident_id,
                    "",
                )
            )
        )
        or True
    )

    bad = RuntimeNodeRef("tier1", "tier1_variant", "bad-id", "bad-leaf")
    good = RuntimeNodeRef("tier1", "tier1_variant", "good-id", "good-leaf")
    snapshot = CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        proxy_nodes=(bad, good),
    )
    binding = RuntimeRouteBinding(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        observed_outbound="GENERAL",
        route_chain=("GENERAL", "FINAL-EXIT", "bad-leaf"),
        node=bad,
    )
    failure = BoundRuntimeFailure(
        binding=binding,
        protocol="TCP",
        observed_outbound="GENERAL",
        destination_token="business-destination",
        error_type="timeout",
        observed_at_monotonic=time.monotonic(),
    )
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
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

    runtime._start_runtime_recovery(
        failure,
        snapshot,
        {},
        incident_id=confirmed.incident_id,
        quarantine_token=confirmed.probe_token,
        confirmed_alternative=good,
    )
    deadline = time.monotonic() + 1.0
    while runtime._runtime_failure_recovery_threads and time.monotonic() < deadline:
        time.sleep(0.005)

    assert runtime._runtime_failure_recovery_threads == set()
    assert health_calls == [confirmed.incident_id]
    assert fast_switches == [good]
    assert runtime._runtime_route_mutation_lease["to"] == "good-leaf"
    assert runtime._runtime_route_mutation_lease["group"] == "TIER1-AUTO"
    assert runtime._runtime_route_mutation_lease["parent_incident_id"] == (
        "previous-a-b"
    )
    assert runtime._runtime_incident_recovery_state["previous-a-b"] == (
        "route_superseded"
    )
    assert runtime._runtime_incident_recovery_state[confirmed.incident_id] == "ready"
    assert runtime._runtime_recovery_outcomes[confirmed.incident_id].status is (
        RuntimeRecoveryStatus.RECOVERED
    )
    assert rearm_states == ["ready"]


def _ordered_recovery_fixture() -> tuple[
    DesktopRuntime,
    BoundRuntimeFailure,
    CoreLoadedRouteSnapshot,
    dict[str, object],
    tuple[RuntimeNodeRef, ...],
]:
    runtime = object.__new__(DesktopRuntime)
    runtime.logger = logging.getLogger("test-ordered-runtime-recovery")
    refs = tuple(
        RuntimeNodeRef("tier3", "tier3b", f"id-{name}", name)
        for name in ("A", "B", "C", "D", "E", "F", "G")
    )
    snapshot = CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=12,
        group_graph=(
            ("FINAL-EXIT", ("TIER3-AUTO", "REJECT")),
            ("TIER3-AUTO", tuple(ref.proxy_name for ref in refs)),
        ),
        proxy_nodes=refs,
    )
    binding = RuntimeRouteBinding(
        process_pid=snapshot.process_pid,
        process_generation=snapshot.process_generation,
        core_generation=snapshot.core_generation,
        semantic_digest=snapshot.semantic_digest,
        route_epoch=snapshot.route_epoch,
        observed_outbound="FINAL-EXIT",
        route_chain=("FINAL-EXIT", "TIER3-AUTO", "B"),
        node=refs[1],
    )
    failure = BoundRuntimeFailure(
        binding=binding,
        protocol="PROBE",
        observed_outbound="FINAL-EXIT",
        destination_token="opaque",
        error_type="activation_final_exit",
        observed_at_monotonic=time.monotonic(),
        deterministic_proxy_endpoint=True,
    )
    payload: dict[str, object] = {
        "proxies": {
            "proxies": {
                "FINAL-EXIT": {
                    "type": "Selector",
                    "now": "TIER3-AUTO",
                    "all": ["TIER3-AUTO", "REJECT"],
                },
                "TIER3-AUTO": {
                    "type": "Fallback",
                    "now": "B",
                    "all": [ref.proxy_name for ref in refs],
                },
                **{
                    ref.proxy_name: {"type": "Shadowsocks"}
                    for ref in refs
                },
            }
        },
        "proxy_map": {
            ref.proxy_name: {"node": SimpleNamespace(latency=200)}
            for ref in refs
        },
    }
    candidate_snapshot = SimpleNamespace(
        revision=5,
        runtime_generation=9,
        candidates=tuple(
            SimpleNamespace(key=ref.key, eligible=True) for ref in refs
        ),
    )
    runtime.manager = SimpleNamespace(
        runtime_candidate_snapshot=lambda: candidate_snapshot
    )
    runtime._runtime_egress_coordinator = SimpleNamespace(
        current_snapshot_for=lambda _binding: None
    )
    return runtime, failure, snapshot, payload, refs


def test_alternatives_start_after_current_without_wrap_or_four_item_cap() -> None:
    runtime, failure, snapshot, payload, refs = _ordered_recovery_fixture()

    alternatives = runtime._runtime_ordered_alternative_refs(
        failure,
        snapshot,
        payload,
    )

    assert alternatives == list(refs[2:])
    assert refs[0] not in alternatives
    assert alternatives[-1].proxy_name == "G"


def test_missing_earlier_hot_evidence_does_not_select_later_standby() -> None:
    runtime, failure, snapshot, payload, refs = _ordered_recovery_fixture()
    runtime._runtime_hot_standby_lock = threading.RLock()
    runtime._runtime_hot_standby_evidence = {}
    runtime._runtime_network_context_epoch = 4
    runtime._runtime_recovery_monotonic = lambda: 100.0
    later = refs[3]
    action = runtime._runtime_alternative_switch_action(
        failure,
        later,
        payload,
    )
    assert action is not None
    runtime._runtime_hot_standby_evidence[later.key] = _HotStandbyEvidence(
        ref=later,
        action=dict(action),
        snapshot_key=snapshot.snapshot_key,
        network_context_epoch=4,
        verified_at_monotonic=99.0,
        latency_ms=8.0,
        probe_result={"delay": 8},
        selection_group=str(action["group"]),
        selection_target=str(action["target"]),
        selection_mode=str(action["mode"]),
        probe_url="https://www.gstatic.com/generate_204",
        expected_status="204",
        probed_proxy_name=later.proxy_name,
    )

    assert runtime._fresh_runtime_hot_standby(
        failure,
        snapshot,
        payload,
    ) is None


def test_cold_candidate_uses_adaptive_800ms_budget_and_accepts_600ms_pass() -> None:
    runtime, failure, snapshot, payload, refs = _ordered_recovery_fixture()
    runtime._runtime_ordered_alternative_refs = lambda *_args, **_kwargs: [refs[2]]
    fake_now = [100.0]
    runtime._runtime_recovery_monotonic = lambda: fake_now[0]
    observed_timeouts: list[int] = []

    def probe(_ref, _payload, *, timeout_ms, **_kwargs):
        observed_timeouts.append(timeout_ms)
        fake_now[0] += 0.6
        return {"delay": 600}

    runtime._probe_runtime_leaf = probe

    selected, result = runtime._probe_managed_alternative(
        failure,
        snapshot,
        payload,
        max_parallel=1,
        deadline_monotonic=106.0,
    )

    assert selected == refs[2]
    assert result == {"delay": 600}
    assert observed_timeouts == [800]


def test_fifth_candidate_is_reached_after_four_authoritative_failures() -> None:
    runtime, failure, snapshot, payload, refs = _ordered_recovery_fixture()
    candidates = list(refs[2:7])
    runtime._runtime_ordered_alternative_refs = (
        lambda *_args, **_kwargs: candidates
    )
    probed: list[str] = []

    def probe(ref, _payload, **_kwargs):
        probed.append(ref.proxy_name)
        if ref is candidates[-1]:
            return {"delay": 9}
        raise RuntimeError("authoritative failure")

    runtime._probe_runtime_leaf = probe

    selected, result = runtime._probe_managed_alternative(
        failure,
        snapshot,
        payload,
        max_parallel=2,
    )

    assert selected == candidates[-1]
    assert result == {"delay": 9}
    assert set(probed) == {ref.proxy_name for ref in candidates}


def test_earlier_indeterminate_candidate_blocks_later_fast_pass() -> None:
    runtime, failure, snapshot, payload, refs = _ordered_recovery_fixture()
    runtime._runtime_ordered_alternative_refs = (
        lambda *_args, **_kwargs: [refs[2], refs[3]]
    )

    def probe(ref, _payload, **_kwargs):
        if ref == refs[2]:
            raise _RuntimeProbeBudgetUnavailable("occupied")
        return {"delay": 1}

    runtime._probe_runtime_leaf = probe

    selected, result = runtime._probe_managed_alternative(
        failure,
        snapshot,
        payload,
        max_parallel=2,
    )

    assert selected is None
    assert result == {"probe_indeterminate": True}


def test_transient_controller_error_on_earlier_candidate_blocks_later_pass() -> None:
    runtime, failure, snapshot, payload, refs = _ordered_recovery_fixture()
    runtime._runtime_ordered_alternative_refs = (
        lambda *_args, **_kwargs: [refs[2], refs[3]]
    )

    def probe(ref, _payload, **_kwargs):
        if ref == refs[2]:
            raise RuntimeError("controller unavailable: 503")
        return {"delay": 7}

    runtime._probe_runtime_leaf = probe

    selected, result = runtime._probe_managed_alternative(
        failure,
        snapshot,
        payload,
        max_parallel=2,
    )

    assert selected is None
    assert result == {"probe_indeterminate": True}


def test_candidate_health_snapshot_failure_is_indeterminate_not_all_failed() -> None:
    runtime, failure, snapshot, payload, _refs = _ordered_recovery_fixture()
    runtime.manager = SimpleNamespace(
        runtime_candidate_snapshot=lambda: (_ for _ in ()).throw(
            RuntimeError("snapshot unavailable")
        )
    )
    runtime._runtime_ordered_alternative_refs = lambda *_args, **_kwargs: (
        (_ for _ in ()).throw(
            AssertionError("unavailable health must stop before probing")
        )
    )

    selected, result = runtime._probe_managed_alternative(
        failure,
        snapshot,
        payload,
    )

    assert selected is None
    assert result == {"probe_indeterminate": True}


def test_verified_automatic_lease_can_atomically_chain_a_b_to_b_c() -> None:
    runtime, failure, _snapshot, _payload, refs = _ordered_recovery_fixture()
    runtime._runtime_failure_monitor_lock = threading.Lock()
    runtime._runtime_incident_recovery_state = {"incident-a-b": "ready"}
    runtime._runtime_pending_confirmed_incidents = {"incident-a-b": {}}
    runtime._runtime_pending_recovered_payloads = {"incident-a-b": {}}
    runtime._runtime_origin_diagnostic_bindings = {"incident-a-b": object()}
    runtime._runtime_deferred_provisional_confirmations = {
        "incident-a-b": {}
    }
    runtime._runtime_route_mutation_lease = {
        "automatic": True,
        "incident_id": "incident-a-b",
        "owner_token": "owner-a-b",
        "from": "A",
        "to": "B",
        "route_epoch": failure.binding.route_epoch,
        "selector_intent_revision": 3,
        "verified_at_monotonic": 10.0,
    }

    with runtime._runtime_failure_monitor_lock:
        parent = runtime._runtime_successor_parent_lease_locked(
            failure,
            selector_intent_revision=3,
        )
        committed = runtime._runtime_commit_successor_lease_locked(
            {
                "incident_id": "incident-b-c",
                "owner_token": "owner-b-c",
                "from": "B",
                "to": "C",
                "route_epoch": 13,
                "selector_intent_revision": 3,
                "verified_at_monotonic": 11.0,
            },
            parent,
        )

    assert committed is True
    assert runtime._runtime_route_mutation_lease["to"] == "C"
    assert (
        runtime._runtime_route_mutation_lease["parent_incident_id"]
        == "incident-a-b"
    )
    assert runtime._runtime_incident_recovery_state["incident-a-b"] == (
        "route_superseded"
    )
    assert "incident-a-b" not in runtime._runtime_origin_diagnostic_bindings
    with runtime._runtime_failure_monitor_lock:
        assert runtime._runtime_successor_parent_lease_locked(
            failure,
            selector_intent_revision=4,
        ) is None


def test_runtime_recovery_structured_event_contains_only_hashed_identity() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_recovery_tickets = {}
    runtime._runtime_recovery_outcomes = {}
    runtime._runtime_recovery_outcome_events = {}
    runtime._runtime_facility_epoch = 2
    runtime._runtime_selector_intent_revision = 3
    runtime._runtime_route_epoch = 17
    runtime._runtime_worker_token = lambda: (1,)
    runtime._runtime_recovery_monotonic = lambda: 101.0
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("test-runtime-recovery-structured-event")
    logger.handlers = [Capture()]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    runtime.logger = logger
    failed = RuntimeNodeRef(
        "tier1", "tier1_variant", "secret-id-A", "secret-proxy-A"
    )
    recovered = RuntimeNodeRef(
        "tier1", "tier1_variant", "secret-id-B", "secret-proxy-B"
    )
    snapshot = CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=17,
        proxy_nodes=(failed, recovered),
    )
    binding = RuntimeRouteBinding(
        process_pid=snapshot.process_pid,
        process_generation=snapshot.process_generation,
        core_generation=snapshot.core_generation,
        semantic_digest=snapshot.semantic_digest,
        route_epoch=snapshot.route_epoch,
        observed_outbound="FINAL-EXIT",
        route_chain=("FINAL-EXIT", "secret-proxy-A"),
        node=failed,
    )
    failure = BoundRuntimeFailure(
        binding=binding,
        protocol="PROBE",
        observed_outbound="FINAL-EXIT",
        destination_token="secret-destination",
        error_type="activation_final_exit",
        observed_at_monotonic=100.0,
    )

    ticket = runtime._register_runtime_recovery_ticket(
        failure,
        snapshot,
        "raw-incident-secret",
    )
    runtime._complete_runtime_recovery_outcome(
        "raw-incident-secret",
        RuntimeRecoveryStatus.RECOVERED,
        reason_code="verified_route",
        to_ref=recovered,
        route_epoch=18,
        expected_ticket=ticket,
        facts={"put": True, "readback": True, "final_204": True},
    )

    # The node-judgement arm diagnostics (T-F01, 2026-10) add plain
    # "Post-dial observer ..." records (no event_key) through the same logger:
    # arm_diagnostics_state plus one recovery_phase line per phase.  The
    # structured events are still exactly two, and every record -- diagnostic
    # ones included -- must carry hashed identity only.
    structured = [
        record for record in records if record.event_key == "routing.runtime_recovery"
    ]
    assert len(structured) == 2
    assert {record.event_key for record in records} <= {"routing.runtime_recovery", ""}
    serialized = json.dumps(
        structured[-1].event_params,
        ensure_ascii=False,
        sort_keys=True,
    )
    everything = "\n".join(
        [record.getMessage() for record in records]
        + [
            json.dumps(getattr(record, "event_params", None), ensure_ascii=False, sort_keys=True, default=str)
            for record in records
        ]
    )
    for raw_value in (
        "raw-incident-secret",
        "secret-id-A",
        "secret-proxy-A",
        "secret-id-B",
        "secret-proxy-B",
        "secret-destination",
    ):
        assert raw_value not in serialized
        assert raw_value not in everything
    assert '"identity_hash"' in serialized
    assert '"proxy_name_hash"' in serialized
    assert '"final_204": true' in serialized


def test_rebinding_incident_clears_previous_route_terminal_outcome() -> None:
    runtime, failure, snapshot, _payload, refs = _ordered_recovery_fixture()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_recovery_tickets = {}
    runtime._runtime_recovery_outcomes = {}
    runtime._runtime_recovery_outcome_events = {}
    runtime._runtime_facility_epoch = 0
    runtime._runtime_selector_intent_revision = 0
    runtime._runtime_route_epoch = snapshot.route_epoch
    runtime._runtime_worker_token = lambda: (1,)
    runtime._runtime_recovery_monotonic = (
        lambda: failure.observed_at_monotonic + 0.1
    )

    incident_id = "rebound-incident"
    old_ticket = runtime._register_runtime_recovery_ticket(
        failure,
        snapshot,
        incident_id,
    )
    runtime._complete_runtime_recovery_outcome(
        incident_id,
        RuntimeRecoveryStatus.RECOVERED,
        reason_code="first_route_verified",
        to_ref=refs[2],
        route_epoch=snapshot.route_epoch + 1,
        expected_ticket=old_ticket,
    )
    old_event = runtime._runtime_recovery_outcome_events[incident_id]
    assert old_event.is_set()

    rebound_snapshot = CoreLoadedRouteSnapshot(
        process_pid=snapshot.process_pid,
        process_generation=snapshot.process_generation,
        core_generation=snapshot.core_generation,
        semantic_digest=snapshot.semantic_digest,
        route_epoch=snapshot.route_epoch + 1,
        group_graph=snapshot.group_graph,
        proxy_nodes=snapshot.proxy_nodes,
    )
    rebound_binding = RuntimeRouteBinding(
        process_pid=rebound_snapshot.process_pid,
        process_generation=rebound_snapshot.process_generation,
        core_generation=rebound_snapshot.core_generation,
        semantic_digest=rebound_snapshot.semantic_digest,
        route_epoch=rebound_snapshot.route_epoch,
        observed_outbound="FINAL-EXIT",
        route_chain=("FINAL-EXIT", "TIER3-AUTO", refs[2].proxy_name),
        node=refs[2],
    )
    rebound_failure = BoundRuntimeFailure(
        binding=rebound_binding,
        protocol="PROBE",
        observed_outbound="FINAL-EXIT",
        destination_token="opaque",
        error_type="activation_final_exit",
        observed_at_monotonic=failure.observed_at_monotonic + 0.2,
        deterministic_proxy_endpoint=True,
    )

    ticket = runtime._register_runtime_recovery_ticket(
        rebound_failure,
        rebound_snapshot,
        incident_id,
    )

    assert ticket.from_ref == refs[2]
    assert incident_id not in runtime._runtime_recovery_outcomes
    assert runtime._complete_runtime_recovery_outcome(
        incident_id,
        RuntimeRecoveryStatus.RECOVERED,
        reason_code="late_old_worker",
        expected_ticket=old_ticket,
    ) is None
    assert incident_id not in runtime._runtime_recovery_outcomes
    new_event = runtime._runtime_recovery_outcome_events[incident_id]
    assert new_event is not old_event
    assert not new_event.is_set()


def test_recovery_deadline_completes_controller_hold_instead_of_none() -> None:
    runtime, failure, snapshot, _payload, _refs = _ordered_recovery_fixture()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_recovery_ticket_revision = 0
    runtime._runtime_recovery_tickets = {}
    runtime._runtime_recovery_outcomes = {}
    runtime._runtime_recovery_outcome_events = {}
    runtime._runtime_incident_recovery_state = {}
    runtime._runtime_route_mutation_lease = None
    runtime._runtime_core_stopped_recovery_owners = {}
    runtime._runtime_route_epoch = snapshot.route_epoch
    runtime._runtime_facility_epoch = 0
    runtime._runtime_selector_intent_revision = 0
    runtime._runtime_worker_token = lambda: (1,)
    runtime._runtime_worker_token_is_current = lambda _token: True
    runtime._runtime_facility_hold_reason = lambda: ""
    runtime._runtime_node_attribution_hold_reason = lambda: ""
    runtime._runtime_recovery_monotonic = (
        lambda: failure.observed_at_monotonic + 6.01
    )
    ticket = runtime._register_runtime_recovery_ticket(
        failure,
        snapshot,
        "deadline-incident",
    )

    outcome = runtime._wait_runtime_recovery_outcome(ticket)

    assert outcome is not None
    assert outcome.status is RuntimeRecoveryStatus.CONTROLLER_HOLD
    assert outcome.reason == "incident_budget_expired"


def test_recovery_ticket_deadline_cannot_be_extended_and_late_ready_is_held() -> None:
    runtime, failure, snapshot, _payload, refs = _ordered_recovery_fixture()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_recovery_ticket_revision = 0
    runtime._runtime_recovery_tickets = {}
    runtime._runtime_recovery_outcomes = {}
    runtime._runtime_recovery_outcome_events = {}
    runtime._runtime_route_epoch = snapshot.route_epoch
    runtime._runtime_facility_epoch = 0
    runtime._runtime_selector_intent_revision = 0
    runtime._runtime_worker_token = lambda: (1,)
    now = [failure.observed_at_monotonic + 6.01]
    runtime._runtime_recovery_monotonic = lambda: now[0]
    ticket = runtime._register_runtime_recovery_ticket(
        failure,
        snapshot,
        "late-ready-incident",
    )

    assert runtime._runtime_recovery_ticket_is_current(
        ticket,
        deadline_monotonic=float("inf"),
    ) is False
    outcome = runtime._complete_runtime_recovery_outcome(
        ticket.incident_id,
        RuntimeRecoveryStatus.RECOVERED,
        reason_code="late_route_claim",
        to_ref=refs[2],
        expected_ticket=ticket,
        completed_at_monotonic=now[0],
        facts={"put": True, "readback": True, "final_204": True},
    )

    assert outcome is not None
    assert outcome.status is RuntimeRecoveryStatus.CONTROLLER_HOLD
    assert outcome.reason == "incident_budget_expired"


def test_generationless_old_lease_cannot_complete_current_ticket() -> None:
    runtime, failure, snapshot, _payload, _refs = _ordered_recovery_fixture()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_recovery_ticket_revision = 0
    runtime._runtime_recovery_tickets = {}
    runtime._runtime_recovery_outcomes = {}
    runtime._runtime_recovery_outcome_events = {}
    runtime._runtime_incident_recovery_state = {
        "generation-incident": "ready"
    }
    runtime._runtime_core_stopped_recovery_owners = {}
    runtime._runtime_route_epoch = snapshot.route_epoch
    runtime._runtime_facility_epoch = 0
    runtime._runtime_selector_intent_revision = 0
    runtime._runtime_worker_token = lambda: (1,)
    runtime._runtime_worker_token_is_current = lambda _token: True
    runtime._runtime_facility_hold_reason = lambda: ""
    runtime._runtime_node_attribution_hold_reason = lambda: ""
    runtime._runtime_recovery_monotonic = (
        lambda: failure.observed_at_monotonic + 6.01
    )
    ticket = runtime._register_runtime_recovery_ticket(
        failure,
        snapshot,
        "generation-incident",
    )
    runtime._runtime_route_mutation_lease = {
        "incident_id": ticket.incident_id,
        "to": "C",
        "verified_at_monotonic": failure.observed_at_monotonic + 0.2,
        # Intentionally no recovery_ticket_generation: this models an old
        # failback/core-reconcile writer from before the ticket rebound.
    }

    outcome = runtime._wait_runtime_recovery_outcome(ticket)

    assert outcome is not None
    assert outcome.status is RuntimeRecoveryStatus.CONTROLLER_HOLD
    assert outcome.reason == "incident_budget_expired"


def test_manual_selector_intent_supersedes_inflight_automatic_recovery() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_hot_standby_lock = threading.RLock()
    runtime._core_transition_lock = threading.RLock()
    runtime._runtime_failure_recovery_bindings = set()
    runtime._runtime_failure_recovery_threads = set()
    runtime._runtime_incident_recovery_state = {}
    runtime._runtime_pending_recovered_payloads = {}
    runtime._runtime_route_mutation_lease = None
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_route_epoch = 9
    runtime._runtime_facility_epoch = 0
    runtime._runtime_selector_intent_revision = 4
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_worker_token = lambda: (1,)
    runtime._runtime_worker_token_is_current = lambda _token: True
    runtime._runtime_facility_hold_reason = lambda: ""
    runtime._runtime_node_attribution_hold_reason = lambda: ""
    runtime._apply_confirmed_runtime_health = (
        lambda *_args, **_kwargs: {"accepted": True, "changed": True}
    )
    entered_recovery = threading.Event()
    release_recovery = threading.Event()
    runtime._activate_killswitch_for_egress_failure = (
        lambda _reason: (_ for _ in ()).throw(
            AssertionError("non-Gateway recovery must not activate killswitch")
        )
    )

    def blocked_fast_recovery(*_args, **_kwargs):
        entered_recovery.set()
        release_recovery.wait(1.0)
        return {
            "fast_path_available": True,
            "post_switch_connectivity_verified": True,
            "effective_egress_ok": True,
            "selected_chain": ["FINAL-EXIT", "leaf-c"],
            "alternative_leaf": "leaf-c",
            "post_switch_result": {"status": 204},
            "loaded_route_epoch": 10,
            "selection_action": {
                "group": "FINAL-EXIT",
                "target": "leaf-c",
                "mode": "fixed",
            },
        }

    runtime._fast_recover_confirmed_alternative = blocked_fast_recovery
    runtime._set_effective_egress_readiness_state = lambda *_args: None
    runtime._queue_runtime_route_snapshot_refresh = lambda: None
    runtime._start_runtime_connection_cleanup = lambda _binding: None
    runtime._set_runtime_audit_result = lambda *_args, **_kwargs: None
    failed = RuntimeNodeRef("tier1", "tier1_variant", "failed", "leaf-b")
    alternative = RuntimeNodeRef(
        "tier1", "tier1_variant", "alternative", "leaf-c"
    )
    snapshot = CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="loaded",
        route_epoch=9,
        proxy_nodes=(failed, alternative),
    )
    binding = RuntimeRouteBinding(
        process_pid=snapshot.process_pid,
        process_generation=snapshot.process_generation,
        core_generation=snapshot.core_generation,
        semantic_digest=snapshot.semantic_digest,
        route_epoch=snapshot.route_epoch,
        observed_outbound="FINAL-EXIT",
        route_chain=("FINAL-EXIT", "leaf-b"),
        node=failed,
    )
    failure = BoundRuntimeFailure(
        binding=binding,
        protocol="PROBE",
        observed_outbound="FINAL-EXIT",
        destination_token="opaque",
        error_type="runtime_probe",
        observed_at_monotonic=time.monotonic(),
    )
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
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

    runtime._start_runtime_recovery(
        failure,
        snapshot,
        {},
        incident_id=confirmed.incident_id,
        quarantine_token=confirmed.probe_token,
        confirmed_alternative=alternative,
    )
    assert entered_recovery.wait(0.5)
    runtime._runtime_selector_intent_revision += 1
    release_recovery.set()
    deadline = time.monotonic() + 1.0
    while runtime._runtime_failure_recovery_threads and time.monotonic() < deadline:
        time.sleep(0.005)

    assert runtime._runtime_failure_recovery_threads == set()
    assert runtime._runtime_route_mutation_lease is None
    assert runtime._runtime_incident_recovery_state[confirmed.incident_id] == (
        "manual_superseded"
    )
    assert runtime._runtime_recovery_outcomes[confirmed.incident_id].status is (
        RuntimeRecoveryStatus.MANUAL_SUPERSEDED
    )


def test_hot_standby_transaction_uses_two_seconds_from_incident_binding() -> None:
    runtime, failure, snapshot, payload, refs = _ordered_recovery_fixture()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_hot_standby_lock = threading.RLock()
    runtime._core_transition_lock = threading.RLock()
    runtime._runtime_failure_recovery_bindings = set()
    runtime._runtime_failure_probe_bindings = set()
    runtime._runtime_failure_recovery_threads = set()
    runtime._runtime_incident_recovery_state = {}
    runtime._runtime_pending_recovered_payloads = {}
    runtime._runtime_pending_confirmed_incidents = {}
    runtime._runtime_origin_diagnostic_bindings = {}
    runtime._runtime_deferred_provisional_confirmations = {}
    runtime._runtime_incident_bindings = {}
    runtime._runtime_incident_route_payloads = {}
    runtime._runtime_route_mutation_lease = None
    runtime._runtime_network_context_epoch = snapshot.network_context_epoch
    runtime._runtime_facility_epoch = 0
    runtime._runtime_selector_intent_revision = 0
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
    runtime._runtime_worker_token = lambda: (1,)
    runtime._runtime_worker_token_is_current = lambda _token: True
    runtime._runtime_facility_hold_reason = lambda: ""
    runtime._runtime_recovery_monotonic = (
        lambda: failure.observed_at_monotonic + 0.1
    )
    action = {
        "group": "TIER3-AUTO",
        "target": refs[2].proxy_name,
        "mode": "fixed",
    }
    runtime._runtime_alternative_switch_action = lambda *_args: dict(action)
    runtime._runtime_hot_standby_contract_matches = (
        lambda *_args: True
    )
    captured_deadlines: list[float] = []

    def fast_switch(*_args, deadline_monotonic, **_kwargs):
        captured_deadlines.append(deadline_monotonic)
        return {
            "fast_path_available": True,
            "selection_action": dict(action),
            "post_switch_connectivity_verified": True,
            "effective_egress_ok": True,
            "selected_chain": ["FINAL-EXIT", refs[2].proxy_name],
            "alternative_leaf": refs[2].proxy_name,
            "post_switch_result": {"status": 204},
            "loaded_route_epoch": snapshot.route_epoch + 1,
        }

    runtime._fast_recover_confirmed_alternative = fast_switch
    runtime._set_effective_egress_state = lambda *_args: None
    runtime._start_runtime_independent_probe = lambda *_args, **_kwargs: None
    evidence = _HotStandbyEvidence(
        ref=refs[2],
        action=dict(action),
        snapshot_key=snapshot.snapshot_key,
        network_context_epoch=snapshot.network_context_epoch,
        verified_at_monotonic=failure.observed_at_monotonic,
        latency_ms=10.0,
        probe_result={"delay": 10},
        selection_group="TIER3-AUTO",
        selection_target=refs[2].proxy_name,
        selection_mode="fixed",
    )
    decision = runtime._runtime_egress_coordinator.observe(
        failure,
        current_snapshot=snapshot,
    )

    assert runtime._start_runtime_provisional_diversion(
        failure,
        snapshot,
        payload,
        incident_id=decision.incident_id,
        probe_token=decision.probe_token,
        evidence=evidence,
    ) is True
    deadline = time.monotonic() + 1.0
    while runtime._runtime_failure_recovery_threads and time.monotonic() < deadline:
        time.sleep(0.005)

    assert captured_deadlines == [failure.observed_at_monotonic + 2.0]
    assert runtime._runtime_route_mutation_lease["to"] == refs[2].proxy_name
    assert runtime._runtime_route_mutation_lease["provisional"] is True
    assert runtime._runtime_incident_recovery_state[decision.incident_id] == (
        "diverted"
    )
    assert decision.incident_id not in runtime._runtime_recovery_outcomes


def test_cold_confirmation_and_recovery_share_one_six_second_deadline() -> None:
    runtime, failure, snapshot, payload, refs = _ordered_recovery_fixture()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_failure_probe_bindings = set()
    runtime._runtime_failure_recovery_bindings = set()
    runtime._runtime_failure_recovery_threads = set()
    runtime._runtime_origin_diagnostic_bindings = {}
    runtime._runtime_route_mutation_lease = None
    runtime._runtime_facility_epoch = 0
    runtime._runtime_selector_intent_revision = 0
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
    runtime._runtime_worker_token = lambda: (1,)
    runtime._runtime_worker_token_is_current = lambda _token: True
    runtime._runtime_facility_hold_reason = lambda: ""
    runtime._runtime_origin_diagnostic_binding_for_probe = (
        lambda *_args, **_kwargs: None
    )
    runtime._runtime_confirmation_timeout_ms = lambda *_args: 500
    runtime._probe_runtime_leaf = lambda *_args, **_kwargs: (
        (_ for _ in ()).throw(RuntimeError("selected leaf failed"))
    )
    candidate_deadlines: list[float] = []

    def alternative(*_args, deadline_monotonic, **_kwargs):
        candidate_deadlines.append(deadline_monotonic)
        return refs[2], {"delay": 7}

    runtime._probe_managed_alternative = alternative
    recovered = threading.Event()
    recovery_deadlines: list[float] = []

    def start_recovery(*_args, incident_deadline_monotonic, **_kwargs):
        recovery_deadlines.append(incident_deadline_monotonic)
        recovered.set()

    runtime._start_runtime_recovery = start_recovery
    runtime._record_runtime_facility_degradation = lambda reason: (
        (_ for _ in ()).throw(AssertionError(reason))
    )
    decision = runtime._runtime_egress_coordinator.observe(
        failure,
        current_snapshot=snapshot,
    )

    runtime._start_runtime_independent_probe(
        failure,
        snapshot,
        payload,
        probe_token=decision.probe_token,
    )

    assert recovered.wait(1.0)
    assert candidate_deadlines == [failure.observed_at_monotonic + 4.25]
    assert recovery_deadlines == [failure.observed_at_monotonic + 6.0]


def _activation_authority_fixture(
    *,
    route_epoch: int = 9,
    proxy_name: str = "activation-leaf",
    identity: str = "activation-leaf-id",
) -> tuple[CoreLoadedRouteSnapshot, dict[str, object], RuntimeRouteBinding]:
    ref = RuntimeNodeRef(
        tier="tier3",
        pool_kind="tier3a",
        identity=identity,
        proxy_name=proxy_name,
    )
    snapshot = CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=7,
        core_generation=3,
        semantic_digest="activation-loaded",
        route_epoch=route_epoch,
        network_context_epoch=2,
        group_graph=(("FINAL-EXIT", (ref.proxy_name, "REJECT")),),
        group_states=(
            CoreLoadedGroupState(
                name="FINAL-EXIT",
                type_name="Selector",
                members=(ref.proxy_name, "REJECT"),
                now=ref.proxy_name,
            ),
        ),
        proxy_nodes=(ref,),
    )
    payload: dict[str, object] = {
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
    binding = RuntimeRouteBinding(
        process_pid=snapshot.process_pid,
        process_generation=snapshot.process_generation,
        core_generation=snapshot.core_generation,
        semantic_digest=snapshot.semantic_digest,
        route_epoch=snapshot.route_epoch,
        network_context_epoch=snapshot.network_context_epoch,
        observed_outbound="FINAL-EXIT",
        route_chain=("FINAL-EXIT", ref.proxy_name),
        node=ref,
    )
    return snapshot, payload, binding


def test_explicit_stale_probe_binding_never_falls_forward_to_current_leaf() -> None:
    runtime = object.__new__(DesktopRuntime)
    stale_snapshot, stale_payload, stale_binding = _activation_authority_fixture(
        route_epoch=8,
        proxy_name="failed-a",
        identity="failed-a-id",
    )
    current_snapshot, _current_payload, _current_binding = (
        _activation_authority_fixture(
            route_epoch=9,
            proxy_name="current-b",
            identity="current-b-id",
        )
    )
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_egress_coordinator.set_current_snapshot(current_snapshot)
    runtime._runtime_facility_hold_reason = lambda: ""
    runtime._runtime_node_attribution_hold_reason = lambda: ""
    runtime._record_runtime_facility_degradation = lambda *_args: None
    runtime._start_runtime_facility_confirmation = lambda *_args: None
    current_reads: list[str] = []
    runtime._current_runtime_failure_binding_state = lambda **_kwargs: (
        current_reads.append("current")
        or (_ for _ in ()).throw(
            AssertionError("explicit stale A evidence must not bind current B")
        )
    )
    refreshes: list[str] = []
    runtime._queue_runtime_route_snapshot_refresh = lambda: refreshes.append(
        "refresh"
    )

    submitted = runtime._submit_current_route_probe_failure(
        "late failure from A",
        source="activation_final_exit",
        binding_state=(stale_binding, stale_snapshot, stale_payload),
    )

    assert submitted is False
    assert current_reads == []
    assert refreshes == ["refresh"]


def test_revalidation_queue_coalesces_same_authority_and_holds_terminal_result() -> None:
    runtime = object.__new__(DesktopRuntime)
    authority_key = [("route-a", 1)]
    runtime._revalidation_mode_for_runtime_state = (
        lambda _mode, *, source: "incremental"
    )
    runtime._tier3_standby_activation_key = lambda: authority_key[0]
    runtime._tier3_egress_activation_lock = threading.Lock()
    runtime._tier3_egress_activation_pending = False
    runtime._tier3_egress_activation_pending_mode = ""
    runtime._tier3_egress_activation_pending_key = None
    runtime._tier3_egress_activation_active_mode = "incremental"
    runtime._tier3_egress_activation_active_key = (
        "incremental",
        authority_key[0],
    )
    runtime._tier3_egress_activation_worker_running = True
    runtime._tier3_egress_activation_generation = 7
    runtime._tier3_standby_activation_failure_key = None
    runtime._tier3_standby_activation_facility_hold = False
    runtime.stop_event = threading.Event()

    runtime._queue_egress_revalidation("incremental")
    runtime._queue_egress_revalidation("incremental")

    assert runtime._tier3_egress_activation_generation == 7
    assert runtime._tier3_egress_activation_pending is False
    authority_key[0] = ("route-a", 2)
    runtime._queue_egress_revalidation("incremental")

    # An authority/provider successor is queued without cancelling the active
    # lease. The worker advances the generation only when it claims that
    # successor.
    assert runtime._tier3_egress_activation_generation == 7
    assert runtime._tier3_egress_activation_pending is True
    assert runtime._tier3_egress_activation_pending_key == (
        "incremental",
        authority_key[0],
    )

    runtime._tier3_egress_activation_pending = False
    runtime._tier3_egress_activation_pending_mode = ""
    runtime._tier3_egress_activation_pending_key = None
    runtime._tier3_egress_activation_worker_running = False
    runtime._tier3_egress_activation_active_key = None
    runtime._tier3_standby_activation_failure_key = authority_key[0]
    runtime._tier3_standby_activation_facility_hold = True

    runtime._queue_egress_revalidation("incremental")

    assert runtime._tier3_egress_activation_generation == 7
    assert runtime._tier3_egress_activation_pending is False


def test_ready_commit_consumes_pre_ready_activation_successor() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._runtime_egress_recovery_profile = "early_single_coordinator"
    runtime._tier3_egress_activation_lock = threading.RLock()
    runtime._tier3_egress_activation_active_generation = 9
    runtime._tier3_egress_activation_pending = True
    runtime._tier3_egress_activation_pending_mode = "activation"
    runtime._tier3_egress_activation_pending_key = (
        "activation",
        ("provider-version", 2),
    )
    stages: list[str] = []
    runtime._record_tier3_initial_activation_stage = (
        lambda stage, **_kwargs: stages.append(stage) or {}
    )
    lease = _InitialEgressAdmissionLease(
        activation_generation=9,
        lifecycle_revision=1,
        runtime_generation=1,
        core_generation=3,
        process_pid=123,
        process_generation=4,
        selector_intent_revision=0,
        worker_token=(1, 3, 123, 4),
        deadline_monotonic=time.monotonic() + 5.0,
    )

    assert runtime._coalesce_pre_ready_activation_successor(lease) is True
    assert runtime._tier3_egress_activation_pending is False
    assert runtime._tier3_egress_activation_pending_mode == ""
    assert runtime._tier3_egress_activation_pending_key is None
    assert stages == ["ready_successor_coalesced"]


def test_activation_generation_marker_alone_never_freezes_provider_as_online() -> None:
    runtime = object.__new__(DesktopRuntime)
    runtime._core_lifecycle_lock = threading.RLock()
    runtime._core_generation = 12
    runtime._runtime_initial_activation_generation = 12
    runtime._runtime_egress_readiness = SimpleNamespace(
        state=RuntimeEgressReadinessState.ACTIVATION_HOLD,
        validation_active=True,
    )
    runtime.manager = SimpleNamespace(effective_egress_ok=None)

    assert runtime._runtime_initial_activation_was_completed() is False

    runtime._runtime_egress_readiness = SimpleNamespace(
        state=RuntimeEgressReadinessState.READY,
        validation_active=False,
    )
    runtime.manager.effective_egress_ok = True
    assert runtime._runtime_initial_activation_was_completed() is True


def test_initial_admission_reserves_two_seconds_for_controller_authority() -> None:
    runtime = object.__new__(DesktopRuntime)
    runner = _Runner()
    runtime.core_runner = runner
    runtime.stop_event = threading.Event()
    runtime._core_lifecycle_lock = threading.RLock()
    runtime._core_generation = 3
    runtime._lifecycle_revision = 5
    runtime._runtime_generation = 6
    runtime._runtime_selector_intent_revision = 0
    runtime._runtime_traffic_capture_wanted = lambda: True
    runtime._disarm_runtime_recovery = lambda _reason="": None
    runtime._record_tier3_initial_activation_stage = lambda *_args, **_kwargs: {}
    runtime._publish_runtime_egress_readiness = lambda *_args, **_kwargs: None
    observed: list[dict[str, object]] = []

    def authority(**kwargs):
        observed.append(dict(kwargs))
        raise RuntimeControllerReadbackError("synthetic controller stop")

    runtime._tier3_activation_authority_state = authority
    lease = _InitialEgressAdmissionLease(
        activation_generation=9,
        lifecycle_revision=5,
        runtime_generation=6,
        core_generation=3,
        process_pid=4312,
        process_generation=7,
        selector_intent_revision=0,
        worker_token=(6, 3, 4312, 7),
        deadline_monotonic=time.monotonic() + 8.0,
    )

    try:
        runtime._run_initial_egress_activation_rollback(
            runner=runner,
            activation_generation=9,
            core_generation=3,
            selector_intent_revision=0,
            admission_lease=lease,
        )
    except RuntimeControllerReadbackError as exc:
        assert "synthetic controller stop" in str(exc)
    else:
        raise AssertionError("the synthetic controller readback must stop admission")

    assert len(observed) == 1
    assert observed[0]["timeout_seconds"] == 2.0
    assert observed[0]["require_managed_final_exit"] is False


def test_final_exit_proxy_delay_freezes_binding_before_probe() -> None:
    runtime = object.__new__(DesktopRuntime)
    snapshot, payload, binding = _activation_authority_fixture(
        route_epoch=8,
        proxy_name="failed-a",
        identity="failed-a-id",
    )
    binding_reads: list[dict[str, object]] = []
    runtime._current_runtime_failure_binding_state = lambda **kwargs: (
        binding_reads.append(dict(kwargs)) or (binding, snapshot, payload)
    )
    runtime._default_probe_url = lambda: "https://probe.invalid/generate_204"
    runtime._probe_proxy_delay_with_urls = lambda *_args, **_kwargs: (
        (_ for _ in ()).throw(RuntimeError("late failure from A"))
    )
    submitted: list[dict[str, object]] = []
    runtime._submit_current_route_probe_failure = lambda _reason, **kwargs: (
        submitted.append(dict(kwargs)) or False
    )

    try:
        runtime.test_proxy_delay("FINAL-EXIT", timeout_ms=800)
    except RuntimeError as exc:
        assert "late failure from A" in str(exc)
    else:
        raise AssertionError("the injected FINAL-EXIT probe must fail")

    assert binding_reads == [{"timeout_seconds": 0.75, "refresh": True}]
    assert submitted == [
        {
            "source": "proxy_delay",
            "binding_state": (binding, snapshot, payload),
            "binding_captured": True,
        }
    ]


def test_stdout_guard_freezes_binding_before_async_probe() -> None:
    runtime = object.__new__(DesktopRuntime)
    snapshot, payload, binding = _activation_authority_fixture(
        route_epoch=8,
        proxy_name="failed-a",
        identity="failed-a-id",
    )
    runtime.core_runner = _Runner()
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_failure_guard_probe_last_at = 0.0
    runtime._runtime_facility_hold_reason = lambda: ""
    runtime._record_runtime_facility_degradation = lambda reason: (
        (_ for _ in ()).throw(AssertionError(reason))
    )
    runtime._runtime_worker_token = lambda: (1,)
    runtime._runtime_worker_token_is_current = lambda _token: True
    binding_reads: list[dict[str, object]] = []
    runtime._current_runtime_failure_binding_state = lambda **kwargs: (
        binding_reads.append(dict(kwargs)) or (binding, snapshot, payload)
    )
    runtime._probe_mihomo_mixed_port_once = lambda **_kwargs: (
        (_ for _ in ()).throw(RuntimeError("late guard failure from A"))
    )
    submitted: list[dict[str, object]] = []
    submitted_event = threading.Event()

    def submit(_reason, **kwargs):
        submitted.append(dict(kwargs))
        submitted_event.set()
        return False

    runtime._submit_current_route_probe_failure = submit

    runtime._maybe_probe_runtime_failure_guard()

    assert submitted_event.wait(1.0)
    assert binding_reads == [{"timeout_seconds": 0.75, "refresh": True}]
    assert submitted == [
        {
            "source": "stdout_guard",
            "binding_state": (binding, snapshot, payload),
            "binding_captured": True,
        }
    ]


def test_activation_authority_uses_complete_matching_tuple_without_refresh() -> None:
    runtime = object.__new__(DesktopRuntime)
    snapshot, payload, _binding = _activation_authority_fixture()
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
    runtime._runtime_route_snapshot_state = lambda: (snapshot, payload)
    refresh_calls: list[dict[str, object]] = []
    runtime._refresh_loaded_runtime_authority = lambda **kwargs: (
        refresh_calls.append(dict(kwargs))
        or (_ for _ in ()).throw(
            AssertionError("matching activation authority must not refresh")
        )
    )

    actual_snapshot, actual_payload, actual_binding = (
        runtime._tier3_activation_authority_state(worker_token=(1,))
    )

    assert actual_snapshot is snapshot
    assert actual_payload is payload
    assert actual_binding.snapshot_key == snapshot.snapshot_key
    assert actual_binding.node.proxy_name == "activation-leaf"
    assert refresh_calls == []


def test_activation_authority_mismatch_refreshes_once_and_uses_returned_tuple() -> None:
    runtime = object.__new__(DesktopRuntime)
    stale, stale_payload, _stale_binding = _activation_authority_fixture(
        route_epoch=8
    )
    fresh, fresh_payload, fresh_binding = _activation_authority_fixture(
        route_epoch=9
    )
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_egress_coordinator.set_current_snapshot(stale)
    runtime._runtime_route_snapshot_state = lambda: (fresh, stale_payload)
    refresh_calls: list[dict[str, object]] = []

    def refresh(**kwargs):
        refresh_calls.append(dict(kwargs))
        runtime._runtime_egress_coordinator.set_current_snapshot(fresh)
        return fresh, fresh_payload, fresh_binding

    runtime._refresh_loaded_runtime_authority = refresh

    authority = runtime._tier3_activation_authority_state(worker_token=(4, 2))

    assert authority == (fresh, fresh_payload, fresh_binding)
    assert refresh_calls == [
        {
            "timeout_seconds": 0.75,
            "require_managed_final_exit": True,
            "worker_token": (4, 2),
        }
    ]


def test_activation_authority_missing_payload_holds_after_one_bounded_refresh() -> None:
    runtime = object.__new__(DesktopRuntime)
    snapshot, _payload, _binding = _activation_authority_fixture()
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
    runtime._runtime_route_snapshot_state = lambda: (snapshot, None)
    refresh_calls: list[dict[str, object]] = []
    route_probes: list[str] = []
    runtime._probe_mihomo_mixed_port = lambda: route_probes.append("probe")
    runtime._refresh_loaded_runtime_authority = lambda **kwargs: (
        refresh_calls.append(dict(kwargs)) or None
    )

    try:
        runtime._tier3_activation_authority_state(worker_token=(7,))
    except RuntimeControllerReadbackError as exc:
        assert "750 ms" in str(exc)
    else:
        raise AssertionError("missing activation payload must enter controller hold")

    assert len(refresh_calls) == 1
    assert route_probes == []


def test_activation_worker_missing_payload_holds_without_probe_or_incident() -> None:
    runtime = object.__new__(DesktopRuntime)
    snapshot, _payload, _binding = _activation_authority_fixture()
    runtime.logger = logging.getLogger("test-activation-authority-worker")
    runtime.stop_event = threading.Event()
    runtime.core_runner = _Runner()
    runtime._core_lifecycle_lock = threading.RLock()
    runtime._core_generation = snapshot.core_generation
    runtime._runtime_recovery_pressure = None
    runtime._tier3_egress_activation_lock = threading.RLock()
    runtime._tier3_egress_activation_pending = True
    runtime._tier3_egress_activation_pending_mode = "incremental"
    frozen_authority_key = ("route-a", 1)
    runtime._tier3_egress_activation_pending_key = (
        "incremental",
        frozen_authority_key,
    )
    runtime._tier3_egress_activation_active_key = None
    runtime._tier3_egress_activation_active_mode = ""
    runtime._tier3_egress_activation_generation = 3
    runtime._tier3_egress_activation_worker_running = True
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
    runtime._runtime_route_snapshot_state = lambda: (snapshot, None)
    runtime._runtime_worker_token = lambda: (4, 2)
    runtime._tier3_standby_activation_key = lambda: frozen_authority_key
    runtime._revalidation_mode_for_runtime_state = (
        lambda mode, **_kwargs: mode
    )
    refresh_calls: list[dict[str, object]] = []
    runtime._refresh_loaded_runtime_authority = lambda **kwargs: (
        refresh_calls.append(dict(kwargs)) or None
    )
    forbidden: list[str] = []
    runtime._verify_runtime_final_exit = lambda **_kwargs: forbidden.append(
        "final_204"
    )
    runtime._verify_tier3_final_exit_for_activation = (
        lambda **_kwargs: forbidden.append("recovery")
    )
    runtime._runtime_recovery_tickets = {}
    holds: list[tuple[str, tuple[object, ...] | None]] = []
    runtime._publish_tier3_controller_hold = (
        lambda reason, **kwargs: (
            holds.append((str(reason), kwargs.get("authority_key"))) or {}
        )
    )

    runtime._tier3_egress_activation_worker()

    assert len(refresh_calls) == 1
    assert forbidden == []
    assert runtime._runtime_recovery_tickets == {}
    assert len(holds) == 1
    assert holds[0][1] == frozen_authority_key
    assert runtime._tier3_egress_activation_worker_running is False


def test_activation_worker_does_not_publish_old_failure_over_new_authority() -> None:
    runtime = object.__new__(DesktopRuntime)
    snapshot, _payload, _binding = _activation_authority_fixture()
    runtime.logger = logging.getLogger("test-activation-authority-superseded")
    runtime.stop_event = threading.Event()
    runtime.core_runner = _Runner()
    runtime._core_lifecycle_lock = threading.RLock()
    runtime._core_generation = snapshot.core_generation
    runtime._runtime_recovery_pressure = None
    runtime._tier3_egress_activation_lock = threading.RLock()
    old_key = ("route-a", 1)
    new_key = ("route-b", 2)
    live_key = [old_key]
    runtime._tier3_egress_activation_pending = True
    runtime._tier3_egress_activation_pending_mode = "incremental"
    runtime._tier3_egress_activation_pending_key = (
        "incremental",
        old_key,
    )
    runtime._tier3_egress_activation_active_key = None
    runtime._tier3_egress_activation_active_mode = ""
    runtime._tier3_egress_activation_generation = 3
    runtime._tier3_egress_activation_worker_running = True
    runtime._tier3_standby_activation_failure_key = None
    runtime._tier3_standby_activation_facility_hold = False
    runtime._runtime_worker_token = lambda: (4, 2)
    runtime._tier3_standby_activation_key = lambda: live_key[0]
    runtime._revalidation_mode_for_runtime_state = (
        lambda mode, **_kwargs: mode
    )
    authority_calls: list[tuple[object, ...]] = []

    def read_authority(*, worker_token):
        authority_calls.append(tuple(worker_token))
        if len(authority_calls) == 1:
            live_key[0] = new_key
            with runtime._tier3_egress_activation_lock:
                runtime._tier3_egress_activation_pending = True
                runtime._tier3_egress_activation_pending_mode = "incremental"
                runtime._tier3_egress_activation_pending_key = (
                    "incremental",
                    new_key,
                )
        raise RuntimeControllerReadbackError("controller unavailable")

    runtime._tier3_activation_authority_state = read_authority
    holds: list[tuple[str, tuple[object, ...] | None]] = []
    runtime._publish_tier3_controller_hold = (
        lambda reason, **kwargs: (
            holds.append((str(reason), kwargs.get("authority_key"))) or {}
        )
    )

    runtime._tier3_egress_activation_worker()

    assert authority_calls == [(4, 2), (4, 2)]
    assert holds == [("controller unavailable", new_key)]
    assert runtime._tier3_egress_activation_generation == 4
    assert runtime._tier3_egress_activation_worker_running is False


def test_activation_terminal_key_does_not_consume_newer_authority_event() -> None:
    runtime = object.__new__(DesktopRuntime)
    frozen_key = ("route-a", 1)
    current_key = ("route-b", 2)
    runtime._tier3_egress_activation_lock = threading.Lock()
    runtime._tier3_standby_activation_failure_key = None
    runtime._tier3_standby_activation_retry_stage = 0
    runtime._tier3_standby_activation_retry_at = 0.0
    runtime._tier3_standby_activation_facility_hold = False
    runtime._tier3_standby_activation_hold_kind = ""
    runtime._tier3_standby_activation_last_reason = ""
    runtime._tier3_runtime_qualification_retry_at = 0.0
    runtime._runtime_local_data_plane_blocked = lambda: False
    runtime._runtime_facility_hold_reason = lambda: ""
    runtime._tier3_standby_activation_key = lambda: current_key
    runtime._revalidation_mode_for_runtime_state = (
        lambda _mode, *, source: "incremental"
    )
    runtime._tier3_egress_activation_pending = False
    runtime._tier3_egress_activation_pending_mode = ""
    runtime._tier3_egress_activation_pending_key = None
    runtime._tier3_egress_activation_active_mode = ""
    runtime._tier3_egress_activation_active_key = None
    runtime._tier3_egress_activation_worker_running = True
    runtime._tier3_egress_activation_generation = 7
    runtime.stop_event = threading.Event()

    state = runtime._record_tier3_standby_activation_failure(
        "route A failed",
        authority_key=frozen_key,
    )
    runtime._queue_egress_revalidation("incremental")

    assert state["failure_key"] == frozen_key
    assert runtime._tier3_standby_activation_failure_key == frozen_key
    assert runtime._tier3_egress_activation_pending is True
    assert runtime._tier3_egress_activation_pending_key == (
        "incremental",
        current_key,
    )
    assert runtime._tier3_egress_activation_generation == 7


def test_activation_failure_and_indeterminate_are_event_gated_by_failure_key() -> None:
    for recorder_name in (
        "_record_tier3_standby_activation_failure",
        "_record_tier3_standby_activation_indeterminate",
    ):
        runtime = object.__new__(DesktopRuntime)
        runtime._tier3_egress_activation_lock = threading.Lock()
        runtime._tier3_standby_activation_failure_key = None
        runtime._tier3_standby_activation_retry_stage = 0
        runtime._tier3_standby_activation_retry_at = 0.0
        runtime._tier3_standby_activation_facility_hold = False
        runtime._tier3_standby_activation_hold_kind = ""
        runtime._tier3_standby_activation_last_reason = ""
        runtime._tier3_runtime_qualification_retry_at = 0.0
        failure_key = [["route-a", 3]]
        runtime._tier3_standby_activation_key = lambda: tuple(failure_key[0])
        runtime._runtime_local_data_plane_blocked = lambda: False
        runtime._runtime_facility_hold_reason = lambda: ""
        runtime.manager = SimpleNamespace(
            tier3_startup_gate_satisfied=True,
            effective_egress_ok=False,
        )
        runtime.core_runner = _Runner()
        runtime._current_core_runtime_phase = lambda: "formal_no_capture"
        runtime._egress_revalidation_active = lambda: False
        runtime._runtime_traffic_capture_wanted = lambda: True
        queued: list[str] = []
        runtime._queue_tier3_egress_activation = lambda: queued.append("queued")

        state = getattr(runtime, recorder_name)("same immutable failure")
        runtime._maybe_retry_tier3_standby_activation()
        runtime._maybe_retry_tier3_standby_activation()

        assert state["retry_at"] == 0.0
        assert runtime._tier3_standby_activation_facility_hold is True
        assert queued == []

        failure_key[0] = ["route-a", 4]
        runtime._maybe_retry_tier3_standby_activation()

        assert queued == ["queued"]
        assert runtime._tier3_standby_activation_failure_key is None


def test_terminal_ticket_reopens_only_for_material_route_authority_change() -> None:
    runtime = object.__new__(DesktopRuntime)
    base_snapshot, base_payload, base_binding = _activation_authority_fixture()
    alternative = RuntimeNodeRef(
        tier="tier3",
        pool_kind="tier3a",
        identity="activation-candidate-id",
        proxy_name="activation-candidate",
    )
    snapshot = CoreLoadedRouteSnapshot(
        process_pid=base_snapshot.process_pid,
        process_generation=base_snapshot.process_generation,
        core_generation=base_snapshot.core_generation,
        semantic_digest=base_snapshot.semantic_digest,
        route_epoch=base_snapshot.route_epoch,
        network_context_epoch=base_snapshot.network_context_epoch,
        proxy_nodes=(base_binding.node, alternative),
    )
    payload = {
        **base_payload,
        "proxies": {
            **base_payload["proxies"],
            "FINAL-EXIT": {
                **base_payload["proxies"]["FINAL-EXIT"],
                "all": [
                    base_binding.node.proxy_name,
                    alternative.proxy_name,
                    "REJECT",
                ],
            },
            alternative.proxy_name: {"type": "Shadowsocks"},
        },
        "proxy_map": {
            **base_payload["proxy_map"],
            alternative.proxy_name: {
                "tier": alternative.tier,
                "pool_kind": alternative.pool_kind,
                "identity": alternative.identity,
            },
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
        route_chain=("FINAL-EXIT", base_binding.node.proxy_name),
        node=base_binding.node,
    )
    runtime.core_runner = _Runner()
    runtime._core_lifecycle_lock = threading.RLock()
    runtime._core_generation = snapshot.core_generation
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_route_snapshot = snapshot
    runtime._runtime_route_snapshot_payload = payload
    runtime._runtime_loaded_export_contract = {
        "semantic_digest": snapshot.semantic_digest,
        "provider_semantic_digest": "provider-a",
    }
    runtime._runtime_network_context_epoch = snapshot.network_context_epoch
    health_revision = [5]
    alternative_eligible = [True]
    runtime.manager = SimpleNamespace(
        runtime_candidate_snapshot=lambda: SimpleNamespace(
            revision=health_revision[0],
            runtime_generation=2,
            candidates=(
                SimpleNamespace(key=binding.node.key, eligible=True),
                SimpleNamespace(
                    key=alternative.key,
                    eligible=alternative_eligible[0],
                ),
            ),
        )
    )
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_recovery_tickets = {}
    runtime._runtime_recovery_outcomes = {}
    runtime._runtime_recovery_outcome_events = {}
    runtime._runtime_incident_recovery_state = {}
    runtime._runtime_recovery_ticket_revision = 0
    runtime._runtime_facility_epoch = 0
    runtime._runtime_selector_intent_revision = 0
    runtime._runtime_worker_token = lambda: (1,)
    runtime._runtime_facility_hold_reason = lambda: ""
    runtime._runtime_node_attribution_hold_reason = lambda: ""
    runtime._current_runtime_failure_binding_state = lambda **_kwargs: (
        binding,
        snapshot,
        payload,
    )
    runtime._fresh_runtime_hot_standby = lambda *_args, **_kwargs: None
    starts: list[str] = []
    runtime._start_runtime_independent_probe = (
        lambda *_args, **_kwargs: starts.append("probe")
    )
    runtime._queue_runtime_route_snapshot_refresh = lambda: None

    first = runtime._submit_current_route_probe_failure("first monitor tick")
    assert starts == ["probe"]
    incident_id = first.incident_id
    runtime._runtime_recovery_outcomes[incident_id] = RuntimeRecoveryOutcome(
        status=RuntimeRecoveryStatus.CONTROLLER_HOLD,
        incident_id=incident_id,
        route_epoch=snapshot.route_epoch,
        budget_ms=20,
        reason="controller_authority_unavailable",
        from_ref=binding.node,
    )
    before_duplicate = (
        runtime._runtime_egress_coordinator.current_snapshot_for(binding)
    )
    assert before_duplicate is not None

    second = runtime._submit_current_route_probe_failure("same monitor tick")

    assert second is first
    assert starts == ["probe"]
    after_duplicate = (
        runtime._runtime_egress_coordinator.current_snapshot_for(binding)
    )
    assert after_duplicate is not None
    assert after_duplicate.failure_count == before_duplicate.failure_count
    assert (
        after_duplicate.last_observed_monotonic
        == before_duplicate.last_observed_monotonic
    )

    health_revision[0] += 1
    same_candidates = runtime._submit_current_route_probe_failure(
        "raw health revision only"
    )

    assert same_candidates is first
    assert starts == ["probe"]

    alternative_eligible[0] = False
    health_revision[0] += 1
    third = runtime._submit_current_route_probe_failure(
        "eligible candidate set changed"
    )

    assert third is first
    assert starts == ["probe"]

    runtime._runtime_loaded_export_contract = {
        "semantic_digest": snapshot.semantic_digest,
        "provider_semantic_digest": "provider-b",
    }
    fourth = runtime._submit_current_route_probe_failure(
        "loaded Provider contract changed"
    )

    assert fourth is first
    assert starts == ["probe"]

    runtime._runtime_network_context_epoch += 1
    fifth = runtime._submit_current_route_probe_failure(
        "network authority changed"
    )

    assert fifth is not first
    assert fifth.generation > first.generation
    assert starts == ["probe", "probe"]


def test_terminal_outcome_during_origin_probe_blocks_late_circuit_and_health() -> None:
    runtime, failure, snapshot, payload, refs = _ordered_recovery_fixture()
    runtime.stop_event = threading.Event()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_failure_probe_bindings = set()
    runtime._runtime_failure_recovery_bindings = set()
    runtime._runtime_failure_recovery_threads = set()
    runtime._runtime_origin_diagnostic_bindings = {}
    runtime._runtime_route_mutation_lease = None
    runtime._runtime_incident_recovery_state = {}
    runtime._runtime_selector_intent_revision = 0
    runtime._runtime_facility_epoch = 0
    runtime._runtime_recovery_tickets = {}
    runtime._runtime_recovery_outcomes = {}
    runtime._runtime_recovery_outcome_events = {}
    runtime._runtime_recovery_ticket_revision = 0
    runtime._runtime_worker_token = lambda: (1,)
    runtime._runtime_worker_token_is_current = lambda _token: True
    runtime._runtime_facility_hold_reason = lambda: ""
    runtime._runtime_origin_diagnostic_binding_for_probe = (
        lambda *_args, **_kwargs: None
    )
    runtime._runtime_confirmation_timeout_ms = lambda *_args: 500
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)

    health_writes: list[dict[str, object]] = []
    runtime.manager = SimpleNamespace(
        runtime_candidate_snapshot=lambda: SimpleNamespace(
            revision=5,
            runtime_generation=9,
        ),
        apply_runtime_health_observation=lambda observation: (
            health_writes.append(dict(observation))
            or {"accepted": True, "changed": True}
        ),
    )
    probe_entered = threading.Event()
    release_probe = threading.Event()

    def blocked_failed_origin(*_args, **_kwargs):
        probe_entered.set()
        assert release_probe.wait(1.0)
        raise RuntimeError("old origin failed after terminal hold")

    runtime._probe_runtime_leaf = blocked_failed_origin
    runtime._probe_managed_alternative = lambda *_args, **_kwargs: (
        refs[2],
        {"delay": 7},
    )
    recoveries: list[str] = []
    runtime._start_runtime_recovery = lambda *_args, **_kwargs: (
        recoveries.append("started")
    )
    runtime._record_runtime_facility_degradation = lambda _reason: {}
    opened = runtime._runtime_egress_coordinator.observe(
        failure,
        current_snapshot=snapshot,
    )

    runtime._start_runtime_independent_probe(
        failure,
        snapshot,
        payload,
        probe_token=opened.probe_token,
    )
    assert probe_entered.wait(0.5)
    ticket = runtime._runtime_recovery_tickets[opened.incident_id]
    outcome = runtime._complete_runtime_recovery_outcome(
        opened.incident_id,
        RuntimeRecoveryStatus.CONTROLLER_HOLD,
        reason_code="controller_readback_lost",
        expected_ticket=ticket,
    )
    assert outcome is not None
    release_probe.set()

    deadline = time.monotonic() + 1.0
    while (
        failure.circuit_key in runtime._runtime_failure_probe_bindings
        and time.monotonic() < deadline
    ):
        time.sleep(0.005)

    circuit = runtime._runtime_egress_coordinator.current_snapshot_for(
        failure.binding
    )
    assert circuit is not None
    assert circuit.confirmed_monotonic == 0.0
    assert recoveries == []
    assert health_writes == []
    assert runtime._runtime_recovery_outcomes[opened.incident_id] is outcome


def test_terminal_outcome_blocks_late_recovery_health_admission() -> None:
    runtime, failure, snapshot, payload, refs = _ordered_recovery_fixture()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_failure_recovery_bindings = set()
    runtime._runtime_failure_recovery_threads = set()
    runtime._runtime_pending_confirmed_incidents = {}
    runtime._runtime_route_mutation_lease = None
    runtime._runtime_incident_recovery_state = {}
    runtime._runtime_selector_intent_revision = 0
    runtime._runtime_facility_epoch = 0
    runtime._runtime_recovery_tickets = {}
    runtime._runtime_recovery_outcomes = {}
    runtime._runtime_recovery_outcome_events = {}
    runtime._runtime_recovery_ticket_revision = 0
    runtime._runtime_worker_token = lambda: (1,)
    runtime._runtime_worker_token_is_current = lambda _token: True
    runtime._runtime_facility_hold_reason = lambda: ""
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
    opened = runtime._runtime_egress_coordinator.observe(
        failure,
        current_snapshot=snapshot,
    )
    ticket = runtime._register_runtime_recovery_ticket(
        failure,
        snapshot,
        opened.incident_id,
    )
    outcome = runtime._complete_runtime_recovery_outcome(
        opened.incident_id,
        RuntimeRecoveryStatus.CONTROLLER_HOLD,
        reason_code="controller_readback_lost",
        expected_ticket=ticket,
    )
    assert outcome is not None

    health_writes: list[dict[str, object]] = []
    runtime.manager = SimpleNamespace(
        apply_runtime_health_observation=lambda observation: (
            health_writes.append(dict(observation))
            or {"accepted": True, "changed": True}
        )
    )

    runtime._start_runtime_recovery(
        failure,
        snapshot,
        payload,
        incident_id=opened.incident_id,
        quarantine_token=opened.probe_token,
        confirmed_alternative=refs[2],
    )

    assert runtime._runtime_failure_recovery_bindings == set()
    assert runtime._runtime_failure_recovery_threads == set()
    assert health_writes == []
    assert runtime._runtime_recovery_outcomes[opened.incident_id] is outcome


def test_manual_selector_revision_blocks_confirmed_health_writeback() -> None:
    runtime, failure, snapshot, payload, _refs = _ordered_recovery_fixture()
    runtime._core_transition_lock = threading.RLock()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_route_snapshot = snapshot
    runtime._runtime_network_context_epoch = snapshot.network_context_epoch
    runtime._runtime_generation = 9
    runtime._runtime_facility_epoch = 0
    runtime._runtime_selector_intent_revision = 0
    runtime._runtime_recovery_tickets = {}
    runtime._runtime_recovery_outcomes = {}
    runtime._runtime_recovery_outcome_events = {}
    runtime._runtime_recovery_ticket_revision = 0
    runtime._runtime_worker_token = lambda: (1,)
    runtime._runtime_worker_token_is_current = lambda _token: True
    runtime._runtime_facility_hold_reason = lambda: ""
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
    writes: list[dict[str, object]] = []
    runtime.manager = SimpleNamespace(
        runtime_generation=9,
        apply_runtime_health_observation=lambda observation: (
            writes.append(dict(observation))
            or {"accepted": True, "changed": True}
        ),
    )
    opened = runtime._runtime_egress_coordinator.observe(
        failure,
        current_snapshot=snapshot,
    )
    ticket = runtime._register_runtime_recovery_ticket(
        failure,
        snapshot,
        opened.incident_id,
    )
    runtime._runtime_selector_intent_revision = 1

    transition = runtime._apply_confirmed_runtime_health(
        failure,
        snapshot,
        payload,
        incident_id=opened.incident_id,
        quarantine_token=opened.probe_token,
        expected_facility_epoch=0,
        expected_ticket=ticket,
        deadline_monotonic=ticket.deadline_monotonic,
    )

    assert transition["accepted"] is False
    assert transition["action"] == "terminal"
    assert writes == []


def test_terminal_outcome_during_prewarm_prevents_late_selector_put() -> None:
    runtime, failure, snapshot, payload, refs = _ordered_recovery_fixture()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_recovery_tickets = {}
    runtime._runtime_recovery_outcomes = {}
    runtime._runtime_recovery_outcome_events = {}
    runtime._runtime_recovery_ticket_revision = 0
    runtime._runtime_facility_epoch = 0
    runtime._runtime_selector_intent_revision = 0
    runtime._runtime_worker_token = lambda: (1,)
    runtime._runtime_worker_token_is_current = lambda _token: True
    runtime._runtime_facility_hold_reason = lambda: ""
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
    opened = runtime._runtime_egress_coordinator.observe(
        failure,
        current_snapshot=snapshot,
    )
    ticket = runtime._register_runtime_recovery_ticket(
        failure,
        snapshot,
        opened.incident_id,
    )
    runtime._runtime_alternative_switch_action = lambda *_args: {
        "group": "TIER3-AUTO",
        "target": refs[2].proxy_name,
        "previous": failure.binding.node.proxy_name,
        "mode": "fixed",
    }
    prewarm_entered = threading.Event()
    release_prewarm = threading.Event()

    def blocked_prewarm(*_args, **_kwargs):
        prewarm_entered.set()
        assert release_prewarm.wait(1.0)
        return {"delay": 7}

    runtime._prewarm_runtime_switch_target = blocked_prewarm
    puts: list[dict[str, object]] = []
    runtime._apply_runtime_automatic_selection = (
        lambda action, **_kwargs: puts.append(dict(action)) or {}
    )
    result: dict[str, object] = {}

    def recover() -> None:
        result.update(
            runtime._fast_recover_confirmed_alternative(
                failure,
                snapshot,
                payload,
                refs[2],
                incident_id=opened.incident_id,
                selector_intent_revision=0,
                deadline_monotonic=failure.observed_at_monotonic + 6.0,
                recovery_ticket=ticket,
            )
        )

    worker = threading.Thread(target=recover)
    worker.start()
    assert prewarm_entered.wait(0.5)
    terminal = runtime._complete_runtime_recovery_outcome(
        opened.incident_id,
        RuntimeRecoveryStatus.CONTROLLER_HOLD,
        reason_code="controller_readback_lost",
        expected_ticket=ticket,
    )
    assert terminal is not None
    release_prewarm.set()
    worker.join(timeout=1.0)

    assert worker.is_alive() is False
    assert puts == []
    assert result["stale"] is True


def test_terminal_outcome_blocks_late_physical_fail_closed_commit() -> None:
    runtime, _failure, _old_snapshot, _payload, _refs = (
        _ordered_recovery_fixture()
    )
    snapshot, _binding, failure = _fixed_gateway_failure_fixture(
        route_epoch=12
    )
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_route_epoch = snapshot.route_epoch
    runtime._runtime_recovery_tickets = {}
    runtime._runtime_recovery_outcomes = {}
    runtime._runtime_recovery_outcome_events = {}
    runtime._runtime_recovery_ticket_revision = 0
    runtime._runtime_incident_recovery_state = {}
    runtime._runtime_core_stopped_recovery_owners = {}
    runtime._runtime_route_mutation_lease = None
    runtime._runtime_facility_epoch = 0
    runtime._runtime_selector_intent_revision = 0
    runtime._runtime_worker_token = lambda: (1,)
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
    ticket = runtime._register_runtime_recovery_ticket(
        failure,
        snapshot,
        "terminal-before-fail-closed",
    )
    terminal = runtime._complete_runtime_recovery_outcome(
        ticket.incident_id,
        RuntimeRecoveryStatus.CONTROLLER_HOLD,
        reason_code="controller_readback_lost",
        expected_ticket=ticket,
    )
    assert terminal is not None

    disposition = runtime._commit_runtime_fail_closed_outcome(
        physical={"route_verified": True, "enforced": True},
        incident_id=ticket.incident_id,
        from_proxy=failure.binding.node.proxy_name,
        selector_intent_revision=0,
        expected_ticket=ticket,
    )

    assert disposition == "superseded"
    assert runtime._runtime_route_mutation_lease is None
    assert runtime._runtime_incident_recovery_state == {}
    assert runtime._runtime_core_stopped_recovery_owners == {}


def test_non_gateway_ticket_cannot_commit_physical_fail_closed_outcome() -> None:
    runtime, failure, snapshot, _payload, _refs = _ordered_recovery_fixture()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_route_epoch = snapshot.route_epoch
    runtime._runtime_recovery_tickets = {}
    runtime._runtime_recovery_outcomes = {}
    runtime._runtime_recovery_outcome_events = {}
    runtime._runtime_recovery_ticket_revision = 0
    runtime._runtime_incident_recovery_state = {}
    runtime._runtime_core_stopped_recovery_owners = {}
    runtime._runtime_route_mutation_lease = None
    runtime._runtime_facility_epoch = 0
    runtime._runtime_selector_intent_revision = 0
    runtime._runtime_worker_token = lambda: (1,)
    ticket = runtime._register_runtime_recovery_ticket(
        failure,
        snapshot,
        "non-gateway-fail-closed-commit",
    )

    disposition = runtime._commit_runtime_fail_closed_outcome(
        physical={"route_verified": True, "enforced": True},
        incident_id=ticket.incident_id,
        from_proxy=failure.binding.node.proxy_name,
        selector_intent_revision=0,
        expected_ticket=ticket,
    )

    assert disposition == "indeterminate"
    assert runtime._runtime_route_mutation_lease is None
    assert runtime._runtime_incident_recovery_state == {}
    assert runtime._runtime_core_stopped_recovery_owners == {}


def test_non_gateway_fail_closed_outcome_is_downgraded_to_controller_hold() -> None:
    runtime, failure, snapshot, _payload, _refs = _ordered_recovery_fixture()
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_route_epoch = snapshot.route_epoch
    runtime._runtime_recovery_tickets = {}
    runtime._runtime_recovery_outcomes = {}
    runtime._runtime_recovery_outcome_events = {}
    runtime._runtime_recovery_ticket_revision = 0
    runtime._runtime_incident_recovery_state = {}
    runtime._runtime_fail_closed_admissions = {}
    runtime._runtime_facility_epoch = 0
    runtime._runtime_selector_intent_revision = 0
    runtime._runtime_worker_token = lambda: (1,)
    ticket = runtime._register_runtime_recovery_ticket(
        failure,
        snapshot,
        "non-gateway-fail-closed-outcome",
    )

    outcome = runtime._complete_runtime_recovery_outcome(
        ticket.incident_id,
        RuntimeRecoveryStatus.FAIL_CLOSED,
        reason_code="legacy_non_gateway_fail_closed",
        expected_ticket=ticket,
        to_ref=failure.binding.node,
    )

    assert outcome is not None
    assert outcome.status is RuntimeRecoveryStatus.CONTROLLER_HOLD
    assert outcome.reason == "non_gateway_fail_closed_blocked"
    assert outcome.to_ref is None


def test_fail_closed_admission_blocks_competing_hold_until_physical_commit() -> None:
    runtime, _failure, _old_snapshot, _payload, _refs = (
        _ordered_recovery_fixture()
    )
    snapshot, _binding, failure = _fixed_gateway_failure_fixture(
        route_epoch=12
    )
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_route_epoch = snapshot.route_epoch
    runtime._runtime_recovery_tickets = {}
    runtime._runtime_recovery_outcomes = {}
    runtime._runtime_recovery_outcome_events = {}
    runtime._runtime_recovery_ticket_revision = 0
    runtime._runtime_incident_recovery_state = {}
    runtime._runtime_core_stopped_recovery_owners = {}
    runtime._runtime_route_mutation_lease = None
    runtime._runtime_fail_closed_admissions = {}
    runtime._runtime_facility_epoch = 0
    runtime._runtime_selector_intent_revision = 0
    runtime._runtime_worker_token = lambda: (1,)
    runtime._invalidate_exit_route_context = lambda: None
    runtime._runtime_egress_coordinator = RuntimeEgressCoordinator()
    runtime._runtime_egress_coordinator.set_current_snapshot(snapshot)
    ticket = runtime._register_runtime_recovery_ticket(
        failure,
        snapshot,
        "fail-closed-admission",
    )

    assert runtime._admit_runtime_fail_closed_mutation(
        ticket,
        deadline_monotonic=ticket.deadline_monotonic,
        failure_binding=failure.binding,
        gateway_failure_confirmed=True,
        expected_facility_epoch=0,
    ) is True
    runtime._runtime_node_attribution_hold_reason = (
        lambda: "controller_readback_lost"
    )
    runtime._runtime_facility_hold_reason = lambda: ""
    runtime._runtime_worker_token_is_current = lambda _token: True
    waited: list[RuntimeRecoveryOutcome | None] = []
    waiter = threading.Thread(
        target=lambda: waited.append(
            runtime._wait_runtime_recovery_outcome(ticket)
        )
    )
    waiter.start()
    time.sleep(0.05)

    assert waiter.is_alive() is True
    assert waited == []

    disposition = runtime._commit_runtime_fail_closed_outcome(
        physical={"route_verified": True, "enforced": True},
        incident_id=ticket.incident_id,
        from_proxy=failure.binding.node.proxy_name,
        selector_intent_revision=0,
        expected_ticket=ticket,
    )
    runtime._release_runtime_fail_closed_admission(ticket)
    waiter.join(timeout=1.0)

    assert disposition == "route_verified"
    assert waiter.is_alive() is False
    assert len(waited) == 1
    assert waited[0] is not None
    assert waited[0].status is RuntimeRecoveryStatus.FAIL_CLOSED
    assert runtime._runtime_route_mutation_lease["to"] == "REJECT"
    assert runtime._runtime_recovery_outcomes[ticket.incident_id].status is (
        RuntimeRecoveryStatus.FAIL_CLOSED
    )


def test_same_controller_readback_does_not_reopen_controller_hold() -> None:
    runtime, _runner, _client, selected, _stops, _effective = (
        _physical_fail_closed_runtime()
    )
    runtime.manager = None
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._tier3_egress_activation_lock = threading.RLock()
    runtime._tier3_standby_activation_failure_key = None
    runtime._tier3_standby_activation_retry_stage = 0
    runtime._tier3_standby_activation_retry_at = 0.0
    runtime._tier3_standby_activation_facility_hold = False
    runtime._tier3_standby_activation_hold_kind = ""
    runtime._tier3_standby_activation_last_reason = ""
    runtime._tier3_runtime_qualification_retry_at = 0.0
    runtime._runtime_local_data_plane_blocked = lambda: False
    queued: list[str] = []
    runtime._queue_tier3_egress_activation = lambda: queued.append("activation")

    first = runtime._refresh_runtime_route_snapshot()
    assert first is not None
    recorded_key = runtime._tier3_standby_activation_key()
    runtime._tier3_standby_activation_failure_key = recorded_key
    runtime._tier3_standby_activation_facility_hold = True
    runtime._tier3_standby_activation_hold_kind = "controller_hold"

    same = runtime._refresh_runtime_route_snapshot()

    assert same is not None
    assert same.snapshot_key == first.snapshot_key
    assert queued == []
    assert runtime._tier3_standby_activation_failure_key == recorded_key

    selected["leaf"] = "good-leaf"
    changed = runtime._refresh_runtime_route_snapshot()

    assert changed is not None
    assert changed.snapshot_key != first.snapshot_key
    assert queued == ["activation"]
    assert runtime._tier3_standby_activation_failure_key is None


def test_activation_failure_key_ignores_unloaded_candidate_health_changes() -> None:
    runtime, _failure, snapshot, payload, refs = _ordered_recovery_fixture()
    runtime.core_runner = _Runner()
    runtime._core_lifecycle_lock = threading.RLock()
    runtime._core_generation = snapshot.core_generation
    runtime._runtime_route_snapshot_lock = threading.RLock()
    runtime._runtime_route_snapshot = snapshot
    runtime._runtime_route_snapshot_payload = payload
    runtime._runtime_loaded_export_contract = {
        "semantic_digest": snapshot.semantic_digest,
        "provider_semantic_digest": "provider-a",
    }
    runtime._runtime_network_context_epoch = snapshot.network_context_epoch
    runtime._runtime_selector_intent_revision = 3
    health_revision = [11]
    eligible = {ref.key: True for ref in refs}
    runtime.manager = SimpleNamespace(
        runtime_candidate_snapshot=lambda: SimpleNamespace(
            revision=health_revision[0],
            runtime_generation=5,
            candidates=tuple(
                SimpleNamespace(
                    key=ref.key,
                    eligible=eligible[ref.key],
                )
                for ref in refs
            ),
        )
    )

    first = runtime._tier3_standby_activation_key()
    health_revision[0] += 1
    second = runtime._tier3_standby_activation_key()
    eligible[refs[2].key] = False
    health_revision[0] += 1
    third = runtime._tier3_standby_activation_key()

    assert second == first
    assert third == first

    runtime._runtime_loaded_export_contract = {
        "semantic_digest": snapshot.semantic_digest,
        "provider_semantic_digest": "provider-b",
    }
    assert runtime._tier3_standby_activation_key() == first

    payload["proxies"]["proxies"]["TIER3-AUTO"]["now"] = "C"
    assert runtime._tier3_standby_activation_key() != first
