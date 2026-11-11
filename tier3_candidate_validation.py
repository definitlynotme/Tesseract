from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from models import NodeStatus


@dataclass(frozen=True)
class Tier3ValidationRetryPlan:
    should_retry: bool
    empty_retry_used: bool
    target_retry_used: bool
    batch_limit: int
    max_retry_count: int


@dataclass(frozen=True)
class Tier3TcpPrefilterPlan:
    enabled: bool
    candidate_limit: int


@dataclass(frozen=True)
class Tier3TcpPrefilterOutcome:
    nodes: list[Any]
    return_without_full_validation: bool
    report_selected_candidate_count: int


@dataclass(frozen=True)
class Tier3PrimaryValidationPlan:
    limit: int
    primary_limit: int
    candidates: list[Any]


@dataclass(frozen=True)
class Tier3ValidationRetryBatch:
    candidates: list[Any]
    next_tested_count: int


@dataclass(frozen=True)
class Tier3ValidationBatchRunResult:
    tested_nodes: list[Any]
    tested_count: int
    empty_retry_used: bool
    target_retry_used: bool


@dataclass(frozen=True)
class Tier3ValidationBatchProgress:
    tested_nodes: list[Any]
    tested_count: int
    candidate_count: int
    selected_candidate_count: int
    current_alive_count: int
    target_alive_count: int
    batch_index: int
    batch_size: int
    limit: int
    retry_limit: int


def tier3_validation_target_alive_count(
    *,
    candidate_count: int,
    target_alive: Any,
    default_target_alive: int,
) -> int:
    if candidate_count <= 0:
        return 0
    return min(max(1, int(target_alive or default_target_alive)), candidate_count)


def tier3_primary_validation_limit(
    *,
    candidate_count: int,
    candidate_limit: Any,
    default_candidate_limit: int,
) -> tuple[int, int]:
    limit = int(default_candidate_limit) if candidate_limit is None else max(0, int(candidate_limit))
    primary_limit = candidate_count if limit <= 0 else min(limit, candidate_count)
    return limit, primary_limit


def tier3_primary_validation_plan(
    selected_nodes: list[Any],
    *,
    candidate_count: int,
    candidate_limit: Any,
    default_candidate_limit: int,
) -> Tier3PrimaryValidationPlan:
    limit, primary_limit = tier3_primary_validation_limit(
        candidate_count=candidate_count,
        candidate_limit=candidate_limit,
        default_candidate_limit=default_candidate_limit,
    )
    primary_limit = min(primary_limit, len(selected_nodes))
    return Tier3PrimaryValidationPlan(
        limit=limit,
        primary_limit=primary_limit,
        candidates=selected_nodes[:primary_limit],
    )


def tier3_validation_retry_limit(
    *,
    empty_retry_limit: Any,
    default_empty_retry_limit: int,
) -> int:
    if empty_retry_limit is None:
        return int(default_empty_retry_limit)
    return max(0, int(empty_retry_limit))


def tier3_validation_batch_limit(
    *,
    batch_size: Any,
    default_batch_size: int,
) -> int:
    if batch_size is None:
        return int(default_batch_size)
    return max(1, int(batch_size))


def tier3_validation_configured_batch_size(
    *,
    configured_batch_size: Any,
    helper_concurrency: Any,
    default_batch_size: int,
) -> int:
    raw_value = configured_batch_size
    if raw_value is None:
        try:
            helper_limit = int(helper_concurrency or 3)
        except (TypeError, ValueError):
            helper_limit = 3
        raw_value = max(helper_limit * 2, int(default_batch_size))
    try:
        return max(1, int(raw_value))
    except (TypeError, ValueError):
        return int(default_batch_size)


def tier3_validation_retry_plan(
    *,
    current_alive_count: int,
    target_alive_count: int,
    retry_limit: int,
    tested_count: int,
    candidate_count: int,
    batch_size: Any,
    default_batch_size: int,
) -> Tier3ValidationRetryPlan:
    should_retry = (
        current_alive_count < target_alive_count
        and retry_limit > tested_count
        and tested_count < candidate_count
    )
    if not should_retry:
        return Tier3ValidationRetryPlan(
            should_retry=False,
            empty_retry_used=False,
            target_retry_used=False,
            batch_limit=0,
            max_retry_count=min(retry_limit, candidate_count),
        )
    return Tier3ValidationRetryPlan(
        should_retry=True,
        empty_retry_used=current_alive_count == 0,
        target_retry_used=True,
        batch_limit=tier3_validation_batch_limit(
            batch_size=batch_size,
            default_batch_size=default_batch_size,
        ),
        max_retry_count=min(retry_limit, candidate_count),
    )


def tier3_next_validation_retry_batch(
    selected_nodes: list[Any],
    *,
    tested_count: int,
    retry_plan: Tier3ValidationRetryPlan,
) -> Tier3ValidationRetryBatch:
    if not retry_plan.should_retry or tested_count >= retry_plan.max_retry_count:
        return Tier3ValidationRetryBatch(candidates=[], next_tested_count=tested_count)
    next_count = min(tested_count + retry_plan.batch_limit, retry_plan.max_retry_count)
    return Tier3ValidationRetryBatch(
        candidates=selected_nodes[tested_count:next_count],
        next_tested_count=next_count,
    )


def tier3_validation_target_reached(
    *,
    current_alive_count: int,
    target_alive_count: int,
) -> bool:
    return current_alive_count >= target_alive_count


def run_tier3_validation_batches(
    selected_nodes: list[Any],
    *,
    primary_plan: Tier3PrimaryValidationPlan,
    target_alive_count: int,
    retry_limit: int,
    batch_size: Any,
    default_batch_size: int,
    candidate_tester: Callable[[list[Any]], list[Any]],
    healthy_counter: Callable[[list[Any]], int],
    batch_observer: Callable[[Tier3ValidationBatchProgress], None] | None = None,
    stream_primary: bool = False,
    should_stop: Callable[[], bool] | None = None,
    stop_on_target: bool = True,
) -> Tier3ValidationBatchRunResult:
    tested_count = 0
    tested_nodes: list[Any] = []
    batch_index = 0

    def stop_requested() -> bool:
        return bool(should_stop and should_stop())

    def notify_batch(batch_len: int) -> None:
        nonlocal batch_index
        if batch_len <= 0:
            return
        batch_index += 1
        if batch_observer is None:
            return
        batch_observer(
            Tier3ValidationBatchProgress(
                tested_nodes=list(tested_nodes),
                tested_count=tested_count,
                candidate_count=len(selected_nodes),
                selected_candidate_count=len(selected_nodes),
                current_alive_count=healthy_counter(tested_nodes),
                target_alive_count=target_alive_count,
                batch_index=batch_index,
                batch_size=batch_len,
                limit=primary_plan.limit,
                retry_limit=retry_limit,
            )
        )

    if primary_plan.primary_limit > 0:
        if stream_primary:
            primary_batch_limit = tier3_validation_batch_limit(
                batch_size=batch_size,
                default_batch_size=default_batch_size,
            )
            while tested_count < primary_plan.primary_limit and not stop_requested():
                next_count = min(tested_count + primary_batch_limit, primary_plan.primary_limit)
                current_batch = primary_plan.candidates[tested_count:next_count]
                tested_nodes.extend(candidate_tester(current_batch))
                tested_count = next_count
                notify_batch(len(current_batch))
                if stop_on_target and tier3_validation_target_reached(
                    current_alive_count=healthy_counter(tested_nodes),
                    target_alive_count=target_alive_count,
                ):
                    break
        elif not stop_requested():
            tested_nodes.extend(candidate_tester(primary_plan.candidates))
            tested_count = len(primary_plan.candidates)
            notify_batch(len(primary_plan.candidates))

    retry_plan = tier3_validation_retry_plan(
        current_alive_count=healthy_counter(tested_nodes),
        target_alive_count=target_alive_count,
        retry_limit=retry_limit,
        tested_count=tested_count,
        candidate_count=len(selected_nodes),
        batch_size=batch_size,
        default_batch_size=default_batch_size,
    )
    if retry_plan.should_retry and not stop_requested():
        while tested_count < retry_plan.max_retry_count and not stop_requested():
            retry_batch = tier3_next_validation_retry_batch(
                selected_nodes,
                tested_count=tested_count,
                retry_plan=retry_plan,
            )
            if not retry_batch.candidates:
                break
            tested_nodes.extend(candidate_tester(retry_batch.candidates))
            tested_count = retry_batch.next_tested_count
            notify_batch(len(retry_batch.candidates))
            if stop_on_target and tier3_validation_target_reached(
                current_alive_count=healthy_counter(tested_nodes),
                target_alive_count=target_alive_count,
            ):
                break

    return Tier3ValidationBatchRunResult(
        tested_nodes=tested_nodes,
        tested_count=tested_count,
        empty_retry_used=retry_plan.empty_retry_used,
        target_retry_used=retry_plan.target_retry_used,
    )


def tier3_tcp_prefilter_plan(
    *,
    fast_probe_limit: int,
    full_validation_enabled: bool,
    prelaunch_validation: str,
    candidate_count: int,
) -> Tier3TcpPrefilterPlan:
    enabled = (
        fast_probe_limit > 0
        and (full_validation_enabled or prelaunch_validation == "tcp")
        and candidate_count > 0
    )
    if not enabled:
        return Tier3TcpPrefilterPlan(enabled=False, candidate_limit=0)
    return Tier3TcpPrefilterPlan(
        enabled=True,
        candidate_limit=min(int(fast_probe_limit), int(candidate_count)),
    )


def tier3_tcp_prefilter_responsive_nodes(nodes: Iterable[Any]) -> list[Any]:
    return [
        node
        for node in nodes
        if getattr(node, "status", None) in {NodeStatus.ALIVE, NodeStatus.SLOW}
    ]


def tier3_tcp_prefilter_outcome(
    responsive_nodes: list[Any],
) -> Tier3TcpPrefilterOutcome:
    if not responsive_nodes:
        return Tier3TcpPrefilterOutcome(
            nodes=[],
            return_without_full_validation=True,
            report_selected_candidate_count=0,
        )
    return Tier3TcpPrefilterOutcome(
        nodes=list(responsive_nodes),
        return_without_full_validation=False,
        report_selected_candidate_count=len(responsive_nodes),
    )


def _required_validation_value(value: int | None, name: str) -> int:
    if value is None:
        raise ValueError(f"{name} is required for this Tier3 validation mode")
    return value


def tier3_refresh_validation_kwargs(
    *,
    intensive_expansion: bool,
    underfilled_empty_retry_limit: int | None = None,
    underfilled_batch_size: int | None = None,
) -> dict[str, Any]:
    if intensive_expansion:
        return {
            "empty_retry_limit": _required_validation_value(underfilled_empty_retry_limit, "underfilled_empty_retry_limit"),
            "batch_size": _required_validation_value(underfilled_batch_size, "underfilled_batch_size"),
        }
    return {}


def tier3_validation_report(
    *,
    candidate_count: int,
    selected_candidate_count: int,
    tested_count: int,
    limit: Any,
    empty_retry_limit: Any,
    empty_retry_used: bool,
    target_retry_used: bool,
    target_alive: int,
    tcp_prefilter_tested_count: int,
    tcp_prefilter_responsive_count: int,
    prelaunch_validation: str,
) -> dict[str, Any]:
    return {
        "candidate_count": candidate_count,
        "selected_candidate_count": selected_candidate_count,
        "tested_count": tested_count,
        "limit": limit,
        "empty_retry_limit": empty_retry_limit,
        "empty_retry_used": empty_retry_used,
        "target_retry_used": target_retry_used,
        "target_alive": target_alive,
        "tcp_prefilter_tested_count": tcp_prefilter_tested_count,
        "tcp_prefilter_responsive_count": tcp_prefilter_responsive_count,
        "prelaunch_validation": prelaunch_validation,
    }


def tier3_prefilter_only_validation_report(
    *,
    candidate_count: int,
    selected_candidate_count: int,
    candidate_limit: Any,
    default_candidate_limit: int,
    empty_retry_limit: Any,
    default_empty_retry_limit: int,
    target_alive: Any,
    default_target_alive: int,
    tcp_prefilter_tested_count: int,
    tcp_prefilter_responsive_count: int,
    prelaunch_validation: str,
) -> dict[str, Any]:
    return tier3_validation_report(
        candidate_count=candidate_count,
        selected_candidate_count=selected_candidate_count,
        tested_count=0,
        limit=candidate_limit if candidate_limit is not None else default_candidate_limit,
        empty_retry_limit=empty_retry_limit if empty_retry_limit is not None else default_empty_retry_limit,
        empty_retry_used=False,
        target_retry_used=False,
        target_alive=tier3_validation_target_alive_count(
            candidate_count=candidate_count,
            target_alive=target_alive,
            default_target_alive=default_target_alive,
        ),
        tcp_prefilter_tested_count=tcp_prefilter_tested_count,
        tcp_prefilter_responsive_count=tcp_prefilter_responsive_count,
        prelaunch_validation=prelaunch_validation,
    )


def tier3_unusable_node_count(nodes: Iterable[Any]) -> int:
    healthy_statuses = {NodeStatus.ALIVE, NodeStatus.SLOW}
    return sum(1 for node in nodes if getattr(node, "status", None) not in healthy_statuses)


def with_tier3_candidate_filter_counts(
    report: dict[str, Any],
    *,
    original_candidate_count: int,
    negative_cache_skipped_count: int,
) -> dict[str, Any]:
    updated = dict(report)
    updated["original_candidate_count"] = original_candidate_count
    updated["negative_cache_skipped_count"] = negative_cache_skipped_count
    return updated


def with_tier3_tested_unusable_count(
    report: dict[str, Any],
    tested_nodes: Iterable[Any],
) -> dict[str, Any]:
    updated = dict(report)
    updated["tested_unusable_count"] = tier3_unusable_node_count(tested_nodes)
    return updated


def with_tier3_revival_counts(
    report: dict[str, Any],
    revival_report: dict[str, Any],
    revived_nodes: Iterable[Any],
) -> dict[str, Any]:
    updated = dict(report)
    updated["revival_candidate_count"] = revival_report["candidate_count"]
    updated["revival_tested_count"] = revival_report["tested_count"]
    updated["revival_unusable_count"] = tier3_unusable_node_count(revived_nodes)
    return updated
