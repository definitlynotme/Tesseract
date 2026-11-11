from __future__ import annotations

from pathlib import Path
from typing import Any


def default_core_status() -> dict[str, Any]:
    return {"running": False, "pid": None, "recent_logs": []}


def subscription_auth_required(subscription_config: Any) -> bool:
    if not isinstance(subscription_config, dict):
        return False
    return bool(subscription_config.get("token"))


def runtime_state_status(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": path.exists(),
    }


def desktop_runtime_status_payload(
    *,
    running: bool,
    last_error: str | None,
    clash_config_url: str,
    subscription_config: Any,
    core_status: dict[str, Any],
    system_proxy_state: dict[str, Any],
    network_mode: str,
    is_admin: bool,
    runtime_state_path: Path,
    geo_update_status: dict[str, Any],
    autostart_status: dict[str, Any],
    runtime_snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "running": running,
        "last_error": last_error,
        "clash_config_url": clash_config_url,
        "subscription_auth_required": subscription_auth_required(subscription_config),
        "core": core_status,
        "system_proxy": system_proxy_state,
        "network_mode": network_mode,
        "is_admin": is_admin,
        "runtime_state": runtime_state_status(runtime_state_path),
        "geo_update": geo_update_status,
        "autostart": autostart_status,
        "runtime": runtime_snapshot,
    }
