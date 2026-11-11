from __future__ import annotations

import json
import re
from pathlib import Path


FIXTURE = Path(__file__).parent / "fixtures" / "tier3_success_baseline_v1.json"
REGRESSION_FIXTURE = (
    Path(__file__).parent / "fixtures" / "tier3_performance_regression_v2.json"
)
TAIL_REGRESSION_FIXTURE = (
    Path(__file__).parent / "fixtures" / "tier3_tail_regression_v3.json"
)
ASYNC_REWARD_REGRESSION_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "tier3_tail_feedback_regression_v4.json"
)
GLOBAL_LOOKAHEAD_REGRESSION_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "tier3_global_lookahead_regression_v5.json"
)
HEX16 = re.compile(r"^[0-9a-f]{16}$")


def test_success_baseline_is_sanitized_and_preserves_stage_mix() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    commits = payload["commits"]

    assert payload["schema_version"] == 1
    assert payload["pool_revision_milestones_ms"] == {
        "1": 9218,
        "3": 17687,
        "25": 51234,
    }
    assert len(commits) == 25
    assert sum(item["stage"] == "fast_probe" for item in commits) == 24
    assert sum(item["stage"] == "canonical_deep" for item in commits) == 1
    assert all(item["endpoint_result"] == "pass" for item in commits)
    assert all(HEX16.fullmatch(item["candidate_hash"]) for item in commits)
    assert all(HEX16.fullmatch(item["endpoint_hash"]) for item in commits)

    serialized = json.dumps(payload, sort_keys=True)
    assert "://" not in serialized
    assert "uri" not in serialized.lower()
    assert "password" not in serialized.lower()


def test_regression_v2_preserves_only_observed_event_truth() -> None:
    payload = json.loads(REGRESSION_FIXTURE.read_text(encoding="utf-8"))

    assert payload["schema_version"] == 2
    assert payload["performance"] == {
        "time_to_pool_revision_3_ms": 4907,
        "net_new_pool_growth_at_20s": 5,
        "observation_duration_ms": 360000,
        "final_pool_healthy": 21,
        "target_alive": 25,
    }
    assert payload["candidate_pipeline"] == {
        "unique_candidate_count": 7480,
        "legacy_prestart_handoff_count": 3453,
        "legacy_deferred_count": 2244,
        "tcp_responsive_count": 912,
        "net_new_pass_count": 21,
    }
    streams = payload["event_streams"]
    assert streams["transport_start"] == {"availability": "unavailable"}
    assert streams["cohort_decision"] == {"availability": "unavailable"}
    assert streams["actor_slot_timeline"] == {"availability": "unavailable"}

    serialized = json.dumps(payload, sort_keys=True)
    assert "//" not in serialized
    assert "uuid" not in serialized.lower()
    assert "password" not in serialized.lower()
    assert "candidate_hash" not in serialized


def test_tail_regression_v3_preserves_scheduler_and_reward_truth() -> None:
    payload = json.loads(TAIL_REGRESSION_FIXTURE.read_text(encoding="utf-8"))

    assert payload["schema_version"] == 3
    assert payload["performance"]["time_to_pool_revision_3_ms"] == 4422
    assert payload["performance"]["net_new_pool_growth_at_20s"] == 6
    assert payload["candidate_pipeline"] == {
        "unique_candidate_count": 7344,
        "tcp_start_acked_count": 2226,
        "tcp_start_deferred_count": 2,
        "tcp_responsive_count": 720,
        "fast_completion_count": 715,
        "fast_pass_count": 10,
        "deep_completion_count": 102,
        "deep_pass_count": 2,
        "net_new_pass_count": 12,
    }
    assert payload["reward"]["service_handoff_count"] == 94
    assert payload["reward"]["service_fast_pass_count"] == 0
    assert payload["reward"]["observed_sparse_service_completed"] == 2
    assert payload["reward"]["observed_sparse_service_passed"] == 2

    serialized = json.dumps(payload, sort_keys=True)
    assert "://" not in serialized
    assert "uuid" not in serialized.lower()
    assert "password" not in serialized.lower()


def test_async_reward_regression_v4_preserves_only_observed_aggregate_truth() -> None:
    payload = json.loads(
        ASYNC_REWARD_REGRESSION_FIXTURE.read_text(encoding="utf-8")
    )

    assert payload["schema_version"] == 4
    assert payload["performance"]["time_to_pool_revision_3_ms"] == 4718
    assert payload["performance"]["net_new_pool_growth_at_20s"] == 7
    assert payload["candidate_pipeline"] == {
        "unique_candidate_count": 7498,
        "tcp_start_acked_count": 1761,
        "tcp_responsive_count": 549,
        "fast_completion_count": 545,
        "fast_pass_count": 16,
        "deep_completion_count": 63,
        "deep_pass_count": 0,
        "net_new_pass_count": 16,
    }
    assert payload["exact_reward"]["selected_count"] == 1
    assert payload["exact_reward"]["expired_unused_credit_count"] == 255
    assert payload["service_reward"]["bad_leaf_selected_before_feedback"] == 75
    assert payload["service_reward"]["bad_leaf_resolved_count"] == 7
    assert payload["event_truth"]["individual_event_trace_available"] is False
    assert (
        payload["event_truth"]["aggregate_values_must_not_be_synthesized_into_events"]
        is True
    )

    serialized = json.dumps(payload, sort_keys=True)
    assert "://" not in serialized
    assert "uuid" not in serialized.lower()
    assert "password" not in serialized.lower()


def test_global_lookahead_regression_v5_preserves_only_observed_truth() -> None:
    payload = json.loads(
        GLOBAL_LOOKAHEAD_REGRESSION_FIXTURE.read_text(encoding="utf-8")
    )

    assert payload["schema_version"] == 5
    assert payload["performance"] == {
        "final_pool_healthy": 19,
        "target_alive": 25,
        "observed_stall_ms": 200000,
        "timing_precision": "approximate_from_authoritative_report",
    }
    assert payload["candidate_pipeline"] == {
        "unique_candidate_count": 7794,
        "tcp_start_acked_count": 4815,
        "tcp_responsive_count": 1493,
        "fast_completion_count": 1476,
        "fast_pass_count": 20,
        "deep_completion_count": 142,
        "deep_pass_count": 1,
        "validated_pass_count": 21,
        "net_new_pass_count": 19,
    }
    assert payload["production_selection"] == {
        "service_selected_count": 8,
        "service_fast_pass_count": 0,
        "structural_routing_was_enabled": True,
        "whole_inventory_visibility_was_available": False,
    }
    assert payload["event_truth"]["individual_event_trace_available"] is False
    assert (
        payload["event_truth"]["aggregate_values_must_not_be_synthesized_into_events"]
        is True
    )

    serialized = json.dumps(payload, sort_keys=True)
    assert "://" not in serialized
    assert "uuid" not in serialized.lower()
    assert "password" not in serialized.lower()
