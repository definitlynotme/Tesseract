from __future__ import annotations

import unittest

from exit_nodes import exit_nodes_from_entry, mark_exit_node
from models import Node


SS_LINK = "ss://YWVzLTI1Ni1nY206cGFzc0BleGl0LmV4YW1wbGUuY29tOjgzODg#Exit-SS"


class ExitNodesTests(unittest.TestCase):
    def test_exit_nodes_from_string_link_marks_gateway_metadata(self) -> None:
        nodes = exit_nodes_from_entry(SS_LINK, 0)

        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].tier, "exit")
        self.assertEqual(nodes[0].source, "config.yaml")
        self.assertTrue(nodes[0].extra["exit_node"])
        self.assertEqual(nodes[0].name, "Exit-SS")

    def test_exit_nodes_from_dict_link_can_override_single_name(self) -> None:
        nodes = exit_nodes_from_entry({"name": "Gateway", "uri": SS_LINK}, 0)

        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].name, "Gateway")
        self.assertEqual(nodes[0].remarks, "Gateway")
        self.assertEqual(nodes[0].tier, "exit")

    def test_exit_nodes_from_disabled_entry_returns_empty(self) -> None:
        self.assertEqual(exit_nodes_from_entry({"enabled": "false", "uri": SS_LINK}, 0), [])

    def test_mark_exit_node_preserves_existing_source_and_assigns_default_name(self) -> None:
        node = Node(name="", address="gateway.example.com", port=443, source="cache.json")

        marked = mark_exit_node(node, index=2)

        self.assertIs(marked, node)
        self.assertEqual(node.tier, "exit")
        self.assertEqual(node.source, "cache.json")
        self.assertTrue(node.extra["exit_node"])
        self.assertEqual(node.name, "exit-3")


if __name__ == "__main__":
    unittest.main()
