import unittest

from models import Node
from runtime_node_pool import dedupe_runtime_nodes, runtime_node_dedupe_key, runtime_node_index


def node(name: str, *, uri: str = "", uuid: str | None = None, password: str | None = None) -> Node:
    return Node(
        name=name,
        protocol="ss",
        uri=uri,
        address="example.com",
        port=8388,
        tier="tier2",
        uuid=uuid,
        password=password,
    )


class RuntimeNodePoolTests(unittest.TestCase):
    def test_runtime_dedupe_key_prefers_auth_then_uri_then_name(self) -> None:
        self.assertEqual(runtime_node_dedupe_key(node("name", uuid="id"))[-1], "id")
        self.assertEqual(runtime_node_dedupe_key(node("name", password="pass"))[-1], "pass")
        self.assertEqual(runtime_node_dedupe_key(node("name", uri="ss://one"))[-1], "ss://one")
        self.assertEqual(runtime_node_dedupe_key(node("name"))[-1], "name")

    def test_dedupe_runtime_nodes_keeps_first_node_for_duplicate_key(self) -> None:
        first = node("first", uri="ss://same")
        duplicate = node("duplicate", uri="ss://same")
        distinct = node("distinct", uri="ss://distinct")

        self.assertEqual(dedupe_runtime_nodes([first, duplicate, distinct]), [first, distinct])

    def test_runtime_node_index_uses_supplied_identity_function(self) -> None:
        first = node("first")
        second = node("second")

        self.assertEqual(
            runtime_node_index(
                [first, second],
                "node:second",
                identity_fn=lambda item: f"node:{item.name}",
            ),
            1,
        )
        self.assertIsNone(
            runtime_node_index(
                [first, second],
                "node:missing",
                identity_fn=lambda item: f"node:{item.name}",
            )
        )


if __name__ == "__main__":
    unittest.main()
