"""Cold discovery must not repeatedly search cache-only inventory."""

from collections import Counter
from unittest.mock import patch

import pytest

from tests.test_tier3_candidate_scheduler import candidate, feature
from tests.test_tier3_exact_inventory_index import complete
from tier3_candidate_scheduler import (
    Tier3CandidateLookaheadEvidence,
    Tier3CandidateScheduler,
)


def populated(*, shared_leaf=False, unknown=False, roots=5):
    scheduler = Tier3CandidateScheduler(
        max_active_arms_per_family=4,
        lookahead_evidence_provider=lambda item: Tier3CandidateLookaheadEvidence(
            tcp_only=item.node.name.startswith("cached-"),
        ),
    )
    scheduler.set_credit_envelope(16, handoff_window=16)
    for index in range(1024 + int(unknown)):
        cached = index < 1024
        name = f"cached-{index}" if cached else "new-candidate"
        leaf = "shared" if shared_leaf else f"leaf-{index}"
        scheduler.enqueue(
            candidate(name), None, arm_key=name, coarse_key=name,
            source_root=f"anonymous-{index % roots}", family="family",
            feature_key=feature(service_profiles=("l0", "l1", "l2", leaf)),
        )
    return scheduler


@pytest.mark.parametrize("roots", [1, 2, 5, 17])
def test_cache_only_leaves_do_not_enter_cold_discovery_search(roots):
    scheduler = populated(roots=roots)
    selected, examined = scheduler._pop_cold_leaf_coverage_candidate_unlocked(
        ignore_credits=False, provisionally_selected_arms=set(),
    )
    assert selected is None
    assert examined == 0
    assert scheduler.quick_metrics()["selected_total"] == 0
    scheduler.release_outstanding()


@pytest.mark.parametrize("shared_leaf", [False, True])
def test_cold_discovery_searches_only_arms_with_an_eligible_candidate(shared_leaf):
    scheduler = populated(shared_leaf=shared_leaf, unknown=True, roots=1)
    selected, examined = scheduler._pop_cold_leaf_coverage_candidate_unlocked(
        ignore_credits=False, provisionally_selected_arms=set(),
    )
    assert selected is not None
    assert selected.node.name == "new-candidate"
    assert examined == 1
    assert scheduler.quick_metrics()["selected_total"] == 0
    scheduler.release_outstanding()


def expected_index(scheduler):
    result = {}
    for arm_id, arm in scheduler._arms.items():
        for item in arm.items:
            if item.visibility_class != "unknown":
                continue
            keys = (*scheduler._candidate_service_cohort_keys(item),
                    scheduler._candidate_similar_cohort_key(item))
            for key in keys:
                result.setdefault(key, Counter())[arm_id] += 1
    return result


class UnfilteredReference(Tier3CandidateScheduler):
    """Previous search scope; selection and feedback stay unchanged."""

    def _pop_cold_leaf_coverage_candidate_unlocked(self, **kwargs):
        self._cohort_unknown_queued_arms = {
            key: {arm_id: count for arms in buckets.values()
                  for arm_id, count in arms.items()}
            for key, buckets in self._cohort_queued_arms.items()
        }
        try:
            return super()._pop_cold_leaf_coverage_candidate_unlocked(**kwargs)
        finally:
            self._cohort_unknown_queued_arms = expected_index(self)


@pytest.mark.parametrize("roots", [1, 2, 5, 17])
def test_eligible_index_preserves_async_selection_and_cancellation_trace(roots):
    traces = []
    for scheduler_type in (UnfilteredReference, Tier3CandidateScheduler):
        with patch("tier3_candidate_scheduler.time.monotonic", return_value=100.0):
            scheduler = scheduler_type(
                max_active_arms_per_family=4,
                lookahead_evidence_provider=lambda item: Tier3CandidateLookaheadEvidence(
                    tcp_only=int(item.node.name.split("-")[-1]) % 3 == 0,
                ),
            )
            scheduler.set_credit_envelope(16, handoff_window=16)
            for index in range(1200):
                leaf = index % 70
                scheduler.enqueue(
                    candidate(f"node-{index}"), None,
                    arm_key=f"arm-{index % 110}", coarse_key=f"arm-{index % 110}",
                    source_root=f"anonymous-{index % roots}", family="family",
                    feature_key=feature(service_profiles=(
                        "l0", f"l1-{leaf % 4}", f"l2-{leaf % 10}", f"leaf-{leaf}",
                    )),
                )
            trace = []
            for epoch in range(20):
                batch = scheduler.reserve_handoffs(16)
                assert scheduler._cohort_unknown_queued_arms == expected_index(scheduler)
                for offset, reservation in enumerate(reversed(batch)):
                    trace.append((reservation.candidate.node.name,
                                  reservation.ticket.selection_reason))
                    if (epoch + offset) % 7 == 0:
                        scheduler.cancel_handoff(reservation, requeue=True)
                    elif (epoch + offset) % 11 == 0:
                        scheduler.cancel_handoff(reservation, requeue=False)
                    else:
                        complete(scheduler, reservation, passed=offset % 5 == 0)
                assert scheduler._cohort_unknown_queued_arms == expected_index(scheduler)
            scheduler.release_outstanding()
            assert scheduler.pending_handoff_count() == 0
            assert scheduler._tickets == {}
            assert scheduler._cohort_unknown_queued_arms == expected_index(scheduler)
            traces.append(trace)
    assert len(traces[0]) >= 256
    assert traces[0] == traces[1]
