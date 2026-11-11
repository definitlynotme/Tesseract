from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.tier3_audit_activity import AuditActivityRecorder
from scripts.tier3_audit_liveness import activity_failures
from tier3_validation_protocol import EndpointAttemptKind, Tier3ValidationPurpose


def work(**values):
    return SimpleNamespace(**{
        **{name: 0 for name in AuditActivityRecorder.FIELDS},
        'healthy_count': 0, 'hard_resource_guardrail': False, **values,
    })


def completion(**values):
    return SimpleNamespace(**dict(
        purpose=Tier3ValidationPurpose.AUTO_ADMISSION, kind=EndpointAttemptKind.PASS,
        attempt_facts=(object(),), lease_id='one', sequence=1, **values,
    ))


def test_scheduler_observation_keeps_return_identity_and_counts_only_commits(tmp_path, monkeypatch):
    committed = (SimpleNamespace(scheduler_generation=1, ticket_id=2),)
    class Scheduler:
        def commit_handoffs(self, reservations):
            assert reservations is source
            return committed
    source = object()
    recorder = AuditActivityRecorder(tmp_path)
    recorder.install_scheduler(SimpleNamespace(Tier3CandidateScheduler=Scheduler), monkeypatch.setattr)
    owner = Scheduler()
    assert owner.commit_handoffs(source) is committed
    assert owner.commit_handoffs(source) is committed
    assert recorder.snapshot()['tcp_start_count'] == 1
    committed = ()
    assert owner.commit_handoffs(source) == ()
    assert recorder.snapshot()['tcp_start_count'] == 1


def test_controller_observation_cannot_replace_input_or_decision(tmp_path, monkeypatch):
    snapshot, decision = work(candidate_fresh_backlog=12), object()
    class Controller:
        def expansion_envelope(self, **kwargs):
            assert kwargs == {'work_snapshot': snapshot, 'target_alive': 25}
            return decision
    recorder = AuditActivityRecorder(tmp_path)
    recorder.install_controller(SimpleNamespace(Tier3PIController=Controller), monkeypatch.setattr)
    assert Controller().expansion_envelope(work_snapshot=snapshot, target_alive=25) is decision
    sampled, = recorder.snapshot()['work']
    assert sampled['candidate_fresh_backlog'] == 12 and sampled['target'] == 25
    sampled['candidate_fresh_backlog'] = 0
    assert recorder.snapshot()['work'][0]['candidate_fresh_backlog'] == 12


def test_supervisor_sink_receives_original_result_once_and_returns_its_value(tmp_path, monkeypatch):
    class Supervisor:
        def __init__(self, *, attempt_sink):
            self.sink = attempt_sink
    recorder = AuditActivityRecorder(tmp_path)
    recorder.install_supervisor(SimpleNamespace(Tier3ValidatorSupervisor=Supervisor), monkeypatch.setattr)
    result, returned = completion(), object()
    sink = Mock(return_value=returned)
    owner = Supervisor(attempt_sink=sink)
    assert owner.sink(result) is returned
    sink.assert_called_once_with(result)
    assert recorder.snapshot()['completion_count'] == 1
    sink.side_effect = ValueError('original sink failed')
    with pytest.raises(ValueError, match='original sink failed'):
        owner.sink(result)
    assert recorder.snapshot()['completion_count'] == 1


@pytest.mark.parametrize('kind', [EndpointAttemptKind.FACILITY_FAIL, EndpointAttemptKind.CANCELLED])
def test_facility_or_cancel_cannot_supply_progress(tmp_path, kind):
    recorder = AuditActivityRecorder(tmp_path)
    result = completion()
    result.kind = kind
    recorder.record_completion(object(), result)
    assert recorder.snapshot()['completion_count'] == 0


def test_stop_boundary_precedes_drain_and_cannot_be_reset(tmp_path, monkeypatch):
    now = [0.0]
    recorder = AuditActivityRecorder(tmp_path, clock=lambda: now[0])
    class Manager:
        def request_stop(self):
            assert recorder.snapshot()['stop_at_seconds'] == 3
            now[0] = 20
            return False
    recorder.install_manager(SimpleNamespace(ProxyTierManager=Manager), monkeypatch.setattr)
    now[0] = 3
    assert Manager().request_stop() is False
    recorder.record_stop()
    assert recorder.snapshot()['stop_at_seconds'] == 3


def test_helper_empty_snapshot_cannot_overwrite_application_activity(tmp_path):
    destination = tmp_path / 'activity-control.json'
    destination.write_text('original')
    AuditActivityRecorder(tmp_path).save()
    assert destination.read_text() == 'original'


def test_bounded_observation_fails_closed_when_any_evidence_is_dropped(tmp_path):
    now = [0.0]
    recorder = AuditActivityRecorder(tmp_path, clock=lambda: now[0], limit=1)
    recorder.record_work(work(fast_active=1), 25)
    recorder.record_work(work(fast_active=1), 25)
    owner = object()
    recorder.record_starts(owner, [SimpleNamespace(scheduler_generation=1, ticket_id=1)])
    recorder.record_completion(owner, completion())
    now[0] = 1
    recorder.record_stop()
    recorder.save()
    payload = json.loads((tmp_path / 'activity-control.json').read_text())
    assert activity_failures(payload) == ['stage_activity_recording_incomplete']
    assert len(payload['work']) == len(payload['events']) == 1


def test_activity_output_does_not_contain_candidate_or_endpoint_values(tmp_path):
    now = [0.0]
    recorder = AuditActivityRecorder(tmp_path, clock=lambda: now[0])
    recorder.record_work(work(fast_active=1), 25)
    ticket = SimpleNamespace(scheduler_generation=1, ticket_id=2, identity='private://password')
    result = completion()
    result.node_identity = 'private://password'
    owner = object()
    recorder.record_starts(owner, [ticket])
    recorder.record_completion(owner, result)
    now[0] = 1
    recorder.record_stop()
    recorder.save()
    text = (tmp_path / 'activity-control.json').read_text()
    assert 'password' not in text and 'private://' not in text
    assert not activity_failures(json.loads(text))
