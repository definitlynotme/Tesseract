from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import ipaddress
import math

from alert_config import (
    ALERT_CHANNEL_CONTROL_FIELDS,
    ALL_KNOWN_ALERT_CHANNEL_TYPES,
    validate_notification_channel_config,
)
from config_item_identity import source_entry_value
from config_utils import subscription_entry_url
from dns_resolver_policy import DNS_MODES, DNS_MODE_CUSTOM, dns_config_list, normalize_dns_mode
from runtime_settings import validate_primary_gateway_max_latency_ms


def validate_local_config(payload: dict[str, Any]) -> None:
    if "testing" in payload:
        validate_testing_local(payload["testing"])

    client = payload.get("client") if isinstance(payload.get("client"), dict) else {}
    if "dns" in client:
        validate_dns_local(client.get("dns"))

    tiers = payload.get("tiers", {}) if isinstance(payload.get("tiers"), dict) else {}
    tier1_nodes = tiers.get("tier1", {}).get("nodes", [])
    validate_tier1_local(tier1_nodes)

    tier2_config = tiers.get("tier2", {})
    validate_imported_tier_local(
        "tier2",
        tier2_config.get("nodes", []),
        tier2_config.get("subscription_urls", []),
    )

    tier3_config = payload.get("tiers", {}).get("tier3", {}) if isinstance(payload.get("tiers"), dict) else {}
    tier3_config = tier3_config if isinstance(tier3_config, dict) else {}
    validate_imported_tier_local(
        "tier3",
        tier3_config.get("nodes", []),
        tier3_config.get("subscription_urls", []),
    )
    tier3_bootstrap_sources = tier3_config.get("bootstrap_sources", None)
    if tier3_bootstrap_sources is not None:
        raise ValueError("tiers.tier3.bootstrap_sources has moved to sources.yaml")

    exit_config = payload.get("tiers", {}).get("exit", {}) if isinstance(payload.get("tiers"), dict) else {}
    exit_config = exit_config if isinstance(exit_config, dict) else {}
    validate_imported_tier_local(
        "exit",
        exit_config.get("nodes", []),
        exit_config.get("subscription_urls", []),
    )

    killswitch = payload.get("killswitch", {})
    validate_killswitch_local(killswitch)
    validate_alerts_local(payload.get("alerts"))
    validate_ui_local(payload.get("ui"))


def validate_ui_local(ui: Any) -> None:
    """Only the typed D8 key is checked; other ui keys stay unvalidated as before."""
    if not isinstance(ui, dict) or "start_in_tray_on_autostart" not in ui:
        return
    if not isinstance(ui["start_in_tray_on_autostart"], bool):
        raise ValueError("ui.start_in_tray_on_autostart must be a boolean")


_DNS_LIST_KEYS = (
    "nameserver",
    "default_nameserver",
    "proxy_server_nameserver",
    "direct_nameserver",
    "fallback",
)


def validate_dns_local(dns: Any) -> None:
    """Validate ``client.dns``: a known mode, list-shaped lists, literal bootstrap IPs."""

    if dns is None:
        return
    if not isinstance(dns, dict):
        raise ValueError("client.dns must be an object")
    mode = str(dns.get("mode") or "").strip().lower().replace("-", "_")
    if mode and mode not in DNS_MODES:
        raise ValueError(f"client.dns.mode must be one of {', '.join(DNS_MODES)}")
    for key in _DNS_LIST_KEYS:
        for variant in (key, key.replace("_", "-")):
            value = dns.get(variant)
            if value is not None and not isinstance(value, (str, list, tuple)):
                raise ValueError(f"client.dns.{variant} must be a list of resolver addresses")
    if normalize_dns_mode(dns) == DNS_MODE_CUSTOM:
        # Mihomo refuses hostnames here and would fail to load the Core.
        for entry in dns_config_list(dns, "default_nameserver"):
            if entry == "system":
                continue
            try:
                ipaddress.ip_address(entry)
            except ValueError as exc:
                raise ValueError(
                    "client.dns.default_nameserver entries must be IP literals or 'system'"
                ) from exc


def validate_testing_local(testing: Any) -> None:
    if not isinstance(testing, dict):
        raise ValueError("testing must be an object")
    if "primary_gateway_max_latency_ms" in testing:
        validate_primary_gateway_max_latency_ms(
            testing["primary_gateway_max_latency_ms"]
        )


def validate_tier1_local(nodes: Any) -> None:
    if nodes is not None and not isinstance(nodes, list):
        raise ValueError("tiers.tier1.nodes must be a list")
    for node in nodes or []:
        if not isinstance(node, dict):
            raise ValueError("tier1 node must be an object")
        validate_tier1_node(node)


def validate_imported_tier_local(tier: str, nodes: Any, urls: Any) -> None:
    if nodes is not None and not isinstance(nodes, list):
        raise ValueError(f"tiers.{tier}.nodes must be a list")
    for node in nodes or []:
        if not isinstance(node, (dict, str)):
            raise ValueError(f"{tier} node must be an object or URI string")
    if urls is not None and not isinstance(urls, list):
        raise ValueError(f"tiers.{tier}.subscription_urls must be a list")
    for entry in urls or []:
        if not isinstance(entry, (dict, str)):
            raise ValueError(f"{tier} subscription must be an object or URL string")
        url = subscription_entry_url(entry)
        if not url:
            raise ValueError(f"{tier} subscription URL is required")
        validate_url(url)


def validate_killswitch_local(killswitch: Any) -> None:
    if killswitch is not None and not isinstance(killswitch, dict):
        raise ValueError("killswitch must be an object")
    if isinstance(killswitch, dict):
        whitelist = killswitch.get("additional_whitelist", [])
        if whitelist is not None and not isinstance(whitelist, list):
            raise ValueError("killswitch.additional_whitelist must be a list")


def validate_alerts_local(alerts: Any) -> None:
    if alerts is None:
        return
    if not isinstance(alerts, dict):
        raise ValueError("alerts must be an object")

    for channel_type in ALL_KNOWN_ALERT_CHANNEL_TYPES:
        if channel_type not in alerts:
            continue
        config = alerts[channel_type]
        if not isinstance(config, dict):
            raise ValueError(f"alerts.{channel_type} must be an object")
        validate_notification_channel_config(
            channel_type,
            config,
            path=f"alerts.{channel_type}",
        )

    channels = alerts.get("channels")
    if channels is None:
        return
    if not isinstance(channels, list):
        raise ValueError("alerts.channels must be a list")

    seen_ids: set[str] = set()
    for index, item in enumerate(channels):
        path = f"alerts.channels[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{path} must be an object")
        channel_type = str(
            item.get("type") or item.get("channel") or item.get("kind") or ""
        ).strip().lower()
        if not channel_type:
            raise ValueError(f"{path}.type is required")
        channel_id = str(
            item.get("id") or item.get("key") or item.get("name") or f"{channel_type}-{index + 1}"
        ).strip()
        if channel_id in seen_ids:
            raise ValueError(f"{path}.id is duplicated: {channel_id}")
        seen_ids.add(channel_id)

        raw_config = item.get("config")
        if raw_config is not None and not isinstance(raw_config, dict):
            raise ValueError(f"{path}.config must be an object")
        config = dict(raw_config if isinstance(raw_config, dict) else {})
        for field, value in item.items():
            if field not in {
                "id",
                "key",
                "name",
                "label",
                "type",
                "channel",
                "kind",
                "group",
                "cost",
                "config",
            }:
                config.setdefault(field, value)
        for field in ALERT_CHANNEL_CONTROL_FIELDS:
            if field in item:
                config[field] = item[field]
        validate_notification_channel_config(
            channel_type,
            config,
            path=path,
        )


def validate_sources_config(payload: dict[str, Any]) -> None:
    profiles = payload.get('fetch_profiles', {})
    if not isinstance(profiles, dict):
        raise ValueError('fetch_profiles must be a mapping')
    for name, paths in profiles.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError('fetch profile names must be nonempty strings')
        _validate_fetch_paths(paths)
    for key in ["github_repos", "telegram_channels", "subscription_urls", "local_files"]:
        if key in payload and not isinstance(payload[key], list):
            raise ValueError(f"{key} must be a list")
    for key in ["github_repos", "subscription_urls"]:
        for entry in payload.get(key, []) or []:
            validate_url(source_entry_value(entry))
            if isinstance(entry, dict) and "github_delivery" in entry:
                _validate_github_delivery(entry["github_delivery"])
    for key in ['github_repos', 'subscription_urls', 'telegram_channels', 'local_files']:
        for entry in payload.get(key, []) or []:
            if not isinstance(entry, dict):
                continue
            if 'fetch_paths' in entry:
                _validate_fetch_paths(entry['fetch_paths'])
                paths = entry['fetch_paths']
            else:
                profile = entry.get('fetch_profile') or ''
                if not isinstance(profile, str) or (profile and profile not in profiles):
                    raise ValueError('source references an undefined fetch profile')
                paths = profiles.get(profile, [])
            if key == 'github_repos' and any(
                item.get('kind') == 'url' and not str(item.get('unit') or '').strip()
                for item in paths
            ):
                raise ValueError('repository URL fetch paths must name their unit')
    delivery = payload.get("github_delivery")
    if delivery is not None:
        _validate_github_delivery(delivery)


def _validate_fetch_paths(paths: Any) -> None:
    if not isinstance(paths, list):
        raise ValueError('fetch_paths must be a list')
    for item in paths:
        if not isinstance(item, dict) or item.get('kind') not in {
            'url', 'github_api', 'github_proxy', 'github_cdn',
        }:
            raise ValueError('unsupported source fetch path')
        if item.get('kind') != 'github_api':
            value = str(item.get('url') or '')
            validate_url(value)
            parsed = urlparse(value)
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError('fetch paths cannot contain credentials or query parameters')
        try:
            delay = float(item.get('delay_seconds', 0))
        except (TypeError, ValueError) as exc:
            raise ValueError('fetch path delay must be a number') from exc
        if not math.isfinite(delay) or not 0 <= delay <= 5:
            raise ValueError('fetch path delay must be between 0 and 5 seconds')


def _validate_github_delivery(delivery: Any) -> None:
    if not isinstance(delivery, dict):
        raise ValueError("github_delivery must be a mapping")

    def validate_delay(value: Any) -> None:
        try:
            delay = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("github_delivery delay must be a number") from exc
        if not math.isfinite(delay) or not 0.0 <= delay <= 5.0:
            raise ValueError("github_delivery delay must be between 0 and 5 seconds")

    for key in ("proxies", "mirrors"):
        if key in delivery and not isinstance(delivery[key], list):
            raise ValueError(f"github_delivery.{key} must be a list")
        for item in delivery.get(key) or []:
            value = str(item.get("url") or "") if isinstance(item, dict) else str(item)
            if key == "mirrors" and "://" not in value:
                value = "https://" + value
            validate_url(value)
            parsed = urlparse(value)
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("github_delivery endpoints cannot contain credentials or query parameters")
            if isinstance(item, dict) and "delay_seconds" in item:
                validate_delay(item["delay_seconds"])
    if "hedge_seconds" in delivery:
        validate_delay(delivery["hedge_seconds"])


def validate_tier1_node(node: dict[str, Any]) -> None:
    if not str(node.get("name", "")).strip():
        raise ValueError("tier1 node name is required")
    if not str(node.get("address", "")).strip():
        raise ValueError("tier1 node address is required")
    try:
        port = int(node.get("port", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("tier1 node port must be an integer") from exc
    if port < 1 or port > 65535:
        raise ValueError("tier1 node port must be between 1 and 65535")


def validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("URL must be http or https")
