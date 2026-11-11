from __future__ import annotations

import queue
import time
from collections import OrderedDict

import pytest
from dataclasses import replace

from tier3_endpoint_controller import Tier3EndpointController
from tier3_expansion_actor import (
    Tier3ExpansionActor,
    _CandidateState,
    _StageRollingSample,
)
from tier3_validation_protocol import (
    AdmissionCommitted,
    CandidateEnvelope,
    EndpointAttemptKind,
    ProbeAttemptFact,
    Tier3DeepRetentionDecision,
    Tier3TerminalKind,
    Tier3ValidatorCapacityLease,
    EndpointAttemptResult,
    ValidationStage,
    ValidationTerminal,
)


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


def _candidate(
    digest: str,
    root: str,
    family: str,
    coarse: str = "default",
    probe_urls: tuple[str, ...] = (),
    dispatch_selection_reason: str = "",
    dispatch_evidence_origin: str = "cold_unknown",
) -> CandidateEnvelope:
    return CandidateEnvelope(
        refresh_epoch="epoch-1",
        candidate_digest=digest,
        config_digest=f"config-{digest}",
        node_json="{}",
        report_json="{}",
        source_root=root,
        source_family=family,
        source_coarse=coarse,
        dispatch_selection_reason=dispatch_selection_reason,
        dispatch_evidence_origin=dispatch_evidence_origin,
        probe_urls=probe_urls,
    )


def _endpoint_result(command, kind: EndpointAttemptKind) -> EndpointAttemptResult:
    completed = kind is not EndpointAttemptKind.RIGHT_CENSORED
    return EndpointAttemptResult(
        refresh_epoch=command.refresh_epoch,
        transaction_id=command.transaction_id,
        candidate_digest=command.candidate_digest,
        config_digest=command.config_digest,
        lease_id=command.lease_id,
        slice_id=command.slice_id,
        sequence=command.sequence,
        stage=command.stage,
        worker_id=1,
        worker_generation=1,
        node_json=command.node_json,
        kind=kind,
        reason=f"fixture_{kind.value}",
        attempt_facts=(
            ProbeAttemptFact(
                endpoint=command.endpoint,
                elapsed_ms=int(command.probe_budget_seconds * 1000),
                full_budget=completed,
                completed=completed,
                outcome=kind.value,
                attempt_id=f"{command.lease_id}:attempt",
                stage=command.stage.value,
                granted_budget_ms=int(command.probe_budget_seconds * 1000),
                lifecycle="completed" if completed else "censored",
            ),
        ),
        probe_policy_revision=command.probe_policy_revision,
        portfolio_digest=command.portfolio_digest,
        clean_confirmed=True,
    )


def test_fast_budget_is_identical_for_replay_and_cold_candidates() -> None:

    import tier3_expansion_actor as actor_mod

    actor = actor_mod.Tier3ExpansionActor.__new__(actor_mod.Tier3ExpansionActor)
    actor._fast_soft_entitlement_seconds = actor_mod._FAST_SOFT_ENTITLEMENT_DEFAULT_SECONDS

    def envelope(origin):
        return CandidateEnvelope(
            refresh_epoch="e", candidate_digest="d", config_digest="c",
            node_json="{}", report_json="{}", source_root="r", source_family="f",
            dispatch_evidence_origin=origin,
        )

    for origin in ("cold_unknown", "cold_discovery", ""):
        assert actor._fast_probe_budget_seconds(envelope(origin)) == pytest.approx(
            actor_mod._FAST_SOFT_ENTITLEMENT_DEFAULT_SECONDS
        ), origin
    for origin in ("fresh_replay", "historical_reprobe"):
        assert actor._fast_probe_budget_seconds(envelope(origin)) == pytest.approx(
            actor_mod._FAST_SOFT_ENTITLEMENT_DEFAULT_SECONDS
        ), origin


def test_fast_entitlement_default_is_restored_but_never_inflated() -> None:
    import tier3_expansion_actor as actor_mod

    actor = actor_mod.Tier3ExpansionActor.__new__(actor_mod.Tier3ExpansionActor)
    actor._fast_soft_completion_counter = 0
    actor._fast_soft_last_adjust_completion = 0
    actor._fast_soft_reset_count = 0

    actor._fast_soft_entitlement_seconds = 1.5
    actor._refresh_fast_soft_entitlement()
    assert actor._fast_soft_entitlement_seconds == pytest.approx(
        actor_mod._FAST_SOFT_ENTITLEMENT_DEFAULT_SECONDS
    )
    actor._refresh_fast_soft_entitlement()
    assert actor._fast_soft_entitlement_seconds == pytest.approx(
        actor_mod._FAST_SOFT_ENTITLEMENT_DEFAULT_SECONDS
    )



def test_actor_reports_candidate_mailbox_separately_from_control_events() -> None:
    actor = Tier3ExpansionActor(
        "epoch-1", dispatch_sink=lambda _command: False,
        admission_sink=lambda _intent: True, terminal_sink=queue.Queue(),
    )
    for _ in range(9):
        actor.set_worker_envelope(16)
    assert actor.snapshot().inbox_depth == 9
    assert actor.snapshot().candidate_inbox_depth == 0
    for index in range(3):
        assert actor.submit(_candidate(str(index) * 64, "root", "family"))
    assert actor.snapshot().inbox_depth == 12
    assert actor.snapshot().candidate_inbox_depth == 3
    actor.start()
    try:
        _wait_until(lambda: actor.snapshot().inbox_depth == 0)
        assert actor.snapshot().candidate_inbox_depth == 0
        assert actor.snapshot().queued_fast == 3
    finally:
        assert actor.stop(timeout_seconds=2.0)


def test_failed_mailbox_enqueue_restores_both_counters(monkeypatch) -> None:
    actor = Tier3ExpansionActor(
        "epoch-1", dispatch_sink=lambda _command: False,
        admission_sink=lambda _intent: True, terminal_sink=queue.Queue(),
    )

    def fail(_event):
        raise queue.Full

    monkeypatch.setattr(actor._events, "put_nowait", fail)
    for kind in ("candidate", "capacity"):
        try:
            actor._enqueue_event(kind, None)
        except queue.Full:
            pass
        else:
            raise AssertionError("queue failure was swallowed")
        assert actor.snapshot().inbox_depth == 0
        assert actor.snapshot().candidate_inbox_depth == 0


def test_actor_requires_concrete_idle_helper_lease_before_dispatch() -> None:
    dispatched = []
    leases = [
        Tier3ValidatorCapacityLease(1, 1, 1),
        Tier3ValidatorCapacityLease(2, 1, 2),
    ]

    def reserve() -> Tier3ValidatorCapacityLease | None:
        return leases.pop(0) if leases else None

    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=lambda command: dispatched.append(command) or True,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        capacity_reservation_sink=reserve,
        capacity_release_sink=lambda _lease: None,
    )
    actor.start()
    try:
        for value in ("a", "b", "c"):
            assert actor.submit(_candidate(value * 64, "root-a", "vless"))
        actor.capacity_available(8)
        _wait_until(lambda: len(dispatched) == 2)
        time.sleep(0.1)

        snapshot = actor.snapshot()
        assert len(dispatched) == 2
        assert snapshot.active == 2
        assert snapshot.available_capacity == 0
        assert snapshot.capacity_reservation_rejected_count == 1
        assert {
            (command.worker_id, command.worker_generation)
            for command in dispatched
        } == {(1, 1), (2, 1)}
        assert {command.capacity_reservation_id for command in dispatched} == {
            1,
            2,
        }
    finally:
        actor.stop(timeout_seconds=2.0)


def test_actor_returning_bucket_cannot_repeat_newcomer_priority() -> None:
    for same_root in (False, True):
        actor = Tier3ExpansionActor(
            "epoch-1",
            dispatch_sink=lambda _command: True,
            admission_sink=lambda _intent: True,
            terminal_sink=queue.Queue(),
            initial_worker_envelope=16,
        )
        waiting = _candidate("a" * 64, "root-waiting", "family-waiting")
        actor._add_candidate(waiting)
        transient_root = "root-waiting" if same_root else "root-returning"
        first = _candidate("b" * 64, transient_root, "family-returning")
        actor._add_candidate(first)
        assert actor._next_candidate_digest(ValidationStage.FAST_PROBE) == first.candidate_digest
        actor._release_owner_credit(first.candidate_digest)
        actor._states[first.candidate_digest].state = "terminal"

        second = _candidate("c" * 64, transient_root, "family-returning")
        actor._add_candidate(second)
        assert actor._next_candidate_digest(ValidationStage.FAST_PROBE) == waiting.candidate_digest
        assert actor._next_candidate_digest(ValidationStage.FAST_PROBE) == second.candidate_digest


def test_actor_publishes_only_accepted_fast_observation_before_continuation() -> None:
    dispatched = []
    feedback_order: list[str] = []
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=lambda command: dispatched.append(command) or True,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        fast_observation_sink=lambda _result: feedback_order.append("observation"),
        continuation_sink=(
            lambda _digest, _decision, _result: feedback_order.append(
                "continuation"
            )
        ),
    )
    actor.start()
    try:
        assert actor.submit(
            _candidate(
                "f" * 64,
                "root-fast-observation",
                "vless",
                probe_urls=("https://probe.invalid/204",),
            )
        )
        actor.capacity_available(1)
        _wait_until(lambda: len(dispatched) == 1)
        result = _endpoint_result(
            dispatched[0],
            EndpointAttemptKind.CANDIDATE_FAIL,
        )

        actor.endpoint_result(replace(result, lease_id="stale-lease"))
        time.sleep(0.05)
        assert feedback_order == []

        actor.endpoint_result(result)
        _wait_until(lambda: len(feedback_order) == 2)
        assert feedback_order == ["observation", "continuation"]
        snapshot = actor.snapshot()
        assert snapshot.fast_observation_count == 1
        assert snapshot.fast_observation_publish_failure_count == 0
    finally:
        actor.stop(timeout_seconds=2.0)


def test_escape_deep_continuation_finishes_before_same_bucket_ordinary_debt() -> None:
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
    )
    envelopes = (
        _candidate("a" * 64, "root-a", "vless"),
        _candidate("b" * 64, "root-a", "vless"),
        _candidate(
            "e" * 64,
            "root-a",
            "vless",
            dispatch_selection_reason="credit_escape",
        ),
    )
    for envelope in envelopes:
        state = _CandidateState(
            envelope=envelope,
            transaction_id=f"epoch-1:{envelope.candidate_digest[:24]}",
        )
        actor._states[envelope.candidate_digest] = state
        actor._park_deep(state, continuation=True)

    selected = actor._pop_parked_deep()
    assert selected is not None
    assert selected.envelope.candidate_digest == "e" * 64
    # Priority is local to this fair root/family bucket. A continuation is
    # remaining work, so the most recently released bounded endpoint slice
    # finishes its ledger before the bucket admits another fresh candidate.
    next_selected = actor._pop_parked_deep()
    assert next_selected is not None
    assert next_selected.envelope.candidate_digest == "b" * 64


def test_blocked_fast_supply_prioritizes_bounded_escape_debt_across_roots() -> None:
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    ordinary = (
        _candidate("a" * 64, "root-a", "vless"),
        _candidate("b" * 64, "root-b", "vmess"),
    )
    escape = _candidate(
        "e" * 64,
        "root-late",
        "trojan",
        dispatch_selection_reason="credit_escape",
    )
    for envelope in (*ordinary, escape):
        state = _CandidateState(
            envelope=envelope,
            transaction_id=f"epoch-1:{envelope.candidate_digest[:24]}",
        )
        actor._states[envelope.candidate_digest] = state
        actor._park_deep(state, continuation=True)

    actor._fast_supply_state = "blocked"
    selected = actor._pop_parked_deep()
    assert selected is not None
    assert selected.envelope.candidate_digest == "e" * 64
    assert actor._deep_parked_count == 2
    assert actor._queued_deep_count == 2

    # The recovery preference is scoped to blocked supply. Once a real FAST
    # reservation exists again, ordinary root/family rotation remains the
    # canonical ordering contract.
    actor._park_deep(selected, continuation=True)
    actor._fast_supply_state = "ready"
    next_selected = actor._pop_parked_deep()
    assert next_selected is not None
    assert next_selected.envelope.candidate_digest == "a" * 64


def test_blocked_escape_continuation_finishes_before_next_escape_lease() -> None:
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    first = _CandidateState(
        envelope=_candidate(
            "a" * 64,
            "root-a",
            "vless",
            dispatch_selection_reason="credit_escape",
        ),
        transaction_id="epoch-1:first-escape",
    )
    second = _CandidateState(
        envelope=_candidate(
            "b" * 64,
            "root-b",
            "vmess",
            dispatch_selection_reason="credit_escape",
        ),
        transaction_id="epoch-1:second-escape",
    )
    for state in (first, second):
        actor._states[state.envelope.candidate_digest] = state
        actor._park_deep(state)

    actor._fast_supply_state = "blocked"
    selected = actor._pop_parked_deep()
    assert selected is first
    selected.deep_dispatch_count = 1
    actor._park_deep(selected, continuation=True)

    # Completing the current bounded ledger releases one scheduler escape
    # lease sooner than endpoint-level round robin across both transactions.
    assert actor._pop_parked_deep() is first


def test_deep_continuation_finishes_before_fresh_same_family_candidate() -> None:
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
    )
    first = _CandidateState(
        envelope=_candidate("a" * 64, "root-a", "vless"),
        transaction_id="epoch-1:first",
    )
    fresh = _CandidateState(
        envelope=_candidate("b" * 64, "root-a", "vless"),
        transaction_id="epoch-1:fresh",
    )
    for state in (first, fresh):
        actor._states[state.envelope.candidate_digest] = state
        actor._park_deep(state)

    selected = actor._pop_parked_deep()
    assert selected is first
    selected.deep_dispatch_count = 1
    actor._park_deep(selected, continuation=True)

    continued = actor._pop_parked_deep()
    assert continued is first
    next_selected = actor._pop_parked_deep()
    assert next_selected is fresh


def test_deep_continuations_prioritize_replay_then_cold_discovery() -> None:
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
    )
    ordinary = _CandidateState(
        envelope=_candidate(
            "a" * 64,
            "root-a",
            "opaque-family",
        ),
        transaction_id="epoch-1:ordinary",
    )
    discovery = _CandidateState(
        envelope=_candidate(
            "b" * 64,
            "root-a",
            "opaque-family",
            dispatch_evidence_origin="cold_discovery",
        ),
        transaction_id="epoch-1:discovery",
    )
    replay = _CandidateState(
        envelope=_candidate(
            "c" * 64,
            "root-a",
            "opaque-family",
            dispatch_evidence_origin="fresh_replay",
        ),
        transaction_id="epoch-1:replay",
    )
    for state in (ordinary, discovery, replay):
        actor._states[state.envelope.candidate_digest] = state
        actor._park_deep(state, continuation=True)

    assert actor._pop_parked_deep() is replay
    assert actor._pop_parked_deep() is discovery
    assert actor._pop_parked_deep() is ordinary


def test_actor_enforces_single_lease_terminal_and_admission_intent() -> None:
    dispatched = []
    admissions = []
    terminals: queue.Queue[ValidationTerminal] = queue.Queue()
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=dispatched.append,
        admission_sink=lambda intent: admissions.append(intent) or True,
        terminal_sink=terminals,
    )
    actor.start()
    try:
        assert actor.submit(_candidate("a" * 64, "root-a", "vless"))
        assert actor.submit(_candidate("a" * 64, "root-a", "vless"))
        assert actor.submit(_candidate("b" * 64, "root-b", "trojan"))
        actor.capacity_available(2)
        _wait_until(lambda: len(dispatched) == 2)

        first, second = dispatched
        assert first.stage is ValidationStage.FAST_PROBE
        assert second.stage is ValidationStage.FAST_PROBE
        actor.terminal(
            ValidationTerminal(
                refresh_epoch="epoch-1",
                candidate_digest=first.candidate_digest,
                config_digest=first.config_digest,
                lease_id="wrong-lease",
                sequence=first.sequence,
                worker_id=1,
                worker_generation=1,
                kind=Tier3TerminalKind.PASS,
                node_json="{}",
                transaction_id=first.transaction_id,
                slice_id=first.slice_id,
                stage=first.stage,
            )
        )
        actor.terminal(
            ValidationTerminal(
                refresh_epoch="epoch-1",
                candidate_digest=first.candidate_digest,
                config_digest=first.config_digest,
                lease_id=first.lease_id,
                sequence=first.sequence,
                worker_id=1,
                worker_generation=1,
                kind=Tier3TerminalKind.PASS,
                node_json="{}",
                transaction_id=first.transaction_id,
                slice_id=first.slice_id,
                stage=first.stage,
                clean_confirmed=True,
            )
        )
        continuation = EndpointAttemptResult(
                refresh_epoch="epoch-1",
                transaction_id=second.transaction_id,
                candidate_digest=second.candidate_digest,
                config_digest=second.config_digest,
                lease_id=second.lease_id,
                slice_id=second.slice_id,
                sequence=second.sequence,
                stage=second.stage,
                worker_id=2,
                worker_generation=1,
                node_json="{}",
                reason="fast_continue",
                clean_confirmed=True,
            )
        actor.endpoint_result(continuation)
        actor.endpoint_result(continuation)
        actor.capacity_available(2)
        _wait_until(lambda: len(dispatched) == 3)
        deep = dispatched[2]
        assert deep.candidate_digest == second.candidate_digest
        assert deep.stage is ValidationStage.CANONICAL_DEEP
        assert deep.transaction_id == second.transaction_id
        actor.terminal(
            ValidationTerminal(
                refresh_epoch="epoch-1",
                candidate_digest=deep.candidate_digest,
                config_digest=deep.config_digest,
                lease_id=deep.lease_id,
                sequence=deep.sequence,
                worker_id=2,
                worker_generation=1,
                kind=Tier3TerminalKind.CENSORED,
                node_json="{}",
                transaction_id=deep.transaction_id,
                slice_id=deep.slice_id,
                stage=deep.stage,
                clean_confirmed=True,
            )
        )
        _wait_until(lambda: actor.snapshot().terminal == 2)
        assert len(admissions) == 1
        intent = admissions[0]
        actor.admission_result(
            AdmissionCommitted(
                refresh_epoch=intent.refresh_epoch,
                candidate_digest=intent.candidate_digest,
                lease_id=intent.lease_id,
                sequence=intent.sequence,
                pool_revision=1,
                net_new_pool_growth=True,
            )
        )
        _wait_until(lambda: actor.snapshot().admission_committed == 1)
        snapshot = actor.snapshot()
        assert snapshot.submitted == 2
        assert snapshot.queued == 0
        assert snapshot.leased == 0
        assert snapshot.passed == 1
        assert snapshot.censored == 1
        assert snapshot.slice_dispatched == 3
        assert snapshot.slice_completed == 3
        assert snapshot.fast_continued == 1
        assert snapshot.deep_completed == 1
        # Neither synthetic result carries a trusted transport attempt.  Such
        # facility/no-transport outcomes must not train lane allocation.
        assert snapshot.fast_rolling_completions == 0
        assert snapshot.fast_rolling_terminals == 0
        assert snapshot.fast_rolling_net_new == 0
        assert snapshot.deep_rolling_completions == 0
        assert snapshot.deep_rolling_terminals == 0
        assert snapshot.conservation_ok
    finally:
        assert actor.stop()


def test_actor_reclaims_idle_capacity_and_cancels_queued_on_seal() -> None:
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
    )
    actor.start()
    try:
        actor.capacity_available(1)
        actor.capacity_available(-1)
        assert actor.submit(_candidate("c" * 64, "root", "ss"))
        _wait_until(lambda: actor.snapshot().submitted == 1)
        assert actor.snapshot().queued == 1
        actor.seal()
        _wait_until(lambda: actor.snapshot().cancelled == 1)
        snapshot = actor.snapshot()
        assert snapshot.terminal == 1
        assert snapshot.completed == 0
        assert snapshot.cancelled_before_dispatch == 1
        assert snapshot.conservation_ok
    finally:
        actor.stop()


def test_actor_seal_converts_active_late_pass_to_cancelled_without_admission() -> None:
    dispatched = []
    admissions = []
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=dispatched.append,
        admission_sink=lambda intent: admissions.append(intent) or True,
        terminal_sink=queue.Queue(),
    )
    actor.start()
    try:
        assert actor.submit(_candidate("d" * 64, "root", "vless"))
        actor.capacity_available(1)
        _wait_until(lambda: len(dispatched) == 1)
        command = dispatched[0]
        actor.seal()
        _wait_until(lambda: actor.snapshot().sealed)
        actor.terminal(
            ValidationTerminal(
                refresh_epoch=command.refresh_epoch,
                candidate_digest=command.candidate_digest,
                config_digest=command.config_digest,
                lease_id=command.lease_id,
                sequence=command.sequence,
                worker_id=1,
                worker_generation=1,
                kind=Tier3TerminalKind.PASS,
                node_json="{}",
                transaction_id=command.transaction_id,
                slice_id=command.slice_id,
                stage=command.stage,
                clean_confirmed=True,
            )
        )
        _wait_until(lambda: actor.snapshot().terminal == 1)
        snapshot = actor.snapshot()
        assert snapshot.cancelled == 1
        assert snapshot.passed == 0
        assert snapshot.admission_intents == 0
        assert admissions == []
        assert snapshot.conservation_ok
    finally:
        actor.stop()


def test_actor_distinguishes_dispatched_work_from_queued_stop_cancellation() -> None:
    dispatched = []
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=dispatched.append,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(maxsize=1024),
        initial_worker_envelope=16,
    )
    actor.start()
    try:
        for index in range(128):
            digest = f"{index:064x}"
            assert actor.submit(
                _candidate(digest, f"root-{index % 5}", "vless")
            )
        actor.capacity_available(49)
        _wait_until(lambda: len(dispatched) == 32)
        actor.seal()
        _wait_until(
            lambda: actor.snapshot().cancelled_before_dispatch == 96
        )
        snapshot = actor.snapshot()
        assert snapshot.submitted == 128
        assert snapshot.dispatched == 32
        assert snapshot.active == 32
        assert snapshot.completed == 0
        assert snapshot.cancelled_before_dispatch == 96
        assert snapshot.terminal == 96
        assert snapshot.actor_owner_used == 32
        assert snapshot.ready_work_used == 32
        assert snapshot.conservation_ok
    finally:
        actor.stop()


def test_actor_stop_cancels_active_slice_without_counting_completion() -> None:
    dispatched = []
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=dispatched.append,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
    )
    actor.start()
    assert actor.submit(_candidate("e" * 64, "root", "vless"))
    actor.capacity_available(1)
    _wait_until(lambda: len(dispatched) == 1)

    assert actor.stop(timeout_seconds=1.0)
    snapshot = actor.snapshot()
    assert snapshot.cancelled == 1
    assert snapshot.slice_dispatched == 1
    assert snapshot.slice_completed == 0
    assert snapshot.slice_cancelled == 1
    assert snapshot.active == 0
    assert snapshot.conservation_ok


def test_actor_preserves_ingress_fifo_inside_root_family() -> None:
    dispatched = []
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=dispatched.append,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
    )
    actor.start()
    try:
        for index in range(20):
            assert actor.submit(
                _candidate(f"{index:064x}", "root-a", "vless", "coarse-a")
            )
        assert actor.submit(_candidate("f" * 64, "root-a", "vless", "coarse-b"))
        actor.capacity_available(2)
        _wait_until(lambda: len(dispatched) == 2)
        assert [item.candidate_digest for item in dispatched] == [
            "0" * 64,
            f"{1:064x}",
        ]
    finally:
        actor.stop()


def test_actor_lends_idle_capacity_to_one_deep_root_family() -> None:
    dispatched = []
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=dispatched.append,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    actor.start()
    try:
        for index in range(3):
            assert actor.submit(
                _candidate(f"{index + 1:064x}", "root-a", "trojan", f"a-{index}")
            )
        actor.capacity_available(3)
        _wait_until(lambda: len(dispatched) == 3)
        for command in list(dispatched):
            actor.endpoint_result(
                EndpointAttemptResult(
                    refresh_epoch=command.refresh_epoch,
                    transaction_id=command.transaction_id,
                    candidate_digest=command.candidate_digest,
                    config_digest=command.config_digest,
                    lease_id=command.lease_id,
                    slice_id=command.slice_id,
                    sequence=command.sequence,
                    stage=command.stage,
                    worker_id=1,
                    worker_generation=1,
                    node_json="{}",
                    reason="fast_continue",
                    clean_confirmed=True,
                )
            )
        actor.capacity_available(3)
        _wait_until(lambda: len(dispatched) == 6)
        assert all(
            command.stage is ValidationStage.CANONICAL_DEEP
            for command in dispatched[3:6]
        )
        assert actor.snapshot().active_deep == 3
    finally:
        actor.stop()


def test_actor_prioritizes_late_deep_root_on_next_release() -> None:
    dispatched = []
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=dispatched.append,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    actor.start()
    try:
        for index in range(4):
            assert actor.submit(
                _candidate(f"{index + 1:064x}", "root-a", "trojan", f"a-{index}")
            )
        actor.capacity_available(4)
        _wait_until(lambda: len(dispatched) == 4)
        for command in list(dispatched):
            actor.endpoint_result(
                EndpointAttemptResult(
                    refresh_epoch=command.refresh_epoch,
                    transaction_id=command.transaction_id,
                    candidate_digest=command.candidate_digest,
                    config_digest=command.config_digest,
                    lease_id=command.lease_id,
                    slice_id=command.slice_id,
                    sequence=command.sequence,
                    stage=command.stage,
                    worker_id=1,
                    worker_generation=1,
                    node_json="{}",
                    reason="fast_continue",
                    clean_confirmed=True,
                )
            )
        actor.capacity_available(4)
        _wait_until(lambda: len(dispatched) == 8)
        assert all(
            command.stage is ValidationStage.CANONICAL_DEEP
            for command in dispatched[4:8]
        )

        assert actor.submit(_candidate("e" * 64, "root-b", "vless", "b-1"))
        actor.capacity_available(1)
        _wait_until(lambda: len(dispatched) == 9)
        newcomer_fast = dispatched[8]
        actor.endpoint_result(
            EndpointAttemptResult(
                refresh_epoch=newcomer_fast.refresh_epoch,
                transaction_id=newcomer_fast.transaction_id,
                candidate_digest=newcomer_fast.candidate_digest,
                config_digest=newcomer_fast.config_digest,
                lease_id=newcomer_fast.lease_id,
                slice_id=newcomer_fast.slice_id,
                sequence=newcomer_fast.sequence,
                stage=newcomer_fast.stage,
                worker_id=2,
                worker_generation=1,
                node_json="{}",
                reason="fast_continue",
                clean_confirmed=True,
            )
        )
        actor.capacity_available(1)
        _wait_until(lambda: len(dispatched) == 10)
        assert dispatched[9].stage is ValidationStage.CANONICAL_DEEP
        assert dispatched[9].source_root == "root-b"
    finally:
        actor.stop()


def test_actor_starts_with_frozen_twelve_four_baseline() -> None:
    dispatched = []
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=dispatched.append,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    actor.start()
    try:
        actor.set_expansion_control(
            pressure=1.0,
            dispatch_enabled=True,
            external_fresh_backlog=48,
            fast_supply_state="ready",
        )
        _wait_until(lambda: actor.snapshot().fast_supply_state == "ready")
        for index in range(48):
            assert actor.submit(
                _candidate(
                    f"{index + 1:064x}",
                    f"root-{index % 8}",
                    f"family-{index % 8}",
                )
            )
        actor.capacity_available(16)
        _wait_until(lambda: len(dispatched) == 16)
        assert all(
            command.stage is ValidationStage.FAST_PROBE
            for command in dispatched
        )
        snapshot = actor.snapshot()
        assert (snapshot.fast_slots, snapshot.deep_slots) == (12, 4)
        assert snapshot.allocation_reason in {
            "endpoint_discovery_75_25",
            "transport_evidence_baseline",
        }

        for index, command in enumerate(list(dispatched[:16]), start=1):
            actor.endpoint_result(
                _endpoint_result(command, EndpointAttemptKind.TRANSIENT)
            )
            actor.capacity_available(1)
            _wait_until(lambda expected=16 + index: len(dispatched) == expected)
        snapshot = actor.snapshot()
        assert (snapshot.fast_slots, snapshot.deep_slots) == (12, 4)
        assert snapshot.allocation_reason in {
            "endpoint_discovery_75_25",
            "transport_evidence_baseline",
        }
        assert snapshot.active_fast == 12
        assert snapshot.active_deep == 4
    finally:
        actor.stop()


def test_actor_one_by_one_fast_completions_fill_baseline_deep_slots() -> None:
    dispatched = []
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=dispatched.append,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    actor.start()
    try:
        actor.set_expansion_control(
            pressure=1.0,
            dispatch_enabled=True,
            external_fresh_backlog=48,
            fast_supply_state="ready",
        )
        _wait_until(lambda: actor.snapshot().fast_supply_state == "ready")
        for index in range(48):
            assert actor.submit(
                _candidate(
                    f"{index + 1:064x}",
                    f"root-{index % 8}",
                    f"family-{index % 8}",
                )
            )
        actor.capacity_available(16)
        _wait_until(lambda: len(dispatched) == 16)
        initial_fast = list(dispatched)
        assert all(
            command.stage is ValidationStage.FAST_PROBE
            for command in initial_fast
        )

        for index, command in enumerate(initial_fast, start=1):
            actor.endpoint_result(
                _endpoint_result(command, EndpointAttemptKind.TRANSIENT)
            )
            actor.capacity_available(1)
            _wait_until(lambda expected=16 + index: len(dispatched) == expected)

        snapshot = actor.snapshot()
        assert snapshot.fast_slots == 12
        assert snapshot.deep_slots == 4
        assert snapshot.active_fast == 12
        assert snapshot.active_deep == 4
        assert sum(
            command.stage is ValidationStage.CANONICAL_DEEP
            for command in dispatched[-16:]
        ) == 4
    finally:
        actor.stop()


def test_actor_dispatches_shared_endpoint_controller_decisions() -> None:
    endpoints = (
        "https://probe-a.example/204",
        "http://probe-b.example/204",
        "https://probe-c.example/204",
        "http://probe-d.example/204",
    )
    controller = Tier3EndpointController(endpoints)
    dispatched = []
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=dispatched.append,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
        endpoint_controller=controller,
    )
    actor.start()
    try:
        for index in range(32):
            assert actor.submit(
                _candidate(
                    f"{index + 1:064x}",
                    f"root-{index % 8}",
                    f"family-{index % 8}",
                    probe_urls=endpoints,
                )
            )
        actor.capacity_available(16)
        _wait_until(lambda: len(dispatched) == 16)
        for command in list(dispatched):
            actor.terminal(
                ValidationTerminal(
                    refresh_epoch=command.refresh_epoch,
                    candidate_digest=command.candidate_digest,
                    config_digest=command.config_digest,
                    lease_id=command.lease_id,
                    sequence=command.sequence,
                    worker_id=1,
                    worker_generation=1,
                    kind=Tier3TerminalKind.PASS,
                    node_json=command.node_json,
                    transaction_id=command.transaction_id,
                    slice_id=command.slice_id,
                    stage=command.stage,
                    probe_policy_revision=command.probe_policy_revision,
                    portfolio_digest=command.portfolio_digest,
                    clean_confirmed=True,
                )
            )
        actor.capacity_available(16)
        _wait_until(lambda: len(dispatched) == 32)

        counts = {endpoint: 0 for endpoint in endpoints}
        for command in dispatched:
            counts[command.endpoint] += 1
            assert command.portfolio_digest
            assert command.probe_policy_revision >= 1
        assert all(count >= 2 for count in counts.values())
        snapshot = actor.snapshot()
        assert snapshot.controller_decision_count == 32
        assert snapshot.controller_dispatch_mismatch_count == 0
        assert controller.snapshot()["_policy"]["fast_entitlement_count"] == 32
    finally:
        actor.stop()


def test_actor_canonical_continuation_reparks_and_releases_credit_for_fresh_fast() -> None:
    endpoints = (
        "http://probe-a.example/204",
        "http://probe-b.example/204",
        "https://probe-c.example/204",
    )
    dispatched = []
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=dispatched.append,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=4,
        endpoint_controller=Tier3EndpointController(endpoints),
    )
    actor.start()
    try:
        actor.set_expansion_control(
            pressure=1.0,
            dispatch_enabled=True,
            external_fresh_backlog=8,
            fast_supply_state="ready",
        )
        _wait_until(lambda: actor.snapshot().fast_supply_state == "ready")
        for index in range(8):
            actor.submit(
                _candidate(
                    f"{index + 1:064x}",
                    "large-root",
                    "vless",
                    probe_urls=endpoints,
                )
            )
        actor.capacity_available(8)
        _wait_until(lambda: len(dispatched) == 8)
        for command in list(dispatched):
            actor.endpoint_result(
                _endpoint_result(command, EndpointAttemptKind.RIGHT_CENSORED)
            )
        _wait_until(lambda: actor.snapshot().fast_continued == 8)
        snapshot = actor.snapshot()
        assert snapshot.deep_ready <= snapshot.deep_cohort_limit
        assert snapshot.deep_retention_used > 0

        actor.capacity_available(1)
        _wait_until(lambda: len(dispatched) == 9)
        first_deep = dispatched[-1]
        assert first_deep.stage is ValidationStage.CANONICAL_DEEP
        late_digest = f"{999:064x}"
        assert actor.submit(
            _candidate(
                late_digest,
                "late-root",
                "trojan",
                probe_urls=endpoints,
            )
        )
        _wait_until(lambda: actor.snapshot().queued_fast > 0)
        actor.endpoint_result(
            _endpoint_result(first_deep, EndpointAttemptKind.TRANSIENT)
        )
        _wait_until(lambda: actor.snapshot().deep_parked > 0)
        parked = actor.snapshot()
        assert parked.actor_owner_used < parked.actor_owner_limit
        actor.capacity_available(1)
        _wait_until(lambda: len(dispatched) == 10)
        fresh = dispatched[-1]
        assert fresh.stage is ValidationStage.FAST_PROBE
        assert fresh.candidate_digest == late_digest
        assert actor.snapshot().conservation_ok
    finally:
        actor.stop()


def test_actor_facility_redispatch_reparks_instead_of_retaining_resident_credit() -> None:
    endpoints = ("http://probe-a.example/204", "https://probe-b.example/204")
    dispatched = []
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=dispatched.append,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=2,
        endpoint_controller=Tier3EndpointController(endpoints),
    )
    released: list[str] = []
    original_release = actor._release_owner_credit

    def record_release(candidate_digest: str) -> None:
        released.append(candidate_digest)
        original_release(candidate_digest)

    actor._release_owner_credit = record_release
    actor.start()
    try:
        assert actor.submit(
            _candidate(f"{1:064x}", "root-a", "vless", probe_urls=endpoints)
        )
        actor.capacity_available(1)
        _wait_until(lambda: len(dispatched) == 1)
        first = dispatched[-1]
        actor.endpoint_result(
            _endpoint_result(first, EndpointAttemptKind.FACILITY_FAIL)
        )
        _wait_until(lambda: first.candidate_digest in released)
        assert actor.snapshot().conservation_ok
    finally:
        actor.stop()


def test_actor_fast_yield_keeps_discovery_allocation() -> None:
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    actor._candidate_dispatched_count = 64
    actor._fast_net_new_count = 1

    fast, deep, reason = actor._desired_stage_slot_limits()

    assert (fast, deep) == (12, 4)
    assert reason == "transport_evidence_baseline"


def test_actor_frozen_baseline_ignores_historical_fast_throughput() -> None:
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    # Packaged audit shape: FAST produced more admissions per worker-second.
    # Dividing Wilson LCB by cumulative lane time instead of mean completion
    # time incorrectly made the smaller DEEP sample look several times better.
    actor._fast_completed_count = 68
    actor._fast_passed_count = 12
    actor._fast_occupied_ms = 193_414
    actor._deep_completed_count = 5
    actor._deep_passed_count = 2
    actor._deep_occupied_ms = 44_000
    actor._candidate_dispatched_count = 64
    actor._fast_net_new_count = 1
    actor._deep_net_new_count = 1

    fast, deep, reason = actor._desired_stage_slot_limits()

    assert (fast, deep) == (12, 4)
    assert reason == "transport_evidence_baseline"


def test_actor_frozen_baseline_ignores_historical_deep_throughput() -> None:
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    actor._fast_completed_count = 64
    actor._fast_passed_count = 8
    actor._fast_occupied_ms = 192_000
    actor._deep_completed_count = 16
    actor._deep_passed_count = 8
    actor._deep_occupied_ms = 48_000
    actor._candidate_dispatched_count = 64
    actor._fast_net_new_count = 1
    actor._deep_net_new_count = 1

    fast, deep, reason = actor._desired_stage_slot_limits()

    assert (fast, deep) == (12, 4)
    assert reason == "transport_evidence_baseline"


def test_actor_does_not_borrow_mature_fast_evidence_for_tiny_deep_sample() -> None:
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    now = time.monotonic()
    for index in range(16):
        actor._rolling_stage_samples.append(
            _StageRollingSample(
                completed_at=now,
                candidate_digest=f"fast-mature-{index}",
                stage=ValidationStage.FAST_PROBE,
                occupied_ms=2_000,
                passed=index < 3,
                terminal=index < 3,
                net_new=index < 3,
                transport_completed=True,
            )
        )
    # This mirrors the regressed packaged trace: DEEP happened to pass two of
    # seven early candidates, but has not earned enough independent evidence
    # to take half of the validator pool away from FAST discovery.
    for index in range(7):
        actor._rolling_stage_samples.append(
            _StageRollingSample(
                completed_at=now,
                candidate_digest=f"deep-tiny-{index}",
                stage=ValidationStage.CANONICAL_DEEP,
                occupied_ms=4_500,
                passed=index < 2,
                terminal=index < 2,
                net_new=index < 2,
                transport_completed=True,
            )
        )

    fast, deep, reason = actor._desired_stage_slot_limits()

    assert (fast, deep) == (12, 4)
    assert reason == "transport_evidence_baseline"


def test_actor_keeps_frozen_fast_share_when_session_dominance_is_proven() -> None:
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    actor._external_fresh_backlog = 1_000
    actor._fast_supply_state = "ready"
    now = time.monotonic()
    for index in range(64):
        actor._rolling_stage_samples.append(
            _StageRollingSample(
                completed_at=now - 30.0,
                candidate_digest=f"fast-session-{index}",
                stage=ValidationStage.FAST_PROBE,
                occupied_ms=1_100,
                passed=index < 10,
                terminal=index < 10,
                net_new=index < 10,
                transport_completed=True,
            )
        )
    for index in range(24):
        actor._rolling_stage_samples.append(
            _StageRollingSample(
                completed_at=now - 5.0,
                candidate_digest=f"deep-session-{index}",
                stage=ValidationStage.CANONICAL_DEEP,
                occupied_ms=4_200,
                passed=index == 0,
                terminal=index == 0,
                net_new=index == 0,
                transport_completed=True,
            )
        )

    fast, deep, reason = actor._desired_stage_slot_limits()

    assert (fast, deep) == (12, 4)
    assert reason == "fresh_supply_hard_baseline"


def test_actor_does_not_claim_fast_dominance_without_independent_deep_evidence() -> None:
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    actor._external_fresh_backlog = 1_000
    now = time.monotonic()
    for index in range(64):
        actor._rolling_stage_samples.append(
            _StageRollingSample(
                completed_at=now,
                candidate_digest=f"fast-only-{index}",
                stage=ValidationStage.FAST_PROBE,
                occupied_ms=1_100,
                passed=index < 10,
                terminal=index < 10,
                net_new=index < 10,
                transport_completed=True,
            )
        )
    for index in range(7):
        actor._rolling_stage_samples.append(
            _StageRollingSample(
                completed_at=now,
                candidate_digest=f"deep-tiny-{index}",
                stage=ValidationStage.CANONICAL_DEEP,
                occupied_ms=4_200,
                transport_completed=True,
            )
        )

    fast, deep, reason = actor._desired_stage_slot_limits()

    assert (fast, deep) == (12, 4)
    assert reason == "fresh_supply_hard_baseline"


def test_actor_places_zero_yield_deep_on_early_probation_after_fast_growth() -> None:
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    actor._external_fresh_backlog = 1_000
    actor._expected_fast_supply = True
    actor._fast_supply_state = "ready"
    now = time.monotonic()
    for index in range(8):
        actor._rolling_stage_samples.append(
            _StageRollingSample(
                completed_at=now,
                candidate_digest=f"fast-probation-{index}",
                stage=ValidationStage.FAST_PROBE,
                occupied_ms=2_500,
                passed=index == 0,
                terminal=index == 0,
                net_new=index == 0,
                transport_completed=True,
            )
        )
    # Zero-yield evidence remains authoritative after it ages out of the
    # 60-second rolling window; otherwise the lane drifts back to 12/4 without
    # any new DEEP success.
    for index in range(4):
        actor._rolling_stage_samples.append(
            _StageRollingSample(
                completed_at=now - 61.0,
                candidate_digest=f"deep-probation-{index}",
                stage=ValidationStage.CANONICAL_DEEP,
                occupied_ms=5_000,
                transport_completed=True,
            )
        )

    fast, deep, reason = actor._desired_stage_slot_limits()

    assert (fast, deep) == (12, 4)
    assert reason == "fresh_supply_hard_baseline"


def test_actor_keeps_frozen_baseline_before_deep_zero_yield_threshold() -> None:
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    actor._external_fresh_backlog = 1_000
    actor._expected_fast_supply = True
    actor._fast_supply_state = "ready"
    now = time.monotonic()
    for index in range(16):
        actor._rolling_stage_samples.append(
            _StageRollingSample(
                completed_at=now,
                candidate_digest=f"fast-zero-probation-{index}",
                stage=ValidationStage.FAST_PROBE,
                occupied_ms=2_500,
                passed=index < 3,
                terminal=index < 3,
                net_new=index < 3,
                transport_completed=True,
            )
        )
    for index in range(31):
        actor._rolling_stage_samples.append(
            _StageRollingSample(
                completed_at=now,
                candidate_digest=f"deep-zero-probation-{index}",
                stage=ValidationStage.CANONICAL_DEEP,
                occupied_ms=2_000,
                transport_completed=True,
            )
        )

    fast, deep, reason = actor._desired_stage_slot_limits()

    assert (fast, deep) == (12, 4)
    assert reason == "fresh_supply_hard_baseline"


def test_actor_places_sustained_zero_yield_deep_on_single_debt_slot() -> None:
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    actor._external_fresh_backlog = 1_000
    actor._expected_fast_supply = True
    actor._fast_supply_state = "ready"
    now = time.monotonic()
    for index in range(16):
        actor._rolling_stage_samples.append(
            _StageRollingSample(
                completed_at=now,
                candidate_digest=f"fast-zero-debt-{index}",
                stage=ValidationStage.FAST_PROBE,
                occupied_ms=2_500,
                passed=index < 3,
                terminal=index < 3,
                net_new=index < 3,
                transport_completed=True,
            )
        )
    for index in range(32):
        actor._rolling_stage_samples.append(
            _StageRollingSample(
                completed_at=now,
                candidate_digest=f"deep-zero-debt-{index}",
                stage=ValidationStage.CANONICAL_DEEP,
                occupied_ms=1_875,
                transport_completed=True,
            )
        )

    fast, deep, reason = actor._desired_stage_slot_limits()

    assert (fast, deep) == (15, 1)
    assert reason == "deep_zero_yield_debt"


def test_actor_does_not_enter_early_deep_probation_without_fast_pool_growth() -> None:
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    actor._external_fresh_backlog = 1_000
    actor._expected_fast_supply = True
    actor._fast_supply_state = "ready"
    now = time.monotonic()
    for index in range(8):
        actor._rolling_stage_samples.append(
            _StageRollingSample(
                completed_at=now,
                candidate_digest=f"fast-no-growth-{index}",
                stage=ValidationStage.FAST_PROBE,
                occupied_ms=2_500,
                transport_completed=True,
            )
        )
    for index in range(4):
        actor._rolling_stage_samples.append(
            _StageRollingSample(
                completed_at=now,
                candidate_digest=f"deep-no-growth-{index}",
                stage=ValidationStage.CANONICAL_DEEP,
                occupied_ms=5_000,
                transport_completed=True,
            )
        )

    fast, deep, reason = actor._desired_stage_slot_limits()

    assert (fast, deep) == (12, 4)
    assert reason == "fresh_supply_hard_baseline"


def test_actor_keeps_frozen_baseline_with_single_deep_gain() -> None:
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    actor._external_fresh_backlog = 1_000
    actor._expected_fast_supply = True
    actor._fast_supply_state = "ready"
    now = time.monotonic()
    for index in range(16):
        actor._rolling_stage_samples.append(
            _StageRollingSample(
                completed_at=now,
                candidate_digest=f"fast-low-yield-{index}",
                stage=ValidationStage.FAST_PROBE,
                occupied_ms=2_500,
                passed=index < 4,
                terminal=index < 4,
                net_new=index < 4,
                transport_completed=True,
            )
        )
    for index in range(8):
        actor._rolling_stage_samples.append(
            _StageRollingSample(
                completed_at=now,
                candidate_digest=f"deep-single-gain-{index}",
                stage=ValidationStage.CANONICAL_DEEP,
                occupied_ms=3_750,
                passed=index == 0,
                terminal=index == 0,
                net_new=index == 0,
                transport_completed=True,
            )
        )

    fast, deep, reason = actor._desired_stage_slot_limits()

    assert (fast, deep) == (12, 4)
    assert reason == "fresh_supply_hard_baseline"


def test_actor_keeps_baseline_when_deep_has_two_recent_pool_gains() -> None:
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    actor._external_fresh_backlog = 1_000
    actor._expected_fast_supply = True
    actor._fast_supply_state = "ready"
    now = time.monotonic()
    for index in range(16):
        actor._rolling_stage_samples.append(
            _StageRollingSample(
                completed_at=now,
                candidate_digest=f"fast-two-deep-{index}",
                stage=ValidationStage.FAST_PROBE,
                occupied_ms=2_500,
                passed=index < 4,
                terminal=index < 4,
                net_new=index < 4,
                transport_completed=True,
            )
        )
    for index in range(8):
        actor._rolling_stage_samples.append(
            _StageRollingSample(
                completed_at=now,
                candidate_digest=f"deep-two-gains-{index}",
                stage=ValidationStage.CANONICAL_DEEP,
                occupied_ms=3_750,
                passed=index < 2,
                terminal=index < 2,
                net_new=index < 2,
                transport_completed=True,
            )
        )

    fast, deep, reason = actor._desired_stage_slot_limits()

    assert (fast, deep) == (12, 4)
    assert reason == "fresh_supply_hard_baseline"


def test_actor_deep_zero_yield_moves_to_bounded_debt_share() -> None:
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    actor._fast_parked_count = 1
    now = time.monotonic()
    # An early DEEP admission has aged out. The current 60s/64-completion
    # window is a full worker-minute with no durable pool growth.
    actor._rolling_stage_samples.append(
        _StageRollingSample(
            completed_at=now - 61.0,
            candidate_digest="early-pass",
            stage=ValidationStage.CANONICAL_DEEP,
            occupied_ms=5_000,
            passed=True,
            net_new=True,
        )
    )
    for index in range(32):
        actor._rolling_stage_samples.append(
            _StageRollingSample(
                completed_at=now,
                candidate_digest=f"deep-zero-{index}",
                stage=ValidationStage.CANONICAL_DEEP,
                occupied_ms=1_875,
            )
        )
    actor._allocation_last_evaluated_at = 0.0

    actor._refresh_stage_slot_limits()
    assert (actor._fast_slot_limit, actor._deep_slot_limit) == (12, 4)
    actor._allocation_last_evaluated_at = 0.0
    actor._allocation_last_moved_at = 0.0
    actor._refresh_stage_slot_limits()
    assert (actor._fast_slot_limit, actor._deep_slot_limit) == (13, 3)
    actor._allocation_last_evaluated_at = 0.0
    actor._allocation_last_moved_at = 0.0
    actor._refresh_stage_slot_limits()
    assert (actor._fast_slot_limit, actor._deep_slot_limit) == (14, 2)
    actor._allocation_last_evaluated_at = 0.0
    actor._allocation_last_moved_at = 0.0
    actor._refresh_stage_slot_limits()
    assert (actor._fast_slot_limit, actor._deep_slot_limit) == (15, 1)
    assert actor._allocation_reason == "deep_zero_yield_debt"


def test_actor_uses_external_fresh_supply_for_deep_zero_yield() -> None:
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    actor._external_fresh_backlog = 1129
    actor._expected_fast_supply = True
    actor._fast_supply_state = "ready"
    now = time.monotonic()
    for index in range(32):
        actor._rolling_stage_samples.append(
            _StageRollingSample(
                completed_at=now,
                candidate_digest=f"external-deep-zero-{index}",
                stage=ValidationStage.CANONICAL_DEEP,
                occupied_ms=1_875,
            )
        )

    fast, deep, reason = actor._desired_stage_slot_limits()

    assert (fast, deep) == (15, 1)
    assert reason == "deep_zero_yield_debt"


def test_actor_deep_zero_yield_latch_survives_window_age_and_fresh_return() -> None:
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    actor._external_fresh_backlog = 100
    actor._fast_supply_state = "ready"
    now = time.monotonic()
    for index in range(32):
        actor._rolling_stage_samples.append(
            _StageRollingSample(
                completed_at=now,
                candidate_digest=f"latch-{index}",
                stage=ValidationStage.CANONICAL_DEEP,
                occupied_ms=1_875,
            )
        )

    assert actor._desired_stage_slot_limits()[:2] == (15, 1)
    assert actor._deep_zero_yield_latched is True

    for sample in actor._rolling_stage_samples:
        sample.completed_at = now - 120.0
    assert actor._desired_stage_slot_limits()[:2] == (15, 1)

    actor._external_fresh_backlog = 0
    actor._fast_supply_state = "exhausted"
    actor._fast_supply_nonactionable_revisions = 2
    actor._deep_parked_count = 64
    actor._actionable_deep = 64
    actor._fast_slot_limit = 15
    actor._deep_slot_limit = 1
    assert actor._stage_slot_limits() == (0, 16)

    actor._external_fresh_backlog = 1
    actor._fast_supply_state = "imminent"
    actor._fast_supply_nonactionable_revisions = 0
    assert actor._stage_slot_limits() == (14, 2)
    actor._fast_ready_count = 1
    assert actor._stage_slot_limits() == (15, 1)

    actor._deep_net_new_count += 1
    actor._desired_stage_slot_limits()
    assert actor._deep_zero_yield_latched is True
    assert actor._deep_probation_active is True
    assert actor._desired_stage_slot_limits()[:2] == (14, 2)


def test_actor_latches_deep_when_worker_second_yield_is_far_below_fast() -> None:
    actor = Tier3ExpansionActor(
        "epoch-worker-efficiency",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    actor._external_fresh_backlog = 1_000
    actor._fast_supply_state = "ready"
    now = time.monotonic()
    for index in range(32):
        actor._rolling_stage_samples.append(
            _StageRollingSample(
                completed_at=now,
                candidate_digest=f"fast-efficient-{index}",
                stage=ValidationStage.FAST_PROBE,
                occupied_ms=1_250,
                net_new=index < 10,
                transport_completed=True,
            )
        )
    for index in range(20):
        actor._rolling_stage_samples.append(
            _StageRollingSample(
                completed_at=now,
                candidate_digest=f"deep-inefficient-{index}",
                stage=ValidationStage.CANONICAL_DEEP,
                occupied_ms=3_500,
                net_new=index < 5,
                transport_completed=True,
            )
        )

    # DEEP contributed one third of admissions, so the old admission-share
    # heuristic retained 12/4 despite using roughly six times the worker time
    # per net-new node. Two independent allocator revisions must latch 15/1.
    actor._deep_low_yield_last_evaluated_at = 0.0
    assert actor._desired_stage_slot_limits()[:2] == (12, 4)
    actor._deep_low_yield_last_evaluated_at = 0.0
    assert actor._desired_stage_slot_limits()[:2] == (15, 1)
    assert actor._deep_zero_yield_latched is True


def test_actor_requires_two_independent_deep_recovery_windows() -> None:
    actor = Tier3ExpansionActor(
        "epoch-gradual-recovery",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    actor._external_fresh_backlog = 100
    actor._fast_supply_state = "ready"
    actor._deep_zero_yield_latched = True
    actor._deep_low_yield_latch_reason = "deep_low_yield_latched"
    now = time.monotonic()
    for index in range(32):
        actor._rolling_stage_samples.append(
            _StageRollingSample(
                completed_at=now,
                candidate_digest=f"fast-recovery-{index}",
                stage=ValidationStage.FAST_PROBE,
                occupied_ms=1_000,
                net_new=index < 8,
                transport_completed=True,
            )
        )

    def add_deep_net_new(index: int) -> None:
        actor._deep_completed_count += 1
        actor._deep_net_new_count += 1
        actor._rolling_stage_samples.append(
            _StageRollingSample(
                completed_at=now,
                candidate_digest=f"deep-recovery-{index}",
                stage=ValidationStage.CANONICAL_DEEP,
                occupied_ms=1_000,
                net_new=True,
                transport_completed=True,
            )
        )

    add_deep_net_new(1)
    assert actor._desired_stage_slot_limits()[:2] == (14, 2)
    assert actor._deep_recovery_level == 1

    add_deep_net_new(2)
    assert actor._desired_stage_slot_limits()[:2] == (14, 2)
    add_deep_net_new(3)
    assert actor._desired_stage_slot_limits()[:2] == (13, 3)
    assert actor._deep_zero_yield_latched is True
    assert actor._deep_recovery_level == 2

    add_deep_net_new(4)
    assert actor._desired_stage_slot_limits()[:2] == (13, 3)
    add_deep_net_new(5)
    assert actor._desired_stage_slot_limits()[:2] == (12, 4)
    assert actor._deep_zero_yield_latched is False
    assert actor._deep_recovery_level == 0


def test_actor_failed_deep_recovery_relocks_without_reusing_old_pass() -> None:
    actor = Tier3ExpansionActor(
        "epoch-failed-recovery",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    actor._external_fresh_backlog = 100
    actor._fast_supply_state = "ready"
    actor._deep_zero_yield_latched = True
    actor._deep_low_yield_latch_reason = "deep_low_yield_latched"
    now = time.monotonic()
    actor._deep_completed_count = 1
    actor._deep_net_new_count = 1
    actor._rolling_stage_samples.append(
        _StageRollingSample(
            completed_at=now,
            candidate_digest="single-deep-pass",
            stage=ValidationStage.CANONICAL_DEEP,
            occupied_ms=1_000,
            net_new=True,
            transport_completed=True,
        )
    )
    assert actor._desired_stage_slot_limits()[:2] == (14, 2)

    for index in range(8):
        actor._deep_completed_count += 1
        actor._rolling_stage_samples.append(
            _StageRollingSample(
                completed_at=now,
                candidate_digest=f"failed-deep-window-{index}",
                stage=ValidationStage.CANONICAL_DEEP,
                occupied_ms=1_000,
                transport_completed=True,
            )
        )
    assert actor._desired_stage_slot_limits()[:2] == (15, 1)
    assert actor._deep_probation_active is False
    assert actor._deep_recovery_level == 0
    assert actor._deep_zero_yield_latch_net_new_count == 1
    assert actor._desired_stage_slot_limits()[:2] == (15, 1)


def test_actor_deep_cohort_work_steals_full_envelope_without_fast_supply() -> None:
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    actor._fast_slot_limit = 12
    actor._deep_slot_limit = 4
    actor._expected_fast_supply = False
    actor._fast_supply_state = "exhausted"
    actor._fast_supply_nonactionable_revisions = 2

    assert actor._deep_cohort_limit() == 16

    actor._expected_fast_supply = True
    actor._fast_supply_state = "ready"
    assert actor._deep_cohort_limit() == 8


def test_actor_blocked_upstream_lends_at_most_two_fast_slots() -> None:
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    actor._fast_slot_limit = 15
    actor._deep_slot_limit = 1
    actor._fast_supply_state = "blocked"
    actor._fast_supply_nonactionable_revisions = 2
    actor._external_fresh_backlog = 1_000
    actor._fast_ready_count = 2
    actor._active_fast_count = 1
    actor._deep_parked_count = 80
    actor._actionable_deep = 80

    # Actor-owned FAST is immediately consumable, so it retains the declared
    # 15/1 policy even when the upstream label is conservatively blocked.
    assert actor._stage_slot_limits() == (15, 1)
    assert actor._deep_cohort_limit() == 2

    # During a real handoff gap, actionable upstream allows only the bounded
    # two-slot DEEP loan rather than the old 1/15 or 2/14 swing.
    actor._fast_ready_count = 0
    actor._active_fast_count = 0
    assert actor._stage_slot_limits() == (13, 3)
    assert actor._deep_cohort_limit() == 6

    # Real actor-owned FAST work restores the declared 15/1 policy. DEEP may
    # borrow two slots only during the bounded gap before the handoff arrives.
    actor._fast_supply_state = "imminent"
    actor._external_fresh_backlog = 1
    actor._fast_ready_count = 1
    assert actor._stage_slot_limits() == (15, 1)


def test_actor_full_deep_work_steal_requires_two_truly_exhausted_revisions() -> None:
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    actor._fast_slot_limit = 15
    actor._deep_slot_limit = 1
    actor._deep_parked_count = 80
    actor._actionable_deep = 80
    actor._fast_supply_state = "exhausted"
    actor._fast_supply_nonactionable_revisions = 1

    assert actor._stage_slot_limits() == (13, 3)

    actor._fast_supply_nonactionable_revisions = 2
    assert actor._stage_slot_limits() == (0, 16)


def test_actor_deep_retention_is_bounded_without_closing_fast() -> None:
    terminals: queue.Queue[ValidationTerminal] = queue.Queue()
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=terminals,
        initial_worker_envelope=1,
    )
    retained = []
    for index in range(8):
        state = _CandidateState(
            envelope=_candidate(
                f"{index + 1:064x}",
                f"root-{index}",
                "vless",
            ),
            transaction_id=f"epoch-1:{index}",
        )
        actor._states[state.envelope.candidate_digest] = state
        assert actor._retain_deep(state) is Tier3DeepRetentionDecision.RETAINED
        actor._park_deep(state)
        retained.append(state)

    overflow = _CandidateState(
        envelope=_candidate("f" * 64, "root-overflow", "trojan"),
        transaction_id="epoch-1:overflow",
    )
    actor._states[overflow.envelope.candidate_digest] = overflow
    assert (
        actor._retain_deep(overflow)
        is Tier3DeepRetentionDecision.OVERFLOW_CENSORED
    )
    snapshot = actor._build_snapshot()
    assert snapshot.deep_retention_limit == 8
    assert snapshot.deep_retention_used == 8
    assert snapshot.deep_retention_overflow_censored == 1
    assert overflow.deep_retained is False


def test_actor_late_bucket_fairly_replaces_overrepresented_retention() -> None:
    terminals: queue.Queue[ValidationTerminal] = queue.Queue()
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=terminals,
        initial_worker_envelope=1,
    )
    for index in range(8):
        state = _CandidateState(
            envelope=_candidate(
                f"{index + 1:064x}",
                f"root-{index // 2}",
                "vless",
            ),
            transaction_id=f"epoch-1:{index}",
        )
        actor._states[state.envelope.candidate_digest] = state
        assert actor._retain_deep(state) is Tier3DeepRetentionDecision.RETAINED
        actor._park_deep(state)

    newcomer = _CandidateState(
        envelope=_candidate("e" * 64, "root-late", "trojan"),
        transaction_id="epoch-1:newcomer",
    )
    actor._states[newcomer.envelope.candidate_digest] = newcomer
    assert (
        actor._retain_deep(newcomer)
        is Tier3DeepRetentionDecision.FAIR_REPLACED
    )
    actor._park_deep(newcomer)

    snapshot = actor._build_snapshot()
    assert snapshot.deep_retention_used == 8
    assert snapshot.deep_retention_fair_replaced == 1
    assert newcomer.deep_retained is True
    replaced = terminals.get_nowait()
    assert replaced.kind is Tier3TerminalKind.CENSORED
    assert replaced.reason == "deep_retention_fair_replaced"


def test_actor_bounds_deep_borrowing_while_fast_supply_is_between_handoffs() -> None:
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    actor._fast_slot_limit = 15
    actor._deep_slot_limit = 1
    actor._fast_supply_state = "ready"
    actor._deep_ready_count = 10
    actor._active_deep_count = 1

    assert actor._stage_slot_limits() == (13, 3)
    assert actor._choose_stage() is ValidationStage.CANONICAL_DEEP

    snapshot = actor._build_snapshot()
    assert snapshot.desired_fast_slots == 15
    assert snapshot.desired_deep_slots == 1
    assert snapshot.fast_slots == 13
    assert snapshot.deep_slots == 3
    assert snapshot.borrowed_deep_slots == 2


def test_actor_latched_deep_lane_borrows_at_most_one_fast_slot() -> None:
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    actor._fast_slot_limit = 15
    actor._deep_slot_limit = 1
    actor._deep_zero_yield_latched = True
    actor._fast_supply_state = "blocked"
    actor._fast_supply_nonactionable_revisions = 1
    actor._deep_parked_count = 64
    actor._actionable_deep = 64

    assert actor._stage_slot_limits() == (14, 2)

    snapshot = actor._build_snapshot()
    assert snapshot.desired_fast_slots == 15
    assert snapshot.desired_deep_slots == 1
    assert snapshot.borrowed_deep_slots == 1


def test_actor_fast_entitlement_stays_frozen_despite_recent_net_new_p95() -> None:
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    now = time.monotonic()
    for index in range(32):
        actor._rolling_stage_samples.append(
            _StageRollingSample(
                completed_at=now,
                candidate_digest=f"fast-{index}",
                stage=ValidationStage.FAST_PROBE,
                occupied_ms=2_100,
                passed=index < 8,
                net_new=index < 8,
                pass_elapsed_ms=2_000,
            )
        )
    actor._fast_soft_completion_counter = 32

    actor._refresh_fast_soft_entitlement()

    # The restored throughput baseline freezes FAST at a full 3s entitlement.
    # Historical success timing may be observed, but cannot shorten it.
    assert actor._fast_soft_entitlement_seconds == 3.0


def test_actor_dispatched_command_keeps_frozen_fast_budget() -> None:
    commands = []
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=commands.append,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=4,
    )
    actor._fast_soft_entitlement_seconds = 2.75
    actor.start()
    try:
        assert actor.submit(
            _candidate(
                "f" * 64,
                "root-a",
                "vless",
                probe_urls=("http://probe.example/204",),
            )
        )
        actor.capacity_available(1)
        _wait_until(lambda: len(commands) == 1)
        assert commands[0].probe_budget_seconds == 3.0
        assert commands[0].candidate_hard_cap_seconds == 5.75
    finally:
        assert actor.stop(1.0)


def test_actor_deep_net_new_resets_underbudgeted_censored_fast_slice() -> None:
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    actor._fast_soft_entitlement_seconds = 2.25
    candidate_digest = "tail-pass"
    fast_sample = _StageRollingSample(
        completed_at=time.monotonic(),
        candidate_digest=candidate_digest,
        stage=ValidationStage.FAST_PROBE,
        occupied_ms=2_250,
        probe_elapsed_ms=2_250,
        right_censored=True,
    )
    deep_sample = _StageRollingSample(
        completed_at=time.monotonic(),
        candidate_digest=candidate_digest,
        stage=ValidationStage.CANONICAL_DEEP,
        occupied_ms=2_800,
        passed=True,
    )
    actor._rolling_stage_samples.extend((fast_sample, deep_sample))
    actor._latest_stage_sample_by_candidate[
        (candidate_digest, ValidationStage.FAST_PROBE)
    ] = fast_sample
    actor._latest_stage_sample_by_candidate[
        (candidate_digest, ValidationStage.CANONICAL_DEEP)
    ] = deep_sample

    actor._mark_rolling_net_new(
        candidate_digest,
        ValidationStage.CANONICAL_DEEP,
    )

    assert actor._fast_soft_entitlement_seconds == 3.0
    assert actor._fast_soft_reset_count == 1


def test_actor_same_worker_envelope_does_not_reset_recovered_allocation() -> None:
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    actor._fast_completed_count = 32
    actor._fast_passed_count = 1
    actor._fast_slot_limit = 8
    actor._deep_slot_limit = 8
    actor._deep_completed_count = 16
    actor._deep_passed_count = 1
    actor._allocation_reason = "both_stages_yield_50_50"
    actor._allocation_candidate_reason = actor._allocation_reason
    actor.start()
    try:
        actor.set_worker_envelope(16)
        _wait_until(
            lambda: actor.snapshot().allocation_reason
            == "both_stages_yield_50_50"
        )
        snapshot = actor.snapshot()
        assert (snapshot.fast_slots, snapshot.deep_slots) == (8, 8)
    finally:
        actor.stop()


def _seed_lane_samples(
    actor: Tier3ExpansionActor,
    *,
    now: float,
    fast_completions: int,
    fast_net_new: int,
    deep_completions: int,
) -> None:
    """Seed transport-backed samples for both lanes."""

    for index in range(fast_completions):
        actor._rolling_stage_samples.append(
            _StageRollingSample(
                completed_at=now,
                candidate_digest=f"fast-{index}",
                stage=ValidationStage.FAST_PROBE,
                occupied_ms=1_000,
                passed=index < fast_net_new,
                net_new=index < fast_net_new,
                transport_completed=True,
            )
        )
    for index in range(deep_completions):
        actor._rolling_stage_samples.append(
            _StageRollingSample(
                completed_at=now,
                candidate_digest=f"deep-{index}",
                stage=ValidationStage.CANONICAL_DEEP,
                occupied_ms=1_875,
                transport_completed=True,
            )
        )


def test_deep_experimental_zero_run_guard_is_test_only() -> None:
    from tests.tier3_takeover_policy_oracle import claude_zero_run_is_conclusive

    # Same evidence, different decision. This comparison is not a throughput
    # claim; the unverified guard must not change the production baseline.
    assert not claude_zero_run_is_conclusive(100, 3, 32)

    actor = Tier3ExpansionActor(
        "epoch-scarce",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    actor._fast_parked_count = 1
    _seed_lane_samples(
        actor,
        now=time.monotonic(),
        fast_completions=100,
        fast_net_new=3,
        deep_completions=32,
    )
    actor._allocation_last_evaluated_at = 0.0

    actor._refresh_stage_slot_limits()

    assert actor._deep_zero_yield_latched is True
    assert actor._desired_stage_slot_limits()[:2] == (15, 1)


def test_deep_is_still_latched_once_the_empty_run_is_long_enough() -> None:
    """The guard delays the verdict on noise; it never removes it."""

    actor = Tier3ExpansionActor(
        "epoch-conclusive",
        dispatch_sink=lambda _command: None,
        admission_sink=lambda _intent: True,
        terminal_sink=queue.Queue(),
        initial_worker_envelope=16,
    )
    actor._fast_parked_count = 1
    # At FAST's own 3% rate, 160 empty DEEP completions would have yielded
    # about five admissions, so the emptiness is now the lane's own.
    _seed_lane_samples(
        actor,
        now=time.monotonic(),
        fast_completions=100,
        fast_net_new=3,
        deep_completions=160,
    )
    actor._allocation_last_evaluated_at = 0.0

    actor._refresh_stage_slot_limits()

    assert actor._deep_zero_yield_latched is True
