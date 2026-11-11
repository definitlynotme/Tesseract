import unittest

from tier3_empty_refresh import (
    Tier3EmptyRefreshDecision,
    Tier3EmptyRefreshState,
    next_tier3_empty_refresh_state,
    should_handle_tier3_empty_refresh,
    tier3_empty_refresh_decision,
)


class Tier3EmptyRefreshTests(unittest.TestCase):
    def test_empty_refresh_policy_is_identical_for_startup_and_background(self) -> None:
        self.assertTrue(
            should_handle_tier3_empty_refresh(
                startup_mode=False,
                alive_count=0,
                previous_alive_count=2,
            )
        )
        self.assertTrue(
            should_handle_tier3_empty_refresh(
                startup_mode=True,
                alive_count=0,
                previous_alive_count=2,
            )
        )
        self.assertFalse(
            should_handle_tier3_empty_refresh(
                startup_mode=False,
                alive_count=1,
                previous_alive_count=2,
            )
        )
        self.assertFalse(
            should_handle_tier3_empty_refresh(
                startup_mode=False,
                alive_count=0,
                previous_alive_count=0,
            )
        )

    def test_next_empty_refresh_state_suppresses_until_replace_threshold(self) -> None:
        self.assertEqual(
            next_tier3_empty_refresh_state(
                current_streak=0,
                replace_after=3,
            ),
            Tier3EmptyRefreshState(streak=1, replace_after=3, suppressed=True),
        )
        self.assertEqual(
            next_tier3_empty_refresh_state(
                current_streak=2,
                replace_after=3,
            ),
            Tier3EmptyRefreshState(streak=3, replace_after=3, suppressed=False),
        )

    def test_empty_refresh_decision_suppresses_before_replace_threshold(self) -> None:
        self.assertEqual(
            tier3_empty_refresh_decision(
                startup_mode=False,
                alive_count=0,
                previous_alive_count=2,
                current_streak=1,
                replace_after=3,
            ),
            Tier3EmptyRefreshDecision(
                handled=True,
                next_streak=2,
                replace_after=3,
                suppressed=True,
                replace_pool=False,
            ),
        )

    def test_empty_refresh_decision_replaces_pool_at_threshold(self) -> None:
        self.assertEqual(
            tier3_empty_refresh_decision(
                startup_mode=False,
                alive_count=0,
                previous_alive_count=2,
                current_streak=2,
                replace_after=3,
            ),
            Tier3EmptyRefreshDecision(
                handled=True,
                next_streak=3,
                replace_after=3,
                suppressed=False,
                replace_pool=True,
            ),
        )

    def test_empty_refresh_decision_does_not_special_case_startup(self) -> None:
        self.assertEqual(
            tier3_empty_refresh_decision(
                startup_mode=True,
                alive_count=0,
                previous_alive_count=2,
                current_streak=2,
                replace_after=3,
            ),
            Tier3EmptyRefreshDecision(
                handled=True,
                next_streak=3,
                replace_after=3,
                suppressed=False,
                replace_pool=True,
            ),
        )

    def test_empty_refresh_decision_resets_streak_after_recovery(self) -> None:
        self.assertEqual(
            tier3_empty_refresh_decision(
                startup_mode=False,
                alive_count=1,
                previous_alive_count=2,
                current_streak=2,
                replace_after=3,
            ),
            Tier3EmptyRefreshDecision(
                handled=False,
                next_streak=0,
                replace_after=3,
                suppressed=False,
                replace_pool=False,
            ),
        )


if __name__ == "__main__":
    unittest.main()
