import unittest

from models import Node, NodeStatus
from tier3_refresh_controller import Tier3ControllerDecision, Tier3RefreshPlan
from tier3_refresh_service import build_tier3_refresh_inputs, collect_tier3_source_candidates
from tier3_sources import CollectionReport


def node(name: str) -> Node:
    return Node(
        name=name,
        protocol="ss",
        uri=f"ss://{name}",
        address=f"{name}.example.com",
        port=8388,
        tier="tier3",
        status=NodeStatus.ALIVE,
        method="aes-256-gcm",
        password="pass",
    )


class Tier3RefreshServiceTests(unittest.TestCase):
    def test_build_refresh_inputs_copies_previous_pool_and_preserves_runtime_context(self) -> None:
        previous = [node("previous")]
        decision = Tier3ControllerDecision(intensity=1.0, source_budget=2, collect_interval=30, error=0.0, integral=0.0)
        plan = Tier3RefreshPlan(decision=decision, target_alive=3, intensive_expansion=True)
        sources_payload = {"subscription_urls": ["https://sources.example/sub"]}
        settings = {"request_timeout": 7}
        tier3_config = {"enabled": True}

        inputs = build_tier3_refresh_inputs(
            previous_nodes=previous,
            sources_payload=sources_payload,
            refresh_plan=plan,
            settings=settings,
            tier3_config=tier3_config,
        )

        self.assertEqual(inputs.previous_nodes, previous)
        self.assertIsNot(inputs.previous_nodes, previous)
        self.assertIs(inputs.sources_payload, sources_payload)
        self.assertIs(inputs.settings, settings)
        self.assertIs(inputs.tier3_config, tier3_config)
        self.assertIs(inputs.decision, decision)
        self.assertEqual(inputs.target_alive, 3)
        self.assertTrue(inputs.intensive_expansion)

    def test_collect_candidates_collects_tier3b_sources_from_sources_file(self) -> None:
        calls: list[list[str]] = []

        def collect(entries, settings):
            values = [entry["value"] for entry in entries]
            calls.append(values)
            if values == ["https://tier3a.example/sub"]:
                nodes = [node("tier3a")]
            elif values == ["https://tier3b-one.example/sub"]:
                nodes = [node("tier3b")]
            else:
                nodes = []
            return nodes, CollectionReport(
                source_count=len(entries),
                fetched_sources=len(entries),
                node_count=len(nodes),
            )

        result = collect_tier3_source_candidates(
            tier3_config={
                "subscription_urls": ["https://tier3a.example/sub"],
                "bootstrap_enabled": True,
            },
            sources_payload={"subscription_urls": ["https://tier3b-one.example/sub", "https://tier3b-two.example/sub"]},
            settings={"request_timeout": 5},
            decision=Tier3ControllerDecision(intensity=1.0, source_budget=1, collect_interval=30, error=0.0, integral=0.0),
            previous_nodes=[],
            previous_alive_count=0,
            target_alive=2,
            min_alive_nodes=2,
            intensive_expansion=True,
            startup_mode=True,
            collect_entries=collect,
        )

        self.assertEqual(calls, [
            ["https://tier3a.example/sub"],
            ["https://tier3b-one.example/sub"],
        ])
        self.assertEqual([item.name for item in result.nodes], ["tier3a", "tier3b"])
        self.assertEqual(result.report.source_count, 2)
        self.assertEqual(result.report.node_count, 2)

    def test_collect_candidates_uses_tier3b_sources_during_background_refresh(self) -> None:
        calls: list[list[str]] = []

        def collect(entries, settings):
            values = [entry["value"] for entry in entries]
            calls.append(values)
            if values == ["https://tier3b-one.example/sub"]:
                nodes = [node("tier3b")]
            else:
                nodes = []
            return nodes, CollectionReport(
                source_count=len(entries),
                fetched_sources=len(entries),
                node_count=len(nodes),
            )

        result = collect_tier3_source_candidates(
            tier3_config={
                "bootstrap_enabled": True,
            },
            sources_payload={"subscription_urls": ["https://tier3b-one.example/sub"]},
            settings={"request_timeout": 5},
            decision=Tier3ControllerDecision(intensity=1.0, source_budget=1, collect_interval=30, error=0.0, integral=0.0),
            previous_nodes=[],
            previous_alive_count=0,
            target_alive=3,
            min_alive_nodes=3,
            intensive_expansion=True,
            startup_mode=False,
            collect_entries=collect,
        )

        self.assertEqual(calls, [["https://tier3b-one.example/sub"]])
        self.assertEqual([item.name for item in result.nodes], ["tier3b"])
        self.assertEqual(result.report.source_count, 1)
        self.assertEqual(result.report.node_count, 1)

    def test_collect_candidates_ignores_legacy_bootstrap_sources_field(self) -> None:
        calls: list[list[str]] = []

        def collect(entries, settings):
            values = [entry["value"] for entry in entries]
            calls.append(values)
            return [], CollectionReport(
                source_count=len(entries),
                fetched_sources=len(entries),
                node_count=0,
            )

        result = collect_tier3_source_candidates(
            tier3_config={
                "bootstrap_enabled": True,
                "bootstrap_sources": ["https://bootstrap.example/sub"],
            },
            sources_payload={},
            settings={"request_timeout": 5},
            decision=Tier3ControllerDecision(intensity=0.9, source_budget=11, collect_interval=474, error=0.88, integral=1.0),
            previous_nodes=[node("previous-1"), node("previous-2"), node("previous-3")],
            previous_alive_count=3,
            target_alive=25,
            min_alive_nodes=3,
            intensive_expansion=False,
            startup_mode=False,
            collect_entries=collect,
        )

        self.assertEqual(calls, [])
        self.assertEqual(result.nodes, [])
        self.assertEqual(result.report.source_count, 0)

    def test_collect_candidates_skips_bootstrap_when_previous_pool_meets_threshold(self) -> None:
        calls: list[list[str]] = []

        def collect(entries, settings):
            values = [entry["value"] for entry in entries]
            calls.append(values)
            return [], CollectionReport(source_count=len(entries), fetched_sources=len(entries), node_count=0)

        result = collect_tier3_source_candidates(
            tier3_config={
                "subscription_urls": ["https://tier3a.example/sub"],
                "bootstrap_enabled": True,
            },
            sources_payload={},
            settings={},
            decision=Tier3ControllerDecision(intensity=1.0, source_budget=1, collect_interval=30, error=0.0, integral=0.0),
            previous_nodes=[node("previous")],
            previous_alive_count=1,
            target_alive=1,
            min_alive_nodes=1,
            intensive_expansion=False,
            startup_mode=False,
            collect_entries=collect,
        )

        self.assertEqual(calls, [["https://tier3a.example/sub"]])
        self.assertEqual(result.nodes, [])
        self.assertEqual(result.report.source_count, 1)

    def test_collect_candidates_batch_observer_can_stop_before_later_sources(self) -> None:
        calls: list[list[str]] = []
        observed_batches: list[list[str]] = []

        def collect(entries, settings):
            values = [entry["value"] for entry in entries]
            calls.append(values)
            nodes = [node("tier3a")] if values == ["https://tier3a.example/sub"] else [node("later")]
            return nodes, CollectionReport(
                source_count=len(entries),
                fetched_sources=len(entries),
                node_count=len(nodes),
            )

        def observe_batch(nodes, report):
            observed_batches.append([item.name for item in nodes])
            return True

        result = collect_tier3_source_candidates(
            tier3_config={
                "subscription_urls": ["https://tier3a.example/sub"],
                "bootstrap_enabled": True,
            },
            sources_payload={"subscription_urls": ["https://tier3b.example/sub"]},
            settings={"request_timeout": 5},
            decision=Tier3ControllerDecision(intensity=1.0, source_budget=1, collect_interval=30, error=0.0, integral=0.0),
            previous_nodes=[],
            previous_alive_count=0,
            target_alive=2,
            min_alive_nodes=2,
            intensive_expansion=True,
            startup_mode=True,
            collect_entries=collect,
            batch_observer=observe_batch,
        )

        self.assertEqual(calls, [["https://tier3a.example/sub"]])
        self.assertEqual(observed_batches, [["tier3a"]])
        self.assertEqual([item.name for item in result.nodes], ["tier3a"])

    def test_collect_candidates_stream_sources_flattens_tier3a_and_all_tier3b_sources(self) -> None:
        calls: list[list[str]] = []

        def collect(entries, settings):
            values = [entry["value"] for entry in entries]
            calls.append(values)
            return [node("combined")], CollectionReport(
                source_count=len(entries),
                fetched_sources=len(entries),
                node_count=1,
            )

        result = collect_tier3_source_candidates(
            tier3_config={
                "subscription_urls": ["https://tier3a.example/sub"],
                "bootstrap_enabled": True,
            },
            sources_payload={
                "subscription_urls": [
                    "https://tier3b-one.example/sub",
                    "https://tier3b-two.example/sub",
                ]
            },
            settings={"request_timeout": 5},
            decision=Tier3ControllerDecision(intensity=1.0, source_budget=1, collect_interval=30, error=0.0, integral=0.0),
            previous_nodes=[],
            previous_alive_count=0,
            target_alive=3,
            min_alive_nodes=3,
            intensive_expansion=True,
            startup_mode=True,
            collect_entries=collect,
            stream_sources=True,
        )

        self.assertEqual(
            calls,
            [[
                "https://tier3a.example/sub",
                "https://tier3b-one.example/sub",
                "https://tier3b-two.example/sub",
            ]],
        )
        self.assertEqual([item.name for item in result.nodes], ["combined"])
        self.assertEqual(result.report.source_count, 3)

    def test_streaming_collection_finishes_local_files_before_creating_remote_batch(self) -> None:
        calls: list[list[str]] = []
        local_finished = False

        def collect(entries, settings):
            nonlocal local_finished
            values = [entry["value"] for entry in entries]
            calls.append(values)
            if values == ["nodes.txt"]:
                local_finished = True
                return [node("local")], CollectionReport(
                    source_count=1,
                    configured_source_count=1,
                    fetched_sources=1,
                    node_count=1,
                )
            self.assertTrue(local_finished, "remote collection started before local_file completed")
            return [], CollectionReport(
                source_count=len(entries),
                configured_source_count=len(entries),
                fetched_sources=len(entries),
                node_count=0,
            )

        result = collect_tier3_source_candidates(
            tier3_config={
                "subscription_urls": ["https://tier3a.example/sub"],
                "bootstrap_enabled": True,
            },
            sources_payload={
                "local_files": ["nodes.txt"],
                "subscription_urls": ["https://tier3b.example/sub"],
            },
            settings={},
            decision=Tier3ControllerDecision(
                intensity=1.0,
                source_budget=2,
                collect_interval=30,
                error=0.0,
                integral=0.0,
            ),
            previous_nodes=[],
            previous_alive_count=0,
            target_alive=3,
            min_alive_nodes=3,
            intensive_expansion=True,
            startup_mode=True,
            collect_entries=collect,
            stream_sources=True,
        )

        self.assertEqual(
            calls,
            [
                ["nodes.txt"],
                ["https://tier3a.example/sub", "https://tier3b.example/sub"],
            ],
        )
        self.assertEqual([item.name for item in result.nodes], ["local"])

    def test_collect_candidates_dedupes_same_root_across_config_and_sources_file(self) -> None:
        calls: list[list[str]] = []

        def collect(entries, settings):
            values = [entry["value"] for entry in entries]
            calls.append(values)
            return [], CollectionReport(
                source_count=len(entries),
                configured_source_count=len(entries),
                fetched_sources=len(entries),
                node_count=0,
            )

        result = collect_tier3_source_candidates(
            tier3_config={
                "subscription_urls": ["https://same.example/sub"],
                "bootstrap_enabled": True,
            },
            sources_payload={"subscription_urls": ["https://same.example/sub"]},
            settings={},
            decision=Tier3ControllerDecision(
                intensity=1.0,
                source_budget=5,
                collect_interval=30,
                error=0.0,
                integral=0.0,
            ),
            previous_nodes=[],
            previous_alive_count=0,
            target_alive=25,
            min_alive_nodes=3,
            intensive_expansion=True,
            startup_mode=True,
            collect_entries=collect,
            stream_sources=True,
        )

        self.assertEqual(calls, [["https://same.example/sub"]])
        self.assertEqual(result.report.source_count, 1)
        self.assertEqual(result.report.configured_source_count, 1)

    def test_streaming_collection_treats_tier3b_entries_as_normal_source_roots(self) -> None:
        calls: list[list[str]] = []

        def collect(entries, settings):
            values = [entry["value"] for entry in entries]
            calls.append(values)
            return [node("large-primary") for _entry in range(30)], CollectionReport(
                source_count=len(entries),
                configured_source_count=len(entries),
                fetched_sources=len(entries),
                node_count=30,
            )

        result = collect_tier3_source_candidates(
            tier3_config={
                "bootstrap_enabled": True,
            },
            sources_payload={"subscription_urls": ["https://primary.example/sub", "https://bootstrap.example/sub"]},
            settings={},
            decision=Tier3ControllerDecision(
                intensity=1.0,
                source_budget=1,
                collect_interval=30,
                error=0.0,
                integral=0.0,
            ),
            previous_nodes=[],
            previous_alive_count=0,
            target_alive=25,
            min_alive_nodes=3,
            intensive_expansion=True,
            startup_mode=True,
            collect_entries=collect,
            stream_sources=True,
        )

        self.assertEqual(
            calls,
            [["https://primary.example/sub", "https://bootstrap.example/sub"]],
        )
        self.assertEqual(result.report.source_count, 2)


if __name__ == "__main__":
    unittest.main()
