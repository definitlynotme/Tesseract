"""Rebuild the post-dial fixtures from the fallback-harness run data (not run by pytest).

    python tests/fixtures/post_dial/build_fixtures.py [<runs_dir> [<out_dir>]]

Default runs dir: %TEMP%/claude/fallback-harness/runs; default out dir: this
directory.  See README.md for what
each fixture contains and how it was derived.  Only public hostnames of the
harness test targets (and RFC 2606 ``.test`` names) are kept; every other
destination host is replaced by a stable alias before anything is written.
"""

from __future__ import annotations

import glob
import gzip
import hashlib
import hmac
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
OUT = HERE
DEFAULT_RUNS = Path(os.environ.get("TEMP", "")) / "claude" / "fallback-harness" / "runs"

# Public test targets of the harness runs (plus the contract host).
PUBLIC_TARGETS = {
    "www.google.com",
    "www.youtube.com",
    "github.com",
    "api.github.com",
    "www.wikipedia.org",
    "en.wikipedia.org",
    "cp.cloudflare.com",
    "www.gstatic.com",
    "speed.cloudflare.com",
    "go.dev",
    "www.rust-lang.org",
    "www.python.org",
    "docs.python.org",
    "developer.mozilla.org",
    "www.mozilla.org",
    "www.kernel.org",
    "www.debian.org",
    "news.ycombinator.com",
    "stackoverflow.com",
}

X1_RUNS = [
    ("R1", "info-google-20260926-072140"),
    ("R2", "info-google-d400-20260926-072747"),
    ("R3", "debug-google-20260926-073408"),
    ("R4", "info-cpcf-d400-20260926-074043"),
    ("R5", "local-slow40-d80-20260926-074904"),
]
# (fixture tag, run directory, rep-directory glob)
VPROTO_RUNS = [
    ("P1", "P1-20260926-113522", "P1-r*"),
    ("P3", "P3-20260926-114633", "P3-r*"),
    ("P5", "P5-20260926-124303", "P5-r*"),
    ("P4", "P4-20260926-115230", "freeze-vid2-*-r*"),
    ("P6", "P6-20260926-120007", "P6stall-r*"),
    ("P6b", "P6-b-20260926-120711", "P6*-r*"),
    ("P7", "P7-20260926-121556", "P7*-r*"),
    ("IDLE", "IDLE-20260926-122913", "IDLE-r*"),
]
# The v3 classifier has these endings; the prototype's STALLED rule was
# replaced by the WAITING shadow candidate (prototype deviation 4).
PROTO_OUTCOME_KINDS = ("FAILED_FAST", "AGED")
TCP_LINE = re.compile(r'msg="\[TCP\] ')
DST_RE = re.compile(r"--> (\[[^\]]+\]|[^\s:]+):(\d+) ")


def jl(path: Path) -> list[dict[str, Any]]:
    rows = []
    if not path.exists():
        return rows
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return rows


def write_gz(name: str, payload: Any) -> int:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path = OUT / name
    with gzip.GzipFile(filename="", mode="wb", fileobj=open(path, "wb"), mtime=0) as handle:
        handle.write(data)
    size = path.stat().st_size
    assert size <= 150_000, (name, size)
    return size


class Aliaser:
    def __init__(self) -> None:
        self.map: dict[str, str] = {}

    def host(self, host: str) -> str:
        bare = host.strip("[]").lower()
        if bare in PUBLIC_TARGETS or bare.endswith(".test") or bare.endswith(".invalid"):
            return host
        alias = self.map.get(bare)
        if alias is None:
            alias = f"host-{len(self.map) + 1:03d}.invalid"
            self.map[bare] = alias
        return alias

    def line(self, raw: str) -> str:
        match = DST_RE.search(raw)
        if not match:
            return raw
        host = match.group(1)
        new = self.host(host)
        if new == host:
            return raw
        return raw[: match.start(1)] + new + raw[match.end(1) :]


def time_of(raw: str) -> float:
    sys.path.insert(0, str(HERE.parents[2]))
    from runtime_post_dial import parse_rfc3339

    match = re.search(r'time="([^"]+)"', raw)
    value = parse_rfc3339(match.group(1)) if match else None
    assert value is not None, raw
    return value


# ---------------------------------------------------------------------------


def build_conformance(runs: Path) -> None:
    lines: list[str] = []
    for path in sorted(glob.glob(str(runs / "conformance" / "*" / "**" / "mihomo_stdout.jsonl"), recursive=True)):
        if "invalid-" in path:
            continue
        for row in jl(Path(path)):
            raw = row.get("line") or ""
            if "level=info" in raw and 'msg="[TCP] ' in raw:
                lines.append(raw)
    size = write_gz("conformance_lines.json.gz", {"source": "conformance/*/logs/mihomo_stdout.jsonl", "lines": lines})
    print("conformance", len(lines), size)


def build_x1(runs: Path) -> None:
    for tag, name in X1_RUNS:
        run = runs / "X1-zombie-signals" / name
        analysis = json.loads((run / "x1_analysis.json").read_text(encoding="utf-8"))
        expected = next(
            grid
            for grid in analysis["classifier_grid"]
            if grid["rec"] == "poll_100" and grid["X"] == 0 and grid["T"] == 1.0
        )
        stdout = [row for row in jl(run / "logs" / "mihomo_stdout.jsonl") if TCP_LINE.search(row.get("line") or "")]
        lines = [[round(row["ts"], 6), row["line"]] for row in stdout]
        line_index: dict[tuple[int, float], int] = {}
        for index, (_, raw) in enumerate(lines):
            match = re.search(r"level=info msg=\"\[TCP\] [0-9.]+:(\d+)", raw)
            if match is None:
                continue  # dial-error warnings stay in the fixture; the parser must reject them
            line_index[(int(match.group(1)), round(time_of(raw), 6))] = index
        base = min(ts for ts, _ in lines)
        conns: dict[str, int] = {}
        conn_rows: list[list[Any]] = []
        polls: list[list[Any]] = []
        for row in jl(run / "logs" / "poll_100.jsonl"):
            if "c" not in row:
                continue
            entries = []
            for cid, sport, host, dport, chains, up, down, start in (item[:8] for item in row["c"]):
                index = conns.get(cid)
                if index is None:
                    index = conns[cid] = len(conn_rows)
                    conn_rows.append([int(sport or 0), host, int(dport or 0), (chains or "").split("/")[0], start])
                entries.append([index, int(up or 0), int(down or 0)])
            polls.append([round((row["t0"] - base) * 10000), entries])
        rows = []
        for row in jl(run / "x1_rows.jsonl"):
            port = row.get("client_port")
            t_log = row.get("t_log")
            index = -1
            if port and t_log:
                index = line_index.get((int(port), round(float(t_log), 6)), -1)
                if index < 0:
                    # t_log came from the same line; tolerate float rounding.
                    for (p, t), i in line_index.items():
                        if p == int(port) and abs(t - float(t_log)) < 1e-4:
                            index = i
                            break
            rows.append([index, row["cat"]])
        payload = {
            "run": f"X1-zombie-signals/{name}",
            "tag": tag,
            "t_switch": analysis["meta"]["t_switch"],
            "base": base,
            "expected": {key: expected[key] for key in ("tp", "fp", "fn", "precision", "recall")},
            "lines": lines,
            "conns": conn_rows,
            "polls": polls,
            "rows": rows,
        }
        size = write_gz(f"x1_{tag}.json.gz", payload)
        print("x1", tag, len(lines), len(polls), size)


def _node_conns(run: Path) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for path in sorted((run / "harness" / "logs" / "nodes").glob("*.jsonl")):
        node = path.stem
        opens: dict[int, dict[str, Any]] = {}
        for row in jl(path):
            if row.get("ev") == "open":
                opens[row["conn"]] = {"open_ts": row["ts"], "target": row["target"], "close": None}
            elif row.get("ev") == "close" and row.get("conn") in opens:
                opens[row["conn"]]["close"] = row
        out[node] = sorted(opens.values(), key=lambda item: item["open_ts"])
    return out


def _schedule(conn: dict[str, Any] | None) -> list[Any] | None:
    """[t_close, t_first_up, t_eof_up, bytes_up, t_first_down, t_eof_down, bytes_down]."""

    if conn is None:
        return None
    close = conn["close"]
    if close is None:
        return [None, None, None, 0, None, None, 0]
    t_accept = close["t_accept"]

    def at(ms: Any) -> float | None:
        return None if ms is None else round(t_accept + float(ms) / 1000.0, 6)

    t_close = round(close["ts"] + 0.001, 6)
    # Mihomo counts what it wrote to the node, including bytes the fake node
    # read and dropped (stall / zombie modes).
    bytes_up = int(close.get("bytes_up") or 0) + int(close.get("dropped_up") or 0)
    t_first_up = at(close.get("first_up_ms"))
    t_eof_up = at(close.get("eof_up_ms"))
    first_bytes = int(close.get("first_bytes_len") or 0)
    if bytes_up == 0 and first_bytes > 0:
        # SNI-inspecting modes (zombie_*, zombie_deny) read the ClientHello
        # without counting it; Mihomo wrote it in one go at first_up.
        bytes_up = first_bytes
        t_eof_up = t_first_up
    return [
        t_close,
        t_first_up,
        t_eof_up,
        bytes_up,
        at(close.get("first_down_ms")),
        at(close.get("eof_down_ms")),
        int(close.get("bytes_down") or 0),
    ]


def _proto_token(key: bytes, host: str, port: int) -> str:
    return hmac.new(key, f"{host.lower()}:{port}".encode(), hashlib.sha256).hexdigest()[:12]


def build_vproto(runs: Path) -> None:
    for tag, name, rep_glob in VPROTO_RUNS:
        run = runs / "V-proto" / name
        stdout = [row for row in jl(run / "harness" / "logs" / "mihomo_stdout.jsonl") if TCP_LINE.search(row.get("line") or "")]
        nodes = _node_conns(run)
        used: set[int] = set()
        aliaser = Aliaser()
        meta = {row["label"]: row for row in jl(run / "runs.jsonl")}
        reps = []
        for rep_dir in sorted(run.glob(rep_glob)):
            events = jl(rep_dir / "proto" / "judge.jsonl")
            attach = next(event for event in events if event["ev"] == "attach")
            exit_ev = next((event for event in events if event["ev"] == "exit"), events[-1])
            key = bytes.fromhex((rep_dir / "proto" / "eval_hmac_key.hex").read_text().strip())
            t0, t1 = attach["ts"], exit_ev["ts"]
            rep_lines = []
            matched_oids: set[int] = set()
            proto_opens = [event for event in events if event["ev"] == "open"]
            outcomes = {
                event["oid"]: event
                for event in events
                if event["ev"] == "outcome" and not event.get("legacy") and event.get("kind") in PROTO_OUTCOME_KINDS
            }
            for row in stdout:
                if not (t0 <= row["ts"] <= t1):
                    continue
                raw = row["line"]
                t_line = time_of(raw)
                match = re.search(r"\[TCP\] [0-9.]+:(\d+) --> (\S+):(\d+) (?:match \S+ )?using \S+?\[(\w+)\]", raw)
                sched = None
                proto_oid = None
                if match:
                    host, port, leaf = match.group(2), int(match.group(3)), match.group(4)
                    best = None
                    for conn in nodes.get(leaf, ()):
                        if id(conn) in used or conn["target"] != f"{host}:{port}":
                            continue
                        dt = t_line - conn["open_ts"]
                        if -0.05 <= dt <= 0.5 and (best is None or abs(dt) < abs(t_line - best["open_ts"])):
                            best = conn
                    if best is not None:
                        used.add(id(best))
                        sched = _schedule(best)
                    token = _proto_token(key, host, port)
                    cands = [
                        event
                        for event in proto_opens
                        if event.get("leaf") == leaf and event.get("tok") == token and abs(event["ts"] - row["ts"]) < 0.25
                    ]
                    if cands:
                        chosen = min(cands, key=lambda event: abs(event["ts"] - row["ts"]))
                        proto_opens.remove(chosen)
                        proto_oid = chosen["oid"]
                        matched_oids.add(proto_oid)
                outcome = outcomes.get(proto_oid, {}).get("kind") if proto_oid is not None else None
                rep_lines.append([round(row["ts"], 6), aliaser.line(raw), sched, outcome])
            triggers = [
                [event["ts"], event["kind"], event["leaf"]] for event in events if event["ev"] == "trigger"
            ]
            probes = [
                [event["name"], event["leaf"], event["cls"], event["ms"], event["ts"]]
                for event in events
                if event["ev"] == "probe_result"
            ]
            starts = [[event["name"], event["leaf"], event["ts"]] for event in events if event["ev"] == "probe_start"]
            verdicts = [
                [event["ts"], event["verdict"], (event.get("info") or {}).get("successor")]
                for event in events
                if event["ev"] == "verdict"
            ]
            states = [[event["succ"], event["alive"], event["now"]] for event in events if event["ev"] == "judge_state"]
            label = rep_dir.name
            info = meta.get(label, {})
            reps.append(
                {
                    "label": label,
                    "t_attach": t0,
                    "t_exit": t1,
                    "t_fault": info.get("t_fault"),
                    "members": attach.get("members"),
                    "lines": rep_lines,
                    "proto": {
                        "outcomes_unmatched": sorted(set(outcomes) - matched_oids),
                        "triggers": triggers,
                        "probe_starts": starts,
                        "probe_results": probes,
                        "judge_state": states,
                        "verdicts": verdicts,
                        "n_outcomes": {kind: sum(1 for e in outcomes.values() if e["kind"] == kind) for kind in ("FAILED_FAST", "AGED")},
                    },
                }
            )
        size = write_gz(f"vproto_{tag}.json.gz", {"run": f"V-proto/{name}", "scenario": tag, "reps": reps})
        print("vproto", tag, len(reps), sum(len(rep["lines"]) for rep in reps), size, "aliases", len(aliaser.map))


def main() -> None:
    global OUT
    runs = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_RUNS
    if len(sys.argv) > 2:
        OUT = Path(sys.argv[2])
        OUT.mkdir(parents=True, exist_ok=True)
    build_conformance(runs)
    build_x1(runs)
    build_vproto(runs)


if __name__ == "__main__":
    main()
