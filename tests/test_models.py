import unittest
import threading

from models import Node, NodeStatus, deduplicate_nodes


class ModelTests(unittest.TestCase):
    def test_deduplicate_honors_preexisting_cancel(self) -> None:
        cancel_event = threading.Event()
        cancel_event.set()
        nodes = [
            Node(
                name=f"node-{index}",
                protocol="ss",
                address=f"{index}.example",
                port=8388,
            )
            for index in range(300)
        ]

        self.assertEqual(deduplicate_nodes(nodes, cancel_event=cancel_event), [])

    def test_deduplicate_checks_cancel_during_large_input(self) -> None:
        class CancelAfterTwoChecks:
            def __init__(self) -> None:
                self.checks = 0

            def is_set(self) -> bool:
                self.checks += 1
                return self.checks >= 2

        cancel_event = CancelAfterTwoChecks()
        nodes = [
            Node(
                name=f"node-{index}",
                protocol="ss",
                address=f"{index}.example",
                port=8388,
            )
            for index in range(1_000)
        ]

        self.assertEqual(deduplicate_nodes(nodes, cancel_event=cancel_event), [])
        self.assertGreaterEqual(cancel_event.checks, 2)

    def test_deduplicate_checks_cancel_before_returning_small_input(self) -> None:
        class CancelOnFinalCheck:
            def __init__(self) -> None:
                self.checks = 0

            def is_set(self) -> bool:
                self.checks += 1
                return self.checks >= 2

        cancel_event = CancelOnFinalCheck()
        nodes = [
            Node(
                name=f"node-{index}",
                protocol="ss",
                address=f"{index}.example",
                port=8388,
            )
            for index in range(4)
        ]

        self.assertEqual(deduplicate_nodes(nodes, cancel_event=cancel_event), [])
        self.assertEqual(cancel_event.checks, 2)

    def test_deduplicate_preserves_protocols_on_same_endpoint(self) -> None:
        nodes = [
            Node(name="vless", protocol="vless", uri="", address="example.com", port=443, uuid="id"),
            Node(name="hy2", protocol="hysteria2", uri="", address="example.com", port=443, password="pass"),
        ]

        deduped = deduplicate_nodes(nodes)

        self.assertEqual([node.protocol for node in deduped], ["vless", "hysteria2"])

    def test_node_from_dict_treats_unknown_status_as_unknown(self) -> None:
        node = Node.from_dict(
            {
                "name": "node",
                "protocol": "ss",
                "uri": "ss://node",
                "address": "example.com",
                "port": 8388,
                "status": "retired",
            }
        )

        self.assertEqual(node.status, NodeStatus.UNKNOWN)

    def test_node_uri_defaults_to_empty_string(self) -> None:
        node = Node(name="node", protocol="ss", address="example.com", port=8388)

        self.assertEqual(node.uri, "")


if __name__ == "__main__":
    unittest.main()
