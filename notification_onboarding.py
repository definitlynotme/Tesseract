from __future__ import annotations

import io
import json
import re
import secrets
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from alert_config import (
    ALERT_NOTIFICATION_CHANNEL_TYPES,
    alert_channel_group,
    alert_channel_has_cost,
    alert_channel_type_label,
    normalized_notification_channels,
    upsert_notification_channel_payload,
)
from config_utils import coerce_bool, ensure_mapping_section


SESSION_TTL_SECONDS = 10 * 60
DEFAULT_TIMEOUT_SECONDS = 10

SECRET_KEY_MARKERS = (
    "access_token",
    "api_key",
    "app_token",
    "authorization",
    "auth_token",
    "bot_token",
    "client_secret",
    "key",
    "password",
    "secret",
    "token",
    "url",
    "webhook",
)
SUMMARY_SCALAR_ALLOWLIST = {"id", "key", "label", "provider", "mode", "enabled", "priority", "sound", "device", "topic", "bot_username", "chat_title"}
SUMMARY_HOST_KEYS = {"server"}

WEBHOOK_TYPES = {"slack", "discord", "teams", "google_chat", "generic_webhook"}


ONBOARDING_OPTIONS: dict[str, list[dict[str, Any]]] = {
    "desktop": [],
    "sound": [],
    "telegram": [
        {
            "mode": "bot_start_qr",
            "label": "Telegram bot start link",
            "qr": True,
            "automatic": "semi",
            "requires": ["bot_token"],
        },
    ],
    "whatsapp": [],
    "slack": [],
    "discord": [],
    "teams": [],
    "google_chat": [],
    "ntfy": [
        {
            "mode": "qr_subscribe",
            "label": "Open ntfy topic on another device",
            "qr": True,
            "automatic": "auxiliary",
            "auxiliary": True,
        },
    ],
    "pushover": [
        {
            "mode": "subscription_qr",
            "label": "Open Pushover subscription link",
            "qr": True,
            "automatic": "auxiliary",
            "auxiliary": True,
            "requires": ["subscription_code"],
            "cost": True,
        },
    ],
    "gotify": [],
    "email_smtp": [],
    "email_sendgrid": [],
    "sms_twilio": [],
    "generic_webhook": [],
}

ONBOARDING_PREREQUISITE_SPECS: dict[tuple[str, str], list[dict[str, Any]]] = {
    ("telegram", "bot_start_qr"): [
        {"key": "bot_token", "secret": True},
    ],
    ("pushover", "subscription_qr"): [
        {"key": "subscription_code", "secret": True},
    ],
}

ONBOARDING_RESULT_KINDS: dict[tuple[str, str], str] = {
    ("telegram", "bot_start_qr"): "bot_pairing",
    ("ntfy", "qr_subscribe"): "auxiliary_link",
    ("pushover", "subscription_qr"): "auxiliary_link",
}

SMTP_PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "gmail": {"host": "smtp.gmail.com", "port": 587, "starttls": True, "ssl": False},
    "google": {"host": "smtp.gmail.com", "port": 587, "starttls": True, "ssl": False},
    "outlook": {"host": "smtp.office365.com", "port": 587, "starttls": True, "ssl": False},
    "office365": {"host": "smtp.office365.com", "port": 587, "starttls": True, "ssl": False},
    "custom": {},
}


class OnboardingError(ValueError):
    pass


@dataclass
class OnboardingSession:
    session_id: str
    channel_type: str
    mode: str
    created_at: float
    expires_at: float
    nonce: str
    params: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    open_url: str = ""
    qr_payload: str = ""
    qr_svg: str | None = None
    required_inputs: list[str] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    qr_error: str = ""
    availability: bool = True
    availability_reason: str = ""


def _onboarding_result_kind(channel_type: str, mode: str) -> str:
    configured = ONBOARDING_RESULT_KINDS.get((channel_type, mode))
    if configured:
        return configured
    return "manual_config"


def _is_auxiliary_mode(channel_type: str, mode: str) -> bool:
    return any(
        str(spec.get("mode") or "") == mode and bool(spec.get("auxiliary"))
        for spec in ONBOARDING_OPTIONS.get(channel_type, [])
    )


def _public_onboarding_mode(channel_type: str, spec: dict[str, Any]) -> dict[str, Any]:
    mode = str(spec.get("mode") or "")
    response = dict(spec)
    response["prerequisite_specs"] = [
        dict(item)
        for item in ONBOARDING_PREREQUISITE_SPECS.get((channel_type, mode), [])
    ]
    response["result_kind"] = _onboarding_result_kind(channel_type, mode)
    response["availability"] = True
    response["availability_reason"] = ""
    response["can_start"] = True
    response["allowed_actions"] = ["start"]
    return response


def list_onboarding_options() -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for channel_type in ALERT_NOTIFICATION_CHANNEL_TYPES:
        specs = ONBOARDING_OPTIONS.get(channel_type, [])
        options.append(
            {
                "type": channel_type,
                "group": alert_channel_group(channel_type),
                "label": alert_channel_type_label(channel_type),
                "cost": alert_channel_has_cost(channel_type),
                "modes": [_public_onboarding_mode(channel_type, spec) for spec in specs],
            }
        )
    return options


def notification_channel_public_state(alerts_config: dict[str, Any]) -> list[dict[str, Any]]:
    public_channels: list[dict[str, Any]] = []
    for item in normalized_notification_channels(alerts_config if isinstance(alerts_config, dict) else {}):
        config = item.get("config", {}) if isinstance(item.get("config"), dict) else {}
        channel_type = str(item.get("type") or "").strip().lower()
        retired = bool(item.get("retired"))
        enabled = coerce_bool(config.get("enabled", False), False) and not retired
        public_channels.append(
            {
                "id": str(item.get("key") or ""),
                "type": channel_type,
                "group": str(item.get("group") or alert_channel_group(channel_type)),
                "label": str(item.get("label") or alert_channel_type_label(channel_type)),
                "enabled": enabled,
                "cost": alert_channel_has_cost(channel_type),
                "secret_present": _secret_present(config),
                "status": "retired" if retired else ("enabled" if enabled else "configured"),
                "retired": retired,
                "requested_enabled": bool(item.get("requested_enabled")),
                "diagnostic": str(item.get("diagnostic") or ""),
                "summary": _safe_channel_summary(channel_type, config),
            }
        )
    return public_channels


class NotificationOnboardingService:
    def __init__(
        self,
        *,
        urlopen: Callable[..., Any] | None = None,
        now: Callable[[], float] | None = None,
        token_factory: Callable[[int], str] | None = None,
    ) -> None:
        self._urlopen = urlopen or urllib.request.urlopen
        self._now = now or time.time
        self._token_factory = token_factory or secrets.token_urlsafe
        self._sessions: dict[str, OnboardingSession] = {}

    def list_onboarding_options(self) -> list[dict[str, Any]]:
        return list_onboarding_options()

    def start_onboarding(self, channel_type: str, mode: str | None = None, params: dict[str, Any] | None = None) -> dict[str, Any]:
        normalized_type = str(channel_type or "").strip().lower()
        if normalized_type not in ALERT_NOTIFICATION_CHANNEL_TYPES:
            raise OnboardingError(f"unsupported notification channel type: {channel_type}")
        selected_mode = mode or _default_mode(normalized_type)
        selected_spec = next(
            (
                item
                for item in ONBOARDING_OPTIONS.get(normalized_type, [])
                if str(item.get("mode")) == selected_mode
            ),
            None,
        )
        if selected_spec is None:
            raise OnboardingError(f"unsupported onboarding mode for {normalized_type}: {selected_mode}")
        safe_params = dict(params if isinstance(params, dict) else {})
        session = self._new_session(normalized_type, selected_mode, safe_params)
        starter = {
            "bot_start_qr": self._start_telegram_bot_qr,
            "qr_subscribe": self._start_ntfy_qr,
            "subscription_qr": self._start_pushover_subscription,
        }[selected_mode]
        starter(session)
        self._sessions[session.session_id] = session
        return self._session_response(session)

    def poll_onboarding(self, session_id: str) -> dict[str, Any]:
        session = self._get_session(session_id)
        if session.mode == "bot_start_qr":
            self._poll_telegram_bot_qr(session)
        return self._session_response(session)

    def complete_onboarding(
        self,
        session_id: str,
        payload: dict[str, Any],
        completion_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session = self._get_session(session_id)
        data = dict(completion_payload if isinstance(completion_payload, dict) else {})
        if _is_auxiliary_mode(session.channel_type, session.mode):
            raise OnboardingError("auxiliary QR links do not configure or enable notification channels")
        completer = {
            "bot_start_qr": self._complete_telegram,
        }[session.mode]
        channel_id = completer(session, payload, data)
        session.status = "configured"
        alerts = ensure_mapping_section(payload, "alerts")
        public_channel = _find_public_channel(alerts, channel_id)
        return {"status": session.status, "channel_id": channel_id, "channel": public_channel}

    def cancel_onboarding(self, session_id: str) -> dict[str, Any]:
        session = self._sessions.pop(str(session_id or ""), None)
        if session is None:
            return {"status": "missing", "session_id": str(session_id or "")}
        session.status = "cancelled"
        return self._session_response(session)

    def _new_session(self, channel_type: str, mode: str, params: dict[str, Any]) -> OnboardingSession:
        now = self._now()
        session_id = _compact_token(self._token_factory(16))
        nonce = _compact_token(self._token_factory(12))
        return OnboardingSession(
            session_id=session_id,
            channel_type=channel_type,
            mode=mode,
            created_at=now,
            expires_at=now + SESSION_TTL_SECONDS,
            nonce=nonce,
            params=params,
        )

    def _get_session(self, session_id: str) -> OnboardingSession:
        key = str(session_id or "").strip()
        session = self._sessions.get(key)
        if session is None:
            raise OnboardingError(f"unknown onboarding session: {session_id}")
        if self._now() > session.expires_at:
            session.status = "expired"
            raise OnboardingError(f"onboarding session expired: {session_id}")
        return session

    def _session_response(self, session: OnboardingSession) -> dict[str, Any]:
        result_kind = _onboarding_result_kind(session.channel_type, session.mode)
        status = str(session.status or "").strip().lower()
        terminal = status in {"blocked", "cancelled", "configured", "expired", "failed"}
        can_start = bool(
            session.availability
            and status == "pending"
            and not session.open_url
        )
        can_poll = bool(
            session.availability
            and not terminal
            and (
                (session.mode == "bot_start_qr" and bool(session.open_url) and status == "pending")
            )
        )
        auxiliary = _is_auxiliary_mode(session.channel_type, session.mode)
        can_complete = bool(session.availability and status == "ready" and not auxiliary)
        allowed_actions: list[str] = []
        if can_poll:
            allowed_actions.append("poll")
        if can_complete:
            allowed_actions.append("complete")
        if session.open_url:
            allowed_actions.extend(("open_link", "copy_link"))
        if not terminal:
            allowed_actions.append("cancel")
        return {
            "session_id": session.session_id,
            "channel_type": session.channel_type,
            "mode": session.mode,
            "status": session.status,
            "result_kind": result_kind,
            "prerequisite_specs": [
                dict(item)
                for item in ONBOARDING_PREREQUISITE_SPECS.get((session.channel_type, session.mode), [])
            ],
            "availability": bool(session.availability),
            "availability_reason": str(session.availability_reason or ""),
            "can_start": can_start,
            "can_poll": can_poll,
            "can_complete": can_complete,
            "auxiliary": auxiliary,
            "allowed_actions": allowed_actions,
            "expires_at": session.expires_at,
            "open_url": session.open_url,
            "qr_payload": session.qr_payload,
            "qr_svg": session.qr_svg,
            "qr_available": bool(session.qr_svg),
            "qr_error": str(session.qr_error or ""),
            "required_inputs": list(session.required_inputs),
            "redacted_summary": _redacted_summary(session.params, session.result),
            "error": _redact_text(session.error, session.params, session.result),
        }

    def _set_qr(self, session: OnboardingSession, payload: str) -> None:
        session.qr_payload = payload
        session.open_url = payload
        session.qr_svg, session.qr_error = _qr_svg(payload)

    def _json_request(
        self,
        url: str,
        *,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        request = urllib.request.Request(url, method="GET")
        with self._urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise OnboardingError(f"remote API returned non-JSON response: {exc}") from exc
        if not isinstance(parsed, dict):
            raise OnboardingError("remote API returned an unexpected response")
        return parsed

    def _start_telegram_bot_qr(self, session: OnboardingSession) -> None:
        token = str(session.params.get("bot_token") or "").strip()
        if not token:
            session.required_inputs = ["bot_token"]
            return
        response = self._json_request(
            f"https://api.telegram.org/bot{urllib.parse.quote(token, safe='')}/getMe"
        )
        if response.get("ok") is False:
            raise OnboardingError(_redact_text(str(response.get("description") or "Telegram rejected the bot token"), session.params))
        result = response.get("result") if isinstance(response.get("result"), dict) else {}
        username = str(result.get("username") or "").strip()
        if not username:
            raise OnboardingError("Telegram bot token did not return a bot username")
        session.params["bot_username"] = username
        start_value = f"tesseract_{session.nonce}"[:64]
        session.params["start_value"] = start_value
        self._set_qr(session, f"https://t.me/{urllib.parse.quote(username, safe='')}?start={urllib.parse.quote(start_value, safe='')}")

    def _poll_telegram_bot_qr(self, session: OnboardingSession) -> None:
        token = str(session.params.get("bot_token") or "").strip()
        if not token:
            session.required_inputs = ["bot_token"]
            return
        query = {"timeout": 0, "allowed_updates": json.dumps(["message"])}
        if session.params.get("update_offset") not in (None, ""):
            query["offset"] = str(session.params.get("update_offset"))
        url = f"https://api.telegram.org/bot{urllib.parse.quote(token, safe='')}/getUpdates?{urllib.parse.urlencode(query)}"
        response = self._json_request(url)
        if response.get("ok") is False:
            session.error = str(response.get("description") or "Telegram getUpdates failed")
            return
        updates = response.get("result") if isinstance(response.get("result"), list) else []
        start_value = str(session.params.get("start_value") or "")
        for update in updates:
            if not isinstance(update, dict):
                continue
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                session.params["update_offset"] = update_id + 1
            message = update.get("message") if isinstance(update.get("message"), dict) else {}
            text = str(message.get("text") or "")
            if start_value and start_value not in text:
                continue
            chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
            chat_id = chat.get("id")
            if chat_id in (None, ""):
                continue
            session.result.update(
                {
                    "chat_id": str(chat_id),
                    "chat_title": chat.get("title") or chat.get("username") or chat.get("first_name") or str(chat_id),
                }
            )
            session.status = "ready"
            return

    def _start_ntfy_qr(self, session: OnboardingSession) -> None:
        server = _strip_trailing_slash(str(session.params.get("server") or "https://ntfy.sh").strip())
        _require_http_url(server, label="ntfy server")
        topic = _safe_topic(str(session.params.get("topic") or f"tesseract-{_compact_token(self._token_factory(8)).lower()}"))
        session.params["server"] = server
        session.params["topic"] = topic
        self._set_qr(session, f"{server}/{urllib.parse.quote(topic, safe='')}")
        session.status = "ready"

    def _start_pushover_subscription(self, session: OnboardingSession) -> None:
        code = str(session.params.get("subscription_code") or "").strip()
        if not code:
            session.required_inputs = ["subscription_code"]
            return
        if not re.fullmatch(r"[A-Za-z0-9_-]{4,128}", code):
            raise OnboardingError("Pushover subscription code has an invalid format")
        self._set_qr(session, f"https://pushover.net/subscribe/{urllib.parse.quote(code, safe='')}")
        session.status = "ready"

    def _complete_telegram(self, session: OnboardingSession, payload: dict[str, Any], data: dict[str, Any]) -> str:
        chat_id = str(data.get("chat_id") or session.result.get("chat_id") or "").strip()
        token = str(session.params.get("bot_token") or data.get("bot_token") or "").strip()
        if not token or not chat_id:
            raise OnboardingError("Telegram onboarding requires bot_token and discovered chat_id")
        config = _base_config(session, data)
        config.update(
            {
                "bot_token": token,
                "chat_id": chat_id,
                "parse_mode": data.get("parse_mode", session.params.get("parse_mode", "HTML")),
                "message_thread_id": data.get("message_thread_id", session.params.get("message_thread_id", "")),
                "silent": coerce_bool(data.get("silent", session.params.get("silent", True)), True),
                "protect_content": coerce_bool(data.get("protect_content", session.params.get("protect_content", False)), False),
                "disable_link_preview": coerce_bool(data.get("disable_link_preview", session.params.get("disable_link_preview", True)), True),
            }
        )
        channel_id = _channel_id(session, data)
        upsert_notification_channel_payload(payload, {"id": channel_id, "type": "telegram", "label": _label(session, data), "config": config})
        return channel_id

def _default_mode(channel_type: str) -> str:
    options = ONBOARDING_OPTIONS.get(channel_type, [])
    return str(options[0].get("mode")) if options else ""


def _find_public_channel(alerts_config: dict[str, Any], channel_id: str) -> dict[str, Any] | None:
    for item in notification_channel_public_state(alerts_config):
        if item.get("id") == channel_id:
            return item
    return None


def _base_config(session: OnboardingSession, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": coerce_bool(data.get("enabled"), False),
        "label": _label(session, data),
    }


def _label(session: OnboardingSession, data: dict[str, Any]) -> str:
    return str(data.get("label") or session.params.get("label") or alert_channel_type_label(session.channel_type))


def _channel_id(session: OnboardingSession, data: dict[str, Any]) -> str:
    return str(data.get("id") or data.get("key") or session.params.get("id") or session.params.get("key") or session.channel_type).strip()


def _redacted_summary(*configs: dict[str, Any]) -> dict[str, Any]:
    combined: dict[str, Any] = {}
    for config in configs:
        if not isinstance(config, dict):
            continue
        for key, value in config.items():
            key_text = str(key)
            key_lower = key_text.lower()
            if _is_secret_key(key_text):
                if _has_value(value):
                    combined[f"{key_text}_present"] = True
                continue
            if isinstance(value, dict):
                nested = _redacted_summary(value)
                if nested:
                    combined[key_text] = nested
            elif isinstance(value, (list, tuple, set)):
                combined[f"{key_text}_count"] = len([item for item in value if _has_value(item)])
            elif key_lower in SUMMARY_HOST_KEYS and _has_value(value):
                host = _url_host(str(value))
                combined[f"{key_text}_host"] = host or True
            elif key_lower in SUMMARY_SCALAR_ALLOWLIST and _has_value(value):
                combined[key_text] = value
            elif _has_value(value):
                combined[f"{key_text}_present"] = True
    return combined


def _secret_present(value: Any, key_hint: str = "") -> bool:
    if isinstance(value, dict):
        return any(_secret_present(item, str(key)) for key, item in value.items())
    if isinstance(value, (list, tuple, set)):
        return any(_secret_present(item, key_hint) for item in value)
    return _is_secret_key(key_hint) and _has_value(value)


def _safe_channel_summary(channel_type: str, config: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    if channel_type in WEBHOOK_TYPES:
        host = _url_host(str(config.get("url") or config.get("webhook_url") or ""))
        if host:
            summary["webhook_host"] = host
    if channel_type == "telegram":
        if config.get("chat_id"):
            summary["chat_id_present"] = True
        if config.get("bot_username"):
            summary["bot_username"] = config.get("bot_username")
    if channel_type == "whatsapp":
        if config.get("phone_number_id"):
            summary["phone_number_id_present"] = True
        if config.get("to"):
            summary["recipient_present"] = True
        summary["mode"] = config.get("mode", "text")
    if channel_type == "ntfy":
        summary["server_host"] = _url_host(str(config.get("server") or ""))
        if config.get("topic"):
            summary["topic"] = str(config.get("topic"))
    if channel_type == "pushover":
        if config.get("device"):
            summary["device"] = config.get("device")
        if config.get("user_key") or config.get("group_key") or config.get("subscription_code"):
            summary["recipient_present"] = True
    if channel_type == "gotify":
        summary["server_host"] = _url_host(str(config.get("server") or ""))
    if channel_type in {"email_smtp", "email_sendgrid"}:
        summary["from_present"] = bool(config.get("from_addr") or config.get("from"))
        summary["to_count"] = len(_entries(config.get("to_addrs") or config.get("to")))
        if config.get("provider"):
            summary["provider"] = config.get("provider")
    if channel_type == "sms_twilio":
        summary["to_count"] = len(_entries(config.get("to_numbers") or config.get("to")))
        summary["cost_sensitive"] = True
    return {key: value for key, value in summary.items() if value not in (None, "", [], {})}


def _redact_text(text: str, *configs: dict[str, Any]) -> str:
    redacted = str(text or "")
    for config in configs:
        if not isinstance(config, dict):
            continue
        for key, value in config.items():
            if isinstance(value, dict):
                redacted = _redact_text(redacted, value)
                continue
            if isinstance(value, (list, tuple, set)):
                if _is_secret_key(str(key)):
                    for item in value:
                        redacted = _replace_secret(redacted, item)
                for item in value:
                    if isinstance(item, dict):
                        redacted = _redact_text(redacted, item)
                continue
            if _is_secret_key(str(key)):
                redacted = _replace_secret(redacted, value)
    return redacted


def _replace_secret(text: str, value: Any) -> str:
    secret = str(value or "").strip()
    if not secret:
        return text
    return text.replace(secret, "***").replace(urllib.parse.quote(secret, safe=""), "***")


def _is_secret_key(key: str) -> bool:
    lowered = str(key or "").lower()
    return any(marker in lowered for marker in SECRET_KEY_MARKERS)


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _qr_svg(payload: str) -> tuple[str | None, str]:
    if not payload:
        return None, "qr_payload_missing"
    try:
        import segno  # type: ignore[import-not-found]
    except Exception:
        return None, "qr_generator_unavailable"
    try:
        buffer = io.BytesIO()
        segno.make(payload).save(buffer, kind="svg", scale=4)
        svg = buffer.getvalue().decode("utf-8")
    except Exception:
        return None, "qr_generation_failed"
    if "<svg" not in svg:
        return None, "qr_generation_failed"
    return svg, ""


def _require_http_url(url: str, *, label: str) -> None:
    parsed = urllib.parse.urlparse(str(url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise OnboardingError(f"{label} must be an http or https URL")


def _url_host(url: str) -> str:
    parsed = urllib.parse.urlparse(str(url or ""))
    return parsed.netloc


def _strip_trailing_slash(value: str) -> str:
    return str(value or "").rstrip("/")


def _compact_token(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", str(value or ""))


def _safe_topic(value: str) -> str:
    topic = re.sub(r"[^A-Za-z0-9_-]", "-", str(value or "").strip())
    if not topic:
        raise OnboardingError("topic is required")
    return topic[:64]


def _entries(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.replace("\n", ",").split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(part).strip() for part in value if str(part).strip()]
    return [str(value).strip()]
