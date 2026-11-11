"""Pull-only RT-5 projection; no runtime wiring or transport I/O.

One adapter belongs to one manager lifetime and one bridge consumer thread.
Source ks_seq is a resume cursor; topic seq also counts gap markers. The
publisher supplies epoch, rev and timestamps when it submits these templates.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Callable

from killswitch import KillSwitchEvent, KillSwitchManager


TOPIC = "killswitch.state"


def state_dto(event: KillSwitchEvent) -> dict[str, Any]:
    # T-P4-C1 #5 (R3/R-08): KillSwitchManager.active is set only after the
    # block rules were installed (state "triggered") and cleared when they are
    # removed, so "triggered" (rules in force, blocking) is that same fact,
    # published explicitly. Armed = enabled and admin, no error, not triggered.
    data: dict[str, Any] = {
        "enabled": event.enabled, "active": event.active, "admin": event.admin,
        "error": None, "triggered": bool(event.active),
    }
    if event.error_code is not None:
        data["error"] = {
            "code": event.error_code,
            "message_key": "errors.killswitch.command_failed",
            "params": {},
        }
    return data


class KillSwitchAdapter:
    def __init__(self, manager: KillSwitchManager) -> None:
        self.manager = manager
        self.ks_seq = 0
        self.topic_seq = 0
        self._pending: deque[dict[str, Any]] | None = None
        self._dispatching = False

    def _require_no_pending(self) -> None:
        if self._pending:
            raise RuntimeError("retry pending killswitch delivery first")

    def snapshot(self) -> dict[str, Any]:
        """Establish a subscription baseline before reading later deltas."""
        self._require_no_pending()
        event = self.manager.snapshot_with_seq()
        self.ks_seq = event.ks_seq
        return {"topic": TOPIC, "kind": "snapshot", "data": state_dto(event)}

    def drain(self) -> list[dict[str, Any]]:
        """Return all unread edges, or a gap, retained edges and latest state.

        Returned templates must enter a reliable bounded publisher queue. Use
        dispatch for an enqueue callback that may reject or raise. A snapshot
        after overflow follows retained edges so older facts cannot overwrite
        the latest state. Repeated reads without changes produce no frames.
        """
        self._require_no_pending()
        batch = self.manager.drain(self.ks_seq)
        frames: list[dict[str, Any]] = []
        if batch.gap is not None:
            dropped = batch.gap.to_seq - batch.gap.from_seq + 1
            start = self.topic_seq + 1
            end = self.topic_seq + dropped
            self.topic_seq = end + 1
            frames.append({
                "topic": TOPIC, "kind": "gap", "seq": self.topic_seq,
                "data": {"from_seq": start, "to_seq": end, "dropped": dropped},
            })
        for event in batch.events:
            self.topic_seq += 1
            frames.append({"topic": TOPIC, "kind": "edge",
                           "seq": self.topic_seq, "data": state_dto(event)})
        if batch.gap is not None:
            frames.append({"topic": TOPIC, "kind": "snapshot",
                           "data": state_dto(batch.snapshot)})
        self.ks_seq = batch.snapshot.ks_seq
        return frames

    def dispatch(self, enqueue: Callable[[dict[str, Any]], bool | None]) -> bool:
        """Outside the manager lock, enqueue templates into a bounded queue.

        enqueue must only enqueue; False or an exception means no acceptance.
        Successful callbacks may return None. Retry a rejected tail on the next
        dispatch before reading more source events. At most ring capacity + 2
        templates are retained. Partial success never duplicates accepted frames.
        Call only from the bridge consumer, which must not own manager.lock;
        runtime state methods never invoke this method (including nested calls).
        """
        if self._dispatching:
            return False
        self._dispatching = True
        try:
            return self._dispatch_pending(enqueue)
        finally:
            self._dispatching = False

    def _dispatch_pending(self, enqueue: Callable[[dict[str, Any]], bool | None]) -> bool:
        if self._pending is None:
            frames = self.drain()
            self._pending = deque(frames, maxlen=len(frames))
        while self._pending:
            # Give the sink its own flat DTO so a failed callback cannot mutate
            # the retry copy, including the nested Error object's empty params.
            frame = self._pending[0]
            candidate = {**frame, "data": dict(frame["data"])}
            if candidate["data"].get("error") is not None:
                candidate["data"]["error"] = {
                    **candidate["data"]["error"], "params": {},
                }
            try:
                if enqueue(candidate) is False:
                    return False
            except Exception:
                return False
            self._pending.popleft()
        self._pending = None
        return True
