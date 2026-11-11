"""Deterministic event protocol for Tesseract's operational log feed.

The raw log message remains the diagnostic source of truth.  This module only
turns registered event keys and explicitly whitelisted facts into concise,
localizable reports.  It intentionally never summarizes arbitrary prose.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence


Translate = Callable[[str], str]

# Keep upstream prose matching explicitly coupled to the core artifact shipped
# by scripts/download_mihomo.ps1.  A version bump must update the fixed corpus
# tests before these patterns can be treated as complete again.
MIHOMO_PATTERN_CATALOG_VERSION = "1.19.27"

_COMMON_CONTEXT_PARAMS = (
    "title",
    "message",
    "source",
    "scope",
    "event_group",
    "alert_type",
    "metadata",
)
_SENSITIVE_NAME_PARTS = (
    "access_token",
    "api_key",
    "authorization",
    "auth_token",
    "bot_token",
    "cookie",
    "password",
    "secret",
    "token",
    "url",
    "webhook",
)
_URL_PATTERN = re.compile(r"\b[a-z][a-z0-9+.-]{1,31}://\S+", re.IGNORECASE)
_WINDOWS_PATH_PATTERN = re.compile(
    r"(?<![\w])(?:[A-Za-z]:[\\/][^\s,;]+|\\\\[^\s\\/]+[\\/][^\s,;]+)"
)
_POSIX_PATH_PATTERN = re.compile(r"(?<![\w])/(?:[^/\s,;]+/)+[^\s,;]+")
# Unbroken machine tokens (hashes, keys, base64).  CJK prose has no spaces,
# so a CJK character ends a run; otherwise a localized sentence would be
# compacted as if it were a secret.
_LONG_TOKEN_PATTERN = re.compile(
    r"(?:(?![\u3000-\u303f\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af\uff00-\uffef])\S){33,}"
)
_TRACEBACK_PATTERN = re.compile(r"\bTraceback \(most recent call last\):.*", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class LogEventSpec:
    """One closed, deterministic event contract."""

    event_key: str
    category: str
    level: str
    is_key_event: bool
    required_params: tuple[str, ...]
    optional_params: tuple[str, ...]
    headline_template: str
    detail_template: str
    persist_raw: bool = False
    # Ordered (param names, template) pairs.  The first variant whose params
    # are all present replaces ``detail_template``; a failure that carries
    # its cause says so instead of pointing the reader at the raw log.
    detail_variants: tuple[tuple[tuple[str, ...], str], ...] = ()
    # Core-output events only: the sanitized core line itself is the cause
    # (``reason``) or, for uncategorized warnings, the whole detail.
    raw_param: str = ""

    @property
    def allowed_params(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.required_params, *self.optional_params)))


@dataclass(frozen=True, slots=True)
class LogEventReport:
    headline: str
    detail: str
    complete: bool
    match_kind: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "headline": self.headline,
            "detail": self.detail,
            "complete": self.complete,
            "match_kind": self.match_kind,
        }


@dataclass(frozen=True, slots=True)
class MihomoEventMatch:
    event_key: str
    params: Mapping[str, Any]
    match_kind: str
    case_id: str = ""


def _spec(
    event_key: str,
    category: str,
    level: str,
    headline: str,
    detail: str,
    *,
    required: Sequence[str] = (),
    optional: Sequence[str] = (),
    is_key_event: bool = True,
    persist_raw: bool = False,
    variants: Sequence[tuple[Sequence[str], str]] = (),
    raw_param: str = "",
) -> LogEventSpec:
    detail_variants = tuple((tuple(names), template) for names, template in variants)
    variant_params = tuple(name for names, _template in detail_variants for name in names)
    optional_params = tuple(
        dict.fromkeys((*optional, *variant_params, *_COMMON_CONTEXT_PARAMS))
    )
    return LogEventSpec(
        event_key=event_key,
        category=category,
        level=level,
        is_key_event=is_key_event,
        persist_raw=persist_raw,
        required_params=tuple(required),
        optional_params=optional_params,
        headline_template=headline,
        detail_template=detail,
        detail_variants=detail_variants,
        raw_param=raw_param,
    )


# A failure that knows its cause shows it; the generic detail remains the
# fallback for callers (or older sidecar records) that carry no reason.
_REASON = ((("reason",), "原因：{reason}"),)
_TIER_COUNTS = ("tier1_alive", "tier1_total", "tier2_alive", "tier3_alive")


# The first sixteen entries mirror every AlertEvent emitted by
# manager_alert_policy.py.  The remaining entries are the closed first-party
# runtime/configuration/update families and supported Mihomo pattern results.
_EVENT_SPEC_ITEMS: tuple[LogEventSpec, ...] = (
    # Alert notifications for mode changes.  The event flow records the same
    # transitions as tier.* events (tier_log_events), which are not subject
    # to alert cooldowns or flap suppression; these stay in the raw view,
    # the alert history and the structured sidecar.
    _spec("mode.recovery.vanguard", "health", "INFO", "节点已恢复", "节点已恢复可用。", is_key_event=False, persist_raw=True),
    _spec(
        "mode.tier1_degraded",
        "health",
        "WARNING",
        "节点部分不可用",
        "可用 {tier1_alive}/{tier1_total}。",
        required=("tier1_alive", "tier1_total"),
        is_key_event=False,
        persist_raw=True,
    ),
    _spec(
        "mode.recovery.sentinel",
        "health",
        "INFO",
        "节点已恢复",
        "可用 {tier2_alive}。",
        required=("tier2_alive",),
        is_key_event=False,
        persist_raw=True,
    ),
    _spec(
        "mode.tier2_active",
        "routing",
        "CRITICAL",
        "备用节点接管",
        "可用 {tier2_alive}。",
        required=("tier2_alive",),
        is_key_event=False,
        persist_raw=True,
    ),
    _spec("mode.tier2_active.backup_reminder", "update", "WARNING", "备份提醒", "请执行备份。", is_key_event=False, persist_raw=True),
    _spec("mode.tier3_active", "routing", "CRITICAL", "备用已启用", "当前使用备用节点。", is_key_event=False, persist_raw=True),
    _spec("mode.offline", "health", "CRITICAL", "所有代理层均不可用", "当前没有可用出口，需要检查节点、订阅或网络环境。", is_key_event=False, persist_raw=True),
    _spec(
        "tier1.node_down",
        "health",
        "WARNING",
        "节点不可用",
        "不可用 {node_count} 个。",
        required=("node_count",),
        optional=("nodes",),
    ),
    _spec(
        "tier1.protocol_fallback",
        "routing",
        "WARNING",
        "协议已切换",
        "已切换 {fallback_count} 个节点。",
        required=("fallback_count",),
        optional=("fallbacks",),
    ),
    _spec("tier3.intervention_required", "health", "CRITICAL", "没有备用节点", "已配置来源没有可用节点。"),
    _spec("kill_switch.disabled", "circuit_breaker", "WARNING", "熔断保护不可用", "系统无法启用完整的断网保护，请检查管理员权限和系统策略。"),
    _spec("kill_switch.activated", "circuit_breaker", "CRITICAL", "熔断保护已启用", "所有代理层不可用，系统已阻止流量直连泄漏。"),
    _spec("kill_switch.deactivated", "circuit_breaker", "INFO", "熔断保护已解除", "代理出口已经恢复，系统重新允许受控出站连接。"),
    _spec("mode.tier2_tier3_flap", "health", "WARNING", "备用状态变化", "备用路径正在切换。", is_key_event=False, persist_raw=True),
    _spec("kill_switch.manual_override", "circuit_breaker", "WARNING", "熔断保护已被手动强制开启", "自动恢复不会解除保护，需执行手动关闭。"),
    _spec("kill_switch.manual_clear", "circuit_breaker", "INFO", "熔断保护的手动覆盖已清除", "若所有代理层仍不可用，自动保护可能立即再次启用。"),
    _spec("alert.legacy", "alert", "WARNING", "收到运行告警", "这是兼容入口产生的结构化告警，请打开原始记录核对完整内容。"),
    # 提醒 redesign 10-30: one record per notice the policy raised
    # (bridge_alerts_desktop, alert_policy), shown or not. title / message are
    # the notice's own plain text in the user's language at that time.
    _spec("alert.notice.error", "alert", "ERROR", "{title}", "{message}", required=("title",)),
    _spec("alert.notice.warn", "alert", "WARNING", "{title}", "{message}", required=("title",)),
    _spec("alert.notice.info", "alert", "INFO", "{title}", "{message}", required=("title",)),
    _spec("alert.notice.resolved", "alert", "INFO", "{title}", "{message}", required=("title",)),
    # Marks where a run begins (the log views separate runs by it); the
    # started/failed events that follow carry the outcome.
    _spec(
        "runtime.start_requested",
        "runtime",
        "INFO",
        "开始运行",
        "Tesseract 收到启动请求。",
        optional=("generation",),
        is_key_event=False,
        persist_raw=True,
    ),
    _spec("runtime.desktop_started", "runtime", "INFO", "Tesseract 运行时已启动", "桌面运行环境已准备完成。"),
    _spec("runtime.desktop_stopped", "runtime", "INFO", "Tesseract 运行时已停止", "本次运行会话已经结束。"),
    _spec(
        "runtime.desktop_failed",
        "runtime",
        "ERROR",
        "Tesseract 运行时启动失败",
        "系统保留原有配置，请打开原始日志查看启动失败证据。",
        variants=((("reason", "endpoints"), "原因：{reason} 被占用的端口：{endpoints}"), *_REASON),
    ),
    _spec("runtime.mihomo_started", "runtime", "INFO", "Mihomo 核心已启动", "代理核心已经开始提供服务。"),
    _spec("runtime.mihomo_ready", "runtime", "INFO", "Mihomo 核心已就绪", "核心初始化完成，用时 {duration}。", required=("duration",)),
    _spec("runtime.mihomo_stopped", "runtime", "INFO", "Mihomo 核心已停止", "代理核心已退出，本次核心会话结束。"),
    _spec("runtime.mihomo_forced_stop", "runtime", "WARNING", "Mihomo 核心已被强制结束", "优雅停止超时，系统已终止核心进程。"),
    _spec("runtime.mihomo_stop_failed", "runtime", "ERROR", "Mihomo 核心未能停止", "终止请求后进程仍未退出，请检查原始日志和系统进程。"),
    _spec("runtime.mihomo_log_reader_failed", "runtime", "ERROR", "Mihomo 日志读取已中断", "核心输出读取器异常停止，运行状态仍需结合健康检查确认。", variants=_REASON, raw_param="reason"),
    _spec("runtime.mihomo_restarted", "runtime", "INFO", "Mihomo 核心已重启", "新的核心进程已经接替上一运行代。"),
    _spec("runtime.mihomo_restart_failed", "runtime", "ERROR", "Mihomo 核心自动恢复失败", "核心未能完成重启，请打开原始日志检查进程和配置状态。", variants=_REASON),
    _spec("runtime.mihomo_failed", "runtime", "ERROR", "Mihomo 核心启动失败", "核心未能开始提供服务，请打开原始日志查看失败证据。", variants=_REASON, raw_param="reason"),
    _spec(
        "runtime.mihomo_optional_dns_listener_failed",
        "runtime",
        "WARNING",
        "Mihomo DNS 监听未启用",
        "当前模式的数据面不依赖该监听器，核心和代理端口仍按实际就绪状态判断。",
        is_key_event=False,
    ),
    _spec("routing.route_switched", "routing", "INFO", "活动出口已切换", "流量现在由 {target} 承载。", required=("target",), optional=("previous",)),
    _spec("routing.route_failed", "routing", "ERROR", "出口切换失败", "系统未能应用目标路由，请检查可用出口和核心状态。", optional=("target",), variants=_REASON),
    _spec("health.layer_degraded", "health", "WARNING", "{layer} 已降级", "{total_count} 个资源中仍有 {available_count} 个可用。", required=("layer", "available_count", "total_count")),
    _spec("health.layer_recovered", "health", "INFO", "{layer} 已恢复", "该层级重新达到可用条件。", required=("layer",)),
    _spec("circuit_breaker.opened", "circuit_breaker", "WARNING", "熔断器已打开", "系统暂时停止使用连续失败的路径。"),
    _spec("circuit_breaker.recovered", "circuit_breaker", "INFO", "熔断器已恢复", "目标路径重新通过健康检查，可以再次使用。"),
    _spec("configuration.applied", "configuration", "INFO", "配置已应用", "新的运行配置已经生效。", optional=("scope_name",)),
    _spec("configuration.failed", "configuration", "ERROR", "配置应用失败", "系统保留原有配置，请打开原始日志查看校验结果。", optional=("scope_name",), variants=_REASON, raw_param="reason"),
    _spec(
        "update.subscription_completed",
        "update",
        "INFO",
        "订阅刷新完成",
        "共处理 {source_total} 个来源，{failed_count} 个失败，获得 {usable_count} 个可用节点。",
        required=("source_total", "failed_count", "usable_count"),
        optional=("sources", "failed_sources", "usable_nodes"),
    ),
    _spec("update.subscription_failed", "update", "ERROR", "订阅刷新失败", "本次没有获得可应用的订阅结果，现有配置保持不变。", optional=("source_total", "failed_count"), variants=_REASON),
    _spec("update.geodata_completed", "update", "INFO", "GeoData 更新完成", "新的规则数据已经可供后续配置使用。"),
    _spec("update.geodata_failed", "update", "ERROR", "GeoData 更新失败", "系统继续使用现有规则数据，请检查原始日志中的下载或校验结果。", variants=_REASON),
    _spec(
        "update.sources_completed",
        "update",
        "INFO",
        "节点来源刷新完成",
        # usable_count here is parsed candidates, not validated nodes; calling
        # them "usable" read as a pool size next to "Bastion 可用 N".
        "共处理 {source_total} 个来源，{failed_count} 个失败，得到 {usable_count} 个候选节点。",
        required=("source_total", "failed_count", "usable_count"),
        optional=("sources", "failed_sources", "usable_nodes"),
    ),
    _spec(
        "update.sources_failed",
        "update",
        "ERROR",
        "节点来源刷新失败",
        "所有已启用来源均未返回可用结果，现有节点不会被空结果覆盖。",
        optional=("source_total", "failed_count", "failure_kinds"),
        variants=((("failure_summary",), "{source_total} 个来源全部失败：{failure_summary}。现有节点不会被空结果覆盖。"),),
    ),
    _spec(
        "dns.resolver_qualified",
        "runtime",
        "INFO",
        "DNS 解析器已就绪",
        "本次运行使用 {summary} 解析域名（原因：{reason}）。",
        required=("summary", "reason"),
        optional=("rung", "revision", "wall_time_ms", "fingerprint"),
    ),
    _spec(
        "dns.resolver_degraded",
        "runtime",
        "WARNING",
        "DNS 解析器已降级",
        "当前网络无法使用上一档解析器，已切换为 {summary}（原因：{reason}）。",
        required=("summary", "reason"),
        optional=("rung", "revision", "previous_rung"),
    ),
    _spec(
        "tier3.source_supply_no_egress",
        "alert",
        "ERROR",
        "节点来源无可用出口",
        "当前网络无法获取任何节点来源（{detail}），自动收集已暂停并将稍后重试。",
        required=("detail",),
        optional=("rung", "retry_after_seconds"),
    ),
    _spec(
        "tier3.source_supply_restored",
        "update",
        "INFO",
        "节点来源已恢复",
        "此前判断当前网络没有可用出口，现在已成功获取节点来源，自动收集已恢复。",
        optional=("detail", "parked_seconds"),
    ),
    # Tier state transitions (tier_log_events.TierLogTracker).  One event per
    # change of the carrying tier, pool milestone, or verified egress; the
    # alert-driven ``mode.*`` notifications above stay out of the event flow.
    _spec(
        "tier.vanguard_healthy",
        "health",
        "INFO",
        "Vanguard 全部可用",
        "可用 {tier1_alive}/{tier1_total}。",
        optional=_TIER_COUNTS,
        variants=((("previous",), "可用 {tier1_alive}/{tier1_total}，流量已从 {previous} 切回 Vanguard。"),),
    ),
    _spec(
        "tier.vanguard_degraded",
        "health",
        "WARNING",
        "Vanguard 部分节点不可用",
        "可用 {tier1_alive}/{tier1_total}，流量仍由 Vanguard 承载。",
        optional=_TIER_COUNTS,
        variants=((("previous",), "可用 {tier1_alive}/{tier1_total}，流量已从 {previous} 切回 Vanguard。"),),
    ),
    _spec(
        "tier.sentinel_active",
        "routing",
        "WARNING",
        "流量由 Sentinel 承载",
        "Vanguard 没有可用节点，Sentinel 可用 {tier2_alive}。",
        optional=_TIER_COUNTS,
        variants=((("previous",), "Sentinel 可用 {tier2_alive}，流量已从 {previous} 切换过来。"),),
    ),
    _spec(
        "tier.bastion_active",
        "routing",
        "WARNING",
        "流量由 Bastion 承载",
        "Bastion 可用 {tier3_alive}。Bastion 使用公共节点，请避免登录或提交敏感信息。",
        optional=_TIER_COUNTS,
        variants=(
            (
                ("previous",),
                "Bastion 可用 {tier3_alive}，流量已从 {previous} 切换过来。Bastion 使用公共节点，请避免登录或提交敏感信息。",
            ),
        ),
    ),
    _spec(
        "tier.offline",
        "health",
        "CRITICAL",
        "所有层级均不可用",
        "Vanguard、Sentinel 与 Bastion 都没有可用节点，当前没有代理出口。",
        optional=_TIER_COUNTS,
        variants=((("previous",), "此前由 {previous} 承载；现在 Vanguard、Sentinel 与 Bastion 都没有可用节点。"),),
    ),
    _spec(
        "tier3.pool_ready",
        "health",
        "INFO",
        "Bastion 已有可用节点",
        "可用 {alive_count}。",
        required=("alive_count",),
        variants=((("target_count",), "可用 {alive_count}，目标 {target_count}。"),),
    ),
    _spec(
        "tier3.pool_target_reached",
        "health",
        "INFO",
        "Bastion 已达到目标数量",
        "可用 {alive_count}/{target_count}。",
        required=("alive_count", "target_count"),
    ),
    _spec(
        "tier3.pool_empty",
        "health",
        "WARNING",
        "Bastion 已没有可用节点",
        "此前可用 {previous_count}；Bastion 会继续收集并验证新节点。",
        required=("previous_count",),
    ),
    _spec(
        "tier.egress_ready",
        "routing",
        "INFO",
        "代理出口已可用",
        "出口校验通过。",
        variants=((("exit_name",), "出口校验通过，流量经 {exit_name} 转发。"),),
    ),
    _spec("tier.egress_failed", "routing", "ERROR", "代理出口不可用", "出口校验未通过。", variants=_REASON),
    _spec(
        "tier.egress_restored",
        "routing",
        "INFO",
        "代理出口已恢复",
        "出口校验重新通过。",
        variants=((("exit_name",), "出口校验重新通过，流量经 {exit_name} 转发。"),),
    ),
    _spec("operation.action_required", "alert", "WARNING", "需要人工处理", "自动恢复已停止，请按运行告警中的指引完成处理。", optional=("action",)),
    _spec("mihomo.configuration_failed", "configuration", "ERROR", "Mihomo 配置校验失败", "核心拒绝了当前配置，请打开原始日志查看具体字段。", variants=_REASON, raw_param="reason"),
    _spec("mihomo.connection_timeout", "connection", "INFO", "连接超时", "单次连接未完成；目标和失败原因保留在原始日志中。", is_key_event=False, persist_raw=True),
    _spec("mihomo.dns_resolve_failed", "connection", "INFO", "DNS 解析失败", "核心无法解析目标域名；若在收集节点期间大量出现，通常说明当前网络阻止了所配置的 DNS 解析器，请在网络设置中改用可用的解析器。", is_key_event=False, persist_raw=True),
    _spec("mihomo.connection_failed", "connection", "INFO", "连接失败", "单次连接未完成；目标和失败原因保留在原始日志中。", is_key_event=False, persist_raw=True),
    # Unregistered core vocabulary: the headline stays honest about the
    # source and severity; the detail is the sanitized core line itself.
    _spec("mihomo.uncategorized_warning", "runtime", "WARNING", "Mihomo 警告", "当前版本未匹配到已登记模式，请打开原始日志核对证据。", variants=((("core_message",), "{core_message}"),), raw_param="core_message"),
    _spec("mihomo.uncategorized_error", "runtime", "ERROR", "Mihomo 错误", "当前版本未匹配到已登记模式，请打开原始日志核对证据。", variants=((("core_message",), "{core_message}"),), raw_param="core_message"),
    _spec("mihomo.uncategorized_critical", "runtime", "CRITICAL", "Mihomo 严重错误", "当前版本未匹配到已登记模式，请打开原始日志核对证据。", variants=((("core_message",), "{core_message}"),), raw_param="core_message"),
)

_event_keys = tuple(item.event_key for item in _EVENT_SPEC_ITEMS)
if len(_event_keys) != len(set(_event_keys)):  # pragma: no cover - import-time invariant
    raise RuntimeError("duplicate LogEventSpec event_key")

LOG_EVENT_SPECS: Mapping[str, LogEventSpec] = MappingProxyType(
    {item.event_key: item for item in _EVENT_SPEC_ITEMS}
)


def event_spec(event_key: Any) -> LogEventSpec | None:
    return LOG_EVENT_SPECS.get(str(event_key or "").strip())


def is_registered_event_key(event_key: Any) -> bool:
    return event_spec(event_key) is not None


def _is_sensitive_name(name: Any) -> bool:
    lowered = str(name or "").strip().lower()
    return any(part in lowered for part in _SENSITIVE_NAME_PARTS)


def _compact_long_token(match: re.Match[str]) -> str:
    token = match.group(0)
    return f"{token[:20]}…{token[-8:]}"


def _clean_text(value: Any, *, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    text = _TRACEBACK_PATTERN.sub("[堆栈已隐藏]", text)
    text = _URL_PATTERN.sub("[链接已隐藏]", text)
    text = _WINDOWS_PATH_PATTERN.sub("[路径已隐藏]", text)
    text = _POSIX_PATH_PATTERN.sub("[路径已隐藏]", text)
    text = _LONG_TOKEN_PATTERN.sub(_compact_long_token, text)
    if len(text) > limit:
        return text[: max(0, limit - 1)].rstrip() + "…"
    return text


def _safe_json_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    if _is_sensitive_name(key):
        return "[REDACTED]"
    if depth >= 5:
        return "[省略]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, Mapping):
        return {
            str(child_key): _safe_json_value(child_value, key=str(child_key), depth=depth + 1)
            for child_key, child_value in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        materialized = list(value)
        return [
            _safe_json_value(item, key=key, depth=depth + 1)
            for item in materialized[:50]
        ]
    return _clean_text(value)


def safe_event_metadata(value: Any) -> Mapping[str, Any]:
    """Return a JSON-safe, redacted copy suitable for capture and sidecars."""

    if not isinstance(value, Mapping):
        return MappingProxyType({})
    cleaned = _safe_json_value(value)
    return MappingProxyType(dict(cleaned) if isinstance(cleaned, Mapping) else {})


def _flatten_metadata(params: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    nested = params.get("metadata")
    if isinstance(nested, Mapping):
        result.update({str(key): value for key, value in nested.items()})
    result.update({str(key): value for key, value in params.items()})
    return result


def _derive_counts(params: dict[str, Any]) -> None:
    count_sources = (
        ("node_count", "nodes"),
        ("fallback_count", "fallbacks"),
        ("source_total", "sources"),
        ("failed_count", "failed_sources"),
        ("usable_count", "usable_nodes"),
    )
    for count_key, values_key in count_sources:
        if params.get(count_key) not in (None, ""):
            continue
        values = params.get(values_key)
        if isinstance(values, (list, tuple, set, frozenset, Mapping)):
            params[count_key] = len(values)


def sanitize_event_params(event_key: Any, params: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Whitelist and sanitize facts according to the event's registered schema."""

    spec = event_spec(event_key)
    prepared = _flatten_metadata(dict(params or {}))
    _derive_counts(prepared)
    cleaned: dict[str, Any] = {}
    # Unregistered explicit events may still travel through the compatibility
    # alert API.  They retain only the globally declared alert context; no
    # event-specific free-form parameter is admitted.
    allowed_params = spec.allowed_params if spec is not None else _COMMON_CONTEXT_PARAMS
    for key in allowed_params:
        if key not in prepared:
            continue
        cleaned[key] = _safe_json_value(prepared[key], key=key)
    return MappingProxyType(cleaned)


class _SafeFormatValues(dict[str, Any]):
    def __missing__(self, _key: str) -> str:
        return "—"


def _translated_template(template: str, translate: Translate | None) -> str:
    if translate is None:
        return template
    try:
        return str(translate(template))
    except Exception:
        return template


def _record_value(record: Any, key: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(key, default)
    return getattr(record, key, default)


def _record_metadata(record: Any) -> Mapping[str, Any]:
    value = _record_value(record, "metadata", {})
    return value if isinstance(value, Mapping) else {}


# Source-refresh failure classes (collector._source_failure_kind).  Labels are
# localized at render time so stored sidecar facts stay language-neutral.
_SOURCE_FAILURE_KIND_LABELS: Mapping[str, str] = MappingProxyType(
    {
        "timeout": "超时",
        "dns": "DNS 解析失败",
        "tls": "TLS 握手失败",
        "refused": "连接被拒绝",
        "connection": "无法连接",
        "blocked_no_route": "没有可用路径",
        "unsupported_content": "内容无法解析",
        "empty": "内容为空",
        "other": "其他错误",
    }
)
# Params whose value may itself be a catalog sentence (e.g. a localized
# startup-blocked message) and is therefore offered to the translator.
_TRANSLATED_PARAMS = ("reason",)
# Events classified from the text of a core/runner line.  Only for these is
# the matched line itself admissible as the cause.
_PATTERN_MATCH_KINDS = frozenset(
    {"core_runner_pattern", "mihomo_pattern", "mihomo_safe_fallback"}
)


def _present(value: Any) -> bool:
    return value not in (None, "", (), [], {})


_EXCEPTION_LINE = re.compile(r"^[A-Za-z_][\w.]*:\s*(?P<message>\S.*)$")


def _traceback_exception_message(message_raw: Any) -> str:
    """The final ``Type: message`` line of a logged traceback, if any.

    That line is ``str(exc)`` -- exactly what newer callers pass as
    ``reason`` -- so records written before they did still show a cause.
    """

    lines = [line.strip() for line in str(message_raw or "").splitlines()]
    if "Traceback (most recent call last):" not in lines:
        return ""
    for line in reversed(lines):
        if not line:
            continue
        match = _EXCEPTION_LINE.match(line)
        return match.group("message").strip() if match else ""
    return ""


def _failure_kind_summary(kinds: Mapping[str, Any], translate: Translate | None) -> str:
    counted: list[tuple[int, str]] = []
    for kind, count in kinds.items():
        try:
            number = int(count)
        except (TypeError, ValueError):
            continue
        if number > 0:
            counted.append((number, str(kind)))
    counted.sort(key=lambda item: (-item[0], item[1]))
    parts: list[str] = []
    for number, kind in counted[:4]:
        status = re.fullmatch(r"http_(\d{3})", kind)
        if status is not None:
            label = f"HTTP {status.group(1)}"
        else:
            label = _translated_template(
                _SOURCE_FAILURE_KIND_LABELS.get(kind, _SOURCE_FAILURE_KIND_LABELS["other"]),
                translate,
            )
        parts.append(f"{label} ×{number}")
    return ", ".join(parts)


def _render_params(params: Mapping[str, Any], translate: Translate | None) -> dict[str, Any]:
    rendered = dict(params)
    kinds = rendered.get("failure_kinds")
    if isinstance(kinds, Mapping) and not _present(rendered.get("failure_summary")):
        summary = _failure_kind_summary(kinds, translate)
        if summary:
            rendered["failure_summary"] = summary
    for name in _TRANSLATED_PARAMS:
        value = rendered.get(name)
        if isinstance(value, str) and value:
            rendered[name] = _translated_template(value, translate)
    return rendered


def _detail_template_for(spec: LogEventSpec, params: Mapping[str, Any]) -> str:
    for names, template in spec.detail_variants:
        if all(_present(params.get(name)) for name in names):
            return template
    return spec.detail_template


def _format_report(
    spec: LogEventSpec,
    params: Mapping[str, Any],
    *,
    translate: Translate | None,
    match_kind: str,
) -> LogEventReport:
    complete = all(_present(params.get(name)) for name in spec.required_params)
    rendered = _render_params(params, translate)
    values = _SafeFormatValues(rendered)
    headline = _translated_template(spec.headline_template, translate).format_map(values)
    detail = _translated_template(
        _detail_template_for(spec, rendered),
        translate,
    ).format_map(values)
    return LogEventReport(
        headline=" ".join(headline.split()),
        detail=" ".join(detail.split()),
        complete=complete,
        match_kind=match_kind,
    )


_CORE_RUNNER_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "runner_started",
        re.compile(r"^Started mihomo:\s*.+$", re.IGNORECASE),
        "runtime.mihomo_started",
    ),
    (
        "runner_stopped",
        re.compile(r"^Stopped mihomo$", re.IGNORECASE),
        "runtime.mihomo_stopped",
    ),
    (
        "runner_forced_stop",
        re.compile(r"^Killed mihomo after graceful stop timed out$", re.IGNORECASE),
        "runtime.mihomo_forced_stop",
    ),
    (
        "runner_hot_reload_ok",
        re.compile(r"^Hot-reloaded config via\s+.+$", re.IGNORECASE),
        "configuration.applied",
    ),
    (
        "runner_hot_reload_failed",
        re.compile(r"^Hot reload failed:\s*.+$", re.IGNORECASE),
        "configuration.failed",
    ),
)

_MIHOMO_CONNECTION_PREFIX = (
    r"^\[(?:TCP|UDP)\]\s+dial\s+.+?\s+\S+\s+-->\s+\S+\s+error:\s+"
)
_MIHOMO_CONNECTION_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        # A resolver fault reads as an ordinary timeout unless it is matched
        # first, which hides the one failure that stalls every DIRECT route at
        # once (source supply included) instead of a single connection.
        "mihomo_dns_resolve_failed",
        re.compile(
            _MIHOMO_CONNECTION_PREFIX + r".*\bdns resolve failed\b.*$",
            re.IGNORECASE,
        ),
        "mihomo.dns_resolve_failed",
    ),
    (
        "mihomo_connection_timeout",
        re.compile(
            _MIHOMO_CONNECTION_PREFIX + r".*\b(?:i/o timeout|context deadline exceeded|connection timed out)\b.*$",
            re.IGNORECASE,
        ),
        "mihomo.connection_timeout",
    ),
    (
        "mihomo_connection_failed",
        re.compile(_MIHOMO_CONNECTION_PREFIX + r".+$", re.IGNORECASE),
        "mihomo.connection_failed",
    ),
)

_MIHOMO_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "mihomo_stop_failed",
        re.compile(r"^mihomo did not exit after kill request$", re.IGNORECASE),
        "runtime.mihomo_stop_failed",
    ),
    (
        "mihomo_reader_failed",
        re.compile(r"^mihomo log reader stopped:\s*.+$", re.IGNORECASE),
        "runtime.mihomo_log_reader_failed",
    ),
    (
        "mihomo_ready",
        re.compile(r"^Initial configuration complete, total time:\s*(?P<duration>.+)$", re.IGNORECASE),
        "runtime.mihomo_ready",
    ),
    (
        "mihomo_optional_dns_listener_failed",
        re.compile(
            r'^.*\bStart DNS server(?:\((?:TCP|UDP)\))? error:\s*'
            r'listen\s+(?:udp|tcp)\s+'
            r'\S+:\s*bind:\s*.+$',
            re.IGNORECASE,
        ),
        "runtime.mihomo_optional_dns_listener_failed",
    ),
    (
        "mihomo_config_failed",
        re.compile(r"^(?:.*\b(?:configuration|config).*(?:invalid|failed|error)\b.*)$", re.IGNORECASE),
        "mihomo.configuration_failed",
    ),
    (
        "mihomo_start_failed",
        re.compile(r"^(?:.*\b(?:start|listen|bind).*(?:failed|error|address already in use)\b.*)$", re.IGNORECASE),
        "runtime.mihomo_failed",
    ),
    (
        "mihomo_stopped",
        re.compile(r"^(?:.*\b(?:mihomo|core).*(?:stopped|shutdown|exited)\b.*)$", re.IGNORECASE),
        "runtime.mihomo_stopped",
    ),
)

MIHOMO_PATTERN_CASE_IDS = frozenset(
    case_id for case_id, _pattern, _event_key in (
        *_CORE_RUNNER_PATTERNS, *_MIHOMO_CONNECTION_PATTERNS, *_MIHOMO_PATTERNS,
    )
)


def _match_patterns(
    message: str,
    patterns: Sequence[tuple[str, re.Pattern[str], str]],
    *,
    match_kind: str,
) -> MihomoEventMatch | None:
    for case_id, pattern, event_key in patterns:
        match = pattern.fullmatch(message)
        if match:
            params = sanitize_event_params(event_key, match.groupdict())
            return MihomoEventMatch(event_key, params, match_kind, case_id)
    return None


def match_core_runner_event(message_raw: Any) -> MihomoEventMatch | None:
    """Match only the five exact lifecycle messages owned by core_runner."""

    message = " ".join(str(message_raw or "").split())
    return _match_patterns(message, _CORE_RUNNER_PATTERNS, match_kind="core_runner_pattern")


def match_mihomo_event(message_raw: Any, level: Any = "INFO") -> MihomoEventMatch | None:
    """Match only the supported Mihomo vocabulary; never infer free text."""

    message = " ".join(str(message_raw or "").split())
    normalized_level = str(level or "INFO").strip().upper()
    # Individual connection failures are diagnostics, not proof that the
    # service is unavailable. Never demote upstream ERROR/CRITICAL evidence.
    if normalized_level in {"DEBUG", "INFO", "WARN", "WARNING"}:
        connection = _match_patterns(
            message, _MIHOMO_CONNECTION_PATTERNS, match_kind="mihomo_pattern",
        )
        if connection is not None:
            return connection
    exact = _match_patterns(message, _CORE_RUNNER_PATTERNS, match_kind="mihomo_pattern")
    if exact is not None:
        return exact
    matched = _match_patterns(message, _MIHOMO_PATTERNS, match_kind="mihomo_pattern")
    if matched is not None:
        return matched
    normalized_level = str(level or "INFO").strip().upper()
    fallback_keys = {
        "WARN": "mihomo.uncategorized_warning",
        "WARNING": "mihomo.uncategorized_warning",
        "ERROR": "mihomo.uncategorized_error",
        "CRITICAL": "mihomo.uncategorized_critical",
        "FATAL": "mihomo.uncategorized_critical",
    }
    fallback_key = fallback_keys.get(normalized_level)
    if fallback_key:
        return MihomoEventMatch(
            fallback_key,
            MappingProxyType({}),
            "mihomo_safe_fallback",
        )
    return None


def reclassify_mihomo_fallback(
    event_key: Any, message_raw: Any, level: Any, metadata: Mapping[str, Any],
) -> MihomoEventMatch | None:
    """Refresh old catalog gaps without rewriting their original evidence."""

    if str(event_key or "") not in {
        "mihomo.uncategorized_warning", "mihomo.uncategorized_error",
        "mihomo.uncategorized_critical",
    }:
        return None
    observed_level = str(metadata.get("observed_level") or level).strip().upper()
    preserved_key = {
        "ERROR": "mihomo.uncategorized_error",
        "CRITICAL": "mihomo.uncategorized_critical",
        "FATAL": "mihomo.uncategorized_critical",
    }.get(observed_level)
    if preserved_key:
        return (
            MihomoEventMatch(preserved_key, MappingProxyType({}), "mihomo_safe_fallback")
            if preserved_key != event_key else None
        )
    matched = match_mihomo_event(message_raw, observed_level)
    if matched is not None and matched.match_kind == "mihomo_pattern":
        return matched
    return None


def build_log_event_report(
    record: Any,
    translate: Translate | None = None,
) -> LogEventReport:
    """Build a concise report from registered facts, never from an excerpt.

    Two pieces of text are admitted, both sanitized and both labelled as a
    cause rather than presented as a summary: a pattern-matched core line
    (``LogEventSpec.raw_param``), and for failure events without a
    ``reason`` the exception line of a logged traceback (``str(exc)``).
    """

    metadata = _record_metadata(record)
    event_key = str(_record_value(record, "event_key", "") or "").strip()
    raw_params = metadata.get("event_params", _record_value(record, "event_params", {}))
    params = raw_params if isinstance(raw_params, Mapping) else {}
    match_kind = str(metadata.get("event_match_kind") or "event_key")
    spec = event_spec(event_key)

    if spec is None and str(_record_value(record, "source", "")).strip().upper() == "MIHOMO":
        match = match_mihomo_event(
            _record_value(record, "message_raw", _record_value(record, "message", "")),
            _record_value(record, "level", "INFO"),
        )
        if match is not None:
            event_key = match.event_key
            params = match.params
            match_kind = match.match_kind
            spec = event_spec(event_key)

    if spec is None:
        return LogEventReport(
            headline=_translated_template("未登记日志事件", translate),
            detail=_translated_template("该记录没有可用的结构化简报，请打开原始日志核对证据。", translate),
            complete=False,
            match_kind="unregistered",
        )
    sanitized = sanitize_event_params(event_key, params)
    if (
        spec.raw_param
        and match_kind in _PATTERN_MATCH_KINDS
        and not _present(sanitized.get(spec.raw_param))
    ):
        # The core line that matched *is* the evidence; show it sanitized
        # (URLs, paths and long tokens hidden) instead of "open the raw log".
        core_line = _clean_text(
            _record_value(record, "message_raw", _record_value(record, "message", ""))
        )
        if core_line:
            sanitized = MappingProxyType({**sanitized, spec.raw_param: core_line})
    if not _present(sanitized.get("reason")) and any(
        "reason" in names for names, _template in spec.detail_variants
    ):
        exception_message = _clean_text(
            _traceback_exception_message(
                _record_value(record, "message_raw", _record_value(record, "message", ""))
            )
        )
        if exception_message:
            sanitized = MappingProxyType({**sanitized, "reason": exception_message})
    return _format_report(spec, sanitized, translate=translate, match_kind=match_kind)


def _logging_level(value: int | str) -> int:
    if isinstance(value, int):
        return value
    normalized = str(value or "INFO").strip().upper()
    if normalized == "WARN":
        normalized = "WARNING"
    if normalized == "FATAL":
        normalized = "CRITICAL"
    return int(getattr(logging, normalized, logging.INFO))


def log_event(
    logger: logging.Logger,
    level: int | str,
    event_key: str,
    message_raw: Any,
    *,
    category: str | None = None,
    params: Mapping[str, Any] | None = None,
    is_key_event: bool | None = None,
    exc_info: Any = None,
) -> None:
    """Emit one raw log line with its deterministic event facts attached."""

    spec = event_spec(event_key)
    sanitized = sanitize_event_params(event_key, params)
    # A registered event has one catalog-owned severity, category, and key
    # classification.  Compatibility arguments apply only to unknown keys.
    resolved_category = str(spec.category if spec is not None else (category or "general"))
    resolved_key_event = bool(spec.is_key_event) if spec is not None else bool(is_key_event)
    resolved_level = spec.level if spec is not None else level
    param_values = dict(sanitized)
    logger.log(
        _logging_level(resolved_level),
        str(message_raw or ""),
        exc_info=exc_info,
        extra={
            "event_key": str(event_key or "").strip(),
            "category": resolved_category,
            "event_params": param_values,
            "is_key_event": resolved_key_event,
            "event_match_kind": "event_key",
            "event_group": str(param_values.get("event_group") or ""),
            "alert_type": str(param_values.get("alert_type") or ""),
            "log_source": "ALERT" if param_values.get("alert_type") else "TESSERACT",
        },
    )


def log_raw_diagnostic(
    logger: logging.Logger,
    level: int | str,
    message: Any,
    *args: Any,
    **kwargs: Any,
) -> None:
    """Emit an explicitly non-event diagnostic record.

    Call sites use this helper when a warning or error is useful as raw
    troubleshooting evidence but must not be promoted into the operational
    event feed.  ``exception`` mirrors :meth:`logging.Logger.exception` by
    logging at ERROR and including the active exception unless the caller
    explicitly supplies ``exc_info``.
    """

    normalized_level = str(level or "INFO").strip().lower() if not isinstance(level, int) else ""
    if normalized_level == "warn":
        normalized_level = "warning"
    elif normalized_level == "fatal":
        normalized_level = "critical"
    if normalized_level == "exception":
        kwargs.setdefault("exc_info", True)
    supplied_extra = kwargs.pop("extra", None)
    extra = dict(supplied_extra) if isinstance(supplied_extra, Mapping) else {}
    extra.update(
        {
            "event_key": "",
            "event_match_kind": "raw_only",
            "is_key_event": False,
        }
    )
    if normalized_level in {"debug", "info", "warning", "error", "critical", "exception"}:
        getattr(logger, normalized_level)(
            str(message or ""),
            *args,
            extra=extra,
            **kwargs,
        )
        return
    logger.log(_logging_level(level), str(message or ""), *args, extra=extra, **kwargs)


__all__ = [
    "LOG_EVENT_SPECS",
    "MIHOMO_PATTERN_CATALOG_VERSION",
    "MIHOMO_PATTERN_CASE_IDS",
    "LogEventReport",
    "LogEventSpec",
    "MihomoEventMatch",
    "build_log_event_report",
    "event_spec",
    "is_registered_event_key",
    "log_event",
    "log_raw_diagnostic",
    "match_core_runner_event",
    "match_mihomo_event",
    "safe_event_metadata",
    "sanitize_event_params",
]
