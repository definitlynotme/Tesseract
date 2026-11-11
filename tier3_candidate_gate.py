from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class Tier3CandidateGateResult:
    candidates: list[Any]
    original_candidate_count: int
    negative_cache_skipped_count: int
    preserve_validation_order: bool = False
    validation_lane_counts: dict[str, int] = field(default_factory=dict)


def tier3_negative_cache_exempt_identities(
    candidates: list[Any],
    previous_nodes: list[Any],
    *,
    identity_fn: Callable[[Any], Any],
) -> set[Any]:
    return {identity_fn(node) for node in previous_nodes}


def tier3_candidate_gate_result(
    original_candidates: list[Any],
    filtered_candidates: list[Any],
    *,
    negative_cache_skipped_count: int,
    startup_mode: bool,
    preserve_validation_order: bool = False,
    validation_lane_counts: dict[str, int] | None = None,
) -> Tier3CandidateGateResult:
    original_candidate_count = len(original_candidates)
    return Tier3CandidateGateResult(
        candidates=list(filtered_candidates),
        original_candidate_count=original_candidate_count,
        negative_cache_skipped_count=negative_cache_skipped_count,
        preserve_validation_order=preserve_validation_order,
        validation_lane_counts=dict(validation_lane_counts or {}),
    )


@dataclass(frozen=True)
class Tier3CandidateLayeredOrder:
    candidates: list[Any]
    lane_counts: dict[str, int]


def _interleave_candidate_lanes(primary: list[Any], secondary: list[Any]) -> list[Any]:
    ordered: list[Any] = []
    max_len = max(len(primary), len(secondary))
    for index in range(max_len):
        if index < len(primary):
            ordered.append(primary[index])
        if index < len(secondary):
            ordered.append(secondary[index])
    return ordered


def tier3_background_expansion_candidate_layers(
    candidates: list[Any],
    previous_nodes: list[Any],
    *,
    identity_fn: Callable[[Any], Any],
    include_pool_check: bool = False,
    exploration_limit: int | None = None,
) -> Tier3CandidateLayeredOrder:
    previous_identities = {identity_fn(node) for node in previous_nodes}
    exploration: list[Any] = []
    incumbent: list[Any] = []

    for candidate in candidates:
        if identity_fn(candidate) in previous_identities:
            incumbent.append(candidate)
        else:
            exploration.append(candidate)

    if include_pool_check:
        if exploration_limit is None or exploration_limit <= 0:
            selected_exploration = list(exploration)
            deferred_exploration_count = 0
        else:
            selected_exploration = exploration[:exploration_limit]
            deferred_exploration_count = max(0, len(exploration) - len(selected_exploration))
        return Tier3CandidateLayeredOrder(
            candidates=_interleave_candidate_lanes(selected_exploration, incumbent),
            lane_counts={
                "exploration": len(selected_exploration),
                "exploration_deferred": deferred_exploration_count,
                "incumbent_tested": len(incumbent),
                "incumbent_retained": 0,
            },
        )
    if exploration_limit is not None and exploration_limit > 0:
        selected_exploration = exploration[:exploration_limit]
        deferred_exploration_count = max(0, len(exploration) - len(selected_exploration))
    else:
        selected_exploration = list(exploration)
        deferred_exploration_count = 0
    lane_counts = {
        "exploration": len(selected_exploration),
        "incumbent_retained": len(incumbent),
    }
    if deferred_exploration_count:
        lane_counts["exploration_deferred"] = deferred_exploration_count
    return Tier3CandidateLayeredOrder(
        candidates=selected_exploration,
        lane_counts=lane_counts,
    )
