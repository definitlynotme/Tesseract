from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from models import count_healthy_nodes
from tier3_candidate_validation import with_tier3_tested_unusable_count
from tier3_pool_retention import (
    clear_tier3_cache_revalidation_markers_for_nodes,
    clear_tier3_startup_markers_for_nodes,
)


@dataclass(frozen=True)
class Tier3ValidatedPoolOutcome:
    nodes: list[Any]
    alive_count: int
    validation_report: dict[str, Any]
    observed_nodes: list[Any]


def normalize_tier3_validated_pool(
    nodes: list[Any],
    validation_report: dict[str, Any],
    previous_nodes: list[Any],
    *,
    startup_mode: bool,
    max_pool_size: int,
    pool_nodes_after_validation_fn: Callable[[list[Any], list[Any]], list[Any]],
    sort_fn: Callable[[list[Any]], list[Any]],
) -> Tier3ValidatedPoolOutcome:
    observed_nodes = list(nodes)
    normalized_nodes = list(nodes)
    clear_tier3_startup_markers_for_nodes(normalized_nodes)
    clear_tier3_cache_revalidation_markers_for_nodes(normalized_nodes)
    normalized_nodes = pool_nodes_after_validation_fn(normalized_nodes, previous_nodes)
    updated_report = with_tier3_tested_unusable_count(validation_report, observed_nodes)
    normalized_nodes = sort_fn(normalized_nodes)[:max_pool_size]
    return Tier3ValidatedPoolOutcome(
        nodes=normalized_nodes,
        alive_count=count_healthy_nodes(normalized_nodes),
        validation_report=updated_report,
        observed_nodes=observed_nodes,
    )
