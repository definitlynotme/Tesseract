from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from config_utils import coerce_bool
from models import Node
from parser import SUPPORTED_URI_SCHEMES, parse_node_uri


PROTOCOL_LABELS = {
    "vless": "VLESS",
    "vless_reality": "VLESS Reality",
    "vmess": "Vmess",
    "trojan": "Trojan",
    "ss": "Shadowsocks",
    "socks5": "SOCKS5",
    "hysteria2": "Hysteria2",
    "anytls": "AnyTLS",
    "naive": "Naive HTTPS",
    "argo_xhttp": "Argo XHTTP",
    "xhttp": "VLESS XHTTP",
    "http": "HTTPS",
    "tuic": "TUIC",
    "wireguard": "WireGuard",
}


def protocol_label(protocol_key: str) -> str:
    return PROTOCOL_LABELS.get(protocol_key, protocol_key.replace("_", " ").title())


def physical_node_name(node: Node) -> str:
    return str(node.extra.get("physical_node") or node.name or node.address)


def safe_group_name(value: str, *, prefix: str = "") -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_.-]+", "-", value.strip()).strip("-")
    return f"{prefix}{cleaned or 'node'}"


def protocol_variant_key(node: Node) -> str:
    return str(node.extra.get("protocol_key") or node.protocol)


def tier1_proxy_name(node: Node) -> str:
    physical = physical_node_name(node)
    label = str(node.extra.get("protocol_label") or protocol_label(protocol_variant_key(node)))
    return f"{physical} :: {label}"


def tier1_auto_group_name(physical_name: str) -> str:
    return safe_group_name(physical_name, prefix="TIER1-") + "-AUTO"


def tier1_select_group_name(physical_name: str) -> str:
    return safe_group_name(physical_name, prefix="TIER1-") + "-SELECT"


def is_tier1_protocol_variant(node: Node) -> bool:
    return node.tier == "tier1" and bool(node.extra.get("physical_node"))


def group_tier1_nodes(nodes: list[Node]) -> list[tuple[str, list[Node]]]:
    grouped: dict[str, list[Node]] = {}
    order: list[str] = []
    for node in nodes:
        key = physical_node_name(node)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(node)
    return [(key, order_protocol_nodes(grouped[key])) for key in order]


def order_protocol_nodes(nodes: list[Node]) -> list[Node]:
    if not nodes:
        return []
    fallback_order = _normalize_fallback_order(nodes[0].extra.get("fallback_order"))
    if not fallback_order:
        return list(nodes)
    rank: dict[str, int] = {}
    for index, value in enumerate(fallback_order):
        for alias in _fallback_aliases(value):
            rank.setdefault(alias, index)

    def sort_key(item: tuple[int, Node]) -> tuple[int, int]:
        original_index, node = item
        aliases = _node_fallback_aliases(node)
        node_rank = min((rank[alias] for alias in aliases if alias in rank), default=len(fallback_order) + original_index)
        return node_rank, original_index

    return [node for _, node in sorted(enumerate(nodes), key=sort_key)]


def expand_tier1_entry(entry: dict[str, Any], *, logger: logging.Logger | None = None) -> list[Node]:
    base_name = str(entry.get("name") or entry.get("address") or "tier1-node")
    default_address = str(entry.get("address") or "")
    default_port = int(entry.get("port") or 0)
    nodes: list[Node] = []

    legacy_links = [
        ("vless_reality", entry.get("vless_link")),
        ("hysteria2", entry.get("hysteria2_link")),
        ("naive", entry.get("naive_link")),
        (str(entry.get("protocol") or _protocol_key_from_link(entry.get("imported_link")) or "imported"), entry.get("imported_link")),
    ]
    for key, link in legacy_links:
        if link:
            node = _node_from_uri_or_link(key, str(link), entry, logger=logger)
            if node:
                nodes.append(_decorate_node(node, entry, key))

    protocols = entry.get("protocols", {})
    if isinstance(protocols, dict):
        for raw_key, raw_config in protocols.items():
            key = _canonical_protocol_key(str(raw_key))
            if raw_config is None or raw_config is False:
                continue
            if isinstance(raw_config, dict) and not coerce_bool(raw_config.get("enabled", True), True):
                continue
            node = _node_from_protocol_config(key, raw_config, entry, logger=logger)
            if node:
                nodes.append(_decorate_node(node, entry, key))

    if not nodes and default_address and default_port:
        nodes.append(
            _decorate_node(
                Node(
                    name=base_name,
                    protocol=str(entry.get("protocol") or "vless"),
                    uri=str(entry.get("vless_link") or entry.get("imported_link") or ""),
                    address=default_address,
                    port=default_port,
                    source="config.yaml",
                    tier="tier1",
                    remarks=base_name,
                ),
                entry,
                str(entry.get("protocol") or "vless"),
            )
        )
    return order_protocol_nodes(_dedupe_variants(nodes))


def parse_tier1_multiprotocol_block(text: str, *, name: str | None = None) -> dict[str, Any]:
    sections = _split_sections(text)
    protocols: dict[str, Any] = {}
    fallback_order: list[str] = []
    detected_name = name or ""
    default_address = ""
    default_port = 0

    def update_default_from_node(node: Node | None) -> None:
        nonlocal detected_name, default_address, default_port
        if not node:
            return
        if not detected_name:
            detected_name = node.remarks or node.name
        if not default_address:
            default_address = node.address
        if not default_port:
            default_port = int(node.port)

    for section_name, lines in sections:
        key = _section_to_protocol_key(section_name)
        if key == "fallback_order":
            fallback_order = _parse_fallback_order_lines(lines)
            continue
        body = "\n".join(lines).strip()
        if not body:
            continue
        first_uri = _first_uri(body)
        if key in {"vless_reality", "vless"} and first_uri:
            node = parse_node_uri(first_uri, source="native-app-import", tier="tier1")
            update_default_from_node(node)
            protocols[key] = {"uri": first_uri}
            continue
        if key == "hysteria2" and first_uri:
            node = _parse_hysteria2_uri(first_uri, {})
            update_default_from_node(node)
            protocols[key] = {"uri": first_uri}
            continue
        if key == "naive" and first_uri:
            node = _parse_naive_uri(first_uri, {})
            update_default_from_node(node)
            protocols[key] = {"uri": first_uri}
            continue
        if first_uri:
            node = parse_node_uri(
                first_uri,
                source="native-app-import",
                tier="tier1",
            )
            update_default_from_node(node)
            protocols[key] = {"uri": first_uri}
            continue

        values = _parse_key_value_lines(lines)
        if key == "anytls":
            if "server_name" not in values and "sni" in values:
                values["server_name"] = values["sni"]
            protocols[key] = values
        elif key in {"argo_xhttp", "xhttp"}:
            listen = _first_bare_endpoint(lines)
            if listen and "listen" not in values:
                values["listen"] = listen
            protocols[key] = values
        elif values:
            protocols[key] = values

        if not default_address:
            default_address = str(values.get("server") or values.get("address") or "").strip()
        if not default_port and values.get("port"):
            try:
                default_port = int(values["port"])
            except (TypeError, ValueError):
                pass

    if not protocols:
        raise ValueError("no supported protocol sections found")
    if not default_address:
        for config in protocols.values():
            if isinstance(config, dict):
                default_address = str(config.get("server") or config.get("address") or "").strip()
                if default_address:
                    break
    if not default_address:
        for key, config in protocols.items():
            if key not in {"argo_xhttp", "xhttp"} or not isinstance(config, dict):
                continue
            listen = str(config.get("listen") or "").strip()
            if ":" in listen:
                default_address = listen.rsplit(":", 1)[0].strip()
                if default_address:
                    break
    if not default_port:
        for config in protocols.values():
            if isinstance(config, dict) and config.get("port"):
                try:
                    default_port = int(config["port"])
                    break
                except (TypeError, ValueError):
                    pass
    if not default_address:
        raise ValueError("tier1 multiprotocol block is missing a server/address")
    if not default_port:
        default_port = 443
    result = {
        "name": detected_name or default_address,
        "address": default_address,
        "port": default_port,
        "protocols": protocols,
    }
    if fallback_order:
        result["fallback_order"] = fallback_order
    return result


def _canonical_protocol_key(key: str) -> str:
    normalized = key.strip().lower().replace("-", "_")
    aliases = {
        "hy2": "hysteria2",
        "hysteria": "hysteria2",
        "hysteria_2": "hysteria2",
        "vless_reality_vision": "vless_reality",
        "reality": "vless_reality",
        "any_tls": "anytls",
        "naiveproxy": "naive",
        "naive_https": "naive",
        "argo": "argo_xhttp",
        "argo_local": "argo_xhttp",
        "vless_xhttp": "xhttp",
        "wg": "wireguard",
        "wire_guard": "wireguard",
        "amneziawg": "wireguard",
        "amnezia_wg": "wireguard",
        "tuic_v5": "tuic",
        "tuicv5": "tuic",
        "socks": "socks5",
    }
    return aliases.get(normalized, normalized)


def ordered_tier1_protocol_keys(
    protocols: dict[str, Any],
    fallback_order: Any,
) -> list[str]:
    """Resolve persisted fallback aliases to the actual configured keys.

    Runtime ordering has long accepted legacy names such as ``reality`` and
    ``hy2``.  The editor and read-only overview must resolve those aliases the
    same way or opening an older configuration can silently display and save a
    different route order.
    """

    available = [
        str(key).strip()
        for key in protocols
        if str(key).strip()
    ]
    ordered: list[str] = []
    for raw_value in parse_fallback_order_text(fallback_order):
        aliases = _fallback_aliases(str(raw_value))
        match = next(
            (
                key
                for key in available
                if key not in ordered and aliases.intersection(_fallback_aliases(key))
            ),
            "",
        )
        if match:
            ordered.append(match)
    ordered.extend(key for key in available if key not in ordered)
    return ordered


def _section_to_protocol_key(section_name: str) -> str:
    normalized = section_name.strip().lower()
    if "fallback" in normalized or "failover" in normalized or "退回" in normalized or "回退" in normalized:
        return "fallback_order"
    if "vless" in normalized and "xhttp" in normalized:
        return "xhttp"
    if "vless" in normalized or "reality" in normalized:
        return "vless_reality"
    if "hysteria" in normalized or "hy2" in normalized:
        return "hysteria2"
    if "anytls" in normalized or "any tls" in normalized:
        return "anytls"
    if "naive" in normalized:
        return "naive"
    if "argo" in normalized:
        return "argo_xhttp"
    if "xhttp" in normalized:
        return "xhttp"
    if "tuic" in normalized:
        return "tuic"
    if "wireguard" in normalized or "wire guard" in normalized or "wg" in normalized or "amnezia" in normalized:
        return "wireguard"
    return _canonical_protocol_key(section_name)


def parse_fallback_order_text(value: str | list[Any] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return _parse_fallback_order_lines(str(value).splitlines())


def _parse_fallback_order_lines(lines: list[str]) -> list[str]:
    text = "\n".join(lines)
    text = re.sub(r"(?i)^fallback(?:\s+order)?\s*[:=]", "", text.strip())
    chunks = re.split(r"\s*(?:->|,|>|/|\n)\s*", text)
    return [chunk.strip() for chunk in chunks if chunk.strip()]


def _normalize_fallback_order(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return parse_fallback_order_text(value)
    return []


def _fallback_aliases(value: str) -> set[str]:
    raw = value.strip()
    canonical = _canonical_protocol_key(raw)
    return {
        raw,
        raw.lower(),
        raw.lower().replace(" ", "_").replace("-", "_"),
        canonical,
        protocol_label(canonical),
        protocol_label(canonical).lower(),
    }


def _node_fallback_aliases(node: Node) -> set[str]:
    key = protocol_variant_key(node)
    label = str(node.extra.get("protocol_label") or protocol_label(key))
    return {
        key,
        key.lower(),
        label,
        label.lower(),
        node.name,
        node.name.lower(),
        tier1_proxy_name(node),
        tier1_proxy_name(node).lower(),
    }


def _split_sections(text: str) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = []
    current_name = "default"
    current_lines: list[str] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.endswith(":") and "://" not in line and "=" not in line:
            if current_lines:
                sections.append((current_name, current_lines))
            current_name = line[:-1].strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_lines:
        sections.append((current_name, current_lines))
    return sections


def _first_uri(text: str) -> str:
    for match in re.finditer(
        r"([a-z][a-z0-9+.-]*)://[^\s\"'<>]+",
        text,
        re.IGNORECASE,
    ):
        if match.group(1).lower() in SUPPORTED_URI_SCHEMES:
            return match.group(0)
    return ""


def _parse_key_value_lines(lines: list[str]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for line in lines:
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        normalized_key = key.strip().replace("-", "_")
        raw_value = value.strip()
        if raw_value.lower() in {"true", "false"}:
            values[normalized_key] = raw_value.lower() == "true"
        else:
            values[normalized_key] = raw_value
    return values


def _first_bare_endpoint(lines: list[str]) -> str:
    for line in lines:
        if "=" in line or "://" in line:
            continue
        if re.match(r"^[^\s:]+:\d+$", line):
            return line
    return ""


def _node_from_protocol_config(
    key: str,
    raw_config: Any,
    entry: dict[str, Any],
    *,
    logger: logging.Logger | None,
) -> Node | None:
    if isinstance(raw_config, str):
        return _node_from_uri_or_link(key, raw_config, entry, logger=logger)
    if not isinstance(raw_config, dict):
        return None
    link = raw_config.get("uri") or raw_config.get("link") or raw_config.get("url")
    if link:
        node = _node_from_uri_or_link(key, str(link), {**entry, **raw_config}, logger=logger)
        if node:
            return _merge_config_over_node(node, raw_config)
    if key == "hysteria2":
        return _build_hysteria2_node(raw_config, entry)
    if key == "anytls":
        return _build_anytls_node(raw_config, entry)
    if key == "naive":
        return _build_naive_node(raw_config, entry)
    if key in {"argo_xhttp", "xhttp"}:
        return _build_xhttp_node(raw_config, entry, key)
    if key in {"vless", "vless_reality"}:
        return _build_vless_node(raw_config, entry, key)
    if key == "wireguard":
        return _build_wireguard_node(raw_config, entry)
    if key == "tuic":
        return _build_tuic_node(raw_config, entry)
    return None


def _node_from_uri_or_link(
    key: str,
    link: str,
    entry: dict[str, Any],
    *,
    logger: logging.Logger | None,
) -> Node | None:
    scheme = link.split("://", 1)[0].lower() if "://" in link else ""
    if scheme in SUPPORTED_URI_SCHEMES:
        return parse_node_uri(link, source="config.yaml", tier="tier1", logger=logger)
    if key == "hysteria2":
        return _parse_hysteria2_uri(link, entry)
    if scheme in {"http", "https"} or key == "naive":
        return _parse_naive_uri(link, entry)
    if scheme == "anytls" or key == "anytls":
        return _parse_anytls_uri(link, entry)
    if scheme in {"wireguard", "wg"} or key == "wireguard":
        return parse_node_uri(link, source="config.yaml", tier="tier1", logger=logger)
    if scheme == "tuic" or key == "tuic":
        return parse_node_uri(link, source="config.yaml", tier="tier1", logger=logger)
    return None


def _protocol_key_from_link(link: Any) -> str:
    text = str(link or "")
    if "://" not in text:
        return ""
    scheme = text.split("://", 1)[0].strip().lower()
    if scheme == "hy2":
        return "hysteria2"
    if scheme in {"http", "https"}:
        return "naive"
    return _canonical_protocol_key(scheme)


def _parse_hysteria2_uri(uri: str, entry: dict[str, Any]) -> Node | None:
    parsed = urlsplit(uri)
    if parsed.scheme.lower() != "hysteria2":
        return None
    if parsed.username:
        password = unquote(parsed.username)
    elif "@" in parsed.netloc:
        password = unquote(parsed.netloc.split("@", 1)[0])
    else:
        password = ""
    address = parsed.hostname or str(entry.get("address") or "")
    port = int(parsed.port or entry.get("port") or 443)
    query = parse_qs(parsed.query)
    remarks = unquote(parsed.fragment) if parsed.fragment else str(entry.get("name") or address)
    node = Node(
        name=remarks,
        protocol="hysteria2",
        uri=uri,
        address=address,
        port=port,
        source="config.yaml",
        tier="tier1",
        password=password,
        sni=_first(query, "sni") or _first(query, "peer"),
        tls=True,
        remarks=remarks,
        extra={key: value[0] if len(value) == 1 else value for key, value in query.items()},
    )
    _copy_bool_query(query, node.extra, "insecure", "skip-cert-verify")
    return node


def _parse_anytls_uri(uri: str, entry: dict[str, Any]) -> Node | None:
    parsed = urlsplit(uri)
    address = parsed.hostname or str(entry.get("address") or "")
    port = int(parsed.port or entry.get("port") or 443)
    query = parse_qs(parsed.query)
    remarks = unquote(parsed.fragment) if parsed.fragment else str(entry.get("name") or address)
    return Node(
        name=remarks,
        protocol="anytls",
        uri=uri,
        address=address,
        port=port,
        source="config.yaml",
        tier="tier1",
        password=unquote(parsed.username or ""),
        sni=_first(query, "sni") or _first(query, "server_name"),
        tls=True,
        remarks=remarks,
        extra={key: value[0] if len(value) == 1 else value for key, value in query.items()},
    )


def _parse_naive_uri(uri: str, entry: dict[str, Any]) -> Node | None:
    parsed = urlsplit(uri)
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    address = parsed.hostname or str(entry.get("address") or "")
    port = int(parsed.port or (443 if parsed.scheme.lower() == "https" else 80))
    remarks = unquote(parsed.fragment) if parsed.fragment else str(entry.get("name") or address)
    return Node(
        name=remarks,
        protocol="http",
        uri=uri,
        address=address,
        port=port,
        source="config.yaml",
        tier="tier1",
        password=unquote(parsed.password or ""),
        tls=parsed.scheme.lower() == "https",
        remarks=remarks,
        extra={
            "username": unquote(parsed.username or ""),
            "skip-cert-verify": False,
        },
    )


def _build_hysteria2_node(config: dict[str, Any], entry: dict[str, Any]) -> Node | None:
    address = str(config.get("server") or config.get("address") or entry.get("address") or "")
    password = str(config.get("password") or config.get("auth") or "")
    if not password:
        return None
    return Node(
        name=str(config.get("name") or entry.get("name") or address),
        protocol="hysteria2",
        uri=str(config.get("uri") or config.get("link") or ""),
        address=address,
        port=int(config.get("port") or entry.get("port") or 443),
        source="config.yaml",
        tier="tier1",
        password=password,
        sni=str(config.get("sni") or config.get("server_name") or "") or None,
        tls=True,
        remarks=str(config.get("name") or entry.get("name") or address),
        extra=_copy_known_options(
            config,
            [
                "ports",
                "hop-interval",
                "hop_interval",
                "up",
                "down",
                "obfs",
                "obfs-password",
                "obfs_password",
                "fingerprint",
                "alpn",
                "skip-cert-verify",
                "skip_cert_verify",
                "insecure",
            ],
        ),
    )


def _build_anytls_node(config: dict[str, Any], entry: dict[str, Any]) -> Node | None:
    address = str(config.get("server") or config.get("address") or entry.get("address") or "")
    password = str(config.get("password") or "")
    if not password:
        return None
    return Node(
        name=str(config.get("name") or entry.get("name") or address),
        protocol="anytls",
        uri=str(config.get("uri") or config.get("link") or ""),
        address=address,
        port=int(config.get("port") or entry.get("port") or 443),
        source="config.yaml",
        tier="tier1",
        password=password,
        sni=str(config.get("sni") or config.get("server_name") or "") or None,
        tls=True,
        remarks=str(config.get("name") or entry.get("name") or address),
        extra=_copy_known_options(
            config,
            [
                "client-fingerprint",
                "client_fingerprint",
                "alpn",
                "skip-cert-verify",
                "skip_cert_verify",
                "insecure",
                "idle-session-check-interval",
                "idle_session_check_interval",
                "idle-session-timeout",
                "idle_session_timeout",
                "min-idle-session",
                "min_idle_session",
            ],
        ),
    )


def _build_naive_node(config: dict[str, Any], entry: dict[str, Any]) -> Node | None:
    address = str(config.get("server") or config.get("address") or entry.get("address") or "")
    password = str(config.get("password") or "")
    if not password:
        return None
    return Node(
        name=str(config.get("name") or entry.get("name") or address),
        protocol="http",
        uri=str(config.get("uri") or config.get("link") or ""),
        address=address,
        port=int(config.get("port") or entry.get("port") or 443),
        source="config.yaml",
        tier="tier1",
        password=password,
        sni=str(config.get("sni") or config.get("server_name") or "") or None,
        tls=True,
        remarks=str(config.get("name") or entry.get("name") or address),
        extra=_copy_known_options(
            config,
            ["username", "skip-cert-verify", "skip_cert_verify", "insecure", "fingerprint"],
        ),
    )


def _build_xhttp_node(config: dict[str, Any], entry: dict[str, Any], key: str) -> Node | None:
    listen = str(config.get("listen") or "")
    address = str(config.get("server") or config.get("address") or entry.get("address") or "")
    port = int(config.get("port") or entry.get("port") or 443)
    if listen and ":" in listen:
        address, raw_port = listen.rsplit(":", 1)
        port = int(raw_port)
    uuid = str(config.get("uuid") or config.get("id") or "")
    if not uuid:
        return None
    security = str(config.get("security") or "none").lower()
    host = str(config.get("host") or config.get("server_name") or config.get("servername") or config.get("authority") or "") or None
    path = str(config.get("path") or config.get("xhttp_path") or "/")
    sni = str(config.get("sni") or config.get("server_name") or config.get("servername") or "") or None
    return Node(
        name=str(config.get("name") or entry.get("name") or address),
        protocol="vless",
        uri=str(config.get("uri") or config.get("link") or ""),
        address=address,
        port=port,
        source="config.yaml",
        tier="tier1",
        uuid=uuid,
        encryption=str(config.get("encryption") or "none"),
        security=security,
        network="xhttp",
        host=host,
        path=path,
        sni=sni,
        tls=security in {"tls", "reality"},
        remarks=str(config.get("name") or entry.get("name") or address),
        extra={
            **_copy_known_options(
                config,
                [
                    "mode",
                    "headers",
                    "no-grpc-header",
                    "no_grpc_header",
                    "x-padding-bytes",
                    "x_padding_bytes",
                    "sc-max-each-post-bytes",
                    "sc_max_each_post_bytes",
                    "alpn",
                    "client-fingerprint",
                    "client_fingerprint",
                    "skip-cert-verify",
                    "skip_cert_verify",
                    "insecure",
                    "pbk",
                    "sid",
                    "fp",
                    "flow",
                ],
            ),
            "protocol_key": key,
        },
    )


def _build_vless_node(config: dict[str, Any], entry: dict[str, Any], key: str) -> Node | None:
    address = str(config.get("server") or config.get("address") or entry.get("address") or "")
    security = str(config.get("security") or ("reality" if key == "vless_reality" else "none"))
    uuid = str(config.get("uuid") or "")
    if not uuid:
        return None
    return Node(
        name=str(config.get("name") or entry.get("name") or address),
        protocol="vless",
        uri=str(config.get("uri") or config.get("link") or ""),
        address=address,
        port=int(config.get("port") or entry.get("port") or 443),
        source="config.yaml",
        tier="tier1",
        uuid=uuid,
        encryption=str(config.get("encryption") or "none"),
        security=security,
        network=str(config.get("network") or config.get("type") or "tcp"),
        host=str(config.get("host") or "") or None,
        path=str(config.get("path") or "") or None,
        sni=str(config.get("sni") or config.get("server_name") or "") or None,
        tls=security in {"tls", "reality"},
        remarks=str(config.get("name") or entry.get("name") or address),
        extra=_copy_known_options(config, ["flow", "pbk", "sid", "fp", "client-fingerprint", "client_fingerprint"]),
    )


def _build_wireguard_node(config: dict[str, Any], entry: dict[str, Any]) -> Node | None:
    address = str(config.get("server") or config.get("address") or config.get("endpoint_host") or entry.get("address") or "")
    port_value = (
        config.get("port")
        or config.get("endpoint_port")
        or entry.get("port")
        or 51820
    )
    try:
        port = int(port_value)
    except (TypeError, ValueError):
        port = 51820
    private_key = str(config.get("private_key") or config.get("private-key") or config.get("privkey") or "")
    public_key = str(
        config.get("peer_public_key")
        or config.get("public_key")
        or config.get("public-key")
        or config.get("peer-public-key")
        or config.get("peer_pubkey")
        or ""
    )
    if not address or not private_key or not public_key:
        return None
    extra: dict[str, Any] = {"public-key": public_key}
    preshared = (
        config.get("preshared_key")
        or config.get("pre-shared-key")
        or config.get("preshared-key")
        or config.get("psk")
    )
    if preshared:
        extra["pre-shared-key"] = str(preshared)
    allowed_ips = config.get("allowed_ips") or config.get("allowed-ips") or config.get("allowedips")
    if isinstance(allowed_ips, str):
        allowed_ips = [chunk.strip() for chunk in allowed_ips.split(",") if chunk.strip()]
    extra["allowed-ips"] = list(allowed_ips) if allowed_ips else ["0.0.0.0/0", "::/0"]
    address_field = config.get("address") or config.get("ip") or config.get("local_address")
    if isinstance(address_field, str):
        address_list = [chunk.strip() for chunk in address_field.split(",") if chunk.strip()]
    elif isinstance(address_field, list):
        address_list = [str(item).strip() for item in address_field if str(item).strip()]
    else:
        address_list = []
    for local_ip in address_list:
        bare = local_ip.split("/", 1)[0]
        if ":" in bare:
            extra.setdefault("ipv6", bare)
        else:
            extra.setdefault("ip", bare)
    reserved = config.get("reserved")
    if isinstance(reserved, str):
        reserved = [chunk.strip() for chunk in reserved.split(",") if chunk.strip()]
    if isinstance(reserved, list) and reserved:
        parsed_reserved: list[Any] = []
        for chunk in reserved:
            try:
                parsed_reserved.append(int(str(chunk).strip()))
            except (TypeError, ValueError):
                parsed_reserved.append(chunk)
        extra["reserved"] = parsed_reserved
    for amnezia_key in ("jc", "jmin", "jmax", "s1", "s2", "h1", "h2", "h3", "h4"):
        value = config.get(amnezia_key)
        if value is None:
            continue
        try:
            extra[amnezia_key] = int(value)
        except (TypeError, ValueError):
            extra[amnezia_key] = value
    mtu = config.get("mtu")
    if mtu is not None:
        try:
            extra["mtu"] = int(mtu)
        except (TypeError, ValueError):
            pass
    keepalive = config.get("persistent_keepalive") or config.get("persistent-keepalive") or config.get("keepalive")
    if keepalive is not None:
        try:
            extra["persistent-keepalive"] = int(keepalive)
        except (TypeError, ValueError):
            pass
    dns = config.get("dns")
    if isinstance(dns, str):
        dns = [chunk.strip() for chunk in dns.split(",") if chunk.strip()]
    if isinstance(dns, list) and dns:
        extra["dns"] = list(dns)
    workers = config.get("workers")
    if workers is not None:
        try:
            extra["workers"] = int(workers)
        except (TypeError, ValueError):
            pass
    name = str(config.get("name") or entry.get("name") or address)
    return Node(
        name=name,
        protocol="wireguard",
        uri=str(config.get("uri") or config.get("link") or ""),
        address=address,
        port=port,
        source="config.yaml",
        tier="tier1",
        password=private_key,
        tls=False,
        remarks=name,
        extra=extra,
    )


def _build_tuic_node(config: dict[str, Any], entry: dict[str, Any]) -> Node | None:
    address = str(config.get("server") or config.get("address") or entry.get("address") or "")
    if not address:
        return None
    try:
        port = int(config.get("port") or entry.get("port") or 443)
    except (TypeError, ValueError):
        port = 443
    uuid = str(config.get("uuid") or config.get("id") or "")
    password = str(config.get("password") or config.get("token") or "")
    if not (uuid and password) and not config.get("token"):
        # Mihomo accepts (uuid, password) for v5 and token for v4.
        if not password and not uuid:
            return None
    extra = _copy_known_options(
        config,
        [
            "ip",
            "version",
            "heartbeat-interval",
            "heartbeat_interval",
            "disable-sni",
            "disable_sni",
            "reduce-rtt",
            "reduce_rtt",
            "request-timeout",
            "request_timeout",
            "udp-relay-mode",
            "udp_relay_mode",
            "congestion-controller",
            "congestion_control",
            "congestion_controller",
            "bbr-profile",
            "bbr_profile",
            "max-udp-relay-packet-size",
            "max_udp_relay_packet_size",
            "fast-open",
            "fast_open",
            "max-open-streams",
            "max_open_streams",
            "alpn",
            "skip-cert-verify",
            "skip_cert_verify",
            "insecure",
            "token",
        ],
    )
    if config.get("version") is not None and "version" not in extra:
        try:
            extra["version"] = int(config["version"])
        except (TypeError, ValueError):
            extra["version"] = config["version"]
    name = str(config.get("name") or entry.get("name") or address)
    return Node(
        name=name,
        protocol="tuic",
        uri=str(config.get("uri") or config.get("link") or ""),
        address=address,
        port=port,
        source="config.yaml",
        tier="tier1",
        uuid=uuid or None,
        password=password or None,
        sni=str(config.get("sni") or config.get("server_name") or "") or None,
        tls=True,
        remarks=name,
        extra=extra,
    )


def _merge_config_over_node(node: Node, config: dict[str, Any]) -> Node:
    if config.get("name"):
        node.name = str(config["name"])
    if config.get("server_name") and not node.sni:
        node.sni = str(config["server_name"])
    for key, value in _copy_known_options(config, list(config.keys())).items():
        node.extra.setdefault(key, value)
    return node


def _decorate_node(node: Node, entry: dict[str, Any], key: str) -> Node:
    physical = str(entry.get("name") or entry.get("address") or node.name or node.address)
    canonical_key = _canonical_protocol_key(key)
    if canonical_key == "vless_reality" and node.protocol == "vless" and (node.security or "").lower() != "reality":
        canonical_key = "vless"
    label = protocol_label(canonical_key)
    node.tier = "tier1"
    node.source = node.source or "config.yaml"
    node.extra["physical_node"] = physical
    node.extra["protocol_key"] = canonical_key
    node.extra["protocol_label"] = label
    fallback_order = entry.get("fallback_order") or entry.get("protocol_fallback") or entry.get("failover_order")
    if fallback_order:
        node.extra["fallback_order"] = _normalize_fallback_order(fallback_order)
    entry_dns = entry.get("dns")
    if isinstance(entry_dns, dict):
        node.extra["dns"] = dict(entry_dns)
    node.extra["tier1_address"] = entry.get("address", node.address)
    node.extra["tier1_port"] = entry.get("port", node.port)
    node.name = tier1_proxy_name(node)
    return node


def _dedupe_variants(nodes: list[Node]) -> list[Node]:
    seen: set[tuple[str, str, int, str]] = set()
    unique: list[Node] = []
    for node in nodes:
        key = (protocol_variant_key(node), node.address, int(node.port), str(node.uuid or node.password or node.uri))
        if key in seen:
            continue
        seen.add(key)
        unique.append(node)
    return unique


def _first(values: dict[str, list[str]], key: str, default: str | None = None) -> str | None:
    items = values.get(key, [])
    return items[0] if items else default


def _copy_bool_query(query: dict[str, list[str]], target: dict[str, Any], source_key: str, target_key: str) -> None:
    value = _first(query, source_key)
    if value is not None:
        target[target_key] = str(value).lower() in {"1", "true", "yes"}


def _copy_known_options(config: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    copied: dict[str, Any] = {}
    for key in keys:
        if key not in config:
            continue
        normalized = key.replace("_", "-")
        if normalized == "insecure":
            copied["skip-cert-verify"] = coerce_bool(config[key], False)
        else:
            copied[normalized] = config[key]
    return copied
