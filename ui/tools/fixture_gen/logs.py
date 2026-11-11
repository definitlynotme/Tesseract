"""T-P4-05c: writes the logs page's scenario fixtures (ui/test/fixtures/scenarios/logs_*.json).

    .venv/Scripts/python.exe -B ui/tools/fixture_gen/logs.py [<ui dir>]

Deterministic (no clock, no randomness). Every file is then checked by
ui/tools/validate_ui_fixtures.py (the v1 frame oracle). The fixture clock starts at
2026-09-29 12:00 Asia/Shanghai (1790654400000); the records sit in the ten minutes before it.
Records carry the T-P4-C1 fields (is_key_event, run_id) unless a scenario says otherwise.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

# Collected into ui/tools/fixture_gen/ by T-P4-12 (第八波裁定 05a-§11.5); usage and state: ui/README.md.
import sys  # noqa: E402
UI = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
OUT = UI / "test" / "fixtures" / "scenarios"
E1, E2 = "rt-fixture-e1", "rt-fixture-e2"
NOW = 1790654400000
BASE = json.loads((OUT / "logs_i18n_running.json").read_text(encoding="utf-8"))

COMMANDS = ["service.start", "service.stop", "service.restart", "app.quit", "app.info", "logs.tail", "logs.query",
            "logs.setMihomoLevel", "logs.exportBundle", "logs.exportFiltered"]
TOPICS = ["service.state", "route.published", "tiers.summary", "traffic.tick", "traffic.history", "logs.batch",
          "logs.feed", "core.info", "jobs"]


def evt(topic, rev, kind, data, epoch=E1, seq=None):
    f = {"t": "evt", "epoch": epoch, "produced_at_ms": NOW, "sent_at_ms": NOW + 1, "topic": topic, "rev": rev,
         "kind": kind, "data": data}
    if kind in ("edge", "gap"):
        f["seq"] = rev if seq is None else seq
    return f


def hello(epoch=E1, commands=COMMANDS, lang="zh_CN"):
    h = copy.deepcopy(BASE["steps"][1]["deliver"])
    h["epoch"] = epoch
    h["lang"] = lang
    h["caps"] = {"commands": list(commands), "topics": list(TOPICS), "features": []}
    return h


def service(phase="running", stop=False, stale=False, uptime=600, epoch=E1, rev=1, failure=None):
    d = copy.deepcopy(BASE["steps"][2]["deliver"]["data"])
    running = phase == "running"
    d["intent"]["desired_running"] = phase not in ("stopped", "failed")
    d["intent"]["stop_requested"] = stop
    d["runtime"]["runtime_running"] = phase in ("running", "collecting", "core_standby", "stopping")
    d["runtime"]["startup_phase"] = phase
    d["runtime"]["failure_code"] = failure["code"] if failure else None
    if failure:
        d["runtime"]["failure"] = failure
    d["core"]["running"] = running
    d["core"]["pid"] = 7654 if running else None
    d["freshness"]["stale"] = stale
    d["freshness"]["uptime_s"] = uptime if running else None
    return evt("service.state", rev, "snapshot", d, epoch)


def feed(state="connected", level="info", dropped=None, run_id="run-42", reason=None, epoch=E1, rev=1):
    dc = {"debug": 0, "info": 0, "warn": 0, "error": 0, "critical": 0}
    dc.update(dropped or {})
    dc["total"] = sum(dc.values())
    d = {"state": state, "level": level, "updated_at_ms": NOW - 30000, "dropped": dc, "run_id": run_id}
    if reason:
        d["reason"] = reason
    return evt("logs.feed", rev, "snapshot", d, epoch)


def base_steps(epoch=E1, phase="running", feed_frame=None, svc=None, lang="zh_CN", visible=True):
    s = [{"deliver": {"t": "rt:reset", "epoch": epoch, "reason": "reconnect"}},
         {"deliver": hello(epoch, lang=lang)},
         {"deliver": svc or service(phase, epoch=epoch)}]
    for i in range(3, 8):          # route.published, tiers.summary, traffic.tick, traffic.history, core.info
        f = copy.deepcopy(BASE["steps"][i]["deliver"])
        f["epoch"] = epoch
        s.append({"deliver": f})
    s.append({"deliver": feed_frame or feed(epoch=epoch, state="connected" if phase == "running" else "stopped",
                                             run_id="run-42" if phase == "running" else None)})
    s.append({"deliver": {"t": "host:language", "language": lang, "epoch": epoch}})
    s.append({"deliver": {"t": "host:visibility", "visible": visible, "epoch": epoch}})
    return s


def res(cmd, data=None, error=None, epoch=E1):
    f = {"t": "res", "epoch": epoch, "produced_at_ms": NOW, "sent_at_ms": NOW + 1, "id": 1, "req_id": "$req_id",
         "owner": "$owner", "cmd": cmd, "ok": error is None}
    if error is None:
        f["data"] = data
    else:
        f["error"] = error
    return f


def err(code, key=None, reason=None, params=None):
    e = {"code": code, "message_key": key or f"errors.bridge.{code}", "params": params or {}}
    if reason:
        e["reason_code"] = reason
    return e


def on(cmd, reply=None, args=None, reject=None):
    o = {"on_invoke": {"cmd": cmd}}
    if args is not None:
        o["on_invoke"]["args"] = args
    if reply is not None:
        o["reply"] = reply
    if reject is not None:
        o["reject"] = reject
    return o


def app_info(epoch=E1):
    return on("app.info", [res("app.info", {"version": "1.0.0", "language": "zh_CN", "caps": [], "listeners": [
        {"kind": "mixed", "host": "127.0.0.1", "port": 7890, "available": True},
        {"kind": "controller", "host": "127.0.0.1", "port": 9090, "available": True}]}, epoch=epoch)])


def page(records, watermark=None, cursor=None, gap=False, dropped=0):
    return {"records": records, "watermark": watermark if watermark is not None else (records[-1]["seq"] if records else 0),
            "next_cursor": cursor, "gap": gap, "dropped": dropped}


def tail(p, args=None, epoch=E1):
    return on("logs.tail", [res("logs.tail", p, epoch=epoch)], args=args)


def tail_err(code, args=None, epoch=E1):
    return on("logs.tail", [res("logs.tail", error=err(code), epoch=epoch)], args=args)


# ------------------------------------------------------------------------------------- records
def r(seq, ts, source, level, category, key, title="", detail="", raw="", ev=None, run="run-42", **extra):
    d = {"seq": seq, "ts_ms": ts, "rx_ms": ts + 9, "source": source, "level": level, "category": category, "key": key,
         "title": title, "detail": detail, "raw": raw}
    if ev is not None:
        d["is_key_event"] = ev
    if run:
        d["run_id"] = run
    d.update(extra)
    return d


T = NOW - 600000      # 11:50:00


def history(start_seq=1, t0=T, run="run-42", c1=True):
    """fourteen ASCII records of one run (C1 fields unless c1=False)"""
    ev = (lambda v: v) if c1 else (lambda v: None)
    rn = run if c1 else None
    rows = [
        ("TESSERACT", "info", "runtime", "runtime.start_requested", "Starting service", "Collecting node sources and preparing the core.", "Desktop runtime start requested (generation=42)", True),
        ("MIHOMO", "info", "runtime", "log.raw", "", "", "Start initial configuration in progress", False),
        ("MIHOMO", "info", "connection", "log.raw", "", "", "[TCP] 127.0.0.1:50110 --> www.example.com:443 match RuleSet(proxy) using Vanguard", False),
        ("TESSERACT", "info", "routing", "tier.vanguard_active", "Traffic on Vanguard", "Vanguard has 2 usable nodes.", "carrier=vanguard usable=2", True),
        ("MIHOMO", "warn", "connection", "mihomo.connection_timeout", "Connection timed out", "203.0.113.9:443", "dial tcp 203.0.113.9:443: i/o timeout", False),
        ("MIHOMO", "warn", "connection", "mihomo.connection_timeout", "Connection timed out", "203.0.113.9:443", "dial tcp 203.0.113.9:443: i/o timeout", False),
        ("MIHOMO", "warn", "connection", "mihomo.connection_timeout", "Connection timed out", "203.0.113.10:443", "dial tcp 203.0.113.10:443: i/o timeout", False),
        ("TESSERACT", "error", "update", "update.sources_failed", "Source refresh failed", "All 5 sources failed; the pool keeps 4 usable nodes.", "", True),
        ("ALERT", "info", "runtime", "alert.delivered", "Alert delivered", "1 channel delivered.", "alert delivered channel=ntfy", True),
        ("TESSERACT", "warn", "routing", "tier.bastion_active", "Traffic on Bastion", "Bastion has 6 usable nodes, switched from Sentinel.", "carrier=bastion usable=6", True),
        ("MIHOMO", "debug", "connection", "log.raw", "", "", "[DNS] example.org --> 93.184.216.34", False),
        ("TESSERACT", "info", "configuration", "proxy.system_set", "System proxy set", "127.0.0.1:7890 written to the Windows proxy settings.", "system proxy set 127.0.0.1:7890", True),
        ("MIHOMO", "info", "other", "log.raw", "", "", "panic recovered\n  goroutine 12 [running]\n  main.serve()", False),
        ("TESSERACT", "info", "runtime", "log.raw", "", "", "Monitor heartbeat ok", False),
    ]
    out = []
    for i, (src, lv, cat, key, title, detail, raw, kev) in enumerate(rows):
        if not c1 and key == "log.raw":
            title = raw                                # a frozen sidecar: a raw line's title is the message (R1)
        out.append(r(start_seq + i, t0 + i * 20000, src, lv, cat, key, title, detail, raw, ev=ev(kev), run=rn))
    return out


def live_lines(start_seq, n, t0, run="run-42", source="MIHOMO"):
    return [r(start_seq + i, t0 + i * 20, source, "info", "connection", "log.raw", raw=f"[TCP] 127.0.0.1:{50200 + (start_seq + i) % 9000} --> h{(start_seq + i) % 97}.example.net:443", ev=False, run=run)
            for i in range(n)]


def batch(rev, records, dropped=0, dropped_total=0, gap=False, epoch=E1, from_seq=None, seq=None):
    return evt("logs.batch", rev, "edge", {"from_seq": from_seq if from_seq is not None else records[0]["seq"], "records": records,
                                           "dropped": dropped, "dropped_total": dropped_total, "gap": gap or dropped > 0}, epoch, seq)


def scenario(name, desc, steps, lang="zh_CN", allow_host=None):
    d = {"name": name, "desc": desc, "lang": lang}
    if allow_host:
        d["allow_host"] = allow_host
    d["steps"] = steps
    return d


def standard(extra_steps=(), invokes=(), h=None, **kw):
    """base frames + a seeded history (logs.tail answers with it) + app.info"""
    h = h if h is not None else history()
    return base_steps(**kw) + [tail(page(h)), app_info()] + list(invokes) + list(extra_steps)


SCEN = {}


def add(name, desc, steps, **kw):
    SCEN[name] = scenario(name, desc, steps, **kw)


H = history()
WM = H[-1]["seq"]

# ---------------------------------------------------------------------------------- L-1 … L-13
add("logs_seed_loading", "L-1：首次进入，logs.tail 还没有回答：列表骨架、页脚「正在载入日志…」；其间到达的实时批次先缓在缓冲里",
    base_steps() + [on("logs.tail", args={"n": 4000}), app_info(), {"deliver": batch(1, live_lines(WM + 1, 3, NOW - 2000))}])
add("logs_seed_failed", "L-2：logs.tail 返回 runtime_unavailable：空状态「无法载入日志」+「重试」（重试仍按场景回答）",
    base_steps() + [tail_err("runtime_unavailable"), app_info(), tail(page(H))])
add("logs_seed_snapshot_retry", "L-3：拉第 2 页时游标失效（snapshot_required）：静默整组重取，第二组成功，列表完整",
    base_steps() + [
        tail(page(H[:7], watermark=WM, cursor="c1"), args={"n": 4000}),
        tail_err("snapshot_required", args={"cursor": "c1"}),
        tail(page(H[7:], watermark=WM), args={"cursor": "c2"}),
        tail(page(H[:7], watermark=WM, cursor="c2"), args={"n": 4000}),
        app_info()])
add("logs_seed_evicted", "L-4：种子之前已淘汰 1,208 条（LogPage.gap/dropped）：历史起点行「更早的 1,208 条已从内存移除」（B-25 降级文案）",
    base_steps() + [tail(page(H, gap=True, dropped=1208)), app_info()])
_gapsteps = [
    {"deliver": batch(1, live_lines(WM + 1, 2, NOW - 9000))},                                   # 15-16
    {"deliver": batch(2, live_lines(WM + 6, 2, NOW - 8000))},                                   # jump: 17-19 missing
    {"deliver": batch(3, live_lines(WM + 8, 2, NOW - 7000), dropped=4, dropped_total=4)},       # dropped 4
    {"deliver": evt("logs.batch", 4, "gap", {"from_seq": 4, "to_seq": 5, "dropped": 2}, seq=6)},  # two batch frames lost
    {"deliver": batch(7, live_lines(WM + 15, 2, NOW - 5000), dropped_total=4, seq=7)},          # the records they held: 24-28
    {"deliver": {"t": "rt:log-gap", "epoch": E1, "data": {"from_seq": 8, "to_seq": 9, "dropped": 2, "record_from_seq": WM + 17, "record_to_seq": WM + 20, "record_dropped": 4}}},
    {"deliver": batch(10, live_lines(WM + 21, 2, NOW - 3000), dropped_total=4, seq=10)},
]
add("logs_live_gap", "L-5：实时缺口：from_seq 跳号、本批 dropped>0、独立 kind:'gap' 帧、外壳 rt:log-gap，各插一行「此处有 N 条日志未送达」；页脚「已丢弃」取 logs.feed.dropped",
    base_steps(feed_frame=feed(dropped={"debug": 2, "info": 6, "warn": 1})) + [tail(page(H)), app_info()] + _gapsteps)
_b1 = json.dumps({"from": "mihomo_api", "to": "stdout", "at_ms": NOW - 60000, "reason": "stream_error", "continuity": "unknown"})
_b2 = json.dumps({"from": "stdout", "to": "mihomo_api", "at_ms": NOW - 20000, "reason": "reconnected", "continuity": "unknown"})
_bound = H + [
    r(WM + 1, NOW - 60000, "MIHOMO", "info", "other", "source_boundary", "Core log source boundary", _b1, _b1, ev=False),
    r(WM + 2, NOW - 50000, "MIHOMO", "info", "connection", "log.raw", raw="[TCP] 127.0.0.1:50300 --> api.example.com:443 match Match using Vanguard", ev=False),
    r(WM + 3, NOW - 20000, "MIHOMO", "info", "other", "source_boundary", "Core log source boundary", _b2, _b2, ev=False),
    r(WM + 4, NOW - 10000, "MIHOMO", "info", "connection", "log.raw", raw="[TCP] 127.0.0.1:50301 --> cdn.example.com:443 match Match using Vanguard", ev=False),
]
add("logs_source_boundary", "L-6：Mihomo 日志来源切换（key=source_boundary 的边界记录，两个视图都显示、不被筛选隐藏）；标题是桥接的固定英文，页面按 to 走目录",
    base_steps(feed_frame=feed(state="stdout")) + [tail(page(_bound)), app_info()])
_e2 = [{"deliver": {"t": "rt:reset", "epoch": E1, "reason": "runtime_gone"}},
       {"deliver": {"t": "rt:reset", "epoch": E2, "reason": "runtime_gone"}}] + base_steps(epoch=E2)[1:] + [
       {"deliver": batch(1, live_lines(4, 2, NOW - 1000, run="run-43"), epoch=E2)}]
_h2 = [dict(x, run_id="run-43") for x in history(1, NOW - 5000)[:3]]
add("logs_sidecar_restart", "L-7：侧车重启（rt:reset runtime_gone，新 epoch，seq 从 1 重新计）：清空缓冲，插「应用后台已重新启动」，按新 epoch 重新种子（两个 logs.tail 回答都给新 epoch：页面先后取几次都只用新的那次）",
    base_steps() + [{"deliver": batch(1, live_lines(WM + 1, 2, NOW - 30000))}, tail(page(H), epoch=E2)] + _e2
    + [tail(page(_h2), epoch=E2), app_info(E2), app_info(E2)])
add("logs_mihomo_starting", "L-8：服务正在启动：页脚 Mihomo「连接中」，调试日志采集不可用",
    base_steps(phase="collecting", feed_frame=feed(state="stopped", run_id="run-42")) + [tail(page(H[:2])), app_info()])
add("logs_feed_reconnecting", "L-9：Mihomo 日志流断开、正在重连（logs.feed.state=reconnecting）：页脚琥珀色「重连中」",
    base_steps(feed_frame=feed(state="reconnecting")) + [tail(page(H)), app_info()])
add("logs_feed_controller_disabled", "L-10：控制器未启用（logs.feed.reason=controller_disabled，经标准输出接收）：页脚显示原因",
    base_steps(feed_frame=feed(state="stdout", reason="controller_disabled")) + [tail(page(H)), app_info()])
add("logs_state_stale", "L-11：service.state 不新鲜（freshness.stale）：运行摘要条「数据未更新」",
    base_steps(svc=service(stale=True)) + [tail(page(H)), app_info()])
add("logs_paused_overflow", "L-12：暂停期间缓冲溢出：种子 14 条；用例点「实时跟随」暂停后投 4,300 条（五批），4,000 条上限挤出其中 300 条新日志，浮标写「（其中 300 条已被挤出）」",
    standard())
add("logs_live_title_language", "L-13：实时批次的关键事件标题由桥接按当前语言渲染（T-P4-C1）：页面原样显示，不另取 logs.query",
    base_steps(lang="en_US") + [tail(page(H)), app_info(),
        {"deliver": batch(1, [r(WM + 1, NOW - 3000, "TESSERACT", "warn", "routing", "tier.sentinel_active", "Traffic on Sentinel", "Vanguard has no usable node; Sentinel carries the traffic.", "carrier=sentinel", ev=True)])}],
    lang="en_US")

# ---------------------------------------------------------------------------------- O-1 … O-9
_saved = lambda ref="save-1": on("host.saveFile", [res("host.saveFile", {"cancelled": False, "ref": ref})])


def job(name, state, result=None, error=None, rev=1):
    d = {"op": "op-1", "req_id": "$req_id", "owner": "$owner", "name": name, "state": state, "started_at_ms": NOW}
    if result is not None:
        d["result"] = result
    if error is not None:
        d["error"] = error
    return evt("jobs", rev, "edge", d)


def ack():
    return {"t": "ack", "epoch": E1, "produced_at_ms": NOW, "sent_at_ms": NOW + 1, "id": 1, "req_id": "$req_id", "owner": "$owner", "accepted": True, "op": "op-1"}


add("logs_export_filtered_done", "O-1 导出当前视图：另存为 → logs.exportFiltered{seqs}（只用 seqs）→ done（missing_seqs 两条：部分已淘汰）→ 提示条「在文件夹中显示」→ host.reveal",
    standard(invokes=[_saved(), on("logs.exportFiltered", [ack(), job("logs.exportFiltered", "done", {"ref": "exp-1", "count": 12, "missing_seqs": [3, 4]})]),
                      on("host.reveal", [res("host.reveal", {"revealed": True})])]))
add("logs_export_bundle_done", "O-1 导出诊断包：另存为（zip）→ logs.exportBundle → done → 结果弹层（大小、文件清单，不显示文件名与位置）→「打开所在文件夹」= host.reveal",
    standard(invokes=[_saved("save-2"), on("logs.exportBundle", [ack(), job("logs.exportBundle", "done", {"ref": "bundle-1", "size": 1843200, "files": ["monitor.log", "sidecar-stderr.log", "state.json", "config.redacted.yaml", "runs/run-41.log", "runs/run-42.log", "tier3-source-state.json", "manifest.json"]})]),
                      on("host.reveal", [res("host.reveal", {"revealed": True})])]))
add("logs_export_cancelled", "O-1 另存为对话框取消（cancelled:true）：不启动导出、不提示",
    standard(invokes=[on("host.saveFile", [res("host.saveFile", {"cancelled": True, "ref": None})])]))
add("logs_export_dialog_open", "O-1 / 批评 B8：host.saveFile 返回 conflict + reason_code dialog_open：提示「另一个对话框还开着，关掉后再试」",
    standard(invokes=[on("host.saveFile", [res("host.saveFile", error=err("conflict", "errors.host.conflict", "dialog_open"))])]))
add("logs_export_failed", "O-1 导出作业失败（io）：错误提示条「日志导出失败」+ 原因；保存授权过期（not_allowed）另有「请重新导出」",
    standard(invokes=[_saved(), on("logs.exportFiltered", [ack(), job("logs.exportFiltered", "failed", error=err("io"))])]))
add("logs_export_interrupted", "O-1 导出作业在排队时被取消（cancelled）：提示「导出已中断，请重新导出」",
    standard(invokes=[_saved(), on("logs.exportFiltered", [ack(), job("logs.exportFiltered", "cancelled")])]))
add("logs_export_reveal_gone", "O-1 导出完成后「在文件夹中显示」：host.reveal 返回 not_allowed（侧车重启后 ref 失效）：提示「找不到导出的文件」",
    standard(invokes=[_saved(), on("logs.exportFiltered", [ack(), job("logs.exportFiltered", "done", {"ref": "exp-1", "count": 14, "missing_seqs": []})]),
                      on("host.reveal", [res("host.reveal", error=err("not_allowed", "errors.host.not_allowed"))])]))
add("logs_export_empty", "O-2 当前视图 0 条：⋯ 里导出当前视图与清空不可用，右键「导出当前筛选」不可用（诊断包仍可导出）",
    base_steps(phase="stopped") + [tail(page([])), app_info()])
add("logs_clear_undo", "O-3 清空当前视图：提示条带 5 秒倒计时环与「撤销」；期间到达的新日志不顶掉撤销；撤销后恢复",
    standard())
add("logs_debug_failed", "O-4 采集 Mihomo 调试日志失败（io）：错误提示条，开关回到 logs.feed 的实际级别（info）",
    standard(invokes=[on("logs.setMihomoLevel", [res("logs.setMihomoLevel", error=err("io"))], args={"level": "debug"})]))
add("logs_folder_failed", "O-5 打开日志文件夹失败（opened:false）：提示「没能打开日志文件夹」",
    standard(invokes=[on("host.openFolder", [res("host.openFolder", {"opened": False})], args={"kind": "data"})]))  # 第八波 #5 (D17): the data folder
add("logs_locate_gone", "O-6 定位失败：阅读器打开的记录随后被 4,000 条上限挤出内存：「这条记录已不在内存中」（用例投 4,100 条）",
    standard())
add("logs_copy_failed", "O-7 复制失败（剪贴板被拒，用例里让剪贴板拒绝）：提示「没能复制到剪贴板」",
    standard())
add("logs_multi_copy", "O-8 多行选择：Ctrl 点选、Shift 点选范围、Ctrl C 复制：「已复制 N 条」",
    standard())
add("logs_relabel_failed", "O-9 切换界面语言后重取 logs.tail 失败：保留旧文字，警告条带「重试」",
    standard(invokes=[tail_err("runtime_unavailable")]))

# ---------------------------------------------------------------------------------- the rest
_re = [{"deliver": batch(1, live_lines(WM + 1, 2, NOW - 9000))}, {"deliver": batch(2, live_lines(WM + 3, 2, NOW - 8000))},
       {"deliver": {"t": "rt:reset", "epoch": E1, "reason": "reconnect"}}, {"deliver": hello(E1)},
       {"deliver": service()}, {"deliver": feed()},
       {"deliver": batch(1, live_lines(WM + 1, 2, NOW - 9000))}, {"deliver": batch(2, live_lines(WM + 3, 2, NOW - 8000))},
       {"deliver": batch(3, live_lines(WM + 5, 1, NOW - 7000))}]
add("logs_reconnect_same_epoch", "同 epoch 重连（批评 B9）：rt:reset{reconnect} 后外壳回放同样的日志尾：不清空、不插分隔、不重复，只多出新的一条",
    standard(extra_steps=_re))
_frozen = [dict(x, key="", title=x["raw"]) for x in live_lines(WM + 1, 2, NOW - 4000, run=None)]
for _x in _frozen:
    _x.pop("is_key_event", None)
add("logs_key_event_absent", "冻结侧车（批评 B10）：记录没有 is_key_event / run_id，原始行的标题是原文，实时批次里原始行 key 为空串：按 key 判断关键事件、按 runtime.start_requested 切分运行",
    standard(h=history(c1=False), extra_steps=[{"deliver": batch(1, _frozen)}]))
_mal = H + [
    r(WM + 1, NOW - 9000, "TESSERACT", "error", "runtime", "runtime.desktop_failed", '<img src="x" onerror="window.__pwned=1">', '<script>window.__pwned=1</script><b>bold?</b>', '<iframe src="javascript:window.__pwned=1"></iframe>', ev=True),
    r(WM + 2, NOW - 8000, "MIHOMO", "warn", "connection", "log.raw", raw='<script>window.__pwned=1</script> & "quoted" <img src=x onerror=window.__pwned=1>', ev=False),
    r(WM + 3, NOW - 7000, "MIHOMO", "info", "connection", "log.raw", raw="L" + "o" * 4990 + "ng", ev=False),
    r(WM + 4, NOW - 6000, "TESSERACT", "warn", "routing", "tier.sentinel_active", "Traffic on ‮Sentinel‬ ⁦mixed⁩", "bidi ‏‫ controls ‬ end", "carrier=sentinel ‮", ev=True),
]
_b22 = base_steps()
_b22[1]["deliver"]["caps"]["features"] = ["B-20", "B-21", "B-22", "B-23", "B-24", "B-25"]
add("logs_b22_advice", "B-22 建议卡（批评 A2）：侧车声明 B-20…B-25；只有 --h-features all 的设计构建同时放行时，阅读器里 runtime.desktop_failed 才出现建议卡（普通构建不出现）",
    _b22 + [tail(page(H + [r(WM + 1, NOW - 5000, "TESSERACT", "error", "runtime", "runtime.desktop_failed", "Start failed", "Port 7890 is in use by another program.", "RuntimeListenerPreflightError: port 7890 in use", ev=True)])), app_info()])
add("logs_malicious_text","不可信日志文本：<img onerror>、<script>、超长行、双向控制字符只出现在文本节点（注入检查）",
    base_steps() + [tail(page(_mal)), app_info()])


def stress(name, desc, hidden):
    seed = [r(i + 1, NOW - 900000 + i * 200, "MIHOMO" if i % 3 else "TESSERACT", ("info", "info", "debug", "warn", "info", "error")[i % 6],
              ("connection", "runtime", "routing", "health", "connection", "update")[i % 6],
              ("log.raw", "log.raw", "tier.vanguard_active", "log.raw", "mihomo.connection_timeout", "update.sources_failed")[i % 6],
              *(("", "", f"line {i + 1}") if i % 6 in (0, 1, 3) else ("Traffic on Vanguard", "Vanguard has 2 usable nodes.", "carrier=vanguard") if i % 6 == 2
                else ("Connection timed out", "203.0.113.9:443", "dial tcp 203.0.113.9:443: i/o timeout") if i % 6 == 4
                else ("Source refresh failed", "All 5 sources failed.", "")),
              ev=i % 6 in (2, 5)) for i in range(4000)]
    pages, size = [], 1000
    for k in range(0, 4000, size):
        cur = f"s{k // size + 1}" if k + size < 4000 else None
        pages.append(page(seed[k:k + size], watermark=4000, cursor=cur))
    inv = [tail(pages[0], args={"n": 4000})] + [tail(pages[i], args={"cursor": f"s{i}"}) for i in range(1, len(pages))] + [app_info()]
    if hidden:
        inv = [tail(page(H)), app_info()]            # the hidden period needs no big history
    steps = base_steps(visible=not hidden) + inv
    seq, rev = (WM + 1 if hidden else 4001), 1
    seconds = 30 if hidden else 10
    for s in range(seconds * 5):                    # 200 ms batches of 10 lines: 50 lines a second
        steps.append({"advance_ms": 200})
        steps.append({"deliver": batch(rev, live_lines(seq, 10, NOW + s * 200))})
        seq += 10
        rev += 1
    if hidden:
        steps.append({"deliver": {"t": "host:visibility", "visible": True, "epoch": E1}})
    add(name, desc, steps)


stress("logs_stress_4000", "压力（给 T-P4-09）：logs.tail 四页种子 4,000 条（256 KiB 分页、游标续页），之后每 200 ms 一批 10 条，即每秒 50 条，持续 10 秒", False)
stress("logs_hidden_30s", "隐藏零开销（给 T-P4-09）：窗口隐藏（host:visibility false）期间每秒 50 条、持续 30 秒，之后恢复可见；隐藏期间列表与侧栏徽标都不写 DOM", True)

for name, doc in SCEN.items():
    big = name.startswith(("logs_stress", "logs_hidden"))
    text = (json.dumps(doc, ensure_ascii=False, separators=(",", ":")) if big else json.dumps(doc, ensure_ascii=False, indent=2)) + "\n"
    (OUT / f"{name}.json").write_bytes(text.encode("utf-8"))
print(f"wrote {len(SCEN)} scenarios")
