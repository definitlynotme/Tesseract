from __future__ import annotations

from dataclasses import FrozenInstanceError
import threading
import time
import unittest

from runtime_egress_recovery import (
    BoundRuntimeFailure,
    CoreLoadedRouteSnapshot,
    EgressRecoveryPolicy,
    LayerRecoveryDirective,
    NodeHealthObservation,
    NodeHealthTransition,
    RouteStabilityState,
    RuntimeEgressCoordinator,
    RuntimeFailureCircuit,
    RuntimeIncidentProbeContract,
    RuntimeNodeRef,
    RuntimeRecoveryOutcome,
    RuntimeRecoveryStatus,
    RuntimeRouteBinding,
    Tier3RuntimeFailureCircuit,
    Tier3RuntimeFailureEvent,
    all_identity_attempts_transport_failed,
    egress_recovery_policy,
    final_exit_transport_failure_from_log,
    identity_attempt_error_is_transport,
    runtime_transport_failure_from_log,
    runtime_destination_token,
    should_rotate_after_identity_failure,
)


def unavailable_payload(error: str, *, stable: bool = True) -> dict:
    return {
        "identity": {
            "ipv4": {
                "state": "unavailable",
                "evidence": [
                    {
                        "source": "ident-v4",
                        "attempts": [
                            {"round": 1, "status": "error", "error": error},
                            {"round": 2, "status": "error", "error": error},
                        ],
                    }
                ],
            },
            "ipv6": {
                "state": "unavailable",
                "evidence": [
                    {
                        "source": "ident-v6",
                        "attempts": [
                            {"round": 1, "status": "error", "error": error},
                        ],
                    }
                ],
            },
        },
        "route": {
            "confirmed_before": True,
            "confirmed_after": True,
            "stable_during_check": stable,
        },
    }


class _ObservedRLock:
    """RLock test double that exposes when one named contender enters."""

    def __init__(self, watched_thread_name: str) -> None:
        self._lock = threading.RLock()
        self._watched_thread_name = watched_thread_name
        self.attempted = threading.Event()
        self.blocked = threading.Event()
        self.acquired = threading.Event()

    def __enter__(self):
        watched = threading.current_thread().name == self._watched_thread_name
        if not watched:
            self._lock.acquire()
            return self
        if self._lock.acquire(blocking=False):
            self.acquired.set()
            self.attempted.set()
            return self
        self.blocked.set()
        self.attempted.set()
        self._lock.acquire()
        self.acquired.set()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self._lock.release()


class RuntimeEgressRecoveryTests(unittest.TestCase):
    @staticmethod
    def _binding(
        *,
        route_epoch: int = 3,
        digest: str = "digest",
        tier: str = "tier2",
        pool_kind: str = "subscription",
        identity: str = "identity-a",
        proxy_name: str = "leaf-a",
        observed_outbound: str = "GENERAL",
    ) -> RuntimeRouteBinding:
        return RuntimeRouteBinding(
            process_pid=4321,
            process_generation=2,
            core_generation=7,
            semantic_digest=digest,
            route_epoch=route_epoch,
            observed_outbound=observed_outbound,
            route_chain=(observed_outbound, "FINAL-EXIT", proxy_name),
            node=RuntimeNodeRef(
                tier=tier,
                pool_kind=pool_kind,
                identity=identity,
                proxy_name=proxy_name,
            ),
        )

    @staticmethod
    def _snapshot(
        *,
        route_epoch: int = 3,
        digest: str = "digest",
        process_pid: int = 4321,
        process_generation: int = 2,
        core_generation: int = 7,
    ) -> CoreLoadedRouteSnapshot:
        return CoreLoadedRouteSnapshot(
            process_pid=process_pid,
            process_generation=process_generation,
            core_generation=core_generation,
            semantic_digest=digest,
            route_epoch=route_epoch,
        )

    @staticmethod
    def _parsed(
        destination: str,
        *,
        at: float,
        outbound: str = "GENERAL",
        protocol: str = "TCP",
        error: str = "context deadline exceeded",
        source_port: int | None = 55001,
        extra: dict | None = None,
    ):
        source = (
            f"127.0.0.1:{source_port} " if source_port is not None else ""
        )
        record = {
            "type": "warning",
            "payload": (
                f"[{protocol}] dial {outbound} (match Match/) "
                f"{source}--> {destination}:443 error: {error}"
            ),
        }
        record.update(extra or {})
        return runtime_transport_failure_from_log(
            record,
            observed_at_monotonic=at,
        )

    @classmethod
    def _bound(
        cls,
        destination: str,
        *,
        at: float,
        binding: RuntimeRouteBinding | None = None,
        source_port: int | None = 55001,
        error: str = "context deadline exceeded",
        extra: dict | None = None,
    ) -> BoundRuntimeFailure:
        selected_binding = binding or cls._binding()
        parsed = cls._parsed(
            destination,
            at=at,
            outbound=selected_binding.observed_outbound,
            source_port=source_port,
            error=error,
            extra=extra,
        )
        assert parsed is not None
        result = BoundRuntimeFailure.from_parsed(parsed, selected_binding)
        assert result is not None
        return result

    @staticmethod
    def _failure(destination: str, at: float) -> Tier3RuntimeFailureEvent:
        return Tier3RuntimeFailureEvent(
            core_generation=7,
            semantic_digest="digest",
            selected_leaf="leaf-a",
            selected_identity="identity-a",
            destination=destination,
            error_type="timeout",
            observed_at_monotonic=at,
        )

    def test_real_mihomo_warning_is_parsed_without_retaining_destination(self) -> None:
        event = runtime_transport_failure_from_log(
            {
                "type": "warning",
                "payload": (
                    "[TCP] dial GENERAL (match Match/) 127.0.0.1:55231 "
                    "--> cp.cloudflare.com:443 error: context deadline exceeded"
                ),
            },
            observed_at_monotonic=10.0,
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.protocol, "TCP")
        self.assertEqual(event.observed_outbound, "GENERAL")
        self.assertEqual(event.error_type, "timeout")
        self.assertEqual(event.observed_at_monotonic, 10.0)
        self.assertTrue(event.flow_token)
        self.assertNotEqual(event.destination_token, "cp.cloudflare.com")
        self.assertNotIn("cloudflare", repr(event))
        self.assertNotIn("55231", repr(event))
        self.assertFalse(hasattr(event, "destination"))

    def test_tcp_udp_and_transport_error_corpus(self) -> None:
        corpus = (
            (
                "TCP",
                "GENERAL",
                "cp.cloudflare.com",
                "connectex: No connection could be made",
                "connection_refused",
            ),
            (
                "UDP",
                "FINAL-EXIT",
                "dns.google",
                "read udp 10.0.0.2:53000: i/o timeout",
                "timeout",
            ),
            (
                "TCP",
                "PROXY-FALLBACK",
                "www.gstatic.com",
                "remote error: tls: handshake failure",
                "tls",
            ),
            (
                "TCP",
                "TIER2-AUTO",
                "example.net",
                "lookup example.net: no such host",
                "dns",
            ),
            (
                "TCP",
                "GENERAL",
                "example.org",
                "read: connection reset by peer",
                "connection_reset",
            ),
            (
                "TCP",
                "GENERAL",
                "example.org",
                "wsarecv: An existing connection was forcibly closed by the remote host",
                "connection_reset",
            ),
            (
                "TCP",
                "GENERAL",
                "[2606:4700:4700::1111]",
                "unexpected EOF",
                "eof",
            ),
            (
                "TCP",
                "GENERAL",
                "198.51.100.22",
                "connect: network is unreachable",
                "network_unreachable",
            ),
        )
        for protocol, outbound, destination, error, error_type in corpus:
            with self.subTest(protocol=protocol, outbound=outbound, error=error):
                endpoint = (
                    f"{destination}:443"
                    if not destination.startswith("[")
                    else f"{destination}:443"
                )
                parsed = runtime_transport_failure_from_log(
                    {
                        "type": "warning",
                        "payload": (
                            f"[{protocol}] dial {outbound} (match Match/) "
                            f"10.0.0.1:50000 --> {endpoint} error: {error}"
                        ),
                    },
                    observed_at_monotonic=20.0,
                )
                self.assertIsNotNone(parsed)
                assert parsed is not None
                self.assertEqual(parsed.protocol, protocol)
                self.assertEqual(parsed.observed_outbound, outbound)
                self.assertEqual(parsed.error_type, error_type)

    def test_destination_distinctness_ignores_port_but_uses_keyed_token(self) -> None:
        first = self._parsed("cp.cloudflare.com", at=1.0)
        second = runtime_transport_failure_from_log(
            {
                "type": "warning",
                "payload": (
                    "[TCP] dial GENERAL (match Match/) 127.0.0.1:1 "
                    "--> cp.cloudflare.com:8443 error: i/o timeout"
                ),
            },
            observed_at_monotonic=2.0,
        )
        other = self._parsed("www.gstatic.com", at=3.0)
        assert first is not None and second is not None and other is not None
        self.assertEqual(first.destination_token, second.destination_token)
        self.assertNotEqual(first.destination_token, other.destination_token)

    def test_parser_rejects_non_data_plane_and_application_failures(self) -> None:
        rejected = (
            "[TCP] dial DIRECT (match FINAL-EXIT) 10.0.0.1:1 --> example.com:443 error: i/o timeout",
            "[TCP] dial GENERAL (match Match/) 10.0.0.1:1 --> localhost:443 error: timeout",
            "[TCP] dial GENERAL (match Match/) 10.0.0.1:1 --> 127.0.0.1:9090 error: timeout",
            "[TCP] dial GENERAL health-check --> example.com:443 error: timeout",
            "[TCP] dial GENERAL controller request --> example.com:443 error: timeout",
            "[TCP] dial GENERAL (match Match/) --> example.com:443 error: context canceled",
            "[TCP] dial GENERAL (match Match/) --> example.com:443 error: HTTP status 503",
            "[TCP] dial GENERAL (match Match/) --> example.com:443 error: invalid JSON response body",
            "dial GENERAL without an arrow error: timeout",
        )
        for line in rejected:
            with self.subTest(line=line):
                self.assertIsNone(
                    runtime_transport_failure_from_log(
                        {"type": "warning", "payload": line}
                    )
                )
        self.assertIsNone(
            runtime_transport_failure_from_log(
                {
                    "type": "info",
                    "payload": (
                        "[TCP] dial GENERAL (match Match/) --> example.com:443 "
                        "error: timeout"
                    ),
                }
            )
        )

    def test_raw_mihomo_info_level_dial_error_is_not_business_failure(self) -> None:
        info_line = (
            'time="2026-08-23T12:00:00+08:00" level=info '
            'msg="[TCP] dial GENERAL (match Match/) 127.0.0.1:50000 '
            '--> cp.cloudflare.com:443 error: i/o timeout"'
        )
        warning_line = info_line.replace("level=info", "level=warning")

        self.assertIsNone(runtime_transport_failure_from_log(info_line))
        parsed = runtime_transport_failure_from_log(warning_line)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(
            parsed.destination_token,
            runtime_destination_token("cp.cloudflare.com"),
        )

    def test_final_exit_compatibility_parser_uses_privacy_token(self) -> None:
        event = final_exit_transport_failure_from_log(
            {
                "type": "warning",
                "payload": (
                    "[TCP] dial FINAL-EXIT (match Match/) --> "
                    "cp.cloudflare.com:443 error: i/o timeout"
                ),
            },
            core_generation=7,
            semantic_digest="digest",
            selected_leaf="leaf-a",
            selected_identity="identity-a",
            observed_at_monotonic=10.0,
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertNotEqual(event.destination, "cp.cloudflare.com")
        self.assertEqual(event.binding, (7, "digest", "leaf-a", "identity-a"))
        self.assertIsNone(
            final_exit_transport_failure_from_log(
                {
                    "type": "warning",
                    "payload": (
                        "[TCP] dial GENERAL (match Match/) --> "
                        "cp.cloudflare.com:443 error: timeout"
                    ),
                },
                core_generation=7,
                semantic_digest="digest",
                selected_leaf="leaf-a",
            )
        )

    def test_loaded_snapshot_and_route_binding_are_immutable(self) -> None:
        binding = self._binding()
        snapshot = CoreLoadedRouteSnapshot(
            process_pid=4321,
            process_generation=2,
            core_generation=7,
            semantic_digest="digest",
            route_epoch=3,
            group_graph={"GENERAL": ["FINAL-EXIT"], "FINAL-EXIT": ["leaf-a"]},
            proxy_nodes=[binding.node],
            loaded_at_monotonic=4.0,
        )
        self.assertTrue(snapshot.owns(binding))
        self.assertIsInstance(snapshot.group_graph, tuple)
        self.assertIsInstance(snapshot.group_graph[0][1], tuple)
        self.assertIsInstance(snapshot.proxy_nodes, tuple)
        with self.assertRaises(FrozenInstanceError):
            binding.route_epoch = 4  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            snapshot.route_epoch = 4  # type: ignore[misc]

    def test_runtime_recovery_outcome_has_only_the_six_terminal_states(self) -> None:
        self.assertEqual(
            {status.value for status in RuntimeRecoveryStatus},
            {
                "recovered",
                "fail_closed",
                "facility_hold",
                "controller_hold",
                "stale",
                "manual_superseded",
            },
        )
        outcome = RuntimeRecoveryOutcome(
            status="recovered",  # type: ignore[arg-type]
            incident_id="incident-a",
            route_epoch=3,
            budget_ms=2_000,
            reason="replacement_verified",
        )
        self.assertIs(outcome.status, RuntimeRecoveryStatus.RECOVERED)
        with self.assertRaises(ValueError):
            RuntimeRecoveryOutcome(
                status="indeterminate",  # type: ignore[arg-type]
                incident_id="incident-a",
                route_epoch=3,
                budget_ms=2_000,
                reason="candidate_unresolved",
            )
        with self.assertRaises(FrozenInstanceError):
            outcome.reason = "changed"  # type: ignore[misc]

    def test_runtime_recovery_outcome_keeps_control_refs_but_serializes_hashes(
        self,
    ) -> None:
        from_ref = RuntimeNodeRef(
            tier="tier2",
            pool_kind="main",
            identity="vless://private-from-identity",
            proxy_name="private-from-proxy",
        )
        to_ref = RuntimeNodeRef(
            tier="tier2",
            pool_kind="main",
            identity="vless://private-to-identity",
            proxy_name="private-to-proxy",
        )
        incident_id = "private-incident-id"
        outcome = RuntimeRecoveryOutcome(
            status=RuntimeRecoveryStatus.RECOVERED,
            incident_id=incident_id,
            route_epoch=8,
            budget_ms=6_000,
            reason=(
                f"switched {from_ref.identity} to {to_ref.proxy_name} "
                f"for {incident_id}"
            ),
            from_ref=from_ref,
            to_ref=to_ref,
        )

        self.assertIs(outcome.from_ref, from_ref)
        self.assertIs(outcome.to_ref, to_ref)
        serialized = outcome.to_dict()
        self.assertEqual(serialized["status"], "recovered")
        self.assertEqual(serialized["route_epoch"], 8)
        self.assertEqual(serialized["budget_ms"], 6_000)
        self.assertNotIn("incident_id", serialized)
        self.assertTrue(serialized["incident_hash"].startswith("hmac:"))
        self.assertEqual(
            set(serialized["from_ref"]),
            {"tier", "pool_kind", "identity_hash", "proxy_name_hash"},
        )
        self.assertEqual(
            set(serialized["to_ref"]),
            {"tier", "pool_kind", "identity_hash", "proxy_name_hash"},
        )
        for private_value in (
            incident_id,
            from_ref.identity,
            from_ref.proxy_name,
            to_ref.identity,
            to_ref.proxy_name,
        ):
            self.assertNotIn(private_value, repr(serialized))

    def test_incident_probe_contract_is_immutable_and_privacy_safe(self) -> None:
        contract = RuntimeIncidentProbeContract(
            probe_url="https://private-probe.example/generate_204",
            expected_status="204",
            method="get",
        )

        self.assertEqual(contract.method, "GET")
        self.assertEqual(
            contract.contract_key,
            (
                "GET",
                "https://private-probe.example/generate_204",
                "204",
            ),
        )
        serialized = contract.to_dict()
        self.assertTrue(serialized["probe_url_hash"].startswith("hmac:"))
        self.assertNotIn("private-probe.example", repr(serialized))
        with self.assertRaises(FrozenInstanceError):
            contract.probe_url = "https://other.example/"  # type: ignore[misc]

    def test_incident_accepts_probe_result_only_for_its_frozen_contract(
        self,
    ) -> None:
        binding = self._binding()
        event = self._bound("cp.cloudflare.com", at=10.0, binding=binding)
        contract = RuntimeIncidentProbeContract(
            probe_url="https://probe-a.example/generate_204",
        )
        different = RuntimeIncidentProbeContract(
            probe_url="https://probe-b.example/generate_204",
        )
        circuit = RuntimeFailureCircuit(active_binding=binding)

        opened = circuit.observe(event, probe_contract=contract)
        self.assertIs(opened.probe_contract, contract)
        mismatch = circuit.record_probe(
            event,
            succeeded=False,
            probe_token=opened.probe_token,
            probe_contract=different,
        )
        missing = circuit.record_probe(
            event,
            succeeded=False,
            probe_token=opened.probe_token,
        )
        accepted = circuit.record_probe(
            event,
            succeeded=False,
            probe_token=opened.probe_token,
            probe_contract=contract,
        )

        self.assertFalse(mismatch.accepted)
        self.assertTrue(mismatch.stale)
        self.assertEqual(mismatch.reason, "incident_probe_contract_mismatch")
        self.assertFalse(missing.accepted)
        self.assertEqual(missing.reason, "incident_probe_contract_mismatch")
        self.assertTrue(accepted.confirmed)
        self.assertIs(circuit.snapshot().probe_contract, contract)

    def test_all_terminal_outcomes_hold_the_same_incident_until_reset(self) -> None:
        binding = self._binding()
        for status in RuntimeRecoveryStatus:
            with self.subTest(status=status.value):
                circuit = RuntimeFailureCircuit(active_binding=binding)
                event = self._bound(
                    f"{status.value}.example",
                    at=10.0,
                    binding=binding,
                )
                opened = circuit.observe(event)
                terminal = RuntimeRecoveryOutcome(
                    status=status,
                    incident_id=opened.incident_id,
                    route_epoch=binding.route_epoch,
                    budget_ms=500,
                    reason="terminal_test",
                )
                snapshot = circuit.mark_terminal_outcome(
                    terminal,
                    binding=binding,
                    quarantine_token=opened.probe_token,
                )
                replayed = circuit.observe(
                    self._bound(
                        f"replay-{status.value}.example",
                        at=time.monotonic() + 0.1,
                        binding=binding,
                    )
                )

                self.assertEqual(snapshot.terminal_status, status)
                self.assertTrue(replayed.accepted)
                self.assertTrue(replayed.deduplicated)
                self.assertFalse(replayed.start_probe)
                self.assertEqual(replayed.incident_id, opened.incident_id)
                self.assertEqual(replayed.probe_token, opened.probe_token)
                self.assertEqual(replayed.terminal_status, status)

    def test_bound_failure_requires_the_observed_outbound(self) -> None:
        parsed = self._parsed("cp.cloudflare.com", at=10.0)
        assert parsed is not None
        binding = self._binding()
        self.assertIsNotNone(BoundRuntimeFailure.from_parsed(parsed, binding))
        self.assertIsNone(
            BoundRuntimeFailure.from_parsed(
                parsed,
                self._binding(observed_outbound="FINAL-EXIT"),
            )
        )

    def test_generic_circuit_requires_independent_flows_before_diverting(self) -> None:
        binding = self._binding()
        first_parsed = self._parsed(
            "cp.cloudflare.com",
            at=10.0,
            source_port=55001,
        )
        same_flow_second_target = self._parsed(
            "www.gstatic.com",
            at=11.0,
            source_port=55001,
        )
        second_parsed = self._parsed(
            "www.gstatic.com",
            at=12.1,
            source_port=55002,
        )
        assert first_parsed is not None and second_parsed is not None
        first = BoundRuntimeFailure.from_parsed(first_parsed, binding)
        same_flow = BoundRuntimeFailure.from_parsed(
            same_flow_second_target, binding  # type: ignore[arg-type]
        )
        second = BoundRuntimeFailure.from_parsed(second_parsed, binding)
        assert first is not None and same_flow is not None and second is not None
        circuit = RuntimeFailureCircuit(active_binding=binding)

        decision = circuit.observe(first)
        self.assertTrue(decision.start_probe)
        duplicate = circuit.observe(first)
        self.assertTrue(duplicate.deduplicated)
        self.assertEqual(circuit.snapshot().failure_count, 1)
        one_flow = circuit.observe(same_flow)
        self.assertFalse(one_flow.confirmed)
        self.assertFalse(one_flow.divert_recommended)
        correlated = circuit.observe(second)
        self.assertFalse(correlated.confirmed)
        self.assertTrue(correlated.divert_recommended)
        self.assertEqual(
            correlated.stability_state,
            RouteStabilityState.DIVERTED,
        )
        self.assertEqual(circuit.snapshot().failure_count, 2)
        self.assertEqual(circuit.snapshot().state, "suspect")
        serialized = circuit.snapshot().to_dict()
        self.assertEqual(serialized["destination_count"], 2)
        self.assertEqual(serialized["flow_count"], 2)
        self.assertEqual(serialized["cluster_count"], 2)
        self.assertNotIn("identity", serialized)
        self.assertTrue(serialized["identity_hash"].startswith("hmac:"))
        self.assertNotIn(binding.node.identity, repr(serialized))
        self.assertNotIn("destination_tokens", serialized)
        self.assertNotIn("cloudflare", repr(serialized))

    def test_anonymous_burst_never_reissues_the_incident_probe(self) -> None:
        binding = self._binding()
        circuit = RuntimeFailureCircuit(active_binding=binding)

        first = circuit.observe(
            self._bound(
                "cp.cloudflare.com",
                at=10.0,
                binding=binding,
                source_port=None,
            )
        )
        within_gate = circuit.observe(
            self._bound(
                "www.gstatic.com",
                at=10.5,
                binding=binding,
                source_port=None,
            )
        )
        after_gate = circuit.observe(
            self._bound(
                "third.example",
                at=12.1,
                binding=binding,
                source_port=None,
            )
        )

        self.assertTrue(first.start_probe)
        self.assertFalse(within_gate.start_probe)
        self.assertFalse(after_gate.start_probe)
        self.assertFalse(within_gate.divert_recommended)
        self.assertFalse(after_gate.divert_recommended)
        self.assertFalse(after_gate.confirmed)
        snapshot = circuit.snapshot()
        self.assertEqual(snapshot.failure_count, 1)
        self.assertEqual(snapshot.to_dict()["cluster_count"], 0)
        self.assertEqual(snapshot.probe_intent_count, 1)
        self.assertEqual(first.probe_token, after_gate.probe_token)

    def test_one_destination_across_many_flows_does_not_divert(self) -> None:
        binding = self._binding()
        circuit = RuntimeFailureCircuit(active_binding=binding)
        decisions = [
            circuit.observe(
                self._bound(
                    "one-site.example",
                    at=10.0 + index,
                    binding=binding,
                    source_port=55001 + index,
                )
            )
            for index in range(3)
        ]

        self.assertTrue(all(not decision.confirmed for decision in decisions))
        self.assertTrue(
            all(not decision.divert_recommended for decision in decisions)
        )
        self.assertEqual(circuit.snapshot().to_dict()["destination_count"], 1)

    def test_mux_session_collapses_distinct_application_flows(self) -> None:
        binding = self._binding()
        circuit = RuntimeFailureCircuit(active_binding=binding)
        shared_mux = {"mux_id": "private-session-a"}

        circuit.observe(
            self._bound(
                "cp.cloudflare.com",
                at=10.0,
                binding=binding,
                source_port=55001,
                extra=shared_mux,
            )
        )
        same_mux = circuit.observe(
            self._bound(
                "www.gstatic.com",
                at=11.0,
                binding=binding,
                source_port=55002,
                extra=shared_mux,
            )
        )
        independent_mux = circuit.observe(
            self._bound(
                "third.example",
                at=12.0,
                binding=binding,
                source_port=55003,
                extra={"mux_id": "private-session-b"},
            )
        )

        self.assertFalse(same_mux.divert_recommended)
        self.assertTrue(independent_mux.divert_recommended)
        self.assertEqual(circuit.snapshot().to_dict()["cluster_count"], 2)
        self.assertNotIn("private-session", repr(circuit.snapshot()))

    def test_three_slow_clusters_recommend_diversion_within_thirty_seconds(self) -> None:
        binding = self._binding()
        circuit = RuntimeFailureCircuit(active_binding=binding)

        first = circuit.observe(
            self._bound(
                "first.example",
                at=10.0,
                binding=binding,
                source_port=55001,
            )
        )
        second = circuit.observe(
            self._bound(
                "second.example",
                at=18.0,
                binding=binding,
                source_port=55002,
            )
        )
        third = circuit.observe(
            self._bound(
                "first.example",
                at=29.0,
                binding=binding,
                source_port=55003,
            )
        )

        self.assertFalse(first.divert_recommended)
        self.assertFalse(second.divert_recommended)
        self.assertTrue(third.divert_recommended)
        self.assertEqual(third.reason, "intermittent_failure_clusters")
        self.assertFalse(third.confirmed)

    def test_deterministic_proxy_endpoint_recommends_diversion_not_health(self) -> None:
        binding = self._binding()
        event = self._bound(
            "cp.cloudflare.com",
            at=10.0,
            binding=binding,
            error="proxy authentication failed",
            extra={"failure_phase": "proxy_authentication"},
        )
        circuit = RuntimeFailureCircuit(active_binding=binding)

        decision = circuit.observe(event)

        self.assertTrue(event.deterministic_proxy_endpoint)
        self.assertTrue(decision.deterministic)
        self.assertTrue(decision.divert_recommended)
        self.assertFalse(decision.confirmed)
        self.assertEqual(decision.stability_state, RouteStabilityState.DIVERTED)

    def test_structured_facility_evidence_never_confirms_the_leaf(self) -> None:
        binding = self._binding()
        event = self._bound(
            "cp.cloudflare.com",
            at=10.0,
            binding=binding,
            extra={"failure_domain": "facility"},
        )
        circuit = RuntimeFailureCircuit(active_binding=binding)

        decision = circuit.observe(event)

        self.assertTrue(decision.facility)
        self.assertFalse(decision.start_probe)
        self.assertFalse(decision.divert_recommended)
        self.assertFalse(decision.confirmed)
        self.assertEqual(
            circuit.snapshot().stability_state,
            RouteStabilityState.FACILITY_HOLD,
        )

    def test_generic_circuit_rejects_stale_route_ownership(self) -> None:
        current = self._binding(route_epoch=4)
        stale_binding = self._binding(route_epoch=3)
        parsed = self._parsed("cp.cloudflare.com", at=10.0)
        assert parsed is not None
        stale_event = BoundRuntimeFailure.from_parsed(parsed, stale_binding)
        assert stale_event is not None
        circuit = RuntimeFailureCircuit(active_binding=current)
        decision = circuit.observe(stale_event, active_binding=current)
        self.assertFalse(decision.accepted)
        self.assertTrue(decision.stale)
        self.assertEqual(circuit.snapshot().state, "closed")

        stale_snapshot = CoreLoadedRouteSnapshot(
            process_pid=9999,
            process_generation=3,
            core_generation=8,
            semantic_digest="next",
            route_epoch=5,
        )
        current_event = BoundRuntimeFailure.from_parsed(parsed, current)
        assert current_event is not None
        decision = circuit.observe(
            current_event,
            current_snapshot=stale_snapshot,
        )
        self.assertFalse(decision.accepted)
        self.assertTrue(decision.stale)

    def test_generic_probe_token_prevents_old_probe_from_confirming(self) -> None:
        binding = self._binding()
        parsed = self._parsed("cp.cloudflare.com", at=10.0)
        assert parsed is not None
        event = BoundRuntimeFailure.from_parsed(parsed, binding)
        assert event is not None
        circuit = RuntimeFailureCircuit(active_binding=binding)
        first = circuit.observe(event)
        stale = circuit.record_probe(
            event,
            succeeded=False,
            probe_token="old-probe-token",
        )
        self.assertFalse(stale.accepted)
        self.assertTrue(stale.stale)
        facility = circuit.record_probe(
            event,
            succeeded=False,
            facility_failure=True,
            probe_token=first.probe_token,
        )
        self.assertTrue(facility.accepted)
        self.assertFalse(facility.confirmed)
        withheld = circuit.record_probe(
            event,
            succeeded=False,
            probe_token=first.probe_token,
        )
        self.assertFalse(withheld.confirmed)
        self.assertTrue(withheld.facility)
        self.assertEqual(
            circuit.snapshot().stability_state,
            RouteStabilityState.FACILITY_HOLD,
        )

        circuit.reset(binding)
        reopened = circuit.observe(event)
        confirmed = circuit.record_probe(
            event,
            succeeded=False,
            probe_token=reopened.probe_token,
        )
        self.assertTrue(confirmed.confirmed)

    def test_generic_probe_rejects_empty_token(self) -> None:
        binding = self._binding()
        event = self._bound("cp.cloudflare.com", at=10.0, binding=binding)
        circuit = RuntimeFailureCircuit(active_binding=binding)
        circuit.observe(event)

        missing = circuit.record_probe(
            event,
            succeeded=False,
            probe_token="",
        )

        self.assertFalse(missing.accepted)
        self.assertEqual(missing.reason, "missing_probe_token")
        self.assertEqual(circuit.snapshot().state, "suspect")

    def test_generic_circuit_rejects_out_of_order_observation(self) -> None:
        binding = self._binding()
        circuit = RuntimeFailureCircuit(active_binding=binding)
        first = self._bound("cp.cloudflare.com", at=10.0, binding=binding)
        later_duplicate = self._bound(
            "cp.cloudflare.com",
            at=12.0,
            binding=binding,
        )
        reversed_distinct = self._bound(
            "www.gstatic.com",
            at=11.0,
            binding=binding,
        )

        circuit.observe(first)
        circuit.observe(later_duplicate)
        rejected = circuit.observe(reversed_distinct)

        self.assertFalse(rejected.accepted)
        self.assertTrue(rejected.stale)
        self.assertEqual(rejected.reason, "out_of_order_observation")
        snapshot = circuit.snapshot()
        self.assertEqual(snapshot.state, "suspect")
        self.assertEqual(snapshot.failure_count, 1)
        self.assertEqual(snapshot.last_observed_monotonic, 12.0)

    def test_independent_probe_cannot_clear_probe_confirmation(self) -> None:
        binding = self._binding()
        first_event = self._bound(
            "cp.cloudflare.com",
            at=10.0,
            binding=binding,
            source_port=55001,
        )
        second_event = self._bound(
            "www.gstatic.com",
            at=11.0,
            binding=binding,
            source_port=55002,
        )
        circuit = RuntimeFailureCircuit(active_binding=binding)

        first = circuit.observe(first_event)
        correlated = circuit.observe(second_event)
        self.assertTrue(correlated.divert_recommended)
        confirmed = circuit.record_probe(
            first_event,
            succeeded=False,
            probe_token=first.probe_token,
        )
        retained = circuit.record_probe(
            first_event,
            succeeded=True,
            probe_token=first.probe_token,
        )

        self.assertTrue(confirmed.confirmed)
        self.assertTrue(retained.confirmed)
        self.assertFalse(retained.cleared)
        self.assertEqual(circuit.snapshot().state, "confirmed")

    def test_coordinator_accepts_burst_nonblocking_and_preserves_fifo(self) -> None:
        coordinator = RuntimeEgressCoordinator(queue_capacity=128)
        coordinator.set_current_snapshot(self._snapshot())
        binding_a = self._binding(identity="identity-a", proxy_name="leaf-a")
        binding_b = self._binding(identity="identity-b", proxy_name="leaf-b")
        submitted: list[BoundRuntimeFailure] = []
        for index in range(100):
            binding = binding_a if index % 2 == 0 else binding_b
            event = self._bound(
                f"destination-{index}.example",
                at=10.0 + index / 100.0,
                binding=binding,
            )
            submitted.append(event)
            decision = coordinator.submit(event)
            self.assertTrue(decision.accepted)
            self.assertEqual(decision.sequence, index + 1)

        queue_state = coordinator.queue_snapshot()
        self.assertEqual(queue_state.pending_count, 100)
        self.assertEqual(queue_state.submitted_count, 100)
        drained = [coordinator.next_queued() for _ in range(100)]
        self.assertEqual(
            [item.sequence for item in drained if item is not None],
            list(range(1, 101)),
        )
        self.assertEqual(
            [item.failure for item in drained if item is not None],
            submitted,
        )
        self.assertIsNone(coordinator.next_queued())

    def test_coordinator_overflow_sets_one_sticky_guard(self) -> None:
        coordinator = RuntimeEgressCoordinator(queue_capacity=3)
        coordinator.set_current_snapshot(self._snapshot())
        events = [
            self._bound(f"destination-{index}.example", at=float(index + 1))
            for index in range(5)
        ]
        for event in events[:3]:
            self.assertTrue(coordinator.submit(event).accepted)
        first_overflow = coordinator.submit(events[3])
        second_overflow = coordinator.submit(events[4])
        self.assertFalse(first_overflow.accepted)
        self.assertTrue(first_overflow.overflowed)
        self.assertTrue(first_overflow.guard_required)
        self.assertFalse(second_overflow.accepted)
        state = coordinator.queue_snapshot()
        self.assertEqual(state.pending_count, 3)
        self.assertEqual(state.dropped_count, 2)
        self.assertTrue(state.overflow_guard_required)
        self.assertTrue(coordinator.consume_overflow_guard())
        self.assertFalse(coordinator.consume_overflow_guard())
        self.assertFalse(coordinator.queue_snapshot().overflow_guard_required)

    def test_coordinator_overflow_does_not_self_wake_when_guard_is_disabled(
        self,
    ) -> None:
        coordinator = RuntimeEgressCoordinator(
            queue_capacity=1,
            automatic_guard_enabled=False,
        )
        self.assertTrue(coordinator.submit_ingress("first").accepted)

        overflow = coordinator.submit_ingress("second")
        coordinator.request_guard("reader_missing")

        self.assertTrue(overflow.overflowed)
        self.assertFalse(overflow.guard_required)
        self.assertFalse(
            coordinator.queue_snapshot().overflow_guard_required
        )
        self.assertIsNone(coordinator.claim_guard())

    def test_indeterminate_incident_only_reopens_on_later_external_error(
        self,
    ) -> None:
        binding = self._binding()
        started = time.monotonic()
        first_event = self._bound(
            "first.example",
            at=started,
            binding=binding,
        )
        circuit = RuntimeFailureCircuit(active_binding=binding)
        opened = circuit.observe(first_event)

        finished = circuit.finish_indeterminate(
            incident_id=opened.incident_id,
            quarantine_token=opened.probe_token,
            binding=binding,
        )
        replay = circuit.observe(
            self._bound(
                "same-burst.example",
                at=started + 1.0,
                binding=binding,
            )
        )
        later = circuit.observe(
            self._bound(
                "later-business-error.example",
                at=started + 3.0,
                binding=binding,
            )
        )

        self.assertEqual(finished.state, "indeterminate")
        self.assertTrue(replay.deduplicated)
        self.assertEqual(replay.reason, "indeterminate_burst_held")
        self.assertTrue(later.start_probe)
        self.assertNotEqual(later.incident_id, opened.incident_id)

    def test_coordinator_owns_raw_ingress_fifo_and_capacity(self) -> None:
        coordinator = RuntimeEgressCoordinator(queue_capacity=3)
        payloads = [object() for _ in range(4)]

        for payload in payloads[:3]:
            self.assertTrue(coordinator.submit_ingress(payload).accepted)
        overflow = coordinator.submit_ingress(payloads[3])

        self.assertFalse(overflow.accepted)
        self.assertTrue(overflow.overflowed)
        self.assertEqual(coordinator.queue_snapshot().pending_count, 3)
        self.assertIs(coordinator.next_ingress(), payloads[0])
        self.assertIs(coordinator.next_ingress(), payloads[1])
        self.assertIs(coordinator.next_ingress(), payloads[2])
        self.assertIsNone(coordinator.next_ingress())

    def test_guard_ack_cannot_clear_overflow_that_arrived_after_claim(self) -> None:
        coordinator = RuntimeEgressCoordinator(queue_capacity=1)
        self.assertTrue(coordinator.submit_ingress("first").accepted)
        self.assertTrue(coordinator.submit_ingress("second").overflowed)
        first_guard = coordinator.claim_guard()
        self.assertIsNotNone(first_guard)

        self.assertTrue(coordinator.submit_ingress("third").overflowed)
        self.assertTrue(
            coordinator.acknowledge_guard(first_guard, completed=True)  # type: ignore[arg-type]
        )
        self.assertTrue(coordinator.queue_snapshot().overflow_guard_required)

        second_guard = coordinator.claim_guard()
        self.assertIsNotNone(second_guard)
        self.assertGreater(second_guard.revision, first_guard.revision)  # type: ignore[union-attr]
        self.assertTrue(
            coordinator.acknowledge_guard(second_guard, completed=True)  # type: ignore[arg-type]
        )
        self.assertFalse(coordinator.queue_snapshot().overflow_guard_required)

    def test_old_epoch_guard_cannot_ack_restarted_coordinator(self) -> None:
        coordinator = RuntimeEgressCoordinator(queue_capacity=1)
        coordinator.request_guard("reader_missing")
        old_guard = coordinator.claim_guard()
        self.assertIsNotNone(old_guard)

        coordinator.restart(current_snapshot=self._snapshot())
        coordinator.request_guard("queue_overflow")

        self.assertFalse(
            coordinator.acknowledge_guard(old_guard, completed=True)  # type: ignore[arg-type]
        )
        self.assertTrue(coordinator.queue_snapshot().overflow_guard_required)

    def test_coordinator_keeps_independent_circuits_per_full_binding(self) -> None:
        coordinator = RuntimeEgressCoordinator()
        snapshot = self._snapshot()
        coordinator.set_current_snapshot(snapshot)
        binding_a = self._binding(
            tier="tier2",
            pool_kind="subscription",
            identity="shared-identity",
            proxy_name="leaf-a",
        )
        binding_b = self._binding(
            tier="tier3",
            pool_kind="tier3b",
            identity="shared-identity",
            proxy_name="leaf-b",
        )
        event_a = self._bound("cp.cloudflare.com", at=10.0, binding=binding_a)
        event_b = self._bound("www.gstatic.com", at=10.1, binding=binding_b)
        self.assertTrue(coordinator.observe(event_a).start_probe)
        self.assertTrue(coordinator.observe(event_b).start_probe)
        snapshots = coordinator.current_snapshots()
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(
            {snapshot.binding.node.tier for snapshot in snapshots if snapshot.binding},
            {"tier2", "tier3"},
        )
        self.assertEqual(
            coordinator.current_snapshot_for(binding_a).failure_count,  # type: ignore[union-attr]
            1,
        )
        self.assertEqual(
            coordinator.current_snapshot_for(binding_b).failure_count,  # type: ignore[union-attr]
            1,
        )

    def test_same_leaf_on_different_route_chains_never_cross_corroborates(self) -> None:
        coordinator = RuntimeEgressCoordinator()
        coordinator.set_current_snapshot(self._snapshot())
        general = self._binding(observed_outbound="GENERAL")
        final_exit = RuntimeRouteBinding(
            process_pid=general.process_pid,
            process_generation=general.process_generation,
            core_generation=general.core_generation,
            semantic_digest=general.semantic_digest,
            route_epoch=general.route_epoch,
            observed_outbound="FINAL-EXIT",
            route_chain=("FINAL-EXIT", general.node.proxy_name),
            node=general.node,
        )

        first = coordinator.observe(
            self._bound("cp.cloudflare.com", at=10.0, binding=general)
        )
        second = coordinator.observe(
            self._bound("www.gstatic.com", at=11.0, binding=final_exit)
        )

        self.assertTrue(first.start_probe)
        self.assertTrue(second.start_probe)
        self.assertFalse(second.confirmed)
        self.assertEqual(len(coordinator.current_snapshots()), 2)

    def test_coordinator_owns_runtime_recovery_state_transitions(self) -> None:
        coordinator = RuntimeEgressCoordinator()
        coordinator.set_current_snapshot(self._snapshot())
        binding = self._binding()
        first = self._bound("cp.cloudflare.com", at=10.0, binding=binding)

        opened = coordinator.observe(first)
        self.assertTrue(
            coordinator.record_probe(
                first,
                succeeded=False,
                probe_token=opened.probe_token,
            ).confirmed
        )
        recovering = coordinator.mark_recovering(
            binding,
            incident_id=opened.incident_id,
            quarantine_token=opened.probe_token,
        )
        quarantined = coordinator.mark_quarantined(
            binding,
            ttl_seconds=30.0,
            incident_id=opened.incident_id,
            quarantine_token=opened.probe_token,
        )
        recovered = coordinator.mark_recovered(
            binding,
            incident_id=opened.incident_id,
            quarantine_token=opened.probe_token,
        )

        self.assertEqual(recovering.state, "recovering")  # type: ignore[union-attr]
        self.assertEqual(quarantined.state, "quarantined")  # type: ignore[union-attr]
        self.assertEqual(recovered.state, "recovered")  # type: ignore[union-attr]
        self.assertIsNotNone(recovered.recovery_latency_ms)  # type: ignore[union-attr]

    def test_terminal_incident_requires_explicit_authority_reset_before_reopen(
        self,
    ) -> None:
        binding = self._binding()
        circuit = RuntimeFailureCircuit(active_binding=binding)
        old = circuit.observe(
            self._bound("cp.cloudflare.com", at=10.0, binding=binding)
        )
        self.assertTrue(
            circuit.record_probe(
                self._bound("cp.cloudflare.com", at=10.0, binding=binding),
                succeeded=False,
                probe_token=old.probe_token,
            ).confirmed
        )
        circuit.mark_recovering(
            binding=binding,
            incident_id=old.incident_id,
            quarantine_token=old.probe_token,
        )
        circuit.mark_recovered(
            binding=binding,
            incident_id=old.incident_id,
            quarantine_token=old.probe_token,
        )

        replayed_at = time.monotonic() + 0.1
        replayed = circuit.observe(
            self._bound(
                "reopened-a.example",
                at=replayed_at,
                binding=binding,
            )
        )
        self.assertTrue(replayed.deduplicated)
        self.assertEqual(replayed.incident_id, old.incident_id)
        self.assertEqual(replayed.probe_token, old.probe_token)
        self.assertEqual(
            replayed.terminal_status,
            RuntimeRecoveryStatus.RECOVERED,
        )
        self.assertFalse(replayed.start_probe)

        # A material authority revision is expressed by the owner explicitly
        # resetting or activating the circuit. Monitor/heartbeat replay alone
        # is never sufficient to open a new transaction.
        circuit.reset(binding)
        reopened_at = replayed_at + 0.1
        new = circuit.observe(
            self._bound(
                "reopened-a.example",
                at=reopened_at,
                binding=binding,
            )
        )
        self.assertNotEqual(new.incident_id, old.incident_id)
        self.assertTrue(
            circuit.record_probe(
                self._bound(
                    "reopened-a.example",
                    at=reopened_at,
                    binding=binding,
                ),
                succeeded=False,
                probe_token=new.probe_token,
            ).confirmed
        )
        new_snapshot = circuit.snapshot()

        for transition in (
            lambda: circuit.mark_recovering(
                binding=binding,
                incident_id=old.incident_id,
                quarantine_token=old.probe_token,
            ),
            lambda: circuit.mark_quarantined(
                binding=binding,
                ttl_seconds=30.0,
                incident_id=old.incident_id,
                quarantine_token=old.probe_token,
            ),
            lambda: circuit.mark_recovered(
                binding=binding,
                incident_id=old.incident_id,
                quarantine_token=old.probe_token,
            ),
        ):
            self.assertEqual(transition(), new_snapshot)

        recovering = circuit.mark_recovering(
            binding=binding,
            incident_id=new.incident_id,
            quarantine_token=new.probe_token,
        )
        self.assertEqual(recovering.state, "recovering")

    def test_coordinator_observe_snapshot_check_and_mutation_are_atomic(self) -> None:
        coordinator = RuntimeEgressCoordinator()
        observed_lock = _ObservedRLock("observe-snapshot-setter")
        coordinator._state_lock = observed_lock
        coordinator.set_current_snapshot(self._snapshot())
        binding = self._binding()
        first = self._bound("cp.cloudflare.com", at=10.0, binding=binding)
        second = self._bound(
            "www.gstatic.com",
            at=11.0,
            binding=binding,
            source_port=55002,
        )
        coordinator.observe(first)
        circuit = coordinator._circuits[first.circuit_key]
        original_observe = circuit.observe
        entered = threading.Event()
        release = threading.Event()
        setter_done = threading.Event()
        results: list[object] = []

        def gated_observe(*args, **kwargs):
            entered.set()
            release.wait(2.0)
            return original_observe(*args, **kwargs)

        circuit.observe = gated_observe  # type: ignore[method-assign]

        observe_thread = threading.Thread(
            target=lambda: results.append(coordinator.observe(second))
        )
        observe_thread.start()
        self.assertTrue(entered.wait(1.0))

        def replace_snapshot() -> None:
            coordinator.set_current_snapshot(self._snapshot(route_epoch=4))
            setter_done.set()

        setter_thread = threading.Thread(
            target=replace_snapshot,
            name="observe-snapshot-setter",
        )
        setter_thread.start()
        self.assertTrue(observed_lock.attempted.wait(1.0))
        self.assertTrue(observed_lock.blocked.is_set())
        self.assertFalse(observed_lock.acquired.is_set())
        self.assertFalse(setter_done.is_set())
        release.set()
        observe_thread.join(2.0)
        setter_thread.join(2.0)

        self.assertFalse(observe_thread.is_alive())
        self.assertFalse(setter_thread.is_alive())
        self.assertTrue(observed_lock.acquired.is_set())
        self.assertTrue(setter_done.is_set())
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].divert_recommended)  # type: ignore[union-attr]
        self.assertFalse(results[0].confirmed)  # type: ignore[union-attr]
        stale = coordinator.observe(
            self._bound("third.example", at=12.0, binding=binding)
        )
        self.assertFalse(stale.accepted)
        self.assertTrue(stale.stale)

    def test_coordinator_probe_snapshot_check_and_mutation_are_atomic(self) -> None:
        coordinator = RuntimeEgressCoordinator()
        observed_lock = _ObservedRLock("probe-snapshot-setter")
        coordinator._state_lock = observed_lock
        coordinator.set_current_snapshot(self._snapshot())
        binding = self._binding()
        event = self._bound("cp.cloudflare.com", at=10.0, binding=binding)
        opened = coordinator.observe(event)
        circuit = coordinator._circuits[event.circuit_key]
        original_record_probe = circuit.record_probe
        entered = threading.Event()
        release = threading.Event()
        setter_done = threading.Event()
        results: list[object] = []

        def gated_record_probe(*args, **kwargs):
            entered.set()
            release.wait(2.0)
            return original_record_probe(*args, **kwargs)

        circuit.record_probe = gated_record_probe  # type: ignore[method-assign]
        probe_thread = threading.Thread(
            target=lambda: results.append(
                coordinator.record_probe(
                    event,
                    succeeded=False,
                    probe_token=opened.probe_token,
                )
            )
        )
        probe_thread.start()
        self.assertTrue(entered.wait(1.0))

        def replace_snapshot() -> None:
            coordinator.set_current_snapshot(self._snapshot(route_epoch=4))
            setter_done.set()

        setter_thread = threading.Thread(
            target=replace_snapshot,
            name="probe-snapshot-setter",
        )
        setter_thread.start()
        self.assertTrue(observed_lock.attempted.wait(1.0))
        self.assertTrue(observed_lock.blocked.is_set())
        self.assertFalse(observed_lock.acquired.is_set())
        self.assertFalse(setter_done.is_set())
        release.set()
        probe_thread.join(2.0)
        setter_thread.join(2.0)

        self.assertFalse(probe_thread.is_alive())
        self.assertFalse(setter_thread.is_alive())
        self.assertTrue(observed_lock.acquired.is_set())
        self.assertTrue(setter_done.is_set())
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].confirmed)  # type: ignore[union-attr]

    def test_coordinator_rejects_stale_observe_and_probe_against_loaded_snapshot(self) -> None:
        coordinator = RuntimeEgressCoordinator()
        current_snapshot = self._snapshot(route_epoch=4)
        coordinator.set_current_snapshot(current_snapshot)
        stale_binding = self._binding(route_epoch=3)
        stale_event = self._bound(
            "cp.cloudflare.com",
            at=10.0,
            binding=stale_binding,
        )
        stale = coordinator.observe(stale_event)
        self.assertFalse(stale.accepted)
        self.assertTrue(stale.stale)
        self.assertEqual(coordinator.current_snapshots(), ())
        probe = coordinator.record_probe(stale_event, succeeded=False)
        self.assertFalse(probe.accepted)
        self.assertTrue(probe.stale)
        self.assertFalse(
            coordinator.snapshot_ownership_matches(stale_binding)
        )

        current_binding = self._binding(route_epoch=4)
        current_event = self._bound(
            "www.gstatic.com",
            at=11.0,
            binding=current_binding,
        )
        stale_expected = self._snapshot(route_epoch=3)
        expected_mismatch = coordinator.observe(
            current_event,
            current_snapshot=stale_expected,
        )
        self.assertFalse(expected_mismatch.accepted)
        self.assertTrue(expected_mismatch.stale)

    def test_coordinator_global_mutation_lease_is_serial_and_cas_bound(self) -> None:
        coordinator = RuntimeEgressCoordinator()
        loaded = self._snapshot()
        coordinator.set_current_snapshot(loaded)
        binding_a = self._binding(identity="identity-a", proxy_name="leaf-a")
        binding_b = self._binding(identity="identity-b", proxy_name="leaf-b")
        lease_a = coordinator.begin_core_mutation(
            binding_a,
            expected_snapshot=loaded,
        )
        self.assertIsNotNone(lease_a)
        assert lease_a is not None
        self.assertTrue(coordinator.core_mutation_lease_is_current(lease_a))
        self.assertIsNone(coordinator.begin_core_mutation(binding_b))

        coordinator.set_current_snapshot(self._snapshot(route_epoch=4))
        self.assertFalse(coordinator.core_mutation_lease_is_current(lease_a))
        self.assertTrue(coordinator.end_core_mutation(lease_a))
        self.assertIsNone(
            coordinator.begin_core_mutation(
                binding_b,
                expected_snapshot=loaded,
            )
        )

    def test_coordinator_stop_restart_discards_old_work_and_invalidates_lease(self) -> None:
        coordinator = RuntimeEgressCoordinator(queue_capacity=4)
        loaded = self._snapshot()
        coordinator.set_current_snapshot(loaded)
        binding = self._binding()
        event = self._bound("cp.cloudflare.com", at=10.0, binding=binding)
        coordinator.submit(event)
        coordinator.observe(event)
        lease = coordinator.begin_core_mutation(binding)
        self.assertIsNotNone(lease)
        assert lease is not None

        stopped = coordinator.stop()
        self.assertFalse(stopped.running)
        self.assertEqual(stopped.pending_count, 0)
        self.assertEqual(coordinator.current_snapshots(), ())
        self.assertFalse(coordinator.submit(event).accepted)
        self.assertFalse(coordinator.core_mutation_lease_is_current(lease))

        restarted = coordinator.restart(current_snapshot=loaded)
        self.assertTrue(restarted.running)
        self.assertEqual(restarted.pending_count, 0)
        self.assertFalse(restarted.overflow_guard_required)
        # The old holder may only release its invalidated lease.  No new Core
        # mutation is admitted until that release completes.
        self.assertIsNone(coordinator.begin_core_mutation(binding))
        self.assertTrue(coordinator.end_core_mutation(lease))
        replacement = coordinator.begin_core_mutation(binding)
        self.assertIsNotNone(replacement)
        assert replacement is not None
        self.assertTrue(coordinator.end_core_mutation(replacement))
        self.assertTrue(coordinator.submit(event).accepted)

    def test_health_domain_types_are_immutable_and_preserve_raw_self_check_boundary(self) -> None:
        node = self._binding().node
        observation = NodeHealthObservation(
            node=node,
            source="runtime_transport",
            verdict="suspicious",
            failure_domain="leaf",
            incident_id="incident",
            failure_epoch=4,
            quarantine_token="token",
            observed_at_monotonic=10.0,
        )
        transition = NodeHealthTransition(
            node=node,
            previous=None,
            current=observation,
            usable_before=True,
            usable_after=False,
            reason="confirmed_runtime_failure",
        )
        directive = LayerRecoveryDirective(
            node=node,
            action="targeted_recheck",
            incident_id=observation.incident_id,
            quarantine_token=observation.quarantine_token,
        )
        self.assertFalse(transition.usable_after)
        self.assertTrue(directive.require_health_recheck)
        with self.assertRaises(FrozenInstanceError):
            observation.verdict = "alive"  # type: ignore[misc]

    def test_different_destinations_confirm_once(self) -> None:
        circuit = Tier3RuntimeFailureCircuit(corroboration_seconds=5.0)
        first = circuit.observe(self._failure("cp.cloudflare.com", 10.0))
        self.assertTrue(first.start_probe)
        second = circuit.observe(self._failure("www.gstatic.com", 12.0))
        self.assertTrue(second.confirmed)
        self.assertEqual(circuit.snapshot().state, "confirmed")
        serialized = circuit.snapshot().to_dict()
        self.assertNotIn("selected_identity", serialized)
        self.assertTrue(
            serialized["selected_identity_hash"].startswith("hmac:")
        )
        self.assertNotIn("identity-a", repr(serialized))
        repeated = circuit.observe(self._failure("example.net", 12.5))
        self.assertTrue(repeated.confirmed)
        self.assertEqual(circuit.snapshot().failure_count, 2)

    def test_single_destination_probe_controls_confirmation(self) -> None:
        circuit = Tier3RuntimeFailureCircuit()
        event = self._failure("cp.cloudflare.com", 10.0)
        circuit.observe(event)
        cleared = circuit.record_probe(event, succeeded=True)
        self.assertTrue(cleared.cleared)
        self.assertEqual(circuit.snapshot().state, "closed")

        next_event = self._failure(
            "cp.cloudflare.com",
            time.monotonic() + 0.1,
        )
        circuit.observe(next_event)
        confirmed = circuit.record_probe(next_event, succeeded=False)
        self.assertTrue(confirmed.confirmed)
        self.assertEqual(
            circuit.snapshot().confirmation_reason,
            "independent_probe_failed",
        )

    def test_tier3_compat_rejects_out_of_order_observation(self) -> None:
        circuit = Tier3RuntimeFailureCircuit(corroboration_seconds=5.0)
        circuit.observe(self._failure("cp.cloudflare.com", 10.0))
        circuit.observe(self._failure("cp.cloudflare.com", 12.0))

        rejected = circuit.observe(self._failure("www.gstatic.com", 11.0))

        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.reason, "out_of_order_observation")
        snapshot = circuit.snapshot()
        self.assertEqual(snapshot.state, "suspect")
        self.assertEqual(snapshot.last_observed_monotonic, 12.0)

    def test_stale_probe_cannot_confirm_new_leaf(self) -> None:
        circuit = Tier3RuntimeFailureCircuit()
        old = self._failure("cp.cloudflare.com", 10.0)
        circuit.observe(old)
        circuit.observe(
            Tier3RuntimeFailureEvent(
                core_generation=8,
                semantic_digest="next",
                selected_leaf="leaf-b",
                selected_identity="identity-b",
                destination="example.net",
                error_type="timeout",
                observed_at_monotonic=11.0,
            )
        )
        stale = circuit.record_probe(old, succeeded=False)
        self.assertFalse(stale.accepted)
        self.assertEqual(circuit.snapshot().selected_leaf, "leaf-b")

    def test_policy_defaults_and_bounds(self) -> None:
        self.assertEqual(
            egress_recovery_policy({}),
            EgressRecoveryPolicy(
                enabled=True,
                max_rotations=2,
                exclusion_ttl_seconds=120.0,
                retry_source_refresh=True,
            ),
        )
        self.assertEqual(
            egress_recovery_policy(
                {
                    "client": {
                        "egress_recovery": {
                            "enabled": "false",
                            "max_rotations": 999,
                            "exclusion_ttl_seconds": 1,
                            "retry_source_refresh": "false",
                        }
                    }
                }
            ),
            EgressRecoveryPolicy(
                enabled=False,
                max_rotations=4,
                exclusion_ttl_seconds=30.0,
                retry_source_refresh=False,
            ),
        )

    def test_tls_dns_connection_and_timeout_are_transport_failures(self) -> None:
        for error in (
            "SSLEOFError: EOF occurred in violation of protocol",
            "getaddrinfo failed: DNS name resolution failed",
            "Connection reset by peer",
            "request timed out",
        ):
            with self.subTest(error=error):
                self.assertTrue(identity_attempt_error_is_transport(error))
                self.assertTrue(
                    all_identity_attempts_transport_failed(unavailable_payload(error))
                )

    def test_http_4xx_and_content_errors_never_count_as_transport(self) -> None:
        for error in (
            "HTTP 403 Forbidden",
            "HTTP status 429",
            "identity response contained invalid JSON",
            "response did not contain a public IP",
        ):
            with self.subTest(error=error):
                self.assertFalse(identity_attempt_error_is_transport(error))
                self.assertFalse(
                    all_identity_attempts_transport_failed(unavailable_payload(error))
                )

    def test_rotation_requires_a_stable_confirmed_route(self) -> None:
        policy = EgressRecoveryPolicy()
        self.assertTrue(
            should_rotate_after_identity_failure(
                unavailable_payload("TLS handshake unexpected EOF"),
                policy,
            )
        )
        self.assertFalse(
            should_rotate_after_identity_failure(
                unavailable_payload("TLS handshake unexpected EOF", stable=False),
                policy,
            )
        )
        unconfirmed = unavailable_payload("TLS handshake unexpected EOF")
        unconfirmed["route"]["confirmed_before"] = False
        self.assertFalse(should_rotate_after_identity_failure(unconfirmed, policy))


if __name__ == "__main__":
    unittest.main()
