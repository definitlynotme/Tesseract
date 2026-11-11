"""Static freeze contract; no PyInstaller execution or runtime startup."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def spec(name="tesseract-runtime.spec"):
    return ast.parse((ROOT / name).read_bytes().decode("utf-8-sig"))


def assignment(tree, name):
    return next(node.value for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == name
                        for target in node.targets))


def keyword(call, name):
    return next(item.value for item in call.keywords if item.arg == name)


def test_sidecar_entry_and_onedir_layout():
    tree = spec()
    analysis = assignment(tree, "a")
    assert analysis.func.id == "Analysis"
    assert ast.literal_eval(analysis.args[0]) == ["runtime_main.pyw"]
    exe = assignment(tree, "exe")
    assert ast.literal_eval(keyword(exe, "name")) == "tesseract-runtime"
    assert ast.literal_eval(keyword(exe, "exclude_binaries")) is True
    assert ast.literal_eval(keyword(exe, "contents_directory")) == "_internal"
    assert assignment(tree, "coll").func.id == "COLLECT"


def test_sidecar_excludes_qt_and_keeps_helpers_and_bridge_modules():
    tree = spec()
    excludes = ast.literal_eval(assignment(tree, "excludes"))
    assert {"PySide6", "shiboken6", "PySide2", "shiboken2", "PyQt5", "PyQt6"} <= set(excludes)
    assert keyword(assignment(tree, "a"), "excludes").id == "excludes"
    imports = set(ast.literal_eval(assignment(tree, "hiddenimports")))
    assert {path.stem for path in ROOT.glob("bridge_*.py")} <= imports
    assert {"atomic_file", "desktop_runtime", "runtime_network_recovery"} <= imports
    assert {"scripts." + path.stem for path in (ROOT / "scripts").glob("tier3_audit_*.py")} <= imports
    assert not any(isinstance(node, (ast.Import, ast.ImportFrom))
                   and "PySide6" in ast.unparse(node) for node in ast.walk(tree))


def test_sidecar_audit_hook_order_before_entry():
    tree = spec()
    hooks = assignment(tree, "runtime_hooks")
    # The audit hook first, then the platform seed hook (T-P2-H7), before
    # PyInstaller's standard hooks.
    assert len(hooks.elts) == 2
    assert "scripts" in ast.unparse(hooks.elts[0])
    assert "tier3_audit_runtime_hook.py" in ast.unparse(hooks.elts[0])
    assert "scripts" in ast.unparse(hooks.elts[1])
    assert "sidecar_platform_seed_rthook.py" in ast.unparse(hooks.elts[1])
    components = ast.unparse(assignment(tree, "component_files"))
    assert "'scripts/sidecar_platform_seed_rthook.py'" in components
    assert "'sidecar_platform_seed.py'" in components
    assert keyword(assignment(tree, "a"), "runtime_hooks").id == "runtime_hooks"
    entry = ast.parse((ROOT / "runtime_main.pyw").read_bytes())
    freeze = next(node for node in entry.body if isinstance(node, ast.Expr)
                  and isinstance(node.value, ast.Call)
                  and ast.unparse(node.value.func) == "multiprocessing.freeze_support")
    main_import = next(node for node in ast.walk(entry) if isinstance(node, ast.ImportFrom)
                       and node.module == "bridge_main")
    assert freeze.lineno < main_import.lineno
