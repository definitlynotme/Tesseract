"""RT-6 source-log adapter; no runtime, callbacks, threads or transport I/O."""

from __future__ import annotations

from datetime import datetime
import re
from threading import Lock
from typing import Any

from alert_messages import ALERT_TEXT_PATTERNS
from alerts import AlertManager
from bridge_redact import redact_text


_SEVERITY = {
    "WARNING": "warn", "CRITICAL": "critical", "EMERGENCY": "critical",
    "BACKUP_REMINDER": "warn", "KILL_SWITCH_ON": "critical",
    "KILL_SWITCH_DISABLED": "warn", "KILL_SWITCH_OVERRIDE": "warn",
}
_SAFE_PATTERNS = tuple(re.compile(pattern) for pattern, _ in ALERT_TEXT_PATTERNS)
_EVENT_IDS = frozenset({
    "mode.recovery.vanguard", "mode.recovery.sentinel", "mode.tier1_degraded", "mode.tier2_active",
    "mode.tier2_active.backup_reminder", "mode.tier3_active", "mode.offline",
    "tier1.node_down", "tier1.protocol_fallback", "tier3.intervention_required",
    "tier3.source_supply_no_egress", "kill_switch.disabled", "kill_switch.activated",
    "kill_switch.deactivated", "mode.tier2_tier3_flap", "kill_switch.manual_override",
    "kill_switch.manual_clear",
})
_UNAVAILABLE = "Alert text unavailable"


def _redacted(value: str, config: dict[str, Any]) -> str:
    try:
        safe = redact_text(AlertManager._redact_text(value, config))
        # General redaction decodes escapes; reapply known channel secrets after
        # decoding so an encoded credential cannot turn back into plaintext.
        return AlertManager._redact_text(safe, config)
    except (ValueError, TypeError, RecursionError):
        return _UNAVAILABLE


def _safe_text(value: str, config: dict[str, Any]) -> str:
    # AlertRecord cannot distinguish arbitrary str(exception) from authored
    # prose. Only known whole-text templates without free-text substitutions
    # are exported. Unknown text, node labels/listings and exception reasons
    # use a fixed placeholder; do not attempt to recognize every exception.
    safe = _redacted(value, config)
    for pattern in _SAFE_PATTERNS:
        match = pattern.fullmatch(safe)
        if match and all(
            name in {"alive", "total"} and group.isascii() and group.isdecimal()
            for name, group in match.groupdict().items() if group is not None
        ):
            return safe
    return _UNAVAILABLE


def alert_dto(item: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Project only the seven Alert fields from PROTOCOL §16."""
    seq = item["alert_seq"]
    event_id = _redacted(item["event_key"], config)
    if event_id not in _EVENT_IDS:
        event_id = "alerts.unknown"
    try:
        ts_ms = max(0, int(datetime.fromisoformat(item["timestamp"]).timestamp() * 1000))
    except (ValueError, TypeError, OverflowError, OSError):
        ts_ms = 0
    return {
        "id": f"alert-{seq}", "seq": seq,
        "severity": _SEVERITY.get(item["type"], "info"), "event_id": event_id,
        "title": _safe_text(item["title"], config),
        "detail": _safe_text(item["message"], config), "ts_ms": ts_ms,
    }


class AlertsHistoryAdapter:
    """One owner per manager/epoch returns ordered, unstamped evt templates.

    snapshot() seeds the source cursor; poll() emits one edge per alert. Source
    alert_seq/last_seq and template edge/gap seq are separate counters. After a
    gap, poll returns gap then a replacement snapshot, discarding the since tail.
    The publisher must enqueue each result in order before the next call, add
    epoch/rev/produced_at_ms/sent_at_ms, and own any later transport loss using
    this same topic sequence domain. Templates are not directly sendable frames.
    Do not reuse this adapter across manager/epoch replacement.
    Alert text follows a fail-closed catalog projection: unregistered/free-text
    templates need a typed, safe parameter contract before exposing their text.
    """

    def __init__(self, source: AlertManager) -> None:
        self.source = source
        self._lock = Lock()
        self._cursor: int | None = None
        self._edge_seq = 0

    def _snapshot(self) -> tuple[dict[str, Any], int, int]:
        items, last_seq = self.source.recent_with_cursor()
        min_seq = items[0]["alert_seq"] if items else last_seq + 1
        return {"topic": "alerts.history", "kind": "snapshot", "data": {
            "items": [alert_dto(item, self.source.config) for item in items],
            "last_seq": last_seq,
        }}, last_seq, min_seq

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            event, cursor, _ = self._snapshot()
            self._cursor = cursor
            return event

    def poll(self) -> list[dict[str, Any]]:
        with self._lock:
            if self._cursor is None:
                raise RuntimeError("alerts history requires an initial snapshot")
            result = self.source.since(self._cursor)
            if result["gap"]:
                snapshot, cursor, min_seq = self._snapshot()
                # Include additional eviction between since() and the reseed.
                dropped = min_seq - self._cursor - 1
                first = self._edge_seq + 1
                last = self._edge_seq + dropped
                self._edge_seq = last + 1
                self._cursor = cursor
                return [{"topic": "alerts.history", "kind": "gap",
                         "seq": self._edge_seq, "data": {
                             "from_seq": first, "to_seq": last, "dropped": dropped,
                         }}, snapshot]
            events = []
            for item in result["items"]:
                seq = self._edge_seq + len(events) + 1
                events.append({"topic": "alerts.history", "kind": "edge",
                               "seq": seq, "data": {
                                   "items": [alert_dto(item, self.source.config)],
                                   "last_seq": item["alert_seq"],
                               }})
            # Commit only after all DTOs have been constructed successfully.
            self._edge_seq += len(events)
            self._cursor = result["last_seq"]
            return events
