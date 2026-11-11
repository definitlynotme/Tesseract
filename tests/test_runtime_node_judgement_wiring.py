"""Desktop wiring of the node-judgement P0 items (design v3 §12 P0-2..P0-6).

Every item is exercised with its ``client.egress_recovery.node_judgement``
flag on and off.  The pure modules have their own suites; these tests pin the
integration: which controller calls happen, with which contract and timeout,
and what stays byte-for-byte unchanged when a flag is off.
"""

from __future__ import annotations

import copy
import logging
import socket
import threading
import time
from dataclasses import replace as _dataclass_replace
from pathlib import Path
from typing import Any

import pytest

from desktop_runtime import (
    DesktopRuntime,
    StartupRouteIntent,
    _ManualRouteContractPlan,
    _NodeJudgementRuntimeState,
    _RuntimeAbortHandle,
    _RuntimeProbeBudgetUnavailable,
    _RuntimeRecoveryArm,
)
from mihomo_api import (
    MihomoDelayStatusMismatch,
    MihomoDelayTimeout,
    MihomoDelayTransportFailed,
)
from runtime_egress_recovery import (
    CoreLoadedGroupState,
    CoreLoadedRouteSnapshot,
    RuntimeEgressCoordinator,
    RuntimeFailureCircuitDecision,
    RuntimeNodeRef,
    RuntimeRouteBinding,
)
from runtime_manual_validation import (
    MANUAL_ROUTE_CONTRACT_ID,
    ManualDataPlaneProbeResult,
    ManualProbeFailureKind,
    RuntimeManualValidation,
)
from runtime_node_judge import NodeJudgementPolicy
from runtime_post_dial import PostDialTokenizer
from runtime_probe_broker import ProbePriority, RuntimeProbeBroker
from runtime_route_audit import inspect_runtime_route
from system_proxy import ProxySettings, SystemProxyManager
from tests import test_exit_quality_runtime as exit_runtime_tests
from tests.test_desktop_runtime_recovery import FakeCoreRunner, MemoryProxyBackend


CONTRACT_URL = "https://www.google.com/generate_204"
NEUTRAL_URLS = [
    "https://cp.cloudflare.com/generate_204",
    "https://www.gstatic.com/generate_204",
]


def _judgement_config(**flags: Any) -> dict[str, Any]:
    return {"client": {"egress_recovery": {"node_judgement": dict(flags)}}}


def _bare_runtime(config: dict[str, Any] | None = None) -> DesktopRuntime:
    runtime = object.__new__(DesktopRuntime)
    runtime.config = config if config is not None else {}
    runtime.stop_event = threading.Event()
    runtime.logger = logging.getLogger("test.node-judgement-wiring")
    runtime._core_generation = 7
    return runtime


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self, prefix: str) -> list[str]:
        return [
            record.getMessage()
            for record in self.records
            if record.getMessage().startswith(prefix)
        ]


def _wait_for(predicate: Any, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


# ---------------------------------------------------------------------------
# P0-6: policy


def test_policy_is_read_from_the_live_config_so_reloads_apply_at_once() -> None:
    runtime = _bare_runtime({})
    policy = runtime._node_judgement_policy()
    assert policy == NodeJudgementPolicy()
    assert policy.group_round_native_timeout is False
    assert policy.release_stale_fallback_pins is False
    assert policy.manual_check_uses_route_contract is True
    assert policy.evidence_log is True

    runtime.config = _judgement_config(
        group_round_native_timeout=True,
        release_stale_fallback_pins="true",
        manual_check_uses_route_contract=False,
    )
    policy = runtime._node_judgement_policy()
    assert policy.group_round_native_timeout is True
    assert policy.release_stale_fallback_pins is True
    assert policy.manual_check_uses_route_contract is False

    runtime.config = _judgement_config(enabled=False)
    assert runtime._node_judgement_policy() == NodeJudgementPolicy.disabled()


def test_rearm_on_stale_route_round_trips_through_the_local_config_merge(tmp_path: Path) -> None:
    from main import load_config

    import yaml

    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    base = tmp_path / "config.yaml"
    base.write_bytes(example.read_bytes())
    # The example documents the key commented out: it parses to the default.
    runtime = _bare_runtime(load_config(str(base), local_path=None))
    assert runtime._node_judgement_policy().rearm_on_stale_route is True  # 10-28 default
    assert runtime._node_judgement_policy() == NodeJudgementPolicy()

    (tmp_path / "config.local.yaml").write_text(
        yaml.safe_dump(
            {
                "client": {
                    "egress_recovery": {
                        "node_judgement": {
                            "post_dial_judge": "act",
                            "rearm_on_stale_route": True,
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    runtime.config = load_config(str(base))
    policy = runtime._node_judgement_policy()
    assert policy.rearm_on_stale_route is True
    assert policy.acts is True
    assert policy.durable_conviction is False


def test_policy_tolerates_embedders_without_config() -> None:
    runtime = object.__new__(DesktopRuntime)
    assert runtime._node_judgement_policy() == NodeJudgementPolicy()


# ---------------------------------------------------------------------------
# P0-2: group-round hygiene


class _GroupDelayClient:
    def __init__(self, *, legacy: bool = False) -> None:
        self.legacy = legacy
        self.calls: list[dict[str, Any]] = []

    def group_delay(
        self,
        group: str,
        *,
        url: str,
        timeout_ms: int = 5000,
        request_timeout: float | None = None,
        connection_abort_sink: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if self.legacy and (connection_abort_sink is not None or kwargs):
            raise TypeError("legacy group_delay signature")
        self.calls.append(
            {
                "group": group,
                "url": url,
                "timeout_ms": timeout_ms,
                "request_timeout": request_timeout,
                **kwargs,
            }
        )
        return {"member": 12}


def _group_delay_runtime(
    client: _GroupDelayClient,
) -> tuple[DesktopRuntime, list[dict[str, Any]]]:
    runtime = _bare_runtime()
    brokered: list[dict[str, Any]] = []

    def run_brokered(run: Any, **kwargs: Any) -> Any:
        brokered.append({"issued_at": time.monotonic(), **kwargs})
        return run(None)

    runtime._run_brokered_runtime_probe = run_brokered
    runtime._register_runtime_internal_probe = lambda *_args, **_kwargs: None
    runtime._mark_runtime_probe_socket_started = lambda *_args, **_kwargs: None
    runtime._require_api_client = lambda: client
    return runtime, brokered


def test_test_group_delay_sends_expected_only_when_given() -> None:
    client = _GroupDelayClient()
    runtime, brokered = _group_delay_runtime(client)

    runtime.test_group_delay("TIER3-AUTO", url=CONTRACT_URL, timeout_ms=5000)
    runtime.test_group_delay(
        "TIER3-AUTO",
        url=CONTRACT_URL,
        timeout_ms=5000,
        expected_status="204",
    )

    assert "expected_status" not in client.calls[0]
    assert client.calls[1]["expected_status"] == "204"
    for call, admission in zip(client.calls, brokered):
        assert call["timeout_ms"] == 5000
        # Mihomo finishes the members at ``timeout`` and only then replies:
        # HTTP and broker deadlines keep the framing slack after it.
        assert call["request_timeout"] == pytest.approx(5.25)
        assert (
            admission["execution_deadline_monotonic"] - admission["issued_at"]
            >= 5.25 - 0.05
        )


def test_test_group_delay_legacy_client_fallback_drops_new_arguments() -> None:
    client = _GroupDelayClient(legacy=True)
    runtime, _brokered = _group_delay_runtime(client)

    runtime.test_group_delay(
        "TIER3-AUTO",
        url=CONTRACT_URL,
        timeout_ms=5000,
        expected_status="204",
    )

    assert client.calls == [
        {
            "group": "TIER3-AUTO",
            "url": CONTRACT_URL,
            "timeout_ms": 5000,
            "request_timeout": None,
        }
    ]


def test_native_round_deadline_also_covers_its_admission_wait() -> None:
    client = _GroupDelayClient()
    runtime, brokered = _group_delay_runtime(client)

    runtime.test_group_delay("TIER3-AUTO", url=CONTRACT_URL, timeout_ms=5000)
    runtime.test_group_delay(
        "TIER3-AUTO",
        url=CONTRACT_URL,
        timeout_ms=5000,
        expected_status="204",
        native_admission_seconds=1.5,
    )
    runtime.test_group_delay(
        "TIER1-AUTO",
        url=CONTRACT_URL,
        timeout_ms=800,
        expected_status="204",
        native_admission_seconds=0.2,
    )

    legacy, native, short_queue = brokered
    # Legacy callers: today's queue bound and deadline, measured at issue.
    assert legacy["queue_timeout_seconds"] == pytest.approx(1.5)
    assert legacy["execution_deadline_monotonic"] - legacy["issued_at"] == pytest.approx(
        5.25, abs=0.05
    )
    # Native rounds: the deadline is issue + admission wait + full window.
    assert native["queue_timeout_seconds"] == pytest.approx(1.5)
    assert native["execution_deadline_monotonic"] - native["issued_at"] == pytest.approx(
        5.25 + 1.5 + 0.05, abs=0.05
    )
    assert short_queue["queue_timeout_seconds"] == pytest.approx(0.2)
    assert short_queue[
        "execution_deadline_monotonic"
    ] - short_queue["issued_at"] == pytest.approx(1.05 + 0.2 + 0.05, abs=0.05)
    # The controller request itself is unchanged: native timeout + slack.
    assert [call["request_timeout"] for call in client.calls] == [
        pytest.approx(5.25),
        pytest.approx(5.25),
        pytest.approx(1.05),
    ]


class _SlowGroupClient:
    """Replies after ``timeout_ms`` like Mihomo's group round, unless aborted."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def group_delay(
        self,
        group: str,
        *,
        url: str,
        timeout_ms: int = 5000,
        request_timeout: float | None = None,
        connection_abort_sink: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        aborted = threading.Event()
        if connection_abort_sink is not None:
            connection_abort_sink(aborted.set)
        call = {"group": group, "started": time.monotonic(), "aborted": None}
        self.calls.append(call)
        if aborted.wait(timeout_ms / 1000.0):
            call["aborted"] = time.monotonic() - call["started"]
            raise ConnectionAbortedError("controller socket aborted mid-round")
        return {"member": 12}


def _busy_broker_runtime(
    busy_seconds: float,
) -> tuple[DesktopRuntime, _SlowGroupClient, list[threading.Thread]]:
    runtime = _bare_runtime()
    broker = RuntimeProbeBroker(max_active_sockets=2)
    runtime._runtime_probe_broker = broker
    runtime._register_runtime_internal_probe = lambda *_args, **_kwargs: None
    client = _SlowGroupClient()
    runtime._require_api_client = lambda: client
    started = threading.Barrier(3)

    def background(context: Any) -> None:
        context.register_abort_callback(lambda: None)
        context.mark_socket_started()
        started.wait(1.0)
        time.sleep(busy_seconds)

    holders = [
        threading.Thread(
            target=broker.run,
            args=(ProbePriority.BACKGROUND, background),
            kwargs={"queue_timeout_seconds": 1.0},
            daemon=True,
        )
        for _ in range(2)
    ]
    for holder in holders:
        holder.start()
    started.wait(1.0)
    return runtime, client, holders


def test_native_round_queued_behind_busy_slots_still_gets_its_full_window() -> None:
    runtime, client, holders = _busy_broker_runtime(0.4)

    result = runtime._run_native_group_round(
        "TIER1-AUTO",
        probe_url=CONTRACT_URL,
        expected_status="204",
        timeout_ms=600,
    )
    for holder in holders:
        holder.join(2.0)

    assert result == {"member": 12}
    assert len(client.calls) == 1
    assert client.calls[0]["aborted"] is None


def test_legacy_round_queued_behind_busy_slots_is_still_cut_as_today() -> None:
    # Flag off keeps today's deadline, measured before the queue wait: the
    # same queued round is aborted before Mihomo would have replied.
    runtime, client, holders = _busy_broker_runtime(0.4)

    with pytest.raises(_RuntimeProbeBudgetUnavailable):
        runtime.test_group_delay("TIER1-AUTO", url=CONTRACT_URL, timeout_ms=600)
    for holder in holders:
        holder.join(2.0)

    assert len(client.calls) == 1
    assert client.calls[0]["aborted"] is not None
    assert client.calls[0]["aborted"] < 0.6


def test_native_round_without_its_full_window_is_skipped_not_issued() -> None:
    client = _GroupDelayClient()
    runtime = _bare_runtime()
    runtime._register_runtime_internal_probe = lambda *_args, **_kwargs: None
    runtime._require_api_client = lambda: client

    class _LateContext:
        # Admitted with less than the native window left.
        execution_deadline_monotonic = time.monotonic() + 5.0

    def run_brokered(run: Any, **_kwargs: Any) -> Any:
        return run(_LateContext())

    runtime._run_brokered_runtime_probe = run_brokered
    logs = _ListHandler()
    runtime.logger.addHandler(logs)
    runtime.logger.setLevel(logging.DEBUG)
    try:
        with pytest.raises(_RuntimeProbeBudgetUnavailable):
            runtime.test_group_delay(
                "TIER3-AUTO",
                url=CONTRACT_URL,
                timeout_ms=5000,
                expected_status="204",
                native_admission_seconds=1.5,
            )
    finally:
        runtime.logger.removeHandler(logs)
        runtime.logger.setLevel(logging.NOTSET)

    assert client.calls == []
    assert logs.messages("Native group round skipped") == [
        "Native group round skipped (group=TIER3-AUTO reason=group_round_skipped_queue)"
    ]


def test_native_timeout_is_5000_for_provider_groups_and_the_ceiling_inline() -> None:
    runtime = _bare_runtime({"testing": {"primary_gateway_max_latency_ms": 1200}})
    assert runtime._runtime_group_native_timeout_ms("TIER3-AUTO") == 5000
    assert runtime._runtime_group_native_timeout_ms("PROXY-FALLBACK") == 5000
    assert runtime._runtime_group_native_timeout_ms("TIER2-AUTO") == 1200
    assert runtime._runtime_group_native_timeout_ms("TIER1-node-a-AUTO") == 1200


def _health_snapshot(group: str = "TIER3-AUTO") -> CoreLoadedRouteSnapshot:
    return CoreLoadedRouteSnapshot(
        process_pid=4312,
        process_generation=8,
        core_generation=7,
        semantic_digest="startup-graph",
        route_epoch=5,
        group_states=(
            CoreLoadedGroupState(
                name=group,
                type_name="Fallback",
                members=("leaf-a", "leaf-b"),
                now="leaf-a",
                test_url=CONTRACT_URL,
                expected_status="204",
            ),
        ),
    )


def _startup_runtime(
    *,
    flag: bool | None,
    group: str = "TIER3-AUTO",
) -> tuple[DesktopRuntime, list[tuple[str, str, int, dict[str, Any]]]]:
    config = {} if flag is None else _judgement_config(group_round_native_timeout=flag)
    runtime = _bare_runtime(config)
    runtime._runtime_traffic_capture_wanted = lambda: False
    runtime._startup_native_fallback_health_group = lambda *_args, **_kwargs: group
    calls: list[tuple[str, str, int, dict[str, Any]]] = []

    def group_delay(g: str, *, url: str, timeout_ms: int, **kwargs: Any) -> dict[str, Any]:
        calls.append((g, url, int(timeout_ms), dict(kwargs)))
        return {"leaf-a": 10}

    runtime.test_group_delay = group_delay
    return runtime, calls


_INTENT = object()
# Every native round sends the contract status and starts its window at
# broker admission, after at most the usual 1.5 s of queueing.
_NATIVE_ROUND_KWARGS = {"expected_status": "204", "native_admission_seconds": 1.5}


@pytest.mark.parametrize("flag", [None, False])
def test_startup_round_flag_off_keeps_the_budget_shortened_round(flag: bool | None) -> None:
    runtime, calls = _startup_runtime(flag=flag)

    source = runtime._trigger_startup_native_fallback_health(
        _health_snapshot(),
        _INTENT,  # type: ignore[arg-type]
        deadline_monotonic=time.monotonic() + 4.0,
    )

    assert source == "group_healthcheck"
    assert len(calls) == 1
    group, url, timeout_ms, kwargs = calls[0]
    assert (group, url) == ("TIER3-AUTO", CONTRACT_URL)
    assert 2500 <= timeout_ms < 3100  # today: remaining minus the reserve
    assert kwargs == {}
    assert runtime.__dict__.get("_node_judgement_runtime_state") is None


def test_startup_round_flag_on_uses_the_native_contract_when_it_fits() -> None:
    runtime, calls = _startup_runtime(flag=True)

    source = runtime._trigger_startup_native_fallback_health(
        _health_snapshot(),
        _INTENT,  # type: ignore[arg-type]
        deadline_monotonic=time.monotonic() + 10.0,
    )

    assert source == "group_healthcheck"
    assert calls == [("TIER3-AUTO", CONTRACT_URL, 5000, _NATIVE_ROUND_KWARGS)]


def test_startup_round_flag_on_defers_one_round_until_ready_instead_of_shortening() -> None:
    runtime, calls = _startup_runtime(flag=True)

    source = runtime._trigger_startup_native_fallback_health(
        _health_snapshot(),
        _INTENT,  # type: ignore[arg-type]
        deadline_monotonic=time.monotonic() + 4.0,
    )

    assert source == "group_healthcheck_deferred"
    assert calls == []
    state = runtime._node_judgement_state()
    assert state.deferred_group_round is not None
    assert state.deferred_group_round["timeout_ms"] == 5000

    assert runtime._start_deferred_native_group_round() is True
    assert _wait_for(lambda: len(calls) == 1)
    assert calls == [("TIER3-AUTO", CONTRACT_URL, 5000, _NATIVE_ROUND_KWARGS)]
    # One-shot: nothing left to run, no loop.
    assert runtime._start_deferred_native_group_round() is False
    time.sleep(0.05)
    assert len(calls) == 1


def test_startup_round_queues_only_as_long_as_the_round_still_fits() -> None:
    runtime, calls = _startup_runtime(flag=True)

    source = runtime._trigger_startup_native_fallback_health(
        _health_snapshot(),
        _INTENT,  # type: ignore[arg-type]
        deadline_monotonic=time.monotonic() + 6.5,
    )

    assert source == "group_healthcheck"
    assert len(calls) == 1
    _group, _url, timeout_ms, kwargs = calls[0]
    assert timeout_ms == 5000
    # 6.5 s - 1.0 s reserve - 5.25 s window - 0.05 s dispatch.
    assert kwargs["native_admission_seconds"] == pytest.approx(0.2, abs=0.05)


@pytest.mark.parametrize(
    ("reason", "deferred"),
    [
        ("queue_timeout", True),
        ("socket_not_started", True),
        ("execution_timeout", False),
    ],
)
def test_startup_round_that_never_left_the_queue_is_deferred_until_ready(
    reason: str,
    deferred: bool,
) -> None:
    runtime, _calls = _startup_runtime(flag=True)
    attempts: list[str] = []

    def busy(group: str, **_kwargs: Any) -> dict[str, Any]:
        attempts.append(group)
        raise _RuntimeProbeBudgetUnavailable(
            "runtime data-plane probe is indeterminate",
            probe_indeterminate_reason=reason,
        )

    runtime.test_group_delay = busy

    source = runtime._trigger_startup_native_fallback_health(
        _health_snapshot(),
        _INTENT,  # type: ignore[arg-type]
        deadline_monotonic=time.monotonic() + 10.0,
    )

    assert attempts == ["TIER3-AUTO"]
    record = runtime._node_judgement_state().deferred_group_round
    if deferred:
        assert source == "group_healthcheck_deferred"
        assert record is not None and record["source"] == "startup_queue"
    else:
        # A round that reached Mihomo is never repeated.
        assert source == "group_healthcheck"
        assert record is None


def test_startup_round_inline_group_uses_its_own_timeout() -> None:
    runtime, calls = _startup_runtime(flag=True, group="TIER1-AUTO")

    source = runtime._trigger_startup_native_fallback_health(
        _health_snapshot("TIER1-AUTO"),
        _INTENT,  # type: ignore[arg-type]
        deadline_monotonic=time.monotonic() + 4.0,
    )

    assert source == "group_healthcheck"
    assert calls == [("TIER1-AUTO", CONTRACT_URL, 800, _NATIVE_ROUND_KWARGS)]


def test_deferred_round_is_dropped_after_a_core_generation_change() -> None:
    runtime, calls = _startup_runtime(flag=True)
    runtime._defer_native_group_round(
        group="TIER3-AUTO",
        probe_url=CONTRACT_URL,
        expected_status="204",
        timeout_ms=5000,
        source="startup",
    )
    runtime._core_generation = 8

    assert runtime._start_deferred_native_group_round() is False
    time.sleep(0.05)
    assert calls == []
    assert runtime._node_judgement_state().deferred_group_round is None


def test_deferred_round_falls_back_for_adapters_without_expected_status() -> None:
    runtime, _calls = _startup_runtime(flag=True)
    legacy: list[tuple[str, str, int]] = []
    runtime.test_group_delay = lambda g, *, url, timeout_ms: legacy.append(
        (g, url, int(timeout_ms))
    ) or {}

    runtime._run_native_group_round(
        "TIER3-AUTO",
        probe_url=CONTRACT_URL,
        expected_status="204",
        timeout_ms=5000,
    )

    assert legacy == [("TIER3-AUTO", CONTRACT_URL, 5000)]


def test_monitoring_lease_commit_starts_the_deferred_round_right_after_ready() -> None:
    runtime, calls = _startup_runtime(flag=True)
    runtime._runtime_monitoring_lease = None
    runtime._runtime_initial_activation_complete = lambda: True
    runtime._runtime_recovery_is_armed = lambda *_args, **_kwargs: True
    runtime._runtime_recovery_arm = _RuntimeRecoveryArm(
        worker_token=(1, 7, 4312, 8),
        snapshot_key=(4312, 8, 7, "startup-graph", 5, 0),
        binding_key=("tier3", "tier3b", "id-a", "leaf-a"),
        route_epoch=5,
        facility_epoch=0,
        selector_intent_revision=0,
        lifecycle_revision=0,
        armed_at_monotonic=time.monotonic(),
    )
    runtime._runtime_worker_token = lambda: (1, 7, 0, 4312, 8)
    runtime._runtime_generation = 1
    runtime._lifecycle_revision = 0
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._defer_native_group_round(
        group="TIER3-AUTO",
        probe_url=CONTRACT_URL,
        expected_status="204",
        timeout_ms=5000,
        source="startup",
    )

    assert runtime._commit_runtime_monitoring_lease() is True
    assert _wait_for(lambda: len(calls) == 1)
    assert runtime._commit_runtime_monitoring_lease() is True
    time.sleep(0.05)
    assert calls == [("TIER3-AUTO", CONTRACT_URL, 5000, _NATIVE_ROUND_KWARGS)]


def _capture_replay_runtime(
    config: dict[str, Any],
) -> tuple[DesktopRuntime, list[tuple[str, str, int, dict[str, Any]]]]:
    leaf_a = RuntimeNodeRef("tier3", "tier3b", "id-a", "leaf-a")
    leaf_b = RuntimeNodeRef("tier3", "tier3b", "id-b", "leaf-b")

    def snapshot(epoch: int, final_now: str, now: str) -> CoreLoadedRouteSnapshot:
        return CoreLoadedRouteSnapshot(
            process_pid=4312,
            process_generation=8,
            core_generation=12,
            semantic_digest="native-fallback-graph",
            route_epoch=epoch,
            network_context_epoch=3,
            group_graph=(
                ("FINAL-EXIT", ("PROXY-FALLBACK", "REJECT")),
                ("PROXY-FALLBACK", ("TIER3-AUTO",)),
                ("TIER3-AUTO", ("leaf-a", "leaf-b")),
            ),
            group_states=(
                CoreLoadedGroupState(
                    name="FINAL-EXIT",
                    type_name="Selector",
                    members=("PROXY-FALLBACK", "REJECT"),
                    now=final_now,
                ),
                CoreLoadedGroupState(
                    name="TIER3-AUTO",
                    type_name="Fallback",
                    members=("leaf-a", "leaf-b"),
                    now=now,
                    test_url=CONTRACT_URL,
                    expected_status="204",
                ),
            ),
            proxy_nodes=(leaf_a, leaf_b),
        )

    before = snapshot(60, "REJECT", "leaf-a")
    after = snapshot(61, "PROXY-FALLBACK", "leaf-b")
    runtime = object.__new__(DesktopRuntime)
    runtime.config = config
    runtime.logger = logging.getLogger("test.node-judgement-capture")
    runtime.stop_event = threading.Event()
    runtime._core_lifecycle_lock = threading.RLock()
    runtime._core_generation = 12
    runtime._runtime_generation = 0
    runtime._lifecycle_revision = 0
    runtime._tier3_egress_activation_generation = 0
    runtime._runtime_selector_intent_revision = 7
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_route_mutation_lease = {}
    runtime._runtime_route_snapshot_lock = threading.RLock()
    state = {"snapshot": before, "payload": {"proxies": {"phase": "reset"}}}
    runtime._runtime_route_snapshot_state = lambda: (state["snapshot"], state["payload"])
    runtime._refresh_runtime_route_snapshot = lambda **_kwargs: state["snapshot"]
    runtime._apply_runtime_automatic_selection = (
        lambda action, **_kwargs: {"commit_state": "committed"}
    )

    def binding() -> RuntimeRouteBinding:
        return RuntimeRouteBinding(
            process_pid=after.process_pid,
            process_generation=after.process_generation,
            core_generation=after.core_generation,
            semantic_digest=after.semantic_digest,
            route_epoch=after.route_epoch,
            network_context_epoch=after.network_context_epoch,
            observed_outbound="FINAL-EXIT",
            route_chain=("FINAL-EXIT", "PROXY-FALLBACK", "TIER3-AUTO", "leaf-b"),
            node=leaf_b,
        )

    def authority(**_kwargs: Any) -> Any:
        state["snapshot"] = after
        return after, state["payload"], binding()

    runtime._refresh_loaded_runtime_authority = authority
    runtime._runtime_route_binding_for_outbound = lambda *_args, **_kwargs: binding()
    runtime._verify_runtime_final_exit = lambda **_kwargs: {"status": 204}
    calls: list[tuple[str, str, int, dict[str, Any]]] = []
    runtime.test_group_delay = lambda g, *, url, timeout_ms, **kwargs: calls.append(
        (g, url, int(timeout_ms), dict(kwargs))
    ) or {}
    runtime._capture_replay_leaf = leaf_a  # type: ignore[attr-defined]
    runtime._capture_replay_intent = StartupRouteIntent(  # type: ignore[attr-defined]
        actions=(("FINAL-EXIT", "PROXY-FALLBACK", "selector"),),
        top_level_group="FINAL-EXIT",
        top_level_target="PROXY-FALLBACK",
        expected_gateway_key=None,
        selector_intent_revision=7,
        provider_version="provider-v8",
        loaded_order=("leaf-a", "leaf-b"),
        snapshot_key=before.snapshot_key,
    )
    return runtime, calls


def _run_capture_replay(runtime: DesktopRuntime) -> dict[str, Any]:
    return runtime._verify_capture_route_after_reload(
        lease_record=None,
        route_intent=runtime._capture_replay_intent,  # type: ignore[attr-defined]
        preferred_leaf_ref=runtime._capture_replay_leaf,  # type: ignore[attr-defined]
        preferred_probe_group="TIER3-AUTO",
        timeout_seconds=5.0,
    )


def test_post_capture_replay_flag_off_keeps_the_shortened_round() -> None:
    runtime, calls = _capture_replay_runtime({})

    assert _run_capture_replay(runtime) == {"status": 204}

    assert len(calls) == 1
    group, url, timeout_ms, kwargs = calls[0]
    assert (group, url, kwargs) == ("TIER3-AUTO", CONTRACT_URL, {})
    assert timeout_ms < 5000
    assert runtime.__dict__.get("_node_judgement_runtime_state") is None


def test_post_capture_replay_flag_on_never_shortens_and_defers_until_ready() -> None:
    runtime, calls = _capture_replay_runtime(
        _judgement_config(group_round_native_timeout=True)
    )

    assert _run_capture_replay(runtime) == {"status": 204}

    # The replay window (<= 6 s, 2.5 s reserved) cannot hold 5 s + slack.
    assert calls == []
    deferred = runtime._node_judgement_state().deferred_group_round
    assert deferred is not None
    assert (deferred["group"], deferred["probe_url"], deferred["timeout_ms"]) == (
        "TIER3-AUTO",
        CONTRACT_URL,
        5000,
    )
    assert deferred["expected_status"] == "204"
    assert deferred["source"] == "post_capture"


@pytest.mark.parametrize("queue_lost", [False, True])
def test_post_capture_replay_round_that_fits_starts_its_window_at_admission(
    queue_lost: bool,
) -> None:
    runtime, calls = _capture_replay_runtime(
        _judgement_config(group_round_native_timeout=True)
    )
    # An inline-sized native timeout fits the replay window.
    runtime._runtime_group_native_timeout_ms = lambda _group: 800
    if queue_lost:

        def busy(g: str, *, url: str, timeout_ms: int, **kwargs: Any) -> dict[str, Any]:
            calls.append((g, url, int(timeout_ms), dict(kwargs)))
            raise _RuntimeProbeBudgetUnavailable(
                "runtime data-plane probe is indeterminate",
                probe_indeterminate_reason="queue_timeout",
            )

        runtime.test_group_delay = busy

    assert _run_capture_replay(runtime) == {"status": 204}

    assert len(calls) == 1
    group, url, timeout_ms, kwargs = calls[0]
    assert (group, url, timeout_ms) == ("TIER3-AUTO", CONTRACT_URL, 800)
    assert kwargs["expected_status"] == "204"
    # 5.0 s replay window - 2.5 s reserve - 1.05 s round - 0.05 s dispatch.
    assert kwargs["native_admission_seconds"] == pytest.approx(1.4, abs=0.05)
    deferred = runtime._node_judgement_state().deferred_group_round
    if queue_lost:
        # Never issued: one round right after READY instead.
        assert deferred is not None and deferred["source"] == "post_capture_queue"
    else:
        assert deferred is None


def _warm_runtime(
    config: dict[str, Any],
    snapshot: CoreLoadedRouteSnapshot | None,
) -> tuple[DesktopRuntime, list[tuple[str, str, int, dict[str, Any]]]]:
    runtime = _bare_runtime(config)
    runtime._tier3_is_primary_export = lambda: True

    def mixed_port_fails() -> dict[str, Any]:
        raise RuntimeError("mixed-port probe failed")

    runtime._probe_mihomo_mixed_port = mixed_port_fails
    runtime._mihomo_controller_config = lambda: {"enabled": True}
    runtime._refresh_runtime_route_snapshot = lambda **_kwargs: snapshot
    runtime._runtime_route_snapshot_state = lambda: (snapshot, {})
    runtime._default_probe_urls = lambda: list(NEUTRAL_URLS)
    runtime._startup_probe_timeout_seconds = lambda: 3.0
    calls: list[tuple[str, str, int, dict[str, Any]]] = []
    runtime.test_group_delay = lambda g, *, url, timeout_ms, **kwargs: calls.append(
        (g, url, int(timeout_ms), dict(kwargs))
    ) or {}
    return runtime, calls


def test_warm_flag_off_keeps_the_neutral_url_round() -> None:
    runtime, calls = _warm_runtime({}, _health_snapshot())

    assert runtime._warm_tier3_group_after_core_start() is True

    assert calls == [("TIER3-AUTO", NEUTRAL_URLS[0], 3000, {})]


def test_warm_flag_on_runs_the_contract_round_at_the_native_timeout() -> None:
    runtime, calls = _warm_runtime(
        _judgement_config(group_round_native_timeout=True),
        _health_snapshot(),
    )

    assert runtime._warm_tier3_group_after_core_start() is True

    assert calls == [("TIER3-AUTO", CONTRACT_URL, 5000, _NATIVE_ROUND_KWARGS)]


def test_warm_flag_on_never_falls_back_to_a_neutral_url() -> None:
    runtime, calls = _warm_runtime(
        _judgement_config(group_round_native_timeout=True),
        None,
    )

    assert runtime._warm_tier3_group_after_core_start() is False

    assert calls == []


# ---------------------------------------------------------------------------
# P0-3 and P0-5: route-snapshot publication


class _PinController:
    """Fake controller for the TIER3-AUTO group read and DELETE."""

    def __init__(self, state: dict[str, Any], *, commit: bool = True) -> None:
        self.state = state
        self.commit = commit
        self.deletes: list[str] = []
        self.reads: list[str] = []
        self.deleted = threading.Event()

    def proxy(self, name: str, *, timeout: float | None = None) -> dict[str, Any]:
        del timeout
        self.reads.append(name)
        return copy.deepcopy(self.state["proxies"]["proxies"][name])

    def unfix_proxy(self, group: str, *, timeout: float | None = None) -> dict[str, Any]:
        del timeout
        self.deletes.append(group)
        if self.commit:
            entry = self.state["proxies"]["proxies"][group]
            entry["fixed"] = ""
            entry["now"] = entry["all"][1]
        self.deleted.set()
        return {"group": group, "fixed": False}


def _publication_runtime(
    tmp_path: Path,
    *,
    flags: dict[str, Any] | None = None,
    fixed: str = "",
    alive: tuple[bool, ...] = (True, True, True),
    ready: bool = True,
) -> tuple[DesktopRuntime, dict[str, Any], _ListHandler]:
    config: dict[str, Any] = {
        "client": {
            "runtime_state_path": str(tmp_path / "runtime-state.json"),
            "system_proxy": {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 7890,
                "health_failures_before_restore": 2,
            },
        },
        "alerts": {
            "log_file": str(tmp_path / "monitor.log"),
            "desktop": {"enabled": False},
        },
        # These tests pin the publication hooks under the
        # release_stale_fallback_pins flag alone; the verified provider lane
        # (11-02) always releases a stale pin at publication, which its own
        # tests cover (tests/test_tier3_verified_provider_writes_real_core.py).
        "tiers": {"tier3": {"verified_provider_writes": False}},
    }
    if flags is not None:
        config["client"]["egress_recovery"] = {"node_judgement": dict(flags)}
    runtime = DesktopRuntime(config, project_root=tmp_path)
    for handler in list(runtime.logger.handlers):
        handler.close()
        runtime.logger.removeHandler(handler)
    runtime.logger = logging.getLogger(f"test.node-judgement-publication.{id(runtime)}")
    runtime.logger.handlers.clear()
    runtime.logger.setLevel(logging.DEBUG)
    runtime.logger.propagate = False
    handler = _ListHandler()
    runtime.logger.addHandler(handler)
    runtime.system_proxy = SystemProxyManager(
        config["client"]["system_proxy"],
        backend=MemoryProxyBackend(ProxySettings(enabled=False, server="", override=False)),
        platform_name="windows",
    )
    runner = FakeCoreRunner(running=True, port_ok=True)
    runner.status_payload.update({"pid": 4321, "process_generation": 2})
    runtime.core_runner = runner
    runtime._core_generation = 7
    members = ["member-alpha", "member-beta", "member-gamma"]
    leaves = {
        name: {
            "type": "Shadowsocks",
            "alive": is_alive,
            "extra": {CONTRACT_URL: {"alive": is_alive, "history": []}},
        }
        for name, is_alive in zip(members, alive)
    }
    state: dict[str, Any] = {
        "proxies": {
            "proxies": {
                "FINAL-EXIT": {
                    "type": "Selector",
                    "now": "PROXY-FALLBACK",
                    "all": ["PROXY-FALLBACK", "REJECT"],
                },
                "PROXY-FALLBACK": {
                    "type": "Fallback",
                    "now": "TIER3-AUTO",
                    "all": ["TIER3-AUTO"],
                    "testUrl": NEUTRAL_URLS[0],
                    "expectedStatus": "204",
                },
                "TIER3-AUTO": {
                    "type": "Fallback",
                    "now": members[0],
                    "fixed": fixed,
                    "all": list(members),
                    "testUrl": CONTRACT_URL,
                    "expectedStatus": "204",
                },
                **leaves,
            }
        }
    }
    runtime.mihomo_proxies = lambda: copy.deepcopy(state["proxies"])
    proxy_map = {
        name: {
            "tier": "tier3",
            "pool_kind": "tier3b",
            "identity": f"{name}-identity",
            "proxy_name": name,
        }
        for name in members
    }
    runtime._runtime_loaded_export_contract = {
        "semantic_digest": "loaded-digest",
        "proxy_map": proxy_map,
    }
    runtime._runtime_initial_activation_complete = lambda: ready
    return runtime, state, handler


def test_route_view_line_is_hashed_and_logged_only_when_it_changes(tmp_path: Path) -> None:
    runtime, state, handler = _publication_runtime(tmp_path, alive=(True, False, True))

    assert runtime._refresh_runtime_route_snapshot() is not None
    assert runtime._refresh_runtime_route_snapshot() is not None

    lines = handler.messages("Runtime route view")
    assert len(lines) == 1
    tokenizer = runtime._node_judgement_state().tokenizer
    assert f"now={tokenizer.leaf('member-alpha')}" in lines[0]
    assert "fixed=none" in lines[0]
    assert "alive=2/3" in lines[0]
    assert "stale_fixed=False" in lines[0]
    assert "exhausted=False" in lines[0]
    assert "member-" not in lines[0]
    assert "google" not in lines[0]

    leaves = state["proxies"]["proxies"]
    for name in ("member-alpha", "member-gamma"):
        leaves[name]["alive"] = False
        leaves[name]["extra"][CONTRACT_URL]["alive"] = False
    assert runtime._refresh_runtime_route_snapshot() is not None

    lines = handler.messages("Runtime route view")
    assert len(lines) == 2
    assert "alive=0/3" in lines[1]
    assert "exhausted=True" in lines[1]


@pytest.mark.parametrize(
    "flags",
    [
        # Legacy shadow mode pinned (10-28: act is now the default and an act
        # runtime builds its judgement state for the route view).
        {"evidence_log": False, "post_dial_judge": "shadow", "tier_ladder_judge": False},
        {"enabled": False, "release_stale_fallback_pins": True},
    ],
)
def test_route_view_and_pin_release_are_silent_when_off(
    tmp_path: Path,
    flags: dict[str, Any],
) -> None:
    runtime, _state, handler = _publication_runtime(
        tmp_path,
        flags=flags,
        fixed="member-removed",
    )
    controller = _PinController(_state)
    runtime._require_api_client = lambda: controller

    assert runtime._refresh_runtime_route_snapshot() is not None
    time.sleep(0.1)

    assert handler.messages("Runtime route view") == []
    assert handler.messages("Stale fallback pin release") == []
    assert controller.deletes == []
    assert controller.reads == []
    assert runtime.__dict__.get("_node_judgement_runtime_state") is None


def test_stale_non_member_pin_is_released_once_per_core_generation(tmp_path: Path) -> None:
    runtime, state, handler = _publication_runtime(
        tmp_path,
        flags={"release_stale_fallback_pins": True},
        fixed="member-removed",
    )
    controller = _PinController(state, commit=False)
    runtime._require_api_client = lambda: controller

    snapshot = runtime._refresh_runtime_route_snapshot()
    assert snapshot is not None
    assert controller.deleted.wait(2.0)
    assert _wait_for(lambda: handler.messages("Stale fallback pin release"))

    assert controller.deletes == ["TIER3-AUTO"]
    lines = handler.messages("Stale fallback pin release")
    tokenizer = runtime._node_judgement_state().tokenizer
    assert lines == [
        "Stale fallback pin release (group=TIER3-AUTO "
        f"fixed={tokenizer.leaf('member-removed')} outcome=not_committed)"
    ]
    # The release revoked the snapshot like any application-owned write.
    assert runtime._runtime_route_epoch > snapshot.route_epoch
    view_lines = handler.messages("Runtime route view")
    assert view_lines and "stale_fixed=True" in view_lines[0]

    # The Core kept the pin: the same (generation, group, name) is not retried.
    assert _wait_for(lambda: runtime._refresh_runtime_route_snapshot() is not None)
    time.sleep(0.1)
    assert controller.deletes == ["TIER3-AUTO"]

    # A new Core generation may release the same name once more.
    runtime._core_generation = 8
    assert runtime._refresh_runtime_route_snapshot() is not None
    assert _wait_for(lambda: len(controller.deletes) == 2)


def test_released_pin_lets_mihomo_pick_again_and_is_read_back(tmp_path: Path) -> None:
    runtime, state, handler = _publication_runtime(
        tmp_path,
        flags={"release_stale_fallback_pins": True},
        fixed="member-removed",
    )
    controller = _PinController(state)
    runtime._require_api_client = lambda: controller

    assert runtime._refresh_runtime_route_snapshot() is not None
    assert controller.deleted.wait(2.0)
    assert _wait_for(lambda: handler.messages("Stale fallback pin release"))

    assert handler.messages("Stale fallback pin release")[0].endswith("outcome=released)")
    # Pre-read and readback, one DELETE in between.
    assert controller.reads == ["TIER3-AUTO", "TIER3-AUTO"]
    assert state["proxies"]["proxies"]["TIER3-AUTO"]["fixed"] == ""


def test_pin_naming_a_member_is_never_touched(tmp_path: Path) -> None:
    runtime, state, handler = _publication_runtime(
        tmp_path,
        flags={"release_stale_fallback_pins": True},
        fixed="member-beta",
    )
    controller = _PinController(state)
    runtime._require_api_client = lambda: controller

    assert runtime._refresh_runtime_route_snapshot() is not None
    time.sleep(0.1)

    assert controller.deletes == []
    assert controller.reads == []
    assert handler.messages("Stale fallback pin release") == []


def test_pin_release_waits_for_ready(tmp_path: Path) -> None:
    runtime, state, _handler = _publication_runtime(
        tmp_path,
        flags={"release_stale_fallback_pins": True},
        fixed="member-removed",
        ready=False,
    )
    controller = _PinController(state)
    runtime._require_api_client = lambda: controller

    assert runtime._refresh_runtime_route_snapshot() is not None
    time.sleep(0.1)

    assert controller.deletes == []


def test_pin_release_never_waits_for_a_busy_core_transaction(tmp_path: Path) -> None:
    # The flag is off here so publication queues nothing; the worker is
    # driven directly while another Core transaction owns the lease.
    runtime, state, handler = _publication_runtime(
        tmp_path,
        flags={"release_stale_fallback_pins": False},
        fixed="member-removed",
    )
    controller = _PinController(state)
    runtime._require_api_client = lambda: controller
    snapshot = runtime._refresh_runtime_route_snapshot()
    assert snapshot is not None
    _snapshot, payload = runtime._runtime_route_snapshot_state()
    assert payload is not None
    binding = runtime._runtime_route_binding_for_outbound(snapshot, payload, "FINAL-EXIT")
    lease = runtime._runtime_egress_coordinator.begin_core_mutation(
        binding,
        expected_snapshot=snapshot,
    )
    assert lease is not None
    key = (7, "TIER3-AUTO", "member-removed")
    try:
        started = time.monotonic()
        outcome = runtime._release_stale_fallback_pin(snapshot, key)
        elapsed = time.monotonic() - started
    finally:
        runtime._runtime_egress_coordinator.end_core_mutation(lease)

    assert outcome == "lease_busy"
    assert elapsed < 0.2
    assert controller.deletes == []
    assert controller.reads == []
    state_holder = runtime._node_judgement_state()
    assert key in state_holder.stale_pin_retry_after
    assert key not in state_holder.stale_pin_released
    # Busy is not logged as an outcome, and the back-off suppresses a queue.
    assert handler.messages("Stale fallback pin release") == []
    runtime.config["client"]["egress_recovery"]["node_judgement"][
        "release_stale_fallback_pins"
    ] = True
    view = runtime._runtime_tier3_route_view(snapshot, payload["proxies"])
    assert view is not None and view["stale_fixed"] is True
    assert runtime._queue_stale_fallback_pin_release(snapshot, view) is False
    state_holder.stale_pin_retry_after[key] = 0.0
    assert runtime._queue_stale_fallback_pin_release(snapshot, view) is True
    assert controller.deleted.wait(2.0)


def _drive_pin_release_directly(
    tmp_path: Path,
) -> tuple[DesktopRuntime, dict[str, Any], _PinController, CoreLoadedRouteSnapshot]:
    # The flag is off so publication queues nothing; the worker is driven
    # directly, exactly as the queued thread would run it.
    runtime, state, _handler = _publication_runtime(
        tmp_path,
        flags={"release_stale_fallback_pins": False},
        fixed="member-removed",
    )
    controller = _PinController(state)
    runtime._require_api_client = lambda: controller
    snapshot = runtime._refresh_runtime_route_snapshot()
    assert snapshot is not None
    return runtime, state, controller, snapshot


def test_pin_release_backs_off_while_a_user_or_reload_holds_the_core_lock(
    tmp_path: Path,
) -> None:
    runtime, _state, controller, snapshot = _drive_pin_release_directly(tmp_path)
    held = threading.Event()
    release = threading.Event()

    def hold_core_lock() -> None:
        # A user selection / ApplyConfig owns only the shared Core lock.
        with runtime._core_transition_lock:
            held.set()
            release.wait(2.0)

    holder = threading.Thread(target=hold_core_lock, daemon=True)
    holder.start()
    assert held.wait(2.0)
    key = (7, "TIER3-AUTO", "member-removed")
    try:
        started = time.monotonic()
        outcome = runtime._release_stale_fallback_pin(snapshot, key)
        elapsed = time.monotonic() - started
    finally:
        release.set()
        holder.join(2.0)

    assert outcome == "core_busy"
    assert elapsed < 0.2
    assert controller.reads == []
    assert controller.deletes == []
    # The lease was given back, and the attempt is retried later.
    assert runtime._runtime_egress_coordinator.core_mutation_active() is False
    state_holder = runtime._node_judgement_state()
    assert key in state_holder.stale_pin_retry_after
    assert key not in state_holder.stale_pin_released


def test_pin_that_became_a_member_by_the_pre_read_is_never_touched(
    tmp_path: Path,
) -> None:
    # Published as a stale non-member pin, but by the worker's own pre-read
    # the name is a member again (a provider update in between): P0-3 never
    # touches a pin that names a member.
    runtime, state, controller, snapshot = _drive_pin_release_directly(tmp_path)
    group = state["proxies"]["proxies"]["TIER3-AUTO"]
    group["all"] = [*group["all"], "member-removed"]
    key = (7, "TIER3-AUTO", "member-removed")

    outcome = runtime._release_stale_fallback_pin(snapshot, key)

    assert outcome == "not_stale"
    assert controller.reads == ["TIER3-AUTO"]
    assert controller.deletes == []
    assert group["fixed"] == "member-removed"
    state_holder = runtime._node_judgement_state()
    assert key in state_holder.stale_pin_released
    assert key not in state_holder.stale_pin_retry_after


def test_user_selection_landing_during_the_pre_read_is_never_erased(
    tmp_path: Path,
) -> None:
    runtime, state, controller, snapshot = _drive_pin_release_directly(tmp_path)
    selections: list[tuple[str, str, float]] = []

    def select_proxy(group: str, name: str, *, timeout: float | None = None) -> dict[str, Any]:
        del timeout
        state["proxies"]["proxies"][group]["fixed"] = name
        selections.append((group, name, time.monotonic()))
        return {"group": group, "fixed": name}

    controller.select_proxy = select_proxy
    user = threading.Thread(
        target=lambda: runtime.select_mihomo_proxy(
            "TIER3-AUTO", "member-beta", revalidate=False
        ),
        daemon=True,
    )
    original_proxy = controller.proxy
    pre_read_done: list[float] = []

    def proxy(name: str, *, timeout: float | None = None) -> dict[str, Any]:
        value = original_proxy(name, timeout=timeout)
        if not pre_read_done:
            # The user clicks during the pre-read: the intent is declared
            # first, then the selection waits for the shared Core lock.
            runtime._declare_runtime_selector_intent("manual Mihomo selection")
            user.start()
            time.sleep(0.05)
            pre_read_done.append(time.monotonic())
        return value

    controller.proxy = proxy
    key = (7, "TIER3-AUTO", "member-removed")

    outcome = runtime._release_stale_fallback_pin(
        snapshot,
        key,
        selector_intent_revision=0,
    )
    user.join(2.0)

    assert outcome == "superseded"
    assert controller.deletes == []
    # The selection waited for the release to let go of the Core lock, and
    # its pin stands.
    assert [(group, name) for group, name, _at in selections] == [
        ("TIER3-AUTO", "member-beta")
    ]
    assert selections[0][2] >= pre_read_done[0]
    assert state["proxies"]["proxies"]["TIER3-AUTO"]["fixed"] == "member-beta"
    state_holder = runtime._node_judgement_state()
    assert key not in state_holder.stale_pin_released


# ---------------------------------------------------------------------------
# P0-4: 校验 by the owning group's route contract


class _RouteContractController:
    """Controller URL tests.  A failed test on the accused leaf demotes it
    natively (the owning group moves to the successor) before it replies."""

    def __init__(
        self,
        on_accused: Any,
        *,
        accused_error: Exception | None,
        successor_delay: Any = None,
    ) -> None:
        self.on_accused = on_accused
        self.accused_error = accused_error
        self.successor_delay = successor_delay
        self.calls: list[dict[str, Any]] = []

    def proxy_delay(
        self,
        name: str,
        *,
        url: str,
        timeout_ms: int = 5000,
        expected_status: Any = None,
        request_timeout: float | None = None,
        connection_abort_sink: Any = None,
        verify_expected_status: bool = False,
    ) -> dict[str, Any]:
        aborted = threading.Event()
        if connection_abort_sink is not None:
            connection_abort_sink(aborted.set)
        self.calls.append(
            {
                "name": name,
                "url": url,
                "timeout_ms": timeout_ms,
                "expected_status": expected_status,
                "request_timeout": request_timeout,
                "verify": verify_expected_status,
                "at": time.monotonic(),
            }
        )
        if name == "leaf-A":
            if self.accused_error is not None:
                # Mihomo writes the failed sample before it replies.
                self.on_accused()
                raise self.accused_error
            return {"delay": 40}
        if self.successor_delay is not None:
            self.successor_delay(aborted)
        return {"delay": 55}


def _route_contract_snapshot(
    *,
    selected: RuntimeNodeRef,
    route_epoch: int,
    core_generation: int = 11,
) -> CoreLoadedRouteSnapshot:
    return CoreLoadedRouteSnapshot(
        process_pid=101,
        process_generation=7,
        core_generation=core_generation,
        semantic_digest="loaded-digest",
        route_epoch=route_epoch,
        group_graph=(
            ("FINAL-EXIT", ("TIER3-AUTO", "REJECT")),
            ("TIER3-AUTO", ("leaf-A", "leaf-B")),
        ),
        group_states=(
            CoreLoadedGroupState(
                name="FINAL-EXIT",
                type_name="Selector",
                members=("TIER3-AUTO", "REJECT"),
                now="TIER3-AUTO",
            ),
            CoreLoadedGroupState(
                name="TIER3-AUTO",
                type_name="Fallback",
                members=("leaf-A", "leaf-B"),
                now=selected.proxy_name,
                test_url=CONTRACT_URL,
                expected_status="204",
            ),
        ),
        proxy_nodes=(_LEAF_A, _LEAF_B),
    )


_LEAF_A = RuntimeNodeRef(
    tier="tier3",
    pool_kind="tier3b",
    identity="leaf-a-identity",
    proxy_name="leaf-A",
)
_LEAF_B = RuntimeNodeRef(
    tier="tier3",
    pool_kind="tier3b",
    identity="leaf-b-identity",
    proxy_name="leaf-B",
)


def _route_contract_payload(selected: str, *, accused_dead: bool) -> dict[str, Any]:
    leaf_a_alive = not accused_dead
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
                    "testUrl": CONTRACT_URL,
                    "expectedStatus": "204",
                },
                "leaf-A": {
                    "type": "Shadowsocks",
                    "alive": leaf_a_alive,
                    "extra": {CONTRACT_URL: {"alive": leaf_a_alive}},
                },
                "leaf-B": {
                    "type": "Shadowsocks",
                    "alive": True,
                    "extra": {CONTRACT_URL: {"alive": True}},
                },
            }
        }
    }


def _route_contract_binding(
    snapshot: CoreLoadedRouteSnapshot,
    selected: RuntimeNodeRef,
) -> RuntimeRouteBinding:
    return RuntimeRouteBinding(
        process_pid=snapshot.process_pid,
        process_generation=snapshot.process_generation,
        core_generation=snapshot.core_generation,
        semantic_digest=snapshot.semantic_digest,
        route_epoch=snapshot.route_epoch,
        observed_outbound="FINAL-EXIT",
        route_chain=("FINAL-EXIT", "TIER3-AUTO", selected.proxy_name),
        node=selected,
    )


class _ManualHarness:
    def __init__(
        self,
        *,
        config: dict[str, Any] | None = None,
        accused_error: Exception | None,
        neutral_ok: bool = True,
        neutral_delay: float = 0.0,
        successor_delay: Any = None,
        moved_core_generation: int = 11,
        budget: tuple[float, float] | None = None,
        real_coordinator: bool = False,
    ) -> None:
        fixture = exit_runtime_tests.ExitQualityRuntimeTests()
        runtime, _service, _audit = fixture._runtime_for_check(
            exit_runtime_tests._unavailable_identity_payload(),
            delay_client=exit_runtime_tests._DelayClient(),
        )
        self.runtime = runtime
        runtime.config = config if config is not None else {
            "client": {"mihomo_mixed_port": 7890}
        }
        runtime.refresh_exit_quality_static_data_async = lambda **_kwargs: False
        runtime._queue_final_exit_identity_diagnostic = lambda **_kwargs: None
        runtime._runtime_failure_monitor_lock = threading.RLock()
        runtime._runtime_route_snapshot_lock = threading.RLock()
        runtime._runtime_route_mutation_lease = None
        runtime._runtime_incident_recovery_state = {}
        runtime._runtime_selector_intent_revision = 0
        runtime._runtime_probe_broker = RuntimeProbeBroker(max_active_sockets=2)
        runtime._default_probe_urls = lambda: list(NEUTRAL_URLS)
        runtime._register_runtime_internal_probe = lambda *_args, **_kwargs: None
        if budget is not None:
            runtime._manual_exit_quality_probe_budget = lambda **_kwargs: budget
        self.snapshot_a = _route_contract_snapshot(selected=_LEAF_A, route_epoch=8)
        self.snapshot_b = _route_contract_snapshot(
            selected=_LEAF_B,
            route_epoch=9,
            core_generation=moved_core_generation,
        )
        self.state: dict[str, Any] = {
            "snapshot": self.snapshot_a,
            "binding": _route_contract_binding(self.snapshot_a, _LEAF_A),
            "payload": _route_contract_payload("leaf-A", accused_dead=False),
        }
        runtime._runtime_route_epoch = 8
        runtime._runtime_network_context_epoch = 0
        runtime._current_runtime_failure_binding_state = lambda **_kwargs: (
            self.state["binding"],
            self.state["snapshot"],
            self.state["payload"],
        )
        def current_route_context(**_kwargs: Any) -> dict[str, Any]:
            # The same logical route the check derives from the controller.
            snapshot = self.state["snapshot"]
            route = dict(
                inspect_runtime_route(
                    self.state["payload"]["proxies"],
                    start="FINAL-EXIT",
                )
            )
            route.update(
                {
                    "path_kind": "mihomo_proxy",
                    "core_instance": (
                        f"0:{snapshot.core_generation}:{snapshot.process_pid}"
                    ),
                    "confirmed": True,
                }
            )
            return route

        runtime.current_route_context = current_route_context
        state = self.state
        runtime._runtime_route_snapshot = self.snapshot_a
        runtime._runtime_route_snapshot_payload = self.state["payload"]
        # Ticket and health-commit bookkeeping the §18.7 native commit uses.
        runtime._runtime_recovery_tickets = {}
        runtime._runtime_recovery_outcomes = {}
        runtime._runtime_recovery_outcome_events = {}
        runtime._runtime_incident_bindings = {}
        runtime._runtime_incident_route_payloads = {}
        runtime._runtime_generation = 3
        runtime._runtime_worker_token = lambda: (1,)
        runtime._runtime_worker_token_is_current = lambda _token: True
        runtime._runtime_required_pass_count = lambda *_args, **_kwargs: 1
        self.revoked: list[str] = []
        runtime._revoke_tier3_runtime_leaf_qualification = self.revoked.append
        self.health: list[dict[str, Any]] = []
        self.rechecks: list[Any] = []
        self.health_accepted = True

        def apply_health(observation: dict[str, Any]) -> dict[str, Any]:
            self.health.append(dict(observation))
            return {
                "accepted": self.health_accepted,
                "changed": self.health_accepted,
                "action": "suspicious" if self.health_accepted else "rejected",
            }

        runtime.manager = type(
            "Manager",
            (),
            {
                "effective_egress_ok": True,
                "runtime_generation": 3,
                "apply_runtime_health_observation": staticmethod(apply_health),
                "enqueue_runtime_leaf_recheck": staticmethod(
                    lambda node, **kwargs: self.rechecks.append((node, kwargs))
                ),
            },
        )()

        class Coordinator:
            def __init__(self) -> None:
                self.observed: list[Any] = []
                self.probes: list[Any] = []
                self.quarantined: list[Any] = []
                self.terminal: list[Any] = []
                self.indeterminate: list[Any] = []

            @staticmethod
            def current_route_snapshot() -> CoreLoadedRouteSnapshot:
                return state["snapshot"]

            @staticmethod
            def snapshot_ownership_matches(
                binding: RuntimeRouteBinding,
                *,
                expected_snapshot: CoreLoadedRouteSnapshot | None = None,
            ) -> bool:
                current = state["snapshot"]
                return bool(
                    current.owns(binding)
                    and (
                        expected_snapshot is None
                        or expected_snapshot.snapshot_key == current.snapshot_key
                    )
                )

            def observe(self, failure: Any, **kwargs: Any) -> RuntimeFailureCircuitDecision:
                self.observed.append((failure, kwargs.get("current_snapshot")))
                return RuntimeFailureCircuitDecision(
                    accepted=True,
                    start_probe=True,
                    incident_id="native-incident",
                    probe_token="native-probe-token",
                )

            @staticmethod
            def current_snapshot_for(_binding: Any) -> None:
                return None

            def record_probe(self, failure: Any, **kwargs: Any) -> RuntimeFailureCircuitDecision:
                self.probes.append((failure, kwargs))
                return RuntimeFailureCircuitDecision(
                    accepted=True,
                    confirmed=True,
                    incident_id="native-incident",
                    probe_token="native-probe-token",
                )

            def mark_quarantined(self, binding: Any, **kwargs: Any) -> None:
                self.quarantined.append((binding, kwargs))

            def mark_terminal_outcome(self, binding: Any, outcome: Any, **kwargs: Any) -> None:
                self.terminal.append((binding, outcome))

            def finish_indeterminate(self, binding: Any, **kwargs: Any) -> None:
                self.indeterminate.append((binding, kwargs))

        self.coordinator = Coordinator()
        runtime._runtime_egress_coordinator = self.coordinator
        self.real_coordinator: RuntimeEgressCoordinator | None = None
        if real_coordinator:
            self.real_coordinator = RuntimeEgressCoordinator(
                queue_capacity=8,
                automatic_guard_enabled=False,
            )
            self.real_coordinator.set_current_snapshot(self.snapshot_a)
            runtime._runtime_egress_coordinator = self.real_coordinator
        self.recoveries: list[Any] = []
        runtime._start_runtime_recovery = (
            lambda *args, **_kwargs: self.recoveries.append(args)
        )
        runtime._run_runtime_early_confirmed_recovery = (
            lambda *args, **_kwargs: self.recoveries.append(args)
        )
        self.controller = _RouteContractController(
            self._demote_accused,
            accused_error=accused_error,
            successor_delay=successor_delay,
        )
        runtime._require_api_client = lambda: self.controller
        self.neutral_calls: list[dict[str, Any]] = []

        def neutral(**kwargs: Any) -> dict[str, Any]:
            context = kwargs.get("execution_context")
            if context is not None:
                assert context.mark_socket_started()
            if neutral_delay:
                time.sleep(neutral_delay)
            # The fake binds the neutral CONNECT to the group's pick at dial
            # time, as Mihomo's fallback does.
            self.neutral_calls.append(
                {
                    "url": kwargs.get("probe_url"),
                    "leaf": self.state["binding"].node.proxy_name,
                    "at": time.monotonic(),
                }
            )
            if not neutral_ok:
                raise TimeoutError("neutral HTTPS timed out")
            return {"status": 204}

        runtime._probe_mihomo_mixed_port_once_leased = neutral

    def _demote_accused(self) -> None:
        if self.state["snapshot"] is self.snapshot_b:
            return
        self.state.update(
            {
                "snapshot": self.snapshot_b,
                "binding": _route_contract_binding(self.snapshot_b, _LEAF_B),
                "payload": _route_contract_payload("leaf-B", accused_dead=True),
            }
        )
        self.runtime._runtime_route_epoch = 9
        self.runtime._runtime_route_snapshot = self.snapshot_b
        self.runtime._runtime_route_snapshot_payload = self.state["payload"]
        if self.real_coordinator is not None:
            self.real_coordinator.set_current_snapshot(self.snapshot_b)

    def check(self) -> dict[str, Any]:
        return self.runtime.check_exit_quality(
            proxy_url=None,
            request_id="manual-route-contract",
        )

    def calls_for(self, name: str) -> list[dict[str, Any]]:
        return [call for call in self.controller.calls if call["name"] == name]


def test_zombie_that_fails_only_its_group_contract_moves_natively_and_is_verified() -> None:
    harness = _ManualHarness(
        accused_error=MihomoDelayTransportFailed("controller HTTP 503 from delay test"),
    )

    result = harness.check()

    neutral = result["diagnostics"]["neutral_connectivity"]
    assert neutral["route_contract"] is True
    assert neutral["route_contract_leaf_failure"] is True
    assert neutral["contract_failure_kinds"] == ["connect", "none"]
    assert neutral["outcome"] == "switch_before_return"
    assert neutral["switch_committed"] is True
    assert neutral["switch_reason"] == "native_fallback_verified"
    assert neutral["state"] == "recovered"
    assert neutral["backup_verified"] is True
    assert result["state"] == "pending_identity"
    # The accused URL test ran on the group's exact contract.
    accused = harness.calls_for("leaf-A")
    assert [(call["url"], call["expected_status"], call["verify"]) for call in accused] == [
        (CONTRACT_URL, "204", True)
    ]
    # ... only after the neutral connection had been routed via the leaf.
    assert [call["leaf"] for call in harness.neutral_calls] == ["leaf-A"]
    assert harness.neutral_calls[0]["url"] == NEUTRAL_URLS[0]
    assert harness.neutral_calls[0]["at"] <= accused[0]["at"]
    # Mihomo's own successor passed both contracts; nothing was written.
    successor = harness.calls_for("leaf-B")
    assert sorted(call["url"] for call in successor) == sorted(
        [CONTRACT_URL, NEUTRAL_URLS[0]]
    )
    assert all(call["timeout_ms"] >= 1500 for call in successor)
    assert harness.recoveries == []
    assert harness.runtime._runtime_route_mutation_lease is None
    # §18.7 native_commit: the failed leaf entered the pool rules, bound to
    # the rebound snapshot (Mihomo's own move), exactly once.
    assert [
        (failure.binding.node, failure.binding.route_epoch, snapshot.route_epoch)
        for failure, snapshot in harness.coordinator.observed
    ] == [(_LEAF_A, 9, 9)]
    assert len(harness.coordinator.probes) == 1
    assert [
        (item["proxy_name"], item["verdict"], item["core_generation"])
        for item in harness.health
    ] == [("leaf-A", "confirmed", 11)]
    assert [node for node, _kwargs in harness.rechecks] == [_LEAF_A]
    assert harness.revoked == ["leaf-a-identity"]
    assert [binding.node for binding, _kwargs in harness.coordinator.quarantined] == [
        _LEAF_A
    ]
    outcome = harness.runtime._runtime_recovery_outcomes["native-incident"]
    assert outcome.status.value == "recovered"
    assert outcome.to_ref == _LEAF_B
    assert harness.coordinator.indeterminate == []


def test_native_move_commit_makes_no_selector_write() -> None:
    harness = _ManualHarness(
        accused_error=MihomoDelayTransportFailed("controller HTTP 503 from delay test"),
    )
    writes: list[Any] = []

    def forbid(*args: Any, **kwargs: Any) -> None:
        writes.append((args, kwargs))
        raise AssertionError("P0-4 native commit must not write a selector")

    for name in ("select_proxy", "unfix_proxy", "fix_proxy", "delete_connections"):
        setattr(harness.controller, name, forbid)
    harness.runtime._apply_runtime_automatic_selection = forbid
    harness.runtime._fast_recover_confirmed_alternative = forbid
    harness.runtime._start_runtime_connection_cleanup = forbid

    result = harness.check()

    neutral = result["diagnostics"]["neutral_connectivity"]
    assert neutral["switch_committed"] is True
    assert neutral["switch_reason"] == "native_fallback_verified"
    assert writes == []
    assert len(harness.health) == 1
    assert harness.runtime._runtime_route_mutation_lease is None


def test_native_move_with_a_rejected_health_commit_changes_nothing_else() -> None:
    harness = _ManualHarness(
        accused_error=MihomoDelayTransportFailed("controller HTTP 503 from delay test"),
    )
    harness.health_accepted = False
    published: list[Any] = []
    harness.runtime._publish_runtime_egress_readiness = (
        lambda *args, **kwargs: published.append((args, kwargs))
    )
    harness.runtime._set_effective_egress_state = (
        lambda *args, **kwargs: published.append((args, kwargs))
    )
    logs = _ListHandler()
    harness.runtime.logger.addHandler(logs)
    try:
        result = harness.check()
    finally:
        harness.runtime.logger.removeHandler(logs)

    neutral = result["diagnostics"]["neutral_connectivity"]
    # The verified native move stands; only the pool-rule commit was refused.
    assert neutral["switch_committed"] is True
    assert neutral["switch_reason"] == "native_fallback_verified_commit_rejected"
    assert len(harness.health) == 1
    assert harness.rechecks == []
    assert harness.coordinator.quarantined == []
    assert len(harness.coordinator.indeterminate) == 1
    assert published == []
    lines = logs.messages("Manual route-contract native commit")
    assert len(lines) == 1
    assert "accepted=False" in lines[0]
    assert "leaf-A" not in lines[0] and "leaf-B" not in lines[0]


def test_native_commit_runs_the_real_coordinator_incident_to_its_terminal() -> None:
    harness = _ManualHarness(
        accused_error=MihomoDelayTransportFailed("controller HTTP 503 from delay test"),
        real_coordinator=True,
    )

    result = harness.check()

    neutral = result["diagnostics"]["neutral_connectivity"]
    assert neutral["switch_committed"] is True
    assert neutral["switch_reason"] == "native_fallback_verified"
    assert [item["proxy_name"] for item in harness.health] == ["leaf-A"]
    coordinator = harness.real_coordinator
    assert coordinator is not None
    rebound = _dataclass_replace(
        _route_contract_binding(harness.snapshot_a, _LEAF_A),
        route_epoch=harness.snapshot_b.route_epoch,
    )
    circuit = coordinator.current_snapshot_for(rebound)
    assert circuit is not None
    assert circuit.state == "recovered"
    assert circuit.terminal_status is not None
    assert circuit.terminal_status.value == "recovered"
    # No Core mutation lease was ever taken: nothing was written.
    assert coordinator.core_mutation_active() is False
    assert harness.runtime._runtime_route_mutation_lease is None


def test_same_zombie_with_the_flag_off_keeps_the_neutral_pair() -> None:
    harness = _ManualHarness(
        config=_judgement_config(manual_check_uses_route_contract=False),
        accused_error=MihomoDelayTransportFailed("controller HTTP 503 from delay test"),
    )

    result = harness.check()

    neutral = result["diagnostics"]["neutral_connectivity"]
    assert "route_contract" not in neutral
    assert "route_contract_leaf_failure" not in neutral
    assert neutral["contract_failure_kinds"] == ["none", "none"]
    assert neutral["outcome"] == "current_healthy"
    assert neutral["switch_committed"] is False
    assert harness.controller.calls == []
    assert sorted(call["url"] for call in harness.neutral_calls) == sorted(NEUTRAL_URLS)


def test_group_contract_answered_with_a_status_never_switches() -> None:
    harness = _ManualHarness(
        accused_error=MihomoDelayStatusMismatch("expected status readback false"),
    )

    result = harness.check()

    neutral = result["diagnostics"]["neutral_connectivity"]
    assert neutral["contract_failure_kinds"] == ["http_status", "none"]
    assert neutral["outcome"] == "current_mixed"
    assert neutral["route_contract_leaf_failure"] is False
    assert neutral["switch_committed"] is False
    assert harness.calls_for("leaf-B") == []
    assert harness.recoveries == []


def test_core_generation_change_during_the_check_is_still_rejected() -> None:
    harness = _ManualHarness(
        accused_error=MihomoDelayTransportFailed("controller HTTP 503 from delay test"),
        moved_core_generation=12,
    )

    result = harness.check()

    neutral = result["diagnostics"]["neutral_connectivity"]
    assert neutral["switch_reason"] == "manual_execution_binding_stale"
    assert neutral["switch_committed"] is False
    assert result["state"] == "data_plane_indeterminate"
    assert harness.calls_for("leaf-B") == []
    assert harness.recoveries == []


def test_group_move_while_the_accused_is_still_alive_is_not_a_native_commit() -> None:
    # §18.7 native_commit requires G3.now == S AND not alive(L): a group that
    # moved while L is still (or again) Core-alive is no demotion of L.
    harness = _ManualHarness(
        accused_error=MihomoDelayTransportFailed("controller HTTP 503 from delay test"),
    )

    def moved_but_alive() -> None:
        if harness.state["snapshot"] is harness.snapshot_b:
            return
        harness.state.update(
            {
                "snapshot": harness.snapshot_b,
                "binding": _route_contract_binding(harness.snapshot_b, _LEAF_B),
                "payload": _route_contract_payload("leaf-B", accused_dead=False),
            }
        )
        harness.runtime._runtime_route_epoch = 9
        harness.runtime._runtime_route_snapshot = harness.snapshot_b
        harness.runtime._runtime_route_snapshot_payload = harness.state["payload"]

    harness.controller.on_accused = moved_but_alive

    result = harness.check()

    neutral = result["diagnostics"]["neutral_connectivity"]
    assert neutral["switch_reason"] == "manual_execution_binding_stale"
    assert neutral["switch_committed"] is False
    assert result["state"] == "data_plane_indeterminate"
    # No successor proof, no health strike on the leaf, nothing written.
    assert harness.calls_for("leaf-B") == []
    assert harness.health == [] and harness.rechecks == []
    assert harness.coordinator.observed == []
    assert harness.recoveries == []


def test_neutral_routed_after_the_demotion_is_not_counted_for_the_leaf() -> None:
    # The neutral CONNECT is not answered within the gate: the URL test runs
    # first, Mihomo demotes the leaf, and the neutral is routed via leaf-B.
    harness = _ManualHarness(
        accused_error=MihomoDelayTransportFailed("controller HTTP 503 from delay test"),
        neutral_delay=0.5,
    )

    result = harness.check()

    assert [call["leaf"] for call in harness.neutral_calls] == ["leaf-B"]
    neutral = result["diagnostics"]["neutral_connectivity"]
    assert neutral["contract_failure_kinds"] == ["connect", "facility"]
    assert neutral["success_count"] == 0
    assert neutral["switch_committed"] is False
    assert "neutral_unattributed" in result["diagnostics"]["probe_indeterminate_reasons"]
    assert harness.calls_for("leaf-B") == []
    assert harness.recoveries == []


def test_late_neutral_still_counts_when_the_group_contract_passed() -> None:
    # A passing URL test leaves Mihomo's pick alone, so a neutral whose
    # CONNECT was answered late was still routed via the leaf.
    harness = _ManualHarness(accused_error=None, neutral_delay=0.5)

    result = harness.check()

    assert [call["leaf"] for call in harness.neutral_calls] == ["leaf-A"]
    assert harness.neutral_calls[0]["at"] > harness.calls_for("leaf-A")[0]["at"]
    neutral = result["diagnostics"]["neutral_connectivity"]
    assert neutral["contract_failure_kinds"] == ["none", "none"]
    assert neutral["outcome"] == "current_healthy"
    assert neutral["route_contract_leaf_failure"] is False
    assert result["state"] == "pending_identity"


def test_black_hole_without_budget_for_the_successor_never_probes_it() -> None:
    # Both current contracts time out; the native successor would get less
    # than the floor timeout, so it is not written dead by a short URL test.
    harness = _ManualHarness(
        accused_error=MihomoDelayTimeout("controller HTTP 504 Timeout"),
        neutral_ok=False,
        budget=(1.9, 1.9),
    )

    result = harness.check()

    neutral = result["diagnostics"]["neutral_connectivity"]
    assert neutral["contract_failure_kinds"] == ["timeout", "timeout"]
    assert neutral["switch_reason"] == "native_fallback_unverified_budget"
    assert neutral["switch_committed"] is False
    assert harness.calls_for("leaf-B") == []
    assert harness.recoveries == []


def test_black_hole_with_a_slow_successor_respects_the_click_deadline() -> None:
    def stall_until_aborted(aborted: threading.Event) -> None:
        aborted.wait(5.0)
        raise TimeoutError("successor URL test aborted")

    harness = _ManualHarness(
        accused_error=MihomoDelayTimeout("controller HTTP 504 Timeout"),
        neutral_ok=False,
        successor_delay=stall_until_aborted,
        budget=(2.0, 3.0),
    )
    started = time.monotonic()

    result = harness.check()

    assert time.monotonic() - started <= 3.0 + 0.75
    neutral = result["diagnostics"]["neutral_connectivity"]
    assert neutral["switch_committed"] is False
    assert neutral["switch_reason"] == "native_fallback_unverified"
    assert neutral["outcome"] == "backup_unavailable"
    assert len(harness.calls_for("leaf-B")) >= 1
    assert harness.recoveries == []


def test_without_a_visible_native_move_the_existing_backup_loop_runs_on_aligned_ids() -> None:
    # Every other member is already Core-dead, so the failed URL test leaves
    # Mihomo on proxies[0] (the leaf itself): no selection change.  Today's
    # backup loop and commit path then run, with the backup pair labelled by
    # the same contracts as the current pair.
    harness = _ManualHarness(
        accused_error=MihomoDelayTransportFailed("controller HTTP 503 from delay test"),
    )
    harness.controller.on_accused = lambda: None
    runtime = harness.runtime
    runtime._runtime_ordered_alternative_refs = lambda *_args, **_kwargs: [_LEAF_B]
    snapshot_a = harness.snapshot_a

    class Coordinator:
        @staticmethod
        def current_route_snapshot() -> CoreLoadedRouteSnapshot:
            return snapshot_a

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
    started: list[Any] = []

    def start_recovery(failure: Any, *_args: Any, **kwargs: Any) -> None:
        started.append((failure.binding.node, kwargs["confirmed_alternative"]))
        with runtime._runtime_failure_monitor_lock:
            runtime._runtime_route_mutation_lease = {
                "incident_id": kwargs["incident_id"],
                "to": _LEAF_B.proxy_name,
                "verified_at_monotonic": time.monotonic(),
            }

    runtime._start_runtime_recovery = start_recovery

    result = harness.check()

    neutral = result["diagnostics"]["neutral_connectivity"]
    assert neutral["route_contract_leaf_failure"] is True
    assert neutral["outcome"] == "switch_before_return"
    assert neutral["switch_reason"] == "replacement_selected_and_verified"
    assert neutral["switch_committed"] is True
    assert started == [(_LEAF_A, _LEAF_B)]
    successor = harness.calls_for("leaf-B")
    assert sorted(call["url"] for call in successor) == sorted(
        [CONTRACT_URL, NEUTRAL_URLS[0]]
    )


def test_route_contract_plan_requires_a_non_gateway_fallback_https_204_contract() -> None:
    runtime = _bare_runtime()
    runtime._default_probe_urls = lambda: list(NEUTRAL_URLS)
    snapshot = _route_contract_snapshot(selected=_LEAF_A, route_epoch=3)
    binding = _route_contract_binding(snapshot, _LEAF_A)

    plan = runtime._manual_route_contract_plan((binding, snapshot, {}))
    assert plan == _ManualRouteContractPlan(
        group="TIER3-AUTO",
        leaf="leaf-A",
        probe_url=CONTRACT_URL,
        expected_status="204",
        neutral_url=NEUTRAL_URLS[0],
        neutral_contract_id="neutral-0",
    )
    assert "leaf-A" not in repr(plan) and "google" not in repr(plan)

    # The contract host equals the first neutral: the second one pairs.
    runtime._default_probe_urls = lambda: [CONTRACT_URL, NEUTRAL_URLS[0]]
    plan = runtime._manual_route_contract_plan((binding, snapshot, {}))
    assert plan is not None
    assert (plan.neutral_url, plan.neutral_contract_id) == (NEUTRAL_URLS[0], "neutral-1")

    runtime._default_probe_urls = lambda: list(NEUTRAL_URLS)
    gateway = RuntimeNodeRef("exit", "gateway", "gw", "leaf-A")
    assert runtime._manual_route_contract_plan(
        (_route_contract_binding(snapshot, gateway), snapshot, {})
    ) is None
    no_contract = CoreLoadedRouteSnapshot(
        process_pid=101,
        process_generation=7,
        core_generation=11,
        semantic_digest="loaded-digest",
        route_epoch=3,
        group_states=(
            CoreLoadedGroupState(
                name="TIER3-AUTO",
                type_name="Fallback",
                members=("leaf-A", "leaf-B"),
                now="leaf-A",
            ),
        ),
        proxy_nodes=(_LEAF_A, _LEAF_B),
    )
    assert runtime._manual_route_contract_plan((binding, no_contract, {})) is None
    status_only = CoreLoadedRouteSnapshot(
        process_pid=101,
        process_generation=7,
        core_generation=11,
        semantic_digest="loaded-digest",
        route_epoch=3,
        group_states=(
            CoreLoadedGroupState(
                name="TIER3-AUTO",
                type_name="Fallback",
                members=("leaf-A", "leaf-B"),
                now="leaf-A",
                test_url=CONTRACT_URL,
                expected_status="200",
            ),
        ),
        proxy_nodes=(_LEAF_A, _LEAF_B),
    )
    assert runtime._manual_route_contract_plan((binding, status_only, {})) is None


def test_controller_slot_errors_are_classified_by_type_not_text() -> None:
    runtime = _bare_runtime()
    runtime._runtime_probe_broker = RuntimeProbeBroker(max_active_sockets=2)
    session = RuntimeManualValidation(total_timeout_seconds=2.0, per_probe_timeout_seconds=1.0)

    def probe(contract_id: str, url: str, timeout: float, context: Any) -> dict[str, Any]:
        del url, timeout
        context.register_abort_callback(lambda: None)
        assert context.mark_socket_started()
        if contract_id == MANUAL_ROUTE_CONTRACT_ID:
            raise MihomoDelayTransportFailed("controller HTTP 503 from delay test")
        raise RuntimeError("HTTP 503 from the neutral endpoint")

    typed = runtime._run_manual_exit_quality_contracts(
        session,
        probe=probe,
        phase_timeout_seconds=1.0,
        contract_urls=(CONTRACT_URL, NEUTRAL_URLS[0]),
        contract_ids=(MANUAL_ROUTE_CONTRACT_ID, "neutral-0"),
        typed_error_slots=(0,),
    )
    assert [result.contract_id for result in typed] == [MANUAL_ROUTE_CONTRACT_ID, "neutral-0"]
    assert typed[0].normalised_failure_kind is ManualProbeFailureKind.CONNECT
    # The text normaliser keeps its meaning for the neutral slot.
    assert typed[1].normalised_failure_kind is ManualProbeFailureKind.HTTP_STATUS

    default = runtime._run_manual_exit_quality_contracts(
        session,
        probe=lambda *_args: {"status": 204},
        phase_timeout_seconds=0.0,
        contract_urls=(CONTRACT_URL, NEUTRAL_URLS[0]),
    )
    assert [result.contract_id for result in default] == ["neutral-0", "neutral-1"]


def test_backup_alignment_keeps_contract_ids_honest() -> None:
    plan = _ManualRouteContractPlan(
        group="TIER3-AUTO",
        leaf="leaf-A",
        probe_url=CONTRACT_URL,
        expected_status="204",
        neutral_url=NEUTRAL_URLS[1],
        neutral_contract_id="neutral-1",
    )
    current = (
        ManualDataPlaneProbeResult.transport_failure(
            MANUAL_ROUTE_CONTRACT_ID,
            ManualProbeFailureKind.CONNECT,
        ),
        ManualDataPlaneProbeResult.https_204("neutral-1"),
    )
    candidate = (
        ManualDataPlaneProbeResult.https_204("neutral-0"),
        ManualDataPlaneProbeResult.https_204("neutral-1"),
    )
    session = RuntimeManualValidation(route_contract_enabled=True)

    # Same group contract and the same neutral URL: the same ids.
    aligned, base, relabelled = DesktopRuntime._manual_route_contract_align_backup(
        plan,
        current,
        candidate,
        (CONTRACT_URL, NEUTRAL_URLS[1]),
        CONTRACT_URL,
    )
    assert relabelled is False and base == current
    assert [result.contract_id for result in aligned] == [
        MANUAL_ROUTE_CONTRACT_ID,
        "neutral-1",
    ]
    assert session.evaluate(base, aligned).switch_before_return is True

    # The default pair: judged on the neutral contract alone, so a
    # route-contract failure alone never switches to such a candidate.
    aligned, base, relabelled = DesktopRuntime._manual_route_contract_align_backup(
        plan,
        current,
        candidate,
        tuple(NEUTRAL_URLS),
        "",
    )
    assert relabelled is True and aligned == candidate
    assert [result.contract_id for result in base] == ["neutral-0", "neutral-1"]
    assert session.evaluate(base, aligned).switch_before_return is False


def test_overview_names_the_route_contract_failure() -> None:
    runtime = _bare_runtime()
    runtime._runtime_uses_early_single_coordinator = lambda: False
    runtime._collect_current_exit_identity = lambda **_kwargs: {
        "state": "data_plane_failed",
        "freshness": {"state": "fresh", "status": "failed"},
        "diagnostics": {
            "neutral_connectivity": {
                "state": "mixed",
                "outcome": "backup_unavailable",
                "route_contract": True,
                "route_contract_leaf_failure": True,
                "contract_failure_kinds": ["connect", "none"],
            }
        },
    }
    runtime._overview_identity_progress = lambda *_args, **_kwargs: None

    result = runtime.check_current_exit_identity(
        proxy_url=None,
        request_id="overview-route-contract",
        timeout_seconds=2.0,
    )

    assert result["diagnostics"]["overview_failure_reason"] == "route_contract_failed"
    assert result["state"] == "data_plane_failed"


def test_neutral_probe_reports_the_answered_connect_before_tls() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def serve() -> None:
        connection, _address = server.accept()
        with connection:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = connection.recv(1024)
                if not chunk:
                    return
                data += chunk
            connection.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            # Close before any TLS answer: the probe then fails in TLS.

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    events: list[str] = []
    try:
        with pytest.raises(Exception):
            DesktopRuntime._abortable_https_204_via_http_proxy(
                proxy_host="127.0.0.1",
                proxy_port=port,
                probe_url=NEUTRAL_URLS[0],
                timeout_seconds=2.0,
                abort_owner=_RuntimeAbortHandle(),
                on_tunnel_established=lambda: events.append("tunnel"),
            )
    finally:
        thread.join(2.0)
        server.close()

    assert events == ["tunnel"]


def test_route_contract_waits_for_the_real_neutral_connect_answer_plus_grace() -> None:
    # Drives the production neutral adapter (the real
    # _probe_mihomo_mixed_port_once_leased and its tunnel-callback forwarding)
    # against a local CONNECT stub that answers late.
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    answered: list[float] = []
    requests: list[bytes] = []

    def serve() -> None:
        connection, _address = server.accept()
        with connection:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = connection.recv(1024)
                if not chunk:
                    return
                data += chunk
            requests.append(data.split(b"\r\n", 1)[0])
            time.sleep(0.10)
            answered.append(time.monotonic())
            connection.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            # Hold the TLS handshake unanswered, then close: the neutral fails
            # in TLS well after the gate (0.30 s) would have expired, so only
            # the forwarded tunnel callback can release the URL test early.
            time.sleep(0.5)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    runtime = _bare_runtime({"client": {"mihomo_mixed_port": port}})
    runtime._runtime_probe_broker = RuntimeProbeBroker(max_active_sockets=2)
    runtime._register_runtime_internal_probe = lambda *_args, **_kwargs: None
    assert (
        runtime._probe_mihomo_mixed_port_once_leased.__func__
        is DesktopRuntime._probe_mihomo_mixed_port_once_leased
    )
    route_calls: list[float] = []

    def route_delay(name: str, urls: list[str], **kwargs: Any) -> dict[str, Any]:
        context = kwargs["execution_context"]
        context.register_abort_callback(lambda: None)
        assert context.mark_socket_started()
        route_calls.append(time.monotonic())
        assert (name, urls, kwargs["expected_status"]) == ("leaf-A", [CONTRACT_URL], "204")
        return {"delay": 40, "expected_status_verified": True}

    runtime._probe_proxy_delay_with_urls_leased = route_delay
    session = RuntimeManualValidation(
        total_timeout_seconds=3.0,
        per_probe_timeout_seconds=2.0,
        route_contract_enabled=True,
    )
    plan = _ManualRouteContractPlan(
        group="TIER3-AUTO",
        leaf="leaf-A",
        probe_url=CONTRACT_URL,
        expected_status="204",
        neutral_url=NEUTRAL_URLS[0],
        neutral_contract_id="neutral-0",
    )
    try:
        route_result, neutral_result = runtime._manual_route_contract_current_contracts(
            session,
            plan,
        )
    finally:
        thread.join(2.0)
        server.close()

    assert requests == [b"CONNECT cp.cloudflare.com:443 HTTP/1.1"]
    assert len(answered) == 1 and len(route_calls) == 1
    # The URL test left only after the CONNECT answer reached the probe,
    # plus the grace (a heuristic ordering, see the constants).
    assert route_calls[0] - answered[0] >= 0.05 - 0.005
    # ... and it was released by that callback, not by the gate timing out
    # (~0.20 s after the answer) or by the neutral ending (~0.5 s after).
    assert route_calls[0] - answered[0] < 0.14
    assert route_result.contract_id == MANUAL_ROUTE_CONTRACT_ID
    assert route_result.is_https_204_success(session.budget)
    # The neutral itself failed in TLS; it was attributed (its tunnel came
    # before the URL test), so it is a transport failure, not "unattributed".
    assert neutral_result.contract_id == "neutral-0"
    assert neutral_result.succeeded is False
    assert neutral_result.probe_indeterminate_reason != "neutral_unattributed"


def test_state_holder_keeps_its_key_out_of_reprs() -> None:
    state = _NodeJudgementRuntimeState()
    assert "PostDialTokenizer(<key>)" == repr(state.tokenizer)
    assert state.tokenizer.leaf("leaf-A") == state.tokenizer.leaf("leaf-A")
    # Stage 2 re-pin: the key is derived from the per-process runtime key
    # (runtime_process_subkey), so the route-view and post-dial observer
    # lines hash a leaf alike within one process; the per-process difference
    # is pinned in tests/test_runtime_post_dial_observer.py.
    assert state.tokenizer.leaf("leaf-A") == _NodeJudgementRuntimeState().tokenizer.leaf("leaf-A")
    assert state.tokenizer.leaf("leaf-A") != PostDialTokenizer(bytes(32)).leaf("leaf-A")
