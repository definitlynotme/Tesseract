import unittest

from github_free_node_finder import (
    CandidateSource,
    GitHubDiscoveryLimits,
    _build_opener,
    existing_source_values,
    github_discovery_limits,
    merge_discovered_sources,
    raw_github_url,
)


class GitHubFreeNodeFinderTests(unittest.TestCase):
    def test_discovery_opener_never_uses_host_default_network(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "routed proxy"):
            _build_opener(None)

    def test_raw_github_url_quotes_branch_and_path_segments(self) -> None:
        url = raw_github_url("owner/repo", "feature branch", "Base64/Sub 1.txt")

        self.assertEqual(url, "https://raw.githubusercontent.com/owner/repo/feature%20branch/Base64/Sub%201.txt")

    def test_github_discovery_limits_coerce_invalid_config_to_defaults(self) -> None:
        self.assertEqual(
            github_discovery_limits(
                {
                    "max_repositories": "bad",
                    "min_nodes_per_file": "bad",
                }
            ),
            GitHubDiscoveryLimits(max_repositories=30, min_nodes_per_file=3),
        )
        self.assertEqual(
            github_discovery_limits(
                {
                    "max_repositories": 1,
                    "min_nodes_per_file": 2,
                },
                max_repositories_override=12,
                min_nodes_override=4,
            ),
            GitHubDiscoveryLimits(max_repositories=12, min_nodes_per_file=4),
        )

    def test_merge_discovered_sources_skips_existing_values(self) -> None:
        sources_config = {
            "github_repos": ["https://raw.githubusercontent.com/owner/repo/main/sub.txt"],
            "subscription_urls": [],
            "local_files": [],
        }
        candidates = [
            CandidateSource(
                url="https://raw.githubusercontent.com/owner/repo/main/sub.txt",
                repo="owner/repo",
                path="sub.txt",
                node_count=10,
            ),
            CandidateSource(
                url="https://raw.githubusercontent.com/other/repo/main/sub.txt",
                repo="other/repo",
                path="sub.txt",
                node_count=8,
            ),
        ]

        merged, added = merge_discovered_sources(sources_config, candidates)

        self.assertEqual(added, 1)
        self.assertIn("https://raw.githubusercontent.com/owner/repo/main/sub.txt", existing_source_values(merged))
        self.assertIn("https://raw.githubusercontent.com/other/repo/main/sub.txt", existing_source_values(merged))


if __name__ == "__main__":
    unittest.main()
