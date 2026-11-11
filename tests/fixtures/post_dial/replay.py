"""Fake-clock replay helpers shared by the post-dial and node-judge tests.

Loaded by path (``tests/fixtures`` is not a package).  Pure: no sockets, no
threads, no sleeping.  Wall and perf clocks are the same simulated number
unless ``drive(perf_offset=...)`` runs perf at ``wall - perf_offset``.
"""

from __future__ import annotations

import gzip
import json
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from runtime_post_dial import (
    OriginRules,
    PostDialObserverCore,
    PostDialTokenizer,
    Trigger,
    ShadowTrigger,
    parse_open_line,
    parse_rfc3339,
)

HERE = Path(__file__).resolve().parent
KEY = bytes(range(32))
TOKENIZER = PostDialTokenizer(KEY)
_LINE_RE = re.compile(
    r"\[TCP\] (?P<sip>\[[^\]]+\]|[0-9.]+):(?P<sport>\d+)(?:\(.*?\))? --> (?P<dst>\S+) "
    r"(?:match .+? )?using (?P<using>.+)\"$"
)


def load_fixture(name: str) -> Any:
    with gzip.open(HERE / name, "rb") as handle:
        return json.loads(handle.read().decode("utf-8"))


def rfc3339(epoch: float, offset_hours: int = -4) -> str:
    """Mihomo-style local time with a 7-digit fraction (Conf-c)."""

    local = epoch + offset_hours * 3600
    whole = math.floor(local)
    fraction = int(round((local - whole) * 10_000_000))
    if fraction >= 10_000_000:
        whole += 1
        fraction -= 10_000_000
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(whole))
    sign = "-" if offset_hours < 0 else "+"
    return f"{stamp}.{fraction:07d}{sign}{abs(offset_hours):02d}:00"


def go_quote(text: str) -> str:
    out = []
    for char in text:
        if char == '"':
            out.append('\\"')
        elif char == "\\":
            out.append("\\\\")
        elif char == "\t":
            out.append("\\t")
        elif char == "\n":
            out.append("\\n")
        elif char == "‍":
            out.append("\\u200d")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def open_line(
    t: float,
    *,
    sport: int,
    host: str,
    port: int = 443,
    leaf: str = "n1",
    outer: str = "GENERAL",
    rule: str | None = "Match",
    source_ip: str = "127.0.0.1",
    process: str | None = None,
) -> str:
    src = f"[{source_ip}]:{sport}" if ":" in source_ip else f"{source_ip}:{sport}"
    if process:
        src += f"({process})"
    dst = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    using = f"{outer}[{leaf}]" if leaf is not None else outer
    match = f"match {rule} " if rule is not None else ""
    msg = f"[TCP] {src} --> {dst} {match}using {using}"
    return f'time="{rfc3339(t)}" level=info msg={go_quote(msg)}'


@dataclass
class FakeConn:
    conn_id: str
    t_open: float
    sport: int
    host: str
    port: int
    leaf: str
    t_close: float | None = None
    up: Callable[[float], int] = lambda t: 0
    down: Callable[[float], int] = lambda t: 0
    source_ip: str = "127.0.0.1"
    inbound: str = "DEFAULT-MIXED"
    chains_tail: tuple[str, ...] = ("TIER3-AUTO", "PROXY-FALLBACK", "FINAL-EXIT", "GENERAL")
    conn_type: str = "HTTP"
    special: str = ""
    start: str = ""

    def __post_init__(self) -> None:
        if not self.start:
            self.start = rfc3339(self.t_open)


def step(t0: float, value: int) -> Callable[[float], int]:
    return lambda t: value if t >= t0 else 0


def ramp(t0: float | None, t1: float | None, total: int) -> Callable[[float], int]:
    if t0 is None or total <= 0:
        return lambda t: 0
    if t1 is None or t1 <= t0:
        return lambda t: total if t >= t0 else 0

    def value(t: float) -> int:
        if t < t0:
            return 0
        return max(1, min(total, int(total * (t - t0) / (t1 - t0))))

    return value


class FakeMihomo:
    """``/connections`` over ground-truth connection lifetimes."""

    def __init__(self) -> None:
        self.conns: list[FakeConn] = []
        self.snapshots = 0

    def add(self, conn: FakeConn) -> FakeConn:
        self.conns.append(conn)
        return conn

    def close(self, conn_id: str, t: float) -> None:
        for conn in self.conns:
            if conn.conn_id == conn_id and (conn.t_close is None or conn.t_close > t):
                conn.t_close = t

    def snapshot(self, t: float) -> dict[str, Any]:
        self.snapshots += 1
        items = []
        for conn in self.conns:
            if t < conn.t_open + 0.0005:
                continue
            if conn.t_close is not None and t >= conn.t_close:
                continue
            metadata = {
                "network": "tcp",
                "type": conn.conn_type,
                "sourceIP": conn.source_ip,
                "sourcePort": str(conn.sport),
                "host": conn.host,
                "destinationPort": str(conn.port),
                "inboundName": conn.inbound,
                "specialProxy": conn.special,
            }
            items.append(
                {
                    "id": conn.conn_id,
                    "metadata": metadata,
                    "upload": conn.up(t),
                    "download": conn.down(t),
                    "start": conn.start,
                    "chains": [conn.leaf, *conn.chains_tail],
                    "rule": "Match",
                    "rulePayload": "",
                }
            )
        return {"downloadTotal": 0, "uploadTotal": 0, "connections": items}


@dataclass
class DriveResult:
    polls: list[float] = field(default_factory=list)
    triggers: list[Trigger] = field(default_factory=list)
    shadow: list[ShadowTrigger] = field(default_factory=list)
    records: list[dict[str, Any]] = field(default_factory=list)


def drive(
    core: PostDialObserverCore,
    lines: Iterable[tuple[float, str]],
    mihomo: FakeMihomo,
    *,
    t_start: float,
    t_end: float,
    rules: OriginRules | None = None,
    cost: float = 0.003,
    is_exonerated: Callable[[str, str], bool] | None = None,
    on_poll: Callable[[float, Any], None] | None = None,
    perf_offset: float = 0.0,
) -> DriveResult:
    """Run the §18.4 loop body against ``mihomo`` with the core's own schedule.

    Times in the result are wall times; the core sees perf = wall - perf_offset.
    """

    feed = sorted(lines, key=lambda item: item[0])
    rules = rules or OriginRules()
    result = DriveResult()
    index = 0
    t = t_start
    while t <= t_end:
        while index < len(feed) and feed[index][0] <= t:
            record = parse_open_line(feed[index][1], feed[index][0], tokenizer=core.tokenizer, origin_rules=rules)
            if record is not None:
                core.ingest_open(record, now_wall=feed[index][0], now_perf=feed[index][0] - perf_offset)
            index += 1
        due_perf = core.next_poll_time(t - perf_offset, t)
        due = None if due_perf is None else due_perf + perf_offset
        next_line = feed[index][0] if index < len(feed) else math.inf
        if due is None or due > t:
            target = min(math.inf if due is None else due, next_line)
            if target == math.inf or target > t_end:
                break
            t = target
            continue
        digest = core.ingest_snapshot(mihomo.snapshot(t), t, t - perf_offset, cost_s=cost)
        result.polls.append(t)
        evaluation = core.evaluate_triggers(t, is_exonerated=is_exonerated)
        result.triggers.extend(evaluation.triggers)
        result.shadow.extend(evaluation.shadow)
        if on_poll is not None:
            on_poll(t, evaluation)
        t += cost
        del digest
    result.records.extend(core.drain_records())
    return result


def line_identity(raw: str, tokenizer: PostDialTokenizer = TOKENIZER) -> tuple[float, str] | None:
    record = parse_open_line(raw, 0.0, tokenizer=tokenizer, origin_rules=OriginRules())
    if record is None:
        return None
    return (round(record.t_open, 3), record.token[:12])


def outcomes_by_line(
    lines: list[str],
    records: list[dict[str, Any]],
    tokenizer: PostDialTokenizer = TOKENIZER,
) -> dict[int, str]:
    """Map fixture line index -> evidence class via the core's open/outcome records.

    UNKNOWN outcomes appear as ``UNKNOWN/<would-be class>``.
    """

    opens: dict[int, tuple[float, str]] = {}
    for record in records:
        if record["ev"] == "open":
            opens[record["oid"]] = (record["t"], record["tok"])
    classes: dict[tuple[float, str], list[str]] = {}
    for record in records:
        if record["ev"] == "outcome" and record["oid"] in opens and record["class"] != "WAITING":
            value = record["class"]
            if record.get("would"):
                value = f"{value}/{record['would']}"
            classes.setdefault(opens[record["oid"]], []).append(value)
    out: dict[int, str] = {}
    for index, raw in enumerate(lines):
        identity = line_identity(raw, tokenizer)
        if identity is not None and classes.get(identity):
            out[index] = classes[identity].pop(0)
    return out


def vproto_mihomo(rep: dict[str, Any], *, t_end: float | None = None) -> tuple[FakeMihomo, list[tuple[float, str]]]:
    """Mihomo state for one V-proto rep, reconstructed from the fake-node records."""

    mihomo = FakeMihomo()
    feed: list[tuple[float, str]] = []
    for index, (ts, raw, sched, _outcome) in enumerate(rep["lines"]):
        feed.append((ts, raw))
        match = _LINE_RE.search(raw)
        if match is None or sched is None:
            continue
        t_open = parse_rfc3339(re.search(r'time="([^"]+)"', raw).group(1))
        using = match.group("using")
        leaf = using[using.index("[") + 1 : using.rindex("]")] if "[" in using else None
        if leaf is None:
            continue
        host, _, port = match.group("dst").rpartition(":")
        t_close, t_fu, t_eu, b_up, t_fd, t_ed, b_down = sched
        mihomo.add(
            FakeConn(
                conn_id=f"c{index}",
                t_open=t_open,
                sport=int(match.group("sport")),
                host=host.strip("[]"),
                port=int(port),
                leaf=leaf,
                t_close=t_close if t_close is not None else t_end,
                up=ramp(t_fu, t_eu if t_eu is not None else t_close, int(b_up)),
                down=ramp(t_fd, t_ed if t_ed is not None else t_close, int(b_down)),
            )
        )
    return mihomo, feed


def x1_snapshots(fixture: dict[str, Any]) -> list[tuple[float, dict[str, Any]]]:
    base = fixture["base"]
    conns = fixture["conns"]
    out = []
    for t_rel, entries in fixture["polls"]:
        items = []
        for index, up, down in entries:
            sport, host, dport, chain0, start = conns[index]
            items.append(
                {
                    "id": f"x{index}",
                    "metadata": {
                        "network": "tcp",
                        "type": "HTTP",
                        "sourceIP": "127.0.0.1",
                        "sourcePort": str(sport),
                        "host": host,
                        "destinationPort": str(dport),
                        "inboundName": "DEFAULT-MIXED",
                        "specialProxy": "",
                    },
                    "upload": up,
                    "download": down,
                    "start": start,
                    "chains": [chain0, "TIER3-AUTO", "PROXY-FALLBACK", "FINAL-EXIT", "GENERAL"],
                }
            )
        out.append((base + t_rel / 10000.0, {"connections": items}))
    return out
