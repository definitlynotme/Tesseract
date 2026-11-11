from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config_utils import coerce_bool
from models import NodeStatus, count_healthy_nodes
from tier3_collection_report import tier3_source_stream_outcome_counts
from tier3_pool_retention import node_has_tier3_cache_revalidation_pending
from tier3_origin import tier3_auto_pool_nodes, tier3_user_pool_nodes
from tier3_verification_state import (
    node_has_startup_core_verification,
    node_is_unverified_tcp_startup_candidate,
)


@dataclass(frozen=True, slots=True)
class Tier3PoolCountSnapshot:
    revision: int
    healthy: int
    total: int


@dataclass(frozen=True, slots=True)
class Tier3AutoPoolCountSnapshot:
    revision: int
    healthy: int
    total: int


@dataclass(frozen=True, slots=True)
class Tier3UserPoolHealthSnapshot:
    revision: int
    healthy: int
    total: int


@dataclass(frozen=True, slots=True)
class Tier3StartupGateProgress:
    healthy: int
    required: int


def tier3_main_pool_nodes_for_display(nodes: list[Any], report: dict[str, Any] | None = None) -> list[Any]:
    return [
        node
        for node in nodes
        if not node_is_unverified_tcp_startup_candidate(node)
        and not node_has_tier3_cache_revalidation_pending(node)
    ]


def tier3_auto_pool_nodes_for_display(nodes: list[Any], report: dict[str, Any] | None = None) -> list[Any]:
    return tier3_auto_pool_nodes(tier3_main_pool_nodes_for_display(nodes, report))


def tier3_user_pool_nodes_for_display(nodes: list[Any], report: dict[str, Any] | None = None) -> list[Any]:
    return tier3_user_pool_nodes(tier3_main_pool_nodes_for_display(nodes, report))


def tier3_core_verified_nodes_for_display(nodes: list[Any], report: dict[str, Any] | None) -> list[Any]:
    return tier3_main_pool_nodes_for_display(nodes, report)


def tier3_effective_nodes_for_display(
    nodes: list[Any],
    health_owner: Any | None = None,
) -> list[Any]:
    """Project Manager's volatile runtime health onto a display-only copy.

    Older snapshot producers do not expose the runtime-health API, so the
    view keeps the canonical nodes as a compatibility fallback.  The helper
    never mutates the durable Tier3 pool.
    """

    current = list(nodes)
    getter = getattr(health_owner, "runtime_effective_nodes", None)
    if not callable(getter):
        return current
    try:
        effective = getter("tier3", current)
    except (AttributeError, KeyError, TypeError, ValueError):
        return current
    return list(effective) if effective is not None else current


def tier3_collection_core_counts(nodes: list[Any]) -> dict[str, int]:
    return {
        "core_verified_count": sum(1 for node in nodes if node_has_startup_core_verification(node)),
        "core_usable_count": sum(
            1
            for node in nodes
            if getattr(node, "status", None) in {NodeStatus.ALIVE, NodeStatus.SLOW}
            and node_has_startup_core_verification(node)
        ),
        "tcp_candidate_count": sum(1 for node in nodes if node_is_unverified_tcp_startup_candidate(node)),
    }


def _nonnegative_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _nonnegative_float(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def tier3_pool_chart_stats(tier: dict[str, Any] | None) -> dict[str, int]:
    payload = tier if isinstance(tier, dict) else {}
    auto_pool = payload.get("auto_pool")
    pool_payload = auto_pool if isinstance(auto_pool, dict) else payload
    if payload.get("core_inventory_verified") is False:
        return {}
    alive = _nonnegative_count(pool_payload.get("alive"))
    slow = _nonnegative_count(pool_payload.get("slow"))
    has_current_ratio = (
        "current_total" in pool_payload
        and "current_healthy" in pool_payload
    )
    if payload.get("core_inventory_verified") is True and not has_current_ratio:
        return {}
    if has_current_ratio:
        total = _nonnegative_count(pool_payload.get("current_total"))
        healthy = min(
            total,
            _nonnegative_count(pool_payload.get("current_healthy")),
        )
    else:
        # Compatibility for snapshots that predate explicit Core inventory
        # truth. New runtime snapshots always carry core_inventory_verified.
        total = _nonnegative_count(pool_payload.get("total"))
        healthy = min(total, alive + slow)
    return {"total": total, "healthy": healthy}


def tier3_display_status_counts(nodes: list[Any]) -> dict[str, int]:
    return {
        "total": len(nodes),
        "alive": sum(1 for node in nodes if getattr(node, "status", None) == NodeStatus.ALIVE),
        "slow": sum(1 for node in nodes if getattr(node, "status", None) == NodeStatus.SLOW),
        "healthy": count_healthy_nodes(nodes),
        "dead": sum(1 for node in nodes if getattr(node, "status", None) == NodeStatus.DEAD),
        "suspicious": sum(1 for node in nodes if getattr(node, "status", None) == NodeStatus.SUSPICIOUS),
    }


def tier3_collection_stats(
    *,
    report: dict[str, Any] | None,
    nodes: list[Any],
    tier3_source_count: int,
    background_target_alive: int,
    next_collect_due_seconds: int,
    refresh_progress: dict[str, Any] | None = None,
) -> dict[str, Any]:
    report = report if isinstance(report, dict) else {}
    progress = refresh_progress if isinstance(refresh_progress, dict) else {}
    outcome_counts = tier3_source_stream_outcome_counts(report)
    progress_outcome_keys = {
        "source_stream_failed_count",
        "source_stream_hard_failed_count",
        "source_stream_deferred_count",
        "source_stream_cancelled_count",
        "source_stream_unsupported_count",
        "source_stream_tcp_failed_count",
        "source_stream_tcp_deferred_count",
        "source_stream_tcp_cancelled_count",
        "source_stream_full_validation_failed_count",
        "source_stream_full_validation_deferred_count",
        "source_stream_full_validation_cancelled_count",
        "source_stream_unsupported_proxy_count",
    }
    progress_outcome_counts = (
        tier3_source_stream_outcome_counts(progress)
        if any(key in progress for key in progress_outcome_keys)
        else outcome_counts
    )
    validation = report.get("validation", {})
    validation = validation if isinstance(validation, dict) else {}
    core_repair = report.get("startup_core_repair", {})
    core_repair = core_repair if isinstance(core_repair, dict) else {}
    collection = {
        "source_count": int(report.get("source_count", tier3_source_count) or 0),
        "fetched_sources": int(report.get("fetched_sources", 0) or 0),
        "failed_sources": int(report.get("failed_sources", 0) or 0),
        "node_count": int(report.get("node_count", 0) or 0),
        "candidate_count": int(validation.get("candidate_count", 0) or 0),
        "original_candidate_count": int(validation.get("original_candidate_count", validation.get("candidate_count", 0)) or 0),
        "negative_cache_skipped_count": int(validation.get("negative_cache_skipped_count", 0) or 0),
        "selected_candidate_count": int(validation.get("selected_candidate_count", 0) or 0),
        "tested_count": int(validation.get("tested_count", 0) or 0),
        "tested_unusable_count": int(validation.get("tested_unusable_count", 0) or 0),
        "revival_unusable_count": int(validation.get("revival_unusable_count", 0) or 0),
        "target_alive_count": int(validation.get("target_alive", report.get("target_alive", 0)) or 0),
        "background_target_alive_count": int(background_target_alive or 0),
        "startup_gate_alive_count": int(report.get("min_alive_nodes", 0) or 0),
        "pi_intensity": _nonnegative_float(report.get("pi_intensity")),
        "expansion_pressure": _nonnegative_float(report.get("expansion_pressure")),
        "tier3_throughput_pressure": _nonnegative_float(report.get("tier3_throughput_pressure", report.get("expansion_pressure"))),
        "reserve_deficit": int(report.get("reserve_deficit", 0) or 0),
        "reserve_deficit_ratio": _nonnegative_float(report.get("reserve_deficit_ratio")),
        "backlog_pressure": _nonnegative_float(report.get("backlog_pressure")),
        "recent_pass_rate": _nonnegative_float(report.get("recent_pass_rate", 1.0)),
        "pass_rate_pressure": _nonnegative_float(report.get("pass_rate_pressure")),
        "pressure_source_budget": int(report.get("pressure_source_budget", 0) or 0),
        "validation_budget": int(report.get("validation_budget", 0) or 0),
        "validation_session_wave_count": int(report.get("validation_session_wave_count", 0) or 0),
        "validation_session_budget": int(report.get("validation_session_budget", 0) or 0),
        "validation_batch_size": int(report.get("validation_batch_size", 0) or 0),
        "validation_concurrency": int(report.get("validation_concurrency", validation.get("validation_concurrency", 0)) or 0),
        "validation_worker_count": int(report.get("validation_worker_count", 0) or 0),
        "validation_chunk_size": int(report.get("validation_chunk_size", 0) or 0),
        "source_stream_budget": int(report.get("source_stream_budget", 0) or 0),
        "source_stream_queued_candidate_count": int(report.get("source_stream_queued_candidate_count", 0) or 0),
        "source_stream_tested_count": int(report.get("source_stream_tested_count", 0) or 0),
        "source_stream_observed_candidate_count": int(report.get("source_stream_observed_candidate_count", 0) or 0),
        "source_stream_healthy_observed_count": int(report.get("source_stream_healthy_observed_count", 0) or 0),
        **outcome_counts,
        "source_stream_committed_alive_count": int(report.get("source_stream_committed_alive_count", 0) or 0),
        "source_stream_committed_total_count": int(report.get("source_stream_committed_total_count", 0) or 0),
        "source_stream_streamed_batch_count": int(report.get("source_stream_streamed_batch_count", 0) or 0),
        "source_stream_backlog_count": int(report.get("source_stream_backlog_count", 0) or 0),
        "source_stream_queued_batches": int(report.get("source_stream_queued_batches", 0) or 0),
        "source_stream_inflight_batches": int(report.get("source_stream_inflight_batches", 0) or 0),
        "source_stream_window": int(report.get("source_stream_window", 0) or 0),
        "tcp_prefilter_tested_count": int(validation.get("tcp_prefilter_tested_count", 0) or 0),
        "tcp_prefilter_responsive_count": int(validation.get("tcp_prefilter_responsive_count", 0) or 0),
        "prelaunch_validation": str(validation.get("prelaunch_validation") or ""),
        "startup_mode": coerce_bool(report.get("startup_mode", False), False),
        "background_expansion_due": coerce_bool(report.get("background_expansion_due", False), False),
        "underfilled_expansion_mode": coerce_bool(report.get("underfilled_expansion_mode", False), False),
        "partial_refresh_buffered": coerce_bool(report.get("partial_refresh_buffered", False), False),
        "retained_previous_candidate_count": int(report.get("retained_previous_candidate_count", 0) or 0),
        "candidate_healthy_count": int(report.get("candidate_healthy_count", 0) or 0),
        "post_buffer_healthy_count": int(report.get("post_buffer_healthy_count", 0) or 0),
        "previous_healthy_count": int(report.get("previous_healthy_count", 0) or 0),
        "core_working_count": int(core_repair.get("working_count", 0) or 0),
        "core_pruned_count": int(core_repair.get("pruned_count", 0) or 0),
        "next_collect_due_seconds": next_collect_due_seconds,
    }
    qualification_source = progress if (
        "runtime_qualified_count" in progress
        or "runtime_exported_count" in progress
    ) else report
    collection["runtime_qualified_count"] = _nonnegative_count(
        qualification_source.get("runtime_qualified_count")
    )
    collection["runtime_exported_count"] = _nonnegative_count(
        qualification_source.get("runtime_exported_count")
    )
    if report.get("startup_core_verification_failed"):
        collection["startup_core_verification_failed"] = True
    if progress:
        collection.update(
            {
                "refresh_running": coerce_bool(progress.get("running", False), False),
                "refresh_phase": str(progress.get("phase") or ""),
                "refresh_candidate_count": int(progress.get("candidate_count", 0) or 0),
                "refresh_selected_candidate_count": int(progress.get("selected_candidate_count", 0) or 0),
                "refresh_tested_count": int(progress.get("tested_count", 0) or 0),
                "refresh_healthy_count": int(progress.get("healthy_count", 0) or 0),
                "refresh_committed_alive_count": int(progress.get("committed_alive_count", 0) or 0),
                "refresh_committed_total_count": int(progress.get("committed_total_count", 0) or 0),
                "refresh_added_count": int(progress.get("added_count", 0) or 0),
                "refresh_startup_mode": coerce_bool(progress.get("startup_mode", collection["startup_mode"]), collection["startup_mode"]),
                "refresh_target_alive_count": int(progress.get("target_alive", 0) or 0),
                "refresh_background_target_alive_count": int(progress.get("background_target_alive", 0) or 0),
                "refresh_startup_gate_alive_count": int(progress.get("startup_gate_alive", collection["startup_gate_alive_count"]) or 0),
                "refresh_pi_intensity": _nonnegative_float(progress.get("pi_intensity", collection["pi_intensity"])),
                "refresh_expansion_pressure": _nonnegative_float(progress.get("expansion_pressure", collection["expansion_pressure"])),
                "refresh_tier3_throughput_pressure": _nonnegative_float(progress.get("tier3_throughput_pressure", collection["tier3_throughput_pressure"])),
                "refresh_reserve_deficit": int(progress.get("reserve_deficit", collection["reserve_deficit"]) or 0),
                "refresh_reserve_deficit_ratio": _nonnegative_float(progress.get("reserve_deficit_ratio", collection["reserve_deficit_ratio"])),
                "refresh_backlog_pressure": _nonnegative_float(progress.get("backlog_pressure", collection["backlog_pressure"])),
                "refresh_recent_pass_rate": _nonnegative_float(progress.get("recent_pass_rate", collection["recent_pass_rate"])),
                "refresh_pass_rate_pressure": _nonnegative_float(progress.get("pass_rate_pressure", collection["pass_rate_pressure"])),
                "refresh_pressure_source_budget": int(progress.get("pressure_source_budget", collection["pressure_source_budget"]) or 0),
                "refresh_validation_budget": int(progress.get("validation_budget", collection["validation_budget"]) or 0),
                "refresh_validation_session_wave_count": int(progress.get("validation_session_wave_count", collection["validation_session_wave_count"]) or 0),
                "refresh_validation_session_budget": int(progress.get("validation_session_budget", collection["validation_session_budget"]) or 0),
                "refresh_validation_batch_size": int(progress.get("validation_batch_size", collection["validation_batch_size"]) or 0),
                "refresh_validation_concurrency": int(progress.get("validation_concurrency", collection["validation_concurrency"]) or 0),
                "refresh_validation_worker_count": int(progress.get("validation_worker_count", collection["validation_worker_count"]) or 0),
                "refresh_validation_chunk_size": int(progress.get("validation_chunk_size", collection["validation_chunk_size"]) or 0),
                "refresh_source_stream_budget": int(progress.get("source_stream_budget", collection["source_stream_budget"]) or 0),
                "refresh_source_stream_queued_candidate_count": int(progress.get("source_stream_queued_candidate_count", collection["source_stream_queued_candidate_count"]) or 0),
                "refresh_source_stream_tested_count": int(progress.get("source_stream_tested_count", collection["source_stream_tested_count"]) or 0),
                "refresh_source_stream_observed_candidate_count": int(progress.get("source_stream_observed_candidate_count", collection["source_stream_observed_candidate_count"]) or 0),
                "refresh_source_stream_healthy_observed_count": int(progress.get("source_stream_healthy_observed_count", collection["source_stream_healthy_observed_count"]) or 0),
                "refresh_source_stream_hard_failed_count": progress_outcome_counts[
                    "source_stream_hard_failed_count"
                ],
                "refresh_source_stream_failed_count": progress_outcome_counts[
                    "source_stream_failed_count"
                ],
                "refresh_source_stream_deferred_count": progress_outcome_counts[
                    "source_stream_deferred_count"
                ],
                "refresh_source_stream_cancelled_count": progress_outcome_counts[
                    "source_stream_cancelled_count"
                ],
                "refresh_source_stream_unsupported_count": progress_outcome_counts[
                    "source_stream_unsupported_count"
                ],
                "refresh_source_stream_committed_alive_count": int(progress.get("source_stream_committed_alive_count", collection["source_stream_committed_alive_count"]) or 0),
                "refresh_source_stream_committed_total_count": int(progress.get("source_stream_committed_total_count", collection["source_stream_committed_total_count"]) or 0),
                "refresh_source_stream_streamed_batch_count": int(progress.get("source_stream_streamed_batch_count", collection["source_stream_streamed_batch_count"]) or 0),
                "refresh_source_stream_backlog_count": int(progress.get("source_stream_backlog_count", collection["source_stream_backlog_count"]) or 0),
                "refresh_source_stream_queued_batches": int(progress.get("source_stream_queued_batches", collection["source_stream_queued_batches"]) or 0),
                "refresh_source_stream_inflight_batches": int(progress.get("source_stream_inflight_batches", collection["source_stream_inflight_batches"]) or 0),
                "refresh_source_stream_window": int(progress.get("source_stream_window", collection["source_stream_window"]) or 0),
            }
        )
        if progress.get("validation_lane_counts"):
            collection["refresh_validation_lane_counts"] = dict(progress.get("validation_lane_counts", {}) or {})
    if validation.get("validation_lane_counts"):
        collection["validation_lane_counts"] = dict(validation.get("validation_lane_counts", {}) or {})
    if collection.get("refresh_running"):
        collection["candidate_count"] = max(
            collection["candidate_count"],
            collection["refresh_candidate_count"],
        )
        collection["selected_candidate_count"] = max(
            collection["selected_candidate_count"],
            collection["refresh_selected_candidate_count"],
        )
        collection["tested_count"] = max(
            collection["tested_count"],
            collection["refresh_tested_count"],
        )
        collection["target_alive_count"] = max(
            collection["target_alive_count"],
            collection["refresh_target_alive_count"],
        )
        # This field describes the current configured target. Keep the
        # in-flight refresh target in refresh_background_target_alive_count so
        # a persisted report cannot make diagnostics show a stale policy.
        collection["background_target_alive_count"] = int(
            background_target_alive or 0
        )
        collection["tier3_throughput_pressure"] = max(
            collection["tier3_throughput_pressure"],
            collection["refresh_tier3_throughput_pressure"],
        )
        collection["backlog_pressure"] = max(
            collection["backlog_pressure"],
            collection["refresh_backlog_pressure"],
        )
        collection["pass_rate_pressure"] = max(
            collection["pass_rate_pressure"],
            collection["refresh_pass_rate_pressure"],
        )
        collection["validation_budget"] = max(
            collection["validation_budget"],
            collection["refresh_validation_budget"],
        )
        collection["validation_session_wave_count"] = max(
            collection["validation_session_wave_count"],
            collection["refresh_validation_session_wave_count"],
        )
        collection["validation_session_budget"] = max(
            collection["validation_session_budget"],
            collection["refresh_validation_session_budget"],
        )
        collection["pressure_source_budget"] = max(
            collection["pressure_source_budget"],
            collection["refresh_pressure_source_budget"],
        )
        collection["validation_batch_size"] = max(
            collection["validation_batch_size"],
            collection["refresh_validation_batch_size"],
        )
        collection["validation_concurrency"] = max(
            collection["validation_concurrency"],
            collection["refresh_validation_concurrency"],
        )
        collection["validation_worker_count"] = max(
            collection["validation_worker_count"],
            collection["refresh_validation_worker_count"],
        )
        collection["validation_chunk_size"] = max(
            collection["validation_chunk_size"],
            collection["refresh_validation_chunk_size"],
        )
        collection["source_stream_budget"] = max(
            collection["source_stream_budget"],
            collection["refresh_source_stream_budget"],
        )
        collection["source_stream_queued_candidate_count"] = max(
            collection["source_stream_queued_candidate_count"],
            collection["refresh_source_stream_queued_candidate_count"],
        )
        collection["source_stream_tested_count"] = max(
            collection["source_stream_tested_count"],
            collection["refresh_source_stream_tested_count"],
        )
        collection["source_stream_observed_candidate_count"] = max(
            collection["source_stream_observed_candidate_count"],
            collection["refresh_source_stream_observed_candidate_count"],
        )
        collection["source_stream_healthy_observed_count"] = max(
            collection["source_stream_healthy_observed_count"],
            collection["refresh_source_stream_healthy_observed_count"],
        )
        for metric in (
            "hard_failed_count",
            "failed_count",
            "deferred_count",
            "cancelled_count",
            "unsupported_count",
        ):
            key = f"source_stream_{metric}"
            refresh_key = f"refresh_source_stream_{metric}"
            collection[key] = max(collection[key], collection[refresh_key])
        collection["source_stream_failed_count"] = collection[
            "source_stream_hard_failed_count"
        ]
        collection["source_stream_committed_alive_count"] = max(
            collection["source_stream_committed_alive_count"],
            collection["refresh_source_stream_committed_alive_count"],
        )
        collection["source_stream_committed_total_count"] = max(
            collection["source_stream_committed_total_count"],
            collection["refresh_source_stream_committed_total_count"],
        )
        collection["source_stream_streamed_batch_count"] = max(
            collection["source_stream_streamed_batch_count"],
            collection["refresh_source_stream_streamed_batch_count"],
        )
        collection["source_stream_backlog_count"] = max(
            collection["source_stream_backlog_count"],
            collection["refresh_source_stream_backlog_count"],
        )
        collection["source_stream_queued_batches"] = max(
            collection["source_stream_queued_batches"],
            collection["refresh_source_stream_queued_batches"],
        )
        collection["source_stream_inflight_batches"] = max(
            collection["source_stream_inflight_batches"],
            collection["refresh_source_stream_inflight_batches"],
        )
        collection["source_stream_window"] = max(
            collection["source_stream_window"],
            collection["refresh_source_stream_window"],
        )
    collection.update(tier3_collection_core_counts(nodes))
    return collection


def tier3_stats_snapshot(
    *,
    enabled: bool,
    nodes: list[Any],
    report: dict[str, Any] | None,
    tier3_source_count: int,
    background_target_alive: int,
    next_collect_due_seconds: int,
    level: int,
    level_reason: str,
    controller: dict[str, Any],
    revival_count: int,
    refresh_progress: dict[str, Any] | None = None,
    pool_count_snapshot: Tier3PoolCountSnapshot | None = None,
    auto_pool_count_snapshot: Tier3AutoPoolCountSnapshot | None = None,
    user_pool_health_snapshot: Tier3UserPoolHealthSnapshot | None = None,
    startup_gate_progress: Tier3StartupGateProgress | None = None,
    runtime_health_owner: Any | None = None,
    runtime_truth: dict[str, Any] | None = None,
) -> dict[str, Any]:
    report_payload = report if isinstance(report, dict) else {}
    active_nodes = list(nodes) if enabled else []
    effective_nodes = tier3_effective_nodes_for_display(
        active_nodes,
        runtime_health_owner,
    )
    display_nodes = tier3_main_pool_nodes_for_display(effective_nodes, report_payload)
    display_counts = tier3_display_status_counts(display_nodes)
    auto_pool_nodes = tier3_auto_pool_nodes_for_display(effective_nodes, report_payload)
    auto_pool_counts = tier3_display_status_counts(auto_pool_nodes)
    user_pool_nodes = tier3_user_pool_nodes_for_display(effective_nodes, report_payload)
    user_pool_counts = tier3_display_status_counts(user_pool_nodes)
    snapshot = pool_count_snapshot or Tier3PoolCountSnapshot(
        revision=0,
        healthy=display_counts["healthy"],
        total=display_counts["total"],
    )
    auto_snapshot = auto_pool_count_snapshot or Tier3AutoPoolCountSnapshot(
        revision=int(snapshot.revision),
        healthy=auto_pool_counts["healthy"],
        total=auto_pool_counts["total"],
    )
    user_snapshot = user_pool_health_snapshot or Tier3UserPoolHealthSnapshot(
        revision=0,
        healthy=user_pool_counts["healthy"],
        total=user_pool_counts["total"],
    )
    gate = startup_gate_progress or Tier3StartupGateProgress(
        healthy=snapshot.healthy,
        required=0,
    )
    collection = tier3_collection_stats(
        report=report_payload,
        nodes=effective_nodes,
        tier3_source_count=tier3_source_count,
        background_target_alive=background_target_alive,
        next_collect_due_seconds=next_collect_due_seconds,
        refresh_progress=refresh_progress,
    )
    collection["pool_revision"] = int(auto_snapshot.revision)
    collection["pool_healthy_count"] = int(auto_snapshot.healthy)
    collection["pool_total_count"] = int(auto_snapshot.total)
    collection["overview_healthy_count"] = int(snapshot.healthy)
    collection["overview_total_count"] = int(snapshot.total)
    collection["tier3a_health_revision"] = int(user_snapshot.revision)
    collection["startup_gate_healthy_count"] = int(gate.healthy)
    collection["startup_gate_required_count"] = int(gate.required)
    progress_payload = refresh_progress if isinstance(refresh_progress, dict) else {}
    collection["pending_commit_count"] = max(
        _nonnegative_count(
            report_payload.get("source_stream_admission_commit_pending_count")
        ),
        _nonnegative_count(
            progress_payload.get("source_stream_admission_commit_pending_count")
        ),
    )
    if isinstance(report_payload.get("pool_last_mutation"), dict):
        collection["pool_last_mutation"] = dict(
            report_payload["pool_last_mutation"]
        )
    if isinstance(report_payload.get("unauthorized_pool_mutation"), dict):
        collection["unauthorized_pool_mutation"] = dict(
            report_payload["unauthorized_pool_mutation"]
        )
    # The Manager snapshots are the sole effective-health truth.  Do not
    # subtract the retired Tier3 circuit/report overlay again: replacement
    # traffic and a leaf's own recheck have deliberately different semantics.
    live_truth = runtime_truth if isinstance(runtime_truth, dict) else {}
    core_inventory_verified = bool(
        live_truth.get("core_inventory_verified", False)
    )
    snapshot_payload = {
        "enabled": bool(enabled),
        "total": snapshot.total,
        # Top-level Tier3 is the complete user-visible 3A+3B inventory.  The
        # dedicated automatic-pool indicator consumes the nested auto_pool
        # snapshot below, so these compatibility aliases must not collapse
        # the layer selector back to Tier3B-only semantics.
        "core_inventory_verified": core_inventory_verified,
        "raw_total": len(active_nodes),
        "alive": display_counts["alive"],
        "slow": display_counts["slow"],
        "healthy": snapshot.healthy,
        "pool_revision": auto_snapshot.revision,
        "dead": display_counts["dead"],
        "suspicious": display_counts["suspicious"],
        "level": level if enabled else 0,
        "level_reason": level_reason,
        "controller": controller,
        "auto_pool": {
            "revision": auto_snapshot.revision,
            "total": auto_snapshot.total,
            "alive": auto_pool_counts["alive"],
            "slow": auto_pool_counts["slow"],
            "healthy": auto_snapshot.healthy,
            "dead": auto_pool_counts["dead"],
            "suspicious": auto_pool_counts["suspicious"],
        },
        "user_pool": {
            "revision": user_snapshot.revision,
            "total": user_snapshot.total,
            "alive": user_pool_counts["alive"],
            "slow": user_pool_counts["slow"],
            "healthy": user_snapshot.healthy,
            "dead": user_pool_counts["dead"],
            "suspicious": user_pool_counts["suspicious"],
        },
        "collection": collection,
        "revival_count": revival_count,
    }
    if core_inventory_verified:
        current_total = _nonnegative_count(
            live_truth.get("core_inventory_total")
        )
        current_auto_total = _nonnegative_count(
            live_truth.get("core_inventory_auto_total")
        )
        current_user_total = _nonnegative_count(
            live_truth.get("core_inventory_user_total")
        )
        snapshot_payload.update(
            {
                "current_total": current_total,
                "current_healthy": min(
                    current_total,
                    _nonnegative_count(
                        live_truth.get("core_inventory_healthy")
                    ),
                ),
            }
        )
        snapshot_payload["auto_pool"].update(
            {
                "current_total": current_auto_total,
                "current_healthy": min(
                    current_auto_total,
                    _nonnegative_count(
                        live_truth.get("core_inventory_auto_healthy")
                    ),
                ),
            }
        )
        snapshot_payload["user_pool"].update(
            {
                "current_total": current_user_total,
                "current_healthy": min(
                    current_user_total,
                    _nonnegative_count(
                        live_truth.get("core_inventory_user_healthy")
                    ),
                ),
            }
        )
    return snapshot_payload
