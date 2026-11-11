from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

from tier_cache import TierObservationCache, _write_observation_payload


def test_incremental_cache_encoding_preserves_exact_json_and_record_order():
    payload = {
        "schema_version": 3,
        "generated_at": "2026-09-12T00:00:00Z",
        "nodes": [{"identity": "node-1", "extra": {"line": "\n\t\"\\", "value": "\u4e2d\u6587"}},
                  {"identity": "node-2", "nested": [None, False, 1.5]}],
        "negative": [{"identity": "node-3", "kind": "validation_fail"}],
    }
    output = io.StringIO()
    _write_observation_payload(output, payload)
    assert output.getvalue() == json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    assert json.loads(output.getvalue()) == payload


def test_cache_encoder_never_receives_the_whole_inventory():
    records = [{"identity": f"node-{index}", "extra": {"text": "x" * 4096}}
               for index in range(5000)]
    payload = {"schema_version": 3, "nodes": records, "negative": []}
    original = json.JSONEncoder.encode
    encoded_records = []

    def bounded(encoder, value):
        assert value is not payload
        assert not isinstance(value, list)
        if isinstance(value, dict):
            encoded_records.append(value["identity"])
        return original(encoder, value)

    with patch.object(json.JSONEncoder, "encode", bounded):
        _write_observation_payload(io.StringIO(), payload)
    assert encoded_records == [record["identity"] for record in records]


def test_record_encoding_coalesces_disk_writes_without_buffering_whole_cache():
    records = [{"identity": str(index), "value": "x" * 4096} for index in range(5000)]
    payload = {"schema_version": 3, "nodes": records, "negative": []}
    writes = []

    class Writer:
        def write(self, value):
            writes.append(value)

    _write_observation_payload(Writer(), payload)
    assert 50 < len(writes) < 100
    assert max(map(len, writes)) < 270_000
    assert json.loads("".join(writes)) == payload


@pytest.mark.parametrize("failure", ["encode", "replace"])
def test_partial_cache_write_never_replaces_durable_or_published_state(tmp_path, failure):
    cache = TierObservationCache(tmp_path / "observations.json")
    cache._write_records({}, generated_at="previous")
    previous = cache.path.read_bytes()
    published = []

    def broken_write(handle, payload):
        handle.write('{"nodes":[')
        raise OSError("injected encoder failure")

    target = "tier_cache._write_observation_payload" if failure == "encode" else "tier_cache.replace_with_retry"
    effect = broken_write if failure == "encode" else OSError("injected replace failure")
    with patch(target, side_effect=effect), patch.object(cache, "_publish_cached_payload", published.append):
        with pytest.raises(OSError):
            cache._write_records({}, generated_at="next")
    assert cache.path.read_bytes() == previous
    assert not published
    assert list(tmp_path.glob(".*.tmp")) == []
