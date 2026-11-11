from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from app_updater import DownloadedUpdate, compare_versions, normalize_version


UPDATE_STATE_FILENAME = "app-update-state.json"
UPDATE_STATE_SCHEMA_VERSION = 1
MAX_RELEASE_NOTES_CHARS = 65_536
MAX_SHOWN_VERSIONS = 32
MAX_NOTICED_VERSIONS = 16


@dataclass(frozen=True)
class CompletedUpdateNotice:
    previous_version: str
    installed_version: str
    release_url: str
    release_notes: str
    installer_sha256: str
    manifest_key_id: str
    manifest_verified: bool
    requested_at: str


def update_state_path(data_dir: Path) -> Path:
    return Path(data_dir) / UPDATE_STATE_FILENAME


def _bounded_text(value: object, *, limit: int = MAX_RELEASE_NOTES_CHARS) -> str:
    return str(value or "")[:limit]


def _safe_release_url(value: object) -> str:
    url = _bounded_text(value, limit=2048).strip()
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    return url


def _read_state(path: Path, *, strict: bool = False) -> dict[str, Any]:
    """Qt reads stay tolerant; strict revocation reads also fail on I/O errors.

    Only an I/O error can hide a receipt that a later read would resolve.
    Corrupt or foreign-schema content resolves to no receipt for every reader,
    so it must not block downloads forever.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except OSError:
        if strict:
            raise
        return {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    if payload.get("schema_version") != UPDATE_STATE_SCHEMA_VERSION:
        return {}
    return payload


def _write_state(path: Path, payload: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    serialized = json.dumps(
        dict(payload),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _noticed_versions(state: Mapping[str, Any]) -> list[str]:
    values = state.get("noticed_versions", [])
    if not isinstance(values, list):
        return []
    versions: list[str] = []
    for value in values:
        version = normalize_version(value) if isinstance(value, str) else ""
        if version:
            if version in versions:
                versions.remove(version)
            versions.append(version)
    return versions[-MAX_NOTICED_VERSIONS:]


def notice_due(path: Path, version: str) -> bool:
    """New-release notices are separate from Qt's completed-install notices."""
    normalized = normalize_version(version)
    return bool(normalized and normalized not in _noticed_versions(_read_state(path)))


def record_notice_delivered(path: Path, version: str) -> None:
    """Call only after a validated successful shell delivery acknowledgement."""
    normalized = normalize_version(version)
    if not normalized:
        return
    state = _read_state(path)
    versions = _noticed_versions(state)
    if normalized in versions:
        versions.remove(normalized)
    versions.append(normalized)
    state.update(schema_version=UPDATE_STATE_SCHEMA_VERSION,
                 noticed_versions=versions[-MAX_NOTICED_VERSIONS:])
    _write_state(path, state)


def record_pending_update(
    path: Path, downloaded: DownloadedUpdate, *, receipt_ref: str | None = None,
) -> None:
    """Persist a receipt only after the signed manifest and installer passed verification."""

    manifest_verified = bool(getattr(downloaded, "manifest_verified", False))
    manifest_key_id = str(getattr(downloaded, "manifest_key_id", "") or "").strip()
    if not manifest_verified or not manifest_key_id:
        raise ValueError("refusing to record an update without a verified release manifest")

    update = downloaded.update
    target_version = normalize_version(update.latest_version)
    previous_version = normalize_version(update.current_version)
    installer_sha256 = str(downloaded.sha256 or "").strip().lower()
    if not target_version or not previous_version or len(installer_sha256) != 64:
        raise ValueError("verified update receipt is incomplete")

    state = _read_state(path)
    shown_versions = state.get("shown_versions")
    if not isinstance(shown_versions, list):
        shown_versions = []
    payload = {
        **state,
        "schema_version": UPDATE_STATE_SCHEMA_VERSION,
        "shown_versions": [
            normalize_version(str(version))
            for version in shown_versions[-MAX_SHOWN_VERSIONS:]
            if normalize_version(str(version))
        ],
        "pending": {
            "previous_version": previous_version,
            "target_version": target_version,
            "release_url": _safe_release_url(update.release_url),
            "release_notes": _bounded_text(update.release_notes),
            "installer_sha256": installer_sha256,
            "manifest_key_id": _bounded_text(manifest_key_id, limit=160),
            "manifest_verified": True,
            "requested_at": datetime.now(UTC).isoformat(),
        },
    }
    if receipt_ref is not None:
        payload["pending"].update(
            receipt_ref=receipt_ref, installer_name=Path(downloaded.installer_path).name,
        )
    _write_state(path, payload)


def discard_pending_update(path: Path) -> None:
    """Revoke a receipt before its installer can be overwritten or deleted."""
    state = _read_state(path, strict=True)
    if "pending" in state:
        state.pop("pending")
        _write_state(path, state)


def completed_update_notice(path: Path, current_version: str) -> CompletedUpdateNotice | None:
    """Return a once-only notice after the running app reaches the recorded target."""

    state = _read_state(path)
    pending = state.get("pending")
    if not isinstance(pending, dict):
        return None
    target_version = normalize_version(str(pending.get("target_version") or ""))
    running_version = normalize_version(current_version)
    shown_versions = {
        normalize_version(str(value))
        for value in state.get("shown_versions", [])
        if normalize_version(str(value))
    }
    if (
        not target_version
        or not running_version
        or target_version in shown_versions
        or compare_versions(running_version, target_version) < 0
        or pending.get("manifest_verified") is not True
    ):
        return None
    key_id = _bounded_text(pending.get("manifest_key_id"), limit=160).strip()
    digest = str(pending.get("installer_sha256") or "").strip().lower()
    if not key_id or len(digest) != 64:
        return None
    return CompletedUpdateNotice(
        previous_version=normalize_version(str(pending.get("previous_version") or "")),
        installed_version=target_version,
        release_url=_safe_release_url(pending.get("release_url")),
        release_notes=_bounded_text(pending.get("release_notes")),
        installer_sha256=digest,
        manifest_key_id=key_id,
        manifest_verified=True,
        requested_at=_bounded_text(pending.get("requested_at"), limit=80),
    )


def mark_update_notice_shown(path: Path, installed_version: str) -> None:
    state = _read_state(path)
    pending = state.get("pending")
    if not isinstance(pending, dict):
        return
    target_version = normalize_version(str(pending.get("target_version") or ""))
    installed = normalize_version(installed_version)
    if not target_version or compare_versions(installed, target_version) < 0:
        return
    shown = [
        normalize_version(str(value))
        for value in state.get("shown_versions", [])
        if normalize_version(str(value))
    ]
    if target_version not in shown:
        shown.append(target_version)
    state.pop("pending", None)
    _write_state(
        path,
        {
            **state,
            "schema_version": UPDATE_STATE_SCHEMA_VERSION,
            "shown_versions": shown[-MAX_SHOWN_VERSIONS:],
        },
    )
