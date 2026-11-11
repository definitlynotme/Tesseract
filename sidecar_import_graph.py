"""Static import graph of the sidecar (``runtime_main.pyw``) over this repository.

T-P2-H2 (决定_第五波裁定 33-#20): the graph helpers moved here from
``tests/test_bridge_qt_guard.py`` so the frozen build (``tesseract-runtime.spec``
identity components, ``scripts/build_runtime_sidecar.ps1`` bundle check) no
longer depends on a test module; that test module re-exports these names, so
both callers keep working unchanged. Standard library only (no pytest).

The module itself is not part of the sidecar's import closure
(tests/test_bridge_qt_guard.py asserts it). What the graph holds:

* every import statement of the repository's own modules reachable from the
  entry -- module level, class level and inside functions and methods --
  plus string-literal dynamic imports (``importlib.import_module``,
  ``__import__``; ``find_spec`` of a Qt root);
* eager (import-time) and lazy (function-level) edges, and the Qt import sites.
"""
from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENTRY = ROOT / "runtime_main.pyw"
QT_ROOTS = frozenset({"PySide6", "shiboken6", "PySide2", "shiboken2", "PyQt4", "PyQt5", "PyQt6",
                      "PySide", "qtpy", "qtconsole"})


def _local(name: str, root: Path = ROOT) -> tuple[str | None, Path | None]:
    """Longest repository module for a dotted import name."""
    parts = [part for part in name.split(".") if part]
    for size in range(len(parts), 0, -1):
        base = root.joinpath(*parts[:size])
        if base.with_suffix(".py").is_file():
            return ".".join(parts[:size]), base.with_suffix(".py")
        if (base / "__init__.py").is_file():
            return ".".join(parts[:size]), base / "__init__.py"
    return None, None


# Calls whose first string-literal argument is a module import.  find_spec
# only locates a module, so it is recorded only for a Qt root.
DYNAMIC_IMPORTS = frozenset({"import_module", "__import__"})


class _Imports(ast.NodeVisitor):
    """Import statements with the enclosing function (None = import time).

    The function is the outermost function or method: ``name`` for a
    top-level function, ``Class.name`` for a method (nested classes join
    with dots); imports in a class body run at import time (None).
    """

    def __init__(self, module: str, is_package: bool):
        self.package = module if is_package else module.rpartition(".")[0]
        self.found: list[tuple[str, str | None, int]] = []
        self._functions: list[str] = []
        self._classes: list[str] = []

    def _absolute(self, node: ast.ImportFrom) -> str:
        if not node.level:
            return node.module or ""
        parts = self.package.split(".") if self.package else []
        parts = parts[:len(parts) - (node.level - 1)] if node.level > 1 else parts
        return ".".join([*parts, node.module] if node.module else parts)

    def _where(self) -> str | None:
        return self._functions[0] if self._functions else None

    def visit_ClassDef(self, node):
        self._classes.append(node.name)
        self.generic_visit(node)
        self._classes.pop()

    def visit_FunctionDef(self, node):
        if not self._functions:
            self._functions.append(".".join([*self._classes, node.name]))
        else:
            self._functions.append(node.name)
        self.generic_visit(node)
        self._functions.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node):
        func = node.func
        name = (func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute) else "")
        argument = node.args[0] if node.args else None
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            target = argument.value.strip()
            if name in DYNAMIC_IMPORTS or (name == "find_spec" and target.split(".")[0] in QT_ROOTS):
                self.found.append((target, self._where(), node.lineno))
        self.generic_visit(node)

    def visit_Import(self, node):
        for alias in node.names:
            self.found.append((alias.name, self._where(), node.lineno))

    def visit_ImportFrom(self, node):
        base = self._absolute(node)
        self.found.append((base, self._where(), node.lineno))
        for alias in node.names:
            self.found.append((f"{base}.{alias.name}", self._where(), node.lineno))


@lru_cache(maxsize=None)
def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_bytes(), filename=str(path))


def _imports(module: str, path: Path) -> list[tuple[str, str | None, int]]:
    visitor = _Imports(module, path.name == "__init__.py")
    visitor.visit(_parse(path))
    return visitor.found


@lru_cache(maxsize=1)
def sidecar_graph() -> dict:
    """{module: path}, eager/lazy edges and Qt import sites of the closure."""
    return build_graph("runtime_main", ENTRY, ROOT)


def build_graph(entry_module: str, entry_path: Path, base: Path) -> dict:
    modules: dict[str, Path] = {}
    eager: dict[str, set[str]] = {}
    lazy: dict[str, set[tuple[str, str]]] = {}
    qt_sites: dict[str, list[tuple[str | None, str, int]]] = {}
    todo = [(entry_module, entry_path)]
    while todo:
        module, path = todo.pop()
        if module in modules:
            continue
        modules[module] = path
        eager[module], lazy[module] = set(), set()
        for name, function, line in _imports(module, path):
            root = name.split(".")[0]
            if root in QT_ROOTS:
                qt_sites.setdefault(module, []).append((function, name, line))
                continue
            target, target_path = _local(name, base)
            if target is None:
                continue
            if function is None:
                eager[module].add(target)
            else:
                lazy[module].add((function, target))
            if target not in modules:
                todo.append((target, target_path))
    return dict(modules=modules, eager=eager, lazy=lazy, qt_sites=qt_sites)


def _eager_closure(graph: dict, start: str) -> set[str]:
    seen, todo = set(), [start]
    while todo:
        module = todo.pop()
        if module not in seen:
            seen.add(module)
            todo.extend(graph["eager"].get(module, ()))
    return seen


def qt_tainted_modules(graph: dict) -> set[str]:
    """Modules whose import itself (module/class level) reaches Qt."""
    direct = {module for module, sites in graph["qt_sites"].items()
              if any(function is None for function, _name, _line in sites)}
    return {module for module in graph["modules"] if _eager_closure(graph, module) & direct}


def _callables(tree: ast.Module):
    """(qualified name, class or None, node) of top-level functions and methods."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name, None, node
        elif isinstance(node, ast.ClassDef):
            stack = [(node.name, node)]
            while stack:
                prefix, klass = stack.pop()
                for item in klass.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        yield f"{prefix}.{item.name}", prefix, item
                    elif isinstance(item, ast.ClassDef):
                        stack.append((f"{prefix}.{item.name}", item))


def _calls_into(node: ast.AST, klass: str | None, names: set[str]) -> bool:
    """A plain call of a same-module function, or a self./cls./Class. call of
    a method of the same class, named in ``names``."""
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if isinstance(func, ast.Name) and func.id in names:
            return True
        if klass and isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) \
                and func.value.id in {"self", "cls", klass.rpartition(".")[2]} \
                and f"{klass}.{func.attr}" in names:
            return True
    return False


def qt_functions(graph: dict) -> set[tuple[str, str]]:
    """(module, top-level function or Class.method) whose body imports Qt or a
    tainted module, closed over same-module function calls and same-class
    method calls."""
    tainted = qt_tainted_modules(graph)
    found = {(module, function) for module, sites in graph["qt_sites"].items()
             for function, _name, _line in sites if function is not None}
    found |= {(module, function) for module, edges in graph["lazy"].items()
              for function, target in edges if target in tainted}
    changed = True
    while changed:
        changed = False
        for module in {module for module, _function in found}:
            names = {function for owner, function in found if owner == module}
            for qualname, klass, node in _callables(_parse(graph["modules"][module])):
                if (module, qualname) not in found and _calls_into(node, klass, names):
                    found.add((module, qualname))
                    changed = True
    return found


def foreign_references(graph: dict, functions: set[tuple[str, str]]) -> list[str]:
    """Other closure modules that name a Qt function (from-import or attribute)."""
    by_module: dict[str, set[str]] = {}
    for module, function in functions:
        by_module.setdefault(module, set()).add(function)
    problems = []
    for module, path in graph["modules"].items():
        tree = _parse(path)
        aliases = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and not node.level and node.module in by_module \
                    and module != node.module:
                for alias in node.names:
                    if alias.name in by_module[node.module] or alias.name == "*":
                        problems.append(f"{module}:{node.lineno} imports {node.module}.{alias.name}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in by_module:
                        aliases[alias.asname or alias.name] = alias.name
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id in aliases and module != aliases[node.value.id]
                    and node.attr in by_module[aliases[node.value.id]]):
                problems.append(f"{module}:{node.lineno} uses {aliases[node.value.id]}.{node.attr}")
    return problems


def sidecar_modules() -> list[str]:
    """Every closure module the sidecar may import: all but the Qt-tainted."""
    graph = sidecar_graph()
    return sorted(set(graph["modules"]) - qt_tainted_modules(graph) - {"runtime_main"})


def sidecar_source_files() -> list[str]:
    """Repository-relative files of the whole closure (entry included), for the
    frozen build's identity components (tesseract-runtime.spec)."""
    return sorted(path.relative_to(ROOT).as_posix() for path in sidecar_graph()["modules"].values())
