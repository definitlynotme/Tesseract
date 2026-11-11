from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import tester
from manager import Tier3ControllerDecision
from models import Node, NodeStatus
from tests import test_manager as manager_tests
from tier3_sources import CollectionReport
import manager as manager_module
import tier3_candidate_scheduler as scheduler_module
from tier3_tcp_dns import Tier3TcpDnsResolver, Tier3TcpDnsBusy


def nodes():
    return [Node(name=name, protocol="ss", address=f"{name}.example", port=8388,
                 tier="tier3") for name in ("slow", "fast")]


def probe_waiting_for_fast_handoff(release, finished):
    async def probe(node, **kwargs):
        if node.name == "slow":
            try:
                while not release.is_set():
                    await asyncio.sleep(0.005)
            finally:
                finished.set()
        updated = node.clone()
        updated.status = NodeStatus.ALIVE
        updated.latency_ms = 5
        return updated
    return probe


def test_tcp_result_observer_does_not_wait_for_sibling_entitlement():
    release, finished = threading.Event(), threading.Event()
    observed = []

    def observe(node):
        observed.append((node.name, finished.is_set()))
        if node.name == "fast":
            release.set()

    with patch.object(tester, "_tcp_probe", probe_waiting_for_fast_handoff(release, finished)):
        outcome = asyncio.run(tester.scan_tier3_tcp_candidates(
            nodes(), testing_config={"tcp_timeout": 0.2, "tcp_probe_entitlement_seconds": 0.2},
            logger=logging.getLogger(__name__), concurrency=2, result_observer=observe,
        ))
    assert observed[0] == ("fast", False)
    assert [name for name, _ in observed] == ["fast", "slow"]
    assert outcome.metadata["completed_count"] == 2
    assert outcome.metadata["entitlement_exhausted_count"] == 0


@pytest.mark.parametrize("cancel_after_fast", [False, True])
def test_manager_hands_completed_tcp_to_validation_before_wave_end(tmp_path, cancel_after_fast):
    owner = manager_tests.ManagerTests()._minimal_manager(tmp_path, tier3_enabled=True)
    release, finished, cancel = threading.Event(), threading.Event(), threading.Event()
    candidates = nodes()
    reports = {node.name: CollectionReport(source_count=index + 1, node_count=1)
               for index, node in enumerate(candidates)}
    contexts = {node.name: object() for node in candidates}
    handoffs = []

    def enqueue(node, report, context):
        handoffs.append((node.name, finished.is_set(), report, context))
        if node.name == "fast":
            if cancel_after_fast:
                cancel.set()
            release.set()
        return True

    try:
        with patch.object(tester, "_tcp_probe", probe_waiting_for_fast_handoff(release, finished)):
            outcome = owner._stream_tier3_outside_admission_batch(
                candidates, CollectionReport(source_count=2, node_count=2),
                startup_mode=True, intensive_expansion=True, target_alive=25,
                decision=Tier3ControllerDecision(intensity=1.0, source_budget=2,
                                                collect_interval=1, error=1.0, integral=0.0),
                max_pool_size=100, tcp_prefilter_timeout_seconds=0.2,
                tcp_prefilter_concurrency=2, full_validation_enqueue=enqueue,
                source_report_for_node=lambda node: reports[node.name],
                dispatch_context_for_node=lambda node: contexts[node.name],
                node_observer=lambda _node: None, cancel_event=cancel,
            )
        assert handoffs[0] == ("fast", False, reports["fast"], contexts["fast"])
        assert [row[0] for row in handoffs] == (["fast"] if cancel_after_fast else ["fast", "slow"])
        assert len({item.node.name for item in outcome.observed_items}) == len(outcome.observed_items)
        if not cancel_after_fast:
            assert outcome.validation_report["full_validation_queued_count"] == 2
    finally:
        owner.close(timeout_seconds=3.0)


@pytest.mark.parametrize("transfer_fails", [False, True])
def test_slow_or_failed_handoff_does_not_block_sibling_tcp_clock(tmp_path, transfer_fails):
    owner = manager_tests.ManagerTests()._minimal_manager(tmp_path, tier3_enabled=True)
    delivered = []
    completed = []

    async def probe(node, **kwargs):
        if node.name == "slow":
            await asyncio.sleep(0.05)
        completed.append(node.name)
        updated = node.clone()
        updated.status = NodeStatus.ALIVE
        updated.latency_ms = 5
        return updated

    def enqueue(node, report):
        delivered.append(node.name)
        time.sleep(0.2)
        if transfer_fails:
            raise RuntimeError("transfer failed")
        return True

    def run():
        return owner._stream_tier3_outside_admission_batch(
            nodes(), CollectionReport(source_count=2, node_count=2),
            startup_mode=True, intensive_expansion=True, target_alive=25,
            decision=Tier3ControllerDecision(intensity=1.0, source_budget=2,
                                            collect_interval=1, error=1.0, integral=0.0),
            max_pool_size=100, tcp_prefilter_timeout_seconds=0.1,
            tcp_prefilter_concurrency=2, full_validation_enqueue=enqueue,
            node_observer=lambda _node: None,
        )

    try:
        with patch.object(tester, "_tcp_probe", probe):
            if transfer_fails:
                with pytest.raises(RuntimeError, match="transfer failed"):
                    run()
                assert delivered == ["fast"]
            else:
                outcome = run()
                assert delivered == ["fast", "slow"]
                assert outcome.validation_report["tcp_prefilter_responsive_count"] == 2
                assert outcome.validation_report["tcp_entitlement_exhausted_count"] == 0
                assert outcome.validation_report["tcp_scan_elapsed_ms"] < 150
                assert outcome.validation_report["tcp_to_responsive_backlog_handoff_ms"] >= 350
        assert completed == ["fast", "slow"]
    finally:
        owner.close(timeout_seconds=3.0)


def test_started_tcp_censorship_releases_capacity_before_stop(tmp_path):
    fixture = manager_tests.ManagerTests()
    fixture.setUp()
    owner = fixture._minimal_manager(
        tmp_path, tier3_enabled=True,
        tier3_overrides={"bootstrap_enabled": False,
                         "subscription_urls": ["https://opaque.example/sub"],
                         "validation_concurrency_min": 4,
                         "validation_concurrency_max": 4},
    )
    owner.mark_tier3_core_running(effective_egress_ok=True)
    owner.set_effective_egress_state(True)
    owner.runtime_source_fetch_proxy_provider = lambda _phase: "http://127.0.0.1:19101"
    Path(owner.sources_file).write_text(
        "subscription_urls:\n  - https://opaque.example/sub\n"
        "github_repos: []\ntelegram_channels: []\nlocal_files: []\n",
        encoding="utf-8",
    )
    candidates = [Node(name=f"candidate-{i}", protocol="ss",
                       address=f"opaque-{i}.invalid", port=443,
                       method="aes-128-gcm", password="fixture", tier="tier3",
                       source="https://opaque.example/sub") for i in range(32)]
    resolutions = []
    done = threading.Event()
    errors = []
    starts = []
    wave_sizes_and_slots = []
    original = scheduler_module.Tier3CandidateScheduler.on_tcp_outcome
    original_stream = owner._stream_tier3_outside_admission_batch

    def stream(wave_nodes, *args, **kwargs):
        wave_sizes_and_slots.append((
            len(wave_nodes), kwargs["tcp_prefilter_concurrency"],
        ))
        return original_stream(wave_nodes, *args, **kwargs)

    def record(scheduler, ticket, **kwargs):
        result = original(scheduler, ticket, **kwargs)
        resolutions.append((ticket.ticket_id, kwargs, scheduler.diagnostic_snapshot()))
        if len(resolutions) == len(candidates):
            done.set()
        return result

    async def collect(*_args, **kwargs):
        report = CollectionReport(source_count=1, fetched_sources=1,
                                  node_count=len(candidates),
                                  sources=[{"type": "subscription_url",
                                            "value": "https://opaque.example/sub", "ok": True}])
        kwargs["source_observer"](candidates, report)
        return candidates, report

    async def probe(node, **kwargs):
        starts.append(node.name)
        await asyncio.sleep(5)
        return node.clone()

    def refresh():
        try:
            owner.refresh_tier3(suppress_intervention_alert=True, startup_mode=False)
        except Exception as exc:
            errors.append(exc)

    try:
        with patch.object(manager_module, "collect_nodes_from_sources_with_report", collect), \
                patch.object(tester, "_tcp_probe", probe), \
                patch.object(owner, "_stream_tier3_outside_admission_batch", stream), \
                patch.object(scheduler_module.Tier3CandidateScheduler, "on_tcp_outcome", record):
            worker = threading.Thread(target=refresh)
            worker.start()
            try:
                all_released = done.wait(20)
            finally:
                owner.request_stop()
                worker.join(5)
        assert not worker.is_alive()
        assert not errors
        assert all_released, (
            f"Only {len(resolutions)}/32 outcomes released before stop; "
            f"starts={len(starts)}; report=" + str({key: value for key, value in
                owner.tier3_last_collection_report.items() if key in (
                    "source_stream_tcp_entitlement_exhausted_count",
                    "source_stream_tcp_prestart_active_count",
                    "candidate_scheduler_selected_total",
                    "candidate_scheduler_backlog_count", "source_stream_error")})
        )
        assert len({row[0] for row in resolutions}) == 32
        assert wave_sizes_and_slots
        assert all(size <= slots for size, slots in wave_sizes_and_slots)
        assert owner.tier3_last_collection_report[
            "source_stream_tcp_start_deferred_count"
        ] == 0
        assert all(row[1].get("hard_censored") for row in resolutions)
        assert resolutions[-1][2]["tcp_handoff_lease_count"] == 0
        assert not owner.runtime_nodes["tier3"]
    finally:
        owner.close(timeout_seconds=3.0)
        fixture.doCleanups()


def test_manager_stream_keeps_hard_censored_observations(tmp_path):
    owner = manager_tests.ManagerTests()._minimal_manager(tmp_path, tier3_enabled=True)
    candidates = nodes()
    async def probe(node, **kwargs):
        await asyncio.sleep(5)
        return node.clone()

    try:
        with patch.object(tester, "_tcp_probe", probe):
            outcome = owner._stream_tier3_outside_admission_batch(
                candidates, CollectionReport(source_count=1, node_count=2),
                startup_mode=True, intensive_expansion=True, target_alive=25,
                decision=Tier3ControllerDecision(intensity=1.0, source_budget=2,
                                                collect_interval=1, error=1.0, integral=0.0),
                max_pool_size=100, tcp_prefilter_timeout_seconds=0.05,
                tcp_prefilter_concurrency=2, full_validation_enqueue=lambda *_: False,
                node_observer=lambda _: None,
            )
        assert len(outcome.observed_items) == 2
        assert all(not item.responsive and item.failure_stage == "tcp_deferred"
                   and "tcp_probe_entitlement_exhausted" in item.failure_reason
                   for item in outcome.observed_items)
    finally:
        owner.close(timeout_seconds=3)


@pytest.mark.parametrize("legacy_wave_owned_dns", [True, False])
def test_tcp_wave_deadline_is_not_extended_by_cancelled_resolver_thread(tmp_path, legacy_wave_owned_dns):
    owner = manager_tests.ManagerTests()._minimal_manager(tmp_path, tier3_enabled=True)
    resolver_entered, resolver_release, batch_done = (
        threading.Event(), threading.Event(), threading.Event())
    outcomes, errors = [], []

    def resolve(*_args):
        resolver_entered.set()
        resolver_release.wait(5)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 1))]

    resolver = Tier3TcpDnsResolver(capacity=1)
    async def legacy_getaddrinfo(*args, **kwargs):
        return await asyncio.get_running_loop().run_in_executor(None, resolve, *args)

    def run():
        try:
            outcomes.append(owner._scan_tier3_source_stream_tcp_candidates(
                nodes()[:1], testing_config={"tcp_timeout": 0.05}, concurrency=1,
            ))
        except Exception as exc:
            errors.append(exc)
        finally:
            batch_done.set()

    try:
        with patch.object(socket, "getaddrinfo", resolve), \
                patch.object(manager_module, "tier3_tcp_dns_resolver", resolver), \
                patch.object(resolver, "getaddrinfo", legacy_getaddrinfo if legacy_wave_owned_dns
                             else resolver.getaddrinfo):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                assert resolver_entered.wait(2)
                delivered_before_resolver_exit = batch_done.wait(0.7)
                if not legacy_wave_owned_dns:
                    assert resolver.snapshot()["active_calls"] == 1
                    with socket.socket() as listener:
                        listener.bind(("127.0.0.1", 0))
                        listener.listen(4)
                        candidate = Node(protocol="ss", tier="tier3", address="127.0.0.1",
                                         port=listener.getsockname()[1])
                        next_wave = owner._scan_tier3_source_stream_tcp_candidates(
                            [candidate], testing_config={"tcp_timeout": 0.2}, concurrency=1,
                        )
                    assert next_wave.nodes[0].status in {NodeStatus.ALIVE, NodeStatus.SLOW}
                    assert not resolver_release.is_set()
            finally:
                resolver_release.set()
                worker.join(3)
        assert not errors
        assert not worker.is_alive()
        assert delivered_before_resolver_exit is not legacy_wave_owned_dns
        assert outcomes[0].metadata["completed_count"] == 1
        assert outcomes[0].nodes[0].status is NodeStatus.DEAD
    finally:
        resolver_release.set()
        resolver.shutdown()
        owner.close(timeout_seconds=3)


def test_resolver_capacity_is_held_until_os_call_really_exits():
    resolver = Tier3TcpDnsResolver(capacity=2)
    entered = threading.Barrier(3)
    release = threading.Event()

    def resolve(*_args):
        entered.wait(timeout=3)
        release.wait(5)
        return []

    async def run():
        tasks = [asyncio.create_task(resolver.getaddrinfo("opaque.invalid", 443)) for _ in range(2)]
        await asyncio.to_thread(entered.wait, 3)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert resolver.snapshot()["active_calls"] == 2
        for _ in range(1000):
            with pytest.raises(Tier3TcpDnsBusy):
                await resolver.getaddrinfo("other.invalid", 443)
        assert resolver.snapshot()["high_water"] == 2
        loop = asyncio.get_running_loop()
        original_getaddrinfo = loop.getaddrinfo
        loop.getaddrinfo = resolver.getaddrinfo
        try:
            result = await tester._tcp_probe(nodes()[0], timeout_seconds=1.25, slow_threshold_ms=1000)
            assert result.status is NodeStatus.SUSPICIOUS
            assert result.extra["tier3_failure_stage"] == "tcp_deferred"
            assert "tcp_dns_facility_saturated" in result.suspicious_reason
        finally:
            loop.getaddrinfo = original_getaddrinfo

    try:
        with patch.object(socket, "getaddrinfo", resolve):
            asyncio.run(run())
    finally:
        release.set()
        resolver.shutdown()
    deadline = time.monotonic() + 2
    while resolver.snapshot()["active_calls"] and time.monotonic() < deadline:
        time.sleep(0.005)
    assert resolver.snapshot()["active_calls"] == 0
