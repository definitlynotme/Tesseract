from __future__ import annotations

import asyncio

import pytest

from manager import (
    Tier3CandidateDispatchContext,
    Tier3TcpPrestartLeaseLedger,
    _Tier3TcpTransportEntitlementSelection,
)
from models import Node, NodeStatus
from tester import Tier3TcpProbeEntitlement, _run_tcp_probe_entitlement
from tier3_candidate_scheduler import Tier3CandidateFeedbackTicket


SECOND = 1_000_000_000


def reserve(ledger, ticket_id, *, at=0.0):
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
        context,
        node=Node(name=f"candidate-{ticket_id}", protocol="ss",
                  address="fixture.invalid", port=443, tier="tier3"),
        enqueue_sequence=ticket_id,
        reserved_monotonic_ns=int(at * SECOND),
        inflight_limit=16,
    )


def start(ledger, ticket_id, *, at=0.01, wait_seconds=2.0):
    assert ledger.claim_start(ticket_id, now_monotonic_ns=int(at * SECOND))
    assert ledger.mark_started(
        ticket_id, now_monotonic_ns=int(at * SECOND),
        transport_wait_after_ns=int(wait_seconds * SECOND),
    )


def reclaim(ledger, at, *, active_wait=2.0):
    return ledger.reclaim_stale(
        now_monotonic_ns=int(at * SECOND),
        stale_after_ns=SECOND // 2,
        active_wait_after_ns=int(active_wait * SECOND),
    )


@pytest.mark.parametrize("started_count", [8, 14])
def test_normal_partial_tcp_wave_keeps_queued_leases_and_capacity(started_count):
    ledger = Tier3TcpPrestartLeaseLedger()
    for ticket_id in range(1, 17):
        reserve(ledger, ticket_id)
    for ticket_id in range(1, started_count + 1):
        start(ledger, ticket_id)

    # The production warning had 8-14 active probes and 2-8 ordinary waiters.
    # Their 1.25 s transport entitlement outlives the old 0.5 s watchdog.
    assert reclaim(ledger, 0.75) == ()
    assert reclaim(ledger, 1.25) == ()
    assert ledger.available_capacity(16) == 0
    state = ledger.snapshot(now_monotonic_ns=int(1.25 * SECOND))
    assert state.active_count == 16
    assert state.reserved_count == 16 - started_count
    assert state.reclaimed_total == 0
    assert state.late_reserved_count == 16 - started_count
    assert state.stalled_reserved_count == 0

    for ticket_id in range(1, started_count + 1):
        ledger.resolve(ticket_id)
    assert reclaim(ledger, 1.30) == ()
    for ticket_id in range(started_count + 1, 17):
        start(ledger, ticket_id, at=1.31)
    assert ledger.snapshot().started_total == 16
    assert ledger.snapshot().reclaimed_total == 0


def test_idle_queue_without_any_start_ack_still_reclaims_at_half_second():
    ledger = Tier3TcpPrestartLeaseLedger()
    reserve(ledger, 1)
    assert reclaim(ledger, 0.49) == ()
    assert [item.ticket_id for item in reclaim(ledger, 0.5)] == [1]
    assert ledger.available_capacity(16) == 16
    assert ledger.consume_tombstone(1)
    assert not ledger.consume_tombstone(1)


def test_short_wave_between_watchdog_samples_has_bounded_dispatch_grace():
    ledger = Tier3TcpPrestartLeaseLedger()
    reserve(ledger, 1)
    reserve(ledger, 2)
    start(ledger, 1, at=0.30)
    ledger.resolve(1)

    assert reclaim(ledger, 0.5) == ()
    assert [item.ticket_id for item in reclaim(ledger, 0.8)] == [2]


def test_lost_queue_item_is_not_renewed_forever_by_new_transport_acks():
    ledger = Tier3TcpPrestartLeaseLedger()
    reserve(ledger, 1)
    reserve(ledger, 2)
    start(ledger, 1)
    assert reclaim(ledger, 0.75) == ()
    ledger.resolve(1)

    reserve(ledger, 3, at=1.0)
    start(ledger, 3, at=1.8)
    assert reclaim(ledger, 1.99) == ()
    assert [item.ticket_id for item in reclaim(ledger, 2.0)] == [2]
    assert ledger.state(3) == "started"
    assert not ledger.claim_start(2, now_monotonic_ns=2 * SECOND)


def test_active_transport_keeps_its_budget_when_controller_shortens_timeout():
    ledger = Tier3TcpPrestartLeaseLedger()
    reserve(ledger, 1)
    reserve(ledger, 2)
    start(ledger, 1, wait_seconds=3.0)

    pending = ledger.snapshot(
        now_monotonic_ns=int(2.5 * SECOND), active_wait_after_ns=2 * SECOND,
    )
    assert pending.oldest_started_age_ms == 2490
    assert pending.stalled_started_count == 0
    assert reclaim(ledger, 2.5, active_wait=2.0) == ()
    assert ledger.snapshot(
        now_monotonic_ns=int(3.02 * SECOND), active_wait_after_ns=2 * SECOND,
    ).stalled_started_count == 1
    ledger.resolve(1)
    assert reclaim(ledger, 2.75, active_wait=2.0) == ()
    assert [item.ticket_id for item in reclaim(ledger, 3.0)] == [2]


def test_claimed_transport_still_protects_waiters_until_finite_wave_bound():
    ledger = Tier3TcpPrestartLeaseLedger()
    reserve(ledger, 1)
    reserve(ledger, 2)
    assert ledger.claim_start(1, now_monotonic_ns=10)

    assert reclaim(ledger, 0.75) == ()
    assert [item.ticket_id for item in reclaim(ledger, 2.0)] == [2]
    assert ledger.state(1) == "start_claimed"


def test_stalled_snapshot_and_reclamation_share_the_protected_deadline():
    ledger = Tier3TcpPrestartLeaseLedger()
    reserve(ledger, 1)
    reserve(ledger, 2)
    start(ledger, 1)

    def snapshot(at):
        return ledger.snapshot(
            now_monotonic_ns=int(at * SECOND),
            active_wait_after_ns=2 * SECOND,
        )

    assert snapshot(0.75).late_reserved_count == 1
    assert snapshot(0.75).stalled_reserved_count == 0
    assert reclaim(ledger, 0.75) == ()
    assert snapshot(2.0).stalled_reserved_count == 1
    assert [entry.ticket_id for entry in reclaim(ledger, 2.0)] == [2]
    # Current diagnosis recovers although historical counters retain evidence.
    assert snapshot(2.1).stalled_reserved_count == 0
    assert snapshot(2.1).reclaimed_total == 1


def test_snapshot_does_not_persist_wait_protection_or_change_reclamation():
    ledgers = [Tier3TcpPrestartLeaseLedger(), Tier3TcpPrestartLeaseLedger()]
    for ledger in ledgers:
        reserve(ledger, 1)
        reserve(ledger, 2)
        start(ledger, 1)

    observed = ledgers[0].snapshot(
        now_monotonic_ns=int(0.75 * SECOND), active_wait_after_ns=2 * SECOND,
    )
    assert observed.late_reserved_count == 1
    assert observed.stalled_reserved_count == 0
    for ledger in ledgers:
        ledger.resolve(1)
        assert [entry.ticket_id for entry in reclaim(ledger, 0.8)] == [2]


def test_probe_and_start_ack_keep_selection_when_pi_changes_between_callbacks():
    ledger = Tier3TcpPrestartLeaseLedger()
    reserve(ledger, 1)
    reserve(ledger, 2)
    node = Node(name="selected", protocol="ss", address="fixture.invalid",
                port=443, tier="tier3")
    current_budget = [(41, 3.0)]
    selected = _Tier3TcpTransportEntitlementSelection(
        lambda _node, _at: current_budget[0],
    )
    observed_timeouts = []

    def provider(start_node, started_at):
        result = selected.select(start_node, started_at)
        # Model the PI thread updating shared state after provider selection.
        current_budget[0] = (42, 1.25)
        return result

    def acknowledge(start_node, started_at):
        assert current_budget[0] == (42, 1.25)
        chosen = selected.take(start_node, started_at)
        assert chosen == (41, 3.0)
        assert selected.take(start_node, started_at) is None
        assert ledger.claim_start(1, now_monotonic_ns=0)
        return ledger.mark_started(
            1, now_monotonic_ns=0,
            transport_wait_after_ns=int(max(2.0, chosen[1] + 0.75) * SECOND),
        )

    async def run():
        entitlement = Tier3TcpProbeEntitlement(node=node)

        async def probe(timeout):
            observed_timeouts.append(timeout)
            updated = node.clone()
            updated.status = NodeStatus.DEAD
            return updated

        await _run_tcp_probe_entitlement(
            entitlement,
            semaphore=asyncio.Semaphore(1),
            entitlement_seconds=1.25,
            cleanup_slack_seconds=0.25,
            cancel_event=None,
            probe_factory=probe,
            transport_entitlement_provider=provider,
            transport_start_observer=acknowledge,
        )
        return entitlement

    entitlement = asyncio.run(run())
    assert observed_timeouts == [3.0]
    assert entitlement.envelope_revision == 41
    assert entitlement.entitlement_seconds == 3.0
    assert reclaim(ledger, 2.5, active_wait=2.0) == ()
    assert [entry.ticket_id for entry in reclaim(ledger, 3.75)] == [2]
