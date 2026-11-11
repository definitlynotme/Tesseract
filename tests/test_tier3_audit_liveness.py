import copy

import pytest

from scripts.tier3_audit_liveness import activity_failures, auto_admission_completion, maximum_gap, stage_gaps


def test_user_health_and_facility_do_not_mask_admission_stall():
    from types import SimpleNamespace
    from tier3_validation_protocol import EndpointAttemptKind, Tier3ValidationPurpose

    result = SimpleNamespace(purpose=Tier3ValidationPurpose.AUTO_ADMISSION,
                             kind=EndpointAttemptKind.PASS, attempt_facts=(object(),))
    assert auto_admission_completion(result)
    result.purpose = Tier3ValidationPurpose.USER_POOL_HEALTH
    assert not auto_admission_completion(result)
    result.purpose = Tier3ValidationPurpose.AUTO_ADMISSION
    result.kind = EndpointAttemptKind.FACILITY_FAIL
    assert not auto_admission_completion(result)
    result.kind = EndpointAttemptKind.CANCELLED
    assert not auto_admission_completion(result)
    result.kind = EndpointAttemptKind.PASS
    result.attempt_facts = ()
    assert not auto_admission_completion(result)


def work(at, **kwargs):
    result = dict(at=at, healthy=0, target=25, guardrail=False,
                  candidate_reserved_handoff_count=0, candidate_tcp_handoff_lease_count=0,
                  candidate_fresh_backlog=0, commit_pending=0,
                  validation_ingress_depth=0, fast_ready=0, fast_active=0,
                  deep_ready=0, deep_parked=0, deep_active=0)
    result.update(kwargs)
    return result


def evidence(samples, events, end=10):
    return dict(liveness_schema=2, work=samples, events=events,
                work_snapshot_count=len(samples), stop_at_seconds=end,
                stop_boundary_observed=True,
                tcp_start_count=sum(kind == "tcp_start" for _, kind in events),
                completion_count=sum(kind == "validation_completion" for _, kind in events))


def test_moving_tcp_does_not_hide_stalled_validation():
    samples = [work(0, fast_active=1, candidate_tcp_handoff_lease_count=1)]
    events = [(second, "tcp_start") for second in range(11)] + [(0, "validation_completion")]
    assert maximum_gap(samples, events, 10) == 1
    payload = evidence(samples, events)
    # A stale or tampered summary must never substitute for raw receipts.
    payload["stages"] = {"maximum_no_progress_seconds": {"tcp": 0, "validation": 0}}
    assert activity_failures(payload) == ["validation_activity_unproven_or_stalled"]


def test_validation_cannot_hide_reserved_tcp_stall():
    samples = [work(0, fast_active=1, candidate_reserved_handoff_count=16)]
    events = [(second, "validation_completion") for second in range(11)] + [(0, "tcp_start")]
    assert activity_failures(evidence(samples, events)) == ["tcp_activity_unproven_or_stalled"]


def test_missing_control_revision_keeps_work_actionable():
    samples = [work(0, deep_parked=1), work(10)]
    assert stage_gaps(samples, [], 11)["maximum_no_progress_seconds"]["validation"] == 10


def test_starved_feeder_cannot_hide_behind_empty_stage_queues():
    samples = [work(0, candidate_fresh_backlog=6000)]
    events = [(0, "tcp_start"), (0, "validation_completion")]
    assert activity_failures(evidence(samples, events)) == ["pipeline_activity_unproven_or_stalled"]


def test_backlog_waiting_while_validation_progresses_is_not_deadlock():
    samples = [work(0, candidate_fresh_backlog=6000, fast_active=16)]
    events = [(0, "tcp_start")] + [(i, "validation_completion") for i in range(11)]
    assert activity_failures(evidence(samples, events)) == []


def test_pending_commit_without_progress_is_not_closed_activity():
    samples = [work(0, commit_pending=1)]
    assert activity_failures(evidence(samples, [(0, "tcp_start"), (0, "validation_completion")]))


def test_stop_and_first_target_bound_the_first_expansion():
    samples = [work(0, fast_active=1), work(2, healthy=25), work(4, fast_active=1)]
    assert stage_gaps(samples, [], 20)["maximum_no_progress_seconds"]["validation"] == 2
    assert stage_gaps(samples, [], 1)["maximum_no_progress_seconds"]["validation"] == 1


def test_no_tcp_lease_has_no_tcp_liveness_requirement():
    samples = [work(0, deep_active=1, tcp_pending_depth=70)]
    assert stage_gaps(samples, [], 10)["maximum_no_progress_seconds"]["tcp"] == 0


def test_sub_millisecond_excess_is_not_rounded_into_acceptance():
    samples = [work(0, fast_active=1)]
    payload = evidence(samples, [(0, "tcp_start"), (0, "validation_completion")], 8.0001)
    assert activity_failures(payload) == ["validation_activity_unproven_or_stalled"]


@pytest.mark.parametrize("missing", ["work", "events", "stop_at_seconds", "liveness_schema"])
def test_missing_receipts_are_not_zero_gap(missing):
    payload = evidence([work(0)], [(0, "tcp_start"), (0, "validation_completion")])
    assert activity_failures(payload) == []
    payload.pop(missing)
    assert activity_failures(payload) == ["stage_activity_evidence_missing"]


@pytest.mark.parametrize("value", [False, None, -1, float("inf"), float("nan"), "10"])
def test_invalid_stop_boundary_fails(value):
    payload = evidence([work(0)], [(0, "tcp_start"), (0, "validation_completion")])
    payload["stop_at_seconds"] = value
    assert activity_failures(payload)


@pytest.mark.parametrize("change", [
    {"fast_active": None}, {"fast_active": True}, {"fast_active": -1},
    {"guardrail": "false"}, {"at": float("nan")}, {"target": 0},
])
def test_invalid_work_cannot_hide_activity(change):
    payload = evidence([work(0)], [(0, "tcp_start"), (0, "validation_completion")])
    payload["work"][0].update(change)
    assert activity_failures(payload) == ["stage_activity_work_invalid"]


def test_wrong_counts_reversed_revisions_and_unknown_events_fail():
    valid = evidence([work(0)], [(0, "tcp_start"), (0, "validation_completion")])
    payload = copy.deepcopy(valid)
    payload["completion_count"] = 0
    assert activity_failures(payload) == ["stage_activity_receipt_count_invalid"]
    payload = copy.deepcopy(valid)
    payload["work"] = [work(1), work(0)]
    payload["work_snapshot_count"] = 2
    assert activity_failures(payload) == ["stage_activity_work_invalid"]
    payload = copy.deepcopy(valid)
    payload["events"].append((2, "facility"))
    assert activity_failures(payload) == ["stage_activity_event_invalid"]
