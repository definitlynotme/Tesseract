from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
import urllib.parse
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from atomic_file import replace_with_retry
from config_utils import coerce_bool
from models import utc_now_iso


DELIVERY_HISTORY_SCHEMA_VERSION = 2
DEFAULT_DELIVERY_HISTORY_LIMIT = 200
MAX_DELIVERY_ERROR_LENGTH = 600

_LOGGER = logging.getLogger(__name__)

_SIGNATURE_IGNORED_KEYS = frozenset({"enabled", "label", "type"})
_HISTORY_TOP_LEVEL_FIELDS = frozenset(
    {"schema_version", "updated_at", "records"}
)
_HISTORY_RECORD_FIELDS = frozenset(
    {
        "timestamp",
        "channel_id",
        "channel_type",
        "outcome",
        "error",
        "config_signature",
    }
)
_SECRET_MARKERS = (
    "access_token",
    "api_key",
    "app_token",
    "authorization",
    "auth_token",
    "bot_token",
    "client_secret",
    "password",
    "secret",
    "token",
    "url",
    "webhook",
)
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"""(?ix)
    (?<![\w])
    ["']?
    (
        access[_-]?token|api[_-]?key|app[_-]?token|authorization|
        auth[_-]?token|bearer|bot[_-]?token|client[_-]?secret|
        password|recipients?|secret|token|
        to(?:[_-]?(?:addrs|numbers))?|email|phone|
        user[_-]?key|chat[_-]?id
    )
    ["']?
    \s*[:=]\s*
    (?:
        "(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|
        \[[^\]\r\n]*\]|\{[^}\r\n]*\}|[^\s,;]+
    )
    """
)
_AUTH_HEADER_RE = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")
_EMAIL_RE = re.compile(r"(?i)(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Z]{2,}(?![\w.-])")
_PHONE_RE = re.compile(r"(?<!\w)\+?\d(?:[\d().\s-]{5,}\d)(?!\w)")
_HTTP_URL_RE = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
_HEX_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)


@dataclass(frozen=True)
class AlertDeliveryRecord:
    timestamp: str
    channel_id: str
    channel_type: str
    outcome: str
    error: str
    config_signature: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AlertDeliveryRecord":
        return cls(
            timestamp=_normalized_history_timestamp(
                payload.get("timestamp")
            ),
            channel_id=_normalized_history_channel_id(
                payload.get("channel_id")
            ),
            channel_type=_normalized_channel_type(
                payload.get("channel_type")
            ),
            outcome=_normalized_delivery_outcome(
                payload.get("outcome")
            ),
            error=redact_delivery_history_text(payload.get("error") or "")[
                :MAX_DELIVERY_ERROR_LENGTH
            ],
            config_signature=_normalized_config_signature(
                payload.get("config_signature")
            ),
        )


@dataclass(frozen=True)
class AlertChannelHealth:
    channel_id: str
    channel_type: str
    enabled: bool
    configured: bool
    status: str
    verified: bool
    last_attempt_at: str
    last_success_at: str
    last_error: str
    last_outcome: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def alert_channel_config_signature(channel_type: str, config: dict[str, Any]) -> str:
    """Return a one-way identity for transport-affecting channel settings.

    The persisted value is only the digest. Raw settings and secrets never enter
    the delivery history file.
    """

    normalized = {
        str(key): _signature_value(value)
        for key, value in sorted((config if isinstance(config, dict) else {}).items())
        if str(key) not in _SIGNATURE_IGNORED_KEYS
    }
    raw = json.dumps(
        {"channel_type": str(channel_type or "").strip().lower(), "config": normalized},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def alert_channel_configured(channel_type: str, config: dict[str, Any]) -> bool:
    channel = str(channel_type or "").strip().lower()
    values = config if isinstance(config, dict) else {}
    if channel in {"desktop", "sound"}:
        return True
    required_all: dict[str, tuple[str, ...]] = {
        "telegram": ("bot_token", "chat_id"),
        "slack": ("url",),
        "discord": ("url",),
        "teams": ("url",),
        "google_chat": ("url",),
        "ntfy": ("server", "topic"),
        "pushover": ("app_token", "user_key"),
        "gotify": ("server", "app_token"),
        "email_smtp": ("host", "from_addr", "to_addrs"),
        "email_sendgrid": ("api_key", "from_addr", "to_addrs"),
        "generic_webhook": ("url",),
    }
    if channel == "whatsapp":
        return all(_has_value(values.get(key)) for key in ("access_token", "to")) and any(
            _has_value(values.get(key)) for key in ("phone_number_id", "api_url")
        )
    if channel == "sms_twilio":
        to_numbers = values.get("to_numbers")
        if not _has_value(to_numbers):
            to_numbers = values.get("to")
        return all(
            (
                _has_value(values.get("account_sid")),
                _has_value(values.get("auth_token")),
                _has_value(to_numbers),
            )
        ) and any(
            _has_value(values.get(key))
            for key in ("from_number", "messaging_service_sid", "from")
        )
    fields = required_all.get(channel)
    if fields is None:
        return False
    return all(_has_value(values.get(key)) for key in fields)


def redact_delivery_error(text: Any, *configs: dict[str, Any]) -> str:
    redacted = str(text or "").strip()
    if not redacted:
        return ""
    for config in configs:
        for key, value in _iter_scalar_values(config):
            if _is_secret_key(key) and value:
                redacted = redacted.replace(value, "<redacted>")
    return redact_delivery_history_text(redacted)[:MAX_DELIVERY_ERROR_LENGTH]


def redact_delivery_history_text(text: Any) -> str:
    """Remove recipient and credential material from persisted history text."""

    redacted = str(text or "").strip()
    if not redacted:
        return ""
    redacted = _AUTH_HEADER_RE.sub(r"\1 <redacted>", redacted)
    redacted = _SENSITIVE_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}=<redacted>",
        redacted,
    )
    redacted = _HTTP_URL_RE.sub(_redacted_history_url, redacted)
    redacted = _EMAIL_RE.sub("<redacted-email>", redacted)
    redacted = _PHONE_RE.sub("<redacted-phone>", redacted)
    return " ".join(redacted.split())


class AlertDeliveryStore:
    def __init__(
        self,
        path: str | Path,
        *,
        limit: int = DEFAULT_DELIVERY_HISTORY_LIMIT,
        logger: logging.Logger | None = None,
    ) -> None:
        self.path = Path(path)
        self.limit = max(1, int(limit))
        self._lock = threading.RLock()
        # T-P2-RG: monotonic in-memory revision, advanced by every record /
        # record_many / delete_channel that changed the records (P2-22).
        self._revision = 0
        self._logger = logger or _LOGGER
        self.load_warning: dict[str, Any] | None = None
        self._records, needs_rewrite = self._load()
        if needs_rewrite:
            try:
                with self._lock:
                    self._save_locked()
            except Exception:
                self._logger.warning(
                    "Failed to rewrite migrated alert delivery history: %s",
                    self.path,
                    exc_info=True,
                )

    def record(
        self,
        *,
        channel_id: str,
        channel_type: str,
        config: dict[str, Any],
        outcome: str,
        error: Any = "",
        timestamp: str | None = None,
    ) -> AlertDeliveryRecord:
        record = self._new_record(
            channel_id=channel_id,
            channel_type=channel_type,
            config=config,
            outcome=outcome,
            error=error,
            timestamp=timestamp,
        )
        self._append_records((record,))
        return record

    def record_many(
        self,
        deliveries: Iterable[dict[str, Any]],
    ) -> list[AlertDeliveryRecord]:
        """Append multiple delivery results with one atomic history rewrite."""

        records: list[AlertDeliveryRecord] = []
        for delivery in deliveries:
            if not isinstance(delivery, dict):
                continue
            config = (
                delivery.get("config")
                if isinstance(delivery.get("config"), dict)
                else {}
            )
            records.append(
                self._new_record(
                    channel_id=str(delivery.get("channel_id") or ""),
                    channel_type=str(delivery.get("channel_type") or ""),
                    config=config,
                    outcome=str(delivery.get("outcome") or "failed"),
                    error=delivery.get("error", ""),
                    timestamp=(
                        str(delivery.get("timestamp"))
                        if delivery.get("timestamp")
                        else None
                    ),
                )
            )
        if records:
            self._append_records(records)
        return records

    @staticmethod
    def _new_record(
        *,
        channel_id: str,
        channel_type: str,
        config: dict[str, Any],
        outcome: str,
        error: Any,
        timestamp: str | None,
    ) -> AlertDeliveryRecord:
        normalized_outcome = str(outcome or "failed").strip().lower()
        if normalized_outcome not in {"success", "failed", "dropped"}:
            normalized_outcome = "failed"
        return AlertDeliveryRecord(
            timestamp=(
                _normalized_history_timestamp(timestamp)
                or utc_now_iso()
            ),
            channel_id=_normalized_history_channel_id(
                channel_id or channel_type
            ),
            channel_type=_normalized_channel_type(
                channel_type or channel_id
            ),
            outcome=normalized_outcome,
            error=redact_delivery_error(error, config),
            config_signature=alert_channel_config_signature(channel_type, config),
        )

    def _append_records(
        self,
        records: Iterable[AlertDeliveryRecord],
    ) -> None:
        with self._lock:
            self._records.extend(records)
            self._records = self._records[-self.limit :]
            self._revision += 1
            self._save_locked()

    def recent(self, limit: int | None = None, *, channel_id: str | None = None) -> list[dict[str, Any]]:
        size = self.limit if limit is None else max(1, int(limit))
        with self._lock:
            records = list(self._records)
        if channel_id:
            key = _normalized_history_channel_id(channel_id)
            records = [record for record in records if record.channel_id == key]
        return [record.to_dict() for record in records[-size:]]

    def delete_channel(self, channel_id: str) -> int:
        """Delete persisted delivery history for one exact channel id."""

        key = _normalized_history_channel_id(channel_id)
        if not key:
            return 0
        with self._lock:
            retained = [
                record for record in self._records if record.channel_id != key
            ]
            removed = len(self._records) - len(retained)
            if not removed:
                return 0
            self._records = retained
            self._revision += 1
            self._save_locked()
            return removed

    @property
    def revision(self) -> int:
        """In-memory change counter; lock-free O(1) read (int read is atomic).

        Advanced before the history file is rewritten, because health and
        recent() already reflect the in-memory records even if that save fails.
        """
        return self._revision

    def health_for_entry(self, entry: dict[str, Any]) -> AlertChannelHealth:
        channel_id = str(entry.get("key") or entry.get("id") or entry.get("type") or "")
        history_channel_id = _normalized_history_channel_id(channel_id)
        channel_type = str(entry.get("type") or channel_id).strip().lower()
        config = entry.get("config") if isinstance(entry.get("config"), dict) else {}
        enabled = coerce_bool(config.get("enabled"), False)
        configured = alert_channel_configured(channel_type, config)
        signature = alert_channel_config_signature(channel_type, config)
        with self._lock:
            matching = [
                record
                for record in self._records
                if record.channel_id == history_channel_id
                and record.config_signature == signature
            ]
        last = matching[-1] if matching else None
        successes = [record for record in matching if record.outcome == "success"]
        verified = bool(successes)
        # A dropped record (queue full, manager closing, or a desktop notice
        # that had no visible surface) is not a transport verdict. The most
        # recent success or failure decides the health status.
        decisive = [record for record in matching if record.outcome != "dropped"]
        last_decisive = decisive[-1] if decisive else None
        if not configured:
            status = "incomplete"
        elif not enabled:
            status = "disabled"
        elif last_decisive is None:
            status = "unverified"
        elif last_decisive.outcome == "success":
            status = "submitted"
        else:
            status = "failed"
        return AlertChannelHealth(
            channel_id=channel_id,
            channel_type=channel_type,
            enabled=enabled,
            configured=configured,
            status=status,
            verified=verified,
            last_attempt_at=last.timestamp if last is not None else "",
            last_success_at=successes[-1].timestamp if successes else "",
            last_error=(
                last_decisive.error
                if last_decisive is not None and last_decisive.outcome != "success"
                else ""
            ),
            last_outcome=last.outcome if last is not None else "",
        )

    def health_for_entries(self, entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self.health_for_entry(entry).to_dict() for entry in entries if isinstance(entry, dict)]

    def _load(self) -> tuple[list[AlertDeliveryRecord], bool]:
        if not self.path.exists():
            return [], False
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            raw_records = payload.get("records", []) if isinstance(payload, dict) else payload
            if not isinstance(raw_records, list):
                raise ValueError("alert delivery history records must be a list")
            schema_version = (
                _safe_int(payload.get("schema_version"), 1)
                if isinstance(payload, dict)
                else 1
            )
            records = [
                AlertDeliveryRecord.from_dict(item)
                for item in raw_records
                if isinstance(item, dict)
            ][-self.limit :]
            serialized_records = [record.to_dict() for record in records]
            needs_rewrite = (
                schema_version != DELIVERY_HISTORY_SCHEMA_VERSION
                or not isinstance(payload, dict)
                or set(payload) != _HISTORY_TOP_LEVEL_FIELDS
                or type(payload.get("schema_version")) is not int
                or not isinstance(payload.get("updated_at"), str)
                or not _normalized_history_timestamp(
                    payload.get("updated_at")
                )
                or _normalized_history_timestamp(payload.get("updated_at"))
                != payload.get("updated_at")
                or any(
                    not isinstance(original, dict)
                    or set(original) != _HISTORY_RECORD_FIELDS
                    for original in raw_records
                )
                or len(serialized_records) != len(raw_records)
                or any(
                    serialized != original
                    for serialized, original in zip(
                        serialized_records,
                        raw_records[-self.limit :],
                    )
                )
            )
            return records, needs_rewrite
        except Exception as exc:
            quarantined = self._quarantine_corrupt_file()
            self.load_warning = {
                "kind": "corrupt_history",
                "isolated": quarantined is not None,
            }
            self._logger.warning(
                "Alert delivery history is corrupt and was isolated: %s%s",
                self.path,
                f" -> {quarantined}" if quarantined is not None else "",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            return [], False

    def _quarantine_corrupt_file(self) -> Path | None:
        if not self.path.exists():
            return None
        timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        quarantined = self.path.with_name(
            f"{self.path.name}.corrupt-{timestamp}-{uuid.uuid4().hex[:8]}"
        )
        try:
            os.replace(self.path, quarantined)
        except OSError:
            self._logger.warning(
                "Failed to isolate corrupt alert delivery history: %s",
                self.path,
                exc_info=True,
            )
            return None
        return quarantined

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": DELIVERY_HISTORY_SCHEMA_VERSION,
            "updated_at": utc_now_iso(),
            "records": [record.to_dict() for record in self._records[-self.limit :]],
        }
        fd, temp_path = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            replace_with_retry(temp_path, self.path)
        except Exception:
            try:
                Path(temp_path).unlink()
            except FileNotFoundError:
                pass
            raise


def _redacted_history_url(match: re.Match[str]) -> str:
    raw = match.group(0)
    trailing = ""
    while raw and raw[-1] in ".,);]":
        trailing = raw[-1] + trailing
        raw = raw[:-1]
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        return "<redacted-url>" + trailing
    hostname = parsed.hostname or ""
    if not hostname:
        return "<redacted-url>" + trailing
    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        port = ""
    netloc = f"{hostname}{port}"
    path = "/<redacted>" if parsed.path not in {"", "/"} else parsed.path
    cleaned = urllib.parse.urlunsplit(
        (parsed.scheme, netloc, path, "", "")
    )
    if parsed.query or parsed.fragment:
        cleaned = f"{cleaned}?<redacted>"
    return cleaned + trailing


def _normalized_config_signature(value: Any) -> str:
    signature = str(value or "").strip()
    if not signature:
        return ""
    if _HEX_DIGEST_RE.fullmatch(signature):
        return signature.lower()
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()


def _signature_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _signature_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (set, frozenset)):
        normalized = [_signature_value(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ),
        )
    if isinstance(value, (list, tuple)):
        return [_signature_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _iter_scalar_values(value: Any, prefix: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _iter_scalar_values(item, f"{prefix}.{key}" if prefix else str(key))
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_scalar_values(item, prefix)
        return
    if value not in (None, ""):
        yield prefix, str(value)


def _is_secret_key(key: str) -> bool:
    lowered = str(key or "").lower()
    return any(marker in lowered for marker in _SECRET_MARKERS)


def _has_value(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return value not in (None, "")


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _normalized_delivery_outcome(value: Any) -> str:
    outcome = str(value or "failed").strip().lower()
    return outcome if outcome in {"success", "failed", "dropped"} else "failed"


def _normalized_channel_type(value: Any) -> str:
    channel_type = str(value or "").strip().lower()
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", channel_type):
        return channel_type
    return "unknown"


def _normalized_history_timestamp(value: Any) -> str:
    timestamp = str(value or "").strip()
    if not timestamp or len(timestamp) > 64:
        return ""
    try:
        datetime.fromisoformat(
            timestamp[:-1] + "+00:00"
            if timestamp.endswith("Z")
            else timestamp
        )
    except (TypeError, ValueError):
        return ""
    return timestamp


def _normalized_history_channel_id(value: Any) -> str:
    raw = " ".join(str(value or "").strip().split())
    if not raw:
        return ""
    if re.fullmatch(r"private:[0-9a-f]{64}", raw):
        return raw
    redacted = redact_delivery_history_text(raw)
    if redacted != raw or len(raw) > 128:
        return f"private:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"
    return redacted
