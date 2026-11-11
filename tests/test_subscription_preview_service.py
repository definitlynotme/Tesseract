import asyncio
import copy
import logging
import threading
import unittest

from desktop_runtime import DesktopRuntime
from models import Node, NodeStatus
from node_status_view import node_identity, node_status_snapshot
from subscription_operations import SubscriptionOperationCoordinator
from subscription_preview_service import SubscriptionPreviewService


SUBSCRIPTION_URL = "https://sub.example/list?token=secret-token"


def preview_config(url: str = SUBSCRIPTION_URL) -> dict:
    return {
        "tiers": {
            "tier2": {"subscription_urls": [url]},
            "tier3": {"subscription_urls": []},
            "exit": {"subscription_urls": []},
        },
        "testing": {"validation_mode": "tcp"},
    }


class FakeCollectionReport:
    def to_dict(self) -> dict:
        return {
            "source_count": 1,
            "fetched_sources": 1,
            "failed_sources": 0,
            "node_count": 1,
            "sources": [{"url": SUBSCRIPTION_URL, "error": "token leak"}],
        }


class SubscriptionPreviewServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.logger = logging.getLogger(f"subscription-preview-{id(self)}")
        self.logger.handlers.clear()
        self.logger.addHandler(logging.NullHandler())

    def test_preview_collection_requires_explicit_route_for_remote_sources(self) -> None:
        settings = SubscriptionPreviewService._collection_settings(preview_config())
        self.assertTrue(settings["require_remote_source_route"])
        self.assertNotIn("runtime_source_fetch_proxy_url", settings)

        config = preview_config()
        config["egress_proxy"] = {
            "enabled": True,
            "server": "proxy.example.com",
            "port": 8080,
        }
        explicit = SubscriptionPreviewService._collection_settings(config)
        self.assertEqual(explicit["egress_proxy"], config["egress_proxy"])

    def test_preview_accepts_authoritative_runtime_collection_settings(self) -> None:
        observed_settings: list[dict] = []

        async def collector(**kwargs):
            observed_settings.append(dict(kwargs["settings"]))
            return [], FakeCollectionReport()

        service = SubscriptionPreviewService(
            preview_config(),
            self.logger,
            collector=collector,
            collection_settings_provider=lambda _config: {
                "require_remote_source_route": True,
                "runtime_source_fetch_proxy_url": "http://127.0.0.1:19094",
                "runtime_source_fetch_proxy_first": True,
            },
        )

        service.diagnose_subscription(
            {"tier": "tier2", "kind": "subscription", "url": SUBSCRIPTION_URL},
            "runtime-route",
        )

        self.assertEqual(
            observed_settings[0]["runtime_source_fetch_proxy_url"],
            "http://127.0.0.1:19094",
        )
        self.assertNotIn("egress_proxy", observed_settings[0])

    def test_diagnosis_is_temporary_sanitized_and_does_not_mutate_input(self) -> None:
        config = preview_config()
        collected = Node(
            name="private node",
            protocol="vless",
            uri="vless://uuid@example.com:443?private_key=key",
            address="example.com",
            port=443,
            source=SUBSCRIPTION_URL,
            tier="tier2",
            uuid="uuid-secret",
            password="password-secret",
            extra={"private_key": "key-secret"},
        )
        original = copy.deepcopy(collected.to_dict())

        async def collector(**_kwargs):
            return [collected], FakeCollectionReport()

        async def tester(nodes, **_kwargs):
            self.assertIsNot(nodes[0], collected)
            nodes[0].status = NodeStatus.ALIVE
            nodes[0].latency_ms = 17
            nodes[0].last_checked_at = "2026-08-02T00:00:00+00:00"
            return nodes

        service = SubscriptionPreviewService(
            lambda: config,
            self.logger,
            collector=collector,
            tester=tester,
        )

        result = service.diagnose_subscription(
            {"tier": "tier2", "kind": "subscription", "url": SUBSCRIPTION_URL},
            "request-1",
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["reason"], "manual_diagnosis_completed")
        self.assertEqual(result["url"], SUBSCRIPTION_URL)
        self.assertTrue(result["diagnostic_only"])
        self.assertTrue(result["temporary"])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["healthy"], 1)
        self.assertIsNotNone(result["completed_at"])
        self.assertEqual(collected.to_dict(), original)
        self.assertNotIn("sources", result["collection"])
        self.assertNotIn("identity", result["nodes"][0])
        self.assertNotIn("connection", result["nodes"][0])
        for secret_key in ("uri", "uuid", "password", "private_key"):
            self.assertNotIn(secret_key, result["nodes"][0])
        self.assertNotIn("uuid-secret", repr(result["nodes"]))
        self.assertNotIn("password-secret", repr(result["nodes"]))
        self.assertEqual(service.active_request_ids(), ())

    def test_system_operation_blocks_manual_with_exact_reason(self) -> None:
        coordinator = SubscriptionOperationCoordinator()
        system_lease = coordinator.begin_system("tier2", SUBSCRIPTION_URL)
        service = SubscriptionPreviewService(
            preview_config(),
            self.logger,
            operation_coordinator=coordinator,
            collector=lambda **_kwargs: self.fail("collector must not run"),
        )
        try:
            result = service.diagnose_subscription(
                {"tier": "tier2", "kind": "subscription", "url": SUBSCRIPTION_URL},
                "request-blocked",
            )
        finally:
            coordinator.finish(system_lease)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "automatic_refresh_in_progress")
        self.assertTrue(result["blocked_by_system"])
        self.assertEqual(result["url"], SUBSCRIPTION_URL)

    def test_system_preempts_manual_and_discards_late_nodes(self) -> None:
        coordinator = SubscriptionOperationCoordinator()
        collector_started = threading.Event()
        result_holder: list[dict] = []

        async def collector(*, cancel_event, **_kwargs):
            collector_started.set()
            while not cancel_event.is_set():
                await asyncio.sleep(0.005)
            return [Node(name="late", tier="tier2")], FakeCollectionReport()

        service = SubscriptionPreviewService(
            preview_config(),
            self.logger,
            operation_coordinator=coordinator,
            collector=collector,
        )
        worker = threading.Thread(
            target=lambda: result_holder.append(
                service.diagnose_subscription(
                    {"tier": "tier2", "kind": "subscription", "url": SUBSCRIPTION_URL},
                    "request-preempted",
                )
            )
        )
        worker.start()
        self.assertTrue(collector_started.wait(timeout=1.0))
        system_lease = coordinator.begin_system("tier2", SUBSCRIPTION_URL)
        worker.join(timeout=2.0)
        coordinator.finish(system_lease)

        self.assertFalse(worker.is_alive())
        self.assertEqual(result_holder[0]["status"], "cancelled")
        self.assertEqual(result_holder[0]["nodes"], [])
        self.assertEqual(coordinator.manual_active_count("tier2", SUBSCRIPTION_URL), 0)

    def test_changed_config_rejects_completed_result(self) -> None:
        config = preview_config()

        async def collector(**_kwargs):
            config["tiers"]["tier2"]["subscription_urls"] = []
            return [Node(name="stale", tier="tier2")], FakeCollectionReport()

        async def tester(nodes, **_kwargs):
            return nodes

        service = SubscriptionPreviewService(
            lambda: config,
            self.logger,
            collector=collector,
            tester=tester,
        )
        result = service.diagnose_subscription(
            {"tier": "tier2", "kind": "subscription", "url": SUBSCRIPTION_URL},
            "request-stale",
        )

        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["reason"], "subscription_config_changed")
        self.assertEqual(result["nodes"], [])

    def test_cancel_all_cancels_each_independent_request(self) -> None:
        second_url = "https://sub.example/second"
        config = preview_config()
        config["tiers"]["tier2"]["subscription_urls"].append(second_url)
        started = threading.Barrier(3)
        results: list[dict] = []

        async def collector(*, cancel_event, **_kwargs):
            started.wait(timeout=1.0)
            while not cancel_event.is_set():
                await asyncio.sleep(0.005)
            return [], FakeCollectionReport()

        service = SubscriptionPreviewService(
            config,
            self.logger,
            collector=collector,
        )
        workers = [
            threading.Thread(
                target=lambda url=url, request_id=request_id: results.append(
                    service.diagnose_subscription(
                        {"tier": "tier2", "kind": "subscription", "url": url},
                        request_id,
                    )
                )
            )
            for url, request_id in (
                (SUBSCRIPTION_URL, "cancel-one"),
                (second_url, "cancel-two"),
            )
        ]
        for worker in workers:
            worker.start()
        started.wait(timeout=1.0)

        self.assertEqual(service.cancel_all(), 2)
        for worker in workers:
            worker.join(timeout=2.0)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual({result["status"] for result in results}, {"cancelled"})
        self.assertEqual(service.active_request_ids(), ())

    def test_preview_leaves_runtime_cache_and_core_state_unchanged(self) -> None:
        config = preview_config()
        runtime_state = {
            "cache_files": {
                "tier2": b"tier2-cache-before",
                "tier3": b"tier3-cache-before",
                "exit": b"exit-cache-before",
            },
            "runtime_nodes": {
                "tier2": [Node(name="official-tier2", tier="tier2").to_dict()],
                "tier3": [Node(name="official-tier3", tier="tier3").to_dict()],
                "exit": [Node(name="official-exit", tier="exit").to_dict()],
            },
            "last_check_at": {
                "tier2": "tier2-check-before",
                "tier3": "tier3-check-before",
                "exit": "exit-check-before",
            },
            "tier3_pool_revision": 41,
            "tier3_last_collection_report": {"node_count": 9},
            "core_reload_calls": [],
        }
        before_config = copy.deepcopy(config)
        before_state = copy.deepcopy(runtime_state)

        async def collector(**_kwargs):
            return [
                Node(
                    name="preview-only",
                    protocol="vless",
                    address="preview.example",
                    port=443,
                    tier="tier2",
                )
            ], FakeCollectionReport()

        async def tester(nodes, **_kwargs):
            nodes[0].status = NodeStatus.ALIVE
            return nodes

        service = SubscriptionPreviewService(
            lambda: config,
            self.logger,
            collector=collector,
            tester=tester,
        )
        result = service.diagnose_subscription(
            {"tier": "tier2", "kind": "subscription", "url": SUBSCRIPTION_URL},
            "side-effect-guard",
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(config, before_config)
        self.assertEqual(runtime_state, before_state)

    def test_collector_failure_and_empty_parse_are_terminal_without_details(self) -> None:
        async def failing_collector(**_kwargs):
            raise RuntimeError(f"secret failure for {SUBSCRIPTION_URL}")

        failed = SubscriptionPreviewService(
            preview_config(),
            self.logger,
            collector=failing_collector,
        ).diagnose_subscription(
            {"tier": "tier2", "kind": "subscription", "url": SUBSCRIPTION_URL},
            "collector-failed",
        )
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["reason"], "diagnosis_failed")
        self.assertNotIn("secret failure", failed["error"])

        async def empty_collector(**_kwargs):
            return [], FakeCollectionReport()

        empty = SubscriptionPreviewService(
            preview_config(),
            self.logger,
            collector=empty_collector,
        ).diagnose_subscription(
            {"tier": "tier2", "kind": "subscription", "url": SUBSCRIPTION_URL},
            "collector-empty",
        )
        self.assertEqual(empty["status"], "empty")
        self.assertEqual(empty["reason"], "no_nodes")


class DesktopRuntimeSubscriptionSnapshotTests(unittest.TestCase):
    def test_first_import_system_context_is_released_on_success_and_failure(self) -> None:
        logger = logging.getLogger(f"first-import-refresh-{id(self)}")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())

        for should_fail in (False, True):
            with self.subTest(should_fail=should_fail):
                unrelated_state = {
                    "tier2_cache": ["previous-tier2"],
                    "tier3_cache": ["previous-tier3"],
                    "exit_cache": ["previous-exit"],
                    "tier3_pool_revision": 12,
                    "tier3_report": {"node_count": 4},
                }
                before = copy.deepcopy(unrelated_state)

                class ShortLivedManager:
                    def refresh_subscription_url_system(self, tier, url):
                        if should_fail:
                            raise RuntimeError("refresh failed")
                        return [
                            Node(
                                name="imported",
                                tier=tier,
                                source=url,
                                status=NodeStatus.ALIVE,
                            )
                        ]

                manager = ShortLivedManager()
                released: list[object] = []
                runtime = DesktopRuntime.__new__(DesktopRuntime)
                runtime.manager = None
                runtime.logger = logger
                runtime._runtime_lifecycle_lock = threading.RLock()
                runtime._reload_after = (
                    lambda action, *, target_tier: action()
                )
                runtime._ensure_subscription_refresh_manager = (
                    lambda *, target_tier: manager
                )
                runtime._release_subscription_refresh_manager = (
                    lambda current: released.append(current)
                )

                result = runtime._import_subscription_with_initial_refresh(
                    "tier2",
                    {"url": SUBSCRIPTION_URL},
                    lambda: {"path": "config.local.yaml"},
                )

                self.assertEqual(released, [manager])
                self.assertEqual(unrelated_state, before)
                if should_fail:
                    self.assertIn("refresh_error", result)
                    self.assertFalse(result["refresh"]["stored"])
                else:
                    self.assertTrue(result["refresh"]["stored"])
                    self.assertTrue(result["refresh"]["system_owned"])

    def test_extra_only_subscription_source_is_persisted_until_tier_check(self) -> None:
        static = Node(
            name="static",
            protocol="vless",
            address="static.example",
            port=443,
            tier="tier2",
            source="config",
        )
        extra_only = Node(
            name="cached-subscription",
            protocol="vless",
            address="subscription.example",
            port=443,
            tier="tier2",
            extra={"subscription_url": SUBSCRIPTION_URL},
            last_checked_at="2026-08-01T00:00:00+00:00",
        )
        shared_source = Node(
            name="shared-subscription",
            protocol="vless",
            address="shared.example",
            port=443,
            tier="tier2",
            sources=["https://other.example/list", SUBSCRIPTION_URL],
        )

        class FakeManager:
            def __init__(self) -> None:
                self.lock = threading.RLock()
                self.runtime_nodes = {
                    "tier2": [static, extra_only, shared_source],
                }
                self.tier2_last_refresh_at = "2026-08-02T01:00:00+00:00"
                self.last_check_at = None
                self.runtime_generation = 0
                self.source_evidence: dict[str, dict] = {}

            @staticmethod
            def _node_identity(node: Node) -> str:
                return node_identity(node)

            def get_node_status_snapshot(self) -> dict:
                return {
                    "tiers": {
                        "tier2": {
                            "last_check_at": self.last_check_at,
                            "nodes": [
                                node_status_snapshot(node, identity=node_identity(node))
                                for node in self.runtime_nodes["tier2"]
                            ],
                        }
                    }
                }

            def subscription_source_check_snapshot(self, _tier: str) -> dict:
                return copy.deepcopy(self.source_evidence)

        runtime = DesktopRuntime.__new__(DesktopRuntime)
        runtime.config = preview_config()
        runtime.manager = FakeManager()

        snapshot = runtime.node_status_snapshot()
        by_name = {node["name"]: node for node in snapshot["tiers"]["tier2"]["nodes"]}
        self.assertEqual(by_name["static"]["observation_origin"], "live")
        self.assertEqual(by_name["cached-subscription"]["observation_origin"], "persisted_cache")
        self.assertEqual(by_name["shared-subscription"]["observation_origin"], "persisted_cache")
        self.assertEqual(
            by_name["cached-subscription"]["cached_at"],
            "2026-08-02T01:00:00+00:00",
        )
        self.assertEqual(
            by_name["cached-subscription"]["observed_at"],
            "2026-08-01T00:00:00+00:00",
        )

        runtime.manager.last_check_at = "2026-08-02T02:00:00+00:00"
        tier_only_check = runtime.node_status_snapshot()
        tier_only_by_name = {
            node["name"]: node
            for node in tier_only_check["tiers"]["tier2"]["nodes"]
        }
        self.assertEqual(tier_only_by_name["static"]["observation_origin"], "live")
        self.assertEqual(
            tier_only_by_name["cached-subscription"]["observation_origin"],
            "persisted_cache",
        )
        self.assertEqual(
            tier_only_by_name["shared-subscription"]["observation_origin"],
            "persisted_cache",
        )

        runtime.manager.source_evidence[SUBSCRIPTION_URL] = {
            "runtime_generation": 0,
            "checked_at": "2026-08-02T03:00:00+00:00",
        }
        checked = runtime.node_status_snapshot()
        checked_by_name = {
            node["name"]: node
            for node in checked["tiers"]["tier2"]["nodes"]
        }
        for name in ("cached-subscription", "shared-subscription"):
            self.assertEqual(checked_by_name[name]["observation_origin"], "live")
            self.assertNotIn("cached_at", checked_by_name[name])

    def test_targeted_source_check_does_not_promote_other_subscription(self) -> None:
        for tier in ("tier2", "exit"):
            with self.subTest(tier=tier):
                first_url = f"https://{tier}.example/a"
                second_url = f"https://{tier}.example/b"
                first = Node(
                    name="source-a",
                    protocol="vless",
                    address=f"a.{tier}.example",
                    port=443,
                    tier=tier,
                    source=first_url,
                    sources=[first_url],
                    last_checked_at="2026-08-02T04:00:00+00:00",
                )
                second = Node(
                    name="source-b",
                    protocol="vless",
                    address=f"b.{tier}.example",
                    port=443,
                    tier=tier,
                    source=second_url,
                    sources=[second_url],
                    last_checked_at="2026-08-01T01:00:00+00:00",
                )
                static = Node(
                    name="static",
                    protocol="vless",
                    address=f"static.{tier}.example",
                    port=443,
                    tier=tier,
                    source="config",
                )

                class FakeManager:
                    def __init__(self) -> None:
                        self.lock = threading.RLock()
                        self.runtime_nodes = {tier: [first, second, static]}
                        setattr(
                            self,
                            f"{tier}_last_refresh_at",
                            "2026-08-02T03:30:00+00:00",
                        )
                        self.runtime_generation = 7
                        self.evidence = {
                            first_url: {
                                "runtime_generation": 7,
                                "checked_at": "2026-08-02T04:00:00+00:00",
                            }
                        }

                    @staticmethod
                    def _node_identity(node: Node) -> str:
                        return node_identity(node)

                    def subscription_source_check_snapshot(self, _tier: str) -> dict:
                        return copy.deepcopy(self.evidence)

                    def get_node_status_snapshot(self) -> dict:
                        return {
                            "tiers": {
                                tier: {
                                    "last_check_at": "2026-08-02T04:00:01+00:00",
                                    "nodes": [
                                        node_status_snapshot(
                                            node,
                                            identity=node_identity(node),
                                        )
                                        for node in self.runtime_nodes[tier]
                                    ],
                                }
                            }
                        }

                runtime = DesktopRuntime.__new__(DesktopRuntime)
                runtime.config = {
                    "tiers": {
                        tier: {
                            "subscription_urls": [first_url, second_url],
                        }
                    }
                }
                runtime.manager = FakeManager()

                targeted = runtime.node_status_snapshot()
                by_name = {
                    node["name"]: node
                    for node in targeted["tiers"][tier]["nodes"]
                }
                self.assertEqual(by_name["source-a"]["observation_origin"], "live")
                self.assertEqual(
                    by_name["source-a"]["observed_at"],
                    "2026-08-02T04:00:00+00:00",
                )
                self.assertEqual(
                    by_name["source-b"]["observation_origin"],
                    "persisted_cache",
                )
                self.assertEqual(
                    by_name["source-b"]["observed_at"],
                    "2026-08-01T01:00:00+00:00",
                )
                self.assertNotEqual(
                    by_name["source-b"]["observed_at"],
                    targeted["tiers"][tier]["last_check_at"],
                )
                self.assertEqual(by_name["static"]["observation_origin"], "live")
                self.assertEqual(
                    by_name["static"]["observed_at"],
                    targeted["tiers"][tier]["last_check_at"],
                )

                runtime.manager.evidence[second_url] = {
                    "runtime_generation": 7,
                    "checked_at": "2026-08-02T05:00:00+00:00",
                }
                second.last_checked_at = "2026-08-02T05:00:00+00:00"
                full_check = runtime.node_status_snapshot()
                full_by_name = {
                    node["name"]: node
                    for node in full_check["tiers"][tier]["nodes"]
                }
                self.assertEqual(
                    full_by_name["source-b"]["observation_origin"],
                    "live",
                )
                self.assertEqual(
                    full_by_name["source-b"]["observed_at"],
                    "2026-08-02T05:00:00+00:00",
                )

    def test_tier3_subscription_snapshot_policy_is_unchanged(self) -> None:
        node = Node(
            name="tier3-subscription",
            protocol="vless",
            address="tier3.example",
            port=443,
            tier="tier3",
            source="https://tier3.example/sub",
        )

        class FakeManager:
            lock = threading.RLock()
            runtime_nodes = {"tier3": [node]}

            @staticmethod
            def _node_identity(current: Node) -> str:
                return node_identity(current)

            @staticmethod
            def get_node_status_snapshot() -> dict:
                return {
                    "tiers": {
                        "tier3": {
                            "last_check_at": "2026-08-02T06:00:00+00:00",
                            "nodes": [
                                node_status_snapshot(
                                    node,
                                    identity=node_identity(node),
                                )
                            ],
                        }
                    }
                }

        runtime = DesktopRuntime.__new__(DesktopRuntime)
        runtime.config = {
            "tiers": {
                "tier3": {
                    "subscription_urls": ["https://tier3.example/sub"],
                }
            }
        }
        runtime.manager = FakeManager()

        snapshot = runtime.node_status_snapshot()
        rendered = snapshot["tiers"]["tier3"]["nodes"][0]
        self.assertEqual(rendered["observation_origin"], "live")
        self.assertEqual(
            rendered["observed_at"],
            "2026-08-02T06:00:00+00:00",
        )


if __name__ == "__main__":
    unittest.main()
