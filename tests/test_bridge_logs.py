from io import BytesIO
from itertools import count
import json
import logging
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
import time

import pytest

import bridge_protocol as protocol
from bridge_logs import BoundedMihomoStream, LogCollector
from bridge_publish import Publisher
from bridge_state import PublicationState

REDACTION_REGRESSIONS = json.loads((Path(__file__).parent /
                                   "fixtures/redaction_corpus/astra_regressions.json").read_bytes())
REDACTION_CORPUS = json.loads((Path(__file__).parent /
                              "fixtures/redaction_corpus/synthetic.json").read_bytes()) + REDACTION_REGRESSIONS + json.loads((Path(__file__).parent /
    "fixtures/redaction_corpus/round3_credentials.json").read_bytes())


def new_collector(**kwargs):
    state = PublicationState("synthetic", edge_capacity=kwargs.pop("edge_capacity", 128))
    state.subscribe(["logs.batch"])
    return state, LogCollector(state, **kwargs)


@pytest.mark.parametrize("emit_count", [1, 3])
def test_stop_closes_emit_intake_and_waits_for_real_blocked_formatter(emit_count):
    logger = logging.Logger("blocked-log-formatter", level=logging.INFO)
    _, logs = new_collector(logger=logger)
    entered = [Event() for _ in range(emit_count)]
    releases = [Event() for _ in range(emit_count)]

    class Message:
        def __init__(self, index):
            self.index = index

        def __str__(self):
            entered[self.index].set()
            assert releases[self.index].wait(5)
            return "in-flight log"

    class LateMessage:
        def __str__(self):
            pytest.fail("closed handler formatted a new callback")

    logs.start()
    handler = logs._handler
    emitters = [Thread(target=lambda: logger.info(Message(0)))]
    # Direct emit calls also occur outside Handler.handle's serialization.
    # Exercise simultaneous callbacks through the same real handler.
    emitters.extend(Thread(target=lambda i=i: handler.emit(logging.LogRecord(
        "parallel", logging.INFO, "", 0, Message(i), (), None))) for i in range(1, emit_count))
    for emitter in emitters:
        emitter.start()
    try:
        assert all(event.wait(1) for event in entered)
        began = time.monotonic()
        assert not logs.stop(.03)
        assert time.monotonic() - began < .3
        assert all(emitter.is_alive() for emitter in emitters) and logs._handler is handler
        assert not logger.handlers and logs.metrics()["queue_depth"] == 0
        # A caller retaining the detached handler must be rejected before
        # formatting too; logging.Handler.close alone does not enforce this.
        late = logging.LogRecord("late", logging.INFO, "", 0, LateMessage(), (), None)
        handler.emit(late)
        assert handler._active_emits == emit_count
        assert not logs.stop(.01)
        if emit_count > 1:
            releases[0].set()
            emitters[0].join(1)
            assert not emitters[0].is_alive()
            assert not logs.stop(.01)
            assert handler._active_emits == emit_count - 1
        for release in releases:
            release.set()
        assert logs.stop(1)
        for emitter in emitters:
            emitter.join(1)
        assert all(not emitter.is_alive() for emitter in emitters) and logs._handler is None
        assert logs.metrics()["queue_depth"] == emit_count
        assert [row["raw"] for row in logs.flush()["records"]] == ["in-flight log"] * emit_count
        handler.emit(late)
        assert logs.metrics()["queue_depth"] == 0
    finally:
        for release in releases:
            release.set()
        for emitter in emitters:
            emitter.join(1)
        logs.stop(1)


def test_ring_overflow_counts_and_sequence():
    state, logs = new_collector(capacity=2)
    for i in range(5):
        assert logs.put_nowait({"message": f"line {i}", "source": "TESSERACT"})
    dto = logs.flush()
    assert [r["seq"] for r in dto["records"]] == [4, 5]
    assert (dto["from_seq"], dto["dropped"], dto["dropped_total"], dto["gap"]) == (1, 3, 3, True)
    state.ack(state.pending())
    logs.put_nowait({"message": "next"})
    second = logs.flush()
    assert (second["from_seq"], second["records"][0]["seq"], second["dropped"], second["dropped_total"]) == (6, 6, 0, 3)
    assert not second["gap"]
    assert protocol.validate_logs_batch(dto) == []


def test_only_broken_record_dropped_and_no_raw_queued():
    _, logs = new_collector()
    logs.put_nowait({"message": "password=synthetic"})
    assert not logs.put_nowait({"message": object()})
    logs.put_nowait({"message": "clean \ud800"})
    with logs._lock:
        assert logs._ring[0][0]["raw"] == "[REDACTED]"
    dto = logs.flush()
    assert [r["seq"] for r in dto["records"]] == [1, 2]
    assert dto["records"][1]["raw"] == "clean \ufffd"
    assert (dto["dropped"], dto["dropped_total"], dto["gap"]) == (1, 1, True)
    assert logs.metrics()["serialization_failures"] == 1


@pytest.mark.parametrize("sample", REDACTION_CORPUS, ids=lambda s: s["kind"])
def test_redaction_regressions_are_safe_before_real_collector_enqueue(sample):
    state, logs = new_collector(known_secret_values=sample.get("known_secret_values", {}))
    assert logs.put_nowait({"message": sample["input"], "raw_message": sample["input"],
                           "detail": sample["input"]})
    with logs._lock:
        queued = dict(logs._ring[0][0])
    for field in ("title", "detail", "raw"):
        assert queued[field] == sample["expected"]
        for identifier in sample.get("absent", []):
            assert identifier not in queued[field]
    dto = logs.flush()
    assert protocol.validate_logs_batch(dto) == []
    assert dto["dropped"] == 0 and logs.metrics()["diagnostics"] == []
    assert state.pending()[0]["data"]["records"] == [queued]
    class Transport:
        frames = []
        def poll_write(self):
            return "complete"
        def send(self, frame):
            self.frames.append(protocol.encode_frame(frame))
            return "complete"
    publisher = Publisher(state, Transport())
    assert publisher.tick()
    assert publisher.transport.frames
    wire = b"".join(publisher.transport.frames).decode("utf-8")
    for identifier in sample.get("absent", []):
        assert identifier not in wire
    encoded = json.loads(wire)
    assert encoded["items"][0]["data"]["records"][0]["raw"] == sample["expected"]


def test_oversize_record_is_counted_instead_of_truncated():
    _, logs = new_collector()
    assert not logs.put_nowait({"message": "x" * 20000})
    dto = logs.flush()
    assert dto["records"] == [] and dto["dropped"] == 1 and dto["gap"]


def test_small_batch_reserves_envelope_and_cannot_stall_head():
    _, logs = new_collector(batch_bytes=4096)
    assert not logs.put_nowait({"message": "x" * 3800})
    assert logs.put_nowait({"message": "fits"})
    dto = logs.flush()
    assert dto["records"][0]["raw"] == "fits" and dto["dropped"] == 1


@pytest.mark.parametrize("capacity", [1.5, True, 0])
def test_ring_requires_integer_capacity(capacity):
    with pytest.raises(ValueError):
        new_collector(capacity=capacity)


def test_multiple_producers_have_strict_sequence():
    _, logs = new_collector(capacity=400)
    threads = [Thread(target=lambda: [logs.put_nowait({"message": "safe"}) for _ in range(100)])
               for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()
    dto = logs.flush()
    assert [row["seq"] for row in dto["records"]] == list(range(1, 401))
    assert dto["dropped"] == 0


def test_all_bad_records_publish_count_only_batch():
    _, logs = new_collector()
    logs.put_nowait({"message": object()})
    assert logs.flush() == {"from_seq": 1, "records": [], "dropped": 1, "dropped_total": 1, "gap": True}
    assert logs.flush() is None


def test_protocol_scan_hit_is_diagnostic_not_a_redaction_fallback(monkeypatch):
    monkeypatch.setattr("bridge_logs.redact_record", lambda record, **kwargs: dict(record))
    state, logs = new_collector()
    logs.put_nowait({"message": "token=synthetic"})
    protocol.privacy_diagnostics.clear()
    logs.flush()
    assert state.pending()[0]["data"]["records"][0]["raw"] == "token=synthetic"
    assert logs.metrics()["dropped_total"] == 0
    assert not logs.diagnostics
    assert protocol.privacy_diagnostics
    assert "synthetic" not in str(protocol.privacy_diagnostics)
    assert "synthetic" not in str(logs.metrics())


def test_state_overflow_gap_and_record_loss_are_separate():
    state, logs = new_collector(edge_capacity=1)
    for text in ("first", "second"):
        logs.put_nowait({"message": text})
        logs.flush()
    pending = state.pending()
    assert len(pending) == 1 and pending[0]["kind"] == "gap"
    assert pending[0]["data"] == {"from_seq": 1, "to_seq": 2, "dropped": 2}
    assert pending[0]["seq"] == 3
    assert state.log_records_dropped == 2
    state.ack(pending)
    dto = logs.flush()
    assert (dto["dropped"], dto["dropped_total"], dto["gap"]) == (2, 2, True)
    assert state.pending()[0]["seq"] == 4
    assert logs.flush() is None


def test_ring_and_publisher_losses_are_counted_once():
    state, logs = new_collector(capacity=1, edge_capacity=1)
    logs.put_nowait({"message": "ring loser"})
    logs.put_nowait({"message": "first batch"})
    logs.flush()
    logs.put_nowait({"message": "second batch"})
    logs.flush()
    assert state.log_records_dropped == 2
    state.ack(state.pending())
    dto = logs.flush()
    assert (dto["dropped"], dto["dropped_total"]) == (2, 3)
    assert logs.flush() is None


@pytest.mark.parametrize("outcome", ["complete", "pending", "busy", "error"])
def test_loss_settles_after_inflight_send_and_gap_ack(outcome):
    state, logs = new_collector(edge_capacity=1)
    sending, release = Event(), Event()
    class Transport:
        frames = []
        first = True
        def poll_write(self):
            return "complete"
        def send(self, frame):
            assert protocol.validate_frame(frame) == []
            if self.first:
                self.first = False
                sending.set()
                assert release.wait(2)
                if outcome == "error":
                    raise OSError("synthetic")
                if outcome == "busy":
                    return "busy"
                self.frames.append(frame)
                return outcome
            self.frames.append(frame)
            return "complete"
    logs.put_nowait({"message": "A"})
    logs.flush()
    transport = Transport()
    publisher = Publisher(state, transport)
    result = []
    sender = Thread(target=lambda: result.append(publisher.tick()))
    sender.start()
    try:
        assert sending.wait(1)
        logs.put_nowait({"message": "B"})
        logs.flush()
        assert state.log_records_dropped == 2  # A is still rescuable.
        assert logs.flush() is None  # No provisional loss may reach the wire.
        assert logs.metrics()["dropped_total"] == 0
    finally:
        release.set()
        sender.join(2)
    assert not sender.is_alive()
    rescued = outcome in {"complete", "pending"}
    assert result == [rescued]
    assert state.log_records_dropped == (1 if rescued else 2)
    assert publisher.tick()  # Ordered submission of the corrected gap settles loss.
    dto = logs.flush()
    expected = 1 if rescued else 2
    assert (dto["records"], dto["dropped"], dto["dropped_total"], dto["gap"]) == ([], expected, expected, True)
    assert logs.metrics()["dropped_total"] == expected
    assert logs.flush() is None
    gaps = [item for frame in transport.frames for item in frame["items"] if item["kind"] == "gap"]
    assert gaps[0]["data"] == {"from_seq": 2 if rescued else 1, "to_seq": 2, "dropped": expected}


def test_batch_limits_and_order():
    state, logs = new_collector(capacity=2001)
    for i in range(1001):
        logs.put_nowait({"message": str(i)})
    a = logs.flush()
    state.ack(state.pending())
    b = logs.flush()
    # Byte budget may divide the 1,000-record budget earlier.
    assert 0 < len(a["records"]) <= 1000
    assert b["records"][0]["seq"] == a["records"][-1]["seq"] + 1
    assert b["from_seq"] == b["records"][0]["seq"]


def test_mihomo_and_logging_public_capture():
    logger = logging.Logger("synthetic-bridge", level=logging.DEBUG)
    state, logs = new_collector(logger=logger)
    logs.start()
    try:
        logger.warning("password=synthetic", extra={"event_key": "errors.test.failure", "category": "health"})
        logs.ingest_mihomo({"type": "debug", "payload": "[TCP] 192.0.2.1 --> example.invalid:443"})
        dto = logs.flush()
        a, b = dto["records"]
        assert (a["source"], a["level"], a["category"], a["key"]) == ("TESSERACT", "warn", "health", "errors.test.failure")
        # T-P4-C1 #2: a classified line keeps no raw text; its live title and
        # detail are the rendered event report, the same text logs.tail gives.
        assert a["raw"] == "" and "synthetic" not in a["title"] + a["detail"]
        history = logs.tail({"n": 10})["records"]
        assert (a["title"], a["detail"]) == (history[0]["title"], history[0]["detail"]) != ("", "")
        assert b["source"] == "MIHOMO" and b["level"] == "debug"
        assert b["raw"] == "[TCP] 192.0.2.1 --> example.invalid:443"
        assert protocol.validate_logs_batch(dto) == []
    finally:
        logs.stop()
    assert logger.handlers == []
    assert not logs._thread.is_alive()


@pytest.mark.parametrize("source,category,expected", [("ALERT", "", "alert"),
                                                    ("TESSERACT", "route", "routing"),
                                                    ("MIHOMO", "connections", "connection"),
                                                    ("TESSERACT", "validation", "health"),
                                                    ("UNKNOWN", "general", "other")])
def test_category_mapping(source, category, expected):
    _, logs = new_collector()
    logs.put_nowait({"source": source, "category": category, "message": "synthetic"})
    assert logs.flush()["records"][0]["category"] == expected


def test_classified_connection_failure_transmits_only_key():
    _, logs = new_collector()
    logs.put_nowait({"source": "MIHOMO", "level": "INFO", "event_key": "mihomo.connection_failed",
                      "message": "raw suspicious reason", "raw_message": "raw suspicious reason"})
    row = logs.flush()["records"][0]
    assert row["key"] == "mihomo.connection_failed"
    # T-P4-C1 #2: only the catalog report (title/detail) is rendered; the raw
    # suspicious reason never reaches the wire.
    assert row["raw"] == "" and row["title"] and row["detail"]
    assert "suspicious" not in row["title"] + row["detail"]
    assert row["is_key_event"] is False  # catalog: is_key_event=False


def test_mihomo_oversize_line_is_one_loss_and_next_line_recovers():
    records, dropped = [], []
    response = BytesIO(b"x" * 50 + b"\n" + b'{"payload":"safe"}\n')
    client = SimpleNamespace(base_url="http://127.0.0.1:1", secret="synthetic", timeout=1,
                             opener=lambda req, timeout: response)
    stream = BoundedMihomoStream(lambda: client, "/logs", records.append,
                                 note_dropped=lambda: dropped.append(1), line_limit=32)
    stream._consume(client)
    assert records == [{"payload": "safe"}]
    assert dropped == [1] and response.closed


def test_reconnect_backoff_is_exponential_and_bounded():
    waits, attempts, received = [], [], []
    class Stop:
        stopped = False
        def is_set(self):
            return self.stopped
        def wait(self, delay):
            waits.append(delay)
            self.stopped = len(waits) == 4
            return self.stopped
    def opener(request, timeout):
        attempts.append(request.full_url)
        if len(attempts) == 1:
            raise OSError("synthetic disconnect")
        return BytesIO(b'{"type":"info","payload":"safe"}\n')
    client = SimpleNamespace(base_url="http://127.0.0.1:1", secret="", timeout=1, opener=opener)
    stream = BoundedMihomoStream(lambda: client, "/logs", received.append, backoff=0.5, max_backoff=1)
    stream.stop_event = Stop()
    stream.run()
    assert waits == [0.5, 1, 1, 1]
    assert len(attempts) == 4 and len(received) == 3
    assert stream.reconnects == 3


@pytest.mark.parametrize("endpoint", ["/logs", "/traffic"])
def test_cpu_metrics_update_while_stream_connection_remains_open(endpoint, monkeypatch):
    # Windows thread CPU counters can remain zero for sub-clock-tick work.
    # A deterministic clock tests refresh timing without a CPU burn or sleeps.
    cpu_clock = count(0, 1000)
    monkeypatch.setattr(time, "thread_time_ns", lambda: next(cpu_clock))
    blocked, next_blocked, release = Event(), Event(), Event()
    attempts, records = [], []
    class Response:
        reads = 0
        closed = False
        def readline(self, limit):
            self.reads += 1
            if self.reads == 2:
                blocked.set()
                assert release.wait(2)
            elif self.reads == 3:
                release.clear()
                next_blocked.set()
                release.wait(2)
                return b""
            return b'{"type":"info","payload":"safe","up":1,"down":2}\n'
        def close(self):
            self.closed = True
            release.set()
    response = Response()
    def opener(request, timeout):
        attempts.append(request.full_url)
        return response
    client = SimpleNamespace(base_url="http://127.0.0.1:1", secret="", timeout=1, opener=opener)
    def consume(record):
        records.append(record)
    stream = BoundedMihomoStream(lambda: client, endpoint, consume)
    stream.start()
    try:
        assert blocked.wait(1)
        first_cpu = stream.worker_thread_cpu_ns
        assert first_cpu > 0 and len(records) == 1 and not response.closed
        release.set()
        assert next_blocked.wait(1)
        assert stream.worker_thread_cpu_ns > first_cpu
        assert len(records) == 2 and len(attempts) == 1 and stream.reconnects == 0
        assert not response.closed and stream.thread.is_alive()
    finally:
        stream.stop()
    assert response.closed and not stream.thread.is_alive()


def test_stop_is_bounded_and_closes_only_owned_response():
    reading, release = Event(), Event()
    class Response:
        closed = False
        def readline(self, limit):
            reading.set()
            release.wait(2)
            return b""
        def close(self):
            self.closed = True
            release.set()
    response = Response()
    other = BytesIO(b"unrelated")
    client = SimpleNamespace(base_url="http://127.0.0.1:1", secret="", timeout=1,
                             opener=lambda req, timeout: response)
    stream = BoundedMihomoStream(lambda: client, "/logs", lambda r: None)
    stream.start()
    assert reading.wait(1)
    started = time.monotonic()
    stream.stop(timeout=0.5)
    assert time.monotonic() - started < 0.8
    assert response.closed and not other.closed and not stream.thread.is_alive()


def test_one_publisher_frame_and_protocol_validation():
    class Transport:
        frames = []
        def send(self, frame):
            protocol.encode_frame(frame)
            self.frames.append(frame)
            return "complete"
        def poll_write(self):
            return "complete"
    state, logs = new_collector()
    transport = Transport()
    publisher = Publisher(state, transport)
    for i in range(10):
        logs.put_nowait({"message": f"subscription https://example.invalid/{i}"})
    logs.flush()
    publisher.tick()
    assert len(transport.frames) == 1
    assert transport.frames[0]["t"] == "batch"
    assert protocol.validate_frame(transport.frames[0]) == []


@pytest.mark.parametrize("period", [0.01, 0.3])
def test_invalid_batch_period(period):
    with pytest.raises(ValueError):
        new_collector(period=period)


def test_exhausted_processing_budget_drops_before_capture_or_filter(monkeypatch):
    state, logs = new_collector(work_budget_ns=1)
    logger = logs.logger
    before = list(logger.handlers)
    def forbidden(*args, **kwargs):
        raise AssertionError("budget loser reached processing")
    logs.start()
    try:
        with logs._lock:
            logs._work_tokens = -1000000
        monkeypatch.setattr("bridge_logs.captured_record_from_text_line", forbidden)
        monkeypatch.setattr("bridge_logs.redact_record", forbidden)
        assert not logs.ingest_mihomo({"payload": "token=NEVER_QUEUE"})
        assert not logs.put_nowait({"message": "password=NEVER_QUEUE"})
        assert logs.metrics()["budget_dropped"] == 2
        batch = logs.flush()
        assert batch["records"] == []
        assert (batch["dropped"], batch["dropped_total"], batch["gap"]) == (2, 2, True)
        assert protocol.validate_logs_batch(batch) == []
        assert "NEVER_QUEUE" not in str(state.pending())
    finally:
        logs.stop()
    assert logger.handlers == before


def test_discarded_metadata_still_has_a_work_limit_and_gap():
    _, logs = new_collector()
    assert not logs.put_nowait({"message": "safe", "metadata": ["token=private"] * 2049})
    assert logs.metrics()["budget_dropped"] == 1
    assert logs.metrics()["serialization_failures"] == 0
    batch = logs.flush()
    assert batch["records"] == [] and batch["dropped"] == 1 and batch["gap"]


def test_nonfinite_discarded_metadata_is_not_hidden_by_projection():
    _, logs = new_collector()
    assert not logs.put_nowait({"message": "safe", "metadata": {"value": float("nan")}})
    assert logs.flush()["dropped"] == 1


def test_refresh_registry_owns_values_and_preserves_previous_on_invalid_refresh():
    _, logs = new_collector()
    values = {"OpaqueSyntheticValue": "password"}
    logs.refresh_known_secret_values(values)
    values.clear()
    with pytest.raises(ValueError):
        logs.refresh_known_secret_values({"OpaqueSyntheticValue": "bad:label"})
    assert logs.put_nowait({"message": "OpaqueSyntheticValue"})
    assert logs.flush()["records"][0]["raw"] == "[redacted:password]"
    logs.refresh_known_secret_values({"NewOpaqueSyntheticValue": "token"})
    assert logs.put_nowait({"message": "NewOpaqueSyntheticValue"})
    assert logs.flush()["records"][0]["raw"] == "[redacted:token]"
    assert "OpaqueSynthetic" not in str(logs.metrics())


def test_actual_cpu_token_bucket_debt_refill_and_nested_measurement(monkeypatch):
    import bridge_logs
    _, logs = new_collector(work_budget_ns=20_000_000)
    clock = [0.0]
    monkeypatch.setattr(bridge_logs.time, "monotonic", lambda: clock[0])
    logs._budget_active = True
    logs._budget_at = 0.0
    assert logs._admit()
    assert logs._work_tokens == 40_000_000  # Admission reserves nothing.
    logs._charge_work(45_000_000)
    assert not logs._admit()
    clock[0] = .5
    assert logs._admit() and logs._work_tokens == 5_000_000
    clock[0] = 10
    assert logs._admit() and logs._work_tokens == 40_000_000
    cpu = count(0, 1000)
    monkeypatch.setattr(bridge_logs.time, "thread_time_ns", lambda: next(cpu))
    prior = logs.work_cpu_ns
    assert logs.ingest_mihomo({"payload": "safe"})
    assert logs.work_cpu_ns - prior == 3000  # outer interval, inner put not double charged


def test_state_log_validation_scans_payload_once_and_keeps_envelope_defense(monkeypatch):
    state, logs = new_collector()
    assert logs.put_nowait({"message": "safe"})
    validate = protocol.validate_logs_batch
    scans = []
    def measured(dto):
        scans.append(len(dto["records"]))
        return validate(dto)
    monkeypatch.setattr(protocol, "validate_logs_batch", measured)
    batch = logs.flush()
    assert scans.count(1) == 1
    assert protocol.validate_logs_batch(batch) == []
    state2 = PublicationState("https://example.invalid/secret-epoch")
    with pytest.raises(ValueError):
        state2.record_edge("logs.batch", batch)
    unsafe = dict(batch, records=[dict(batch["records"][0], raw="password=SyntheticValue")])
    protocol.privacy_diagnostics.clear()
    state.record_edge("logs.batch", unsafe)
    assert protocol.privacy_diagnostics
    assert state.depth == 2


def test_publication_cpu_bucket_debits_observed_cost_and_blocks_unredacted_input(monkeypatch):
    import bridge_logs
    state, logs = new_collector()
    clock = [0.0]
    monkeypatch.setattr(bridge_logs.time, "monotonic", lambda: clock[0])
    logs._budget_active = True
    logs._budget_at = logs._publication_at = 0.0
    assert logs._admit()
    state._log_publication_cpu_ns = 90_000_000  # synthetic completed thread CPU work
    assert not logs.put_nowait({"message": "OpaqueNeverQueuedValue"})
    assert logs.budget_dropped == 1 and not logs._ring
    assert logs._publication_tokens == -10_000_000
    clock[0] = 1.0
    assert logs._admit()
    assert logs._publication_tokens == 30_000_000
    batch = logs.flush()
    assert batch["dropped"] == 1 and batch["gap"]
    assert "OpaqueNeverQueuedValue" not in str(state.pending())


def test_publication_thread_cpu_settles_ack_and_busy_retry_without_double_count(monkeypatch):
    import bridge_state
    state, logs = new_collector()
    assert logs.put_nowait({"message": "safe"})
    logs.flush()
    cpu = [100]
    monkeypatch.setattr(bridge_state, "thread_time_ns", lambda: cpu[0])
    pending = state.pending()
    cpu[0] = 300
    # A busy/error attempt has no ack, so its cost settles on the next read.
    retry = state.pending()
    assert state.log_publication_cpu_ns == 200
    assert retry == pending
    cpu[0] = 700
    state.ack(retry)
    assert state.log_publication_cpu_ns == 600
    state.ack(retry)
    assert state.log_publication_cpu_ns == 600
    assert state.depth == 0


@pytest.mark.parametrize("sample", __import__("json").loads(
    (Path(__file__).parent / "fixtures/redaction_corpus/round4_decision.json").read_bytes()),
    ids=lambda sample: sample["kind"])
def test_round4_collector_publisher_wire_has_no_registered_or_feature_credentials(sample):
    from bridge_publish import Publisher
    import json
    class Transport:
        frames = []
        def send(self, frame):
            self.frames.append(protocol.decode_line(protocol.encode_frame(frame)))
            return "complete"
        def poll_write(self):
            return "complete"
    state, logs = new_collector(known_secret_values=sample.get("known_secret_values", {}))
    protocol.privacy_diagnostics.clear()
    assert logs.put_nowait({"message": sample["input"]})
    queued = logs._ring[0][0]
    assert queued["raw"] == sample["expected"]  # Already safe before batching.
    logs.flush()
    publisher = Publisher(state, Transport())
    publisher.tick()
    assert not protocol.privacy_diagnostics
    assert not logs.diagnostics and logs.dropped_total == 0
    assert publisher.transport.frames[-1]["items"][0]["data"]["records"][0]["raw"] == sample["expected"]
    wire = json.dumps(publisher.transport.frames[-1])
    for value in sample.get("known_secret_values", {}):
        assert value not in wire


def test_repeated_capture_cache_keeps_current_time_severity_and_registry(monkeypatch):
    import bridge_logs
    state, logs = new_collector(known_secret_values={"SyntheticFirstSecret": "token"})
    capture = bridge_logs.captured_record_from_text_line
    calls = []
    def counted(*args, **kwargs):
        calls.append(1)
        return capture(*args, **kwargs)
    monkeypatch.setattr(bridge_logs, "captured_record_from_text_line", counted)
    now = [1000]
    monkeypatch.setattr(bridge_logs, "ms", lambda: now[0])
    text = "SyntheticFirstSecret SyntheticNextSecret"
    assert logs.ingest_mihomo({"type": "info", "payload": text})
    first = logs.flush()["records"][0]
    assert "SyntheticFirstSecret" not in first["raw"] and "SyntheticNextSecret" in first["raw"]
    logs.refresh_known_secret_values({"SyntheticNextSecret": "password"})
    now[0] = 2000
    assert logs.ingest_mihomo({"type": "debug", "payload": text})
    second = logs.flush()["records"][0]
    assert len(calls) == 1
    assert second["ts_ms"] == second["rx_ms"] == 2000 and second["level"] == "debug"
    assert second["seq"] > first["seq"]
    assert "SyntheticNextSecret" not in second["raw"] and "SyntheticFirstSecret" in second["raw"]
    for i in range(300):
        assert logs.ingest_mihomo({"type": "info", "payload": f"plain distinct line {i}"})
    assert len(logs._capture_cache) == 128
    assert logs._known_secrets.filter_text.cache_info().currsize <= 256


def test_repeated_capture_preserves_embedded_log_timestamp():
    _, logs = new_collector()
    text = 'time="2026-09-30T12:00:00+00:00" level=info msg="plain"'
    assert logs.ingest_mihomo({"type": "info", "payload": text})
    first = logs.flush()["records"][0]
    assert logs.ingest_mihomo({"type": "info", "payload": text})
    second = logs.flush()["records"][0]
    assert first["ts_ms"] == second["ts_ms"]
    assert second["rx_ms"] >= first["rx_ms"]


def test_flat_log_state_copies_are_owned_and_nested_extensions_still_deepcopy():
    from bridge_state import _copy_log_payload
    state, logs = new_collector()
    assert logs.put_nowait({"message": "plain"})
    batch = logs.flush()
    batch["records"][0]["raw"] = "mutated by producer"
    owned = state.pending()
    assert owned[0]["data"]["records"][0]["raw"] == "plain"
    owned[0]["data"]["records"][0]["raw"] = "mutated by reader"
    assert state.pending()[0]["data"]["records"][0]["raw"] == "plain"
    nested = {"records": [{"raw": "plain", "extension": {"safe": [1]}}], "dropped": 0}
    copied = _copy_log_payload(nested)
    nested["records"][0]["extension"]["safe"].append(2)
    assert copied["records"][0]["extension"]["safe"] == [1]


def test_invalid_capture_is_not_cached_and_only_that_record_is_lost(monkeypatch):
    import bridge_logs
    _, logs = new_collector()
    capture = bridge_logs.captured_record_from_text_line
    calls = [0]
    def first_bad(*args, **kwargs):
        calls[0] += 1
        result = capture(*args, **kwargs)
        if calls[0] == 1:
            result["metadata"]["broken"] = object()
        return result
    monkeypatch.setattr(bridge_logs, "captured_record_from_text_line", first_bad)
    record = {"type": "info", "payload": "plain same text"}
    assert not logs.ingest_mihomo(record)
    assert not logs._capture_cache
    assert logs.ingest_mihomo(record)
    batch = logs.flush()
    assert calls[0] == 2 and len(batch["records"]) == 1
    assert batch["dropped"] == 1 and batch["gap"]
