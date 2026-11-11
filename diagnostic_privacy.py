from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from typing import Any


_PROCESS_HMAC_KEY = secrets.token_bytes(32)
_URI_TOKEN_RE = re.compile(
    r"(?i)(?:[a-z][a-z0-9+.-]*://|(?:ss|vmess|vless|trojan):)"
    r"[^\s\"'<>]+"
)
_UUID_TOKEN_RE = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
)
_ENDPOINT_KEYS = frozenset(
    {
        "endpoint",
        "promoted_endpoint",
        "temporary_promoted_endpoint",
        "source_url",
    }
)
# Report schema keys (lower-case ASCII words) repeat in every report, so their
# classification is memoised.  Only such keys are stored: they can never hold
# URI/UUID material, and keys of *by_identity* maps are never looked up here.
# Dynamic keys (hosts, "host:port", root families, paths, unicode) are always
# classified afresh and never retained.  The memo is cleared when full, so a
# burst of dynamic schema-shaped keys (e.g. numeric map keys) cannot disable it.
_SCHEMA_KEY_RE = re.compile(r"[a-z0-9_]+")
_KEY_CLASS_CACHE: dict[str, tuple[str, bool, bool]] = {}
_KEY_CLASS_CACHE_LIMIT = 4096


def _may_hold_token(text: str) -> bool:
    """Return False only when neither token pattern can match ``text``.

    Every ``_URI_TOKEN_RE`` alternative needs a literal ``:`` and
    ``_UUID_TOKEN_RE`` needs a literal ``-``; tests pin this to the patterns.
    """

    return ":" in text or "-" in text


def _classify_diagnostic_key(
    key: str,
    *,
    cacheable: bool,
) -> tuple[str, bool, bool]:
    """Return (normalised key, is identity key, contains URI/UUID material)."""

    exact_str = type(key) is str
    if cacheable and exact_str:
        cached = _KEY_CLASS_CACHE.get(key)
        if cached is not None:
            return cached
    child_key = key.strip().lower()
    is_identity_key = child_key == "identity" or (
        child_key.endswith("_identity")
        and not child_key.endswith("_identity_hash")
        and not child_key.endswith("by_identity")
    )
    key_contains_sensitive_material = (
        not is_identity_key
        and (not exact_str or _may_hold_token(key))
        and bool(_URI_TOKEN_RE.search(key) or _UUID_TOKEN_RE.search(key))
    )
    classification = (
        child_key,
        is_identity_key,
        key_contains_sensitive_material,
    )
    if (
        cacheable
        and exact_str
        and _SCHEMA_KEY_RE.fullmatch(key) is not None
    ):
        if len(_KEY_CLASS_CACHE) >= _KEY_CLASS_CACHE_LIMIT:
            _KEY_CLASS_CACHE.clear()
        _KEY_CLASS_CACHE[key] = classification
    return classification


def runtime_scoped_hmac(
    value: Any,
    *,
    namespace: str,
    length: int = 24,
) -> str:
    """Return a process-local opaque identifier without persisting the key."""

    namespace_bytes = str(namespace or "diagnostic").encode("utf-8")
    value_bytes = str(value or "").encode("utf-8")
    digest = hmac.new(
        _PROCESS_HMAC_KEY,
        namespace_bytes + b"\x00" + value_bytes,
        hashlib.sha256,
    ).hexdigest()
    return digest[: max(12, min(64, int(length)))]


def runtime_identity_hash(value: Any) -> str:
    return f"hmac:{runtime_scoped_hmac(value, namespace='node-identity')}"


def _sanitized_identity(value: Any) -> str:
    text = str(value or "")
    return text if text.startswith("hmac:") else runtime_identity_hash(text)


def _redacted_token(match: re.Match[str], *, namespace: str) -> str:
    return (
        "[redacted:"
        f"{runtime_scoped_hmac(match.group(0), namespace=namespace, length=16)}"
        "]"
    )


def redact_diagnostic_text(value: Any) -> str:
    """Remove reversible URI and UUID material from diagnostic text."""

    text = str(value or "")
    if type(text) is str and not _may_hold_token(text):
        return text
    text = _URI_TOKEN_RE.sub(
        lambda match: _redacted_token(match, namespace="diagnostic-uri"),
        text,
    )
    return _UUID_TOKEN_RE.sub(
        lambda match: _redacted_token(match, namespace="diagnostic-uuid"),
        text,
    )


def sanitize_diagnostic_payload(
    value: Any,
    *,
    namespace: str = "diagnostic",
    key_hint: str = "",
) -> Any:
    """Recursively sanitize a diagnostic payload without changing control data."""

    value_type = type(value)
    if (
        value_type is int
        or value_type is float
        or value_type is bool
        or value is None
    ):
        # Exact scalars carry no text; the generic fall-through returns them
        # unchanged too.
        return value
    normalized_key = str(key_hint or "").strip().lower()
    if isinstance(value, dict):
        identity_keyed = "by_identity" in normalized_key
        sanitized: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            (
                child_key,
                is_identity_key,
                key_contains_sensitive_material,
            ) = _classify_diagnostic_key(key, cacheable=not identity_keyed)
            if is_identity_key:
                output_identity_key = (
                    "identity_hash"
                    if child_key == "identity"
                    else f"{child_key}_hash"
                )
                sanitized[output_identity_key] = _sanitized_identity(raw_value)
                continue
            output_key = key
            if identity_keyed:
                output_key = _sanitized_identity(key)
            elif key_contains_sensitive_material:
                output_key = "hmac:" + runtime_scoped_hmac(
                    key,
                    namespace=namespace + ":key",
                )
            sanitized[output_key] = sanitize_diagnostic_payload(
                raw_value,
                namespace=f"{namespace}:{child_key or 'value'}",
                key_hint=child_key,
            )
        return sanitized
    if isinstance(value, list):
        if normalized_key.endswith("_identities"):
            return [_sanitized_identity(item) for item in value]
        return [
            sanitize_diagnostic_payload(
                item,
                namespace=namespace,
                key_hint=normalized_key,
            )
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            sanitize_diagnostic_payload(
                item,
                namespace=namespace,
                key_hint=normalized_key,
            )
            for item in value
        )
    if isinstance(value, str):
        if value.startswith("hmac:") or value.startswith("[redacted:"):
            return value
        if normalized_key in _ENDPOINT_KEYS:
            return f"hmac:{runtime_scoped_hmac(value, namespace=namespace)}"
        return redact_diagnostic_text(value)
    return value
