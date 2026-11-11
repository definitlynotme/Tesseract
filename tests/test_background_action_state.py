import unittest

from background_action_state import (
    empty_background_action_state,
    finish_background_action_state,
    start_background_action_state,
)


class BackgroundActionStateTests(unittest.TestCase):
    def test_empty_state_uses_runtime_snapshot_shape(self) -> None:
        self.assertEqual(
            empty_background_action_state(),
            {
                "running": False,
                "last_started_at": None,
                "last_completed_at": None,
                "last_error": None,
                "last_result": None,
            },
        )

    def test_start_marks_idle_action_running_and_clears_error(self) -> None:
        state = {
            "running": False,
            "last_started_at": "old-start",
            "last_completed_at": "old-end",
            "last_error": "old-error",
            "last_result": {"old": True},
            "extra": "kept",
        }

        accepted, next_state = start_background_action_state(state, started_at="new-start")

        self.assertTrue(accepted)
        self.assertEqual(
            next_state,
            {
                "running": True,
                "last_started_at": "new-start",
                "last_completed_at": "old-end",
                "last_error": None,
                "last_result": None,
                "extra": "kept",
            },
        )
        self.assertFalse(state["running"])
        self.assertEqual(state["last_error"], "old-error")

    def test_start_rejects_running_action_without_changing_state(self) -> None:
        state = {
            "running": True,
            "last_started_at": "current-start",
            "last_completed_at": "old-end",
            "last_error": "old-error",
            "last_result": {"old": True},
        }

        accepted, next_state = start_background_action_state(state, started_at="ignored")

        self.assertFalse(accepted)
        self.assertEqual(next_state, state)
        self.assertIsNot(next_state, state)

    def test_finish_records_completion_and_error(self) -> None:
        state = {
            "running": True,
            "last_started_at": "start",
            "last_completed_at": None,
            "last_error": None,
            "last_result": None,
        }

        next_state = finish_background_action_state(
            state,
            completed_at="end",
            error="boom",
            result={"healthy": 1},
        )

        self.assertEqual(
            next_state,
            {
                "running": False,
                "last_started_at": "start",
                "last_completed_at": "end",
                "last_error": "boom",
                "last_result": {"healthy": 1},
            },
        )
        self.assertTrue(state["running"])


if __name__ == "__main__":
    unittest.main()
