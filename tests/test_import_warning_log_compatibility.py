from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from log_store import LogStore, serialize_log_records


NOW = datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc)


def _record(
    message: str,
    *,
    level: str = "INFO",
    event_key: str | None = None,
    offset: int = 0,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "timestamp": NOW.replace(microsecond=offset * 1000).isoformat(),
        "received_at": NOW.replace(microsecond=(offset + 1) * 1000).isoformat(),
        "source": "TESSERACT",
        "level": level,
        "message": message,
    }
    if event_key:
        payload.update(
            {
                "event_key": event_key,
                "event_group": "configuration",
                "event_params": {"scope": "import"},
            }
        )
    return payload


class ImportWarningLogCompatibilityTests(unittest.TestCase):
    def test_compact_and_raw_views_keep_levels_fields_filters_and_export(self) -> None:
        store = LogStore(now=lambda: NOW)
        raw_warning = "Import diagnostic details: future-cipher at source line 7"
        appended = store.append_many(
            [
                _record("ordinary import trace"),
                _record(
                    "Import validation failed",
                    level="WARNING",
                    event_key="alert.legacy",
                    offset=1,
                ),
                _record(raw_warning, level="WARNING", offset=2),
            ]
        )

        self.assertEqual([record.level for record in appended], ["INFO", "WARNING", "WARNING"])
        self.assertEqual([record.message_raw for record in store.snapshot(tab="raw")][-1], raw_warning)
        key_records = store.snapshot(tab="key")
        self.assertEqual(len(key_records), 1)
        self.assertEqual(key_records[0].event_key, "alert.legacy")
        self.assertEqual(key_records[0].metadata["event_params"]["scope"], "import")

        filtered = store.snapshot(
            {
                "tab": "raw",
                "source": "tesseract",
                "levels": ["WARNING"],
                "query": "future-cipher",
            }
        )
        self.assertEqual([record.message_raw for record in filtered], [raw_warning])

        payload = json.loads(serialize_log_records(filtered, "jsonl"))
        self.assertEqual(payload["level"], "WARNING")
        self.assertEqual(payload["message_raw"], raw_warning)
        self.assertNotIn("report", payload)
        text = serialize_log_records(filtered, "text")
        self.assertIn(raw_warning, text)


if __name__ == "__main__":
    unittest.main()
