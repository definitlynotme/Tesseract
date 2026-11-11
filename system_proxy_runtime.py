from __future__ import annotations

import re
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address
from typing import Any

from config_utils import coerce_bool, coerce_port, coerce_positive_int
from runtime_settings import client_system_proxy_block


DEFAULT_SYSTEM_PROXY_BYPASS = ["localhost", "127.*", "<local>"]
SYSTEM_PROXY_CAPTURE_MODES = {"system_proxy", "mixed"}

_ALLOWED_IPV4_BYPASS_NETWORKS = (
    IPv4Network("10.0.0.0/8"),
    IPv4Network("127.0.0.0/8"),
    IPv4Network("169.254.0.0/16"),
    IPv4Network("172.16.0.0/12"),
    IPv4Network("192.168.0.0/16"),
)
_ALLOWED_IPV6_BYPASS_NETWORKS = (
    IPv6Network("::1/128"),
    IPv6Network("fc00::/7"),
    IPv6Network("fe80::/10"),
)
_SINGLE_LABEL_HOST_PATTERN = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
    re.IGNORECASE,
)
_LOCAL_BYPASS_SUFFIX_PATTERNS = {"*.local", "*.lan", "*.home.arpa"}
_LOCAL_BYPASS_SUFFIX_ROOTS = {"home.arpa"}
_LOCAL_BYPASS_DOMAIN_SUFFIXES = (".local", ".lan", ".home.arpa")


@dataclass(frozen=True)
class SystemProxyRuntimeConfig:
    raw_config: dict[str, Any]
    enabled: bool
    host: str
    port: int
    bypass: list[str]


@dataclass(frozen=True)
class EffectiveEgressState:
    ok: bool | None
    reason: str = ""


@dataclass(frozen=True)
class HealthFailureProgress:
    failure_count: int
    restart_due: bool


@dataclass(frozen=True)
class SystemProxyCapturePlan:
    wanted: bool
    probe_target_active: bool


def _system_proxy_bypass_network_allowed(
    value: IPv4Address | IPv4Network | IPv6Address | IPv6Network,
) -> bool:
    allowed_networks = (
        _ALLOWED_IPV4_BYPASS_NETWORKS
        if value.version == 4
        else _ALLOWED_IPV6_BYPASS_NETWORKS
    )
    if isinstance(value, (IPv4Address, IPv6Address)):
        return any(value in network for network in allowed_networks)
    return any(value.subnet_of(network) for network in allowed_networks)


def _normalize_ipv4_bypass_wildcard(value: str) -> str | None:
    parts = value.split(".")
    if "*" not in parts or not 2 <= len(parts) <= 4:
        return None
    wildcard_index = parts.index("*")
    if wildcard_index == 0 or any(part != "*" for part in parts[wildcard_index:]):
        return None
    fixed_octets: list[int] = []
    for part in parts[:wildcard_index]:
        if not part.isascii() or not part.isdecimal():
            return None
        octet = int(part)
        if not 0 <= octet <= 255:
            return None
        fixed_octets.append(octet)
    network_address = ".".join(
        [*(str(octet) for octet in fixed_octets), *("0" for _ in range(4 - len(fixed_octets)))]
    )
    network = IPv4Network(f"{network_address}/{len(fixed_octets) * 8}")
    if not _system_proxy_bypass_network_allowed(network):
        return None
    return ".".join([*(str(octet) for octet in fixed_octets), "*"])


def _normalize_single_label_host(value: str) -> str | None:
    normalized = value.casefold()
    try:
        ascii_label = normalized.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if not _SINGLE_LABEL_HOST_PATTERN.fullmatch(ascii_label):
        return None
    if ascii_label.isdecimal():
        return None
    return normalized


def _normalize_local_domain(value: str) -> str | None:
    normalized = value.casefold()
    try:
        ascii_domain = normalized.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if len(ascii_domain) > 253 or not any(
        ascii_domain.endswith(suffix) for suffix in _LOCAL_BYPASS_DOMAIN_SUFFIXES
    ):
        return None
    if not all(
        _SINGLE_LABEL_HOST_PATTERN.fullmatch(label)
        for label in ascii_domain.split(".")
    ):
        return None
    return normalized


def normalize_system_proxy_bypass_entry(value: Any) -> str | None:
    """Return a safe Windows proxy override entry, or ``None`` when disallowed."""

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or any(separator in text for separator in (";", "\r", "\n", "%")):
        return None

    lowered = text.casefold()
    if lowered == "localhost":
        return "localhost"
    if lowered == "<local>":
        return "<local>"
    if lowered in _LOCAL_BYPASS_SUFFIX_PATTERNS or lowered in _LOCAL_BYPASS_SUFFIX_ROOTS:
        return lowered
    if "*" in text:
        return _normalize_ipv4_bypass_wildcard(text)
    # Windows ProxyOverride documents host/IP patterns, not CIDR ranges.
    if "/" in text:
        return None

    bracketed_ipv6 = text.startswith("[") and text.endswith("]")
    address_text = text[1:-1] if bracketed_ipv6 else text
    try:
        parsed = ip_address(address_text)
    except ValueError:
        return _normalize_local_domain(text) or _normalize_single_label_host(text)
    if bracketed_ipv6 and not isinstance(parsed, IPv6Address):
        return None
    if not _system_proxy_bypass_network_allowed(parsed):
        return None
    if bracketed_ipv6:
        return f"[{parsed}]"
    return str(parsed)


def normalize_system_proxy_bypass(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_values = [
            part.strip()
            for line in value.splitlines()
            for part in line.split(";")
        ]
    elif isinstance(value, (list, tuple, set)):
        raw_values = list(value)
    else:
        raw_values = list(DEFAULT_SYSTEM_PROXY_BYPASS)
    entries: list[str] = []
    for item in raw_values:
        normalized = normalize_system_proxy_bypass_entry(item)
        if normalized and normalized not in entries:
            entries.append(normalized)
    return entries


def client_mixed_port(client_config: dict[str, Any] | None) -> int:
    client = client_config if isinstance(client_config, dict) else {}
    system_proxy = client_system_proxy_block(client)
    return coerce_port(client.get("mihomo_mixed_port", system_proxy.get("port", 7890)), 7890)


def system_proxy_runtime_config(client_config: dict[str, Any] | None) -> SystemProxyRuntimeConfig:
    client = client_config if isinstance(client_config, dict) else {}
    proxy_config = client_system_proxy_block(client)
    return SystemProxyRuntimeConfig(
        raw_config=proxy_config,
        enabled=coerce_bool(proxy_config.get("enabled", True), True),
        host=str(proxy_config.get("host", "127.0.0.1")),
        port=client_mixed_port(client),
        bypass=normalize_system_proxy_bypass(proxy_config.get("bypass", DEFAULT_SYSTEM_PROXY_BYPASS)),
    )


def system_proxy_capture_wanted(network_mode: str, system_proxy_enabled: bool) -> bool:
    return str(network_mode or "") in SYSTEM_PROXY_CAPTURE_MODES and bool(system_proxy_enabled)


def tun_mode_core_health_monitor_active(
    *,
    network_mode: str,
    system_proxy_active: bool,
) -> bool:
    return str(network_mode or "") == "tun" and not bool(system_proxy_active)


def system_proxy_capture_plan(
    *,
    network_mode: str,
    system_proxy_enabled: bool,
    active: bool,
) -> SystemProxyCapturePlan:
    wanted = system_proxy_capture_wanted(network_mode, system_proxy_enabled)
    return SystemProxyCapturePlan(wanted=wanted, probe_target_active=bool(wanted and not active))


def system_proxy_capture_active(
    *,
    network_mode: str,
    system_proxy_enabled: bool,
    active: bool,
    target_active: bool,
) -> bool:
    return bool(active) or (
        system_proxy_capture_wanted(network_mode, system_proxy_enabled) and bool(target_active)
    )


def effective_egress_state(
    *,
    running: bool,
    core_present: bool,
    core_running: bool,
    capture_wanted: bool,
    capture_active: bool,
) -> EffectiveEgressState:
    if not running:
        return EffectiveEgressState(None, "")
    if not core_present:
        return EffectiveEgressState(False, "mihomo core is not running")
    if capture_wanted and not capture_active:
        return EffectiveEgressState(False, "system proxy capture is inactive")
    if not core_running:
        return EffectiveEgressState(False, "mihomo core is not running")
    return EffectiveEgressState(True, "")


def missing_core_egress_state(*, running: bool) -> EffectiveEgressState:
    return effective_egress_state(
        running=running,
        core_present=False,
        core_running=False,
        capture_wanted=False,
        capture_active=False,
    )


def health_failure_restart_threshold(system_proxy_config: dict[str, Any] | None) -> int:
    proxy_config = system_proxy_config if isinstance(system_proxy_config, dict) else {}
    return coerce_positive_int(proxy_config.get("health_failures_before_restore", 3), 3)


def health_failure_restart_due(
    *,
    failure_count: int,
    system_proxy_config: dict[str, Any] | None,
) -> bool:
    return int(failure_count) >= health_failure_restart_threshold(system_proxy_config)


def health_failure_progress(
    *,
    failure_count: int,
    system_proxy_config: dict[str, Any] | None,
) -> HealthFailureProgress:
    next_failure_count = failure_count + 1
    return HealthFailureProgress(
        failure_count=next_failure_count,
        restart_due=health_failure_restart_due(
            failure_count=next_failure_count,
            system_proxy_config=system_proxy_config,
        ),
    )


def should_attempt_core_restart(*, running: bool, stopping: bool) -> bool:
    return bool(running and not stopping)
