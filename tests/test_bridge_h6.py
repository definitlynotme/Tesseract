"""T-P2-H6: running gateway.current, stopped-gateway basis, publication fixes.

Isolated: the real DesktopRuntime methods run on the P2-04 ``__new__``
fixture (no app start, Core, controller, registry or network). Test-hygiene
checks only exercise the warm-up helpers with ``subprocess.run`` replaced.
"""

from __future__ import annotations

from copy import deepcopy
import inspect
import json
import os
from pathlib import Path
import subprocess

import pytest

from bridge_publish import Publisher
from bridge_runtime_access import RuntimeAccess, sampling_config_identity, sampling_fact_versions
from bridge_state import PublicationState
from bridge_tiers import SampledTopics
from desktop_runtime import DesktopRuntime
from gateway_selection import (GatewaySelection, gateway_identity, read_selection,
                               write_bound_selection, write_unresolved_selection)
from models import Node, NodeStatus
from tests.test_bridge_p2_04 import runtime  # noqa: F401  isolated __new__ fixture
from tests.test_bridge_p2_08 import dto, observe
from tests.test_bridge_h2 import EPOCH, Wire, ticks

ROOT = Path(__file__).resolve().parents[1]


class Forbidden:
    """A lock the observed-version getter must never take."""

    def __enter__(self):
        pytest.fail("bridge_sampling_fact_versions took a manager/config lock")

    def __exit__(self, *args):
        return False


def exit_node(password, name="Gateway A"):
    return Node(name=name, protocol="trojan", tier="exit", address="example.invalid", port=443,
                password=password, source="https://example.invalid/private-sub",
                status=NodeStatus.ALIVE, latency_ms=12, sources=["https://example.invalid/private-sub"])


# ===================================== 1. DesktopRuntime.bridge_sampling_fact_versions

def test_versions_start_at_zero_and_stay_put_without_a_change(runtime):
    runtime.manager.lock = Forbidden()
    runtime._config_change_lock = Forbidden()
    first = runtime.bridge_sampling_fact_versions()
    assert first == (0, 0, 0)
    for _ in range(5):
        assert runtime.bridge_sampling_fact_versions() == first
    assert runtime.api_client.calls == [] and runtime.registry_reads == []
    # The access layer accepts the receipt and carries it in the token.
    access = RuntimeAccess(runtime)
    assert sampling_fact_versions(access) == (0, 0, 0)
    assert sampling_config_identity(access)[-1] == (0, 0, 0)


def test_config_counter_follows_object_revision_and_saved_selection(runtime):
    runtime.bridge_sampling_fact_versions()
    runtime.bridge_commit_config_revision()               # a committed bridge write
    assert runtime.bridge_sampling_fact_versions() == (1, 0, 0)
    runtime.config["tiers"]["exit"]["selected"] = "Gateway B"   # in place, no commit
    assert runtime.bridge_sampling_fact_versions() == (2, 0, 0)
    runtime.config["tiers"]["exit"]["selected_proxy"] = "Gateway B"
    assert runtime.bridge_sampling_fact_versions() == (3, 0, 0)
    runtime.config = deepcopy(runtime.config)             # same content, new owner object
    assert runtime.bridge_sampling_fact_versions() == (4, 0, 0)
    assert runtime.bridge_sampling_fact_versions() == (4, 0, 0)


def test_sampling_fact_versions_observe_the_selection_record(runtime):
    """T-GW-ID: the saved Gateway selection is a node identity record
    (``selected_node``) beside its label. A change of the record alone (the
    identity under the same label, then an unresolved record) is a config
    fact change; an unchanged record is none. No lock, no commit."""
    runtime.manager.lock = Forbidden()
    runtime._config_change_lock = Forbidden()
    block = runtime.config["tiers"]["exit"]
    picked, twin = exit_node("Gateway A"), exit_node("Rotated", name="Gateway A")
    write_bound_selection(block, "Gateway A", gateway_identity(picked))
    base = runtime.bridge_sampling_fact_versions()
    assert runtime.bridge_sampling_fact_versions() == base
    block["selected_node"]["identity"] = gateway_identity(twin)   # in place, label unchanged
    assert read_selection(block) == GatewaySelection("bound", "Gateway A", identity=gateway_identity(twin))
    assert runtime.bridge_sampling_fact_versions() == (base[0] + 1, base[1], base[2])
    write_unresolved_selection(block, "Gateway A")
    assert block["selected_proxy"] == "Gateway A" and read_selection(block).state == "unresolved"
    assert runtime.bridge_sampling_fact_versions() == (base[0] + 2, base[1], base[2])
    assert runtime.bridge_sampling_fact_versions() == (base[0] + 2, base[1], base[2])
    assert sampling_fact_versions(RuntimeAccess(runtime)) == (base[0] + 2, base[1], base[2])
    assert runtime.api_client.calls == [] and runtime.registry_reads == []


def test_sources_counter_follows_the_store_and_the_file(runtime, tmp_path):
    sources = tmp_path / "sources.yaml"
    sources.write_bytes(b"subscription_urls: []\n")
    runtime.config_store.sources_path = sources
    base = runtime.bridge_sampling_fact_versions()
    sources.write_bytes(b"subscription_urls: [https://example.invalid/new-private]\n")
    assert runtime.bridge_sampling_fact_versions() == (base[0], base[1] + 1, base[2])
    sources.unlink()                                      # unreadable: observed as None
    assert runtime.bridge_sampling_fact_versions()[1] == base[1] + 2
    assert runtime.bridge_sampling_fact_versions()[1] == base[1] + 2
    store = type(runtime.config_store)(**vars(runtime.config_store))
    runtime.config_store = store                          # rebuilt store owner
    assert runtime.bridge_sampling_fact_versions()[1] == base[1] + 3


def test_node_counter_follows_loaded_credentials_and_the_manager(runtime):
    nodes = runtime.manager.runtime_nodes
    base = runtime.bridge_sampling_fact_versions()
    nodes["exit"] = [exit_node("Gateway A")]              # how the manager reloads a tier
    assert runtime.bridge_sampling_fact_versions() == (base[0], base[1], base[2] + 1)
    nodes["exit"].append(exit_node("other", name="Gateway C"))
    assert runtime.bridge_sampling_fact_versions()[2] == base[2] + 2
    # Two credential replacements between samples: the held list cannot be
    # freed, so a new list never reuses its id and the change is still seen.
    nodes["tier2"] = [exit_node("Tier2 One", name="T2")]
    nodes["tier2"] = [exit_node("Tier2 Two", name="T2")]
    assert runtime.bridge_sampling_fact_versions()[2] == base[2] + 3
    runtime.manager = type(runtime.manager)(runtime.config)
    assert runtime.bridge_sampling_fact_versions()[2] == base[2] + 4
    # Documented limit: an in-place credential edit on a loaded node is not
    # observed (no production path does one); the 30 s recheck covers it.
    runtime.manager.runtime_nodes["exit"][0].password = "InPlaceOnly"
    assert runtime.bridge_sampling_fact_versions()[2] == base[2] + 4


def test_node_counter_ignores_swaps_with_equal_credentials(runtime):
    """修复第 1 轮 M1: the manager swaps whole tier lists on every health
    result (_replace_tested_runtime_node: copy, put a clone in, re-sort,
    assign) and Tier3 admission. Equal credentials are no fact change."""
    from models import sort_nodes_for_use
    nodes = runtime.manager.runtime_nodes
    nodes["tier3"] = [exit_node(f"pool-{index}", name=f"P{index}") for index in range(5)]
    base = runtime.bridge_sampling_fact_versions()
    for round_ in range(10):
        for tier in ("exit", "tier3"):
            current = list(nodes[tier])
            clone = current[round_ % len(current)].clone()
            clone.latency_ms = 10 + round_                  # health facts change, secrets do not
            clone.extra["tier3_tcp_prefilter_observed"] = True
            current[round_ % len(current)] = clone
            nodes[tier] = sort_nodes_for_use(list(reversed(current)))
        assert runtime.bridge_sampling_fact_versions() == base
    nodes["tier3"][0].extra["tier3_consecutive_failures"] = 3   # in-place bookkeeping
    nodes["tier3"] = list(nodes["tier3"])
    assert runtime.bridge_sampling_fact_versions() == base
    # A secret-bearing extra or a changed credential in a swapped list is seen.
    changed = list(nodes["tier3"])
    changed[0] = changed[0].clone()
    changed[0].extra["password"] = "ExtraSecretOnly"
    nodes["tier3"] = changed
    assert runtime.bridge_sampling_fact_versions()[2] == base[2] + 1
    changed = list(nodes["exit"])
    changed[0] = changed[0].clone()
    changed[0].password = "RotatedPassword"
    nodes["exit"] = changed
    assert runtime.bridge_sampling_fact_versions()[2] == base[2] + 2


def test_an_older_observation_never_overwrites_a_newer_one(runtime):
    """A sampler call that observed before a concurrent call stored must not
    write its older view back (that made one change advance up to 3 times)."""
    base = runtime.bridge_sampling_fact_versions()
    original = runtime._bridge_read_owner
    nested = []

    def owner():
        snapshot = original()
        if not nested:
            nested.append(True)
            # A newer caller sees a credential change and stores first.
            runtime.manager.runtime_nodes["exit"] = [exit_node("Rotated")]
            nested.append(runtime.bridge_sampling_fact_versions())
        return snapshot

    runtime._bridge_read_owner = owner
    older = runtime.bridge_sampling_fact_versions()
    runtime._bridge_read_owner = original
    assert nested[1][2] == base[2] + 1
    assert older == nested[1]                               # dropped, not written back
    assert runtime.bridge_sampling_fact_versions() == nested[1]


def test_node_secret_fingerprint_matches_the_redaction_inputs():
    from bridge_runtime_access import node_secret_fingerprint
    first = [exit_node("Alpha"), exit_node("Beta", name="B")]
    assert node_secret_fingerprint(first) == node_secret_fingerprint(list(reversed(first)))
    assert node_secret_fingerprint(first) == node_secret_fingerprint([n.clone() for n in first])
    bookkeeping = [n.clone() for n in first]
    bookkeeping[0].extra.update(tier3_admission_pool_revision=4, tier3_last_failure_reason="timeout")
    assert node_secret_fingerprint(bookkeeping) == node_secret_fingerprint(first)
    for field, value in (("password", "Gamma"), ("uuid", "UuidOnly"), ("uri", "trojan://x@h:1"),
                         ("source", "https://example.invalid/other"),
                         ("sources", ["https://example.invalid/other"])):
        other = [n.clone() for n in first]
        setattr(other[0], field, value)
        assert node_secret_fingerprint(other) != node_secret_fingerprint(first), field
    url = [n.clone() for n in first]
    url[0].extra["subscription"] = "https://example.invalid/extra-sub"
    assert node_secret_fingerprint(url) != node_secret_fingerprint(first)
    # Unreadable rows: a key equal to nothing (a conservative change).
    assert node_secret_fingerprint([object()]) != node_secret_fingerprint([object()])


def test_getter_is_appended_in_the_bridge_region_only():
    source = (ROOT / "desktop_runtime.py").read_bytes()
    # One line-ending style throughout (.gitattributes checks out LF everywhere).
    assert source.count(b"\r\n") in (0, source.count(b"\n"))
    text = source.decode("utf-8")
    start = text.index("    def bridge_sampling_fact_versions(self):")
    assert start > text.index("    def bridge_exit_proxy_name(self, node_id):")
    assert start < text.index("class RefreshLockBusy(RuntimeError):")
    body = inspect.getsource(DesktopRuntime.bridge_sampling_fact_versions)
    for forbidden in ("manager.lock", "_config_change_lock", "api_client", "read_sources",
                      "_refresh_runtime_route_snapshot", "deepcopy"):
        assert forbidden not in body, forbidden


# ============================================ 2. running Core: gateway.current publishes

def running(runtime):
    access = RuntimeAccess(runtime)
    lifecycle = dto("service.state")
    lifecycle["intent"].update(runtime_generation=7, lifecycle_revision=11)
    lifecycle["core"].update(pid=42, process_generation=3)
    runtime.lifecycle_status = lambda: deepcopy(lifecycle)
    work = []
    state = PublicationState("h6-running")
    topics = SampledTopics(state, access, submit_normal=lambda fn: work.append(fn) or True,
                           config_revision=runtime.bridge_config_revision)
    state.subscribe(["gateway.current"])
    return state, topics, work


def test_running_gateway_publishes_with_the_real_runtime_versions(runtime):
    """Before T-P2-H6 the real runtime had no fact versions, so this topic
    never published while Core ran (05b's Gateway card stayed empty)."""
    assert "bridge_sampling_fact_versions" not in vars(runtime)
    state, topics, work = running(runtime)
    topics.sample(observe)
    assert len(work) == 1
    work.pop()()
    snapshot = state.snapshot("gateway.current")
    assert snapshot is not None and topics.status["gateway.current"] == "current"
    assert snapshot["data"]["selected"] == "Gateway A"
    assert snapshot["data"]["effective"] == "Gateway A" and snapshot["data"]["applied"] is True
    # Unchanged facts: neither a re-read nor an edge.
    topics.sample(observe)
    assert not work
    assert runtime.api_client.calls == [] and runtime._node_judgement_state.calls == []


def test_running_gateway_rereads_at_once_when_the_loaded_secrets_change(runtime):
    state, topics, work = running(runtime)
    topics.sample(observe)
    work.pop()()
    assert state.snapshot("gateway.current")["data"]["selected"] == "Gateway A"
    # A refresh replaces the exit list; the new credential equals the name.
    runtime.manager.runtime_nodes["exit"] = [exit_node("Gateway A")]
    topics.sample(observe)
    assert state.snapshot("gateway.current") is None          # withdrawn before the re-read
    assert any(item["kind"] == "gap" for item in state.pending_for("gateway.current"))
    assert len(work) == 1
    work.pop()()
    data = state.snapshot("gateway.current")["data"]
    assert "Gateway A" not in json.dumps(data)


def test_running_gateway_keeps_its_baseline_through_health_list_swaps(runtime):
    """修复第 1 轮 M1 regression: 60 one-second ticks, each swapping exit and
    tier3 lists the way health results do (equal credentials). The baseline
    stays, no gap goes out, and bridge_gateway_status runs once plus the
    30 s rechecks, never once per tick."""
    from models import sort_nodes_for_use
    nodes = runtime.manager.runtime_nodes
    nodes["tier3"] = [exit_node(f"pool-{index}", name=f"P{index}") for index in range(5)]
    access = RuntimeAccess(runtime)
    lifecycle = dto("service.state")
    lifecycle["intent"].update(runtime_generation=7, lifecycle_revision=11)
    lifecycle["core"].update(pid=42, process_generation=3)
    runtime.lifecycle_status = lambda: deepcopy(lifecycle)
    calls = []
    original = access.gateway_status
    access.gateway_status = lambda: calls.append(True) or original()
    now = [100.0]
    work = []
    state = PublicationState("h6-swaps")
    topics = SampledTopics(state, access, submit_normal=lambda fn: work.append(fn) or True,
                           config_revision=runtime.bridge_config_revision, clock=lambda: now[0])
    state.subscribe(["gateway.current"])
    topics.sample(observe)
    work.pop()()
    assert state.snapshot("gateway.current") is not None and calls == [True]
    seen = set()
    for tick in range(60):
        now[0] += 1.0
        for tier in ("exit", "tier3"):
            current = list(nodes[tier])
            clone = current[tick % len(current)].clone()
            clone.latency_ms = 20 + tick % 7
            current[tick % len(current)] = clone
            nodes[tier] = sort_nodes_for_use(current)
        topics.sample(observe)
        assert state.snapshot("gateway.current") is not None, tick
        seen.update(item["kind"] for item in state.pending_for("gateway.current"))
        while work:
            work.pop()()
    assert "gap" not in seen
    assert 1 < len(calls) <= 1 + 60 // 30                   # first read + 30 s rechecks
    assert topics.status["gateway.current"] == "current"


# ============================================== 3. stopped Core: id(config) + revision

def stopped(runtime, *, versions):
    runtime.core_fact = lambda: {"running": False, "pid": None, "process_generation": 0,
                                 "core_generation": 8, "cleanup_pending": False}
    if not versions:
        runtime.bridge_sampling_fact_versions = None      # a runtime without the getter
    access = RuntimeAccess(runtime)
    lifecycle = dto("service.state")
    lifecycle["intent"].update(runtime_generation=7, lifecycle_revision=11)
    lifecycle["core"].update(running=False, pid=None, process_generation=0, cleanup_pending=False)
    runtime.lifecycle_status = lambda: deepcopy(lifecycle)
    work = []
    state = PublicationState("h6-stopped")
    topics = SampledTopics(state, access, submit_normal=lambda fn: work.append(fn) or True,
                           config_revision=runtime.bridge_config_revision)
    state.subscribe(["gateway.current"])
    topics.sample(observe)
    work.pop()()
    snapshot = state.snapshot("gateway.current")
    assert snapshot["data"] == {"selected": "Gateway A", "effective": None, "missing": False,
                                "applied": False, "pending": None}
    return access, state, topics, work


def published_after(state, mark):
    return [event for event in state.pending(256) if event["topic"] == "gateway.current"
            and event["kind"] in ("snapshot", "edge") and event["rev"] > mark]


@pytest.mark.parametrize("versions", [True, False])
def test_stopped_in_place_secret_with_revision_withdraws_the_old_display_name(runtime, versions):
    """决定_第六波裁定 H2-4: a committed in-place secret change (revision +1)
    withdraws the saved-intent baseline at the next sample; the old display
    name is never published again, the re-read redacts it."""
    _access, state, topics, work = stopped(runtime, versions=versions)
    mark = state.snapshot("gateway.current")["rev"]
    runtime.config["client"]["mihomo_controller"]["secret"] = "Gateway A"
    runtime.bridge_commit_config_revision()
    topics.sample(observe)
    assert state.snapshot("gateway.current") is None
    assert not published_after(state, mark)
    assert len(work) == 1
    work.pop()()
    snapshot = state.snapshot("gateway.current")
    assert snapshot is not None and snapshot["data"]["selected"] != "Gateway A"
    assert "Gateway A" not in json.dumps([event["data"] for event in published_after(state, mark)])


@pytest.mark.parametrize("versions", [True, False])
def test_stopped_config_owner_replacement_withdraws_the_baseline(runtime, versions):
    _access, state, topics, work = stopped(runtime, versions=versions)
    replacement = deepcopy(runtime.config)
    replacement["client"]["mihomo_controller"]["secret"] = "Gateway A"
    runtime.config = runtime.manager.config = replacement  # reload: new object, same revision
    topics.sample(observe)
    assert state.snapshot("gateway.current") is None
    work.pop()()
    assert state.snapshot("gateway.current")["data"]["selected"] != "Gateway A"


def test_stopped_read_straddling_a_revision_change_is_dropped(runtime):
    access, state, topics, work = stopped(runtime, versions=False)
    original = access.gateway_status

    def straddled():
        raw = original()
        runtime.config["client"]["mihomo_controller"]["secret"] = "Gateway A"
        runtime.bridge_commit_config_revision()
        return raw

    topics._gateway_recheck_at = topics._gateway_retry_at = 0.0   # periodic re-read now
    access.gateway_status = straddled
    topics.sample(observe)
    work.pop()()
    assert state.snapshot("gateway.current") is None       # stale read withheld
    access.gateway_status = original
    topics.sample(observe)
    work.pop()()
    assert state.snapshot("gateway.current")["data"]["selected"] != "Gateway A"


def test_stopped_in_place_selection_change_is_seen_by_the_real_versions(runtime):
    _access, state, topics, work = stopped(runtime, versions=True)
    runtime.config["tiers"]["exit"]["selected"] = "Gateway B"     # no revision commit
    topics.sample(observe)
    assert state.snapshot("gateway.current") is None
    work.pop()()
    assert state.snapshot("gateway.current")["data"]["selected"] == "Gateway B"


# ============================================================= 4. publication layer

def subscribed_state(capacity=128):
    state = PublicationState(EPOCH, edge_capacity=capacity)
    base = dto("service.state")
    state.record_state("service.state", base)
    state.record_state("route.published", dto("route.published"))
    state.subscribe(["service.state", "route.published"])
    return state, base


def test_pending_for_is_one_topic_of_light_receipts_without_log_accounting():
    state, base = subscribed_state(capacity=2)
    settled = []
    state._settle_log_publication_work = lambda: settled.append(True)
    state.record_edge("service.state", base)
    state.record_edge("route.published", dto("route.published"))
    receipts = state.pending_for("service.state")
    assert receipts == [{"topic": "service.state", "kind": "edge", "rev": receipts[0]["rev"],
                         "seq": 1}]
    # A gap comes first and the restored snapshot last, as in pending().
    state.record_edge("route.published", dto("route.published"))  # overflow: service.state lost
    receipts = state.pending_for("service.state")
    assert [item["kind"] for item in receipts] == ["gap", "snapshot"]
    assert receipts[0]["seq"] == 2 and "seq" not in receipts[1]
    assert all(set(item) <= {"topic", "kind", "rev", "seq"} for item in receipts)
    assert state.pending_for("traffic.tick") == []                 # not subscribed
    state.ack(receipts[:1], settle_log_cpu=False)
    assert settled == []
    state.ack([])
    assert settled == [True]


def test_drop_covered_reads_one_topic_and_never_the_whole_queue():
    state, base = subscribed_state()
    wire = Wire()
    publisher = Publisher(state, wire)
    publisher.subscription_frames({"t": "subscribe", "epoch": EPOCH, "produced_at_ms": 1,
                                   "sent_at_ms": 1, "id": 1, "topics": ["service.state"]})
    ticks(publisher, 3)
    limits, settled = [], []
    original = state.pending
    state.pending = lambda limit=128: limits.append(limit) or original(limit)
    state._settle_log_publication_work = lambda: settled.append(True)
    older = deepcopy(base)
    older["runtime"]["startup_phase"] = "collecting"
    state.record_edge("service.state", older)
    state.record_edge("service.state", base)
    publisher.snapshot_response({"t": "snapshot_req", "epoch": EPOCH, "produced_at_ms": 1,
                                 "sent_at_ms": 1, "id": 5, "topic": "service.state"})
    publisher.tick()                                   # the control tick sends snapshot_res
    assert any(frame["t"] == "snapshot_res" for frame in wire.frames)
    assert limits == [] and settled == []              # no whole-queue copy, no log settle
    assert state.pending_for("service.state") == []    # covered edges acknowledged
    assert not publisher.diagnostics


def test_rescue_that_trims_the_gap_away_returns_its_reserved_seq():
    """T-P2-H2 review: the writer rescued every edge of a gap, the gap was
    trimmed away and its reserved seq never went on the wire, so the shell's
    EventCursor saw a jump. The reserved seq is now given back."""
    state, base = subscribed_state(capacity=2)
    wire = Wire()
    publisher = Publisher(state, wire)
    publisher.subscription_frames({"t": "subscribe", "epoch": EPOCH, "produced_at_ms": 1,
                                   "sent_at_ms": 1, "id": 1,
                                   "topics": ["service.state", "route.published"]})
    ticks(publisher, 4)
    first, second, later = (deepcopy(base) for _ in range(3))
    first["runtime"]["startup_phase"] = "collecting"
    second["runtime"]["startup_phase"] = "core_standby"
    state.record_edge("service.state", first)          # seq 1
    state.record_edge("service.state", second)         # seq 2 (ring full)
    original_send = wire.send
    fired = []

    def send(frame):
        outcome = original_send(frame)
        if not fired and any(item.get("topic") == "service.state" and item.get("kind") == "edge"
                             for item in frame.get("items", ())):
            fired.append(True)
            # While the writer owns its copy: another topic overflows the
            # ring (service.state's seqs 1-2 become a gap reserving seq 3),
            # then a new service.state edge is queued (state seq 4).
            state.record_edge("route.published", dto("route.published"))
            state.record_edge("service.state", later)
        return outcome

    wire.send = send
    ticks(publisher, 6)
    assert fired and not publisher.diagnostics, publisher.diagnostics
    phases = [data["runtime"]["startup_phase"] for kind, data in wire.items("service.state")
              if kind == "edge"]
    assert phases == ["collecting", "core_standby", later["runtime"]["startup_phase"]]
    assert wire.seqs["service.state"] == 3              # 1, 2, 3: no jump over 3
    assert state.dropped == 0
    state.record_edge("service.state", base)
    ticks(publisher, 3)
    assert wire.seqs["service.state"] == 4 and not publisher.diagnostics


def test_rescue_without_later_edges_rewinds_the_counter():
    state, base = subscribed_state(capacity=2)
    state.record_edge("service.state", base)
    state.record_edge("service.state", base)
    copy = state.pending(128)                          # the writer's copy (seqs 1, 2)
    state.record_edge("route.published", dto("route.published"))   # gap 1-2, seq 3 reserved
    assert state.pending_for("service.state")[0]["kind"] == "gap"
    state.ack([item for item in copy if item["kind"] == "edge"])
    assert [item["kind"] for item in state.pending_for("service.state")] == ["snapshot"]
    assert state.record_edge("service.state", base) == 3


def test_partial_rescue_keeps_the_gap_and_its_seq():
    state, base = subscribed_state(capacity=3)
    state.record_edge("service.state", base)           # 1
    copy = state.pending(128)
    state.record_edge("service.state", base)           # 2, not in the copy
    state.record_edge("route.published", dto("route.published"))
    state.record_edge("route.published", dto("route.published"))   # overflow: gap 1-2, seq 3
    state.ack([item for item in copy if item["kind"] == "edge"])
    gap = state.pending_for("service.state")[0]
    assert gap["kind"] == "gap" and gap["seq"] == 3
    assert state.record_edge("service.state", base) == 4


def test_ack_refuses_a_data_less_logs_rescue_receipt():
    """pending_for receipts carry no data; a logs.batch rescue needs the
    writer's copy to settle record counts, so it fails loudly, untouched."""
    state = PublicationState(EPOCH, edge_capacity=2)
    state.subscribe(["logs.batch", "service.state"])
    state.record_state("service.state", dto("service.state"))
    receipt = {"topic": "logs.batch", "kind": "edge", "rev": 1, "seq": 1}
    state._gaps["logs.batch"] = {"topic": "logs.batch", "kind": "gap", "seq": 3, "rev": 1,
                                 "data": {"from_seq": 1, "to_seq": 2, "dropped": 2}}
    state.dropped = 2
    with pytest.raises(ValueError, match="logs.batch rescue"):
        state.ack([receipt], settle_log_cpu=False)
    assert state.dropped == 2


@pytest.mark.parametrize("seed", range(50))
def test_rescue_and_seq_reclaim_interleavings_keep_the_wire_continuous(seed):
    """修复第 1 轮 SHOULD: seeded interleavings of ring overflow during send,
    gap merges and whole trims, covered-edge drops (snapshot_req) and
    rescues. Wire enforces the shell's EventCursor rules on every frame."""
    import random
    rng = random.Random(seed)
    state, base = subscribed_state(capacity=rng.randint(2, 4))
    wire = Wire()
    publisher = Publisher(state, wire)
    publisher.subscription_frames({"t": "subscribe", "epoch": EPOCH, "produced_at_ms": 1,
                                   "sent_at_ms": 1, "id": 1,
                                   "topics": ["service.state", "route.published"]})
    ticks(publisher, 3)
    phases = ("collecting", "core_standby", "running")

    def produce():
        if rng.random() < 0.6:
            data = deepcopy(base)
            data["runtime"]["startup_phase"] = rng.choice(phases)
            state.record_edge("service.state", data)
        else:
            state.record_edge("route.published", dto("route.published"))

    original_send = wire.send

    def send(frame):
        outcome = original_send(frame)
        for _ in range(rng.randint(0, 5) if rng.random() < 0.5 else 0):
            produce()                                  # while the writer owns its copy
        return outcome

    wire.send = send
    request = 10
    for _ in range(40):
        roll = rng.random()
        if roll < 0.5:
            produce()
        elif roll < 0.65:
            request += 1
            publisher.snapshot_response({"t": "snapshot_req", "epoch": EPOCH, "produced_at_ms": 1,
                                         "sent_at_ms": 1, "id": request,
                                         "topic": rng.choice(["service.state", "route.published"])})
        else:
            publisher.tick()
    wire.send = original_send
    ticks(publisher, 12)
    assert not publisher.diagnostics, publisher.diagnostics
    assert all(not state.pending_for(topic) or
               [item["kind"] for item in state.pending_for(topic)] == ["snapshot"]
               for topic in ("service.state", "route.published"))


# =============================================================== 5. test hygiene

def test_integration_warmup_is_profile_isolated_and_survives_a_timeout(monkeypatch):
    import sidecar_import_graph
    import tests.test_bridge_integration as integration
    seen = []

    def run(argv, **kwargs):
        seen.append(kwargs)
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    monkeypatch.setattr(integration, "_WARM_CACHE", None)
    monkeypatch.setattr(sidecar_import_graph, "sidecar_modules", lambda: ["json"])
    monkeypatch.setattr(subprocess, "run", run)
    cache = integration.warm_bytecode_cache()
    assert cache and Path(cache).is_dir() and integration._WARM_CACHE == cache
    env = seen[0]["env"]
    for name in ("APPDATA", "LOCALAPPDATA"):
        assert env[name] != os.environ.get(name)
        assert Path(env[name]).is_dir() and "tesseract-it-warm-profile-" in env[name]
    assert env["PYTHONPYCACHEPREFIX"] == cache and "PYTHONDONTWRITEBYTECODE" not in env


def test_smoke_driver_warmup_is_profile_isolated_and_survives_a_timeout(monkeypatch, tmp_path):
    import sidecar_import_graph
    import tests.test_bridge_p2_33 as smoke
    seen = []

    def run(argv, **kwargs):
        seen.append(kwargs)
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    monkeypatch.setattr(sidecar_import_graph, "sidecar_modules", lambda: ["json"])
    monkeypatch.setattr(subprocess, "run", run)
    sandbox = {"temp": str(tmp_path), "env": {"PYTHONDONTWRITEBYTECODE": "1", "PATH": "x"}}
    assert smoke.warm_source_bytecode(sandbox) >= 0
    env = seen[0]["env"]
    assert env["APPDATA"] == env["LOCALAPPDATA"] == str(tmp_path / "warm-profile")
    assert "PYTHONDONTWRITEBYTECODE" not in env
    assert sandbox["env"]["PYTHONPYCACHEPREFIX"] == str(tmp_path / "pycache-warm")


def test_ps1_warmup_uses_the_allowlisted_environment():
    text = (ROOT / "scripts" / "p1_integration_smoke.ps1").read_text(encoding="utf-8")
    start = text.index("$WarmInfo = New-Object System.Diagnostics.ProcessStartInfo")
    block = text[start:text.index("$ChildEnv[\"PYTHONPYCACHEPREFIX\"] = $PyCache", start)]
    assert "$WarmInfo.EnvironmentVariables.Clear()" in block
    assert "foreach ($Name in $ChildEnv.Keys)" in block
    assert '$WarmInfo.EnvironmentVariables["APPDATA"] = $WarmProfile' in block
    assert '$WarmInfo.EnvironmentVariables["LOCALAPPDATA"] = $WarmProfile' in block
    assert "$Warm.Kill()" in block
    assert "$env:APPDATA" not in text and "$env:LOCALAPPDATA" not in text


def test_shell_hello_waits_thirty_seconds_by_default():
    import tests.test_bridge_integration as integration
    source = inspect.getsource(integration)
    assert ('HELLO_TIMEOUT = max(RECEIVE_TIMEOUT, float(os.environ.get('
            '"TESSERACT_TEST_HELLO_TIMEOUT") or 30))') in source
    if not os.environ.get("TESSERACT_TEST_HELLO_TIMEOUT"):
        assert integration.HELLO_TIMEOUT >= 30
    assert "self.receive(HELLO_TIMEOUT)" in inspect.getsource(integration.Shell.hello)
