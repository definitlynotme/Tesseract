from __future__ import annotations

from typing import Any

from alerts import AlertEvent
from models import AlertType, Node


ALERT_GROUP_BACKUP = "backup"
ALERT_GROUP_EMERGENCY_FALLBACK = "emergency_fallback"
ALERT_GROUP_PROTECTION = "protection"
ALERT_GROUP_ROUTE_HEALTH = "route_health"
ALERT_GROUP_VANGUARD_NODES = "vanguard_nodes"


def tier3_top_nodes_listing(nodes: list[Node]) -> str:
    return ", ".join(f"{node.name}@{node.address}:{node.port}" for node in nodes) or "No healthy fallback nodes"


def _compact_notice(text: str) -> str:
    return " ".join(str(text or "").split())


def _event(
    event_key: str,
    alert_type: AlertType,
    title: str,
    message: str,
    *,
    event_group: str = "",
    source: str = "manager.evaluate_state",
    scope: str = "",
    metadata: dict[str, Any] | None = None,
) -> AlertEvent:
    return AlertEvent(
        event_key=event_key,
        alert_type=alert_type,
        title=title,
        message=message,
        event_group=event_group,
        source=source,
        scope=scope,
        metadata=dict(metadata or {}),
    )


def mode_transition_events(
    *,
    new_mode: str,
    previous_mode: str | None,
    metrics: dict[str, Any],
    tier3_listing: str,
    tier3_warning_banner: str,
) -> list[AlertEvent]:
    if new_mode == "tier1_healthy":
        if previous_mode and previous_mode != "tier1_healthy":
            return [
                _event(
                    "mode.recovery.vanguard",
                    AlertType.RECOVERY,
                    "Nodes restored",
                    "Nodes are healthy again.",
                    event_group=ALERT_GROUP_ROUTE_HEALTH,
                    scope=str(previous_mode),
                )
            ]
        return []

    if new_mode == "tier1_degraded":
        return [
            _event(
                "mode.tier1_degraded",
                AlertType.WARNING,
                "Nodes degraded",
                f"Available: {metrics['tier1_alive']}/{metrics['tier1_total']}.",
                event_group=ALERT_GROUP_ROUTE_HEALTH,
                metadata={"tier1_alive": metrics["tier1_alive"], "tier1_total": metrics["tier1_total"]},
            )
        ]

    if new_mode == "tier2_active":
        if previous_mode == "tier3_active" or previous_mode == "offline":
            return [
                _event(
                    "mode.recovery.sentinel",
                    AlertType.RECOVERY,
                    "Nodes restored",
                    f"Available: {metrics['tier2_alive']}.",
                    event_group=ALERT_GROUP_ROUTE_HEALTH,
                    scope=str(previous_mode),
                    metadata={"tier2_alive": metrics["tier2_alive"]},
                )
            ]
        return [
            _event(
                "mode.tier2_active",
                AlertType.CRITICAL,
                "Nodes unavailable",
                f"Fallback available: {metrics['tier2_alive']}.",
                event_group=ALERT_GROUP_ROUTE_HEALTH,
                metadata={"tier2_alive": metrics["tier2_alive"]},
            ),
            _event(
                "mode.tier2_active.backup_reminder",
                AlertType.BACKUP_REMINDER,
                "Degradation-triggered backup",
                "Primary infrastructure is failing. Push critical backups now.",
                event_group=ALERT_GROUP_BACKUP,
            ),
        ]

    if new_mode == "tier3_active":
        warning = _compact_notice(tier3_warning_banner)
        return [
            _event(
                "mode.tier3_active",
                AlertType.EMERGENCY,
                "Fallback active",
                f"Available fallback nodes: {tier3_listing}. {warning}",
                event_group=ALERT_GROUP_EMERGENCY_FALLBACK,
                metadata={"tier3_listing": tier3_listing},
            )
        ]

    if new_mode == "offline":
        warning = _compact_notice(tier3_warning_banner)
        return [
            _event(
                "mode.offline",
                AlertType.EMERGENCY,
                "No nodes available",
                f"No available nodes. {warning}",
                event_group=ALERT_GROUP_EMERGENCY_FALLBACK,
            )
        ]

    return []


def _join_labels(values: list[str]) -> str:
    cleaned = [str(value or "").strip() for value in values if str(value or "").strip()]
    return ", ".join(cleaned)


def tier1_node_down_event(nodes: list[str]) -> AlertEvent:
    labels = _join_labels(nodes)
    title = "Node unavailable" if len(nodes) == 1 else "Nodes unavailable"
    message = f"Unavailable: {labels}."
    return _event(
        "tier1.node_down",
        AlertType.WARNING,
        title,
        message,
        event_group=ALERT_GROUP_VANGUARD_NODES,
        source="manager.check_tier1",
        metadata={"nodes": list(nodes)},
    )


def tier1_protocol_fallback_event(fallbacks: list[dict[str, Any]]) -> AlertEvent:
    parts: list[str] = []
    for item in fallbacks:
        node = str(item.get("node") or "Node").strip()
        failed = [str(value).strip() for value in item.get("failed_protocols", []) if str(value).strip()]
        fallback = str(item.get("fallback_protocol") or "").strip()
        failed_text = _join_labels(failed)
        parts.append(f"{node}: {failed_text} failed; falling back to {fallback}.")
    return _event(
        "tier1.protocol_fallback",
        AlertType.WARNING,
        "Protocol changed",
        " ".join(parts),
        event_group=ALERT_GROUP_VANGUARD_NODES,
        source="manager.check_tier1",
        metadata={"fallbacks": list(fallbacks)},
    )


def tier3_intervention_event() -> AlertEvent:
    return _event(
        "tier3.intervention_required",
        AlertType.EMERGENCY,
        "No fallback nodes",
        "No usable nodes in configured sources.",
        event_group=ALERT_GROUP_EMERGENCY_FALLBACK,
    )


_SOURCE_SUPPLY_NO_EGRESS_MESSAGES = {
    "dns_unresolvable": "Node sources cannot be resolved on this network. Check DNS settings, configure an egress proxy, or import a node.",
    "sources_unreachable": "No node source is reachable on this network. Configure an egress proxy or import a node manually.",
    "no_route": "No route is available for node sources. Configure an egress proxy or import a node.",
    "captive_portal_suspected": "This network requires sign-in first; collection resumes automatically afterwards.",
    "encrypted_dns_unreachable": "Encrypted-only DNS is pinned but unreachable here. Allow the system resolver or configure an egress proxy.",
}


def tier3_source_supply_no_egress_event(detail: str, *, rung: str = "") -> AlertEvent:
    """Bounded terminal state: source supply has no usable egress on this network."""

    normalized = str(detail or "").strip() or "sources_unreachable"
    message = _SOURCE_SUPPLY_NO_EGRESS_MESSAGES.get(
        normalized, _SOURCE_SUPPLY_NO_EGRESS_MESSAGES["sources_unreachable"],
    )
    return _event(
        "tier3.source_supply_no_egress",
        AlertType.EMERGENCY,
        "Node sources unreachable",
        message,
        event_group=ALERT_GROUP_EMERGENCY_FALLBACK,
        metadata={"detail": normalized, "rung": str(rung or "")},
    )


def kill_switch_unavailable_event(message: str, *, source: str) -> AlertEvent:
    return _event(
        "kill_switch.disabled",
        AlertType.KILL_SWITCH_DISABLED,
        "Kill switch unavailable",
        message,
        event_group=ALERT_GROUP_PROTECTION,
        source=source,
    )


def kill_switch_activated_event(message: str) -> AlertEvent:
    return _event(
        "kill_switch.activated",
        AlertType.KILL_SWITCH_ON,
        "Kill switch activated",
        message,
        event_group=ALERT_GROUP_PROTECTION,
    )


def kill_switch_deactivated_event() -> AlertEvent:
    return _event(
        "kill_switch.deactivated",
        AlertType.KILL_SWITCH_OFF,
        "Kill switch deactivated",
        "A proxy tier recovered. Outbound connectivity has been restored.",
        event_group=ALERT_GROUP_PROTECTION,
    )


def tier2_tier3_flap_event(active_layer: str) -> AlertEvent:
    del active_layer
    return _event(
        "mode.tier2_tier3_flap",
        AlertType.WARNING,
        "Fallback changing",
        "Fallback path is changing.",
        event_group=ALERT_GROUP_ROUTE_HEALTH,
    )


def kill_switch_manual_override_event() -> AlertEvent:
    return _event(
        "kill_switch.manual_override",
        AlertType.KILL_SWITCH_OVERRIDE,
        "Kill switch manual override enabled",
        "Manual Force On is active. Automatic recovery will not clear the kill switch until Force Off is used.",
        event_group=ALERT_GROUP_PROTECTION,
        source="manager.manual_kill_switch",
    )


def kill_switch_manual_clear_event() -> AlertEvent:
    return _event(
        "kill_switch.manual_clear",
        AlertType.KILL_SWITCH_OFF,
        "Kill switch manual override cleared",
        "Manual Force Off removed kill switch rules. Automatic protection may re-engage immediately if all tiers remain down.",
        event_group=ALERT_GROUP_PROTECTION,
        source="manager.manual_kill_switch",
    )
