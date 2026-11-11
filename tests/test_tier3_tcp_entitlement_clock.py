"""The TCP probe's entitlement clock measures transport time only.

The start ACK (prestart-ledger claim, scheduler ``commit_handoff``, ledger
``mark_started``) runs synchronously on the wave's event loop before the
transport starts. Under lock, GIL or CPU contention it can take a large part
of the 1.25 s entitlement. That wait must not be charged to the probe that
waits, nor to the other probes of the same wave, whose transports only start
after every ACK of the wave has returned. Charging it turned responsive and
refused candidates into hard-censored TCP outcomes (28-80% of late outcomes in
slow or contended runs).

The ACK itself is unchanged: it still returns before the transport starts, so
``commit_handoff`` precedes every outcome for its ticket.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import manager as manager_module
import tester as tester_module
from manager import Tier3CandidateDispatchContext, Tier3TcpPrestartLeaseLedger
from models import Node, NodeStatus
from tester import (
    Tier3TcpProbeEntitlement,
    _collect_tcp_probe_entitlements,
    _run_tcp_probe_entitlement,
    scan_tier3_tcp_candidates,
)
from tier3_candidate_scheduler import (
    Tier3CandidateFeedbackTicket,
    Tier3CandidateScheduler,
)
from tier3_sources import CollectionReport


# Production transport floor at PI pressure 1 and the delivery grace.
ENTITLEMENT = 1.25
GRACE = 0.25
# Windows time.monotonic() ticks every 15.6 ms and asyncio may fire a timer
# up to one tick early.
CLOCK_TICK = 0.02
EXHAUSTED = "tcp_probe_entitlement_exhausted"


def _node(name: str) -> Node:
    return Node(name=name, protocol="ss", address=f"{name}.invalid", port=443,
                tier="tier3")


def _result(node: Node, status: NodeStatus, reason: str = "") -> Node:
    updated = node.clone()
    updated.status = status
    updated.latency_ms = 50 if status is NodeStatus.ALIVE else None
    updated.suspicious_reason = reason
    return updated


def _wave(
    nodes: list[Node],
    *,
    acknowledge,
    probe,
    entitlement_seconds: float = ENTITLEMENT,
    result_observer=None,
    transport_began=None,
):
    """Run one wave the way scan_tier3_tcp_candidates does, keeping entitlements."""

    async def run():
        semaphore = asyncio.Semaphore(len(nodes))
        task_map: dict[asyncio.Task[Node], Tier3TcpProbeEntitlement] = {}
        for node in nodes:
            entitlement = Tier3TcpProbeEntitlement(node=node)
            task = asyncio.create_task(
                _run_tcp_probe_entitlement(
                    entitlement,
                    semaphore=semaphore,
                    entitlement_seconds=entitlement_seconds,
                    cleanup_slack_seconds=GRACE,
                    cancel_event=None,
                    transport_start_observer=acknowledge,
                    transport_began_observer=transport_began,
                    probe_factory=lambda timeout, node=node: probe(node, timeout),
                )
            )
            task_map[task] = entitlement
        outcome = await _collect_tcp_probe_entitlements(
            task_map,
            entitlement_seconds=entitlement_seconds,
            dispatch_start_guard_seconds=0.25,
            cleanup_slack_seconds=GRACE,
            result_observer=result_observer,
        )
        return outcome, {e.node.name: e for e in task_map.values()}

    return asyncio.run(run())


def test_slow_start_ack_does_not_censor_a_responsive_probe() -> None:
    events: list[tuple[str, str, float]] = []

    def acknowledge(node: Node, _started_at: float) -> bool:
        # A blocking lock wait on the probe's own loop.
        time.sleep(0.8)
        events.append(("ack", node.name, time.monotonic()))
        return True

    async def fake_tcp_probe(node, *, timeout_seconds, **_kwargs):
        events.append(("transport", node.name, time.monotonic()))
        assert timeout_seconds == ENTITLEMENT
        await asyncio.sleep(0.6)
        return _result(node, NodeStatus.ALIVE)

    def observe(node: Node) -> None:
        events.append(("outcome", node.name, time.monotonic()))

    with patch.object(tester_module, "_tcp_probe", fake_tcp_probe):
        outcome = asyncio.run(
            scan_tier3_tcp_candidates(
                [_node("responsive")],
                testing_config={
                    "tcp_timeout": ENTITLEMENT,
                    "tcp_probe_entitlement_seconds": ENTITLEMENT,
                },
                logger=logging.getLogger("test-entitlement-clock"),
                concurrency=1,
                result_observer=observe,
                transport_start_observer=acknowledge,
            )
        )

    # 0.8 s ACK + 0.6 s transport used to read as 1.4 s > 1.25 s: censored.
    (result,) = outcome.nodes
    assert result.status is NodeStatus.ALIVE, result.suspicious_reason
    metadata = outcome.metadata
    assert metadata["hard_censored_count"] == 0
    assert metadata["entitlement_exhausted_count"] == 0
    assert metadata["late_completed_ignored"] == 0
    assert metadata["completed_count"] == 1
    assert metadata["completed_before_deadline"] == 1
    assert metadata["transport_start_acknowledged_count"] == 1
    assert metadata["transport_started_count"] == 1
    # The ACK is still recorded before the transport and before the outcome.
    assert [kind for kind, _name, _at in events] == [
        "ack", "transport", "outcome",
    ]
    times = [at for _kind, _name, at in events]
    assert times == sorted(times)
    # The dispatch-to-transport latency now includes the ACK.
    assert metadata["transport_start_latency_p50_ms"] >= 800 - 1000 * CLOCK_TICK


def test_other_start_acks_of_the_wave_are_not_charged_to_earlier_probes() -> None:
    nodes = [_node(f"sibling-{index}") for index in range(4)]
    ack_returned: dict[str, float] = {}
    transport_started: dict[str, float] = {}

    def acknowledge(node: Node, _started_at: float) -> bool:
        time.sleep(0.35)
        ack_returned[node.name] = time.monotonic()
        return True

    async def probe(node: Node, timeout: float) -> Node:
        transport_started[node.name] = time.monotonic()
        assert timeout == ENTITLEMENT
        await asyncio.sleep(0.6)
        return _result(node, NodeStatus.ALIVE)

    outcome, entitlements = _wave(nodes, acknowledge=acknowledge, probe=probe)

    # Four 0.35 s ACKs run on the loop before any transport of the wave.
    # Charged, they censored the first three probes (the first as 2.0 s).
    assert [node.status for node in outcome.nodes] == [NodeStatus.ALIVE] * 4
    assert outcome.metadata["hard_censored_count"] == 0
    assert outcome.metadata["completed_before_deadline"] == 4
    last_ack = max(ack_returned.values())
    for node in nodes:
        entitlement = entitlements[node.name]
        assert entitlement.start_acknowledged
        assert entitlement.outcome_state == "completed"
        assert ack_returned[node.name] <= transport_started[node.name]
        # The clock starts at the transport, after every ACK of the wave.
        assert last_ack <= entitlement.transport_started_at
        assert entitlement.transport_started_at <= transport_started[node.name]
        assert abs(
            entitlement.transport_deadline
            - entitlement.transport_started_at
            - ENTITLEMENT
        ) < 1e-9
        assert entitlement.completed_at <= entitlement.transport_deadline


def test_full_budget_dead_after_slow_start_ack_keeps_its_verdict() -> None:
    entitlement_seconds = 0.5

    def acknowledge(_node: Node, _started_at: float) -> bool:
        time.sleep(0.8)
        return True

    async def fake_tcp_probe(node, *, timeout_seconds, **_kwargs):
        # The real probe's inner timeout: DEAD exactly at the full budget.
        try:
            await asyncio.wait_for(asyncio.sleep(30), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            return _result(node, NodeStatus.DEAD, "tcp connection timed out")
        raise AssertionError("unreachable")

    with patch.object(tester_module, "_tcp_probe", fake_tcp_probe):
        outcome = asyncio.run(
            scan_tier3_tcp_candidates(
                [_node("full-budget-dead")],
                testing_config={
                    "tcp_timeout": entitlement_seconds,
                    "tcp_probe_entitlement_seconds": entitlement_seconds,
                },
                logger=logging.getLogger("test-entitlement-clock-dead"),
                concurrency=1,
                transport_start_observer=acknowledge,
            )
        )

    (result,) = outcome.nodes
    assert result.status is NodeStatus.DEAD
    assert EXHAUSTED not in str(result.suspicious_reason)
    assert outcome.metadata["hard_censored_count"] == 0
    assert outcome.metadata["completed_count"] == 1


def test_probes_that_overrun_their_transport_entitlement_are_still_censored() -> None:
    # Hard-censor rules are unchanged; only their clock moved to the
    # transport start. The stuck probe is first in the wave, so its clock
    # starts after the second probe's 0.4 s ACK: its hard cap must not be
    # armed before that.
    entitlement_seconds = 0.5
    nodes = [_node("stuck"), _node("late-alive")]
    observed_at: dict[str, float] = {}
    ack_returned: list[float] = []

    def acknowledge(_node: Node, _started_at: float) -> bool:
        time.sleep(0.4)
        ack_returned.append(time.monotonic())
        return True

    async def probe(node: Node, timeout: float) -> Node:
        if node.name == "stuck":
            await asyncio.sleep(30)
        # Responsive, but after its transport deadline and within the cap.
        await asyncio.sleep(timeout + 0.1)
        return _result(node, NodeStatus.ALIVE)

    def observe(node: Node) -> None:
        observed_at[node.name] = time.monotonic()

    outcome, entitlements = _wave(
        nodes,
        acknowledge=acknowledge,
        probe=probe,
        entitlement_seconds=entitlement_seconds,
        result_observer=observe,
    )

    assert all(EXHAUSTED in str(node.suspicious_reason) for node in outcome.nodes)
    assert outcome.metadata["hard_censored_count"] == 2
    assert outcome.metadata["entitlement_exhausted_count"] == 2
    assert outcome.metadata["late_completed_ignored"] == 1
    assert outcome.metadata["completed_count"] == 0
    stuck = entitlements["stuck"]
    late = entitlements["late-alive"]
    assert stuck.outcome_state == late.outcome_state == "hard_censored"
    assert late.late_completion_ignored and not stuck.late_completion_ignored
    assert late.completed_at > late.transport_deadline
    # The stuck probe got its full entitlement plus grace from its own
    # transport start, which came after both ACKs.
    assert len(ack_returned) == 2
    assert stuck.transport_started_at >= max(ack_returned)
    assert observed_at["stuck"] >= stuck.delivery_grace_deadline - CLOCK_TICK
    assert abs(
        stuck.delivery_grace_deadline
        - stuck.transport_started_at
        - (entitlement_seconds + GRACE)
    ) < 1e-9


# --- the Manager lease ledger on the same transport clock ---------------------


SECOND = 1_000_000_000
# What the start ACK passes to mark_started at E = 1.25 s.
TRANSPORT_WAIT = max(2.0, ENTITLEMENT + 0.75)


def _reserve(ledger: Tier3TcpPrestartLeaseLedger, ticket_id: int,
             *, at: float = 0.0) -> None:
    context = Tier3CandidateDispatchContext(
        ticket=Tier3CandidateFeedbackTicket(
            scheduler_generation=1,
            ticket_id=ticket_id,
            arm_key="arm", source_root="root", family="family",
            coarse_key=f"candidate-{ticket_id}", selection_reason="ordinary",
            dispatched_monotonic_ns=int(at * SECOND),
        ),
        node_identity=f"identity-{ticket_id}",
    )
    assert ledger.reserve(
        context, node=_node(f"lease-{ticket_id}"), enqueue_sequence=ticket_id,
        reserved_monotonic_ns=int(at * SECOND), inflight_limit=16,
    )


def _start(ledger: Tier3TcpPrestartLeaseLedger, ticket_id: int,
           *, at: float) -> None:
    assert ledger.claim_start(ticket_id, now_monotonic_ns=int(at * SECOND))
    assert ledger.mark_started(
        ticket_id, now_monotonic_ns=int(at * SECOND),
        transport_wait_after_ns=int(TRANSPORT_WAIT * SECOND),
    )


def test_started_lease_stall_is_timed_on_the_transport_clock() -> None:
    def snapshot(ledger, at):
        return ledger.snapshot(
            now_monotonic_ns=int(at * SECOND), active_wait_after_ns=2 * SECOND,
        )

    # Without a transport report the pre-ACK start stays the anchor.
    unreported = Tier3TcpPrestartLeaseLedger()
    _reserve(unreported, 1)
    _start(unreported, 1, at=0.1)
    assert snapshot(unreported, 2.2).oldest_started_age_ms == 2100
    assert snapshot(unreported, 2.2).stalled_started_count == 1

    # A 0.8 s ACK phase: the transport began at 0.9 s.
    ledger = Tier3TcpPrestartLeaseLedger()
    _reserve(ledger, 1)
    assert not ledger.mark_transport_began(1, now_monotonic_ns=SECOND // 2)
    assert ledger.claim_start(1, now_monotonic_ns=SECOND // 10)
    assert not ledger.mark_transport_began(1, now_monotonic_ns=SECOND // 2)
    assert ledger.mark_started(
        1, now_monotonic_ns=SECOND // 10,
        transport_wait_after_ns=int(TRANSPORT_WAIT * SECOND),
    )
    assert ledger.mark_transport_began(1, now_monotonic_ns=int(0.9 * SECOND))
    # The first report wins: a lease cannot renew its allowance.
    assert not ledger.mark_transport_began(1, now_monotonic_ns=int(1.5 * SECOND))
    assert not ledger.mark_transport_began(99, now_monotonic_ns=SECOND)
    healthy = snapshot(ledger, 2.2)
    assert healthy.oldest_started_age_ms == 1300
    assert healthy.stalled_started_count == 0
    assert healthy.started_count == 1
    assert snapshot(ledger, 2.9).stalled_started_count == 1
    # A report can never move the anchor before the start ACK.
    early = Tier3TcpPrestartLeaseLedger()
    _reserve(early, 1)
    _start(early, 1, at=0.5)
    assert early.mark_transport_began(1, now_monotonic_ns=0)
    assert snapshot(early, 2.5).stalled_started_count == 1


def test_transport_report_does_not_change_reservation_reclaim() -> None:
    # Reclaim stays anchored to reservation time and transport_wait_ns.
    ledgers = [Tier3TcpPrestartLeaseLedger(), Tier3TcpPrestartLeaseLedger()]
    for ledger in ledgers:
        _reserve(ledger, 1)
        _reserve(ledger, 2)
        _start(ledger, 1, at=0.01)
    assert ledgers[1].mark_transport_began(1, now_monotonic_ns=int(0.9 * SECOND))

    def observe(ledger, at):
        state = ledger.snapshot(
            now_monotonic_ns=int(at * SECOND), active_wait_after_ns=2 * SECOND,
        )
        reclaimed = ledger.reclaim_stale(
            now_monotonic_ns=int(at * SECOND), stale_after_ns=SECOND // 2,
            active_wait_after_ns=2 * SECOND,
        )
        return (
            state.late_reserved_count, state.stalled_reserved_count,
            [entry.ticket_id for entry in reclaimed],
        )

    for at in (0.3, 0.75, 1.5, 1.99, 2.0, 2.1):
        assert observe(ledgers[0], at) == observe(ledgers[1], at), at
    assert [ledger.state(2) for ledger in ledgers] == ["reclaimed"] * 2


def test_transport_began_is_reported_once_after_the_wave_acks() -> None:
    nodes = [_node(f"reported-{index}") for index in range(3)]
    ack_returned: dict[str, float] = {}
    reported: list[tuple[str, float]] = []
    transport_started: dict[str, float] = {}

    def acknowledge(node: Node, _started_at: float) -> bool:
        time.sleep(0.2)
        ack_returned[node.name] = time.monotonic()
        return True

    def transport_began(node: Node, began_at: float) -> None:
        reported.append((node.name, began_at))
        # A failing diagnostic observer must never block the transport.
        raise RuntimeError("diagnostic observer failure")

    async def probe(node: Node, timeout: float) -> Node:
        transport_started[node.name] = time.monotonic()
        await asyncio.sleep(0.1)
        return _result(node, NodeStatus.ALIVE)

    outcome, entitlements = _wave(
        nodes, acknowledge=acknowledge, probe=probe,
        transport_began=transport_began,
    )

    assert [node.status for node in outcome.nodes] == [NodeStatus.ALIVE] * 3
    assert sorted(name for name, _at in reported) == sorted(
        node.name for node in nodes
    )
    last_ack = max(ack_returned.values())
    for name, began_at in reported:
        entitlement = entitlements[name]
        # Reported after every ACK of the wave, at or before the clock anchor
        # and the transport itself.
        assert last_ack <= began_at <= entitlement.transport_started_at
        assert entitlement.transport_started_at <= transport_started[name]


def test_slow_wave_acks_do_not_report_healthy_started_leases_as_stalled() -> None:
    """Mirror the source-stream start ACK against a real lease ledger.

    Four 0.3 s ACKs run before any transport of the wave, and every probe
    then takes its full 1.25 s budget. Timed from the pre-ACK start, the first
    leases read 2.45 s and 2.15 s old at their outcome, past the 2.0 s stall
    line, although each transport got exactly its entitlement.
    """

    def run(report_transport: bool):
        ledger = Tier3TcpPrestartLeaseLedger()
        nodes = [_node(f"stall-{index}") for index in range(4)]
        tickets = {node.name: index + 1 for index, node in enumerate(nodes)}
        for node in nodes:
            _reserve(ledger, tickets[node.name], at=time.monotonic())
        at_outcome: dict[str, tuple[int, int]] = {}

        def acknowledge(node: Node, started_at: float) -> bool:
            started_ns = int(round(started_at * SECOND))
            ticket_id = tickets[node.name]
            assert ledger.claim_start(ticket_id, now_monotonic_ns=started_ns)
            time.sleep(0.3)  # commit_handoff under lock contention
            return ledger.mark_started(
                ticket_id, now_monotonic_ns=started_ns,
                transport_wait_after_ns=int(TRANSPORT_WAIT * SECOND),
            )

        def transport_began(node: Node, began_at: float) -> None:
            ledger.mark_transport_began(
                tickets[node.name],
                now_monotonic_ns=int(round(began_at * SECOND)),
            )

        async def probe(node: Node, timeout: float) -> Node:
            try:
                await asyncio.wait_for(asyncio.sleep(30), timeout=timeout)
            except asyncio.TimeoutError:
                state = ledger.snapshot()
                at_outcome[node.name] = (
                    state.stalled_started_count, state.oldest_started_age_ms,
                )
                return _result(node, NodeStatus.DEAD, "tcp connection timed out")
            raise AssertionError("unreachable")

        outcome, _entitlements = _wave(
            nodes, acknowledge=acknowledge, probe=probe,
            transport_began=transport_began if report_transport else None,
        )
        assert [node.status for node in outcome.nodes] == [NodeStatus.DEAD] * 4
        assert outcome.metadata["hard_censored_count"] == 0
        return at_outcome

    reported = run(report_transport=True)
    assert len(reported) == 4
    for name, (stalled, oldest_age_ms) in reported.items():
        assert stalled == 0, (name, reported)
        assert oldest_age_ms < 1000 * TRANSPORT_WAIT, (name, reported)
    # Control: the same wave timed from the pre-ACK start reads as stalled.
    unreported = run(report_transport=False)
    assert max(stalled for stalled, _age in unreported.values()) >= 1, unreported


# --- real refresh_tier3 session ----------------------------------------------


def test_session_slow_commit_handoff_keeps_ack_order_and_conservation(
    tmp_path: Path,
) -> None:
    """A slow scheduler ACK in a real session: no censoring, exact ledgers.

    ``commit_handoff`` takes 0.8 s in every start ACK and each probe returns
    0.4 s before its own transport deadline. Charged to the entitlement, the
    ACK made every such result land after the grace deadline and be
    hard-censored.
    """

    from tests.test_manager import ManagerTests

    fixture = ManagerTests()
    fixture.setUp()
    owner = fixture._minimal_manager(
        tmp_path,
        tier3_enabled=True,
        tier3_overrides={
            "bootstrap_enabled": False,
            "subscription_urls": ["https://opaque.example/sub"],
            "validation_concurrency_min": 4,
            "validation_concurrency_max": 4,
        },
    )
    owner.mark_tier3_core_running(effective_egress_ok=True)
    owner.set_effective_egress_state(True)
    owner.runtime_source_fetch_proxy_provider = (
        lambda _phase: "http://127.0.0.1:19101"
    )
    Path(owner.sources_file).write_text(
        "subscription_urls:\n  - https://opaque.example/sub\n"
        "github_repos: []\ntelegram_channels: []\nlocal_files: []\n",
        encoding="utf-8",
    )
    candidates = [
        Node(name=f"clock-{index}", protocol="ss",
             address=f"clock-{index}.invalid", port=443,
             method="aes-128-gcm", password="fixture", tier="tier3",
             source="https://opaque.example/sub")
        for index in range(8)
    ]
    lock = threading.Lock()
    commits: dict[int, float] = {}
    outcomes: list[dict[str, Any]] = []
    timeouts: list[float] = []
    probed: list[str] = []
    all_probed = threading.Event()
    errors: list[BaseException] = []
    ledgers: list[Tier3TcpPrestartLeaseLedger] = []
    transport_reports: list[tuple[int, bool, float]] = []
    stalled_at_outcome: list[int] = []
    real_commit = Tier3CandidateScheduler.commit_handoff
    real_outcome = Tier3CandidateScheduler.on_tcp_outcome
    real_mark_started = Tier3TcpPrestartLeaseLedger.mark_started
    real_mark_transport_began = Tier3TcpPrestartLeaseLedger.mark_transport_began

    def ticket_id_of(value) -> int:
        value = getattr(value, "ticket", value)
        if isinstance(value, dict):
            return int(value.get("ticket_id", 0) or 0)
        return int(getattr(value, "ticket_id", 0) or 0)

    def slow_commit(self, reservation):
        # The start ACK's scheduler step, slowed as by lock/CPU contention.
        time.sleep(0.8)
        committed = real_commit(self, reservation)
        if committed:
            with lock:
                commits[ticket_id_of(reservation)] = time.monotonic()
        return committed

    def recording_outcome(self, ticket, **kwargs):
        ticket_id = ticket_id_of(ticket)
        with lock:
            outcomes.append({
                "ticket_id": ticket_id,
                "at": time.monotonic(),
                "committed": ticket_id in commits,
                **kwargs,
            })
        return real_outcome(self, ticket, **kwargs)

    def recording_mark_started(self, ticket_id, **kwargs):
        with lock:
            if self not in ledgers:
                ledgers.append(self)
        return real_mark_started(self, ticket_id, **kwargs)

    def recording_mark_transport_began(self, ticket_id, **kwargs):
        marked = real_mark_transport_began(self, ticket_id, **kwargs)
        with lock:
            transport_reports.append(
                (int(ticket_id), bool(marked), time.monotonic())
            )
        return marked

    async def collect(*_args, **kwargs):
        report = CollectionReport(
            source_count=1, fetched_sources=1, node_count=len(candidates),
            sources=[{"type": "subscription_url",
                      "value": "https://opaque.example/sub", "ok": True}],
        )
        kwargs["source_observer"](candidates, report)
        return candidates, report

    async def fake_tcp_probe(node, *, timeout_seconds, **_kwargs):
        with lock:
            timeouts.append(float(timeout_seconds))
        try:
            await asyncio.sleep(max(0.05, float(timeout_seconds) - 0.4))
            # Once every lease is in transport, none of them is stalled,
            # whatever its wave's ACK phase took. (A lease still waiting
            # behind its wave's ACKs is timed from its own start ACK, as
            # before this change.)
            with lock:
                if len({
                    ticket_id for ticket_id, ok, _at in transport_reports if ok
                }) == len(candidates):
                    stalled_at_outcome.extend(
                        ledger.snapshot().stalled_started_count
                        for ledger in ledgers
                    )
        finally:
            with lock:
                probed.append(node.name)
                if len(probed) >= len(candidates):
                    all_probed.set()
        return _result(node, NodeStatus.DEAD, "tcp connection refused")

    def refresh() -> None:
        try:
            owner.refresh_tier3(
                suppress_intervention_alert=True, startup_mode=False,
            )
        except BaseException as exc:
            errors.append(exc)

    try:
        with patch.object(
            manager_module, "collect_nodes_from_sources_with_report", collect,
        ), patch.object(tester_module, "_tcp_probe", fake_tcp_probe), \
                patch.object(Tier3CandidateScheduler, "commit_handoff", slow_commit), \
                patch.object(
                    Tier3CandidateScheduler, "on_tcp_outcome", recording_outcome,
                ), \
                patch.object(
                    Tier3TcpPrestartLeaseLedger, "mark_started",
                    recording_mark_started,
                ), \
                patch.object(
                    Tier3TcpPrestartLeaseLedger, "mark_transport_began",
                    recording_mark_transport_began,
                ):
            worker = threading.Thread(target=refresh)
            worker.start()
            try:
                assert all_probed.wait(60), f"probed {len(probed)}"
                # Let a progress report carry the final counters.
                time.sleep(0.5)
            finally:
                owner.request_stop()
                worker.join(15)
        assert not worker.is_alive()
        assert not errors
        report = dict(owner.tier3_last_collection_report or {})
    finally:
        owner.close(timeout_seconds=3.0)
        fixture.doCleanups()

    assert len(probed) == len(candidates)
    assert timeouts and max(timeouts) - min(timeouts) < 1e-9
    assert report["source_stream_tcp_entitlement_exhausted_count"] == 0
    assert report["source_stream_tcp_scanned_count"] == len(candidates)
    assert report["source_stream_tcp_start_acked_count"] == len(candidates)
    assert report["source_stream_tcp_prestart_started_total"] == len(candidates)
    # reserved = start_acked + start_deferred + cancelled_before_start + open
    assert report["source_stream_tcp_reservation_invariant_ok"] is True
    assert report["source_stream_tcp_reservation_unresolved_count"] == 0
    assert report["source_stream_tcp_reserved_count"] == (
        report["source_stream_tcp_start_acked_count"]
        + report["source_stream_tcp_start_deferred_count"]
        + report["source_stream_tcp_prestart_reclaimed_total"]
        + report["source_stream_tcp_cancelled_before_start_count"]
    )
    # One committed ACK per ticket, each before that ticket's only outcome.
    assert len(commits) == len(candidates)
    per_ticket: dict[int, list[dict[str, Any]]] = {}
    for item in outcomes:
        per_ticket.setdefault(item["ticket_id"], []).append(item)
    # Exactly one on_tcp_outcome per committed ticket, cancelled or not.
    for ticket_id in commits:
        assert len(per_ticket.get(ticket_id, [])) == 1, (
            ticket_id, per_ticket.get(ticket_id),
        )
    # Each started lease was re-timed once, at its transport, after its ACK.
    assert len(ledgers) == 1
    marked = [item for item in transport_reports if item[1]]
    assert sorted(ticket_id for ticket_id, _ok, _at in marked) == sorted(commits)
    for ticket_id, _ok, at in marked:
        assert commits[ticket_id] <= at, (ticket_id, commits[ticket_id], at)
        (terminal_outcome,) = per_ticket[ticket_id]
        assert at <= terminal_outcome["at"], (ticket_id, at, terminal_outcome)
    assert stalled_at_outcome, "no probe finished after every transport began"
    assert set(stalled_at_outcome) == {0}, stalled_at_outcome
    terminal = [item for item in outcomes if not item.get("cancelled")]
    assert sorted(item["ticket_id"] for item in terminal) == sorted(commits)
    for item in terminal:
        assert item["committed"], item
        assert commits[item["ticket_id"]] <= item["at"], item
        assert not item.get("hard_censored"), item
        assert item["responsive"] is False
