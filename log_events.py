from __future__ import annotations

import unicodedata
import re
from datetime import datetime
from typing import Any, Callable


LOG_LINE_PATTERN = re.compile(
    r"^(?:(?P<date>\d{4}-\d{2}-\d{2})\s+)?(?P<time>\d{2}:\d{2}:\d{2})(?:,\d+)?\s+\[(?P<level>[A-Za-z]+)\]\s+(?P<message>.*)$"
)
LOG_LEVELS = ("ALL", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
LOG_SOURCES = ("ALL", "TESSERACT", "MIHOMO", "ALERT", "UNKNOWN")
KNOWN_LOG_SOURCES = frozenset((*LOG_SOURCES, "CONNECTION"))

LOG_SOURCE_LABELS = {
    "TESSERACT": "Tesseract 日志",
    "MIHOMO": "Mihomo 日志",
    "CONNECTION": "连接",
    "ALERT": "运行告警",
    "UNKNOWN": "未知来源",
}

ALERT_TYPE_LABELS = {
    "WARNING": "警告",
    "CRITICAL": "严重",
    "EMERGENCY": "紧急",
    "RECOVERY": "恢复",
    "POOL_UPDATE": "池更新",
    "BACKUP_REMINDER": "备份提醒",
    "KILL_SWITCH_ON": "熔断开启",
    "KILL_SWITCH_OFF": "熔断关闭",
    "KILL_SWITCH_DISABLED": "熔断不可用",
    "KILL_SWITCH_OVERRIDE": "熔断手动覆盖",
}

RUNTIME_NOTICE_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        r"^Finished initial GeoSite rule (?P<rule>.+?) => .+?, records: (?P<records>\d+)$",
        "GeoSite 规则初始化完成，记录数: {records}",
    ),
    (
        r"^Initial configuration complete, total time: (?P<duration>.+)$",
        "初始配置完成，总耗时: {duration}",
    ),
    (
        r"^RESTful API pipe listening at: (?P<address>.+)$",
        "RESTful API 管道监听于: {address}",
    ),
    (
        r"^RESTful API listening at: (?P<address>.+)$",
        "RESTful API 监听于: {address}",
    ),
    (
        r"^DNS server\((?P<protocol>[^)]+)\) listening at: (?P<address>.+)$",
        "DNS 服务器({protocol})监听于: {address}",
    ),
    (
        r"^Mixed\(http\+socks\) proxy listening at: (?P<address>.+)$",
        "Mixed(http+socks) 代理监听于: {address}",
    ),
    (
        r"^Start initial compatible \S+ .+$",
        "正在初始化兼容规则",
    ),
)

RUNTIME_NOTICE_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("Initial configuration complete", "初始配置完成"),
    ("Sniffer is closed", "嗅探器已关闭"),
    ("Nodes restored", "可用"),
    ("Nodes are healthy again.", ""),
    ("Nodes degraded", "不可用"),
    ("Available:", "可用"),
    ("Nodes unavailable", "不可用"),
    ("Fallback available:", "可用"),
    ("Fallback active", "Bastion"),
    ("Available fallback nodes:", "可用"),
    ("No nodes available", "未配置"),
    ("Node unavailable", "不可用"),
    ("Unavailable:", "不可用"),
    ("Protocol changed", "切换"),
    ("No fallback nodes", "未配置"),
    ("No usable nodes in configured sources.", "未配置"),
    ("Fallback changing", "Bastion"),
    ("Fallback path is changing.", ""),
    ("Degradation-triggered backup", "降级触发备份提醒"),
    ("Primary infrastructure is failing. Push critical backups now.", "主基础设施正在失败，请立即推送关键备份。"),
    ("Kill switch unavailable", "熔断保护不可用"),
    ("Kill switch requires Administrator privileges. Run as admin for full protection.", "熔断保护需要管理员权限。请以管理员身份运行以获得完整保护。"),
    ("Started mihomo", "Mihomo 已启动"),
    ("Stopped mihomo", "Mihomo 已停止"),
    ("Hot-reloaded config", "已热重载配置"),
    ("Hot reload failed", "热重载失败"),
    ("Subscription server listening", "订阅服务监听中"),
    ("Desktop runtime started in native app mode", "桌面运行时已以原生应用模式启动"),
    ("Desktop runtime stopped", "桌面运行时已停止"),
)


def _fmt(value: Any, fallback: str = "--") -> str:
    if value is None or value == "":
        return fallback
    return str(value)


def _identity(text: str) -> str:
    return text


def normalize_log_level(value: Any) -> str:
    level = str(value or "INFO").strip().upper()
    if level == "WARN":
        return "WARNING"
    if level in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "FATAL"}:
        return "CRITICAL" if level in {"CRITICAL", "FATAL"} else level
    return "INFO"


def normalize_log_source(value: Any) -> str:
    source = str(value or "UNKNOWN").strip().upper()
    return source if source in KNOWN_LOG_SOURCES and source != "ALL" else "UNKNOWN"


def log_source_label(source: Any, translate: Callable[[str], str] = _identity) -> str:
    return translate(LOG_SOURCE_LABELS.get(normalize_log_source(source), "未知来源"))


def log_source_filter_options(
    *,
    all_label: str,
    translate: Callable[[str], str] = _identity,
) -> list[tuple[str, str]]:
    return [
        (all_label if source == "ALL" else log_source_label(source, translate), source)
        for source in LOG_SOURCES
    ]


def log_level_filter_options(*, all_label: str) -> list[tuple[str, str]]:
    return [(all_label if level == "ALL" else level, level) for level in LOG_LEVELS]


def log_time_now(now: Callable[[], datetime] = datetime.now) -> str:
    return now().strftime("%m-%d %H:%M:%S")


def display_log_time(value: Any, *, now: Callable[[], datetime] = datetime.now) -> str:
    raw = str(value or "").strip()
    if not raw:
        return log_time_now(now)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        return parsed.strftime("%m-%d %H:%M:%S")
    except Exception:
        match = LOG_LINE_PATTERN.match(raw)
        if match:
            date_text = match.group("date") or ""
            display_date = date_text[5:] if len(date_text) >= 10 else now().strftime("%m-%d")
            return f"{display_date} {match.group('time')}"
        return raw[:19]


def alert_log_level(alert_type: Any) -> str:
    normalized = str(alert_type or "").strip().upper()
    if normalized in {"CRITICAL", "EMERGENCY"}:
        return "CRITICAL"
    if normalized == "KILL_SWITCH_ON":
        return "ERROR"
    if normalized in {"WARNING", "BACKUP_REMINDER", "KILL_SWITCH_DISABLED", "KILL_SWITCH_OVERRIDE"}:
        return "WARNING"
    return "INFO"


def alert_record_key(item: dict[str, Any]) -> str:
    return "|".join(str(item.get(part) or "") for part in ("timestamp", "type", "title", "message"))


def alert_type_text(value: Any, translate: Callable[[str], str] = _identity) -> str:
    source = ALERT_TYPE_LABELS.get(str(value))
    return translate(source) if source else _fmt(value)


def alert_log_entry_from_item(
    item: dict[str, Any],
    *,
    translate: Callable[[str], str] = _identity,
    display_time: Callable[[Any], str] = display_log_time,
) -> dict[str, str]:
    alert_type = item.get("type")
    title = runtime_notice_text(item.get("title"), translate)
    message = runtime_notice_text(item.get("message"), translate)
    type_text = alert_type_text(alert_type, translate)
    if title and message:
        text = f"{type_text} - {title}: {message}"
    else:
        text = f"{type_text} - {title or message}"
    text = " ".join(text.split())
    return {
        "time": display_time(item.get("timestamp")),
        "level": alert_log_level(alert_type),
        "source": "ALERT",
        "message": text.strip(" -:"),
    }


def localized_alert_row(row: list[Any], translate: Callable[[str], str] = _identity) -> list[Any]:
    localized = list(row)
    if len(localized) > 1:
        localized[1] = alert_type_text(localized[1], translate)
    if len(localized) > 2:
        localized[2] = runtime_notice_text(localized[2], translate)
    if len(localized) > 3:
        localized[3] = runtime_notice_text(localized[3], translate)
    return localized


def runtime_notice_text(value: Any, translate: Callable[[str], str] = _identity) -> str:
    text = _fmt(value)
    for pattern, template in RUNTIME_NOTICE_PATTERNS:
        match = re.match(pattern, text)
        if match:
            return translate(template).format(**match.groupdict())
    replaced = False
    for source, target in RUNTIME_NOTICE_REPLACEMENTS:
        if source in text:
            text = text.replace(source, translate(target))
            replaced = True
    return strip_log_icons(text if replaced else translate(text))


def strip_log_icons(text: Any) -> str:
    """Remove decorative emoji/icons from user-facing log rows."""
    cleaned: list[str] = []
    for char in str(text):
        if _is_decorative_log_icon(char):
            cleaned.append(" ")
            continue
        cleaned.append(char)
    return re.sub(r"\s{2,}", " ", "".join(cleaned)).strip()


def _is_decorative_log_icon(char: str) -> bool:
    codepoint = ord(char)
    if codepoint in {0xFE0E, 0xFE0F, 0x200D}:
        return True
    if 0x1F000 <= codepoint <= 0x1FAFF:
        return True
    if 0x2600 <= codepoint <= 0x27BF and unicodedata.category(char).startswith("S"):
        return True
    return False


def localized_log_text(text: str, translate: Callable[[str], str] = _identity) -> str:
    return "\n".join(runtime_notice_text(line, translate) for line in str(text).splitlines())


def log_entry_from_text_line(
    line: str,
    *,
    source: str = "MIHOMO",
    translate: Callable[[str], str] = _identity,
    now: Callable[[], datetime] = datetime.now,
) -> dict[str, str] | None:
    text = str(line or "").strip()
    if not text:
        return None
    match = LOG_LINE_PATTERN.match(text)
    if match:
        date_text = match.group("date") or ""
        display_date = date_text[5:] if len(date_text) >= 10 else now().strftime("%m-%d")
        return {
            "time": f"{display_date} {match.group('time')}",
            "level": normalize_log_level(match.group("level")),
            "source": normalize_log_source(source),
            "message": runtime_notice_text(match.group("message"), translate),
        }
    return {
        "time": log_time_now(now),
        "level": "INFO",
        "source": normalize_log_source(source),
        "message": runtime_notice_text(text, translate),
    }


def log_entry_from_stream_record(
    record: dict[str, Any],
    *,
    translate: Callable[[str], str] = _identity,
    now: Callable[[], datetime] = datetime.now,
) -> dict[str, str] | None:
    payload = str(record.get("payload") or "").strip()
    if not payload:
        return None
    timestamp = record.get("timestamp", record.get("time"))
    return {
        "time": display_log_time(timestamp, now=now) if timestamp else log_time_now(now),
        "level": normalize_log_level(record.get("type")),
        "source": normalize_log_source(record.get("source") or "MIHOMO"),
        "message": runtime_notice_text(payload, translate),
    }


def log_entry_visible(
    entry: dict[str, str],
    *,
    selected_level: str = "ALL",
    selected_source: str = "ALL",
    needle: str = "",
    translate: Callable[[str], str] = _identity,
) -> bool:
    entry_source = normalize_log_source(entry.get("source"))
    if str(selected_source or "ALL").upper() != "ALL" and entry_source != str(selected_source).upper():
        return False
    if str(selected_level or "ALL").upper() != "ALL" and normalize_log_level(entry.get("level")) != str(selected_level).upper():
        return False
    normalized_needle = str(needle or "").strip().lower()
    if not normalized_needle:
        return True
    haystack = " ".join(
        [str(entry.get(key, "")) for key in ("time", "level", "source", "message")]
        + [log_source_label(entry_source, translate)]
    ).lower()
    return normalized_needle in haystack


def log_entry_tree_row(
    entry: dict[str, str],
    *,
    translate: Callable[[str], str] = _identity,
) -> tuple[str, str, str]:
    source = normalize_log_source(entry.get("source"))
    message = str(entry.get("message", ""))
    if source != "MIHOMO":
        message = f"[{log_source_label(source, translate)}] {message}"
    return (
        str(entry.get("time", "")),
        normalize_log_level(entry.get("level")),
        message,
    )


def append_log_record_state(
    lines: list[str],
    records: list[dict[str, str]],
    entry: dict[str, Any],
    *,
    max_items: int = 4000,
    keep_items: int = 2000,
) -> tuple[list[str], list[dict[str, str]], dict[str, str], bool]:
    normalized_entry = {
        "time": str(entry.get("time", "")),
        "level": normalize_log_level(entry.get("level")),
        "source": normalize_log_source(entry.get("source")),
        "message": str(entry.get("message", "")),
    }
    line = f"{normalized_entry.get('time', '')} [{normalized_entry['level']}] {normalized_entry.get('message', '')}"
    lines.append(line)
    records.append(normalized_entry)
    trimmed = False
    if len(lines) > max_items:
        lines = lines[-keep_items:]
    if len(records) > max_items:
        records = records[-keep_items:]
        trimmed = True
    return lines, records, normalized_entry, trimmed
