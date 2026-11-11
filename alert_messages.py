"""Localization of outbound alert text for notification channels.

Runtime alerts are authored in English (``manager_alert_policy``) so that
``monitor.log``, ``events.jsonl`` and the delivery history stay greppable.
External channels (Telegram, ntfy, e-mail, SMS, ...) are read by a person,
so the text sent there follows the application language instead.

The mapping goes English -> Chinese source string -> locale catalog, because
the Chinese strings are the translation keys used everywhere else in the UI.
When a locale has no translation for a string the English original is kept;
a user must never receive a half-translated message.
"""
from __future__ import annotations

import re
from typing import Callable, Iterable

from log_events import ALERT_TYPE_LABELS
from ui_i18n import (
    SOURCE_LANGUAGE,
    SUPPORTED_LANGUAGES,
    TRANSLATIONS_BY_LANGUAGE,
    TRANSLATIONS_EN,
)


ENGLISH_LANGUAGE = "en_US"

# Whole-text templates. The first element is a regular expression matched
# against the full English text; named groups are substituted into the
# translated Chinese template.
ALERT_TEXT_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"^Nodes restored$", "节点已恢复"),
    (r"^Nodes are healthy again\.$", "节点已恢复正常。"),
    (r"^Nodes degraded$", "节点部分不可用"),
    (r"^Available: (?P<alive>\d+)/(?P<total>\d+)\.$", "可用：{alive}/{total}。"),
    (r"^Available: (?P<alive>\d+)\.$", "可用：{alive}。"),
    (r"^Nodes unavailable$", "节点不可用"),
    (r"^Node unavailable$", "节点不可用"),
    (r"^Fallback available: (?P<alive>\d+)\.$", "备用可用：{alive}。"),
    (r"^Degradation-triggered backup$", "降级触发备份提醒"),
    (
        r"^Primary infrastructure is failing\. Push critical backups now\.$",
        "主基础设施正在失败，请立即推送关键备份。",
    ),
    (r"^Fallback active$", "已切换到 Bastion 应急线路"),
    (
        r"^Available fallback nodes: (?P<listing>.*?)\. "
        r"Fallback nodes are public\. Do not sign in, enter credentials, "
        r"or access private data\.$",
        "可用应急节点：{listing}。应急节点为公共节点，请勿登录、输入凭据或访问私密数据。",
    ),
    (r"^No nodes available$", "没有可用节点"),
    (
        r"^No available nodes\. Fallback nodes are public\. Do not sign in, "
        r"enter credentials, or access private data\.$",
        "当前没有可用节点。应急节点为公共节点，请勿登录、输入凭据或访问私密数据。",
    ),
    (r"^Unavailable: (?P<labels>.+)\.$", "不可用：{labels}。"),
    (r"^Protocol changed$", "协议已切换"),
    (r"^No fallback nodes$", "没有应急节点"),
    (
        r"^No usable nodes in configured sources\.$",
        "已配置的来源中没有可用节点。",
    ),
    (r"^Node sources unreachable$", "节点来源不可达"),
    (
        r"^Node sources cannot be resolved on this network\. Check DNS settings, "
        r"configure an egress proxy, or import a node\.$",
        "当前网络无法解析节点来源。请检查 DNS 设置、配置出口代理或手动导入节点。",
    ),
    (
        r"^No node source is reachable on this network\. Configure an egress "
        r"proxy or import a node manually\.$",
        "当前网络无法访问任何节点来源。请配置出口代理或手动导入节点。",
    ),
    (
        r"^No route is available for node sources\. Configure an egress proxy "
        r"or import a node\.$",
        "节点来源没有可用路由。请配置出口代理或手动导入节点。",
    ),
    (
        r"^This network requires sign-in first; collection resumes "
        r"automatically afterwards\.$",
        "当前网络需要先登录认证，认证后会自动恢复收集。",
    ),
    (
        r"^Encrypted-only DNS is pinned but unreachable here\. Allow the system "
        r"resolver or configure an egress proxy\.$",
        "已固定仅加密 DNS，但当前网络无法访问。请允许系统解析器或配置出口代理。",
    ),
    (r"^Kill switch unavailable$", "熔断保护不可用"),
    (
        r"^Kill switch requires Administrator privileges\. Run as admin for "
        r"full protection\.$",
        "熔断保护需要管理员权限。请以管理员身份运行以获得完整保护。",
    ),
    (
        r"^Kill switch is Windows-only in this version and is disabled on "
        r"this platform\.$",
        "此版本的熔断保护仅支持 Windows，当前平台已停用。",
    ),
    (r"^Kill switch activated$", "熔断保护已启动"),
    (
        r"^All proxy tiers are down\. Outbound traffic is blocked to protect "
        r"your real IP address and will be restored automatically when any "
        r"tier recovers\.(?:\n\nReason: (?P<reason>.+))?$",
        "所有代理线路均不可用。为保护真实 IP，出站流量已被阻断；任一线路恢复后会自动恢复。",
    ),
    (r"^Kill switch deactivated$", "熔断保护已解除"),
    (
        r"^A proxy tier recovered\. Outbound connectivity has been restored\.$",
        "代理线路已恢复，出站连接已恢复。",
    ),
    (r"^Fallback changing$", "应急线路切换中"),
    (r"^Fallback path is changing\.$", "应急路径正在切换。"),
    (r"^Kill switch manual override enabled$", "熔断保护已手动强制开启"),
    (
        r"^Manual Force On is active\. Automatic recovery will not clear the "
        r"kill switch until Force Off is used\.$",
        "已手动强制开启熔断。在使用“强制关闭”之前，自动恢复不会解除熔断。",
    ),
    (r"^Kill switch manual override cleared$", "熔断保护手动覆盖已清除"),
    (
        r"^Manual Force Off removed kill switch rules\. Automatic protection "
        r"may re-engage immediately if all tiers remain down\.$",
        "已手动强制关闭并移除熔断规则。若所有线路仍不可用，自动保护可能立即重新启用。",
    ),
)

# Fragments that can appear inside a captured group (for example the node
# listing of a Bastion alert) or that repeat within one message.
ALERT_TEXT_FRAGMENTS: tuple[tuple[str, str], ...] = (
    (
        r"(?P<node>[^:;]+?): (?P<failed>.+?) failed; falling back to "
        r"(?P<fallback>[^.]+)\.",
        "{node}：{failed} 失败，已回退到 {fallback}。",
    ),
    (r"No healthy fallback nodes", "没有健康的应急节点"),
    (r"\bReason: (?P<reason>.+)$", "原因：{reason}"),
)

ALERT_FIELD_LABELS: dict[str, str] = {
    "Severity": "级别",
    "Time": "时间",
}

ALERT_SEVERITY_SOURCES: dict[str, str] = {
    **ALERT_TYPE_LABELS,
    "TEST": "测试",
}

_COMPILED_PATTERNS = tuple(
    (re.compile(pattern), template) for pattern, template in ALERT_TEXT_PATTERNS
)
_COMPILED_FRAGMENTS = tuple(
    (re.compile(pattern), template) for pattern, template in ALERT_TEXT_FRAGMENTS
)


def normalize_alert_language(language: object) -> str:
    key = str(language or "").strip()
    if not key:
        return ENGLISH_LANGUAGE
    candidates = (key, key.replace("-", "_"))
    for candidate in candidates:
        if candidate in SUPPORTED_LANGUAGES:
            return candidate
    lowered = key.lower().replace("-", "_")
    for supported in SUPPORTED_LANGUAGES:
        if supported.lower() == lowered or supported.lower().startswith(lowered + "_"):
            return supported
    return ENGLISH_LANGUAGE


def _catalog_translation(language: str, source: str) -> str | None:
    """Return the localized string for a Chinese source, or None when absent."""

    if language == SOURCE_LANGUAGE:
        return source
    catalog = TRANSLATIONS_BY_LANGUAGE.get(language)
    if catalog and source in catalog:
        return str(catalog[source])
    if language == ENGLISH_LANGUAGE and source in TRANSLATIONS_EN:
        return str(TRANSLATIONS_EN[source])
    return None


def _translator(language: str) -> Callable[[str], str | None]:
    return lambda source: _catalog_translation(language, source)


def _localize_fragments(text: str, translate: Callable[[str], str | None]) -> str:
    localized = text
    for pattern, template in _COMPILED_FRAGMENTS:
        translated_template = translate(template)
        if translated_template is None:
            continue

        def _replace(match: re.Match[str], _template: str = translated_template) -> str:
            groups = {key: value for key, value in match.groupdict().items() if value is not None}
            try:
                return _template.format(**groups)
            except (KeyError, IndexError, ValueError):
                return match.group(0)

        localized = pattern.sub(_replace, localized)
    return localized


def localize_alert_text(text: object, language: object) -> str:
    """Localize one alert title or message for the given UI language.

    English input is returned unchanged for ``en_US`` and for any text the
    table does not know. For other languages the Chinese source is looked up
    in the locale catalog; a missing catalog entry keeps the English text.
    """

    original = str(text or "")
    if not original.strip():
        return original
    normalized_language = normalize_alert_language(language)
    if normalized_language == ENGLISH_LANGUAGE:
        return original
    translate = _translator(normalized_language)
    stripped = original.strip()
    for pattern, template in _COMPILED_PATTERNS:
        match = pattern.match(stripped)
        if match is None:
            continue
        translated_template = translate(template)
        if translated_template is None:
            return original
        groups = {
            key: _localize_fragments(value, translate)
            for key, value in match.groupdict().items()
            if value is not None
        }
        try:
            localized = translated_template.format(**groups)
        except (KeyError, IndexError, ValueError):
            return original
        reason = match.groupdict().get("reason") if "reason" in pattern.groupindex else None
        if reason:
            reason_template = translate("原因：{reason}")
            if reason_template is not None:
                localized = f"{localized}\n\n{reason_template.format(reason=reason)}"
        return localized
    localized = _localize_fragments(stripped, translate)
    return localized if localized != stripped else original


def localize_alert_severity(alert_type: object, language: object, fallback: str = "") -> str:
    """Return the severity label for an alert type in the given language."""

    normalized_language = normalize_alert_language(language)
    source = ALERT_SEVERITY_SOURCES.get(str(alert_type or "").upper())
    if source is None or normalized_language == ENGLISH_LANGUAGE:
        return fallback
    localized = _catalog_translation(normalized_language, source)
    return localized if localized is not None else fallback


def localized_alert_field_labels(language: object) -> tuple[str, str]:
    """Return the (severity, time) field labels used in plain-text bodies."""

    normalized_language = normalize_alert_language(language)
    labels: list[str] = []
    for english, source in ALERT_FIELD_LABELS.items():
        if normalized_language == ENGLISH_LANGUAGE:
            labels.append(english)
            continue
        localized = _catalog_translation(normalized_language, source)
        labels.append(localized if localized is not None else english)
    return labels[0], labels[1]


def alert_message_translation_sources() -> Iterable[str]:
    """Every Chinese source string this module can emit (for catalog checks)."""

    yield from (template for _pattern, template in ALERT_TEXT_PATTERNS)
    yield from (template for _pattern, template in ALERT_TEXT_FRAGMENTS)
    yield from ALERT_FIELD_LABELS.values()
    yield "测试"
