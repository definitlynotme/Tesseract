from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

import tier3_validation_domain as validation
from models import Node
from tier3_candidate_scheduler import Tier3CandidateDispatchTicket
from tier3_refresh_controller import tier3_validation_fresh_pipeline_headroom
from tier3_validation_feedback import Tier3CandidateDispatchContext
from tier3_validation_protocol import (
    EndpointAttemptKind, EndpointAttemptResult, Tier3DeepRetentionDecision,
    ValidationStage,
)


def _fresh_depth(snapshot):
    return validation.validation_fresh_supply_depth(snapshot)


@pytest.fixture
def domain():
    result = validation.Tier3ValidationDomain(
        refresh_epoch="fresh-owner",
        binary_path="fixture-not-started",
        probe_urls=["http://fixture/probe"],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        commit_node=lambda *_args: None,
        terminal_observer=lambda *_args: None,
        min_workers=1,
        max_workers=1,
    )
    result.snapshot()
    result.actor.start()
    deadline = time.monotonic() + 2.0
    while result.actor.snapshot().inbox_depth and time.monotonic() < deadline:
        time.sleep(0.001)
    assert result.actor.snapshot().inbox_depth == 0
    yield result
    assert result.actor.stop(timeout_seconds=2.0)
    result.release_dispatch_contexts()


def _submit(domain):
    node = Node(
        name="candidate", protocol="http", address="192.0.2.1",
        port=8080, source="opaque-root", tier="tier3",
    )
    context = Tier3CandidateDispatchContext(
        ticket=Tier3CandidateDispatchTicket(
            scheduler_generation=1, ticket_id=1, arm_key="arm",
            source_root="opaque-root", family="http", coarse_key="coarse",
            selection_reason="coverage", dispatched_monotonic_ns=time.monotonic_ns(),
        ),
        node_identity="private-test-identity",
    )
    assert domain.submit(node, {}, dispatch_context=context)
    return domain.candidate_digest_for_node(node)


def test_actor_transfer_cannot_create_false_fresh_headroom(domain, monkeypatch):
    _submit(domain)
    envelope = domain._ingress.pop()
    entered = threading.Event()
    release = threading.Event()
    original_dispatch = domain.actor._dispatch

    def pause_before_snapshot():
        if domain.actor._states:
            entered.set()
            assert release.wait(2.0)
        original_dispatch()

    monkeypatch.setattr(domain.actor, "_dispatch", pause_before_snapshot)
    assert domain.actor.submit(envelope)
    domain.actor.start()
    try:
        assert entered.wait(2.0)
        snapshot = domain.snapshot()
        assert snapshot.ingress_depth == snapshot.actor.inbox_depth == 0
        assert snapshot.actor.ready_fast == snapshot.actor.active_fast == 0
        assert _fresh_depth(snapshot) == 1
        assert tier3_validation_fresh_pipeline_headroom(
            actor_fresh_depth=_fresh_depth(snapshot), tcp_pending_batches=0,
            tcp_active_batches=0, wave_capacity=4, high_water=1,
        ) == 0
    finally:
        release.set()


def test_ingress_transfer_is_counted_without_manager_context():
    snapshot = SimpleNamespace(
        ingress_depth=0, ingress_transfer_count=3,
        actor=SimpleNamespace(inbox_depth=0, ready_fast=0, parked_fast=0, active_fast=0),
    )
    assert _fresh_depth(snapshot) == 3


def test_control_mailbox_does_not_reserve_fresh_candidate_capacity():
    snapshot = SimpleNamespace(
        ingress_depth=0, ingress_transfer_count=0,
        fresh_transaction_pending_count=15,
        actor=SimpleNamespace(
            inbox_depth=9, candidate_inbox_depth=0,
            ready_fast=1, parked_fast=0, active_fast=14,
        ),
    )
    assert _fresh_depth(snapshot) == 15


def test_only_candidate_mailbox_events_contribute_to_fresh_supply():
    snapshot = SimpleNamespace(
        ingress_depth=2, ingress_transfer_count=1,
        actor=SimpleNamespace(
            inbox_depth=12, candidate_inbox_depth=3,
            ready_fast=1, parked_fast=0, active_fast=4,
        ),
    )
    assert _fresh_depth(snapshot) == 11


def test_fast_observation_releases_fresh_not_deep_transaction(domain):
    digest = _submit(domain)
    domain._ingress.pop()
    assert _fresh_depth(domain.snapshot()) == 1
    domain.dispatch_context_for_attempt(digest)
    domain.dispatch_context_for_continuation(digest)
    assert domain.dispatch_context_count() == 1
    assert _fresh_depth(domain.snapshot()) == 0
    domain.dispatch_context_for_terminal(digest, passed=False)
    assert domain.dispatch_context_count() == 0
    assert _fresh_depth(domain.snapshot()) == 0


@pytest.mark.parametrize("passed", [False, True])
def test_terminal_or_cancel_does_not_leave_fresh_work(domain, passed):
    digest = _submit(domain)
    domain._ingress.pop()
    domain.dispatch_context_for_terminal(digest, passed=passed)
    assert _fresh_depth(domain.snapshot()) == 0
    if passed:
        assert domain.dispatch_context_count() == 1
        domain.dispatch_context_for_admission(digest)
    assert domain.dispatch_context_count() == 0


@pytest.mark.parametrize("outcome", [
    EndpointAttemptKind.TRANSIENT, EndpointAttemptKind.RIGHT_CENSORED,
    EndpointAttemptKind.CANDIDATE_FAIL,
])
def test_continuation_keeps_actor_accepted_observation_class(domain, outcome):
    digest = _submit(domain)
    feedback = []
    domain._feedback_observer = feedback.append
    result = EndpointAttemptResult(
        refresh_epoch=domain.refresh_epoch, transaction_id="transaction",
        candidate_digest=digest, config_digest=digest, lease_id="lease",
        slice_id=0, sequence=0, stage=ValidationStage.FAST_PROBE,
        worker_id=1, worker_generation=1, node_json="{}", kind=outcome,
        portfolio_elapsed_ms=1593,
    )
    domain._accepted_fast_observation(result)
    domain._continuation_decision(digest, Tier3DeepRetentionDecision.RETAINED, result)
    assert len(feedback) == 2
    assert feedback[1].right_censored == feedback[0].right_censored
    assert feedback[1].outcome == feedback[0].outcome
