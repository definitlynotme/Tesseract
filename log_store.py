from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Sequence

from log_events import normalize_log_level, normalize_log_source


KEY_EVENT_CATEGORIES = frozenset(
    {
        "runtime",
        "routing",
        "health",
        "circuit_breaker",
        "configuration",
        "update",
        "alert",
    }
)

_CONNECTION_NOISE_PATTERNS = (
    re.compile(r"\b(?:download|upload|down|up)(?:load)?\s*(?:rate|speed)\b", re.IGNORECASE),
    re.compile(r"\b(?:connections?|sessions?)\s*[:=]\s*\d+\b", re.IGNORECASE),
    re.compile(r"\b(?:bytes?|traffic|throughput)\b", re.IGNORECASE),
    re.compile(r"\b(?:rx|tx)\s*[:=]", re.IGNORECASE),
)
_VALIDATION_NOISE_PATTERNS = (
    re.compile(r"\b(?:validation|validate|checking|probing|latency test|health check)\b", re.IGNORECASE),
    re.compile(r"\b(?:candidate|proxy)\s+pool\b", re.IGNORECASE),
    re.compile(r"\b(?:candidate|node)\s+(?:accepted|rejected|tested|score)\b", re.IGNORECASE),
)
_CATEGORY_PATTERNS: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = (
    (
        "circuit_breaker",
        (
            re.compile(r"\b(?:circuit breaker|kill switch|failsafe)\b", re.IGNORECASE),
            re.compile(r"(?:熔断|断路器|应急开关)", re.IGNORECASE),
        ),
    ),
    (
        "routing",
        (
            re.compile(r"\b(?:route|routing|selector|gateway|active exit|proxy group)\b", re.IGNORECASE),
            re.compile(r"\b(?:switch(?:ed|ing)?|fail(?:ed)? over)\s+(?:to|from)\b", re.IGNORECASE),
            re.compile(r"(?:路由|出口|切换到|已切换)", re.IGNORECASE),
        ),
    ),
    (
        "health",
        (
            re.compile(
                r"\b(?:tier|layer|node|pool).*(?:degrad\w*|recover\w*|restor\w*|healthy again|unavailable|fully down|partially down)\b",
                re.IGNORECASE,
            ),
            re.compile(r"\b(?:degrad\w*|recover\w*|restor\w*|healthy again).*(?:tier|layer|node|pool)\b", re.IGNORECASE),
            re.compile(r"(?:降级|恢复|不可用|健康节点|全部宕机)", re.IGNORECASE),
        ),
    ),
    (
        "configuration",
        (
            re.compile(r"\b(?:config|configuration|setting|preferences?).*(?:saved|loaded|reloaded|failed|complete)\b", re.IGNORECASE),
            re.compile(r"\b(?:hot[- ]reload|save(?:d)? settings?)\b", re.IGNORECASE),
            re.compile(r"(?:配置|设置).*(?:保存|加载|重载|失败|完成)", re.IGNORECASE),
        ),
    ),
    (
        "update",
        (
            re.compile(r"\b(?:subscription|rule set|geosite|geoip|database|application).*(?:updated?|upgrade|downloaded|failed)\b", re.IGNORECASE),
            re.compile(r"(?:订阅|规则|数据库|应用).*(?:更新|升级|下载|失败)", re.IGNORECASE),
        ),
    ),
    (
        "runtime",
        (
            re.compile(r"\b(?:tesseract|mihomo|core|desktop runtime|service).*(?:start\w*|stop\w*|restart\w*|ready|listening|failed|crash\w*|exit\w*)\b", re.IGNORECASE),
            re.compile(r"\b(?:start(?:ed|ing)?|stop(?:ped|ping)?|restart(?:ed|ing)?)\s+(?:tesseract|mihomo|core|runtime|service)\b", re.IGNORECASE),
            re.compile(r"(?:启动|停止|重启|退出|崩溃)", re.IGNORECASE),
        ),
    ),
)


class LogFeedState(str, Enum):
    NOT_STARTED = "not_started"
    LIVE = "live"
    RECONNECTING = "reconnecting"
    DISCONNECTED = "disconnected"


@dataclass(frozen=True, slots=True)
class LogViewWatermarkState:
    tab: str
    watermark: int
    revision: int


def _now_local() -> datetime:
    return datetime.now().astimezone()


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_value(item) for item in value)
    return value


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_value(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat(timespec="milliseconds")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _parse_datetime(value: Any, *, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    raw = str(value or "").strip()
    if not raw:
        return fallback
    normalized = raw.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        pass
    for pattern in ("%Y-%m-%d %H:%M:%S,%f", "%Y-%m-%d %H:%M:%S", "%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(raw, pattern)
            if pattern.startswith("%m"):
                parsed = parsed.replace(year=fallback.year)
            if fallback.tzinfo is not None:
                parsed = parsed.replace(tzinfo=fallback.tzinfo)
            return parsed
        except ValueError:
            continue
    return fallback


def _normalize_category(value: Any) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    aliases = {
        "config": "configuration",
        "circuit": "circuit_breaker",
        "killswitch": "circuit_breaker",
        "kill_switch": "circuit_breaker",
        "route": "routing",
        "alerts": "alert",
        "connections": "connection",
    }
    return aliases.get(normalized, normalized or "general")


def classify_log_category(
    message: Any,
    *,
    source: Any = "UNKNOWN",
    category: Any = None,
) -> str:
    if category not in (None, ""):
        return _normalize_category(category)
    normalized_source = normalize_log_source(source)
    if normalized_source == "ALERT":
        return "alert"
    text = str(message or "")
    if normalized_source == "CONNECTION" or any(pattern.search(text) for pattern in _CONNECTION_NOISE_PATTERNS):
        return "connection"
    if any(pattern.search(text) for pattern in _VALIDATION_NOISE_PATTERNS):
        return "validation"
    for candidate, patterns in _CATEGORY_PATTERNS:
        if any(pattern.search(text) for pattern in patterns):
            return candidate
    return "general"


@dataclass(frozen=True, slots=True)
class LogRecord:
    sequence: int
    occurred_at: datetime
    received_at: datetime
    source: str
    level: str
    category: str
    event_key: str | None
    message_raw: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized_level = normalize_log_level(self.level)
        normalized_category = _normalize_category(self.category)
        normalized_event_key = str(self.event_key).strip() if self.event_key else None
        metadata = dict(self.metadata or {})
        if normalize_log_source(self.source) == "MIHOMO" and normalized_event_key:
            from log_event_reports import reclassify_mihomo_fallback

            refreshed = reclassify_mihomo_fallback(
                normalized_event_key, self.message_raw, normalized_level, metadata,
            )
            if refreshed is not None:
                metadata.setdefault("observed_event_key", normalized_event_key)
                normalized_event_key = refreshed.event_key
                metadata["event_params"] = dict(refreshed.params)
                metadata["event_match_kind"] = refreshed.match_kind
        if normalized_event_key:
            from log_event_reports import event_spec

            spec = event_spec(normalized_event_key)
            if spec is not None:
                if normalized_level != spec.level:
                    metadata.setdefault("observed_level", normalized_level)
                if normalized_category != spec.category:
                    metadata.setdefault("observed_category", normalized_category)
                observed_key_flag = metadata.get("is_key_event")
                if observed_key_flag is not None and bool(observed_key_flag) != spec.is_key_event:
                    metadata.setdefault("observed_is_key_event", bool(observed_key_flag))
                normalized_level = spec.level
                normalized_category = spec.category
                metadata["is_key_event"] = bool(spec.is_key_event)
        object.__setattr__(self, "sequence", max(0, int(self.sequence)))
        object.__setattr__(self, "source", normalize_log_source(self.source))
        object.__setattr__(self, "level", normalized_level)
        object.__setattr__(self, "category", normalized_category)
        object.__setattr__(self, "event_key", normalized_event_key)
        object.__setattr__(self, "message_raw", str(self.message_raw or ""))
        object.__setattr__(self, "metadata", _freeze_value(metadata))

    @classmethod
    def from_mapping(
        cls,
        entry: Mapping[str, Any],
        *,
        sequence: int = 0,
        received_at: datetime | None = None,
        now: Callable[[], datetime] = _now_local,
    ) -> "LogRecord":
        received = received_at or _parse_datetime(entry.get("received_at"), fallback=now())
        occurred_value = entry.get("occurred_at", entry.get("timestamp", entry.get("time")))
        occurred = _parse_datetime(occurred_value, fallback=received)
        message = entry.get(
            "message_raw",
            entry.get("raw_message", entry.get("message", entry.get("payload", ""))),
        )
        source = normalize_log_source(entry.get("source"))
        metadata = dict(entry.get("metadata") or {})
        for key in (
            "event_group",
            "alert_type",
            "runtime_generation",
            "event_params",
            "event_match_kind",
            "is_key_event",
        ):
            if key in entry and key not in metadata:
                metadata[key] = entry[key]
        return cls(
            sequence=sequence,
            occurred_at=occurred,
            received_at=received,
            source=source,
            level=normalize_log_level(entry.get("level", entry.get("type"))),
            category=classify_log_category(
                message,
                source=source,
                category=entry.get("category", entry.get("event_group")),
            ),
            event_key=entry.get("event_key"),
            message_raw=str(message or ""),
            metadata=metadata,
        )

    @property
    def is_key_event(self) -> bool:
        return is_key_event(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "occurred_at": self.occurred_at.isoformat(timespec="milliseconds"),
            "received_at": self.received_at.isoformat(timespec="milliseconds"),
            "source": self.source,
            "level": self.level,
            "category": self.category,
            "event_key": self.event_key,
            "message_raw": self.message_raw,
            "metadata": _json_value(self.metadata),
        }

    def to_legacy_entry(self) -> dict[str, str]:
        return {
            "time": self.occurred_at.strftime("%m-%d %H:%M:%S"),
            "level": self.level,
            "source": self.source,
            "message": self.message_raw,
        }


def is_key_event(record: LogRecord) -> bool:
    if not record.event_key:
        return False
    # Local import avoids making the event-report module depend on LogRecord
    # at runtime.  The registry, not severity or category heuristics, is the
    # classification source of truth.
    from log_event_reports import event_spec

    spec = event_spec(record.event_key)
    return bool(spec is not None and spec.is_key_event)


def _record_fingerprint(record: LogRecord) -> tuple[str, str, str, str]:
    occurred_ms = record.occurred_at.isoformat(timespec="milliseconds")
    digest = hashlib.sha256(record.message_raw.encode("utf-8", errors="replace")).hexdigest()
    return record.source, occurred_ms, record.level, digest


class LogStore:
    """Thread-safe, bounded session log storage with per-tab view watermarks."""

    def __init__(
        self,
        *,
        max_items: int = 4000,
        dedupe_window: int = 16000,
        now: Callable[[], datetime] = _now_local,
    ) -> None:
        if max_items <= 0:
            raise ValueError("max_items must be positive")
        if dedupe_window <= 0:
            raise ValueError("dedupe_window must be positive")
        self.max_items = int(max_items)
        self.dedupe_window = int(dedupe_window)
        self._now = now
        self._records: deque[LogRecord] = deque()
        self._fingerprints: set[tuple[str, str, str, str]] = set()
        self._fingerprint_order: deque[tuple[str, str, str, str]] = deque()
        self._next_sequence = 1
        self._dropped_count = 0
        self._catalog_gap_count = 0
        self._watermarks: dict[str, int] = {"key": 0, "raw": 0}
        self._watermark_revisions: dict[str, int] = {"key": 0, "raw": 0}
        self._frozen = False
        self._frozen_after_sequence = 0
        self._lock = threading.RLock()

    @staticmethod
    def _tab_key(tab: Any) -> str:
        normalized = str(tab or "raw").strip().lower().replace("-", "_")
        if normalized in {"key", "keys", "event", "events", "important", "critical", "关键事件"}:
            return "key"
        return "raw"

    @property
    def dropped_count(self) -> int:
        with self._lock:
            return self._dropped_count

    @property
    def catalog_gap_count(self) -> int:
        """Cumulative supported-core messages that used the safe fallback."""

        with self._lock:
            return self._catalog_gap_count

    @property
    def latest_sequence(self) -> int:
        with self._lock:
            return self._next_sequence - 1

    @property
    def current_sequence(self) -> int:
        """Compatibility alias used by the desktop refresh loop."""
        return self.latest_sequence

    @property
    def frozen(self) -> bool:
        with self._lock:
            return self._frozen

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

    def records(self) -> tuple[LogRecord, ...]:
        """Every retained record in sequence order, ignoring view watermarks."""

        with self._lock:
            return tuple(self._records)

    def append_many(self, entries: Iterable[LogRecord | Mapping[str, Any]]) -> list[LogRecord]:
        appended: list[LogRecord] = []
        with self._lock:
            for entry in entries:
                if isinstance(entry, LogRecord):
                    draft = entry
                else:
                    draft = LogRecord.from_mapping(entry, now=self._now)
                candidate = replace(draft, sequence=self._next_sequence)
                fingerprint = _record_fingerprint(candidate)
                if fingerprint in self._fingerprints:
                    continue
                self._next_sequence += 1
                self._records.append(candidate)
                appended.append(candidate)
                if (
                    candidate.event_key == "mihomo.uncategorized_warning"
                    or candidate.metadata.get("event_match_kind") == "mihomo_safe_fallback"
                ):
                    self._catalog_gap_count += 1
                self._fingerprints.add(fingerprint)
                self._fingerprint_order.append(fingerprint)
                while len(self._fingerprint_order) > self.dedupe_window:
                    expired = self._fingerprint_order.popleft()
                    self._fingerprints.discard(expired)
            overflow = len(self._records) - self.max_items
            for _ in range(max(0, overflow)):
                self._records.popleft()
                self._dropped_count += 1
        return appended

    def snapshot(
        self,
        filters: Mapping[str, Any] | None = None,
        **overrides: Any,
    ) -> list[LogRecord]:
        selected = dict(filters or {})
        selected.update(overrides)
        tab = self._tab_key(selected.pop("tab", "raw"))
        query = str(selected.pop("query", selected.pop("search", "")) or "").strip().casefold()
        after_sequence = int(selected.pop("after_sequence", 0) or 0)
        limit_value = selected.pop("limit", None)
        limit = max(0, int(limit_value)) if limit_value is not None else None
        sources = self._filter_values(selected.pop("sources", selected.pop("source", None)), normalize_log_source)
        levels = self._filter_values(selected.pop("levels", selected.pop("level", None)), normalize_log_level)
        categories = self._filter_values(selected.pop("categories", selected.pop("category", None)), _normalize_category)
        with self._lock:
            watermark = max(self._watermarks.get(tab, 0), after_sequence)
            records = list(self._records)
        result: list[LogRecord] = []
        for record in records:
            if record.sequence <= watermark:
                continue
            if tab == "key" and not is_key_event(record):
                continue
            if sources and record.source not in sources:
                continue
            if levels and record.level not in levels:
                continue
            if categories and record.category not in categories:
                continue
            if query and query not in self._search_text(record):
                continue
            result.append(record)
        if limit is not None:
            return result[-limit:] if limit else []
        return result

    @staticmethod
    def _filter_values(value: Any, normalize: Callable[[Any], str]) -> set[str]:
        if value in (None, "", "ALL"):
            return set()
        values: Sequence[Any]
        if isinstance(value, str):
            values = (value,)
        else:
            try:
                values = tuple(value)
            except TypeError:
                values = (value,)
        normalized = {normalize(item) for item in values if str(item or "").upper() != "ALL"}
        return normalized

    @staticmethod
    def _search_text(record: LogRecord) -> str:
        return " ".join(
            (
                record.occurred_at.isoformat(timespec="milliseconds"),
                record.source,
                record.level,
                record.category,
                record.event_key or "",
                record.message_raw,
                json.dumps(_json_value(record.metadata), ensure_ascii=False, sort_keys=True),
            )
        ).casefold()

    def clear_view(self, tab: Any, sequence: int | None = None) -> int:
        key = self._tab_key(tab)
        with self._lock:
            latest = self._next_sequence - 1
            watermark = latest if sequence is None else min(latest, max(0, int(sequence)))
            self._watermarks[key] = max(self._watermarks.get(key, 0), watermark)
            self._watermark_revisions[key] = self._watermark_revisions.get(key, 0) + 1
            return self._watermarks[key]

    def view_watermark(self, tab: Any) -> int:
        return self.view_watermark_state(tab).watermark

    def view_watermark_state(self, tab: Any) -> LogViewWatermarkState:
        key = self._tab_key(tab)
        with self._lock:
            return LogViewWatermarkState(
                tab=key,
                watermark=self._watermarks.get(key, 0),
                revision=self._watermark_revisions.get(key, 0),
            )

    def restore_view_watermark(
        self,
        tab: Any,
        target: int,
        *,
        expected_state: LogViewWatermarkState,
    ) -> bool:
        key = self._tab_key(tab)
        restored_watermark = max(0, int(target))
        with self._lock:
            if self._tab_key(expected_state.tab) != key:
                return False
            current_watermark = self._watermarks.get(key, 0)
            current_revision = self._watermark_revisions.get(key, 0)
            if (
                expected_state.watermark != current_watermark
                or expected_state.revision != current_revision
                or restored_watermark >= current_watermark
            ):
                return False
            self._watermarks[key] = restored_watermark
            self._watermark_revisions[key] = current_revision + 1
            return True

    def set_frozen(self, frozen: bool) -> None:
        with self._lock:
            requested = bool(frozen)
            if requested and not self._frozen:
                self._frozen_after_sequence = self.latest_sequence
            elif not requested:
                self._frozen_after_sequence = self.latest_sequence
            self._frozen = requested

    def pending_count(self) -> int:
        with self._lock:
            if not self._frozen:
                return 0
            return max(0, self.latest_sequence - self._frozen_after_sequence)

    def reset(self) -> None:
        """Clear in-memory session data; view clearing should normally use clear_view()."""
        with self._lock:
            self._records.clear()
            self._fingerprints.clear()
            self._fingerprint_order.clear()
            current = self.latest_sequence
            self._watermarks = {"key": current, "raw": current}
            for key in ("key", "raw"):
                self._watermark_revisions[key] = self._watermark_revisions.get(key, 0) + 1
            self._dropped_count = 0
            self._catalog_gap_count = 0
            self._frozen_after_sequence = current


def _report_payload(report: Any) -> dict[str, Any] | None:
    if report is None:
        return None
    if isinstance(report, Mapping):
        values = report
    elif hasattr(report, "to_dict"):
        values = report.to_dict()
    else:
        values = {
            key: getattr(report, key)
            for key in ("headline", "detail", "complete", "match_kind")
            if hasattr(report, key)
        }
    payload = _json_value(values)
    return dict(payload) if isinstance(payload, Mapping) else None


def serialize_log_records(
    records: Iterable[LogRecord],
    format: str = "jsonl",
    report_provider: Callable[[LogRecord], Any] | None = None,
) -> str:
    normalized = str(format or "jsonl").strip().lower()
    materialized = list(records)
    if normalized == "jsonl":
        lines: list[str] = []
        for record in materialized:
            payload = record.to_dict()
            if report_provider is not None:
                report = _report_payload(report_provider(record))
                if report is not None:
                    payload["report"] = report
            lines.append(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        return "".join(lines)
    if normalized in {"text", "txt"}:
        chunks: list[str] = []
        for record in materialized:
            if report_provider is not None:
                report = _report_payload(report_provider(record))
                if report is not None:
                    headline = str(report.get("headline") or "").strip()
                    detail = str(report.get("detail") or "").strip()
                    chunks.append(f"[REPORT] {headline}\n")
                    if detail:
                        chunks.append(f"{detail}\n")
                    chunks.append("[RAW] ")
            chunks.append(
                f"{record.occurred_at.isoformat(timespec='milliseconds')} "
                f"[{record.source}] [{record.level}] [{record.category}] {record.message_raw}\n"
            )
        return "".join(chunks)
    raise ValueError(f"unsupported log export format: {format}")


__all__ = [
    "KEY_EVENT_CATEGORIES",
    "LogFeedState",
    "LogRecord",
    "LogStore",
    "classify_log_category",
    "is_key_event",
    "serialize_log_records",
]
