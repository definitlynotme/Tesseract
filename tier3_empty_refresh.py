from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tier3EmptyRefreshState:
    streak: int
    replace_after: int
    suppressed: bool


@dataclass(frozen=True)
class Tier3EmptyRefreshDecision:
    handled: bool
    next_streak: int
    replace_after: int
    suppressed: bool
    replace_pool: bool


def should_handle_tier3_empty_refresh(
    *,
    startup_mode: bool,
    alive_count: int,
    previous_alive_count: int,
) -> bool:
    return bool(alive_count == 0 and previous_alive_count > 0)


def next_tier3_empty_refresh_state(
    *,
    current_streak: int,
    replace_after: int,
) -> Tier3EmptyRefreshState:
    next_streak = current_streak + 1
    return Tier3EmptyRefreshState(
        streak=next_streak,
        replace_after=replace_after,
        suppressed=next_streak < replace_after,
    )


def tier3_empty_refresh_decision(
    *,
    startup_mode: bool,
    alive_count: int,
    previous_alive_count: int,
    current_streak: int,
    replace_after: int,
) -> Tier3EmptyRefreshDecision:
    if should_handle_tier3_empty_refresh(
        startup_mode=startup_mode,
        alive_count=alive_count,
        previous_alive_count=previous_alive_count,
    ):
        state = next_tier3_empty_refresh_state(
            current_streak=current_streak,
            replace_after=replace_after,
        )
        return Tier3EmptyRefreshDecision(
            handled=True,
            next_streak=state.streak,
            replace_after=state.replace_after,
            suppressed=state.suppressed,
            replace_pool=not state.suppressed,
        )
    return Tier3EmptyRefreshDecision(
        handled=False,
        next_streak=0 if alive_count > 0 else current_streak,
        replace_after=replace_after,
        suppressed=False,
        replace_pool=False,
    )
