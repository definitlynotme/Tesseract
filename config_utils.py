from __future__ import annotations

from typing import Any


TRUE_VALUES = {"1", "true", "yes", "y", "on", "enabled"}
FALSE_VALUES = {"0", "false", "no", "n", "off", "disabled"}
ROUTING_TARGET_RULE_TYPES = ("direct", "proxy", "reject")
# Schema tombstones are tolerated in existing files but are not migrated into
# the active layout, shown in the UI, or compiled into Mihomo rules.
LEGACY_ROUTING_RULE_BUCKET_KEYS = ("ai_fixed_ip",)
ROUTING_RULE_BUCKET_KEYS = (
    *ROUTING_TARGET_RULE_TYPES,
    "raw",
    "rule_sets",
    "compatibility_defaults",
)


def coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if not text:
        return bool(default)
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    return bool(default)


def routing_rule_mapping(routing_config: Any) -> dict[str, Any]:
    """Return one canonical rule mapping for nested and legacy flat layouts.

    Nested ``routing.rules`` is the current schema and wins if one file still
    contains both layouts. Callers that merge base/local config must
    canonicalize each layer before merging so the later file keeps priority.
    """

    if not isinstance(routing_config, dict):
        return {}
    nested = routing_config.get("rules")
    rules = dict(nested) if isinstance(nested, dict) else {}
    for key in ROUTING_RULE_BUCKET_KEYS:
        if key in routing_config and key not in rules:
            rules[key] = routing_config[key]
    return rules


def canonicalize_routing_rule_layout(payload: dict[str, Any]) -> bool:
    """Move legacy ``routing.<bucket>`` keys into ``routing.rules`` in place."""

    routing = payload.get("routing")
    if not isinstance(routing, dict):
        return False
    flat_keys = [key for key in ROUTING_RULE_BUCKET_KEYS if key in routing]
    if not flat_keys:
        return False
    routing["rules"] = routing_rule_mapping(routing)
    for key in flat_keys:
        del routing[key]
    return True


def normalize_routing_rule_order(value: Any) -> list[str]:
    """Return the match order of the direct / proxy / reject lists.

    ``routing.rule_order`` names the three target lists in the order their
    rules are compiled; each list keeps its own top-to-bottom order. Unknown
    or repeated names are ignored and any list left out keeps its place from
    the default order (direct, proxy, reject), so the result is always a full
    permutation and an absent key compiles exactly as before.
    """

    order: list[str] = []
    if isinstance(value, (list, tuple)):
        for entry in value:
            name = str(entry or "").strip().lower()
            if name in ROUTING_TARGET_RULE_TYPES and name not in order:
                order.append(name)
    order.extend(name for name in ROUTING_TARGET_RULE_TYPES if name not in order)
    return order


def normalize_routing_compatibility_defaults(value: Any) -> list[dict[str, str]]:
    """Normalize the ordered, editable compatibility routing stage.

    Compatibility defaults intentionally use rows instead of custom target
    buckets because their cross-target order is part of the legacy behavior.
    Invalid rows are ignored rather than being allowed to introduce an
    unrecognized Mihomo policy.
    """

    if not isinstance(value, (list, tuple)):
        return []
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entry in value:
        if not isinstance(entry, dict):
            continue
        rule_type = str(
            entry.get("rule_type")
            or entry.get("type")
            or entry.get("target")
            or ""
        ).strip().lower().replace("-", "_")
        rule_value = str(entry.get("value") or entry.get("rule") or "").strip()
        identity = (rule_type, rule_value)
        if (
            rule_type in LEGACY_ROUTING_RULE_BUCKET_KEYS
            or rule_type not in ROUTING_TARGET_RULE_TYPES
            or not rule_value
            or identity in seen
        ):
            continue
        seen.add(identity)
        normalized.append({"rule_type": rule_type, "value": rule_value})
    return normalized


def config_entry_enabled(entry: Any) -> bool:
    """Return whether a persisted config entry is active.

    Legacy entries, including bare URI strings, predate the ``enabled`` field
    and remain enabled by default.
    """
    if not isinstance(entry, dict):
        return True
    return coerce_bool(entry.get("enabled", True), True)


def subscription_entry_url(entry: Any) -> str:
    """Return the URL stored by a tier subscription entry.

    Tier subscriptions historically used bare strings.  Mapping entries are
    accepted so an ``enabled`` flag and provider metadata can be persisted
    without losing the URL.
    """
    if isinstance(entry, str):
        return entry.strip()
    if isinstance(entry, dict):
        value = entry.get("url") or entry.get("value") or entry.get("uri")
        return str(value or "").strip()
    return ""


def enabled_subscription_urls(value: Any) -> list[str]:
    """Normalize and deduplicate enabled tier subscription URLs.

    The string branch preserves the manager's tolerance for older hand-edited
    comma/newline separated configuration, while validated persisted config
    continues to use a list.
    """
    if isinstance(value, str):
        raw_items: list[Any] = value.replace(",", "\n").splitlines()
    elif isinstance(value, (list, tuple)):
        raw_items = list(value)
    else:
        raw_items = []
    urls: list[str] = []
    for entry in raw_items:
        if not config_entry_enabled(entry):
            continue
        url = subscription_entry_url(entry)
        if url and url not in urls:
            urls.append(url)
    return urls


def coerce_positive_int(value: Any, default: int, *, minimum: int = 1) -> int:
    raw_value = value
    if raw_value is None:
        raw_value = default
    if isinstance(raw_value, str):
        stripped = raw_value.strip()
        if not stripped or stripped.lower() in {"inherit", "none", "null"}:
            raw_value = default
        else:
            raw_value = stripped
    try:
        return max(minimum, int(raw_value))
    except (TypeError, ValueError):
        return max(minimum, int(default))


def coerce_float(value: Any, default: float, *, minimum: float | None = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = float(default)
    if minimum is not None:
        return max(float(minimum), result)
    return result


def coerce_port(value: Any, default: int) -> int:
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return int(default)
    return port if 1 <= port <= 65535 else int(default)


def ensure_mapping_section(payload: dict[str, Any], key: str) -> dict[str, Any]:
    section = payload.setdefault(key, {})
    if not isinstance(section, dict):
        section = {}
        payload[key] = section
    return section


def ensure_list_section(payload: dict[str, Any], key: str) -> list[Any]:
    section = payload.setdefault(key, [])
    if not isinstance(section, list):
        section = []
        payload[key] = section
    return section
