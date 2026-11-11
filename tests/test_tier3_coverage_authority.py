"""Coverage repays root/family debt, not a weak-cache allocation entitlement."""

import pytest

from tests.test_tier3_tcp_hint_exposure import add, make_scheduler
from tests.test_tier3_candidate_scheduler import candidate, feature
from tests.test_tier3_exact_inventory_index import complete


def finish_dead(scheduler, batch):
    for reservation in batch:
        scheduler.commit_handoff(reservation)
        scheduler.on_tcp_outcome(reservation.ticket, responsive=False)


@pytest.mark.parametrize("roots", [1, 2, 5, 17])
def test_served_bucket_does_not_fill_unused_coverage_with_weak_cache(roots):
    scheduler = make_scheduler()
    for index in range(2048):
        add(scheduler, f"unknown-{index}", root=f"opaque-{index % roots}", leaf=f"new-{index}")
        add(scheduler, f"tcp-{index}", root=f"opaque-{index % roots}", leaf=f"old-{index}")

    starts = []
    for _ in range(8):
        batch = scheduler.reserve_handoffs(16)
        assert len(batch) == 16
        assert sum(item.ticket.unknown_exploration for item in batch) >= 3
        assert sum(item.ticket.coverage_selected for item in batch) <= 4
        starts.extend(item.ticket for item in batch)
        finish_dead(scheduler, batch)

    # Every root has unknown inventory: coverage can repay exactly the same
    # debt without preferentially sending merely TCP-responsive cache rows.
    assert all(ticket.visibility_class == "unknown" for ticket in starts)
    assert {ticket.source_root for ticket in starts} == {
        f"opaque-{index}" for index in range(roots)
    }
    assert scheduler.quick_metrics()["selected_total"] == len(starts)
    scheduler.release_outstanding()


def test_weak_only_new_root_still_receives_real_coverage():
    scheduler = make_scheduler()
    for index in range(128):
        add(scheduler, f"unknown-{index}", root="served", leaf=f"new-{index}")
    finish_dead(scheduler, scheduler.reserve_handoffs(16))
    add(scheduler, "tcp-late", root="late", leaf="late-leaf")

    batch = scheduler.reserve_handoffs(16)
    covered = [item for item in batch if item.candidate.node.name == "tcp-late"]
    assert len(covered) == 1
    assert covered[0].ticket.coverage_selected
    assert covered[0].ticket.visibility_class == "tcp_only"
    finish_dead(scheduler, batch)
    scheduler.release_outstanding()


def test_coverage_cancel_restores_order_without_class_or_fairness_side_effects():
    scheduler = make_scheduler()
    for index in range(32):
        add(scheduler, f"unknown-{index}", leaf=f"new-{index}")
        add(scheduler, f"tcp-{index}", leaf=f"old-{index}")
    batch = scheduler.reserve_handoffs(4)
    before = [item.candidate.node.name for item in batch]
    for item in reversed(batch):
        assert scheduler.cancel_handoff(item, requeue=True)
        assert not scheduler.cancel_handoff(item, requeue=True)
    assert scheduler.quick_metrics()["selected_total"] == 0
    retried = scheduler.reserve_handoffs(4)
    assert [item.candidate.node.name for item in retried] == before
    assert all(item.ticket.visibility_class == "unknown" for item in retried)
    finish_dead(scheduler, retried)
    scheduler.release_outstanding()
    assert scheduler.quick_metrics()["active_ticket_count"] == 0


def test_exact_transport_preference_cannot_borrow_another_grant_or_feature():
    scheduler = make_scheduler()

    def enqueue(name, *, arm="exact", leaf="matching"):
        scheduler.enqueue(
            candidate(name), None, arm_key=arm, coarse_key="shared-coarse",
            source_root="opaque", family="opaque-family",
            feature_key=feature(
                family="opaque-family", service_profiles=("l0", "l1", "l2", leaf),
            ),
        )

    enqueue("seed")
    complete(scheduler, scheduler.reserve_handoffs(1)[0], passed=True)
    enqueue("unknown-first")
    enqueue("tcp-wrong-feature", leaf="different")
    enqueue("tcp-wrong-grant", arm="different")
    enqueue("tcp-matching-first")
    enqueue("tcp-matching-second")
    selected, _ = scheduler._pop_global_matching_candidate_unlocked(
        require_exact_reward=True,
    )
    assert selected.node.name == "tcp-matching-first"
    assert selected.dispatch_ticket.reward_scope == "exact"
    before = scheduler.quick_metrics()["selected_total"]
    assert scheduler.cancel_handoff(selected.dispatch_ticket, requeue=True)
    retried, _ = scheduler._pop_global_matching_candidate_unlocked(
        require_exact_reward=True,
    )
    assert retried.node.name == "tcp-matching-first"
    assert scheduler.quick_metrics()["selected_total"] == before
    scheduler.release_outstanding()
