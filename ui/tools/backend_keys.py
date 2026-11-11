"""Enumerate every backend message key, catalog label id and reason code (T-P4-03).

The page translates ``Error.message_key`` / ``FieldSpec.label_id`` / ``reason_code``
values it never sees at build time, so the zh_CN catalog has to cover the full set
the sidecar and the shell can send (P4 architecture §3.1, §3.9; critique B6, B7, C8).

Sources (each key records where it came from):

* literal ``"errors.<...>"`` strings (AST, not grep) in every top-level module whose source
  mentions ``errors.`` — ``bridge_*.py`` and what they call into (``desktop_runtime.py``
  builds ``errors.config.*``, ``runtime_settings_apply.py`` builds ``errors.settings.*``) —
  and in the shell's ``shell/app/src/*.rs`` (test modules excluded);
* every dynamic site that builds a key (f-string or ``"errors.x." + y``), expanded
  by a small constant evaluator (call sites, local assignments, loops over module
  constants, local helper returns) or by a declared rule below.  A dynamic site
  that neither resolves nor has a rule is reported under ``unresolved``; the gate
  treats that as a failure, so a new backend key family cannot slip through;
* the PROTOCOL §14 start-failure table;
* ``bridge_messages._REASONS``; ``bridge_diagnostics.DIAGNOSE_REASON_CODES``;
  ``bridge_exit.EXIT_REASON_CODES`` / ``FALLBACK_FLAGS``; ``bridge_protocol.ERROR_CODES``;
* the shell: ``fail("<code>", "<reason>")`` in ``shell/app/src/host.rs``
  (``errors.host.<code>``, reason codes for ``shell.host_reason.*``);
* keys appearing in ``bridge_schema/fixtures/**`` and ``ui/test/fixtures/**``
  (``invalid`` fixture directories and the i18n gate fixtures are skipped);
* ``bridge_catalog`` label ids, choice values and notice codes (imported: the
  module and its imports are Qt-free; checked at run time).

Only the standard library is used; nothing is written.

    python -B ui/tools/backend_keys.py [--repo <dir>] [--strict]

prints one JSON document on stdout.  ``--strict`` exits 3 when a dynamic site is
unresolved.
"""
from __future__ import annotations

import argparse
import ast
import itertools
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

KEY_RE = re.compile(r"^errors\.[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
TOKEN_RE = re.compile(r"^[a-z][a-z0-9_]*$")
NONE = object()          # the value None, as seen by the evaluator
MAX_DEPTH = 8


class Unresolved(Exception):
    pass


# ---------------------------------------------------------------------------
# A tiny constant evaluator over one module
# ---------------------------------------------------------------------------

class Module:
    def __init__(self, path: Path):
        self.path = path
        self.name = path.name
        self.tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        # one pass: parents, functions, calls by callee name, string constants
        # (desktop_runtime.py is ~50k lines; every later lookup uses these indexes)
        self._parent: dict[ast.AST, ast.AST] | None = None  # built on first use
        self.functions: dict[str, list[ast.AST]] = {}
        self.calls: dict[str, list[ast.Call]] = {}
        self.strings: list[ast.Constant] = []
        self.sites: list[ast.AST] = []
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions.setdefault(node.name, []).append(node)
            elif isinstance(node, ast.Call):
                f = node.func
                name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
                if name:
                    self.calls.setdefault(name, []).append(node)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.strings.append(node)
            if (isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[0], ast.Constant)
                    and str(node.values[0].value).startswith("errors.")) or (
                    isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add) and isinstance(node.left, ast.Constant)
                    and isinstance(node.left.value, str) and node.left.value.startswith("errors.")):
                self.sites.append(node)
        self.globals: dict[str, ast.AST] = {}
        for node in self.tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.globals[target.id] = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
                self.globals[node.target.id] = node.value

    @property
    def parent(self) -> dict[ast.AST, ast.AST]:
        if self._parent is None:
            self._parent = {child: node for node in ast.walk(self.tree) for child in ast.iter_child_nodes(node)}
        return self._parent

    def enclosing_function(self, node: ast.AST):
        cur = self.parent.get(node)
        while cur is not None and not isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            cur = self.parent.get(cur)
        return cur

    def calls_to(self, name: str) -> list[ast.Call]:
        return list(self.calls.get(name, ()))

    def line(self, node: ast.AST) -> int:
        return getattr(node, "lineno", 0)


def _params(fn) -> list[str]:
    args = fn.args
    return [a.arg for a in [*args.posonlyargs, *args.args]]


def _bind(fn, call: ast.Call) -> dict[str, ast.AST]:
    """Map parameter names of ``fn`` to argument expressions of ``call``."""
    names = _params(fn)
    if names and names[0] in ("self", "cls"):
        names = names[1:]
    env: dict[str, ast.AST] = {}
    for name, arg in zip(names, call.args):
        if isinstance(arg, ast.Starred):
            raise Unresolved("starred argument")
        env[name] = arg
    for kw in call.keywords:
        if kw.arg is None:
            raise Unresolved("**kwargs")
        env[kw.arg] = kw.value
    defaults = fn.args.defaults
    positional = [a.arg for a in [*fn.args.posonlyargs, *fn.args.args]]
    for name, default in zip(positional[len(positional) - len(defaults):], defaults):
        env.setdefault(name, default)
    for a, default in zip(fn.args.kwonlyargs, fn.args.kw_defaults):
        if default is not None:
            env.setdefault(a.arg, default)
    return env


def _elements(node: ast.AST, mod: Module, depth: int) -> set:
    """Members of a literal collection (tuple/list/set/frozenset()/dict keys)."""
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        out: set = set()
        for elt in node.elts:
            out |= values(elt, mod, {}, depth + 1)
        return out
    if isinstance(node, ast.Dict):
        out = set()
        for key in node.keys:
            if key is None:
                raise Unresolved("dict unpacking")
            out |= values(key, mod, {}, depth + 1)
        return out
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("frozenset", "set", "tuple", "MappingProxyType") and len(node.args) == 1:
        return _elements(node.args[0], mod, depth + 1)
    if isinstance(node, ast.Name) and node.id in mod.globals:
        return _elements(mod.globals[node.id], mod, depth + 1)
    raise Unresolved(f"not a literal collection: {ast.dump(node)[:60]}")


def values(node: ast.AST, mod: Module, env: dict[str, set], depth: int = 0) -> set:
    """Every string (or NONE) ``node`` can evaluate to, or raise Unresolved."""
    if depth > MAX_DEPTH:
        raise Unresolved("too deep")
    if isinstance(node, ast.Constant):
        if node.value is None:
            return {NONE}
        if isinstance(node.value, str):
            return {node.value}
        raise Unresolved("non-string constant")
    if isinstance(node, ast.IfExp):
        return values(node.body, mod, env, depth + 1) | values(node.orelse, mod, env, depth + 1)
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        out: set = set()
        for operand in node.values:
            vals = values(operand, mod, env, depth + 1)
            out |= vals - {NONE, ""}
            if not (vals & {NONE, ""}):
                break
        return out
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append({v.value})
            elif isinstance(v, ast.FormattedValue):
                parts.append({x for x in values(v.value, mod, env, depth + 1) if x is not NONE})
            else:
                raise Unresolved("f-string part")
        return {"".join(p) for p in itertools.product(*parts)}
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = values(node.left, mod, env, depth + 1)
        right = values(node.right, mod, env, depth + 1)
        return {a + b for a in left for b in right if a is not NONE and b is not NONE}
    if isinstance(node, ast.Name):
        if node.id in env:
            bound = env[node.id]
            # bound is a set, or the caller's argument expression (evaluated lazily,
            # in the caller's scope, only when the key actually needs it)
            return set(bound) if isinstance(bound, set) else values(bound, mod, {}, depth + 1)
        fn = mod.enclosing_function(node)
        if fn is not None and not isinstance(fn, ast.Lambda):
            assigned = _local_values(node.id, fn, mod, env, depth)
            if assigned is not None:
                return assigned
        if node.id in mod.globals:
            return values(mod.globals[node.id], mod, {}, depth + 1)
        raise Unresolved(f"name {node.id}")
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in mod.functions:
        out = set()
        for fn in mod.functions[node.func.id]:
            for ret in ast.walk(fn):
                if isinstance(ret, ast.Return) and ret.value is not None and mod.enclosing_function(ret) is fn:
                    out |= values(ret.value, mod, {}, depth + 1)
        return out
    raise Unresolved(f"{type(node).__name__} at line {getattr(node, 'lineno', '?')}")


def _local_values(name: str, fn, mod: Module, env: dict[str, set], depth: int):
    """Values of a local name: parameter (via call sites), assignments or loop target."""
    out: set = set()
    found = False
    for node in ast.walk(fn):
        if mod.enclosing_function(node) is not fn:
            continue
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    found = True
                    out |= values(node.value, mod, env, depth + 1)
                elif isinstance(target, (ast.Tuple, ast.List)) and any(isinstance(e, ast.Name) and e.id == name for e in target.elts):
                    raise Unresolved(f"tuple unpacking of {name}")
        elif isinstance(node, ast.For) and isinstance(node.target, ast.Name) and node.target.id == name:
            found = True
            out |= _elements(node.iter, mod, depth + 1)
    if name in _params(fn) or name in [a.arg for a in fn.args.kwonlyargs]:
        found = True
        out |= _param_values(name, fn, mod, depth)
    return out if found else None


def _param_values(name: str, fn, mod: Module, depth: int) -> set:
    calls = mod.calls_to(fn.name)
    if not calls:
        raise Unresolved(f"parameter {name} of {fn.name}() has no call site in {mod.name}")
    out: set = set()
    for call in calls:
        env_nodes = _bind(fn, call)
        if name not in env_nodes:
            raise Unresolved(f"call to {fn.name}() at line {call.lineno} does not bind {name}")
        out |= values(env_nodes[name], mod, {}, depth + 1)
    return out


def site_values(site: ast.AST, mod: Module) -> set:
    """Evaluate one key-building expression, binding the enclosing function's
    parameters per call site (so ``reason or code`` stays precise)."""
    fn = mod.enclosing_function(site)
    if fn is None or isinstance(fn, ast.Lambda):
        return values(site, mod, {})
    names = [n.id for n in ast.walk(site) if isinstance(n, ast.Name)]
    params = [p for p in _params(fn) + [a.arg for a in fn.args.kwonlyargs] if p in names]
    reassigned = {t.id for node in ast.walk(fn) if isinstance(node, ast.Assign)
                  for t in node.targets if isinstance(t, ast.Name)}
    params = [p for p in params if p not in reassigned]
    if not params:
        return values(site, mod, {})
    calls = mod.calls_to(fn.name)
    if not calls:
        raise Unresolved(f"{fn.name}() has no call site in {mod.name}")
    out: set = set()
    for call in calls:
        bound = _bind(fn, call)
        env = {p: bound[p] if p in bound else {NONE} for p in params}
        out |= values(site, mod, env)
    return out


# ---------------------------------------------------------------------------
# Repository readers
# ---------------------------------------------------------------------------

_MODULES: dict[Path, Module] = {}


def module(repo: Path, name: str) -> Module:
    path = (repo / name).resolve()
    if path not in _MODULES:
        _MODULES[path] = Module(path)
    return _MODULES[path]


_TEXT: dict[Path, str] = {}


def wire_modules(repo: Path, needle: str) -> list[Module]:
    """Top-level modules of the sidecar (bridge_*.py and what they call into, e.g.
    desktop_runtime.py and runtime_settings_apply.py; tests are separate) whose source
    contains ``needle`` — a plain-text prefilter, so only relevant files are parsed."""
    out = []
    for path in sorted(repo.glob("*.py")):
        text = _TEXT.get(path)
        if text is None:
            text = _TEXT[path] = path.read_text(encoding="utf-8")
        if needle in text:
            out.append(module(repo, path.name))
    return out


def callers_constants(repo: Path, func: str, *, index: int | None = None, kw: str | None = None) -> tuple[set[str], int]:
    """call_constants over every top-level module."""
    found, dynamic = set(), 0
    for mod in wire_modules(repo, func + "("):
        vals, dyn = call_constants(mod, func, index=index, kw=kw)
        found |= vals
        dynamic += dyn
    return found, dynamic


def shell_literal_keys(repo: Path) -> list[tuple[str, str]]:
    """"errors.<…>" string literals in the shell sources (test modules excluded)."""
    out = []
    for path in sorted((repo / "shell" / "app" / "src").glob("*.rs")):
        text = path.read_text(encoding="utf-8").split("#[cfg(test)]", 1)[0]
        for m in re.finditer(r'"(errors\.[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*)"', text):
            out.append((m.group(1), f"shell:{path.name}:{text.count(chr(10), 0, m.start()) + 1}"))
    return out


def const_collection(repo: Path, file: str, name: str) -> set[str]:
    mod = module(repo, file)
    if name not in mod.globals:
        raise KeyError(f"{file}: {name} not found")
    return {v for v in _elements(mod.globals[name], mod, 0) if isinstance(v, str)}


def dict_value_tuples(repo: Path, file: str, name: str) -> list[tuple]:
    mod = module(repo, file)
    node = mod.globals[name]
    out = []
    for key, val in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and isinstance(val, ast.Tuple):
            out.append((key.value, *[e.value for e in val.elts if isinstance(e, ast.Constant)]))
    return out


def protocol_error_codes(repo: Path) -> set[str]:
    return const_collection(repo, "bridge_protocol.py", "ERROR_CODES")


def call_constants(mod: Module, func: str, *, index: int | None = None, kw: str | None = None) -> tuple[set[str], int]:
    """String constants passed to ``func`` (positional ``index`` or keyword ``kw``);
    returns (values, number of non-constant arguments)."""
    found, dynamic = set(), 0
    for call in mod.calls_to(func):
        arg = None
        if index is not None and len(call.args) > index:
            arg = call.args[index]
        if kw is not None:
            for k in call.keywords:
                if k.arg == kw:
                    arg = k.value
        if arg is None:
            continue
        try:
            found |= {v for v in values(arg, mod, {}) if isinstance(v, str)}
        except Unresolved:
            dynamic += 1
    return found, dynamic


def string_constants(mod: Module, pattern: re.Pattern) -> set[str]:
    return {n.value for n in mod.strings if pattern.fullmatch(n.value)}


def norm_import_reason(value: str) -> str | None:
    # bridge_import._reason
    text = re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")
    return text if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", text) else None


def skipped_reasons(repo: Path) -> tuple[set[str], list[str]]:
    """``ack.skipped_reason`` universe: protocol error codes (intents are validated
    against them) plus every constant given to Ack / RequestReceipt / StopIntent."""
    found = set(protocol_error_codes(repo))
    notes = []
    for path in sorted(repo.glob("bridge_*.py")):
        mod = module(repo, path.name)
        for func, index in (("Ack", 1), ("RequestReceipt", 3), ("StopIntent", 1)):
            vals, _dyn = call_constants(mod, func, index=index, kw="skipped_reason")
            found |= {v for v in vals if TOKEN_RE.fullmatch(v)}
            # dynamic arguments here are re-validated against ERROR_CODES by bridge_jobs
    found.discard("")
    notes.append("dynamic skipped_reason arguments are validated against bridge_protocol.ERROR_CODES (bridge_jobs.py)")
    return found, notes


# Dynamic arguments of the reason sources feeding errors.import.* / errors.autostart.*:
# every call site whose reason argument the evaluator cannot follow must be declared here
# (file, callee, enclosing function) together with where its values are enumerated instead.
# An undeclared one goes to ``unresolved`` (the gate fails), so a new reason family cannot
# slip through as a mere note (T-P4-03 fix round 1: parser.NodeValidationError was missed).
DYNAMIC_REASON_RULES: dict[tuple[str, str, str], str] = {
    ("bridge_import.py", "import_error", "_issue_error"):
        "_reason(issue.code, 'invalid_node'): import_service blocking_errors codes "
        "(parser.ContentIssue <- NodeValidationError, import_service._service_issue)",
    ("bridge_import.py", "import_error", "_reject"): "forwards _reject's reason (its call sites are enumerated)",
    ("bridge_import.py", "import_error", "_subscriptions"):
        "_reason(ImportDiagnosticError code, 'import_rejected'): import_service._import_diagnostic codes",
    ("bridge_import.py", "_reject", "_validate_draft"):
        "_reason(ShadowsocksCredentialIssue code, 'ss_credential_invalid'): shadowsocks_capabilities codes",
    ("bridge_import.py", "_reject", "_paste"): "ImportPlans.take reasons",
    ("bridge_import.py", "_reject", "_saved_nodes"):
        "_reason(ImportDiagnosticError code, 'import_rejected'): import_service._import_diagnostic codes",
    ("import_service.py", "_import_diagnostic", "_known_import_error_diagnostic"):
        "(phrases, code) pairs of _known_import_error_diagnostic",
    ("parser.py", "ContentIssue", "_analyze_content_blob"):
        "exc.code of NodeValidationError (its first argument, enumerated)",
    ("parser.py", "NodeValidationError", "_validate_node_for_runtime"):
        "credential_issue.code: ShadowsocksCredentialIssue codes",
    ("bridge_autostart.py", "_error", "autostart_state"):
        "manager._layout_error(): SidecarAutostartManager reason or validate_shell_path ValueError text (both enumerated)",
    ("bridge_autostart.py", "SidecarAutostartManager", "sidecar_autostart_manager"):
        "str(exc) restricted to {shell_missing, shell_path_invalid} by the enclosing conditional",
}


def reason_constants(mod: Module, func: str, *, index: int | None = None, kw: str | None = None,
                     unresolved: list[dict[str, Any]] | None = None) -> set[str]:
    """call_constants, except that every non-constant argument must have a
    DYNAMIC_REASON_RULES entry; the others are appended to ``unresolved``."""
    found: set[str] = set()
    for call in mod.calls_to(func):
        arg = None
        if index is not None and len(call.args) > index:
            arg = call.args[index]
        if kw is not None:
            for k in call.keywords:
                if k.arg == kw:
                    arg = k.value
        if arg is None:
            continue
        try:
            found |= {v for v in values(arg, mod, {}) if isinstance(v, str)}
        except Unresolved as exc:
            fname = getattr(mod.enclosing_function(call), "name", "<module>")
            if (mod.name, func, fname) not in DYNAMIC_REASON_RULES and unresolved is not None:
                unresolved.append({"site": f"{mod.name}:{call.lineno} {fname}() -> {func}()",
                                   "why": f"non-constant reason argument without a declared rule ({exc})"})
    return found


def import_reasons(repo: Path, unresolved: list[dict[str, Any]] | None = None) -> tuple[set[str], list[str]]:
    """Every ``errors.import.<reason>``: bridge_import's own constants plus the codes the
    import service and the parser hand to it (``_issue_error`` / ``ImportDiagnosticError``)."""
    unresolved = unresolved if unresolved is not None else []
    mod = module(repo, "bridge_import.py")
    found: set[str] = set()
    for func, idx in (("import_error", 1), ("_reject", 1), ("_reason", 1)):
        found |= reason_constants(mod, func, index=idx, unresolved=unresolved)
    # ImportPlans.take(...) -> (None, "<reason>")
    for fn in mod.functions.get("take", []):
        for ret in ast.walk(fn):
            if isinstance(ret, ast.Return) and isinstance(ret.value, ast.Tuple) and len(ret.value.elts) == 2:
                last = ret.value.elts[1]
                if isinstance(last, ast.Constant) and isinstance(last.value, str) and last.value:
                    found.add(last.value)
    notes = ["bridge_import: import_error/_reject/_reason constants and ImportPlans.take reasons",
             "dynamic reason arguments are declared in DYNAMIC_REASON_RULES; undeclared ones are unresolved"]
    svc = module(repo, "import_service.py")
    for func in ("_import_diagnostic", "_service_issue"):
        found |= reason_constants(svc, func, index=0, unresolved=unresolved)
    # (phrases, code) pairs of _known_import_error_diagnostic
    for fn in svc.functions.get("_known_import_error_diagnostic", []):
        for node in ast.walk(fn):
            if isinstance(node, ast.Tuple) and len(node.elts) == 2 and isinstance(node.elts[1], ast.Constant) \
                    and isinstance(node.elts[1].value, str) and isinstance(node.elts[0], ast.Tuple):
                found.add(node.elts[1].value)
    parser = module(repo, "parser.py")
    # ContentIssue(code=exc.code) forwards NodeValidationError's first argument:
    # parser._analyze_content_blob -> import_service blocking_errors -> bridge_import._issue_error
    found |= reason_constants(parser, "ContentIssue", kw="code", unresolved=unresolved)
    found |= reason_constants(parser, "ContentIssue", index=0, unresolved=unresolved)
    found |= reason_constants(parser, "NodeValidationError", index=0, unresolved=unresolved)
    ss = module(repo, "shadowsocks_capabilities.py")
    found |= reason_constants(ss, "ShadowsocksCredentialIssue", kw="code", unresolved=unresolved)
    found |= reason_constants(ss, "ShadowsocksCredentialIssue", index=0, unresolved=unresolved)
    out = {r for r in (norm_import_reason(v) for v in found) if r}
    return out, notes


def autostart_reasons(repo: Path, unresolved: list[dict[str, Any]] | None = None) -> set[str]:
    unresolved = unresolved if unresolved is not None else []
    mod = module(repo, "bridge_autostart.py")
    found = reason_constants(mod, "_error", index=0, unresolved=unresolved)
    found |= reason_constants(mod, "SidecarAutostartManager", index=2, unresolved=unresolved)
    found |= reason_constants(mod, "ValueError", index=0, unresolved=unresolved)
    return {v for v in found if TOKEN_RE.fullmatch(v)}


def host_failures(repo: Path) -> tuple[list[tuple[str, str]], int]:
    path = repo / "shell" / "app" / "src" / "host.rs"
    text = path.read_text(encoding="utf-8")
    text = text.split("#[cfg(test)]", 1)[0]
    pairs = sorted(set(re.findall(r'\bfail\(\s*"([a-z_]+)"\s*,\s*"([a-z_]+)"\s*\)', text)))
    pairs += sorted(set(re.findall(r'Fail\s*\{\s*code:\s*"([a-z_]+)"\s*,\s*reason:\s*"([a-z_]+)"\s*\}', text)))
    calls = len(re.findall(r'\bfail\(', text)) - text.count("fn fail(")
    literal = len(re.findall(r'\bfail\(\s*"[a-z_]+"\s*,\s*"[a-z_]+"\s*\)', text))
    # fail(<variable>, "<reason>"): declared forwards. "grant_not_sent" forwards the shell
    # lane's send error, which is job_capacity (lane full) or runtime_gone (session moved
    # on) — host.rs save-grant comment. Anything else stays unresolved.
    forwards = {"grant_not_sent": ("job_capacity", "runtime_gone")}
    covered = 0
    for reason in re.findall(r'\bfail\(\s*[a-z_]+\s*,\s*"([a-z_]+)"\s*\)', text):
        if reason in forwards:
            covered += 1
            pairs += [(code, reason) for code in forwards[reason]]
    return sorted(set(pairs)), calls - literal - covered


def protocol_section_keys(repo: Path, section: str) -> set[str]:
    text = (repo / "bridge_schema" / "PROTOCOL.md").read_text(encoding="utf-8")
    m = re.search(rf"^## {re.escape(section)}\b.*?(?=^## )", text, re.S | re.M)
    if not m:
        raise KeyError(f"PROTOCOL §{section} not found")
    return set(re.findall(r"errors\.[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*", m.group(0)))


def fixture_keys(repo: Path) -> dict[str, set[str]]:
    """message_key / errors.* / catalog.* strings in fixture JSON (not invalid/, not i18n gate fixtures)."""
    out: dict[str, set[str]] = {"errors": set(), "catalog": set()}
    roots = [repo / "bridge_schema" / "fixtures", repo / "ui" / "test" / "fixtures"]
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.json")):
            rel = path.relative_to(repo).as_posix()
            parts = path.relative_to(root).parts
            if "invalid" in parts or rel.startswith("ui/test/fixtures/i18n/"):
                continue
            try:
                doc = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            stack = [doc]
            while stack:
                cur = stack.pop()
                if isinstance(cur, dict):
                    stack.extend(cur.values())
                elif isinstance(cur, list):
                    stack.extend(cur)
                elif isinstance(cur, str):
                    if KEY_RE.fullmatch(cur):
                        out["errors"].add(cur)
                    elif cur.startswith("catalog.") and re.fullmatch(r"catalog\.[A-Za-z0-9_.\-]+", cur):
                        out["catalog"].add(cur)
    return out


def catalog_ids(repo: Path) -> tuple[dict[str, set[str]], list[str]]:
    sys.path.insert(0, str(repo))
    notes: list[str] = []
    import bridge_catalog  # noqa: E402  (Qt-free; asserted below)
    if any(name.startswith(("PySide6", "PyQt")) for name in sys.modules):
        raise RuntimeError("bridge_catalog pulled in Qt")
    out = {"label": set(), "choice": set(), "notice": set()}
    specs = bridge_catalog.build_import_specs()
    # alerts.catalog (alert channels) was removed by the 10-30 alert redesign.
    for proto in specs["protocols"]:
        out["label"].add(proto["label_id"])
        for field in proto["fields"]:
            out["label"].add(field["label_id"])
            for choice in field.get("choices") or []:
                out["choice"].add(f"{field['label_id']}.choice.{choice}")
            for notice in (field.get("conditions") or {}).get("notices") or []:
                out["notice"].add(f"catalog.notice.{notice['code']}")
    notes.append(f"import.specs: {len(specs['protocols'])} protocols")
    return out, notes


# ---------------------------------------------------------------------------
# Dynamic key sites
# ---------------------------------------------------------------------------

def dynamic_sites(mod: Module) -> list[ast.AST]:
    """f-strings and "errors.x." + y concatenations (indexed by Module)."""
    return list(mod.sites)


def enumerate_all(repo: Path) -> dict[str, Any]:
    keys: dict[str, set[str]] = {}
    unresolved: list[dict[str, Any]] = []
    notes: list[str] = []

    def add(key: str, source: str) -> None:
        if not KEY_RE.fullmatch(key):
            unresolved.append({"site": source, "why": f"generated an invalid key {key!r}"})
            return
        keys.setdefault(key, set()).add(source)

    error_codes = protocol_error_codes(repo)
    skipped, skipped_notes = skipped_reasons(repo)
    notes += skipped_notes
    start_keys = {t[2] for t in dict_value_tuples(repo, "bridge_messages.py", "_REASONS")}
    diagnose = const_collection(repo, "bridge_diagnostics.py", "DIAGNOSE_REASON_CODES")
    exit_reasons = const_collection(repo, "bridge_exit.py", "EXIT_REASON_CODES")
    imports, import_notes = import_reasons(repo, unresolved)
    notes += import_notes
    settings_messages = {"apply_failed"}
    for idx, kw in ((2, None), (None, "message")):
        vals, dyn = callers_constants(repo, "apply_error", index=idx, kw=kw)
        settings_messages |= vals
        if dyn:
            unresolved.append({"site": "apply_error() callers", "why": f"{dyn} non-constant message argument(s)"})

    # Declared expansions for sites the evaluator cannot follow (file, function).
    rules = {
        ("bridge_jobs.py", "_error"): ("errors.{c}", error_codes, "bridge_protocol.ERROR_CODES"),
        ("bridge_main.py", "reject_request"): ("errors.bridge.{c}", error_codes | skipped, "ERROR_CODES ∪ ack.skipped_reason"),
        ("bridge_main.py", "pump"): ("errors.bridge.{c}", error_codes | skipped, "ERROR_CODES ∪ ack.skipped_reason"),
        ("bridge_main.py", "*"): ("errors.bridge.{c}", error_codes | skipped, "ERROR_CODES ∪ ack.skipped_reason"),
        ("bridge_messages.py", "classify_start_failure"): ("errors.start.{c}", start_keys | {"unexpected"}, "bridge_messages._REASONS"),
        ("bridge_import.py", "import_error"): ("errors.import.{c}", imports, "bridge_import/import_service/parser/shadowsocks reason constants"),
        ("bridge_autostart.py", "_error"): ("errors.autostart.{c}", autostart_reasons(repo, unresolved), "bridge_autostart reason constants"),
        ("bridge_vocab.py", "error_message_key"): None,  # expanded below from its callers
        # runtime_settings_apply.apply_error(code, reason_code, message="apply_failed"): called from
        # desktop_runtime.py as well, so the message values come from every module's call sites
        ("runtime_settings_apply.py", "apply_error"): ("errors.settings.{c}", settings_messages, "apply_error message argument (all callers) + default apply_failed"),
    }

    for mod in wire_modules(repo, "errors."):
        # literal keys
        for node in mod.strings:
            if KEY_RE.fullmatch(node.value):
                add(node.value, f"literal:{mod.name}:{node.lineno}")
        for site in dynamic_sites(mod):
            fn = mod.enclosing_function(site)
            fname = getattr(fn, "name", "<module>")
            where = f"{mod.name}:{site.lineno} {fname}()"
            rule_key = (mod.name, fname) if (mod.name, fname) in rules else (mod.name, "*") if (mod.name, "*") in rules else None
            if rule_key is not None:
                rule = rules[rule_key]
                if rule is None:
                    continue
                template, codes, why = rule
                for c in sorted(codes):
                    add(template.format(c=c), f"rule:{where} [{why}]")
                continue
            try:
                for value in sorted(v for v in site_values(site, mod) if isinstance(v, str)):
                    add(value, f"expanded:{where}")
            except Unresolved as exc:
                unresolved.append({"site": where, "why": str(exc)})

    # error_message_key(code, domain=...) callers
    for mod in wire_modules(repo, "error_message_key("):
        for call in mod.calls_to("error_message_key"):
            fn = mod.enclosing_function(call)
            fname = getattr(fn, "name", "<module>")
            where = f"{mod.name}:{call.lineno} {fname}()"
            if mod.name == "bridge_diagnostics.py":
                for c in sorted(diagnose | {"unavailable"}):
                    add(f"errors.diagnose.{c}", f"rule:{where} [DIAGNOSE_REASON_CODES + unavailable]")
            elif mod.name == "bridge_exit.py":
                for c in sorted(exit_reasons):
                    add(f"errors.exit.{c}", f"rule:{where} [EXIT_REASON_CODES]")
            else:
                unresolved.append({"site": where, "why": "error_message_key caller without a rule"})

    # PROTOCOL §14 table (start failures) and §7 examples
    for key in sorted(protocol_section_keys(repo, "14")):
        add(key, "protocol:§14")
    for key in sorted(protocol_section_keys(repo, "15")):
        add(key, "protocol:§15")

    # shell literal keys (e.g. transport.rs errors.bridge.job_capacity) and host commands: errors.host.<code>
    for key, src in shell_literal_keys(repo):
        add(key, src)
    host_pairs, host_dynamic = host_failures(repo)
    for code, _reason in host_pairs:
        add(f"errors.host.{code}", "shell:host.rs fail()")
    if host_dynamic:
        unresolved.append({"site": "shell/app/src/host.rs", "why": f"{host_dynamic} fail() call(s) with non-literal arguments"})

    # the catch-all key the page uses for anything unknown
    add("errors.unknown", "ui:fallback")

    fixtures = fixture_keys(repo)
    for key in sorted(fixtures["errors"]):
        add(key, "fixture")

    catalog, catalog_notes = catalog_ids(repo)
    notes += catalog_notes
    for key in fixtures["catalog"]:
        catalog["label"].add(key)

    # reason_code vocabularies per page namespace (architecture §3.1, critique B7)
    node_reasons = set()
    nsv = module(repo, "node_status_view.py")
    for fn in nsv.functions.get("_runtime_failure_display_category", []):
        for ret in ast.walk(fn):
            if isinstance(ret, ast.Return) and isinstance(ret.value, ast.Tuple) and ret.value.elts \
                    and isinstance(ret.value.elts[0], ast.Constant) and ret.value.elts[0].value:
                node_reasons.add(ret.value.elts[0].value)
    nodes_mod = module(repo, "bridge_nodes.py")
    node_reasons |= {"disabled", "subscription_recheck_pending", "node_unavailable"} & string_constants(nodes_mod, TOKEN_RE)
    alerts_mod = module(repo, "bridge_alerts.py")
    alert_test, _ = call_constants(alerts_mod, "_failed", index=1)
    alert_test |= call_constants(alerts_mod, "_error", index=1)[0]
    # alerts.onboarding was removed by the 10-30 alert redesign (bridge_onboarding.py
    # retired): no sidecar sends an Onboarding.reason_code any more.
    onboarding: set[str] = set()
    exit_mod = module(repo, "bridge_exit.py")
    phases, _ = call_constants(exit_mod, "_set_running", index=1)
    exit_all = exit_reasons | const_collection(repo, "bridge_exit.py", "FALLBACK_FLAGS") \
        | const_collection(repo, "bridge_exit.py", "WARNING_REASONS") \
        | const_collection(repo, "overview_auto_identity.py", "AUTO_SKIPPED_REASONS") | phases
    reasons = {
        "common.skipped": sorted(skipped),
        "tiers.reason": sorted(diagnose | node_reasons),
        "alerts.test_reason": sorted(alert_test),
        "overview.exit_reason": sorted(exit_all),
        "alerts.onboarding_reason": sorted(onboarding),
        "shell.host_reason": sorted({r for _c, r in host_pairs}),
    }
    reason_sources = {
        "common.skipped": "bridge_protocol.ERROR_CODES + Ack/RequestReceipt/StopIntent constants (bridge_*.py)",
        "tiers.reason": "bridge_diagnostics.DIAGNOSE_REASON_CODES (nodes.diagnose) + Node.reason_code (node_status_view display categories, bridge_nodes)",
        "alerts.test_reason": "bridge_alerts._failed/_error reason constants (alerts.test)",
        "overview.exit_reason": "bridge_exit EXIT_REASON_CODES, FALLBACK_FLAGS, WARNING_REASONS, running phases; overview_auto_identity.AUTO_SKIPPED_REASONS (IdentityLeg/IdentityResult)",
        "alerts.onboarding_reason": "none: alerts.onboarding was removed by the 10-30 alert redesign",
        "shell.host_reason": "shell/app/src/host.rs fail(code, reason)",
    }

    by_domain: dict[str, int] = {}
    for key in keys:
        parts = key.split(".")
        domain = parts[1] if len(parts) > 2 else "(code)"
        by_domain[domain] = by_domain.get(domain, 0) + 1
    return {
        "errors": {k: sorted(v) for k, v in sorted(keys.items())},
        "errors_by_domain": dict(sorted(by_domain.items())),
        "catalog": {k: sorted(v) for k, v in catalog.items()},
        "reasons": reasons,
        "reason_sources": reason_sources,
        "unresolved": unresolved,
        "notes": notes,
        "qt_free": not any(name.startswith(("PySide6", "PyQt")) for name in sys.modules),
    }


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args(list(argv) if argv is not None else None)
    result = enumerate_all(Path(args.repo).resolve())
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=1, sort_keys=False) + "\n")
    if args.strict and result["unresolved"]:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
