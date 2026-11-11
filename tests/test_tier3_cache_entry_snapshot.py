"""The source entry gate must not wait on a full-cache fsync/replace."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import threading

import pytest

import tier_cache as module
from models import NodeStatus
from tests.test_tier_cache import make_node
from tier_cache import Tier3ObservationFact, Tier3ObservationKind, TierObservationCache


def fail(cache, node):
    cache.observe_tier3_facts([
        Tier3ObservationFact(node, Tier3ObservationKind.POOL_HEALTH_FAIL),
    ])


@pytest.mark.parametrize("fail_write", [False, True])
def test_entry_gate_reads_completed_snapshot_during_blocked_persistence(tmp_path, monkeypatch, fail_write):
    cache = TierObservationCache(tmp_path / "cache.json")
    old = make_node("old-failure", NodeStatus.DEAD)
    new = make_node("new-failure", NodeStatus.DEAD)
    fail(cache, old)
    entered, release, reader_done = threading.Event(), threading.Event(), threading.Event()
    replace = module.replace_with_retry

    def paused_replace(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        if fail_write:
            raise OSError("fixture disk failure")
        return replace(*args, **kwargs)

    monkeypatch.setattr(module, "replace_with_retry", paused_replace)

    def read():
        try:
            return cache.filter_recently_failed_tier3([old, new], cooldown_hours=6)
        finally:
            reader_done.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(fail, cache, new)
        assert entered.wait(5)
        reader = pool.submit(read)
        try:
            assert reader_done.wait(0.5), "source entry blocked on cache persistence lock"
            assert reader.result() == ([new], 1)
        finally:
            release.set()
        if fail_write:
            with pytest.raises(OSError, match="fixture disk failure"):
                writer.result()
        else:
            writer.result()
        reader.result()
    assert cache.filter_recently_failed_tier3([old, new], cooldown_hours=6) == (
        ([new], 1) if fail_write else ([], 2)
    )


def test_external_atomic_cache_update_invalidates_published_entry_view(tmp_path):
    cache = TierObservationCache(tmp_path / "cache.json")
    node = make_node("external-failure", NodeStatus.DEAD)
    assert cache.filter_recently_failed_tier3([node], cooldown_hours=6) == ([node], 0)
    other = TierObservationCache(cache.path)
    fail(other, node)
    assert cache.filter_recently_failed_tier3([node], cooldown_hours=6) == ([], 1)
    assert cache.filter_recently_failed_tier3(
        [node], cooldown_hours=6, exempt_identities={cache._node_identity(node)},
    ) == ([node], 0)
    other.observe_tier3_facts([
        Tier3ObservationFact(node, Tier3ObservationKind.MIXED_PORT_PASS,
                            (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()),
    ])
    assert cache.filter_recently_failed_tier3([node], cooldown_hours=6) == ([node], 0)


def test_entry_snapshot_does_not_share_mutable_records_and_respects_expiry(tmp_path):
    cache = TierObservationCache(tmp_path / "cache.json")
    node = make_node("expired-negative", NodeStatus.DEAD)
    cache.observe_tier3_facts([
        Tier3ObservationFact(node, Tier3ObservationKind.POOL_HEALTH_FAIL,
                            (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()),
    ])
    assert cache.filter_recently_failed_tier3([node], cooldown_hours=6) == ([], 1)
    assert cache.filter_recently_failed_tier3([node], cooldown_hours=1) == ([node], 0)
    payload = cache.load()
    payload["negative"].clear()
    assert cache.filter_recently_failed_tier3([node], cooldown_hours=6) == ([], 1)
