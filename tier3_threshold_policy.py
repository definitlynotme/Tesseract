from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tier3_defaults import (
    DEFAULT_TIER3_MAX_POOL_SIZE,
    DEFAULT_TIER3_MIN_ALIVE_NODES,
    DEFAULT_TIER3_TARGET_ALIVE,
    DEFAULT_TIER3_TARGET_FLOOR_MULTIPLIER,
    DEFAULT_TIER3_TARGET_RESERVE_NODES,
)


@dataclass(frozen=True)
class Tier3Thresholds:
    """Normalized internal thresholds for Tier3 admission and expansion."""

    max_pool_size: int
    min_alive_nodes: int
    configured_target_alive: int
    automatic_target_alive: int
    effective_target_alive: int


def _coerce_int(value: Any, default: int) -> int:
    raw_value = default if value is None else value
    if isinstance(raw_value, str):
        raw_value = raw_value.strip()
        if not raw_value or raw_value.lower() in {"inherit", "none", "null"}:
            raw_value = default
    try:
        return int(raw_value)
    except (TypeError, ValueError, OverflowError):
        return int(default)


def _configured_target(value: Any) -> int:
    target = _coerce_int(value, DEFAULT_TIER3_TARGET_ALIVE)
    if target < 1:
        return DEFAULT_TIER3_TARGET_ALIVE
    return target


def resolve_tier3_thresholds(
    tier3_config: Mapping[str, Any] | None,
) -> Tier3Thresholds:
    """Resolve C/M/B/T thresholds without modifying the supplied configuration.

    The automatic target is ``max(25, M + 22, 2 * M)`` and the effective
    target is capped to C after honoring any higher advanced target B.
    """

    config = tier3_config if isinstance(tier3_config, Mapping) else {}
    max_pool_size = max(
        1,
        _coerce_int(config.get("max_pool_size"), DEFAULT_TIER3_MAX_POOL_SIZE),
    )
    min_alive_nodes = min(
        max_pool_size,
        max(
            1,
            _coerce_int(
                config.get("min_alive_nodes"),
                DEFAULT_TIER3_MIN_ALIVE_NODES,
            ),
        ),
    )
    controller = config.get("pi_controller")
    controller_config = controller if isinstance(controller, Mapping) else {}
    configured_target_alive = _configured_target(
        controller_config.get("target_alive")
    )
    automatic_target_alive = max(
        DEFAULT_TIER3_TARGET_ALIVE,
        min_alive_nodes + DEFAULT_TIER3_TARGET_RESERVE_NODES,
        DEFAULT_TIER3_TARGET_FLOOR_MULTIPLIER * min_alive_nodes,
    )
    effective_target_alive = min(
        max_pool_size,
        max(configured_target_alive, automatic_target_alive),
    )
    return Tier3Thresholds(
        max_pool_size=max_pool_size,
        min_alive_nodes=min_alive_nodes,
        configured_target_alive=configured_target_alive,
        automatic_target_alive=automatic_target_alive,
        effective_target_alive=effective_target_alive,
    )
