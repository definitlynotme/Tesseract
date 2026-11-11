from __future__ import annotations

import hashlib
from typing import Any


FINAL_EXIT_GROUP = "FINAL-EXIT"
DIRECT_PROXY = "DIRECT"
UNSAFE_DIRECT_PROXIES = frozenset({DIRECT_PROXY, "COMPATIBLE"})
FAIL_CLOSED_PROXIES = frozenset({"REJECT", "REJECT-DROP"})
BUILTIN_TERMINAL_PROXIES = UNSAFE_DIRECT_PROXIES | FAIL_CLOSED_PROXIES


def builtin_terminal_kind(proxy_name: Any) -> str:
    """Classify Mihomo built-in terminal leaves used by runtime routing."""
    normalized = str(proxy_name or "").strip().upper()
    if normalized in FAIL_CLOSED_PROXIES:
        return "fail_closed"
    if normalized in UNSAFE_DIRECT_PROXIES:
        return "unsafe_direct"
    return ""


def mihomo_proxies_entries(payload: Any) -> dict[str, Any]:
    """The one accessor for the Mihomo ``GET /proxies`` entries map.

    Accepts exactly the two shapes the runtime holds:

    * the controller response ``{"proxies": {name: entry}}`` (what
      ``_mihomo_proxies_bounded`` returns and publication hooks receive);
    * the stored publication ``_runtime_route_snapshot_payload`` =
      ``{"proxies": <that controller response>, "proxy_map": {...}}`` (what
      ``_runtime_post_dial_route_refs`` / ``_runtime_route_snapshot_state``
      return).

    Returns ``{name: entry}``; ``{}`` for anything else (a bare entries map
    without the ``"proxies"`` key is NOT accepted, so a wrong-shape caller
    reads nothing instead of mistaking the wrapper for the members).  An
    entry is told from a nested map by its string ``type``: a node literally
    named ``proxies`` in a controller response is not unwrapped.
    (10-30: the exhausted-hold admission passed the stored wrapper to a
    reader that unwrapped once, so every member looked dead.)
    """

    if not isinstance(payload, dict):
        return {}
    level = payload.get("proxies")
    if not isinstance(level, dict):
        return {}
    inner = level.get("proxies")
    if isinstance(inner, dict) and not isinstance(inner.get("type"), str):
        return inner
    return level


def _proxy_entries(payload: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        return {}
    proxies = payload.get("proxies")
    if isinstance(proxies, dict):
        return {str(key): value for key, value in proxies.items() if isinstance(value, dict)}
    return {str(key): value for key, value in payload.items() if isinstance(value, dict)}


def _group_kind(entry: dict[str, Any]) -> str:
    type_text = str(entry.get("type") or entry.get("typeName") or "").strip().lower()
    if "select" in type_text or "selector" in type_text:
        return "select"
    if "fallback" in type_text:
        return "fallback"
    if entry.get("all") is not None:
        return "fallback"
    return "proxy"


def _group_candidates(entry: dict[str, Any]) -> list[str]:
    value = entry.get("all")
    if value is None:
        value = entry.get("proxies")
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item or "").strip()]
    return []


def inspect_runtime_route(payload: Any, *, start: str = FINAL_EXIT_GROUP, max_depth: int = 16) -> dict[str, Any]:
    """Follow Mihomo's current group selections to the runtime leaf proxy."""
    entries = _proxy_entries(payload)
    current = str(start or FINAL_EXIT_GROUP)
    chain: list[dict[str, Any]] = []
    selected_names: list[str] = []
    visited: set[str] = set()
    facility_failure = ""

    for _depth in range(max(1, int(max_depth or 1))):
        if current in visited:
            facility_failure = f"route cycle detected at {current}"
            break
        visited.add(current)
        entry = entries.get(current)
        if entry is None:
            break
        candidates = _group_candidates(entry)
        kind = _group_kind(entry)
        if candidates:
            now = str(entry.get("now") or entry.get("selected") or "").strip()
            layer = {
                "name": current,
                "type": kind,
                "now": now,
                "candidates": candidates,
                "pinned": kind == "select" and bool(now),
            }
            chain.append(layer)
            selected_names.append(f"{current}={now or '?'}")
            if not now:
                facility_failure = f"group {current} has no selected proxy"
                break
            current = now
            continue
        break

    leaf_proxy = current if current and not facility_failure else ""
    selected_chain = [layer["name"] for layer in chain]
    if leaf_proxy:
        selected_chain.append(leaf_proxy)
    marker_source = "|".join(selected_names + ([leaf_proxy] if leaf_proxy else []))
    route_marker = hashlib.sha256(marker_source.encode("utf-8")).hexdigest()[:16] if marker_source else ""
    return {
        "selected_chain": selected_chain,
        "layers": chain,
        "route_marker": route_marker,
        "leaf_proxy": leaf_proxy,
        "facility_failure": facility_failure,
    }


def next_viable_candidate(
    route: dict[str, Any],
    *,
    failed_proxies: set[str],
    active_gateway: bool,
) -> dict[str, str] | None:
    """Pick a conservative select-group candidate from the current route."""
    failed = {str(item) for item in failed_proxies}
    for layer in reversed(list(route.get("layers") or [])):
        if layer.get("type") != "select":
            continue
        group_name = str(layer.get("name") or "")
        current = str(layer.get("now") or "")
        if active_gateway and group_name == FINAL_EXIT_GROUP:
            continue
        for candidate in list(layer.get("candidates") or []):
            candidate = str(candidate or "").strip()
            if (
                not candidate
                or candidate == current
                or candidate.upper() in BUILTIN_TERMINAL_PROXIES
            ):
                continue
            if candidate in failed:
                continue
            if active_gateway and group_name == FINAL_EXIT_GROUP and candidate != "EXIT-SELECT":
                continue
            return {"group": group_name, "proxy": candidate}
    return None
