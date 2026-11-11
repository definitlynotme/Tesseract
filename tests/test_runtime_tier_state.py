import unittest
from pathlib import Path

from models import Node, NodeStatus
from runtime_tier_state import (
    CoreExportSemanticDigest,
    apply_tier3_core_repair_results,
    build_core_reload_context,
    build_core_reload_unavailable_result,
    build_keep_existing_core_reload_failure_result,
    build_tier3_core_reload_rollback_report,
    build_tier3_core_reload_failure_result,
    build_tier3_startup_core_repair_report,
    build_tier3_startup_core_verification_failed_report,
    ensure_tier3_collection_report,
    has_unverified_tier3_tcp_candidates,
    node_has_startup_core_verification,
    primary_layers_configured,
    primary_runtime_healthy_count,
    primary_startup_unhealthy_message,
    reload_core_config_succeeded,
    should_attempt_deferred_tier3_core_start,
    should_keep_existing_core_for_empty_tier3_export,
    should_keep_existing_core_for_empty_tier3_marker,
    should_prime_tier3_pool_before_hot_reload,
    should_reload_tier3_core_config_for_marker,
    should_restore_tier3_pool_after_failed_reload,
    should_stop_tier3_core_probe,
    stop_sensitive_core_reload_failure_action,
    Tier3CachedStartVerification,
    Tier3CoreSyncAction,
    tier3_alive_target_from_manager,
    tier3_cached_start_verification,
    tier3_cache_requires_core_verification,
    tier3_core_sync_action_after_pool_update,
    tier3_core_probe_plan,
    tier3_core_delay_from_probe_result,
    tier3_core_repair_targets,
    tier3_core_repair_uses_startup_target,
    tier3_core_sync_failure_is_recent,
    tier3_core_sync_retry_interval,
    tier3_enabled_without_primary,
    tier3_export_node_signature,
    tier3_export_signature,
    tier3_export_uses_tcp_prelaunch_validation,
    tier3_is_primary_export,
    tier3_probe_urls_for_node,
    tier3_reload_rollback_source_nodes,
    tier3_runtime_export_limit,
    tier3_startup_background_expansion_decision,
    tier3_startup_failure_detail,
    tier3_startup_readiness_error,
    tier3_startup_core_verified_nodes,
)


def node(name: str, *, tier: str = "tier3", status: NodeStatus = NodeStatus.ALIVE, extra: dict | None = None) -> Node:
    return Node(
        name=name,
        protocol="ss",
        uri=f"ss://{name}",
        address=f"{name}.example.com",
        port=8388,
        tier=tier,
        status=status,
        method="aes-256-gcm",
        password="pass",
        extra=extra or {},
    )


class RuntimeTierStateTests(unittest.TestCase):
    def test_primary_startup_unhealthy_message_preserves_failure_context_limit(self) -> None:
        self.assertEqual(
            primary_startup_unhealthy_message([]),
            "Primary startup checks found no healthy nodes",
        )
        self.assertEqual(
            primary_startup_unhealthy_message(["tier1=timeout", "tier2=refused", "tier2=late"]),
            "Primary startup checks found no healthy nodes (tier1=timeout; tier2=refused)",
        )

    def test_ensure_tier3_collection_report_preserves_or_replaces_report_dict(self) -> None:
        class Manager:
            pass

        manager = Manager()
        manager.tier3_last_collection_report = {"existing": True}
        report = ensure_tier3_collection_report(manager)

        self.assertIs(report, manager.tier3_last_collection_report)
        self.assertEqual(report, {"existing": True})

        manager.tier3_last_collection_report = []
        report = ensure_tier3_collection_report(manager)

        self.assertIs(report, manager.tier3_last_collection_report)
        self.assertEqual(report, {})

        del manager.tier3_last_collection_report
        report = ensure_tier3_collection_report(manager)

        self.assertIs(report, manager.tier3_last_collection_report)
        self.assertEqual(report, {})

    def test_startup_core_repair_report_preserves_runtime_fields(self) -> None:
        self.assertEqual(
            build_tier3_startup_core_repair_report(
                tested_count=4,
                candidate_count=6,
                concurrency=2,
                working_count=3,
                current_node_count=5,
                retained_node_count=3,
                prune_to_successful=True,
                target_verified_count=3,
                minimum_verified_count=2,
                selected="fast-node",
                delay_ms=12.345,
            ),
            {
                "tested_count": 4,
                "candidate_count": 6,
                "concurrency": 2,
                "working_count": 3,
                "core_verified_count": 3,
                "core_failed_count": 0,
                "retained_failed_count": 0,
                "evicted_failed_count": 0,
                "pruned_count": 2,
                "pruned_to_successful": True,
                "target_verified_count": 3,
                "minimum_verified_count": 2,
                "stopped_after_target": True,
                "selected": "fast-node",
                "delay_ms": 12.35,
            },
        )
        self.assertEqual(
            build_tier3_startup_core_repair_report(
                tested_count=1,
                candidate_count=1,
                concurrency=1,
                working_count=1,
                current_node_count=1,
                retained_node_count=3,
                prune_to_successful=False,
                target_verified_count=2,
                minimum_verified_count=1,
                selected="fallback-node",
                delay_ms=7.0,
            )["pruned_count"],
            0,
        )

    def test_apply_tier3_core_repair_results_updates_and_sorts_current_nodes(self) -> None:
        slow = node("slow")
        slow.latency_ms = 500
        fast = node("fast", status=NodeStatus.SUSPICIOUS)
        untouched = node("untouched")
        untouched.latency_ms = 10

        applied = apply_tier3_core_repair_results(
            [slow, untouched, fast],
            [
                (80.123, slow.clone(), "slow-proxy", "https://probe/slow"),
                (20.456, fast.clone(), "fast-proxy", "https://probe/fast"),
            ],
            prune_to_successful=False,
            slow_threshold_ms=50,
            identity_fn=lambda item: item.name,
            now_iso_fn=lambda: "2026-05-31T00:00:00Z",
        )

        self.assertEqual([item.name for item in applied.nodes], ["fast", "slow", "untouched"])
        self.assertEqual(applied.current_node_count, 3)
        self.assertEqual(applied.retained_node_count, 3)
        self.assertEqual(applied.working_count, 2)
        self.assertEqual(applied.selected_proxy_name, "fast-proxy")
        self.assertEqual(applied.selected_delay_ms, 20.456)
        self.assertEqual(fast.status, NodeStatus.ALIVE)
        self.assertEqual(fast.latency_ms, 20.46)
        self.assertEqual(fast.extra["startup_core_probe_url"], "https://probe/fast")
        self.assertEqual(slow.status, NodeStatus.SLOW)
        self.assertEqual(slow.extra["startup_core_delay_ms"], 80.12)

    def test_apply_tier3_core_repair_results_prunes_to_verified_nodes(self) -> None:
        good = node("good")
        bad = node("bad")

        applied = apply_tier3_core_repair_results(
            [bad, good],
            [(25.0, good.clone(), "good-proxy", "https://probe/good")],
            prune_to_successful=True,
            slow_threshold_ms=100,
            identity_fn=lambda item: item.name,
            now_iso_fn=lambda: "2026-05-31T00:00:00Z",
        )

        self.assertEqual(applied.nodes, [good])
        self.assertEqual(applied.current_node_count, 2)
        self.assertEqual(applied.retained_node_count, 1)

    def test_apply_tier3_core_repair_results_demotes_failed_probe_without_pruning(self) -> None:
        good = node("good")
        bad = node("bad")

        applied = apply_tier3_core_repair_results(
            [bad, good],
            [(25.0, good.clone(), "good-proxy", "https://probe/good")],
            prune_to_successful=False,
            slow_threshold_ms=100,
            identity_fn=lambda item: item.name,
            now_iso_fn=lambda: "2026-05-31T00:00:00Z",
            failed_tested_identities={"bad"},
            retain_failed_until_threshold=True,
            failure_threshold=3,
        )

        self.assertEqual([item.name for item in applied.pool_nodes], ["bad", "good"])
        self.assertEqual(applied.pool_nodes[0].status, NodeStatus.SUSPICIOUS)
        self.assertEqual(applied.pool_nodes[0].extra["tier3_consecutive_failures"], 1)
        self.assertEqual(applied.retained_failed_count, 1)
        self.assertEqual(applied.evicted_failed_count, 0)
        self.assertEqual(applied.failed_tested_count, 1)

    def test_apply_tier3_core_repair_results_eviction_threshold_updates_pool_nodes(self) -> None:
        good = node("good")
        bad = node("bad", status=NodeStatus.SUSPICIOUS, extra={"tier3_consecutive_failures": 2})

        applied = apply_tier3_core_repair_results(
            [bad, good],
            [(25.0, good.clone(), "good-proxy", "https://probe/good")],
            prune_to_successful=False,
            slow_threshold_ms=100,
            identity_fn=lambda item: item.name,
            now_iso_fn=lambda: "2026-05-31T00:00:00Z",
            failed_tested_identities={"bad"},
            retain_failed_until_threshold=True,
            failure_threshold=3,
        )

        self.assertEqual([item.name for item in applied.pool_nodes], ["good"])
        self.assertEqual([item.name for item in applied.observed_failed_nodes], ["bad"])
        self.assertEqual(applied.retained_failed_count, 0)
        self.assertEqual(applied.evicted_failed_count, 1)

    def test_apply_tier3_core_repair_results_retains_failed_probe_until_threshold(self) -> None:
        good = node("good")
        bad = node("bad")

        applied = apply_tier3_core_repair_results(
            [bad, good],
            [(25.0, good.clone(), "good-proxy", "https://probe/good")],
            prune_to_successful=True,
            slow_threshold_ms=100,
            identity_fn=lambda item: item.name,
            now_iso_fn=lambda: "2026-05-31T00:00:00Z",
            failed_tested_identities={"bad"},
            retain_failed_until_threshold=True,
            failure_threshold=3,
        )

        self.assertEqual([item.name for item in applied.nodes], ["good", "bad"])
        self.assertEqual(applied.retained_node_count, 2)
        self.assertEqual(applied.nodes[1].status, NodeStatus.SUSPICIOUS)
        self.assertEqual(applied.nodes[1].suspicious_reason, "mihomo_validation_failed: core export verification failed")
        self.assertEqual(applied.nodes[1].extra["tier3_consecutive_failures"], 1)
        self.assertTrue(applied.nodes[1].extra["tier3_retained_after_failure"])

    def test_apply_tier3_core_repair_results_drops_failed_probe_at_threshold(self) -> None:
        good = node("good")
        bad = node("bad", status=NodeStatus.SUSPICIOUS, extra={"tier3_consecutive_failures": 2})

        applied = apply_tier3_core_repair_results(
            [bad, good],
            [(25.0, good.clone(), "good-proxy", "https://probe/good")],
            prune_to_successful=True,
            slow_threshold_ms=100,
            identity_fn=lambda item: item.name,
            now_iso_fn=lambda: "2026-05-31T00:00:00Z",
            failed_tested_identities={"bad"},
            retain_failed_until_threshold=True,
            failure_threshold=3,
        )

        self.assertEqual(applied.nodes, [good])
        self.assertEqual(applied.retained_node_count, 1)

    def test_tier3_core_probe_plan_batches_by_effective_concurrency(self) -> None:
        plan = tier3_core_probe_plan(
            ["a", "b", "c", "d", "e"],
            configured_concurrency=2,
        )

        self.assertEqual(plan.concurrency, 2)
        self.assertEqual(plan.batches, [["a", "b"], ["c", "d"], ["e"]])

        capped = tier3_core_probe_plan(
            ["a", "b"],
            configured_concurrency=10,
        )

        self.assertEqual(capped.concurrency, 2)
        self.assertEqual(capped.batches, [["a", "b"]])

        empty = tier3_core_probe_plan([], configured_concurrency=3)
        self.assertEqual(empty.concurrency, 0)
        self.assertEqual(empty.batches, [])

        invalid = tier3_core_probe_plan(["a", "b"], configured_concurrency=0)
        self.assertEqual(invalid.concurrency, 1)
        self.assertEqual(invalid.batches, [["a"], ["b"]])

        malformed = tier3_core_probe_plan(["a", "b"], configured_concurrency="bad")
        self.assertEqual(malformed.concurrency, 1)
        self.assertEqual(malformed.batches, [["a"], ["b"]])

    def test_tier3_core_delay_from_probe_result_preserves_delay_parsing_contract(self) -> None:
        self.assertEqual(tier3_core_delay_from_probe_result({"delay": "12.5"}), 12.5)
        self.assertEqual(tier3_core_delay_from_probe_result({"delay": None}), 0.0)
        self.assertEqual(tier3_core_delay_from_probe_result({}), 0.0)
        with self.assertRaises(ValueError):
            tier3_core_delay_from_probe_result({"delay": "bad"})
        with self.assertRaises(ValueError):
            tier3_core_delay_from_probe_result(None)

    def test_tier3_core_probe_stop_policy_only_stops_pruning_at_target(self) -> None:
        self.assertTrue(
            should_stop_tier3_core_probe(
                prune_to_successful=True,
                successful_count=3,
                target_verified=3,
            )
        )
        self.assertFalse(
            should_stop_tier3_core_probe(
                prune_to_successful=True,
                successful_count=2,
                target_verified=3,
            )
        )
        self.assertFalse(
            should_stop_tier3_core_probe(
                prune_to_successful=False,
                successful_count=3,
                target_verified=3,
            )
        )

    def test_startup_core_verification_failed_report_preserves_retry_contract(self) -> None:
        report = build_tier3_startup_core_verification_failed_report(
            reason="x" * 600,
            rejected_count=4,
        )

        self.assertEqual(report["reason"], "x" * 500)
        self.assertEqual(report["rejected_count"], 4)
        self.assertTrue(report["will_retry_collection"])

        self.assertEqual(
            build_tier3_startup_core_verification_failed_report(
                reason="",
                rejected_count=0,
            )["reason"],
            "Fallback egress verification failed",
        )

    def test_tier3_startup_background_expansion_decision_preserves_last_collect_contract(self) -> None:
        underfilled = tier3_startup_background_expansion_decision(
            healthy_count=1,
            background_target=3,
            mark_collect_complete_when_full=True,
            last_check_monotonic={"tier3": 42.0},
            fallback_now=99.0,
        )
        self.assertTrue(underfilled.background_expansion_due)
        self.assertEqual(underfilled.next_last_collect_tier3, 0.0)

        complete = tier3_startup_background_expansion_decision(
            healthy_count=3,
            background_target=3,
            mark_collect_complete_when_full=True,
            last_check_monotonic={"tier3": 42.0},
            fallback_now=99.0,
        )
        self.assertFalse(complete.background_expansion_due)
        self.assertEqual(complete.next_last_collect_tier3, 42.0)

        fallback = tier3_startup_background_expansion_decision(
            healthy_count=3,
            background_target=3,
            mark_collect_complete_when_full=True,
            last_check_monotonic=[],
            fallback_now=99.0,
        )
        self.assertFalse(fallback.background_expansion_due)
        self.assertEqual(fallback.next_last_collect_tier3, 99.0)

        unchanged = tier3_startup_background_expansion_decision(
            healthy_count=3,
            background_target=3,
            mark_collect_complete_when_full=False,
            last_check_monotonic={"tier3": 42.0},
            fallback_now=99.0,
        )
        self.assertFalse(unchanged.background_expansion_due)
        self.assertIsNone(unchanged.next_last_collect_tier3)

    def test_tier3_alive_target_from_manager_uses_ui_min_alive_floor(self) -> None:
        class Manager:
            def _tier3_min_alive_nodes(self) -> int:
                return 3

        self.assertEqual(tier3_alive_target_from_manager(Manager(), default=1), 3)

    def test_tier3_alive_target_from_manager_falls_back_safely(self) -> None:
        class BadManager:
            def _tier3_min_alive_nodes(self) -> str:
                return "bad"

        self.assertEqual(tier3_alive_target_from_manager(BadManager(), default=2), 2)
        self.assertEqual(tier3_alive_target_from_manager(object(), default=0), 1)

    def test_tier3_startup_readiness_error_preserves_gate_messages(self) -> None:
        self.assertIsNone(
            tier3_startup_readiness_error(
                required_for_start=False,
                core_sync_marker=(),
                current_healthy_count=0,
                required_healthy_count=3,
                failure_detail="ignored",
            )
        )
        self.assertEqual(
            tier3_startup_readiness_error(
                required_for_start=True,
                core_sync_marker=(),
                current_healthy_count=0,
                required_healthy_count=3,
                failure_detail="collection failed",
            ),
            "Fallback has no validated proxy nodes; startup remains fail-closed (collection failed)",
        )
        self.assertEqual(
            tier3_startup_readiness_error(
                required_for_start=True,
                core_sync_marker=("node",),
                current_healthy_count=1,
                required_healthy_count=3,
                failure_detail="validation failed",
            ),
            "Fallback has only 1/3 validated proxy nodes; startup remains fail-closed (validation failed)",
        )
        self.assertIsNone(
            tier3_startup_readiness_error(
                required_for_start=True,
                core_sync_marker=("node",),
                current_healthy_count=3,
                required_healthy_count=3,
                failure_detail="",
            )
        )
        detail_calls: list[str] = []
        self.assertIsNone(
            tier3_startup_readiness_error(
                required_for_start=True,
                core_sync_marker=("node",),
                current_healthy_count=3,
                required_healthy_count=3,
                failure_detail=lambda: detail_calls.append("built") or "unused",
            )
        )
        self.assertEqual(detail_calls, [])

    def test_core_reload_rollback_report_preserves_retained_count(self) -> None:
        self.assertEqual(
            build_tier3_core_reload_rollback_report(retained_count=7),
            {
                "core_reload_rollback": True,
                "core_reload_rollback_retained_count": 7,
            },
        )

    def test_failed_reload_tier3_restore_requires_healthy_previous_pool(self) -> None:
        self.assertFalse(should_restore_tier3_pool_after_failed_reload([]))
        self.assertFalse(
            should_restore_tier3_pool_after_failed_reload(
                [node("dead", status=NodeStatus.DEAD)],
            )
        )
        self.assertTrue(
            should_restore_tier3_pool_after_failed_reload(
                [node("dead", status=NodeStatus.DEAD), node("healthy")],
            )
        )

    def test_tier3_reload_rollback_source_prefers_last_synced_pool(self) -> None:
        current = [node("current")]
        last_synced = [node("last-synced")]

        self.assertIs(
            tier3_reload_rollback_source_nodes(
                last_synced_nodes=last_synced,
                current_nodes=current,
            ),
            last_synced,
        )
        self.assertIs(
            tier3_reload_rollback_source_nodes(
                last_synced_nodes=[],
                current_nodes=current,
            ),
            current,
        )

    def test_core_reload_context_clones_rollback_source_and_copies_inputs(self) -> None:
        current = [node("current")]
        last_synced = [node("last-synced")]
        cloned: list[str] = []

        def clone_nodes(nodes: list[Node]) -> list[Node]:
            cloned.extend(node.name for node in nodes)
            return list(nodes)

        context = build_core_reload_context(
            runtime_config_path=Path("runtime.yaml"),
            previous_config_text="old",
            controller={"enabled": True},
            current_tier3_nodes=current,
            last_synced_tier3_nodes=last_synced,
            clone_nodes=clone_nodes,
        )

        self.assertEqual(context.runtime_config_path, Path("runtime.yaml"))
        self.assertEqual(context.previous_config_text, "old")
        self.assertEqual(context.controller, {"enabled": True})
        self.assertEqual([item.name for item in context.current_tier3_nodes], ["current"])
        self.assertEqual([item.name for item in context.rollback_tier3_nodes], ["last-synced"])
        self.assertEqual(cloned, ["last-synced"])
        self.assertIsNot(context.current_tier3_nodes, current)
        self.assertIsNot(context.rollback_tier3_nodes, last_synced)

    def test_core_sync_marker_decisions_preserve_start_and_reload_gates(self) -> None:
        self.assertTrue(
            should_attempt_deferred_tier3_core_start(
                running=True,
                marker=("tier3", 1),
                last_marker=("tier3", 0),
            )
        )
        self.assertFalse(
            should_attempt_deferred_tier3_core_start(
                running=True,
                marker=(),
                last_marker=("tier3", 0),
            )
        )
        self.assertFalse(
            should_attempt_deferred_tier3_core_start(
                running=False,
                marker=("tier3", 1),
                last_marker=("tier3", 0),
            )
        )
        self.assertTrue(
            should_attempt_deferred_tier3_core_start(
                running=True,
                marker=("tier3", 1),
                last_marker=("tier3", 1),
            )
        )

        self.assertTrue(
            should_reload_tier3_core_config_for_marker(
                marker=(),
                last_marker=("tier3", 1),
            )
        )
        self.assertFalse(
            should_reload_tier3_core_config_for_marker(
                marker=None,
                last_marker=("tier3", 1),
            )
        )
        self.assertFalse(
            should_reload_tier3_core_config_for_marker(
                marker=("tier3", 1),
                last_marker=("tier3", 1),
            )
        )

    def test_core_sync_action_preserves_running_and_empty_marker_semantics(self) -> None:
        cases = [
            ("stopping", True, True, True, ("tier3", 1), ("tier3", 0), Tier3CoreSyncAction.NONE),
            ("deferred start", False, True, False, ("tier3", 1), ("tier3", 0), Tier3CoreSyncAction.DEFERRED_START),
            ("runtime inactive", False, False, False, ("tier3", 1), ("tier3", 0), Tier3CoreSyncAction.NONE),
            ("empty stopped core", False, True, False, (), ("tier3", 0), Tier3CoreSyncAction.NONE),
            ("empty running core", False, True, True, (), ("tier3", 1), Tier3CoreSyncAction.RELOAD_RUNNING_CORE),
            ("disabled tier3", False, True, True, None, ("tier3", 1), Tier3CoreSyncAction.NONE),
            ("unchanged marker", False, True, True, ("tier3", 1), ("tier3", 1), Tier3CoreSyncAction.NONE),
            ("changed marker", False, True, True, ("tier3", 2), ("tier3", 1), Tier3CoreSyncAction.RELOAD_RUNNING_CORE),
        ]
        for name, stop_requested, runtime_running, core_running, marker, last_marker, expected in cases:
            with self.subTest(name=name):
                self.assertIs(
                    tier3_core_sync_action_after_pool_update(
                        stop_requested=stop_requested,
                        runtime_running=runtime_running,
                        core_running=core_running,
                        marker=marker,
                        last_marker=last_marker,
                    ),
                    expected,
                )

    def test_core_sync_failure_recency_preserves_retry_gate(self) -> None:
        marker = ("tier3", "node")
        self.assertTrue(
            tier3_core_sync_failure_is_recent(
                marker=marker,
                last_failed_marker=marker,
                last_failed_at=90.0,
                now=100.0,
                retry_interval=30.0,
            )
        )
        self.assertFalse(
            tier3_core_sync_failure_is_recent(
                marker=marker,
                last_failed_marker=marker,
                last_failed_at=60.0,
                now=100.0,
                retry_interval=30.0,
            )
        )
        self.assertFalse(
            tier3_core_sync_failure_is_recent(
                marker=("tier3", "new"),
                last_failed_marker=marker,
                last_failed_at=90.0,
                now=100.0,
                retry_interval=30.0,
            )
        )
        self.assertFalse(
            tier3_core_sync_failure_is_recent(
                marker=None,
                last_failed_marker=marker,
                last_failed_at=90.0,
                now=100.0,
                retry_interval=30.0,
            )
        )

    def test_core_sync_retry_interval_preserves_config_fallbacks(self) -> None:
        self.assertEqual(
            tier3_core_sync_retry_interval(
                {"tiers": {"tier3": {"core_sync_retry_interval": 5, "min_collect_interval": 60}}}
            ),
            5.0,
        )
        self.assertEqual(
            tier3_core_sync_retry_interval({"tiers": {"tier3": {"min_collect_interval": 7}}}),
            7.0,
        )
        self.assertEqual(
            tier3_core_sync_retry_interval({"tiers": {"tier3": {"core_sync_retry_interval": 0}}}),
            1.0,
        )
        self.assertEqual(
            tier3_core_sync_retry_interval(
                {"tiers": {"tier3": {"core_sync_retry_interval": None, "min_collect_interval": 7}}}
            ),
            120.0,
        )
        self.assertEqual(tier3_core_sync_retry_interval({"tiers": []}), 120.0)

    def test_reload_core_config_success_interprets_runtime_results(self) -> None:
        self.assertTrue(reload_core_config_succeeded(None))
        self.assertFalse(reload_core_config_succeeded("failed"))
        self.assertFalse(reload_core_config_succeeded({"kept_existing_core": True, "reloaded": True}))
        self.assertTrue(reload_core_config_succeeded({"reloaded": True}))
        self.assertTrue(reload_core_config_succeeded({"core": {"running": True}}))
        self.assertFalse(reload_core_config_succeeded({"core": {"running": False}}))
        self.assertFalse(reload_core_config_succeeded({"core": None}))

    def test_stop_sensitive_reload_failure_action_separates_stop_from_keep_existing(self) -> None:
        stopping = stop_sensitive_core_reload_failure_action(
            {"reloaded": False, "reason": "reload interrupted", "stop_sensitive": True},
            stop_requested=True,
        )
        self.assertEqual(stopping.unavailable_reason, "runtime stopping")
        self.assertIsNone(stopping.keep_existing_reason)

        keep_existing = stop_sensitive_core_reload_failure_action(
            {"reloaded": False, "reason": "verification failed", "stop_sensitive": True},
            stop_requested=False,
        )
        self.assertIsNone(keep_existing.unavailable_reason)
        self.assertEqual(keep_existing.keep_existing_reason, "verification failed")

        success = stop_sensitive_core_reload_failure_action({"reloaded": True}, stop_requested=True)
        self.assertIsNone(success.unavailable_reason)
        self.assertIsNone(success.keep_existing_reason)

    def test_keep_existing_core_reload_failure_result_preserves_contract(self) -> None:
        restore_reload = {"reloaded": False, "reason": "restore failed"}
        core_status = {"running": True, "pid": 123}

        self.assertEqual(
            build_keep_existing_core_reload_failure_result(
                reason="hot reload failed",
                restored_config=True,
                restore_reload=restore_reload,
                core_status=core_status,
            ),
            {
                "reloaded": False,
                "reason": "hot reload failed",
                "kept_existing_core": True,
                "restored_config": True,
                "restore_reload": restore_reload,
                "core": core_status,
            },
        )

    def test_core_reload_unavailable_result_preserves_contract(self) -> None:
        core_status = {"running": True, "pid": 456}

        result = build_core_reload_unavailable_result(
            reason="runtime stopping",
            core_status=core_status,
        )

        self.assertEqual(
            result,
            {
                "reloaded": False,
                "reason": "runtime stopping",
                "core": core_status,
            },
        )
        self.assertIs(result["core"], core_status)

    def test_tier3_core_reload_failure_result_preserves_contract(self) -> None:
        self.assertEqual(
            build_tier3_core_reload_failure_result(
                reason="verification failed",
                stop_sensitive=False,
            ),
            {
                "reloaded": False,
                "reason": "verification failed",
                "stop_sensitive": False,
            },
        )

        reload_result = {"reloaded": False, "reason": "HTTP 400"}
        result = build_tier3_core_reload_failure_result(
            reason="hot reload failed",
            stop_sensitive=True,
            reload_result=reload_result,
        )

        self.assertEqual(
            result,
            {
                "reloaded": False,
                "reason": "hot reload failed",
                "stop_sensitive": True,
                "reload": reload_result,
            },
        )
        self.assertIs(result["reload"], reload_result)

    def test_primary_layers_configured_matches_runtime_contract(self) -> None:
        self.assertFalse(primary_layers_configured({}))
        self.assertFalse(primary_layers_configured({"tiers": {"tier3": {"enabled": True}}}))
        self.assertTrue(primary_layers_configured({"tiers": {"tier1": {"nodes": [{"name": "a"}]}}}))
        self.assertTrue(primary_layers_configured({"tiers": {"tier2": {"nodes": [{"name": "b"}]}}}))
        self.assertTrue(primary_layers_configured({"tiers": {"tier2": {"subscription_urls": ["https://sub.example"]}}}))
        self.assertFalse(
            primary_layers_configured(
                {
                    "tiers": {
                        "tier2": {
                            "subscription_urls": [
                                {"url": "https://draft.example/sub", "enabled": "false"}
                            ]
                        }
                    }
                }
            )
        )
        self.assertFalse(
            primary_layers_configured(
                {
                    "tiers": {
                        "tier1": {"nodes": [{"name": "draft-one", "enabled": False}]},
                        "tier2": {"nodes": [{"name": "draft-two", "enabled": "false"}]},
                    }
                }
            )
        )
        self.assertTrue(
            primary_layers_configured(
                {
                    "tiers": {
                        "tier1": {"nodes": [{"name": "draft", "enabled": False}]},
                        "tier2": {"nodes": ["ss://legacy"]},
                    }
                }
            )
        )

    def test_primary_runtime_healthy_count_ignores_tier3(self) -> None:
        runtime_nodes = {
            "tier1": [node("alive-one", tier="tier1"), node("dead-one", tier="tier1", status=NodeStatus.DEAD)],
            "tier2": [node("slow-two", tier="tier2", status=NodeStatus.SLOW)],
            "tier3": [node("alive-three", tier="tier3")],
        }

        self.assertEqual(primary_runtime_healthy_count(runtime_nodes), 2)
        self.assertEqual(primary_runtime_healthy_count(None), 0)

    def test_tier3_hot_reload_prime_decision_requires_empty_or_unverified_tcp_pool(self) -> None:
        healthy = node("healthy")
        verified_tcp = node("verified", extra={"startup_prelaunch_validation": "tcp", "startup_core_delay_ms": 12})
        unverified_tcp = node("unverified", extra={"startup_prelaunch_validation": "tcp"})
        malformed_extra = node("malformed")
        malformed_extra.extra = []

        self.assertFalse(has_unverified_tier3_tcp_candidates([healthy, verified_tcp, malformed_extra]))
        self.assertTrue(has_unverified_tier3_tcp_candidates([healthy, unverified_tcp]))

        self.assertFalse(should_prime_tier3_pool_before_hot_reload([healthy, verified_tcp]))
        self.assertTrue(should_prime_tier3_pool_before_hot_reload([]))
        self.assertTrue(should_prime_tier3_pool_before_hot_reload([node("dead", status=NodeStatus.DEAD)]))
        self.assertTrue(should_prime_tier3_pool_before_hot_reload([healthy, unverified_tcp]))

    def test_tier3_cache_requires_core_verification_from_report_or_node_marker(self) -> None:
        self.assertTrue(tier3_cache_requires_core_verification([], {"validation": {"prelaunch_validation": "tcp"}}))
        self.assertTrue(
            tier3_cache_requires_core_verification(
                [node("tcp", extra={"startup_prelaunch_validation": "tcp"})],
                {},
            )
        )
        self.assertFalse(tier3_cache_requires_core_verification([node("plain")], {"validation": {}}))

    def test_tier3_cached_start_verification_preserves_plain_cache(self) -> None:
        cached = [node("plain")]

        self.assertEqual(
            tier3_cached_start_verification(cached, {"validation": {}}),
            Tier3CachedStartVerification(
                nodes=cached,
                requires_core_verification=False,
                discarded_count=0,
            ),
        )

    def test_tier3_cached_start_verification_discards_unverified_tcp_cache(self) -> None:
        verified = node("verified", extra={"startup_prelaunch_validation": "tcp", "startup_core_delay_ms": 12})
        unverified = node("unverified", extra={"startup_prelaunch_validation": "tcp"})

        self.assertEqual(
            tier3_cached_start_verification(
                [verified, unverified],
                {"validation": {"prelaunch_validation": "tcp"}},
            ),
            Tier3CachedStartVerification(
                nodes=[verified],
                requires_core_verification=True,
                discarded_count=1,
            ),
        )

    def test_tier3_startup_core_verified_nodes_accept_probe_url_or_delay_marker(self) -> None:
        url_verified = node("url", extra={"startup_core_probe_url": "https://cp.cloudflare.com"})
        delay_verified = node("delay", extra={"startup_core_delay_ms": 0})
        unverified = node("plain", extra={"startup_prelaunch_validation": "tcp"})

        self.assertTrue(node_has_startup_core_verification(url_verified))
        self.assertTrue(node_has_startup_core_verification(delay_verified))
        self.assertFalse(node_has_startup_core_verification(unverified))
        self.assertEqual(tier3_startup_core_verified_nodes([url_verified, delay_verified, unverified]), [url_verified, delay_verified])

    def test_tier3_primary_export_and_empty_export_decisions_are_explicit(self) -> None:
        self.assertTrue(tier3_is_primary_export(primary_configured=False, tier3_required_for_start=False, core_sync_marker=("a",)))
        self.assertTrue(tier3_is_primary_export(primary_configured=True, tier3_required_for_start=True, core_sync_marker=("a",)))
        self.assertFalse(tier3_is_primary_export(primary_configured=True, tier3_required_for_start=False, core_sync_marker=("a",)))
        self.assertFalse(tier3_is_primary_export(primary_configured=False, tier3_required_for_start=True, core_sync_marker=()))

        self.assertTrue(tier3_enabled_without_primary(tier3_enabled=True, primary_configured=False))
        self.assertFalse(tier3_enabled_without_primary(tier3_enabled=True, primary_configured=True))

        self.assertFalse(
            should_keep_existing_core_for_empty_tier3_export(
                tier3_enabled=False,
                primary_configured=False,
                tier3_required_for_start=False,
                primary_healthy_count=0,
            )
        )
        self.assertTrue(
            should_keep_existing_core_for_empty_tier3_export(
                tier3_enabled=True,
                primary_configured=False,
                tier3_required_for_start=False,
                primary_healthy_count=99,
            )
        )
        self.assertTrue(
            should_keep_existing_core_for_empty_tier3_export(
                tier3_enabled=True,
                primary_configured=True,
                tier3_required_for_start=True,
                primary_healthy_count=99,
            )
        )
        self.assertTrue(
            should_keep_existing_core_for_empty_tier3_export(
                tier3_enabled=True,
                primary_configured=True,
                tier3_required_for_start=False,
                primary_healthy_count=0,
            )
        )
        self.assertFalse(
            should_keep_existing_core_for_empty_tier3_export(
                tier3_enabled=True,
                primary_configured=True,
                tier3_required_for_start=False,
                primary_healthy_count=1,
            )
        )

    def test_empty_tier3_marker_keep_existing_core_decision_is_explicit(self) -> None:
        self.assertTrue(
            should_keep_existing_core_for_empty_tier3_marker(
                marker=(),
                keep_existing_for_empty_export=True,
            )
        )
        self.assertFalse(
            should_keep_existing_core_for_empty_tier3_marker(
                marker=(),
                keep_existing_for_empty_export=False,
            )
        )
        self.assertFalse(
            should_keep_existing_core_for_empty_tier3_marker(
                marker=None,
                keep_existing_for_empty_export=True,
            )
        )
        self.assertFalse(
            should_keep_existing_core_for_empty_tier3_marker(
                marker=("tier3", "node"),
                keep_existing_for_empty_export=True,
            )
        )

    def test_tier3_runtime_export_limit_preserves_startup_probe_expansion(self) -> None:
        cases = [
            ("normal primary export", 1, 3, False, True, False, 1),
            ("no primary layers", 1, 3, False, False, False, 3),
            ("tier3 required for start", 1, 3, True, True, False, 3),
            ("tcp prelaunch validation", 1, 3, False, True, True, 3),
            ("configured limit already higher", 5, 3, True, False, True, 5),
        ]
        for (
            name,
            configured_limit,
            startup_probe_limit,
            tier3_required,
            primary_configured,
            tcp_prelaunch,
            expected,
        ) in cases:
            with self.subTest(name=name):
                self.assertEqual(
                    tier3_runtime_export_limit(
                        configured_limit=configured_limit,
                        startup_probe_limit=startup_probe_limit,
                        tier3_required_for_start=tier3_required,
                        primary_configured=primary_configured,
                        uses_tcp_prelaunch_validation=tcp_prelaunch,
                    ),
                    expected,
                )

    def test_tier3_core_repair_target_policy_uses_startup_for_primary_export_safety(self) -> None:
        self.assertFalse(
            tier3_core_repair_uses_startup_target(
                tier3_required_for_start=False,
                uses_tcp_prelaunch_validation=False,
            )
        )
        self.assertTrue(
            tier3_core_repair_uses_startup_target(
                tier3_required_for_start=True,
                uses_tcp_prelaunch_validation=False,
            )
        )
        self.assertTrue(
            tier3_core_repair_uses_startup_target(
                tier3_required_for_start=False,
                uses_tcp_prelaunch_validation=True,
            )
        )

    def test_tier3_core_repair_targets_preserve_non_pruning_defaults(self) -> None:
        targets = tier3_core_repair_targets(
            prune_to_successful=False,
            target_alive=5,
            minimum_alive=4,
            default_target_alive=3,
        )

        self.assertEqual(targets.target_verified, 1)
        self.assertEqual(targets.minimum_verified, 1)

    def test_tier3_core_repair_targets_clamp_target_and_minimum(self) -> None:
        targets = tier3_core_repair_targets(
            prune_to_successful=True,
            target_alive=2,
            minimum_alive=3,
            default_target_alive=5,
        )

        self.assertEqual(targets.target_verified, 3)
        self.assertEqual(targets.minimum_verified, 3)

    def test_tier3_core_repair_targets_use_default_when_target_missing(self) -> None:
        targets = tier3_core_repair_targets(
            prune_to_successful=True,
            target_alive=None,
            minimum_alive=None,
            default_target_alive=4,
        )

        self.assertEqual(targets.target_verified, 4)
        self.assertEqual(targets.minimum_verified, 4)

    def test_tier3_tcp_prelaunch_export_decision_uses_report_or_node_marker(self) -> None:
        self.assertTrue(
            tier3_export_uses_tcp_prelaunch_validation(
                report={"startup_mode": True, "validation": {"prelaunch_validation": "tcp"}},
                nodes=[],
            )
        )
        self.assertTrue(
            tier3_export_uses_tcp_prelaunch_validation(
                report={},
                nodes=[node("tcp", extra={"startup_prelaunch_validation": "tcp"})],
            )
        )
        self.assertFalse(
            tier3_export_uses_tcp_prelaunch_validation(
                report={
                    "startup_mode": False,
                    "validation": {"prelaunch_validation": "tcp"},
                },
                nodes=[node("tcp", extra={"startup_prelaunch_validation": "tcp"})],
            )
        )
        malformed_extra = node("malformed")
        malformed_extra.extra = []
        self.assertFalse(
            tier3_export_uses_tcp_prelaunch_validation(
                report={},
                nodes=[malformed_extra, node("plain")],
            )
        )

    def test_tier3_startup_failure_detail_preserves_diagnostic_parts(self) -> None:
        alive = node("alive")
        first_failed = node("failed-one", status=NodeStatus.SUSPICIOUS)
        first_failed.suspicious_reason = "timeout"
        duplicate_failed = node("failed-duplicate", status=NodeStatus.DEAD)
        duplicate_failed.suspicious_reason = "timeout"
        second_failed = node("failed-two", status=NodeStatus.DEAD)
        second_failed.suspicious_reason = "tls"

        self.assertEqual(
            tier3_startup_failure_detail(
                primary_startup_error="primary unavailable",
                report={
                    "source_count": 4,
                    "fetched_sources": 1,
                    "failed_sources": 3,
                    "node_count": 6,
                    "sources": [
                        {"value": "ok", "ok": True},
                        {"value": "bad-a", "ok": False},
                        {"id": "bad-b", "ok": False},
                        {"value": "bad-c", "ok": False},
                    ],
                    "validation": {
                        "tested_count": 2,
                        "candidate_count": 5,
                        "limit": 3,
                    },
                },
                nodes=[alive, first_failed, duplicate_failed, second_failed],
                last_startup_error="collection failed",
            ),
            "primary=primary unavailable; "
            "collection sources=4, fetched=1, failed=3, parsed_nodes=6; "
            "source_supply phase=unknown, reason=source_fetch_failed, diagnosis=none, "
            "terminal=none, "
            "failure_class=transport_failed, blocked=0, failed=3; "
            "source_failures rule_blocked=0, no_proxy=0, transport_failed=3; "
            "validation tested=2/5, limit=3; "
            "validation healthy=1/4; "
            "sample_validation_errors=timeout | tls; "
            "startup_error=collection failed",
        )

    def test_tier3_startup_failure_detail_redacts_source_identity_and_classifies_failures(self) -> None:
        secret_source = (
            "https://user:top-secret@source.example/sub?token=query-secret"
        )
        cases = (
            ("runtime_source_retry_disabled", "blocked_pre_core", "rule_blocked"),
            ("prerequisite_missing: geodata", "blocked_pre_core", "rule_blocked"),
            ("rule_blocked: REJECT", "pre_core_remote", "rule_blocked"),
            (f"blocked_no_route: source={secret_source}", "blocked_pre_core", "no_proxy"),
            ("runtime_egress_recovery_failed", "pre_core_remote", "transport_failed"),
        )
        for blocked_reason, source_phase, expected_class in cases:
            with self.subTest(expected_class=expected_class):
                detail = tier3_startup_failure_detail(
                    primary_startup_error=(
                        f"source failed: {secret_source}"
                    ),
                    report={
                        "source_count": 2,
                        "fetched_sources": 0,
                        "failed_sources": 2,
                        "node_count": 0,
                        "sources": [
                            {"value": secret_source, "ok": False},
                            {"id": "source-id-with-top-secret", "ok": False},
                        ],
                        "source_supply_phase": source_phase,
                        "source_retry_blocked_reason": blocked_reason,
                        "source_stream_sampling_diagnosis": secret_source,
                        "source_supply_blocked_count": 2,
                        "cache_local_healthy_count": 0,
                        "cache_local_required_count": 3,
                    },
                    nodes=[],
                    last_startup_error=(
                        "transport password=plain-secret via "
                        "socks5://proxy-user:proxy-secret@127.0.0.1:1080?token=proxy-query"
                    ),
                )

                self.assertNotIn("top-secret", detail)
                self.assertNotIn("query-secret", detail)
                self.assertNotIn("plain-secret", detail)
                self.assertNotIn("proxy-secret", detail)
                self.assertNotIn("proxy-query", detail)
                self.assertNotIn(secret_source, detail)
                self.assertNotIn("source-id-with", detail)
                self.assertIn("https://source.example", detail)
                self.assertIn("socks5://127.0.0.1:1080", detail)
                self.assertIn("password=<redacted>", detail)
                self.assertIn(f"failure_class={expected_class}", detail)
                self.assertIn("cache_local healthy=0/3", detail)
                self.assertIn(
                    f"{expected_class}=2",
                    detail,
                )

    def test_tier3_startup_failure_detail_handles_missing_report(self) -> None:
        self.assertEqual(
            tier3_startup_failure_detail(
                primary_startup_error=None,
                report=[],
                nodes=[],
                last_startup_error="collection failed",
            ),
            "startup_error=collection failed",
        )

    def test_tier3_export_signature_tracks_rendered_proxy_not_runtime_metadata(self) -> None:
        marked = node("marked", extra={"tier3_probe_attempt": 1})
        initial = tier3_export_node_signature(marked)

        marked.extra["tier3_probe_attempt"] = 99
        marked.extra["tier3_source_route"] = "runtime-private"
        self.assertEqual(tier3_export_node_signature(marked), initial)

        marked.password = "changed-password"
        self.assertNotEqual(tier3_export_node_signature(marked), initial)

        unnamed = node("unnamed")
        unnamed.name = ""
        self.assertEqual(
            tier3_export_signature([unnamed])[0][0],
            "ss-unnamed.example.com:8388",
        )

    def test_core_export_semantic_digest_covers_groups_routes_listeners_and_overlay(self) -> None:
        rendered = {
            "proxies": [{"name": "leaf-a", "type": "ss", "server": "a"}],
            "proxy-groups": [{"name": "TIER3-AUTO", "proxies": ["leaf-a"]}],
            "listeners": [{"name": "tier3-source-final", "port": 12345}],
            "rules": ["MATCH,FINAL-EXIT"],
        }
        initial = CoreExportSemanticDigest.from_rendered_config(
            rendered,
            overlay_membership=("leaf-b",),
        )
        same = CoreExportSemanticDigest.from_rendered_config(
            rendered,
            overlay_membership=("leaf-b",),
        )
        changed_group = CoreExportSemanticDigest.from_rendered_config(
            {
                **rendered,
                "proxy-groups": [
                    {"name": "TIER3-AUTO", "proxies": ["leaf-b", "leaf-a"]}
                ],
            },
            overlay_membership=("leaf-b",),
        )
        changed_overlay = CoreExportSemanticDigest.from_rendered_config(
            rendered,
            overlay_membership=(),
        )

        self.assertEqual(initial, same)
        self.assertNotEqual(initial.digest, changed_group.digest)
        self.assertNotEqual(initial.digest, changed_overlay.digest)
        self.assertEqual(initial.proxy_names, ("leaf-a",))
        self.assertEqual(
            initial.group_membership,
            (("TIER3-AUTO", ("leaf-a",)),),
        )

    def test_tier3_probe_urls_for_node_preserves_custom_first_and_dedupes_defaults(self) -> None:
        custom = node(
            "custom",
            extra={
                "validation_probe_url": " https://custom.example/ping ",
                "validation_probe_urls": [
                    "https://backup.example/ping",
                    " https://custom.example/ping ",
                    "",
                    "https://third.example/ping",
                ],
            },
        )

        self.assertEqual(
            tier3_probe_urls_for_node(
                custom,
                [
                    "https://default.example/generate_204",
                    "https://custom.example/ping",
                    "https://backup.example/ping",
                    "https://default.example/generate_204",
                ],
            ),
            [
                "https://custom.example/ping",
                "https://backup.example/ping",
                "https://third.example/ping",
                "https://default.example/generate_204",
            ],
        )

        malformed_extra = node("malformed")
        malformed_extra.extra = []
        self.assertEqual(
            tier3_probe_urls_for_node(
                malformed_extra,
                ["https://one.example", "https://one.example", "https://two.example"],
            ),
            ["https://one.example", "https://two.example"],
        )


if __name__ == "__main__":
    unittest.main()
