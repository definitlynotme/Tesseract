"""Give the 04e settings scenarios what the 05d page reads on enter (minimal, cross-boundary: see report).

Run: .venv/Scripts/python.exe -B ui/tools/fixture_gen/settings_i18n_patch.py [<ui dir>] (idempotent)
"""
# Collected into ui/tools/fixture_gen/ by T-P4-12 (第八波裁定 05a-§11.5); usage and state: ui/README.md.
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import settings as G  # noqa: E402 (05d gen_scen.py)

D = (Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]) / "test" / "fixtures" / "scenarios"
for name, run in [("settings_i18n_running", "running"), ("settings_i18n_stopped", "stopped")]:
    p = D / f"{name}.json"
    sc = json.loads(p.read_text(encoding="utf-8"))
    if any("on_invoke" in s and s["on_invoke"].get("cmd") == "config.snapshot" for s in sc["steps"]):
        print(name, "already patched"); continue
    steps = sc["steps"]
    for s in steps:
        f = s.get("deliver")
        if f and f.get("t") == "hello":
            f["caps"]["commands"] = sorted(set(f["caps"]["commands"]) | set(G.COMMANDS) | {"app.quit"})
            f["caps"]["topics"] = sorted(set(f["caps"]["topics"]) | set(G.TOPICS))
    # insert the runtime topics the page reads, before the shell frames (host:*) at the end
    at = next((i for i, s in enumerate(steps) if s.get("deliver", {}).get("t", "").startswith("host:")), len(steps))
    vals = G.snapshot_values()
    extra = [{"deliver": G.evt("network.mode", {"mode": "system", "admin": True, "switching": False, "proxy_state": "owned" if run == "running" else "disabled", "drift": False})},
             {"deliver": G.evt("killswitch.state", {"enabled": True, "active": False, "admin": True, "error": None, "triggered": False})},
             {"deliver": G.evt("update.state", {"state": "idle", "version": None, "update_available": False, "installable": False, "error": None, "current_version": "1.0.0", "previous_version": None})},
             {"deliver": G.evt("geo.status", G.geo())},
             {"deliver": G.evt("bastion.sources", {"items": vals["sources"]})},
             {"deliver": G.nodes_list("vanguard", [120, 480, None])}, {"deliver": G.nodes_list("sentinel", [260, 900])}, {"deliver": G.nodes_list("gateway", [])}]
    if run == "running":
        extra.insert(2, {"deliver": G.evt("bastion.pool", {"runtime_alive": 18, "user_alive": 2, "capacity": 100, "min_alive_nodes": 5, "target_alive": 30, "collecting": True, "observed_at_ms": G.T0})})
    steps[at:at] = extra
    snap = {"rev": 7, "values": vals, "secret_fields_set": [], "read_errors": [], "pending": None}
    # every visit reads them again (B-27's fallback): enough answers for the tests' revisits
    for i in range(4):
        steps.append(G.on("config.snapshot", reply=[G.res("config.snapshot", snap)]))
        steps.append(G.on("autostart.state", reply=[G.res("autostart.state", {"supported": True, "enabled": True, "target": "shell", "mismatch": False, "error": None})]))
        steps.append(G.on("app.info", reply=[G.res("app.info", {"version": "1.0.0", "language": "zh_CN", "caps": [], "listeners": [{"kind": "mixed", "host": "127.0.0.1", "port": 7890, "available": True}]})]))
        steps.append(G.on("geo.status", reply=[G.res("geo.status", G.geo())]))
    steps.append(G.on("alerts.catalog", reply=[G.res("alerts.catalog", {"channels": [], "event_groups": [], "scopes": [], "acl": {}})]))
    # the rule-data update of the geo_fail case: one kind failed (Geo.items), the reason by message_key
    if run == "running":
        steps.append(G.on("geo.update", reply=G.job("geo.update", "op-geo-1", 1, result={"triggered": True, "completed": False,
            "status": G.geo(errors=[G.err("io", "errors.geo.update_failed", "download_failed")],
                            items=[{"kind": "geosite", "updated_at_ms": G.T0 - 3600000}, {"kind": "geoip", "updated_at_ms": G.T0 - 3600000},
                                   {"kind": "mmdb", "updated_at_ms": G.T0 - 3600000}, {"kind": "asn", "updated_at_ms": None}])},
            error=G.err("io", "errors.geo.update_failed", "download_failed"), final="failed")))
    sc["desc"] = sc["desc"].replace("the settings page itself still takes its values from the design data until T-P4-05d.",
                                    "T-P4-05d: plus the v1 caps, the runtime topics and the query answers the page reads on every visit (config.snapshot etc.), and a geo.update that fails one kind.")
    p.write_bytes((json.dumps(sc, ensure_ascii=False) + "\n").encode("utf-8"))
    print(name, "patched", len(steps))
