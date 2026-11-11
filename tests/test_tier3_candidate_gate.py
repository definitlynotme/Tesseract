import unittest

from models import Node, NodeStatus
from tier3_candidate_gate import (
    Tier3CandidateLayeredOrder,
    Tier3CandidateGateResult,
    tier3_background_expansion_candidate_layers,
    tier3_candidate_gate_result,
    tier3_negative_cache_exempt_identities,
)


def node(name: str, *, status: NodeStatus = NodeStatus.UNKNOWN, extra: dict | None = None) -> Node:
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


class Tier3CandidateGateTests(unittest.TestCase):
    def test_negative_cache_exempt_identities_include_only_previous_nodes(self) -> None:
        previous = node("previous")
        tier3a = node("static", extra={"tier3_origin": "tier3a"})
        source_origin = node("source", extra={"source_origin": "tier3a"})
        ordinary = node("ordinary")

        self.assertEqual(
            tier3_negative_cache_exempt_identities(
                [tier3a, source_origin, ordinary],
                [previous],
                identity_fn=lambda value: value.name,
            ),
            {"previous"},
        )

    def test_candidate_gate_result_preserves_filtered_candidates_for_normal_refresh(self) -> None:
        original = [node("a"), node("b")]
        filtered = [original[0]]

        self.assertEqual(
            tier3_candidate_gate_result(
                original,
                filtered,
                negative_cache_skipped_count=1,
                startup_mode=False,
            ),
            Tier3CandidateGateResult(
                candidates=filtered,
                original_candidate_count=2,
                negative_cache_skipped_count=1,
            ),
        )

    def test_startup_gate_result_keeps_negative_cache_filtered_candidates_out(self) -> None:
        original = [node("a"), node("b")]

        self.assertEqual(
            tier3_candidate_gate_result(
                original,
                [],
                negative_cache_skipped_count=2,
                startup_mode=True,
            ),
            Tier3CandidateGateResult(
                candidates=[],
                original_candidate_count=2,
                negative_cache_skipped_count=2,
            ),
        )

    def test_startup_gate_result_keeps_empty_when_there_were_no_original_candidates(self) -> None:
        self.assertEqual(
            tier3_candidate_gate_result(
                [],
                [],
                negative_cache_skipped_count=0,
                startup_mode=True,
            ),
            Tier3CandidateGateResult(
                candidates=[],
                original_candidate_count=0,
                negative_cache_skipped_count=0,
            ),
        )

    def test_background_expansion_only_validates_new_candidates_and_retains_pool_nodes(self) -> None:
        stable_previous = node("stable", status=NodeStatus.ALIVE)
        failed_previous = node(
            "failed",
            status=NodeStatus.SUSPICIOUS,
            extra={
                "tier3_consecutive_failures": 1,
                "tier3_last_failure_at": "2026-06-02T00:00:00+00:00",
            },
        )
        fresh = node("fresh")

        ordered = tier3_background_expansion_candidate_layers(
            [stable_previous, fresh, failed_previous],
            [stable_previous, failed_previous],
            identity_fn=lambda value: value.name,
        )

        self.assertEqual(
            ordered,
            Tier3CandidateLayeredOrder(
                candidates=[fresh],
                lane_counts={
                    "exploration": 1,
                    "incumbent_retained": 2,
                },
            ),
        )

    def test_combined_pool_check_tests_pool_and_bounded_new_candidates(self) -> None:
        stable_previous = node("stable", status=NodeStatus.ALIVE)
        failed_previous = node(
            "failed",
            status=NodeStatus.SUSPICIOUS,
            extra={
                "tier3_consecutive_failures": 1,
                "tier3_last_failure_at": "2026-06-02T00:00:00+00:00",
            },
        )
        fresh_one = node("fresh-one")
        fresh_two = node("fresh-two")
        fresh_three = node("fresh-three")

        ordered = tier3_background_expansion_candidate_layers(
            [stable_previous, fresh_one, failed_previous, fresh_two, fresh_three],
            [stable_previous, failed_previous],
            identity_fn=lambda value: value.name,
            include_pool_check=True,
            exploration_limit=2,
        )

        self.assertEqual(
            ordered,
            Tier3CandidateLayeredOrder(
                candidates=[fresh_one, stable_previous, fresh_two, failed_previous],
                lane_counts={
                    "exploration": 2,
                    "exploration_deferred": 1,
                    "incumbent_tested": 2,
                    "incumbent_retained": 0,
                },
            ),
        )


if __name__ == "__main__":
    unittest.main()
