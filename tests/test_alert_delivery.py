import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from alert_delivery import (
    AlertDeliveryStore,
    alert_channel_config_signature,
    alert_channel_configured,
    redact_delivery_error,
)


class AlertDeliveryStoreTests(unittest.TestCase):
    def test_store_is_bounded_persistent_and_contains_no_payload_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "alert-delivery-history.json"
            store = AlertDeliveryStore(path, limit=3)
            config = {"enabled": True, "bot_token": "secret-token", "chat_id": "42"}
            for index in range(5):
                store.record(
                    channel_id="telegram",
                    channel_type="telegram",
                    config=config,
                    outcome="failed" if index == 4 else "success",
                    error=f"provider rejected secret-token at https://example.test/?token=secret-token {index}",
                )

            records = AlertDeliveryStore(path, limit=3).recent()
            self.assertEqual(len(records), 3)
            self.assertEqual(records[-1]["outcome"], "failed")
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("secret-token", raw)
            self.assertNotIn("message", raw)
            self.assertEqual(json.loads(raw)["schema_version"], 2)
            self.assertEqual(
                set(json.loads(raw)["records"][0]),
                {
                    "timestamp",
                    "channel_id",
                    "channel_type",
                    "outcome",
                    "error",
                    "config_signature",
                },
            )

    def test_corrupt_file_is_quarantined_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "history.json"
            path.write_text("not-json", encoding="utf-8")
            with self.assertLogs("alert_delivery", level="WARNING") as logs:
                store = AlertDeliveryStore(path)
            self.assertEqual(store.recent(), [])
            self.assertFalse(path.exists())
            quarantined = list(
                Path(temp_dir).glob("history.json.corrupt-*")
            )
            self.assertEqual(len(quarantined), 1)
            self.assertIn("corrupt", " ".join(logs.output).lower())
            self.assertEqual(
                store.load_warning,
                {"kind": "corrupt_history", "isolated": True},
            )
            self.assertNotIn(
                str(path),
                json.dumps(store.load_warning, sort_keys=True),
            )
            store.record(
                channel_id="desktop",
                channel_type="desktop",
                config={"enabled": True},
                outcome="success",
            )
            self.assertEqual(len(store.recent()), 1)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))[
                    "schema_version"
                ],
                2,
            )

    def test_v1_history_migrates_to_v2_and_redacts_personal_data(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "history.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "records": [
                            {
                                "record_id": "legacy",
                                "timestamp": "2026-07-14T00:00:00+00:00",
                                "channel_id": "owner@example.com",
                                "channel_type": "generic_webhook",
                                "source": "runtime",
                                "alert_type": "WARNING",
                                "event_group": "protection",
                                "outcome": "failed",
                                "duration_ms": 12,
                                "error": (
                                    '{"token":"json-secret",'
                                    '"recipients":["owner@example.com","+15551234567"],'
                                    '"webhook":"https://hooks.slack.com/services/'
                                    'T/B/webhook-secret?token=query-secret"} '
                                    "https://discord.com/api/webhooks/123/"
                                    "discord-secret?wait=true"
                                ),
                                "config_signature": "legacy-raw-secret",
                                "recipient": "owner@example.com",
                                "phone": "+15551234567",
                                "token": "secret-token",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            store = AlertDeliveryStore(path)
            records = store.recent()
            raw = path.read_text(encoding="utf-8")

            self.assertEqual(len(records), 1)
            self.assertEqual(
                json.loads(raw)["schema_version"],
                2,
            )
            for secret in (
                "owner@example.com",
                "+15551234567",
                "secret-token",
                "json-secret",
                "query-secret",
                "legacy-raw-secret",
                "webhook-secret",
                "discord-secret",
            ):
                self.assertNotIn(secret, raw)
            self.assertNotIn("recipient", json.loads(raw)["records"][0])
            self.assertNotIn("record_id", json.loads(raw)["records"][0])
            self.assertNotIn("source", json.loads(raw)["records"][0])
            self.assertNotIn("alert_type", json.loads(raw)["records"][0])
            self.assertNotIn("event_group", json.loads(raw)["records"][0])
            self.assertNotIn("duration_ms", json.loads(raw)["records"][0])
            self.assertEqual(
                len(records[0]["config_signature"]),
                64,
            )

    def test_v2_schema_is_rewritten_to_exact_safe_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "history.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "updated_at": "2026-07-14T00:00:00+00:00",
                        "token": "top-secret",
                        "records": [
                            {
                                "timestamp": "token=timestamp-secret",
                                "channel_id": "owner@example.com",
                                "channel_type": "owner@example.com",
                                "outcome": "token=outcome-secret",
                                "error": '{"token":"error-secret"}',
                                "config_signature": "legacy-secret",
                                "recipient": "owner@example.com",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            records = AlertDeliveryStore(path).recent()
            payload = json.loads(path.read_text(encoding="utf-8"))
            raw = json.dumps(payload, sort_keys=True)

            self.assertEqual(
                set(payload),
                {"schema_version", "updated_at", "records"},
            )
            self.assertEqual(
                set(payload["records"][0]),
                {
                    "timestamp",
                    "channel_id",
                    "channel_type",
                    "outcome",
                    "error",
                    "config_signature",
                },
            )
            self.assertEqual(records[0]["timestamp"], "")
            self.assertEqual(records[0]["channel_type"], "unknown")
            self.assertEqual(records[0]["outcome"], "failed")
            for secret in (
                "top-secret",
                "timestamp-secret",
                "outcome-secret",
                "error-secret",
                "legacy-secret",
                "owner@example.com",
            ):
                self.assertNotIn(secret, raw)

    def test_delete_channel_removes_only_matching_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "history.json"
            store = AlertDeliveryStore(path)
            for channel_id in ("ops", "audit", "ops"):
                store.record(
                    channel_id=channel_id,
                    channel_type="generic_webhook",
                    config={
                        "enabled": True,
                        "url": f"https://example.test/{channel_id}",
                    },
                    outcome="success",
                )

            self.assertEqual(store.delete_channel("ops"), 2)
            self.assertEqual(store.delete_channel("ops"), 0)
            self.assertEqual(
                [record["channel_id"] for record in store.recent()],
                ["audit"],
            )
            reloaded = AlertDeliveryStore(path)
            self.assertEqual(
                [record["channel_id"] for record in reloaded.recent()],
                ["audit"],
            )

    def test_persistence_finishes_with_atomic_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "history.json"
            store = AlertDeliveryStore(path)
            from alert_delivery import os as delivery_os

            with patch("alert_delivery.os.replace", wraps=delivery_os.replace) as replace:
                store.record(
                    channel_id="desktop",
                    channel_type="desktop",
                    config={"enabled": True},
                    outcome="success",
                )

            replace.assert_called_once()
            self.assertEqual(replace.call_args.args[1], path)

    def test_concurrent_writes_remain_valid_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "history.json"
            store = AlertDeliveryStore(path, limit=40)
            errors = []
            error_lock = threading.Lock()

            def write(index: int) -> None:
                try:
                    store.record(
                        channel_id=f"channel-{index % 3}",
                        channel_type="generic_webhook",
                        config={"enabled": True, "url": f"https://example.test/{index}"},
                        outcome="success",
                    )
                except Exception as exc:
                    with error_lock:
                        errors.append(exc)

            threads = [threading.Thread(target=write, args=(index,)) for index in range(70)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            self.assertEqual(len(store.recent()), 40)
            self.assertEqual(len(json.loads(path.read_text(encoding="utf-8"))["records"]), 40)

    def test_short_replace_lock_is_retried_without_dropping_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "history.json"
            store = AlertDeliveryStore(path)
            original = os.replace
            calls = []

            def locked_then_released(source, target):
                calls.append((source, target))
                if len(calls) <= 2:
                    raise PermissionError("temporary file lock")
                return original(source, target)

            with patch("alert_delivery.os.replace", side_effect=locked_then_released):
                store.record(channel_id="desktop", channel_type="desktop",
                             config={"enabled": True}, outcome="success")
            self.assertEqual(len(calls), 3)
            self.assertEqual(len(AlertDeliveryStore(path).recent()), 1)
            self.assertEqual(list(Path(temp_dir).glob("*.tmp")), [])

    def test_persistent_replace_failure_is_bounded_and_preserves_old_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "history.json"
            store = AlertDeliveryStore(path)
            store.record(channel_id="desktop", channel_type="desktop",
                         config={"enabled": True}, outcome="success")
            before = path.read_bytes()
            with patch("alert_delivery.os.replace", side_effect=PermissionError("denied")) as replace:
                with self.assertRaises(PermissionError):
                    store.record(channel_id="desktop", channel_type="desktop",
                                 config={"enabled": True}, outcome="failed")
            self.assertEqual(replace.call_count, 4)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(Path(temp_dir).glob("*.tmp")), [])

    def test_health_tracks_matching_configuration_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AlertDeliveryStore(Path(temp_dir) / "history.json")
            config = {"enabled": True, "server": "https://ntfy.sh", "topic": "ops", "token": "alpha"}
            entry = {"key": "ntfy", "type": "ntfy", "config": config}
            self.assertEqual(store.health_for_entry(entry).status, "unverified")
            store.record(
                channel_id="ntfy",
                channel_type="ntfy",
                config=config,
                outcome="success",
            )
            self.assertEqual(store.health_for_entry(entry).status, "submitted")
            changed_scope = {
                **config,
                "event_groups": ["backup"],
            }
            self.assertEqual(
                store.health_for_entry(
                    {
                        "key": "ntfy",
                        "type": "ntfy",
                        "config": changed_scope,
                    }
                ).status,
                "unverified",
            )
            changed = {**config, "token": "beta"}
            changed_entry = {"key": "ntfy", "type": "ntfy", "config": changed}
            self.assertEqual(store.health_for_entry(changed_entry).status, "unverified")
            store.record(
                channel_id="ntfy",
                channel_type="ntfy",
                config=changed,
                outcome="failed",
                error="unauthorized beta",
            )
            health = store.health_for_entry(changed_entry)
            self.assertEqual(health.status, "failed")
            self.assertNotIn("beta", health.last_error)

    def test_health_uses_the_same_redacted_channel_id_as_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AlertDeliveryStore(Path(temp_dir) / "history.json")
            channel_id = "owner@example.com"
            config = {
                "enabled": True,
                "url": "https://example.test/alerts",
            }
            store.record(
                channel_id=channel_id,
                channel_type="generic_webhook",
                config=config,
                outcome="success",
            )

            health = store.health_for_entry(
                {
                    "key": channel_id,
                    "type": "generic_webhook",
                    "config": config,
                }
            )

            self.assertEqual(health.channel_id, channel_id)
            self.assertEqual(health.status, "submitted")
            self.assertEqual(health.last_outcome, "success")

    def test_disabled_and_incomplete_statuses_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AlertDeliveryStore(Path(temp_dir) / "history.json")
            incomplete = {"key": "telegram", "type": "telegram", "config": {"enabled": False}}
            disabled = {
                "key": "telegram",
                "type": "telegram",
                "config": {"enabled": False, "bot_token": "token", "chat_id": "42"},
            }
            self.assertEqual(store.health_for_entry(incomplete).status, "incomplete")
            self.assertEqual(store.health_for_entry(disabled).status, "disabled")

    def test_signature_ignores_enable_but_tracks_scope_and_transport(self) -> None:
        first = {"enabled": False, "event_groups": ["backup"], "url": "https://a.test"}
        enabled = {**first, "enabled": True}
        changed_scope = {**enabled, "event_groups": ["protection"]}
        changed_transport = {**changed_scope, "url": "https://b.test"}
        self.assertEqual(
            alert_channel_config_signature("generic_webhook", first),
            alert_channel_config_signature("generic_webhook", enabled),
        )
        self.assertNotEqual(
            alert_channel_config_signature("generic_webhook", enabled),
            alert_channel_config_signature("generic_webhook", changed_scope),
        )
        self.assertNotEqual(
            alert_channel_config_signature("generic_webhook", changed_scope),
            alert_channel_config_signature("generic_webhook", changed_transport),
        )
        self.assertEqual(
            alert_channel_config_signature(
                "generic_webhook",
                {"scope": {"b", "a"}},
            ),
            alert_channel_config_signature(
                "generic_webhook",
                {"scope": {"a", "b"}},
            ),
        )

    def test_configured_rules_and_error_redaction(self) -> None:
        self.assertTrue(alert_channel_configured("telegram", {"bot_token": "x", "chat_id": "1"}))
        self.assertFalse(alert_channel_configured("telegram", {"bot_token": "x"}))
        self.assertTrue(
            alert_channel_configured(
                "sms_twilio",
                {
                    "account_sid": "AC123",
                    "auth_token": "secret",
                    "from": "+15550000001",
                    "to": "+15550000002",
                },
            )
        )
        self.assertTrue(
            alert_channel_configured(
                "sms_twilio",
                {
                    "account_sid": "AC123",
                    "auth_token": "secret",
                    "from_number": "",
                    "to_numbers": [],
                    "from": "+15550000001",
                    "to": ["+15550000002"],
                },
            )
        )
        self.assertFalse(
            alert_channel_configured(
                "sms_twilio",
                {
                    "account_sid": "AC123",
                    "auth_token": "secret",
                    "from": "+15550000001",
                },
            )
        )
        self.assertEqual(
            redact_delivery_error("failed x", {"bot_token": "x"}),
            "failed <redacted>",
        )
        self.assertFalse(alert_channel_configured("unknown", {"url": "https://example.test"}))


if __name__ == "__main__":
    unittest.main()
