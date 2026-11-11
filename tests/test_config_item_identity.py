import unittest

from config_item_identity import imported_node_uri, source_entry_value


class ConfigItemIdentityTests(unittest.TestCase):
    def test_imported_node_uri_accepts_string_and_legacy_fields(self) -> None:
        self.assertEqual(imported_node_uri("vless://node"), "vless://node")
        self.assertEqual(imported_node_uri({"uri": "vless://uri"}), "vless://uri")
        self.assertEqual(imported_node_uri({"link": "trojan://link"}), "trojan://link")
        self.assertEqual(imported_node_uri({"url": "ss://url"}), "ss://url")
        self.assertEqual(imported_node_uri({"name": "missing"}), "")

    def test_source_entry_value_accepts_string_and_legacy_fields(self) -> None:
        self.assertEqual(source_entry_value("https://example.com/sub.txt"), "https://example.com/sub.txt")
        self.assertEqual(source_entry_value({"value": "@channel"}), "@channel")
        self.assertEqual(source_entry_value({"url": "https://example.com/free.txt"}), "https://example.com/free.txt")
        self.assertEqual(source_entry_value({"path": "C:/subs/free.txt"}), "C:/subs/free.txt")
        self.assertEqual(source_entry_value({"enabled": True}), "")


if __name__ == "__main__":
    unittest.main()
