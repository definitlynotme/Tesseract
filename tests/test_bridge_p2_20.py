import atexit
from dataclasses import FrozenInstanceError
import json
import queue
import subprocess
import threading
from unittest.mock import patch

import pytest

import bridge_protocol as protocol
from bridge_killswitch import KillSwitchAdapter, state_dto
from killswitch import KillSwitchManager


@pytest.fixture
def make_manager():
    managers = []

    def make(capacity=16, runner=None):
        def fake_runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch("killswitch.platform.system", return_value="Windows"), patch.object(
            KillSwitchManager, "_detect_admin", return_value=True,
        ):
            manager = KillSwitchManager(
                {"enabled": True}, [], command_runner=runner or fake_runner,
                **({"event_capacity": capacity} if capacity is not None else {}),
            )
        managers.append(manager)
        return manager

    yield make
    for manager in managers:
        # All firewall execution uses the injected fake, including teardown.
        atexit.unregister(manager.cleanup)


def assert_templates_valid(frames):
    for rev, frame in enumerate(frames, 1):
        wire = {"t": "evt", "epoch": "test-rt5", "produced_at_ms": 1,
                "sent_at_ms": 1, "rev": rev, **frame}
        assert protocol.validate_frame(wire) == []


def test_same_tick_keeps_both_edges(make_manager):
    manager = make_manager()
    adapter = KillSwitchAdapter(manager)
    assert adapter.snapshot()["data"]["active"] is False
    assert manager.activate()
    assert manager.deactivate()
    frames = adapter.drain()
    assert [frame["kind"] for frame in frames] == ["edge", "edge"]
    assert [frame["data"]["active"] for frame in frames] == [True, False]
    assert [frame["seq"] for frame in frames] == [1, 2]
    assert adapter.drain() == []
    assert_templates_valid(frames)


def test_nested_force_and_reconfigure_edges(make_manager):
    manager = make_manager()
    assert manager.force_on()
    with patch.object(manager, "_detect_admin", return_value=True):
        manager.reconfigure({"enabled": True}, [])
    batch = manager.drain(0)
    assert [e.reason for e in batch.events] == [
        "activate", "force_on", "reconfigure", "activate", "force_on",
    ]
    assert [e.active for e in batch.events] == [True, True, False, True, True]
    assert [e.ks_seq for e in batch.events] == list(range(1, 6))
    assert manager.force_off()
    assert [e.reason for e in manager.drain(5).events] == ["deactivate", "force_off"]
    assert manager.get_state()["manual_override"] == "none"


def test_other_thread_cannot_consume_until_outermost_lock_released(make_manager):
    manager = make_manager()
    started, finished = threading.Event(), threading.Event()
    result = []

    def consumer():
        started.set()
        result.append(manager.drain(0))
        finished.set()

    with manager.lock:
        manager.force_on()  # outer lock -> force_on -> activate
        worker = threading.Thread(target=consumer)
        worker.start()
        assert started.wait(1)
        assert not finished.wait(0.02)
        manager.force_off()
    assert finished.wait(1)
    worker.join(1)
    assert not worker.is_alive()
    assert [e.reason for e in result[0].events] == [
        "activate", "force_on", "deactivate", "force_off",
    ]


def test_sink_runs_without_manager_lock_and_reentrancy_is_rejected(make_manager):
    manager = make_manager()
    manager.force_on()
    adapter = KillSwitchAdapter(manager)
    accepted = []

    def sink(frame):
        finished = threading.Event()

        def another_thread():
            manager.snapshot_with_seq()
            finished.set()

        worker = threading.Thread(target=another_thread)
        worker.start()
        assert finished.wait(1)  # Cannot pass if caller still owns outer RLock.
        worker.join(1)
        assert adapter.dispatch(lambda _: None) is False
        accepted.append(frame)

    assert adapter.dispatch(sink)
    assert len(accepted) == 2


def test_sink_exception_preserves_retry_and_does_not_affect_killswitch(make_manager):
    manager = make_manager(2)
    adapter = KillSwitchAdapter(manager)
    manager.activate()
    manager.deactivate()
    accepted = []

    def failing_sink(frame):
        if not accepted:
            accepted.append(frame)
        else:
            frame["data"]["active"] = True
            raise RuntimeError("fake pipe failure")

    assert not adapter.dispatch(failing_sink)
    assert manager.get_state()["active"] is False
    assert len(adapter._pending) == 1
    with pytest.raises(RuntimeError):
        adapter.drain()
    with pytest.raises(RuntimeError):
        adapter.snapshot()
    manager.activate()
    manager.deactivate()
    manager.activate()  # Pending tail stays bounded while source ring overflows.
    assert manager.get_state()["active"] is True
    assert len(adapter._pending) == 1
    assert adapter.dispatch(accepted.append)
    assert [frame["seq"] for frame in accepted] == [1, 2]
    assert accepted[-1]["data"]["active"] is False
    frames = adapter.drain()
    assert [frame["kind"] for frame in frames] == ["gap", "edge", "edge", "snapshot"]
    assert_templates_valid(frames)


def test_full_bounded_sink_retries_without_advancing_again(make_manager):
    manager = make_manager()
    manager.activate()
    adapter = KillSwitchAdapter(manager)
    sink_queue = queue.Queue(maxsize=1)
    sink_queue.put("occupied")
    assert not adapter.dispatch(sink_queue.put_nowait)
    assert adapter.ks_seq == 1
    assert len(adapter._pending) == 1
    sink_queue.get_nowait()
    assert adapter.dispatch(sink_queue.put_nowait)
    assert sink_queue.get_nowait()["seq"] == 1
    assert adapter.drain() == []


def test_overflow_gap_and_snapshot_use_separate_topic_sequence(make_manager):
    manager = make_manager(2)
    adapter = KillSwitchAdapter(manager)
    adapter.snapshot()
    for _ in range(2):
        manager.activate()
        manager.deactivate()
    batch = manager.drain(0)
    assert (batch.gap.from_seq, batch.gap.to_seq) == (1, 2)
    assert [event.ks_seq for event in batch.events] == [3, 4]
    frames = adapter.drain()
    assert frames[0] == {"topic": "killswitch.state", "kind": "gap", "seq": 3,
                         "data": {"from_seq": 1, "to_seq": 2, "dropped": 2}}
    assert [frame["seq"] for frame in frames if "seq" in frame] == [3, 4, 5]
    assert frames[-1]["kind"] == "snapshot"
    assert frames[-1]["data"] == state_dto(batch.snapshot)
    manager.activate()
    assert adapter.drain()[0]["seq"] == 6
    for _ in range(2):
        manager.deactivate()
        manager.activate()
    next_frames = adapter.drain()
    assert next_frames[0]["data"] == {"from_seq": 7, "to_seq": 8, "dropped": 2}
    assert next_frames[0]["seq"] == 9
    assert_templates_valid(frames + next_frames)


def test_source_gap_is_cursor_relative_and_reads_do_not_delete(make_manager):
    manager = make_manager(1)
    manager.activate()
    manager.deactivate()
    manager.activate()
    assert manager.drain(0).gap.from_seq == 1
    assert manager.drain(1).gap.from_seq == 2
    assert manager.drain(2).gap is None
    assert manager.drain(2) == manager.drain(2)
    assert manager.drain(3).events == ()
    for invalid in (-1, 4, True, 1.0):
        with pytest.raises(ValueError):
            manager.drain(invalid)


def test_snapshot_then_concurrent_delta_neither_misses_nor_duplicates(make_manager):
    manager = make_manager()
    manager.activate()
    snapshot = manager.snapshot_with_seq()
    adapter = KillSwitchAdapter(manager)
    assert adapter.snapshot()["data"]["active"] is True

    def producer():
        manager.deactivate()
        manager.activate()

    worker = threading.Thread(target=producer)
    worker.start()
    worker.join(1)
    assert not worker.is_alive()
    batch = manager.drain(snapshot.ks_seq)
    assert [e.ks_seq for e in batch.events] == [2, 3]
    assert [f["data"]["active"] for f in adapter.drain()] == [False, True]
    assert adapter.drain() == []
    assert snapshot.ks_seq == 1 and snapshot.active is True
    with pytest.raises(FrozenInstanceError):
        snapshot.active = False
    with pytest.raises(FrozenInstanceError):
        batch.events[0].ks_seq = 99


def test_error_raw_text_never_leaks_and_force_semantics_unchanged(make_manager):
    def runner(command):
        if "add" in command:
            raise RuntimeError(r"C:\private\secret.exe ProxyManager-KillSwitch-Block 中文")
        return subprocess.CompletedProcess(command, 0, "", "")

    manager = make_manager(runner=runner)
    adapter = KillSwitchAdapter(manager)
    assert manager.force_on()  # Existing behavior after activation failure.
    assert manager.state == "triggered" and manager.active is False
    batch = manager.drain(0)
    assert [e.reason for e in batch.events] == ["error", "force_on"]
    assert all(e.error_code == "io" for e in batch.events)
    frames = adapter.drain()
    raw = json.dumps(frames, ensure_ascii=False)
    assert "private" not in raw and "ProxyManager" not in raw and "中文" not in raw
    assert "private" not in repr(batch)
    assert frames[0]["data"]["error"]["code"] == "io"
    assert_templates_valid(frames)
    manager.reconfigure({"enabled": False}, [])
    assert manager.snapshot_with_seq().error_code is None


def test_error_when_refreshing_active_rule_is_recorded(make_manager):
    manager = make_manager()
    manager.activate()
    with patch.object(manager, "_refresh_active_tier1_rule_if_due", side_effect=OSError("raw")):
        assert not manager.activate()
    assert manager.drain(1).events[0].reason == "error"
    assert manager.snapshot_with_seq().active is False


def test_already_forced_refresh_failure_keeps_error_and_force_edges(make_manager):
    manager = make_manager()
    manager.force_on()
    baseline = manager.snapshot_with_seq().ks_seq
    with patch.object(manager, "_refresh_active_tier1_rule_if_due", side_effect=OSError("raw")):
        assert not manager.force_on()  # Preserve the existing return value.
    batch = manager.drain(baseline)
    assert [e.reason for e in batch.events] == ["error", "force_on"]
    assert manager.manual_override == "forced_on" and manager.state == "triggered"
    assert manager.active is False
    assert all(e.error_code == "io" for e in batch.events)


def test_support_change_cleanup_and_reconfigure_are_observable(make_manager):
    manager = make_manager()
    manager.activate()
    manager.admin_available = False
    assert not manager.deactivate()
    assert manager.drain(1).events[0].reason == "disabled"
    with patch.object(manager, "_detect_admin", return_value=True):
        manager.reconfigure({"enabled": True}, [])
    assert manager.cleanup_stale_rules()
    manager.force_on()
    manager.cleanup()
    events = manager.drain(0).events
    assert [e.reason for e in events] == [
        "activate", "disabled", "reconfigure", "cleanup_stale_rules",
        "activate", "force_on", "cleanup",
    ]
    assert events[-1].active is False
    assert manager.manual_override == "none"
    manager.reconfigure({"enabled": False}, [])
    assert manager.drain(events[-1].ks_seq).events[0].enabled is False


@pytest.mark.parametrize("capacity", [0, None])
def test_disabled_outlet_allocates_no_ring_or_event_records(make_manager, capacity):
    manager = make_manager(capacity)
    assert manager._events is None
    with patch.object(manager, "_event_snapshot", side_effect=AssertionError("allocated event")):
        manager.force_on()
        manager.force_off()
        with patch.object(manager, "_detect_admin", return_value=True):
            manager.reconfigure({"enabled": True}, [])
        manager.cleanup_stale_rules()
        manager.cleanup()
    assert manager._events is None and manager._ks_seq == 0
    assert manager.drain(0).events == ()


@pytest.mark.parametrize("capacity", [-1, True, 1.5])
def test_invalid_ring_capacity_is_rejected(make_manager, capacity):
    with pytest.raises(ValueError):
        make_manager(capacity)


@pytest.mark.parametrize("change_before_lock", [True, False])
def test_snapshot_lock_boundary_partitions_each_edge_once(make_manager, change_before_lock):
    from concurrent.futures import ThreadPoolExecutor

    manager = make_manager()
    adapter = KillSwitchAdapter(manager)
    reached, release, producer_attempted = (threading.Event() for _ in range(3))
    original_lock = manager.lock
    snapshot_thread = []

    class SnapshotGate:
        def __enter__(self):
            is_snapshot = threading.get_ident() in snapshot_thread
            if is_snapshot and change_before_lock:
                reached.set()
                assert release.wait(2)
            original_lock.acquire()
            if is_snapshot and not change_before_lock:
                reached.set()
                assert release.wait(2)
            return self

        def __exit__(self, *args):
            original_lock.release()

    def take_snapshot():
        snapshot_thread.append(threading.get_ident())
        return adapter.snapshot()

    def activate():
        producer_attempted.set()
        return manager.activate()

    manager.lock = SnapshotGate()
    with ThreadPoolExecutor(max_workers=2) as pool:
        snapshot_future = pool.submit(take_snapshot)
        try:
            assert reached.wait(2)
            producer = pool.submit(activate)
            assert producer_attempted.wait(2)
            if change_before_lock:
                assert producer.result(timeout=2)
            else:
                assert not producer.done()  # Snapshot already owns the source lock.
        finally:
            release.set()
        snapshot = snapshot_future.result(timeout=2)
        assert producer.result(timeout=2)
    watermark = adapter.ks_seq
    assert watermark == int(change_before_lock)
    assert snapshot["data"]["active"] is change_before_lock
    assert manager.deactivate()
    tail = manager.drain(watermark)
    assert tail.gap is None
    # Source edges covered by the baseline and unread tail partition [1, 2].
    covered = list(range(1, watermark + 1)) + [e.ks_seq for e in tail.events]
    assert covered == [1, 2] and len(set(covered)) == 2
    frames = adapter.drain()
    assert [f["data"]["active"] for f in frames] == (
        [False] if change_before_lock else [True, False])
    assert [f["seq"] for f in frames] == list(range(1, len(frames) + 1))
    assert adapter.ks_seq == 2 and adapter.drain() == []
    assert_templates_valid([snapshot] + frames)
