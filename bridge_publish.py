"""One-frame periodic publisher and at-most-1-Hz sampling worker."""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from threading import Event, Lock, Thread
import json
import logging
import time
from uuid import uuid4
from typing import Any, Callable

import bridge_protocol as protocol
from bridge_state import PublicationState, LIST_TOPICS, diff_lists, list_slot
from bridge_runtime_access import RuntimeAccess


# T-P6-08 R2: at most this many control frames per publish tick. Only one
# subscription's seed (its initial state snapshots, then its list seeds)
# continues a burst; replies and every other control keep one frame per tick
# (_burst_step). The shell's one subscribe seeds 29 slots, so an all-small
# seed fits the tick after `subscribed`; the cap still bounds a burst of many
# tiny frames (each one validate + encode + WriteFile on the main thread).
# Bytes are bounded separately by max_bytes (the regular frame budget).
CONTROL_FRAMES_PER_TICK = 32
# Bastion trace (CLAUDE_CHANGES §8r, diagnostic, temporary): bastion.pool frames that reached the
# transport, logged to bridge.log; capped per process
POOL_TRACE_MAX = 400
_TRACE = logging.getLogger("tesseract.bridge.trace")


def _ms() -> int:
    return time.time_ns() // 1_000_000


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if hasattr(value, "items"):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


class Publisher:
    def __init__(self, state: PublicationState, transport: Any, *, period: float = 0.2,
                 jobs: Any = None, max_bytes: int = 256 * 1024,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if not 0.1 <= period <= 0.25 or not 4096 <= max_bytes <= protocol.HARD_FRAME_LIMIT:
            raise ValueError("invalid tick budget")
        self.state, self.transport, self.jobs = state, transport, jobs
        self.base_period = period
        self.period = period
        self.max_bytes = max_bytes
        self.clock = clock
        self.next_tick = clock() + period
        self.tick_seq = 0
        self.pool_trace_lines = 0
        self.publisher_thread_cpu_ns = 0
        self.tick_planned_ns = 0
        self.tick_actual_ns = 0
        self.lateness_ns = 0
        self.serialization_ns = 0
        self.production_to_send_ns = 0
        self.pending_write = False
        self.ticks = 0
        self.frames = 0
        self._rates: deque[tuple[float, bool]] = deque()
        self._stop = Event()
        self._thread: Thread | None = None
        self._writer_lock = Lock()
        self._controls: deque[dict[str, Any]] = deque()
        self._wire_revs: dict[str, int] = {}
        self.diagnostics: deque[dict[str, str]] = deque(maxlen=64)
        self.transport_send_cpu_ns = 0
        self.oversize_critical_frames = 0
        self._list_sent: dict[tuple, dict] = {}
        self._list_prepared: dict[tuple, tuple] = {}
        self._list_groups: dict[str, dict] = {}
        self._list_cursors: dict[str, str] = {}
        self._list_paused: set[tuple] = set()
        self._list_owners: dict[tuple, str] = {}
        # T-P2-H2 (33-#12): edges/gaps a sent state snapshot already covers are
        # dropped, never sent after it. The receiver's per-topic edge seq must
        # stay continuous (PROTOCOL §4), so each drop shifts that topic's later
        # wire seqs down by the skipped count. Keyed by topic; state seqs.
        self._seq_offsets: dict[str, int] = {}
        self._seq_submitted: dict[str, int] = {}
        # T-P6-08 R2: per-tick control burst budget (see _control_burst).
        self.control_frames_per_tick = CONTROL_FRAMES_PER_TICK
        self.control_bytes_per_tick = max_bytes
        self._burst: dict[str, Any] | None = None
        self._batch_owed = False
        self.control_burst_peak_frames = 0
        self.control_burst_peak_bytes = 0

    @staticmethod
    def _snapshot_required() -> dict:
        return {"code": "snapshot_required", "message_key": "errors.bridge.snapshot_required", "params": {}}

    @staticmethod
    def _owner(request, owner=None):
        value = owner if owner is not None else request.get("owner", "shell")
        if not isinstance(value, str) or not value:
            raise ValueError("invalid snapshot owner")
        return value

    def _list_size(self, frame):
        sized = {**frame, "sent_at_ms": _ms()}
        if "snapshot" in frame:
            sized["snapshot"] = {**frame["snapshot"], "sent_at_ms": _ms()}
        errors = protocol.validate_frame(sized)
        if errors:
            raise ValueError("invalid_frame:" + ",".join(errors))
        # This may be an aggregate table larger than the transport hard cap;
        # only its individual emitted pages are encode_frame candidates.
        return len(json.dumps(sized, ensure_ascii=False, separators=(",", ":"),
                              allow_nan=False).encode("utf-8")) + 1

    def _invalidate_list_group(self, identity):
        group = self._list_groups[identity]
        if group.get("invalid"):
            return
        self._list_cursors.pop(group.get("cursor"), None)
        self._list_paused.add(group["slot"])
        # Retain only a bounded lightweight failure receipt, not the table.
        self._list_groups[identity] = {"owner": group["owner"], "slot": group["slot"], "invalid": True}

    def _restart_list_slot(self, slot):
        """One wire baseline per slot, regardless of the requesting owner.

        Called under the writer lock before enqueuing a replacement. Invalidation
        receipts precede new pages; old queued pages become errors, and unstarted
        seeds are removed so they cannot recreate an obsolete generation later.
        """
        for identity, group in list(self._list_groups.items()):
            if group["slot"] == slot:
                self._invalidate_list_group(identity)
        for control in list(self._controls):
            if control.get("list_slot") == slot:
                control["list_revoked"] = True
                control["reply"] = self._list_control_frame(control)
                control["list_baseline"] = None
            seeds = control.get("list_seeds")
            if seeds and any(list_slot(self.state.epoch, seed["topic"], seed.get("key")) == slot
                             for seed in seeds):
                if list_slot(self.state.epoch, seeds[0]["topic"], seeds[0].get("key")) == slot:
                    control.pop("seed_control", None)
                    control.pop("seed_needs_page", None)
                control["list_seeds"] = [seed for seed in seeds
                    if list_slot(self.state.epoch, seed["topic"], seed.get("key")) != slot]
                if control["reply_sent"] and not control["topics"] and not control["list_seeds"]:
                    self._controls.remove(control)

    def _expire_list_groups(self):
        for identity, group in list(self._list_groups.items()):
            if group.get("invalid"):
                continue
            _patches, missing = self.state.list_catchup(group["slot"], group["baseline"]["rev"], check_only=True)
            if self.clock() >= group["active_at"] + 30 or missing:
                self._invalidate_list_group(identity)

    def revoke_owner(self, owner: str) -> None:
        """Register this method with P2-03 register_owner_revoke_hook."""
        with self._writer_lock:
            self._list_paused.update(slot for slot, value in self._list_owners.items() if value == owner)
            for identity, group in list(self._list_groups.items()):
                if group["owner"] == owner:
                    self._invalidate_list_group(identity)
            for control in self._controls:
                if control.get("list_owner") == owner and control.get("list_slot"):
                    control["list_revoked"] = True
                if control.get("list_owner") == owner and control.get("list_seeds"):
                    control["list_revoked"] = True
                    if control.get("seed_control"):
                        control["seed_control"]["list_revoked"] = True

    def _freeze_list(self, baseline, owner):
        """Partition once in linear time; budget includes response envelopes."""
        slot = list_slot(self.state.epoch, baseline["topic"], baseline.get("key"))
        if (len(self._list_groups) >= 128 or
                sum(group["owner"] == owner and not group.get("invalid")
                    for group in self._list_groups.values()) >= 4):
            return None
        # Reserve ample space for page, snapshot_res and batch envelopes.
        capacity = self.max_bytes - 1024
        chunks, rows, used = [], [], 0
        for row in baseline["data"]["items"]:
            size = len(json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()) + 1
            if size > capacity:
                return None  # An indivisible row cannot bypass the regular budget.
            if rows and used + size > capacity:
                chunks.append(rows)
                rows, used = [], 0
            rows.append(row)
            used += size
        chunks.append(rows)
        identity = uuid4().hex
        self._list_groups[identity] = {"slot": slot, "owner": owner, "baseline": baseline,
            "chunks": chunks, "index": 0, "cursor": None, "ready": False,
            "queued": True, "active_at": self.clock()}
        self._list_paused.discard(slot)
        return identity

    def _list_page(self, identity):
        group = self._list_groups[identity]
        index = group["index"]
        complete = index == len(group["chunks"]) - 1
        return {**group["baseline"], "data": {"items": group["chunks"][index]},
                "page": {"snapshot_id": identity, "index": index, "complete": complete,
                         "next_cursor": None if complete else uuid4().hex}}

    def _list_control(self, baseline, owner, reply=None, *, revoked=False):
        slot = list_slot(self.state.epoch, baseline["topic"], baseline.get("key"))
        frame = {**reply, "snapshot": baseline} if reply else baseline
        identity = None
        if revoked or self._list_size(frame) > self.max_bytes - 256:
            identity = None if revoked else self._freeze_list(baseline, owner)
            if identity is None:
                frame = {**reply, "error": self._snapshot_required()} if reply else {
                    "t": "error", "epoch": self.state.epoch, "produced_at_ms": _ms(),
                    "owner": owner, "error": {**self._snapshot_required(),
                    "params": {"topic": slot[1], **({"tier": slot[2]} if slot[2] else {})}}}
                self._list_paused.add(slot)
            else:
                page = self._list_page(identity)
                frame = {**reply, "snapshot": page} if reply else page
        else:
            self._list_paused.discard(slot)
        return {"reply": frame, "topics": [], "snapshots": {}, "reply_sent": False,
                "list_slot": slot, "list_owner": owner, "list_group": identity,
                "list_baseline": baseline if "error" not in frame else None}

    def _list_control_frame(self, control):
        identity = control.get("list_group")
        if control.get("list_revoked") or (identity and (
                identity not in self._list_groups or self._list_groups[identity].get("invalid"))):
            frame = control["reply"]
            if frame["t"] == "snapshot_res":
                return {key: value for key, value in {**frame, "error": self._snapshot_required()}.items()
                        if key != "snapshot"}
            return {"t": "error", "epoch": self.state.epoch, "produced_at_ms": _ms(),
                    "owner": control["list_owner"], "error": {**self._snapshot_required(),
                    "params": {"topic": control["list_slot"][1],
                    **({"tier": control["list_slot"][2]} if control["list_slot"][2] else {})}}}
        return control["reply"]

    def _list_control_submitted(self, control, frame):
        if "error" in frame:
            return
        identity = control.get("list_group")
        if identity:
            group = self._list_groups[identity]
            _patches, missing = self.state.list_catchup(group["slot"], group["baseline"]["rev"], check_only=True)
            if missing:
                # A producer may advance while transport.send owns its copy.
                # The next ordered error invalidates even a just-submitted
                # final page; no patch may follow this half-accepted group.
                self._invalidate_list_group(identity)
                return
            page = frame.get("snapshot", frame)["page"]
            self._list_cursors.pop(group["cursor"], None)
            if not page["complete"]:
                group.update(cursor=page["next_cursor"], index=group["index"] + 1,
                             ready=True, queued=False, active_at=self.clock())
                self._list_cursors[group["cursor"]] = identity
                return  # Half a table is never a patch baseline.
            self._list_groups.pop(identity)
        baseline = control["list_baseline"]
        previous = self._list_sent.get(control["list_slot"])
        if previous is None or previous["rev"] <= baseline["rev"]:
            self._list_sent[control["list_slot"]] = baseline
        self._list_prepared.pop(control["list_slot"], None)
        self.state.list_submitted(baseline)

    def _list_candidates(self):
        blocked = {group["slot"] for group in self._list_groups.values()} | self._list_paused
        blocked.update(control["list_slot"] for control in self._controls if control.get("list_slot"))
        blocked.update(list_slot(self.state.epoch, seed["topic"], seed.get("key"))
                       for control in self._controls for seed in control.get("list_seeds", []))
        result = []
        for slot, rev in self.state.list_pending_revisions().items():
            if slot in blocked:
                continue
            cached = self._list_prepared.get(slot)
            if cached is None or cached[0] != rev:
                baseline = self.state.list_snapshot(slot[1], slot[2])
                previous = self._list_sent.get(slot)
                event = baseline
                if previous is not None:
                    delta = diff_lists(previous["data"]["items"], baseline["data"]["items"])
                    if not delta["changed"] and not delta["removed"]:
                        self.state.list_submitted(baseline)
                        continue
                    event = {**baseline, "kind": "patch", "base_rev": previous["rev"], "data": delta}
                    if self._list_size(event) > self.max_bytes - 1024:
                        event = baseline  # Coalesce an over-budget patch into a fresh snapshot.
                cached = (baseline["rev"], event, baseline)
                self._list_prepared[slot] = cached
            event = cached[1]
            if self._list_size(event) > self.max_bytes - 1024:
                if len(self._controls) < 64:
                    self._controls.append(self._list_control(cached[2], self._list_owners.get(slot, "shell")))
                continue
            result.append(event)
        return result

    def subscription_frames(self, request: dict[str, Any], *, owner: str | None = None) -> list[dict[str, Any]]:
        """Enqueue a subscription barrier; return unstamped preview templates.

        Only tick submits these frames. Callers must not send the previews.
        Missing first samples keep the barrier closed until a complete DTO exists.
        """
        if protocol.validate_frame(request) or request["t"] != "subscribe" or request["epoch"] != self.state.epoch:
            raise ValueError("invalid subscription request")
        with self._writer_lock:
            if len(self._controls) >= 64:
                raise ValueError("control queue full")
            list_owner = self._owner(request, owner)
            history_already = self.state.history_sent
            snapshots = self.state.subscribe(request["topics"])
            for seed in snapshots:
                if seed["topic"] in LIST_TOPICS:
                    slot = list_slot(self.state.epoch, seed["topic"], seed.get("key"))
                    self._restart_list_slot(slot)
                    self._list_owners[slot] = list_owner
            reply = {"t": "subscribed", "epoch": self.state.epoch,
                     "produced_at_ms": _ms(), "id": request["id"],
                     "topics": list(request["topics"])}
            cached = {item["topic"]: item for item in snapshots if item["topic"] not in LIST_TOPICS}
            topics = [topic for topic in request["topics"]
                       if topic in protocol.V1_SNAPSHOT_TOPICS and topic not in LIST_TOPICS
                      and (topic != "traffic.history" or not history_already)]
            self._controls.append({"reply": reply, "topics": topics,
                                   "snapshots": cached, "reply_sent": False,
                                   "list_seeds": [item for item in snapshots if item["topic"] in LIST_TOPICS],
                                   "list_owner": list_owner})
            return deepcopy([reply] + snapshots)

    def snapshot_response(self, request: dict[str, Any], *, owner: str | None = None) -> dict[str, Any]:
        if protocol.validate_frame(request) or request["t"] != "snapshot_req" or request["epoch"] != self.state.epoch:
            raise ValueError("invalid snapshot request")
        with self._writer_lock:
            if len(self._controls) >= 64:
                raise ValueError("control queue full")
            if request["topic"] in LIST_TOPICS:
                return self._list_snapshot_response(request, self._owner(request, owner))
            snapshot = self.state.snapshot(request["topic"])
            response = {"t": "snapshot_res", "epoch": self.state.epoch,
                        "produced_at_ms": snapshot["produced_at_ms"] if snapshot else _ms(),
                        "id": request["id"], "topic": request["topic"]}
            if snapshot is None:
                response["error"] = {"code": "snapshot_required",
                                     "message_key": "errors.bridge.snapshot_required", "params": {}}
            else:
                response["snapshot"] = snapshot
                self.state.restore_after_snapshot(request["topic"])
            self._controls.append({"reply": response, "topics": [],
                                   "snapshots": {}, "reply_sent": False})
            return deepcopy(response)

    def _list_snapshot_response(self, request, owner):
        slot = list_slot(self.state.epoch, request["topic"], request.get("key"))
        # Cursor requests use the frozen group, never the current full table.
        self.state.require_list_subscription(slot[1], slot[2])
        reply = {"t": "snapshot_res", "epoch": self.state.epoch, "produced_at_ms": _ms(),
                 "id": request["id"], "topic": request["topic"]}
        if slot[2] is not None:
            reply["key"] = slot[2]
        self._expire_list_groups()
        if "cursor" not in request:
            baseline = self.state.list_snapshot(slot[1], slot[2])
            self._restart_list_slot(slot)
            self._list_owners[slot] = owner
            control = self._list_control(baseline, owner, reply)
        else:
            identity = self._list_cursors.get(request["cursor"])
            group = self._list_groups.get(identity)
            if (group is None or group.get("invalid") or group["owner"] != owner
                    or group["slot"] != slot or not group["ready"] or group["queued"]):
                control = {"reply": {**reply, "error": self._snapshot_required()},
                           "topics": [], "snapshots": {}, "reply_sent": False}
            else:
                group["queued"] = True
                group["active_at"] = self.clock()
                control = {"reply": {**reply, "snapshot": self._list_page(identity)},
                    "topics": [], "snapshots": {}, "reply_sent": False,
                    "list_slot": slot, "list_owner": owner, "list_group": identity,
                    "list_baseline": group["baseline"]}
        self._controls.append(control)
        return deepcopy(control["reply"])

    def _submit(self, frame: dict[str, Any], stamp: int) -> str:
        """Assign transmission revisions and timestamps only at submission.

        State revs are internal coalescing/ack tokens. Replayed cached edges keep
        their original seq and production time; their wire rev follows the new
        snapshot baseline. A final latest snapshot restores the current state.
        """
        wire = deepcopy(frame)
        revisions = self._wire_revs.copy()
        submitted = {}
        def event(item):
            item["sent_at_ms"] = stamp
            topic = item["topic"]
            if topic != "jobs" and topic not in LIST_TOPICS:
                revisions[topic] = revisions.get(topic, 0) + 1
                item["rev"] = revisions[topic]
                if item.get("kind") in ("edge", "gap") and "seq" in item:
                    submitted[topic] = max(submitted.get(topic, 0), item["seq"])
                    offset = self._seq_offsets.get(topic, 0)
                    if offset:
                        # Covered seqs were dropped (_drop_covered): the wire
                        # numbering continues from the last seq actually sent.
                        item["seq"] -= offset
                        if item["kind"] == "gap":
                            item["data"]["from_seq"] -= offset
                            item["data"]["to_seq"] -= offset
        wire["sent_at_ms"] = stamp
        if wire["t"] == "batch":
            for item in wire["items"]:
                event(item)
        elif wire["t"] == "evt":
            event(wire)
        elif "snapshot" in wire:
            event(wire["snapshot"])
        stamp = _ms()
        wire["sent_at_ms"] = stamp
        for item in wire.get("items", []):
            item["sent_at_ms"] = stamp
        if "snapshot" in wire:
            wire["snapshot"]["sent_at_ms"] = stamp
        burst, size = self._burst, 0
        if burst is not None:
            # T-P6-08 R2: a control burst stops before the frame that would
            # exceed this tick's frame cap or byte budget. The first frame of
            # a tick always goes, as before. No state was changed above.
            try:
                size = len(json.dumps(wire, ensure_ascii=False, separators=(",", ":"),
                                      allow_nan=False).encode("utf-8")) + 1
            except (TypeError, ValueError):
                size = 0  # The transport's own encode reports the defect.
            if burst["frames"] and (burst["frames"] >= self.control_frames_per_tick
                                    or burst["bytes"] + size > self.control_bytes_per_tick):
                burst["deferred"] = burst["budget_spent"] = True
                return "busy"
        started = time.thread_time_ns()
        try:
            # Includes the transport's second encoding and submission CPU, not
            # claimed to be serialization alone or total GIL time.
            outcome = self.transport.send(wire)
        finally:
            self.transport_send_cpu_ns += time.thread_time_ns() - started
        if outcome not in {"complete", "pending", "busy"}:
            raise ValueError("invalid transport outcome")
        if outcome != "busy":
            if burst is not None:
                burst["frames"] += 1
                burst["bytes"] += size
                self.control_burst_peak_frames = max(self.control_burst_peak_frames, burst["frames"])
                self.control_burst_peak_bytes = max(self.control_burst_peak_bytes, burst["bytes"])
            self._wire_revs = revisions
            self._last_sent_ms = stamp
            for topic, seq in submitted.items():
                self._seq_submitted[topic] = max(self._seq_submitted.get(topic, 0), seq)
            self._trace_pool_tx(wire)
        self.pending_write = outcome in {"pending", "busy"}
        return outcome

    def _trace_pool_tx(self, wire: dict[str, Any]) -> None:
        """Bastion trace (CLAUDE_CHANGES §8r): each bastion.pool frame handed to the transport,
        with its wire rev and seq and the count it carries; a replacement reply without a
        snapshot logs its error code. Numbers and fixed codes only."""
        if self.pool_trace_lines >= POOL_TRACE_MAX:
            return
        if wire["t"] == "snapshot_res" and wire.get("topic") == "bastion.pool" and "snapshot" not in wire:
            self.pool_trace_lines += 1
            _TRACE.info("bastion.pool tx via=snapshot_res kind=none error=%s",
                      (wire.get("error") or {}).get("code", "-"))
            return
        items = (wire["items"] if wire["t"] == "batch" else [wire] if wire["t"] == "evt"
                 else [wire["snapshot"]] if "snapshot" in wire else [])
        for item in items:
            if item.get("topic") != "bastion.pool" or self.pool_trace_lines >= POOL_TRACE_MAX:
                continue
            self.pool_trace_lines += 1
            data = item.get("data") if isinstance(item.get("data"), dict) else {}
            _TRACE.info("bastion.pool tx via=%s kind=%s rev=%s seq=%s alive=%s", wire["t"], item.get("kind", "-"),
                      item.get("rev", "-"), item.get("seq", "-"), data.get("runtime_alive", "-"))

    def _drop_covered(self, snapshot: dict[str, Any]) -> None:
        """T-P2-H2 (33-#12): a state snapshot just submitted (subscription
        barrier or snapshot_res) is the latest projection; every still-queued
        edge or gap of its topic with an older or equal state rev is already
        reflected in it. Sending those afterwards made the receiver step
        back through older states (e.g. shutdown finished -> recovering ->
        ... -> finished). They are acknowledged as sent without reaching the
        wire; the skipped seqs shift this topic's later wire seqs (_submit).
        T-P2-H6: reads only this topic's receipts (state.pending_for), not a
        deep copy of the whole queue, and stays out of the log CPU account."""
        topic = snapshot.get("topic")
        if (snapshot.get("kind") != "snapshot" or topic in LIST_TOPICS
                or topic in ("jobs", "logs.batch") or not isinstance(snapshot.get("rev"), int)):
            return
        # Known window (predates H6, same with the old pending()): pending_for
        # and ack lock separately; a producer overflowing the ring in between
        # sends a covered edge through ack's rescue branch (dropped -= 1).
        covered = [item for item in self.state.pending_for(topic)
                   if item["kind"] in ("edge", "gap") and item["rev"] <= snapshot["rev"]]
        if not covered:
            return
        top = max(item["seq"] for item in covered)
        last = self._seq_submitted.get(topic, 0)
        if top > last:
            self._seq_offsets[topic] = self._seq_offsets.get(topic, 0) + (top - last)
            self._seq_submitted[topic] = top
        self.state.ack(covered, settle_log_cpu=False)

    def _reread_snapshot_reply(self, control: dict[str, Any]) -> dict[str, Any]:
        """T-P2-H2 (33-#11): a state snapshot_res answers from the state at
        dequeue, not at request time. A reply held behind a subscription
        barrier (or backpressure) whose topic has a baseline by now answers
        that snapshot instead of snapshot_required; a baseline withdrawn
        meanwhile answers snapshot_required."""
        reply = control["reply"]
        try:
            snapshot = self.state.snapshot(reply["topic"])
        except ValueError:
            return reply
        fresh = {key: value for key, value in reply.items() if key not in ("snapshot", "error")}
        if snapshot is None:
            fresh["error"] = self._snapshot_required()
        else:
            fresh["snapshot"] = snapshot
            fresh["produced_at_ms"] = snapshot["produced_at_ms"]
            self.state.restore_after_snapshot(reply["topic"])
        control["reply"] = fresh
        return fresh

    def _list_seed_tick(self, control: dict[str, Any], stamp: int) -> bool | None:
        """Submit this subscription control's next list seed, if one is ready.

        True/False as _control_tick; None when the control has no seed that
        may go now (none left, or an initial page group waits for an owner
        slot). Seeds keep their subscription order.
        """
        if not control["reply_sent"] or not control.get("list_seeds"):
            return None
        if not control.get("list_revoked") and not control.get("seed_control"):
            seed = control["list_seeds"][0]
            if self.state.list_revision(seed["topic"], seed.get("key")) != seed["rev"]:
                control["list_seeds"][0] = self.state.list_snapshot(seed["topic"], seed.get("key"))
                control.pop("seed_needs_page", None)
            if "seed_needs_page" not in control:
                control["seed_needs_page"] = self._list_size(control["list_seeds"][0]) > self.max_bytes - 256
            # Initial seeds wait for a free owner slot. Continue scanning
            # controls so cursor pulls can finish the four existing groups.
            if (control["seed_needs_page"] and
                    sum(group["owner"] == control["list_owner"] and not group.get("invalid")
                        for group in self._list_groups.values()) >= 4):
                return None
        if not self._burst_step(control):
            return False
        seed = control["list_seeds"][0]
        child = control.get("seed_control")
        if child is None:
            child = self._list_control(seed, control["list_owner"],
                                       revoked=control.get("list_revoked", False))
            child["list_revoked"] = control.get("list_revoked", False)
            control["seed_control"] = child
        frame = self._list_control_frame(child)
        if self._submit(frame, stamp) == "busy":
            return False
        self._list_control_submitted(child, frame)
        if child.get("list_group") and self._burst is not None:
            # A first page of a multi-page group fills the tick's byte budget
            # by construction (_freeze_list); it ends this tick's burst and
            # counts as a spent budget, so the next tick is the batch's turn.
            self._burst["control"] = None
            self._burst["budget_spent"] = True
        control["list_seeds"].pop(0)
        control.pop("seed_control")
        control.pop("seed_needs_page", None)
        if not control["list_seeds"] and not control["topics"]:
            self._controls.remove(control)
        self.frames += 1
        return True

    def _burst_step(self, seed_of: dict[str, Any] | None) -> bool:
        """T-P6-08 R2: may this control frame go in the current tick?

        The first control frame of a tick always may. A further one only
        continues the same subscription control's seed: its initial state
        snapshots, then its list seeds (``seed_of`` is that control). A
        reply (``subscribed``/``snapshot_res``) or a list error receipt
        (``seed_of`` None) still takes a tick of its own, and so does a frame
        of any other control, so each burst step sends exactly the frame the
        next one-frame tick would have sent. A refused step is deferred
        unchanged; byte and frame budgets are checked in _submit.
        """
        burst = self._burst
        if burst is None or not burst["frames"]:
            if burst is not None:
                burst["control"] = seed_of
            return True
        if seed_of is not None and burst.get("control") is seed_of:
            return True
        burst["deferred"] = True
        return False

    def _control_tick(self, stamp: int) -> bool | None:
        self._expire_list_groups()
        for identity, group in list(self._list_groups.items()):
            if group.get("invalid"):
                slot = group["slot"]
                frame = {"t": "error", "epoch": self.state.epoch, "produced_at_ms": _ms(),
                         "owner": group["owner"], "error": {**self._snapshot_required(),
                         "params": {"topic": slot[1], **({"tier": slot[2]} if slot[2] else {})}}}
                if not self._burst_step(None) or self._submit(frame, stamp) == "busy":
                    return False
                self._list_groups.pop(identity)
                self.frames += 1
                return True
        # An unavailable initial DTO only blocks its own topic. Keep replies,
        # other initial snapshots and jobs progressing, without fabricating a
        # baseline or allowing a later snapshot to overtake that topic's barrier.
        blocked = set()
        for control in self._controls:
            topic = None
            if not control["reply_sent"]:
                frame = self._list_control_frame(control) if control.get("list_slot") else control["reply"]
                if frame.get("topic") in blocked:
                    blocked.update(control["topics"])
                    continue
                if not self._burst_step(None):
                    return False
                if (frame["t"] == "snapshot_res" and not control.get("list_slot")
                        and frame.get("topic") not in LIST_TOPICS):
                    frame = self._reread_snapshot_reply(control)
            else:
                if not self._burst_step(control):
                    return False
                frame = None
                for candidate in list(control["topics"]):
                    if candidate in blocked:
                        continue
                    frame = control["snapshots"].get(candidate)
                    if frame is not None and self.state.baseline_revoked(frame):
                        control["snapshots"].pop(candidate, None)
                        control["topics"].remove(candidate)
                        frame = None
                        continue  # Let the owner's queued gap reach the receiver.
                    if frame is None:
                        frame = self.state.snapshot(candidate)
                        if frame is None:
                            if self.state.baseline_unavailable(candidate):
                                control["topics"].remove(candidate)
                            continue
                        control["snapshots"][candidate] = frame
                        self.state.restore_after_snapshot(candidate)
                        if candidate == "traffic.history":
                            self.state.claim_history()
                    topic = candidate
                    break
                if frame is None:
                    # T-P6-08 R1: list seeds go only after every ready state
                    # snapshot of this control. A state topic still without
                    # its first DTO does not hold the seeds back.
                    seeded = self._list_seed_tick(control, stamp)
                    if seeded is not None:
                        return seeded
                    if not control["topics"] and not control.get("list_seeds"):
                        self._controls.remove(control)
                        return None
                    blocked.update(control["topics"])
                    continue
            # A replacement response can wait under backpressure past revocation.
            if "snapshot" in frame and self.state.baseline_revoked(frame["snapshot"]):
                frame = {key: value for key, value in frame.items() if key != "snapshot"}
                frame["error"] = self._snapshot_required()
                control["reply"] = frame
            if self._submit(frame, stamp) == "busy":
                return False
            snapshot = frame if frame["t"] == "evt" else frame.get("snapshot")
            if control.get("list_slot"):
                self._list_control_submitted(control, frame)
            elif snapshot is not None:
                self._drop_covered(snapshot)
                self.state.snapshot_submitted(snapshot)
            self.frames += 1
            if control["reply_sent"]:
                control["topics"].remove(topic)
            else:
                control["reply_sent"] = True
            if not control["topics"] and not control.get("list_seeds"):
                self._controls.remove(control)
            return True
        return None  # No control frame ready; unrelated batch items may proceed.

    def _control_burst(self, stamp: int, burst: dict[str, Any]) -> tuple[bool, bool]:
        """T-P6-08 R2: submit consecutive control frames within one tick.

        Continues while the transport answers ``complete``, the next frame
        continues the same subscription seed (_burst_step) and this tick's
        control budget (CONTROL_FRAMES_PER_TICK frames, ``max_bytes`` bytes,
        checked in _submit before the send) is not spent. Returns
        ``(sent, proceed)``: ``proceed`` is False when the transport is
        pending or busy. A refused or over-budget frame is deferred exactly
        like ``busy``: unsent, it is the first frame of a later tick, so
        nothing is lost or repeated. _tick decides whether the ordinary batch
        follows (nothing sent) or takes the next tick (frames sent).
        """
        sent = False
        self._burst = burst
        try:
            while True:
                depth = len(self._controls) + len(self._list_groups)
                result = self._control_tick(stamp)
                if result is None:
                    if len(self._controls) + len(self._list_groups) < depth:
                        continue  # An exhausted control was retired; rescan.
                    return sent, True
                if result is False:
                    if burst["deferred"]:
                        return sent, True
                    return sent, False  # Transport busy.
                sent = True
                if self.pending_write:
                    return sent, False
        finally:
            self._burst = None

    def _owe_batch(self, burst: dict[str, Any]) -> bool:
        return bool((self._controls or self._list_groups)
                    and (burst["budget_spent"] or self.pending_write))

    def _batch_items(self, candidates, stamp):
        # Encode each candidate once to size a prefix. No repeated whole-batch
        # serialization when a budget is exceeded. The transport encodes once.
        self._oversize_selected = False
        header = {"t": "batch", "epoch": self.state.epoch,
                  "produced_at_ms": stamp, "sent_at_ms": stamp,
                  "tick_seq": self.tick_seq + 1, "items": []}
        size = len(json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode()) + 1
        chosen, blocked = [], set()
        edge_topics = {item["topic"] for item in candidates if item["kind"] != "snapshot"}
        for item in candidates:
            topic = item["topic"]
            if topic in blocked or len(chosen) >= 128:
                continue
            # A coalesced snapshot cannot overtake an unsent edge of its topic.
            if item["kind"] == "snapshot" and topic in edge_topics:
                if any(other["topic"] == topic and other["kind"] != "snapshot"
                       and other not in chosen for other in candidates):
                    continue
            started = time.thread_time_ns()
            try:
                # Allow for the largest possible new wire revision and earlier
                # production timestamp in the envelope (conservative sizing).
                sized = {**item, "sent_at_ms": stamp,
                         "rev": max(item["rev"], self._wire_revs.get(topic, 0) + 128)}
                length = len(protocol.encode_frame(sized)) - 1 + 24
            except ValueError as exc:
                self.diagnostics.append({"source": topic, "error": str(exc).split(":")[0]})
                if topic == "logs.batch" and item["kind"] == "edge":
                    self.state.drop_logs(item)
                blocked.add(topic)
                continue
            finally:
                self.serialization_ns += time.thread_time_ns() - started
            next_size = size + length + bool(chosen)
            if next_size > self.max_bytes:
                if topic == "logs.batch" and item["kind"] == "edge" and length > self.max_bytes:
                    self.state.drop_logs(item)
                    blocked.add(topic)
                    continue
                if not chosen and topic != "logs.batch" and next_size <= protocol.HARD_FRAME_LIMIT:
                    # An indivisible critical DTO is retained, exceptionally
                    # submitted alone above the regular budget, below hard cap.
                    self._oversize_selected = True
                    return [item]
                blocked.add(topic)
                continue
            chosen.append(item)
            size = next_size
        return chosen

    def tick(self) -> bool:
        with self._writer_lock:
            return self._tick()

    def _tick(self) -> bool:
        cpu_start = time.thread_time_ns()
        now = self.clock()
        planned = self.next_tick
        self.tick_planned_ns = int(planned * 1e9)
        self.tick_actual_ns = int(now * 1e9)
        self.lateness_ns = max(0, self.tick_actual_ns - self.tick_planned_ns)
        self.period = min(0.5, max(self.base_period, self.period * 1.5)) if now - planned > self.period else self.base_period
        self.next_tick = now + self.period
        self.ticks += 1
        sent = False
        prepared_jobs = []
        try:
            if self.transport.poll_write() == "pending":
                self.pending_write = True
                return False
            self.pending_write = False
            stamp = _ms()
            burst = {"frames": 0, "bytes": 0, "deferred": False, "budget_spent": False}
            batch_turn, self._batch_owed = self._batch_owed, False
            if (self._controls or self._list_groups) and not batch_turn:
                sent, proceed = self._control_burst(stamp, burst)
                if sent:
                    # T-P6-08 R2: a tick that wrote control frames writes no
                    # batch (as before). If the budget ran out (or the pipe
                    # filled) with controls left, the next tick is the batch's
                    # turn, so a long seed never holds jobs, edges or logs for
                    # more than one tick.
                    self._batch_owed = self._owe_batch(burst)
                    return sent
                if not proceed:
                    return sent
            blocked = {topic for control in self._controls for topic in control["topics"]}
            # A ready reply that waits for a batch turn (T-P6-08) still goes
            # before any batch item of its topic, as when controls had every tick.
            blocked.update(control["reply"].get("topic") for control in self._controls
                           if not control["reply_sent"])
            pending = [item for item in self.state.pending(128) if item["topic"] not in blocked]
            pending += self._list_candidates()
            edges = [item for item in pending if item["kind"] != "snapshot"]
            states = [item for item in pending if item["kind"] == "snapshot"]
            prepared_jobs = self.jobs.prepare_for_send(stamp, 128) if self.jobs else []
            # Reserved jobs must not be starved by a full ring or a large log.
            critical = [item for item in edges if item["topic"] != "logs.batch"]
            logs = [item for item in edges if item["topic"] == "logs.batch"]
            items = self._batch_items(prepared_jobs + critical + logs + states, stamp)
            if not items:
                if self._controls or (batch_turn and self._list_groups):
                    # Nothing to batch: controls may use this tick, within
                    # this tick's control budget (T-P6-08 R2).
                    sent, _proceed = self._control_burst(stamp, burst)
                    if sent:
                        self._batch_owed = self._owe_batch(burst)
                return sent
            selected_jobs = [item for item in items if item["topic"] == "jobs"]
            if prepared_jobs and len(selected_jobs) != len(prepared_jobs):
                self.jobs.abort_send()
                prepared_jobs = self.jobs.prepare_for_send(stamp, len(selected_jobs)) if selected_jobs else []
            frame = {"t": "batch", "epoch": self.state.epoch,
                     "produced_at_ms": min(item["produced_at_ms"] for item in items),
                     "tick_seq": self.tick_seq + 1, "items": items}
            outcome = self._submit(frame, stamp)
            if outcome == "busy":
                return sent
            self.tick_seq += 1
            self.frames += 1
            self.oversize_critical_frames += self._oversize_selected
            sent = True
            self.pending_write = outcome == "pending"
            stamp = self._last_sent_ms
            self.production_to_send_ns = max(
                self.production_to_send_ns,
                max(max(0, (stamp - item["produced_at_ms"]) * 1_000_000)
                    for item in items))
            for item in items:
                if item["topic"] in LIST_TOPICS:
                    slot = list_slot(self.state.epoch, item["topic"], item.get("key"))
                    baseline = self._list_prepared[slot][2]
                    self._list_sent[slot] = baseline
                    self.state.list_submitted(baseline)
                    self._list_prepared.pop(slot)
            self.state.ack([item for item in items if item["topic"] != "jobs" and item["topic"] not in LIST_TOPICS])
            if selected_jobs:
                self.jobs.ack_sent(selected_jobs)
                prepared_jobs = []
            return True
        except Exception as exc:
            self.diagnostics.append({"source": "publisher", "error": type(exc).__name__})
            return False
        finally:
            if prepared_jobs:
                self.jobs.abort_send()
            self.publisher_thread_cpu_ns += time.thread_time_ns() - cpu_start
            self._rates.append((now, sent))
            while self._rates and self._rates[0][0] < now - 1:
                self._rates.popleft()

    def run(self) -> None:
        start = time.thread_time_ns()
        prior = self.publisher_thread_cpu_ns
        try:
            while not self._stop.wait(max(0, self.next_tick - self.clock())):
                self.tick()
                self.publisher_thread_cpu_ns = prior + time.thread_time_ns() - start
        finally:
            # Windows GetThreadTimes is coarse: summing sub-quantum ticks can
            # report zero. The thread lifetime is the authoritative total.
            self.publisher_thread_cpu_ns = prior + time.thread_time_ns() - start

    def start(self) -> None:
        self._thread = Thread(target=self.run, name="bridge-publisher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def metrics(self) -> dict[str, Any]:
        return {"publisher_thread_cpu_ns": self.publisher_thread_cpu_ns,
                "tick_planned_ns": self.tick_planned_ns,
                "tick_actual_ns": self.tick_actual_ns, "lateness_ns": self.lateness_ns,
                "serialization_ns": self.serialization_ns,
                "transport_send_cpu_ns": self.transport_send_cpu_ns,
                "production_to_send_ns": self.production_to_send_ns,
                "queue_depth": self.state.depth + (self.jobs.pending if self.jobs else 0),
                "merged": self.state.merged, "dropped": self.state.dropped,
                "log_records_dropped": self.state.log_records_dropped,
                "state_lock_wait_ns": self.state.lock_wait_ns,
                "state_lock_held_ns": self.state.lock_held_ns,
                "control_depth": len(self._controls),
                "oversize_critical_frames": self.oversize_critical_frames,
                "pending_write": self.pending_write,
                "ticks_per_second": len(self._rates),
                "frames_per_second": sum(sent for _, sent in self._rates)}


class SamplingWorker:
    """Sample only public compact DTOs, with independent source failure recovery.

    All four sources pass through RuntimeAccess. Route reads only project an
    already published snapshot; no refresh, controller read or probe is allowed.
    None from tiers means an owner changed: retain the last observed summary
    and retry next period, without splicing it into a newer lifecycle sample.
    Authoritative instantaneous edges are injected via state.record_edge().
    """
    def __init__(self, state: PublicationState, runtime: Any, *,
                 period: float = 1.0, sampled_topics: Any = None) -> None:
        if period < 1.0:
            raise ValueError("sampling exceeds 1 Hz")
        self.state = state
        self.runtime = runtime if isinstance(runtime, RuntimeAccess) else RuntimeAccess(runtime)
        self.period = period
        self.worker_thread_cpu_ns = 0
        self._last: dict[str, Any] = {}
        self._stop = Event()
        self._thread: Thread | None = None
        self.samples = 0
        self.sampled_topics = sampled_topics
        self.diagnostics: deque[dict[str, str]] = deque(maxlen=64)
        self.source_costs: dict[str, dict[str, int]] = {}

    def _observe(self, name, action):
        cpu, wall = time.thread_time_ns(), time.perf_counter_ns()
        costs = self.source_costs.setdefault(name, {"cpu_ns": 0, "elapsed_ns": 0,
                                                   "successes": 0, "failures": 0})
        try:
            result = action()
            costs["successes"] += 1
            return result
        except Exception as exc:
            costs["failures"] += 1
            self.diagnostics.append({"source": name, "error": type(exc).__name__})
        finally:
            costs["cpu_ns"] += time.thread_time_ns() - cpu
            costs["elapsed_ns"] += time.perf_counter_ns() - wall

    def _service(self):
        lifecycle = _plain(self.runtime.lifecycle_status())
        active = lifecycle["runtime"]["startup_phase"] not in {"stopped", "failed"}
        semantic = deepcopy(lifecycle)
        for key in ("sampled_at_ms", "snapshot_revision", "uptime_s"):
            semantic["freshness"].pop(key, None)
        if active or semantic != self._last.get("service.state"):
            self.state.record_state("service.state", lifecycle)
        self._last["service.state"] = semantic
        self._last["active"] = active

    def _route(self):
        route = _plain(self.runtime.published_route_status())
        # Expiry policy and its invalidity signal belong to the runtime getter.
        # Until RT-4 supplies them, forwarding valid=True is not expiry acceptance;
        # this worker must neither invent a TTL nor refresh the published route.
        # Validity/ownership can change without a new publication revision.
        # The wall-clock projection of the same observation may drift by 1 ms.
        semantic = {key: value for key, value in route.items() if key != "observed_at_ms"}
        if semantic != self._last.get("route.published"):
            self.state.record_state("route.published", route)
        self._last["route.published"] = deepcopy(semantic)

    def _core(self):
        fact = _plain(self.runtime.core_fact())
        core = {"running": bool(fact["running"]), "pid": fact["pid"],
                "version": fact.get("version"), "generation": fact["core_generation"],
                "cleanup_pending": bool(fact["cleanup_pending"])}
        if core != self._last.get("core.info"):
            self.state.record_state("core.info", core)
        self._last["core.info"] = core

    def _tiers(self):
        raw = self.runtime.tiers_summary_status()
        if raw is None:
            return  # Cross-domain owner change; the public API rejects this sample.
        _revision, summary = raw
        summary = _plain(summary)
        active = self._last.get("active")
        stopped = active is False and self._last.get("tiers.active") is not False
        if summary != self._last.get("tiers.summary") or stopped:
            self.state.record_state("tiers.summary", summary)
        self._last["tiers.summary"] = deepcopy(summary)
        self._last["tiers.active"] = active

    def sample(self) -> None:
        started = time.thread_time_ns()
        try:
            self.samples += 1
            self._observe("service.state", self._service)
            self._observe("route.published", self._route)
            self._observe("core.info", self._core)
            self._observe("tiers.summary", self._tiers)
            if self.sampled_topics is not None:
                self._observe("p2.readonly", lambda: self.sampled_topics.sample(self._observe))
        finally:
            self.worker_thread_cpu_ns += time.thread_time_ns() - started

    def run(self) -> None:
        start = time.thread_time_ns()
        prior = self.worker_thread_cpu_ns
        next_at = time.monotonic()
        try:
            while not self._stop.is_set():
                self.sample()
                self.worker_thread_cpu_ns = prior + time.thread_time_ns() - start
                next_at = max(next_at + self.period, time.monotonic() + self.period)
                self._stop.wait(max(0, next_at - time.monotonic()))
        finally:
            self.worker_thread_cpu_ns = prior + time.thread_time_ns() - start

    def start(self) -> None:
        self._thread = Thread(target=self.run, name="bridge-sampler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2) -> bool:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(0, timeout))
        return self._thread is None or not self._thread.is_alive()
