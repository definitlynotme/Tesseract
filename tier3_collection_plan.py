from __future__ import annotations

from typing import Any


def _coerce_interval(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return max(1, int(default))


def tier3_collect_bounds(
    tier3_config: dict[str, Any],
    *,
    default_min_interval: int,
    default_max_interval: int,
) -> tuple[int, int]:
    min_interval = _coerce_interval(
        tier3_config.get("min_collect_interval", default_min_interval),
        default_min_interval,
    )
    default_max = _coerce_interval(
        tier3_config.get("auto_collect_interval", default_max_interval),
        default_max_interval,
    )
    max_interval = _coerce_interval(
        tier3_config.get("max_collect_interval", default_max),
        default_max,
    )
    if max_interval < min_interval:
        max_interval = min_interval
    return min_interval, max_interval


def tier3_collection_entry_groups(
    tier3a_entries: list[dict[str, Any]],
    tier3b_entries: list[dict[str, Any]],
    *,
    source_budget: int,
) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    if tier3a_entries:
        groups.append(tier3a_entries)
    budgeted_tier3b_entries = tier3b_entries[:source_budget]
    if budgeted_tier3b_entries:
        groups.append(budgeted_tier3b_entries)
    return groups


def tier3_bootstrap_threshold(
    *,
    target_alive: int,
    min_alive_nodes: int,
    intensive_expansion: bool,
) -> int:
    # The collection target already encodes the current phase: startup/underfilled
    # refreshes pass the admission floor, while background expansion passes the
    # PI controller's larger target. Bootstrap must follow that target so an
    # idle pool at the floor can still grow.
    return max(target_alive, min_alive_nodes)


def should_collect_tier3_bootstrap(
    *,
    bootstrap_enabled: bool,
    previous_alive_count: int,
    bootstrap_threshold: int,
    collected_node_count: int,
    startup_mode: bool,
) -> bool:
    # Startup changes collection timeouts/budgets, not bootstrap admission.
    return bool(
        bootstrap_enabled
        and previous_alive_count < bootstrap_threshold
        and collected_node_count < bootstrap_threshold
    )


def should_refresh_tier3_during_check(
    *,
    force_collect: bool,
    has_runtime_nodes: bool,
    elapsed_seconds: float,
    min_collect_interval: int,
) -> bool:
    if not force_collect and has_runtime_nodes:
        return False
    return bool(force_collect or elapsed_seconds >= min_collect_interval)


def should_urgent_recheck_tier3(
    *,
    tier1_alive_count: int,
    tier2_alive_count: int,
    tier3_enabled: bool,
    tier3_node_count: int,
    tier3_alive_count: int,
    urgent_due: bool,
) -> bool:
    return bool(
        tier1_alive_count == 0
        and tier3_enabled
        and tier2_alive_count == 0
        and tier3_node_count > 0
        and tier3_alive_count == 0
        and urgent_due
    )


def tier3_collect_interval_for_pool(
    *,
    collect_interval: int,
    alive_count: int,
    min_alive_nodes: int,
    underfilled_collect_interval: int,
) -> int:
    if alive_count < min_alive_nodes:
        return min(collect_interval, underfilled_collect_interval)
    return collect_interval
