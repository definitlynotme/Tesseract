from __future__ import annotations

from typing import Any

from config_utils import coerce_positive_int
from tier3_sources import DEFAULT_SOURCE_MAX_BYTES


def collection_timeout_seconds(settings: Any, default: int = 15) -> int:
    config = settings if isinstance(settings, dict) else {}
    return coerce_positive_int(config.get("request_timeout"), default)


def collection_retries(settings: Any, default: int = 2) -> int:
    config = settings if isinstance(settings, dict) else {}
    return coerce_positive_int(config.get("retries"), default, minimum=0)


def collection_max_bytes(settings: Any, default: int = DEFAULT_SOURCE_MAX_BYTES) -> int:
    config = settings if isinstance(settings, dict) else {}
    return coerce_positive_int(
        config.get("max_source_bytes", config.get("source_max_bytes")),
        default,
    )


def collection_user_agents(settings: Any, defaults: list[str]) -> list[str]:
    config = settings if isinstance(settings, dict) else {}
    values = config.get("user_agents")
    agents = (
        [str(value).strip() for value in values or [] if str(value).strip()]
        if isinstance(values, (list, tuple))
        else []
    )
    return agents or list(defaults)
