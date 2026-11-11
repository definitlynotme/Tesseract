"""Independent per-stage liveness checks from recorded audit events."""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any


STAGES = {
    "tcp": ("tcp_start", (
        "candidate_reserved_handoff_count", "candidate_tcp_handoff_lease_count")),
    "validation": ("validation_completion", (
        "validation_ingress_depth", "fast_ready", "fast_active",
        "deep_ready", "deep_parked", "deep_active")),
}


def auto_admission_completion(result) -> bool:
    from tier3_validation_protocol import EndpointAttemptKind, Tier3ValidationPurpose

    return bool(
        result.purpose is Tier3ValidationPurpose.AUTO_ADMISSION
        and result.kind not in {EndpointAttemptKind.FACILITY_FAIL, EndpointAttemptKind.CANCELLED}
        and result.attempt_facts
    )


def maximum_gap(sampled, recorded, end):
    """Measure one projected stage; caller validates raw receipt types."""
    cursor, latest, active_since, maximum = 0, None, None, 0.0
    events = sorted(recorded)
    for index, item in enumerate(sampled):
        at = item["at"]
        until = min(end, sampled[index + 1]["at"] if index + 1 < len(sampled) else end)
        if at >= until:
            continue
        while cursor < len(events) and events[cursor][0] <= at:
            latest = events[cursor][0]
            cursor += 1
        actionable = (item["healthy"] < item["target"] and not item["guardrail"]
                      and any(value > 0 for key, value in item.items()
                              if key not in {"at", "healthy", "target", "guardrail"}))
        if not actionable:
            active_since = None
            continue
        if active_since is None:
            active_since = at
        # Missing control revisions retain the last authoritative work, so a
        # blocked controller cannot erase its own liveness gap.
        while cursor < len(events) and events[cursor][0] < until:
            event_at = events[cursor][0]
            maximum = max(maximum, event_at - max(active_since, latest or active_since))
            latest = event_at
            cursor += 1
        maximum = max(maximum, until - max(active_since, latest or active_since))
    return maximum


def stage_gaps(sampled, recorded, end):
    """Other stages and later maintenance cannot lend progress evidence."""
    target_at = next((item["at"] for item in sampled
                      if item["healthy"] >= item["target"]), end)
    end = min(end, target_at)
    result = {}
    for stage, (kind, fields) in STAGES.items():
        projection = [{key: item[key] for key in (
            "at", "healthy", "target", "guardrail", *fields)} for item in sampled]
        result[stage] = maximum_gap(
            projection, [item for item in recorded if item[1] == kind], end)
    return {"measured_until_seconds": end, "maximum_no_progress_seconds": result}


def _nonnegative_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def _counter(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def activity_failures(payload: Mapping[str, Any]) -> list[str]:
    """Recompute evidence instead of trusting a persisted zero-gap summary."""
    if payload.get('recording_errors', []):
        return ['stage_activity_recording_incomplete']
    stop_at = payload.get("stop_at_seconds")
    work = payload.get("work")
    events = payload.get("events")
    if (payload.get("liveness_schema") != 2
            or payload.get("stop_boundary_observed") is not True
            or not _nonnegative_number(stop_at) or stop_at <= 0
            or not isinstance(work, list) or not work
            or not isinstance(events, list) or not events
            or not _counter(payload.get("work_snapshot_count"))
            or payload["work_snapshot_count"] != len(work)):
        return ["stage_activity_evidence_missing"]
    fields = {field for _, names in STAGES.values() for field in names}
    fields.update({"candidate_fresh_backlog", "commit_pending"})
    previous = -1.0
    for item in work:
        if (not isinstance(item, Mapping) or not _nonnegative_number(item.get("at"))
                or item["at"] < previous or not isinstance(item.get("guardrail"), bool)
                or any(not _counter(item.get(name)) for name in fields | {"healthy", "target"})
                or item["target"] <= 0):
            return ["stage_activity_work_invalid"]
        previous = item["at"]
    counts = {kind: 0 for kind, _ in STAGES.values()}
    for event in events:
        if (not isinstance(event, Sequence) or isinstance(event, (str, bytes))
                or len(event) != 2 or not _nonnegative_number(event[0])
                or not isinstance(event[1], str) or event[1] not in counts):
            return ["stage_activity_event_invalid"]
        counts[event[1]] += 1
    if (any(count <= 0 for count in counts.values())
            or not _counter(payload.get("tcp_start_count"))
            or not _counter(payload.get("completion_count"))
            or payload["tcp_start_count"] != counts["tcp_start"]
            or payload["completion_count"] != counts["validation_completion"]):
        return ["stage_activity_receipt_count_invalid"]
    stages = stage_gaps(work, events, stop_at)
    failures = [f"{stage}_activity_unproven_or_stalled"
                for stage, gap in stages["maximum_no_progress_seconds"].items() if gap > 8.0]
    # A dead feeder can leave both execution stages empty despite queued input.
    # Validate and project only owner fields, never arbitrary diagnostic values.
    pipeline = [{key: item[key] for key in fields | {"at", "healthy", "target", "guardrail"}}
                for item in work]
    if not failures and maximum_gap(pipeline, events, stages["measured_until_seconds"]) > 8.0:
        failures.append("pipeline_activity_unproven_or_stalled")
    return failures
