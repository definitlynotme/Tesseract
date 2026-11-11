from __future__ import annotations

import base64
import json
import logging
import logging.handlers
import math
import platform
import queue
import re
import smtplib
import sys
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field, replace as dataclass_replace
from email.header import Header
from email.message import EmailMessage
from html import escape as html_escape
from pathlib import Path
from typing import Any, Callable

from alert_config import (
    ALERT_EVENT_GROUP_KEYS,
    ALERT_NOTIFICATION_CHANNEL_TYPES,
    COST_SENSITIVE_ALERT_TYPES,
    alert_channel_accepts_event,
    normalize_alert_event_groups,
    normalize_alert_type_filters,
    normalized_notification_channels,
    validate_notification_channel_config,
)
from alert_delivery import AlertDeliveryStore, redact_delivery_history_text
from alert_messages import (
    localize_alert_severity,
    localize_alert_text,
    localized_alert_field_labels,
    normalize_alert_language,
)
from config_utils import coerce_bool
from models import AlertRecord, AlertType, utc_now_iso
from log_capture import install_structured_event_sidecar
from log_event_reports import log_event, log_raw_diagnostic


DESKTOP_TITLE_LIMIT = 63
DESKTOP_MESSAGE_LIMIT = 240
TELEGRAM_MESSAGE_LIMIT = 4096
WHATSAPP_TEXT_BODY_LIMIT = 4096
DISCORD_CONTENT_LIMIT = 2000
PUSHOVER_MESSAGE_LIMIT = 1024
PUSHOVER_TITLE_LIMIT = 250
DEFAULT_HTTP_TIMEOUT_SECONDS = 10
DEFAULT_HTTP_RETRIES = 1
MAX_HTTP_TIMEOUT_SECONDS = 300
MAX_HTTP_RETRIES = 10
DEFAULT_ALERT_WORKERS = 4
DEFAULT_MAX_PENDING_SENDERS = 64

# Rotate after ~5 MiB, keep five backups. Keeps worst-case disk usage ~30 MiB
# and still leaves enough history for debugging a week of operation.
LOG_ROTATION_MAX_BYTES = 5 * 1024 * 1024
LOG_ROTATION_BACKUP_COUNT = 5

SENSITIVE_KEYS = (
    "access_token",
    "api_key",
    "app_token",
    "authorization",
    "auth_token",
    "bot_token",
    "key",
    "password",
    "secret",
    "token",
    "url",
    "webhook",
)

WEBHOOK_PRESET_TYPES = {
    "slack",
    "discord",
    "teams",
    "google_chat",
}
DEFAULT_ALERT_PAYLOAD = {
    "app": "{app}",
    "severity": "{severity}",
    "alert_type": "{alert_type}",
    "title": "{title}",
    "message": "{message}",
    "timestamp": "{timestamp}",
}

ALERT_SEVERITY_LABELS = {
    AlertType.WARNING.value: "Warning",
    AlertType.CRITICAL.value: "Critical",
    AlertType.EMERGENCY.value: "Emergency",
    AlertType.RECOVERY.value: "Recovery",
    AlertType.POOL_UPDATE.value: "Pool update",
    AlertType.BACKUP_REMINDER.value: "Backup reminder",
    AlertType.KILL_SWITCH_ON.value: "Kill switch",
    AlertType.KILL_SWITCH_OFF.value: "Kill switch",
    AlertType.KILL_SWITCH_DISABLED.value: "Kill switch",
    AlertType.KILL_SWITCH_OVERRIDE.value: "Kill switch",
}

EVENT_RUN_ONCE_KEYS = {
    "kill_switch.disabled",
}
_DISPATCH_STOP = object()
_TEAMS_EMBEDDED_HTTP_ERROR_RE = re.compile(
    r"Microsoft Teams endpoint returned HTTP error\s+(\d{3})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AlertEnvelope:
    alert_type: str
    title: str
    message: str
    timestamp: str
    event_key: str = ""
    event_group: str = ""
    # Localized severity label for outbound channels; empty means English.
    severity_label: str = ""

    @property
    def severity(self) -> str:
        if self.severity_label:
            return self.severity_label
        return ALERT_SEVERITY_LABELS.get(self.alert_type, self.alert_type.title() if self.alert_type else "Alert")

    def values(self) -> dict[str, str]:
        return {
            "app": "Tesseract",
            "severity": self.severity,
            "alert_type": self.alert_type,
            "title": self.title,
            "message": self.message,
            "timestamp": self.timestamp,
            "event_key": self.event_key,
            "event_group": self.event_group,
        }


@dataclass(frozen=True)
class AlertEvent:
    event_key: str
    alert_type: AlertType
    title: str
    message: str
    event_group: str = ""
    source: str = "runtime"
    scope: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AlertPolicyDecision:
    send: bool
    event: AlertEvent | None = None
    reason: str = ""


class AlertPolicy:
    def __init__(self) -> None:
        self._sent_once_keys: set[str] = set()

    def evaluate(self, event: AlertEvent) -> AlertPolicyDecision:
        if not isinstance(event.alert_type, AlertType):
            return AlertPolicyDecision(
                send=False,
                reason="unsupported alert type",
            )
        event_key = str(event.event_key or "").strip()
        if not event_key:
            return AlertPolicyDecision(send=False, reason="missing event key")

        if event_key in EVENT_RUN_ONCE_KEYS and event_key in self._sent_once_keys:
            return AlertPolicyDecision(send=False, reason="run-once event already sent")

        if event_key in EVENT_RUN_ONCE_KEYS:
            self._sent_once_keys.add(event_key)
        return AlertPolicyDecision(send=True, event=event, reason="allowed")


@dataclass(frozen=True)
class _AlertDispatchTask:
    sender: Callable[..., Any]
    args: tuple[Any, ...]

def setup_logging(log_file: str, level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("proxy_manager")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)
    logger.propagate = False

    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    try:
        file_handler: logging.Handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=LOG_ROTATION_MAX_BYTES,
            backupCount=LOG_ROTATION_BACKUP_COUNT,
            encoding="utf-8",
            delay=False,
        )
    except Exception:
        # Fallback: rotation may fail on exotic filesystems (e.g. network
        # mounts without rename). We still want some form of file logging.
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
    setattr(file_handler, "_tesseract_authoritative_file_handler", True)
    setattr(
        file_handler,
        "_tesseract_authoritative_log_path",
        str(log_path.resolve()),
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    try:
        install_structured_event_sidecar(
            logger,
            log_path,
            max_bytes=LOG_ROTATION_MAX_BYTES,
            backup_count=LOG_ROTATION_BACKUP_COUNT,
            owner_handler=file_handler,
        )
    except Exception:
        # monitor.log remains authoritative if the structured sidecar cannot
        # be opened on an unusual or read-only filesystem.
        pass
    return logger


def flush_logging_handlers(logger: logging.Logger) -> int:
    """Flush the runtime logger and its configured proxy-manager sink once."""

    candidates: list[logging.Logger] = []
    current: logging.Logger | None = logger
    while current is not None:
        candidates.append(current)
        if not current.propagate:
            break
        current = current.parent
    proxy_manager_logger = logging.getLogger("proxy_manager")
    if proxy_manager_logger not in candidates:
        candidates.append(proxy_manager_logger)

    flushed = 0
    seen: set[int] = set()
    for candidate in candidates:
        for handler in tuple(candidate.handlers):
            handler_id = id(handler)
            if handler_id in seen:
                continue
            seen.add(handler_id)
            try:
                handler.flush()
                flushed += 1
            except Exception:
                continue
    return flushed


def authoritative_log_handler_status(
    logger: logging.Logger,
    expected_path: str | Path,
) -> dict[str, object]:
    """Return the single durable monitor handler truth without mutating logging."""

    expected = str(Path(expected_path).resolve())
    handlers = [
        handler
        for handler in tuple(logger.handlers)
        if getattr(handler, "_tesseract_authoritative_file_handler", False)
    ]
    actual_paths: list[str] = []
    for handler in handlers:
        configured = str(
            getattr(handler, "_tesseract_authoritative_log_path", "") or ""
        ).strip()
        base_filename = str(getattr(handler, "baseFilename", "") or "").strip()
        candidate = configured or base_filename
        if candidate:
            try:
                candidate = str(Path(candidate).resolve())
            except OSError:
                pass
        actual_paths.append(candidate)
    valid = len(handlers) == 1 and actual_paths == [expected]
    return {
        "valid": bool(valid),
        "expected_path": expected,
        "handler_count": len(handlers),
        "actual_paths": actual_paths,
    }


class AlertManager:
    def __init__(
        self,
        config: dict[str, Any],
        logger: logging.Logger,
        *,
        delivery_store: AlertDeliveryStore | None = None,
        language: str | None = None,
    ) -> None:
        self.config = config
        self.logger = logger
        # Language used for text sent to external channels. Log lines and the
        # in-app history keep the English source text.
        self.language = normalize_alert_language(language)
        self.history_limit = int(config.get("history_limit", 200))
        self.history: deque[AlertRecord] = deque(maxlen=self.history_limit)
        self._history_lock = threading.RLock()
        # RT-6 is an in-memory source log; Qt keeps its existing AlertRecord API.
        self._alert_seq = 0
        self._sequenced_history: deque[tuple[int, AlertRecord]] = deque(
            maxlen=self.history_limit
        )
        self.max_pending_senders = DEFAULT_MAX_PENDING_SENDERS
        self.sender_worker_count = DEFAULT_ALERT_WORKERS
        self._dispatch_condition = threading.Condition(threading.RLock())
        self._dispatch_queue: queue.Queue[_AlertDispatchTask | object] = (
            queue.Queue()
        )
        self._worker_threads: list[threading.Thread] = []
        self._pending_dispatches = 0
        self._accepting_dispatches = True
        self._dispatch_stop_signaled = False
        self.policy = AlertPolicy()
        self.delivery_store = delivery_store

    def set_language(self, language: str | None) -> None:
        self.language = normalize_alert_language(language)

    def _field_labels(self) -> tuple[str, str]:
        return localized_alert_field_labels(self.language)

    def _localized_envelope(self, envelope: AlertEnvelope) -> AlertEnvelope:
        language = self.language
        title = localize_alert_text(envelope.title, language)
        message = localize_alert_text(envelope.message, language)
        severity_label = localize_alert_severity(
            envelope.alert_type,
            language,
            fallback=envelope.severity_label,
        )
        if (
            title == envelope.title
            and message == envelope.message
            and severity_label == envelope.severity_label
        ):
            return envelope
        return dataclass_replace(
            envelope,
            title=title,
            message=message,
            severity_label=severity_label,
        )

    def send_event(self, event: AlertEvent) -> bool:
        decision = self.policy.evaluate(event)
        if not decision.send or decision.event is None:
            self.logger.debug("Alert event suppressed (%s): %s", event.event_key, decision.reason)
            return False
        self._dispatch_alert(decision.event)
        return True

    def _dispatch_alert(self, event: AlertEvent) -> None:
        alert_type = event.alert_type
        envelope = AlertEnvelope(
            alert_type=alert_type.value,
            title=str(event.title or "").strip(),
            message=str(event.message or "").strip(),
            timestamp=utc_now_iso(),
            event_key=str(event.event_key or "").strip(),
            event_group=str(event.event_group or "").strip(),
        )
        level_map = {
            AlertType.WARNING: logging.WARNING,
            AlertType.CRITICAL: logging.CRITICAL,
            AlertType.EMERGENCY: logging.CRITICAL,
            AlertType.RECOVERY: logging.INFO,
            AlertType.POOL_UPDATE: logging.INFO,
            AlertType.BACKUP_REMINDER: logging.WARNING,
            AlertType.KILL_SWITCH_ON: logging.CRITICAL,
            AlertType.KILL_SWITCH_OFF: logging.INFO,
            AlertType.KILL_SWITCH_DISABLED: logging.WARNING,
            AlertType.KILL_SWITCH_OVERRIDE: logging.WARNING,
        }
        with self._history_lock:
            record = AlertRecord(
                timestamp=envelope.timestamp,
                alert_type=envelope.alert_type,
                title=envelope.title,
                message=envelope.message,
                event_key=envelope.event_key,
                event_group=envelope.event_group,
            )
            self._alert_seq += 1
            self.history.append(record)
            self._sequenced_history.append((self._alert_seq, record))
        event_params: dict[str, Any] = {
            "title": envelope.title,
            "message": envelope.message,
            "source": str(event.source or "runtime"),
            "scope": str(event.scope or ""),
            "event_group": envelope.event_group,
            "alert_type": envelope.alert_type,
            "metadata": dict(event.metadata or {}),
        }
        log_event(
            self.logger,
            level_map.get(alert_type, logging.INFO),
            envelope.event_key,
            f"[{envelope.alert_type}] {envelope.title} - {envelope.message}",
            params=event_params,
            is_key_event=True,
        )
        # Alert redesign 10-30: notices are local only. Runtime alerts stay in
        # the history and the log; nothing is pushed to external channels and
        # desktop notices come from alert_policy through the bridge.

    def _configured_alert_channels(self) -> list[dict[str, Any]]:
        return normalized_notification_channels(self.config)

    def _select_configured_channel(self, channel: str) -> tuple[dict[str, Any] | None, str | None]:
        requested = str(channel or "").strip().lower()
        if not requested:
            return None, "unsupported alert channel: "
        configured_channels = self._configured_alert_channels()
        for item in configured_channels:
            if requested == str(item.get("key", "")).lower():
                return item, None
        type_matches = [
            item
            for item in configured_channels
            if requested == str(item.get("type", "")).lower()
        ]
        if len(type_matches) == 1:
            return type_matches[0], None
        if len(type_matches) > 1:
            enabled_matches = [item for item in type_matches if self._channel_enabled(item)]
            if len(enabled_matches) == 1:
                return enabled_matches[0], None
            return None, f"ambiguous alert channel: {channel}; use the channel id"
        if requested in ALERT_NOTIFICATION_CHANNEL_TYPES:
            section = self._channel_config(self.config, requested)
            if section:
                for item in normalized_notification_channels({requested: section}):
                    return item, None
        return None, f"unsupported alert channel: {channel}"

    @staticmethod
    def _channel_enabled(channel: dict[str, Any]) -> bool:
        config = channel.get("config", {})
        return isinstance(config, dict) and coerce_bool(config.get("enabled", False), False)

    @staticmethod
    def _normalize_alert_type_filters(value: Any, channel_type: str) -> set[str]:
        return normalize_alert_type_filters(value, channel_type)

    def _channel_accepts_alert(self, channel: dict[str, Any], envelope: AlertEnvelope) -> bool:
        config = channel.get("config", {})
        channel_type = str(channel.get("type", ""))
        return alert_channel_accepts_event(
            channel_type,
            config,
            alert_type=envelope.alert_type,
            event_group=envelope.event_group,
        )

    def _send_configured_channel(
        self,
        channel: dict[str, Any],
        envelope: AlertEnvelope,
        alert_type: AlertType,
        *,
        log_failure: bool = True,
        source: str = "runtime",
    ) -> tuple[bool, str | None]:
        channel_type = str(channel.get("type") or "").strip().lower()
        config = channel.get("config", {})
        if not isinstance(config, dict):
            error = f"{channel_type or 'alert'} config must be a mapping"
            self._record_delivery(channel, envelope, outcome="failed", error=error)
            return False, error
        try:
            validate_notification_channel_config(
                channel_type,
                config,
                path=f"alerts.{channel_type or 'channel'}",
            )
        except ValueError as exc:
            error = str(exc)
            self._record_delivery(
                channel,
                envelope,
                outcome="failed",
                error=error,
            )
            return False, error
        if not coerce_bool(config.get("enabled", False), False):
            error = f"{channel_type} alert is not enabled"
            self._record_delivery(channel, envelope, outcome="failed", error=error)
            return False, error
        if channel_type == "desktop" and source == "runtime" and coerce_bool(config.get("disabled_in_native_ui"), False):
            # The desktop shell owns the real in-app/tray display and records
            # that result after it is shown. Avoid a second synthetic attempt.
            return True, None
        if channel_type not in {"desktop", "sound"}:
            # External channels are read by a person: send the text in the
            # application language. Test messages arrive already localized.
            envelope = self._localized_envelope(envelope)
        sender = {
            "desktop": lambda: self._send_desktop(envelope, config),
            "telegram": lambda: self._send_telegram(envelope, config),
            "whatsapp": lambda: self._send_whatsapp(envelope, config),
            "sound": lambda: self._send_sound(alert_type, config),
            "generic_webhook": lambda: self._send_generic_webhook(envelope, config),
            "ntfy": lambda: self._send_ntfy(envelope, config),
            "pushover": lambda: self._send_pushover(envelope, config),
            "gotify": lambda: self._send_gotify(envelope, config),
            "email_smtp": lambda: self._send_email_smtp(envelope, config),
            "email_sendgrid": lambda: self._send_email_sendgrid(envelope, config),
            "sms_twilio": lambda: self._send_sms_twilio(envelope, config),
        }.get(channel_type)
        if sender is None and channel_type in WEBHOOK_PRESET_TYPES:
            sender = lambda: self._send_webhook_preset(channel_type, envelope, config)
        if sender is None:
            error = f"unsupported alert channel type: {channel_type}"
            self._record_delivery(channel, envelope, outcome="failed", error=error)
            return False, error
        try:
            ok, error = sender()
        except Exception as exc:
            ok, error = False, self._redact_text(str(exc), config)
        self._record_delivery(
            channel,
            envelope,
            outcome="success" if ok else "failed",
            error=error or "",
        )
        if not ok and log_failure:
            log_raw_diagnostic(self.logger, "warning", "%s alert failed: %s", channel_type, error)
        return ok, error

    def _record_delivery(
        self,
        channel: dict[str, Any],
        envelope: AlertEnvelope,
        *,
        outcome: str,
        error: Any = "",
    ) -> None:
        store = self.delivery_store
        if store is None:
            return
        config = channel.get("config") if isinstance(channel.get("config"), dict) else {}
        channel_type = str(channel.get("type") or channel.get("key") or "").strip().lower()
        channel_id = str(channel.get("key") or channel_type)
        try:
            store.record(
                channel_id=channel_id,
                channel_type=channel_type,
                config=config,
                outcome=outcome,
                error=error,
                timestamp=envelope.timestamp,
            )
        except Exception:
            log_raw_diagnostic(
                self.logger,
                "warning",
                "Failed to persist alert delivery result",
                exc_info=True,
            )

    def wait_for_pending(self, timeout: float | None = None) -> bool:
        """Wait for accepted dispatches using one total timeout."""

        deadline = (
            None
            if timeout is None
            else time.monotonic() + max(0.0, float(timeout))
        )
        with self._dispatch_condition:
            while self._pending_dispatches:
                remaining = (
                    None
                    if deadline is None
                    else max(0.0, deadline - time.monotonic())
                )
                if remaining == 0:
                    break
                self._dispatch_condition.wait(timeout=remaining)
            return self._pending_dispatches == 0

    def close(self, timeout: float = 3.0) -> bool:
        """Stop accepting alerts and drain workers against one deadline."""

        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._dispatch_condition:
            self._accepting_dispatches = False
            while self._pending_dispatches:
                remaining = max(0.0, deadline - time.monotonic())
                if remaining == 0:
                    break
                self._dispatch_condition.wait(timeout=remaining)

        dropped = self._drop_queued_dispatches_on_close()
        self._record_dropped_dispatches(
            dropped,
            "alert manager closed before dispatch",
        )

        with self._dispatch_condition:
            workers = list(self._worker_threads)
            if workers and not self._dispatch_stop_signaled:
                self._dispatch_stop_signaled = True
                for _worker in workers:
                    self._dispatch_queue.put_nowait(_DISPATCH_STOP)

        for worker in workers:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining == 0:
                break
            worker.join(timeout=remaining)

        with self._dispatch_condition:
            return (
                self._pending_dispatches == 0
                and all(not worker.is_alive() for worker in workers)
            )

    def _send_async(
        self,
        sender: Callable[..., Any],
        *args: Any,
    ) -> None:
        task = _AlertDispatchTask(sender=sender, args=tuple(args))
        drop_reason = ""
        with self._dispatch_condition:
            if not self._accepting_dispatches:
                drop_reason = "alert manager is closed"
            elif self._pending_dispatches >= self.max_pending_senders:
                drop_reason = "delivery queue is full"
            else:
                self._ensure_dispatch_workers_locked()
                self._pending_dispatches += 1
                self._dispatch_queue.put_nowait(task)
                self._dispatch_condition.notify_all()
        if not drop_reason:
            return
        log_raw_diagnostic(
            self.logger,
            "warning",
            "Dropping alert sender: %s",
            drop_reason,
        )
        self._record_dropped_dispatch(task, drop_reason)

    def _ensure_dispatch_workers_locked(self) -> None:
        if self._worker_threads:
            return
        for index in range(self.sender_worker_count):
            worker = threading.Thread(
                target=self._dispatch_worker,
                name=f"alert-dispatch-{index + 1}",
                daemon=True,
            )
            self._worker_threads.append(worker)
            worker.start()

    def _dispatch_worker(self) -> None:
        while True:
            queued = self._dispatch_queue.get()
            try:
                if queued is _DISPATCH_STOP:
                    return
                if not isinstance(queued, _AlertDispatchTask):
                    continue
                try:
                    queued.sender(*queued.args)
                except Exception as exc:
                    log_raw_diagnostic(
                        self.logger,
                        "warning",
                        "Alert sender failed: %s",
                        exc,
                    )
                finally:
                    with self._dispatch_condition:
                        self._pending_dispatches = max(
                            0,
                            self._pending_dispatches - 1,
                        )
                        self._dispatch_condition.notify_all()
            finally:
                self._dispatch_queue.task_done()

    def _drop_queued_dispatches_on_close(
        self,
    ) -> list[_AlertDispatchTask]:
        dropped: list[_AlertDispatchTask] = []
        stop_markers = 0
        with self._dispatch_condition:
            while True:
                try:
                    queued = self._dispatch_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    if isinstance(queued, _AlertDispatchTask):
                        dropped.append(queued)
                        self._pending_dispatches = max(
                            0,
                            self._pending_dispatches - 1,
                        )
                    elif queued is _DISPATCH_STOP:
                        stop_markers += 1
                finally:
                    self._dispatch_queue.task_done()
            for _index in range(stop_markers):
                self._dispatch_queue.put_nowait(_DISPATCH_STOP)
            self._dispatch_condition.notify_all()
        return dropped

    def _record_dropped_dispatch(
        self,
        task: _AlertDispatchTask,
        reason: str,
    ) -> None:
        self._record_dropped_dispatches((task,), reason)

    def _record_dropped_dispatches(
        self,
        tasks: Iterable[_AlertDispatchTask],
        reason: str,
    ) -> None:
        store = self.delivery_store
        if store is None:
            return
        deliveries: list[dict[str, Any]] = []
        for task in tasks:
            args = task.args
            if (
                len(args) < 2
                or not isinstance(args[0], dict)
                or not isinstance(args[1], AlertEnvelope)
            ):
                continue
            channel = args[0]
            envelope = args[1]
            config = (
                channel.get("config")
                if isinstance(channel.get("config"), dict)
                else {}
            )
            channel_type = str(
                channel.get("type") or channel.get("key") or ""
            ).strip().lower()
            deliveries.append(
                {
                    "channel_id": str(channel.get("key") or channel_type),
                    "channel_type": channel_type,
                    "config": config,
                    "outcome": "dropped",
                    "error": reason,
                    "timestamp": envelope.timestamp,
                }
            )
        if not deliveries:
            return
        try:
            store.record_many(deliveries)
        except Exception:
            log_raw_diagnostic(
                self.logger,
                "warning",
                "Failed to persist dropped alert delivery results",
                exc_info=True,
            )

    def test_channel(self, channel: str, title: str, message: str) -> tuple[bool, str | None]:
        selected, selection_error = self._select_configured_channel(channel)
        if selected is None:
            return False, selection_error or f"unsupported alert channel: {channel}"
        return self.test_channel_entry(selected, title, message)

    def test_channel_entry(
        self,
        entry: dict[str, Any],
        title: str,
        message: str,
    ) -> tuple[bool, str | None]:
        """Send one test message through a normalized channel entry.

        The entry may describe an unsaved draft. ``enabled`` is forced on for
        the attempt so a channel can be verified before it is switched on; the
        delivery record carries the draft's transport signature, so a later
        save of the same settings shows the channel as verified.
        """

        if not isinstance(entry, dict):
            return False, "unsupported alert channel"
        channel = dict(entry)
        config = dict(channel.get("config") if isinstance(channel.get("config"), dict) else {})
        config["enabled"] = True
        channel["config"] = config
        envelope = AlertEnvelope(
            alert_type="TEST",
            title=str(title or "").strip(),
            message=str(message or "").strip(),
            timestamp=utc_now_iso(),
            event_key="test.channel",
            severity_label=localize_alert_severity("TEST", self.language),
        )
        return self._send_configured_channel(
            channel,
            envelope,
            AlertType.WARNING,
            log_failure=False,
            source="test",
        )

    def recent(self, limit: int = 100) -> list[dict[str, str]]:
        size = max(1, int(limit))
        with self._history_lock:
            records = list(self.history)[-size:]
        return [record.to_dict() for record in records]

    def recent_with_cursor(self) -> tuple[list[dict[str, Any]], int]:
        """Atomically copy the retained RT-6 log and its source watermark.

        Unlike recent(), this returns all retained records, each with alert_seq.
        No alert history is persisted/restored; sequences belong to this manager.
        """
        with self._history_lock:
            items = [
                {**record.to_dict(), "alert_seq": seq}
                for seq, record in self._sequenced_history
            ]
            return items, self._alert_seq

    def since(self, after_seq: int) -> dict[str, Any]:
        """Copy records strictly after a source cursor; never conceal eviction.

        min_seq is the oldest retained seq, or last_seq + 1 for an empty log.
        A cursor just before min_seq is continuous. Future cursors are invalid,
        including a cursor accidentally reused with a new manager/epoch.
        """
        if type(after_seq) is not int or after_seq < 0:
            raise ValueError("invalid alert cursor")
        with self._history_lock:
            if after_seq > self._alert_seq:
                raise ValueError("alert cursor exceeds watermark")
            min_seq = (
                self._sequenced_history[0][0]
                if self._sequenced_history else self._alert_seq + 1
            )
            return {
                "items": [
                    {**record.to_dict(), "alert_seq": seq}
                    for seq, record in self._sequenced_history if seq > after_seq
                ],
                "last_seq": self._alert_seq,
                "min_seq": min_seq,
                "gap": after_seq < min_seq - 1,
            }

    @staticmethod
    def _channel_config(config: dict[str, Any], channel: str) -> dict[str, Any]:
        value = config.get(channel, {}) if isinstance(config, dict) else {}
        return value if isinstance(value, dict) else {}

    def _send_desktop(self, _envelope: AlertEnvelope, desktop_config: dict[str, Any] | None = None) -> tuple[bool, str | None]:
        desktop_config = desktop_config if isinstance(desktop_config, dict) else self._channel_config(self.config, "desktop")
        if coerce_bool(desktop_config.get("disabled_in_native_ui", False), False):
            return False, "desktop alert is disabled in native UI"
        if not coerce_bool(desktop_config.get("enabled", False), False):
            return False, "desktop alert is not enabled"
        return False, "desktop notification requires native UI"

    @staticmethod
    def _desktop_payload(title: str, message: str) -> tuple[str, str]:
        return (
            AlertManager._limit_desktop_text(title, DESKTOP_TITLE_LIMIT),
            AlertManager._limit_desktop_text(message, DESKTOP_MESSAGE_LIMIT),
        )

    @staticmethod
    def _limit_desktop_text(value: str, limit: int) -> str:
        text = str(value or "").strip()
        if len(text) <= limit:
            return text
        return f"{text[: max(0, limit - 3)].rstrip()}..."

    @staticmethod
    def _limit_text(value: str, limit: int) -> str:
        text = str(value or "").strip()
        if len(text) <= limit:
            return text
        return f"{text[: max(0, limit - 3)].rstrip()}..."

    @staticmethod
    def _plain_alert_text(
        envelope: AlertEnvelope,
        *,
        include_title: bool = True,
        labels: tuple[str, str] = ("Severity", "Time"),
    ) -> str:
        severity_label, time_label = labels
        lines = []
        if include_title and envelope.title:
            lines.append(envelope.title)
        lines.extend(
            [
                f"{severity_label}: {envelope.severity}",
                f"{time_label}: {envelope.timestamp}",
            ]
        )
        if envelope.message:
            lines.extend(["", envelope.message])
        return "\n".join(lines).strip()

    @staticmethod
    def _compact_alert_text(envelope: AlertEnvelope) -> str:
        """Body for push services whose title travels in its own field."""

        return str(envelope.message or envelope.title or envelope.severity).strip()

    @staticmethod
    def _html_alert_text(
        envelope: AlertEnvelope,
        *,
        labels: tuple[str, str] = ("Severity", "Time"),
    ) -> str:
        severity_label, time_label = labels
        parts = []
        if envelope.title:
            parts.append(f"<b>{html_escape(envelope.title)}</b>")
        parts.extend(
            [
                f"<b>{html_escape(severity_label)}:</b> {html_escape(envelope.severity)}",
                f"<b>{html_escape(time_label)}:</b> {html_escape(envelope.timestamp)}",
            ]
        )
        if envelope.message:
            parts.extend(["", html_escape(envelope.message)])
        return "\n".join(parts).strip()

    @staticmethod
    def _render_template_value(value: Any, envelope: AlertEnvelope) -> str:
        text = str(value or "")
        try:
            return text.format(**envelope.values())
        except Exception:
            return text

    @staticmethod
    def _render_template_object(value: Any, envelope: AlertEnvelope) -> Any:
        if isinstance(value, str):
            return AlertManager._render_template_value(value, envelope)
        if isinstance(value, list):
            return [AlertManager._render_template_object(item, envelope) for item in value]
        if isinstance(value, dict):
            return {key: AlertManager._render_template_object(item, envelope) for key, item in value.items()}
        return value

    @staticmethod
    def _redact_text(text: str, *configs: dict[str, Any]) -> str:
        redacted = str(text or "")
        secrets: set[str] = set()
        for config in configs:
            if not isinstance(config, dict):
                continue
            for key, value in config.items():
                if isinstance(value, dict):
                    redacted = AlertManager._redact_text(redacted, value)
                    continue
                key_lower = str(key).lower()
                if isinstance(value, (list, tuple, set)):
                    if any(marker in key_lower for marker in SENSITIVE_KEYS):
                        for item in value:
                            secret = str(item or "").strip()
                            if secret:
                                secrets.add(secret)
                    for item in value:
                        if isinstance(item, dict):
                            redacted = AlertManager._redact_text(redacted, item)
                    continue
                if any(marker in key_lower for marker in SENSITIVE_KEYS):
                    secret = str(value or "").strip()
                    if secret:
                        secrets.add(secret)
        for secret in sorted(secrets, key=len, reverse=True):
            redacted = redacted.replace(secret, "***")
            redacted = redacted.replace(urllib.parse.quote(secret, safe=""), "***")
        return redact_delivery_history_text(redacted)

    @staticmethod
    def _http_settings(config: dict[str, Any]) -> tuple[float, int]:
        try:
            timeout = float(
                config.get("timeout", DEFAULT_HTTP_TIMEOUT_SECONDS)
            )
        except (TypeError, ValueError):
            timeout = float(DEFAULT_HTTP_TIMEOUT_SECONDS)
        if not math.isfinite(timeout) or timeout <= 0:
            timeout = float(DEFAULT_HTTP_TIMEOUT_SECONDS)
        try:
            retries = int(config.get("retries", DEFAULT_HTTP_RETRIES))
        except (TypeError, ValueError):
            retries = DEFAULT_HTTP_RETRIES
        return (
            min(float(MAX_HTTP_TIMEOUT_SECONDS), timeout),
            min(MAX_HTTP_RETRIES, max(0, retries)),
        )

    def _json_post(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        config: dict[str, Any],
        response_error_parser: Callable[[dict[str, Any]], str | None] | None = None,
        response_text_error_parser: Callable[[str], str | None] | None = None,
        retry_response_errors: bool = False,
    ) -> tuple[bool, str | None]:
        timeout, retries = self._http_settings(config)
        safe_headers = {"Content-Type": "application/json", **(headers or {})}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        attempt = 0
        while True:
            request = urllib.request.Request(url, data=body, headers=safe_headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    raw = response.read().decode("utf-8")
                if response_text_error_parser is not None:
                    text_error = response_text_error_parser(raw)
                    if text_error:
                        error = self._redact_text(text_error, config)
                        if not retry_response_errors or attempt >= retries:
                            return False, error
                        attempt += 1
                        self._sleep_before_retry(attempt, None)
                        continue
                try:
                    response_payload = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    response_payload = {}
                if isinstance(response_payload, dict):
                    if response_error_parser is not None:
                        parsed_error = response_error_parser(response_payload)
                        if parsed_error:
                            return False, self._redact_text(parsed_error, config)
                    if response_payload.get("ok") is False:
                        error = response_payload.get("description") or response_payload.get("error") or "remote API rejected the alert"
                        return False, self._redact_text(str(error), config)
                return True, None
            except urllib.error.HTTPError as exc:
                status = int(getattr(exc, "code", 0) or 0)
                retry_after = exc.headers.get("Retry-After") if getattr(exc, "headers", None) else None
                try:
                    error_body = exc.read().decode("utf-8")
                except Exception:
                    error_body = ""
                error = self._format_http_error(status, error_body, str(exc), config)
                if attempt >= retries or status not in {429, 500, 502, 503, 504}:
                    return False, error
                attempt += 1
                self._sleep_before_retry(attempt, retry_after)
            except Exception as exc:
                error = self._redact_text(str(exc), config)
                if attempt >= retries:
                    return False, error
                attempt += 1
                self._sleep_before_retry(attempt, None)

    def _http_post(
        self,
        url: str,
        body: bytes,
        *,
        headers: dict[str, str] | None = None,
        config: dict[str, Any],
        response_error_parser: Callable[[dict[str, Any]], str | None] | None = None,
    ) -> tuple[bool, str | None]:
        timeout, retries = self._http_settings(config)
        attempt = 0
        while True:
            request = urllib.request.Request(url, data=body, headers=headers or {}, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    raw = response.read().decode("utf-8")
                try:
                    response_payload = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    response_payload = {}
                if isinstance(response_payload, dict):
                    if response_error_parser is not None:
                        parsed_error = response_error_parser(response_payload)
                        if parsed_error:
                            return False, self._redact_text(parsed_error, config)
                    if response_payload.get("ok") is False:
                        error = response_payload.get("description") or response_payload.get("error") or "remote API rejected the alert"
                        return False, self._redact_text(str(error), config)
                return True, None
            except urllib.error.HTTPError as exc:
                status = int(getattr(exc, "code", 0) or 0)
                retry_after = exc.headers.get("Retry-After") if getattr(exc, "headers", None) else None
                try:
                    error_body = exc.read().decode("utf-8")
                except Exception:
                    error_body = ""
                error = self._format_http_error(status, error_body, str(exc), config)
                if attempt >= retries or status not in {429, 500, 502, 503, 504}:
                    return False, error
                attempt += 1
                self._sleep_before_retry(attempt, retry_after)
            except Exception as exc:
                error = self._redact_text(str(exc), config)
                if attempt >= retries:
                    return False, error
                attempt += 1
                self._sleep_before_retry(attempt, None)

    def _raw_post(
        self,
        url: str,
        text: str,
        *,
        headers: dict[str, str] | None = None,
        config: dict[str, Any],
    ) -> tuple[bool, str | None]:
        safe_headers = {"Content-Type": "text/plain; charset=utf-8", **(headers or {})}
        return self._http_post(url, str(text or "").encode("utf-8"), headers=safe_headers, config=config)

    def _form_post(
        self,
        url: str,
        fields: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        config: dict[str, Any],
        response_error_parser: Callable[[dict[str, Any]], str | None] | None = None,
    ) -> tuple[bool, str | None]:
        normalized_fields = {
            key: str(value)
            for key, value in fields.items()
            if value not in (None, "")
        }
        body = urllib.parse.urlencode(normalized_fields).encode("utf-8")
        safe_headers = {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}
        return self._http_post(url, body, headers=safe_headers, config=config, response_error_parser=response_error_parser)

    def _with_retries(
        self,
        config: dict[str, Any],
        operation: Callable[[], tuple[bool, str | None]],
        *,
        default_retries: int = 0,
    ) -> tuple[bool, str | None]:
        try:
            retries = int(config.get("retries", default_retries))
        except (TypeError, ValueError):
            retries = default_retries
        retries = min(MAX_HTTP_RETRIES, max(0, retries))
        attempt = 0
        while True:
            try:
                ok, error = operation()
            except Exception as exc:
                ok, error = False, self._redact_text(str(exc), config)
            if ok or attempt >= retries:
                return ok, error
            attempt += 1
            self._sleep_before_retry(attempt, None)

    def _format_http_error(self, status: int, body: str, fallback: str, config: dict[str, Any]) -> str:
        detail = fallback
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = error.get("message") or error.get("error_user_msg") or error.get("code")
                detail = str(message or fallback)
            elif payload.get("description"):
                detail = str(payload.get("description"))
            elif payload.get("message"):
                detail = str(payload.get("message"))
            elif payload.get("errors"):
                errors = payload.get("errors")
                if isinstance(errors, list):
                    detail = ", ".join(str(item) for item in errors if item) or fallback
                else:
                    detail = str(errors)
        elif body:
            detail = body[:300]
        prefix = f"HTTP {status}" if status else "HTTP request failed"
        return self._redact_text(f"{prefix}: {detail}", config)

    @staticmethod
    def _sleep_before_retry(attempt: int, retry_after: str | None) -> None:
        try:
            delay = float(retry_after) if retry_after else 0.0
        except (TypeError, ValueError):
            delay = 0.0
        if delay <= 0:
            delay = min(2.0, 0.25 * (2 ** max(0, attempt - 1)))
        import time

        time.sleep(delay)

    @staticmethod
    def _entries(value: Any) -> list[str]:
        if value in (None, ""):
            return []
        if isinstance(value, str):
            return [part.strip() for part in value.replace("\n", ",").split(",") if part.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(part).strip() for part in value if str(part).strip()]
        return [str(value).strip()]

    @staticmethod
    def _headers(value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        return {str(key): str(item) for key, item in value.items() if key and item is not None}

    @staticmethod
    def _append_url_path_segment(url: str, segment: str) -> str:
        parsed = urllib.parse.urlsplit(str(url or "").strip())
        base_path = parsed.path.rstrip("/")
        encoded_segment = urllib.parse.quote(str(segment or ""), safe="")
        path = f"{base_path}/{encoded_segment}" if base_path else f"/{encoded_segment}"
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, path, parsed.query, "")
        )

    @staticmethod
    def _url_with_default_query(
        url: str,
        key: str,
        value: str,
    ) -> str:
        parsed = urllib.parse.urlsplit(str(url or "").strip())
        query = urllib.parse.parse_qsl(
            parsed.query,
            keep_blank_values=True,
        )
        if not any(
            str(item_key).casefold() == str(key).casefold()
            for item_key, _item_value in query
        ):
            query.append((key, value))
        return urllib.parse.urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                urllib.parse.urlencode(query),
                parsed.fragment,
            )
        )

    @staticmethod
    def _http_header_value(value: Any) -> str:
        text = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
        try:
            text.encode("ascii")
        except UnicodeEncodeError:
            return Header(text, "utf-8").encode()
        return text

    def _template_text(self, config: dict[str, Any], key: str, default: str, envelope: AlertEnvelope) -> str:
        return self._render_template_value(config.get(key, default), envelope)

    def _default_json_payload(self, envelope: AlertEnvelope) -> dict[str, Any]:
        return self._render_template_object(DEFAULT_ALERT_PAYLOAD, envelope)

    @staticmethod
    def _basic_auth_header(username: str, password: str) -> str:
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        return f"Basic {token}"

    @staticmethod
    def _telegram_response_error(payload: dict[str, Any]) -> str | None:
        if payload.get("ok") is True:
            return None
        return str(
            payload.get("description")
            or payload.get("error")
            or "Telegram response did not confirm success"
        )

    @staticmethod
    def _teams_response_error(text: str) -> str | None:
        match = _TEAMS_EMBEDDED_HTTP_ERROR_RE.search(str(text or ""))
        if not match:
            return None
        return f"Microsoft Teams endpoint returned HTTP error {match.group(1)}"

    def _send_telegram(self, envelope: AlertEnvelope, telegram_config: dict[str, Any] | None = None) -> tuple[bool, str | None]:
        telegram_config = telegram_config if isinstance(telegram_config, dict) else self._channel_config(self.config, "telegram")
        if not coerce_bool(telegram_config.get("enabled", False), False):
            return False, "telegram alert is not enabled"
        token = str(telegram_config.get("bot_token") or "").strip()
        chat_id = str(telegram_config.get("chat_id") or "").strip()
        if not token or not chat_id:
            log_raw_diagnostic(
                self.logger,
                "warning",
                "Telegram alert enabled without bot_token/chat_id",
            )
            return False, "Telegram requires bot_token and chat_id"
        parse_mode = str(telegram_config.get("parse_mode") or "HTML").strip()
        labels = self._field_labels()
        text = (
            self._html_alert_text(envelope, labels=labels)
            if parse_mode.upper() == "HTML"
            else self._plain_alert_text(envelope, labels=labels)
        )
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": self._limit_text(text, TELEGRAM_MESSAGE_LIMIT),
        }
        if parse_mode and parse_mode.lower() not in {"none", "plain", "text"}:
            payload["parse_mode"] = parse_mode
        if coerce_bool(telegram_config.get("silent") or telegram_config.get("disable_notification"), False):
            payload["disable_notification"] = True
        if coerce_bool(telegram_config.get("protect_content"), False):
            payload["protect_content"] = True
        disable_link_preview = telegram_config.get("disable_link_preview")
        if disable_link_preview is None:
            disable_link_preview = telegram_config.get("disable_web_page_preview", True)
        if coerce_bool(disable_link_preview, True):
            payload["link_preview_options"] = {"is_disabled": True}
        thread_id = telegram_config.get("message_thread_id")
        if thread_id not in (None, ""):
            try:
                payload["message_thread_id"] = int(thread_id)
            except (TypeError, ValueError):
                return False, "Telegram message_thread_id must be an integer"
        ok, error = self._json_post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            payload,
            config=telegram_config,
            response_error_parser=self._telegram_response_error,
        )
        if not ok:
            log_raw_diagnostic(self.logger, "warning", "Telegram alert failed: %s", error)
        return ok, error

    def _send_whatsapp(self, envelope: AlertEnvelope, whatsapp_config: dict[str, Any] | None = None) -> tuple[bool, str | None]:
        whatsapp_config = whatsapp_config if isinstance(whatsapp_config, dict) else self._channel_config(self.config, "whatsapp")
        if not coerce_bool(whatsapp_config.get("enabled", False), False):
            return False, "whatsapp alert is not enabled"
        access_token = str(whatsapp_config.get("access_token") or "").strip()
        to_number = str(whatsapp_config.get("to") or "").strip()
        api_url = str(whatsapp_config.get("api_url") or "").strip()
        phone_number_id = str(whatsapp_config.get("phone_number_id") or "").strip()
        graph_api_version = str(whatsapp_config.get("graph_api_version") or "v25.0").strip()
        if not api_url and phone_number_id:
            api_url = f"https://graph.facebook.com/{graph_api_version}/{urllib.parse.quote(phone_number_id, safe='')}/messages"
        if not access_token or not to_number or not api_url:
            log_raw_diagnostic(
                self.logger,
                "warning",
                "WhatsApp alert enabled without access_token/to/api_url or phone_number_id",
            )
            return False, "WhatsApp requires access_token, recipient, and api_url or phone_number_id"
        mode = str(whatsapp_config.get("mode") or "text").strip().lower()
        if mode == "template":
            payload_or_error = self._whatsapp_template_payload(whatsapp_config, envelope, to_number)
        elif mode == "text":
            payload_or_error = self._whatsapp_text_payload(whatsapp_config, envelope, to_number)
        else:
            return False, "WhatsApp mode must be 'text' or 'template'"
        if isinstance(payload_or_error, str):
            return False, payload_or_error
        ok, error = self._json_post(
            api_url,
            payload_or_error,
            headers={
                "Authorization": f"Bearer {access_token}",
            },
            config=whatsapp_config,
        )
        if not ok:
            log_raw_diagnostic(self.logger, "warning", "WhatsApp alert failed: %s", error)
        return ok, error

    def _whatsapp_text_payload(
        self,
        config: dict[str, Any],
        envelope: AlertEnvelope,
        to_number: str,
    ) -> dict[str, Any]:
        preview_url = coerce_bool(config.get("preview_url"), False)
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_number,
            "type": "text",
            "text": {
                "preview_url": preview_url,
                "body": self._limit_text(
                    self._plain_alert_text(envelope, labels=self._field_labels()),
                    WHATSAPP_TEXT_BODY_LIMIT,
                ),
            },
        }

    def _whatsapp_template_payload(
        self,
        config: dict[str, Any],
        envelope: AlertEnvelope,
        to_number: str,
    ) -> dict[str, Any] | str:
        template_config = config.get("template", {})
        if not isinstance(template_config, dict):
            template_config = {}
        template_name = str(template_config.get("name") or config.get("template_name") or "").strip()
        if not template_name:
            return "WhatsApp template mode requires template.name"
        language_code = str(template_config.get("language") or config.get("template_language") or "en_US").strip()
        template: dict[str, Any] = {
            "name": template_name,
            "language": {"code": language_code},
        }
        components = template_config.get("components")
        if components:
            template["components"] = self._render_template_object(components, envelope)
        else:
            params = template_config.get("body_params", config.get("template_body_params"))
            if params is None:
                params = ["{severity}", "{title}", "{message}", "{timestamp}"]
            if isinstance(params, dict):
                parameters = [
                    {
                        "type": "text",
                        "parameter_name": str(key),
                        "text": self._render_template_value(value, envelope),
                    }
                    for key, value in params.items()
                ]
            elif isinstance(params, list):
                parameters = [
                    {"type": "text", "text": self._render_template_value(value, envelope)}
                    for value in params
                ]
            else:
                return "WhatsApp template body_params must be a list or mapping"
            if parameters:
                template["components"] = [{"type": "body", "parameters": parameters}]
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_number,
            "type": "template",
            "template": template,
        }

    def _send_generic_webhook(self, envelope: AlertEnvelope, config: dict[str, Any]) -> tuple[bool, str | None]:
        url = str(config.get("url") or "").strip()
        if not url:
            return False, "generic_webhook requires url"
        headers = self._headers(config.get("headers"))
        body_format = str(config.get("body_format") or "json").strip().lower()
        if body_format in {"raw", "text"}:
            text = self._template_text(
                config,
                "body_template",
                self._plain_alert_text(envelope, labels=self._field_labels()),
                envelope,
            )
            return self._raw_post(url, text, headers=headers, config=config)
        payload = config.get("payload")
        if not isinstance(payload, dict) or not payload:
            payload = self._default_json_payload(envelope)
        return self._json_post(url, self._render_template_object(payload, envelope), headers=headers, config=config)

    def _send_webhook_preset(self, channel_type: str, envelope: AlertEnvelope, config: dict[str, Any]) -> tuple[bool, str | None]:
        url = str(config.get("url") or config.get("webhook_url") or "").strip()
        if not url:
            return False, f"{channel_type} requires url"
        if channel_type == "discord":
            url = self._url_with_default_query(url, "wait", "true")
        text = self._template_text(
            config,
            "message_template",
            self._plain_alert_text(envelope, labels=self._field_labels()),
            envelope,
        )
        payload = self._webhook_preset_payload(channel_type, envelope, config, text)
        if isinstance(payload, str):
            return False, payload
        headers = self._headers(config.get("headers"))
        return self._json_post(
            url,
            payload,
            headers=headers,
            config=config,
            response_text_error_parser=(
                self._teams_response_error
                if channel_type == "teams"
                else None
            ),
            retry_response_errors=channel_type == "teams",
        )

    def _webhook_preset_payload(
        self,
        channel_type: str,
        envelope: AlertEnvelope,
        config: dict[str, Any],
        text: str,
    ) -> dict[str, Any] | str:
        payload_override = config.get("payload")
        if isinstance(payload_override, dict) and payload_override:
            rendered = self._render_template_object(
                payload_override,
                envelope,
            )
            if (
                channel_type == "discord"
                and isinstance(rendered, dict)
                and isinstance(rendered.get("content"), str)
            ):
                rendered["content"] = self._limit_text(
                    rendered["content"],
                    DISCORD_CONTENT_LIMIT,
                )
            return rendered
        if channel_type == "slack":
            return {"text": text}
        if channel_type == "discord":
            return {
                "content": self._limit_text(
                    text,
                    DISCORD_CONTENT_LIMIT,
                ),
                "allowed_mentions": {"parse": []},
            }
        if channel_type == "teams":
            return {
                "type": "message",
                "attachments": [
                    {
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "content": {
                            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                            "type": "AdaptiveCard",
                            "version": "1.4",
                            "body": [
                                {"type": "TextBlock", "weight": "Bolder", "text": envelope.title or envelope.severity, "wrap": True},
                                {"type": "TextBlock", "text": text, "wrap": True},
                            ],
                        },
                    }
                ],
            }
        if channel_type == "google_chat":
            return {"text": text}
        return f"unsupported webhook preset: {channel_type}"

    def _send_ntfy(self, envelope: AlertEnvelope, config: dict[str, Any]) -> tuple[bool, str | None]:
        server = str(config.get("server") or "https://ntfy.sh").strip().rstrip("/")
        topic = str(config.get("topic") or "").strip().strip("/")
        if not topic:
            return False, "ntfy requires topic"
        if not re.fullmatch(r"[-_A-Za-z0-9]{1,64}", topic):
            return False, (
                "ntfy topic must use only letters, numbers, hyphen, or "
                "underscore and be at most 64 characters"
            )
        headers: dict[str, str] = {
            "Title": self._http_header_value(
                self._template_text(
                    config,
                    "title_template",
                    envelope.title or envelope.severity,
                    envelope,
                )
            ),
        }
        priority = str(config.get("priority") or "").strip()
        tags = ",".join(self._entries(config.get("tags")))
        token = str(config.get("token") or "").strip()
        if priority:
            headers["Priority"] = priority
        if tags:
            headers["Tags"] = self._http_header_value(tags)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        message = self._template_text(
            config,
            "message_template",
            self._compact_alert_text(envelope),
            envelope,
        )
        return self._raw_post(
            self._append_url_path_segment(server, topic),
            message,
            headers=headers,
            config=config,
        )

    def _send_pushover(self, envelope: AlertEnvelope, config: dict[str, Any]) -> tuple[bool, str | None]:
        token = str(config.get("app_token") or "").strip()
        user = str(config.get("user_key") or "").strip()
        if not token or not user:
            return False, "Pushover requires app_token and user_key"
        fields = {
            "token": token,
            "user": user,
            "title": self._limit_text(
                self._template_text(
                    config,
                    "title_template",
                    envelope.title or envelope.severity,
                    envelope,
                ),
                PUSHOVER_TITLE_LIMIT,
            ),
            "message": self._limit_text(
                self._template_text(
                    config,
                    "message_template",
                    self._compact_alert_text(envelope),
                    envelope,
                ),
                PUSHOVER_MESSAGE_LIMIT,
            ),
            "device": config.get("device"),
            "priority": config.get("priority"),
            "sound": config.get("sound"),
        }
        return self._form_post(
            "https://api.pushover.net/1/messages.json",
            fields,
            config=config,
            response_error_parser=self._pushover_response_error,
        )

    @staticmethod
    def _pushover_response_error(payload: dict[str, Any]) -> str | None:
        status = payload.get("status")
        if not isinstance(status, bool) and status in (1, "1"):
            return None
        errors = payload.get("errors")
        if isinstance(errors, list):
            return ", ".join(str(item) for item in errors if item) or "Pushover rejected the notification"
        if errors:
            return str(errors)
        if payload.get("error"):
            return str(payload.get("error"))
        return "Pushover rejected the notification"

    def _send_gotify(self, envelope: AlertEnvelope, config: dict[str, Any]) -> tuple[bool, str | None]:
        server = str(config.get("server") or "").strip().rstrip("/")
        token = str(config.get("app_token") or "").strip()
        if not server or not token:
            return False, "Gotify requires server and app_token"
        fields = {
            "title": self._template_text(config, "title_template", envelope.title or envelope.severity, envelope),
            "message": self._template_text(
                config,
                "message_template",
                self._compact_alert_text(envelope),
                envelope,
            ),
            "priority": config.get("priority", 5),
        }
        return self._form_post(
            self._append_url_path_segment(server, "message"),
            fields,
            headers={"X-Gotify-Key": token},
            config=config,
        )

    def _send_email_smtp(self, envelope: AlertEnvelope, config: dict[str, Any]) -> tuple[bool, str | None]:
        host = str(config.get("host") or "").strip()
        from_addr = str(config.get("from_addr") or config.get("from") or "").strip()
        to_addrs = self._entries(config.get("to_addrs") or config.get("to"))
        if not host or not from_addr or not to_addrs:
            return False, "SMTP email requires host, from_addr, and to_addrs"
        try:
            port = int(config.get("port", 465 if coerce_bool(config.get("ssl"), False) else 587))
        except (TypeError, ValueError):
            port = 587
        timeout, _retries = self._http_settings(config)
        message = EmailMessage()
        message["From"] = from_addr
        message["To"] = ", ".join(to_addrs)
        message["Subject"] = self._template_text(config, "subject_template", "[Tesseract] {severity}: {title}", envelope)
        message.set_content(
            self._template_text(
                config,
                "body_template",
                self._plain_alert_text(envelope, labels=self._field_labels()),
                envelope,
            )
        )
        smtp_class = smtplib.SMTP_SSL if coerce_bool(config.get("ssl"), False) else smtplib.SMTP
        def send_once() -> tuple[bool, str | None]:
            try:
                with smtp_class(host, port, timeout=timeout) as smtp:
                    if not coerce_bool(config.get("ssl"), False) and coerce_bool(config.get("starttls"), True):
                        smtp.starttls()
                    username = str(config.get("username") or "").strip()
                    password = str(config.get("password") or "")
                    if username:
                        smtp.login(username, password)
                    smtp.send_message(message)
                return True, None
            except Exception as exc:
                return False, self._redact_text(str(exc), config)

        return self._with_retries(config, send_once)

    def _send_email_sendgrid(self, envelope: AlertEnvelope, config: dict[str, Any]) -> tuple[bool, str | None]:
        api_key = str(config.get("api_key") or "").strip()
        from_addr = str(config.get("from_addr") or config.get("from") or "").strip()
        to_addrs = self._entries(config.get("to_addrs") or config.get("to"))
        if not api_key or not from_addr or not to_addrs:
            return False, "SendGrid email requires api_key, from_addr, and to_addrs"
        payload = {
            "personalizations": [{"to": [{"email": address} for address in to_addrs]}],
            "from": {"email": from_addr},
            "subject": self._template_text(config, "subject_template", "[Tesseract] {severity}: {title}", envelope),
            "content": [
                {
                    "type": "text/plain",
                    "value": self._template_text(
                        config,
                        "body_template",
                        self._plain_alert_text(envelope, labels=self._field_labels()),
                        envelope,
                    ),
                }
            ],
        }
        url = str(config.get("api_url") or "https://api.sendgrid.com/v3/mail/send").strip()
        return self._json_post(url, payload, headers={"Authorization": f"Bearer {api_key}"}, config=config)

    def _send_sms_twilio(self, envelope: AlertEnvelope, config: dict[str, Any]) -> tuple[bool, str | None]:
        account_sid = str(config.get("account_sid") or "").strip()
        auth_token = str(config.get("auth_token") or "").strip()
        to_numbers = self._entries(config.get("to_numbers") or config.get("to"))
        from_number = str(config.get("from_number") or config.get("from") or "").strip()
        messaging_service_sid = str(config.get("messaging_service_sid") or "").strip()
        if not account_sid or not auth_token or not to_numbers or not (from_number or messaging_service_sid):
            return False, "Twilio SMS requires account_sid, auth_token, to_numbers, and from_number or messaging_service_sid"
        url = str(config.get("api_url") or f"https://api.twilio.com/2010-04-01/Accounts/{urllib.parse.quote(account_sid, safe='')}/Messages.json")
        headers = {"Authorization": self._basic_auth_header(account_sid, auth_token)}
        body = self._limit_text(
            self._template_text(config, "body_template", "{severity}: {title}\n{message}", envelope),
            1600,
        )
        last_error: str | None = None
        for to_number in to_numbers:
            fields = {
                "To": to_number,
                "Body": body,
            }
            if messaging_service_sid:
                fields["MessagingServiceSid"] = messaging_service_sid
            else:
                fields["From"] = from_number
            ok, error = self._form_post(url, fields, headers=headers, config=config)
            if not ok:
                last_error = error
        return (last_error is None), last_error

    def _send_sound(self, alert_type: AlertType, sound_config: dict[str, Any] | None = None) -> tuple[bool, str | None]:
        sound_config = sound_config if isinstance(sound_config, dict) else self._channel_config(self.config, "sound")
        if not coerce_bool(sound_config.get("enabled", False), False):
            return False, "sound alert is not enabled"
        try:
            if platform.system().lower() == "windows":
                import winsound

                frequency = int(sound_config.get("frequency", 1000))
                duration = int(sound_config.get("duration_ms", 450))
                winsound.Beep(frequency, duration)
                return True, None
            sys.stdout.write("\a")
            sys.stdout.flush()
            return True, None
        except Exception as exc:
            error = self._redact_text(str(exc), sound_config)
            log_raw_diagnostic(self.logger, "warning", "Sound alert failed: %s", error)
            return False, error
