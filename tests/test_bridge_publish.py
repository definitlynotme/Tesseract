"""Publisher contract tests; the CPU harness is opt in."""

import json
import os
import asyncio
from pathlib import Path
import queue
import threading
import time
from copy import deepcopy

import pytest

import bridge_protocol as protocol
from bridge_jobs import JobEventRecorder
from bridge_publish import Publisher, SamplingWorker
from bridge_state import PublicationState
from bridge_runtime_access import RuntimeAccess


FIXTURES = Path(__file__).resolve().parents[1] / "bridge_schema" / "fixtures" / "valid"


def dto(topic):
    return json.loads((FIXTURES / ("topic_" + topic.replace(".", "_") + ".json")).read_text(
        encoding="utf-8"))["data"]


class Transport:
    def __init__(self):
        self.frames = []
        self.pending = False

    def poll_write(self):
        return "pending" if self.pending else "complete"

    def send(self, frame):
        self.frames.append(protocol.decode_line(protocol.encode_frame(frame)))
        return "complete"


def test_one_batch_latest_edge_and_valid_frames():
    state, transport = PublicationState("epoch"), Transport()
    state.record_state("service.state", dto("service.state"))
    state.subscribe(["service.state", "core.info"])
    state.record_state("service.state", dto("service.state"))
    state.record_state("service.state", dto("service.state"))
    state.record_edge("core.info", dto("core.info"))
    pub = Publisher(state, transport)
    assert pub.tick()
    assert len(transport.frames) == 1
    frame = transport.frames[0]
    assert frame["t"] == "batch"
    assert [item["topic"] for item in frame["items"]] == ["core.info", "service.state"]
    assert protocol.validate_frame(frame) == []
    assert state.depth == 0
    assert pub.metrics()["merged"] == 1


def test_no_wakeup_primitives(monkeypatch):
    state = PublicationState("epoch")
    def forbidden(*_args, **_kwargs):
        raise AssertionError("producer woke a consumer")
    monkeypatch.setattr(threading.Event, "set", forbidden)
    monkeypatch.setattr(queue.Queue, "put", forbidden)
    monkeypatch.setattr(asyncio.AbstractEventLoop, "call_soon_threadsafe", forbidden)
    state.record_state("core.info", dto("core.info"))
    state.record_edge("core.info", dto("core.info"))


def test_gap_and_jobs_rejection():
    state = PublicationState("epoch", edge_capacity=2)
    state.subscribe(["core.info"])
    for _ in range(3):
        state.record_edge("core.info", dto("core.info"))
    gap = state.pending()[0]
    assert gap["kind"] == "gap"
    assert gap["data"] == {"from_seq": 1, "to_seq": 3, "dropped": 3}
    assert gap["seq"] == 4
    assert state.record_edge("core.info", dto("core.info")) == 5
    assert state.record_edge("core.info", dto("core.info")) == 6
    assert state.record_edge("core.info", dto("core.info")) == 7
    assert state.pending()[0]["data"]["to_seq"] == 7
    assert state.record_edge("core.info", dto("core.info")) == 9
    with pytest.raises(ValueError):
        state.record_edge("jobs", dto("jobs"))
    assert protocol.validate_frame({**gap, "sent_at_ms": 1}) == []


def test_subscription_snapshot_and_replacement():
    state = PublicationState("epoch")
    state.record_state("core.info", dto("core.info"))
    first = state.subscribe(["core.info"])
    assert len(first) == 1 and first[0]["kind"] == "snapshot"
    assert state.snapshot("core.info")["rev"] == first[0]["rev"]
    state.record_state("core.info", dto("core.info"))
    assert state.pending()[0]["rev"] > first[0]["rev"]
    assert protocol.validate_frame({**first[0], "sent_at_ms": 1}) == []


def test_control_snapshots_are_complete_and_ordered():
    state, transport = PublicationState("epoch"), Transport()
    state.record_state("core.info", dto("core.info"))
    pub = Publisher(state, transport)
    request = {"t": "subscribe", "epoch": "epoch", "produced_at_ms": 1,
               "sent_at_ms": 1, "id": 7, "topics": ["core.info"]}
    frames = pub.subscription_frames(request)
    assert [frame["t"] for frame in frames] == ["subscribed", "evt"]
    assert all("sent_at_ms" not in frame for frame in frames)
    assert pub.tick() and pub.tick()
    assert all(protocol.validate_frame(frame) == [] for frame in transport.frames)
    snapshot_req = {"t": "snapshot_req", "epoch": "epoch", "produced_at_ms": 2,
                    "sent_at_ms": 2, "id": 8, "topic": "core.info"}
    replacement = pub.snapshot_response(snapshot_req)
    assert replacement["snapshot"]["kind"] == "snapshot"
    assert "sent_at_ms" not in replacement
    assert pub.tick()
    assert protocol.validate_frame(transport.frames[-1]) == []


def test_history_is_cached_once_per_epoch():
    state = PublicationState("epoch")
    history = dto("traffic.history")
    state.record_state("traffic.history", history)
    first = state.subscribe(["traffic.history"])
    assert len(first) == 1
    state.record_state("traffic.history", history)
    assert state.subscribe(["traffic.history"]) == []
    assert state.snapshot("traffic.history")["rev"] == first[0]["rev"]


def test_late_tick_and_pending_write():
    now = [0.0]
    state, transport = PublicationState("epoch"), Transport()
    state.subscribe(["core.info"])
    state.record_state("core.info", dto("core.info"))
    pub = Publisher(state, transport, clock=lambda: now[0])
    transport.pending = True
    assert not pub.tick() and state.depth == 1
    now[0] = 1.0
    transport.pending = False
    state.record_state("core.info", dto("core.info"))
    assert pub.tick()
    assert pub.period > pub.base_period
    assert len(transport.frames) == 1
    assert len(transport.frames[0]["items"]) == 1


def test_reserved_jobs_fifo_precedes_state():
    state, transport = PublicationState("epoch"), Transport()
    state.subscribe(["core.info"])
    state.record_state("core.info", dto("core.info"))
    recorder = JobEventRecorder()
    event = json.loads((FIXTURES / "topic_jobs.json").read_text(encoding="utf-8"))
    event.update(epoch="epoch", rev=1, seq=1)
    assert recorder.reserve(1)
    recorder.append_reserved(event)
    pub = Publisher(state, transport, jobs=recorder)
    assert pub.tick()
    assert [item["topic"] for item in transport.frames[0]["items"]] == ["jobs", "core.info"]
    assert recorder.pending == 0
    assert protocol.validate_frame(transport.frames[0]) == []
    state.record_state("core.info", dto("core.info"))
    assert pub.tick()  # Empty recorder after the last jobs prefix was acked.
    assert not pub.diagnostics


class Runtime:
    def __init__(self):
        self.service = dto("service.state")
        self.route = dto("route.published")
        self.core = {"running": False, "pid": None, "core_generation": 0,
                     "cleanup_pending": False}
        self.summary = (1, dto("tiers.summary"))
        self.calls = {name: 0 for name in ("lifecycle_status", "published_route_status",
                                         "core_fact", "tiers_summary_status")}
        self.forbidden = []

    def __getattribute__(self, name):
        if "refresh" in name or "api_client" in name or name in {"status", "manager"}:
            object.__getattribute__(self, "forbidden").append(name)
            raise AssertionError("forbidden runtime path: " + name)
        return object.__getattribute__(self, name)

    def lifecycle_status(self):
        self.calls["lifecycle_status"] += 1
        return self.service

    def published_route_status(self):
        self.calls["published_route_status"] += 1
        return self.route

    def core_fact(self):
        self.calls["core_fact"] += 1
        return self.core

    def tiers_summary_status(self):
        self.calls["tiers_summary_status"] += 1
        return self.summary


def test_worker_stop_is_change_triggered():
    runtime = Runtime()
    runtime.service["runtime"]["startup_phase"] = "stopped"
    state = PublicationState("epoch")
    state.subscribe(["service.state", "route.published", "core.info"])
    worker = SamplingWorker(state, runtime)
    worker.sample()
    first = state.pending()
    state.ack(first)
    worker.sample()
    assert state.pending() == []
    runtime.service["intent"]["lifecycle_revision"] += 1
    worker.sample()
    assert [item["topic"] for item in state.pending()] == ["service.state"]


def subscribe_request(topics=("core.info",), request_id=7):
    return {"t": "subscribe", "epoch": "epoch", "produced_at_ms": 1,
            "sent_at_ms": 1, "id": request_id, "topics": list(topics)}


def snapshot_request(topic="core.info", request_id=8):
    return {"t": "snapshot_req", "epoch": "epoch", "produced_at_ms": 1,
            "sent_at_ms": 1, "id": request_id, "topic": topic}


def job_recorder(count=1):
    recorder = JobEventRecorder()
    assert recorder.reserve(count)
    for seq in range(1, count + 1):
        event = json.loads((FIXTURES / "topic_jobs.json").read_text(encoding="utf-8"))
        event.update(epoch="epoch", rev=seq, seq=seq)
        recorder.append_reserved(event)
    return recorder


def events(frames):
    result = []
    for frame in frames:
        if frame["t"] == "evt":
            result.append(frame)
        result.extend(frame.get("items", []))
        if "snapshot" in frame:
            result.append(frame["snapshot"])
    return result


def assert_wire_order(frames):
    revisions, sequences = {}, {}
    for frame in frames:
        assert protocol.validate_frame(frame) == []
    for event in events(frames):
        topic = event["topic"]
        assert event["rev"] > revisions.get(topic, 0)
        revisions[topic] = event["rev"]
        if event["kind"] in {"edge", "gap"}:
            previous = sequences.get(topic, 0)
            if event["kind"] == "gap":
                assert event["data"]["from_seq"] == previous + 1
                assert event["seq"] == event["data"]["to_seq"] + 1
            else:
                assert event["seq"] == previous + 1
            sequences[topic] = event["seq"]


def test_empty_jobs_and_full_edge_budget():
    state, transport = PublicationState("epoch", edge_capacity=128), Transport()
    state.subscribe(["core.info"])
    state.record_state("core.info", dto("core.info"))
    pub = Publisher(state, transport, jobs=JobEventRecorder())
    assert pub.tick() and not pub.diagnostics
    for _ in range(128):
        state.record_edge("core.info", dto("core.info"))
    pub.jobs = job_recorder()
    assert pub.tick()
    assert transport.frames[-1]["items"][0]["topic"] == "jobs"
    assert pub.jobs.pending == 0
    assert pub.tick() and not pub.diagnostics
    assert_wire_order(transport.frames)


@pytest.mark.parametrize("outcome", ["busy", "error", "pending"])
def test_jobs_reservation_submission_failure_and_pending(outcome):
    class Unreliable(Transport):
        def send(self, frame):
            if self.outcome == "busy":
                return "busy"
            if self.outcome == "error":
                raise OSError("injected submission failure")
            super().send(frame)
            self.pending = self.outcome == "pending"
            return "pending" if self.pending else "complete"
    state, transport, recorder = PublicationState("epoch"), Unreliable(), job_recorder(3)
    transport.outcome = outcome
    pub = Publisher(state, transport, jobs=recorder)
    submitted = pub.tick()
    assert submitted == (outcome == "pending")
    assert recorder.pending == (0 if submitted else 3)
    if submitted:
        assert not pub.tick() and len(transport.frames) == 1
    else:
        prepared = recorder.prepare_for_send(1)
        assert len(prepared) == 3  # No abandoned inflight reservation.
        recorder.abort_send()
    transport.outcome, transport.pending = "complete", False
    pub.tick()
    assert recorder.pending == 0
    assert len(transport.frames) == 1
    assert_wire_order(transport.frames)


@pytest.mark.parametrize("field", ["intent", "cleanup", "readiness", "stale", "core"])
def test_stopped_semantic_dedup_keeps_business_changes(field):
    runtime, state = Runtime(), PublicationState("epoch")
    runtime.service["runtime"]["startup_phase"] = "stopped"
    state.subscribe(["service.state"])
    worker = SamplingWorker(state, runtime)
    worker.sample()
    state.ack(state.pending())
    for _ in range(3):
        runtime.service["freshness"]["sampled_at_ms"] += 1000
        runtime.service["freshness"]["snapshot_revision"] += 1
        runtime.service["freshness"]["uptime_s"] += 1
        worker.sample()
        assert state.pending() == []
    if field == "intent":
        runtime.service["intent"]["runtime_generation"] += 1
    elif field == "cleanup":
        runtime.service["runtime"]["shutdown_cleanup_pending"] = True
    elif field == "readiness":
        runtime.service["readiness"]["validation_active"] = True
    elif field == "stale":
        runtime.service["freshness"]["stale"] = True
    else:
        runtime.service["core"]["core_generation"] += 1
    worker.sample()
    assert len(state.pending()) == 1
    assert state.pending()[0]["data"] == runtime.service


def test_running_worker_keeps_sample_heartbeat():
    runtime, state = Runtime(), PublicationState("epoch")
    state.subscribe(["service.state"])
    worker = SamplingWorker(state, runtime)
    worker.sample()
    state.ack(state.pending())
    worker.sample()
    assert len(state.pending()) == 1


@pytest.mark.parametrize("replacement", [False, True])
def test_control_barrier_busy_timestamps_replayed_edges_and_baseline(monkeypatch, replacement):
    state, transport = PublicationState("epoch"), Transport()
    pub = Publisher(state, transport)
    state.subscribe(["core.info"])
    old = dto("core.info")
    state.record_edge("core.info", old)
    latest = {**old, "generation": old["generation"] + 1}
    state.record_state("core.info", latest)
    stamp = [100]
    monkeypatch.setattr("bridge_publish._ms", lambda: stamp[0])
    preview = (pub.snapshot_response(snapshot_request()) if replacement else
               pub.subscription_frames(subscribe_request()))
    assert "sent_at_ms" not in (preview if replacement else preview[0])
    transport.pending = True
    stamp[0] = 200
    assert not pub.tick() and not transport.frames
    transport.pending = False
    for _ in range(4):
        pub.tick()
    assert all(frame["sent_at_ms"] == 200 for frame in transport.frames)
    emitted = events(transport.frames)
    # T-P2-H2 (33-#12): the control snapshot is the latest projection; the
    # older queued edge it covers is dropped, never replayed after it.
    assert [item["kind"] for item in emitted] == ["snapshot"]
    assert emitted[-1]["data"] == latest
    assert state.depth == 0
    assert_wire_order(transport.frames)


def test_first_sample_barrier_and_multiple_requests():
    state, transport = PublicationState("epoch"), Transport()
    pub = Publisher(state, transport)
    pub.subscription_frames(subscribe_request())
    assert pub.tick() and transport.frames[-1]["t"] == "subscribed"
    assert not pub.tick()  # First snapshot unavailable; no incrementals escape.
    state.record_edge("core.info", dto("core.info"))
    pub.snapshot_response(snapshot_request())
    assert pub.tick() and transport.frames[-1]["t"] == "evt"
    assert pub.tick() and transport.frames[-1]["t"] == "snapshot_res"
    # T-P2-H2 (33-#12): the edge is covered by both snapshots; nothing follows.
    assert not pub.tick() and state.depth == 0
    assert_wire_order(transport.frames)


def test_control_busy_and_exception_retry_without_duplicate_revision():
    class Busy(Transport):
        attempt = 0
        def send(self, frame):
            self.attempt += 1
            if self.attempt == 1:
                return "busy"
            if self.attempt == 2:
                raise OSError("retry")
            return super().send(frame)
    state, transport = PublicationState("epoch"), Busy()
    state.record_state("core.info", dto("core.info"))
    pub = Publisher(state, transport)
    pub.subscription_frames(subscribe_request())
    assert not pub.tick() and not pub.tick()
    assert pub.tick() and pub.tick()
    assert [frame["t"] for frame in transport.frames] == ["subscribed", "evt"]
    assert events(transport.frames)[0]["rev"] == 1


def test_first_history_sample_is_waited_for_once():
    state, transport = PublicationState("epoch"), Transport()
    pub = Publisher(state, transport)
    pub.subscription_frames(subscribe_request(["traffic.history"]))
    pub.subscription_frames(subscribe_request(["traffic.history"], 10))
    assert pub.tick() and pub.tick()  # Independent replies can pass a missing DTO.
    assert not pub.tick()
    assert [frame["t"] for frame in transport.frames] == ["subscribed", "subscribed"]
    state.record_state("traffic.history", dto("traffic.history"))
    assert pub.tick()
    assert not pub.tick()  # The epoch still delivers only one history snapshot.
    pub.subscription_frames(subscribe_request(["traffic.history"], 9))
    assert pub.tick() and not pub.tick()
    assert len(events(transport.frames)) == 1


def test_missing_replacement_is_explicit_error():
    state, transport = PublicationState("epoch"), Transport()
    state.subscribe(["core.info"])
    pub = Publisher(state, transport)
    pub.snapshot_response(snapshot_request())
    assert pub.tick()
    assert transport.frames[0]["error"]["code"] == "snapshot_required"


def test_pre_subscription_logs_and_unsubscribed_topic_isolation():
    state, transport = PublicationState("epoch", edge_capacity=1), Transport()
    state.record_edge("logs.batch", dto("logs.batch"))
    for _ in range(5):
        state.record_edge("core.info", dto("core.info"))
    assert state.dropped == 0 and len(state._edges) == 1
    pub = Publisher(state, transport)
    pub.subscription_frames(subscribe_request(["logs.batch"]))
    assert pub.tick() and pub.tick()
    assert events(transport.frames)[0]["topic"] == "logs.batch"
    assert_wire_order(transport.frames)


def test_cross_topic_overflow_is_bounded_with_exact_gap_accounting():
    topics = ["core.info", "service.state", "route.published", "tiers.summary", "logs.batch"]
    state, transport = PublicationState("epoch", edge_capacity=1), Transport()
    state.subscribe(topics)
    for _ in range(4):
        for topic in topics:
            state.record_edge(topic, dto(topic))
            assert len(state._edges) <= 1
            assert len(state._gaps) <= len(topics)
            assert state.depth <= 1 + len(topics) + len(protocol.P1_SNAPSHOT_TOPICS)
    assert state.dropped == 19
    assert state.log_records_dropped == 3
    pub = Publisher(state, transport)
    assert pub.tick()
    assert_wire_order(transport.frames)
    assert state.depth == 0


@pytest.mark.parametrize("hard", [False, True])
def test_oversized_log_gap_and_job_terminal_progress(hard):
    state, transport = PublicationState("epoch"), Transport()
    state.subscribe(["logs.batch", "core.info"])
    log = dto("logs.batch")
    log["records"][0]["raw"] = "x" * (protocol.HARD_FRAME_LIMIT if hard else 8000)
    state.record_edge("logs.batch", log)
    state.record_edge("core.info", dto("core.info"))
    pub = Publisher(state, transport, jobs=job_recorder(), max_bytes=4096)
    for _ in range(4):
        pub.tick()
    assert pub.jobs.pending == 0
    assert state.log_records_dropped == 1
    assert state.dropped == 1
    assert any(item["kind"] == "gap" for item in events(transport.frames))
    assert_wire_order(transport.frames)
    assert state.depth == 0


def test_indivisible_critical_above_regular_budget_is_retained():
    state, transport = PublicationState("epoch"), Transport()
    state.subscribe(["service.state"])
    service = dto("service.state")
    service["readiness"]["reason"] = "x" * 8000
    state.record_edge("service.state", service)
    pub = Publisher(state, transport, max_bytes=4096)
    assert pub.tick()
    assert pub.oversize_critical_frames == 1
    assert len(protocol.encode_frame(transport.frames[0])) > 4096
    assert events(transport.frames)[0]["data"] == service


def test_hard_oversized_critical_is_rejected_before_acceptance():
    state = PublicationState("epoch")
    service = dto("service.state")
    service["readiness"]["reason"] = "x" * protocol.HARD_FRAME_LIMIT
    with pytest.raises(ValueError, match="frame_too_large"):
        state.record_edge("service.state", service)
    with pytest.raises(ValueError, match="frame_too_large"):
        state.record_state("service.state", service)
    assert state.depth == 0 and state._seqs == {}


def test_small_budget_splits_jobs_prefix_and_preserves_fifo():
    state, transport = PublicationState("epoch"), Transport()
    recorder = job_recorder(24)
    pub = Publisher(state, transport, jobs=recorder, max_bytes=4096)
    for _ in range(24):
        pub.tick()
    assert recorder.pending == 0
    assert len(transport.frames) > 1
    assert all(len(protocol.encode_frame(frame)) <= 4096 for frame in transport.frames)
    assert [item["seq"] for item in events(transport.frames)] == list(range(1, 25))
    assert not pub.diagnostics


@pytest.mark.parametrize("source", ["service.state", "route.published", "core.info", "tiers.summary"])
def test_worker_source_failure_recovers_and_does_not_stop_other_sources(source):
    state, runtime = PublicationState("epoch"), Runtime()
    state.subscribe(["service.state", "route.published", "core.info", "tiers.summary"])
    names = {"service.state": "lifecycle_status", "route.published": "published_route_status",
             "core.info": "core_fact", "tiers.summary": "tiers_summary_status"}
    fail = [True]
    action = getattr(runtime, names[source])
    def maybe():
        if fail[0]:
            raise OSError("injected")
        return action()
    setattr(runtime, names[source], maybe)
    worker = SamplingWorker(state, RuntimeAccess(runtime))
    worker.sample()
    assert worker.source_costs[source]["failures"] == 1
    assert all(worker.source_costs[other]["successes"] == 1
               for other in worker.source_costs if other != source)
    fail[0] = False
    worker.sample()
    assert worker.source_costs[source]["successes"] == 1
    assert {item["topic"] for item in state.pending()} == set(names)
    assert worker.diagnostics[0]["source"] == source
    assert runtime.forbidden == []


@pytest.mark.parametrize("wrapped", [False, True])
def test_all_four_topics_pass_through_real_access_layer(monkeypatch, wrapped):
    state, runtime, transport = PublicationState("epoch"), Runtime(), Transport()
    access_calls = {name: 0 for name in runtime.calls}
    for name in access_calls:
        action = getattr(RuntimeAccess, name)
        def counted(self, _name=name, _action=action):
            access_calls[_name] += 1
            return _action(self)
        monkeypatch.setattr(RuntimeAccess, name, counted)
    state.subscribe(["service.state", "route.published", "tiers.summary", "core.info"])
    worker = SamplingWorker(state, RuntimeAccess(runtime) if wrapped else runtime)
    pub = Publisher(state, transport)
    for _ in range(6):  # Also crosses the removed 5-second refresh cadence.
        worker.sample()
        pub.tick()
    assert access_calls == runtime.calls == dict.fromkeys(access_calls, 6)
    assert not runtime.forbidden and not worker.diagnostics and not pub.diagnostics
    emitted = {item["topic"]: item["data"] for item in events(transport.frames)}
    assert emitted["service.state"] == runtime.service
    assert emitted["route.published"] == runtime.route
    assert emitted["tiers.summary"] == runtime.summary[1]
    assert emitted["core.info"] == {"running": False, "pid": None, "version": None,
                                     "generation": 0, "cleanup_pending": False}
    assert_wire_order(transport.frames)


@pytest.mark.parametrize("field", ["carrier", "offline_reason", "order", "degraded", "fallback",
                                  "enabled", "total", "usable", "pool"])
def test_tiers_dedup_compares_business_semantics(field):
    state, runtime = PublicationState("epoch"), Runtime()
    state.subscribe(["tiers.summary"])
    worker = SamplingWorker(state, runtime)
    worker.sample()
    initial = state.snapshot("tiers.summary")
    state.ack(state.pending())
    # Source revision churn alone must not advance publication rev.
    runtime.summary = (2, deepcopy(runtime.summary[1]))
    worker.sample()
    assert state.pending() == []
    assert state.snapshot("tiers.summary")["rev"] == initial["rev"]
    summary = runtime.summary[1]
    if field == "carrier":
        summary[field] = "sentinel"
    elif field == "offline_reason":
        summary[field] = "route_owner_changed"
    elif field == "order":
        summary[field].reverse()
    elif field in {"degraded", "fallback"}:
        summary[field] = True
    elif field == "pool":
        summary["pool"]["total"] += 1
    elif field == "enabled":
        summary["tiers"]["gateway"][field] = True
    else:
        summary["tiers"]["vanguard"][field] += 1
    worker.sample()  # DTO change is retained even if a source revision is unchanged.
    assert state.snapshot("tiers.summary")["rev"] == initial["rev"] + 1
    assert state.pending()[0]["data"] == summary
    state.ack(state.pending())
    worker.sample()
    assert state.pending() == []


@pytest.mark.parametrize("skip_at_stop", [False, True])
def test_tiers_stop_once_and_owner_change_none_retries_next_sample(skip_at_stop):
    state, runtime, transport = PublicationState("epoch"), Runtime(), Transport()
    state.subscribe(["service.state", "tiers.summary"])
    worker, pub = SamplingWorker(state, runtime), Publisher(state, transport)
    worker.sample()
    pub.tick()
    initial = state.snapshot("tiers.summary")
    summary = runtime.summary
    runtime.summary = None  # Backend rejected a cross-generation combination.
    runtime.service["freshness"]["stale"] = True
    runtime.service["intent"]["runtime_generation"] += 1
    if skip_at_stop:
        runtime.service["runtime"]["startup_phase"] = "stopped"
    worker.sample()
    assert state.snapshot("tiers.summary") == initial
    assert {item["topic"] for item in state.pending()} == {"service.state"}
    pub.tick()
    assert events(transport.frames)[-1]["data"]["freshness"]["stale"] is True
    runtime.summary = summary
    runtime.service["runtime"]["startup_phase"] = "stopped"
    worker.sample()
    assert state.snapshot("tiers.summary")["rev"] == initial["rev"] + 1
    pub.tick()
    worker.sample()
    assert state.pending() == []
    assert not worker.diagnostics and not runtime.forbidden
    assert_wire_order(transport.frames)


@pytest.mark.parametrize("source_fails", [False, True])
def test_missing_first_tiers_sample_only_blocks_its_topic(source_fails):
    state, runtime, transport = PublicationState("epoch"), Runtime(), Transport()
    runtime.summary = None
    if source_fails:
        def unavailable():
            raise OSError("unavailable")
        runtime.tiers_summary_status = unavailable
    worker = SamplingWorker(state, runtime)
    worker.sample()
    pub = Publisher(state, transport, jobs=job_recorder())
    # Missing tiers comes first; service/core baselines must still be delivered.
    pub.subscription_frames(subscribe_request(["tiers.summary", "service.state", "core.info"]))
    for _ in range(8):
        pub.tick()
    assert pub.jobs.pending == 0
    emitted = events(transport.frames)
    assert {item["topic"] for item in emitted} == {"jobs", "service.state", "core.info"}
    assert len(pub._controls) == 1  # Preserve the missing topic's initial barrier.
    # A later response and an established topic heartbeat are not held hostage.
    pub.snapshot_response(snapshot_request())
    assert pub.tick() and transport.frames[-1]["t"] == "snapshot_res"
    worker.sample()
    assert pub.tick()
    assert events(transport.frames[-1:])[0]["topic"] == "service.state"
    # Recover through the next ordinary sample, then send its complete baseline.
    if source_fails:
        del runtime.tiers_summary_status
    runtime.summary = (1, dto("tiers.summary"))
    worker.sample()
    assert pub.tick()
    assert transport.frames[-1]["topic"] == "tiers.summary"
    assert transport.frames[-1]["kind"] == "snapshot"
    assert not pub._controls and not pub.diagnostics and not runtime.forbidden
    pub.tick()
    assert_wire_order(transport.frames)


@pytest.mark.parametrize("reason", ["route_owner_changed", "core_owner_changed", "snapshot_expired"])
def test_source_reported_route_invalidity_same_revision_is_forwarded_without_refresh(reason):
    # This tests forwarding the source's expiry signal, not a runtime age/TTL rule.
    state, runtime, transport = PublicationState("epoch"), Runtime(), Transport()
    state.subscribe(["route.published"])
    worker, pub = SamplingWorker(state, runtime), Publisher(state, transport)
    worker.sample()
    pub.tick()
    initial = deepcopy(runtime.route)
    runtime.route.update(valid=False, invalid_reason=reason)
    worker.sample()
    pub.tick()
    assert state.snapshot("route.published")["rev"] == 2
    assert runtime.route["owner"] == initial["owner"]
    assert runtime.route["publication_revision"] == initial["publication_revision"]
    assert events(transport.frames)[-1]["data"] == runtime.route
    worker.sample()
    assert state.pending() == []
    runtime.route.update(valid=True, invalid_reason=None)
    worker.sample()
    pub.tick()
    assert state.snapshot("route.published")["rev"] == 3
    assert not worker.diagnostics and not runtime.forbidden
    assert_wire_order(transport.frames)


class PublishedRouteRuntime(Runtime):
    """Use the actual RT-4 getter on isolated immutable route/Core facts."""

    def __init__(self):
        from runtime_egress_recovery import (
            CoreLoadedGroupState, CoreLoadedRouteSnapshot, RuntimeNodeRef,
        )

        super().__init__()
        self.core.update(running=True, pid=42, process_generation=3, core_generation=8)
        self._runtime_route_snapshot_lock = threading.Lock()
        self._runtime_route_epoch = 7
        self._runtime_network_context_epoch = 2
        self._runtime_confirmed_fail_closed_snapshot_key = None
        self._runtime_route_snapshot = CoreLoadedRouteSnapshot(
            process_pid=42, process_generation=3, core_generation=8,
            semantic_digest="a" * 64, route_epoch=7, network_context_epoch=2,
            group_states=(CoreLoadedGroupState(
                name="FINAL-EXIT", type_name="Selector", members=("node-a",), now="node-a"),),
            proxy_nodes=(RuntimeNodeRef(
                tier="tier1", pool_kind="user", identity="a", proxy_name="node-a"),),
            loaded_at_monotonic=100.0,
        )

    def published_route_status(self):
        from desktop_runtime import DesktopRuntime

        self.calls["published_route_status"] += 1
        return DesktopRuntime.published_route_status(self)

    def _runtime_route_snapshot_state(self):
        pytest.fail("getter must not deep-copy the controller payload")


@pytest.mark.parametrize("domain", ["route", "network", "core"])
def test_real_route_getter_invalidity_reaches_wire_without_refresh(monkeypatch, domain):
    runtime, state, transport = PublishedRouteRuntime(), PublicationState("epoch"), Transport()
    monkeypatch.setattr("desktop_runtime.time.monotonic", lambda: 101.0)
    state.subscribe(["route.published"])
    worker = SamplingWorker(state, RuntimeAccess(runtime))
    pub = Publisher(state, transport)
    worker.sample()
    assert pub.tick()
    initial = events(transport.frames)[-1]
    assert initial["data"]["valid"] is True
    if domain == "route":
        runtime._runtime_route_epoch += 1
    elif domain == "network":
        runtime._runtime_network_context_epoch += 1
    else:
        runtime.core["core_generation"] += 1
    worker.sample()
    assert pub.tick()
    stale = events(transport.frames)[-1]
    assert stale["data"]["valid"] is False
    assert stale["data"]["invalid_reason"] == (
        "core_owner_changed" if domain == "core" else "route_owner_changed")
    assert stale["data"]["owner"] == initial["data"]["owner"]
    assert stale["data"]["publication_revision"] > initial["data"]["publication_revision"]
    assert stale["rev"] > initial["rev"]
    worker.sample()
    assert not pub.tick()  # Repeated invalidity is a semantic duplicate.
    assert runtime.calls["published_route_status"] == 3
    assert not worker.diagnostics and not pub.diagnostics and not runtime.forbidden
    assert_wire_order(transport.frames)


# Lead ruling (PROTOCOL.md §4 table, plan §0 item 17): route.published does not age by silence.
# While the published snapshot's six-part owner key still matches the runtime, it is the route the
# runtime itself believes; age alone never invalidates it and never triggers a refresh. Freshness is
# shown from observed_at_ms. Invalidation comes only from owner/epoch mismatch or invalid runtime facts
# (covered by the invalidation test above).
def test_real_route_getter_does_not_age_out_by_silence_and_never_refreshes(monkeypatch):
    runtime, state, transport = PublishedRouteRuntime(), PublicationState("epoch"), Transport()
    now = [101.0]
    monkeypatch.setattr("desktop_runtime.time.monotonic", lambda: now[0])
    monkeypatch.setattr("desktop_runtime.time.time", lambda: 1_800_000_000.0 + now[0])
    state.subscribe(["route.published"])
    worker = SamplingWorker(state, RuntimeAccess(runtime))
    pub = Publisher(state, transport)
    worker.sample()
    assert pub.tick()
    initial = events(transport.frames)[-1]
    assert initial["data"]["valid"] is True
    assert initial["data"]["invalid_reason"] is None
    now[0] = runtime._runtime_route_snapshot.loaded_at_monotonic + 999900.0
    worker.sample()
    sent = pub.tick()
    aged = state.snapshot("route.published")
    assert runtime.calls["published_route_status"] == 2
    assert aged["data"]["owner"] == initial["data"]["owner"]
    assert not worker.diagnostics and not pub.diagnostics and not runtime.forbidden
    assert_wire_order(transport.frames)
    # Owner still matches: the snapshot stays valid, is a semantic duplicate (no new wire rev), and the
    # age is carried by observed_at_ms rather than by a synthetic expiry.
    assert aged["data"]["valid"] is True
    assert aged["data"]["invalid_reason"] is None
    assert aged["data"]["publication_revision"] == initial["data"]["publication_revision"]
    assert aged["data"]["observed_at_ms"] == initial["data"]["observed_at_ms"]
    assert aged["rev"] == initial["rev"] and not sent


def test_samples_keep_source_generation_facts_and_do_not_splice_domains():
    state, runtime, transport = PublicationState("epoch"), Runtime(), Transport()
    state.subscribe(["service.state", "route.published", "core.info", "tiers.summary"])
    worker, pub = SamplingWorker(state, runtime), Publisher(state, transport)
    worker.sample()
    pub.tick()
    runtime.service["intent"]["runtime_generation"] += 1
    runtime.service["core"]["core_generation"] += 1
    runtime.service["freshness"]["stale"] = True
    runtime.route["valid"] = False
    runtime.route["invalid_reason"] = "core_owner_changed"
    runtime.core.update(running=True, pid=7655, core_generation=9)
    runtime.summary = None
    worker.sample()
    pub.tick()
    emitted = {item["topic"]: item["data"] for item in events(transport.frames[-1:])}
    assert emitted["service.state"] == runtime.service
    assert emitted["route.published"] == runtime.route  # Keeps the old route owner.
    assert emitted["core.info"]["generation"] == 9
    assert "tiers.summary" not in emitted
    assert not worker.diagnostics and not runtime.forbidden
    assert_wire_order(transport.frames)


def test_route_observation_clock_projection_is_not_a_business_change():
    state, runtime = PublicationState("epoch"), Runtime()
    state.subscribe(["route.published"])
    worker = SamplingWorker(state, runtime)
    worker.sample()
    state.ack(state.pending())
    runtime.route["observed_at_ms"] += 1
    worker.sample()
    assert state.pending() == []
    runtime.route["owner"]["core_generation"] += 1
    worker.sample()
    assert state.pending()[0]["data"] == runtime.route


@pytest.mark.skipif(os.name != "nt", reason="Windows transport implementation")
def test_pipe_transport_actual_send_pending_busy_and_failure(monkeypatch):
    import ctypes
    import bridge_transport as pipe
    payloads, status = [], ["pending"]
    def write(_handle, buffer, size, count, _op):
        if status[0] == "error":
            ctypes.set_last_error(5)
            return False
        payloads.append(ctypes.string_at(buffer, size))
        if status[0] == "pending":
            ctypes.set_last_error(pipe.ERROR_IO_PENDING)
            return False
        ctypes.cast(count, ctypes.POINTER(pipe.wintypes.DWORD))[0] = size
        return True
    def finish(_handle, _op, count, _wait):
        if status[0] == "pending":
            ctypes.set_last_error(pipe.ERROR_IO_INCOMPLETE)
            return False
        ctypes.cast(count, ctypes.POINTER(pipe.wintypes.DWORD))[0] = transport._write_expected
        return True
    monkeypatch.setattr(pipe._blocking, "WriteFile", write)
    monkeypatch.setattr(pipe._immediate, "GetOverlappedResult", finish)
    transport = object.__new__(pipe.PipeTransport)
    transport.handle = 1
    transport._closed, transport._disconnected = threading.Event(), threading.Event()
    transport._write_lock = threading.Lock()
    transport._write_pending = False
    transport._write_buffer = None
    transport._write_op = pipe.OVERLAPPED()
    transport._write_event = 0
    state, recorder = PublicationState("epoch"), job_recorder()
    state.subscribe(["core.info"])
    state.record_state("core.info", dto("core.info"))
    pub = Publisher(state, transport, jobs=recorder)
    assert pub.tick() and pub.pending_write
    assert recorder.pending == 0 and state.depth == 0
    state.record_state("core.info", dto("core.info"))
    assert not pub.tick() and len(payloads) == 1
    status[0] = "complete"
    assert pub.tick() and len(payloads) == 2
    recorder = job_recorder()
    recorder._events[0]["seq"] = recorder._events[0]["rev"] = 2
    pub.jobs = recorder
    status[0] = "error"
    assert not pub.tick() and recorder.pending == 1
    assert len(recorder.prepare_for_send(1)) == 1
    recorder.abort_send()
    status[0] = "complete"
    assert pub.tick() and recorder.pending == 0
    assert_wire_order([protocol.decode_line(payload) for payload in payloads])


def test_codec_failure_retains_jobs_and_other_topics_progress():
    recorder, state, transport = job_recorder(), PublicationState("epoch"), Transport()
    recorder._events[0]["data"]["op"] = "x" * protocol.HARD_FRAME_LIMIT
    assert protocol.validate_frame(recorder._events[0]) == []
    state.subscribe(["core.info"])
    state.record_state("core.info", dto("core.info"))
    pub = Publisher(state, transport, jobs=recorder)
    assert pub.tick()  # Invalid job is diagnosed, retained, and cannot block core.
    assert recorder.pending == 1
    assert all(item["topic"] == "core.info" for item in events(transport.frames))
    assert {"source": "jobs", "error": "frame_too_large"} in pub.diagnostics
    assert recorder.prepare_for_send(1)
    recorder.abort_send()


def test_publisher_thread_survives_transient_send_exception():
    sent = threading.Event()
    class FailOnce(Transport):
        attempts = 0
        def send(self, frame):
            self.attempts += 1
            if self.attempts == 1:
                raise OSError("transient")
            result = super().send(frame)
            sent.set()
            return result
    state, transport, recorder = PublicationState("epoch"), FailOnce(), job_recorder()
    pub = Publisher(state, transport, jobs=recorder, period=0.1)
    pub.start()
    try:
        assert sent.wait(3)
        assert pub._thread.is_alive() and recorder.pending == 0
    finally:
        pub.stop()
    assert not pub._thread.is_alive()


@pytest.mark.parametrize("topic", ["core.info", "logs.batch"])
@pytest.mark.parametrize("initial_gap", [False, True])
def test_producer_overflow_during_submission_has_no_duplicate_gap(topic, initial_gap):
    state = PublicationState("epoch", edge_capacity=2)
    state.subscribe([topic])
    class OverflowDuringSend(Transport):
        overflow = True
        def send(self, frame):
            if self.overflow:
                self.overflow = False
                for _ in range(3):
                    state.record_edge(topic, dto(topic))
            return super().send(frame)
    transport = OverflowDuringSend()
    count = 4 if initial_gap else 2
    for _ in range(count):
        state.record_edge(topic, dto(topic))
    pub = Publisher(state, transport)
    for _ in range(5):
        pub.tick()
    assert_wire_order(transport.frames)
    submitted_edges = [item for item in events(transport.frames) if item["kind"] == "edge"]
    assert state.dropped == count + 3 - len(submitted_edges)
    assert state.log_records_dropped == (state.dropped if topic == "logs.batch" else 0)
    assert state.depth == 0
    assert state.settled_log_records_dropped == state.log_records_dropped


def test_extended_gap_defers_settlement_until_ordered_current_gap_submission():
    state = PublicationState("epoch", edge_capacity=1)
    state.subscribe(["logs.batch"])
    row_count = len(dto("logs.batch")["records"])
    for _ in range(2):
        state.record_edge("logs.batch", dto("logs.batch"))
    old_gap = state.pending()[0]
    for _ in range(2):
        state.record_edge("logs.batch", dto("logs.batch"))
    assert state.log_records_dropped == 4 * row_count
    assert state.settled_log_records_dropped == 0
    # The copied gap covers only the older prefix, not the new overflow.
    state.ack([old_gap])
    assert state.settled_log_records_dropped == 0
    current = state.pending()[0]
    assert current["kind"] == "gap" and current["data"]["from_seq"] > old_gap["seq"]
    state.ack([current])
    assert state.settled_log_records_dropped == 4 * row_count
    state.ack([old_gap, current])  # Duplicate acknowledgements cannot add loss.
    assert state.settled_log_records_dropped == 4 * row_count


@pytest.mark.skipif(os.environ.get("BRIDGE_SLOW") != "1", reason="set BRIDGE_SLOW=1")
def test_cpu_budget_30_seconds():
    class TimedRuntime(Runtime):
        def tiers_summary_status(self):
            # P1-12a measured 1.7383 ms for 800 nodes. Simulate a conservative
            # 2 ms CPU-bound projection, without Tier3 or competing busy threads.
            until = time.perf_counter_ns() + 2_000_000
            while time.perf_counter_ns() < until:
                pass
            return super().tiers_summary_status()

    state, transport, runtime = PublicationState("cpu"), Transport(), TimedRuntime()
    state.subscribe(["service.state", "route.published", "core.info", "tiers.summary"])
    pub = Publisher(state, transport, jobs=JobEventRecorder())
    worker = SamplingWorker(state, RuntimeAccess(runtime))
    pub.start()
    worker.start()
    elapsed_start = time.monotonic()
    try:
        time.sleep(30)
        assert worker._thread.is_alive() and pub._thread.is_alive()
    finally:
        worker.stop()
        pub.stop()
    elapsed = time.monotonic() - elapsed_start
    publisher_cores = pub.publisher_thread_cpu_ns / (elapsed * 1e9)
    worker_cores = worker.worker_thread_cpu_ns / (elapsed * 1e9)
    print(f"elapsed_s={elapsed:.6f} publisher_cpu_ns={pub.publisher_thread_cpu_ns} "
          f"worker_cpu_ns={worker.worker_thread_cpu_ns} "
          f"publisher_cores={publisher_cores:.8f} worker_cores={worker_cores:.8f} "
          f"combined_cores={publisher_cores + worker_cores:.8f} "
          f"ticks={pub.ticks} frames={pub.frames} samples={worker.samples} "
          f"access_calls={runtime.calls} forbidden_paths={runtime.forbidden} "
          f"state_lock_wait_ns={state.lock_wait_ns} state_lock_held_ns={state.lock_held_ns} "
          f"source_costs={worker.source_costs}")
    assert not worker._thread.is_alive() and not pub._thread.is_alive()
    assert not pub.diagnostics and not worker.diagnostics and not runtime.forbidden
    assert worker.samples >= 28 and pub.ticks >= 100 and pub.frames >= 28
    assert runtime.calls == dict.fromkeys(runtime.calls, worker.samples)
    assert_wire_order(transport.frames)
    assert publisher_cores + worker_cores <= 0.1
