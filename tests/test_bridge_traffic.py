from copy import deepcopy
from datetime import datetime, timezone
from io import BytesIO
import json
import os
from threading import current_thread
import time
from types import SimpleNamespace

import pytest

import bridge_protocol as protocol
from bridge_logs import LogCollector
from bridge_publish import Publisher
from bridge_runtime_access import RuntimeAccess
from bridge_state import PublicationState
from bridge_traffic import TrafficCollector, collection_metrics, history_data


class Monitor:
    """Owned monitor substitute; integration belongs here, never in the bridge."""
    def __init__(self, runtime):
        self.runtime = runtime
        self.running = True
        self.latest = None
        self.error = None
        self.totals = {"upload_bytes": 0, "download_bytes": 0}
        self.previous = None
        self.reads = 0

    def emit(self, up, down, t_ms):
        if self.previous is not None:
            elapsed = min(5, max(0, (t_ms - self.previous) / 1000))
            self.totals["upload_bytes"] += int(up * elapsed)
            self.totals["download_bytes"] += int(down * elapsed)
        self.previous = t_ms
        self.latest = {"timestamp": datetime.fromtimestamp(t_ms / 1000, timezone.utc).isoformat(),
                       "up": up, "down": down}

    def tick_snapshot(self):
        self.reads += 1
        return {"supported": True, "running": self.running, "latest": deepcopy(self.latest),
                "session": dict(self.totals), "error": self.error, "history_seq": 1}

    def history_buckets(self):
        self.runtime.copies += 1
        self.runtime.copy_threads.append(current_thread().name)
        return deepcopy(self.runtime.history)


class Runtime:
    def __init__(self):
        self.copies = 0
        self.core_running = True
        self.copy_threads = []
        self.history = {"live": [], "h24": [], "source": "bucketed"}
        self.traffic_monitor = Monitor(self)

    def core_fact(self):
        return {"running": self.core_running}

    def traffic_snapshot(self):
        raise AssertionError("raw history must not be copied")


def test_tick_single_sample_and_1hz_no_history_copy():
    now = [10.0]
    state, runtime = PublicationState("synthetic"), Runtime()
    state.subscribe(["traffic.tick"])
    traffic = TrafficCollector(state, runtime, clock=lambda: now[0])
    runtime.traffic_monitor.emit(10, 20, 1000)
    traffic.poll()
    first = state.snapshot("traffic.tick")["data"]
    assert first == {"t_ms": 1000, "up_bps": 10, "down_bps": 20,
                     "session": {"up_bytes": 0, "down_bytes": 0}, "running": True, "error_code": None}
    now[0] = 10.5
    runtime.traffic_monitor.emit(30, 40, 1500)
    traffic.poll()
    assert state.snapshot("traffic.tick")["data"] == first
    assert runtime.traffic_monitor.reads == 1
    now[0] = 11
    traffic.poll()
    second = state.snapshot("traffic.tick")["data"]
    assert second["t_ms"] == 1500
    assert second["session"] == {"up_bytes": 15, "down_bytes": 20}
    assert protocol.validate_topic_payload("traffic.tick", second) == []
    assert runtime.copies == 0
    revision = state.snapshot("traffic.tick")["rev"]
    now[0] = 12
    traffic.poll()
    assert state.snapshot("traffic.tick")["rev"] == revision
    assert runtime.traffic_monitor.reads == 3


def test_core_stop_transition_and_same_monitor_restart_retains_session():
    now = [1.0]
    state, runtime = PublicationState("synthetic"), Runtime()
    state.subscribe(["traffic.tick"])
    monitor = runtime.traffic_monitor
    traffic = TrafficCollector(state, runtime, clock=lambda: now[0])
    monitor.emit(2, 4, 1000)
    traffic.poll()
    monitor.emit(2, 4, 1500)
    runtime.core_running = False
    monitor.running = False
    monitor.previous = None  # The monitor resets elapsed integration on stop.
    now[0] = 2
    traffic.poll()
    assert not state.snapshot("traffic.tick")["data"]["running"]
    rev = state.snapshot("traffic.tick")["rev"]
    now[0] = 3
    traffic.poll()
    assert state.snapshot("traffic.tick")["rev"] == rev
    runtime.core_running = True
    monitor.running = True
    now[0] = 4
    monitor.emit(5, 6, 4000)
    traffic.poll()
    assert state.snapshot("traffic.tick")["data"]["session"] == {"up_bytes": 1, "down_bytes": 2}


def test_compact_monitor_copied_without_mutating_and_scan():
    state, runtime = PublicationState("synthetic"), Runtime()
    state.subscribe(["traffic.tick"])
    monitor = runtime.traffic_monitor
    monitor.emit(3, 4, 1)
    monitor.totals = {"upload_bytes": 7, "download_bytes": 8}
    raw = monitor.tick_snapshot()
    monitor.tick_snapshot = lambda: raw
    traffic = TrafficCollector(state, runtime)
    traffic.poll()
    raw["session"]["upload_bytes"] = 100
    raw["latest"]["up"] = 200
    event = state.snapshot("traffic.tick")
    event["sent_at_ms"] = 1
    assert event["data"]["session"]["up_bytes"] == 7
    assert event["data"]["up_bps"] == 3
    assert protocol.validate_frame(event) == []
    assert runtime.copies == 0


def test_runtime_monitor_is_the_only_monitoring_source(monkeypatch):
    import bridge_logs
    def forbidden(*args, **kwargs):
        raise AssertionError("bridge must never open /traffic")
    monkeypatch.setattr(bridge_logs, "BoundedMihomoStream", forbidden)
    state, runtime = PublicationState("synthetic"), Runtime()
    state.subscribe(["traffic.tick"])
    runtime.traffic_monitor.emit(12, 34, 1000)
    traffic = TrafficCollector(state, runtime)
    traffic.poll()
    dto = state.snapshot("traffic.tick")["data"]
    assert (dto["up_bps"], dto["down_bps"], dto["running"]) == (12, 34, True)
    assert runtime.copies == 0 and not hasattr(traffic, "stream")
    assert protocol.validate_topic_payload("traffic.tick", dto) == []


def test_compact_monitor_stop_retains_same_sample_totals():
    state, runtime = PublicationState("synthetic"), Runtime()
    state.subscribe(["traffic.tick"])
    runtime.core_running = False
    runtime.traffic_monitor.running = False
    runtime.traffic_monitor.error = "private endpoint failed"
    runtime.traffic_monitor.totals = {"upload_bytes": 7, "download_bytes": 8}
    traffic = TrafficCollector(state, runtime, wall_clock=lambda: 1)
    traffic.poll()
    assert state.snapshot("traffic.tick")["data"] == {
        "t_ms": 1, "up_bps": 0, "down_bps": 0, "session": {"up_bytes": 7, "down_bytes": 8},
        "running": False, "error_code": None}


def test_history_bucket_means_limits_and_order():
    end = 1_800_000_000_000
    live = [{"t_ms": end - i * 1000, "up_bps": i, "down_bps": i * 2, "n": 1}
            for i in reversed(range(300))]
    h24 = [{"t_ms": end - i * 300000, "up_bps": 150.5, "down_bps": 301, "n": 300}
           for i in reversed(range(288))]
    dto = history_data({"live": live, "h24": h24}, end)
    assert len(dto["h24"]) == 288 and len(dto["live"]) == 300
    assert dto["source"] == "bucketed"
    assert dto["h24"][0]["t_ms"] < dto["h24"][-1]["t_ms"]
    assert dto["live"][0]["t_ms"] < dto["live"][-1]["t_ms"]
    assert dto["h24"][-2]["sample_count"] == 300
    assert dto["h24"][-2]["up_bps"] == sum(range(1, 301)) / 300
    assert protocol.validate_topic_payload("traffic.history", dto) == []


def test_empty_buckets_and_invalid_rates_skipped():
    instant = 1_800_000_000_000
    dto = history_data({"h24": [{"t_ms": instant, "up_bps": 2, "down_bps": 4, "n": 1},
                              {"t_ms": instant + 1, "up_bps": float("nan"), "down_bps": 4, "n": 1},
                              {"t_ms": instant + 2, "up_bps": None, "down_bps": None, "n": 0}]}, instant)
    assert dto["h24"] == [{"t_ms": instant, "up_bps": 2, "down_bps": 4, "sample_count": 1}]


def test_history_copy_once_worker_not_io_tick_each_epoch():
    class Transport:
        frames = []
        def send(self, frame):
            assert protocol.validate_frame(frame) == []
            self.frames.append(frame)
            return "complete"
        def poll_write(self):
            return "complete"
    runtime = Runtime()
    for epoch in ("one", "two"):
        state = PublicationState(epoch)
        traffic = TrafficCollector(state, RuntimeAccess(runtime))
        publisher = Publisher(state, Transport())
        req = {"t": "subscribe", "epoch": epoch, "produced_at_ms": 1, "sent_at_ms": 1,
               "id": 1, "topics": ["traffic.history"]}
        before = runtime.copies
        publisher.subscription_frames(req)
        publisher.tick()
        assert runtime.copies == before
        traffic.start()
        deadline = time.monotonic() + 2
        try:
            while state.snapshot("traffic.history") is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert state.snapshot("traffic.history") is not None
        finally:
            traffic.stop()
        assert runtime.copies == before + 1
        assert runtime.copy_threads[-1] == "bridge-traffic-sampler"
        for i in range(4):
            traffic.request_history()
            traffic.poll()
            publisher.tick()
        publisher.subscription_frames({**req, "id": 2})
        for _ in range(4):
            publisher.tick()
        assert runtime.copies == before + 1
        history_frames = [f for f in publisher.transport.frames if f.get("topic") == "traffic.history" and f["epoch"] == epoch]
        assert len(history_frames) == 1
        snapreq = {"t": "snapshot_req", "epoch": epoch, "produced_at_ms": 1, "sent_at_ms": 1,
                   "id": 3, "topic": "traffic.history"}
        assert publisher.snapshot_response(snapreq)["snapshot"]["data"]["source"] == "bucketed"
        assert runtime.copies == before + 1


def test_history_failure_does_not_repeat_copy():
    class Broken(Monitor):
        def history_buckets(self):
            self.runtime.copies += 1
            raise OSError("synthetic")
    state, runtime = PublicationState("synthetic"), Runtime()
    runtime.traffic_monitor = Broken(runtime)
    traffic = TrafficCollector(state, runtime)
    traffic.request_history()
    for _ in range(5):
        traffic.poll()
    assert runtime.copies == 1
    assert traffic.metrics()["source_costs"]["traffic.history"]["failures"] == 1
    assert traffic.diagnostics


def test_metrics_extend_p1_12():
    publisher = SimpleNamespace(metrics=lambda: {"publisher_thread_cpu_ns": 5, "queue_depth": 0})
    sampler = SimpleNamespace(worker_thread_cpu_ns=7, source_costs={})
    state = PublicationState("synthetic")
    metrics = collection_metrics(publisher, sampler, LogCollector(state), TrafficCollector(state, Runtime()))
    assert metrics["publisher_thread_cpu_ns"] == 5 and metrics["state_worker_thread_cpu_ns"] == 7
    assert "producer_cpu_ns" in metrics["logs"] and "source_costs" in metrics["traffic"]


@pytest.mark.skipif(os.environ.get("TESSERACT_BRIDGE_CPU_TEST") != "1",
                    reason="CPU measurement requires an explicitly scheduled window")
@pytest.mark.parametrize("rate", [50, 500])
@pytest.mark.parametrize("registry_size", [0, 1000])
def test_slow_logs_per_second_and_1hz_traffic_thread_cpu(rate, registry_size, monkeypatch):
    from threading import Event
    import statistics
    import bridge_logs

    class Transport:
        records = 0
        gaps = 0
        def send(self, frame):
            protocol.encode_frame(frame)  # Real wire validation/serialization cost.
            for item in frame.get("items", []):
                if item["topic"] == "logs.batch" and item["kind"] == "edge":
                    self.records += len(item["data"]["records"])
                    self.gaps += item["data"]["dropped"]
            return "complete"
        def poll_write(self):
            return "complete"

    # Public Mihomo /logs wire shape, paced independently of collector/publisher.
    # No live controller, network, host TUN or process-wide CPU accounting.
    lines = (
        "[TCP] 192.0.2.1:53123 --> example.invalid:443 match Match using FINAL-EXIT[Vanguard]",
        "[UDP] 192.0.2.2:53000 --> dns.example.invalid:53 match Domain using Sentinel",
        "[DNS] resolve example.invalid from udp://192.0.2.53:53",
        "Start initial configuration in progress",
        "[TCP] 192.0.2.3:54000 --> static.example.invalid:80 match DomainSuffix using Vanguard",
        "[DNS] cache hit example.invalid --> [192.0.2.10]",
    )
    values = {f"OpaqueSynthetic{i:04d}Value": "token" for i in range(registry_size)}
    if registry_size:
        # Mixed ordinary/credential/JSON workload, including both ends of the
        # registry. The registration build is off-path; matching is measured.
        lines += ("connection OpaqueSynthetic0999Value",
                  'Authentication Basic c3ludGhldGljOnBhc3N3b3Jk',
                  '{"nested":["OpaqueSynthetic0000Value", {"value":12345678}]}',
                  'Gotify app token SyntheticGotifyValue')
    packets = [(json.dumps({"type": "info", "payload": line}) + "\n").encode()
               for line in lines]
    stopped = Event()
    duration = float(os.environ.get("TESSERACT_BRIDGE_CPU_SECONDS", "30"))
    class Response:
        count = 0
        next_at = None
        def readline(self, limit):
            if self.next_at is None:
                self.next_at = time.perf_counter()
            if stopped.wait(max(0, self.next_at - time.perf_counter())):
                return b""
            packet = packets[self.count % len(packets)]
            self.count += 1
            self.next_at += 1 / rate
            return packet
        def close(self):
            stopped.set()
    response = Response()
    client = SimpleNamespace(base_url="http://127.0.0.1:1", secret="", timeout=1,
                             opener=lambda req, timeout: response)
    latencies = []
    redact = bridge_logs.redact_record
    def measured_redact(record, **kwargs):
        started = time.perf_counter_ns()
        try:
            return redact(record, **kwargs)
        finally:
            latencies.append(time.perf_counter_ns() - started)
    monkeypatch.setattr(bridge_logs, "redact_record", measured_redact)
    state = PublicationState("synthetic")
    state.subscribe(["logs.batch", "traffic.tick"])
    logs = LogCollector(state, client_factory=lambda: client, known_secret_values=values)
    runtime = Runtime()
    traffic = TrafficCollector(state, runtime)
    publisher = Publisher(state, Transport())
    wall = time.perf_counter()
    publisher.start()
    logs.start()
    traffic.start()
    samples = 0
    sample_cpu_ns = 0
    try:
        while time.perf_counter() - wall < duration:
            sample_started = time.thread_time_ns()
            runtime.traffic_monitor.emit(100, 200, int(time.time() * 1000))
            sample_cpu_ns += time.thread_time_ns() - sample_started
            samples += 1
            time.sleep(min(1, max(0, duration - (time.perf_counter() - wall))))
    finally:
        stopped.set()
        logs.stream.stop()
        # Drain on the actual measured threads, including the final loss batch.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with logs._lock:
                drained = not logs._ring and logs._reported_total == logs.dropped_total
            if drained and state.depth == 0:
                break
            time.sleep(.01)
        logs.stop()
        traffic.stop()
        publisher.stop()
    elapsed = time.perf_counter() - wall
    stream = logs.stream.worker_thread_cpu_ns / (elapsed * 1e9)
    collector = logs.worker_thread_cpu_ns / (elapsed * 1e9)
    publish = publisher.publisher_thread_cpu_ns / (elapsed * 1e9)
    traffic_cpu = traffic.worker_thread_cpu_ns / (elapsed * 1e9)
    # Main-thread traffic callback is disjoint from worker lifetimes.
    traffic_cpu += sample_cpu_ns / (elapsed * 1e9)
    # put_nowait CPU is nested inside the stream lifetime and is NOT added twice.
    log_cores = stream + collector + publish
    median = statistics.median(latencies) / 1000
    p95 = sorted(latencies)[int((len(latencies) - 1) * .95)] / 1000
    drop_rate = logs.metrics()["dropped_total"] / response.count
    print(f"rate={rate} registry_values={registry_size} elapsed_s={elapsed:.6f} input={response.count} "
          f"sent={publisher.transport.records} dropped={logs.metrics()['dropped_total']} "
          f"redact_samples={len(latencies)} redact_median_us={median:.3f} redact_p95_us={p95:.3f} "
          f"collector_cpu_ns={logs.worker_thread_cpu_ns} stream_cpu_ns={logs.stream.worker_thread_cpu_ns} "
          f"publisher_cpu_ns={publisher.publisher_thread_cpu_ns} "
          f"collector_cores={collector:.8f} stream_cores={stream:.8f} "
          f"publisher_cores={publish:.8f} log_path_cores={log_cores:.8f} traffic_cores={traffic_cpu:.8f} "
          f"sample_cpu_ns={sample_cpu_ns} dropped_pct={drop_rate * 100:.4f} "
          f"charged_work_cpu_ns={logs.work_cpu_ns} budget_ns={logs.work_budget_ns} "
          f"publication_cpu_ns={state.log_publication_cpu_ns}")
    assert response.count >= duration * rate * .95 and samples >= duration * .9
    assert publisher.transport.records + logs.metrics()["dropped_total"] == response.count
    assert publisher.transport.gaps == logs.metrics()["dropped_total"]
    assert drained and state.depth == 0
    assert latencies and median <= 50
    # Default 20ms/s is the collector's actual capture/redact/batch token
    # budget. PublicationState also debits actual publication work against
    # a separate 40ms/s safety ceiling; total lifetimes remain measured above.
    assert log_cores <= (0.02 if rate == 50 else 0.1)
    assert logs.work_cpu_ns / (elapsed * 1e9) <= .025
    if rate == 50:
        assert logs.metrics()["dropped_total"] == 0
    assert not logs.diagnostics and not traffic.diagnostics and not publisher.diagnostics
    assert not logs._thread.is_alive() and not logs.stream.thread.is_alive()
    assert not traffic._thread.is_alive() and not publisher._thread.is_alive()
