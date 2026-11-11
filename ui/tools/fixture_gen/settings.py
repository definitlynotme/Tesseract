"""Generate the T-P4-05d scenarios (ui/test/fixtures/scenarios/settings_*.json).

Run: .venv/Scripts/python.exe -B ui/tools/fixture_gen/settings.py [<ui dir>]
Every scenario: the shell's rt:reset + a v1 hello (page commands and queries the settings page
uses; ui.language NOT declared: the shell does not forward it, so a language save waits 3 s and
says to reopen the window, critique B3) + the runtime topics the page reads + on_invoke answers
for the queries the page sends on enter (config.snapshot, autostart.state, app.info, geo.status,
alerts.catalog). Data is ASCII except the sidecar's masked source hint (the bullet).
"""
# Collected into ui/tools/fixture_gen/ by T-P4-12 (第八波裁定 05a-§11.5); usage and state: ui/README.md.
import copy
import json
import sys
from pathlib import Path

UI = Path(sys.argv[1]) if __name__ == "__main__" and len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
OUT = UI / "test" / "fixtures" / "scenarios"
EP = "rt-fixture-e1"
T0 = 1790000000000


def base_frame(**kw):
    f = {"epoch": EP, "produced_at_ms": T0, "sent_at_ms": T0 + 1}
    f.update(kw)
    return f


def shell_frame(**kw):
    """rt:* / host:* frames: the shell's own, no sidecar time fields"""
    f = {"epoch": EP}
    f.update(kw)
    return f


def evt(topic, data, kind="snapshot", rev=1, **kw):
    return base_frame(t="evt", topic=topic, rev=rev, kind=kind, data=data, **kw)


COMMANDS = ["alerts.catalog", "app.info", "app.update.check", "app.update.install", "autostart.state",
            "config.snapshot", "geo.status", "geo.update", "mode.set", "service.restart",
            "service.start", "service.stop", "settings.apply"]
TOPICS = ["bastion.pool", "bastion.sources", "config.changed", "core.info", "geo.status", "jobs",
          "killswitch.state", "network.mode", "nodes.list", "route.published", "service.state",
          "tiers.summary", "update.state"]


def hello(commands=None, topics=None, features=None):
    return base_frame(t="hello", proto=1, app_version="1.0.0", runtime_pid=4381, lang="zh_CN",
                      caps={"commands": sorted(commands or COMMANDS), "topics": sorted(topics or TOPICS), "features": features or []},
                      scope={"kind": "custom", "id": "rt-fixture"},
                      network_recovery={"proxy": {"state": "restored", "error_code": None, "remaining_count": 0},
                                        "firewall": {"state": "not_needed", "error_code": None, "remaining_count": 0}})


PHASE = {"running": ("running", True, True), "starting": ("collecting", True, False), "stopped": ("stopped", False, False)}


def service_state(run="running", rev=1, kind="snapshot"):
    phase, rt_running, core_running = PHASE[run]
    return evt("service.state", {
        "intent": {"desired_running": run != "stopped", "stop_requested": False, "lifecycle_revision": 12, "runtime_generation": 4},
        "runtime": {"runtime_running": rt_running, "startup_phase": phase, "shutdown_cleanup_pending": False, "failure_code": None},
        "core": {"running": core_running, "pid": 7654 if core_running else None, "process_generation": 4, "core_generation": 8, "cleanup_pending": False},
        "readiness": {"state": "ready", "validation_active": False, "reason": "", "remaining_ms": None, "publication_revision": 36,
                      "lifecycle_revision": 12, "route_epoch": 7, "network_context_epoch": 2},
        "freshness": {"sampled_at_ms": T0, "snapshot_revision": 51, "stale": False, "uptime_s": 120 if run == "running" else None}}, rev=rev, kind=kind)


def tiers_summary():
    return evt("tiers.summary", {"carrier": "vanguard", "offline_reason": None, "order": ["vanguard", "sentinel", "bastion", "gateway"],
                                 "degraded": False, "fallback": False,
                                 "tiers": {"vanguard": {"enabled": True, "total": 3, "usable": 2}, "sentinel": {"enabled": True, "total": 2, "usable": 1},
                                           "bastion": {"enabled": True, "total": 40, "usable": 25}, "gateway": {"enabled": False, "total": 0, "usable": 0}},
                                 "pool": {"usable": 5, "total": 7}})


def node(i, tier, lat):
    return {"id": f"{tier}-{i}", "name": f"{tier}-node-{i}", "tier": tier, "kind": "manual", "enabled": True,
            "status": "healthy" if lat is not None else "unknown", "reason_code": None, "protocol": "vless", "latency_ms": lat}


def nodes_list(tier, lats):
    return evt("nodes.list", {"items": [node(i, tier, l) for i, l in enumerate(lats)]}, key=tier)


def snapshot_values(**over):
    v = {
        "network": {"allow_lan": False, "ipv6": True, "mihomo_mixed_port": 7890, "mode": "system",
                    "system_proxy": {"bypass": ["localhost", "127.*", "192.168.*"]},
                    "subscription": {"port": 25500},
                    "egress_recovery": {"enabled": True, "retry_source_refresh": False}},
        "dns": {"mode": "custom", "respect_rules": True, "nameserver": ["https://dns.example.net/dns-query"],
                "proxy_server_nameserver": ["tls://dot.example.net"], "default_nameserver": ["192.0.2.53"]},
        "routing": {"rules": {"direct": ["suffix:example.cn", "geoip:CN"], "proxy": ["suffix:example.io"], "reject": ["keyword:ads"],
                              "compatibility_defaults": [{"rule_type": "direct", "value": "suffix:example.com"}]},
                    # 配置整理 P2: the one list and the rule sets the sidecar projects (routing_rules.
                    # effective_rule_list / provider_rows of the lists above)
                    "rule_list": [{"kind": "suffix", "value": "example.cn", "to": "direct"}, {"kind": "geoip", "value": "CN", "to": "direct"},
                                  {"kind": "suffix", "value": "example.io", "to": "proxy"}, {"kind": "keyword", "value": "ads", "to": "reject"}],
                    "providers": []},
        "latency": {"primary_gateway_max_latency_ms": 800},
        "sources": [{"id": "src-gh-1", "kind": "github_repos", "display": "GitHub", "enabled": True, "hint": "e\u2022\u2022\u2022/f\u2022\u2022\u2022"},
                    {"id": "src-sub-1", "kind": "subscription_urls", "display": "Subscription", "enabled": True}],
        "alerts": {"channels": []},
        "ui": {"language": "zh_CN", "overview_auto_exit_lookup": True, "start_in_tray_on_autostart": False},
        "autostart": {"enabled": True},
        "killswitch": {"enabled": True, "allow_lan": True, "allow_dns": True, "dns_whitelist": ["192.0.2.53"],
                       "additional_whitelist": ["LocalSubnet", "203.0.113.5"]},
        "bastion": {"enabled": True, "min_alive_nodes": 5},
    }
    for k, val in over.items():
        v[k] = val
    return v


def res(cmd, data=None, error=None, rid=1):
    f = base_frame(t="res", id=rid, req_id="$req_id", owner="$owner", cmd=cmd, ok=error is None)
    if error is None:
        f["data"] = data
    else:
        f["error"] = error
    return f


def err(code, key, reason=None, field=None, hold=None, params=None):
    e = {"code": code, "message_key": key, "params": params or {}}
    if reason is not None:
        e["reason_code"] = reason
    if field is not None:
        e["field"] = field
    if hold is not None:
        e["hold"] = hold
    return e


def on(cmd, reply=None, reject=None, args=None):
    s = {"on_invoke": {"cmd": cmd}}
    if args is not None:
        s["on_invoke"]["args"] = args
    if reply is not None:
        s["reply"] = reply
    if reject is not None:
        s["reject"] = reject
    return s


def job(name, op, seq, result=None, error=None, final="done", rev0=None):
    """ack + jobs queued / running / final (seq, seq+1, seq+2)."""
    rev0 = seq if rev0 is None else rev0
    ack = base_frame(t="ack", id=2, req_id="$req_id", owner="$owner", accepted=True, op=op)

    def edge(state, i, res_=None, er=None):
        return evt("jobs", {"op": op, "req_id": "$req_id", "owner": "$owner", "name": name, "state": state,
                            "started_at_ms": None if state == "queued" else T0, "progress": None,
                            "result": res_, "error": er}, kind="edge", rev=rev0 + i, seq=seq + i)
    return [ack, edge("queued", 0), edge("running", 1), edge(final, 2, result, error)]


def section(name, saved=True, applied=True, effect="none", error=None):
    return {"section": name, "saved": saved, "applied": applied, "effect": effect, "error": error}


def config_changed(rev, sections, pending=None, seq=1):
    return evt("config.changed", {"rev": rev, "files": ["config"], "sections": sections, "pending": pending,
                                  "external_detection": "bridge_writes_only"}, kind="edge", rev=seq, seq=seq)


def pending(state, effect="reload", error=None, rev=8):
    return {"intent_id": "intent-1", "config_rev": rev, "generation": 4, "state": state, "effect": effect, "error": error}


def update_state(**kw):
    u = {"state": "idle", "version": None, "update_available": False, "installable": False, "error": None,
         "current_version": "1.0.0", "previous_version": None}
    u.update(kw)
    return evt("update.state", u)


def geo(running=False, updated=T0 - 3600000, errors=None, items=None):
    g = {"running": running, "updated_at_ms": updated, "errors": errors or []}
    if items is not None:
        g["items"] = items
    return g


def scenario(name, desc, *, run="running", values=None, secret_fields=None, read_errors=None, snap_pending=None,
             snapshot_reject=None, autostart=None, autostart_error=None, mode=None, ks=None, pool=None,
             upd=None, geo_status=None, core_version="1.19.27", extra_steps=(), commands=None, topics=None,
             nodes=True, rev=7, second_snapshot=None, features=None, visits=1):
    steps = [
        {"deliver": shell_frame(t="rt:reset", reason="reconnect")},
        {"deliver": hello(commands, topics, features)},
        {"deliver": service_state(run)},
        {"deliver": tiers_summary()},
        {"deliver": evt("network.mode", mode or {"mode": "system", "admin": True, "switching": False, "proxy_state": "owned", "drift": False})},
        {"deliver": evt("killswitch.state", ks or {"enabled": True, "active": False, "admin": True, "error": None, "triggered": False})},
        {"deliver": evt("core.info", {"running": run == "running", "pid": 7654 if run == "running" else None, "version": core_version,
                                      "generation": 8, "cleanup_pending": False})},
        {"deliver": upd or update_state()},
        {"deliver": evt("geo.status", geo_status or geo())},
    ]
    if run != "stopped":
        steps.append({"deliver": evt("bastion.pool", pool or {"runtime_alive": 18, "user_alive": 2, "capacity": 100, "min_alive_nodes": 5,
                                                              "target_alive": 30, "collecting": True, "observed_at_ms": T0})})
    vals = values if values is not None else snapshot_values()
    if "sources" in vals:
        steps.append({"deliver": evt("bastion.sources", {"items": copy.deepcopy(vals["sources"])})})
    if nodes:
        steps += [{"deliver": nodes_list("vanguard", [120, 480, None])}, {"deliver": nodes_list("sentinel", [260, 900])},
                  {"deliver": nodes_list("gateway", [])}]
    steps += [{"deliver": shell_frame(t="host:language", language="zh_CN")}, {"deliver": shell_frame(t="host:visibility", visible=True)}]
    snap = {"rev": rev, "values": vals, "secret_fields_set": secret_fields or [], "read_errors": read_errors or [], "pending": snap_pending}
    for _visit in range(visits):
        if snapshot_reject:
            steps.append(on("config.snapshot", reply=[res("config.snapshot", error=snapshot_reject)]))
        else:
            steps.append(on("config.snapshot", reply=[res("config.snapshot", snap)]))
        if second_snapshot is not None:
            s2 = copy.deepcopy(snap)
            s2.update(second_snapshot)
            steps.append(on("config.snapshot", reply=[res("config.snapshot", s2, rid=3)]))
        a = autostart if autostart is not None else {"supported": True, "enabled": True, "target": "shell", "mismatch": False, "error": None}
        if autostart_error:
            steps.append(on("autostart.state", reply=[res("autostart.state", error=autostart_error)]))
        else:
            steps.append(on("autostart.state", reply=[res("autostart.state", a)]))
        steps.append(on("app.info", reply=[res("app.info", {"version": "1.0.0", "language": "zh_CN", "caps": [],
                                                            "listeners": [{"kind": "mixed", "host": "127.0.0.1", "port": 7890, "available": True},
                                                                          {"kind": "subscription", "host": "127.0.0.1", "port": 25500, "available": True}]})]))
        steps.append(on("geo.status", reply=[res("geo.status", geo_status or geo())]))
        steps.append(on("alerts.catalog", reply=[res("alerts.catalog", {"channels": [], "event_groups": [], "scopes": [], "acl": {}})]))
    steps += list(extra_steps)
    return {"name": name, "desc": desc, "lang": "zh_CN", "steps": steps}


def write(sc):
    p = OUT / f"{sc['name']}.json"
    p.write_bytes((json.dumps(sc, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return p.name


ALL = []


def add(name, desc, **kw):
    ALL.append(scenario(name, desc, **kw))


def apply_job(result=None, error=None, final=None, seq=1):
    final = final or ("failed" if error else "done")
    return job("settings.apply", "op-save-1", seq, result=result, error=error, final=final)


# ---- the page --------------------------------------------------------------------------------
add("settings_page_running", "T-P4-05d: the whole settings page from config.snapshot while the service runs (every section, runtime numbers from bastion.pool, the latency preview from nodes.list)")
add("settings_page_stopped", "T-P4-05d: the settings page while the service is stopped (no bastion.pool baseline: the pool numbers are unknown; effect tags say next start)", run="stopped")
add("settings_page_starting", "T-P4-05d: the settings page while the service is starting (collecting counts as running: PROTOCOL 15)", run="starting")

# S-01 the third result: saved, not applied (restart failed) - and 重试应用 restarts the service
add("settings_save_not_applied", "S-01: settings.apply fails with every section saved but network not applied (the Core restart failed): the bar's third result, the rows say not applied",
    extra_steps=[on("settings.apply", reply=apply_job(
        result={"rev": 8, "sections": [section("network", True, False, "restart", err("io", "errors.settings.apply_failed", "core_restart_failed"))], "pending": None},
        error=err("io", "errors.settings.apply_failed", "core_restart_failed")))])
# S-02 saved, pending (starting) then the PendingApply turns applied / failed
add("settings_save_pending", "S-02: a save while the service starts: the section is deferred (PendingApply pending): 已保存，待应用 and 待生效 on the row; config.changed.pending then turns applied",
    run="starting",
    extra_steps=[on("settings.apply", reply=apply_job(
        result={"rev": 8, "sections": [section("network", True, False, "deferred")], "pending": pending("pending", "restart")})
        + [config_changed(8, [section("network", True, False, "deferred")], pending("pending", "restart"), seq=1)])])
add("settings_save_pending_failed", "S-02: a deferred save whose intent later fails (config.changed.pending failed): the row turns 未生效 with the reason",
    run="starting",
    extra_steps=[on("settings.apply", reply=apply_job(
        result={"rev": 8, "sections": [section("network", True, False, "deferred")], "pending": pending("pending", "restart")}))])
# S-03 stopped
add("settings_save_stopped", "S-03: a save while the service is stopped: saved and applied=true (nothing runs): 下次启动生效, never a failure",
    run="stopped",
    extra_steps=[on("settings.apply", reply=apply_job(result={"rev": 8, "sections": [section("network", True, True, "none")], "pending": None}))])
# S-04 partial
add("settings_save_partial", "S-04: two sections, one saved and applied (ui), the other not saved (network validation io): no rollback, each row its own label",
    extra_steps=[on("settings.apply", reply=apply_job(
        result={"rev": 8, "sections": [section("ui", True, True, "ui"), section("network", False, False, "none", err("io", "errors.settings.save_failed", "write_failed"))], "pending": None},
        error=err("io", "errors.settings.save_failed", "write_failed")))])
add("settings_save_applied", "S-04 (all applied): the instant sections are in effect: 已保存 · 已生效; the language row too (ui) and nothing else changes",
    extra_steps=[on("settings.apply", reply=apply_job(result={"rev": 8, "sections": [section("ui", True, True, "ui"), section("network", True, True, "reload")], "pending": None})
                    + [config_changed(8, [section("ui", True, True, "ui"), section("network", True, True, "reload")], None, seq=1)])])
# S-05 hold at a field
add("settings_save_hold_field", "S-05: settings.apply holds (validation, hold, field network.mihomo_mixed_port): the reason lands on the port row, the drafts stay",
    extra_steps=[on("settings.apply", reply=apply_job(
        result={"rev": 7, "sections": [section("network", False, False, "none")], "pending": None},
        error=err("validation", "errors.bridge.runtime_listener_conflict", "runtime_listener_conflict", field="network.mihomo_mixed_port", hold=True, params={"port": 7891})))])
# S-06 conflict
add("settings_save_conflict", "S-06: base_rev is stale (conflict, base_rev_mismatch): the page re-reads config.snapshot; rows without a draft follow, the bar says 配置已在别处修改",
    second_snapshot={"rev": 9, "values": snapshot_values(latency={"primary_gateway_max_latency_ms": 1200})},
    extra_steps=[on("settings.apply", reply=apply_job(
        result={"rev": 9, "sections": [section("network", False, False, "none")], "pending": None},
        error=err("conflict", "errors.settings.conflict", "base_rev_mismatch", hold=True)))])
# S-07 lifecycle busy (D-sem 3)
add("settings_save_lifecycle_busy", "S-07: a save during startup recovery: lifecycle_busy hold (recovery_in_progress, D-sem 3): 服务正在启动，就绪后再保存",
    run="starting",
    extra_steps=[on("settings.apply", reply=apply_job(
        result={"rev": 7, "sections": [section("network", False, False, "none")], "pending": None},
        error=err("lifecycle_busy", "errors.settings.lifecycle_busy", "recovery_in_progress", hold=True)))])
# S-08 port conflict (no field yet: the bar says it)
add("settings_save_port_conflict", "S-08: a port conflict before B-26(3): saved, not applied, the reason (no field) on the save bar",
    extra_steps=[on("settings.apply", reply=apply_job(
        result={"rev": 8, "sections": [section("network", True, False, "restart", err("io", "errors.bridge.runtime_listener_conflict", "runtime_listener_conflict", params={"port": 7891}))], "pending": None},
        error=err("io", "errors.bridge.runtime_listener_conflict", "runtime_listener_conflict", params={"port": 7891})))])
# S-09 read errors
add("settings_read_errors", "S-09: config.snapshot carries read_errors: the error bar with 打开配置目录 (no path, G48)",
    read_errors=[err("io", "errors.config.local_read", "config_read_failed"), err("io", "errors.config.sources_read", "sources_read_failed")],
    extra_steps=[on("host.openFolder", reply=[res("host.openFolder", {"opened": True})])])
# S-10 snapshot failed
add("settings_snapshot_failed", "S-10: config.snapshot fails (runtime_unavailable): the error bar with 重试, every contract row read-only",
    snapshot_reject=err("runtime_unavailable", "errors.runtime_unavailable"))
# S-11 autostart
add("settings_autostart_unsupported", "S-11: autostart.state supported=false: the switch is read-only, 当前安装方式不支持开机自启; D8 row disabled",
    autostart={"supported": False, "enabled": False, "target": "none", "mismatch": False, "error": None},
    values=snapshot_values(autostart={"enabled": False}))
add("settings_autostart_error", "S-11: autostart.state answers with an error: 无法读取",
    autostart={"supported": True, "enabled": False, "target": "none", "mismatch": False, "error": err("io", "errors.autostart.read_failed", "registry_read_failed")})
add("settings_autostart_mismatch", "S-11: autostart points at the legacy Qt build (mismatch, legacy_qt): 指向旧版本 · 保存后改为当前版本",
    autostart={"supported": True, "enabled": True, "target": "legacy_qt", "mismatch": True, "error": None})
add("settings_autostart_off", "S-11 / D8: autostart off: 开机自启时进入托盘 is disabled with its reason",
    autostart={"supported": True, "enabled": False, "target": "none", "mismatch": False, "error": None},
    values=snapshot_values(autostart={"enabled": False}))
# S-12 core version
add("settings_core_unknown", "S-12: core.info without a version: 代理核心 未检测", core_version=None, run="stopped")
# S-13 latency recheck
add("settings_save_recheck", "S-13: a latency save answered with effect recheck: 已生效 · 正在按新上限复检节点",
    extra_steps=[on("settings.apply", reply=apply_job(result={"rev": 8, "sections": [section("latency", True, True, "recheck")], "pending": None}))])
# S-14 language
add("settings_lang_saved", "S-14 / B3: ui.language saved (applied) but the shell does not forward ui.language (not in caps): no host:language within 3 s -> 语言已保存，重新打开窗口后生效",
    extra_steps=[on("settings.apply", reply=apply_job(result={"rev": 8, "sections": [section("ui", True, True, "ui")], "pending": None}))])
add("settings_lang_failed", "S-14: ui.language not saved (io): the choice rolls back, the row says why",
    extra_steps=[on("settings.apply", reply=apply_job(
        result={"rev": 7, "sections": [section("ui", False, False, "none", err("io", "errors.settings.save_failed", "write_failed"))], "pending": None},
        error=err("io", "errors.settings.save_failed", "write_failed")))])
# S-15 update states
add("settings_update_available", "S-15: update.state available + installable + notes (plain text, untrusted): 发现新版本, 更新说明, 立即更新",
    upd=update_state(state="available", version="1.1.0", update_available=True, installable=True, notes="Fixes:\n- <b>bold?</b> <img src=x onerror=window.__pwned=1>\n- example.org/path", last_check_at_ms=T0 - 600000))
add("settings_update_not_installable", "S-15: available but not installable: 无法在应用内安装 + 前往发布页 (host.openUrl)",
    upd=update_state(state="available", version="1.1.0", update_available=True, installable=False))
add("settings_update_downloading", "S-15: downloading (no percentage: B-30(c))", upd=update_state(state="downloading", version="1.1.0", update_available=True, installable=True))
add("settings_update_handoff", "S-15: handoff: 正在关闭 Tesseract 并安装", upd=update_state(state="handoff", version="1.1.0", update_available=True, installable=True))
add("settings_update_failed", "S-15: failed with a reason", upd=update_state(state="failed", error=err("io", "errors.update.operation_failed", "download_failed")))
add("settings_update_disabled", "S-15: this build has no in-app update (enabled=false)", upd=update_state(enabled=False))
add("settings_update_updated", "S-15: just updated (previous_version): 已更新到", upd=update_state(previous_version="0.9.0"))
# S-16 bastion minimum above capacity
add("settings_bastion_capacity", "S-16: the minimum's upper bound is bastion.pool.capacity (here 40), never 60",
    pool={"runtime_alive": 18, "user_alive": 2, "capacity": 40, "min_alive_nodes": 5, "target_alive": 30, "collecting": False, "observed_at_ms": T0})
# S-17 / S-18 mode.set
add("settings_mode_cleanup", "S-17: mode.set saved but not applied, apply_error cleanup_pending: the status line says so",
    extra_steps=[on("mode.set", reply=job("mode.set", "op-mode-1", 1, result={"mode": "mixed", "changed": True, "saved": True, "restarted": False, "applied": False,
                                                                                 "apply_error": err("lifecycle_busy", "errors.mode.apply_failed", "cleanup_pending")},
                                          error=err("lifecycle_busy", "errors.mode.apply_failed", "cleanup_pending"), final="failed"))])
add("settings_mode_admin_required", "S-18: mode.set refused (admin_required) while stopped; the cards say 以管理员身份启动 Tesseract 后可选 (D7)", run="stopped",
    mode={"mode": "system", "admin": True, "switching": False, "proxy_state": "disabled", "drift": False},
    extra_steps=[on("mode.set", reply=job("mode.set", "op-mode-1", 1, error=err("admin_required", "errors.admin_required"), final="failed"))])
add("settings_mode_no_admin", "S-18 / D7: no administrator rights: TUN and mixed cards are gated (no reopen button)",
    mode={"mode": "system", "admin": False, "switching": False, "proxy_state": "owned", "drift": False},
    ks={"enabled": True, "active": False, "admin": False, "error": None, "triggered": False})
# S-19 kill switch error / triggered
add("settings_ks_error", "S-19: killswitch.state with an error: 出错", ks={"enabled": True, "active": False, "admin": True, "error": err("io", "errors.killswitch.command_failed", "firewall_failed"), "triggered": False})
add("settings_ks_triggered", "S-19: killswitch.state triggered", ks={"enabled": True, "active": True, "admin": True, "error": None, "triggered": True})
# S-20 drift
add("settings_drift", "S-20: the system proxy rewritten by another program (drift): 系统代理被其他程序改写",
    mode={"mode": "system", "admin": True, "switching": False, "proxy_state": "other", "drift": True})
# S-21 geo
add("settings_geo_partial", "S-21: geo.status with items (one kind failed) and errors by message_key",
    geo_status=geo(errors=[err("io", "errors.geo.update_failed", "download_failed", params={"kind": "asn"})],
                   items=[{"kind": "geosite", "updated_at_ms": T0 - 3600000}, {"kind": "geoip", "updated_at_ms": T0 - 3600000},
                          {"kind": "mmdb", "updated_at_ms": T0 - 3600000}, {"kind": "asn", "updated_at_ms": None}]))
add("settings_geo_stale", "S-21: rule data older than 36 hours: 未按时更新", geo_status=geo(updated=T0 - 40 * 3600000))
add("settings_geo_needs_route", "S-21: geo.update fails with needs_route: 需要先有可用出口 (the button's failure word)",
    extra_steps=[on("geo.update", reply=job("geo.update", "op-geo-1", 1, result={"triggered": False, "completed": False, "status": geo()},
                                            error=err("needs_route", "errors.needs_route", "needs_route"), final="failed"))])
add("settings_geo_disabled", "S-21 / 31b-18: geo.update refused as disabled (conflict, reason disabled): the update button rests, the reason is shown",
    extra_steps=[on("geo.update", reply=job("geo.update", "op-geo-1", 1, result={"triggered": False, "completed": False, "status": geo()},
                                            error=err("conflict", "errors.geo.disabled", "disabled"), final="failed"))])
# S-22 sources hold
add("settings_sources_hold", "S-22: the sources batch holds (validation, hold, field sources.items[1].value, source_duplicate): nothing is written, the reason lands on the sources row",
    extra_steps=[on("settings.apply", reply=apply_job(
        result={"rev": 7, "sections": [section("sources", False, False, "none")], "pending": None},
        error=err("validation", "errors.settings.invalid_field", "source_duplicate", field="sources.items[2].value", hold=True)))])
# hidden list (secret_fields_set: the projection withheld a list whole)
add("settings_hidden_list", "C1-5: killswitch.additional_whitelist withheld whole (secret_fields_set): 含已隐藏的项, an edit asks before it replaces the list",
    values=snapshot_values(killswitch={"enabled": True, "allow_lan": True, "allow_dns": False}),
    secret_fields=["killswitch.additional_whitelist", "killswitch.dns_whitelist"])
# a frozen sidecar (fields absent): rows read-only
add("settings_frozen_sidecar", "R-03 / R-07: a projection without killswitch exceptions, egress recovery, bastion and the D8 key: those rows are read-only (never saved over a value the page could not read)",
    values={k: v for k, v in snapshot_values(
        network={"allow_lan": False, "ipv6": True, "mihomo_mixed_port": 7890, "system_proxy": {"bypass": ["localhost"]}, "subscription": {"port": 25500}},
        killswitch={"enabled": True}, ui={"language": "zh_CN", "overview_auto_exit_lookup": True}).items() if k != "bastion"})
# dialog_open
add("settings_dialog_open", "B8: host.openFolder answers conflict + dialog_open: the button's failure word is the shell's generic reason",
    extra_steps=[{"on_invoke": {"host": "rt"}, "reply": []}] if False else
    [on("host.openFolder", reply=[res("host.openFolder", error=err("conflict", "errors.host.conflict", "dialog_open"))])])

# {focus} landings: T.go('settings', {focus}) on the open page is a second visit (the queries again)
add("settings_focus", "focus: host:navigate / the update banner land on the alerts heading or the update row (a second visit re-reads the queries)", visits=2)

# B items (architecture 5): key 1 (the sidecar declares them); key 2 is the build's
add("settings_features", "B items: hello.caps.features declares every settings B item; the controls show only in a --h-features all build",
    features=["B-07", "B-23", "B-24", "B-27", "B-28", "B-30a", "B-30c", "B-30d", "B-31", "B-32", "B-33", "B-34", "B-35", "B-36"])

if __name__ != "__main__":
    ALL.clear()
for sc in ALL:
    print(write(sc))
print(len(ALL), "scenarios")
