import unittest

from models import Node, NodeStatus, count_healthy_nodes
from tier3_pool_retention import (
    Tier3PartialRefreshBuffer,
    clear_tier3_cache_revalidation_markers,
    clear_tier3_retention_markers,
    clear_tier3_startup_markers,
    clear_tier3_startup_markers_for_nodes,
    retained_previous_candidate_count,
    should_buffer_partial_refresh,
    should_attempt_tier3_revival,
    merge_tier3_revived_nodes,
    tier3_active_pool_nodes,
    tier3_partial_refresh_buffer,
    tier3_pool_nodes_after_validation,
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


def identity(value: Node) -> str:
    return value.name


def dedupe(nodes: list[Node]) -> list[Node]:
    seen: set[str] = set()
    result: list[Node] = []
    for item in nodes:
        if item.name in seen:
            continue
        seen.add(item.name)
        result.append(item)
    return result


class Tier3PoolRetentionTests(unittest.TestCase):
    def test_active_pool_nodes_keeps_alive_and_slow_only(self) -> None:
        alive = node("alive", status=NodeStatus.ALIVE)
        slow = node("slow", status=NodeStatus.SLOW)
        dead = node("dead", status=NodeStatus.DEAD)

        self.assertEqual(tier3_active_pool_nodes([alive, dead, slow]), [alive, slow])

    def test_clear_retention_markers_removes_only_retention_metadata(self) -> None:
        current = node(
            "current",
            extra={
                "tier3_consecutive_failures": 2,
                "tier3_last_failure_reason": "timeout",
                "tier3_last_failure_at": "old",
                "tier3_retained_after_failure": True,
                "tier3_retained_unchecked": True,
                "source_origin": "tier3a",
            },
        )

        clear_tier3_retention_markers(current)

        self.assertEqual(current.extra, {"source_origin": "tier3a"})

    def test_clear_startup_markers_removes_only_startup_probe_metadata(self) -> None:
        current = node(
            "current",
            extra={
                "startup_prelaunch_validation": "tcp",
                "startup_export_rank": 1,
                "startup_core_probe_url": "https://cp.cloudflare.com",
                "startup_core_delay_ms": 100,
                "source_origin": "tier3a",
            },
        )

        clear_tier3_startup_markers(current)

        self.assertEqual(current.extra, {"source_origin": "tier3a"})

    def test_clear_startup_markers_for_nodes_mutates_and_returns_same_list(self) -> None:
        nodes = [
            node("first", extra={"startup_core_delay_ms": 10}),
            node("second", extra={"startup_export_rank": 2, "other": "keep"}),
        ]

        result = clear_tier3_startup_markers_for_nodes(nodes)

        self.assertIs(result, nodes)
        self.assertEqual(nodes[0].extra, {})
        self.assertEqual(nodes[1].extra, {"other": "keep"})

    def test_clear_cache_revalidation_markers_removes_pending_only(self) -> None:
        current = node(
            "current",
            extra={
                "tier3_cache_revalidation_pending": True,
                "source_origin": "tier3a",
            },
        )

        clear_tier3_cache_revalidation_markers(current)

        self.assertEqual(current.extra, {"source_origin": "tier3a"})

    def test_pool_after_validation_grows_gradually_from_validated_nodes(self) -> None:
        pool: list[Node] = []
        expected_sizes = [1, 2, 3, 5]

        for size in expected_sizes:
            tested = [node(f"node-{index}") for index in range(size)]
            pool = tier3_pool_nodes_after_validation(
                tested,
                pool,
                threshold=3,
                identity_fn=identity,
                dedupe_fn=dedupe,
                now_iso_fn=lambda: "unused",
            )
            self.assertEqual(len(pool), size)
            self.assertEqual(count_healthy_nodes(pool), size)

    def test_pool_after_validation_retains_failed_members_in_main_denominator(self) -> None:
        previous_five = [node(f"node-{index}") for index in range(5)]
        tested_five = [node(f"node-{index}") for index in range(4)]
        tested_five.append(node("node-4", status=NodeStatus.SUSPICIOUS))

        five_result = tier3_pool_nodes_after_validation(
            tested_five,
            previous_five,
            threshold=3,
            identity_fn=identity,
            dedupe_fn=dedupe,
            now_iso_fn=lambda: "2026-05-31T00:00:00Z",
        )

        self.assertEqual(len(five_result), 5)
        self.assertEqual(count_healthy_nodes(five_result), 4)
        self.assertTrue(five_result[-1].extra["tier3_retained_after_failure"])

        previous_seven = [node(f"node-{index}") for index in range(7)]
        tested_seven = [node(f"node-{index}") for index in range(6)]
        tested_seven.append(node("node-6", status=NodeStatus.DEAD))

        seven_result = tier3_pool_nodes_after_validation(
            tested_seven,
            previous_seven,
            threshold=3,
            identity_fn=identity,
            dedupe_fn=dedupe,
            now_iso_fn=lambda: "2026-05-31T00:00:00Z",
        )

        self.assertEqual(len(seven_result), 7)
        self.assertEqual(count_healthy_nodes(seven_result), 6)

    def test_pool_after_validation_retains_failed_previous_until_threshold(self) -> None:
        previous = node("same", extra={"tier3_consecutive_failures": 1})
        tested = node("same", status=NodeStatus.SUSPICIOUS)
        tested.suspicious_reason = "mihomo_validation_failed: timed out"

        result = tier3_pool_nodes_after_validation(
            [tested],
            [previous],
            threshold=3,
            identity_fn=identity,
            dedupe_fn=dedupe,
            now_iso_fn=lambda: "2026-05-31T00:00:00Z",
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "same")
        self.assertEqual(result[0].status, NodeStatus.SUSPICIOUS)
        self.assertEqual(result[0].extra["tier3_consecutive_failures"], 2)
        self.assertEqual(result[0].extra["tier3_last_failure_reason"], "mihomo_validation_failed: timed out")
        self.assertEqual(result[0].extra["tier3_last_failure_at"], "2026-05-31T00:00:00Z")
        self.assertTrue(result[0].extra["tier3_retained_after_failure"])

    def test_pool_after_validation_only_admits_pending_revalidation_after_success(self) -> None:
        accepted = node("accepted", extra={"tier3_cache_revalidation_pending": True})
        previous = node("previous", extra={"tier3_cache_revalidation_pending": True})
        failed_previous = node("failed", extra={"tier3_cache_revalidation_pending": True})
        failed = node("failed", status=NodeStatus.SUSPICIOUS, extra={"tier3_cache_revalidation_pending": True})

        result = tier3_pool_nodes_after_validation(
            [accepted, failed],
            [previous, failed_previous],
            threshold=3,
            identity_fn=identity,
            dedupe_fn=dedupe,
            now_iso_fn=lambda: "2026-05-31T00:00:00Z",
        )

        self.assertEqual([item.name for item in result], ["accepted"])
        self.assertTrue(all("tier3_cache_revalidation_pending" not in item.extra for item in result))

    def test_pool_after_validation_drops_failed_previous_at_threshold(self) -> None:
        previous = node("same", extra={"tier3_consecutive_failures": 2})
        tested = node("same", status=NodeStatus.DEAD)

        self.assertEqual(
            tier3_pool_nodes_after_validation(
                [tested],
                [previous],
                threshold=3,
                identity_fn=identity,
                dedupe_fn=dedupe,
                now_iso_fn=lambda: "unused",
            ),
            [],
        )

    def test_pool_after_validation_retains_unchecked_previous_candidates(self) -> None:
        previous = node("previous")
        tested = node("new", status=NodeStatus.ALIVE)

        result = tier3_pool_nodes_after_validation(
            [tested],
            [previous],
            threshold=3,
            identity_fn=identity,
            dedupe_fn=dedupe,
            now_iso_fn=lambda: "unused",
        )

        self.assertEqual([item.name for item in result], ["new", "previous"])
        self.assertTrue(result[1].extra["tier3_retained_unchecked"])

    def test_retention_count_and_partial_buffer_policy_are_explicit(self) -> None:
        retained_after_failure = node("a", extra={"tier3_retained_after_failure": True})
        retained_unchecked = node("b", extra={"tier3_retained_unchecked": True})
        plain = node("c")

        self.assertEqual(retained_previous_candidate_count([retained_after_failure, retained_unchecked, plain]), 2)
        self.assertTrue(
            should_buffer_partial_refresh(
                startup_mode=False,
                previous_alive_count=3,
                alive_count=1,
                target_alive=10,
            )
        )
        self.assertTrue(
            should_buffer_partial_refresh(
                startup_mode=True,
                previous_alive_count=3,
                alive_count=1,
                target_alive=10,
            )
        )
        self.assertFalse(
            should_buffer_partial_refresh(
                startup_mode=False,
                previous_alive_count=3,
                alive_count=3,
                target_alive=10,
            )
        )

    def test_partial_refresh_buffer_does_not_special_case_startup(self) -> None:
        current = [node("current")]
        previous = [node("previous")]

        result = tier3_partial_refresh_buffer(
            current,
            previous,
            startup_mode=True,
            previous_alive_count=3,
            alive_count=1,
            target_alive=10,
            max_pool_size=10,
            sort_fn=lambda values: values,
            dedupe_fn=dedupe,
        )

        self.assertEqual(
            result,
            Tier3PartialRefreshBuffer(
                buffered=True,
                nodes=[*current, *previous],
                retained_previous_count=0,
                log_retained_count=1,
            ),
        )

    def test_partial_refresh_buffer_merges_previous_nodes_when_refresh_drops_below_target(self) -> None:
        current = [node("current")]
        previous = [node("previous-a"), node("previous-b")]

        result = tier3_partial_refresh_buffer(
            current,
            previous,
            startup_mode=False,
            previous_alive_count=3,
            alive_count=1,
            target_alive=10,
            max_pool_size=10,
            sort_fn=lambda values: values,
            dedupe_fn=dedupe,
        )

        self.assertTrue(result.buffered)
        self.assertEqual([item.name for item in result.nodes], ["current", "previous-a", "previous-b"])
        self.assertEqual(result.retained_previous_count, 0)
        self.assertEqual(result.log_retained_count, 2)

    def test_partial_refresh_buffer_uses_retained_marker_count_for_log_count(self) -> None:
        current = [node("current", extra={"tier3_retained_after_failure": True})]
        previous = [node("current")]

        result = tier3_partial_refresh_buffer(
            current,
            previous,
            startup_mode=False,
            previous_alive_count=3,
            alive_count=1,
            target_alive=10,
            max_pool_size=10,
            sort_fn=lambda values: values,
            dedupe_fn=dedupe,
        )

        self.assertTrue(result.buffered)
        self.assertEqual([item.name for item in result.nodes], ["current"])
        self.assertEqual(result.retained_previous_count, 1)
        self.assertEqual(result.log_retained_count, 1)

    def test_revival_attempt_is_not_gated_by_target_deficit(self) -> None:
        self.assertTrue(
            should_attempt_tier3_revival(
                alive_count=1,
                target_alive=3,
                source_count=1,
            )
        )
        self.assertTrue(
            should_attempt_tier3_revival(
                alive_count=3,
                target_alive=3,
                source_count=1,
            )
        )
        self.assertTrue(
            should_attempt_tier3_revival(
                alive_count=1,
                target_alive=3,
                source_count=0,
            )
        )

    def test_merge_revived_nodes_filters_to_active_pool_for_background_refresh(self) -> None:
        current = [node("current", status=NodeStatus.ALIVE)]
        revived = [
            node("revived-good", status=NodeStatus.ALIVE),
            node("revived-dead", status=NodeStatus.DEAD),
        ]

        result = merge_tier3_revived_nodes(
            current,
            revived,
            startup_mode=False,
            max_pool_size=10,
            active_pool_fn=tier3_active_pool_nodes,
            sort_fn=lambda values: values,
            dedupe_fn=dedupe,
        )

        self.assertEqual([item.name for item in result], ["current", "revived-good"])

    def test_merge_revived_nodes_never_admits_tcp_only_startup_candidates(self) -> None:
        current = [node("current", status=NodeStatus.ALIVE)]
        revived = [node("tcp-candidate", status=NodeStatus.DEAD, extra={"startup_prelaunch_validation": "tcp"})]

        result = merge_tier3_revived_nodes(
            current,
            revived,
            startup_mode=True,
            max_pool_size=10,
            active_pool_fn=tier3_active_pool_nodes,
            sort_fn=lambda values: values,
            dedupe_fn=dedupe,
        )

        self.assertEqual([item.name for item in result], ["current"])

    def test_merge_revived_nodes_applies_dedupe_sort_and_pool_limit(self) -> None:
        current = [node("same", status=NodeStatus.ALIVE), node("keep", status=NodeStatus.ALIVE)]
        revived = [node("same", status=NodeStatus.ALIVE), node("new", status=NodeStatus.ALIVE)]

        result = merge_tier3_revived_nodes(
            current,
            revived,
            startup_mode=False,
            max_pool_size=2,
            active_pool_fn=tier3_active_pool_nodes,
            sort_fn=lambda values: list(reversed(values)),
            dedupe_fn=dedupe,
        )

        self.assertEqual([item.name for item in result], ["new", "keep"])


if __name__ == "__main__":
    unittest.main()
