"""Differential tests for the scheduler's incremental projections.

Every random operation sequence runs twice: once on the production scheduler
and once on a subclass whose projection reads are bound back to the reference
full scans over ``_tickets`` and ``_cohort_yield_states``. Both must make
identical selections and report identical counters, and the production
projections must equal the reference scans after every step.
"""

from __future__ import annotations

import math
import os
import random
import types
from typing import Any, Callable

import pytest

from models import Node
from tier3_candidate_scheduler import (
    Tier3CandidateFeatureKey,
    Tier3CandidateLookaheadEvidence,
    Tier3CandidateScheduler,
    Tier3ServiceCohortEligibility,
    _COHORT_OUTSTANDING_COUNTERS,
    _COHORT_PROJECTION_BRUTEFORCE_METHODS,
    _CohortYieldState,
    _TICKET_PROJECTION_BRUTEFORCE_METHODS,
)
from tier3_defaults import tier3_tcp_breadth_scaled_limit


PROJECTION_BRUTEFORCE_METHODS = {
    **_TICKET_PROJECTION_BRUTEFORCE_METHODS,
    **_COHORT_PROJECTION_BRUTEFORCE_METHODS,
}

BruteForceProjectionScheduler = type(
    "BruteForceProjectionScheduler",
    (Tier3CandidateScheduler,),
    {
        fast_name: getattr(Tier3CandidateScheduler, reference_name)
        for fast_name, reference_name in PROJECTION_BRUTEFORCE_METHODS.items()
    },
)

# Every step of every sequence runs the full O(tickets x cohorts) projection
# verify, about 25 ms per sequence on a quiet machine and several times that
# under load. Seven injected index bugs were each caught within the first
# 150 seeds, so the default keeps a margin above that and the long soak is
# opt-in: TIER3_SCHEDULER_INDEX_SEQUENCES=40000 for a deep local run.
SEQUENCES = int(os.environ.get("TIER3_SCHEDULER_INDEX_SEQUENCES", "400"))
CHUNKS = 20
TERMINAL_OUTCOMES = (
    "pass",
    "fail",
    "validation_fail",
    "facility_unavailable",
    "cancelled",
    "closed",
    "duplicate",
    "rejected",
    "timeout",
    "right_censored",
    "",
)
VOLATILE_METRICS = frozenset({"last_tick_ms"})


def evidence_for(item: Any, shift: int = 0) -> Tier3CandidateLookaheadEvidence:
    kind = (int(item.node.name.rsplit("-", 1)[-1]) + shift) % 13
    return Tier3CandidateLookaheadEvidence(
        validated_positive=kind in {0, 5},
        validated_reprobe=kind == 1,
        tcp_only=kind in {2, 3},
        observed_nonpositive=kind == 4,
    )


def feature_for(rng: random.Random, l2_count: int) -> Tier3CandidateFeatureKey:
    depth = rng.choices((0, 1, 2, 3, 4), weights=(1, 1, 2, 4, 24))[0]
    l2 = rng.randrange(l2_count)
    leaf = rng.randrange(3)
    # Occasionally give an L2 a second parent: the cold-leaf lane then falls
    # back to its full-sort ordering for ambiguous children.
    l1 = rng.randrange(3) if rng.random() < 0.05 else l2 % 3
    profiles = ("l0", f"l1-{l1}", f"l2-{l2}", f"l3-{l2}-{leaf}")[:depth]
    return Tier3CandidateFeatureKey.build(
        service_family="vless",
        transport=rng.choice(("ws", "tcp")),
        security="tls",
        port_bucket="443",
        address_bucket=f"host:{l2 % 4}",
        host_suffix="example.com",
        service_profiles=profiles,
    )


class Twin:
    """Drive the indexed and the reference scheduler in lockstep."""

    def __init__(self, rng: random.Random, clock: list[float]) -> None:
        self.rng = rng
        self.clock = clock
        kwargs: dict[str, Any] = {
            "lookahead_evidence_provider": evidence_for,
            "max_active_arms_per_family": rng.choice((2, 4, 16)),
            "unknown_exploration_ratio": rng.choice((0.0, 0.15, 0.5)),
        }
        if rng.random() < 0.2:
            kwargs["_enable_legacy_structural_routing_for_test"] = True
        self.sides: tuple[Tier3CandidateScheduler, Tier3CandidateScheduler] = (
            Tier3CandidateScheduler(**kwargs),
            BruteForceProjectionScheduler(**kwargs),
        )
        self.tickets: tuple[dict[int, Any], dict[int, Any]] = ({}, {})
        self.reservations: tuple[dict[int, Any], dict[int, Any]] = ({}, {})
        # The lookahead arm heaps key entries by ``id(candidate)``. Keeping
        # every reserved candidate alive stops a freed address from being
        # reused by a later candidate of the same arm, which would reorder
        # that arm's FIFO differently on each side of the twin.
        self.keepalive: tuple[list[Any], list[Any]] = ([], [])
        self.serial = 0
        self.l2_count = rng.choice((1, 2, 4, 10, 24))
        self.coverage: set[str] = set()
        self.sealed = False
        helpers = rng.choice((1, 4, 16, 64))
        tcp_limit = rng.choice((4, 16, 64))
        self.both(
            lambda side: side.set_credit_envelope(
                helpers,
                tcp_execution_limit=tcp_limit,
            )
        )

    @property
    def fast(self) -> Tier3CandidateScheduler:
        return self.sides[0]

    def both(self, action: Callable[[Tier3CandidateScheduler], Any]) -> Any:
        results = [action(side) for side in self.sides]
        assert results[0] == results[1]
        return results[0]

    def both_indexed(
        self,
        action: Callable[[int, Tier3CandidateScheduler], Any],
    ) -> Any:
        results = [action(index, side) for index, side in enumerate(self.sides)]
        assert results[0] == results[1]
        return results[0]

    def ticket_arg(self, index: int, ticket_id: int) -> Any:
        ticket = self.tickets[index][ticket_id]
        return ticket.to_dict() if ticket_id % 7 == 0 else ticket

    def pick_ticket(self) -> int | None:
        if not self.tickets[0]:
            return None
        ids = sorted(self.tickets[0])
        # Prefer recent tickets; older ones exercise stale/no-op feedback.
        return ids[-1 - min(len(ids) - 1, int(self.rng.expovariate(0.15)))]

    def enqueue(self) -> None:
        for _ in range(self.rng.randint(1, 8)):
            self.serial += 1
            feature = feature_for(self.rng, self.l2_count)
            arm = self.rng.randrange(12)
            root = f"root-{self.rng.randrange(3)}"
            family = self.rng.choice(("vless", "trojan"))
            credit_exempt = self.rng.random() < 0.05
            name = f"n-{self.serial}"
            for side in self.sides:
                side.enqueue(
                    Node(
                        name=name,
                        protocol="vless",
                        address=f"{name}.example.com",
                        port=443,
                        tier="tier3",
                    ),
                    None,
                    arm_key=f"arm-{arm}",
                    source_root=root,
                    family=family,
                    coarse_key=f"coarse-{arm}",
                    feature_key=feature,
                    credit_exempt=credit_exempt,
                )

    def reserve(self) -> None:
        limit = self.rng.randint(1, 16)
        revision = self.rng.randint(0, 5)
        with self.fast._lock:
            if self.fast._stale_terminal_ticket_ids_unlocked(self.clock[0] - 120.0):
                self.coverage.add("terminal_pruned")

        def run(index: int, side: Tier3CandidateScheduler) -> list[tuple]:
            reservations = side.reserve_handoffs(limit, control_revision=revision)
            for reservation in reservations:
                ticket = reservation.ticket
                self.tickets[index][ticket.ticket_id] = ticket
                self.reservations[index][ticket.ticket_id] = reservation
                self.keepalive[index].append(reservation.candidate)
            return [
                (
                    reservation.ticket.ticket_id,
                    reservation.candidate.node.name,
                    reservation.ticket.selection_reason,
                    reservation.ticket.visibility_class,
                    reservation.ticket.evidence_origin,
                    reservation.ticket.reward_scope,
                    reservation.ticket.reward_cohort_key,
                    reservation.ticket.reward_cohort_state,
                    reservation.ticket.unknown_exploration,
                    reservation.ticket.coverage_selected,
                    reservation.control_revision,
                )
                for reservation in reservations
            ]

        if self.both_indexed(run):
            self.coverage.add("reserved")

    def commit(self) -> None:
        open_ids = sorted(self.reservations[0])
        if not open_ids:
            return
        chosen = [
            ticket_id for ticket_id in open_ids if self.rng.random() < 0.8
        ]
        for index in range(2):
            for ticket_id in chosen:
                self.reservations[index].pop(ticket_id, None)
        self.both_indexed(
            lambda index, side: [
                ticket.ticket_id
                for ticket in side.commit_handoffs(
                    [self.tickets[index][ticket_id] for ticket_id in chosen]
                )
            ]
        )

    def cancel(self) -> None:
        open_ids = sorted(self.reservations[0])
        if not open_ids:
            return
        ticket_id = self.rng.choice(open_ids)
        requeue = self.rng.random() < 0.6
        reservations = [self.reservations[index].pop(ticket_id) for index in range(2)]
        self.both_indexed(
            lambda index, side: side.cancel_handoff(
                reservations[index],
                requeue=requeue,
            )
        )

    def requeue_front(self) -> None:
        open_ids = sorted(self.reservations[0])
        if not open_ids:
            return
        chosen = self.rng.sample(open_ids, self.rng.randint(1, min(3, len(open_ids))))
        candidates = [
            [self.reservations[index].pop(ticket_id).candidate for ticket_id in chosen]
            for index in range(2)
        ]
        self.both_indexed(
            lambda index, side: side.requeue_front(candidates[index])
        )

    def tcp_outcome(self) -> None:
        ticket_id = self.pick_ticket()
        if ticket_id is None:
            return
        flags = {
            "responsive": self.rng.random() < 0.45,
            "cancelled": self.rng.random() < 0.06,
            "hard_censored": self.rng.random() < 0.06,
            "validation_required": self.rng.random() < 0.1,
        }
        for index in range(2):
            self.reservations[index].pop(ticket_id, None)
        self.both_indexed(
            lambda index, side: side.on_tcp_outcome(
                self.ticket_arg(index, ticket_id),
                **flags,
            )
        )

    def fast_observation(self) -> None:
        ticket_id = self.pick_ticket()
        if ticket_id is None:
            return
        outcome = self.rng.choice(("pass", "fail", "transient", "right_censored"))
        kwargs = {
            "outcome": outcome,
            "busy_worker_ms": self.rng.randint(0, 4000),
            "right_censored": self.rng.random() < 0.1,
            "facility_failure": self.rng.random() < 0.05,
            "passed": outcome == "pass" and self.rng.random() < 0.8,
        }
        self.both_indexed(
            lambda index, side: side.on_fast_observation(
                self.ticket_arg(index, ticket_id),
                **kwargs,
            )
        )

    def fast_yield(self) -> None:
        ticket_id = self.pick_ticket()
        if ticket_id is None:
            return
        kwargs = {
            "continued": self.rng.random() < 0.9,
            "outcome": self.rng.choice(("", "fail", "transient", "right_censored")),
            "busy_worker_ms": self.rng.randint(0, 4000),
            "right_censored": self.rng.random() < 0.1,
            "facility_failure": self.rng.random() < 0.05,
        }
        self.both_indexed(
            lambda index, side: side.on_fast_yield(
                self.ticket_arg(index, ticket_id),
                **kwargs,
            )
        )

    def lifecycle(self) -> None:
        """Run one plausible start-to-finish path for a recent ticket."""

        ticket_id = self.pick_ticket()
        if ticket_id is None:
            return
        for index in range(2):
            self.reservations[index].pop(ticket_id, None)
        mode = self.rng.choice(("pass", "pass", "fail", "deep", "deep_after_pass"))
        net_new = self.rng.random() < 0.7
        worker_ms = self.rng.randint(200, 3000)
        censored_deep = self.rng.random() < 0.5

        def run(index: int, side: Tier3CandidateScheduler) -> None:
            ticket = self.tickets[index][ticket_id]
            side.commit_handoff(ticket)
            side.on_tcp_outcome(ticket, responsive=True)
            if mode in {"pass", "fail", "deep_after_pass"}:
                passed = mode != "fail"
                side.on_fast_observation(
                    ticket,
                    outcome="pass" if passed else "fail",
                    passed=passed,
                    busy_worker_ms=worker_ms,
                )
            if mode in {"deep", "deep_after_pass"}:
                # A retained DEEP flight stays open when FAST is censored.
                side.on_fast_yield(
                    ticket,
                    busy_worker_ms=worker_ms,
                    right_censored=censored_deep,
                    facility_failure=not censored_deep,
                )
                return
            side.on_terminal(
                ticket,
                passed=mode == "pass",
                outcome=mode,
                busy_worker_ms=worker_ms,
                stage="fast_probe",
            )
            if mode == "pass" and net_new:
                side.on_commit_complete(ticket, net_new=True)

        self.both_indexed(run)

    def terminal(self) -> None:
        ticket_id = self.pick_ticket()
        if ticket_id is None:
            return
        outcome = self.rng.choice(TERMINAL_OUTCOMES)
        kwargs = {
            "passed": outcome == "pass" or self.rng.random() < 0.1,
            "outcome": outcome,
            "busy_worker_ms": self.rng.randint(0, 6000),
            "stage": self.rng.choice(("fast_probe", "fast", "deep", "")),
        }
        for index in range(2):
            self.reservations[index].pop(ticket_id, None)
        self.both_indexed(
            lambda index, side: side.on_terminal(
                self.ticket_arg(index, ticket_id),
                **kwargs,
            )
        )

    def commit_complete(self) -> None:
        ticket_id = self.pick_ticket()
        if ticket_id is None:
            return
        net_new = self.rng.random() < 0.6
        direct = self.rng.random() < 0.2
        for index in range(2):
            self.reservations[index].pop(ticket_id, None)
        if direct:
            self.both_indexed(
                lambda index, side: side.on_net_new_commit(
                    self.ticket_arg(index, ticket_id)
                )
            )
        else:
            self.both_indexed(
                lambda index, side: side.on_commit_complete(
                    self.ticket_arg(index, ticket_id),
                    net_new=net_new,
                )
            )

    def record_feedback(self) -> None:
        arm = self.rng.randrange(12)
        responsive = self.rng.random() < 0.5
        passed = self.rng.random() < 0.3
        self.both(
            lambda side: side.record_feedback(
                f"arm-{arm}",
                responsive=responsive,
                passed=passed,
            )
        )

    def reconcile(self) -> None:
        active = sorted(self.fast.active_handoff_ticket_ids())
        authoritative = [ticket_id for ticket_id in active if self.rng.random() < 0.6]
        stale_after = self.rng.choice((0.05, 0.5, 5.0))
        reclaimed, closed = self.both(
            lambda side: side.reconcile_tcp_execution_leases(
                authoritative,
                stale_after_seconds=stale_after,
            )
        )
        for ticket_id in (*reclaimed, *closed):
            for index in range(2):
                self.reservations[index].pop(ticket_id, None)
        if reclaimed or closed:
            self.coverage.add("reconciled")

    def release(self) -> None:
        self.both(lambda side: side.release_outstanding())
        for index in range(2):
            self.reservations[index].clear()

    def drain_all(self) -> None:
        """Flush the backlog and seal what was in flight, ending the sequence.

        ``drain_all`` clears the root/family rotation, but arms survive it.
        A candidate that later re-enters an existing arm (a new enqueue, a
        cancel/requeue or a reconcile reclaim) is not re-registered in its
        root's family rotation, and ``_rotate_after_bucket_selection_unlocked``
        then spins forever once another family of that root is registered.
        That holds with or without the incremental projections, so the
        harness uses a drain only as the owner does: a final flush.
        """

        self.both(
            lambda side: [candidate.node.name for candidate in side.drain_all()]
        )
        self.both(lambda side: side.release_outstanding())
        for index in range(2):
            self.reservations[index].clear()
        self.sealed = True

    def envelope(self) -> None:
        helpers = self.rng.choice((1, 2, 4, 8, 16, 32, 64, 96))
        window = self.rng.choice((None, 16, 64))
        tcp_limit = self.rng.choice((None, 4, 16, 64))
        free_fast = self.rng.choice((None, 0, 4, 16))
        ceiling = self.rng.choice((None, 64, 128))
        # Opt-in TCP breadth widens the cold-L2 training limit on both sides.
        breadth = self.rng.choice((None, None, 1.0, 1.5, 2.0, 4.0))
        self.both(
            lambda side: side.set_credit_envelope(
                helpers,
                handoff_window=window,
                tcp_execution_limit=tcp_limit,
                free_fast_slots=free_fast,
                active_helper_ceiling=ceiling,
            )
        )
        if breadth is not None:
            self.both(lambda side: side.set_cold_l2_training_breadth(breadth))

    def evidence(self) -> None:
        shift = self.rng.randrange(13)
        # Production installs the provider once, before any candidate is
        # queued. Swapping it with queued inventory reclassifies candidates
        # under their per-cohort visibility counts, which the scheduler (with
        # or without these projections) does not reconcile.
        if self.fast.size():
            return

        def provider(item: Any) -> Tier3CandidateLookaheadEvidence:
            return evidence_for(item, shift)

        self.both(lambda side: side.set_lookahead_evidence_provider(provider))

    def seed_prior(self) -> None:
        features = [feature_for(self.rng, self.l2_count) for _ in range(3)]
        self.both(lambda side: side.seed_service_pool_prior(features))

    def advance(self) -> None:
        self.clock[0] += self.rng.choice((0.01, 0.2, 1.0, 3.0, 20.0, 61.0, 125.0))

    def compare_metrics(self) -> None:
        metrics = [
            {
                key: value
                for key, value in side.quick_metrics().items()
                if key not in VOLATILE_METRICS
            }
            for side in self.sides
        ]
        assert metrics[0] == metrics[1]

    def check(self, *, snapshot: bool = True) -> None:
        self.fast.debug_verify_ticket_indices()
        self.both(lambda side: side.pending_handoff_count())
        self.both(lambda side: side.size())
        self.both(lambda side: side.active_handoff_ticket_ids())
        if snapshot:
            self.both(lambda side: side.supply_snapshot())
        fast = self.fast
        if fast._cold_l2_training_deferred_total:
            self.coverage.add("cold_l2_deferred")
        if fast._cold_l2_training_borrowed_total:
            self.coverage.add("cold_l2_borrowed")
        if fast._terminal_ticket_ids:
            self.coverage.add("terminal_retained")
        if fast._confirmation_fast_pending_ticket_count:
            self.coverage.add("confirmation_fast_pending")
        if fast._confirmation_deep_pending_ticket_count:
            self.coverage.add("confirmation_deep_pending")
        if fast._replay_discovery_open_count:
            self.coverage.add("replay_discovery_open")
        if fast._feedback_only_ticket_count:
            self.coverage.add("feedback_only")
        if fast._reserved_discovery_handoffs:
            self.coverage.add("parent_discovery_reserved")
        if fast._cold_l2_training_current_limit_unlocked() > 4:
            self.coverage.add("cold_l2_limit_above_floor")
        if (
            fast._cold_l2_training_breadth > 1.0
            and fast._cold_l2_training_outstanding_total_unlocked()
            and fast._cold_l2_training_current_limit_unlocked()
            > min(
                max(1, fast._active_helper_count),
                max(
                    4,
                    math.ceil(
                        max(1, fast._active_helper_count)
                        / max(1, fast._cold_l2_training_cohort_count_unlocked())
                    ),
                ),
            )
        ):
            self.coverage.add("cold_l2_limit_widened_by_breadth")
        with fast._lock:
            if fast._leaf_confirmation_open_cohorts_unlocked():
                self.coverage.add("leaf_confirmation_open")
            if fast._parent_discovery_open_cohorts_unlocked():
                self.coverage.add("parent_discovery_open")
            if fast._production_service_cohorts_with_inventory_unlocked():
                self.coverage.add("production_service_cohort")
            if fast._structural_production_limit_unlocked() > 0:
                self.coverage.add("structural_production_limit")
            for name in _COHORT_OUTSTANDING_COUNTERS:
                if fast._cohort_counter_total_unlocked(name):
                    self.coverage.add(name)
            _parent_by_child, ambiguous = fast._cold_leaf_parent_links_unlocked()
            if ambiguous:
                self.coverage.add("ambiguous_cold_leaf_parent")


OPERATIONS: tuple[tuple[str, float], ...] = (
    ("enqueue", 14.0),
    ("reserve", 16.0),
    ("commit", 12.0),
    ("cancel", 3.0),
    ("requeue_front", 1.5),
    ("tcp_outcome", 14.0),
    ("fast_observation", 7.0),
    ("fast_yield", 6.0),
    ("terminal", 10.0),
    ("commit_complete", 6.0),
    ("lifecycle", 8.0),
    ("record_feedback", 1.0),
    ("reconcile", 1.5),
    ("release", 0.4),
    ("drain_all", 0.3),
    ("envelope", 1.5),
    ("seed_prior", 0.5),
    ("evidence", 0.5),
    ("advance", 6.0),
    ("compare_metrics", 2.0),
)


def run_sequence(seed: int, steps: int, coverage: set[str]) -> None:
    rng = random.Random(seed)
    clock = [1_000.0 + seed]
    names = [name for name, _weight in OPERATIONS]
    weights = [weight for _name, weight in OPERATIONS]
    import tier3_candidate_scheduler as module

    original_monotonic = module.time.monotonic
    original_monotonic_ns = module.time.monotonic_ns
    module.time.monotonic = lambda: clock[0]
    module.time.monotonic_ns = lambda: int(clock[0] * 1_000_000_000)
    try:
        twin = Twin(rng, clock)
        twin.enqueue()
        for step in range(steps):
            operation = rng.choices(names, weights=weights)[0]
            getattr(twin, operation)()
            # Projections are verified after every step; the full supply
            # snapshot comparison runs on every third step.
            twin.check(snapshot=step % 3 == 0)
            if twin.sealed:
                break
        twin.compare_metrics()
        twin.both(lambda side: side.release_outstanding())
        twin.check()
        assert twin.fast._tickets == {}
        assert twin.fast._ticket_index_signatures == {}
        assert twin.fast._cold_l2_outstanding == {}
        assert twin.fast._cold_l2_outstanding_total == 0
        coverage.update(twin.coverage)
    finally:
        module.time.monotonic = original_monotonic
        module.time.monotonic_ns = original_monotonic_ns


def test_reference_scheduler_reads_only_bruteforce_projections() -> None:
    for fast_name, reference_name in PROJECTION_BRUTEFORCE_METHODS.items():
        assert getattr(Tier3CandidateScheduler, fast_name) is not getattr(
            Tier3CandidateScheduler,
            reference_name,
        )
        assert getattr(BruteForceProjectionScheduler, fast_name) is getattr(
            Tier3CandidateScheduler,
            reference_name,
        )


COVERAGE: set[str] = set()


@pytest.mark.parametrize("chunk", range(CHUNKS))
def test_random_sequences_match_bruteforce_reference(chunk: int) -> None:
    per_chunk = max(1, SEQUENCES // CHUNKS)
    for offset in range(per_chunk):
        seed = chunk * 100_003 + offset
        run_sequence(seed, steps=24 + (seed % 5) * 12, coverage=COVERAGE)


def test_random_sequences_exercised_every_projection() -> None:
    expected = {
        "reserved",
        "cold_l2_deferred",
        "cold_l2_borrowed",
        "terminal_retained",
        "terminal_pruned",
        "confirmation_fast_pending",
        "confirmation_deep_pending",
        "feedback_only",
        "parent_discovery_reserved",
        "reconciled",
        "cold_l2_limit_above_floor",
        "cold_l2_limit_widened_by_breadth",
        "leaf_confirmation_open",
        "parent_discovery_open",
        "production_service_cohort",
        "structural_production_limit",
        "discovery_outstanding_total",
        "confirmation_outstanding_total",
        "probation_outstanding_total",
        "production_outstanding_total",
        "ambiguous_cold_leaf_parent",
    }
    # Runs its own sequences when selected alone or with a small budget.
    seed = 900_000
    while not expected <= COVERAGE and seed < 900_600:
        run_sequence(seed, steps=96, coverage=COVERAGE)
        seed += 1
    assert expected <= COVERAGE, sorted(expected - COVERAGE)


def test_consistency_check_reports_drift() -> None:
    scheduler = Tier3CandidateScheduler()
    scheduler.set_credit_envelope(16, handoff_window=16)
    for index in range(8):
        scheduler.enqueue(
            Node(
                name=f"drift-{index}",
                protocol="vless",
                address=f"drift-{index}.example.com",
                port=443,
                tier="tier3",
            ),
            None,
            arm_key=f"arm-{index}",
            source_root="root",
            family="vless",
            coarse_key=f"coarse-{index}",
            feature_key=Tier3CandidateFeatureKey.build(
                service_family="vless",
                transport="ws",
                security="tls",
                port_bucket="443",
                address_bucket="host:a",
                host_suffix="example.com",
                service_profiles=("l0", "l1", f"l2-{index % 2}", f"l3-{index}"),
            ),
        )
    reservations = scheduler.reserve_handoffs(4)
    assert reservations
    scheduler.debug_verify_ticket_indices()
    with scheduler._lock:
        cohort_key = next(iter(scheduler._cold_l2_outstanding))
        scheduler._cold_l2_outstanding[cohort_key] += 1
    with pytest.raises(AssertionError, match="cold_l2_outstanding"):
        scheduler.debug_verify_ticket_indices()


def test_cold_l2_deferral_counts_match_reference_under_concentrated_inventory() -> None:
    results = []
    for scheduler_type in (Tier3CandidateScheduler, BruteForceProjectionScheduler):
        scheduler = scheduler_type()
        scheduler.set_credit_envelope(64, tcp_execution_limit=256)
        for index in range(600):
            l2 = 0 if index % 5 else index % 17
            scheduler.enqueue(
                Node(
                    name=f"dense-{index}",
                    protocol="vless",
                    address=f"dense-{index}.example.com",
                    port=443,
                    tier="tier3",
                ),
                None,
                arm_key=f"arm-{index % 40}",
                source_root=f"root-{index % 3}",
                family="vless",
                coarse_key=f"coarse-{index % 40}",
                feature_key=Tier3CandidateFeatureKey.build(
                    service_family="vless",
                    transport="ws",
                    security="tls",
                    port_bucket="443",
                    address_bucket="host:a",
                    host_suffix="example.com",
                    service_profiles=(
                        "l0",
                        "l1",
                        f"l2-{l2}",
                        f"l3-{l2}-{index % 4}",
                    ),
                ),
            )
        trace = []
        for _round in range(12):
            reservations = scheduler.reserve_handoffs(32)
            trace.append([item.candidate.node.name for item in reservations])
            scheduler.commit_handoffs(reservations)
            for position, reservation in enumerate(reservations):
                scheduler.on_tcp_outcome(
                    reservation.ticket,
                    responsive=position % 3 == 0,
                )
        metrics = scheduler.quick_metrics()
        results.append(
            (
                trace,
                metrics["cold_l2_training_deferred_total"],
                metrics["cold_l2_training_borrowed_total"],
                metrics["cold_l2_training_outstanding_count"],
                metrics["cold_l2_training_cohort_count"],
                metrics["cold_l2_training_current_limit"],
                metrics["lookahead_examined_total"],
            )
        )
        if scheduler_type is Tier3CandidateScheduler:
            scheduler.debug_verify_ticket_indices()
    assert results[0] == results[1]
    assert results[0][1] > 0


def replay_trial_trace(
    scheduler_type: type[Tier3CandidateScheduler],
    observe: Callable[[Tier3CandidateScheduler], None],
) -> list[Any]:
    scheduler = scheduler_type(
        max_active_arms_per_family=1,
        lookahead_evidence_provider=lambda item: Tier3CandidateLookaheadEvidence(
            validated_positive=item.node.name.startswith("replay"),
            tcp_only=item.node.name.startswith("peer"),
        ),
    )
    scheduler.set_credit_envelope(16, handoff_window=16)

    def add(name: str, leaf: str = "precise-leaf", source: str = "opaque") -> None:
        scheduler.enqueue(
            Node(
                name=name,
                protocol="vless",
                address=f"{name}.example.com",
                port=443,
                tier="tier3",
            ),
            None,
            arm_key=name,
            coarse_key=name,
            source_root=source,
            family="opaque-family",
            feature_key=Tier3CandidateFeatureKey.build(
                service_family="opaque-family",
                transport="ws",
                security="tls",
                port_bucket="443",
                address_bucket="host:a",
                host_suffix="example.com",
                service_profiles=("l0", "l1", "l2", leaf),
            ),
        )

    def complete(ticket: Any, passed: bool) -> None:
        scheduler.on_tcp_outcome(ticket, responsive=True)
        observe(scheduler)
        scheduler.on_fast_observation(
            ticket,
            passed=passed,
            busy_worker_ms=1000,
            outcome="pass" if passed else "fail",
        )
        observe(scheduler)
        scheduler.on_terminal(
            ticket,
            passed=passed,
            outcome="pass" if passed else "fail",
            stage="fast_probe",
        )
        observe(scheduler)
        if passed:
            scheduler.on_commit_complete(ticket, net_new=True)
            observe(scheduler)

    add("replay-donor")
    donor = scheduler.reserve_handoffs(1)[0]
    scheduler.commit_handoff(donor)
    complete(donor.ticket, passed=True)
    for index in range(64):
        add(f"ordinary-{index}", leaf=f"other-{index}", source=f"opaque-{index % 3}")
    for index in range(8):
        add(f"peer-{index}")
    trace: list[Any] = []
    for _round in range(4):
        batch = scheduler.reserve_handoffs(16)
        observe(scheduler)
        trace.append(
            [
                (
                    item.ticket.ticket_id,
                    item.candidate.node.name,
                    item.ticket.selection_reason,
                )
                for item in batch
            ]
        )
        for item in batch:
            scheduler.commit_handoff(item)
            observe(scheduler)
            if item.ticket.selection_reason == "replay_leaf_discovery":
                complete(item.ticket, passed=True)
            else:
                scheduler.on_tcp_outcome(item.ticket, responsive=False)
                observe(scheduler)
    trace.append(
        sorted(
            (key, value)
            for key, value in scheduler.quick_metrics().items()
            if key not in VOLATILE_METRICS
        )
    )
    scheduler.release_outstanding()
    observe(scheduler)
    return trace


def test_replay_leaf_trial_matches_reference() -> None:
    import tier3_candidate_scheduler as module

    replay_open_seen: list[int] = []

    def verify(scheduler: Tier3CandidateScheduler) -> None:
        scheduler.debug_verify_ticket_indices()
        replay_open_seen.append(scheduler._replay_discovery_open_count)

    original_monotonic = module.time.monotonic
    original_monotonic_ns = module.time.monotonic_ns
    module.time.monotonic = lambda: 5_000.0
    module.time.monotonic_ns = lambda: 5_000_000_000_000
    try:
        indexed = replay_trial_trace(Tier3CandidateScheduler, verify)
        reference = replay_trial_trace(
            BruteForceProjectionScheduler,
            lambda _scheduler: None,
        )
    finally:
        module.time.monotonic = original_monotonic
        module.time.monotonic_ns = original_monotonic_ns
    assert indexed == reference
    assert any(
        reason == "replay_leaf_discovery"
        for batch in indexed[:-1]
        for _ticket_id, _name, reason in batch
    )
    assert max(replay_open_seen) > 0


def observed_cohort_scheduler() -> Tier3CandidateScheduler:
    scheduler = Tier3CandidateScheduler()
    scheduler.set_credit_envelope(16, handoff_window=16)
    for index in range(24):
        scheduler.enqueue(
            Node(
                name=f"cohort-{index}",
                protocol="vless",
                address=f"cohort-{index}.example.com",
                port=443,
                tier="tier3",
            ),
            None,
            arm_key=f"arm-{index % 6}",
            source_root=f"root-{index % 2}",
            family="vless",
            coarse_key=f"coarse-{index % 6}",
            feature_key=Tier3CandidateFeatureKey.build(
                service_family="vless",
                transport="ws",
                security="tls",
                port_bucket="443",
                address_bucket="host:a",
                host_suffix="example.com",
                service_profiles=(
                    "l0",
                    "l1",
                    f"l2-{index % 3}",
                    f"l3-{index % 3}-{index % 2}",
                ),
            ),
        )
    for reservation in scheduler.reserve_handoffs(8):
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
        scheduler.on_fast_observation(
            reservation.ticket,
            outcome="pass",
            passed=True,
            busy_worker_ms=1000,
        )
    assert scheduler._cohort_yield_states
    scheduler.debug_verify_ticket_indices()
    return scheduler


def test_cohort_index_rebuilds_after_direct_state_insertion() -> None:
    scheduler = observed_cohort_scheduler()
    with scheduler._lock:
        state = _CohortYieldState(scope="service")
        state.discovery_outstanding_total = 2
        scheduler._cohort_yield_states["service:inserted"] = state
        mismatches = scheduler._debug_cohort_index_mismatches_unlocked()
        # Only the bypassed registration is reported; the read rebuilt it.
        assert [item.split(":", 1)[0] for item in mismatches] == [
            "cohort_index_registered"
        ]
        assert scheduler._debug_cohort_index_mismatches_unlocked() == []
        assert scheduler._cohort_counter_total_unlocked(
            "discovery_outstanding_total"
        ) == scheduler._cohort_counter_total_bruteforce_unlocked(
            "discovery_outstanding_total"
        )
        # An unregistered state changing eligibility also forces a rebuild.
        stray = _CohortYieldState(scope="service")
        scheduler._cohort_yield_states["service:stray"] = stray
        scheduler._set_service_eligibility_unlocked(
            stray,
            Tier3ServiceCohortEligibility.PARENT_DISCOVERY,
            reason="test",
        )
        assert [
            key
            for key, _state in scheduler._cohort_items_with_eligibility_unlocked(
                (Tier3ServiceCohortEligibility.PARENT_DISCOVERY,)
            )
        ][-1:] == ["service:stray"]
        assert scheduler._debug_cohort_index_mismatches_unlocked() == []
    scheduler.release_outstanding()


def _eligibility_items(
    scheduler: Tier3CandidateScheduler,
    eligibilities: tuple[Tier3ServiceCohortEligibility, ...],
    *,
    reference: bool = False,
) -> list[tuple[str, int]]:
    items = (
        scheduler._cohort_items_with_eligibility_bruteforce_unlocked(eligibilities)
        if reference
        else scheduler._cohort_items_with_eligibility_unlocked(eligibilities)
    )
    return [
        (cohort_key, id(state))
        for cohort_key, state in items
        if state.eligibility in eligibilities
    ]


def _rewrite_replace(scheduler: Tier3CandidateScheduler, key: str, state: Any) -> None:
    scheduler._cohort_yield_states[key] = state


def _rewrite_delete_and_append(
    scheduler: Tier3CandidateScheduler, key: str, state: Any
) -> None:
    del scheduler._cohort_yield_states[key]
    scheduler._cohort_yield_states[key] = state


def _rewrite_pop_and_append(
    scheduler: Tier3CandidateScheduler, key: str, state: Any
) -> None:
    scheduler._cohort_yield_states.pop(key)
    scheduler._cohort_yield_states[key] = state


def _rewrite_update(scheduler: Tier3CandidateScheduler, key: str, state: Any) -> None:
    scheduler._cohort_yield_states.update({key: state})


def _rewrite_in_place_or(
    scheduler: Tier3CandidateScheduler, key: str, state: Any
) -> None:
    states = scheduler._cohort_yield_states
    states |= {key: state}
    assert states is scheduler._cohort_yield_states


def _rewrite_clear_and_restore(
    scheduler: Tier3CandidateScheduler, key: str, state: Any
) -> None:
    states = scheduler._cohort_yield_states
    saved = dict(states)
    saved[key] = state
    states.clear()
    for cohort_key, cohort_state in saved.items():
        states[cohort_key] = cohort_state


@pytest.mark.parametrize(
    "rewrite",
    [
        _rewrite_replace,
        _rewrite_delete_and_append,
        _rewrite_pop_and_append,
        _rewrite_update,
        _rewrite_in_place_or,
        _rewrite_clear_and_restore,
    ],
)
def test_cohort_index_rebuilds_after_a_state_rewrite(rewrite: Callable) -> None:
    """A new state object at an existing key must not vanish from the index.

    Before the map counted rewrites, the eligibility buckets kept describing
    the old object, so an indexed read returned a subset of the reference
    and silently skipped the new state.
    """

    scheduler = observed_cohort_scheduler()
    discovery = (Tier3ServiceCohortEligibility.PARENT_DISCOVERY,)
    with scheduler._lock:
        cohort_key, old_state = next(
            (key, state)
            for key, state in scheduler._cohort_yield_states.items()
            if state.eligibility not in discovery and state.scope == "service"
        )
        replacement = _CohortYieldState(scope="similar")
        replacement.eligibility = Tier3ServiceCohortEligibility.PARENT_DISCOVERY
        replacement.discovery_outstanding_total = 3
        size = len(scheduler._cohort_yield_states)
        rewrite(scheduler, cohort_key, replacement)
        assert len(scheduler._cohort_yield_states) == size
        assert scheduler._cohort_yield_states[cohort_key] is replacement
        # The first read already sees the replacement: no subset.
        items = _eligibility_items(scheduler, discovery)
        assert (cohort_key, id(replacement)) in items
        assert items == _eligibility_items(scheduler, discovery, reference=True)
        assert (cohort_key, id(old_state)) not in _eligibility_items(
            scheduler, tuple(Tier3ServiceCohortEligibility)
        )
        assert [key for key, _state in scheduler._similar_cohort_items_unlocked()] == [
            key
            for key, state in scheduler._cohort_yield_states.items()
            if state.scope == "similar"
        ]
        assert scheduler._cohort_counter_total_unlocked(
            "discovery_outstanding_total", "similar"
        ) == scheduler._cohort_counter_total_bruteforce_unlocked(
            "discovery_outstanding_total", "similar"
        )
        assert scheduler._debug_cohort_index_mismatches_unlocked() == []
    scheduler.debug_verify_ticket_indices()
    scheduler.release_outstanding()


def test_cohort_index_flags_a_pending_rewrite_before_rebuilding() -> None:
    scheduler = observed_cohort_scheduler()
    with scheduler._lock:
        cohort_key, state = next(iter(scheduler._cohort_yield_states.items()))
        # Storing the same object again is not a rewrite.
        scheduler._cohort_yield_states[cohort_key] = state
        assert scheduler._debug_cohort_index_mismatches_unlocked() == []
        scheduler._cohort_yield_states[cohort_key] = _CohortYieldState(
            scope=state.scope
        )
        mismatches = scheduler._debug_cohort_index_mismatches_unlocked()
        assert [item.split(":", 1)[0] for item in mismatches] == [
            "cohort_index_registered"
        ]
        assert scheduler._debug_cohort_index_mismatches_unlocked() == []
    scheduler.release_outstanding()


# --- cold-L2 policy hooks ------------------------------------------------------


def _tcp_only_cold_diversity(scheduler: Tier3CandidateScheduler) -> dict[str, int]:
    """The audit harness hook behind --tcp-only-cold-diversity, same logic."""

    counts: dict[str, int] = {}
    for state in scheduler._tickets.values():
        if state.phase not in {"reserved", "tcp_active"}:
            continue
        ticket = state.ticket
        if ticket.evidence_origin not in {"cold_unknown", "cold_discovery"}:
            continue
        if len(ticket.service_cohort_keys) >= 3:
            key = ticket.service_cohort_keys[2]
            counts[key] = counts.get(key, 0) + 1
    return counts


def _fair_share_limit(
    scheduler: Tier3CandidateScheduler,
    outstanding: Any,
) -> int:
    """The cold-training-width experiment's limit hook (floor 1), same logic."""

    visible = {
        key
        for key, count in scheduler._cold_unknown_l2_queued_inventory.items()
        if count > 0
    }
    visible.update(key for key, count in outstanding.items() if count > 0)
    helpers = max(1, scheduler._active_helper_count)
    return min(helpers, max(1, math.ceil(helpers / max(1, len(visible)))))


class PreCounterColdL2Scheduler(Tier3CandidateScheduler):
    """Cold-L2 reads written as they were before the incremental counters.

    Every read evaluates the two policy hooks from scratch, so replacing a
    hook on this class shows what that replacement did before the counters.
    The limit hook is the old full scan, over the breadth-scaled capacity.
    """

    def _cold_l2_training_outstanding_total_unlocked(self) -> int:
        return sum(self._cold_l2_training_outstanding_unlocked().values())

    def _cold_l2_training_cohort_count_unlocked(self) -> int:
        return len(
            {
                *self._cold_unknown_l2_queued_inventory,
                *self._cold_l2_training_outstanding_unlocked(),
            }
        )

    def _cold_l2_training_current_limit_unlocked(
        self,
        outstanding: Any = None,
    ) -> int:
        del outstanding
        return self._cold_l2_training_limit_unlocked(
            self._cold_l2_training_outstanding_unlocked()
        )

    def _cold_l2_training_limit_unlocked(self, outstanding: Any) -> int:
        visible_cohorts = {
            cohort_key
            for cohort_key, count in self._cold_unknown_l2_queued_inventory.items()
            if int(count) > 0
        }
        visible_cohorts.update(
            cohort_key for cohort_key, count in outstanding.items() if int(count) > 0
        )
        helper_count = tier3_tcp_breadth_scaled_limit(
            max(1, int(self._active_helper_count)),
            self._cold_l2_training_breadth,
        )
        cohort_count = max(1, len(visible_cohorts))
        return min(
            helper_count,
            max(4, int(math.ceil(helper_count / float(cohort_count)))),
        )


_HOOKS = {
    "_cold_l2_training_outstanding_unlocked": _tcp_only_cold_diversity,
    "_cold_l2_training_limit_unlocked": _fair_share_limit,
}
_COLD_L2_METRICS = (
    "cold_l2_training_outstanding_count",
    "cold_l2_training_outstanding_high_water",
    "cold_l2_training_cohort_count",
    "cold_l2_training_current_limit",
    "cold_l2_training_deferred_total",
    "cold_l2_training_borrowed_total",
)


def _concentrated_cold_trace(
    scheduler_type: type[Tier3CandidateScheduler],
    hooks: tuple[str, ...],
    *,
    on_instance: bool,
    breadth: float = 1.0,
    helper_ceiling: int | None = None,
) -> tuple[list[list[str]], dict[str, Any]]:
    if hooks and not on_instance:
        scheduler_type = type(
            "Hooked" + scheduler_type.__name__,
            (scheduler_type,),
            {name: _HOOKS[name] for name in hooks},
        )
    scheduler = scheduler_type()
    if on_instance:
        for name in hooks:
            setattr(scheduler, name, types.MethodType(_HOOKS[name], scheduler))
    scheduler.set_credit_envelope(
        64,
        tcp_execution_limit=256,
        active_helper_ceiling=helper_ceiling,
    )
    scheduler.set_cold_l2_training_breadth(breadth)
    for index in range(600):
        l2 = 0 if index % 5 else index % 17
        scheduler.enqueue(
            Node(
                name=f"hook-{index}",
                protocol="vless",
                address=f"hook-{index}.example.com",
                port=443,
                tier="tier3",
            ),
            None,
            arm_key=f"arm-{index % 40}",
            source_root=f"root-{index % 3}",
            family="vless",
            coarse_key=f"coarse-{index % 40}",
            feature_key=Tier3CandidateFeatureKey.build(
                service_family="vless",
                transport="ws",
                security="tls",
                port_bucket="443",
                address_bucket="host:a",
                host_suffix="example.com",
                service_profiles=("l0", "l1", f"l2-{l2}", f"l3-{l2}-{index % 4}"),
            ),
        )
    trace = []
    for _round in range(10):
        reservations = scheduler.reserve_handoffs(32)
        trace.append([item.candidate.node.name for item in reservations])
        scheduler.commit_handoffs(reservations)
        for reservation in reservations:
            # Responsive tickets become feedback_only, which the harness hook
            # leaves out of its count.
            scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
    metrics = scheduler.quick_metrics()
    scheduler.release_outstanding()
    return trace, {key: metrics[key] for key in _COLD_L2_METRICS}


@pytest.mark.parametrize("on_instance", [False, True])
@pytest.mark.parametrize(
    "hooks",
    [
        (),
        ("_cold_l2_training_outstanding_unlocked",),
        ("_cold_l2_training_limit_unlocked",),
        (
            "_cold_l2_training_outstanding_unlocked",
            "_cold_l2_training_limit_unlocked",
        ),
    ],
)
def test_replaced_cold_l2_hooks_govern_selection_and_reports(
    hooks: tuple[str, ...],
    on_instance: bool,
) -> None:
    """Audit harnesses replace the cold-L2 hooks; the O(1) path honours them."""

    actual = _concentrated_cold_trace(
        Tier3CandidateScheduler, hooks, on_instance=on_instance
    )
    expected = _concentrated_cold_trace(
        PreCounterColdL2Scheduler, hooks, on_instance=on_instance
    )
    assert actual == expected
    if hooks:
        # The replacement really changes the policy, so the check has teeth.
        unhooked = _concentrated_cold_trace(
            Tier3CandidateScheduler, (), on_instance=False
        )
        assert actual[0] != unhooked[0]
        assert actual[1] != unhooked[1]


@pytest.mark.parametrize("on_instance", [False, True])
def test_a_replaced_limit_hook_caps_every_cold_lane(on_instance: bool) -> None:
    def one_per_l2(scheduler: Tier3CandidateScheduler, outstanding: Any) -> int:
        del scheduler, outstanding
        return 1

    scheduler_type: type[Tier3CandidateScheduler] = Tier3CandidateScheduler
    if not on_instance:
        scheduler_type = type(
            "OnePerL2Scheduler",
            (Tier3CandidateScheduler,),
            {"_cold_l2_training_limit_unlocked": one_per_l2},
        )
    scheduler = scheduler_type(unknown_exploration_ratio=0.15)
    if on_instance:
        scheduler._cold_l2_training_limit_unlocked = types.MethodType(
            one_per_l2, scheduler
        )
    scheduler.set_credit_envelope(16, handoff_window=32, tcp_execution_limit=32)
    for l2 in range(4):
        for index in range(20):
            name = f"cap-{l2}-{index}"
            scheduler.enqueue(
                Node(
                    name=name,
                    protocol="vless",
                    address=f"{name}.example.com",
                    port=443,
                    tier="tier3",
                ),
                None,
                arm_key=f"arm-{l2}-{index}",
                source_root="root",
                family="vless",
                coarse_key=f"coarse-{l2}-{index}",
                feature_key=Tier3CandidateFeatureKey.build(
                    service_family="vless",
                    transport="ws",
                    security="tls",
                    port_bucket="443",
                    address_bucket="host:a",
                    host_suffix="example.com",
                    service_profiles=("l0", "l1", f"l2-{l2}", f"l3-{l2}"),
                ),
            )
    wave = scheduler.reserve_handoffs(32)
    for reservation in wave:
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
    assert sorted(
        reservation.ticket.service_cohort_keys[2] for reservation in wave
    ) == [f"service:l2-{l2}" for l2 in range(4)]
    assert scheduler.reserve_handoffs(32) == []
    metrics = scheduler.quick_metrics()
    assert metrics["cold_l2_training_current_limit"] == 1
    assert metrics["cold_l2_training_deferred_total"] > 0
    scheduler.release_outstanding()


@pytest.mark.parametrize("breadth", [1.5, 2.0])
def test_cold_l2_counters_match_the_pre_counter_reads_under_breadth(
    breadth: float,
) -> None:
    # 64 identities over 17 visible L2 cohorts: the fair share binds.
    actual = _concentrated_cold_trace(
        Tier3CandidateScheduler,
        (),
        on_instance=False,
        breadth=breadth,
        helper_ceiling=64,
    )
    assert actual == _concentrated_cold_trace(
        PreCounterColdL2Scheduler,
        (),
        on_instance=False,
        breadth=breadth,
        helper_ceiling=64,
    )
    default = _concentrated_cold_trace(
        Tier3CandidateScheduler, (), on_instance=False, helper_ceiling=64
    )
    # ceil(64 / 17) = 4 at the default; ceil(96 / 17) = 6, ceil(128 / 17) = 8.
    assert default[1]["cold_l2_training_current_limit"] == 4
    assert actual[1]["cold_l2_training_current_limit"] == {1.5: 6, 2.0: 8}[breadth]
    assert actual[0] != default[0]


def test_cohort_consistency_check_reports_drift() -> None:
    scheduler = observed_cohort_scheduler()
    with scheduler._lock:
        cohort_key, state = next(iter(scheduler._cohort_yield_states.items()))
        scheduler._cohort_keys_by_eligibility[state.eligibility].pop(cohort_key)
    with pytest.raises(AssertionError, match="cohort_eligibility_buckets"):
        scheduler.debug_verify_ticket_indices()
    scheduler.release_outstanding()
