from __future__ import annotations

from typing import Any


def empty_background_action_state() -> dict[str, Any]:
    return {
        "running": False,
        "last_started_at": None,
        "last_completed_at": None,
        "last_error": None,
        "last_result": None,
    }


def start_background_action_state(
    state: dict[str, Any],
    *,
    started_at: str,
) -> tuple[bool, dict[str, Any]]:
    if state.get("running"):
        return False, dict(state)

    next_state = dict(state)
    next_state["running"] = True
    next_state["last_started_at"] = started_at
    next_state["last_error"] = None
    next_state["last_result"] = None
    return True, next_state


def finish_background_action_state(
    state: dict[str, Any],
    *,
    completed_at: str,
    error: str | None,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    next_state = dict(state)
    next_state["running"] = False
    next_state["last_completed_at"] = completed_at
    next_state["last_error"] = error
    next_state["last_result"] = dict(result) if isinstance(result, dict) else None
    return next_state
