from __future__ import annotations

import json
import logging
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import Mock

from log_event_reports import (
    LOG_EVENT_SPECS,
    MIHOMO_PATTERN_CATALOG_VERSION,
    MIHOMO_PATTERN_CASE_IDS,
    LogEventReport,
    build_log_event_report,
    log_event,
    log_raw_diagnostic,
    match_core_runner_event,
    match_mihomo_event,
    sanitize_event_params,
)
from log_capture import LogCaptureQueue
from log_store import LogRecord, LogStore


ALERT_EVENT_KEYS = {
    "mode.recovery.vanguard",
    "mode.tier1_degraded",
    "mode.recovery.sentinel",
    "mode.tier2_active",
    "mode.tier2_active.backup_reminder",
    "mode.tier3_active",
    "mode.offline",
    "tier1.node_down",
    "tier1.protocol_fallback",
    "tier3.intervention_required",
    "kill_switch.disabled",
    "kill_switch.activated",
    "kill_switch.deactivated",
    "mode.tier2_tier3_flap",
    "kill_switch.manual_override",
    "kill_switch.manual_clear",
}


class _RecordHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class LogEventRegistryTests(unittest.TestCase):
    def test_mihomo_pattern_catalog_is_bound_to_packaged_core_version(self) -> None:
        download_script = Path("scripts/download_mihomo.ps1").read_text(encoding="utf-8")

        self.assertEqual(MIHOMO_PATTERN_CATALOG_VERSION, "1.19.27")
        self.assertIn(f'$Version = "{MIHOMO_PATTERN_CATALOG_VERSION}"', download_script)

    def test_versioned_mihomo_corpus_covers_every_registered_pattern(self) -> None:
        fixture = json.loads(
            Path("tests/fixtures/mihomo_log_corpus_1_19_27.json").read_text(encoding="utf-8")
        )
        self.assertEqual(fixture["catalog_version"], MIHOMO_PATTERN_CATALOG_VERSION)
        cases = fixture["cases"]
        case_ids = [item["case_id"] for item in cases]
        self.assertEqual(len(case_ids), len(set(case_ids)))
        self.assertEqual(
            {item["case_id"] for item in cases if item["case_id"] in MIHOMO_PATTERN_CASE_IDS},
            set(MIHOMO_PATTERN_CASE_IDS),
        )

        gaps = LogStore()
        for item in cases:
            matcher = match_core_runner_event if item["origin"] == "core_runner" else match_mihomo_event
            match = (
                matcher(item["payload"])
                if item["origin"] == "core_runner"
                else matcher(item["payload"], item["level"])
            )
            with self.subTest(case_id=item["case_id"]):
                if item["event_key"] is None:
                    self.assertIsNone(match)
                else:
                    self.assertIsNotNone(match)
                    self.assertEqual(match.event_key, item["event_key"])
                    self.assertEqual(dict(match.params), item["event_params"])
                    self.assertEqual(match.match_kind, item["match_kind"])
                    if item["case_id"] in MIHOMO_PATTERN_CASE_IDS:
                        self.assertEqual(match.case_id, item["case_id"])

                queue = LogCaptureQueue(capacity=2)
                queue.put_nowait(
                    {
                        "source": "TESSERACT" if item["origin"] == "core_runner" else "MIHOMO",
                        "level": item["level"],
                        "message_raw": item["payload"],
                    }
                )
                captured = queue.drain()[0]
                self.assertEqual(captured["level"], item["expected_effective_level"])
                if item["event_key"] is not None:
                    self.assertEqual(captured["event_key"], item["event_key"])
                    report = build_log_event_report(captured)
                    rendered = f"{report.headline} {report.detail}"
                    self.assertNotEqual(rendered.strip(), item["payload"].strip())
                    self.assertNotIn("://", rendered)
                    self.assertNotIn("C:/", rendered)
                    gaps.append_many((captured,))

        self.assertEqual(gaps.catalog_gap_count, 2)

    def test_registry_keys_are_unique_and_cover_all_alert_events(self) -> None:
        self.assertEqual(len(LOG_EVENT_SPECS), len(set(LOG_EVENT_SPECS)))
        self.assertTrue(ALERT_EVENT_KEYS.issubset(LOG_EVENT_SPECS))
        for spec in LOG_EVENT_SPECS.values():
            with self.subTest(event_key=spec.event_key):
                self.assertTrue(spec.headline_template)
                self.assertTrue(set(spec.required_params).issubset(spec.allowed_params))

    def test_report_and_spec_are_immutable(self) -> None:
        report = LogEventReport("headline", "detail", True, "event_key")
        with self.assertRaises(FrozenInstanceError):
            report.headline = "changed"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            LOG_EVENT_SPECS["runtime.mihomo_started"].level = "ERROR"  # type: ignore[misc]

    def test_raw_diagnostic_helper_marks_record_without_promoting_an_event(self) -> None:
        logger = logging.getLogger("test-log-raw-diagnostic")
        logger.handlers.clear()
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
        handler = _RecordHandler()
        logger.addHandler(handler)

        log_raw_diagnostic(
            logger,
            "warning",
            "Per-node diagnostic failed: %s",
            "node-a",
            extra={"category": "update", "event_key": "health.layer_degraded"},
        )

        self.assertEqual(len(handler.records), 1)
        record = handler.records[0]
        self.assertEqual(record.levelno, logging.WARNING)
        self.assertEqual(record.getMessage(), "Per-node diagnostic failed: node-a")
        self.assertEqual(record.category, "update")
        self.assertEqual(record.event_key, "")
        self.assertEqual(record.event_match_kind, "raw_only")
        self.assertFalse(record.is_key_event)

    def test_raw_diagnostic_exception_level_defaults_to_current_exception(self) -> None:
        logger = logging.getLogger("test-log-raw-diagnostic-exception")
        logger.handlers.clear()
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
        handler = _RecordHandler()
        logger.addHandler(handler)

        try:
            raise ValueError("broken")
        except ValueError:
            log_raw_diagnostic(logger, "exception", "Diagnostic crashed")

        self.assertEqual(len(handler.records), 1)
        record = handler.records[0]
        self.assertEqual(record.levelno, logging.ERROR)
        self.assertIsNotNone(record.exc_info)
        self.assertIs(record.exc_info[0], ValueError)
        self.assertEqual(record.event_match_kind, "raw_only")
        self.assertFalse(record.is_key_event)

    def test_raw_diagnostic_uses_named_logger_method_for_string_level(self) -> None:
        logger = Mock(spec=logging.Logger)

        log_raw_diagnostic(logger, "warning", "Diagnostic %s", "detail")

        logger.warning.assert_called_once()
        logger.log.assert_not_called()
        args, kwargs = logger.warning.call_args
        self.assertEqual(args, ("Diagnostic %s", "detail"))
        self.assertEqual(kwargs["extra"]["event_match_kind"], "raw_only")
        self.assertFalse(kwargs["extra"]["is_key_event"])

    def test_counted_alert_report_uses_facts_instead_of_raw_excerpt(self) -> None:
        raw = "node-a.example:443, node-b.example:443 failed with a very long traceback"
        record = LogRecord.from_mapping(
            {
                "source": "ALERT",
                "level": "WARNING",
                "category": "health",
                "event_key": "tier1.node_down",
                "message_raw": raw,
                "metadata": {
                    "event_params": {
                        "nodes": ["node-a.example:443", "node-b.example:443"],
                    }
                },
            }
        )

        report = build_log_event_report(record)

        self.assertEqual(report.headline, "节点不可用")
        self.assertIn("不可用 2 个", report.detail)
        self.assertNotIn("node-a", report.detail)
        self.assertNotIn(raw, f"{report.headline} {report.detail}")
        self.assertTrue(report.complete)

    def test_missing_required_params_never_leave_template_placeholders(self) -> None:
        record = LogRecord.from_mapping(
            {
                "source": "TESSERACT",
                "level": "WARNING",
                "event_key": "health.layer_degraded",
                "message_raw": "raw evidence",
            }
        )

        report = build_log_event_report(record)

        self.assertFalse(report.complete)
        self.assertNotIn("{layer}", report.headline)
        self.assertNotIn("{available_count}", report.detail)
        self.assertIn("—", f"{report.headline} {report.detail}")

    def test_translation_is_applied_to_templates_before_formatting(self) -> None:
        record = LogRecord.from_mapping(
            {
                "source": "TESSERACT",
                "level": "INFO",
                "event_key": "routing.route_switched",
                "message_raw": "raw route evidence",
                "metadata": {"event_params": {"target": "Sentinel"}},
            }
        )
        translations = {
            "活动出口已切换": "Active route changed",
            "流量现在由 {target} 承载。": "Traffic is now carried by {target}.",
        }

        report = build_log_event_report(record, lambda text: translations.get(text, text))

        self.assertEqual(report.headline, "Active route changed")
        self.assertEqual(report.detail, "Traffic is now carried by Sentinel.")

    def test_unknown_event_does_not_echo_or_truncate_raw_message(self) -> None:
        raw = "secret diagnostic content that must not masquerade as a summary"
        report = build_log_event_report(
            {
                "source": "TESSERACT",
                "level": "ERROR",
                "event_key": "unknown.event",
                "message_raw": raw,
            }
        )

        self.assertEqual(report.match_kind, "unregistered")
        self.assertFalse(report.complete)
        self.assertNotIn("secret diagnostic", f"{report.headline} {report.detail}")


class EventParameterTests(unittest.TestCase):
    def test_event_params_are_whitelisted_redacted_and_aggregated(self) -> None:
        params = sanitize_event_params(
            "update.subscription_completed",
            {
                "sources": ["one", "two", "three"],
                "failed_sources": ["two"],
                "usable_nodes": ["a", "b"],
                "api_token": "secret",
                "unexpected": "must be dropped",
                "message": (
                    "see https://example.test/private ss://secret@host "
                    "C:\\Users\\name\\secret.txt C:/Users/name/key /home/name/key"
                ),
            },
        )

        self.assertEqual(params["source_total"], 3)
        self.assertEqual(params["failed_count"], 1)
        self.assertEqual(params["usable_count"], 2)
        self.assertNotIn("unexpected", params)
        self.assertNotIn("api_token", params)
        self.assertNotIn("https://", params["message"])
        self.assertNotIn("ss://", params["message"])
        self.assertNotIn("C:\\Users", params["message"])
        self.assertNotIn("C:/Users", params["message"])
        self.assertNotIn("/home/name", params["message"])

        route = sanitize_event_params("routing.route_switched", {"target": "X" * 500})
        self.assertLessEqual(len(route["target"]), 32)
        self.assertIn("…", route["target"])

    def test_log_event_attaches_only_sanitized_protocol_fields(self) -> None:
        logger = logging.Logger("event-protocol", level=logging.DEBUG)
        handler = _RecordHandler()
        logger.addHandler(handler)

        log_event(
            logger,
            logging.CRITICAL,
            "routing.route_switched",
            "full raw route record",
            category="wrong",
            params={"target": "Sentinel", "password": "do-not-capture"},
            is_key_event=False,
        )

        record = handler.records[-1]
        self.assertEqual(record.event_key, "routing.route_switched")
        self.assertEqual(record.event_params, {"target": "Sentinel"})
        self.assertTrue(record.is_key_event)
        self.assertEqual(record.category, "routing")
        self.assertEqual(record.levelname, "INFO")
        self.assertEqual(record.getMessage(), "full raw route record")


class MihomoPatternTests(unittest.TestCase):
    def test_current_core_runner_messages_match_fully_anchored_patterns(self) -> None:
        cases = {
            "Started mihomo: mihomo.exe -f config.yaml": "runtime.mihomo_started",
            "Stopped mihomo": "runtime.mihomo_stopped",
            "Killed mihomo after graceful stop timed out": "runtime.mihomo_forced_stop",
            "Hot-reloaded config via http://127.0.0.1:9090/configs": "configuration.applied",
            "Hot reload failed: connection refused": "configuration.failed",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                match = match_mihomo_event(message, "INFO")
                self.assertIsNotNone(match)
                self.assertEqual(match.event_key, expected)
                self.assertEqual(match.match_kind, "mihomo_pattern")

        self.assertIsNone(match_mihomo_event("prefix Started mihomo: core", "INFO"))
        self.assertIsNone(match_mihomo_event("Stopped mihomo suffix", "INFO"))

    def test_unknown_info_stays_raw_and_unknown_warning_has_safe_fallback(self) -> None:
        self.assertIsNone(match_mihomo_event("accepted connection", "INFO"))
        fallback = match_mihomo_event("future core warning vocabulary", "WARNING")

        self.assertIsNotNone(fallback)
        self.assertEqual(fallback.event_key, "mihomo.uncategorized_warning")
        self.assertEqual(fallback.params, {})
        self.assertEqual(fallback.match_kind, "mihomo_safe_fallback")
        error = match_mihomo_event("future core error vocabulary", "ERROR")
        critical = match_mihomo_event("future core fatal vocabulary", "FATAL")
        self.assertEqual(error.event_key, "mihomo.uncategorized_error")
        self.assertEqual(critical.event_key, "mihomo.uncategorized_critical")


class LogEventCauseTests(unittest.TestCase):
    """A failure event says why it failed instead of "open the raw log"."""

    def test_failure_with_reason_shows_cause_and_falls_back_without_one(self) -> None:
        with_reason = LogRecord.from_mapping(
            {
                "source": "TESSERACT",
                "level": "ERROR",
                "event_key": "update.subscription_failed",
                "message_raw": "Subscription refresh failed for tier2",
                "metadata": {"event_params": {"reason": "HTTP 403 from provider"}},
            }
        )
        without_reason = LogRecord.from_mapping(
            {
                "source": "TESSERACT",
                "level": "ERROR",
                "event_key": "update.subscription_failed",
                "message_raw": "Subscription refresh failed for tier2",
            }
        )

        self.assertEqual(build_log_event_report(with_reason).detail, "原因：HTTP 403 from provider")
        self.assertEqual(
            build_log_event_report(without_reason).detail,
            "本次没有获得可应用的订阅结果，现有配置保持不变。",
        )

    def test_start_failure_names_the_occupied_endpoints(self) -> None:
        record = LogRecord.from_mapping(
            {
                "source": "TESSERACT",
                "level": "ERROR",
                "event_key": "runtime.desktop_failed",
                "message_raw": "Desktop runtime start failed",
                "metadata": {
                    "event_params": {
                        "reason": "启动所需的网络资源正被占用，请停止占用后重试。",
                        "endpoints": "127.0.0.1:7890, 127.0.0.1:9090",
                    }
                },
            }
        )
        translations = {
            "启动所需的网络资源正被占用，请停止占用后重试。": "A required network resource is in use.",
            "原因：{reason} 被占用的端口：{endpoints}": "Reason: {reason} Ports in use: {endpoints}",
        }

        report = build_log_event_report(record, lambda text: translations.get(text, text))

        self.assertEqual(
            report.detail,
            "Reason: A required network resource is in use. Ports in use: 127.0.0.1:7890, 127.0.0.1:9090",
        )

    def test_all_sources_failed_summarizes_failure_classes(self) -> None:
        record = LogRecord.from_mapping(
            {
                "source": "TESSERACT",
                "level": "ERROR",
                "event_key": "update.sources_failed",
                "message_raw": "Node source refresh failed for all sources",
                "metadata": {
                    "event_params": {
                        "source_total": 5,
                        "failed_count": 5,
                        "failure_kinds": {"timeout": 3, "dns": 1, "http_403": 1},
                    }
                },
            }
        )

        report = build_log_event_report(record)

        self.assertEqual(
            report.detail,
            "5 个来源全部失败：超时 ×3, DNS 解析失败 ×1, HTTP 403 ×1。现有节点不会被空结果覆盖。",
        )

    def test_uncategorized_core_warning_shows_sanitized_core_line(self) -> None:
        record = LogRecord.from_mapping(
            {
                "source": "MIHOMO",
                "level": "WARNING",
                "message_raw": (
                    "because TIER3-AUTO failed multiple times, activate health check "
                    "https://probe.example/x C:\\Users\\user\\core.yaml"
                ),
            }
        )

        report = build_log_event_report(record)

        self.assertEqual(report.headline, "Mihomo 警告")
        self.assertTrue(report.detail.startswith("because TIER3-AUTO failed multiple times"))
        self.assertNotIn("://", report.detail)
        self.assertNotIn("Users", report.detail)

    def test_pattern_matched_core_failure_uses_its_line_as_reason(self) -> None:
        record = LogRecord.from_mapping(
            {
                "source": "MIHOMO",
                "level": "ERROR",
                "message_raw": "configuration invalid: proxy provider missing",
            }
        )

        report = build_log_event_report(record)

        self.assertEqual(report.headline, "Mihomo 配置校验失败")
        self.assertEqual(report.detail, "原因：configuration invalid: proxy provider missing")

    def test_older_failure_record_takes_its_cause_from_the_traceback(self) -> None:
        record = LogRecord.from_mapping(
            {
                "source": "TESSERACT",
                "level": "ERROR",
                "event_key": "runtime.desktop_failed",
                "message_raw": "\n".join(
                    [
                        "Desktop runtime start failed",
                        "Traceback (most recent call last):",
                        '  File "desktop_runtime.py", line 2056, in _start_locked',
                        "desktop_runtime.RuntimeListenerPreflightError: 启动所需的网络资源正被占用，请停止占用后重试。",
                    ]
                ),
            }
        )

        report = build_log_event_report(record)

        self.assertEqual(report.detail, "原因：启动所需的网络资源正被占用，请停止占用后重试。")

    def test_traceback_without_exception_message_keeps_generic_detail(self) -> None:
        record = LogRecord.from_mapping(
            {
                "source": "TESSERACT",
                "level": "ERROR",
                "event_key": "runtime.desktop_failed",
                "message_raw": "Desktop runtime start failed\nTraceback (most recent call last):\nTimeoutError",
            }
        )

        self.assertEqual(
            build_log_event_report(record).detail,
            "系统保留原有配置，请打开原始日志查看启动失败证据。",
        )

    def test_first_party_event_never_promotes_its_raw_message_to_a_cause(self) -> None:
        record = LogRecord.from_mapping(
            {
                "source": "TESSERACT",
                "level": "ERROR",
                "event_key": "configuration.failed",
                "message_raw": "internal diagnostic prose that must stay in the raw view",
            }
        )

        report = build_log_event_report(record)

        self.assertNotIn("internal diagnostic", report.detail)

    def test_cjk_sentences_are_not_compacted_as_machine_tokens(self) -> None:
        sentence = "启动所需的网络资源正被占用请停止占用后重试这是一段没有空格的很长中文说明文字用于检查"
        params = sanitize_event_params("runtime.desktop_failed", {"reason": sentence})

        self.assertEqual(params["reason"], sentence)
        token = sanitize_event_params("runtime.desktop_failed", {"reason": "k" * 48})
        self.assertIn("…", token["reason"])


class LogEventTranslationCoverageTests(unittest.TestCase):
    def test_every_event_template_and_failure_label_is_localized(self) -> None:
        from log_event_reports import _SOURCE_FAILURE_KIND_LABELS
        from ui_i18n import TRANSLATIONS_BY_LANGUAGE, TRANSLATIONS_EN, is_translation_neutral

        texts: set[str] = set(_SOURCE_FAILURE_KIND_LABELS.values())
        for spec in LOG_EVENT_SPECS.values():
            texts.update((spec.headline_template, spec.detail_template))
            texts.update(template for _names, template in spec.detail_variants)
        for text in sorted(texts):
            if is_translation_neutral(text):
                continue
            with self.subTest(text=text):
                self.assertIn(text, TRANSLATIONS_EN)
                for language, catalog in TRANSLATIONS_BY_LANGUAGE.items():
                    self.assertIn(text, catalog, language)


if __name__ == "__main__":
    unittest.main()
