from __future__ import annotations

from runtime_facility_state import (
    RuntimeFacilityImpact,
    RuntimeListenerKind,
    RuntimeLocalFacilityState,
    classify_runtime_listener_fault,
)


def _fault(line: str, *, mode: str = "system_proxy", observed: float = 10.0):
    return classify_runtime_listener_fault(
        line,
        runtime_generation=7,
        core_generation=11,
        observed_at_monotonic=observed,
        network_mode=mode,
        mixed_endpoint="127.0.0.1:7890",
        controller_endpoint="127.0.0.1:9090",
        dns_endpoint="127.0.0.1:1053",
        source_endpoints=("127.0.0.1:31451", "127.0.0.1:31452"),
    )


def test_dns_bind_failure_is_capture_only_in_system_proxy_mode() -> None:
    fault = _fault(
        "listen tcp 127.0.0.1:1053: bind: An attempt was made to access a socket"
    )

    assert fault is not None
    assert fault.kind is RuntimeListenerKind.DNS
    assert fault.impact is RuntimeFacilityImpact.CAPTURE_ONLY
    assert fault.blocks_capture is True
    assert fault.blocks_data_plane is False
    assert fault.blocks_node_attribution is False


def test_dns_bind_failure_blocks_tun_data_plane_but_source_listener_does_not() -> None:
    dns = _fault(
        "listen udp 127.0.0.1:1053: bind: permission denied",
        mode="tun",
    )
    source = _fault(
        "listener 127.0.0.1:31451 bind: address already in use",
        mode="tun",
    )

    assert dns is not None and dns.impact is RuntimeFacilityImpact.DATA_PLANE
    assert source is not None
    assert source.kind is RuntimeListenerKind.SOURCE_PRIVATE
    assert source.impact is RuntimeFacilityImpact.SOURCE_ONLY
    assert source.blocks_node_attribution is False


def test_wildcard_ipv4_and_ipv6_bind_lines_are_classified() -> None:
    ipv4 = classify_runtime_listener_fault(
        "listen tcp 0.0.0.0:7890: bind: address already in use",
        runtime_generation=7,
        core_generation=11,
        observed_at_monotonic=10.0,
        network_mode="system_proxy",
        mixed_endpoint="0.0.0.0:7890",
        controller_endpoint="127.0.0.1:9090",
        dns_endpoint="127.0.0.1:1053",
    )
    ipv6 = classify_runtime_listener_fault(
        "listen udp [::]:1053: bind: permission denied",
        runtime_generation=7,
        core_generation=11,
        observed_at_monotonic=10.0,
        network_mode="system_proxy",
        mixed_endpoint="127.0.0.1:7890",
        controller_endpoint="127.0.0.1:9090",
        dns_endpoint="[::]:1053",
    )

    assert ipv4 is not None and ipv4.kind is RuntimeListenerKind.MIXED
    assert ipv6 is not None and ipv6.kind is RuntimeListenerKind.DNS


def test_mixed_fault_requires_final_exit_proof_but_controller_only_blocks_attribution() -> None:
    state = RuntimeLocalFacilityState()
    mixed = _fault("listener 127.0.0.1:7890 bind: address already in use")
    controller = _fault(
        "listen tcp 127.0.0.1:9090: bind: only one usage",
        observed=12.0,
    )
    assert mixed is not None and controller is not None
    assert state.observe(mixed) is True
    assert state.observe(controller) is True
    assert state.snapshot().data_plane_blocked is True
    assert controller.impact is RuntimeFacilityImpact.ATTRIBUTION_ONLY

    assert state.record_probe(
        mixed.token,
        now_monotonic=11.0,
        endpoint_ready=True,
        final_exit_verified=False,
    ) is False
    assert state.record_probe(
        mixed.token,
        now_monotonic=13.0,
        endpoint_ready=True,
        final_exit_verified=True,
    ) is True
    assert state.snapshot().data_plane_blocked is False
    assert state.snapshot().node_attribution_blocked is True
    assert state.record_probe(
        controller.token,
        now_monotonic=14.0,
        endpoint_ready=True,
    ) is True
    assert state.snapshot().data_plane_blocked is False


def test_repeated_stdout_does_not_extend_first_retry_or_duplicate_incident() -> None:
    state = RuntimeLocalFacilityState()
    first = _fault("dns server listen 127.0.0.1:1053 bind: permission denied")
    repeated = _fault(
        "dns server listen 127.0.0.1:1053 bind: permission denied",
        observed=99.0,
    )
    assert first is not None and repeated is not None
    assert first.token == repeated.token
    assert state.observe(first) is True
    assert state.observe(repeated) is False
    assert state.snapshot().faults[0].next_probe_at_monotonic == 11.0


def test_failed_rechecks_back_off_and_causal_wake_is_bounded() -> None:
    state = RuntimeLocalFacilityState()
    fault = _fault("listener 127.0.0.1:7890 bind: permission denied")
    assert fault is not None
    state.observe(fault)
    assert state.due(10.9) == ()
    assert state.due(11.0) == (fault,)

    assert state.record_probe(
        fault.token,
        now_monotonic=11.0,
        endpoint_ready=False,
    ) is False
    assert state.due(12.9) == ()
    assert len(state.due(13.0)) == 1
    assert state.wake(now_monotonic=12.0, core_generation=11) == 1
    assert len(state.due(12.0)) == 1
    assert state.wake(now_monotonic=12.0, core_generation=11) == 0


def test_old_generation_callback_cannot_clear_new_fault() -> None:
    state = RuntimeLocalFacilityState()
    old = _fault("listener 127.0.0.1:7890 bind: permission denied")
    assert old is not None
    state.observe(old)
    assert state.discard_stale(runtime_generation=7, core_generation=12) == 1
    assert state.record_probe(
        old.token,
        now_monotonic=20.0,
        endpoint_ready=True,
        final_exit_verified=True,
    ) is False


def test_non_bind_transport_error_is_not_a_listener_fault() -> None:
    assert (
        _fault(
            "dial GENERAL (match Match/) example.com:443 error: context deadline exceeded"
        )
        is None
    )
