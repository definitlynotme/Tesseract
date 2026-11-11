import json
from collections import deque
from datetime import datetime, timezone
from threading import Event, RLock, Thread

import pytest

import traffic_monitor as module
from traffic_monitor import MihomoTrafficMonitor


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def monitor_at(tmp_path, now, **kwargs):
    return MihomoTrafficMonitor(base_url="http://127.0.0.1:1",
                               history_path=tmp_path / "traffic.json", now_fn=lambda: now[0], **kwargs)


def record(monitor, epoch, up=10, down=20):
    monitor.record_sample({"up": up, "down": down}, timestamp_epoch=epoch)


def test_atomic_tick_one_lock_no_history_scan_and_detached_copies(tmp_path):
    monitor = monitor_at(tmp_path, [600])
    record(monitor, 600)
    record(monitor, 601, 30, 40)

    class CountLock:
        def __init__(self):
            self.lock, self.entries = RLock(), 0
        def __enter__(self):
            self.entries += 1
            self.lock.acquire()
        def __exit__(self, *args):
            self.lock.release()

    class NoScan(deque):
        def __iter__(self):
            raise AssertionError("tick must not scan history")

    monitor._history = NoScan(monitor._history)
    monitor._lock = CountLock()
    tick = monitor.tick_snapshot()
    assert monitor._lock.entries == 1
    assert tick == {"supported": True, "running": False, "error": None,
                    "latest": {"timestamp": iso(601), "up": 30, "down": 40},
                    "session": {"upload_bytes": 30, "download_bytes": 40}, "history_seq": 2}
    tick["latest"]["up"] = 999
    tick["session"]["upload_bytes"] = 999
    assert monitor.latest()["latest"]["up"] == 30
    assert monitor.session()["upload_bytes"] == 30
    monitor._error = "stream unavailable"
    assert monitor.latest()["error"] == "stream unavailable"


def test_empty_monitor_and_gaps_are_not_zero_samples(tmp_path):
    monitor = monitor_at(tmp_path, [600])
    assert monitor.tick_snapshot()["latest"] is None
    assert monitor.tick_snapshot()["history_seq"] is None
    assert monitor.history_buckets()["h24"] == []
    record(monitor, 600, 0, 0)
    record(monitor, 600.5, 20, 40)
    record(monitor, 1200, 30, 60)
    buckets = monitor.history_buckets()
    assert buckets["h24"] == [
        {"seq": 2, "t_ms": 600000, "n": 2, "up_bps": 10, "down_bps": 20},
        {"seq": 3, "t_ms": 900000, "n": 0, "up_bps": None, "down_bps": None},
        {"seq": 4, "t_ms": 1200000, "n": 1, "up_bps": 30, "down_bps": 60},
    ]
    assert len(buckets["live"]) == 300
    assert sum(row["n"] for row in buckets["live"]) == 1
    assert all(row["up_bps"] is None for row in buckets["live"][:-1])
    buckets["h24"][0]["n"] = 999
    assert monitor.history_buckets()["h24"][0]["n"] == 2


def test_288_and_300_limits_with_incremental_bucket_means(tmp_path):
    monitor = monitor_at(tmp_path, [100000])
    monitor._should_save_history_locked = lambda epoch: False
    for index in range(290):
        record(monitor, index * 300, index, index * 2)
    for offset in range(1, 302):
        record(monitor, 289 * 300 + offset, 8, 16)
    history = monitor.history_buckets()
    assert len(history["h24"]) == 288
    assert len(history["live"]) == 300
    assert history["h24"][0]["seq"] == 3
    assert history["live"][0]["seq"] == 289 * 300 + 2
    assert history["h24"][-2]["n"] == 300
    assert history["h24"][-2]["up_bps"] == (289 + 299 * 8) / 300
    assert all(row["n"] == 1 for row in history["live"])


def test_legacy_file_migrates_once_and_ignores_invalid_rows(tmp_path, monkeypatch):
    rows = [{"timestamp": iso(0), "up": 1, "down": 1},
            {"timestamp": "bad", "up": 2, "down": 4},
            {"timestamp": iso(100000), "up": 2, "down": 4},
            {"timestamp": iso(100000.5), "up": 6, "down": 8},
            {"timestamp": iso(100000.6), "up": float("nan"), "down": 8}]
    (tmp_path / "traffic.json").write_bytes(json.dumps(rows).encode())
    monitor = monitor_at(tmp_path, [100001])
    assert len(monitor.snapshot()["history"]) == 2
    assert monitor.history_buckets()["h24"][-1]["n"] == 2
    assert monitor.history_buckets()["live"][-1]["up_bps"] == 4
    assert monitor.session() == {"upload_bytes": 0, "download_bytes": 0}
    monkeypatch.setattr(module, "_epoch_from_iso", lambda _: pytest.fail("history reparsed after load"))
    for _ in range(3):
        monitor.tick_snapshot()
        monitor.history_buckets()
        monitor.snapshot()
    record(monitor, 100002)


def test_new_file_is_legacy_readable_and_preserves_aggregates_after_raw_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "TRAFFIC_RAW_HISTORY_LIMIT", 3)
    monitor = monitor_at(tmp_path, [600])
    monitor._should_save_history_locked = lambda epoch: False
    for index in range(5):
        record(monitor, 600 + index * .1, index, index * 2)
    monitor.stop()
    payload = json.loads(monitor.history_path.read_bytes())
    assert isinstance(payload, list) and len(payload) == 3
    # Exact legacy loader/pruner contract: a JSON list of timestamp/up/down rows.
    legacy_rows = [item for item in payload if isinstance(item, dict)
                   and module._epoch_from_iso(item.get("timestamp")) >= 600 - 86400]
    assert len(legacy_rows) == 3
    assert all({"timestamp", "up", "down"} <= row.keys() for row in legacy_rows)
    assert payload[-1]["_traffic_buckets_v1"]["h24"]["buckets"][0]["n"] == 5
    rebuilt = monitor_at(tmp_path, [600])
    assert rebuilt.history_buckets() == monitor.history_buckets()
    assert rebuilt.latest() == monitor.latest()
    assert rebuilt.session() == {"upload_bytes": 0, "download_bytes": 0}


def test_invalid_bucket_extension_falls_back_to_legacy_rows(tmp_path):
    monitor = monitor_at(tmp_path, [600])
    record(monitor, 600)
    payload = json.loads(monitor.history_path.read_bytes())
    payload[-1]["_traffic_buckets_v1"]["h24"]["buckets"][0]["n"] = -1
    monitor.history_path.write_bytes(json.dumps(payload).encode())
    rebuilt = monitor_at(tmp_path, [600])
    assert rebuilt.history_buckets()["h24"][0]["n"] == 1
    assert rebuilt.latest()["error"] is None


def test_stop_start_retains_data_but_rebuilt_monitor_resets_session(tmp_path):
    now = [600]
    monitor = monitor_at(tmp_path, now, opener=lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")))
    record(monitor, 600)
    record(monitor, 602, 30, 40)
    before = monitor.tick_snapshot()
    buckets = monitor.history_buckets()
    monitor.stop()
    now[0] = 1000000
    assert monitor.tick_snapshot() == before
    assert monitor.history_buckets() == buckets
    now[0] = 603
    monitor.start()
    monitor._thread.join(timeout=2)
    assert not monitor._thread.is_alive()
    assert monitor.session() == before["session"]
    record(monitor, 603, 100, 200)
    assert monitor.session() == before["session"]  # Restart does not integrate a stream gap.
    monitor.stop()
    rebuilt = monitor_at(tmp_path, now)
    assert rebuilt.session() == {"upload_bytes": 0, "download_bytes": 0}
    assert rebuilt.history_buckets() == monitor.history_buckets()
    record(rebuilt, 604)
    assert rebuilt.session() == {"upload_bytes": 0, "download_bytes": 0}


def test_stream_disconnect_and_silent_period_do_not_make_samples(tmp_path):
    waiting, release = Event(), Event()
    now = [600]

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.close()
        def __iter__(self):
            yield b'{"up":10,"down":20}\n'
            waiting.set()
            assert release.wait(2)
            yield b'\n'
            raise OSError("disconnected")
        def close(self):
            release.set()

    monitor = monitor_at(tmp_path, now, opener=lambda *args, **kwargs: Response())
    monitor.start()
    try:
        assert waiting.wait(2)
        before = monitor.history_buckets()
        now[0] = 1200
        for _ in range(5):
            assert monitor.history_buckets() == before
        release.set()
        monitor._thread.join(timeout=2)
        assert not monitor._thread.is_alive()
        assert monitor.history_buckets() == before
        assert monitor.session() == {"upload_bytes": 0, "download_bytes": 0}
        assert monitor.tick_snapshot()["latest"]["timestamp"] == iso(600)
        assert monitor.tick_snapshot()["error"] == "disconnected"
        assert not monitor.tick_snapshot()["running"]
    finally:
        release.set()
        monitor.stop()


def test_forward_and_backward_jumps_keep_unique_buckets_and_real_sample_time(tmp_path):
    monitor = monitor_at(tmp_path, [600])
    monitor._should_save_history_locked = lambda epoch: False
    record(monitor, 600)
    record(monitor, 1200)
    total = monitor.session()
    record(monitor, 601, 30, 60)
    assert monitor.session() == total
    assert monitor.tick_snapshot()["latest"]["timestamp"] == iso(601)
    assert monitor.tick_snapshot()["history_seq"] == 4
    assert monitor.history_buckets()["h24"][0]["n"] == 2
    assert monitor.history_buckets()["h24"][0]["up_bps"] == 20
    record(monitor, 200000)
    after_forward = monitor.history_buckets()
    assert monitor.session()["upload_bytes"] == total["upload_bytes"] + 50
    record(monitor, 600)  # Too old for either ring, cannot overwrite a newer slot.
    assert monitor.history_buckets() == after_forward
    assert monitor.tick_snapshot()["latest"]["timestamp"] == iso(600)
    for rows in (after_forward["live"], after_forward["h24"]):
        assert len({row["seq"] for row in rows}) == len(rows)
        assert all(row["n"] >= 0 and (row["up_bps"] is None or row["up_bps"] >= 0) for row in rows)
    record(monitor, 200001)
    assert monitor.history_buckets()["h24"][-1]["n"] == 2


@pytest.mark.parametrize("jump", [0, 10_000_000])
@pytest.mark.parametrize("save_due", [False, True])
def test_full_history_record_operation_count_is_bounded(tmp_path, jump, save_due, monkeypatch):
    monitor = monitor_at(tmp_path, [100000])

    class CountDeque(deque):
        visits = 0
        pops = 0
        indexes = 0
        appends = 0
        def __iter__(self):
            for value in super().__iter__():
                self.visits += 1
                yield value
        def __getitem__(self, index):
            self.indexes += 1
            return super().__getitem__(index)
        def popleft(self):
            self.pops += 1
            return super().popleft()
        def append(self, value):
            self.appends += 1
            return super().append(value)

    class CountSlots(list):
        reads = 0
        writes = 0
        visits = 0
        def __iter__(self):
            for value in super().__iter__():
                self.visits += 1
                yield value
        def __getitem__(self, index):
            self.reads += 1
            return super().__getitem__(index)
        def __setitem__(self, index, value):
            self.writes += 1
            return super().__setitem__(index, value)

    count = module.TRAFFIC_RAW_HISTORY_LIMIT
    monitor._history = CountDeque(({"timestamp": iso(100000 - count + i), "up": 1, "down": 1}
                                  for i in range(count)), maxlen=count)
    monitor._history_epochs = CountDeque(range(100000 - count, 100000), maxlen=count)
    for buckets in (monitor._live, monitor._h24):
        buckets.slots = CountSlots(buckets.slots)
    monitor._last_history_save_epoch = 100000 + jump - (30 if save_due else 0)
    copies = []
    monkeypatch.setattr(monitor, "_write_history",
                        lambda history, version, **kwargs: copies.append(history))
    monkeypatch.setattr(module, "_epoch_from_iso", lambda _: pytest.fail("record parsed history"))
    record(monitor, 100000 + jump)
    for history in (monitor._history, monitor._history_epochs):
        assert history.visits <= count if save_due else history.visits == 0
        assert history.appends == 1
        assert history.pops <= module.TRAFFIC_HISTORY_PRUNE_BUDGET
        assert history.indexes <= module.TRAFFIC_HISTORY_PRUNE_BUDGET
        assert len(history) <= count
    for buckets in (monitor._live, monitor._h24):
        assert buckets.slots.reads == 1
        assert buckets.slots.writes == 1
        assert buckets.slots.visits == (buckets.limit if save_due else 0)
    assert len(copies) == int(save_due)
    if save_due:
        assert len(copies[0]) <= count
        assert monitor._history.visits == len(copies[0])


def test_lightweight_readers_work_during_record_save(tmp_path, monkeypatch):
    monitor = monitor_at(tmp_path, [600])
    entered, release = Event(), Event()
    original = module.os.replace

    def blocked_replace(source, target):
        entered.set()
        assert release.wait(2)
        original(source, target)

    monkeypatch.setattr(module.os, "replace", blocked_replace)
    worker = Thread(target=record, args=(monitor, 600))
    worker.start()
    try:
        assert entered.wait(2)
        assert monitor._lock.acquire(timeout=1)
        monitor._lock.release()
        assert monitor.tick_snapshot()["latest"]["timestamp"] == iso(600)
        assert monitor.latest()["latest"]["up"] == 10
        assert monitor.session()["upload_bytes"] == 0
        assert monitor.history_buckets()["h24"][0]["n"] == 1
    finally:
        release.set()
        worker.join(timeout=2)
    assert not worker.is_alive()


def test_disk_snapshot_is_bounded_and_io_does_not_block_tick_or_lose_dirty_state(tmp_path, monkeypatch):
    monitor = monitor_at(tmp_path, [600])
    monitor._last_history_save_epoch = 600
    record(monitor, 600)
    entered, release = Event(), Event()
    original = module.os.replace
    snapshots = []

    def blocked_replace(source, target):
        snapshots.append(json.loads(source.read_bytes()))
        entered.set()
        assert release.wait(2)
        original(source, target)

    monkeypatch.setattr(module.os, "replace", blocked_replace)
    writer = Thread(target=monitor._save_history_locked, kwargs={"saved_at_epoch": 600})
    writer.start()
    try:
        assert entered.wait(2)
        # Acquiring from another thread while disk I/O is blocked proves the lock boundary.
        assert monitor._lock.acquire(timeout=1)
        monitor._lock.release()
        record(monitor, 601, 30, 40)
        assert monitor.tick_snapshot()["latest"]["timestamp"] == iso(601)
        assert len(snapshots[0]) <= module.TRAFFIC_RAW_HISTORY_LIMIT
        assert len(snapshots[0][-1]["_traffic_buckets_v1"]["live"]["buckets"]) <= 300
        assert len(snapshots[0][-1]["_traffic_buckets_v1"]["h24"]["buckets"]) <= 288
    finally:
        release.set()
        writer.join(timeout=2)
    assert not writer.is_alive() and monitor._history_dirty
    monitor.stop()
    rebuilt = monitor_at(tmp_path, [601])
    assert rebuilt.latest()["latest"]["timestamp"] == iso(601)
    assert rebuilt.history_buckets()["h24"][0]["n"] == 2


def test_legacy_snapshot_keys_and_negative_rate_behavior_stay_compatible(tmp_path):
    monitor = monitor_at(tmp_path, [600])
    record(monitor, 600, -1, -2)
    record(monitor, 601, -1, -2)
    snapshot = monitor.snapshot()
    assert set(snapshot) == {"supported", "running", "latest", "history", "session", "session_totals", "error"}
    assert snapshot["latest"] == {"timestamp": iso(601), "up": -1, "down": -2}
    assert snapshot["session"] == snapshot["session_totals"] == {"upload_bytes": 0, "download_bytes": 0}
    assert monitor.history_buckets()["h24"][0]["up_bps"] == 0


def test_legacy_writer_append_does_not_reuse_stale_bucket_extension(tmp_path):
    monitor = monitor_at(tmp_path, [600])
    record(monitor, 600)
    payload = json.loads(monitor.history_path.read_bytes())
    # Old versions retain unknown keys but append ordinary rows. The extension
    # is no longer on the last row, so it must not override the newer sample.
    payload.append({"timestamp": iso(601), "up": 30, "down": 40})
    monitor.history_path.write_bytes(json.dumps(payload).encode())
    rebuilt = monitor_at(tmp_path, [601])
    assert rebuilt.latest()["latest"]["timestamp"] == iso(601)
    assert rebuilt.history_buckets()["h24"][0]["n"] == 2
    assert rebuilt.history_buckets()["h24"][0]["up_bps"] == 20


@pytest.mark.parametrize("outside_window", [False, True])
def test_time_jumps_save_and_rebuild_preserve_watermarks_and_counts(tmp_path, outside_window):
    now = [200000 if outside_window else 1200]
    monitor = monitor_at(tmp_path, now)
    monitor._should_save_history_locked = lambda epoch: False

    def check(current, latest, anchor, live_count, h24_count, session):
        tick = current.tick_snapshot()
        buckets = current.history_buckets()
        assert tick["latest"]["timestamp"] == iso(latest)
        assert tick["history_seq"] == buckets["history_seq"] == anchor // 300
        assert buckets["live"][-1]["seq"] == anchor
        assert buckets["h24"][-1]["seq"] == anchor // 300
        assert sum(row["n"] for row in buckets["live"]) == live_count
        assert sum(row["n"] for row in buckets["h24"]) == h24_count
        assert tick["session"] == session
        for rows in (buckets["live"], buckets["h24"]):
            assert len({row["seq"] for row in rows}) == len(rows)

    zero = {"upload_bytes": 0, "download_bytes": 0}
    total = {"upload_bytes": 50, "download_bytes": 100}
    record(monitor, 600)
    check(monitor, 600, 600, 1, 1, zero)
    forward = now[0]
    record(monitor, forward)
    check(monitor, forward, forward, 1, 1 if outside_window else 2, total)
    backward = 600 if outside_window else 601
    record(monitor, backward, 30, 60)
    check(monitor, backward, forward, 1, 1 if outside_window else 3, total)
    before = monitor.history_buckets()
    monitor.stop()  # Explicit persistence after both jumps.
    check(monitor, backward, forward, 1, 1 if outside_window else 3, total)
    payload = json.loads(monitor.history_path.read_bytes())[-1]["_traffic_buckets_v1"]
    assert payload["latest"]["timestamp"] == iso(backward)
    assert payload["anchor_epoch"] == forward
    assert payload["live"]["last_seq"] == forward
    assert payload["h24"]["last_seq"] == forward // 300
    rebuilt = monitor_at(tmp_path, now)
    check(rebuilt, backward, forward, 1, 1 if outside_window else 3, zero)
    assert rebuilt.history_buckets() == before
    record(rebuilt, forward + 1)
    check(rebuilt, forward + 1, forward + 1, 2, 2 if outside_window else 4, zero)


def test_runtime_reload_replaces_monitor_and_same_instance_restart_keeps_totals(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    import desktop_runtime as runtime_module

    now = [602]
    built = []

    def offline(*args, **kwargs):
        raise OSError("isolated stream")  # Never call a socket or start Mihomo.

    def build_monitor(**kwargs):
        result = MihomoTrafficMonitor(**kwargs, now_fn=lambda: now[0], opener=offline)
        built.append(result)
        return result

    runtime = Mock()
    runtime.app_paths = SimpleNamespace(data_dir=tmp_path, config_path=tmp_path / "config.yaml",
                                       local_config_path=tmp_path / "local.yaml")
    runtime._disable_external_desktop_notifications.side_effect = lambda config: config
    runtime._client_config.return_value = {}
    runtime._mihomo_controller_config.return_value = {"enabled": True}
    runtime.mihomo_controller_base_url.return_value = "http://127.0.0.1:1"
    runtime._build_traffic_monitor.side_effect = lambda: runtime_module.DesktopRuntime._build_traffic_monitor(runtime)
    runtime._current_core_runtime_phase.return_value = "formal_no_capture"
    runtime._runtime_traffic_capture_wanted.return_value = False
    runtime._runtime_initial_activation_complete.return_value = True
    runtime.core_runner.is_running.return_value = True
    runtime._runtime_intent_lock = RLock()
    runtime.stop_event = Event()
    runtime.manager = None
    runtime.status.return_value = {"isolated": True}
    monkeypatch.setattr(runtime_module, "bootstrap_user_files", Mock())
    monkeypatch.setattr(runtime_module, "load_config", Mock(return_value={}))
    monkeypatch.setattr(runtime_module, "apply_release_path_overrides", lambda config, paths: config)
    monkeypatch.setattr(runtime_module, "log_event", Mock())
    monkeypatch.setattr(runtime_module, "MihomoTrafficMonitor", build_monitor)
    old = runtime.traffic_monitor = runtime._build_traffic_monitor()
    record(old, 600)
    record(old, 602, 30, 40)
    assert old.session() == {"upload_bytes": 60, "download_bytes": 80}
    old_buckets = old.history_buckets()
    try:
        assert runtime_module.DesktopRuntime.reload_config(runtime) == {"isolated": True}
        new = runtime.traffic_monitor
        assert built == [old, new] and new is not old
        assert new.history_path == tmp_path / "traffic-history.json"
        assert not old.tick_snapshot()["running"]
        new._thread.join(timeout=2)  # reload actually invoked start on the new monitor.
        assert not new._thread.is_alive()
        assert new.session() == {"upload_bytes": 0, "download_bytes": 0}
        assert new.history_buckets() == old_buckets
        record(new, 603)
        record(new, 605, 30, 40)
        total, buckets = new.session(), new.history_buckets()
        assert total == {"upload_bytes": 60, "download_bytes": 80}
        new.stop()
        new.start()
        new._thread.join(timeout=2)
        assert not new._thread.is_alive()
        assert runtime.traffic_monitor is new
        assert new.session() == total and new.history_buckets() == buckets
        new.stop()
        assert new.session() == total
        assert old.session() == total
    finally:
        for monitor in built:
            monitor.stop()
