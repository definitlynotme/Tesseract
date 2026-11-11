"""Desktop notice policy (提醒): which runtime facts become a notice, and when.

Pure and clock-driven. ``IncidentTracker.observe(facts, now)`` is fed about
once a second with what the overview shows (``service.state``,
``tiers.summary``, ``killswitch.state`` and the ``network.mode`` drift flag,
see ``facts_from``) and returns the notices to publish. No I/O, no locks and
no runtime access: the caller owns threading and delivery.

Rules (.build/提醒模块重设计_20261030.md §2, §3, §10):

* Nothing is reported while a run starts. A run is armed at its first ready
  sample that names a carrier tier, or ``ARM_TIMEOUT_S`` after it began
  running; a fault still present then is reported at once. A failed start is
  the exception and is reported immediately.
* A fault must persist before it opens (``OUTAGE_HOLD_S``, ``CAPTURE_HOLD_S``,
  ``FALLBACK_HOLD_S``) and stay away ``CLEAR_HOLD_S`` before it resolves.
* One incident is one notice id: it is updated or resolved in place, and a
  recovery is reported only for an incident that was reported.
* The ``REPEAT_COUNT``-th outage (or tier fallback) within ``REPEAT_WINDOW_S``
  becomes one "unstable" notice and mutes that kind for the window; an incident still
  open when the mute ends is reported then. A rewritten system proxy and a
  failed start are reported each time (a failed retry updates its card).
* A triggered kill switch is said on the card of the fault it belongs to: the
  outage card, or the failed-start card when that has no reason of its own.
  A fail-closed route alone never claims the kill switch.
* Conditions that never change (no administrator rights for the kill switch,
  a single node down) are not notices; the user's own stop closes whatever is
  open without a recovery notice.
* Tier fallbacks are judged against the run's own normal tier (the tier it
  settled on, or a better one that held ``BASELINE_HOLD_S``), so a run whose
  usual carrier is Bastion is not told about Bastion.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

ARM_TIMEOUT_S = 120.0
OUTAGE_HOLD_S = 20.0
CAPTURE_HOLD_S = 10.0
FALLBACK_HOLD_S = 30.0
CLEAR_HOLD_S = 5.0
BASELINE_HOLD_S = 60.0
REPEAT_WINDOW_S = 600.0
REPEAT_COUNT = 3

TTL_INFO_MS = 8000
TTL_WARN_MS = 10000
TTL_RESOLVED_MS = 6000

GROUP_CONNECTION = "connection"
GROUP_TIER = "tier_fallback"
GROUP_UPDATE = "update"
GROUP_PREVIEW = "preview"
# The groups the settings page can switch off, in display order.
NOTICE_GROUPS = (GROUP_CONNECTION, GROUP_TIER, GROUP_UPDATE)

_RUN_OF = {"collecting": "starting", "core_standby": "starting", "running": "running",
           "stopping": "stopping", "stopped": "stopped", "failed": "failed"}
_TIER_NAMES = {"vanguard": "Vanguard", "sentinel": "Sentinel", "bastion": "Bastion", "gateway": "Gateway"}
# Gateway is a fixed exit, never a fallback: it ranks with the first tier.
_RANK = {"vanguard": 0, "gateway": 0, "sentinel": 1, "bastion": 2}
# Kinds whose repeats within REPEAT_WINDOW_S become one "unstable" notice, and
# its detail. A rewritten system proxy or a failed start is reported each time.
_REPEAT_DETAIL = {"outage": "notice.unstable.detail.connection", "fallback": "notice.unstable.detail.tier"}
# tiers.summary offline_reason -> (quoted reason id, fix id) on an outage notice.
_OUTAGE_REASONS = {
    "source_supply_no_egress": ("notice.outage.reason.source_supply", "notice.outage.fix.source_supply"),
}
_MAX_PARAMS = 8


@dataclass(frozen=True)
class Facts:
    """One sample of what the overview shows (see ``facts_from``)."""

    run: str  # starting | running | stopping | stopped | failed | unknown
    generation: int = 0
    ready: bool = False
    carrier: str | None = None
    blocked: bool = False  # fail-closed readiness or route, or kill switch triggered
    triggered: bool = False  # the kill switch's firewall rules are blocking outbound traffic
    data_plane_failed: bool = False
    core_running: bool = True
    drift: bool = False  # the Windows system proxy no longer points at this Core
    offline_reason: str | None = None
    failure: Mapping[str, Any] | None = None


def facts_from(lifecycle: Any, tiers: Any = None, killswitch: Any = None,
               network: Any = None) -> Facts | None:
    """Facts from the bridge's public DTOs; None when no lifecycle sample exists."""
    if not isinstance(lifecycle, Mapping):
        return None
    runtime = _mapping(lifecycle.get("runtime"))
    intent = _mapping(lifecycle.get("intent"))
    readiness = _mapping(lifecycle.get("readiness"))
    freshness = _mapping(lifecycle.get("freshness"))
    core = _mapping(lifecycle.get("core"))
    run = _RUN_OF.get(runtime.get("startup_phase"), "unknown")
    failure = runtime.get("failure") if isinstance(runtime.get("failure"), Mapping) else None
    if run == "failed" and failure and "superseded" in (failure.get("code"), failure.get("reason_code")):
        run = "stopped"  # a stop that cancelled a start reads as stopped (overview S-R3)
    summary = _mapping(tiers)
    carrier = summary.get("carrier") if summary.get("carrier") in _RANK else None
    offline = summary.get("offline_reason")
    offline = offline if isinstance(offline, str) and offline else None
    ks = _mapping(killswitch)
    triggered = bool(ks.get("triggered", ks.get("active")))
    state = readiness.get("state")
    stale = bool(freshness.get("stale"))
    try:
        generation = int(intent.get("runtime_generation") or 0)
    except (TypeError, ValueError):
        generation = 0
    return Facts(
        run=run, generation=generation,
        ready=state in ("serving", "ready") and not stale,
        carrier=carrier,
        blocked=state == "fail_closed" or offline == "fail_closed" or triggered,
        triggered=triggered,
        data_plane_failed=state == "data_plane_failed",
        core_running=bool(core.get("running", True)),
        drift=bool(_mapping(network).get("drift")),
        offline_reason=offline,
        failure=failure,
    )


@dataclass(frozen=True)
class Notice:
    """One notice for ``alerts.desktop`` (PROTOCOL addition, plan §10.3)."""

    kind: str
    incident: str
    state: str  # open | update | resolved | closed
    group: str
    severity: str  # info | warn | error
    title_id: str
    detail_id: str | None = None
    reason_id: str | None = None
    fix_id: str | None = None
    params: Mapping[str, Any] = field(default_factory=dict)
    action: str | None = None
    target: str = "overview"
    sticky: bool = False
    ttl_ms: int = 0
    # A connection problem may ring; the owner ANDs this with the sound setting.
    audible: bool = False

    def wire(self, *, sound: bool) -> dict[str, Any]:
        """The ``notice`` object of an ``alerts.desktop`` frame."""
        data: dict[str, Any] = {
            "kind": self.kind, "incident": self.incident, "state": self.state, "group": self.group,
            "title_id": self.title_id, "params": dict(self.params),
            "sound": bool(sound and self.audible and self.state == "open"),
            "sticky": self.sticky, "ttl_ms": int(self.ttl_ms),
        }
        for name in ("detail_id", "reason_id", "fix_id", "action"):
            value = getattr(self, name)
            if value:
                data[name] = value
        return data


@dataclass
class _Incident:
    kind: str
    incident: str
    opened_at: float
    notice: Notice
    shown: bool = False


class IncidentTracker:
    """Turns ``Facts`` samples into ``Notice`` objects (see the module rules)."""

    def __init__(self) -> None:
        self._generation: int | None = None
        self._running_since: float | None = None
        self._armed = False
        self._baseline: int | None = None
        self._better: tuple[int, float] | None = None
        self._open: dict[str, _Incident] = {}
        self._pending: dict[str, float] = {}
        self._clearing: dict[str, float] = {}
        self._recent: dict[str, deque[float]] = {}
        self._muted_until: dict[str, float] = {}
        self._failed_generations: deque[int] = deque(maxlen=8)
        self._seq = 0

    # ------------------------------------------------------------ entry

    def observe(self, facts: Facts | None, now: float) -> list[Notice]:
        if facts is None:
            return []
        out: list[Notice] = []
        run = facts.run
        if run in ("starting", "running") and facts.generation != self._generation:
            # A new run: whatever is still open belonged to the previous one.
            out += self._close_all(keep=("start_failed",))
            self._new_run(facts.generation)
        if run in ("stopping", "stopped"):
            out += self._close_all()
            self._running_since = None
            self._armed = False
            return out
        if run == "failed":
            out += self._close_all(keep=("start_failed",))
            self._running_since = None
            self._armed = False
            out += self._start_failed(facts, now)
            return out
        if run != "running":
            return out
        if self._running_since is None:
            self._running_since = now
        if facts.ready and facts.carrier:
            out += self._resolve("start_failed", now, _resolve_start)
        timed_out = False
        if not self._armed:
            timed_out = now - self._running_since >= ARM_TIMEOUT_S
            if not ((facts.ready and facts.carrier) or timed_out):
                return out
            self._armed = True
            self._baseline = _RANK.get(facts.carrier) if facts.carrier else None
        out += self._outage(facts, now, timed_out)
        out += self._capture(facts, now, timed_out)
        out += self._fallback(facts, now)
        out += self._unmute(now)
        return out

    # ------------------------------------------------------------ run state

    def _new_run(self, generation: int) -> None:
        self._generation = generation
        self._running_since = None
        self._armed = False
        self._baseline = None
        self._better = None
        self._pending.clear()
        self._clearing.clear()

    def _close_all(self, keep: tuple[str, ...] = ()) -> list[Notice]:
        out = []
        for kind in list(self._open):
            if kind in keep:
                continue
            incident = self._open.pop(kind)
            if incident.shown:
                out.append(replace(incident.notice, state="closed", audible=False))
        for kind in list(self._pending):
            if kind not in keep:
                self._pending.pop(kind)
        for kind in list(self._clearing):
            if kind not in keep:
                self._clearing.pop(kind)
        return out

    # ------------------------------------------------------------ incidents

    def _step(self, kind: str, active: bool, now: float, hold: float, *,
              opened: bool, make, update=None, resolve=None) -> list[Notice]:
        """Open after ``hold``, update while open, resolve after CLEAR_HOLD_S."""
        incident = self._open.get(kind)
        if active:
            self._clearing.pop(kind, None)
            if incident is None:
                since = self._pending.setdefault(kind, now)
                if opened or now - since >= hold:
                    self._pending.pop(kind, None)
                    return self._open_incident(kind, now, make)
                return []
            return update(incident) if update is not None else []
        self._pending.pop(kind, None)
        if incident is None:
            return []
        since = self._clearing.setdefault(kind, now)
        if now - since < CLEAR_HOLD_S:
            return []
        self._clearing.pop(kind, None)
        return self._resolve(kind, now, resolve)

    def _open_incident(self, kind: str, now: float, make) -> list[Notice]:
        self._seq += 1
        incident_id = f"{kind}-{self._generation or 0}-{self._seq}"
        notice = make(incident_id)
        incident = _Incident(kind, incident_id, now, notice)
        self._open[kind] = incident
        if kind in _REPEAT_DETAIL:
            recent = self._recent.setdefault(kind, deque())
            while recent and now - recent[0] > REPEAT_WINDOW_S:
                recent.popleft()
            recent.append(now)
            if self._muted_until.get(kind, 0.0) > now:
                return []
            if len(recent) >= REPEAT_COUNT:
                # Repeated within the window: one "unstable" notice, then quiet.
                self._muted_until[kind] = now + REPEAT_WINDOW_S
                return [self._unstable(kind, notice.group, len(recent))]
        incident.shown = True
        return [notice]

    def _resolve(self, kind: str, now: float, resolve=None) -> list[Notice]:
        incident = self._open.pop(kind, None)
        if incident is None or not incident.shown:
            return []
        notice = resolve(incident, now) if resolve is not None else None
        return [notice] if notice is not None else []

    def _unmute(self, now: float) -> list[Notice]:
        out = []
        for kind, until in list(self._muted_until.items()):
            if until > now:
                continue
            del self._muted_until[kind]
            incident = self._open.get(kind)
            if incident is not None and not incident.shown:
                incident.shown = True
                out.append(incident.notice)
        return out

    def _unstable(self, kind: str, group: str, count: int) -> Notice:
        self._seq += 1
        return Notice(kind="unstable", incident=f"unstable-{kind}-{self._seq}", state="open",
                      group=group, severity="warn", title_id="notice.unstable.title",
                      detail_id=_REPEAT_DETAIL[kind],
                      params={"minutes": int(REPEAT_WINDOW_S // 60), "count": count},
                      action="view", ttl_ms=TTL_WARN_MS, audible=group == GROUP_CONNECTION)

    # ------------------------------------------------------------ kinds

    def _outage(self, facts: Facts, now: float, timed_out: bool) -> list[Notice]:
        active = (facts.blocked or facts.data_plane_failed or facts.carrier is None
                  or not facts.core_running)

        def detail() -> str:
            # "The kill switch is on" only when its rules are in place: a
            # fail-closed route alone (switch off, or no administrator
            # rights) blocks nothing outside the proxy.
            if facts.triggered:
                return "notice.outage.detail_blocked"
            if not facts.core_running:
                return "notice.outage.detail_core"
            return "notice.outage.detail"

        def make(incident_id: str) -> Notice:
            reason, fix = _OUTAGE_REASONS.get(facts.offline_reason or "", (None, None))
            return Notice(kind="outage", incident=incident_id, state="open", group=GROUP_CONNECTION,
                          severity="error", title_id="notice.outage.title", detail_id=detail(),
                          reason_id=reason, fix_id=fix, action="view", sticky=True, audible=True)

        def update(incident: _Incident) -> list[Notice]:
            current = detail()
            if current == incident.notice.detail_id:
                return []
            incident.notice = replace(incident.notice, detail_id=current)
            return [replace(incident.notice, state="update", audible=False)] if incident.shown else []

        def resolve(incident: _Incident, at: float) -> Notice:
            params: dict[str, Any] = {"minutes": max(0, int((at - incident.opened_at) // 60))}
            detail_id = None
            if facts.carrier:
                params["tier"] = _TIER_NAMES[facts.carrier]
                detail_id = "notice.outage.resolved.detail"
            return replace(incident.notice, state="resolved", severity="info",
                           title_id="notice.outage.resolved.title", detail_id=detail_id,
                           reason_id=None, fix_id=None, params=params, action=None,
                           sticky=False, ttl_ms=TTL_RESOLVED_MS, audible=False)

        return self._step("outage", active, now, OUTAGE_HOLD_S, opened=timed_out,
                          make=make, update=update, resolve=resolve)

    def _capture(self, facts: Facts, now: float, timed_out: bool) -> list[Notice]:
        def make(incident_id: str) -> Notice:
            return Notice(kind="capture_lost", incident=incident_id, state="open",
                          group=GROUP_CONNECTION, severity="error", title_id="notice.capture.title",
                          detail_id="notice.capture.detail", action="view", sticky=True, audible=True)

        def resolve(incident: _Incident, _at: float) -> Notice:
            return replace(incident.notice, state="resolved", severity="info",
                           title_id="notice.capture.resolved.title",
                           detail_id="notice.capture.resolved.detail", action=None,
                           sticky=False, ttl_ms=TTL_RESOLVED_MS, audible=False)

        return self._step("capture_lost", facts.drift, now, CAPTURE_HOLD_S, opened=timed_out,
                          make=make, resolve=resolve)

    def _fallback(self, facts: Facts, now: float) -> list[Notice]:
        if "outage" in self._open or "outage" in self._pending:
            return []  # an outage says more than any tier word; judge tiers once it is over
        rank = _RANK.get(facts.carrier) if facts.carrier else None
        if rank is None:
            return []
        if self._baseline is None:
            self._baseline = rank
        if rank < self._baseline:
            # A better tier becomes the run's normal once it has held.
            if self._better is None or self._better[0] != rank:
                self._better = (rank, now)
            elif now - self._better[1] >= BASELINE_HOLD_S:
                self._baseline, self._better = rank, None
        else:
            self._better = None
        normal = next(name for name, value in _RANK.items() if value == self._baseline)

        def make(incident_id: str) -> Notice:
            return _fallback_notice(incident_id, facts.carrier, normal)

        def update(incident: _Incident) -> list[Notice]:
            if incident.notice.params.get("tier") == _TIER_NAMES[facts.carrier]:
                return []
            incident.notice = _fallback_notice(incident.incident, facts.carrier, normal)
            return [replace(incident.notice, state="update")] if incident.shown else []

        def resolve(incident: _Incident, _at: float) -> Notice:
            return replace(incident.notice, state="resolved", severity="info",
                           title_id="notice.fallback.resolved.title", detail_id=None,
                           params={"tier": _TIER_NAMES[facts.carrier]}, action=None,
                           sticky=False, ttl_ms=TTL_RESOLVED_MS)

        return self._step("fallback", rank > self._baseline, now, FALLBACK_HOLD_S, opened=False,
                          make=make, update=update, resolve=resolve)

    def _start_failed(self, facts: Facts, now: float) -> list[Notice]:
        if facts.generation in self._failed_generations:
            # The kill switch fires a moment after the start fails (the next
            # state evaluation): the card on screen then says so, once.
            existing = self._open.get("start_failed")
            if (existing is not None and facts.triggered
                    and existing.notice.detail_id == "notice.start_failed.detail"):
                existing.notice = replace(existing.notice, detail_id="notice.outage.detail_blocked")
                return [replace(existing.notice, state="update", audible=False)] if existing.shown else []
            return []
        self._failed_generations.append(facts.generation)
        failure = _mapping(facts.failure)
        key = failure.get("message_key")
        detail = key if isinstance(key, str) and key.startswith("errors.start.") else "notice.start_failed.detail"
        if detail == "notice.start_failed.detail" and facts.triggered:
            # No reason of its own to give: say that outbound traffic is blocked.
            detail = "notice.outage.detail_blocked"
        params = _scalar_params(failure.get("params")) if detail.startswith("errors.start.") else {}

        def make(incident_id: str) -> Notice:
            return Notice(kind="start_failed", incident=incident_id, state="open",
                          group=GROUP_CONNECTION, severity="error", title_id="notice.start_failed.title",
                          detail_id=detail, params=params, action="view", sticky=True, audible=True)

        self._generation = facts.generation
        existing = self._open.get("start_failed")
        if existing is not None:
            # A retry failed again: the card still on screen takes the new reason.
            existing.notice = make(existing.incident)
            return [replace(existing.notice, state="update", audible=False)] if existing.shown else []
        return self._open_incident("start_failed", now, make)


def _resolve_start(incident: _Incident, _at: float) -> Notice:
    """The recovery of a failed start (a later run became ready)."""
    return replace(incident.notice, state="resolved", severity="info",
                   title_id="notice.start_failed.resolved.title", detail_id=None, params={},
                   action=None, sticky=False, ttl_ms=TTL_RESOLVED_MS, audible=False)


def _fallback_notice(incident_id: str, carrier: str, normal: str) -> Notice:
    if carrier == "bastion":
        return Notice(kind="fallback", incident=incident_id, state="open", group=GROUP_TIER,
                      severity="warn", title_id="notice.fallback.title",
                      detail_id="notice.fallback.detail.bastion", params={"tier": _TIER_NAMES[carrier]},
                      action="view", sticky=True)
    return Notice(kind="fallback", incident=incident_id, state="open", group=GROUP_TIER,
                  severity="warn", title_id="notice.fallback.title",
                  detail_id="notice.fallback.detail.other",
                  params={"tier": _TIER_NAMES[carrier], "from": _TIER_NAMES[normal]},
                  action="view", ttl_ms=TTL_WARN_MS)


def notify_settings(alerts_config: Any) -> dict[str, Any]:
    """``alerts.notify`` with its defaults: on, every group, sound on.

    ``events`` keeps NOTICE_GROUPS order and drops unknown names; a value that
    is not a list falls back to every group. Shared by the notice owner, the
    settings validator and the config snapshot so the three never disagree.
    """
    block = _mapping(_mapping(alerts_config).get("notify"))
    events = block.get("events", NOTICE_GROUPS)
    if not isinstance(events, (list, tuple)):
        events = NOTICE_GROUPS
    return {
        "enabled": _flag(block.get("enabled"), True),
        "events": [group for group in NOTICE_GROUPS if group in events],
        "sound": _flag(block.get("sound"), True),
    }


def _flag(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false", "yes", "no", "on", "off", "1", "0"):
        return value.strip().lower() in ("true", "yes", "on", "1")
    if isinstance(value, int):
        return bool(value)
    return default


def update_notice(latest: str, current: str, incident: str) -> Notice:
    return Notice(kind="update", incident=incident, state="open", group=GROUP_UPDATE, severity="info",
                  title_id="notice.update.title", detail_id="notice.update.detail",
                  params={"latest": latest, "current": current}, action="view", target="update",
                  ttl_ms=TTL_INFO_MS)


def preview_notice(incident: str) -> Notice:
    return Notice(kind="preview", incident=incident, state="open", group=GROUP_PREVIEW, severity="info",
                  title_id="notice.preview.title", detail_id="notice.preview.detail",
                  ttl_ms=TTL_INFO_MS)


# ---------------------------------------------------------------- fallback text
#
# Cards and in-app notices render the message ids above through the UI
# catalogs (ui/src/i18n/locales/*/notice.json). ``title`` / ``detail`` of the
# frame only feed the native tray balloon, which the shell uses when it cannot
# show a card; they are therefore short, plain and limited to zh / en.

_TEXT = {
    "zh_CN": {
        "notice.outage.title": "连接已中断",
        "notice.outage.detail": "所有层级都不可用，经代理的连接暂时无法建立。",
        "notice.outage.detail_blocked": "所有层级都不可用。熔断已开启，出站已阻断。",
        "notice.outage.detail_core": "代理核心没有在运行，自动重启没有成功。",
        "notice.outage.resolved.title": "连接已恢复",
        "notice.outage.resolved.detail": "当前经 {tier}。",
        "notice.capture.title": "系统代理被其他程序改写",
        "notice.capture.detail": "流量不再经过 Tesseract。重启服务可以重新接管。",
        "notice.capture.resolved.title": "系统代理已恢复",
        "notice.capture.resolved.detail": "流量重新经过 Tesseract。",
        "notice.start_failed.title": "服务未能启动",
        "notice.start_failed.detail": "打开 Tesseract 查看原因。",
        "notice.start_failed.resolved.title": "服务已启动",
        "notice.fallback.title": "已回退到 {tier}",
        "notice.fallback.detail.bastion": "Bastion 使用公共节点，请勿登录任何账号。",
        "notice.fallback.detail.other": "{from} 暂时不可用。",
        "notice.fallback.resolved.title": "已回到 {tier}",
        "notice.unstable.title": "连接不稳定",
        "notice.unstable.detail.connection": "{minutes} 分钟内中断了 {count} 次。",
        "notice.unstable.detail.tier": "{minutes} 分钟内切换了 {count} 次层级。",
        "notice.update.title": "发现新版本 {latest}",
        "notice.update.detail": "当前 {current}。",
        "notice.preview.title": "提醒已开启",
        "notice.preview.detail": "这是一条预览提醒。",
    },
    "en_US": {
        "notice.outage.title": "Connection lost",
        "notice.outage.detail": "No tier is available. Proxied connections cannot be made right now.",
        "notice.outage.detail_blocked": "No tier is available. The kill switch is on and outbound traffic is blocked.",
        "notice.outage.detail_core": "The proxy core is not running and could not be restarted.",
        "notice.outage.resolved.title": "Connection restored",
        "notice.outage.resolved.detail": "Now through {tier}.",
        "notice.capture.title": "Another program changed the system proxy",
        "notice.capture.detail": "Traffic no longer goes through Tesseract. Restart the service to take it back.",
        "notice.capture.resolved.title": "System proxy restored",
        "notice.capture.resolved.detail": "Traffic goes through Tesseract again.",
        "notice.start_failed.title": "The service could not start",
        "notice.start_failed.detail": "Open Tesseract to see why.",
        "notice.start_failed.resolved.title": "The service started",
        "notice.fallback.title": "Fell back to {tier}",
        "notice.fallback.detail.bastion": "Bastion uses public nodes. Do not sign in to any account.",
        "notice.fallback.detail.other": "{from} is unavailable for now.",
        "notice.fallback.resolved.title": "Back on {tier}",
        "notice.unstable.title": "Connection unstable",
        "notice.unstable.detail.connection": "Lost {count} times in {minutes} minutes.",
        "notice.unstable.detail.tier": "Switched tiers {count} times in {minutes} minutes.",
        "notice.update.title": "Version {latest} is available",
        "notice.update.detail": "You have {current}.",
        "notice.preview.title": "Notices are on",
        "notice.preview.detail": "This is a preview notice.",
    },
}


class _Missing(dict):
    def __missing__(self, key: str) -> str:
        return "—"


def fallback_text(notice: Notice, language: str) -> tuple[str, str]:
    """Plain (title, detail) for the native balloon, in zh_CN or English."""
    table = _TEXT["zh_CN"] if str(language or "").startswith("zh") else _TEXT["en_US"]
    params = _Missing({key: str(value) for key, value in notice.params.items()})

    def text(message_id: str | None, default: str = "") -> str:
        template = table.get(message_id or "")
        if template is None:
            return default
        try:
            return template.format_map(params)
        except (ValueError, IndexError):
            return template

    title = text(notice.title_id, "Tesseract")
    if notice.title_id == "notice.outage.resolved.title" and int(notice.params.get("minutes") or 0) > 0:
        title += (f" · 中断 {notice.params['minutes']} 分钟" if table is _TEXT["zh_CN"]
                  else f" · down {notice.params['minutes']} min")
    detail = text(notice.detail_id, text("notice.start_failed.detail") if notice.kind == "start_failed" else "")
    return title, detail


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _scalar_params(value: Any) -> dict[str, Any]:
    """At most ``_MAX_PARAMS`` str / int params (lists are joined), keys as given."""
    result: dict[str, Any] = {}
    for key, item in _mapping(value).items():
        if len(result) >= _MAX_PARAMS:
            break
        if isinstance(item, bool) or not isinstance(key, str):
            continue
        if isinstance(item, (str, int)):
            result[key] = item
        elif isinstance(item, (list, tuple)) and all(isinstance(entry, (str, int)) for entry in item):
            result[key] = ", ".join(str(entry) for entry in item)
    return result
