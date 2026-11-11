from __future__ import annotations

import hashlib
import re
from typing import Any

from models import Node
from tier1_protocols import expand_tier1_entry


TIER1_PROTOCOL_NAME_TOKENS = frozenset(
    {
        "anytls",
        "h2",
        "http",
        "https",
        "hysteria",
        "hysteria2",
        "hy2",
        "naive",
        "reality",
        "ss",
        "socks",
        "socks5",
        "tcp",
        "trojan",
        "tuic",
        "udp",
        "vless",
        "vmess",
        "wg",
        "wireguard",
        "xhttp",
    }
)


def _import_warning(
    code: str,
    message: str,
    *,
    params: dict[str, Any] | None = None,
    display: bool = True,
) -> dict[str, Any]:
    """Build a stable warning payload while retaining the legacy log text."""

    return {
        "code": str(code),
        "params": dict(params or {}),
        "message": str(message),
        "display": bool(display),
    }


def import_warnings_for_tier(tier: str, nodes: list[Node]) -> list[dict[str, Any]]:
    del tier, nodes
    return []


def multiprotocol_import_warnings(entry: dict[str, Any], nodes: list[Node]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    protocols = entry.get("protocols", {}) if isinstance(entry.get("protocols"), dict) else {}
    supported = {
        "vless",
        "vless_reality",
        "vmess",
        "trojan",
        "ss",
        "ssr",
        "socks5",
        "hysteria2",
        "anytls",
        "naive",
        "argo_xhttp",
        "xhttp",
        "wireguard",
        "tuic",
    }
    unsupported = sorted(set(protocols) - supported)
    if unsupported:
        warnings.append(
            _import_warning(
                "unsupported_sections_ignored",
                f"unsupported sections ignored: {', '.join(unsupported)}",
                params={"sections": unsupported},
            )
        )
    if not any(node.uri for node in nodes):
        warnings.append(
            _import_warning(
                "structured_export_incomplete",
                "this import uses structured protocol fields; v2ray plain subscription export may not include every variant",
                display=False,
            )
        )
    return warnings


def summarize_tier1_import_entry(entry: dict[str, Any]) -> dict[str, Any]:
    protocols = entry.get("protocols", {}) if isinstance(entry.get("protocols"), dict) else {}
    return {
        "name": entry.get("name", ""),
        "address": entry.get("address", ""),
        "port": entry.get("port", 0),
        "protocols": sorted(protocols.keys()),
        "fallback_order": list(entry.get("fallback_order", []) or []),
    }


def summarize_import_node(node: Node) -> dict[str, Any]:
    fingerprint_source = node.uri or "|".join(node.dedupe_key)
    return {
        "name": node.name,
        "protocol": node.protocol,
        "address": node.address,
        "port": node.port,
        "tier": node.tier,
        "fingerprint": hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()[:10],
        "has_tls": bool(node.tls or node.security in {"tls", "reality"}),
        "security": node.security,
        "network": node.network,
    }


def node_to_tier1_config(node: Node) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": node.name or node.remarks or f"{node.protocol}-{node.address}-{node.port}",
        "address": node.address,
        "port": int(node.port),
    }
    if node.protocol == "vless":
        payload["vless_link"] = node.uri
    else:
        payload["imported_link"] = node.uri
        payload["protocol"] = tier1_protocol_key_from_node(node)
    return payload


def tier1_import_entries_from_nodes(nodes: list[Node]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Node]] = {}
    order: list[str] = []
    for node in nodes:
        key = _tier1_physical_group_key(node)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(node)

    entries: list[dict[str, Any]] = []
    for key in order:
        group_nodes = grouped[key]
        merged = _merged_tier1_physical_entry(group_nodes)
        if merged is not None:
            entries.append(merged)
        else:
            entries.extend(node_to_tier1_config(node) for node in group_nodes)
    return entries


def _merged_tier1_physical_entry(nodes: list[Node]) -> dict[str, Any] | None:
    if len(nodes) < 2:
        return None
    protocols: dict[str, dict[str, str]] = {}
    for node in nodes:
        if not node.uri:
            return None
        protocol_key = tier1_protocol_key_from_node(node)
        if not protocol_key or protocol_key in protocols:
            return None
        protocols[protocol_key] = {"uri": node.uri}
    if len(protocols) < 2:
        return None
    address = str(nodes[0].address or "").strip()
    if not address:
        return None
    return {
        "name": _tier1_physical_import_name(nodes, fallback=address),
        "address": address,
        "port": _preferred_tier1_port(nodes),
        "protocols": protocols,
        "fallback_order": list(protocols.keys()),
    }


def _tier1_physical_group_key(node: Node) -> str:
    address = str(node.address or "").strip().lower()
    return address or "|".join(node.dedupe_key)


def tier1_protocol_key_from_node(node: Node) -> str:
    protocol = str(node.protocol or "").strip().lower()
    security = str(node.security or "").strip().lower()
    network = str(node.network or "").strip().lower()
    if protocol == "vless":
        if network == "xhttp":
            return "xhttp"
        if security == "reality":
            return "vless_reality"
        return "vless"
    if protocol in {"http", "https"}:
        return "naive"
    if protocol in {"hy2", "hysteria2"}:
        return "hysteria2"
    if protocol in {"socks", "socks5"}:
        return "socks5"
    if protocol in {"wireguard", "wg"}:
        return "wireguard"
    return protocol


def _preferred_tier1_port(nodes: list[Node]) -> int:
    ports = [int(node.port) for node in nodes if int(node.port or 0) > 0]
    if 443 in ports:
        return 443
    return ports[0] if ports else 443


def _tier1_physical_import_name(nodes: list[Node], *, fallback: str) -> str:
    names = [
        str(node.remarks or node.name or "").strip()
        for node in nodes
        if str(node.remarks or node.name or "").strip()
    ]
    common_tokens = _common_non_protocol_name_tokens(names)
    if common_tokens:
        return "-".join(common_tokens)
    return fallback


def _common_non_protocol_name_tokens(names: list[str]) -> list[str]:
    if len(names) < 2:
        return []
    token_lists = [_name_tokens(name) for name in names]
    common: list[str] = []
    for column in zip(*token_lists):
        first = column[0]
        if not first:
            break
        if any(token.lower() != first.lower() for token in column[1:]):
            break
        if first.lower() in TIER1_PROTOCOL_NAME_TOKENS:
            break
        common.append(first)
    return common


def _name_tokens(name: str) -> list[str]:
    return [token for token in re.split(r"[\s_.-]+", name.strip()) if token]


def unique_import_name(name: str, existing_names: set[str]) -> str:
    base = name or "imported-node"
    if base not in existing_names:
        return base
    suffix = 2
    while f"{base}-{suffix}" in existing_names:
        suffix += 1
    return f"{base}-{suffix}"


def existing_tier1_duplicate_keys(entries: list[Any]) -> set[tuple[str, ...]]:
    keys: set[tuple[str, ...]] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            variants = expand_tier1_entry(entry)
        except Exception:
            variants = []
        for node in variants:
            keys.add(node_duplicate_key(node))
    return keys


def node_duplicate_key(node: Node) -> tuple[str, ...]:
    return node.dedupe_key
