from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from models import NodeStatus


TIER3_USABLE_STATUSES = {NodeStatus.ALIVE, NodeStatus.SLOW}
RETENTION_MARKERS = (
    "tier3_consecutive_failures",
    "tier3_last_failure_reason",
    "tier3_last_failure_at",
    "tier3_retained_after_failure",
    "tier3_retained_unchecked",
    "tier3_evicted_from_pool",
    "tier3_evicted_from_pool_at",
    "tier3_eviction_failure_count",
    "revived_from_cache",
    "cache_revive_count",
)
STARTUP_MARKERS = (
    "startup_prelaunch_validation",
    "startup_export_rank",
    "startup_core_probe_url",
    "startup_core_delay_ms",
)
CACHE_REVALIDATION_MARKERS = (
    "tier3_cache_revalidation_pending",
)


@dataclass(frozen=True)
class Tier3PartialRefreshBuffer:
    buffered: bool
    nodes: list[Any]
    retained_previous_count: int
    log_retained_count: int


def tier3_active_pool_nodes(nodes: list[Any]) -> list[Any]:
    return [node for node in nodes if getattr(node, "status", None) in TIER3_USABLE_STATUSES]


def clear_tier3_retention_markers(node: Any) -> None:
    extra = getattr(node, "extra", {})
    if not isinstance(extra, dict):
        return
    for key in RETENTION_MARKERS:
        extra.pop(key, None)


def clear_tier3_startup_markers(node: Any) -> None:
    extra = getattr(node, "extra", {})
    if not isinstance(extra, dict):
        return
    for key in STARTUP_MARKERS:
        extra.pop(key, None)


def clear_tier3_startup_markers_for_nodes(nodes: list[Any]) -> list[Any]:
    for node in nodes:
        clear_tier3_startup_markers(node)
    return nodes


def clear_tier3_cache_revalidation_markers(node: Any) -> None:
    extra = getattr(node, "extra", {})
    if not isinstance(extra, dict):
        return
    for key in CACHE_REVALIDATION_MARKERS:
        extra.pop(key, None)


def clear_tier3_cache_revalidation_markers_for_nodes(nodes: list[Any]) -> list[Any]:
    for node in nodes:
        clear_tier3_cache_revalidation_markers(node)
    return nodes


def node_has_tier3_cache_revalidation_pending(node: Any) -> bool:
    extra = getattr(node, "extra", {})
    return bool(isinstance(extra, dict) and extra.get("tier3_cache_revalidation_pending"))


def mark_tier3_cache_revalidation_pending(node: Any) -> None:
    extra = getattr(node, "extra", None)
    if not isinstance(extra, dict):
        extra = {}
        try:
            node.extra = extra
        except Exception:
            return
    extra["tier3_cache_revalidation_pending"] = True


def retained_previous_candidate_count(nodes: list[Any]) -> int:
    return sum(
        1
        for node in nodes
        if getattr(node, "extra", {}).get("tier3_retained_after_failure")
        or getattr(node, "extra", {}).get("tier3_retained_unchecked")
    )


def should_buffer_partial_refresh(
    *,
    startup_mode: bool,
    previous_alive_count: int,
    alive_count: int,
    target_alive: int,
) -> bool:
    return (
        previous_alive_count > 0
        and alive_count < previous_alive_count
        and alive_count < target_alive
    )


def should_attempt_tier3_revival(
    *,
    alive_count: int,
    target_alive: int,
    source_count: int,
) -> bool:
    return True


def merge_tier3_revived_nodes(
    nodes: list[Any],
    revived_nodes: list[Any],
    *,
    startup_mode: bool,
    max_pool_size: int,
    active_pool_fn: Callable[[list[Any]], list[Any]],
    sort_fn: Callable[[list[Any]], list[Any]],
    dedupe_fn: Callable[[list[Any]], list[Any]],
) -> list[Any]:
    merge_candidates = active_pool_fn(revived_nodes)
    return sort_fn(dedupe_fn([*nodes, *merge_candidates]))[:max_pool_size]


def tier3_partial_refresh_buffer(
    nodes: list[Any],
    previous_nodes: list[Any],
    *,
    startup_mode: bool,
    previous_alive_count: int,
    alive_count: int,
    target_alive: int,
    max_pool_size: int,
    sort_fn: Callable[[list[Any]], list[Any]],
    dedupe_fn: Callable[[list[Any]], list[Any]],
) -> Tier3PartialRefreshBuffer:
    if not should_buffer_partial_refresh(
        startup_mode=startup_mode,
        previous_alive_count=previous_alive_count,
        alive_count=alive_count,
        target_alive=target_alive,
    ):
        return Tier3PartialRefreshBuffer(
            buffered=False,
            nodes=nodes,
            retained_previous_count=0,
            log_retained_count=0,
        )
    retained_previous_count = retained_previous_candidate_count(nodes)
    buffered_nodes = sort_fn(dedupe_fn([*nodes, *previous_nodes]))[:max_pool_size]
    if len(buffered_nodes) <= len(nodes) and retained_previous_count <= 0:
        return Tier3PartialRefreshBuffer(
            buffered=False,
            nodes=nodes,
            retained_previous_count=retained_previous_count,
            log_retained_count=0,
        )
    return Tier3PartialRefreshBuffer(
        buffered=True,
        nodes=buffered_nodes,
        retained_previous_count=retained_previous_count,
        log_retained_count=max(retained_previous_count, len(buffered_nodes) - len(nodes)),
    )


def tier3_pool_nodes_after_validation(
    tested_nodes: list[Any],
    previous_nodes: list[Any],
    *,
    threshold: int,
    identity_fn: Callable[[Any], str],
    dedupe_fn: Callable[[list[Any]], list[Any]],
    now_iso_fn: Callable[[], str],
) -> list[Any]:
    previous_by_identity = {identity_fn(node): node for node in previous_nodes}
    tested_identities: set[str] = set()
    pool_nodes: list[Any] = []
    for node in tested_nodes:
        identity = identity_fn(node)
        tested_identities.add(identity)
        if getattr(node, "status", None) in TIER3_USABLE_STATUSES:
            clear_tier3_retention_markers(node)
            clear_tier3_cache_revalidation_markers(node)
            pool_nodes.append(node)
            continue
        previous = previous_by_identity.get(identity)
        if previous is None:
            continue
        if node_has_tier3_cache_revalidation_pending(previous):
            continue
        failure_count = int(previous.extra.get("tier3_consecutive_failures", 0) or 0) + 1
        if failure_count >= threshold:
            continue
        retained = node.clone()
        retained.extra["tier3_consecutive_failures"] = failure_count
        retained.extra["tier3_last_failure_reason"] = node.suspicious_reason or node.status.value
        retained.extra["tier3_last_failure_at"] = node.last_checked_at or now_iso_fn()
        retained.extra["tier3_retained_after_failure"] = True
        retained.extra.pop("tier3_retained_unchecked", None)
        clear_tier3_cache_revalidation_markers(retained)
        pool_nodes.append(retained)

    for previous in previous_nodes:
        identity = identity_fn(previous)
        if identity in tested_identities:
            continue
        if node_has_tier3_cache_revalidation_pending(previous):
            continue
        failure_count = int(previous.extra.get("tier3_consecutive_failures", 0) or 0)
        if previous.status not in TIER3_USABLE_STATUSES and failure_count >= threshold:
            continue
        retained = previous.clone()
        retained.extra["tier3_retained_unchecked"] = True
        clear_tier3_cache_revalidation_markers(retained)
        pool_nodes.append(retained)
    return dedupe_fn(pool_nodes)
