import tempfile
import threading
import time
import unittest
from pathlib import Path

from traffic_monitor import MihomoTrafficMonitor, parse_traffic_sample


class StreamingResponse:
    def __init__(self, lines):
        self.lines = lines
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def __iter__(self):
        for line in self.lines:
            if self.closed:
                break
            yield line

    def close(self):
        self.closed = True


class TrafficMonitorTests(unittest.TestCase):
    def test_parse_json_line_sample(self) -> None:
        sample = parse_traffic_sample(b'{"up": 12, "down": 34}\n')

        self.assertEqual(sample, {"up": 12.0, "down": 34.0})

    def test_stream_samples_are_collected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            times = iter([1000.0, 1001.0, 1002.0, 1003.0])

            def now_fn():
                try:
                    return next(times)
                except StopIteration:
                    return 1003.0

            def opener(request, timeout=0):
                self.assertTrue(str(request.full_url).endswith("/traffic"))
                return StreamingResponse([b'{"up": 10, "down": 20}\n', b'{"up": 30, "down": 40}\n'])

            monitor = MihomoTrafficMonitor(
                base_url="http://127.0.0.1:9090",
                history_path=Path(temp_dir) / "traffic-history.json",
                opener=opener,
                now_fn=now_fn,
            )
            monitor.start()
            for _ in range(50):
                if len(monitor.snapshot()["history"]) == 2:
                    break
                time.sleep(0.01)

            snapshot = monitor.snapshot()

            self.assertFalse(snapshot["running"])
            self.assertEqual(snapshot["latest"]["up"], 30.0)
            self.assertEqual(snapshot["latest"]["down"], 40.0)
            self.assertGreater(snapshot["session"]["upload_bytes"], 0)
            self.assertGreater(snapshot["session"]["download_bytes"], 0)
            self.assertTrue((Path(temp_dir) / "traffic-history.json").exists())

    def test_request_failure_returns_empty_state_with_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            def opener(request, timeout=0):
                raise OSError("controller unavailable")

            monitor = MihomoTrafficMonitor(
                base_url="http://127.0.0.1:9090",
                history_path=Path(temp_dir) / "traffic-history.json",
                opener=opener,
            )
            monitor.start()
            for _ in range(50):
                if not monitor.snapshot()["running"]:
                    break
                time.sleep(0.01)

            snapshot = monitor.snapshot()

            self.assertFalse(snapshot["running"])
            self.assertEqual(snapshot["history"], [])
            self.assertIn("controller unavailable", snapshot["error"])

    def test_string_false_enabled_does_not_start_monitor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            def opener(_request, timeout=0):
                raise AssertionError("traffic monitor should not open a stream")

            monitor = MihomoTrafficMonitor(
                base_url="http://127.0.0.1:9090",
                history_path=Path(temp_dir) / "traffic-history.json",
                enabled="false",
                opener=opener,
            )
            monitor.start()

            snapshot = monitor.snapshot()

            self.assertFalse(monitor.enabled)
            self.assertFalse(snapshot["running"])
            self.assertEqual(snapshot["history"], [])

    def test_history_keeps_only_recent_24_hours(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            monitor = MihomoTrafficMonitor(
                base_url="http://127.0.0.1:9090",
                history_path=Path(temp_dir) / "traffic-history.json",
                now_fn=lambda: 100000.0,
            )
            monitor.record_sample({"up": 1, "down": 1}, timestamp_epoch=100000.0 - 90000.0)
            monitor.record_sample({"up": 2, "down": 3}, timestamp_epoch=100000.0)

            snapshot = monitor.snapshot()

            self.assertEqual(len(snapshot["history"]), 1)
            self.assertEqual(snapshot["latest"]["up"], 2.0)
            self.assertEqual(snapshot["latest"]["down"], 3.0)

    def test_session_totals_grow_with_samples(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            monitor = MihomoTrafficMonitor(
                base_url="http://127.0.0.1:9090",
                history_path=Path(temp_dir) / "traffic-history.json",
            )
            monitor.record_sample({"up": 100, "down": 200}, timestamp_epoch=0.0)
            first = monitor.snapshot()["session"]
            monitor.record_sample({"up": 300, "down": 500}, timestamp_epoch=2.0)
            second = monitor.snapshot()["session"]
            monitor.record_sample({"up": 400, "down": 600}, timestamp_epoch=4.0)
            third = monitor.snapshot()["session"]

            self.assertEqual(first, {"upload_bytes": 0, "download_bytes": 0})
            self.assertGreater(second["upload_bytes"], first["upload_bytes"])
            self.assertGreater(second["download_bytes"], first["download_bytes"])
            self.assertGreater(third["upload_bytes"], second["upload_bytes"])
            self.assertGreater(third["download_bytes"], second["download_bytes"])

    def test_session_totals_cap_elapsed_after_sleep(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            monitor = MihomoTrafficMonitor(
                base_url="http://127.0.0.1:9090",
                history_path=Path(temp_dir) / "traffic-history.json",
            )

            monitor.record_sample({"up": 100, "down": 200}, timestamp_epoch=0.0)
            monitor.record_sample({"up": 100, "down": 200}, timestamp_epoch=3600.0)

            self.assertEqual(monitor.snapshot()["session"], {"upload_bytes": 500, "download_bytes": 1000})

    def test_history_writes_are_batched_between_flushes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            monitor = MihomoTrafficMonitor(
                base_url="http://127.0.0.1:9090",
                history_path=Path(temp_dir) / "traffic-history.json",
            )
            save_count = 0
            original_save = monitor._save_history_locked

            def counted_save(*, saved_at_epoch=None):
                nonlocal save_count
                save_count += 1
                original_save(saved_at_epoch=saved_at_epoch)

            monitor._save_history_locked = counted_save

            monitor.record_sample({"up": 100, "down": 200}, timestamp_epoch=0.0)
            monitor.record_sample({"up": 100, "down": 200}, timestamp_epoch=1.0)
            monitor.record_sample({"up": 100, "down": 200}, timestamp_epoch=2.0)

            self.assertEqual(save_count, 1)
            monitor.stop()
            self.assertEqual(save_count, 2)

    def test_stop_is_bounded_when_response_close_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            close_started = threading.Event()
            close_release = threading.Event()

            class BlockingResponse:
                def close(self):
                    close_started.set()
                    close_release.wait(timeout=5.0)

            monitor = MihomoTrafficMonitor(
                base_url="http://127.0.0.1:9090",
                history_path=Path(temp_dir) / "traffic-history.json",
            )
            monitor._response = BlockingResponse()

            started_at = time.monotonic()
            try:
                monitor.stop(timeout=0.2)
                elapsed = time.monotonic() - started_at
                self.assertTrue(close_started.wait(timeout=0.1))
                self.assertLess(elapsed, 0.3)
            finally:
                close_release.set()


if __name__ == "__main__":
    unittest.main()
