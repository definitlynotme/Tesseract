from __future__ import annotations

import asyncio
import logging

import collector
from models import Node
from tier3_sources import SourceFetchResult


def test_parse_valid_timestamp_is_after_parse_not_body_arrival(monkeypatch):
    clock = [100.0]

    class Source:
        source_id = "opaque-source"
        source_type = "subscription_url"
        value = "https://fixture.invalid/subscription"
        origin = "tier3b"

        def metadata(self):
            return {"id": self.source_id, "type": self.source_type,
                    "value": self.value, "origin": self.origin, "tags": []}

        async def fetch(self, *args, **kwargs):
            return SourceFetchResult(self.source_id, self.source_type, self.value,
                                     self.origin, ok=True, content="opaque-payload")

    source = Source()

    def parse(*args, **kwargs):
        clock[0] += 0.4
        return [Node(name="fixture", protocol="http", address="127.0.0.1",
                     port=9, tier="tier3")]

    monkeypatch.setattr(collector, "build_source", lambda *args, **kwargs: source)
    monkeypatch.setattr(collector, "_parse_content_blob_bounded", parse)
    monkeypatch.setattr(collector.time, "perf_counter", lambda: clock[0])
    nodes, report = asyncio.run(collector.collect_nodes_from_sources_with_report(
        [{"type": source.source_type, "value": source.value}], tier="tier3",
        logger=logging.getLogger("test-parse-timing"),
        settings={"request_timeout": 1, "retries": 0},
        source_observer=lambda *_: False,
    ))
    assert len(nodes) == 1
    unit = report.sources[0]["units"][0]
    assert unit["parse_elapsed_ms"] == 400
    assert unit["parse_valid_elapsed_ms"] == 400
    assert unit["observer_handoff_elapsed_ms"] >= unit["parse_valid_elapsed_ms"]
