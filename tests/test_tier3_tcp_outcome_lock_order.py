from __future__ import annotations

import ast
import asyncio
import sys
import threading
import traceback
from pathlib import Path
from unittest.mock import patch

import manager as manager_module
import tester
from models import Node
from tests import test_manager as helpers
from tier3_sources import CollectionReport


def test_prestart_requeue_does_not_hold_pi_source_metrics_lock(tmp_path):
    """Force the observed metrics -> drain -> envelope -> metrics cycle."""
    owner = helpers.ManagerTests()._minimal_manager(
        tmp_path, tier3_enabled=True,
        tier3_overrides={"bootstrap_enabled": False,
                         "subscription_urls": ["https://opaque.invalid/source"]},
    )
    owner.runtime_source_fetch_proxy_provider = lambda _phase: "http://127.0.0.1:19101"
    owner.testing_config.update(mihomo_helper={
        "binary_path": str(Path(__file__).resolve().parents[1] / "bin" / "mihomo.exe"),
        "probe_urls": ["http://127.0.0.1:9/generate_204"],
        "expected_statuses": [204], "probe_timeout": 5.0,
    }, mihomo_validation_temp_dir=str(tmp_path))
    node = Node(name="deferred", protocol="ss", address="opaque.invalid", port=443,
                method="aes-128-gcm", password="fixture", tier="tier3")
    tree = ast.parse(Path(manager_module.__file__).read_text(encoding="utf-8"))
    function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                    and n.name == "record_source_stream_tcp_outcome")
    drain_lines = {n.lineno for n in ast.walk(function) if isinstance(n, ast.With)
                   and any(isinstance(i.context_expr, ast.Name)
                           and i.context_expr.id == "source_stream_candidate_drain_lock"
                           for i in n.items)}
    assert drain_lines
    captured, proceed, waiting_for_drain = (threading.Event() for _ in range(3))
    locks, errors = {}, []

    def trace(frame, event, _arg):
        if frame.f_code.co_name != "record_source_stream_tcp_outcome":
            return None
        if event == "line" and frame.f_lineno in drain_lines:
            waiting_for_drain.set()
        return trace

    def profile(frame, event, _arg):
        if event != "call" or frame.f_code.co_name != "record_source_stream_tcp_outcome":
            return
        outcome = frame.f_locals["outcome"]
        if captured.is_set() or not any(
            "tcp_transport_start_deferred" in item.failure_reason
            for item in outcome.observed_items
        ):
            return
        locks["metrics"] = frame.f_locals["source_stream_source_validation_lock"]
        locks["drain"] = frame.f_locals["source_stream_candidate_drain_lock"]
        sys.settrace(trace)
        frame.f_trace = trace
        captured.set()
        assert proceed.wait(3), "test did not release the TCP result"

    async def collect(*_args, **kwargs):
        report = CollectionReport(source_count=1, fetched_sources=1, node_count=1,
                                  sources=[dict(kwargs["source_entries"][0], ok=True,
                                                pending=False, retryable=False)])
        kwargs["source_observer"]([node], report)
        return [node], report

    original_probe = tester._run_tcp_probe_entitlement

    async def wait_for_transport(*args, **kwargs):
        semaphore = kwargs["semaphore"]
        permits = semaphore._value
        for _ in range(permits):
            await semaphore.acquire()
        try:
            # The real tester enforces its pre-start deadline. No socket or
            # validation/admission result is synthesized by this test.
            return await original_probe(*args, **kwargs)
        finally:
            for _ in range(permits):
                semaphore.release()

    def refresh():
        try:
            owner.refresh_tier3(suppress_intervention_alert=True, startup_mode=True)
        except BaseException:
            errors.append(traceback.format_exc())

    previous_profile = threading.getprofile()
    threading.setprofile(profile)
    with patch.object(manager_module, "collect_nodes_from_sources_with_report", collect), \
            patch.object(tester, "_run_tcp_probe_entitlement", wait_for_transport):
        worker = threading.Thread(target=refresh)
        worker.start()
        try:
            assert captured.wait(6), errors
            assert locks["drain"].acquire(timeout=1)
            try:
                proceed.set()
                assert waiting_for_drain.wait(2), errors
                metrics_available = locks["metrics"].acquire(timeout=.2)
                if metrics_available:
                    locks["metrics"].release()
            finally:
                locks["drain"].release()
        finally:
            proceed.set()
            owner.request_stop()
            worker.join(6)
            owner.close(timeout_seconds=3)
            threading.setprofile(previous_profile)
        assert not worker.is_alive()
        assert not errors
        assert metrics_available, "TCP requeue blocked the PI metrics snapshot while waiting for drain"
        assert not owner.runtime_nodes["tier3"]
