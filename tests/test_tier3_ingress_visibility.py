from __future__ import annotations

from collections import defaultdict

import pytest

import manager as manager_module
from collector import CollectionReport
from models import Node


def payload(families: int, *, prefix: str = "opaque", per_family: int = 64):
    return [Node(name=f"{family}:{index}", protocol=f"{prefix}-{family}",
                 uri=f"private-fixture://{family}/{index}")
            for family in range(families) for index in range(per_family)]


@pytest.mark.parametrize("families", [1, 2, 5, 17])
def test_first_chunk_exposes_every_already_parsed_family_without_reordering_within_it(families):
    normalized = []
    for prefix in ("opaque", "renamed-reverse-lexical"):
        nodes = payload(families, prefix=prefix)
        report = CollectionReport(source_count=1, node_count=len(nodes))
        batch = manager_module._Tier3SourceIngressBatch(nodes, report, "exploration")
        first, remaining = batch.take(32)
        assert len({node.protocol for node in first}) == families
        output = list(first)
        while remaining is not None:
            assert remaining.nodes is nodes
            assert remaining.report is report
            assert remaining.lane == "exploration"
            assert remaining.remaining_count == len(nodes) - len(output)
            chunk, remaining = remaining.take(32)
            output.extend(chunk)
        assert len(output) == len(nodes)
        assert {id(node) for node in output} == {id(node) for node in nodes}
        by_family = defaultdict(list)
        for node in output:
            by_family[node.protocol].append(node.name)
        for family in range(families):
            assert by_family[f"{prefix}-{family}"] == [f"{family}:{i}" for i in range(64)]
        normalized.append([node.name for node in output])
        assert "private-fixture" not in repr(batch)
    assert normalized[0] == normalized[1]


def test_chunk_size_does_not_change_family_cursor_order_or_mutate_input():
    nodes = payload(5)
    original_order = [id(node) for node in nodes]
    report = CollectionReport(source_count=1, node_count=len(nodes))
    traces = []
    for limit in (1, 7, 16, 32, 97):
        batch = manager_module._Tier3SourceIngressBatch(nodes, report, "exploration")
        output = []
        while batch is not None:
            chunk, batch = batch.take(limit)
            output.extend(node.name for node in chunk)
        traces.append(output)
    assert all(trace == traces[0] for trace in traces)
    assert [id(node) for node in nodes] == original_order


def test_cursor_classifies_complete_payload_only_once(monkeypatch):
    nodes = payload(17, per_family=600)
    report = CollectionReport(source_count=1, node_count=len(nodes))
    calls = []
    original = manager_module.tier3_candidate_service_family

    def classify(node):
        calls.append(id(node))
        return original(node)

    monkeypatch.setattr(manager_module, "tier3_candidate_service_family", classify)
    batch = manager_module._Tier3SourceIngressBatch(nodes, report, "exploration")
    output = []
    while batch is not None:
        chunk, batch = batch.take(32)
        output.extend(chunk)
    assert len(calls) == len(nodes)
    assert len(set(calls)) == len(nodes)
    assert len(output) == len(nodes)


def test_existing_offset_does_not_reintroduce_consumed_nodes():
    nodes = payload(3, per_family=64)
    report = CollectionReport(source_count=1, node_count=len(nodes))
    batch = manager_module._Tier3SourceIngressBatch(nodes, report, "exploration", offset=60)
    output = []
    while batch is not None:
        chunk, batch = batch.take(32)
        output.extend(chunk)
    assert len(output) == len(nodes) - 60
    assert {id(node) for node in output} == {id(node) for node in nodes[60:]}
    assert [node.protocol for node in output[:3]] == ["opaque-0", "opaque-1", "opaque-2"]


@pytest.mark.parametrize("lane", ["validated_replay", "snapshot_revalidation"])
def test_trusted_replay_keeps_existing_freshness_order(lane, monkeypatch):
    nodes = payload(5)
    report = CollectionReport(source_count=1, node_count=len(nodes))

    def unexpected_classification(_node):
        raise AssertionError("trusted replay must not enter cold interleaving")

    monkeypatch.setattr(manager_module, "tier3_candidate_service_family", unexpected_classification)
    batch = manager_module._Tier3SourceIngressBatch(nodes, report, lane)
    output = []
    while batch is not None:
        chunk, batch = batch.take(32)
        output.extend(chunk)
    assert output == nodes
