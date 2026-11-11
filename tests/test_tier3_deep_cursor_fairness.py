from __future__ import annotations

import queue

import pytest

from tier3_expansion_actor import Tier3ExpansionActor, _CandidateState
from tier3_validation_protocol import CandidateEnvelope, ValidationStage


def actor_and_states(*, same_root: bool, origin: str):
    actor = Tier3ExpansionActor(
        "deep-fairness", dispatch_sink=lambda _command: True,
        admission_sink=lambda _intent: True, terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    states = []
    for index, root, family in (
        (0, "opaque-a", "opaque-x"),
        (1, "opaque-a" if same_root else "opaque-b", "opaque-y"),
        (2, "opaque-a", "opaque-x"),
    ):
        envelope = CandidateEnvelope(
            refresh_epoch="deep-fairness", candidate_digest=f"{index:064x}",
            config_digest=f"config-{index}", node_json="{}", report_json="{}",
            source_root=root, source_family=family, source_coarse="opaque",
            dispatch_evidence_origin=origin,
            probe_urls=("https://neutral.invalid/204",),
        )
        state = _CandidateState(envelope=envelope, transaction_id=f"tx-{index}")
        actor._states[envelope.candidate_digest] = state
        states.append(state)
    return actor, states


@pytest.mark.parametrize("same_root", [False, True])
@pytest.mark.parametrize("origin", ["cold_unknown", "fresh_replay", "cold_discovery"])
def test_deep_empty_bucket_is_not_a_repeated_newcomer(same_root, origin):
    actor, (returning, waiting, _unused) = actor_and_states(
        same_root=same_root, origin=origin,
    )
    stage = ValidationStage.CANONICAL_DEEP
    assert actor._enqueue_stage(returning, stage)
    assert actor._next_candidate_digest(stage) == returning.envelope.candidate_digest
    assert actor._enqueue_stage(waiting, stage)
    returning.deep_dispatch_count = 1
    assert actor._enqueue_stage(returning, stage, continuation=True)

    assert actor._next_candidate_digest(stage) == waiting.envelope.candidate_digest
    assert actor._next_candidate_digest(stage) == returning.envelope.candidate_digest


@pytest.mark.parametrize("same_root", [False, True])
@pytest.mark.parametrize("origin", ["cold_unknown", "fresh_replay", "cold_discovery"])
def test_deep_continuation_priority_cannot_rewind_global_rotation(same_root, origin):
    actor, (returning, waiting, sibling) = actor_and_states(
        same_root=same_root, origin=origin,
    )
    # Keep the first bucket nonempty when the selected endpoint returns.
    actor._park_deep(returning)
    actor._park_deep(sibling)
    actor._park_deep(waiting)
    assert actor._pop_parked_deep() is returning
    returning.deep_dispatch_count = 1
    actor._park_deep(returning, continuation=True)

    assert actor._pop_parked_deep() is waiting
    # Continuation preference within its own fair bucket is unchanged.
    assert actor._pop_parked_deep() is returning
    assert actor._pop_parked_deep() is sibling


@pytest.mark.parametrize("same_root", [False, True])
@pytest.mark.parametrize("origin", ["fresh_replay", "cold_discovery"])
def test_deep_ready_continuation_cannot_bypass_another_ready_bucket(same_root, origin):
    actor, (returning, waiting, sibling) = actor_and_states(
        same_root=same_root, origin=origin,
    )
    stage = ValidationStage.CANONICAL_DEEP
    assert actor._enqueue_stage(returning, stage)
    assert actor._enqueue_stage(sibling, stage)
    assert actor._next_candidate_digest(stage) == returning.envelope.candidate_digest
    assert actor._enqueue_stage(waiting, stage)
    returning.deep_dispatch_count = 1
    assert actor._enqueue_stage(returning, stage, continuation=True)

    assert actor._next_candidate_digest(stage) == waiting.envelope.candidate_digest
