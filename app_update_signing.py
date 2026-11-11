from __future__ import annotations

import base64
import binascii
import hashlib
import json
from pathlib import Path
from collections.abc import Iterable
from typing import Any


SIGNATURE_SCHEMA_VERSION = 1
SIGNATURE_ALGORITHM = "Ed25519"
SIGNED_MANIFEST_NAME = "SHA256SUMS.txt"
SIGNATURE_DOCUMENT_FIELDS = frozenset(
    {"schema_version", "algorithm", "key_id", "signed_file", "signature"}
)
MAX_SIGNATURE_DOCUMENT_BYTES = 4096


class ReleaseSignatureError(ValueError):
    """Raised when a release-signing input is missing, malformed, or invalid."""


def _ed25519_types() -> tuple[Any, Any]:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )
    except ImportError as exc:  # pragma: no cover - exercised by packaged smoke
        raise ReleaseSignatureError(
            "The cryptography package is unavailable; release signatures cannot be verified."
        ) from exc
    return Ed25519PrivateKey, Ed25519PublicKey


def _decode_canonical_base64(
    value: str,
    *,
    expected_size: int,
    label: str,
    allow_outer_whitespace: bool = False,
) -> bytes:
    text = value.strip() if allow_outer_whitespace else value
    if not text or any(character.isspace() for character in text):
        raise ReleaseSignatureError(f"{label} is not canonical Base64.")
    try:
        decoded = base64.b64decode(text, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ReleaseSignatureError(f"{label} is not valid Base64.") from exc
    if len(decoded) != expected_size:
        raise ReleaseSignatureError(f"{label} must decode to exactly {expected_size} bytes.")
    if base64.b64encode(decoded).decode("ascii") != text:
        raise ReleaseSignatureError(f"{label} is not canonical Base64.")
    return decoded


def decode_public_key(public_key_base64: str | bytes) -> bytes:
    if isinstance(public_key_base64, bytes):
        try:
            text = public_key_base64.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ReleaseSignatureError("The release public key is not ASCII Base64.") from exc
    elif isinstance(public_key_base64, str):
        text = public_key_base64
    else:
        raise ReleaseSignatureError("The release public key is missing.")
    return _decode_canonical_base64(
        text,
        expected_size=32,
        label="The release public key",
        allow_outer_whitespace=True,
    )


def public_key_id(public_key_bytes: bytes) -> str:
    if len(public_key_bytes) != 32:
        raise ReleaseSignatureError("An Ed25519 public key must be exactly 32 bytes.")
    return f"sha256:{hashlib.sha256(public_key_bytes).hexdigest()}"


def public_key_base64_from_private_key(private_key: Any) -> str:
    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError as exc:  # pragma: no cover - exercised by packaged smoke
        raise ReleaseSignatureError("The cryptography package is unavailable.") from exc
    raw_public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw_public_key).decode("ascii")


def load_encrypted_private_key(private_key_path: Path, password: bytes) -> Any:
    if not password:
        raise ReleaseSignatureError("A non-empty private-key password is required.")
    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError as exc:  # pragma: no cover - exercised by release tooling
        raise ReleaseSignatureError("The cryptography package is unavailable.") from exc
    try:
        key_data = private_key_path.read_bytes()
        private_key = serialization.load_pem_private_key(key_data, password=password)
    except (OSError, TypeError, ValueError) as exc:
        raise ReleaseSignatureError("The encrypted Ed25519 private key could not be loaded.") from exc
    Ed25519PrivateKey, _ = _ed25519_types()
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ReleaseSignatureError("The release private key is not an Ed25519 key.")
    return private_key


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseSignatureError(f"The signature document repeats field {key!r}.")
        result[key] = value
    return result


def parse_signature_document(signature_document: bytes) -> dict[str, Any]:
    if not signature_document or len(signature_document) > MAX_SIGNATURE_DOCUMENT_BYTES:
        raise ReleaseSignatureError("The release signature document has an invalid size.")
    try:
        text = signature_document.decode("utf-8")
        payload = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
    except UnicodeDecodeError as exc:
        raise ReleaseSignatureError("The release signature document is not UTF-8.") from exc
    except json.JSONDecodeError as exc:
        raise ReleaseSignatureError("The release signature document is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise ReleaseSignatureError("The release signature document must be a JSON object.")
    if frozenset(payload) != SIGNATURE_DOCUMENT_FIELDS:
        raise ReleaseSignatureError("The release signature document has unexpected or missing fields.")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != SIGNATURE_SCHEMA_VERSION:
        raise ReleaseSignatureError("The release signature schema version is unsupported.")
    if payload["algorithm"] != SIGNATURE_ALGORITHM:
        raise ReleaseSignatureError("The release signature algorithm is unsupported.")
    if payload["signed_file"] != SIGNED_MANIFEST_NAME:
        raise ReleaseSignatureError("The release signature names an unexpected manifest.")
    if not isinstance(payload["key_id"], str) or not isinstance(payload["signature"], str):
        raise ReleaseSignatureError("The release signature fields have invalid types.")
    _decode_canonical_base64(payload["signature"], expected_size=64, label="The release signature")
    return payload


def create_signature_document(manifest_bytes: bytes, private_key: Any) -> bytes:
    Ed25519PrivateKey, _ = _ed25519_types()
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ReleaseSignatureError("The release private key is not an Ed25519 key.")
    public_key_base64 = public_key_base64_from_private_key(private_key)
    raw_public_key = decode_public_key(public_key_base64)
    signature = private_key.sign(manifest_bytes)
    payload = {
        "schema_version": SIGNATURE_SCHEMA_VERSION,
        "algorithm": SIGNATURE_ALGORITHM,
        "key_id": public_key_id(raw_public_key),
        "signed_file": SIGNED_MANIFEST_NAME,
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    return (json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")


def verify_signature_document(
    manifest_bytes: bytes,
    signature_document: bytes,
    public_key_base64: str | bytes,
) -> str:
    payload = parse_signature_document(signature_document)
    raw_public_key = decode_public_key(public_key_base64)
    expected_key_id = public_key_id(raw_public_key)
    if payload["key_id"] != expected_key_id:
        raise ReleaseSignatureError("The release signature key ID does not match the trusted public key.")
    signature = _decode_canonical_base64(
        payload["signature"], expected_size=64, label="The release signature"
    )
    _, Ed25519PublicKey = _ed25519_types()
    try:
        Ed25519PublicKey.from_public_bytes(raw_public_key).verify(signature, manifest_bytes)
    except ValueError as exc:
        raise ReleaseSignatureError("The trusted Ed25519 public key is invalid.") from exc
    except Exception as exc:
        try:
            from cryptography.exceptions import InvalidSignature
        except ImportError:  # pragma: no cover
            InvalidSignature = ()  # type: ignore[assignment,misc]
        if InvalidSignature and isinstance(exc, InvalidSignature):
            raise ReleaseSignatureError("SHA256SUMS.txt did not pass Ed25519 verification.") from exc
        raise
    return expected_key_id


def verify_signature_document_with_keyring(
    manifest_bytes: bytes,
    signature_document: bytes,
    public_keys_base64: Iterable[str | bytes],
) -> str:
    """Verify against the exact key ID named by a strict signature document.

    A key ring exists solely for deliberate old/new-key transition releases.
    Duplicate keys and an unknown key ID fail closed instead of trying every
    key and obscuring which project identity signed the manifest.
    """

    payload = parse_signature_document(signature_document)
    keys_by_id: dict[str, str | bytes] = {}
    for public_key in public_keys_base64:
        raw_public_key = decode_public_key(public_key)
        key_id = public_key_id(raw_public_key)
        if key_id in keys_by_id:
            raise ReleaseSignatureError("The trusted release key ring contains a duplicate key.")
        keys_by_id[key_id] = public_key
    if not keys_by_id:
        raise ReleaseSignatureError("No trusted release-signing public key is configured.")
    document_key_id = str(payload["key_id"])
    trusted_key = keys_by_id.get(document_key_id)
    if trusted_key is None:
        raise ReleaseSignatureError("The release signature key ID is not in the trusted key ring.")
    return verify_signature_document(manifest_bytes, signature_document, trusted_key)


def write_new_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(content)
    except FileExistsError as exc:
        raise ReleaseSignatureError(f"Refusing to overwrite existing file: {path}") from exc
    except OSError as exc:
        path.unlink(missing_ok=True)
        raise ReleaseSignatureError(f"Could not write file: {path}") from exc
