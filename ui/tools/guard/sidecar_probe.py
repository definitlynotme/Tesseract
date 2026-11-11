"""Real-sidecar contract probe of the UI guard (T-P6-05, item 3).

    python -B ui/tools/guard/sidecar_probe.py --exe <dist>/tesseract-runtime.exe --out <dir>
    python -B ui/tools/guard/sidecar_probe.py --source --out <dir>      # runtime_main.pyw of this tree

Starts ONE sidecar in an isolated custom scope and talks to it the way the shell does, over a
private overlapped pipe (the T-P2-33 driver, tests/test_bridge_p2_33.py, imported read only):

- sandbox: tests/test_bridge_p2_33.prepare_sandbox -- custom scope under %TEMP%, empty
  sources.yaml with the three migration markers, every listener on a free high port, every
  start-up network action off (Tier3, GeoData, DNS qualification, system proxy, kill switch,
  egress proxy, automatic exit lookup, every alert channel), APPDATA / LOCALAPPDATA inside the
  sandbox, an allow-listed environment without TESSERACT_ISOLATED_AUDIT_MANIFEST;
- `isolation_problems` is checked first: any problem means the sidecar is NOT started;
- the HKCU proxy values and the Tesseract firewall rule names are read before and after;
- hello, hello_ack, one subscribe of every hello topic, the initial snapshots, a set of
  read-only queries (every v1 query plus settings.validate), app.quit with the shell identity,
  bye; every frame goes through bridge_protocol.validate_frame (the Sidecar driver does it).

Nothing here writes outside --out and the temporary sandbox (deleted at the end). service.start
is never sent, so Mihomo is never launched. Writes <out>/frames.jsonl and <out>/probe.json and
exits 1 when the probe found a problem.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_REPO = HERE.parents[2]
SCOPE_LABEL = "ui-guard-probe"
# The queries a page makes without side effects (PROTOCOL section 11), plus settings.validate
# (a pure validation that answers res).
EXTRA_READS = ("settings.validate",)


def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def isolation_problems(sandbox: dict, *, live_env: dict | None = None) -> list[str]:
    """The T-P2-33 smoke rules (same as scripts/build_shell_preview.py isolation_problems, kept
    here so the guard does not import a file another ticket is editing). Any problem: do not
    start."""
    import yaml
    live_env = os.environ if live_env is None else live_env
    problems: list[str] = []
    env = sandbox["env"]
    temp_root = Path(tempfile.gettempdir()).resolve()
    for key in ("TESSERACT_CONFIG_DIR", "TESSERACT_DATA_DIR", "APPDATA", "LOCALAPPDATA", "TEMP"):
        path = Path(env.get(key, "") or ".").resolve()
        if temp_root not in path.parents:
            problems.append(f"{key} is not under the temporary directory: {path}")
    for live in (live_env.get("APPDATA"), live_env.get("LOCALAPPDATA")):
        if not live:
            continue
        live_dir = Path(live, "Tesseract").resolve()
        for key in ("TESSERACT_CONFIG_DIR", "TESSERACT_DATA_DIR"):
            value = env.get(key)
            if value and (Path(value).resolve() == live_dir or live_dir in Path(value).resolve().parents):
                problems.append(f"{key} lies in the live profile {live_dir}")
    if not env.get("TESSERACT_INSTANCE_SCOPE", "").strip():
        problems.append("no custom scope label (TESSERACT_INSTANCE_SCOPE)")
    if "TESSERACT_ISOLATED_AUDIT_MANIFEST" in env:
        problems.append("an isolated-audit manifest is set")
    config_dir = Path(sandbox["config_dir"])
    sources = yaml.safe_load((config_dir / "sources.yaml").read_text(encoding="utf-8")) or {}
    for name in ("github_repos", "telegram_channels", "subscription_urls", "local_files"):
        if sources.get(name):
            problems.append(f"sources.yaml {name} is not empty")
    for marker in ("tier3_source_portfolio_defaults_v1", "tier3_source_portfolio_defaults_v2",
                   "tier3_source_default_delivery_v1"):
        if (sources.get("migrations") or {}).get(marker) is not True:
            problems.append(f"sources.yaml lacks the migration marker {marker}")
    if (sources.get("github_discovery") or {}).get("enabled") is not False:
        problems.append("GitHub discovery is not off")
    config = yaml.safe_load((config_dir / "config.yaml").read_text(encoding="utf-8"))
    off = {"tiers.tier3": config["tiers"]["tier3"]["enabled"], "geo_update": config["geo_update"]["enabled"],
           "system_proxy": config["client"]["system_proxy"]["enabled"], "killswitch": config["killswitch"]["enabled"],
           "egress_proxy": config["egress_proxy"]["enabled"], "dns": config["client"]["dns"]["enable"],
           "auto_exit_lookup": config["ui"]["overview_auto_exit_lookup"]}
    problems += [f"{name} is on" for name, value in off.items() if value]
    for name, port in sandbox["ports"].items():
        if not 20000 <= int(port) < 40000:
            problems.append(f"{name} port {port} is not a free high port")
    return problems


def _enum_walk(spec, path: str, out: dict, dtos: dict, tiers: list, depth: int = 0) -> None:
    """Collect the closed string sets of one bridge_protocol spec: "a|b|c" fields (a trailing "?" is
    the optional marker), "tier", inside DTOs and arrays ("[X]" -> "<path>[]")."""
    if depth > 16:
        return
    if isinstance(spec, dict):
        for field, field_spec in spec.items():
            _enum_walk(field_spec, f"{path}.{field.rstrip('?')}", out, dtos, tiers, depth + 1)
        return
    if not isinstance(spec, str):
        return
    name = spec[:-1] if spec.endswith("?") else spec
    if name.startswith("[") and name.endswith("]"):
        _enum_walk(name[1:-1], f"{path}[]", out, dtos, tiers, depth + 1)
    elif name in dtos:
        _enum_walk(dtos[name], path, out, dtos, tiers, depth + 1)
    elif name == "tier":
        out[path] = list(tiers)
    elif "|" in name and not name.startswith("="):
        out[path] = sorted(name.split("|"))


# The enum fields of the P1 topics (review 1 MUST 1). bridge_protocol checks these topics with
# functions (_TOPIC_VALIDATORS: validate_service_state, validate_jobs, ...), not with a declarative
# spec, so their enum fields are listed here, with the paths project_frames.mjs uses. The value says
# where the declared values come from: a module constant of bridge_protocol (getattr), or None: the
# set the field's predicate passes to _enum, read from the validator's source with ast
# (p1_enum_scan). A field the scan finds that this table lacks is reported (test_ui_guard.py and
# guard item 1), so a new P1 enum field of the backend cannot slip past the guard.
P1_ENUM_PATHS: dict[str, dict[str, str | None]] = {
    "topic:service.state": {".readiness.state": "READINESS_STATES", ".runtime.startup_phase": None},
    "topic:route.published": {".managed_tier": "_TIERS"},
    "topic:tiers.summary": {".carrier": "_TIERS", ".order[]": "_TIERS"},
    "topic:traffic.history": {".source": None},
    "topic:logs.batch": {".records[].source": None, ".records[].level": None, ".records[].category": None},
    "topic:jobs": {".name": None, ".state": "JOB_STATES", ".result.startup_phase": None},
}
# Enum fields the scan finds inside shared helpers whose path is a parameter (not one topic's
# field): `code` of _error_payload is the error code, whose texts items 2 and 3 already check
# (backend_keys errors.*, text_missing); listed by "<function>.<field>".
P1_ENUM_EXEMPT = {"_error_payload.code": "error code: items 2 / 3 check its texts (errors.*), not an enum the page maps"}


def _p1_resolve_path(node, assigns: dict) -> str | None:
    """The path argument of one _need / _optional call as a string ("[{index}]" -> "[]"), a local
    `path = ...` followed once; None when it cannot be read statically (a parameter)."""
    import ast
    if isinstance(node, ast.Name):
        node = assigns.get(node.id)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        text = "".join(part.value if isinstance(part, ast.Constant) else "\0" for part in node.values)
        text = text.replace("[\0]", "[]")
        return None if "\0" in text else text
    return None


def _p1_enum_call(predicate):
    """(the _enum call inside a field predicate, whether it checks list items) or (None, False)."""
    import ast
    param = predicate.args.args[0].arg if isinstance(predicate, ast.Lambda) and predicate.args.args else None
    for node in ast.walk(predicate):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_enum" and len(node.args) == 2:
            item = not (isinstance(node.args[0], ast.Name) and node.args[0].id == param)
            return node, item
    return None, False


def p1_enum_scan(protocol) -> dict:
    """The enum fields of the P1 topic validators, read from the target bridge_protocol's source:
    every _need / _optional with enum=True or whose predicate calls _enum, in the functions of
    _TOPIC_VALIDATORS and the module functions they call. -> {"fields": {"topic:<t>": {"<path>":
    "<the set expression>"}}, "unresolved": ["<function>.<field>"], "error": str | None}."""
    import ast
    import inspect
    result: dict = {"fields": {}, "unresolved": [], "error": None}
    try:
        tree = ast.parse(inspect.getsource(protocol))
        validators = getattr(protocol, "_TOPIC_VALIDATORS")
        topics = sorted(validators, key=len, reverse=True)
        funcs = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        todo, seen = [getattr(f, "__name__", "") for f in validators.values()], set()
        while todo:
            name = todo.pop()
            if name in seen or name not in funcs:
                continue
            seen.add(name)
            func = funcs[name]
            assigns = {node.targets[0].id: node.value for node in ast.walk(func) if isinstance(node, ast.Assign)
                       and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)}
            for node in ast.walk(func):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                    continue
                if node.func.id in funcs:
                    todo.append(node.func.id)
                if node.func.id not in ("_need", "_optional") or len(node.args) < 4:
                    continue
                key = node.args[1].value if isinstance(node.args[1], ast.Constant) else None
                flagged = any(k.arg == "enum" and isinstance(k.value, ast.Constant) and k.value.value is True for k in node.keywords)
                call, item = _p1_enum_call(node.args[3])
                if not isinstance(key, str) or not (flagged or call is not None):
                    continue
                base = _p1_resolve_path(node.args[2], assigns)
                full = f"{base}.{key}" if base else key
                topic = next((t for t in topics if base is not None and (full.startswith(t + ".") or full.startswith(t + "["))), None)
                if topic is None:
                    result["unresolved"].append(f"{name}.{key}")
                    continue
                path = full[len(topic):] + ("[]" if item else "")
                result["fields"].setdefault(f"topic:{topic}", {})[path] = ast.unparse(call.args[1]) if call is not None else None
        result["unresolved"] = sorted(set(result["unresolved"]))
    except Exception as exc:  # reported by the guard (item 1) and the self-test
        result["error"] = f"{type(exc).__name__}: {exc}"[:500]
    return result


def _p1_declared(protocol, source: str | None, expression: str | None) -> list:
    """Declared values of one P1 enum field: the union of the named module constant and the
    scanned set expression (a literal, or evaluated in bridge_protocol's own namespace), so a value
    the validator adds next to the constant (CONST | {"new"}) still counts as declared; [] when
    neither is readable (the dynamic rule of project_frames.mjs needs only the path)."""
    import ast
    values = set()
    if source:
        try:
            values |= {str(v) for v in getattr(protocol, source)}
        except Exception:
            pass
    if expression:
        try:
            try:
                scanned = ast.literal_eval(expression)
            except ValueError:
                scanned = eval(compile(expression, "<bridge_protocol enum>", "eval"), vars(protocol))  # the tree's own code
            values |= {str(v) for v in scanned}
        except Exception:
            pass
    return sorted(values)


def p1_enum_paths(protocol, scan: dict | None = None) -> tuple[dict, list[str]]:
    """(P1_ENUM_PATHS with the declared values, the scanned P1 enum fields the table lacks)."""
    scan = p1_enum_scan(protocol) if scan is None else scan
    out = {}
    for key, paths in P1_ENUM_PATHS.items():
        if key[len("topic:"):] not in set(getattr(protocol, "V1_TOPICS", ())):
            continue
        scanned = scan["fields"].get(key, {})
        out[key] = {path: _p1_declared(protocol, source, scanned.get(path)) for path, source in paths.items()}
    unlisted = sorted(f"{key} {path}" for key, paths in scan["fields"].items() for path in paths
                      if path not in P1_ENUM_PATHS.get(key, {}))
    unlisted += [f"unresolved {name}" for name in scan["unresolved"] if name not in P1_ENUM_EXEMPT]
    return out, unlisted


def enum_paths(protocol) -> dict:
    """{"topics": {"topic:<t>" | "res:<cmd>": {"<path>": [values]}}, "uncovered": [...],
    "p1_unlisted": [...], "p1_scan_error": str | None}: the enum fields of every v1 topic payload and
    query reply of the target tree's bridge_protocol, with the paths project_frames.mjs uses
    (".proxy_state", ".items[].status"). The function-checked P1 topics come from P1_ENUM_PATHS
    (declared values from bridge_protocol); `uncovered` lists the topics with neither (they have no
    enum field: core.info, traffic.tick), `p1_unlisted` the P1 enum fields the scan found that the
    table lacks (guard item 1 fails on them)."""
    dtos, tiers = getattr(protocol, "V1_DTOS", {}), sorted(getattr(protocol, "_TIERS", ()))
    scan = p1_enum_scan(protocol)
    p1, unlisted = p1_enum_paths(protocol, scan)
    out: dict = dict(p1)
    for topic, spec in getattr(protocol, "V1_TOPIC_SPECS", {}).items():
        paths: dict = {}
        _enum_walk(spec[2], "", paths, dtos, tiers)
        if paths:
            out[f"topic:{topic}"] = paths
    for cmd, spec in getattr(protocol, "V1_QUERY_SPECS", {}).items():
        paths = {}
        _enum_walk(spec[1], "", paths, dtos, tiers)
        if paths:
            out[f"res:{cmd}"] = paths
    uncovered = sorted(set(getattr(protocol, "V1_TOPICS", ())) - set(getattr(protocol, "V1_TOPIC_SPECS", {}))
                       - {key[len("topic:"):] for key in p1})
    return {"topics": out, "uncovered": uncovered, "p1_unlisted": unlisted, "p1_scan_error": scan["error"]}


def _fields_walk(spec, path: str, out: dict, dtos: dict, depth: int = 0) -> None:
    if depth > 16:
        return
    if isinstance(spec, dict):
        out.setdefault(path, set()).update(field.rstrip("?") for field in spec)
        for field, field_spec in spec.items():
            _fields_walk(field_spec, f"{path}.{field.rstrip('?')}", out, dtos, depth + 1)
        return
    if not isinstance(spec, str):
        return
    name = spec[:-1] if spec.endswith("?") else spec
    if name.startswith("[") and name.endswith("]"):
        _fields_walk(name[1:-1], f"{path}[]", out, dtos, depth + 1)
    elif name in dtos:
        _fields_walk(dtos[name], path, out, dtos, depth + 1)


def schema_fields(protocol) -> dict:
    """{"<topic>": {"<object path>": [declared keys]}} of every v1 topic payload (paths as in
    enum_paths: "" for the payload, ".items[]", ".pending"): the guard names the field a fixture
    still carries when bridge_protocol reports only `unknown:<topic>`."""
    dtos, out = getattr(protocol, "V1_DTOS", {}), {}
    for topic, spec in getattr(protocol, "V1_TOPIC_SPECS", {}).items():
        paths: dict = {}
        _fields_walk(spec[2], "", paths, dtos)
        if paths:
            out[topic] = {path: sorted(keys) for path, keys in paths.items()}
    return out


def _load_protocol(repo: Path):
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    import bridge_protocol as protocol  # noqa: E402  (the target tree's contract)
    return protocol


def _load_driver(repo: Path):
    protocol = _load_protocol(repo)
    import tests.test_bridge_p2_33 as driver  # noqa: E402  (read only: sandbox + pipe driver)
    return protocol, driver


def _hello_problems(protocol, driver, hello: dict, pid: int) -> list[str]:
    out = []
    caps = hello.get("caps") or {}
    commands, topics = set(caps.get("commands") or ()), set(caps.get("topics") or ())
    if commands != set(protocol.HELLO_CAP_COMMANDS):
        out.append(f"hello.caps.commands differs from bridge_protocol.HELLO_CAP_COMMANDS: "
                   f"{sorted(commands ^ set(protocol.HELLO_CAP_COMMANDS))}")
    if topics != set(protocol.V1_TOPICS):
        out.append(f"hello.caps.topics differs from bridge_protocol.V1_TOPICS: {sorted(topics ^ set(protocol.V1_TOPICS))}")
    scope = hello.get("scope") or {}
    if scope.get("kind") != "custom":
        out.append(f"hello.scope.kind {scope.get('kind')!r} (the probe runs a custom scope)")
    if hello.get("runtime_pid") != pid:
        out.append(f"hello.runtime_pid {hello.get('runtime_pid')} is not the probe's sidecar {pid}")
    if hello.get("network_recovery") != driver.ISOLATED_RECOVERY:
        out.append(f"hello.network_recovery {hello.get('network_recovery')}")
    return out


def run_probe(*, repo: Path, exe: Path | None, out: Path, settle_s: float = 20.0,
              hello_timeout_s: float = 30.0) -> dict:
    protocol, driver = _load_driver(repo)
    out.mkdir(parents=True, exist_ok=True)
    result: dict = {"started_utc": utc(), "form": "frozen" if exe else "source", "exe": str(exe) if exe else None,
                    "repo": str(repo), "started": False, "failures": [], "queries": [], "metrics": {},
                    # slots this stopped scope cannot fill (T-P2-33); project_frames.mjs records
                    # their gap / resync as known instead of failing on them
                    "stopped_unavailable": dict(driver.STOPPED_UNAVAILABLE),
                    # the target tree's enum fields; project_frames.mjs reports a real value on one
                    # of these paths the UI fixtures never carry (enum_unknown)
                    "enum_paths": enum_paths(protocol)}
    temp = Path(tempfile.mkdtemp(prefix="tesseract-ui-guard-probe-"))
    failures: list[str] = result["failures"]
    try:
        sandbox = driver.prepare_sandbox(temp / "sandbox", frozen=exe is not None)
        sandbox["env"]["TESSERACT_INSTANCE_SCOPE"] = SCOPE_LABEL
        result["ports"] = sandbox["ports"]
        problems = isolation_problems(sandbox)
        if exe is not None and not Path(exe).is_file():
            problems.append(f"missing sidecar {exe}")
        if problems:
            failures.append(f"not started (isolation): {problems}")
            result["isolation_problems"] = problems
            return result
        before = driver.host_facts()
        result["host_before"] = before
        if "error" in before["firewall"]:
            failures.append(f"not started: no firewall baseline ({before['firewall']['error']})")
            return result
        report = driver.Report()
        sidecar = driver.Sidecar(sandbox, exe=str(exe) if exe else None, logs=out / "sidecar")
        result["started"] = True
        try:
            _scenario(protocol, driver, sidecar, report, result, settle_s=settle_s, hello_timeout_s=hello_timeout_s)
        except Exception as exc:  # recorded; the verdict fails on it
            failures.append(f"probe aborted: {exc!r}"[:2000])
        finally:
            alive = sidecar.close()
            result["residual_processes"] = alive
            result["descendants_seen"] = sorted({name for name, _ in sidecar.watch.seen.values()})
            if alive:
                failures.append(f"residual processes after the sidecar exited: {alive}")
            failures.extend(f"frame: {p}" for p in sidecar.problems)
            failures.extend(report.problems)
            with open(out / "frames.jsonl", "w", encoding="utf-8", newline="\n") as stream:
                for frame in sidecar.frames:
                    stream.write(json.dumps(frame, ensure_ascii=False, sort_keys=True) + "\n")
            result["metrics"]["frames"] = len(sidecar.frames)
            result["metrics"]["exit_code"] = sidecar.process.poll()
        after = driver.host_facts()
        result["host_after"] = after
        if before["internet_settings"] != after["internet_settings"]:
            failures.append(f"HKCU Internet Settings changed: {before['internet_settings']} -> {after['internet_settings']}")
        if "error" in after["firewall"]:
            failures.append(f"firewall rules unreadable after the run: {after['firewall']['error']}")
        elif before["firewall"] != after["firewall"]:
            failures.append(f"firewall rules changed: {before['firewall']} -> {after['firewall']}")
        try:
            readback = driver.sandbox_readback(sandbox)
            result["sandbox_readback"] = readback
            failures.extend(f"sandbox: {p}" for p in driver.readback_problems(readback))
        except Exception as exc:
            failures.append(f"sandbox readback failed: {exc!r}"[:500])
        leaked = sorted(str(p) for base in (sandbox["appdata"], sandbox["localappdata"]) for p in Path(base).iterdir())
        if leaked:
            failures.append(f"default profile directories were written inside the sandbox: {leaked}")
        launched = [name for name in result.get("descendants_seen", ()) if "mihomo" in name.lower()]
        if launched:
            failures.append(f"Mihomo was launched: {launched}")
        return result
    finally:
        result["finished_utc"] = utc()
        for _ in range(5):
            shutil.rmtree(temp, ignore_errors=True)
            if not temp.exists():
                break
            time.sleep(1)


def _scenario(protocol, driver, sidecar, report, result: dict, *, settle_s: float, hello_timeout_s: float) -> None:
    metrics, failures = result["metrics"], result["failures"]
    hello = sidecar.receive(hello_timeout_s)
    metrics["hello_after_s"] = round(time.monotonic() - sidecar.launched, 3)
    if hello.get("t") != "hello":
        failures.append(f"first frame is {hello.get('t')!r}, not hello")
        return
    sidecar.epoch = hello["epoch"]
    result["hello"] = {"caps": hello.get("caps"), "scope": hello.get("scope"), "app_version": hello.get("app_version"),
                       "lang": hello.get("lang"), "network_recovery": hello.get("network_recovery")}
    failures.extend(f"hello: {p}" for p in _hello_problems(protocol, driver, hello, sidecar.process.pid))
    sidecar.send("hello_ack", proto=1, accepted=True)
    topics = sorted((hello.get("caps") or {}).get("topics") or ())
    subscribe = sidecar.send("subscribe", id=1, topics=topics)
    subscribed = sidecar.until(lambda f: f.get("id") == 1 and f["t"] in {"subscribed", "error"}, timeout=30)
    failures.extend(f"subscribe: {e}" for e in protocol.validate_control_reply(subscribe, subscribed))
    # Initial snapshots: every snapshot topic (per key for keyed topics), as T-P2-33 expects in
    # a stopped custom scope; a slot without one keeps its page in the connecting / stale state.
    expected = set()
    for topic in set(protocol.V1_SNAPSHOT_TOPICS) - set(driver.STOPPED_UNAVAILABLE) - {"shutdown"}:
        keys = ("vanguard", "sentinel", "bastion", "gateway")
        expected |= {(topic, k) for k in keys} if topic in protocol.KEYED_TOPICS else {(topic, None)}
    seen: set = set()
    started = time.monotonic()
    deadline = started + settle_s
    while not expected <= seen and time.monotonic() < deadline:
        try:
            frame = sidecar.receive(max(.01, deadline - time.monotonic()))
        except AssertionError:
            break
        for item in driver.events(frame):
            if item.get("kind") == "snapshot" and not (item.get("page") or {}).get("index"):
                seen.add((item.get("topic"), item.get("key")))
    metrics["initial_snapshots_s"] = round(time.monotonic() - started, 3)
    missing = sorted(f"{t}#{k}" if k else t for t, k in expected - seen)
    result["initial_snapshots_missing"] = missing
    if missing:
        failures.append(f"no initial snapshot within {settle_s:.0f} s: {missing}")
    # Read-only requests, page identity.
    ident = 100
    for name in sorted(set(protocol.V1_QUERIES) | set(EXTRA_READS)):
        ident += 1
        args = driver.request_args(name)
        try:
            request, reply = sidecar.request(ident, name, args, origin="page")
        except AssertionError as exc:
            failures.append(f"{name}: no reply ({exc})")
            continue
        record = {"cmd": name, "args": args, "reply": reply}
        for error in protocol.validate_response(request, reply):
            failures.append(f"{name}: response {error}")
        error = driver._error_of(reply)
        if error is not None:
            record["code"] = error.get("code")
            if error.get("code") in driver.FORBIDDEN_CODES:
                failures.append(f"{name}: answered {error.get('code')!r} (a crashed handler or a lost runtime)")
        result["queries"].append(record)
    # Quit with the shell identity; bye is the last frame. What arrives from here on is the
    # shutdown (the page is closing); project_frames.mjs projects only the frames before it, so a
    # timing-dependent edge of the quit cannot make the guard flaky. Every frame is still checked
    # against bridge_protocol by the driver.
    result["quit_frame_index"] = len(sidecar.frames)
    ident += 1
    record = driver.exercise(sidecar, report, "app.quit", ident)
    result["quit"] = record
    bye = driver.receive_bye(sidecar, report, "ui-guard")
    result["bye"] = {"exit_code": bye.get("exit_code"), "network_recovery": bye.get("network_recovery")}
    code = sidecar.process.wait(timeout=60)
    metrics["exit_code"] = code
    if code != 0 or bye.get("exit_code") != 0:
        failures.append(f"sidecar exit {code}, bye.exit_code {bye.get('exit_code')}")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="sidecar_probe.py", description=__doc__.splitlines()[0])
    form = parser.add_mutually_exclusive_group(required=True)
    form.add_argument("--exe", type=Path, help="the frozen tesseract-runtime.exe of the dist under test")
    form.add_argument("--source", action="store_true", help="runtime_main.pyw of --repo (no PyInstaller build)")
    form.add_argument("--enum-paths", action="store_true",
                      help="only print the enum fields of --repo's bridge_protocol as JSON (no sidecar is started)")
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--settle", type=float, default=20.0, help="seconds to wait for the initial snapshots")
    parser.add_argument("--hello-timeout", type=float, default=None)
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    if args.enum_paths:
        # only bridge_protocol (review 1 SHOULD 1): a half-edited test file of another session
        # must not make item 1 blame the backend's protocol
        protocol = _load_protocol(repo)
        print(json.dumps({**enum_paths(protocol), "fields": schema_fields(protocol)}, ensure_ascii=False, sort_keys=True))
        return 0
    if args.out is None:
        parser.error("--out is required")
    hello_timeout = args.hello_timeout or (30.0 if args.exe else 120.0)
    result = run_probe(repo=repo, exe=args.exe.resolve() if args.exe else None, out=args.out.resolve(),
                       settle_s=args.settle, hello_timeout_s=hello_timeout)
    (args.out / "probe.json").write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str) + "\n",
                                         encoding="utf-8")
    print(json.dumps({"started": result["started"], "failures": result["failures"][:20],
                      "metrics": result["metrics"]}, ensure_ascii=False))
    return 1 if result["failures"] or not result["started"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
