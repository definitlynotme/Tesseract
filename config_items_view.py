from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from config_item_identity import imported_node_uri, source_entry_value
from config_utils import config_entry_enabled, subscription_entry_url
from ui_format import fmt


def _identity(text: str) -> str:
    return text


@dataclass(frozen=True)
class ConfigItemsTableState:
    tier_filter: Any
    items: list[dict[str, Any]]
    title: str
    signature: tuple[tuple[str, str, str, str, str, str, str, str], ...]


def filtered_config_items(items: list[Any], tier_filter: Any) -> list[dict[str, Any]]:
    return [
        item
        for item in items
        if isinstance(item, dict)
        and not redundant_subscription_config_item(item)
        and (tier_filter == "all" or str(item.get("tier")) == tier_filter)
    ]


def redundant_subscription_config_item(item: dict[str, Any]) -> bool:
    return item.get("kind") == "subscription" and str(item.get("tier") or "") in {"tier2", "tier3"}


def config_items_signature(
    tier_filter: Any,
    items: list[dict[str, Any]],
) -> tuple[tuple[str, str, str, str, str, str, str, str], ...]:
    return tuple(
        (
            tier_filter,
            fmt(item.get("tier")),
            fmt(item.get("kind")),
            fmt(item.get("section")),
            fmt(item.get("key")),
            fmt(item.get("label")),
            fmt(item.get("detail")),
            fmt(item.get("enabled")),
        )
        for item in items
    )


def config_kind_label(
    item: dict[str, Any],
    *,
    translate: Callable[[str], str] = _identity,
) -> str:
    kind = str(item.get("kind", "") or "")
    if kind == "subscription":
        return translate("订阅")
    if kind == "source":
        section = str(item.get("section", "") or "")
        return translate(section)
    return translate("节点")


def config_item_table_values(
    item: dict[str, Any],
    *,
    tier_label: Callable[[Any], str],
    translate: Callable[[str], str] = _identity,
) -> list[str]:
    return [
        tier_label(item.get("tier")),
        config_kind_label(item, translate=translate),
        fmt(item.get("label")),
        fmt(item.get("detail")),
    ]


def config_items_table_state(
    items: list[Any],
    tier_filter: Any,
    *,
    tier_label: Callable[[Any], str],
    translate: Callable[[str], str] = _identity,
) -> ConfigItemsTableState:
    filtered_items = filtered_config_items(items, tier_filter)
    title = f"{tier_label(tier_filter)} 已配置项目" if tier_filter != "all" else translate("已配置项目")
    return ConfigItemsTableState(
        tier_filter=tier_filter,
        items=filtered_items,
        title=title,
        signature=config_items_signature(tier_filter, filtered_items),
    )


def configured_config_items(local_config: dict[str, Any], sources: dict[str, Any]) -> list[dict[str, Any]]:
    tiers = local_config.get("tiers", {}) if isinstance(local_config.get("tiers"), dict) else {}
    items: list[dict[str, Any]] = []

    tier1_config = tiers.get("tier1", {}) if isinstance(tiers.get("tier1"), dict) else {}
    items.extend(tier1_configured_items(tier1_config))

    for tier_name in ["tier2", "tier3", "exit"]:
        tier_config = tiers.get(tier_name, {}) if isinstance(tiers.get(tier_name), dict) else {}
        items.extend(imported_tier_configured_items(tier_name, tier_config))

    items.extend(tier3_source_configured_items(sources))
    return items


def tier1_configured_items(tier1_config: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for node in tier1_config.get("nodes", []) or []:
        if not isinstance(node, dict):
            continue
        name = str(node.get("name", "")).strip()
        if not name:
            continue
        address = str(node.get("address", "")).strip()
        port = node.get("port", "")
        protocols = node.get("protocols", {}) if isinstance(node.get("protocols"), dict) else {}
        items.append(
            {
                "tier": "tier1",
                "kind": "node",
                "key": name,
                "label": name,
                "detail": f"{address}:{port}" if address and port else address,
                "name": name,
                "address": address,
                "port": port,
                "protocol": tier1_protocol_label(node),
                "vless_link": str(node.get("vless_link", "") or ""),
                "hysteria2_link": str(node.get("hysteria2_link", "") or ""),
                "naive_link": str(node.get("naive_link", "") or ""),
                "protocols": protocols,
                "fallback_order": list(node.get("fallback_order", []) or []),
                "enabled": config_entry_enabled(node),
                "editable": True,
            }
        )
    return items


def imported_tier_configured_items(tier_name: str, tier_config: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for node in tier_config.get("nodes", []) or []:
        item = imported_tier_node_configured_item(tier_name, node)
        if item is not None:
            items.append(item)
    for url in tier_config.get("subscription_urls", []) or []:
        item = imported_tier_subscription_configured_item(tier_name, url)
        if item is not None:
            items.append(item)
    return items


def imported_tier_node_configured_item(tier_name: str, node: dict[str, Any] | str) -> dict[str, Any] | None:
    uri = imported_node_uri(node)
    if not uri:
        return None
    label = imported_node_label(node, uri)
    uri_parts = uri_parts_from_value(uri)
    return {
        "tier": tier_name,
        "kind": "node",
        "key": uri,
        "label": label,
        "detail": display_uri(uri),
        "name": label,
        "uri": uri,
        "protocol": uri_parts["protocol"],
        "address": uri_parts["address"],
        "port": uri_parts["port"],
        "enabled": config_entry_enabled(node),
        "editable": True,
    }


def imported_tier_subscription_configured_item(tier_name: str, url: Any) -> dict[str, Any] | None:
    url_text = subscription_entry_url(url)
    if not url_text:
        return None
    configured_label = ""
    if isinstance(url, dict):
        configured_label = str(url.get("name") or url.get("label") or "").strip()
    return {
        "tier": tier_name,
        "kind": "subscription",
        "key": url_text,
        "label": configured_label or display_url_label(url_text),
        "detail": display_uri(url_text),
        "url": url_text,
        "uri": url_text,
        "enabled": config_entry_enabled(url),
        "editable": tier_name in {"tier2", "tier3", "exit"},
    }


def tier3_source_configured_items(sources: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for section in ["github_repos", "telegram_channels", "subscription_urls", "local_files"]:
        for entry in sources.get(section, []) or []:
            value = source_entry_value(entry)
            if not value:
                continue
            items.append(
                {
                    "tier": "tier3",
                    "kind": "source",
                    "section": section,
                    "key": value,
                    "label": section,
                    "detail": display_source(value),
                }
            )
    return items


def tier1_protocol_label(node: dict[str, Any]) -> str:
    protocols = node.get("protocols", {}) if isinstance(node.get("protocols"), dict) else {}
    keys = sorted(str(key) for key in protocols.keys())
    if keys:
        return ", ".join(keys)
    labels = []
    if node.get("vless_link"):
        labels.append("vless")
    if node.get("hysteria2_link"):
        labels.append("hysteria2")
    if node.get("naive_link"):
        labels.append("naive")
    return ", ".join(labels)


def imported_node_label(node: dict[str, Any] | str, uri: str) -> str:
    if isinstance(node, dict):
        name = str(node.get("name", "") or "").strip()
        if name:
            return name
    return display_url_label(uri)


def display_url_label(value: str) -> str:
    parsed = urlparse(str(value))
    netloc = display_netloc(parsed)
    if netloc:
        return netloc
    if parsed.fragment:
        return parsed.fragment
    return fingerprint(value)


def display_uri(value: str) -> str:
    parsed = urlparse(str(value))
    netloc = display_netloc(parsed)
    if parsed.scheme and netloc:
        return f"{parsed.scheme}://{netloc}"
    if parsed.scheme:
        return f"{parsed.scheme}://{fingerprint(value)}"
    return fingerprint(value)


def display_source(value: str) -> str:
    parsed = urlparse(str(value))
    netloc = display_netloc(parsed)
    if parsed.scheme and netloc:
        return f"{parsed.scheme}://{netloc}"
    return Path(value).name or fingerprint(value)


def display_netloc(parsed: Any) -> str:
    host = parsed.hostname or ""
    if not host:
        return ""
    try:
        port = parsed.port
    except ValueError:
        port = None
    return f"{host}:{port}" if port else host


def fingerprint(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:10]


def uri_parts_from_value(value: str) -> dict[str, Any]:
    parsed = urlparse(str(value))
    try:
        port = parsed.port
    except ValueError:
        port = None
    return {
        "protocol": parsed.scheme,
        "address": parsed.hostname or "",
        "port": port or "",
    }
