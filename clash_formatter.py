from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import logging
import re
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

import yaml

from config_utils import (
    coerce_bool,
    normalize_routing_rule_order,
    routing_rule_mapping,
)
from exit_quality_endpoints import EXIT_QUALITY_SYSTEM_HOSTS
from gateway_selection import GatewaySelection, gateway_identity
from geodata_runtime import runtime_config_text_uses_geodata
from log_event_reports import log_raw_diagnostic
from models import Node
from shadowsocks_capabilities import (
    SUPPORTED_SHADOWSOCKS_CIPHERS,
    normalize_shadowsocks_cipher,
    validate_shadowsocks_credential,
)
from runtime_settings import (
    DEFAULT_PRIMARY_GATEWAY_MAX_LATENCY_MS,
    DEFAULT_ROUTE_HEALTH_CHECK_URL,
    validate_primary_gateway_max_latency_ms,
)
from dns_resolver_policy import (
    DEFAULT_FAKE_IP_FILTER,
    DNS_MODE_AUTO,
    dns_config_list,
    normalize_dns_mode,
    static_dns_lists,
)
from runtime_source_transport import (
    RUNTIME_SOURCE_BOOTSTRAP_POOL_PROXY,
    RUNTIME_SOURCE_EGRESS_PROXY_NAME,
    RUNTIME_SOURCE_LISTENER_NAMES,
    RuntimeSourceListenerSpec,
)
from routing_rules import compile_rule_list, stored_rule_list
from tier1_protocols import group_tier1_nodes, is_tier1_protocol_variant, tier1_auto_group_name, tier1_proxy_name, tier1_select_group_name


SUPPORTED_CLASH_PROTOCOLS = {"vless", "vmess", "trojan", "ss", "ssr", "hysteria2", "anytls", "tuic", "http", "socks5", "wireguard"}
# Resolver defaults are not written here any more: ``client.dns.mode`` selects
# a neutral static pool, and the runtime replaces it with a qualified rung.

ROUTING_RULE_TARGETS = {
    "direct": "DIRECT",
    "proxy": "GENERAL",
    "reject": "REJECT",
}

MIHOMO_RESERVED_PROXY_NAMES = {
    "DIRECT",
    "COMPATIBLE",
    "REJECT",
    "REJECT-DROP",
    "PASS",
    "PASS-RULE",
    "TIER1-AUTO",
    "TIER1-SELECT",
    "TIER2-AUTO",
    "TIER3-AUTO",
    "PROXY-FALLBACK",
    "EXIT-SELECT",
    "FINAL-EXIT",
    "AI-FIXED-IP",
    "GENERAL",
    "GLOBAL",
    "BOOTSTRAP-EGRESS-PROXY",
    "BOOTSTRAP-GEODATA",
    RUNTIME_SOURCE_EGRESS_PROXY_NAME,
}

# First-run GeoData download (bootstrap Core only): its own authenticated
# loopback listener, bound to a group that rotates over the Bastion Layer pool.
BOOTSTRAP_GEODATA_GROUP_NAME = "BOOTSTRAP-GEODATA"
BOOTSTRAP_GEODATA_LISTENER_NAME = "bootstrap-geodata"
_BOOTSTRAP_GEODATA_INHERITED_KEYS = (
    "proxies",
    "use",
    "url",
    "interval",
    "lazy",
    "timeout",
    "expected-status",
    "max-failed-times",
)

UNAUTHORIZED_DIRECT_POLICIES = {"DIRECT", "COMPATIBLE"}
REMOVED_ROUTING_POLICIES = {"AI-FIXED-IP", "AI_FIXED_IP"}
DISALLOWED_ROUTING_POLICIES = (
    UNAUTHORIZED_DIRECT_POLICIES | REMOVED_ROUTING_POLICIES
)

LOCAL_NETWORK_SYSTEM_RULES = (
    "DOMAIN-SUFFIX,localhost,DIRECT",
    "DOMAIN-SUFFIX,local,DIRECT",
    "DOMAIN-SUFFIX,lan,DIRECT",
    "DOMAIN-SUFFIX,home.arpa,DIRECT",
    "IP-CIDR,127.0.0.0/8,DIRECT,no-resolve",
    "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve",
    "IP-CIDR,172.16.0.0/12,DIRECT,no-resolve",
    "IP-CIDR,192.168.0.0/16,DIRECT,no-resolve",
    "IP-CIDR,169.254.0.0/16,DIRECT,no-resolve",
    "IP-CIDR6,::1/128,DIRECT,no-resolve",
    "IP-CIDR6,fc00::/7,DIRECT,no-resolve",
    "IP-CIDR6,fe80::/10,DIRECT,no-resolve",
)

# TIER3-AUTO re-proves its free-node members against a real site at least this
# often, so a member that stops completing real HTTPS leaves the route within
# about two minutes.  The cost is one Core-side request per exported member per
# interval; it never touches the Tier3 validation pipeline.
TIER3_ROUTE_HEALTH_CHECK_MAX_INTERVAL_SECONDS = 120


def _final_exit_rule_for_probe_url(value: Any) -> str | None:
    """Compile only a probe URL's authority into an exact system route.

    Userinfo, path, query, and fragment data must never be copied into the
    generated configuration.  Literal addresses use CIDR rules because a
    DOMAIN rule is not authoritative for an IP URL in Mihomo.
    """

    try:
        hostname = str(urlsplit(str(value or "").strip()).hostname or "").strip()
    except (TypeError, ValueError):
        return None
    if not hostname:
        return None
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            normalized_host = hostname.rstrip(".").encode("idna").decode("ascii").lower()
        except (UnicodeError, ValueError):
            return None
        if not normalized_host or any(
            character.isspace() or character in {",", "/", "\\", "#", "?", "@", "[", "]"}
            for character in normalized_host
        ):
            return None
        return f"DOMAIN,{normalized_host},FINAL-EXIT"
    if address.version == 4:
        return f"IP-CIDR,{address.compressed}/32,FINAL-EXIT,no-resolve"
    return f"IP-CIDR6,{address.compressed}/128,FINAL-EXIT,no-resolve"


def build_final_exit_system_rules(probe_urls: Any = None) -> list[str]:
    """Return ordered, de-duplicated routes that ordinary rules cannot override."""

    rules = [f"DOMAIN,{host},FINAL-EXIT" for host in EXIT_QUALITY_SYSTEM_HOSTS]
    if isinstance(probe_urls, str):
        values = [probe_urls]
    elif isinstance(probe_urls, (list, tuple, set)):
        values = list(probe_urls)
    else:
        values = []
    for value in values:
        rule = _final_exit_rule_for_probe_url(value)
        if rule and rule not in rules:
            rules.append(rule)
    return rules


def ensure_final_exit_probe_rules(config_yaml: str, probe_urls: Any) -> str:
    """Place every authoritative probe authority before user routing rules."""

    payload = yaml.safe_load(config_yaml)
    if not isinstance(payload, dict):
        raise ValueError("Mihomo runtime config must be a mapping")
    existing_rules = payload.get("rules")
    if not isinstance(existing_rules, list):
        existing_rules = []
    required_rules = build_final_exit_system_rules(probe_urls)
    payload["rules"] = [
        *required_rules,
        *(rule for rule in existing_rules if rule not in required_rules),
    ]
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


def _logger_or_default(logger: logging.Logger | None) -> logging.Logger:
    return logger or logging.getLogger("proxy_manager.clash")


def _primary_gateway_timeout_ms(value: Any) -> int:
    try:
        return validate_primary_gateway_max_latency_ms(value)
    except ValueError:
        return DEFAULT_PRIMARY_GATEWAY_MAX_LATENCY_MS


def _base_name(node: Node) -> str:
    if is_tier1_protocol_variant(node):
        return tier1_proxy_name(node)
    return node.name or node.remarks or f"{node.protocol}-{node.address}:{node.port}"


def _unique_name(base_name: str, seen_names: set[str]) -> str:
    candidate = base_name
    suffix = 2
    while candidate in seen_names:
        candidate = f"{base_name}-{suffix}"
        suffix += 1
    seen_names.add(candidate)
    return candidate


def _stable_proxy_name(base_name: str, proxy: dict[str, Any], seen_names: set[str]) -> str:
    """Name a proxy by its rendered definition instead of its render position.

    Positional names (``-2``, ``-3`` in render order) move between nodes when
    a member leaves or the order changes, so a Core-loaded name can come to
    mean a different server.  ``base · <digest>`` keeps one name for one
    definition: the same definition always gets the same name, a changed
    definition gets a new one, and no other member's name depends on it.
    The digest widens only on a collision.
    """

    material = json.dumps(
        {key: value for key, value in proxy.items() if key != "name"},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    base = str(base_name or "").strip() or str(proxy.get("type") or "proxy")
    # Mihomo's controller decodes a name segment twice when its escaping
    # equals Go's default (url.PathUnescape after routing), so a literal
    # "%41" addresses "A" and 404s; U+FF05 (fullwidth percent sign) keeps
    # every stable name exact.
    base = base.replace("%", "\uff05")
    for width in (6, 10, 16, 64):
        candidate = f"{base}·{digest[:width]}"
        if candidate not in seen_names:
            seen_names.add(candidate)
            return candidate
    return _unique_name(f"{base}·{digest}", seen_names)


def is_stable_proxy_name(proxy_name: str, proxy: Any) -> bool:
    """Whether ``proxy_name`` is a name _stable_proxy_name gives ``proxy``.

    Lets a reader that must show a node's own name recognise the Core's
    ``base·digest`` handle without parsing names: the name has to be one of
    the candidates this very definition produces (a positional ``base`` or
    ``base-2`` never is).
    """

    base, separator, _digest = str(proxy_name or "").rpartition("·")
    if not separator or not base or not isinstance(proxy, dict):
        return False
    seen: set[str] = set()
    # One candidate per digest width, narrowest first, as on a collision.
    return any(_stable_proxy_name(base, proxy, seen) == proxy_name for _width in range(4))


def _normalize_network(node: Node) -> str:
    network = str(node.network or "").strip().lower()
    if network in {"ws", "grpc", "h2", "http", "tcp", "xhttp"}:
        return network
    return "tcp"


def _is_tls_enabled(node: Node) -> bool:
    security = str(node.security or "").strip().lower()
    return bool(node.tls or security in {"tls", "reality"} or node.protocol == "trojan")


def _client_fingerprint(node: Node) -> str | None:
    fingerprint = node.extra.get("fp") or node.extra.get("client-fingerprint")
    return str(fingerprint) if fingerprint else None


def _is_ip_literal(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _safe_server_name(*values: Any) -> str | None:
    for value in values:
        server_name = str(value or "").strip()
        if server_name and not _is_ip_literal(server_name):
            return server_name
    return None


def _extra_value(node: Node, *keys: str) -> Any:
    for key in keys:
        if key in node.extra and node.extra[key] is not None and node.extra[key] != "":
            return node.extra[key]
    return None


def _apply_alpn_options(
    proxy: dict[str, Any], node: Node, logger: logging.Logger
) -> bool:
    raw = _extra_value(node, "alpn")
    if raw is None:
        return True
    # URI queries encode a comma-separated string; structured input already
    # has an ordered list. Mihomo requires the latter on the config boundary.
    if isinstance(raw, str):
        values = [value.strip() for value in raw.split(",") if value.strip()]
    elif isinstance(raw, (list, tuple)):
        values = list(raw)
    else:
        values = None
    if values is None or any(
        not isinstance(value, str) or not value.strip() for value in values
    ):
        log_raw_diagnostic(
            logger, "warning", "Skipping proxy with invalid ALPN configuration type"
        )
        return False
    if values:
        proxy["alpn"] = values
    return True


def _extra_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "y", "on"}


def _apply_transport_options(proxy: dict[str, Any], node: Node) -> None:
    network = _normalize_network(node)
    proxy["network"] = network
    if network == "ws":
        ws_opts: dict[str, Any] = {}
        if node.path:
            ws_opts["path"] = node.path
        if node.host:
            ws_opts["headers"] = {"Host": node.host}
        if ws_opts:
            proxy["ws-opts"] = ws_opts
        return
    if network == "grpc":
        service_name = node.extra.get("serviceName") or node.extra.get("grpc-service-name") or node.path
        if service_name:
            proxy["grpc-opts"] = {"grpc-service-name": str(service_name).lstrip("/")}
        return
    if network == "h2":
        h2_opts: dict[str, Any] = {}
        if node.host:
            h2_opts["host"] = [node.host]
        if node.path:
            h2_opts["path"] = node.path
        if h2_opts:
            proxy["h2-opts"] = h2_opts
        return
    if network == "http":
        http_opts: dict[str, Any] = {}
        if node.path:
            http_opts["path"] = [node.path]
        if node.host:
            http_opts["headers"] = {"Host": [node.host]}
        if http_opts:
            proxy["http-opts"] = http_opts
        return
    if network == "xhttp":
        xhttp_opts: dict[str, Any] = {}
        if node.path:
            xhttp_opts["path"] = node.path
        if node.host:
            xhttp_opts["host"] = node.host
        for source_key, target_key in {
            "mode": "mode",
            "headers": "headers",
            "no-grpc-header": "no-grpc-header",
            "x-padding-bytes": "x-padding-bytes",
            "sc-max-each-post-bytes": "sc-max-each-post-bytes",
        }.items():
            if source_key in node.extra:
                xhttp_opts[target_key] = node.extra[source_key]
        if xhttp_opts:
            proxy["xhttp-opts"] = xhttp_opts


def _apply_tls_options(proxy: dict[str, Any], node: Node, *, vless_or_vmess: bool) -> None:
    if not _is_tls_enabled(node):
        return
    proxy["tls"] = True
    server_name = _safe_server_name(node.sni, node.host)
    if server_name:
        key = "servername" if vless_or_vmess else "sni"
        proxy[key] = server_name
    client_fingerprint = _client_fingerprint(node)
    if client_fingerprint:
        proxy["client-fingerprint"] = client_fingerprint
    skip_cert = _extra_value(
        node,
        "skip-cert-verify",
        "skip_cert_verify",
        "allowInsecure",
        "allow_insecure",
        "insecure",
    )
    if skip_cert is not None:
        proxy["skip-cert-verify"] = _extra_bool(skip_cert)
    if str(node.security or "").strip().lower() == "reality" or node.extra.get("pbk"):
        public_key = node.extra.get("pbk")
        short_id = node.extra.get("sid")
        if public_key and short_id is not None:
            proxy["reality-opts"] = {
                "public-key": str(public_key),
                "short-id": str(short_id),
            }


def _parse_ss_plugin(plugin: str | None) -> tuple[str | None, dict[str, Any] | None]:
    if not plugin:
        return None, None
    chunks = [chunk for chunk in str(plugin).split(";") if chunk]
    if not chunks:
        return None, None
    plugin_name = chunks[0]
    plugin_opts: dict[str, Any] = {}
    for chunk in chunks[1:]:
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        normalized_key = key.replace("_", "-")
        if normalized_key in {"obfs", "mode"}:
            plugin_opts["mode"] = value
        elif normalized_key in {"obfs-host", "host"}:
            plugin_opts["host"] = value
        else:
            plugin_opts[normalized_key] = value
    return plugin_name, plugin_opts or None


def node_to_clash_proxy(
    node: Node,
    *,
    logger: logging.Logger | None = None,
    seen_names: set[str] | None = None,
) -> dict[str, Any] | None:
    logger = _logger_or_default(logger)
    seen_names = seen_names if seen_names is not None else set()

    protocol = str(node.protocol or "").strip().lower()
    if protocol not in SUPPORTED_CLASH_PROTOCOLS:
        log_raw_diagnostic(
            logger,
            "warning",
            "Skipping unsupported Clash/Mihomo protocol %s for %s",
            node.protocol,
            node.name or node.address,
        )
        return None

    proxy: dict[str, Any] = {
        "name": _unique_name(_base_name(node), seen_names),
        "type": protocol,
        "server": node.address,
        "port": int(node.port),
        "udp": True,
    }

    if protocol == "vless":
        if not node.uuid:
            log_raw_diagnostic(logger, "warning", "Skipping VLESS node %s without UUID", node.name or node.address)
            return None
        proxy["uuid"] = node.uuid
        flow = node.extra.get("flow")
        if flow:
            proxy["flow"] = flow
        _apply_transport_options(proxy, node)
        _apply_tls_options(proxy, node, vless_or_vmess=True)
        return proxy

    if protocol == "vmess":
        if not node.uuid:
            log_raw_diagnostic(logger, "warning", "Skipping VMess node %s without UUID", node.name or node.address)
            return None
        proxy["uuid"] = node.uuid
        proxy["alterId"] = int(node.extra.get("aid", 0) or 0)
        proxy["cipher"] = str(node.extra.get("scy") or node.encryption or "auto")
        _apply_transport_options(proxy, node)
        _apply_tls_options(proxy, node, vless_or_vmess=True)
        return proxy

    if protocol == "trojan":
        if not node.password:
            log_raw_diagnostic(
                logger,
                "warning",
                "Skipping Trojan node %s without password",
                node.name or node.address,
            )
            return None
        proxy["password"] = node.password
        _apply_transport_options(proxy, node)
        _apply_tls_options(proxy, node, vless_or_vmess=False)
        return proxy

    if protocol == "ss":
        cipher = normalize_shadowsocks_cipher(node.method)
        if not cipher:
            log_raw_diagnostic(
                logger,
                "warning",
                "Skipping Shadowsocks node %s without cipher",
                node.name or node.address,
            )
            return None
        credential_issue = validate_shadowsocks_credential(cipher, node.password)
        if credential_issue is not None:
            log_raw_diagnostic(
                logger,
                "warning",
                "Skipping Shadowsocks node %s with invalid cipher credential %s (%s)",
                node.name or node.address,
                cipher,
                credential_issue.code,
            )
            return None
        proxy["cipher"] = cipher
        proxy["password"] = str(node.password or "")
        plugin_name, plugin_opts = _parse_ss_plugin(node.plugin)
        if plugin_name:
            proxy["plugin"] = plugin_name
        if plugin_opts:
            proxy["plugin-opts"] = plugin_opts
        return proxy

    if protocol == "ssr":
        if not node.method or not node.password:
            log_raw_diagnostic(
                logger,
                "warning",
                "Skipping ShadowsocksR node %s without cipher/password",
                node.name or node.address,
            )
            return None
        proxy["cipher"] = node.method
        proxy["password"] = node.password
        proxy["protocol"] = str(node.extra.get("protocol") or "origin")
        proxy["obfs"] = str(node.extra.get("obfs") or node.plugin or "plain")
        protocol_param = _extra_value(node, "protocol-param", "protocol_param", "protoparam")
        obfs_param = _extra_value(node, "obfs-param", "obfs_param", "obfsparam")
        if protocol_param:
            proxy["protocol-param"] = str(protocol_param)
        if obfs_param:
            proxy["obfs-param"] = str(obfs_param)
        return proxy

    if protocol == "hysteria2":
        if not node.password:
            log_raw_diagnostic(
                logger,
                "warning",
                "Skipping Hysteria2 node %s without password",
                node.name or node.address,
            )
            return None
        proxy["password"] = node.password
        for source_key, target_key in {
            "ports": "ports",
            "hop-interval": "hop-interval",
            "up": "up",
            "down": "down",
            "obfs": "obfs",
            "obfs-password": "obfs-password",
            "fingerprint": "fingerprint",
        }.items():
            value = _extra_value(node, source_key)
            if value is not None:
                proxy[target_key] = value
        if not _apply_alpn_options(proxy, node, logger):
            return None
        server_name = _safe_server_name(node.sni)
        if server_name:
            proxy["sni"] = server_name
        if "skip-cert-verify" in node.extra:
            proxy["skip-cert-verify"] = _extra_bool(node.extra["skip-cert-verify"])
        return proxy

    if protocol == "anytls":
        if not node.password:
            log_raw_diagnostic(
                logger,
                "warning",
                "Skipping AnyTLS node %s without password",
                node.name or node.address,
            )
            return None
        proxy["password"] = node.password
        proxy["udp"] = True
        server_name = _safe_server_name(node.sni)
        if server_name:
            proxy["sni"] = server_name
        for source_key, target_key in {
            "client-fingerprint": "client-fingerprint",
            "idle-session-check-interval": "idle-session-check-interval",
            "idle-session-timeout": "idle-session-timeout",
            "min-idle-session": "min-idle-session",
        }.items():
            value = _extra_value(node, source_key)
            if value is not None:
                proxy[target_key] = value
        if not _apply_alpn_options(proxy, node, logger):
            return None
        if "skip-cert-verify" in node.extra:
            proxy["skip-cert-verify"] = _extra_bool(node.extra["skip-cert-verify"])
        return proxy

    if protocol == "tuic":
        token = _extra_value(node, "token")
        if token:
            proxy["token"] = str(token)
        else:
            if not node.uuid or not node.password:
                log_raw_diagnostic(
                    logger,
                    "warning",
                    "Skipping TUIC node %s without token or UUID/password",
                    node.name or node.address,
                )
                return None
            proxy["uuid"] = node.uuid
            proxy["password"] = node.password
        server_name = _safe_server_name(node.sni, _extra_value(node, "server_name"))
        if server_name:
            proxy["sni"] = server_name
        for source_keys, target_key in [
            (("ip",), "ip"),
            (("heartbeat-interval", "heartbeat_interval"), "heartbeat-interval"),
            (("disable-sni", "disable_sni"), "disable-sni"),
            (("reduce-rtt", "reduce_rtt"), "reduce-rtt"),
            (("request-timeout", "request_timeout"), "request-timeout"),
            (("udp-relay-mode", "udp_relay_mode"), "udp-relay-mode"),
            (("congestion-controller", "congestion_control", "congestion_controller"), "congestion-controller"),
            (("bbr-profile", "bbr_profile"), "bbr-profile"),
            (("max-udp-relay-packet-size", "max_udp_relay_packet_size"), "max-udp-relay-packet-size"),
            (("fast-open", "fast_open"), "fast-open"),
            (("max-open-streams", "max_open_streams"), "max-open-streams"),
            (("version",), "version"),
        ]:
            value = _extra_value(node, *source_keys)
            if value is not None:
                proxy[target_key] = value
        if not _apply_alpn_options(proxy, node, logger):
            return None
        if "skip-cert-verify" in node.extra:
            proxy["skip-cert-verify"] = _extra_bool(node.extra["skip-cert-verify"])
        return proxy

    if protocol == "http":
        username = node.extra.get("username")
        if username:
            proxy["username"] = str(username)
        if node.password:
            proxy["password"] = node.password
        if node.tls:
            proxy["tls"] = True
        server_name = _safe_server_name(node.sni)
        if server_name:
            proxy["sni"] = server_name
        if "skip-cert-verify" in node.extra:
            proxy["skip-cert-verify"] = _extra_bool(node.extra["skip-cert-verify"])
        if node.extra.get("fingerprint"):
            proxy["fingerprint"] = node.extra["fingerprint"]
        return proxy

    if protocol == "socks5":
        username = node.extra.get("username") or node.uuid
        if bool(username) != bool(node.password):
            log_raw_diagnostic(
                logger,
                "warning",
                "Skipping SOCKS5 node %s with incomplete username/password credentials",
                node.name or node.address,
            )
            return None
        if username:
            proxy["username"] = str(username)
        if node.password:
            proxy["password"] = node.password
        if node.tls:
            proxy["tls"] = True
        server_name = _safe_server_name(node.sni)
        if server_name:
            proxy["sni"] = server_name
        if "skip-cert-verify" in node.extra:
            proxy["skip-cert-verify"] = _extra_bool(node.extra["skip-cert-verify"])
        return proxy

    if protocol == "wireguard":
        private_key = node.password or node.extra.get("private-key") or node.extra.get("private_key")
        public_key = node.extra.get("public-key") or node.extra.get("public_key") or node.extra.get("peer-public-key")
        if not private_key or not public_key:
            log_raw_diagnostic(
                logger,
                "warning",
                "Skipping WireGuard node %s without private/public key",
                node.name or node.address,
            )
            return None
        proxy["private-key"] = str(private_key)
        proxy["public-key"] = str(public_key)
        proxy["udp"] = True
        preshared = node.extra.get("pre-shared-key") or node.extra.get("preshared-key") or node.extra.get("preshared_key")
        if preshared:
            proxy["pre-shared-key"] = str(preshared)
        allowed_ips = node.extra.get("allowed-ips") or node.extra.get("allowed_ips")
        if isinstance(allowed_ips, str):
            allowed_ips = [chunk.strip() for chunk in allowed_ips.split(",") if chunk.strip()]
        if allowed_ips:
            proxy["allowed-ips"] = list(allowed_ips)
        else:
            proxy["allowed-ips"] = ["0.0.0.0/0", "::/0"]
        ip = node.extra.get("ip") or node.extra.get("ipv4") or node.extra.get("address")
        if ip:
            proxy["ip"] = str(ip).split("/", 1)[0]
        ipv6 = node.extra.get("ipv6")
        if ipv6:
            proxy["ipv6"] = str(ipv6).split("/", 1)[0]
        reserved = node.extra.get("reserved")
        if isinstance(reserved, str):
            reserved = [chunk.strip() for chunk in reserved.split(",") if chunk.strip()]
        if isinstance(reserved, list) and reserved:
            parsed_reserved: list[Any] = []
            for chunk in reserved:
                try:
                    parsed_reserved.append(int(chunk))
                except (TypeError, ValueError):
                    parsed_reserved.append(chunk)
            proxy["reserved"] = parsed_reserved
        mtu = node.extra.get("mtu")
        if mtu is not None:
            try:
                proxy["mtu"] = int(mtu)
            except (TypeError, ValueError):
                pass
        keepalive = node.extra.get("persistent-keepalive") or node.extra.get("persistent_keepalive")
        if keepalive is not None:
            try:
                proxy["persistent-keepalive"] = int(keepalive)
            except (TypeError, ValueError):
                pass
        dns = node.extra.get("dns")
        if isinstance(dns, str):
            dns = [chunk.strip() for chunk in dns.split(",") if chunk.strip()]
        if isinstance(dns, list) and dns:
            proxy["dns"] = list(dns)
        workers = node.extra.get("workers")
        if workers is not None:
            try:
                proxy["workers"] = int(workers)
            except (TypeError, ValueError):
                pass
        # AmneziaWG obfuscation parameters; Mihomo forwards them transparently.
        for amnezia_key in ("jc", "jmin", "jmax", "s1", "s2", "h1", "h2", "h3", "h4"):
            value = node.extra.get(amnezia_key)
            if value is None:
                continue
            try:
                proxy[amnezia_key] = int(value)
            except (TypeError, ValueError):
                proxy[amnezia_key] = value
        return proxy

    return None


def build_clash_proxy_list(
    nodes: list[Node],
    *,
    logger: logging.Logger | None = None,
    seen_names: set[str] | None = None,
    rendered_pairs: list[tuple[Node, dict[str, Any]]] | None = None,
    stable_names: bool = False,
) -> list[dict[str, Any]]:
    logger = _logger_or_default(logger)
    seen_names = seen_names if seen_names is not None else set()
    proxies: list[dict[str, Any]] = []
    for node in nodes:
        if stable_names:
            proxy = node_to_clash_proxy(node, logger=logger, seen_names=set())
            if proxy:
                proxy["name"] = _stable_proxy_name(_base_name(node), proxy, seen_names)
        else:
            proxy = node_to_clash_proxy(node, logger=logger, seen_names=seen_names)
        if proxy:
            proxies.append(proxy)
            if rendered_pairs is not None:
                rendered_pairs.append((node, proxy))
    return proxies


# Replays the renderer's naming only; the render itself logged any skip.
_NAMING_REPLAY_LOGGER = logging.Logger("clash-naming-replay")
_NAMING_REPLAY_LOGGER.addHandler(logging.NullHandler())


def _rendered_exit_pairs(
    tier_nodes: dict[str, list[Node]],
    *,
    stable_tier3_names: bool,
) -> list[tuple[Node, str]]:
    seen_names: set[str] = set(MIHOMO_RESERVED_PROXY_NAMES)
    exit_pairs: list[tuple[Node, dict[str, Any]]] = []
    for tier_id in ("tier1", "tier2", "tier3", "exit"):
        build_clash_proxy_list(
            list(tier_nodes.get(tier_id, [])),
            logger=_NAMING_REPLAY_LOGGER,
            seen_names=seen_names,
            rendered_pairs=exit_pairs if tier_id == "exit" else None,
            stable_names=tier_id == "tier3" and stable_tier3_names,
        )
    return [(node, str(proxy["name"])) for node, proxy in exit_pairs]


rendered_exit_pairs = _rendered_exit_pairs

_LABEL_SUFFIX_RE = re.compile(r"(.*)-([2-9]|[1-9][0-9]+)")


def gateway_legacy_family(label: str, nodes: Iterable[Node]) -> set[str]:
    """Identities of the exit nodes an old name-only selection could mean.

    A saved label ``X`` or ``X-<n>`` names a node whose base name is ``X``:
    positional suffixes follow the export order, so every node with that base
    is in the family, whatever its status.
    """

    match = _LABEL_SUFFIX_RE.fullmatch(label)
    stem = match.group(1) if match else None
    return {
        gateway_identity(node)
        for node in nodes
        if _base_name(node) == label or (stem is not None and _base_name(node) == stem)
    }


def selected_gateway_nodes(selection: GatewaySelection, nodes: Iterable[Node]) -> list[Node]:
    """The exit nodes a saved selection names, without rendering.

    Bound: the nodes carrying the identity.  Name only: the nodes of the one
    identity in the label's family, when one of them has the label as its
    base name (a suffixed label names a render position, not a node).
    Anything else names no node.
    """

    nodes = list(nodes)
    if selection.state == "bound":
        return [node for node in nodes if gateway_identity(node) == selection.identity]
    if selection.state != "legacy":
        return []
    family = gateway_legacy_family(selection.label, nodes)
    if len(family) != 1:
        return []
    (identity,) = family
    carriers = [node for node in nodes if gateway_identity(node) == identity]
    if not any(_base_name(node) == selection.label for node in carriers):
        return []
    return carriers


def resolve_gateway_member(
    selection: GatewaySelection,
    exit_pairs: list[tuple[Node, str]],
    tier_nodes: dict[str, list[Node]],
    *,
    inventory: Iterable[Node],
    stable_tier3_names: bool,
) -> tuple[str, dict[str, Any]]:
    """The EXIT-SELECT member a saved selection names in this render.

    ``exit_pairs`` are this render's exported exit nodes and their proxy
    names, in render order.  Bound: the first exported node carrying the
    identity, else "" (never another node, never by name).  Name only (saved
    before identities, or by a name-only pick): fails closed when the label's
    family over ``inventory`` (every exit node, any status) holds more than
    one identity; otherwise the exit rendered under the label, then the one
    the other Tier3 naming renders under it, then one whose base name is the
    label.  Unresolved and invalid selections name nothing.  Returns
    ``(member, decision)``; the decision is recorded in the render receipt.
    """

    def decision(member: str = "", identity: str = "", resolution: str = "") -> tuple[str, dict[str, Any]]:
        return member, {
            "requested": selection.label,
            "key": selection.key,
            "state": selection.state,
            "member": member,
            "identity": identity,
            "resolution": resolution,
        }

    if selection.state == "none":
        return decision(resolution="none")
    if selection.state == "bound":
        for node, name in exit_pairs:  # render order: the first exported carrier
            if gateway_identity(node) == selection.identity:
                return decision(name, selection.identity, "identity")
        return decision(resolution="unmatched")
    if selection.state != "legacy":
        return decision(resolution=selection.state)
    label = selection.label
    family = gateway_legacy_family(label, [*inventory, *(node for node, _name in exit_pairs)])
    if len(family) >= 2:
        return decision(resolution="legacy_ambiguous")
    if not family:
        return decision(resolution="legacy_missing")
    candidate: tuple[Node, str] | None = None
    resolution = ""
    exact = [(node, name) for node, name in exit_pairs if name == label]
    if exact:
        candidate, resolution = exact[0], "legacy_exact"
    else:
        other = _rendered_exit_pairs(tier_nodes, stable_tier3_names=not stable_tier3_names)
        # The same nodes in the same order: the two lists pair node by node.
        for (node, now), (_same, then) in zip(exit_pairs, other):
            if then == label:
                candidate, resolution = (node, now), "legacy_cross_naming"
                break
        if candidate is None:
            based = [(node, name) for node, name in exit_pairs if _base_name(node) == label]
            if based:
                candidate, resolution = based[0], "legacy_base"
    if candidate is None:
        return decision(resolution="legacy_unconfirmed")
    identity = gateway_identity(candidate[0])
    if identity not in family:  # defensive: a candidate is always in the family
        return decision(resolution="legacy_unconfirmed")
    return decision(candidate[1], identity, resolution)


def build_clash_provider_payload(nodes: list[Node], *, logger: logging.Logger | None = None) -> dict[str, Any]:
    return {"proxies": build_clash_proxy_list(nodes, logger=logger)}


def dump_clash_yaml(payload: dict[str, Any]) -> str:
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


def _clash_config_payload(value: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    loaded = yaml.safe_load(str(value or ""))
    if not isinstance(loaded, dict):
        raise ValueError("mihomo configuration must be a mapping")
    return loaded


def build_capture_disabled_clash_config(
    config: str | dict[str, Any],
    *,
    runtime_source_listeners: Iterable[RuntimeSourceListenerSpec] | None = None,
    runtime_source_egress_proxy: dict[str, Any] | None = None,
) -> str:
    """Return the same route graph and DNS policy without host traffic capture.

    Private source listeners share the Core's resolver with ordinary traffic.
    In particular, direct-nameserver is global to every DIRECT outbound, so a
    source-only startup must not inject a temporary override. System-proxy
    activation can retain this configuration without reloading the Core.
    """

    payload = copy.deepcopy(_clash_config_payload(config))
    payload.pop("tun", None)
    payload.pop("sniffer", None)
    payload.pop("redir-port", None)
    payload.pop("tproxy-port", None)
    payload["allow-lan"] = False
    groups = payload.get("proxy-groups")
    if isinstance(groups, list):
        for group in groups:
            if not isinstance(group, dict):
                continue
            # Mihomo versions before 1.19.27 hard-coded COMPATIBLE (a
            # DIRECT-equivalent outbound) while a group was empty or still
            # initializing.  The packaged Core supports empty-fallback, so
            # every generated group explicitly fails closed during startup
            # and after hot reloads.
            group["empty-fallback"] = "REJECT"
            group_type = str(group.get("type") or "").strip().lower()
            if group_type in {"fallback", "url-test", "url_test", "urltest"}:
                # No-capture startup has no ordinary traffic to lazily trigger
                # group initialization.  Eager checks let the controller
                # readiness gate observe a real proxy or REJECT selection
                # before the mixed-port is exposed to source collection.
                group["lazy"] = False
    if runtime_source_listeners is not None:
        _apply_runtime_source_listeners(
            payload,
            runtime_source_listeners,
            egress_proxy=runtime_source_egress_proxy,
        )
    return dump_clash_yaml(payload)


def _bootstrap_egress_proxy(
    egress_proxy: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(egress_proxy, dict) or not coerce_bool(
        egress_proxy.get("enabled", False),
        False,
    ):
        return None
    use_for = egress_proxy.get("use_for")
    if isinstance(use_for, dict) and not coerce_bool(
        use_for.get("collection", True),
        True,
    ):
        return None

    scheme = str(egress_proxy.get("scheme") or "http").strip().lower()
    server = str(
        egress_proxy.get("server") or egress_proxy.get("host") or ""
    ).strip()
    try:
        port = int(egress_proxy.get("port"))
    except (TypeError, ValueError) as exc:
        raise ValueError("bootstrap egress proxy port is invalid") from exc
    if not server or not 1 <= port <= 65535:
        raise ValueError("bootstrap egress proxy server or port is invalid")

    if scheme in {"http", "https"}:
        proxy_type = "http"
    elif scheme in {"socks", "socks5", "socks5h"}:
        proxy_type = "socks5"
    else:
        raise ValueError(
            "bootstrap egress proxy scheme must be http, https, or socks5"
        )

    proxy: dict[str, Any] = {
        "name": "BOOTSTRAP-EGRESS-PROXY",
        "type": proxy_type,
        "server": server,
        "port": port,
    }
    username = str(egress_proxy.get("username") or "")
    password = str(egress_proxy.get("password") or "")
    if username:
        proxy["username"] = username
    if password:
        proxy["password"] = password
    if scheme == "https":
        proxy["tls"] = True
    return proxy


def _runtime_source_egress_proxy(
    egress_proxy: dict[str, Any] | None,
) -> dict[str, Any] | None:
    proxy = _bootstrap_egress_proxy(egress_proxy)
    if proxy is None:
        return None
    proxy["name"] = RUNTIME_SOURCE_EGRESS_PROXY_NAME
    return proxy


def _apply_runtime_source_listeners(
    payload: dict[str, Any],
    listeners: Iterable[RuntimeSourceListenerSpec],
    *,
    egress_proxy: dict[str, Any] | None = None,
) -> None:
    """Replace only Tesseract's private listeners in a runtime payload."""

    specs = tuple(listeners)
    if any(not isinstance(spec, RuntimeSourceListenerSpec) for spec in specs):
        raise TypeError("runtime source listeners must be RuntimeSourceListenerSpec values")
    names = [spec.name for spec in specs]
    if len(set(names)) != len(names):
        raise ValueError("runtime source listener names must be unique")

    proxies = payload.get("proxies")
    if not isinstance(proxies, list):
        proxies = []
        payload["proxies"] = proxies
    proxies[:] = [
        proxy
        for proxy in proxies
        if not (
            isinstance(proxy, dict)
            and str(proxy.get("name") or "") == RUNTIME_SOURCE_EGRESS_PROXY_NAME
        )
    ]
    requires_egress = any(
        spec.proxy == RUNTIME_SOURCE_EGRESS_PROXY_NAME for spec in specs
    )
    if requires_egress:
        explicit_egress = _runtime_source_egress_proxy(egress_proxy)
        if explicit_egress is None:
            raise ValueError(
                "runtime source egress listener requires an enabled collection egress proxy"
            )
        proxies.append(explicit_egress)

    available_targets = {
        "DIRECT",
        "REJECT",
        "REJECT-DROP",
        "PASS",
        "COMPATIBLE",
        *(str(proxy.get("name") or "") for proxy in proxies if isinstance(proxy, dict)),
        *(
            str(group.get("name") or "")
            for group in payload.get("proxy-groups", [])
            if isinstance(group, dict)
        ),
    }
    missing_targets = sorted(
        {spec.proxy for spec in specs if spec.proxy not in available_targets}
    )
    if missing_targets:
        raise ValueError(
            "runtime source listener proxy target is unavailable: "
            + ", ".join(missing_targets)
        )

    existing = payload.get("listeners")
    retained = [
        listener
        for listener in existing if isinstance(listener, dict)
        and str(listener.get("name") or "") not in RUNTIME_SOURCE_LISTENER_NAMES
    ] if isinstance(existing, list) else []
    compiled = [spec.to_mihomo_listener() for spec in specs]
    if retained or compiled:
        payload["listeners"] = [*retained, *compiled]
    else:
        payload.pop("listeners", None)


def inject_runtime_source_listeners(
    config: str | dict[str, Any],
    listeners: Iterable[RuntimeSourceListenerSpec],
    *,
    egress_proxy: dict[str, Any] | None = None,
) -> str:
    """Inject private runtime-only listeners without changing ordinary routes."""

    payload = copy.deepcopy(_clash_config_payload(config))
    _apply_runtime_source_listeners(payload, listeners, egress_proxy=egress_proxy)
    return dump_clash_yaml(payload)


def _apply_bootstrap_geodata_listener(
    payload: dict[str, Any],
    listener: Mapping[str, Any],
) -> bool:
    """Give the first-run GeoData download its own way through the pool.

    PROXY-FALLBACK hands every connection to the first pool member that has
    not failed a health check, and a bootstrap Core has usually not run one
    yet.  A first member that answers probes but carries bulk data slowly
    then holds the whole download (11-06: 92 s for 16 MB while fifteen other
    members were idle).  This listener is bound to a load-balance group over
    the same members, so every connection -- each asset, each retry -- leaves
    through the next one.  The group inherits TIER3-AUTO's members and health
    contract: it adds no probe of its own and, like every group of this
    config, falls back to REJECT, never DIRECT.

    Returns False, leaving the payload untouched, when there is no pool group.
    """

    groups = payload.get("proxy-groups")
    if not isinstance(groups, list):
        return False
    pool = next(
        (
            group
            for group in groups
            if isinstance(group, dict) and str(group.get("name") or "") == "TIER3-AUTO"
        ),
        None,
    )
    if pool is None:
        return False
    try:
        port = int(listener.get("port"))
    except (TypeError, ValueError) as exc:
        raise ValueError("bootstrap GeoData listener port is invalid") from exc
    username = str(listener.get("username") or "")
    password = str(listener.get("password") or "")
    if not 1 <= port <= 65535 or not username or not password:
        raise ValueError("bootstrap GeoData listener needs a port and credentials")

    group: dict[str, Any] = {
        "name": BOOTSTRAP_GEODATA_GROUP_NAME,
        "type": "load-balance",
        "strategy": "round-robin",
        "empty-fallback": "REJECT",
    }
    for key in _BOOTSTRAP_GEODATA_INHERITED_KEYS:
        if key in pool:
            group[key] = copy.deepcopy(pool[key])
    groups[:] = [
        entry
        for entry in groups
        if not (
            isinstance(entry, dict)
            and str(entry.get("name") or "") == BOOTSTRAP_GEODATA_GROUP_NAME
        )
    ]
    groups.append(group)

    existing = payload.get("listeners")
    listeners = [
        entry
        for entry in existing
        if not (
            isinstance(entry, dict)
            and str(entry.get("name") or "") == BOOTSTRAP_GEODATA_LISTENER_NAME
        )
    ] if isinstance(existing, list) else []
    listeners.append(
        {
            "name": BOOTSTRAP_GEODATA_LISTENER_NAME,
            "type": "http",
            "listen": "127.0.0.1",
            "port": port,
            "proxy": BOOTSTRAP_GEODATA_GROUP_NAME,
            "users": [{"username": username, "password": password}],
        }
    )
    payload["listeners"] = listeners
    return True


def resolved_client_dns_block(dns_config: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the ``dns`` block the formal renderer emits for this client policy.

    ``None`` only when the user explicitly disabled DNS; an absent block
    resolves to the shipped defaults exactly as :func:`build_full_clash_config`.
    """

    dns_block = _build_dns_block(dns_config)
    dns_explicitly_disabled = (
        isinstance(dns_config, dict) and not coerce_bool(dns_config.get("enable"), True)
    )
    if dns_block is None and not dns_explicitly_disabled:
        dns_block = _build_dns_block({"enable": True})
    return dns_block


def build_bootstrap_clash_config(
    config: str | dict[str, Any],
    *,
    egress_proxy: dict[str, Any] | None = None,
    runtime_source_listeners: Iterable[RuntimeSourceListenerSpec] | None = None,
    runtime_source_egress_proxy: dict[str, Any] | None = None,
    defer_geodata_rules: bool = False,
    geodata_listener: Mapping[str, Any] | None = None,
) -> str:
    """Build a no-capture, fail-closed config for pre-floor source supply.

    The caller supplies the normal rendered config so bootstrap and formal
    operation share one rule compiler, one ordered route graph and one DNS
    block: the resolver rung is decided once per runtime run and rendered
    into the formal payload, because cold start is ordinary expansion with an
    empty pool.  The only extra outbound is the explicitly authorised
    collection proxy, placed last in PROXY-FALLBACK.

    ``defer_geodata_rules`` drops GEOIP/GEOSITE rules while the GeoData files
    are not on disk yet (a first run).  Mihomo would otherwise fetch them at
    load time over the host's default route, and Tesseract fetches GeoData
    only through a proxy -- which a first run has only after this bootstrap
    Core has supplied the Bastion Layer pool.  The bootstrap captures no host
    traffic, so no user traffic is routed by the shortened list; the formal
    Core always loads the full rule list once GeoData has been downloaded.

    ``geodata_listener`` (``port``, ``username``, ``password``) adds the
    listener that download goes through, see
    :func:`_apply_bootstrap_geodata_listener`.
    """

    payload = _clash_config_payload(build_capture_disabled_clash_config(config))
    if defer_geodata_rules and isinstance(payload.get("rules"), list):
        payload["rules"] = [
            rule
            for rule in payload["rules"]
            if not runtime_config_text_uses_geodata(str(rule))
        ]
    dns = payload.get("dns")
    if isinstance(dns, dict) and dns.get("respect-rules"):
        # Every proxy outbound is stripped below; a rule-routed DNS query
        # would otherwise wait on REJECT.  Formal DNS policy is unchanged.
        dns["respect-rules"] = False
    groups = payload.get("proxy-groups")
    if not isinstance(groups, list):
        raise ValueError("mihomo bootstrap configuration has no proxy groups")

    for group in groups:
        if not isinstance(group, dict):
            continue
        proxies = group.get("proxies")
        if not isinstance(proxies, list):
            continue
        filtered = [
            str(name)
            for name in proxies
            if str(name).strip().upper() not in UNAUTHORIZED_DIRECT_POLICIES
        ]
        group["proxies"] = filtered or ["REJECT"]

    explicit_egress = _bootstrap_egress_proxy(egress_proxy)
    if explicit_egress is not None:
        proxies = payload.get("proxies")
        if not isinstance(proxies, list):
            proxies = []
            payload["proxies"] = proxies
        proxies[:] = [
            proxy
            for proxy in proxies
            if not (
                isinstance(proxy, dict)
                and str(proxy.get("name") or "")
                == "BOOTSTRAP-EGRESS-PROXY"
            )
        ]
        proxies.append(explicit_egress)

        fallback = next(
            (
                group
                for group in groups
                if isinstance(group, dict)
                and str(group.get("name") or "") == "PROXY-FALLBACK"
            ),
            None,
        )
        if fallback is None:
            raise ValueError("mihomo bootstrap configuration has no PROXY-FALLBACK")
        candidates = fallback.get("proxies")
        candidates = list(candidates) if isinstance(candidates, list) else []
        candidates = [
            str(name)
            for name in candidates
            if str(name) not in {"REJECT", "BOOTSTRAP-EGRESS-PROXY"}
        ]
        candidates.append("BOOTSTRAP-EGRESS-PROXY")
        fallback["proxies"] = candidates

    if runtime_source_listeners is not None:
        _apply_runtime_source_listeners(
            payload,
            runtime_source_listeners,
            egress_proxy=(
                runtime_source_egress_proxy
                if runtime_source_egress_proxy is not None
                else egress_proxy
            ),
        )

    # Gate-before-READY keeps ordinary FINAL-EXIT at REJECT. Only the
    # authenticated source listener may borrow the already loaded proxy pool.
    for listener in payload.get("listeners", []):
        if (
            isinstance(listener, dict)
            and listener.get("name") in RUNTIME_SOURCE_LISTENER_NAMES
            and listener.get("proxy") == "FINAL-EXIT"
        ):
            listener["proxy"] = RUNTIME_SOURCE_BOOTSTRAP_POOL_PROXY

    if geodata_listener is not None:
        _apply_bootstrap_geodata_listener(payload, geodata_listener)

    return dump_clash_yaml(payload)


def build_clash_provider_yaml(nodes: list[Node], *, logger: logging.Logger | None = None) -> str:
    return dump_clash_yaml(build_clash_provider_payload(nodes, logger=logger))


def _as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = value.splitlines()
    elif isinstance(value, list):
        values = value
    else:
        values = [value]
    return [str(item).strip() for item in values if str(item).strip()]


def _routing_rules_config(routing_config: dict[str, Any] | None) -> dict[str, Any]:
    return routing_rule_mapping(routing_config)


def _targeted_rule(entry: str, target: str) -> str | None:
    value = entry.strip()
    if not value or value.startswith("#"):
        return None
    lowered = value.lower()
    prefix_map = {
        "domain:": "DOMAIN",
        "suffix:": "DOMAIN-SUFFIX",
        "keyword:": "DOMAIN-KEYWORD",
        "geosite:": "GEOSITE",
        "geoip:": "GEOIP",
        "ip-cidr:": "IP-CIDR",
        "ip-cidr6:": "IP-CIDR6",
        "process:": "PROCESS-NAME",
    }
    for prefix, rule_type in prefix_map.items():
        if lowered.startswith(prefix):
            body = value[len(prefix) :].strip()
            if not body:
                return None
            if rule_type == "GEOIP":
                return f"{rule_type},{body},{target},no-resolve"
            return f"{rule_type},{body},{target}"

    if "," in value:
        parts = [part.strip() for part in value.split(",") if part.strip()]
        if len(parts) < 2:
            return None
        rule_type = parts[0].upper()
        if rule_type == "GEOIP" and len(parts) >= 3 and parts[2].lower() == "no-resolve":
            return f"{parts[0]},{parts[1]},{target},no-resolve"
        if len(parts) >= 3:
            return ",".join([parts[0], parts[1], target, *parts[3:]])
        return ",".join([*parts, target])

    try:
        network = ipaddress.ip_network(value, strict=False)
    except ValueError:
        network = None
    if network is not None:
        rule_type = "IP-CIDR" if network.version == 4 else "IP-CIDR6"
        return f"{rule_type},{network.with_prefixlen},{target},no-resolve"

    if value.startswith("+."):
        value = value[2:]
    elif value.startswith("*."):
        value = value[2:]
    return f"DOMAIN-SUFFIX,{value},{target}"


def _rule_targets_disallowed_policy(rule: str) -> bool:
    parts = [part.strip().upper() for part in str(rule).split(",") if part.strip()]
    if len(parts) < 2:
        return False
    if parts[0] == "MATCH":
        policy_index = 1
    elif parts[0] in {"AND", "OR", "NOT", "SUB-RULE"}:
        policy_index = len(parts) - 1
    else:
        policy_index = 2
    return (
        len(parts) > policy_index
        and parts[policy_index] in DISALLOWED_ROUTING_POLICIES
    )


def _rule_set_entries(entries: Any) -> list[tuple[str, bool]]:
    """``routing.rules.rule_sets`` as (rule, dropped): a rule set naming a policy
    the lists may not use (DIRECT …) is dropped by the legacy compile."""

    rules: list[tuple[str, bool]] = []
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, str):
            parts = [part.strip() for part in entry.split(",") if part.strip()]
            if len(parts) == 2:
                rule = f"RULE-SET,{parts[0]},{parts[1]}"
                rules.append((rule, _rule_targets_disallowed_policy(rule)))
            elif len(parts) >= 3 and parts[0].upper() == "RULE-SET":
                rule = ",".join(parts)
                rules.append((rule, _rule_targets_disallowed_policy(rule)))
            continue
        if not isinstance(entry, dict):
            continue
        provider = str(entry.get("provider") or entry.get("name") or "").strip()
        target_key = str(entry.get("target") or entry.get("policy") or "proxy").strip().lower()
        target = ROUTING_RULE_TARGETS.get(target_key, str(entry.get("target") or entry.get("policy") or "GENERAL"))
        if provider:
            rules.append((f"RULE-SET,{provider},{target}", str(target).strip().upper() in DISALLOWED_ROUTING_POLICIES))
    return rules


def _rule_set_rules(entries: Any) -> list[str]:
    return [rule for rule, dropped in _rule_set_entries(entries) if not dropped]


def legacy_routing_rule_entries(routing_config: dict[str, Any] | None) -> list[tuple[str, bool]]:
    """The legacy rule layout in compile order, as (rule, dropped).

    The direct / proxy / reject lists in ``routing.rule_order`` (default
    direct, proxy, reject), then rule_sets, then raw. A ``dropped`` rule is one
    the legacy compile skips (a raw rule or rule set naming DIRECT or
    COMPATIBLE, a raw line that is not a rule); the others compile exactly as
    listed, each once. routing_rules.legacy_rule_list reads the same entries.
    """

    rules_config = _routing_rules_config(routing_config)
    entries: list[tuple[str, bool]] = []
    seen: set[str] = set()

    def add(rule: str | None, dropped: bool = False) -> None:
        if not rule:
            return
        if dropped:
            entries.append((rule, True))
        elif rule not in seen:
            seen.add(rule)
            entries.append((rule, False))

    order = normalize_routing_rule_order(
        routing_config.get("rule_order") if isinstance(routing_config, dict) else None
    )
    for bucket in order:
        for entry in _as_string_list(rules_config.get(bucket)):
            add(_targeted_rule(entry, ROUTING_RULE_TARGETS[bucket]))
    for rule, dropped in _rule_set_entries(rules_config.get("rule_sets")):
        add(rule, dropped)
    for entry in _as_string_list(rules_config.get("raw")):
        if entry.startswith("#"):
            continue
        add(entry, "," not in entry or _rule_targets_disallowed_policy(entry))
    return entries


def build_custom_routing_rules(routing_config: dict[str, Any] | None) -> list[str]:
    """The user's rules in match order: ``routing.rules.list`` when the
    configuration holds one (routing_rules), else the legacy layout."""

    stored = stored_rule_list(routing_config)
    if stored is not None:
        return compile_rule_list(stored)
    return [rule for rule, dropped in legacy_routing_rule_entries(routing_config) if not dropped]


def build_rule_providers(routing_config: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(routing_config, dict):
        return {}
    providers = routing_config.get("rule_providers") or routing_config.get("rule-providers")
    if not isinstance(providers, dict):
        return {}
    normalized: dict[str, Any] = {}
    for name, provider in providers.items():
        if not isinstance(provider, dict):
            normalized[str(name)] = provider
            continue
        entry = dict(provider)
        if str(entry.get("type") or "").strip().lower() == "http":
            requested_proxy = str(entry.get("proxy") or "").strip()
            if not requested_proxy or requested_proxy.upper() in DISALLOWED_ROUTING_POLICIES:
                entry["proxy"] = "FINAL-EXIT"
        normalized[str(name)] = entry
    return normalized


def _build_tun_block(tun_config: dict[str, Any] | None, *, enabled: bool) -> dict[str, Any] | None:
    if not enabled:
        return None
    tun_config = tun_config if isinstance(tun_config, dict) else {}
    dns_hijack = tun_config.get("dns_hijack") or tun_config.get("dns-hijack") or ["any:53"]
    if not isinstance(dns_hijack, list):
        dns_hijack = [str(dns_hijack)]
    block: dict[str, Any] = {
        "enable": True,
        "stack": str(tun_config.get("stack", "mixed")),
        "device": str(tun_config.get("device", "Tesseract")),
        "auto-route": coerce_bool(tun_config.get("auto_route", tun_config.get("auto-route", True)), True),
        "auto-detect-interface": coerce_bool(
            tun_config.get("auto_detect_interface", tun_config.get("auto-detect-interface", True)),
            True,
        ),
        "strict-route": coerce_bool(tun_config.get("strict_route", tun_config.get("strict-route", False)), False),
        "dns-hijack": [str(entry) for entry in dns_hijack if entry],
        "mtu": int(tun_config.get("mtu", 9000)),
    }
    return block


def _build_dns_block(dns_config: dict[str, Any] | None) -> dict[str, Any] | None:
    """Render ``client.dns`` for the requested ``mode``.

    ``custom`` renders the user's lists verbatim.  Every other mode renders a
    region-neutral static default (an IP-literal encrypted pool, or the system
    resolver); the runtime then replaces the lists with the rung it qualified
    for the current network.
    """

    if not isinstance(dns_config, dict):
        return None
    if not coerce_bool(dns_config.get("enable", True), True):
        return None
    mode = normalize_dns_mode(dns_config)
    static_lists = static_dns_lists(mode)
    if static_lists is None:
        neutral = static_dns_lists(DNS_MODE_AUTO) or {}
        nameserver = dns_config_list(dns_config, "nameserver") or list(neutral["nameserver"])
        # Mihomo rejects hostnames and empty lists here; ``system`` is legal.
        default_ns = dns_config_list(dns_config, "default_nameserver") or ["system"]
        proxy_server_ns = dns_config_list(dns_config, "proxy_server_nameserver") or list(nameserver)
        fallback = dns_config_list(dns_config, "fallback")
        direct_ns = dns_config_list(dns_config, "direct_nameserver")
        nameserver_policy = (
            dns_config.get("nameserver_policy")
            if dns_config.get("nameserver_policy") is not None
            else dns_config.get("nameserver-policy")
        )
    else:
        nameserver = static_lists["nameserver"]
        default_ns = static_lists["default-nameserver"]
        proxy_server_ns = static_lists["proxy-server-nameserver"]
        fallback = []
        direct_ns = []
        nameserver_policy = None
    fake_ip_filter = (
        dns_config.get("fake_ip_filter")
        or dns_config.get("fake-ip-filter")
        or list(DEFAULT_FAKE_IP_FILTER)
    )
    enhanced_mode = str(dns_config.get("enhanced_mode") or dns_config.get("enhanced-mode") or "fake-ip")
    block: dict[str, Any] = {
        "enable": True,
        "listen": str(dns_config.get("listen", "127.0.0.1:1053")),
        "ipv6": coerce_bool(dns_config.get("ipv6", False), False),
        "enhanced-mode": enhanced_mode,
        "respect-rules": coerce_bool(
            dns_config.get("respect_rules", dns_config.get("respect-rules")),
            False,
        ),
        "default-nameserver": [str(entry) for entry in default_ns if entry],
        "nameserver": [str(entry) for entry in nameserver if entry],
        "proxy-server-nameserver": [str(entry) for entry in proxy_server_ns if entry],
    }
    if enhanced_mode == "fake-ip":
        block["fake-ip-range"] = str(dns_config.get("fake_ip_range") or dns_config.get("fake-ip-range") or "198.18.0.1/16")
        block["fake-ip-filter"] = [str(entry) for entry in fake_ip_filter if entry]
    if fallback:
        # No region-shaped fallback filter: Mihomo's own arbitration applies.
        block["fallback"] = [str(entry) for entry in fallback if entry]
    if direct_ns:
        block["direct-nameserver"] = [str(entry) for entry in direct_ns if entry]
        block["direct-nameserver-follow-policy"] = coerce_bool(
            dns_config.get(
                "direct_nameserver_follow_policy",
                dns_config.get("direct-nameserver-follow-policy"),
            ),
            False,
        )
    if isinstance(nameserver_policy, dict) and nameserver_policy:
        block["nameserver-policy"] = {
            str(key): (
                [str(item) for item in value if str(item or "").strip()]
                if isinstance(value, (list, tuple))
                else str(value)
            )
            for key, value in nameserver_policy.items()
            if str(key or "").strip()
        }
    return block


DEFAULT_GEOX_URL = {
    "geoip": "https://testingcf.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@release/geoip.dat",
    "geosite": "https://testingcf.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@release/geosite.dat",
    "mmdb": "https://testingcf.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@release/country.mmdb",
}


def _normalize_geox_url(value: dict[str, Any] | None) -> dict[str, str]:
    merged: dict[str, str] = {
        "geoip": DEFAULT_GEOX_URL["geoip"],
        "geosite": DEFAULT_GEOX_URL["geosite"],
    }
    if isinstance(value, dict):
        for key in ("geoip", "geosite", "mmdb", "asn"):
            raw = value.get(key) or value.get(key.replace("_", "-"))
            if raw:
                merged[key] = str(raw).strip()
    return merged


def build_full_clash_config(
    tier_nodes: dict[str, list[Node]],
    *,
    logger: logging.Logger | None = None,
    probe_url: str = "https://www.gstatic.com/generate_204",
    probe_urls: Any = None,
    probe_interval: int = 300,
    external_controller: str | None = "127.0.0.1:9090",
    secret: str | None = "",
    network_mode: str = "system_proxy",
    tun_config: dict[str, Any] | None = None,
    dns_config: dict[str, Any] | None = None,
    routing_config: dict[str, Any] | None = None,
    ipv6: bool = False,
    allow_lan: bool = False,
    mixed_port: int = 7890,
    geox_url: dict[str, Any] | None = None,
    active_exit_proxy: str | None = None,
    primary_gateway_max_latency_ms: int = DEFAULT_PRIMARY_GATEWAY_MAX_LATENCY_MS,
    runtime_source_listeners: Iterable[RuntimeSourceListenerSpec] | None = None,
    runtime_source_egress_proxy: dict[str, Any] | None = None,
    render_receipt: dict[str, Any] | None = None,
    tier3_health_check_url: str | None = None,
    stable_tier3_names: bool = False,
    active_exit_selection: GatewaySelection | None = None,
    exit_inventory: Iterable[Node] | None = None,
) -> str:
    logger = _logger_or_default(logger)
    probe_interval = max(30, int(probe_interval))
    primary_gateway_timeout_ms = _primary_gateway_timeout_ms(primary_gateway_max_latency_ms)
    # User traffic over the free pool is chosen by TIER3-AUTO's own URLTest
    # (Mihomo keys member liveness per URL), so that group proves a real site
    # rather than the neutral probe_url.  Only the group's health contract
    # changes: this URL is not an authoritative probe, so it gets no
    # FINAL-EXIT system rule and the user's routing rules for its host stand.
    tier3_health_url = (
        str(tier3_health_check_url or "").strip() or DEFAULT_ROUTE_HEALTH_CHECK_URL
    )
    tier3_health_interval = min(
        probe_interval,
        TIER3_ROUTE_HEALTH_CHECK_MAX_INTERVAL_SECONDS,
    )
    authoritative_probe_urls = [probe_url]
    if isinstance(probe_urls, str):
        authoritative_probe_urls.append(probe_urls)
    elif isinstance(probe_urls, (list, tuple, set)):
        authoritative_probe_urls.extend(probe_urls)
    normalized_geox_url = _normalize_geox_url(geox_url)
    system_route_urls = [
        *authoritative_probe_urls,
        *normalized_geox_url.values(),
    ]
    seen_names: set[str] = set(MIHOMO_RESERVED_PROXY_NAMES)
    rendered_pairs: dict[str, list[tuple[Node, dict[str, Any]]]] = {
        "tier1": [],
        "tier2": [],
        "tier3": [],
        "exit": [],
    }
    tier1_proxies = build_clash_proxy_list(
        tier_nodes.get("tier1", []),
        logger=logger,
        seen_names=seen_names,
        rendered_pairs=rendered_pairs["tier1"],
    )
    tier2_proxies = build_clash_proxy_list(
        tier_nodes.get("tier2", []),
        logger=logger,
        seen_names=seen_names,
        rendered_pairs=rendered_pairs["tier2"],
    )
    tier3_proxies = build_clash_proxy_list(
        tier_nodes.get("tier3", []),
        logger=logger,
        seen_names=seen_names,
        rendered_pairs=rendered_pairs["tier3"],
        # Tier3 members are the online-mutable provider inventory: their
        # names must not depend on which other members exist or their order.
        stable_names=stable_tier3_names,
    )
    exit_proxies = build_clash_proxy_list(
        tier_nodes.get("exit", []),
        logger=logger,
        seen_names=seen_names,
        rendered_pairs=rendered_pairs["exit"],
    )
    for proxy in exit_proxies:
        proxy["dialer-proxy"] = "PROXY-FALLBACK"
    all_proxies = [*tier1_proxies, *tier2_proxies, *tier3_proxies, *exit_proxies]
    if render_receipt is not None:
        render_receipt.clear()
        render_receipt["proxies"] = tuple(
            {
                "tier": tier_id,
                # Freeze the identity source together with the rendered proxy.
                # Manager health/provider updates may mutate the live Node as
                # soon as rendering returns; a receipt must keep describing
                # the exact input accepted by this render transaction.
                "node": node.clone(),
                # Copy only after every renderer-owned transformation (for
                # example the Gateway dialer-proxy) has been applied.
                "proxy": copy.deepcopy(proxy),
            }
            for tier_id in ("tier1", "tier2", "tier3", "exit")
            for node, proxy in rendered_pairs[tier_id]
        )

    def names_or_reject(proxies: list[dict[str, Any]]) -> list[str]:
        # Keep every generated group non-empty without allowing Mihomo to fall
        # back to its direct-compatible empty-group policy.
        return [proxy["name"] for proxy in proxies] or ["REJECT"]

    tier1_groups: list[dict[str, Any]] = []
    tier1_entry_groups: list[str] = []
    if tier1_proxies:
        proxy_names = {proxy["name"] for proxy in tier1_proxies}
        for physical_name, nodes in group_tier1_nodes(tier_nodes.get("tier1", [])):
            protocol_names = [tier1_proxy_name(node) for node in nodes if tier1_proxy_name(node) in proxy_names]
            if not protocol_names:
                continue
            auto_name = tier1_auto_group_name(physical_name)
            select_name = tier1_select_group_name(physical_name)
            tier1_groups.extend(
                [
                    {
                        "name": auto_name,
                        "type": "fallback",
                        "empty-fallback": "REJECT",
                        "proxies": protocol_names,
                        "url": probe_url,
                        "interval": probe_interval,
                        "lazy": True,
                        "timeout": primary_gateway_timeout_ms,
                        "expected-status": 204,
                    },
                    {
                        "name": select_name,
                        "type": "select",
                        "empty-fallback": "REJECT",
                        "proxies": [auto_name, *protocol_names],
                    },
                ]
            )
            tier1_entry_groups.append(select_name)
    tier1_fallback_proxies = tier1_entry_groups or names_or_reject(tier1_proxies)
    if isinstance(active_exit_selection, GatewaySelection):
        selection = active_exit_selection
    else:
        active_exit = str(active_exit_proxy or "").strip()
        selection = GatewaySelection("legacy", active_exit) if active_exit else GatewaySelection("none")
    # The saved selection names a node: this render names its member (the
    # identity over the exported exits, never another node), and the decision
    # is recorded so readers of the loaded route never replay the naming.
    active_exit_name, exit_decision = resolve_gateway_member(
        selection,
        [(node, str(proxy["name"])) for node, proxy in rendered_pairs["exit"]],
        tier_nodes,
        inventory=exit_inventory if exit_inventory is not None else tier_nodes.get("exit", []),
        stable_tier3_names=stable_tier3_names,
    )
    active_exit_requested = selection.active
    if render_receipt is not None and active_exit_requested:
        render_receipt["exit_selection"] = exit_decision
    proxy_fallback_proxies: list[str] = []
    if tier1_proxies:
        proxy_fallback_proxies.append("TIER1-SELECT")
    if tier2_proxies:
        proxy_fallback_proxies.append("TIER2-AUTO")
    if tier3_proxies:
        proxy_fallback_proxies.append("TIER3-AUTO")
    if not proxy_fallback_proxies:
        proxy_fallback_proxies.append("REJECT")
    tier3_is_primary_fallback = bool(tier3_proxies) and not tier1_proxies and not tier2_proxies
    tier3_healthcheck_lazy = not tier3_is_primary_fallback
    exit_groups = []
    if active_exit_requested:
        # A selected fixed exit is a fail-closed policy, not permission to
        # fall back to the ordinary tier chain when that exit is currently
        # absent from the ALIVE/SLOW runtime export.
        exit_select_proxy_names = [active_exit_name] if active_exit_name else ["REJECT"]
        exit_groups.extend(
            [
                {
                    "name": "EXIT-SELECT",
                    "type": "select",
                    "empty-fallback": "REJECT",
                    "proxies": exit_select_proxy_names,
                },
            ]
        )
    gateway_protected = active_exit_requested
    # REJECT must be an actual selectable member, not merely empty-fallback:
    # the runtime recovery coordinator uses it to fail closed without stopping
    # Core when no verified route remains.  The application UI does not expose
    # FINAL-EXIT's internal members.
    final_exit_proxies = [
        "EXIT-SELECT" if gateway_protected else "PROXY-FALLBACK",
        "REJECT",
    ]
    general_proxies = ["FINAL-EXIT"]
    global_proxies = ["GENERAL", "FINAL-EXIT"]

    payload = {
        "mixed-port": int(mixed_port),
        "external-controller": external_controller,
        "allow-lan": coerce_bool(allow_lan, False),
        "mode": "rule",
        "log-level": "info",
        "ipv6": coerce_bool(ipv6, False),
        "geodata-mode": True,
        "geox-url": normalized_geox_url,
        "proxies": all_proxies or [],
        "proxy-groups": [
            *tier1_groups,
            {
                "name": "TIER1-AUTO",
                "type": "fallback",
                "empty-fallback": "REJECT",
                "proxies": tier1_fallback_proxies,
                "url": probe_url,
                "interval": probe_interval,
                "lazy": True,
                "timeout": primary_gateway_timeout_ms,
                "expected-status": 204,
            },
            {
                "name": "TIER1-SELECT",
                "type": "select",
                "empty-fallback": "REJECT",
                "proxies": ["TIER1-AUTO", *tier1_fallback_proxies],
            },
            {
                "name": "TIER2-AUTO",
                "type": "fallback",
                "empty-fallback": "REJECT",
                "proxies": names_or_reject(tier2_proxies),
                "url": probe_url,
                "interval": probe_interval,
                "lazy": True,
                "timeout": primary_gateway_timeout_ms,
                "expected-status": 204,
            },
            {
                # When no member passes the real-site check Mihomo's fallback
                # still returns its first loaded member (REJECT is never a
                # leading member and never alive), exactly as it did when no
                # member passed the neutral URL; runtime recovery reserves the
                # FINAL-EXIT REJECT member for a failed fixed Gateway only.
                "name": "TIER3-AUTO",
                "type": "fallback",
                "empty-fallback": "REJECT",
                "proxies": names_or_reject(tier3_proxies),
                "url": tier3_health_url,
                "interval": tier3_health_interval,
                "lazy": tier3_healthcheck_lazy,
                "timeout": 5000,
                "expected-status": 204,
            },
            {
                "name": "PROXY-FALLBACK",
                "type": "fallback",
                "empty-fallback": "REJECT",
                "proxies": proxy_fallback_proxies,
                "url": probe_url,
                "interval": probe_interval,
                "lazy": tier3_healthcheck_lazy,
                "timeout": 5000,
                "expected-status": 204,
            },
            *exit_groups,
            {
                "name": "FINAL-EXIT",
                "type": "select",
                "empty-fallback": "REJECT",
                "proxies": final_exit_proxies,
            },
            {
                "name": "GENERAL",
                "type": "select",
                "empty-fallback": "REJECT",
                "proxies": general_proxies,
            },
            {
                "name": "GLOBAL",
                "type": "select",
                "empty-fallback": "REJECT",
                "proxies": global_proxies,
            },
        ],
        "rules": [
            *build_final_exit_system_rules(system_route_urls),
            *LOCAL_NETWORK_SYSTEM_RULES,
            *build_custom_routing_rules(routing_config),
            "MATCH,GENERAL",
        ],
    }
    rule_providers = build_rule_providers(routing_config)
    # Core can refresh native fallback health after a short real dial-error
    # burst, independently of the scheduled Manager checks. The loaded member
    # order, health-check interval and per-tier latency contract stay intact.
    for group in payload["proxy-groups"]:
        if group.get("type") == "fallback":
            group["max-failed-times"] = 2
    if rule_providers:
        payload["rule-providers"] = rule_providers
    if secret:
        payload["secret"] = secret
    if not external_controller:
        payload.pop("external-controller", None)

    normalized_mode = str(network_mode or "system_proxy").lower().replace("-", "_")
    tun_enabled = normalized_mode in {"tun", "mixed"}
    tun_block = _build_tun_block(tun_config, enabled=tun_enabled)
    if tun_block is not None:
        payload["tun"] = tun_block
    dns_block = _build_dns_block(dns_config)
    # DNS is always needed: even in system_proxy mode mihomo must resolve
    # proxy-server hostnames (CDN-fronted nodes use domains, not IPs).
    # Without an explicit DNS block, mihomo falls back to its built-in
    # resolver chain that may be unreachable in the current network.
    # However, if the user *explicitly* set enable=False, honour that choice.
    dns_explicitly_disabled = (
        isinstance(dns_config, dict) and not coerce_bool(dns_config.get("enable"), True)
    )
    if dns_block is None and not dns_explicitly_disabled:
        dns_block = _build_dns_block({"enable": True})
    if dns_block is not None:
        payload["dns"] = dns_block
    if tun_enabled:
        # TUN needs sniffing for TLS SNI based rules to work on fake-ip flows.
        payload["sniffer"] = {
            "enable": True,
            "sniff": {"TLS": {"ports": [443, 8443]}, "HTTP": {"ports": [80, 8080, 8880]}},
            "skip-domain": ["Mijia Cloud"],
        }
    if runtime_source_listeners is not None:
        _apply_runtime_source_listeners(
            payload,
            runtime_source_listeners,
            egress_proxy=runtime_source_egress_proxy,
        )
    return dump_clash_yaml(payload)
