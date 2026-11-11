from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alert_config import (
    COST_SENSITIVE_ALERT_TYPES,
    default_notification_channel_config,
    normalize_alert_type_filters,
)
from alert_delivery import AlertDeliveryStore
from alert_messages import (
    alert_message_translation_sources,
    localize_alert_severity,
    localize_alert_text,
    localized_alert_field_labels,
)
from alerts import AlertEvent, AlertManager
from manager import KILL_SWITCH_ALERT_MESSAGE
from manager_alert_policy import (
    kill_switch_activated_event,
    mode_transition_events,
    tier1_protocol_fallback_event,
)
from models import AlertType
from ui_i18n import TRANSLATIONS_BY_LANGUAGE, TRANSLATIONS_EN


class _FakeResponse:
    def __init__(self, body: bytes = b'{"ok": true}') -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _runtime_event(alert_type: AlertType, title: str, message: str) -> AlertEvent:
    return AlertEvent(
        event_key="test.event",
        alert_type=alert_type,
        title=title,
        message=message,
        event_group="route_health",
    )


class AlertMessageLocalizationTests(unittest.TestCase):
    def test_english_text_is_returned_unchanged_for_english_and_unknown_text(self) -> None:
        self.assertEqual(localize_alert_text("Nodes restored", "en_US"), "Nodes restored")
        self.assertEqual(localize_alert_text("Nodes restored", ""), "Nodes restored")
        self.assertEqual(
            localize_alert_text("Totally unknown runtime text", "zh_CN"),
            "Totally unknown runtime text",
        )

    def test_chinese_and_other_locales_translate_titles_and_templates(self) -> None:
        self.assertEqual(localize_alert_text("Nodes restored", "zh_CN"), "节点已恢复")
        self.assertEqual(localize_alert_text("Available: 2/3.", "zh_CN"), "可用：2/3。")
        self.assertEqual(
            localize_alert_text("Available: 2/3.", "ja_JP"),
            TRANSLATIONS_BY_LANGUAGE["ja_JP"]["可用：{alive}/{total}。"].format(alive="2", total="3"),
        )
        self.assertEqual(localize_alert_text("Nodes restored", "zh-CN"), "节点已恢复")

    def test_bastion_listing_and_protocol_fallback_messages_localize(self) -> None:
        events = mode_transition_events(
            new_mode="tier3_active",
            previous_mode="tier2_active",
            metrics={"tier1_alive": 0, "tier1_total": 2, "tier2_alive": 0},
            tier3_listing="node-a@1.2.3.4:443, No healthy fallback nodes",
            tier3_warning_banner=(
                "Fallback nodes are public. Do not sign in, enter credentials, "
                "or access private data."
            ),
        )
        localized = localize_alert_text(events[0].message, "zh_CN")
        self.assertTrue(localized.startswith("可用应急节点：node-a@1.2.3.4:443, 没有健康的应急节点。"))
        self.assertIn("应急节点为公共节点", localized)

        fallback = tier1_protocol_fallback_event(
            [
                {"node": "hk-1", "failed_protocols": ["VLESS"], "fallback_protocol": "Trojan"},
                {"node": "jp-2", "failed_protocols": ["VMess", "TUIC"], "fallback_protocol": "Hysteria2"},
            ]
        )
        self.assertEqual(
            localize_alert_text(fallback.message, "zh_CN"),
            "hk-1：VLESS 失败，已回退到 Trojan。 jp-2：VMess, TUIC 失败，已回退到 Hysteria2。",
        )

    def test_kill_switch_alert_is_a_plain_sentence_with_optional_reason(self) -> None:
        self.assertNotIn("+---", KILL_SWITCH_ALERT_MESSAGE)
        self.assertNotIn("\n", KILL_SWITCH_ALERT_MESSAGE)
        event = kill_switch_activated_event(f"{KILL_SWITCH_ALERT_MESSAGE}\n\nReason: egress probe failed")
        localized = localize_alert_text(event.message, "zh_CN")
        self.assertTrue(localized.startswith("所有代理线路均不可用。"))
        self.assertTrue(localized.endswith("原因：egress probe failed"))
        self.assertEqual(localize_alert_text(event.title, "zh_CN"), "熔断保护已启动")

    def test_severity_and_field_labels_follow_the_language(self) -> None:
        self.assertEqual(localize_alert_severity("CRITICAL", "en_US", fallback="Critical"), "Critical")
        self.assertEqual(localize_alert_severity("CRITICAL", "zh_CN"), "严重")
        self.assertEqual(localize_alert_severity("TEST", "zh_CN"), "测试")
        self.assertEqual(localized_alert_field_labels("en_US"), ("Severity", "Time"))
        self.assertEqual(localized_alert_field_labels("zh_CN"), ("级别", "时间"))

    def test_every_message_source_has_complete_locale_coverage(self) -> None:
        sources = set(alert_message_translation_sources())
        missing_english = sorted(source for source in sources if source not in TRANSLATIONS_EN)
        self.assertFalse(missing_english, missing_english)
        for language, catalog in TRANSLATIONS_BY_LANGUAGE.items():
            if language == "en_US":
                continue
            missing = sorted(source for source in sources if source not in catalog)
            self.assertFalse(missing, f"{language}: {missing}")


class AlertManagerLanguageTests(unittest.TestCase):
    def _manager(self, config: dict, language: str) -> tuple[AlertManager, list]:
        requests: list = []

        def fake_urlopen(request, timeout=0):
            requests.append(request)
            return _FakeResponse()

        manager = AlertManager(config, logging.getLogger("test-alert-language"), language=language)
        self.addCleanup(manager.close, 1.0)
        patcher = patch("urllib.request.urlopen", fake_urlopen)
        patcher.start()
        self.addCleanup(patcher.stop)
        return manager, requests

    def test_runtime_alerts_stay_local_even_with_enabled_channels(self) -> None:
        # Alert redesign 10-30: notices are local only. A runtime alert stays
        # in the history and the log; no configured channel receives it.
        manager, requests = self._manager(
            {
                "channels": [
                    {
                        "id": "phone",
                        "type": "ntfy",
                        "enabled": True,
                        "config": {"enabled": True, "server": "https://ntfy.sh", "topic": "ops", "retries": 0},
                    }
                ],
                "telegram": {"enabled": True, "bot_token": "token", "chat_id": "chat", "retries": 0},
            },
            "zh_CN",
        )
        self.assertTrue(manager.send_event(_runtime_event(AlertType.RECOVERY, "Nodes restored", "Nodes are healthy again.")))
        self.assertTrue(manager.wait_for_pending(timeout=3))
        self.assertEqual(requests, [])
        self.assertEqual(manager.recent(limit=1)[-1]["title"], "Nodes restored")

    def test_test_channel_entry_forces_enabled_and_records_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AlertDeliveryStore(Path(temp_dir) / "history.json")
            requests: list = []

            def fake_urlopen(request, timeout=0):
                requests.append(request)
                return _FakeResponse()

            manager = AlertManager({}, logging.getLogger("test-alert-entry"), delivery_store=store, language="zh_CN")
            entry = {
                "key": "draft-ntfy",
                "type": "ntfy",
                "config": {"enabled": False, "server": "https://ntfy.sh", "topic": "draft", "retries": 0},
            }
            with patch("urllib.request.urlopen", fake_urlopen):
                ok, error = manager.test_channel_entry(entry, "测试提醒", "正文")
            self.assertTrue(ok, error)
            self.assertEqual(requests[0].full_url, "https://ntfy.sh/draft")
            self.assertEqual(requests[0].data.decode("utf-8"), "正文")
            record = store.recent(limit=1, channel_id="draft-ntfy")[-1]
            self.assertEqual(record["outcome"], "success")
            # The caller's draft is not mutated.
            self.assertFalse(entry["config"]["enabled"])
            health = store.health_for_entry({"key": "draft-ntfy", "type": "ntfy", "config": {**entry["config"], "enabled": True}})
            self.assertEqual(health.status, "submitted")
            self.assertTrue(health.verified)


class CostChannelDefaultTests(unittest.TestCase):
    def test_cost_channels_default_to_urgent_only(self) -> None:
        for channel in ("sms_twilio", "pushover", "email_sendgrid", "whatsapp"):
            with self.subTest(channel=channel):
                self.assertEqual(
                    normalize_alert_type_filters(None, channel),
                    set(COST_SENSITIVE_ALERT_TYPES),
                )
                self.assertEqual(
                    default_notification_channel_config(channel)["alert_types"],
                    list(COST_SENSITIVE_ALERT_TYPES),
                )
        for channel in ("ntfy", "telegram", "desktop", "gotify", "slack"):
            with self.subTest(channel=channel):
                self.assertEqual(normalize_alert_type_filters(None, channel), set())
                self.assertNotIn("alert_types", default_notification_channel_config(channel))

    def test_explicit_empty_scope_still_means_all_severities(self) -> None:
        self.assertEqual(normalize_alert_type_filters([], "pushover"), set())


class DeliveryHealthDroppedRecordTests(unittest.TestCase):
    def test_dropped_records_do_not_flag_a_channel_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AlertDeliveryStore(Path(temp_dir) / "history.json")
            config = {"enabled": True}
            entry = {"key": "desktop", "type": "desktop", "config": config}
            store.record(channel_id="desktop", channel_type="desktop", config=config, outcome="success")
            store.record(
                channel_id="desktop",
                channel_type="desktop",
                config=config,
                outcome="dropped",
                error="notification_not_visible",
            )
            health = store.health_for_entry(entry)
            self.assertEqual(health.status, "submitted")
            self.assertEqual(health.last_outcome, "dropped")
            self.assertEqual(health.last_error, "")
            self.assertTrue(health.verified)

            store.delete_channel("desktop")
            store.record(channel_id="desktop", channel_type="desktop", config=config, outcome="dropped")
            self.assertEqual(store.health_for_entry(entry).status, "unverified")

            store.record(channel_id="desktop", channel_type="desktop", config=config, outcome="failed", error="boom")
            store.record(channel_id="desktop", channel_type="desktop", config=config, outcome="dropped")
            health = store.health_for_entry(entry)
            self.assertEqual(health.status, "failed")
            self.assertEqual(health.last_error, "boom")


if __name__ == "__main__":
    unittest.main()
