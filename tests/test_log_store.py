from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

from log_store import (
    LogFeedState,
    LogRecord,
    LogStore,
    LogViewWatermarkState,
    classify_log_category,
    is_key_event,
    serialize_log_records,
)
from log_event_reports import LogEventReport


BASE_TIME = datetime(2026, 7, 15, 9, 30, tzinfo=timezone.utc)


def record_mapping(
    message: str,
    *,
    offset_ms: int = 0,
    source: str = "TESSERACT",
    level: str = "INFO",
    category: str | None = None,
    **extra: object,
) -> dict[str, object]:
    result: dict[str, object] = {
        "timestamp": (BASE_TIME + timedelta(milliseconds=offset_ms)).isoformat(),
        "received_at": (BASE_TIME + timedelta(milliseconds=offset_ms + 1)).isoformat(),
        "source": source,
        "level": level,
        "message": message,
    }
    if category is not None:
        result["category"] = category
    result.update(extra)
    return result


class LogRecordTests(unittest.TestCase):
    def test_record_is_immutable_and_freezes_nested_metadata(self) -> None:
        record = LogRecord.from_mapping(
            record_mapping(
                "started",
                metadata={"tags": ["runtime"], "context": {"generation": 2}},
            )
        )

        with self.assertRaises(FrozenInstanceError):
            record.level = "ERROR"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            record.metadata["new"] = True  # type: ignore[index]
        with self.assertRaises(TypeError):
            record.metadata["context"]["generation"] = 3  # type: ignore[index]
        self.assertEqual(record.metadata["tags"], ("runtime",))

    def test_capture_aliases_preserve_critical_and_unknown_source(self) -> None:
        record = LogRecord.from_mapping(
            {
                "timestamp": BASE_TIME.isoformat(),
                "received_at": BASE_TIME.isoformat(),
                "source": "third-party",
                "type": "fatal",
                "raw_message": "traceback text",
                "event_key": "runtime-crash",
                "event_group": "runtime",
                "alert_type": "EMERGENCY",
            }
        )

        self.assertEqual(record.source, "UNKNOWN")
        self.assertEqual(record.level, "CRITICAL")
        self.assertEqual(record.message_raw, "traceback text")
        self.assertEqual(record.event_key, "runtime-crash")
        self.assertEqual(record.metadata["event_group"], "runtime")
        self.assertEqual(record.metadata["alert_type"], "EMERGENCY")

    def test_feed_state_values_are_stable(self) -> None:
        self.assertEqual(LogFeedState.NOT_STARTED.value, "not_started")
        self.assertEqual(LogFeedState.LIVE.value, "live")
        self.assertEqual(LogFeedState.RECONNECTING.value, "reconnecting")
        self.assertEqual(LogFeedState.DISCONNECTED.value, "disconnected")


class LogClassificationTests(unittest.TestCase):
    def test_key_events_require_a_registered_event_key(self) -> None:
        started = LogRecord.from_mapping(
            record_mapping("Started mihomo", category="runtime", event_key="runtime.mihomo_started")
        )
        routing = LogRecord.from_mapping(
            record_mapping(
                "Switched active exit to JP",
                event_key="routing.route_switched",
                metadata={"event_params": {"target": "JP"}},
            )
        )
        warning = LogRecord.from_mapping(record_mapping("ordinary warning", level="WARNING"))

        self.assertEqual(routing.category, "routing")
        self.assertTrue(is_key_event(started))
        self.assertTrue(routing.is_key_event)
        self.assertFalse(warning.is_key_event)

    def test_connection_rate_and_validation_churn_are_not_key_events(self) -> None:
        throughput = LogRecord.from_mapping(record_mapping("download rate=12MB/s upload rate=3MB/s"))
        validation = LogRecord.from_mapping(record_mapping("Checking candidate pool node score"))

        self.assertEqual(throughput.category, "connection")
        self.assertEqual(validation.category, "validation")
        self.assertFalse(throughput.is_key_event)
        self.assertFalse(validation.is_key_event)

    def test_event_registry_is_the_only_key_event_truth(self) -> None:
        unregistered = LogRecord.from_mapping(
            record_mapping(
                "ordinary info",
                event_key="custom.unregistered",
                metadata={"is_key_event": True},
            )
        )
        registered = LogRecord.from_mapping(
            record_mapping(
                "Started mihomo",
                level="CRITICAL",
                category="wrong",
                event_key="runtime.mihomo_started",
                metadata={"is_key_event": False},
            )
        )

        self.assertFalse(unregistered.is_key_event)
        self.assertTrue(registered.is_key_event)
        self.assertEqual(registered.level, "INFO")
        self.assertEqual(registered.category, "runtime")
        self.assertEqual(registered.metadata["observed_level"], "CRITICAL")
        self.assertEqual(registered.metadata["observed_category"], "wrong")
        self.assertFalse(registered.metadata["observed_is_key_event"])
        self.assertEqual(classify_log_category("Kill switch enabled"), "circuit_breaker")


class LogStoreTests(unittest.TestCase):
    def test_mihomo_safe_fallback_increments_catalog_gap_once_per_record(self) -> None:
        store = LogStore(now=lambda: BASE_TIME)
        fallback = record_mapping(
            "unknown severe core message",
            source="MIHOMO",
            level="ERROR",
            event_key="mihomo.uncategorized_warning",
            metadata={"event_match_kind": "mihomo_safe_fallback"},
        )

        store.append_many([fallback, dict(fallback)])

        self.assertEqual(store.catalog_gap_count, 1)
        store.reset()
        self.assertEqual(store.catalog_gap_count, 0)

    def test_assigns_monotonic_sequences_and_deduplicates_exact_fingerprint(self) -> None:
        store = LogStore(now=lambda: BASE_TIME)
        duplicate = record_mapping("same", source="MIHOMO", level="INFO")

        appended = store.append_many(
            [
                duplicate,
                dict(duplicate),
                record_mapping("same", source="TESSERACT", level="INFO"),
                record_mapping("same", source="MIHOMO", level="WARNING"),
                record_mapping("same", source="MIHOMO", level="INFO", offset_ms=1),
            ]
        )

        self.assertEqual([record.sequence for record in appended], [1, 2, 3, 4])
        self.assertEqual(len(store), 4)

    def test_structured_sidecar_seed_wins_before_matching_monitor_tail(self) -> None:
        store = LogStore(now=lambda: BASE_TIME)
        structured = record_mapping(
            "Started mihomo: C:/runtime/mihomo.exe",
            source="TESSERACT",
            level="INFO",
            event_key="runtime.mihomo_started",
            metadata={
                "capture_kind": "structured_event_tail",
                "event_match_kind": "event_key",
                "event_params": {},
            },
        )
        monitor_tail = record_mapping(
            "Started mihomo: C:/runtime/mihomo.exe",
            source="TESSERACT",
            level="INFO",
            metadata={"capture_kind": "monitor_tail"},
        )

        appended = store.append_many([structured, monitor_tail])

        self.assertEqual(len(appended), 1)
        [record] = store.snapshot(tab="raw")
        self.assertEqual(record.event_key, "runtime.mihomo_started")
        self.assertEqual(record.metadata["capture_kind"], "structured_event_tail")

    def test_dedupe_uses_millisecond_time_not_microsecond_precision(self) -> None:
        first = record_mapping("same", source="MIHOMO")
        second = dict(first)
        second["timestamp"] = "2026-07-15T09:30:00.000900+00:00"
        store = LogStore(now=lambda: BASE_TIME)

        self.assertEqual(len(store.append_many([first, second])), 1)

    def test_ten_thousand_record_burst_is_bounded_and_counts_eviction(self) -> None:
        store = LogStore(max_items=4000, now=lambda: BASE_TIME)
        entries = [record_mapping(f"line {index}", offset_ms=index) for index in range(10_000)]

        appended = store.append_many(entries)
        snapshot = store.snapshot()

        self.assertEqual(len(appended), 10_000)
        self.assertEqual(len(store), 4000)
        self.assertEqual(store.dropped_count, 6000)
        self.assertEqual(snapshot[0].sequence, 6001)
        self.assertEqual(snapshot[-1].sequence, 10_000)

    def test_key_and_raw_view_watermarks_are_independent(self) -> None:
        store = LogStore(now=lambda: BASE_TIME)
        records = store.append_many(
            [
                record_mapping("ordinary info", offset_ms=0),
                record_mapping(
                    "Started mihomo",
                    offset_ms=1,
                    category="runtime",
                    event_key="runtime.mihomo_started",
                ),
                record_mapping(
                    "route failed",
                    offset_ms=2,
                    level="ERROR",
                    event_key="routing.route_failed",
                ),
            ]
        )

        store.clear_view("key", records[-1].sequence)

        self.assertEqual(store.snapshot({"tab": "key"}), [])
        self.assertEqual(len(store.snapshot({"tab": "raw"})), 3)

        store.append_many(
            [
                record_mapping(
                    "service restarted",
                    offset_ms=3,
                    category="runtime",
                    event_key="runtime.mihomo_restarted",
                )
            ]
        )
        self.assertEqual([record.message_raw for record in store.snapshot({"tab": "key"})], ["service restarted"])

        store.clear_view("raw", store.latest_sequence)
        self.assertEqual(store.snapshot({"tab": "raw"}), [])
        self.assertEqual(len(store.snapshot({"tab": "key"})), 1)

    def test_view_watermark_state_is_immutable_and_restores_atomically(self) -> None:
        store = LogStore(now=lambda: BASE_TIME)
        store.append_many(
            [
                record_mapping("one"),
                record_mapping("two", offset_ms=1),
                record_mapping("three", offset_ms=2),
            ]
        )
        previous = store.view_watermark_state("raw")

        self.assertEqual(previous, LogViewWatermarkState(tab="raw", watermark=0, revision=0))
        with self.assertRaises(FrozenInstanceError):
            previous.watermark = 1  # type: ignore[misc]

        self.assertEqual(store.clear_view("raw"), 3)
        cleared = store.view_watermark_state("raw")
        self.assertEqual(cleared, LogViewWatermarkState(tab="raw", watermark=3, revision=1))
        self.assertTrue(
            store.restore_view_watermark(
                "raw",
                previous.watermark,
                expected_state=cleared,
            )
        )
        self.assertEqual(store.view_watermark_state("raw"), LogViewWatermarkState("raw", 0, 2))
        self.assertEqual([record.message_raw for record in store.snapshot(tab="raw")], ["one", "two", "three"])

    def test_new_records_do_not_invalidate_watermark_restore(self) -> None:
        store = LogStore(now=lambda: BASE_TIME)
        store.append_many(
            [
                record_mapping("before one"),
                record_mapping("before two", offset_ms=1),
            ]
        )
        previous = store.view_watermark_state("raw")
        store.clear_view("raw")
        cleared = store.view_watermark_state("raw")

        store.append_many([record_mapping("after clear", offset_ms=2)])

        self.assertEqual(store.view_watermark_state("raw"), cleared)
        self.assertTrue(
            store.restore_view_watermark(
                "raw",
                previous.watermark,
                expected_state=cleared,
            )
        )
        self.assertEqual(
            [record.message_raw for record in store.snapshot(tab="raw")],
            ["before one", "before two", "after clear"],
        )

    def test_later_clear_and_reset_invalidate_old_restore_tokens(self) -> None:
        store = LogStore(now=lambda: BASE_TIME)
        store.append_many([record_mapping("one")])
        store.clear_view("raw")
        first_clear = store.view_watermark_state("raw")

        store.clear_view("raw")

        second_clear = store.view_watermark_state("raw")
        self.assertEqual(second_clear.watermark, first_clear.watermark)
        self.assertEqual(second_clear.revision, first_clear.revision + 1)
        self.assertFalse(
            store.restore_view_watermark(
                "raw",
                0,
                expected_state=first_clear,
            )
        )

        before_reset_key = store.view_watermark_state("key")
        before_reset_raw = store.view_watermark_state("raw")
        store.reset()

        after_reset_key = store.view_watermark_state("events")
        after_reset_raw = store.view_watermark_state("raw")
        self.assertEqual(after_reset_key.revision, before_reset_key.revision + 1)
        self.assertEqual(after_reset_raw.revision, before_reset_raw.revision + 1)
        self.assertFalse(
            store.restore_view_watermark(
                "raw",
                0,
                expected_state=before_reset_raw,
            )
        )

    def test_watermark_revisions_are_isolated_by_normalized_tab(self) -> None:
        store = LogStore(now=lambda: BASE_TIME)
        store.append_many(
            [
                record_mapping("ordinary"),
                record_mapping(
                    "Started mihomo",
                    offset_ms=1,
                    category="runtime",
                    event_key="runtime.mihomo_started",
                ),
            ]
        )
        store.clear_view("raw")
        raw_cleared = store.view_watermark_state("raw")

        store.clear_view("events")

        self.assertEqual(store.view_watermark_state("important").tab, "key")
        self.assertEqual(store.view_watermark_state("raw"), raw_cleared)
        self.assertTrue(
            store.restore_view_watermark(
                "raw",
                0,
                expected_state=raw_cleared,
            )
        )
        self.assertFalse(
            store.restore_view_watermark(
                "key",
                0,
                expected_state=raw_cleared,
            )
        )

    def test_bounded_eviction_only_limits_records_restored_to_view(self) -> None:
        store = LogStore(max_items=2, now=lambda: BASE_TIME)
        store.append_many(
            [
                record_mapping("one"),
                record_mapping("two", offset_ms=1),
                record_mapping("three", offset_ms=2),
            ]
        )
        store.clear_view("raw")
        cleared = store.view_watermark_state("raw")

        store.append_many([record_mapping("four", offset_ms=3)])

        self.assertEqual(store.view_watermark_state("raw"), cleared)
        self.assertTrue(
            store.restore_view_watermark(
                "raw",
                0,
                expected_state=cleared,
            )
        )
        self.assertEqual([record.message_raw for record in store.snapshot(tab="raw")], ["three", "four"])
        self.assertEqual(store.dropped_count, 2)

    def test_freeze_counts_new_records_without_stopping_storage(self) -> None:
        store = LogStore(max_items=2, now=lambda: BASE_TIME)
        store.append_many([record_mapping("before")])
        store.set_frozen(True)

        store.append_many(
            [
                record_mapping("one", offset_ms=1),
                record_mapping("two", offset_ms=2),
                record_mapping("three", offset_ms=3),
            ]
        )

        self.assertEqual(store.pending_count(), 3)
        self.assertEqual([record.message_raw for record in store.snapshot()], ["two", "three"])
        store.set_frozen(False)
        self.assertEqual(store.pending_count(), 0)

    def test_snapshot_filters_source_level_category_and_search(self) -> None:
        store = LogStore(now=lambda: BASE_TIME)
        store.append_many(
            [
                record_mapping("Mihomo started", source="MIHOMO", category="runtime"),
                record_mapping("DNS route failed", source="TESSERACT", level="ERROR", category="routing", offset_ms=1),
                record_mapping("quiet", source="TESSERACT", offset_ms=2),
            ]
        )

        result = store.snapshot(
            {"source": "tesseract", "levels": ["ERROR"], "category": "routing", "query": "dns"}
        )

        self.assertEqual([record.message_raw for record in result], ["DNS route failed"])

    def test_serializes_jsonl_and_text_with_raw_fields(self) -> None:
        store = LogStore(now=lambda: BASE_TIME)
        record = store.append_many(
            [record_mapping("配置完成\nsecond line", category="configuration", event_key="config-ok")]
        )[0]

        jsonl = serialize_log_records([record], "jsonl")
        text = serialize_log_records([record], "text")
        payload = json.loads(jsonl)

        self.assertEqual(payload["source"], "TESSERACT")
        self.assertEqual(payload["level"], "INFO")
        self.assertEqual(payload["category"], "configuration")
        self.assertEqual(payload["message_raw"], "配置完成\nsecond line")
        self.assertIn("[TESSERACT] [INFO] [configuration] 配置完成\nsecond line", text)
        with self.assertRaises(ValueError):
            serialize_log_records([record], "csv")

    def test_optional_report_provider_adds_report_without_changing_default(self) -> None:
        record = LogRecord.from_mapping(
            record_mapping(
                "Started mihomo: mihomo.exe -f config.yaml",
                event_key="runtime.mihomo_started",
                category="runtime",
            )
        )
        report = LogEventReport("Mihomo 核心已启动", "代理核心已经开始提供服务。", True, "event_key")

        default_payload = json.loads(serialize_log_records([record], "jsonl"))
        report_payload = json.loads(
            serialize_log_records([record], "jsonl", report_provider=lambda _record: report)
        )
        text = serialize_log_records([record], "text", report_provider=lambda _record: report)

        self.assertNotIn("report", default_payload)
        self.assertEqual(report_payload["report"]["headline"], "Mihomo 核心已启动")
        self.assertIn("[REPORT] Mihomo 核心已启动", text)
        self.assertIn("[RAW] ", text)


if __name__ == "__main__":
    unittest.main()
