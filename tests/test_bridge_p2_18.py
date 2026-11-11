import json
import os
from io import BytesIO
from threading import Event, Thread
from types import SimpleNamespace
import time

import pytest

import bridge_protocol as protocol
from bridge_log_view import LogView, PAGE_BYTES, SnapshotRequired, wire_bytes
from bridge_logs import BoundedMihomoStream, LogCollector, LogNotRunning
from bridge_state import PublicationState
from log_store import LogStore
from log_run_boundary import is_run_start


def collector(**kwargs):
    state = PublicationState("p2-18-isolated")
    state.subscribe(["logs.batch"])
    return LogCollector(state, **kwargs)


def row(text="same", **kwargs):
    return dict(ts_ms=1, rx_ms=1, key="", source="MIHOMO", level="info",
                category="other", title="", detail="", raw=text, **kwargs)


def test_seed_file_live_seam_preserves_two_equal_events_and_redacts(tmp_path):
    path = tmp_path / "history.jsonl"
    data = {"source": "TESSERACT", "message_raw": "same OpaqueSeedCredential", "level": "info"}
    line = (json.dumps(data) + "\n").encode()
    path.write_bytes(line * 2)
    logs = collector(known_secret_values={"OpaqueSeedCredential": "password"})
    assert logs.seed_file(path, generation=1) == 2
    assert not logs.ingest_source(data, source="runtime", generation=1, cursor=len(line) * 2)
    assert logs.ingest_source(data, source="runtime", generation=1, cursor=len(line) * 3)
    page = logs.tail({"n": 4000, "since_seq": 0})
    assert [r["seq"] for r in page["records"]] == [1, 2, 3]
    assert all("OpaqueSeedCredential" not in r["raw"] for r in page["records"])
    assert path.read_bytes() == line * 2  # Evidence untouched.


def test_logstore_adapter_and_timestamp_and_live_cursor():
    store = LogStore()
    store.append_many([{"message": "one", "timestamp": "2026-10-01T01:02:03+00:00"},
                       {"message": "two", "timestamp": "2026-10-01T01:02:04+00:00"}])
    logs = collector()
    assert logs.seed(store, generation=9) == 2
    assert logs.seed(store, generation=9) == 0
    assert logs.view.snapshot()[0][0]["ts_ms"] == 1790816523000
    assert logs.ingest_source({"message": "two"}, source="runtime", generation=9, cursor=3)


def test_monitor_tail_and_partial_last_line_are_bounded(tmp_path):
    path = tmp_path / "monitor.log"
    path.write_bytes(b"2026-10-01 01:02:03 [INFO] plain\npartial unfinished")
    logs = collector()
    assert logs.seed_file(path, generation=1) == 1
    assert logs.tail({"n": 1})["records"][0]["raw"] == "plain"


def boundaries(logs):
    return [json.loads(r[0]["raw"]) for r in logs.view.snapshot() if r[0]["key"] == "source_boundary"]


def events(logs):
    return [r[0] for r in logs.view.snapshot() if r[0]["key"] != "source_boundary"]


def test_source_local_cursors_preserve_equal_events_without_shared_identity():
    logs = collector(client_factory=lambda: None)
    logs.stdout_started(1)
    assert logs._ingest_stream({"type": "info", "payload": "same"})
    assert logs._ingest_stream({"type": "info", "payload": "same"})
    assert not logs.ingest_source({"payload": "same"}, source="logs", generation=0, cursor=2)
    assert not logs.ingest_stdout("same", generation=1, cursor=1)
    logs.stream_state("reconnecting")
    assert logs.ingest_stdout("same", generation=1, cursor=2)
    assert not logs.ingest_stdout("same", generation=1, cursor=2)
    assert logs.ingest_stdout("same", generation=1, cursor=3)
    assert logs._ingest_stream({"type": "info", "payload": "same"})
    assert not logs.ingest_stdout("same", generation=1, cursor=4)
    assert [r["raw"] for r in events(logs)] == ["same"] * 5
    origins = [e.source_cursor for e in logs.view._rows if e.source_cursor]
    assert origins == [("mihomo_api", 0, 1), ("mihomo_api", 0, 2),
                       ("stdout", 1, 2), ("stdout", 1, 3), ("mihomo_api", 0, 3)]
    assert all(b["continuity"] == "unknown" for b in boundaries(logs))


def test_open_response_does_not_restore_until_first_event():
    logs = collector(client_factory=lambda: None)
    logs.stdout_started(1)
    logs.stream_state("connected")
    assert logs.feed()["state"] == "stdout"
    assert logs.ingest_stdout("before first API event", generation=1, cursor=1)
    assert logs._ingest_stream({"type": "info", "payload": "first API event"})
    assert logs.feed()["state"] == "connected"
    assert not logs.ingest_stdout("after first API event", generation=1, cursor=2)


def test_stdout_only_and_tail_watermark_stay_stable():
    logs = collector()
    logs.stdout_started(1)
    assert logs.ingest_stdout("same", generation=1, cursor=1)
    assert logs.feed()["state"] == "stdout"
    for i in range(2, 5):
        logs.ingest_stdout("same", generation=1, cursor=i)
    page = logs.tail({"since_seq": 0, "n": 2})
    assert [r["seq"] for r in page["records"]] == [1, 2]
    assert page["records"][0]["key"] == "source_boundary"
    logs.ingest_stdout("new", generation=1, cursor=5)
    following = logs.tail({"since_seq": 0, "n": 2, "cursor": page["next_cursor"]})
    assert following["watermark"] == 5
    assert [r["seq"] for r in following["records"]] == [3, 4]


def test_seed_reads_only_last_lines_and_byte_offsets(tmp_path):
    path = tmp_path / "history.jsonl"
    lines = [(json.dumps({"message": str(i)}) + "\n").encode() for i in range(6)]
    path.write_bytes(b"".join(lines))
    logs = collector()
    assert logs.seed_file(path, generation=1, limit=2) == 2
    assert [r["raw"] for r in logs.tail({"n": 4000})["records"]] == ["4", "5"]
    assert not logs.ingest_source({"message": "5"}, source="runtime", generation=1,
                                  cursor=sum(map(len, lines)))


def test_generation_and_old_source_cursor_are_rejected():
    logs = collector(capacity=2)
    logs.stdout_started(1)
    for i in range(3):
        assert logs.ingest_source({"message": "same"}, source="stdout", generation=1, cursor=i)
    assert not logs.ingest_source({"message": "same"}, source="stdout", generation=1, cursor=2)
    assert not logs.ingest_source({"message": "same"}, source="stdout", generation=0, cursor=999)
    logs.stdout_started(2)
    assert logs.ingest_source({"message": "same"}, source="stdout", generation=2, cursor=0)
    assert len(events(logs)) == 4


def test_stream_callback_states_and_info_debug_only_new_connection():
    urls, states = [], []
    client = SimpleNamespace(base_url="http://example.invalid", secret="", timeout=1,
        opener=lambda request, timeout: (urls.append(request.full_url) or BytesIO(b"")))
    stream = BoundedMihomoStream(lambda: client, "/logs", lambda r: None, on_state=states.append)
    stream._consume(client)
    assert stream.set_level("debug") == {"level": "debug", "applied": True}
    stream._consume(client)
    stream.set_level("info")
    stream._consume(client)
    assert urls == ["http://example.invalid/logs?level=info", "http://example.invalid/logs?level=debug",
                    "http://example.invalid/logs?level=info"]
    assert states == ["connected", "reconnecting"] * 3
    with pytest.raises(ValueError):
        stream.set_level("silent")
    with pytest.raises(LogNotRunning):
        collector().set_mihomo_level({"level": "debug"})
    logs = collector(client_factory=lambda: None)
    assert logs.set_mihomo_level({"level": "debug"})["applied"]
    assert logs.feed()["level"] == "debug"


def test_bounded_storage_gap_and_evicted_cursor():
    view = LogView(max_items=3)
    for _ in range(3):
        view.append(row())
    cursor = view.tail(n=1, since_seq=0)["next_cursor"]
    view.append(row())
    assert view.min_seq == 2
    page = view.tail(n=3, since_seq=0)
    assert page["gap"] and page["dropped"] == 1
    with pytest.raises(SnapshotRequired):
        view.tail(n=1, since_seq=0, cursor=cursor)
    byte_view = LogView(max_bytes=1024)
    for _ in range(10):
        byte_view.append(row("x" * 200))
    assert byte_view.metrics()["history_bytes"] <= 1024
    assert byte_view.metrics()["history_evicted"] > 0


def test_cursor_expiry_binding_tampering_and_clear():
    clock = [0]
    view = LogView(clock=lambda: clock[0])
    for _ in range(4):
        view.append(row())
    page = view.query(before_seq=5, filter={}, n=1)
    cursor = page["next_cursor"]
    for changes in ({"n": 2}, {"before_seq": 4}, {"filter": {"text": "same"}},
                    {"cursor": cursor + "x"}):
        with pytest.raises(SnapshotRequired):
            view.query(**(dict(before_seq=5, filter={}, n=1, cursor=cursor) | changes))
    clock[0] = 60
    with pytest.raises(SnapshotRequired):
        view.query(before_seq=5, filter={}, n=1, cursor=cursor)
    clock[0] = 0
    view.clear_view()
    with pytest.raises(SnapshotRequired):
        view.query(before_seq=5, filter={}, n=1, cursor=cursor)


def test_clear_is_only_view_watermark_and_pending_batch_survives():
    logs = collector()
    logs.put_nowait({"message": "before"})
    assert logs.clear_view() == 1
    assert len(logs.view.snapshot()) == 1
    assert not logs.tail({"n": 4000})["records"]
    assert logs.flush()["records"][0]["raw"] == "before"
    logs.put_nowait({"message": "after"})
    assert logs.tail({"n": 4000})["records"][0]["seq"] == 2


def test_fixed_watermark_query_filter_and_new_writes():
    view = LogView()
    for i in range(6):
        view.append(row(str(i)))
    page = view.query(before_seq=7, filter={"sources": ["MIHOMO"]}, n=2)
    assert [r["seq"] for r in page["records"]] == [6, 5]
    view.append(row("new"))
    result = list(page["records"])
    while page["next_cursor"]:
        page = view.query(before_seq=7, filter={"sources": ["MIHOMO"]}, n=2, cursor=page["next_cursor"])
        assert page["watermark"] == 6
        result.extend(page["records"])
    assert [r["seq"] for r in result] == [6, 5, 4, 3, 2, 1]
    assert view.query(before_seq=8, filter={"text": "new"}, n=4)["records"][0]["seq"] == 7


def test_translation_and_filter_outside_lock_and_owned_snapshot():
    view = LogView()
    facts = {"event_key": "runtime.start_requested", "source": "TESSERACT", "metadata": {}}
    view.append(row(), report=facts)
    entered, release = Event(), Event()
    result = []
    def translate(text):
        entered.set()
        assert release.wait(2)
        return text
    thread = Thread(target=lambda: result.append(view.query(before_seq=2, filter={}, n=4, translate=translate)))
    thread.start()
    assert entered.wait(2)
    view.append(row("during query"))  # Must not wait for translation.
    release.set()
    thread.join(2)
    assert not thread.is_alive() and result[0]["watermark"] == 1
    result[0]["records"][0]["raw"] = "mutated"
    assert view.snapshot()[0][0]["raw"] == "same"


def test_page_exact_256k_and_one_byte_over_and_multibyte():
    # A page ending precisely at 256 KiB must fit, including UTF-8 and envelope.
    view = LogView(max_bytes=1024 * 1024)
    count = 34
    base = [dict(row("中" * 2500), key="log.raw", seq=i + 1) for i in range(count)]
    empty = dict(records=base, watermark=count, next_cursor=None, gap=False, dropped=0)
    padding = PAGE_BYTES - len(wire_bytes(empty))
    assert padding > 0
    base[-1]["raw"] += "x" * padding
    for item in base:
        view.append(item)
    page = view.tail(n=4000, since_seq=0)
    assert len(wire_bytes(page)) == PAGE_BYTES
    assert page["next_cursor"] is None
    larger = LogView(max_bytes=1024 * 1024)
    base[-1]["raw"] += "x"
    for item in base:
        larger.append(item)
    page = larger.tail(n=4000, since_seq=0)
    assert len(wire_bytes(page)) <= PAGE_BYTES and page["next_cursor"]
    next_page = larger.tail(n=4000, since_seq=0, cursor=page["next_cursor"])
    assert len(page["records"]) + len(next_page["records"]) == count


def test_run_boundaries_hash_text_and_report_facts_redaction():
    logs = collector(known_secret_values={"OpaqueReportCredential": "password"})
    assert logs.feed()["run_id"] is None
    for start in ({"source": "TESSERACT", "event_key": "runtime.start_requested", "message": "start"},
                  {"source": "TESSERACT", "message": "Desktop runtime start requested manual"}):
        previous = logs.feed()["run_id"]
        assert is_run_start(start) and logs.put_nowait(start)
        assert logs.feed()["run_id"] and logs.feed()["run_id"] != previous
    line = "Runtime route view (observed) binding_hash=4a28f930 route_hash=78c931ad"
    assert logs.put_nowait({"source": "TESSERACT", "message": line})
    assert logs.view.snapshot()[-1][0]["raw"] == line
    assert logs.put_nowait({"source": "TESSERACT", "event_key": "runtime.start_failed",
        "message": "OpaqueReportCredential", "metadata": {"event_params": {"reason": "OpaqueReportCredential"}}})
    assert "OpaqueReportCredential" not in json.dumps(logs.view.snapshot())


def test_dropcounts_total_and_wire_dtos_and_query_cpu_charged():
    logs = collector(capacity=1)
    logs.put_nowait({"message": "one", "level": "critical"})
    logs.put_nowait({"message": "two", "level": "debug"})
    logs.note_dropped(2, level="warn")
    feed = logs.feed()
    assert feed["dropped"]["critical"] == 1 and feed["dropped"]["warn"] == 2
    assert feed["dropped"]["total"] == sum(feed["dropped"][k] for k in ("debug", "info", "warn", "error", "critical"))
    page = logs.query({"before_seq": 3, "filter": {}, "n": 4000})
    errors = []
    protocol._schema(page, "LogPage", "page", errors)
    assert errors == []
    assert logs.view.query_cpu_ns >= 0 and logs.metrics()["work_cpu_ns"] >= logs.view.query_cpu_ns


@pytest.mark.parametrize("n", [0, 4001, True])
def test_page_n_bounds(n):
    with pytest.raises(ValueError):
        LogView().tail(n=n)


@pytest.mark.skipif(os.environ.get("TESSERACT_BRIDGE_CPU_TEST") != "1",
                    reason="CPU measurement requires an explicitly scheduled window")
def test_storage_and_query_cpu_per_record():
    logs = collector()
    count = 1000
    started = time.process_time_ns()
    for i in range(count):
        logs.ingest_mihomo({"type": "info", "payload": f"plain event {i % 6}"})
        if i % 100 == 99:
            logs.flush()
            logs.state.ack(logs.state.pending())
        if i % 250 == 249:
            logs.query({"before_seq": i + 2, "filter": {"text": "plain"}, "n": 4000})
    cpu = time.process_time_ns() - started
    average_us = cpu / count / 1000
    print(f"p2_18 input={count} history={logs.view.metrics()['history_count']} "
          f"cpu_ns={cpu} avg_cpu_us={average_us:.3f} query_cpu_ns={logs.view.query_cpu_ns}")
    # Same 50/s <= .02-core and 500/s <= .1-core budgets, with query work.
    assert average_us * 50 / 1e6 <= .02
    assert average_us * 500 / 1e6 <= .1
    assert logs.view.metrics()["history_count"] == count


@pytest.mark.parametrize("source", ["direct", "runtime", "stdout", "logs"])
def test_source_budget_exhaustion_precedes_capture_and_redaction(source, monkeypatch):
    import bridge_logs
    logs = collector()
    logs.stdout_started(1)
    logs._budget_active = True
    logs._work_tokens = -1_000_000_000
    def forbidden(*args, **kwargs):
        pytest.fail("exhausted budget reached event parsing or redaction")
    monkeypatch.setattr(bridge_logs, "captured_record_from_text_line", forbidden)
    monkeypatch.setattr(bridge_logs, "redact_record", forbidden)
    record = {"message": "event", "level": "critical"}
    if source == "direct":
        accepted = logs.put_nowait(record)
    elif source == "stdout":
        accepted = logs.ingest_stdout('time="2026-10-01T00:00:00Z" level=fatal msg="event"',
                                      generation=1, cursor=1)
    elif source == "logs":
        accepted = logs._ingest_stream({"type": "fatal", "payload": "event"})
    else:
        accepted = logs.ingest_source(record, source=source, generation=1, cursor=1)
    assert not accepted and logs.budget_dropped == 1
    assert logs.feed()["dropped"] == dict(debug=0, info=0, warn=0, error=0, critical=1, total=1)
    assert logs.view.latest_seq == (2 if source == "logs" else 1)
    assert not events(logs)


def test_new_sources_and_logging_handler_admit_exactly_once(monkeypatch):
    import logging
    from bridge_logs import _MeasuredLogHandler
    logs = collector()
    logs.stdout_started(1)
    calls = []
    monkeypatch.setattr(logs, "_admit", lambda level="info": calls.append(level) or True)
    assert logs.ingest_source({"message": "runtime"}, source="runtime", generation=1, cursor=1)
    assert logs.ingest_stdout("stdout", generation=1, cursor=1)
    assert logs._ingest_stream({"type": "info", "payload": "logs"})
    handler = _MeasuredLogHandler(logs)
    try:
        handler.emit(logging.LogRecord("isolated", logging.INFO, "", 0, "handler", (), None))
    finally:
        handler.close()
    assert calls == ["info"] * 4
    assert logs.view.latest_seq == 6


def test_formatter_nested_runtime_record_does_not_borrow_handler_admission(monkeypatch):
    import logging
    from bridge_logs import _MeasuredLogHandler
    logs = collector()
    decisions = iter([True, False])
    monkeypatch.setattr(logs, "_admit", lambda level="info": next(decisions))
    class Message:
        def __str__(self):
            assert not logs.ingest_source({"message": "nested"}, source="runtime", generation=1, cursor=1)
            return "outer"
    handler = _MeasuredLogHandler(logs)
    try:
        handler.emit(logging.LogRecord("isolated", logging.INFO, "", 0, Message(), (), None))
    finally:
        handler.close()
    assert [r[0]["raw"] for r in logs.view.snapshot()] == ["outer"]


def test_stdout_reader_lifecycle_and_late_old_generation_callbacks():
    logs = collector(client_factory=lambda: None)
    assert logs.stdout_started(1)
    assert logs.ingest_stdout("old", generation=1, cursor=1)
    old_stream = logs._bind_stream()
    logs.stream_state("stopped")
    assert logs.feed()["state"] == "stopped"
    assert not logs.stdout_started(1)  # A stopped reader cannot reacquire authority.
    logs.stream_state("reconnecting")
    assert logs.feed()["state"] == "reconnecting"
    assert not logs.ingest_stdout("late old", generation=1, cursor=2)
    assert logs.stdout_started(2)
    logs.stdout_stopped(1)
    assert not logs.stdout_started(1)
    assert logs.feed()["state"] == "stdout"
    assert not logs.ingest_stdout("late old", generation=1, cursor=3)
    with pytest.raises(RuntimeError, match="Core log stream binding expired"):
        old_stream({"type": "info", "payload": "late old API"})
    assert logs.ingest_stdout("new", generation=2, cursor=1)
    logs.stdout_stopped(2)
    assert logs.feed()["state"] == "reconnecting"
    assert not logs.ingest_stdout("after EOF", generation=2, cursor=2)
    assert not logs.stdout_started(2)
    assert [r["raw"] for r in events(logs)] == ["old", "new"]
    assert [b["reason"] for b in boundaries(logs)] == ["started", "stopped", "core_generation", "stopped"]


def test_real_wire_shape_has_no_joint_event_identity_across_disconnect_and_restore():
    from core_runner import CoreOutputEvent
    logs = collector(client_factory=lambda: None)
    assert logs.stdout_started(7)
    event = CoreOutputEvent(raw_line='time="2026-10-01T00:00:00Z" level=info msg="same"',
                            pid=123, process_generation=7, received_at_monotonic=1.0)
    class Response(BytesIO):
        def readline(self, size=-1):
            value = super().readline(size)
            if not value:
                assert logs.feed()["state"] == "connected"
                assert not logs.ingest_stdout(event.raw_line, generation=7, cursor=100)
            return value
    client = SimpleNamespace(base_url="http://example.invalid", secret="", timeout=1,
        opener=lambda request, timeout: Response(b'{"type":"info","payload":"same"}\n'))
    logs.stream._consume(client)
    assert logs.feed()["state"] == "stdout"
    assert logs.ingest_stdout(event.raw_line, generation=event.process_generation, cursor=2)
    assert logs.ingest_stdout(event.raw_line, generation=event.process_generation, cursor=3)
    logs.stream._consume(client)
    assert logs.feed()["state"] == "stdout"
    assert [r["raw"] for r in events(logs)] == ["same"] * 4
    switches = boundaries(logs)[1:]
    assert [(b["from"], b["to"], b["reason"]) for b in switches] == [
        ("stdout", "mihomo_api", "recovered"), ("mihomo_api", "stdout", "disconnected"),
        ("stdout", "mihomo_api", "recovered"), ("mihomo_api", "stdout", "disconnected")]
    assert all(b["at_ms"] >= 0 and b["continuity"] == "unknown" for b in switches)
    assert logs._source_cursors == {"logs": (0, 2), "stdout": (7, 3)}
    assert not logs.diagnostics
    assert not protocol.validate_logs_batch(logs.flush())


def test_stream_run_detects_eof_and_restores_with_real_messages(monkeypatch):
    from core_runner import CoreOutputEvent
    logs = collector(client_factory=lambda: client)
    logs.stdout_started(7)
    event = CoreOutputEvent(raw_line='time="2026-10-01T00:00:00Z" level=info msg="same"',
                            pid=123, process_generation=7, received_at_monotonic=1.0)
    observations, waits = [], []
    class Response(BytesIO):
        def readline(self, size=-1):
            value = super().readline(size)
            if not value:
                observations.append(logs.feed()["state"])
                assert not logs.ingest_stdout(event.raw_line, generation=7, cursor=100)
            return value
    client = SimpleNamespace(base_url="http://example.invalid", secret="", timeout=1,
        opener=lambda request, timeout: Response(b'{"type":"info","payload":"same"}\n'))
    def backoff_wait(delay):
        waits.append(logs.feed()["state"])
        if len(waits) == 1:
            assert logs.ingest_stdout(event.raw_line, generation=7, cursor=1)
            return False
        logs.stream.stop_event.set()
        return True
    monkeypatch.setattr(logs.stream.stop_event, "wait", backoff_wait)
    logs.stream.run()
    assert observations == ["connected", "connected"]
    assert waits == ["stdout", "stdout"]
    assert [r["raw"] for r in events(logs)] == ["same"] * 3
    assert [b["reason"] for b in boundaries(logs)] == [
        "started", "recovered", "disconnected", "recovered", "disconnected", "stopped"]
    assert logs.feed()["state"] == "stopped"


@pytest.mark.parametrize("disconnect", ["eof", "read_error", "oversized_eof", "close_error"])
def test_disconnect_handoff_precedes_blocked_close_and_cleanup_is_bounded(disconnect, monkeypatch):
    logs = collector(client_factory=lambda: client)
    logs.stdout_started(7)
    closing, release, cleanup_wait = Event(), Event(), Event()
    states, attempts, close_calls = [], [], []
    class Response:
        reads = 0
        def readline(self, size):
            self.reads += 1
            if self.reads == 1:
                return b'{"type":"info","payload":"same"}\n'
            if disconnect == "read_error":
                raise OSError("synthetic read failure")
            if disconnect == "oversized_eof" and self.reads == 2:
                return b"x" * size
            return b""
        def close(self):
            close_calls.append(1)
            closing.set()
            release.wait(3)
            if disconnect == "close_error":
                raise OSError("synthetic close failure")
    response = Response()
    def opener(request, timeout):
        attempts.append(request.full_url)
        return response
    client = SimpleNamespace(base_url="http://example.invalid", secret="", timeout=1, opener=opener)
    def on_state(state):
        states.append(state)
        logs.stream_state(state)
    logs.stream.on_state = on_state
    logs.stream.backoff = 0.01
    original_wait = logs.stream.stop_event.wait
    def wait(delay):
        if delay == 0.1:
            cleanup_wait.set()
        return original_wait(delay)
    monkeypatch.setattr(logs.stream.stop_event, "wait", wait)
    logs.stream.start()
    try:
        assert closing.wait(1)
        assert logs.feed()["state"] == "stdout"
        boundary = boundaries(logs)[-1]
        assert (boundary["from"], boundary["to"], boundary["reason"], boundary["continuity"]) == (
            "mihomo_api", "stdout", "disconnected", "unknown")
        assert logs.feed()["updated_at_ms"] == boundary["at_ms"]
        assert logs.ingest_stdout('time="2026-10-01T00:00:00Z" level=info msg="same"',
                                  generation=7, cursor=1)
        assert cleanup_wait.wait(1)  # The bounded join returned while close is blocked.
        assert len(attempts) == 1 and close_calls == [1]
        assert states == ["connected", "reconnecting"]
        assert [r["raw"] for r in events(logs)] == ["same", "same"]
        closer, worker = logs.stream._close_thread, logs.stream.thread
        assert not logs.stream.stop(0.01)
        assert not logs.stream.stop(0.01)
        assert logs.stream._close_thread is closer and close_calls == [1]
        logs.stream.start()
        assert logs.stream.thread is worker  # No restart/helper accumulation during cleanup.
    finally:
        release.set()
        assert logs.stream.stop(1)
    assert states.count("reconnecting") == 1 and close_calls == [1]


@pytest.mark.parametrize("lifecycle", ["replace", "stop"])
def test_connection_freezes_generation_before_opener(lifecycle):
    logs = collector(client_factory=lambda: None)
    logs.stdout_started(1)
    previous = logs.view.latest_seq
    def opener(request, timeout):
        if lifecycle == "replace":
            logs.stdout_started(2)
        else:
            logs.stdout_stopped(1)
        return BytesIO(b'{"type":"info","payload":"late old API event"}\n')
    client = SimpleNamespace(base_url="http://example.invalid", secret="", timeout=1, opener=opener)
    logs.stream._consume(client)
    assert not events(logs)
    assert logs.view.latest_seq == previous + 1
    assert logs.feed()["state"] == ("stdout" if lifecycle == "replace" else "reconnecting")


@pytest.mark.parametrize("lifecycle", ["replace", "stop_restart"])
@pytest.mark.parametrize("phase", ["opener", "read"])
def test_generation_change_discards_response_and_reconnects(lifecycle, phase, monkeypatch):
    logs = collector(client_factory=lambda: client)
    logs.stdout_started(1)
    responses, waits, observations = [], [], []
    def change_generation():
        if lifecycle == "stop_restart":
            logs.stdout_stopped(1)
        logs.stdout_started(2)
    class Response(BytesIO):
        reads = 0
        closes = 0
        def readline(self, size=-1):
            self.reads += 1
            if len(responses) == 1 and self.reads == 1 and phase == "read":
                change_generation()
            if len(responses) == 2 and self.reads == 2:
                observations.append(logs.feed()["state"])
                assert not logs.ingest_stdout("blocked", generation=2, cursor=2)
            return super().readline(size)
        def close(self):
            self.closes += 1
            super().close()
    def opener(request, timeout):
        if not responses and phase == "opener":
            change_generation()
        payload = b'{"type":"info","payload":"new Core"}\n'
        response = Response(payload * (3 if not responses else 1))
        responses.append(response)
        return response
    client = SimpleNamespace(base_url="http://example.invalid", secret="", timeout=1, opener=opener)
    def backoff_wait(delay):
        waits.append(logs.feed()["state"])
        assert logs.feed()["state"] == "stdout"
        if len(waits) == 1:
            assert responses[0].closed and responses[0].reads == 1
            assert not events(logs)
            assert not logs.ingest_stdout("late old stdout", generation=1, cursor=99)
            assert logs.ingest_stdout("new stdout", generation=2, cursor=1)
            return False
        logs.stream.stop_event.set()
        return True
    monkeypatch.setattr(logs.stream.stop_event, "wait", backoff_wait)
    logs.stream.run()
    assert len(responses) == 2 and all(r.closed and r.closes == 1 for r in responses)
    assert waits == ["stdout", "stdout"] and observations == ["connected"]
    assert [r["raw"] for r in events(logs)] == ["new stdout", "new Core"]
    assert [b["reason"] for b in boundaries(logs)] == (
        ["started", "core_generation", "recovered", "disconnected", "stopped"]
        if lifecycle == "replace" else
        ["started", "stopped", "core_generation", "recovered", "disconnected", "stopped"])
    assert logs._source_cursors == {"stdout": (2, 1), "logs": (logs._core_epoch - 1, 1)}


def test_stream_admission_rejection_keeps_valid_connection(monkeypatch):
    logs = collector(client_factory=lambda: None)
    logs.stdout_started(1)
    admit = logs._admit
    accepted = []
    def reject_first(level):
        accepted.append(level)
        return len(accepted) > 1 and admit(level)
    monkeypatch.setattr(logs, "_admit", reject_first)
    response = BytesIO(b'{"type":"info","payload":"budget rejected"}\n'
                       b'{"type":"info","payload":"accepted"}\n')
    client = SimpleNamespace(base_url="http://example.invalid", secret="", timeout=1,
                             opener=lambda request, timeout: response)
    logs.stream._consume(client)
    assert [r["raw"] for r in events(logs)] == ["accepted"]
    assert logs._source_cursors == {"logs": (0, 2)}
    assert [b["reason"] for b in boundaries(logs)] == ["started", "recovered", "disconnected"]


@pytest.mark.parametrize("active", ["stdout", "mihomo_api"])
def test_core_replacement_marks_boundary_and_revokes_old_inputs(active):
    from core_runner import CoreOutputEvent
    logs = collector(client_factory=lambda: None)
    logs.stdout_started(7)
    if active == "mihomo_api":
        logs._ingest_stream({"type": "info", "payload": "old"})
    old_api = logs._bind_stream()
    previous = logs.view.latest_seq
    assert logs.stdout_started(8)
    assert logs.view.latest_seq == previous + 1
    assert boundaries(logs)[-1]["reason"] == "core_generation"
    assert (boundaries(logs)[-1]["from"], boundaries(logs)[-1]["to"]) == (active, "stdout")
    event = CoreOutputEvent(raw_line='time="2026-10-01T00:00:00Z" level=info msg="late"',
                            pid=123, process_generation=7, received_at_monotonic=1.0)
    assert not logs.ingest_stdout(event.raw_line, generation=event.process_generation, cursor=999)
    with pytest.raises(RuntimeError, match="Core log stream binding expired"):
        old_api({"type": "info", "payload": "late"})
    assert logs.view.latest_seq == previous + 1
    assert logs.feed()["state"] == "stdout"
    assert logs.ingest_stdout("new", generation=8, cursor=1)


@pytest.mark.parametrize("mode", ["tail", "query"])
def test_pages_cross_source_boundaries_with_fixed_watermark_and_filters(mode):
    logs = collector(client_factory=lambda: None)
    logs.stdout_started(1)
    logs._ingest_stream({"type": "info", "payload": "same"})
    logs.stream_state("reconnecting")
    logs.ingest_stdout("same", generation=1, cursor=1)
    logs._ingest_stream({"type": "info", "payload": "same"})
    watermark = logs.view.latest_seq
    args = ({"since_seq": 0, "n": 2} if mode == "tail" else
            {"before_seq": watermark + 1, "filter": {"sources": ["MIHOMO"], "text": "same"}, "n": 2})
    fetch = logs.tail if mode == "tail" else logs.query
    page = fetch(args)
    logs._ingest_stream({"type": "info", "payload": "later"})
    rows = []
    while True:
        errors = []
        protocol._schema(page, "LogPage", "page", errors)
        assert not errors and page["watermark"] == watermark
        assert len(wire_bytes(page)) <= PAGE_BYTES and not page["gap"]
        rows.extend(page["records"])
        if page["next_cursor"] is None:
            break
        page = fetch(dict(args, cursor=page["next_cursor"]))
    expected = list(range(1, watermark + 1))
    assert [r["seq"] for r in rows] == (expected if mode == "tail" else expected[::-1])
    assert sum(r["key"] == "source_boundary" for r in rows) == 4
    assert [r["raw"] for r in rows if r["key"] != "source_boundary"] == ["same"] * 3


def test_boundaries_survive_exhausted_data_budget_and_feed_is_synchronized(monkeypatch):
    logs = collector(client_factory=lambda: None)
    logs.stdout_started(1)
    logs._budget_active = True
    logs._work_tokens = -1_000_000_000
    logs._ingest_stream({"type": "info", "payload": "budget rejected"})
    assert logs.feed()["state"] == "connected"
    assert not events(logs)
    logs.stream_state("reconnecting")
    assert logs.feed()["state"] == "stdout"
    assert boundaries(logs)[-1]["reason"] == "disconnected"
    entered, release, observed = Event(), Event(), []
    append = logs.view.append
    def blocked_append(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return append(*args, **kwargs)
    monkeypatch.setattr(logs.view, "append", blocked_append)
    writer = Thread(target=lambda: logs._ingest_stream({"type": "info", "payload": "restore"}))
    writer.start()
    assert entered.wait(2)
    reader = Thread(target=lambda: observed.append(logs.feed()))
    reader.start()
    assert not observed
    release.set()
    writer.join(2)
    reader.join(2)
    assert not writer.is_alive() and not reader.is_alive()
    assert observed[0]["state"] == "connected"
    assert boundaries(logs)[-1]["reason"] == "recovered"


@pytest.mark.parametrize("rescue", [False, True])
def test_publication_severity_receipt_overflow_inflight_and_repeated_settlement(rescue):
    state = PublicationState("severity", edge_capacity=1)
    state.subscribe(["logs.batch"])
    logs = LogCollector(state)
    for level in ("critical", "warn"):
        assert logs.put_nowait({"message": "first", "level": level})
    logs.flush()
    inflight = state.pending()
    for level in ("debug", "error"):
        assert logs.put_nowait({"message": "second", "level": level})
    logs.flush()  # Both batches provisionally lost; the first may still be rescued.
    assert logs.feed()["dropped"]["total"] == 0
    if rescue:
        state.ack(inflight)
        state.ack(inflight)
        assert logs.feed()["dropped"]["total"] == 0
    gaps = state.pending()
    state.ack(gaps)
    expected = dict(debug=1, info=0, warn=0 if rescue else 1,
                    error=1, critical=0 if rescue else 1, total=2 if rescue else 4)
    for _ in range(3):
        state.ack(gaps)
        assert logs.feed()["dropped"] == expected
        logs.flush()
    receipt = state.settled_log_drop_counts
    receipt["critical"] = 999
    assert state.settled_log_drop_counts["critical"] == expected["critical"]


def test_publication_budget_rejection_preserves_critical_severity():
    logs = collector()
    assert logs.put_nowait({"message": "critical", "level": "critical"})
    logs.flush()
    edge = logs.state.pending()[0]
    logs.state.drop_logs(edge)
    assert logs.feed()["dropped"]["total"] == 0
    logs.state.ack(logs.state.pending())
    assert logs.feed()["dropped"] == dict(debug=0, info=0, warn=0, error=0, critical=1, total=1)


def _check_full_history_pages(monkeypatch):
    import bridge_log_view
    logs = collector()
    count = logs.view.max_items
    for i in range(count):
        assert logs.ingest_source({"message": "start", "event_key": "runtime.start_requested"},
                                  source="runtime", generation=1, cursor=i)
        if i % 1000 == 999:
            logs.flush()
            logs.state.ack(logs.state.pending())
    build = bridge_log_view.build_log_event_report
    calls = []
    def measured(*args, **kwargs):
        calls.append(1)
        return build(*args, **kwargs)
    monkeypatch.setattr(bridge_log_view, "build_log_event_report", measured)
    assert logs.query({"before_seq": count + 1, "filter": {}, "n": 1})["next_cursor"]
    assert len(calls) == 2  # Page plus one lookahead, rather than all 10k reports.
    calls.clear()
    args = {"before_seq": count + 1, "filter": {}, "n": 128}
    page, seqs, pages = logs.query(args), [], 0
    while True:
        pages += 1
        seqs.extend(row["seq"] for row in page["records"])
        assert len(wire_bytes(page)) <= PAGE_BYTES and page["watermark"] == count
        if not page["next_cursor"]:
            break
        page = logs.query(dict(args, cursor=page["next_cursor"]))
    assert seqs == list(range(count, 0, -1))
    assert len(calls) == count + pages - 1
    return logs, count, pages


def test_full_history_incremental_reports_and_continuous_pages(monkeypatch):
    _check_full_history_pages(monkeypatch)


@pytest.mark.skipif(os.environ.get("TESSERACT_BRIDGE_CPU_TEST") != "1",
                    reason="CPU measurement requires an explicitly scheduled window")
def test_full_history_incremental_reports_and_continuous_page_cpu(monkeypatch):
    started = time.process_time_ns()
    logs, count, pages = _check_full_history_pages(monkeypatch)
    average_us = (time.process_time_ns() - started) / count / 1000
    print(f"p2_18_full_history input={count} pages={pages} "
          f"avg_cpu_us={average_us:.3f} query_cpu_ns={logs.view.query_cpu_ns}")
    assert average_us * 50 / 1e6 <= .02
    assert average_us * 500 / 1e6 <= .1


def test_cached_classified_capture_reuses_validation_but_redacts_each_record(monkeypatch):
    import bridge_logs
    capture = bridge_logs.captured_record_from_text_line
    def classified(*args, **kwargs):
        record = capture(*args, **kwargs)
        record["event_key"] = "runtime.start_failed"
        record["metadata"]["event_params"] = {"reason": "OpaqueClassificationCredential"}
        return record
    monkeypatch.setattr(bridge_logs, "captured_record_from_text_line", classified)
    validate = bridge_logs.validate_record
    calls = []
    def measured(record):
        calls.append(1)
        return validate(record)
    monkeypatch.setattr(bridge_logs, "validate_record", measured)
    logs = collector()
    assert logs.ingest_mihomo({"type": "error", "payload": "classified"})
    first_calls = len(calls)
    logs.refresh_known_secret_values({"OpaqueClassificationCredential": "password"})
    assert logs.ingest_mihomo({"type": "critical", "payload": "classified"})
    assert len(calls) == first_calls
    entries = logs.view.snapshot()
    assert entries[-1][0]["level"] == "critical"
    assert "OpaqueClassificationCredential" not in json.dumps(entries[-1])
    assert logs.ingest_mihomo({"type": object(), "payload": "classified"}) is False
