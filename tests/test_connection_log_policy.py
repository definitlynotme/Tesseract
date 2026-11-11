from __future__ import annotations

import json
import logging

import pytest

from log_capture import (
    LogCaptureQueue, MihomoStdoutFallbackTracker, StructuredEventJsonHandler,
    seed_structured_event_log,
)
from log_event_reports import match_mihomo_event
from log_store import LogRecord, LogStore


TIMEOUT = (
    "[TCP] dial DIRECT (match DomainSuffix/githubusercontent.com) "
    "127.0.0.1:57989 --> raw.githubusercontent.com:443 error: "
    "dial tcp 185.199.111.133:443: i/o timeout\n"
    "dial tcp 185.199.110.133:443: i/o timeout"
)


def captured(message=TIMEOUT, level="WARNING", **extra):
    queue = LogCaptureQueue()
    queue.put_nowait({
        "source": "MIHOMO", "level": level, "message_raw": message, **extra,
    })
    return queue.drain()[0]


DNS_FAILURE = (
    "[TCP] dial DIRECT 127.0.0.1:58165 --> raw.githubusercontent.com:443 error: "
    "dns resolve failed: all DNS requests failed, first error: requesting "
    "https://dns.alidns.com:443/dns-query: Get \"https://dns.alidns.com:443/dns-query?dns=AAAB\": "
    "dns resolve failed: all DNS requests failed, first error: "
    "read udp 172.21.6.66:60746->223.5.5.5:53: i/o timeout (resErr:<nil>)"
)


@pytest.mark.parametrize("message,key", [
    (TIMEOUT, "mihomo.connection_timeout"),
    (DNS_FAILURE, "mihomo.dns_resolve_failed"),
    ("[TCP] dial DIRECT 127.0.0.1:59633 --> cdn.jsdelivr.net:443 error: "
     "dns resolve failed: context deadline exceeded", "mihomo.dns_resolve_failed"),
    ("[TCP] dial DIRECT 127.0.0.1:58557 --> t.me:443 error: dial tcp 149.154.167.99:443: i/o timeout", "mihomo.connection_timeout"),
    ("[TCP] dial FINAL-EXIT (match Match/) 127.0.0.1:1 --> example.org:443 error: connection refused", "mihomo.connection_failed"),
    ("[UDP] dial DIRECT 127.0.0.1:1234 --> example.org:443 error: network is unreachable", "mihomo.connection_failed"),
])
def test_individual_connection_failure_is_durable_raw_info(message, key):
    row = captured(message)
    store = LogStore()
    record = store.append_many([row])[0]
    assert record.event_key == key
    assert record.level == "INFO"
    assert record.category == "connection"
    assert record.metadata["observed_level"] == "WARNING"
    assert record.message_raw == message
    assert not record.is_key_event
    assert store.snapshot(tab="key") == []
    assert store.snapshot(levels=["WARNING", "ERROR", "CRITICAL"]) == []
    assert store.snapshot() == [record]
    assert store.catalog_gap_count == 0


@pytest.mark.parametrize("message,level,expected", [
    (TIMEOUT, "ERROR", "ERROR"), (TIMEOUT, "CRITICAL", "CRITICAL"),
    (TIMEOUT, "FATAL", "CRITICAL"),
    ("a previously unseen core warning", "WARNING", "WARNING"),
    ("listen tcp 127.0.0.1:7890: bind: address already in use", "WARNING", "ERROR"),
    ("configuration invalid: missing proxy", "ERROR", "ERROR"),
])
def test_service_faults_and_unknown_warnings_remain_visible(message, level, expected):
    record = LogRecord.from_mapping(captured(message, level))
    assert record.level == expected
    assert record.is_key_event
    assert record.message_raw == message


def test_historical_gap_is_reclassified_without_changing_original_file(tmp_path):
    old = {
        "source": "MIHOMO", "level": "WARNING", "message_raw": TIMEOUT,
        "event_key": "mihomo.uncategorized_warning", "category": "runtime",
        "metadata": {"event_match_kind": "mihomo_safe_fallback", "is_key_event": True},
    }
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps(old), encoding="utf-8")
    original = path.read_bytes()
    direct = LogRecord.from_mapping(old)
    seeded = LogRecord.from_mapping(seed_structured_event_log(path)[0])
    for record in (direct, seeded):
        assert record.event_key == "mihomo.connection_timeout"
        assert record.level == "INFO"
        assert record.metadata["observed_level"] == "WARNING"
        assert record.metadata["observed_event_key"] == "mihomo.uncategorized_warning"
        assert record.message_raw == TIMEOUT
        assert not record.is_key_event
    assert path.read_bytes() == original


@pytest.mark.parametrize("observed", ["ERROR", "CRITICAL"])
def test_historical_original_error_is_never_demoted(observed):
    old = {
        "source": "MIHOMO", "level": "WARNING", "message_raw": TIMEOUT,
        "event_key": "mihomo.uncategorized_warning",
        "metadata": {"observed_level": observed},
    }
    for row in (old, captured(**{k: v for k, v in old.items() if k not in ("source", "level", "message_raw")})):
        record = LogRecord.from_mapping(row)
        assert record.level == observed
        assert record.metadata["observed_level"] == observed
        assert record.is_key_event


def test_live_diagnostics_survive_sidecar_and_restart_without_duplication(tmp_path):
    path = tmp_path / "events.jsonl"
    logger = logging.Logger("connection-diagnostics", level=logging.DEBUG)
    sidecar = StructuredEventJsonHandler(path)
    logger.addHandler(sidecar)
    queue = LogCaptureQueue()
    queue.bind_external_event_logger(logger)
    try:
        queue.put_nowait({
            "source": "MIHOMO", "level": "WARNING", "message_raw": TIMEOUT,
            "timestamp": "2026-09-07T11:28:01.952+08:00",
            "metadata": {"capture_kind": "mihomo_stream"},
        })
        sidecar.flush()
        live = queue.drain()[0]
        seeded = seed_structured_event_log(path)
        assert len(seeded) == 1
        queue.put_nowait(seeded[0])
        sidecar.flush()
        assert len(seed_structured_event_log(path)) == 1
        for row in (live, seeded[0], queue.drain()[0]):
            assert row["message_raw"] == TIMEOUT
            assert row["level"] == "INFO"
            assert row["metadata"]["observed_level"] == "WARNING"
            assert not row["is_key_event"]
        assert queue.sidecar_failure_count == 0
    finally:
        logger.removeHandler(sidecar)
        sidecar.close()


def test_stdout_fallback_keeps_the_same_diagnostic_policy():
    tracker = MihomoStdoutFallbackTracker()
    row = tracker.consume_once(
        1, "2026-09-07 11:28:01,952 [WARNING] " + TIMEOUT.splitlines()[0],
    )[0]
    assert row["event_key"] == "mihomo.connection_timeout"
    assert row["level"] == "INFO"
    assert row["metadata"]["observed_level"] == "WARNING"
    assert not row["is_key_event"]


def test_unrelated_timeout_text_does_not_silence_a_core_warning():
    match = match_mihomo_event("runtime watchdog reached i/o timeout", "WARNING")
    assert match.event_key == "mihomo.uncategorized_warning"


@pytest.mark.parametrize("level,expected,key", [
    ("warning", "INFO", "mihomo.connection_timeout"),
    ("error", "ERROR", "mihomo.uncategorized_error"),
    ("critical", "CRITICAL", "mihomo.uncategorized_critical"),
])
def test_real_core_logfmt_preserves_severity_and_multiline_evidence(level, expected, key):
    raw_line = f'time="2026-09-07T11:28:01+08:00" level={level} msg={json.dumps(TIMEOUT)}'
    row = MihomoStdoutFallbackTracker().consume_once(1, raw_line)[0]
    record = LogRecord.from_mapping(row)
    assert record.event_key == key
    assert record.level == expected
    assert record.message_raw == TIMEOUT
    assert record.metadata["raw_line"] == raw_line
    assert record.is_key_event == (level != "warning")
    if level == "warning":
        assert record.metadata["observed_level"] == "WARNING"
