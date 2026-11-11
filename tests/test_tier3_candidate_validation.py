import unittest

from tier3_candidate_validation import (
    tier3_primary_validation_limit,
    tier3_primary_validation_plan,
    tier3_prefilter_only_validation_report,
    tier3_refresh_validation_kwargs,
    run_tier3_validation_batches,
    Tier3ValidationBatchRunResult,
    Tier3TcpPrefilterOutcome,
    Tier3TcpPrefilterPlan,
    Tier3PrimaryValidationPlan,
    tier3_unusable_node_count,
    tier3_tcp_prefilter_outcome,
    tier3_tcp_prefilter_plan,
    tier3_tcp_prefilter_responsive_nodes,
    tier3_validation_batch_limit,
    tier3_validation_configured_batch_size,
    tier3_next_validation_retry_batch,
    tier3_validation_report,
    tier3_validation_retry_limit,
    tier3_validation_retry_plan,
    Tier3ValidationRetryBatch,
    Tier3ValidationRetryPlan,
    tier3_validation_target_reached,
    tier3_validation_target_alive_count,
    with_tier3_candidate_filter_counts,
    with_tier3_revival_counts,
    with_tier3_tested_unusable_count,
)
from models import Node, NodeStatus


def node(name: str, status: NodeStatus) -> Node:
    return Node(
        name=name,
        protocol="ss",
        uri=f"ss://{name}",
        address=f"{name}.example.com",
        port=8388,
        tier="tier3",
        status=status,
    )


class Tier3CandidateValidationTests(unittest.TestCase):
    def test_target_alive_count_clamps_to_candidate_count_and_defaults_when_missing(self) -> None:
        self.assertEqual(
            tier3_validation_target_alive_count(
                candidate_count=0,
                target_alive=10,
                default_target_alive=3,
            ),
            0,
        )
        self.assertEqual(
            tier3_validation_target_alive_count(
                candidate_count=5,
                target_alive=None,
                default_target_alive=3,
            ),
            3,
        )
        self.assertEqual(
            tier3_validation_target_alive_count(
                candidate_count=5,
                target_alive=10,
                default_target_alive=3,
            ),
            5,
        )

    def test_primary_validation_limit_preserves_unlimited_zero_semantics(self) -> None:
        self.assertEqual(
            tier3_primary_validation_limit(
                candidate_count=5,
                candidate_limit=None,
                default_candidate_limit=2,
            ),
            (2, 2),
        )
        self.assertEqual(
            tier3_primary_validation_limit(
                candidate_count=5,
                candidate_limit=0,
                default_candidate_limit=2,
            ),
            (0, 5),
        )
        self.assertEqual(
            tier3_primary_validation_limit(
                candidate_count=5,
                candidate_limit=-4,
                default_candidate_limit=2,
            ),
            (0, 5),
        )

    def test_primary_validation_plan_preserves_original_limit_and_caps_selected_nodes(self) -> None:
        selected = [
            node("a", NodeStatus.ALIVE),
            node("b", NodeStatus.ALIVE),
        ]

        self.assertEqual(
            tier3_primary_validation_plan(
                selected,
                candidate_count=5,
                candidate_limit=None,
                default_candidate_limit=4,
            ),
            Tier3PrimaryValidationPlan(
                limit=4,
                primary_limit=2,
                candidates=selected,
            ),
        )
        self.assertEqual(
            tier3_primary_validation_plan(
                selected,
                candidate_count=5,
                candidate_limit=0,
                default_candidate_limit=4,
            ),
            Tier3PrimaryValidationPlan(
                limit=0,
                primary_limit=2,
                candidates=selected,
            ),
        )
        self.assertEqual(
            tier3_primary_validation_plan(
                selected,
                candidate_count=5,
                candidate_limit=1,
                default_candidate_limit=4,
            ),
            Tier3PrimaryValidationPlan(
                limit=1,
                primary_limit=1,
                candidates=selected[:1],
            ),
        )

    def test_retry_and_batch_limits_keep_existing_floor_behavior(self) -> None:
        self.assertEqual(
            tier3_validation_retry_limit(
                empty_retry_limit=None,
                default_empty_retry_limit=4,
            ),
            4,
        )
        self.assertEqual(
            tier3_validation_retry_limit(
                empty_retry_limit=-1,
                default_empty_retry_limit=4,
            ),
            0,
        )
        self.assertEqual(
            tier3_validation_batch_limit(
                batch_size=None,
                default_batch_size=3,
            ),
            3,
        )
        self.assertEqual(
            tier3_validation_batch_limit(
                batch_size=0,
                default_batch_size=3,
            ),
            1,
        )

    def test_configured_batch_size_uses_helper_concurrency_or_default_safely(self) -> None:
        self.assertEqual(
            tier3_validation_configured_batch_size(
                configured_batch_size=None,
                helper_concurrency=5,
                default_batch_size=4,
            ),
            10,
        )
        self.assertEqual(
            tier3_validation_configured_batch_size(
                configured_batch_size=0,
                helper_concurrency=5,
                default_batch_size=4,
            ),
            1,
        )
        self.assertEqual(
            tier3_validation_configured_batch_size(
                configured_batch_size="invalid",
                helper_concurrency=5,
                default_batch_size=4,
            ),
            4,
        )
        self.assertEqual(
            tier3_validation_configured_batch_size(
                configured_batch_size=None,
                helper_concurrency="invalid",
                default_batch_size=4,
            ),
            6,
        )

    def test_retry_plan_starts_empty_retry_when_no_candidate_is_alive(self) -> None:
        plan = tier3_validation_retry_plan(
            current_alive_count=0,
            target_alive_count=3,
            retry_limit=6,
            tested_count=2,
            candidate_count=8,
            batch_size=2,
            default_batch_size=4,
        )

        self.assertTrue(plan.should_retry)
        self.assertTrue(plan.empty_retry_used)
        self.assertTrue(plan.target_retry_used)
        self.assertEqual(plan.batch_limit, 2)
        self.assertEqual(plan.max_retry_count, 6)

    def test_retry_plan_continues_without_empty_retry_when_below_target(self) -> None:
        plan = tier3_validation_retry_plan(
            current_alive_count=1,
            target_alive_count=3,
            retry_limit=9,
            tested_count=2,
            candidate_count=5,
            batch_size=None,
            default_batch_size=4,
        )

        self.assertTrue(plan.should_retry)
        self.assertFalse(plan.empty_retry_used)
        self.assertTrue(plan.target_retry_used)
        self.assertEqual(plan.batch_limit, 4)
        self.assertEqual(plan.max_retry_count, 5)

    def test_retry_plan_stops_when_target_or_retry_bound_is_met(self) -> None:
        self.assertFalse(
            tier3_validation_retry_plan(
                current_alive_count=3,
                target_alive_count=3,
                retry_limit=6,
                tested_count=2,
                candidate_count=8,
                batch_size=2,
                default_batch_size=4,
            ).should_retry
        )
        self.assertFalse(
            tier3_validation_retry_plan(
                current_alive_count=0,
                target_alive_count=3,
                retry_limit=2,
                tested_count=2,
                candidate_count=8,
                batch_size=2,
                default_batch_size=4,
            ).should_retry
        )

    def test_next_retry_batch_advances_by_batch_limit_until_retry_bound(self) -> None:
        nodes = [
            node("a", NodeStatus.ALIVE),
            node("b", NodeStatus.ALIVE),
            node("c", NodeStatus.ALIVE),
            node("d", NodeStatus.ALIVE),
        ]
        plan = Tier3ValidationRetryPlan(
            should_retry=True,
            empty_retry_used=True,
            target_retry_used=True,
            batch_limit=2,
            max_retry_count=3,
        )

        self.assertEqual(
            tier3_next_validation_retry_batch(
                nodes,
                tested_count=1,
                retry_plan=plan,
            ),
            Tier3ValidationRetryBatch(
                candidates=nodes[1:3],
                next_tested_count=3,
            ),
        )
        self.assertEqual(
            tier3_next_validation_retry_batch(
                nodes,
                tested_count=3,
                retry_plan=plan,
            ),
            Tier3ValidationRetryBatch(candidates=[], next_tested_count=3),
        )

    def test_retry_batch_and_target_stop_helpers_preserve_disabled_retry(self) -> None:
        nodes = [node("a", NodeStatus.ALIVE)]
        disabled = Tier3ValidationRetryPlan(
            should_retry=False,
            empty_retry_used=False,
            target_retry_used=False,
            batch_limit=0,
            max_retry_count=1,
        )

        self.assertEqual(
            tier3_next_validation_retry_batch(
                nodes,
                tested_count=0,
                retry_plan=disabled,
            ),
            Tier3ValidationRetryBatch(candidates=[], next_tested_count=0),
        )
        self.assertTrue(
            tier3_validation_target_reached(
                current_alive_count=3,
                target_alive_count=3,
            )
        )
        self.assertFalse(
            tier3_validation_target_reached(
                current_alive_count=2,
                target_alive_count=3,
            )
        )

    def test_validation_batch_runner_retries_until_target_is_reached(self) -> None:
        selected = [
            node("a", NodeStatus.DEAD),
            node("b", NodeStatus.ALIVE),
            node("c", NodeStatus.ALIVE),
            node("d", NodeStatus.ALIVE),
        ]
        calls: list[list[Node]] = []

        def tester(candidates: list[Node]) -> list[Node]:
            calls.append(candidates)
            return candidates

        result = run_tier3_validation_batches(
            selected,
            primary_plan=Tier3PrimaryValidationPlan(
                limit=2,
                primary_limit=2,
                candidates=selected[:2],
            ),
            target_alive_count=2,
            retry_limit=4,
            batch_size=1,
            default_batch_size=3,
            candidate_tester=tester,
            healthy_counter=lambda nodes: sum(1 for item in nodes if item.status == NodeStatus.ALIVE),
        )

        self.assertEqual(
            result,
            Tier3ValidationBatchRunResult(
                tested_nodes=selected[:3],
                tested_count=3,
                empty_retry_used=False,
                target_retry_used=True,
            ),
        )
        self.assertEqual(calls, [selected[:2], selected[2:3]])

    def test_validation_batch_runner_streams_primary_batches(self) -> None:
        selected = [
            node("a", NodeStatus.ALIVE),
            node("b", NodeStatus.ALIVE),
            node("c", NodeStatus.ALIVE),
            node("d", NodeStatus.ALIVE),
        ]
        calls: list[list[Node]] = []
        progress: list[tuple[int, int, int]] = []

        def tester(candidates: list[Node]) -> list[Node]:
            calls.append(candidates)
            return candidates

        result = run_tier3_validation_batches(
            selected,
            primary_plan=Tier3PrimaryValidationPlan(
                limit=4,
                primary_limit=4,
                candidates=selected,
            ),
            target_alive_count=3,
            retry_limit=4,
            batch_size=2,
            default_batch_size=4,
            candidate_tester=tester,
            healthy_counter=lambda nodes: sum(1 for item in nodes if item.status == NodeStatus.ALIVE),
            batch_observer=lambda item: progress.append(
                (item.batch_index, item.tested_count, item.current_alive_count)
            ),
            stream_primary=True,
        )

        self.assertEqual(result.tested_nodes, selected[:4])
        self.assertEqual(result.tested_count, 4)
        self.assertEqual(calls, [selected[:2], selected[2:4]])
        self.assertEqual(progress, [(1, 2, 2), (2, 4, 4)])

    def test_validation_batch_runner_marks_empty_retry_when_primary_has_no_alive_nodes(self) -> None:
        selected = [
            node("a", NodeStatus.DEAD),
            node("b", NodeStatus.DEAD),
            node("c", NodeStatus.ALIVE),
        ]

        result = run_tier3_validation_batches(
            selected,
            primary_plan=Tier3PrimaryValidationPlan(
                limit=2,
                primary_limit=2,
                candidates=selected[:2],
            ),
            target_alive_count=1,
            retry_limit=3,
            batch_size=1,
            default_batch_size=3,
            candidate_tester=lambda candidates: candidates,
            healthy_counter=lambda nodes: sum(1 for item in nodes if item.status == NodeStatus.ALIVE),
        )

        self.assertEqual(
            result,
            Tier3ValidationBatchRunResult(
                tested_nodes=selected,
                tested_count=3,
                empty_retry_used=True,
                target_retry_used=True,
            ),
        )

    def test_tcp_prefilter_plan_requires_probe_budget_and_validation_mode(self) -> None:
        self.assertEqual(
            tier3_tcp_prefilter_plan(
                fast_probe_limit=3,
                full_validation_enabled=False,
                prelaunch_validation="tcp",
                candidate_count=10,
            ),
            Tier3TcpPrefilterPlan(enabled=True, candidate_limit=3),
        )
        self.assertEqual(
            tier3_tcp_prefilter_plan(
                fast_probe_limit=20,
                full_validation_enabled=True,
                prelaunch_validation="full",
                candidate_count=5,
            ),
            Tier3TcpPrefilterPlan(enabled=True, candidate_limit=5),
        )
        self.assertFalse(
            tier3_tcp_prefilter_plan(
                fast_probe_limit=0,
                full_validation_enabled=True,
                prelaunch_validation="tcp",
                candidate_count=5,
            ).enabled
        )
        self.assertFalse(
            tier3_tcp_prefilter_plan(
                fast_probe_limit=3,
                full_validation_enabled=False,
                prelaunch_validation="full",
                candidate_count=5,
            ).enabled
        )
        self.assertFalse(
            tier3_tcp_prefilter_plan(
                fast_probe_limit=3,
                full_validation_enabled=True,
                prelaunch_validation="tcp",
                candidate_count=0,
            ).enabled
        )

    def test_tcp_prefilter_responsive_nodes_keep_alive_and_slow_only(self) -> None:
        alive = node("alive", NodeStatus.ALIVE)
        slow = node("slow", NodeStatus.SLOW)
        dead = node("dead", NodeStatus.DEAD)
        suspicious = node("suspicious", NodeStatus.SUSPICIOUS)

        self.assertEqual(
            tier3_tcp_prefilter_responsive_nodes([dead, alive, suspicious, slow]),
            [alive, slow],
        )

    def test_tcp_prefilter_outcome_returns_empty_when_no_responsive_nodes(self) -> None:
        self.assertEqual(
            tier3_tcp_prefilter_outcome(
                [],
            ),
            Tier3TcpPrefilterOutcome(
                nodes=[],
                return_without_full_validation=True,
                report_selected_candidate_count=0,
            ),
        )

    def test_tcp_prefilter_outcome_always_continues_to_full_validation(self) -> None:
        first = node("first", NodeStatus.ALIVE)
        second = node("second", NodeStatus.SLOW)

        outcome = tier3_tcp_prefilter_outcome(
            [first, second],
        )

        self.assertEqual(
            outcome,
            Tier3TcpPrefilterOutcome(
                nodes=[first, second],
                return_without_full_validation=False,
                report_selected_candidate_count=2,
            ),
        )
        self.assertNotIn("startup_prelaunch_validation", first.extra)
        self.assertNotIn("startup_prelaunch_validation", second.extra)

    def test_tcp_prefilter_outcome_continues_full_validation_for_full_mode(self) -> None:
        first = node("first", NodeStatus.ALIVE)
        second = node("second", NodeStatus.SLOW)

        outcome = tier3_tcp_prefilter_outcome(
            [first, second],
        )

        self.assertEqual(
            outcome,
            Tier3TcpPrefilterOutcome(
                nodes=[first, second],
                return_without_full_validation=False,
                report_selected_candidate_count=2,
            ),
        )
        self.assertNotIn("startup_prelaunch_validation", first.extra)

    def test_refresh_validation_kwargs_uses_underfilled_mode_for_unified_expansion(self) -> None:
        self.assertEqual(
            tier3_refresh_validation_kwargs(
                intensive_expansion=True,
                underfilled_empty_retry_limit=9,
                underfilled_batch_size=10,
            ),
            {"empty_retry_limit": 9, "batch_size": 10},
        )
        self.assertEqual(
            tier3_refresh_validation_kwargs(
                intensive_expansion=False,
            ),
            {},
        )

    def test_refresh_validation_kwargs_rejects_incomplete_mode_arguments(self) -> None:
        with self.assertRaisesRegex(ValueError, "underfilled_empty_retry_limit"):
            tier3_refresh_validation_kwargs(
                intensive_expansion=True,
            )

    def test_validation_report_preserves_all_runtime_fields(self) -> None:
        self.assertEqual(
            tier3_validation_report(
                candidate_count=5,
                selected_candidate_count=3,
                tested_count=2,
                limit=4,
                empty_retry_limit=6,
                empty_retry_used=True,
                target_retry_used=False,
                target_alive=3,
                tcp_prefilter_tested_count=7,
                tcp_prefilter_responsive_count=2,
                prelaunch_validation="tcp",
            ),
            {
                "candidate_count": 5,
                "selected_candidate_count": 3,
                "tested_count": 2,
                "limit": 4,
                "empty_retry_limit": 6,
                "empty_retry_used": True,
                "target_retry_used": False,
                "target_alive": 3,
                "tcp_prefilter_tested_count": 7,
                "tcp_prefilter_responsive_count": 2,
                "prelaunch_validation": "tcp",
            },
        )

    def test_prefilter_only_report_preserves_no_full_validation_contract(self) -> None:
        self.assertEqual(
            tier3_prefilter_only_validation_report(
                candidate_count=5,
                selected_candidate_count=2,
                candidate_limit=None,
                default_candidate_limit=4,
                empty_retry_limit=None,
                default_empty_retry_limit=6,
                target_alive=10,
                default_target_alive=3,
                tcp_prefilter_tested_count=3,
                tcp_prefilter_responsive_count=2,
                prelaunch_validation="tcp",
            ),
            {
                "candidate_count": 5,
                "selected_candidate_count": 2,
                "tested_count": 0,
                "limit": 4,
                "empty_retry_limit": 6,
                "empty_retry_used": False,
                "target_retry_used": False,
                "target_alive": 5,
                "tcp_prefilter_tested_count": 3,
                "tcp_prefilter_responsive_count": 2,
                "prelaunch_validation": "tcp",
            },
        )

    def test_prefilter_only_report_keeps_explicit_limits(self) -> None:
        report = tier3_prefilter_only_validation_report(
            candidate_count=2,
            selected_candidate_count=0,
            candidate_limit=0,
            default_candidate_limit=4,
            empty_retry_limit=0,
            default_empty_retry_limit=6,
            target_alive=None,
            default_target_alive=3,
            tcp_prefilter_tested_count=2,
            tcp_prefilter_responsive_count=0,
            prelaunch_validation="full",
        )

        self.assertEqual(report["limit"], 0)
        self.assertEqual(report["empty_retry_limit"], 0)
        self.assertEqual(report["target_alive"], 2)

    def test_unusable_node_count_treats_alive_and_slow_as_usable(self) -> None:
        self.assertEqual(
            tier3_unusable_node_count(
                [
                    node("alive", NodeStatus.ALIVE),
                    node("slow", NodeStatus.SLOW),
                    node("dead", NodeStatus.DEAD),
                    node("suspicious", NodeStatus.SUSPICIOUS),
                ]
            ),
            2,
        )

    def test_candidate_filter_counts_do_not_mutate_input_report(self) -> None:
        base = {"candidate_count": 2}

        updated = with_tier3_candidate_filter_counts(
            base,
            original_candidate_count=5,
            negative_cache_skipped_count=3,
        )

        self.assertEqual(base, {"candidate_count": 2})
        self.assertEqual(updated["candidate_count"], 2)
        self.assertEqual(updated["original_candidate_count"], 5)
        self.assertEqual(updated["negative_cache_skipped_count"], 3)

    def test_tested_unusable_and_revival_counts_are_explicit_report_extensions(self) -> None:
        base = {"candidate_count": 2}

        updated = with_tier3_tested_unusable_count(
            base,
            [
                node("alive", NodeStatus.ALIVE),
                node("dead", NodeStatus.DEAD),
            ],
        )
        revived = with_tier3_revival_counts(
            updated,
            {"candidate_count": 4, "tested_count": 3},
            [
                node("slow", NodeStatus.SLOW),
                node("suspicious", NodeStatus.SUSPICIOUS),
            ],
        )

        self.assertEqual(base, {"candidate_count": 2})
        self.assertEqual(updated["tested_unusable_count"], 1)
        self.assertEqual(revived["revival_candidate_count"], 4)
        self.assertEqual(revived["revival_tested_count"], 3)
        self.assertEqual(revived["revival_unusable_count"], 1)


if __name__ == "__main__":
    unittest.main()
