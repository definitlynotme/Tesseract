from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

from models import Node
from tier1_rows import node_row_meta, tier1_display_rows, tier1_variants_from_status_payload
from tier1_protocols import (
    ordered_tier1_protocol_keys,
    protocol_label,
    protocol_variant_key,
)
from tier3_origin import is_tier3_auto_pool_node
from ui_format import fmt


CONNECTION_DETAIL_KEYS = (
    "port",
    "uuid",
    "password",
    "method",
    "encryption",
    "security",
    "tls",
    "network",
    "flow",
    "fingerprint",
    "alpn",
    "sni",
    "host",
    "path",
    "grpc_service_name",
    "alter_id",
    "scy",
    "public_key",
    "short_id",
    "private_key",
    "allowed_ips",
    "reserved",
    "mtu",
    "ip",
    "ipv6",
    "obfs",
    "obfs_password",
    "udp_relay_mode",
    "congestion_control",
    "skip_cert_verify",
)
NODE_TIER_ORDER = ("tier1", "tier2", "tier3", "exit")
NODE_EDIT_TIER1_MULTIPROTOCOL_FIELDS = frozenset({"name", "address", "port"})
NODE_EDIT_IMPORTED_FIELDS = frozenset({"name", "address", "port", "uri"})
NODE_EDIT_IMPORTED_EDITABLE_FIELDS = frozenset({"name", "uri"})
NODE_EDIT_SUBSCRIPTION_FIELDS = frozenset({"uri"})
NODE_EDIT_TIER1_BASE_FIELDS = frozenset({"name", "address", "port"})
NODE_EDIT_TIER1_LEGACY_LINK_FIELDS = ("vless_link", "hysteria2_link", "naive_link")
SUBSCRIPTION_SOURCE_TYPES = frozenset({"subscription_url", "url"})
SUBSCRIPTION_GROUP_ENTRY_KIND = "subscription_group"
SUBSCRIPTION_NODE_ENTRY_KIND = "subscription_node"
SUBSCRIPTION_GROUP_BACK_ENTRY_KIND = "subscription_group_back"


def _identity(text: str) -> str:
    return text


def config_item_enabled(item: dict[str, Any] | None) -> bool:
    """Return the normalized enabled state for a persisted node config."""
    if not isinstance(item, dict) or "enabled" not in item:
        return True
    value = item.get("enabled")
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off", "disabled"}
    return bool(value)


def node_row_enabled(row: list[Any] | None) -> bool:
    if not isinstance(row, list):
        return True
    meta = node_row_meta(row)
    if "enabled" in meta:
        return config_item_enabled({"enabled": meta.get("enabled")})
    item = meta.get("config_item")
    return config_item_enabled(item if isinstance(item, dict) else None)


def active_rows_before_drafts(rows: list[list[Any]]) -> list[list[Any]]:
    """Keep source order stable while placing disabled drafts last."""
    return sorted(rows, key=lambda row: 0 if node_row_enabled(row) else 1)


def tier1_single_protocol_edit_fields(item: dict[str, Any] | None) -> frozenset[str]:
    visible = set(NODE_EDIT_TIER1_BASE_FIELDS)
    if not isinstance(item, dict):
        return frozenset(visible)
    for key in NODE_EDIT_TIER1_LEGACY_LINK_FIELDS:
        if fmt(item.get(key), "").strip():
            visible.add(key)
    return frozenset(visible)


STATUS_DEFAULT_ACCENT = "#d6e2ee"
STATUS_OK_ACCENT = "#4fd2a0"
STATUS_WARNING_ACCENT = "#f2c84b"
STATUS_ERROR_ACCENT = "#e86b70"


def node_status_accent(
    status: str,
    risk: str = "",
    *,
    risk_marks_error: bool = True,
) -> str:
    if status in {"alive", "running", "active"}:
        return STATUS_OK_ACCENT
    if status == "slow":
        return STATUS_WARNING_ACCENT
    if status in {"dead", "suspicious", "error", "failed"} or (
        risk_marks_error and ("异常" in risk or "Config Error" in risk)
    ):
        return STATUS_ERROR_ACCENT
    return STATUS_DEFAULT_ACCENT


def node_status_display_text(
    status: Any,
    *,
    status_label: Callable[[Any], str],
    translate: Callable[[str], str] = _identity,
) -> str:
    normalized = fmt(status, "").strip().lower()
    if normalized == "disabled":
        return translate("草稿")
    if normalized in {"dead", "suspicious", "error", "failed"}:
        return translate("不可用")
    if normalized in {"", "--", "unknown", "configured"}:
        return translate("待自动检查")
    return status_label(normalized)


def _runtime_failure_display_category(reason: Any) -> tuple[str, str]:
    """Return a bounded, user-facing category for a raw runtime failure.

    The runtime keeps the original exception in its own snapshot and logs.  A
    desktop row must never copy that exception text into a card or detail
    surface; it may expose only one of these stable product categories.
    """

    normalized = fmt(reason, "").strip().casefold()
    if not normalized:
        return "", ""
    if normalized == "latency_limit_exceeded":
        return "latency_limit_exceeded", "延迟超过上限"
    if (
        normalized == "tcp_dns_failed"
        or "getaddrinfo" in normalized
        or "name or service not known" in normalized
        or "no such host" in normalized
        or re.search(
            r"\b(?:errno|winerror)\b[\s:=\[\](){}-]*(?:11001|11003)\b",
            normalized,
        )
    ):
        return "dns_resolution_failed", "域名解析失败"
    if (
        normalized
        in {
            "connection_timeout",
            "connect_timeout",
            "probe_timeout",
            "tcp_connect_timeout",
            "tcp_timeout",
            "timeout",
            "timeout_error",
        }
        or "timed out" in normalized
        or "connection timeout" in normalized
        or "connect timeout" in normalized
        or "deadline exceeded" in normalized
        or "i/o timeout" in normalized
        or "io timeout" in normalized
        or re.search(
            r"\b(?:errno|winerror)\b[\s:=\[\](){}-]*10060\b",
            normalized,
        )
    ):
        return "connection_timeout", "连接超时"
    if (
        normalized
        in {
            "connection_refused",
            "connect_refused",
            "tcp_connection_refused",
            "tcp_refused",
        }
        or "connection refused" in normalized
        or "actively refused" in normalized
        or re.search(
            r"\b(?:errno|winerror)\b[\s:=\[\](){}-]*(?:111|10061)\b",
            normalized,
        )
    ):
        return "connection_refused", "连接被拒绝"
    if (
        normalized
        in {
            "certificate_failed",
            "ssl_error",
            "ssl_failed",
            "tls_error",
            "tls_failed",
            "tls_handshake_failed",
        }
        or "tls" in normalized
        or "ssl" in normalized
        or "x509" in normalized
        or "certificate" in normalized
    ):
        return "tls_failed", "TLS 连接失败"
    return "node_unavailable", "节点不可用"


def node_identity(node: Node) -> str:
    return "|".join([str(node.tier), *node.dedupe_key])


def _extra_value(extra: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = extra.get(name)
        if value not in (None, ""):
            return value
    return None


def node_connection_snapshot(node: Node) -> dict[str, Any]:
    extra = node.extra if isinstance(node.extra, dict) else {}
    return {
        "uri": node.uri,
        "uuid": node.uuid,
        "password": node.password,
        "method": node.method,
        "encryption": node.encryption,
        "security": node.security,
        "network": node.network,
        "host": node.host,
        "path": node.path,
        "sni": node.sni,
        "tls": bool(node.tls) if node.tls is not None else None,
        "flow": _extra_value(extra, "flow"),
        "fingerprint": _extra_value(extra, "fp", "client-fingerprint", "fingerprint"),
        "alpn": _extra_value(extra, "alpn"),
        "alter_id": _extra_value(extra, "aid"),
        "scy": _extra_value(extra, "scy"),
        "public_key": _extra_value(extra, "public-key", "pbk", "publickey"),
        "short_id": _extra_value(extra, "sid", "short-id"),
        "private_key": node.password if node.protocol == "wireguard" else None,
        "ip": _extra_value(extra, "ip"),
        "ipv6": _extra_value(extra, "ipv6"),
        "allowed_ips": _extra_value(extra, "allowed-ips", "allowed_ips", "allowedips"),
        "reserved": _extra_value(extra, "reserved"),
        "mtu": _extra_value(extra, "mtu"),
        "obfs": _extra_value(extra, "obfs"),
        "obfs_password": _extra_value(extra, "obfs-password", "obfs_password"),
        "udp_relay_mode": _extra_value(extra, "udp-relay-mode", "udp_relay_mode"),
        "congestion_control": _extra_value(extra, "congestion-controller", "congestion_control", "congestion_controller"),
        "skip_cert_verify": _extra_value(extra, "skip-cert-verify"),
        "grpc_service_name": _extra_value(extra, "grpc-service-name", "serviceName", "grpc-service-name"),
    }


def _format_connection_value(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, list):
        return ", ".join(str(item) for item in value if str(item))
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def rendered_connection_detail(connection: Any, *, port: Any = None) -> dict[str, Any]:
    connection_payload = connection if isinstance(connection, dict) else {}
    rendered = {key: _format_connection_value(connection_payload.get(key)) for key in CONNECTION_DETAIL_KEYS}
    rendered["port"] = _format_connection_value(port if port not in (None, "") else connection_payload.get("port"))
    uri = _format_connection_value(connection_payload.get("uri"))
    return {
        "fields": rendered,
        "uri": uri,
        "show_details": any(rendered.values()) or bool(connection_payload.get("uri")),
    }


def config_item_for_node_row(row: list[Any], config_items: list[dict[str, Any]]) -> dict[str, Any] | None:
    meta = node_row_meta(row)
    if meta.get("entry_kind") == SUBSCRIPTION_NODE_ENTRY_KIND:
        return None
    embedded = meta.get("config_item")
    if isinstance(embedded, dict):
        return embedded
    tier = str(row[0] if len(row) > 0 else "")
    name = fmt(row[1] if len(row) > 1 else "")
    address = fmt(meta.get("address"), "")
    port = fmt(meta.get("port"), "")
    physical_name = fmt(meta.get("physical_node"), "")
    candidates = [
        item
        for item in config_items
        if item.get("tier") == tier and item.get("kind") == "node"
        and config_item_enabled(item)
    ]
    if tier == "tier1":
        for item in candidates:
            if item.get("key") in {physical_name, name}:
                return item
    strong_matches = []
    for item in candidates:
        if fmt(item.get("label")) != name:
            continue
        item_address = fmt(item.get("address"), "")
        item_port = fmt(item.get("port"), "")
        if address and item_address and address != item_address:
            continue
        if port and item_port and port != item_port:
            continue
        strong_matches.append(item)
    if len(strong_matches) == 1:
        return strong_matches[0]
    label_matches = [item for item in candidates if fmt(item.get("label")) == name]
    return label_matches[0] if len(label_matches) == 1 else None


def node_row_key(row: list[Any]) -> str:
    meta = node_row_meta(row)
    entry_kind = meta.get("entry_kind")
    if entry_kind == SUBSCRIPTION_GROUP_ENTRY_KIND:
        config_item = meta.get("config_item")
        return subscription_group_key(config_item if isinstance(config_item, dict) else None)
    if entry_kind == SUBSCRIPTION_GROUP_BACK_ENTRY_KIND:
        return "|".join(
            [
                fmt(row[0] if len(row) > 0 else ""),
                SUBSCRIPTION_GROUP_BACK_ENTRY_KIND,
                fmt(meta.get("subscription_source"), ""),
            ]
        )
    config_item = meta.get("config_item")
    if isinstance(config_item, dict):
        return "|".join(
            [
                fmt(config_item.get("tier")),
                fmt(config_item.get("kind")),
                fmt(config_item.get("key")),
            ]
        )
    return "|".join(
        [
            fmt(row[0] if len(row) > 0 else ""),
            fmt((meta.get("physical_node") or row[1]) if len(row) > 1 else ""),
            fmt(row[2] if len(row) > 2 else ""),
            fmt(row[3] if len(row) > 3 else ""),
        ]
    )


def node_row_display_key(row: list[Any]) -> str:
    return "|".join(fmt(value) for value in row[:4])


def rows_for_tier(rows: list[list[Any]], tier: str) -> list[list[Any]]:
    return [row for row in rows if len(row) > 0 and str(row[0]) == tier]


def subscription_config_url(item: dict[str, Any] | None) -> str:
    if not isinstance(item, dict):
        return ""
    return fmt(item.get("url") or item.get("uri") or item.get("key"), "").strip()


def subscription_group_key(item: dict[str, Any] | None) -> str:
    return "|".join(
        [
            fmt(item.get("tier") if isinstance(item, dict) else ""),
            SUBSCRIPTION_GROUP_ENTRY_KIND,
            subscription_config_url(item),
        ]
    )


def subscription_group_items_for_tier(tier: str, config_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if tier not in {"tier2", "tier3", "exit"}:
        return []
    return [
        item
        for item in config_items
        if item.get("tier") == tier
        and item.get("kind") == "subscription"
        and subscription_config_url(item)
    ]


def node_row_source_values(row: list[Any]) -> tuple[str, ...]:
    meta = node_row_meta(row)
    values: list[str] = []
    for key in ("source", "source_value", "subscription_url"):
        value = fmt(meta.get(key), "").strip()
        if value and value not in values:
            values.append(value)
    sources = meta.get("sources")
    if isinstance(sources, (list, tuple, set)):
        for source in sources:
            value = fmt(source, "").strip()
            if value and value not in values:
                values.append(value)
    return tuple(values)


def row_matches_subscription_config(row: list[Any], item: dict[str, Any]) -> bool:
    if not config_item_enabled(item):
        return False
    url = subscription_config_url(item)
    if not url:
        return False
    tier = fmt(item.get("tier"), "")
    if not row or fmt(row[0] if len(row) > 0 else "") != tier:
        return False
    meta = node_row_meta(row)
    source_type = fmt(meta.get("source_type"), "").strip()
    if source_type and source_type not in SUBSCRIPTION_SOURCE_TYPES:
        return False
    if tier == "tier3":
        origin = fmt(meta.get("tier3_origin") or meta.get("source_origin"), "").strip()
        if origin != "tier3a":
            return False
    return url in node_row_source_values(row)


def row_has_subscription_source(row: list[Any]) -> bool:
    meta = node_row_meta(row)
    source_type = fmt(meta.get("source_type"), "").strip()
    if source_type in SUBSCRIPTION_SOURCE_TYPES:
        return True
    if fmt(meta.get("subscription_source") or meta.get("subscription_url"), "").strip():
        return True
    return any(value.startswith(("http://", "https://")) for value in node_row_source_values(row))


def subscription_pending_recheck_row(row: list[Any]) -> list[Any]:
    """Return a membership-only display copy of a subscription node row.

    The formal row may retain a previous self-check on disk or in Runtime.  A
    stopped/new Core generation is not allowed to present that observation as
    current UI health, so only volatile fields are removed from this copy.
    """

    copied = copy.deepcopy(row)
    while len(copied) < 11:
        copied.append("")
    meta = dict(node_row_meta(copied))
    copied[4] = "configured"
    copied[5] = ""
    copied[6] = ""
    copied[9] = fmt(meta.get("static_risk"), "")
    meta["observation_origin"] = "persisted_cache"
    meta["observed_at"] = ""
    meta["display_pending_recheck"] = True
    for key in (
        "suspicious_reason",
        "failure_reason",
        "check_error",
        "last_error",
    ):
        meta.pop(key, None)
    copied[10] = meta
    return copied


def subscription_runtime_rows_for_item(
    item: dict[str, Any],
    runtime_rows: list[list[Any]],
    *,
    translate: Callable[[str], str] = _identity,
) -> list[list[Any]]:
    url = subscription_config_url(item)
    rows: list[list[Any]] = []
    for row in runtime_rows:
        if not row_matches_subscription_config(row, item):
            continue
        copied = copy.deepcopy(row)
        while len(copied) < 11:
            copied.append("")
        meta = dict(node_row_meta(copied))
        unlocked_groups = meta.get(
            "subscription_display_unlocked_group_keys"
        )
        group_key = subscription_group_key(item)
        group_locked = bool(
            isinstance(unlocked_groups, (list, tuple, set))
            and group_key not in unlocked_groups
        )
        if (
            _normalized_observation_origin(meta.get("observation_origin"))
            == "persisted_cache"
            or group_locked
        ):
            copied = subscription_pending_recheck_row(copied)
        copied[7] = translate("订阅导入")
        copied[8] = url
        meta = dict(node_row_meta(copied))
        meta.pop("config_item", None)
        meta["entry_kind"] = SUBSCRIPTION_NODE_ENTRY_KIND
        meta["subscription_source"] = url
        meta["subscription_label"] = fmt(item.get("label"), "")
        meta["subscription_config_item"] = item
        copied[10] = meta
        rows.append(copied)
    return rows


def _healthy_subscription_row_count(
    rows: list[list[Any]],
    *,
    require_observed_at: bool = False,
) -> int:
    return sum(
        1
        for row in rows
        if (
            not require_observed_at
            or bool(fmt(node_row_meta(row).get("observed_at"), "").strip())
        )
        if fmt(row[4] if len(row) > 4 else "").lower() in {"alive", "running", "active", "slow"}
    )


_NODE_OBSERVATION_ORIGINS = frozenset({"live", "persisted_cache"})


def _normalized_observation_origin(value: Any, *, default: str = "live") -> str:
    normalized = fmt(value, "").strip().lower()
    # Older runtime payloads may still carry the former stopped-Core
    # ``offline_refresh`` marker; render those legacy rows with neutral cache
    # semantics. Current manual checks update live manager health directly.
    if normalized == "offline_refresh":
        return "persisted_cache"
    return normalized if normalized in _NODE_OBSERVATION_ORIGINS else default


def _subscription_group_observation(child_rows: list[list[Any]]) -> tuple[str, str]:
    if not child_rows:
        return "", ""
    observations = [
        (
            _normalized_observation_origin(node_row_meta(row).get("observation_origin")),
            fmt(node_row_meta(row).get("observed_at"), "").strip(),
        )
        for row in child_rows
    ]
    origin = next(
        (
            candidate
            for candidate in ("persisted_cache", "live")
            if any(item_origin == candidate for item_origin, _observed_at in observations)
        ),
        "live",
    )
    observed_values = [
        observed_at
        for item_origin, observed_at in observations
        if item_origin == origin and observed_at
    ]
    return origin, max(observed_values, default="")


def subscription_group_row(
    item: dict[str, Any],
    child_rows: list[list[Any]],
    *,
    translate: Callable[[str], str] = _identity,
    expanded: bool = False,
) -> list[Any]:
    url = subscription_config_url(item)
    enabled = config_item_enabled(item)
    total = len(child_rows)
    observation_origin, observed_at = _subscription_group_observation(child_rows)
    pending_recheck = observation_origin == "persisted_cache"
    visible_child_rows = [
        subscription_pending_recheck_row(child)
        if pending_recheck
        else child
        for child in child_rows
    ]
    healthy = (
        0
        if pending_recheck
        else _healthy_subscription_row_count(visible_child_rows)
    )
    if pending_recheck:
        observed_at = ""
    status = (
        "disabled"
        if not enabled
        else "configured"
        if observation_origin == "persisted_cache"
        else "alive"
        if healthy
        else "configured"
    )
    meta = {
        "tier": fmt(item.get("tier"), ""),
        "name": fmt(item.get("label"), ""),
        "protocol": translate("订阅分组"),
        "config_item": item,
        "entry_kind": SUBSCRIPTION_GROUP_ENTRY_KIND,
        "subscription_group": True,
        "subscription_group_expanded": expanded,
        "subscription_source": url,
        "subscription_child_count": total,
        "subscription_healthy_count": healthy,
        "subscription_child_preview": tuple(
            {
                "name": fmt(child[1] if len(child) > 1 else ""),
                "status": fmt(child[4] if len(child) > 4 else ""),
                "latency": layer_node_card_latency_label(child),
                "enabled": node_row_enabled(child),
                "observation_origin": _normalized_observation_origin(
                    node_row_meta(child).get("observation_origin")
                ),
                "observed_at": fmt(node_row_meta(child).get("observed_at"), "").strip(),
            }
            for child in visible_child_rows[:3]
        ),
        "observation_origin": observation_origin,
        "observed_at": observed_at,
        "enabled": enabled,
    }
    return [
        fmt(item.get("tier"), ""),
        fmt(item.get("label"), ""),
        translate("订阅分组"),
        fmt(item.get("detail") or url),
        status,
        "--",
        "--",
        translate("订阅"),
        url,
        "--",
        meta,
    ]


def subscription_group_back_row(
    tier: str,
    item: dict[str, Any],
    *,
    translate: Callable[[str], str] = _identity,
) -> list[Any]:
    url = subscription_config_url(item)
    return [
        tier,
        fmt(item.get("label"), "") or translate("订阅分组"),
        translate("订阅分组"),
        fmt(item.get("detail") or url),
        "configured",
        "--",
        "--",
        translate("订阅"),
        url,
        "--",
        {
            "tier": tier,
            "name": fmt(item.get("label"), "") or translate("订阅分组"),
            "protocol": translate("订阅分组"),
            "entry_kind": SUBSCRIPTION_GROUP_BACK_ENTRY_KIND,
            "subscription_group_key": subscription_group_key(item),
            "subscription_source": url,
            "config_item": item,
        },
    ]


def subscription_group_rows_for_tier(
    tier: str,
    node_rows: list[list[Any]],
    config_items: list[dict[str, Any]],
    *,
    translate: Callable[[str], str] = _identity,
) -> list[list[Any]]:
    runtime_rows = rows_for_tier(node_rows, tier)
    rows: list[list[Any]] = []
    for item in subscription_group_items_for_tier(tier, config_items):
        children = subscription_runtime_rows_for_item(item, runtime_rows, translate=translate)
        rows.append(subscription_group_row(item, children, translate=translate))
    return rows


def subscription_group_detail_rows(
    tier: str,
    item: dict[str, Any],
    node_rows: list[list[Any]],
    *,
    translate: Callable[[str], str] = _identity,
) -> list[list[Any]]:
    runtime_rows = rows_for_tier(node_rows, tier)
    children = subscription_runtime_rows_for_item(item, runtime_rows, translate=translate)
    back_row = subscription_group_back_row(tier, item, translate=translate)
    return [back_row, *children]


def is_subscription_group_row(row: list[Any] | None) -> bool:
    if not isinstance(row, list):
        return False
    return node_row_meta(row).get("entry_kind") == SUBSCRIPTION_GROUP_ENTRY_KIND


def is_subscription_group_back_row(row: list[Any] | None) -> bool:
    if not isinstance(row, list):
        return False
    return node_row_meta(row).get("entry_kind") == SUBSCRIPTION_GROUP_BACK_ENTRY_KIND


def is_subscription_node_row(row: list[Any] | None) -> bool:
    if not isinstance(row, list):
        return False
    return node_row_meta(row).get("entry_kind") == SUBSCRIPTION_NODE_ENTRY_KIND


def filtered_node_rows(rows: list[list[Any]], *, tier_filter: Any = "all", status_filter: Any = "all") -> list[list[Any]]:
    selected_tier = str(tier_filter or "all")
    selected_status = str(status_filter or "all")
    filtered = []
    for row in rows:
        tier = str(row[0]) if len(row) > 0 else ""
        status = str(row[4]) if len(row) > 4 else ""
        if selected_tier != "all" and tier != selected_tier:
            continue
        if selected_status != "all" and status != selected_status:
            continue
        filtered.append(row)
    return filtered


def route_tier_row_groups(
    rows: list[list[Any]],
    *,
    tier_order: tuple[str, ...] = NODE_TIER_ORDER,
) -> dict[str, list[list[Any]]]:
    grouped: dict[str, list[list[Any]]] = {tier: [] for tier in tier_order}
    for row in rows:
        tier = str(row[0]) if len(row) > 0 else ""
        if tier in grouped:
            grouped[tier].append(row)
    return grouped


def node_rows_by_display_tier(
    rows: list[list[Any]],
    *,
    tier_order: tuple[str, ...] = NODE_TIER_ORDER,
) -> list[tuple[str, list[list[Any]]]]:
    grouped: dict[str, list[list[Any]]] = {}
    for row in rows:
        tier = fmt(row[0] if len(row) > 0 else "")
        grouped.setdefault(tier, []).append(row)

    ordered: list[tuple[str, list[list[Any]]]] = []
    for tier in tier_order:
        tier_rows = grouped.pop(tier, [])
        if tier_rows:
            ordered.append((tier, tier_rows))
    for tier in sorted(grouped):
        ordered.append((tier, grouped[tier]))
    return ordered


def _layer_node_signature_value(value: Any) -> Any:
    """Freeze nested row metadata so card-only changes invalidate the view."""
    if isinstance(value, dict):
        return tuple(
            sorted(
                (fmt(key), _layer_node_signature_value(item))
                for key, item in value.items()
            )
        )
    if isinstance(value, (list, tuple)):
        return tuple(_layer_node_signature_value(item) for item in value)
    if isinstance(value, set):
        return tuple(
            sorted(
                (_layer_node_signature_value(item) for item in value),
                key=repr,
            )
        )
    return fmt(value)


def layer_node_rows_signature(tier: str, rows: list[list[Any]]) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (tier, *tuple(_layer_node_signature_value(value) for value in row))
        for row in rows
    )


def fallback_order_text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return " -> ".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def parse_fallback_order_text(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    parts = re.split(r"\s*(?:->|,|\n)\s*", text)
    if len(parts) == 1 and " " in text:
        parts = re.split(r"\s+", text)
    return [item.strip() for item in parts if item.strip()]


def protocols_update_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for raw_key, raw_config in value.items():
        key = str(raw_key or "").strip()
        if not key or raw_config is False or raw_config is None:
            continue
        if isinstance(raw_config, dict):
            config = {
                str(field_key).strip(): field_value
                for field_key, field_value in raw_config.items()
                if str(field_key).strip()
            }
            result[key] = config
            continue
        text = str(raw_config or "").strip()
        result[key] = {"uri": text} if text else {}
    return result


def _ordered_protocol_keys(protocols: dict[str, Any], fallback_order: Any) -> list[str]:
    return ordered_tier1_protocol_keys(protocols, fallback_order)


def configured_tier1_protocol_variants(item: dict[str, Any]) -> list[dict[str, Any]]:
    protocols = item.get("protocols", {}) if isinstance(item.get("protocols"), dict) else {}
    if not protocols:
        return []
    fallback_order = list(item.get("fallback_order", []) or [])
    endpoint = fmt(item.get("detail") or f"{fmt(item.get('address'), '')}:{fmt(item.get('port'), '')}")
    return [
        {
            "name": fmt(item.get("label")),
            "protocol": protocol_label(key),
            "protocol_key": key,
            "status": "configured",
            "latency": "--",
            "endpoint": endpoint,
            "identity": "",
            "has_uri": bool(isinstance(protocols.get(key), dict) and protocols.get(key, {}).get("uri")),
            "fallback_order": fallback_order,
            "config": (
                copy.deepcopy(protocols.get(key))
                if isinstance(protocols.get(key), dict)
                else {}
            ),
        }
        for key in _ordered_protocol_keys(protocols, fallback_order)
    ]


def node_row_alive_count(rows: list[list[Any]]) -> int:
    return sum(1 for row in rows if len(row) > 4 and str(row[4]) in {"alive", "slow"})


def node_tier_group_title(
    tier: str,
    rows: list[list[Any]],
    *,
    tier_label: Callable[[Any], str],
    translate: Callable[[str], str] = _identity,
) -> str:
    return f"{tier_label(tier)}  {node_row_alive_count(rows)}/{len(rows)} {translate('可用')}"


def subscription_node_pending_recheck(row: list[Any]) -> bool:
    meta = node_row_meta(row)
    if meta.get("display_pending_recheck") is True:
        return True
    return bool(
        meta.get("entry_kind") == SUBSCRIPTION_NODE_ENTRY_KIND
        and _normalized_observation_origin(meta.get("observation_origin"))
        == "persisted_cache"
    )


def layer_node_item_values(
    row: list[Any],
    *,
    status_label: Callable[[Any], str],
    translate: Callable[[str], str] = _identity,
) -> list[str]:
    status = fmt(row[4] if len(row) > 4 else "")
    pending_recheck = subscription_node_pending_recheck(row)
    status_text = (
        translate("待复检")
        if pending_recheck
        else node_status_display_text(
            status,
            status_label=status_label,
            translate=translate,
        )
    )
    return [
        fmt(row[1] if len(row) > 1 else ""),
        fmt(row[2] if len(row) > 2 else ""),
        status_text,
        "" if pending_recheck else fmt(row[5] if len(row) > 5 else ""),
        fmt(row[3] if len(row) > 3 else ""),
        fmt(row[7] if len(row) > 7 else ""),
        fmt(row[9] if len(row) > 9 else "")
        if pending_recheck
        else "--"
        if status.strip().lower() in {"", "--", "unknown", "configured", "disabled"}
        else fmt(row[9] if len(row) > 9 else ""),
    ]


def node_tree_item_values(
    row: list[Any],
    *,
    status_label: Callable[[Any], str],
    translate: Callable[[str], str] = _identity,
) -> list[str]:
    status = fmt(row[4] if len(row) > 4 else "")
    pending_recheck = subscription_node_pending_recheck(row)
    return [
        fmt(row[1] if len(row) > 1 else ""),
        fmt(row[2] if len(row) > 2 else ""),
        translate("待复检")
        if pending_recheck
        else node_status_display_text(
            status,
            status_label=status_label,
            translate=translate,
        ),
        "" if pending_recheck else fmt(row[5] if len(row) > 5 else ""),
        fmt(row[7] if len(row) > 7 else "0"),
        fmt(row[9] if len(row) > 9 else "")
        if pending_recheck
        else "--"
        if status.strip().lower() in {"", "--", "unknown", "configured", "disabled"}
        else fmt(row[9] if len(row) > 9 else ""),
    ]


def node_detail_values(
    row: list[Any],
    *,
    tier_label: Callable[[Any], str],
    status_label: Callable[[Any], str],
    translate: Callable[[str], str] = _identity,
) -> dict[str, str]:
    raw_status = fmt(row[4] if len(row) > 4 else "")
    pending_recheck = subscription_node_pending_recheck(row)
    status_text = (
        translate("待复检")
        if pending_recheck
        else node_status_display_text(
            raw_status,
            status_label=status_label,
            translate=translate,
        )
    )
    if is_subscription_group_row(row):
        status_text = translate("已禁用" if raw_status.strip().lower() == "disabled" else "已启用")
    last_checked = fmt(row[6] if len(row) > 6 else "", "").strip()
    if pending_recheck:
        last_checked = ""
    elif not last_checked or last_checked == "--":
        last_checked = translate("待自动检查")
    return {
        "tier": tier_label(row[0]) if len(row) > 0 else "--",
        "name": fmt(row[1] if len(row) > 1 else ""),
        "protocol": fmt(row[2] if len(row) > 2 else ""),
        "address": fmt(row[3] if len(row) > 3 else ""),
        "status": status_text,
        "latency": (
            ""
            if pending_recheck
            else fmt(row[5] if len(row) > 5 else "")
        ),
        "last_checked": last_checked,
        "source": fmt(row[7] if len(row) > 7 else ""),
        "subscription": fmt(row[8] if len(row) > 8 else ""),
        "risk": (
            fmt(row[9] if len(row) > 9 else "")
            if pending_recheck
            else "--"
            if raw_status.strip().lower() in {"", "--", "unknown", "configured", "disabled"}
            else fmt(row[9] if len(row) > 9 else "")
        ),
    }


def node_connection_payload(row: list[Any]) -> tuple[dict[str, Any], Any]:
    meta = node_row_meta(row)
    connection = meta.get("connection") if isinstance(meta.get("connection"), dict) else {}
    return connection, meta.get("port")


@dataclass(frozen=True)
class NodeEditViewState:
    tier: str
    kind: str
    editable: bool
    multiprotocol: bool
    values: dict[str, Any]
    visible_fields: frozenset[str]
    editable_fields: frozenset[str]
    original_values: dict[str, Any]
    can_delete: bool
    delete_label: str
    message: str
    can_toggle_enabled: bool
    enabled: bool


@dataclass(frozen=True)
class NodeActionButtonState:
    can_save: bool
    can_delete: bool


@dataclass(frozen=True)
class LayerScopeViewState:
    nodes_title: str
    nodes_minimum_height: int
    tree_headers: list[str]


@dataclass(frozen=True)
class LayerNodeEmptyState:
    title: str
    description: str
    action_text: str


@dataclass(frozen=True)
class NodeProtocolRow:
    priority: str
    protocol: str
    status: str
    status_label: str
    latency: str
    accent: str
    payload: dict[str, Any]

    @property
    def values(self) -> tuple[str, str, str, str]:
        return (self.priority, self.protocol, self.status_label, self.latency)


@dataclass(frozen=True)
class NodeProtocolTreeState:
    visible: bool
    rows: tuple[NodeProtocolRow, ...]


@dataclass(frozen=True)
class NodeTreeItemState:
    values: list[str]
    accent: str
    key: str
    payload: list[Any]


@dataclass(frozen=True)
class SubscriptionNodePreviewState:
    name: str
    status_label: str
    status_state: str
    latency_label: str = ""


@dataclass(frozen=True)
class LayerNodeCardState:
    title: str
    protocol: str
    protocol_count_label: str
    summary: str
    protocol_tooltip: str
    latency_label: str
    status: str
    status_label: str
    status_state: str
    selection_label: str
    key: str
    payload: list[Any]
    endpoint_label: str = ""
    endpoint_tooltip: str = ""
    origin_label: str = ""
    origin_tooltip: str = ""
    protocol_parts: tuple[str, ...] = ()
    latency_semantics: str = "none"
    telemetry_caption: str = ""
    availability_ratio: float = -1.0
    meta_label: str = ""
    meta_tooltip: str = ""
    enabled: bool = True
    entry_kind: str = ""
    source_label: str = ""
    source_tooltip: str = ""
    count_label: str = ""
    health_label: str = ""
    health_state: str = "unknown"
    action_label: str = ""
    compact_status_label: str = ""
    compact_action_label: str = ""
    primary_protocol: str = ""
    kind_tag_label: str = ""
    state_tag_label: str = ""
    state_tag_state: str = "unknown"
    is_current_exit: bool = False
    presentation_key: str = "unknown"
    subscription_node_previews: tuple[SubscriptionNodePreviewState, ...] = ()
    subscription_remaining_count: int = 0


def layer_node_presentation_key(row: list[Any]) -> str:
    """Return the explicit list/detail profile for this node type.

    This is intentionally protocol-aware.  Visual components consume the
    profile instead of inferring one generic card from whichever strings happen
    to be populated.
    """

    meta = node_row_meta(row)
    entry_kind = str(meta.get("entry_kind") or "")
    if entry_kind == SUBSCRIPTION_GROUP_ENTRY_KIND:
        return "subscription_group"
    if entry_kind == SUBSCRIPTION_GROUP_BACK_ENTRY_KIND:
        return "subscription_context"
    variants = meta.get("variants")
    protocol_parts = layer_node_card_protocol_parts(row)
    if (
        isinstance(variants, list)
        and sum(1 for item in variants if isinstance(item, dict)) > 1
    ) or len(protocol_parts) > 1:
        return "tier1_multiprotocol"

    config_item = meta.get("config_item")
    config_protocol = ""
    config_uri = ""
    if isinstance(config_item, dict):
        config_protocol = str(
            config_item.get("protocol")
            or config_item.get("protocol_key")
            or ""
        )
        config_uri = str(
            config_item.get("uri")
            or config_item.get("url")
            or ""
        )
    scheme = config_uri.split("://", 1)[0] if "://" in config_uri else ""
    raw = " ".join(
        part
        for part in (
            scheme,
            config_protocol,
            str(row[2] if len(row) > 2 else ""),
            *protocol_parts,
        )
        if part
    ).casefold()
    normalized = (
        raw.replace("-", "_")
        .replace(" ", "_")
        .replace("/", "_")
    )
    if "argo" in normalized and "xhttp" in normalized:
        return "argo_xhttp"
    if "vless" in normalized and "reality" in normalized:
        return "vless_reality"
    if "xhttp" in normalized:
        return "xhttp"
    if "wireguard" in normalized or normalized.startswith("wg"):
        return "wireguard"
    if "hysteria2" in normalized or "hy2" in normalized:
        return "hysteria2"
    if "anytls" in normalized:
        return "anytls"
    if "shadowsocksr" in normalized or "ssr" in normalized:
        return "ssr"
    if "shadowsocks" in normalized or normalized.startswith("ss_"):
        return "ss"
    if "socks5" in normalized or "socks" in normalized:
        return "socks5"
    if "vmess" in normalized:
        return "vmess"
    if "trojan" in normalized:
        return "trojan"
    if "tuic" in normalized:
        return "tuic"
    if "naive" in normalized:
        return "naive"
    if "https" in normalized:
        return "https"
    if "http" in normalized:
        return "http"
    if "vless" in normalized:
        return "vless"
    return "unknown"


def node_protocol_tree_state(
    row: list[Any] | None,
    *,
    status_label: Callable[[Any], str] = fmt,
    translate: Callable[[str], str] = _identity,
) -> NodeProtocolTreeState:
    variants: list[dict[str, Any]] = []
    if isinstance(row, list):
        raw_variants = node_row_meta(row).get("variants")
        if isinstance(raw_variants, list):
            variants = [item for item in raw_variants if isinstance(item, dict)]
    visible = len(variants) > 1
    if not visible:
        return NodeProtocolTreeState(visible=False, rows=())
    rows: list[NodeProtocolRow] = []
    for index, variant in enumerate(variants, start=1):
        status = fmt(variant.get("status"), "")
        payload = dict(variant)
        payload["priority"] = str(index)
        rows.append(
            NodeProtocolRow(
                priority=str(index),
                protocol=fmt(variant.get("protocol")),
                status=status,
                status_label=node_status_display_text(
                    status,
                    status_label=status_label,
                    translate=translate,
                ),
                latency=fmt(variant.get("latency")),
                accent=node_status_accent(status, risk_marks_error=False),
                payload=payload,
            )
        )
    return NodeProtocolTreeState(visible=True, rows=tuple(rows))


def node_protocol_detail_lines(
    protocol: Any,
    *,
    translate: Callable[[str], str] = _identity,
    status_label: Callable[[Any], str] = fmt,
) -> tuple[str, ...]:
    return ()


def layer_node_tree_item_state(
    row: list[Any],
    *,
    status_label: Callable[[Any], str],
    translate: Callable[[str], str] = _identity,
) -> NodeTreeItemState:
    status = fmt(row[4] if len(row) > 4 else "")
    risk = fmt(row[9] if len(row) > 9 else "")
    return NodeTreeItemState(
        values=layer_node_item_values(row, status_label=status_label, translate=translate),
        accent=node_status_accent(status, risk),
        key=node_row_key(row),
        payload=row,
    )


def layer_node_status_state(status: str) -> str:
    normalized = fmt(status, "").lower()
    if normalized in {"alive", "running", "active"}:
        return "ok"
    if normalized == "slow":
        return "warning"
    if normalized in {"dead", "suspicious", "error", "failed"}:
        return "error"
    return "unknown"


def _compact_value(value: Any) -> str:
    return fmt(value, "").strip()


def layer_node_card_protocol_summary(
    row: list[Any],
    *,
    translate: Callable[[str], str] = _identity,
) -> tuple[str, str, str]:
    def compact_labels(labels: list[str], fallback: str) -> tuple[str, str, str]:
        cleaned = [label for label in labels if label]
        if not cleaned:
            return fallback, "", fallback
        chain = " -> ".join(cleaned)
        visible = " / ".join(cleaned)
        return visible, "", chain

    protocol = fmt(row[2] if len(row) > 2 else "")
    variants = [item for item in node_row_meta(row).get("variants", []) if isinstance(item, dict)]
    if len(variants) <= 1:
        if " -> " in protocol:
            labels = [part.strip() for part in protocol.split(" -> ") if part.strip()]
            if len(labels) > 1:
                return compact_labels(labels, protocol)
        return protocol, "", protocol
    labels = [fmt(item.get("protocol")) for item in variants if fmt(item.get("protocol"))]
    return compact_labels(labels, protocol)


def layer_node_card_protocol_parts(row: list[Any]) -> tuple[str, ...]:
    protocol = fmt(row[2] if len(row) > 2 else "", "").strip()
    variants = [item for item in node_row_meta(row).get("variants", []) if isinstance(item, dict)]
    labels = tuple(
        label
        for label in (fmt(item.get("protocol"), "").strip() for item in variants)
        if label
    )
    if len(labels) > 1:
        return labels
    if " -> " in protocol:
        parts = tuple(part.strip() for part in protocol.split(" -> ") if part.strip())
        if parts:
            return parts
    return (protocol,) if protocol and protocol != "--" else ()


def layer_node_card_primary_protocol(
    protocol_parts: tuple[str, ...],
    *,
    fallback: str = "",
) -> str:
    """Return the single compact protocol label used by layer-node rows."""
    if not protocol_parts:
        rendered_fallback = fmt(fallback, "").strip()
        return "" if rendered_fallback == "--" else rendered_fallback
    extra_count = len(protocol_parts) - 1
    return protocol_parts[0] if extra_count <= 0 else f"{protocol_parts[0]} +{extra_count}"


def layer_node_card_latency_label(row: list[Any]) -> str:
    value = fmt(row[5] if len(row) > 5 else "", "").strip()
    if not value or value == "--":
        return ""
    normalized = value.lower().replace(" ", "")
    if normalized.endswith("ms") or not normalized.replace(".", "", 1).isdigit():
        return value
    return f"{value} ms"


def layer_node_card_latency_semantics(
    *,
    enabled: bool,
    status_state: str,
    latency_label: str,
) -> str:
    if not enabled or not latency_label or status_state in {"draft", "unknown", "subscription"}:
        return "none"
    return "last" if status_state == "error" else "live"


def layer_node_card_state_tag(
    *,
    enabled: bool,
    status_state: str,
    is_current_exit: bool,
    translate: Callable[[str], str] = _identity,
) -> tuple[str, str]:
    """Normalize operational truth into the card's one visible status tag."""
    if not enabled:
        return translate("草稿"), "draft"
    if is_current_exit:
        if status_state == "error":
            return f"{translate('当前')} · {translate('故障')}", "error"
        if status_state == "warning":
            return f"{translate('当前')} · {translate('慢速')}", "warning"
        if status_state == "unknown":
            return f"{translate('当前')} · {translate('待检查')}", "unknown"
        return translate("当前"), "ok"
    if status_state == "ok":
        return translate("可用"), "ok"
    if status_state == "warning":
        return translate("慢速"), "warning"
    if status_state == "error":
        return translate("不可用"), "error"
    return translate("待检查"), "unknown"


def subscription_source_display(source: str) -> str:
    rendered = fmt(source, "").strip()
    if not rendered:
        return ""
    try:
        parsed = urlsplit(rendered)
    except ValueError:
        return rendered
    return parsed.hostname or rendered


def node_detail_field_visible(
    key: str,
    value: Any,
    *,
    translate: Callable[[str], str] = _identity,
) -> bool:
    text = _compact_value(value)
    if not text or text == "--":
        return False
    if key == "risk":
        return text not in {"正常", "Normal", translate("正常")}
    if key == "source":
        return text not in {"配置", "Config", "Settings", translate("配置"), "用户导入", "User Imported", translate("用户导入")}
    return True


def layer_node_card_state(
    row: list[Any],
    *,
    status_label: Callable[[Any], str],
    translate: Callable[[str], str] = _identity,
) -> LayerNodeCardState:
    meta = node_row_meta(row)
    status = fmt(row[4] if len(row) > 4 else "")
    enabled = node_row_enabled(row)
    gateway_effective = bool(meta.get("gateway_effective")) and enabled
    entry_kind = meta.get("entry_kind")
    if entry_kind == SUBSCRIPTION_GROUP_BACK_ENTRY_KIND:
        source = fmt(row[8] if len(row) > 8 else "")
        display_source = fmt(row[3] if len(row) > 3 else "") or source
        back_label = translate("返回节点列表")
        return LayerNodeCardState(
            title=fmt(row[1] if len(row) > 1 else ""),
            protocol=fmt(row[2] if len(row) > 2 else ""),
            protocol_count_label="",
            summary=display_source,
            protocol_tooltip=source,
            latency_label="",
            status=status,
            status_label=back_label,
            status_state="unknown",
            selection_label=translate("当前"),
            key=node_row_key(row),
            payload=row,
            endpoint_label=display_source,
            endpoint_tooltip=display_source,
            origin_label=source,
            origin_tooltip=source,
            entry_kind=SUBSCRIPTION_GROUP_BACK_ENTRY_KIND,
            source_label=source,
            source_tooltip=source,
            action_label=translate("管理订阅"),
            compact_status_label=translate("返回"),
            compact_action_label=translate("管理"),
            primary_protocol=translate("订阅"),
            state_tag_label=back_label,
            state_tag_state="unknown",
            presentation_key="subscription_context",
        )
    if entry_kind == SUBSCRIPTION_GROUP_ENTRY_KIND:
        total = int(meta.get("subscription_child_count", 0) or 0)
        healthy = int(meta.get("subscription_healthy_count", 0) or 0)
        observation_origin = _normalized_observation_origin(
            meta.get("observation_origin"),
        )
        observed_at = fmt(meta.get("observed_at"), "").strip()
        source = fmt(meta.get("subscription_source") or (row[8] if len(row) > 8 else ""), "")
        source_display = subscription_source_display(source)
        display_source = fmt(row[3] if len(row) > 3 else "") or source
        ratio_label = f"{healthy}/{total}"
        count_label = translate("未加载") if not enabled else str(total)
        health_label = (
            f"{translate('本次')} {ratio_label}"
            if enabled and total > 0
            else ratio_label
        )
        health_state = (
            "unknown"
            if not enabled or total <= 0
            else "ok"
            if healthy >= total
            else "warning"
            if healthy > 0
            else "error"
        )
        availability_ratio = (healthy / total) if enabled and total > 0 else -1.0
        card_status_label = translate("订阅") if enabled else translate("已禁用")
        group_tag_label = translate("可用") if enabled else translate("已禁用")
        group_tag_state = "ok" if enabled else "draft"
        summary_metric = ratio_label
        if not enabled:
            health_label = translate("未加载")
        elif total <= 0:
            card_status_label = translate("待更新")
            group_tag_label = translate("待更新")
            group_tag_state = "unknown"
            health_label = translate("未加载")
            health_state = "unknown"
            availability_ratio = -1.0
            summary_metric = health_label
        elif enabled and observation_origin == "persisted_cache":
            card_status_label = translate("待复检")
            group_tag_label = translate("待复检")
            group_tag_state = "unknown"
            health_label = translate("{count} 个节点").format(count=total)
            health_state = "unknown"
            availability_ratio = -1.0
            summary_metric = health_label
        summary = f"{summary_metric} · {display_source}" if display_source else summary_metric
        child_previews: list[SubscriptionNodePreviewState] = []
        raw_child_previews = meta.get("subscription_child_preview")
        if isinstance(raw_child_previews, (list, tuple)):
            for raw_preview in raw_child_previews:
                if not isinstance(raw_preview, dict):
                    continue
                child_enabled = bool(raw_preview.get("enabled", True))
                child_status_state = (
                    layer_node_status_state(raw_preview.get("status"))
                    if child_enabled
                    else "draft"
                )
                preview_origin = _normalized_observation_origin(
                    raw_preview.get("observation_origin"),
                    default=observation_origin,
                )
                if preview_origin == "persisted_cache":
                    child_status_label = translate("待复检")
                    child_tag_state = "unknown"
                else:
                    child_status_label, child_tag_state = layer_node_card_state_tag(
                        enabled=child_enabled,
                        status_state=child_status_state,
                        is_current_exit=False,
                        translate=translate,
                    )
                child_latency = fmt(raw_preview.get("latency"), "").strip()
                if child_latency == "--":
                    child_latency = ""
                child_previews.append(
                    SubscriptionNodePreviewState(
                        name=fmt(raw_preview.get("name"), "") or "--",
                        status_label=child_status_label,
                        status_state=child_tag_state,
                        latency_label=(
                            child_latency
                            if (
                                preview_origin != "persisted_cache"
                                and child_enabled
                                and child_status_state in {"ok", "warning"}
                            )
                            else ""
                        ),
                    )
                )
        return LayerNodeCardState(
            title=fmt(row[1] if len(row) > 1 else ""),
            protocol=fmt(row[2] if len(row) > 2 else ""),
            protocol_count_label="",
            summary=summary,
            protocol_tooltip=f"{summary_metric} · {source}" if source else summary,
            latency_label="",
            status=status,
            status_label=card_status_label,
            status_state=(
                "unknown"
                if enabled
                and (
                    total <= 0
                    or observation_origin == "persisted_cache"
                )
                else "subscription"
                if enabled
                else "draft"
            ),
            selection_label=translate("当前"),
            key=node_row_key(row),
            payload=row,
            endpoint_label=display_source,
            endpoint_tooltip=display_source,
            origin_label=source,
            origin_tooltip=source,
            entry_kind=SUBSCRIPTION_GROUP_ENTRY_KIND,
            source_label=source_display,
            source_tooltip=source,
            count_label=count_label,
            health_label=health_label,
            health_state=health_state,
            availability_ratio=availability_ratio,
            action_label=translate("详情") if enabled else translate("管理"),
            enabled=enabled,
            primary_protocol=translate("订阅"),
            state_tag_label=group_tag_label,
            state_tag_state=group_tag_state,
            presentation_key="subscription_group",
            subscription_node_previews=tuple(child_previews),
            subscription_remaining_count=max(0, total - len(child_previews)),
        )
    protocol, protocol_count_label, protocol_tooltip = layer_node_card_protocol_summary(row, translate=translate)
    protocol_parts = layer_node_card_protocol_parts(row)
    endpoint = fmt(row[3] if len(row) > 3 else "", "")
    tier = fmt(row[0] if len(row) > 0 else "")
    summary_parts = [protocol, protocol_count_label]
    if tier == "exit":
        summary_parts.append(endpoint)
        source = fmt(row[7] if len(row) > 7 else "", "")
        if source and source != "--":
            summary_parts.append(source)
    summary = " · ".join(part for part in summary_parts if part and part != "--")
    source = fmt(
        meta.get("source")
        or meta.get("source_origin")
        or (row[7] if len(row) > 7 else ""),
        "",
    )
    meta_parts = [endpoint]
    if source and source != "--" and source != endpoint:
        meta_parts.append(source)
    meta_label = " · ".join(part for part in meta_parts if part and part != "--")
    status_state = layer_node_status_state(status) if enabled else "draft"
    latency_label = layer_node_card_latency_label(row) if enabled else ""
    persisted_subscription_observation = bool(
        enabled
        and entry_kind == SUBSCRIPTION_NODE_ENTRY_KIND
        and _normalized_observation_origin(
            meta.get("observation_origin")
        )
        == "persisted_cache"
    )
    if persisted_subscription_observation:
        status_state = "unknown"
        latency_semantics = "none"
    else:
        latency_semantics = layer_node_card_latency_semantics(
            enabled=enabled,
            status_state=status_state,
            latency_label=latency_label,
        )
    if latency_semantics == "none":
        latency_label = ""
    selection_label = translate("当前") if gateway_effective and enabled else ""
    if persisted_subscription_observation:
        state_tag_label = translate("待复检")
        state_tag_state = "unknown"
    else:
        state_tag_label, state_tag_state = layer_node_card_state_tag(
            enabled=enabled,
            status_state=status_state,
            is_current_exit=gateway_effective,
            translate=translate,
        )
    presentation_key = layer_node_presentation_key(row)
    return LayerNodeCardState(
        title=fmt(row[1] if len(row) > 1 else ""),
        protocol=protocol,
        protocol_count_label=protocol_count_label,
        summary=summary,
        protocol_tooltip=protocol_tooltip,
        latency_label=latency_label,
        status=status,
        status_label=(
            state_tag_label
            if persisted_subscription_observation
            else node_status_display_text(
                status,
                status_label=status_label,
                translate=translate,
            )
            if enabled
            else translate("草稿")
        ),
        status_state=status_state,
        selection_label=selection_label,
        key=node_row_key(row),
        payload=row,
        endpoint_label=endpoint,
        endpoint_tooltip=endpoint,
        origin_label=source if source != endpoint else "",
        origin_tooltip=source if source != endpoint else "",
        protocol_parts=protocol_parts,
        latency_semantics=latency_semantics,
        telemetry_caption=(
            translate("待复检")
            if persisted_subscription_observation
            else selection_label
            if enabled
            else translate("不参与运行")
        ),
        meta_label=meta_label,
        meta_tooltip=meta_label,
        enabled=enabled,
        entry_kind=fmt(entry_kind, "node"),
        action_label=translate("查看详情"),
        primary_protocol=layer_node_card_primary_protocol(protocol_parts, fallback=protocol),
        kind_tag_label=(
            translate("多协议")
            if presentation_key == "tier1_multiprotocol"
            else ""
        ),
        state_tag_label=state_tag_label,
        state_tag_state=state_tag_state,
        is_current_exit=gateway_effective,
        presentation_key=presentation_key,
    )


def layer_node_empty_state(
    tier: str,
    *,
    translate: Callable[[str], str] = _identity,
) -> LayerNodeEmptyState:
    normalized = str(tier or "").strip().lower()
    if normalized == "exit":
        keys = (
            "暂无出口节点",
            "导入出口节点后，可在这里选择当前 Gateway。",
            "导入出口节点",
        )
    elif normalized == "tier3":
        keys = (
            "暂无用户自定义 Bastion 来源",
            "添加 Bastion 来源后，订阅与节点会显示在这里。",
            "添加 Bastion 来源",
        )
    else:
        keys = (
            "没有可用节点",
            "添加节点或订阅后，可在这里查看状态与配置。",
            "添加单节点或订阅",
        )
    return LayerNodeEmptyState(*(translate(key) for key in keys))


def layer_node_empty_text(tier: str, *, translate: Callable[[str], str] = _identity) -> str:
    return layer_node_empty_state(tier, translate=translate).title


def layer_node_empty_description(
    tier: str,
    *,
    translate: Callable[[str], str] = _identity,
) -> str:
    return layer_node_empty_state(tier, translate=translate).description


def layer_node_empty_action_text(
    tier: str,
    *,
    translate: Callable[[str], str] = _identity,
) -> str:
    return layer_node_empty_state(tier, translate=translate).action_text


def node_tree_item_state(
    row: list[Any],
    *,
    status_label: Callable[[Any], str],
    translate: Callable[[str], str] = _identity,
) -> NodeTreeItemState:
    status = fmt(row[4] if len(row) > 4 else "")
    risk = fmt(row[9] if len(row) > 9 else "")
    return NodeTreeItemState(
        values=node_tree_item_values(row, status_label=status_label, translate=translate),
        accent=node_status_accent(status, risk),
        key=node_row_display_key(row),
        payload=row,
    )


def layer_scope_view_state(layer: str, *, translate: Callable[[str], str] = _identity) -> LayerScopeViewState:
    if layer == "exit":
        return LayerScopeViewState(
            nodes_title="",
            nodes_minimum_height=230,
            tree_headers=[
                translate("名称"),
                translate("协议"),
                translate("状态"),
                translate("延迟"),
                translate("地址"),
                translate("来源"),
                translate("风险"),
            ],
        )
    tier3 = layer == "tier3"
    return LayerScopeViewState(
        nodes_title="",
        nodes_minimum_height=150 if tier3 else 230,
        tree_headers=(
            [
                translate("名称"),
                translate("类型"),
                translate("状态"),
                translate("延迟"),
                translate("地址 / URL"),
                translate("来源"),
                translate("风险"),
            ]
            if tier3
            else [
                translate("名称"),
                translate("协议"),
                translate("状态"),
                translate("延迟"),
                translate("地址"),
                translate("来源"),
                translate("风险"),
            ]
        ),
    )


def node_action_button_state(
    item: dict[str, Any] | None,
    *,
    edit_has_changes: bool,
    busy_tasks: set[str] | frozenset[str],
) -> NodeActionButtonState:
    can_edit = bool(item and item.get("editable") and edit_has_changes)
    can_delete = bool(item and item.get("kind") in {"node", "subscription"})
    return NodeActionButtonState(
        can_save=can_edit and "save-node-config" not in busy_tasks,
        can_delete=can_delete and "delete-config-item" not in busy_tasks,
    )


def node_edit_view_state(
    item: dict[str, Any] | None,
    row: list[Any] | None,
    *,
    translate: Callable[[str], str] = _identity,
) -> NodeEditViewState:
    tier = str(item.get("tier", "") if item else row[0] if row and len(row) > 0 else "")
    kind = str(item.get("kind", "") if item else "")
    editable = bool(item and item.get("editable"))
    can_toggle_enabled = bool(
        item
        and editable
        and (
            (kind == "node" and tier in NODE_TIER_ORDER)
            or (kind == "subscription" and tier in {"tier2", "tier3", "exit"})
        )
        and not item.get("tier3_auto_pool")
    )
    enabled = config_item_enabled(item)
    values = {
        "name": fmt(item.get("name") or item.get("label"), "") if item else "",
        "address": fmt(item.get("address"), "") if item else "",
        "port": fmt(item.get("port"), "") if item else "",
        "uri": fmt(item.get("uri") or item.get("url") or item.get("key"), "") if item else "",
        "vless_link": fmt(item.get("vless_link"), "") if item else "",
        "hysteria2_link": fmt(item.get("hysteria2_link"), "") if item else "",
        "naive_link": fmt(item.get("naive_link"), "") if item else "",
        "fallback_order": fallback_order_text(item.get("fallback_order")) if item else "",
        "enabled": enabled,
    }
    legacy_protocol_count = (
        sum(
            1
            for key in ("vless_link", "hysteria2_link", "naive_link")
            if str(item.get(key) or "").strip()
        )
        if item
        else 0
    )
    imported_single_node = bool(
        item
        and tier in {"tier2", "tier3", "exit"}
        and kind == "node"
        and editable
        and fmt(item.get("uri"), "").strip()
    )
    multiprotocol = bool(
        item
        and (
            (isinstance(item.get("protocols"), dict) and item.get("protocols"))
            or legacy_protocol_count > 1
            or imported_single_node
        )
    )
    visible_fields = (
        NODE_EDIT_TIER1_MULTIPROTOCOL_FIELDS
        if tier == "tier1" and kind == "node" and multiprotocol
        else tier1_single_protocol_edit_fields(item)
        if tier == "tier1" and kind == "node"
        else NODE_EDIT_IMPORTED_FIELDS
        if tier in {"tier2", "tier3", "exit"} and kind == "node"
        else NODE_EDIT_SUBSCRIPTION_FIELDS
        if editable and tier in {"tier2", "tier3", "exit"} and kind == "subscription"
        else frozenset()
    )
    editable_fields = (
        frozenset(visible_fields)
        if editable and tier == "tier1" and kind == "node"
        else NODE_EDIT_IMPORTED_EDITABLE_FIELDS.intersection(visible_fields)
        if editable and tier in {"tier2", "tier3", "exit"} and kind == "node"
        else NODE_EDIT_SUBSCRIPTION_FIELDS.intersection(visible_fields)
        if editable and tier in {"tier2", "tier3", "exit"} and kind == "subscription"
        else frozenset()
    )
    if editable:
        message = ""
    elif row and node_row_meta(row).get("entry_kind") == SUBSCRIPTION_NODE_ENTRY_KIND:
        message = translate("订阅管理")
    elif item and item.get("kind") == "subscription":
        message = ""
    elif not item and not row:
        message = ""
    else:
        message = translate("只读")
    original_values = {key: values.get(key, "") for key in editable_fields}
    if editable and tier == "tier1" and kind == "node" and multiprotocol:
        original_values["fallback_order"] = values.get("fallback_order", "")
        original_values["protocols"] = item.get("protocols", {}) if item else {}
    elif editable and imported_single_node:
        original_values["protocols"] = {fmt(item.get("protocol"), ""): {"uri": values.get("uri", "")}}
    return NodeEditViewState(
        tier=tier,
        kind=kind,
        editable=editable,
        multiprotocol=multiprotocol,
        values=values,
        visible_fields=frozenset(visible_fields),
        editable_fields=frozenset(editable_fields),
        original_values=original_values,
        can_delete=bool(item and item.get("kind") in {"node", "subscription"}),
        delete_label=translate("删除"),
        message=message,
        can_toggle_enabled=can_toggle_enabled,
        enabled=enabled,
    )


def node_edit_has_changes(original_values: dict[str, Any], current_values: dict[str, Any]) -> bool:
    for key, original in original_values.items():
        if isinstance(original, (dict, list)) or isinstance(current_values.get(key), (dict, list)):
            if current_values.get(key) != original:
                return True
            continue
        if key in current_values and str(current_values.get(key, "")).strip() != str(original).strip():
            return True
    return False


def node_edit_update_payload(
    item: dict[str, Any] | None,
    current_values: dict[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    tier = str(item.get("tier", "") or "")
    kind = str(item.get("kind", "") or "")
    if tier == "tier1":
        payload = {
            key: str(current_values.get(key, "")).strip()
            for key in ("name", "address", "port", "vless_link", "hysteria2_link", "naive_link")
            if key in current_values
        }
        if "fallback_order" in current_values:
            payload["fallback_order"] = parse_fallback_order_text(current_values.get("fallback_order"))
        if "protocols" in current_values:
            payload["protocols"] = protocols_update_payload(current_values.get("protocols"))
        if "enabled" in current_values:
            payload["enabled"] = bool(current_values.get("enabled"))
        return payload
    if tier in {"tier2", "tier3", "exit"} and kind == "subscription":
        payload: dict[str, Any] = {"url": str(current_values.get("uri", "")).strip()}
        if "enabled" in current_values:
            payload["enabled"] = bool(current_values.get("enabled"))
        return payload
    if tier in {"tier2", "tier3", "exit"}:
        protocols = current_values.get("protocols")
        uri = str(current_values.get("uri", "")).strip()
        if isinstance(protocols, dict) and len(protocols) == 1:
            only_config = next(iter(protocols.values()))
            if isinstance(only_config, dict):
                uri = str(only_config.get("uri") or only_config.get("link") or only_config.get("url") or uri).strip()
        payload = {
            "name": str(current_values.get("name", "")).strip(),
            "uri": uri,
        }
        if kind == "node" and "enabled" in current_values:
            payload["enabled"] = bool(current_values.get("enabled"))
        return payload
    return None


def runtime_node_row_from_status(
    tier: str,
    node: dict[str, Any],
    *,
    translate: Callable[[str], str] = _identity,
) -> list[Any]:
    integrity_ok = node.get("config_integrity_ok")
    integrity_errors = node.get("config_integrity_errors") or []
    address = fmt(node.get("address"))
    port = fmt(node.get("port"))
    protocol = fmt(node.get("protocol_label") or node.get("protocol"))
    name = fmt(node.get("name"))
    normalized_status = fmt(node.get("status"), "").strip().casefold()
    self_check_status = fmt(
        node.get("self_check_status", node.get("status")),
        "",
    ).strip().casefold()
    self_check_last_checked_at = fmt(
        node.get("self_check_last_checked_at", node.get("last_checked_at")),
        "",
    ).strip()
    health_origin = fmt(node.get("health_origin"), "").strip()
    health_observed_at = fmt(
        node.get("health_observed_at", node.get("runtime_observed_at")),
        "",
    ).strip()
    pending_recheck = node.get("pending_recheck") is True
    usable = bool(
        node.get(
            "usable",
            normalized_status in {"alive", "slow"},
        )
    )
    static_risk = ""
    runtime_issue_kind = ""
    if integrity_ok is False:
        static_risk = translate("配置异常")
        runtime_issue_kind = "configuration_invalid"
    if not static_risk:
        static_risk = translate("正常")
    reason_code = fmt(node.get("suspicious_reason"), "")
    issue_kind, issue_text = _runtime_failure_display_category(reason_code)
    if issue_kind:
        risk = translate(issue_text)
        runtime_issue_kind = issue_kind
    elif normalized_status in {
        "dead",
        "suspicious",
        "error",
        "failed",
    }:
        # Runtime reason strings can contain Python exception names, Winsock
        # error numbers, upstream command output, local paths, or other
        # implementation details.  They remain available to the runtime log,
        # but the desktop row is a user-facing model and therefore exposes
        # only a bounded product vocabulary.
        risk = translate("节点不可用")
        runtime_issue_kind = "node_unavailable"
    else:
        risk = ""
    risk = risk or static_risk
    if not risk:
        risk = translate("正常")
    return [
        tier,
        name,
        protocol,
        f"{address}:{port}",
        fmt(node.get("status")),
        fmt(node.get("latency_ms")),
        fmt(node.get("last_checked_at")),
        fmt(node.get("source_count"), "0"),
        translate("是") if node.get("has_uri") else translate("否"),
        risk,
        {
            "tier": tier,
            "name": name,
            "protocol": protocol,
            "address": address,
            "port": port,
            "identity": fmt(node.get("identity"), ""),
            "physical_node": fmt(node.get("physical_node"), ""),
            "protocol_key": fmt(node.get("protocol_key"), ""),
            "protocol_label": fmt(node.get("protocol_label"), ""),
            "fallback_order": list(node.get("fallback_order", []) or []),
            "tier3_origin": fmt(node.get("tier3_origin"), ""),
            "source_origin": fmt(node.get("source_origin"), ""),
            "source_type": fmt(node.get("source_type"), ""),
            "source": fmt(node.get("source"), ""),
            "sources": [fmt(item, "") for item in node.get("sources", []) or [] if fmt(item, "")],
            "config_integrity_ok": integrity_ok,
            "config_integrity_error_count": len(integrity_errors),
            "static_risk": static_risk,
            "runtime_issue_kind": runtime_issue_kind,
            # Runtime health is an effective in-memory overlay.  Preserve the
            # authoritative self-check result and its timestamp separately so
            # a card cannot make a newly quarantined leaf look as if a
            # periodic self-check had already failed.
            "self_check_status": self_check_status,
            "self_check_last_checked_at": self_check_last_checked_at,
            "health_origin": health_origin,
            "health_observed_at": health_observed_at,
            "pending_recheck": pending_recheck,
            "usable": usable,
            "observation_origin": _normalized_observation_origin(
                node.get("observation_origin")
            ),
            "observed_at": fmt(node.get("observed_at"), "").strip(),
            "diagnostic_only": node.get("diagnostic_only") is True,
            "temporary": node.get("temporary") is True,
            "tier3_auto_pool": bool(node.get("tier3_auto_pool")),
            "has_uri": bool(node.get("has_uri")),
            "connection": dict(node.get("connection") or {}),
            "aggregate": bool(node.get("aggregate")),
            "variants": tier1_variants_from_status_payload(node.get("protocols")) if tier == "tier1" else [],
        },
    ]


def runtime_node_rows_from_snapshot(
    payload: dict[str, Any],
    *,
    translate: Callable[[str], str] = _identity,
) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for tier, tier_payload in payload.get("tiers", {}).items():
        for node in tier_payload.get("nodes", []):
            rows.append(runtime_node_row_from_status(tier, node, translate=translate))
    return rows


def configured_node_rows_for_tier(
    tier: str,
    config_items: list[dict[str, Any]],
    *,
    translate: Callable[[str], str] = _identity,
) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for item in config_items:
        if item.get("tier") != tier or item.get("kind") != "node":
            continue
        enabled = config_item_enabled(item)
        address = fmt(item.get("address"), "")
        port = fmt(item.get("port"), "")
        endpoint = f"{address}:{port}" if address and port else address or fmt(item.get("detail"))
        rows.append(
            [
                tier,
                fmt(item.get("label")),
                fmt(item.get("protocol")),
                endpoint,
                "configured" if enabled else "disabled",
                "--",
                "--",
                translate("配置"),
                translate("是") if item.get("uri") else "--",
                "--",
                {
                    "tier": tier,
                    "name": fmt(item.get("label")),
                    "protocol": fmt(item.get("protocol")),
                    "address": address,
                    "port": port,
                    "variants": configured_tier1_protocol_variants(item) if tier == "tier1" else [],
                    "fallback_order": list(item.get("fallback_order", []) or []),
                    "config_item": item,
                    "enabled": enabled,
                    "draft": not enabled,
                },
            ]
        )
    return active_rows_before_drafts(rows)


def runtime_row_for_config_item(item: dict[str, Any], node_rows: list[list[Any]]) -> list[Any] | None:
    if item.get("kind") != "node" or not config_item_enabled(item):
        return None
    tier = str(item.get("tier", "") or "")
    address = fmt(item.get("address"), "")
    port = fmt(item.get("port"), "")
    label = fmt(item.get("label") or item.get("name"), "")
    matches: list[list[Any]] = []
    for row in node_rows:
        if len(row) == 0 or str(row[0]) != tier:
            continue
        meta = node_row_meta(row)
        if tier == "tier3" and (
            bool(meta.get("tier3_auto_pool"))
            or fmt(meta.get("tier3_origin") or meta.get("source_origin"), "")
            .strip()
            .casefold()
            == "tier3b"
        ):
            # Tier3B is an internal automatic-candidate pool.  Even when a
            # candidate happens to share an endpoint with a configured Tier3A
            # node, its runtime state must not be projected onto that user's
            # editable card.
            continue
        row_address = fmt(meta.get("address"), "")
        row_port = fmt(meta.get("port"), "")
        if address and row_address and address != row_address:
            continue
        if port and row_port and port != row_port:
            continue
        if not address and not port and label and fmt(row[1] if len(row) > 1 else "") != label:
            continue
        matches.append(row)
    return matches[0] if matches else None


def runtime_rows_with_config_items(
    rows: list[list[Any]],
    config_items: list[dict[str, Any]],
) -> list[list[Any]]:
    """Attach persisted config identity so draft/active refreshes keep selection."""
    attached: list[list[Any]] = []
    for row in rows:
        item = config_item_for_node_row(row, config_items)
        if item is None:
            attached.append(row)
            continue
        copied = list(row)
        while len(copied) <= 10:
            copied.append({} if len(copied) == 10 else "")
        meta = dict(node_row_meta(copied))
        meta["config_item"] = item
        meta["enabled"] = True
        meta["draft"] = False
        copied[10] = meta
        attached.append(copied)
    return attached


def config_rows_for_tier(
    tier: str,
    config_items: list[dict[str, Any]],
    node_rows: list[list[Any]],
    *,
    translate: Callable[[str], str] = _identity,
) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for item in config_items:
        if item.get("tier") != tier or item.get("kind") not in {"node", "subscription"}:
            continue
        enabled = config_item_enabled(item) if item.get("kind") == "node" else True
        runtime_row = runtime_row_for_config_item(item, node_rows)
        kind = str(item.get("kind", "") or "")
        if runtime_row is not None:
            status = fmt(runtime_row[4] if len(runtime_row) > 4 else "")
            latency = fmt(runtime_row[5] if len(runtime_row) > 5 else "")
            last_checked = fmt(runtime_row[6] if len(runtime_row) > 6 else "")
            risk = fmt(runtime_row[9] if len(runtime_row) > 9 else translate("正常"))
            runtime_meta = dict(node_row_meta(runtime_row))
            endpoint = fmt(runtime_row[3] if len(runtime_row) > 3 else item.get("detail"))
        else:
            status = "configured" if enabled else "disabled"
            latency = "--"
            last_checked = "--"
            risk = "--"
            runtime_meta = {}
            address = fmt(item.get("address"), "")
            port = fmt(item.get("port"), "")
            endpoint = f"{address}:{port}" if address and port else fmt(item.get("detail"))
        if kind == "subscription":
            endpoint = fmt(item.get("detail"))
            protocol = translate("订阅")
            status = "configured"
            latency = "--"
            last_checked = "--"
            risk = "--"
            subscription = translate("是") if item.get("uri") else "--"
        else:
            protocol = fmt(item.get("protocol"))
            subscription = translate("是") if item.get("uri") else "--"
        meta = {
            **runtime_meta,
            "tier": tier,
            "name": fmt(item.get("label")),
            "protocol": protocol,
            "address": fmt(item.get("address"), ""),
            "port": fmt(item.get("port"), ""),
            "config_item": item,
            "entry_kind": kind,
            "enabled": enabled,
            "draft": not enabled,
        }
        rows.append(
            [
                tier,
                fmt(item.get("label")),
                protocol,
                endpoint,
                status,
                latency,
                last_checked,
                translate("用户导入"),
                subscription,
                risk,
                meta,
            ]
        )
    return active_rows_before_drafts(rows)


def _row_matches_any_config_node(row: list[Any], config_items: list[dict[str, Any]]) -> bool:
    return config_item_for_node_row(row, config_items) is not None


def _config_rows_shadowed_by_subscription(
    tier: str,
    config_items: list[dict[str, Any]],
    runtime_rows: list[list[Any]],
    grouped_ids: set[int],
    *,
    translate: Callable[[str], str] = _identity,
) -> list[list[Any]]:
    """Keep a manual config card when its runtime identity is also supplied by a subscription."""
    shadowed_items: list[dict[str, Any]] = []
    for item in config_items:
        if item.get("tier") != tier or item.get("kind") != "node" or not config_item_enabled(item):
            continue
        runtime_row = runtime_row_for_config_item(item, runtime_rows)
        if runtime_row is not None and id(runtime_row) in grouped_ids:
            shadowed_items.append(item)
    return config_rows_for_tier(
        tier,
        shadowed_items,
        runtime_rows,
        translate=translate,
    )


def layer_scope_rows(
    tier: str,
    node_rows: list[list[Any]],
    config_items: list[dict[str, Any]],
    *,
    translate: Callable[[str], str] = _identity,
) -> list[list[Any]]:
    runtime_rows = rows_for_tier(node_rows, tier)
    configured_rows = configured_node_rows_for_tier(tier, config_items, translate=translate)
    draft_rows = [row for row in configured_rows if not node_row_enabled(row)]
    if tier == "tier1":
        if runtime_rows:
            active_rows = runtime_rows_with_config_items(
                tier1_display_rows(runtime_rows, translate=translate),
                config_items,
            )
            return active_rows_before_drafts([*active_rows, *draft_rows])
        return configured_rows
    if tier == "tier2":
        group_items = subscription_group_items_for_tier(tier, config_items)
        if group_items:
            grouped_ids = {
                id(row)
                for row in runtime_rows
                if any(row_matches_subscription_config(row, item) for item in group_items)
            }
            visible_runtime_rows = [
                row
                for row in runtime_rows
                if id(row) not in grouped_ids and not row_has_subscription_source(row)
            ]
            visible_runtime_rows = runtime_rows_with_config_items(visible_runtime_rows, config_items)
            shadowed_config_rows = _config_rows_shadowed_by_subscription(
                tier,
                config_items,
                runtime_rows,
                grouped_ids,
                translate=translate,
            )
            visible_config_rows = [*shadowed_config_rows, *draft_rows] if runtime_rows else configured_rows
            return active_rows_before_drafts([
                *visible_runtime_rows,
                *visible_config_rows,
                *subscription_group_rows_for_tier(tier, node_rows, config_items, translate=translate),
            ])
        visible_runtime_rows = [row for row in runtime_rows if not row_has_subscription_source(row)]
        visible_runtime_rows = runtime_rows_with_config_items(visible_runtime_rows, config_items)
        return active_rows_before_drafts([*visible_runtime_rows, *draft_rows]) if runtime_rows else configured_rows
    if tier == "tier3":
        node_items = [item for item in config_items if item.get("kind") == "node"]
        return active_rows_before_drafts([
            *config_rows_for_tier(tier, node_items, runtime_rows, translate=translate),
            *subscription_group_rows_for_tier(tier, node_rows, config_items, translate=translate),
        ])
    if tier == "exit":
        group_items = subscription_group_items_for_tier(tier, config_items)
        grouped_ids = {
            id(row)
            for row in runtime_rows
            if any(row_matches_subscription_config(row, item) for item in group_items)
        }
        visible_runtime_rows = [
            row
            for row in runtime_rows
            if id(row) not in grouped_ids and not row_has_subscription_source(row)
        ]
        visible_runtime_rows = runtime_rows_with_config_items(visible_runtime_rows, config_items)
        shadowed_config_rows = _config_rows_shadowed_by_subscription(
            tier,
            config_items,
            runtime_rows,
            grouped_ids,
            translate=translate,
        )
        visible_config_rows = [*shadowed_config_rows, *draft_rows] if runtime_rows else configured_rows
        return active_rows_before_drafts([
            *visible_runtime_rows,
            *visible_config_rows,
            *subscription_group_rows_for_tier(tier, node_rows, config_items, translate=translate),
        ])
    active_runtime_rows = runtime_rows_with_config_items(runtime_rows, config_items)
    return active_rows_before_drafts([*active_runtime_rows, *draft_rows]) if runtime_rows else configured_rows


GATEWAY_SYSTEM_PROXY_NAMES = frozenset({"", "EXIT-AUTO", "EXIT-SELECT", "FINAL-EXIT", "PROXY-FALLBACK", "DIRECT"})


def gateway_proxy_name_for_row(row: list[Any] | None) -> str:
    if not isinstance(row, list):
        return ""
    meta = node_row_meta(row)
    if meta.get("entry_kind") in {
        SUBSCRIPTION_GROUP_ENTRY_KIND,
        SUBSCRIPTION_GROUP_BACK_ENTRY_KIND,
    }:
        return node_row_key(row)
    proxy_name = fmt(meta.get("name"), "").strip()
    if proxy_name:
        return proxy_name
    return fmt(row[1] if len(row) > 1 else "", "").strip()


def _gateway_row_with_meta(
    row: list[Any],
    *,
    candidate_count: int,
    selected_proxy_name: str,
) -> list[Any]:
    copied = list(row)
    while len(copied) <= 10:
        copied.append({} if len(copied) == 10 else "")
    meta = dict(node_row_meta(copied))
    meta["gateway_effective"] = True
    meta["gateway_candidate_count"] = candidate_count
    meta["gateway_selected_proxy"] = selected_proxy_name
    copied[10] = meta
    return copied


def gateway_effective_exit_row(
    rows: list[list[Any]],
    *,
    selected_proxy_name: str = "",
) -> list[Any] | None:
    candidate_rows = [
        row
        for row in rows
        if node_row_meta(row).get("entry_kind")
        not in {SUBSCRIPTION_GROUP_ENTRY_KIND, SUBSCRIPTION_GROUP_BACK_ENTRY_KIND}
    ]
    if not candidate_rows:
        return None
    selected = str(selected_proxy_name or "").strip()
    if not selected or selected in GATEWAY_SYSTEM_PROXY_NAMES:
        return None
    for row in candidate_rows:
        if not node_row_enabled(row):
            continue
        if gateway_proxy_name_for_row(row) == selected:
            return _gateway_row_with_meta(
                row,
                candidate_count=len(candidate_rows),
                selected_proxy_name=selected,
            )
    return None


def gateway_layer_scope_rows(
    tier: str,
    node_rows: list[list[Any]],
    config_items: list[dict[str, Any]],
    *,
    selected_proxy_name: str = "",
    translate: Callable[[str], str] = _identity,
) -> list[list[Any]]:
    rows = layer_scope_rows(tier, node_rows, config_items, translate=translate)
    if tier != "exit":
        return rows
    active = gateway_effective_exit_row(rows, selected_proxy_name=selected_proxy_name)
    if active is None:
        return rows
    active_name = gateway_proxy_name_for_row(active)
    ordered = [active]
    seen = {active_name} if active_name else set()
    for row in rows:
        proxy_name = gateway_proxy_name_for_row(row)
        if proxy_name and proxy_name in seen:
            continue
        ordered.append(row)
        if proxy_name:
            seen.add(proxy_name)
    return ordered


def node_status_snapshot(
    node: Node,
    *,
    identity: str | None = None,
    tier3_auto_pool: bool | None = None,
    health_owner: Any | None = None,
) -> dict[str, Any]:
    identity = identity if identity is not None else node_identity(node)
    tier3_auto_pool = is_tier3_auto_pool_node(node) if tier3_auto_pool is None else tier3_auto_pool
    extra = node.extra if isinstance(node.extra, dict) else {}
    raw_status = getattr(node.status, "value", node.status)
    effective_status = raw_status
    usable = str(raw_status or "").strip().casefold() in {"alive", "slow"}
    health_entry: dict[str, Any] | None = None
    if health_owner is not None:
        status_getter = getattr(health_owner, "effective_node_status", None)
        if callable(status_getter):
            try:
                candidate = status_getter(str(node.tier or ""), node)
            except (AttributeError, KeyError, TypeError, ValueError):
                candidate = None
            if candidate is not None:
                effective_status = getattr(candidate, "value", candidate)
        usable_getter = getattr(health_owner, "effective_node_usable", None)
        if callable(usable_getter):
            try:
                usable = bool(usable_getter(str(node.tier or ""), node))
            except (AttributeError, KeyError, TypeError, ValueError):
                usable = str(effective_status or "").strip().casefold() in {
                    "alive",
                    "slow",
                }
        else:
            usable = str(effective_status or "").strip().casefold() in {
                "alive",
                "slow",
            }
        health_getter = getattr(health_owner, "runtime_health_snapshot", None)
        if callable(health_getter):
            try:
                health_payload = health_getter()
            except (AttributeError, KeyError, TypeError, ValueError):
                health_payload = {}
            entries = (
                health_payload.get("entries", [])
                if isinstance(health_payload, dict)
                else []
            )
            matches = [
                entry
                for entry in entries
                if isinstance(entry, dict)
                and str(entry.get("tier") or "") == str(node.tier or "")
                and str(entry.get("identity") or "") == identity
                # One server can sit in both Tier3 pools.
                and (
                    str(node.tier or "") != "tier3"
                    or str(entry.get("pool_kind") or "")
                    in {"", "tier3b" if tier3_auto_pool else "tier3a"}
                )
            ]
            if len(matches) > 1:
                # An incident names its Core leaf (a stable Tier3 name or a
                # "-2" suffix), so node.name only breaks a same-identity tie.
                matches = [
                    entry
                    for entry in matches
                    if str(entry.get("proxy_name") or "") == str(node.name or "")
                ]
            if len(matches) == 1:
                health_entry = matches[0]
    health_origin = (
        str(health_entry.get("source") or "runtime")
        if health_entry is not None
        else "self_check"
        if node.last_checked_at
        else "unobserved"
    )
    health_observed_at = (
        str(health_entry.get("observed_at") or "")
        if health_entry is not None
        else str(node.last_checked_at or "")
    )
    effective_latency_ms = (
        None
        if health_entry is not None and not usable
        else node.latency_ms
    )
    return {
        "id": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12],
        "identity": identity,
        "name": node.name,
        "protocol": node.protocol,
        "address": node.address,
        "port": node.port,
        "tier": node.tier,
        "physical_node": extra.get("physical_node"),
        "protocol_key": protocol_variant_key(node),
        "protocol_label": extra.get("protocol_label"),
        "fallback_order": list(extra.get("fallback_order", []) or []),
        "tier3_origin": extra.get("tier3_origin"),
        "source_origin": extra.get("source_origin"),
        "source_type": extra.get("source_type"),
        "source": node.source,
        "sources": list(node.sources),
        "tier3_auto_pool": tier3_auto_pool,
        "status": str(effective_status or ""),
        "self_check_status": str(raw_status or ""),
        "self_check_last_checked_at": node.last_checked_at,
        "health_origin": health_origin,
        "health_observed_at": health_observed_at,
        "pending_recheck": (
            bool(health_entry.get("pending_recheck", True))
            if health_entry is not None
            else False
        ),
        "usable": usable,
        "latency_ms": effective_latency_ms,
        "last_checked_at": node.last_checked_at,
        "suspicious_reason": node.suspicious_reason,
        "source_count": len(node.sources),
        "has_uri": bool(node.uri),
        "config_integrity_ok": extra.get("config_integrity_ok"),
        "config_integrity_errors": extra.get("config_integrity_errors", []),
        "connection": node_connection_snapshot(node),
    }
