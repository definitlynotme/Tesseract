from __future__ import annotations

import asyncio
import threading
import time
import traceback
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import manager as manager_module
from runtime_source_transport import (
    RuntimeSourceFetchEndpoint,
    RuntimeSourceListenerCredentials,
    RuntimeSourceRouteKind,
)
from tests import test_manager as helpers
from tier3_sources import CollectionReport
from tier3_source_supply import Tier3SourceSupplySupervisor


@pytest.mark.parametrize("phase", ["bootstrap", "core"])
def test_real_source_batch_requests_qualification_before_gate(tmp_path, phase):
    owner = helpers.ManagerTests()._minimal_manager(tmp_path, tier3_enabled=True)
    owner.runtime_source_route_demand = Mock()
    owner.runtime_source_fetch_endpoint_provider = lambda *_args: ()
    try:
        assert owner._runtime_source_endpoint_map(phase, []) == {}
        owner.runtime_source_route_demand.assert_not_called()
        owner._runtime_source_endpoint_map(phase, [
            {"type": "subscription_url", "value": "https://opaque.invalid/source"},
        ])
        owner.runtime_source_route_demand.assert_called_once_with(phase)
    finally:
        owner.close(timeout_seconds=3)


@pytest.mark.parametrize("readiness", ["ready", "delayed", "missing", "stale"])
def test_bootstrap_start_refreshes_private_source_authority(tmp_path, readiness):
    owner = helpers.ManagerTests()._minimal_manager(
        tmp_path, tier3_enabled=True,
        tier3_overrides={"bootstrap_enabled": False,
                         "subscription_urls": ["https://opaque.invalid/source"]},
    )
    owner.testing_config.update(mihomo_helper={
        "binary_path": str(Path(__file__).resolve().parents[1] / "bin" / "mihomo.exe"),
        "probe_urls": ["http://127.0.0.1:9/generate_204"],
        "expected_statuses": [204], "probe_timeout": 5.0,
    }, mihomo_validation_temp_dir=str(tmp_path))
    bootstrapped, dispatched = threading.Event(), threading.Event()
    route_ready_at = float("inf")
    calls, errors = [], []
    endpoint = RuntimeSourceFetchEndpoint(
        RuntimeSourceRouteKind.DIRECT, "private-source", "127.0.0.1", 19102,
        owner.runtime_generation + (1 if readiness == "stale" else 0),
        1, True, 0, 0, RuntimeSourceListenerCredentials("test", "secret"),
    )

    def endpoint_provider(phase, _prior=None):
        if phase != "bootstrap" or time.monotonic() < route_ready_at:
            return ()
        return (endpoint,)

    def start_provider(phase):
        nonlocal route_ready_at
        if phase != "bootstrap":
            return None
        if not bootstrapped.is_set():
            route_ready_at = time.monotonic() + (.3 if readiness == "delayed" else 0)
            if readiness == "missing":
                route_ready_at = float("inf")
            bootstrapped.set()
        # The ordinary app route may still be REJECT. This URL only starts Core.
        return "http://127.0.0.1:19101"

    owner.runtime_source_fetch_endpoint_provider = endpoint_provider
    owner.runtime_source_fetch_proxy_provider = start_provider

    async def collect(*_args, **kwargs):
        calls.append(dict(kwargs["settings"]))
        dispatched.set()
        entries = kwargs["source_entries"]
        report = CollectionReport(source_count=len(entries), sources=[
            dict(entry, ok=True, pending=False, retryable=False, status="ok", node_count=0)
            for entry in entries
        ])
        return [], report

    def refresh():
        try:
            owner.refresh_tier3(suppress_intervention_alert=True, startup_mode=True)
        except BaseException:
            errors.append(traceback.format_exc())

    with patch.object(manager_module, "collect_nodes_from_sources_with_report", collect):
        worker = threading.Thread(target=refresh)
        worker.start()
        try:
            assert bootstrapped.wait(5), errors
            if readiness in {"missing", "stale"}:
                assert not dispatched.wait(.4), "unqualified mixed-port reached Collector"
            else:
                if readiness == "delayed":
                    assert not dispatched.wait(.1), "readiness was bypassed"
                assert dispatched.wait(3), errors
                first = calls[0]
                endpoint_map = first.get("runtime_source_fetch_endpoints_by_authority")
                assert endpoint_map, "first source fetch used ordinary mixed-port"
                assert list(endpoint_map.values()) == [(endpoint,)]
                assert "runtime_source_fetch_proxy_url" not in first
                assert first["require_remote_source_route"] is True
        finally:
            owner.request_stop()
            worker.join(6)
            owner.close(timeout_seconds=3)
        assert not worker.is_alive()
        assert not errors


def test_contended_bootstrap_provider_defers_without_any_fallback_route(tmp_path):
    """Source supply keeps its private-lane contract while a Core transition runs.

    The desktop runtime's supply provider answers None at once while another
    thread owns the Core transition (Tier1/Tier2 checks, promotion).  Supply
    must neither dispatch nor fall back to a raw proxy or the ordinary
    mixed-port meanwhile, and must use the private lane once it is published.
    """

    owner = helpers.ManagerTests()._minimal_manager(
        tmp_path, tier3_enabled=True,
        tier3_overrides={"bootstrap_enabled": False,
                         "subscription_urls": ["https://opaque.invalid/source"]},
    )
    owner.testing_config.update(mihomo_helper={
        "binary_path": str(Path(__file__).resolve().parents[1] / "bin" / "mihomo.exe"),
        "probe_urls": ["http://127.0.0.1:9/generate_204"],
        "expected_statuses": [204], "probe_timeout": 5.0,
    }, mihomo_validation_temp_dir=str(tmp_path))
    dispatched = threading.Event()
    transition_ends_at = time.monotonic() + 0.5
    contended_answers = []
    calls, errors = [], []
    endpoint = RuntimeSourceFetchEndpoint(
        RuntimeSourceRouteKind.DIRECT, "private-source", "127.0.0.1", 19103,
        owner.runtime_generation, 1, True, 0, 0,
        RuntimeSourceListenerCredentials("test", "secret"),
    )

    def endpoint_provider(phase, _prior=None):
        if phase != "bootstrap" or time.monotonic() < transition_ends_at:
            return ()
        return (endpoint,)

    def contended_provider(phase):
        if phase == "bootstrap" and time.monotonic() < transition_ends_at:
            contended_answers.append(time.monotonic())
            return None
        return "http://127.0.0.1:19104"

    owner.runtime_source_fetch_endpoint_provider = endpoint_provider
    owner.runtime_source_fetch_proxy_provider = contended_provider

    async def collect(*_args, **kwargs):
        calls.append(dict(kwargs["settings"]))
        dispatched.set()
        entries = kwargs["source_entries"]
        report = CollectionReport(source_count=len(entries), sources=[
            dict(entry, ok=True, pending=False, retryable=False, status="ok", node_count=0)
            for entry in entries
        ])
        return [], report

    def refresh():
        try:
            owner.refresh_tier3(suppress_intervention_alert=True, startup_mode=True)
        except BaseException:
            errors.append(traceback.format_exc())

    with patch.object(manager_module, "collect_nodes_from_sources_with_report", collect):
        worker = threading.Thread(target=refresh)
        worker.start()
        try:
            assert dispatched.wait(5), errors
            first = calls[0]
            assert time.monotonic() >= transition_ends_at
            assert contended_answers, "supply never polled during the transition"
            endpoint_map = first.get("runtime_source_fetch_endpoints_by_authority")
            assert endpoint_map, "source fetch used a fallback route"
            assert list(endpoint_map.values()) == [(endpoint,)]
            assert "runtime_source_fetch_proxy_url" not in first
            assert not first.get("egress_proxy")
            assert first["require_remote_source_route"] is True
        finally:
            owner.request_stop()
            worker.join(6)
            owner.close(timeout_seconds=3)
        assert not worker.is_alive()
        assert not errors


@pytest.mark.parametrize("root_count,retry_limit", [(1, 1), (5, 5), (5, 1)])
def test_prefetch_cannot_dispatch_roots_owned_by_continuous_supply(
    tmp_path, root_count, retry_limit,
):
    source_urls = [f"https://opaque-{i}.invalid/source" for i in range(root_count)]
    owner = helpers.ManagerTests()._minimal_manager(
        tmp_path, tier3_enabled=True,
        tier3_overrides={"bootstrap_enabled": False,
                         "subscription_urls": source_urls},
    )
    owner.testing_config.update(mihomo_helper={
        "binary_path": str(Path(__file__).resolve().parents[1] / "bin" / "mihomo.exe"),
        "probe_urls": ["http://127.0.0.1:9/generate_204"],
        "expected_statuses": [204], "probe_timeout": 5.0,
    }, mihomo_validation_temp_dir=str(tmp_path))
    endpoint = RuntimeSourceFetchEndpoint(
        RuntimeSourceRouteKind.DIRECT, "private-source", "127.0.0.1", 19102,
        owner.runtime_generation, 1, True, 0, 0,
        RuntimeSourceListenerCredentials("test", "secret"),
    )
    owner.runtime_source_fetch_endpoint_provider = lambda *_args: (endpoint,)
    retry_dispatched, prefetch_claimed = threading.Event(), threading.Event()
    calls, errors = [], []
    original_claim = Tier3SourceSupplySupervisor.claim_due

    def claim(supervisor, *args, **kwargs):
        is_prefetch = threading.current_thread().name.endswith("source-prefetch")
        if is_prefetch:
            assert retry_dispatched.wait(5), "continuous supplier never dispatched"
        else:
            kwargs["limit"] = retry_limit
        result = original_claim(supervisor, *args, **kwargs)
        if is_prefetch:
            assert len(result) == root_count - retry_limit
            prefetch_claimed.set()
        return result

    async def collect(*_args, **kwargs):
        entries = kwargs["source_entries"]
        calls.append((threading.current_thread().name, [e["value"] for e in entries]))
        observer = kwargs["settings"].get("source_unit_transport_started_observer")
        assert callable(observer), "first transport readiness must work for either owner"
        observer("opaque-root", "opaque-unit", "direct", time.monotonic())
        retry_dispatched.set()
        while not kwargs["cancel_event"].is_set():
            await asyncio.sleep(.01)
        return [], CollectionReport(source_count=len(entries), sources=[
            dict(entry, ok=False, pending=True, retryable=True, status="pending")
            for entry in entries
        ])

    def refresh():
        try:
            owner.refresh_tier3(suppress_intervention_alert=True, startup_mode=True)
        except BaseException:
            errors.append(traceback.format_exc())

    with patch.object(Tier3SourceSupplySupervisor, "claim_due", claim), patch.object(
        manager_module, "collect_nodes_from_sources_with_report", collect,
    ):
        worker = threading.Thread(target=refresh)
        worker.start()
        try:
            assert prefetch_claimed.wait(6), errors
            time.sleep(.1)
            dispatched = [url for _thread, urls in calls for url in urls]
            assert sorted(dispatched) == sorted(source_urls), (
                "initial collection ignored the dispatch lease"
            )
            assert calls[0][0].endswith("source-supply")
        finally:
            owner.request_stop()
            worker.join(6)
            owner.close(timeout_seconds=3)
        assert not worker.is_alive()
        assert not errors


def test_delivered_supply_lifts_a_no_egress_park(tmp_path):
    """A park is set after fetching nothing; a delivery is newer evidence."""

    owner = helpers.ManagerTests()._minimal_manager(tmp_path, tier3_enabled=True)
    try:
        owner.park_tier3_source_supply(300.0, "no_egress")
        assert owner._tier3_source_supply_park_remaining() > 0.0
        assert owner._tier3_source_supply_park_delivered() is None
        with owner.lock:
            owner.tier3_last_collection_report.update(
                {"source_fetch_ok": 2, "node_count": 900}
            )
        assert owner._tier3_source_supply_park_delivered() == {
            "source_fetch_ok": 2,
            "node_count": 900,
        }
        owner.resume_tier3_source_supply("supply_observed")
        assert owner._tier3_source_supply_park_remaining() == 0.0
    finally:
        owner.close(timeout_seconds=3)
