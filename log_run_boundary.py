"""Qt-free copy of run boundary helpers; Qt callers stay frozen.

Copied from log_console_view.py:122-133 (_value), :140-142
(_normalized_source), :158-159 (_raw_message), :263-272 (is_run_start).
"""

from typing import Any, Mapping

RUN_START_EVENT_KEY = "runtime.start_requested"
_LEGACY_RUN_START_PREFIX = "Desktop runtime start requested"


def _value(record: Any, name: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        if name in record:
            return record[name]
        if name == "message_raw":
            return record.get("message", default)
        if name == "occurred_at":
            return record.get("time", default)
        return default
    return getattr(record, name, default)


def _normalized_source(record: Any) -> str:
    source = str(_value(record, "source", "UNKNOWN") or "UNKNOWN").strip().upper()
    return source or "UNKNOWN"


def _raw_message(record: Any) -> str:
    return str(_value(record, "message_raw", "") or "")


def is_run_start(record: Any) -> bool:
    if str(_value(record, "event_key", "") or "") == RUN_START_EVENT_KEY:
        return True
    return _normalized_source(record) == "TESSERACT" and _raw_message(record).startswith(
        _LEGACY_RUN_START_PREFIX
    )
