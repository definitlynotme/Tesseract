from __future__ import annotations

from typing import Any, Iterable


def _coerce_score_bonus(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def mark_tier3_static_nodes(nodes: Iterable[Any], *, minimum_score_bonus: float = 0.15) -> list[Any]:
    marked_nodes = list(nodes)
    for node in marked_nodes:
        extra = getattr(node, "extra", None)
        if not isinstance(extra, dict):
            extra = {}
            setattr(node, "extra", extra)
        extra["tier3_origin"] = "tier3a"
        extra["tier3_score_bonus"] = max(
            _coerce_score_bonus(extra.get("tier3_score_bonus", 0), 0.0),
            minimum_score_bonus,
        )
    return marked_nodes
