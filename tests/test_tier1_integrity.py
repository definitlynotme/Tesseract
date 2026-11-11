import unittest

from models import Node
from tier1_integrity import mark_tier1_link_integrity, tier1_link_integrity_issues


def reality_node(**overrides) -> Node:
    values = {
        "name": "reality",
        "protocol": "vless",
        "uri": "vless://reality",
        "address": "example.com",
        "port": 443,
        "tier": "tier1",
        "uuid": "11111111-1111-1111-1111-111111111111",
        "security": "reality",
        "extra": {"pbk": "public-key", "sid": "abcd"},
    }
    values.update(overrides)
    return Node(**values)


class Tier1IntegrityTests(unittest.TestCase):
    def test_valid_reality_link_marks_integrity_ok(self) -> None:
        node = reality_node()

        issues = mark_tier1_link_integrity(
            node,
            {"name": "Edge", "address": "example.com", "port": 443},
        )

        self.assertEqual(issues, [])
        self.assertTrue(node.extra["config_integrity_ok"])
        self.assertNotIn("config_integrity_errors", node.extra)

    def test_detects_reality_key_and_endpoint_mismatches(self) -> None:
        node = reality_node(address="parsed.example", port=8443, extra={})

        issues = tier1_link_integrity_issues(
            node,
            {"address": "configured.example", "port": 443},
        )

        self.assertIn("address mismatch (parsed.example != configured.example)", issues)
        self.assertIn("port mismatch (8443 != 443)", issues)
        self.assertIn("missing reality public key", issues)
        self.assertIn("missing reality short id", issues)

    def test_invalid_configured_port_becomes_integrity_error_instead_of_crashing(self) -> None:
        node = reality_node()

        issues = mark_tier1_link_integrity(
            node,
            {"address": "example.com", "port": "bad-port"},
        )

        self.assertEqual(issues, ["invalid configured port (bad-port)"])
        self.assertFalse(node.extra["config_integrity_ok"])
        self.assertEqual(node.extra["config_integrity_errors"], issues)


if __name__ == "__main__":
    unittest.main()
