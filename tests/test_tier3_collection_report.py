import unittest
from types import SimpleNamespace

from tier3_collection_report import (
    build_tier3_collection_report,
    build_tier3_refresh_collection_report,
    merge_collection_reports,
    tier3_controller_decision_report,
    tier3_source_stream_outcome_counts,
    with_empty_refresh_state,
    with_partial_refresh_buffering,
)
from tier3_sources import CollectionReport


class Tier3CollectionReportTests(unittest.TestCase):
    def test_merge_collection_reports_sums_counts_and_preserves_source_order(self) -> None:
        merged = merge_collection_reports(
            [
                CollectionReport(
                    source_count=2,
                    configured_source_count=2,
                    fetched_sources=1,
                    failed_sources=1,
                    node_count=3,
                    sources=[{"source": "a"}],
                ),
                CollectionReport(
                    source_count=1,
                    configured_source_count=1,
                    fetched_sources=1,
                    failed_sources=0,
                    node_count=4,
                    sources=[{"source": "b"}, {"source": "c"}],
                ),
            ]
        )

        self.assertEqual(merged.source_count, 3)
        self.assertEqual(merged.configured_source_count, 3)
        self.assertEqual(merged.fetched_sources, 2)
        self.assertEqual(merged.failed_sources, 1)
        self.assertEqual(merged.node_count, 7)
        self.assertEqual(merged.sources, [{"source": "a"}, {"source": "b"}, {"source": "c"}])

    def test_merge_collection_reports_handles_empty_input(self) -> None:
        self.assertEqual(merge_collection_reports([]), CollectionReport())

    def test_source_stream_outcomes_split_hard_failures_from_incomplete_work(self) -> None:
        counts = tier3_source_stream_outcome_counts(
            {
                "source_stream_failed_count": 147,
                "source_stream_tcp_failed_count": 51,
                "source_stream_tcp_deferred_count": 96,
                "source_stream_tcp_cancelled_count": 2,
                "source_stream_full_validation_failed_count": 0,
                "source_stream_full_validation_deferred_count": 3,
                "source_stream_full_validation_cancelled_count": 4,
                "source_stream_unsupported_proxy_count": 5,
            }
        )

        self.assertEqual(counts["source_stream_hard_failed_count"], 51)
        self.assertEqual(counts["source_stream_failed_count"], 51)
        self.assertEqual(counts["source_stream_deferred_count"], 99)
        self.assertEqual(counts["source_stream_cancelled_count"], 6)
        self.assertEqual(counts["source_stream_unsupported_count"], 5)

    def test_source_stream_outcomes_keep_legacy_failed_only_report_readable(self) -> None:
        counts = tier3_source_stream_outcome_counts(
            {"source_stream_failed_count": 7}
        )

        self.assertEqual(counts["source_stream_hard_failed_count"], 7)
        self.assertEqual(counts["source_stream_failed_count"], 7)
        self.assertEqual(counts["source_stream_deferred_count"], 0)

    def test_collection_report_preserves_configured_width_for_retry_subset(self) -> None:
        report = CollectionReport(
            source_count=2,
            configured_source_count=5,
            fetched_sources=2,
            failed_sources=0,
            node_count=10,
        ).to_dict()

        self.assertEqual(report["source_count"], 5)
        self.assertEqual(report["configured_source_count"], 5)
        self.assertEqual(report["collection_source_count"], 2)
        self.assertEqual(report["source_fetch_pending"], 3)

    def test_collection_report_counts_pending_sources_as_retry_due_not_failed(self) -> None:
        report = CollectionReport(
            source_count=5,
            configured_source_count=5,
            fetched_sources=1,
            failed_sources=0,
            node_count=10,
            sources=[
                {"type": "subscription_url", "value": "https://ok.example/sub", "ok": True},
                {
                    "type": "subscription_url",
                    "value": "https://pending.example/sub",
                    "ok": False,
                    "pending": True,
                    "retryable": True,
                },
            ],
        ).to_dict()

        self.assertEqual(report["source_fetch_ok"], 1)
        self.assertEqual(report["source_fetch_failed"], 0)
        self.assertEqual(report["source_fetch_pending"], 4)
        self.assertEqual(report["retryable_pending_sources"], 1)
        self.assertEqual(report["source_retry_due_count"], 1)

    def test_partial_root_is_fetched_without_double_counting_pending_sibling_units(self) -> None:
        report = CollectionReport(
            source_count=5,
            configured_source_count=5,
            fetched_sources=1,
            failed_sources=0,
            discovered_source_units=2,
            fetched_source_units=1,
            pending_source_units=1,
            sources=[
                {
                    "type": "github_repository",
                    "value": "owner/repository",
                    "ok": True,
                    "pending": True,
                    "status": "partial",
                    "units": [
                        {"id": "vless", "ok": True, "pending": False},
                        {"id": "vmess", "ok": False, "pending": True},
                    ],
                }
            ],
        ).to_dict()

        self.assertEqual(report["source_fetch_ok"], 1)
        self.assertEqual(report["source_fetch_pending"], 4)
        self.assertEqual(
            report["source_fetch_ok"]
            + report["source_fetch_pending"]
            + report["source_fetch_failed"],
            5,
        )
        self.assertEqual(report["pending_source_units"], 1)

    def test_controller_decision_report_copies_public_fields(self) -> None:
        decision = SimpleNamespace(
            intensity=0.5,
            source_budget=4,
            collect_interval=120,
            error=0.25,
            integral=0.75,
        )

        self.assertEqual(
            tier3_controller_decision_report(decision),
            {
                "intensity": 0.5,
                "source_budget": 4,
                "collect_interval": 120,
                "error": 0.25,
                "integral": 0.75,
            },
        )

    def test_build_report_preserves_collection_and_runtime_fields(self) -> None:
        validation = {"tested_count": 3}
        decision = SimpleNamespace(
            intensity=1.0,
            source_budget=8,
            collect_interval=30,
            error=1.0,
            integral=1.0,
        )

        report = build_tier3_collection_report(
            CollectionReport(
                source_count=2,
                fetched_sources=1,
                failed_sources=1,
                node_count=5,
                sources=[{"value": "https://example.com/list.txt"}],
            ),
            validation,
            decision,
            startup_mode=False,
            target_alive=10,
            background_target_alive=20,
            alive_count=2,
            min_alive_nodes=3,
            intensive_expansion=True,
        )

        self.assertEqual(report["source_count"], 2)
        self.assertEqual(report["fetched_sources"], 1)
        self.assertEqual(report["failed_sources"], 1)
        self.assertEqual(report["node_count"], 5)
        self.assertIs(report["validation"], validation)
        self.assertEqual(report["controller"]["source_budget"], 8)
        self.assertFalse(report["startup_mode"])
        self.assertEqual(report["target_alive"], 10)
        self.assertEqual(report["background_target_alive"], 20)
        self.assertTrue(report["background_expansion_due"])
        self.assertTrue(report["underfilled_expansion_mode"])
        self.assertTrue(report["intensive_expansion_mode"])

    def test_startup_report_marks_background_expansion_without_underfilled_mode(self) -> None:
        decision = SimpleNamespace(
            intensity=0.1,
            source_budget=1,
            collect_interval=3600,
            error=0.0,
            integral=0.0,
        )

        report = build_tier3_collection_report(
            CollectionReport(source_count=0),
            {},
            decision,
            startup_mode=True,
            target_alive=3,
            background_target_alive=10,
            alive_count=3,
            min_alive_nodes=3,
            intensive_expansion=False,
        )

        self.assertTrue(report["startup_mode"])
        self.assertTrue(report["background_expansion_due"])
        self.assertFalse(report["underfilled_expansion_mode"])

    def test_report_marks_background_expansion_above_minimum_below_background_target(self) -> None:
        decision = SimpleNamespace(
            intensity=0.4,
            source_budget=3,
            collect_interval=900,
            error=0.5,
            integral=0.5,
        )

        report = build_tier3_collection_report(
            CollectionReport(source_count=1, fetched_sources=1, node_count=20),
            {"target_alive": 9},
            decision,
            startup_mode=False,
            target_alive=9,
            background_target_alive=9,
            alive_count=3,
            min_alive_nodes=3,
            intensive_expansion=False,
        )

        self.assertTrue(report["background_expansion_due"])
        self.assertFalse(report["underfilled_expansion_mode"])
        self.assertFalse(report["intensive_expansion_mode"])

    def test_partial_refresh_buffering_adds_counts_without_mutating_input(self) -> None:
        base = {"background_expansion_due": True}

        report = with_partial_refresh_buffering(
            base,
            candidate_alive_count=1,
            post_buffer_alive_count=2,
            previous_alive_count=3,
            retained_previous_count=4,
        )

        self.assertEqual(base, {"background_expansion_due": True})
        self.assertTrue(report["partial_refresh_buffered"])
        self.assertEqual(report["candidate_healthy_count"], 1)
        self.assertEqual(report["post_buffer_healthy_count"], 2)
        self.assertEqual(report["previous_healthy_count"], 3)
        self.assertEqual(report["retained_previous_candidate_count"], 4)

    def test_build_refresh_report_adds_partial_buffering_when_requested(self) -> None:
        decision = SimpleNamespace(
            intensity=1.0,
            source_budget=8,
            collect_interval=30,
            error=1.0,
            integral=1.0,
        )

        report = build_tier3_refresh_collection_report(
            CollectionReport(source_count=2, fetched_sources=1, failed_sources=1, node_count=5),
            {"tested_count": 3},
            decision,
            startup_mode=False,
            target_alive=10,
            background_target_alive=20,
            alive_count=2,
            min_alive_nodes=3,
            intensive_expansion=True,
            partial_refresh_buffered=True,
            candidate_alive_count=1,
            previous_alive_count=3,
            retained_previous_count=4,
        )

        self.assertEqual(report["target_alive"], 10)
        self.assertTrue(report["partial_refresh_buffered"])
        self.assertEqual(report["candidate_healthy_count"], 1)
        self.assertEqual(report["post_buffer_healthy_count"], 2)
        self.assertEqual(report["previous_healthy_count"], 3)
        self.assertEqual(report["retained_previous_candidate_count"], 4)

    def test_empty_refresh_state_records_suppression_window(self) -> None:
        base = {"background_expansion_due": False}

        suppressed = with_empty_refresh_state(
            base,
            empty_refresh_streak=1,
            replace_after=3,
            previous_alive_count=5,
        )
        replacing = with_empty_refresh_state(
            base,
            empty_refresh_streak=3,
            replace_after=3,
            previous_alive_count=5,
        )

        self.assertEqual(base, {"background_expansion_due": False})
        self.assertTrue(suppressed["empty_refresh_suppressed"])
        self.assertTrue(suppressed["background_expansion_due"])
        self.assertEqual(suppressed["empty_refresh_streak"], 1)
        self.assertEqual(suppressed["empty_refresh_replace_after_failures"], 3)
        self.assertEqual(suppressed["previous_healthy_count"], 5)
        self.assertFalse(replacing["empty_refresh_suppressed"])
        self.assertFalse(replacing["background_expansion_due"])


if __name__ == "__main__":
    unittest.main()
