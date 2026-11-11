"""Self-tests of the T-P6-05 dist refresh and UI guard (stand-in builds, no cargo / PyInstaller).

    .venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider ui/tools/guard/test_ui_guard.py

Not under tests/ (pytest.ini testpaths) on purpose: the ticket allows new files only here. Nothing
touches dist-shell, the live profile, the HKCU proxy or the firewall: the refresh tests run on a
temporary tree with a stand-in builder, guard and process list; the guard tests run pure functions,
a stand-in --print-scope program and the node projection on fixture frames.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


refresh = _load("claude_shell_dist_refresh", REPO / ".build" / "claude_shell_dist_refresh.py")
guard = _load("ui_guard", REPO / "scripts" / "ui_guard.py")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ============================================================ refresh: stand-in tree


def write(path: Path, data: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    return path


# A stand-in ui/build.mjs: sourceDigest() over ui/src and ui/css, run by the real
# refresh.ui_source_digest (node), like the real one.
STUB_BUILD_MJS = r"""import { createHash } from 'node:crypto';
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
const UI = dirname(fileURLToPath(import.meta.url));
function walk(d, out = []) {
  if (!existsSync(d)) return out;
  for (const n of readdirSync(d).sort()) { const p = join(d, n); if (statSync(p).isDirectory()) walk(p, out); else out.push(p); }
  return out;
}
export function sourceDigest() {
  const h = createHash('sha256');
  for (const f of [...walk(join(UI, 'src')), ...walk(join(UI, 'css'))]) h.update(relative(UI, f).replace(/\\/g, '/') + '\0' + readFileSync(f, 'utf8'));
  return h.digest('hex');
}
"""


def make_tree(tmp: Path) -> refresh.Layout:
    root = tmp / "repo"
    # the inputs the snapshot covers, and one sidecar component
    write(root / "app_version.txt", "0.1.0\n")
    write(root / "ui" / "build.mjs", STUB_BUILD_MJS)
    write(root / "ui" / "src" / "pages" / "overview.js", "export const x = 1;\n")
    write(root / "ui" / "css" / "index.css", "body { color: black; }\n")
    write(root / "config.example.yaml", "alerts: {}\n")  # a packaged sidecar data file
    write(root / "locales" / "zh_CN.json", "{}\n")
    write(root / "shell" / "app" / "src" / "main.rs", "fn main() {}\n")
    write(root / "bridge_example.py", "X = 1\n")
    # the old dist (dist/ since the P8 cut-over) and what must survive a delivery
    dist = root / refresh.DIST_DIRNAME
    write(dist / "Tesseract" / "Tesseract.exe", b"old shell")
    write(dist / "Tesseract" / "tesseract-runtime.exe", b"old sidecar")
    write(dist / "Tesseract" / "_internal" / "old-only.txt", "old")
    write(dist / "preview-build.json", json.dumps({"version": "old"}))
    write(dist / "run-preview.cmd", "@echo old\r\n")
    write(dist / "preview-profile" / "config" / "config.yaml", "user: keep\n")
    return refresh.Layout(root)


def stand_in_builder(*, mutate=None, code: int = 0, ui_digest: str | None = None):
    """Like `build_shell_preview.py build --out <candidate>`: copies a ready-made directory and
    writes the attachments into the candidate's parent. `ui_digest` overrides the recorded
    ui_source_digest (a web UI built from another source, e.g. a reused stale UI_OUT)."""
    def build(layout, tag):
        if code:
            return code
        candidate = layout.candidate(tag)
        shell, sidecar = b"new shell " + tag.encode(), b"new sidecar " + tag.encode()
        write(candidate / "Tesseract.exe", shell)
        write(candidate / "tesseract-runtime.exe", sidecar)
        write(candidate / "_internal" / "new-only.txt", "new")
        component = (layout.root / "bridge_example.py").read_bytes()
        write(candidate / "tier3_build_id.json", json.dumps({"build_id": "b-" + tag, "components": {"bridge_example.py": sha(component)}}))
        digest = ui_digest or refresh.ui_source_digest(layout.root)
        assert digest, "the stand-in ui/build.mjs must give a digest"
        write(layout.record(tag) / "preview-build.json", json.dumps({"version": "0.1.0", "distribution": True,
                                                                      "shell_sha256": sha(shell), "sidecar_sha256": sha(sidecar),
                                                                      "ui_source_digest": digest}))
        write(layout.record(tag) / "run-preview-isolated.cmd", "@echo new\r\n")
        write(layout.record(tag) / "使用说明.txt", "\ufeff说明".encode("utf-8"))
        if mutate:
            mutate(layout)
        return 0
    return build


def stand_in_guard(verdict: str = "PASS", *, items=refresh.GUARD_ITEMS, unit_scope: str = "full", helper_smoke=True):
    """A guard.json like ui_guard.py writes: every item PASS, except item 2 when the verdict is not PASS."""
    def run(layout, tag):
        status = "PASS" if verdict == "PASS" else "FAIL"
        rows = [{"id": i, "status": "PASS", "findings": []} for i in items if i != "2"]
        if "2" in items:
            rows.append({"id": "2", "status": status, "findings": [] if status == "PASS" else [
                {"what": "后端 message_key errors.x 在 zh_CN 没有条目", "where": "ui/src/i18n/locales/zh_CN/errors.json", "owner": "后端道"}]})
        return {"verdict": verdict, "seconds": 1.0, "out": str(layout.meta(tag) / "guard"), "items": rows,
                "unit_scope": unit_scope, "helper_smoke": helper_smoke}
    return run


no_processes = lambda target: []


def test_build_then_deliver_swaps_once_backs_up_and_keeps_the_profile(tmp_path):
    layout = make_tree(tmp_path)
    tag = "20261026-1"
    assert refresh.build(tag, layout=layout, builder=stand_in_builder(), guard=stand_in_guard()) == 0
    state = json.loads((layout.meta(tag) / "state.json").read_text(encoding="utf-8"))
    assert state["result"] == "built" and state["guard"]["verdict"] == "PASS"
    assert (layout.target / "Tesseract.exe").read_bytes() == b"old shell"  # build never touches the dist
    assert refresh.deliver(tag, layout=layout, processes=no_processes) == 0
    assert (layout.target / "Tesseract.exe").read_bytes() == b"new shell " + tag.encode()
    assert not (layout.target / "_internal" / "old-only.txt").exists()
    backup = layout.backup(tag)
    assert (backup / "Tesseract" / "Tesseract.exe").read_bytes() == b"old shell"
    assert (backup / "run-preview.cmd").is_file() and (backup / "preview-build.json").is_file()
    assert not (layout.dist_root / "run-preview.cmd").exists()
    assert (layout.dist_root / "run-preview-isolated.cmd").is_file() and (layout.dist_root / "使用说明.txt").is_file()
    assert json.loads((layout.dist_root / "preview-build.json").read_text(encoding="utf-8"))["distribution"] is True
    assert (layout.dist_root / "preview-profile" / "config" / "config.yaml").read_text() == "user: keep\n"
    assert not layout.stage(tag).parent.exists()
    report = json.loads((layout.meta(tag) / "refresh.json").read_text(encoding="utf-8"))
    assert report["ui_risk"] is None and report["guard"]["verdict"] == "PASS"
    assert report["removed_files"] == ["_internal/old-only.txt"] and "_internal/new-only.txt" in report["added_files"]
    assert report["old_shell_sha256"] == sha(b"old shell")
    # a tag is used once; a second delivery of it is refused (the backup exists)
    with pytest.raises(refresh.Refused):
        refresh.build(tag, layout=layout, builder=stand_in_builder(), guard=stand_in_guard())
    with pytest.raises(refresh.Refused, match="already delivered"):
        refresh.deliver(tag, layout=layout, processes=no_processes)


def test_deliver_refuses_a_guard_failure_unless_forced_and_records_the_risk(tmp_path):
    layout = make_tree(tmp_path)
    tag = "20261026-2"
    assert refresh.build(tag, layout=layout, builder=stand_in_builder(), guard=stand_in_guard("FAIL")) == 2
    with pytest.raises(refresh.Refused, match="UI guard is 'FAIL'"):
        refresh.deliver(tag, layout=layout, processes=no_processes)
    assert (layout.target / "Tesseract.exe").read_bytes() == b"old shell" and not layout.backup(tag).exists()
    assert refresh.deliver(tag, layout=layout, force_ui_risk=True, processes=no_processes) == 0
    risk = json.loads((layout.meta(tag) / "refresh.json").read_text(encoding="utf-8"))["ui_risk"]
    assert risk["verdict"] == "FAIL" and risk["findings"][0]["where"].endswith("errors.json")


def test_incomplete_guard_is_refused_too(tmp_path):
    layout = make_tree(tmp_path)
    tag = "20261026-3"
    assert refresh.build(tag, layout=layout, builder=stand_in_builder(), guard=stand_in_guard("INCOMPLETE")) == 2
    with pytest.raises(refresh.Refused, match="INCOMPLETE"):
        refresh.deliver(tag, layout=layout, processes=no_processes)


def test_deliver_refuses_while_a_dist_program_runs(tmp_path):
    layout = make_tree(tmp_path)
    tag = "20261026-4"
    refresh.build(tag, layout=layout, builder=stand_in_builder(), guard=stand_in_guard())
    running = lambda target: [f"4242 Tesseract.exe {target / 'Tesseract.exe'}"]
    with pytest.raises(refresh.Refused, match="is running"):
        refresh.deliver(tag, layout=layout, processes=running)
    assert (layout.target / "Tesseract.exe").read_bytes() == b"old shell"
    assert not layout.backup(tag).exists() and not layout.stage(tag).parent.exists()


@pytest.mark.parametrize("edit", [
    lambda root: write(root / "ui" / "src" / "pages" / "overview.js", "export const x = 2;\n"),
    lambda root: write(root / "shell" / "app" / "src" / "main.rs", "fn main() { }\n"),
    lambda root: write(root / "ui" / "src" / "pages" / "new.js", "1\n"),
    lambda root: write(root / "bridge_example.py", "X = 2\n"),  # a sidecar component
    lambda root: write(root / "ui" / "css" / "index.css", "body { color: red; }\n"),  # review B M1
    lambda root: write(root / "config.example.yaml", "alerts: {ntfy: {}}\n"),  # review B SHOULD 1
    lambda root: write(root / "locales" / "zh_CN.json", "{\"a\": 1}\n"),
], ids=["ui", "shell", "ui-added", "sidecar", "ui-css", "sidecar-data", "sidecar-locales"])
def test_deliver_refuses_when_the_source_changed_after_the_build(tmp_path, edit):
    layout = make_tree(tmp_path)
    tag = "20261026-5"
    refresh.build(tag, layout=layout, builder=stand_in_builder(), guard=stand_in_guard())
    edit(layout.root)
    with pytest.raises(refresh.Refused, match="source changed after the candidate build"):
        refresh.deliver(tag, layout=layout, processes=no_processes)
    assert (layout.target / "Tesseract.exe").read_bytes() == b"old shell" and not layout.backup(tag).exists()


def test_source_changed_during_the_build_rejects_the_candidate(tmp_path):
    layout = make_tree(tmp_path)
    tag = "20261026-6"
    builder = stand_in_builder(mutate=lambda lay: write(lay.root / "ui" / "src" / "pages" / "overview.js", "changed\n"))
    assert refresh.build(tag, layout=layout, builder=builder, guard=stand_in_guard()) == 1
    assert json.loads((layout.meta(tag) / "state.json").read_text(encoding="utf-8"))["result"] == "candidate_rejected"
    with pytest.raises(refresh.Refused, match="no usable candidate"):
        refresh.deliver(tag, layout=layout, processes=no_processes)


def test_failed_build_is_not_deliverable(tmp_path):
    layout = make_tree(tmp_path)
    tag = "20261026-7"
    assert refresh.build(tag, layout=layout, builder=stand_in_builder(code=1), guard=stand_in_guard()) == 1
    with pytest.raises(refresh.Refused, match="build_failed"):
        refresh.deliver(tag, layout=layout, processes=no_processes)


def test_a_failed_swap_puts_the_old_dist_back(tmp_path, monkeypatch):
    layout = make_tree(tmp_path)
    tag = "20261026-8"
    refresh.build(tag, layout=layout, builder=stand_in_builder(), guard=stand_in_guard())
    stage = layout.stage(tag)
    real_rename = Path.rename

    def rename(self, target):
        if Path(self) == stage:
            raise OSError("simulated: the directory is in use")
        return real_rename(self, target)
    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(refresh.Refused, match="old dist was put back"):
        refresh.deliver(tag, layout=layout, processes=no_processes)
    monkeypatch.setattr(Path, "rename", real_rename)
    assert (layout.target / "Tesseract.exe").read_bytes() == b"old shell"
    assert (layout.dist_root / "run-preview.cmd").is_file() and (layout.dist_root / "preview-build.json").is_file()
    assert not layout.backup(tag).exists()
    assert not layout.stage(tag).parent.exists()  # review B SHOULD 4: no stage left after a rollback


def test_candidate_tampered_after_the_build_is_refused(tmp_path):
    layout = make_tree(tmp_path)
    tag = "20261026-9"
    refresh.build(tag, layout=layout, builder=stand_in_builder(), guard=stand_in_guard())
    write(layout.candidate(tag) / "Tesseract.exe", b"tampered")
    with pytest.raises(refresh.Refused, match="executables changed"):
        refresh.deliver(tag, layout=layout, processes=no_processes)


def test_tag_format_and_the_real_builder_refuses_without_out(tmp_path, monkeypatch):
    layout = make_tree(tmp_path)
    with pytest.raises(refresh.Refused, match="YYYYMMDD-N"):
        refresh.build("latest", layout=layout, builder=stand_in_builder(), guard=stand_in_guard())
    monkeypatch.setattr(refresh, "builder_supports_out", lambda layout: False)
    with pytest.raises(refresh.Refused, match="no --out"):
        refresh.build("20261026-10", layout=layout, builder=refresh.real_builder, guard=stand_in_guard())
    state = json.loads((layout.meta("20261026-10") / "state.json").read_text(encoding="utf-8"))
    assert state["result"] == "build_refused"
    assert (layout.target / "Tesseract.exe").read_bytes() == b"old shell"


def test_real_helpers_are_harmless_here(tmp_path):
    # --help only (no build); a bool either way (False until T-P6-06 adds --out)
    assert isinstance(refresh.builder_supports_out(refresh.Layout(REPO)), bool)
    assert refresh.processes_under(tmp_path) == []


# ============================================================ guard: item 0 (--print-scope)

STUB = r'''
import json, os, sys
app = sys.argv[1]
assert sys.argv[2] == "--print-scope"
mode = os.environ.get("STUB_MODE", "good")
scope = os.environ.get("TESSERACT_INSTANCE_SCOPE")
if scope is None:
    if any(k.upper().startswith("TESSERACT_") for k in os.environ):
        print(json.dumps({"kind": "error", "error": "unexpected TESSERACT variable"})); sys.exit(0)
    names = json.load(open(os.environ["STUB_VECTORS"], encoding="utf-8"))["cases"][0]["names"]
    if mode == "legacy-broken":
        names = dict(names, shell_mutex="Local\\Wrong")
    print(json.dumps({"kind": "default", "id": "default", "config": os.path.join(os.environ["APPDATA"], "Tesseract"),
                      "data": os.path.join(os.environ["LOCALAPPDATA"], "Tesseract"), "names": names,
                      "sidecar": os.path.join(app, "tesseract-runtime.exe")}))
else:
    sys.path.insert(0, os.environ["STUB_REPO"])
    from bridge_instance_scope import custom_scope_id
    config, data = os.environ["TESSERACT_CONFIG_DIR"], os.environ["TESSERACT_DATA_DIR"]
    sid = custom_scope_id(os.path.realpath(config), os.path.realpath(data))
    stem = "Local\\TesseractCustom-" + sid
    print(json.dumps({"kind": "custom", "id": sid, "config": config, "data": data,
                      "names": {"legacy_mutex": stem + "-instance", "restore_event": stem + "-restore",
                                "runtime_mutex": stem + "-runtime", "shell_mutex": stem + "-shell",
                                "quitting_event": stem + "-quitting"},
                      "sidecar": os.path.join(app, "tesseract-runtime.exe")}))
'''


def _scope_stub(tmp_path, monkeypatch, mode):
    stub = write(tmp_path / "stub_print_scope.py", STUB)
    app = tmp_path / "app"
    app.mkdir()
    # the stub reads these; fake_env passes only the allow-list, so extend it for the test
    monkeypatch.setattr(guard, "ENV_ALLOWLIST", guard.ENV_ALLOWLIST + ("STUB_MODE", "STUB_VECTORS", "STUB_REPO"))
    monkeypatch.setenv("STUB_MODE", mode)
    monkeypatch.setenv("STUB_VECTORS", str(REPO / "bridge_schema" / "instance_scope_vectors.json"))
    monkeypatch.setenv("STUB_REPO", str(REPO))
    monkeypatch.setenv("TESSERACT_INSTANCE_SCOPE", "must-not-leak")  # the fake env must drop it
    return guard.check_dist_scope(app, REPO, tmp_path / "out", command=[sys.executable, "-B", str(stub), str(app)])


def test_print_scope_stand_in_passes(tmp_path, monkeypatch):
    result = _scope_stub(tmp_path, monkeypatch, "good")
    assert result["status"] == "PASS", json.dumps(result, ensure_ascii=False, indent=1)
    assert result["details"]["runs"]["default"]["output"]["kind"] == "default"
    assert result["details"]["runs"]["custom"]["output"]["kind"] == "custom"


def test_print_scope_wrong_legacy_names_fail(tmp_path, monkeypatch):
    result = _scope_stub(tmp_path, monkeypatch, "legacy-broken")
    assert result["status"] == "FAIL"
    assert any("names=" in f["what"] for f in result["findings"])


def test_print_scope_judges():
    names = guard.default_names(REPO)
    app, appdata, local = Path("C:/x/app"), Path("C:/t/appdata"), Path("C:/t/local")
    good = {"kind": "default", "id": "default", "config": str(appdata / "Tesseract"), "data": str(local / "Tesseract"),
            "names": names, "sidecar": str(app / "tesseract-runtime.exe"), "error": None}
    assert guard.judge_default_scope(good, appdata=appdata, localappdata=local, app=app, names=names) == []
    dev = dict(good, kind=None, error="explicit_custom_scope_required", config=None, data=None, names=None, sidecar=None)
    problems = guard.judge_default_scope(dev, appdata=appdata, localappdata=local, app=app, names=names)
    assert any("不是分发构建" in p for p in problems) and len(problems) >= 4
    live = dict(good, config="C:/Users/bob/AppData/Roaming/Tesseract")
    assert guard.judge_default_scope(live, appdata=appdata, localappdata=local, app=app, names=names)
    view = guard.scope_view({"scope": {"kind": "default", "id": "default"}, "config_dir": "c", "data_dir": "d",
                             "names": {}, "sidecar": {"error": "sidecar_override_refused"}})
    assert view["kind"] == "default" and view["config"] == "c" and view["error"] == "sidecar_override_refused"


def test_print_scope_is_never_run_on_a_shell_without_it(tmp_path):
    app = tmp_path / "app"
    write(app / "Tesseract.exe", b"MZ no such flag here")
    result = guard.check_dist_scope(app, REPO, tmp_path / "out")
    assert result["status"] == "SKIP" and not (tmp_path / "out" / "print-scope-default.log").exists()


# ============================================================ guard: verdict helpers


def test_smoke_verdict_ignores_only_the_reveal_timeout():
    base = {"run": 1, "hello_ms": 1200, "host_ready_ms": 4000, "exit_code": 0, "sidecar_exit": ["sidecar_exit_0"],
            "failures": ["window not revealed by host_ready before the 1.5 s timeout: window_reveal_timeout waited_ms=1500 ok=true"]}
    assert guard.smoke_verdict({"runs": [base], "failures": [f"run 1: {base['failures'][0]}"]})[0] == []
    for change, word in ((dict(hello_ms=None, failures=["no hello (sidecar_ready) in shell.log"]), "hello"),
                         (dict(host_ready_ms=None), "host_ready"), (dict(exit_code=1), "exit code"),
                         (dict(failures=["process audit failed: residue"]), "residue")):
        problems, _ = guard.smoke_verdict({"runs": [dict(base, **change)], "failures": []})
        assert any(word in p for p in problems), (change, problems)
    problems, _ = guard.smoke_verdict({"runs": [base], "failures": ["host facts changed: a -> b"]})
    assert problems == ["host facts changed: a -> b"]
    assert guard.smoke_verdict({"runs": [], "failures": []})[0] == ["no cold start ran"]


def test_i18n_findings_name_the_key_the_file_and_the_owner():
    report = {"checks": [
        {"id": "backend_keys", "ok": False, "items": [{"id": "errors.bridge.new_code", "msg": "backend message_key missing in zh_CN",
                                                       "sources": ["bridge_x.py:10"]}]},
        {"id": "reason.tiers.reason", "ok": False, "items": [{"id": "tiers.reason.new_reason", "msg": "reason_code has no message"}]},
        {"id": "missing", "ok": False, "items": [{"lang": "de_DE", "id": "errors.bridge.new_code", "why": "missing"}]},
        {"id": "orphans", "ok": False, "items": [{"lang": "fr_FR", "id": "errors.bridge.old_code"}]},
        {"id": "truncation", "ok": False, "items": [{"id": "x"}]},
        {"id": "catalog", "ok": False, "error": "backend_keys.py: boom", "items": []},
    ]}
    found = guard.i18n_findings(report)
    by_key = {f.get("key"): f for f in found if not f.get("langs")}
    assert by_key["errors.bridge.new_code"]["where"].endswith("zh_CN/errors.json") and "后端道" in by_key["errors.bridge.new_code"]["owner"]
    assert "_desc" in by_key["errors.bridge.new_code"]["fix"]
    assert by_key["tiers.reason.new_reason"]["where"].endswith("zh_CN/tiers.json")
    assert any(f.get("langs") == ["de_DE"] for f in found) and any(f.get("langs") == ["fr_FR"] for f in found)
    assert not any(f.get("key") == "x" for f in found)  # truncation is left out
    assert any("没能运行" in f["what"] for f in found)


def test_verdict_and_summary():
    items = [guard.item_result("1", "PASS"), guard.item_result("0", "SKIP", [guard.finding("a", "b", "c")])]
    assert guard.verdict_of(items) == "INCOMPLETE"
    items.append(guard.item_result("2", "FAIL", [guard.finding("缺 key", "加条目", "后端道", where="zh_CN/errors.json", key="errors.k")]))
    assert guard.verdict_of(items) == "FAIL"
    text = guard.summary_markdown({"verdict": "FAIL", "app": "a", "repo": "r", "started_utc": "s", "finished_utc": "f",
                                   "seconds": 1, "items": items})
    assert "errors.k" in text and "谁改：后端道" in text and "--force-ui-risk" in text


# ============================================================ guard: projection of frames (node)


def _fixture_frames() -> list[dict]:
    doc = json.loads((REPO / "ui/test/fixtures/scenarios/rt_connect_replay.json").read_text(encoding="utf-8"))
    return [s["deliver"] for s in doc["steps"] if "deliver" in s and not s["deliver"]["t"].startswith(("rt:", "host:"))]


def _project(tmp_path, frames, queries=(), *, enum_paths=None, baseline: Path | None = None):
    frames_path = tmp_path / "frames.jsonl"
    frames_path.write_text("".join(json.dumps(f) + "\n" for f in frames), encoding="utf-8")
    probe = write(tmp_path / "probe.json", json.dumps({"queries": list(queries), "stopped_unavailable": {},
                                                       **({"enum_paths": enum_paths} if enum_paths else {})}))
    out = tmp_path / "projection.json"
    extra = ["--baseline", str(baseline) if baseline else ""]
    done = subprocess.run(["node", str(REPO / "ui/tools/guard/project_frames.mjs"), "--frames", str(frames_path), "--probe",
                           str(probe), "--out", str(out), *extra], cwd=REPO, capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert out.is_file(), done.stdout + done.stderr
    return done.returncode, json.loads(out.read_text(encoding="utf-8"))


def test_projection_of_fixture_frames_is_clean(tmp_path):
    code, result = _project(tmp_path, _fixture_frames())
    assert code == 0, json.dumps({k: result[k] for k in ("exceptions", "contract_diag", "reconnects", "type_mismatch",
                                                         "enum_unknown", "text_missing")}, ensure_ascii=False)[:3000]


def test_projection_reports_a_type_change_an_unknown_reason_and_a_missing_text(tmp_path):
    frames = _fixture_frames()
    epoch = frames[0]["epoch"]
    for frame in frames:
        if frame.get("t") == "evt" and frame.get("topic") == "tiers.summary":
            frame["data"]["tiers"]["vanguard"]["total"] = str(frame["data"]["tiers"]["vanguard"]["total"])
    frames.append({"t": "evt", "epoch": epoch, "produced_at_ms": 1, "sent_at_ms": 2, "topic": "nodes.list", "key": "vanguard",
                   "kind": "snapshot", "rev": 1, "data": {"items": [{"id": "n1", "name": "n1", "status": "failed",
                                                                      "reason_code": "guard_selftest_reason"}]}})
    queries = [{"cmd": "app.info", "args": {}, "reply": {"t": "res", "epoch": epoch, "produced_at_ms": 1, "sent_at_ms": 2, "id": 9,
                                                        "req_id": "x", "owner": "o", "cmd": "app.info", "ok": False,
                                                        "error": {"code": "guard_selftest_code", "message_key": "errors.guard_selftest_missing",
                                                                  "params": {}}}}]
    code, result = _project(tmp_path, frames, queries)
    assert code == 1
    assert any(m["key"] == "topic:tiers.summary" and m["path"].endswith(".vanguard.total") for m in result["type_mismatch"]), result["type_mismatch"]
    assert any(e["value"] == "guard_selftest_reason" for e in result["enum_unknown"]), result["enum_unknown"]
    assert any(t["kind"] == "error_text_missing" and t["id"] == "errors.guard_selftest_missing" for t in result["text_missing"]), result["text_missing"]
    findings = guard.projection_findings(result)
    assert any("字段类型变了" in f["what"] for f in findings) and any(f.get("key") == "errors.guard_selftest_missing" for f in findings)


# ============================================================ guard: item 5 batch check (Claude ruling 1, 10-26)

INDEX_JS = r"""'use strict';
const { readdirSync } = require('node:fs');
const { join } = require('node:path');
const { pathToFileURL } = require('node:url');
(async () => {
  for (const name of readdirSync(__dirname).filter(n => n.endsWith('.test.mjs')).sort()) {
    await import(pathToFileURL(join(__dirname, name)).href);
  }
})().catch(err => { console.error(err); process.exitCode = 1; });
"""


def _unit_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    unit = tmp_path / "ui" / "test" / "unit"
    write(unit / "index.js", INDEX_JS)
    for name, text in files.items():
        write(unit / name, text)
    return unit


UNIT_A = "import { test } from 'node:test';\ntest('a1', () => {});\nfor (const i of [1, 2]) test('a' + i, { skip: i === 2 }, () => {});\n"
UNIT_B = "import { test, before } from 'node:test';\nbefore(() => {});\ntest('b1', async () => {});\n"


def test_unit_collect_counts_the_cases_index_js_collects_without_running_them(tmp_path):
    unit = _unit_tree(tmp_path, {"a.test.mjs": UNIT_A, "b.test.mjs": UNIT_B + "test('b2', () => { throw new Error('ran'); });\n"})
    data = guard.collect_unit(tmp_path, tmp_path / "out")
    assert data["error"] is None and data["total"] == 5, data
    assert {Path(k).name: v for k, v in data["files"].items()} == {"a.test.mjs": 3, "b.test.mjs": 2}
    # a file that fails to import is reported, never counted silently as fewer files
    write(unit / "c.test.mjs", "import { nothing } from './missing.mjs';\n")
    assert guard.collect_unit(tmp_path, tmp_path / "out2")["error"]


def test_unit_parallel_run_is_checked_against_the_collected_batch(tmp_path):
    _unit_tree(tmp_path, {"a.test.mjs": UNIT_A, "b.test.mjs": UNIT_B})
    result = guard.check_unit(tmp_path, tmp_path / "out")
    assert result["status"] == "PASS", json.dumps(result, ensure_ascii=False)
    d = result["details"]
    assert (d["collected_cases"], d["tests"], d["collected_files"], d["ran_files"]) == (4, 4, 2, 2)


def test_batch_findings_report_any_difference():
    a, b = str(Path("C:/x/ui/test/unit/a.test.mjs")), str(Path("C:/x/ui/test/unit/b.test.mjs"))
    collected = {"files": {a: 3, b: 2}, "total": 5, "error": None}
    same = {"files": {a: {"tests": 3}, b: {"tests": 2}}, "unattributed": 0}
    assert guard.batch_findings(collected, same, [Path(a), Path(b)], full=True) == []
    # subtests are counted apart and never compared (review A SHOULD 1)
    with_subtests = {"files": {a: {"tests": 3, "subtests": 4}, b: {"tests": 2}}, "total": 5, "unattributed": 0}
    assert guard.batch_findings(collected, with_subtests, [Path(a), Path(b)], full=True) == []
    fewer = {"files": {a: {"tests": 3}}, "unattributed": 0}
    found = guard.batch_findings(collected, fewer, [Path(a)], full=True)
    assert len(found) == 1 and "不是同一批" in found[0]["what"] and any("b.test.mjs" in p for p in found[0]["differences"])
    other_count = {"files": {a: {"tests": 2}, b: {"tests": 2}}, "unattributed": 0}
    assert guard.batch_findings(collected, other_count, [Path(a), Path(b)], full=True)
    assert guard.batch_findings({"error": "boom"}, same, [], full=True)[0]["what"].startswith("无法确定")
    assert guard.batch_findings(collected, None, [], full=True)
    # --unit-subset (manual only): only the subset's files are compared
    assert guard.batch_findings(collected, fewer, [Path(a)], full=False) == []


def test_refresh_guard_runs_the_whole_unit_suite_and_the_helper_smoke(tmp_path, monkeypatch):
    seen = {}

    def fake_run_logged(command, log, cwd):
        seen["command"] = [str(c) for c in command]
        return 0
    monkeypatch.setattr(refresh, "run_logged", fake_run_logged)
    layout = refresh.Layout(tmp_path)
    refresh.real_guard(layout, "20261026-1")
    assert "--skip-helper" not in seen["command"] and "--unit-subset" not in seen["command"]
    assert "--only" not in seen["command"]
    # the guard's item 4b passes --skip-helper to the smoke only when asked
    captured = []
    monkeypatch.setattr(guard, "smoke_supports_app", lambda repo: True)
    monkeypatch.setattr(guard, "run_logged", lambda command, log, **kw: (captured.append([str(c) for c in command]), (1, "x"))[1])
    guard.check_shell_smoke(tmp_path, tmp_path, tmp_path / "o1")
    guard.check_shell_smoke(tmp_path, tmp_path, tmp_path / "o2", helper=False)
    assert "--skip-helper" not in captured[0] and "--skip-helper" in captured[1]


# ============================================================ fix round 1 (review A / B, 10-26)


def test_build_rejects_a_candidate_whose_web_ui_is_not_from_the_current_source(tmp_path):
    # review B M2: a reused / stale UI_OUT would pass the snapshot check; the recorded
    # ui_source_digest must equal the current ui/build.mjs sourceDigest
    layout = make_tree(tmp_path)
    tag = "20261026-11"
    assert refresh.build(tag, layout=layout, builder=stand_in_builder(ui_digest="0" * 64), guard=stand_in_guard()) == 1
    state = json.loads((layout.meta(tag) / "state.json").read_text(encoding="utf-8"))
    assert state["result"] == "candidate_rejected" and "not built from the current ui source" in state["error"]
    assert "reuse_shell" not in state


def test_reuse_shell_is_no_longer_offered(tmp_path, monkeypatch):
    with pytest.raises(SystemExit):
        refresh.main(["build", "--tag", "20261026-12", "--reuse-shell"])
    seen = {}

    def fake_run_logged(command, log, cwd):
        seen["command"] = [str(c) for c in command]
        return 0
    monkeypatch.setattr(refresh, "builder_supports_out", lambda layout: True)
    monkeypatch.setattr(refresh, "run_logged", fake_run_logged)
    refresh.real_builder(refresh.Layout(tmp_path), "20261026-12")
    assert "--reuse-shell" not in seen["command"] and "--out" in seen["command"]


@pytest.mark.parametrize("guard_kw", [
    dict(items=("0", "1", "2", "3", "4a", "5")),          # 4b missing
    dict(items=()),                                         # nothing ran, yet "PASS"
    dict(unit_scope="subset"),
    dict(helper_smoke=False),
], ids=["no-4b", "no-items", "unit-subset", "no-helper"])
def test_deliver_refuses_an_incomplete_pass(tmp_path, guard_kw):
    # review B SHOULD 3: a PASS verdict is not enough, the run must be complete
    layout = make_tree(tmp_path)
    tag = "20261026-13"
    refresh.build(tag, layout=layout, builder=stand_in_builder(), guard=stand_in_guard("PASS", **guard_kw))
    with pytest.raises(refresh.Refused, match="not a complete PASS"):
        refresh.deliver(tag, layout=layout, processes=no_processes)
    assert (layout.target / "Tesseract.exe").read_bytes() == b"old shell"
    assert refresh.deliver(tag, layout=layout, processes=no_processes, force_ui_risk=True) == 0
    risk = json.loads((layout.meta(tag) / "refresh.json").read_text(encoding="utf-8"))["ui_risk"]
    assert risk["incomplete"]


def test_deliver_checks_running_programs_again_right_before_the_swap(tmp_path):
    # review B SHOULD 2: renaming works while a program runs, so the second check is the guard
    layout = make_tree(tmp_path)
    tag = "20261026-14"
    refresh.build(tag, layout=layout, builder=stand_in_builder(), guard=stand_in_guard())
    calls = []

    def processes(target):
        calls.append(target)
        return [] if len(calls) == 1 else [f"77 Tesseract.exe {target / 'Tesseract.exe'}"]
    with pytest.raises(refresh.Refused, match="started while the candidate was staged"):
        refresh.deliver(tag, layout=layout, processes=processes)
    assert len(calls) == 2
    assert (layout.target / "Tesseract.exe").read_bytes() == b"old shell"
    assert not layout.backup(tag).exists() and not layout.stage(tag).parent.exists()


@pytest.mark.parametrize("failure", ["attachment-copy", "verify"])
def test_a_failure_after_the_swap_puts_the_old_dist_back(tmp_path, monkeypatch, failure):
    # review B SHOULD 4: the attachment copy and the post-swap check are inside the rollback
    layout = make_tree(tmp_path)
    tag = "20261026-15"
    refresh.build(tag, layout=layout, builder=stand_in_builder(), guard=stand_in_guard())
    if failure == "attachment-copy":
        def copy2(src, dst, *a, **kw):
            raise OSError("simulated: attachment locked")
        monkeypatch.setattr(refresh.shutil, "copy2", copy2)
    else:
        real_file_list = refresh.file_list
        monkeypatch.setattr(refresh, "file_list", lambda root: [] if Path(root) == layout.target else real_file_list(root))
    with pytest.raises(refresh.Refused, match="old dist was put back"):
        refresh.deliver(tag, layout=layout, processes=no_processes)
    monkeypatch.undo()
    assert (layout.target / "Tesseract.exe").read_bytes() == b"old shell"
    assert (layout.target / "_internal" / "old-only.txt").is_file()
    assert (layout.dist_root / "run-preview.cmd").is_file()
    assert json.loads((layout.dist_root / "preview-build.json").read_text(encoding="utf-8")) == {"version": "old"}
    assert not (layout.dist_root / "run-preview-isolated.cmd").exists()
    assert not layout.backup(tag).exists() and not layout.stage(tag).parent.exists()
    state = json.loads((layout.meta(tag) / "state.json").read_text(encoding="utf-8"))
    assert state["result"] == "built"
    assert refresh.deliver(tag, layout=layout, processes=no_processes) == 0  # a retry works


def test_verdict_of_nothing_is_not_a_pass():
    assert guard.verdict_of([]) == "INCOMPLETE"


# ---- item 0 safety (review B SHOULD 7)

CHILD_STUB = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "print(child.pid, flush=True)\n"
    "time.sleep(120)\n"
)


def _alive(pid: int) -> bool:
    import ctypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    handle = kernel.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED
    if not handle:
        return False
    try:
        return kernel.WaitForSingleObject(ctypes.c_void_p(handle), 3000) != 0
    finally:
        kernel.CloseHandle(ctypes.c_void_p(handle))


def test_run_contained_kills_the_whole_tree_on_timeout(tmp_path):
    import os
    stub = write(tmp_path / "tree.py", CHILD_STUB)
    code, text = guard.run_contained([sys.executable, "-B", str(stub)], tmp_path / "log.txt", cwd=tmp_path, timeout=4,
                                     env=dict(os.environ))
    assert code is None and "timeout" in text, text
    child = int(text.split()[0])
    assert not _alive(child), "the grandchild survived the job"


def test_item0_runs_only_a_recorded_distribution_build(tmp_path):
    app = tmp_path / "out" / "Tesseract"
    exe = b"MZ ... --print-scope ..."
    write(app / "Tesseract.exe", exe)
    skip, problems = guard.dist_build_problems(app)
    assert skip and not problems  # no preview-build.json: not run
    info = {"distribution": True, "shell_cargo_features": ["distribution"], "shell_sha256": sha(exe)}
    write(app.parent / "preview-build.json", json.dumps(info))
    assert guard.dist_build_problems(app) == (None, [])
    for change in (dict(distribution=False), dict(shell_cargo_features=[]), dict(shell_sha256="0" * 64)):
        write(app.parent / "preview-build.json", json.dumps({**info, **change}))
        assert guard.dist_build_problems(app)[1], change
    result = guard.check_dist_scope(app, REPO, tmp_path / "o")  # the exe is never started
    assert result["status"] == "FAIL" and result["details"]["ran"] is False
    assert not (tmp_path / "o" / "print-scope-default.log").exists()


# ---- enum coverage (review A MUST 1)


def _probe_enum_paths() -> dict:
    done = subprocess.run([sys.executable, "-B", str(REPO / "ui/tools/guard/sidecar_probe.py"), "--enum-paths", "--repo", str(REPO)],
                          cwd=REPO, capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_enum_paths_come_from_the_target_bridge_protocol():
    data = _probe_enum_paths()
    assert data["topics"]["topic:network.mode"][".proxy_state"] == ["disabled", "other", "owned", "unknown"]
    assert data["topics"]["topic:nodes.list"][".items[].status"]
    assert "res:autostart.state" in data["topics"]
    assert "drift" in data["fields"]["network.mode"][""]
    # review 1 MUST 1: the function-checked P1 topics are part of the enum paths now
    import bridge_protocol
    p1 = data["topics"]
    assert p1["topic:service.state"][".readiness.state"] == sorted(bridge_protocol.READINESS_STATES)
    assert p1["topic:jobs"][".state"] == sorted(bridge_protocol.JOB_STATES)
    assert "service.start" in p1["topic:jobs"][".name"]
    assert "critical" in p1["topic:logs.batch"][".records[].level"] and "MIHOMO" in p1["topic:logs.batch"][".records[].source"]
    assert p1["topic:traffic.history"][".source"] and p1["topic:tiers.summary"][".order[]"] == sorted(bridge_protocol._TIERS)
    assert "stopped" in p1["topic:service.state"][".runtime.startup_phase"] and p1["topic:route.published"][".managed_tier"]
    assert data["uncovered"] == ["core.info", "traffic.tick"] and data["p1_unlisted"] == [] and data["p1_scan_error"] is None


probe_module = _load("ui_guard_sidecar_probe", HERE / "sidecar_probe.py")
if str(REPO) not in sys.path:  # bridge_protocol of this tree (read only)
    sys.path.insert(0, str(REPO))


def _p1_enum_true_fields(source: str, validators: dict) -> set[tuple[str, str]]:
    """Independent of sidecar_probe.p1_enum_scan: every _need / _optional with enum=True in the
    _TOPIC_VALIDATORS functions and the module functions they call -> {(function, field)}."""
    import ast
    tree = ast.parse(source)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    todo, seen, out = [f.__name__ for f in validators.values()], set(), set()
    while todo:
        name = todo.pop()
        if name in seen or name not in funcs:
            continue
        seen.add(name)
        for node in ast.walk(funcs[name]):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                todo.append(node.func.id)
                if node.func.id in ("_need", "_optional") and any(
                        k.arg == "enum" and getattr(k.value, "value", None) is True for k in node.keywords):
                    out.add((name, node.args[1].value))
    return out


def test_p1_enum_table_lists_every_enum_field_of_the_p1_validators():
    # review 1 MUST 1, point 2: a new enum=True field in a P1 validator of bridge_protocol must be
    # added to sidecar_probe.P1_ENUM_PATHS (or, for a shared helper with a path parameter, to
    # P1_ENUM_EXEMPT) -- otherwise the guard would not see its unknown values
    import bridge_protocol
    flagged = _p1_enum_true_fields(Path(bridge_protocol.__file__).read_text(encoding="utf-8"), bridge_protocol._TOPIC_VALIDATORS)
    scan = probe_module.p1_enum_scan(bridge_protocol)
    assert scan["error"] is None
    listed = {(key, path) for key, paths in probe_module.P1_ENUM_PATHS.items() for path in paths}
    scanned = {(key, path) for key, paths in scan["fields"].items() for path in paths}
    assert scanned <= listed, f"P1 enum fields missing from P1_ENUM_PATHS: {sorted(scanned - listed)}"
    assert set(scan["unresolved"]) <= set(probe_module.P1_ENUM_EXEMPT), scan["unresolved"]
    # every enum=True field the independent scan sees is one of the scanned paths or exempt
    # (by multiplicity: two validators flagging the same field name need two scanned paths ending in it,
    # so one same-named field cannot mask another)
    from collections import Counter
    by_field = Counter(path.rsplit(".", 1)[-1].removesuffix("[]") for _, path in scanned)
    need = Counter(field for function, field in flagged if f"{function}.{field}" not in probe_module.P1_ENUM_EXEMPT)
    for field, count in sorted(need.items()):
        assert by_field[field] >= count, (field, count, by_field[field])
    assert len(flagged) >= 10, flagged  # the scan really reads the validators
    # every table entry is a real field (no stale row)
    assert listed <= scanned, f"P1_ENUM_PATHS rows the validators no longer have: {sorted(listed - scanned)}"


STUB_PROTOCOL = '''
def _enum(v, values):
    return v in values
def _need(obj, key, path, predicate, errors, *, enum=False):
    return obj.get(key)
def _optional(obj, key, path, predicate, errors, *, enum=False):
    return None
READINESS_STATES = frozenset({"ready", "degraded"})
JOB_STATES = frozenset({"queued"})
V1_TOPICS = ("service.state", "jobs")
def validate_service_state(value):
    errors = []
    readiness = value.get("readiness")
    _need(readiness, "state", "service.state.readiness", lambda v: _enum(v, READINESS_STATES), errors, enum=True)
    _need(readiness, "mood", "service.state.readiness", lambda v: _enum(v, {"calm", "busy"}), errors, enum=True)
    return errors
def validate_jobs(value):
    errors = []
    _need(value, "state", "jobs", lambda v: _enum(v, JOB_STATES), errors, enum=True)
    return errors
_TOPIC_VALIDATORS = {"service.state": validate_service_state, "jobs": validate_jobs}
'''


def test_a_new_p1_enum_field_is_reported_by_enum_paths(tmp_path):
    # a stand-in tree whose service.state validator gained an enum field the table lacks
    write(tmp_path / "bridge_protocol.py", STUB_PROTOCOL)
    done = subprocess.run([sys.executable, "-B", str(HERE / "sidecar_probe.py"), "--enum-paths", "--repo", str(tmp_path)],
                          cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert done.returncode == 0, done.stderr
    data = json.loads(done.stdout.strip().splitlines()[-1])
    assert data["p1_unlisted"] == ["topic:service.state .readiness.mood"], data
    assert data["topics"]["topic:service.state"][".readiness.state"] == ["degraded", "ready"]
    assert data["topics"]["topic:jobs"][".state"] == ["queued"]


def test_item1_fails_on_an_unlisted_p1_enum_field(tmp_path):
    enum_file = write(tmp_path / "enum_paths.json", json.dumps({"topics": {}}))
    assert enum_file.is_file()
    shapes = {"topics": {}, "uncovered": [], "p1_unlisted": ["topic:service.state .readiness.mood"], "p1_scan_error": None}
    found, details = guard.enum_coverage_findings(REPO, tmp_path, shapes)
    assert details["enum_p1_unlisted"] == 1
    assert [f.get("key") for f in found] == ["p1_unlisted topic:service.state .readiness.mood"]
    assert "P1_ENUM_PATHS" in found[0]["fix"]


def _readiness_frame(epoch, state, rev=99):
    doc = json.loads((REPO / "ui/test/fixtures/scenarios/rt_connect_replay.json").read_text(encoding="utf-8"))
    frame = next(s["deliver"] for s in doc["steps"] if s.get("deliver", {}).get("topic") == "service.state")
    frame = json.loads(json.dumps(frame))
    frame.update(epoch=epoch, rev=rev)
    frame["data"]["readiness"]["state"] = state
    return frame


def test_projection_reports_a_new_readiness_state_from_the_p1_enum_paths(tmp_path):
    # the review 1 injection: READINESS_STATES gains 'degraded' and service.state carries it
    frames = _fixture_frames()
    epoch = frames[0]["epoch"]
    enum_paths = _probe_enum_paths()
    enum_paths["topics"]["topic:service.state"][".readiness.state"].append("degraded")
    _, result = _project(tmp_path, frames + [_readiness_frame(epoch, "degraded")], enum_paths=enum_paths)
    assert ("topic:service.state", ".readiness.state", "degraded") in _untested(result), result["enum_unknown"]
    found = guard.projection_findings(result)
    assert any(f.get("key") == "topic:service.state .readiness.state=degraded" for f in found), found
    # a declared state the fixtures carry is fine
    (tmp_path / "c").mkdir()
    _, clean = _project(tmp_path / "c", frames + [_readiness_frame(epoch, "fail_closed")], enum_paths=_probe_enum_paths())
    assert _untested(clean) == [], clean["enum_unknown"]


def _mode_frame(epoch, proxy_state):
    return {"t": "evt", "epoch": epoch, "produced_at_ms": 1, "sent_at_ms": 2, "topic": "network.mode", "kind": "snapshot", "rev": 99,
            "data": {"mode": "system", "admin": False, "switching": False, "proxy_state": proxy_state, "drift": False}}


def _untested(result):
    return [(e["topic"], e["path"], e["value"]) for e in result["enum_unknown"] if e.get("rule") == "untested_value"]


def test_projection_reports_a_new_enum_value_the_fixtures_never_carry(tmp_path):
    # the review A injection: network.mode.proxy_state gains 'inactive' and the sidecar sends it
    frames = _fixture_frames()
    epoch = frames[0]["epoch"]
    enum_paths = {"topics": {"topic:network.mode": {".proxy_state": ["disabled", "inactive", "other", "owned", "unknown"]}}}
    _, result = _project(tmp_path, frames + [_mode_frame(epoch, "inactive")], enum_paths=enum_paths)
    assert _untested(result) == [("topic:network.mode", ".proxy_state", "inactive")]
    found = guard.projection_findings(result)
    assert any(f.get("key") == "topic:network.mode .proxy_state=inactive" and "从没出现过" in f["what"] for f in found)
    # a value the fixtures carry is fine
    (tmp_path / "c").mkdir()
    _, clean = _project(tmp_path / "c", frames + [_mode_frame(epoch, "disabled")], enum_paths=enum_paths)
    assert _untested(clean) == []
    # a value the baseline lists is known, not a finding
    base = write(tmp_path / "base.json", json.dumps({"gaps": {"topic:network.mode": {".proxy_state": ["inactive"]}}}))
    (tmp_path / "k").mkdir()
    _, known = _project(tmp_path / "k", frames + [_mode_frame(epoch, "inactive")], enum_paths=enum_paths, baseline=base)
    assert _untested(known) == [] and known["info"]["enum_known_untested"][0]["value"] == "inactive"


def _coverage(tmp_path, enum_file: Path, baseline: Path | None, name: str):
    out = tmp_path / f"cov-{name}.json"
    command = ["node", str(REPO / "ui/tools/guard/project_frames.mjs"), "--enum-coverage", str(enum_file), "--out", str(out)]
    if baseline is not False:
        command += ["--baseline", str(baseline) if baseline else ""]
    done = subprocess.run(command, cwd=REPO, capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert out.is_file(), done.stdout + done.stderr
    return done.returncode, json.loads(out.read_text(encoding="utf-8"))


def test_static_enum_coverage_fails_only_on_values_outside_the_baseline(tmp_path):
    enum_file = write(tmp_path / "enum.json", json.dumps({"topics": {
        "topic:network.mode": {".proxy_state": ["disabled", "inactive", "other", "owned", "unknown"]},
        "topic:ui.language": {".language": ["zz_ZZ"]}}}))  # shell-only topic: never the page's
    code, result = _coverage(tmp_path, enum_file, None, "none")
    assert code == 1 and [(n["topic"], n["value"]) for n in result["new"]] == [("topic:network.mode", "inactive")]
    base = write(tmp_path / "base.json", json.dumps({"gaps": {"topic:network.mode": {".proxy_state": ["inactive"]},
                                                              "res:gone": {".x": ["y"]}}}))
    code, result = _coverage(tmp_path, enum_file, base, "base")
    assert code == 0 and result["new"] == [] and result["stale"] == [{"topic": "res:gone", "path": ".x", "value": "y"}]


def test_the_shipped_enum_baseline_leaves_the_current_tree_clean(tmp_path):
    enum_file = write(tmp_path / "enum.json", json.dumps(_probe_enum_paths()))
    code, result = _coverage(tmp_path, enum_file, False, "shipped")  # the default: ui/tools/guard/enum_baseline.json
    assert code == 0, result["new"][:5]


def test_projection_text_chain_is_the_pages_own(tmp_path):
    # review A SHOULD 4: errors.js errorText decides; message_key missing but errors.<code> present
    # is no finding (the page shows errors.<code>), both missing is one
    frames = _fixture_frames()
    epoch = frames[0]["epoch"]

    def reply(code):
        return {"cmd": "app.info", "args": {}, "reply": {"t": "res", "epoch": epoch, "produced_at_ms": 1, "sent_at_ms": 2, "id": 9,
                                                          "req_id": "x", "owner": "o", "cmd": "app.info", "ok": False,
                                                          "error": {"code": code, "message_key": "errors.guard_selftest_missing", "params": {}}}}
    _, result = _project(tmp_path, frames, [reply("validation")])
    assert not [t for t in result["text_missing"] if t["kind"] == "error_text_missing"], result["text_missing"]
    (tmp_path / "b").mkdir()
    _, result = _project(tmp_path / "b", frames, [reply("guard_selftest_code")])
    miss = [t for t in result["text_missing"] if t["kind"] == "error_text_missing"]
    assert miss and miss[0]["chain"] == ["errors.guard_selftest_missing", "errors.guard_selftest_code"]


# ---- item 1: the removed field is named (review A SHOULD 3)


def test_unknown_topic_problem_names_the_extra_field(tmp_path):
    frame = {"t": "evt", "topic": "network.mode", "kind": "snapshot", "data": {"mode": "system", "admin": False, "switching": False,
                                                                              "proxy_state": "owned", "drift": False}}
    write(tmp_path / "ui/test/fixtures/scenarios/x.json", json.dumps({"steps": [{"deliver": frame}]}))
    fields = {"network.mode": {"": ["admin", "mode", "proxy_state", "switching"]}}
    assert guard.unknown_fields(tmp_path, "unknown:network.mode", ["x.json"], fields) == ("network.mode", ["drift"])
    rows = {"t": "evt", "topic": "nodes.list", "kind": "patch", "data": {"changed": [{"id": "a", "extra": 1}], "removed": []}}
    write(tmp_path / "ui/test/fixtures/scenarios/y.json", json.dumps({"frames": [{"t": "batch", "items": [rows]}]}))
    fields = {"nodes.list": {"": ["items"], ".items[]": ["id"]}}
    assert guard.unknown_fields(tmp_path, "unknown:nodes.list.items[0]", ["y.json"], fields) == ("nodes.list", ["extra"])


# ---- item 2: a mistaken zh_CN deletion has one fix (review A SHOULD 2)


def test_a_zh_cn_entry_the_backend_still_sends_is_restored_not_deleted_everywhere():
    ident = "errors.autostart.development_mode"
    report = {"checks": [
        {"id": "frozen", "ok": False, "items": [{"id": ident, "msg": "frozen id was deleted from zh_CN (update frozen.json ...)"}]},
        {"id": "backend_keys", "ok": False, "items": [{"id": ident, "msg": "backend message_key missing in zh_CN"}]},
        {"id": "orphans", "ok": False, "items": [{"lang": lang, "id": ident} for lang in ("en_US", "de_DE")]},
    ]}
    found = guard.i18n_findings(report)
    assert len(found) == 1, found
    fix = found[0]["fix"]
    assert "恢复" in fix and "不要删除" in fix and "git show HEAD:ui/src/i18n/locales/zh_CN/errors.json" in fix
    # a frozen deletion the backend does not send keeps its own (non-contradicting) fix
    alone = guard.i18n_findings({"checks": [report["checks"][0]]})
    assert len(alone) == 1 and "frozen.json" in alone[0]["fix"] and "误删" in alone[0]["fix"]


# ---- item 5: subtests are not a different batch (review A SHOULD 1)


def test_unit_batch_check_with_subtests_and_suites(tmp_path):
    nested = ("import { test, describe, it } from 'node:test';\n"
              "describe('g', () => { it('g1', () => {}); describe('h', () => { it('h1', () => {}); }); });\n"
              "test('c1', async t => { await t.test('sub', () => {}); "
              "await t.test('sub2', async t2 => { await t2.test('s', () => {}); }); });\n")
    _unit_tree(tmp_path, {"a.test.mjs": UNIT_A, "c.test.mjs": nested})
    result = guard.check_unit(tmp_path, tmp_path / "out")
    assert result["status"] == "PASS", json.dumps(result, ensure_ascii=False)
    d = result["details"]
    assert (d["collected_cases"], d["ran_cases"], d["subtests"]) == (6, 6, 3)


def test_p1_declared_values_union_the_constant_and_the_validator_expression():
    # review 2 SHOULD 1: a validator that widens the table's constant (CONST | {"new"}) must not
    # hide the new value from item 1's static coverage check
    import types
    protocol = types.SimpleNamespace(READINESS_STATES=frozenset({"ready", "fail_closed"}))
    widened = probe_module._p1_declared(protocol, "READINESS_STATES", 'READINESS_STATES | {"degraded"}')
    assert widened == ["degraded", "fail_closed", "ready"]
    assert probe_module._p1_declared(protocol, "READINESS_STATES", None) == ["fail_closed", "ready"]
    assert probe_module._p1_declared(protocol, None, '{"a", "b"}') == ["a", "b"]
    assert probe_module._p1_declared(protocol, "MISSING", "undefined_name | {1}") == []
