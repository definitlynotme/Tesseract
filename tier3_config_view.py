from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config_utils import coerce_bool, ensure_mapping_section
from startup_planning import coerce_int, tier3_config_from_runtime
from tier3_defaults import DEFAULT_TIER3_MIN_ALIVE_NODES

# The Bastion policy card edits only the on/off switch and the minimum.  The
# batch validator ("fast") capacity profile ships as the base default in
# config.example.yaml and has no settings switch; the fallback-source switch
# (tiers.tier3.bootstrap_enabled) was retired too, so fallback sources are
# always used: saving the card drops the key and the runtime reads a missing
# key as on.


@dataclass(frozen=True)
class Tier3ConfigFormState:
    enabled: bool
    min_alive_nodes: int
    max_pool_size: int


def effective_tier3_config(runtime_config: dict[str, Any], local_config: dict[str, Any]) -> dict[str, Any]:
    runtime_tiers = runtime_config.get("tiers", {}) if isinstance(runtime_config.get("tiers"), dict) else {}
    runtime_tier3 = runtime_tiers.get("tier3", {}) if isinstance(runtime_tiers.get("tier3"), dict) else {}
    local_tiers = local_config.get("tiers", {}) if isinstance(local_config.get("tiers"), dict) else {}
    local_tier3 = local_tiers.get("tier3", {}) if isinstance(local_tiers.get("tier3"), dict) else {}
    return {**runtime_tier3, **local_tier3}


def tier3_default_min_alive(runtime_config: dict[str, Any]) -> int:
    return coerce_int(
        tier3_config_from_runtime(runtime_config).get("min_alive_nodes"),
        DEFAULT_TIER3_MIN_ALIVE_NODES,
        minimum=1,
    )


def tier3_config_form_state(
    runtime_config: dict[str, Any],
    local_config: dict[str, Any],
) -> Tier3ConfigFormState:
    tier3 = effective_tier3_config(runtime_config, local_config)
    maximum = coerce_int(tier3.get("max_pool_size"), 100, minimum=1)
    minimum = min(
        maximum,
        coerce_int(
            tier3.get("min_alive_nodes"),
            DEFAULT_TIER3_MIN_ALIVE_NODES,
            minimum=1,
        ),
    )
    return Tier3ConfigFormState(
        enabled=coerce_bool(tier3.get("enabled", False), False),
        min_alive_nodes=minimum,
        max_pool_size=maximum,
    )


def apply_tier3_config_payload(
    payload: dict[str, Any],
    *,
    enabled: bool,
    min_alive_text: str,
    default_min_alive: int,
    max_pool_size: int | None = None,
) -> dict[str, Any]:
    """Write the Bastion policy form into ``payload``.

    Validation-capacity keys are never written here, so hand-written values
    survive a save.  The retired fallback-source switch is dropped from the
    local layer so fallback sources follow the always-on default.
    """
    tiers = ensure_mapping_section(payload, "tiers")
    tier3 = ensure_mapping_section(tiers, "tier3")
    tier3["enabled"] = bool(enabled)
    tier3.pop("bootstrap_enabled", None)
    maximum = coerce_int(
        max_pool_size if max_pool_size is not None else tier3.get("max_pool_size"),
        100,
        minimum=1,
    )
    minimum = min(
        maximum,
        coerce_int(min_alive_text, default_min_alive, minimum=1),
    )
    tier3["min_alive_nodes"] = minimum
    tier3.pop("bootstrap_sources", None)
    tier3.pop("bootstrap_defaults_initialized", None)
    return payload
