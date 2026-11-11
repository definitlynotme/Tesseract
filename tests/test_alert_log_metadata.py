from __future__ import annotations

import logging
import threading
import unittest

from alerts import AlertEvent, AlertManager
from log_capture import LogCaptureQueue, LogQueueHandler
from models import AlertType


class _RecordHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class AlertLogMetadataTests(unittest.TestCase):
    def test_alert_logger_record_contains_structured_event_metadata(self) -> None:
        logger = logging.Logger("alert-metadata", level=logging.DEBUG)
        handler = _RecordHandler()
        capture = LogCaptureQueue(capacity=10)
        logger.addHandler(handler)
        logger.addHandler(LogQueueHandler(capture))
        manager = AlertManager({"desktop": {"enabled": False}}, logger)

        # tier1.node_down stays a key event; mode.* notifications are logged
        # too but the event flow records mode changes as tier.* events.
        manager.send_event(
            AlertEvent(
                event_key="tier1.node_down",
                event_group="route_health",
                alert_type=AlertType.CRITICAL,
                title="Route degraded",
                message="Sentinel active",
                source="manager.evaluate_state",
                scope="vanguard",
                metadata={"tier1_alive": 1, "tier1_total": 3},
            )
        )

        record = handler.records[-1]
        self.assertEqual(record.event_key, "tier1.node_down")
        self.assertEqual(record.event_group, "route_health")
        self.assertEqual(record.alert_type, AlertType.CRITICAL.value)
        self.assertEqual(record.category, "health")
        self.assertEqual(record.levelname, "WARNING")
        self.assertTrue(record.is_key_event)
        self.assertEqual(record.event_params["source"], "manager.evaluate_state")
        self.assertEqual(record.event_params["scope"], "vanguard")
        self.assertEqual(record.event_params["title"], "Route degraded")
        self.assertEqual(record.event_params["message"], "Sentinel active")
        self.assertEqual(record.event_params["metadata"]["tier1_alive"], 1)

        captured = capture.drain()[0]
        self.assertEqual(captured["source"], "ALERT")
        self.assertEqual(captured["category"], "health")
        self.assertEqual(captured["event_key"], "tier1.node_down")
        self.assertTrue(captured["is_key_event"])
        self.assertEqual(captured["metadata"]["event_params"]["scope"], "vanguard")
        self.assertEqual(captured["metadata"]["event_params"]["metadata"]["tier1_total"], 3)

    def test_history_append_and_recent_are_safe_under_concurrent_access(self) -> None:
        logger = logging.Logger("alert-history-concurrency", level=logging.CRITICAL + 1)
        manager = AlertManager({"history_limit": 200}, logger)
        failures: list[BaseException] = []
        writers_done = threading.Event()

        def writer(prefix: str) -> None:
            try:
                for number in range(250):
                    manager.send_event(
                        AlertEvent(
                            event_key="test.concurrent_history",
                            event_group="route_health",
                            alert_type=AlertType.WARNING,
                            title=prefix,
                            message=str(number),
                            source="test",
                        )
                    )
            except BaseException as exc:  # pragma: no cover - assertion aid
                failures.append(exc)

        def reader() -> None:
            try:
                while not writers_done.is_set():
                    snapshot = manager.recent(200)
                    if len(snapshot) > 200:
                        raise AssertionError("history snapshot exceeded its configured bound")
            except BaseException as exc:  # pragma: no cover - assertion aid
                failures.append(exc)

        reader_thread = threading.Thread(target=reader)
        writers = [threading.Thread(target=writer, args=(f"writer-{number}",)) for number in range(4)]
        reader_thread.start()
        for thread in writers:
            thread.start()
        for thread in writers:
            thread.join()
        writers_done.set()
        reader_thread.join()

        self.assertEqual(failures, [])
        self.assertEqual(len(manager.recent(500)), 200)


if __name__ == "__main__":
    unittest.main()
