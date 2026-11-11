from __future__ import annotations

import time

import pytest

from models import Node
from tier3_candidate_scheduler import Tier3CandidateFeedbackTicket
from tier3_validation_domain import Tier3ValidationDomain
from tier3_validation_feedback import (
    Tier3CandidateDispatchContext,
    Tier3ValidationFeedbackKind,
)
from tier3_validation_protocol import (
    AdmissionCommitted,
    AdmissionIntent,
    AdmissionRejected,
    ValidationStage,
    canonical_json,
)


@pytest.mark.parametrize('stage', list(ValidationStage))
@pytest.mark.parametrize('outcome', ['committed', 'rejected', 'exception'])
def test_admission_feedback_preserves_intent_stage_and_generation(stage, outcome):
    events = []
    commits = []

    def commit(_node, _report, intent):
        commits.append(intent)
        fields = dict(
            refresh_epoch=intent.refresh_epoch,
            candidate_digest=intent.candidate_digest,
            lease_id=intent.lease_id,
            sequence=intent.sequence,
        )
        if outcome == 'exception':
            raise RuntimeError('fixture commit failure')
        if outcome == 'rejected':
            return AdmissionRejected(**fields, reason='fixture_reject')
        return AdmissionCommitted(**fields, pool_revision=1, net_new_pool_growth=True)

    domain = Tier3ValidationDomain(
        refresh_epoch='admission-provenance', binary_path='fixture',
        probe_urls=['http://fixture/204'], expected_statuses={204},
        probe_timeout_seconds=5.0, slow_threshold_ms=3000,
        commit_node=commit, terminal_observer=lambda *_args: None,
        feedback_observer=events.append, min_workers=1, max_workers=1,
    )
    node = Node(name='fixture', protocol='http', address='127.0.0.1',
                port=8080, source='opaque-root', tier='tier3')
    ticket = Tier3CandidateFeedbackTicket(
        scheduler_generation=1, ticket_id=1, arm_key='opaque-arm',
        source_root='opaque-root', family='http', coarse_key='opaque-coarse',
        selection_reason='coverage', dispatched_monotonic_ns=time.monotonic_ns(),
    )
    context = Tier3CandidateDispatchContext(ticket=ticket, node_identity='private')
    assert domain.submit(node, {}, dispatch_context=context)
    envelope = domain._ingress.pop()
    assert envelope is not None
    intent = AdmissionIntent(
        refresh_epoch=domain.refresh_epoch,
        candidate_digest=envelope.candidate_digest,
        config_digest=envelope.config_digest,
        lease_id='lease', sequence=1, worker_id=3, worker_generation=7,
        node_json=canonical_json(node.to_dict()), report_json='{}', stage=stage,
    )
    domain.committer.start()
    try:
        # The first result precedes terminal reporting; a duplicate intent must
        # still carry its original stage without running the mutation twice.
        assert domain.committer.put_nowait(intent)
        assert domain.committer.put_nowait(intent)
        assert domain.committer.wait_empty(2.0)
        assert len(commits) == 1
        admission_events = [event for event in events
                            if event.kind is Tier3ValidationFeedbackKind.ADMISSION]
        assert len(admission_events) == 2
        assert all(event.stage is stage for event in admission_events)
        assert all(event.worker_generation == 7 for event in admission_events)
        assert admission_events[0].ticket is ticket
        assert admission_events[0].net_new_pool_growth == (outcome == 'committed')
        assert not admission_events[1].net_new_pool_growth
        assert admission_events[1].outcome == 'duplicate_admission_intent'
        assert domain.dispatch_context_for_terminal(
            envelope.candidate_digest, passed=True,
        ) is context
        assert domain.dispatch_context_count() == 0
    finally:
        assert domain.committer.stop(2.0)
