import unittest

from models import Node, NodeStatus
from tier3_validated_pool import normalize_tier3_validated_pool


def node(name: str, *, status: NodeStatus = NodeStatus.ALIVE, extra: dict | None = None) -> Node:
    return Node(
        name=name,
        protocol="ss",
        uri=f"ss://{name}",
        address=f"{name}.example.com",
        port=8388,
        tier="tier3",
        status=status,
        extra=extra or {},
    )


class Tier3ValidatedPoolTests(unittest.TestCase):
    def test_startup_full_validation_keeps_only_active_pool_nodes(self) -> None:
        failed = node("failed", status=NodeStatus.SUSPICIOUS)
        alive = node("alive", status=NodeStatus.ALIVE)
        slow = node("slow", status=NodeStatus.SLOW)

        outcome = normalize_tier3_validated_pool(
            [failed, alive, slow],
            {"prelaunch_validation": "full"},
            [],
            startup_mode=True,
            max_pool_size=10,
            pool_nodes_after_validation_fn=lambda current, _previous: [
                item for item in current if item.status in {NodeStatus.ALIVE, NodeStatus.SLOW}
            ],
            sort_fn=lambda values: values,
        )

        self.assertEqual([item.name for item in outcome.nodes], ["alive", "slow"])
        self.assertEqual(outcome.alive_count, 2)
        self.assertEqual(outcome.validation_report["tested_unusable_count"], 1)
        self.assertEqual(outcome.observed_nodes, [failed, alive, slow])

    def test_startup_full_validation_clears_pending_revalidation_markers(self) -> None:
        alive = node(
            "alive",
            status=NodeStatus.ALIVE,
            extra={
                "tier3_cache_revalidation_pending": True,
                "startup_prelaunch_validation": "full",
                "startup_core_delay_ms": 42,
                "source_origin": "tier3a",
            },
        )
        slow = node(
            "slow",
            status=NodeStatus.SLOW,
            extra={
                "tier3_cache_revalidation_pending": True,
                "startup_export_rank": 3,
            },
        )

        outcome = normalize_tier3_validated_pool(
            [alive, slow],
            {"prelaunch_validation": "full"},
            [],
            startup_mode=True,
            max_pool_size=10,
            pool_nodes_after_validation_fn=lambda current, _previous: current,
            sort_fn=lambda values: values,
        )

        self.assertEqual(outcome.nodes, [alive, slow])
        self.assertEqual(alive.extra, {"source_origin": "tier3a"})
        self.assertEqual(slow.extra, {})

    def test_legacy_tcp_prevalidation_marker_is_cleared_and_never_special_cased(self) -> None:
        candidate = node(
            "tcp-candidate",
            status=NodeStatus.DEAD,
            extra={"startup_prelaunch_validation": "tcp"},
        )

        outcome = normalize_tier3_validated_pool(
            [candidate],
            {"prelaunch_validation": "tcp"},
            [],
            startup_mode=True,
            max_pool_size=10,
            pool_nodes_after_validation_fn=lambda current, _previous: current,
            sort_fn=lambda values: values,
        )

        self.assertEqual(outcome.nodes, [candidate])
        self.assertEqual(outcome.alive_count, 0)
        self.assertEqual(outcome.validation_report["tested_unusable_count"], 1)
        self.assertNotIn("startup_prelaunch_validation", candidate.extra)

    def test_background_refresh_clears_startup_markers_before_retention_policy(self) -> None:
        current = node("current", extra={"startup_core_delay_ms": 42, "source_origin": "tier3a"})
        previous = node("previous")
        seen_current: list[Node] = []
        seen_previous: list[Node] = []

        def retain(current_nodes: list[Node], previous_nodes: list[Node]) -> list[Node]:
            seen_current.extend(current_nodes)
            seen_previous.extend(previous_nodes)
            return [*current_nodes, *previous_nodes]

        outcome = normalize_tier3_validated_pool(
            [current],
            {"prelaunch_validation": "full"},
            [previous],
            startup_mode=False,
            max_pool_size=10,
            pool_nodes_after_validation_fn=retain,
            sort_fn=lambda values: values,
        )

        self.assertEqual(outcome.nodes, [current, previous])
        self.assertEqual(seen_current, [current])
        self.assertEqual(seen_previous, [previous])
        self.assertEqual(current.extra, {"source_origin": "tier3a"})

    def test_max_pool_size_is_applied_after_sorting(self) -> None:
        first = node("b")
        second = node("a")

        outcome = normalize_tier3_validated_pool(
            [first, second],
            {"prelaunch_validation": "full"},
            [],
            startup_mode=True,
            max_pool_size=1,
            pool_nodes_after_validation_fn=lambda current, _previous: current,
            sort_fn=lambda values: sorted(values, key=lambda item: item.name),
        )

        self.assertEqual([item.name for item in outcome.nodes], ["a"])


if __name__ == "__main__":
    unittest.main()
