"""T-P2-33 / plan §5.6: the sidecar never imports Qt (PySide6, shiboken6, ...).

Three layers of evidence:

1. Static AST import graph from ``runtime_main.pyw`` over the repository's
   own modules, every import statement included (module level, class level
   and inside functions and methods), plus string-literal dynamic imports
   (``importlib.import_module``, ``__import__``; ``find_spec`` of a Qt root).
   Module-level edges must never reach a Qt root, and neither may any import
   inside a function or method: ``ALLOWED_QT_FUNCTIONS`` is empty since the
   Qt desktop app was removed.
2. Runtime: a child interpreter with a meta-path blocker for the Qt roots
   imports every non-Qt module of that closure; nothing tries to load Qt.
3. Frozen bundle (when ``dist-sidecar`` exists): no Qt module in the PYZ, and
   a build whose component hashes match the sources contains every module
   of the closure.

The graph helpers live in ``sidecar_import_graph.py`` (T-P2-H2, 33-#20) and
are re-exported here: ``tesseract-runtime.spec`` (identity components) and
``scripts/build_runtime_sidecar.ps1`` (bundle check) still import them from
this module, so it must stay importable without pytest: pytest is imported
inside the tests only.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys

# Re-exported for tesseract-runtime.spec and scripts/build_runtime_sidecar.ps1.
from sidecar_import_graph import (  # noqa: F401
    DYNAMIC_IMPORTS, ENTRY, QT_ROOTS, ROOT, _callables, _calls_into, _eager_closure, _Imports,
    _imports, _local, _parse, build_graph, foreign_references, qt_functions, qt_tainted_modules,
    sidecar_graph, sidecar_modules, sidecar_source_files,
)

# Functions that may import Qt lazily: none since the Qt desktop app (and its
# packaged ``--smoke-test`` in desktop_app_shell) was removed.
ALLOWED_QT_FUNCTIONS: frozenset[tuple[str, str]] = frozenset()
BUNDLE = ROOT / "dist-sidecar" / "tesseract-runtime"


# ------------------------------------------------------------------- tests

def test_import_time_closure_of_the_entry_never_reaches_qt():
    graph = sidecar_graph()
    closure = _eager_closure(graph, "runtime_main")
    tainted = qt_tainted_modules(graph)
    assert not closure & tainted, sorted(closure & tainted)
    # The sidecar's own layer (every bridge_* module and what it imports at
    # import time) is Qt free as well, lazily imported or not.
    bridge = {module for module in graph["modules"] if module.startswith("bridge_")}
    assert {path.stem for path in ROOT.glob("bridge_*.py")} <= bridge
    for module in bridge:
        assert not _eager_closure(graph, module) & tainted, module


def test_dynamic_imports_and_methods_are_part_of_the_graph(tmp_path):
    """String-literal dynamic imports and class methods are Qt import sites,
    and a method calling a Qt method of its class is tainted as well."""
    (tmp_path / "entry.py").write_text(
        "import importlib\n"
        "import helper\n"
        "def plain():\n    return importlib.import_module('PySide6.QtWidgets')\n"
        "def located():\n    return importlib.util.find_spec('shiboken6')\n"
        "def harmless():\n    return importlib.util.find_spec('json')\n"
        "def dunder():\n    return __import__('helper2')\n"
        "class Window:\n"
        "    import json\n"
        "    def show(self):\n        from PySide6 import QtCore\n"
        "    def open(self):\n        return self.show()\n"
        "    def unrelated(self):\n        return 1\n"
        "    class Inner:\n        def go(self):\n            import PyQt6\n",
        encoding="utf-8")
    (tmp_path / "helper.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "helper2.py").write_text("Y = 1\n", encoding="utf-8")
    graph = build_graph("entry", tmp_path / "entry.py", tmp_path)
    assert set(graph["modules"]) == {"entry", "helper", "helper2"}
    assert ("dunder", "helper2") in graph["lazy"]["entry"]
    sites = {(function, name) for function, name, _line in graph["qt_sites"]["entry"]}
    assert sites == {("plain", "PySide6.QtWidgets"), ("located", "shiboken6"),
                     ("Window.show", "PySide6"), ("Window.show", "PySide6.QtCore"),
                     ("Window.Inner.go", "PyQt6")}
    assert qt_functions(graph) == {("entry", "plain"), ("entry", "located"),
                                   ("entry", "Window.show"), ("entry", "Window.open"),
                                   ("entry", "Window.Inner.go")}
    assert qt_tainted_modules(graph) == set()


def test_no_function_of_the_closure_imports_qt_lazily():
    graph = sidecar_graph()
    functions = qt_functions(graph)
    assert functions <= ALLOWED_QT_FUNCTIONS, sorted(functions - ALLOWED_QT_FUNCTIONS)
    assert foreign_references(graph, functions) == []
    # The one entry the sidecar does take into that module stays Qt free.
    assert ("desktop_app_shell", "run_validator_helper_smoke") not in functions


def test_every_non_qt_module_of_the_closure_imports_with_qt_blocked():
    modules = sidecar_modules()
    assert "bridge_main" in modules and "desktop_runtime" in modules
    script = """
import importlib, importlib.abc, json, sys
ROOTS = %r
class Block(importlib.abc.MetaPathFinder):
    attempts = []
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ROOTS:
            Block.attempts.append(name)
            raise ImportError("blocked " + name)
        return None
sys.meta_path.insert(0, Block())
for root in ROOTS:
    sys.modules.pop(root, None)
failed = {}
for name in json.loads(sys.argv[1]):
    try:
        importlib.import_module(name)
    except Exception as exc:
        failed[name] = repr(exc)[:300]
loaded = sorted(name for name in sys.modules if name.split(".")[0] in ROOTS)
print(json.dumps(dict(failed=failed, attempts=Block.attempts, loaded=loaded)))
""" % (sorted(QT_ROOTS),)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env.pop("TESSERACT_ISOLATED_AUDIT_MANIFEST", None)
    result = subprocess.run([sys.executable, "-B", "-c", script, json.dumps(modules)], cwd=ROOT,
                            env=env, capture_output=True, text=True, timeout=240)
    assert result.returncode == 0, result.stderr[-4000:]
    outcome = json.loads(result.stdout.strip().splitlines()[-1])
    assert outcome == dict(failed={}, attempts=[], loaded=[]), outcome


def _bundle_modules() -> set[str]:
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(str(BUNDLE / "tesseract-runtime.exe"))
    names = set(archive.toc)
    for name in archive.toc:
        if name.endswith(".pyz"):
            names |= set(archive.open_embedded_archive(name).toc)
    return names


def frozen_build_current() -> tuple[bool, list[str]]:
    """Do the bundle's declared component hashes match today's sources?"""
    declaration = json.loads((BUNDLE / "tier3_build_id.json").read_bytes())
    stale = [name for name, digest in sorted(declaration.get("components", {}).items())
             if not (ROOT / name).is_file()
             or hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest]
    return not stale, stale


def test_frozen_bundle_has_no_qt_and_holds_the_whole_closure():
    import pytest
    if not (BUNDLE / "tesseract-runtime.exe").is_file():
        pytest.skip("no frozen sidecar")
    names = _bundle_modules()
    assert not {name for name in names if name.split(".")[0] in QT_ROOTS}
    files = [str(path.relative_to(BUNDLE)) for path in BUNDLE.rglob("*")
             if path.name.casefold().startswith(("qt", "pyside", "shiboken"))
             and path.suffix.casefold() in {".dll", ".pyd"}]
    assert files == []
    current, stale = frozen_build_current()
    if not current:
        pytest.skip(f"frozen sidecar predates the sources: {stale[:5]}")
    missing = sorted(set(sidecar_modules()) - names)
    assert missing == [], missing
    # "current" covers the whole closure: the spec declares every closure
    # file as an identity component (T-P2-33 fix round 1).
    declared = set(json.loads((BUNDLE / "tier3_build_id.json").read_bytes())["components"])
    assert sorted(set(sidecar_source_files()) - declared) == []


def test_graph_helper_module_is_outside_the_sidecar_closure():
    """T-P2-H2 (33-#20): the helper module is build/test tooling only."""
    graph = sidecar_graph()
    assert "sidecar_import_graph" not in graph["modules"]
    assert "sidecar_import_graph" not in sidecar_modules()
    assert "sidecar_import_graph.py" not in sidecar_source_files()
    assert not any(module.startswith("tests") for module in graph["modules"])


def test_build_helpers_in_p2_33_import_without_pytest():
    """scripts/build_shell_preview.py imports host_facts, process_table and the sandbox
    helpers from tests.test_bridge_p2_33 with pytest unavailable; a module-level
    `import pytest` there failed release run 43119781754 at the assemble gate."""
    script = ("import sys; sys.modules['pytest'] = None\n"
              "import tests.test_bridge_p2_33 as m\n"
              "print(callable(m.process_table), callable(m.host_facts), callable(m.prepare_sandbox))")
    result = subprocess.run([sys.executable, "-B", "-c", script], cwd=ROOT, capture_output=True,
                            text=True, timeout=120, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == "True True True"


def test_graph_helpers_import_without_pytest():
    """The spec and the build check import this module; it must not need pytest."""
    script = ("import sys; sys.modules['pytest'] = None\n"
              "from tests.test_bridge_qt_guard import sidecar_source_files, sidecar_modules\n"
              "print(len(sidecar_source_files()), len(sidecar_modules()))")
    result = subprocess.run([sys.executable, "-B", "-c", script], cwd=ROOT, capture_output=True,
                            text=True, timeout=120, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    assert result.returncode == 0, result.stderr[-2000:]
