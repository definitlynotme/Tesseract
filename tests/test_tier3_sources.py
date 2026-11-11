import asyncio
import tempfile
import unittest
from pathlib import Path

from tier3_sources import (
    GitHubRepositorySource,
    LocalFileSource,
    RemoteTextSource,
    SourceFetchResult,
    _unit_delivery_timeout_seconds,
    bootstrap_source_entries,
    build_source,
    canonical_source_identity,
    extract_telegram_static_content,
    infer_source_type,
    source_entries_from_sources_config,
    source_entries_from_tier3_config,
    telegram_web_url,
)


class Tier3SourcesTests(unittest.TestCase):
    def test_github_empty_portfolio_reports_no_transport_or_recursive_fetch(self) -> None:
        source = GitHubRepositorySource(
            "https://github.com/owner/repo",
            candidate_paths=["Config/vless.txt"],
        )
        source._candidate_unit_portfolio = lambda _budget: []
        events: list[tuple[str, ...]] = []

        async def fake_fetch(*_args, **_kwargs):
            events.append(("fetch",))
            return SourceFetchResult(
                source.source_id,
                source.source_type,
                source.value,
                source.origin,
                ok=True,
                content="vless://uuid@example.com:443#node",
            )

        source.fetch = fake_fetch

        async def collect_units():
            return [
                result
                async for result in source.fetch_units(
                    None,
                    timeout_seconds=1,
                    retries=0,
                    user_agent="test",
                    proxy_url=None,
                    unit_deadline_seconds=1,
                    unit_route_key="bootstrap",
                    unit_transport_started_observer=(
                        lambda root, unit, route, _started: events.append(
                            ("transport", root, unit, route)
                        )
                    ),
                )
            ]

        units = asyncio.run(collect_units())

        self.assertFalse(units[0].ok)
        self.assertFalse(units[0].transport_started)
        self.assertEqual(events, [])

    def test_telegram_aliases_share_one_canonical_source_identity(self) -> None:
        self.assertEqual(
            canonical_source_identity("telegram_channel", "@V2RayRootFree"),
            canonical_source_identity(
                "telegram_channel",
                "https://t.me/s/V2RayRootFree",
            ),
        )

    def test_github_retry_entry_can_limit_fetch_to_retryable_units(self) -> None:
        source = build_source(
            {
                "type": "github_repo",
                "value": "https://github.com/owner/repo",
                "candidate_paths": ["Config/vless.txt"],
            },
            candidate_paths=["Config/vmess.txt", "Config/trojan.txt"],
        )

        self.assertIsInstance(source, GitHubRepositorySource)
        self.assertEqual(source.candidate_paths, ["Config/vless.txt"])

    def test_sources_config_separates_3b_source_types(self) -> None:
        entries = source_entries_from_sources_config(
            {
                "github_repos": ["https://github.com/owner/repo"],
                "telegram_channels": ["@public_proxy_nodes"],
                "subscription_urls": ["https://example.com/free.txt"],
                "local_files": ["nodes.txt"],
            }
        )

        self.assertEqual([entry["type"] for entry in entries], ["local_file", "subscription_url", "github_repo", "telegram_channel"])
        self.assertTrue(all(entry["origin"] == "tier3b" for entry in entries))

    def test_tier3_config_subscription_entries_are_3a(self) -> None:
        entries = source_entries_from_tier3_config({"subscription_urls": ["https://paid.example.com/sub"]})

        self.assertEqual(entries[0]["type"], "subscription_url")
        self.assertEqual(entries[0]["origin"], "tier3a")

    def test_bootstrap_sources_require_explicit_configuration(self) -> None:
        entries = bootstrap_source_entries()

        self.assertEqual(entries, [])

    def test_bootstrap_sources_can_be_overridden(self) -> None:
        entries = bootstrap_source_entries(
            [
                "https://github.com/owner/repo",
                "@public_proxy_nodes",
                "https://example.com/free.txt",
            ]
        )

        self.assertEqual([entry["type"] for entry in entries], ["github_repo", "telegram_channel", "subscription_url"])
        self.assertTrue(all(entry["origin"] == "bootstrap" for entry in entries))
        self.assertEqual(bootstrap_source_entries([]), [])

    def test_string_false_disables_source_entries(self) -> None:
        entries = source_entries_from_sources_config(
            {
                "subscription_urls": [
                    {"url": "https://enabled.example.com/sub.txt", "enabled": "true"},
                    {"url": "https://disabled.example.com/sub.txt", "enabled": "false"},
                ],
            }
        )

        self.assertEqual([entry["value"] for entry in entries], ["https://enabled.example.com/sub.txt"])

        bootstrap_entries = bootstrap_source_entries(
            [{"url": "https://disabled-bootstrap.example.com/sub.txt", "enabled": "off"}]
        )
        self.assertEqual(bootstrap_entries, [])

    def test_source_type_inference(self) -> None:
        self.assertEqual(infer_source_type("https://github.com/owner/repo"), "github_repo")
        self.assertEqual(infer_source_type("https://t.me/channel"), "telegram_channel")
        self.assertEqual(infer_source_type("@channel"), "telegram_channel")
        self.assertEqual(infer_source_type("https://example.com/sub.txt"), "subscription_url")

    def test_telegram_channel_uses_public_web_view(self) -> None:
        self.assertEqual(telegram_web_url("@channel_name"), "https://t.me/s/channel_name")
        self.assertEqual(telegram_web_url("https://t.me/channel_name"), "https://t.me/s/channel_name")

    def test_telegram_static_extraction_uses_message_content_only(self) -> None:
        content = extract_telegram_static_content(
            """
            <html>
              <body>
                <div class="tgme_channel_info">Telegram preview shell</div>
                <div class="tgme_widget_message_text">
                  Fresh node <a href="vless://uuid@example.com:443?security=tls#node">link</a>
                </div>
              </body>
            </html>
            """
        )

        self.assertIn("vless://uuid@example.com:443?security=tls#node", content)
        self.assertIn("Fresh node", content)
        self.assertNotIn("Telegram preview shell", content)

    def test_telegram_static_extraction_rejects_js_only_preview(self) -> None:
        content = extract_telegram_static_content("<html><body><script>window.messages=[]</script></body></html>")

        self.assertEqual(content, "")

    def test_github_repo_source_discovers_candidate_raw_urls(self) -> None:
        source = GitHubRepositorySource("https://github.com/owner/repo", candidate_paths=["sub.txt"])
        candidates = source.discover()

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].value, "https://raw.githubusercontent.com/owner/repo/main/sub.txt")

    def test_github_repo_first_unit_budget_covers_protocol_families(self) -> None:
        source = GitHubRepositorySource("https://github.com/owner/repo")

        units = source.unit_descriptors(unit_budget=6)

        self.assertEqual(
            [unit["family"] for unit in units],
            ["vless", "vmess", "trojan", "ss", "modern", "aggregate"],
        )

    def test_github_repo_source_preserves_raw_candidate_bodies_for_collector(self) -> None:
        class Candidate:
            source_type = "subscription_url"
            origin = "tier3b"

            def __init__(self, value: str, content: str) -> None:
                self.value = value
                self.content = content

            @property
            def source_id(self) -> str:
                return f"subscription_url:{self.value}"

            async def fetch(self, *_args, **_kwargs):
                return SourceFetchResult(self.source_id, self.source_type, self.value, self.origin, ok=True, content=self.content)

        source = GitHubRepositorySource("https://github.com/owner/repo")
        source.discover = lambda: [
            Candidate("https://raw.githubusercontent.com/owner/repo/main/README.md", "# no nodes here"),
            Candidate("https://raw.githubusercontent.com/owner/repo/main/sub.txt", "vless://uuid@example.com:443#node"),
        ]

        content = asyncio.run(source._fetch(None, timeout_seconds=1, retries=0, user_agent="test", proxy_url=None, max_bytes=1024))

        self.assertIn("# no nodes here", content)
        self.assertIn("vless://uuid@example.com:443#node", content)

    def test_github_repo_source_fetches_candidate_paths_concurrently(self) -> None:
        fast_started = asyncio.Event()
        slow_saw_fast_start = {"value": False}

        class Candidate:
            source_type = "subscription_url"
            origin = "tier3b"

            def __init__(self, value: str, content: str, *, slow: bool = False) -> None:
                self.value = value
                self.content = content
                self.slow = slow

            @property
            def source_id(self) -> str:
                return f"subscription_url:{self.value}"

            async def fetch(self, *_args, **_kwargs):
                if self.slow:
                    await asyncio.wait_for(fast_started.wait(), timeout=1)
                    slow_saw_fast_start["value"] = True
                    return SourceFetchResult(self.source_id, self.source_type, self.value, self.origin, ok=True, content=self.content)
                fast_started.set()
                await asyncio.sleep(0.05)
                return SourceFetchResult(self.source_id, self.source_type, self.value, self.origin, ok=True, content=self.content)

        source = GitHubRepositorySource("https://github.com/owner/repo")
        source.discover = lambda: [
            Candidate("https://raw.githubusercontent.com/owner/repo/main/README.md", "# no nodes here", slow=True),
            Candidate("https://raw.githubusercontent.com/owner/repo/main/sub.txt", "vless://uuid@example.com:443#node"),
        ]

        content = asyncio.run(source._fetch(None, timeout_seconds=1, retries=0, user_agent="test", proxy_url=None, max_bytes=1024))

        self.assertIn("# no nodes here", content)
        self.assertIn("vless://uuid@example.com:443#node", content)
        self.assertTrue(slow_saw_fast_start["value"])

    def test_github_repo_source_emits_fast_ss_and_delayed_vless_units(self) -> None:
        vless_cancelled = {"value": False}

        class Candidate:
            source_type = "subscription_url"
            origin = "tier3b"

            def __init__(self, value: str, content: str, delay: float) -> None:
                self.value = value
                self.content = content
                self.delay = delay

            @property
            def source_id(self) -> str:
                return f"subscription_url:{self.value}"

            async def fetch(self, *_args, **_kwargs):
                try:
                    await asyncio.sleep(self.delay)
                except asyncio.CancelledError:
                    if "vless" in self.value:
                        vless_cancelled["value"] = True
                    raise
                return SourceFetchResult(
                    self.source_id,
                    self.source_type,
                    self.value,
                    self.origin,
                    ok=True,
                    content=self.content,
                )

        paths = ["Config/shadowsocks.txt", "Config/vless.txt"]
        source = GitHubRepositorySource(
            "https://github.com/owner/repo",
            candidate_paths=paths,
        )
        source.discover = lambda: [
            Candidate(
                "https://raw.githubusercontent.com/owner/repo/main/Config/shadowsocks.txt",
                "vless://fast@example.com:443#fast-ss-unit",
                0.001,
            ),
            Candidate(
                "https://raw.githubusercontent.com/owner/repo/main/Config/vless.txt",
                "vless://delayed@example.com:443#delayed-vless-unit",
                0.05,
            ),
        ]

        async def collect_units():
            return [
                result
                async for result in source.fetch_units(
                    None,
                    timeout_seconds=1,
                    retries=0,
                    user_agent="test",
                    proxy_url=None,
                    max_bytes=1024,
                    unit_budget=6,
                    unit_concurrency=6,
                    unit_deadline_seconds=1,
                )
            ]

        units = asyncio.run(collect_units())

        self.assertEqual([unit.unit_id for unit in units], paths)
        self.assertEqual([unit.unit_family for unit in units], ["ss", "vless"])
        self.assertTrue(all(unit.ok for unit in units))
        self.assertFalse(vless_cancelled["value"])

    def test_github_aggregate_is_deferred_when_protocol_unit_supplies_nodes(self) -> None:
        fetch_counts = {"protocol": 0, "aggregate": 0}

        class Candidate:
            source_type = "subscription_url"
            origin = "tier3b"

            def __init__(self, value: str, content: str, key: str) -> None:
                self.value = value
                self.content = content
                self.key = key

            @property
            def source_id(self) -> str:
                return f"subscription_url:{self.value}"

            async def fetch(self, *_args, **_kwargs):
                fetch_counts[self.key] += 1
                return SourceFetchResult(
                    self.source_id,
                    self.source_type,
                    self.value,
                    self.origin,
                    ok=True,
                    content=self.content,
                )

        paths = ["Config/vless.txt", "Config/all_config.txt"]
        source = GitHubRepositorySource(
            "https://github.com/owner/repo",
            candidate_paths=paths,
        )
        source.discover = lambda: [
            Candidate(
                "https://raw.example/vless.txt",
                "vless://uuid@example.com:443#protocol",
                "protocol",
            ),
            Candidate(
                "https://raw.example/all_config.txt",
                "vless://other@example.com:443#aggregate",
                "aggregate",
            ),
        ]

        async def collect_units():
            units = []
            async for result in source.fetch_units(
                    None,
                    timeout_seconds=1,
                    retries=0,
                    user_agent="test",
                    proxy_url=None,
                    max_bytes=1024,
                    unit_budget=2,
                    unit_concurrency=2,
                    unit_deadline_seconds=1,
                    unit_hedge_delay_seconds=0.05,
                ):
                units.append(result)
                if result.unit_family != "aggregate":
                    result.acknowledge_parse_valid(True)
            return units

        units = asyncio.run(collect_units())

        self.assertEqual(fetch_counts, {"protocol": 1, "aggregate": 0})
        self.assertTrue(units[0].ok)
        self.assertEqual(len(units), 1)

    def test_github_aggregate_hedges_when_protocol_body_is_not_parse_valid(self) -> None:
        fetch_counts = {"protocol": 0, "aggregate": 0}

        class Candidate:
            source_type = "subscription_url"
            origin = "tier3b"

            def __init__(self, value: str, content: str, key: str) -> None:
                self.value = value
                self.content = content
                self.key = key

            @property
            def source_id(self) -> str:
                return f"subscription_url:{self.value}"

            async def fetch(self, *_args, **_kwargs):
                fetch_counts[self.key] += 1
                return SourceFetchResult(
                    self.source_id,
                    self.source_type,
                    self.value,
                    self.origin,
                    ok=True,
                    content=self.content,
                )

        source = GitHubRepositorySource(
            "https://github.com/owner/repo",
            candidate_paths=["Config/vless.txt", "Config/all_config.txt"],
        )
        source.discover = lambda: [
            Candidate("https://raw.example/vless.txt", "# no nodes", "protocol"),
            Candidate(
                "https://raw.example/all_config.txt",
                "vless://other@example.com:443#aggregate",
                "aggregate",
            ),
        ]

        async def collect_units():
            units = []
            async for result in source.fetch_units(
                    None,
                    timeout_seconds=1,
                    retries=0,
                    user_agent="test",
                    proxy_url=None,
                    max_bytes=1024,
                    unit_budget=2,
                    unit_concurrency=2,
                    unit_deadline_seconds=1,
                    unit_hedge_delay_seconds=0.01,
                ):
                units.append(result)
                result.acknowledge_parse_valid(
                    result.unit_family == "aggregate"
                )
            return units

        units = asyncio.run(collect_units())

        self.assertEqual(fetch_counts, {"protocol": 1, "aggregate": 1})
        self.assertTrue(units[0].ok)
        self.assertTrue(units[1].ok)
        self.assertEqual(units[1].unit_family, "aggregate")

        source.candidate_paths = ["Config/all_config.txt"]
        source.discover = lambda: [
            Candidate(
                "https://raw.example/all_config.txt",
                "vless://other@example.com:443#aggregate",
                "aggregate",
            )
        ]
        retry_units = asyncio.run(collect_units())

        self.assertEqual(fetch_counts, {"protocol": 1, "aggregate": 2})
        self.assertEqual(len(retry_units), 1)
        self.assertTrue(retry_units[0].ok)

    def test_unit_deadline_has_bounded_terminal_delivery_grace(self) -> None:
        self.assertEqual(_unit_delivery_timeout_seconds(8.0, 0.25), 8.25)

        class DelayedSource(RemoteTextSource):
            def __init__(self, delay: float) -> None:
                super().__init__("https://example.invalid/source")
                self.delay = delay

            async def fetch(self, *_args, **_kwargs):
                await asyncio.sleep(self.delay)
                return SourceFetchResult(
                    self.source_id,
                    self.source_type,
                    self.value,
                    self.origin,
                    ok=True,
                    content="vless://uuid@example.com:443#node",
                )

        async def collect(delay: float):
            source = DelayedSource(delay)
            return [
                result
                async for result in source.fetch_units(
                    None,
                    timeout_seconds=1,
                    retries=0,
                    user_agent="test",
                    proxy_url=None,
                    unit_deadline_seconds=0.08,
                    delivery_slack_seconds=0.05,
                )
            ][0]

        within_grace = asyncio.run(collect(0.09))
        beyond_grace = asyncio.run(collect(0.16))

        self.assertTrue(within_grace.ok)
        self.assertFalse(beyond_grace.ok)
        self.assertIn("timeout after 0.08s", beyond_grace.error)

    def test_github_slow_primary_tranche_does_not_block_fast_hedge_units(self) -> None:
        class Candidate:
            source_type = "subscription_url"
            origin = "tier3b"

            def __init__(self, value: str, *, delay: float) -> None:
                self.value = value
                self.delay = delay

            @property
            def source_id(self) -> str:
                return f"subscription_url:{self.value}"

            async def fetch(self, *_args, **_kwargs):
                await asyncio.sleep(self.delay)
                return SourceFetchResult(
                    self.source_id,
                    self.source_type,
                    self.value,
                    self.origin,
                    ok=True,
                    content=(
                        f"vless://uuid@{self.value.rsplit('/', 1)[-1]}:443#node"
                    ),
                    first_byte_elapsed_ms=max(1, int(self.delay * 500)),
                    transport_elapsed_ms=max(1, int(self.delay * 1000)),
                )

        paths = [
            "Config/vless-slow.txt",
            "Config/vless-fast.txt",
            "Config/vmess-slow.txt",
            "Config/vmess-fast.txt",
            "Config/trojan-slow.txt",
            "Config/trojan-fast.txt",
            "Config/ss.txt",
            "Config/shadowsocks.txt",
            "Config/hy2-slow.txt",
            "Config/hy2-fast.txt",
            "Config/opaque-slow.txt",
            "Config/opaque-fast.txt",
        ]
        source = GitHubRepositorySource(
            "https://github.com/owner/repo",
            candidate_paths=paths,
        )
        source.discover = lambda: [
            Candidate(
                f"https://raw.example/{path}",
                delay=(0.35 if "slow" in path or path.endswith("ss.txt") else 0.03),
            )
            for path in paths
        ]

        async def collect_units():
            started = asyncio.get_running_loop().time()
            units = []
            first_elapsed = 0.0
            async for result in source.fetch_units(
                None,
                timeout_seconds=1,
                retries=0,
                user_agent="test",
                proxy_url=None,
                max_bytes=4096,
                unit_budget=12,
                unit_concurrency=12,
                unit_deadline_seconds=1,
                unit_hedge_delay_seconds=0.02,
            ):
                units.append(result)
                if len(units) == 1:
                    first_elapsed = asyncio.get_running_loop().time() - started
            return units, first_elapsed

        units, first_elapsed = asyncio.run(collect_units())

        self.assertEqual(len(units), 12)
        self.assertEqual(units[0].launch_tranche, "hedge")
        self.assertLess(first_elapsed, 0.15)
        self.assertGreaterEqual(units[0].transport_started_elapsed_ms, 10)
        self.assertTrue(all(unit.ok for unit in units))

    def test_github_parse_valid_primary_tranche_suppresses_hedge_units(self) -> None:
        class Candidate:
            source_type = "subscription_url"
            origin = "tier3b"

            def __init__(self, value: str) -> None:
                self.value = value

            @property
            def source_id(self) -> str:
                return f"subscription_url:{self.value}"

            async def fetch(self, *_args, **_kwargs):
                await asyncio.sleep(0.005)
                return SourceFetchResult(
                    self.source_id,
                    self.source_type,
                    self.value,
                    self.origin,
                    ok=True,
                    content=f"vless://uuid@{self.value.rsplit('/', 1)[-1]}:443#node",
                )

        paths = [
            "Config/vless-a.txt",
            "Config/vless-b.txt",
            "Config/vmess-a.txt",
            "Config/vmess-b.txt",
            "Config/trojan-a.txt",
            "Config/trojan-b.txt",
            "Config/ss.txt",
            "Config/shadowsocks.txt",
            "Config/hy2-a.txt",
            "Config/hy2-b.txt",
            "Config/opaque-a.txt",
            "Config/opaque-b.txt",
        ]
        source = GitHubRepositorySource(
            "https://github.com/owner/repo",
            candidate_paths=paths,
        )
        source.discover = lambda: [
            Candidate(f"https://raw.example/{path}") for path in paths
        ]

        async def collect_units():
            units = []
            async for result in source.fetch_units(
                None,
                timeout_seconds=1,
                retries=0,
                user_agent="test",
                proxy_url=None,
                max_bytes=4096,
                unit_budget=12,
                unit_concurrency=12,
                unit_deadline_seconds=1,
                unit_hedge_delay_seconds=0.05,
            ):
                units.append(result)
                result.acknowledge_parse_valid(True)
            return units

        units = asyncio.run(collect_units())

        self.assertEqual(len(units), 6)
        self.assertTrue(all(unit.launch_tranche == "primary" for unit in units))

    def test_github_portfolio_interleaves_opaque_families_without_named_priority(self) -> None:
        paths = [
            "one/a-1.txt",
            "one/a-2.txt",
            "two/b-1.txt",
            "two/b-2.txt",
            "three/c-1.txt",
            "three/c-2.txt",
        ]
        source = GitHubRepositorySource(
            "https://github.com/owner/repo",
            candidate_paths=paths,
        )
        candidates = source.discover()
        family_by_path = {
            "one/a-1.txt": "opaque-a",
            "one/a-2.txt": "opaque-a",
            "two/b-1.txt": "opaque-b",
            "two/b-2.txt": "opaque-b",
            "three/c-1.txt": "opaque-c",
            "three/c-2.txt": "opaque-c",
        }
        source._candidate_unit_family = lambda path: family_by_path[path]
        source.discover = lambda: candidates

        selected = source._candidate_unit_portfolio(6)

        self.assertEqual(
            [path for path, _candidate, _family in selected],
            [
                "one/a-1.txt",
                "two/b-1.txt",
                "three/c-1.txt",
                "one/a-2.txt",
                "two/b-2.txt",
                "three/c-2.txt",
            ],
        )

    def test_local_file_source_reads_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "nodes.txt"
            path.write_text("vless://uuid@example.com:443#node", encoding="utf-8")
            source = LocalFileSource(str(path))

            result = asyncio.run(source.fetch(None, timeout_seconds=1, retries=0, user_agent="test", proxy_url=None))

            self.assertTrue(result.ok)
            self.assertIn("vless://uuid@example.com:443#node", result.content)

    def test_local_file_source_rejects_oversized_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "nodes.txt"
            path.write_text("x" * 32, encoding="utf-8")
            source = LocalFileSource(str(path))

            result = asyncio.run(source.fetch(None, timeout_seconds=1, retries=0, user_agent="test", proxy_url=None, max_bytes=8))

            self.assertFalse(result.ok)
            self.assertIn("read cap", result.error)


if __name__ == "__main__":
    unittest.main()
