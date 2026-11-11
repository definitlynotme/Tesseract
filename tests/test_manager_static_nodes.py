import unittest

from manager_static_nodes import nodes_from_single_protocol_entries


SS_LINK = "ss://YWVzLTEyOC1nY206cGFzc0BleGFtcGxlLmNvbTo4Mzg4#parsed-name"


class ManagerStaticNodesTests(unittest.TestCase):
    def test_single_protocol_entries_skip_disabled_items(self) -> None:
        self.assertEqual(
            nodes_from_single_protocol_entries(
                [
                    {
                        "enabled": "false",
                        "uri": SS_LINK,
                    }
                ],
                "tier2",
            ),
            [],
        )

    def test_single_protocol_entries_override_single_parsed_name(self) -> None:
        nodes = nodes_from_single_protocol_entries(
            [
                {
                    "name": "Configured",
                    "uri": SS_LINK,
                }
            ],
            "tier2",
        )

        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].name, "Configured")
        self.assertEqual(nodes[0].remarks, "Configured")
        self.assertEqual(nodes[0].tier, "tier2")

    def test_single_protocol_string_entries_assign_target_tier(self) -> None:
        nodes = nodes_from_single_protocol_entries([SS_LINK], "tier3")

        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].tier, "tier3")


if __name__ == "__main__":
    unittest.main()
