from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from desktop_runtime import DesktopRuntime, _RuntimeProbeBudgetUnavailable
from runtime_manual_validation import (
    ManualDataPlaneProbeResult,
    ManualProbeFailureKind,
    RuntimeManualValidation,
)
from runtime_probe_broker import (
    ProbeDisposition,
    ProbeIndeterminateReason,
    ProbePriority,
    RuntimeProbeBroker,
)
from tests import test_exit_quality_runtime as exit_runtime_tests


def _probe_runtime() -> DesktopRuntime:
    runtime = object.__new__(DesktopRuntime)
    runtime.config = {"client": {"mihomo_mixed_port": 7890}}
    runtime.stop_event = threading.Event()
    runtime._runtime_probe_broker = RuntimeProbeBroker(max_active_sockets=2)
    runtime._default_probe_urls = lambda: [
        "https://one.test/generate_204",
        "https://two.test/generate_204",
    ]
    runtime._register_runtime_internal_probe = lambda *_args, **_kwargs: None
    return runtime


def test_manual_pair_waits_for_path_watcher_without_losing_transport_budget():
    runtime = _probe_runtime()
    admission = runtime._runtime_probe_broker.acquire(
        ProbePriority.BACKGROUND, timeout_seconds=0.1
    )
    assert admission.ticket is not None
    release = threading.Timer(0.35, admission.ticket.release)
    calls = []

    def healthy_probe(contract_id, url, timeout_seconds, context):
        context.register_abort_callback(lambda: None)
        assert context.mark_socket_started()
        calls.append((contract_id, timeout_seconds))
        return {"status": 204}

    session = RuntimeManualValidation(
        total_timeout_seconds=2.0, per_probe_timeout_seconds=0.15
    )
    release.start()
    try:
        results = runtime._run_manual_exit_quality_contracts(
            session, probe=healthy_probe, phase_timeout_seconds=0.15
        )
    finally:
        release.join(1.0)
        admission.ticket.release()

    assert len(calls) == 2
    assert all(result.is_https_204_success(session.budget) for result in results)
    assert all(0 < timeout <= 0.15 for _, timeout in calls)
    assert not runtime._runtime_probe_broker.poisoned


def test_pair_execution_timeout_starts_after_queue_and_aborts_both_sockets():
    broker = RuntimeProbeBroker(max_active_sockets=2)
    admission = broker.acquire(ProbePriority.BACKGROUND, timeout_seconds=0.1)
    assert admission.ticket is not None
    release = threading.Timer(0.2, admission.ticket.release)
    started = []
    deadlines = []

    def stalled_probe(context):
        aborted = threading.Event()
        context.register_abort_callback(aborted.set)
        assert context.mark_socket_started()
        started.append(time.monotonic())
        deadlines.append(context.execution_deadline_monotonic)
        assert aborted.wait(1.0)
        raise TimeoutError("test socket cancelled at execution deadline")

    begin = time.monotonic()
    release.start()
    try:
        results = broker.run_pair(
            ProbePriority.RUNTIME_MANUAL,
            stalled_probe,
            stalled_probe,
            queue_timeout_seconds=0.8,
            socket_start_timeout_seconds=0.1,
            execution_deadline_monotonic=begin + 2.0,
            execution_timeout_seconds=0.15,
        )
    finally:
        release.join(1.0)
        admission.ticket.release()

    assert len(started) == 2
    assert min(started) - begin >= 0.15
    assert all(0 < deadline - start <= 0.151 for start, deadline in zip(started, deadlines))
    assert all(
        result.indeterminate_reason is ProbeIndeterminateReason.EXECUTION_TIMEOUT
        for result in results
    )
    assert not broker.poisoned
    assert broker.snapshot().active_socket_slots == 0


def test_pair_relative_execution_budget_never_extends_outer_deadline():
    broker = RuntimeProbeBroker(max_active_sockets=2)
    deadline = time.monotonic() + 0.15
    deadlines = []

    def stalled_probe(context):
        aborted = threading.Event()
        context.register_abort_callback(aborted.set)
        assert context.mark_socket_started()
        deadlines.append(context.execution_deadline_monotonic)
        assert aborted.wait(1.0)

    results = broker.run_pair(
        ProbePriority.RUNTIME_MANUAL,
        stalled_probe,
        stalled_probe,
        execution_deadline_monotonic=deadline,
        execution_timeout_seconds=2.0,
    )

    assert deadlines == [deadline, deadline]
    assert all(result.disposition is ProbeDisposition.INDETERMINATE for result in results)
    assert not broker.poisoned


def test_pair_rejects_invalid_execution_timeout_before_reserving_slots():
    broker = RuntimeProbeBroker(max_active_sockets=2)
    with pytest.raises(ValueError):
        broker.run_pair(
            ProbePriority.RUNTIME_MANUAL,
            lambda _context: None,
            lambda _context: None,
            execution_timeout_seconds=-1,
        )
    assert broker.snapshot().active_socket_slots == 0


def test_current_contracts_use_the_two_selected_https_destinations():
    runtime = _probe_runtime()
    runtime._default_probe_urls = lambda: [
        "http://[invalid",
        "http://plain.test/generate_204",
        "https://one.test/generate_204",
        "https://one.test/duplicate",
        "https://two.test/generate_204",
    ]
    requested = []

    def transport(**kwargs):
        requested.append(kwargs["probe_url"])
        return 204, b""

    runtime._abortable_https_204_via_http_proxy = transport
    session = RuntimeManualValidation(total_timeout_seconds=2.0, per_probe_timeout_seconds=1.0)

    results = runtime._manual_current_exit_quality_contracts(session)

    assert sorted(requested) == [
        "https://one.test/generate_204",
        "https://two.test/generate_204",
    ]
    assert all(result.is_https_204_success(session.budget) for result in results)


def test_host_proof_selects_https_even_when_health_check_prefers_http():
    runtime = _probe_runtime()
    runtime._default_probe_urls = lambda: [
        "http://[invalid", "http://plain.test/generate_204", "https://one.test/generate_204"
    ]
    runtime.network_mode = "system_proxy"
    runtime.system_proxy = SimpleNamespace(
        enabled=True,
        host="127.0.0.1",
        port=7890,
        is_target_active=lambda: True,
        proxy_server=lambda: "127.0.0.1:7890",
        backend=SimpleNamespace(
            read=lambda: SimpleNamespace(enabled=True, server="127.0.0.1:7890")
        ),
    )
    requested = []

    def transport(**kwargs):
        requested.append(kwargs["probe_url"])
        return 204, b""

    runtime._abortable_https_204_via_http_proxy = transport

    result = runtime._verify_system_proxy_host_capture(
        deadline_monotonic=time.monotonic() + 1.0,
        priority=ProbePriority.RUNTIME_MANUAL,
    )

    assert result["verified"]
    assert requested == ["https://one.test/generate_204"]


def _manual_runtime():
    fixture = exit_runtime_tests.ExitQualityRuntimeTests()
    runtime, *_rest = fixture._gateway_manual_runtime()
    runtime._runtime_egress_recovery_profile = "early_single_coordinator"
    runtime._promote_manual_egress_after_verified_probe = lambda **_kwargs: False
    identity_requests = []
    runtime._queue_final_exit_identity_diagnostic = (
        lambda **kwargs: identity_requests.append(kwargs)
    )
    return runtime, identity_requests


def test_unexecuted_pair_is_indeterminate_without_identity_or_node_recovery():
    runtime, identity_requests = _manual_runtime()
    runtime._manual_current_exit_quality_contracts = lambda _session: tuple(
        ManualDataPlaneProbeResult.transport_failure(
            f"neutral-{index}", ManualProbeFailureKind.FACILITY
        )
        for index in range(2)
    )
    runtime._runtime_ordered_alternative_refs = lambda *_args, **_kwargs: pytest.fail(
        "facility outcome must not rotate a healthy node"
    )

    result = runtime.check_exit_quality(proxy_url=None, request_id="queue-busy")

    assert result["state"] == "data_plane_indeterminate"
    assert result["freshness"]["reason"] == "manual_probe_not_completed"
    assert result["diagnostics"]["neutral_connectivity"]["state"] == "indeterminate"
    assert identity_requests == []


def test_success_from_stale_execution_cannot_start_identity_for_same_route():
    runtime, identity_requests = _manual_runtime()

    def successful_but_superseded(_session):
        runtime._runtime_network_context_epoch += 1
        return tuple(
            ManualDataPlaneProbeResult.https_204(f"neutral-{index}")
            for index in range(2)
        )

    runtime._manual_current_exit_quality_contracts = successful_but_superseded

    result = runtime.check_exit_quality(proxy_url=None, request_id="stale-execution")

    assert result["state"] == "data_plane_indeterminate"
    assert result["freshness"]["reason"] == "manual_execution_binding_unverified"
    assert not result["diagnostics"]["identity_pending"]
    assert identity_requests == []


def test_host_probe_budget_unavailable_does_not_report_broken_data_plane():
    runtime, identity_requests = _manual_runtime()
    runtime._manual_current_exit_quality_contracts = lambda _session: tuple(
        ManualDataPlaneProbeResult.https_204(f"neutral-{index}")
        for index in range(2)
    )
    runtime._system_proxy_capture_proof_required = lambda: True
    runtime.system_proxy = SimpleNamespace(is_target_active=lambda: True)

    def busy_host_probe(**_kwargs):
        raise _RuntimeProbeBudgetUnavailable("runtime probe queue is busy")

    runtime._verify_system_proxy_host_capture = busy_host_probe

    result = runtime.check_exit_quality(proxy_url=None, request_id="host-queue-busy")

    assert result["state"] == "data_plane_indeterminate"
    assert result["freshness"]["reason"] == "system_proxy_host_probe_not_completed"
    assert result["diagnostics"]["neutral_connectivity"]["success_count"] == 2
    assert identity_requests == []
