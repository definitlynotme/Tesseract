from __future__ import annotations

import unittest
from datetime import datetime

from log_events import (
    append_log_record_state,
    alert_log_entry_from_item,
    alert_log_level,
    alert_record_key,
    alert_type_text,
    display_log_time,
    localized_alert_row,
    log_entry_from_text_line,
    log_entry_tree_row,
    log_entry_visible,
    log_level_filter_options,
    log_source_filter_options,
    normalize_log_level,
    normalize_log_source,
    runtime_notice_text,
    strip_log_icons,
)


class LogEventsTests(unittest.TestCase):
    def test_normalizes_log_level_and_source(self) -> None:
        self.assertEqual(normalize_log_level("warn"), "WARNING")
        self.assertEqual(normalize_log_level("critical"), "CRITICAL")
        self.assertEqual(normalize_log_level("unknown"), "INFO")
        self.assertEqual(normalize_log_source("alert"), "ALERT")
        self.assertEqual(normalize_log_source("unexpected"), "UNKNOWN")
        self.assertEqual(normalize_log_source(None), "UNKNOWN")

    def test_runtime_notice_hides_internal_source_names(self) -> None:
        translations = {
            "GeoSite 规则初始化完成，记录数: {records}": "GeoSite rule ready, records: {records}",
            "正在初始化兼容规则": "Initializing compatibility rules",
        }
        translate = lambda text: translations.get(text, text)

        geosite = runtime_notice_text(
            "Finished initial GeoSite rule google => system, records: 1082",
            translate,
        )
        compatibility = runtime_notice_text("Start initial compatible source system", translate)

        self.assertEqual(geosite, "GeoSite rule ready, records: 1082")
        self.assertNotIn("google", geosite)
        self.assertNotIn("system", geosite)
        self.assertEqual(compatibility, "Initializing compatibility rules")
        self.assertNotIn("system", compatibility)

    def test_runtime_notice_strips_decorative_log_icons(self) -> None:
        self.assertEqual(strip_log_icons("match GENERAL🔵 @proxy"), "match GENERAL @proxy")
        self.assertEqual(runtime_notice_text("✅ core ready"), "core ready")

    def test_parses_structured_text_log_line(self) -> None:
        entry = log_entry_from_text_line(
            "2026-05-30 23:01:55,123 [INFO] Initial configuration complete",
            translate=lambda text: {"初始配置完成": "Initial configuration complete"}.get(text, text),
        )

        self.assertEqual(entry["time"], "05-30 23:01:55")
        self.assertEqual(entry["level"], "INFO")
        self.assertEqual(entry["source"], "MIHOMO")
        self.assertEqual(entry["message"], "Initial configuration complete")

    def test_uses_supplied_clock_for_unstructured_lines_and_dates(self) -> None:
        now = lambda: datetime(2026, 5, 31, 7, 8, 9)

        plain = log_entry_from_text_line("plain message", now=now)
        no_date = log_entry_from_text_line("23:01:55 [WARN] warning message", now=now)

        self.assertEqual(plain["time"], "05-31 07:08:09")
        self.assertEqual(no_date["time"], "05-31 23:01:55")
        self.assertEqual(no_date["level"], "WARNING")
        self.assertEqual(display_log_time("", now=now), "05-31 07:08:09")

    def test_alert_level_mapping(self) -> None:
        self.assertEqual(alert_log_level("KILL_SWITCH_ON"), "ERROR")
        self.assertEqual(alert_log_level("CRITICAL"), "CRITICAL")
        self.assertEqual(alert_log_level("BACKUP_REMINDER"), "WARNING")
        self.assertEqual(alert_log_level("RECOVERY"), "INFO")

    def test_alert_log_entry_from_item_matches_desktop_format(self) -> None:
        item = {
            "timestamp": "2026-05-30T10:00:00+00:00",
            "type": "CUSTOM",
            "title": "Title",
            "message": "Message\nBlock",
        }

        entry = alert_log_entry_from_item(item, display_time=lambda _value: "05-30 10:00:00")

        self.assertEqual(alert_record_key(item), "2026-05-30T10:00:00+00:00|CUSTOM|Title|Message\nBlock")
        self.assertEqual(
            entry,
            {
                "time": "05-30 10:00:00",
                "level": "INFO",
                "source": "ALERT",
                "message": "CUSTOM - Title: Message Block",
            },
        )

    def test_alert_log_entry_omits_empty_title_separator(self) -> None:
        entry = alert_log_entry_from_item(
            {"timestamp": "", "type": "CUSTOM", "title": "", "message": "Message"},
            display_time=lambda _value: "05-31 07:08:09",
        )

        self.assertEqual(entry["message"], "CUSTOM - --: Message")

    def test_alert_type_text_and_row_localization(self) -> None:
        translations = {
            "熔断开启": "Kill switch on",
            "Mihomo 已启动": "Mihomo started",
            "初始配置完成": "Initial configuration complete",
        }
        translate = lambda text: translations.get(text, text)

        self.assertEqual(alert_type_text("KILL_SWITCH_ON", translate), "Kill switch on")
        self.assertEqual(alert_type_text("CUSTOM", translate), "CUSTOM")
        self.assertEqual(
            localized_alert_row(
                ["2026-05-30T10:00:00+00:00", "KILL_SWITCH_ON", "Started mihomo", "Initial configuration complete"],
                translate,
            ),
            ["2026-05-30T10:00:00+00:00", "Kill switch on", "Mihomo started", "Initial configuration complete"],
        )

    def test_filter_matches_level_source_text_and_source_label(self) -> None:
        entry = {"time": "05-30 23:01:55", "level": "WARNING", "source": "ALERT", "message": "DNS server failed"}
        translate = lambda text: {"运行告警": "Runtime Alerts"}.get(text, text)

        self.assertTrue(log_entry_visible(entry, selected_level="WARNING", selected_source="ALERT", needle="runtime", translate=translate))
        self.assertFalse(log_entry_visible(entry, selected_level="INFO", selected_source="ALERT"))
        self.assertFalse(log_entry_visible(entry, selected_level="WARNING", selected_source="MIHOMO"))

    def test_log_tree_row_prefixes_non_mihomo_sources_and_normalizes_level(self) -> None:
        translate = lambda text: {"运行告警": "Runtime Alerts"}.get(text, text)

        self.assertEqual(
            log_entry_tree_row(
                {"time": "05-30 23:01:55", "level": "warn", "source": "ALERT", "message": "DNS server failed"},
                translate=translate,
            ),
            ("05-30 23:01:55", "WARNING", "[Runtime Alerts] DNS server failed"),
        )
        self.assertEqual(
            log_entry_tree_row({"time": "05-30 23:01:55", "level": "info", "source": "MIHOMO", "message": "ready"}),
            ("05-30 23:01:55", "INFO", "ready"),
        )

    def test_log_filter_options_preserve_all_labels_and_values(self) -> None:
        translate = lambda text: {"运行告警": "Runtime Alerts"}.get(text, text)

        self.assertEqual(log_level_filter_options(all_label="All Levels")[0], ("All Levels", "ALL"))
        self.assertEqual(log_level_filter_options(all_label="All Levels")[-1], ("CRITICAL", "CRITICAL"))
        self.assertEqual(log_source_filter_options(all_label="All Types", translate=translate)[0], ("All Types", "ALL"))
        self.assertIn(("Runtime Alerts", "ALERT"), log_source_filter_options(all_label="All Types", translate=translate))

    def test_append_log_record_state_normalizes_and_trims_records(self) -> None:
        lines, records, entry, trimmed = append_log_record_state(
            ["old"],
            [{"time": "old", "level": "INFO", "source": "MIHOMO", "message": "old"}],
            {"time": "05-30 23:01:55", "level": "warn", "source": "connection", "message": "connected"},
            max_items=2,
            keep_items=1,
        )

        self.assertFalse(trimmed)
        self.assertEqual(lines[-1], "05-30 23:01:55 [WARNING] connected")
        self.assertEqual(entry["level"], "WARNING")
        self.assertEqual(entry["source"], "CONNECTION")
        self.assertEqual(records[-1], entry)

        lines, records, _entry, trimmed = append_log_record_state(
            lines,
            records,
            {"time": "05-30 23:01:56", "level": "critical", "source": "alert", "message": "down"},
            max_items=2,
            keep_items=1,
        )

        self.assertTrue(trimmed)
        self.assertEqual(lines, ["05-30 23:01:56 [CRITICAL] down"])
        self.assertEqual(records, [{"time": "05-30 23:01:56", "level": "CRITICAL", "source": "ALERT", "message": "down"}])


if __name__ == "__main__":
    unittest.main()
