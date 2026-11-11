from __future__ import annotations

import logging
import tempfile
import threading
import time
import unittest
from pathlib import Path

from bounded_logging import try_log_nowait
from log_capture import (
    DEFAULT_CAPTURE_CAPACITY,
    LogCaptureQueue,
    LogQueueHandler,
    MihomoStdoutFallbackTracker,
    StructuredEventJsonHandler,
    install_proxy_manager_log_capture,
    remove_proxy_manager_log_capture,
    seed_structured_event_log,
    seed_monitor_log,
)
from log_event_reports import log_event


class LogCaptureQueueTests(unittest.TestCase):
    def test_default_queue_is_bounded_at_ten_thousand(self) -> None:
        capture = LogCaptureQueue()

        self.assertEqual(capture.capacity, DEFAULT_CAPTURE_CAPACITY)
        for number in range(DEFAULT_CAPTURE_CAPACITY):
            self.assertTrue(capture.put_nowait({"number": number}))
        self.assertFalse(capture.put_nowait({"number": "overflow"}))
        self.assertEqual(capture.pending_count(), DEFAULT_CAPTURE_CAPACITY)
        self.assertEqual(capture.dropped_count, 1)

    def test_drain_is_fifo_and_never_exceeds_ui_batch_limit(self) -> None:
        capture = LogCaptureQueue(capacity=800)
        for number in range(620):
            capture.put_nowait({"number": number})

        first = capture.drain(limit=10_000)
        second = capture.drain(limit=500)

        self.assertEqual([item["number"] for item in first], list(range(500)))
        self.assertEqual([item["number"] for item in second], list(range(500, 620)))

    def test_clear_barrier_can_atomically_take_every_queued_record(self) -> None:
        capture = LogCaptureQueue(capacity=800)
        for number in range(620):
            capture.put_nowait({"number": number})

        records = capture.drain_all()

        self.assertEqual([item["number"] for item in records], list(range(620)))
        self.assertEqual(capture.pending_count(), 0)

    def test_sidecar_failure_never_prevents_live_mihomo_enqueue(self) -> None:
        class FailingSidecar(StructuredEventJsonHandler):
            def __init__(self) -> None:
                logging.Handler.__init__(self)

            def enqueue_captured(self, record):
                raise OSError("sidecar unavailable")

            def close(self) -> None:
                logging.Handler.close(self)

        logger = logging.Logger("failing-sidecar", level=logging.DEBUG)
        logger.propagate = False
        handler = FailingSidecar()
        logger.addHandler(handler)
        capture = LogCaptureQueue(capacity=2)
        capture.bind_external_event_logger(logger)

        accepted = capture.put_nowait(
            {
                "source": "MIHOMO",
                "level": "INFO",
                "message_raw": "configuration invalid: missing provider",
                "metadata": {"capture_kind": "mihomo_stream"},
            }
        )

        self.assertTrue(accepted)
        self.assertEqual(capture.pending_count(), 1)
        self.assertEqual(capture.sidecar_failure_count, 1)
        logger.removeHandler(handler)
        handler.close()

    def test_registered_event_overrides_observed_category_and_key_flag(self) -> None:
        capture = LogCaptureQueue(capacity=2)
        capture.put_nowait(
            {
                "source": "MIHOMO",
                "level": "CRITICAL",
                "event_key": "routing.route_switched",
                "category": "wrong",
                "is_key_event": False,
                "event_params": {"target": "Sentinel"},
                "message_raw": "complete evidence",
            }
        )

        record = capture.drain()[0]
        self.assertEqual(record["level"], "INFO")
        self.assertEqual(record["category"], "routing")
        self.assertTrue(record["is_key_event"])
        self.assertEqual(record["metadata"]["observed_level"], "CRITICAL")
        self.assertEqual(record["metadata"]["observed_category"], "wrong")
        self.assertFalse(record["metadata"]["observed_is_key_event"])


class LogQueueHandlerTests(unittest.TestCase):
    def _logger(self, name: str) -> logging.Logger:
        logger = logging.Logger(name, level=logging.DEBUG)
        logger.propagate = False
        return logger

    def test_handler_preserves_exception_and_stack_in_raw_message(self) -> None:
        capture = LogCaptureQueue(capacity=10)
        logger = self._logger("capture-exception")
        logger.addHandler(LogQueueHandler(capture))

        try:
            raise ValueError("broken capture")
        except ValueError:
            logger.error("startup failed", exc_info=True, stack_info=True)

        record = capture.drain()[0]
        self.assertEqual(record["source"], "TESSERACT")
        self.assertEqual(record["level"], "ERROR")
        self.assertIn("startup failed", record["raw_message"])
        self.assertIn("Traceback", record["raw_message"])
        self.assertIn("ValueError: broken capture", record["raw_message"])
        self.assertIn("Stack", record["raw_message"])

    def test_install_is_idempotent_and_remove_is_scoped(self) -> None:
        logger = self._logger("capture-install")
        capture = LogCaptureQueue()

        first = install_proxy_manager_log_capture(capture, logger=logger)
        second = install_proxy_manager_log_capture(capture, logger=logger)

        self.assertIs(first, second)
        self.assertEqual(sum(isinstance(item, LogQueueHandler) for item in logger.handlers), 1)
        self.assertEqual(remove_proxy_manager_log_capture(first, logger=logger), 1)
        self.assertFalse(logger.handlers)

    def test_try_log_nowait_forwards_logging_kwargs(self) -> None:
        capture = LogCaptureQueue(capacity=10)
        logger = self._logger("capture-kwargs")
        logger.addHandler(LogQueueHandler(capture))

        try:
            raise RuntimeError("runtime failed")
        except RuntimeError:
            emitted = try_log_nowait(
                logger,
                "error",
                "failed with %s",
                "details",
                exc_info=True,
                extra={"event_key": "runtime.failed", "category": "RUNTIME"},
            )

        self.assertTrue(emitted)
        record = capture.drain()[0]
        self.assertEqual(record["event_key"], "runtime.failed")
        self.assertEqual(record["category"], "RUNTIME")
        self.assertIn("RuntimeError: runtime failed", record["raw_message"])

    def test_queue_enriches_exact_core_lifecycle_and_mihomo_warning(self) -> None:
        capture = LogCaptureQueue(capacity=10)

        self.assertTrue(
            capture.put_nowait(
                {
                    "source": "TESSERACT",
                    "level": "INFO",
                    "message_raw": "Hot-reloaded config via http://127.0.0.1:9090/configs",
                }
            )
        )
        self.assertTrue(
            capture.put_nowait(
                {
                    "source": "MIHOMO",
                    "level": "WARNING",
                    "message_raw": "future warning vocabulary",
                }
            )
        )

        lifecycle, warning = capture.drain()
        self.assertEqual(lifecycle["event_key"], "configuration.applied")
        self.assertEqual(lifecycle["category"], "configuration")
        self.assertTrue(lifecycle["is_key_event"])
        self.assertEqual(warning["event_key"], "mihomo.uncategorized_warning")
        self.assertEqual(warning["event_match_kind"], "mihomo_safe_fallback")

    def test_tesseract_generic_errors_are_not_guessed_as_mihomo_events(self) -> None:
        capture = LogCaptureQueue(capacity=10)
        capture.put_nowait(
            {
                "source": "TESSERACT",
                "level": "ERROR",
                "message_raw": "Subscription configuration failed for an unrelated reason",
            }
        )

        record = capture.drain()[0]
        self.assertFalse(record.get("event_key"))
        self.assertFalse(record.get("is_key_event"))

    def test_bare_first_party_warning_is_explicitly_captured_as_raw_only(self) -> None:
        capture = LogCaptureQueue(capacity=10)
        logger = self._logger("capture-raw-only")
        logger.addHandler(LogQueueHandler(capture))

        logger.warning("per-node diagnostic failure")

        record = capture.drain()[0]
        self.assertFalse(record["is_key_event"])
        self.assertEqual(record["event_match_kind"], "raw_only")
        self.assertEqual(record["metadata"]["event_match_kind"], "raw_only")


class MonitorLogSeedTests(unittest.TestCase):
    def test_seed_reads_only_last_500_lines_of_current_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "monitor.log"
            rotated_path = Path(temp_dir) / "monitor.log.1"
            lines = []
            for number in range(510):
                level = "CRITICAL" if number == 509 else "INFO"
                lines.append(f"2026-07-15 10:00:{number % 60:02d},123 [{level}] current-{number}\n")
            log_path.write_text("".join(lines), encoding="utf-8")
            rotated_path.write_text(
                "2026-07-14 10:00:00,000 [ERROR] rotated-marker\n",
                encoding="utf-8",
            )

            records = seed_monitor_log(log_path)

        self.assertEqual(len(records), 500)
        self.assertEqual(records[0]["raw_message"], "current-10")
        self.assertEqual(records[-1]["raw_message"], "current-509")
        self.assertEqual(records[-1]["level"], "CRITICAL")
        self.assertTrue(all(item["source"] == "TESSERACT" for item in records))
        self.assertNotIn("rotated-marker", {item["raw_message"] for item in records})

    def test_missing_monitor_log_is_an_empty_seed(self) -> None:
        self.assertEqual(seed_monitor_log(Path("definitely-missing-monitor.log")), [])

    def test_seed_keeps_traceback_lines_with_their_entry(self) -> None:
        lines = [
            '  File "orphan.py", line 1, in cut_off_entry',
            "2026-09-20 04:35:51,983 [WARNING] Runtime listener preflight found occupied endpoints",
            "2026-09-20 04:35:51,984 [ERROR] Desktop runtime start failed",
            "Traceback (most recent call last):",
            '  File "desktop_runtime.py", line 2056, in _start_locked',
            "desktop_runtime.RuntimeListenerPreflightError: port in use",
            "2026-09-20 04:38:41,955 [INFO] Desktop runtime start requested",
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "monitor.log"
            log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            records = seed_monitor_log(log_path)

        self.assertEqual(len(records), 3)
        failure = records[1]
        self.assertEqual(failure["level"], "ERROR")
        self.assertEqual(
            failure["raw_message"],
            "\n".join(["Desktop runtime start failed", *lines[3:6]]),
        )
        self.assertNotIn("orphan", " ".join(item["raw_message"] for item in records))
        self.assertEqual(records[2]["raw_message"], "Desktop runtime start requested")

    def test_merged_seed_is_time_ordered_and_sidecar_copy_wins_dedupe(self) -> None:
        from log_capture import merge_seed_records
        from log_store import LogStore

        message = "Desktop runtime start failed\nTraceback (most recent call last):\nboom"
        sidecar = [
            {
                "timestamp": "2026-09-20T04:30:00.000-04:00",
                "source": "TESSERACT",
                "level": "INFO",
                "message_raw": "older sidecar event",
            },
            {
                "timestamp": "2026-09-20T04:35:51.984-04:00",
                "source": "TESSERACT",
                "level": "ERROR",
                "event_key": "runtime.desktop_failed",
                "message_raw": message,
                "metadata": {"capture_kind": "structured_event_tail"},
            },
        ]
        monitor = [
            {
                "timestamp": "2026-09-20T04:31:00.000-04:00",
                "source": "TESSERACT",
                "level": "INFO",
                "message": "monitor line between sidecar events",
            },
            {
                "timestamp": "2026-09-20T04:35:51.984-04:00",
                "source": "TESSERACT",
                "level": "ERROR",
                "message": message,
                "metadata": {"capture_kind": "monitor_tail"},
            },
        ]

        merged = merge_seed_records(sidecar, monitor)
        store = LogStore()
        store.append_many(merged)
        rows = store.snapshot(tab="raw")

        self.assertEqual(
            [row.message_raw.splitlines()[0] for row in rows],
            [
                "older sidecar event",
                "monitor line between sidecar events",
                "Desktop runtime start failed",
            ],
        )
        self.assertEqual(rows[-1].metadata["capture_kind"], "structured_event_tail")
        self.assertEqual(rows[-1].event_key, "runtime.desktop_failed")


class StructuredEventSidecarTests(unittest.TestCase):
    def test_sidecar_writes_only_structured_events_and_seeds_current_file(self) -> None:
        with tempfile.TemporaryDirectory(dir=".") as temp_dir:
            path = Path(temp_dir) / "events.jsonl"
            logger = logging.Logger("sidecar-test", level=logging.DEBUG)
            handler = StructuredEventJsonHandler(path, max_bytes=1024 * 1024, backup_count=1)
            logger.addHandler(handler)
            try:
                logger.warning("unstructured warning")
                log_event(
                    logger,
                    "INFO",
                    "routing.route_switched",
                    "full raw route evidence",
                    params={"target": "Sentinel"},
                )
                handler.flush()
                records = seed_structured_event_log(path)
            finally:
                logger.removeHandler(handler)
                handler.close()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["event_key"], "routing.route_switched")
        self.assertEqual(records[0]["message_raw"], "full raw route evidence")
        self.assertEqual(records[0]["event_params"]["target"], "Sentinel")
        self.assertEqual(records[0]["metadata"]["capture_kind"], "structured_event_tail")

    def test_live_and_stdout_mihomo_events_share_the_durable_sidecar(self) -> None:
        with tempfile.TemporaryDirectory(dir=".") as temp_dir:
            path = Path(temp_dir) / "events.jsonl"
            logger = logging.Logger("mihomo-sidecar-test", level=logging.DEBUG)
            logger.propagate = False
            sidecar = StructuredEventJsonHandler(path, max_bytes=1024 * 1024, backup_count=1)
            logger.addHandler(sidecar)
            capture = LogCaptureQueue(capacity=10)
            queue_handler = install_proxy_manager_log_capture(capture, logger=logger)
            tracker = MihomoStdoutFallbackTracker()
            try:
                stream_record = {
                    "timestamp": "2026-07-18T02:00:00.000100Z",
                    "source": "MIHOMO",
                    "level": "INFO",
                    "message_raw": "configuration invalid: proxy provider missing",
                    "metadata": {"capture_kind": "mihomo_stream"},
                }
                capture.put_nowait(stream_record)
                equivalent_millisecond = dict(stream_record)
                equivalent_millisecond["timestamp"] = "2026-07-18T02:00:00.000900+00:00"
                capture.put_nowait(equivalent_millisecond)
                for record in tracker.consume_once(
                    "generation-1",
                    "2026-07-18 10:00:00,001 [INFO] controller bind failed",
                ):
                    capture.put_nowait(record)
                sidecar.flush()
                first_seed = seed_structured_event_log(path)

                # Re-enqueueing a startup seed must not append it to the
                # sidecar again and grow the current file on every launch.
                capture.put_nowait(first_seed[0])
                sidecar.flush()
                second_seed = seed_structured_event_log(path)
            finally:
                remove_proxy_manager_log_capture(queue_handler, logger=logger)
                logger.removeHandler(sidecar)
                sidecar.close()

        self.assertEqual(
            [item["event_key"] for item in first_seed],
            ["mihomo.configuration_failed", "runtime.mihomo_failed"],
        )
        self.assertTrue(all(item["source"] == "MIHOMO" for item in first_seed))
        self.assertTrue(all(item["level"] == "ERROR" for item in first_seed))
        self.assertEqual(len(second_seed), 2)

    def test_closed_sidecar_cannot_reopen_itself_from_external_capture(self) -> None:
        with tempfile.TemporaryDirectory(dir=".") as temp_dir:
            path = Path(temp_dir) / "events.jsonl"
            handler = StructuredEventJsonHandler(path, max_bytes=1024 * 1024, backup_count=1)
            handler.close()

            persisted = handler.append_captured(
                {
                    "timestamp": "2026-07-18T10:00:00.001+08:00",
                    "source": "MIHOMO",
                    "level": "ERROR",
                    "message_raw": "configuration invalid: missing provider",
                    "metadata": {"capture_kind": "mihomo_stream"},
                }
            )

            lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []

        self.assertFalse(persisted)
        self.assertEqual(lines, [])

    def test_live_mihomo_enqueue_does_not_wait_for_slow_sidecar_io(self) -> None:
        with tempfile.TemporaryDirectory(dir=".") as temp_dir:
            path = Path(temp_dir) / "events.jsonl"
            logger = logging.Logger("slow-sidecar-test", level=logging.DEBUG)
            logger.propagate = False
            sidecar = StructuredEventJsonHandler(path, max_bytes=1024 * 1024, backup_count=1)
            logger.addHandler(sidecar)
            capture = LogCaptureQueue(capacity=10)
            capture.bind_external_event_logger(logger)
            started = threading.Event()
            release = threading.Event()
            original_write = sidecar._write_payload

            def slow_write(payload, *, logger_name, levelno, levelname):
                started.set()
                release.wait(timeout=1.0)
                return original_write(
                    payload,
                    logger_name=logger_name,
                    levelno=levelno,
                    levelname=levelname,
                )

            sidecar._write_payload = slow_write  # type: ignore[method-assign]
            try:
                before = time.perf_counter()
                accepted = capture.put_nowait(
                    {
                        "timestamp": "2026-07-18T10:00:00.001+08:00",
                        "source": "MIHOMO",
                        "level": "ERROR",
                        "message_raw": "configuration invalid: missing provider",
                        "metadata": {"capture_kind": "mihomo_stream"},
                    }
                )
                elapsed = time.perf_counter() - before
                self.assertTrue(started.wait(timeout=1.0))
                self.assertTrue(accepted)
                self.assertLess(elapsed, 0.15)
                self.assertEqual(capture.pending_count(), 1)
            finally:
                release.set()
                sidecar.flush()
                logger.removeHandler(sidecar)
                sidecar.close()

    def test_real_sink_failure_is_counted_without_poisoning_dedupe(self) -> None:
        class BrokenStream:
            def seek(self, *_args) -> None:
                return None

            def tell(self) -> int:
                return 0

            def write(self, _value: str) -> int:
                raise OSError("disk write failed")

            def flush(self) -> None:
                return None

            def close(self) -> None:
                return None

        with tempfile.TemporaryDirectory(dir=".") as temp_dir:
            path = Path(temp_dir) / "events.jsonl"
            logger = logging.Logger("broken-stream-sidecar", level=logging.DEBUG)
            logger.propagate = False
            sidecar = StructuredEventJsonHandler(path, max_bytes=1024 * 1024, backup_count=1)
            logger.addHandler(sidecar)
            original_stream = sidecar._sink.stream
            if original_stream is not None:
                original_stream.close()
            sidecar._sink.stream = BrokenStream()
            capture = LogCaptureQueue(capacity=10)
            capture.bind_external_event_logger(logger)
            try:
                accepted = capture.put_nowait(
                    {
                        "timestamp": "2026-07-18T10:00:00.001+08:00",
                        "source": "MIHOMO",
                        "level": "ERROR",
                        "message_raw": "configuration invalid: missing provider",
                        "metadata": {"capture_kind": "mihomo_stream"},
                    }
                )
                sidecar.flush()
                file_size = path.stat().st_size if path.exists() else 0
            finally:
                logger.removeHandler(sidecar)
                sidecar.close()

        self.assertTrue(accepted)
        self.assertEqual(capture.pending_count(), 1)
        self.assertEqual(sidecar.external_failure_count, 1)
        self.assertEqual(len(sidecar._fingerprints), 0)
        self.assertEqual(file_size, 0)


class MihomoStdoutFallbackTrackerTests(unittest.TestCase):
    def test_stdout_is_consumed_once_per_runtime_generation(self) -> None:
        tracker = MihomoStdoutFallbackTracker()
        text = "2026-07-15 10:00:00,001 [ERROR] controller bind failed"

        first = tracker.consume_once(7, text)
        duplicate = tracker.consume_once(7, "a later duplicate")
        next_generation = tracker.consume_once(8, "startup retry")

        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["source"], "MIHOMO")
        self.assertEqual(first[0]["level"], "ERROR")
        self.assertEqual(first[0]["metadata"]["runtime_generation"], "7")
        self.assertEqual(duplicate, [])
        self.assertEqual(len(next_generation), 1)

    def test_empty_stdout_does_not_consume_generation(self) -> None:
        tracker = MihomoStdoutFallbackTracker()

        self.assertEqual(tracker.consume_once("g1", ""), [])
        self.assertFalse(tracker.was_consumed("g1"))
        self.assertEqual(len(tracker.consume_once("g1", "now available")), 1)
        self.assertTrue(tracker.was_consumed("g1"))


if __name__ == "__main__":
    unittest.main()
