from __future__ import annotations

import os
import secrets
import shutil
import sys
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from windows_file_security import restrict_to_current_user

from app_info import APP_NAME
from dns_resolver_policy import (
    DNS_MODE_AUTO,
    DNS_MODE_CUSTOM,
    DNS_MODE_ENCRYPTED_ONLY,
    DNS_MODE_SYSTEM,
    dns_lists_are_legacy_defaults,
)
from config_utils import (
    canonicalize_routing_rule_layout,
    coerce_bool,
    normalize_routing_compatibility_defaults,
)
from tier3_sources import canonical_source_identity, infer_source_type


LOCAL_CONFIG_TEMPLATE = """# Local private overrides for Tesseract.
# Add personal VPS nodes, subscription URLs, and proxy credentials here.

migrations:
  tier3_min_alive_default_v2: true
  tier3_fast_validation_default_v1: true
  tier3_bootstrap_switch_removed_v1: true

tiers:
  tier1:
    nodes: []
  tier2:
    nodes: []
    subscription_urls: []
  tier3:
    enabled: false
    nodes: []
    subscription_urls: []
    min_alive_nodes: 3
  exit:
    subscription_urls: []
    nodes: []

egress_proxy:
  enabled: false
  scheme: http
  server: ''
  port: 0
  username: ''
  password: ''

alerts:
  telegram:
    enabled: false
    bot_token: ''
    chat_id: ''
    parse_mode: HTML
    silent: false
    protect_content: false
    disable_link_preview: true
  whatsapp:
    enabled: false
    mode: text
    access_token: ''
    phone_number_id: ''
    to: ''
    api_url: ''
    graph_api_version: v25.0
    template:
      name: ''
      language: en_US
      body_params:
        - '{{severity}}'
        - '{{title}}'
        - '{{message}}'
        - '{{timestamp}}'

ui:
  language: en_US
  theme: daylight

client:
  mihomo_controller:
    secret: '{controller_secret}'
  system_proxy:
    enabled: true
"""

DEFAULT_MANAGED_CONFIG_PATHS: tuple[tuple[str, ...], ...] = (
    ("tiers", "tier3", "enabled"),
    ("tiers", "tier3", "min_alive_nodes"),
    ("tiers", "tier3", "validation_candidate_limit"),
    ("tiers", "tier3", "validation_empty_retry_limit"),
    ("tiers", "tier3", "validation_batch_size"),
    ("tiers", "tier3", "validation_concurrency_min"),
    ("tiers", "tier3", "validation_concurrency_max"),
    ("tiers", "tier3", "validation_worker_min"),
    ("tiers", "tier3", "validation_worker_max"),
    ("tiers", "tier3", "full_validation_process_max"),
    # The batch validator capacity profile ships as the base default; managing
    # these keys converges existing base files (overrides live in the local
    # layer, which this sync never touches).
    ("tiers", "tier3", "validation_slots_per_process"),
    ("tiers", "tier3", "tcp_breadth_per_identity"),
    ("tiers", "tier3", "tcp_dns_capacity"),
    ("tiers", "tier3", "full_validation_concurrency_max"),
    ("tiers", "tier3", "full_validation_timeout_floor"),
    ("tiers", "tier3", "source_stream_window_min"),
    ("tiers", "tier3", "source_stream_window_max"),
    ("tiers", "tier3", "validation_budget_multiplier"),
    ("tiers", "tier3", "negative_cache_cooldown_hours"),
    ("tiers", "tier3", "pool_eviction_failure_threshold"),
    ("tiers", "tier3", "pi_controller", "kp"),
    ("tiers", "tier3", "pi_controller", "ki"),
    ("tiers", "tier3", "pi_controller", "min_intensity"),
    ("tiers", "tier3", "risk_control", "enabled"),
    ("tiers", "tier3", "risk_control", "rollout_mode"),
    ("tiers", "tier3", "risk_control", "fast_allocation_interval_ms"),
    ("tiers", "tier3", "risk_control", "envelope_interval_ms"),
    ("tiers", "tier3", "risk_control", "model_refresh_interval_seconds"),
    ("tiers", "tier3", "risk_control", "max_learning_correction"),
    ("tiers", "tier3", "risk_control", "initial_safe_worker_cap"),
    ("tiers", "tier3", "risk_control", "hard_worker_max"),
    ("tiers", "tier3", "risk_control", "canary_step"),
    ("tiers", "tier3", "risk_control", "canary_cooldown_seconds"),
    ("tiers", "tier3", "risk_control", "min_candidate_observations"),
    ("tiers", "tier3", "risk_control", "min_candidate_passes"),
    ("tiers", "tier3", "risk_control", "mature_candidate_observations"),
    ("tiers", "tier3", "risk_control", "mature_candidate_passes"),
    ("tiers", "tier3", "risk_control", "min_cvar_sessions"),
    ("tiers", "tier3", "startup_validation_candidate_limit"),
    ("tiers", "tier3", "startup_validation_empty_retry_limit"),
    ("tiers", "tier3", "startup_validation_batch_size"),
    ("tiers", "tier3", "startup_validation_concurrency"),
    ("tiers", "tier3", "startup_prelaunch_validation"),
    ("tiers", "tier3", "startup_fast_probe_limit"),
    ("tiers", "tier3", "startup_export_candidate_limit"),
    ("tiers", "tier3", "startup_core_probe_limit"),
    ("tiers", "tier3", "startup_request_timeout"),
    ("tiers", "tier3", "startup_retries"),
    ("tiers", "tier3", "startup_tcp_timeout"),
    ("tiers", "tier3", "startup_probe_timeout"),
    ("tiers", "tier3", "startup_helper_timeout"),
    ("tiers", "tier3", "empty_refresh_replace_after_failures"),
)

OBSOLETE_DEFAULT_CONFIG_PATHS: tuple[tuple[str, ...], ...] = (
    ("ui", "click_sound"),
    ("tiers", "tier3", "startup_min_alive_nodes"),
    ("tiers", "tier3", "zero_asset_startup_min_alive_nodes"),
    ("tiers", "tier3", "zero_asset_startup_core_min_alive_nodes"),
    ("tiers", "tier3", "zero_asset_startup_collect_interval"),
    ("tiers", "tier3", "zero_asset_startup_source_budget"),
    ("tiers", "tier3", "zero_asset_validation_empty_retry_limit"),
    ("tiers", "tier3", "zero_asset_validation_batch_size"),
    # The fallback-source switch was retired: fallback sources are always used.
    ("tiers", "tier3", "bootstrap_enabled"),
)

_MISSING = object()
CURRENT_TIER3_MIN_ALIVE_DEFAULT = 3
LEGACY_TIER3_MIN_ALIVE_DEFAULT = 20
TIER3_MIN_ALIVE_DEFAULT_MIGRATION = "tier3_min_alive_default_v2"
# The batch validator ("fast expansion") profile became the shipped base
# default and its Settings switch was removed.  Once, drop the local copies
# that switch wrote -- either its on profile or its off fingerprint -- so the
# local layer inherits the base again.  Any other hand-written value stays.
TIER3_FAST_VALIDATION_DEFAULT_MIGRATION = "tier3_fast_validation_default_v1"
LEGACY_TIER3_FAST_EXPANSION_PROFILE: dict[str, Any] = {
    "validation_slots_per_process": 8,
    "validation_worker_max": 8,
    "full_validation_process_max": 8,
    "validation_concurrency_max": 8,
    "tcp_breadth_per_identity": 2.0,
    "tcp_dns_capacity": "auto",
}
# Switching off popped every profile key and wrote only these two.
LEGACY_TIER3_STANDARD_VALIDATION_PROFILE: dict[str, Any] = {
    "validation_slots_per_process": 1,
    "tcp_breadth_per_identity": 1.0,
}
# The Settings card always wrote tiers.tier3.bootstrap_enabled to the local
# layer.  The switch is gone, so drop that copy once (missing == enabled).
TIER3_BOOTSTRAP_SWITCH_REMOVAL_MIGRATION = "tier3_bootstrap_switch_removed_v1"
ROUTING_DIRECT_DEFAULTS_MIGRATION = "routing_direct_defaults_v1"
ROUTING_RULE_DEFAULTS_V2_MIGRATION = "routing_rule_defaults_v2"
ROUTING_RULE_DEFAULTS_MIGRATION = "routing_rule_defaults_v3"
TIER3_SOURCE_PORTFOLIO_DEFAULTS_MIGRATION = "tier3_source_portfolio_defaults_v1"
# v2 adds only the template entries tagged ``added_in: <this key>``, so a
# default the user deleted after v1 is never brought back.
TIER3_SOURCE_PORTFOLIO_DEFAULTS_V2_MIGRATION = "tier3_source_portfolio_defaults_v2"
TIER3_SOURCE_DEFAULT_DELIVERY_MIGRATION = "tier3_source_default_delivery_v1"
# v3: the shipped Cloudflare/Google DoH ``proxy_server_nameserver`` pair is
# recognised as a shipped default too (v2 filed it as a custom policy).
DNS_RESOLVER_DEFAULTS_MIGRATION = "dns_resolver_defaults_v3"
# Remove the hidden compatibility-defaults routing stage (former CN and
# bootstrap-source DIRECT rules the Settings UI never exposed).
ROUTING_COMPATIBILITY_PURGE_MIGRATION = "routing_compatibility_purge_v1"
# Seed the regional DIRECT default into the user-editable list once, so it is
# visible and deletable in Settings instead of being applied invisibly.
# v2: v1 seeded the base layer only, so an empty ``direct`` left in the local
# layer by the compatibility purge shadowed it and nothing rendered.
ROUTING_REGION_DIRECT_DEFAULTS_MIGRATION = "routing_region_direct_defaults_v2"
REGION_DIRECT_DEFAULT_RULES: tuple[str, ...] = ("geosite:CN", "geoip:CN")
# The fake-IP exclusion list shipped before the region-neutral resolver policy.
_LEGACY_FAKE_IP_FILTER = (
    "*.lan",
    "*.local",
    "+.msftconnecttest.com",
    "+.msftncsi.com",
    "localhost.ptlogin2.qq.com",
)
_DNS_LEGACY_LIST_KEYS = (
    "nameserver",
    "default_nameserver",
    "default-nameserver",
    "proxy_server_nameserver",
    "proxy-server-nameserver",
    "fallback",
)


@dataclass(frozen=True)
class AppPaths:
    resource_root: Path
    config_dir: Path
    data_dir: Path
    config_path: Path
    local_config_path: Path
    sources_path: Path
    log_file: Path
    tier3_nodes_file: Path
    tier2_cache_file: Path
    exit_cache_file: Path
    tier_observation_cache_file: Path
    tier3_control_state_path: Path
    mihomo_binary_path: Path
    mihomo_work_dir: Path
    mihomo_runtime_config: Path
    runtime_state_path: Path
    alert_delivery_history_path: Path
    config_backup_dir: Path
    unkill_script_path: Path
    restore_proxy_script_path: Path
    geo_metadata_path: Path
    exit_quality_dir: Path
    exit_quality_results_path: Path

    def resource_path(self, *parts: str) -> Path:
        return self.resource_root.joinpath(*parts)

    @property
    def dns_resolver_memory_file(self) -> Path:
        """Per-network resolver ordering hints (positive evidence only)."""

        return self.data_dir / "dns-resolver-memory.json"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def default_resource_root() -> Path:
    if is_frozen() and hasattr(sys, "_MEIPASS"):
        return Path(str(sys._MEIPASS)).resolve()
    return Path(__file__).resolve().parent


def default_config_dir() -> Path:
    configured = os.environ.get("TESSERACT_CONFIG_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return (Path(base) / APP_NAME).resolve()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg).expanduser() if xdg else Path.home() / ".config").joinpath(APP_NAME).resolve()


def default_data_dir() -> Path:
    configured = os.environ.get("TESSERACT_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return (Path(base) / APP_NAME).resolve()
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share").joinpath(APP_NAME).resolve()


def build_app_paths(
    *,
    resource_root: str | Path | None = None,
    config_dir: str | Path | None = None,
    data_dir: str | Path | None = None,
) -> AppPaths:
    resource = Path(resource_root).resolve() if resource_root is not None else default_resource_root()
    config = Path(config_dir).resolve() if config_dir is not None else default_config_dir()
    data = Path(data_dir).resolve() if data_dir is not None else default_data_dir()
    return AppPaths(
        resource_root=resource,
        config_dir=config,
        data_dir=data,
        config_path=config / "config.yaml",
        local_config_path=config / "config.local.yaml",
        sources_path=config / "sources.yaml",
        log_file=data / "monitor.log",
        tier3_nodes_file=data / "nodes.json",
        tier2_cache_file=data / "tier2_cache.json",
        exit_cache_file=data / "exit_cache.json",
        tier_observation_cache_file=data / "tier_observation_cache.json",
        tier3_control_state_path=data / "tier3-control-state.json",
        mihomo_binary_path=resource / "bin" / "mihomo.exe",
        mihomo_work_dir=data / "mihomo",
        mihomo_runtime_config=data / "mihomo-runtime.yaml",
        runtime_state_path=data / "runtime-state.json",
        alert_delivery_history_path=data / "alert-delivery-history.json",
        config_backup_dir=data / "config-backups",
        unkill_script_path=data / "unkill.bat",
        restore_proxy_script_path=data / "restore_proxy.bat",
        geo_metadata_path=data / "geo-metadata.json",
        exit_quality_dir=data / "exit-quality",
        exit_quality_results_path=data / "exit-quality" / "results-v2.json",
    )


def bootstrap_user_files(paths: AppPaths) -> None:
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    restrict_to_current_user(paths.config_dir)

    base_config_existed = paths.config_path.exists()
    base_compatibility_snapshot = _routing_compatibility_snapshot(
        paths.config_path
    )
    local_compatibility_snapshot = _routing_compatibility_snapshot(
        paths.local_config_path
    )

    legacy_routing_direct_values: tuple[str, ...] | None = None
    if not _config_migration_enabled(
        paths.local_config_path,
        ROUTING_RULE_DEFAULTS_V2_MIGRATION,
    ) and not _config_migration_enabled(
        paths.local_config_path,
        ROUTING_DIRECT_DEFAULTS_MIGRATION,
    ):
        legacy_routing_direct_values = _legacy_routing_direct_values(
            paths.local_config_path
        )
        if legacy_routing_direct_values is None:
            legacy_routing_direct_values = _legacy_routing_direct_values(
                paths.config_path
            )

    if not paths.config_path.exists():
        template = paths.resource_path("config.example.yaml")
        if not template.exists():
            raise FileNotFoundError(f"Missing default config template: {template}")
        shutil.copy2(template, paths.config_path)
    else:
        _migrate_config_defaults(
            paths.config_path,
            paths.resource_path("config.example.yaml"),
        )

    sources_existed = paths.sources_path.exists()
    sources_template = paths.resource_path("sources.example.yaml")
    if not sources_existed:
        if not sources_template.exists():
            raise FileNotFoundError(f"Missing default sources template: {sources_template}")
        shutil.copy2(sources_template, paths.sources_path)

    if not paths.local_config_path.exists():
        paths.local_config_path.write_text(_local_config_template_text(), encoding="utf-8")

    if sources_existed:
        _migrate_tier3_source_portfolio_defaults(paths, sources_template)
        _migrate_tier3_source_default_delivery(paths, sources_template)
        _migrate_explicit_source_delivery(paths)
    _migrate_legacy_bootstrap_sources_to_tier3b_sources(paths)
    _migrate_local_config_defaults(paths.local_config_path)
    _migrate_routing_rule_defaults(
        paths.config_path,
        paths.local_config_path,
        paths.resource_path("config.example.yaml"),
        legacy_direct_values=legacy_routing_direct_values,
        base_config_existed=base_config_existed,
        base_compatibility_snapshot=base_compatibility_snapshot,
        local_compatibility_snapshot=local_compatibility_snapshot,
    )
    _migrate_routing_compatibility_purge(
        paths.config_path, paths.local_config_path
    )
    _migrate_routing_region_direct_defaults(
        paths.config_path, paths.local_config_path
    )
    _migrate_dns_resolver_defaults(paths.config_path, paths.local_config_path)

    for config_file in (
        paths.config_path,
        paths.local_config_path,
        paths.sources_path,
    ):
        if config_file.exists():
            restrict_to_current_user(config_file)


def _local_config_template_text() -> str:
    return LOCAL_CONFIG_TEMPLATE.format(
        controller_secret=_generate_controller_secret(),
    )


def _generate_controller_secret() -> str:
    return secrets.token_urlsafe(32)


def _merge_missing_defaults(payload: dict[str, Any], defaults: dict[str, Any]) -> bool:
    changed = False
    for key, default_value in defaults.items():
        if key not in payload or payload.get(key) is None:
            payload[key] = _deep_copy(default_value)
            changed = True
            continue
        current_value = payload.get(key)
        if isinstance(current_value, dict) and isinstance(default_value, dict):
            changed = _merge_missing_defaults(current_value, default_value) or changed
    return changed


def _migrate_config_defaults(config_path: Path, template_path: Path) -> None:
    if not template_path.exists():
        return
    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        defaults = yaml.safe_load(template_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return
    if not isinstance(payload, dict) or not isinstance(defaults, dict):
        return

    changed = canonicalize_routing_rule_layout(payload)
    canonicalize_routing_rule_layout(defaults)
    # Compatibility defaults carry user-owned ordering and deletion
    # provenance.  They must be migrated from the pre-merge snapshot below;
    # treating them as an ordinary missing default would erase the difference
    # between "inherit" and "the user removed every compatibility rule".
    default_routing = defaults.get("routing")
    default_rules = (
        default_routing.get("rules")
        if isinstance(default_routing, dict)
        else None
    )
    if isinstance(default_rules, dict):
        default_rules.pop("compatibility_defaults", None)
    changed = _merge_missing_defaults(payload, defaults) or changed
    changed = _sync_default_managed_values(payload, defaults) or changed
    changed = _prune_obsolete_default_values(payload) or changed
    if changed:
        config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _get_nested(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return _MISSING
        current = current[key]
    return current


def _set_nested(payload: dict[str, Any], path: tuple[str, ...], value: Any) -> bool:
    current: dict[str, Any] = payload
    for key in path[:-1]:
        next_value = current.get(key)
        if not isinstance(next_value, dict):
            next_value = {}
            current[key] = next_value
        current = next_value
    leaf = path[-1]
    if current.get(leaf) == value:
        return False
    current[leaf] = _deep_copy(value)
    return True


def _delete_nested(payload: dict[str, Any], path: tuple[str, ...]) -> bool:
    current: Any = payload
    for key in path[:-1]:
        if not isinstance(current, dict) or key not in current:
            return False
        current = current[key]
    if not isinstance(current, dict):
        return False
    leaf = path[-1]
    if leaf not in current:
        return False
    del current[leaf]
    return True


def _sync_default_managed_values(payload: dict[str, Any], defaults: dict[str, Any]) -> bool:
    changed = False
    for path in DEFAULT_MANAGED_CONFIG_PATHS:
        default_value = _get_nested(defaults, path)
        if default_value is _MISSING:
            continue
        changed = _set_nested(payload, path, default_value) or changed
    return changed


def _prune_obsolete_default_values(payload: dict[str, Any]) -> bool:
    changed = False
    for path in OBSOLETE_DEFAULT_CONFIG_PATHS:
        changed = _delete_nested(payload, path) or changed
    return changed


def _normalize_bootstrap_source_entry(entry: Any) -> dict[str, Any] | None:
    if isinstance(entry, str):
        value = entry.strip()
        if not value:
            return None
        return {
            "type": infer_source_type(value),
            "value": value,
            "enabled": True,
            "origin": "bootstrap",
            "tags": ["bootstrap-user"],
        }
    if not isinstance(entry, dict):
        return None
    value = str(entry.get("value") or entry.get("url") or entry.get("path") or entry.get("channel") or "").strip()
    if not value:
        return None
    normalized = dict(entry)
    normalized["type"] = str(entry.get("type") or infer_source_type(value))
    normalized["value"] = value
    normalized["enabled"] = coerce_bool(entry.get("enabled", True), True)
    normalized["origin"] = str(entry.get("origin") or "bootstrap")
    tags = entry.get("tags", [])
    normalized["tags"] = list(tags) if isinstance(tags, list) and tags else ["bootstrap-user"]
    headers = entry.get("headers", {})
    if headers:
        normalized["headers"] = dict(headers) if isinstance(headers, dict) else {}
    return normalized


def _source_section_for_type(source_type: Any) -> str:
    normalized = str(source_type or "subscription_url").strip().lower()
    if normalized in {"github", "github_repo", "github_repos"}:
        return "github_repos"
    if normalized in {"telegram", "telegram_channel", "telegram_channels", "telegram_public_channel"}:
        return "telegram_channels"
    if normalized in {"file", "local", "local_file", "local_files"}:
        return "local_files"
    return "subscription_urls"


def _source_type_for_section(section: str) -> str:
    return {
        "github_repos": "github_repo",
        "telegram_channels": "telegram_channel",
        "local_files": "local_file",
        "subscription_urls": "subscription_url",
    }.get(section, "subscription_url")


def _source_config_entry_identity(section: str, entry: Any) -> tuple[str, str]:
    if isinstance(entry, str):
        value = entry.strip()
    elif isinstance(entry, dict):
        value = str(entry.get("value") or entry.get("url") or entry.get("path") or entry.get("channel") or "").strip()
    else:
        value = ""
    if not value:
        return "", ""
    source_type = _source_type_for_section(section)
    canonical = canonical_source_identity(source_type, value)
    normalized_type, normalized_value = canonical.split(":", 1)
    return normalized_type, normalized_value


def _sources_payload_entry(entry: dict[str, Any]) -> Any:
    value = str(entry.get("value") or "").strip()
    if not value:
        return ""
    payload: dict[str, Any] = {"value": value}
    if not coerce_bool(entry.get("enabled", True), True):
        payload["enabled"] = False
    headers = entry.get("headers")
    if isinstance(headers, dict) and headers:
        payload["headers"] = dict(headers)
    tags = entry.get("tags")
    if isinstance(tags, list) and tags and tags not in (["bootstrap-user"], ["bootstrap-default"]):
        payload["tags"] = list(tags)
    return value if set(payload) == {"value"} else payload


def _merge_tier3b_source_entries(sources_payload: dict[str, Any], entries: list[dict[str, Any]]) -> bool:
    changed = False
    seen: set[tuple[str, str]] = set()
    for section in ("github_repos", "telegram_channels", "subscription_urls", "local_files"):
        items = sources_payload.get(section)
        if not isinstance(items, list):
            items = []
            sources_payload[section] = items
            changed = True
        for item in items:
            identity = _source_config_entry_identity(section, item)
            if identity[1]:
                seen.add(identity)
    for entry in entries:
        section = _source_section_for_type(entry.get("type"))
        identity = (_source_type_for_section(section), str(entry.get("value") or "").strip())
        if not identity[1] or identity in seen:
            continue
        sources_payload[section].append(_sources_payload_entry(entry))
        seen.add(identity)
        changed = True
    return changed


def _dns_migration_mode(dns: dict[str, Any]) -> str:
    """Derive ``client.dns.mode`` for a block the current migration has not seen.

    ``auto`` may have been merged from the new template a moment ago and
    ``custom`` may be the verdict of an earlier migration round, so neither
    says anything about the user's intent; the lists do.  ``system`` and
    ``encrypted_only`` can only come from the user and are kept.
    """

    if coerce_bool(dns.get("encrypted_only"), False):
        return DNS_MODE_ENCRYPTED_ONLY
    existing = str(dns.get("mode") or "").strip().lower().replace("-", "_")
    if existing in {DNS_MODE_SYSTEM, DNS_MODE_ENCRYPTED_ONLY}:
        return existing
    if dns_lists_are_legacy_defaults(dns):
        return DNS_MODE_AUTO
    return DNS_MODE_CUSTOM


def _apply_dns_resolver_migration(payload: dict[str, Any]) -> bool:
    client = payload.get("client")
    if not isinstance(client, dict):
        return False
    dns = client.get("dns")
    if not isinstance(dns, dict):
        return False
    changed = False
    mode = _dns_migration_mode(dns)
    if mode == DNS_MODE_AUTO:
        # The shipped lists were never the user's choice; the runtime now
        # qualifies resolvers per network instead of loading these.
        for key in _DNS_LEGACY_LIST_KEYS:
            if key in dns:
                dns.pop(key, None)
                changed = True
        for key in ("fake_ip_filter", "fake-ip-filter"):
            entries = dns.get(key)
            if isinstance(entries, list) and tuple(str(item) for item in entries) == _LEGACY_FAKE_IP_FILTER:
                dns.pop(key, None)
                changed = True
    if dns.get("mode") != mode:
        dns["mode"] = mode
        changed = True
    if "encrypted_only" in dns:
        dns.pop("encrypted_only", None)
        changed = True
    return changed


def _migrate_routing_region_direct_defaults(
    config_path: Path, local_config_path: Path
) -> None:
    """Seed the regional DIRECT default once, into the list Settings shows.

    It runs only when neither config layer already expresses a direct choice,
    so a user who emptied the list or wrote their own is never overridden.  The
    entries land in ``routing.rules.direct``, which the UI renders and the user
    can delete permanently; nothing is applied behind that list.
    """

    if _config_migration_enabled(
        local_config_path, ROUTING_REGION_DIRECT_DEFAULTS_MIGRATION
    ):
        return
    try:
        local_payload = (
            yaml.safe_load(local_config_path.read_text(encoding="utf-8")) or {}
        )
    except Exception:
        return
    if not isinstance(local_payload, dict):
        return
    base_payload: dict[str, Any] | None = None
    if config_path.exists():
        try:
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except Exception:
            loaded = None
        # Only a config that already carries a routing section is seeded.  A
        # fresh install receives the default by copying the template, and a
        # minimal config that never mentioned routing is left untouched.
        if isinstance(loaded, dict) and isinstance(loaded.get("routing"), dict):
            base_payload = loaded
    local_present, _local_value = _routing_rule_entry(local_payload, "direct")
    base_present, _base_value = (
        _routing_rule_entry(base_payload, "direct")
        if isinstance(base_payload, dict)
        else (False, _MISSING)
    )
    base_changed = False
    if not local_present and not base_present and isinstance(base_payload, dict):
        base_changed = _set_routing_rule_entry(
            base_payload, "direct", list(REGION_DIRECT_DEFAULT_RULES)
        )
    migrations = local_payload.get("migrations")
    if not isinstance(migrations, dict):
        migrations = {}
        local_payload["migrations"] = migrations
    migrations[ROUTING_REGION_DIRECT_DEFAULTS_MIGRATION] = True
    local_config_path.write_text(
        yaml.safe_dump(local_payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    if base_changed and isinstance(base_payload, dict):
        config_path.write_text(
            yaml.safe_dump(base_payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )


def _migrate_routing_compatibility_purge(
    config_path: Path, local_config_path: Path
) -> None:
    """Drop the hidden ``compatibility_defaults`` routing stage once.

    That stage silently appended CN and bootstrap-source ``DIRECT`` rules the
    Settings UI never showed, so the persisted routing config was not the whole
    truth and forced country-shaped and source-host traffic direct for every
    user.  Public DIRECT is now authorized only by ``routing.rules.direct``.
    """

    if _config_migration_enabled(
        local_config_path, ROUTING_COMPATIBILITY_PURGE_MIGRATION
    ):
        return
    try:
        local_payload = (
            yaml.safe_load(local_config_path.read_text(encoding="utf-8")) or {}
        )
    except Exception:
        return
    if not isinstance(local_payload, dict):
        return
    canonicalize_routing_rule_layout(local_payload)
    _delete_routing_rule_entry(local_payload, "compatibility_defaults")
    base_payload: dict[str, Any] | None = None
    base_changed = False
    if config_path.exists():
        try:
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except Exception:
            loaded = None
        if isinstance(loaded, dict):
            base_payload = loaded
            base_changed = canonicalize_routing_rule_layout(base_payload)
            base_changed = (
                _delete_routing_rule_entry(base_payload, "compatibility_defaults")
                or base_changed
            )
    migrations = local_payload.get("migrations")
    if not isinstance(migrations, dict):
        migrations = {}
        local_payload["migrations"] = migrations
    migrations[ROUTING_COMPATIBILITY_PURGE_MIGRATION] = True
    local_config_path.write_text(
        yaml.safe_dump(local_payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    if base_changed and isinstance(base_payload, dict):
        config_path.write_text(
            yaml.safe_dump(base_payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )


def _migrate_dns_resolver_defaults(config_path: Path, local_config_path: Path) -> None:
    """Turn the historical resolver lists into an explicit ``client.dns.mode`` once."""

    if _config_migration_enabled(local_config_path, DNS_RESOLVER_DEFAULTS_MIGRATION):
        return
    payloads: dict[Path, dict[str, Any]] = {}
    for path in (config_path, local_config_path):
        if not path.exists():
            payloads[path] = {}
            continue
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            return
        if not isinstance(loaded, dict):
            return
        payloads[path] = loaded
    changed = {path: _apply_dns_resolver_migration(payload) for path, payload in payloads.items()}
    local_payload = payloads[local_config_path]
    migrations = local_payload.get("migrations")
    if not isinstance(migrations, dict):
        migrations = {}
        local_payload["migrations"] = migrations
    migrations[DNS_RESOLVER_DEFAULTS_MIGRATION] = True
    changed[local_config_path] = True
    for path, was_changed in changed.items():
        if not was_changed:
            continue
        if path == config_path and not config_path.exists():
            continue
        path.write_text(
            yaml.safe_dump(payloads[path], allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )


def _migrate_explicit_source_delivery(paths: AppPaths) -> None:
    """Bind legacy global roads once to existing sources, never to future ones."""
    try:
        payload = yaml.safe_load(paths.sources_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return
    if not isinstance(payload, dict):
        return
    from urllib.parse import urlparse
    from tier3_sources import legacy_fetch_paths, source_value
    if payload.get('github_delivery') is not None and not isinstance(payload['github_delivery'], dict):
        return
    global_delivery = payload.pop('github_delivery', None)
    changed = isinstance(global_delivery, dict)
    if isinstance(global_delivery, dict):
        for section in ('github_repos', 'subscription_urls'):
            entries = payload.get(section)
            if not isinstance(entries, list):
                continue
            for index, value in enumerate(entries):
                entry = dict(value) if isinstance(value, dict) else {'url': value}
                if 'fetch_profile' in entry or 'fetch_paths' in entry:
                    continue
                url = source_value(entry)
                # Bind only the sources to which the old GitHub conversion applied.
                parsed = urlparse(url)
                if parsed.hostname not in {'github.com', 'www.github.com', 'raw.githubusercontent.com'}:
                    continue
                local_delivery = entry.get('github_delivery', {})
                if not isinstance(local_delivery, dict):
                    # Leave invalid input intact for normal validation to report.
                    continue
                entry.pop('github_delivery', None)
                merged = {**global_delivery, **local_delivery}
                entry['fetch_paths'] = legacy_fetch_paths(merged)
                entries[index] = entry
    settings = [payload.get("github_delivery")]
    for section in ("github_repos", "subscription_urls", "telegram_channels"):
        entries = payload.get(section)
        if isinstance(entries, list):
            settings.extend(entry.get("github_delivery") for entry in entries if isinstance(entry, dict))
    for delivery in settings:
        if not isinstance(delivery, dict):
            continue
        for kind in ("proxies", "mirrors"):
            entries = delivery.get(kind)
            if not isinstance(entries, list):
                continue
            for index, entry in enumerate(entries):
                if not isinstance(entry, str) or not entry.strip():
                    continue
                url = entry.strip()
                if kind == "mirrors" and "://" not in url:
                    url = "https://" + url
                entries[index] = {
                    "url": url,
                    "enabled": True,
                    "delay_seconds": (0.0 if kind == "proxies" and index == 0
                                      else delivery.get("hedge_seconds", 0.75)),
                }
                changed = True
    if changed:
        _backup_sources_file(paths)
        _write_yaml_atomic(paths.sources_path, payload)


def _migrate_tier3_source_portfolio_defaults(
    paths: AppPaths,
    template_path: Path,
) -> None:
    """Apply the YAML-owned source portfolio once, then return ownership to the user."""

    if not template_path.exists() or not paths.sources_path.exists():
        return
    try:
        payload = yaml.safe_load(paths.sources_path.read_text(encoding="utf-8")) or {}
        defaults = yaml.safe_load(template_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return
    if not isinstance(payload, dict) or not isinstance(defaults, dict):
        return

    default_migrations = defaults.get("migrations")
    if not isinstance(default_migrations, dict):
        return
    enabled = [
        key
        for key in (
            TIER3_SOURCE_PORTFOLIO_DEFAULTS_MIGRATION,
            TIER3_SOURCE_PORTFOLIO_DEFAULTS_V2_MIGRATION,
        )
        if coerce_bool(default_migrations.get(key), False)
    ]
    if not enabled:
        return
    migrations = payload.get("migrations")
    if not isinstance(migrations, dict):
        migrations = {}
        payload["migrations"] = migrations
    pending = [key for key in enabled if not coerce_bool(migrations.get(key), False)]
    if not pending:
        return

    if TIER3_SOURCE_PORTFOLIO_DEFAULTS_MIGRATION in pending:
        # The full portfolio already contains every later addition.
        _merge_source_portfolio_lists(payload, defaults)
        _merge_source_candidate_paths(payload, defaults)
    else:
        _merge_source_portfolio_lists(
            payload,
            defaults,
            only_added_in=TIER3_SOURCE_PORTFOLIO_DEFAULTS_V2_MIGRATION,
        )
    for key in pending:
        migrations[key] = True

    _backup_sources_file(paths)
    _write_yaml_atomic(paths.sources_path, payload)


def _migrate_tier3_source_default_delivery(
    paths: AppPaths,
    template_path: Path,
) -> None:
    """Bind the template's roads once to the default sources the app itself seeded.

    Configs seeded before roads existed hold bare URLs, so every fetch stays on the
    original address; where that address is blocked the source is simply lost, even
    though the template ships a road for it (2026-09-19: three of five defaults died
    with "Cannot connect to host ..."). Only entries matching a default that carries
    roads are bound, and only while they declare no delivery of their own. Anything
    the user added, overrode (including an explicit empty list), authenticated, or
    later edits stays untouched, and the ledger entry stops this from running again.
    """

    if not template_path.exists() or not paths.sources_path.exists():
        return
    try:
        payload = yaml.safe_load(paths.sources_path.read_text(encoding="utf-8")) or {}
        defaults = yaml.safe_load(template_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return
    if not isinstance(payload, dict) or not isinstance(defaults, dict):
        return

    default_migrations = defaults.get("migrations")
    if not isinstance(default_migrations, dict) or not coerce_bool(
        default_migrations.get(TIER3_SOURCE_DEFAULT_DELIVERY_MIGRATION),
        False,
    ):
        return
    migrations = payload.get("migrations")
    if not isinstance(migrations, dict):
        migrations = {}
        payload["migrations"] = migrations
    if coerce_bool(migrations.get(TIER3_SOURCE_DEFAULT_DELIVERY_MIGRATION), False):
        return

    from tier3_sources import source_fetch_paths, source_value

    profiles = defaults.get("fetch_profiles")
    bound = False
    for section in ("github_repos", "subscription_urls", "telegram_channels", "local_files"):
        entries = payload.get(section)
        default_entries = defaults.get(section)
        if not isinstance(entries, list) or not isinstance(default_entries, list):
            continue
        roads_by_identity: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for default_entry in default_entries:
            if not isinstance(default_entry, dict):
                continue
            identity = _source_config_entry_identity(section, default_entry)
            roads = source_fetch_paths(default_entry, profiles)
            if identity[1] and roads:
                roads_by_identity[identity] = roads
        if not roads_by_identity:
            continue
        for index, value in enumerate(entries):
            roads = roads_by_identity.get(_source_config_entry_identity(section, value))
            if not roads:
                continue
            entry = dict(value) if isinstance(value, dict) else {"url": value}
            if {"fetch_paths", "fetch_profile", "github_delivery"} & set(entry):
                continue
            if entry.get("headers") or "?" in source_value(entry):
                # Authenticated sources keep their original address only.
                continue
            entry["fetch_paths"] = _deep_copy(roads)
            entries[index] = entry
            bound = True

    migrations[TIER3_SOURCE_DEFAULT_DELIVERY_MIGRATION] = True
    if bound:
        # Only a change to the user's own sources earns a backup; recording that
        # this ran is ledger upkeep, and must not bury real backups.
        _backup_sources_file(paths)
    _write_yaml_atomic(paths.sources_path, payload)


def _merge_source_portfolio_lists(
    payload: dict[str, Any],
    defaults: dict[str, Any],
    *,
    only_added_in: str | None = None,
) -> bool:
    changed = False
    for section in (
        "github_repos",
        "telegram_channels",
        "subscription_urls",
        "local_files",
    ):
        current = payload.get(section)
        if not isinstance(current, list):
            current = []
            payload[section] = current
            changed = True
        seen = {
            _source_config_entry_identity(section, entry)
            for entry in current
            if _source_config_entry_identity(section, entry)[1]
        }
        default_entries = defaults.get(section)
        if not isinstance(default_entries, list):
            continue
        for entry in default_entries:
            if only_added_in is not None and not (
                isinstance(entry, dict) and entry.get("added_in") == only_added_in
            ):
                continue
            identity = _source_config_entry_identity(section, entry)
            if not identity[1] or identity in seen:
                continue
            copied = _deep_copy(entry)
            if isinstance(copied, dict):
                copied.pop("added_in", None)
            if isinstance(copied, dict) and 'fetch_profile' in copied:
                from tier3_sources import source_fetch_paths
                copied['fetch_paths'] = source_fetch_paths(copied, defaults.get('fetch_profiles'))
                copied.pop('fetch_profile', None)
            current.append(copied)
            seen.add(identity)
            changed = True
    return changed


def _merge_source_candidate_paths(
    payload: dict[str, Any],
    defaults: dict[str, Any],
) -> bool:
    default_discovery = defaults.get("github_discovery")
    if not isinstance(default_discovery, dict):
        return False
    default_paths = default_discovery.get("candidate_paths")
    if not isinstance(default_paths, list):
        return False

    discovery = payload.get("github_discovery")
    if not isinstance(discovery, dict):
        discovery = {}
        payload["github_discovery"] = discovery
    current_paths = discovery.get("candidate_paths")
    if not isinstance(current_paths, list):
        current_paths = []
        discovery["candidate_paths"] = current_paths
    seen = {str(path).strip() for path in current_paths if str(path).strip()}
    changed = False
    for path in default_paths:
        normalized = str(path).strip()
        if not normalized or normalized in seen:
            continue
        current_paths.append(_deep_copy(path))
        seen.add(normalized)
        changed = True
    return changed


def _backup_sources_file(paths: AppPaths) -> Path:
    paths.config_backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = paths.config_backup_dir / f"sources.yaml.{stamp}.bak"
    shutil.copy2(paths.sources_path, backup_path)
    restrict_to_current_user(paths.config_backup_dir)
    restrict_to_current_user(backup_path)
    return backup_path


def _write_yaml_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        temporary.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    restrict_to_current_user(path)


def _read_legacy_bootstrap_sources(config_path: Path) -> list[dict[str, Any]]:
    if not config_path.exists():
        return []
    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []
    tiers = payload.get("tiers")
    tier3 = tiers.get("tier3") if isinstance(tiers, dict) else None
    if not isinstance(tier3, dict):
        return []
    raw_sources = tier3.get("bootstrap_sources")
    entries: list[dict[str, Any]] = []
    for item in raw_sources or []:
        normalized = _normalize_bootstrap_source_entry(item)
        if normalized:
            entries.append(normalized)
    return entries


def _delete_legacy_bootstrap_source_block(config_path: Path) -> None:
    if not config_path.exists():
        return
    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    tiers = payload.get("tiers")
    tier3 = tiers.get("tier3") if isinstance(tiers, dict) else None
    if not isinstance(tier3, dict):
        return
    changed = False
    if "bootstrap_sources" in tier3:
        del tier3["bootstrap_sources"]
        changed = True
    if "bootstrap_defaults_initialized" in tier3:
        del tier3["bootstrap_defaults_initialized"]
        changed = True
    if changed:
        config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _migrate_legacy_bootstrap_sources_to_tier3b_sources(paths: AppPaths) -> None:
    entries = [
        *_read_legacy_bootstrap_sources(paths.config_path),
        *_read_legacy_bootstrap_sources(paths.local_config_path),
    ]
    if not entries:
        return
    try:
        sources_payload = yaml.safe_load(paths.sources_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return
    if not isinstance(sources_payload, dict):
        return
    if _merge_tier3b_source_entries(sources_payload, entries):
        paths.sources_path.write_text(
            yaml.safe_dump(sources_payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    _delete_legacy_bootstrap_source_block(paths.config_path)
    _delete_legacy_bootstrap_source_block(paths.local_config_path)


def _migrate_local_config_defaults(local_config_path: Path) -> None:
    try:
        payload = yaml.safe_load(local_config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return
    if not isinstance(payload, dict):
        return

    changed = canonicalize_routing_rule_layout(payload)
    client = payload.setdefault("client", {})
    if isinstance(client, dict):
        controller = client.setdefault("mihomo_controller", {})
        if isinstance(controller, dict) and not str(controller.get("secret") or "").strip():
            controller["secret"] = _generate_controller_secret()
            changed = True

    tiers = payload.setdefault("tiers", {})
    if not isinstance(tiers, dict):
        if changed:
            local_config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return
    tier3 = tiers.setdefault("tier3", {})
    if not isinstance(tier3, dict):
        if changed:
            local_config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return

    changed = _migrate_legacy_tier3_min_alive_default(payload, tier3) or changed
    changed = _migrate_tier3_fast_validation_default(payload, tier3) or changed
    changed = _migrate_retired_tier3_bootstrap_switch(payload, tier3) or changed
    changed = _delete_nested(payload, ("ui", "click_sound")) or changed

    if "enabled" not in tier3:
        tier3["enabled"] = False
        changed = True
    else:
        enabled = coerce_bool(tier3.get("enabled"), False)
        if tier3.get("enabled") is not enabled:
            tier3["enabled"] = enabled
            changed = True

    changed = _delete_nested(payload, ("tiers", "tier3", "bootstrap_sources")) or changed
    changed = _delete_nested(payload, ("tiers", "tier3", "bootstrap_defaults_initialized")) or changed

    if changed:
        local_config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _routing_rule_entry(payload: dict[str, Any], key: str) -> tuple[bool, Any]:
    routing = payload.get("routing")
    if not isinstance(routing, dict):
        return False, _MISSING
    rules = routing.get("rules")
    if isinstance(rules, dict) and key in rules:
        return True, rules[key]
    if key in routing:
        return True, routing[key]
    return False, _MISSING


def _routing_compatibility_entries(value: Any) -> tuple[tuple[str, str], ...]:
    return tuple(
        (entry["rule_type"], entry["value"])
        for entry in normalize_routing_compatibility_defaults(value)
    )


def _routing_compatibility_payload(
    entries: tuple[tuple[str, str], ...],
) -> list[dict[str, str]]:
    return [
        {"rule_type": rule_type, "value": value}
        for rule_type, value in entries
    ]


def _routing_compatibility_snapshot(
    config_path: Path,
) -> tuple[bool, tuple[tuple[str, str], ...]] | None:
    """Read compatibility provenance before template defaults are merged.

    ``present`` deliberately stays separate from the normalized tuple so an
    explicit empty override cannot be mistaken for a missing/inherited value.
    ``None`` means the file could not be parsed and migration should leave it
    alone instead of guessing.
    """

    if not config_path.exists():
        return False, ()
    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    present, value = _routing_rule_entry(payload, "compatibility_defaults")
    return present, _routing_compatibility_entries(value if present else [])


def _routing_rule_values(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        entries = value.splitlines()
    elif isinstance(value, (list, tuple)):
        entries = value
    else:
        return ()
    return tuple(str(entry).strip() for entry in entries if str(entry).strip())


def _config_migration_enabled(config_path: Path, key: str) -> bool:
    if not config_path.exists():
        return False
    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    migrations = payload.get("migrations")
    return isinstance(migrations, dict) and coerce_bool(migrations.get(key), False)


def _legacy_routing_direct_values(config_path: Path) -> tuple[str, ...] | None:
    """Snapshot an explicit pre-v2 direct bucket before defaults are merged."""

    if not config_path.exists():
        return None
    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    compatibility_present, _ = _routing_rule_entry(
        payload,
        "compatibility_defaults",
    )
    direct_present, direct_value = _routing_rule_entry(payload, "direct")
    if compatibility_present or not direct_present:
        return None
    return _routing_rule_values(direct_value)


def _set_routing_rule_entry(
    payload: dict[str, Any],
    key: str,
    value: Any,
) -> bool:
    routing = payload.get("routing")
    if routing is None:
        routing = {}
        payload["routing"] = routing
    if not isinstance(routing, dict):
        return False
    rules = routing.get("rules")
    if rules is None:
        rules = {}
        routing["rules"] = rules
    if not isinstance(rules, dict):
        return False
    if rules.get(key, _MISSING) == value:
        return False
    rules[key] = _deep_copy(value)
    return True


def _delete_routing_rule_entry(payload: dict[str, Any], key: str) -> bool:
    routing = payload.get("routing")
    if not isinstance(routing, dict):
        return False
    changed = False
    rules = routing.get("rules")
    if isinstance(rules, dict) and key in rules:
        del rules[key]
        changed = True
    if key in routing:
        del routing[key]
        changed = True
    return changed


def _normalize_persisted_compatibility_defaults(
    payload: dict[str, Any],
) -> bool:
    present, value = _routing_rule_entry(payload, "compatibility_defaults")
    if not present:
        return False
    normalized = normalize_routing_compatibility_defaults(value)
    return _set_routing_rule_entry(payload, "compatibility_defaults", normalized)


def _is_legacy_cn_direct_default(value: str) -> bool:
    return value.strip().casefold() in {"geosite:cn", "geoip:cn"}


def _remove_legacy_cn_direct_defaults(payload: dict[str, Any]) -> bool:
    present, value = _routing_rule_entry(payload, "direct")
    if not present:
        return False
    entries = _routing_rule_values(value)
    filtered = tuple(
        entry for entry in entries if not _is_legacy_cn_direct_default(entry)
    )
    if filtered == entries:
        return False
    return _set_routing_rule_entry(payload, "direct", list(filtered))


def _migrate_routing_rule_defaults(
    config_path: Path,
    local_config_path: Path,
    template_path: Path,
    *,
    legacy_direct_values: tuple[str, ...] | None = None,
    base_config_existed: bool = True,
    base_compatibility_snapshot: tuple[
        bool,
        tuple[tuple[str, str], ...],
    ]
    | None = None,
    local_compatibility_snapshot: tuple[
        bool,
        tuple[tuple[str, str], ...],
    ]
    | None = None,
) -> None:
    """Migrate editable routing defaults while preserving explicit choices.

    Only the exact historical application default is upgraded in place.
    Explicit base values stay in the base file, explicit local values stay in
    the local file, and a missing value remains distinguishable from ``[]``.
    This preserves layer ownership and makes repeat startup byte-stable.
    """

    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        local_payload = yaml.safe_load(local_config_path.read_text(encoding="utf-8")) or {}
        template_payload = yaml.safe_load(template_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return
    if (
        not isinstance(payload, dict)
        or not isinstance(local_payload, dict)
        or not isinstance(template_payload, dict)
    ):
        return

    base_changed = canonicalize_routing_rule_layout(payload)
    local_changed = canonicalize_routing_rule_layout(local_payload)
    canonicalize_routing_rule_layout(template_payload)
    base_changed = (
        _normalize_persisted_compatibility_defaults(payload) or base_changed
    )
    local_changed = (
        _normalize_persisted_compatibility_defaults(local_payload)
        or local_changed
    )

    migrations = local_payload.get("migrations")
    if not isinstance(migrations, dict):
        migrations = {}
        local_payload["migrations"] = migrations
        local_changed = True

    default_present, default_value = _routing_rule_entry(
        template_payload, "compatibility_defaults"
    )
    default_entries = _routing_compatibility_entries(
        default_value if default_present else []
    )
    if not default_entries:
        return

    if base_compatibility_snapshot is None:
        base_compatibility_snapshot = _routing_compatibility_snapshot(config_path)
    if local_compatibility_snapshot is None:
        local_compatibility_snapshot = _routing_compatibility_snapshot(
            local_config_path
        )
    if (
        base_compatibility_snapshot is None
        or local_compatibility_snapshot is None
    ):
        return

    base_compat_present, base_entries_before_merge = (
        base_compatibility_snapshot
    )
    local_compat_present, _local_entries_before_merge = (
        local_compatibility_snapshot
    )
    # V2 shipped the first two compatibility rows from the then-current
    # template.  Deriving that fingerprint keeps migration data next to the
    # actual ordered defaults instead of duplicating domains in Python.
    v2_entries = default_entries[:2]
    v2_applied = coerce_bool(
        migrations.get(ROUTING_RULE_DEFAULTS_V2_MIGRATION),
        False,
    )
    v3_applied = coerce_bool(
        migrations.get(ROUTING_RULE_DEFAULTS_MIGRATION),
        False,
    )

    legacy_v1_applied = legacy_direct_values is not None or coerce_bool(
        migrations.get(ROUTING_DIRECT_DEFAULTS_MIGRATION),
        False,
    )

    local_override_to_create: tuple[tuple[str, str], ...] | None = None
    if (
        not v2_applied
        and not base_compat_present
        and not local_compat_present
        and legacy_v1_applied
    ):
        local_direct_present, local_direct_value = _routing_rule_entry(
            local_payload, "direct"
        )
        _base_direct_present, base_direct_value = _routing_rule_entry(
            payload, "direct"
        )
        authoritative_direct = (
            legacy_direct_values
            if legacy_direct_values is not None
            else (
                _routing_rule_values(local_direct_value)
                if local_direct_present
                else _routing_rule_values(base_direct_value)
            )
        )
        active_cn_values = {
            entry.casefold()
            for entry in authoritative_direct
            if _is_legacy_cn_direct_default(entry)
        }
        if (
            legacy_direct_values is None
            and not local_direct_present
            and not active_cn_values
        ):
            # v1 deliberately skipped CN injection when custom rules existed;
            # the old hidden stage still applied, so v2 restores that behavior.
            active_cn_values = {
                value.casefold()
                for rule_type, value in v2_entries
                if rule_type == "direct"
                and _is_legacy_cn_direct_default(value)
            }

        base_changed = _remove_legacy_cn_direct_defaults(payload) or base_changed
        local_changed = (
            _remove_legacy_cn_direct_defaults(local_payload) or local_changed
        )

        desired_v2_entries = tuple(
            (rule_type, value)
            for rule_type, value in v2_entries
            if not (
                rule_type == "direct"
                and _is_legacy_cn_direct_default(value)
                and value.casefold() not in active_cn_values
            )
        )
        if desired_v2_entries != v2_entries:
            local_override_to_create = desired_v2_entries

    if base_compat_present:
        # Only the exact historical default fingerprint is application-owned.
        # Every other base value, including [] and reordered/partial/custom
        # rows, remains in the base file where the user put it.
        if (
            base_entries_before_merge == v2_entries
            and not local_compat_present
        ):
            base_changed = (
                _set_routing_rule_entry(
                    payload,
                    "compatibility_defaults",
                    _routing_compatibility_payload(default_entries),
                )
                or base_changed
            )
    elif not base_config_existed or not (v2_applied or v3_applied):
        # New installs and pre-v2 configs inherit the current defaults.  Once
        # v2/v3 has run, a missing base key is durable evidence of deletion and
        # must not be filled back in merely because the marker already exists.
        base_changed = (
            _set_routing_rule_entry(
                payload,
                "compatibility_defaults",
                _routing_compatibility_payload(default_entries),
            )
            or base_changed
        )
    else:
        base_changed = (
            _delete_routing_rule_entry(payload, "compatibility_defaults")
            or base_changed
        )

    if local_override_to_create is not None:
        local_changed = (
            _set_routing_rule_entry(
                local_payload,
                "compatibility_defaults",
                _routing_compatibility_payload(local_override_to_create),
            )
            or local_changed
        )

    if not coerce_bool(
        migrations.get(ROUTING_RULE_DEFAULTS_V2_MIGRATION),
        False,
    ):
        migrations[ROUTING_RULE_DEFAULTS_V2_MIGRATION] = True
        local_changed = True
    if not coerce_bool(
        migrations.get(ROUTING_RULE_DEFAULTS_MIGRATION),
        False,
    ):
        migrations[ROUTING_RULE_DEFAULTS_MIGRATION] = True
        local_changed = True
    if base_changed:
        config_path.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    if local_changed:
        local_config_path.write_text(
            yaml.safe_dump(local_payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )


def _int_literal_equals(value: Any, expected: int) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value == expected
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return False
        try:
            return int(text) == expected
        except ValueError:
            return False
    return False


def _migrate_legacy_tier3_min_alive_default(payload: dict[str, Any], tier3: dict[str, Any]) -> bool:
    migrations = payload.get("migrations")
    changed = False
    if not isinstance(migrations, dict):
        migrations = {}
        payload["migrations"] = migrations
        changed = True
    if coerce_bool(migrations.get(TIER3_MIN_ALIVE_DEFAULT_MIGRATION), False):
        return changed
    if _int_literal_equals(tier3.get("min_alive_nodes", _MISSING), LEGACY_TIER3_MIN_ALIVE_DEFAULT):
        tier3["min_alive_nodes"] = CURRENT_TIER3_MIN_ALIVE_DEFAULT
        changed = True
    migrations[TIER3_MIN_ALIVE_DEFAULT_MIGRATION] = True
    return True


def _local_migrations(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    migrations = payload.get("migrations")
    if isinstance(migrations, dict):
        return migrations, False
    migrations = {}
    payload["migrations"] = migrations
    return migrations, True


def _tier3_profile_value_matches(value: Any, expected: Any) -> bool:
    """Whether ``value`` is the literal the retired Settings switch wrote."""

    if isinstance(expected, str):
        return isinstance(value, str) and value.strip().lower() == expected
    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return False
    if isinstance(value, (int, float)):
        return float(value) == float(expected)
    return False


def _tier3_holds_profile(tier3: dict[str, Any], profile: dict[str, Any]) -> bool:
    return all(
        key in tier3 and _tier3_profile_value_matches(tier3[key], expected)
        for key, expected in profile.items()
    )


def _migrate_tier3_fast_validation_default(payload: dict[str, Any], tier3: dict[str, Any]) -> bool:
    migrations, changed = _local_migrations(payload)
    if coerce_bool(migrations.get(TIER3_FAST_VALIDATION_DEFAULT_MIGRATION), False):
        return changed
    if _tier3_holds_profile(tier3, LEGACY_TIER3_FAST_EXPANSION_PROFILE):
        # Switch on: the same values now ship in the base layer.
        for key in LEGACY_TIER3_FAST_EXPANSION_PROFILE:
            del tier3[key]
    elif _tier3_holds_profile(tier3, LEGACY_TIER3_STANDARD_VALIDATION_PROFILE) and not any(
        key in tier3
        for key in LEGACY_TIER3_FAST_EXPANSION_PROFILE
        if key not in LEGACY_TIER3_STANDARD_VALIDATION_PROFILE
    ):
        # Switch off: an explicit single-slot pin is only ever this exact
        # pair; with any capacity key beside it the layout is hand-written.
        for key in LEGACY_TIER3_STANDARD_VALIDATION_PROFILE:
            del tier3[key]
    migrations[TIER3_FAST_VALIDATION_DEFAULT_MIGRATION] = True
    return True


def _migrate_retired_tier3_bootstrap_switch(payload: dict[str, Any], tier3: dict[str, Any]) -> bool:
    migrations, changed = _local_migrations(payload)
    if coerce_bool(migrations.get(TIER3_BOOTSTRAP_SWITCH_REMOVAL_MIGRATION), False):
        return changed
    tier3.pop("bootstrap_enabled", None)
    migrations[TIER3_BOOTSTRAP_SWITCH_REMOVAL_MIGRATION] = True
    return True


def apply_release_path_overrides(config: dict[str, Any], paths: AppPaths) -> dict[str, Any]:
    patched = _deep_copy(config)
    paths_config = patched.setdefault("paths", {})
    paths_config["sources_file"] = str(paths.sources_path)
    paths_config["tier3_nodes_file"] = str(paths.tier3_nodes_file)
    paths_config["tier2_cache_file"] = str(paths.tier2_cache_file)
    paths_config["exit_cache_file"] = str(paths.exit_cache_file)
    paths_config["tier_observation_cache_file"] = str(paths.tier_observation_cache_file)
    paths_config["tier3_control_state_path"] = str(paths.tier3_control_state_path)
    paths_config["dns_resolver_memory_file"] = str(paths.dns_resolver_memory_file)

    tiers = patched.setdefault("tiers", {})
    tier2 = tiers.setdefault("tier2", {})
    tier2["cache_file"] = str(paths.tier2_cache_file)
    exit_tier = tiers.setdefault("exit", {})
    exit_tier["cache_file"] = str(paths.exit_cache_file)

    alerts = patched.setdefault("alerts", {})
    alerts["log_file"] = str(paths.log_file)

    client = patched.setdefault("client", {})
    client["mihomo_binary_path"] = str(paths.mihomo_binary_path)
    client["mihomo_work_dir"] = str(paths.mihomo_work_dir)
    client["mihomo_runtime_config"] = str(paths.mihomo_runtime_config)
    client["runtime_state_path"] = str(paths.runtime_state_path)
    return patched


def _deep_copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _deep_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_deep_copy(item) for item in value]
    return value
