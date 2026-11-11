"""Per-network resolver memory: ordering hints only, never a verdict.

The file records which endpoints last qualified on a given network
fingerprint so the next qualification probes them first and can finish in
one round trip.  It never records failures and it never lets a stale entry
publish a rung by itself; policy always comes from fresh positive evidence.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from atomic_file import replace_with_retry

ORDERING_TTL_SECONDS = 6 * 3600
RETENTION_SECONDS = 30 * 86400
MAX_ENTRIES = 16
_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ResolverMemoryHint:
    fingerprint: str
    rung: str
    encrypted_urls: tuple[str, ...]
    link_resolvers: tuple[str, ...]
    recorded_at: float


class ResolverMemory:
    def __init__(
        self,
        path: str | Path,
        *,
        ordering_ttl_seconds: float = ORDERING_TTL_SECONDS,
        retention_seconds: float = RETENTION_SECONDS,
        max_entries: int = MAX_ENTRIES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.ordering_ttl_seconds = max(0.0, float(ordering_ttl_seconds))
        self.retention_seconds = max(0.0, float(retention_seconds))
        self.max_entries = max(1, int(max_entries))
        self._clock = clock

    # ----------------------------------------------------------- reading

    def load(self) -> dict[str, dict[str, Any]]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return {}
        if not isinstance(payload, dict) or payload.get("version") != _SCHEMA_VERSION:
            return {}
        entries = payload.get("networks")
        if not isinstance(entries, dict):
            return {}
        cleaned: dict[str, dict[str, Any]] = {}
        for fingerprint, entry in entries.items():
            if isinstance(fingerprint, str) and isinstance(entry, dict):
                cleaned[fingerprint] = dict(entry)
        return cleaned

    def hint(self, fingerprint: str) -> ResolverMemoryHint | None:
        key = str(fingerprint or "").strip()
        if not key or key == "unknown":
            return None
        entry = self.load().get(key)
        if not isinstance(entry, dict):
            return None
        try:
            recorded_at = float(entry.get("recorded_at") or 0.0)
        except (TypeError, ValueError):
            return None
        if recorded_at <= 0.0 or self._clock() - recorded_at > self.ordering_ttl_seconds:
            return None
        return ResolverMemoryHint(
            fingerprint=key,
            rung=str(entry.get("rung") or ""),
            encrypted_urls=tuple(_strings(entry.get("encrypted_urls"))),
            link_resolvers=tuple(_strings(entry.get("link_resolvers"))),
            recorded_at=recorded_at,
        )

    # ----------------------------------------------------------- writing

    def record(
        self,
        fingerprint: str,
        *,
        rung: str,
        encrypted_urls: Iterable[str] = (),
        link_resolvers: Iterable[str] = (),
    ) -> bool:
        """Persist positive evidence only; empty evidence is not recorded."""

        key = str(fingerprint or "").strip()
        urls = list(_strings(encrypted_urls))
        links = list(_strings(link_resolvers))
        if not key or key == "unknown" or not (urls or links):
            return False
        now = self._clock()
        entries = self.load()
        entries = {
            existing_key: entry
            for existing_key, entry in entries.items()
            if now - float(entry.get("recorded_at") or 0.0) <= self.retention_seconds
        }
        entries[key] = {
            "rung": str(rung or ""),
            "encrypted_urls": urls,
            "link_resolvers": links,
            "recorded_at": now,
        }
        if len(entries) > self.max_entries:
            ordered = sorted(
                entries.items(),
                key=lambda item: float(item[1].get("recorded_at") or 0.0),
                reverse=True,
            )
            entries = dict(ordered[: self.max_entries])
        payload = {"version": _SCHEMA_VERSION, "networks": entries}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            with temp_path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            replace_with_retry(temp_path, self.path)
        except OSError:
            return False
        return True


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item or "").strip()]
    return []


__all__ = ["ResolverMemory", "ResolverMemoryHint"]
