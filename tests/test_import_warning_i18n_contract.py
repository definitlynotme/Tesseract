from __future__ import annotations

import unittest

from import_node_rules import import_warnings_for_tier, multiprotocol_import_warnings
from models import Node


class ImportWarningI18nContractTests(unittest.TestCase):
    def test_import_warning_producers_return_code_params_and_keep_legacy_log_text(self) -> None:
        tier_warning = import_warnings_for_tier(
            "tier3",
            [Node(name="backup", protocol="ssr", address="ssr.example", port=443)],
        )
        multiprotocol = multiprotocol_import_warnings(
            {"protocols": {"vless": {}, "future-protocol": {}}},
            [Node(name="structured", protocol="vless", address="vless.example", port=443)],
        )
        warnings = [*tier_warning, *multiprotocol]

        self.assertTrue(all(isinstance(item, dict) for item in warnings))
        self.assertTrue(all({"code", "params", "message"} <= set(item) for item in warnings))
        by_code = {item["code"]: item for item in warnings}
        self.assertEqual(
            set(by_code),
            {"unsupported_sections_ignored", "structured_export_incomplete"},
        )
        self.assertEqual(by_code["unsupported_sections_ignored"]["params"]["sections"], ["future-protocol"])
        self.assertIn("future-protocol", by_code["unsupported_sections_ignored"]["message"])
        self.assertFalse(by_code["structured_export_incomplete"]["display"])
        self.assertFalse(any("unsupported by Mihomo" in item["message"] for item in warnings))


if __name__ == "__main__":
    unittest.main()
