from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from config_utils import coerce_bool, ensure_mapping_section


ALERT_CHANNEL_KEYS = ("desktop", "telegram", "whatsapp")
LEGACY_ALERT_CHANNEL_KEYS = (*ALERT_CHANNEL_KEYS, "sound")
COST_SENSITIVE_ALERT_TYPES = ("CRITICAL", "EMERGENCY", "KILL_SWITCH_ON")

ALERT_EVENT_GROUPS: tuple[tuple[str, str, str], ...] = (
    (
        "vanguard_nodes",
        "Vanguard 节点与协议",
        "核心节点不可用、单节点协议回退等。",
    ),
    (
        "route_health",
        "线路降级与恢复",
        "主线路降级、恢复、备用线路波动等。",
    ),
    (
        "emergency_fallback",
        "Bastion 应急与全断",
        "进入 Bastion 应急、全断、需要人工介入等。",
    ),
    (
        "protection",
        "熔断保护",
        "熔断开启、关闭、不可用或手动覆盖。",
    ),
    (
        "backup",
        "备份提醒",
        "降级时触发的备份提醒。",
    ),
)
ALERT_EVENT_GROUP_KEYS = tuple(group_key for group_key, _label, _hint in ALERT_EVENT_GROUPS)

ALERT_CHANNEL_GROUPS: tuple[tuple[str, str], ...] = (
    ("built_in", "内置"),
    ("chat", "聊天 / 团队"),
    ("push", "个人 Push"),
    ("email", "邮件"),
    ("sms", "短信"),
    ("generic", "通用"),
)

SUPPORTED_ALERT_CHANNEL_TYPES = (
    "desktop",
    "sound",
    "telegram",
    "whatsapp",
    "slack",
    "discord",
    "teams",
    "google_chat",
    "ntfy",
    "pushover",
    "gotify",
    "email_smtp",
    "email_sendgrid",
    "sms_twilio",
    "generic_webhook",
)
RETIRED_ALERT_CHANNEL_TYPES = (
    "whatsapp_web_bridge",
    "signal_bridge",
    "telegram_user_bridge",
    "email_outlook_profile",
    "apprise",
)
ALL_KNOWN_ALERT_CHANNEL_TYPES = (
    *SUPPORTED_ALERT_CHANNEL_TYPES,
    *RETIRED_ALERT_CHANNEL_TYPES,
)

ALERT_CHANNEL_SPECS: dict[str, dict[str, Any]] = {
    "desktop": {"group": "built_in", "label": "桌面通知", "cost": False},
    "sound": {"group": "built_in", "label": "声音提示", "cost": False},
    "telegram": {"group": "chat", "label": "Telegram", "cost": False},
    "whatsapp": {"group": "chat", "label": "WhatsApp", "cost": True},
    "slack": {"group": "chat", "label": "Slack", "cost": False},
    "discord": {"group": "chat", "label": "Discord", "cost": False},
    "teams": {"group": "chat", "label": "Microsoft Teams", "cost": False},
    "google_chat": {"group": "chat", "label": "Google Chat", "cost": False},
    "ntfy": {"group": "push", "label": "ntfy", "cost": False},
    "pushover": {"group": "push", "label": "Pushover", "cost": True},
    "gotify": {"group": "push", "label": "Gotify", "cost": False},
    "email_smtp": {"group": "email", "label": "SMTP 邮件", "cost": False},
    "email_sendgrid": {"group": "email", "label": "SendGrid 邮件", "cost": True},
    "sms_twilio": {"group": "sms", "label": "Twilio 短信", "cost": True},
    "generic_webhook": {"group": "generic", "label": "通用 Webhook", "cost": False},
}

RETIRED_ALERT_CHANNEL_SPECS: dict[str, dict[str, Any]] = {
    "whatsapp_web_bridge": {"group": "chat", "label": "WhatsApp Web local bridge"},
    "signal_bridge": {"group": "chat", "label": "Signal local bridge"},
    "telegram_user_bridge": {"group": "chat", "label": "Telegram user bridge"},
    "email_outlook_profile": {"group": "email", "label": "Outlook local bridge"},
    "apprise": {"group": "generic", "label": "Apprise"},
}

# Compatibility alias used by the existing UI/runtime. It intentionally exposes
# only the release-supported catalog; retired types are read separately below.
ALERT_NOTIFICATION_CHANNEL_TYPES = SUPPORTED_ALERT_CHANNEL_TYPES
ALERT_CHANNEL_CONTROL_FIELDS = frozenset({"enabled", "alert_types", "event_groups", "timeout", "retries"})
RESERVED_ALERT_CHANNEL_KEYS = frozenset(LEGACY_ALERT_CHANNEL_KEYS)
CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x1f\x7f]")
MULTILINE_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
INTEGER_TEXT_RE = re.compile(r"[+-]?\d+\Z")


def normalize_alert_event_groups(value: Any) -> list[str]:
    """Return unique valid event groups; callers preserve explicit empty scope."""
    if value in (None, "", []):
        return []
    if isinstance(value, str):
        raw_items = [part.strip() for part in value.replace("\n", ",").split(",")]
    elif isinstance(value, (list, tuple, set)):
        raw_items = [str(part).strip() for part in value]
    else:
        raw_items = [str(value).strip()]
    allowed = set(ALERT_EVENT_GROUP_KEYS)
    normalized: list[str] = []
    for item in raw_items:
        if item in allowed and item not in normalized:
            normalized.append(item)
    return normalized


def normalize_alert_type_filters(value: Any, channel_type: str) -> set[str]:
    """Normalize severity filters while preserving an explicit empty list.

    Channels that can create provider costs (SMS, Pushover, SendGrid,
    WhatsApp) keep a conservative critical-only default when the setting is
    missing, but an explicit empty list means the user selected all severities.
    """

    if value is None or value == "":
        return set(
            COST_SENSITIVE_ALERT_TYPES
            if alert_channel_has_cost(channel_type)
            else ()
        )
    if isinstance(value, str):
        raw_items = [
            part.strip()
            for part in value.replace("\n", ",").split(",")
        ]
    elif isinstance(value, (list, tuple, set)):
        raw_items = [str(part).strip() for part in value]
    else:
        raw_items = [str(value).strip()]
    return {item.upper() for item in raw_items if item}


def alert_channel_accepts_event(
    channel_type: str,
    config: dict[str, Any],
    *,
    alert_type: Any,
    event_group: Any,
) -> bool:
    """Return whether one channel scope accepts an alert event."""

    if not isinstance(config, dict):
        return True
    filters = normalize_alert_type_filters(
        config.get("alert_types"),
        channel_type,
    )
    if filters and str(alert_type or "").upper() not in filters:
        return False
    if "event_groups" not in config:
        return True
    event_groups = normalize_alert_event_groups(config.get("event_groups"))
    if not event_groups:
        return False
    normalized_group = str(event_group or "").strip()
    if (
        not normalized_group
        or normalized_group not in ALERT_EVENT_GROUP_KEYS
    ):
        return False
    return normalized_group in event_groups


def _alert_channel_spec(channel_type: str) -> dict[str, Any] | None:
    normalized = str(channel_type or "").strip().lower()
    return ALERT_CHANNEL_SPECS.get(normalized) or RETIRED_ALERT_CHANNEL_SPECS.get(normalized)


def alert_channel_type_label(channel_type: str) -> str:
    spec = _alert_channel_spec(channel_type)
    return str(spec.get("label") if spec else str(channel_type or "").replace("_", " ").title())


def alert_channel_group(channel_type: str) -> str:
    spec = _alert_channel_spec(channel_type)
    return str(spec.get("group") if spec else "generic")


def alert_channel_group_label(group: str) -> str:
    labels = dict(ALERT_CHANNEL_GROUPS)
    return labels.get(str(group or ""), str(group or "").replace("_", " ").title())


def alert_channel_has_cost(channel_type: str) -> bool:
    spec = _alert_channel_spec(channel_type)
    return bool(spec.get("cost")) if spec else False


def default_notification_channel_config(channel_type: str) -> dict[str, Any]:
    channel = str(channel_type or "").strip().lower()
    base: dict[str, Any] = {
        "enabled": False,
        "type": channel,
        "label": alert_channel_type_label(channel),
    }
    if channel == "generic_webhook":
        base.update({"url": "", "headers": {}, "payload": {}, "body_format": "json"})
    elif channel in {"slack", "discord", "teams", "google_chat"}:
        base.update({"url": ""})
    elif channel == "ntfy":
        base.update({"server": "https://ntfy.sh", "topic": "", "token": "", "priority": "", "tags": []})
    elif channel == "pushover":
        base.update({"app_token": "", "user_key": "", "device": "", "priority": "", "sound": ""})
    elif channel == "gotify":
        base.update({"server": "", "app_token": "", "priority": 5})
    elif channel == "email_smtp":
        base.update(
            {
                "host": "",
                "port": 587,
                "username": "",
                "password": "",
                "from_addr": "",
                "to_addrs": [],
                "starttls": True,
                "ssl": False,
            }
        )
    elif channel == "email_sendgrid":
        base.update({"api_key": "", "from_addr": "", "to_addrs": []})
    elif channel == "sms_twilio":
        base.update(
            {
                "account_sid": "",
                "auth_token": "",
                "from_number": "",
                "messaging_service_sid": "",
                "to_numbers": [],
            }
        )
    if alert_channel_has_cost(channel):
        # Paid providers start with the urgent-only scope so a fresh channel
        # never bills for routine warnings or recoveries.
        base["alert_types"] = list(COST_SENSITIVE_ALERT_TYPES)
    return base


def _has_config_value(config: dict[str, Any], *keys: str) -> bool:
    for key in keys:
        value = config.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            if value.strip():
                return True
            continue
        if isinstance(value, (list, tuple, set, dict)):
            if value:
                return True
            continue
        return True
    return False


def _validate_http_url(value: Any, *, field: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    text = value.strip()
    if not text:
        return
    if CONTROL_CHARACTER_RE.search(text):
        raise ValueError(f"{field} must not contain control characters")
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field} must be an http or https URL")


def _validate_string_field(
    config: dict[str, Any],
    key: str,
    *,
    path: str,
    allow_multiline: bool = False,
) -> None:
    if key not in config or config[key] is None:
        return
    value = config[key]
    if not isinstance(value, str):
        raise ValueError(f"{path}.{key} must be a string")
    pattern = MULTILINE_CONTROL_CHARACTER_RE if allow_multiline else CONTROL_CHARACTER_RE
    if pattern.search(value):
        raise ValueError(f"{path}.{key} must not contain control characters")


def _validate_string_fields(
    config: dict[str, Any],
    keys: tuple[str, ...],
    *,
    path: str,
    multiline_keys: frozenset[str] = frozenset(),
) -> None:
    for key in keys:
        _validate_string_field(
            config,
            key,
            path=path,
            allow_multiline=key in multiline_keys,
        )


def _validate_string_list_field(
    config: dict[str, Any],
    key: str,
    *,
    path: str,
) -> None:
    if key not in config or config[key] is None:
        return
    value = config[key]
    if not isinstance(value, list):
        raise ValueError(f"{path}.{key} must be a list")
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(f"{path}.{key}[{index}] must be a string")
        if CONTROL_CHARACTER_RE.search(item):
            raise ValueError(
                f"{path}.{key}[{index}] must not contain control characters"
            )


def _config_integer(
    config: dict[str, Any],
    key: str,
    *,
    path: str,
    allow_empty: bool = False,
) -> int | None:
    """Read an integer config field without silently truncating other types."""

    if key not in config or config[key] is None:
        return None
    value = config[key]
    if allow_empty and isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, bool):
        raise ValueError(f"{path}.{key} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and INTEGER_TEXT_RE.fullmatch(value.strip()):
        return int(value.strip())
    raise ValueError(f"{path}.{key} must be an integer")


def _config_number(
    config: dict[str, Any],
    key: str,
    *,
    path: str,
) -> float | None:
    """Read a finite numeric config field while rejecting booleans."""

    if key not in config or config[key] is None:
        return None
    value = config[key]
    if isinstance(value, bool):
        raise ValueError(f"{path}.{key} must be a number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path}.{key} must be a number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{path}.{key} must be a finite number")
    return result


def validate_notification_channel_config(
    channel_type: str,
    config: dict[str, Any],
    *,
    path: str = "notification channel",
) -> None:
    """Validate one stored notification channel without rewriting it.

    Disabled supported channels may be incomplete so example files and drafts
    remain loadable. Retired channel payloads are deliberately accepted for
    read compatibility; normalization always forces them off.
    """

    normalized_type = str(channel_type or "").strip().lower()
    if normalized_type not in ALL_KNOWN_ALERT_CHANNEL_TYPES:
        raise ValueError(f"{path} has unsupported type: {normalized_type or '<empty>'}")
    if not isinstance(config, dict):
        raise ValueError(f"{path} config must be an object")
    if normalized_type in RETIRED_ALERT_CHANNEL_TYPES:
        return

    enabled = coerce_bool(config.get("enabled", False), False)
    _validate_string_fields(config, ("label",), path=path)
    if "timeout" in config:
        timeout = _config_number(config, "timeout", path=path)
        assert timeout is not None
        if timeout <= 0 or timeout > 300:
            raise ValueError(f"{path}.timeout must be greater than 0 and at most 300")
    if "retries" in config:
        retries = _config_integer(config, "retries", path=path)
        assert retries is not None
        if retries < 0 or retries > 10:
            raise ValueError(f"{path}.retries must be between 0 and 10")
    if "alert_types" in config:
        _validate_string_list_field(config, "alert_types", path=path)
    if "event_groups" in config:
        raw_groups = config["event_groups"]
        if not isinstance(raw_groups, (list, tuple, set, str)):
            raise ValueError(f"{path}.event_groups must be a list or comma-separated string")
        raw_group_items = (
            [part.strip() for part in raw_groups.replace("\n", ",").split(",")]
            if isinstance(raw_groups, str)
            else [
                item.strip()
                for item in raw_groups
                if isinstance(item, str)
            ]
        )
        if not isinstance(raw_groups, str) and len(raw_group_items) != len(raw_groups):
            raise ValueError(f"{path}.event_groups entries must be strings")
        unknown_groups = sorted(
            {item for item in raw_group_items if item and item not in ALERT_EVENT_GROUP_KEYS}
        )
        if unknown_groups:
            raise ValueError(f"{path}.event_groups contains unsupported values: {', '.join(unknown_groups)}")
    if "headers" in config and not isinstance(config["headers"], dict):
        raise ValueError(f"{path}.headers must be an object")
    if isinstance(config.get("headers"), dict):
        for header_name, header_value in config["headers"].items():
            if not isinstance(header_name, str) or not isinstance(header_value, str):
                raise ValueError(f"{path}.headers keys and values must be strings")
            if CONTROL_CHARACTER_RE.search(header_name) or CONTROL_CHARACTER_RE.search(header_value):
                raise ValueError(f"{path}.headers must not contain control characters")
    if "payload" in config and not isinstance(config["payload"], dict):
        raise ValueError(f"{path}.payload must be an object")

    if normalized_type == "sound":
        if "frequency" in config:
            frequency = _config_integer(
                config,
                "frequency",
                path=path,
            )
            assert frequency is not None
            if frequency < 37 or frequency > 32767:
                raise ValueError(
                    f"{path}.frequency must be between 37 and 32767"
                )
        if "duration_ms" in config:
            duration = _config_integer(
                config,
                "duration_ms",
                path=path,
            )
            assert duration is not None
            if duration < 20 or duration > 10000:
                raise ValueError(
                    f"{path}.duration_ms must be between 20 and 10000"
                )
        return

    if normalized_type == "telegram":
        _validate_string_fields(
            config,
            (
                "bot_token",
                "chat_id",
                "parse_mode",
            ),
            path=path,
        )
        if enabled and not (_has_config_value(config, "bot_token") and _has_config_value(config, "chat_id")):
            raise ValueError(f"{path} requires bot_token and chat_id when enabled")
        if _has_config_value(config, "message_thread_id"):
            _config_integer(config, "message_thread_id", path=path)
        return

    if normalized_type == "whatsapp":
        _validate_string_fields(
            config,
            (
                "access_token",
                "phone_number_id",
                "to",
                "api_url",
                "mode",
                "graph_api_version",
                "template_name",
                "template_language",
            ),
            path=path,
        )
        mode = str(config.get("mode") or "text").strip().lower()
        if mode not in {"text", "template"}:
            raise ValueError(f"{path}.mode must be text or template")
        if _has_config_value(config, "api_url"):
            _validate_http_url(config.get("api_url"), field=f"{path}.api_url")
        if enabled and not (
            _has_config_value(config, "access_token")
            and _has_config_value(config, "to")
            and (_has_config_value(config, "phone_number_id") or _has_config_value(config, "api_url"))
        ):
            raise ValueError(
                f"{path} requires access_token, to, and phone_number_id or api_url when enabled"
            )
        template = config.get("template")
        if template is not None and not isinstance(template, dict):
            raise ValueError(f"{path}.template must be an object")
        if isinstance(template, dict):
            _validate_string_fields(
                template,
                ("name", "language"),
                path=f"{path}.template",
            )
            _validate_string_list_field(
                template,
                "body_params",
                path=f"{path}.template",
            )
        if enabled and mode == "template":
            template_config = template if isinstance(template, dict) else {}
            if not _has_config_value(template_config, "name") and not _has_config_value(config, "template_name"):
                raise ValueError(f"{path} requires a template name in template mode")
        return

    if normalized_type in {"slack", "discord", "teams", "google_chat", "generic_webhook"}:
        _validate_string_fields(
            config,
            (
                "url",
                "webhook_url",
                "body_format",
                "body_template",
                "message_template",
            ),
            path=path,
            multiline_keys=frozenset({"body_template", "message_template"}),
        )
        url = config.get("url") or config.get("webhook_url")
        if url:
            _validate_http_url(url, field=f"{path}.url")
        if enabled and not _has_config_value(config, "url", "webhook_url"):
            raise ValueError(f"{path} requires url when enabled")
        if normalized_type == "generic_webhook":
            body_format = str(config.get("body_format") or "json").strip().lower()
            if body_format not in {"json", "raw", "text"}:
                raise ValueError(f"{path}.body_format must be json, raw, or text")
        return

    if normalized_type == "ntfy":
        _validate_string_fields(
            config,
            ("server", "topic", "token"),
            path=path,
        )
        _validate_string_list_field(config, "tags", path=path)
        if _has_config_value(config, "server"):
            _validate_http_url(config.get("server"), field=f"{path}.server")
        if _has_config_value(config, "topic") and not re.fullmatch(
            r"[-_A-Za-z0-9]{1,64}",
            str(config.get("topic") or ""),
        ):
            raise ValueError(
                f"{path}.topic must use only letters, numbers, hyphen, "
                "or underscore and be at most 64 characters"
            )
        priority = _config_integer(
            config,
            "priority",
            path=path,
            allow_empty=True,
        )
        if priority is not None and not 1 <= priority <= 5:
            raise ValueError(f"{path}.priority must be between 1 and 5")
        if enabled and not (
            _has_config_value(config, "server") and _has_config_value(config, "topic")
        ):
            raise ValueError(f"{path} requires server and topic when enabled")
        return

    if normalized_type == "pushover":
        _validate_string_fields(
            config,
            ("app_token", "user_key", "device", "sound"),
            path=path,
        )
        priority = _config_integer(
            config,
            "priority",
            path=path,
            allow_empty=True,
        )
        if priority is not None and not -2 <= priority <= 1:
            raise ValueError(f"{path}.priority must be between -2 and 1")
        if enabled and not (
            _has_config_value(config, "app_token") and _has_config_value(config, "user_key")
        ):
            raise ValueError(f"{path} requires app_token and user_key when enabled")
        return

    if normalized_type == "gotify":
        _validate_string_fields(
            config,
            ("server", "app_token"),
            path=path,
        )
        if _has_config_value(config, "server"):
            _validate_http_url(config.get("server"), field=f"{path}.server")
        _config_integer(config, "priority", path=path)
        if enabled and not (
            _has_config_value(config, "server") and _has_config_value(config, "app_token")
        ):
            raise ValueError(f"{path} requires server and app_token when enabled")
        return

    if normalized_type == "email_smtp":
        _validate_string_fields(
            config,
            (
                "host",
                "username",
                "password",
                "from_addr",
                "from",
                "to",
                "subject_template",
                "body_template",
            ),
            path=path,
            multiline_keys=frozenset({"body_template"}),
        )
        _validate_string_list_field(config, "to_addrs", path=path)
        if "port" in config:
            port = _config_integer(config, "port", path=path)
            assert port is not None
            if port < 1 or port > 65535:
                raise ValueError(f"{path}.port must be between 1 and 65535")
        if enabled and not (
            _has_config_value(config, "host")
            and _has_config_value(config, "from_addr", "from")
            and _has_config_value(config, "to_addrs", "to")
        ):
            raise ValueError(f"{path} requires host, from_addr, and to_addrs when enabled")
        return

    if normalized_type == "email_sendgrid":
        _validate_string_fields(
            config,
            (
                "api_key",
                "from_addr",
                "from",
                "to",
                "api_url",
                "subject_template",
                "body_template",
            ),
            path=path,
            multiline_keys=frozenset({"body_template"}),
        )
        _validate_string_list_field(config, "to_addrs", path=path)
        if _has_config_value(config, "api_url"):
            _validate_http_url(config.get("api_url"), field=f"{path}.api_url")
        if enabled and not (
            _has_config_value(config, "api_key")
            and _has_config_value(config, "from_addr", "from")
            and _has_config_value(config, "to_addrs", "to")
        ):
            raise ValueError(f"{path} requires api_key, from_addr, and to_addrs when enabled")
        return

    if normalized_type == "sms_twilio":
        _validate_string_fields(
            config,
            (
                "account_sid",
                "auth_token",
                "from_number",
                "from",
                "messaging_service_sid",
                "to",
                "api_url",
                "body_template",
            ),
            path=path,
            multiline_keys=frozenset({"body_template"}),
        )
        _validate_string_list_field(config, "to_numbers", path=path)
        if _has_config_value(config, "api_url"):
            _validate_http_url(config.get("api_url"), field=f"{path}.api_url")
        if enabled and not (
            _has_config_value(config, "account_sid")
            and _has_config_value(config, "auth_token")
            and _has_config_value(config, "to_numbers", "to")
            and _has_config_value(config, "from_number", "from", "messaging_service_sid")
        ):
            raise ValueError(
                f"{path} requires account_sid, auth_token, recipients, and a sender when enabled"
            )


def notification_channel_entry(
    *,
    key: str,
    channel_type: str,
    label: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_type = str(channel_type or "").strip().lower()
    normalized_label = str(label or alert_channel_type_label(normalized_type))
    normalized_config = default_notification_channel_config(normalized_type)
    if isinstance(config, dict):
        normalized_config.update(config)
    normalized_config["type"] = normalized_type
    requested_enabled = coerce_bool(normalized_config.get("enabled", False), False)
    retired = normalized_type in RETIRED_ALERT_CHANNEL_TYPES
    if retired:
        normalized_config["enabled"] = False
    if "event_groups" in normalized_config:
        normalized_config["event_groups"] = normalize_alert_event_groups(
            normalized_config.get("event_groups")
        )
    if not isinstance(config, dict) or "label" not in config:
        normalized_config["label"] = normalized_label
    return {
        "key": str(key or normalized_type),
        "type": normalized_type,
        "group": alert_channel_group(normalized_type),
        "label": normalized_label,
        "config": normalized_config,
        "supported": normalized_type in SUPPORTED_ALERT_CHANNEL_TYPES,
        "retired": retired,
        "requested_enabled": requested_enabled,
        "diagnostic": "retired_channel_type" if retired else "",
    }


def normalized_notification_channels(alerts_config: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(alerts_config, dict):
        return []
    entries: dict[str, dict[str, Any]] = {}
    for channel_type in ALL_KNOWN_ALERT_CHANNEL_TYPES:
        value = alerts_config.get(channel_type)
        if isinstance(value, dict):
            entries[channel_type] = notification_channel_entry(
                key=channel_type,
                channel_type=channel_type,
                label=str(value.get("label") or alert_channel_type_label(channel_type)),
                config=value,
            )

    raw_channels = alerts_config.get("channels", [])
    if isinstance(raw_channels, list):
        for index, item in enumerate(raw_channels):
            if not isinstance(item, dict):
                continue
            channel_type = str(item.get("type") or item.get("channel") or item.get("kind") or "").strip().lower()
            if channel_type not in ALL_KNOWN_ALERT_CHANNEL_TYPES:
                continue
            key = str(item.get("id") or item.get("key") or item.get("name") or f"{channel_type}-{index + 1}").strip()
            label = str(item.get("label") or item.get("name") or alert_channel_type_label(channel_type))
            config = dict(item.get("config") if isinstance(item.get("config"), dict) else {})
            for field, value in item.items():
                if field not in {"id", "key", "name", "label", "type", "channel", "kind", "group", "cost", "config"}:
                    config.setdefault(field, value)
            for field in ALERT_CHANNEL_CONTROL_FIELDS:
                if field in item:
                    config[field] = item[field]
            config["enabled"] = coerce_bool(config.get("enabled", False), False)
            config.setdefault("label", label)
            if key in entries:
                continue
            entries[key] = notification_channel_entry(key=key, channel_type=channel_type, label=label, config=config)
    return list(entries.values())


@dataclass(frozen=True)
class AlertConfigFormState:
    active_channels: set[str]
    telegram_token: str
    telegram_chat: str
    telegram_parse_mode: str
    telegram_thread_id: str
    telegram_silent: bool
    telegram_protect_content: bool
    telegram_disable_link_preview: bool
    whatsapp_token: str
    whatsapp_phone_id: str
    whatsapp_to: str
    whatsapp_api_url: str
    whatsapp_mode: str
    whatsapp_template_name: str
    whatsapp_template_language: str
    whatsapp_template_params: list[str]


def disable_external_desktop_notifications(config: dict) -> dict:
    alerts_config = config.get("alerts")
    if not isinstance(alerts_config, dict):
        return config
    desktop_config = alerts_config.get("desktop")
    if not isinstance(desktop_config, dict):
        return config
    desktop_config["enabled"] = False
    desktop_config["disabled_in_native_ui"] = True
    return config


def effective_alerts_config(runtime_config: dict[str, Any], local_config: dict[str, Any]) -> dict[str, Any]:
    local_alerts = local_config.get("alerts") if isinstance(local_config.get("alerts"), dict) else {}
    runtime_alerts = runtime_config.get("alerts", {}) if isinstance(runtime_config.get("alerts"), dict) else {}
    merged = dict(runtime_alerts)
    for key, value in local_alerts.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def alert_config_form_state(runtime_config: dict[str, Any], local_config: dict[str, Any]) -> AlertConfigFormState:
    alerts = effective_alerts_config(runtime_config, local_config)
    telegram = alerts.get("telegram", {}) if isinstance(alerts.get("telegram"), dict) else {}
    whatsapp = alerts.get("whatsapp", {}) if isinstance(alerts.get("whatsapp"), dict) else {}
    desktop = alerts.get("desktop", {}) if isinstance(alerts.get("desktop"), dict) else {}
    template = whatsapp.get("template", {}) if isinstance(whatsapp.get("template"), dict) else {}
    body_params = template.get("body_params", whatsapp.get("template_body_params", []))
    if isinstance(body_params, dict):
        template_params = [str(value) for value in body_params.values()]
    elif isinstance(body_params, list):
        template_params = [str(value) for value in body_params]
    else:
        template_params = []
    active_channels = {
        str(item.get("key") or item.get("type") or "")
        for item in normalized_notification_channels(alerts)
        if isinstance(item.get("config"), dict) and coerce_bool(item["config"].get("enabled"), False)
    }
    active_channels.discard("")
    return AlertConfigFormState(
        active_channels=active_channels,
        telegram_token=str(telegram.get("bot_token", "")),
        telegram_chat=str(telegram.get("chat_id", "")),
        telegram_parse_mode=str(telegram.get("parse_mode", "HTML") or "HTML"),
        telegram_thread_id=str(telegram.get("message_thread_id", "")),
        telegram_silent=coerce_bool(telegram.get("silent") or telegram.get("disable_notification"), False),
        telegram_protect_content=coerce_bool(telegram.get("protect_content"), False),
        telegram_disable_link_preview=coerce_bool(
            telegram.get("disable_link_preview", telegram.get("disable_web_page_preview", True)),
            True,
        ),
        whatsapp_token=str(whatsapp.get("access_token", "")),
        whatsapp_phone_id=str(whatsapp.get("phone_number_id", "")),
        whatsapp_to=str(whatsapp.get("to", "")),
        whatsapp_api_url=str(whatsapp.get("api_url", "")),
        whatsapp_mode=str(whatsapp.get("mode", "text") or "text"),
        whatsapp_template_name=str(template.get("name", whatsapp.get("template_name", "")) or ""),
        whatsapp_template_language=str(template.get("language", whatsapp.get("template_language", "en_US")) or "en_US"),
        whatsapp_template_params=template_params,
    )


def disable_all_alert_channels_payload(payload: dict[str, Any]) -> dict[str, Any]:
    alerts = ensure_mapping_section(payload, "alerts")
    for key in LEGACY_ALERT_CHANNEL_KEYS:
        channel_config = alerts.setdefault(key, {})
        if isinstance(channel_config, dict):
            channel_config["enabled"] = False
    for key in ALL_KNOWN_ALERT_CHANNEL_TYPES:
        if key in LEGACY_ALERT_CHANNEL_KEYS:
            continue
        channel_config = alerts.get(key)
        if isinstance(channel_config, dict):
            channel_config["enabled"] = False
    raw_channels = alerts.get("channels")
    if isinstance(raw_channels, list):
        for item in raw_channels:
            if not isinstance(item, dict):
                continue
            item["enabled"] = False
            config = item.get("config")
            if isinstance(config, dict):
                config["enabled"] = False
    return payload


def delete_alert_channel_payload(payload: dict[str, Any], channel: str) -> dict[str, Any]:
    alerts = ensure_mapping_section(payload, "alerts")
    target = str(channel or "").strip()
    alerts.pop(target, None)
    raw_channels = alerts.get("channels")
    if isinstance(raw_channels, list):
        filtered_channels = []
        for index, item in enumerate(raw_channels):
            if not isinstance(item, dict):
                filtered_channels.append(item)
                continue
            channel_type = str(item.get("type") or item.get("channel") or item.get("kind") or "").strip().lower()
            identifiers = {
                str(item.get("id") or "").strip(),
                str(item.get("key") or "").strip(),
                str(item.get("name") or "").strip(),
            }
            if channel_type:
                identifiers.add(f"{channel_type}-{index + 1}")
            if target and target in identifiers:
                continue
            filtered_channels.append(item)
        alerts["channels"] = filtered_channels
    return payload


def upsert_notification_channel_payload(payload: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    alerts = ensure_mapping_section(payload, "alerts")
    channel_type = str(entry.get("type") or "").strip().lower()
    if channel_type not in ALERT_NOTIFICATION_CHANNEL_TYPES:
        raise ValueError(f"unsupported notification channel type: {channel_type}")
    key = str(entry.get("key") or entry.get("id") or channel_type).strip()
    label = str(entry.get("label") or alert_channel_type_label(channel_type))
    config = dict(entry.get("config") if isinstance(entry.get("config"), dict) else {})
    enabled_value = entry["enabled"] if "enabled" in entry else config.get("enabled", False)
    config["enabled"] = coerce_bool(enabled_value, False)
    for field in ALERT_CHANNEL_CONTROL_FIELDS:
        if field != "enabled" and field in entry:
            config[field] = entry[field]
    config.setdefault("label", label)
    validate_notification_channel_config(
        channel_type,
        config,
        path=f"alerts.{key or channel_type}",
    )
    if "event_groups" in config:
        config["event_groups"] = normalize_alert_event_groups(
            config.get("event_groups")
        )
    if channel_type in LEGACY_ALERT_CHANNEL_KEYS:
        existing = alerts.get(channel_type)
        if not isinstance(existing, dict):
            existing = {}
            alerts[channel_type] = existing
        existing.update(config)
        return payload

    raw_channels = alerts.get("channels")
    if not isinstance(raw_channels, list):
        raw_channels = []
        alerts["channels"] = raw_channels
    if key in RESERVED_ALERT_CHANNEL_KEYS:
        raise ValueError(f"notification channel key is reserved: {key}")
    stored = {
        "id": key,
        "type": channel_type,
        "group": alert_channel_group(channel_type),
        "label": label,
        "enabled": bool(config.get("enabled", False)),
        "config": config,
    }
    for index, item in enumerate(raw_channels):
        if isinstance(item, dict) and str(item.get("id") or item.get("key") or item.get("name") or "") == key:
            existing_type = str(item.get("type") or item.get("channel") or item.get("kind") or "").strip().lower()
            if existing_type != channel_type:
                raise ValueError(f"notification channel key already belongs to {existing_type or 'unknown'}: {key}")
            raw_channels[index] = stored
            break
    else:
        raw_channels.append(stored)
    return payload
