import json
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

import pytest

from alert_delivery import AlertDeliveryStore
from alerts import AlertEvent, AlertManager
from bridge_alerts_history import AlertsHistoryAdapter
from bridge_protocol import validate_frame
from models import AlertType


def manager(limit=200, **kwargs):
    logger = logging.Logger("rt6-test", level=logging.CRITICAL + 1)
    return AlertManager({"history_limit": limit}, logger, **kwargs)


def append(source, *, key="test.warning", title="Warning", message="Check route",
           alert_type=AlertType.WARNING):
    return source.send_event(AlertEvent(key, alert_type, title, message))


def validate(events):
    for rev, event in enumerate(events, 1):
        frame = {"t": "evt", "epoch": "rt6-test", "rev": rev,
                 "produced_at_ms": 1, "sent_at_ms": 2, **event}
        assert validate_frame(frame) == []


def test_eviction_gap_uses_oldest_sequence_and_boundary():
    source = manager(2)
    for _ in range(4):
        append(source)
    result = source.since(0)
    assert result["gap"] is True
    assert (result["min_seq"], result["last_seq"]) == (3, 4)
    assert [x["alert_seq"] for x in result["items"]] == [3, 4]
    assert source.since(2)["gap"] is False
    assert source.since(4)["items"] == []


def test_concurrent_appends_have_unique_strictly_increasing_sequences():
    source = manager(100)
    start = Event()

    def write(_):
        assert start.wait(2)
        for _ in range(20):
            assert append(source)
            items, cursor = source.recent_with_cursor()
            assert [item["alert_seq"] for item in items] == list(range(1, cursor + 1))

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(write, i) for i in range(4)]
        start.set()
        for future in futures:
            future.result(timeout=5)
    items, cursor = source.recent_with_cursor()
    assert cursor == 80
    assert [item["alert_seq"] for item in items] == list(range(1, 81))


def test_initialization_append_between_snapshot_and_cursor_install_is_not_lost():
    source = manager()
    append(source)
    adapter = AlertsHistoryAdapter(source)
    captured, release = Event(), Event()
    original = source.recent_with_cursor

    def delayed_snapshot():
        result = original()
        captured.set()
        assert release.wait(2)
        return result

    with patch.object(source, "recent_with_cursor", side_effect=delayed_snapshot):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(adapter.snapshot)
            assert captured.wait(2)
            append(source)
            release.set()
            snapshot = future.result(timeout=2)
    edges = adapter.poll()
    assert snapshot["data"]["last_seq"] == 1
    assert [item["seq"] for event in edges for item in event["data"]["items"]] == [2]
    assert adapter.poll() == []
    validate([snapshot] + edges)


def test_equal_alert_contents_are_two_events_and_qt_format_is_unchanged():
    source = manager()
    with patch("alerts.utc_now_iso", return_value="2026-10-01T00:00:00+00:00"):
        append(source)
        append(source)
    qt = source.recent()
    assert len(qt) == 2 and qt[0] == qt[1]
    assert set(qt[0]) == {"timestamp", "type", "title", "message", "event_key", "event_group"}
    snapshot = AlertsHistoryAdapter(source).snapshot()
    rows = snapshot["data"]["items"]
    assert [row["seq"] for row in rows] == [1, 2]
    assert rows[0]["id"] != rows[1]["id"]
    assert rows[0]["ts_ms"] == rows[1]["ts_ms"] == 1790812800000
    validate([snapshot])


def test_gap_reseed_uses_latest_watermark_and_counts_extra_eviction():
    source = manager(2)
    append(source)
    adapter = AlertsHistoryAdapter(source)
    initial = adapter.snapshot()
    for _ in range(3):
        append(source)
    original = source.recent_with_cursor

    def more_eviction():
        append(source)
        return original()

    with patch.object(source, "recent_with_cursor", side_effect=more_eviction):
        gap, seed = adapter.poll()
    assert gap["data"] == {"from_seq": 1, "to_seq": 2, "dropped": 2}
    assert gap["seq"] == 3
    assert seed["data"]["last_seq"] == 5
    assert [row["seq"] for row in seed["data"]["items"]] == [4, 5]
    assert adapter.poll() == []
    append(source)
    edge, = adapter.poll()
    assert edge["seq"] == 4  # Transport domain includes the gap marker.
    assert edge["data"]["last_seq"] == 6  # Source domain does not.
    validate([initial, gap, seed, edge])


def test_append_after_reseed_is_delivered_once():
    source = manager(1)
    adapter = AlertsHistoryAdapter(source)
    adapter.snapshot()
    append(source)
    append(source)
    original = source.recent_with_cursor

    def after_copy():
        result = original()
        append(source)
        return result

    with patch.object(source, "recent_with_cursor", side_effect=after_copy):
        gap, snapshot = adapter.poll()
    assert snapshot["data"]["last_seq"] == 2
    edge, = adapter.poll()
    assert edge["data"]["last_seq"] == 3
    assert adapter.poll() == []
    validate([gap, snapshot, edge])


def test_zero_capacity_and_empty_log_report_loss_without_fake_records():
    source = manager(0)
    adapter = AlertsHistoryAdapter(source)
    assert adapter.snapshot()["data"] == {"items": [], "last_seq": 0}
    assert source.since(0)["min_seq"] == 1
    append(source)
    assert source.since(0) == {"items": [], "last_seq": 1, "min_seq": 2, "gap": True}
    gap, seed = adapter.poll()
    assert gap["data"]["dropped"] == 1
    assert seed["data"] == {"items": [], "last_seq": 1}
    assert adapter.poll() == []
    validate([gap, seed])


@pytest.mark.parametrize("cursor", [-1, True, 1.0, "1", 1])
def test_invalid_or_future_cursor_is_rejected(cursor):
    with pytest.raises(ValueError):
        manager().since(cursor)


def test_poll_requires_snapshot_and_returned_records_are_owned():
    source = manager()
    adapter = AlertsHistoryAdapter(source)
    with pytest.raises(RuntimeError):
        adapter.poll()
    append(source, title="Node unavailable")
    rows, _ = source.recent_with_cursor()
    rows[0]["title"] = "Mutated"
    result = source.since(0)
    result["items"][0]["title"] = "Mutated again"
    snapshot = adapter.snapshot()
    snapshot["data"]["items"][0]["title"] = "Mutated DTO"
    assert source.recent()[0]["title"] == "Node unavailable"
    assert adapter.snapshot()["data"]["items"][0]["title"] == "Node unavailable"


def test_only_policy_accepted_alerts_enter_log_without_bridge():
    source = manager()
    assert append(source, key="kill_switch.disabled", alert_type=AlertType.KILL_SWITCH_DISABLED)
    assert not append(source, key="kill_switch.disabled", alert_type=AlertType.KILL_SWITCH_DISABLED)
    assert not append(source, key="")
    rows, cursor = source.recent_with_cursor()
    assert cursor == 1 and len(rows) == 1
    assert source.recent() == [{k: v for k, v in rows[0].items() if k != "alert_seq"}]
    source.config["telegram"] = {"enabled": True, "bot_token": "fake-secret", "chat_id": "42"}
    with patch.object(source, "_send_async") as dispatch:
        assert append(source)
    dispatch.assert_not_called()   # 提醒 redesign 10-30: local notices only
    assert len(source.recent()) == 2


def test_legacy_delivery_history_loads_without_becoming_rt6_alerts(tmp_path):
    path = tmp_path / "history.json"
    path.write_bytes(json.dumps({"schema_version": 1, "records": [{
        "timestamp": "2026-10-01T00:00:00+00:00", "channel_id": "desktop",
        "channel_type": "desktop", "outcome": "success", "error": "",
        "config_signature": "legacy",
    }]}).encode())
    store = AlertDeliveryStore(path)
    source = manager(delivery_store=store)
    assert len(store.recent()) == 1
    assert source.recent_with_cursor() == ([], 0)
    append(source)
    assert source.recent_with_cursor()[1] == 1
    payload = json.loads(path.read_bytes())
    assert payload["schema_version"] == 2
    assert "alert_seq" not in payload["records"][0]
    assert len(AlertDeliveryStore(path).recent()) == 1


@pytest.mark.parametrize("private", [
    "RuntimeError: raw-error-body", "Traceback (most recent call last): raw-error-body",
    r"C:\Private\runtime.log", r"\\server\share\runtime.log", "/opt/private/runtime.log",
    r"\\?\C:\Private\runtime.log",
])
def test_dto_excludes_paths_and_exception_shaped_text(private):
    source = manager()
    append(source, key=private, title=private, message=private)
    snapshot = AlertsHistoryAdapter(source).snapshot()
    raw = json.dumps(snapshot)
    assert private not in raw
    row = snapshot["data"]["items"][0]
    assert row["event_id"] == "alerts.unknown"
    assert set(row) == {"id", "seq", "severity", "event_id", "title", "detail", "ts_ms"}
    validate([snapshot])


@pytest.mark.parametrize("private", [
    "open('/var/lib/private.db')", "[Errno 13] Permission denied",
    str(KeyError("private-parser-value")), "raw socket failure without an exception name",
    r"opaque\u002dsecret", "opaque%2Dsecret",
])
def test_unclassified_text_and_encoded_channel_secrets_fail_closed(private):
    source = manager()
    source.config["telegram"] = {"enabled": False, "bot_token": "opaque-secret"}
    append(source, key=private, title=private, message=private)
    snapshot = AlertsHistoryAdapter(source).snapshot()
    row = snapshot["data"]["items"][0]
    assert row["title"] == row["detail"] == "Alert text unavailable"
    assert row["event_id"] == "alerts.unknown"
    assert "opaque-secret" not in json.dumps(snapshot)
    validate([snapshot])


def test_registered_templates_preserve_counts_but_not_free_text_parameters():
    source = manager()
    append(source, key="mode.tier1_degraded", title="Nodes degraded", message="Available: 2/3.")
    append(source, key="tier1.node_down", title="Node unavailable", message="Unavailable: private-node.")
    snapshot = AlertsHistoryAdapter(source).snapshot()
    rows = snapshot["data"]["items"]
    assert rows[0]["title"] == "Nodes degraded"
    assert rows[0]["detail"] == "Available: 2/3."
    assert rows[0]["event_id"] == "mode.tier1_degraded"
    assert rows[1]["title"] == "Node unavailable"
    assert rows[1]["detail"] == "Alert text unavailable"
    validate([snapshot])


def test_dto_redacts_configured_and_encoded_credentials_and_preserves_safe_text():
    source = manager()
    source.config["telegram"] = {"enabled": False, "bot_token": "opaque-secret"}
    append(source, key="opaque-secret", title="Warning opaque-secret",
           message="token=unknown-secret https://example.test/secret\n\ud800")
    snapshot = AlertsHistoryAdapter(source).snapshot()
    raw = json.dumps(snapshot)
    for secret in ("opaque-secret", "unknown-secret", "example.test", "token=", "\\ud800"):
        assert secret not in raw
    validate([snapshot])


@pytest.mark.parametrize("alert_type,severity", [
    (AlertType.WARNING, "warn"), (AlertType.CRITICAL, "critical"),
    (AlertType.EMERGENCY, "critical"), (AlertType.RECOVERY, "info"),
    (AlertType.KILL_SWITCH_ON, "critical"), (AlertType.BACKUP_REMINDER, "warn"),
])
def test_severity_projection(alert_type, severity):
    source = manager()
    append(source, alert_type=alert_type)
    snapshot = AlertsHistoryAdapter(source).snapshot()
    assert snapshot["data"]["items"][0]["severity"] == severity
    validate([snapshot])


@pytest.mark.parametrize("gap_reseed", [False, True])
def test_dto_failure_retries_without_committing_either_cursor(gap_reseed):
    import bridge_alerts_history as module

    source = manager(2 if gap_reseed else 10)
    append(source)
    adapter = AlertsHistoryAdapter(source)
    initial = adapter.snapshot()
    append(source)
    delivered = adapter.poll()  # Make both cursors nonzero before failure.
    for _ in range(3 if gap_reseed else 2):
        append(source)
    before = (adapter._cursor, adapter._edge_seq)
    original = module.alert_dto
    converted = []

    def fail_second(item, config):
        converted.append(item["alert_seq"])
        if len(converted) == 2:
            raise RuntimeError("injected DTO failure")
        return original(item, config)

    with patch.object(module, "alert_dto", side_effect=fail_second):
        with pytest.raises(RuntimeError, match="injected DTO failure"):
            adapter.poll()
    assert len(converted) == 2  # One DTO succeeded before the injected failure.
    assert (adapter._cursor, adapter._edge_seq) == before
    recovered = adapter.poll()
    if gap_reseed:
        gap, seed = recovered
        assert gap["kind"] == "gap" and gap["seq"] == 3
        assert gap["data"] == {"from_seq": 2, "to_seq": 2, "dropped": 1}
        assert seed["kind"] == "snapshot" and seed["data"]["last_seq"] == 5
        assert [row["seq"] for row in seed["data"]["items"]] == [4, 5]
        assert adapter._edge_seq == 3
    else:
        assert [event["seq"] for event in recovered] == [2, 3]
        assert [event["data"]["items"][0]["seq"] for event in recovered] == [3, 4]
        assert adapter._edge_seq == 3
    rows = [row["seq"] for event in [initial] + delivered + recovered
            if event["kind"] != "gap" for row in event["data"]["items"]]
    assert rows == ([1, 2, 4, 5] if gap_reseed else [1, 2, 3, 4])
    assert len(rows) == len(set(rows))
    assert adapter._cursor == (5 if gap_reseed else 4)
    assert adapter.poll() == []
    append(source)
    edge, = adapter.poll()
    assert edge["seq"] == 4
    assert edge["data"]["last_seq"] == (6 if gap_reseed else 5)
    assert adapter.poll() == []
    validate([initial] + delivered + recovered + [edge])
