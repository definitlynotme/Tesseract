import json
import unittest
from unittest import mock

from alert_config import RETIRED_ALERT_CHANNEL_TYPES, SUPPORTED_ALERT_CHANNEL_TYPES
from notification_onboarding import (
    NotificationOnboardingService,
    OnboardingError,
    list_onboarding_options,
    notification_channel_public_state,
)


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return self.body


class _TokenFactory:
    def __init__(self, *values: str) -> None:
        self.values = list(values)

    def __call__(self, _size: int) -> str:
        if self.values:
            return self.values.pop(0)
        return "fallback-token"


class NotificationOnboardingTests(unittest.TestCase):
    def test_catalog_contains_only_release_supported_channels(self) -> None:
        options = {item["type"]: item for item in list_onboarding_options()}

        self.assertEqual(set(options), set(SUPPORTED_ALERT_CHANNEL_TYPES))
        self.assertEqual(len(options), 15)
        self.assertTrue(set(RETIRED_ALERT_CHANNEL_TYPES).isdisjoint(options))
        self.assertEqual(options["telegram"]["modes"][0]["mode"], "bot_start_qr")
        for channel_type in (
            "desktop",
            "sound",
            "whatsapp",
            "slack",
            "discord",
            "teams",
            "google_chat",
            "gotify",
            "email_smtp",
            "email_sendgrid",
            "sms_twilio",
            "generic_webhook",
        ):
            self.assertEqual(options[channel_type]["modes"], [], channel_type)

        serialized = json.dumps(options, sort_keys=True)
        self.assertNotIn("experimental", serialized)
        self.assertNotIn("external_broker", serialized)
        self.assertNotIn("oauth_loopback", serialized)
        self.assertNotIn("guided_", serialized)

    def test_auxiliary_qr_modes_are_not_completion_flows(self) -> None:
        options = {item["type"]: item for item in list_onboarding_options()}

        for channel_type in ("ntfy", "pushover"):
            mode = options[channel_type]["modes"][0]
            self.assertTrue(mode["auxiliary"])
            self.assertEqual(mode["automatic"], "auxiliary")
            self.assertEqual(mode["result_kind"], "auxiliary_link")

    def test_public_state_redacts_secrets_and_diagnoses_retired_entries(self) -> None:
        state = notification_channel_public_state(
            {
                "channels": [
                    {
                        "id": "ops-slack",
                        "type": "slack",
                        "enabled": True,
                        "config": {
                            "enabled": True,
                            "url": "https://hooks.slack.test/services/secret-token",
                            "headers": {"Authorization": "Bearer header-secret"},
                        },
                    },
                    {
                        "id": "old-apprise",
                        "type": "apprise",
                        "enabled": True,
                        "config": {
                            "enabled": True,
                            "urls": ["json://retired-secret"],
                        },
                    },
                ]
            }
        )

        serialized = json.dumps(state, sort_keys=True)
        self.assertNotIn("secret-token", serialized)
        self.assertNotIn("header-secret", serialized)
        self.assertNotIn("retired-secret", serialized)
        slack = next(item for item in state if item["id"] == "ops-slack")
        self.assertEqual(slack["summary"], {"webhook_host": "hooks.slack.test"})
        self.assertTrue(slack["secret_present"])
        retired = next(item for item in state if item["id"] == "old-apprise")
        self.assertEqual(retired["status"], "retired")
        self.assertFalse(retired["enabled"])
        self.assertTrue(retired["requested_enabled"])
        self.assertEqual(retired["diagnostic"], "retired_channel_type")

    def test_telegram_qr_poll_and_complete_discovers_chat_id(self) -> None:
        requests = []

        def fake_urlopen(request, timeout=0):
            requests.append(request)
            if request.full_url.endswith("/getMe"):
                return _FakeResponse(
                    b'{"ok": true, "result": {"username": "TesseractBot"}}'
                )
            if "getUpdates" in request.full_url:
                return _FakeResponse(
                    b'{"ok": true, "result": [{"update_id": 10, "message": '
                    b'{"text": "/start tesseract_nonce", "chat": '
                    b'{"id": 12345, "title": "Ops"}}}]}'
                )
            raise AssertionError(f"unexpected URL {request.full_url}")

        service = NotificationOnboardingService(
            urlopen=fake_urlopen,
            token_factory=_TokenFactory("session", "nonce"),
        )

        started = service.start_onboarding(
            "telegram",
            "bot_start_qr",
            {"bot_token": "secret-token", "enabled": True},
        )
        self.assertEqual(started["status"], "pending")
        self.assertEqual(
            started["qr_payload"],
            "https://t.me/TesseractBot?start=tesseract_nonce",
        )
        self.assertTrue(started["qr_available"])
        self.assertEqual(started["result_kind"], "bot_pairing")
        self.assertTrue(started["can_poll"])
        self.assertFalse(started["can_complete"])
        self.assertFalse(started["auxiliary"])
        self.assertNotIn("secret-token", json.dumps(started, sort_keys=True))

        polled = service.poll_onboarding("session")
        self.assertEqual(polled["status"], "ready")
        self.assertFalse(polled["can_poll"])
        self.assertTrue(polled["can_complete"])

        payload: dict = {}
        completed = service.complete_onboarding(
            "session",
            payload,
            {"enabled": True},
        )

        self.assertEqual(completed["channel_id"], "telegram")
        self.assertEqual(payload["alerts"]["telegram"]["chat_id"], "12345")
        self.assertEqual(
            payload["alerts"]["telegram"]["bot_token"],
            "secret-token",
        )
        self.assertTrue(payload["alerts"]["telegram"]["enabled"])
        self.assertTrue(completed["channel"]["enabled"])
        self.assertNotIn("secret-token", json.dumps(completed, sort_keys=True))
        self.assertEqual(len(requests), 2)

    def test_ntfy_qr_is_auxiliary_and_never_writes_or_enables(self) -> None:
        service = NotificationOnboardingService(
            token_factory=_TokenFactory("session", "nonce", "topicseed")
        )
        started = service.start_onboarding(
            "ntfy",
            "qr_subscribe",
            {
                "id": "phone-push",
                "server": "https://ntfy.sh",
                "token": "ntfy-secret",
                "enabled": True,
            },
        )

        self.assertEqual(
            started["qr_payload"],
            "https://ntfy.sh/tesseract-topicseed",
        )
        self.assertTrue(started["qr_available"])
        self.assertTrue(started["auxiliary"])
        self.assertFalse(started["can_complete"])
        self.assertEqual(
            set(started["allowed_actions"]),
            {"open_link", "copy_link", "cancel"},
        )
        self.assertNotIn("ntfy-secret", json.dumps(started, sort_keys=True))

        payload: dict = {}
        with self.assertRaisesRegex(OnboardingError, "do not configure or enable"):
            service.complete_onboarding("session", payload)
        self.assertEqual(payload, {})

    def test_ntfy_topic_is_normalized_to_official_constraints(self) -> None:
        service = NotificationOnboardingService(
            token_factory=_TokenFactory("session", "nonce")
        )
        started = service.start_onboarding(
            "ntfy",
            "qr_subscribe",
            {
                "server": "https://ntfy.sh",
                "topic": "ops.prod-" + ("x" * 80),
            },
        )

        topic = started["qr_payload"].rsplit("/", 1)[-1]
        self.assertEqual(len(topic), 64)
        self.assertNotIn(".", topic)
        self.assertRegex(topic, r"\A[-_A-Za-z0-9]{1,64}\Z")

    def test_pushover_subscription_qr_is_auxiliary(self) -> None:
        service = NotificationOnboardingService(
            token_factory=_TokenFactory("session", "nonce")
        )
        started = service.start_onboarding(
            "pushover",
            "subscription_qr",
            {
                "subscription_code": "subcode",
                "app_token": "must-not-be-saved",
                "enabled": True,
            },
        )

        self.assertEqual(
            started["qr_payload"],
            "https://pushover.net/subscribe/subcode",
        )
        self.assertTrue(started["auxiliary"])
        self.assertFalse(started["can_complete"])
        payload: dict = {}
        with self.assertRaises(OnboardingError):
            service.complete_onboarding("session", payload)
        self.assertEqual(payload, {})

    def test_manual_only_channels_reject_removed_onboarding_modes(self) -> None:
        service = NotificationOnboardingService(
            token_factory=_TokenFactory("session", "nonce")
        )

        for channel_type, old_mode in (
            ("whatsapp", "guided_business_cloud_api"),
            ("slack", "guided_webhook"),
            ("discord", "oauth_loopback_user_app"),
            ("generic_webhook", "guided_webhook_builder"),
        ):
            with self.subTest(channel_type=channel_type):
                with self.assertRaisesRegex(
                    OnboardingError,
                    "unsupported onboarding mode",
                ):
                    service.start_onboarding(channel_type, old_mode, {})

        for retired in RETIRED_ALERT_CHANNEL_TYPES:
            with self.subTest(retired=retired):
                with self.assertRaisesRegex(
                    OnboardingError,
                    "unsupported notification channel type",
                ):
                    service.start_onboarding(retired, None, {})

    def test_qr_generation_failure_keeps_auxiliary_link_openable(self) -> None:
        service = NotificationOnboardingService(
            token_factory=_TokenFactory("session", "nonce", "topicseed")
        )

        with mock.patch(
            "notification_onboarding._qr_svg",
            return_value=(None, "qr_generator_unavailable"),
        ):
            started = service.start_onboarding(
                "ntfy",
                "qr_subscribe",
                {"server": "https://ntfy.sh"},
            )

        self.assertEqual(
            started["qr_payload"],
            "https://ntfy.sh/tesseract-topicseed",
        )
        self.assertEqual(started["open_url"], started["qr_payload"])
        self.assertFalse(started["qr_available"])
        self.assertEqual(started["qr_error"], "qr_generator_unavailable")
        self.assertFalse(started["can_complete"])


if __name__ == "__main__":
    unittest.main()
