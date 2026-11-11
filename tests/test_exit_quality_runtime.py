from __future__ import annotations

import copy
import logging
import threading
import time
import unittest
from types import SimpleNamespace
from typing import Any

from desktop_runtime import DesktopRuntime, _RuntimeRecoveryTicket
from runtime_egress_recovery import (
    CoreLoadedRouteSnapshot,
    RuntimeFailureCircuitDecision,
    RuntimeNodeRef,
    RuntimeRecoveryOutcome,
    RuntimeRecoveryStatus,
    RuntimeRouteBinding,
)
from runtime_manual_validation import (
    ManualDataPlaneProbeResult,
    ManualProbeFailureKind,
    RuntimeManualValidation,
)
from runtime_route_audit import inspect_runtime_route


class _CoreRunner:
    def __init__(self, *, running: bool = True, pid: int = 101) -> None:
        self.running = running
        self.pid = pid

    def status(self) -> dict[str, Any]:
        return {"running": self.running, "pid": self.pid}

    def is_running(self) -> bool:
        return self.running


class _ExitQualityResult:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = copy.deepcopy(payload)

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.payload)


class _ExitQualityService:
    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        *,
        latest: dict[str, Any] | None = None,
    ) -> None:
        self.payload = copy.deepcopy(payload or {})
        self.latest = copy.deepcopy(latest)
        self.check_calls: list[dict[str, Any]] = []
        self.latest_calls = 0

    def check(self, *, proxy_url: str | None, request_id: str) -> _ExitQualityResult:
        self.check_calls.append({"proxy_url": proxy_url, "request_id": request_id})
        return _ExitQualityResult(self.payload)

    def latest_result(self) -> dict[str, Any] | None:
        self.latest_calls += 1
        return copy.deepcopy(self.latest)


class _DelayClient:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str | None, int]] = []

    def proxy_delay(
        self,
        name: str,
        *,
        url: str | None = None,
        timeout_ms: int = 5000,
    ) -> dict[str, Any]:
        self.calls.append((name, url, timeout_ms))
        if self.error is not None:
            raise self.error
        return {"delay": 37}


def _route_payload(leaf: str) -> dict[str, Any]:
    return {
        "proxies": {
            "FINAL-EXIT": {
                "type": "Selector",
                "now": "PROXY-FALLBACK",
                "all": ["PROXY-FALLBACK", "DIRECT"],
            },
            "PROXY-FALLBACK": {
                "type": "Fallback",
                "now": leaf,
                "all": [leaf],
            },
            leaf: {"type": "Shadowsocks"},
        }
    }


def _logical_route(
    leaf: str,
    *,
    core_instance: str = "7:11:101",
) -> dict[str, Any]:
    route = {
        "route_marker": f"route-{leaf}",
        "selected_chain": ["FINAL-EXIT", "PROXY-FALLBACK", leaf],
        "leaf_proxy": leaf,
        "path_kind": "mihomo_proxy",
        "core_instance": core_instance,
        "confirmed": True,
    }
    route["binding_marker"] = DesktopRuntime._exit_route_binding_marker(route)
    route["execution_binding_marker"] = (
        DesktopRuntime._exit_route_execution_binding_marker(route)
    )
    return route


def _unavailable_identity_payload() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "request_id": "request-1",
        "state": "unavailable",
        "identity": {
            "ipv4": {
                "family": 4,
                "state": "unavailable",
                "confidence": "none",
                "ip": None,
            },
            "ipv6": {
                "family": 6,
                "state": "unavailable",
                "confidence": "none",
                "ip": None,
            },
        },
        "diagnostics": {
            "all_identity_failed": True,
            "source_errors": ["identity"],
        },
    }


def _identity_transport_failure_payload(error: str) -> dict[str, Any]:
    payload = _unavailable_identity_payload()
    for family_name in ("ipv4", "ipv6"):
        payload["identity"][family_name]["evidence"] = [
            {
                "source": f"identity-{family_name}",
                "attempts": [
                    {
                        "round": 1,
                        "status": "error",
                        "error": error,
                    }
                ],
            }
        ]
    payload["route"] = {
        "confirmed_before": True,
        "confirmed_after": True,
        "stable_during_check": True,
        "binding_marker": "stable-route",
    }
    return payload


def _confirmed_identity_payload() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "request_id": "request-1",
        "state": "complete",
        "identity": {
            "ipv4": {
                "family": 4,
                "state": "confirmed",
                "confidence": "medium",
                "ip": "203.0.113.10",
            },
            "ipv6": {
                "family": 6,
                "state": "unavailable",
                "confidence": "none",
                "ip": None,
            },
        },
        "diagnostics": {"all_identity_failed": False, "source_errors": []},
        "route": {
            "confirmed_before": True,
            "confirmed_after": True,
            "stable_during_check": True,
            "binding_marker": "stable-route-after-switch",
        },
    }


class ExitQualityRuntimeTests(unittest.TestCase):
    def _runtime_for_check(
        self,
        payload: dict[str, Any],
        *,
        delay_client: _DelayClient,
    ) -> tuple[DesktopRuntime, _ExitQualityService, list[str]]:
        runtime = object.__new__(DesktopRuntime)
        service = _ExitQualityService(payload, latest=payload)
        audit_reasons: list[str] = []
        runtime.stop_event = threading.Event()
        runtime._core_lifecycle_lock = threading.RLock()
        runtime._runtime_route_snapshot_lock = threading.RLock()
        runtime._runtime_hot_standby_lock = threading.RLock()
        runtime._runtime_local_facility_fault_reason = ""
        runtime._runtime_network_context_hold_active = False
        runtime._runtime_route_epoch = 0
        runtime._runtime_network_context_epoch = 0
        runtime._runtime_facility_epoch = 0
        runtime._runtime_selector_intent_revision = 0
        runtime._core_route_mutation_pending = False
        runtime._core_runtime_phase = "formal_capture"
        runtime.exit_quality_service = service
        runtime.core_runner = _CoreRunner(running=True)
        runtime.manager = SimpleNamespace(effective_egress_ok=True)
        runtime.config = {"client": {"mihomo_mixed_port": 7890}}
        runtime.logger = logging.getLogger("test.exit-quality-runtime")
        runtime._require_api_client = lambda: delay_client
        runtime._default_probe_url = lambda: "https://probe.test/generate_204"
        runtime._default_probe_urls = lambda: [
            "https://cp.cloudflare.com/generate_204",
            "https://www.gstatic.com/generate_204",
        ]
        runtime._running_core_mixed_proxy_url = lambda: "http://127.0.0.1:7890"

        def probe_mixed_port() -> dict[str, Any]:
            response = delay_client.proxy_delay(
                "FINAL-EXIT",
                url="https://probe.test/generate_204",
                timeout_ms=5000,
            )
            return {"status": 204, "latency_ms": response.get("delay")}

        # Route-readiness/controller integration is covered by the Desktop
        # runtime recovery suite.  These focused exit-quality tests provide a
        # verified formal route and isolate only the neutral-probe decision.
        runtime._probe_mihomo_mixed_port = probe_mixed_port
        runtime._probe_mihomo_mixed_port_once = lambda **_kwargs: probe_mixed_port()

        def leased_probe_mixed_port_once(
            **kwargs: Any,
        ) -> dict[str, Any]:
            context = kwargs.get("execution_context")
            if context is not None:
                self.assertTrue(context.mark_socket_started())
            return probe_mixed_port()

        runtime._probe_mihomo_mixed_port_once_leased = (
            leased_probe_mixed_port_once
        )
        runtime._current_tier3_runtime_failure_binding = lambda: None
        runtime_probe_failures: list[tuple[str, str]] = []
        runtime._test_runtime_probe_failures = runtime_probe_failures
        runtime._submit_current_route_probe_failure = (
            lambda reason, *, source="runtime_probe": (
                runtime_probe_failures.append((str(reason), str(source))) or True
            )
        )

        def audit_runtime_egress(
            reason: str = "runtime egress verification failed",
            *,
            trigger: str = "final_exit_delay",
            **_kwargs: Any,
        ) -> dict[str, Any]:
            del trigger
            audit_reasons.append(reason)
            return {
                "attempted": True,
                "post_switch_verified": False,
            }

        runtime.audit_runtime_egress = audit_runtime_egress
        return runtime, service, audit_reasons

    @staticmethod
    def _wait_for_identity_call(
        service: _ExitQualityService,
        *,
        timeout: float = 0.5,
    ) -> None:
        deadline = time.monotonic() + timeout
        while not service.check_calls and time.monotonic() < deadline:
            time.sleep(0.005)

    def test_neutral_failure_without_backup_proof_starts_no_leaf_incident_or_identity(self) -> None:
        delay_client = _DelayClient(error=TimeoutError("neutral probe timed out"))
        runtime, service, audit_reasons = self._runtime_for_check(
            _identity_transport_failure_payload("request timed out"),
            delay_client=delay_client,
        )
        result = runtime.check_exit_quality(
            proxy_url=None,
            request_id="manual-neutral-failure",
        )

        self.assertEqual(runtime._test_runtime_probe_failures, [])
        self._wait_for_identity_call(service)
        self.assertEqual(service.check_calls, [])
        self.assertEqual(audit_reasons, [])
        self.assertEqual(
            result["diagnostics"]["neutral_connectivity"]["state"],
            "failed",
        )
        self.assertFalse(result["diagnostics"]["identity_pending"])

    def test_manual_probe_failure_is_discarded_when_leaf_changes_before_confirmation(
        self,
    ) -> None:
        runtime, _service, _audit_reasons = self._runtime_for_check(
            _unavailable_identity_payload(),
            delay_client=_DelayClient(),
        )
        runtime.refresh_exit_quality_static_data_async = lambda **_kwargs: False
        runtime._queue_final_exit_identity_diagnostic = lambda **_kwargs: None
        runtime._runtime_failure_monitor_lock = threading.RLock()
        runtime._runtime_route_snapshot_lock = threading.RLock()
        runtime._runtime_route_mutation_lease = None
        runtime._runtime_incident_recovery_state = {}
        runtime._runtime_selector_intent_revision = 0

        leaf_a = RuntimeNodeRef(
            tier="tier3",
            pool_kind="tier3b",
            identity="leaf-a-identity",
            proxy_name="leaf-A",
        )
        leaf_b = RuntimeNodeRef(
            tier="tier3",
            pool_kind="tier3b",
            identity="leaf-b-identity",
            proxy_name="leaf-B",
        )

        def route_payload(selected: str) -> dict[str, Any]:
            return {
                "proxies": {
                    "proxies": {
                        "FINAL-EXIT": {
                            "type": "Selector",
                            "now": "TIER3-AUTO",
                            "all": ["TIER3-AUTO", "REJECT"],
                        },
                        "TIER3-AUTO": {
                            "type": "Fallback",
                            "now": selected,
                            "all": ["leaf-A", "leaf-B"],
                        },
                        "leaf-A": {"type": "Shadowsocks"},
                        "leaf-B": {"type": "Shadowsocks"},
                    }
                }
            }

        def snapshot_and_binding(
            *,
            selected: RuntimeNodeRef,
            route_epoch: int,
        ) -> tuple[CoreLoadedRouteSnapshot, RuntimeRouteBinding, dict[str, Any]]:
            snapshot = CoreLoadedRouteSnapshot(
                process_pid=101,
                process_generation=7,
                core_generation=11,
                semantic_digest="loaded-digest",
                route_epoch=route_epoch,
                group_graph=(
                    ("FINAL-EXIT", ("TIER3-AUTO", "REJECT")),
                    ("TIER3-AUTO", ("leaf-A", "leaf-B")),
                ),
                proxy_nodes=(leaf_a, leaf_b),
            )
            binding = RuntimeRouteBinding(
                process_pid=snapshot.process_pid,
                process_generation=snapshot.process_generation,
                core_generation=snapshot.core_generation,
                semantic_digest=snapshot.semantic_digest,
                route_epoch=snapshot.route_epoch,
                observed_outbound="FINAL-EXIT",
                route_chain=("FINAL-EXIT", "TIER3-AUTO", selected.proxy_name),
                node=selected,
            )
            return snapshot, binding, route_payload(selected.proxy_name)

        snapshot_a, binding_a, payload_a = snapshot_and_binding(
            selected=leaf_a,
            route_epoch=8,
        )
        snapshot_b, binding_b, payload_b = snapshot_and_binding(
            selected=leaf_b,
            route_epoch=9,
        )
        execution_state: dict[str, Any] = {
            "snapshot": snapshot_a,
            "binding": binding_a,
            "payload": payload_a,
        }
        runtime._runtime_route_epoch = snapshot_a.route_epoch
        runtime._runtime_network_context_epoch = snapshot_a.network_context_epoch
        runtime._current_runtime_failure_binding_state = lambda **_kwargs: (
            execution_state["binding"],
            execution_state["snapshot"],
            execution_state["payload"],
        )
        runtime.current_route_context = lambda **_kwargs: _logical_route(
            execution_state["binding"].node.proxy_name
        )

        def fail_a_then_select_b(
            _session: Any,
        ) -> tuple[ManualDataPlaneProbeResult, ManualDataPlaneProbeResult]:
            # Both sockets were opened while A owned FINAL-EXIT. Before their
            # failure can be submitted, Mihomo has already selected B and the
            # route epoch has advanced. The old evidence must be discarded.
            execution_state.update(
                {
                    "snapshot": snapshot_b,
                    "binding": binding_b,
                    "payload": payload_b,
                }
            )
            runtime._runtime_route_epoch = snapshot_b.route_epoch
            return self._manual_double_transport_failure()

        runtime._manual_current_exit_quality_contracts = fail_a_then_select_b
        runtime._manual_backup_exit_quality_contracts = (
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("stale A evidence must not probe or rotate B")
            )
        )
        runtime._runtime_ordered_alternative_refs = (
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("stale A evidence must not enumerate B alternatives")
            )
        )
        observed: list[Any] = []

        class Coordinator:
            @staticmethod
            def current_route_snapshot() -> CoreLoadedRouteSnapshot:
                return execution_state["snapshot"]

            @staticmethod
            def observe(*args: Any, **_kwargs: Any) -> RuntimeFailureCircuitDecision:
                observed.append(args)
                raise AssertionError("stale A evidence must not reach the circuit")

        runtime._runtime_egress_coordinator = Coordinator()
        recoveries: list[Any] = []
        runtime._start_runtime_recovery = (
            lambda *args, **_kwargs: recoveries.append(args)
        )

        result = runtime.check_exit_quality(
            proxy_url=None,
            request_id="manual-leaf-changed-during-probe",
        )

        self.assertEqual(observed, [])
        self.assertEqual(recoveries, [])
        self.assertEqual(execution_state["binding"].node, leaf_b)
        self.assertEqual(
            result["diagnostics"]["neutral_connectivity"]["switch_reason"],
            "manual_execution_binding_stale",
        )
        self.assertFalse(
            result["diagnostics"]["neutral_connectivity"]["switch_committed"]
        )
        self.assertEqual(runtime._runtime_route_mutation_lease, None)
        self.assertEqual(runtime._test_runtime_probe_failures, [])

    def test_manual_double_failure_during_facility_hold_never_reaches_node_recovery(
        self,
    ) -> None:
        (
            runtime,
            _gateway_ref,
            _underlay_ref,
            _backup_ref,
            snapshot,
            _gateway_binding,
            _snapshot_payload,
        ) = self._gateway_manual_runtime()
        runtime._manual_current_exit_quality_contracts = (
            lambda _session: self._manual_double_transport_failure()
        )
        runtime._runtime_facility_hold_reason = (
            lambda: "runtime network context is stabilizing"
        )
        runtime._manual_backup_exit_quality_contracts = (
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("facility hold must not probe a backup")
            )
        )
        runtime._runtime_ordered_alternative_refs = (
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("facility hold must not enumerate alternatives")
            )
        )
        circuit_calls: list[str] = []

        class Coordinator:
            @staticmethod
            def current_route_snapshot() -> CoreLoadedRouteSnapshot:
                return snapshot

            @staticmethod
            def observe(*_args: Any, **_kwargs: Any) -> RuntimeFailureCircuitDecision:
                circuit_calls.append("observe")
                raise AssertionError("facility hold must not observe a node incident")

            @staticmethod
            def record_probe(*_args: Any, **_kwargs: Any) -> RuntimeFailureCircuitDecision:
                circuit_calls.append("record_probe")
                raise AssertionError("facility hold must not record node evidence")

        runtime._runtime_egress_coordinator = Coordinator()
        recoveries: list[Any] = []
        runtime._start_runtime_recovery = (
            lambda *args, **_kwargs: recoveries.append(args)
        )
        runtime._release_runtime_network_context_hold_after_verified_probe = (
            lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("failed contracts cannot release facility hold")
            )
        )
        runtime._set_effective_egress_readiness_state = (
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("failed contracts cannot promote readiness")
            )
        )

        result = runtime.check_exit_quality(
            proxy_url=None,
            request_id="manual-facility-hold-failure",
        )

        neutral = result["diagnostics"]["neutral_connectivity"]
        self.assertEqual(neutral["state"], "failed")
        self.assertEqual(neutral["switch_reason"], "facility_hold")
        self.assertEqual(neutral["gateway_underlay_contracts"], 0)
        self.assertFalse(neutral["backup_verified"])
        self.assertEqual(circuit_calls, [])
        self.assertEqual(recoveries, [])
        self.assertEqual(runtime._runtime_route_mutation_lease, None)

    def test_manual_facility_hold_releases_only_for_dual_success_with_current_execution_binding(
        self,
    ) -> None:
        cases = (
            ("double_failure", self._manual_double_transport_failure(), False, False),
            (
                "mixed",
                (
                    ManualDataPlaneProbeResult.https_204("neutral-0"),
                    ManualDataPlaneProbeResult.transport_failure(
                        "neutral-1",
                        ManualProbeFailureKind.TIMEOUT,
                    ),
                ),
                False,
                False,
            ),
            ("dual_success_stale", self._manual_double_204_success(), True, False),
            ("dual_success_current", self._manual_double_204_success(), False, True),
        )
        for case_name, probe_results, make_stale, should_release in cases:
            with self.subTest(case=case_name):
                (
                    runtime,
                    _gateway_ref,
                    _underlay_ref,
                    _backup_ref,
                    snapshot,
                    _gateway_binding,
                    _snapshot_payload,
                ) = self._gateway_manual_runtime()
                runtime.manager = SimpleNamespace(effective_egress_ok=False)
                hold_active = [True]
                runtime._runtime_facility_hold_reason = (
                    lambda: (
                        "runtime network context is stabilizing"
                        if hold_active[0]
                        else ""
                    )
                )

                def current_contracts(
                    _session: Any,
                ) -> tuple[
                    ManualDataPlaneProbeResult,
                    ManualDataPlaneProbeResult,
                ]:
                    if make_stale:
                        runtime._runtime_route_epoch = snapshot.route_epoch + 1
                    return probe_results

                runtime._manual_current_exit_quality_contracts = current_contracts
                runtime._manual_backup_exit_quality_contracts = (
                    lambda *_args, **_kwargs: (_ for _ in ()).throw(
                        AssertionError("facility hold must not probe a backup")
                    )
                )
                circuit_calls: list[str] = []

                class Coordinator:
                    @staticmethod
                    def current_route_snapshot() -> CoreLoadedRouteSnapshot:
                        return snapshot

                    @staticmethod
                    def observe(
                        *_args: Any,
                        **_kwargs: Any,
                    ) -> RuntimeFailureCircuitDecision:
                        circuit_calls.append("observe")
                        raise AssertionError("facility hold must not observe a node incident")

                    @staticmethod
                    def record_probe(
                        *_args: Any,
                        **_kwargs: Any,
                    ) -> RuntimeFailureCircuitDecision:
                        circuit_calls.append("record_probe")
                        raise AssertionError("facility hold must not record node evidence")

                runtime._runtime_egress_coordinator = Coordinator()
                release_bindings: list[Any] = []

                def release_hold(
                    *,
                    expected_mutation_binding: Any,
                    **_expected_epochs: Any,
                ) -> bool:
                    release_bindings.append(expected_mutation_binding)
                    self.assertEqual(
                        tuple(expected_mutation_binding.snapshot_key),
                        tuple(snapshot.snapshot_key),
                    )
                    hold_active[0] = False
                    return True

                runtime._release_runtime_network_context_hold_after_verified_probe = (
                    release_hold
                )
                readiness_writes: list[tuple[Any, ...]] = []
                runtime._set_effective_egress_readiness_state = (
                    lambda *args, **_kwargs: readiness_writes.append(args)
                )
                recoveries: list[Any] = []
                runtime._start_runtime_recovery = (
                    lambda *args, **_kwargs: recoveries.append(args)
                )

                result = runtime.check_exit_quality(
                    proxy_url=None,
                    request_id=f"manual-facility-{case_name}",
                )

                neutral = result["diagnostics"]["neutral_connectivity"]
                self.assertEqual(bool(release_bindings), should_release)
                self.assertEqual(bool(readiness_writes), should_release)
                self.assertEqual(
                    neutral["execution_binding_current"],
                    not make_stale,
                )
                self.assertEqual(circuit_calls, [])
                self.assertEqual(recoveries, [])

    def test_manual_promotion_cas_rejects_facility_entry_at_final_boundary(
        self,
    ) -> None:
        (
            runtime,
            _gateway_ref,
            _underlay_ref,
            _backup_ref,
            snapshot,
            _gateway_binding,
            _snapshot_payload,
        ) = self._gateway_manual_runtime()
        runtime.manager = SimpleNamespace(effective_egress_ok=False)
        runtime._runtime_network_context_hold_active = False
        runtime._runtime_network_context_hold_reason = ""
        runtime._runtime_network_context_hold_pending_candidate = False
        runtime._runtime_local_facility_fault_reason = ""
        runtime._runtime_facility_epoch = 4
        binding = runtime._runtime_mutation_binding_for_snapshot(snapshot)
        original_is_current = runtime._runtime_mutation_binding_is_current
        readiness: list[tuple[object, ...]] = []
        activations: list[str] = []
        runtime._set_effective_egress_readiness_state = (
            lambda *args, **_kwargs: readiness.append(args)
        )
        runtime._queue_tier3_egress_activation = (
            lambda: activations.append("activation")
        )

        def enter_hold_before_final_cas(candidate: Any) -> bool:
            runtime._enter_runtime_facility_hold(
                "network context changed at manual promotion boundary"
            )
            return original_is_current(candidate)

        runtime._runtime_mutation_binding_is_current = (
            enter_hold_before_final_cas
        )

        promoted = runtime._promote_manual_egress_after_verified_probe(
            mutation_binding=binding,
            clean_current_success=True,
        )

        self.assertFalse(promoted)
        self.assertEqual(
            readiness,
            [
                (
                    None,
                    "network context changed at manual promotion boundary",
                )
            ],
        )
        self.assertEqual(activations, [])
        self.assertTrue(runtime._runtime_network_context_hold_active)
        self.assertEqual(runtime._runtime_facility_epoch, 5)

    def test_blocked_identity_cannot_overwrite_coordinator_recovery(self) -> None:
        delay_client = _DelayClient()
        runtime, service, _audit_reasons = self._runtime_for_check(
            _unavailable_identity_payload(),
            delay_client=delay_client,
        )
        identity_started = threading.Event()
        release_identity = threading.Event()
        original_check = service.check

        def blocked_check(**kwargs: Any) -> _ExitQualityResult:
            identity_started.set()
            self.assertTrue(release_identity.wait(1.0))
            return original_check(**kwargs)

        service.check = blocked_check  # type: ignore[method-assign]

        class EffectiveManager:
            def __init__(self) -> None:
                self.effective_egress_ok = True
                self.states: list[tuple[bool | None, str]] = []

            def set_effective_egress_state(
                self,
                ok: bool | None,
                reason: str = "",
            ) -> None:
                self.effective_egress_ok = ok
                self.states.append((ok, reason))

        manager = EffectiveManager()
        runtime.manager = manager

        runtime._submit_current_route_probe_failure = (
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("healthy data-plane evidence must not submit an incident")
            )
        )
        started_at = time.monotonic()
        result = runtime.check_exit_quality(
            proxy_url=None,
            request_id="blocked-identity-recovery",
        )
        self.assertLess(time.monotonic() - started_at, 0.5)
        self.assertTrue(identity_started.wait(0.5))
        self.assertEqual(
            result["diagnostics"]["neutral_connectivity"]["state"],
            "ok",
        )
        manager.set_effective_egress_state(False, "newer incident")
        release_identity.set()
        identity_thread = runtime._runtime_identity_diagnostic_thread
        if identity_thread is not None:
            identity_thread.join(1.0)
        self.assertEqual(
            manager.states,
            [(False, "newer incident")],
        )

    def test_blocked_identity_cannot_clear_a_newer_runtime_incident(self) -> None:
        delay_client = _DelayClient()
        runtime, service, _audit_reasons = self._runtime_for_check(
            _unavailable_identity_payload(),
            delay_client=delay_client,
        )
        identity_started = threading.Event()
        release_identity = threading.Event()
        original_check = service.check

        def blocked_check(**kwargs: Any) -> _ExitQualityResult:
            identity_started.set()
            self.assertTrue(release_identity.wait(1.0))
            return original_check(**kwargs)

        service.check = blocked_check  # type: ignore[method-assign]
        states: list[tuple[bool | None, str]] = []
        runtime.manager = SimpleNamespace(
            effective_egress_ok=True,
            set_effective_egress_state=lambda ok, reason="": states.append(
                (ok, reason)
            ),
        )
        started_at = time.monotonic()
        result = runtime.check_exit_quality(
            proxy_url=None,
            request_id="blocked-identity-new-incident",
        )
        self.assertLess(time.monotonic() - started_at, 0.5)
        self.assertTrue(identity_started.wait(0.5))
        self.assertEqual(
            result["diagnostics"]["neutral_connectivity"]["state"],
            "ok",
        )
        runtime.manager.set_effective_egress_state(False, "newer incident")
        release_identity.set()
        identity_thread = runtime._runtime_identity_diagnostic_thread
        if identity_thread is not None:
            identity_thread.join(1.0)
        self.assertEqual(states, [(False, "newer incident")])

    def test_one_current_contract_success_and_one_failure_never_switches(self) -> None:
        runtime, _service, _audit_reasons = self._runtime_for_check(
            _unavailable_identity_payload(),
            delay_client=_DelayClient(),
        )
        runtime.refresh_exit_quality_static_data_async = lambda **_kwargs: False
        runtime._current_runtime_failure_binding_state = lambda **_kwargs: None
        runtime._manual_current_exit_quality_contracts = lambda _session: (
            ManualDataPlaneProbeResult.https_204("neutral-0"),
            ManualDataPlaneProbeResult.transport_failure(
                "neutral-1",
                ManualProbeFailureKind.TIMEOUT,
            ),
        )
        runtime._manual_backup_exit_quality_contracts = (
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("mixed current evidence must not probe a backup")
            )
        )
        runtime._manual_confirm_and_switch_before_return = (
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("mixed current evidence must not switch")
            )
        )
        runtime._queue_final_exit_identity_diagnostic = lambda **_kwargs: None

        result = runtime.check_exit_quality(
            proxy_url=None,
            request_id="manual-mixed-contract",
        )

        neutral = result["diagnostics"]["neutral_connectivity"]
        self.assertEqual(neutral["state"], "mixed")
        self.assertEqual(neutral["success_count"], 1)
        self.assertEqual(neutral["outcome"], "current_mixed")
        self.assertFalse(neutral["switch_committed"])
        self.assertEqual(runtime._test_runtime_probe_failures, [])

    def test_manual_success_requires_active_windows_system_proxy_target(self) -> None:
        runtime, _service, _audit_reasons = self._runtime_for_check(
            _unavailable_identity_payload(),
            delay_client=_DelayClient(),
        )
        runtime.network_mode = "system_proxy"
        runtime.system_proxy = SimpleNamespace(
            enabled=True,
            is_target_active=lambda: False,
        )
        runtime._current_runtime_failure_binding_state = lambda **_kwargs: None
        runtime._manual_current_exit_quality_contracts = lambda _session: (
            ManualDataPlaneProbeResult.https_204("neutral-0"),
            ManualDataPlaneProbeResult.https_204("neutral-1"),
        )

        result = runtime.check_exit_quality(
            proxy_url=None,
            request_id="manual-system-proxy-not-captured",
        )

        self.assertEqual(result["state"], "data_plane_failed")
        self.assertEqual(
            result["freshness"]["reason"],
            "system_proxy_capture_inactive",
        )
        self.assertEqual(
            result["diagnostics"]["neutral_connectivity"]["state"],
            "failed",
        )

    def test_manual_success_requires_fresh_windows_host_path_proof(self) -> None:
        runtime, _service, _audit_reasons = self._runtime_for_check(
            _unavailable_identity_payload(),
            delay_client=_DelayClient(),
        )
        runtime.network_mode = "system_proxy"
        runtime.system_proxy = SimpleNamespace(
            enabled=True,
            is_target_active=lambda: True,
        )
        runtime._current_runtime_failure_binding_state = lambda **_kwargs: None
        runtime._manual_current_exit_quality_contracts = lambda _session: (
            ManualDataPlaneProbeResult.https_204("neutral-0"),
            ManualDataPlaneProbeResult.https_204("neutral-1"),
        )
        runtime._verify_system_proxy_host_capture = (
            lambda **_kwargs: (_ for _ in ()).throw(
                TimeoutError("host path did not complete")
            )
        )

        result = runtime.check_exit_quality(
            proxy_url=None,
            request_id="manual-system-proxy-host-path-failed",
        )

        self.assertEqual(result["state"], "data_plane_failed")
        self.assertEqual(
            result["freshness"]["reason"],
            "system_proxy_host_path_unverified",
        )
        self.assertFalse(
            result["diagnostics"]["system_proxy_capture"][
                "host_path_verified"
            ]
        )
        self.assertEqual(
            result["diagnostics"]["neutral_connectivity"]["state"],
            "failed",
        )

    def test_early_manual_double_failure_uses_bounded_coordinator_switch(self) -> None:
        runtime = object.__new__(DesktopRuntime)
        current = RuntimeNodeRef(
            tier="tier3",
            pool_kind="tier3b",
            identity="current-id",
            proxy_name="current-leaf",
        )
        alternative = RuntimeNodeRef(
            tier="tier3",
            pool_kind="tier3b",
            identity="alternative-id",
            proxy_name="alternative-leaf",
        )
        snapshot = CoreLoadedRouteSnapshot(
            process_pid=4312,
            process_generation=7,
            core_generation=3,
            semantic_digest="loaded",
            route_epoch=9,
            proxy_nodes=(current, alternative),
        )
        binding = RuntimeRouteBinding(
            process_pid=4312,
            process_generation=7,
            core_generation=3,
            semantic_digest="loaded",
            route_epoch=9,
            observed_outbound="FINAL-EXIT",
            route_chain=("FINAL-EXIT", "TIER3-AUTO", "current-leaf"),
            node=current,
        )
        decision = RuntimeFailureCircuitDecision(
            accepted=True,
            start_probe=True,
            incident_id="manual-incident",
            probe_token="manual-probe-token",
        )
        runtime._runtime_egress_coordinator = SimpleNamespace(
            observe=lambda *_args, **_kwargs: decision,
        )
        now = time.monotonic()
        ticket = _RuntimeRecoveryTicket(
            generation=1,
            incident_id=decision.incident_id,
            observed_at_monotonic=now,
            deadline_monotonic=now + 1.0,
            snapshot_key=tuple(snapshot.snapshot_key),
            route_epoch=snapshot.route_epoch,
            facility_epoch=0,
            selector_intent_revision=0,
            worker_token=(1,),
            from_ref=current,
        )
        runtime._ensure_runtime_recovery_ticket = (
            lambda *_args, **_kwargs: ticket
        )
        calls: list[RuntimeNodeRef] = []

        def recover(*_args: Any, **kwargs: Any) -> RuntimeRecoveryOutcome:
            calls.append(kwargs["alternative"])
            return RuntimeRecoveryOutcome(
                status=RuntimeRecoveryStatus.RECOVERED,
                incident_id=ticket.incident_id,
                route_epoch=snapshot.route_epoch + 1,
                budget_ms=800,
                reason="verified",
                from_ref=current,
                to_ref=alternative,
            )

        runtime._run_runtime_early_confirmed_recovery = recover
        session = RuntimeManualValidation(
            total_timeout_seconds=1.0,
            per_probe_timeout_seconds=0.2,
        )

        committed, reason = (
            runtime._manual_confirm_and_switch_before_return_early(
                session,
                binding_state=(binding, snapshot, {"proxies": {}}),
                alternative=alternative,
                request_id="manual-fallback",
                expected_mutation_binding=None,
            )
        )

        self.assertTrue(committed)
        self.assertEqual(reason, "replacement_selected_and_verified")
        self.assertEqual(calls, [alternative])

    def test_double_failure_skips_dead_first_backup_and_waits_for_healthy_readback(
        self,
    ) -> None:
        runtime, _service, _audit_reasons = self._runtime_for_check(
            _unavailable_identity_payload(),
            delay_client=_DelayClient(),
        )
        runtime.refresh_exit_quality_static_data_async = lambda **_kwargs: False
        runtime._queue_final_exit_identity_diagnostic = lambda **_kwargs: None
        runtime._runtime_failure_monitor_lock = threading.RLock()
        runtime._runtime_route_mutation_lease = None
        runtime._runtime_incident_recovery_state = {}

        current_ref = RuntimeNodeRef(
            tier="tier3",
            pool_kind="auto",
            identity="current-identity",
            proxy_name="current-leaf",
        )
        backup_ref = RuntimeNodeRef(
            tier="tier3",
            pool_kind="auto",
            identity="backup-identity",
            proxy_name="backup-leaf",
        )
        dead_backup_ref = RuntimeNodeRef(
            tier="tier3",
            pool_kind="auto",
            identity="dead-backup-identity",
            proxy_name="dead-backup-leaf",
        )
        snapshot = CoreLoadedRouteSnapshot(
            process_pid=101,
            process_generation=7,
            core_generation=11,
            semantic_digest="loaded-digest",
            route_epoch=3,
            group_graph=(
                ("FINAL-EXIT", ("TIER3-AUTO", "REJECT")),
                (
                    "TIER3-AUTO",
                    ("current-leaf", "dead-backup-leaf", "backup-leaf"),
                ),
            ),
            proxy_nodes=(current_ref, dead_backup_ref, backup_ref),
        )
        binding = RuntimeRouteBinding(
            process_pid=snapshot.process_pid,
            process_generation=snapshot.process_generation,
            core_generation=snapshot.core_generation,
            semantic_digest=snapshot.semantic_digest,
            route_epoch=snapshot.route_epoch,
            observed_outbound="FINAL-EXIT",
            route_chain=("FINAL-EXIT", "TIER3-AUTO", "current-leaf"),
            node=current_ref,
        )
        snapshot_payload = {
            "proxies": {
                "proxies": {
                    "FINAL-EXIT": {
                        "type": "Selector",
                        "now": "TIER3-AUTO",
                        "all": ["TIER3-AUTO", "REJECT"],
                    },
                    "TIER3-AUTO": {
                        "type": "Fallback",
                        "now": "current-leaf",
                        "all": [
                            "current-leaf",
                            "dead-backup-leaf",
                            "backup-leaf",
                        ],
                    },
                    "current-leaf": {"type": "Shadowsocks"},
                    "dead-backup-leaf": {"type": "Shadowsocks"},
                    "backup-leaf": {"type": "Shadowsocks"},
                }
            }
        }
        runtime._current_runtime_failure_binding_state = (
            lambda **_kwargs: (binding, snapshot, snapshot_payload)
        )
        runtime._runtime_route_snapshot_lock = threading.RLock()
        runtime._runtime_route_epoch = snapshot.route_epoch
        runtime._runtime_network_context_epoch = snapshot.network_context_epoch
        runtime._runtime_selector_intent_revision = 0
        runtime._runtime_ordered_alternative_refs = (
            lambda *_args, **_kwargs: [dead_backup_ref, backup_ref]
        )
        runtime._runtime_gateway_underlay_binding = (
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("an ordinary FINAL-EXIT leaf must not enter Gateway attribution")
            )
        )
        runtime._manual_current_exit_quality_contracts = lambda _session: (
            ManualDataPlaneProbeResult.transport_failure(
                "neutral-0",
                ManualProbeFailureKind.CONNECT,
            ),
            ManualDataPlaneProbeResult.transport_failure(
                "neutral-1",
                ManualProbeFailureKind.RESET,
            ),
        )
        probed_backups: list[RuntimeNodeRef] = []

        def probe_backup(_session, ref, **_kwargs):
            probed_backups.append(ref)
            if ref == dead_backup_ref:
                return self._manual_double_transport_failure()
            return self._manual_double_204_success()

        runtime._manual_backup_exit_quality_contracts = probe_backup

        class Coordinator:
            @staticmethod
            def current_route_snapshot() -> CoreLoadedRouteSnapshot:
                return snapshot

            @staticmethod
            def observe(*_args: Any, **_kwargs: Any) -> RuntimeFailureCircuitDecision:
                return RuntimeFailureCircuitDecision(
                    accepted=True,
                    start_probe=True,
                    incident_id="manual-incident",
                    probe_token="manual-probe-token",
                )

            @staticmethod
            def record_probe(*_args: Any, **_kwargs: Any) -> RuntimeFailureCircuitDecision:
                return RuntimeFailureCircuitDecision(
                    accepted=True,
                    confirmed=True,
                    incident_id="manual-incident",
                    probe_token="manual-probe-token",
                )

        runtime._runtime_egress_coordinator = Coordinator()
        selector_readback = threading.Event()
        selector_readback_at: list[float] = []
        recovery_threads: list[threading.Thread] = []

        def start_recovery(failure: Any, *_args: Any, **kwargs: Any) -> None:
            self.assertEqual(failure.binding.node, current_ref)
            self.assertEqual(kwargs["confirmed_alternative"], backup_ref)
            self.assertEqual(
                kwargs["confirmed_alternative_probe_result"],
                {"manual_contracts": 2, "verified": True},
            )

            def selector_transaction() -> None:
                # The production recovery owner publishes this lease only
                # after PUT, controller readback, and neutral verification.
                time.sleep(0.02)
                selector_readback_at.append(time.monotonic())
                selector_readback.set()
                with runtime._runtime_failure_monitor_lock:
                    runtime._runtime_route_mutation_lease = {
                        "incident_id": kwargs["incident_id"],
                        "to": backup_ref.proxy_name,
                        "verified_at_monotonic": time.monotonic(),
                    }
                    runtime._runtime_incident_recovery_state[
                        kwargs["incident_id"]
                    ] = "ready"

            thread = threading.Thread(target=selector_transaction, daemon=True)
            recovery_threads.append(thread)
            thread.start()

        runtime._start_runtime_recovery = start_recovery

        result = runtime.check_exit_quality(
            proxy_url=None,
            request_id="manual-double-failure",
        )
        returned_at = time.monotonic()

        for thread in recovery_threads:
            thread.join(1.0)
        self.assertTrue(selector_readback.is_set())
        self.assertEqual(probed_backups, [dead_backup_ref, backup_ref])
        self.assertLessEqual(selector_readback_at[0], returned_at)
        neutral = result["diagnostics"]["neutral_connectivity"]
        self.assertEqual(neutral["state"], "recovered")
        self.assertEqual(neutral["outcome"], "switch_before_return")
        self.assertTrue(neutral["backup_verified"])
        self.assertTrue(neutral["switch_committed"])
        self.assertEqual(
            neutral["switch_reason"],
            "replacement_selected_and_verified",
        )
        self.assertEqual(runtime._test_runtime_probe_failures, [])

    def _gateway_manual_binding_state(
        self,
    ) -> tuple[
        RuntimeNodeRef,
        RuntimeNodeRef,
        RuntimeNodeRef,
        CoreLoadedRouteSnapshot,
        RuntimeRouteBinding,
        dict[str, Any],
    ]:
        gateway_ref = RuntimeNodeRef(
            tier="exit",
            pool_kind="gateway",
            identity="gateway-identity",
            proxy_name="gateway-G",
        )
        underlay_ref = RuntimeNodeRef(
            tier="tier3",
            pool_kind="tier3b",
            identity="underlay-a-identity",
            proxy_name="underlay-A",
        )
        backup_ref = RuntimeNodeRef(
            tier="tier3",
            pool_kind="tier3b",
            identity="underlay-b-identity",
            proxy_name="underlay-B",
        )
        snapshot = CoreLoadedRouteSnapshot(
            process_pid=101,
            process_generation=7,
            core_generation=11,
            semantic_digest="gateway-loaded-digest",
            route_epoch=4,
            group_graph=(
                ("FINAL-EXIT", ("gateway-G", "REJECT")),
                ("PROXY-FALLBACK", ("underlay-A", "underlay-B")),
            ),
            proxy_nodes=(gateway_ref, underlay_ref, backup_ref),
        )
        gateway_binding = RuntimeRouteBinding(
            process_pid=snapshot.process_pid,
            process_generation=snapshot.process_generation,
            core_generation=snapshot.core_generation,
            semantic_digest=snapshot.semantic_digest,
            route_epoch=snapshot.route_epoch,
            observed_outbound="FINAL-EXIT",
            route_chain=("FINAL-EXIT", "gateway-G"),
            node=gateway_ref,
        )
        snapshot_payload = {
            "proxies": {
                "proxies": {
                    "FINAL-EXIT": {
                        "type": "Selector",
                        "now": "gateway-G",
                        "all": ["gateway-G", "REJECT"],
                    },
                    "PROXY-FALLBACK": {
                        "type": "Fallback",
                        "now": "underlay-A",
                        "all": ["underlay-A", "underlay-B"],
                    },
                    "gateway-G": {"type": "Shadowsocks"},
                    "underlay-A": {"type": "Shadowsocks"},
                    "underlay-B": {"type": "Shadowsocks"},
                }
            },
            "proxy_map": {
                "gateway-G": {
                    "proxy": {
                        "name": "gateway-G",
                        "dialer-proxy": "PROXY-FALLBACK",
                    }
                },
                "underlay-A": {"proxy": {"name": "underlay-A"}},
                "underlay-B": {"proxy": {"name": "underlay-B"}},
            },
        }
        return (
            gateway_ref,
            underlay_ref,
            backup_ref,
            snapshot,
            gateway_binding,
            snapshot_payload,
        )

    @staticmethod
    def _manual_double_transport_failure(
    ) -> tuple[ManualDataPlaneProbeResult, ManualDataPlaneProbeResult]:
        return (
            ManualDataPlaneProbeResult.transport_failure(
                "neutral-0",
                ManualProbeFailureKind.CONNECT,
            ),
            ManualDataPlaneProbeResult.transport_failure(
                "neutral-1",
                ManualProbeFailureKind.RESET,
            ),
        )

    @staticmethod
    def _manual_double_204_success(
    ) -> tuple[ManualDataPlaneProbeResult, ManualDataPlaneProbeResult]:
        return (
            ManualDataPlaneProbeResult.https_204("neutral-0"),
            ManualDataPlaneProbeResult.https_204("neutral-1"),
        )

    def _gateway_manual_runtime(
        self,
    ) -> tuple[
        DesktopRuntime,
        RuntimeNodeRef,
        RuntimeNodeRef,
        RuntimeNodeRef,
        CoreLoadedRouteSnapshot,
        RuntimeRouteBinding,
        dict[str, Any],
    ]:
        runtime, _service, _audit_reasons = self._runtime_for_check(
            _unavailable_identity_payload(),
            delay_client=_DelayClient(),
        )
        runtime.refresh_exit_quality_static_data_async = lambda **_kwargs: False
        runtime._queue_final_exit_identity_diagnostic = lambda **_kwargs: None
        runtime._runtime_failure_monitor_lock = threading.RLock()
        runtime._runtime_route_mutation_lease = None
        runtime._runtime_incident_recovery_state = {}
        (
            gateway_ref,
            underlay_ref,
            backup_ref,
            snapshot,
            gateway_binding,
            snapshot_payload,
        ) = self._gateway_manual_binding_state()
        runtime._current_runtime_failure_binding_state = (
            lambda **_kwargs: (gateway_binding, snapshot, snapshot_payload)
        )
        runtime._runtime_route_snapshot_lock = threading.RLock()
        runtime._runtime_route_epoch = snapshot.route_epoch
        runtime._runtime_network_context_epoch = snapshot.network_context_epoch
        runtime._runtime_selector_intent_revision = 0
        route = inspect_runtime_route(
            snapshot_payload["proxies"],
            start="FINAL-EXIT",
        )
        route.update(
            {
                "path_kind": "mihomo_proxy",
                "core_instance": "7:11:101",
                "confirmed": True,
            }
        )
        route = DesktopRuntime._exit_quality_route_for_display(route)
        runtime.current_route_context = lambda **_kwargs: dict(route)
        runtime._manual_current_exit_quality_contracts = (
            lambda _session: self._manual_double_transport_failure()
        )

        class Coordinator:
            @staticmethod
            def current_route_snapshot() -> CoreLoadedRouteSnapshot:
                return snapshot

            @staticmethod
            def observe(*_args: Any, **_kwargs: Any) -> RuntimeFailureCircuitDecision:
                return RuntimeFailureCircuitDecision(
                    accepted=True,
                    start_probe=True,
                    incident_id="gateway-manual-incident",
                    probe_token="gateway-manual-probe-token",
                )

            @staticmethod
            def record_probe(*_args: Any, **_kwargs: Any) -> RuntimeFailureCircuitDecision:
                return RuntimeFailureCircuitDecision(
                    accepted=True,
                    confirmed=True,
                    incident_id="gateway-manual-incident",
                    probe_token="gateway-manual-probe-token",
                )

        runtime._runtime_egress_coordinator = Coordinator()
        return (
            runtime,
            gateway_ref,
            underlay_ref,
            backup_ref,
            snapshot,
            gateway_binding,
            snapshot_payload,
        )

    def test_gateway_chain_failure_with_healthy_current_underlay_penalizes_only_gateway_and_fails_closed(
        self,
    ) -> None:
        (
            runtime,
            gateway_ref,
            underlay_ref,
            backup_ref,
            _snapshot,
            _gateway_binding,
            _snapshot_payload,
        ) = self._gateway_manual_runtime()
        probed_refs: list[RuntimeNodeRef] = []

        def probe_member(
            _session: Any,
            ref: RuntimeNodeRef,
            *_args: Any,
            **_kwargs: Any,
        ) -> tuple[ManualDataPlaneProbeResult, ManualDataPlaneProbeResult]:
            probed_refs.append(ref)
            if ref == underlay_ref:
                return self._manual_double_204_success()
            if ref == backup_ref:
                return self._manual_double_204_success()
            raise AssertionError("manual Gateway attribution probed an unrelated member")

        runtime._manual_backup_exit_quality_contracts = probe_member
        runtime._runtime_ordered_alternative_refs = (
            lambda *_args, **_kwargs: [backup_ref]
        )
        recoveries: list[tuple[Any, RuntimeNodeRef | None]] = []

        def start_recovery(failure: Any, *_args: Any, **kwargs: Any) -> None:
            alternative = kwargs["confirmed_alternative"]
            recoveries.append((failure, alternative))
            self.assertIsNone(alternative)
            incident_id = str(kwargs["incident_id"])
            with runtime._runtime_failure_monitor_lock:
                runtime._runtime_route_mutation_lease = {
                    "group": "FINAL-EXIT",
                    "from": gateway_ref.proxy_name,
                    "to": "REJECT",
                    "incident_id": incident_id,
                    "route_epoch": failure.binding.route_epoch,
                }
                runtime._runtime_incident_recovery_state[incident_id] = "ready"

        runtime._start_runtime_recovery = start_recovery

        result = runtime.check_exit_quality(
            proxy_url=None,
            request_id="manual-gateway-self-failure",
        )

        self.assertEqual(probed_refs, [underlay_ref])
        self.assertEqual(len(recoveries), 1)
        failure, alternative = recoveries[0]
        self.assertEqual(failure.binding.node, gateway_ref)
        self.assertIsNone(alternative)
        self.assertEqual(runtime._runtime_route_mutation_lease["to"], "REJECT")
        self.assertEqual(
            result["route"]["selected_chain"],
            ["FINAL-EXIT", "gateway-G"],
        )
        self.assertFalse(result.get("route_changed", False))

    def test_gateway_chain_failure_with_failed_underlay_rotates_only_underlay_and_keeps_gateway_identity(
        self,
    ) -> None:
        (
            runtime,
            gateway_ref,
            underlay_ref,
            backup_ref,
            _snapshot,
            _gateway_binding,
            _snapshot_payload,
        ) = self._gateway_manual_runtime()
        probed_refs: list[RuntimeNodeRef] = []

        def probe_member(
            _session: Any,
            ref: RuntimeNodeRef,
            *_args: Any,
            **_kwargs: Any,
        ) -> tuple[ManualDataPlaneProbeResult, ManualDataPlaneProbeResult]:
            probed_refs.append(ref)
            if ref == underlay_ref:
                return self._manual_double_transport_failure()
            if ref == backup_ref:
                return self._manual_double_204_success()
            raise AssertionError("manual Gateway attribution probed an unrelated member")

        runtime._manual_backup_exit_quality_contracts = probe_member

        def alternatives(failure: Any, *_args: Any, **_kwargs: Any) -> list[RuntimeNodeRef]:
            self.assertEqual(failure.binding.node, underlay_ref)
            return [backup_ref]

        runtime._runtime_ordered_alternative_refs = alternatives
        recoveries: list[tuple[Any, RuntimeNodeRef | None]] = []

        def start_recovery(failure: Any, *_args: Any, **kwargs: Any) -> None:
            alternative = kwargs["confirmed_alternative"]
            recoveries.append((failure, alternative))
            incident_id = str(kwargs["incident_id"])
            with runtime._runtime_failure_monitor_lock:
                runtime._runtime_route_mutation_lease = {
                    "group": "PROXY-FALLBACK",
                    "from": underlay_ref.proxy_name,
                    "to": backup_ref.proxy_name,
                    "incident_id": incident_id,
                    "route_epoch": failure.binding.route_epoch,
                    "verified_at_monotonic": time.monotonic(),
                }
                runtime._runtime_incident_recovery_state[incident_id] = "ready"

        runtime._start_runtime_recovery = start_recovery

        result = runtime.check_exit_quality(
            proxy_url=None,
            request_id="manual-gateway-underlay-failure",
        )

        self.assertEqual(probed_refs, [underlay_ref, backup_ref])
        self.assertEqual(len(recoveries), 1)
        failure, alternative = recoveries[0]
        self.assertEqual(failure.binding.node, underlay_ref)
        self.assertEqual(alternative, backup_ref)
        self.assertEqual(runtime._runtime_route_mutation_lease["group"], "PROXY-FALLBACK")
        self.assertEqual(runtime._runtime_route_mutation_lease["to"], backup_ref.proxy_name)
        self.assertEqual(
            result["route"]["selected_chain"],
            ["FINAL-EXIT", "gateway-G"],
        )
        self.assertFalse(result.get("route_changed", False))

    def test_blocked_identity_diagnostic_does_not_delay_second_validation(self) -> None:
        runtime, service, _audit_reasons = self._runtime_for_check(
            _unavailable_identity_payload(),
            delay_client=_DelayClient(),
        )
        runtime.refresh_exit_quality_static_data_async = lambda **_kwargs: False
        runtime._current_runtime_failure_binding_state = lambda **_kwargs: None
        validation_calls: list[float] = []

        def current_contracts(_session: Any):
            validation_calls.append(time.monotonic())
            return (
                ManualDataPlaneProbeResult.https_204("neutral-0"),
                ManualDataPlaneProbeResult.https_204("neutral-1"),
            )

        runtime._manual_current_exit_quality_contracts = current_contracts
        identity_started = threading.Event()
        release_identity = threading.Event()
        original_check = service.check

        def blocked_check(**kwargs: Any) -> _ExitQualityResult:
            identity_started.set()
            self.assertTrue(release_identity.wait(1.0))
            return original_check(**kwargs)

        service.check = blocked_check  # type: ignore[method-assign]

        first = runtime.check_exit_quality(
            proxy_url=None,
            request_id="manual-identity-first",
        )
        self.assertTrue(identity_started.wait(0.5))
        second_started = time.monotonic()
        second = runtime.check_exit_quality(
            proxy_url=None,
            request_id="manual-identity-second",
        )
        second_elapsed = time.monotonic() - second_started

        self.assertLess(second_elapsed, 0.5)
        self.assertEqual(len(validation_calls), 2)
        self.assertEqual(
            first["diagnostics"]["neutral_connectivity"]["state"],
            "ok",
        )
        self.assertEqual(
            second["diagnostics"]["neutral_connectivity"]["state"],
            "ok",
        )
        # The second check coalesces onto the still-running identity worker;
        # only its own data-plane validation is authoritative and immediate.
        self.assertEqual(service.check_calls, [])

        release_identity.set()
        identity_thread = runtime._runtime_identity_diagnostic_thread
        if identity_thread is not None:
            identity_thread.join(1.0)
        self.assertEqual(len(service.check_calls), 1)
        self.assertEqual(
            service.check_calls[0]["request_id"],
            "manual-identity-first",
        )

    def test_route_selection_changes_logical_marker_but_core_restart_only_changes_execution_marker(self) -> None:
        runtime = object.__new__(DesktopRuntime)
        runtime._exit_quality_route_lock = threading.RLock()
        runtime._exit_quality_route_cache = {}
        runtime._exit_quality_route_cache_at = 0.0
        runtime._runtime_generation = 7
        runtime._core_generation = 11
        runtime.network_mode = "rule"
        runtime.core_runner = _CoreRunner(running=True, pid=101)
        current_payload = {"value": _route_payload("edge-a")}
        runtime.mihomo_proxies = lambda: current_payload["value"]

        first = runtime.current_route_context(force=True)
        current_payload["value"] = _route_payload("edge-b")
        switched = runtime.current_route_context(force=True)
        runtime.core_runner.pid = 202
        restarted = runtime.current_route_context(force=True)
        runtime._core_generation = 12
        regenerated = runtime.current_route_context(force=True)

        self.assertEqual(first["selected_chain"], ["FINAL-EXIT", "PROXY-FALLBACK", "edge-a"])
        self.assertEqual(switched["selected_chain"], ["FINAL-EXIT", "PROXY-FALLBACK", "edge-b"])
        self.assertNotEqual(first["route_marker"], switched["route_marker"])
        self.assertNotEqual(first["binding_marker"], switched["binding_marker"])
        self.assertEqual(switched["route_marker"], restarted["route_marker"])
        self.assertEqual(switched["core_instance"], "7:11:101")
        self.assertEqual(restarted["core_instance"], "7:11:202")
        self.assertEqual(switched["binding_marker"], restarted["binding_marker"])
        self.assertNotEqual(
            switched["execution_binding_marker"],
            restarted["execution_binding_marker"],
        )
        self.assertEqual(restarted["route_marker"], regenerated["route_marker"])
        self.assertEqual(regenerated["core_instance"], "7:12:202")
        self.assertEqual(restarted["binding_marker"], regenerated["binding_marker"])
        self.assertNotEqual(
            restarted["execution_binding_marker"],
            regenerated["execution_binding_marker"],
        )
        self.assertTrue(first["confirmed"])
        self.assertTrue(switched["confirmed"])

    def test_current_route_context_exposes_unique_core_loaded_managed_tier(self) -> None:
        runtime = object.__new__(DesktopRuntime)
        runtime._exit_quality_route_lock = threading.RLock()
        runtime._exit_quality_route_cache = {}
        runtime._exit_quality_route_cache_at = 0.0
        runtime._runtime_generation = 7
        runtime._core_generation = 11
        runtime.network_mode = "rule"
        runtime.core_runner = _CoreRunner(running=True, pid=101)
        runtime.mihomo_proxies = lambda: _route_payload("edge-a")
        runtime._runtime_route_snapshot_lock = threading.RLock()
        runtime._runtime_route_snapshot = CoreLoadedRouteSnapshot(
            process_pid=101,
            process_generation=3,
            core_generation=11,
            semantic_digest="digest-a",
            route_epoch=5,
            proxy_nodes=(
                RuntimeNodeRef(
                    tier="tier3",
                    pool_kind="tier3b",
                    identity="identity-a",
                    proxy_name="edge-a",
                ),
            ),
        )
        runtime._runtime_confirmed_fail_closed_snapshot_key = None

        route = runtime.current_route_context(force=True)

        self.assertEqual(route["managed_tier"], "tier3")
        self.assertEqual(route["managed_pool_kind"], "tier3b")
        self.assertFalse(route["fail_closed_confirmed"])

    def test_latest_does_not_expose_identity_from_another_logical_route(self) -> None:
        runtime = object.__new__(DesktopRuntime)
        service = _ExitQualityService(
            latest={
                "request_id": "old-check",
                "state": "complete",
                "route": {"binding_marker": "binding-old"},
            }
        )
        runtime.exit_quality_service = service
        runtime.current_route_context = lambda force=False: {
            "binding_marker": "binding-current"
        }

        latest = runtime.latest_exit_quality_result()
        current = runtime.current_route_context()

        self.assertEqual(latest, {})
        self.assertEqual(current["binding_marker"], "binding-current")
        self.assertEqual(service.latest_calls, 1)

    def test_latest_identity_survives_pid_change_on_same_logical_route(self) -> None:
        cached = _confirmed_identity_payload()
        cached["request_id"] = "persisted-old-request"
        cached["route"] = _logical_route("edge-a", core_instance="7:11:101")
        runtime = object.__new__(DesktopRuntime)
        runtime.exit_quality_service = _ExitQualityService(latest=cached)
        current = _logical_route("edge-a", core_instance="8:12:202")
        runtime.current_route_context = lambda force=False: copy.deepcopy(current)

        latest = runtime.latest_exit_quality_result()

        self.assertEqual(latest["request_id"], "persisted-old-request")
        self.assertEqual(latest["identity_source"], "cache")
        self.assertNotIn("route_changed", latest)
        self.assertEqual(
            latest["route"]["binding_marker"],
            current["binding_marker"],
        )
        self.assertNotEqual(
            latest["route"]["execution_binding_marker"],
            current["execution_binding_marker"],
        )

    def test_manual_result_does_not_rewrite_mismatched_cached_identity(self) -> None:
        cached = _confirmed_identity_payload()
        cached["request_id"] = "persisted-old-request"
        cached["route"] = _logical_route("edge-old")
        runtime, service, _audit_reasons = self._runtime_for_check(
            cached,
            delay_client=_DelayClient(),
        )
        current = _logical_route("edge-current", core_instance="8:12:202")
        runtime.current_route_context = lambda force=False: copy.deepcopy(current)
        runtime._queue_final_exit_identity_diagnostic = lambda **_kwargs: None
        runtime.refresh_exit_quality_static_data_async = (
            lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("manual validation must not start static refresh")
            )
        )

        result = runtime.check_exit_quality(
            proxy_url=None,
            request_id="manual-current-request",
        )

        self.assertEqual(result["request_id"], "manual-current-request")
        self.assertEqual(result["state"], "pending_identity")
        self.assertEqual(result["identity"], {})
        self.assertEqual(
            result["diagnostics"]["identity_cache"]["state"],
            "route_mismatch",
        )
        self.assertEqual(service.latest["request_id"], "persisted-old-request")

    def test_manual_result_uses_only_logical_route_matched_identity_as_cache(self) -> None:
        cached = _confirmed_identity_payload()
        cached["request_id"] = "persisted-old-request"
        cached["checked_at"] = "2026-08-26T14:16:13Z"
        cached["route"] = _logical_route("edge-a", core_instance="7:11:101")
        runtime, _service, _audit_reasons = self._runtime_for_check(
            cached,
            delay_client=_DelayClient(),
        )
        current = _logical_route("edge-a", core_instance="8:12:202")
        runtime.current_route_context = lambda force=False: copy.deepcopy(current)
        runtime._queue_final_exit_identity_diagnostic = lambda **_kwargs: None

        result = runtime.check_exit_quality(
            proxy_url=None,
            request_id="manual-current-request",
        )

        self.assertEqual(result["request_id"], "manual-current-request")
        self.assertEqual(result["identity_source"], "cache")
        self.assertEqual(result["identity"]["ipv4"]["ip"], "203.0.113.10")
        self.assertEqual(result["identity_checked_at"], cached["checked_at"])
        self.assertEqual(
            result["diagnostics"]["identity_cache"],
            {
                "state": "matched",
                "source_request_id": "persisted-old-request",
                "source_checked_at": cached["checked_at"],
                "binding_marker": current["binding_marker"],
            },
        )
        self.assertEqual(
            result["route"]["binding_marker"],
            current["binding_marker"],
        )
        self.assertNotEqual(
            result["route"]["execution_binding_marker"],
            cached["route"]["execution_binding_marker"],
        )

    def test_automatic_route_settling_during_check_is_not_sticky_route_changed(self) -> None:
        runtime, _service, _audit_reasons = self._runtime_for_check(
            _unavailable_identity_payload(),
            delay_client=_DelayClient(),
        )
        routes = iter(
            [
                _logical_route("edge-a", core_instance="7:11:101"),
                _logical_route("edge-b", core_instance="7:11:101"),
            ]
        )
        runtime.current_route_context = lambda force=False: copy.deepcopy(next(routes))
        runtime._queue_final_exit_identity_diagnostic = lambda **_kwargs: None

        result = runtime.check_exit_quality(
            proxy_url=None,
            request_id="manual-logical-change",
        )

        self.assertEqual(result["state"], "data_plane_indeterminate")
        self.assertNotIn("route_changed", result)
        self.assertEqual(
            result["freshness"]["reason"],
            "route_settled_during_check",
        )
        self.assertFalse(result["route"]["stable_during_check"])

    def test_manual_core_restart_on_same_logical_route_is_not_route_changed(self) -> None:
        runtime, _service, _audit_reasons = self._runtime_for_check(
            _unavailable_identity_payload(),
            delay_client=_DelayClient(),
        )
        routes = iter(
            [
                _logical_route("edge-a", core_instance="7:11:101"),
                _logical_route("edge-a", core_instance="8:12:202"),
            ]
        )
        runtime.current_route_context = lambda force=False: copy.deepcopy(next(routes))
        runtime._queue_final_exit_identity_diagnostic = lambda **_kwargs: None

        result = runtime.check_exit_quality(
            proxy_url=None,
            request_id="manual-core-restart",
        )

        self.assertEqual(result["state"], "pending_identity")
        self.assertNotIn("route_changed", result)
        self.assertTrue(result["route"]["stable_during_check"])
        self.assertNotEqual(
            result["diagnostics"]["route_before"]["execution_binding_marker"],
            result["diagnostics"]["route_after"]["execution_binding_marker"],
        )

    def test_core_off_route_context_is_unavailable_not_confirmed_direct(self) -> None:
        runtime = object.__new__(DesktopRuntime)
        runtime._exit_quality_route_lock = threading.RLock()
        runtime._exit_quality_route_cache = {}
        runtime._exit_quality_route_cache_at = 0.0
        runtime._runtime_generation = 7
        runtime._core_generation = 11
        runtime.network_mode = "system_proxy"
        runtime.core_runner = _CoreRunner(running=False)

        route = runtime.current_route_context(force=True)

        self.assertEqual(route["selected_chain"], [])
        self.assertEqual(route["leaf_proxy"], "")
        self.assertEqual(route["path_kind"], "unavailable")
        self.assertFalse(route["confirmed"])
        self.assertIn("not running", route["facility_failure"])

    def test_all_identity_sources_failed_but_neutral_probe_succeeds_without_audit(self) -> None:
        delay_client = _DelayClient()
        runtime, service, audit_reasons = self._runtime_for_check(
            _unavailable_identity_payload(),
            delay_client=delay_client,
        )

        result = runtime.check_exit_quality(
            proxy_url="http://198.51.100.7:9999",
            request_id="request-1",
        )

        self._wait_for_identity_call(service)
        self.assertEqual(service.check_calls[0]["request_id"], "request-1")
        self.assertEqual(result["state"], "pending_identity")
        self.assertEqual(
            result["diagnostics"]["neutral_connectivity"]["state"],
            "ok",
        )
        self.assertEqual(
            result["diagnostics"]["neutral_connectivity"]["success_count"],
            2,
        )
        self.assertNotIn("runtime_audit", result["diagnostics"])
        self.assertEqual(audit_reasons, [])
        self.assertEqual(len(delay_client.calls), 2)

    def test_identity_transport_failure_cannot_rotate_a_healthy_data_plane(self) -> None:
        delay_client = _DelayClient()
        runtime, service, _audit_reasons = self._runtime_for_check(
            _identity_transport_failure_payload(
                "SSLEOFError: EOF occurred in violation of protocol"
            ),
            delay_client=delay_client,
        )
        runtime.audit_runtime_egress = lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("identity diagnostics must never rotate the route")
        )

        result = runtime.check_exit_quality(proxy_url=None, request_id="request-transport")

        self.assertEqual(result["state"], "pending_identity")
        self.assertNotIn("egress_recovery", result["diagnostics"])
        self.assertEqual(runtime._test_runtime_probe_failures, [])
        self.assertEqual(len(delay_client.calls), 2)
        self._wait_for_identity_call(service)
        self.assertEqual(len(service.check_calls), 1)

    def test_http_4xx_identity_failure_does_not_rotate(self) -> None:
        delay_client = _DelayClient()
        runtime, service, _audit_reasons = self._runtime_for_check(
            _identity_transport_failure_payload("HTTP status 403 Forbidden"),
            delay_client=delay_client,
        )
        runtime.audit_runtime_egress = lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("HTTP 4xx must not rotate the route")
        )

        result = runtime.check_exit_quality(proxy_url=None, request_id="request-403")

        self.assertEqual(result["state"], "pending_identity")
        self.assertEqual(result["diagnostics"]["neutral_connectivity"]["state"], "ok")
        self.assertNotIn("egress_recovery", result["diagnostics"])
        self._wait_for_identity_call(service)
        self.assertEqual(len(service.check_calls), 1)

    def test_identity_transport_failure_preserves_verified_204_route(self) -> None:
        delay_client = _DelayClient()
        runtime, service, _audit_reasons = self._runtime_for_check(
            _identity_transport_failure_payload("TLS handshake unexpected EOF"),
            delay_client=delay_client,
        )

        class EffectiveManager:
            def __init__(self) -> None:
                self.effective_egress_ok = True
                self.states: list[tuple[bool | None, str]] = []

            def set_effective_egress_state(self, ok: bool | None, reason: str = "") -> None:
                self.states.append((ok, reason))

        manager = EffectiveManager()
        runtime.manager = manager
        runtime.audit_runtime_egress = lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("identity diagnostics must never own recovery")
        )

        result = runtime.check_exit_quality(proxy_url=None, request_id="request-exhausted")

        self._wait_for_identity_call(service)
        self.assertEqual(len(service.check_calls), 1)
        self.assertEqual(result["state"], "pending_identity")
        self.assertNotIn("egress_recovery", result["diagnostics"])
        self.assertEqual(runtime._test_runtime_probe_failures, [])
        # Identity remains an unavailable diagnostic, but it must not demote
        # a route whose authoritative FINAL-EXIT 204 probe is healthy.
        self.assertTrue(manager.effective_egress_ok)
        self.assertEqual(manager.states, [])

    def test_exit_quality_check_fails_before_network_when_core_is_unavailable(self) -> None:
        delay_client = _DelayClient()
        runtime, service, audit_reasons = self._runtime_for_check(
            _unavailable_identity_payload(),
            delay_client=delay_client,
        )
        runtime.core_runner.running = False

        result = runtime.check_exit_quality(
            proxy_url=None,
            request_id="request-offline",
        )

        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(
            result["diagnostics"]["neutral_connectivity"]["state"],
            "not_run",
        )
        self.assertEqual(service.check_calls, [])
        self.assertEqual(delay_client.calls, [])
        self.assertEqual(audit_reasons, [])

    def test_static_refresh_defers_without_core_and_never_calls_service(self) -> None:
        refresh_calls: list[dict[str, Any]] = []

        class StaticService:
            @staticmethod
            def refresh_static_sources(**kwargs: Any) -> dict[str, Any]:
                refresh_calls.append(dict(kwargs))
                return {}

        runtime = object.__new__(DesktopRuntime)
        runtime.exit_quality_service = StaticService()
        runtime.core_runner = _CoreRunner(running=False)
        runtime.logger = logging.getLogger("test.exit-quality-runtime")
        runtime.stop_event = threading.Event()
        runtime._exit_quality_static_refresh_cancel_event = threading.Event()

        self.assertFalse(runtime.refresh_exit_quality_static_data_async(force=True))
        self.assertEqual(refresh_calls, [])

    def test_neutral_failure_uses_global_coordinator_not_identity_audit(self) -> None:
        delay_client = _DelayClient(error=TimeoutError("neutral probe timed out"))
        runtime, _service, audit_reasons = self._runtime_for_check(
            _unavailable_identity_payload(),
            delay_client=delay_client,
        )

        result = runtime.check_exit_quality(
            proxy_url="http://127.0.0.1:7890",
            request_id="request-2",
        )

        self.assertEqual(audit_reasons, [])
        self.assertEqual(runtime._test_runtime_probe_failures, [])
        self.assertEqual(
            result["diagnostics"]["neutral_connectivity"]["state"],
            "failed",
        )
        self.assertEqual(
            result["diagnostics"]["neutral_connectivity"]["outcome"],
            "backup_required",
        )
        self.assertNotIn("runtime_audit", result["diagnostics"])

    def test_neutral_failure_does_not_run_legacy_audit_or_recheck_identity(self) -> None:
        delay_client = _DelayClient(error=TimeoutError("neutral probe timed out"))
        runtime, service, _audit_reasons = self._runtime_for_check(
            _unavailable_identity_payload(),
            delay_client=delay_client,
        )
        runtime.audit_runtime_egress = lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy audit must not race the global coordinator")
        )

        result = runtime.check_exit_quality(proxy_url=None, request_id="legacy-recheck")

        self._wait_for_identity_call(service)
        self.assertEqual(service.check_calls, [])
        self.assertEqual(result["state"], "data_plane_failed")
        self.assertEqual(runtime._test_runtime_probe_failures, [])
        self.assertNotIn("runtime_audit", result["diagnostics"])

    def test_reputation_provider_failure_does_not_trigger_runtime_degradation(self) -> None:
        payload = {
            "schema_version": 2,
            "request_id": "request-3",
            "state": "partial",
            "identity": {
                "ipv4": {
                    "family": 4,
                    "state": "confirmed",
                    "confidence": "medium",
                    "ip": "1.1.1.1",
                },
                "ipv6": {
                    "family": 6,
                    "state": "unavailable",
                    "confidence": "none",
                    "ip": None,
                },
            },
            "diagnostics": {
                "all_identity_failed": False,
                "source_errors": ["proxycheck"],
            },
            "route": _logical_route("edge-a"),
        }
        delay_client = _DelayClient()
        runtime, _service, audit_reasons = self._runtime_for_check(
            payload,
            delay_client=delay_client,
        )
        current_route = _logical_route("edge-a", core_instance="8:12:202")
        runtime.current_route_context = lambda force=False: copy.deepcopy(
            current_route
        )

        result = runtime.check_exit_quality(
            proxy_url=None,
            request_id="request-3",
        )

        self.assertEqual(result["state"], "pending_identity")
        self.assertEqual(result["request_id"], "request-3")
        self.assertEqual(result["identity_source"], "cache")
        self.assertEqual(
            result["diagnostics"]["identity_cache"]["source_request_id"],
            "request-3",
        )
        self.assertEqual(result["identity"]["ipv4"]["state"], "confirmed")
        self.assertEqual(result["diagnostics"]["source_errors"], ["proxycheck"])
        self.assertEqual(
            result["diagnostics"]["neutral_connectivity"]["state"],
            "ok",
        )
        self.assertEqual(len(delay_client.calls), 2)
        self.assertEqual(audit_reasons, [])


if __name__ == "__main__":
    unittest.main()
