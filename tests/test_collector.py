import unittest
import asyncio
import base64
import hashlib
import io
import logging
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

from collector import (
    BlockingWorkCancellationTimeout,
    DEFAULT_USER_AGENTS,
    _Tier3SourceUnitTransportBroker,
    _parse_content_blob_bounded,
    _run_blocking_cancellable,
    collect_nodes_from_sources_with_report,
    load_snapshot,
    proxy_url_from_config,
    save_snapshot,
)
from models import Node, PoolSnapshot
from parser import parse_content_blob
from runtime_source_transport import (
    RuntimeSourceFetchEndpoint,
    RuntimeSourceListenerCredentials,
    RuntimeSourceRouteKind,
)
from tier3_sources import SourceFetchResult, SourceUnitResult


class CollectorTests(unittest.TestCase):
    def test_source_transport_start_observer_precedes_ingress_observer(self) -> None:
        events: list[str] = []

        class FakeSource:
            value = "https://source.example/transport-start"
            source_type = "subscription_url"
            origin = "tier3b"

            @property
            def source_id(self):
                return f"subscription_url:{self.value}"

            def metadata(self):
                return {
                    "id": self.source_id,
                    "type": self.source_type,
                    "value": self.value,
                    "origin": self.origin,
                    "tags": [],
                }

            async def fetch_units(self, *_args, **kwargs):
                observer = kwargs.get("unit_transport_started_observer")
                self.assert_observer(observer)
                observer(
                    self.source_id,
                    "default",
                    str(kwargs.get("unit_route_key") or "default"),
                    time.monotonic(),
                )
                payload = base64.urlsafe_b64encode(
                    b"aes-256-gcm:pass@transport-start.example:8388"
                ).decode("ascii").rstrip("=")
                yield SourceUnitResult(
                    root_id=self.source_id,
                    root_type=self.source_type,
                    root_value=self.value,
                    origin=self.origin,
                    unit_id="default",
                    unit_value=self.value,
                    unit_family="default",
                    ok=True,
                    content=f"ss://{payload}#transport-start",
                )

            @staticmethod
            def assert_observer(observer):
                if not callable(observer):
                    raise AssertionError("transport-start observer was not forwarded")

        source = FakeSource()

        async def scenario():
            with patch("collector.build_source", return_value=source):
                return await collect_nodes_from_sources_with_report(
                    [{"type": source.source_type, "value": source.value}],
                    tier="tier3",
                    logger=logging.getLogger("test-source-transport-start"),
                    settings={
                        "request_timeout": 1,
                        "retries": 0,
                        "source_unit_transport_started_observer": (
                            lambda *_args: events.append("transport")
                        ),
                    },
                    source_observer=(
                        lambda _nodes, _report: events.append("ingress") or False
                    ),
                )

        nodes, _report = asyncio.run(scenario())

        self.assertEqual([node.name for node in nodes], ["transport-start"])
        self.assertEqual(events[:2], ["transport", "ingress"])

    def test_source_transport_broker_rotates_roots_before_reusing_a_route(self) -> None:
        async def scenario() -> list[str]:
            broker = _Tier3SourceUnitTransportBroker(1)
            holder_ready = asyncio.Event()
            release_holder = asyncio.Event()
            starts: list[str] = []

            async def holder() -> None:
                async with broker.lease("holder", "route"):
                    holder_ready.set()
                    await release_holder.wait()

            async def use(root: str, label: str) -> None:
                async with broker.lease(root, "route"):
                    starts.append(label)
                    await asyncio.sleep(0)

            holding = asyncio.create_task(holder())
            await holder_ready.wait()
            queued = [
                asyncio.create_task(use("root-a", "a-1")),
                asyncio.create_task(use("root-a", "a-2")),
                asyncio.create_task(use("root-b", "b-1")),
            ]
            await asyncio.sleep(0)
            release_holder.set()
            await asyncio.gather(holding, *queued)
            return starts

        self.assertEqual(asyncio.run(scenario()), ["a-1", "b-1", "a-2"])

    def test_large_source_parse_work_uses_bounded_parallelism_across_units(self) -> None:
        active = 0
        peak = 0
        lock = threading.Lock()

        class FakeSource:
            source_type = "subscription_url"
            origin = "tier3b"

            def __init__(self, index: int) -> None:
                self.index = index
                self.value = f"https://source.example/{index}"
                self.source_id = f"subscription_url:{self.value}"

            def metadata(self):
                return {
                    "id": self.source_id,
                    "type": self.source_type,
                    "value": self.value,
                    "origin": self.origin,
                    "tags": [],
                }

            async def fetch_units(self, *_args, **_kwargs):
                payload = base64.urlsafe_b64encode(
                    f"aes-256-gcm:pass@node-{self.index}.example:8388".encode()
                ).decode("ascii").rstrip("=")
                yield SourceUnitResult(
                    root_id=self.source_id,
                    root_type=self.source_type,
                    root_value=self.value,
                    origin=self.origin,
                    unit_id="default",
                    unit_value=self.value,
                    unit_family="default",
                    ok=True,
                    content=f"ss://{payload}#node-{self.index}",
                )

        def measured_parse(*_args, **_kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.05)
                return parse_content_blob(*_args, **_kwargs)
            finally:
                with lock:
                    active -= 1

        sources = [FakeSource(index) for index in range(4)]

        async def scenario():
            with patch("collector.build_source", side_effect=sources):
                return await collect_nodes_from_sources_with_report(
                    [
                        {"type": "subscription_url", "value": source.value}
                        for source in sources
                    ],
                    tier="tier3",
                    logger=logging.getLogger("test-collector-parse-slots"),
                    settings={
                        "request_timeout": 1,
                        "retries": 0,
                        "source_root_concurrency": 4,
                        "source_parse_concurrency": 2,
                    },
                )

        with patch("collector.parse_content_blob", side_effect=measured_parse):
            nodes, _report = asyncio.run(scenario())

        self.assertEqual(len(nodes), 4)
        self.assertEqual(peak, 2)

    def test_compact_source_unit_keeps_a_parse_slot_ahead_of_bulk_queue(self) -> None:
        bulk_started = 0
        peak_bulk = 0
        bulk_finished = threading.Event()
        release_bulk = threading.Event()
        two_bulk_started = threading.Event()
        priority_started = threading.Event()
        priority_started_before_bulk_finished = False
        lock = threading.Lock()

        class FakeSource:
            source_type = "subscription_url"
            origin = "tier3b"

            def __init__(self, name: str, *, priority: bool) -> None:
                self.name = name
                self.priority = priority
                self.value = f"https://source.example/{name}"
                self.source_id = f"subscription_url:{self.value}"

            def metadata(self):
                return {
                    "id": self.source_id,
                    "type": self.source_type,
                    "value": self.value,
                    "origin": self.origin,
                    "tags": [],
                }

            async def fetch_units(self, *_args, **_kwargs):
                if self.priority:
                    deadline = time.monotonic() + 0.5
                    while not two_bulk_started.is_set() and time.monotonic() < deadline:
                        await asyncio.sleep(0.001)
                    # Give already-created bulk root tasks a chance to occupy
                    # the old shared bulkhead. The priority lane must still
                    # start before any of them is released.
                    await asyncio.sleep(0.02)
                    content = "ss://priority"
                else:
                    # Small in bytes but expensive in candidate count. This
                    # catches aggregate protocol files that the old byte-only
                    # threshold misclassified as compact work.
                    content = "\n".join("ss://bulk" for _ in range(500))
                yield SourceUnitResult(
                    root_id=self.source_id,
                    root_type=self.source_type,
                    root_value=self.value,
                    origin=self.origin,
                    unit_id="default",
                    unit_value=self.value,
                    unit_family="default",
                    ok=True,
                    content=content,
                )

        def measured_parse(_content, *, source, tier, logger, **_kwargs):
            nonlocal bulk_started, peak_bulk, priority_started_before_bulk_finished
            is_priority = source.endswith("/priority")
            if is_priority:
                priority_started_before_bulk_finished = not bulk_finished.is_set()
                priority_started.set()
                release_bulk.set()
            else:
                with lock:
                    bulk_started += 1
                    peak_bulk = max(peak_bulk, bulk_started)
                    if bulk_started >= 2:
                        two_bulk_started.set()
                release_bulk.wait(timeout=0.75)
                bulk_finished.set()
                with lock:
                    bulk_started -= 1
            suffix = source.rsplit("/", 1)[-1]
            payload = base64.urlsafe_b64encode(
                f"aes-256-gcm:pass@{suffix}.example:8388".encode()
            ).decode("ascii").rstrip("=")
            return parse_content_blob(
                f"ss://{payload}#{suffix}",
                source=source,
                tier=tier,
                logger=logger,
            )

        sources = [
            FakeSource("bulk-a", priority=False),
            FakeSource("bulk-b", priority=False),
            FakeSource("bulk-c", priority=False),
            FakeSource("priority", priority=True),
        ]

        async def scenario():
            with patch("collector.build_source", side_effect=sources):
                return await collect_nodes_from_sources_with_report(
                    [
                        {"type": "subscription_url", "value": source.value}
                        for source in sources
                    ],
                    tier="tier3",
                    logger=logging.getLogger("test-collector-priority-parse-slot"),
                    settings={
                        "request_timeout": 1,
                        "retries": 0,
                        "source_root_concurrency": 4,
                        "source_parse_concurrency": 3,
                        "source_bulk_parse_concurrency": 2,
                        "source_priority_parse_candidate_limit": 384,
                    },
                )

        with patch("collector.parse_content_blob", side_effect=measured_parse):
            nodes, _report = asyncio.run(scenario())

        self.assertEqual(len(nodes), 4)
        self.assertTrue(priority_started.is_set())
        self.assertTrue(priority_started_before_bulk_finished)
        self.assertLessEqual(peak_bulk, 2)

    def test_default_bulk_parse_lane_is_serialized(self) -> None:
        active = 0
        peak = 0
        lock = threading.Lock()

        class FakeSource:
            source_type = "subscription_url"
            origin = "tier3b"

            def __init__(self, index: int) -> None:
                self.index = index
                self.value = f"https://source.example/bulk-{index}"
                self.source_id = f"subscription_url:{self.value}"

            def metadata(self):
                return {
                    "id": self.source_id,
                    "type": self.source_type,
                    "value": self.value,
                    "origin": self.origin,
                    "tags": [],
                }

            async def fetch_units(self, *_args, **_kwargs):
                yield SourceUnitResult(
                    root_id=self.source_id,
                    root_type=self.source_type,
                    root_value=self.value,
                    origin=self.origin,
                    unit_id="default",
                    unit_value=self.value,
                    unit_family="aggregate",
                    ok=True,
                    content="\n".join("ss://bulk" for _ in range(500)),
                )

        def measured_parse(_content, *, source, tier, logger, **_kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.03)
                payload = base64.urlsafe_b64encode(
                    (
                        "aes-256-gcm:pass@"
                        f"{source.rsplit('/', 1)[-1]}.example:8388"
                    ).encode()
                ).decode("ascii").rstrip("=")
                return parse_content_blob(
                    f"ss://{payload}",
                    source=source,
                    tier=tier,
                    logger=logger,
                )
            finally:
                with lock:
                    active -= 1

        sources = [FakeSource(index) for index in range(3)]

        async def scenario():
            with patch("collector.build_source", side_effect=sources):
                return await collect_nodes_from_sources_with_report(
                    [
                        {"type": "subscription_url", "value": source.value}
                        for source in sources
                    ],
                    tier="tier3",
                    logger=logging.getLogger("test-collector-bulk-parse-lane"),
                    settings={
                        "request_timeout": 1,
                        "retries": 0,
                        "source_root_concurrency": 3,
                        "source_parse_concurrency": 3,
                        "source_priority_parse_candidate_limit": 384,
                    },
                )

        with patch("collector.parse_content_blob", side_effect=measured_parse):
            nodes, _report = asyncio.run(scenario())

        self.assertEqual(len(nodes), 3)
        self.assertEqual(peak, 1)

    def test_cancelled_snapshot_is_not_written(self) -> None:
        cancel_event = threading.Event()
        cancel_event.set()
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "cancelled.json"
            snapshot = PoolSnapshot(generated_at="2026-07-27T00:00:00Z", nodes=[])

            written = save_snapshot(snapshot, output, cancel_event=cancel_event)

            self.assertFalse(written)
            self.assertFalse(output.exists())

    def test_source_failures_emit_one_structured_aggregate_and_keep_units_raw_only(self) -> None:
        class FakeSource:
            source_type = "subscription_url"
            origin = "tier3b"

            def __init__(self, value: str) -> None:
                self.value = value
                self.source_id = f"subscription_url:{value}"

            def metadata(self):
                return {
                    "id": self.source_id,
                    "type": self.source_type,
                    "value": self.value,
                    "origin": self.origin,
                    "tags": [],
                }

            async def fetch(self, *_args, **_kwargs):
                return SourceFetchResult(
                    self.source_id,
                    self.source_type,
                    self.value,
                    self.origin,
                    ok=False,
                    error="connection refused",
                )

        class CaptureHandler(logging.Handler):
            def __init__(self) -> None:
                super().__init__()
                self.records: list[logging.LogRecord] = []

            def emit(self, record: logging.LogRecord) -> None:
                self.records.append(record)

        logger = logging.Logger("test-collector-aggregate", level=logging.DEBUG)
        handler = CaptureHandler()
        logger.addHandler(handler)

        async def collect():
            with patch(
                "collector.build_source",
                side_effect=lambda entry, **_kwargs: FakeSource(str(entry["value"])),
            ):
                return await collect_nodes_from_sources_with_report(
                    [
                        {"type": "subscription_url", "value": "source-a"},
                        {"type": "subscription_url", "value": "source-b"},
                    ],
                    tier="tier3",
                    logger=logger,
                    settings={"request_timeout": 1, "retries": 0},
                )

        nodes, report = asyncio.run(collect())

        self.assertEqual(nodes, [])
        self.assertEqual(report.failed_sources, 2)
        aggregate = [
            record
            for record in handler.records
            if getattr(record, "event_key", "") == "update.sources_failed"
        ]
        self.assertEqual(len(aggregate), 1)
        self.assertEqual(aggregate[0].event_params["source_total"], 2)
        self.assertEqual(aggregate[0].event_params["failed_count"], 2)
        unit_failures = [
            record for record in handler.records if record.getMessage().startswith("Collection failed")
        ]
        self.assertEqual(len(unit_failures), 2)
        self.assertTrue(all(getattr(record, "is_key_event", None) is False for record in unit_failures))

    def test_canonical_source_id_deduplicates_alias_fetches_and_width(self) -> None:
        fetch_count = 0

        class FakeSource:
            value = "https://t.me/s/V2RayRootFree"
            source_type = "telegram_channel"
            origin = "bootstrap"
            source_id = "telegram_channel:https://t.me/s/V2RayRootFree"

            def metadata(self):
                return {
                    "id": self.source_id,
                    "type": self.source_type,
                    "value": self.value,
                    "origin": self.origin,
                    "tags": [],
                }

            async def fetch(self, *_args, **_kwargs):
                nonlocal fetch_count
                fetch_count += 1
                return SourceFetchResult(
                    self.source_id,
                    self.source_type,
                    self.value,
                    self.origin,
                    ok=True,
                    content="",
                )

        async def collect():
            with patch("collector.build_source", side_effect=lambda *_args, **_kwargs: FakeSource()):
                return await collect_nodes_from_sources_with_report(
                    [
                        {"type": "telegram_channel", "value": "@V2RayRootFree"},
                        {
                            "type": "telegram_channel",
                            "value": "https://t.me/s/V2RayRootFree",
                        },
                    ],
                    tier="tier3",
                    logger=logging.getLogger("test-collector"),
                    settings={"request_timeout": 1, "retries": 0},
                )

        _nodes, report = asyncio.run(collect())

        self.assertEqual(fetch_count, 1)
        self.assertEqual(report.source_count, 1)
        self.assertEqual(report.configured_source_count, 1)

    def test_proxy_url_from_config_encodes_credentials(self) -> None:
        proxy_url = proxy_url_from_config(
            {
                "enabled": True,
                "scheme": "http",
                "server": "proxy.example.com",
                "port": 8004,
                "username": "user name",
                "password": "p@ss:word",
                "use_for": {"collection": True},
            }
        )

        self.assertEqual(proxy_url, "http://user%20name:p%40ss%3Aword@proxy.example.com:8004")

    def test_proxy_url_from_config_respects_use_for(self) -> None:
        proxy_url = proxy_url_from_config(
            {
                "enabled": True,
                "server": "proxy.example.com",
                "port": 8004,
                "use_for": {"collection": False},
            }
        )

        self.assertIsNone(proxy_url)

    def test_proxy_url_from_config_treats_string_false_as_disabled(self) -> None:
        self.assertIsNone(
            proxy_url_from_config(
                {
                    "enabled": "false",
                    "server": "proxy.example.com",
                    "port": 8004,
                }
            )
        )
        self.assertIsNone(
            proxy_url_from_config(
                {
                    "enabled": "true",
                    "server": "proxy.example.com",
                    "port": 8004,
                    "use_for": {"collection": "false"},
                }
            )
        )

    def test_runtime_source_fetch_proxy_retries_network_failures_only_for_source_fetch(self) -> None:
        class FakeSource:
            value = "https://source.example/sub"
            source_type = "subscription_url"
            origin = "tier3b"

            def __init__(self) -> None:
                self.seen_proxy_urls: list[str | None] = []

            @property
            def source_id(self):
                return f"subscription_url:{self.value}"

            def metadata(self):
                return {"type": self.source_type, "value": self.value, "origin": self.origin, "tags": []}

            async def fetch(self, *args, **kwargs):
                import base64

                proxy_url = kwargs.get("proxy_url")
                self.seen_proxy_urls.append(proxy_url)
                if proxy_url is None:
                    return SourceFetchResult(
                        self.source_id,
                        self.source_type,
                        self.value,
                        self.origin,
                        ok=False,
                        error="Cannot connect to host source.example:443",
                    )
                payload = base64.urlsafe_b64encode(
                    b"aes-256-gcm:pass@runtime-fetched.example.com:8388"
                ).decode("ascii").rstrip("=")
                return SourceFetchResult(
                    self.source_id,
                    self.source_type,
                    self.value,
                    self.origin,
                    ok=True,
                    content=f"ss://{payload}#runtime-fetched",
                )

        fake_source = FakeSource()

        async def collect():
            with patch("collector.build_source", return_value=fake_source):
                return await collect_nodes_from_sources_with_report(
                    [{"type": "url", "value": fake_source.value}],
                    tier="tier3",
                    logger=logging.getLogger("test-collector"),
                    settings={
                        "request_timeout": 1,
                        "retries": 0,
                        "runtime_source_fetch_proxy_url": "http://127.0.0.1:7890",
                    },
                )

        nodes, report = asyncio.run(collect())
        payload = report.to_dict()

        self.assertEqual([node.name for node in nodes], ["runtime-fetched"])
        self.assertEqual(fake_source.seen_proxy_urls, [None, "http://127.0.0.1:7890"])
        self.assertEqual(payload["source_fetch_egress_mode"], "runtime_proxy")
        self.assertEqual(payload["runtime_proxy_retry_count"], 1)
        self.assertEqual(payload["runtime_proxy_retry_success_count"], 1)
        self.assertEqual(payload["retryable_failed_sources"], 0)
        self.assertEqual(payload["source_retry_due_count"], 0)

    def test_runtime_source_fetch_proxy_first_marks_retry_metadata(self) -> None:
        class FakeSource:
            value = "https://source.example/sub"
            source_type = "subscription_url"
            origin = "tier3b"

            def __init__(self) -> None:
                self.seen_proxy_urls: list[str | None] = []

            @property
            def source_id(self):
                return f"subscription_url:{self.value}"

            def metadata(self):
                return {"type": self.source_type, "value": self.value, "origin": self.origin, "tags": []}

            async def fetch(self, *args, **kwargs):
                import base64

                proxy_url = kwargs.get("proxy_url")
                self.seen_proxy_urls.append(proxy_url)
                payload = base64.urlsafe_b64encode(
                    b"aes-256-gcm:pass@runtime-first.example.com:8388"
                ).decode("ascii").rstrip("=")
                return SourceFetchResult(
                    self.source_id,
                    self.source_type,
                    self.value,
                    self.origin,
                    ok=True,
                    content=f"ss://{payload}#runtime-first",
                )

        fake_source = FakeSource()

        async def collect():
            with patch("collector.build_source", return_value=fake_source):
                return await collect_nodes_from_sources_with_report(
                    [{"type": "url", "value": fake_source.value}],
                    tier="tier3",
                    logger=logging.getLogger("test-collector"),
                    settings={
                        "request_timeout": 1,
                        "retries": 0,
                        "runtime_source_fetch_proxy_url": "http://127.0.0.1:7890",
                        "runtime_source_fetch_proxy_first": True,
                    },
                )

        nodes, report = asyncio.run(collect())
        payload = report.to_dict()

        self.assertEqual([node.name for node in nodes], ["runtime-first"])
        self.assertEqual(fake_source.seen_proxy_urls, ["http://127.0.0.1:7890"])
        self.assertEqual(payload["source_fetch_egress_mode"], "runtime_proxy")
        self.assertEqual(payload["runtime_proxy_retry_count"], 1)
        self.assertEqual(payload["runtime_proxy_retry_success_count"], 1)

    def test_runtime_source_endpoint_race_publishes_only_the_parsed_winner(self) -> None:
        class FakeSource:
            value = "https://source.example/sub"
            source_type = "subscription_url"
            origin = "tier3b"

            def __init__(self) -> None:
                self.cancelled_routes: list[int] = []

            @property
            def source_id(self):
                return f"subscription_url:{self.value}"

            def metadata(self):
                return {
                    "id": self.source_id,
                    "type": self.source_type,
                    "value": self.value,
                    "origin": self.origin,
                    "tags": [],
                }

            async def fetch(self, *_args, **kwargs):
                proxy_url = str(kwargs.get("proxy_url") or "")
                port = 19302 if ":19302" in proxy_url else 19301
                try:
                    await asyncio.sleep(0.01 if port == 19302 else 0.30)
                except asyncio.CancelledError:
                    self.cancelled_routes.append(port)
                    raise
                encoded = base64.urlsafe_b64encode(
                    f"aes-256-gcm:pass@winner-{port}.example.com:8388".encode()
                ).decode("ascii").rstrip("=")
                return SourceFetchResult(
                    self.source_id,
                    self.source_type,
                    self.value,
                    self.origin,
                    ok=True,
                    content=f"ss://{encoded}#winner-{port}",
                )

        fake_source = FakeSource()
        credentials = RuntimeSourceListenerCredentials("tier3", "route-secret")
        endpoints = (
            RuntimeSourceFetchEndpoint(
                route_kind=RuntimeSourceRouteKind.FINAL_EXIT,
                listener_name="source-final-exit",
                host="127.0.0.1",
                port=19301,
                runtime_generation=3,
                config_generation=8,
                ready=True,
                priority=0,
                stagger_ms=0,
                _credentials=credentials,
            ),
            RuntimeSourceFetchEndpoint(
                route_kind=RuntimeSourceRouteKind.DIRECT,
                listener_name="source-direct",
                host="127.0.0.1",
                port=19302,
                runtime_generation=3,
                config_generation=8,
                ready=True,
                priority=1,
                stagger_ms=10,
                _credentials=credentials,
            ),
        )
        observed: list[list[str]] = []
        winners: list[tuple[str, str]] = []

        async def collect():
            with patch("collector.build_source", return_value=fake_source):
                return await collect_nodes_from_sources_with_report(
                    [{"type": "subscription_url", "value": fake_source.value}],
                    tier="tier3",
                    logger=logging.getLogger("test-collector-runtime-race"),
                    settings={
                        "request_timeout": 1,
                        "retries": 0,
                        "require_remote_source_route": True,
                        "runtime_source_fetch_endpoints_by_authority": {
                            fake_source.source_id: endpoints,
                        },
                        "runtime_source_fetch_route_success_observer": (
                            lambda authority, endpoint: winners.append(
                                (authority, endpoint.route_kind.value)
                            )
                        ),
                    },
                    source_observer=lambda nodes, _report: observed.append(
                        [node.name for node in nodes]
                    ),
                )

        nodes, report = asyncio.run(collect())

        self.assertEqual([node.name for node in nodes], ["winner-19302"])
        self.assertEqual(observed, [["winner-19302"]])
        self.assertEqual(winners, [(fake_source.source_id, "direct")])
        self.assertEqual(
            report.sources[0]["runtime_source_route_winner"],
            "direct",
        )
        self.assertEqual(
            nodes[0].extra["tier3_source_fetch_route_kind"],
            "direct",
        )
        self.assertIn(19301, fake_source.cancelled_routes)
        self.assertNotIn("route-secret", str(report.to_dict()))
        self.assertNotIn("route-secret", repr(nodes[0].extra))

    def test_runtime_source_endpoint_race_rejects_faster_unparseable_content(self) -> None:
        class FakeSource:
            value = "https://source.example/sub"
            source_type = "subscription_url"
            origin = "tier3b"

            @property
            def source_id(self):
                return f"subscription_url:{self.value}"

            def metadata(self):
                return {
                    "id": self.source_id,
                    "type": self.source_type,
                    "value": self.value,
                    "origin": self.origin,
                    "tags": [],
                }

            async def fetch(self, *_args, **kwargs):
                proxy_url = str(kwargs.get("proxy_url") or "")
                if ":19311" in proxy_url:
                    await asyncio.sleep(0.005)
                    content = "HTTP fetch succeeded but no node URI is present"
                else:
                    await asyncio.sleep(0.03)
                    payload = base64.urlsafe_b64encode(
                        b"aes-256-gcm:pass@parsed-winner.example.com:8388"
                    ).decode("ascii").rstrip("=")
                    content = f"ss://{payload}#parsed-winner"
                return SourceFetchResult(
                    self.source_id,
                    self.source_type,
                    self.value,
                    self.origin,
                    ok=True,
                    content=content,
                )

        fake_source = FakeSource()
        credentials = RuntimeSourceListenerCredentials("tier3", "route-secret")
        endpoints = (
            RuntimeSourceFetchEndpoint(
                route_kind=RuntimeSourceRouteKind.FINAL_EXIT,
                listener_name="source-final-exit",
                host="127.0.0.1",
                port=19311,
                runtime_generation=3,
                config_generation=8,
                ready=True,
                priority=0,
                stagger_ms=0,
                _credentials=credentials,
            ),
            RuntimeSourceFetchEndpoint(
                route_kind=RuntimeSourceRouteKind.DIRECT,
                listener_name="source-direct",
                host="127.0.0.1",
                port=19312,
                runtime_generation=3,
                config_generation=8,
                ready=True,
                priority=1,
                stagger_ms=0,
                _credentials=credentials,
            ),
        )
        observed: list[list[str]] = []
        failures: list[tuple[object, ...]] = []

        async def collect():
            with patch("collector.build_source", return_value=fake_source):
                return await collect_nodes_from_sources_with_report(
                    [{"type": "subscription_url", "value": fake_source.value}],
                    tier="tier3",
                    logger=logging.getLogger("test-collector-runtime-parse-race"),
                    settings={
                        "request_timeout": 1,
                        "retries": 0,
                        "require_remote_source_route": True,
                        "runtime_source_fetch_endpoints_by_authority": {
                            fake_source.source_id: endpoints,
                        },
                        "runtime_source_fetch_route_failure_observer": (
                            lambda *args: failures.append(args)
                        ),
                    },
                    source_observer=lambda nodes, _report: observed.append(
                        [node.name for node in nodes]
                    ),
                )

        nodes, report = asyncio.run(collect())

        self.assertEqual([node.name for node in nodes], ["parsed-winner"])
        self.assertEqual(observed, [["parsed-winner"]])
        self.assertEqual(
            report.sources[0]["runtime_source_route_winner"],
            "direct",
        )
        # A race that has a winner is not a route failure, but the losing
        # lane's reason stays visible without any raw error text.
        self.assertEqual(failures, [])
        self.assertEqual(
            report.sources[0]["runtime_source_route_errors"],
            ["final_exit:rejected:unsupported_content"],
        )

    def test_runtime_source_endpoint_race_preserves_retry_when_all_leases_expire(self) -> None:
        class FakeSource:
            value = "https://source.example/sub"
            source_type = "subscription_url"
            origin = "tier3b"

            def __init__(self) -> None:
                self.fetch_count = 0

            @property
            def source_id(self):
                return f"subscription_url:{self.value}"

            def metadata(self):
                return {
                    "id": self.source_id,
                    "type": self.source_type,
                    "value": self.value,
                    "origin": self.origin,
                    "tags": [],
                }

            async def fetch(self, *_args, **_kwargs):
                self.fetch_count += 1
                raise AssertionError("expired source endpoint must not dispatch")

        fake_source = FakeSource()
        credentials = RuntimeSourceListenerCredentials("tier3", "route-secret")
        endpoints = tuple(
            RuntimeSourceFetchEndpoint(
                route_kind=route,
                listener_name=f"source-{route.value.replace('_', '-')}",
                host="127.0.0.1",
                port=port,
                runtime_generation=3,
                config_generation=8,
                ready=True,
                priority=index,
                stagger_ms=0,
                _credentials=credentials,
            )
            for index, (route, port) in enumerate(
                (
                    (RuntimeSourceRouteKind.FINAL_EXIT, 19321),
                    (RuntimeSourceRouteKind.DIRECT, 19322),
                )
            )
        )

        async def collect():
            with patch("collector.build_source", return_value=fake_source):
                return await collect_nodes_from_sources_with_report(
                    [{"type": "subscription_url", "value": fake_source.value}],
                    tier="tier3",
                    logger=logging.getLogger("test-collector-runtime-stale-race"),
                    settings={
                        "request_timeout": 1,
                        "retries": 0,
                        "require_remote_source_route": True,
                        "source_supply_phase": "core",
                        "runtime_source_fetch_endpoints_by_authority": {
                            fake_source.source_id: endpoints,
                        },
                        "runtime_source_fetch_endpoint_lease_validator": (
                            lambda _phase, _authority, _endpoint: False
                        ),
                        "runtime_source_fetch_route_failure_observer": (
                            lambda *args: failures.append(args)
                        ),
                    },
                )

        failures: list[tuple[object, ...]] = []
        nodes, report = asyncio.run(collect())
        payload = report.to_dict()

        self.assertEqual(nodes, [])
        self.assertEqual(fake_source.fetch_count, 0)
        self.assertEqual(payload["configured_source_count"], 1)
        self.assertEqual(payload["source_fetch_failed"], 0)
        self.assertEqual(payload["source_fetch_pending"], 1)
        self.assertEqual(payload["runtime_proxy_retry_count"], 0)
        self.assertEqual(payload["source_retry_due_count"], 1)
        self.assertTrue(
            report.sources[0]["runtime_source_route_dispatch_deferred"]
        )
        self.assertNotIn("route-secret", str(payload))
        # Expired leases never dispatched: that is route revalidation, not a
        # lane failure, so the remembered lane must not be forgotten.
        self.assertEqual(failures, [])
        self.assertCountEqual(
            report.sources[0]["runtime_source_route_errors"],
            ["final_exit:deferred", "direct:deferred"],
        )

    def test_runtime_source_endpoint_race_without_winner_records_lanes_not_first_lane(
        self,
    ) -> None:
        class FakeSource:
            value = "https://source.example/sub"
            source_type = "subscription_url"
            origin = "tier3b"

            @property
            def source_id(self):
                return f"subscription_url:{self.value}"

            def metadata(self):
                return {
                    "id": self.source_id,
                    "type": self.source_type,
                    "value": self.value,
                    "origin": self.origin,
                    "tags": [],
                }

            async def fetch(self, *_args, **kwargs):
                proxy_url = str(kwargs.get("proxy_url") or "")
                if ":19351" in proxy_url:
                    # The pool lane's exit breaks TLS for every site.
                    error = (
                        "Cannot connect to host source.example:443 ssl:default "
                        "[EOF occurred in violation of protocol]"
                    )
                else:
                    await asyncio.sleep(0.01)
                    error = "HTTP 404"
                return SourceFetchResult(
                    self.source_id,
                    self.source_type,
                    self.value,
                    self.origin,
                    ok=False,
                    error=error,
                )

        fake_source = FakeSource()
        credentials = RuntimeSourceListenerCredentials("tier3", "route-secret")
        endpoints = tuple(
            RuntimeSourceFetchEndpoint(
                route_kind=route,
                listener_name=f"source-{route.value.replace('_', '-')}",
                host="127.0.0.1",
                port=port,
                runtime_generation=3,
                config_generation=8,
                ready=True,
                priority=index,
                stagger_ms=0,
                _credentials=credentials,
            )
            for index, (route, port) in enumerate(
                (
                    (RuntimeSourceRouteKind.FINAL_EXIT, 19351),
                    (RuntimeSourceRouteKind.DIRECT, 19352),
                )
            )
        )
        winners: list[str] = []
        failures: list[tuple[str, list[str]]] = []

        async def collect():
            with patch("collector.build_source", return_value=fake_source):
                return await collect_nodes_from_sources_with_report(
                    [{"type": "subscription_url", "value": fake_source.value}],
                    tier="tier3",
                    logger=logging.getLogger("test-collector-runtime-no-winner"),
                    settings={
                        "request_timeout": 1,
                        "retries": 0,
                        "require_remote_source_route": True,
                        "runtime_source_fetch_endpoints_by_authority": {
                            fake_source.source_id: endpoints,
                        },
                        "runtime_source_fetch_route_success_observer": (
                            lambda _authority, endpoint: winners.append(
                                endpoint.route_kind.value
                            )
                        ),
                        "runtime_source_fetch_route_failure_observer": (
                            lambda authority, failed: failures.append(
                                (
                                    authority,
                                    [endpoint.route_kind.value for endpoint in failed],
                                )
                            )
                        ),
                    },
                )

        nodes, report = asyncio.run(collect())
        source = report.sources[0]
        unit = source["units"][0]

        self.assertEqual(nodes, [])
        self.assertEqual(winners, [])
        self.assertEqual(
            failures,
            [(fake_source.source_id, ["final_exit", "direct"])],
        )
        self.assertEqual(source["runtime_source_route_winner"], "")
        self.assertCountEqual(
            source["runtime_source_routes_attempted"],
            ["final_exit", "direct"],
        )
        self.assertCountEqual(
            source["runtime_source_route_errors"],
            ["final_exit:rejected:connection", "direct:rejected:http_404"],
        )
        self.assertNotIn("source.example", str(source["runtime_source_route_errors"]))
        # The file is not credited to the first lane that dispatched: the
        # server's 404 seen by DIRECT outranks FINAL-EXIT's transport failure,
        # and the unit names the attempted lanes instead of one route.
        self.assertEqual(unit["error"], "HTTP 404")
        self.assertFalse(unit["retryable"])
        self.assertEqual(unit["runtime_source_route_kind"], "")
        self.assertCountEqual(
            unit["runtime_source_routes_attempted"],
            ["final_exit", "direct"],
        )
        self.assertFalse(source["retryable"])
        self.assertNotIn("route-secret", str(report.to_dict()))

    def test_runtime_source_race_without_winner_keeps_lane_order_for_lane_local_errors(
        self,
    ) -> None:
        # Only a fact about the file (a body, an empty body, 404/410) may
        # replace the first lane's answer.  A 5xx, 429, 403, a proxy's own
        # 502 or a dropped connection can be lane-local; letting it win made
        # a source terminal that the first lane's timeout kept retryable.
        timeout = "source unit timeout"
        proxy_502 = (
            "502, message='Bad Gateway', url='http://source.example/sub'"
        )
        cases = (
            ("proxy_502", {19351: timeout, 19352: proxy_502, 19353: timeout}, timeout, True),
            ("rate_limited", {19351: timeout, 19353: "429, message='Too Many Requests'"}, timeout, True),
            ("forbidden", {19351: timeout, 19353: "403, message='Forbidden'"}, timeout, True),
            ("disconnected", {19351: timeout, 19353: "Server disconnected"}, timeout, True),
            ("gone", {19351: timeout, 19353: "410, message='Gone'"}, "410, message='Gone'", False),
            ("empty_body", {19351: timeout, 19353: "empty"}, "empty", False),
        )
        lanes = {
            19351: RuntimeSourceRouteKind.FINAL_EXIT,
            19352: RuntimeSourceRouteKind.EGRESS,
            19353: RuntimeSourceRouteKind.DIRECT,
        }
        for label, errors_by_port, expected_error, expected_retryable in cases:
            with self.subTest(label):

                class FakeSource:
                    value = "https://source.example/sub"
                    source_type = "subscription_url"
                    origin = "tier3b"

                    @property
                    def source_id(self):
                        return f"subscription_url:{self.value}"

                    def metadata(self):
                        return {
                            "id": self.source_id,
                            "type": self.source_type,
                            "value": self.value,
                            "origin": self.origin,
                            "tags": [],
                        }

                    async def fetch(self, *_args, **kwargs):
                        proxy_url = str(kwargs.get("proxy_url") or "")
                        port = next(
                            port for port in errors_by_port if f":{port}" in proxy_url
                        )
                        if port != 19351:
                            # A later lane answers after the first one failed.
                            await asyncio.sleep(0.01)
                        return SourceFetchResult(
                            self.source_id,
                            self.source_type,
                            self.value,
                            self.origin,
                            ok=False,
                            error=errors_by_port[port],
                        )

                fake_source = FakeSource()
                credentials = RuntimeSourceListenerCredentials("tier3", "route-secret")
                endpoints = tuple(
                    RuntimeSourceFetchEndpoint(
                        route_kind=lanes[port],
                        listener_name=f"source-{lanes[port].value.replace('_', '-')}",
                        host="127.0.0.1",
                        port=port,
                        runtime_generation=3,
                        config_generation=8,
                        ready=True,
                        priority=index,
                        stagger_ms=0,
                        _credentials=credentials,
                    )
                    for index, port in enumerate(errors_by_port)
                )

                async def collect():
                    with patch("collector.build_source", return_value=fake_source):
                        return await collect_nodes_from_sources_with_report(
                            [{"type": "subscription_url", "value": fake_source.value}],
                            tier="tier3",
                            logger=logging.getLogger(
                                "test-collector-runtime-no-winner-lane-order"
                            ),
                            settings={
                                "request_timeout": 1,
                                "retries": 0,
                                "require_remote_source_route": True,
                                "runtime_source_fetch_endpoints_by_authority": {
                                    fake_source.source_id: endpoints,
                                },
                            },
                        )

                nodes, report = asyncio.run(collect())
                source = report.sources[0]
                unit = source["units"][0]

                self.assertEqual(nodes, [])
                self.assertEqual(source["runtime_source_route_winner"], "")
                self.assertEqual(unit["error"], expected_error)
                self.assertIs(unit["retryable"], expected_retryable)
                self.assertIs(bool(source["retryable"]), expected_retryable)

    def test_runtime_source_endpoint_race_closes_every_non_winning_iterator(self) -> None:
        class FakeSource:
            value = "https://source.example/sub"
            source_type = "subscription_url"
            origin = "tier3b"

            def __init__(self) -> None:
                self.started: set[int] = set()
                self.closed: list[int] = []
                self.both_started = asyncio.Event()

            @property
            def source_id(self):
                return f"subscription_url:{self.value}"

            def metadata(self):
                return {
                    "id": self.source_id,
                    "type": self.source_type,
                    "value": self.value,
                    "origin": self.origin,
                    "tags": [],
                }

            async def fetch_units(self, *_args, **kwargs):
                proxy_url = str(kwargs.get("proxy_url") or "")
                port = 19332 if ":19332" in proxy_url else 19331
                self.started.add(port)
                if len(self.started) == 2:
                    self.both_started.set()
                await self.both_started.wait()
                payload = base64.urlsafe_b64encode(
                    f"aes-256-gcm:pass@winner-{port}.example.com:8388".encode()
                ).decode("ascii").rstrip("=")
                try:
                    yield SourceUnitResult(
                        root_id=self.source_id,
                        root_type=self.source_type,
                        root_value=self.value,
                        origin=self.origin,
                        unit_id="default",
                        unit_value=self.value,
                        unit_family="default",
                        ok=True,
                        content=f"ss://{payload}#winner-{port}",
                    )
                    await asyncio.sleep(0)
                finally:
                    self.closed.append(port)

        fake_source = FakeSource()
        credentials = RuntimeSourceListenerCredentials("tier3", "route-secret")
        endpoints = tuple(
            RuntimeSourceFetchEndpoint(
                route_kind=route,
                listener_name=f"source-{route.value.replace('_', '-')}",
                host="127.0.0.1",
                port=port,
                runtime_generation=3,
                config_generation=8,
                ready=True,
                priority=index,
                stagger_ms=0,
                _credentials=credentials,
            )
            for index, (route, port) in enumerate(
                (
                    (RuntimeSourceRouteKind.FINAL_EXIT, 19331),
                    (RuntimeSourceRouteKind.DIRECT, 19332),
                )
            )
        )
        observed: list[list[str]] = []

        async def collect():
            with patch("collector.build_source", return_value=fake_source):
                return await collect_nodes_from_sources_with_report(
                    [{"type": "subscription_url", "value": fake_source.value}],
                    tier="tier3",
                    logger=logging.getLogger("test-collector-runtime-close-race"),
                    settings={
                        "request_timeout": 1,
                        "retries": 0,
                        "require_remote_source_route": True,
                        "runtime_source_fetch_endpoints_by_authority": {
                            fake_source.source_id: endpoints,
                        },
                    },
                    source_observer=lambda nodes, _report: observed.append(
                        [node.name for node in nodes]
                    ),
                )

        nodes, _report = asyncio.run(collect())

        self.assertEqual(len(nodes), 1)
        self.assertEqual(len(observed), 1)
        self.assertCountEqual(fake_source.closed, [19331, 19332])

    def test_runtime_source_endpoint_race_parses_identical_payload_once(self) -> None:
        class FakeSource:
            value = "https://source.example/large-sub"
            source_type = "subscription_url"
            origin = "tier3b"

            def __init__(self) -> None:
                self.started: set[int] = set()
                self.closed: list[int] = []
                self.both_started = asyncio.Event()

            @property
            def source_id(self):
                return f"subscription_url:{self.value}"

            def metadata(self):
                return {
                    "id": self.source_id,
                    "type": self.source_type,
                    "value": self.value,
                    "origin": self.origin,
                    "tags": [],
                }

            async def fetch_units(self, *_args, **kwargs):
                proxy_url = str(kwargs.get("proxy_url") or "")
                port = 19342 if ":19342" in proxy_url else 19341
                self.started.add(port)
                if len(self.started) == 2:
                    self.both_started.set()
                await self.both_started.wait()
                payload = base64.urlsafe_b64encode(
                    b"aes-256-gcm:pass@shared.example.com:8388"
                ).decode("ascii").rstrip("=")
                try:
                    yield SourceUnitResult(
                        root_id=self.source_id,
                        root_type=self.source_type,
                        root_value=self.value,
                        origin=self.origin,
                        unit_id="default",
                        unit_value=self.value,
                        unit_family="default",
                        ok=True,
                        content=f"ss://{payload}#shared-payload",
                    )
                    await asyncio.sleep(0)
                finally:
                    self.closed.append(port)

        fake_source = FakeSource()
        credentials = RuntimeSourceListenerCredentials("tier3", "route-secret")
        endpoints = tuple(
            RuntimeSourceFetchEndpoint(
                route_kind=route,
                listener_name=f"source-{route.value.replace('_', '-')}",
                host="127.0.0.1",
                port=port,
                runtime_generation=3,
                config_generation=8,
                ready=True,
                priority=index,
                stagger_ms=0,
                _credentials=credentials,
            )
            for index, (route, port) in enumerate(
                (
                    (RuntimeSourceRouteKind.FINAL_EXIT, 19341),
                    (RuntimeSourceRouteKind.DIRECT, 19342),
                )
            )
        )
        parse_calls = 0
        parse_lock = threading.Lock()

        def counted_parse(*args, **kwargs):
            nonlocal parse_calls
            with parse_lock:
                parse_calls += 1
            time.sleep(0.05)
            return parse_content_blob(*args, **kwargs)

        async def collect():
            with (
                patch("collector.build_source", return_value=fake_source),
                patch("collector.parse_content_blob", side_effect=counted_parse),
            ):
                return await collect_nodes_from_sources_with_report(
                    [{"type": "subscription_url", "value": fake_source.value}],
                    tier="tier3",
                    logger=logging.getLogger("test-collector-runtime-parse-once"),
                    settings={
                        "request_timeout": 1,
                        "retries": 0,
                        "require_remote_source_route": True,
                        "runtime_source_fetch_endpoints_by_authority": {
                            fake_source.source_id: endpoints,
                        },
                    },
                )

        nodes, _report = asyncio.run(collect())

        self.assertEqual(len(nodes), 1)
        self.assertEqual(parse_calls, 1)
        self.assertCountEqual(fake_source.closed, [19341, 19342])

    def test_unchanged_source_unit_skips_parse_and_preserves_success(self) -> None:
        payload = base64.urlsafe_b64encode(
            b"aes-256-gcm:pass@unchanged.example.com:8388"
        ).decode("ascii").rstrip("=")
        body = f"ss://{payload}#unchanged"

        class FakeSource:
            value = "https://source.example/unchanged"
            source_type = "subscription_url"
            origin = "tier3b"

            @property
            def source_id(self):
                return f"subscription_url:{self.value}"

            def metadata(self):
                return {
                    "id": self.source_id,
                    "type": self.source_type,
                    "value": self.value,
                    "origin": self.origin,
                    "tags": [],
                }

            async def fetch_units(self, *_args, **_kwargs):
                yield SourceUnitResult(
                    root_id=self.source_id,
                    root_type=self.source_type,
                    root_value=self.value,
                    origin=self.origin,
                    unit_id="default",
                    unit_value=self.value,
                    unit_family="default",
                    ok=True,
                    content=body,
                )

        fake_source = FakeSource()
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()

        async def collect():
            with (
                patch("collector.build_source", return_value=fake_source),
                patch("collector.parse_content_blob") as parse_mock,
            ):
                result = await collect_nodes_from_sources_with_report(
                    [{"type": "subscription_url", "value": fake_source.value}],
                    tier="tier3",
                    logger=logging.getLogger("test-collector-unchanged-unit"),
                    settings={
                        "request_timeout": 1,
                        "retries": 0,
                        "source_content_facts_by_authority": {
                            fake_source.source_id: {
                                "default": {
                                    "content_digest": digest,
                                    "node_count": 17,
                                }
                            }
                        },
                    },
                )
                return result, parse_mock.call_count

        (nodes, report), parse_calls = asyncio.run(collect())

        self.assertEqual(nodes, [])
        self.assertEqual(parse_calls, 0)
        self.assertEqual(report.fetched_sources, 1)
        unit = report.sources[0]["units"][0]
        self.assertTrue(unit["ok"])
        self.assertTrue(unit["content_unchanged"])
        self.assertEqual(unit["node_count"], 17)

    def test_cross_domain_redirect_keeps_every_hop_on_runtime_mixed_port(self) -> None:
        requested_targets: list[str] = []
        payload = base64.urlsafe_b64encode(
            b"aes-256-gcm:pass@redirected.example.com:8388"
        ).decode("ascii").rstrip("=")
        body = f"ss://{payload}#redirected-via-mixed".encode("utf-8")

        async def proxy_handler(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            try:
                request_line = (await reader.readline()).decode(
                    "ascii",
                    errors="replace",
                ).strip()
                while True:
                    header_line = await reader.readline()
                    if header_line in {b"", b"\r\n"}:
                        break
                target = request_line.split(" ", 2)[1]
                requested_targets.append(target)
                if target == "http://origin.invalid/start":
                    response = (
                        b"HTTP/1.1 302 Found\r\n"
                        b"Location: http://redirect.invalid/final\r\n"
                        b"Content-Length: 0\r\n"
                        b"Connection: close\r\n\r\n"
                    )
                else:
                    response = (
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: text/plain; charset=utf-8\r\n"
                        + f"Content-Length: {len(body)}\r\n".encode("ascii")
                        + b"Connection: close\r\n\r\n"
                        + body
                    )
                writer.write(response)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        async def collect():
            server = await asyncio.start_server(
                proxy_handler,
                "127.0.0.1",
                0,
            )
            port = int(server.sockets[0].getsockname()[1])
            try:
                return await collect_nodes_from_sources_with_report(
                    [
                        {
                            "type": "subscription_url",
                            "value": "http://origin.invalid/start",
                        }
                    ],
                    tier="tier3",
                    logger=logging.getLogger("test-collector-redirect-proxy"),
                    settings={
                        "request_timeout": 2,
                        "retries": 0,
                        "require_remote_source_route": True,
                        "runtime_source_fetch_proxy_url": (
                            f"http://127.0.0.1:{port}"
                        ),
                        "runtime_source_fetch_proxy_first": True,
                    },
                )
            finally:
                server.close()
                await server.wait_closed()

        nodes, report = asyncio.run(collect())
        report_payload = report.to_dict()

        self.assertEqual(
            requested_targets,
            [
                "http://origin.invalid/start",
                "http://redirect.invalid/final",
            ],
        )
        self.assertEqual([node.name for node in nodes], ["redirected-via-mixed"])
        self.assertEqual(report_payload["source_fetch_egress_mode"], "runtime_proxy")
        self.assertEqual(report_payload["runtime_proxy_retry_count"], 1)

    def test_runtime_source_fetch_proxy_is_not_used_without_runtime_proxy_setting(self) -> None:
        class FakeSource:
            value = "https://source.example/sub"
            source_type = "subscription_url"
            origin = "tier3b"

            def __init__(self) -> None:
                self.seen_proxy_urls: list[str | None] = []

            @property
            def source_id(self):
                return f"subscription_url:{self.value}"

            def metadata(self):
                return {"type": self.source_type, "value": self.value, "origin": self.origin, "tags": []}

            async def fetch(self, *args, **kwargs):
                self.seen_proxy_urls.append(kwargs.get("proxy_url"))
                return SourceFetchResult(
                    self.source_id,
                    self.source_type,
                    self.value,
                    self.origin,
                    ok=False,
                    error="Cannot connect to host source.example:443",
                )

        fake_source = FakeSource()

        async def collect():
            with patch("collector.build_source", return_value=fake_source):
                return await collect_nodes_from_sources_with_report(
                    [{"type": "url", "value": fake_source.value}],
                    tier="tier3",
                    logger=logging.getLogger("test-collector"),
                    settings={"request_timeout": 1, "retries": 0},
                )

        nodes, report = asyncio.run(collect())
        payload = report.to_dict()

        self.assertEqual(nodes, [])
        self.assertEqual(fake_source.seen_proxy_urls, [None])
        self.assertEqual(payload["source_fetch_egress_mode"], "direct")
        self.assertEqual(payload["runtime_proxy_retry_count"], 0)
        self.assertEqual(payload["retryable_failed_sources"], 1)
        self.assertEqual(payload["source_retry_due_count"], 1)

    def test_remote_source_is_blocked_without_an_authorized_route(self) -> None:
        class FakeSource:
            value = "https://source.example/sub"
            source_type = "subscription_url"
            origin = "tier3b"
            source_id = "subscription_url:https://source.example/sub"

            def __init__(self) -> None:
                self.fetch_calls = 0

            def metadata(self):
                return {
                    "id": self.source_id,
                    "type": self.source_type,
                    "value": self.value,
                    "origin": self.origin,
                    "tags": [],
                }

            async def fetch(self, *_args, **_kwargs):
                self.fetch_calls += 1
                raise AssertionError("blocked remote source must not start a request")

        source = FakeSource()

        async def collect():
            with patch("collector.build_source", return_value=source):
                return await collect_nodes_from_sources_with_report(
                    [{"type": "url", "value": source.value}],
                    tier="tier3",
                    logger=logging.getLogger("test-collector"),
                    settings={
                        "request_timeout": 1,
                        "retries": 0,
                        "require_remote_source_route": True,
                    },
                )

        nodes, report = asyncio.run(collect())
        payload = report.to_dict()

        self.assertEqual(nodes, [])
        self.assertEqual(source.fetch_calls, 0)
        self.assertEqual(payload["source_fetch_egress_mode"], "blocked_no_route")
        self.assertEqual(payload["retryable_failed_sources"], 0)
        self.assertEqual(payload["source_retry_due_count"], 0)
        self.assertFalse(payload["sources"][0]["retryable"])
        self.assertIn("blocked_no_route", payload["sources"][0]["error"])

    def test_local_file_source_remains_available_without_an_egress_route(self) -> None:
        class FakeLocalSource:
            value = "C:/fixtures/local-nodes.txt"
            source_type = "local_file"
            origin = "tier3b"
            source_id = "local_file:C:/fixtures/local-nodes.txt"

            def __init__(self) -> None:
                self.seen_proxy_urls: list[str | None] = []

            def metadata(self):
                return {
                    "id": self.source_id,
                    "type": self.source_type,
                    "value": self.value,
                    "origin": self.origin,
                    "tags": [],
                }

            async def fetch(self, *_args, **kwargs):
                import base64

                self.seen_proxy_urls.append(kwargs.get("proxy_url"))
                encoded = base64.urlsafe_b64encode(
                    b"aes-256-gcm:pass@local-file.example.com:8388"
                ).decode("ascii").rstrip("=")
                return SourceFetchResult(
                    self.source_id,
                    self.source_type,
                    self.value,
                    self.origin,
                    ok=True,
                    content=f"ss://{encoded}#local-file-node",
                )

        source = FakeLocalSource()

        async def collect():
            with patch("collector.build_source", return_value=source):
                return await collect_nodes_from_sources_with_report(
                    [{"type": "local_file", "value": source.value}],
                    tier="tier3",
                    logger=logging.getLogger("test-collector"),
                    settings={
                        "request_timeout": 1,
                        "retries": 0,
                        "require_remote_source_route": True,
                    },
                )

        nodes, report = asyncio.run(collect())

        self.assertEqual([node.name for node in nodes], ["local-file-node"])
        self.assertEqual(source.seen_proxy_urls, [None])
        self.assertEqual(report.failed_sources, 0)

    def test_explicit_egress_proxy_authorizes_remote_source_fetch(self) -> None:
        class FakeSource:
            value = "https://source.example/sub"
            source_type = "subscription_url"
            origin = "tier3b"
            source_id = "subscription_url:https://source.example/sub"

            def __init__(self) -> None:
                self.seen_proxy_urls: list[str | None] = []

            def metadata(self):
                return {
                    "id": self.source_id,
                    "type": self.source_type,
                    "value": self.value,
                    "origin": self.origin,
                    "tags": [],
                }

            async def fetch(self, *_args, **kwargs):
                self.seen_proxy_urls.append(kwargs.get("proxy_url"))
                return SourceFetchResult(
                    self.source_id,
                    self.source_type,
                    self.value,
                    self.origin,
                    ok=True,
                    content="",
                )

        source = FakeSource()

        async def collect():
            with patch("collector.build_source", return_value=source):
                return await collect_nodes_from_sources_with_report(
                    [{"type": "url", "value": source.value}],
                    tier="tier3",
                    logger=logging.getLogger("test-collector"),
                    settings={
                        "request_timeout": 1,
                        "retries": 0,
                        "require_remote_source_route": True,
                        "egress_proxy": {
                            "enabled": True,
                            "scheme": "http",
                            "server": "proxy.example.com",
                            "port": 8080,
                            "username": "route user",
                            "password": "p@ss",
                            "use_for": {"collection": True},
                        },
                    },
                )

        _nodes, report = asyncio.run(collect())

        self.assertEqual(
            source.seen_proxy_urls,
            ["http://route%20user:p%40ss@proxy.example.com:8080"],
        )
        self.assertEqual(report.sources[0]["egress_mode"], "configured_proxy")

    def test_load_snapshot_recovers_from_corrupt_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot_path = Path(temp_dir) / "nodes.json"
            snapshot_path.write_text('{"nodes": [', encoding="utf-8")

            snapshot = load_snapshot(snapshot_path)

            self.assertEqual(snapshot.nodes, [])
            self.assertEqual(snapshot.metadata.get("load_error"), "invalid_snapshot")

    def test_load_snapshot_retries_a_transient_replace_read(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot_path = Path(temp_dir) / "nodes.json"
            snapshot_path.write_text("{}", encoding="utf-8")
            payloads = iter(
                (
                    '{"nodes": [',
                    '{"generated_at":"ok","metadata":{"tier":"tier3"},"nodes":[]}',
                )
            )

            def open_snapshot(*_args, **_kwargs):
                return io.StringIO(next(payloads))

            with (
                patch.object(Path, "open", side_effect=open_snapshot),
                patch("collector.time.sleep") as sleep_mock,
            ):
                snapshot = load_snapshot(snapshot_path)

            self.assertEqual(snapshot.generated_at, "ok")
            self.assertEqual(snapshot.metadata, {"tier": "tier3"})
            sleep_mock.assert_called_once_with(0.01)

    def test_tier3_origin_metadata_is_only_written_for_tier3(self) -> None:
        class FakeSource:
            value = "memory://nodes"
            source_type = "subscription_url"
            origin = "tier3b"

            @property
            def source_id(self):
                return "subscription_url:memory://nodes"

            def metadata(self):
                return {"type": self.source_type, "value": self.value, "origin": self.origin, "tags": []}

            async def fetch(self, *args, **kwargs):
                return SourceFetchResult(
                    self.source_id,
                    self.source_type,
                    self.value,
                    self.origin,
                    ok=True,
                    content="ss://YWVzLTI1Ni1nY206cGFzc0BleGFtcGxlLmNvbTo4Mzg4#node",
                )

        async def collect(tier: str):
            with patch("collector.build_source", return_value=FakeSource()):
                return await collect_nodes_from_sources_with_report(
                    [{"type": "url", "value": "memory://nodes"}],
                    tier=tier,
                    logger=logging.getLogger("test-collector"),
                    settings={"request_timeout": 1, "retries": 0},
                )

        tier2_nodes, _tier2_report = asyncio.run(collect("tier2"))
        tier3_nodes, _tier3_report = asyncio.run(collect("tier3"))

        self.assertNotIn("tier3_origin", tier2_nodes[0].extra)
        self.assertNotIn("tier3_source_parsed_from_content", tier2_nodes[0].extra)
        self.assertTrue(tier2_nodes[0].extra["source_parsed_from_content"])
        self.assertEqual(tier2_nodes[0].extra["source_origin"], "tier3b")
        self.assertEqual(tier3_nodes[0].extra["tier3_origin"], "tier3b")
        self.assertTrue(tier3_nodes[0].extra["source_parsed_from_content"])
        self.assertTrue(tier3_nodes[0].extra["tier3_source_parsed_from_content"])

    def test_null_source_byte_cap_uses_safe_default(self) -> None:
        class FakeSource:
            value = "memory://nodes"
            source_type = "subscription_url"
            origin = "tier3b"
            seen_max_bytes: list[int] = []

            @property
            def source_id(self):
                return "subscription_url:memory://nodes"

            def metadata(self):
                return {"type": self.source_type, "value": self.value, "origin": self.origin, "tags": []}

            async def fetch(self, *args, **kwargs):
                self.seen_max_bytes.append(kwargs["max_bytes"])
                return SourceFetchResult(
                    self.source_id,
                    self.source_type,
                    self.value,
                    self.origin,
                    ok=True,
                    content="ss://YWVzLTI1Ni1nY206cGFzc0BleGFtcGxlLmNvbTo4Mzg4#node",
                )

        fake_source = FakeSource()

        async def collect():
            with patch("collector.build_source", return_value=fake_source):
                return await collect_nodes_from_sources_with_report(
                    [{"type": "url", "value": "memory://nodes"}],
                    tier="tier3",
                    logger=logging.getLogger("test-collector"),
                    settings={"request_timeout": 1, "retries": 0, "max_source_bytes": None},
                )

        nodes, _report = asyncio.run(collect())

        self.assertEqual(len(nodes), 1)
        self.assertEqual(fake_source.seen_max_bytes, [8 * 1024 * 1024])

    def test_invalid_collection_request_settings_use_safe_defaults(self) -> None:
        class FakeSource:
            value = "memory://nodes"
            source_type = "subscription_url"
            origin = "tier3b"
            seen_kwargs: list[dict[str, object]] = []

            @property
            def source_id(self):
                return "subscription_url:memory://nodes"

            def metadata(self):
                return {"type": self.source_type, "value": self.value, "origin": self.origin, "tags": []}

            async def fetch(self, *args, **kwargs):
                self.seen_kwargs.append(kwargs)
                return SourceFetchResult(
                    self.source_id,
                    self.source_type,
                    self.value,
                    self.origin,
                    ok=True,
                    content="ss://YWVzLTI1Ni1nY206cGFzc0BleGFtcGxlLmNvbTo4Mzg4#node",
                )

        fake_source = FakeSource()

        async def collect():
            with patch("collector.build_source", return_value=fake_source):
                return await collect_nodes_from_sources_with_report(
                    [{"type": "url", "value": "memory://nodes"}],
                    tier="tier3",
                    logger=logging.getLogger("test-collector"),
                    settings={
                        "request_timeout": "bad",
                        "retries": "bad",
                        "source_max_bytes": "bad",
                        "user_agents": "bad",
                    },
                )

        nodes, _report = asyncio.run(collect())

        self.assertEqual(len(nodes), 1)
        self.assertEqual(fake_source.seen_kwargs[0]["timeout_seconds"], 15)
        self.assertEqual(fake_source.seen_kwargs[0]["retries"], 2)
        self.assertEqual(fake_source.seen_kwargs[0]["max_bytes"], 8 * 1024 * 1024)
        self.assertIn(fake_source.seen_kwargs[0]["user_agent"], DEFAULT_USER_AGENTS)

    def test_source_observer_receives_fast_source_before_slow_source_finishes(self) -> None:
        class FakeSource:
            source_type = "subscription_url"
            origin = "tier3b"

            def __init__(self, value: str, delay: float, name: str) -> None:
                self.value = value
                self.delay = delay
                self.name = name

            @property
            def source_id(self):
                return f"subscription_url:{self.value}"

            def metadata(self):
                return {"type": self.source_type, "value": self.value, "origin": self.origin, "tags": []}

            async def fetch(self, *args, **kwargs):
                import base64

                await asyncio.sleep(self.delay)
                payload = base64.urlsafe_b64encode(
                    f"aes-256-gcm:pass@{self.name}.example.com:8388".encode("utf-8")
                ).decode("ascii").rstrip("=")
                return SourceFetchResult(
                    self.source_id,
                    self.source_type,
                    self.value,
                    self.origin,
                    ok=True,
                    content=f"ss://{payload}#{self.name}",
                )

        slow_source = FakeSource("memory://slow", 0.05, "slow")
        fast_source = FakeSource("memory://fast", 0.0, "fast")
        observed: list[tuple[list[str], int, int]] = []

        def observe(nodes, report):
            observed.append(([node.name for node in nodes], report.fetched_sources, report.node_count))
            return False

        async def collect():
            with patch("collector.build_source", side_effect=[slow_source, fast_source]):
                return await collect_nodes_from_sources_with_report(
                    [
                        {"type": "url", "value": slow_source.value},
                        {"type": "url", "value": fast_source.value},
                    ],
                    tier="tier3",
                    logger=logging.getLogger("test-collector"),
                    settings={"request_timeout": 1, "retries": 0},
                    source_observer=observe,
                )

        nodes, report = asyncio.run(collect())

        self.assertEqual(observed[0], (["fast"], 1, 1))
        self.assertEqual(observed[-1], (["slow"], 2, 2))
        self.assertEqual([node.name for node in nodes], ["fast", "slow"])
        self.assertEqual(report.fetched_sources, 2)

    def test_root_terminal_observer_fires_before_slow_sibling_finishes(self) -> None:
        class FakeSource:
            source_type = "subscription_url"
            origin = "tier3b"

            def __init__(self, value: str, *, delay: float, ok: bool) -> None:
                self.value = value
                self.delay = delay
                self.ok = ok

            @property
            def source_id(self):
                return f"subscription_url:{self.value}"

            def metadata(self):
                return {
                    "type": self.source_type,
                    "value": self.value,
                    "origin": self.origin,
                    "tags": [],
                }

            async def fetch(self, *_args, **_kwargs):
                import base64

                await asyncio.sleep(self.delay)
                if not self.ok:
                    return SourceFetchResult(
                        self.source_id,
                        self.source_type,
                        self.value,
                        self.origin,
                        ok=False,
                        error="connect timeout",
                    )
                payload = base64.urlsafe_b64encode(
                    b"aes-256-gcm:pass@slow.example.com:8388"
                ).decode("ascii").rstrip("=")
                return SourceFetchResult(
                    self.source_id,
                    self.source_type,
                    self.value,
                    self.origin,
                    ok=True,
                    content=f"ss://{payload}#slow",
                )

        failed = FakeSource("memory://failed", delay=0.0, ok=False)
        slow = FakeSource("memory://slow", delay=0.15, ok=True)
        terminal_events: list[tuple[str, float]] = []
        started = time.monotonic()

        def observe_terminal(source):
            terminal_events.append(
                (str(source.get("value") or ""), time.monotonic() - started)
            )

        async def collect():
            with patch("collector.build_source", side_effect=[failed, slow]):
                return await collect_nodes_from_sources_with_report(
                    [
                        {"type": "url", "value": failed.value},
                        {"type": "url", "value": slow.value},
                    ],
                    tier="tier3",
                    logger=logging.getLogger("test-collector-root-terminal"),
                    settings={
                        "request_timeout": 1,
                        "retries": 0,
                        "source_root_terminal_observer": observe_terminal,
                    },
                )

        _nodes, report = asyncio.run(collect())

        self.assertEqual([value for value, _elapsed in terminal_events], [
            failed.value,
            slow.value,
        ])
        self.assertLess(terminal_events[0][1], 0.10)
        self.assertGreaterEqual(terminal_events[1][1], 0.12)
        self.assertEqual(report.fetched_sources, 1)
        self.assertEqual(report.failed_sources, 1)

    def test_source_observer_status_payload_does_not_cancel_pending_sources(self) -> None:
        class FakeSource:
            source_type = "subscription_url"
            origin = "tier3b"

            def __init__(self, value: str, delay: float, name: str) -> None:
                self.value = value
                self.delay = delay
                self.name = name

            @property
            def source_id(self):
                return f"subscription_url:{self.value}"

            def metadata(self):
                return {"type": self.source_type, "value": self.value, "origin": self.origin, "tags": []}

            async def fetch(self, *args, **kwargs):
                import base64

                await asyncio.sleep(self.delay)
                payload = base64.urlsafe_b64encode(
                    f"aes-256-gcm:pass@{self.name}.example.com:8388".encode("utf-8")
                ).decode("ascii").rstrip("=")
                return SourceFetchResult(
                    self.source_id,
                    self.source_type,
                    self.value,
                    self.origin,
                    ok=True,
                    content=f"ss://{payload}#{self.name}",
                )

        slow_source = FakeSource("memory://slow", 0.05, "slow")
        fast_source = FakeSource("memory://fast", 0.0, "fast")
        observed: list[int] = []

        def observe(nodes, report):
            observed.append(report.fetched_sources)
            return {"status": "queued", "stop": False}

        async def collect():
            with patch("collector.build_source", side_effect=[slow_source, fast_source]):
                return await collect_nodes_from_sources_with_report(
                    [
                        {"type": "url", "value": slow_source.value},
                        {"type": "url", "value": fast_source.value},
                    ],
                    tier="tier3",
                    logger=logging.getLogger("test-collector"),
                    settings={"request_timeout": 1, "retries": 0},
                    source_observer=observe,
                )

        nodes, report = asyncio.run(collect())

        self.assertEqual(observed, [1, 2])
        self.assertEqual([node.name for node in nodes], ["fast", "slow"])
        self.assertEqual(report.fetched_sources, 2)

    def test_source_observer_non_explicit_truthy_payload_does_not_cancel_pending_sources(self) -> None:
        class FakeSource:
            source_type = "subscription_url"
            origin = "tier3b"

            def __init__(self, value: str, delay: float, name: str) -> None:
                self.value = value
                self.delay = delay
                self.name = name

            @property
            def source_id(self):
                return f"subscription_url:{self.value}"

            def metadata(self):
                return {"type": self.source_type, "value": self.value, "origin": self.origin, "tags": []}

            async def fetch(self, *args, **kwargs):
                import base64

                await asyncio.sleep(self.delay)
                payload = base64.urlsafe_b64encode(
                    f"aes-256-gcm:pass@{self.name}.example.com:8388".encode("utf-8")
                ).decode("ascii").rstrip("=")
                return SourceFetchResult(
                    self.source_id,
                    self.source_type,
                    self.value,
                    self.origin,
                    ok=True,
                    content=f"ss://{payload}#{self.name}",
                )

        slow_source = FakeSource("memory://slow", 0.05, "slow")
        fast_source = FakeSource("memory://fast", 0.0, "fast")

        def observe(nodes, report):
            return {"backpressure": True, "queued": report.fetched_sources}

        async def collect():
            with patch("collector.build_source", side_effect=[slow_source, fast_source]):
                return await collect_nodes_from_sources_with_report(
                    [
                        {"type": "url", "value": slow_source.value},
                        {"type": "url", "value": fast_source.value},
                    ],
                    tier="tier3",
                    logger=logging.getLogger("test-collector"),
                    settings={"request_timeout": 1, "retries": 0},
                    source_observer=observe,
                )

        nodes, report = asyncio.run(collect())

        self.assertEqual([node.name for node in nodes], ["fast", "slow"])
        self.assertEqual(report.fetched_sources, 2)

    def test_source_observer_explicit_stop_cancels_pending_sources(self) -> None:
        class FakeSource:
            source_type = "subscription_url"
            origin = "tier3b"

            def __init__(self, value: str, delay: float, name: str) -> None:
                self.value = value
                self.delay = delay
                self.name = name

            @property
            def source_id(self):
                return f"subscription_url:{self.value}"

            def metadata(self):
                return {"type": self.source_type, "value": self.value, "origin": self.origin, "tags": []}

            async def fetch(self, *args, **kwargs):
                import base64

                await asyncio.sleep(self.delay)
                payload = base64.urlsafe_b64encode(
                    f"aes-256-gcm:pass@{self.name}.example.com:8388".encode("utf-8")
                ).decode("ascii").rstrip("=")
                return SourceFetchResult(
                    self.source_id,
                    self.source_type,
                    self.value,
                    self.origin,
                    ok=True,
                    content=f"ss://{payload}#{self.name}",
                )

        slow_source = FakeSource("memory://slow", 0.2, "slow")
        fast_source = FakeSource("memory://fast", 0.0, "fast")

        def observe(nodes, report):
            return {"stop": True}

        async def collect():
            with patch("collector.build_source", side_effect=[slow_source, fast_source]):
                return await collect_nodes_from_sources_with_report(
                    [
                        {"type": "url", "value": slow_source.value},
                        {"type": "url", "value": fast_source.value},
                    ],
                    tier="tier3",
                    logger=logging.getLogger("test-collector"),
                    settings={"request_timeout": 1, "retries": 0},
                    source_observer=observe,
                )

        nodes, report = asyncio.run(collect())

        self.assertEqual([node.name for node in nodes], ["fast"])
        self.assertEqual(report.fetched_sources, 1)

    def test_source_floor_stop_prevents_unstarted_root_fetches(self) -> None:
        source_stop_event = threading.Event()
        fetch_calls: list[str] = []

        class FakeSource:
            source_type = "subscription_url"
            origin = "tier3b"

            def __init__(self, value: str) -> None:
                self.value = value
                self.source_id = f"subscription_url:{value}"

            def metadata(self):
                return {
                    "id": self.source_id,
                    "type": self.source_type,
                    "value": self.value,
                    "origin": self.origin,
                    "tags": [],
                }

            async def fetch(self, *_args, **_kwargs):
                fetch_calls.append(self.value)
                source_stop_event.set()
                return SourceFetchResult(
                    self.source_id,
                    self.source_type,
                    self.value,
                    self.origin,
                    ok=False,
                    error="should be superseded by floor stop",
                )

        entries = [
            {"type": "subscription_url", "value": f"memory://source-{index}"}
            for index in range(3)
        ]

        async def collect():
            with patch(
                "collector.build_source",
                side_effect=lambda entry, **_kwargs: FakeSource(str(entry["value"])),
            ):
                return await collect_nodes_from_sources_with_report(
                    entries,
                    tier="tier3",
                    logger=logging.getLogger("test-collector-floor-stop"),
                    settings={
                        "request_timeout": 1,
                        "retries": 0,
                        "source_root_concurrency": 1,
                    },
                    source_stop_event=source_stop_event,
                )

        nodes, report = asyncio.run(collect())

        self.assertEqual(nodes, [])
        self.assertEqual(fetch_calls, ["memory://source-0"])
        self.assertEqual(report.source_count, 3)
        self.assertEqual(report.failed_sources, 3)
        self.assertTrue(
            all(source.get("status") == "deferred" for source in report.sources)
        )
        self.assertTrue(
            all(source.get("retryable") is False for source in report.sources)
        )

    def test_streaming_source_roots_have_independent_first_result_deadlines(self) -> None:
        class FakeSource:
            source_type = "subscription_url"
            origin = "tier3b"

            def __init__(self, value: str, *, fast: bool) -> None:
                self.value = value
                self.fast = fast

            @property
            def source_id(self):
                return f"subscription_url:{self.value}"

            def metadata(self):
                return {
                    "type": self.source_type,
                    "value": self.value,
                    "origin": self.origin,
                    "tags": [],
                }

            async def fetch(self, *args, **kwargs):
                import base64

                if not self.fast:
                    await asyncio.sleep(2.0)
                payload = base64.urlsafe_b64encode(
                    f"aes-256-gcm:pass@{self.value}.example.com:8388".encode("utf-8")
                ).decode("ascii").rstrip("=")
                return SourceFetchResult(
                    self.source_id,
                    self.source_type,
                    self.value,
                    self.origin,
                    ok=True,
                    content=f"ss://{payload}#{self.value}",
                )

        sources = [FakeSource(f"source-{index}", fast=index == 0) for index in range(5)]

        async def collect():
            with patch("collector.build_source", side_effect=sources):
                return await collect_nodes_from_sources_with_report(
                    [
                        {"type": "subscription_url", "value": source.value}
                        for source in sources
                    ],
                    tier="tier3",
                    logger=logging.getLogger("test-collector-root-deadline"),
                    settings={
                        "request_timeout": 10,
                        "retries": 0,
                        "source_first_result_deadline_seconds": 0.05,
                        "source_cancel_slack_seconds": 0.01,
                    },
                    source_observer=lambda _nodes, _report: False,
                )

        started = time.monotonic()
        nodes, report = asyncio.run(collect())
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.35)
        self.assertEqual([node.name for node in nodes], ["source-0"])
        self.assertEqual(report.source_count, 5)
        self.assertEqual(report.fetched_sources, 1)
        self.assertEqual(report.failed_sources, 4)
        timed_out = [source for source in report.sources if not source.get("ok")]
        self.assertEqual(len(timed_out), 4)
        self.assertTrue(all(source.get("retryable") for source in timed_out))
        self.assertTrue(
            all("first-result timeout" in str(source.get("error")) for source in timed_out)
        )

    def test_github_source_units_stream_independently_with_unit_attribution(self) -> None:
        paths = ["Config/shadowsocks.txt", "Config/vless.txt"]

        class Candidate:
            source_type = "subscription_url"
            origin = "tier3b"

            def __init__(self, value: str, content: str, delay: float) -> None:
                self.value = value
                self.content = content
                self.delay = delay

            @property
            def source_id(self):
                return f"subscription_url:{self.value}"

            async def fetch(self, *_args, **_kwargs):
                await asyncio.sleep(self.delay)
                return SourceFetchResult(
                    self.source_id,
                    self.source_type,
                    self.value,
                    self.origin,
                    ok=True,
                    content=self.content,
                )

        candidates = [
            Candidate(
                "https://raw.githubusercontent.com/owner/repo/main/Config/shadowsocks.txt",
                "vless://fast@example.com:443#fast-unit",
                0.001,
            ),
            Candidate(
                "https://raw.githubusercontent.com/owner/repo/main/Config/vless.txt",
                "vless://delayed@example.com:443#delayed-unit",
                0.04,
            ),
        ]
        observed_batches: list[list[str]] = []

        async def collect():
            with patch(
                "tier3_sources.GitHubRepositorySource.discover",
                return_value=candidates,
            ):
                return await collect_nodes_from_sources_with_report(
                    [{"type": "github_repo", "value": "https://github.com/owner/repo"}],
                    tier="tier3",
                    logger=logging.getLogger("test-collector-github-units"),
                    settings={
                        "request_timeout": 1,
                        "retries": 0,
                        "github_candidate_paths": paths,
                        "source_unit_budget": 6,
                        "source_unit_concurrency": 6,
                    },
                    source_observer=lambda nodes, _report: observed_batches.append(
                        [node.name for node in nodes]
                    )
                    or False,
                )

        nodes, report = asyncio.run(collect())

        self.assertEqual([node.name for node in nodes], ["fast-unit", "delayed-unit"])
        self.assertEqual(observed_batches, [["fast-unit"], ["delayed-unit"]])
        self.assertEqual(report.source_count, 1)
        self.assertEqual(report.fetched_sources, 1)
        self.assertEqual(report.discovered_source_units, 2)
        self.assertEqual(report.fetched_source_units, 2)
        self.assertEqual(report.pending_source_units, 0)
        self.assertEqual(
            [unit["id"] for unit in report.sources[0]["units"]],
            paths,
        )
        self.assertEqual(
            {node.extra["tier3_source_unit_family"] for node in nodes},
            {"ss", "vless"},
        )
        self.assertTrue(all(node.extra["tier3_source_root_id"] for node in nodes))
        units = report.sources[0]["units"]
        # These in-memory candidate fixtures never opened a transport.
        self.assertTrue(all(not unit["transport_started"] for unit in units))
        self.assertTrue(all(unit["parse_valid"] for unit in units))
        self.assertTrue(all(unit["observer_handoff"] for unit in units))
        report_payload = report.to_dict()
        self.assertEqual(report_payload["source_unit_launch_debt"], 0)
        self.assertEqual(report_payload["source_unit_transport_started_count"], 0)
        self.assertEqual(report_payload["source_unit_parse_valid_count"], 2)
        self.assertEqual(report_payload["source_unit_observer_handoff_count"], 2)

    def test_github_root_first_result_does_not_cancel_delayed_sibling_unit(self) -> None:
        paths = ["Config/shadowsocks.txt", "Config/vless.txt"]

        class Candidate:
            source_type = "subscription_url"
            origin = "tier3b"

            def __init__(self, value: str, content: str, delay: float) -> None:
                self.value = value
                self.content = content
                self.delay = delay

            @property
            def source_id(self):
                return f"subscription_url:{self.value}"

            async def fetch(self, *_args, **_kwargs):
                await asyncio.sleep(self.delay)
                return SourceFetchResult(
                    self.source_id,
                    self.source_type,
                    self.value,
                    self.origin,
                    ok=True,
                    content=self.content,
                )

        candidates = [
            Candidate(
                "https://raw.example/fast",
                "vless://fast@example.com:443#fast-unit",
                0.005,
            ),
            Candidate(
                "https://raw.example/delayed",
                "vless://delayed@example.com:443#delayed-unit",
                0.08,
            ),
        ]

        async def collect():
            with (
                patch(
                    "tier3_sources.GitHubRepositorySource.discover",
                    return_value=candidates,
                ),
                patch("collector.parse_content_blob", wraps=parse_content_blob) as parser,
            ):
                result = await collect_nodes_from_sources_with_report(
                    [
                        {
                            "type": "github_repo",
                            "value": "https://github.com/owner/repo",
                        }
                    ],
                    tier="tier3",
                    logger=logging.getLogger("test-collector-unit-entitlement"),
                    settings={
                        "request_timeout": 1,
                        "retries": 0,
                        "github_candidate_paths": paths,
                        "source_unit_budget": 2,
                        "source_unit_concurrency": 2,
                        "source_first_result_deadline_seconds": 0.02,
                        "source_unit_deadline_seconds": 0.2,
                    },
                )
                return result, parser.call_count

        (nodes, report), parse_calls = asyncio.run(collect())

        self.assertEqual([node.name for node in nodes], ["fast-unit", "delayed-unit"])
        self.assertEqual(parse_calls, 2)
        self.assertEqual(report.fetched_source_units, 2)
        self.assertEqual(report.failed_source_units, 0)
        self.assertGreaterEqual(report.sources[0]["first_body_elapsed_ms"], 1)

    def test_github_partial_root_retries_timeout_unit_but_not_missing_unit(self) -> None:
        paths = ["Config/vless.txt", "Config/vmess.txt", "Config/trojan.txt"]

        class Candidate:
            source_type = "subscription_url"
            origin = "tier3b"

            def __init__(self, value: str, result: SourceFetchResult) -> None:
                self.value = value
                self.result = result

            @property
            def source_id(self):
                return f"subscription_url:{self.value}"

            async def fetch(self, *_args, **_kwargs):
                return self.result

        def result(value: str, *, ok: bool, content: str = "", error: str = ""):
            return SourceFetchResult(
                f"subscription_url:{value}",
                "subscription_url",
                value,
                "tier3b",
                ok=ok,
                content=content,
                error=error,
            )

        values = [f"https://raw.example/{path}" for path in paths]
        candidates = [
            Candidate(
                values[0],
                result(
                    values[0],
                    ok=True,
                    content="vless://uuid@example.com:443#working-vless",
                ),
            ),
            Candidate(values[1], result(values[1], ok=False, error="HTTP 404 Not Found")),
            Candidate(values[2], result(values[2], ok=False, error="connection timed out")),
        ]

        async def collect():
            with patch(
                "tier3_sources.GitHubRepositorySource.discover",
                return_value=candidates,
            ):
                return await collect_nodes_from_sources_with_report(
                    [{"type": "github_repo", "value": "https://github.com/owner/repo"}],
                    tier="tier3",
                    logger=logging.getLogger("test-collector-partial-units"),
                    settings={
                        "request_timeout": 1,
                        "retries": 0,
                        "github_candidate_paths": paths,
                        "source_unit_budget": 6,
                        "source_unit_concurrency": 6,
                    },
                )

        nodes, report = asyncio.run(collect())
        root = report.sources[0]
        units = {unit["id"]: unit for unit in root["units"]}

        self.assertEqual([node.name for node in nodes], ["working-vless"])
        self.assertEqual(root["status"], "partial")
        self.assertEqual(report.fetched_sources, 1)
        self.assertEqual(report.failed_sources, 0)
        self.assertFalse(units[paths[1]]["retryable"])
        self.assertTrue(units[paths[2]]["retryable"])
        self.assertEqual(report.retry_due_source_units, 1)

    def test_slow_source_observer_does_not_stretch_root_deadlines(self) -> None:
        class FakeSource:
            source_type = "subscription_url"
            origin = "tier3b"

            def __init__(self, value: str, *, fast: bool) -> None:
                self.value = value
                self.fast = fast

            @property
            def source_id(self):
                return f"subscription_url:{self.value}"

            def metadata(self):
                return {
                    "type": self.source_type,
                    "value": self.value,
                    "origin": self.origin,
                    "tags": [],
                }

            async def fetch(self, *_args, **_kwargs):
                import base64

                if not self.fast:
                    await asyncio.sleep(2.0)
                payload = base64.urlsafe_b64encode(
                    f"aes-256-gcm:pass@{self.value}.example.com:8388".encode("utf-8")
                ).decode("ascii").rstrip("=")
                return SourceFetchResult(
                    self.source_id,
                    self.source_type,
                    self.value,
                    self.origin,
                    ok=True,
                    content=f"ss://{payload}#{self.value}",
                )

        sources = [FakeSource(f"source-{index}", fast=index == 0) for index in range(5)]

        def slow_observer(_nodes, _report):
            time.sleep(0.20)
            return False

        async def collect():
            with patch("collector.build_source", side_effect=sources):
                return await collect_nodes_from_sources_with_report(
                    [
                        {"type": "subscription_url", "value": source.value}
                        for source in sources
                    ],
                    tier="tier3",
                    logger=logging.getLogger("test-collector-observer-deadline"),
                    settings={
                        "request_timeout": 10,
                        "retries": 0,
                        "source_first_result_deadline_seconds": 0.05,
                        "source_cancel_slack_seconds": 0.01,
                    },
                    source_observer=slow_observer,
                )

        started = time.monotonic()
        _nodes, report = asyncio.run(collect())
        elapsed = time.monotonic() - started
        timed_out = [source for source in report.sources if not source.get("ok")]
        self.assertLess(elapsed, 0.30)
        self.assertEqual(len(timed_out), 4)
        self.assertTrue(
            all(int(source.get("deadline_actual_ms", 0) or 0) <= 150 for source in timed_out)
        )

    def test_external_cancel_event_interrupts_blocked_source_fetch_promptly(self) -> None:
        fetch_cancelled = threading.Event()
        cancel_event = threading.Event()

        class SlowSource:
            source_type = "subscription_url"
            origin = "tier3b"
            source_id = "subscription_url:memory://blocked"
            value = "memory://blocked"

            def metadata(self):
                return {
                    "id": self.source_id,
                    "type": self.source_type,
                    "value": self.value,
                    "origin": self.origin,
                    "tags": [],
                }

            async def fetch(self, *_args, **_kwargs):
                try:
                    await asyncio.sleep(2.0)
                finally:
                    fetch_cancelled.set()

        def request_cancel() -> None:
            time.sleep(0.05)
            cancel_event.set()

        stopper = threading.Thread(target=request_cancel)
        stopper.start()
        started = time.monotonic()
        try:
            with patch("collector.build_source", return_value=SlowSource()):
                nodes, report = asyncio.run(
                    collect_nodes_from_sources_with_report(
                        [{"type": "url", "value": "memory://blocked"}],
                        tier="tier3",
                        logger=logging.getLogger("test-collector-external-cancel"),
                        settings={"request_timeout": 10, "retries": 0},
                        cancel_event=cancel_event,
                    )
                )
        finally:
            stopper.join(timeout=1.0)

        self.assertLess(time.monotonic() - started, 0.25)
        self.assertTrue(fetch_cancelled.wait(0.25))
        self.assertEqual(nodes, [])
        self.assertEqual(report.fetched_sources, 0)

    def test_collection_returns_only_after_inner_source_task_is_cancelled(self) -> None:
        cancel_event = threading.Event()

        async def scenario() -> tuple[list[Node], bool, list[str]]:
            fetch_started = asyncio.Event()
            fetch_finished = asyncio.Event()

            class SlowSource:
                source_type = "subscription_url"
                origin = "tier3b"
                source_id = "subscription_url:memory://persistent-loop"
                value = "memory://persistent-loop"

                def metadata(self):
                    return {
                        "id": self.source_id,
                        "type": self.source_type,
                        "value": self.value,
                        "origin": self.origin,
                        "tags": [],
                    }

                async def fetch(self, *_args, **_kwargs):
                    fetch_started.set()
                    try:
                        await asyncio.Future()
                    finally:
                        fetch_finished.set()

            with patch("collector.build_source", return_value=SlowSource()):
                collection_task = asyncio.create_task(
                    collect_nodes_from_sources_with_report(
                        [{"type": "url", "value": "memory://persistent-loop"}],
                        tier="tier3",
                        logger=logging.getLogger("test-collector-persistent-loop-cancel"),
                        settings={"request_timeout": 10, "retries": 0},
                        cancel_event=cancel_event,
                    )
                )
                await asyncio.wait_for(fetch_started.wait(), timeout=1.0)
                cancel_event.set()
                nodes, _report = await asyncio.wait_for(collection_task, timeout=1.0)
                await asyncio.sleep(0)
                pending_source_tasks = [
                    task.get_coro().__qualname__
                    for task in asyncio.all_tasks()
                    if task is not asyncio.current_task()
                    and not task.done()
                    and "collect_source" in task.get_coro().__qualname__
                ]
                return nodes, fetch_finished.is_set(), pending_source_tasks

        nodes, fetch_finished, pending_source_tasks = asyncio.run(scenario())

        self.assertEqual(nodes, [])
        self.assertTrue(fetch_finished)
        self.assertEqual(pending_source_tasks, [])

    def test_uncooperative_inner_source_task_is_reported_as_drain_failure(self) -> None:
        cancel_event = threading.Event()

        async def scenario() -> bool:
            fetch_started = asyncio.Event()
            fetch_finished = asyncio.Event()
            release_fetch = asyncio.Event()

            class UncooperativeSource:
                source_type = "subscription_url"
                origin = "tier3b"
                source_id = "subscription_url:memory://uncooperative"
                value = "memory://uncooperative"

                def metadata(self):
                    return {
                        "id": self.source_id,
                        "type": self.source_type,
                        "value": self.value,
                        "origin": self.origin,
                        "tags": [],
                    }

                async def fetch(self, *_args, **_kwargs):
                    fetch_started.set()
                    try:
                        while not release_fetch.is_set():
                            try:
                                await asyncio.sleep(0.01)
                            except asyncio.CancelledError:
                                continue
                    finally:
                        fetch_finished.set()
                    raise asyncio.CancelledError

            with patch("collector.build_source", return_value=UncooperativeSource()):
                collection_task = asyncio.create_task(
                    collect_nodes_from_sources_with_report(
                        [{"type": "url", "value": "memory://uncooperative"}],
                        tier="tier3",
                        logger=logging.getLogger("test-collector-source-drain-failure"),
                        settings={
                            "request_timeout": 10,
                            "retries": 0,
                            "source_cancel_slack_seconds": 0.01,
                            "source_cancel_drain_seconds": 0.05,
                        },
                        cancel_event=cancel_event,
                    )
                )
                await asyncio.wait_for(fetch_started.wait(), timeout=1.0)
                cancel_event.set()
                try:
                    with self.assertRaises(BlockingWorkCancellationTimeout):
                        await asyncio.wait_for(collection_task, timeout=1.0)
                finally:
                    release_fetch.set()
                    await asyncio.wait_for(fetch_finished.wait(), timeout=1.0)
                return collection_task.done()

        self.assertTrue(asyncio.run(scenario()))

    def test_external_cancel_discards_queued_observer_notifications(self) -> None:
        observer_started = threading.Event()
        release_observer = threading.Event()
        cancel_event = threading.Event()
        observer_calls: list[list[str]] = []

        class FastSource:
            source_type = "subscription_url"
            origin = "tier3b"

            def __init__(self, index: int) -> None:
                self.source_id = f"subscription_url:memory://observer-{index}"
                self.value = f"memory://observer-{index}"
                self.index = index

            def metadata(self):
                return {
                    "id": self.source_id,
                    "type": self.source_type,
                    "value": self.value,
                    "origin": self.origin,
                    "tags": [],
                }

            async def fetch(self, *_args, **_kwargs):
                return SourceFetchResult(
                    self.source_id,
                    self.source_type,
                    self.value,
                    self.origin,
                    ok=True,
                    content=(
                        "vless://11111111-1111-1111-1111-111111111111"
                        f"@node-{self.index}.example:443?encryption=none#node-{self.index}"
                    ),
                )

        def observe(nodes, _report):
            observer_calls.append([node.name for node in nodes])
            observer_started.set()
            release_observer.wait(1.0)
            return False

        def request_cancel() -> None:
            if observer_started.wait(1.0):
                time.sleep(0.03)
                cancel_event.set()
                time.sleep(0.01)
                release_observer.set()

        sources = [FastSource(1), FastSource(2)]
        stopper = threading.Thread(target=request_cancel)
        stopper.start()
        try:
            with patch("collector.build_source", side_effect=sources):
                nodes, _report = asyncio.run(
                    collect_nodes_from_sources_with_report(
                        [
                            {"type": "url", "value": "memory://observer-1"},
                            {"type": "url", "value": "memory://observer-2"},
                        ],
                        tier="tier3",
                        logger=logging.getLogger("test-collector-observer-cancel"),
                        settings={"request_timeout": 1, "retries": 0},
                        source_observer=observe,
                        cancel_event=cancel_event,
                    )
                )
        finally:
            release_observer.set()
            stopper.join(timeout=1.0)

        self.assertTrue(cancel_event.is_set())
        self.assertEqual(nodes, [])
        self.assertEqual(len(observer_calls), 1)

    def test_first_explicit_observer_stop_discards_queued_notifications(self) -> None:
        observer_started = threading.Event()
        release_observer = threading.Event()
        observer_calls: list[list[str]] = []

        class FastSource:
            source_type = "subscription_url"
            origin = "tier3b"

            def __init__(self, index: int) -> None:
                self.source_id = f"subscription_url:memory://explicit-stop-{index}"
                self.value = f"memory://explicit-stop-{index}"
                self.index = index

            def metadata(self):
                return {
                    "id": self.source_id,
                    "type": self.source_type,
                    "value": self.value,
                    "origin": self.origin,
                    "tags": [],
                }

            async def fetch(self, *_args, **_kwargs):
                return SourceFetchResult(
                    self.source_id,
                    self.source_type,
                    self.value,
                    self.origin,
                    ok=True,
                    content=(
                        "vless://11111111-1111-1111-1111-111111111111"
                        f"@explicit-{self.index}.example:443"
                        f"?encryption=none#explicit-{self.index}"
                    ),
                )

        def observe(nodes, _report):
            observer_calls.append([node.name for node in nodes])
            if len(observer_calls) == 1:
                observer_started.set()
                release_observer.wait(1.0)
                return {"stop": True}
            return False

        def release_first_observer() -> None:
            if observer_started.wait(1.0):
                time.sleep(0.08)
                release_observer.set()

        sources = [FastSource(index) for index in range(5)]
        releaser = threading.Thread(target=release_first_observer)
        releaser.start()
        try:
            with patch("collector.build_source", side_effect=sources):
                asyncio.run(
                    collect_nodes_from_sources_with_report(
                        [
                            {"type": "url", "value": source.value}
                            for source in sources
                        ],
                        tier="tier3",
                        logger=logging.getLogger("test-collector-observer-explicit-stop"),
                        settings={"request_timeout": 1, "retries": 0},
                        source_observer=observe,
                    )
                )
        finally:
            release_observer.set()
            releaser.join(timeout=1.0)

        self.assertTrue(observer_started.is_set())
        self.assertEqual(len(observer_calls), 1)

    def test_task_cancel_cooperatively_drains_local_parse_and_dedupe(self) -> None:
        for worker_name in ("parse_content_blob", "deduplicate_nodes"):
            with self.subTest(worker_name=worker_name):
                started = threading.Event()
                finished = threading.Event()
                external_cancel = threading.Event()
                received_cancel_events: list[threading.Event | None] = []

                def cooperative_worker(*, cancel_event=None):
                    received_cancel_events.append(cancel_event)
                    started.set()
                    try:
                        if cancel_event is not None:
                            cancel_event.wait(2.0)
                        return []
                    finally:
                        finished.set()

                cooperative_worker.__name__ = worker_name

                async def scenario() -> None:
                    task = asyncio.create_task(
                        _run_blocking_cancellable(
                            cooperative_worker,
                            cancel_event=external_cancel,
                            cooperative_cancel=True,
                            drain_seconds=0.25,
                        )
                    )
                    deadline = asyncio.get_running_loop().time() + 1.0
                    while not started.is_set():
                        if asyncio.get_running_loop().time() >= deadline:
                            self.fail("local worker did not start")
                        await asyncio.sleep(0.001)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task

                asyncio.run(scenario())

                self.assertTrue(finished.is_set())
                self.assertEqual(len(received_cancel_events), 1)
                self.assertIsNot(received_cancel_events[0], external_cancel)
                self.assertFalse(
                    any(
                        thread.is_alive()
                        and thread.name == f"collector-local-{worker_name}"
                        for thread in threading.enumerate()
                    )
                )

    def test_task_cancel_reports_uncooperative_local_worker(self) -> None:
        started = threading.Event()
        finished = threading.Event()
        release_worker = threading.Event()

        def uncooperative_worker(*, cancel_event=None):
            del cancel_event
            started.set()
            try:
                release_worker.wait(1.0)
                return []
            finally:
                finished.set()

        async def scenario() -> None:
            task = asyncio.create_task(
                _run_blocking_cancellable(
                    uncooperative_worker,
                    cooperative_cancel=True,
                    drain_seconds=0.02,
                )
            )
            deadline = asyncio.get_running_loop().time() + 1.0
            while not started.is_set():
                if asyncio.get_running_loop().time() >= deadline:
                    self.fail("local worker did not start")
                await asyncio.sleep(0.001)
            task.cancel()
            with self.assertRaises(BlockingWorkCancellationTimeout):
                await task

        try:
            asyncio.run(scenario())
        finally:
            release_worker.set()
            self.assertTrue(finished.wait(1.0))

    def test_repeated_task_cancel_does_not_interrupt_local_worker_drain(self) -> None:
        started = threading.Event()
        cancel_received = threading.Event()
        finished = threading.Event()

        def slow_cooperative_worker(*, cancel_event=None):
            started.set()
            try:
                if cancel_event is not None:
                    cancel_event.wait(1.0)
                cancel_received.set()
                time.sleep(0.05)
                return []
            finally:
                finished.set()

        async def scenario() -> None:
            task = asyncio.create_task(
                _run_blocking_cancellable(
                    slow_cooperative_worker,
                    cooperative_cancel=True,
                    drain_seconds=0.25,
                )
            )
            deadline = asyncio.get_running_loop().time() + 1.0
            while not started.is_set():
                if asyncio.get_running_loop().time() >= deadline:
                    self.fail("local worker did not start")
                await asyncio.sleep(0.001)
            task.cancel()
            deadline = asyncio.get_running_loop().time() + 1.0
            while not cancel_received.is_set():
                if asyncio.get_running_loop().time() >= deadline:
                    self.fail("local worker did not receive cancellation")
                await asyncio.sleep(0.001)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())

        self.assertTrue(finished.is_set())
        self.assertFalse(
            any(
                thread.is_alive()
                and thread.name == "collector-local-slow_cooperative_worker"
                for thread in threading.enumerate()
            )
        )

    def test_external_cancel_cooperatively_drains_local_parse(self) -> None:
        parse_started = threading.Event()
        parse_finished = threading.Event()
        cancel_event = threading.Event()

        class FastSource:
            source_type = "subscription_url"
            origin = "tier3b"
            source_id = "subscription_url:memory://parse"
            value = "memory://parse"

            def metadata(self):
                return {
                    "id": self.source_id,
                    "type": self.source_type,
                    "value": self.value,
                    "origin": self.origin,
                    "tags": [],
                }

            async def fetch(self, *_args, **_kwargs):
                return SourceFetchResult(
                    self.source_id,
                    self.source_type,
                    self.value,
                    self.origin,
                    ok=True,
                    content="large-source-payload",
                )

        def slow_parse(*_args, cancel_event=None, **_kwargs):
            parse_started.set()
            try:
                if cancel_event is not None:
                    cancel_event.wait(2.0)
                return []
            finally:
                parse_finished.set()

        def request_cancel() -> None:
            if parse_started.wait(1.0):
                cancel_event.set()

        stopper = threading.Thread(target=request_cancel)
        stopper.start()
        started = time.monotonic()
        try:
            with (
                patch("collector.build_source", return_value=FastSource()),
                patch("collector.parse_content_blob", side_effect=slow_parse),
            ):
                nodes, report = asyncio.run(
                    collect_nodes_from_sources_with_report(
                        [{"type": "url", "value": "memory://parse"}],
                        tier="tier3",
                        logger=logging.getLogger("test-collector-parse-cancel"),
                        settings={"request_timeout": 10, "retries": 0},
                        cancel_event=cancel_event,
                    )
                )
        finally:
            stopper.join(timeout=1.0)

        self.assertTrue(parse_started.is_set())
        self.assertTrue(parse_finished.wait(0.25))
        self.assertLess(time.monotonic() - started, 0.25)
        self.assertEqual(nodes, [])
        self.assertEqual(report.node_count, 0)

    def test_external_cancel_cooperatively_drains_local_dedupe(self) -> None:
        dedupe_started = threading.Event()
        dedupe_finished = threading.Event()
        cancel_event = threading.Event()
        parsed_node = Node(
            name="candidate",
            protocol="ss",
            uri="ss://candidate",
            address="candidate.example",
            port=8388,
            tier="tier3",
        )

        class FastSource:
            source_type = "subscription_url"
            origin = "tier3b"
            source_id = "subscription_url:memory://dedupe"
            value = "memory://dedupe"

            def metadata(self):
                return {
                    "id": self.source_id,
                    "type": self.source_type,
                    "value": self.value,
                    "origin": self.origin,
                    "tags": [],
                }

            async def fetch(self, *_args, **_kwargs):
                return SourceFetchResult(
                    self.source_id,
                    self.source_type,
                    self.value,
                    self.origin,
                    ok=True,
                    content="source-payload",
                )

        def slow_dedupe(nodes, *, cancel_event=None):
            dedupe_started.set()
            try:
                if cancel_event is not None:
                    cancel_event.wait(2.0)
                return []
            finally:
                dedupe_finished.set()

        def request_cancel() -> None:
            if dedupe_started.wait(1.0):
                cancel_event.set()

        stopper = threading.Thread(target=request_cancel)
        stopper.start()
        started = time.monotonic()
        try:
            with (
                patch("collector.build_source", return_value=FastSource()),
                patch("collector.parse_content_blob", return_value=[parsed_node]),
                patch("collector.deduplicate_nodes", side_effect=slow_dedupe),
            ):
                nodes, report = asyncio.run(
                    collect_nodes_from_sources_with_report(
                        [{"type": "url", "value": "memory://dedupe"}],
                        tier="tier3",
                        logger=logging.getLogger("test-collector-dedupe-cancel"),
                        settings={"request_timeout": 10, "retries": 0},
                        cancel_event=cancel_event,
                    )
                )
        finally:
            stopper.join(timeout=1.0)

        self.assertTrue(dedupe_started.is_set())
        self.assertTrue(dedupe_finished.wait(0.25))
        self.assertLess(time.monotonic() - started, 0.25)
        self.assertEqual(nodes, [])
        self.assertEqual(report.node_count, 0)


if __name__ == "__main__":
    unittest.main()
