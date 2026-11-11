"""T-P4-05b fix round: new tiers_import_unapplied.json, nodes.reveal step for the manage drawer in tiers_reset.json.

Run after tiers.mjs: .venv/Scripts/python.exe -B ui/tools/fixture_gen/tiers_fix1.py --force [<ui dir>/test/fixtures/scenarios]

OUTDATED (T-P4-12 fix 1): it follows tiers.mjs, whose output T-P4-H2 hand-edited; it refuses to write
without --force (ui/README.md §6). Run it on a copy of the scenarios directory.
"""
# Collected into ui/tools/fixture_gen/ by T-P4-12 (第八波裁定 05a-§11.5); usage and state: ui/README.md.
import copy
import json
import sys
from pathlib import Path

if "--force" not in sys.argv[1:]:
    sys.exit("tiers_fix1.py is outdated (it follows tiers.mjs, whose output T-P4-H2 hand-edited) and would "
             "overwrite test/fixtures/scenarios; see ui/README.md §6. Refusing to write; re-run with --force, "
             "preferably on a copy of the scenarios directory.")
ARGS = [a for a in sys.argv[1:] if a != "--force"]
D = Path(ARGS[0]) if ARGS else Path(__file__).resolve().parents[2] / "test" / "fixtures" / "scenarios"
T0 = 1790000000000
E1 = "rt-fixture-e1"


def load(n):
    return json.loads((D / f"{n}.json").read_text(encoding="utf-8"))


def dump(n, doc):
    (D / f"{n}.json").write_bytes((json.dumps(doc, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))


def job(cmd, op, rev0, terminal, result, error):
    head = {"epoch": E1, "produced_at_ms": T0, "sent_at_ms": T0 + 1}
    frames = [{"t": "ack", **head, "id": 2, "req_id": "$req_id", "owner": "$owner", "accepted": True, "op": op}]
    states = [("queued", None, None, None), ("running", T0, None, None), (terminal, T0, result, error)]
    for i, (state, started, res, err) in enumerate(states):
        frames.append({"t": "evt", **head, "topic": "jobs", "rev": rev0 + i, "kind": "edge",
                       "data": {"op": op, "req_id": "$req_id", "owner": "$owner", "name": cmd, "state": state,
                                "started_at_ms": started, "progress": None, "result": res, "error": err},
                       "seq": rev0 + i})
    return frames


def imp_error(reason):
    return {"code": "io", "message_key": f"errors.import.{reason}", "params": {}, "reason_code": reason}


GENERIC_IO = {"code": "io", "message_key": "errors.io", "params": {}}

# ---- tiers_import_unapplied ---------------------------------------------------------------------------
imp = load("tiers_import")
errs = load("tiers_import_errors")
steps = copy.deepcopy(imp["steps"][:21])
preview = copy.deepcopy(imp["steps"][21])
done_frames = imp["steps"][24]["reply"]
snap = copy.deepcopy(next(f for f in done_frames if f.get("topic") == "nodes.list"))
specs = copy.deepcopy(next(s for s in errs["steps"] if s.get("on_invoke", {}).get("cmd") == "import.specs"))

# paste: saved, then the Core reload failed (bridge_import._finish_apply: applied false, apply_error core_reload_failed)
paste_result = {"saved": True, "applied": False, "items": ["batch-1", "batch-2"], "subscriptions": [],
                "apply_error": imp_error("core_reload_failed")}
paste = {"on_invoke": {"cmd": "import.apply", "args": {"plan_id": "plan-1"}},
         "reply": job("import.apply", "op-import.apply-23", 10, "failed", paste_result, GENERIC_IO) + [snap]}

# manual: written, the runtime reload failed (bridge_import._saved_nodes: _failure_outcome saved=True runtime_reload_failed)
manual_snap = copy.deepcopy(snap)
manual_snap["data"]["items"] = [x for x in manual_snap["data"]["items"] if x["id"] not in ("v-imp1", "v-imp2")]
manual_snap["data"]["items"].append({"id": "v-man1", "name": "manual-one", "tier": "vanguard", "kind": "manual",
                                     "enabled": True, "status": "pending", "reason_code": None, "protocol": "socks5",
                                     "latency_ms": None, "subscription_id": None, "slow": False,
                                     "last_checked_at_ms": None})
manual_result = {"saved": True, "applied": False, "items": ["batch-m"], "subscriptions": [],
                 "apply_error": imp_error("runtime_reload_failed")}
manual = {"on_invoke": {"cmd": "import.apply", "args": {"tier": "vanguard", "mode": "manual"}},
          "reply": job("import.apply", "op-import.apply-24", 20, "failed", manual_result, imp_error("runtime_reload_failed")) + [manual_snap]}

steps += [preview, paste, specs, manual]
dump("tiers_import_unapplied", {
    "name": "tiers_import_unapplied",
    "desc": ("T-P4-05b review fix (PROTOCOL section 15, ticket item 4): import.apply ends failed after it saved. "
             "Paste (vanguard, plan-1): the Core reload failed (saved=true, applied=false, apply_error core_reload_failed); "
             "manual (vanguard, socks5 from import.specs): the runtime reload failed (saved=true, runtime_reload_failed). "
             "Each reply carries the next nodes.list snapshot: the form closes, 'saved, not applied' is said and the new rows glow by difference."),
    "lang": imp.get("lang", "zh_CN"),
    "steps": steps,
})

# ---- tiers_reset: nodes.reveal of a subscription address for the manage drawer --------------------------
rs = load("tiers_reset")
if not any(s.get("on_invoke", {}).get("cmd") == "nodes.reveal" for s in rs["steps"]):
    rs["steps"].append({"on_invoke": {"cmd": "nodes.reveal", "args": {"item_id": "sub-nebula", "fields": ["subscription_url"]}},
                        "reply": [{"t": "res", "epoch": E1, "produced_at_ms": T0, "sent_at_ms": T0 + 1, "id": 1,
                                   "req_id": "$req_id", "owner": "$owner", "cmd": "nodes.reveal", "ok": True,
                                   "data": {"item_id": "sub-nebula",
                                            "fields": {"subscription_url": "https://nebula.example.com/sub?token=FIXTURE-SECRET-7f3a"}}}]})
    rs["desc"] += (" The manage drawer of nebula reveals its address (nodes.reveal subscription_url, private): "
                   "a new generation or closing the drawer drops it (PROTOCOL section 12).")
    dump("tiers_reset", rs)
print("ok")
