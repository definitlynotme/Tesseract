import unittest

from ui_format import fmt, fmt_bool, fmt_bytes, fmt_percent, fmt_rate, fmt_uptime, short_result


class UiFormatTests(unittest.TestCase):
    def test_fmt_uses_fallback_for_empty_values(self) -> None:
        self.assertEqual(fmt(None), "--")
        self.assertEqual(fmt(""), "--")
        self.assertEqual(fmt("", ""), "")
        self.assertEqual(fmt(0), "0")

    def test_bool_uptime_bytes_rate_and_percent_formatting(self) -> None:
        self.assertEqual(fmt_bool(True), "on")
        self.assertEqual(fmt_bool(False), "off")
        self.assertEqual(fmt_uptime(3661), "1h 1m 1s")
        self.assertEqual(fmt_uptime("bad"), "--")
        self.assertEqual(fmt_bytes(1024), "1.00 KB")
        self.assertEqual(fmt_bytes("bad"), "0 B")
        self.assertEqual(fmt_rate(2048), "2.00 KB/s")
        self.assertEqual(fmt_percent(1, 4), "25.0%")
        self.assertEqual(fmt_percent(1, 0), "--")

    def test_short_result_matches_task_notice_cases(self) -> None:
        self.assertEqual(short_result({"action": "import", "status": "ok"}), "import: ok")
        self.assertEqual(short_result({"added": 2, "duplicate_count": 1}), "added 2, skipped 1 duplicate")
        self.assertEqual(short_result({"path": "C:/tmp/out.yaml"}), "C:/tmp/out.yaml")
        self.assertEqual(short_result(object()), "done")


if __name__ == "__main__":
    unittest.main()
