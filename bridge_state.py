"""Bounded, wakeup-free publication state for one sidecar epoch."""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from copy import deepcopy
import json
from threading import Lock, local
from time import perf_counter_ns, thread_time_ns, time_ns
from typing import Any

from bridge_protocol import V1_SNAPSHOT_TOPICS, V1_TOPICS, TOPIC_KINDS, validate_topic_payload
import bridge_protocol as protocol


def _ms() -> int:
    return time_ns() // 1_000_000


_SCALARS = (str, int, float, bool, type(None))


def _copy_log_payload(value):
    """Own a validated flat log DTO cheaply; extensions keep deep-copy semantics."""
    rows = value.get("records")
    if (isinstance(rows, list) and
            all(type(v) in _SCALARS for k, v in value.items() if k != "records") and
            all(isinstance(row, dict) and all(type(v) in _SCALARS for v in row.values())
                for row in rows)):
        return {**value, "records": [dict(row) for row in rows]}
    return deepcopy(value)


def _copy_edge(value):
    if value["topic"] == "logs.batch" and value["kind"] == "edge":
        return {**value, "data": _copy_log_payload(value["data"])}
    return deepcopy(value)


LIST_TOPICS = protocol.KEYED_TOPICS | {"bastion.sources", "config.items"}
LIST_KEYS = ("vanguard", "sentinel", "bastion", "gateway")


def list_slot(epoch: str, topic: str, key: str | None = None) -> tuple:
    if topic not in LIST_TOPICS or (topic in protocol.KEYED_TOPICS
            and key not in LIST_KEYS) or (topic not in protocol.KEYED_TOPICS and key is not None):
        raise ValueError("invalid list topic/key")
    return epoch, topic, key


def diff_lists(previous: list[dict], current: list[dict]) -> dict[str, list]:
    """Linear ID diff; ordering alone is not a change. Own replacement rows."""
    def indexed(rows):
        result = {}
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                    or not row["id"] or row["id"] in result):
                raise ValueError("invalid or duplicate list ID")
            result[row["id"]] = row
        return result
    old, new = indexed(previous), indexed(current)
    return {"changed": [deepcopy(row) for identity, row in new.items()
                        if identity not in old or old[identity] != row],
            "removed": [identity for identity in old if identity not in new]}


class PublicationState:
    def __init__(self, epoch: str, *, edge_capacity: int = 128,
                 list_history_capacity: int = 32, list_history_bytes: int = 256 * 1024) -> None:
        if not epoch or min(edge_capacity, list_history_capacity, list_history_bytes) < 1:
            raise ValueError("invalid epoch or edge capacity")
        self.epoch = epoch
        self.edge_capacity = edge_capacity
        self._lock = Lock()
        self._latest: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, dict[str, Any]] = {}
        self._edges: deque[dict[str, Any]] = deque()
        # Loss bookkeeping is separate from the DTO ring: at most one compact
        # interval per edge topic (five P1 topics), regardless of producer load.
        self._gaps: dict[str, dict[str, Any]] = {}
        self._revs: dict[str, int] = {}
        self._seqs: dict[str, int] = {}
        self._sent_seqs: dict[str, int] = {}
        self._invalidated_revs: dict[str, int] = {}
        self._subscribed: set[str] = set()
        self._history_sent = False
        self.merged = 0
        self.dropped = 0
        self.log_records_dropped = 0
        self._settled_log_records_dropped = 0
        self._log_drop_counts = dict.fromkeys(("debug", "info", "warn", "error", "critical"), 0)
        self._settled_log_drop_counts = dict(self._log_drop_counts)
        self.lock_wait_ns = 0
        self.lock_held_ns = 0
        self._publication_work = local()
        self._log_publication_cpu_ns = 0
        # Ten fixed slots, one latest pending baseline per slot. A bounded
        # journal retains catch-up patches for frozen snapshots, never a tick
        # projection or an unbounded chain of complete tables.
        self._list_latest: dict[tuple, dict] = {}
        self._list_pending: dict[tuple, dict] = {}
        self._list_history: dict[tuple, deque] = {}
        self._list_history_sizes: dict[tuple, int] = {}
        self.list_history_capacity = list_history_capacity
        self.list_history_bytes = list_history_bytes

    def record_list(self, topic: str, items: list[dict], *, key: str | None = None) -> bool:
        """Accept a changed, already projected DTO; never call business sources."""
        slot = list_slot(self.epoch, topic, key)
        if not isinstance(items, list):
            raise ValueError("invalid list DTO")
        owned = deepcopy(items)
        event = {"t": "evt", "epoch": self.epoch, "produced_at_ms": _ms(),
                 "sent_at_ms": 0, "topic": topic, "rev": 1,
                 "kind": "snapshot", "data": {"items": owned}}
        if key is not None:
            event["key"] = key
        if protocol.validate_frame(event):
            raise ValueError("invalid list DTO")
        with self._locked():
            previous = self._list_latest.get(slot)
            delta = diff_lists(previous["data"]["items"] if previous else [], owned)
            if previous is not None and not delta["changed"] and not delta["removed"]:
                return False
            event.pop("sent_at_ms")
            event["rev"] = previous["rev"] + 1 if previous else 1
            if previous is not None:
                patch = {**event, "kind": "patch", "base_rev": previous["rev"], "data": delta}
                size = len(json.dumps(patch, ensure_ascii=False, separators=(",", ":")).encode())
                history = self._list_history.setdefault(slot, deque())
                history.append((patch, size))
                total = self._list_history_sizes.get(slot, 0) + size
                while len(history) > self.list_history_capacity or total > self.list_history_bytes:
                    total -= history.popleft()[1]
                self._list_history_sizes[slot] = total
            self._list_latest[slot] = event
            if topic in self._subscribed:
                if slot in self._list_pending:
                    self.merged += 1
                self._list_pending[slot] = event
            return True

    def require_list_subscription(self, topic: str, key: str | None = None) -> None:
        """Validate cursor eligibility without copying or creating a baseline."""
        list_slot(self.epoch, topic, key)
        with self._locked():
            if topic not in self._subscribed:
                raise ValueError("topic is not subscribed")

    def list_snapshot(self, topic: str, key: str | None = None) -> dict:
        slot = list_slot(self.epoch, topic, key)
        with self._locked():
            if topic not in self._subscribed:
                raise ValueError("topic is not subscribed")
            if slot not in self._list_latest:
                # Empty keys are real first baselines, including rev=1.
                event = {"t": "evt", "epoch": self.epoch, "topic": topic, "rev": 1,
                         "kind": "snapshot", "produced_at_ms": _ms(), "data": {"items": []}}
                if key is not None:
                    event["key"] = key
                self._list_latest[slot] = event
            return deepcopy(self._list_latest[slot])

    def list_pending_revisions(self) -> dict[tuple, int]:
        with self._locked():
            return {slot: event["rev"] for slot, event in self._list_pending.items()}

    def list_revision(self, topic: str, key: str | None = None) -> int:
        slot = list_slot(self.epoch, topic, key)
        with self._locked():
            return self._list_latest[slot]["rev"]

    def list_catchup(self, slot: tuple, rev: int, *, check_only: bool = False) -> tuple[list[dict] | None, bool]:
        """A missing journal prefix explicitly invalidates a frozen watermark."""
        with self._locked():
            current = self._list_latest[slot]["rev"]
            patches = [item for item, _size in self._list_history.get(slot, ()) if item["rev"] > rev]
            expected = rev
            for patch in patches:
                if patch["base_rev"] != expected:
                    return None, True
                expected = patch["rev"]
            if expected != current:
                return None, True
            return [] if check_only else deepcopy(patches), False

    def list_submitted(self, baseline: dict) -> None:
        slot = list_slot(self.epoch, baseline["topic"], baseline.get("key"))
        with self._locked():
            current = self._list_pending.get(slot)
            if current is not None and current["rev"] == baseline["rev"]:
                self._list_pending.pop(slot)

    @contextmanager
    def _locked(self):
        waiting = perf_counter_ns()
        self._lock.acquire()
        acquired = perf_counter_ns()
        try:
            yield
        finally:
            self.lock_wait_ns += acquired - waiting
            self.lock_held_ns += perf_counter_ns() - acquired
            self._lock.release()

    def _fits(self, topic, dto, kind):
        frame = {"t": "evt", "epoch": self.epoch, "produced_at_ms": _ms(),
                 "sent_at_ms": _ms(), "topic": topic, "rev": 1, "kind": kind, "data": dto}
        if kind == "edge":
            frame["seq"] = 1
        try:
            if topic == "logs.batch":
                # record_edge has scanned the complete log payload once. Check
                # the independently generated envelope with an empty record list,
                # then size the real JSON bytes without rescanning every row.
                envelope = {**frame, "data": {**dto, "records": []}}
                if protocol.validate_frame(envelope):
                    raise ValueError("invalid log envelope")
                wire = json.dumps(frame, ensure_ascii=False, allow_nan=False,
                                  separators=(",", ":")).encode("utf-8") + b"\n"
                return len(wire) <= protocol.HARD_FRAME_LIMIT - 1024
            return len(protocol.encode_frame(frame)) <= protocol.HARD_FRAME_LIMIT - 1024
        except ValueError as exc:
            if str(exc) == "frame_too_large":
                return False
            raise

    def _rev(self, topic: str) -> int:
        value = self._revs.get(topic, 0) + 1
        self._revs[topic] = value
        return value

    def record_state(self, topic: str, dto: dict[str, Any], *, history_reseed: bool = False) -> None:
        # P1 stays once-per-epoch by default; only monitor replacement may reseed.
        if history_reseed and (topic != "traffic.history" or dto.get("gap") is not True):
            raise ValueError("invalid history reseed")
        if topic not in V1_SNAPSHOT_TOPICS or validate_topic_payload(topic, dto):
            raise ValueError("invalid state DTO")
        if not self._fits(topic, dto, "snapshot"):
            raise ValueError("frame_too_large")
        now = _ms()
        with self._locked():
            if topic == "traffic.history" and self._history_sent and topic in self._latest and not history_reseed:
                return
            event = {"t": "evt", "epoch": self.epoch, "produced_at_ms": now,
                     "topic": topic, "rev": self._rev(topic), "kind": "snapshot",
                     "data": deepcopy(dto)}
            if topic in self._pending:
                self.merged += 1
            self._latest[topic] = event
            if topic in self._subscribed and (history_reseed or not (topic == "traffic.history" and self._history_sent)):
                self._pending[topic] = event

    def invalidate_state(self, topic: str, *, notify: bool = False) -> None:
        """Withdraw a baseline; sampling owners can report a withheld fact as gap.

        The reserved sequence represents the unpublishable source observation.
        Gap already means stale in v1; recovery requires a complete snapshot.
        This opt-in path does not change unrelated v1 owners' loss semantics.
        """
        if topic not in V1_SNAPSHOT_TOPICS:
            raise ValueError("invalid state topic")
        with self._locked():
            self._latest.pop(topic, None)
            self._pending.pop(topic, None)
            if not notify:
                return
            # A repeated failed observation has no new baseline to revoke.
            # T-P2-31b: a topic that never had a baseline is marked unavailable
            # on its first failed observation too (rev 0), so a subscription
            # barrier waiting for its initial snapshot does not wait forever.
            if (topic in self._invalidated_revs
                    and self._invalidated_revs[topic] == self._revs.get(topic, 0)):
                return
            seq = self._seqs.get(topic, 0) + 1
            self._seqs[topic] = seq
            unavailable = {"seq": seq}
            self._lose_topic(topic, _ms(), extra=unavailable)
            self._invalidated_revs[topic] = self._revs.get(topic, 0)

    def baseline_revoked(self, event: dict[str, Any]) -> bool:
        """Check source rev, before Publisher assigns a transmission rev."""
        with self._locked():
            return event["rev"] <= self._invalidated_revs.get(event["topic"], 0)

    def baseline_unavailable(self, topic: str) -> bool:
        with self._locked():
            return topic in self._invalidated_revs and topic not in self._latest

    def record_edge(self, topic: str, dto: dict[str, Any]) -> int:
        if topic == "jobs":
            raise ValueError("jobs must use JobEventRecorder reservations")
        if topic not in V1_TOPICS or "edge" not in TOPIC_KINDS[topic]:
            raise ValueError("invalid edge DTO")
        errors = (protocol.validate_logs_batch(dto) if topic == "logs.batch"
                  else validate_topic_payload(topic, dto))
        if errors:
            raise ValueError("invalid edge DTO")
        fits = self._fits(topic, dto, "edge")
        if not fits and topic != "logs.batch":
            # Reject before accepting an indivisible critical DTO; accepted
            # critical events are never truncated or silently discarded.
            raise ValueError("frame_too_large")
        now = _ms()
        with self._locked():
            seq = self._seqs.get(topic, 0) + 1
            if topic in self._subscribed or topic == "logs.batch":
                self._seqs[topic] = seq
            event = {"t": "evt", "epoch": self.epoch, "produced_at_ms": now,
                     "topic": topic, "rev": self._rev(topic), "kind": "edge",
                     "seq": seq, "data": (_copy_log_payload(dto) if topic == "logs.batch"
                                          else deepcopy(dto)) if fits else dto}
            if not fits:
                self._lose_topic(topic, now, extra=event)
                return seq
            if topic in V1_SNAPSHOT_TOPICS:
                self._latest[topic] = {key: deepcopy(value) for key, value in event.items()
                                       if key != "seq"}
                self._latest[topic]["kind"] = "snapshot"
                # The edge is a complete DTO. An older pending snapshot would
                # otherwise follow it with a lower transmission revision.
                self._pending.pop(topic, None)
            # State topics retain their current projection before subscription;
            # logs, which have no snapshot, always retain a bounded tail.
            if topic not in self._subscribed and topic != "logs.batch":
                return seq
            if len(self._edges) >= self.edge_capacity:
                self._lose_topic(self._edges[0]["topic"], now,
                                 extra=event if self._edges[0]["topic"] == topic else None)
                if topic in self._gaps and self._gaps[topic]["seq"] > seq:
                    return seq
            self._edges.append(event)
            return seq

    def _lose_topic(self, topic: str, now: int, *, extra=None) -> None:
        lost = [item for item in self._edges if item["topic"] == topic]
        if extra is not None:
            lost.append(extra)
        old = self._gaps.get(topic)
        first = old["data"]["from_seq"] if old else min(item["seq"] for item in lost)
        last = self._seqs[topic]
        self._seqs[topic] = last + 1
        self._edges = deque(item for item in self._edges if item["topic"] != topic)
        self._gaps[topic] = {"t": "evt", "epoch": self.epoch,
            "produced_at_ms": now, "topic": topic, "rev": self._rev(topic),
            "kind": "gap", "seq": last + 1,
            "data": {"from_seq": first, "to_seq": last, "dropped": last - first + 1}}
        # Count actual newly lost events/records, not previous loss a second time.
        self.dropped += len(lost)
        if topic == "logs.batch":
            self.log_records_dropped += sum(len(item["data"]["records"]) for item in lost)
            for item in lost:
                for row in item["data"]["records"]:
                    self._log_drop_counts[row["level"]] += 1
        elif topic in self._latest and topic in self._subscribed:
            self._pending[topic] = self._latest[topic]

    def drop_logs(self, event: dict[str, Any]) -> None:
        """Budget rejection is explicit loss, with the same bounded gap path."""
        with self._locked():
            if any(item["topic"] == "logs.batch" and item["seq"] == event["seq"]
                   for item in self._edges):
                self._lose_topic("logs.batch", _ms())

    def subscribe(self, topics: list[str]) -> list[dict[str, Any]]:
        if not topics or len(topics) != len(set(topics)) or not set(topics) <= V1_TOPICS | LIST_TOPICS:
            raise ValueError("invalid subscription")
        with self._locked():
            self._subscribed.update(topics)
            result = []
            for topic in topics:
                if topic in LIST_TOPICS:
                    continue  # List seeds are copied below, outside this lock.
                if topic == "traffic.history":
                    if self._history_sent:
                        continue
                    # Reserve the epoch's first delivery even before sampling,
                    # so concurrent queued subscriptions cannot duplicate it.
                    self._history_sent = True
                current = self._latest.get(topic)
                if current is None or topic not in V1_SNAPSHOT_TOPICS:
                    continue
                result.append(deepcopy(current))
                if any(item["topic"] == topic for item in self._edges) or topic in self._gaps:
                    # Replay retained edges after the initial snapshot, then
                    # restore the current full projection. Wire revs are assigned
                    # by Publisher at submission, independently of these tokens.
                    self._pending[topic] = current
                else:
                    self._pending.pop(topic, None)
        for topic in topics:
            if topic in LIST_TOPICS:
                for key in LIST_KEYS if topic in protocol.KEYED_TOPICS else (None,):
                    result.append(self.list_snapshot(topic, key))
        return result

    def snapshot(self, topic: str) -> dict[str, Any] | None:
        with self._locked():
            if topic not in self._subscribed or topic not in V1_SNAPSHOT_TOPICS:
                raise ValueError("topic is not an allowed subscribed snapshot")
            event = self._latest.get(topic)
            return deepcopy(event) if event is not None else None

    def restore_after_snapshot(self, topic: str) -> None:
        with self._locked():
            if (topic in self._latest and
                    (topic in self._gaps or any(item["topic"] == topic for item in self._edges))):
                self._pending[topic] = self._latest[topic]

    def snapshot_submitted(self, event: dict[str, Any]) -> None:
        with self._locked():
            topic = event["topic"]
            if topic in self._gaps or any(item["topic"] == topic for item in self._edges):
                return  # The final current projection follows the retained replay.
            current = self._pending.get(topic)
            if current is not None and current["rev"] == event["rev"]:
                self._pending.pop(topic)

    def _settle_log_publication_work(self) -> None:
        # Called under _locked. Same-thread CPU from pending through ack includes
        # sizing, validation and transport encoding, never sleeping wall time.
        # A busy/error attempt has no ack: the next pending settles it instead.
        started = getattr(self._publication_work, "started", None)
        if started is not None:
            self._log_publication_cpu_ns += max(0, thread_time_ns() - started)
            self._publication_work.started = None

    @property
    def log_publication_cpu_ns(self) -> int:
        """Local CPU counter only; no effect on ordered submission/loss ack."""
        with self._locked():
            return self._log_publication_cpu_ns

    def pending(self, limit: int = 128) -> list[dict[str, Any]]:
        started = thread_time_ns()
        with self._locked():
            self._settle_log_publication_work()
            edges = [deepcopy(item) for item in self._gaps.values()
                     if item["topic"] in self._subscribed]
            edges += [_copy_edge(item) for item in self._edges
                      if item["topic"] in self._subscribed]
            states = [deepcopy(item) for item in self._pending.values()]
            result = (edges + states)[:limit]
            if any(item["topic"] == "logs.batch" for item in result):
                self._publication_work.started = started
            return result

    def pending_for(self, topic: str) -> list[dict[str, Any]]:
        """T-P2-H6: one subscribed topic's queued gap, edges and pending
        snapshot, in pending() order, as light receipts
        ``{topic, kind, rev, seq?}`` -- no DTO copy and no other topic. Not a
        publication attempt: it neither starts nor settles the log CPU
        account. Receipts carry no ``data``, so they suit ``ack`` only for
        queued edges (never a writer's rescued copy)."""
        with self._locked():
            if topic not in self._subscribed:
                return []
            items = [self._gaps[topic]] if topic in self._gaps else []
            items += [item for item in self._edges if item["topic"] == topic]
            if topic in self._pending:
                items.append(self._pending[topic])
            return [{key: item[key] for key in ("topic", "kind", "rev", "seq") if key in item}
                    for item in items]

    def _reclaim_gap_seq(self, topic: str, reserved: int) -> None:
        """T-P2-H6: a rescue trimmed this topic's gap away, so the seq that
        _lose_topic reserved for the gap frame will never reach the wire.
        Give it back: the topic's later edges (queued after the loss, so no
        writer holds a copy of them yet) move down by one and so does the
        counter. The topic's wire seqs stay continuous (PROTOCOL §4)."""
        for item in self._edges:
            if item["topic"] == topic and item["seq"] > reserved:
                item["seq"] -= 1
        self._seqs[topic] = max(0, self._seqs.get(topic, reserved) - 1)

    def ack(self, sent: list[dict[str, Any]], *, settle_log_cpu: bool = True) -> None:
        """settle_log_cpu=False: the caller is not a publication attempt
        (Publisher._drop_covered), so the log CPU account is left alone."""
        with self._locked():
            if settle_log_cpu:
                self._settle_log_publication_work()
            for event in sent:
                if event["kind"] == "snapshot":
                    current = self._pending.get(event["topic"])
                    if current is not None and current["rev"] == event["rev"]:
                        self._pending.pop(event["topic"])
                elif event["kind"] == "gap":
                    current = self._gaps.get(event["topic"])
                    if current is not None and current["seq"] == event["seq"]:
                        self._gaps.pop(event["topic"])
                else:
                    topic = event["topic"]
                    current = self._gaps.get(topic)
                    queued = any(item["topic"] == topic and item["seq"] == event["seq"]
                                 for item in self._edges)
                    if (not queued and current is not None
                            and current["data"]["from_seq"] <= event["seq"] <= current["data"]["to_seq"]
                            and event["seq"] > self._sent_seqs.get(topic, 0)):
                        # A producer overflowed the ring while send owned its
                        # copied prefix. Submission rescued this event: it must
                        # neither be counted lost nor reappear inside a gap.
                        if topic == "logs.batch" and "data" not in event:
                            # pending_for receipts carry no data; only a
                            # writer's sent copy can settle rescued records.
                            raise ValueError("logs.batch rescue needs the sent event's data")
                        self.dropped -= 1
                        if topic == "logs.batch":
                            self.log_records_dropped -= len(event["data"]["records"])
                            for row in event["data"]["records"]:
                                self._log_drop_counts[row["level"]] -= 1
                    self._edges = deque(item for item in self._edges
                        if (item["topic"], item["seq"]) != (event["topic"], event["seq"]))
                if event["kind"] != "snapshot":
                    topic = event["topic"]
                    self._sent_seqs[topic] = max(self._sent_seqs.get(topic, 0), event["seq"])
                    gap = self._gaps.get(topic)
                    if gap is not None:
                        first = max(gap["data"]["from_seq"], self._sent_seqs[topic] + 1)
                        if first > gap["data"]["to_seq"]:
                            self._gaps.pop(topic)
                            self._reclaim_gap_seq(topic, gap["seq"])
                        else:
                            gap["data"]["from_seq"] = first
                            gap["data"]["dropped"] = gap["data"]["to_seq"] - first + 1
            if "logs.batch" not in self._gaps:
                # Until the ordered gap is submitted, a copied edge can still
                # be rescued by the writer. Only this watermark is irreversible.
                self._settled_log_records_dropped = self.log_records_dropped
                self._settled_log_drop_counts = dict(self._log_drop_counts)

    @property
    def settled_log_drop_counts(self) -> dict[str, int]:
        """Owned severity receipt, settled at the same ordered gap barrier."""
        with self._locked():
            return dict(self._settled_log_drop_counts)

    @property
    def settled_log_records_dropped(self) -> int:
        """Monotonic record loss confirmed by ordered gap submission."""
        with self._locked():
            return self._settled_log_records_dropped

    @property
    def depth(self) -> int:
        with self._locked():
            return len(self._pending) + len(self._edges) + len(self._gaps) + len(self._list_pending)

    @property
    def history_sent(self) -> bool:
        with self._locked():
            return self._history_sent

    def claim_history(self) -> None:
        with self._locked():
            self._history_sent = True
