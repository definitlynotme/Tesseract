from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import yaml

from alerts import AlertManager, setup_logging
from config_utils import canonicalize_routing_rule_layout, config_entry_enabled, coerce_bool
from killswitch import KillSwitchManager, build_unkill_script
from log_event_reports import log_raw_diagnostic
from manager import ProxyTierManager
from manager_alert_policy import kill_switch_unavailable_event
from runtime_settings import alerts_block, killswitch_block, tier_block
from subscription import SubscriptionServer
from subscription_server_config import subscription_server_runtime_config


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_yaml_object(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError(f"YAML root must be an object: {path}")
    return payload


def load_config(path: str = "config.yaml", local_path: str | None = "config.local.yaml") -> dict:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Missing config file: {config_path}")
    config = load_yaml_object(config_path)
    canonicalize_routing_rule_layout(config)
    if local_path:
        override_path = Path(local_path)
        if not override_path.is_absolute():
            override_path = config_path.parent / override_path
        if override_path.exists():
            override = load_yaml_object(override_path)
            canonicalize_routing_rule_layout(override)
            config = deep_merge(config, override)
    return config


def tier1_nodes_for_killswitch(config: dict[str, Any]) -> list[Any]:
    nodes = tier_block(config, "tier1").get("nodes", [])
    if not isinstance(nodes, list):
        return []
    return [node for node in nodes if config_entry_enabled(node)]


def build_subscription_server(manager: ProxyTierManager, config: dict[str, Any]) -> SubscriptionServer:
    server_config = subscription_server_runtime_config(config)
    return SubscriptionServer(
        manager=manager,
        host=server_config.host,
        port=server_config.port,
        limits={"tier3_limit": server_config.tier3_limit},
        token=server_config.token,
        cors_allowed_origins=server_config.cors_allowed_origins,
    )


def main() -> int:
    config = load_config()
    alerts_config = alerts_block(config)
    logger = setup_logging(
        log_file=alerts_config.get("log_file", "data/monitor.log"),
        level=alerts_config.get("level", "INFO"),
    )
    alert_manager = AlertManager(alerts_config, logger)
    build_unkill_script("unkill.bat")
    kill_switch_config = killswitch_block(config)
    kill_switch = KillSwitchManager(
        config=kill_switch_config,
        tier1_nodes=tier1_nodes_for_killswitch(config),
    )
    kill_switch.cleanup_stale_rules()
    kill_switch_state = kill_switch.get_state()
    if coerce_bool(kill_switch_config.get("enabled", True), True) and kill_switch_state["state"] == "disabled":
        if kill_switch_state["supported"]:
            warning_message = "Kill switch requires Administrator privileges. Run as admin for full protection."
        else:
            warning_message = "Kill switch is Windows-only in this version and is disabled on this platform."
        log_raw_diagnostic(logger, "warning", warning_message)
        alert_manager.send_event(kill_switch_unavailable_event(warning_message, source="main"))

    manager = ProxyTierManager(config=config, logger=logger, alerts=alert_manager, kill_switch=kill_switch)

    server_config = subscription_server_runtime_config(config)
    subscription_server = build_subscription_server(manager, config)
    subscription_server.start()
    logger.info(
        "Subscription server listening on http://%s:%s",
        server_config.host,
        server_config.port,
    )
    try:
        while True:
            manager.run_cycle()
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutdown requested by user")
    finally:
        subscription_server.stop()
        kill_switch.cleanup()
        manager.close()
        if not alert_manager.close(timeout=3.0):
            log_raw_diagnostic(
                logger,
                "warning",
                "Alert dispatcher did not fully drain before the 3-second shutdown deadline",
            )
        logger.info("Subscription server stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
