from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config_utils import ensure_mapping_section
from runtime_settings import (
    MAX_PRIMARY_GATEWAY_MAX_LATENCY_MS,
    MIN_PRIMARY_GATEWAY_MAX_LATENCY_MS,
    primary_gateway_max_latency_ms_from_testing,
    primary_gateway_slow_threshold_ms,
)


@dataclass(frozen=True)
class LatencyPolicyBoundaries:
    normal_max_ms: float
    slow_max_ms: int


@dataclass(frozen=True)
class LatencyPolicyFormState:
    max_latency_ms: int
    boundaries: LatencyPolicyBoundaries


def _testing_section(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict):
        return {}
    testing = config.get("testing")
    return dict(testing) if isinstance(testing, dict) else {}


def validate_latency_policy_max_latency_ms(value: Any) -> int:
    """Parse and strictly validate a settings-form latency value."""

    if isinstance(value, bool):
        parsed: Any = value
    elif isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
    else:
        parsed = None
    if (
        isinstance(parsed, bool)
        or not isinstance(parsed, int)
        or not MIN_PRIMARY_GATEWAY_MAX_LATENCY_MS
        <= parsed
        <= MAX_PRIMARY_GATEWAY_MAX_LATENCY_MS
    ):
        raise ValueError(
            "maximum latency must be an integer between "
            f"{MIN_PRIMARY_GATEWAY_MAX_LATENCY_MS} and "
            f"{MAX_PRIMARY_GATEWAY_MAX_LATENCY_MS} ms"
        )
    return parsed


def latency_policy_boundaries(max_latency_ms: Any) -> LatencyPolicyBoundaries:
    maximum = validate_latency_policy_max_latency_ms(max_latency_ms)
    slow_threshold = primary_gateway_slow_threshold_ms(maximum)
    return LatencyPolicyBoundaries(
        normal_max_ms=slow_threshold,
        slow_max_ms=maximum,
    )


def latency_policy_form_state(
    runtime_config: dict[str, Any],
    local_config: dict[str, Any],
) -> LatencyPolicyFormState:
    testing = _testing_section(runtime_config)
    testing.update(_testing_section(local_config))
    maximum = primary_gateway_max_latency_ms_from_testing(testing)
    return LatencyPolicyFormState(
        max_latency_ms=maximum,
        boundaries=latency_policy_boundaries(maximum),
    )


def apply_latency_policy_config_payload(
    payload: dict[str, Any],
    max_latency_ms: Any,
) -> dict[str, Any]:
    maximum = validate_latency_policy_max_latency_ms(max_latency_ms)
    testing = ensure_mapping_section(payload, "testing")
    testing["primary_gateway_max_latency_ms"] = maximum
    return payload


__all__ = [
    "LatencyPolicyBoundaries",
    "LatencyPolicyFormState",
    "apply_latency_policy_config_payload",
    "latency_policy_boundaries",
    "latency_policy_form_state",
    "validate_latency_policy_max_latency_ms",
]
