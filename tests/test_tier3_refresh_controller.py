import unittest
from dataclasses import replace

from tier3_refresh_controller import (
    Tier3ControllerDecision,
    Tier3LaneServiceMetrics,
    Tier3PipelineWorkSnapshot,
    Tier3PIController,
    Tier3ValidatorEnvelopeController,
    tier3_pipeline_fast_supply_state,
    tier3_candidate_handoff_window,
    apply_tier3_bounded_lane_correction,
    tier3_candidate_credit_escape_budget,
    tier3_canonical_probe_priority,
    tier3_initial_validation_lane,
    tier3_operational_validation_supply_watermarks,
    tier3_initial_candidate_coalesce_pending,
    tier3_primary_probe_endpoint,
    tier3_process_envelope_target,
    tier3_probe_portfolio_limited,
    tier3_dual_threshold_errors,
    tier3_validation_fresh_pipeline_headroom,
    tier3_validation_supply_pause_state,
    tier3_tcp_responsive_reservation_ratio,
    tier3_tcp_responsive_probability_lcb,
    tier3_validator_worker_dispatch_lane,
    tier3_validator_worker_lane,
    tier3_refresh_plan,
    tier3_validation_budget,
)


def test_initial_candidate_coalesce_waits_for_cold_diversity_only() -> None:
    assert tier3_initial_candidate_coalesce_pending(
        first_candidate_at=10.0,
        now=10.1,
        first_tcp_enqueued=False,
        trusted_replay_inventory=0,
        ready_bucket_count=1,
        wave_capacity=4,
    )
    assert not tier3_initial_candidate_coalesce_pending(
        first_candidate_at=10.0,
        now=10.1,
        first_tcp_enqueued=False,
        trusted_replay_inventory=0,
        ready_bucket_count=4,
        wave_capacity=4,
    )
    assert not tier3_initial_candidate_coalesce_pending(
        first_candidate_at=10.0,
        now=10.251,
        first_tcp_enqueued=False,
        trusted_replay_inventory=0,
        ready_bucket_count=1,
        wave_capacity=4,
    )


def test_initial_candidate_coalesce_never_delays_trusted_replay() -> None:
    assert not tier3_initial_candidate_coalesce_pending(
        first_candidate_at=10.0,
        now=10.01,
        first_tcp_enqueued=False,
        trusted_replay_inventory=1,
        ready_bucket_count=1,
        wave_capacity=4,
    )


class Tier3RefreshControllerTests(unittest.TestCase):
    def test_source_unit_launch_debt_is_actionable_before_candidates_arrive(self) -> None:
        work = Tier3PipelineWorkSnapshot(
            sampled_monotonic=1.0,
            healthy_count=0,
            active_helpers=4,
            source_unit_launch_debt=6,
            source_unit_parse_valid_supply=0,
        )

        self.assertTrue(work.actionable_work)

    def test_pipeline_fast_supply_excludes_actor_owned_work(self) -> None:
        self.assertEqual(
            tier3_pipeline_fast_supply_state(
                "blocked",
                downstream_imminent_count=0,
            ),
            "blocked",
        )
        self.assertEqual(
            tier3_pipeline_fast_supply_state(
                "blocked",
                downstream_imminent_count=1,
            ),
            "imminent",
        )
        self.assertEqual(
            tier3_pipeline_fast_supply_state(
                "ready",
                downstream_imminent_count=0,
            ),
            "ready",
        )

    def test_process_fresh_pipeline_uses_rolling_real_headroom(self) -> None:
        self.assertEqual(
            tier3_validation_fresh_pipeline_headroom(
                actor_fresh_depth=40,
                tcp_pending_batches=2,
                tcp_active_batches=1,
                wave_capacity=4,
                high_water=64,
            ),
            12,
        )
        # Exact lease-node truth must override the conservative
        # batches-times-max-wave compatibility estimate.
        self.assertEqual(
            tier3_validation_fresh_pipeline_headroom(
                actor_fresh_depth=40,
                tcp_pending_batches=2,
                tcp_active_batches=1,
                wave_capacity=4,
                high_water=64,
                tcp_inflight_nodes=3,
            ),
            21,
        )
        self.assertEqual(
            tier3_validation_fresh_pipeline_headroom(
                actor_fresh_depth=60,
                tcp_pending_batches=1,
                tcp_active_batches=0,
                wave_capacity=4,
                high_water=64,
            ),
            0,
        )

        # Once the live TCP evidence says every candidate is responsive, the
        # same in-flight work reserves its complete validation capacity.
        self.assertEqual(
            tier3_validation_fresh_pipeline_headroom(
                actor_fresh_depth=60,
                tcp_pending_batches=1,
                tcp_active_batches=0,
                wave_capacity=4,
                high_water=64,
                responsive_reservation_ratio=1.0,
            ),
            0,
        )

    def test_validation_supply_tracks_operational_helper_ramp(self) -> None:
        self.assertEqual(
            tier3_operational_validation_supply_watermarks(
                configured_low_water=32,
                configured_high_water=64,
                operational_helpers=0,
                wave_capacity=4,
            ),
            (0, 4),
        )
        self.assertEqual(
            tier3_operational_validation_supply_watermarks(
                configured_low_water=32,
                configured_high_water=64,
                operational_helpers=7,
                wave_capacity=4,
            ),
            (7, 11),
        )
        self.assertEqual(
            tier3_operational_validation_supply_watermarks(
                configured_low_water=32,
                configured_high_water=64,
                operational_helpers=16,
                wave_capacity=4,
            ),
            (16, 20),
        )

    def test_fresh_supply_excludes_deep_capacity_and_tracks_carryover(self) -> None:
        for operational, desired_fast, active_fast, active_deep, expected in (
            (16, 12, 12, 4, (12, 16)),
            (16, 15, 15, 1, (15, 19)),
            (16, 15, 8, 8, (8, 12)),
            (16, 12, 14, 2, (14, 18)),
            (4, 12, 4, 0, (4, 8)),
            (0, 12, 0, 0, (0, 4)),
        ):
            with self.subTest(
                operational=operational, desired_fast=desired_fast,
                active_fast=active_fast, active_deep=active_deep,
            ):
                self.assertEqual(
                    tier3_operational_validation_supply_watermarks(
                        configured_low_water=32, configured_high_water=64,
                        operational_helpers=operational, wave_capacity=4,
                        desired_fast_slots=desired_fast,
                        active_fast=active_fast, active_deep=active_deep,
                    ),
                    expected,
                )

    def test_tcp_responsive_reservation_starts_unproven_and_converges(self) -> None:
        self.assertEqual(
            tier3_tcp_responsive_reservation_ratio(
                tcp_scanned=0,
                tcp_responsive=0,
                wave_capacity=4,
            ),
            1.0,
        )
        low_yield = tier3_tcp_responsive_reservation_ratio(
            tcp_scanned=100,
            tcp_responsive=20,
            wave_capacity=4,
        )
        high_yield = tier3_tcp_responsive_reservation_ratio(
            tcp_scanned=100,
            tcp_responsive=90,
            wave_capacity=4,
        )
        self.assertGreater(low_yield, 0.25)
        self.assertLess(low_yield, 0.5)
        self.assertGreater(high_yield, low_yield)
        self.assertLessEqual(high_yield, 1.0)

    def test_first_revision_cannot_prefetch_multiple_fast_cohorts(self) -> None:
        # A fast TCP wave can finish before the 250ms envelope publishes its
        # first response sample. Each handoff remains owned by TCP or FAST.
        ratio = tier3_tcp_responsive_reservation_ratio(
            tcp_scanned=0, tcp_responsive=0, wave_capacity=4,
        )
        for high in (8, 12, 16, 19):
            starts = min(16, high)
            pending_tcp = starts
            fresh = 0
            while pending_tcp:
                transferred = min(4, pending_tcp)
                pending_tcp -= transferred
                fresh += transferred
                headroom = tier3_validation_fresh_pipeline_headroom(
                    actor_fresh_depth=fresh, tcp_pending_batches=0,
                    tcp_active_batches=0, wave_capacity=4, high_water=high,
                    responsive_reservation_ratio=ratio,
                    tcp_inflight_nodes=pending_tcp,
                )
                refill = min(4, 16 - pending_tcp, headroom)
                pending_tcp += refill
                starts += refill
                self.assertLessEqual(fresh + pending_tcp, high)
            self.assertEqual(starts, high)

    def test_startup_capacity_prior_does_not_change_arrival_estimate(self) -> None:
        for completed in (0, 1, 4, 7):
            self.assertEqual(
                tier3_tcp_responsive_reservation_ratio(
                    tcp_scanned=completed, tcp_responsive=completed,
                    wave_capacity=4,
                ),
                1.0,
            )
        self.assertEqual(
            tier3_tcp_responsive_probability_lcb(tcp_scanned=0, tcp_responsive=0),
            0.25,
        )

    def test_low_yield_tcp_pipeline_keeps_frozen_baseline_breadth(self) -> None:
        ratio = tier3_tcp_responsive_reservation_ratio(
            tcp_scanned=100,
            tcp_responsive=20,
            wave_capacity=4,
        )

        # Twenty-four in-flight TCP waves represent 96 candidates. Treating
        # all of them as future responsive work reduces this headroom to zero
        # and reproduces the slow packaged runs. The live upper estimate keeps
        # a bounded 16-candidate handoff opportunity for later root/families.
        self.assertEqual(
            tier3_validation_fresh_pipeline_headroom(
                actor_fresh_depth=20,
                tcp_pending_batches=16,
                tcp_active_batches=8,
                wave_capacity=4,
                high_water=64,
                responsive_reservation_ratio=ratio,
            ),
            16,
        )
        self.assertEqual(
            tier3_validation_fresh_pipeline_headroom(
                actor_fresh_depth=20,
                tcp_pending_batches=16,
                tcp_active_batches=8,
                wave_capacity=4,
                high_water=64,
                responsive_reservation_ratio=1.0,
            ),
            0,
        )

    def test_candidate_credit_escape_budget_replays_21_of_25_stall(self) -> None:
        budget = tier3_candidate_credit_escape_budget(
            healthy_count=21,
            target_alive=25,
            fast_slots=15,
            active_fast=0,
            ready_fast=0,
            fresh_candidate_backlog=1129,
            ready_arm_count=41,
            normal_dispatchable_arm_count=0,
            credit_blocked_arm_count=41,
            overdue_credit_bucket_count=41,
        )

        self.assertEqual(budget, 3)
        self.assertEqual(
            tier3_candidate_credit_escape_budget(
                healthy_count=25,
                target_alive=25,
                fast_slots=15,
                active_fast=0,
                ready_fast=0,
                fresh_candidate_backlog=1129,
                ready_arm_count=41,
                normal_dispatchable_arm_count=0,
                credit_blocked_arm_count=41,
                overdue_credit_bucket_count=41,
            ),
            0,
        )
        self.assertEqual(
            tier3_candidate_credit_escape_budget(
                healthy_count=21,
                target_alive=25,
                fast_slots=15,
                active_fast=0,
                ready_fast=0,
                fresh_candidate_backlog=1129,
                ready_arm_count=41,
                normal_dispatchable_arm_count=1,
                credit_blocked_arm_count=40,
                overdue_credit_bucket_count=40,
            ),
            3,
        )
        # Actor ready capacity can be full while a TCP worker releases a real
        # handoff opportunity. That opportunity must still reserve its fair
        # escape share for an overdue root/family.
        self.assertEqual(
            tier3_candidate_credit_escape_budget(
                healthy_count=21,
                target_alive=25,
                fast_slots=15,
                active_fast=15,
                ready_fast=32,
                fresh_candidate_backlog=1129,
                ready_arm_count=41,
                normal_dispatchable_arm_count=20,
                credit_blocked_arm_count=21,
                overdue_credit_bucket_count=1,
            ),
            1,
        )
        self.assertEqual(
            tier3_candidate_credit_escape_budget(
                healthy_count=21,
                target_alive=25,
                fast_slots=15,
                active_fast=0,
                ready_fast=0,
                fresh_candidate_backlog=1129,
                ready_arm_count=41,
                normal_dispatchable_arm_count=0,
                credit_blocked_arm_count=41,
                overdue_credit_bucket_count=0,
            ),
            3,
        )

    def test_pipeline_snapshot_replays_14_of_25_credit_stall_in_one_revision(self) -> None:
        controller = Tier3PIController(
            {
                "target_alive": 25,
                "kp": 0.5,
                "ki": 0.0,
                "min_intensity": 0.0,
            }
        )
        work = Tier3PipelineWorkSnapshot(
            sampled_monotonic=20.0,
            healthy_count=14,
            active_helpers=16,
            candidate_fresh_backlog=499,
            candidate_ready_arm_count=31,
            candidate_normal_dispatchable_arm_count=0,
            candidate_credit_blocked_arm_count=31,
            candidate_normal_reservable_count=0,
            candidate_escape_eligible_bucket_count=4,
            fast_supply_state="blocked",
            candidate_credit_deadlock=True,
            fast_ready=0,
            fast_active=0,
            deep_parked=1300,
            deep_active=16,
            actor_fast_slots=15,
            actor_deep_slots=1,
            scheduler_saturation_state="arm_credit_saturated",
        )

        decision = controller.expansion_envelope(
            healthy_count=0,
            min_alive_nodes=3,
            target_alive=25,
            source_budget_min=1,
            source_budget_max=12,
            collect_interval_min=1,
            collect_interval_max=60,
            helper_min=4,
            helper_max=16,
            active_helpers=1,
            tcp_inflight_min=4,
            tcp_inflight_max=64,
            last_net_new_commit_monotonic=0.0,
            work_snapshot=work,
        )

        self.assertEqual(decision.healthy_count, 14)
        self.assertEqual(decision.pressure, 1.0)
        self.assertEqual(decision.desired_helpers, 16)
        self.assertEqual(decision.fresh_candidate_backlog, 499)
        self.assertFalse(decision.expected_fast_supply)
        self.assertEqual(decision.fast_supply_state, "blocked")
        self.assertTrue(decision.candidate_credit_deadlock)
        self.assertGreater(decision.credit_escape_budget, 0)
        self.assertEqual(decision.saturation_state, "candidate_credit_recovering")

    def test_dual_threshold_errors_cover_critical_expansion_and_maintenance(self) -> None:
        expected_states = {
            0: (1.0, 1.0),
            3: (22 / 25, 1.0),
            4: (21 / 25, 2 / 3),
            5: (20 / 25, 1 / 3),
            6: (19 / 25, 0.0),
            13: (12 / 25, 0.0),
            24: (1 / 25, 0.0),
            25: (0.0, 0.0),
        }
        previous_effective = 1.0
        for healthy, (target_error, critical_error) in expected_states.items():
            actual_target, actual_critical, effective = tier3_dual_threshold_errors(
                healthy_count=healthy,
                min_alive_nodes=3,
                target_alive=25,
            )
            self.assertAlmostEqual(actual_target, target_error)
            self.assertAlmostEqual(actual_critical, critical_error)
            self.assertLessEqual(effective, previous_effective)
            previous_effective = effective

    def test_expansion_envelope_budgets_are_monotonic_and_gate_independent(self) -> None:
        config = {
            "target_alive": 25,
            "kp": 1.0,
            "ki": 0.0,
            "min_intensity": 0.0,
        }

        def envelope(healthy: int):
            return Tier3PIController(dict(config)).expansion_envelope(
                healthy_count=healthy,
                min_alive_nodes=3,
                target_alive=25,
                source_budget_min=1,
                source_budget_max=12,
                collect_interval_min=1,
                collect_interval_max=60,
                helper_min=4,
                helper_max=16,
                active_helpers=4,
                tcp_inflight_min=4,
                tcp_inflight_max=64,
                now_monotonic=1.0,
            )

        decisions = [envelope(value) for value in (0, 3, 4, 5, 6, 13, 24, 25)]
        self.assertEqual(decisions[0].desired_helpers, 16)
        self.assertEqual(decisions[0].resident_transaction_limit, 160)
        self.assertEqual(decisions[0].ready_work_limit, 32)
        for higher_pressure, lower_pressure in zip(decisions, decisions[1:]):
            self.assertGreaterEqual(higher_pressure.pressure, lower_pressure.pressure)
            self.assertLessEqual(
                higher_pressure.tcp_transport_timeout_seconds,
                lower_pressure.tcp_transport_timeout_seconds,
            )
            self.assertGreaterEqual(higher_pressure.desired_helpers, lower_pressure.desired_helpers)
            self.assertGreaterEqual(
                higher_pressure.resident_transaction_limit,
                lower_pressure.resident_transaction_limit,
            )
            self.assertGreaterEqual(higher_pressure.ready_work_limit, lower_pressure.ready_work_limit)
            self.assertGreaterEqual(higher_pressure.source_refill_tokens, lower_pressure.source_refill_tokens)
            self.assertGreaterEqual(
                higher_pressure.source_unit_budget,
                lower_pressure.source_unit_budget,
            )
            self.assertEqual(
                higher_pressure.source_unit_concurrency,
                higher_pressure.source_unit_budget,
            )
            self.assertGreaterEqual(
                higher_pressure.source_unit_transport_limit,
                lower_pressure.source_unit_transport_limit,
            )
            self.assertGreaterEqual(higher_pressure.tcp_inflight_limit, lower_pressure.tcp_inflight_limit)
        self.assertEqual(decisions[0].source_unit_budget, 12)
        self.assertEqual(decisions[0].source_unit_concurrency, 12)
        self.assertEqual(decisions[0].source_unit_transport_limit, 48)
        self.assertEqual(decisions[1].state, "critical")
        self.assertEqual(decisions[-1].state, "maintenance")
        self.assertFalse(decisions[-1].expansion_active)

    def test_running_envelope_shortens_only_new_tcp_entitlements_as_debt_rises(self) -> None:
        controller = Tier3PIController(
            {
                "target_alive": 25,
                "kp": 0.5,
                "ki": 0.0,
                "min_intensity": 0.0,
            }
        )
        common = {
            "healthy_count": 22,
            "min_alive_nodes": 3,
            "target_alive": 25,
            "source_budget_min": 1,
            "source_budget_max": 12,
            "collect_interval_min": 1,
            "collect_interval_max": 60,
            "helper_min": 4,
            "helper_max": 16,
            "active_helpers": 9,
            "tcp_inflight_min": 4,
            "tcp_inflight_max": 16,
            "actionable_work": True,
            "last_net_new_commit_monotonic": 10.0,
            "tcp_transport_base_timeout_seconds": 5.0,
            "tcp_transport_timeout_floor_seconds": 1.25,
        }

        warm = controller.expansion_envelope(now_monotonic=10.0, **common)
        full_pressure = controller.expansion_envelope(now_monotonic=16.0, **common)

        self.assertGreater(warm.tcp_transport_timeout_seconds, 1.25)
        self.assertEqual(full_pressure.pressure, 1.0)
        self.assertEqual(full_pressure.tcp_transport_timeout_seconds, 1.25)
        self.assertGreaterEqual(
            full_pressure.tcp_inflight_limit,
            warm.tcp_inflight_limit,
        )

    def test_target_unmet_commit_cannot_reduce_full_chain_pressure(self) -> None:
        controller = Tier3PIController(
            {
                "target_alive": 25,
                "kp": 0.5,
                "ki": 0.0,
                "min_intensity": 0.0,
                "source_budget_min": 1,
                "source_budget_max": 16,
            }
        )
        actionable = Tier3PipelineWorkSnapshot(
            sampled_monotonic=20.0,
            healthy_count=3,
            active_helpers=16,
            candidate_fresh_backlog=128,
            candidate_normal_reservable_count=64,
            fast_supply_state="ready",
            actor_fast_slots=15,
        )
        common = {
            "min_alive_nodes": 3,
            "target_alive": 25,
            "source_budget_min": 1,
            "source_budget_max": 16,
            "collect_interval_min": 1,
            "collect_interval_max": 60,
            "helper_min": 4,
            "helper_max": 16,
            "tcp_inflight_min": 4,
            "tcp_inflight_max": 16,
            "tcp_transport_base_timeout_seconds": 5.0,
            "tcp_transport_timeout_floor_seconds": 1.25,
        }
        saturated = controller.expansion_envelope(
            healthy_count=3,
            active_helpers=16,
            last_net_new_commit_monotonic=0.0,
            now_monotonic=20.0,
            work_snapshot=actionable,
            **common,
        )
        after_commit = controller.expansion_envelope(
            healthy_count=22,
            active_helpers=16,
            last_net_new_commit_monotonic=20.24,
            now_monotonic=20.25,
            work_snapshot=replace(
                actionable,
                sampled_monotonic=20.25,
                healthy_count=22,
            ),
            **common,
        )

        self.assertEqual(saturated.pressure, 1.0)
        self.assertEqual(after_commit.pressure, 1.0)
        self.assertEqual(after_commit.desired_helpers, 16)
        self.assertEqual(after_commit.tcp_inflight_limit, 16)
        self.assertEqual(after_commit.tcp_transport_timeout_seconds, 1.25)
        self.assertGreaterEqual(
            after_commit.scan_token_capacity,
            saturated.scan_token_capacity,
        )
        self.assertGreaterEqual(
            after_commit.handoff_window_limit,
            saturated.handoff_window_limit,
        )

    def test_expansion_envelope_applies_target_hot_updates_without_pool_mutation(self) -> None:
        controller = Tier3PIController(
            {
                "target_alive": 15,
                "kp": 1.0,
                "ki": 0.0,
                "min_intensity": 0.0,
            }
        )
        common = {
            "healthy_count": 13,
            "min_alive_nodes": 3,
            "source_budget_min": 1,
            "source_budget_max": 12,
            "collect_interval_min": 1,
            "collect_interval_max": 60,
            "helper_min": 1,
            "helper_max": 16,
            "active_helpers": 4,
            "tcp_inflight_min": 4,
            "tcp_inflight_max": 64,
        }
        lower_target = controller.expansion_envelope(
            target_alive=15,
            now_monotonic=1.0,
            **common,
        )

        controller.config["target_alive"] = 25
        raised_target = controller.expansion_envelope(
            target_alive=25,
            now_monotonic=2.0,
            **common,
        )

        self.assertEqual(lower_target.healthy_count, 13)
        self.assertEqual(raised_target.healthy_count, 13)
        self.assertGreater(raised_target.pressure, lower_target.pressure)
        self.assertGreaterEqual(
            raised_target.desired_helpers,
            lower_target.desired_helpers,
        )
        self.assertGreaterEqual(
            raised_target.resident_transaction_limit,
            lower_target.resident_transaction_limit,
        )

    def test_expansion_envelope_delivery_debt_only_raises_pressure_and_holds_helpers(self) -> None:
        controller = Tier3PIController(
            {
                "target_alive": 25,
                "kp": 0.5,
                "ki": 0.0,
                "min_intensity": 0.0,
            }
        )
        common = {
            "healthy_count": 16,
            "min_alive_nodes": 3,
            "target_alive": 25,
            "source_budget_min": 1,
            "source_budget_max": 12,
            "collect_interval_min": 1,
            "collect_interval_max": 60,
            "helper_min": 4,
            "helper_max": 16,
            "active_helpers": 16,
            "tcp_inflight_min": 64,
            "tcp_inflight_max": 256,
            "scan_session_budget": 500,
            "last_net_new_commit_monotonic": 0.0,
        }

        idle = controller.expansion_envelope(
            actionable_work=False,
            now_monotonic=5.75,
            **common,
        )
        active = controller.expansion_envelope(
            actionable_work=True,
            now_monotonic=5.75,
            **common,
        )

        self.assertEqual(idle.delivery_debt, 0.0)
        self.assertEqual(active.delivery_debt, 1.0)
        self.assertGreaterEqual(active.pressure, idle.pressure)
        self.assertEqual(active.pressure, 1.0)
        self.assertEqual(active.desired_helpers, 16)
        self.assertEqual(
            active.helper_hold_reason,
            "below_target_actionable_work",
        )
        self.assertFalse(active.candidate_scan_lifetime_cap_enabled)
        self.assertEqual(active.handoff_window_limit, 1024)
        self.assertEqual(active.scan_token_capacity, 1024)
        self.assertEqual(active.scan_token_refill, 1024)

    def test_target_unmet_actionable_work_holds_full_tcp_after_ramp(self) -> None:
        controller = Tier3PIController(
            {
                "target_alive": 25,
                "kp": 0.5,
                "ki": 0.0,
                "min_intensity": 0.0,
            }
        )
        work = Tier3PipelineWorkSnapshot(
            sampled_monotonic=100.0,
            healthy_count=12,
            active_helpers=16,
            candidate_fresh_backlog=4866,
            candidate_normal_reservable_count=64,
            fast_supply_state="ready",
            actor_fast_slots=15,
        )

        decision = controller.expansion_envelope(
            healthy_count=12,
            min_alive_nodes=3,
            target_alive=25,
            source_budget_min=1,
            source_budget_max=12,
            collect_interval_min=1,
            collect_interval_max=60,
            helper_min=4,
            helper_max=16,
            active_helpers=16,
            tcp_inflight_min=3,
            tcp_inflight_max=16,
            last_net_new_commit_monotonic=100.0,
            now_monotonic=100.0,
            work_snapshot=work,
        )

        self.assertLess(decision.pressure, 1.0)
        self.assertEqual(decision.desired_helpers, 16)
        self.assertEqual(decision.tcp_inflight_limit, 16)
        self.assertEqual(
            decision.resource_hold_reason,
            "target_unmet_actionable",
        )

    def test_tcp_handoff_window_expands_from_n_to_four_n(self) -> None:
        self.assertEqual(
            tier3_candidate_handoff_window(
                tcp_inflight_limit=16,
                free_fast_slots=12,
                pressure=0.0,
                responsive_probability_lcb=0.25,
            ),
            16,
        )
        self.assertEqual(
            tier3_candidate_handoff_window(
                tcp_inflight_limit=16,
                free_fast_slots=12,
                pressure=1.0,
                responsive_probability_lcb=0.25,
            ),
            64,
        )
        self.assertEqual(
            tier3_candidate_handoff_window(
                tcp_inflight_limit=16,
                free_fast_slots=12,
                pressure=1.0,
                responsive_probability_lcb=0.0,
            ),
            64,
        )
        self.assertEqual(
            tier3_candidate_handoff_window(
                tcp_inflight_limit=16,
                free_fast_slots=12,
                pressure=1.0,
                responsive_probability_lcb=0.75,
            ),
            22,
        )

    def test_tcp_responsive_lcb_is_conservative_and_evidence_driven(self) -> None:
        self.assertEqual(
            tier3_tcp_responsive_probability_lcb(
                tcp_scanned=0,
                tcp_responsive=0,
            ),
            0.25,
        )
        low = tier3_tcp_responsive_probability_lcb(
            tcp_scanned=64,
            tcp_responsive=16,
        )
        high = tier3_tcp_responsive_probability_lcb(
            tcp_scanned=64,
            tcp_responsive=48,
        )
        self.assertGreater(low, 0.0)
        self.assertLess(low, 0.25)
        self.assertGreater(high, low)

    def test_expansion_envelope_gate_state_does_not_change_delivery_floor(self) -> None:
        config = {
            "target_alive": 25,
            "kp": 1.0,
            "ki": 0.0,
            "min_intensity": 0.0,
        }
        common = {
            "healthy_count": 3,
            "min_alive_nodes": 3,
            "target_alive": 25,
            "source_budget_min": 1,
            "source_budget_max": 12,
            "collect_interval_min": 1,
            "collect_interval_max": 60,
            "helper_min": 4,
            "helper_max": 16,
            "active_helpers": 16,
            "tcp_inflight_min": 64,
            "tcp_inflight_max": 256,
            "actionable_work": True,
            "last_net_new_commit_monotonic": 0.0,
            "now_monotonic": 2.0,
        }

        before_gate = Tier3PIController(dict(config)).expansion_envelope(**common)
        after_gate = Tier3PIController(dict(config)).expansion_envelope(**common)

        self.assertEqual(
            before_gate.__dict__ | {"revision": 0},
            after_gate.__dict__ | {"revision": 0},
        )

    def test_pi_reuses_one_revision_inside_250ms_control_window(self) -> None:
        controller = Tier3PIController(
            {
                "target_alive": 25,
                "kp": 0.7,
                "ki": 0.3,
                "min_intensity": 0.0,
            }
        )
        common = {
            "healthy_count": 8,
            "min_alive_nodes": 3,
            "target_alive": 25,
            "source_budget_min": 1,
            "source_budget_max": 12,
            "collect_interval_min": 1,
            "collect_interval_max": 60,
            "helper_min": 4,
            "helper_max": 16,
            "active_helpers": 8,
            "tcp_inflight_min": 8,
            "tcp_inflight_max": 64,
            "actionable_work": True,
        }

        first = controller.expansion_envelope(now_monotonic=10.0, **common)
        first_integral = controller.integral
        duplicate = controller.expansion_envelope(now_monotonic=10.1, **common)

        self.assertIs(duplicate, first)
        self.assertEqual(duplicate.revision, first.revision)
        self.assertEqual(controller.integral, first_integral)

        next_window = controller.expansion_envelope(
            now_monotonic=10.25,
            **common,
        )
        self.assertEqual(next_window.revision, first.revision + 1)
        self.assertGreater(controller.integral, first_integral)

    def test_validation_supply_watermarks_pause_and_resume_with_hysteresis(self) -> None:
        self.assertFalse(
            tier3_validation_supply_pause_state(
                currently_paused=False,
                supply_depth=31,
                low_water=16,
                high_water=32,
            )
        )
        self.assertTrue(
            tier3_validation_supply_pause_state(
                currently_paused=False,
                supply_depth=32,
                low_water=16,
                high_water=32,
            )
        )
        self.assertTrue(
            tier3_validation_supply_pause_state(
                currently_paused=True,
                supply_depth=17,
                low_water=16,
                high_water=32,
            )
        )
        self.assertFalse(
            tier3_validation_supply_pause_state(
                currently_paused=True,
                supply_depth=16,
                low_water=16,
                high_water=32,
            )
        )

    def test_process_envelope_target_keeps_sampling_sticky_but_applies_canary_transitions(self) -> None:
        self.assertEqual(
            tier3_process_envelope_target(
                cached_target=16,
                risk_desired=18,
                active_count=16,
                interval_due=False,
                canary_state="testing_18",
            ),
            (18, True),
        )
        self.assertEqual(
            tier3_process_envelope_target(
                cached_target=18,
                risk_desired=16,
                active_count=18,
                interval_due=False,
                canary_state="stable",
            ),
            (16, True),
        )
        self.assertEqual(
            tier3_process_envelope_target(
                cached_target=16,
                risk_desired=18,
                active_count=16,
                interval_due=False,
                canary_state="stable",
            ),
            (16, False),
        )

    def test_fresh_validation_dispatch_always_uses_primary_ingress(self) -> None:
        counts = {"primary": 0, "canonical": 0}
        trace: list[str] = []
        for _ in range(10):
            lane = tier3_initial_validation_lane(
                primary_dispatched=counts["primary"],
                canonical_dispatched=counts["canonical"],
                canonical_share=0.60,
            )
            counts[lane] += 1
            trace.append(lane)

        self.assertEqual(counts, {"primary": 10, "canonical": 0})
        self.assertEqual(trace, ["primary"] * 10)

    def test_validator_envelope_scales_up_immediately_and_down_with_hysteresis(self) -> None:
        envelope = Tier3ValidatorEnvelopeController(
            minimum_count=1,
            maximum_count=16,
            scale_down_samples=3,
            scale_down_step=2,
        )

        high = envelope.update(16, primary_backlog=True, canonical_backlog=True)
        self.assertEqual(high.active_count, 16)
        self.assertEqual(high.primary_slots, 10)
        self.assertEqual(high.canonical_slots, 6)
        self.assertEqual(high.allocation_reason, "dual_lane_60_40")

        first = envelope.update(4, primary_backlog=True, canonical_backlog=True)
        second = envelope.update(4, primary_backlog=True, canonical_backlog=True)
        third = envelope.update(4, primary_backlog=True, canonical_backlog=True)
        self.assertEqual(first.active_count, 16)
        self.assertEqual(second.active_count, 16)
        self.assertEqual(third.active_count, 14)

    def test_process_domain_envelope_controls_only_worker_count(self) -> None:
        envelope = Tier3ValidatorEnvelopeController(
            minimum_count=4,
            maximum_count=16,
            initial_count=4,
            scale_up_step=2,
        )

        first = envelope.update_process_domain(16, canary_level=16)
        second = envelope.update_process_domain(16, canary_level=16)

        self.assertEqual(first.active_count, 6)
        self.assertEqual(second.active_count, 8)
        self.assertEqual(first.primary_slots, first.active_count)
        self.assertEqual(first.canonical_slots, 0)
        self.assertEqual(first.retry_slots, 0)
        self.assertEqual(first.allocation_reason, "process_domain_actor_drr")

    def test_validator_envelope_work_steals_when_only_one_lane_has_backlog(self) -> None:
        envelope = Tier3ValidatorEnvelopeController(maximum_count=16)
        envelope.update(16, primary_backlog=True, canonical_backlog=True)

        primary_only = envelope.update(
            16,
            primary_backlog=True,
            canonical_backlog=False,
        )
        canonical_only = envelope.update(
            16,
            primary_backlog=False,
            canonical_backlog=True,
        )

        self.assertEqual((primary_only.primary_slots, primary_only.canonical_slots), (16, 0))
        self.assertEqual((canonical_only.primary_slots, canonical_only.canonical_slots), (0, 16))

    def test_validator_envelope_enters_60_40_immediately_when_canonical_arrives(self) -> None:
        envelope = Tier3ValidatorEnvelopeController(
            maximum_count=16,
            initial_count=16,
        )
        primary_only = envelope.update(
            16,
            primary_backlog=True,
            canonical_backlog=False,
        )
        dual_lane = envelope.update(
            16,
            primary_backlog=True,
            canonical_backlog=True,
            primary_depth=128,
            canonical_depth=16,
            primary_oldest_wait_ms=5000,
            canonical_oldest_wait_ms=0,
        )

        self.assertEqual((primary_only.primary_slots, primary_only.canonical_slots), (16, 0))
        self.assertEqual((dual_lane.primary_slots, dual_lane.canonical_slots), (10, 6))
        self.assertEqual(dual_lane.allocation_reason, "dual_lane_60_40")

    def test_validator_envelope_can_start_at_pi_baseline_then_scale_only_by_two(self) -> None:
        envelope = Tier3ValidatorEnvelopeController(
            minimum_count=1,
            maximum_count=24,
            initial_count=16,
            scale_up_step=2,
        )

        self.assertEqual(envelope.active_count, 16)
        first = envelope.update(24, primary_backlog=True, canonical_backlog=True)
        second = envelope.update(24, primary_backlog=True, canonical_backlog=True)

        self.assertEqual(first.active_count, 18)
        self.assertEqual(second.active_count, 20)

    def test_validator_envelope_moves_one_slot_after_two_canonical_yield_samples(self) -> None:
        envelope = Tier3ValidatorEnvelopeController(
            minimum_count=1,
            maximum_count=16,
            initial_count=16,
        )
        primary = Tier3LaneServiceMetrics(
            completed_count=16,
            definitive_count=16,
            pass_count=3,
            occupied_ms=49_600,
        )
        canonical = Tier3LaneServiceMetrics(
            completed_count=16,
            definitive_count=16,
            pass_count=12,
            occupied_ms=96_000,
        )

        first = envelope.update(
            16,
            primary_backlog=True,
            canonical_backlog=True,
            primary_depth=24,
            canonical_depth=24,
            primary_service=primary,
            canonical_service=canonical,
            now_monotonic=0.0,
        )
        second = envelope.update(
            16,
            primary_backlog=True,
            canonical_backlog=True,
            primary_depth=24,
            canonical_depth=24,
            primary_service=primary,
            canonical_service=canonical,
            now_monotonic=0.25,
        )

        self.assertEqual(first.primary_slots, 10)
        self.assertEqual(second.primary_slots, 9)
        self.assertEqual(second.canonical_slots, 7)
        self.assertEqual(second.allocation_reason, "canonical_yield_or_wait_advantage")

    def test_validator_envelope_keeps_evidence_poor_lanes_in_stable_band(self) -> None:
        envelope = Tier3ValidatorEnvelopeController(
            minimum_count=1,
            maximum_count=20,
            initial_count=20,
        )
        weak_primary = Tier3LaneServiceMetrics(
            completed_count=64,
            pass_count=1,
            occupied_ms=64_000,
        )
        strong_canonical = Tier3LaneServiceMetrics(
            completed_count=64,
            pass_count=20,
            occupied_ms=64_000,
        )

        for sample in range(8):
            decision = envelope.update(
                20,
                primary_backlog=True,
                canonical_backlog=True,
                primary_depth=100,
                canonical_depth=100,
                primary_oldest_wait_ms=0.0,
                canonical_oldest_wait_ms=5_000.0,
                primary_service=weak_primary,
                canonical_service=strong_canonical,
                now_monotonic=float(sample),
            )

        self.assertGreaterEqual(decision.primary_slots, 8)
        self.assertLessEqual(decision.primary_slots, 12)
        self.assertGreaterEqual(decision.canonical_slots, 8)
        self.assertLessEqual(decision.canonical_slots, 12)

    def test_validator_envelope_moves_at_most_one_slot_per_second(self) -> None:
        envelope = Tier3ValidatorEnvelopeController(
            minimum_count=1,
            maximum_count=16,
            initial_count=16,
        )
        primary = Tier3LaneServiceMetrics(
            completed_count=32,
            pass_count=3,
            occupied_ms=32_000,
        )
        canonical = Tier3LaneServiceMetrics(
            completed_count=32,
            pass_count=20,
            occupied_ms=32_000,
        )

        decisions = []
        for now in (0.0, 0.1, 0.2, 0.3, 1.2):
            decisions.append(
                envelope.update(
                    16,
                    primary_backlog=True,
                    canonical_backlog=True,
                    primary_depth=100,
                    canonical_depth=100,
                    primary_service=primary,
                    canonical_service=canonical,
                    now_monotonic=now,
                )
            )

        self.assertEqual(decisions[1].primary_slots, 9)
        self.assertEqual(decisions[3].primary_slots, 9)
        self.assertEqual(decisions[4].primary_slots, 8)

    def test_validator_envelope_splits_canonical_fresh_and_resume_and_bounds_retry(self) -> None:
        envelope = Tier3ValidatorEnvelopeController(
            minimum_count=1,
            maximum_count=16,
            initial_count=16,
        )

        decision = envelope.update(
            16,
            primary_backlog=True,
            canonical_backlog=True,
            primary_depth=20,
            canonical_depth=20,
            canonical_fresh_depth=10,
            canonical_resume_depth=10,
            retry_depth=20,
        )

        self.assertEqual(decision.retry_slots, 0)
        self.assertEqual(decision.primary_slots + decision.canonical_slots + decision.retry_slots, 16)
        self.assertEqual(
            decision.canonical_fresh_slots + decision.canonical_resume_slots,
            decision.canonical_slots,
        )
        self.assertEqual(
            (decision.canonical_fresh_slots, decision.canonical_resume_slots),
            (4, 2),
        )
        self.assertGreaterEqual(decision.primary_slots, 4)
        self.assertGreaterEqual(decision.canonical_slots, 4)

        debt_decision = envelope.update(
            16,
            primary_backlog=True,
            canonical_backlog=True,
            primary_depth=20,
            canonical_depth=20,
            canonical_fresh_depth=10,
            canonical_resume_depth=10,
            retry_depth=20,
            main_completion_count=16,
        )
        self.assertEqual(debt_decision.retry_slots, 1)
        self.assertIn("retry_service_debt", debt_decision.allocation_reason)

        evidence_decision = envelope.update(
            16,
            primary_backlog=True,
            canonical_backlog=True,
            primary_depth=20,
            canonical_depth=20,
            canonical_fresh_depth=10,
            canonical_resume_depth=10,
            retry_depth=20,
            retry_service=Tier3LaneServiceMetrics(
                completed_count=8,
                definitive_count=8,
                pass_count=4,
                occupied_ms=8000.0,
            ),
            main_completion_count=17,
        )
        self.assertEqual(evidence_decision.retry_slots, 2)
        self.assertIn("retry_evidence", evidence_decision.allocation_reason)

    def test_worker_lane_mapping_exactly_consumes_envelope_decision(self) -> None:
        for workers in range(1, 25):
            envelope = Tier3ValidatorEnvelopeController(
                minimum_count=1,
                maximum_count=24,
                initial_count=workers,
            )
            decision = envelope.update(
                workers,
                primary_backlog=True,
                canonical_backlog=True,
                primary_depth=100,
                canonical_depth=100,
                retry_depth=100,
            )
            lanes = [
                tier3_validator_worker_lane(decision, index)
                for index in range(1, workers + 1)
            ]

            self.assertEqual(lanes.count("primary"), decision.primary_slots)
            self.assertEqual(lanes.count("canonical"), decision.canonical_slots)
            self.assertEqual(lanes.count("retry"), decision.retry_slots)
            self.assertIsNone(tier3_validator_worker_lane(decision, workers + 1))

    def test_worker_dispatch_mapping_executes_fresh_resume_allocation(self) -> None:
        envelope = Tier3ValidatorEnvelopeController(
            minimum_count=1,
            maximum_count=16,
            initial_count=16,
        )
        decision = envelope.update(
            16,
            primary_backlog=True,
            canonical_backlog=True,
            primary_depth=100,
            canonical_depth=100,
            canonical_fresh_depth=100,
            canonical_resume_depth=100,
        )

        lanes = [
            tier3_validator_worker_dispatch_lane(decision, worker_index)
            for worker_index in range(1, 17)
        ]

        self.assertEqual(lanes.count("primary"), decision.primary_slots)
        self.assertEqual(
            lanes.count("canonical_fresh"),
            decision.canonical_fresh_slots,
        )
        self.assertEqual(
            lanes.count("canonical_resume"),
            decision.canonical_resume_slots,
        )

    def test_overdue_canonical_resume_debt_temporarily_raises_deep_share(self) -> None:
        envelope = Tier3ValidatorEnvelopeController(
            minimum_count=1,
            maximum_count=16,
            initial_count=16,
        )

        decision = envelope.update(
            16,
            primary_backlog=True,
            canonical_backlog=True,
            primary_depth=100,
            canonical_depth=100,
            canonical_fresh_depth=100,
            canonical_resume_depth=100,
            canonical_fresh_oldest_wait_ms=2_000.0,
            canonical_resume_oldest_wait_ms=15_000.0,
        )

        self.assertEqual(
            (decision.canonical_fresh_slots, decision.canonical_resume_slots),
            (4, 2),
        )

    def test_overdue_canonical_resume_can_raise_share_only_with_yield_evidence(self) -> None:
        envelope = Tier3ValidatorEnvelopeController(
            minimum_count=1,
            maximum_count=16,
            initial_count=16,
        )

        decision = envelope.update(
            16,
            primary_backlog=True,
            canonical_backlog=True,
            primary_depth=100,
            canonical_depth=100,
            canonical_fresh_depth=100,
            canonical_resume_depth=100,
            canonical_fresh_oldest_wait_ms=2_000.0,
            canonical_resume_oldest_wait_ms=15_000.0,
            canonical_resume_service=Tier3LaneServiceMetrics(
                completed_count=8,
                definitive_count=8,
                pass_count=2,
                occupied_ms=64_000.0,
            ),
        )

        self.assertEqual(
            (decision.canonical_fresh_slots, decision.canonical_resume_slots),
            (3, 3),
        )

    def test_endpoint_policy_bounds_health_tilt_and_preserves_exploration(self) -> None:
        best = "http://best.example/generate_204"
        alternate = "http://alternate.example/generate_204"
        https = "https://probe.example/generate_204"
        stats = {
            best: {
                "attempted": 20,
                "root_count": 3,
                "protocol_count": 3,
                "success_throughput_lcb": 0.8,
                "limited": False,
            },
            alternate: {
                "attempted": 20,
                "root_count": 3,
                "protocol_count": 3,
                "success_throughput_lcb": 0.2,
                "limited": False,
            },
            https: {
                "attempted": 20,
                "root_count": 3,
                "protocol_count": 3,
                "success_throughput_lcb": 0.4,
                "limited": False,
            },
        }
        selections = [
            tier3_primary_probe_endpoint(
                [best, alternate, https],
                identity=f"candidate-{index}",
                endpoint_stats=stats,
            )
            for index in range(256)
        ]
        exploration_count = sum(1 for _url, exploration in selections if exploration)

        self.assertGreaterEqual(exploration_count, 50)
        self.assertLessEqual(exploration_count, 80)
        self.assertEqual(
            tier3_canonical_probe_priority(
                [best, alternate, https],
                attempted_urls={best},
                endpoint_stats=stats,
            ),
            [alternate, https],
        )

    def test_endpoint_policy_keeps_preference_when_only_best_is_not_degraded(self) -> None:
        best = "http://best.example/generate_204"
        degraded = "http://degraded.example/generate_204"
        stats = {
            best: {
                "attempted": 32,
                "root_count": 3,
                "protocol_count": 3,
                "success_throughput_lcb": 0.5,
                "limited": False,
            },
            degraded: {
                "attempted": 32,
                "root_count": 3,
                "protocol_count": 3,
                "success_throughput_lcb": 0.0,
                "limited": True,
            },
        }

        selections = [
            tier3_primary_probe_endpoint(
                [best, degraded],
                identity=f"candidate-{index}",
                endpoint_stats=stats,
            )[0]
            for index in range(512)
        ]
        best_share = selections.count(best) / len(selections)

        self.assertGreaterEqual(best_share, 0.85)
        self.assertLessEqual(best_share, 0.92)
        self.assertGreater(selections.count(degraded), 0)

    def test_canonical_priority_replays_positive_censored_endpoint_first(self) -> None:
        good = "http://good.example/generate_204"
        bad = "http://bad.example/generate_204"
        https = "https://probe.example/generate_204"

        ordered = tier3_canonical_probe_priority(
            [good, bad, https],
            attempted_urls={good},
            completed_urls=set(),
            exhausted_urls=set(),
            censored_urls={good},
            endpoint_stats={
                good: {"success_throughput_lcb": 0.25, "limited": False},
                bad: {"success_throughput_lcb": 0.0, "limited": False},
                https: {"success_throughput_lcb": 0.0, "limited": False},
            },
        )

        self.assertEqual(ordered, [good, bad, https])

    def test_canonical_zero_yield_collapses_to_debt_slot_then_recovers(self) -> None:
        envelope = Tier3ValidatorEnvelopeController(
            minimum_count=1,
            maximum_count=16,
            initial_count=16,
        )
        primary = Tier3LaneServiceMetrics(
            completed_count=32,
            pass_count=4,
            occupied_ms=60_000.0,
            net_new_count=4,
        )
        zero_yield = Tier3LaneServiceMetrics(
            completed_count=32,
            pass_count=0,
            occupied_ms=60_000.0,
            net_new_count=0,
        )

        collapsed = envelope.update(
            16,
            primary_backlog=True,
            canonical_backlog=True,
            primary_depth=100,
            canonical_depth=100,
            primary_service=primary,
            canonical_service=zero_yield,
            now_monotonic=1.0,
        )

        self.assertEqual(collapsed.canonical_slots, 1)
        self.assertEqual(collapsed.primary_slots, 15)
        self.assertEqual(collapsed.allocation_reason, "canonical_zero_yield_debt")

        recovering = envelope.update(
            16,
            primary_backlog=True,
            canonical_backlog=True,
            primary_depth=100,
            canonical_depth=100,
            primary_service=primary,
            canonical_service=Tier3LaneServiceMetrics(
                completed_count=33,
                pass_count=1,
                occupied_ms=64_000.0,
                net_new_count=1,
            ),
            now_monotonic=2.1,
        )
        self.assertEqual(recovering.canonical_slots, 2)
        self.assertEqual(recovering.allocation_reason, "canonical_yield_recovery")

        restored = envelope.update(
            16,
            primary_backlog=True,
            canonical_backlog=True,
            primary_depth=100,
            canonical_depth=100,
            primary_service=primary,
            canonical_service=Tier3LaneServiceMetrics(
                completed_count=35,
                pass_count=3,
                occupied_ms=70_000.0,
                net_new_count=3,
            ),
            now_monotonic=3.2,
        )
        self.assertGreaterEqual(restored.canonical_slots, 4)

    def test_primary_proven_yield_puts_unproven_canonical_on_one_slot_probation(self) -> None:
        envelope = Tier3ValidatorEnvelopeController(
            minimum_count=1,
            maximum_count=16,
            initial_count=16,
        )

        decision = envelope.update(
            16,
            primary_backlog=True,
            canonical_backlog=True,
            primary_depth=100,
            canonical_depth=100,
            primary_service=Tier3LaneServiceMetrics(
                completed_count=8,
                pass_count=3,
                occupied_ms=16_000.0,
                net_new_count=3,
                lifetime_completed_count=8,
            ),
            canonical_service=Tier3LaneServiceMetrics(
                completed_count=4,
                pass_count=0,
                occupied_ms=20_000.0,
                net_new_count=0,
                lifetime_completed_count=4,
            ),
            now_monotonic=5.0,
        )

        self.assertEqual((decision.primary_slots, decision.canonical_slots), (15, 1))
        self.assertEqual(decision.allocation_reason, "canonical_yield_probation")

    def test_single_primary_growth_triggers_early_canonical_probation(self) -> None:
        envelope = Tier3ValidatorEnvelopeController(
            minimum_count=1,
            maximum_count=16,
            initial_count=16,
        )

        decision = envelope.update(
            16,
            primary_backlog=True,
            canonical_backlog=True,
            primary_depth=100,
            canonical_depth=100,
            primary_service=Tier3LaneServiceMetrics(
                completed_count=8,
                pass_count=1,
                occupied_ms=16_000.0,
                net_new_count=1,
                lifetime_completed_count=8,
            ),
            canonical_service=Tier3LaneServiceMetrics(
                completed_count=4,
                pass_count=0,
                occupied_ms=20_000.0,
                net_new_count=0,
                lifetime_completed_count=4,
            ),
            now_monotonic=5.0,
        )

        self.assertEqual((decision.primary_slots, decision.canonical_slots), (15, 1))
        self.assertEqual(decision.allocation_reason, "canonical_yield_probation")

    def test_endpoint_policy_reduces_zero_yield_exploration_after_broad_evidence(self) -> None:
        best = "http://best.example/generate_204"
        zero_yield = "http://zero.example/generate_204"
        stats = {
            best: {
                "attempted": 32,
                "root_count": 3,
                "protocol_count": 3,
                "success_throughput_lcb": 0.5,
                "limited": False,
            },
            zero_yield: {
                "attempted": 32,
                "success": 0,
                "root_count": 3,
                "protocol_count": 3,
                "success_throughput_lcb": 0.0,
                "limited": False,
            },
        }

        selections = [
            tier3_primary_probe_endpoint(
                [best, zero_yield],
                identity=f"candidate-{index}",
                endpoint_stats=stats,
            )[0]
            for index in range(1024)
        ]
        exploration_share = selections.count(zero_yield) / len(selections)

        self.assertGreaterEqual(exploration_share, 0.10)
        self.assertLessEqual(exploration_share, 0.15)

    def test_probe_portfolio_is_limited_only_when_every_endpoint_is_limited(self) -> None:
        self.assertFalse(tier3_probe_portfolio_limited({}))
        self.assertFalse(
            tier3_probe_portfolio_limited(
                {
                    "http://bad.example/204": {"limited": True},
                    "http://good.example/204": {"limited": False},
                }
            )
        )
        self.assertTrue(
            tier3_probe_portfolio_limited(
                {
                    "http://bad-a.example/204": {"limited": True},
                    "http://bad-b.example/204": {"limited": True},
                }
            )
        )

    def test_audit_shaped_lane_replay_beats_fixed_fifty_fifty(self) -> None:
        envelope = Tier3ValidatorEnvelopeController(
            minimum_count=1,
            maximum_count=16,
            initial_count=16,
        )
        primary = Tier3LaneServiceMetrics(
            completed_count=32,
            definitive_count=4,
            pass_count=3,
            occupied_ms=32 * 3100,
        )
        canonical = Tier3LaneServiceMetrics(
            completed_count=32,
            definitive_count=24,
            pass_count=20,
            occupied_ms=32 * 6000,
        )
        adaptive = None
        for now in range(6):
            adaptive = envelope.update(
                16,
                primary_backlog=True,
                canonical_backlog=True,
                primary_depth=64,
                canonical_depth=64,
                primary_service=primary,
                canonical_service=canonical,
                now_monotonic=float(now),
            )
        self.assertIsNotNone(adaptive)
        primary_rate = (3 / 32) / 3.1
        canonical_rate = (20 / 32) / 6.0
        fixed_rate = 8 * primary_rate + 8 * canonical_rate
        adaptive_rate = (
            adaptive.primary_slots * primary_rate
            + adaptive.canonical_slots * canonical_rate
        )

        self.assertGreater(adaptive_rate * 20.0, fixed_rate * 20.0)
        self.assertLessEqual(3.0 / adaptive_rate, 3.0 / fixed_rate)

    def test_learning_lane_correction_is_bounded_around_deterministic_baseline(self) -> None:
        envelope = Tier3ValidatorEnvelopeController(
            minimum_count=1,
            maximum_count=20,
            initial_count=20,
        )
        baseline = envelope.update(
            20,
            primary_backlog=True,
            canonical_backlog=True,
            primary_depth=100,
            canonical_depth=100,
            retry_depth=100,
        )
        corrected = apply_tier3_bounded_lane_correction(
            baseline,
            suggested_primary=20,
            suggested_canonical=0,
            suggested_retry=0,
            correction_fraction=0.20,
            canonical_fresh_depth=10,
            canonical_resume_depth=10,
        )
        movement = (
            abs(corrected.primary_slots - baseline.primary_slots)
            + abs(corrected.canonical_slots - baseline.canonical_slots)
            + abs(corrected.retry_slots - baseline.retry_slots)
        ) // 2

        self.assertLessEqual(movement, 4)
        self.assertGreaterEqual(corrected.primary_slots, 5)
        self.assertGreaterEqual(corrected.canonical_slots, 5)
        self.assertLessEqual(corrected.retry_slots, 2)
        self.assertEqual(
            corrected.primary_slots
            + corrected.canonical_slots
            + corrected.retry_slots,
            20,
        )

    def test_pi_integral_decays_after_pool_recovers(self) -> None:
        controller = Tier3PIController(
            {
                "target_alive": 10,
                "kp": 0.08,
                "ki": 0.015,
                "min_intensity": 0.0,
                "max_intensity": 1.0,
                "integral_decay": 0.85,
            }
        )

        depleted = controller.evaluate(alive_count=0, target_alive=10, min_interval=120, max_interval=3600)
        recovered = controller.evaluate(alive_count=20, target_alive=10, min_interval=120, max_interval=3600)

        self.assertGreater(depleted.integral, 0)
        self.assertLess(recovered.integral, depleted.integral)
        self.assertLess(recovered.intensity, depleted.intensity)

    def test_pi_default_expands_aggressively_when_pool_is_depleted(self) -> None:
        controller = Tier3PIController({})

        decision = controller.evaluate(alive_count=2, target_alive=25, min_interval=120, max_interval=3600)

        self.assertGreaterEqual(decision.source_budget, 10)
        self.assertLessEqual(decision.collect_interval, 900)
        self.assertEqual(controller.to_dict()["source_budget"], decision.source_budget)

    def test_pi_controller_falls_back_on_invalid_numeric_config(self) -> None:
        controller = Tier3PIController(
            {
                "target_alive": "bad",
                "kp": "bad",
                "ki": "bad",
                "min_intensity": "bad",
                "max_intensity": "bad",
                "integral_decay": "bad",
                "source_budget_min": "bad",
                "source_budget_max": "bad",
            }
        )

        decision = controller.evaluate(
            alive_count=0,
            target_alive=10,
            min_interval=120,
            max_interval=3600,
        )

        self.assertEqual(
            decision,
            Tier3ControllerDecision(
                intensity=1.0,
                source_budget=12,
                collect_interval=120,
                error=1.0,
                integral=1.0,
            ),
        )

    def test_refresh_plan_marks_underfilled_without_replacing_pi_decision(self) -> None:
        base = Tier3ControllerDecision(
            intensity=0.2,
            source_budget=3,
            collect_interval=900,
            error=0.5,
            integral=0.2,
        )

        plan = tier3_refresh_plan(
            previous_alive_count=1,
            target_alive=3,
            base_decision=base,
        )

        self.assertTrue(plan.intensive_expansion)
        self.assertTrue(plan.floor_deficit)
        self.assertEqual(plan.target_alive, 3)
        self.assertEqual(plan.reserve_deficit, 2)
        self.assertEqual(plan.reserve_deficit_ratio, 0.6667)
        self.assertIs(plan.decision, base)

    def test_refresh_plan_keeps_base_decision_when_target_is_met(self) -> None:
        base = Tier3ControllerDecision(
            intensity=0.2,
            source_budget=3,
            collect_interval=900,
            error=0.0,
            integral=0.0,
        )

        plan = tier3_refresh_plan(
            previous_alive_count=3,
            target_alive=3,
            base_decision=base,
        )

        self.assertFalse(plan.intensive_expansion)
        self.assertIs(plan.decision, base)
        self.assertEqual(plan.target_alive, 3)

    def test_refresh_plan_marks_reserve_deficit_above_min_alive_without_floor_boost(self) -> None:
        base = Tier3ControllerDecision(
            intensity=0.2,
            source_budget=3,
            collect_interval=900,
            error=0.5,
            integral=0.2,
        )

        plan = tier3_refresh_plan(
            previous_alive_count=3,
            target_alive=9,
            min_alive_nodes=3,
            base_decision=base,
        )

        self.assertTrue(plan.intensive_expansion)
        self.assertFalse(plan.floor_deficit)
        self.assertEqual(plan.reserve_deficit, 6)
        self.assertEqual(plan.reserve_deficit_ratio, 0.6667)
        self.assertIs(plan.decision, base)
        self.assertEqual(plan.target_alive, 9)

    def test_refresh_plan_keeps_background_target_while_underfilled(self) -> None:
        base = Tier3ControllerDecision(
            intensity=0.2,
            source_budget=3,
            collect_interval=900,
            error=0.8,
            integral=0.8,
        )

        plan = tier3_refresh_plan(
            previous_alive_count=1,
            target_alive=9,
            min_alive_nodes=3,
            base_decision=base,
        )

        self.assertTrue(plan.intensive_expansion)
        self.assertTrue(plan.floor_deficit)
        self.assertEqual(plan.target_alive, 9)
        self.assertEqual(plan.reserve_deficit, 8)
        self.assertEqual(plan.reserve_deficit_ratio, 0.8889)
        self.assertIs(plan.decision, base)

    def test_validation_budget_scales_continuously_with_pi_pressure_and_reserve_deficit(self) -> None:
        low = tier3_validation_budget(
            decision=Tier3ControllerDecision(
                intensity=0.1,
                source_budget=2,
                collect_interval=1800,
                error=0.1,
                integral=0.0,
            ),
            alive_count=24,
            target_alive=25,
            source_budget_min=1,
            source_budget_max=12,
            validation_candidate_limit=4,
            validation_empty_retry_limit=12,
            validation_budget_multiplier=4.0,
            validation_batch_size_min=4,
            validation_batch_size_max=16,
            validation_concurrency_min=2,
            validation_concurrency_max=8,
            validation_worker_min=1,
            validation_worker_max=4,
            source_stream_window_min=8,
            source_stream_window_max=50,
        )
        medium = tier3_validation_budget(
            decision=Tier3ControllerDecision(
                intensity=0.5,
                source_budget=6,
                collect_interval=600,
                error=0.5,
                integral=0.5,
            ),
            alive_count=12,
            target_alive=25,
            source_budget_min=1,
            source_budget_max=12,
            validation_candidate_limit=4,
            validation_empty_retry_limit=12,
            validation_budget_multiplier=4.0,
            validation_batch_size_min=4,
            validation_batch_size_max=16,
            validation_concurrency_min=2,
            validation_concurrency_max=8,
            validation_worker_min=1,
            validation_worker_max=4,
            source_stream_window_min=8,
            source_stream_window_max=50,
        )
        high = tier3_validation_budget(
            decision=Tier3ControllerDecision(
                intensity=0.9,
                source_budget=10,
                collect_interval=120,
                error=0.9,
                integral=0.9,
            ),
            alive_count=2,
            target_alive=25,
            source_budget_min=1,
            source_budget_max=12,
            validation_candidate_limit=4,
            validation_empty_retry_limit=12,
            validation_budget_multiplier=4.0,
            validation_batch_size_min=4,
            validation_batch_size_max=16,
            validation_concurrency_min=2,
            validation_concurrency_max=8,
            validation_worker_min=1,
            validation_worker_max=4,
            source_stream_window_min=8,
            source_stream_window_max=50,
        )

        self.assertLess(low.expansion_pressure, medium.expansion_pressure)
        self.assertLess(medium.expansion_pressure, high.expansion_pressure)
        self.assertLess(low.validation_budget, medium.validation_budget)
        self.assertLess(medium.validation_budget, high.validation_budget)
        self.assertLess(low.source_budget, medium.source_budget)
        self.assertLess(medium.source_budget, high.source_budget)
        self.assertLess(low.validation_concurrency, medium.validation_concurrency)
        self.assertLessEqual(medium.validation_concurrency, high.validation_concurrency)
        self.assertLess(low.validation_worker_count, medium.validation_worker_count)
        self.assertLessEqual(medium.validation_worker_count, high.validation_worker_count)
        self.assertLess(low.source_stream_window, medium.source_stream_window)
        self.assertLess(medium.source_stream_window, high.source_stream_window)
        self.assertLess(low.mihomo_process_count, high.mihomo_process_count)
        self.assertLess(low.proxy_delay_concurrency_total, high.proxy_delay_concurrency_total)
        self.assertLess(low.responsive_queue_drain_budget, high.responsive_queue_drain_budget)

    def test_high_pressure_can_use_sixteen_mihomo_processes_without_changing_semantics(self) -> None:
        budget = tier3_validation_budget(
            decision=Tier3ControllerDecision(
                intensity=1.0,
                source_budget=12,
                collect_interval=1,
                error=1.0,
                integral=1.0,
            ),
            alive_count=0,
            target_alive=25,
            backlog_count=3000,
            recent_pass_rate=0.0,
            source_budget_min=1,
            source_budget_max=12,
            validation_candidate_limit=24,
            validation_empty_retry_limit=100,
            validation_budget_multiplier=4.0,
            validation_batch_size_min=4,
            validation_batch_size_max=16,
            validation_concurrency_min=3,
            validation_concurrency_max=16,
            validation_worker_min=1,
            validation_worker_max=16,
            source_stream_window_min=8,
            source_stream_window_max=100,
            mihomo_process_min=1,
            mihomo_process_max=16,
        )

        self.assertEqual(budget.validation_worker_count, 16)
        self.assertEqual(budget.mihomo_process_count, 16)

    def test_validation_budget_uses_backlog_and_pass_rate_as_continuous_pressure_inputs(self) -> None:
        base_kwargs = {
            "decision": Tier3ControllerDecision(
                intensity=0.8,
                source_budget=8,
                collect_interval=900,
                error=0.8,
                integral=0.8,
            ),
            "alive_count": 5,
            "target_alive": 25,
            "source_budget_min": 1,
            "source_budget_max": 12,
            "validation_candidate_limit": 24,
            "validation_empty_retry_limit": 100,
            "validation_budget_multiplier": 4.0,
            "validation_batch_size_min": 4,
            "validation_batch_size_max": 16,
            "validation_concurrency_min": 2,
            "validation_concurrency_max": 12,
            "validation_worker_min": 1,
            "validation_worker_max": 5,
            "source_stream_window_min": 8,
            "source_stream_window_max": 50,
            "validation_session_wave_min": 1,
            "validation_session_wave_max": 5,
        }

        no_backlog = tier3_validation_budget(
            **base_kwargs,
            backlog_count=0,
            recent_pass_rate=1.0,
        )
        some_backlog = tier3_validation_budget(
            **base_kwargs,
            backlog_count=25,
            recent_pass_rate=0.5,
        )
        large_backlog = tier3_validation_budget(
            **base_kwargs,
            backlog_count=3600,
            recent_pass_rate=1 / 87,
        )
        target_met = tier3_validation_budget(
            **{
                **base_kwargs,
                "alive_count": 25,
                "decision": Tier3ControllerDecision(
                    intensity=0.12,
                    source_budget=2,
                    collect_interval=3200,
                    error=0.0,
                    integral=0.0,
                ),
            },
            backlog_count=3600,
            recent_pass_rate=1 / 87,
        )

        self.assertLess(no_backlog.tier3_throughput_pressure, some_backlog.tier3_throughput_pressure)
        self.assertLess(some_backlog.tier3_throughput_pressure, large_backlog.tier3_throughput_pressure)
        self.assertLessEqual(no_backlog.validation_session_wave_count, some_backlog.validation_session_wave_count)
        self.assertLessEqual(some_backlog.validation_session_wave_count, large_backlog.validation_session_wave_count)
        self.assertEqual(target_met.tier3_throughput_pressure, 0.0)
        self.assertEqual(target_met.validation_session_wave_count, 1)
        self.assertEqual(target_met.mihomo_process_count, 1)
        self.assertEqual(target_met.proxy_delay_concurrency_total, base_kwargs["validation_concurrency_min"])

    def test_validation_budget_uses_negative_skip_rate_as_continuous_pressure_input(self) -> None:
        base_kwargs = {
            "decision": Tier3ControllerDecision(
                intensity=0.4,
                source_budget=4,
                collect_interval=900,
                error=0.4,
                integral=0.4,
            ),
            "alive_count": 8,
            "target_alive": 25,
            "source_budget_min": 1,
            "source_budget_max": 12,
            "validation_candidate_limit": 24,
            "validation_empty_retry_limit": 100,
            "validation_budget_multiplier": 4.0,
            "validation_batch_size_min": 4,
            "validation_batch_size_max": 16,
            "validation_concurrency_min": 2,
            "validation_concurrency_max": 12,
            "validation_worker_min": 1,
            "validation_worker_max": 5,
            "source_stream_window_min": 8,
            "source_stream_window_max": 50,
            "validation_session_wave_min": 1,
            "validation_session_wave_max": 5,
            "backlog_count": 100,
            "recent_pass_rate": 0.4,
        }

        low_skip = tier3_validation_budget(**base_kwargs, recent_negative_skip_rate=0.0)
        high_skip = tier3_validation_budget(**base_kwargs, recent_negative_skip_rate=0.9)
        target_met = tier3_validation_budget(
            **{
                **base_kwargs,
                "alive_count": 25,
                "decision": Tier3ControllerDecision(
                    intensity=0.0,
                    source_budget=1,
                    collect_interval=3600,
                    error=0.0,
                    integral=0.0,
                ),
            },
            recent_negative_skip_rate=0.9,
        )

        self.assertEqual(low_skip.recent_negative_skip_rate, 0.0)
        self.assertEqual(high_skip.recent_negative_skip_rate, 0.9)
        self.assertGreater(high_skip.negative_cache_pressure, low_skip.negative_cache_pressure)
        self.assertGreater(high_skip.tier3_throughput_pressure, low_skip.tier3_throughput_pressure)
        self.assertEqual(target_met.negative_cache_pressure, 0.0)
        self.assertEqual(target_met.tier3_throughput_pressure, 0.0)


if __name__ == "__main__":
    unittest.main()
