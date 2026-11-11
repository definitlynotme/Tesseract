from __future__ import annotations

import json

import pytest

from tier3_risk_control import (
    TIER3_RISK_CONTROL_SCHEMA_VERSION,
    TIER3_VALIDATION_CONTRACT_HASH,
    Tier3ControlSnapshot,
    Tier3LaneAllocation,
    Tier3OutcomeKind,
    Tier3ReachabilityClass,
    Tier3RiskController,
    Tier3SourceControlSnapshot,
    Tier3SourceRiskState,
    empirical_cvar95,
    lane_allocation,
)


def make_snapshot(**overrides):
    values = {
        "now_monotonic": 100.0,
        "alive_count": 0,
        "min_alive_nodes": 3,
        "target_alive": 25,
        "pi_desired_workers": 16,
        "active_workers": 16,
        "primary_depth": 64,
        "canonical_depth": 32,
        "retry_depth": 16,
        "primary_oldest_wait_ms": 1200.0,
        "canonical_oldest_wait_ms": 700.0,
        "retry_oldest_wait_ms": 500.0,
        "supported_backlog_count": 100,
        "source_remaining_upper": 100,
        "recent_admissions_per_second": 1.0,
        "recent_candidate_pass_rate": 0.2,
        "facility_failure_rate": 0.0,
        "warm_reload_p95_ms": 500.0,
        "process_reset_rate": 0.0,
        "facility_validation_count": 64,
        "session_committed_count": 1,
        "capacity_epoch_id": 1,
        "capacity_epoch_dispatched_count": 16,
        "capacity_epoch_completed_count": 16,
        "capacity_epoch_occupied_ms": 16_000.0,
        "capacity_epoch_committed_count": 1,
    }
    values.update(overrides)
    return Tier3ControlSnapshot(**values)


def qualify_policy_replay(controller: Tier3RiskController) -> None:
    assert controller.record_policy_replay(
        sample_count=200,
        baseline_cvar95_ms=10_000.0,
        bounded_cvar95_ms=8_900.0,
        baseline_gap_20s=10.0,
        bounded_gap_20s=10.0,
    )


def test_cvar_requires_sample_floor_and_uses_worst_five_percent() -> None:
    assert empirical_cvar95([1.0] * 199, minimum_samples=200) is None
    values = [1.0] * 190 + [10.0] * 10
    assert empirical_cvar95(values, minimum_samples=200) == 10.0


def test_cvar_session_objective_uses_120_second_loss_for_censored_gate() -> None:
    controller = Tier3RiskController()
    for _index in range(200):
        controller.observe_session(
            time_to_gate_ms=None,
            alive_at_20s=0,
            target_alive=25,
            observation_horizon_ms=60_000,
        )
    metrics = controller.objective_metrics()
    assert metrics["cvar95_time_to_gate_ms"] == 120_000
    assert metrics["expected_target_gap_20s"] == 25.0
    assert metrics["session_censored_count"] == 200


def test_right_censored_candidate_is_not_counted_as_failure_or_negative() -> None:
    controller = Tier3RiskController(
        config={"min_candidate_observations": 1, "min_candidate_passes": 1},
        random_seed=1,
    )
    controller.observe_candidate(
        outcome=Tier3OutcomeKind.RIGHT_CENSORED,
        elapsed_seconds=8.0,
        root="root-a",
        family="vless",
        coarse="443",
    )
    diagnostics = controller.diagnostics()
    assert diagnostics["candidate_observation_count"] == 1
    assert diagnostics["candidate_pass_count"] == 0
    assert controller.candidate_pass_lcb(root="root-a", family="vless", coarse="443") == 0.0


def test_facility_failure_does_not_update_candidate_model() -> None:
    controller = Tier3RiskController(random_seed=1)
    controller.observe_candidate(
        outcome=Tier3OutcomeKind.REAL_DEAD,
        elapsed_seconds=1.0,
        root="root-a",
        family="vless",
        coarse="443",
        facility_failure=True,
    )
    assert controller.diagnostics()["candidate_observation_count"] == 0


def test_hierarchical_model_falls_back_until_local_sample_floor() -> None:
    controller = Tier3RiskController(random_seed=1)
    for _index in range(30):
        controller.observe_candidate(
            outcome=Tier3OutcomeKind.PASS,
            elapsed_seconds=2.0,
            root="root-a",
            family="vless",
            coarse="443",
        )
    for _index in range(5):
        controller.observe_candidate(
            outcome=Tier3OutcomeKind.REAL_DEAD,
            elapsed_seconds=2.0,
            root="root-a",
            family="vless",
            coarse="8443",
        )
    assert controller.candidate_pass_lcb(root="root-a", family="vless", coarse="8443") > 0.5


def test_competing_risk_age_buckets_keep_slow_pass_hazard_visible() -> None:
    controller = Tier3RiskController(config={"rollout_mode": "shadow"})
    for _index in range(30):
        controller.observe_candidate(
            outcome=Tier3OutcomeKind.REAL_DEAD,
            elapsed_seconds=1.0,
            root="root",
            family="vless",
            coarse="mixed",
        )
        controller.observe_candidate(
            outcome=Tier3OutcomeKind.PASS,
            elapsed_seconds=10.0,
            root="root",
            family="vless",
            coarse="mixed",
        )
    report = controller.evaluate(make_snapshot()).to_report()
    assert report["risk_retry_hazard_lcb"] > report["risk_primary_hazard_lcb"]
    assert report["risk_canonical_queue_debt"] > 0


def test_learning_correction_remains_zero_before_calibration_gate() -> None:
    controller = Tier3RiskController(
        config={
            "rollout_mode": "auto_bounded",
            "min_candidate_observations": 500,
            "min_candidate_passes": 20,
        },
        random_seed=1,
    )
    for _index in range(40):
        controller.observe_candidate(
            outcome=Tier3OutcomeKind.PASS,
            elapsed_seconds=1.0,
            root="good",
            family="vless",
            coarse="443",
            predicted_pass_probability=1.0,
        )
    assert controller.candidate_correction(root="good", family="vless", coarse="443") == 0.0
    assert controller.evaluate(make_snapshot()).rollout_stage == "shadow"


def test_learning_correction_is_bounded_after_gate() -> None:
    controller = Tier3RiskController(
        config={
            "rollout_mode": "auto_bounded",
            "min_candidate_observations": 20,
            "min_candidate_passes": 5,
            "mature_candidate_observations": 10_000,
            "mature_candidate_passes": 10_000,
        },
        random_seed=1,
    )
    for _index in range(30):
        controller.observe_candidate(
            outcome=Tier3OutcomeKind.PASS,
            elapsed_seconds=1.0,
            root="good",
            family="vless",
            coarse="443",
            predicted_pass_probability=1.0,
        )
    for _index in range(30):
        controller.observe_candidate(
            outcome=Tier3OutcomeKind.REAL_DEAD,
            elapsed_seconds=5.0,
            root="bad",
            family="ss",
            coarse="80",
            predicted_pass_probability=0.0,
        )
    qualify_policy_replay(controller)
    decision = controller.evaluate(make_snapshot())
    assert decision.rollout_stage == "bounded_10"
    assert decision.candidate_correction_limit == pytest.approx(0.1)
    assert 0.0 < controller.candidate_correction(root="good", family="vless", coarse="443") <= 0.1
    assert -0.1 <= controller.candidate_correction(root="bad", family="ss", coarse="80") <= 0.0


def test_learning_model_updates_are_published_only_on_refresh_interval() -> None:
    controller = Tier3RiskController(
        config={
            "rollout_mode": "auto_bounded",
            "model_refresh_interval_seconds": 15,
            "min_candidate_observations": 20,
            "min_candidate_passes": 5,
            "mature_candidate_observations": 10_000,
            "mature_candidate_passes": 10_000,
        }
    )
    for _index in range(30):
        controller.observe_candidate(
            outcome=Tier3OutcomeKind.PASS,
            elapsed_seconds=1.0,
            root="good",
            family="vless",
            coarse="443",
            predicted_pass_probability=1.0,
        )
        controller.observe_candidate(
            outcome=Tier3OutcomeKind.REAL_DEAD,
            elapsed_seconds=5.0,
            root="bad",
            family="ss",
            coarse="80",
            predicted_pass_probability=0.0,
        )
    qualify_policy_replay(controller)
    controller.evaluate(make_snapshot(now_monotonic=100.0))
    before = controller.candidate_correction(
        root="late",
        family="trojan",
        coarse="443",
    )
    for _index in range(30):
        controller.observe_candidate(
            outcome=Tier3OutcomeKind.PASS,
            elapsed_seconds=1.0,
            root="late",
            family="trojan",
            coarse="443",
            predicted_pass_probability=1.0,
        )
    controller.evaluate(make_snapshot(now_monotonic=110.0))
    not_yet_published = controller.candidate_correction(
        root="late",
        family="trojan",
        coarse="443",
    )
    controller.evaluate(make_snapshot(now_monotonic=116.0))
    published = controller.candidate_correction(
        root="late",
        family="trojan",
        coarse="443",
    )
    assert not_yet_published == before
    assert published > not_yet_published


def test_drift_forces_shadow_fallback() -> None:
    controller = Tier3RiskController(
        config={
            "min_candidate_observations": 1,
            "min_candidate_passes": 1,
            "mature_candidate_observations": 1,
            "mature_candidate_passes": 1,
        }
    )
    for _index in range(150):
        controller.observe_candidate(
            outcome=Tier3OutcomeKind.PASS,
            elapsed_seconds=1.0,
            root="root",
            family="vless",
            coarse="443",
            predicted_pass_probability=1.0,
        )
    for _index in range(50):
        controller.observe_candidate(
            outcome=Tier3OutcomeKind.REAL_DEAD,
            elapsed_seconds=1.0,
            root="root",
            family="vless",
            coarse="443",
            predicted_pass_probability=0.0,
        )
    decision = controller.evaluate(make_snapshot())
    assert decision.rollout_stage == "shadow"
    assert decision.drift_detected is True
    assert decision.calibration_lambda == 0.0


@pytest.mark.parametrize("workers", range(1, 25))
def test_lane_allocator_honors_constraints_for_all_worker_counts(workers: int) -> None:
    allocation = lane_allocation(
        workers,
        primary_depth=100,
        canonical_depth=100,
        retry_depth=100,
    )
    assert allocation.total == workers
    assert allocation.primary_tokens >= 1
    assert allocation.retry_tokens <= workers // 4
    if workers >= 4:
        assert allocation.primary_tokens >= (workers + 1) // 2
        assert allocation.canonical_tokens >= workers // 4


def test_lane_allocator_work_steals_when_only_canonical_has_work() -> None:
    assert lane_allocation(
        8,
        primary_depth=0,
        canonical_depth=10,
        retry_depth=0,
    ) == Tier3LaneAllocation(0, 8, 0)


def test_source_facility_failure_does_not_penalize_posterior() -> None:
    controller = Tier3RiskController(random_seed=1)
    controller.observe_source_action(
        "root-a",
        admissions=0,
        token_seconds=5.0,
        facility_healthy=False,
    )
    root = controller.source_diagnostics()["roots"]["root-a"]
    assert root["actions"] == 0
    assert root["state"] == Tier3SourceRiskState.FACILITY_BLOCKED.value


def test_source_probe_transient_does_not_penalize_posterior() -> None:
    controller = Tier3RiskController(random_seed=1)
    controller.observe_source_action(
        "root-a",
        admissions=0,
        token_seconds=8.0,
        facility_healthy=True,
        informative=False,
        state=Tier3SourceRiskState.COOLDOWN,
    )
    root = controller.source_diagnostics()["roots"]["root-a"]
    assert root["actions"] == 0
    assert root["thompson_mean"] == 0.5
    assert root["state"] == Tier3SourceRiskState.COOLDOWN.value


def test_source_reward_is_normalized_by_fetch_token_time() -> None:
    fast = Tier3RiskController(random_seed=1)
    slow = Tier3RiskController(random_seed=1)
    fast.observe_source_action(
        "root",
        admissions=2,
        token_seconds=1.0,
        facility_healthy=True,
    )
    slow.observe_source_action(
        "root",
        admissions=2,
        token_seconds=10.0,
        facility_healthy=True,
    )
    assert (
        fast.source_diagnostics()["roots"]["root"]["thompson_mean"]
        > slow.source_diagnostics()["roots"]["root"]["thompson_mean"]
    )


def test_source_fairness_promotes_retry_due_root_within_two_rounds() -> None:
    controller = Tier3RiskController(random_seed=7)
    sources = (
        Tier3SourceControlSnapshot("high", state="fresh_high_yield"),
        Tier3SourceControlSnapshot("retry", state="failed_retryable", retry_due=True),
    )
    controller.observe_source_action(
        "high",
        admissions=10,
        token_seconds=1.0,
        facility_healthy=True,
    )
    first = controller.rank_sources(sources)
    second = controller.rank_sources(sources)
    assert "retry" in first
    assert second[0] == "retry"


def test_reachability_distinguishes_supply_and_facility_limits() -> None:
    controller = Tier3RiskController(config={"rollout_mode": "shadow"})
    supply = controller.evaluate(
        make_snapshot(
            active_workers=4,
            primary_depth=1,
            canonical_depth=0,
            retry_depth=0,
            supported_backlog_count=0,
            source_remaining_upper=1,
        )
    )
    assert supply.reachability_class is Tier3ReachabilityClass.SUPPLY_LIMITED

    for _index in range(64):
        controller.observe_facility(
            worker_count=16,
            reload_elapsed_ms=500,
            failed=_index < 16,
        )
    facility = controller.evaluate(make_snapshot(facility_validation_count=64))
    assert facility.reachability_class is Tier3ReachabilityClass.FACILITY_LIMITED


def test_reachability_remains_uncertain_without_pass_model_evidence() -> None:
    controller = Tier3RiskController(config={"rollout_mode": "shadow"})
    decision = controller.evaluate(
        make_snapshot(
            active_workers=16,
            primary_depth=100,
            canonical_depth=100,
            retry_depth=10,
            supported_backlog_count=100,
            source_remaining_upper=100,
            facility_validation_count=64,
            facility_failure_rate=0.0,
        )
    )
    assert decision.reachability_class is Tier3ReachabilityClass.UNCERTAIN


def test_state_round_trip_is_aggregate_and_atomic(tmp_path) -> None:
    path = tmp_path / "tier3-control-state.json"
    controller = Tier3RiskController(state_path=path, random_seed=1)
    controller.observe_candidate(
        outcome=Tier3OutcomeKind.PASS,
        elapsed_seconds=1.0,
        root="root-a",
        family="vless",
        coarse="443",
    )
    controller.observe_session(time_to_gate_ms=5000, alive_at_20s=10, target_alive=25)
    controller.save()
    payload = json.loads(path.read_text(encoding="utf-8"))
    serialized = json.dumps(payload)
    assert payload["validation_contract_hash"] == TIER3_VALIDATION_CONTRACT_HASH
    assert "probe_endpoint" not in serialized
    assert "vless://" not in serialized
    assert not path.with_suffix(".json.tmp").exists()

    restored = Tier3RiskController(state_path=path, random_seed=1)
    assert restored.diagnostics()["candidate_observation_count"] == 1
    assert restored.diagnostics()["session_sample_count"] == 1


def test_persisted_state_hashes_source_urls_and_hierarchical_root_keys(tmp_path) -> None:
    path = tmp_path / "tier3-control-state.json"
    source_url = "https://user:secret@example.invalid/private/subscription"
    controller = Tier3RiskController(state_path=path, random_seed=1)
    controller.observe_candidate(
        outcome=Tier3OutcomeKind.PASS,
        elapsed_seconds=1.0,
        root=source_url,
        family="vless",
        coarse="host:private.example.invalid",
    )
    controller.observe_source_action(
        source_url,
        admissions=1,
        token_seconds=1.0,
        facility_healthy=True,
    )
    controller.save()
    serialized = path.read_text(encoding="utf-8")
    assert source_url not in serialized
    assert "secret" not in serialized
    assert "private.example.invalid" not in serialized


def test_shadow_evaluation_does_not_consume_source_fairness_rounds() -> None:
    controller = Tier3RiskController(config={"rollout_mode": "shadow"}, random_seed=3)
    source = Tier3SourceControlSnapshot(
        "retry",
        state=Tier3SourceRiskState.FAILED_RETRYABLE.value,
        retry_due=True,
    )
    snapshot = make_snapshot(source_states=(source,))
    first = controller.evaluate(snapshot)
    second = controller.evaluate(snapshot)
    assert first.source_order == second.source_order == ("retry",)
    assert controller.source_diagnostics()["roots"]["retry"]["fairness_debt"] == 0


def test_whittle_shadow_needs_replay_improvement_before_promotion() -> None:
    controller = Tier3RiskController(random_seed=5)
    for _index in range(100):
        controller.observe_source_action(
            "root-a",
            admissions=1,
            token_seconds=1.0,
            facility_healthy=True,
        )
    assert controller.source_diagnostics()["whittle_ready"] is False
    controller.record_source_policy_replay(relative_improvement=0.09)
    assert controller.source_diagnostics()["whittle_ready"] is False
    controller.record_source_policy_replay(relative_improvement=0.10)
    assert controller.source_diagnostics()["whittle_ready"] is True


def _mature_canary_controller(*, hard_worker_max: int = 24) -> Tier3RiskController:
    controller = Tier3RiskController(
        config={
            "rollout_mode": "auto_bounded",
            "min_candidate_observations": 1,
            "min_candidate_passes": 1,
            "mature_candidate_observations": 1,
            "mature_candidate_passes": 1,
            "initial_safe_worker_cap": 16,
            "hard_worker_max": hard_worker_max,
            "envelope_interval_ms": 2000,
        },
        random_seed=7,
    )
    for _index in range(64):
        controller.observe_candidate(
            outcome=Tier3OutcomeKind.PASS,
            elapsed_seconds=1.0,
            root="root",
            family="vless",
            coarse="443",
            predicted_pass_probability=1.0,
        )
        controller.observe_facility(
            worker_count=16,
            reload_elapsed_ms=500.0,
            failed=False,
            admissions_per_second=1.0,
        )
    qualify_policy_replay(controller)
    return controller


def test_auto_bounded_requires_two_hundred_seed_non_regression_evidence() -> None:
    controller = Tier3RiskController(
        config={
            "rollout_mode": "auto_bounded",
            "min_candidate_observations": 1,
            "min_candidate_passes": 1,
            "mature_candidate_observations": 1,
            "mature_candidate_passes": 1,
        }
    )
    controller.observe_candidate(
        outcome=Tier3OutcomeKind.PASS,
        elapsed_seconds=1.0,
        root="root",
        family="vless",
        coarse="443",
        predicted_pass_probability=1.0,
    )
    assert controller.evaluate(make_snapshot()).rollout_stage == "shadow"
    assert not controller.record_policy_replay(
        sample_count=199,
        baseline_cvar95_ms=10_000.0,
        bounded_cvar95_ms=8_000.0,
        baseline_gap_20s=10.0,
        bounded_gap_20s=9.0,
    )
    assert controller.evaluate(make_snapshot()).rollout_stage == "shadow"
    assert not controller.record_policy_replay(
        sample_count=200,
        baseline_cvar95_ms=10_000.0,
        bounded_cvar95_ms=10_600.0,
        baseline_gap_20s=10.0,
        bounded_gap_20s=8.0,
    )
    assert controller.evaluate(make_snapshot()).rollout_stage == "shadow"
    qualify_policy_replay(controller)
    assert controller.evaluate(make_snapshot()).rollout_stage == "bounded_20"


def test_two_hundred_deterministic_seed_replay_drives_promotion_metrics() -> None:
    import random

    baseline_gate: list[float] = []
    bounded_gate: list[float] = []
    baseline_gap: list[float] = []
    bounded_gap: list[float] = []
    for seed in range(200):
        rng = random.Random(seed)
        tail_penalty = 8_000.0 if seed % 20 == 0 else 0.0
        baseline = 8_000.0 + rng.uniform(0.0, 2_000.0) + tail_penalty
        baseline_gate.append(baseline)
        bounded_gate.append(baseline * 0.88)
        gap = float(8 + rng.randrange(0, 5))
        baseline_gap.append(gap)
        bounded_gap.append(gap * 0.98)

    baseline_cvar = empirical_cvar95(baseline_gate, minimum_samples=200)
    bounded_cvar = empirical_cvar95(bounded_gate, minimum_samples=200)
    assert baseline_cvar is not None and bounded_cvar is not None
    controller = Tier3RiskController()
    assert controller.record_policy_replay(
        sample_count=200,
        baseline_cvar95_ms=baseline_cvar,
        bounded_cvar95_ms=bounded_cvar,
        baseline_gap_20s=sum(baseline_gap) / len(baseline_gap),
        bounded_gap_20s=sum(bounded_gap) / len(bounded_gap),
    )


def test_shadow_counterfactual_replay_promotes_without_crossing_fairness_groups() -> None:
    controller = Tier3RiskController(
        config={
            "rollout_mode": "auto_bounded",
            "min_candidate_observations": 1,
            "min_candidate_passes": 1,
            "mature_candidate_observations": 1,
            "mature_candidate_passes": 1,
        },
        random_seed=11,
    )
    for _index in range(25):
        controller.observe_candidate(
            outcome=Tier3OutcomeKind.PASS,
            elapsed_seconds=1.0,
            root="root",
            family="vless",
            coarse="good",
            predicted_pass_probability=1.0,
        )
        controller.observe_candidate(
            outcome=Tier3OutcomeKind.REAL_DEAD,
            elapsed_seconds=5.0,
            root="root",
            family="vless",
            coarse="bad",
            predicted_pass_probability=0.0,
        )
    controller.observe_session(
        time_to_gate_ms=6_100,
        alive_at_20s=1,
        target_alive=2,
        min_alive_nodes=1,
        initial_alive=0,
    )

    for _seed in range(200):
        # Position zero is the preserved unknown-exploration slot. Positions
        # one and two share the same root/family fairness group, so only their
        # coarse-arm tie-break may change in the counterfactual.
        controller.observe_candidate(
            outcome=Tier3OutcomeKind.REAL_DEAD,
            elapsed_seconds=0.1,
            root="unknown-root",
            family="other",
            coarse="unknown",
            predicted_pass_probability=0.0,
        )
        controller.observe_candidate(
            outcome=Tier3OutcomeKind.REAL_DEAD,
            elapsed_seconds=5.0,
            root="root",
            family="vless",
            coarse="bad",
            predicted_pass_probability=0.0,
        )
        controller.observe_candidate(
            outcome=Tier3OutcomeKind.PASS,
            elapsed_seconds=1.0,
            root="root",
            family="vless",
            coarse="good",
            predicted_pass_probability=1.0,
        )
        controller.observe_session(
            time_to_gate_ms=6_100,
            alive_at_20s=1,
            target_alive=2,
            min_alive_nodes=1,
            initial_alive=0,
        )

    diagnostics = controller.diagnostics()
    assert diagnostics["shadow_replay_session_count"] == 201
    assert diagnostics["policy_replay"]["qualified"] is True
    assert controller.evaluate(make_snapshot()).rollout_stage == "bounded_20"


def _enter_eighteen_worker_canary(controller: Tier3RiskController):
    decision = None
    for index, now in enumerate((100.0, 100.5)):
        decision = controller.evaluate(
            make_snapshot(
                now_monotonic=now,
                pi_desired_workers=16,
                active_workers=16,
                facility_validation_count=64,
                recent_admissions_per_second=1.0,
                session_elapsed_seconds=3.0 + index * 0.5,
                validation_completion_count=16 + index,
                validation_completion_throughput=4.0,
                primary_completed_count=16,
                primary_pass_count=8,
                primary_occupied_ms=16_000.0,
            )
        )
    assert decision is not None
    return decision


def test_capacity_canary_enters_only_by_two_and_promotes_after_evidence() -> None:
    controller = _mature_canary_controller()
    canary = _enter_eighteen_worker_canary(controller)
    assert canary.rollout_stage == "bounded_20"
    assert canary.canary_state == "testing_18"
    assert canary.safe_worker_cap == 18
    assert canary.desired_workers == 18

    target_started = controller.evaluate(
        make_snapshot(
            now_monotonic=101.0,
            pi_desired_workers=16,
            active_workers=18,
            facility_validation_count=80,
            session_elapsed_seconds=4.0,
            validation_completion_count=17,
            validation_completion_throughput=4.0,
            primary_completed_count=17,
            primary_pass_count=9,
            primary_occupied_ms=17_000.0,
        )
    )
    assert target_started.canary_state == "testing_18"

    promoted = controller.evaluate(
        make_snapshot(
            now_monotonic=104.1,
            pi_desired_workers=16,
            active_workers=18,
            facility_validation_count=128,
            recent_admissions_per_second=1.06,
            facility_failure_rate=0.0,
            warm_reload_p95_ms=600.0,
            session_elapsed_seconds=7.0,
            validation_completion_count=33,
            validation_completion_throughput=4.1,
            primary_completed_count=33,
            primary_pass_count=17,
            primary_occupied_ms=33_000.0,
            session_committed_count=2,
            capacity_epoch_committed_count=2,
        )
    )
    assert promoted.safe_worker_cap == 18
    assert promoted.canary_state == "stable"


def test_previous_capacity_epoch_commit_cannot_promote_current_canary() -> None:
    controller = _mature_canary_controller()
    _enter_eighteen_worker_canary(controller)

    controller.evaluate(
        make_snapshot(
            now_monotonic=101.0,
            active_workers=18,
            capacity_epoch_id=2,
            capacity_epoch_dispatched_count=1,
            capacity_epoch_completed_count=1,
            capacity_epoch_occupied_ms=1_000.0,
            capacity_epoch_committed_count=0,
            validation_completion_count=17,
        )
    )
    decision = controller.evaluate(
        make_snapshot(
            now_monotonic=104.2,
            active_workers=18,
            session_committed_count=9,
            # All nine commits belong to work dispatched at cap16. The cap18
            # epoch has completed work but has not produced an admission.
            capacity_epoch_id=2,
            capacity_epoch_dispatched_count=17,
            capacity_epoch_completed_count=17,
            capacity_epoch_occupied_ms=17_000.0,
            capacity_epoch_committed_count=0,
            validation_completion_count=33,
            validation_completion_throughput=4.0,
        )
    )

    assert decision.safe_worker_cap == 16
    assert decision.canary_state == "cooldown"


def test_fast_canary_does_not_age_or_promote_before_target_workers_are_active() -> None:
    controller = _mature_canary_controller()
    _enter_eighteen_worker_canary(controller)

    still_waiting = controller.evaluate(
        make_snapshot(
            now_monotonic=119.0,
            active_workers=16,
            facility_validation_count=256,
            validation_completion_count=128,
            validation_completion_throughput=8.0,
            primary_completed_count=128,
            primary_pass_count=64,
            primary_occupied_ms=128_000.0,
        )
    )

    assert still_waiting.canary_state == "testing_18"
    assert still_waiting.desired_workers == 18
    assert still_waiting.safe_worker_cap == 18


def test_facility_conformal_bounds_do_not_extrapolate_to_untested_capacity() -> None:
    controller = Tier3RiskController()
    for _index in range(64):
        controller.observe_facility(
            worker_count=16,
            reload_elapsed_ms=500.0,
            failed=False,
        )
    at_sixteen = controller.facility_diagnostics(16)
    at_eighteen = controller.facility_diagnostics(18)
    assert at_sixteen["sample_count"] == 64
    assert at_sixteen["failure_upper"] <= 0.05
    assert at_eighteen["sample_count"] == 0
    assert at_eighteen["failure_upper"] == 1.0


def test_capacity_canary_failure_rolls_back_and_enters_cooldown() -> None:
    controller = _mature_canary_controller()
    _enter_eighteen_worker_canary(controller)
    failed = controller.evaluate(
        make_snapshot(
            now_monotonic=119.0,
            pi_desired_workers=16,
            active_workers=18,
            facility_validation_count=128,
            recent_admissions_per_second=0.9,
            facility_failure_rate=0.10,
            warm_reload_p95_ms=1500.0,
            session_elapsed_seconds=7.0,
            validation_completion_count=33,
            validation_completion_throughput=3.0,
            hard_facility_failure_count=1,
        )
    )
    assert failed.safe_worker_cap == 16
    assert failed.canary_state == "cooldown"


def test_capacity_canary_cooldown_survives_state_round_trip(tmp_path) -> None:
    path = tmp_path / "tier3-control-state.json"
    controller = _mature_canary_controller()
    controller.state_path = path
    _enter_eighteen_worker_canary(controller)
    controller.evaluate(
        make_snapshot(
            now_monotonic=119.0,
            active_workers=18,
            facility_validation_count=128,
            recent_admissions_per_second=0.5,
            facility_failure_rate=0.2,
            warm_reload_p95_ms=2000.0,
            session_elapsed_seconds=7.0,
            validation_completion_count=33,
            validation_completion_throughput=2.0,
            hard_facility_failure_count=1,
        )
    )
    controller.save()

    restored = Tier3RiskController(
        state_path=path,
        config={"initial_safe_worker_cap": 16, "hard_worker_max": 24},
    )
    assert restored.safe_worker_cap == 16
    assert restored.diagnostics()["worker_canary_state"] == "cooldown"


def test_fast_canary_runs_in_shadow_without_enabling_learning() -> None:
    controller = Tier3RiskController(
        config={
            "rollout_mode": "shadow",
            "initial_safe_worker_cap": 16,
            "hard_worker_max": 24,
        }
    )
    first = controller.evaluate(
        make_snapshot(
            now_monotonic=10.0,
            pi_desired_workers=24,
            active_workers=16,
            facility_validation_count=16,
            session_elapsed_seconds=3.0,
            validation_completion_count=16,
            validation_completion_throughput=4.0,
            primary_completed_count=16,
            primary_pass_count=8,
            primary_occupied_ms=16_000.0,
        )
    )
    second = controller.evaluate(
        make_snapshot(
            now_monotonic=10.5,
            pi_desired_workers=24,
            active_workers=16,
            facility_validation_count=17,
            session_elapsed_seconds=3.5,
            validation_completion_count=17,
            validation_completion_throughput=4.0,
            primary_completed_count=16,
            primary_pass_count=8,
            primary_occupied_ms=16_000.0,
        )
    )

    assert first.rollout_stage == "shadow"
    assert first.calibration_lambda == 0.0
    assert second.rollout_stage == "shadow"
    assert second.calibration_lambda == 0.0
    assert second.canary_state == "testing_18"
    assert second.safe_worker_cap == 18


def test_baseline_capacity_lock_keeps_canary_at_initial_cap() -> None:
    controller = Tier3RiskController(
        config={
            "initial_safe_worker_cap": 16,
            "hard_worker_max": 24,
            "baseline_capacity_canary_enabled": False,
        }
    )
    decision = None
    for offset in range(6):
        decision = controller.evaluate(
            make_snapshot(
                now_monotonic=10.0 + offset,
                pi_desired_workers=24,
                active_workers=16,
                primary_depth=64,
                primary_oldest_wait_ms=2_000.0,
                facility_validation_count=128,
                primary_completed_count=128,
                primary_pass_count=16,
                capacity_epoch_committed_count=16,
                capacity_epoch_completed_count=128,
                capacity_epoch_occupied_ms=64_000.0,
                recent_admissions_per_second=4.0,
                validation_completion_throughput=8.0,
                validation_completion_count=128,
                session_elapsed_seconds=20.0,
            )
        )
    assert decision is not None
    assert decision.safe_worker_cap == 16
    assert decision.desired_workers <= 16
    assert decision.canary_state == "baseline_locked"
    assert decision.canary_reason == "validator_baseline_recovery"


def test_first_fast_canary_requires_admission_evidence_not_only_facility_work() -> None:
    controller = Tier3RiskController(
        config={
            "rollout_mode": "shadow",
            "initial_safe_worker_cap": 16,
            "hard_worker_max": 24,
        }
    )
    for now in (10.0, 10.5):
        decision = controller.evaluate(
            make_snapshot(
                now_monotonic=now,
                pi_desired_workers=24,
                active_workers=16,
                facility_validation_count=16,
                session_elapsed_seconds=3.0 + (now - 10.0),
                validation_completion_count=0,
                validation_completion_throughput=0.0,
                primary_completed_count=16,
                primary_pass_count=8,
                primary_occupied_ms=16_000.0,
                session_committed_count=0,
                capacity_epoch_committed_count=0,
            )
        )

    assert decision.canary_state == "stable"
    assert decision.safe_worker_cap == 16


def test_fast_canary_requires_sixteen_candidate_completions_at_baseline() -> None:
    controller = Tier3RiskController(
        config={
            "rollout_mode": "shadow",
            "initial_safe_worker_cap": 16,
            "hard_worker_max": 24,
        }
    )
    for now in (10.0, 10.5):
        decision = controller.evaluate(
            make_snapshot(
                now_monotonic=now,
                pi_desired_workers=24,
                active_workers=16,
                facility_validation_count=64,
                session_elapsed_seconds=3.0 + (now - 10.0),
                validation_completion_count=8,
                validation_completion_throughput=4.0,
                primary_completed_count=8,
                primary_pass_count=2,
                primary_occupied_ms=24_000.0,
            )
        )

    assert decision.canary_state == "stable"
    assert decision.safe_worker_cap == 16


def test_fast_canary_promotes_to_twenty_but_shadow_cannot_cross_it() -> None:
    controller = Tier3RiskController(
        config={
            "rollout_mode": "shadow",
            "initial_safe_worker_cap": 16,
            "hard_worker_max": 24,
        }
    )
    controller.evaluate(
        make_snapshot(
            now_monotonic=10.0,
            pi_desired_workers=24,
            active_workers=16,
            session_elapsed_seconds=3.0,
            validation_completion_count=16,
            validation_completion_throughput=4.0,
            primary_completed_count=16,
            primary_pass_count=8,
            primary_occupied_ms=16_000.0,
        )
    )
    controller.evaluate(
        make_snapshot(
            now_monotonic=10.5,
            pi_desired_workers=24,
            active_workers=16,
            session_elapsed_seconds=3.5,
            validation_completion_count=17,
            validation_completion_throughput=4.0,
            primary_completed_count=16,
            primary_pass_count=8,
            primary_occupied_ms=16_000.0,
        )
    )
    controller.evaluate(
        make_snapshot(
            now_monotonic=11.0,
            pi_desired_workers=24,
            active_workers=18,
            session_elapsed_seconds=4.0,
            validation_completion_count=18,
            validation_completion_throughput=4.0,
            primary_completed_count=18,
            primary_pass_count=9,
            primary_occupied_ms=18_000.0,
        )
    )
    promoted_eighteen = controller.evaluate(
        make_snapshot(
            now_monotonic=14.1,
            pi_desired_workers=24,
            active_workers=18,
            session_elapsed_seconds=7.1,
            validation_completion_count=34,
            validation_completion_throughput=4.1,
            primary_completed_count=34,
            primary_pass_count=18,
            primary_occupied_ms=34_000.0,
            session_committed_count=2,
            capacity_epoch_committed_count=2,
        )
    )
    assert promoted_eighteen.safe_worker_cap == 18
    assert promoted_eighteen.canary_state == "stable"

    controller.evaluate(
        make_snapshot(
            now_monotonic=14.6,
            pi_desired_workers=24,
            active_workers=18,
            session_elapsed_seconds=7.0,
            validation_completion_count=35,
            validation_completion_throughput=4.1,
            primary_completed_count=35,
            primary_pass_count=18,
            primary_occupied_ms=35_000.0,
        )
    )
    testing_twenty = controller.evaluate(
        make_snapshot(
            now_monotonic=15.1,
            pi_desired_workers=24,
            active_workers=18,
            session_elapsed_seconds=7.5,
            validation_completion_count=36,
            validation_completion_throughput=4.1,
            primary_completed_count=36,
            primary_pass_count=18,
            primary_occupied_ms=36_000.0,
        )
    )
    assert testing_twenty.safe_worker_cap == 20
    assert testing_twenty.canary_state == "testing_20"

    controller.evaluate(
        make_snapshot(
            now_monotonic=15.6,
            pi_desired_workers=24,
            active_workers=20,
            session_elapsed_seconds=8.6,
            validation_completion_count=37,
            validation_completion_throughput=4.1,
            primary_completed_count=37,
            primary_pass_count=19,
            primary_occupied_ms=37_000.0,
        )
    )
    promoted_twenty = controller.evaluate(
        make_snapshot(
            now_monotonic=18.7,
            pi_desired_workers=24,
            active_workers=20,
            session_elapsed_seconds=10.5,
            validation_completion_count=53,
            validation_completion_throughput=4.2,
            primary_completed_count=53,
            primary_pass_count=27,
            primary_occupied_ms=53_000.0,
            session_committed_count=3,
            capacity_epoch_committed_count=3,
        )
    )
    assert promoted_twenty.safe_worker_cap == 20
    assert promoted_twenty.canary_state == "stable"

    still_twenty = controller.evaluate(
        make_snapshot(
            now_monotonic=40.0,
            pi_desired_workers=24,
            active_workers=20,
            session_elapsed_seconds=33.0,
            validation_completion_count=200,
            validation_completion_throughput=5.0,
            primary_completed_count=200,
            primary_pass_count=100,
            primary_occupied_ms=200_000.0,
        )
    )
    assert still_twenty.rollout_stage == "shadow"
    assert still_twenty.safe_worker_cap == 20


def test_fast_canary_uses_admission_lcb_per_busy_worker_second() -> None:
    controller = Tier3RiskController(
        config={
            "rollout_mode": "shadow",
            "initial_safe_worker_cap": 16,
            "hard_worker_max": 24,
        }
    )
    baseline_service = {
        "primary_completed_count": 16,
        "primary_pass_count": 8,
        "primary_occupied_ms": 16_000.0,
    }
    controller.evaluate(
        make_snapshot(
            now_monotonic=10.0,
            active_workers=16,
            facility_validation_count=16,
            session_elapsed_seconds=3.0,
            validation_completion_count=16,
            validation_completion_throughput=4.0,
            **baseline_service,
        )
    )
    testing = controller.evaluate(
        make_snapshot(
            now_monotonic=10.5,
            active_workers=16,
            facility_validation_count=17,
            session_elapsed_seconds=3.5,
            validation_completion_count=17,
            validation_completion_throughput=4.0,
            **baseline_service,
        )
    )
    assert testing.canary_state == "testing_18"

    regressed = controller.evaluate(
        make_snapshot(
            now_monotonic=11.0,
            active_workers=18,
            facility_validation_count=18,
            session_elapsed_seconds=4.0,
            validation_completion_count=18,
            validation_completion_throughput=10.0,
            recent_admissions_per_second=10.0,
            primary_completed_count=18,
            primary_pass_count=8,
            primary_occupied_ms=18_000.0,
        )
    )
    assert regressed.canary_state == "testing_18"
    controller.evaluate(
        make_snapshot(
            now_monotonic=14.1,
            active_workers=18,
            facility_validation_count=34,
            session_elapsed_seconds=7.1,
            validation_completion_count=34,
            validation_completion_throughput=10.0,
            recent_admissions_per_second=10.0,
            primary_completed_count=34,
            primary_pass_count=8,
            primary_occupied_ms=34_000.0,
        )
    )
    regressed = controller.evaluate(
        make_snapshot(
            now_monotonic=14.6,
            active_workers=18,
            facility_validation_count=35,
            session_elapsed_seconds=7.6,
            validation_completion_count=35,
            validation_completion_throughput=10.0,
            recent_admissions_per_second=10.0,
            primary_completed_count=35,
            primary_pass_count=8,
            primary_occupied_ms=35_000.0,
        )
    )

    assert regressed.canary_state == "cooldown"
    assert regressed.safe_worker_cap == 16


@pytest.mark.parametrize("hard_cap", [4, 8, 12, 16, 18, 20, 22, 24])
def test_capacity_decision_never_exceeds_explicit_hard_cap(hard_cap: int) -> None:
    controller = _mature_canary_controller(hard_worker_max=hard_cap)
    for step in range(20):
        decision = controller.evaluate(
            make_snapshot(
                now_monotonic=100.0 + step * 16.0,
                pi_desired_workers=24,
                active_workers=min(hard_cap, max(16, controller.safe_worker_cap)),
                facility_validation_count=64 * (step + 1),
                recent_admissions_per_second=2.0 + step,
                facility_failure_rate=0.0,
                warm_reload_p95_ms=500.0,
            )
        )
        assert decision.safe_worker_cap <= hard_cap
        assert decision.desired_workers <= hard_cap


def test_corrupt_state_falls_back_to_shadow_and_cap_16(tmp_path) -> None:
    path = tmp_path / "tier3-control-state.json"
    path.write_text("{broken", encoding="utf-8")
    controller = Tier3RiskController(state_path=path)
    decision = controller.evaluate(make_snapshot())
    assert decision.rollout_stage == "shadow"
    assert decision.safe_worker_cap == 16


def test_incompatible_state_schema_is_ignored_without_blocking_startup(tmp_path) -> None:
    path = tmp_path / "tier3-control-state.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "safe_worker_cap": 24,
                "candidate_observations": 9999,
                "candidate_passes": 999,
            }
        ),
        encoding="utf-8",
    )
    controller = Tier3RiskController(state_path=path)
    decision = controller.evaluate(make_snapshot())
    assert decision.rollout_stage == "shadow"
    assert decision.safe_worker_cap == 16
    assert controller.diagnostics()["candidate_observation_count"] == 0


def test_validation_contract_change_resets_learning_and_canary_evidence(tmp_path) -> None:
    path = tmp_path / "tier3-control-state.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": TIER3_RISK_CONTROL_SCHEMA_VERSION,
                "validation_contract_hash": "stale-contract",
                "safe_worker_cap": 24,
                "candidate_observations": 9999,
                "candidate_passes": 999,
                "hazards": {"global": {"pass": [999, 0, 0, 0]}},
                "sources": {
                    "retained-root": {
                        "alpha": 4.0,
                        "beta": 2.0,
                        "actions": 5,
                        "admissions": 3,
                        "token_seconds": 8.0,
                        "last_selected_round": 2,
                        "state": "fresh_high_yield",
                        "whittle_index": 0.0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    controller = Tier3RiskController(state_path=path)
    decision = controller.evaluate(make_snapshot())
    diagnostics = controller.diagnostics()
    assert decision.rollout_stage == "shadow"
    assert decision.safe_worker_cap == 16
    assert diagnostics["candidate_observation_count"] == 0
    assert diagnostics["candidate_pass_count"] == 0
    assert diagnostics["validation_contract_hash"] == TIER3_VALIDATION_CONTRACT_HASH
    assert diagnostics["state_migration_reason"] == "validation_contract_changed"
    assert "retained-root" in controller.source_diagnostics()["roots"]


def test_probe_endpoint_storm_marks_facility_limited_and_freezes_cap_16() -> None:
    controller = _mature_canary_controller()
    decision = controller.evaluate(
        make_snapshot(
            now_monotonic=100.0,
            pi_desired_workers=24,
            active_workers=16,
            probe_endpoint_limited=True,
        )
    )
    assert decision.reachability_class is Tier3ReachabilityClass.FACILITY_LIMITED
    assert decision.safe_worker_cap == 16
    assert decision.desired_workers <= 16
    assert decision.canary_state == "probe_endpoint_limited"
