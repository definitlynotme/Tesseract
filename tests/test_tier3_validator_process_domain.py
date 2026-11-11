from __future__ import annotations

import asyncio
import gc
import hashlib
import inspect
import logging
import multiprocessing
import os
import queue
import shutil
import sys
import socketserver
import tempfile
import threading
import time
import types
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

import yaml
import pytest

from alerts import AlertManager
from manager import ProxyTierManager
from models import Node, NodeStatus
from test_support.tier3_uncooperative_helper import uncooperative_helper_bootstrap
from tester import _build_mihomo_direct_config, _mihomo_direct_validate
from test_support.tier3_deterministic_helper import (
    deterministic_helper_bootstrap,
    deterministic_no_child_bootstrap,
)
from test_support.tier3_late_terminal_helper import late_terminal_helper_bootstrap
from test_support.tier3_slow_start_helper import slow_progress_helper_bootstrap
from tier3_validation_domain import (
    Tier3ValidationDomain,
    calculate_validation_fast_ingress_window,
    process_validator_settings,
    tier3_candidate_config_digest,
)
from tier3_candidate_scheduler import Tier3CandidateDispatchTicket
from tier3_validation_feedback import (
    Tier3CandidateDispatchContext,
    Tier3ValidationFeedbackKind,
)
from tier3_endpoint_controller import Tier3EndpointController
from tier3_expansion_actor import Tier3ExpansionActor
from tier3_validation_protocol import (
    AdmissionCommitted,
    AdmissionRejected,
    CancelCmd,
    CandidateEnvelope,
    EndpointAttemptKind,
    GenerationGone,
    Tier3WorkerPhase,
    Tier3TerminalKind,
    ProbeAttemptFact,
    TIER3_HELPER_PREWARM_READY_TIMEOUT_SECONDS,
    TIER3_HELPER_START_TIMEOUT_SECONDS,
    TIER3_HELPER_BOOTSTRAP_TIMEOUT_SECONDS,
    EndpointAttemptResult,
    Tier3ValidationPurpose,
    ValidationStage,
    ValidationTerminal,
    ValidateEndpointCmd,
    canonical_json,
    dataclass_payload,
    digest_json,
    message_bytes,
    parse_message,
    validate_endpoint_cmd_from_payload,
)
from tier3_validation_parity_audit import (
    _build_parity_supervisor,
    _parity_helper_command,
    _run_helper_attempt,
    _wait_for_helpers,
)
from tier3_validator_supervisor import (
    Tier3ValidatorWatchdog,
    Tier3ValidatorSupervisor,
    _WorkerGeneration,
    _process_is_alive,
    _process_sentinel,
    _terminate_jobs_bounded,
)
from tier3_validator_helper import helper_process_bootstrap
from tier3_validator_helper import BOOTSTRAP_READY_MESSAGE
from tier3_validator_runtime import ValidatorHelperRuntime
from tier3_windows_job import WindowsJob


@pytest.fixture(scope="module", autouse=True)
def dispose_finished_qt_test_objects_before_native_timing():
    # processEvents() in earlier Qt tests does not deliver DeferredDelete.
    # Dispose only objects already scheduled for deletion, not live windows.
    gc.collect()
    qt_core = sys.modules.get("PySide6.QtCore")
    if qt_core is not None and qt_core.QCoreApplication.instance() is not None:
        qt_core.QCoreApplication.sendPostedEvents(
            None,
            qt_core.QEvent.Type.DeferredDelete,
        )
    gc.collect()


def test_process_domain_publishes_worker_envelope_when_credit_limits_are_stable() -> None:
    domain = object.__new__(Tier3ValidationDomain)
    domain._desired_workers = 16
    domain._pressure = 1.0
    domain._resident_limit_override = 160
    domain._ready_limit_override = 32
    domain._actor_owner_limit = 160
    domain._ready_work_limit = 32
    domain._actor_worker_envelope = 4
    domain._ingress_metrics_lock = threading.Lock()
    domain._ingress_event = threading.Event()
    published_envelopes: list[int] = []
    published_credits: list[tuple[int, int]] = []
    domain.actor = types.SimpleNamespace(
        set_worker_envelope=published_envelopes.append,
        set_credit_limits=lambda owner, ready: published_credits.append(
            (owner, ready)
        ),
    )
    domain.supervisor = types.SimpleNamespace(max_workers=16)

    limits = domain._sync_active_worker_limits(
        types.SimpleNamespace(active_workers=4)
    )

    assert limits == (160, 32)
    assert published_envelopes == [16]
    assert published_credits == []
    assert domain._ingress_event.is_set()


ROOT = Path(__file__).resolve().parents[1]
MIHOMO = ROOT / "bin" / "mihomo.exe"


def test_packaged_parity_supervisor_uses_current_constructor_contract() -> None:
    results: queue.Queue[EndpointAttemptResult] = queue.Queue()
    supervisor = _build_parity_supervisor(
        mihomo_binary_path=MIHOMO,
        endpoints=("https://probe.example/generate_204",),
        validator={
            "expected_statuses": {204},
            "probe_timeout_seconds": 5.0,
            "ipv6": False,
        },
        testing={"slow_threshold_ms": 3000},
        results=results,
    )

    assert isinstance(supervisor, Tier3ValidatorSupervisor)
    assert supervisor.min_workers == 1
    assert supervisor.max_workers == 1


def test_validator_prewarm_rejects_unmatched_traffic() -> None:
    runtime = object.__new__(ValidatorHelperRuntime)
    runtime.mixed_port = 17890
    runtime.controller_port = 19090
    runtime.ipv6 = True
    runtime._dns_block = None

    config = runtime._base_config()

    assert config["rules"] == ["MATCH,REJECT"]
    assert config["proxies"] == []
    assert config["proxy-groups"] == []
    assert config["ipv6"] is True


def test_helper_start_timeout_covers_cold_mihomo_prewarm() -> None:
    assert TIER3_HELPER_PREWARM_READY_TIMEOUT_SECONDS >= 8.0
    assert (
        TIER3_HELPER_START_TIMEOUT_SECONDS
        >= TIER3_HELPER_PREWARM_READY_TIMEOUT_SECONDS + 2.0
    )

    supervisor = Tier3ValidatorSupervisor(
        binary_path="unused-by-contract-test",
        probe_urls=["http://127.0.0.1/never"],
        expected_statuses={204},
        probe_timeout_seconds=5.0,
        slow_threshold_ms=3000,
        attempt_sink=lambda _result: None,
        capacity_sink=lambda _count: None,
        min_workers=1,
        max_workers=1,
    )

    assert supervisor.start_timeout_seconds == TIER3_HELPER_START_TIMEOUT_SECONDS
    assert TIER3_HELPER_BOOTSTRAP_TIMEOUT_SECONDS > supervisor.start_timeout_seconds


def test_helper_start_progress_extends_each_bounded_startup_stage() -> None:
    supervisor = Tier3ValidatorSupervisor(
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        attempt_sink=lambda _result: None,
        capacity_sink=lambda _count: None,
        min_workers=1,
        max_workers=1,
        # Three 700 ms fixture stages still exceed one lease, while this
        # leaves enough Windows process-spawn headroom to test progress
        # renewal instead of scheduler jitter.
        start_timeout_seconds=1.5,
        helper_target=slow_progress_helper_bootstrap,
    )
    supervisor.start()
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if supervisor.snapshot().active_workers == 1:
                break
            time.sleep(0.02)
        snapshot = supervisor.snapshot()
        assert snapshot.active_workers == 1, (
            snapshot.last_failure_reason,
            snapshot.startup_failure_count,
            snapshot.replacement_count,
            snapshot.forced_job_termination_count,
            snapshot.disabled_worker_count,
            snapshot.stop_status,
            snapshot.workers,
        )
        assert snapshot.startup_failure_count == 0
        assert snapshot.replacement_count == 0
        assert snapshot.forced_job_termination_count == 0
        assert snapshot.workers[0]["startup_phase"] == "ready"
        assert snapshot.workers[0]["mixed_port"] > 0
        assert snapshot.workers[0]["controller_port"] > 0
        assert (
            snapshot.workers[0]["mixed_port"]
            != snapshot.workers[0]["controller_port"]
        )
    finally:
        assert supervisor.stop(timeout_seconds=3.0) == "STOPPED"


def test_supervisor_assigns_unique_ports_to_concurrent_generations() -> None:
    supervisor = Tier3ValidatorSupervisor(
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        attempt_sink=lambda _result: None,
        capacity_sink=lambda _count: None,
        min_workers=4,
        max_workers=4,
        helper_target=deterministic_no_child_bootstrap,
    )
    supervisor.start()
    try:
        _wait_until(lambda: supervisor.snapshot().active_workers == 4, timeout=8.0)
        snapshot = supervisor.snapshot()
        ports = [
            int(worker[key])
            for worker in snapshot.workers
            for key in ("mixed_port", "controller_port")
        ]
        assert len(ports) == 8
        assert all(port > 0 for port in ports)
        assert len(set(ports)) == len(ports)
    finally:
        assert supervisor.stop(timeout_seconds=3.0) == "STOPPED"


def test_supervisor_publishes_each_ready_helper_capacity() -> None:
    capacities: list[int] = []
    supervisor = Tier3ValidatorSupervisor(
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        attempt_sink=lambda _result: None,
        capacity_sink=capacities.append,
        min_workers=4,
        max_workers=4,
        helper_target=deterministic_no_child_bootstrap,
    )
    supervisor.start()
    try:
        _wait_until(lambda: supervisor.snapshot().active_workers == 4, timeout=8.0)
        snapshot = supervisor.snapshot()
        assert capacities
        assert all(item == 1 for item in capacities)
        assert sum(capacities) == snapshot.active_workers
        lease = supervisor.reserve_capacity()
        assert lease is not None
        supervisor.release_capacity(lease)
    finally:
        assert supervisor.stop(timeout_seconds=3.0) == "STOPPED"

def test_helper_runtime_honors_supervisor_port_lease() -> None:
    class _Connection:
        def send_bytes(self, _payload) -> None:
            return None

    runtime = ValidatorHelperRuntime(
        _Connection(),
        {
            "worker_id": 1,
            "worker_generation": 2,
            "binary_path": "fixture",
            "probe_urls": ["http://fixture/probe"],
            "expected_statuses": [204],
            "mixed_port": 17891,
            "controller_port": 19091,
        },
    )
    try:
        assert runtime.mixed_port == 17891
        assert runtime.controller_port == 19091
    finally:
        shutil.rmtree(runtime.work_dir, ignore_errors=True)


def test_supervisor_assigns_ready_capacity_before_starting_more_helpers() -> None:
    source = inspect.getsource(Tier3ValidatorSupervisor._run)
    assert source.index("self._assign_pending()") < source.index(
        "self._start_needed_workers()"
    )


def test_endpoint_protocol_rejects_missing_or_unknown_stage() -> None:
    base = {
        "refresh_epoch": "epoch",
        "candidate_digest": "a" * 64,
        "config_digest": "b" * 64,
        "lease_id": "lease",
        "sequence": 1,
        "node_json": "{}",
        "source_root": "root",
        "source_family": "family",
        "endpoint": "http://fixture.invalid/generate_204",
    }

    for invalid_stage in (None, "FAST_HTTP", "unknown"):
        payload = dict(base)
        if invalid_stage is not None:
            payload["stage"] = invalid_stage
        try:
            validate_endpoint_cmd_from_payload(payload)
        except ValueError:
            continue
        raise AssertionError(f"invalid stage was accepted: {invalid_stage!r}")

    protocol_source = (ROOT / "tier3_validation_protocol.py").read_text(
        encoding="utf-8"
    )
    assert "class ValidateCmd" not in protocol_source
    assert "class ValidateSliceCmd" not in protocol_source
    assert "FAST_HTTP" not in protocol_source
    assert not hasattr(ValidatorHelperRuntime, "validate")
    assert not hasattr(ValidatorHelperRuntime, "validate_slice")
    supervisor_source = (ROOT / "tier3_validator_supervisor.py").read_text(
        encoding="utf-8"
    )
    assert "slice_yield" not in supervisor_source
    assert "terminal_sink" not in supervisor_source
    assert 'kind == "terminal"' not in supervisor_source


def test_fast_ingress_window_counts_tcp_and_unconsumed_actor_events() -> None:
    window = calculate_validation_fast_ingress_window(
        active_workers=16,
        pressure=1.0,
        tcp_selected=40,
        tcp_completed=16,
        validation_accepted=50,
        actor_submitted=45,
        actor_fast_queued=20,
        actor_fast_active=5,
    )

    assert window.limit == 32
    assert window.tcp_pending == 24
    assert window.actor_ingress_pending == 5
    assert window.actor_fast_pending == 25
    assert window.actor_deep_pending == 0
    assert window.actor_transaction_pending == 30
    assert window.used == 30
    assert window.credit == 2
    assert not window.saturated


def test_helper_and_legacy_oracle_share_single_proxy_config() -> None:
    class _Connection:
        def send_bytes(self, _payload) -> None:
            return None

    node = Node(
        name="shared-config",
        protocol="http",
        address="127.0.0.1",
        port=8080,
        tier="tier3",
    )
    runtime = ValidatorHelperRuntime(
        _Connection(),
        {
            "worker_id": 1,
            "worker_generation": 1,
            "binary_path": "fixture",
            "probe_urls": ["http://probe.example/204"],
            "expected_statuses": [204],
            "probe_timeout_seconds": 5.0,
            "slow_threshold_ms": 3000,
        },
    )
    try:
        helper_config = runtime._candidate_config(node)
        oracle_config = _build_mihomo_direct_config(
            node,
            runtime.mixed_port,
            logging.getLogger("shared-config"),
        )
        assert helper_config is not None
        assert oracle_config is not None
        helper_config = dict(helper_config)
        helper_config.pop("external-controller", None)
        assert helper_config == oracle_config
        assert helper_config["proxy-groups"] == [
            {
                "name": "TEST-DIRECT",
                "type": "select",
                "empty-fallback": "REJECT",
                "proxies": ["TEST-PROXY"],
            }
        ]
        assert helper_config["rules"] == ["MATCH,TEST-DIRECT"]
        assert "MATCH,REJECT" not in helper_config["rules"]
    finally:
        shutil.rmtree(runtime.work_dir, ignore_errors=True)


def test_helper_creates_missing_validation_temp_parent() -> None:
    class _Connection:
        def send_bytes(self, _payload) -> None:
            return None

    with tempfile.TemporaryDirectory() as root:
        parent = Path(root) / "mihomo" / "validation"
        runtime = ValidatorHelperRuntime(
            _Connection(),
            {
                "worker_id": 1,
                "worker_generation": 1,
                "binary_path": "fixture",
                "probe_urls": ["http://probe.example/204"],
                "expected_statuses": [204],
                "probe_timeout_seconds": 5.0,
                "slow_threshold_ms": 3000,
                "temp_parent": str(parent),
            },
        )
        try:
            assert parent.is_dir()
            assert runtime.work_dir.parent == parent
        finally:
            asyncio.run(runtime.close())


def test_helper_close_accepts_mihomo_already_removed_by_job() -> None:
    class _Connection:
        def send_bytes(self, _payload) -> None:
            return None

    class _RacedProcess:
        returncode = None

        def terminate(self) -> None:
            raise ProcessLookupError

        async def wait(self) -> int:
            return 0

    temp_parent = ROOT / ".build" / "validator-helper-close-fixture"
    temp_parent.mkdir(parents=True, exist_ok=True)
    work_dir = temp_parent / "runtime"
    work_dir.mkdir(exist_ok=True)
    with patch(
        "tier3_validator_runtime.tempfile.mkdtemp",
        return_value=str(work_dir),
    ):
        runtime = ValidatorHelperRuntime(
            _Connection(),
            {
                "worker_id": 1,
                "worker_generation": 1,
                "binary_path": "fixture",
                "probe_urls": ["http://probe.example/204"],
                "expected_statuses": [204],
                "probe_timeout_seconds": 5.0,
                "slow_threshold_ms": 3000,
                "temp_parent": str(temp_parent),
            },
        )
    runtime.process = _RacedProcess()

    asyncio.run(runtime.close())

    assert runtime.process is None
    assert not work_dir.exists()


def test_validation_domain_creates_temp_parent_before_supervisor_start() -> None:
    with tempfile.TemporaryDirectory() as root:
        parent = Path(root) / "mihomo" / "validation"
        domain = Tier3ValidationDomain(
            refresh_epoch="missing-temp-parent",
            binary_path="fixture",
            probe_urls=["http://fixture/probe"],
            expected_statuses={204},
            probe_timeout_seconds=1.0,
            slow_threshold_ms=3000,
            commit_node=lambda _node, _report, intent: AdmissionRejected(
                refresh_epoch=intent.refresh_epoch,
                candidate_digest=intent.candidate_digest,
                lease_id=intent.lease_id,
                sequence=intent.sequence,
                reason="not_expected",
            ),
            terminal_observer=lambda _node, _terminal: None,
            min_workers=1,
            max_workers=1,
            temp_parent=str(parent),
            helper_target=deterministic_helper_bootstrap,
        )
        try:
            assert parent.is_dir()
            assert domain.supervisor.temp_parent == str(parent)
            assert domain.start() is True
            _wait_until(lambda: domain.snapshot().supervisor.idle_workers == 1)
        finally:
            assert domain.stop(timeout_seconds=3.0) == "STOPPED"


def test_validation_domain_prewarm_requires_callbacks_before_submit_and_start() -> None:
    domain = Tier3ValidationDomain(
        refresh_epoch="helper-only-prewarm",
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        commit_node=None,
        terminal_observer=None,
        min_workers=1,
        max_workers=1,
        helper_target=deterministic_no_child_bootstrap,
    )
    candidate = Node(
        name="prewarm-candidate",
        protocol="http",
        address="127.0.0.1",
        port=8080,
        tier="tier3",
    )
    try:
        assert domain.prewarm() is True
        _wait_until(lambda: domain.snapshot().supervisor.active_workers == 1)
        assert domain.snapshot().stop_status == "prewarmed"
        assert domain.submit(candidate, {"configured_source_count": 1}) is False
        try:
            domain.start()
        except RuntimeError as exc:
            assert str(exc) == "validation callbacks are not bound"
        else:
            raise AssertionError("unbound prewarmed domain must not start")

        domain.bind_callbacks(
            commit_node=lambda _node, _report, intent: AdmissionRejected(
                refresh_epoch=intent.refresh_epoch,
                candidate_digest=intent.candidate_digest,
                lease_id=intent.lease_id,
                sequence=intent.sequence,
                reason="not_expected",
            ),
            terminal_observer=lambda _node, _terminal: None,
        )
        assert domain.start() is True
        assert domain.submit(candidate, {"configured_source_count": 1}) is True
    finally:
        assert domain.stop(timeout_seconds=3.0) == "STOPPED"


def test_validation_domain_unbound_prewarm_stops_without_starting_business_threads() -> None:
    domain = Tier3ValidationDomain(
        refresh_epoch="cancelled-helper-only-prewarm",
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        commit_node=None,
        terminal_observer=None,
        min_workers=1,
        max_workers=1,
        helper_target=deterministic_no_child_bootstrap,
    )

    assert domain.prewarm() is True
    _wait_until(lambda: domain.snapshot().supervisor.active_workers == 1)
    assert domain.stop(timeout_seconds=3.0) == "STOPPED"
    snapshot = domain.snapshot()
    assert snapshot.supervisor.active_workers == 0
    assert snapshot.commit_pending == 0
    assert snapshot.ingress_depth == 0


def test_process_domain_submit_is_not_blocked_by_actor_ingress_backpressure() -> None:
    domain = Tier3ValidationDomain(
        refresh_epoch="ingress-lock-scope",
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        commit_node=lambda _node, _report, intent: AdmissionRejected(
            refresh_epoch=intent.refresh_epoch,
            candidate_digest=intent.candidate_digest,
            lease_id=intent.lease_id,
            sequence=intent.sequence,
            reason="not_expected",
        ),
        terminal_observer=lambda _node, _terminal: None,
        min_workers=1,
        max_workers=1,
        helper_target=deterministic_helper_bootstrap,
    )
    actor_entered = threading.Event()
    actor_release = threading.Event()

    class _BlockingActor:
        def submit(self, _envelope) -> bool:
            actor_entered.set()
            actor_release.wait(1.0)
            return True

        @staticmethod
        def pending_work() -> int:
            return 0

    domain.actor = _BlockingActor()
    domain._transaction_window = lambda: (8, 0)
    domain._ingress_accepting.set()
    first = Node(
        name="first",
        protocol="http",
        address="127.0.0.1",
        port=8001,
        tier="tier3",
    )
    second = Node(
        name="second",
        protocol="http",
        address="127.0.0.2",
        port=8002,
        tier="tier3",
    )
    assert domain.submit(first, {"configured_source_count": 1})
    worker = threading.Thread(target=domain._ingress_loop)
    worker.start()
    try:
        assert actor_entered.wait(0.5)
        assert domain.pending_work() == 1
        started = time.perf_counter()
        assert domain.submit(second, {"configured_source_count": 1})
        assert time.perf_counter() - started < 0.1
    finally:
        actor_release.set()
        domain._ingress_stop.set()
        domain._ingress_event.set()
        worker.join(1.0)
        assert not worker.is_alive()


@pytest.mark.parametrize("unit_family", ["default", "aggregate"])
@pytest.mark.parametrize("with_context", [False, True])
def test_process_domain_preserves_protocol_fairness_for_aggregate_units(
    unit_family: str, with_context: bool,
) -> None:
    domain = Tier3ValidationDomain(
        refresh_epoch="aggregate-protocol-fairness",
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        commit_node=lambda *_args: None,
        terminal_observer=lambda *_args: None,
        min_workers=1,
        max_workers=1,
        helper_target=deterministic_helper_bootstrap,
    )
    submitted = []
    for index, protocol in enumerate(["http"] * 32 + ["socks5"]):
        node = Node(
            name=f"candidate-{index}", protocol=protocol,
            address="127.0.0.1", port=8000 + index, tier="tier3",
            source="opaque-root",
            extra={
                "tier3_source_unit_family": unit_family,
                "tier3_source_root_id": "opaque-root",
                "tier3_source_coarse_key": "coarse-original",
            },
        )
        context = None
        if with_context:
            context = Tier3CandidateDispatchContext(
                ticket=Tier3CandidateDispatchTicket(
                    scheduler_generation=1, ticket_id=index + 1,
                    arm_key=f"arm-{protocol}", source_root="opaque-root",
                    family=protocol, coarse_key="coarse-original",
                    selection_reason="ordinary", dispatched_monotonic_ns=1,
                ),
                node_identity=f"candidate-{index}",
            )
            node.extra["tier3_source_root_id"] = f"changed-{index}"
            node.extra["tier3_source_coarse_key"] = "changed-coarse"
        assert domain.submit(node, {}, dispatch_context=context)
        submitted.append(domain.candidate_digest_for_node(node))

    envelopes = domain._ingress.drain()
    assert envelopes[1].candidate_digest == submitted[-1]
    assert {item.source_family for item in envelopes} == {"http", "socks5"}
    assert {item.source_root for item in envelopes} == {"opaque-root"}
    assert {item.source_coarse for item in envelopes} == {"coarse-original"}
    assert __import__("json").loads(envelopes[0].node_json)["extra"][
        "tier3_source_unit_family"
    ] == unit_family


def test_process_domain_ipc_report_is_compact_and_preserves_commit_counts() -> None:
    domain = Tier3ValidationDomain(
        refresh_epoch="compact-report",
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        commit_node=lambda _node, _report, intent: AdmissionRejected(
            refresh_epoch=intent.refresh_epoch,
            candidate_digest=intent.candidate_digest,
            lease_id=intent.lease_id,
            sequence=intent.sequence,
            reason="not_expected",
        ),
        terminal_observer=lambda _node, _terminal: None,
        min_workers=1,
        max_workers=1,
        helper_target=deterministic_helper_bootstrap,
    )
    report = {
        "configured_source_count": 5,
        "fetched_sources": 4,
        "failed_sources": 1,
        "node_count": 20_381,
        "sources": [{"units": [{"detail": "x" * 4096}]}] * 5,
        "candidate_scheduler_backlog_count": 12_000,
    }
    assert domain.submit(
        Node(
            name="compact",
            protocol="http",
            address="127.0.0.1",
            port=8080,
            tier="tier3",
        ),
        report,
    )
    envelope = domain._ingress.pop()
    assert envelope is not None
    payload = __import__("json").loads(envelope.report_json)
    assert payload == {
        "configured_source_count": 5,
        "failed_sources": 1,
        "fetched_sources": 4,
        "node_count": 20_381,
    }


def test_watchdog_never_retires_supervisor_owned_deadline() -> None:
    class _Supervisor:
        def __init__(self) -> None:
            self.aborts: list[tuple[int, int, str]] = []
            self.wakes = 0
            self.phase = "cancelling"

        def snapshot(self):
            return types.SimpleNamespace(
                idle_workers=0,
                validating_workers=1,
                last_completion_monotonic=0.0,
                workers=(
                    {
                        "worker_id": 1,
                        "generation": 2,
                        "phase": self.phase,
                        "deadline_at": 1.0,
                    },
                ),
            )

        def wake(self) -> None:
            self.wakes += 1

        def request_abort(self, worker_id: int, generation: int, reason: str) -> None:
            self.aborts.append((worker_id, generation, reason))

    supervisor = _Supervisor()
    watchdog = Tier3ValidatorWatchdog(
        supervisor,
        backlog_depth=lambda: 1,
        max_completion_gap_seconds=20.0,
    )
    watchdog._inspect_once(now=2.0)
    assert supervisor.aborts == []
    supervisor.phase = "validating"
    watchdog._inspect_once(now=2.0)
    assert supervisor.aborts == []
    assert supervisor.wakes == 1


def test_user_pool_health_pass_has_no_admission_or_auto_lane_feedback() -> None:
    terminals: list[ValidationTerminal] = []
    commits = []
    domain = Tier3ValidationDomain(
        refresh_epoch="user-pool-health",
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        commit_node=lambda node, _report, _intent: commits.append(node),
        terminal_observer=lambda _node, terminal: terminals.append(terminal),
        min_workers=1,
        max_workers=1,
        helper_target=deterministic_helper_bootstrap,
    )
    try:
        assert domain.start() is True
        node = Node(
            name="manual-health",
            protocol="http",
            address="127.0.0.1",
            port=8080,
            tier="tier3",
            extra={
                "tier3_origin": "tier3a",
                "fixture_validation_outcome": "pass",
            },
        )
        assert domain.submit(
            node,
            {"fixture_validation_outcome": "pass"},
            purpose=Tier3ValidationPurpose.USER_POOL_HEALTH,
            transaction_nonce="manual-health-1",
        )
        _wait_until(lambda: len(terminals) == 1)

        snapshot = domain.snapshot().actor
        assert terminals[0].purpose is Tier3ValidationPurpose.USER_POOL_HEALTH
        assert terminals[0].kind is Tier3TerminalKind.PASS
        assert commits == []
        assert snapshot.admission_intents == 0
        assert snapshot.fast_completed == 0
        assert snapshot.fast_passed == 0
        assert snapshot.fast_rolling_completions == 0
    finally:
        assert domain.stop(timeout_seconds=3.0) == "STOPPED"


def test_bootstrap_error_uses_current_schema_and_preserves_exception_type() -> None:
    class _Connection:
        def __init__(self) -> None:
            self.messages: list[bytes] = []
            self.closed = False

        def recv_bytes(self) -> bytes:
            # A valid START envelope reaches the runtime constructor, where the
            # absent worker identity raises the real bootstrap exception.
            return message_bytes("start", {})

        def send_bytes(self, payload: bytes) -> None:
            self.messages.append(payload)

        def close(self) -> None:
            self.closed = True

    connection = _Connection()
    try:
        helper_process_bootstrap(connection)
    except KeyError as exc:
        assert exc.args == ("worker_id",)
    else:
        raise AssertionError("invalid START payload did not fail bootstrap")

    assert connection.closed is True
    assert connection.messages[0] == BOOTSTRAP_READY_MESSAGE
    kind, payload = parse_message(connection.messages[-1])
    assert kind == "bootstrap_error"
    assert payload["error_type"] == "KeyError"
    assert "worker_id" in payload["error"]


def test_ready_window_is_stage_neutral_across_fast_to_deep_continuation() -> None:
    fast = calculate_validation_fast_ingress_window(
        active_workers=8,
        pressure=0.0,
        tcp_selected=100,
        tcp_completed=96,
        validation_accepted=12,
        actor_submitted=12,
        actor_fast_queued=8,
        actor_fast_active=4,
    )
    deep = calculate_validation_fast_ingress_window(
        active_workers=8,
        pressure=0.0,
        tcp_selected=100,
        tcp_completed=96,
        validation_accepted=12,
        actor_submitted=12,
        actor_fast_queued=0,
        actor_fast_active=0,
        actor_deep_queued=8,
        actor_deep_active=4,
    )

    assert fast.limit == deep.limit == 8
    assert fast.used == deep.used == 8
    assert fast.credit == deep.credit == 0
    assert deep.actor_transaction_pending == 12
    assert deep.saturated


def test_process_domain_keeps_excess_transactions_in_durable_ingress() -> None:
    domain = Tier3ValidationDomain(
        refresh_epoch="transaction-window-domain",
        binary_path="fixture",
        probe_urls=["http://fixture/probe-a", "https://fixture/probe-b"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        commit_node=lambda _node, _report, intent: AdmissionRejected(
            refresh_epoch=intent.refresh_epoch,
            candidate_digest=intent.candidate_digest,
            lease_id=intent.lease_id,
            sequence=intent.sequence,
            reason="not_expected",
        ),
        terminal_observer=lambda _node, _terminal: None,
        min_workers=1,
        max_workers=1,
        helper_target=deterministic_helper_bootstrap,
    )
    domain.start()
    try:
        _wait_until(lambda: domain.snapshot().supervisor.active_workers == 1)
        for index in range(80):
            assert domain.submit(
                Node(
                    name=f"window-{index}",
                    protocol="http",
                    address=f"10.40.0.{index + 1}",
                    port=8000 + index,
                    source="window-root",
                    tier="tier3",
                    extra={
                        "tier3_source_root_id": "window-root",
                        "tier3_source_unit_family": "http",
                    },
                ),
                {
                    "configured_source_count": 1,
                    "fixture_delay_seconds": 0.2,
                },
            )

        _wait_until(
            lambda: (
                domain.snapshot().accepted_transactions == 80
                and domain.snapshot().actor_submitted_transactions == 80
            )
        )
        snapshot = domain.snapshot()
        assert snapshot.actor_owner_limit == 10
        assert snapshot.actor_owner_used <= 8
        assert snapshot.ready_work_limit == 2
        assert snapshot.ready_work_used <= 2
        assert snapshot.ingress_depth == 0
        assert snapshot.transaction_conservation_ok
        # The actor inbox also carries capacity/control events, so its depth is
        # only an upper bound for candidate events that have not reached the
        # processed counter. Candidate conservation is asserted independently
        # by ``transaction_conservation_ok`` above.
        assert snapshot.actor.submitted <= snapshot.actor_submitted_transactions
        assert (
            snapshot.actor_submitted_transactions - snapshot.actor.submitted
            <= snapshot.actor.inbox_depth
        )
        _wait_until(
            lambda: (
                domain.snapshot().actor.submitted == 80
                and domain.snapshot().actor.inbox_depth == 0
            )
        )
        snapshot = domain.snapshot()
        assert snapshot.actor.inbox_depth == 0
        assert snapshot.actor.parked_fast > 0

        # FAST -> canonical keeps lightweight actor ownership while releasing
        # worker-ready credit for another FAST candidate.
        _wait_until(lambda: domain.snapshot().actor.fast_continued >= 1, timeout=4.0)
        continued = domain.snapshot()
        assert continued.actor_owner_used <= continued.actor_owner_limit
        assert continued.ready_work_used <= continued.ready_work_limit
        assert continued.actor_submitted_transactions == 80
        assert continued.transaction_conservation_ok
        assert continued.actor.controller_decision_count >= 1
        assert continued.actor.controller_dispatch_mismatch_count == 0
        assert (
            continued.endpoint_health["_policy"]["fast_entitlement_count"]
            >= 1
        )
    finally:
        assert domain.stop(timeout_seconds=4.0) == "STOPPED", domain.snapshot()


def test_process_domain_owns_dispatch_context_through_terminal_commit_race() -> None:
    domain = Tier3ValidationDomain(
        refresh_epoch="dispatch-context-domain",
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        commit_node=lambda _node, _report, intent: AdmissionRejected(
            refresh_epoch=intent.refresh_epoch,
            candidate_digest=intent.candidate_digest,
            lease_id=intent.lease_id,
            sequence=intent.sequence,
            reason="not_expected",
        ),
        terminal_observer=lambda _node, _terminal: None,
        min_workers=1,
        max_workers=1,
        helper_target=deterministic_helper_bootstrap,
    )
    node = Node(
        name="context-owned",
        protocol="http",
        address="10.60.0.1",
        port=8080,
        source="root-a",
        tier="tier3",
    )
    ticket = Tier3CandidateDispatchTicket(
        scheduler_generation=1,
        ticket_id=7,
        arm_key="arm-a",
        source_root="root-a",
        family="http",
        coarse_key="coarse-a",
        selection_reason="coverage",
        dispatched_monotonic_ns=time.monotonic_ns(),
    )
    context = Tier3CandidateDispatchContext(
        ticket=ticket,
        node_identity="http:10.60.0.1:8080",
    )

    assert domain.submit(node, {}, dispatch_context=context)
    submitted_envelope = domain._ingress.pop()
    assert submitted_envelope is not None
    assert submitted_envelope.dispatch_selection_reason == "coverage"
    candidate_digest = domain.candidate_digest_for_node(node)
    assert domain.dispatch_context_for_attempt(candidate_digest) == context
    assert domain.dispatch_context_for_attempt(candidate_digest) is None

    # Admission may race ahead of the terminal observer. Both observers see
    # the same immutable context and the domain releases it only afterwards.
    assert domain.dispatch_context_for_admission(candidate_digest) == context
    assert domain.dispatch_context_for_terminal(
        candidate_digest,
        passed=True,
    ) == context
    assert domain.dispatch_context_count() == 0

    duplicate_ticket = Tier3CandidateDispatchTicket(
        scheduler_generation=1,
        ticket_id=8,
        arm_key="arm-a",
        source_root="root-a",
        family="http",
        coarse_key="coarse-a",
        selection_reason="coverage",
        dispatched_monotonic_ns=time.monotonic_ns(),
    )
    assert not domain.submit(
        node,
        {},
        dispatch_context=Tier3CandidateDispatchContext(
            ticket=duplicate_ticket,
            node_identity="http:10.60.0.1:8080",
        ),
    )
    assert domain.snapshot().duplicate_submission_count == 1


def test_process_domain_dispatch_context_conserves_one_thousand_mixed_transactions() -> None:
    domain = Tier3ValidationDomain(
        refresh_epoch="dispatch-context-property",
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        commit_node=lambda _node, _report, intent: AdmissionRejected(
            refresh_epoch=intent.refresh_epoch,
            candidate_digest=intent.candidate_digest,
            lease_id=intent.lease_id,
            sequence=intent.sequence,
            reason="not_expected",
        ),
        terminal_observer=lambda _node, _terminal: None,
        min_workers=1,
        max_workers=1,
        helper_target=deterministic_helper_bootstrap,
    )

    for index in range(1000):
        node = Node(
            name=f"context-{index}",
            protocol="http",
            address=f"10.61.{index // 250}.{index % 250 + 1}",
            port=8080,
            source="root-a",
            tier="tier3",
        )
        context = Tier3CandidateDispatchContext(
            ticket=Tier3CandidateDispatchTicket(
                scheduler_generation=1,
                ticket_id=index + 1,
                arm_key=f"arm-{index % 7}",
                source_root=f"root-{index % 5}",
                family="http",
                coarse_key=f"coarse-{index % 11}",
                selection_reason="coverage",
                dispatched_monotonic_ns=time.monotonic_ns(),
            ),
            node_identity=f"http:{node.address}:8080",
        )
        assert domain.submit(node, {}, dispatch_context=context)
        digest = domain.candidate_digest_for_node(node)
        if index % 3 == 0:
            assert domain.dispatch_context_for_attempt(digest) == context
        if index % 5 == 0:
            if index % 2:
                assert domain.dispatch_context_for_admission(digest) == context
                assert domain.dispatch_context_for_terminal(
                    digest, passed=True
                ) == context
            else:
                assert domain.dispatch_context_for_terminal(
                    digest, passed=True
                ) == context
                assert domain.dispatch_context_for_admission(digest) == context
        else:
            assert domain.dispatch_context_for_terminal(
                digest, passed=False
            ) == context

    assert domain.dispatch_context_count() == 0
    snapshot = domain.snapshot()
    assert snapshot.duplicate_feedback_count == 0
    assert snapshot.missing_dispatch_context_count == 0


def test_two_level_credit_keeps_fast_breadth_ahead_of_parked_deep() -> None:
    commands: queue.Queue = queue.Queue()
    terminals: queue.Queue = queue.Queue()
    committed: list[str] = []
    actor_ref: dict[str, Tier3ExpansionActor] = {}

    def admission_sink(intent) -> bool:
        committed.append(intent.candidate_digest)
        actor_ref["actor"].admission_result(
            AdmissionCommitted(
                refresh_epoch=intent.refresh_epoch,
                candidate_digest=intent.candidate_digest,
                lease_id=intent.lease_id,
                sequence=intent.sequence,
                pool_revision=len(committed),
                net_new_pool_growth=True,
            )
        )
        return True

    actor = Tier3ExpansionActor(
        "two-level-credit",
        dispatch_sink=commands.put_nowait,
        admission_sink=admission_sink,
        terminal_sink=terminals,
        initial_worker_envelope=16,
    )
    actor_ref["actor"] = actor
    endpoints = tuple(f"http://probe-{index}.example/204" for index in range(4))
    actor.start()
    try:
        actor.set_credit_limits(128, 32)
        actor.set_expansion_control(
            pressure=1.0,
            dispatch_enabled=True,
            external_fresh_backlog=64,
            fast_supply_state="ready",
        )
        _wait_until(lambda: actor.snapshot().fast_supply_state == "ready")
        for index in range(64):
            node = Node(
                name=f"credit-{index}",
                protocol="http",
                address=f"10.77.{index // 250}.{index % 250 + 1}",
                port=8000 + index,
                source="one-large-root",
                tier="tier3",
                extra={
                    "tier3_source_root_id": "one-large-root",
                    "tier3_source_unit_family": "http",
                },
            )
            digest = f"{index + 1:064x}"
            assert actor.submit(
                CandidateEnvelope(
                    refresh_epoch="two-level-credit",
                    candidate_digest=digest,
                    config_digest=digest,
                    node_json=canonical_json(node.to_dict()),
                    report_json="{}",
                    source_root="one-large-root",
                    source_family="http",
                    probe_urls=endpoints,
                )
            )
        actor.capacity_available(16)

        fast_dispatched: set[str] = set()
        deep_held: list[object] = []
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and (
            len(fast_dispatched) < 64 or len(committed) < 3
        ):
            try:
                command = commands.get(timeout=0.1)
            except queue.Empty:
                continue
            if command.stage is ValidationStage.CANONICAL_DEEP:
                # Keep the first deep cohort active. FAST must still consume
                # the remaining worker-ready credits and reach candidates
                # beyond the original 32-item window.
                deep_held.append(command)
                continue
            fast_dispatched.add(command.candidate_digest)
            candidate_index = int(command.candidate_digest, 16) - 1
            passed = 32 <= candidate_index < 40
            fact = ProbeAttemptFact(
                endpoint=command.endpoint,
                elapsed_ms=25,
                full_budget=True,
                completed=True,
                status_code=204 if passed else 502,
                outcome="pass" if passed else "transient",
                failure_domain="none" if passed else "candidate_or_endpoint",
                detail="" if passed else "http_502",
                attempt_id=f"{command.lease_id}:1",
                candidate_fingerprint=command.candidate_digest,
                session_lease_id=command.lease_id,
                stage=command.stage.value,
                granted_budget_ms=3000,
            )
            actor.endpoint_result(
                EndpointAttemptResult(
                    refresh_epoch=command.refresh_epoch,
                    transaction_id=command.transaction_id,
                    candidate_digest=command.candidate_digest,
                    config_digest=command.config_digest,
                    lease_id=command.lease_id,
                    slice_id=command.slice_id,
                    sequence=command.sequence,
                    stage=command.stage,
                    worker_id=1,
                    worker_generation=1,
                    node_json=command.node_json,
                    kind=(
                        EndpointAttemptKind.PASS
                        if passed
                        else EndpointAttemptKind.TRANSIENT
                    ),
                    report_json=command.report_json,
                    reason="pass" if passed else "http_502",
                    latency_ms=25.0 if passed else None,
                    probe_url=command.endpoint if passed else "",
                    attempt_facts=(fact,),
                    clean_confirmed=True,
                    probe_policy_revision=command.probe_policy_revision,
                    portfolio_digest=command.portfolio_digest,
                )
            )
            actor.capacity_available(1)

        _wait_until(lambda: actor.snapshot().fast_completed == 64)
        snapshot = actor.snapshot()
        assert len(fast_dispatched) == 64
        assert len(committed) >= 3
        assert snapshot.deep_retention_used == 4
        assert snapshot.deep_retention_overflow_censored == 52
        assert snapshot.ready_work_used <= snapshot.ready_work_limit == 32
        assert snapshot.actor_owner_used <= snapshot.actor_owner_limit == 128
    finally:
        assert actor.stop(timeout_seconds=3.0)


def test_process_domain_work_steals_deep_for_one_root_family() -> None:
    terminals = []
    domain = Tier3ValidationDomain(
        refresh_epoch="single-bucket-deep-work-steal",
        binary_path="fixture",
        probe_urls=["http://fixture/probe-a", "https://fixture/probe-b"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        commit_node=lambda _node, _report, intent: AdmissionRejected(
            refresh_epoch=intent.refresh_epoch,
            candidate_digest=intent.candidate_digest,
            lease_id=intent.lease_id,
            sequence=intent.sequence,
            reason="not_expected",
        ),
        terminal_observer=lambda _node, terminal: terminals.append(terminal),
        min_workers=8,
        max_workers=8,
        helper_target=deterministic_helper_bootstrap,
    )
    candidates = []
    index = 1
    while len(candidates) < 16:
        node = Node(
            name=f"deep-steal-{index}",
            protocol="http",
            address=f"10.41.{index // 250}.{index % 250 + 1}",
            port=9000 + index,
            source="one-root",
            tier="tier3",
            extra={
                "tier3_source_root_id": "one-root",
                "tier3_source_unit_family": "http",
            },
        )
        if int(tier3_candidate_config_digest(node)[:8], 16) % 5 != 0:
            candidates.append(node)
        index += 1

    domain.start()
    domain.set_envelope(8, 1.0, 25)
    try:
        _wait_until(
            lambda: domain.snapshot().supervisor.active_workers == 8,
            timeout=12.0,
        )
        started = time.monotonic()
        for node in candidates:
                assert domain.submit(
                    node,
                    {
                        "configured_source_count": 1,
                        "fixture_delay_seconds": 1.0,
                        "fixture_validation_outcome": "candidate_fail",
                    },
                )

        assert domain.wait_idle(12.0), domain.snapshot()
        elapsed = time.monotonic() - started
        snapshot = domain.snapshot()
        assert elapsed < 12.0
        assert snapshot.actor.terminal == len(candidates)
        assert snapshot.actor.deep_completed >= 8
        assert snapshot.actor.deep_retention_overflow_censored > 0
        assert snapshot.actor.queued_deep == 0
        assert snapshot.actor.active_deep == 0
        assert snapshot.actor.terminal == len(candidates)
        assert snapshot.transaction_conservation_ok
    finally:
        before_stop = domain.snapshot().supervisor
        status = domain.stop(timeout_seconds=4.0)
        assert status == "STOPPED", {
            'before': before_stop.failure_reason_counts,
            'last_failure': before_stop.last_failure_reason,
            'workers': [(w['worker_id'], w['phase'], w['startup_phase'],
                         w['retirement_reason']) for w in before_stop.workers],
            'after': domain.snapshot().supervisor.failure_reason_counts,
        }


def test_job_termination_bulkhead_uses_one_shared_deadline() -> None:
    class _SlowJob:
        def __init__(self) -> None:
            self.closed = threading.Event()

        def terminate(self, _exit_code: int) -> None:
            time.sleep(0.1)

        def close(self) -> None:
            self.closed.set()

    jobs = [_SlowJob() for _ in range(8)]
    started = time.monotonic()

    completed = _terminate_jobs_bounded(  # type: ignore[arg-type]
        jobs,
        exit_code=74,
        timeout_seconds=0.3,
        close_after=True,
    )

    assert completed
    assert time.monotonic() - started < 0.25
    assert all(job.closed.is_set() for job in jobs)


def test_slow_job_termination_does_not_block_supervisor_dispatch(monkeypatch) -> None:
    original_terminate = WindowsJob.terminate

    def delayed_terminate(job, exit_code: int = 74) -> None:
        time.sleep(0.75)
        original_terminate(job, exit_code)

    monkeypatch.setattr(WindowsJob, "terminate", delayed_terminate)
    supervisor = Tier3ValidatorSupervisor(
        binary_path="unused-by-fixture",
        probe_urls=["http://127.0.0.1/never"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        attempt_sink=lambda _result: None,
        capacity_sink=lambda _count: None,
        min_workers=2,
        max_workers=2,
        cleanup_seconds=0.1,
        job_empty_wait_seconds=1.5,
        helper_target=uncooperative_helper_bootstrap,
    )
    supervisor.start()
    try:
        _wait_until(lambda: supervisor.snapshot().idle_workers == 2)
        supervisor.submit(
            ValidateEndpointCmd(
                refresh_epoch="slow-job-termination",
                candidate_digest="1" * 64,
                config_digest="a" * 64,
                lease_id="slow-job-termination-1",
                sequence=1,
                node_json=canonical_json(
                    Node(
                        name="slow-job-termination-1",
                        protocol="http",
                        address="127.0.0.1",
                        port=9,
                        tier="tier3",
                    ).to_dict()
                ),
                source_root="root",
                source_family="http",
                candidate_hard_cap_seconds=0.2,
            )
        )
        _wait_until(lambda: supervisor.snapshot().retiring_workers == 1, timeout=2.0)

        started = time.monotonic()
        supervisor.submit(
            ValidateEndpointCmd(
                refresh_epoch="slow-job-termination",
                candidate_digest="2" * 64,
                config_digest="b" * 64,
                lease_id="slow-job-termination-2",
                sequence=2,
                node_json=canonical_json(
                    Node(
                        name="slow-job-termination-2",
                        protocol="http",
                        address="127.0.0.1",
                        port=9,
                        tier="tier3",
                    ).to_dict()
                ),
                source_root="root",
                source_family="http",
                candidate_hard_cap_seconds=5.0,
            )
        )
        _wait_until(lambda: supervisor.snapshot().validating_workers == 1, timeout=0.5)
        assert time.monotonic() - started < 0.5
    finally:
        monkeypatch.setattr(WindowsJob, "terminate", original_terminate)
        assert supervisor.stop(timeout_seconds=4.0) == "STOPPED"


class _ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requests: list[bytes] = []


class _PassProxyHandler(socketserver.BaseRequestHandler):
    def _read_headers(self) -> bytes:
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 32768:
            block = self.request.recv(4096)
            if not block:
                break
            data += block
        return data

    def handle(self) -> None:
        self.request.settimeout(2.0)
        data = self._read_headers()
        self.server.requests.append(data)  # type: ignore[attr-defined]
        if data.startswith(b"CONNECT "):
            self.request.sendall(
                b"HTTP/1.1 200 Connection Established\r\n"
                b"Proxy-Agent: tier3-test\r\n\r\n"
            )
            tunneled = self._read_headers()
            self.server.requests.append(tunneled)  # type: ignore[attr-defined]
        self.request.sendall(
            b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n"
            b"Connection: close\r\n\r\n"
        )


class _SelectiveProxyHandler(_PassProxyHandler):
    def handle(self) -> None:
        self.request.settimeout(2.0)
        data = self._read_headers()
        self.server.requests.append(data)  # type: ignore[attr-defined]
        endpoint_request = data
        if data.startswith(b"CONNECT "):
            self.request.sendall(
                b"HTTP/1.1 200 Connection Established\r\n"
                b"Proxy-Agent: tier3-test\r\n\r\n"
            )
            endpoint_request = self._read_headers()
            self.server.requests.append(endpoint_request)  # type: ignore[attr-defined]
        bad_marker = bytes(getattr(self.server, "bad_marker", b""))
        if bad_marker and (bad_marker in data or bad_marker in endpoint_request):
            self.request.sendall(
                b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n"
                b"Connection: close\r\n\r\n"
            )
            return
        self.request.sendall(
            b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n"
            b"Connection: close\r\n\r\n"
        )


class _EndpointBudgetProxyHandler(_PassProxyHandler):
    def handle(self) -> None:
        self.request.settimeout(2.0)
        data = self._read_headers()
        self.server.requests.append(data)  # type: ignore[attr-defined]
        endpoint_request = data
        if data.startswith(b"CONNECT "):
            self.request.sendall(
                b"HTTP/1.1 200 Connection Established\r\n"
                b"Proxy-Agent: tier3-test\r\n\r\n"
            )
            endpoint_request = self._read_headers()
            self.server.requests.append(endpoint_request)  # type: ignore[attr-defined]
        delay_seconds, status = (0.0, 502)
        for marker, configured in getattr(
            self.server, "endpoint_behaviors", {}
        ).items():
            if marker in data or marker in endpoint_request:
                delay_seconds, status = configured
                break
        time.sleep(float(delay_seconds))
        response = (
            f"HTTP/1.1 {int(status)} "
            f"{'No Content' if int(status) == 204 else 'Bad Gateway'}\r\n"
            "Content-Length: 0\r\nConnection: close\r\n\r\n"
        ).encode("ascii")
        try:
            self.request.sendall(response)
        except OSError:
            pass


class _AlwaysBadProxyHandler(_PassProxyHandler):
    def handle(self) -> None:
        self.request.settimeout(2.0)
        data = self._read_headers()
        self.server.requests.append(data)  # type: ignore[attr-defined]
        if data.startswith(b"CONNECT "):
            self.request.sendall(
                b"HTTP/1.1 200 Connection Established\r\n"
                b"Proxy-Agent: tier3-test\r\n\r\n"
            )
            tunneled = self._read_headers()
            self.server.requests.append(tunneled)  # type: ignore[attr-defined]
        try:
            self.request.sendall(
                b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n"
                b"Connection: close\r\n\r\n"
            )
        except OSError:
            pass


class _OnlyMarkerProxyHandler(_PassProxyHandler):
    def handle(self) -> None:
        self.request.settimeout(2.0)
        data = self._read_headers()
        self.server.requests.append(data)  # type: ignore[attr-defined]
        endpoint_request = data
        if data.startswith(b"CONNECT "):
            self.request.sendall(
                b"HTTP/1.1 200 Connection Established\r\n"
                b"Proxy-Agent: tier3-test\r\n\r\n"
            )
            endpoint_request = self._read_headers()
            self.server.requests.append(endpoint_request)  # type: ignore[attr-defined]
        marker = bytes(getattr(self.server, "pass_marker", b""))
        status = 204 if marker and (marker in data or marker in endpoint_request) else 502
        response = (
            f"HTTP/1.1 {status} "
            f"{'No Content' if status == 204 else 'Bad Gateway'}\r\n"
            "Content-Length: 0\r\nConnection: close\r\n\r\n"
        ).encode("ascii")
        try:
            self.request.sendall(response)
        except OSError:
            pass


def _wait_until(predicate, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition did not become true")


def test_real_process_domain_reloads_mihomo_probes_and_commits() -> None:
    server = _ProxyServer(("127.0.0.1", 0), _PassProxyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    committed = []
    observed = []
    admission_results = []
    commit_event = threading.Event()

    def commit_node(node, _report, intent):
        committed.append(node)
        result = AdmissionCommitted(
            refresh_epoch=intent.refresh_epoch,
            candidate_digest=intent.candidate_digest,
            lease_id=intent.lease_id,
            sequence=intent.sequence,
            pool_revision=1,
            net_new_pool_growth=True,
        )
        commit_event.set()
        return result

    domain = Tier3ValidationDomain(
        refresh_epoch="real-domain",
        binary_path=str(MIHOMO),
        probe_urls=["http://127.0.0.1:18080/generate_204"],
        expected_statuses={204},
        probe_timeout_seconds=2.0,
        slow_threshold_ms=3000,
        commit_node=commit_node,
        terminal_observer=lambda node, terminal: observed.append((node, terminal)),
        admission_observer=admission_results.append,
        logger=logging.getLogger("test-tier3-process-domain"),
        min_workers=1,
        max_workers=1,
    )
    try:
        domain.start()
        node = Node(
            name="local-http-proxy",
            protocol="http",
            address="127.0.0.1",
            port=int(server.server_address[1]),
            source="local-test",
            tier="tier3",
            extra={
                "tier3_source_root_id": "local-root",
                "tier3_source_unit_family": "http",
            },
        )
        assert domain.submit(node, {"configured_source_count": 1})
        assert commit_event.wait(8.0), (domain.snapshot(), observed, server.requests)
        _wait_until(lambda: len(admission_results) == 1)
        assert len(committed) == 1
        assert committed[0].status in {NodeStatus.ALIVE, NodeStatus.SLOW}
        assert committed[0].extra["validation_profile"] == "endpoint_process_helper"
        assert committed[0].extra["tier3_validator_stage"] == "fast_probe"
        assert committed[0].extra["tier3_validator_clean_confirmed"] is True
        assert committed[0].extra["tier3_validator_submitted_monotonic_ns"] > 0
        assert committed[0].extra["tier3_validator_dispatch_monotonic_ns"] > 0
        assert committed[0].extra["tier3_validator_helper_started_monotonic_ns"] > 0
        assert committed[0].extra["tier3_validator_portfolio_started_monotonic_ns"] > 0
        assert committed[0].extra["real_probe_elapsed_ms"] >= 0
        _wait_until(lambda: bool(observed))
        assert observed[0][1].kind is Tier3TerminalKind.PASS
        assert observed[0][1].probe_policy_revision == 1
        assert observed[0][1].portfolio_digest
        assert domain.snapshot().actor.conservation_ok
    finally:
        status = domain.stop()
        server.shutdown()
        server.server_close()
        assert status == "STOPPED"


def test_packaged_parity_path_matches_legacy_oracle_for_same_endpoint() -> None:
    server = _ProxyServer(("127.0.0.1", 0), _PassProxyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    results: queue.Queue = queue.Queue()
    endpoint = "http://parity.invalid/generate_204"
    supervisor = Tier3ValidatorSupervisor(
        binary_path=str(MIHOMO),
        probe_urls=[endpoint],
        expected_statuses={204},
        probe_timeout_seconds=2.0,
        slow_threshold_ms=3000,
        capacity_sink=lambda _count: None,
        attempt_sink=results.put_nowait,
        min_workers=1,
        max_workers=1,
    )
    node = Node(
        name="parity-local-proxy",
        protocol="http",
        address="127.0.0.1",
        port=int(server.server_address[1]),
        source="parity-root",
        tier="tier3",
        extra={
            "tier3_source_root_id": "parity-root",
            "tier3_source_unit_family": "http",
        },
    )
    try:
        supervisor.start()
        _wait_for_helpers(supervisor, 1)
        helper_result = _run_helper_attempt(
            supervisor,
            results,
            node,
            endpoint,
            1,
        )
        oracle_result = _mihomo_direct_validate(
            node,
            {
                "request_timeout": 2.0,
                "slow_threshold_ms": 3000,
                "mihomo_binary_path": str(MIHOMO),
                "mihomo_helper": {
                    "binary_path": str(MIHOMO),
                    "probe_timeout": 2.0,
                    "port_attempts": 1,
                },
            },
            3000,
            logging.getLogger("parity-local"),
            _probe_urls_override=[endpoint],
        )
        assert helper_result.kind is EndpointAttemptKind.PASS
        assert oracle_result.status in {NodeStatus.ALIVE, NodeStatus.SLOW}
    finally:
        assert supervisor.stop(timeout_seconds=4.0) == "STOPPED"
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


@pytest.mark.skipif(os.environ.get("GITHUB_ACTIONS") == "true",
                    reason="speed gate against real helper processes or a real Core; "
                    "the release runner is slower than these budgets (runs locally)")
def test_process_helper_parity_classification_is_stable_at_1_4_8_16() -> None:
    server = _ProxyServer(("127.0.0.1", 0), _PassProxyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = "http://parity-ladder.invalid/generate_204"
    node = Node(
        name="parity-ladder-proxy",
        protocol="http",
        address="127.0.0.1",
        port=int(server.server_address[1]),
        source="parity-root",
        tier="tier3",
        extra={
            "tier3_source_root_id": "parity-root",
            "tier3_source_unit_family": "http",
        },
    )
    try:
        for worker_count in (1, 4, 8, 16):
            results: queue.Queue = queue.Queue()
            supervisor = Tier3ValidatorSupervisor(
                binary_path=str(MIHOMO),
                probe_urls=[endpoint],
                expected_statuses={204},
                # This ladder proves classification parity across worker
                # counts, not the timeout boundary. Match the helper command's
                # 5s canonical entitlement so a heavily loaded full-suite run
                # cannot turn scheduler starvation into a semantic mismatch.
                probe_timeout_seconds=5.0,
                slow_threshold_ms=3000,
                capacity_sink=lambda _count: None,
                attempt_sink=results.put_nowait,
                min_workers=worker_count,
                max_workers=worker_count,
            )
            supervisor.start()
            try:
                _wait_for_helpers(supervisor, worker_count)
                for sequence in range(1, 17):
                    supervisor.submit(
                        _parity_helper_command(
                            node,
                            endpoint,
                            worker_count * 100 + sequence,
                        )
                    )
                observed = []
                deadline = time.monotonic() + 12.0
                while len(observed) < 16 and time.monotonic() < deadline:
                    try:
                        observed.append(results.get(timeout=0.1))
                    except queue.Empty:
                        continue
                assert len(observed) == 16, supervisor.snapshot()
                failures = [
                    {
                        "kind": result.kind.value,
                        "reason": result.reason,
                        "worker": result.worker_id,
                    }
                    for result in observed
                    if result.kind is not EndpointAttemptKind.PASS
                ]
                assert not failures, {
                    "workers": worker_count,
                    "failures": failures,
                    "supervisor": supervisor.snapshot(),
                }
                assert not supervisor.snapshot().hard_stall
            finally:
                assert supervisor.stop(timeout_seconds=5.0) == "STOPPED"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


def test_process_validator_settings_preserve_user_order_then_append_fallbacks() -> None:
    configured = [
        "https://configured.example/first",
        "http://configured.example/second",
    ]
    settings = process_validator_settings(
        {"mihomo_helper": {"probe_urls": configured}}
    )
    assert settings["probe_urls"][:2] == configured
    assert len(settings["probe_urls"]) > len(configured)
    assert len(settings["probe_urls"]) == len(set(settings["probe_urls"]))


def test_real_process_domain_fast_continue_then_deep_http_passes_once() -> None:
    server = _ProxyServer(("127.0.0.1", 0), _SelectiveProxyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    commits = []
    observed = []
    committed = threading.Event()
    endpoints = (
        "http://probe-a.invalid/fast-a",
        "http://probe-b.invalid/fast-b",
    )

    def commit_node(node, _report, intent):
        commits.append((node, intent))
        committed.set()
        return AdmissionCommitted(
            refresh_epoch=intent.refresh_epoch,
            candidate_digest=intent.candidate_digest,
            lease_id=intent.lease_id,
            sequence=intent.sequence,
            pool_revision=1,
            net_new_pool_growth=True,
        )

    domain = Tier3ValidationDomain(
        refresh_epoch="real-staged-domain",
        binary_path=str(MIHOMO),
        probe_urls=list(endpoints),
        expected_statuses={204},
        probe_timeout_seconds=5.0,
        slow_threshold_ms=3000,
        commit_node=commit_node,
        terminal_observer=lambda node, terminal: observed.append((node, terminal)),
        min_workers=1,
        max_workers=1,
    )
    node = Node(
        name="staged-local-proxy",
        protocol="http",
        address="127.0.0.1",
        port=int(server.server_address[1]),
        source="local-test",
        tier="tier3",
        extra={
            "tier3_source_root_id": "local-root",
            "tier3_source_unit_family": "http",
        },
    )
    identity = {
        "protocol": node.protocol,
        "address": node.address,
        "port": int(node.port),
        "uuid": node.uuid,
        "password": node.password,
        "method": node.method,
        "security": node.security,
        "network": node.network,
        "host": node.host,
        "path": node.path,
        "sni": node.sni,
        "plugin": node.plugin,
    }
    candidate_digest = digest_json(identity)
    fast_endpoint = Tier3EndpointController(endpoints).work_decision(
        candidate_digest,
        ValidationStage.FAST_PROBE,
        source_root="local-root",
        source_family="http",
    ).endpoint
    server.bad_marker = str(urlsplit(fast_endpoint).hostname).encode("ascii")  # type: ignore[attr-defined]
    try:
        domain.start()
        assert domain.submit(node, {"configured_source_count": 1})
        if not committed.wait(10.0):
            snapshot = domain.snapshot()
            raise AssertionError(
                {
                    "actor": {
                        "submitted": snapshot.actor.submitted,
                        "queued": snapshot.actor.queued,
                        "active": snapshot.actor.active,
                        "terminal": snapshot.actor.terminal,
                        "passed": snapshot.actor.passed,
                        "censored": snapshot.actor.censored,
                        "fast_completed": snapshot.actor.fast_completed,
                        "deep_completed": snapshot.actor.deep_completed,
                    },
                    "terminals": [
                        {
                            "kind": terminal.kind.value,
                            "reason": terminal.reason,
                            "probe_url": terminal.probe_url,
                            "attempts": [
                                {
                                    "endpoint": fact.endpoint,
                                    "outcome": fact.outcome,
                                    "completed": fact.completed,
                                    "full_budget": fact.full_budget,
                                    "lifecycle": fact.lifecycle,
                                    "detail": fact.detail,
                                }
                                for fact in terminal.attempt_facts
                            ],
                        }
                        for _node, terminal in observed
                    ],
                    "request_count": len(server.requests),
                    "request_lines": [
                        request.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
                        for request in server.requests[-12:]
                    ],
                }
            )
        _wait_until(lambda: len(observed) == 1)
        snapshot = domain.snapshot()
        assert len(commits) == 1
        assert snapshot.actor.fast_continued == 1
        assert snapshot.actor.deep_completed == 1
        assert snapshot.actor.deep_passed == 1
        assert snapshot.actor.slice_dispatched == 2
        assert snapshot.actor.terminal == 1
        assert snapshot.actor.admission_intents == 1
        assert snapshot.actor.conservation_ok
        terminal = observed[0][1]
        assert terminal.stage is ValidationStage.CANONICAL_DEEP
        assert terminal.kind is Tier3TerminalKind.PASS
        assert terminal.probe_url != fast_endpoint
    finally:
        assert domain.stop() == "STOPPED"
        server.shutdown()
        server.server_close()


def test_real_process_domain_later_endpoint_keeps_full_entitlement() -> None:
    server = _ProxyServer(("127.0.0.1", 0), _EndpointBudgetProxyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoints = (
        "http://probe-a.invalid/generate_204",
        "http://probe-b.invalid/generate_204",
        "http://probe-c.invalid/generate_204",
    )
    server.endpoint_behaviors = {  # type: ignore[attr-defined]
        b"probe-a.invalid": (0.70, 502),
        b"probe-b.invalid": (0.70, 502),
        b"probe-c.invalid": (0.05, 204),
    }
    commits = []
    observed = []
    committed = threading.Event()

    def commit_node(node, _report, intent):
        commits.append((node, intent))
        committed.set()
        return AdmissionCommitted(
            refresh_epoch=intent.refresh_epoch,
            candidate_digest=intent.candidate_digest,
            lease_id=intent.lease_id,
            sequence=intent.sequence,
            pool_revision=1,
            net_new_pool_growth=True,
        )

    node = Node(
        name="endpoint-budget-proxy",
        protocol="http",
        address="127.0.0.1",
        port=int(server.server_address[1]),
        source="local-test",
        tier="tier3",
        extra={
            "tier3_source_root_id": "local-root",
            "tier3_source_unit_family": "http",
        },
    )
    for index in range(32):
        node.path = f"/selector-{index}"
        identity = {
            "protocol": node.protocol,
            "address": node.address,
            "port": int(node.port),
            "uuid": node.uuid,
            "password": node.password,
            "method": node.method,
            "security": node.security,
            "network": node.network,
            "host": node.host,
            "path": node.path,
            "sni": node.sni,
            "plugin": node.plugin,
        }
        candidate_digest = digest_json(identity)
        selector = int(
            hashlib.sha256(candidate_digest.encode("utf-8")).hexdigest()[:8],
            16,
        )
        if selector % len(endpoints) != 2:
            break
    else:
        raise AssertionError("could not select a timeout endpoint for FAST")

    domain = Tier3ValidationDomain(
        refresh_epoch="endpoint-entitlement-domain",
        binary_path=str(MIHOMO),
        probe_urls=list(endpoints),
        expected_statuses={204},
        probe_timeout_seconds=0.40,
        slow_threshold_ms=3000,
        commit_node=commit_node,
        terminal_observer=lambda result_node, terminal: observed.append(
            (result_node, terminal)
        ),
        min_workers=1,
        max_workers=1,
    )
    try:
        domain.start()
        _wait_until(
            lambda: domain.snapshot().supervisor.active_workers >= 1,
            timeout=15.0,
        )
        assert domain.submit(node, {"configured_source_count": 1})
        if not committed.wait(10.0):
            snapshot = domain.snapshot()
            raise AssertionError(
                {
                    "actor": {
                        "submitted": snapshot.actor.submitted,
                        "queued": snapshot.actor.queued,
                        "active": snapshot.actor.active,
                        "terminal": snapshot.actor.terminal,
                        "passed": snapshot.actor.passed,
                        "censored": snapshot.actor.censored,
                        "fast_completed": snapshot.actor.fast_completed,
                        "deep_completed": snapshot.actor.deep_completed,
                    },
                    "terminals": [
                        {
                            "kind": terminal.kind.value,
                            "reason": terminal.reason,
                            "probe_url": terminal.probe_url,
                            "attempts": [
                                {
                                    "endpoint": fact.endpoint,
                                    "outcome": fact.outcome,
                                    "completed": fact.completed,
                                    "full_budget": fact.full_budget,
                                    "lifecycle": fact.lifecycle,
                                    "detail": fact.detail,
                                }
                                for fact in terminal.attempt_facts
                            ],
                        }
                        for _node, terminal in observed
                    ],
                    "request_count": len(server.requests),
                    "request_lines": [
                        request.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
                        for request in server.requests[-12:]
                    ],
                }
            )
        _wait_until(lambda: len(observed) == 1)
        terminal = observed[0][1]
        attempted = [fact.endpoint for fact in terminal.attempt_facts]
        assert terminal.kind is Tier3TerminalKind.PASS
        assert terminal.probe_url == endpoints[2]
        assert endpoints[2] in attempted
        assert len(commits) == 1
        assert domain.snapshot().actor.admission_intents == 1
    finally:
        assert domain.stop(timeout_seconds=4.0) == "STOPPED"
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


@pytest.mark.skipif(os.environ.get("GITHUB_ACTIONS") == "true",
                    reason="speed gate against real helper processes or a real Core; "
                    "the release runner is slower than these budgets (runs locally)")
def test_real_process_domain_meets_controlled_gate_twenty_second_and_target_budgets() -> None:
    server = _ProxyServer(("127.0.0.1", 0), _PassProxyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    committed_count = 0
    committed_lock = threading.Lock()
    gate_reached = threading.Event()
    twenty_second_target_reached = threading.Event()
    target_reached = threading.Event()

    def commit_node(node, _report, intent):
        nonlocal committed_count
        with committed_lock:
            committed_count += 1
            revision = committed_count
            if revision >= 3:
                gate_reached.set()
            if revision >= 15:
                twenty_second_target_reached.set()
            if revision >= 25:
                target_reached.set()
        return AdmissionCommitted(
            refresh_epoch=intent.refresh_epoch,
            candidate_digest=intent.candidate_digest,
            lease_id=intent.lease_id,
            sequence=intent.sequence,
            pool_revision=revision,
            net_new_pool_growth=True,
        )

    domain = Tier3ValidationDomain(
        refresh_epoch="controlled-performance-domain",
        binary_path=str(MIHOMO),
        probe_urls=[
            "http://probe-a.invalid/generate_204",
            "http://probe-b.invalid/generate_204",
            "http://probe-c.invalid/generate_204",
        ],
        expected_statuses={204},
        probe_timeout_seconds=5.0,
        slow_threshold_ms=3000,
        commit_node=commit_node,
        terminal_observer=lambda _node, _terminal: None,
        min_workers=4,
        max_workers=16,
    )
    try:
        # Startup is part of the expansion budget, including helper readiness.
        started = time.monotonic()
        domain.start()
        domain.set_envelope(16, pressure=1.0, deficit=25)
        for index in range(25):
            node = Node(
                name=f"controlled-pass-{index}",
                protocol="http",
                address="127.0.0.1",
                port=int(server.server_address[1]),
                path=f"/candidate-{index}",
                password=f"fixture-password-{index}",
                source=f"local-root-{index % 5}",
                tier="tier3",
                extra={
                    "username": "fixture-user",
                    "tier3_source_root_id": f"local-root-{index % 5}",
                    "tier3_source_unit_family": "http",
                },
            )
            assert domain.submit(node, {"configured_source_count": 5})

        gate_budget = max(0.0, 10.0 - (time.monotonic() - started))
        assert gate_reached.wait(gate_budget), domain.snapshot()
        twenty_second_budget = max(0.0, 20.0 - (time.monotonic() - started))
        assert twenty_second_target_reached.wait(
            twenty_second_budget
        ), domain.snapshot()
        target_budget = max(0.0, 61.0 - (time.monotonic() - started))
        assert target_reached.wait(target_budget), domain.snapshot()
        _wait_until(lambda: domain.snapshot().actor.admission_committed >= 25)
        snapshot = domain.snapshot()
        assert committed_count == 25
        assert snapshot.actor.passed == 25
        assert snapshot.actor.admission_intents == 25
        assert snapshot.actor.admission_committed == 25
        assert snapshot.actor.conservation_ok
    finally:
        assert domain.stop(timeout_seconds=6.0) == "STOPPED"
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


@pytest.mark.skipif(os.environ.get("GITHUB_ACTIONS") == "true",
                    reason="speed gate against real helper processes or a real Core; "
                    "the release runner is slower than these budgets (runs locally)")
def test_real_process_domain_low_yield_root_does_not_block_late_endpoint_passes() -> None:
    bad_server = _ProxyServer(("127.0.0.1", 0), _AlwaysBadProxyHandler)
    good_server = _ProxyServer(("127.0.0.1", 0), _OnlyMarkerProxyHandler)
    good_server.pass_marker = b"127.0.0.1:18084"  # type: ignore[attr-defined]
    threads = [
        threading.Thread(target=bad_server.serve_forever, daemon=True),
        threading.Thread(target=good_server.serve_forever, daemon=True),
    ]
    for thread in threads:
        thread.start()
    committed_count = 0
    committed_lock = threading.Lock()
    gate_reached = threading.Event()
    target_reached = threading.Event()

    def commit_node(_node, _report, intent):
        nonlocal committed_count
        with committed_lock:
            committed_count += 1
            revision = committed_count
            if revision >= 3:
                gate_reached.set()
            if revision >= 15:
                target_reached.set()
        return AdmissionCommitted(
            refresh_epoch=intent.refresh_epoch,
            candidate_digest=intent.candidate_digest,
            lease_id=intent.lease_id,
            sequence=intent.sequence,
            pool_revision=revision,
            net_new_pool_growth=True,
        )

    endpoints = [
        "http://127.0.0.1:18081/probe-a",
        "http://127.0.0.1:18082/probe-b",
        "http://127.0.0.1:18083/probe-c",
        "http://127.0.0.1:18084/late-pass",
    ]
    domain = Tier3ValidationDomain(
        refresh_epoch="low-yield-late-root",
        binary_path=str(MIHOMO),
        probe_urls=endpoints,
        expected_statuses={204},
        probe_timeout_seconds=5.0,
        slow_threshold_ms=3000,
        commit_node=commit_node,
        terminal_observer=lambda _node, _terminal: None,
        min_workers=4,
        max_workers=16,
    )
    try:
        domain.set_envelope(16, pressure=1.0, deficit=25)
        domain.start()
        _wait_until(
            lambda: domain.snapshot().supervisor.active_workers >= 4,
            # This test measures scheduling after workers are ready. Give
            # Windows process creation its own bound so full-suite load is
            # not charged against the 3/3 and 20-second admission clocks.
            timeout=20.0,
        )
        started = time.monotonic()
        for index in range(300):
            assert domain.submit(
                Node(
                    name=f"early-bad-{index}",
                    protocol="http",
                    address="127.0.0.1",
                    port=int(bad_server.server_address[1]),
                    path=f"/early-{index}",
                    password=f"early-password-{index}",
                    source="early-large-root",
                    tier="tier3",
                    extra={
                        "username": "fixture-user",
                        "tier3_source_root_id": "early-large-root",
                        "tier3_source_unit_family": "http",
                    },
                ),
                {"configured_source_count": 5},
            )
        for index in range(20):
            assert domain.submit(
                Node(
                    name=f"late-good-{index}",
                    protocol="http",
                    address="127.0.0.1",
                    port=int(good_server.server_address[1]),
                    path=f"/late-{index}",
                    password=f"late-password-{index}",
                    source=f"late-root-{index % 4}",
                    tier="tier3",
                    extra={
                        "username": "fixture-user",
                        "tier3_source_root_id": f"late-root-{index % 4}",
                        "tier3_source_unit_family": "http",
                    },
                ),
                {"configured_source_count": 5},
            )

        _wait_until(
            lambda: domain.snapshot().accepted_transactions == 320,
            timeout=2.0,
        )
        _wait_until(
            lambda: (
                    domain.snapshot().actor_submitted_transactions
                    > domain.snapshot().actor_owner_limit
                and (
                    domain.snapshot().actor.parked_fast
                    + domain.snapshot().actor.deep_parked
                )
                > 0
            ),
            timeout=3.0,
        )
        initial = domain.snapshot()
        assert initial.actor_owner_used <= initial.actor_owner_limit
        assert initial.actor.ready_work_used <= 32
        assert initial.actor.parked_fast + initial.actor.deep_parked > 0
        assert initial.actor_submitted_transactions > initial.actor_owner_limit

        gate_ok = gate_reached.wait(
            max(0.0, 10.0 - (time.monotonic() - started))
        )
        if not gate_ok:
            failed = domain.snapshot()
            raise AssertionError(
                {
                    "actor": {
                        "submitted": failed.actor.submitted,
                        "terminal": failed.actor.terminal,
                        "queued_fast": failed.actor.queued_fast,
                        "active_fast": failed.actor.active_fast,
                        "deep_ready": failed.actor.deep_ready,
                        "deep_parked": failed.actor.deep_parked,
                        "active_deep": failed.actor.active_deep,
                        "fast_completed": failed.actor.fast_completed,
                        "fast_passed": failed.actor.fast_passed,
                        "deep_completed": failed.actor.deep_completed,
                        "deep_passed": failed.actor.deep_passed,
                        "slots": (
                            failed.actor.fast_slots,
                            failed.actor.deep_slots,
                        ),
                        "allocation": failed.actor.allocation_reason,
                    },
                    "ingress": failed.ingress_depth,
                    "window": (
                        failed.transaction_window_used,
                        failed.transaction_window_limit,
                    ),
                    "helpers": {
                        "active": failed.supervisor.active_workers,
                        "idle": failed.supervisor.idle_workers,
                        "validating": failed.supervisor.validating_workers,
                        "pending": failed.supervisor.pending_commands,
                    },
                    "committed": committed_count,
                    "endpoint_health": failed.endpoint_health,
                    "dispatch_tail": failed.dispatch_samples[-8:],
                    "attempt_tail": failed.attempt_samples[-8:],
                    "good_request_tail": [
                        request.split(b"\r\n", 1)[0].decode(
                            "ascii",
                            errors="replace",
                        )
                        for request in good_server.requests[-12:]
                    ],
                }
            )
        target_ok = target_reached.wait(
            max(0.0, 20.0 - (time.monotonic() - started))
        )
        if not target_ok:
            failed = domain.snapshot()
            raise AssertionError(
                {
                    "actor": {
                        "submitted": failed.actor.submitted,
                        "terminal": failed.actor.terminal,
                        "queued_fast": failed.actor.queued_fast,
                        "active_fast": failed.actor.active_fast,
                        "deep_ready": failed.actor.deep_ready,
                        "deep_parked": failed.actor.deep_parked,
                        "active_deep": failed.actor.active_deep,
                        "fast_completed": failed.actor.fast_completed,
                        "fast_passed": failed.actor.fast_passed,
                        "deep_completed": failed.actor.deep_completed,
                        "deep_passed": failed.actor.deep_passed,
                        "slots": (
                            failed.actor.fast_slots,
                            failed.actor.deep_slots,
                        ),
                        "allocation": failed.actor.allocation_reason,
                    },
                    "ingress": failed.ingress_depth,
                    "window": (
                        failed.transaction_window_used,
                        failed.transaction_window_limit,
                    ),
                    "helpers": {
                        "active": failed.supervisor.active_workers,
                        "idle": failed.supervisor.idle_workers,
                        "validating": failed.supervisor.validating_workers,
                        "pending": failed.supervisor.pending_commands,
                    },
                    "committed": committed_count,
                    "endpoint_health": failed.endpoint_health,
                    "dispatch_tail": failed.dispatch_samples[-8:],
                    "attempt_tail": failed.attempt_samples[-8:],
                    "good_request_tail": [
                        request.split(b"\r\n", 1)[0].decode(
                            "ascii",
                            errors="replace",
                        )
                        for request in good_server.requests[-12:]
                    ],
                }
            )
        _wait_until(
            lambda: int(
                domain.snapshot().endpoint_health["_policy"][
                    "fast_entitlement_count"
                ]
            )
            >= 32,
            timeout=3.0,
        )
        snapshot = domain.snapshot()
        assert committed_count >= 15
        assert snapshot.actor.deep_completed > 0
        assert snapshot.actor.controller_dispatch_mismatch_count == 0
        assert snapshot.transaction_window_used <= snapshot.transaction_window_limit
        assert snapshot.ready_work_used <= snapshot.ready_work_limit
        assert snapshot.endpoint_health["_policy"]["fast_entitlement_count"] >= 32
    finally:
        # Cancelling the remaining 300+ transaction fixture is not the stop
        # latency contract exercised here; dedicated stop tests retain the
        # stricter production bound.
        assert domain.stop(timeout_seconds=10.0) == "STOPPED"
        bad_server.shutdown()
        good_server.shutdown()
        bad_server.server_close()
        good_server.server_close()
        for thread in threads:
            thread.join(timeout=1.0)


def test_noncooperative_helper_is_physically_killed_before_replacement(
    monkeypatch,
) -> None:
    def forbidden_serial_wait(_job, _timeout_seconds):
        raise AssertionError("runtime retirement must not synchronously wait for Job empty")

    monkeypatch.setattr(WindowsJob, "wait_empty", forbidden_serial_wait)
    terminals = []
    yields: list[EndpointAttemptResult] = []
    generations: queue.Queue = queue.Queue()
    capacities = []
    supervisor = Tier3ValidatorSupervisor(
        binary_path="unused-by-fixture",
        probe_urls=["http://127.0.0.1/never"],
        expected_statuses={204},
        probe_timeout_seconds=5.0,
        slow_threshold_ms=3000,
        attempt_sink=yields.append,
        capacity_sink=capacities.append,
        generation_sink=generations.put_nowait,
        min_workers=1,
        max_workers=1,
        cleanup_seconds=0.15,
        job_empty_wait_seconds=0.75,
        helper_target=uncooperative_helper_bootstrap,
    )
    supervisor.start()
    try:
        _wait_until(lambda: supervisor.snapshot().idle_workers == 1)
        supervisor.submit(
            ValidateEndpointCmd(
                refresh_epoch="hard-kill",
                candidate_digest="d" * 64,
                config_digest="c" * 64,
                lease_id="lease-1",
                sequence=1,
                node_json=canonical_json(
                    Node(
                        name="never",
                        protocol="http",
                        address="127.0.0.1",
                        port=9,
                        tier="tier3",
                    ).to_dict()
                ),
                source_root="root",
                source_family="http",
                candidate_hard_cap_seconds=0.35,
            )
        )
        _wait_until(lambda: bool(yields), timeout=4.0)
        assert not terminals
        assert yields[0].stage is ValidationStage.FAST_PROBE
        assert yields[0].clean_confirmed is True
        gone = generations.get(timeout=2.0)
        assert gone.job_empty is True
        assert gone.ports_released is True
        assert gone.reason == "dispatch_ack_deadline_cleanup_timeout"
        _wait_until(lambda: supervisor.snapshot().replacement_count == 1)
        _wait_until(lambda: supervisor.snapshot().idle_workers == 1)
        first_attempt_count = len(yields)
        supervisor.submit(
            ValidateEndpointCmd(
                refresh_epoch="hard-kill",
                candidate_digest="d" * 64,
                config_digest="c" * 64,
                lease_id="lease-2",
                sequence=2,
                node_json=canonical_json(
                    Node(
                        name="never-deep",
                        protocol="http",
                        address="127.0.0.1",
                        port=9,
                        tier="tier3",
                    ).to_dict()
                ),
                source_root="root",
                source_family="http",
                transaction_id="hard-kill-transaction",
                slice_id=2,
                stage=ValidationStage.CANONICAL_DEEP,
                probe_budget_seconds=0.2,
                candidate_hard_cap_seconds=0.35,
            )
        )
        _wait_until(lambda: len(yields) > first_attempt_count, timeout=4.0)
        assert not terminals
        assert yields[-1].kind is EndpointAttemptKind.FACILITY_FAIL
        assert yields[-1].attempt_facts == ()
        assert yields[-1].stage is ValidationStage.CANONICAL_DEEP
        snapshot = supervisor.snapshot()
        assert snapshot.active_workers <= 1
        assert snapshot.retiring_workers == 0
    finally:
        assert supervisor.stop(timeout_seconds=3.0) == "STOPPED"


def test_parent_sealed_lease_rejects_late_pass_before_clean_confirmation() -> None:
    terminals = []
    yields: list[EndpointAttemptResult] = []
    supervisor = Tier3ValidatorSupervisor(
        binary_path="unused-by-fixture",
        probe_urls=["http://127.0.0.1/late"],
        expected_statuses={204},
        probe_timeout_seconds=5.0,
        slow_threshold_ms=3000,
        attempt_sink=yields.append,
        capacity_sink=lambda _count: None,
        min_workers=1,
        max_workers=1,
        cleanup_seconds=0.5,
        helper_target=late_terminal_helper_bootstrap,
    )
    supervisor.start()
    try:
        _wait_until(lambda: supervisor.snapshot().idle_workers == 1)
        supervisor.submit(
            ValidateEndpointCmd(
                refresh_epoch="late-terminal",
                candidate_digest="e" * 64,
                config_digest="f" * 64,
                lease_id="lease-late",
                sequence=1,
                node_json=canonical_json(
                    Node(
                        name="late",
                        protocol="http",
                        address="127.0.0.1",
                        port=9,
                        tier="tier3",
                    ).to_dict()
                ),
                source_root="root",
                source_family="http",
                candidate_hard_cap_seconds=0.2,
            )
        )
        _wait_until(lambda: bool(yields), timeout=3.0)
        time.sleep(0.2)
        assert terminals == []
        assert yields[0].reason == "validator_dispatch_ack_timeout"
        assert yields[0].kind is EndpointAttemptKind.FACILITY_FAIL
        assert yields[0].clean_confirmed is True
        _wait_until(lambda: supervisor.snapshot().idle_workers == 1)
        snapshot = supervisor.snapshot()
        assert snapshot.replacement_count == 0
        assert snapshot.forced_job_termination_count == 0
    finally:
        assert supervisor.stop(timeout_seconds=3.0) == "STOPPED"


def test_default_candidate_deadline_includes_terminal_delivery_slack() -> None:
    command = ValidateEndpointCmd(
        refresh_epoch="deadline-default",
        candidate_digest="a" * 64,
        config_digest="b" * 64,
        lease_id="lease-default",
        sequence=1,
        node_json="{}",
        source_root="root",
        source_family="vless",
    )
    assert command.facility_budget_seconds == 2.0
    assert command.stage is ValidationStage.FAST_PROBE
    assert command.probe_budget_seconds == 3.0
    assert command.candidate_hard_cap_seconds == 5.75
    deep = ValidateEndpointCmd(
        refresh_epoch="deadline-deep",
        candidate_digest="c" * 64,
        config_digest="d" * 64,
        lease_id="lease-deep",
        sequence=2,
        node_json="{}",
        source_root="root",
        source_family="vless",
        stage=ValidationStage.CANONICAL_DEEP,
        probe_budget_seconds=5.0,
        candidate_hard_cap_seconds=7.75,
    )
    assert deep.probe_budget_seconds == 5.0
    assert deep.candidate_hard_cap_seconds == 7.75


def test_endpoint_deadline_records_current_attempt_as_right_censored() -> None:
    class _Connection:
        def send_bytes(self, _payload) -> None:
            return None

    runtime = ValidatorHelperRuntime(
        _Connection(),
        {
            "worker_id": 1,
            "worker_generation": 1,
            "binary_path": "fixture",
            "probe_urls": ["http://fixture/slow"],
            "expected_statuses": [204],
            "probe_timeout_seconds": 5.0,
            "slow_threshold_ms": 3000,
        },
    )

    async def _reload(_self, _node, _cmd):
        return True, "", 1

    async def _probe(_self, _endpoint, _budget, _cmd, _attempt_index):
        await asyncio.sleep(1.0)
        raise AssertionError("portfolio timeout should cancel the transport")

    runtime._reload_candidate = types.MethodType(_reload, runtime)
    runtime._probe_once = types.MethodType(_probe, runtime)
    command = ValidateEndpointCmd(
        refresh_epoch="portfolio-censored",
        candidate_digest="a" * 64,
        config_digest="b" * 64,
        lease_id="lease-censored",
        sequence=1,
        node_json=canonical_json(
            Node(
                name="censored",
                protocol="http",
                address="127.0.0.1",
                port=9,
                tier="tier3",
            ).to_dict()
        ),
        source_root="root",
        source_family="http",
        stage=ValidationStage.CANONICAL_DEEP,
        endpoint="http://fixture/slow",
        probe_budget_seconds=0.05,
    )
    try:
        terminal = asyncio.run(runtime.validate_endpoint(command))
    finally:
        asyncio.run(runtime.close())

    assert terminal.kind is EndpointAttemptKind.RIGHT_CENSORED
    assert len(terminal.attempt_facts) == 1
    fact = terminal.attempt_facts[0]
    assert fact.endpoint == "http://fixture/slow"
    assert fact.completed is False
    assert fact.full_budget is False
    assert fact.outcome == "right_censored"
    assert fact.lifecycle == "censored"


def test_endpoint_deadline_does_not_wait_for_slow_transport_cleanup() -> None:
    class _Connection:
        def send_bytes(self, _payload) -> None:
            return None

    runtime = ValidatorHelperRuntime(
        _Connection(),
        {
            "worker_id": 1,
            "worker_generation": 1,
            "binary_path": "fixture",
            "probe_urls": ["http://fixture/slow-cleanup"],
            "expected_statuses": [204],
            "probe_timeout_seconds": 5.0,
            "slow_threshold_ms": 3000,
        },
    )

    async def exercise() -> tuple[EndpointAttemptResult, float, int]:
        cleanup_release = asyncio.Event()
        cleanup_entered = asyncio.Event()

        async def _reload(_self, _node, _cmd):
            return True, "", 1

        async def _probe(_self, _endpoint, _budget, _cmd, _attempt_index):
            try:
                await asyncio.sleep(60.0)
            except asyncio.CancelledError:
                cleanup_entered.set()
                await cleanup_release.wait()
                raise

        runtime._reload_candidate = types.MethodType(_reload, runtime)
        runtime._probe_once = types.MethodType(_probe, runtime)
        command = ValidateEndpointCmd(
            refresh_epoch="slow-probe-cleanup",
            candidate_digest="a" * 64,
            config_digest="b" * 64,
            lease_id="lease-slow-cleanup",
            sequence=1,
            node_json=canonical_json(
                Node(
                    name="slow-cleanup",
                    protocol="http",
                    address="127.0.0.1",
                    port=9,
                    tier="tier3",
                ).to_dict()
            ),
            source_root="root",
            source_family="http",
            endpoint="http://fixture/slow-cleanup",
            probe_budget_seconds=0.05,
        )

        started = time.monotonic()
        result = await runtime.validate_endpoint(command)
        elapsed = time.monotonic() - started
        await asyncio.wait_for(cleanup_entered.wait(), timeout=0.2)
        detached_while_cleanup_blocked = len(runtime._detached_probe_tasks)
        cleanup_release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await runtime.close()
        return result, elapsed, detached_while_cleanup_blocked

    result, elapsed, detached = asyncio.run(exercise())

    assert elapsed < 0.3
    assert result.kind is EndpointAttemptKind.RIGHT_CENSORED
    assert result.attempt_facts[0].outcome == "right_censored"
    assert result.clean_confirmed is False
    assert detached == 1
    assert not runtime._detached_probe_tasks


def test_completed_probes_reuse_session_until_helper_shutdown() -> None:
    class _Connection:
        def send_bytes(self, _payload) -> None:
            return None

    cleanup_entered: asyncio.Event
    cleanup_release: asyncio.Event
    connector_instances: list[object] = []
    session_instances: list[object] = []

    class _Connector:
        def __init__(self, **_kwargs) -> None:
            self.closed = False
            connector_instances.append(self)

        async def close(self, *, abort_ssl: bool = False) -> None:
            assert abort_ssl is True
            cleanup_entered.set()
            await cleanup_release.wait()
            self.closed = True

    class _Content:
        async def read(self, _limit: int) -> bytes:
            await asyncio.sleep(0.04)
            return b""

    class _Response:
        status = 204
        content = _Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _tb) -> bool:
            return False

    class _Session:
        def __init__(self, *, connector, **_kwargs) -> None:
            self.connector = connector
            self.closed = False
            self.request_count = 0
            session_instances.append(self)

        def get(self, *_args, **_kwargs):
            self.request_count += 1
            return _Response()

        async def close(self) -> None:
            await self.connector.close(abort_ssl=True)
            self.closed = True

    runtime = ValidatorHelperRuntime(
        _Connection(),
        {
            "worker_id": 1,
            "worker_generation": 1,
            "binary_path": "fixture",
            "probe_urls": ["http://fixture/completed-before-cleanup"],
            "expected_statuses": [204],
            "probe_timeout_seconds": 5.0,
            "slow_threshold_ms": 3000,
        },
    )

    async def exercise() -> tuple[
        EndpointAttemptResult,
        EndpointAttemptResult,
        float,
        int,
        int,
    ]:
        nonlocal cleanup_entered, cleanup_release
        cleanup_entered = asyncio.Event()
        cleanup_release = asyncio.Event()

        async def _reload(_self, _node, _cmd):
            return True, "", 1

        runtime._reload_candidate = types.MethodType(_reload, runtime)
        def command(sequence: int) -> ValidateEndpointCmd:
            return ValidateEndpointCmd(
                refresh_epoch="completed-before-cleanup",
                candidate_digest=f"{sequence:x}" * 64,
                config_digest=f"{sequence + 1:x}" * 64,
                lease_id=f"lease-completed-before-cleanup-{sequence}",
                sequence=sequence,
                node_json=canonical_json(
                    Node(
                        name=f"completed-before-cleanup-{sequence}",
                        protocol="http",
                        address="127.0.0.1",
                        port=9,
                        tier="tier3",
                    ).to_dict()
                ),
                source_root="root",
                source_family="http",
                endpoint="http://fixture/completed-before-cleanup",
                probe_budget_seconds=0.05,
            )

        with (
            patch("tier3_validator_runtime.aiohttp.TCPConnector", _Connector),
            patch("tier3_validator_runtime.aiohttp.ClientSession", _Session),
        ):
            started = time.monotonic()
            first = await runtime.validate_endpoint(command(1))
            second = await runtime.validate_endpoint(command(2))
            elapsed = time.monotonic() - started
            assert not cleanup_entered.is_set()
            close_task = asyncio.create_task(runtime.close())
            await asyncio.wait_for(cleanup_entered.wait(), timeout=0.1)
            assert not close_task.done()
            cleanup_release.set()
            await close_task
        return (
            first,
            second,
            elapsed,
            len(connector_instances),
            len(session_instances),
        )

    first, second, elapsed, connector_count, session_count = asyncio.run(exercise())

    assert elapsed < 0.2
    assert first.kind is EndpointAttemptKind.PASS
    assert second.kind is EndpointAttemptKind.PASS
    assert connector_count == 1
    assert session_count == 1
    assert session_instances[0].request_count == 2
    assert runtime.probe_session is None
    assert runtime.probe_connector is None


def test_pre_transport_facility_failure_does_not_echo_candidate_attempt_ledger() -> None:
    class _Connection:
        def send_bytes(self, _payload) -> None:
            return None

    runtime = ValidatorHelperRuntime(
        _Connection(),
        {
            "worker_id": 1,
            "worker_generation": 1,
            "binary_path": "fixture",
            "probe_urls": ["http://fixture/probe"],
            "expected_statuses": [204],
            "probe_timeout_seconds": 5.0,
            "slow_threshold_ms": 3000,
        },
    )

    async def _reload(_self, _node, _cmd):
        return False, "reload_status:400", 17

    runtime._reload_candidate = types.MethodType(_reload, runtime)
    historical = ProbeAttemptFact(
        endpoint="http://fixture/previous",
        elapsed_ms=3000,
        full_budget=False,
        completed=False,
        entitlement_exhausted=True,
        outcome="right_censored",
        attempt_id="previous-attempt",
    )
    command = ValidateEndpointCmd(
        refresh_epoch="facility-no-transport",
        candidate_digest="f" * 64,
        config_digest="e" * 64,
        lease_id="lease-facility-no-transport",
        sequence=1,
        node_json=canonical_json(
            Node(
                name="facility-no-transport",
                protocol="http",
                address="127.0.0.1",
                port=9,
                tier="tier3",
            ).to_dict()
        ),
        source_root="root",
        source_family="http",
        endpoint="http://fixture/probe",
        attempt_facts=(historical,),
    )
    try:
        result = asyncio.run(runtime.validate_endpoint(command))
    finally:
        asyncio.run(runtime.close())

    assert result.kind is EndpointAttemptKind.FACILITY_FAIL
    assert result.reason == "reload_status:400"
    assert result.attempt_facts == ()


def test_helper_clean_confirmation_preserves_stop_reason() -> None:
    class _Connection:
        def __init__(self) -> None:
            self.messages: list[bytes] = []

        def send_bytes(self, payload: bytes) -> None:
            self.messages.append(payload)

    connection = _Connection()
    runtime = ValidatorHelperRuntime(
        connection,
        {
            "worker_id": 1,
            "worker_generation": 1,
            "binary_path": "fixture",
            "probe_urls": ["http://fixture/slow"],
            "expected_statuses": [204],
            "probe_timeout_seconds": 5.0,
            "slow_threshold_ms": 3000,
        },
    )
    command = ValidateEndpointCmd(
        refresh_epoch="helper-stop",
        candidate_digest="a" * 64,
        config_digest="b" * 64,
        lease_id="lease-helper-stop",
        sequence=1,
        node_json="{}",
        source_root="root",
        source_family="vless",
    )

    async def _cancel() -> None:
        runtime.active_cmd = command
        runtime.active_task = asyncio.create_task(asyncio.sleep(60.0))
        await runtime._cancel_active(
            CancelCmd(
                refresh_epoch=command.refresh_epoch,
                lease_id=command.lease_id,
                worker_id=1,
                worker_generation=1,
                sequence=1,
                reason="helper_stop",
            )
        )
        await runtime.close()

    asyncio.run(_cancel())
    kind, payload = parse_message(connection.messages[-1])
    assert kind == "clean_confirmed"
    assert payload["reason"] == "helper_stop"


def test_helper_cancel_aborts_probe_session_before_clean_confirmation() -> None:
    class _Connection:
        def __init__(self) -> None:
            self.messages: list[bytes] = []

        def send_bytes(self, payload: bytes) -> None:
            self.messages.append(payload)

    class _Session:
        def __init__(self) -> None:
            self.closed = False

        def detach(self) -> None:
            self.closed = True

    class _Connector:
        def __init__(self, released: asyncio.Event) -> None:
            self.released = released
            self.abort_ssl: bool | None = None

        async def close(self, *, abort_ssl: bool = False) -> None:
            self.abort_ssl = abort_ssl
            self.released.set()

    connection = _Connection()
    runtime = ValidatorHelperRuntime(
        connection,
        {
            "worker_id": 1,
            "worker_generation": 1,
            "binary_path": "fixture",
            "probe_urls": ["http://fixture/slow"],
            "expected_statuses": [204],
            "probe_timeout_seconds": 5.0,
            "slow_threshold_ms": 3000,
        },
    )
    command = ValidateEndpointCmd(
        refresh_epoch="helper-cancel-abort",
        candidate_digest="a" * 64,
        config_digest="b" * 64,
        lease_id="lease-helper-cancel-abort",
        sequence=1,
        node_json="{}",
        source_root="root",
        source_family="vless",
    )

    async def exercise() -> tuple[float, bool | None]:
        released = asyncio.Event()
        session = _Session()
        connector = _Connector(released)

        async def active_probe() -> None:
            try:
                await asyncio.sleep(60.0)
            except asyncio.CancelledError:
                await released.wait()
                raise

        runtime.probe_session = session  # type: ignore[assignment]
        runtime.probe_connector = connector  # type: ignore[assignment]
        runtime.active_cmd = command
        runtime.active_task = asyncio.create_task(active_probe())  # type: ignore[assignment]
        started = time.monotonic()
        await runtime._cancel_active(
            CancelCmd(
                refresh_epoch=command.refresh_epoch,
                lease_id=command.lease_id,
                worker_id=1,
                worker_generation=1,
                sequence=1,
                reason="candidate_probe_deadline",
            )
        )
        elapsed = time.monotonic() - started
        await runtime.close()
        return elapsed, connector.abort_ssl

    elapsed, abort_ssl = asyncio.run(exercise())
    kind, payload = parse_message(connection.messages[-1])

    # Leave deterministic room inside the supervisor's 1.5s physical cleanup
    # entitlement for Pipe delivery and its 50ms polling cadence.
    assert elapsed < 0.9
    assert abort_ssl is True
    assert kind == "clean_confirmed"
    assert payload["active_probe_count"] == 0
    assert runtime.probe_session is None
    assert runtime.probe_connector is None


def test_supervisor_retires_helper_that_reports_unclean_endpoint_result() -> None:
    attempts: list[EndpointAttemptResult] = []
    supervisor = Tier3ValidatorSupervisor(
        binary_path="fixture",
        probe_urls=["http://fixture/slow"],
        expected_statuses={204},
        probe_timeout_seconds=5.0,
        slow_threshold_ms=3000,
        attempt_sink=attempts.append,
        capacity_sink=lambda _count: None,
        min_workers=1,
        max_workers=1,
    )
    command = ValidateEndpointCmd(
        refresh_epoch="unclean-result",
        candidate_digest="a" * 64,
        config_digest="b" * 64,
        lease_id="lease-unclean-result",
        sequence=1,
        node_json="{}",
        source_root="root",
        source_family="vless",
        endpoint="http://fixture/slow",
    )
    fact = ProbeAttemptFact(
        endpoint=command.endpoint,
        elapsed_ms=3000,
        full_budget=False,
        completed=False,
        outcome="right_censored",
        lifecycle="censored",
        stage=command.stage.value,
    )
    result = EndpointAttemptResult(
        refresh_epoch=command.refresh_epoch,
        transaction_id=command.transaction_id,
        candidate_digest=command.candidate_digest,
        config_digest=command.config_digest,
        lease_id=command.lease_id,
        slice_id=command.slice_id,
        sequence=command.sequence,
        stage=command.stage,
        purpose=command.purpose,
        worker_id=1,
        worker_generation=1,
        kind=EndpointAttemptKind.RIGHT_CENSORED,
        node_json=command.node_json,
        reason="endpoint_entitlement_expired",
        attempt_facts=(fact,),
        clean_confirmed=False,
        probe_policy_revision=command.probe_policy_revision,
        portfolio_digest=command.portfolio_digest,
    )
    worker = types.SimpleNamespace(
        command=command,
        worker_id=1,
        generation=1,
        sealed_lease_id="",
        transport_started_at=1.0,
    )

    with patch.object(supervisor, "_retire_worker") as retire:
        supervisor._handle_endpoint_result(worker, dataclass_payload(result))

    assert attempts == [result]
    retire.assert_called_once_with(
        worker,
        "helper_endpoint_result_unclean",
        terminal_kind=None,
    )


def test_helper_ready_retries_lagging_job_membership_without_blocking_pipe_owner() -> None:
    capacities: list[int] = []

    class _LaggingJob:
        def __init__(self) -> None:
            self.calls = 0

        def snapshot(self):
            self.calls += 1
            process_ids = (101,) if self.calls == 1 else (101, 202)
            return types.SimpleNamespace(
                active_processes=len(process_ids),
                process_ids=process_ids,
            )

    supervisor = Tier3ValidatorSupervisor(
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=5.0,
        slow_threshold_ms=3000,
        capacity_sink=capacities.append,
        attempt_sink=lambda _result: None,
    )
    worker = _WorkerGeneration(
        worker_id=1,
        generation=1,
        process=types.SimpleNamespace(pid=101),  # type: ignore[arg-type]
        connection=types.SimpleNamespace(),  # type: ignore[arg-type]
        job=_LaggingJob(),  # type: ignore[arg-type]
        phase=Tier3WorkerPhase.STARTING,
        mixed_port=18080,
        controller_port=19090,
    )
    supervisor._workers[worker.worker_id] = worker

    with patch.object(supervisor, "_retire_worker") as retire:
        supervisor._handle_ready(
            worker,
            {
                "helper_pid": 101,
                "mihomo_pid": 202,
                "mixed_port": 18080,
                "controller_port": 19090,
            },
        )
        assert worker.phase is Tier3WorkerPhase.STARTING
        assert worker.startup_phase == "membership_pending"
        assert capacities == []
        retire.assert_not_called()

        supervisor._check_deadlines()

    assert worker.phase is Tier3WorkerPhase.IDLE
    assert worker.startup_phase == "ready"
    assert capacities == [1]


def test_supervisor_classifies_clean_helper_stop_as_cancelled() -> None:
    terminals: list[ValidationTerminal] = []
    attempts: list[EndpointAttemptResult] = []
    capacities: list[int] = []
    supervisor = Tier3ValidatorSupervisor(
        binary_path="fixture",
        probe_urls=["http://fixture/slow"],
        expected_statuses={204},
        probe_timeout_seconds=5.0,
        slow_threshold_ms=3000,
        attempt_sink=attempts.append,
        capacity_sink=capacities.append,
        min_workers=1,
        max_workers=1,
    )
    command = ValidateEndpointCmd(
        refresh_epoch="helper-stop",
        candidate_digest="a" * 64,
        config_digest="b" * 64,
        lease_id="lease-helper-stop",
        sequence=1,
        node_json="{}",
        source_root="root",
        source_family="vless",
    )
    worker = types.SimpleNamespace(
        command=command,
        worker_id=1,
        generation=1,
        helper_pid=10,
        mihomo_pid=11,
        sealed_lease_id=command.lease_id,
        cancel_reason="helper_stop",
        cancel_requested_monotonic_ns=1,
        deadline_at=1.0,
        cancel_deadline_at=1.0,
        phase=Tier3WorkerPhase.CANCELLING,
        heartbeat_at=0.0,
    )

    supervisor._handle_clean_confirmed(
        worker,
        {"lease_id": command.lease_id, "reason": "helper_stop"},
    )

    assert not terminals
    assert attempts[0].kind is EndpointAttemptKind.CANCELLED
    assert attempts[0].reason == "helper_stop"
    assert capacities == [1]


def test_cancelling_worker_uses_only_fresh_idle_heartbeat_as_cleanup_evidence() -> None:
    attempts: list[EndpointAttemptResult] = []
    capacities: list[int] = []
    supervisor = Tier3ValidatorSupervisor(
        binary_path="fixture",
        probe_urls=["http://fixture/slow"],
        expected_statuses={204},
        probe_timeout_seconds=5.0,
        slow_threshold_ms=3000,
        attempt_sink=attempts.append,
        capacity_sink=capacities.append,
        min_workers=1,
        max_workers=1,
    )
    command = ValidateEndpointCmd(
        refresh_epoch="heartbeat-clean",
        candidate_digest="c" * 64,
        config_digest="d" * 64,
        lease_id="lease-heartbeat-clean",
        sequence=3,
        node_json="{}",
        source_root="root",
        source_family="vless",
    )
    worker = types.SimpleNamespace(
        command=command,
        worker_id=1,
        generation=2,
        helper_pid=10,
        mihomo_pid=11,
        sealed_lease_id=command.lease_id,
        cancel_reason="candidate_probe_deadline",
        cancel_requested_monotonic_ns=100,
        deadline_at=1.0,
        lease_hard_deadline_at=2.0,
        deadline_phase="probe",
        transport_started_at=1.0,
        cancel_deadline_at=3.0,
        phase=Tier3WorkerPhase.CANCELLING,
        reservation_id=0,
        heartbeat_at=0.0,
    )

    supervisor._handle_heartbeat(
        worker,
        {
            "worker_id": 1,
            "worker_generation": 2,
            "mihomo_pid": 11,
            "lease_id": command.lease_id,
            "active_probe_count": 1,
            "monotonic_ns": 101,
        },
    )
    supervisor._handle_heartbeat(
        worker,
        {
            "worker_id": 1,
            "worker_generation": 2,
            "mihomo_pid": 11,
            "lease_id": "",
            "active_probe_count": 0,
            "monotonic_ns": 99,
        },
    )
    assert worker.command is command
    assert attempts == []
    assert capacities == []

    supervisor._handle_heartbeat(
        worker,
        {
            "worker_id": 1,
            "worker_generation": 2,
            "mihomo_pid": 11,
            "lease_id": "",
            "active_probe_count": 0,
            "monotonic_ns": 101,
        },
    )

    assert worker.command is None
    assert worker.phase is Tier3WorkerPhase.IDLE
    assert attempts[0].kind is EndpointAttemptKind.RIGHT_CENSORED
    assert attempts[0].reason == "candidate_probe_deadline"
    assert attempts[0].clean_confirmed is True
    assert capacities == [1]


def test_supervisor_snapshot_counts_failure_categories() -> None:
    supervisor = Tier3ValidatorSupervisor(
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        attempt_sink=lambda _result: None,
        capacity_sink=lambda _count: None,
        min_workers=1,
        max_workers=1,
    )

    supervisor._record_spawn_failure(1, TimeoutError("slow start"))
    supervisor._record_failure_reason("probe_deadline_cleanup_timeout")

    assert dict(supervisor.snapshot().failure_reason_counts) == {
        "probe_deadline_cleanup_timeout": 1,
        "worker_spawn_failed": 1,
    }


def test_endpoint_result_delivered_before_scaled_delivery_slack_is_accepted() -> None:
    attempts = []
    supervisor = Tier3ValidatorSupervisor(
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        attempt_sink=attempts.append,
        capacity_sink=lambda _count: None,
        min_workers=1,
        max_workers=1,
        cleanup_seconds=0.1,
        helper_target=deterministic_helper_bootstrap,
    )
    supervisor.start()
    try:
        _wait_until(lambda: supervisor.snapshot().idle_workers == 1)
        supervisor.submit(
            ValidateEndpointCmd(
                refresh_epoch="delivery-slack",
                candidate_digest="0" * 64,
                config_digest="c" * 64,
                lease_id="lease-before-seal",
                sequence=1,
                node_json=canonical_json(
                    Node(
                        name="before-seal",
                        protocol="http",
                        address="127.0.0.1",
                        port=9,
                        tier="tier3",
                    ).to_dict()
                ),
                report_json='{"fixture_delay_seconds":0.42}',
                source_root="root",
                source_family="http",
                endpoint="http://fixture/probe",
                candidate_hard_cap_seconds=0.6,
            )
        )
        _wait_until(lambda: bool(attempts), timeout=2.0)
        assert attempts[0].kind is EndpointAttemptKind.PASS
        assert attempts[0].reason == "deterministic_pass"
    finally:
        assert supervisor.stop(timeout_seconds=3.0) == "STOPPED"


def test_transport_start_does_not_shorten_candidate_hard_guard(monkeypatch) -> None:
    supervisor = Tier3ValidatorSupervisor(
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=5.0,
        slow_threshold_ms=3000,
        attempt_sink=lambda _result: None,
        capacity_sink=lambda _count: None,
        min_workers=1,
        max_workers=1,
    )
    command = ValidateEndpointCmd(
        refresh_epoch="phase-hard-guard",
        candidate_digest="a" * 64,
        config_digest="b" * 64,
        lease_id="phase-hard-guard-lease",
        sequence=1,
        node_json="{}",
        source_root="root",
        source_family="vless",
        facility_budget_seconds=2.0,
        probe_budget_seconds=3.0,
        candidate_hard_cap_seconds=5.75,
    )
    worker = types.SimpleNamespace(
        command=command,
        deadline_at=0.0,
        lease_hard_deadline_at=0.0,
        deadline_phase="dispatch_ack",
        transport_started_at=0.0,
    )
    clock = iter((99.8, 100.0, 100.2))
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))

    supervisor._handle_slice_phase(
        worker,
        {
            "lease_id": command.lease_id,
            "slice_id": command.slice_id,
            "stage": command.stage.value,
            "phase": "dispatch_received",
        },
    )
    assert worker.deadline_phase == "facility_start"
    assert abs(worker.deadline_at - 100.8) < 1e-9

    supervisor._handle_slice_phase(
        worker,
        {
            "lease_id": command.lease_id,
            "slice_id": command.slice_id,
            "stage": command.stage.value,
            "phase": "facility_started",
        },
    )
    assert abs(worker.deadline_at - 102.75) < 1e-9
    assert abs(worker.lease_hard_deadline_at - 105.75) < 1e-9

    supervisor._handle_slice_phase(
        worker,
        {
            "lease_id": command.lease_id,
            "slice_id": command.slice_id,
            "stage": command.stage.value,
            "phase": "transport_started",
        },
    )
    assert worker.deadline_phase == "probe"
    assert abs(worker.deadline_at - 105.75) < 1e-9


def test_helper_acknowledges_dispatch_before_starting_facility_budget() -> None:
    command = ValidateEndpointCmd(
        refresh_epoch="dispatch-phase-order",
        candidate_digest="a" * 64,
        config_digest="b" * 64,
        lease_id="dispatch-phase-order-lease",
        sequence=1,
        node_json=canonical_json(
            Node(
                name="dispatch-phase-order",
                protocol="http",
                address="127.0.0.1",
                port=9,
                tier="tier3",
            ).to_dict()
        ),
        source_root="root",
        source_family="http",
        worker_id=1,
        worker_generation=1,
        endpoint="http://fixture/probe",
    )

    class _Connection:
        def __init__(self) -> None:
            self.inbound = [
                message_bytes("validate_endpoint", dataclass_payload(command))
            ]
            self.outbound: list[bytes] = []

        def poll(self, _timeout: float = 0.0) -> bool:
            return bool(self.inbound)

        def recv_bytes(self) -> bytes:
            return self.inbound.pop(0)

        def send_bytes(self, payload: bytes) -> None:
            self.outbound.append(payload)
            kind, _message = parse_message(payload)
            if kind == "endpoint_result":
                self.inbound.append(message_bytes("stop", {}))

    connection = _Connection()
    runtime = ValidatorHelperRuntime(
        connection,
        {
            "worker_id": 1,
            "worker_generation": 1,
            "binary_path": "fixture",
            "probe_urls": ["http://fixture/probe"],
            "expected_statuses": [204],
            "probe_timeout_seconds": 5.0,
            "slow_threshold_ms": 3000,
        },
    )

    async def _start(_self) -> None:
        return None

    async def _reload(_self, _node, _cmd):
        return False, "fixture_facility_failure", 0

    runtime.start = types.MethodType(_start, runtime)
    runtime._reload_candidate = types.MethodType(_reload, runtime)
    try:
        asyncio.run(asyncio.wait_for(runtime.run(), timeout=1.0))
    finally:
        asyncio.run(runtime.close())

    phases = [
        str(payload.get("phase") or "")
        for raw in connection.outbound
        for kind, payload in (parse_message(raw),)
        if kind == "slice_phase"
    ]
    assert phases[:2] == ["dispatch_received", "facility_started"]


def test_supervisor_drains_queued_phase_ack_before_dispatch_deadline(monkeypatch) -> None:
    class _QueuedConnection:
        def __init__(self, messages: list[bytes]) -> None:
            self.messages = list(messages)

        def poll(self, _timeout: float = 0.0) -> bool:
            return bool(self.messages)

        def recv_bytes(self) -> bytes:
            return self.messages.pop(0)

    supervisor = Tier3ValidatorSupervisor(
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=5.0,
        slow_threshold_ms=3000,
        attempt_sink=lambda _result: None,
        capacity_sink=lambda _count: None,
        min_workers=1,
        max_workers=1,
    )
    command = ValidateEndpointCmd(
        refresh_epoch="queued-phase-ack",
        candidate_digest="a" * 64,
        config_digest="b" * 64,
        lease_id="queued-phase-ack-lease",
        sequence=1,
        node_json="{}",
        source_root="root",
        source_family="vless",
        facility_budget_seconds=2.0,
        probe_budget_seconds=3.0,
        candidate_hard_cap_seconds=5.75,
    )
    connection = _QueuedConnection(
        [
            message_bytes("heartbeat", {"mihomo_pid": 11}),
            message_bytes(
                "slice_phase",
                {
                    "lease_id": command.lease_id,
                    "slice_id": command.slice_id,
                    "stage": command.stage.value,
                    "phase": "facility_started",
                },
            ),
        ]
    )
    worker = types.SimpleNamespace(
        worker_id=1,
        generation=1,
        connection=connection,
        command=command,
        phase=Tier3WorkerPhase.VALIDATING,
        deadline_at=99.0,
        lease_hard_deadline_at=105.0,
        deadline_phase="dispatch_ack",
        transport_started_at=0.0,
        cancel_deadline_at=0.0,
        sealed_lease_id="",
        heartbeat_at=0.0,
        mihomo_pid=10,
        stop_requested=False,
    )
    supervisor._workers[worker.worker_id] = worker
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)
    cancelled: list[str] = []
    monkeypatch.setattr(
        supervisor,
        "_send",
        lambda _worker, kind, _payload: cancelled.append(kind) or True,
    )
    try:
        assert supervisor._drain_worker_pipe(worker) == 2
        assert worker.mihomo_pid == 11
        assert worker.deadline_phase == "facility"
        assert worker.deadline_at == 102.75

        supervisor._check_deadlines()

        assert worker.phase is Tier3WorkerPhase.VALIDATING
        assert cancelled == []
    finally:
        supervisor._workers.clear()


def test_supervisor_accepts_pre_transport_facility_result_without_attempt_fact(
    monkeypatch,
) -> None:
    attempts: list[EndpointAttemptResult] = []
    capacities: list[int] = []
    retired: list[str] = []
    supervisor = Tier3ValidatorSupervisor(
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        attempt_sink=attempts.append,
        capacity_sink=capacities.append,
        min_workers=1,
        max_workers=1,
    )
    command = ValidateEndpointCmd(
        refresh_epoch="facility-before-transport",
        candidate_digest="a" * 64,
        config_digest="b" * 64,
        lease_id="lease-facility",
        sequence=1,
        node_json="{}",
        source_root="root",
        source_family="vless",
        endpoint="http://fixture/probe",
    )
    worker = types.SimpleNamespace(
        command=command,
        worker_id=1,
        generation=1,
        sealed_lease_id="",
        deadline_at=1.0,
        deadline_phase="facility",
        transport_started_at=0.0,
        cancel_deadline_at=0.0,
        phase=None,
        heartbeat_at=0.0,
    )
    monkeypatch.setattr(
        supervisor,
        "_retire_worker",
        lambda _worker, reason, **_kwargs: retired.append(reason),
    )

    supervisor._handle_endpoint_result(
        worker,
        dataclass_payload(
            EndpointAttemptResult(
                refresh_epoch=command.refresh_epoch,
                transaction_id=command.transaction_id,
                candidate_digest=command.candidate_digest,
                config_digest=command.config_digest,
                lease_id=command.lease_id,
                slice_id=command.slice_id,
                sequence=command.sequence,
                stage=command.stage,
                worker_id=1,
                worker_generation=1,
                node_json=command.node_json,
                kind=EndpointAttemptKind.FACILITY_FAIL,
                reason="reload_status:400",
                attempt_facts=(),
                probe_policy_revision=command.probe_policy_revision,
                portfolio_digest=command.portfolio_digest,
            )
        ),
    )

    assert retired == []
    assert len(attempts) == 1
    assert attempts[0].kind is EndpointAttemptKind.FACILITY_FAIL
    assert attempts[0].attempt_facts == ()
    assert capacities == [1]


def test_supervisor_rejects_probe_result_without_current_attempt_fact(
    monkeypatch,
) -> None:
    attempts: list[EndpointAttemptResult] = []
    retired: list[str] = []
    supervisor = Tier3ValidatorSupervisor(
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        attempt_sink=attempts.append,
        capacity_sink=lambda _count: None,
        min_workers=1,
        max_workers=1,
    )
    command = ValidateEndpointCmd(
        refresh_epoch="missing-attempt-fact",
        candidate_digest="c" * 64,
        config_digest="d" * 64,
        lease_id="lease-missing-fact",
        sequence=1,
        node_json="{}",
        source_root="root",
        source_family="vless",
        endpoint="http://fixture/probe",
    )
    worker = types.SimpleNamespace(
        command=command,
        worker_id=1,
        generation=1,
        sealed_lease_id="",
    )
    monkeypatch.setattr(
        supervisor,
        "_retire_worker",
        lambda _worker, reason, **_kwargs: retired.append(reason),
    )

    supervisor._handle_endpoint_result(
        worker,
        dataclass_payload(
            EndpointAttemptResult(
                refresh_epoch=command.refresh_epoch,
                transaction_id=command.transaction_id,
                candidate_digest=command.candidate_digest,
                config_digest=command.config_digest,
                lease_id=command.lease_id,
                slice_id=command.slice_id,
                sequence=command.sequence,
                stage=command.stage,
                worker_id=1,
                worker_generation=1,
                node_json=command.node_json,
                kind=EndpointAttemptKind.PASS,
                reason="endpoint_probe_passed",
                attempt_facts=(),
                probe_policy_revision=command.probe_policy_revision,
                portfolio_digest=command.portfolio_digest,
            )
        ),
    )

    assert attempts == []
    assert retired == ["helper_endpoint_result_protocol_mismatch"]


def test_endpoint_controller_preserves_configured_order_until_paired_evidence() -> None:
    endpoint_a = "http://a.example/generate_204?secret=one"
    endpoint_b = "http://b.example/generate_204?secret=two"
    endpoint_c = "https://c.example/generate_204?secret=three"
    controller = Tier3EndpointController([endpoint_a, endpoint_b, endpoint_c])

    initial_decisions = [
        controller.decision(f"candidate-{index}")
        for index in range(64)
    ]
    assert all(
        decision.endpoints == (endpoint_a, endpoint_b, endpoint_c)
        for decision in initial_decisions
    )
    assert all(decision.configured_order for decision in initial_decisions)
    initial_revision = initial_decisions[0].policy_revision

    # Promotion may be learned early, but cannot affect routing until the
    # first 32 FAST entitlements have covered the complete portfolio.
    for index in range(32):
        controller.slice_decision(
            f"warmup-{index}",
            ValidationStage.FAST_PROBE,
        )

    for index in range(8):
        controller.observe(
            ValidationTerminal(
                refresh_epoch="endpoint",
                candidate_digest=f"digest-{index}",
                config_digest="config",
                lease_id=f"lease-{index}",
                sequence=index,
                worker_id=1,
                worker_generation=1,
                kind=Tier3TerminalKind.PASS,
                node_json="{}",
                probe_url=endpoint_b,
                attempt_facts=(
                    ProbeAttemptFact(
                        endpoint=endpoint_a,
                        elapsed_ms=100,
                        full_budget=True,
                        completed=True,
                        outcome="transient",
                    ),
                    ProbeAttemptFact(
                        endpoint=endpoint_b,
                        elapsed_ms=100,
                        full_budget=True,
                        completed=True,
                        outcome="pass",
                    ),
                ),
            ),
            root=f"root-{index % 2}",
            protocol="vless" if index % 2 == 0 else "trojan",
        )

    decisions = [controller.decision(f"after-{index}") for index in range(400)]
    selected_b = sum(decision.endpoints[0] == endpoint_b for decision in decisions)
    assert 270 <= selected_b <= 330
    assert all(
        decision.endpoints in {
            (endpoint_a, endpoint_b, endpoint_c),
            (endpoint_b, endpoint_a, endpoint_c),
        }
        for decision in decisions
    )
    assert all(decision.policy_revision > initial_revision for decision in decisions)
    assert all(decision.portfolio_digest for decision in decisions)
    assert all("secret=" not in key for key in controller.snapshot())


def test_endpoint_controller_balances_fast_and_preserves_deep_continuation_order() -> None:
    endpoint_a = "http://a.example/generate_204"
    endpoint_b = "http://b.example/generate_204"
    endpoint_c = "https://c.example/generate_204"
    controller = Tier3EndpointController([endpoint_c, endpoint_a, endpoint_b])

    fast = [
        controller.slice_decision(
            f"candidate-{index}",
            ValidationStage.FAST_PROBE,
        ).endpoints[0]
        for index in range(256)
    ]
    assert set(fast) == {endpoint_a, endpoint_b, endpoint_c}
    assert all(fast[:32].count(endpoint) >= 10 for endpoint in (endpoint_a, endpoint_b, endpoint_c))

    censored = ProbeAttemptFact(
        endpoint=endpoint_a,
        elapsed_ms=3000,
        full_budget=False,
        completed=False,
        outcome="right_censored",
        lifecycle="censored",
        stage=ValidationStage.FAST_PROBE.value,
    )
    deep = controller.slice_decision(
        "candidate-deep",
        ValidationStage.CANONICAL_DEEP,
        attempt_facts=(censored,),
        remaining_urls=(endpoint_c, endpoint_b),
    )
    assert deep.endpoints == (endpoint_c, endpoint_a, endpoint_b)


def test_deep_runtime_restores_configured_order_without_positive_fast_evidence() -> None:
    class _Connection:
        def send_bytes(self, _payload) -> None:
            return None

    endpoint_a = "http://a.example/generate_204"
    endpoint_b = "http://b.example/generate_204"
    endpoint_c = "https://configured.example/generate_204"
    controller = Tier3EndpointController([endpoint_c, endpoint_a, endpoint_b])
    fast_censored = ProbeAttemptFact(
        endpoint=endpoint_a,
        elapsed_ms=3000,
        full_budget=False,
        completed=False,
        outcome="right_censored",
        lifecycle="censored",
        stage=ValidationStage.FAST_PROBE.value,
    )
    decision = controller.slice_decision(
        "candidate-runtime-deep",
        ValidationStage.CANONICAL_DEEP,
        attempt_facts=(fast_censored,),
        remaining_urls=(endpoint_c, endpoint_b, endpoint_a),
    )
    assert decision.endpoints == (endpoint_c, endpoint_a, endpoint_b)

    runtime = ValidatorHelperRuntime(
        _Connection(),
        {
            "worker_id": 1,
            "worker_generation": 1,
            "binary_path": "fixture",
            "probe_urls": list(decision.endpoints),
            "expected_statuses": [204],
            "probe_timeout_seconds": 5.0,
            "slow_threshold_ms": 3000,
        },
    )
    attempted: list[str] = []

    async def _reload(_self, _node, _cmd):
        return True, "", 1

    async def _probe(_self, endpoint, _budget, _cmd, _attempt_index):
        attempted.append(endpoint)
        passed = endpoint == endpoint_a
        return (
            ProbeAttemptFact(
                endpoint=endpoint,
                elapsed_ms=25,
                full_budget=True,
                completed=True,
                status_code=204 if passed else 502,
                outcome="pass" if passed else "transient",
                failure_domain="none" if passed else "candidate_or_endpoint",
                lifecycle="completed",
                stage=ValidationStage.CANONICAL_DEEP.value,
            ),
            passed,
            25.0 if passed else None,
        )

    runtime._reload_candidate = types.MethodType(_reload, runtime)
    runtime._probe_once = types.MethodType(_probe, runtime)
    command = ValidateEndpointCmd(
        refresh_epoch="deep-order-runtime",
        candidate_digest="e" * 64,
        config_digest="f" * 64,
        lease_id="lease-deep-order",
        sequence=1,
        node_json=canonical_json(
            Node(
                name="deep-order",
                protocol="http",
                address="127.0.0.1",
                port=9,
                tier="tier3",
            ).to_dict()
        ),
        source_root="root",
        source_family="http",
        stage=ValidationStage.CANONICAL_DEEP,
        endpoint=decision.endpoints[0],
        probe_budget_seconds=5.0,
        attempt_facts=(fast_censored,),
        probe_policy_revision=decision.policy_revision,
        portfolio_digest=decision.portfolio_digest,
    )
    try:
        terminal = asyncio.run(runtime.validate_endpoint(command))
    finally:
        asyncio.run(runtime.close())

    assert terminal.kind is EndpointAttemptKind.TRANSIENT
    assert attempted == [endpoint_c]


def test_deep_runtime_reaches_https_after_http_portfolio_is_transient() -> None:
    class _Connection:
        def send_bytes(self, _payload) -> None:
            return None

    endpoint_a = "http://a.example/generate_204"
    endpoint_b = "http://b.example/generate_204"
    endpoint_c = "https://configured.example/generate_204"
    runtime = ValidatorHelperRuntime(
        _Connection(),
        {
            "worker_id": 1,
            "worker_generation": 1,
            "binary_path": "fixture",
            "probe_urls": [endpoint_a, endpoint_b, endpoint_c],
            "expected_statuses": [204],
            "probe_timeout_seconds": 5.0,
            "slow_threshold_ms": 3000,
        },
    )
    attempted: list[str] = []

    async def _reload(_self, _node, _cmd):
        return True, "", 1

    async def _probe(_self, endpoint, _budget, _cmd, _attempt_index):
        attempted.append(endpoint)
        passed = endpoint == endpoint_c
        return (
            ProbeAttemptFact(
                endpoint=endpoint,
                elapsed_ms=4200 if passed else 50,
                full_budget=True,
                completed=True,
                status_code=204 if passed else 502,
                outcome="pass" if passed else "transient",
                failure_domain="none" if passed else "candidate_or_endpoint",
                lifecycle="completed",
                stage=ValidationStage.CANONICAL_DEEP.value,
            ),
            passed,
            4200.0 if passed else None,
        )

    runtime._reload_candidate = types.MethodType(_reload, runtime)
    runtime._probe_once = types.MethodType(_probe, runtime)
    command = ValidateEndpointCmd(
        refresh_epoch="deep-https-recovery",
        candidate_digest="d" * 64,
        config_digest="e" * 64,
        lease_id="lease-deep-https",
        sequence=1,
        node_json=canonical_json(
            Node(
                name="deep-https",
                protocol="http",
                address="127.0.0.1",
                port=9,
                tier="tier3",
            ).to_dict()
        ),
        source_root="root",
        source_family="http",
        stage=ValidationStage.CANONICAL_DEEP,
        endpoint=endpoint_c,
        probe_budget_seconds=5.0,
    )
    try:
        terminal = asyncio.run(runtime.validate_endpoint(command))
    finally:
        asyncio.run(runtime.close())

    assert terminal.kind is EndpointAttemptKind.PASS
    assert terminal.probe_url == endpoint_c
    assert attempted == [endpoint_c]


def test_endpoint_controller_counts_completed_slice_attempt_once() -> None:
    endpoint_a = "http://a.example/generate_204"
    endpoint_b = "http://b.example/generate_204"
    controller = Tier3EndpointController([endpoint_a, endpoint_b])
    fact = ProbeAttemptFact(
        endpoint=endpoint_a,
        elapsed_ms=3000,
        full_budget=True,
        completed=True,
        outcome="transient",
        lifecycle="completed",
        stage=ValidationStage.FAST_PROBE.value,
        attempt_id="candidate-a:fast:1",
    )

    controller.observe_attempts(
        (fact,),
        root="root-a",
        protocol="vless",
    )
    controller.observe_attempts(
        (fact,),
        root="root-a",
        protocol="vless",
    )

    endpoint_stats = next(
        value
        for key, value in controller.snapshot().items()
        if key.startswith("http://a.example")
    )
    assert endpoint_stats["completed"] == 1
    assert endpoint_stats["successes"] == 0


def test_endpoint_controller_converges_from_stratified_fast_entitlements() -> None:
    endpoint_a = "http://a.example/generate_204"
    endpoint_b = "http://b.example/generate_204"
    controller = Tier3EndpointController([endpoint_a, endpoint_b])

    for endpoint in (endpoint_a, endpoint_b):
        for index in range(8):
            passed = endpoint == endpoint_b and index == 0
            controller.observe_attempts(
                (
                    ProbeAttemptFact(
                        endpoint=endpoint,
                        elapsed_ms=3000 if not passed else 1200,
                        full_budget=passed,
                        completed=True,
                        outcome="pass" if passed else "transient",
                        lifecycle="completed" if passed else "censored",
                        stage=ValidationStage.FAST_PROBE.value,
                        granted_budget_ms=3000,
                        attempt_id=f"{endpoint}:{index}",
                    ),
                ),
                root=f"root-{index % 2}",
                protocol="vless" if index % 2 == 0 else "trojan",
            )

    decisions = [
        controller.slice_decision(
            f"candidate-{index}",
            ValidationStage.FAST_PROBE,
        )
        for index in range(400)
    ]
    selected_b = sum(item.endpoints == (endpoint_b,) for item in decisions)
    assert 270 <= selected_b <= 330
    snapshot = controller.snapshot()
    assert snapshot["_policy"]["promotion_reason"] == "fast_worker_second_lcb"
    endpoint_a_stats = next(
        value for key, value in snapshot.items() if key.startswith("http://a.example")
    )
    assert endpoint_a_stats["fast_completed"] == 8
    assert endpoint_a_stats["fast_successes"] == 0


def test_endpoint_controller_converges_when_other_fast_endpoints_exhaust() -> None:
    endpoints = (
        "https://a.example/generate_204",
        "https://b.example/generate_204",
        "http://c.example/generate_204",
        "http://d.example/generate_204",
    )
    controller = Tier3EndpointController(endpoints)

    for endpoint in endpoints:
        for index in range(8):
            passed = endpoint == endpoints[-1] and index < 3
            controller.observe_attempts(
                (
                    ProbeAttemptFact(
                        endpoint=endpoint,
                        elapsed_ms=1200 if passed else 3000,
                        full_budget=passed,
                        completed=passed,
                        entitlement_exhausted=not passed,
                        outcome="pass" if passed else "right_censored",
                        lifecycle="completed" if passed else "censored",
                        stage=ValidationStage.FAST_PROBE.value,
                        granted_budget_ms=3000,
                        attempt_id=f"{endpoint}:{index}",
                    ),
                ),
                root=f"root-{index % 2}",
                protocol="vless" if index % 2 == 0 else "trojan",
            )

    decisions = [
        controller.slice_decision(
            f"candidate-{index}",
            ValidationStage.FAST_PROBE,
        )
        for index in range(432)
    ]
    post_warmup = decisions[32:]
    selected_best = sum(item.endpoints == (endpoints[-1],) for item in post_warmup)

    assert 270 <= selected_best <= 330
    snapshot = controller.snapshot()
    assert snapshot["_policy"]["promotion_reason"] == "fast_worker_second_lcb"
    exhausted_stats = next(
        value for key, value in snapshot.items() if key.startswith("https://a.example")
    )
    assert exhausted_stats["fast_entitlement_coverage"] == 8
    assert exhausted_stats["fast_entitlement_exhausted"] == 8
    assert exhausted_stats["fast_completed"] == 0


def test_fast_entitlement_evidence_remains_canonical_work() -> None:
    endpoint_a = "http://a.example/generate_204"
    endpoint_b = "http://b.example/generate_204"
    endpoint_c = "https://c.example/generate_204"
    controller = Tier3EndpointController([endpoint_c, endpoint_a, endpoint_b])
    fast_timeout = ProbeAttemptFact(
        endpoint=endpoint_a,
        elapsed_ms=3000,
        full_budget=False,
        completed=False,
        entitlement_exhausted=True,
        outcome="right_censored",
        lifecycle="censored",
        stage=ValidationStage.FAST_PROBE.value,
        granted_budget_ms=3000,
        attempt_id="fast-entitlement",
    )

    controller.observe_attempts(
        (fast_timeout,),
        root="root-a",
        protocol="vless",
    )
    decision = controller.slice_decision(
        "candidate-deep-entitlement",
        ValidationStage.CANONICAL_DEEP,
        attempt_facts=(fast_timeout,),
        remaining_urls=(endpoint_c, endpoint_b, endpoint_a),
    )

    assert decision.endpoints == (endpoint_c, endpoint_a, endpoint_b)
    snapshot = controller.snapshot()
    endpoint_a_stats = next(
        value for key, value in snapshot.items() if key.startswith("http://a.example")
    )
    assert endpoint_a_stats["fast_entitlement_coverage"] == 1
    assert endpoint_a_stats["fast_entitlement_exhausted"] == 1
    assert endpoint_a_stats["fast_completed"] == 0


def test_manager_source_stream_uses_process_domain_and_durable_commit() -> None:
    server = _ProxyServer(("127.0.0.1", 0), _PassProxyHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        nodes_file = root / "nodes.json"
        tier2_file = root / "tier2.json"
        exit_file = root / "exit.json"
        observation_file = root / "observations.json"
        source_nodes = root / "source-nodes.txt"
        sources_file = root / "sources.yaml"
        nodes_file.write_text(
            '{"generated_at":"","metadata":{"tier":"tier3"},"nodes":[]}',
            encoding="utf-8",
        )
        tier2_file.write_text(
            '{"generated_at":"","metadata":{},"nodes":[]}',
            encoding="utf-8",
        )
        source_nodes.write_text(
            f"http://user:pass@127.0.0.1:{server.server_address[1]}#local-proxy\n",
            encoding="utf-8",
        )
        sources_file.write_text(
            yaml.safe_dump(
                {
                    "github_repos": [],
                    "telegram_channels": [],
                    "subscription_urls": [],
                    "local_files": [str(source_nodes)],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        config = {
            "paths": {
                "sources_file": str(sources_file),
                "tier3_nodes_file": str(nodes_file),
                "tier2_cache_file": str(tier2_file),
                "exit_cache_file": str(exit_file),
                "tier_observation_cache_file": str(observation_file),
            },
            "tiers": {
                "tier1": {"nodes": [], "check_interval": 30},
                "tier2": {
                    "subscription_urls": [],
                    "cache_file": str(tier2_file),
                    "check_interval": 60,
                },
                "tier3": {
                    "enabled": True,
                    "bootstrap_enabled": True,
                    "min_alive_nodes": 1,
                    "max_pool_size": 10,
                    "validation_worker_min": 1,
                    "validation_worker_max": 1,
                    "full_validation_process_max": 1,
                    "source_stream_window_min": 2,
                    "source_stream_window_max": 2,
                    "pi_controller": {
                        "target_alive": 1,
                        "source_budget_min": 1,
                        "source_budget_max": 1,
                    },
                },
                "exit": {"cache_file": str(exit_file), "check_interval": 60},
            },
            "testing": {
                "tcp_timeout": 1.0,
                "concurrency": 2,
                "slow_threshold_ms": 3000,
                "validation_mode": "full",
                "request_timeout": 2.0,
                "mihomo_helper": {
                    "enabled": True,
                    "binary_path": str(MIHOMO),
                    "probe_urls": ["http://127.0.0.1:18080/generate_204"],
                    # Keep the admission gate on but offline: the local pass
                    # proxy answers the confirm like the default Google URL.
                    "confirm_urls": ["http://127.0.0.1:18080/generate_204"],
                    "expected_statuses": [204],
                    "probe_timeout": 2.0,
                },
            },
        }
        logger = logging.getLogger("test-tier3-manager-process-domain")
        manager = ProxyTierManager(
            config=config,
            logger=logger,
            alerts=AlertManager(
                {"desktop": {"enabled": False}},
                logger,
            ),
        )
        try:
            nodes, alive = manager.refresh_tier3(
                suppress_intervention_alert=True,
                startup_mode=True,
            )
            assert alive == 1
            assert len(nodes) == 1
            assert nodes[0].status in {NodeStatus.ALIVE, NodeStatus.SLOW}
            report = manager.tier3_last_collection_report
            assert report["source_stream_validator_execution_domain"] == "process"
            assert "source_stream_legacy_validator_lane_allocator_active" not in report
            assert report["source_stream_mihomo_process_capacity"] == 1
            assert "source_stream_reality_matrix_attempted_count" not in report
            assert "reality_matrix_attempted_count" not in report
            assert report["source_stream_process_ledger_conservation_ok"] is True
            assert report["source_stream_process_dispatched_count"] >= 1
            assert report["source_stream_process_completed_count"] >= 1
            assert report["source_stream_process_slice_dispatched_count"] >= 1
            assert report["source_stream_process_slice_completed_count"] >= 1
            assert (
                report["source_stream_process_fast_pass_count"]
                + report["source_stream_process_deep_pass_count"]
            ) == 1
            assert report["source_stream_process_cancelled_before_dispatch_count"] == 0
            assert report["source_stream_admission_commit_committed_count"] == 1
            assert report["source_stream_process_fast_ingress_window_limit"] >= 1
            assert (
                report["source_stream_process_fast_ingress_window_used"]
                <= report["source_stream_validation_ready_window_high_water"]
            )
            persisted = nodes_file.read_text(encoding="utf-8")
            assert "local-proxy" in persisted
            assert manager.tier3_pool_count_snapshot().revision == 1
        finally:
            manager.close()
            server.shutdown()
            server.server_close()


def test_process_domain_terminal_soak_has_bounded_generations() -> None:
    release_soak = os.environ.get("TESSERACT_TIER3_RELEASE_SOAK") == "1"
    worker_count = 16 if release_soak else 8
    terminal_count = 10_000 if release_soak else 1_000
    commits = []

    def commit_node(_node, _report, intent):
        commits.append(intent.candidate_digest)
        return AdmissionCommitted(
            refresh_epoch=intent.refresh_epoch,
            candidate_digest=intent.candidate_digest,
            lease_id=intent.lease_id,
            sequence=intent.sequence,
            pool_revision=len(commits),
            net_new_pool_growth=True,
        )

    domain = Tier3ValidationDomain(
        refresh_epoch=f"soak-{worker_count}-{terminal_count}",
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        commit_node=commit_node,
        terminal_observer=lambda _node, _terminal: None,
        min_workers=worker_count,
        max_workers=worker_count,
        helper_target=deterministic_helper_bootstrap,
    )
    deadline = time.monotonic() + (120.0 if release_soak else 30.0)
    domain.start()
    try:
        for index in range(terminal_count):
            node = Node(
                name=f"soak-{index}",
                protocol="http",
                address=f"10.{index // 65536}.{(index // 256) % 256}.{index % 256}",
                port=8000 + (index % 1000),
                source=f"root-{index % 5}",
                tier="tier3",
                extra={
                    "tier3_source_root_id": f"root-{index % 5}",
                    "tier3_source_unit_family": "http",
                },
            )
            assert domain.submit(node, {"configured_source_count": 5})
        assert domain.wait_idle(max(0.0, deadline - time.monotonic())), domain.snapshot()
        _wait_until(
            lambda: domain.snapshot().supervisor.active_workers == worker_count,
            timeout=max(0.0, deadline - time.monotonic()),
        )
        snapshot = domain.snapshot()
        assert snapshot.actor.submitted == terminal_count
        assert snapshot.actor.terminal == terminal_count
        assert snapshot.actor.conservation_ok
        assert snapshot.actor.admission_intents == len(commits)
        assert snapshot.actor.admission_committed == len(commits)
        assert snapshot.supervisor.active_workers == worker_count
        assert snapshot.supervisor.replacement_count == 0
        assert snapshot.supervisor.quarantined_workers == 0
        assert len(snapshot.supervisor.workers) == worker_count
    finally:
        assert domain.stop(timeout_seconds=4.0) == "STOPPED"
    assert not [
        child
        for child in multiprocessing.active_children()
        if child.name.startswith("tier3-validator-helper-")
    ]


@pytest.mark.skipif(os.environ.get("GITHUB_ACTIONS") == "true",
                    reason="speed gate against real helper processes or a real Core; "
                    "the release runner is slower than these budgets (runs locally)")
@pytest.mark.parametrize("shell_slots", [2, 8])
def test_process_domain_separates_shell_and_mihomo_start_bulkheads(shell_slots: int) -> None:
    server = _ProxyServer(("127.0.0.1", 0), _PassProxyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    domain = Tier3ValidationDomain(
        refresh_epoch="envelope-16",
        startup_bulkhead=shell_slots,
        binary_path=str(MIHOMO),
        probe_urls=["http://probe.invalid/generate_204"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        commit_node=lambda _node, _report, intent: AdmissionCommitted(
            refresh_epoch=intent.refresh_epoch,
            candidate_digest=intent.candidate_digest,
            lease_id=intent.lease_id,
            sequence=intent.sequence,
            pool_revision=intent.sequence,
            net_new_pool_growth=True,
        ),
        terminal_observer=lambda _node, _terminal: None,
        min_workers=4,
        max_workers=16,
    )
    domain.set_envelope(16, 1.0, 25)
    assert domain.supervisor.startup_bulkhead == shell_slots
    assert domain.supervisor.mihomo_start_bulkhead == 8
    assert len(domain.supervisor._starter._threads) == shell_slots
    domain.start()
    try:
        for index in range(128):
            assert domain.submit(
                Node(
                    name=f"envelope-{index}",
                    protocol="http",
                    address="127.0.0.1",
                    port=int(server.server_address[1]),
                    password=f"fixture-{index}",
                    source=f"root-{index % 5}",
                    tier="tier3",
                    extra={
                        "tier3_source_root_id": f"root-{index % 5}",
                        "tier3_source_unit_family": "http",
                        "username": "fixture",
                    },
                ),
                {
                    "configured_source_count": 5,
                },
            )
        deadline = time.monotonic() + 12.0
        max_starting = 0
        max_pending_spawn = 0
        max_waiting_start = 0
        max_startup_pipeline = 0
        dispatched_during_ramp = False
        while time.monotonic() < deadline:
            snapshot = domain.snapshot()
            if snapshot.supervisor.active_workers < 16:
                dispatched_during_ramp = (
                    dispatched_during_ramp or bool(snapshot.actor.dispatched)
                )
            max_starting = max(max_starting, snapshot.supervisor.starting_workers)
            max_pending_spawn = max(
                max_pending_spawn,
                snapshot.supervisor.pending_spawn_workers,
            )
            max_waiting_start = max(
                max_waiting_start,
                snapshot.supervisor.waiting_start_workers,
            )
            max_startup_pipeline = max(
                max_startup_pipeline,
                snapshot.supervisor.startup_pipeline_workers,
            )
            if (
                snapshot.desired_workers == 16
                and snapshot.supervisor.active_workers == 16
            ):
                break
            time.sleep(0.02)
        else:
            failed = domain.snapshot()
            raise AssertionError({
                "ready_deadline": deadline,
                "active": failed.supervisor.active_workers,
                "committed": failed.actor.admission_committed,
                "failures": failed.supervisor.failure_reason_counts,
                "ready_times": [
                    (item["worker_id"], item["ready_at"], item["startup_phase"],
                     item["prewarm_started"], item["prewarm_membership_pending"])
                    for item in failed.supervisor.workers
                ],
            })
        assert max_starting <= domain.supervisor.mihomo_start_bulkhead
        assert max_pending_spawn <= domain.supervisor.startup_bulkhead
        assert max_waiting_start <= domain.supervisor.max_workers
        assert max_startup_pipeline <= domain.supervisor.max_workers
        assert dispatched_during_ramp
        assert domain.wait_idle(30.0), domain.snapshot()
        assert domain.snapshot().actor.conservation_ok
    finally:
        try:
            assert domain.stop(timeout_seconds=4.0) == "STOPPED"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1.0)


def test_process_domain_promotes_prewarmed_shells_when_envelope_expands() -> None:
    domain = Tier3ValidationDomain(
        refresh_epoch="shell-prewarm",
        binary_path=str(MIHOMO),
        probe_urls=["http://probe.invalid/generate_204"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        commit_node=lambda _node, _report, intent: AdmissionRejected(
            refresh_epoch=intent.refresh_epoch,
            candidate_digest=intent.candidate_digest,
            lease_id=intent.lease_id,
            sequence=intent.sequence,
            reason="not_expected",
        ),
        terminal_observer=lambda _node, _terminal: None,
        min_workers=4,
        max_workers=16,
    )
    domain.set_envelope(4, 1.0, 25)
    domain.set_shell_prewarm_workers(16)
    domain.start()
    try:
        _wait_until(
            lambda: (
                domain.snapshot().supervisor.active_workers == 4
                and domain.snapshot().supervisor.prewarmed_waiting_workers >= 2
            ),
            timeout=12.0,
        )
        staged = domain.snapshot().supervisor
        assert staged.desired_workers == 4
        assert staged.shell_prewarm_workers == 16
        assert staged.active_workers == 4

        domain.set_envelope(16, 1.0, 25)
        _wait_until(
            lambda: domain.snapshot().supervisor.active_workers == 16,
            timeout=12.0,
        )
        assert domain.snapshot().supervisor.prewarmed_waiting_workers == 0
    finally:
        assert domain.stop(timeout_seconds=4.0) == "STOPPED"


def test_process_domain_accepts_bounded_manager_startup_bulkheads() -> None:
    domain = Tier3ValidationDomain(
        refresh_epoch="bounded-startup",
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        commit_node=lambda _node, _report, intent: AdmissionRejected(
            refresh_epoch=intent.refresh_epoch,
            candidate_digest=intent.candidate_digest,
            lease_id=intent.lease_id,
            sequence=intent.sequence,
            reason="not_expected",
        ),
        terminal_observer=lambda _node, _terminal: None,
        min_workers=4,
        max_workers=16,
        startup_bulkhead=4,
        mihomo_start_bulkhead=4,
        helper_target=deterministic_helper_bootstrap,
    )

    assert domain.supervisor.startup_bulkhead == 4
    assert domain.supervisor.mihomo_start_bulkhead == 4
    assert len(domain.supervisor._starter._threads) == 4


def test_supervisor_preserves_independent_startup_limits_after_first_cohort() -> None:
    supervisor = Tier3ValidatorSupervisor(
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        attempt_sink=lambda _result: None,
        capacity_sink=lambda _count: None,
        min_workers=4,
        max_workers=16,
        helper_target=deterministic_helper_bootstrap,
    )
    supervisor._desired_workers = 16
    supervisor._startup_cohort_ready = True
    supervisor._workers = {
        worker_id: types.SimpleNamespace(
            worker_id=worker_id,
            phase=Tier3WorkerPhase.STARTING,
            bootstrap_ready=False,
        )
        for worker_id in range(1, supervisor.startup_bulkhead + 1)
    }
    scheduled: list[int] = []

    with patch.object(
        supervisor,
        "_schedule_worker_spawn",
        side_effect=lambda worker_id: scheduled.append(worker_id) or True,
    ):
        supervisor._start_needed_workers()
        assert scheduled == list(range(9, 17))

        scheduled.clear()
        supervisor._pending_spawns = {
            worker_id: 1
            for worker_id in range(101, 101 + supervisor.startup_bulkhead)
        }
        supervisor._start_needed_workers()
        assert scheduled == []


def test_supervisor_prioritizes_initial_cohort_before_remaining_shells() -> None:
    supervisor = Tier3ValidatorSupervisor(
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        attempt_sink=lambda _: None,
        capacity_sink=lambda _: None,
        min_workers=4,
        max_workers=16,
    )
    supervisor._desired_workers = 16
    scheduled = []
    with patch.object(supervisor, "_schedule_worker_spawn", side_effect=lambda wid: scheduled.append(wid) or True):
        supervisor._start_needed_workers()
        assert scheduled == [1, 2, 3, 4]
        supervisor._workers = {
            wid: types.SimpleNamespace(
                worker_id=wid, phase=Tier3WorkerPhase.STARTING, bootstrap_ready=True,
            )
            for wid in scheduled
        }
        scheduled.clear()
        supervisor._start_needed_workers()
        assert scheduled == []
        for wid in (1, 2, 3):
            supervisor._workers[wid].phase = Tier3WorkerPhase.IDLE
        supervisor._start_needed_workers()
        assert scheduled == []
        for worker in supervisor._workers.values():
            worker.phase = Tier3WorkerPhase.IDLE
        supervisor._start_needed_workers()
        assert scheduled == list(range(5, 13))
        assert supervisor._startup_cohort_ready


def test_supervisor_prewarms_two_shells_without_granting_mihomo_capacity() -> None:
    supervisor = Tier3ValidatorSupervisor(
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        attempt_sink=lambda _result: None,
        capacity_sink=lambda _count: None,
        min_workers=4,
        max_workers=16,
        helper_target=deterministic_helper_bootstrap,
    )
    supervisor._desired_workers = 4
    supervisor._shell_prewarm_workers = 16
    supervisor._workers = {
        worker_id: types.SimpleNamespace(
            worker_id=worker_id,
            phase=Tier3WorkerPhase.IDLE,
            bootstrap_ready=True,
        )
        for worker_id in range(1, 5)
    }
    scheduled: list[int] = []

    with patch.object(
        supervisor,
        "_schedule_worker_spawn",
        side_effect=lambda worker_id: scheduled.append(worker_id) or True,
    ):
        supervisor._start_needed_workers()

    assert scheduled == [5, 6]
    for worker_id in scheduled:
        supervisor._workers[worker_id] = types.SimpleNamespace(
            worker_id=worker_id,
            generation=1,
            phase=Tier3WorkerPhase.WAITING_START,
            bootstrap_ready=True,
            mixed_port=20000 + worker_id,
            controller_port=21000 + worker_id,
        )

    with patch.object(supervisor, "_send", return_value=True) as send:
        supervisor._activate_waiting_workers()
        send.assert_not_called()

        supervisor._desired_workers = 8
        supervisor._activate_waiting_workers()

    assert send.call_count == 2
    assert all(
        supervisor._workers[worker_id].phase is Tier3WorkerPhase.STARTING
        for worker_id in scheduled
    )


def test_bootstrap_ready_prewarmed_shell_has_no_waiting_start_deadline() -> None:
    supervisor = Tier3ValidatorSupervisor(
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        attempt_sink=lambda _result: None,
        capacity_sink=lambda _count: None,
        min_workers=4,
        max_workers=16,
        helper_target=deterministic_helper_bootstrap,
    )
    worker = types.SimpleNamespace(
        worker_id=5,
        phase=Tier3WorkerPhase.WAITING_START,
        bootstrap_ready=True,
        started_at=time.monotonic() - 60.0,
    )
    supervisor._workers = {5: worker}

    with patch.object(supervisor, "_retire_worker") as retire:
        supervisor._check_deadlines()

    retire.assert_not_called()


def test_supervisor_does_not_start_waiting_mihomo_above_bulkhead() -> None:
    supervisor = Tier3ValidatorSupervisor(
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        attempt_sink=lambda _result: None,
        capacity_sink=lambda _count: None,
        min_workers=4,
        max_workers=16,
        helper_target=deterministic_helper_bootstrap,
    )
    supervisor._workers = {
        worker_id: types.SimpleNamespace(
            worker_id=worker_id,
            phase=(
                Tier3WorkerPhase.STARTING
                if worker_id <= supervisor.mihomo_start_bulkhead
                else Tier3WorkerPhase.WAITING_START
            ),
            bootstrap_ready=worker_id > supervisor.mihomo_start_bulkhead,
        )
        for worker_id in range(1, 17)
    }

    with patch.object(supervisor, "_send") as send:
        supervisor._activate_waiting_workers()

    send.assert_not_called()


def test_process_domain_concurrent_stop_is_idempotent(request) -> None:
    domain = Tier3ValidationDomain(
        refresh_epoch="concurrent-stop",
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        commit_node=lambda _node, _report, intent: AdmissionRejected(
            refresh_epoch=intent.refresh_epoch,
            candidate_digest=intent.candidate_digest,
            lease_id=intent.lease_id,
            sequence=intent.sequence,
            reason="not_expected",
        ),
        terminal_observer=lambda _node, _terminal: None,
        min_workers=2,
        max_workers=2,
        helper_target=deterministic_helper_bootstrap,
    )
    request.addfinalizer(lambda: domain.stop(timeout_seconds=6.0))
    domain.start()
    _wait_until(lambda: domain.snapshot().supervisor.idle_workers == 2)
    statuses: list[str] = []
    started = time.monotonic()
    threads = [
        threading.Thread(target=lambda: statuses.append(domain.stop()))
        for _index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3.25)
    elapsed = time.monotonic() - started
    assert not any(thread.is_alive() for thread in threads)
    assert statuses == ["STOPPED", "STOPPED"]
    assert elapsed <= 3.25
    snapshot = domain.snapshot()
    assert snapshot.supervisor.active_workers == 0
    assert snapshot.operational_supervisor.active_workers == 2
    assert snapshot.peak_helper_count == 2
    assert "helper_pipe_lost" not in snapshot.supervisor.last_failure_reason
    assert "supervisor:STOPPED" in snapshot.stop_stages
    assert snapshot.stop_stages[-1] == "actor_stopped"


def test_process_domain_immediate_post_start_stop_reaches_empty_job(request) -> None:
    for iteration in range(6):
        domain = Tier3ValidationDomain(
            refresh_epoch=f"immediate-stop-{iteration}",
            binary_path="fixture",
            probe_urls=["http://fixture/probe"],
            expected_statuses={204},
            probe_timeout_seconds=1.0,
            slow_threshold_ms=3000,
            commit_node=lambda _node, _report, intent: AdmissionRejected(
                refresh_epoch=intent.refresh_epoch,
                candidate_digest=intent.candidate_digest,
                lease_id=intent.lease_id,
                sequence=intent.sequence,
                reason="not_expected",
            ),
            terminal_observer=lambda _node, _terminal: None,
            min_workers=4,
            max_workers=4,
            helper_target=deterministic_helper_bootstrap,
        )

        request.addfinalizer(lambda domain=domain: domain.stop(timeout_seconds=6.0))
        assert domain.start() is True
        assert domain.stop(timeout_seconds=6.0) == "STOPPED"
        snapshot = domain.snapshot()
        assert snapshot.supervisor.active_workers == 0
        assert snapshot.supervisor.pending_commands == 0

    assert not [
        child
        for child in multiprocessing.active_children()
        if child.name.startswith("tier3-validator-helper-")
    ]


def test_process_domain_completed_pass_then_stop_reaches_empty_job(request) -> None:
    terminals: list[ValidationTerminal] = []
    domain = Tier3ValidationDomain(
        refresh_epoch="completed-pass-stop",
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        commit_node=lambda node, _report, intent: AdmissionCommitted(
            refresh_epoch=intent.refresh_epoch,
            candidate_digest=intent.candidate_digest,
            lease_id=intent.lease_id,
            sequence=intent.sequence,
            node=node,
            pool_revision=1,
            pool_before=0,
            pool_after=1,
            net_new=True,
        ),
        terminal_observer=lambda _node, terminal: terminals.append(terminal),
        min_workers=4,
        max_workers=4,
        helper_target=deterministic_helper_bootstrap,
    )

    request.addfinalizer(lambda: domain.stop(timeout_seconds=6.0))
    assert domain.start() is True
    assert domain.submit(
        Node(
            name="completed-pass",
            protocol="http",
            address="127.0.0.1",
            port=8080,
            tier="tier3",
            extra={"fixture_validation_outcome": "pass"},
        ),
        {"fixture_validation_outcome": "pass"},
    )
    _wait_until(lambda: len(terminals) == 1)

    status = domain.stop(timeout_seconds=6.0)
    snapshot = domain.snapshot()
    assert status == "STOPPED", snapshot.stop_stages
    assert snapshot.supervisor.active_workers == 0


def test_process_domain_stop_before_start_permanently_rejects_start() -> None:
    observed: list[ValidationTerminal] = []
    domain = Tier3ValidationDomain(
        refresh_epoch="stop-before-start",
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        commit_node=lambda _node, _report, intent: AdmissionRejected(
            refresh_epoch=intent.refresh_epoch,
            candidate_digest=intent.candidate_digest,
            lease_id=intent.lease_id,
            sequence=intent.sequence,
            reason="not_expected",
        ),
        terminal_observer=lambda _node, terminal: observed.append(terminal),
        min_workers=1,
        max_workers=1,
        helper_target=deterministic_helper_bootstrap,
    )

    assert domain.stop(timeout_seconds=1.0, owner="manager-close") == "STOPPED"
    assert domain.start() is False
    assert domain.start() is False
    assert not domain.submit(
        Node(
            name="must-not-start",
            protocol="http",
            address="127.0.0.1",
            port=8080,
            tier="tier3",
        ),
        {"configured_source_count": 1},
    )
    snapshot = domain.snapshot()
    assert snapshot.stop_status == "STOPPED"
    assert snapshot.supervisor.active_workers == 0
    assert snapshot.supervisor.pending_commands == 0
    assert observed == []


def test_process_domain_mid_start_stop_claim_prevents_later_components() -> None:
    domain = Tier3ValidationDomain(
        refresh_epoch="stop-mid-start",
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        commit_node=lambda _node, _report, intent: AdmissionRejected(
            refresh_epoch=intent.refresh_epoch,
            candidate_digest=intent.candidate_digest,
            lease_id=intent.lease_id,
            sequence=intent.sequence,
            reason="not_expected",
        ),
        terminal_observer=lambda _node, _terminal: None,
        min_workers=1,
        max_workers=1,
        helper_target=deterministic_helper_bootstrap,
    )
    committer_start_entered = threading.Event()
    release_committer_start = threading.Event()
    start_results: list[bool] = []
    stop_results: list[str] = []

    def blocked_committer_start() -> None:
        committer_start_entered.set()
        assert release_committer_start.wait(2.0)

    with (
        patch.object(domain._network_context, "start"),
        patch.object(domain.committer, "start", side_effect=blocked_committer_start),
        patch.object(domain.actor, "start") as actor_start,
        patch.object(domain.supervisor, "start") as supervisor_start,
        patch.object(domain.watchdog, "start") as watchdog_start,
    ):
        start_thread = threading.Thread(
            target=lambda: start_results.append(domain.start())
        )
        start_thread.start()
        assert committer_start_entered.wait(1.0)

        assert domain.claim_stop_owner("manager-close")
        stop_thread = threading.Thread(
            target=lambda: stop_results.append(
                domain.stop(timeout_seconds=2.0, owner="manager-close")
            )
        )
        stop_thread.start()
        release_committer_start.set()

        start_thread.join(timeout=2.0)
        stop_thread.join(timeout=2.0)

    assert not start_thread.is_alive()
    assert not stop_thread.is_alive()
    assert start_results == [False]
    assert stop_results == ["STOPPED"]
    actor_start.assert_not_called()
    supervisor_start.assert_not_called()
    watchdog_start.assert_not_called()
    assert domain.start() is False
    assert domain.snapshot().supervisor.active_workers == 0


def test_reporters_receive_ordered_stop_even_after_join_budget_expires() -> None:
    entered = threading.Event()
    release = threading.Event()
    observed = []
    domain = Tier3ValidationDomain(
        refresh_epoch="zero-join-budget",
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        commit_node=lambda *_: None,
        terminal_observer=lambda *_: None,
        generation_observer=observed.append,
        min_workers=1,
        max_workers=1,
    )
    original = domain._generation_gone

    def delayed_generation(event):
        entered.set()
        release.wait(2.0)
        original(event)

    domain._generation_gone = delayed_generation
    event = GenerationGone(1, 1, 0, 0, "fixture", True, True)
    observer = threading.Thread(target=domain._observer_loop)
    reporter = threading.Thread(target=domain._result_loop)
    domain._observer_thread = observer
    domain._result_thread = reporter
    observer.start()
    reporter.start()
    try:
        domain._terminal_queue.put(event)
        assert entered.wait(1.0)
        assert domain._stop_result_reporter(lambda: 0.0) is False
        assert domain._result_sentinel_sent
        assert not domain._observer_sentinel_sent
        release.set()
        reporter.join(timeout=1.0)
        observer.join(timeout=1.0)
        assert not reporter.is_alive()
        assert not observer.is_alive()
        assert observed == [event]
        assert domain._terminal_queue.unfinished_tasks == 0
        assert domain._observer_queue.unfinished_tasks == 0
        assert domain._stop_result_reporter(lambda: 0.0) is True
        assert domain._observer_queue.empty()
    finally:
        release.set()
        domain._stop_result_reporter(lambda: 1.0)


def test_process_domain_does_not_report_stopped_with_blocked_result_reporter() -> None:
    observer_entered = threading.Event()
    release_observer = threading.Event()

    def blocking_terminal_observer(
        _node: Node,
        _terminal: ValidationTerminal,
    ) -> None:
        observer_entered.set()
        release_observer.wait(2.0)

    domain = Tier3ValidationDomain(
        refresh_epoch="blocked-result-reporter",
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        commit_node=lambda _node, _report, intent: AdmissionRejected(
            refresh_epoch=intent.refresh_epoch,
            candidate_digest=intent.candidate_digest,
            lease_id=intent.lease_id,
            sequence=intent.sequence,
            reason="not_expected",
        ),
        terminal_observer=blocking_terminal_observer,
        min_workers=1,
        max_workers=1,
        helper_target=deterministic_helper_bootstrap,
    )
    domain._terminal_queue = queue.Queue(maxsize=1)
    terminal = ValidationTerminal(
        refresh_epoch=domain.refresh_epoch,
        candidate_digest="a" * 64,
        config_digest="b" * 64,
        lease_id="lease",
        sequence=1,
        worker_id=1,
        worker_generation=1,
        kind=Tier3TerminalKind.CANCELLED,
        node_json=canonical_json(
            Node(
                name="blocked-result",
                protocol="http",
                address="127.0.0.1",
                port=8080,
                tier="tier3",
            ).to_dict()
        ),
        reason="manager_closing",
    )
    domain._result_thread = threading.Thread(
        target=domain._result_loop,
        name="blocked-tier3-result-reporter",
        daemon=False,
    )
    domain._result_thread.start()
    domain._terminal_queue.put(terminal)
    assert observer_entered.wait(1.0)
    domain._terminal_queue.put(terminal)

    started = time.monotonic()
    status = domain.stop(timeout_seconds=0.15, owner="manager-close")
    elapsed = time.monotonic() - started

    assert status == "STOP_FAILED_RESULT_REPORTER_ACTIVE"
    assert elapsed <= 0.3
    snapshot = domain.snapshot()
    assert snapshot.stop_status == status
    assert snapshot.result_reporter_active
    assert "result_reporter_active" in snapshot.stop_stages
    assert domain._result_thread is not None
    assert not domain._result_sentinel_sent

    release_observer.set()
    retry_started = time.monotonic()
    assert domain.stop(timeout_seconds=1.0, owner="manager-close") == "STOPPED"
    assert time.monotonic() - retry_started <= 1.2
    assert domain._result_thread is None
    assert domain._result_sentinel_sent
    snapshot = domain.snapshot()
    assert snapshot.stop_status == "STOPPED"
    assert not snapshot.result_reporter_active
    assert snapshot.result_pending == 0
    assert snapshot.stop_stages[-1] == "result_reporter_reaped"


def test_slow_attempt_observer_does_not_block_supervisor_pipe_reader() -> None:
    observer_entered = threading.Event()
    release_observer = threading.Event()

    def blocking_attempt_observer(
        _node: Node,
        _result: EndpointAttemptResult,
    ) -> None:
        observer_entered.set()
        release_observer.wait(3.0)

    domain = Tier3ValidationDomain(
        refresh_epoch="nonblocking-attempt-feedback",
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        commit_node=lambda _node, _report, intent: AdmissionRejected(
            refresh_epoch=intent.refresh_epoch,
            candidate_digest=intent.candidate_digest,
            lease_id=intent.lease_id,
            sequence=intent.sequence,
            reason="not_expected",
        ),
        terminal_observer=lambda _node, _terminal: None,
        attempt_observer=blocking_attempt_observer,
        min_workers=2,
        max_workers=2,
        helper_target=deterministic_helper_bootstrap,
    )
    assert domain.start()
    try:
        _wait_until(lambda: domain.snapshot().supervisor.idle_workers == 2)
        for index in range(2):
            assert domain.submit(
                Node(
                    name=f"nonblocking-attempt-{index}",
                    protocol="http",
                    address=f"127.0.0.{index + 1}",
                    port=8080 + index,
                    source=f"root-{index}",
                    tier="tier3",
                    extra={
                        "tier3_source_root_id": f"root-{index}",
                        "tier3_source_unit_family": "http",
                        "fixture_validation_outcome": "candidate_fail",
                    },
                ),
                {"configured_source_count": 2},
            )

        assert observer_entered.wait(2.0)
        # The reporter remains deliberately blocked, but the supervisor must
        # drain both helper Pipes and release both worker generations.
        _wait_until(
            lambda: domain.snapshot().supervisor.endpoint_result_count >= 2,
            timeout=2.0,
        )
        # Business feedback may be slow, but it must not serialize actor
        # progression behind the first completed helper result.
        _wait_until(
            lambda: domain.snapshot().actor.fast_completed >= 2,
            timeout=2.0,
        )
        snapshot = domain.snapshot().supervisor
        assert snapshot.replacement_count == 0
        assert snapshot.forced_job_termination_count == 0
        assert snapshot.retiring_workers == 0
    finally:
        release_observer.set()
        assert domain.stop(timeout_seconds=3.0) == "STOPPED"


def test_candidate_credit_feedback_bypasses_blocked_business_observer() -> None:
    observer_entered = threading.Event()
    release_observer = threading.Event()
    feedback_events = []

    def blocking_attempt_observer(
        _node: Node,
        _result: EndpointAttemptResult,
    ) -> None:
        observer_entered.set()
        release_observer.wait(3.0)

    domain = Tier3ValidationDomain(
        refresh_epoch="nonblocking-candidate-credit",
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        commit_node=lambda _node, _report, intent: AdmissionRejected(
            refresh_epoch=intent.refresh_epoch,
            candidate_digest=intent.candidate_digest,
            lease_id=intent.lease_id,
            sequence=intent.sequence,
            reason="not_expected",
        ),
        terminal_observer=lambda _node, _terminal: None,
        attempt_observer=blocking_attempt_observer,
        feedback_observer=feedback_events.append,
        min_workers=1,
        max_workers=1,
        helper_target=deterministic_helper_bootstrap,
    )
    node = Node(
        name="nonblocking-credit",
        protocol="http",
        address="127.0.0.1",
        port=8080,
        source="root-credit",
        tier="tier3",
        extra={
            "tier3_source_root_id": "root-credit",
            "tier3_source_unit_family": "http",
            "fixture_validation_outcome": "candidate_fail",
        },
    )
    ticket = Tier3CandidateDispatchTicket(
        scheduler_generation=1,
        ticket_id=1,
        arm_key="arm-credit",
        source_root="root-credit",
        family="http",
        coarse_key="coarse-credit",
        selection_reason="coverage",
        dispatched_monotonic_ns=time.monotonic_ns(),
    )

    assert domain.start()
    try:
        _wait_until(lambda: domain.snapshot().supervisor.idle_workers == 1)
        assert domain.submit(
            node,
            {"configured_source_count": 1},
            dispatch_context=Tier3CandidateDispatchContext(
                ticket=ticket,
                node_identity="http:127.0.0.1:8080",
            ),
        )
        assert observer_entered.wait(2.0)

        _wait_until(
            lambda: {
                event.kind for event in feedback_events
            }
            >= {
                Tier3ValidationFeedbackKind.FAST_OBSERVATION,
                Tier3ValidationFeedbackKind.CONTINUATION,
                Tier3ValidationFeedbackKind.TERMINAL,
            },
            timeout=3.0,
        )
        continuation = next(
            event
            for event in feedback_events
            if event.kind is Tier3ValidationFeedbackKind.CONTINUATION
        )
        assert continuation.outcome == EndpointAttemptKind.CANDIDATE_FAIL.value
        assert continuation.busy_worker_ms > 0
        assert not continuation.right_censored
        observation = next(
            event
            for event in feedback_events
            if event.kind is Tier3ValidationFeedbackKind.FAST_OBSERVATION
        )
        assert observation.outcome == EndpointAttemptKind.CANDIDATE_FAIL.value
        assert feedback_events.index(observation) < feedback_events.index(continuation)
        assert domain.dispatch_context_count() == 0
        assert domain.snapshot().observer_pending >= 1
    finally:
        release_observer.set()
        assert domain.stop(timeout_seconds=3.0) == "STOPPED"


def test_candidate_config_digest_uses_rendered_proxy_semantics() -> None:
    first = Node(
        name="first-name",
        protocol="vless",
        address="node.example",
        port=443,
        uuid="00000000-0000-4000-8000-000000000001",
        security="tls",
        network="ws",
        host="cdn.example",
        path="/ws",
        sni="origin.example",
        tls=True,
        source="root-a",
        extra={"tier3_source_unit_id": "vless-a"},
    )
    duplicate = first.clone()
    duplicate.name = "other-name"
    duplicate.source = "root-b"
    duplicate.extra["tier3_source_unit_id"] = "aggregate"
    changed = first.clone()
    changed.path = "/other"

    assert tier3_candidate_config_digest(first) == tier3_candidate_config_digest(
        duplicate
    )
    assert tier3_candidate_config_digest(first) != tier3_candidate_config_digest(
        changed
    )


def test_process_domain_snapshot_captures_result_reporter_reference_once() -> None:
    class _AliveThread:
        @staticmethod
        def is_alive() -> bool:
            return True

    class _ResultThreadReadRaceDomain(Tier3ValidationDomain):
        _race_armed = False
        _result_thread_read_count = 0

        def __getattribute__(self, name: str):
            if name == "_result_thread" and object.__getattribute__(
                self, "_race_armed"
            ):
                reads = object.__getattribute__(
                    self, "_result_thread_read_count"
                )
                object.__setattr__(
                    self, "_result_thread_read_count", reads + 1
                )
                if reads > 0:
                    return None
            return super().__getattribute__(name)

    domain = _ResultThreadReadRaceDomain(
        refresh_epoch="snapshot-result-reporter-race",
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        commit_node=lambda _node, _report, intent: AdmissionRejected(
            refresh_epoch=intent.refresh_epoch,
            candidate_digest=intent.candidate_digest,
            lease_id=intent.lease_id,
            sequence=intent.sequence,
            reason="not_expected",
        ),
        terminal_observer=lambda _node, _terminal: None,
        min_workers=1,
        max_workers=1,
        helper_target=deterministic_helper_bootstrap,
    )
    domain._result_thread = _AliveThread()  # type: ignore[assignment]
    domain._race_armed = True

    snapshot = domain.snapshot()

    assert snapshot.result_reporter_active
    assert domain._result_thread_read_count == 1
    domain._race_armed = False
    domain._result_thread = None
    assert domain.stop(timeout_seconds=1.0, owner="manager-close") == "STOPPED"


def test_process_domain_reaps_reporter_without_erasing_supervisor_failure() -> None:
    observer_entered = threading.Event()
    release_observer = threading.Event()

    def blocking_terminal_observer(
        _node: Node,
        _terminal: ValidationTerminal,
    ) -> None:
        observer_entered.set()
        release_observer.wait(2.0)

    domain = Tier3ValidationDomain(
        refresh_epoch="blocked-result-reporter-and-supervisor-failure",
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        commit_node=lambda _node, _report, intent: AdmissionRejected(
            refresh_epoch=intent.refresh_epoch,
            candidate_digest=intent.candidate_digest,
            lease_id=intent.lease_id,
            sequence=intent.sequence,
            reason="not_expected",
        ),
        terminal_observer=blocking_terminal_observer,
        min_workers=1,
        max_workers=1,
        helper_target=deterministic_helper_bootstrap,
    )
    domain._terminal_queue = queue.Queue(maxsize=1)
    terminal = ValidationTerminal(
        refresh_epoch=domain.refresh_epoch,
        candidate_digest="c" * 64,
        config_digest="d" * 64,
        lease_id="lease",
        sequence=1,
        worker_id=1,
        worker_generation=1,
        kind=Tier3TerminalKind.CANCELLED,
        node_json=canonical_json(
            Node(
                name="combined-stop-failure",
                protocol="http",
                address="127.0.0.1",
                port=8080,
                tier="tier3",
            ).to_dict()
        ),
        reason="manager_closing",
    )
    domain._result_thread = threading.Thread(
        target=domain._result_loop,
        name="combined-failure-tier3-result-reporter",
        daemon=False,
    )
    domain._result_thread.start()
    domain._terminal_queue.put(terminal)
    assert observer_entered.wait(1.0)
    domain._terminal_queue.put(terminal)

    supervisor_failure = "STOP_FAILED_HELPERS_ACTIVE"
    try:
        with patch.object(
            domain.supervisor,
            "stop",
            return_value=supervisor_failure,
        ):
            assert (
                domain.stop(timeout_seconds=0.15, owner="manager-close")
                == supervisor_failure
            )
        assert domain.snapshot().result_reporter_active
        assert domain._result_thread is not None

        release_observer.set()
        retry_started = time.monotonic()
        assert (
            domain.stop(timeout_seconds=1.0, owner="manager-close")
            == supervisor_failure
        )
        assert time.monotonic() - retry_started <= 1.2
        assert domain._result_thread is None
        snapshot = domain.snapshot()
        assert snapshot.stop_status == supervisor_failure
        assert not snapshot.result_reporter_active
        assert snapshot.result_pending == 0
    finally:
        release_observer.set()
        result_thread = domain._result_thread
        if result_thread is not None and result_thread.is_alive():
            try:
                domain._terminal_queue.put(None, timeout=0.2)
            except queue.Full:
                pass
            result_thread.join(timeout=1.0)


def test_process_domain_explicit_stop_owner_prevents_source_finally_race() -> None:
    domain = Tier3ValidationDomain(
        refresh_epoch="owned-stop",
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        commit_node=lambda _node, _report, intent: AdmissionRejected(
            refresh_epoch=intent.refresh_epoch,
            candidate_digest=intent.candidate_digest,
            lease_id=intent.lease_id,
            sequence=intent.sequence,
            reason="not_expected",
        ),
        terminal_observer=lambda _node, _terminal: None,
        min_workers=1,
        max_workers=1,
        helper_target=deterministic_helper_bootstrap,
    )
    domain.start()
    _wait_until(lambda: domain.snapshot().supervisor.idle_workers == 1)

    assert domain.claim_stop_owner("manager-close")
    deferred = domain.stop(timeout_seconds=0.05, owner="source-stream")
    assert deferred == "STOP_DEFERRED_TO_OWNER:manager-close"
    assert domain.stop(timeout_seconds=3.0, owner="manager-close") == "STOPPED"


def test_supervisor_stops_sixteen_uncooperative_jobs_with_one_global_deadline(request) -> None:
    supervisor = Tier3ValidatorSupervisor(
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        attempt_sink=lambda _result: None,
        capacity_sink=lambda _count: None,
        min_workers=16,
        max_workers=16,
        helper_target=uncooperative_helper_bootstrap,
    )
    request.addfinalizer(lambda: supervisor.stop(timeout_seconds=3.0))
    supervisor.start()
    # This test measures the one-deadline stop path, not source-mode Python
    # spawn speed. Let every fixture cross the explicit bootstrap stage first.
    _wait_until(lambda: supervisor.snapshot().active_workers == 16, timeout=20.0)
    started = time.monotonic()
    status = supervisor.stop(timeout_seconds=3.0)
    elapsed = time.monotonic() - started
    assert status == "STOPPED"
    assert elapsed <= 3.0
    assert supervisor.snapshot().active_workers == 0
    assert not [
        child
        for child in multiprocessing.active_children()
        if child.name.startswith("tier3-validator-helper-")
    ]


def test_supervisor_stop_verdict_uses_final_job_and_pid_evidence(request) -> None:
    supervisor = Tier3ValidatorSupervisor(
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        attempt_sink=lambda _result: None,
        capacity_sink=lambda _count: None,
        min_workers=4,
        max_workers=4,
        helper_target=uncooperative_helper_bootstrap,
    )
    request.addfinalizer(lambda: supervisor.stop(timeout_seconds=3.0))
    supervisor.start()
    _wait_until(lambda: supervisor.snapshot().active_workers == 4, timeout=10.0)

    original_terminate = _terminate_jobs_bounded

    def completed_but_late(*args, **kwargs):
        original_terminate(*args, **kwargs)
        # Model a termination helper that missed its scheduling deadline even
        # though the Job/PID evidence is already clean.
        return False

    with patch(
        "tier3_validator_supervisor._terminate_jobs_bounded",
        side_effect=completed_but_late,
    ):
        started = time.monotonic()
        status = supervisor.stop(timeout_seconds=3.0)

    assert status == "STOPPED"
    assert time.monotonic() - started <= 3.0
    snapshot = supervisor.snapshot()
    assert snapshot.active_workers == 0
    assert snapshot.quarantined_workers == 0


def test_supervisor_process_wait_snapshot_tolerates_closed_handle() -> None:
    process = multiprocessing.get_context("spawn").Process(target=time.sleep, args=(0,))
    process.close()

    assert _process_is_alive(process) is False
    assert _process_sentinel(process) is None


def test_supervisor_stop_does_not_call_business_terminal_sink() -> None:
    sink_called = threading.Event()

    def blocking_sink(_terminal) -> None:
        sink_called.set()
        time.sleep(8.0)

    supervisor = Tier3ValidatorSupervisor(
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=5.0,
        slow_threshold_ms=3000,
        attempt_sink=blocking_sink,
        capacity_sink=lambda _count: None,
        min_workers=1,
        max_workers=1,
        helper_target=uncooperative_helper_bootstrap,
    )
    supervisor.start()
    _wait_until(lambda: supervisor.snapshot().idle_workers == 1)
    supervisor.submit(
        ValidateEndpointCmd(
            refresh_epoch="bounded-stop",
            candidate_digest="a" * 64,
            config_digest="b" * 64,
            lease_id="bounded-stop-lease",
            sequence=1,
            node_json=canonical_json(
                Node(
                    name="bounded-stop",
                    protocol="http",
                    address="127.0.0.1",
                    port=9,
                    tier="tier3",
                ).to_dict()
            ),
            source_root="root",
            source_family="http",
            candidate_hard_cap_seconds=30.0,
        )
    )
    _wait_until(lambda: supervisor.snapshot().validating_workers == 1)

    started = time.monotonic()
    status = supervisor.stop(timeout_seconds=3.0)
    elapsed = time.monotonic() - started
    assert status == "STOPPED"
    assert elapsed <= 3.0
    assert not sink_called.is_set()


def test_supervisor_stop_does_not_wait_for_logging_handler_lock() -> None:
    logger = logging.getLogger("tier3-validator-stop-lock-test")
    logger.propagate = False
    handler = logging.NullHandler()
    logger.addHandler(handler)
    handler.createLock()
    lock_held = threading.Event()
    release_lock = threading.Event()

    def hold_handler_lock() -> None:
        handler.acquire()
        try:
            lock_held.set()
            release_lock.wait(8.0)
        finally:
            handler.release()

    lock_thread = threading.Thread(target=hold_handler_lock)
    lock_thread.start()
    assert lock_held.wait(1.0)
    supervisor = Tier3ValidatorSupervisor(
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        attempt_sink=lambda _result: None,
        capacity_sink=lambda _count: None,
        logger=logger,
        min_workers=1,
        max_workers=1,
        helper_target=uncooperative_helper_bootstrap,
    )
    status: list[str] = []
    try:
        supervisor.start()
        _wait_until(lambda: supervisor.snapshot().active_workers == 1, timeout=8.0)
        started = time.monotonic()
        stop_thread = threading.Thread(
            target=lambda: status.append(supervisor.stop(timeout_seconds=3.0))
        )
        stop_thread.start()
        stop_thread.join(timeout=3.25)
        elapsed = time.monotonic() - started
        blocked = stop_thread.is_alive()
        release_lock.set()
        stop_thread.join(timeout=2.0)
        assert not blocked
        assert status == ["STOPPED"]
        assert elapsed <= 3.25
        assert supervisor.snapshot().active_workers == 0
    finally:
        release_lock.set()
        lock_thread.join(timeout=2.0)
        logger.removeHandler(handler)


def test_supervisor_owner_does_not_sample_process_resources() -> None:
    supervisor = Tier3ValidatorSupervisor(
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        attempt_sink=lambda _result: None,
        capacity_sink=lambda _count: None,
        min_workers=1,
        max_workers=1,
        helper_target=deterministic_helper_bootstrap,
    )
    supervisor.start()
    try:
        _wait_until(lambda: supervisor.snapshot().idle_workers == 1)
        with patch(
            "tier3_validator_supervisor.process_resource_snapshot",
            return_value=types.SimpleNamespace(private_bytes=1, handle_count=1),
        ) as process_probe:
            time.sleep(0.65)
            process_probe.assert_not_called()
            supervisor.refresh_resource_metrics()
            assert process_probe.call_count >= 1
    finally:
        assert supervisor.stop(timeout_seconds=3.0) == "STOPPED"


def test_production_actor_dispatch_is_bound_to_real_idle_helper_capacity() -> None:
    committed: list[Node] = []
    revision = 0

    def commit(node: Node, _report, intent) -> AdmissionCommitted:
        nonlocal revision
        revision += 1
        committed.append(node)
        return AdmissionCommitted(
            refresh_epoch=intent.refresh_epoch,
            candidate_digest=intent.candidate_digest,
            lease_id=intent.lease_id,
            sequence=intent.sequence,
            pool_revision=revision,
            net_new_pool_growth=True,
        )

    domain = Tier3ValidationDomain(
        refresh_epoch="real-idle-helper-capacity",
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        commit_node=commit,
        terminal_observer=lambda _node, _terminal: None,
        min_workers=2,
        max_workers=2,
        helper_target=deterministic_helper_bootstrap,
    )
    domain.start()
    max_pending = 0
    try:
        _wait_until(lambda: domain.snapshot().supervisor.idle_workers == 2)
        for index in range(12):
            node = Node(
                name=f"capacity-{index}",
                protocol="http",
                address="127.0.0.1",
                port=9000 + index,
                tier="tier3",
                extra={
                    "fixture_validation_outcome": "pass",
                    "tier3_source_root_id": "capacity-root",
                    "tier3_source_unit_family": "http",
                },
            )
            assert domain.submit(
                node,
                {
                    "configured_source_count": 1,
                    "fixture_delay_seconds": 0.1,
                },
            )

        deadline = time.monotonic() + 5.0
        while len(committed) < 12 and time.monotonic() < deadline:
            snapshot = domain.snapshot()
            max_pending = max(max_pending, snapshot.supervisor.pending_commands)
            assert (
                snapshot.supervisor.pending_commands
                <= snapshot.supervisor.reserved_workers
            )
            time.sleep(0.01)
        assert len(committed) == 12
        final_supervisor = domain.snapshot().supervisor
        assert max_pending == 0
        assert final_supervisor.direct_dispatch_count == 12
        assert final_supervisor.assignment_to_send_sample_count == 12
        assert final_supervisor.assignment_to_send_max_ms <= 100.0
        assert final_supervisor.capacity_reservation_rejected_count == 0
        for node in committed:
            assigned = int(
                node.extra["tier3_validator_supervisor_assigned_monotonic_ns"]
            )
            sent = int(
                node.extra["tier3_validator_supervisor_sent_monotonic_ns"]
            )
            helper_started = int(
                node.extra["tier3_validator_helper_started_monotonic_ns"]
            )
            assert assigned > 0
            assert assigned <= sent <= helper_started
            assert sent - assigned <= 100_000_000
            assert helper_started - sent <= 500_000_000
    finally:
        assert domain.stop(timeout_seconds=3.0) == "STOPPED"


def test_supervisor_rejects_stale_same_generation_capacity_reservation() -> None:
    supervisor = Tier3ValidatorSupervisor(
        binary_path="fixture",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        attempt_sink=lambda _result: None,
        capacity_sink=lambda _count: None,
        min_workers=1,
        max_workers=1,
        helper_target=deterministic_helper_bootstrap,
    )
    supervisor.start()
    try:
        _wait_until(lambda: supervisor.snapshot().idle_workers == 1)
        lease = supervisor.reserve_capacity()
        assert lease is not None
        command = ValidateEndpointCmd(
            refresh_epoch="stale-capacity-reservation",
            candidate_digest="a" * 64,
            config_digest="b" * 64,
            lease_id="lease-1",
            sequence=1,
            node_json="{}",
            source_root="root-a",
            source_family="vless",
            worker_id=lease.worker_id,
            worker_generation=lease.worker_generation,
            capacity_reservation_id=lease.reservation_id + 1,
            endpoint="http://fixture/probe",
        )

        assert supervisor.submit(command) is False
        assert supervisor.snapshot().reserved_workers == 1
        supervisor.release_capacity(lease)
        _wait_until(lambda: supervisor.snapshot().idle_workers == 1)
        assert (
            supervisor.snapshot().capacity_reservation_rejected_count == 1
        )
    finally:
        assert supervisor.stop(timeout_seconds=3.0) == "STOPPED"
