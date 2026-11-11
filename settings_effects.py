"""Pure save-effect classification over backend section values.

The plan is internal, not a wire DTO or evidence of saving/applying anything.
Inputs are complete before/after section snapshots (including retained values).
P2-10 executes independent actions; P2-11 owns deferred intent identity/lifetime.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from runtime_reload_gate import RuntimeReloadReason


SECTIONS = ("network", "dns", "routing", "latency", "sources", "alerts",
            "ui", "autostart", "traffic", "killswitch")


@dataclass(frozen=True)
class CoreEffect:
    effect: str = "none"
    reason: str | None = None
    mode: str | None = None
    user_commit_id: str | None = None  # P2-10 must assign a real commit ID.
    fallback_effect: str | None = None
    requested_effect: str | None = None  # Underlying effect when deferred.


@dataclass(frozen=True)
class IndependentAction:
    action: str
    section: str
    target_id: str | None = None
    state: str = "ready"  # ready / deferred / suppressed; never an execution receipt.


@dataclass(frozen=True)
class ApplyPlan:
    core: CoreEffect
    actions: tuple[IndependentAction, ...]
    section_effects: dict[str, str]

    def section_results(self) -> list[dict[str, Any]]:
        """Schema-valid SectionResult templates; caller fills actual facts."""
        return [{"section": section, "saved": False, "applied": False,
                 "effect": effect, "error": None}
                for section, effect in self.section_effects.items()]


def _channel_ids(value: Any) -> set[str]:
    if not isinstance(value, dict):
        return set()
    return {str(row["id"]) for row in value.get("channels", [])
            if isinstance(row, dict) and row.get("id")}


def plan(before: dict, after: dict, *, running: bool, phase: str) -> ApplyPlan:
    """Classify without Core, Qt, filesystem, clock, or registry access.

    Stop wins over startup/running facts. Startup phases may have running=False
    before Core exists; they still defer the requested Core work. Latency recheck
    remains an independent action even when a different section needs restart.
    """
    changed = [s for s in SECTIONS if before.get(s) != after.get(s)]
    starting = phase in {"collecting", "core_standby"}
    stopping = phase in {"stopping", "cleanup_pending"}
    live = running and not starting and not stopping
    runtime_state = "deferred" if starting else "ready" if live else "suppressed"
    actions: list[IndependentAction] = []
    effects = {s: "none" for s in changed}
    restart = False
    graph = False
    dns = "dns" in changed
    latency = "latency" in changed

    if "network" in changed:
        old, new = before.get("network") or {}, after.get("network") or {}
        # Policy is read from manager configuration, never rendered into Core.
        excluded = {"egress_recovery", "controller_port", "mihomo_controller"}
        old_core = {k: v for k, v in old.items() if k not in excluded}
        new_core = {k: v for k, v in new.items() if k not in excluded}
        old_proxy, new_proxy = old_core.pop("system_proxy", {}), new_core.pop("system_proxy", {})
        if old_proxy != new_proxy:
            actions.append(IndependentAction("system_proxy_override", "network"))
        # A bypass-only edit is applied through the OS override, not Core.
        old_core["system_proxy"] = {k: v for k, v in old_proxy.items() if k != "bypass"}
        new_core["system_proxy"] = {k: v for k, v in new_proxy.items() if k != "bypass"}
        restart = old_core != new_core
        effects["network"] = "restart" if restart else "none"
    if "routing" in changed:
        graph = True
        effects["routing"] = "reload"
    # Source editor/upsert only reloads Python configuration in Qt. It does not
    # appear in HOT_RELOAD_AFTER_CONFIG_TASKS; do not invent a graph reload.
    if dns:
        effects["dns"] = "reload"
        actions.append(IndependentAction("refresh_dns_policy", "dns", state=runtime_state))
    if latency:
        effects["latency"] = "recheck"
        actions.append(IndependentAction("latency_recheck", "latency", state=runtime_state))
    for section, action, effect in (
        ("ui", "ui_preferences", "ui"),
        ("autostart", "autostart", "autostart"),
        ("traffic", "traffic_preferences", "ui"),
        ("killswitch", "killswitch_preferences", "none"),
    ):
        if section in changed:
            effects[section] = effect
            actions.append(IndependentAction(action, section))
    if "alerts" in changed:
        removed = _channel_ids(before.get("alerts")) - _channel_ids(after.get("alerts"))
        actions.extend(IndependentAction("delete_delivery_records", "alerts", target)
                       for target in sorted(removed))

    if restart:
        core = CoreEffect("restart")
    elif graph:
        core = CoreEffect("reload", RuntimeReloadReason.USER_GRAPH_CONFIG.value, "strict")
    elif dns:
        core = CoreEffect("reload", RuntimeReloadReason.DNS_LIFECYCLE.value,
                          "incremental_export", fallback_effect="restart")
    elif latency:
        # Recheck executor performs incremental export after exit/tier1/tier2
        # checks, using USER_GRAPH_CONFIG and the orchestrator's commit ID.
        core = CoreEffect("recheck", RuntimeReloadReason.USER_GRAPH_CONFIG.value,
                          "incremental_export")
    else:
        core = CoreEffect()
    if core.effect != "none":
        if starting and not stopping:
            core = CoreEffect("deferred", core.reason, core.mode, core.user_commit_id,
                              core.fallback_effect, core.effect)
        elif not live:
            core = CoreEffect()
    for section in effects:
        if effects[section] in {"recheck", "reload", "restart"}:
            if starting and not stopping:
                effects[section] = "deferred"
            elif not live:
                effects[section] = "none"
    return ApplyPlan(core, tuple(actions), effects)
