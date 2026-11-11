import unittest

from models import Node
from tier3_static_nodes import mark_tier3_static_nodes


def node(name: str, *, extra: dict | None = None) -> Node:
    return Node(
        name=name,
        protocol="ss",
        uri=f"ss://{name}",
        address=f"{name}.example.com",
        port=8388,
        tier="tier3",
        extra=extra or {},
    )


class Tier3StaticNodesTests(unittest.TestCase):
    def test_marks_static_nodes_with_tier3a_origin_and_minimum_bonus(self) -> None:
        first = node("first")
        second = node("second", extra={"tier3_score_bonus": 0.4})

        self.assertEqual(mark_tier3_static_nodes([first, second]), [first, second])

        self.assertEqual(first.extra["tier3_origin"], "tier3a")
        self.assertEqual(first.extra["tier3_score_bonus"], 0.15)
        self.assertEqual(second.extra["tier3_origin"], "tier3a")
        self.assertEqual(second.extra["tier3_score_bonus"], 0.4)

    def test_invalid_existing_score_bonus_falls_back_to_minimum(self) -> None:
        marked = mark_tier3_static_nodes(
            [
                node("invalid", extra={"tier3_score_bonus": "invalid"}),
            ]
        )

        self.assertEqual(marked[0].extra["tier3_score_bonus"], 0.15)


if __name__ == "__main__":
    unittest.main()
