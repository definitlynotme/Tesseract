import unittest

from models import Node, NodeStatus
from tier3_pool_view import (
    Tier3AutoPoolCountSnapshot,
    Tier3PoolCountSnapshot,
    Tier3StartupGateProgress,
    Tier3UserPoolHealthSnapshot,
    node_has_startup_core_verification,
    node_is_unverified_tcp_startup_candidate,
    tier3_collection_core_counts,
    tier3_collection_stats,
    tier3_core_verified_nodes_for_display,
    tier3_display_status_counts,
    tier3_effective_nodes_for_display,
    tier3_main_pool_nodes_for_display,
    tier3_pool_chart_stats,
    tier3_stats_snapshot,
)


def node(name: str, *, status: NodeStatus = NodeStatus.ALIVE, extra: dict | None = None) -> Node:
    return Node(
        name=name,
        protocol="ss",
        uri=f"ss://{name}",
        address=f"{name}.example.com",
        port=8388,
        tier="tier3",
        status=status,
        method="aes-256-gcm",
        password="pass",
        extra=extra or {},
    )


class Tier3PoolViewTests(unittest.TestCase):
    def test_collection_stats_expose_mutually_exclusive_stream_outcomes(self) -> None:
        collection = tier3_collection_stats(
            report={
                "source_stream_failed_count": 99,
                "source_stream_tcp_failed_count": 5,
                "source_stream_full_validation_failed_count": 2,
                "source_stream_tcp_deferred_count": 10,
                "source_stream_full_validation_deferred_count": 3,
                "source_stream_full_validation_cancelled_count": 1,
                "source_stream_unsupported_proxy_count": 4,
            },
            nodes=[],
            tier3_source_count=0,
            background_target_alive=25,
            next_collect_due_seconds=0,
            refresh_progress={
                "running": True,
                "source_stream_tcp_failed_count": 6,
                "source_stream_full_validation_failed_count": 2,
                "source_stream_tcp_deferred_count": 14,
                "source_stream_full_validation_deferred_count": 1,
                "source_stream_tcp_cancelled_count": 2,
                "source_stream_unsupported_proxy_count": 5,
            },
        )

        self.assertEqual(collection["source_stream_hard_failed_count"], 8)
        self.assertEqual(collection["source_stream_failed_count"], 8)
        self.assertEqual(collection["source_stream_deferred_count"], 15)
        self.assertEqual(collection["source_stream_cancelled_count"], 2)
        self.assertEqual(collection["source_stream_unsupported_count"], 5)
        self.assertEqual(collection["refresh_source_stream_hard_failed_count"], 8)
        self.assertEqual(collection["refresh_source_stream_deferred_count"], 15)

    def test_core_verification_accepts_probe_url_or_delay_marker(self) -> None:
        self.assertTrue(node_has_startup_core_verification(node("url", extra={"startup_core_probe_url": "https://cp.cloudflare.com"})))
        self.assertTrue(node_has_startup_core_verification(node("delay", extra={"startup_core_delay_ms": 0})))
        self.assertFalse(node_has_startup_core_verification(node("plain")))

    def test_tcp_candidate_detection_requires_tcp_marker_without_core_verification(self) -> None:
        self.assertTrue(
            node_is_unverified_tcp_startup_candidate(
                node("candidate", extra={"startup_prelaunch_validation": "tcp"})
            )
        )
        self.assertFalse(
            node_is_unverified_tcp_startup_candidate(
                node(
                    "verified",
                    extra={
                        "startup_prelaunch_validation": "tcp",
                        "startup_core_delay_ms": 12,
                    },
                )
            )
        )
        self.assertFalse(node_is_unverified_tcp_startup_candidate(node("plain")))

    def test_display_nodes_filter_tcp_candidates_but_keep_core_verified_nodes(self) -> None:
        tcp_candidate = node("candidate", extra={"startup_prelaunch_validation": "tcp"})
        core_verified = node(
            "verified",
            status=NodeStatus.SLOW,
            extra={
                "startup_prelaunch_validation": "tcp",
                "startup_core_probe_url": "https://cp.cloudflare.com",
            },
        )

        self.assertEqual(
            tier3_main_pool_nodes_for_display(
                [tcp_candidate, core_verified],
                {"validation": {"prelaunch_validation": "tcp"}},
            ),
            [core_verified],
        )

    def test_display_nodes_filter_pending_cache_revalidation_nodes(self) -> None:
        pending = node(
            "pending",
            status=NodeStatus.UNKNOWN,
            extra={"tier3_cache_revalidation_pending": True},
        )
        active = node("active", status=NodeStatus.ALIVE)

        self.assertEqual(
            tier3_main_pool_nodes_for_display([pending, active], {}),
            [active],
        )
        stats = tier3_stats_snapshot(
            enabled=True,
            nodes=[pending],
            report={},
            tier3_source_count=1,
            background_target_alive=25,
            next_collect_due_seconds=0,
            level=1,
            level_reason="startup",
            controller={},
            revival_count=0,
        )

        self.assertEqual(stats["total"], 0)
        self.assertEqual(stats["healthy"], 0)
        self.assertEqual(stats["raw_total"], 1)

    def test_display_nodes_ignore_stale_tcp_report_when_nodes_are_plain_active_pool(self) -> None:
        active = node("active")

        self.assertEqual(
            tier3_main_pool_nodes_for_display(
                [active],
                {"validation": {"prelaunch_validation": "tcp"}},
            ),
            [active],
        )

    def test_display_nodes_filter_by_report_when_core_markers_indicate_tcp_startup(self) -> None:
        verified = node("verified", extra={"startup_core_delay_ms": 1})
        plain = node("plain")

        self.assertEqual(
            tier3_main_pool_nodes_for_display(
                [verified, plain],
                {"validation": {"prelaunch_validation": "tcp"}},
            ),
            [verified, plain],
        )
        self.assertEqual(
            tier3_core_verified_nodes_for_display(
                [verified, plain],
                {"validation": {"prelaunch_validation": "tcp"}},
            ),
            [verified, plain],
        )

    def test_collection_and_display_counts_match_manager_snapshot_contract(self) -> None:
        nodes = [
            node("candidate", extra={"startup_prelaunch_validation": "tcp"}),
            node("verified-slow", status=NodeStatus.SLOW, extra={"startup_core_delay_ms": 9}),
            node("verified-dead", status=NodeStatus.DEAD, extra={"startup_core_delay_ms": 10}),
            node("suspicious", status=NodeStatus.SUSPICIOUS),
        ]

        self.assertEqual(
            tier3_collection_core_counts(nodes),
            {"core_verified_count": 2, "core_usable_count": 1, "tcp_candidate_count": 1},
        )
        self.assertEqual(
            tier3_display_status_counts(nodes),
            {"total": 4, "alive": 1, "slow": 1, "healthy": 2, "dead": 1, "suspicious": 1},
        )

    def test_collection_stats_preserve_snapshot_contract_and_use_current_target(self) -> None:
        nodes = [
            node("candidate", extra={"startup_prelaunch_validation": "tcp"}),
            node("verified-slow", status=NodeStatus.SLOW, extra={"startup_core_delay_ms": 9}),
        ]

        stats = tier3_collection_stats(
            report={
                "source_count": 4,
                "fetched_sources": 3,
                "failed_sources": 1,
                "node_count": 8,
                "target_alive": 2,
                "background_target_alive": 25,
                "startup_mode": "true",
                "background_expansion_due": True,
                "underfilled_expansion_mode": True,
                "partial_refresh_buffered": False,
                "retained_previous_candidate_count": 1,
                "candidate_healthy_count": 2,
                "post_buffer_healthy_count": 3,
                "previous_healthy_count": 4,
                "validation": {
                    "candidate_count": 7,
                    "original_candidate_count": 9,
                    "negative_cache_skipped_count": 2,
                    "selected_candidate_count": 5,
                    "tested_count": 6,
                    "tested_unusable_count": 1,
                    "revival_unusable_count": 1,
                    "target_alive": 3,
                    "tcp_prefilter_tested_count": 4,
                    "tcp_prefilter_responsive_count": 2,
                    "prelaunch_validation": "tcp",
                },
                "startup_core_repair": {
                    "working_count": 1,
                    "pruned_count": 2,
                },
            },
            nodes=nodes,
            tier3_source_count=99,
            background_target_alive=10,
            next_collect_due_seconds=12,
        )

        self.assertEqual(
            stats,
            {
                "source_count": 4,
                "fetched_sources": 3,
                "failed_sources": 1,
                "node_count": 8,
                "candidate_count": 7,
                "original_candidate_count": 9,
                "negative_cache_skipped_count": 2,
                "selected_candidate_count": 5,
                "tested_count": 6,
                "tested_unusable_count": 1,
                "revival_unusable_count": 1,
                "target_alive_count": 3,
                "background_target_alive_count": 10,
                "startup_gate_alive_count": 0,
                "pi_intensity": 0.0,
                "expansion_pressure": 0.0,
                "tier3_throughput_pressure": 0.0,
                "reserve_deficit": 0,
                "reserve_deficit_ratio": 0.0,
                "backlog_pressure": 0.0,
                "recent_pass_rate": 1.0,
                "pass_rate_pressure": 0.0,
                "pressure_source_budget": 0,
                "validation_budget": 0,
                "validation_session_wave_count": 0,
                "validation_session_budget": 0,
                "validation_batch_size": 0,
                "validation_concurrency": 0,
                "validation_worker_count": 0,
                "validation_chunk_size": 0,
                "source_stream_budget": 0,
                "source_stream_queued_candidate_count": 0,
                "source_stream_tested_count": 0,
                "source_stream_observed_candidate_count": 0,
                "source_stream_healthy_observed_count": 0,
                "source_stream_hard_failed_count": 0,
                "source_stream_failed_count": 0,
                "source_stream_deferred_count": 0,
                "source_stream_cancelled_count": 0,
                "source_stream_unsupported_count": 0,
                "source_stream_committed_alive_count": 0,
                "source_stream_committed_total_count": 0,
                "source_stream_streamed_batch_count": 0,
                "source_stream_backlog_count": 0,
                "source_stream_queued_batches": 0,
                "source_stream_inflight_batches": 0,
                "source_stream_window": 0,
                "tcp_prefilter_tested_count": 4,
                "tcp_prefilter_responsive_count": 2,
                "prelaunch_validation": "tcp",
                "startup_mode": True,
                "background_expansion_due": True,
                "underfilled_expansion_mode": True,
                "partial_refresh_buffered": False,
                "retained_previous_candidate_count": 1,
                "candidate_healthy_count": 2,
                "post_buffer_healthy_count": 3,
                "previous_healthy_count": 4,
                "core_working_count": 1,
                "core_pruned_count": 2,
                "next_collect_due_seconds": 12,
                "runtime_qualified_count": 0,
                "runtime_exported_count": 0,
                "core_verified_count": 1,
                "core_usable_count": 1,
                "tcp_candidate_count": 1,
            },
        )

    def test_collection_stats_fall_back_when_report_is_missing(self) -> None:
        self.assertEqual(
            tier3_collection_stats(
                report=[],
                nodes=[],
                tier3_source_count=3,
                background_target_alive=5,
                next_collect_due_seconds=0,
            )["source_count"],
            3,
        )
        self.assertEqual(
            tier3_collection_stats(
                report={},
                nodes=[],
                tier3_source_count=3,
                background_target_alive=5,
                next_collect_due_seconds=0,
            )["background_target_alive_count"],
            5,
        )

    def test_collection_stats_use_current_background_target_over_stale_report(self) -> None:
        stats = tier3_collection_stats(
            report={"background_target_alive": 80},
            nodes=[],
            tier3_source_count=3,
            background_target_alive=42,
            next_collect_due_seconds=0,
            refresh_progress={"background_target_alive": 80},
        )

        self.assertEqual(stats["background_target_alive_count"], 42)
        self.assertEqual(stats["refresh_background_target_alive_count"], 80)

    def test_collection_stats_prefers_live_runtime_qualification_truth(self) -> None:
        stats = tier3_collection_stats(
            report={
                "runtime_qualified_count": 1,
                "runtime_exported_count": 2,
            },
            nodes=[],
            tier3_source_count=5,
            background_target_alive=25,
            next_collect_due_seconds=0,
            refresh_progress={
                "runtime_qualified_count": 3,
                "runtime_exported_count": 14,
            },
        )

        self.assertEqual(stats["runtime_qualified_count"], 3)
        self.assertEqual(stats["runtime_exported_count"], 14)

    def test_collection_stats_overlay_live_refresh_progress(self) -> None:
        stats = tier3_collection_stats(
            report={
                "validation": {
                    "candidate_count": 2,
                    "tested_count": 1,
                    "validation_lane_counts": {"exploration": 4, "incumbent_retained": 3},
                }
            },
            nodes=[],
            tier3_source_count=3,
            background_target_alive=5,
            next_collect_due_seconds=0,
            refresh_progress={
                "running": True,
                "phase": "validating",
                "candidate_count": 8,
                "selected_candidate_count": 6,
                "tested_count": 4,
                "healthy_count": 3,
                "committed_alive_count": 3,
                "committed_total_count": 4,
                "target_alive": 25,
                "background_target_alive": 25,
                "added_count": 1,
                "validation_lane_counts": {"exploration": 4, "incumbent_retained": 3},
            },
        )

        self.assertTrue(stats["refresh_running"])
        self.assertEqual(stats["refresh_phase"], "validating")
        self.assertEqual(stats["candidate_count"], 8)
        self.assertEqual(stats["selected_candidate_count"], 6)
        self.assertEqual(stats["tested_count"], 4)
        self.assertEqual(stats["refresh_added_count"], 1)
        self.assertEqual(stats["refresh_committed_total_count"], 4)
        self.assertEqual(stats["refresh_target_alive_count"], 25)
        self.assertEqual(stats["refresh_background_target_alive_count"], 25)
        self.assertEqual(stats["validation_lane_counts"], {"exploration": 4, "incumbent_retained": 3})
        self.assertEqual(stats["refresh_validation_lane_counts"], {"exploration": 4, "incumbent_retained": 3})

    def test_tier3_stats_snapshot_preserves_manager_contract_and_honest_raw_total(self) -> None:
        tcp_candidate = node("candidate", extra={"startup_prelaunch_validation": "tcp"})
        verified_slow = node("verified", status=NodeStatus.SLOW, extra={"startup_core_delay_ms": 9})

        stats = tier3_stats_snapshot(
            enabled=True,
            nodes=[tcp_candidate, verified_slow],
            report={"validation": {"prelaunch_validation": "tcp"}, "source_count": 4},
            tier3_source_count=99,
            background_target_alive=10,
            next_collect_due_seconds=12,
            level=2,
            level_reason="testing",
            controller={"state": "ok"},
            revival_count=3,
        )

        self.assertTrue(stats["enabled"])
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["raw_total"], 2)
        self.assertEqual(stats["slow"], 1)
        self.assertEqual(stats["healthy"], 1)
        self.assertEqual(stats["level"], 2)
        self.assertEqual(stats["level_reason"], "testing")
        self.assertEqual(stats["controller"], {"state": "ok"})
        self.assertEqual(stats["collection"]["source_count"], 4)
        self.assertEqual(stats["collection"]["next_collect_due_seconds"], 12)
        self.assertEqual(stats["revival_count"], 3)

    def test_pool_snapshot_and_startup_gate_use_independent_denominators(self) -> None:
        stats = tier3_stats_snapshot(
            enabled=True,
            nodes=[node("first"), node("second")],
            report={"source_stream_admission_commit_pending_count": 1},
            tier3_source_count=5,
            background_target_alive=25,
            next_collect_due_seconds=0,
            level=1,
            level_reason="collecting",
            controller={},
            revival_count=0,
            pool_count_snapshot=Tier3PoolCountSnapshot(
                revision=7,
                healthy=2,
                total=2,
            ),
            startup_gate_progress=Tier3StartupGateProgress(
                healthy=2,
                required=3,
            ),
        )

        self.assertEqual((stats["healthy"], stats["total"]), (2, 2))
        self.assertEqual(stats["pool_revision"], 7)
        self.assertEqual(stats["collection"]["pool_total_count"], 2)
        self.assertEqual(stats["collection"]["startup_gate_healthy_count"], 2)
        self.assertEqual(stats["collection"]["startup_gate_required_count"], 3)
        self.assertEqual(stats["collection"]["pending_commit_count"], 1)

    def test_tier3_stats_snapshot_splits_total_tier3_from_auto_pool(self) -> None:
        stats = tier3_stats_snapshot(
            enabled=True,
            nodes=[
                node("tier3a-alive", extra={"tier3_origin": "tier3a"}),
                node("tier3a-slow", status=NodeStatus.SLOW, extra={"source_origin": "tier3a"}),
                node("tier3b-alive", extra={"tier3_origin": "tier3b"}),
                node("tier3b-dead", status=NodeStatus.DEAD, extra={"tier3_origin": "tier3b"}),
            ],
            report={},
            tier3_source_count=1,
            background_target_alive=25,
            next_collect_due_seconds=0,
            level=2,
            level_reason="mixed",
            controller={},
            revival_count=0,
        )

        self.assertEqual(stats["total"], 4)
        self.assertEqual(stats["healthy"], 3)
        self.assertEqual(stats["user_pool"]["total"], 2)
        self.assertEqual(stats["user_pool"]["healthy"], 2)
        self.assertEqual(stats["auto_pool"]["total"], 2)
        self.assertEqual(stats["auto_pool"]["healthy"], 1)
        self.assertFalse(stats["core_inventory_verified"])
        self.assertNotIn("current_healthy", stats)
        self.assertNotIn("current_total", stats)
        self.assertEqual(tier3_pool_chart_stats(stats), {})

    def test_runtime_bad_auto_leaf_uses_manager_health_snapshot_not_report_overlay(self) -> None:
        stats = tier3_stats_snapshot(
            enabled=True,
            nodes=[
                node("tier3a", extra={"tier3_origin": "tier3a"}),
                node("auto-a", extra={"tier3_origin": "tier3b"}),
                node("auto-b", extra={"tier3_origin": "tier3b"}),
            ],
            report={
                "runtime_overlay_unavailable_count": 1,
                "runtime_display_healthy_count": 0,
                "runtime_auto_pool_display_healthy_count": 0,
            },
            tier3_source_count=1,
            background_target_alive=25,
            next_collect_due_seconds=0,
            level=2,
            level_reason="runtime",
            controller={},
            revival_count=0,
            pool_count_snapshot=Tier3PoolCountSnapshot(
                revision=4,
                healthy=2,
                total=3,
            ),
            auto_pool_count_snapshot=Tier3AutoPoolCountSnapshot(
                revision=4,
                healthy=1,
                total=2,
            ),
            user_pool_health_snapshot=Tier3UserPoolHealthSnapshot(
                revision=2,
                healthy=1,
                total=1,
            ),
            runtime_truth={
                "core_inventory_verified": True,
                "core_inventory_total": 3,
                "core_inventory_healthy": 2,
                "core_inventory_auto_total": 2,
                "core_inventory_auto_healthy": 1,
                "core_inventory_user_total": 1,
                "core_inventory_user_healthy": 1,
            },
        )

        self.assertEqual((stats["current_healthy"], stats["current_total"]), (2, 3))
        self.assertEqual((stats["healthy"], stats["total"]), (2, 3))
        self.assertEqual(
            (stats["auto_pool"]["current_healthy"], stats["auto_pool"]["total"]),
            (1, 2),
        )
        self.assertEqual(tier3_pool_chart_stats(stats), {"healthy": 1, "total": 2})

    def test_core_inventory_readback_controls_current_ratio_and_chart_denominator(self) -> None:
        nodes = [
            *[
                node(f"auto-{index:02d}", extra={"tier3_origin": "tier3b"})
                for index in range(15)
            ],
            node(
                "tier3a-dead",
                status=NodeStatus.DEAD,
                extra={"tier3_origin": "tier3a"},
            ),
        ]
        stats = tier3_stats_snapshot(
            enabled=True,
            nodes=nodes,
            report={
                "runtime_qualified_count": 5,
                "runtime_exported_count": 8,
            },
            tier3_source_count=1,
            background_target_alive=25,
            next_collect_due_seconds=0,
            level=2,
            level_reason="runtime",
            controller={},
            revival_count=0,
            pool_count_snapshot=Tier3PoolCountSnapshot(
                revision=9,
                healthy=15,
                total=16,
            ),
            auto_pool_count_snapshot=Tier3AutoPoolCountSnapshot(
                revision=9,
                healthy=15,
                total=15,
            ),
            user_pool_health_snapshot=Tier3UserPoolHealthSnapshot(
                revision=4,
                healthy=0,
                total=1,
            ),
            runtime_truth={
                "core_inventory_verified": True,
                "core_inventory_total": 13,
                "core_inventory_healthy": 12,
                "core_inventory_auto_total": 13,
                "core_inventory_auto_healthy": 12,
                "core_inventory_user_total": 0,
                "core_inventory_user_healthy": 0,
            },
        )

        self.assertEqual((stats["healthy"], stats["total"]), (15, 16))
        self.assertEqual(
            (stats["current_healthy"], stats["current_total"]),
            (12, 13),
        )
        self.assertEqual(
            (
                stats["auto_pool"]["healthy"],
                stats["auto_pool"]["total"],
            ),
            (15, 15),
        )
        self.assertEqual(
            (
                stats["auto_pool"]["current_healthy"],
                stats["auto_pool"]["current_total"],
            ),
            (12, 13),
        )
        self.assertEqual(
            tier3_pool_chart_stats(stats),
            {"healthy": 12, "total": 13},
        )
        self.assertEqual(
            (
                stats["collection"]["runtime_qualified_count"],
                stats["collection"]["runtime_exported_count"],
            ),
            (5, 8),
        )

    def test_runtime_bad_leaf_visible_ratio_follows_self_check_lifecycle(self) -> None:
        auto_nodes = [
            node("auto-a", extra={"tier3_origin": "tier3b"}),
            node("auto-b", extra={"tier3_origin": "tier3b"}),
            node("auto-c", extra={"tier3_origin": "tier3b"}),
        ]

        class HealthOwner:
            def __init__(self, unavailable):
                self.unavailable = set(unavailable)

            def runtime_effective_nodes(self, tier, nodes):
                self.assert_tier = tier
                result = [item.clone() for item in nodes]
                for item in result:
                    if item.name in self.unavailable:
                        item.status = NodeStatus.SUSPICIOUS
                        item.latency_ms = None
                return result

        def ratio(nodes, unavailable=()):
            unavailable_names = set(unavailable)
            current_healthy = sum(
                1
                for item in nodes
                if item.name not in unavailable_names
                and item.status in {NodeStatus.ALIVE, NodeStatus.SLOW}
            )
            stats = tier3_stats_snapshot(
                enabled=True,
                nodes=nodes,
                report={},
                tier3_source_count=1,
                background_target_alive=25,
                next_collect_due_seconds=0,
                level=2,
                level_reason="runtime",
                controller={},
                revival_count=0,
                runtime_health_owner=HealthOwner(unavailable),
                runtime_truth={
                    "core_inventory_verified": True,
                    "core_inventory_total": len(nodes),
                    "core_inventory_healthy": current_healthy,
                    "core_inventory_auto_total": len(nodes),
                    "core_inventory_auto_healthy": current_healthy,
                    "core_inventory_user_total": 0,
                    "core_inventory_user_healthy": 0,
                },
            )
            return (
                stats["auto_pool"]["current_healthy"],
                stats["auto_pool"]["current_total"],
            )

        self.assertEqual(ratio(auto_nodes), (3, 3))
        self.assertEqual(ratio(auto_nodes, {"auto-a"}), (2, 3))

        # A replacement can restore traffic, but it does not restore the bad
        # member's numerator.  Only that member's own self-check can do so.
        self.assertEqual(ratio(auto_nodes, {"auto-a"}), (2, 3))
        self.assertEqual(ratio(auto_nodes), (3, 3))

        suspicious_nodes = [item.clone() for item in auto_nodes]
        suspicious_nodes[0].status = NodeStatus.SUSPICIOUS
        self.assertEqual(ratio(suspicious_nodes), (2, 3))
        self.assertEqual(ratio(suspicious_nodes[1:]), (2, 2))

    def test_effective_nodes_helper_falls_back_when_manager_api_is_absent(self) -> None:
        nodes = [node("auto-a", extra={"tier3_origin": "tier3b"})]

        self.assertEqual(tier3_effective_nodes_for_display(nodes, object()), nodes)

    def test_tier3_stats_snapshot_keeps_full_validation_slow_pool_as_main_pool(self) -> None:
        nodes = [node(f"slow-{index}", status=NodeStatus.SLOW) for index in range(15)]

        stats = tier3_stats_snapshot(
            enabled=True,
            nodes=nodes,
            report={
                "validation": {
                    "prelaunch_validation": "full",
                    "candidate_count": 15,
                    "tested_count": 15,
                },
                "startup_core_repair": {
                    "working_count": 0,
                    "pruned_count": 0,
                },
            },
            tier3_source_count=99,
            background_target_alive=25,
            next_collect_due_seconds=0,
            level=2,
            level_reason="startup",
            controller={},
            revival_count=0,
        )

        self.assertEqual(stats["raw_total"], 15)
        self.assertEqual(stats["total"], 15)
        self.assertEqual(stats["slow"], 15)
        self.assertEqual(stats["healthy"], 15)
        self.assertEqual(stats["collection"]["candidate_count"], 15)
        self.assertEqual(stats["collection"]["core_usable_count"], 0)

    def test_tier3_stats_snapshot_zeroes_counts_when_disabled(self) -> None:
        stats = tier3_stats_snapshot(
            enabled=False,
            nodes=[node("alive")],
            report={},
            tier3_source_count=1,
            background_target_alive=10,
            next_collect_due_seconds=0,
            level=2,
            level_reason="disabled",
            controller={},
            revival_count=0,
        )

        self.assertFalse(stats["enabled"])
        self.assertEqual(stats["total"], 0)
        self.assertEqual(stats["raw_total"], 0)
        self.assertEqual(stats["level"], 0)

    def test_tier3_pool_chart_stats_use_display_pool_counts_for_normal_pool(self) -> None:
        self.assertEqual(
            tier3_pool_chart_stats({"alive": 2, "slow": 1, "total": 5}),
            {"total": 5, "healthy": 3},
        )

    def test_tier3_pool_chart_stats_use_current_pool_counts_during_background_expansion(self) -> None:
        self.assertEqual(
            tier3_pool_chart_stats(
                {
                    "alive": 3,
                    "slow": 0,
                    "total": 3,
                    "collection": {
                        "background_expansion_due": True,
                        "background_target_alive_count": 25,
                    },
                }
            ),
            {"total": 3, "healthy": 3},
        )

    def test_tier3_pool_chart_stats_keeps_failed_pool_members_until_evicted(self) -> None:
        self.assertEqual(
            tier3_pool_chart_stats(
                {
                    "alive": 8,
                    "slow": 0,
                    "total": 12,
                    "collection": {
                        "background_expansion_due": True,
                        "background_target_alive_count": 25,
                    },
                }
            ),
            {"total": 12, "healthy": 8},
        )

    def test_tier3_pool_chart_stats_keep_startup_denominator_during_startup_expansion(self) -> None:
        self.assertEqual(
            tier3_pool_chart_stats(
                {
                    "alive": 3,
                    "slow": 0,
                    "total": 3,
                    "collection": {
                        "startup_mode": True,
                        "background_expansion_due": True,
                        "background_target_alive_count": 25,
                    },
                }
            ),
            {"total": 3, "healthy": 3},
        )

    def test_tier3_pool_chart_stats_hide_background_target_after_startup_core_failure(self) -> None:
        self.assertEqual(
            tier3_pool_chart_stats(
                {
                    "alive": 1,
                    "slow": 0,
                    "total": 1,
                    "collection": {
                        "background_expansion_due": True,
                        "background_target_alive_count": 25,
                        "startup_core_verification_failed": {"reason": "only 2/3"},
                    },
                }
            ),
            {"total": 1, "healthy": 1},
        )

    def test_tier3_pool_chart_stats_do_not_let_core_repair_counts_override_main_pool(self) -> None:
        self.assertEqual(
            tier3_pool_chart_stats(
                {
                    "alive": 10,
                    "slow": 0,
                    "total": 20,
                    "collection": {
                        "core_usable_count": 2,
                        "core_verified_count": 3,
                        "core_working_count": 3,
                    },
                }
            ),
            {"total": 20, "healthy": 10},
        )

    def test_tier3_pool_chart_stats_do_not_let_tcp_candidate_count_override_main_pool(self) -> None:
        self.assertEqual(
            tier3_pool_chart_stats(
                {
                    "alive": 20,
                    "slow": 0,
                    "total": 20,
                    "collection": {"tcp_candidate_count": 20},
                }
            ),
            {"total": 20, "healthy": 20},
        )

    def test_tier3_pool_chart_stats_tolerate_invalid_snapshot_counts(self) -> None:
        self.assertEqual(
            tier3_pool_chart_stats(
                {
                    "alive": "invalid",
                    "slow": None,
                    "total": "invalid",
                    "collection": {"core_usable_count": "invalid"},
                }
            ),
            {"total": 0, "healthy": 0},
        )

    def test_tier3_pool_chart_stats_omits_explicitly_unverified_inventory(self) -> None:
        self.assertEqual(
            tier3_pool_chart_stats(
                {
                    "core_inventory_verified": False,
                    "healthy": 15,
                    "total": 16,
                    "auto_pool": {"healthy": 15, "total": 15},
                }
            ),
            {},
        )


if __name__ == "__main__":
    unittest.main()
