from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Any


MIHOMO_SHADOWSOCKS_VERSION = "1.19.27"
DEFAULT_SHADOWSOCKS_CIPHER = "aes-256-gcm"
SHADOWSOCKS_NONE_NOTICE_CODE = "ss_none_unencrypted"

# Mihomo v1.19.27 Shadowsocks methods. Keep the default first for the native
# non-editable combo box and the deliberately unencrypted method last.
SHADOWSOCKS_CIPHER_CHOICES = (
    DEFAULT_SHADOWSOCKS_CIPHER,
    "aes-128-ctr",
    "aes-192-ctr",
    "aes-256-ctr",
    "aes-128-cfb",
    "aes-192-cfb",
    "aes-256-cfb",
    "aes-128-gcm",
    "aes-192-gcm",
    "aes-128-ccm",
    "aes-192-ccm",
    "aes-256-ccm",
    "aes-128-gcm-siv",
    "aes-256-gcm-siv",
    "chacha20-ietf",
    "chacha20",
    "xchacha20",
    "chacha20-ietf-poly1305",
    "xchacha20-ietf-poly1305",
    "chacha8-ietf-poly1305",
    "xchacha8-ietf-poly1305",
    "2022-blake3-aes-128-gcm",
    "2022-blake3-aes-256-gcm",
    "2022-blake3-chacha20-poly1305",
    "lea-128-gcm",
    "lea-192-gcm",
    "lea-256-gcm",
    "rabbit128-poly1305",
    "aegis-128l",
    "aegis-256",
    "aez-384",
    "deoxys-ii-256-128",
    "rc4-md5",
    "none",
)
SUPPORTED_SHADOWSOCKS_CIPHERS = frozenset(SHADOWSOCKS_CIPHER_CHOICES)

_SHADOWSOCKS_2022_KEY_BYTES = {
    "2022-blake3-aes-128-gcm": 16,
    "2022-blake3-aes-256-gcm": 32,
    "2022-blake3-chacha20-poly1305": 32,
}


@dataclass(frozen=True)
class ShadowsocksCipherCapability:
    name: str
    credential_kind: str = "password"
    decoded_key_bytes: int | None = None
    password_required: bool = True
    notice_code: str = ""


@dataclass(frozen=True)
class ShadowsocksCredentialIssue:
    code: str
    params: dict[str, Any]


def _build_cipher_capability(name: str) -> ShadowsocksCipherCapability:
    if name == "none":
        return ShadowsocksCipherCapability(
            name=name,
            credential_kind="none",
            password_required=False,
            notice_code=SHADOWSOCKS_NONE_NOTICE_CODE,
        )
    decoded_key_bytes = _SHADOWSOCKS_2022_KEY_BYTES.get(name)
    if decoded_key_bytes is not None:
        return ShadowsocksCipherCapability(
            name=name,
            credential_kind="base64_key",
            decoded_key_bytes=decoded_key_bytes,
        )
    return ShadowsocksCipherCapability(name=name)


SHADOWSOCKS_CIPHER_CAPABILITIES = tuple(
    _build_cipher_capability(name) for name in SHADOWSOCKS_CIPHER_CHOICES
)
_SHADOWSOCKS_CAPABILITY_BY_NAME = {
    capability.name: capability for capability in SHADOWSOCKS_CIPHER_CAPABILITIES
}


def normalize_shadowsocks_cipher(cipher: object) -> str:
    return str(cipher or "").strip().lower()


def shadowsocks_cipher_capability(cipher: object) -> ShadowsocksCipherCapability | None:
    return _SHADOWSOCKS_CAPABILITY_BY_NAME.get(normalize_shadowsocks_cipher(cipher))


def validate_shadowsocks_credential(
    cipher: object,
    credential: object,
) -> ShadowsocksCredentialIssue | None:
    """Validate one SS credential without producing user-facing prose.

    Shadowsocks 2022 identity chains use colon-separated Base64 keys. Every
    component must decode to the method's key size, so validating only the
    combined string would reject valid EIH credentials and miss malformed
    individual keys.
    """

    normalized_cipher = normalize_shadowsocks_cipher(cipher)
    capability = shadowsocks_cipher_capability(normalized_cipher)
    if capability is None:
        return ShadowsocksCredentialIssue(
            "unsupported_cipher",
            {
                "cipher": normalized_cipher,
                "core": "Mihomo",
                "version": MIHOMO_SHADOWSOCKS_VERSION,
            },
        )

    if not capability.password_required:
        return None

    normalized_credential = str(credential or "").strip()
    if not normalized_credential:
        code = (
            "missing_ss_2022_key"
            if capability.credential_kind == "base64_key"
            else "missing_ss_password"
        )
        return ShadowsocksCredentialIssue(code, {"cipher": capability.name})

    if capability.credential_kind != "base64_key":
        return None

    expected_bytes = int(capability.decoded_key_bytes or 0)
    key_parts = normalized_credential.split(":")
    for index, key_part in enumerate(key_parts, start=1):
        params: dict[str, Any] = {
            "cipher": capability.name,
            "expected_bytes": expected_bytes,
            "key_index": index,
            "key_count": len(key_parts),
        }
        try:
            decoded = base64.b64decode(key_part, validate=True)
        except (binascii.Error, UnicodeError, ValueError):
            return ShadowsocksCredentialIssue("invalid_ss_2022_key_base64", params)
        if len(decoded) != expected_bytes:
            params["actual_bytes"] = len(decoded)
            return ShadowsocksCredentialIssue("invalid_ss_2022_key_length", params)
    return None
