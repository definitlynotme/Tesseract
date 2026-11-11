"""Producer-side privacy filtering. No protocol scanner is used as a filter."""

from __future__ import annotations

import base64
import binascii
import json
import re
from functools import lru_cache
from typing import Any, Iterable, Mapping
from urllib.parse import unquote

REDACTED = "[REDACTED]"
MAX_TEXT = 16_384
# A conservative, compiled preflight: punctuation capable of introducing an
# encoding/container/assignment/path, or any credential form we recognize.
# Colons in ordinary host:port traffic lines alone do not require decoding.
_POSSIBLE_PRIVATE = re.compile(r'''[/\\%={}"'~\ud800-\udfff]''')
_JSON_ARRAY = re.compile(r'^\s*\[\s*(?:[\[{"0-9-]|true\b|false\b|null\b|\])')
_PRIVATE_WORDS = ("password", "passwd", "pwd", "secret", "token", "key", "authorization",
                  "cookie", "session", "credential", "sendkey", "webhook", "subscription",
                  "url", "uri", "uuid", "suspicious", "monotonic", "deadline", "bearer", "basic", "digest", "auth", "sid", "pushover", "gotify", "smtp", "-----begin")
_CAPTURE_FIELDS = frozenset(("timestamp", "received_at", "source", "level", "type", "message",
    "raw_message", "payload", "event_group", "alert_type", "category", "metadata",
    "raw_line", "title", "detail", "ts_ms", "rx_ms", "reason_code", "params", "path"))
_SURROGATE = re.compile(r"[\ud800-\udfff]")
_LOG_DTO_FIELDS = frozenset(("ts_ms", "rx_ms", "source", "level", "category", "key",
                             "title", "detail", "raw"))
_SAFE_ENUM_TEXT = frozenset(("", "TESSERACT", "MIHOMO", "ALERT", "UNKNOWN", "debug", "info",
    "warn", "error", "critical", "runtime", "routing", "health", "circuit_breaker", "update",
    "configuration", "connection", "alert", "other"))


class KnownSecrets:
    """Owned, compiled exact substrings; build off-path and swap on refresh.

    Mapping keys are secret values and values are safe category labels. A plain
    iterable of strings uses category 'secret'. Longest alternatives win overlaps.
    This object deliberately has no secret-bearing repr or diagnostics.
    """
    def __init__(self, values: Mapping[str, str] | Iterable[str] = ()) -> None:
        pairs = values.items() if isinstance(values, Mapping) else ((v, "secret") for v in values)
        owned = {}
        for value, category in pairs:
            if not isinstance(value, str) or not isinstance(category, str):
                raise TypeError("invalid secret registry")
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", category):
                raise ValueError("invalid secret category")
            if value:
                owned[value] = "[redacted:" + category + "]"
        self._replacements = owned
        self._pattern = (re.compile("|".join(re.escape(v) for v in sorted(owned, key=lambda v: (-len(v), v))))
                         if owned else None)
        # Pure filtering result, scoped to this immutable registry generation.
        # Only bounded in-memory entries; refresh creates a new empty cache.
        self.filter_text = lru_cache(maxsize=256)(
            lambda text: redact_text(text, known_secrets=self))

    def replace(self, text: str) -> str:
        return self._pattern.sub(lambda m: self._replacements[m[0]], text) if self._pattern else text


_EMPTY_SECRETS = KnownSecrets()


class RedactionBudgetExceeded(ValueError):
    """Reject the complete record, never return a partially filtered value."""


def _check_work(value: Any) -> None:
    # Bound work before serialization or recursion, including discarded metadata.
    stack, nodes, chars = [(value, 0)], 0, 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > 2048 or depth > 32:
            raise RedactionBudgetExceeded("record work limit")
        if isinstance(item, str):
            chars += len(item)
            if len(item) > MAX_TEXT or chars > 65_536:
                raise RedactionBudgetExceeded("record text limit")
        elif isinstance(item, dict):
            if len(item) > 1024:
                raise RedactionBudgetExceeded("record work limit")
            for key, child in item.items():
                stack.extend(((key, depth + 1), (child, depth + 1)))
        elif isinstance(item, (list, tuple)):
            if len(item) > 2048:
                raise RedactionBudgetExceeded("record work limit")
            stack.extend((child, depth + 1) for child in item)


_ESCAPE = re.compile(r'\\(?:u[0-9a-fA-F]{4}|["\\/bfnrt])')
_UNICODE_ESCAPE = re.compile(r'\\u[0-9a-fA-F]{4}')
_SIMPLE = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f",
           "n": "\n", "r": "\r", "t": "\t"}
_SECRET_SUFFIX = {"password", "passwd", "pwd", "secret", "token", "apikey", "key",
                  "authorization", "cookie", "session", "credential", "credentials", "sendkey", "webhook", "authtoken", "sid"}
_SAFE_SUFFIX = {"count", "len", "length", "total", "size", "ms", "s", "id"}
_FORBIDDEN = {"subscription_url", "subscriptionurl", "url", "uri", "uuid",
              "suspicious_reason", "monotonic", "deadline", "sendkey", "webhook",
              "bottoken", "accesstoken", "apptoken", "userkey", "privatekey", "presharedkey",
              "smtppassword", "accountsid", "messagingservicesid", "apikey", "webhookurl"}
_ASSIGNMENT = re.compile(
    r'''(?P<key>"[^"\r\n]*"|'[^'\r\n]*'|(?<![a-zA-Z0-9])[a-zA-Z0-9][^=:"'\r\n]*?)'''
    r'''[ \t]*[:=][ \t]*(?P<value>"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;}&]+)?''')
_KEY_ONLY = re.compile(
    r'''(?P<key>"[^"\r\n]*"|'[^'\r\n]*'|(?<![a-zA-Z0-9])[a-zA-Z0-9][^=:"'\r\n]*?)[ \t]*[:=]''')
_URL = re.compile(r"(?<![a-zA-Z0-9])[a-zA-Z][a-zA-Z0-9+.-]{0,63}://[^\s\"'<>]+")
_AUTH = re.compile(
    r"(?<![a-zA-Z0-9])(?:basic|bearer)\s+[a-zA-Z0-9._~+/=-]+|"
    r"(?<![a-zA-Z0-9])digest\s+[^\r\n]+", re.I)
# The shortest JSON object containing alg is 9 bytes (12 base64url chars).
# Excluding dots at the left boundary also prevents IPv4 suffix preflight hits.
_JWT = re.compile(r"(?<![a-zA-Z0-9_.-])[a-zA-Z0-9_-]{12,}\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]*(?![a-zA-Z0-9_-])")
# Opaque provider credentials require context; these also run for JSON keys.
_CONTEXT = re.compile(
    r"\b(?:pushover|gotify|twilio|smtp)\b[ \t:._-]*"
    r"(?:app[ \t._-]*)?(?:auth[ \t._-]*token|token|user[ \t._-]*key|"
    r"account[ \t._-]*sid|sid|password|passwd|pwd)\b[ \t:=]+"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)", re.I)
_PEM = re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----.*?(?:-----END (?:[A-Z]+ )?PRIVATE KEY-----|$)", re.S | re.I)
_HOME = re.compile(
    r'''(?:[a-zA-Z]:(?:[\\/]|\\u005c|\\u002f|%5c|%2f)+Users(?:[\\/]|\\u005c|\\u002f|%5c|%2f)+|/(?:Users|home)/|\\{2,}[^\\\s]+\\+Users\\+)[^\r\n"'<>;,]+''', re.I)
_ENV_HOME = re.compile(r"(?:%USERPROFILE%|%APPDATA%|%LOCALAPPDATA%|~)[\\/][^\r\n\"'<>;,]+", re.I)
_CHANNEL = re.compile(
    r"(?<![a-zA-Z0-9])(?:xox[abcprse]-[a-zA-Z0-9-]+|gh[pousr]_[a-zA-Z0-9]+|"
    r"sk-[a-zA-Z0-9_-]{12,}|bot\d{6,}:[a-zA-Z0-9_-]+|\d{6,}:[a-zA-Z0-9_-]{20,}|"
    r"\d+:[a-zA-Z0-9_-]{35,}|tk_[a-zA-Z0-9_-]{6,}|SG\.[a-zA-Z0-9_-]{6,}\.[a-zA-Z0-9_-]{6,}|SEC[a-zA-Z0-9]{16,}|[a-zA-Z0-9_-]{24,}\.[a-zA-Z0-9_-]{6,}\.[a-zA-Z0-9_-]{20,})")


def clean_unicode(text: str) -> str:
    return _SURROGATE.sub("\ufffd", text)


def _redact_paths(text: str) -> str:
    return _ENV_HOME.sub("[USER_PATH]", _HOME.sub("[USER_PATH]", text))


# One pass over the original spelling: replacements are never decoded again.
_NORMALIZE = re.compile(r'%[0-9a-fA-F]{2}(?:%[0-9a-fA-F]{2})*|\\(?:u[0-9a-fA-F]{4}|["\\/bfnrt])')
_FEATURES = re.compile("|".join(
    "(?P<" + name + ">" + pattern.pattern + ")" for name, pattern in (
        ("pem", _PEM), ("home", _HOME), ("env_home", _ENV_HOME),
        ("url", _URL), ("auth", _AUTH), ("context", _CONTEXT),
        ("channel", _CHANNEL), ("jwt", _JWT))), re.I | re.S)
_PRIVATE_HINT = re.compile("|".join(re.escape(word) for word in _PRIVATE_WORDS))
_FEATURE_HINT = re.compile(_CHANNEL.pattern + "|" + _JWT.pattern)


def _decode(text: str, *, paths: bool = False) -> str:
    if paths:
        text = _redact_paths(text)  # Protect Windows separators from control escapes.
    def normalize(match):
        spelling = match[0]
        if spelling[0] == "%":
            return unquote(spelling, errors="replace")
        return chr(int(spelling[2:], 16)) if spelling[1] == "u" else _SIMPLE[spelling[1]]
    return clean_unicode(_NORMALIZE.sub(normalize, text))


def _feature_replacement(match: re.Match[str]) -> str:
    if match.lastgroup in {"home", "env_home"}:
        return "[USER_PATH]"
    if match.lastgroup == "jwt":
        header = match[0].split(".", 1)[0]
        try:
            decoded = json.loads(base64.b64decode(header + "=" * (-len(header) % 4),
                                                 altchars=b"-_", validate=True))
        except (ValueError, UnicodeError, binascii.Error, RecursionError):
            return match[0]
        if not isinstance(decoded, dict) or "alg" not in decoded:
            return match[0]
    return REDACTED


def sensitive_name(key: str) -> bool:
    if "\\" in key or "%" in key:
        key = _decode(key)
    key = key.lower().strip("\"'")
    terms = re.findall(r"[a-z0-9]+", key)
    return (key in _FORBIDDEN or terms in (["subscription", "url"], ["suspicious", "reason"]) or
            "private" in terms and "key" in terms or
            bool(terms and terms[-1] not in _SAFE_SUFFIX and terms[-1] in _SECRET_SUFFIX))


def _json_projection(value: Any, depth: int = 0, cache=None, known_secrets=_EMPTY_SECRETS) -> Any:
    if depth > 32:
        return REDACTED
    if isinstance(value, dict):
        return {clean_unicode(k): _json_projection(v, depth + 1, cache, known_secrets)
                for k, v in value.items()
                if (k in _CAPTURE_FIELDS or not sensitive_name(k)) and
                redact_text(k, _depth=depth + 1, known_secrets=known_secrets) == clean_unicode(k)}
    if isinstance(value, list):
        return [_json_projection(v, depth + 1, cache, known_secrets) for v in value]
    if isinstance(value, str):
        if cache is None:
            return redact_text(value, _depth=depth + 1, known_secrets=known_secrets)
        if value not in cache:
            cache[value] = redact_text(value, _depth=depth + 1, known_secrets=known_secrets)
        return cache[value]
    if type(value) in (int, float):
        spelling = json.dumps(value, allow_nan=False)
        replaced = known_secrets.replace(spelling)
        return replaced if replaced != spelling else value
    return value


def redact_text(text: str, *, known_secrets: KnownSecrets = _EMPTY_SECRETS, _depth: int = 0) -> str:
    """Remove sensitive names as well as their values (token=… is forbidden)."""
    if not isinstance(text, str):
        raise TypeError("log text must be a string")
    if len(text) > MAX_TEXT:
        raise ValueError("log text too large")
    if _depth > 32:
        return REDACTED
    replaced = known_secrets.replace(text)
    if replaced == text and text in _SAFE_ENUM_TEXT:
        return text
    # Cheap preflight has no per-secret loop; the registry is one compiled matcher.
    candidate_json = bool(_JSON_ARRAY.match(text) or text.lstrip().startswith("{") or '\":' in text)
    if not candidate_json and not _POSSIBLE_PRIVATE.search(text) and not _PRIVATE_HINT.search(text.lower()):
        if not _FEATURE_HINT.search(text):
            return replaced
        return _FEATURES.sub(_feature_replacement, replaced)
    text = clean_unicode(text)
    if candidate_json:
        try:
            parsed = json.loads(text)
        except (ValueError, RecursionError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            projected = json.dumps(_json_projection(parsed, _depth, known_secrets=known_secrets),
                                   ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            # Numeric spelling and nested/JSON-shaped key content must remain safe
            # after projection and serialization, not just before parsing.
            return known_secrets.replace(projected)
    text = _decode(replaced, paths=True)
    if text != replaced:
        text = known_secrets.replace(text)
    text = _FEATURES.sub(_feature_replacement, text)
    text = _ASSIGNMENT.sub(lambda m: REDACTED if sensitive_name(m["key"]) else m[0], text)
    # Scan keys without consuming values too, including assignments inside
    # logfmt quoted messages and incomplete fragments. Residual ambiguous lines
    # are removed in full rather than leaking a partially parsed secret.
    lines = []
    for line in text.split("\n"):
        if any(sensitive_name(m["key"]) for m in _KEY_ONLY.finditer(line)):
            line = REDACTED
        lines.append(line)
    text = "\n".join(lines)
    return clean_unicode(text)


def validate_record(record: dict[str, Any]) -> None:
    # Validate even discarded metadata so a broken record cannot be disguised by str().
    _check_work(record)
    json.dumps(record, ensure_ascii=True, allow_nan=False)


def redact_record(record: dict[str, Any], *, known_secrets: KnownSecrets = _EMPTY_SECRETS) -> dict[str, Any]:
    """Own only JSON-compatible input; bad objects are rejected per record."""
    if (record.keys() == _LOG_DTO_FIELDS and
            all(type(record[k]) is int for k in ("ts_ms", "rx_ms")) and
            all(isinstance(record[k], str) for k in _LOG_DTO_FIELDS - {"ts_ms", "rx_ms"})):
        # The collector's flat projection has no discarded/nested objects to
        # serialize twice. Filter each distinct text once in this call only.
        cache, result, chars = {}, {}, 0
        for key, value in record.items():
            if isinstance(value, str):
                chars += len(value)
                if chars > 65_536:
                    raise RedactionBudgetExceeded("record text limit")
                if value not in cache:
                    cache[value] = known_secrets.filter_text(value)
                result[key] = cache[value]
            else:
                result[key] = value
        return result
    validate_record(record)
    result = _json_projection(record, cache={}, known_secrets=known_secrets)
    # Classification keys have a precise metadata slot; they are never credentials.
    for name in ("key", "event_key"):
        if name in record:
            result[name] = redact_text(record[name], known_secrets=known_secrets)
    json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return result
