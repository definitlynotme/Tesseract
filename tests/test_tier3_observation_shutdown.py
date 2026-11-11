"""Accepted observation facts must survive a bounded, truthful Manager stop."""
from pathlib import Path
import json
import threading
import time
from unittest.mock import patch

from models import Node
from tests import test_manager as manager_fixtures
from tier_cache import Tier3ObservationFact, Tier3ObservationKind


def make_manager(path: Path):
    return manager_fixtures.ManagerTests()._minimal_manager(path, tier3_enabled=True)


def passed_fact(index: int) -> Tier3ObservationFact:
    return Tier3ObservationFact(
        node=Node(name=f"accepted-{index}", protocol="http", tier="tier3",
                  address="127.0.0.1", port=19000 + index,
                  uri=f"http://127.0.0.1:{19000 + index}",
                  extra={"tier3_origin": "tier3b"}),
        kind=Tier3ObservationKind.MIXED_PORT_PASS,
    )


def test_stop_preserves_all_accepted_pass_facts_and_rejects_late_facts(tmp_path):
    manager = make_manager(tmp_path)
    manager._queue_tier3_observation_facts([passed_fact(index) for index in range(3)])
    try:
        manager.request_stop()
        manager._queue_tier3_observation_facts([passed_fact(99)])
        assert manager.close(timeout_seconds=3) in {"STOPPED", "not_started"}
        payload = json.loads((tmp_path / "tier_observation_cache.json").read_text())
        assert len(payload["nodes"]) == 3
        assert all(record.get("last_mixed_port_pass_at") for record in payload["nodes"])
        assert manager._tier3_observation_batch_snapshot()["pending"] == 0
        assert manager._tier3_observation_batch_snapshot()["flushed"] == 3
    finally:
        manager.close(timeout_seconds=3)


def test_slow_observation_write_keeps_close_bounded_and_not_stopped(tmp_path):
    manager = make_manager(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = manager.observation_cache.observe_tier3_facts

    def slow_write(facts):
        entered.set()
        assert release.wait(3)
        original(facts)

    manager.observation_cache.observe_tier3_facts = slow_write
    manager._queue_tier3_observation_facts([passed_fact(1)])
    try:
        started = time.monotonic()
        manager.request_stop()
        assert time.monotonic() - started < .1
        assert entered.wait(.5)
        started = time.monotonic()
        assert manager.close(timeout_seconds=.05) == "STOP_FAILED_STREAM_ACTIVE"
        assert time.monotonic() - started < .5
        assert manager.tier3_last_collection_report.get("source_stream_closed") is False
        assert manager.tier3_last_collection_report["source_stream_observation_batch_inflight_count"] == 1
        release.set()
        assert manager.close(timeout_seconds=3) == "STOPPED"
        assert manager._tier3_observation_batch_snapshot()["pending"] == 0
    finally:
        release.set()
        manager.close(timeout_seconds=3)


def test_observation_worker_start_failure_does_not_lose_accepted_facts(tmp_path):
    manager = make_manager(tmp_path)
    try:
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("controlled start failure")):
            manager._queue_tier3_observation_facts([passed_fact(1)])
        assert manager._tier3_observation_batch_snapshot()["pending"] == 1
        assert manager.close(timeout_seconds=3) in {"STOPPED", "not_started"}
        payload = json.loads((tmp_path / "tier_observation_cache.json").read_text())
        assert len(payload["nodes"]) == 1
        assert manager._tier3_observation_batch_snapshot()["inflight"] == 0
    finally:
        manager.close(timeout_seconds=3)


def test_failed_observation_write_is_retained_for_next_bounded_close(tmp_path):
    manager = make_manager(tmp_path)
    original = manager.observation_cache.observe_tier3_facts

    def failing_write(_facts):
        raise OSError("controlled observation storage failure")

    manager.observation_cache.observe_tier3_facts = failing_write
    manager._queue_tier3_observation_facts([passed_fact(1)])
    try:
        assert manager.close(timeout_seconds=.1) == "STOP_FAILED_STREAM_ACTIVE"
        assert manager._tier3_observation_batch_snapshot()["pending"] == 1
        manager.observation_cache.observe_tier3_facts = original
        assert manager.close(timeout_seconds=3) == "STOPPED"
        assert manager._tier3_observation_batch_snapshot()["pending"] == 0
    finally:
        manager.observation_cache.observe_tier3_facts = original
        manager.close(timeout_seconds=3)


def test_close_waits_for_observation_flush_already_in_progress(tmp_path):
    manager = make_manager(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = manager.observation_cache.observe_tier3_facts

    def slow_write(facts):
        entered.set()
        assert release.wait(3)
        original(facts)

    manager.observation_cache.observe_tier3_facts = slow_write
    manager._queue_tier3_observation_facts([passed_fact(1)])
    writer = threading.Thread(target=manager._flush_tier3_observation_batch)
    writer.start()
    try:
        assert entered.wait(.5)
        assert manager.close(timeout_seconds=.05) == "STOP_FAILED_STREAM_ACTIVE"
        release.set()
        writer.join(1)
        assert not writer.is_alive()
        assert manager.close(timeout_seconds=3) == "STOPPED"
    finally:
        release.set()
        writer.join(3)
        manager.close(timeout_seconds=3)
