from __future__ import annotations

from typing import Any


_POOL_REPORT_KEYS = (
    "pool_revision",
    "pool_mutation_ledger",
    "source_stream_committed_alive_count",
    "source_stream_committed_total_count",
    "source_stream_admission_commit_events",
    "source_stream_admission_commit_committed_count",
    "source_stream_admission_commit_explicit_skip_count",
    "source_stream_commit_timeline",
    "time_to_pool_revision_1_ms",
    "time_to_pool_revision_3_ms",
    "time_to_pool_revision_target_ms",
)


def tier3_collection_report_ref(
    collection_report: dict[str, Any] | None,
) -> dict[str, Any]:
    report = dict(collection_report or {})
    return {
        "kind": "tier3_source_state_ref",
        "runtime_generation": max(
            0,
            int(report.get("runtime_generation", 0) or 0),
        ),
        "report_revision": max(
            0,
            int(report.get("report_revision", 0) or 0),
        ),
        "pool_revision": max(
            0,
            int(report.get("pool_revision", 0) or 0),
        ),
    }


def tier3_pool_report_state(
    collection_report: dict[str, Any] | None,
) -> dict[str, Any]:
    report = dict(collection_report or {})
    return {
        key: report[key]
        for key in _POOL_REPORT_KEYS
        if key in report
    }


def tier3_snapshot_pool_state(metadata: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(metadata or {})
    pool_state = payload.get("pool_state")
    if isinstance(pool_state, dict):
        return dict(pool_state)
    legacy_collection = payload.get("collection")
    if isinstance(legacy_collection, dict):
        return dict(legacy_collection)
    return {}


def tier3_snapshot_metadata(
    *,
    source_count: int,
    last_collection_at: str,
    enabled: bool,
    level: int,
    level_reason: str,
    collection_report: dict[str, Any],
    controller: dict[str, Any],
    revival_count: int,
) -> dict[str, Any]:
    return {
        "tier": "tier3",
        "source_count": source_count,
        "last_collection_at": last_collection_at,
        "enabled": enabled,
        "level": level,
        "level_reason": level_reason,
        "collection_ref": tier3_collection_report_ref(collection_report),
        "pool_state": tier3_pool_report_state(collection_report),
        "controller": dict(controller),
        "revival_count": revival_count,
    }
