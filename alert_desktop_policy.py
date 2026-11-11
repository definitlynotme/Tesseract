"""Pure desktop-notice policy shared by the bridge alert owner (T-P2-22).

Copied from the Qt implementation in ``desktop_app.py`` (about lines
24640-24705: ``_append_alert_log_entries``, ``_desktop_alerts_enabled`` and
``_desktop_alert_notice_allowed``). The Qt code is intentionally unchanged; any
behaviour change must be made in both places until Qt is retired.

No Qt, runtime, locks, threads or I/O.
"""

from __future__ import annotations

from typing import Any, Sequence

from alert_config import alert_channel_accepts_event
from config_utils import coerce_bool


# Node-down events remain in alert history and external delivery records, but
# the desktop node list already represents this state as unavailable. Do not
# duplicate it as a desktop notice every time a self-check confirms the
# failure (desktop_app.py ``_desktop_alert_notice_allowed``).
DESKTOP_SUPPRESSED_EVENT_KEYS = frozenset({"tier1.node_down"})

# PROTOCOL alerts.desktop severity order (lowest first).
SEVERITY_RANK = {"info": 0, "warn": 1, "error": 2, "critical": 3}


def desktop_config_from_alerts(alerts_config: Any) -> dict[str, Any]:
    """The desktop channel section of an effective alerts config."""
    if not isinstance(alerts_config, dict):
        return {}
    section = alerts_config.get("desktop")
    return dict(section) if isinstance(section, dict) else {}


def desktop_notice_enabled(desktop_config: Any) -> bool:
    """Qt ``_desktop_alerts_enabled``: the desktop channel is switched on."""
    return isinstance(desktop_config, dict) and coerce_bool(
        desktop_config.get("enabled"), False
    )


def desktop_alert_notice_allowed(item: dict[str, Any], desktop_config: Any) -> bool:
    """Qt ``_desktop_alert_notice_allowed`` for one raw RT-6 alert record.

    ``item`` carries the raw ``type``/``event_key``/``event_group`` fields of
    an AlertRecord. Channel scope uses the alert type (severity) filter and the
    event-group filter exactly like external channels do.
    """
    if str(item.get("event_key") or "").strip() in DESKTOP_SUPPRESSED_EVENT_KEYS:
        return False
    return alert_channel_accepts_event(
        "desktop",
        desktop_config if isinstance(desktop_config, dict) else {},
        alert_type=item.get("type"),
        event_group=item.get("event_group"),
    )


def highest_severity_index(severities: Sequence[str]) -> int:
    """Index of the highest severity; a later member wins ties (Qt order)."""
    if not severities:
        raise ValueError("no severities")
    best = 0
    for index, severity in enumerate(severities):
        if SEVERITY_RANK.get(severity, 0) >= SEVERITY_RANK.get(severities[best], 0):
            best = index
    return best
