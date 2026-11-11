import json
import logging
import threading
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
from email.header import decode_header, make_header
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from alert_delivery import AlertDeliveryStore
from alerts import (
    AlertEnvelope,
    AlertEvent,
    AlertManager,
    authoritative_log_handler_status,
    flush_logging_handlers,
    setup_logging,
)
from models import AlertType


class _FakeResponse:
    def __init__(self, body: bytes = b'{"ok": true}') -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return self.body


def _runtime_event(
    alert_type: AlertType,
    title: str,
    message: str,
    *,
    event_group: str = "route_health",
) -> AlertEvent:
    return AlertEvent(
        event_key=f"test.{alert_type.value.lower()}",
        alert_type=alert_type,
        title=title,
        message=message,
        event_group=event_group,
        source="test",
    )


class AlertManagerTests(unittest.TestCase):
    def test_setup_logging_marks_and_flushes_authoritative_file_handler(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "monitor.log"
            logger = setup_logging(str(log_path))
            try:
                file_handlers = [
                    handler
                    for handler in logger.handlers
                    if getattr(
                        handler,
                        "_tesseract_authoritative_file_handler",
                        False,
                    )
                ]

                self.assertEqual(len(file_handlers), 1)
                self.assertEqual(
                    file_handlers[0]._tesseract_authoritative_log_path,
                    str(log_path.resolve()),
                )
                self.assertEqual(
                    authoritative_log_handler_status(logger, log_path),
                    {
                        "valid": True,
                        "expected_path": str(log_path.resolve()),
                        "handler_count": 1,
                        "actual_paths": [str(log_path.resolve())],
                    },
                )
                logger.info("tier3-authoritative-heartbeat-test")
                self.assertGreaterEqual(flush_logging_handlers(logger), 1)
                self.assertIn(
                    "tier3-authoritative-heartbeat-test",
                    log_path.read_text(encoding="utf-8"),
                )
            finally:
                for handler in list(logger.handlers):
                    handler.close()
                    logger.removeHandler(handler)

    def test_authoritative_log_handler_status_rejects_duplicate_or_drifted_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            expected = Path(temp_dir) / "monitor.log"
            drifted = Path(temp_dir) / "other.log"
            logger = setup_logging(str(drifted))
            duplicate = logging.FileHandler(drifted, encoding="utf-8")
            duplicate._tesseract_authoritative_file_handler = True
            duplicate._tesseract_authoritative_log_path = str(drifted.resolve())
            logger.addHandler(duplicate)
            try:
                status = authoritative_log_handler_status(logger, expected)
                self.assertFalse(status["valid"])
                self.assertEqual(status["handler_count"], 2)
            finally:
                for handler in list(logger.handlers):
                    handler.close()
                    logger.removeHandler(handler)

    def test_delivery_store_records_test_success_and_sender_exception_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AlertDeliveryStore(Path(temp_dir) / "history.json")
            manager = AlertManager(
                {"telegram": {"enabled": True, "bot_token": "secret", "chat_id": "42"}},
                logging.getLogger("test-alert-delivery-store"),
                delivery_store=store,
            )
            with patch.object(manager, "_send_telegram", return_value=(True, None)):
                self.assertEqual(manager.test_channel("telegram", "private title", "private body"), (True, None))
            records = store.recent()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["outcome"], "success")

            with patch.object(manager, "_send_telegram", side_effect=RuntimeError("bad secret")):
                ok, error = manager.test_channel("telegram", "private title", "private body")
            self.assertFalse(ok)
            self.assertEqual(error, "bad ***")
            records = store.recent()
            self.assertEqual(len(records), 2)
            self.assertEqual(records[-1]["outcome"], "failed")
            raw = (Path(temp_dir) / "history.json").read_text(encoding="utf-8")
            self.assertNotIn("private title", raw)
            self.assertNotIn("private body", raw)
            self.assertNotIn("secret", raw)

    def test_http_timeout_and_retry_limits_match_saved_config_contract(self) -> None:
        manager = AlertManager({}, logging.getLogger("test-alert-http-limits"))
        self.assertEqual(
            manager._http_settings({"timeout": 300, "retries": 10}),
            (300, 10),
        )
        self.assertEqual(
            manager._http_settings({"timeout": 999, "retries": 999}),
            (300, 10),
        )
        self.assertEqual(
            manager._http_settings({"timeout": "2.5", "retries": "10"}),
            (2.5, 10),
        )
        self.assertEqual(
            manager._http_settings({"timeout": 0.5, "retries": 0}),
            (0.5, 0),
        )

        attempts = 0

        def always_fail() -> tuple[bool, str]:
            nonlocal attempts
            attempts += 1
            return False, "failed"

        with patch.object(manager, "_sleep_before_retry", return_value=None):
            self.assertEqual(
                manager._with_retries({"retries": 10}, always_fail),
                (False, "failed"),
            )
        self.assertEqual(attempts, 11)

    def test_queue_full_records_one_dropped_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AlertDeliveryStore(Path(temp_dir) / "history.json")
            with patch("alerts.DEFAULT_ALERT_WORKERS", 1), patch(
                "alerts.DEFAULT_MAX_PENDING_SENDERS",
                1,
            ):
                manager = AlertManager(
                    {},
                    logging.getLogger("test-alert-delivery-queue"),
                    delivery_store=store,
                )
            started = threading.Event()
            release = threading.Event()
            channel = {
                "key": "ops",
                "type": "generic_webhook",
                "config": {"enabled": True, "url": "https://example.test/private"},
            }
            envelope = AlertEnvelope(
                alert_type="WARNING",
                title="private title",
                message="private body",
                timestamp="2026-07-14T00:00:00+00:00",
                event_group="protection",
            )

            def blocking_sender(*_args) -> None:
                started.set()
                release.wait(2)

            manager._send_async(
                blocking_sender,
                channel,
                envelope,
                AlertType.WARNING,
            )
            self.assertTrue(started.wait(1))
            manager._send_async(
                blocking_sender,
                channel,
                envelope,
                AlertType.WARNING,
            )

            records = store.recent()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["outcome"], "dropped")
            release.set()
            self.assertTrue(manager.close(timeout=2))

    def test_send_event_suppresses_kill_switch_disabled_once_per_runtime(self) -> None:
        manager = AlertManager(
            {"desktop": {"enabled": False}},
            logging.getLogger("test-alerts-policy-run-once"),
        )
        event = AlertEvent(
            event_key="kill_switch.disabled",
            alert_type=AlertType.KILL_SWITCH_DISABLED,
            title="Kill switch unavailable",
            message="Run as admin.",
            source="test",
        )

        self.assertTrue(manager.send_event(event))
        self.assertFalse(manager.send_event(event))

        records = manager.recent()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["type"], AlertType.KILL_SWITCH_DISABLED.value)
        self.assertEqual(records[0]["event_key"], "kill_switch.disabled")
        self.assertEqual(records[0]["event_group"], "")

    def test_send_event_rejects_missing_event_key(self) -> None:
        manager = AlertManager(
            {"desktop": {"enabled": True}},
            logging.getLogger("test-alerts-missing-event-key"),
        )

        self.assertFalse(
            manager.send_event(
                AlertEvent(
                    event_key="",
                    alert_type=AlertType.WARNING,
                    title="No key",
                    message="Must not dispatch",
                )
            )
        )
        self.assertEqual(manager.recent(), [])

    def test_configured_event_group_filter_rejects_missing_or_unknown_group(
        self,
    ) -> None:
        sent: list[str] = []
        manager = AlertManager(
            {
                "telegram": {
                    "enabled": True,
                    "bot_token": "token",
                    "chat_id": "chat",
                    "event_groups": ["vanguard_nodes"],
                }
            },
            logging.getLogger("test-alerts-event-groups-fail-closed"),
        )
        manager._send_async = lambda _sender, _channel, envelope, _alert_type: sent.append(envelope.title)

        manager.send_event(
            _runtime_event(
                AlertType.WARNING,
                "Missing group",
                "missing",
                event_group="",
            )
        )
        manager.send_event(
            _runtime_event(
                AlertType.WARNING,
                "Unknown group",
                "unknown",
                event_group="not_a_real_group",
            )
        )

        self.assertEqual(sent, [])

    def test_telegram_can_allow_link_preview(self) -> None:
        requests = []

        def fake_urlopen(request, timeout=0):
            requests.append((request, timeout))
            return _FakeResponse()

        manager = AlertManager(
            {
                "telegram": {
                    "enabled": True,
                    "bot_token": "token",
                    "chat_id": "chat",
                    "disable_link_preview": False,
                }
            },
            logging.getLogger("test-alerts-telegram-preview"),
        )

        with patch("urllib.request.urlopen", fake_urlopen):
            ok, error = manager.test_channel("telegram", "Title", "https://example.com")

        self.assertTrue(ok, error)
        body = json.loads(requests[0][0].data.decode("utf-8"))
        self.assertNotIn("link_preview_options", body)

    def test_bad_channel_sections_are_treated_as_disabled(self) -> None:
        manager = AlertManager(
            {"telegram": "bad", "whatsapp": "bad", "desktop": "bad", "sound": "bad"},
            logging.getLogger("test-alerts-bad-sections"),
        )

        self.assertFalse(manager.test_channel("telegram", "Title", "Body")[0])
        self.assertFalse(manager.test_channel("whatsapp", "Title", "Body")[0])
        self.assertFalse(manager.test_channel("desktop", "Title", "Body")[0])

    def test_whatsapp_template_mode_posts_template_message(self) -> None:
        requests = []

        def fake_urlopen(request, timeout=0):
            requests.append((request, timeout))
            return _FakeResponse()

        manager = AlertManager(
            {
                "whatsapp": {
                    "enabled": True,
                    "mode": "template",
                    "access_token": "token",
                    "phone_number_id": "phone-id",
                    "to": "15551234567",
                    "graph_api_version": "v25.0",
                    "template": {
                        "name": "tesseract_alert",
                        "language": "en_US",
                        "body_params": ["{severity}", "{title}", "{message}", "{timestamp}"],
                    },
                }
            },
            logging.getLogger("test-alerts-whatsapp-template"),
        )

        with patch("urllib.request.urlopen", fake_urlopen):
            ok, error = manager.test_channel("whatsapp", "Template Title", "Template Body")

        self.assertTrue(ok, error)
        request, _timeout = requests[0]
        self.assertEqual(request.full_url, "https://graph.facebook.com/v25.0/phone-id/messages")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["type"], "template")
        self.assertEqual(body["template"]["name"], "tesseract_alert")
        self.assertEqual(body["template"]["language"], {"code": "en_US"})
        params = body["template"]["components"][0]["parameters"]
        self.assertEqual(params[0], {"type": "text", "text": "Test"})
        self.assertEqual(params[1], {"type": "text", "text": "Template Title"})
        self.assertEqual(params[2], {"type": "text", "text": "Template Body"})

    def test_whatsapp_rejects_missing_template_name(self) -> None:
        manager = AlertManager(
            {
                "whatsapp": {
                    "enabled": True,
                    "mode": "template",
                    "access_token": "token",
                    "phone_number_id": "phone-id",
                    "to": "15551234567",
                }
            },
            logging.getLogger("test-alerts-whatsapp-missing-template"),
        )

        ok, error = manager.test_channel("whatsapp", "Title", "Body")

        self.assertFalse(ok)
        self.assertIn("template name", str(error))

    def test_test_channel_reports_missing_telegram_config(self) -> None:
        manager = AlertManager(
            {"telegram": {"enabled": True, "bot_token": "", "chat_id": ""}},
            logging.getLogger("test-alerts-telegram-missing"),
        )

        ok, error = manager.test_channel("telegram", "Title", "Body")

        self.assertFalse(ok)
        self.assertIn("bot_token", str(error))

    def test_native_ui_desktop_alert_flag_blocks_external_toast(self) -> None:
        manager = AlertManager(
            {"desktop": {"enabled": True, "disabled_in_native_ui": True}},
            logging.getLogger("test-alerts-desktop-native-disabled"),
        )

        ok, error = manager.test_channel("desktop", "Title", "Body")

        self.assertFalse(ok)
        self.assertIn("native UI", str(error))

    def test_string_false_disables_external_alert_channels(self) -> None:
        requests = []

        def fake_urlopen(request, timeout=0):
            requests.append((request, timeout))
            return _FakeResponse()

        manager = AlertManager(
            {
                "telegram": {"enabled": "false", "bot_token": "token", "chat_id": "chat"},
                "whatsapp": {
                    "enabled": "false",
                    "access_token": "token",
                    "api_url": "https://example.com/messages",
                    "to": "15551234567",
                },
            },
            logging.getLogger("test-alerts-string-false"),
        )

        with patch("urllib.request.urlopen", fake_urlopen):
            manager.send_event(
                _runtime_event(AlertType.WARNING, "Title", "Body")
            )
            self.assertTrue(manager.close(timeout=2))

        self.assertEqual(requests, [])

    def test_test_channel_posts_only_requested_channel(self) -> None:
        requests = []

        def fake_urlopen(request, timeout=0):
            requests.append((request, timeout))
            return _FakeResponse()

        manager = AlertManager(
            {
                "telegram": {"enabled": True, "bot_token": "token", "chat_id": "chat"},
                "whatsapp": {
                    "enabled": True,
                    "access_token": "token",
                    "api_url": "https://example.com/messages",
                    "to": "15551234567",
                },
            },
            logging.getLogger("test-alerts-channel-test"),
        )

        with patch("urllib.request.urlopen", fake_urlopen):
            ok, error = manager.test_channel("telegram", "Title", "Body")

        self.assertTrue(ok, error)
        self.assertEqual(len(requests), 1)
        self.assertIn("api.telegram.org", requests[0][0].full_url)

    def test_test_channel_by_type_prefers_single_enabled_grouped_channel(self) -> None:
        requests = []

        def fake_urlopen(request, timeout=0):
            requests.append((request, timeout))
            return _FakeResponse()

        manager = AlertManager(
            {
                "channels": [
                    {
                        "id": "disabled-slack",
                        "type": "slack",
                        "enabled": False,
                        "config": {
                            "enabled": False,
                            "url": "https://hooks.example.test/slack/disabled",
                        },
                    },
                    {
                        "id": "enabled-slack",
                        "type": "slack",
                        "enabled": True,
                        "config": {
                            "enabled": True,
                            "url": "https://hooks.example.test/slack/enabled",
                            "retries": 0,
                        },
                    },
                ]
            },
            logging.getLogger("test-alerts-channel-test-type-enabled"),
        )

        with patch("urllib.request.urlopen", fake_urlopen):
            ok, error = manager.test_channel("slack", "Title", "Body")

        self.assertTrue(ok, error)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0][0].full_url, "https://hooks.example.test/slack/enabled")

    def test_test_channel_by_type_reports_ambiguous_grouped_channel(self) -> None:
        requests = []

        def fake_urlopen(request, timeout=0):
            requests.append((request, timeout))
            return _FakeResponse()

        manager = AlertManager(
            {
                "channels": [
                    {
                        "id": "ops-slack",
                        "type": "slack",
                        "enabled": True,
                        "config": {
                            "enabled": True,
                            "url": "https://hooks.example.test/slack/ops",
                        },
                    },
                    {
                        "id": "audit-slack",
                        "type": "slack",
                        "enabled": True,
                        "config": {
                            "enabled": True,
                            "url": "https://hooks.example.test/slack/audit",
                        },
                    },
                ]
            },
            logging.getLogger("test-alerts-channel-test-type-ambiguous"),
        )

        with patch("urllib.request.urlopen", fake_urlopen):
            ok, error = manager.test_channel("slack", "Title", "Body")

        self.assertFalse(ok)
        self.assertEqual(requests, [])
        self.assertIn("ambiguous alert channel", str(error))
        self.assertIn("channel id", str(error))

    def test_webhook_preset_channels_post_platform_payloads(self) -> None:
        cases = {
            "slack": lambda body: self.assertIn("Title", body["text"]),
            "discord": lambda body: self.assertIn("Title", body["content"]),
            "teams": lambda body: self.assertEqual(body["attachments"][0]["contentType"], "application/vnd.microsoft.card.adaptive"),
            "google_chat": lambda body: self.assertIn("Title", body["text"]),
        }

        for channel_type, assert_payload in cases.items():
            with self.subTest(channel_type=channel_type):
                requests = []

                def fake_urlopen(request, timeout=0):
                    requests.append((request, timeout))
                    return _FakeResponse()

                config = {
                    "enabled": True,
                    "url": f"https://hooks.example.test/{channel_type}/secret-token",
                    "retries": 0,
                }
                manager = AlertManager(
                    {
                        "channels": [
                            {
                                "id": f"ops-{channel_type}",
                                "type": channel_type,
                                "label": f"Ops {channel_type}",
                                "enabled": True,
                                "config": config,
                            }
                        ]
                    },
                    logging.getLogger(f"test-alerts-{channel_type}"),
                )

                with patch("urllib.request.urlopen", fake_urlopen):
                    ok, error = manager.test_channel(f"ops-{channel_type}", "Title", "Body")

                self.assertTrue(ok, error)
                request, timeout = requests[0]
                self.assertEqual(timeout, 10)
                expected_url = (
                    f"https://hooks.example.test/{channel_type}/secret-token"
                    + ("?wait=true" if channel_type == "discord" else "")
                )
                self.assertEqual(request.full_url, expected_url)
                assert_payload(json.loads(request.data.decode("utf-8")))

    def test_discord_content_is_limited_for_default_and_custom_payloads(
        self,
    ) -> None:
        for payload in (None, {"content": "{message}"}):
            with self.subTest(custom_payload=payload is not None):
                requests = []

                def fake_urlopen(request, timeout=0):
                    requests.append((request, timeout))
                    return _FakeResponse()

                config = {
                    "enabled": True,
                    "url": "https://discord.example.test/hook",
                    "message_template": "{message}",
                    "retries": 0,
                }
                if payload is not None:
                    config["payload"] = payload
                manager = AlertManager(
                    {
                        "channels": [
                            {
                                "id": "ops-discord",
                                "type": "discord",
                                "enabled": True,
                                "config": config,
                            }
                        ]
                    },
                    logging.getLogger("test-alerts-discord-limit"),
                )

                with patch("urllib.request.urlopen", fake_urlopen):
                    ok, error = manager.test_channel(
                        "ops-discord",
                        "Title",
                        "x" * 2100,
                    )

                self.assertTrue(ok, error)
                body = json.loads(requests[0][0].data.decode("utf-8"))
                self.assertEqual(len(body["content"]), 2000)
                self.assertTrue(body["content"].endswith("..."))

    def test_webhook_preset_can_use_custom_payload_template(self) -> None:
        requests = []

        def fake_urlopen(request, timeout=0):
            requests.append((request, timeout))
            return _FakeResponse()

        manager = AlertManager(
            {
                "channels": [
                    {
                        "id": "teams-workflow",
                        "type": "teams",
                        "enabled": True,
                        "config": {
                            "enabled": True,
                            "url": "https://hooks.example.test/teams",
                            "payload": {"summary": "{severity}", "details": {"title": "{title}", "message": "{message}"}},
                            "retries": 0,
                        },
                    }
                ]
            },
            logging.getLogger("test-alerts-teams-custom-payload"),
        )

        with patch("urllib.request.urlopen", fake_urlopen):
            ok, error = manager.test_channel("teams-workflow", "Custom Title", "Custom Body")

        self.assertTrue(ok, error)
        body = json.loads(requests[0][0].data.decode("utf-8"))
        self.assertEqual(body, {"summary": "Test", "details": {"title": "Custom Title", "message": "Custom Body"}})

    def test_teams_embedded_http_error_is_retried(self) -> None:
        responses = [
            _FakeResponse(
                b"Microsoft Teams endpoint returned HTTP error 429"
            ),
            _FakeResponse(b"1"),
        ]

        def fake_urlopen(_request, timeout=0):
            del timeout
            return responses.pop(0)

        manager = AlertManager(
            {
                "channels": [
                    {
                        "id": "teams-workflow",
                        "type": "teams",
                        "enabled": True,
                        "config": {
                            "enabled": True,
                            "url": "https://hooks.example.test/teams",
                            "retries": 1,
                        },
                    }
                ]
            },
            logging.getLogger("test-alerts-teams-body-retry"),
        )

        with (
            patch("urllib.request.urlopen", fake_urlopen),
            patch("time.sleep", return_value=None),
        ):
            ok, error = manager.test_channel(
                "teams-workflow",
                "Title",
                "Body",
            )

        self.assertTrue(ok, error)
        self.assertEqual(responses, [])

    def test_generic_webhook_posts_rendered_payload_and_headers(self) -> None:
        requests = []

        def fake_urlopen(request, timeout=0):
            requests.append((request, timeout))
            return _FakeResponse()

        manager = AlertManager(
            {
                "channels": [
                    {
                        "id": "ops-webhook",
                        "type": "generic_webhook",
                        "enabled": True,
                        "config": {
                            "enabled": True,
                            "url": "https://hooks.example.test/generic-secret",
                            "headers": {"X-Alert-Key": "secret-token"},
                            "payload": {"severity": "{severity}", "title": "{title}", "body": "{message}"},
                            "timeout": 7,
                            "retries": 0,
                        },
                    }
                ]
            },
            logging.getLogger("test-alerts-generic-webhook"),
        )

        with patch("urllib.request.urlopen", fake_urlopen):
            ok, error = manager.test_channel("ops-webhook", "Webhook Title", "Webhook Body")

        self.assertTrue(ok, error)
        request, timeout = requests[0]
        self.assertEqual(timeout, 7)
        self.assertEqual(request.full_url, "https://hooks.example.test/generic-secret")
        self.assertEqual(request.get_header("X-alert-key"), "secret-token")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body, {"severity": "Test", "title": "Webhook Title", "body": "Webhook Body"})

    def test_channels_list_outer_disabled_blocks_inner_enabled_config(self) -> None:
        requests = []

        def fake_urlopen(request, timeout=0):
            requests.append((request, timeout))
            return _FakeResponse()

        manager = AlertManager(
            {
                "channels": [
                    {
                        "id": "outer-disabled",
                        "type": "generic_webhook",
                        "enabled": False,
                        "config": {
                            "enabled": True,
                            "url": "https://hooks.example.test/should-not-send",
                            "retries": 0,
                        },
                    }
                ]
            },
            logging.getLogger("test-alerts-outer-disabled"),
        )

        with patch("urllib.request.urlopen", fake_urlopen):
            manager.send_event(
                _runtime_event(AlertType.WARNING, "Title", "Body")
            )
            self.assertTrue(manager.close(timeout=2))

        self.assertEqual(requests, [])

    def test_ntfy_posts_raw_body_with_headers(self) -> None:
        requests = []

        def fake_urlopen(request, timeout=0):
            requests.append((request, timeout))
            return _FakeResponse()

        manager = AlertManager(
            {
                "channels": [
                    {
                        "id": "phone-push",
                        "type": "ntfy",
                        "enabled": True,
                        "config": {
                            "enabled": True,
                            "server": "https://ntfy.sh",
                            "topic": "tesseract_alerts",
                            "token": "secret-token",
                            "priority": "5",
                            "tags": ["warning", "proxy"],
                            "retries": 0,
                        },
                    }
                ]
            },
            logging.getLogger("test-alerts-ntfy"),
        )

        with patch("urllib.request.urlopen", fake_urlopen):
            ok, error = manager.test_channel("phone-push", "Push Title", "Push Body")

        self.assertTrue(ok, error)
        request, _timeout = requests[0]
        self.assertEqual(request.full_url, "https://ntfy.sh/tesseract_alerts")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")
        self.assertEqual(request.get_header("Priority"), "5")
        self.assertEqual(request.get_header("Tags"), "warning,proxy")
        self.assertIn("Push Body", request.data.decode("utf-8"))

    def test_email_smtp_uses_starttls_login_and_message(self) -> None:
        instances = []

        class FakeSMTP:
            def __init__(self, host, port, timeout=None):
                self.host = host
                self.port = port
                self.timeout = timeout
                self.started_tls = False
                self.login_args = None
                self.messages = []
                instances.append(self)

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def starttls(self):
                self.started_tls = True

            def login(self, username, password):
                self.login_args = (username, password)

            def send_message(self, message):
                self.messages.append(message)

        manager = AlertManager(
            {
                "channels": [
                    {
                        "id": "ops-email",
                        "type": "email_smtp",
                        "enabled": True,
                        "config": {
                            "enabled": True,
                            "host": "smtp.example.test",
                            "port": 587,
                            "username": "user",
                            "password": "secret-password",
                            "from_addr": "from@example.test",
                            "to_addrs": ["to@example.test"],
                        },
                    }
                ]
            },
            logging.getLogger("test-alerts-smtp"),
        )

        with patch("smtplib.SMTP", FakeSMTP):
            ok, error = manager.test_channel("ops-email", "SMTP Title", "SMTP Body")

        self.assertTrue(ok, error)
        smtp = instances[0]
        self.assertEqual((smtp.host, smtp.port, smtp.timeout), ("smtp.example.test", 587, 10))
        self.assertTrue(smtp.started_tls)
        self.assertEqual(smtp.login_args, ("user", "secret-password"))
        self.assertEqual(smtp.messages[0]["To"], "to@example.test")
        self.assertIn("SMTP Title", smtp.messages[0]["Subject"])
        self.assertIn("SMTP Body", smtp.messages[0].get_content())

    def test_email_smtp_retries_transient_failure(self) -> None:
        attempts = []

        class FlakySMTP:
            def __init__(self, host, port, timeout=None):
                self.messages = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def starttls(self):
                return None

            def send_message(self, message):
                attempts.append(message)
                if len(attempts) == 1:
                    raise RuntimeError("temporary SMTP failure")
                self.messages.append(message)

        manager = AlertManager(
            {
                "channels": [
                    {
                        "id": "ops-email",
                        "type": "email_smtp",
                        "enabled": True,
                        "config": {
                            "enabled": True,
                            "host": "smtp.example.test",
                            "from_addr": "from@example.test",
                            "to_addrs": ["to@example.test"],
                            "retries": 1,
                        },
                    }
                ]
            },
            logging.getLogger("test-alerts-smtp-retry"),
        )

        with patch("smtplib.SMTP", FlakySMTP), patch("time.sleep", lambda _delay: None):
            ok, error = manager.test_channel("ops-email", "SMTP Title", "SMTP Body")

        self.assertTrue(ok, error)
        self.assertEqual(len(attempts), 2)

    def test_pushover_rejects_unsupported_group_key_alias(self) -> None:
        requests = []

        def fake_urlopen(request, timeout=0):
            requests.append((request, timeout))
            return _FakeResponse(b'{"status": 1}')

        manager = AlertManager(
            {
                "channels": [
                    {
                        "id": "push",
                        "type": "pushover",
                        "enabled": True,
                        "config": {
                            "enabled": True,
                            "app_token": "app-token",
                            "group_key": "group-key",
                            "retries": 0,
                        },
                    }
                ]
            },
            logging.getLogger("test-alerts-pushover-group"),
        )

        with patch("urllib.request.urlopen", fake_urlopen):
            ok, error = manager.test_channel("push", "Title", "Body")

        self.assertFalse(ok)
        self.assertIn("user_key", error or "")
        self.assertEqual(requests, [])

    def test_pushover_limits_title_and_message(self) -> None:
        requests = []

        def fake_urlopen(request, timeout=0):
            requests.append((request, timeout))
            return _FakeResponse(b'{"status": 1}')

        manager = AlertManager(
            {
                "channels": [
                    {
                        "id": "push",
                        "type": "pushover",
                        "enabled": True,
                        "config": {
                            "enabled": True,
                            "app_token": "app-token",
                            "user_key": "user-key",
                            "title_template": "{title}",
                            "message_template": "{message}",
                            "retries": 0,
                        },
                    }
                ]
            },
            logging.getLogger("test-alerts-pushover-limits"),
        )

        with patch("urllib.request.urlopen", fake_urlopen):
            ok, error = manager.test_channel(
                "push",
                "T" * 300,
                "M" * 1100,
            )

        self.assertTrue(ok, error)
        body = urllib.parse.parse_qs(requests[0][0].data.decode("utf-8"))
        self.assertEqual(len(body["title"][0]), 250)
        self.assertEqual(len(body["message"][0]), 1024)
        self.assertTrue(body["title"][0].endswith("..."))
        self.assertTrue(body["message"][0].endswith("..."))

    def test_pushover_status_zero_response_is_failure(self) -> None:
        def fake_urlopen(_request, timeout=0):
            return _FakeResponse(b'{"status": 0, "errors": ["invalid user"]}')

        manager = AlertManager(
            {
                "channels": [
                    {
                        "id": "push",
                        "type": "pushover",
                        "enabled": True,
                        "config": {
                            "enabled": True,
                            "app_token": "app-token",
                            "user_key": "user-key",
                            "retries": 0,
                        },
                    }
                ]
            },
            logging.getLogger("test-alerts-pushover-failure"),
        )

        with patch("urllib.request.urlopen", fake_urlopen):
            ok, error = manager.test_channel("push", "Title", "Body")

        self.assertFalse(ok)
        self.assertIn("invalid user", str(error))

    def test_pushover_boolean_true_does_not_masquerade_as_status_one(
        self,
    ) -> None:
        def fake_urlopen(_request, timeout=0):
            del timeout
            return _FakeResponse(b'{"status": true}')

        manager = AlertManager(
            {
                "channels": [
                    {
                        "id": "push",
                        "type": "pushover",
                        "enabled": True,
                        "config": {
                            "enabled": True,
                            "app_token": "app-token",
                            "user_key": "user-key",
                            "retries": 0,
                        },
                    }
                ]
            },
            logging.getLogger("test-alerts-pushover-bool-status"),
        )

        with patch("urllib.request.urlopen", fake_urlopen):
            ok, error = manager.test_channel("push", "Title", "Body")

        self.assertFalse(ok)
        self.assertIn("rejected", str(error).lower())

    def test_pushover_requires_status_one_response(self) -> None:
        def fake_urlopen(_request, timeout=0):
            return _FakeResponse(b"{}")

        manager = AlertManager(
            {
                "channels": [
                    {
                        "id": "push",
                        "type": "pushover",
                        "enabled": True,
                        "config": {
                            "enabled": True,
                            "app_token": "app-token",
                            "user_key": "user-key",
                            "retries": 0,
                        },
                    }
                ]
            },
            logging.getLogger("test-alerts-pushover-status-required"),
        )

        with patch("urllib.request.urlopen", fake_urlopen):
            ok, error = manager.test_channel("push", "Title", "Body")

        self.assertFalse(ok)
        self.assertIn("rejected", str(error).lower())

    def test_gotify_uses_header_token_without_query_secret(self) -> None:
        requests = []

        def fake_urlopen(request, timeout=0):
            requests.append((request, timeout))
            return _FakeResponse(b'{"id": 1}')

        manager = AlertManager(
            {
                "channels": [
                    {
                        "id": "gotify",
                        "type": "gotify",
                        "enabled": True,
                        "config": {
                            "enabled": True,
                            "server": "https://gotify.example.test/",
                            "app_token": "private-token",
                            "priority": 7,
                            "retries": 0,
                        },
                    }
                ]
            },
            logging.getLogger("test-alerts-gotify-header"),
        )

        with patch("urllib.request.urlopen", fake_urlopen):
            ok, error = manager.test_channel(
                "gotify",
                "Title",
                "Body",
            )

        self.assertTrue(ok, error)
        request, _timeout = requests[0]
        self.assertEqual(
            request.full_url,
            "https://gotify.example.test/message",
        )
        self.assertNotIn("private-token", request.full_url)
        self.assertEqual(
            request.get_header("X-gotify-key"),
            "private-token",
        )
        body = urllib.parse.parse_qs(request.data.decode("utf-8"))
        self.assertEqual(body["priority"], ["7"])
        self.assertNotIn("private-token", request.data.decode("utf-8"))

    def test_http_error_does_not_leak_telegram_token(self) -> None:
        def fake_urlopen(_request, timeout=0):
            raise urllib.error.HTTPError(
                "https://api.telegram.org/botsecret-token/sendMessage",
                401,
                "Unauthorized",
                {},
                None,
            )

        manager = AlertManager(
            {
                "telegram": {
                    "enabled": True,
                    "bot_token": "secret-token",
                    "chat_id": "chat",
                    "retries": 0,
                }
            },
            logging.getLogger("test-alerts-telegram-redaction"),
        )

        with patch("urllib.request.urlopen", fake_urlopen):
            ok, error = manager.test_channel("telegram", "Title", "Body")

        self.assertFalse(ok)
        self.assertIn("HTTP 401", str(error))
        self.assertNotIn("secret-token", str(error))

    def test_http_error_does_not_leak_webhook_url_secret(self) -> None:
        def fake_urlopen(_request, timeout=0):
            raise urllib.error.HTTPError(
                "https://hooks.example.test/webhook-secret",
                401,
                "Unauthorized",
                {},
                BytesIO(b'{"message": "https://hooks.example.test/webhook-secret"}'),
            )

        manager = AlertManager(
            {
                "channels": [
                    {
                        "id": "ops-webhook",
                        "type": "generic_webhook",
                        "enabled": True,
                        "config": {
                            "enabled": True,
                            "url": "https://hooks.example.test/webhook-secret",
                            "retries": 0,
                        },
                    }
                ]
            },
            logging.getLogger("test-alerts-webhook-redaction"),
        )

        with patch("urllib.request.urlopen", fake_urlopen):
            ok, error = manager.test_channel("ops-webhook", "Title", "Body")

        self.assertFalse(ok)
        self.assertIn("HTTP 401", str(error))
        self.assertNotIn("webhook-secret", str(error))

    def test_redaction_handles_secret_lists(self) -> None:
        secret_url = "mailto://user:secretpass@example.test"

        redacted = AlertManager._redact_text(
            f"failed to notify {secret_url}",
            {"urls": [secret_url]},
        )

        self.assertNotIn("secretpass", redacted)
        self.assertEqual(redacted, "failed to notify ***")

    def test_async_dispatch_uses_four_workers_and_caps_pending_at_64(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AlertDeliveryStore(Path(temp_dir) / "history.json")
            manager = AlertManager(
                {
                    "sender_workers": "invalid",
                    "max_pending_senders": "invalid",
                },
                logging.getLogger("test-alerts-cap"),
                delivery_store=store,
            )
            release = threading.Event()
            four_started = threading.Event()
            activity_lock = threading.Lock()
            active = 0
            max_active = 0
            channel = {
                "key": "ops",
                "type": "generic_webhook",
                "config": {
                    "enabled": True,
                    "url": "https://example.test/private",
                },
            }
            envelope = AlertEnvelope(
                alert_type="WARNING",
                title="private title",
                message="private body",
                timestamp="2026-07-14T00:00:00+00:00",
                event_group="protection",
            )

            def blocking_sender(*_args) -> None:
                nonlocal active, max_active
                with activity_lock:
                    active += 1
                    max_active = max(max_active, active)
                    if active == 4:
                        four_started.set()
                try:
                    release.wait(2)
                finally:
                    with activity_lock:
                        active -= 1

            for _index in range(70):
                manager._send_async(
                    blocking_sender,
                    channel,
                    envelope,
                    AlertType.WARNING,
                )

            self.assertTrue(four_started.wait(1))
            with manager._dispatch_condition:
                self.assertEqual(manager._pending_dispatches, 64)
                self.assertEqual(len(manager._worker_threads), 4)
            self.assertEqual(
                sum(
                    record["outcome"] == "dropped"
                    for record in store.recent()
                ),
                6,
            )

            release.set()
            self.assertTrue(manager.close(timeout=2))
            self.assertLessEqual(max_active, 4)

    def test_close_batches_many_dropped_records_with_one_slow_save(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AlertDeliveryStore(Path(temp_dir) / "history.json")
            with patch("alerts.DEFAULT_ALERT_WORKERS", 1), patch(
                "alerts.DEFAULT_MAX_PENDING_SENDERS",
                32,
            ):
                manager = AlertManager(
                    {},
                    logging.getLogger("test-alerts-close-batch"),
                    delivery_store=store,
                )
            started = threading.Event()
            release = threading.Event()
            channel = {
                "key": "ops",
                "type": "generic_webhook",
                "config": {
                    "enabled": True,
                    "url": "https://example.test/private",
                },
            }
            envelope = AlertEnvelope(
                alert_type="WARNING",
                title="private title",
                message="private body",
                timestamp="2026-07-14T00:00:00+00:00",
                event_group="protection",
            )

            def blocking_sender(*_args) -> None:
                started.set()
                release.wait(2)

            for _index in range(32):
                manager._send_async(
                    blocking_sender,
                    channel,
                    envelope,
                    AlertType.WARNING,
                )
            self.assertTrue(started.wait(1))

            original_save = store._save_locked
            save_calls = 0

            def slow_save() -> None:
                nonlocal save_calls
                save_calls += 1
                time.sleep(0.03)
                original_save()

            close_started = time.monotonic()
            with patch.object(store, "_save_locked", side_effect=slow_save):
                self.assertFalse(manager.close(timeout=0.01))
            close_elapsed = time.monotonic() - close_started

            self.assertEqual(save_calls, 1)
            self.assertLess(close_elapsed, 0.3)
            self.assertEqual(
                sum(
                    record["outcome"] == "dropped"
                    for record in store.recent()
                ),
                31,
            )
            reloaded = AlertDeliveryStore(
                Path(temp_dir) / "history.json"
            )
            self.assertEqual(
                sum(
                    record["outcome"] == "dropped"
                    for record in reloaded.recent()
                ),
                31,
            )

            release.set()
            self.assertTrue(manager.close(timeout=2))


if __name__ == "__main__":
    unittest.main()
