import unittest

from tier3_level_policy import Tier3LevelDecision, tier3_level_after_refresh


class Tier3LevelPolicyTests(unittest.TestCase):
    def test_refresh_with_alive_count_at_minimum_is_steady_even_below_background_target(self) -> None:
        self.assertEqual(
            tier3_level_after_refresh(
                alive_count=3,
                target_alive=10,
                min_alive=3,
                has_candidates=True,
                source_count=1,
                node_count=5,
                any_failure=False,
            ),
            Tier3LevelDecision(0, "steady"),
        )

    def test_refresh_below_minimum_reports_below_target(self) -> None:
        self.assertEqual(
            tier3_level_after_refresh(
                alive_count=1,
                target_alive=3,
                min_alive=3,
                has_candidates=True,
                source_count=1,
                node_count=5,
                any_failure=False,
            ),
            Tier3LevelDecision(2, "below target alive count"),
        )

    def test_refresh_with_candidates_but_no_alive_nodes_fails_validation(self) -> None:
        self.assertEqual(
            tier3_level_after_refresh(
                alive_count=0,
                target_alive=3,
                min_alive=3,
                has_candidates=True,
                source_count=1,
                node_count=5,
                any_failure=False,
            ),
            Tier3LevelDecision(3, "validation returned empty"),
        )

    def test_empty_source_collection_after_revival_attempt_reports_cache_revival_empty(self) -> None:
        self.assertEqual(
            tier3_level_after_refresh(
                alive_count=0,
                target_alive=3,
                min_alive=3,
                has_candidates=False,
                source_count=1,
                node_count=0,
                any_failure=True,
            ),
            Tier3LevelDecision(3, "cache revival returned empty"),
        )

    def test_failures_without_candidate_shortage_report_source_failure(self) -> None:
        self.assertEqual(
            tier3_level_after_refresh(
                alive_count=3,
                target_alive=3,
                min_alive=3,
                has_candidates=False,
                source_count=1,
                node_count=0,
                any_failure=True,
            ),
            Tier3LevelDecision(1, "one or more sources failed"),
        )

    def test_no_problem_reports_steady(self) -> None:
        self.assertEqual(
            tier3_level_after_refresh(
                alive_count=0,
                target_alive=3,
                min_alive=3,
                has_candidates=False,
                source_count=0,
                node_count=0,
                any_failure=False,
            ),
            Tier3LevelDecision(0, "steady"),
        )


if __name__ == "__main__":
    unittest.main()
