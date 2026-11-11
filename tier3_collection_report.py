from __future__ import annotations

from typing import Any

from tier3_sources import CollectionReport


def _nonnegative_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def tier3_source_stream_outcome_counts(
    report: dict[str, Any] | None,
) -> dict[str, int]:
    """Return the stable, mutually exclusive source-stream outcome counters.

    Older reports exposed ``source_stream_failed_count`` as a mixture of hard
    failures, cancellations and unsupported candidates.  Prefer the detailed
    counters whenever they are present so persisted reports can be corrected
    without a migration.  The legacy field remains as an alias for hard
    failures only.
    """

    payload = report if isinstance(report, dict) else {}
    hard_component_keys = (
        "source_stream_tcp_failed_count",
        "tcp_failed_count",
        "source_stream_full_validation_failed_count",
        "full_validation_failed_count",
    )
    if "source_stream_hard_failed_count" in payload:
        hard_failed = _nonnegative_count(
            payload.get("source_stream_hard_failed_count")
        )
    elif any(key in payload for key in hard_component_keys):
        hard_failed = _nonnegative_count(
            payload.get(
                "source_stream_tcp_failed_count",
                payload.get("tcp_failed_count"),
            )
        ) + _nonnegative_count(
            payload.get(
                "source_stream_full_validation_failed_count",
                payload.get("full_validation_failed_count"),
            )
        )
    else:
        hard_failed = _nonnegative_count(payload.get("source_stream_failed_count"))

    if "source_stream_deferred_count" in payload:
        deferred = _nonnegative_count(payload.get("source_stream_deferred_count"))
    else:
        deferred = _nonnegative_count(
            payload.get(
                "source_stream_tcp_deferred_count",
                payload.get("tcp_deferred_count"),
            )
        ) + _nonnegative_count(
            payload.get(
                "source_stream_full_validation_deferred_count",
                payload.get("full_validation_deferred_count"),
            )
        )

    if "source_stream_cancelled_count" in payload:
        cancelled = _nonnegative_count(payload.get("source_stream_cancelled_count"))
    else:
        cancelled = _nonnegative_count(
            payload.get(
                "source_stream_tcp_cancelled_count",
                payload.get("tcp_cancelled_count"),
            )
        ) + _nonnegative_count(
            payload.get(
                "source_stream_full_validation_cancelled_count",
                payload.get("full_validation_cancelled_count"),
            )
        )

    unsupported = _nonnegative_count(
        payload.get(
            "source_stream_unsupported_count",
            payload.get(
                "source_stream_unsupported_proxy_count",
                payload.get("unsupported_proxy_count"),
            ),
        )
    )
    return {
        "source_stream_hard_failed_count": hard_failed,
        "source_stream_failed_count": hard_failed,
        "source_stream_deferred_count": deferred,
        "source_stream_cancelled_count": cancelled,
        "source_stream_unsupported_count": unsupported,
    }


def merge_collection_reports(reports: list[CollectionReport]) -> CollectionReport:
    sources: list[dict[str, Any]] = []
    for report in reports:
        sources.extend(report.sources)
    return CollectionReport(
        source_count=sum(report.source_count for report in reports),
        configured_source_count=sum(
            max(int(report.configured_source_count or 0), int(report.source_count or 0))
            for report in reports
        ),
        fetched_sources=sum(report.fetched_sources for report in reports),
        failed_sources=sum(report.failed_sources for report in reports),
        node_count=sum(report.node_count for report in reports),
        sources=sources,
    )


def tier3_controller_decision_report(decision: Any) -> dict[str, Any]:
    return {
        "intensity": decision.intensity,
        "source_budget": decision.source_budget,
        "collect_interval": decision.collect_interval,
        "error": decision.error,
        "integral": decision.integral,
    }


def build_tier3_collection_report(
    collection_report: Any,
    validation_report: dict[str, Any],
    decision: Any,
    *,
    startup_mode: bool,
    target_alive: int,
    background_target_alive: int,
    alive_count: int,
    min_alive_nodes: int,
    intensive_expansion: bool,
) -> dict[str, Any]:
    report = collection_report.to_dict()
    report["controller"] = tier3_controller_decision_report(decision)
    report["validation"] = validation_report
    report["startup_mode"] = bool(startup_mode)
    report["target_alive"] = target_alive
    report["background_target_alive"] = background_target_alive
    report["min_alive_nodes"] = min_alive_nodes
    report["background_expansion_due"] = bool(alive_count < background_target_alive)
    report["underfilled_expansion_mode"] = bool(alive_count < min_alive_nodes)
    report["intensive_expansion_mode"] = bool(intensive_expansion)
    return report


def build_tier3_refresh_collection_report(
    collection_report: Any,
    validation_report: dict[str, Any],
    decision: Any,
    *,
    startup_mode: bool,
    target_alive: int,
    background_target_alive: int,
    alive_count: int,
    min_alive_nodes: int,
    intensive_expansion: bool,
    partial_refresh_buffered: bool = False,
    candidate_alive_count: int = 0,
    previous_alive_count: int = 0,
    retained_previous_count: int = 0,
) -> dict[str, Any]:
    report = build_tier3_collection_report(
        collection_report,
        validation_report,
        decision,
        startup_mode=startup_mode,
        target_alive=target_alive,
        background_target_alive=background_target_alive,
        alive_count=alive_count,
        min_alive_nodes=min_alive_nodes,
        intensive_expansion=intensive_expansion,
    )
    if not partial_refresh_buffered:
        return report
    return with_partial_refresh_buffering(
        report,
        candidate_alive_count=candidate_alive_count,
        post_buffer_alive_count=alive_count,
        previous_alive_count=previous_alive_count,
        retained_previous_count=retained_previous_count,
    )


def with_partial_refresh_buffering(
    report: dict[str, Any],
    *,
    candidate_alive_count: int,
    post_buffer_alive_count: int,
    previous_alive_count: int,
    retained_previous_count: int,
) -> dict[str, Any]:
    updated = dict(report)
    updated["partial_refresh_buffered"] = True
    updated["candidate_healthy_count"] = candidate_alive_count
    updated["post_buffer_healthy_count"] = post_buffer_alive_count
    updated["previous_healthy_count"] = previous_alive_count
    updated["retained_previous_candidate_count"] = retained_previous_count
    return updated


def with_empty_refresh_state(
    report: dict[str, Any],
    *,
    empty_refresh_streak: int,
    replace_after: int,
    previous_alive_count: int,
) -> dict[str, Any]:
    updated = dict(report)
    suppressed = empty_refresh_streak < replace_after
    updated["empty_refresh_suppressed"] = suppressed
    updated["empty_refresh_streak"] = empty_refresh_streak
    updated["empty_refresh_replace_after_failures"] = replace_after
    updated["previous_healthy_count"] = previous_alive_count
    if suppressed:
        updated["background_expansion_due"] = True
    return updated
