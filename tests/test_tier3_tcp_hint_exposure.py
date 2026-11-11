"""Weak-history allocation tests; synthetic yields are not network acceptance."""

import pytest

from tests.test_tier3_candidate_scheduler import candidate, feature
from tests.test_tier3_exact_inventory_index import complete
from tier3_candidate_scheduler import Tier3CandidateLookaheadEvidence, Tier3CandidateScheduler


class GreedyTcpCoverageOracle(Tier3CandidateScheduler):
    """v45 coverage-only oracle; real reservations and feedback stay unchanged."""

    def _dequeue_from_arm(self, arm, **kwargs):
        if (
            kwargs.get("selection_reason") == "reward"
            and kwargs.get("preferred_candidate") is None
            and not kwargs.get("required_reward_cohort_key")
            and not kwargs.get("required_service_cohort_key")
        ):
            kwargs["preferred_candidate"] = next(
                (item for item in arm.items
                 if self._candidate_reward_grant_and_scope(item)[1] != "none"),
                None,
            )
        return super()._dequeue_from_arm(arm, **kwargs)

    def _take_visibility_coverage_entry_unlocked(
        self, lanes, *, provisionally_covered, provisionally_selected_arms,
    ):
        lanes = list(lanes)
        if "tcp_only" in lanes and "unknown" in lanes:
            lanes.remove("tcp_only")
            lanes.insert(lanes.index("unknown"), "tcp_only")
        entry, examined = super()._take_visibility_coverage_entry_unlocked(
            lanes, provisionally_covered=provisionally_covered,
            provisionally_selected_arms=provisionally_selected_arms,
        )
        if entry is None and "tcp_only" in lanes:
            entry, extra = self._take_visibility_entry_unlocked(
                ["tcp_only"], provisionally_selected_arms=provisionally_selected_arms,
            )
            examined += extra
        return entry, examined


class GlobalTcpHintLimiter(GreedyTcpCoverageOracle):
    """v43 policy oracle; the cross-leaf limiter is no longer production."""

    def _tcp_hint_coverage_open_unlocked(self):
        if not self._tcp_hint_yield_limited:
            return True
        return (
            list(self._visibility_handoff_history)[-15:].count("tcp_only")
            + self._visibility_reserved_open.get("tcp_only", 0)
        ) < 1

    def _cold_leaf_preferred_visibility_unlocked(self, cohort_key):
        if self._tcp_hint_yield_limited:
            return "unknown", self._cohort_unknown_queued_arms.get(cohort_key, {})
        return super()._cold_leaf_preferred_visibility_unlocked(cohort_key)


def make_scheduler(cls=Tier3CandidateScheduler):
    scheduler = cls(
        max_active_arms_per_family=1,
        lookahead_evidence_provider=lambda item: Tier3CandidateLookaheadEvidence(
            tcp_only=item.node.name.startswith("tcp-"),
        ),
    )
    scheduler.set_credit_envelope(16, handoff_window=16, tcp_execution_limit=16)
    return scheduler


def add(scheduler, name, *, root="opaque", leaf="leaf", shared=False):
    scheduler.enqueue(
        candidate(name), None, arm_key="shared" if shared else name,
        coarse_key="shared" if shared else name,
        source_root=root, family="opaque-family",
        feature_key=feature(
            family="opaque-family", service_profiles=("l0", "l1", "l2", leaf),
        ),
    )


def fast_censor(scheduler, reservation, *, milliseconds=3000, retained=False):
    scheduler.commit_handoff(reservation)
    scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
    scheduler.on_fast_observation(
        reservation.ticket, right_censored=True, busy_worker_ms=milliseconds,
    )
    if retained:
        scheduler.on_fast_yield(reservation.ticket, right_censored=True)
    else:
        scheduler.on_terminal(reservation.ticket, outcome="right_censored", stage="canonical_deep")


def observe_unproductive_history(scheduler, *, retained=False):
    for index in range(16):
        add(scheduler, f"tcp-training-{index}", leaf=f"training-{index}")
    batch = scheduler.reserve_handoffs(16)
    assert len(batch) == 16
    for reservation in batch:
        fast_censor(scheduler, reservation, retained=retained)
    assert scheduler.quick_metrics()["tcp_hint_policy"]["shadow_limited"]
    return batch


@pytest.mark.parametrize("roots", [1, 2, 5, 17])
def test_unproductive_other_leaves_do_not_reduce_weak_only_root_coverage(roots):
    scheduler = make_scheduler()
    observe_unproductive_history(scheduler)
    for prefix in ("unknown", "tcp"):
        for index in range(512):
            add(scheduler, f"{prefix}-{index}", root=f"{prefix}-renamed-{index % roots}", leaf=f"other-{index}")
    starts = []
    for _ in range(12):
        batch = scheduler.reserve_handoffs(16)
        assert len(batch) == 16
        assert sum(r.ticket.visibility_class == "tcp_only" for r in batch) <= 4
        assert sum(r.ticket.coverage_selected for r in batch) <= 4
        assert sum(r.ticket.unknown_exploration for r in batch) >= 3
        for reservation in batch:
            scheduler.commit_handoff(reservation)
            starts.append(reservation.ticket.visibility_class)
            scheduler.on_tcp_outcome(reservation.ticket, responsive=False)
    assert starts.count("tcp_only") >= 12
    metrics = scheduler.quick_metrics()
    assert metrics["visibility_tcp_only_net_new_total"] == 0
    assert metrics["tcp_hint_policy"]["writes_negative"] is False
    assert metrics["tcp_hint_policy"]["mode"] == "shadow_only"
    assert not metrics["tcp_hint_policy"]["limited"]
    assert metrics["tcp_hint_policy"]["coverage_limit"] == 4
    assert metrics["tcp_hint_policy"]["limited_count"] == 0
    scheduler.release_outstanding()


def test_censor_exposure_is_neutral_and_only_observed_once():
    scheduler = make_scheduler()
    batch = observe_unproductive_history(scheduler, retained=True)
    before = scheduler.quick_metrics()["tcp_hint_policy"]
    assert before["window_worker_ms"] == 48000
    assert before["shadow_limited_count"] == 1
    for reservation in reversed(batch):
        scheduler.on_fast_observation(reservation.ticket, right_censored=True, busy_worker_ms=3000)
        scheduler.on_terminal(reservation.ticket, outcome="right_censored", stage="canonical_deep")
        scheduler.on_terminal(reservation.ticket, outcome="cancelled")
    metrics = scheduler.quick_metrics()
    assert metrics["tcp_hint_policy"] == before
    assert metrics["visibility_tcp_only_resolved_total"] == 0
    assert metrics["reward_service_sample_completion_count"] == 0
    scheduler.release_outstanding()
    assert scheduler.quick_metrics()["active_ticket_count"] == 0


@pytest.mark.parametrize("neutral", ["facility", "cancelled"])
def test_non_transport_terminal_cannot_limit_tcp_hint(neutral):
    scheduler = make_scheduler()
    for index in range(32):
        add(scheduler, f"tcp-{index}")
    for _ in range(2):
        for reservation in scheduler.reserve_handoffs(16):
            scheduler.commit_handoff(reservation)
            scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
            scheduler.on_terminal(reservation.ticket, outcome=neutral, busy_worker_ms=3000)
    policy = scheduler.quick_metrics()["tcp_hint_policy"]
    assert not policy["shadow_limited"]
    assert policy["window_fast_observations"] == 0
    scheduler.release_outstanding()


def test_limited_history_does_not_idle_when_it_is_the_only_inventory():
    scheduler = make_scheduler()
    observe_unproductive_history(scheduler)
    for index in range(64):
        add(scheduler, f"tcp-only-{index}")
    for _ in range(4):
        batch = scheduler.reserve_handoffs(16)
        assert len(batch) == 16
        for reservation in batch:
            scheduler.commit_handoff(reservation)
            scheduler.on_tcp_outcome(reservation.ticket, responsive=False)
    assert scheduler.quick_metrics()["selected_total"] == 80
    scheduler.release_outstanding()


def test_late_deep_net_new_restores_preference_but_pass_or_duplicate_does_not():
    scheduler = make_scheduler()
    batch = observe_unproductive_history(scheduler, retained=True)
    first, second = batch[:2]
    scheduler.on_terminal(first.ticket, passed=True, outcome="pass", stage="canonical_deep")
    assert scheduler.quick_metrics()["tcp_hint_policy"]["shadow_limited"]
    scheduler.on_commit_complete(first.ticket, net_new=False)
    assert scheduler.quick_metrics()["tcp_hint_policy"]["shadow_limited"]
    scheduler.on_terminal(second.ticket, passed=True, outcome="pass", stage="canonical_deep")
    scheduler.on_commit_complete(second.ticket, net_new=True)
    scheduler.on_commit_complete(second.ticket, net_new=True)
    policy = scheduler.quick_metrics()["tcp_hint_policy"]
    assert not policy["shadow_limited"]
    assert policy["shadow_restored_by_net_new_count"] == 1
    assert policy["restored_by_net_new_count"] == 0
    scheduler.release_outstanding()


@pytest.mark.parametrize("shared", [False, True])
def test_unrelated_leaf_censors_do_not_reverse_local_confirmation_evidence(shared, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("tier3_candidate_scheduler.time.monotonic", lambda: now[0])
    monkeypatch.setattr("tier3_candidate_scheduler.time.monotonic_ns", lambda: int(now[0] * 1e9))
    scheduler = make_scheduler()
    observe_unproductive_history(scheduler)
    add(scheduler, "seed", shared=shared)
    complete(scheduler, scheduler.reserve_handoffs(1)[0], passed=True)
    for index in range(8):
        add(scheduler, f"tcp-peer-{index}", shared=shared)
        add(scheduler, f"unknown-peer-{index}", shared=shared)
    confirmation, _ = scheduler._pop_leaf_confirmation_candidate_unlocked(ignore_credits=False)
    assert confirmation.node.name == "tcp-peer-0"
    ticket = confirmation.dispatch_ticket
    scheduler.commit_handoff(ticket)
    scheduler.on_tcp_outcome(ticket, responsive=True)
    scheduler.on_terminal(ticket, passed=True, outcome="pass", stage="fast_probe", busy_worker_ms=1000)
    scheduler.on_commit_complete(ticket, net_new=True)
    assert scheduler.quick_metrics()["reward_service_probation_cohort_count"] == 1
    now[0] += 16.0  # The independent exact grant expires, not cohort evidence.
    selected, _ = scheduler._pop_service_reward_candidate_unlocked(ignore_credits=False)
    assert selected is not None
    assert selected.node.name == "tcp-peer-1"
    assert selected.dispatch_ticket.reward_scope == "service"
    assert scheduler.cancel_handoff(selected.dispatch_ticket, requeue=True)
    retried, _ = scheduler._pop_service_reward_candidate_unlocked(ignore_credits=False)
    assert retried.node.name == selected.node.name
    scheduler.release_outstanding()


def test_one_thousand_cancels_outcomes_and_refills_preserve_limited_lane_ownership():
    scheduler = make_scheduler()
    observe_unproductive_history(scheduler)
    for prefix in ("unknown", "tcp"):
        for index in range(1200):
            add(scheduler, f"{prefix}-{index}", root=f"root-{index % 5}", leaf=f"leaf-{index}")
    transactions = 0
    for epoch in range(64):
        batch = scheduler.reserve_handoffs(16)
        assert len(batch) == 16
        for index, reservation in enumerate(reversed(batch)):
            transactions += 1
            mode = (epoch + index) % 6
            if mode == 0:
                assert scheduler.cancel_handoff(reservation, requeue=True)
                assert not scheduler.cancel_handoff(reservation, requeue=True)
                continue
            scheduler.commit_handoff(reservation)
            if mode <= 2:
                scheduler.on_tcp_outcome(reservation.ticket, responsive=False, hard_censored=mode == 2)
            elif mode == 3:
                fast_censor(scheduler, reservation)
            elif mode == 4:
                scheduler.on_tcp_outcome(reservation.ticket, responsive=True)
                scheduler.on_terminal(reservation.ticket, passed=True, outcome="pass", stage="fast_probe")
                scheduler.on_commit_complete(reservation.ticket, net_new=False)
            else:
                scheduler.on_terminal(reservation.ticket, outcome="cancelled")
            scheduler.on_terminal(reservation.ticket, outcome="cancelled")
    assert transactions == 1024
    scheduler.release_outstanding()
    metrics = scheduler.quick_metrics()
    assert metrics["active_ticket_count"] == 0
    assert metrics["tcp_handoff_lease_count"] == 0
    assert metrics["feedback_only_ticket_count"] == 0
    assert scheduler.pending_handoff_count() == 0


def test_global_limit_can_save_exposure_only_under_uniformly_bad_history_assumption():
    def run(cls):
        scheduler = make_scheduler(cls)
        observe_unproductive_history(scheduler)
        for prefix in ("unknown", "tcp"):
            for index in range(1600):
                add(scheduler, f"{prefix}-{index}", root=f"root-{index % 5}", leaf=f"unique-{prefix}-{index}")
        weak_exposure_ms = cold_fast = net_new = 0
        for _ in range(64):
            batch = scheduler.reserve_handoffs(16)
            assert len(batch) == 16
            for reservation in batch:
                scheduler.commit_handoff(reservation)
            # Deliver the same deterministic transport outcomes only after
            # the whole asynchronous wave has actually started.
            for reservation in reversed(batch):
                if reservation.ticket.visibility_class == "tcp_only":
                    weak_exposure_ms += 3000
                    fast_censor(scheduler, reservation)
                    continue
                index = int(reservation.candidate.node.name.rsplit("-", 1)[1])
                responsive = index % 5 == 0
                scheduler.on_tcp_outcome(reservation.ticket, responsive=responsive)
                if responsive:
                    cold_fast += 1
                    passed = index % 100 == 0
                    complete(scheduler, reservation, passed=passed)
                    net_new += int(passed)
        assert scheduler.quick_metrics()["selected_total"] == 1040
        scheduler.release_outstanding()
        assert scheduler.quick_metrics()["active_ticket_count"] == 0
        return weak_exposure_ms, cold_fast, net_new

    ordinary = run(GreedyTcpCoverageOracle)
    limited = run(GlobalTcpHintLimiter)
    current = run(Tier3CandidateScheduler)
    print({"v45_greedy_coverage": ordinary, "v43_global_limit": limited, "debt_coverage": current})
    assert limited[0] <= ordinary[0] * 0.30
    assert limited[1] >= ordinary[1] * 1.15
    assert limited[2] >= ordinary[2]
    assert current[0] == 0
    assert current[1] >= ordinary[1] * 1.15
    assert current[2] >= ordinary[2]


@pytest.mark.parametrize("shared", [False, True])
def test_same_input_useful_leaf_is_not_suppressed_by_other_leaf_exposure(shared, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("tier3_candidate_scheduler.time.monotonic", lambda: now[0])
    monkeypatch.setattr("tier3_candidate_scheduler.time.monotonic_ns", lambda: int(now[0] * 1e9))

    def run(cls):
        scheduler = make_scheduler(cls)
        observe_unproductive_history(scheduler)
        add(scheduler, "seed", shared=shared)
        complete(scheduler, scheduler.reserve_handoffs(1)[0], passed=True)
        for index in range(256):
            add(scheduler, f"unknown-peer-{index}", shared=shared)
        for index in range(32):
            add(scheduler, f"tcp-peer-{index}", shared=shared)
        selected = []
        net_new = 0
        for _ in range(4):
            batch = scheduler.reserve_handoffs(16)
            assert len(batch) == 16
            for reservation in batch:
                scheduler.commit_handoff(reservation)
                selected.append((reservation.candidate.node.name, reservation.ticket.selection_reason))
            # No future feedback is visible while the asynchronous wave is
            # reserved. TCP-only is useful in this leaf, unlike training.
            for reservation in reversed(batch):
                if reservation.ticket.visibility_class == "tcp_only":
                    complete(scheduler, reservation, passed=True)
                    net_new += 1
                else:
                    scheduler.on_tcp_outcome(reservation.ticket, responsive=False)
        assert scheduler.quick_metrics()["selected_total"] == 81
        scheduler.release_outstanding()
        assert scheduler.quick_metrics()["active_ticket_count"] == 0
        return net_new, selected

    limited, old_trace = run(GlobalTcpHintLimiter)
    restored, new_trace = run(Tier3CandidateScheduler)
    print({"v43_net_new": limited, "restored_net_new": restored})
    assert new_trace[0][0].startswith("tcp-peer-")
    assert old_trace[0][0].startswith("unknown-peer-")
    assert restored > limited
