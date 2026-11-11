from __future__ import annotations

import asyncio
import logging
import threading

import pytest

import collector
from models import Node
from tier3_sources import SourceUnitResult


class FakeSource:
    origin = "tier3b"

    def __init__(self, name, errors, *, source_type="github_repo", fallback_errors=None):
        self.value = f"https://fixture.invalid/{name}"
        self.source_type = source_type
        self.source_id = f"{source_type}:{self.value}"
        self.errors = errors
        self.fallback_errors = fallback_errors
        self.calls = []
        self.after_units = None

    def metadata(self):
        return {"id": self.source_id, "type": self.source_type,
                "value": self.value, "origin": self.origin, "tags": []}

    def unit_descriptors(self, **_kwargs):
        return [{"id": f"path-{index}", "value": f"{self.value}/{index}",
                 "family": "default"} for index in range(len(self.errors))]

    async def fetch_units(self, _session, **kwargs):
        self.calls.append(kwargs)
        errors = (self.fallback_errors
                  if kwargs["proxy_url"] and self.fallback_errors is not None
                  else self.errors)
        for index, error in enumerate(errors):
            await asyncio.sleep(0)
            yield SourceUnitResult(
                root_id=self.source_id, root_type=self.source_type,
                root_value=self.value, origin=self.origin, unit_id=f"path-{index}",
                unit_value=f"{self.value}/{index}", unit_family="default",
                ok=error is None, content="valid node" if error is None else "",
                error=error or "",
            )
        if self.after_units is not None:
            self.after_units()


@pytest.fixture
def capture(monkeypatch):
    records = []

    class CaptureHandler(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.Logger("test-collection-warning-aggregation", logging.DEBUG)
    logger.addHandler(CaptureHandler())
    monkeypatch.setattr(
        collector, "_parse_content_blob_bounded",
        lambda _text, **kwargs: [Node(name="fixture", protocol="http",
                                    address="127.0.0.1", port=9, tier=kwargs["tier"])],
    )
    return logger, records


def collect(monkeypatch, capture, sources, *, settings=None, **kwargs):
    by_value = {source.value: source for source in sources}
    monkeypatch.setattr(collector, "build_source",
                        lambda entry, **_kwargs: by_value[entry["value"]])
    return asyncio.run(collector.collect_nodes_from_sources_with_report(
        [{"type": source.source_type, "value": source.value} for source in sources],
        tier="tier3", logger=capture[0],
        settings={"request_timeout": 1, "retries": 0, **(settings or {})}, **kwargs,
    ))


def source_failure_summaries(capture):
    records = [record for record in capture[1]
               if record.getMessage().startswith("Collection source failures for")]
    assert all(record.levelno == logging.INFO for record in records)
    assert all(record.event_match_kind == "raw_only" for record in records)
    assert all(record.is_key_event is False for record in records)
    assert not [record for record in capture[1] if record.levelno == logging.WARNING]
    return records


def event_records(capture, key):
    return [record for record in capture[1] if getattr(record, "event_key", "") == key]


def test_27_failed_units_emit_five_info_summaries_and_preserve_all_diagnostics(monkeypatch, capture):
    sources = [FakeSource(f"repo-{root}", [f"source unit timeout after 8.00s: path-{i}"
               for i in range(count)]) for root, count in enumerate((7, 6, 6, 4, 4))]
    nodes, report = collect(monkeypatch, capture, sources)

    assert not nodes
    assert report.failed_source_units == 27
    assert report.failed_sources == 5
    assert len(source_failure_summaries(capture)) == 5
    assert all("reason=timeout" in record.getMessage() for record in source_failure_summaries(capture))
    original = [record for record in capture[1]
                if record.getMessage().startswith("Collection failed for")]
    assert len(original) == 27
    assert all(record.levelno == logging.DEBUG for record in original)
    for source, state in zip(sources, report.sources):
        assert [unit["error"] for unit in state["units"]] == source.errors
        assert all(unit["retryable"] for unit in state["units"])
        assert len(source.calls) == 1
        assert source.calls[0]["retries"] == 0
    failed = event_records(capture, "update.sources_failed")
    assert len(failed) == 1
    assert failed[0].levelno == logging.ERROR
    assert failed[0].event_params["failed_count"] == 5


def test_different_failure_types_remain_visible_for_the_same_source(monkeypatch, capture):
    source = FakeSource("mixed", ["DNS lookup failed for host-a", "getaddrinfo failed for host-b",
                                  "source unit timeout after 8.00s", "connection refused"])
    _nodes, report = collect(monkeypatch, capture, [source])

    messages = [record.getMessage() for record in source_failure_summaries(capture)]
    assert len(messages) == 3
    assert any("reason=dns failed_units=2" in message for message in messages)
    assert any("reason=timeout failed_units=1" in message for message in messages)
    assert any("reason=refused failed_units=1" in message for message in messages)
    assert report.failed_source_units == 4


@pytest.mark.parametrize(("error", "kind"), [
    ("Cannot connect to host raw.githubusercontent.com:443 ssl:default [None]", "connection"),
    ("Cannot connect to host fixture.invalid:443 ssl:True [certificate verify failed]", "tls"),
    ("SSL handshake failed", "tls"),
])
def test_connection_options_do_not_imply_a_tls_failure(monkeypatch, capture, error, kind):
    source = FakeSource("connection-evidence", [error] * 3)
    _nodes, report = collect(monkeypatch, capture, [source])

    assert len(source_failure_summaries(capture)) == 1
    assert f"reason={kind} failed_units=3" in source_failure_summaries(capture)[0].getMessage()
    assert all(unit["error"] == error for unit in report.sources[0]["units"])


@pytest.mark.parametrize("error", ["404, message='Not Found', url=URL('https://fixture.invalid/sub')",
                                  "HTTP Error 404: Not Found"])
def test_successful_repository_candidate_404s_are_only_diagnostics(monkeypatch, capture, error):
    source = FakeSource("portfolio", [error, error, None])
    nodes, report = collect(monkeypatch, capture, [source])

    assert len(nodes) == 1
    assert not source_failure_summaries(capture)
    assert len(event_records(capture, "update.sources_completed")) == 1
    assert report.sources[0]["status"] == "partial"
    assert report.failed_source_units == 2
    assert report.sources[0]["units"][0]["error"] == error


def test_wholly_missing_repository_keeps_one_info_summary_and_all_failed_error(monkeypatch, capture):
    source = FakeSource("missing", ["404, message='Not Found'"] * 8)
    collect(monkeypatch, capture, [source])

    assert len(source_failure_summaries(capture)) == 1
    assert "reason=http_404 failed_units=8" in source_failure_summaries(capture)[0].getMessage()
    assert len(event_records(capture, "update.sources_failed")) == 1


def test_successful_runtime_fallback_does_not_leave_stale_failure_summary(monkeypatch, capture):
    source = FakeSource("fallback", ["DNS lookup failed"] * 3, fallback_errors=[None] * 3)
    nodes, report = collect(monkeypatch, capture, [source], settings={
        "runtime_source_fetch_proxy_url": "http://127.0.0.1:17890",
    })

    assert nodes
    assert not source_failure_summaries(capture)
    assert len(source.calls) == 2
    assert [call["proxy_url"] for call in source.calls] == [None, "http://127.0.0.1:17890"]
    assert report.failed_source_units == 0
    assert all(unit["fallback_attempted"] for unit in report.sources[0]["units"])
    assert len([record for record in capture[1]
                if record.getMessage().startswith("Collection failed for")]) == 3


def test_failed_runtime_fallback_is_grouped_by_final_reason_with_original_errors_kept(monkeypatch, capture):
    source = FakeSource("fallback-failed", ["DNS lookup failed"] * 3,
                        fallback_errors=["connection refused"] * 3)
    _nodes, report = collect(monkeypatch, capture, [source], settings={
        "runtime_source_fetch_proxy_url": "http://127.0.0.1:17890",
    })

    assert len(source_failure_summaries(capture)) == 1
    assert "reason=refused failed_units=3" in source_failure_summaries(capture)[0].getMessage()
    for unit in report.sources[0]["units"]:
        assert "DNS lookup failed" in unit["error"]
        assert unit["fallback_error"] == "connection refused"


def test_later_failure_is_not_suppressed_after_recovery(monkeypatch, capture):
    source = FakeSource("recovery", ["source unit timeout after 8.00s"] * 4)
    collect(monkeypatch, capture, [source])
    source.errors = [None] * 4
    collect(monkeypatch, capture, [source])
    source.errors = ["source unit timeout after 8.00s"] * 4
    collect(monkeypatch, capture, [source])

    assert len(source_failure_summaries(capture)) == 2
    assert len(event_records(capture, "update.sources_failed")) == 2
    assert len(event_records(capture, "update.sources_completed")) == 1


def test_cancelled_collection_does_not_publish_failure_summary(monkeypatch, capture):
    cancel = threading.Event()
    source = FakeSource("cancelled", ["source unit timeout after 8.00s"])
    source.after_units = cancel.set
    nodes, _report = collect(monkeypatch, capture, [source], cancel_event=cancel)

    assert not nodes
    assert not source_failure_summaries(capture)
    assert not event_records(capture, "update.sources_failed")


def test_partial_refresh_keeps_786_nodes_without_warning_for_three_failed_sources(monkeypatch, capture):
    sources = [FakeSource(f"repo-{index}", ["source unit timeout after 8.00s"])
               for index in range(3)]
    sources.extend(FakeSource(f"repo-{index}", [None]) for index in (3, 4))

    def parse(_text, *, source, tier, **_kwargs):
        first_port = 10000 + int(source.rsplit("-", 1)[-1]) * 393
        return [Node(name=f"node-{port}", protocol="http", address="127.0.0.1",
                     port=port, tier=tier) for port in range(first_port, first_port + 393)]

    monkeypatch.setattr(collector, "_parse_content_blob_bounded", parse)
    nodes, report = collect(monkeypatch, capture, sources)

    assert len(nodes) == 786
    assert report.failed_sources == 3
    assert report.fetched_sources == 2
    assert len(source_failure_summaries(capture)) == 3
    assert not event_records(capture, "update.sources_failed")
    assert not [record for record in capture[1] if record.levelno >= logging.WARNING]
    completed = event_records(capture, "update.sources_completed")
    assert len(completed) == 1
    assert completed[0].event_params == {"source_total": 5, "failed_count": 3, "usable_count": 786}
