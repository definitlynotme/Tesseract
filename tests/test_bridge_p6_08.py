"""T-P6-08: subscription barrier speed-up.

R1: inside one subscription control, every ready state snapshot goes before
the list seeds. R2: one publish tick submits consecutive control frames of
that seed while the transport answers ``complete``, bounded by a byte budget
(``max_bytes``) and a frame cap (CONTROL_FRAMES_PER_TICK). In-memory only.
"""
from copy import deepcopy
import json
import math
from pathlib import Path
import time

import pytest

import bridge_protocol as protocol
from bridge_publish import CONTROL_FRAMES_PER_TICK, Publisher
from bridge_state import LIST_KEYS, LIST_TOPICS, PublicationState


EPOCH = "epoch-p6-08"
V1 = Path(__file__).resolve().parents[1] / "bridge_schema" / "fixtures" / "v1" / "valid"

# The shell's one subscribe, in its order (transport.rs TOPICS, then
# V1_PAGE_TOPICS, then the declared notify.rs SHELL_TOPICS); see T-P6-07.
P1 = ["service.state", "route.published", "tiers.summary", "traffic.tick",
      "traffic.history", "core.info", "logs.batch", "jobs"]
V1_PAGE = ["service.collect", "tier.transition", "exit.identity", "protocols.mix", "network.mode",
           "killswitch.state", "bastion.pool", "bastion.sources", "nodes.list", "subscriptions.list",
           "gateway.current", "config.changed", "config.items", "logs.feed", "alerts.history",
           "update.state", "geo.status"]
SHELL = ["alerts.desktop", "ui.language", "update.prepareExit"]
TOPICS = [topic for topic in P1 + V1_PAGE + SHELL if topic in protocol.V1_TOPICS]
STATE_SLOTS = [topic for topic in TOPICS
               if topic in protocol.V1_SNAPSHOT_TOPICS and topic not in LIST_TOPICS]
LIST_SLOTS = [(topic, key) for topic in TOPICS if topic in LIST_TOPICS
              for key in (LIST_KEYS if topic in protocol.KEYED_TOPICS else (None,))]


def v1(topic):
    return json.loads((V1 / f"topic_{topic}.json").read_bytes())["data"]


def node_rows(count, key, suffix=""):
    example = json.loads((V1 / "p4c2_topic_nodes.list_slow.json").read_bytes())["data"]["items"][0]
    return [{**example, "id": f"{key[:2]}{index:022x}",
             "name": f"HK-{index:04d} :: VLESS Reality{suffix}", "tier": key}
            for index in range(count)]


def shell_state(*, skip=(), nodes=None, suffix=""):
    """Every shell state topic has its first DTO; lists are the fixtures'.

    ``nodes`` maps a key to a nodes.list row count (realistic fixture rows).
    """
    state = PublicationState(EPOCH)
    for topic in STATE_SLOTS:
        if topic not in skip:
            state.record_state(topic, v1(topic))
    for topic in ("bastion.sources", "config.items"):
        state.record_list(topic, v1(topic)["items"])
    state.record_list("subscriptions.list", v1("subscriptions.list")["items"], key="vanguard")
    for key, count in (nodes or {"vanguard": 0}).items():
        state.record_list("nodes.list", node_rows(count, key, suffix) if count
                          else v1("nodes.list")["items"], key=key)
    return state


class Wire:
    """Shell-side receive rules (as tests/test_bridge_h2.py): per-topic
    increasing rev, continuous edge seq, gap from_seq = previous + 1.
    ``script`` maps a 1-based send attempt to "pending" or "busy"."""

    def __init__(self, script=None):
        self.frames, self.sizes, self.revs, self.seqs = [], [], {}, {}
        self.script, self.attempts, self.poll = dict(script or {}), 0, []

    def send(self, frame):
        self.attempts += 1
        outcome = self.script.get(self.attempts, "complete")
        if outcome == "busy":
            return "busy"
        encoded = protocol.encode_frame(frame)
        decoded = protocol.decode_line(encoded)
        assert protocol.validate_frame(decoded) == []
        for item in items(decoded):
            topic = item["topic"]
            if topic != "jobs":
                key = (topic, item.get("key"))
                assert item["rev"] > self.revs.get(key, 0) or topic in LIST_TOPICS, ("rev", item)
                self.revs[key] = item["rev"]
            if item["kind"] in {"edge", "gap"} and topic != "jobs":
                previous = self.seqs.get(topic, 0)
                first = item["data"]["from_seq"] if item["kind"] == "gap" else item["seq"]
                assert first == previous + 1, ("seq", previous, item)
                self.seqs[topic] = item["seq"]
        self.frames.append(decoded)
        self.sizes.append(len(encoded))
        return outcome

    def poll_write(self):
        return self.poll.pop(0) if self.poll else "complete"


def items(frame):
    if frame["t"] == "batch":
        return frame["items"]
    if frame["t"] == "evt":
        return [frame]
    return [frame["snapshot"]] if "snapshot" in frame else []


def slot_of(frame):
    item = frame if frame["t"] == "evt" else frame.get("snapshot", frame)
    if item.get("topic") in LIST_TOPICS:
        return (item["topic"], item.get("key"))
    return item.get("topic")


def subscribe(pub, topics=TOPICS, ident=1, owner="shell"):
    return pub.subscription_frames({"t": "subscribe", "epoch": EPOCH, "produced_at_ms": 1,
                                    "sent_at_ms": 1, "id": ident, "topics": list(topics)},
                                   owner=owner)


def pull_cursors(pub, frames, owner="shell"):
    """Request the next page of every incomplete list page, as the shell does."""
    for frame in frames:
        page = items(frame)[0].get("page") if items(frame) else None
        if page and not page["complete"]:
            pull_cursors.ident += 1
            item = items(frame)[0]
            pub.snapshot_response({"t": "snapshot_req", "epoch": EPOCH, "produced_at_ms": 1,
                                   "sent_at_ms": 1, "id": pull_cursors.ident, "topic": item["topic"],
                                   **({"key": item["key"]} if item.get("key") else {}),
                                   "cursor": page["next_cursor"]}, owner=owner)


pull_cursors.ident = 1000


def run_ticks(pub, wire, limit=200, pull=False):
    """Tick until quiet; return the frames of every tick (empty ticks too).
    ``pull`` requests the next page of every incomplete page (shell cursor pulls)."""
    per_tick, quiet = [], 0
    for _ in range(limit):
        before = len(wire.frames)
        pub.tick()
        per_tick.append(list(range(before, len(wire.frames))))
        if pull:
            pull_cursors(pub, wire.frames[before:])
        quiet = quiet + 1 if not per_tick[-1] and not pub._controls and not pub._list_groups else 0
        if quiet >= 2:
            return per_tick
    raise AssertionError("publisher did not settle")


def seed_order(wire):
    return [slot_of(frame) for frame in wire.frames if frame["t"] == "evt"]


def expected_order():
    return STATE_SLOTS + LIST_SLOTS


def test_the_shell_subscription_covers_28_or_more_slots():
    # 28 since the 提醒 redesign (10-30) retired alerts.delivery / alerts.onboarding.
    assert len(STATE_SLOTS) + len(LIST_SLOTS) >= 28
    assert len(LIST_SLOTS) == 10


def test_state_snapshots_follow_subscribed_and_precede_every_list_seed():
    state, wire = shell_state(), Wire()
    pub = Publisher(state, wire)
    subscribe(pub)
    per_tick = run_ticks(pub, wire)
    assert wire.frames[0]["t"] == "subscribed"
    order = seed_order(wire)
    # R1: request order is kept within states and within list seeds.
    assert order == expected_order()
    first_list = next(index for index, slot in enumerate(order) if isinstance(slot, tuple))
    assert all(isinstance(slot, str) for slot in order[:first_list])
    assert order[0] == "service.state"
    # The reply keeps a tick of its own; the whole small seed is the next tick.
    assert per_tick[0] == [0]
    assert len(per_tick[1]) == len(expected_order()) <= CONTROL_FRAMES_PER_TICK


def test_a_state_topic_without_its_first_dto_does_not_hold_the_list_seeds():
    state, wire = shell_state(skip={"service.state"}), Wire()
    pub = Publisher(state, wire)
    subscribe(pub)
    pub.tick(); pub.tick()
    order = seed_order(wire)
    assert "service.state" not in order
    assert order == [slot for slot in expected_order() if slot != "service.state"]
    state.record_state("service.state", v1("service.state"))
    run_ticks(pub, wire)
    assert seed_order(wire)[-1] == "service.state"
    assert not pub._controls


def pack(sizes, budget, cap):
    """Ticks a greedy per-tick budget needs: the first frame always goes."""
    ticks, used, count = 0, 0, 0
    for size in sizes:
        if count and (count >= cap or used + size > budget):
            ticks, used, count = ticks + 1, 0, 0
        used, count = used + size, count + 1
    return ticks + (count > 0)


@pytest.mark.parametrize("budget", [600, 2048, 8192])
def test_byte_budget_bounds_each_tick_and_the_tick_count_follows_from_it(budget):
    reference = Wire()
    pub = Publisher(shell_state(), reference)
    subscribe(pub)
    run_ticks(pub, reference)
    seed_sizes = reference.sizes[1:]
    state, wire = shell_state(), Wire()
    pub = Publisher(state, wire)
    pub.control_bytes_per_tick = budget
    subscribe(pub)
    per_tick = [tick for tick in run_ticks(pub, wire) if tick]
    assert seed_order(wire) == expected_order()
    for tick in per_tick:
        assert len(tick) == 1 or sum(wire.sizes[i] for i in tick) <= budget
    expected = 1 + pack(seed_sizes, budget, CONTROL_FRAMES_PER_TICK)
    # Sizes differ by timestamp digits only; allow one tick of slack.
    assert expected <= len(per_tick) <= expected + 1
    assert pub.control_burst_peak_bytes <= max(budget, max(seed_sizes))


@pytest.mark.parametrize("cap", [1, 5, 12])
def test_frame_cap_bounds_each_tick(cap):
    state, wire = shell_state(), Wire()
    pub = Publisher(state, wire)
    pub.control_frames_per_tick = cap
    subscribe(pub)
    per_tick = [tick for tick in run_ticks(pub, wire) if tick]
    assert seed_order(wire) == expected_order()
    assert max(len(tick) for tick in per_tick) <= cap
    assert len(per_tick) == 1 + math.ceil(len(expected_order()) / cap)
    assert pub.control_burst_peak_frames <= cap


def test_default_seed_of_1000_nodes_needs_few_ticks_and_stays_in_budget():
    state, wire = shell_state(nodes={"vanguard": 250, "sentinel": 250, "bastion": 250, "gateway": 250}), Wire()
    pub = Publisher(state, wire)
    subscribe(pub)
    per_tick = [tick for tick in run_ticks(pub, wire) if tick]
    assert seed_order(wire) == expected_order()
    assert len(per_tick) <= 4  # reply + about 300 KB of seed at 256 KiB a tick
    for tick in per_tick:
        assert len(tick) == 1 or sum(wire.sizes[i] for i in tick) <= pub.max_bytes


def reference_frames():
    wire = Wire()
    pub = Publisher(shell_state(), wire)
    subscribe(pub)
    run_ticks(pub, wire)
    return wire


def strip(frame):
    frame = deepcopy(frame)
    for item in [frame] + items(frame):
        item.pop("sent_at_ms", None)
        item.pop("produced_at_ms", None)
    return frame


@pytest.mark.parametrize("at", [2, 5, 14])
def test_pending_stops_the_burst_and_the_next_tick_resumes_without_loss_or_duplicate(at):
    reference = reference_frames()
    state, wire = shell_state(), Wire({at: "pending"})
    pub = Publisher(state, wire)
    subscribe(pub)
    pub.tick()                      # subscribed (attempt 1)
    pub.tick()                      # burst up to and including the pending frame
    assert len(wire.frames) == at and pub.pending_write
    wire.poll = ["pending"]         # the overlapped write is still in flight
    assert not pub.tick() and len(wire.frames) == at
    run_ticks(pub, wire)
    assert [strip(frame) for frame in wire.frames] == [strip(frame) for frame in reference.frames]


@pytest.mark.parametrize("at", [2, 3, 9])
def test_busy_mid_burst_retries_the_same_frame_once(at):
    reference = reference_frames()
    state, wire = shell_state(), Wire({at: "busy"})
    pub = Publisher(state, wire)
    subscribe(pub)
    run_ticks(pub, wire)
    assert [strip(frame) for frame in wire.frames] == [strip(frame) for frame in reference.frames]


def test_burst_drops_covered_edges_and_keeps_wire_seq_continuous():
    state, wire = PublicationState(EPOCH), Wire()
    pub = Publisher(state, wire)
    base = v1("service.state")
    state.record_state("service.state", base)
    state.record_state("core.info", v1("core.info"))
    subscribe(pub, ["service.state", "core.info"], ident=1)
    run_ticks(pub, wire)
    state.record_edge("service.state", base)                      # state seq 1, sent
    run_ticks(pub, wire)
    assert wire.seqs["service.state"] == 1
    for topic in STATE_SLOTS:
        if topic not in ("service.state", "core.info"):
            state.record_state(topic, v1(topic))
    older, newer = deepcopy(base), deepcopy(base)
    older["runtime"]["startup_phase"] = "collecting"
    newer["runtime"]["startup_phase"] = "core_standby"
    state.record_edge("service.state", older)                     # state seq 2, queued
    state.record_edge("service.state", newer)                     # state seq 3, queued
    before = len(wire.frames)
    subscribe(pub, TOPICS, ident=2, owner="window-1")
    run_ticks(pub, wire)
    later = wire.frames[before:]
    snapshot = next(index for index, frame in enumerate(later)
                    if frame["t"] == "evt" and frame["topic"] == "service.state")
    assert later[snapshot]["data"]["runtime"]["startup_phase"] == "core_standby"
    assert not any(item["topic"] == "service.state" and item["kind"] != "snapshot"
                   for frame in later for item in items(frame))   # covered: never sent
    latest = deepcopy(base)
    latest["runtime"]["startup_phase"] = "running"
    state.record_edge("service.state", latest)                    # state seq 4 -> wire seq 2
    run_ticks(pub, wire)
    assert wire.seqs["service.state"] == 2                        # Wire asserted continuity
    assert state.pending(64) == []


@pytest.mark.parametrize("kind", ["edge", "gap"])
def test_a_covered_edge_or_gap_mid_burst_is_dropped_and_wire_seq_stays_continuous(kind):
    """Review fix 1: the covered topic (core.info) is in the middle of the
    burst, not its first frame; a queued gap (ring overflow) is covered too."""
    topic = "core.info"
    state = shell_state()
    if kind == "gap":
        state.edge_capacity = 2
    wire = Wire()
    pub = Publisher(state, wire)
    base = v1(topic)
    subscribe(pub, [topic], ident=1)
    run_ticks(pub, wire)
    state.record_edge(topic, base)                                 # state seq 1, sent
    run_ticks(pub, wire)
    assert wire.seqs[topic] == 1
    for _ in range(4 if kind == "gap" else 2):
        state.record_edge(topic, deepcopy(base))                   # queued (and overflowing)
    queued = [item["kind"] for item in state.pending_for(topic)]
    assert ("gap" in queued) == (kind == "gap") and "edge" in queued
    before = len(wire.frames)
    subscribe(pub, TOPICS, ident=2, owner="window-1")
    per_tick = [tick for tick in run_ticks(pub, wire) if tick and tick[0] >= before]
    burst = per_tick[1]
    position = next(offset for offset, index in enumerate(burst)
                    if slot_of(wire.frames[index]) == topic)
    assert 0 < position < len(burst) - 1                          # mid-burst
    assert not any(item["topic"] == topic and item["kind"] != "snapshot"
                   for frame in wire.frames[before:] for item in items(frame))   # covered
    for _ in range(3):                                             # gap case overflows again
        state.record_edge(topic, deepcopy(base))
    run_ticks(pub, wire)
    later = [item["kind"] for frame in wire.frames[before:] for item in items(frame)
             if item["topic"] == topic and item["kind"] != "snapshot"]
    assert later and ("gap" in later) == (kind == "gap")
    assert wire.seqs[topic] > 1                                    # Wire asserted continuity
    assert state.pending(64) == []


LONG_SUFFIX = " | 香港 IPLC 专线 Premium"


def first_page_of_a_group(frame):
    page = items(frame)[0].get("page") if items(frame) else None
    return bool(page) and not page["complete"]


def tick_to_first_multi_page(pub, wire, limit=10):
    for _ in range(limit):
        before = len(wire.frames)
        pub.tick()
        if any(first_page_of_a_group(frame) for frame in wire.frames[before:]):
            pull_cursors(pub, wire.frames[before:])
            return
    raise AssertionError("no multi-page group was opened")


def test_a_multi_page_first_page_gives_the_next_tick_to_the_batch():
    """Review fix: the first page of a paged group fills the byte budget, so
    the next tick is the batch's turn, as when _submit spends the budget."""
    state, wire = shell_state(nodes={"vanguard": 1000}, suffix=LONG_SUFFIX), Wire()
    pub = Publisher(state, wire)
    subscribe(pub)
    tick_to_first_multi_page(pub, wire)
    assert pub._controls                       # later list seeds are still waiting
    state.record_edge("logs.batch", v1("logs.batch"))
    before = len(wire.frames)
    pub.tick()
    assert [frame["t"] for frame in wire.frames[before:]] == ["batch"]
    run_ticks(pub, wire, pull=True)
    assert seed_order(wire) == expected_order()


def test_a_ready_reply_goes_before_a_batch_edge_of_its_topic_on_a_batch_turn():
    """bridge_publish _tick: on the batch's turn, a reply not yet sent still
    goes before every batch item of its topic (as when controls had every tick)."""
    state, wire = shell_state(nodes={"vanguard": 1000}, suffix=LONG_SUFFIX), Wire()
    pub = Publisher(state, wire)
    subscribe(pub)
    tick_to_first_multi_page(pub, wire)
    pub.snapshot_response({"t": "snapshot_req", "epoch": EPOCH, "produced_at_ms": 1,
                           "sent_at_ms": 1, "id": 99, "topic": "core.info"}, owner="shell")
    changed = {**v1("core.info"), "generation": 7}
    state.record_edge("core.info", changed)
    state.record_edge("logs.batch", v1("logs.batch"))
    before = len(wire.frames)
    pub.tick()                                          # the batch's turn
    turn = wire.frames[before:]
    assert [frame["t"] for frame in turn] == ["batch"]
    assert [item["topic"] for item in turn[0]["items"]] == ["logs.batch"]
    run_ticks(pub, wire, pull=True)
    reply = next(index for index, frame in enumerate(wire.frames)
                 if frame["t"] == "snapshot_res" and frame.get("topic") == "core.info")
    assert wire.frames[reply]["snapshot"]["data"] == changed
    edges = [index for index, frame in enumerate(wire.frames)
             if any(item["topic"] == "core.info" and item["kind"] != "snapshot"
                    for item in items(frame))]
    assert all(index > reply for index in edges)


def test_ordinary_publications_are_not_held_for_the_whole_seed():
    state, wire = shell_state(), Wire()
    pub = Publisher(state, wire)
    pub.control_bytes_per_tick = 600     # stretch the seed over many ticks
    subscribe(pub)
    pub.tick(); pub.tick()
    state.record_edge("logs.batch", v1("logs.batch"))
    ticks_until_batch = None
    for index in range(1, 6):
        before = len(wire.frames)
        pub.tick()
        if any(frame["t"] == "batch" for frame in wire.frames[before:]):
            ticks_until_batch = index
            break
    assert ticks_until_batch is not None and ticks_until_batch <= 2
    assert pub._controls               # the seed was still in progress
    run_ticks(pub, wire)
    assert seed_order(wire) == expected_order()
    batches = [index for index, frame in enumerate(wire.frames) if frame["t"] == "batch"]
    last_seed = max(index for index, frame in enumerate(wire.frames) if frame["t"] == "evt")
    assert batches[0] < last_seed


def test_the_seed_follows_the_reply_even_while_a_batch_waits():
    """The reply's own tick is not a spent budget: tick 2 is the seed, not
    a waiting log batch (observed in the first source-mode probe)."""
    state, wire = shell_state(), Wire()
    pub = Publisher(state, wire)
    subscribe(pub)
    state.record_edge("logs.batch", v1("logs.batch"))
    pub.tick()
    assert [frame["t"] for frame in wire.frames] == ["subscribed"]
    pub.tick()
    assert wire.frames[1]["t"] == "evt" and wire.frames[1]["topic"] == "service.state"
    assert seed_order(wire) == expected_order()
    pub.tick()
    assert wire.frames[-1]["t"] == "batch"


def test_one_tick_never_writes_a_batch_after_control_frames():
    """PROTOCOL §1: at most one batch per tick; a tick that wrote control
    frames leaves the batch to the next tick (the batch's turn)."""
    state, wire = shell_state(), Wire()
    pub = Publisher(state, wire)
    pub.control_bytes_per_tick = 600
    subscribe(pub)
    state.record_edge("logs.batch", v1("logs.batch"))
    for tick in run_ticks(pub, wire):
        kinds = [wire.frames[i]["t"] for i in tick]
        assert kinds.count("batch") <= 1
        assert "batch" not in kinds or kinds == ["batch"]


# 1000 fixture rows are about 255 KiB: one frame just under the page limit.
# A longer, realistic subscription name suffix makes the same 1000 rows page.
@pytest.mark.parametrize("suffix,paged", [("", False), (" | 香港 IPLC 专线 Premium", True)])
def test_burst_cost_with_1000_nodes_in_one_tier(suffix, paged):
    """Measured burst cost (the report quotes the evidence script's numbers;
    this keeps a loose regression bound): nodes.list, shell cursor pulls."""
    state, wire = shell_state(nodes={"vanguard": 1000}, suffix=suffix), Wire()
    pub = Publisher(state, wire)
    subscribe(pub)
    worst_ms, worst_bytes, ident = 0.0, 0, 100
    for _ in range(80):
        before = len(wire.frames)
        started = time.thread_time()   # CPU time: independent of machine load
        pub.tick()
        worst_ms = max(worst_ms, (time.thread_time() - started) * 1000)
        worst_bytes = max(worst_bytes, sum(wire.sizes[before:]))
        for frame in wire.frames[before:]:
            page = items(frame)[0].get("page") if items(frame) else None
            if page and not page["complete"]:
                ident += 1
                pub.snapshot_response({"t": "snapshot_req", "epoch": EPOCH, "produced_at_ms": 1,
                                       "sent_at_ms": 1, "id": ident, "topic": "nodes.list",
                                       "key": "vanguard", "cursor": page["next_cursor"]},
                                      owner="shell")
        if not pub._controls and not pub._list_groups and len(wire.frames) == before:
            break
    pages = [items(frame)[0] for frame in wire.frames
             if items(frame) and items(frame)[0].get("topic") == "nodes.list"
             and items(frame)[0].get("key") == "vanguard"]
    assert seed_order(wire)[:len(STATE_SLOTS)] == STATE_SLOTS
    if paged:
        assert len(pages) >= 2 and pages[-1]["page"]["complete"]
        assert protocol.validate_snapshot_pages(pages) == []
    else:
        assert len(pages) == 1 and "page" not in pages[0]
    assert sum(len(page["data"]["items"]) for page in pages) == 1000
    assert worst_bytes <= pub.max_bytes
    # Loose CPU-time regression bound (measured ~100 ms; the report quotes the
    # evidence script's wall-clock numbers): well under the 2 s ping period.
    assert worst_ms < 500
