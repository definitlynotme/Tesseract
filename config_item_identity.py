from __future__ import annotations

from typing import Any


def imported_node_uri(entry: Any) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        value = entry.get("uri") or entry.get("link") or entry.get("url")
        return str(value) if value else ""
    return ""


def source_entry_value(entry: Any) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        value = entry.get("value") or entry.get("url") or entry.get("path")
        return str(value) if value else ""
    return ""
