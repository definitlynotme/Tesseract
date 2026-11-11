"""P2-08 isolated projection/query contracts; no Core, registry writes or probes."""

from copy import deepcopy
import json
from pathlib import Path
from threading import Event, Thread
import threading
import time
from types import SimpleNamespace

import pytest

import bridge_protocol as protocol
from bridge_jobs import JobFailure
from bridge_publish import Publisher, SamplingWorker
from bridge_queries import ReadQueries
from bridge_runtime_access import RuntimeAccess, sampling_config_identity
from bridge_state import PublicationState
from bridge_tiers import SampledTopics, lifecycle_owner
from tests.test_bridge_p2_04 import runtime  # isolated __new__ fixture, never starts an app
from tests.test_bridge_p2_03 import session, request, spin


FIXTURES = Path(__file__).resolve().parents[1] / "bridge_schema" / "fixtures"
TOPICS = ("service.collect", "protocols.mix", "network.mode", "bastion.pool", "gateway.current")


def dto(topic):
    path = FIXTURES / ("v1/valid" if topic in TOPICS else "valid")
    filename = "topic_" + (topic if topic in TOPICS else topic.replace(".", "_")) + ".json"
    return json.loads((path / filename).read_bytes())["data"]


class Access:
    def __init__(self):
        self.runtime = SimpleNamespace(config={}, config_store=object(), manager=object())
        self.fact_versions = (0, 0, 0)
        self.runtime.bridge_sampling_fact_versions = lambda: self.fact_versions
        self.lifecycle = dto("service.state")
        self.route = dto("route.published")
        self.revision = 0
        self.calls = []
        self.records = {topic: dto(topic) for topic in TOPICS}
        self.records["service.collect"]["generation"] = 4
        for row in self.records["service.collect"]["tiers"]:
            row["generation"] = 4

    def lifecycle_status(self):
        return deepcopy(self.lifecycle)

    def published_route_status(self):
        return deepcopy(self.route)

    def _read(self, topic):
        self.calls.append(topic)
        return {**deepcopy(self.records[topic]), "generation": 4, "stale": False}

    def collect_status(self):
        return self._read("service.collect")

    def network_mode(self):
        return self._read("network.mode")

    def bastion_pool(self):
        return self._read("bastion.pool")

    def protocol_mix(self, *, source="loaded"):
        return {**self._read("protocols.mix"), "source": source}

    def gateway_status(self):
        return self._read("gateway.current")

    def core_fact(self):
        return {**self.lifecycle["core"], "version": None}

    def tiers_summary_status(self):
        return None


def projection(access=None, submit=None):
    access = access or Access()
    state = PublicationState("isolated-p2-08")
    work = []
    topics = SampledTopics(state, access, submit_normal=submit or (lambda fn: work.append(fn) or True),
                          config_revision=lambda: access.revision)
    return access, state, topics, work


def observe(name, action):
    return action()


@pytest.mark.parametrize("topic", TOPICS)
def test_first_snapshot_changes_edges_and_no_poll_timestamp_churn(topic):
    access, state, topics, _ = projection()
    raw = access._read(topic)
    owner = lifecycle_owner(access.lifecycle)
    assert topics.project(topic, raw, owner)
    snapshot = state.subscribe([topic])[0]
    assert snapshot["kind"] == "snapshot"
    state.ack(state.pending())
    raw["observed_at_ms"] = raw.get("observed_at_ms", 1) + 1
    assert not topics.project(topic, raw, owner)
    assert not state.pending()
    if topic == "service.collect":
        raw["tiers"][0]["source_count"] = 2
    elif topic == "protocols.mix":
        raw["counts"] = {"trojan": 17}
    elif topic == "network.mode":
        raw["mode"] = "tun"
    elif topic == "bastion.pool":
        raw["runtime_alive"] += 1
    else:
        raw.update(selected="gateway-new", effective=None, applied=False)
    assert topics.project(topic, raw, owner)
    event = state.pending()[0]
    assert event["kind"] == "edge"
    assert protocol.validate_frame({**event, "sent_at_ms": event["produced_at_ms"]}) == []


@pytest.mark.parametrize("topic", TOPICS)
@pytest.mark.parametrize("invalid", ["stale", "generation"])
def test_stale_and_old_generation_never_publish_false_current_counts(topic, invalid):
    access, state, topics, _ = projection()
    owner = lifecycle_owner(access.lifecycle)
    raw = access._read(topic)
    topics.project(topic, raw, owner)
    state.subscribe([topic])
    state.ack(state.pending())
    raw[invalid] = True if invalid == "stale" else 3
    topics.project(topic, raw, owner)
    if topic == "service.collect":
        assert all(row["first_check"] == "unknown" and row["healthy"] is None
                   and row["required"] is None for row in state.pending()[0]["data"]["tiers"])
        state.ack(state.pending())
        assert not topics.project(topic, raw, owner)
    else:
        assert state.pending()[0]["kind"] == "gap"
        assert topics.status[topic] == "stale"
        assert state.snapshot(topic) is None


def test_missing_pool_counts_and_protocol_counts_are_unknown_not_zero():
    access, state, topics, _ = projection()
    owner = lifecycle_owner(access.lifecycle)
    for topic, field in (("bastion.pool", "runtime_alive"), ("protocols.mix", "counts")):
        raw = access._read(topic)
        raw.pop(field)
        assert not topics.project(topic, raw, owner)
        assert topics.status[topic] == "unknown"
        assert state.subscribe([topic]) == []


def test_transition_observed_only_and_reseed_after_gap_or_generation_change():
    access, state, topics, _ = projection()
    assert state.subscribe(["tier.transition"]) == []  # event-only, no invented first edge
    topics.transition(access.lifecycle, access.route)
    assert not state.pending()
    access.route["managed_tier"] = "bastion"
    topics.transition(access.lifecycle, access.route)
    event = state.pending()[0]
    assert event["data"]["from"] == "vanguard"
    assert event["data"]["authority"] == "observed"
    state.ack(state.pending())
    topics.transition(access.lifecycle, None)
    access.route["managed_tier"] = "gateway"
    topics.transition(access.lifecycle, access.route)
    assert not state.pending()
    access.lifecycle["intent"]["runtime_generation"] += 1
    access.route["managed_tier"] = "sentinel"
    topics.transition(access.lifecycle, access.route)
    assert not state.pending()
    access.lifecycle["core"]["core_generation"] += 1
    topics.transition(access.lifecycle, access.route)
    assert topics.status["tier.transition"] == "unknown"


def test_protocol_source_loaded_then_configured_and_change_projection():
    access, state, topics, _ = projection()
    topics.sample(observe)
    assert state.subscribe(["protocols.mix"])[0]["data"]["source"] == "loaded"
    access.lifecycle["core"]["running"] = False
    topics.sample(observe)
    assert state.snapshot("protocols.mix")["data"]["source"] == "configured"
    count = access.calls.count("protocols.mix")
    topics.sample(observe)
    assert access.calls.count("protocols.mix") == count
    access.revision += 1
    topics.sample(observe)
    assert access.calls.count("protocols.mix") == count + 1


def test_gateway_only_first_subscription_and_three_triggers_and_tick_is_pure():
    access, state, topics, work = projection()
    topics.sample(observe)
    assert not work and "gateway.current" not in access.calls
    state.subscribe(["gateway.current"])
    topics.sample(observe)
    topics.sample(observe)
    assert len(work) == 1 and "gateway.current" not in access.calls
    work.pop()()
    assert access.calls.count("gateway.current") == 1
    state.subscribe(["gateway.current"])  # second subscriber reuses baseline
    for _ in range(4):
        topics.sample(observe)
    assert not work
    transport = SimpleNamespace(send=lambda frame: "complete", poll_write=lambda: "complete")
    publisher = Publisher(state, transport)
    for _ in range(5):
        publisher.tick()
    assert access.calls.count("gateway.current") == 1
    for mutate in (lambda: access.route.update(publication_revision=21),
                   lambda: setattr(access, "revision", 1),
                   lambda: (access.lifecycle["core"].update(core_generation=9),
                            access.route["owner"].update(core_generation=9)),
                   lambda: access.lifecycle["intent"].update(runtime_generation=5),
                   lambda: access.lifecycle["intent"].update(lifecycle_revision=13)):
        mutate()
        topics.sample(observe)
        assert len(work) == 1
        work.pop()()
    assert access.calls.count("gateway.current") == 6


def test_gateway_concurrent_requests_coalesce_and_late_old_read_is_withheld():
    access, state, topics, work = projection()
    state.subscribe(["gateway.current"])
    threads = [Thread(target=topics.sample, args=(observe,)) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
    assert len(work) == 1
    entered, release = Event(), Event()
    original = access.gateway_status
    def blocked():
        entered.set()
        assert release.wait(2)
        return original()
    access.gateway_status = blocked
    worker = Thread(target=work.pop())
    worker.start()
    assert entered.wait(2)
    access.route["publication_revision"] += 1
    topics.sample(observe)
    topics.sample(observe)
    assert not work
    release.set()
    worker.join(2)
    assert state.snapshot("gateway.current") is None
    topics.sample(observe)
    assert len(work) == 1
    work.pop()()
    assert state.snapshot("gateway.current") is not None


def test_gateway_capacity_rejection_retries_without_reading_on_sampler():
    access, state, topics, _ = projection(submit=lambda fn: False)
    state.subscribe(["gateway.current"])
    topics.sample(observe)
    topics.sample(observe)
    assert "gateway.current" not in access.calls
    assert not topics._gateway_inflight


def check_res(command, data):
    assert protocol.validate_frame({"t": "res", "epoch": "isolated-p2-08", "id": 1,
        "owner": "page-p2-08", "req_id": "query-1", "cmd": command, "ok": True,
        "produced_at_ms": 1, "sent_at_ms": 2, "data": data}) == []


def test_queries_schema_redaction_and_hidden_config_preservation(runtime, monkeypatch):
    import bridge_runtime_access
    access = RuntimeAccess(runtime)
    queries = ReadQueries(access, revision=lambda: 12)
    hidden = deepcopy(runtime.config["client"]["egress_recovery"]["node_judgement"])
    config = queries.config_snapshot({}, None)
    app = queries.app_info({}, None)
    monkeypatch.setattr(bridge_runtime_access, "geo_status_facts", lambda access: {
        "running": False, "last_update": {"geoip": "2026-10-01T10:00:00+00:00"},
        "errors": {"mmdb": "https://private.invalid/token?secret=SecretOnly"}, "stale": False})
    geo = queries.geo_status({}, None)
    for name, data in (("config.snapshot", config), ("app.info", app), ("geo.status", geo)):
        check_res(name, data)
    assert config["rev"] == 12 and len(config["values"]) == 11  # + bastion (T-P4-C1 #1)
    serialized = json.dumps([config, app, geo])
    for secret in ("node_judgement", "must survive", "ControllerSecretOnly", "ConfiguredUuidOnly",
                   "LoadedPasswordOnly", "CustomHeaderOnly", "https://", "SecretOnly"):
        assert secret not in serialized
    assert runtime.config["client"]["egress_recovery"]["node_judgement"] == hidden
    assert config["secret_fields_set"]
    assert runtime.api_client.calls == []


def test_config_queries_reject_stale_or_revision_changed(runtime):
    access = RuntimeAccess(runtime)
    revisions = iter([1, 2])
    with pytest.raises(JobFailure, match="snapshot_required"):
        ReadQueries(access, revision=lambda: next(revisions)).config_snapshot({}, None)
    access.config_values = lambda: {"stale": True}
    with pytest.raises(JobFailure, match="snapshot_required"):
        ReadQueries(access).config_snapshot({}, None)


def test_autostart_query_uses_p2_28_projection_without_repair(monkeypatch):
    import bridge_autostart
    manager = object()
    calls = []
    expected = {"supported": False, "enabled": True, "target": "legacy_qt", "mismatch": True,
                "error": {"code": "validation", "message_key": "errors.autostart.shell_missing", "params": {}}}
    monkeypatch.setattr(bridge_autostart, "autostart_state", lambda value: calls.append(value) or expected)
    queries = ReadQueries(Access(), autostart_manager=manager)
    result = queries.autostart_state({}, None)
    check_res("autostart.state", result)
    assert calls == [manager] and result == expected


def test_geo_reads_existing_getter_and_rejects_owner_replacement(runtime):
    runtime.lifecycle_status = lambda: dto("service.state")
    runtime.geo_updater = object()
    runtime.geo_update_status = lambda: {"running": True, "last_update": {}, "errors": {}}
    queries = ReadQueries(RuntimeAccess(runtime))
    result = queries.geo_status({}, None)
    # T-P2-H2 wires C2 #11: the known branch also carries Geo.items (none yet).
    assert result == {"running": True, "updated_at_ms": None, "errors": [], "items": []}
    def replacement():
        runtime.geo_updater = object()
        return {"running": False}
    runtime.geo_update_status = replacement
    with pytest.raises(JobFailure, match="snapshot_required"):
        queries.geo_status({}, None)
    unknown = ReadQueries(Access()).geo_status({}, None)
    check_res("geo.status", unknown)
    assert unknown["updated_at_ms"] is None and unknown["errors"]


def test_real_public_getters_zero_controller_calls(runtime):
    # Every controller operation and route refresh in this fixture is forbidden.
    access = RuntimeAccess(runtime)
    state = PublicationState("isolated-p2-08")
    topics = SampledTopics(state, access, submit_normal=lambda fn: False)
    owner = (7, 11, 8, 42)
    for name, getter in (("service.collect", access.collect_status), ("network.mode", access.network_mode),
                         ("bastion.pool", access.bastion_pool), ("gateway.current", access.gateway_status)):
        topics.project(name, getter(), owner)
    for source in ("configured", "loaded"):
        topics.project("protocols.mix", access.protocol_mix(source=source), owner)
    assert runtime.api_client.calls == []
    assert runtime._node_judgement_state.calls == []


def test_query_registration_and_sampling_worker_seam():
    access, state, topics, _ = projection()
    handlers = {}
    ReadQueries(access).register(lambda name, fn: handlers.setdefault(name, fn))
    assert set(handlers) == {"app.info", "autostart.state", "config.snapshot", "geo.status"}
    sampler = SamplingWorker(state, SimpleNamespace(), sampled_topics=topics)
    # P1 source failures remain independent of the new read-only topics.
    sampler.sample()
    assert sampler.source_costs["p2.readonly"]["successes"] == 1
    assert sampler.source_costs["service.collect"]["successes"] == 1
    assert sampling_config_identity(access) == topics._gateway_token()[2]


@pytest.mark.parametrize("command", ["app.info", "config.snapshot", "autostart.state", "geo.status"])
def test_each_query_runs_once_on_normal_worker_and_has_one_res(session, runtime, monkeypatch, command):
    import bridge_autostart
    monkeypatch.setattr(bridge_autostart, "sidecar_autostart_manager", lambda: object())
    monkeypatch.setattr(bridge_autostart, "autostart_state", lambda manager: {
        "supported": True, "enabled": False, "target": "none", "mismatch": False, "error": None})
    runtime.lifecycle_status = lambda: dto("service.state")
    runtime.geo_updater = object()
    runtime.geo_update_status = lambda: {"running": False, "last_update": {}, "errors": {}}
    queries = ReadQueries(RuntimeAccess(runtime))
    called = []
    def register(name, handler):
        def execute(args, context):
            called.append((name, threading.current_thread().name))
            return handler(args, context)
        session.register_query(name, execute)
    queries.register(register)
    frame = request(session, command)
    session.dispatch(frame)
    spin(session, lambda: bool(session.transport.sent))
    for _ in range(3):
        session.pump()
    assert len(called) == len(session.transport.sent) == 1
    assert called[0][1].startswith("bridge-jobs-normal-")
    result = session.transport.sent[0]
    assert result["t"] == "res" and result["ok"] is True
    assert protocol.validate_response(frame, result) == []


def test_gateway_real_normal_worker_domain(session):
    access = Access()
    finished = Event()
    names = []
    original = access.gateway_status
    def read():
        names.append(threading.current_thread().name)
        finished.set()
        return original()
    access.gateway_status = read
    _, state, topics, _ = projection(access, session.executor.submit_normal)
    state.subscribe(["gateway.current"])
    topics.sample(observe)
    assert finished.wait(2)
    assert names == [names[0]] and names[0].startswith("bridge-jobs-normal-")


def test_source_exception_invalidates_seed_and_does_not_create_transition_gap():
    access, state, topics, _ = projection()
    topics.sample(observe)
    state.subscribe(["bastion.pool"])
    def failed():
        raise OSError("synthetic source failure")
    access.bastion_pool = failed
    diagnostics = []
    def observed(name, action):
        try:
            return action()
        except OSError:
            diagnostics.append(name)
    topics.sample(observed)
    assert diagnostics == ["bastion.pool"]
    assert state.snapshot("bastion.pool") is None
    assert topics.status["bastion.pool"] == "stale"


def test_tick_cost_single_isolated_observation(runtime):
    # One bounded measurement, no process CPU gate or real source experiment.
    access = RuntimeAccess(runtime)
    lifecycle = dto("service.state")
    lifecycle["intent"].update(runtime_generation=7, lifecycle_revision=11)
    lifecycle["core"].update(pid=42, process_generation=3)
    runtime.lifecycle_status = lambda: deepcopy(lifecycle)
    state = PublicationState("isolated-cost")
    topics = SampledTopics(state, access, submit_normal=lambda fn: False)
    topics.sample(observe)  # seed, outside the single steady observation
    started = time.perf_counter_ns()
    topics.sample(observe)
    elapsed = time.perf_counter_ns() - started
    print(f"P2-08 isolated additional sampling cost: {elapsed / 1_000_000:.6f} ms/tick (n=1)")
    assert runtime.api_client.calls == []


def test_revalidation_restores_same_dto_after_incomplete_sample():
    access, state, topics, _ = projection()
    raw, owner = access._read("bastion.pool"), lifecycle_owner(access.lifecycle)
    topics.project("bastion.pool", raw, owner)
    state.subscribe(["bastion.pool"])
    state.ack(state.pending())
    incomplete = {**raw, "runtime_alive": None}
    topics.project("bastion.pool", incomplete, owner)
    assert state.snapshot("bastion.pool") is None
    topics.project("bastion.pool", raw, owner)
    assert state.snapshot("bastion.pool")["data"]["runtime_alive"] == raw["runtime_alive"]


def test_gateway_fact_change_read_with_same_content_does_not_emit_edge():
    access, state, topics, work = projection()
    state.subscribe(["gateway.current"])
    topics.sample(observe)
    work.pop()()
    state.ack(state.pending())
    for changed in (lambda: access.route.update(publication_revision=21),
                    lambda: setattr(access, "revision", 1)):
        changed()
        topics.sample(observe)
        assert len(work) == 1
        work.pop()()
        assert all(event["kind"] != "edge" for event in state.pending())
        state.ack(state.pending())


def test_invalidated_snapshot_request_returns_snapshot_required():
    access, state, topics, _ = projection()
    topics.sample(observe)
    state.subscribe(["network.mode"])
    topics.invalidate("network.mode")
    transport = SimpleNamespace(send=lambda frame: "complete", poll_write=lambda: "complete")
    response = Publisher(state, transport).snapshot_response({"t": "snapshot_req",
        "epoch": state.epoch, "id": 1, "topic": "network.mode",
        "produced_at_ms": 1, "sent_at_ms": 2})
    assert response["error"]["code"] == "snapshot_required"


def test_gateway_same_revision_valid_invalid_restored():
    access, state, topics, work = projection()
    state.subscribe(["gateway.current"])
    topics.sample(observe)
    work.pop()()
    state.ack(state.pending())
    access.route["valid"] = False
    topics.sample(observe)
    assert state.snapshot("gateway.current") is None
    assert state.pending()[0]["kind"] == "gap"
    assert not work
    access.route["valid"] = True
    topics.sample(observe)
    topics.sample(observe)
    assert len(work) == 1
    work.pop()()
    assert state.snapshot("gateway.current") is not None
    assert access.calls.count("gateway.current") == 2
    assert [event["kind"] for event in state.pending()] == ["gap", "snapshot"]


@pytest.mark.parametrize("field", ["process_pid", "process_generation", "core_generation"])
def test_gateway_same_revision_route_owner_mismatch_and_restoration(field):
    access, state, topics, work = projection()
    state.subscribe(["gateway.current"])
    topics.sample(observe)
    work.pop()()
    access.route["owner"][field] += 1
    topics.sample(observe)
    assert state.snapshot("gateway.current") is None and not work
    access.route["owner"][field] -= 1
    topics.sample(observe)
    work.pop()()
    access.route["owner"]["network_context_epoch"] += 1
    topics.sample(observe)
    assert len(work) == 1
    work.pop()()
    assert access.calls.count("gateway.current") == 3


def test_gateway_stale_getter_recovers_with_bounded_retries():
    access, state, topics, work = projection()
    now = [0.0]
    topics.clock = lambda: now[0]
    original = access.gateway_status
    access.gateway_status = lambda: {**original(), "stale": True}
    state.subscribe(["gateway.current"])
    topics.sample(observe)
    work.pop()()
    assert topics._gateway_completed is None
    assert state.snapshot("gateway.current") is None
    access.gateway_status = original
    for _ in range(8):
        topics.sample(observe)
    assert not work
    now[0] = 5.0
    topics.sample(observe)
    topics.sample(observe)
    assert len(work) == 1
    work.pop()()
    assert state.snapshot("gateway.current") is not None
    assert access.calls.count("gateway.current") == 2


def test_gateway_lifecycle_stale_recovers_without_revision_change():
    access, state, topics, work = projection()
    state.subscribe(["gateway.current"])
    topics.sample(observe)
    work.pop()()
    access.lifecycle["freshness"]["stale"] = True
    topics.sample(observe)
    assert state.snapshot("gateway.current") is None and not work
    access.lifecycle["freshness"]["stale"] = False
    topics.sample(observe)
    work.pop()()
    assert state.snapshot("gateway.current") is not None
    assert access.calls.count("gateway.current") == 2


@pytest.mark.parametrize("index", [0, 1, 2])
def test_gateway_in_place_owner_revision_changes_coalesce_without_secret_scans(index):
    access, state, topics, work = projection()
    identities = tuple(id(value) for value in
                       (access.runtime.config, access.runtime.config_store, access.runtime.manager))
    state.subscribe(["gateway.current"])
    topics.sample(observe)
    work.pop()()
    versions = list(access.fact_versions)
    versions[index] += 1
    access.fact_versions = tuple(versions)
    threads = [Thread(target=topics.sample, args=(observe,)) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
    assert len(work) == 1
    assert access.calls.count("gateway.current") == 1
    work.pop()()
    topics.sample(observe)
    assert not work and access.calls.count("gateway.current") == 2
    assert identities == tuple(id(value) for value in
                       (access.runtime.config, access.runtime.config_store, access.runtime.manager))


@pytest.mark.parametrize("receipt", [None, (0, 0), (0, True, 0), (0, -1, 0)])
def test_gateway_missing_or_invalid_fact_owner_withholds_baseline_and_recovers(receipt):
    access, state, topics, work = projection()
    state.subscribe(["gateway.current"])
    topics.sample(observe)
    work.pop()()
    access.fact_versions = receipt
    topics.sample(observe)
    assert not work and state.snapshot("gateway.current") is None
    access.fact_versions = (0, 0, 0)
    topics.sample(observe)
    work.pop()()
    assert state.snapshot("gateway.current") is not None


def test_gateway_no_fact_owner_getter_never_publishes_current():
    access, state, topics, work = projection()
    del access.runtime.bridge_sampling_fact_versions
    state.subscribe(["gateway.current"])
    topics.sample(observe)
    assert not work and state.snapshot("gateway.current") is None
    assert "gateway.current" not in access.calls


class Receiver:
    """Contract receiver: gap keeps last display stale; only snapshot reseeds."""
    def __init__(self, topic):
        self.topic, self.value, self.stale = topic, None, True

    def receive(self, frame):
        for event in frame.get("items", [frame]):
            if event.get("topic") != self.topic:
                continue
            if event.get("kind") == "gap":
                self.stale = True
            elif event.get("kind") == "snapshot":
                self.value, self.stale = event["data"], False
            elif event.get("kind") == "edge" and not self.stale:
                self.value = event["data"]


@pytest.mark.parametrize("topic", ["protocols.mix", "network.mode", "bastion.pool", "gateway.current"])
def test_receiver_observes_same_generation_invalidation_and_snapshot_recovery(topic):
    access, state, topics, _ = projection()
    raw, owner = access._read(topic), lifecycle_owner(access.lifecycle)
    topics.project(topic, raw, owner)
    receiver = Receiver(topic)
    frames = []
    def send(frame):
        assert not protocol.validate_frame(frame)
        frames.append(deepcopy(frame))
        receiver.receive(frame)
        return "complete"
    publisher = Publisher(state, SimpleNamespace(send=send, poll_write=lambda: "complete"))
    publisher.subscription_frames({"t": "subscribe", "epoch": state.epoch,
        "id": 1, "topics": [topic], "produced_at_ms": 1, "sent_at_ms": 2})
    publisher.tick()
    publisher.tick()
    fields = [(key.rstrip("?"), key.endswith("?")) for key in protocol.V1_TOPIC_SPECS[topic][2]]
    assert receiver.value == {key: raw[key] for key, optional in fields
                              if not optional or key in raw}
    assert not receiver.stale
    topics.project(topic, {**raw, "stale": True}, owner)
    publisher.tick()
    assert receiver.stale
    count = len(frames)
    topics.project(topic, {**raw, "stale": True}, owner)
    publisher.tick()
    assert len(frames) == count
    topics.project(topic, raw, owner)
    publisher.tick()
    assert not receiver.stale


@pytest.mark.parametrize("control", ["edge", "subscribe", "snapshot_req"])
def test_backpressure_revokes_old_events_and_cached_control_snapshots(control):
    access, state, topics, _ = projection()
    topic = "bastion.pool"
    raw, owner = access._read(topic), lifecycle_owner(access.lifecycle)
    topics.project(topic, raw, owner)
    initial = state.subscribe([topic])[0]
    receiver = Receiver(topic)
    receiver.receive(initial)
    sent, busy = [], [True]
    def send(frame):
        assert protocol.validate_frame(frame) == []
        if busy[0]:
            return "busy"
        sent.append(deepcopy(frame))
        receiver.receive(frame)
        return "complete"
    publisher = Publisher(state, SimpleNamespace(send=send, poll_write=lambda: "complete"))
    if control == "edge":
        topics.project(topic, {**raw, "runtime_alive": raw["runtime_alive"] + 1}, owner)
    elif control == "subscribe":
        publisher.subscription_frames({"t": "subscribe", "epoch": state.epoch,
            "id": 2, "topics": [topic], "produced_at_ms": 1, "sent_at_ms": 2})
    else:
        publisher.snapshot_response({"t": "snapshot_req", "epoch": state.epoch,
            "id": 2, "topic": topic, "produced_at_ms": 1, "sent_at_ms": 2})
    publisher.tick()
    topics.invalidate(topic)
    busy[0] = False
    for _ in range(5):
        publisher.tick()
    assert receiver.stale
    events = [event for frame in sent for event in frame.get("items", [frame])]
    assert not any(event.get("kind") in {"snapshot", "edge"} or "snapshot" in event
                   for event in events)
    if control == "snapshot_req":
        assert sent[0]["error"]["code"] == "snapshot_required"
    topics.project(topic, raw, owner)
    publisher.tick()
    assert not receiver.stale


def test_missing_collect_facts_revoke_previous_success():
    access, state, topics, _ = projection()
    owner = lifecycle_owner(access.lifecycle)
    topics.project("service.collect", access.collect_status(), owner)
    state.subscribe(["service.collect"])
    topics.project("service.collect", None, owner)
    assert topics.status["service.collect"] == "stale"
    assert state.snapshot("service.collect") is None
    assert state.pending()[0]["kind"] == "gap"


def test_gateway_real_getter_refreshes_redaction_on_in_place_secret_version(runtime):
    access = RuntimeAccess(runtime)
    lifecycle = dto("service.state")
    lifecycle["intent"].update(runtime_generation=7, lifecycle_revision=11)
    lifecycle["core"].update(pid=42, process_generation=3)
    runtime.lifecycle_status = lambda: deepcopy(lifecycle)
    versions = [0, 0, 0]
    runtime.bridge_sampling_fact_versions = lambda: tuple(versions)
    work = []
    state = PublicationState("real-getter-isolated")
    topics = SampledTopics(state, access, submit_normal=lambda fn: work.append(fn) or True)
    state.subscribe(["gateway.current"])
    runtime.config["tiers"]["exit"]["selected"] = "LoadedPasswordOnly"
    topics.sample(observe)
    work.pop()()
    assert "LoadedPasswordOnly" not in json.dumps(state.snapshot("gateway.current"))
    # Isolated owner mutation/commit, without replacing objects.
    versions[0] += 1
    versions[2] += 1
    runtime.manager.runtime_nodes["exit"][0].password = "NewLoadedSecretOnly"
    runtime.config["tiers"]["exit"]["selected"] = "NewLoadedSecretOnly"
    topics.sample(observe)
    topics.sample(observe)
    assert len(work) == 1
    work.pop()()
    assert "NewLoadedSecretOnly" not in json.dumps(state.snapshot("gateway.current"))
    assert runtime.api_client.calls == [] and runtime._node_judgement_state.calls == []


def test_gateway_version_changes_during_read_reject_late_old_projection():
    access, state, topics, work = projection()
    state.subscribe(["gateway.current"])
    original = access.gateway_status
    def changed():
        raw = original()
        access.fact_versions = (0, 1, 0)
        return raw
    access.gateway_status = changed
    topics.sample(observe)
    work.pop()()
    assert state.snapshot("gateway.current") is None
    access.gateway_status = original
    topics.sample(observe)
    work.pop()()
    assert state.snapshot("gateway.current") is not None


def test_connect_wires_runtime_revision_pending_queries_and_readonly_subscription(session, runtime, monkeypatch):
    import logging
    import bridge_autostart
    from bridge_main import BridgeSession
    monkeypatch.setattr(bridge_autostart, "sidecar_autostart_manager", lambda: object())
    monkeypatch.setattr(bridge_autostart, "autostart_state", lambda manager: {
        "supported": True, "enabled": False, "target": "none", "mismatch": False, "error": None})
    lifecycle = dto("service.state")
    lifecycle["intent"].update(runtime_generation=7, lifecycle_revision=11)
    lifecycle["core"].update(pid=42, process_generation=3)
    runtime.lifecycle_status = lambda: deepcopy(lifecycle)
    runtime.logger = logging.getLogger("isolated-p2-08-connect")
    runtime.bridge_config_revision = lambda: 12
    pending_reads = []
    runtime.bridge_config_pending = lambda: pending_reads.append(True) or None
    runtime.bridge_sampling_fact_versions = lambda: (12, 2, 3)
    session.runtime, session.access, session.lease = runtime, RuntimeAccess(runtime), object()
    BridgeSession.connect(session, session.transport, lambda: None)
    session.sampler.sample()
    session.dispatch(session.envelope("subscribe", id=1, topics=list(TOPICS)))
    for _ in range(12):
        session.pump(force_publish=True)
        time.sleep(.005)
    events = [event for frame in session.transport.sent for event in frame.get("items", [frame])]
    assert set(TOPICS) <= {event.get("topic") for event in events if event.get("kind") == "snapshot"}
    for ident, command in enumerate(("app.info", "autostart.state", "config.snapshot", "geo.status"), 30):
        frame = request(session, command, ident)
        session.dispatch(frame)
        spin(session, lambda: any(reply.get("t") == "res" and reply["id"] == ident
                                 for reply in session.transport.sent))
        replies = [reply for reply in session.transport.sent if reply.get("t") == "res" and reply["id"] == ident]
        assert len(replies) == 1 and replies[0]["ok"]
        assert protocol.validate_response(frame, replies[0]) == []
        if command == "config.snapshot":
            assert replies[0]["data"]["rev"] == 12
    assert pending_reads == [True]
    assert runtime.api_client.calls == []
    session.logs.stop(1)


def test_query_owner_replacement_is_resolved_without_reconnecting():
    from bridge_runtime_access import config_query_owners
    access = Access()
    revision, pending = config_query_owners(access)
    assert revision() == 0 and pending() is None
    access.runtime.bridge_config_revision = lambda: 8
    access.runtime.bridge_config_pending = lambda: {"injected": True}
    assert revision() == 8 and pending() == {"injected": True}
    access.runtime.bridge_config_revision = lambda: 9
    assert revision() == 9

def test_gateway_periodic_recheck_is_bounded_and_does_not_churn_unchanged_dto():
    access, state, topics, work = projection()
    now = [0.0]
    topics.clock = lambda: now[0]
    state.subscribe(["gateway.current"])
    topics.sample(observe)
    work.pop()()
    state.ack(state.pending())
    for due in (30.0, 60.0, 90.0):
        state.subscribe(["gateway.current"])  # More subscribers share the read.
        now[0] = due - .001
        topics.sample(observe)
        assert not work
        now[0] = due
        for _ in range(8):
            topics.sample(observe)
        assert len(work) == 1
        work.pop()()
        topics.sample(observe)
        assert not work and not state.pending()
    assert access.calls.count("gateway.current") == 4


def test_gateway_periodic_recheck_without_subscription_never_reads():
    access, _, topics, work = projection()
    now = [0.0]
    topics.clock = lambda: now[0]
    for instant in (0.0, 30.0, 60.0, 300.0):
        now[0] = instant
        topics.sample(observe)
    assert not work and "gateway.current" not in access.calls


@pytest.mark.parametrize("trigger", ["publication", "config", "core", "lifecycle"])
def test_gateway_periodic_recheck_coalesces_with_fact_trigger(trigger):
    access, state, topics, work = projection()
    now = [0.0]
    topics.clock = lambda: now[0]
    state.subscribe(["gateway.current"])
    topics.sample(observe)
    work.pop()()
    now[0] = 30.0
    if trigger == "publication":
        access.route["publication_revision"] += 1
    elif trigger == "config":
        access.revision += 1
    elif trigger == "core":
        access.lifecycle["core"]["core_generation"] += 1
        access.route["owner"]["core_generation"] += 1
    else:
        access.lifecycle["intent"]["lifecycle_revision"] += 1
    threads = [Thread(target=topics.sample, args=(observe,)) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()
    assert len(work) == 1
    work.pop()()
    topics.sample(observe)
    assert not work and access.calls.count("gateway.current") == 2
    now[0] = 59.999
    topics.sample(observe)
    assert not work


@pytest.mark.parametrize("failure", ["stale", "exception"])
def test_gateway_periodic_failure_keeps_five_second_retry_limit(failure):
    access, state, topics, work = projection()
    now = [0.0]
    topics.clock = lambda: now[0]
    state.subscribe(["gateway.current"])
    topics.sample(observe)
    work.pop()()
    original = access.gateway_status
    def failed():
        raw = original()
        if failure == "exception":
            raise OSError("synthetic periodic read failure")
        return {**raw, "stale": True}
    access.gateway_status = failed
    now[0] = 30.0
    topics.sample(observe)
    read = work.pop()
    if failure == "exception":
        with pytest.raises(OSError):
            read()
    else:
        read()
    assert state.snapshot("gateway.current") is None
    access.gateway_status = original
    for instant in (30.0, 31.0, 34.999):
        now[0] = instant
        topics.sample(observe)
        assert not work
    now[0] = 35.0
    for _ in range(8):
        topics.sample(observe)
    assert len(work) == 1
    work.pop()()
    assert state.snapshot("gateway.current") is not None
    assert access.calls.count("gateway.current") == 3
    now[0] = 64.999
    topics.sample(observe)
    assert not work
    now[0] = 65.0
    topics.sample(observe)
    assert len(work) == 1


def test_gateway_periodic_real_getter_refreshes_secret_redaction_and_measures_cost(runtime):
    access = RuntimeAccess(runtime)
    lifecycle = dto("service.state")
    lifecycle["intent"].update(runtime_generation=7, lifecycle_revision=11)
    lifecycle["core"].update(pid=42, process_generation=3)
    runtime.lifecycle_status = lambda: deepcopy(lifecycle)
    runtime.bridge_sampling_fact_versions = lambda: (0, 0, 0)
    work, now, reads = [], [0.0], []
    original = access.gateway_status
    def measured():
        started = time.perf_counter_ns()
        raw = original()
        reads.append(time.perf_counter_ns() - started)
        return raw
    access.gateway_status = measured
    state = PublicationState("periodic-real-getter-isolated")
    topics = SampledTopics(state, access, submit_normal=lambda fn: work.append(fn) or True,
                          clock=lambda: now[0])
    state.subscribe(["gateway.current"])
    runtime.config["tiers"]["exit"]["selected"] = "NewLoadedSecretOnly"
    topics.sample(observe)
    work.pop()()
    assert state.snapshot("gateway.current")["data"]["selected"] == "NewLoadedSecretOnly"
    # Only the isolated loaded secret changes; all fact/version tokens stay fixed.
    runtime.manager.runtime_nodes["exit"][0].password = "NewLoadedSecretOnly"
    now[0] = 29.999
    topics.sample(observe)
    assert not work
    now[0] = 30.0
    topics.sample(observe)
    work.pop()()
    assert len(reads) == 2
    assert "NewLoadedSecretOnly" not in json.dumps(state.snapshot("gateway.current"))
    print(f"P2-08b isolated gateway getter cost: {reads[-1] / 1_000_000:.6f} ms/read (n=1)")
    assert runtime.api_client.calls == [] and runtime._node_judgement_state.calls == []
