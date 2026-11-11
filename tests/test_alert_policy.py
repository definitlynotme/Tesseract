"""alert_policy: which runtime facts become a desktop notice, and when."""

from __future__ import annotations

import alert_policy as policy
from alert_policy import Facts, IncidentTracker, facts_from, fallback_text


def running(carrier="vanguard", *, ready=True, generation=1, **extra):
    return Facts(run="running", generation=generation, ready=ready, carrier=carrier, **extra)


def feed(tracker, timeline):
    """timeline: [(t_start, t_end, facts)], sampled once a second; returns [(t, notice)]."""
    out = []
    for start, end, facts in timeline:
        for t in range(start, end):
            out += [(t, notice) for notice in tracker.observe(facts, float(t))]
    return out


def kinds(events):
    return [(notice.kind, notice.state) for _t, notice in events]


def test_users_real_session_shape_is_silent():
    # 68 live sessions: offline at ~1 s, Bastion at ~5–25 s, then steady on Bastion,
    # one Vanguard node down and no admin rights for the kill switch. None was an incident.
    tracker = IncidentTracker()
    events = feed(tracker, [
        (0, 8, Facts(run="starting", generation=7)),
        (8, 26, running(None, ready=False, generation=7)),
        (26, 400, running("bastion", generation=7)),
        (400, 402, Facts(run="stopping", generation=7)),
        (402, 405, Facts(run="stopped", generation=7)),
    ])
    assert events == []


def test_outage_after_ready_opens_after_hold_and_resolves_in_place():
    tracker = IncidentTracker()
    events = feed(tracker, [
        (0, 30, running("vanguard")),
        (30, 230, running(None)),
        (230, 240, running("sentinel")),
    ])
    assert kinds(events) == [("outage", "open"), ("outage", "resolved")]
    (opened_at, opened), (resolved_at, resolved) = events
    assert opened_at == 50  # OUTAGE_HOLD_S after the route went away
    assert opened.severity == "error" and opened.sticky and opened.audible
    assert opened.detail_id == "notice.outage.detail"
    assert resolved_at == 235  # CLEAR_HOLD_S after it came back
    assert resolved.incident == opened.incident
    assert resolved.title_id == "notice.outage.resolved.title"
    assert resolved.params == {"minutes": 3, "tier": "Sentinel"}
    assert not resolved.sticky and resolved.ttl_ms == policy.TTL_RESOLVED_MS


def test_short_outage_is_never_reported():
    tracker = IncidentTracker()
    assert feed(tracker, [(0, 30, running()), (30, 45, running(None)), (45, 90, running())]) == []


def test_start_still_offline_after_arm_timeout_is_reported_at_once():
    tracker = IncidentTracker()
    events = feed(tracker, [(0, 5, Facts(run="starting")), (5, 140, running(None, ready=False))])
    assert kinds(events) == [("outage", "open")]
    assert events[0][0] == 5 + int(policy.ARM_TIMEOUT_S)


def test_kill_switch_during_an_outage_updates_the_same_card():
    tracker = IncidentTracker()
    events = feed(tracker, [
        (0, 10, running()),
        (10, 40, running(None)),
        (40, 50, running(None, blocked=True, triggered=True)),
    ])
    assert kinds(events) == [("outage", "open"), ("outage", "update")]
    assert events[1][1].detail_id == "notice.outage.detail_blocked"
    assert events[1][1].incident == events[0][1].incident
    assert not events[1][1].audible


def test_fail_closed_route_without_the_kill_switch_does_not_claim_it():
    # Kill switch off (the shipped default) or no administrator rights: the
    # route is fail-closed, yet no firewall rule blocks anything.
    tracker = IncidentTracker()
    events = feed(tracker, [(0, 10, running()), (10, 40, running(None, blocked=True))])
    assert kinds(events) == [("outage", "open")]
    assert events[0][1].detail_id == "notice.outage.detail"


def test_failed_start_with_the_kill_switch_triggered_says_outbound_is_blocked():
    tracker = IncidentTracker()
    # The switch fires on the state evaluation after the start failed.
    events = feed(tracker, [
        (0, 2, Facts(run="starting", generation=1)),
        (2, 4, Facts(run="failed", generation=1, failure={"code": "internal"})),
        (4, 9, Facts(run="failed", generation=1, failure={"code": "internal"}, blocked=True, triggered=True)),
    ])
    assert kinds(events) == [("start_failed", "open"), ("start_failed", "update")]
    opened, updated = (notice for _t, notice in events)
    assert opened.detail_id == "notice.start_failed.detail"
    assert updated.detail_id == "notice.outage.detail_blocked" and updated.incident == opened.incident
    assert not updated.audible
    # Already triggered when the failure is first seen: one card, no update.
    tracker = IncidentTracker()
    events = feed(tracker, [(0, 5, Facts(run="failed", generation=1, blocked=True, triggered=True))])
    assert [(n.kind, n.state, n.detail_id) for _t, n in events] == [
        ("start_failed", "open", "notice.outage.detail_blocked")]
    # A start that failed for a reason of its own keeps that reason.
    tracker = IncidentTracker()
    failure = {"code": "io", "message_key": "errors.start.listener_conflict", "params": {"count": 2}}
    events = feed(tracker, [(0, 5, Facts(run="failed", generation=1, failure=failure, triggered=True))])
    assert [(n.state, n.detail_id) for _t, n in events] == [("open", "errors.start.listener_conflict")]


def test_core_down_without_restart_names_the_core():
    tracker = IncidentTracker()
    events = feed(tracker, [(0, 10, running()), (10, 40, running(core_running=False))])
    assert kinds(events) == [("outage", "open")]
    assert events[0][1].detail_id == "notice.outage.detail_core"


def test_outage_reason_from_offline_reason():
    tracker = IncidentTracker()
    events = feed(tracker, [(0, 10, running()),
                            (10, 40, running(None, offline_reason="source_supply_no_egress"))])
    notice = events[0][1]
    assert (notice.reason_id, notice.fix_id) == ("notice.outage.reason.source_supply",
                                                 "notice.outage.fix.source_supply")


def test_system_proxy_rewritten_opens_and_resolves():
    tracker = IncidentTracker()
    events = feed(tracker, [
        (0, 10, running()),
        (10, 30, running(drift=True)),
        (30, 40, running()),
    ])
    assert kinds(events) == [("capture_lost", "open"), ("capture_lost", "resolved")]
    assert events[0][0] == 20 and events[0][1].audible
    assert events[1][1].title_id == "notice.capture.resolved.title"


def test_fallback_against_the_runs_own_normal_tier():
    tracker = IncidentTracker()
    events = feed(tracker, [
        (0, 10, running("vanguard")),
        (10, 50, running("sentinel")),
        (50, 60, running("bastion")),
        (60, 70, running("vanguard")),
    ])
    assert kinds(events) == [("fallback", "open"), ("fallback", "update"), ("fallback", "resolved")]
    opened, deeper, back = (notice for _t, notice in events)
    assert events[0][0] == 40
    assert opened.params == {"tier": "Sentinel", "from": "Vanguard"}
    assert opened.detail_id == "notice.fallback.detail.other" and not opened.sticky
    assert deeper.params == {"tier": "Bastion"} and deeper.sticky
    assert deeper.detail_id == "notice.fallback.detail.bastion"
    assert back.title_id == "notice.fallback.resolved.title" and back.params == {"tier": "Vanguard"}
    assert not any(notice.audible for _t, notice in events)


def test_bastion_as_normal_tier_is_quiet_until_a_better_tier_held():
    tracker = IncidentTracker()
    events = feed(tracker, [
        (0, 100, running("bastion")),
        (100, 130, running("vanguard")),  # not held for BASELINE_HOLD_S yet
        (130, 170, running("bastion")),
        (170, 240, running("vanguard")),  # held: Vanguard is now this run's normal
        (240, 280, running("bastion")),
    ])
    assert kinds(events) == [("fallback", "open")]
    assert events[0][0] == 270


def test_gateway_is_never_a_fallback():
    tracker = IncidentTracker()
    assert feed(tracker, [(0, 10, running("vanguard")), (10, 100, running("gateway"))]) == []


def test_repeated_outage_becomes_one_unstable_notice_then_quiet():
    tracker = IncidentTracker()
    events = feed(tracker, [
        (0, 10, running()),
        (10, 40, running(None)),     # outage 1 opens at 30
        (40, 100, running()),        # resolves at 45
        (100, 130, running(None)),   # outage 2 opens at 120: still reported
        (130, 200, running()),       # resolves at 135
        (200, 230, running(None)),   # outage 3 at 220: third in the window -> unstable
        (230, 300, running()),       # muted: no recovery notice
        (300, 330, running(None)),   # outage 4: muted
        (330, 900, running(None)),   # still down when the mute ends -> reported then
    ])
    assert kinds(events) == [("outage", "open"), ("outage", "resolved"),
                             ("outage", "open"), ("outage", "resolved"),
                             ("unstable", "open"), ("outage", "open")]
    unstable = events[4][1]
    assert unstable.detail_id == "notice.unstable.detail.connection"
    assert unstable.params == {"minutes": 10, "count": policy.REPEAT_COUNT}
    assert events[5][0] == 220 + int(policy.REPEAT_WINDOW_S)


def test_start_failure_reports_reason_updates_on_retry_and_resolves():
    failure = {"code": "io", "message_key": "errors.start.listener_conflict",
               "params": {"endpoints": ["127.0.0.1:7890", "127.0.0.1:9090"], "count": 2, "flag": True},
               "reason_code": "runtime_listener_conflict"}
    tracker = IncidentTracker()
    events = feed(tracker, [
        (0, 3, Facts(run="starting", generation=1)),
        (3, 10, Facts(run="failed", generation=1, failure=failure)),
        (10, 12, Facts(run="starting", generation=2)),
        (12, 20, Facts(run="failed", generation=2, failure={"code": "internal"})),
        (20, 22, Facts(run="starting", generation=3)),
        (22, 30, running(generation=3)),
    ])
    assert kinds(events) == [("start_failed", "open"), ("start_failed", "update"),
                             ("start_failed", "resolved")]
    opened, retry, resolved = (notice for _t, notice in events)
    assert opened.detail_id == "errors.start.listener_conflict"
    assert opened.params == {"endpoints": "127.0.0.1:7890, 127.0.0.1:9090", "count": 2}
    assert retry.detail_id == "notice.start_failed.detail" and retry.incident == opened.incident
    assert resolved.title_id == "notice.start_failed.resolved.title"


def test_user_stop_closes_open_incidents_silently():
    tracker = IncidentTracker()
    events = feed(tracker, [(0, 10, running()), (10, 40, running(None)),
                            (40, 41, Facts(run="stopping")), (41, 50, Facts(run="stopped"))])
    assert kinds(events) == [("outage", "open"), ("outage", "closed")]
    assert not events[1][1].audible


def test_facts_from_overview_dtos():
    lifecycle = {
        "intent": {"runtime_generation": 4, "stop_requested": False},
        "runtime": {"startup_phase": "running", "failure": None},
        "core": {"running": True},
        "readiness": {"state": "serving"},
        "freshness": {"stale": False},
    }
    facts = facts_from(lifecycle, {"carrier": "sentinel", "offline_reason": None},
                       {"enabled": True, "active": False, "admin": False}, {"drift": True})
    assert facts == Facts(run="running", generation=4, ready=True, carrier="sentinel",
                          blocked=False, data_plane_failed=False, core_running=True, drift=True)
    stale = dict(lifecycle, freshness={"stale": True})
    assert facts_from(stale).ready is False
    blocked = facts_from(lifecycle, {"carrier": None, "offline_reason": "fail_closed"},
                         {"enabled": True, "triggered": True})
    assert blocked.blocked and blocked.triggered and blocked.carrier is None
    fail_closed = facts_from(lifecycle, {"carrier": None, "offline_reason": "fail_closed"},
                             {"enabled": False, "active": False})
    assert fail_closed.blocked and not fail_closed.triggered
    superseded = dict(lifecycle, runtime={"startup_phase": "failed",
                                          "failure": {"code": "superseded", "reason_code": "superseded"}})
    assert facts_from(superseded).run == "stopped"
    assert facts_from(None) is None


def test_wire_and_fallback_text():
    tracker = IncidentTracker()
    events = feed(tracker, [(0, 10, running()), (10, 40, running(None))])
    notice = events[0][1]
    wire = notice.wire(sound=True)
    assert wire["sound"] is True and wire["sticky"] is True and wire["state"] == "open"
    assert wire["title_id"] == "notice.outage.title" and "reason_id" not in wire
    assert notice.wire(sound=False)["sound"] is False
    assert fallback_text(notice, "zh_CN") == ("连接已中断", "所有层级都不可用，经代理的连接暂时无法建立。")
    resolved = feed(tracker, [(40, 200, running(None)), (200, 210, running("bastion"))])[-1][1]
    assert resolved.wire(sound=True)["sound"] is False
    assert fallback_text(resolved, "zh_CN") == ("连接已恢复 · 中断 2 分钟", "当前经 Bastion。")
    assert fallback_text(resolved, "de_DE")[0] == "Connection restored · down 2 min"
    update = policy.update_notice("0.2.0", "0.1.0", "update-0.2.0")
    assert fallback_text(update, "zh_CN") == ("发现新版本 0.2.0", "当前 0.1.0。")
