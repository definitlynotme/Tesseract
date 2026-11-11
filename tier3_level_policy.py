from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tier3LevelDecision:
    level: int
    reason: str


def tier3_level_after_refresh(
    *,
    alive_count: int,
    target_alive: int,
    min_alive: int,
    has_candidates: bool,
    source_count: int,
    node_count: int,
    any_failure: bool,
) -> Tier3LevelDecision:
    if alive_count < target_alive and source_count:
        if alive_count >= min_alive:
            return Tier3LevelDecision(0, "steady")
        if alive_count > 0:
            return Tier3LevelDecision(2, "below target alive count")
        if node_count > 0:
            return Tier3LevelDecision(3, "validation returned empty")
        return Tier3LevelDecision(3, "cache revival returned empty")
    if has_candidates and alive_count == 0:
        return Tier3LevelDecision(3, "validation returned empty")
    if has_candidates and alive_count < min_alive:
        return Tier3LevelDecision(2, "below target alive count")
    if any_failure:
        return Tier3LevelDecision(1, "one or more sources failed")
    return Tier3LevelDecision(0, "steady")
