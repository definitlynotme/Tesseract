from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config_utils import coerce_positive_int
from runtime_settings import subscription_server_block


@dataclass
class SubscriptionServerRuntimeConfig:
    host: str
    port: int
    token: str
    cors_allowed_origins: list[str]
    tier3_limit: int


def subscription_server_runtime_config(config: dict[str, Any]) -> SubscriptionServerRuntimeConfig:
    server_config = subscription_server_block(config)
    cors_raw = server_config.get("cors_allowed_origins", [])
    cors_allowed_origins = (
        [str(origin).strip() for origin in cors_raw or [] if str(origin).strip()]
        if isinstance(cors_raw, list)
        else []
    )
    return SubscriptionServerRuntimeConfig(
        host=str(server_config.get("host", "127.0.0.1")),
        port=coerce_positive_int(server_config.get("port", 25500), 25500),
        token=str(server_config.get("token", "") or ""),
        cors_allowed_origins=cors_allowed_origins,
        tier3_limit=coerce_positive_int(server_config.get("tier3_limit", 20), 20),
    )


def subscription_tier3_limit(
    limits: Any,
    subscription_config: Any | None = None,
    default: int = 20,
) -> int:
    limit_config = limits if isinstance(limits, dict) else {}
    server_config = subscription_config if isinstance(subscription_config, dict) else {}
    fallback = coerce_positive_int(server_config.get("tier3_limit", default), default)
    return coerce_positive_int(
        limit_config.get("tier3_limit", fallback),
        fallback,
    )


def subscription_probe_interval(
    subscription_config: Any,
    testing_config: Any,
    default: int = 300,
) -> int:
    server_config = subscription_config if isinstance(subscription_config, dict) else {}
    testing = testing_config if isinstance(testing_config, dict) else {}
    fallback = coerce_positive_int(testing.get("clash_probe_interval", default), default)
    interval = coerce_positive_int(
        server_config.get("probe_interval", fallback),
        fallback,
    )
    return max(30, interval)


def subscription_server_matches(server: Any, desired: SubscriptionServerRuntimeConfig) -> bool:
    limits = getattr(server, "limits", {})
    limits = limits if isinstance(limits, dict) else {}
    return (
        server.host == desired.host
        and server.port == desired.port
        and server.token == desired.token
        and server.cors_allowed_origins == desired.cors_allowed_origins
        and coerce_positive_int(limits.get("tier3_limit", desired.tier3_limit), desired.tier3_limit) == desired.tier3_limit
    )
