from __future__ import annotations

import asyncio
import threading
import time
import traceback
from pathlib import Path
from unittest.mock import patch

import pytest

import manager as manager_module
from tests import test_manager as helpers
from tier3_source_supply import Tier3SourceSupplySupervisor
from tier3_sources import CollectionReport


@pytest.mark.parametrize("phase", ["initial", "retry"])
def test_source_retry_stop_publishes_joined_owner_not_hot_lease(tmp_path, phase):
    manager = helpers.ManagerTests()._minimal_manager(
        tmp_path, tier3_enabled=True,
        tier3_overrides={"bootstrap_enabled": False,
                         "subscription_urls": ["https://opaque.invalid/source"]},
    )
    manager.mark_tier3_core_running(effective_egress_ok=True)
    manager.set_effective_egress_state(True)
    manager.runtime_source_fetch_proxy_provider = lambda _phase: "http://127.0.0.1:19101"
    manager.testing_config.update(mihomo_helper={
        "binary_path": str(Path(__file__).resolve().parents[1] / "bin" / "mihomo.exe"),
        "probe_urls": ["http://127.0.0.1:9/generate_204"],
        "expected_statuses": [204], "probe_timeout": 5.0,
    }, mihomo_validation_temp_dir=str(tmp_path))
    owners, errors = [], []
    retry_started = threading.Event()
    calls = 0

    def make_owner(**kwargs):
        owner = Tier3SourceSupplySupervisor(**kwargs)
        owners.append(owner)
        return owner

    async def collect(*_args, **kwargs):
        nonlocal calls
        calls += 1
        if phase == "initial" or calls > 1:
            retry_started.set()
            while not kwargs["cancel_event"].is_set():
                await asyncio.sleep(.01)
        source = {"type": "subscription_url", "value": "https://opaque.invalid/source",
                  "ok": False, "pending": True, "retryable": True,
                  "error": "source_fetch_pending"}
        report = CollectionReport(source_count=1, sources=[source])
        callback = kwargs["settings"].get("source_root_terminal_observer")
        if callback:
            callback(source)
        if kwargs.get("source_observer"):
            kwargs["source_observer"]([], report)
        return [], report

    def refresh():
        try:
            manager.refresh_tier3(suppress_intervention_alert=True, startup_mode=False)
        except BaseException:
            errors.append(traceback.format_exc())

    with (
        patch.object(manager_module, "Tier3SourceSupplySupervisor", make_owner),
        patch.object(manager_module, "collect_nodes_from_sources_with_report", collect),
    ):
        worker = threading.Thread(target=refresh)
        worker.start()
        try:
            assert retry_started.wait(8), "\n".join(errors) or f"collector calls={calls}"
            assert owners[0].snapshot().active_leases == 1
            if phase == "retry":
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if manager.tier3_last_collection_report.get("source_supply_supervisor_active_leases") == 1:
                        break
                    time.sleep(.025)
                assert manager.tier3_last_collection_report["source_supply_supervisor_active_leases"] == 1
        finally:
            manager.request_stop()
            worker.join(timeout=6)
            manager.close(timeout_seconds=3)
        assert not worker.is_alive()
        assert not errors
    snapshot = owners[0].snapshot()
    assert snapshot.active_leases == 0
    report = manager.tier3_last_collection_report
    assert report["source_stream_shutdown_confirmed"]
    assert report["source_stream_process_idle_helper_count"] == 0
    assert report["source_supply_supervisor_active_leases"] == snapshot.active_leases
    assert report["source_supply_active_lease_count"] == snapshot.active_leases
    assert report["source_supply_supervisor_revision"] == snapshot.revision
    assert report["source_supply_supervisor_dispatch_count"] == snapshot.dispatch_count
    assert report["source_supply_supervisor_deferred_count"] == snapshot.deferred_count
