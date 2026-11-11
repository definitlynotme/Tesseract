from __future__ import annotations

from typing import Any


def fmt(value: Any, fallback: str = "--") -> str:
    if value is None or value == "":
        return fallback
    return str(value)


def fmt_bool(value: Any) -> str:
    return "on" if value else "off"


def fmt_uptime(seconds: Any) -> str:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return "--"
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    return f"{hours}h {minutes}m {secs}s"


def fmt_bytes(value: Any) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        amount = 0.0
    units = ["B", "KB", "MB", "GB", "TB"]
    index = 0
    while amount >= 1024 and index < len(units) - 1:
        amount /= 1024
        index += 1
    if index == 0:
        return f"{int(amount)} {units[index]}"
    return f"{amount:.2f} {units[index]}"


def fmt_rate(value: Any) -> str:
    return f"{fmt_bytes(value)}/s"


def fmt_percent(part: Any, total: Any) -> str:
    try:
        part_value = float(part)
        total_value = float(total)
    except (TypeError, ValueError):
        return "--"
    if total_value <= 0:
        return "--"
    return f"{(part_value / total_value) * 100:.1f}%"


def short_result(result: Any) -> str:
    if isinstance(result, dict):
        if "status" in result and "action" in result:
            return f"{result['action']}: {result['status']}"
        if "added" in result:
            parts = [f"added {result['added']}"]
            skipped = int(result.get("skipped", 0) or result.get("duplicate_count", 0) or 0)
            if skipped:
                parts.append(f"skipped {skipped} duplicate")
            return ", ".join(parts)
        if "path" in result:
            return str(result["path"])
    return "done"
