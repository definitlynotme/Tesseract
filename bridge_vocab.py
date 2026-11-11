"""Stable bridge vocabulary, with no translated text or runtime dependencies."""

from enum import Enum
from dataclasses import dataclass
from types import MappingProxyType
import re


class Tier(str, Enum):
    VANGUARD = "vanguard"
    SENTINEL = "sentinel"
    BASTION = "bastion"
    GATEWAY = "gateway"


class Phase(str, Enum):
    STOPPED = "stopped"
    COLLECTING = "collecting"
    CORE_STANDBY = "core_standby"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"


class Mode(str, Enum):
    SYSTEM = "system"
    TUN = "tun"
    MIXED = "mixed"


RUNTIME_TIERS = MappingProxyType({
    "tier1": "vanguard", "tier2": "sentinel", "tier3": "bastion", "exit": "gateway",
})
PROTOCOL_TIERS = MappingProxyType({v: k for k, v in RUNTIME_TIERS.items()})
RUNTIME_MODES = MappingProxyType({"system": "system_proxy", "tun": "tun", "mixed": "mixed"})
PROTOCOL_MODES = MappingProxyType({v: k for k, v in RUNTIME_MODES.items()})


@dataclass(frozen=True)
class SecretField:
    """One field record; historical read policies are explicit attributes."""
    category: str | None = None
    projection: bool = False
    channels: tuple[str, ...] = ()
    stripped: tuple[str, ...] = ()
    strip_trailing_slash: bool = False
    strip_surrounding_slashes: bool = False
    alert_category: str | None = None
    outside_alert_category: str | None = None
    node_category: str | None = None
    subscription: bool = False
    link: bool = False
    source_edit: bool = False
    source_identity_rank: int | None = None
    header_names: tuple[str, ...] = ()
    header_fragments: tuple[str, ...] = ()


_WEBHOOKS = ("generic_webhook", "slack", "discord", "teams", "google_chat")
# Order is retained for legacy channel projection. This is a type list, not
# another secret-field definition.
_CHANNELS = ("telegram", "whatsapp", *_WEBHOOKS, "ntfy", "pushover", "gotify",
             "email_sendgrid", "sms_twilio", "email_smtp", "desktop", "sound")
SECRET_RECORDS = MappingProxyType({
    "password": SecretField("password", True, ("email_smtp",)),
    "passwd": SecretField("password", True),
    "pwd": SecretField("password"),
    "secret": SecretField("secret", True),
    "token": SecretField("token", True, ("ntfy",), ("ntfy",)),
    "auth_token": SecretField("token", True, ("sms_twilio",), ("sms_twilio",)),
    "bot_token": SecretField("token", True, ("telegram",), ("telegram",)),
    "access_token": SecretField("token", True, ("whatsapp",), ("whatsapp",)),
    "app_token": SecretField("token", True, ("pushover", "gotify"), ("pushover", "gotify")),
    "api_key": SecretField("token", True, ("email_sendgrid",), ("email_sendgrid",)),
    "key": SecretField("secret", True),
    "user_key": SecretField("secret", True, ("pushover",), ("pushover",)),
    "send_key": SecretField("secret"),
    "private_key": SecretField("private_key", True),
    "preshared_key": SecretField("private_key", True),
    "uuid": SecretField("uuid", True),
    "authorization": SecretField("authorization", True),
    "cookie": SecretField("secret", True),
    "credentials": SecretField("secret"),
    "account_sid": SecretField("sid", True, ("sms_twilio",), ("sms_twilio",)),
    "messaging_service_sid": SecretField("sid", True, ("sms_twilio",), ("sms_twilio",)),
    "webhook_url": SecretField("webhook", True, _WEBHOOKS[1:], _WEBHOOKS[1:]),
    "url": SecretField(projection=True, channels=_WEBHOOKS, stripped=_WEBHOOKS,
        alert_category="webhook", node_category="node", link=True,
        source_edit=True, source_identity_rank=1),
    "uri": SecretField(projection=True, alert_category="webhook", node_category="node", link=True),
    "webhook": SecretField(alert_category="webhook"),
    "api_url": SecretField(projection=True, channels=("whatsapp", "email_sendgrid", "sms_twilio"),
        stripped=("whatsapp", "email_sendgrid"), alert_category="webhook"),
    "server": SecretField(channels=("ntfy", "gotify"), stripped=("ntfy", "gotify"),
        strip_trailing_slash=True, alert_category="webhook"),
    "topic": SecretField(channels=("ntfy",), stripped=("ntfy",), strip_surrounding_slashes=True),
    "username": SecretField(projection=True, channels=("email_smtp",), stripped=("email_smtp",),
        outside_alert_category="authorization"),
    "user": SecretField(outside_alert_category="authorization"),
    # Read all header values conservatively; only credential names are protected
    # on write. Both policies are properties of this same record.
    "headers": SecretField(channels=_WEBHOOKS, alert_category="authorization",
        outside_alert_category="authorization",
        header_names=("authorization", "proxy-authorization", "cookie", "x-api-key"),
        header_fragments=("token", "secret", "key")),
    "subscription_url": SecretField(subscription=True),
    "subscription_urls": SecretField(subscription=True),
    "subscriptions": SecretField(subscription=True),
    "link": SecretField(link=True),
    "vless_link": SecretField(link=True),
    "hysteria2_link": SecretField(link=True),
    "naive_link": SecretField(link=True),
    "value": SecretField(source_edit=True, source_identity_rank=0),
    "path": SecretField(source_identity_rank=2),
    "channel": SecretField(source_identity_rank=3),
})


def _secret_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


_SECRET_BY_NAME = MappingProxyType({_secret_name(name): record for name, record in SECRET_RECORDS.items()})
# Compatibility views are derived, never independently maintained lists.
SECRET_FIELDS = MappingProxyType({
    "records": SECRET_RECORDS,
    "alerts": MappingProxyType({kind: frozenset(
        "headers.*" if record.header_names else name
        for name, record in SECRET_RECORDS.items() if kind in record.channels)
        for kind in _CHANNELS}),
    "sources": frozenset(name for name, record in SECRET_RECORDS.items() if record.source_edit),
    "source_identity": tuple(sorted(
        (name for name, record in SECRET_RECORDS.items() if record.source_identity_rank is not None),
        key=lambda name: SECRET_RECORDS[name].source_identity_rank)),
    "projection_names": frozenset(_secret_name(name) for name, record in SECRET_RECORDS.items() if record.projection),
    "alert_stripped": MappingProxyType({kind: frozenset(
        _secret_name(name) for name, record in SECRET_RECORDS.items() if kind in record.stripped)
        for kind in _CHANNELS}),
})
ALERT_SECRETS = SECRET_FIELDS["alerts"]


def alert_secret_path(kind: str, field: str) -> bool:
    """headers.* matches only credential names, case-insensitively."""
    paths = ALERT_SECRETS.get(kind, frozenset())
    if field in paths and field != "headers.*":
        return True
    parent, dot, name = field.partition(".")
    if parent != "headers" or not dot or "headers.*" not in paths:
        return False
    name = name.casefold()
    record = SECRET_RECORDS[parent]
    return (name in record.header_names
            or any(part in name for part in record.header_fragments))


def secret_category(name: str, path: tuple[str, ...], inherited=None):
    """Derive read registration from records and the actual channel context."""
    record = _SECRET_BY_NAME.get(name)
    if record is None:
        return inherited
    category = record.category or inherited
    if record.subscription:
        category = "subscription"
    if "alerts" in path:
        category = record.alert_category or category
        if any(kind in path or _secret_name(kind) in path for kind in record.channels):
            category = category or "secret"
    else:
        category = record.outside_alert_category or category
        if "nodes" in path:
            category = record.node_category or category
    if record.link:
        category = category or "secret"
    return category


def secret_projection_path(kind: str | None, name: str) -> bool:
    """Legacy generic projection plus every declared channel secret."""
    record = _SECRET_BY_NAME.get(_secret_name(name))
    return bool(record and (record.projection or kind in record.channels))


def alert_secret_forms(value: str, path: tuple[str, ...]) -> tuple[str, ...]:
    if "alerts" not in path or "headers" in path:
        return ()
    record = _SECRET_BY_NAME.get(path[-1])
    if record and any(kind in path or _secret_name(kind) in path for kind in record.stripped):
        stripped = value.strip()
        if record.strip_surrounding_slashes:
            return (stripped, stripped.strip("/"))
        return (stripped, stripped.rstrip("/")) if record.strip_trailing_slash else (stripped,)
    return ()


def runtime_mode(mode: str) -> str:
    return RUNTIME_MODES[mode]


def protocol_mode(mode: str) -> str:
    return PROTOCOL_MODES[mode]


def error_message_key(code: str, *, domain: str = "bridge") -> str:
    """Callers supply registered machine codes, never exception prose."""
    if not re.fullmatch(r"[a-z][a-z0-9_]*", code):
        raise ValueError("invalid error code")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", domain):
        raise ValueError("invalid error domain")
    return f"errors.{domain}.{code}"
