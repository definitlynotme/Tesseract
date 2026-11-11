"""Small, request-owned presentation policy for the overview exit lookup."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass


OVERVIEW_PROGRESS_TEXT = {
    "waiting_connection": "正在等待连接...",
    "validating_route": "正在验证出口...",
    "querying_identity": "正在获取...",
    "retrying": "正在重试...",
}

OVERVIEW_FAILURE_TEXT = {
    "validation_timeout": "查询超时，请重试",
    "identity_timeout": "查询超时，请重试",
    "timeout": "查询超时，请重试",
    "waiting_connection": "连接尚未就绪，请稍后刷新",
    "connection_not_ready": "连接尚未就绪，请稍后刷新",
    "probe_busy": "后台检测繁忙，请稍后刷新",
    "validation_cancelled": "本次查询已取消",
    "probe_unavailable": "校验暂不可用，请稍后刷新",
    "partial_validation": "部分校验站点未响应，请稍后刷新",
    "probe_target_error": "检测站点响应异常，网络可能受到拦截",
    "exit_unreachable": "当前出口无法连通，请稍后刷新",
    "system_proxy_capture_inactive": "系统代理未指向本应用，重启服务可恢复",
    "system_proxy_host_path_unverified": "系统代理路径未确认，请稍后刷新",
    "route_changed": "出口已变化，请重新查看",
    "identity_rate_limited": "查询来源限流，请稍后刷新",
    "identity_conflict": "来源冲突",
    "identity_rotating": "地址轮换",
    "identity_unavailable": "查询来源暂时不可用，请稍后刷新",
}

# The exit carried the identity lookup, so its address stays visible; the
# qualifier explains which secondary proof did not pass.
OVERVIEW_WARNING_TEXT = {
    "partial_validation": "部分检测站点未响应",
    "system_proxy_capture_inactive": "系统代理未指向本应用，浏览器流量未走此出口",
    "system_proxy_host_path_unverified": "系统代理路径未确认",
}

OVERVIEW_SERVICE_TEXT = {
    "offline": "服务未启动，启动后可查看",
    "starting": "服务启动中，就绪后可查看",
    "deferred": "连接建立中，就绪后自动查看",
}

OVERVIEW_VERIFIED_TEXT = "已验证"
# An automatic lookup reads identity through the READY route; it never runs
# the click's full verification, so it is never labelled as verified.
OVERVIEW_AUTO_TEXT = "自动获取"
OVERVIEW_AUTO_TOOLTIP = "自动获取：出口由运行时就绪检测确认；点击刷新可完整验证。"
OVERVIEW_SOURCE_REPORTED_TOOLTIP = "来源报告，仅供参考"
OVERVIEW_AUTO_SETTING_TEXT = "自动查询出口 IP"
OVERVIEW_AUTO_SETTING_TOOLTIP = (
    "概览页可见时，在连接就绪或出口变化后自动查询出口 IP；"
    "每小时最多 12 次，经当前出口发送，不会暴露本机真实 IP。"
)

OVERVIEW_VALIDATION_TEXTS = frozenset({
    *OVERVIEW_PROGRESS_TEXT.values(), *OVERVIEW_FAILURE_TEXT.values(),
    *OVERVIEW_WARNING_TEXT.values(), *OVERVIEW_SERVICE_TEXT.values(),
    OVERVIEW_VERIFIED_TEXT, OVERVIEW_AUTO_TEXT, OVERVIEW_AUTO_TOOLTIP,
    OVERVIEW_SOURCE_REPORTED_TOOLTIP, OVERVIEW_AUTO_SETTING_TEXT,
    OVERVIEW_AUTO_SETTING_TOOLTIP,
    "单一来源", "结果已过期，请刷新", "出口验证未通过，请稍后刷新",
})

# Conditions the user cannot fix by retrying harder: shown as a caution, not
# as a failed lookup.
_SOFT_FAILURE_REASONS = frozenset({
    "validation_cancelled", "route_changed", "waiting_connection",
    "connection_not_ready",
})
_TIMEOUT_REASONS = frozenset({"validation_timeout", "identity_timeout"})
# Admission reasons that mean the connection is still being built rather than
# that it failed.
_CONNECTION_PENDING_REASONS = frozenset({
    "runtime_route_not_ready", "core_not_running",
    "initial_egress_admission_pending",
})


def _mapping(value: object) -> Mapping:
    return value if isinstance(value, Mapping) else {}


@dataclass(frozen=True)
class OverviewLookupDisplay:
    state: str
    message: str = ""
    # muted | ok | warning | error: text emphasis only, never the lookup state.
    tone: str = "muted"


def overview_lookup_display(result: Mapping | None) -> OverviewLookupDisplay:
    """Use semantic outcomes before a generic error; never expose raw errors.

    A failed automatic lookup is reported quietly: the user did not ask for it.
    """
    display = _lookup_display(_mapping(result))
    trigger = str(_mapping(_mapping(result).get("diagnostics")).get("overview_trigger") or "")
    if trigger == "auto" and display.tone == "error":
        return OverviewLookupDisplay(display.state, display.message, "muted")
    return display


def overview_result_trigger(result: Mapping | None) -> str:
    return str(_mapping(_mapping(result).get("diagnostics")).get("overview_trigger") or "manual")


def _lookup_display(result: Mapping) -> OverviewLookupDisplay:
    if not result:
        return OverviewLookupDisplay("idle", "点击查看")
    state = str(result.get("state") or "").lower()
    diagnostics = _mapping(result.get("diagnostics"))
    reason = str(diagnostics.get("overview_failure_reason") or "")
    error = str(result.get("error") or "")
    if result.get("route_changed") or state in {"route_changed", "stale"} or reason == "route_changed":
        return OverviewLookupDisplay("stale", OVERVIEW_FAILURE_TEXT["route_changed"], "warning")
    if reason == "validation_cancelled" or state == "cancelled":
        return OverviewLookupDisplay("partial", OVERVIEW_FAILURE_TEXT["validation_cancelled"], "warning")
    identity = _mapping(result.get("identity"))
    family_states = {str(_mapping(identity.get(family)).get("state") or "") for family in ("ipv4", "ipv6")}
    if identity.get("conflict") or "conflict" in family_states or reason == "identity_conflict" or state == "conflict":
        return OverviewLookupDisplay("conflict", OVERVIEW_FAILURE_TEXT["identity_conflict"], "warning")
    if "rotating" in family_states or reason == "identity_rotating" or state == "rotating":
        return OverviewLookupDisplay("partial", OVERVIEW_FAILURE_TEXT["identity_rotating"], "warning")
    warning = str(diagnostics.get("overview_warning_reason") or "")
    if warning in OVERVIEW_WARNING_TEXT and not error and state in {"partial", "complete"}:
        return OverviewLookupDisplay("partial", OVERVIEW_WARNING_TEXT[warning], "warning")
    message = OVERVIEW_FAILURE_TEXT.get(reason) or OVERVIEW_FAILURE_TEXT.get(error)
    if message:
        timed_out = reason in _TIMEOUT_REASONS or error == "timeout"
        soft = reason in _SOFT_FAILURE_REASONS or (not reason and error in _SOFT_FAILURE_REASONS)
        return OverviewLookupDisplay(
            "error" if timed_out else "partial", message, "warning" if soft else "error",
        )
    if state == "data_plane_failed":
        return OverviewLookupDisplay("partial", "出口验证未通过，请稍后刷新", "error")
    if state in {"controller_hold", "data_plane_indeterminate"}:
        return OverviewLookupDisplay("partial", "校验暂不可用，请稍后刷新", "error")
    if error:
        return OverviewLookupDisplay("error", "获取失败，请重试", "error")
    if state == "unavailable":
        return OverviewLookupDisplay("partial", "获取失败，请重试", "error")
    freshness = _mapping(result.get("freshness"))
    if freshness.get("state") in {"stale", "expired"}:
        return OverviewLookupDisplay("stale", "结果已过期，请刷新")
    return OverviewLookupDisplay("ok", "", "ok")


def overview_visible_identity(result: Mapping) -> dict:
    """Retain independent successful families, never a conflicted candidate IP."""
    visible = dict(result)
    identity = dict(_mapping(result.get("identity")))
    blocked = result.get("state") in {
        "unavailable", "data_plane_failed", "controller_hold", "data_plane_indeterminate",
        "route_changed", "stale", "cancelled",
    } or bool(result.get("route_changed"))
    for family in ("ipv4", "ipv6"):
        payload = dict(_mapping(identity.get(family)))
        if blocked or payload.get("state") in {"conflict", "rotating", "stale", "unavailable"}:
            payload.pop("ip", None)
            payload.pop("location", None)
            payload.pop("network", None)
        identity[family] = payload
    visible["identity"] = identity
    if blocked or (result.get("identity") and not any(_mapping(identity.get(f)).get("ip") for f in ("ipv4", "ipv6"))):
        visible.pop("ip", None)
    return visible


def overview_service_availability(
    status: Mapping | None,
    *,
    busy_tasks: Iterable[str] = (),
) -> str:
    """Classify whether a lookup can make progress right now.

    ``offline`` has no Core to carry a lookup, ``starting`` is still building
    one, and ``ready`` lets the runtime decide. A status without an explicit
    lifecycle phase is never treated as offline.
    """
    tasks = set(busy_tasks)
    if "stop-services" in tasks:
        return "offline"
    if tasks & {"start-services", "restart-core"}:
        return "starting"
    payload = _mapping(status)
    phase = str(payload.get("startup_phase") or "").strip().lower()
    if not phase:
        return "ready"
    core = _mapping(payload.get("core"))
    if bool(payload.get("core_running", core.get("running", False))):
        return "offline" if phase == "stopping" else "ready"
    if phase == "collecting":
        return "starting"
    if phase in {"stopped", "stopping", "failed"}:
        return "offline"
    return "ready"


def overview_result_awaits_connection(result: Mapping | None) -> bool:
    """Return whether a lookup ended only because the connection is not built yet."""
    payload = _mapping(result)
    diagnostics = _mapping(payload.get("diagnostics"))
    reason = str(diagnostics.get("overview_failure_reason") or "")
    if reason == "waiting_connection":
        return True
    return bool(
        reason == "connection_not_ready"
        and str(diagnostics.get("data_plane_reason") or "") in _CONNECTION_PENDING_REASONS
    )
