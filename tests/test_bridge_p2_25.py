"""P2-25: runtime monitor identity, continuity and unchanged P1 wire contracts."""
from copy import deepcopy
from datetime import datetime, timezone
from threading import Event
import time

import pytest

import bridge_protocol as protocol
from bridge_publish import Publisher
from bridge_runtime_access import CAPABILITIES, RuntimeAccess, traffic_monitor
from bridge_state import PublicationState
from bridge_traffic import TrafficCollector, history_data
from traffic_monitor import MihomoTrafficMonitor


def iso(t_ms):
    return datetime.fromtimestamp(t_ms / 1000, timezone.utc).isoformat()


class Monitor:
    def __init__(self, t_ms=1000):
        self.raw = {"supported": True, "running": True, "error": None,
                    "latest": {"timestamp": iso(t_ms), "up": 10, "down": 20},
                    "session": {"upload_bytes": 37, "download_bytes": 83}, "history_seq": 0}
        self.buckets = {"source": "bucketed", "history_seq": 0,
                        "live": [{"seq": 1, "t_ms": t_ms, "up_bps": 10, "down_bps": 20, "n": 1}],
                        "h24": [{"seq": 0, "t_ms": 0, "up_bps": 10, "down_bps": 20, "n": 1}]}
        self.reads = self.copies = 0

    def tick_snapshot(self):
        self.reads += 1
        return deepcopy(self.raw)

    def history_buckets(self):
        self.copies += 1
        return deepcopy(self.buckets)

    def latest(self):
        raise AssertionError("tick must be atomic")

    session = latest
    snapshot = latest


class Runtime:
    def __init__(self, monitor):
        self.current = monitor
        self.resolutions = 0
        self.core_running = True
        self.core_reads = 0

    @property
    def traffic_monitor(self):
        self.resolutions += 1
        return self.current

    def core_fact(self):
        self.core_reads += 1
        return {"running": self.core_running}

    def traffic_snapshot(self):
        raise AssertionError("no raw history copy")

    @property
    def api_client(self):
        raise AssertionError("no controller or /traffic connection")


def setup(monitor=None):
    now = [1000]
    runtime = Runtime(monitor or Monitor())
    state = PublicationState("p2-25")
    state.subscribe(["traffic.tick", "traffic.history"])
    collector = TrafficCollector(state, RuntimeAccess(runtime),
                                 clock=lambda: now[0] / 1000, wall_clock=lambda: now[0])
    return now, runtime, state, collector


def assert_frames_valid(state):
    for event in state.pending():
        assert protocol.validate_frame({**event, "sent_at_ms": 1}) == []


def test_access_resolves_every_call_without_extending_commands():
    runtime = Runtime(Monitor())
    access = RuntimeAccess(runtime)
    first = traffic_monitor(access)
    runtime.current = Monitor(2000)
    assert traffic_monitor(access) is runtime.current
    assert first is not runtime.current and runtime.resolutions == 2
    assert "traffic_monitor" not in CAPABILITIES


def test_single_atomic_read_no_controller_no_raw_history_no_double_totals():
    now, runtime, state, collector = setup()
    collector.poll()
    assert runtime.current.reads == runtime.core_reads == 1 and runtime.current.copies == 1
    assert state.snapshot("traffic.tick")["data"]["session"] == {"up_bytes": 37, "down_bytes": 83}
    rev = state.snapshot("traffic.tick")["rev"]
    for _ in range(5):
        now[0] += 1000
        collector.poll()
    assert runtime.current.reads == runtime.core_reads == 6 and runtime.current.copies == 1
    assert state.snapshot("traffic.tick")["rev"] == rev
    assert state.snapshot("traffic.tick")["data"]["session"] == {"up_bytes": 37, "down_bytes": 83}
    assert not hasattr(collector, "stream")
    assert_frames_valid(state)


def test_rebuilt_monitor_reseeds_once_with_bounded_gap_and_resets_session():
    now, runtime, state, collector = setup()
    collector.poll()
    old = runtime.current
    state.ack(state.pending())  # The first seed is already delivered.
    runtime.current = Monitor(2000)
    runtime.current.raw["session"] = {"upload_bytes": 0, "download_bytes": 0}
    now[0] = 2000
    collector.poll()
    new = runtime.current
    assert old.copies == new.copies == 1
    history = state.snapshot("traffic.history")
    assert history["data"]["gap"] is True
    assert history["data"]["live"][0]["t_ms"] == 2000
    assert state.snapshot("traffic.tick")["data"]["session"] == {"up_bytes": 0, "down_bytes": 0}
    assert len([e for e in state.pending() if e["topic"] == "traffic.history"]) == 1
    rev = history["rev"]
    for _ in range(5):
        collector.request_history()
        now[0] += 1000
        collector.poll()
    assert new.copies == 1 and state.snapshot("traffic.history")["rev"] == rev
    assert_frames_valid(state)
    state.ack(state.pending())
    assert state.subscribe(["traffic.history"]) == []


def test_unsubscribed_rebuild_does_not_copy_history_and_first_seed_has_no_gap():
    now, runtime, state, collector = setup()
    # Use another state so history has not been requested or reserved.
    state = PublicationState("unsubscribed")
    collector.state = state
    collector.poll()
    old = runtime.current
    runtime.current = Monitor(2000)
    now[0] = 2000
    collector.poll()
    assert old.copies == runtime.current.copies == 0
    state.subscribe(["traffic.history"])
    collector.poll()
    assert runtime.current.copies == 1
    assert "gap" not in state.snapshot("traffic.history")["data"]


def test_reload_during_history_copy_does_not_publish_old_seed():
    now, runtime, state, collector = setup()
    old = runtime.current
    new = Monitor(2000)
    original = old.history_buckets
    def rebuilding():
        result = original()
        runtime.current = new
        return result
    old.history_buckets = rebuilding
    collector.poll()
    assert state.snapshot("traffic.history") is None
    now[0] = 2000
    collector.poll()
    assert old.copies == new.copies == 1
    assert state.snapshot("traffic.history")["data"]["live"][0]["t_ms"] == 2000
    assert_frames_valid(state)


def test_many_rebuilds_pending_history_and_gap_remain_bounded():
    now, runtime, state, collector = setup()
    collector.poll()
    for i in range(2, 12):
        now[0] = i * 1000
        runtime.current = Monitor(now[0])
        collector.poll()
        assert runtime.current.copies == 1
        seeds = [e for e in state.pending() if e["topic"] == "traffic.history"]
        assert len(seeds) == 1 and seeds[0]["data"]["gap"] is True
        assert len(seeds[0]["data"]["live"]) <= 300
        assert len(seeds[0]["data"]["h24"]) <= 288
    assert_frames_valid(state)


def test_silence_no_tick_stale_after_three_seconds_then_recovers():
    now, runtime, state, collector = setup()
    collector.poll()
    before = state.snapshot("traffic.tick")
    now[0] = 4000
    collector.poll()
    assert not collector.stale()  # Strictly more than three seconds.
    now[0] = 4001
    assert collector.stale() and collector.metrics()["stale"]
    now[0] = 5000
    collector.poll()
    assert state.snapshot("traffic.tick") == before
    runtime.current.raw["latest"] = {"timestamp": iso(6000), "up": 30, "down": 40}
    now[0] = 6000
    collector.poll()
    assert not collector.stale()
    assert state.snapshot("traffic.tick")["data"]["t_ms"] == 6000


def test_no_first_sample_never_fabricates_tick_and_stop_is_not_stale():
    now, runtime, state, collector = setup()
    runtime.current.raw["latest"] = None
    collector.poll()
    assert state.snapshot("traffic.tick") is None
    now[0] = 5000
    collector.poll()
    assert state.snapshot("traffic.tick") is None and collector.stale()
    runtime.core_running = False
    runtime.current.raw["running"] = False
    now[0] = 6000
    collector.poll()
    stopped = state.snapshot("traffic.tick")
    assert stopped["data"]["running"] is False and stopped["data"]["error_code"] is None
    assert not collector.stale(100000)
    now[0] = 10000
    collector.poll()
    assert state.snapshot("traffic.tick") == stopped


def test_collector_stop_does_not_poll_report_errors_or_mark_stale():
    now, runtime, state, collector = setup()
    collector.poll()
    before = state.snapshot("traffic.tick")
    assert collector.stop()
    now[0] = 100000
    runtime.current.raw["error"] = "private endpoint"
    collector.poll()
    assert state.snapshot("traffic.tick") == before
    assert not collector.stale() and not collector.diagnostics
    assert runtime.current.reads == 1


def test_worker_start_stop_does_not_start_or_stop_runtime_monitor():
    seen = Event()
    monitor = Monitor(int(time.time() * 1000))
    original = monitor.tick_snapshot
    def read():
        seen.set()
        return original()
    monitor.tick_snapshot = read
    state = PublicationState("worker")
    collector = TrafficCollector(state, Runtime(monitor))
    collector.start()
    try:
        assert seen.wait(2)
    finally:
        assert collector.stop()
    assert not collector._thread.is_alive() and not collector.diagnostics
    assert monitor.raw["running"] is True


def test_bucket_projection_omits_empty_rows_and_preserves_actual_means_counts():
    monitor = Monitor()
    monitor.buckets["live"].append({"seq": 2, "t_ms": 2000, "up_bps": None, "down_bps": None, "n": 0})
    monitor.buckets["live"].append({"seq": 3, "t_ms": 3000, "up_bps": 25, "down_bps": 50, "n": 2})
    monitor.buckets["h24"][0].update(up_bps=25, down_bps=50, n=2)
    before = deepcopy(monitor.buckets)
    dto = history_data(monitor.buckets, 3000, gap=True)
    assert [row["t_ms"] for row in dto["live"]] == [1000, 3000]
    assert dto["h24"] == [{"t_ms": 0, "up_bps": 25, "down_bps": 50, "sample_count": 2}]
    assert monitor.buckets == before
    assert protocol.validate_topic_payload("traffic.history", dto) == []


def test_real_monitor_totals_are_projected_once_and_reload_resets(tmp_path):
    def new_monitor():
        return MihomoTrafficMonitor(base_url="http://127.0.0.1:1",
            history_path=tmp_path / "isolated-traffic.json", now_fn=lambda: 10)
    monitor = new_monitor()
    monitor._running = True  # Controlled source, no thread, controller or network.
    monitor.record_sample({"up": 10, "down": 20}, timestamp_epoch=1)
    monitor.record_sample({"up": 30, "down": 40}, timestamp_epoch=2)
    now, runtime, state, collector = setup(monitor)
    now[0] = 2000
    collector.poll()
    assert state.snapshot("traffic.tick")["data"]["session"] == {"up_bytes": 30, "down_bytes": 40}
    now[0] = 3000
    collector.poll()
    assert state.snapshot("traffic.tick")["data"]["session"] == {"up_bytes": 30, "down_bytes": 40}
    runtime.current = new_monitor()
    runtime.core_running = False
    collector.poll()  # Rate limit defers resolution until the next 1 Hz slot.
    now[0] = 4000
    collector.poll()
    dto = state.snapshot("traffic.tick")["data"]
    assert dto["session"] == {"up_bytes": 0, "down_bytes": 0} and not dto["running"]
    assert state.snapshot("traffic.history")["data"]["gap"] is True
    assert_frames_valid(state)


def test_reseed_is_explicit_and_only_allowed_for_marked_history():
    state = PublicationState("guard")
    state.subscribe(["traffic.history"])
    first = history_data(Monitor().buckets, 1000)
    state.record_state("traffic.history", first)
    old = state.snapshot("traffic.history")
    state.record_state("traffic.history", history_data(Monitor(2000).buckets, 2000))
    assert state.snapshot("traffic.history") == old
    with pytest.raises(ValueError, match="invalid history reseed"):
        state.record_state("traffic.history", first, history_reseed=True)
    with pytest.raises(ValueError, match="invalid history reseed"):
        state.record_state("traffic.tick", {"gap": True}, history_reseed=True)


def test_publisher_delivers_reseed_once_as_snapshot_with_gap_marker():
    now, runtime, state, collector = setup()
    class Transport:
        def __init__(self):
            self.frames = []
        def send(self, frame):
            assert protocol.validate_frame(frame) == []
            self.frames.append(deepcopy(frame))
            return "complete"
        def poll_write(self):
            return "complete"
    state = PublicationState("p2-25")
    collector.state = state
    transport = Transport()
    publisher = Publisher(state, transport)
    publisher.subscription_frames({"t": "subscribe", "epoch": "p2-25",
        "produced_at_ms": 1, "sent_at_ms": 1, "id": 1,
        "topics": ["traffic.tick", "traffic.history"]})
    collector.poll()
    for _ in range(4):
        publisher.tick()  # ack and each first-subscription snapshot drain in order.
    runtime.current = Monitor(2000)
    now[0] = 2000
    collector.poll()
    publisher.tick()
    for _ in range(3):
        publisher.tick()
    histories = [item for frame in transport.frames
                 for item in (frame.get("items", []) if frame["t"] == "batch" else [frame])
                 if item.get("topic") == "traffic.history"]
    assert len(histories) == 2
    assert histories[1]["kind"] == "snapshot" and histories[1]["data"]["gap"] is True
    assert histories[1]["rev"] > histories[0]["rev"]


@pytest.mark.parametrize("fails", [False, True])
def test_stop_during_pending_atomic_read_suppresses_late_ticks_and_errors(fails):
    entered, release = Event(), Event()
    monitor = Monitor()
    original = monitor.tick_snapshot
    def blocked_read():
        entered.set()
        assert release.wait(2)
        if fails:
            raise OSError("read ended while stopping")
        return original()
    monitor.tick_snapshot = blocked_read
    state = PublicationState("stopping-read")
    state.subscribe(["traffic.tick"])
    collector = TrafficCollector(state, Runtime(monitor))
    collector.start()
    try:
        assert entered.wait(2)
        assert not collector.stop(timeout=0)
    finally:
        release.set()
        assert collector.stop()
    assert state.snapshot("traffic.tick") is None
    assert not collector.diagnostics and not collector.stale()
    assert collector.metrics()["source_costs"]["traffic.tick"]["failures"] == 0


@pytest.mark.parametrize("terminal", ["error", "eof"])
@pytest.mark.parametrize("service_stopped", [False, True])
@pytest.mark.parametrize("has_sample", [False, True])
def test_real_monitor_stream_exit_uses_core_lifecycle(
        tmp_path, terminal, service_stopped, has_sample):
    waiting, release = Event(), Event()

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def __iter__(self):
            if has_sample:
                yield b'{"up":10,"down":20}\n'
            waiting.set()
            assert release.wait(2)
            if terminal == "error":
                raise OSError("synthetic stream disconnected")

        def close(self):
            pass

    response = Response()
    monitor = MihomoTrafficMonitor(base_url="http://127.0.0.1:1",
        history_path=tmp_path / "traffic.json", now_fn=lambda: 1,
        opener=lambda request, timeout: response)
    now, runtime, state, collector = setup(monitor)
    monitor.start()
    try:
        assert waiting.wait(2)
        collector.poll()
        before = state.snapshot("traffic.tick")
        assert (before is not None) == has_sample
        if service_stopped:
            runtime.core_running = False
            monitor.stop(timeout=0)
        release.set()
        monitor._thread.join(2)
        assert not monitor._thread.is_alive()
        raw = monitor.tick_snapshot()
        assert raw["running"] is False
        assert bool(raw["error"]) == (terminal == "error" and not service_stopped)
        now[0] = 4000
        collector.poll()
        assert not collector.stale()  # Exactly three seconds, including no sample.
        now[0] = 4001
        assert collector.stale() is (not service_stopped)
        now[0] = 5000
        collector.poll()
        after = state.snapshot("traffic.tick")
        if service_stopped:
            assert after["data"] == {"t_ms": 4000, "up_bps": 0, "down_bps": 0,
                "session": {"up_bytes": 0, "down_bytes": 0},
                "running": False, "error_code": None}
            assert not collector.stale(100000)
        else:
            assert after == before  # No tick, timestamp or revision invented on exit.
            assert collector.stale() and collector.metrics()["stale"]
            if has_sample:
                assert after["data"]["t_ms"] == 1000 and after["data"]["running"]
        assert not collector.diagnostics
        assert_frames_valid(state)
    finally:
        release.set()
        monitor.stop()


def test_core_stop_is_authoritative_even_while_sampler_is_alive():
    now, runtime, state, collector = setup()
    collector.poll()
    runtime.core_running = False
    now[0] = 2000
    collector.poll()
    assert runtime.current.raw["running"] is True
    assert state.snapshot("traffic.tick")["data"]["running"] is False
    assert not collector.stale(100000)
