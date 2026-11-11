"""P2-07 publication contract, using only in-memory DTO sources/transports."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

import bridge_protocol as protocol
from bridge_publish import Publisher
from bridge_state import LIST_KEYS, LIST_TOPICS, PublicationState, diff_lists, list_slot


FIXTURES = Path(__file__).resolve().parents[1] / "bridge_schema/fixtures/v1/valid"


def rows(topic, count=1, key="vanguard", width=0):
    example = json.loads((FIXTURES / f"topic_{topic}.json").read_bytes())["data"]["items"][0]
    result = []
    for index in range(count):
        row = {**example, "id": f"row-{index}"}
        if "tier" in row:
            row["tier"] = key
        row["name" if "name" in row else "display"] = "行" * width or f"Row {index}"
        result.append(row)
    return result


class Pipe:
    def __init__(self):
        self.frames = []
        self.mode = "complete"
        self.poll = "complete"

    def send(self, frame):
        assert protocol.validate_frame(frame) == []
        if self.mode != "busy":
            self.frames.append(protocol.decode_line(protocol.encode_frame(frame)))
        return self.mode

    def poll_write(self):
        return self.poll


def setup(**state_options):
    state, pipe, now = PublicationState("epoch-07", **state_options), Pipe(), [0.0]
    pub = Publisher(state, pipe, max_bytes=4096, clock=lambda: now[0])
    return state, pipe, pub, now


def request(kind, **fields):
    return {"t": kind, "epoch": "epoch-07", "produced_at_ms": 1,
            "sent_at_ms": 1, "id": 1, **fields}


def drain(pub, limit=40):
    for _ in range(limit):
        if not pub.tick():
            return
    raise AssertionError("publisher did not settle")


def subscribe(pub, *topics, owner="window-7"):
    pub.subscription_frames(request("subscribe", topics=list(topics)), owner=owner)
    drain(pub)


def events(pipe):
    result = []
    for frame in pipe.frames:
        if frame["t"] == "evt":
            result.append(frame)
        elif frame["t"] == "batch":
            result.extend(frame["items"])
        elif "snapshot" in frame:
            result.append(frame["snapshot"])
    return result


def pull(pub, topic, cursor=None, key=None, owner="window-7"):
    args = {"topic": topic}
    if key is not None:
        args["key"] = key
    if cursor is not None:
        args["cursor"] = cursor
    return pub.snapshot_response(request("snapshot_req", **args), owner=owner)


def collect_pages(pub, pipe, first, owner="window-7"):
    pages = [first]
    while not pages[-1]["page"]["complete"]:
        before = len(pipe.frames)
        reply = pull(pub, first["topic"], pages[-1]["page"]["next_cursor"], first.get("key"), owner)
        assert "snapshot" in reply
        drain(pub)
        pages.append(next(frame["snapshot"] for frame in pipe.frames[before:] if "snapshot" in frame))
    assert protocol.validate_snapshot_pages(pages) == []
    return pages


def assemble(pages):
    assert protocol.validate_snapshot_pages(pages) == []
    baseline = deepcopy(pages[0])
    baseline.pop("page")
    baseline["data"]["items"] = [row for page in pages for row in page["data"]["items"]]
    return baseline


@pytest.mark.parametrize("topic", sorted(protocol.KEYED_TOPICS))
def test_seed_four_keys_including_empty_and_independent_revisions(topic):
    state, pipe, pub, _ = setup()
    state.record_list(topic, rows(topic), key="vanguard")
    subscribe(pub, topic)
    seeds = events(pipe)
    assert [item["key"] for item in seeds] == list(LIST_KEYS)
    assert [len(item["data"]["items"]) for item in seeds] == [1, 0, 0, 0]
    assert [item["rev"] for item in seeds] == [1] * 4
    changed = rows(topic, key="sentinel")
    state.record_list(topic, changed, key="sentinel")
    assert pub.tick()
    patch = events(pipe)[-1]
    assert (patch["key"], patch["base_rev"], patch["rev"]) == ("sentinel", 1, 2)
    merged, missing = protocol.apply_topic_patch(seeds[1], patch)
    assert not missing and merged["data"]["items"] == changed
    assert state.list_snapshot(topic, "vanguard")["rev"] == 1
    assert all(slot[0] == "epoch-07" for slot in state._list_latest)


@pytest.mark.parametrize("topic", sorted(LIST_TOPICS))
def test_key_rules_and_row_tiers(topic):
    state, pipe, pub, _ = setup()
    key = "vanguard" if topic in protocol.KEYED_TOPICS else None
    assert state.record_list(topic, rows(topic), key=key)
    subscribe(pub, topic)
    assert all(protocol.validate_frame(frame) == [] for frame in pipe.frames)
    invalid_key = None if key else "vanguard"
    with pytest.raises(ValueError):
        state.record_list(topic, [], key=invalid_key)
    with pytest.raises(ValueError):
        state.record_list(topic, [], key="unknown")
    if key:
        with pytest.raises(ValueError):
            state.record_list(topic, rows(topic, key="gateway"), key=key)
    with pytest.raises(ValueError):
        state.record_list(topic, rows(topic) * 2, key=key)
    req = request("snapshot_req", topic=topic, **({"key": key} if key else {}))
    assert protocol.validate_frame(req) == []
    invalid = {**req, "key": "vanguard"} if key is None else {k: v for k, v in req.items() if k != "key"}
    assert protocol.validate_frame(invalid)
    with pytest.raises(ValueError):
        pub.snapshot_response(invalid)


def test_non_list_topics_refuse_keyed_state():
    state, _, _, _ = setup()
    for topic in protocol.V1_TOPICS - LIST_TOPICS:
        with pytest.raises(ValueError):
            state.record_list(topic, [], key="vanguard")
        assert protocol.validate_frame(request("snapshot_req", topic=topic, key="vanguard"))


def test_diff_replacement_removal_ownership_no_change_and_base_mismatch():
    state, pipe, pub, _ = setup()
    old = rows("config.items", 3)
    state.record_list("config.items", old)
    subscribe(pub, "config.items")
    baseline = events(pipe)[0]
    new = [dict(old[0], enabled=False), old[2], dict(old[1], id="new")]
    delta = diff_lists(old, new)
    assert [item["id"] for item in delta["changed"]] == ["row-0", "new"]
    assert delta["removed"] == ["row-1"]
    assert not {row["id"] for row in delta["changed"]}.intersection(delta["removed"])
    state.record_list("config.items", new)
    new[0]["name"] = "caller mutation"
    assert pub.tick()
    patch = events(pipe)[-1]
    merged, missing = protocol.apply_topic_patch(baseline, patch)
    assert not missing and merged["data"]["items"][0]["name"] != "caller mutation"
    assert protocol.apply_topic_patch({**baseline, "rev": 99}, patch) == (None, True)
    unchanged = state.list_snapshot("config.items")["data"]["items"]
    assert not state.record_list("config.items", list(reversed(unchanged)))
    assert not pub.tick()
    assert diff_lists(unchanged, unchanged) == {"changed": [], "removed": []}
    with pytest.raises(ValueError):
        diff_lists(old + [old[0]], new)


@pytest.mark.parametrize("topic", sorted(LIST_TOPICS))
def test_frozen_pages_and_deferred_patch_match_online_oracles(topic):
    state, pipe, pub, _ = setup()
    key = "vanguard" if topic in protocol.KEYED_TOPICS else None
    original = rows(topic, 35, width=65)
    state.record_list(topic, original, key=key)
    subscribe(pub, topic)
    first = next(item for item in events(pipe) if item.get("page"))
    replacement = deepcopy(original)
    replacement[0]["enabled"] = False
    replacement.pop()
    state.record_list(topic, replacement, key=key)
    assert not pub.tick()  # Never patch a half-assembled table.
    pages = collect_pages(pub, pipe, first)
    assert assemble(pages)["data"]["items"] == original
    assert all(page["rev"] == first["rev"] for page in pages)
    assert pages[-1]["page"]["next_cursor"] is None
    patch = next(item for item in events(pipe) if item["kind"] == "patch")
    assert protocol.apply_topic_patch(first, patch) == (None, True)
    merged, missing = protocol.apply_topic_patch(assemble(pages), patch)
    assert not missing and merged["data"]["items"] == replacement
    assert all(len(protocol.encode_frame(frame)) <= pub.max_bytes for frame in pipe.frames)


def test_cursor_binds_owner_topic_key_epoch_and_is_consumed_once():
    state, pipe, pub, _ = setup()
    state.record_list("nodes.list", rows("nodes.list", 30, width=60), key="vanguard")
    subscribe(pub, "nodes.list", "subscriptions.list")
    first = next(item for item in events(pipe) if item.get("page"))
    cursor = first["page"]["next_cursor"]
    for topic, key, owner in [("nodes.list", "vanguard", "intruder"),
                              ("nodes.list", "sentinel", "window-7"),
                              ("subscriptions.list", "vanguard", "window-7")]:
        assert pull(pub, topic, cursor, key, owner)["error"]["code"] == "snapshot_required"
        drain(pub)
    with pytest.raises(ValueError):
        pub.snapshot_response({**request("snapshot_req", topic="nodes.list", key="vanguard", cursor=cursor), "epoch": "old"})
    reply = pull(pub, "nodes.list", cursor, "vanguard")
    assert reply["snapshot"]["page"]["index"] == 1
    drain(pub)
    assert pull(pub, "nodes.list", cursor, "vanguard")["error"]["code"] == "snapshot_required"


@pytest.mark.parametrize("overflow", ["count", "bytes"])
def test_paging_change_overflow_is_bounded_and_requires_restart(overflow):
    state, pipe, pub, _ = setup(list_history_capacity=2, list_history_bytes=400 if overflow == "bytes" else 262144)
    original = rows("config.items", 30, width=60)
    state.record_list("config.items", original)
    subscribe(pub, "config.items")
    first = events(pipe)[0]
    for index in range(3):
        changed = deepcopy(original)
        changed[0]["name"] = "z" * (450 if overflow == "bytes" else index + 1)
        state.record_list("config.items", changed)
    slot = list_slot(state.epoch, "config.items")
    assert len(state._list_history[slot]) <= 2
    assert state._list_history_sizes[slot] <= state.list_history_bytes
    assert state.list_catchup(slot, first["rev"]) == (None, True)
    reply = pull(pub, "config.items", first["page"]["next_cursor"])
    assert reply["error"]["code"] == "snapshot_required"
    drain(pub)
    assert not any(item["kind"] == "patch" for item in events(pipe))
    restarted = pull(pub, "config.items")
    drain(pub)
    assert restarted["snapshot"]["rev"] == state.list_snapshot("config.items")["rev"]
    assert restarted["snapshot"]["page"]["snapshot_id"] != first["page"]["snapshot_id"]


def test_idle_timeout_uses_monotonic_activity_and_expires_at_30_seconds():
    state, pipe, pub, now = setup()
    state.record_list("config.items", rows("config.items", 50, width=70))
    subscribe(pub, "config.items")
    first = events(pipe)[0]
    now[0] = 29
    second = pull(pub, "config.items", first["page"]["next_cursor"])["snapshot"]
    drain(pub)
    now[0] = 58.999
    third = pull(pub, "config.items", second["page"]["next_cursor"])["snapshot"]
    drain(pub)
    now[0] = 88.999
    assert pull(pub, "config.items", third["page"]["next_cursor"])["error"]["code"] == "snapshot_required"
    drain(pub)
    assert not pub._list_cursors


def test_owner_limit_four_groups_and_revoke_hook_invalidates_cursors():
    state, pipe, pub, _ = setup()
    for key in LIST_KEYS:
        state.record_list("nodes.list", rows("nodes.list", 25, key=key, width=60), key=key)
    state.record_list("config.items", rows("config.items", 25, width=60))
    subscribe(pub, "nodes.list", "config.items")
    assert len(pub._list_cursors) == 4
    assert pull(pub, "config.items")["error"]["code"] == "snapshot_required"
    drain(pub)
    assert "snapshot" in pull(pub, "config.items", owner="other-owner")
    drain(pub)
    cursor = next(item["page"]["next_cursor"] for item in events(pipe) if item.get("key") == "vanguard")
    # Exercise P2-03's actual registration and ordered dispatch with an inert session.
    from bridge_main import BridgeSession
    from types import SimpleNamespace
    import threading
    import time
    session = BridgeSession(None, SimpleNamespace(gate=SimpleNamespace(require_idle=lambda: None)),
                            None, state.epoch, runtime_factory=lambda *_: SimpleNamespace())
    session.transport = Pipe()
    session.publisher = SimpleNamespace(pending_write=False, next_tick=time.monotonic() + 100, tick=lambda: None)
    session.shutdown = SimpleNamespace(quitting=False, _quitting=threading.Event(),
                                       _lock=threading.Lock(), _quit_ack=None)
    try:
        session.register_owner_revoke_hook(pub.revoke_owner)
        session.dispatch(session.envelope("owner_revoke", id=2, owner="window-7"))
        session.pump()
        assert session.transport.frames[0]["accepted"]
        assert pull(pub, "nodes.list", cursor, "vanguard")["error"]["code"] == "snapshot_required"
        assert len(pub._list_cursors) == 1  # The other owner remains live.
    finally:
        session.executor.shutdown(time.monotonic() + 1)


def test_busy_and_pending_do_not_advance_page_or_ack_baseline():
    state, pipe, pub, _ = setup()
    state.record_list("config.items", rows("config.items", 30, width=60))
    pub.subscription_frames(request("subscribe", topics=["config.items"]), owner="window-7")
    assert pub.tick()  # subscribed
    pipe.mode = "busy"
    assert not pub.tick()
    assert not pub._list_cursors and not pub._list_sent
    template = pub._controls[0]["seed_control"]["reply"]
    assert not pub.tick()
    assert pub._controls[0]["seed_control"]["reply"] == template
    pipe.mode = "pending"
    assert pub.tick()
    first = events(pipe)[0]
    cursor = first["page"]["next_cursor"]
    assert cursor in pub._list_cursors
    pipe.poll = "pending"
    assert not pub.tick()
    assert not pub._list_sent
    pipe.poll, pipe.mode = "complete", "complete"
    pages = collect_pages(pub, pipe, first)
    assert len(pages) > 1 and pub._list_sent


def test_backpressure_coalesces_latest_and_large_patch_restarts_as_snapshot():
    state, pipe, pub, _ = setup()
    state.record_list("config.items", rows("config.items", 30))
    subscribe(pub, "config.items")
    first = events(pipe)[0]
    if "page" in first:
        collect_pages(pub, pipe, first)
    pipe.poll = "pending"
    for index in range(8):
        state.record_list("config.items", rows("config.items", 30, width=50 + index))
        assert not pub.tick()
    assert state.depth == 1
    pipe.poll = "complete"
    drain(pub)
    latest = next(item for item in reversed(events(pipe)) if item.get("page"))
    assert latest["rev"] == 9 and latest["kind"] == "snapshot"
    assert not any(item["kind"] == "patch" for item in events(pipe))
    assert assemble(collect_pages(pub, pipe, latest))["data"]["items"] == rows("config.items", 30, width=57)


def test_single_row_over_budget_fails_explicitly_without_oversized_frame():
    state, pipe, pub, _ = setup()
    state.record_list("config.items", rows("config.items", width=2000))
    subscribe(pub, "config.items")
    assert pipe.frames[-1]["error"]["code"] == "snapshot_required"
    assert len(pub._list_groups) == 0
    assert all(len(protocol.encode_frame(frame)) <= pub.max_bytes for frame in pipe.frames)


def test_missing_p1_sample_does_not_block_other_list_seeds():
    state, pipe, pub, _ = setup()
    subscribe(pub, "service.state", "nodes.list")
    assert [item["key"] for item in events(pipe)] == list(LIST_KEYS)
    assert not any(item["topic"] == "service.state" for item in events(pipe))
    assert not pub.tick()


def test_revoke_before_initial_seed_does_not_create_new_groups():
    state, pipe, pub, _ = setup()
    state.record_list("config.items", rows("config.items", 30, width=60))
    pub.subscription_frames(request("subscribe", topics=["config.items"]), owner="window-7")
    assert pub.tick()
    pub.revoke_owner("window-7")
    drain(pub)
    assert not events(pipe) and not pub._list_groups and not pub._list_cursors
    assert pipe.frames[-1]["error"]["code"] == "snapshot_required"
    state.record_list("config.items", rows("config.items", 31, width=60))
    assert not pub.tick()


def test_source_change_during_submission_retains_new_pending_revision():
    state, pipe, pub, _ = setup()
    old = rows("config.items")
    state.record_list("config.items", old)
    subscribe(pub, "config.items")
    baseline = events(pipe)[0]
    state.record_list("config.items", [dict(old[0], enabled=False)])
    original_send = pipe.send
    def concurrent_send(frame):
        outcome = original_send(frame)
        state.record_list("config.items", [dict(old[0], enabled=False, name="Newest")])
        return outcome
    pipe.send = concurrent_send
    assert pub.tick()
    pipe.send = original_send
    assert state.depth == 1
    assert pub.tick()
    for patch in events(pipe)[1:]:
        baseline, missing = protocol.apply_topic_patch(baseline, patch)
        assert not missing
    assert baseline["rev"] == 3 and baseline["data"]["items"][0]["name"] == "Newest"
    assert state.depth == 0


def test_changes_that_cancel_out_before_send_need_no_event():
    state, pipe, pub, _ = setup()
    original = rows("bastion.sources")
    state.record_list("bastion.sources", original)
    subscribe(pub, "bastion.sources")
    baseline = events(pipe)[0]
    state.record_list("bastion.sources", [dict(original[0], enabled=False)])
    state.record_list("bastion.sources", original)
    assert not pub.tick() and state.depth == 0
    state.record_list("bastion.sources", [dict(original[0], enabled=False)])
    assert pub.tick()
    patch = events(pipe)[-1]
    assert patch["base_rev"] == 1 and patch["rev"] == 4
    assert protocol.apply_topic_patch(baseline, patch)[1] is False


def test_queued_page_is_invalidated_on_overflow_before_send():
    state, pipe, pub, _ = setup(list_history_capacity=1)
    old = rows("config.items", 30, width=60)
    state.record_list("config.items", old)
    subscribe(pub, "config.items")
    first = events(pipe)[0]
    pull(pub, "config.items", first["page"]["next_cursor"])
    state.record_list("config.items", [dict(row, enabled=False) for row in old])
    state.record_list("config.items", old)
    before = len(events(pipe))
    drain(pub)
    assert len(events(pipe)) == before
    assert pipe.frames[-1]["t"] == "snapshot_res"
    assert pipe.frames[-1]["error"]["code"] == "snapshot_required"


def test_aggregate_above_hard_frame_cap_is_paginated_under_regular_budget(monkeypatch):
    # Lower the hard cap in the fixture to exercise aggregate/hard-cap separation
    # without building multi-megabyte DTOs or doing a heavy CPU measurement.
    monkeypatch.setattr(protocol, "HARD_FRAME_LIMIT", 8192)
    state, pipe, pub, _ = setup()
    original = rows("config.items", 50, width=70)
    assert len(json.dumps(original, ensure_ascii=False).encode()) > protocol.HARD_FRAME_LIMIT
    state.record_list("config.items", original)
    subscribe(pub, "config.items")
    pages = collect_pages(pub, pipe, events(pipe)[0])
    assert assemble(pages)["data"]["items"] == original
    assert all(len(protocol.encode_frame(frame)) <= pub.max_bytes for frame in pipe.frames)


def test_overflow_during_final_page_submission_invalidates_before_any_patch():
    state, pipe, pub, _ = setup(list_history_capacity=1)
    original = rows("config.items", 20, width=60)
    state.record_list("config.items", original)
    subscribe(pub, "config.items")
    page = events(pipe)[0]
    while True:
        queued = pull(pub, "config.items", page["page"]["next_cursor"])
        if queued["snapshot"]["page"]["complete"]:
            break
        drain(pub)
        page = events(pipe)[-1]
    original_send = pipe.send
    def concurrent_send(frame):
        outcome = original_send(frame)
        if "snapshot" in frame:
            state.record_list("config.items", [dict(row, enabled=False) for row in original])
            state.record_list("config.items", original)
        return outcome
    pipe.send = concurrent_send
    assert pub.tick()
    pipe.send = original_send
    drain(pub)
    assert pipe.frames[-1]["error"]["code"] == "snapshot_required"
    assert not pub._list_sent and not any(item["kind"] == "patch" for item in events(pipe))


def test_eight_large_initial_keys_wait_for_owner_slots_and_allow_cursor_progress():
    state, pipe, pub, _ = setup()
    for topic in sorted(protocol.KEYED_TOPICS):
        for key in LIST_KEYS:
            state.record_list(topic, rows(topic, 25, key=key, width=60), key=key)
    subscribe(pub, "nodes.list", "subscriptions.list")
    assert len(pub._list_cursors) == 4
    assert all(item["topic"] == "nodes.list" for item in events(pipe))
    # A not-yet-started seed freezes the current source when its slot opens.
    newer = rows("subscriptions.list", 26, key="vanguard", width=60)
    state.record_list("subscriptions.list", newer, key="vanguard")
    for _ in range(60):
        groups = {}
        for page in events(pipe):
            groups.setdefault(page["page"]["snapshot_id"], []).append(page)
        unfinished = next((pages[-1] for pages in groups.values() if not pages[-1]["page"]["complete"]), None)
        if unfinished is None:
            break
        pull(pub, unfinished["topic"], unfinished["page"]["next_cursor"], unfinished["key"])
        drain(pub)
        assert sum(not group.get("invalid") for group in pub._list_groups.values()) <= 4
    else:
        raise AssertionError("initial page groups did not finish")
    assert len(groups) == 8 and not pub._controls and not pub._list_groups
    assert all(protocol.validate_snapshot_pages(pages) == [] for pages in groups.values())
    assert not any("error" in frame for frame in pipe.frames)
    updated = next(pages for pages in groups.values() if pages[0]["topic"] == "subscriptions.list" and pages[0]["key"] == "vanguard")
    assert assemble(updated)["data"]["items"] == newer


def test_busy_fourth_initial_page_retries_without_waiting_for_its_own_slot():
    state, pipe, pub, _ = setup()
    for key in LIST_KEYS:
        state.record_list("nodes.list", rows("nodes.list", 25, key=key, width=60), key=key)
    pub.subscription_frames(request("subscribe", topics=["nodes.list"]), owner="window-7")
    for _ in range(4):  # Reply plus the first three keys.
        assert pub.tick()
    pipe.mode = "busy"
    assert not pub.tick() and len(pub._list_groups) == 4
    pipe.mode = "complete"
    assert pub.tick() and len(pub._list_cursors) == 4
    assert [item["key"] for item in events(pipe)] == list(LIST_KEYS)


def test_seed_that_grows_while_waiting_obeys_owner_capacity():
    state, pipe, pub, _ = setup()
    for key in LIST_KEYS:
        state.record_list("nodes.list", rows("nodes.list", 25, key=key, width=60), key=key)
    pub.subscription_frames(request("subscribe", topics=["nodes.list", "subscriptions.list"]), owner="window-7")
    for _ in range(5):
        assert pub.tick()
    state.record_list("subscriptions.list", rows("subscriptions.list", 25, width=60), key="vanguard")
    assert not pub.tick() and len(pub._list_cursors) == 4
    assert not any("error" in frame for frame in pipe.frames)
    collect_pages(pub, pipe, events(pipe)[0])
    assert any(item["topic"] == "subscriptions.list" and item.get("page") for item in events(pipe))


@pytest.mark.parametrize("topic", sorted(LIST_TOPICS))
@pytest.mark.parametrize("restart", ["subscribe", "snapshot"])
@pytest.mark.parametrize("queue_old_page", [False, True])
@pytest.mark.parametrize("new_owner", ["window-7", "other-owner"])
def test_restarted_slot_cannot_finish_old_pages_after_new_baseline(topic, restart, queue_old_page, new_owner):
    state, pipe, pub, _ = setup()
    key = "vanguard" if topic in protocol.KEYED_TOPICS else None
    original = rows(topic, 35, width=65)
    state.record_list(topic, original, key=key)
    subscribe(pub, topic)
    first = next(item for item in events(pipe) if item.get("page"))
    old_id = first["page"]["snapshot_id"]
    old_cursor = first["page"]["next_cursor"]
    if queue_old_page:
        # Queue the final old page so an unchecked send would install rev=1.
        while True:
            queued = pull(pub, topic, old_cursor, key)["snapshot"]
            if queued["page"]["complete"]:
                break
            drain(pub)
            old_cursor = queued["page"]["next_cursor"]
    replacement = [dict(row, enabled=False) for row in original]
    state.record_list(topic, replacement, key=key)
    before = len(events(pipe))
    if restart == "subscribe":
        pub.subscription_frames(request("subscribe", topics=[topic]), owner=new_owner)
    else:
        assert "snapshot" in pull(pub, topic, key=key, owner=new_owner)
    assert old_cursor not in pub._list_cursors
    assert pub._list_groups[old_id]["invalid"]
    drain(pub)
    later = events(pipe)[before:]
    assert not any(item.get("page", {}).get("snapshot_id") == old_id for item in later)
    newest = next(item for item in later if item.get("page"))
    assert newest["rev"] == 2
    baseline = assemble(collect_pages(pub, pipe, newest, owner=new_owner))
    assert baseline["data"]["items"] == replacement
    # Attempt Astra's reverse completion: rev=2 is installed, then rev=1 is pulled.
    assert pull(pub, topic, old_cursor, key)["error"]["code"] == "snapshot_required"
    drain(pub)
    final = deepcopy(replacement)
    final[0]["enabled"] = True
    state.record_list(topic, final, key=key)
    assert pub.tick()
    patch = events(pipe)[-1]
    assert patch["kind"] == "patch" and patch["base_rev"] == 2
    merged, missing = protocol.apply_topic_patch(baseline, patch)
    assert not missing and merged["data"]["items"] == final


def test_repeated_subscription_replaces_four_groups_without_owner_capacity_deadlock():
    state, pipe, pub, _ = setup()
    for key in LIST_KEYS:
        state.record_list("nodes.list", rows("nodes.list", 25, key=key, width=60), key=key)
    subscribe(pub, "nodes.list")
    old_ids = set(pub._list_groups)
    subscribe(pub, "nodes.list")
    assert len(pub._list_groups) == len(pub._list_cursors) == 4
    assert old_ids.isdisjoint(pub._list_groups)
    assert not pub._controls
    for page in events(pipe)[-4:]:
        collect_pages(pub, pipe, page)
    assert not pub._list_groups and not pub._list_cursors


def test_restart_clears_busy_initial_page_and_replaces_it_with_small_snapshot():
    state, pipe, pub, _ = setup()
    state.record_list("config.items", rows("config.items", 30, width=60))
    pub.subscription_frames(request("subscribe", topics=["config.items"]), owner="window-7")
    assert pub.tick()
    pipe.mode = "busy"
    assert not pub.tick()
    state.record_list("config.items", rows("config.items"))
    pub.subscription_frames(request("subscribe", topics=["config.items"]), owner="window-7")
    pipe.mode = "complete"
    drain(pub)
    assert len(events(pipe)) == 1
    assert events(pipe)[0]["rev"] == 2 and "page" not in events(pipe)[0]
    assert not pub._list_groups and not pub._list_cursors


@pytest.mark.parametrize("topic", sorted(LIST_TOPICS))
def test_cursor_pages_do_not_copy_current_full_snapshot(topic, monkeypatch):
    state, pipe, pub, _ = setup()
    key = "vanguard" if topic in protocol.KEYED_TOPICS else None
    state.record_list(topic, rows(topic, 35, width=65), key=key)
    subscribe(pub, topic)
    first = next(item for item in events(pipe) if item.get("page"))
    def unexpected_copy(*args, **kwargs):
        raise AssertionError("cursor request copied the current full snapshot")
    monkeypatch.setattr(state, "list_snapshot", unexpected_copy)
    assert len(collect_pages(pub, pipe, first)) > 1
    assert pull(pub, topic, "invalid-cursor", key)["error"]["code"] == "snapshot_required"


def test_cursor_request_still_requires_subscription():
    _, _, pub, _ = setup()
    with pytest.raises(ValueError, match="topic is not subscribed"):
        pull(pub, "config.items", "unknown-cursor")
