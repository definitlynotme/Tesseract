from __future__ import annotations

import pytest

from tier3_endpoint_controller import Tier3EndpointController, _EndpointStats
from tier3_validation_protocol import ProbeAttemptFact, ValidationStage


def _observe(controller, endpoint, *, completed: int, passed: int, censored: int = 0):
    for index in range(completed + censored):
        done = index < completed
        fact = ProbeAttemptFact(
            endpoint=endpoint,
            elapsed_ms=100 if done else 3000,
            full_budget=done,
            completed=done,
            entitlement_exhausted=not done,
            outcome="pass" if index < passed else "transient" if done else "right_censored",
            lifecycle="completed" if done else "censored",
            stage=ValidationStage.FAST_PROBE.value,
            granted_budget_ms=3000,
            attempt_id=f"{endpoint}:{index}",
        )
        for _ in range(2):
            controller.observe_attempts(
                (fact,), root=f"opaque-root-{index % 2}", protocol=f"family-{index % 2}",
            )


def test_identical_endpoint_quality_does_not_decay_with_more_evidence() -> None:
    smaller = _EndpointStats(completed=10, successes=5, worker_ms=10_000,
                             fast_completed=10, fast_successes=5, fast_worker_ms=10_000)
    larger = _EndpointStats(completed=100, successes=50, worker_ms=100_000,
                            fast_completed=100, fast_successes=50, fast_worker_ms=100_000)
    assert larger.throughput_lcb > smaller.throughput_lcb
    assert larger.fast_throughput_lcb > smaller.fast_throughput_lcb
    assert 0 < larger.fast_throughput_lcb < 0.5


def test_exhausted_endpoint_time_counts_once_without_inventing_failure() -> None:
    endpoint = "https://probe.invalid/204"
    controller = Tier3EndpointController((endpoint,))
    _observe(controller, endpoint, completed=8, passed=4, censored=24)
    stats = controller.snapshot()[controller._diagnostic_label(endpoint)]
    assert stats["completed"] == stats["fast_completed"] == 8
    assert stats["successes"] == stats["fast_successes"] == 4
    assert stats["fast_entitlement_coverage"] == 32
    assert stats["fast_entitlement_exhausted"] == 24
    assert stats["worker_ms"] == stats["fast_worker_ms"] == 72_800
    assert controller.snapshot()["_policy"]["yield_metric_revision"] == 2
    assert controller.snapshot()["_policy"]["worker_time_includes_exhausted"] is True
    completed_only = _EndpointStats(fast_completed=8, fast_successes=4, fast_worker_ms=800)
    assert stats["fast_throughput_lcb"] == pytest.approx(
        completed_only.fast_throughput_lcb * 800 / 72_800
    )


def test_endpoint_promotion_uses_exposure_not_small_completed_denominator() -> None:
    sparse = "https://sparse.invalid/204"
    productive = "https://productive.invalid/204"
    controller = Tier3EndpointController((sparse, productive))
    _observe(controller, sparse, completed=8, passed=4, censored=56)
    _observe(controller, productive, completed=64, passed=32)
    for index in range(33):
        controller.slice_decision(f"candidate-{index}", ValidationStage.FAST_PROBE)
    assert controller.snapshot()["_policy"]["promoted_endpoint"] == controller._diagnostic_label(productive)
