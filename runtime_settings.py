from __future__ import annotations

from dataclasses import dataclass
from os import PathLike
from typing import Any
from urllib.parse import urlsplit

from config_utils import coerce_float, coerce_positive_int
from tier3_defaults import (
    DEFAULT_TIER3_STARTUP_EXPORT_CANDIDATE_LIMIT,
    DEFAULT_TIER3_STARTUP_VALIDATION_CONCURRENCY,
)


VALID_NETWORK_MODES = {"system_proxy", "tun", "mixed"}
DEFAULT_PROBE_URLS = ("https://cp.cloudflare.com", "https://www.gstatic.com/generate_204")
# Real-site HTTPS-204 contract for the group that carries user traffic over the
# Tier3 pool (TIER3-AUTO).  Many free nodes answer the neutral 204 endpoints
# above yet fail TLS to ordinary sites, so the route health check must prove a
# real site instead.  Override with testing.mihomo_helper.route_health_check_url.
DEFAULT_ROUTE_HEALTH_CHECK_URL = "https://www.google.com/generate_204"
DEFAULT_MIHOMO_BINARY_PATH = "bin/mihomo.exe"
DEFAULT_MIHOMO_RUNTIME_CONFIG_PATH = "data/mihomo-runtime.yaml"
DEFAULT_MIHOMO_WORK_DIR = "data/mihomo"
DEFAULT_PRIMARY_GATEWAY_MAX_LATENCY_MS = 800
MIN_PRIMARY_GATEWAY_MAX_LATENCY_MS = 100
MAX_PRIMARY_GATEWAY_MAX_LATENCY_MS = 5000


@dataclass(frozen=True)
class MihomoClientPaths:
    binary_path: str
    runtime_config_path: str
    work_dir: str


def _dict_value(source: dict[str, Any], key: str) -> dict[str, Any]:
    value = source.get(key, {}) if isinstance(source, dict) else {}
    return value if isinstance(value, dict) else {}


def normalize_network_mode(value: Any) -> str:
    mode = str(value or "system_proxy").lower().replace("-", "_")
    if mode not in VALID_NETWORK_MODES:
        return "system_proxy"
    return mode


def testing_block(config: dict[str, Any]) -> dict[str, Any]:
    return _dict_value(config, "testing")


def paths_block(config: dict[str, Any] | None) -> dict[str, Any]:
    return _dict_value(config or {}, "paths")


def alerts_block(config: dict[str, Any] | None) -> dict[str, Any]:
    return _dict_value(config or {}, "alerts")


def tiers_block(config: dict[str, Any] | None) -> dict[str, Any]:
    return _dict_value(config or {}, "tiers")


def tier_block(config: dict[str, Any] | None, tier_id: str) -> dict[str, Any]:
    return _dict_value(tiers_block(config), tier_id)


def verified_tier3_provider_writes(config: dict[str, Any] | None) -> bool:
    """``tiers.tier3.verified_provider_writes`` (default on; 11-02).

    On: Tier3B provider writes keep the live pick first, verify members
    explicitly afterwards, and Tier3 member names follow their definition.
    Off: the pre-11-02 provider lane and positional names.  Two fixes apply
    either way: Tier3 Core probes use the renderer's own names (reserved
    seed, Tier1/Tier2 first), and a saved Gateway name from the other Tier3
    naming resolves to the same Gateway.  The runtime reads this once per
    start (DesktopRuntime._tier3_verified_provider_writes_enabled).
    """

    value = tier_block(config, "tier3").get("verified_provider_writes", True)
    if isinstance(value, str):
        return value.strip().casefold() not in {"", "0", "false", "no", "off"}
    return bool(value)


def killswitch_block(config: dict[str, Any] | None) -> dict[str, Any]:
    return _dict_value(config or {}, "killswitch")


def geo_update_block(config: dict[str, Any] | None) -> dict[str, Any]:
    return _dict_value(config or {}, "geo_update")


def egress_proxy_block(config: dict[str, Any] | None) -> dict[str, Any]:
    return _dict_value(config or {}, "egress_proxy")


def subscription_server_block(config: dict[str, Any] | None) -> dict[str, Any]:
    return _dict_value(config or {}, "subscription_server")


def routing_block(config: dict[str, Any] | None) -> dict[str, Any]:
    return _dict_value(config or {}, "routing")


def client_config_block(config: dict[str, Any] | None) -> dict[str, Any]:
    return _dict_value(config or {}, "client")


def client_subblock(client_config: Any, key: str) -> dict[str, Any]:
    return _dict_value(client_config if isinstance(client_config, dict) else {}, key)


def client_dns_block(client_config: Any) -> dict[str, Any]:
    return client_subblock(client_config, "dns")


def client_mihomo_controller_block(client_config: Any) -> dict[str, Any]:
    return client_subblock(client_config, "mihomo_controller")


def client_system_proxy_block(client_config: Any) -> dict[str, Any]:
    return client_subblock(client_config, "system_proxy")


def client_tun_block(client_config: Any) -> dict[str, Any]:
    return client_subblock(client_config, "tun")


def client_path_value(client_config: Any, key: str, default: str) -> str:
    client = client_config if isinstance(client_config, dict) else {}
    value = client.get(key)
    if isinstance(value, (str, PathLike)):
        text = str(value).strip()
        return text or default
    return default


def mihomo_client_paths(client_config: Any) -> MihomoClientPaths:
    return MihomoClientPaths(
        binary_path=client_path_value(client_config, "mihomo_binary_path", DEFAULT_MIHOMO_BINARY_PATH),
        runtime_config_path=client_path_value(
            client_config,
            "mihomo_runtime_config",
            DEFAULT_MIHOMO_RUNTIME_CONFIG_PATH,
        ),
        work_dir=client_path_value(client_config, "mihomo_work_dir", DEFAULT_MIHOMO_WORK_DIR),
    )


def mihomo_controller_block(config: dict[str, Any] | None) -> dict[str, Any]:
    return client_mihomo_controller_block(client_config_block(config))


def system_proxy_block(config: dict[str, Any] | None) -> dict[str, Any]:
    return client_system_proxy_block(client_config_block(config))


def _mihomo_helper_config(config: dict[str, Any]) -> dict[str, Any]:
    return _dict_value(testing_block(config), "mihomo_helper")


def _tier3_config(config: dict[str, Any]) -> dict[str, Any]:
    return tier_block(config, "tier3")


def as_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_items = value.replace(",", "\n").splitlines()
    elif isinstance(value, (list, tuple)):
        raw_items = list(value)
    else:
        raw_items = []
    items: list[str] = []
    for item in raw_items:
        text = str(item or "").strip()
        if text and text not in items:
            items.append(text)
    return items


def as_config_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    return []


def default_probe_urls(config: dict[str, Any]) -> list[str]:
    testing = testing_block(config)
    mihomo_helper = _mihomo_helper_config(config)
    urls: list[str] = []
    for value in (
        mihomo_helper.get("probe_urls"),
        testing.get("probe_urls"),
        mihomo_helper.get("probe_url"),
        testing.get("probe_url"),
        list(DEFAULT_PROBE_URLS),
    ):
        for url in as_string_list(value):
            if url not in urls:
                urls.append(url)
    return urls or list(DEFAULT_PROBE_URLS)


def default_probe_url(config: dict[str, Any]) -> str:
    return default_probe_urls(config)[0]


def route_health_check_url(config: dict[str, Any]) -> str:
    """Return the HTTPS URL TIER3-AUTO health-checks its members against.

    The loaded group requires HTTP 204, so the URL must answer 204 (a
    ``generate_204``-style endpoint).  Only an absolute HTTPS URL is accepted:
    the check exists to prove TLS to a real site, which a plain-HTTP or neutral
    connectivity endpoint cannot.  Anything else falls back to the default.
    This URL is deliberately not an authoritative probe URL: it gets no
    FINAL-EXIT system rule, so user routing rules for its host stay the truth.
    """

    value = _mihomo_helper_config(config).get("route_health_check_url")
    text = value.strip() if isinstance(value, str) else ""
    try:
        parsed = urlsplit(text)
        valid = parsed.scheme.casefold() == "https" and bool(parsed.hostname)
    except ValueError:
        valid = False
    return text if valid else DEFAULT_ROUTE_HEALTH_CHECK_URL


def default_probe_expected_statuses(config: dict[str, Any]) -> set[int]:
    testing = testing_block(config)
    mihomo_helper = _mihomo_helper_config(config)
    raw_values = (
        mihomo_helper.get("expected_statuses")
        or mihomo_helper.get("expected_status")
        or testing.get("expected_statuses")
        or [204, 200]
    )
    statuses: set[int] = set()
    for value in as_string_list(raw_values):
        try:
            statuses.add(int(value))
        except ValueError:
            continue
    return statuses or {204, 200}


def default_probe_timeout_ms(config: dict[str, Any]) -> int:
    testing = testing_block(config)
    mihomo_helper = _mihomo_helper_config(config)
    raw_timeout = (
        mihomo_helper.get("probe_timeout")
        or testing.get("request_timeout")
        or 8
    )
    try:
        timeout_seconds = float(raw_timeout)
    except (TypeError, ValueError):
        timeout_seconds = 8.0
    return int(max(1000.0, timeout_seconds * 1000.0))


def startup_probe_timeout_seconds(config: dict[str, Any]) -> float:
    testing = testing_block(config)
    mihomo_helper = _mihomo_helper_config(config)
    tier3 = _tier3_config(config)
    explicit = "startup_probe_timeout" in mihomo_helper or "startup_probe_timeout" in tier3
    raw_timeout = (
        mihomo_helper.get("startup_probe_timeout")
        or tier3.get("startup_probe_timeout")
        or mihomo_helper.get("startup_timeout")
        or mihomo_helper.get("probe_timeout")
        or testing.get("request_timeout")
        or 8
    )
    try:
        timeout_seconds = float(raw_timeout)
    except (TypeError, ValueError):
        timeout_seconds = 8.0
    if not explicit:
        timeout_seconds = max(timeout_seconds, 8.0)
    return max(1.0, min(30.0, timeout_seconds))


def startup_core_probe_limit(config: dict[str, Any]) -> int:
    mihomo_helper = _mihomo_helper_config(config)
    tier3 = _tier3_config(config)
    raw_limit = (
        mihomo_helper.get("startup_core_probe_limit")
        or tier3.get("startup_core_probe_limit")
        or tier3.get("startup_export_candidate_limit")
        or DEFAULT_TIER3_STARTUP_EXPORT_CANDIDATE_LIMIT
    )
    try:
        return max(1, int(raw_limit))
    except (TypeError, ValueError):
        return DEFAULT_TIER3_STARTUP_EXPORT_CANDIDATE_LIMIT


def startup_core_probe_concurrency(config: dict[str, Any]) -> int:
    mihomo_helper = _mihomo_helper_config(config)
    tier3 = _tier3_config(config)
    raw_value = (
        tier3.get("startup_validation_concurrency")
        or mihomo_helper.get("startup_validation_concurrency")
        or mihomo_helper.get("concurrency")
        or DEFAULT_TIER3_STARTUP_VALIDATION_CONCURRENCY
    )
    try:
        return max(1, int(raw_value))
    except (TypeError, ValueError):
        return DEFAULT_TIER3_STARTUP_VALIDATION_CONCURRENCY


def slow_threshold_ms_from_testing(testing_config: Any, default_ms: int = 2000) -> int:
    testing = testing_config if isinstance(testing_config, dict) else {}
    return coerce_positive_int(testing.get("slow_threshold_ms"), default_ms)


def validate_primary_gateway_max_latency_ms(value: Any) -> int:
    """Validate the persisted Tier1/Tier2/Gateway latency ceiling."""

    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not MIN_PRIMARY_GATEWAY_MAX_LATENCY_MS
        <= value
        <= MAX_PRIMARY_GATEWAY_MAX_LATENCY_MS
    ):
        raise ValueError(
            "testing.primary_gateway_max_latency_ms must be an integer "
            f"between {MIN_PRIMARY_GATEWAY_MAX_LATENCY_MS} and "
            f"{MAX_PRIMARY_GATEWAY_MAX_LATENCY_MS}"
        )
    return value


def primary_gateway_max_latency_ms_from_testing(testing_config: Any) -> int:
    """Return a safe latency ceiling for Tier1, Tier2, and Gateway checks."""

    testing = testing_config if isinstance(testing_config, dict) else {}
    try:
        return validate_primary_gateway_max_latency_ms(
            testing.get("primary_gateway_max_latency_ms")
        )
    except ValueError:
        return DEFAULT_PRIMARY_GATEWAY_MAX_LATENCY_MS


def primary_gateway_slow_threshold_ms(max_latency_ms: Any) -> float:
    """Return the exact inclusive normal/slow boundary derived from a ceiling."""

    try:
        maximum = validate_primary_gateway_max_latency_ms(max_latency_ms)
    except ValueError:
        maximum = DEFAULT_PRIMARY_GATEWAY_MAX_LATENCY_MS
    return maximum / 2.0


def primary_gateway_slow_threshold_ms_from_testing(testing_config: Any) -> float:
    """Return the exact ``max_latency_ms / 2`` strict-tier boundary."""

    return primary_gateway_max_latency_ms_from_testing(testing_config) / 2.0


def helper_block_from_testing(testing_config: Any, key: str) -> dict[str, Any]:
    testing = testing_config if isinstance(testing_config, dict) else {}
    return _dict_value(testing, key)


def node_test_tcp_timeout_seconds(testing_config: Any, default_seconds: float = 5.0) -> float:
    testing = testing_config if isinstance(testing_config, dict) else {}
    return coerce_float(testing.get("tcp_timeout", default_seconds), default_seconds, minimum=0.1)


def node_test_concurrency_from_testing(
    testing_config: Any,
    *,
    full_validation_enabled: bool,
    has_tier3_nodes: bool,
    has_gateway_nodes: bool,
    default: int = 50,
) -> int:
    testing = testing_config if isinstance(testing_config, dict) else {}
    base_concurrency = coerce_positive_int(testing.get("concurrency"), default)
    if has_gateway_nodes:
        gateway_default = min(base_concurrency, 3)
        return coerce_positive_int(testing.get("gateway_chain_concurrency"), gateway_default)
    helper_config = helper_block_from_testing(testing, "mihomo_helper")
    helper_concurrency = helper_config.get("concurrency")
    if full_validation_enabled and has_tier3_nodes and helper_concurrency:
        return coerce_positive_int(helper_concurrency, base_concurrency)
    return base_concurrency


def configured_tier3_export_limit(config: dict[str, Any]) -> int:
    subscription = subscription_server_block(config)
    return coerce_positive_int(subscription.get("tier3_limit", 20), 20)
