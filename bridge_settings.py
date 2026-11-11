"""T-P2-10 unified save orchestration: settings.apply and settings.validate.

``settings.apply{base_rev, changes}`` runs as one serialized mutation-domain
transaction (cancellable only while queued). Each ConfigChange is handled on
its own, in this order (PROTOCOL sections 10, 15, 16; 决定_P2_ASK02 #2):

1. ``bridge_validation.validate_change`` against the latest backend section
   (effective configuration: base file plus config.local.yaml);
2. (alerts no longer carry secrets: notices are local only since the 提醒
   redesign of 10-30, and ``alerts`` is three switches under ``notify``);
3. the preference sections (ui, traffic) are written without the full Python
   reload (Qt's ``write_ui_preferences`` / the no-reload local entry); the
   other local sections with ONE ``update_local_config`` call; only
   leaves whose value actually changes are written, so hidden keys
   (``node_judgement``), ``ui.theme``, the controller port and every other
   untouched key keep their exact file content and are never replaced by a
   default. Sources go through P2-13's single batch entry (one write, at most
   one reload); autostart is the registry write of P2-28's manager;
4. ``settings_effects.plan()`` over the sections actually saved;
5. the Core plan: live restart/reload through the existing entries with
   reason codes and a real commit ID (DNS incremental reload keeps the
   restart fallback), or the deferred intent of P2-11 (``request``);
6. independent actions: system-proxy bypass override, language sync and the
   separate latency recheck entry.

A failed section never rolls back another one. The job is ``failed`` when any
section has an error, with the complete SaveResult as its result; it is
``done`` only when every section is saved, and a section that is not applied
yet must be covered by ``pending``. ``config.changed`` is recorded after every
controlled write (with ``external_detection: bridge_writes_only``) and
``ui.language`` after a saved language change.

Runs on executor workers only; never on the pipe reader or a publication
tick. Exception text and configuration values never enter a result, an
Error or a log line (type names only).
"""
from __future__ import annotations

import logging
import re
import uuid
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from bridge_jobs import JobFailure
from bridge_validation import validate_change, validate_field
from desktop_runtime_preferences import UI_PREFERENCE_KEYS
from runtime_reload_gate import RuntimeReloadReason


LOGGER = logging.getLogger("tesseract.bridge.settings")
# Sections persisted in config.local.yaml (one merged write).
LOCAL_SECTIONS = ("network", "dns", "routing", "latency", "alerts", "ui", "traffic", "killswitch",
                  "bastion")
# Preference sections: written without the full Python reload_config (which
# clears the audit overlay, rebuilds the traffic monitor and reloads every
# tier's runtime nodes). Never merged into the reload write when a no-reload
# entry is available (see _write_preferences).
PREFERENCE_SECTIONS = ("ui", "traffic")
# Injected by DesktopRuntime._disable_external_desktop_notifications on every
# reload; never a user value, never part of a diff baseline.
_NATIVE_UI_MARKER = "disabled_in_native_ui"
# D8: typed here until bridge_validation lists it (see report, Claude ruling).
TRAY_KEY = "start_in_tray_on_autostart"
# Topic-legal PendingApply states for a ``done`` SaveResult.
DONE_PENDING_STATES = frozenset({"pending", "applying", "applied"})
FAILURE_CODES = frozenset({"validation", "conflict", "io", "lifecycle_busy",
                           "runtime_unavailable", "internal"})
# 提醒 redesign 10-30: the alerts keys still in use (the switches and the
# logging keys the runtime reads). Saving the alerts section removes every
# other key, i.e. the retired channel blocks of any type (desktop, telegram,
# ntfy, channels, ...) and the credentials they hold, from config.local.yaml.
_ALERT_KEEP_KEYS = frozenset(("notify", "log_file", "level", "history_limit"))
_PROXY_OVERRIDE_FAILURES = frozenset({"read_failed", "write_failed"})
# A stop between the latency probe and its Core reload (P2-10 #4): not applied,
# never turned into effect=none by the "Core stopped meanwhile" rule (#11).
RECHECK_STOPPED = "latency_recheck_stopped"
# The Core was already gone (or stopping) when the latency probe would run:
# nothing was probed or reloaded. Settled by #11 (effect none, applied) when
# Core stopped meanwhile; otherwise a plain failure.
RECHECK_NOT_RUNNING = "latency_recheck_not_running"
_MISSING = object()


# -- Error DTOs ----------------------------------------------------------------

def _error(code: str, reason: str, *, key: str | None = None, section: str | None = None,
           field: str | None = None, hold: bool | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {
        "code": code,
        "message_key": key or {
            "validation": "errors.settings.invalid_field",
            "conflict": "errors.settings.conflict",
            "lifecycle_busy": "errors.settings.lifecycle_busy",
            "runtime_unavailable": "errors.settings.apply_unavailable",
        }.get(code, "errors.settings.apply_failed"),
        "params": {"section": section} if section else {},
        "reason_code": reason,
    }
    if field is not None:
        error["field"] = field
        error["params"] = {"field": field}
    if hold is not None:
        error["hold"] = hold
    return error


class ConfigOwnerChanged(RuntimeError):
    """The configuration owner was replaced while the baseline was read."""


def _save_error(section: str, reason: str, code: str = "io") -> dict[str, Any]:
    return _error(code, reason, key="errors.settings.save_failed", section=section)


def _apply_error(section: str, reason: str, code: str = "io") -> dict[str, Any]:
    return _error(code, reason, key="errors.settings.apply_failed", section=section)


def _validation_error(error: Mapping[str, Any]) -> dict[str, Any]:
    """bridge_validation Error plus the focusable field and the hold flag."""
    result = deepcopy(dict(error))
    field = result.get("params", {}).get("field")
    if isinstance(field, str) and field:
        result["field"] = field
    result["hold"] = True
    return result


# -- Backend section snapshots ---------------------------------------------------

def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def backend_sections(config: Mapping[str, Any], *, autostart: bool | None) -> dict[str, Any]:
    """Latest backend sections (validator ``current``; private, with secrets).

    ``network`` is the client block without ``dns`` plus
    ``subscription_port`` (subscription_server.port). Retained keys such as
    ``egress_recovery.node_judgement`` and ``mihomo_controller`` are carried
    over unchanged, so validation can never default them. ``alerts`` is the
    effective ``alerts.notify`` (``alert_policy.notify_settings``), the same
    value config.snapshot shows.
    """
    from alert_policy import notify_settings
    from config_utils import normalize_routing_rule_order
    from routing_rules import effective_rule_list
    config = _mapping(config)
    client = _mapping(config.get("client"))
    network = {key: deepcopy(value) for key, value in client.items() if key != "dns"}
    subscription = _mapping(config.get("subscription_server"))
    if "port" in subscription:
        network["subscription_port"] = subscription["port"]
    routing = _mapping(config.get("routing"))
    return {
        "network": network,
        "dns": deepcopy(_mapping(client.get("dns"))),
        # rule_order is the effective (completed) order, as config.snapshot
        # shows it, so submitting the shown order back writes nothing.
        "routing": {"rules": deepcopy(_mapping(routing.get("rules"))),
                    "rule_order": normalize_routing_rule_order(routing.get("rule_order")),
                    # 配置整理 P2: the rules as one list (the legacy layout read as one while
                    # the file has no routing.rules.list) and the rule sets, with their
                    # addresses (private), as config.snapshot shows them
                    "rule_list": deepcopy(effective_rule_list(routing)),
                    "providers": deepcopy(_mapping(routing.get("rule_providers")))},
        "latency": deepcopy(_mapping(config.get("testing"))),
        "alerts": {"notify": notify_settings(config.get("alerts"))},
        "ui": deepcopy(_mapping(config.get("ui"))),
        "autostart": {} if autostart is None else {"enabled": bool(autostart)},
        "traffic": deepcopy(_mapping(config.get("traffic"))),
        "killswitch": deepcopy(_mapping(config.get("killswitch"))),
        "bastion": bastion_section(config),
    }


def bastion_section(config: Mapping[str, Any]) -> dict[str, Any]:
    """T-P4-C1 #1: Bastion policy as the Qt card reads it (tier3_config_view).

    ``max_pool_size`` is the retained pool capacity (D9 upper bound of
    ``min_alive_nodes``); the page cannot submit it and it is never written.
    """
    from bridge_runtime_access import bastion_capacity, bastion_values
    return {**bastion_values(config), "max_pool_size": bastion_capacity(config)}


# -- Minimal-diff local write ----------------------------------------------------

def _set_path(payload: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = payload
    for key in path[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[path[-1]] = deepcopy(value)


def _write_diff(payload: dict[str, Any], path: tuple[str, ...], old: Any, new: Any) -> None:
    """Write only leaves whose value changes; never touch any other key."""
    if isinstance(old, dict) and isinstance(new, dict):
        for key, value in new.items():
            _write_diff(payload, path + (key,), old.get(key, _MISSING), value)
        return
    if old is not _MISSING and old == new:
        return
    _set_path(payload, path, new)


def write_section(payload: dict[str, Any], section: str, old: Any, new: Any) -> None:
    """Merge one validated section into the config.local.yaml payload."""
    if section == "network":
        for key, value in new.items():
            path = ("subscription_server", "port") if key == "subscription_port" else ("client", key)
            _write_diff(payload, path, old.get(key, _MISSING), value)
        if "ipv6" in new and new.get("ipv6") != old.get("ipv6", _MISSING):
            # Qt's network save keeps the resolver's IPv6 switch with the client.
            _set_path(payload, ("client", "dns", "ipv6"), new["ipv6"])
    elif section == "dns":
        _write_diff(payload, ("client", "dns"), old, new)
    elif section == "routing":
        from config_utils import canonicalize_routing_rule_layout
        canonicalize_routing_rule_layout(payload)
        # 配置整理 P2: the list goes to routing.rules.list and the rule sets to
        # routing.rule_providers, each written whole when it changed (a removed rule set is
        # gone); the legacy keys keep their leaf-by-leaf write
        new, old = dict(new), dict(old)
        lists = [(("routing", "rules", "list"), old.pop("rule_list", _MISSING), new.pop("rule_list", _MISSING)),
                 (("routing", "rule_providers"), old.pop("providers", _MISSING), new.pop("providers", _MISSING))]
        _write_diff(payload, ("routing",), old, new)
        for path, before, after in lists:
            if after is not _MISSING and after != before:
                _set_path(payload, path, deepcopy(after))
    elif section == "latency":
        _write_diff(payload, ("testing",), old, new)
    elif section in {"ui", "traffic", "killswitch"}:
        _write_diff(payload, (section,), old, new)
    elif section == "bastion":
        # The existing Qt save-tier3 entry (desktop_app._run_tier3_config_save):
        # tiers.tier3.enabled / min_alive_nodes, never the capacity keys.
        if (old.get("enabled"), old.get("min_alive_nodes")) != (new.get("enabled"), new.get("min_alive_nodes")):
            from tier3_config_view import apply_tier3_config_payload
            apply_tier3_config_payload(
                payload, enabled=bool(new["enabled"]),
                min_alive_text=str(new["min_alive_nodes"]),
                default_min_alive=int(old.get("min_alive_nodes") or 1),
                max_pool_size=old.get("max_pool_size"))
    elif section == "alerts":
        _write_diff(payload, ("alerts", "notify"), old.get("notify", {}), new.get("notify", {}))
        alerts = payload.get("alerts")
        if isinstance(alerts, dict):
            for key in [key for key in alerts if key not in _ALERT_KEEP_KEYS]:
                alerts.pop(key)
    else:
        raise ValueError("section is not stored in the local configuration")


# -- Runtime port ----------------------------------------------------------------

class SettingsPort(Protocol):
    """Runtime facts and entries used by the save orchestration."""

    def config_revision(self) -> int: ...
    def commit_config_revision(self) -> int: ...
    def effective_config(self) -> Mapping[str, Any]:
        """Base + local merge; raises ConfigOwnerChanged when read stale."""
    def autostart_enabled(self) -> bool | None: ...
    def lifecycle(self) -> tuple[str, bool]:
        """(startup_phase, Core running and not cleanup-pending)."""
    def stop_requested(self) -> bool:
        """Lifecycle intent: a stop is pending or Core is no longer wanted."""
    def update_local(self, updater: Callable[[dict[str, Any]], dict[str, Any]]) -> Any: ...
    def update_local_preferences(self, updater: Callable[[dict[str, Any]], dict[str, Any]]) -> Any:
        """Locked read-update-write of config.local.yaml WITHOUT reload_config.

        DesktopRuntime.update_local_preferences (T-P2-RT2). Raises
        NotImplementedError only on a runtime without that entry.
        """
    def write_ui_preferences(self, preferences: Mapping[str, Any]) -> Any:
        """Qt's no-reload UI preference save (language/theme/overview lookup)."""
    def read_local(self) -> Mapping[str, Any]: ...
    def set_autostart(self, enabled: bool) -> Mapping[str, Any]: ...
    def restart_core(self) -> bool: ...
    def reload_core(self, *, mode: str, reason: str, user_commit_id: str) -> bool: ...
    def refresh_dns_policy(self) -> None: ...
    def recheck_latency(self, user_commit_id: str = "") -> bool: ...
    def probe_latency(self) -> Any:
        """Probe half of the latency recheck (no Core reload, no bridge lock).

        Raises NotImplementedError on a runtime without the split entries;
        the orchestrator then runs ``recheck_latency`` under the lock.
        """
    def reload_latency(self, probe: Any, user_commit_id: str = "") -> str:
        """Reload half, under the network lock: "ok", "failed", "stopped"
        (a stop or another Core owner since the probe abandoned the reload)
        or "not_running" (no running Core at the probe: nothing probed)."""
    def apply_system_proxy_override(self) -> bool: ...
    def sync_language(self, language: str) -> None: ...


class RuntimeSettingsPort:
    """Adapter over DesktopRuntime public methods/attributes only."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def config_revision(self) -> int:
        return int(self.runtime.bridge_config_revision())

    def commit_config_revision(self) -> int:
        return int(self.runtime.bridge_commit_config_revision())

    def effective_config(self) -> Mapping[str, Any]:
        facts = self.runtime.bridge_config_facts()
        if facts.get("stale"):
            raise ConfigOwnerChanged("configuration owner changed during the read")
        return facts["config"]

    def autostart_enabled(self) -> bool | None:
        state = self.runtime.bridge_autostart_state()
        if state.get("read_errors"):
            return None
        enabled = state.get("values", {}).get("enabled")
        return enabled if isinstance(enabled, bool) else None

    def lifecycle(self) -> tuple[str, bool]:
        phase = str(self.runtime.lifecycle_status()["runtime"]["startup_phase"])
        core = self.runtime.core_fact()
        return phase, bool(core["running"]) and not bool(core["cleanup_pending"])

    def stop_requested(self) -> bool:
        # Re-read lifecycle (决定_第二波裁定 P2-10 #11): stop requested, Core
        # no longer wanted, or the runtime already stopping.
        status = self.runtime.lifecycle_status()
        intent = status.get("intent") or {}
        phase = (status.get("runtime") or {}).get("startup_phase")
        return (bool(intent.get("stop_requested")) or intent.get("desired_running") is False
                or phase == "stopping")

    def update_local(self, updater):
        return self.runtime.update_local_config(updater)

    def update_local_preferences(self, updater):
        try:
            entry = self.runtime.update_local_preferences
        except AttributeError:
            raise NotImplementedError("DesktopRuntime.update_local_preferences") from None
        return entry(updater)

    def write_ui_preferences(self, preferences):
        return self.runtime.write_ui_preferences(dict(preferences))

    def read_local(self) -> Mapping[str, Any]:
        return self.runtime.read_local_config()

    def set_autostart(self, enabled: bool) -> Mapping[str, Any]:
        return self.runtime.set_autostart(bool(enabled))

    def restart_core(self) -> bool:
        before = int(self.runtime.core_fact()["core_generation"])
        raw = self.runtime.restart_core()
        after = self.runtime.core_fact()
        return (bool(after["running"]) and not bool(after["cleanup_pending"])
                and int(after["core_generation"]) > before
                and not (isinstance(raw, dict) and (raw.get("cleanup_pending")
                                                    or raw.get("applied") is False)))

    def reload_core(self, *, mode: str, reason: str, user_commit_id: str) -> bool:
        from runtime_tier_state import reload_core_config_succeeded
        result = self.runtime.reload_core_config(mode=mode, reason=reason,
                                                 user_commit_id=user_commit_id)
        return reload_core_config_succeeded(result)

    def refresh_dns_policy(self) -> None:
        self.runtime.refresh_dns_resolver_policy("settings_changed")

    def recheck_latency(self, user_commit_id: str = "") -> bool:
        try:
            recheck = self.runtime.recheck_latency_policy
        except AttributeError:
            raise NotImplementedError("DesktopRuntime.recheck_latency_policy") from None
        result = recheck(user_commit_id=user_commit_id)
        return isinstance(result, dict) and not result.get("errors")

    def probe_latency(self) -> Any:
        try:
            probe = self.runtime.probe_latency_policy
        except AttributeError:
            raise NotImplementedError("DesktopRuntime.probe_latency_policy") from None
        return probe()

    def reload_latency(self, probe: Any, user_commit_id: str = "") -> str:
        result = self.runtime.reload_latency_policy(probe, user_commit_id=user_commit_id)
        if not isinstance(result, dict):
            return "failed"
        if result.get("reload_skipped") == "not_running":
            return "not_running"
        if result.get("reload_skipped"):
            return "stopped"
        return "failed" if result.get("errors") else "ok"

    def apply_system_proxy_override(self) -> bool:
        result = self.runtime.apply_system_proxy_override()
        return not (isinstance(result, dict) and result.get("reason") in _PROXY_OVERRIDE_FAILURES)

    def sync_language(self, language: str) -> None:
        self.runtime.alerts.set_language(language)


# -- Orchestration ---------------------------------------------------------------

@dataclass
class _Section:
    section: str
    saved: bool = False
    applied: bool = False
    effect: str = "none"
    error: dict[str, Any] | None = None
    before: Any = None
    after: Any = None
    written: bool = False      # A controlled write committed a change.
    current: bool = False      # Runtime config reflects the save (plan input).
    language_synced: bool = False  # ui: the write entry itself synced the language.

    def fail(self, error: dict[str, Any]) -> None:
        if self.error is None:
            self.error = error
        self.applied = False

    def dto(self) -> dict[str, Any]:
        return {"section": self.section, "saved": self.saved, "applied": self.applied,
                "effect": self.effect, "error": deepcopy(self.error)}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _at(value: Any, path: tuple) -> Any:
    for part in path:
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and isinstance(part, int) and 0 <= part < len(value):
            value = value[part]
        else:
            return None
    return value


class SettingsService:
    """settings.apply / settings.validate handlers (see module docstring)."""

    def __init__(self, port: SettingsPort, *, sources: Any = None,
                 coordinator: Callable[[], Any] = lambda: None,
                 register_secrets: Callable[[dict[str, str]], None] | None = None,
                 refresh_secrets: Callable[[], None] | None = None,
                 network_guard: Callable[[], Any] | None = None,
                 recovery_gate: Callable[[], None] | None = None,
                 record_edge: Callable[[str, dict[str, Any]], Any] | None = None) -> None:
        self.port = port
        self.sources = sources
        self._coordinator = coordinator
        self._register_secrets = register_secrets
        self._refresh_secrets = refresh_secrets
        self._network_guard = network_guard or nullcontext
        self._recovery_gate = recovery_gate or (lambda: None)
        self._record_edge = record_edge

    # -- settings.validate (normal domain, res, no side effects) ---------------
    def validate(self, args: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
        section, name, value = args["section"], args["field"], args["value"]
        if section == "ui" and name == TRAY_KEY:
            if isinstance(value, bool):
                return {"valid": True, "errors": []}
            return {"valid": False, "errors": [
                {"code": "validation", "message_key": "errors.settings.invalid_field",
                 "params": {"field": f"ui.{TRAY_KEY}"}}]}
        capacity = None
        if section == "bastion" and name == "min_alive_nodes":
            # D9: bound by the current pool capacity; unknown capacity
            # (owner changed during the read) leaves the apply-time check.
            try:
                capacity = bastion_section(self.port.effective_config())["max_pool_size"]
            except Exception:
                capacity = None
        return validate_field(section, name, value, bastion_capacity=capacity)

    # -- settings.apply (mutation domain, job, queued) --------------------------
    def apply(self, args: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
        changes = [deepcopy(change) for change in args["changes"]]
        states = {change["section"]: _Section(change["section"]) for change in changes}
        try:
            self._recovery_gate()
        except Exception:
            raise self._fail_all(states, _error("lifecycle_busy", "recovery_in_progress",
                                                hold=True), self._safe_revision())
        revision = self.port.config_revision()
        if args["base_rev"] != revision:
            raise self._fail_all(states, _error("conflict", "base_rev_mismatch", hold=True),
                                 revision)
        try:
            config = self.port.effective_config()
        except ConfigOwnerChanged:
            # A stale baseline would diff against values that are no longer
            # loaded: reject the whole batch, write nothing.
            raise self._fail_all(states, _error("conflict", "config_owner_changed", hold=True),
                                 revision) from None
        try:
            return self._apply(changes, states, revision, config)
        finally:
            if self._refresh_secrets is not None:
                try:
                    self._refresh_secrets()
                except Exception:
                    LOGGER.warning("Known-secret refresh after settings save failed")

    def _apply(self, changes, states, revision, config):
        before = backend_sections(config, autostart=self.port.autostart_enabled()
                                  if "autostart" in states else None)
        validated: dict[str, Any] = {}
        # 1. validation, per section.
        for change in changes:
            section = change["section"]
            state = states[section]
            if section == "sources" or state.error is not None:
                continue
            result, after, error = self._validate(change, before)
            if error is not None:
                state.fail(error)
                continue
            state.before, state.after = before[section], after
            validated[section] = result
        # 3. writes: preferences without a Python reload, one merged local
        # write for the rest, the source batch, autostart.
        files: list[str] = []
        local = [states[s] for s in LOCAL_SECTIONS if s in validated]
        for state in local:
            if state.after == state.before:
                state.saved = state.applied = state.current = True
        pending_write = [state for state in local if not state.saved]
        reload_group = [state for state in pending_write if state.section not in PREFERENCE_SECTIONS]
        reload_group += self._write_preferences(
            [state for state in pending_write if state.section in PREFERENCE_SECTIONS])
        reload_group.sort(key=lambda state: LOCAL_SECTIONS.index(state.section))
        if reload_group:
            self._write_local(reload_group)
        if any(state.written for state in local):
            files.append("local")
        if "sources" in states:
            if self._save_sources(next(c for c in changes if c["section"] == "sources"),
                                  states["sources"]):
                files.append("sources")
        if "autostart" in validated:
            self._save_autostart(states["autostart"])
        written = any(state.written for state in states.values())
        # 4-6. Everything after a committed write is guarded: a fault here
        # still yields the SaveResult (saved facts kept) and config.changed.
        pending = None
        planned: list[_Section] = []
        try:
            if written:
                revision = self.port.commit_config_revision()
            # 4. plan over what is saved and loaded.
            planned = [state for state in states.values()
                       if state.section != "sources" and state.written and state.current]
            if planned:
                from settings_effects import plan as plan_effects
                phase, live = self.port.lifecycle()
                plan = plan_effects({s.section: s.before for s in planned},
                                    {s.section: s.after for s in planned},
                                    running=live, phase=phase)
                commit_id = f"settings-apply:{revision}:{uuid.uuid4().hex[:12]}"
                # 5. Core plan, then 6. independent actions.
                core_error, recheck_error = self._run_core(plan, commit_id)
                core_stopped = (core_error is not None or recheck_error is not None) \
                    and (self._core_stopped_meanwhile()
                         or (recheck_error or {}).get("reason_code") == RECHECK_NOT_RUNNING)
                pending = self._request_deferred(plan, revision, commit_id)
                action_errors = self._run_actions(plan, states)
                self._settle(plan, states, core_error, recheck_error, action_errors, pending,
                             core_stopped=core_stopped)
            elif written and self._coordinator() is not None:
                # A source/no-Core write still advances a live intent's revision.
                pending = self._request_deferred(None, revision, f"settings-apply:{revision}")
        except Exception as exc:
            LOGGER.warning("Settings apply after the write failed: %s", type(exc).__name__)
            targets = planned or [state for state in states.values()
                                  if state.section != "sources" and state.written]
            for state in targets:
                if state.saved and state.error is None:
                    state.fail(_apply_error(state.section, "apply_exception", "internal"))
            if written:
                revision = self._safe_revision()
        result = {"rev": revision, "sections": [states[c["section"]].dto() for c in changes],
                  "pending": pending if isinstance(pending, dict)
                  and pending.get("state") in DONE_PENDING_STATES else None}
        if written:
            self._publish(result, files, pending, states, revision)
        failed = [states[c["section"]] for c in changes if states[c["section"]].error is not None]
        if failed:
            error = failed[0].error
            code = error["code"] if error["code"] in FAILURE_CODES else "internal"
            raise JobFailure(code, result, error=deepcopy(error))
        return result

    # -- steps ------------------------------------------------------------------
    @staticmethod
    def _validate(change, before):
        section = change["section"]
        tray = _MISSING
        if section == "ui" and isinstance(change.get("values"), dict) and TRAY_KEY in change["values"]:
            change = deepcopy(change)
            tray = change["values"].pop(TRAY_KEY)
            if not isinstance(tray, bool):
                return None, None, _validation_error(
                    {"code": "validation", "message_key": "errors.settings.invalid_field",
                     "params": {"field": f"ui.{TRAY_KEY}"}})
        result = validate_change(change, before)
        if not result.valid:
            return None, None, _validation_error(result.errors[0])
        after = deepcopy(result.values)
        if tray is not _MISSING:
            after[TRAY_KEY] = tray
        if section == "network":
            old = before["network"]
            if "mihomo_mixed_port" in after and after["mihomo_mixed_port"] != old.get("mihomo_mixed_port"):
                # Qt keeps the system-proxy target on the mixed port.
                proxy = after.get("system_proxy")
                proxy = proxy if isinstance(proxy, dict) else {}
                proxy.setdefault("host", "127.0.0.1")
                proxy["port"] = after["mihomo_mixed_port"]
                after["system_proxy"] = proxy
        return result, after, None

    def _write_local(self, states: list[_Section]) -> bool:
        outcome = self._local_write(states)
        if outcome == "failed" and len(states) > 1:
            # Isolate the failing section: a section failure never takes the
            # others with it (the merged write committed nothing).
            committed = False
            for state in states:
                committed = self._local_outcome([state], self._local_write([state])) or committed
            return committed
        return self._local_outcome(states, outcome)

    def _write_preferences(self, states: list[_Section]) -> list[_Section]:
        """ui / traffic without the full Python reload_config (Qt parity).

        Order: the runtime's no-reload local entry for every preference
        section; until that entry exists, a ui change limited to Qt's
        preference keys goes through ``write_ui_preferences`` exactly like
        Qt's UI save. Anything else (the D8 key, traffic) is returned for
        the merged reload write; see the report's runtime-entry section.
        """
        if not states:
            return []
        try:
            outcome = self._local_write(states, writer=self.port.update_local_preferences)
        except NotImplementedError:
            pass
        else:
            self._local_outcome(states, outcome, apply_reason="preferences_apply_failed")
            if outcome == "written":
                for state in states:
                    # The runtime entry already synced the alert language.
                    state.language_synced = state.language_synced or state.section == "ui"
            return []
        remaining = []
        for state in states:
            changed = self._changed_ui_preferences(state)
            if changed is None:
                remaining.append(state)
            else:
                self._write_ui_preferences(state, changed)
        return remaining

    @staticmethod
    def _changed_ui_preferences(state: _Section) -> dict[str, Any] | None:
        """Changed ui keys when all are Qt preference keys, else None."""
        if state.section != "ui":
            return None
        scratch: dict[str, Any] = {}
        write_section(scratch, "ui", state.before, state.after)
        changed = _mapping(scratch.get("ui"))
        if not changed or not set(changed) <= set(UI_PREFERENCE_KEYS):
            return None
        return changed

    def _write_ui_preferences(self, state: _Section, changed: dict[str, Any]) -> None:
        try:
            self.port.write_ui_preferences(changed)
        except Exception as exc:
            LOGGER.warning("UI preference write failed: %s", type(exc).__name__)
            try:
                stored = _mapping(_mapping(self.port.read_local()).get("ui"))
            except Exception:
                state.fail(_save_error(state.section, "save_outcome_unknown"))
                return
            if all(stored.get(key, _MISSING) == value for key, value in changed.items()):
                state.saved = state.written = True
                state.fail(_apply_error(state.section, "preferences_apply_failed"))
            else:
                state.fail(_save_error(state.section, "local_write_failed"))
            return
        # Qt's entry updates runtime.config["ui"] and syncs the alert language.
        state.saved = state.written = state.current = state.language_synced = True

    def _local_write(self, states: list[_Section], writer: Callable[[Any], Any] | None = None) -> str:
        record: dict[str, Any] = {}

        def updater(payload: dict[str, Any]) -> dict[str, Any]:
            record["original"] = deepcopy(payload)
            for state in states:
                write_section(payload, state.section, state.before, state.after)
            record["expected"] = deepcopy(payload)
            return payload

        try:
            (writer or self.port.update_local)(updater)
            return "written"
        except NotImplementedError:
            if "original" not in record:
                raise  # The writer entry does not exist; nothing was read or written.
            LOGGER.warning("Settings local write failed: NotImplementedError")
        except Exception as exc:
            LOGGER.warning("Settings local write failed: %s", type(exc).__name__)
        if "expected" not in record:
            return "failed"
        try:
            current = dict(self.port.read_local() or {})
        except Exception:
            return "unknown"
        if current == record["expected"] and current != record["original"]:
            return "reload_failed"
        return "failed" if current == record["original"] else "unknown"

    @staticmethod
    def _local_outcome(states: list[_Section], outcome: str,
                       apply_reason: str = "config_reload_failed") -> bool:
        for state in states:
            if outcome == "written":
                state.saved = state.written = state.current = True
            elif outcome == "reload_failed":
                state.saved = state.written = True
                state.fail(_apply_error(state.section, apply_reason))
            elif outcome == "unknown":
                state.fail(_save_error(state.section, "save_outcome_unknown"))
            else:
                state.fail(_save_error(state.section, "local_write_failed"))
        return outcome in {"written", "reload_failed"}

    def _save_sources(self, change, state: _Section) -> bool:
        if self.sources is None:
            state.fail(_save_error("sources", "sources_unavailable", "runtime_unavailable"))
            return False
        try:
            outcome = self.sources.save(change)
        except NotImplementedError:
            state.fail(_save_error("sources", "source_batch_unavailable", "runtime_unavailable"))
            return False
        except Exception as exc:
            LOGGER.warning("Source section save failed: %s", type(exc).__name__)
            state.fail(_save_error("sources", "source_save_internal_error", "internal"))
            return False
        section = outcome.section_result()
        state.saved, state.applied, state.effect = section["saved"], section["applied"], section["effect"]
        if section["error"] is not None:
            state.fail(section["error"])
        state.written = bool(outcome.written)
        return state.written

    def _save_autostart(self, state: _Section) -> None:
        wanted = state.after.get("enabled")
        if not isinstance(wanted, bool) or state.before.get("enabled") == wanted:
            state.saved = state.applied = True
            return
        try:
            outcome = dict(self.port.set_autostart(wanted) or {})
        except Exception as exc:
            LOGGER.warning("Autostart write failed: %s", type(exc).__name__)
            state.fail(_save_error("autostart", "autostart_write_failed"))
            return
        if outcome.get("error") is None and outcome.get("enabled") is wanted:
            state.saved = state.written = state.current = True
        elif outcome.get("supported") is False:
            state.fail(_save_error("autostart", "autostart_unsupported", "validation"))
        else:
            state.fail(_save_error("autostart", "autostart_write_failed"))

    def _action_ready(self, plan, name: str) -> bool:
        return any(action.action == name and action.state == "ready" for action in plan.actions)

    def _run_core(self, plan, commit_id: str):
        """Live Core effect under the network lock and recovery gate.

        The latency recheck (决定_第二波裁定 P2-10 #4) probes OUTSIDE the
        network lock and takes it again only for its Core reload; a stop in
        between abandons that reload (see ``_latency_recheck``).
        """
        core = plan.core
        recheck = self._action_ready(plan, "latency_recheck")
        if core.effect not in {"restart", "reload", "recheck"} and not recheck:
            return None, None
        core_error = recheck_error = None
        busy = _error("lifecycle_busy", "recovery_in_progress")
        dns = self._action_ready(plan, "refresh_dns_policy")
        if dns or core.effect in {"reload", "restart"}:
            try:
                with self._network_guard():
                    if dns:
                        try:  # Qt order: re-qualify the resolver, then render it.
                            self.port.refresh_dns_policy()
                        except Exception as exc:
                            LOGGER.warning("DNS policy refresh failed: %s", type(exc).__name__)
                    if core.effect in {"reload", "restart"}:
                        core_error = self._core_effect(core, commit_id)
            except Exception as exc:
                # The recovery gate (RT-1 still running) refused the Core work.
                LOGGER.warning("Settings Core apply refused: %s", type(exc).__name__)
                core_error = core_error or (busy if core.effect in {"reload", "restart"} else None)
                return core_error, (busy if recheck else None)
        if recheck:
            if core_error is not None:
                recheck_error = _apply_error("latency", "latency_recheck_skipped")
            else:
                # Derived from the save's commit ID: correlatable, but
                # distinct, so the reload gate never treats the
                # recheck export as the graph commit already done.
                recheck_error = self._latency_recheck(f"{commit_id}:latency-recheck")
        return core_error, recheck_error

    def _core_effect(self, core, commit_id: str) -> dict[str, Any] | None:
        try:
            if core.effect == "reload":
                graph = core.reason == RuntimeReloadReason.USER_GRAPH_CONFIG.value
                if self.port.reload_core(mode=core.mode or "strict", reason=core.reason,
                                         user_commit_id=commit_id if graph else ""):
                    return None
                if core.fallback_effect != "restart":
                    return _apply_error("core", "reload_not_confirmed")
            # Restart, or the DNS incremental-reload fallback kept from Qt.
            if self.port.restart_core():
                return None
            return _apply_error("core", "restart_not_confirmed")
        except Exception as exc:
            LOGGER.warning("Settings Core apply failed: %s", type(exc).__name__)
            return _apply_error("core", "apply_exception", "internal")

    def _core_stopped_meanwhile(self) -> bool:
        """A stop (control domain) overtook the planned live apply.

        Read only after a Core/recheck failure, by re-reading the lifecycle
        (决定_第二波裁定 P2-10 #11, ``SettingsPort.stop_requested``): a stop
        requested, ``desired_running`` false, or the runtime phase
        ``stopping``. A failed restart alone leaves ``desired_running`` true
        and the phase not ``stopping``, so it is still reported as a failure.
        """
        try:
            return bool(self.port.stop_requested())
        except Exception:
            return False

    def _latency_recheck(self, commit_id: str = "") -> dict[str, Any] | None:
        """Probe without the network lock, reload under it (P2-10 #4).

        A stop (or another Core) between the probe and the reload abandons the
        reload: the latency section is not applied
        (``lifecycle_busy`` / ``latency_recheck_stopped``). That includes a
        Core restarted by another operation: the reload is never attributed
        to a different Core, although that Core started from the saved file.
        No running Core at the probe: nothing is probed or reloaded
        (``latency_recheck_not_running``; #11 settles it as effect none). The
        recovery gate is checked before the probe as well, so an RT-1
        recovery never gets a network probe whose reload it would refuse. A
        runtime without the split entries runs the combined recheck under
        the lock (old path).
        """
        try:
            self._recovery_gate()
        except Exception as exc:
            LOGGER.warning("Latency recheck refused: %s", type(exc).__name__)
            return _error("lifecycle_busy", "recovery_in_progress")
        try:
            probe = self.port.probe_latency()
        except NotImplementedError:
            return self._latency_recheck_combined(commit_id)
        except Exception as exc:
            LOGGER.warning("Latency recheck probe failed: %s", type(exc).__name__)
            return _apply_error("latency", "apply_exception", "internal")
        entered = False
        try:
            with self._network_guard():
                entered = True
                outcome = self.port.reload_latency(probe, commit_id)
        except Exception as exc:
            if not entered:
                LOGGER.warning("Latency recheck reload refused: %s", type(exc).__name__)
                return _error("lifecycle_busy", "recovery_in_progress")
            LOGGER.warning("Latency recheck reload failed: %s", type(exc).__name__)
            return _apply_error("latency", "apply_exception", "internal")
        if outcome == "ok":
            return None
        if outcome == "stopped":
            return _error("lifecycle_busy", RECHECK_STOPPED,
                          key="errors.settings.apply_failed", section="latency")
        if outcome == "not_running":
            return _apply_error("latency", RECHECK_NOT_RUNNING)
        return _apply_error("latency", "latency_recheck_failed")

    def _latency_recheck_combined(self, commit_id: str = "") -> dict[str, Any] | None:
        """Probe and reload in one runtime call, under the network lock."""
        entered = False
        try:
            with self._network_guard():
                entered = True
                if self.port.recheck_latency(commit_id):
                    return None
                return _apply_error("latency", "latency_recheck_failed")
        except NotImplementedError:
            return _apply_error("latency", "latency_recheck_unavailable", "runtime_unavailable")
        except Exception as exc:
            if not entered:
                LOGGER.warning("Latency recheck refused: %s", type(exc).__name__)
                return _error("lifecycle_busy", "recovery_in_progress")
            LOGGER.warning("Latency recheck failed: %s", type(exc).__name__)
            return _apply_error("latency", "apply_exception", "internal")

    def _request_deferred(self, plan, revision: int, commit_id: str):
        """P2-11 intent: new deferred work, or advancing a live intent's rev."""
        coordinator = self._coordinator()
        if coordinator is None:
            return None
        try:
            dto = coordinator.request(plan, revision, commit_id)
            if dto is None:
                # T-P2-RT2: an intent parked between its Core half and its
                # latency reload is ``applying``; the ledger has no pending
                # intent to merge into, but that intent is the live fact.
                current = coordinator.pending()
                if isinstance(current, dict) and current.get("state") == "applying":
                    dto = current
            return dto
        except Exception as exc:
            LOGGER.warning("Deferred settings request failed: %s", type(exc).__name__)
            return None

    def _run_actions(self, plan, states):
        errors: dict[str, dict[str, Any]] = {}
        for action in plan.actions:
            try:
                if action.action == "system_proxy_override":
                    if not self.port.apply_system_proxy_override():
                        errors.setdefault("network", _apply_error("network", "system_proxy_override_failed"))
                elif action.action == "ui_preferences":
                    ui = states["ui"]
                    old, new = ui.before.get("language"), ui.after.get("language")
                    if new is not None and new != old and not ui.language_synced:
                        self.port.sync_language(new)
                        ui.language_synced = True
                # autostart: the registry write is the action. traffic and
                # killswitch preferences: applied by the local write's reload.
                # refresh_dns_policy / latency_recheck: run with the Core plan.
            except Exception as exc:
                LOGGER.warning("Settings action %s failed: %s", action.action, type(exc).__name__)
                reason = {"ui_preferences": "language_sync_failed",
                          "system_proxy_override": "system_proxy_override_failed"}.get(
                              action.action, "action_failed")
                errors.setdefault(action.section, _apply_error(action.section, reason))
        return errors

    @staticmethod
    def _settle(plan, states, core_error, recheck_error, action_errors, pending, *,
                core_stopped: bool = False) -> None:
        deferred_ok = isinstance(pending, dict) and pending.get("state") in DONE_PENDING_STATES
        for section, effect in plan.section_effects.items():
            state = states.get(section)
            if state is None or not state.saved:
                continue
            state.effect = effect
            state.applied = True
            abandoned = (effect == "recheck" and recheck_error is not None
                         and recheck_error.get("reason_code") == RECHECK_STOPPED)
            if core_stopped and effect in {"reload", "restart", "recheck"} and not abandoned:
                # Core stopped under the save (第一波裁定2 跨单 #1): nothing is
                # live to apply to; the next start loads the saved file.
                state.effect = "none"
            elif effect == "deferred":
                state.applied = False
                if not deferred_ok:
                    state.fail(_apply_error(section, "deferred_apply_unavailable",
                                            "runtime_unavailable"))
            elif effect in {"reload", "restart"} and core_error is not None:
                state.fail({**core_error, "params": {"section": section}})
            elif effect == "recheck" and recheck_error is not None:
                state.fail({**recheck_error, "params": {"section": section}})
            if section in action_errors:
                state.fail(action_errors[section])
        for state in states.values():
            # Unchanged-but-submitted saved sections are applied (effect none).
            if state.saved and state.error is None and state.section not in plan.section_effects \
                    and state.section != "sources":
                state.applied = True

    def _publish(self, result, files, pending, states, revision) -> None:
        if self._record_edge is None:
            return
        try:
            self._record_edge("config.changed", {
                "rev": revision, "files": files, "sections": deepcopy(result["sections"]),
                "pending": deepcopy(pending) if isinstance(pending, dict) else None,
                "external_detection": "bridge_writes_only"})
        except Exception as exc:
            LOGGER.warning("config.changed publication failed: %s", type(exc).__name__)
        ui = states.get("ui")
        if ui is None or not ui.written:
            return  # Not saved: the configured language keeps its value.
        language = ui.after.get("language")
        if language is not None and language != ui.before.get("language"):
            try:
                self._record_edge("ui.language", {"language": language, "config_rev": revision})
            except Exception as exc:
                LOGGER.warning("ui.language publication failed: %s", type(exc).__name__)

    def _safe_revision(self) -> int:
        try:
            return int(self.port.config_revision())
        except Exception:
            return 0

    @staticmethod
    def _fail_all(states, error, revision) -> JobFailure:
        sections = []
        for state in states.values():
            state.fail(deepcopy(error))
            sections.append(state.dto())
        result = {"rev": int(revision), "sections": sections, "pending": None}
        return JobFailure(error["code"], result, error=deepcopy(error))


# -- D4 first-run language (T-P4-C1 #6) -------------------------------------------

# GetUserDefaultUILanguage primary language (LANGID & 0x3ff) -> UI language;
# identical to the shell's native_text.rs ``language`` (shared test vectors in
# bridge_schema/fixtures/lang_map_vectors.json). U3: every Chinese variant
# (zh-TW / zh-HK / zh-SG / zh-MO) maps to zh_CN; no match -> en_US.
FIRST_RUN_LANGUAGES = {0x04: "zh_CN", 0x07: "de_DE", 0x0A: "es_ES", 0x0C: "fr_FR",
                       0x11: "ja_JP", 0x12: "ko_KR", 0x16: "pt_BR", 0x19: "ru_RU"}
FIRST_RUN_FALLBACK = "en_US"


def language_for_langid(langid: int) -> str:
    """D4 mapping of a Windows LANGID to one of the nine UI languages."""
    if type(langid) is not int or not 0 <= langid <= 0xFFFF:
        return FIRST_RUN_FALLBACK
    return FIRST_RUN_LANGUAGES.get(langid & 0x3FF, FIRST_RUN_FALLBACK)


def system_ui_langid() -> int | None:
    """``GetUserDefaultUILanguage()`` (Windows), None elsewhere or on failure."""
    import os
    if os.name != "nt":
        return None
    try:
        import ctypes
        value = int(ctypes.windll.kernel32.GetUserDefaultUILanguage())
    except Exception:
        return None
    return value if 0 < value <= 0xFFFF else None


def first_run_language(langid: int | None = None) -> str:
    langid = system_ui_langid() if langid is None else langid
    return FIRST_RUN_FALLBACK if langid is None else language_for_langid(langid)


def write_first_run_language(paths: Any, language: str, *, template: bool = False) -> bool:
    """Record the D4 language in config.local.yaml, the file every
    ``settings.apply{ui.language}`` save writes (ConfigStore.write_local via
    the runtime's preference entries), so the first saved language and every
    later one live in one place and the local layer wins over the template's
    ``ui.language: en_US`` in config.yaml.

    Only for a first run (the caller decides that before app_paths copies the
    template). ``template=True``: config.local.yaml was just created from the
    app_paths local template, whose ``ui.language: en_US`` is a default, not a
    choice, and is replaced. Otherwise a language already in the local layer
    is never overwritten. Returns whether it wrote.
    """
    from config_store import ConfigStore
    from desktop_runtime_preferences import apply_ui_preferences_to_payload
    from bridge_validation import LANGUAGES
    if language not in LANGUAGES:
        raise ValueError("unsupported first-run language")
    store = ConfigStore(local_config_path=paths.local_config_path,
                        sources_path=paths.sources_path, backup_dir=paths.config_backup_dir)
    payload = dict(store.read_local() or {})
    ui = payload.get("ui")
    if isinstance(ui, dict) and ui.get("language") and not template:
        return False
    if isinstance(ui, dict) and ui.get("language") == language:
        return False
    apply_ui_preferences_to_payload(payload, {"language": language})
    store.write_local(payload)
    return True


# -- Session wiring ----------------------------------------------------------------

def _session_recovery_gate(session: Any) -> Callable[[], None]:
    """report.gate.require_idle(), resolved at call time (may be None)."""
    def gate() -> None:
        current = getattr(getattr(session, "report", None), "gate", None)
        if current is not None:
            current.require_idle()
    return gate


def _session_network_guard(session: Any) -> Callable[[], Any]:
    """Same boundary as BridgeSession.restart: network lock + recovery gate."""
    recovery_gate = _session_recovery_gate(session)

    @contextmanager
    def guard():
        lock = getattr(session, "network_lock", None)
        with lock if lock is not None else nullcontext():
            recovery_gate()
            yield
    return guard


def _session_secret_hooks(session: Any):
    """T-P2-31b: draft values are a lease of the session's layered registry
    (bridge_secrets.session_secret_hooks); never a whole-table push."""
    from bridge_secrets import session_secret_hooks
    return session_secret_hooks(session, "settings")


def register(session: Any, *, port: SettingsPort | None = None, sources: Any = None) -> SettingsService:
    """Register settings.apply and settings.validate on session.executor.

    Specs come from bridge_commands: settings.apply is a mutation-domain job
    cancellable only while queued (no cancel callback; a started save always
    finishes and reports its facts), settings.validate a normal-domain res.
    Sources reuse P2-13's saver (``sources`` or ``bridge_sources.register``);
    the deferred intent uses P2-11's ``session.settings_apply`` when wired.
    config.changed / ui.language are recorded on ``session.state``.
    """
    runtime = getattr(session, "runtime", None)
    if runtime is None:
        runtime = session.access.runtime
    if sources is None:
        import bridge_sources
        sources = bridge_sources.register(session)
    register_secrets, refresh_secrets = _session_secret_hooks(session)
    state = getattr(session, "state", None)
    record_edge = getattr(state, "record_edge", None)
    service = SettingsService(
        port or RuntimeSettingsPort(runtime), sources=sources,
        coordinator=lambda: getattr(session, "settings_apply", None),
        register_secrets=register_secrets, refresh_secrets=refresh_secrets,
        network_guard=_session_network_guard(session),
        recovery_gate=_session_recovery_gate(session),
        record_edge=record_edge if callable(record_edge) else None)
    session.executor.register("settings.apply", service.apply)
    session.executor.register("settings.validate", service.validate)
    session.settings = service
    return service
