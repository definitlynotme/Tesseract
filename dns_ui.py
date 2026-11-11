from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from typing import Any

from dns_resolver_policy import (
    DNS_MODE_AUTO,
    DNS_MODE_CUSTOM,
    DNS_MODE_ENCRYPTED_ONLY,
    DNS_MODE_SYSTEM,
    DNS_MODES,
    normalize_dns_mode,
)


DNS_UI_FIELDS = ("nameserver", "default_nameserver", "proxy_server_nameserver")
DNS_UI_FIELD_LABELS = {
    "nameserver": "普通解析器",
    "default_nameserver": "默认解析器",
    "proxy_server_nameserver": "代理域名解析器",
}
# ``client.dns.mode`` choices in display order.
DNS_MODE_ORDER = (DNS_MODE_AUTO, DNS_MODE_ENCRYPTED_ONLY, DNS_MODE_SYSTEM, DNS_MODE_CUSTOM)
DNS_MODE_LABELS = {
    DNS_MODE_AUTO: "自动（推荐）",
    DNS_MODE_ENCRYPTED_ONLY: "仅加密 DNS",
    DNS_MODE_SYSTEM: "系统解析器",
    DNS_MODE_CUSTOM: "自定义",
}
DNS_MODE_HELP_TEXT = (
    "自动：每次启动探测一组中立的加密解析器（IP 直连的 DoH/DoT）并择优；"
    "当前网络无法使用加密解析时才降级到本机网络的解析器，并在界面标示。"
    "仅加密 DNS：不可用时也不降级。系统解析器：始终使用本机网络提供的解析器。"
    "自定义：使用下方列表。"
)


def dns_entries_from_value(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_entries = value.replace(",", "\n").splitlines()
    elif isinstance(value, (list, tuple)):
        raw_entries = value
    else:
        raw_entries = []
    entries: list[str] = []
    for entry in raw_entries:
        text = str(entry or "").strip()
        if text:
            entries.append(text)
    return entries


def dns_entries_for_ui(dns_config: dict[str, Any], key: str) -> list[str]:
    """Configured entries only; nothing is invented for an empty field."""

    hyphen_key = key.replace("_", "-")
    return dns_entries_from_value(dns_config.get(key) or dns_config.get(hyphen_key))


def dns_mode_for_ui(dns_config: Any) -> str:
    mode = normalize_dns_mode(dns_config if isinstance(dns_config, dict) else None)
    return mode if mode in DNS_MODES else DNS_MODE_AUTO


def dns_rows_for_ui(dns_config: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for field in DNS_UI_FIELDS:
        for entry in dns_entries_for_ui(dns_config, field):
            rows.append({"type": field, "value": entry})
    return rows


def dns_resolvers_from_rows(rows: Iterable[Any]) -> dict[str, list[str]]:
    resolvers = {field: [] for field in DNS_UI_FIELDS}
    for data in rows:
        resolver_type = str(data.get("type") if isinstance(data, dict) else "").strip()
        value = str(data.get("value") if isinstance(data, dict) else "").strip()
        if resolver_type in resolvers and value and value not in resolvers[resolver_type]:
            resolvers[resolver_type].append(value)
    return resolvers


def validate_plain_dns_ips(entries: list[str]) -> bool:
    """Mihomo's ``default-nameserver`` accepts IP literals or ``system`` only."""

    try:
        for entry in entries:
            if entry == "system":
                continue
            ipaddress.ip_address(entry)
    except ValueError:
        return False
    return bool(entries)
