"""Validate the UI's FakeTransport scenarios against the v1 contract (T-P4-02a; P4
architecture section 2.7).

    python -B ui/tools/validate_ui_fixtures.py [path ...]
    python -B ui/tools/validate_ui_fixtures.py --export-patch-vectors

Paths are scenario files or directories (default: ui/test/fixtures/scenarios). A file is a
scenario object ({"steps": [...], "replay"?: [...]}), a bare list of steps, or
{"frames": [...]} (T-P4-02b writes its generated frames this way).

Checks:
  * sidecar frames (hello, evt, batch, ack, res, error, bye, fatal, ...) go through
    bridge_protocol.validate_frame, the same oracle as bridge_schema/fixtures;
  * shell-only frames (rt:reset, rt:log-gap, rt:replayed, host:*) are checked against the
    shapes in shell/README (P3 page contract sections) and PROTOCOL section 13;
  * a page scenario must not contain shell-only topics or commands (alerts.desktop,
    ui.language, shutdown, update.prepareExit; app.quit and the other shell commands):
    the shell strips or keeps them, a real page never receives them (criticism B2);
  * on_invoke names a page command, query, cancel, or a known shell invoke;
  * runtime invariants across tiers.summary, route.published and gateway.current (T-P4-H2,
    8th-wave ruling item 3; T-P4-05a review 1): replayed in delivery order (a deliver step, a
    batch or a frame of a {"frames"} file is one unit; on_invoke replies after every deliver step,
    as FakeTransport delivers them; a new epoch or rt:reset{runtime_gone} starts over), after each
    unit, for the values of that moment:
      1. gateway.current.effective set -> tiers.summary.carrier is gateway;
      2. a carrier and a valid route.published -> carrier == route.published.managed_tier;
      3. a carrier -> tiers.summary.order[0] == carrier;
      4. a carrier -> fallback is true exactly when the carrier is sentinel or bastion;
      5. carrier gateway with effective set -> effective == route.published.leaf
    (desktop_runtime tiers_summary_status / bridge_gateway_status: a chosen Gateway is the exit
    FINAL-EXIT -> EXIT-SELECT -> <node>, managed_tier gateway; the tier under it is not reported).
    Convention (T-P4-H2 fix round 1): the sidecar publishes tiers.summary, route.published and
    gateway.current separately, so for an instant during a switch the invariants may not hold
    between two of them. A fixture that wants such a switch puts the frames that change together
    into ONE batch (one unit, checked after the whole batch), not into consecutive deliver steps;
    the invariants are not relaxed for transient states.
    The replay segment (the shell's cached values on a reconnect, rt:reset ... rt:replayed) is
    checked too, as one unit: its frames are applied in order (continuing from the steps; a new
    epoch or rt:reset{runtime_gone} starts over) and the invariants are checked once at its
    rt:replayed (or at its end); a frame after rt:replayed is its own unit again.
Fields added later as optional (T-P4-C1/C2) may be absent: bridge_protocol already treats
them as optional (criticism B10).

hello caps: since T-P2-31b bridge_protocol.validate_hello admits the v1 set (every command
and query the sidecar can register, never host.*; every v1 topic), so scenario hellos go
through the same oracle unchanged (the T-P4-02a temporary relaxation is gone, ruling 4th
wave T-P4-02a item 2).

--export-patch-vectors prints [{name, baseline, patch, merged, needs_snapshot}] computed by
bridge_protocol.apply_patch, for the JS merge test (ui/test/unit/rt_topics.test.mjs).
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

UI = Path(__file__).resolve().parent.parent
REPO = UI.parent
DEFAULT_DIR = UI / "test" / "fixtures" / "scenarios"
SCHEMA = REPO / "bridge_schema" / "fixtures"

sys.path.insert(0, str(REPO))
import bridge_protocol as bp  # noqa: E402

LANGS = {"zh_CN", "en_US", "ja_JP", "ko_KR", "fr_FR", "de_DE", "es_ES", "pt_BR", "ru_RU"}
PAGE_FORBIDDEN_TOPICS = set(bp.SHELL_TOPICS) | {"ui.language", "alerts.desktop"}
HOST_INVOKES = {"rt_resync", "host_ready", "host_notify_ready", "host_notify_rendered",
                "host_confirm_quit", "host_set_page", "host_force_restart", "host_window"}
SIDECAR_TYPES = {"hello", "evt", "batch", "ack", "res", "error", "bye", "fatal", "subscribed", "pong"}
PLACEHOLDER_REQ_ID = "p0.1"
PLACEHOLDER_OWNER = "page.0"


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _is_str(v) -> bool:
    return isinstance(v, str) and bool(v)


def _keys(frame: dict, required: set, optional: set = frozenset()) -> list[str]:
    errors = [f"missing:{k}" for k in sorted(required - set(frame))]
    errors += [f"unknown:{k}" for k in sorted(set(frame) - required - optional)]
    return errors


def validate_shell_frame(frame: dict) -> list[str]:
    """Shell -> page frames (shell/README P3 sections, transport.rs, PROTOCOL section 13)."""
    t = frame.get("t")
    errors: list[str] = []
    if t == "rt:reset":
        errors += _keys(frame, {"t", "epoch", "reason"})
        if frame.get("reason") not in ("runtime_gone", "reconnect"):
            errors.append("enum:reason")
        if not _is_str(frame.get("epoch")):
            errors.append("type:epoch")
    elif t == "rt:replayed":
        errors += _keys(frame, {"t", "epoch"})
    elif t == "rt:stale":
        # PROTOCOL section 13 rt:stale (T-P3-H2): key is a tier of a keyed list topic, else null
        errors += _keys(frame, {"t", "epoch", "topic", "key", "reason"})
        if not _is_str(frame.get("topic")):
            errors.append("type:topic")
        if frame.get("reason") not in ("resync", "exhausted"):
            errors.append("enum:reason")
        key = frame.get("key")
        if key is not None and key not in ("vanguard", "sentinel", "bastion", "gateway"):
            errors.append("enum:key")
        if frame.get("topic") in ("nodes.list", "subscriptions.list") and key is None:
            errors.append("missing:key (a keyed list topic names its tier)")
        if frame.get("topic") not in ("nodes.list", "subscriptions.list") and key is not None:
            errors.append("unknown:key (only nodes.list / subscriptions.list are keyed)")
    elif t == "rt:log-gap":
        errors += _keys(frame, {"t", "epoch", "data"})
        data = frame.get("data")
        if not isinstance(data, dict):
            errors.append("type:data")
        else:
            errors += [f"data.{e}" for e in _keys(data, {"from_seq", "to_seq", "dropped"},
                                                   {"record_from_seq", "record_to_seq", "record_dropped"})]
            for k in ("from_seq", "dropped"):
                if not _is_int(data.get(k)):
                    errors.append(f"type:data.{k}")
            for k in ("to_seq", "record_from_seq", "record_to_seq"):
                if k in data and data[k] is not None and not _is_int(data[k]):
                    errors.append(f"type:data.{k}")
            if "record_dropped" in data and not _is_int(data["record_dropped"]):
                errors.append("type:data.record_dropped")
    elif t == "host:caps":
        errors += _keys(frame, {"t", "invokes", "events"}, {"epoch"})
        for k in ("invokes", "events"):
            if not (isinstance(frame.get(k), list) and all(_is_str(x) for x in frame[k])):
                errors.append(f"type:{k}")
    elif t == "host:banner":
        kind = frame.get("kind")
        if kind == "sidecar_crashed":
            errors += _keys(frame, {"t", "kind", "active", "state", "exit_code", "restarts", "limit", "epoch"})
            if frame.get("state") not in ("restarting", "restarted", "unrecoverable"):
                errors.append("enum:state")
            if frame.get("exit_code") is not None and not _is_int(frame.get("exit_code")):
                errors.append("type:exit_code")
            for k in ("restarts", "limit"):
                if not _is_int(frame.get(k)):
                    errors.append(f"type:{k}")
        elif kind == "sidecar_unresponsive":
            errors += _keys(frame, {"t", "kind", "active", "actions", "epoch"})
            if not (isinstance(frame.get("actions"), list) and all(a == "force_restart" for a in frame["actions"])):
                errors.append("enum:actions")
        else:
            errors.append("enum:kind")
        if not isinstance(frame.get("active"), bool):
            errors.append("type:active")
    elif t == "host:confirmQuit":
        errors += _keys(frame, {"t", "token", "epoch"})
        if not _is_int(frame.get("token")):
            errors.append("type:token")
    elif t == "host:notify":
        errors += _keys(frame, {"t", "delivery_id", "severity", "title", "detail", "target", "epoch"})
        if frame.get("severity") not in ("info", "warn", "error", "critical"):
            errors.append("enum:severity")
        if frame.get("target") not in ("overview", "alerts", "update"):
            errors.append("enum:target")
        for k in ("delivery_id",):
            if not _is_str(frame.get(k)):
                errors.append(f"type:{k}")
        for k in ("title", "detail"):
            if not isinstance(frame.get(k), str):
                errors.append(f"type:{k}")
    elif t == "host:navigate":
        errors += _keys(frame, {"t", "page", "epoch"})
        if frame.get("page") not in ("overview", "alerts", "update"):
            errors.append("enum:page")
    elif t == "host:language":
        errors += _keys(frame, {"t", "language", "epoch"})
        if frame.get("language") not in LANGS:
            errors.append("enum:language")
    elif t == "host:visibility":
        errors += _keys(frame, {"t", "visible", "epoch"})
        if not isinstance(frame.get("visible"), bool):
            errors.append("type:visible")
    elif t == "host:window":
        errors += _keys(frame, {"t", "maximized", "epoch"})
        if not isinstance(frame.get("maximized"), bool):
            errors.append("type:maximized")
    else:
        errors.append("enum:t")
    if "epoch" in frame and not _is_str(frame.get("epoch")):
        errors.append("type:epoch")
    return errors


def _substitute(value, variables):
    if isinstance(value, str):
        return variables.get(value, value)
    if isinstance(value, list):
        return [_substitute(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, variables) for k, v in value.items()}
    return value


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.frames = 0

    def add(self, where: str, problems: list[str]) -> None:
        self.errors.extend(f"{where}: {p}" for p in problems)


INVARIANT_TOPICS = ("tiers.summary", "route.published", "gateway.current")


class RuntimeState:
    """The latest value of the invariant topics in delivery order (T-P4-H2)."""

    def __init__(self) -> None:
        self.epoch = None
        self.values: dict = {}

    def _evt(self, item) -> bool:
        if not isinstance(item, dict) or item.get("t") != "evt" or item.get("topic") not in INVARIANT_TOPICS:
            return False
        if item.get("kind") in ("snapshot", "edge") and isinstance(item.get("data"), dict):
            self.values[item["topic"]] = item["data"]
            return True
        return False

    def apply(self, frame) -> bool:
        """-> whether an invariant topic changed."""
        if not isinstance(frame, dict):
            return False
        t = frame.get("t")
        if t in ("rt:reset", "hello"):
            epoch = frame.get("epoch")
            if (t == "rt:reset" and frame.get("reason") == "runtime_gone") or (self.epoch is not None and epoch != self.epoch):
                self.values = {}
            self.epoch = epoch
            return False
        if t == "batch":
            changed = False
            for item in frame.get("items") or []:
                changed = self._evt(item) or changed
            return changed
        return self._evt(frame)

    def problems(self) -> list[str]:
        ts = self.values.get("tiers.summary")
        rp = self.values.get("route.published")
        gc = self.values.get("gateway.current")
        carrier = ts.get("carrier") if isinstance(ts, dict) else None
        effective = gc.get("effective") if isinstance(gc, dict) else None
        out = []
        if ts is not None and effective is not None and carrier != "gateway":
            out.append(f"invariant:gateway_effective_needs_carrier_gateway (effective={effective!r}, carrier={carrier!r})")
        if carrier is not None:
            if isinstance(rp, dict) and rp.get("valid") is True and rp.get("managed_tier") != carrier:
                out.append(f"invariant:carrier_is_managed_tier (carrier={carrier!r}, managed_tier={rp.get('managed_tier')!r})")
            order = ts.get("order")
            if isinstance(order, list) and order and order[0] != carrier:
                out.append(f"invariant:order_starts_with_carrier (order[0]={order[0]!r}, carrier={carrier!r})")
            if ts.get("fallback") is not (carrier in ("sentinel", "bastion")):
                out.append(f"invariant:fallback_iff_backup_carrier (fallback={ts.get('fallback')!r}, carrier={carrier!r})")
            if carrier == "gateway" and effective is not None and isinstance(rp, dict) and rp.get("valid") is True and rp.get("leaf") != effective:
                out.append(f"invariant:gateway_effective_is_leaf (effective={effective!r}, leaf={rp.get('leaf')!r})")
        return out


def check_invariants(state: RuntimeState, frame, where: str, report: Report) -> None:
    if state.apply(frame):
        report.add(where, state.problems())


def check_frame(frame, where: str, report: Report) -> None:
    report.frames += 1
    if not isinstance(frame, dict) or not isinstance(frame.get("t"), str):
        report.add(where, ["type:frame"])
        return
    t = frame["t"]
    if t.startswith("rt:") or t.startswith("host:"):
        report.add(where, validate_shell_frame(frame))
        return
    if t not in SIDECAR_TYPES:
        report.add(where, [f"not_page_frame:{t}"])
        return
    report.add(where, bp.validate_frame(frame))
    items = frame.get("items") if t == "batch" and isinstance(frame.get("items"), list) else [frame] if t == "evt" else []
    for index, item in enumerate(items):
        if isinstance(item, dict) and item.get("topic") in PAGE_FORBIDDEN_TOPICS:
            report.add(where + (f".items[{index}]" if t == "batch" else ""),
                       [f"shell_only_topic:{item.get('topic')} (the shell strips or keeps it; a page never receives it)"])


def check_scenario(doc, where: str, report: Report) -> None:
    if isinstance(doc, list):
        doc = {"steps": doc}
    if not isinstance(doc, dict):
        report.add(where, ["type:scenario"])
        return
    state = RuntimeState()
    if "frames" in doc:
        for i, frame in enumerate(doc.get("frames") or []):
            check_frame(frame, f"{where} frames[{i}]", report)
            check_invariants(state, frame, f"{where} frames[{i}]", report)
        return
    if "lang" in doc and doc["lang"] not in LANGS:
        report.add(where, ["enum:lang"])
    steps = doc.get("steps")
    if not isinstance(steps, list):
        report.add(where, ["missing:steps"])
        return
    for i, step in enumerate(steps):
        at = f"{where} steps[{i}]"
        if not isinstance(step, dict):
            report.add(at, ["type:step"])
            continue
        if "deliver" in step:
            check_frame(step["deliver"], at, report)
            check_invariants(state, step["deliver"], at, report)
            if "order" in step and not _is_int(step["order"]):
                report.add(at, ["type:order"])
        elif "advance_ms" in step or "jump_ms" in step:
            value = step.get("advance_ms", step.get("jump_ms"))
            if not _is_int(value):
                report.add(at, ["type:advance_ms"])
        elif "on_invoke" in step:
            on = step["on_invoke"]
            if not isinstance(on, dict):
                report.add(at, ["type:on_invoke"])
                continue
            cmd = on.get("cmd")
            if "host" in on:
                if on["host"] not in HOST_INVOKES:
                    report.add(at, [f"enum:on_invoke.host:{on['host']}"])
            elif cmd in bp.SHELL_COMMANDS:
                report.add(at, [f"shell_only_command:{cmd} (pages cannot invoke it)"])
            elif cmd != "cancel" and cmd not in bp.PAGE_COMMANDS and cmd not in bp.V1_QUERIES:
                report.add(at, [f"enum:on_invoke.cmd:{cmd}"])
            variables = {"$req_id": PLACEHOLDER_REQ_ID, "$owner": PLACEHOLDER_OWNER, "$cmd": cmd}
            for j, reply in enumerate(step.get("reply") or []):
                frame = _substitute(copy.deepcopy(reply), variables)
                if isinstance(frame, dict) and frame.get("t") in ("ack", "res", "error"):
                    frame.setdefault("req_id", PLACEHOLDER_REQ_ID)
                    frame.setdefault("owner", PLACEHOLDER_OWNER)
                check_frame(frame, f"{at} reply[{j}]", report)
            if "reject" in step and not _is_str(step["reject"]):
                report.add(at, ["type:reject"])
        else:
            report.add(at, ["unknown:step"])
    # the replies arrive after every deliver step (FakeTransport delivers the steps on connect)
    for i, step in enumerate(steps):
        if isinstance(step, dict) and isinstance(step.get("on_invoke"), dict):
            for j, reply in enumerate(step.get("reply") or []):
                check_invariants(state, reply, f"{where} steps[{i}] reply[{j}]", report)
    # the replay: one unit up to rt:replayed (the shell replays its whole cache at once)
    in_replay, changed, last = True, False, None
    for i, frame in enumerate(doc.get("replay") or []):
        at = f"{where} replay[{i}]"
        check_frame(frame, at, report)
        if not in_replay:
            check_invariants(state, frame, at, report)
            continue
        changed = state.apply(frame) or changed
        last = at
        if isinstance(frame, dict) and frame.get("t") == "rt:replayed":
            in_replay = False
            if changed:
                report.add(at, state.problems())
            changed = False
    if in_replay and changed and last is not None:
        report.add(last, state.problems())


def iter_files(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for raw in paths or [str(DEFAULT_DIR)]:
        p = Path(raw)
        if p.is_dir():
            out.extend(sorted(p.glob("*.json")))
        elif p.exists():
            out.append(p)
        else:
            raise FileNotFoundError(raw)
    return out


def patch_vectors() -> list[dict]:
    def frame_pair(name: str, doc: dict) -> dict:
        base, patch = doc["baseline"], doc["frame"]
        return {"name": name,
                "baseline": {"rev": base["rev"], "items": base["data"]["items"]},
                "patch": {"base_rev": patch["base_rev"], "rev": patch["rev"], **patch["data"]}}

    cases: list[dict] = []
    legacy = json.loads((SCHEMA / "invalid" / "base_rev_mismatch.json").read_text(encoding="utf-8"))
    cases.append({"name": "p1_base_rev_mismatch", "baseline": legacy["baseline"], "patch": legacy["patch"]})
    for fname in ("patch_base_mismatch.json", "patch_key_mismatch.json"):
        doc = json.loads((SCHEMA / "v1" / "invalid" / fname).read_text(encoding="utf-8"))
        cases.append(frame_pair("v1_" + fname[:-5], doc))
    valid = json.loads((SCHEMA / "v1" / "valid" / "patch_nodes.list.json").read_text(encoding="utf-8"))
    p = {"base_rev": valid["base_rev"], "rev": valid["rev"], **valid["data"]}
    row = valid["data"]["changed"][0]
    cases.append({"name": "v1_patch_into_empty", "baseline": {"rev": valid["base_rev"], "items": []}, "patch": p})
    cases.append({"name": "v1_patch_replace", "baseline": {"rev": valid["base_rev"], "items": [dict(row, status="failed")]}, "patch": p})
    a, b, c, d = ({"id": x, "v": n} for n, x in enumerate("abcd"))
    cases += [
        {"name": "remove_replace_append", "baseline": {"rev": 3, "items": [a, b, c]},
         "patch": {"base_rev": 3, "rev": 4, "changed": [dict(b, v=9), d], "removed": ["a"]}},
        {"name": "remove_unknown_id", "baseline": {"rev": 1, "items": [a]},
         "patch": {"base_rev": 1, "rev": 2, "changed": [], "removed": ["zz"]}},
        {"name": "changed_duplicate", "baseline": {"rev": 1, "items": [a]},
         "patch": {"base_rev": 1, "rev": 2, "changed": [b, b], "removed": []}},
        {"name": "changed_removed_overlap", "baseline": {"rev": 1, "items": [a]},
         "patch": {"base_rev": 1, "rev": 2, "changed": [a], "removed": ["a"]}},
        {"name": "rev_not_increasing", "baseline": {"rev": 5, "items": [a]},
         "patch": {"base_rev": 5, "rev": 5, "changed": [], "removed": []}},
        {"name": "base_rev_ahead", "baseline": {"rev": 5, "items": [a]},
         "patch": {"base_rev": 6, "rev": 7, "changed": [], "removed": []}},
        {"name": "baseline_duplicate_ids", "baseline": {"rev": 1, "items": [a, a]},
         "patch": {"base_rev": 1, "rev": 2, "changed": [], "removed": []}},
        {"name": "empty_id", "baseline": {"rev": 1, "items": [a]},
         "patch": {"base_rev": 1, "rev": 2, "changed": [{"id": "", "v": 1}], "removed": []}},
        {"name": "bool_rev", "baseline": {"rev": True, "items": [a]},
         "patch": {"base_rev": 1, "rev": 2, "changed": [], "removed": []}},
        {"name": "removed_not_list", "baseline": {"rev": 1, "items": [a]},
         "patch": {"base_rev": 1, "rev": 2, "changed": [], "removed": "a"}},
    ]
    out = []
    for case in cases:
        merged, needs = bp.apply_patch(copy.deepcopy(case["baseline"]), copy.deepcopy(case["patch"]))
        out.append({**case, "merged": merged, "needs_snapshot": needs})
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*")
    parser.add_argument("--export-patch-vectors", action="store_true")
    args = parser.parse_args(argv)
    if args.export_patch_vectors:
        sys.stdout.write(json.dumps(patch_vectors(), ensure_ascii=True, indent=1) + "\n")
        return 0
    report = Report()
    try:
        files = iter_files(args.paths)
    except FileNotFoundError as exc:
        print(f"[validate_ui_fixtures] no such path: {exc}", file=sys.stderr)
        return 2
    for path in files:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            report.add(path.name, [f"unreadable:{exc}"])
            continue
        check_scenario(doc, path.name, report)
    for line in report.errors:
        print(f"[validate_ui_fixtures] {line}", file=sys.stderr)
    status = "FAILED" if report.errors else "OK"
    print(f"[validate_ui_fixtures] {status}: {len(files)} file(s), {report.frames} frame(s), "
          f"{len(report.errors)} problem(s)")
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
