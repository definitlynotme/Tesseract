"""UI guard (T-P6-05): fast, deterministic, isolated, offline checks that catch the UI problems a
backend change can cause, before anyone opens a new-UI dist.

    python -B scripts/ui_guard.py [--app <dir with Tesseract.exe>] [--out <dir>] [--only 0,1,2,3,4a,4b,5]
                                  [--repo <tree>] [--source-sidecar] [--unit-subset] [--serial] [--skip-helper]

`.build/claude_shell_dist_refresh.py build` runs it on every candidate; it also runs on its own
(default --app: dist/Tesseract, the official dist since the 2026-11-01 cut-over). Items:

  0   dist scope      `Tesseract.exe --print-scope` in a FAKE environment (APPDATA / LOCALAPPDATA in a
                      temp dir, no TESSERACT_* variable): kind=default, directories under the fake
                      profile, the legacy names, the sidecar next to the shell; then once in a custom
                      environment. Only run when the shell has --print-scope (T-P6-06) and
                      preview-build.json says it is the distribution build of this very exe; run in a
                      kill-on-close job object with a short timeout. The dist exe never runs with the
                      real environment and never starts in the default scope.
  1   protocol sync   ui/tools/gen_rt_spec.py --check; ui/tools/validate_ui_fixtures.py (an unknown
                      field is named); the enum coverage: every declared enum value of bridge_protocol
                      (page topics, query replies, and the function-checked P1 topics through
                      sidecar_probe.P1_ENUM_PATHS) the UI fixtures never carry and
                      ui/tools/guard/enum_baseline.json does not list is a FAIL (a new backend value);
                      a P1 enum field the ast scan finds that P1_ENUM_PATHS lacks is a FAIL too
  2   backend texts   ui/tools/i18n_check.mjs (backend_keys, catalog, reason.*, missing, orphans and the
                      other hard checks; truncation, glossary, id_format and three_catalogs left out)
  3   sidecar probe   ui/tools/guard/sidecar_probe.py on the dist's frozen sidecar (isolated custom scope,
                      host facts before / after), then ui/tools/guard/project_frames.mjs feeds the real
                      frames to the page's runtime layer (src/rt) in node; a real value on an enum
                      field that the UI fixtures never carry (and the baseline does not list) is a FAIL
  4a  render smoke    node ui/test/render/run.mjs --build-flags "--i18n-trace" --case smoke
                      --langs zh_CN,en_US,pseudo (a private design build; never ui/dist)
  4b  shell smoke     scripts/build_shell_preview.py smoke --runs 1 [--app <dir>]: custom scope, isolation
                      checked first, the sidecar's --validator-helper-smoke 16 (as the Qt refresh does;
                      --skip-helper leaves it out, for a manual run only), then one cold start of the
                      shell with the frozen sidecar
  5   UI unit tests   every ui/test/unit/*.test.mjs, as separate node --test processes (4 at a time: the
                      same tests as `node --test ui/test/unit/`, which imports them one by one in one
                      process and takes ~7 min; ~3 min this way). The files and the cases run are
                      checked against what `node --test ui/test/unit/` collects (ui/tools/guard/
                      unit_collect.mjs, no test run; subtests are not cases): any difference is a
                      FAIL. --unit-subset (manual
                      runs only) runs only the files about the protocol, the projection and the texts

Writes <out>/guard.json and <out>/摘要.md (one page, Chinese: PASS / FAIL per item, and for each FAIL the
file, the key and who fixes it). Exit 0 = every item PASS, 1 = a FAIL, 3 = no FAIL but an item was
skipped (incomplete). Never writes the source tree, ui/dist, dist, dist-shell or the live profile.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_REPO = Path(__file__).resolve().parents[1]
GUARD_TOOLS = DEFAULT_REPO / "ui" / "tools" / "guard"  # the guard's own helpers (also for a --repo copy)
ITEMS = ("0", "1", "2", "3", "4a", "4b", "5")
NAMES = {"0": "分发构建核对（--print-scope）", "1": "协议与生成物同步", "2": "后端文字键的目录覆盖",
         "3": "真实侧车契约探针", "4a": "界面冒烟（渲染矩阵 smoke）", "4b": "界面冒烟（外壳 + 冻结侧车冷启动）",
         "5": "UI 单测"}
# i18n_check checks the guard leaves out (ticket: truncation needs the whole matrix; the other three
# are report-level style checks, not backend coverage).
I18N_EXCLUDED = {"truncation", "glossary", "id_format", "three_catalogs"}
# UI unit test files about the protocol, the projection and the texts that need no browser and no
# build (--unit-subset; the default runs every file). See the T-P6-05 report for the timing.
UNIT_SUBSET = ("rt_calls", "rt_features", "rt_h2", "rt_project", "rt_reset", "rt_stale", "rt_topics",
               "rt_transport", "overview_data", "i18n_gate", "i18n_fmt", "i18n_icu", "i18n_runtime",
               "logs_buffer", "alerts_form", "shellc_banners", "shellc_install", "shellc_notify",
               "shellc_shell")
# The environment a dist exe may see in item 0 (no TESSERACT_*, no proxy, no tokens).
ENV_ALLOWLIST = ("SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PATH", "PATHEXT", "COMSPEC", "USERNAME", "USERDOMAIN",
                 "COMPUTERNAME", "OS", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
                 "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)", "COMMONPROGRAMW6432", "NUMBER_OF_PROCESSORS",
                 "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "PROCESSOR_LEVEL", "PROCESSOR_REVISION")
REVEAL_TIMEOUT = "window not revealed by host_ready before the 1.5 s timeout"

OWNER_BACKEND = "后端道（改后端的会话或用户）"
OWNER_CLAUDE = "Claude（迁移道）"
OWNER_SHELL = "外壳单（T-P6-06 或后续外壳单）"


def utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def python_exe(repo: Path) -> str:
    venv = repo / ".venv" / "Scripts" / "python.exe"
    if venv.is_file():
        return str(venv)
    main_venv = DEFAULT_REPO / ".venv" / "Scripts" / "python.exe"
    return str(main_venv) if main_venv.is_file() else sys.executable


def run_logged(command: list, log: Path, *, cwd: Path, timeout: float, env: dict | None = None) -> tuple[int | None, str]:
    """Run, write stdout+stderr to log; -> (exit code or None on timeout, output). An explicit `env`
    is passed exactly as given (item 0's fake environment gets nothing added)."""
    log.parent.mkdir(parents=True, exist_ok=True)
    if env is None:
        base = dict(os.environ)
        base.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        base.setdefault("PYTHONIOENCODING", "utf-8")
        base["NO_COLOR"] = "1"
        # node tools that start Python (i18n_check -> backend_keys.py) use this one, so a tree
        # without its own .venv (an injection copy) still runs
        base.setdefault("TESSERACT_UI_PYTHON", python_exe(Path(cwd)))
    else:
        base = dict(env)
    try:
        done = subprocess.run([str(c) for c in command], cwd=cwd, env=base, capture_output=True, timeout=timeout)
        code, raw = done.returncode, done.stdout + done.stderr
    except subprocess.TimeoutExpired as exc:
        code, raw = None, (exc.stdout or b"") + (exc.stderr or b"") + f"\n[ui_guard] timeout after {timeout} s".encode()
    text = raw.decode("utf-8", errors="replace")
    log.write_text(text, encoding="utf-8")
    return code, text


def finding(what: str, fix: str, owner: str, where: str | None = None, **extra) -> dict:
    out = {"what": what, "fix": fix, "owner": owner}
    if where:
        out["where"] = where
    out.update(extra)
    return out


def item_result(item: str, status: str, findings: list | None = None, **details) -> dict:
    return {"id": item, "name": NAMES[item], "status": status, "findings": findings or [], "details": details}


def tail(text: str, lines: int = 15) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


# ============================================================================ item 0


PRINT_SCOPE_TIMEOUT = 8.0  # --print-scope prints one line and exits; anything longer is killed with its tree


def run_contained(command: list, log: Path, *, cwd: Path, timeout: float, env: dict) -> tuple[int | None, str]:
    """run_logged for a dist exe (item 0): started suspended inside a job object with
    KILL_ON_JOB_CLOSE, resumed only once it is in the job, so the whole process tree dies on timeout
    or when the guard ends (subprocess.run's timeout kills only the direct child; review B SHOULD 7).
    Fails closed: if the job cannot be set up the program is not resumed."""
    import ctypes
    from ctypes import wintypes
    log.parent.mkdir(parents=True, exist_ok=True)

    class IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("r_ops", "w_ops", "o_ops", "r_bytes", "w_bytes", "o_bytes")]

    class BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        text = f"[ui_guard] not started: CreateJobObjectW failed ({ctypes.get_last_error()})"
        log.write_text(text, encoding="utf-8")
        return None, text
    proc = None
    try:
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            text = f"[ui_guard] not started: SetInformationJobObject failed ({ctypes.get_last_error()})"
            log.write_text(text, encoding="utf-8")
            return None, text
        proc = subprocess.Popen([str(c) for c in command], cwd=cwd, env=dict(env), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, creationflags=0x4 | 0x08000000)  # CREATE_SUSPENDED | NO_WINDOW
        if not kernel.AssignProcessToJobObject(job, int(proc._handle)):
            proc.kill()
            proc.communicate()
            text = f"[ui_guard] not resumed: AssignProcessToJobObject failed ({ctypes.get_last_error()})"
            log.write_text(text, encoding="utf-8")
            return None, text
        ntdll.NtResumeProcess(int(proc._handle))
        try:
            out, err = proc.communicate(timeout=timeout)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            kernel.TerminateJobObject(job, 1)
            out, err = proc.communicate()
            code, err = None, (err or b"") + f"\n[ui_guard] timeout after {timeout} s: the job (every process) was killed".encode()
        text = ((out or b"") + (err or b"")).decode("utf-8", errors="replace")
        log.write_text(text, encoding="utf-8")
        return code, text
    finally:
        if proc is not None and proc.poll() is None:
            kernel.TerminateJobObject(job, 1)
        kernel.CloseHandle(job)


def dist_build_problems(app: Path) -> tuple[str | None, list[str]]:
    """Before running a dist exe: preview-build.json next to the dist must say it is the distribution
    build (`"distribution": true`, cargo feature "distribution") of this very Tesseract.exe. ->
    (skip reason or None, problems)."""
    info_path = app.parent / "preview-build.json"
    if not info_path.is_file():
        return f"没有 {info_path}（无法确认这是分发构建）：不运行它", []
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return None, [f"{info_path} 不是合法 JSON：{exc}"]
    problems = []
    if info.get("distribution") is not True:
        problems.append(f"preview-build.json 没有 \"distribution\": true（{info.get('distribution')!r}）")
    if "distribution" not in (info.get("shell_cargo_features") or []):
        problems.append(f"preview-build.json 的 shell_cargo_features 没有 distribution（{info.get('shell_cargo_features')!r}）")
    exe = app / "Tesseract.exe"
    if exe.is_file():
        import hashlib
        digest = hashlib.sha256(exe.read_bytes()).hexdigest()
        if info.get("shell_sha256") != digest:
            problems.append(f"preview-build.json 的 shell_sha256 与 {exe} 不一致（这不是记录里的那份外壳）")
    return None, problems


def print_scope_supported(app: Path) -> tuple[bool, str]:
    """Never start a shell that does not know --print-scope (it would try to run the app). The flag is
    a literal in the binary when the shell parses it (T-P6-06 section 5)."""
    exe = app / "Tesseract.exe"
    if not exe.is_file():
        return False, f"缺少 {exe}"
    data = exe.read_bytes()
    if b"--print-scope" not in data:
        return False, "这份外壳不认识 --print-scope（T-P6-06 之前的构建）：不运行它"
    return True, ""


def fake_env(root: Path, *, custom: dict | None = None) -> dict:
    """An allow-listed environment whose profile directories are all under `root` (a temp dir)."""
    env = {k: v for k, v in os.environ.items() if k.upper() in ENV_ALLOWLIST}
    for name in ("appdata", "localappdata", "temp", "home"):
        (root / name).mkdir(parents=True, exist_ok=True)
    env.update(APPDATA=str(root / "appdata"), LOCALAPPDATA=str(root / "localappdata"), TEMP=str(root / "temp"),
               TMP=str(root / "temp"), USERPROFILE=str(root / "home"))
    if custom:
        env.update(custom)
    return env


def env_problems(env: dict, temp_root: Path, *, custom: bool) -> list[str]:
    problems = []
    for key in ("APPDATA", "LOCALAPPDATA", "TEMP", "USERPROFILE"):
        path = Path(env.get(key, "") or ".").resolve()
        if temp_root.resolve() not in path.parents:
            problems.append(f"{key} 不在临时目录下：{path}")
    keys = {k.upper() for k in env}
    if "TESSERACT_ISOLATED_AUDIT_MANIFEST" in keys:
        problems.append("设了 TESSERACT_ISOLATED_AUDIT_MANIFEST")
    scope_keys = {"TESSERACT_INSTANCE_SCOPE", "TESSERACT_CONFIG_DIR", "TESSERACT_DATA_DIR"} & keys
    if not custom and scope_keys:
        problems.append(f"伪造的默认环境里不得有 {sorted(scope_keys)}")
    if custom:
        for key in ("TESSERACT_CONFIG_DIR", "TESSERACT_DATA_DIR"):
            path = Path(env.get(key, "") or ".").resolve()
            if temp_root.resolve() not in path.parents:
                problems.append(f"{key} 不在临时目录下：{path}")
    return problems


def _pick(obj: dict, *paths):
    for path in paths:
        cur = obj
        for part in (path if isinstance(path, tuple) else (path,)):
            if not isinstance(cur, dict) or part not in cur:
                cur = None
                break
            cur = cur[part]
        if cur is not None:
            return cur
    return None


def parse_scope_output(text: str) -> dict | None:
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def _same_path(a, b) -> bool:
    if a is None or b is None:
        return False
    norm = lambda p: os.path.normcase(os.path.normpath(str(p).replace("\\\\?\\", "")))
    return norm(a) == norm(b)


def scope_view(obj: dict) -> dict:
    """The --print-scope fields under the names T-P6-06 section 5 lists (kind, id, config, data, the five
    names, the sidecar path or the refusal code); a few spellings are accepted until the format is fixed."""
    sidecar = _pick(obj, "sidecar", "sidecar_path", ("sidecar", "path"))
    error = _pick(obj, "error", "code", "error_code", "sidecar_error", ("sidecar", "error"))
    if isinstance(sidecar, dict):
        error = error or sidecar.get("error") or sidecar.get("code")
        if sidecar.get("exists") is False:  # T-P6-06 prints {exists, mode, path}
            error = error or "sidecar_missing"
            sidecar = None
        else:
            sidecar = sidecar.get("path")
    return {"kind": _pick(obj, "kind", ("scope", "kind")), "id": _pick(obj, "id", ("scope", "id")),
            "config": _pick(obj, "config", "config_dir", ("dirs", "config"), ("scope", "config")),
            "data": _pick(obj, "data", "data_dir", ("dirs", "data"), ("scope", "data")),
            "names": _pick(obj, "names", ("scope", "names")), "sidecar": sidecar, "error": error}


def default_names(repo: Path) -> dict:
    vectors = json.loads((repo / "bridge_schema" / "instance_scope_vectors.json").read_text(encoding="utf-8"))
    return next(case["names"] for case in vectors["cases"] if case["kind"] == "default")


def custom_names(scope_id: str) -> dict:
    stem = f"Local\\TesseractCustom-{scope_id}"
    return {"legacy_mutex": f"{stem}-instance", "restore_event": f"{stem}-restore", "runtime_mutex": f"{stem}-runtime",
            "shell_mutex": f"{stem}-shell", "quitting_event": f"{stem}-quitting"}


def judge_default_scope(view: dict, *, appdata: Path, localappdata: Path, app: Path, names: dict) -> list[str]:
    problems = []
    if view["kind"] != "default":
        problems.append(f"kind={view['kind']!r}（应为 default；error={view['error']!r}）：这不是分发构建，或默认作用域判定错了")
    if view["id"] not in (None, "default"):
        problems.append(f"id={view['id']!r}（应为 default）")
    if not _same_path(view["config"], appdata / "Tesseract"):
        problems.append(f"config={view['config']!r}（应为伪造的 {appdata / 'Tesseract'}）")
    if not _same_path(view["data"], localappdata / "Tesseract"):
        problems.append(f"data={view['data']!r}（应为伪造的 {localappdata / 'Tesseract'}）")
    if view["names"] != names:
        problems.append(f"names={view['names']!r}（应为旧名字 {names}）")
    if not _same_path(view["sidecar"], app / "tesseract-runtime.exe"):
        problems.append(f"sidecar={view['sidecar']!r}（应为同目录的 {app / 'tesseract-runtime.exe'}）")
    return problems


def judge_custom_scope(view: dict, *, config: Path, data: Path, app: Path, scope_id: str | None) -> list[str]:
    problems = []
    if view["kind"] != "custom":
        problems.append(f"kind={view['kind']!r}（应为 custom；error={view['error']!r}）")
    if scope_id and view["id"] != scope_id:
        problems.append(f"id={view['id']!r}（按 bridge_instance_scope.custom_scope_id 应为 {scope_id}）")
    if not _same_path(view["config"], config):
        problems.append(f"config={view['config']!r}（应为 {config}）")
    if not _same_path(view["data"], data):
        problems.append(f"data={view['data']!r}（应为 {data}）")
    if scope_id and view["names"] != custom_names(scope_id):
        problems.append(f"names={view['names']!r}（应为 {custom_names(scope_id)}）")
    if not _same_path(view["sidecar"], app / "tesseract-runtime.exe"):
        problems.append(f"sidecar={view['sidecar']!r}（应为同目录的 tesseract-runtime.exe）")
    return problems


def check_dist_scope(app: Path, repo: Path, out: Path, *, command: list | None = None) -> dict:
    """Item 0. `command` replaces [Tesseract.exe] in tests (a stand-in that prints a scope line)."""
    if command is None:
        ok, why = print_scope_supported(app)
        if not ok:
            return item_result("0", "SKIP", [finding(f"未运行：{why}", "等 T-P6-06 的分发构建（--features distribution，带 --print-scope）",
                                                     OWNER_SHELL)], supported=False)
        skip, problems = dist_build_problems(app)
        if skip:
            return item_result("0", "SKIP", [finding(f"未运行：{skip}", "用 build_shell_preview.py build 产出的分发构建（带 preview-build.json）",
                                                     OWNER_SHELL)], supported=True, ran=False)
        if problems:
            return item_result("0", "FAIL", [finding(f"未运行 --print-scope：{p}", "build_shell_preview.py 组装时写入（T-P6-06 §6），"
                                                     "并确认 dist 是 --features distribution 的构建", OWNER_SHELL,
                                                     where=str(app.parent / "preview-build.json")) for p in problems],
                               supported=True, ran=False)
        command = [str(app / "Tesseract.exe")]
    temp_root = Path(tempfile.gettempdir())
    tmp = Path(tempfile.mkdtemp(prefix="tesseract-ui-guard-scope-"))
    findings: list = []
    details: dict = {"supported": True, "runs": {}}
    try:
        names = default_names(repo)
        # 1. fake default environment
        root = tmp / "default"
        env = fake_env(root)
        problems = env_problems(env, temp_root, custom=False)
        if problems:
            return item_result("0", "FAIL", [finding(f"伪造环境不合格，未运行：{problems}", "修 scripts/ui_guard.py fake_env", OWNER_CLAUDE)])
        code, text = run_contained([*command, "--print-scope"], out / "print-scope-default.log", cwd=root,
                                   timeout=PRINT_SCOPE_TIMEOUT, env=env)
        obj = parse_scope_output(text)
        details["runs"]["default"] = {"exit": code, "output": obj if obj is not None else tail(text)}
        if code != 0 or obj is None:
            findings.append(finding(f"默认环境下 --print-scope 退出码 {code}，输出不是一行 JSON：{tail(text, 5)}",
                                    "外壳的 --print-scope 应只打印一行 JSON 并以 0 退出（T-P6-06 §5）", OWNER_SHELL))
        else:
            for problem in judge_default_scope(scope_view(obj), appdata=root / "appdata", localappdata=root / "localappdata",
                                               app=app, names=names):
                findings.append(finding(f"默认作用域：{problem}", "修 shell/app/src/main.rs 的作用域判定 / 侧车定位（T-P6-06 §2–3）",
                                        OWNER_SHELL, where="Tesseract.exe --print-scope（伪造的默认环境）"))
        created = [str(p) for p in (root / "appdata" / "Tesseract", root / "localappdata" / "Tesseract") if p.exists()]
        if created:
            findings.append(finding(f"--print-scope 建了目录：{created}", "--print-scope 不得建目录（T-P6-06 §5）", OWNER_SHELL))
        # 2. custom environment
        root = tmp / "custom"
        config, data = root / "scope" / "config", root / "scope" / "data"
        config.mkdir(parents=True)
        data.mkdir(parents=True)
        env = fake_env(root, custom={"TESSERACT_INSTANCE_SCOPE": "ui-guard-scope", "TESSERACT_CONFIG_DIR": str(config),
                                     "TESSERACT_DATA_DIR": str(data)})
        problems = env_problems(env, temp_root, custom=True)
        if problems:
            findings.append(finding(f"custom 环境不合格，未运行：{problems}", "修 scripts/ui_guard.py fake_env", OWNER_CLAUDE))
        else:
            code, text = run_contained([*command, "--print-scope"], out / "print-scope-custom.log", cwd=root,
                                       timeout=PRINT_SCOPE_TIMEOUT, env=env)
            obj = parse_scope_output(text)
            details["runs"]["custom"] = {"exit": code, "output": obj if obj is not None else tail(text)}
            scope_id = None
            try:
                if str(repo) not in sys.path:
                    sys.path.insert(0, str(repo))
                from bridge_instance_scope import custom_scope_id
                scope_id = custom_scope_id(config.resolve(), data.resolve())
            except Exception as exc:  # the id check is skipped, the rest still runs
                details["custom_id_error"] = repr(exc)
            if code != 0 or obj is None:
                findings.append(finding(f"custom 环境下 --print-scope 退出码 {code}：{tail(text, 5)}",
                                        "外壳的 --print-scope 应只打印一行 JSON 并以 0 退出", OWNER_SHELL))
            else:
                for problem in judge_custom_scope(scope_view(obj), config=config.resolve(), data=data.resolve(), app=app,
                                                  scope_id=scope_id):
                    findings.append(finding(f"custom 作用域：{problem}", "修 shell/app/src/main.rs 的作用域判定（T-P6-06 §2）",
                                            OWNER_SHELL, where="Tesseract.exe --print-scope（custom 环境）"))
        build_info = app.parent / "preview-build.json"
        if build_info.is_file():
            details["preview_build_distribution"] = json.loads(build_info.read_text(encoding="utf-8")).get("distribution")
        return item_result("0", "FAIL" if findings else "PASS", findings, **details)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================================ item 1


def protocol_shapes(repo: Path, out: Path) -> tuple[dict | None, str | None]:
    """The enum fields and the declared object keys of `repo`'s bridge_protocol, from
    ui/tools/guard/sidecar_probe.py --enum-paths (a subprocess: the target tree is never imported
    here). -> (data, error)."""
    path = out / "enum_paths.json"
    code, text = run_logged([python_exe(repo), "-B", repo / "ui/tools/guard/sidecar_probe.py", "--enum-paths", "--repo", repo],
                            out / "enum_paths.log", cwd=repo, timeout=120)
    for line in reversed(text.splitlines()):
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except ValueError:
                break
            path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            return data, None
    return None, f"sidecar_probe.py --enum-paths 退出码 {code}：{tail(text, 5)}"


def _fixture_topic_data(doc, topic: str):
    """The event data of `topic` in one UI fixture document (list / keyed patches as {"items": changed})."""
    steps = doc if isinstance(doc, list) else (doc.get("steps") or []) if isinstance(doc, dict) else []
    frames = list((doc.get("frames") or []) + (doc.get("replay") or [])) if isinstance(doc, dict) else []
    frames += [s["deliver"] for s in steps if isinstance(s, dict) and isinstance(s.get("deliver"), dict)]
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        items = [frame] if frame.get("t") == "evt" else (frame.get("items") or []) if frame.get("t") == "batch" else []
        for item in items:
            if isinstance(item, dict) and item.get("t") == "evt" and item.get("topic") == topic and item.get("data") is not None:
                data = item["data"]
                if item.get("kind") == "patch" and isinstance(data, dict) and isinstance(data.get("changed"), list):
                    data = {"items": data["changed"]}
                yield data


def _objects_at(value, tokens: list[str]):
    if not tokens:
        if isinstance(value, dict):
            yield value
        return
    head, rest = tokens[0], tokens[1:]
    if head == "[]":
        for item in value if isinstance(value, list) else ():
            yield from _objects_at(item, rest)
    elif isinstance(value, dict) and head[1:] in value:
        yield from _objects_at(value[head[1:]], rest)


def unknown_fields(repo: Path, message: str, files: list[str], fields: dict) -> tuple[str | None, list[str]]:
    """For a fixture problem `unknown:<topic>[.<path>]` (bridge_protocol names only the object): the
    keys the UI fixtures carry there that the schema no longer declares (review A SHOULD 3).
    -> (topic, sorted extra keys)."""
    where = message.removeprefix("unknown:")
    topic = max((t for t in fields if where == t or where.startswith(t + ".") or where.startswith(t + "[")), key=len, default=None)
    if topic is None:
        return None, []
    container = re.sub(r"\[\d+\]", "[]", where[len(topic):])
    declared = set((fields.get(topic) or {}).get(container) or ())
    tokens = re.findall(r"\.[^.\[]+|\[\]", container)
    seen: set = set()
    for name in files:
        try:
            doc = json.loads((repo / "ui/test/fixtures/scenarios" / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for data in _fixture_topic_data(doc, topic):
            for obj in _objects_at(data, tokens):
                seen |= set(obj)
    return topic, sorted(seen - declared) if declared or container == "" else []


ENUM_FIX = ("Claude：在 ui/src/rt 或页面里给这个值加映射与文案，并在 ui/test/fixtures/scenarios 补一个带该值的场景；"
            "若是误加 / 误发，后端道改回。只有经 Claude 裁定接受这个缺口时，才把它写进 ui/tools/guard/enum_baseline.json")


def enum_coverage_findings(repo: Path, out: Path, shapes: dict) -> tuple[list[dict], dict]:
    """Item 1, static enum coverage (review A MUST 1, point 3): declared enum values the UI fixtures
    never carry and the baseline does not list."""
    enum_file = out / "enum_paths.json"
    result_path = out / "enum_coverage.json"
    code, text = run_logged(["node", repo / "ui/tools/guard/project_frames.mjs", "--enum-coverage", enum_file, "--out", result_path],
                            out / "enum_coverage.log", cwd=repo, timeout=300)
    if not result_path.is_file():
        return [finding(f"枚举覆盖检查没有产出结果（退出码 {code}）：{tail(text, 5)}", "看 item1/enum_coverage.log", OWNER_CLAUDE)], {}
    result = json.loads(result_path.read_text(encoding="utf-8"))
    findings = [finding(f"bridge_protocol 声明的枚举 {n['topic']} {n['path']} 有前端 fixture 从没出现过的值 {n['value']!r}"
                        f"（页面没按这个值测过；fixture 里有 {n.get('tested')}）", ENUM_FIX, f"{OWNER_CLAUDE} / {OWNER_BACKEND}",
                        where=n.get("file"), key=f"{n['topic']} {n['path']}={n['value']}") for n in result.get("new", [])]
    # review 1 MUST 1: the P1 topics are checked by functions; sidecar_probe.P1_ENUM_PATHS lists their
    # enum fields and the ast scan of the target bridge_protocol says when the table fell behind
    for field in shapes.get("p1_unlisted") or []:
        findings.append(finding(f"bridge_protocol 的 P1 校验函数里有守护表没列的枚举字段 {field}（守护对它的未知值不会报警）",
                                "Claude：把它加进 ui/tools/guard/sidecar_probe.py 的 P1_ENUM_PATHS（共享 helper 里按参数传路径的，"
                                "加进 P1_ENUM_EXEMPT 并写明理由），重跑 test_ui_guard.py；新出现的静态缺口按裁定写进 enum_baseline.json",
                                OWNER_CLAUDE, where="ui/tools/guard/sidecar_probe.py (P1_ENUM_PATHS)", key=f"p1_unlisted {field}"))
    if shapes.get("p1_scan_error"):
        findings.append(finding(f"读不出 P1 校验函数的枚举字段：{shapes['p1_scan_error']}",
                                "Claude：修 ui/tools/guard/sidecar_probe.py 的 p1_enum_scan（bridge_protocol 的 _TOPIC_VALIDATORS 结构变了）",
                                OWNER_CLAUDE, where="ui/tools/guard/sidecar_probe.py (p1_enum_scan)"))
    details = {"enum_new": len(result.get("new", [])), "enum_baseline_stale": len(result.get("stale", [])),
               "enum_uncovered_topics": shapes.get("uncovered"), "enum_p1_unlisted": len(shapes.get("p1_unlisted") or [])}
    return findings, details


def check_protocol(repo: Path, out: Path) -> dict:
    py = python_exe(repo)
    findings = []
    shapes, shapes_error = protocol_shapes(repo, out)
    code, text = run_logged([py, "-B", repo / "ui/tools/gen_rt_spec.py", "--check"], out / "gen_rt_spec.log", cwd=repo, timeout=120)
    details = {"gen_rt_spec_exit": code}
    if code != 0:
        findings.append(finding("ui/src/rt/spec.gen.json 与 bridge_protocol 不同步（后端的命令 / 查询 / 主题规格变了）",
                                "Claude：运行 .venv/Scripts/python.exe -B ui/tools/gen_rt_spec.py 重新生成；新增的命令或主题再接入前端"
                                "（ui/src/rt、页面），并确认 bridge_schema/PROTOCOL.md 已同步",
                                OWNER_CLAUDE, where="ui/src/rt/spec.gen.json", log=tail(text, 5)))
    code, text = run_logged([py, "-B", repo / "ui/tools/validate_ui_fixtures.py"], out / "validate_ui_fixtures.log", cwd=repo, timeout=300)
    details["validate_ui_fixtures_exit"] = code
    if code != 0:
        problems = [line.removeprefix("[validate_ui_fixtures] ") for line in text.splitlines()
                    if line.startswith("[validate_ui_fixtures] ") and not re.search(r"\b(OK|FAILED):", line)]
        groups: dict[str, list] = {}
        for line in problems:
            name, _, rest = line.partition(" ")
            message = re.sub(r"^(?:(?:steps|frames|replay)\[\d+\]|reply\[\d+\]|\.items\[\d+\]|\s)*:?\s*", "", rest)
            groups.setdefault(message or rest, []).append(name.rstrip(":"))
        for message, names in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            unique = sorted(set(names))
            extra_text, key = "", message
            if message.startswith("unknown:") and shapes:
                topic, extra = unknown_fields(repo, message, unique, shapes.get("fields") or {})
                if extra:
                    extra_text = f"：fixture 里的字段 {extra} 已不在 schema 里（后端删除或改名了它们）"
                    key = f"{message} " + ", ".join(f".{name}" for name in extra)
            findings.append(finding(f"前端 fixture 不再符合当前 schema：{message}（{len(unique)} 个文件，{len(names)} 处）{extra_text}",
                                    "先确认后端改动是有意的（bridge_protocol / PROTOCOL.md 已同步）。是有意的：Claude 更新这些 fixture 与前端投影"
                                    "（ui/src/rt、页面）；不是：后端道改回",
                                    f"{OWNER_BACKEND} 确认 → {OWNER_CLAUDE} 修",
                                    where="ui/test/fixtures/scenarios/" + ", ".join(unique[:3]) + (" …" if len(unique) > 3 else ""),
                                    key=key, files=unique))
        if not groups:
            findings.append(finding(f"validate_ui_fixtures.py 退出码 {code}：{tail(text, 5)}", "看日志", OWNER_CLAUDE))
        details["problems"] = len(problems)
    if shapes is None:
        findings.append(finding(f"读不出 bridge_protocol 的枚举字段：{shapes_error}", "后端 bridge_protocol 改坏（导入失败）由后端道修；"
                                "工具问题由 Claude 修 ui/tools/guard/sidecar_probe.py", f"{OWNER_BACKEND} / {OWNER_CLAUDE}",
                                where="bridge_protocol.py"))
    else:
        enum_findings, enum_details = enum_coverage_findings(repo, out, shapes)
        findings += enum_findings
        details.update(enum_details)
    return item_result("1", "FAIL" if findings else "PASS", findings, **details)


# ============================================================================ item 2

I18N_FIX = {
    "backend_keys": ("后端道：按「只追加」规则往 ui/src/i18n/locales/zh_CN/errors.json 加 \"{id}\": {{\"m\": \"中文\", \"_desc\": \"出处与用法\"}}；"
                     "然后 Claude 补另外 8 种语言（各语言 errors.json 与 _meta.json）"),
    "catalog": "后端道：往 ui/src/i18n/locales/zh_CN/catalog.json 只追加带 _desc 的 \"{id}\"；然后 Claude 补 8 种语言",
    "reason": "后端道：往 ui/src/i18n/locales/zh_CN/{file}.json 只追加带 _desc 的 \"{id}\"；然后 Claude 补 8 种语言",
    "missing": "Claude：补 {lang} 的 \"{id}\"（{why}；stale = zh_CN 原文改过，译文与 _meta.json 的 src_hash 一起更新）",
    "orphans": "Claude：{lang} 里有 zh_CN 已没有的 \"{id}\"（后端或页面改了名 / 删了）：随 zh_CN 改名或删除",
    "frozen": ("zh_CN 删掉了冻结的 \"{id}\"：误删就从 git 恢复（git show HEAD:ui/src/i18n/locales/zh_CN/{file}.json）；"
               "确属有意删除，Claude 更新 ui/src/i18n/frozen.json 并在报告里列出"),
    "restore": ("zh_CN 误删了后端仍在发送的 \"{id}\"：恢复这一条（git show HEAD:ui/src/i18n/locales/zh_CN/{file}.json 取回原文，"
                "或照其他语言的 _meta.json 找回）。不要删除其他 8 种语言里的它，也不要改 frozen.json"),
    "other": "Claude：修 {where}（{msg}）",
}


def _by_id(items: list[dict]) -> list[tuple[str, list[str], list[str]]]:
    """(id, languages, reasons) per id, in first-seen order."""
    grouped: dict[str, tuple[list, list]] = {}
    for it in items:
        langs, whys = grouped.setdefault(str(it.get("id")), ([], []))
        if it.get("lang") and it["lang"] not in langs:
            langs.append(it["lang"])
        why = it.get("why") or it.get("msg")
        if why and why not in whys:
            whys.append(why)
    return [(ident, langs, whys) for ident, (langs, whys) in grouped.items()]


def _backend_missing_ids(report: dict) -> set[str]:
    """ids a backend still sends that zh_CN lacks (backend_keys "missing in zh_CN" items)."""
    out = set()
    for check in report.get("checks", []):
        if check.get("id") != "backend_keys" or check.get("ok") or check.get("error"):
            continue
        for it in check.get("items", []):
            if it.get("id") is not None and "no backend sends" not in str(it.get("msg")):
                out.add(str(it["id"]))
    return out


def i18n_findings(report: dict) -> list[dict]:
    out = []
    errors: dict[str, list[str]] = {}
    # review A SHOULD 2: an id the backend still sends, missing from zh_CN but present as frozen /
    # in the other languages, was deleted from zh_CN by mistake: one finding (restore it), instead of
    # three contradicting ones (update frozen.json / delete it everywhere / add it back).
    flagged: dict[str, set[str]] = {}
    for check in report.get("checks", []):
        if check.get("id") in ("frozen", "orphans") and not check.get("ok") and not check.get("error"):
            for it in check.get("items", []):
                flagged.setdefault(str(it.get("id")), set()).add(check["id"])
    restore = {ident for ident in _backend_missing_ids(report) if ident in flagged}
    for ident in sorted(restore):
        file = ident.split(".")[0]
        out.append(finding(f"zh_CN 误删了后端仍在发送的 {ident}（同时触发 backend_keys、{'、'.join(sorted(flagged[ident]))}）",
                           I18N_FIX["restore"].format(id=ident, file=file), f"{OWNER_CLAUDE}（或误删的一方）",
                           where=f"ui/src/i18n/locales/zh_CN/{file}.json", key=ident))
    for check in report.get("checks", []):
        cid = check["id"]
        if cid in I18N_EXCLUDED or check.get("ok"):
            continue
        if check.get("error"):
            errors.setdefault(str(check["error"]), []).append(cid)
            continue
        if cid in ("missing", "orphans"):
            for ident, langs, whys in _by_id(check.get("items", [])):
                if ident in restore:
                    continue
                if cid == "missing":
                    out.append(finding(f"{ident} 在 {langs} 缺译文（{', '.join(whys)}）",
                                       I18N_FIX["missing"].format(lang="、".join(langs), id=ident, why=", ".join(whys)), OWNER_CLAUDE,
                                       where=f"ui/src/i18n/locales/<lang>/{ident.split('.')[0]}.json", key=ident, langs=langs))
                else:
                    out.append(finding(f"{langs} 有 zh_CN 没有的 {ident}",
                                       I18N_FIX["orphans"].format(lang="、".join(langs), id=ident), OWNER_CLAUDE,
                                       where=f"ui/src/i18n/locales/<lang>/{ident.split('.')[0]}.json", key=ident, langs=langs))
            continue
        for it in check.get("items", []):
            ident = it.get("id")
            if ident is not None and str(ident) in restore and cid in ("backend_keys", "frozen"):
                continue
            if cid == "frozen":
                file = str(ident).split(".")[0]
                out.append(finding(f"i18n_check frozen：{ident} {it.get('msg', '')}".strip(), I18N_FIX["frozen"].format(id=ident, file=file),
                                   OWNER_CLAUDE, where=f"ui/src/i18n/locales/zh_CN/{file}.json", key=ident))
                continue
            if cid == "backend_keys":
                if ident is None:
                    out.append(finding(f"后端有无法静态解析的动态文字键：{it.get('msg')}", "后端道：改成字面量 key，或在 ui/tools/backend_keys.py 登记",
                                       OWNER_BACKEND, where="ui/tools/backend_keys.py", key=None))
                elif "no backend sends" in str(it.get("msg")):
                    out.append(finding(f"errors.json 里的 {ident} 没有后端再发送（后端改名或删除了 message_key）",
                                       "Claude：确认后在 9 种语言里同步改名或删除该 id", OWNER_CLAUDE,
                                       where="ui/src/i18n/locales/*/errors.json", key=ident))
                else:
                    out.append(finding(f"后端 message_key {ident} 在 zh_CN 没有条目（页面会显示 errors.unknown）",
                                       I18N_FIX["backend_keys"].format(id=ident), f"{OWNER_BACKEND} → {OWNER_CLAUDE}",
                                       where="ui/src/i18n/locales/zh_CN/errors.json", key=ident, sources=it.get("sources")))
            elif cid == "catalog":
                out.append(finding(f"catalog 字段 {ident}（{it.get('group')}）在 zh_CN 没有条目", I18N_FIX["catalog"].format(id=ident),
                                   f"{OWNER_BACKEND} → {OWNER_CLAUDE}", where="ui/src/i18n/locales/zh_CN/catalog.json", key=ident))
            elif cid.startswith("reason."):
                file = cid.split(".", 2)[1]
                out.append(finding(f"后端 reason_code {ident} 在 zh_CN 没有文字（页面会显示 common.reason_unknown）",
                                   I18N_FIX["reason"].format(id=ident, file=file), f"{OWNER_BACKEND} → {OWNER_CLAUDE}",
                                   where=f"ui/src/i18n/locales/zh_CN/{file}.json", key=ident, source=check.get("source")))
            else:
                where = it.get("file") or f"i18n_check {cid}"
                out.append(finding(f"i18n_check {cid}：{ident or ''} {it.get('msg', '')}".strip(),
                                   I18N_FIX["other"].format(where=where, msg=it.get("msg", "")), OWNER_CLAUDE, where=where, key=ident))
    for error, checks in errors.items():
        lines = [line.strip() for line in error.splitlines() if line.strip()]
        out.append(finding(f"i18n_check 的 {checks} 没能运行：{lines[-1] if lines else error}",
                           "看 item2/i18n_check.log 与 ui/tools/backend_keys.py 的报错：后端改动让键提取失败（例如语法错误、"
                           "词表结构变了）由后端道修，工具本身的问题由 Claude 修",
                           f"{OWNER_BACKEND} / {OWNER_CLAUDE}", where="ui/tools/backend_keys.py", error=error[-1500:]))
    return out


def check_i18n(repo: Path, out: Path) -> dict:
    report_path = out / "i18n.json"
    code, text = run_logged(["node", repo / "ui/tools/i18n_check.mjs", "--level", "p4", "--report", report_path,
                             "--repo", repo, "--quiet"], out / "i18n_check.log", cwd=repo, timeout=600)
    if not report_path.is_file():
        return item_result("2", "FAIL", [finding(f"i18n_check.mjs 没有产出报告（退出码 {code}）：{tail(text, 8)}", "看日志", OWNER_CLAUDE)])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    findings = i18n_findings(report)
    counts = {c["id"]: c["count"] for c in report.get("checks", [])}
    return item_result("2", "FAIL" if findings else "PASS", findings, counts=counts,
                       excluded=sorted(I18N_EXCLUDED), report=str(report_path))


# ============================================================================ item 3

PROJECTION_FIX = {
    "exceptions": ("前端运行时层在真实帧上抛异常（栈见 projection.json）", "Claude：修 ui/src/rt；若是后端字段变了，先对照 PROTOCOL.md", OWNER_CLAUDE),
    "contract_diag": ("真实帧不符合前端合同（src/rt 诊断 {kind}，主题 {topic}）",
                      "对照 PROTOCOL.md：后端违约由后端道改；合同有意变更由 Claude 同步 spec.gen.json 与 ui/src/rt", f"{OWNER_BACKEND} / {OWNER_CLAUDE}"),
    "reconnects": ("前端判定数据损坏，要求重连 / 重同步（{kind}，主题 {topic}）", "同上：对照 PROTOCOL.md 的 rev / seq / patch 规则", f"{OWNER_BACKEND} / {OWNER_CLAUDE}"),
    "type_mismatch": ("字段类型变了：{key} {path} 真实帧为 {real}，前端 fixture 为 {fixtures}",
                      "确认是有意改动：是 → PROTOCOL.md / bridge_protocol 同步，Claude 更新 ui/src/rt 投影与 ui/test/fixtures/scenarios；否 → 后端道改回",
                      f"{OWNER_BACKEND} 确认 → {OWNER_CLAUDE}"),
    "enum_unknown": ("前端没有 {topic} {path}={value!r} 的映射（{file}），页面会显示成通用状态",
                     "Claude：在 {file} 加映射与文案；若是误发，后端道改回", f"{OWNER_CLAUDE} / {OWNER_BACKEND}"),
    "enum_untested": ("真实帧 {topic} {path}={value!r}：前端 fixture 在这个位置从没出现过这个值（fixture 里有 {tested}），页面没按它测过",
                      ENUM_FIX, f"{OWNER_CLAUDE} / {OWNER_BACKEND}"),
}


def projection_findings(projection: dict) -> list[dict]:
    out = []
    for text in projection.get("exceptions", []):
        what, fix, owner = PROJECTION_FIX["exceptions"]
        out.append(finding(f"{what}：{text.splitlines()[0] if text else ''}", fix, owner, where="ui/src/rt", stack=text[:1500]))
    for kind in ("contract_diag", "reconnects"):
        for entry in projection.get(kind, []):
            what, fix, owner = PROJECTION_FIX[kind]
            out.append(finding(what.format(kind=entry.get("kind"), topic=entry.get("topic")), fix, owner, where=f"topic {entry.get('topic')}",
                               diag=entry))
    for q in projection.get("queries", []):
        if q.get("problem"):
            out.append(finding(f"页面查询 {q['cmd']} 用真实应答没有正常结束（{q.get('outcome')}）", "Claude：查 ui/src/rt/calls.js；或后端应答不合 PROTOCOL §11",
                               f"{OWNER_CLAUDE} / {OWNER_BACKEND}", where=f"query {q['cmd']}"))
    for m in projection.get("type_mismatch", []):
        what, fix, owner = PROJECTION_FIX["type_mismatch"]
        out.append(finding(what.format(**m), fix, owner, where=m["key"], key=m["path"]))
    for e in projection.get("enum_unknown", []):
        if e.get("rule") == "untested_value":
            what, fix, owner = PROJECTION_FIX["enum_untested"]
            out.append(finding(what.format(**{"tested": [], **e}), fix, owner, where=e.get("file"),
                               key=f"{e.get('topic')} {e.get('path')}={e.get('value')}"))
            continue
        what, fix, owner = PROJECTION_FIX["enum_unknown"]
        out.append(finding(what.format(**{"key": None, **e}), fix.format(file=e.get("file")), owner, where=e.get("file"), key=e.get("value")))
    for t in projection.get("text_missing", []):
        kind = t.get("kind")
        if kind == "error_text_missing":
            out.append(finding(f"真实帧里的错误 {t.get('chain')}（code {t.get('code')}，来自 {t.get('where')}）在 zh_CN 没有文字，页面显示 errors.unknown",
                               I18N_FIX["backend_keys"].format(id=t.get("id")), f"{OWNER_BACKEND} → {OWNER_CLAUDE}",
                               where="ui/src/i18n/locales/zh_CN/errors.json", key=t.get("id")))
        elif kind == "reason_text_missing":
            out.append(finding(f"真实帧里的 reason_code {t.get('reason')}（来自 {t.get('where')}）没有 {t.get('id')}，页面显示 common.reason_unknown",
                               I18N_FIX["reason"].format(id=t.get("id"), file=str(t.get("id")).split(".")[0]), f"{OWNER_BACKEND} → {OWNER_CLAUDE}",
                               where=f"ui/src/i18n/locales/zh_CN/{str(t.get('id')).split('.')[0]}.json", key=t.get("id")))
        else:
            out.append(finding(f"{t.get('id')} 在 {t.get('langs')} 没有译文（真实帧会用到它，来自 {t.get('where')}）",
                               f"Claude：补 {t.get('langs')} 的 \"{t.get('id')}\"", OWNER_CLAUDE, key=t.get("id"), langs=t.get("langs")))
    return out


def caps_findings(repo: Path, hello: dict | None) -> list[dict]:
    if not hello:
        return []
    spec = json.loads((repo / "ui/src/rt/spec.gen.json").read_text(encoding="utf-8"))
    caps = hello.get("caps") or {}
    known_cmds = set(spec.get("commands", {})) | set(spec.get("queries", {}))
    out = []
    unknown_cmds = sorted(set(caps.get("commands") or ()) - known_cmds)
    unknown_topics = sorted(set(caps.get("topics") or ()) - set(spec.get("topics", {})))
    if unknown_cmds or unknown_topics:
        out.append(finding(f"冻结侧车 hello 声明了前端 spec.gen.json 不认识的命令 {unknown_cmds} / 主题 {unknown_topics}",
                           "Claude：重新生成 spec.gen.json（gen_rt_spec.py）并接入前端；若 dist 里是旧侧车，重新构建", OWNER_CLAUDE,
                           where="ui/src/rt/spec.gen.json"))
    return out


def check_probe(app: Path, repo: Path, out: Path, *, source: bool = False) -> dict:
    py = python_exe(repo)
    probe_dir = out / "probe"
    command = [py, "-B", repo / "ui/tools/guard/sidecar_probe.py", "--repo", repo, "--out", probe_dir]
    if source:
        command.append("--source")
    else:
        exe = app / "tesseract-runtime.exe"
        if not exe.is_file():
            return item_result("3", "FAIL", [finding(f"缺少 {exe}", "先构建 dist", OWNER_CLAUDE)])
        command += ["--exe", exe]
    started = time.monotonic()
    code, text = run_logged(command, out / "sidecar_probe.log", cwd=repo, timeout=600)
    probe_path = probe_dir / "probe.json"
    if not probe_path.is_file():
        return item_result("3", "FAIL", [finding(f"探针没有产出 probe.json（退出码 {code}）：{tail(text, 8)}", "看 sidecar_probe.log", OWNER_CLAUDE)])
    probe = json.loads(probe_path.read_text(encoding="utf-8"))
    findings = []
    details = {"started": probe.get("started"), "form": probe.get("form"), "metrics": probe.get("metrics"),
               "probe_seconds": round(time.monotonic() - started, 1), "known_stopped": probe.get("stopped_unavailable")}
    if not probe.get("started"):
        return item_result("3", "FAIL", [finding(f"探针没有启动侧车（做不到隔离就不启动）：{probe.get('failures')}",
                                                 "看 isolation_problems：沙箱不合格是工具问题（Claude）；后端改了沙箱用到的配置项是后端问题",
                                                 f"{OWNER_CLAUDE} / {OWNER_BACKEND}")], **details)
    for failure in probe.get("failures", []):
        findings.append(finding(f"侧车探针：{failure}", "对照 PROTOCOL.md 与 tests/test_bridge_p2_33.py：后端违约由后端道修；"
                                "主机事实变化或残留进程是严重问题，先停用这份 dist", OWNER_BACKEND, where="sidecar_probe"))
    findings += caps_findings(repo, probe.get("hello"))
    frames = probe_dir / "frames.jsonl"
    projection_path = probe_dir / "projection.json"
    if frames.is_file():
        code, text = run_logged(["node", repo / "ui/tools/guard/project_frames.mjs", "--frames", frames, "--probe", probe_path,
                                 "--out", projection_path], out / "project_frames.log", cwd=repo, timeout=300)
        if projection_path.is_file():
            projection = json.loads(projection_path.read_text(encoding="utf-8"))
            findings += projection_findings(projection)
            details["projection"] = {k: (len(v) if isinstance(v, list) else v) for k, v in projection.items() if k != "info"}
            info = projection.get("info", {})
            details["projection_info"] = {k: info.get(k) for k in ("frames", "page_frames", "topics", "uncompared_keys",
                                                                   "enum_paths_checked", "enum_uncovered_topics",
                                                                   "fixture_paths_absent_in_real")}
            details["projection_info"]["enum_known_untested"] = len(info.get("enum_known_untested") or [])
        else:
            findings.append(finding(f"project_frames.mjs 没有产出结果（退出码 {code}）：{tail(text, 8)}", "看 project_frames.log", OWNER_CLAUDE))
    else:
        findings.append(finding("探针没有留下 frames.jsonl", "看 sidecar_probe.log", OWNER_CLAUDE))
    return item_result("3", "FAIL" if findings else "PASS", findings, **details)


# ============================================================================ item 4a


def check_render(repo: Path, out: Path) -> dict:
    render_out = out / "render"
    code, text = run_logged(["node", repo / "ui/test/render/run.mjs", "--build-flags", "--i18n-trace", "--case", "smoke",
                             "--langs", "zh_CN,en_US,pseudo", "--out", render_out], out / "render.log", cwd=repo, timeout=900)
    summary_path = render_out / "summary.json"
    if not summary_path.is_file():
        return item_result("4a", "FAIL", [finding(f"渲染矩阵没有产出 summary.json（退出码 {code}）：{tail(text, 8)}", "看 render.log", OWNER_CLAUDE)])
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    findings = []
    for cell in summary.get("failed", []):
        first = cell.get("failures", [{}])[0] if cell.get("failures") else {}
        findings.append(finding(f"渲染格 {cell.get('cell')} 失败：{json.dumps(first, ensure_ascii=False)[:300]}",
                                "Claude：看截图与 checks.json 定位页面问题；若因后端字段 / 文字变化，按第 1–3 项的修法先修那边",
                                OWNER_CLAUDE, where=cell.get("screenshot"), failures=cell.get("failures", [])[:5]))
    if code != 0 and not findings:
        findings.append(finding(f"渲染矩阵退出码 {code}：{tail(text, 5)}", "看 render.log", OWNER_CLAUDE))
    return item_result("4a", "FAIL" if findings else "PASS", findings, cells=summary.get("cells"),
                       avg_ms_per_cell=summary.get("avg_ms_per_cell"), known_failures=len(summary.get("known_failures", [])),
                       summary=str(summary_path))


# ============================================================================ item 4b


def smoke_verdict(result: dict) -> tuple[list[str], list[dict]]:
    """FAIL conditions of the ticket: no hello, no host_ready, exit code != 0, residue, host facts
    changed (plus the sandbox readback and the process audit). The 1.5 s reveal timeout is only
    recorded (a separate diagnosis ticket owns it)."""
    problems: list[str] = []
    runs = []
    for run in result.get("runs", []):
        own = [f for f in run.get("failures", []) if not f.startswith(REVEAL_TIMEOUT)]
        if run.get("hello_ms") is None and not any("hello" in f for f in own):
            own.append("no hello (sidecar_ready) in shell.log")
        if run.get("host_ready_ms") is None:
            own.append("no host_ready: the page never reported ready")
        if run.get("exit_code") != 0:
            own.append(f"shell exit code {run.get('exit_code')} (expected 0)")
        problems += [f"run {run.get('run')}: {p}" for p in own]
        runs.append({k: run.get(k) for k in ("run", "load_class", "hello_ms", "reveal", "reveal_ms", "window_shown_ms",
                                              "host_ready_ms", "exit_code", "sidecar_exit")})
    for failure in result.get("failures", []):
        if failure.startswith("run "):
            continue  # per run, judged above
        problems.append(failure)
    if not result.get("runs"):
        problems.append("no cold start ran")
    return problems, runs


def smoke_supports_app(repo: Path) -> bool:
    py = python_exe(repo)
    try:
        done = subprocess.run([py, "-B", str(repo / "scripts/build_shell_preview.py"), "smoke", "--help"], cwd=repo,
                              capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "--app" in done.stdout


def check_shell_smoke(app: Path, repo: Path, out: Path, *, helper: bool = True) -> dict:
    """helper=True (the refresh build, Claude ruling 6 of 10-26): the sidecar's validator helper smoke
    (16 helpers) runs first, as in the Qt refresh; its failure or residue is a FAIL like the rest."""
    default_app = (repo / "dist-shell" / "Tesseract").resolve()
    command = [python_exe(repo), "-B", repo / "scripts/build_shell_preview.py", "smoke", "--runs", "1", "--label", "guard",
               "--evidence", out / "smoke"]
    if not helper:
        command.append("--skip-helper")
    if smoke_supports_app(repo):
        command += ["--app", app]
    elif app.resolve() != default_app:
        return item_result("4b", "SKIP", [finding("build_shell_preview.py smoke 还没有 --app（T-P6-06 §8），不能对候选目录冷启动",
                                                  "T-P6-06 完成后自动生效", OWNER_SHELL)], app=str(app))
    code, text = run_logged(command, out / "shell_smoke.log", cwd=repo, timeout=600)
    result_path = out / "smoke" / "smoke-guard" / "smoke-result.json"
    if not result_path.is_file():
        busy = "already running" in text
        status = "SKIP" if busy else "FAIL"
        what = ("dist 里有程序在运行（可能是用户在看预览），而冒烟只能用这个目录：本项未跑" if busy
                else f"外壳冒烟没有产出结果（退出码 {code}）：{tail(text, 8)}")
        return item_result("4b", status, [finding(what, "等预览退出后重跑守护；T-P6-06 之后改用候选目录" if busy else "看 shell_smoke.log",
                                                  OWNER_CLAUDE)])
    result = json.loads(result_path.read_text(encoding="utf-8"))
    problems, runs = smoke_verdict(result)
    findings = [finding(f"外壳冷启动：{p}", "看 smoke/smoke-guard/run1/（shell.log、bridge.log、result.json）：页面没就绪多半是前端或外壳问题"
                        "（Claude），侧车没 hello / 退出码不为 0 多半是侧车或后端问题（后端道）；主机事实变化或残留是严重问题",
                        f"{OWNER_CLAUDE} / {OWNER_BACKEND}", where="build_shell_preview.py smoke") for p in problems]
    return item_result("4b", "FAIL" if findings else "PASS", findings, runs=runs, result=str(result_path),
                       helper_smoke=result.get("helper_smoke", "skipped" if not helper else None),
                       note="首个 service.state 早于 1.5 s 的揭开只记录，不判 FAIL（另有诊断单）")


# ============================================================================ item 5


def unit_files(repo: Path, full: bool = True) -> list[Path]:
    unit = repo / "ui" / "test" / "unit"
    files = sorted(unit.glob("*.test.mjs"))
    if full:
        return files
    return [f for f in files if f.name.removesuffix(".test.mjs") in UNIT_SUBSET]


def parse_node_test(text: str) -> dict:
    counts = {}
    for key in ("tests", "pass", "fail", "skipped", "cancelled"):
        m = re.search(rf"^ℹ {key} (\d+)", text, re.M)
        counts[key] = int(m.group(1)) if m else None
    failing = [line.strip()[2:].strip() for line in text.splitlines() if line.lstrip().startswith("✖")]
    return {"counts": counts, "failing": list(dict.fromkeys(failing))}


def collect_unit(repo: Path, out: Path) -> dict:
    """What `node --test ui/test/unit/` collects (files and case counts), without running a test:
    ui/tools/guard/unit_collect.mjs. -> {"files": {abs path: n}, "total": n, "error": str | None}."""
    code, text = run_logged(["node", GUARD_TOOLS / "unit_collect.mjs", repo / "ui" / "test" / "unit"],
                            out / "unit_collect.log", cwd=repo, timeout=180)
    for line in reversed(text.splitlines()):
        if line.startswith("{") and '"total"' in line:
            try:
                data = json.loads(line)
            except ValueError:
                break
            if code not in (0, None) and not data.get("error"):
                data["error"] = f"unit_collect.mjs exit code {code}"
            return data
    return {"files": {}, "total": None, "error": f"unit_collect.mjs gave no result (exit code {code}): {tail(text, 5)}"}


def batch_findings(collected: dict, ran: dict | None, files: list[Path], *, full: bool) -> list[dict]:
    """Claude ruling 1 of 10-26: the parallel run must be the same batch `node --test ui/test/unit/`
    collects — same files, same number of cases per file, same total — or it is a FAIL (no silent
    gap). Cases are what the collector counts (a subtest is not a case: unit_count_reporter.mjs
    counts it apart, and node's own "ℹ tests" includes it, so that number is not compared; review A
    SHOULD 1). With --unit-subset only the subset's files are compared (manual runs only)."""
    fix = ("Claude：看 item5/unit_collect.log 与 unit_counts.json；多半是 ui/test/unit/index.js 的收集规则或某个文件的"
           "导入方式变了，守护 unit_files() 要与之一致")
    if collected.get("error"):
        return [finding(f"无法确定 node --test ui/test/unit/ 收集到的用例：{collected['error'][:400]}", fix, OWNER_CLAUDE,
                        where="ui/test/unit/index.js")]
    if ran is None:
        return [finding("并行跑没有产出逐文件计数（unit_counts.json），无法核对是否同一批", fix, OWNER_CLAUDE)]
    norm = lambda p: str(Path(p)).lower()  # noqa: E731
    want = {norm(k): v for k, v in collected.get("files", {}).items()}
    got = {norm(k): v.get("tests", 0) for k, v in ran.get("files", {}).items()}
    if not full:
        names = {norm(f) for f in files}
        want = {k: v for k, v in want.items() if k in names}
    problems = []
    for key in sorted(set(want) | set(got)):
        a, b = want.get(key), got.get(key)
        if a != b:
            problems.append(f"{Path(key).name}: 收集 {a if a is not None else '—'}，并行跑 {b if b is not None else '—'}")
    if ran.get("unattributed"):
        problems.append(f"并行跑有 {ran['unattributed']} 个用例没有所属文件")
    ran_total = ran.get("total")
    if ran_total is None:
        ran_total = sum(v.get("tests", 0) for v in ran.get("files", {}).values())
    if full and collected.get("total") is not None and ran_total != collected["total"]:
        problems.append(f"用例总数：收集 {collected['total']}，并行跑 {ran_total}")
    if not problems:
        return []
    return [finding(f"并行跑的 UI 单测与 node --test ui/test/unit/ 收集到的不是同一批（{len(problems)} 处）：{problems[:8]}",
                    fix, OWNER_CLAUDE, where="ui/test/unit", differences=problems[:50])]


def check_unit(repo: Path, out: Path, *, full: bool = True) -> dict:
    files = unit_files(repo, full)
    if not files:
        return item_result("5", "FAIL", [finding("没有找到 UI 单测文件", "看 ui/test/unit", OWNER_CLAUDE)])
    out.mkdir(parents=True, exist_ok=True)
    collected = collect_unit(repo, out)
    counts_path = out / "unit_counts.json"
    counts_path.unlink(missing_ok=True)
    reporter = (GUARD_TOOLS / "unit_count_reporter.mjs").as_uri()
    command = ["node", "--test", "--test-reporter=spec", "--test-reporter-destination=stdout",
               f"--test-reporter={reporter}", f"--test-reporter-destination={counts_path}",
               "--test-concurrency=4", *files]
    code, text = run_logged(command, out / "ui_unit.log", cwd=repo, timeout=1800)
    parsed = parse_node_test(text)
    try:
        ran = json.loads(counts_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        ran = None
    findings = batch_findings(collected, ran, files, full=full)
    if code != 0 or (parsed["counts"].get("fail") or 0) > 0:
        names = [n for n in parsed["failing"] if not n.startswith("failing tests")][:20]
        findings.append(finding(f"UI 单测失败 {parsed['counts'].get('fail')} 个：{names[:5]}",
                                "Claude：看 ui_unit.log；若因后端改动（协议、文字键）引起，先按第 1–3 项修", OWNER_CLAUDE,
                                where="ui/test/unit", failing=names))
    return item_result("5", "FAIL" if findings else "PASS", findings, scope="full" if full else "subset",
                       files=[f.name for f in files], collected_files=len(collected.get("files", {})),
                       collected_cases=collected.get("total"),
                       ran_files=len((ran or {}).get("files", {})), ran_cases=(ran or {}).get("total"),
                       subtests=sum(v.get("subtests", 0) for v in (ran or {}).get("files", {}).values()), **parsed["counts"])


# ============================================================================ the run


def summary_markdown(guard: dict) -> str:
    lines = [f"# UI 守护摘要（{guard['verdict']}）", "",
             f"- 对象：`{guard['app']}`（源码树 `{guard['repo']}`）",
             f"- 时间：{guard['started_utc']} – {guard['finished_utc']}，共 {guard['seconds']} s",
             f"- 结论：**{guard['verdict']}**" + ("" if guard["verdict"] == "PASS" else
                                                  "（deliver 默认拒绝；确需交付用 --force-ui-risk，风险会写进 refresh.json）"), "",
             "| 项 | 内容 | 结果 | 用时 s |", "|---|---|---|---|"]
    for item in guard["items"]:
        lines.append(f"| {item['id']} | {item['name']} | {item['status']} | {item.get('seconds', '')} |")
    for item in guard["items"]:
        if not item["findings"]:
            continue
        lines += ["", f"## {item['id']} {item['name']}：{item['status']}", ""]
        for f in item["findings"][:40]:
            where = f" — `{f['where']}`" if f.get("where") else ""
            key = f"（key `{f['key']}`）" if f.get("key") else ""
            lines.append(f"- {f['what']}{key}{where}")
            lines.append(f"  - 修法：{f['fix']}")
            lines.append(f"  - 谁改：{f['owner']}")
        if len(item["findings"]) > 40:
            lines.append(f"- ……另有 {len(item['findings']) - 40} 条，见 guard.json")
    return "\n".join(lines) + "\n"


def verdict_of(items: list[dict]) -> str:
    statuses = {item["status"] for item in items}
    if "FAIL" in statuses:
        return "FAIL"
    if "SKIP" in statuses or not items:
        return "INCOMPLETE"
    return "PASS"


def run_guard(*, app: Path, repo: Path, out: Path, only: list[str], source_sidecar: bool = False,
              full_unit: bool = True, serial: bool = False, helper: bool = True) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    guard = {"app": str(app), "repo": str(repo), "out": str(out), "started_utc": utc(), "items_requested": only,
             "unit_scope": "full" if full_unit else "subset", "helper_smoke": helper}
    jobs = {
        "0": lambda: check_dist_scope(app, repo, out / "item0"),
        "1": lambda: check_protocol(repo, out / "item1"),
        "2": lambda: check_i18n(repo, out / "item2"),
        "3": lambda: check_probe(app, repo, out / "item3", source=source_sidecar),
        "4a": lambda: check_render(repo, out / "item4a"),
        "5": lambda: check_unit(repo, out / "item5", full=full_unit),
        "4b": lambda: check_shell_smoke(app, repo, out / "item4b", helper=helper),
    }

    def timed(item: str) -> dict:
        t0 = time.monotonic()
        (out / f"item{item}").mkdir(parents=True, exist_ok=True)
        try:
            result = jobs[item]()
        except Exception as exc:  # a crashed check is a FAIL of the guard itself
            import traceback
            result = item_result(item, "FAIL", [finding(f"检查本身出错：{exc!r}", "Claude 修 scripts/ui_guard.py", OWNER_CLAUDE,
                                                        trace=traceback.format_exc()[-2000:])])
        result["seconds"] = round(time.monotonic() - t0, 1)
        print(f"[ui_guard {utc()}] {item} {NAMES[item]}: {result['status']} ({result['seconds']} s)", flush=True)
        return result

    results: dict[str, dict] = {}
    # Phase A: everything except the shell cold start, in parallel (each a subprocess). Phase B:
    # the shell cold start alone, so the shell's 10 s hello window is not raced by the other items.
    phase_a = [i for i in ITEMS if i in only and i != "4b"]
    if serial:
        for item in phase_a:
            results[item] = timed(item)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(phase_a))) as pool:
            for item, result in zip(phase_a, pool.map(timed, phase_a)):
                results[item] = result
    if "4b" in only:
        results["4b"] = timed("4b")
    guard["items"] = [results[i] for i in ITEMS if i in results]
    guard["verdict"] = verdict_of(guard["items"])
    guard["finished_utc"] = utc()
    guard["seconds"] = round(time.monotonic() - started, 1)
    (out / "guard.json").write_text(json.dumps(guard, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    (out / "摘要.md").write_text(summary_markdown(guard), encoding="utf-8")
    return guard


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="ui_guard.py", description=__doc__.splitlines()[0])
    parser.add_argument("--app", type=Path, default=None, help="the dist directory (Tesseract.exe, tesseract-runtime.exe)")
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO, help="the source tree to check (default: this tree)")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--only", default=",".join(ITEMS), help="items, e.g. 1,2,3")
    parser.add_argument("--source-sidecar", action="store_true", help="item 3 on runtime_main.pyw of --repo instead of the frozen sidecar")
    parser.add_argument("--unit-subset", action="store_true", help="item 5: only the protocol / projection / text files (no browser)")
    parser.add_argument("--serial", action="store_true", help="no parallel items")
    parser.add_argument("--skip-helper", action="store_true",
                        help="item 4b without the validator helper smoke (manual runs only; the refresh build never skips it)")
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    app = (args.app or (repo / "dist" / "Tesseract")).resolve()
    only = [i.strip() for i in args.only.split(",") if i.strip()]
    unknown = [i for i in only if i not in ITEMS]
    if unknown:
        parser.error(f"unknown items {unknown}; known: {ITEMS}")
    out = (args.out or (DEFAULT_REPO / ".build" / "ui-guard-runs" / dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S"))).resolve()
    guard = run_guard(app=app, repo=repo, out=out, only=only, source_sidecar=args.source_sidecar,
                      full_unit=not args.unit_subset, serial=args.serial, helper=not args.skip_helper)
    print(f"[ui_guard] {guard['verdict']} in {guard['seconds']} s: " +
          ", ".join(f"{i['id']}={i['status']}" for i in guard["items"]) + f" -> {out / '摘要.md'}", flush=True)
    return {"PASS": 0, "FAIL": 1}.get(guard["verdict"], 3)


if __name__ == "__main__":
    raise SystemExit(main())
