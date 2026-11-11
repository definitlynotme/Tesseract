import unittest

from collection_settings import (
    collection_max_bytes,
    collection_retries,
    collection_timeout_seconds,
    collection_user_agents,
)
from tier3_sources import DEFAULT_SOURCE_MAX_BYTES


class CollectionSettingsTests(unittest.TestCase):
    def test_collection_timeout_seconds_uses_positive_default_for_invalid_values(self) -> None:
        self.assertEqual(collection_timeout_seconds({"request_timeout": "7"}), 7)
        self.assertEqual(collection_timeout_seconds({"request_timeout": "bad"}), 15)
        self.assertEqual(collection_timeout_seconds({"request_timeout": 0}), 1)
        self.assertEqual(collection_timeout_seconds(None), 15)

    def test_collection_retries_allows_zero_and_falls_back(self) -> None:
        self.assertEqual(collection_retries({"retries": "4"}), 4)
        self.assertEqual(collection_retries({"retries": 0}), 0)
        self.assertEqual(collection_retries({"retries": "bad"}), 2)

    def test_collection_max_bytes_supports_legacy_alias_and_default(self) -> None:
        self.assertEqual(collection_max_bytes({"max_source_bytes": "1024"}), 1024)
        self.assertEqual(collection_max_bytes({"source_max_bytes": "2048"}), 2048)
        self.assertEqual(collection_max_bytes({"max_source_bytes": "bad"}), DEFAULT_SOURCE_MAX_BYTES)

    def test_collection_user_agents_filters_blank_values_and_falls_back(self) -> None:
        self.assertEqual(collection_user_agents({"user_agents": [" a ", "", 7]}, ["d"]), ["a", "7"])
        self.assertEqual(collection_user_agents({"user_agents": ("a", "b")}, ["d"]), ["a", "b"])
        self.assertEqual(collection_user_agents({"user_agents": "bad"}, ["d"]), ["d"])


if __name__ == "__main__":
    unittest.main()
