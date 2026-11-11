"""T-P2-H7: the sidecar fills platform's uname cache before anything hits WMI.

aiohttp.helpers calls platform.system() at import, and in the frozen sidecar
PyInstaller's standard runtime hook pyi_rth_setuptools does so even earlier
(setuptools -> jaraco.context). On CPython 3.12 that is a WMI query, and a
WMI timeout (OSError 258) makes platform run ``cmd /c ver``.
sidecar_platform_seed.ensure_seeded() seeds the cache first: frozen from the
custom runtime hook scripts/sidecar_platform_seed_rthook.py (before the
standard hooks), in source from runtime_main.pyw.

Every runtime check runs in a child interpreter: the seed mutates the
process-wide platform module, and the children stub ``_wmi.exec_query`` (a
slow ``OSError(258)``) and ``_winapi.CreateProcess`` (recorded, then
refused), so no test starts a process. The one exception touching real WMI
is the reference comparison (test_seed_matches_the_stdlib_wmi_values): a
read-only local query, still under the CreateProcess refusal stub, skipped
when TESSERACT_TEST_SKIP_REAL_WMI=1. Profiles are throwaway directories,
never %APPDATA%/%LOCALAPPDATA%.
"""
from __future__ import annotations

import ast
import functools
import inspect
import json
import logging
import os
from pathlib import Path
import platform
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "runtime_main.pyw"
SEED_MODULE = ROOT / "sidecar_platform_seed.py"
SEED_HOOK = ROOT / "scripts" / "sidecar_platform_seed_rthook.py"
AUDIT_HOOK = ROOT / "scripts" / "tier3_audit_runtime_hook.py"
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows platform/WMI path")

# Seconds one stubbed WMI query blocks before failing like the field's 258.
WMI_DELAY = 2.0
REFUSE_PROCESS = """
import _winapi
calls = {"wmi": [], "create": []}
def create_process(*args):
    calls["create"].append(" ".join(str(item) for item in args[:2]))
    raise OSError("process creation refused by test")
_winapi.CreateProcess = create_process
"""
STUBS = REFUSE_PROCESS + f"""
import json, os, sys, time
import _wmi
def exec_query(query):
    calls["wmi"].append(query)
    time.sleep({WMI_DELAY})
    raise OSError(0, "WMI wait timed out (test stub)", None, 258)
_wmi.exec_query = exec_query
def report(**fields):
    print("H7:" + json.dumps(dict(fields, calls=calls)), flush=True)
"""

# The frozen sidecar's PYSOURCE order (build-sidecar/tesseract-runtime/
# EXE-00.toc): custom hooks in spec order, then PyInstaller's standard
# runtime hooks for the bundled modules, then the entry.
STANDARD_RTHOOKS = (
    ("PyInstaller", "hooks/rthooks/pyi_rth_inspect.py"),
    ("_pyinstaller_hooks_contrib", "rthooks/pyi_rth_cryptography_openssl.py"),
    ("PyInstaller", "hooks/rthooks/pyi_rth_pkgutil.py"),
    ("PyInstaller", "hooks/rthooks/pyi_rth_setuptools.py"),
    ("PyInstaller", "hooks/rthooks/pyi_rth_multiprocessing.py"),
)


@pytest.fixture(scope="module")
def child_env(tmp_path_factory):
    base = tmp_path_factory.mktemp("h7")
    profile, cache = base / "profile", base / "pycache"
    profile.mkdir()
    cache.mkdir()
    # Bytecode goes to a throwaway prefix, never the source tree.
    env = dict(os.environ, APPDATA=str(profile), LOCALAPPDATA=str(profile),
               PYTHONPYCACHEPREFIX=str(cache))
    for name in ("TESSERACT_ISOLATED_AUDIT_MANIFEST", "PYTHONDONTWRITEBYTECODE"):
        env.pop(name, None)
    return env


def run_child(env, body, timeout=300):
    result = subprocess.run([sys.executable, "-c", STUBS + body], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, result.stderr[-4000:]
    line = next(line for line in result.stdout.splitlines() if line.startswith("H7:"))
    return json.loads(line[3:])


def standard_rthook_paths():
    paths = []
    for package, relative in STANDARD_RTHOOKS:
        module = __import__(package)
        path = Path(module.__file__).parent / relative
        assert path.is_file(), path
        paths.append(str(path))
    return paths


def test_cpython_platform_private_contract_the_seed_relies_on():
    """Locks the 3.12 internals; a Python upgrade that changes them fails here."""
    assert sys.version_info[:2] == (3, 12)
    for name in ("_uname_cache", "_wmi_query", "_syscmd_ver", "_node",
                 "_get_machine_win32", "_win32_ver", "uname_result", "_Processor"):
        assert hasattr(platform, name), name
    assert isinstance(inspect.getattr_static(platform.uname_result, "processor"),
                      functools.cached_property)
    win32 = inspect.getsource(platform._win32_ver)
    assert win32.index("_wmi_query(") < win32.index("_syscmd_ver()[2]")
    assert "except ValueError" in win32 and "platform_version" in win32
    uname = inspect.getsource(platform.uname)
    assert "_uname_cache is not None" in uname and "win32_ver()" in uname
    assert "_get_machine_win32()" in uname and "_uname_cache = uname_result(" in uname
    assert "_wmi_query('CPU', 'Architecture')" in inspect.getsource(platform._get_machine_win32)
    processor = inspect.getsource(platform._Processor.get_win32)
    assert "_wmi_query('CPU'" in processor and "PROCESSOR_IDENTIFIER" in processor
    assert "'cmd /c ver'" in inspect.getsource(platform._syscmd_ver)


def test_control_third_party_import_hits_wmi_then_cmd_ver(child_env):
    """Detector check: without the seed, aiohttp's import queries WMI and,
    after the 258, tries to start ``ver`` (refused here)."""
    data = run_child(child_env, "import aiohttp\nreport()\n")
    assert any("Win32_OperatingSystem" in query for query in data["calls"]["wmi"])
    assert data["calls"]["create"] and all("ver" in item for item in data["calls"]["create"])


FROZEN_ORDER = """
import runpy, types
# What the bootloader provides before the runtime hooks run.
sys._MEIPASS = os.environ["APPDATA"]
sys.argv = ["tesseract-runtime.exe"]
bootstrap = types.ModuleType("pyimod02_importers")
class PyiFrozenFinder:
    pass
bootstrap.PyiFrozenFinder = PyiFrozenFinder
bootstrap.get_pyz_toc_tree = lambda: {{}}
sys.modules["pyimod02_importers"] = bootstrap
steps = []
for path in {hooks!r}:
    runpy.run_path(path, run_name="__main__")
    steps.append([os.path.basename(path), len(calls["wmi"]), len(calls["create"])])
entry = runpy.run_path("runtime_main.pyw", run_name="h7_entry")
import sidecar_platform_seed
jaraco = sorted(name for name in sys.modules if name.endswith("jaraco.context"))
report(steps=steps, entry_seed=entry["_PLATFORM_SEED"], status=sidecar_platform_seed.status(),
       setuptools="setuptools" in sys.modules, jaraco=jaraco)
"""


def test_frozen_hook_order_seeds_before_pyi_rth_setuptools(child_env):
    """The frozen order: custom hooks (audit, then the seed) -> PyInstaller's
    standard runtime hooks (pyi_rth_setuptools imports setuptools ->
    jaraco.context -> platform.system()) -> runtime_main.pyw."""
    standard = standard_rthook_paths()
    data = run_child(child_env, FROZEN_ORDER.format(
        hooks=[str(AUDIT_HOOK), str(SEED_HOOK)] + standard))
    assert data["setuptools"] is True and data["jaraco"], data
    assert data["calls"] == {"wmi": [], "create": []}
    assert all(wmi == 0 and create == 0 for _name, wmi, create in data["steps"]), data["steps"]
    # The hook seeded; the entry's call is a no-op returning that first result.
    assert data["status"] == "seeded" and data["entry_seed"] == "seeded"


def test_frozen_hook_order_without_the_seed_hook_hits_wmi_in_setuptools(child_env):
    """Control for the test above: pyi_rth_setuptools alone already queries
    WMI and tries ``cmd /c ver`` before the entry could seed anything."""
    standard = standard_rthook_paths()
    data = run_child(child_env, FROZEN_ORDER.format(hooks=[str(AUDIT_HOOK)] + standard))
    assert data["setuptools"] is True and data["jaraco"], data
    steps = {name: (wmi, create) for name, wmi, create in data["steps"]}
    assert steps["pyi_rth_pkgutil.py"] == (0, 0)
    wmi, create = steps["pyi_rth_setuptools.py"]
    assert wmi >= 1 and create >= 1
    assert any("Win32_OperatingSystem" in query for query in data["calls"]["wmi"])
    assert all("ver" in item for item in data["calls"]["create"])
    assert data["entry_seed"] == "skipped:cached"


def test_entry_then_sidecar_import_chain_never_queries_wmi_or_spawns(child_env):
    body = """
import runpy
import platform, socket, winreg   # cold imports are not the seed's cost
import sidecar_platform_seed
started = time.perf_counter()
first = sidecar_platform_seed.ensure_seeded()
seed_seconds = time.perf_counter() - started
started = time.perf_counter()
entry = runpy.run_path("runtime_main.pyw", run_name="h7_entry")
entry_seconds = time.perf_counter() - started
started = time.perf_counter()
import bridge_main, desktop_runtime, aiohttp.helpers
import_seconds = time.perf_counter() - started
values = [platform.system(), platform.node(), platform.release(), platform.version(),
          platform.machine(), platform.processor()]
report(first=first, seed=entry["_PLATFORM_SEED"], seed_seconds=seed_seconds,
       entry_seconds=entry_seconds, import_seconds=import_seconds,
       uname=list(platform.uname()), values=values, is_windows=aiohttp.helpers.IS_WINDOWS)
"""
    data = run_child(child_env, body)
    # The exact proof: no WMI query and no process, from the seed onwards.
    assert data["first"] == "seeded" and data["seed"] == "seeded"
    assert data["calls"] == {"wmi": [], "create": []}
    assert data["is_windows"] is True
    assert data["uname"] == data["values"] and data["uname"][0] == "Windows"
    assert all(data["uname"]), data["uname"]
    # Upper bounds only show that nothing waits: the seed itself is
    # in-process registry/env reads (one leaked stub query alone costs
    # WMI_DELAY); entry and import times are compile/disk dominated and get
    # loose bounds so a loaded machine does not misreport.
    assert data["seed_seconds"] < WMI_DELAY
    assert data["entry_seconds"] < 5 * WMI_DELAY
    assert data["import_seconds"] < 120


def stdlib_wmi_reference_uname(child_env):
    """uname() of the unmodified stdlib with real WMI, or pytest.skip."""
    if os.environ.get("TESSERACT_TEST_SKIP_REAL_WMI") == "1":
        pytest.skip("TESSERACT_TEST_SKIP_REAL_WMI=1: no real WMI reference query")
    # Reference: the unmodified stdlib with real WMI (a read-only local query);
    # process creation is refused, so a WMI failure cannot start ``cmd``.
    reference = subprocess.run([sys.executable, "-c", REFUSE_PROCESS + """
import json, platform
ok = []
original = platform._wmi_query
def query(*args):
    result = list(original(*args))
    ok.append(args[0])
    return iter(result)
platform._wmi_query = query
print(json.dumps(dict(uname=list(platform.uname()), wmi=ok, create=calls["create"])))
"""], cwd=ROOT, env=child_env, capture_output=True, text=True, timeout=120)
    assert reference.returncode == 0, reference.stderr
    expected = json.loads(reference.stdout)
    assert expected["create"] == [] or "OS" not in expected["wmi"]
    if "OS" not in expected["wmi"] or "CPU" not in expected["wmi"]:
        pytest.skip(f"reference WMI unavailable now ({expected['wmi']}); not comparable")
    return expected["uname"]


def test_seed_matches_the_stdlib_wmi_values(child_env):
    expected = stdlib_wmi_reference_uname(child_env)
    seeded = run_child(child_env, """
import runpy, platform
entry = runpy.run_path("runtime_main.pyw", run_name="h7_entry")
report(seed=entry["_PLATFORM_SEED"], uname=list(platform.uname()))
""")
    assert seeded["seed"] == "seeded" and seeded["calls"] == {"wmi": [], "create": []}
    assert seeded["uname"] == expected


def test_seed_restores_platform_hooks_and_leaves_win32_ver_alone(child_env):
    """An audit guard wraps platform._syscmd_ver and requires its wrapper to
    stay installed (socket_guard_installed); the seed must hand it back."""
    data = run_child(child_env, """
import platform, runpy
def guard_wrapper(*args, **kwargs):
    guard_calls.append(args)
    return "", "", ""
guard_calls = []
platform._syscmd_ver = guard_wrapper
original_wmi = platform._wmi_query
entry = runpy.run_path("runtime_main.pyw", run_name="h7_entry")
restored = platform._syscmd_ver is guard_wrapper and platform._wmi_query is original_wmi
calls_after_seed = dict(wmi=list(calls["wmi"]), create=list(calls["create"]), guard=len(guard_calls))
platform.win32_ver()
report(seed=entry["_PLATFORM_SEED"], restored=restored, after_seed=calls_after_seed,
       guard_after_win32_ver=len(guard_calls))
""")
    assert data["seed"] == "seeded" and data["restored"] is True
    assert data["after_seed"] == {"wmi": [], "create": [], "guard": 0}
    # win32_ver() is not cached by the seed: it still takes the stdlib path.
    assert len(data["calls"]["wmi"]) == 1 and data["guard_after_win32_ver"] == 1


def test_seed_is_fail_safe_and_never_spawns(child_env):
    data = run_child(child_env, """
import platform, socket, winreg
import sidecar_platform_seed as module
results = {"before": module.status(), "first": module.ensure_seeded()}
seed = module.seed_platform_uname
wmi, ver = platform._wmi_query, platform._syscmd_ver
results["cached"] = seed()
results["again"] = [module.ensure_seeded(), module.status()]
def state():
    return dict(cache=platform._uname_cache is None,
                restored=platform._wmi_query is wmi and platform._syscmd_ver is ver)

platform._uname_cache = None
real_hostname = socket.gethostname
socket.gethostname = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
results["error"] = [seed(), state()]
socket.gethostname = real_hostname

platform._uname_cache = None
saved = {k: os.environ.pop(k) for k in ("PROCESSOR_ARCHITEW6432", "PROCESSOR_ARCHITECTURE") if k in os.environ}
results["incomplete"] = [seed(), state()]
os.environ.update(saved)

platform._uname_cache = None
del platform._wmi_query
results["unsupported"] = [seed(), platform._uname_cache is None]
platform._wmi_query = wmi

real_platform = sys.platform
sys.platform = "linux"
results["not_windows"] = seed()
sys.platform = real_platform

platform._uname_cache = None
real_open = winreg.OpenKeyEx
def no_registry(*args, **kwargs):
    raise OSError("registry unavailable (test)")
winreg.OpenKeyEx = no_registry
results["no_registry"] = [seed(), state(), list(platform.uname())]
winreg.OpenKeyEx = real_open
report(results=results)
""")
    results = data["results"]
    assert data["calls"] == {"wmi": [], "create": []}
    assert results["before"] == "not_run" and results["first"] == "seeded"
    assert results["cached"] == "skipped:cached"
    # ensure_seeded keeps the first attempt's result (what bridge_main logs).
    assert results["again"] == ["seeded", "seeded"]
    # Failure or an incomplete answer keeps nothing: stdlib behaviour as before.
    assert results["error"] == ["skipped:error", {"cache": True, "restored": True}]
    assert results["incomplete"] == ["skipped:incomplete", {"cache": True, "restored": True}]
    assert results["unsupported"] == ["skipped:unsupported", True]
    assert results["not_windows"] == "skipped:not_windows"
    # Without the registry the stdlib's own non-WMI version source is used.
    status, state, uname = results["no_registry"]
    assert status == "seeded" and state == {"cache": False, "restored": True}
    major, minor, build = sys.getwindowsversion().platform_version
    assert uname[3] == f"{major}.{minor}.{build}"


def _module_imports(tree):
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module)
    return names


def test_seed_runs_first_in_the_entry_and_in_the_frozen_hooks():
    tree = ast.parse(ENTRY.read_bytes())
    body = tree.body
    seed = next(index for index, node in enumerate(body) if isinstance(node, ast.Assign)
                and ast.unparse(node) == "_PLATFORM_SEED = ensure_seeded()")
    guard = next(index for index, node in enumerate(body) if isinstance(node, ast.If)
                 and "TESSERACT_ISOLATED_AUDIT_MANIFEST" in ast.unparse(node.test))
    freeze = next(index for index, node in enumerate(body) if isinstance(node, ast.Expr)
                  and ast.unparse(node) == "multiprocessing.freeze_support()")
    main = next(index for index, node in enumerate(body) if isinstance(node, ast.If)
                and ast.unparse(node.test) == "__name__ == '__main__'")
    assert guard < seed < freeze < main
    # Nothing but the stdlib and the stdlib-only seed module before the seed.
    early = {alias.name for node in body[:seed] if isinstance(node, ast.Import)
             for alias in node.names}
    assert early == {"multiprocessing", "os"}
    assert [ast.unparse(node) for node in body[:seed] if isinstance(node, ast.ImportFrom)] == [
        "from sidecar_platform_seed import ensure_seeded"]
    assert _module_imports(ast.parse(SEED_MODULE.read_bytes())) == {"sys", "platform", "winreg"}
    assert _module_imports(ast.parse(SEED_HOOK.read_bytes())) == {"sidecar_platform_seed"}
    # Frozen: custom runtime hooks run first in list order (the audit guard,
    # then the seed), before PyInstaller's standard hooks; then this entry.
    spec = ast.parse((ROOT / "tesseract-runtime.spec").read_bytes().decode("utf-8-sig"))
    analysis = next(node.value for node in spec.body if isinstance(node, ast.Assign)
                    and ast.unparse(node.targets[0]) == "a")
    assert ast.literal_eval(analysis.args[0]) == ["runtime_main.pyw"]
    hooks = next(node.value for node in spec.body if isinstance(node, ast.Assign)
                 and ast.unparse(node.targets[0]) == "runtime_hooks")
    assert len(hooks.elts) == 2
    assert "tier3_audit_runtime_hook.py" in ast.unparse(hooks.elts[0])
    assert "sidecar_platform_seed_rthook.py" in ast.unparse(hooks.elts[1])
    assert "platform" not in AUDIT_HOOK.read_text(encoding="utf-8")
    from PyInstaller.depend import analysis as pyi_analysis
    source = inspect.getsource(pyi_analysis.PyiModuleGraph.analyze_runtime_hooks)
    assert source.index("custom_runhooks") < source.index("_available_rthooks")


# Calls that bypass the uname cache and query WMI (and ``ver``) every time.
UNCACHED = frozenset({"win32_ver", "win32_edition", "win32_is_iot"})


def test_sidecar_closure_never_calls_the_uncached_platform_functions():
    """The seed only fills uname(); platform.platform()/win32_ver()/
    win32_edition()/win32_is_iot() still query WMI on every call. Lock that no
    module of the sidecar's own closure calls them (third-party code is
    outside this graph; at import only platform.system() was found there)."""
    from sidecar_import_graph import sidecar_graph
    offenders = []
    for module, path in sidecar_graph()["modules"].items():
        tree = ast.parse(Path(path).read_bytes())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "platform":
                bad = {alias.name for alias in node.names} & (UNCACHED | {"platform"})
                if bad:
                    offenders.append(f"{module}:{node.lineno} from platform import {sorted(bad)}")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                name, owner = node.func.attr, node.func.value
                if name in UNCACHED or (name == "platform" and isinstance(owner, ast.Name)
                                        and owner.id == "platform"):
                    offenders.append(f"{module}:{node.lineno} {ast.unparse(node.func)}()")
    assert offenders == []
    assert "sidecar_platform_seed" in sidecar_graph()["modules"]


def test_bridge_log_records_the_seed_status(tmp_path, monkeypatch):
    import bridge_main
    import sidecar_platform_seed
    monkeypatch.setattr(sidecar_platform_seed, "_STATUS", "skipped:cached")
    logger = bridge_main.init_logging(SimpleNamespace(data_dir=tmp_path))
    try:
        for handler in logger.handlers:
            handler.flush()
        text = (tmp_path / "bridge.log").read_text(encoding="utf-8")
    finally:
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()
    assert "Platform uname seed: skipped:cached" in text
    assert logging.getLogger("tesseract.bridge").propagate is False
