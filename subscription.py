from __future__ import annotations

import base64
import hmac
import ipaddress
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

from clash_formatter import build_clash_provider_yaml, build_full_clash_config
from config_utils import coerce_bool
from gateway_selection import selection_from_manager
from log_event_reports import log_raw_diagnostic
from manager import ProxyTierManager
from mihomo_api import mihomo_controller_address, mihomo_controller_enabled, mihomo_controller_secret
from models import Node
from runtime_settings import (
    client_dns_block,
    client_mihomo_controller_block,
    client_tun_block,
    default_probe_urls,
    geo_update_block,
    primary_gateway_max_latency_ms_from_testing,
    route_health_check_url,
    routing_block,
    subscription_server_block,
    tier_block,
)
from subscription_server_config import subscription_probe_interval, subscription_tier3_limit
from system_proxy_runtime import client_mixed_port


def _is_loopback_host(host: str) -> bool:
    normalized = str(host or "").strip().lower()
    if normalized in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _extract_bearer_token(header_value: str | None) -> str:
    value = str(header_value or "").strip()
    prefix = "bearer "
    if value.lower().startswith(prefix):
        return value[len(prefix) :].strip()
    return ""


def _request_header_token(headers: Any) -> str:
    return headers.get("X-Tesseract-Token") or _extract_bearer_token(headers.get("Authorization"))


def _request_authorized(required_token: str, parsed: Any, headers: Any) -> bool:
    if not required_token:
        return True
    query_token = parse_qs(parsed.query).get("token", [""])[0]
    supplied = str(query_token or _request_header_token(headers) or "")
    return hmac.compare_digest(supplied, required_token)


def encode_subscription(uris: list[str]) -> str:
    payload = "\n".join(uri for uri in uris if uri).strip()
    return base64.b64encode(payload.encode("utf-8")).decode("utf-8")


def _uri_host(address: str) -> str:
    host = str(address or "")
    if ":" in host and not (host.startswith("[") and host.endswith("]")):
        return f"[{host}]"
    return host


def _node_label(node: Node) -> str:
    return quote(str(node.name or node.remarks or node.address or "node"), safe="")


def _vless_subscription_uri(node: Node, host: str, port: int) -> str:
    query: dict[str, str] = {"encryption": str(node.encryption or "none")}
    for key, value in {
        "security": node.security,
        "type": node.network,
        "host": node.host,
        "path": node.path,
        "sni": node.sni,
        "flow": node.extra.get("flow"),
        "pbk": node.extra.get("pbk"),
        "sid": node.extra.get("sid"),
        "fp": node.extra.get("fp") or node.extra.get("client-fingerprint") or node.extra.get("client_fingerprint"),
        "mode": node.extra.get("mode"),
    }.items():
        if value:
            query[key] = str(value)
    return f"vless://{quote(str(node.uuid), safe='')}@{host}:{port}?{urlencode(query)}#{_node_label(node)}"


def _hysteria2_subscription_uri(node: Node, host: str, port: int) -> str:
    query = {}
    if node.sni:
        query["sni"] = str(node.sni)
    for key in ["obfs", "obfs-password", "obfs_password", "up", "down"]:
        if node.extra.get(key):
            query[key] = str(node.extra[key])
    if coerce_bool(node.extra.get("skip-cert-verify"), False) or coerce_bool(node.extra.get("insecure"), False):
        query["insecure"] = "1"
    suffix = f"?{urlencode(query)}" if query else ""
    return f"hysteria2://{quote(str(node.password), safe='')}@{host}:{port}{suffix}#{_node_label(node)}"


def _anytls_subscription_uri(node: Node, host: str, port: int) -> str:
    query = {}
    if node.sni:
        query["sni"] = str(node.sni)
    if coerce_bool(node.extra.get("skip-cert-verify"), False) or coerce_bool(node.extra.get("insecure"), False):
        query["insecure"] = "1"
    suffix = f"?{urlencode(query)}" if query else ""
    return f"anytls://{quote(str(node.password), safe='')}@{host}:{port}{suffix}#{_node_label(node)}"


def _http_subscription_uri(node: Node, host: str, port: int) -> str:
    scheme = "https" if node.tls else "http"
    username = str(node.extra.get("username") or "")
    auth = f"{quote(username, safe='')}:{quote(str(node.password), safe='')}@" if username else f"{quote(str(node.password), safe='')}@"
    return f"{scheme}://{auth}{host}:{port}#{_node_label(node)}"


def _node_subscription_uri(node: Node) -> str:
    if node.uri:
        return node.uri
    host = _uri_host(node.address)
    port = int(node.port or 0)
    if not host or port <= 0:
        return ""

    if node.protocol == "vless" and node.uuid:
        return _vless_subscription_uri(node, host, port)
    if node.protocol == "hysteria2" and node.password:
        return _hysteria2_subscription_uri(node, host, port)
    if node.protocol == "anytls" and node.password:
        return _anytls_subscription_uri(node, host, port)
    if node.protocol == "http" and node.password:
        return _http_subscription_uri(node, host, port)

    return ""


def _subscription_nodes(manager: ProxyTierManager, tier_id: str, limits: dict[str, Any] | None = None):
    limits = limits or {}
    if tier_id == "tier3":
        return manager.get_nodes_for_subscription("tier3", limit=subscription_tier3_limit(limits))
    return manager.get_nodes_for_subscription(tier_id)


DNS_OVERRIDE_LIST_KEYS = {
    "nameserver": ("nameserver",),
    "default_nameserver": ("default_nameserver", "default-nameserver"),
    "proxy_server_nameserver": ("proxy_server_nameserver", "proxy-server-nameserver"),
    "fallback": ("fallback",),
    "fake_ip_filter": ("fake_ip_filter", "fake-ip-filter"),
}


def _dns_entries(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_entries = value.replace(",", "\n").splitlines()
    elif isinstance(value, (list, tuple, set)):
        raw_entries = value
    else:
        raw_entries = []
    entries: list[str] = []
    for entry in raw_entries:
        text = str(entry or "").strip()
        if text and text not in entries:
            entries.append(text)
    return entries


def _dns_value_from_aliases(payload: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    for alias in aliases:
        if alias in payload:
            return payload.get(alias)
    return None


def _merge_dns_override(base: dict[str, Any], override: Any) -> dict[str, Any]:
    if not isinstance(override, dict):
        return dict(base)
    merged = dict(base)
    consumed: set[str] = set()
    for canonical, aliases in DNS_OVERRIDE_LIST_KEYS.items():
        override_entries = _dns_entries(_dns_value_from_aliases(override, aliases))
        consumed.update(alias for alias in aliases if alias in override)
        if not override_entries:
            continue
        existing_entries: list[str] = []
        for alias in aliases:
            existing_entries.extend(_dns_entries(merged.pop(alias, [])))
        combined = list(existing_entries)
        for entry in override_entries:
            if entry not in combined:
                combined.append(entry)
        merged[canonical] = combined
    for key, value in override.items():
        if key in consumed:
            continue
        normalized = key.replace("-", "_")
        merged[normalized] = value
    return merged


def _scoped_dns_config(manager: ProxyTierManager, tier_nodes: dict[str, list[Node]]) -> dict[str, Any]:
    dns_config = dict(client_dns_block(manager.client_config))
    for tier_id in ("tier1", "tier2", "tier3", "exit"):
        nodes = tier_nodes.get(tier_id) or []
        if not nodes:
            continue
        dns_config = _merge_dns_override(dns_config, tier_block(manager.config, tier_id).get("dns"))
        if tier_id != "tier1":
            continue
        for node in nodes:
            node_dns = node.extra.get("dns") if isinstance(node.extra, dict) else None
            if isinstance(node_dns, dict):
                dns_config = _merge_dns_override(dns_config, node_dns)
    return dns_config


def _mihomo_probe_urls(manager: ProxyTierManager) -> list[str]:
    """Return the complete probe sequence shared with DesktopRuntime.

    Runtime probe iteration is configured from the manager's complete config,
    so subscription rendering must use the same source rather than selecting
    only the first helper URL from the already-derived testing block.
    """

    config = manager.config if isinstance(manager.config, dict) else {}
    return default_probe_urls(config)


PLAIN_SUBSCRIPTION_ROUTES = {"/sub/tier1", "/sub/tier2", "/sub/tier3", "/sub/exit", "/sub/all"}
CLASH_PROVIDER_ROUTES = {"/clash/tier1", "/clash/tier2", "/clash/tier3", "/clash/exit", "/clash/all"}


def _route_tier_id(normalized_route: str) -> str:
    return normalized_route.split("/")[-1]


def _render_plain_subscription(
    manager: ProxyTierManager,
    tier_id: str,
    limits: dict[str, Any],
) -> tuple[str, str]:
    nodes = _subscription_nodes(manager, tier_id, limits)
    uris = [_node_subscription_uri(node) for node in nodes]
    missing = [node.name or node.address for node, uri in zip(nodes, uris) if not uri]
    if missing:
        log_raw_diagnostic(
            manager.logger,
            "warning",
            "Plain subscription skipped %s nodes without URI export support: %s",
            len(missing),
            ", ".join(missing[:5]),
        )
    return "text/plain; charset=utf-8", encode_subscription(uris)


def _render_clash_provider(
    manager: ProxyTierManager,
    tier_id: str,
    limits: dict[str, Any],
) -> tuple[str, str]:
    nodes = _subscription_nodes(manager, tier_id, limits)
    return "application/yaml; charset=utf-8", build_clash_provider_yaml(nodes, logger=manager.logger)


def _render_clash_config(
    manager: ProxyTierManager,
    limits: dict[str, Any],
    *,
    render_receipt: dict[str, Any] | None = None,
) -> tuple[str, str]:
    subscription_config = subscription_server_block(manager.config)
    tier3_limit = subscription_tier3_limit(limits, subscription_config)
    tier_nodes = {
        "tier1": manager.get_nodes_for_subscription("tier1"),
        "tier2": manager.get_nodes_for_subscription("tier2"),
        "tier3": manager.get_nodes_for_subscription("tier3", limit=tier3_limit),
        "exit": manager.get_nodes_for_subscription("exit"),
    }
    probe_urls = _mihomo_probe_urls(manager)
    probe_url = probe_urls[0]
    probe_interval = subscription_probe_interval(subscription_config, manager.testing_config)
    # TIER3-AUTO's real-site health URL comes from the same complete config
    # as the probe URLs; it is a group contract, not an authoritative probe.
    tier3_health_check_url = route_health_check_url(
        manager.config if isinstance(manager.config, dict) else {}
    )
    controller = client_mihomo_controller_block(manager.client_config)
    network_mode = str(manager.client_config.get("network_mode", "system_proxy"))
    tun_config = client_tun_block(manager.client_config)
    dns_config = _scoped_dns_config(manager, tier_nodes)
    ipv6_enabled = coerce_bool(manager.client_config.get("ipv6", dns_config.get("ipv6", False)), False)
    if ipv6_enabled and "ipv6" not in dns_config:
        dns_config["ipv6"] = True
    routing_config = routing_block(manager.config)
    allow_lan = coerce_bool(manager.client_config.get("allow_lan", manager.client_config.get("allow-lan", False)), False)
    mixed_port = client_mixed_port(manager.client_config)
    geo_update_config = geo_update_block(manager.config)
    geox_url = geo_update_config.get("geox_url") or geo_update_config.get("geox-url") or None
    # The saved Gateway names a node; every exit node (any status) shows which
    # other Gateways an old name-only selection could also mean.
    selection = selection_from_manager(manager)
    inventory_getter = getattr(manager, "gateway_exit_inventory", None)
    exit_inventory = inventory_getter() if callable(inventory_getter) else None
    if not isinstance(exit_inventory, (list, tuple)):
        exit_inventory = None
    return "application/yaml; charset=utf-8", build_full_clash_config(
        tier_nodes,
        logger=manager.logger,
        probe_url=probe_url,
        probe_urls=probe_urls,
        probe_interval=probe_interval,
        external_controller=mihomo_controller_address(controller) if mihomo_controller_enabled(controller) else None,
        secret=mihomo_controller_secret(controller),
        network_mode=network_mode,
        tun_config=tun_config,
        dns_config=dns_config,
        routing_config=routing_config,
        ipv6=ipv6_enabled,
        allow_lan=allow_lan,
        mixed_port=mixed_port,
        geox_url=geox_url,
        active_exit_proxy=selection.label or None,
        active_exit_selection=selection,
        exit_inventory=exit_inventory,
        primary_gateway_max_latency_ms=primary_gateway_max_latency_ms_from_testing(manager.testing_config),
        render_receipt=render_receipt,
        tier3_health_check_url=tier3_health_check_url,
        # Only the desktop runtime's own export view sets this (verified
        # provider writes); the subscription server keeps positional names.
        stable_tier3_names=bool(getattr(manager, "stable_tier3_names", False)),
    )


def render_subscription_route(
    manager: ProxyTierManager,
    route: str,
    limits: dict[str, Any] | None = None,
    *,
    render_receipt: dict[str, Any] | None = None,
) -> tuple[str, str]:
    normalized_route = route.rstrip("/")
    limits = limits or {}
    if normalized_route in PLAIN_SUBSCRIPTION_ROUTES:
        return _render_plain_subscription(manager, _route_tier_id(normalized_route), limits)

    if normalized_route in CLASH_PROVIDER_ROUTES:
        return _render_clash_provider(manager, _route_tier_id(normalized_route), limits)

    if normalized_route == "/clash/config":
        return _render_clash_config(
            manager,
            limits,
            render_receipt=render_receipt,
        )

    raise KeyError(route)


class SubscriptionServer:
    def __init__(
        self,
        manager: ProxyTierManager,
        host: str,
        port: int,
        limits: dict[str, Any] | None = None,
        *,
        token: str = "",
        cors_allowed_origins: list[str] | None = None,
    ) -> None:
        self.manager = manager
        self.host = host
        self.port = int(port)
        self.limits = limits or {}
        self.token = str(token or "").strip()
        self.cors_allowed_origins = [str(origin).strip() for origin in cors_allowed_origins or [] if str(origin).strip()]
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self._serve_started = threading.Event()

    def _handler_factory(self):
        manager = self.manager
        limits = self.limits
        token = self.token
        allowed_origins = set(self.cors_allowed_origins)

        class Handler(BaseHTTPRequestHandler):
            def _send_cors_headers(self) -> None:
                origin = self.headers.get("Origin")
                if origin and origin in allowed_origins:
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")
                    self.send_header("Access-Control-Allow-Headers", "Authorization, X-Tesseract-Token")
                    self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")

            def _authorized(self, parsed) -> bool:
                return _request_authorized(token, parsed, self.headers)

            def _send_text_response(self, status: int, body: bytes, *, content_type: str | None = None) -> None:
                self.send_response(status)
                self._send_cors_headers()
                if content_type:
                    self.send_header("Content-Type", content_type)
                self.end_headers()
                self.wfile.write(body)

            def _send_subscription_response(self, content_type: str, payload: str) -> None:
                body = payload.encode("utf-8")
                self.send_response(200)
                self._send_cors_headers()
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self) -> None:
                self.send_response(204)
                self._send_cors_headers()
                self.end_headers()

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                route = parsed.path.rstrip("/")
                if not self._authorized(parsed):
                    self._send_text_response(
                        401,
                        b"unauthorized",
                        content_type="text/plain; charset=utf-8",
                    )
                    return
                try:
                    content_type, payload = render_subscription_route(manager, route, limits)
                except KeyError:
                    self._send_text_response(404, b"not found")
                    return
                self._send_subscription_response(content_type, payload)

            def log_message(self, format: str, *args) -> None:
                return

        return Handler

    def start(self) -> None:
        if not self.token and not _is_loopback_host(self.host):
            raise ValueError("subscription_server.token is required when host is not loopback")
        self.httpd = ThreadingHTTPServer((self.host, self.port), self._handler_factory())
        self.httpd.daemon_threads = True

        def serve() -> None:
            self._serve_started.set()
            assert self.httpd is not None
            self.httpd.serve_forever(poll_interval=0.1)

        self._serve_started.clear()
        self.thread = threading.Thread(target=serve, name="subscription-server", daemon=True)
        self.thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + max(0.0, float(timeout))
        httpd = self.httpd
        thread = self.thread
        closed = False
        if httpd:
            if thread and thread.is_alive():
                if not self._serve_started.wait(
                    timeout=max(0.0, deadline - time.monotonic())
                ):
                    return
                httpd.shutdown()
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
                httpd.server_close()
                closed = True
            else:
                httpd.server_close()
                closed = True
        if closed:
            self.httpd = None
            self.thread = None
            self._serve_started.clear()
