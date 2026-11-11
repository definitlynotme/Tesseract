import unittest

from alert_config import (
    ALERT_NOTIFICATION_CHANNEL_TYPES,
    ALERT_EVENT_GROUP_KEYS,
    RETIRED_ALERT_CHANNEL_TYPES,
    SUPPORTED_ALERT_CHANNEL_TYPES,
    alert_channel_accepts_event,
    alert_channel_group,
    alert_channel_has_cost,
    alert_config_form_state,
    delete_alert_channel_payload,
    default_notification_channel_config,
    disable_all_alert_channels_payload,
    disable_external_desktop_notifications,
    effective_alerts_config,
    normalize_alert_event_groups,
    normalized_notification_channels,
    upsert_notification_channel_payload,
    validate_notification_channel_config,
)


class AlertConfigTests(unittest.TestCase):
    def test_native_ui_disables_external_desktop_notifications(self) -> None:
        config = {
            "alerts": {
                "desktop": {
                    "enabled": True,
                    "timeout": 8,
                    "event_groups": ["protection"],
                }
            }
        }

        patched = disable_external_desktop_notifications(config)

        self.assertFalse(patched["alerts"]["desktop"]["enabled"])
        self.assertTrue(patched["alerts"]["desktop"]["disabled_in_native_ui"])
        self.assertEqual(
            patched["alerts"]["desktop"]["event_groups"],
            ["protection"],
        )

    def test_channel_scope_filter_uses_consistent_semantics_per_channel(
        self,
    ) -> None:
        config = {
            "event_groups": ["protection"],
            "alert_types": ["WARNING"],
        }

        self.assertTrue(
            alert_channel_accepts_event(
                "desktop",
                config,
                alert_type="WARNING",
                event_group="protection",
            )
        )
        self.assertFalse(
            alert_channel_accepts_event(
                "desktop",
                config,
                alert_type="WARNING",
                event_group="route_health",
            )
        )
        self.assertFalse(
            alert_channel_accepts_event(
                "desktop",
                config,
                alert_type="CRITICAL",
                event_group="protection",
            )
        )

    def test_twilio_explicit_empty_severity_filter_means_all(self) -> None:
        self.assertFalse(
            alert_channel_accepts_event(
                "sms_twilio",
                {},
                alert_type="WARNING",
                event_group="route_health",
            )
        )
        self.assertTrue(
            alert_channel_accepts_event(
                "sms_twilio",
                {"alert_types": []},
                alert_type="WARNING",
                event_group="route_health",
            )
        )

    def test_effective_alerts_config_merges_local_channel_fields(self) -> None:
        merged = effective_alerts_config(
            {
                "alerts": {
                    "telegram": {"enabled": False, "bot_token": "runtime", "chat_id": "chat"},
                    "desktop": {"enabled": True},
                }
            },
            {"alerts": {"telegram": {"enabled": True, "bot_token": "local"}, "whatsapp": "bad"}},
        )

        self.assertEqual(merged["telegram"], {"enabled": True, "bot_token": "local", "chat_id": "chat"})
        self.assertEqual(merged["desktop"], {"enabled": True})
        self.assertEqual(merged["whatsapp"], "bad")

    def test_alert_config_form_state_normalizes_active_channels_and_fields(self) -> None:
        state = alert_config_form_state(
            {
                "alerts": {
                    "telegram": {"enabled": "false", "bot_token": "runtime", "chat_id": 123},
                    "desktop": {"enabled": True},
                }
            },
            {
                "alerts": {
                    "telegram": {
                        "enabled": True,
                        "bot_token": "local",
                        "parse_mode": "plain",
                        "message_thread_id": 42,
                        "silent": True,
                        "protect_content": True,
                        "disable_link_preview": False,
                    },
                    "whatsapp": {
                        "enabled": "true",
                        "access_token": "token",
                        "phone_number_id": "phone",
                        "to": "+1555000",
                        "api_url": None,
                        "mode": "template",
                        "template": {
                            "name": "tesseract_alert",
                            "language": "en_US",
                            "body_params": ["{severity}", "{title}", "{message}"],
                        },
                    },
                }
            },
        )

        self.assertEqual(state.active_channels, {"telegram", "whatsapp", "desktop"})
        self.assertEqual(state.telegram_token, "local")
        self.assertEqual(state.telegram_chat, "123")
        self.assertEqual(state.telegram_parse_mode, "plain")
        self.assertEqual(state.telegram_thread_id, "42")
        self.assertTrue(state.telegram_silent)
        self.assertTrue(state.telegram_protect_content)
        self.assertFalse(state.telegram_disable_link_preview)
        self.assertEqual(state.whatsapp_token, "token")
        self.assertEqual(state.whatsapp_phone_id, "phone")
        self.assertEqual(state.whatsapp_to, "+1555000")
        self.assertEqual(state.whatsapp_api_url, "None")
        self.assertEqual(state.whatsapp_mode, "template")
        self.assertEqual(state.whatsapp_template_name, "tesseract_alert")
        self.assertEqual(state.whatsapp_template_language, "en_US")
        self.assertEqual(state.whatsapp_template_params, ["{severity}", "{title}", "{message}"])

    def test_alert_config_form_state_tolerates_bad_alert_sections(self) -> None:
        state = alert_config_form_state({"alerts": "bad"}, {"alerts": {"telegram": "bad"}})

        self.assertEqual(state.active_channels, set())
        self.assertEqual(state.telegram_token, "")
        self.assertEqual(state.telegram_chat, "")
        self.assertEqual(state.telegram_parse_mode, "HTML")
        self.assertEqual(state.whatsapp_mode, "text")

    def test_disable_all_alert_channels_payload_preserves_channel_fields(self) -> None:
        payload = {
            "alerts": {
                "telegram": {"enabled": True, "bot_token": "token", "event_groups": ["backup"]},
                "whatsapp": "bad",
            }
        }

        result = disable_all_alert_channels_payload(payload)

        self.assertIs(result, payload)
        self.assertEqual(
            payload["alerts"]["telegram"],
            {"enabled": False, "bot_token": "token", "event_groups": ["backup"]},
        )
        self.assertEqual(payload["alerts"]["desktop"], {"enabled": False})
        self.assertEqual(payload["alerts"]["whatsapp"], "bad")

    def test_disable_all_alert_channels_payload_disables_channels_list(self) -> None:
        payload = {
            "alerts": {
                "channels": [
                    {
                        "id": "ops-slack",
                        "type": "slack",
                        "enabled": True,
                        "config": {
                            "enabled": True,
                            "url": "https://hooks.slack.test/secret",
                            "event_groups": ["route_health"],
                        },
                    },
                    {"id": "bad"},
                ],
                "sound": {"enabled": True, "duration_ms": 450},
            }
        }

        disable_all_alert_channels_payload(payload)

        self.assertFalse(payload["alerts"]["channels"][0]["enabled"])
        self.assertFalse(payload["alerts"]["channels"][0]["config"]["enabled"])
        self.assertEqual(payload["alerts"]["channels"][0]["config"]["event_groups"], ["route_health"])
        self.assertFalse(payload["alerts"]["sound"]["enabled"])
        self.assertNotIn("ntfy", payload["alerts"])

    def test_disable_all_alert_channels_payload_replaces_invalid_alerts_section(self) -> None:
        payload = {"alerts": "bad"}

        disable_all_alert_channels_payload(payload)

        self.assertEqual(
            payload,
            {
                "alerts": {
                    "desktop": {"enabled": False},
                    "sound": {"enabled": False},
                    "telegram": {"enabled": False},
                    "whatsapp": {"enabled": False},
                }
            },
        )

    def test_delete_alert_channel_payload_removes_only_selected_channel(self) -> None:
        payload = {"alerts": {"telegram": {"enabled": True}, "desktop": {"enabled": True}}}

        result = delete_alert_channel_payload(payload, "telegram")

        self.assertIs(result, payload)
        self.assertEqual(payload, {"alerts": {"desktop": {"enabled": True}}})

    def test_delete_alert_channel_payload_removes_matching_new_channel(self) -> None:
        payload = {
            "alerts": {
                "channels": [
                    {"id": "ops-slack", "type": "slack", "enabled": True},
                    {"id": "ops-email", "type": "email_smtp", "enabled": True},
                ]
            }
        }

        delete_alert_channel_payload(payload, "ops-slack")

        self.assertEqual(payload["alerts"]["channels"], [{"id": "ops-email", "type": "email_smtp", "enabled": True}])

    def test_delete_alert_channel_payload_does_not_delete_channels_by_type(self) -> None:
        payload = {
            "alerts": {
                "channels": [
                    {"id": "ops-slack", "type": "slack", "enabled": True},
                    {"id": "audit-slack", "type": "slack", "enabled": True},
                    {"id": "ops-email", "type": "email_smtp", "enabled": True},
                ]
            }
        }

        delete_alert_channel_payload(payload, "slack")

        self.assertEqual(
            payload["alerts"]["channels"],
            [
                {"id": "ops-slack", "type": "slack", "enabled": True},
                {"id": "audit-slack", "type": "slack", "enabled": True},
                {"id": "ops-email", "type": "email_smtp", "enabled": True},
            ],
        )

    def test_delete_alert_channel_payload_removes_generated_channel_key(self) -> None:
        payload = {
            "alerts": {
                "channels": [
                    {"type": "slack", "enabled": True},
                    {"type": "email_smtp", "enabled": True},
                ]
            }
        }

        delete_alert_channel_payload(payload, "slack-1")

        self.assertEqual(payload["alerts"]["channels"], [{"type": "email_smtp", "enabled": True}])

    def test_normalized_notification_channels_merges_legacy_and_grouped_entries(self) -> None:
        channels = normalized_notification_channels(
            {
                "telegram": {"enabled": True, "bot_token": "token", "chat_id": "chat"},
                "channels": [
                    {
                        "id": "ops-slack",
                        "type": "slack",
                        "label": "Ops Slack",
                        "enabled": True,
                        "config": {"url": "https://hooks.slack.test/secret"},
                    },
                    {"id": "ignored", "type": "not-real", "enabled": True},
                ],
            }
        )

        by_key = {item["key"]: item for item in channels}
        self.assertIn("telegram", by_key)
        self.assertEqual(by_key["telegram"]["group"], "chat")
        self.assertTrue(by_key["telegram"]["config"]["enabled"])
        self.assertEqual(by_key["ops-slack"]["type"], "slack")
        self.assertEqual(by_key["ops-slack"]["group"], "chat")
        self.assertEqual(by_key["ops-slack"]["label"], "Ops Slack")
        self.assertEqual(by_key["ops-slack"]["config"]["label"], "Ops Slack")
        self.assertEqual(by_key["ops-slack"]["config"]["url"], "https://hooks.slack.test/secret")
        self.assertNotIn("ignored", by_key)

    def test_normalized_notification_channels_skips_unsupported_local_ecosystem_presets(self) -> None:
        channels = normalized_notification_channels(
            {
                "channels": [
                    {"id": "local-im", "type": "local_enterprise_im", "enabled": True, "config": {"url": "https://example.test"}},
                    {"id": "vendor-chat", "type": "vendor_chat_bot", "enabled": True, "config": {"url": "https://example.test"}},
                    {"id": "legacy-hook", "type": "legacy_team_hook", "enabled": True, "config": {"url": "https://example.test"}},
                    {"id": "ops-slack", "type": "slack", "enabled": True, "config": {"url": "https://hooks.slack.test"}},
                ],
            }
        )

        self.assertEqual([(item["key"], item["type"]) for item in channels], [("ops-slack", "slack")])

    def test_normalized_notification_channels_preserves_legacy_key_on_conflict(self) -> None:
        channels = normalized_notification_channels(
            {
                "telegram": {"enabled": True, "bot_token": "token", "chat_id": "chat"},
                "channels": [
                    {
                        "id": "telegram",
                        "type": "generic_webhook",
                        "enabled": True,
                        "config": {"url": "https://example.test/hook"},
                    }
                ],
            }
        )

        by_key = {item["key"]: item for item in channels}
        self.assertEqual(by_key["telegram"]["type"], "telegram")
        self.assertEqual(by_key["telegram"]["config"]["bot_token"], "token")

    def test_normalized_notification_channels_outer_controls_override_config(self) -> None:
        channels = normalized_notification_channels(
            {
                "channels": [
                    {
                        "id": "outer-disabled",
                        "type": "generic_webhook",
                        "enabled": False,
                        "alert_types": ["CRITICAL"],
                        "timeout": 3,
                        "retries": 0,
                        "config": {
                            "enabled": True,
                            "alert_types": ["WARNING"],
                            "timeout": 30,
                            "retries": 3,
                            "url": "https://example.test/hook",
                        },
                    }
                ],
            }
        )

        config = channels[0]["config"]
        self.assertFalse(config["enabled"])
        self.assertEqual(config["alert_types"], ["CRITICAL"])
        self.assertEqual(config["timeout"], 3)
        self.assertEqual(config["retries"], 0)

    def test_normalized_notification_channels_preserves_valid_event_groups(self) -> None:
        channels = normalized_notification_channels(
            {
                "telegram": {
                    "enabled": True,
                    "bot_token": "token",
                    "chat_id": "chat",
                    "event_groups": ["vanguard_nodes", "unknown", "backup", "vanguard_nodes"],
                },
                "channels": [
                    {
                        "id": "ops-webhook",
                        "type": "generic_webhook",
                        "enabled": True,
                        "event_groups": ["route_health"],
                        "config": {
                            "url": "https://example.test/hook",
                            "event_groups": ["protection"],
                        },
                    },
                    {
                        "id": "bad-scope",
                        "type": "ntfy",
                        "config": {"event_groups": ["unknown"]},
                    },
                ],
            }
        )

        by_key = {item["key"]: item for item in channels}
        self.assertEqual(by_key["telegram"]["config"]["event_groups"], ["vanguard_nodes", "backup"])
        self.assertEqual(by_key["ops-webhook"]["config"]["event_groups"], ["route_health"])
        self.assertEqual(by_key["bad-scope"]["config"]["event_groups"], [])

    def test_normalize_alert_event_groups_returns_empty_for_empty_or_unknown_scope(self) -> None:
        self.assertEqual(normalize_alert_event_groups(None), [])
        self.assertEqual(normalize_alert_event_groups(["unknown"]), [])
        self.assertEqual(
            normalize_alert_event_groups("vanguard_nodes, backup, vanguard_nodes"),
            ["vanguard_nodes", "backup"],
        )
        self.assertIn("protection", ALERT_EVENT_GROUP_KEYS)

    def test_channel_registry_exposes_cost_and_sms_defaults(self) -> None:
        self.assertEqual(ALERT_NOTIFICATION_CHANNEL_TYPES, SUPPORTED_ALERT_CHANNEL_TYPES)
        self.assertEqual(len(SUPPORTED_ALERT_CHANNEL_TYPES), 15)
        self.assertEqual(alert_channel_group("sms_twilio"), "sms")
        self.assertTrue(alert_channel_has_cost("sms_twilio"))
        self.assertFalse(alert_channel_has_cost("ntfy"))
        self.assertEqual(
            default_notification_channel_config("sms_twilio")["alert_types"],
            ["CRITICAL", "EMERGENCY", "KILL_SWITCH_ON"],
        )
        self.assertEqual(
            set(RETIRED_ALERT_CHANNEL_TYPES),
            {
                "whatsapp_web_bridge",
                "signal_bridge",
                "telegram_user_bridge",
                "email_outlook_profile",
                "apprise",
            },
        )

    def test_retired_channels_are_preserved_for_diagnostics_but_forced_off(self) -> None:
        channels = normalized_notification_channels(
            {
                "apprise": {"enabled": True, "urls": ["json://secret"]},
                "channels": [
                    {
                        "id": "old-signal",
                        "type": "signal_bridge",
                        "enabled": True,
                        "config": {"enabled": True, "bridge_id": "signal", "to": "+15550001"},
                    }
                ],
            }
        )

        by_key = {item["key"]: item for item in channels}
        self.assertEqual(set(by_key), {"apprise", "old-signal"})
        for item in by_key.values():
            self.assertTrue(item["retired"])
            self.assertFalse(item["supported"])
            self.assertTrue(item["requested_enabled"])
            self.assertEqual(item["diagnostic"], "retired_channel_type")
            self.assertFalse(item["config"]["enabled"])

    def test_notification_channel_validation_requires_enabled_credentials(self) -> None:
        validate_notification_channel_config(
            "telegram",
            {"enabled": False, "bot_token": "", "chat_id": ""},
        )
        with self.assertRaisesRegex(ValueError, "bot_token and chat_id"):
            validate_notification_channel_config(
                "telegram",
                {"enabled": True, "bot_token": "", "chat_id": ""},
            )
        with self.assertRaisesRegex(ValueError, "http or https"):
            validate_notification_channel_config(
                "generic_webhook",
                {"enabled": True, "url": "file:///tmp/hook"},
            )

    def test_notification_channel_validation_rejects_non_string_secrets_and_controls(self) -> None:
        with self.assertRaisesRegex(ValueError, "bot_token must be a string"):
            validate_notification_channel_config(
                "telegram",
                {
                    "enabled": True,
                    "bot_token": ["not", "a", "token"],
                    "chat_id": "123",
                },
            )
        with self.assertRaisesRegex(ValueError, "control characters"):
            validate_notification_channel_config(
                "generic_webhook",
                {
                    "enabled": True,
                    "url": "https://example.test/hook\r\nInjected: true",
                },
            )
        with self.assertRaisesRegex(ValueError, "headers must not contain control"):
            validate_notification_channel_config(
                "generic_webhook",
                {
                    "enabled": False,
                    "url": "",
                    "headers": {"Authorization": "Bearer safe\r\nInjected: true"},
                },
            )

    def test_notification_channel_validation_allows_string_recipient_lists(self) -> None:
        validate_notification_channel_config(
            "email_sendgrid",
            {
                "enabled": True,
                "api_key": "secret",
                "from_addr": "from@example.test",
                "to_addrs": ["one@example.test", "two@example.test"],
            },
        )
        with self.assertRaisesRegex(ValueError, r"to_addrs\[0\] must be a string"):
            validate_notification_channel_config(
                "email_sendgrid",
                {
                    "enabled": False,
                    "to_addrs": [{"email": "not-a-string@example.test"}],
                },
            )

    def test_notification_channel_validation_rejects_lossy_numeric_coercion(self) -> None:
        for invalid in (True, 1.5, "1.5"):
            with self.subTest(field="retries", value=invalid):
                with self.assertRaisesRegex(ValueError, "retries must be an integer"):
                    validate_notification_channel_config(
                        "generic_webhook",
                        {"enabled": False, "retries": invalid},
                    )
            with self.subTest(field="duration_ms", value=invalid):
                with self.assertRaisesRegex(ValueError, "duration_ms must be an integer"):
                    validate_notification_channel_config(
                        "sound",
                        {"enabled": False, "duration_ms": invalid},
                    )
            with self.subTest(field="message_thread_id", value=invalid):
                with self.assertRaisesRegex(ValueError, "message_thread_id must be an integer"):
                    validate_notification_channel_config(
                        "telegram",
                        {"enabled": False, "message_thread_id": invalid},
                    )
            with self.subTest(field="port", value=invalid):
                with self.assertRaisesRegex(ValueError, "port must be an integer"):
                    validate_notification_channel_config(
                        "email_smtp",
                        {"enabled": False, "port": invalid},
                    )

        for invalid in (True, float("nan"), float("inf"), float("-inf")):
            with self.subTest(field="timeout", value=invalid):
                with self.assertRaisesRegex(ValueError, r"timeout must be (?:a|a finite) number"):
                    validate_notification_channel_config(
                        "generic_webhook",
                        {"enabled": False, "timeout": invalid},
                    )

        validate_notification_channel_config(
            "email_smtp",
            {"enabled": False, "port": "587", "retries": "2", "timeout": "2.5"},
        )

    def test_push_priority_validation_matches_provider_contracts(self) -> None:
        for priority in ("", 1, "5"):
            with self.subTest(channel="ntfy", priority=priority):
                validate_notification_channel_config(
                    "ntfy",
                    {"enabled": False, "priority": priority},
                )
        for priority in (0, 6, 1.5, True):
            with self.subTest(channel="ntfy", priority=priority):
                with self.assertRaisesRegex(ValueError, "priority"):
                    validate_notification_channel_config(
                        "ntfy",
                        {"enabled": False, "priority": priority},
                    )

        for priority in ("", -2, "1"):
            with self.subTest(channel="pushover", priority=priority):
                validate_notification_channel_config(
                    "pushover",
                    {"enabled": False, "priority": priority},
                )
        for priority in (-3, 2, 1.5, True):
            with self.subTest(channel="pushover", priority=priority):
                with self.assertRaisesRegex(ValueError, "priority"):
                    validate_notification_channel_config(
                        "pushover",
                        {"enabled": False, "priority": priority},
                    )

        validate_notification_channel_config(
            "gotify",
            {"enabled": False, "priority": "-5"},
        )
        for priority in (1.5, "1.5", True):
            with self.subTest(channel="gotify", priority=priority):
                with self.assertRaisesRegex(ValueError, "priority must be an integer"):
                    validate_notification_channel_config(
                        "gotify",
                        {"enabled": False, "priority": priority},
                    )

    def test_sound_and_ntfy_bounds_match_provider_contracts(self) -> None:
        for frequency in (37, 32767):
            validate_notification_channel_config(
                "sound",
                {"enabled": False, "frequency": frequency},
            )
        for frequency in (36, 32768):
            with self.assertRaisesRegex(ValueError, "frequency"):
                validate_notification_channel_config(
                    "sound",
                    {"enabled": False, "frequency": frequency},
                )

        for duration in (20, 10000):
            validate_notification_channel_config(
                "sound",
                {"enabled": False, "duration_ms": duration},
            )
        for duration in (19, 10001):
            with self.assertRaisesRegex(ValueError, "duration_ms"):
                validate_notification_channel_config(
                    "sound",
                    {"enabled": False, "duration_ms": duration},
                )

        for topic in ("a", "Ops_1-release", "x" * 64):
            validate_notification_channel_config(
                "ntfy",
                {"enabled": False, "topic": topic},
            )
        for topic in ("",):
            validate_notification_channel_config(
                "ntfy",
                {"enabled": False, "topic": topic},
            )
        for topic in ("ops.prod", "ops/team", "代理", "x" * 65):
            with self.assertRaisesRegex(ValueError, "topic"):
                validate_notification_channel_config(
                    "ntfy",
                    {"enabled": False, "topic": topic},
                )

    def test_upsert_rejects_retired_channel_types(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported notification channel type"):
            upsert_notification_channel_payload(
                {"alerts": {}},
                {"type": "apprise", "config": {"enabled": False, "urls": []}},
            )

    def test_upsert_always_validates_enabled_channel_config(self) -> None:
        with self.assertRaisesRegex(ValueError, "bot_token must be a string"):
            upsert_notification_channel_payload(
                {"alerts": {}},
                {
                    "type": "telegram",
                    "enabled": True,
                    "config": {"bot_token": ["bad"], "chat_id": "123"},
                },
            )

    def test_upsert_notification_channel_payload_writes_grouped_channel(self) -> None:
        payload = {"alerts": {}}

        result = upsert_notification_channel_payload(
            payload,
            {
                "key": "ops-webhook",
                "type": "generic_webhook",
                "label": "Ops Webhook",
                "enabled": True,
                "config": {
                    "url": "https://example.test/hook",
                    "body_format": "json",
                    "event_groups": ["route_health"],
                },
            },
        )

        self.assertIs(result, payload)
        self.assertEqual(
            payload["alerts"]["channels"],
            [
                {
                    "id": "ops-webhook",
                    "type": "generic_webhook",
                    "group": "generic",
                    "label": "Ops Webhook",
                    "enabled": True,
                    "config": {
                        "url": "https://example.test/hook",
                        "body_format": "json",
                        "event_groups": ["route_health"],
                        "enabled": True,
                        "label": "Ops Webhook",
                    },
                }
            ],
        )

    def test_upsert_preserves_explicit_empty_scope_and_rejects_unknown_scope(self) -> None:
        payload = {"alerts": {}}
        upsert_notification_channel_payload(
            payload,
            {
                "id": "silent-webhook",
                "type": "generic_webhook",
                "enabled": False,
                "event_groups": [],
                "config": {"url": "https://example.test/hook"},
            },
        )
        self.assertEqual(
            payload["alerts"]["channels"][0]["config"]["event_groups"],
            [],
        )

        with self.assertRaisesRegex(ValueError, "unsupported values: unknown"):
            upsert_notification_channel_payload(
                {"alerts": {}},
                {
                    "id": "bad-scope",
                    "type": "generic_webhook",
                    "enabled": False,
                    "event_groups": ["unknown"],
                    "config": {"url": "https://example.test/hook"},
                },
            )

    def test_upsert_notification_channel_payload_keeps_legacy_top_level(self) -> None:
        payload = {"alerts": {}}

        upsert_notification_channel_payload(
            payload,
            {"type": "telegram", "enabled": True, "config": {"bot_token": "token", "chat_id": "chat"}},
        )
        upsert_notification_channel_payload(
            payload,
            {"type": "sound", "enabled": True, "config": {"frequency": 880, "duration_ms": 300}},
        )

        self.assertEqual(
            payload["alerts"],
            {
                "telegram": {"bot_token": "token", "chat_id": "chat", "enabled": True, "label": "Telegram"},
                "sound": {"frequency": 880, "duration_ms": 300, "enabled": True, "label": "声音提示"},
            },
        )

    def test_upsert_notification_channel_payload_coerces_string_false_enabled(self) -> None:
        payload = {"alerts": {}}

        upsert_notification_channel_payload(
            payload,
            {
                "id": "ops-webhook",
                "type": "generic_webhook",
                "enabled": "false",
                "config": {"enabled": True, "url": "https://example.test/hook"},
            },
        )

        stored = payload["alerts"]["channels"][0]
        self.assertFalse(stored["enabled"])
        self.assertFalse(stored["config"]["enabled"])

    def test_upsert_notification_channel_payload_rejects_reserved_new_channel_key(self) -> None:
        payload = {"alerts": {}}

        with self.assertRaises(ValueError):
            upsert_notification_channel_payload(
                payload,
                {"id": "telegram", "type": "generic_webhook", "config": {"url": "https://example.test/hook"}},
            )

    def test_upsert_notification_channel_payload_rejects_duplicate_key_with_different_type(self) -> None:
        payload = {"alerts": {"channels": [{"id": "ops", "type": "slack", "config": {"url": "https://slack.test"}}]}}

        with self.assertRaises(ValueError):
            upsert_notification_channel_payload(
                payload,
                {"id": "ops", "type": "generic_webhook", "config": {"url": "https://example.test/hook"}},
            )

    def test_delete_alert_channel_payload_replaces_invalid_alerts_section(self) -> None:
        payload = {"alerts": "bad"}

        delete_alert_channel_payload(payload, "telegram")

        self.assertEqual(payload, {"alerts": {}})


if __name__ == "__main__":
    unittest.main()
