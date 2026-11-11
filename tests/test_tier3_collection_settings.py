import unittest

from tier3_collection_settings import (
    collection_settings,
    merge_github_candidate_paths,
    tier3_collection_settings,
)


class Tier3CollectionSettingsTests(unittest.TestCase):
    def test_collection_settings_copies_testing_config_and_adds_truthy_egress_proxy(self) -> None:
        testing = {"request_timeout": 5}
        egress = {"url": "socks5://127.0.0.1:1080"}

        settings = collection_settings(testing, egress)

        self.assertEqual(settings, {"request_timeout": 5, "egress_proxy": egress})
        self.assertIsNot(settings, testing)
        self.assertNotIn("egress_proxy", collection_settings(testing, {}))

    def test_merge_github_candidate_paths_preserves_defaults_first_and_deduplicates(self) -> None:
        self.assertEqual(
            merge_github_candidate_paths(
                ["", "custom.txt", "default.txt", "custom.txt"],
                ["default.txt", "backup.txt"],
            ),
            ["default.txt", "backup.txt", "custom.txt"],
        )

    def test_tier3_collection_settings_only_adds_candidate_paths_when_configured(self) -> None:
        base = {"request_timeout": 5}
        self.assertEqual(
            tier3_collection_settings(
                base,
                {"github_discovery": {}},
                default_github_candidate_paths=["default.txt"],
            ),
            {"request_timeout": 5},
        )
        self.assertEqual(
            tier3_collection_settings(
                base,
                {"github_discovery": {"candidate_paths": ["custom.txt"]}},
                default_github_candidate_paths=["default.txt"],
            ),
            {"request_timeout": 5, "github_candidate_paths": ["default.txt", "custom.txt"]},
        )
        self.assertEqual(base, {"request_timeout": 5})


if __name__ == "__main__":
    unittest.main()
