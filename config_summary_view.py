from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urlparse

from config_item_identity import imported_node_uri
from config_utils import coerce_bool, config_entry_enabled, subscription_entry_url


def config_summary(local_config: dict[str, Any], sources: dict[str, Any]) -> dict[str, Any]:
    tiers = dict_section(local_config, "tiers")
    tier3 = tier3_summary(dict_section(tiers, "tier3"))
    tier3["tier3b_source_count"] = tier3b_source_count(sources)
    tier3["bootstrap_source_count"] = tier3["tier3b_source_count"]
    return {
        "tier1": tier1_summary(dict_section(tiers, "tier1")),
        "tier2": imported_tier_summary(dict_section(tiers, "tier2")),
        "tier3": tier3,
        "exit": exit_summary(dict_section(tiers, "exit")),
        "egress_proxy": egress_proxy_summary(dict_section(local_config, "egress_proxy")),
        "killswitch": killswitch_summary(dict_section(local_config, "killswitch")),
        "sources": sources_summary(sources),
    }


def config_summary_with_runtime_status(
    summary: dict[str, Any],
    runtime_status: dict[str, Any] | None,
) -> dict[str, Any]:
    if not runtime_status:
        return summary
    status_tiers = runtime_status.get("tiers", {}) if isinstance(runtime_status, dict) else {}
    for tier_name in ["tier1", "tier2", "tier3", "exit"]:
        if tier_name in summary and tier_name in status_tiers:
            summary[tier_name]["runtime"] = {
                "total": status_tiers[tier_name].get("total", 0),
                "healthy": status_tiers[tier_name].get("healthy", 0),
                "last_check_at": status_tiers[tier_name].get("last_check_at"),
            }
    return summary


def dict_section(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def node_enablement_counts(nodes: list[Any]) -> dict[str, int]:
    enabled_total = sum(1 for node in nodes if config_entry_enabled(node))
    return {
        "total": len(nodes),
        "enabled_total": enabled_total,
        "disabled_total": len(nodes) - enabled_total,
    }


def subscription_enablement_counts(entries: list[Any]) -> dict[str, int]:
    valid_entries = [entry for entry in entries if subscription_entry_url(entry)]
    enabled_total = sum(1 for entry in valid_entries if config_entry_enabled(entry))
    return {
        "subscription_count": len(valid_entries),
        "enabled_subscription_count": enabled_total,
        "disabled_subscription_count": len(valid_entries) - enabled_total,
    }


def tier1_summary(tier_config: dict[str, Any]) -> dict[str, Any]:
    nodes = tier_config.get("nodes", []) or []
    return {
        **node_enablement_counts(nodes),
        "nodes": [summarize_node(node) for node in nodes if isinstance(node, dict)],
    }


def imported_tier_summary(tier_config: dict[str, Any]) -> dict[str, Any]:
    nodes = tier_config.get("nodes", []) or []
    urls = tier_config.get("subscription_urls", []) or []
    return {
        **node_enablement_counts(nodes),
        "nodes": [summarize_imported_node(node) for node in nodes if isinstance(node, (dict, str))],
        **subscription_enablement_counts(urls),
        "subscriptions": [summarize_url(url) for url in urls],
    }


def tier3_summary(tier_config: dict[str, Any]) -> dict[str, Any]:
    nodes = tier_config.get("nodes", []) or []
    urls = tier_config.get("subscription_urls", []) or []
    return {
        "enabled": coerce_bool(tier_config.get("enabled", False), False),
        **node_enablement_counts(nodes),
        "nodes": [summarize_imported_node(node) for node in nodes if isinstance(node, (dict, str))],
        **subscription_enablement_counts(urls),
        "subscriptions": [summarize_url(url) for url in urls],
        "bootstrap_enabled": coerce_bool(tier_config.get("bootstrap_enabled", True), True),
    }


def exit_summary(tier_config: dict[str, Any]) -> dict[str, Any]:
    nodes = tier_config.get("nodes", []) or []
    urls = tier_config.get("subscription_urls", []) or []
    return {
        "configured": bool(nodes or urls),
        **node_enablement_counts(nodes),
        **subscription_enablement_counts(urls),
        "nodes": [summarize_node(node) for node in nodes if isinstance(node, dict)],
        "subscriptions": [summarize_url(url) for url in urls],
    }


def egress_proxy_summary(egress_proxy: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": coerce_bool(egress_proxy.get("enabled", False), False),
        "server": mask_host(str(egress_proxy.get("server", ""))),
        "port": egress_proxy.get("port", 0),
        "has_username": bool(egress_proxy.get("username")),
        "has_password": bool(egress_proxy.get("password")),
    }


def killswitch_summary(killswitch: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": coerce_bool(killswitch.get("enabled", False), False),
        "allow_lan": coerce_bool(killswitch.get("allow_lan", True), True),
        "additional_whitelist_count": len(killswitch.get("additional_whitelist", []) or []),
    }


def sources_summary(sources: dict[str, Any]) -> dict[str, Any]:
    return {
        "github_repos": len(sources.get("github_repos", []) or []),
        "telegram_channels": len(sources.get("telegram_channels", []) or []),
        "subscription_urls": len(sources.get("subscription_urls", []) or []),
        "local_files": len(sources.get("local_files", []) or []),
        "github_discovery_enabled": coerce_bool(sources.get("github_discovery", {}).get("enabled", False), False)
        if isinstance(sources.get("github_discovery"), dict)
        else False,
    }


def tier3b_source_count(sources: dict[str, Any]) -> int:
    if not isinstance(sources, dict):
        return 0
    return (
        len(sources.get("github_repos", []) or [])
        + len(sources.get("telegram_channels", []) or [])
        + len(sources.get("subscription_urls", []) or [])
        + len(sources.get("local_files", []) or [])
    )


def summarize_node(node: dict[str, Any]) -> dict[str, Any]:
    protocols = node.get("protocols", {}) if isinstance(node.get("protocols"), dict) else {}
    return {
        "name": node.get("name", ""),
        "address": node.get("address", ""),
        "port": node.get("port", 0),
        "enabled": config_entry_enabled(node),
        "measured_ping_ms": node.get("measured_ping_ms"),
        "has_server_login": bool(node.get("root_password")),
        "has_vless_link": bool(node.get("vless_link")),
        "has_hysteria2_link": bool(node.get("hysteria2_link")),
        "has_naive_link": bool(node.get("naive_link")),
        "protocols": sorted(protocols.keys()),
    }


def summarize_imported_node(node: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(node, str):
        return {"name": "", "has_uri": bool(node), "enabled": True}
    return {
        "name": node.get("name", ""),
        "has_uri": bool(imported_node_uri(node)),
        "enabled": config_entry_enabled(node),
    }


def summarize_url(entry: Any) -> dict[str, Any]:
    url = subscription_entry_url(entry)
    parsed = urlparse(url)
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return {
        "host": host,
        "scheme": parsed.scheme,
        "fingerprint": digest,
        "enabled": config_entry_enabled(entry),
    }


def mask_host(host: str) -> str:
    if not host:
        return ""
    parts = host.split(".")
    if len(parts) <= 2:
        return host[0:2] + "***" if len(host) > 2 else "***"
    return f"{parts[0][0]}***.{'.'.join(parts[-2:])}"
