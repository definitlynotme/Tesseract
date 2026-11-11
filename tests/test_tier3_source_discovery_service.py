import logging
import unittest

from github_free_node_finder import CandidateSource
from tier3_source_discovery_service import discover_tier3_sources_update


class Tier3SourceDiscoveryServiceTests(unittest.TestCase):
    def test_discovery_disabled_without_force_does_not_call_discoverer(self) -> None:
        calls: list[str] = []

        outcome = discover_tier3_sources_update(
            config={},
            sources_config={"github_discovery": {"enabled": False}},
            limit=5,
            force=False,
            logger=logging.getLogger("test"),
            discoverer=lambda *_args, **_kwargs: calls.append("called") or [],
        )

        self.assertEqual(outcome.result, {"enabled": False, "discovered": 0, "added": 0, "sources": []})
        self.assertIsNone(outcome.merged_sources)
        self.assertEqual(calls, [])

    def test_discovery_merges_only_new_sources_and_preserves_summary(self) -> None:
        candidates = [
            CandidateSource(
                url="https://raw.githubusercontent.com/example/old/main/sub.txt",
                repo="example/old",
                path="sub.txt",
                node_count=5,
            ),
            CandidateSource(
                url="https://raw.githubusercontent.com/example/new/main/sub.txt",
                repo="example/new",
                path="sub.txt",
                node_count=7,
            ),
        ]

        def discoverer(*_args, **kwargs):
            self.assertEqual(kwargs["max_repositories"], 4)
            self.assertEqual(kwargs["min_nodes_per_file"], 2)
            self.assertEqual(kwargs["limit"], 3)
            self.assertEqual(kwargs["proxy_url"], "http://127.0.0.1:19094")
            return candidates

        outcome = discover_tier3_sources_update(
            config={"client": {}},
            sources_config={
                "github_repos": ["https://raw.githubusercontent.com/example/old/main/sub.txt"],
                "github_discovery": {"max_repositories": 4, "min_nodes_per_file": 2},
            },
            limit=3,
            force=False,
            logger=logging.getLogger("test"),
            discoverer=discoverer,
            proxy_url="http://127.0.0.1:19094",
        )

        self.assertEqual(outcome.result["enabled"], True)
        self.assertEqual(outcome.result["discovered"], 2)
        self.assertEqual(outcome.result["added"], 1)
        self.assertEqual(
            outcome.result["sources"],
            [
                {
                    "url": "https://raw.githubusercontent.com/example/new/main/sub.txt",
                    "repo": "example/new",
                    "path": "sub.txt",
                    "node_count": 7,
                }
            ],
        )
        self.assertIsNotNone(outcome.merged_sources)
        self.assertEqual(len(outcome.merged_sources["github_repos"]), 2)

    def test_discovery_returns_no_write_when_every_candidate_exists(self) -> None:
        existing_url = "https://raw.githubusercontent.com/example/old/main/sub.txt"

        outcome = discover_tier3_sources_update(
            config={},
            sources_config={"github_repos": [existing_url]},
            limit=10,
            force=True,
            logger=logging.getLogger("test"),
            discoverer=lambda *_args, **_kwargs: [
                CandidateSource(
                    url=existing_url,
                    repo="example/old",
                    path="sub.txt",
                    node_count=5,
                )
            ],
        )

        self.assertEqual(outcome.result, {"enabled": True, "discovered": 1, "added": 0, "sources": []})
        self.assertIsNone(outcome.merged_sources)


if __name__ == "__main__":
    unittest.main()
