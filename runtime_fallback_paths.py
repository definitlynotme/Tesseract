"""Resolve recovered leaves inside an already loaded native fallback policy."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def preferred_fallback_path(
    entries: Mapping[str, Any],
    group_name: str,
    fixed_member: str,
    recovered_leaf: str,
) -> tuple[str, ...] | None:
    """Return the first earlier lawful branch, preserving nested user choices.

    Only the caller-owned outer fixed selection may be released. Selectors
    and fixed inner groups retain their current member; automatic inner
    fallback groups may discover any member in their existing saved order.
    """

    def kind(entry: Mapping[str, Any]) -> str:
        return str(entry.get("typeName") or entry.get("type") or "").casefold()

    outer = entries.get(group_name)
    if not isinstance(outer, Mapping) or "fallback" not in kind(outer):
        return None
    members = [str(item) for item in list(outer.get("all") or [])]
    if fixed_member not in members:
        return None

    def visit(name: str, path: tuple[str, ...]) -> tuple[str, ...] | None:
        if name in path or len(path) >= 16:
            return None
        path = (*path, name)
        if name == recovered_leaf:
            return path
        entry = entries.get(name)
        if not isinstance(entry, Mapping):
            return None
        type_name = kind(entry)
        children = [str(item) for item in list(entry.get("all") or [])]
        fixed = str(entry.get("fixed") or "")
        if "select" in type_name or fixed:
            chosen = fixed or str(entry.get("now") or "")
            children = [chosen] if chosen in children else []
        elif "fallback" not in type_name:
            return None
        for child in children:
            result = visit(child, path)
            if result is not None:
                return result
        return None

    for member in members[:members.index(fixed_member)]:
        result = visit(member, (group_name,))
        if result is not None:
            return result
    return None
