"""Tier ladder judge service (spec v1 §4.7, §4.10, §4.11; service tests S1-S15).

``PostDialJudgeService`` with ``job.ladder`` set, driven on its real worker,
probe and close threads by fake hooks and an injectable clock (real time plus
an offset a fake probe may advance, so budget cases need no real waiting).
Cases that need a result to arrive *after* a rung's evidence deadline run
with the ladder's margin scaled down (``scaled`` fixture).

Every case asserts what the judge may do: probes through the probe hook,
reads, the rung commit hook, exact-id closes.  Nothing here writes.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

import pytest

import runtime_post_dial_judge as judge_module
import runtime_tier_ladder as tl
from runtime_node_judge import GATE_LEAF_BACKOFF, ProbeClass, StartProbe, Verdict
from runtime_post_dial import PostDialTokenizer, StuckConnections, constants_with
from runtime_post_dial_judge import (
    CLOSE_THREAD_NAME,
    JUDGE_THREAD_NAME,
    PROBE_THREAD_NAME,
    PostDialJudgeHooks,
    PostDialJudgeJob,
    PostDialJudgeService,
    PostDialLadder,
    ProbeOutcome,
    RouteRead,
)
from runtime_post_dial import DemotionWatch
from runtime_tier_ladder import LadderAuthority, Rung, RungKind, authority_diff

TOKENIZER = PostDialTokenizer(bytes(range(32)))
URL_T2 = "http://t2-contract.test/generate_204"
URL_T1 = "http://t1-contract.test/generate_204"
URL_T3 = "http://t3-contract.test/generate_204"
URL_PF = "http://pf-contract.test/generate_204"
PASS, TIMEOUT, FAIL, ANSWER, INDET = (
    ProbeClass.PASS,
    ProbeClass.TIMEOUT,
    ProbeClass.TRANSPORT_FAIL,
    ProbeClass.HTTP_ANSWER,
    ProbeClass.INDETERMINATE,
)
_THREADS = {JUDGE_THREAD_NAME, PROBE_THREAD_NAME, CLOSE_THREAD_NAME}
_LADDER_EVS = {
    "ladder_admit",
    "rung_start",
    "ladder_witness",
    "rung_probe_result",
    "demotion_deferred",
    "rung_verdict",
    "rung_commit",
    "ladder_escalate",
    "ladder_end",
    "probe_refused",
}


@pytest.fixture(autouse=True)
def _no_thread_leak():
    before = {thread for thread in threading.enumerate() if thread.name in _THREADS}
    yield
    deadline = time.monotonic() + 8.0
    leaked: list[threading.Thread] = []
    while time.monotonic() < deadline:
        leaked = [
            thread
            for thread in threading.enumerate()
            if thread.name in _THREADS and thread.is_alive() and thread not in before
        ]
        if not leaked:
            break
        leaked[0].join(0.05)
    assert not leaked, [thread.name for thread in leaked]


@pytest.fixture
def scaled(monkeypatch):
    """The ladder windows at a small margin: T=100 ms rungs get a 0.6 s await
    and a 0.65 s unwitnessed evidence window, so late results take real
    sub-second waits."""

    monkeypatch.setattr(tl, "LADDER_MARGIN_S", 0.5)
    monkeypatch.setattr(tl, "LADDER_NATIVE_TAIL_S", 0.05)
    monkeypatch.setattr(tl, "LADDER_REFRESH_ALLOWANCE_S", 0.05)
    monkeypatch.setattr(tl, "LADDER_REQUEST_EXTRA_S", 0.05)
    return constants_with(min_probe_remaining_s=0.05)


def _rfc3339(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


class Clock:
    """Real monotonic time plus an offset a fake probe may advance."""

    def __init__(self) -> None:
        self.offset = 0.0
        self.lock = threading.Lock()

    def __call__(self) -> float:
        with self.lock:
            return time.monotonic() + self.offset

    def advance(self, seconds: float) -> None:
        with self.lock:
            self.offset += float(seconds)


def rung(
    index: int,
    group: str,
    accused: str,
    depth: int,
    members: tuple[str, ...],
    *,
    url: str,
    timeout_ms: int,
    kind: RungKind | None = None,
    fixed0: str = "",
) -> Rung:
    if kind is None:
        kind = RungKind.GROUP if accused.isupper() else RungKind.LEAF
    return Rung(
        index=index,
        group=group,
        accused=accused,
        depth=depth,
        kind=kind,
        url=url,
        expected="204",
        timeout_ms=timeout_ms,
        members=members,
        fixed0=fixed0,
    )


T2_MEMBERS = ("t2a", "t2b", "t2c")
PF_MEMBERS = ("TIER1-SELECT", "TIER2-AUTO", "TIER3-AUTO")
CHAIN_T2 = ("FINAL-EXIT", "PROXY-FALLBACK", "TIER2-AUTO", "t2a")


def t2_rung(timeout_ms: int = 800, members: tuple[str, ...] = T2_MEMBERS, **kwargs: Any) -> Rung:
    return rung(0, "TIER2-AUTO", "t2a", 3, members, url=URL_T2, timeout_ms=timeout_ms, **kwargs)


def pf_rung(index: int = 1, timeout_ms: int = 5000, accused: str = "TIER2-AUTO", depth: int = 2) -> Rung:
    return rung(index, "PROXY-FALLBACK", accused, depth, PF_MEMBERS, url=URL_PF, timeout_ms=timeout_ms)


class Runner:
    """The observer runner's demotion-watch surface."""

    def __init__(self, stuck: StuckConnections | None = None) -> None:
        self.stuck = stuck
        self.watches: list[tuple[str, float, float]] = []
        self.ended: list[str] = []
        self.marked: list[tuple[str, ...]] = []
        self.on_start: Callable[[], Any] | None = None
        self.start_flags: list[Any] = []

    def start_demotion_watch(self, leaf: str, *, t_demote_wall: float) -> DemotionWatch:
        self.watches.append((leaf, t_demote_wall, time.monotonic()))
        self.start_flags.append(None if self.on_start is None else self.on_start())
        return DemotionWatch(leaf=leaf, t_demote_wall=t_demote_wall, until_perf=time.perf_counter() + 60.0)

    def end_demotion_watch(self, leaf: str) -> None:
        self.ended.append(leaf)

    def stuck_now(self, leaf: str, *, t_demote_wall: float, max_age_s: float) -> StuckConnections | None:
        return self.stuck

    def mark_closed(self, ids: Any) -> None:
        self.marked.append(tuple(ids))


def stuck(*ids: str) -> StuckConnections:
    return StuckConnections(
        ids=tuple(ids),
        aged=len(ids),
        waiting=0,
        kept_progressing=0,
        kept_not_stuck=0,
        kept_after_demotion=0,
        kept_other=0,
        snapshot_wall=time.time(),
    )


class Fake:
    """Fake runtime hooks over a scripted Mihomo.

    ``script[(group, target)]`` = ``(sleep_s, class, advance_s)`` or a
    callable returning one (called on the probe thread).  ``groups[name]`` is
    the read model ``(members, now, alive, unknown)`` a rung read returns.
    """

    def __init__(self, clock: Clock, script: dict[tuple[str, str], Any]) -> None:
        self.clock = clock
        self.script = script
        self.groups: dict[str, tuple[tuple[str, ...], str, dict[str, bool], frozenset[str]]] = {}
        self.escalate_alive: dict[int, dict[str, bool]] = {}
        self.escalate_result: dict[int, str | None] = {}
        self.deeper_moved: dict[int, bool] = {}
        # Seconds the escalation hook's refresh takes (clock advance).
        self.escalate_advance: dict[int, float] = {}
        self.leaf_entries: dict[str, Any] = {}
        self.check: Callable[[PostDialJudgeJob], str | None] | None = None
        self.commit_result: tuple[bool, str] | None = (True, "post_dial_rung_native_rebind")
        self.commit_pick: str | None = None
        self.close_enabled_value = True
        self.lock = threading.Lock()
        self.probes: list[dict[str, Any]] = []
        self.reads: list[int | None] = []
        self.commits: list[tuple[int, Any]] = []
        self.finished: list[str] = []
        self.closes: list[tuple[str, tuple[str, ...]]] = []
        self.checks: list[float] = []
        self.escalations: list[int] = []
        self.ends: list[tuple[str, bool]] = []
        self.service: PostDialJudgeService | None = None

    # hooks ------------------------------------------------------------
    def probe(self, job: PostDialJudgeJob, action: StartProbe) -> ProbeOutcome:
        record = {
            "name": action.name,
            "leaf": action.leaf,
            "group": action.group,
            "url": action.url,
            "expected": action.expected,
            "timeout_ms": action.timeout_ms,
            "request_timeout_s": action.request_timeout_s,
            "admission_s": action.admission_s,
            "await_s": action.await_s,
            "started": self.clock(),
            "rung": None if job.ladder is None else job.ladder.index,
        }
        with self.lock:
            self.probes.append(record)
        entry = self.script[(action.group, action.leaf)] if action.group is not None else self.script[("legacy", action.leaf)]
        if callable(entry):
            entry = entry()
        delay, result, advance = entry
        if delay:
            time.sleep(delay)
        if advance:
            self.clock.advance(advance)
        record["ended"] = self.clock()
        return ProbeOutcome(result, elapsed_ms=round(delay * 1000.0, 1), queue_wait_ms=0.0)

    def read_route(self, job: PostDialJudgeJob) -> RouteRead | None:
        if job.ladder is None:
            group, index = "TIER2-AUTO", None
        else:
            group, index = job.ladder.current.group, job.ladder.index
        with self.lock:
            self.reads.append(index)
        model = self.groups.get(group)
        if model is None:
            return None
        members, now, alive, unknown = model
        return RouteRead(
            members=members,
            now=now,
            fixed="",
            alive=dict(alive),
            read_at=self.clock(),
            unknown=frozenset(unknown),
        )

    def read_leaf(self, job: PostDialJudgeJob, leaf: str) -> Any:
        return self.leaf_entries.get(leaf)

    def commit(self, job: PostDialJudgeJob, done: Any) -> tuple[bool, str]:
        ladder = job.ladder
        index = None if ladder is None else ladder.index
        with self.lock:
            self.commits.append((index, done))
        if ladder is not None:
            pick = self.commit_pick or done.successor
            ladder.note_commit(pick=pick, proven=pick in ladder.passed_at(ladder.index), sacrificial=False)
        if self.commit_result is None:
            raise RuntimeError("commit failed")
        return self.commit_result

    def finish(self, job: PostDialJudgeJob, reason: str) -> None:
        with self.lock:
            self.finished.append(reason)

    def close(self, job: PostDialJudgeJob, leaf: str, ids: tuple[str, ...]) -> Any:
        with self.lock:
            self.closes.append((leaf, tuple(ids)))
        return tuple(ids)

    def ladder_check(self, job: PostDialJudgeJob) -> str | None:
        with self.lock:
            self.checks.append(self.clock())
        return None if self.check is None else self.check(job)

    def ladder_escalate(self, job: PostDialJudgeJob, k: int) -> str | None:
        with self.lock:
            self.escalations.append(k)
        ladder = job.ladder
        assert ladder is not None and ladder.index == k
        if k in self.escalate_alive:
            ladder.set_rung_state(k, alive=self.escalate_alive[k])
        ladder.deeper_moved = bool(self.deeper_moved.get(k))
        if self.escalate_advance.get(k):
            self.clock.advance(self.escalate_advance[k])
        return self.escalate_result.get(k)

    def ladder_end(self, job: PostDialJudgeJob, outcome: str) -> None:
        in_flight = None if self.service is None else self.service.ladder_in_flight()
        with self.lock:
            self.ends.append((outcome, in_flight))

    def hooks(self, *, ladder_hooks: bool = True) -> PostDialJudgeHooks:
        return PostDialJudgeHooks(
            probe=self.probe,
            read_route=self.read_route,
            read_leaf=self.read_leaf,
            commit=self.commit,
            finish=self.finish,
            close=self.close,
            close_enabled=lambda: self.close_enabled_value,
            ladder_check=self.ladder_check if ladder_hooks else None,
            ladder_escalate=self.ladder_escalate if ladder_hooks else None,
            ladder_end=self.ladder_end if ladder_hooks else None,
        )


class Harness:
    def __init__(
        self,
        rungs: tuple[Rung, ...],
        script: dict[tuple[str, str], Any],
        *,
        constants: Any = None,
        runner: Runner | None = None,
        chain0: tuple[str, ...] = CHAIN_T2,
        budget_s: float = 20.0,
        leaf: str | None = None,
        authority: Any = None,
    ) -> None:
        self.clock = Clock()
        self.fake = Fake(self.clock, script)
        self.records: list[tuple[str, dict[str, Any]]] = []
        self.records_lock = threading.Lock()
        kwargs: dict[str, Any] = {}
        if constants is not None:
            kwargs["constants"] = constants
        self.service = PostDialJudgeService(
            hooks=self.fake.hooks(),
            tokenizer=TOKENIZER,
            emit=self.emit,
            clock=self.clock,
            **kwargs,
        )
        self.fake.service = self.service
        self.runner = runner if runner is not None else Runner()
        self.rungs = rungs
        self.leaf = leaf if leaf is not None else rungs[0].accused
        self.deadline = self.clock() + budget_s
        self.job = PostDialJudgeJob(
            leaf=self.leaf,
            kind="FAST",
            tokens=("aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"),
            episode=7,
            test_url=rungs[0].url,
            expected_status="204",
            ticket_deadline=self.deadline,
            runner=self.runner,
            context=None,
        )
        self.job.ladder = PostDialLadder(
            rungs=rungs,
            authority=authority,
            deadline=self.deadline,
            ladder_id="D0123456789a",
            chain0=chain0,
        )
        # Read models: every rung group, everything alive, ``now`` = accused.
        for item in rungs:
            self.fake.groups[item.group] = (item.members, item.accused, {name: True for name in item.members}, frozenset())

    def emit(self, level: str, record: Any) -> None:
        with self.records_lock:
            self.records.append((level, dict(record)))

    def run(self, timeout: float = 15.0) -> None:
        assert self.service.start(self.job)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.ev("ladder_end"):
                break
            time.sleep(0.005)
        assert self.service.wait_idle(timeout), "judge did not go idle"

    # record helpers ---------------------------------------------------
    def ev(self, name: str) -> list[dict[str, Any]]:
        with self.records_lock:
            return [record for _level, record in self.records if record.get("ev") == name]

    def seq(self, predicate: Callable[[dict[str, Any]], bool]) -> list[int]:
        with self.records_lock:
            return [index for index, (_level, record) in enumerate(self.records) if predicate(record)]

    def end(self) -> dict[str, Any]:
        ends = self.ev("ladder_end")
        assert len(ends) == 1, ends
        return ends[0]

    def probes(self, **match: Any) -> list[dict[str, Any]]:
        with self.fake.lock:
            items = list(self.fake.probes)
        return [item for item in items if all(item.get(key) == value for key, value in match.items())]


def tok(name: str) -> str:
    return TOKENIZER.leaf(name)


def ttok(name: str) -> str:
    return "T" + TOKENIZER.leaf(name)


def instant(result: ProbeClass, advance: float = 0.0) -> tuple[float, ProbeClass, float]:
    return (0.0, result, advance)


def exhausted_t2_script() -> dict[tuple[str, str], Any]:
    """Rung ① (Tier2, 3 members, cap 1): A_C and S_C0 fail on transport;
    t2c reads Core-dead, so the rung is EXHAUSTED_COMPLETE."""

    return {
        ("TIER2-AUTO", "t2a"): instant(FAIL),
        ("TIER2-AUTO", "t2b"): instant(FAIL),
    }


def two_rungs(**kwargs: Any) -> tuple[Rung, Rung]:
    return (t2_rung(**kwargs), pf_rung())


def with_exhausted_rung1(h: Harness) -> None:
    h.fake.groups["TIER2-AUTO"] = (T2_MEMBERS, "t2a", {"t2a": True, "t2b": True, "t2c": False}, frozenset())


# ---------------------------------------------------------------------------
# S1 single rung


def test_s1_single_rung_tier2_convict_with_legacy_records() -> None:
    h = Harness(
        (t2_rung(),),
        {
            # A_C at the 800 ms contract times out; S_C0 starts at +0.4 s and passes.
            ("TIER2-AUTO", "t2a"): (0.8, TIMEOUT, 0.0),
            ("TIER2-AUTO", "t2b"): instant(PASS),
        },
    )
    h.run()
    ac = h.probes(name="A_C")[0]
    s0 = h.probes(name="S_C0")[0]
    assert ac["timeout_ms"] == 800 and ac["request_timeout_s"] == 1.45
    assert ac["url"] == URL_T2 and ac["group"] == "TIER2-AUTO" and ac["expected"] == "204"
    assert ac["admission_s"] == 1.9 and ac["await_s"] == 2.8
    assert s0["leaf"] == "t2b" and 0.38 <= s0["started"] - ac["started"] < 0.7
    assert len(h.fake.commits) == 1 and h.fake.commits[0][0] == 0
    assert h.fake.commits[0][1].successor == "t2b"
    (verdict,) = h.ev("verdict")
    assert verdict["verdict"] == "CONVICT_CONTRACT" and verdict["ladder"] == "D0123456789a"
    assert verdict["rung"] == 1 and verdict["rverdict"] == "CONVICT_CONTRACT"
    assert "all_loaded_dead" not in verdict
    (rv,) = h.ev("rung_verdict")
    assert rv["verdict"] == "CONVICT_CONTRACT" and rv["grp"] == "T2" and rv["acc"] == ttok("t2a")
    assert sorted(rv["results"]) == ["A_C:timeout", "S_C0:pass"]  # resolution order
    (commit,) = h.ev("commit")
    assert commit["accepted"] is True and commit["rung"] == 1 and commit["ladder"] == "D0123456789a"
    assert commit["pick"] == ttok("t2b") and commit["proven"] == 1 and commit["sacrificial"] == 0
    assert list(commit)[:7] == ["ev", "t", "leaf", "successor", "accepted", "reason", "durable"]
    assert {record["name"] for record in h.ev("probe_result")} == {"A_C", "S_C0"}
    assert all(record["rung"] == 1 for record in h.ev("probe_result"))
    assert not h.ev("rung_commit") and not h.ev("late_hold")
    end = h.end()
    assert end["outcome"] == "convicted" and end["commit"] == "accepted" and end["rung"] == 1
    assert end["rungs_run"] == 1 and end["probes"] == 2 and end["witnesses"] == 0 and end["hold"] is None
    assert end["dead_marks"] == 1 and end["pf_probe"] == 0
    assert h.fake.ends == [("convicted", False)]
    assert h.fake.finished == []  # the commit settles the ticket
    assert not h.service.gates.in_flight and h.job.settled
    (admit,) = h.ev("ladder_admit")
    assert admit["path"] == ["FE", "PF", "T2", "L"] and admit["rungs"] == 1 and admit["timeouts"] == [800]
    assert admit["budget_s"] == 3.7 and admit["cap_s"] == 20.0 and admit["lease"] == "none"


# ---------------------------------------------------------------------------
# S2 escalation: EXHAUSTED_COMPLETE -> witness -> A_C -> rung commit


def test_s2_exhausted_escalates_witness_first_and_commits_rung2() -> None:
    script = exhausted_t2_script()
    script.update(
        {
            ("PROXY-FALLBACK", "TIER3-AUTO"): instant(PASS),
            ("PROXY-FALLBACK", "TIER2-AUTO"): instant(FAIL),
        }
    )
    h = Harness(two_rungs(), script)
    with_exhausted_rung1(h)
    h.fake.escalate_alive[1] = {"TIER1-SELECT": False, "TIER2-AUTO": True, "TIER3-AUTO": True}
    h.run()
    assert h.fake.escalations == [1]
    names = [(item["rung"], item["name"], item["leaf"]) for item in h.fake.probes]
    assert names == [(0, "A_C", "t2a"), (0, "S_C0", "t2b"), (1, "W0", "TIER3-AUTO"), (1, "A_C", "TIER2-AUTO")]
    upper = h.probes(rung=1)
    assert all(item["url"] == URL_PF and item["timeout_ms"] == 5000 and item["await_s"] == 7.0 for item in upper)
    assert all(item["request_timeout_s"] == 5.65 and item["admission_s"] == 1.9 for item in upper)
    # Ordering: rung ①'s rung_verdict before every rung ② record; the
    # witness pass before rung ②'s A_C.
    first_verdict = h.seq(lambda r: r.get("ev") == "rung_verdict" and r.get("rung") == 1)[0]
    rung2 = h.seq(lambda r: r.get("rung") == 2 or r.get("to_rung") == 2)
    assert rung2 and min(rung2) > first_verdict
    witness = h.seq(lambda r: r.get("ev") == "ladder_witness" and r.get("class") == "pass")[0]
    ac2 = h.seq(lambda r: r.get("ev") == "rung_probe_result" and r.get("name") == "A_C")[0]
    assert witness < ac2
    (rv1, rv2) = h.ev("rung_verdict")
    assert rv1["verdict"] == "EXHAUSTED_COMPLETE" and rv2["verdict"] == "CONVICT_CONTRACT"
    (escalate,) = h.ev("ladder_escalate")
    assert escalate["from_rung"] == 1 and escalate["to_rung"] == 2 and escalate["cause"] == "exhausted_complete"
    assert escalate["refresh"] == "ok" and escalate["deeper_moved"] == 0
    starts = h.ev("rung_start")
    assert [s["rung"] for s in starts] == [1, 2] and [s["witnessed"] for s in starts] == [0, 1]
    assert starts[1]["grp"] == "PF" and starts[1]["akind"] == "group" and starts[1]["ticket_s"] == 7.5
    assert starts[0]["ticket_s"] == 3.7 and starts[1]["gtok"] == ttok("PROXY-FALLBACK")
    (witness_record,) = h.ev("ladder_witness")
    assert witness_record["name"] == "W0" and witness_record["leaf"] == tok("TIER3-AUTO")
    assert witness_record["core_alive"] == 1
    (commit,) = h.ev("rung_commit")
    assert commit["rung"] == 2 and commit["grp"] == "PF" and commit["accepted"] is True
    assert commit["pick"] == ttok("TIER3-AUTO") and commit["proven"] == 1 and commit["acc"] == ttok("TIER2-AUTO")
    assert not h.ev("commit")  # rung ② uses rung_commit
    assert h.fake.commits[0][0] == 1 and h.fake.commits[0][1].successor == "TIER3-AUTO"
    end = h.end()
    assert end["outcome"] == "convicted" and end["rung"] == 2 and end["rungs_run"] == 2
    assert end["witnesses"] == 1 and end["probes"] == 4 and end["pf_probe"] == 1
    assert h.service.gates.hold is None
    assert h.job.ladder.pf_probe_resolved_at is not None


# ---------------------------------------------------------------------------
# S3 facility witness timeout


def test_s3_facility_witness_timeout_confirms_and_holds_once_at_stop() -> None:
    script = {
        ("TIER2-AUTO", "t2a"): instant(TIMEOUT),
        ("TIER2-AUTO", "t2b"): instant(TIMEOUT),
        ("PROXY-FALLBACK", "TIER3-AUTO"): instant(TIMEOUT),
    }
    h = Harness(two_rungs(), script)
    h.fake.escalate_alive[1] = {"TIER1-SELECT": False, "TIER2-AUTO": True, "TIER3-AUTO": True}
    holds: list[tuple[Verdict, int]] = []
    original = h.service.gates.set_hold

    def spy(result: Verdict, now_perf: float) -> None:
        holds.append((result, len(h.fake.probes)))
        original(result, now_perf)

    h.service.gates.set_hold = spy  # type: ignore[method-assign]
    h.run()
    assert [item["name"] for item in h.fake.probes] == ["A_C", "S_C0", "W0"]
    assert not h.probes(group="PROXY-FALLBACK", name="A_C")
    assert holds == [(Verdict.FACILITY, 3)]  # once, after the witness
    assert h.service.gates.hold is Verdict.FACILITY
    end = h.end()
    assert end["outcome"] == "facility_confirmed" and end["hold"] == "FACILITY" and end["commit"] == "none"
    assert h.fake.finished == ["facility_confirmed"]
    (escalate,) = h.ev("ladder_escalate")
    assert escalate["cause"] == "facility"
    rvs = h.ev("rung_verdict")
    assert [r["verdict"] for r in rvs] == ["FACILITY", "FACILITY"]
    assert rvs[1]["results"] == ["W0:timeout"]


# ---------------------------------------------------------------------------
# S4 fit refusals


def test_s4_f2_refusal_stops_with_the_causes_hold() -> None:
    rungs = (
        rung(0, "TIER3-AUTO", "n1", 3, ("n1", "n2"), url=URL_T3, timeout_ms=5000),
        pf_rung(accused="TIER3-AUTO"),
    )
    h = Harness(
        rungs,
        {
            ("TIER3-AUTO", "n1"): instant(FAIL, advance=6.0),
            ("TIER3-AUTO", "n2"): instant(FAIL),
        },
        chain0=("FINAL-EXIT", "PROXY-FALLBACK", "TIER3-AUTO", "n1"),
    )
    h.run()
    assert h.fake.escalations == [] and not h.probes(group="PROXY-FALLBACK")
    end = h.end()
    assert end["outcome"] == "ladder_budget_exhausted" and end["hold"] == "EXHAUSTED"
    assert h.service.gates.hold is Verdict.EXHAUSTED
    assert h.ev("rung_verdict")[0]["verdict"] == "EXHAUSTED_COMPLETE"


def test_s4_f3_refusal_never_issues_the_upper_ac() -> None:
    script = exhausted_t2_script()
    script[("PROXY-FALLBACK", "TIER3-AUTO")] = instant(FAIL, advance=6.0)
    h = Harness(two_rungs(), script)
    with_exhausted_rung1(h)
    h.fake.escalate_alive[1] = {"TIER1-SELECT": True, "TIER2-AUTO": True, "TIER3-AUTO": True}
    h.fake.groups["PROXY-FALLBACK"] = (PF_MEMBERS, "TIER2-AUTO", {}, frozenset())
    # Order: TIER1-SELECT, TIER3-AUTO (both alive): make W0 the failing one.
    script[("PROXY-FALLBACK", "TIER1-SELECT")] = instant(FAIL, advance=6.0)
    h.run()
    assert [item["name"] for item in h.probes(rung=1)] == ["W0"]
    end = h.end()
    assert end["outcome"] == "ladder_budget_exhausted" and end["hold"] == "EXHAUSTED"
    assert end["witnesses"] == 1 and end["witness_wasted"] == 0


def test_s4_f4_refusal_wastes_the_witness() -> None:
    # F2 fits at 4.5 s (4.5 + 15.25 <= 20); the refresh then takes 1.5 s and
    # the witness 6.9 s (inside its 7.0 s bound): 12.9 + 7.5 > 20.  Every
    # clock jump stays inside the await bound of the probe it simulates.
    script = exhausted_t2_script()
    script[("TIER2-AUTO", "t2b")] = instant(FAIL, advance=4.5)
    script[("PROXY-FALLBACK", "TIER3-AUTO")] = instant(PASS, advance=6.9)
    h = Harness(two_rungs(), script)
    with_exhausted_rung1(h)
    h.fake.escalate_advance[1] = 1.5
    h.fake.escalate_alive[1] = {"TIER1-SELECT": False, "TIER2-AUTO": True, "TIER3-AUTO": True}
    h.run()
    assert [item["name"] for item in h.probes(rung=1)] == ["W0"]
    end = h.end()
    assert end["outcome"] == "ladder_budget_exhausted" and end["witness_wasted"] == 1
    assert end["hold"] == "EXHAUSTED" and not h.fake.commits


# ---------------------------------------------------------------------------
# S5 authority, S6 D4 transactions


def _authority(**changes: Any) -> LadderAuthority:
    base = dict(
        pid=10,
        process_generation=1,
        core_generation=2,
        semantic_digest="abc",
        network_context_epoch=3,
        selector_intent_revision=4,
        facility_epoch=5,
        lifecycle_revision=6,
        worker_token=("w", 1),
        group_graph=(("PROXY-FALLBACK", PF_MEMBERS),),
        policy_key=(True, True, False),
        lease_obj_id=None,
        lease_release_state="",
        lease_digest="0" * 16,
    )
    base.update(changes)
    return LadderAuthority(**base)


@pytest.mark.parametrize(
    ("change", "outcome"),
    [
        ({"pid": 11}, "authority_changed_pid"),
        ({"core_generation": 3}, "authority_changed_core_generation"),
        ({"selector_intent_revision": 5}, "authority_changed_selector_intent_revision"),
        ({"policy_key": (False, True, False)}, "authority_changed_policy"),
        ({"lease_digest": "1" * 16}, "authority_changed_lease"),
        ({"lease_obj_id": 1234}, "authority_changed_lease"),
    ],
)
def test_s5_authority_change_before_a_witness_stops_the_ladder(change: dict[str, Any], outcome: str) -> None:
    baseline = _authority()
    current = [baseline]
    script = exhausted_t2_script()

    def s_c0() -> tuple[float, ProbeClass, float]:
        current[0] = _authority(**change)  # changes while rung ① runs
        return instant(FAIL)

    script[("TIER2-AUTO", "t2b")] = s_c0
    h = Harness(two_rungs(), script, authority=baseline)
    with_exhausted_rung1(h)
    h.fake.escalate_alive[1] = {"TIER1-SELECT": False, "TIER2-AUTO": True, "TIER3-AUTO": True}
    h.fake.check = lambda job: authority_diff(job.ladder.authority, current[0])
    h.run()
    assert [item["name"] for item in h.fake.probes] == ["A_C", "S_C0"]  # no witness
    end = h.end()
    assert end["outcome"] == outcome and end["hold"] is None and end["rung"] == 2
    assert h.fake.finished == [outcome]
    assert h.ev("rung_verdict")[-1]["verdict"] == "INDETERMINATE"


def test_s5_authority_change_before_the_first_ac_issues_nothing() -> None:
    h = Harness((t2_rung(),), exhausted_t2_script(), authority=_authority())
    h.fake.check = lambda job: "authority_changed:network_context_epoch"
    h.run()
    assert h.fake.probes == [] and h.fake.commits == []
    end = h.end()
    assert end["outcome"] == "authority_changed_network_context_epoch" and end["probes"] == 0
    assert not h.ev("verdict")  # no legacy verdict without a probe
    assert h.ev("rung_verdict")[0]["verdict"] == "INDETERMINATE"
    assert h.service.gates._backoff.get("t2a") is not None  # INDETERMINATE bookkeeping


@pytest.mark.parametrize("where", ["ac", "successor", "witness", "commit"])
def test_s6_d4_txn_busy_stops_before_the_unit(where: str) -> None:
    busy = threading.Event()
    script = exhausted_t2_script()
    if where == "ac":
        busy.set()
    elif where == "successor":
        script[("TIER2-AUTO", "t2a")] = lambda: (busy.set(), instant(FAIL))[1]
    elif where == "witness":
        script[("TIER2-AUTO", "t2b")] = lambda: (busy.set(), instant(FAIL))[1]
    script[("PROXY-FALLBACK", "TIER3-AUTO")] = instant(PASS)
    script[("PROXY-FALLBACK", "TIER2-AUTO")] = (
        (lambda: (busy.set(), instant(FAIL))[1]) if where == "commit" else instant(FAIL)
    )
    h = Harness(two_rungs(), script)
    with_exhausted_rung1(h)
    h.fake.escalate_alive[1] = {"TIER1-SELECT": False, "TIER2-AUTO": True, "TIER3-AUTO": True}
    h.fake.check = lambda job: "d4_txn_busy:recovery_worker" if busy.is_set() else None
    h.run()
    issued = [item["name"] for item in h.fake.probes]
    expected = {
        "ac": [],
        "successor": ["A_C"],
        "witness": ["A_C", "S_C0"],
        "commit": ["A_C", "S_C0", "W0", "A_C"],
    }[where]
    assert issued == expected
    assert h.fake.commits == []
    end = h.end()
    assert end["outcome"] == "d4_txn_busy_recovery_worker" and end["commit"] == "none"


# ---------------------------------------------------------------------------
# S7 settled verdicts (late results), S8 partial


def _late_script(late: ProbeClass) -> dict[tuple[str, str], Any]:
    return {
        ("TIER2-AUTO", "t2a"): instant(TIMEOUT),
        ("TIER2-AUTO", "t2b"): (0.3, FAIL, 0.0),
        # S_C1 starts at ~0.3 s and answers at ~0.8 s: after the evidence
        # deadline (0.65 s), before its await bound (0.3 + 0.6 s).
        ("TIER2-AUTO", "t2c"): (0.5, late, 0.0),
    }


T2_FIVE = ("t2a", "t2b", "t2c", "t2d", "t2e")  # cap 2


def test_s7_late_timeout_settles_facility_and_escalates_without_a_mid_ladder_hold(scaled) -> None:
    script = _late_script(TIMEOUT)
    script[("PROXY-FALLBACK", "TIER3-AUTO")] = instant(TIMEOUT)
    rungs = (t2_rung(timeout_ms=100, members=T2_FIVE), pf_rung(timeout_ms=100))
    h = Harness(rungs, script, constants=scaled)
    h.fake.escalate_alive[1] = {"TIER1-SELECT": False, "TIER2-AUTO": True, "TIER3-AUTO": True}
    holds: list[int] = []
    original = h.service.gates.set_hold
    h.service.gates.set_hold = lambda result, now: (holds.append(len(h.fake.probes)), original(result, now))[1]  # type: ignore[method-assign]
    h.run()
    (verdict,) = h.ev("verdict")
    assert verdict["verdict"] == "INDETERMINATE" and verdict["rverdict"] == "FACILITY"
    assert h.ev("rung_verdict")[0]["verdict"] == "FACILITY"
    assert h.fake.escalations == [1] and not h.ev("late_hold")
    assert holds == [4]  # only at the stop, after the witness
    assert h.end()["outcome"] == "facility_confirmed"


def test_s7_late_pass_is_commit_late_without_escalation(scaled) -> None:
    rungs = (t2_rung(timeout_ms=100, members=T2_FIVE), pf_rung(timeout_ms=100))
    h = Harness(rungs, _late_script(PASS), constants=scaled)
    h.run()
    (verdict,) = h.ev("verdict")
    assert verdict["verdict"] == "INDETERMINATE" and verdict["rverdict"] == "CONVICT_CONTRACT"
    assert h.fake.escalations == [] and h.fake.commits == []
    (commit,) = h.ev("commit")
    assert commit["accepted"] is False and commit["reason"] == "commit_late" and commit["rung"] == 1
    end = h.end()
    assert end["outcome"] == "commit_late" and end["hold"] is None
    assert h.fake.finished == ["commit_late"] and not h.ev("late_hold")


def test_s8_exhausted_partial_is_moved_unproven_with_the_exhausted_hold() -> None:
    h = Harness(
        (t2_rung(members=T2_FIVE), pf_rung()),
        {
            ("TIER2-AUTO", "t2a"): instant(FAIL),
            ("TIER2-AUTO", "t2b"): instant(FAIL),
            ("TIER2-AUTO", "t2c"): instant(FAIL),
        },
    )
    h.run()
    assert [item["name"] for item in h.fake.probes] == ["A_C", "S_C0", "S_C1"]
    assert h.ev("rung_verdict")[0]["verdict"] == "EXHAUSTED_PARTIAL"
    assert h.fake.escalations == []
    end = h.end()
    assert end["outcome"] == "moved_unproven" and end["hold"] == "EXHAUSTED"
    assert h.service.gates.hold is Verdict.EXHAUSTED


# ---------------------------------------------------------------------------
# S9 NOT_REPRODUCED at an escalated rung


@pytest.mark.parametrize(
    ("cause", "moved", "outcome"),
    [
        ("exhausted", True, "recovered_by_deeper_move"),
        ("exhausted", False, "not_reproduced_upper"),
        ("facility", True, "facility_transient"),
    ],
)
def test_s9_not_reproduced_upper_rung(cause: str, moved: bool, outcome: str) -> None:
    if cause == "exhausted":
        script = exhausted_t2_script()
    else:
        script = {("TIER2-AUTO", "t2a"): instant(TIMEOUT), ("TIER2-AUTO", "t2b"): instant(TIMEOUT)}
    script[("PROXY-FALLBACK", "TIER3-AUTO")] = instant(PASS)
    script[("PROXY-FALLBACK", "TIER2-AUTO")] = instant(PASS)
    runner = Runner(stuck("c1"))
    h = Harness(two_rungs(), script, runner=runner)
    if cause == "exhausted":
        with_exhausted_rung1(h)
    h.fake.escalate_alive[1] = {"TIER1-SELECT": False, "TIER2-AUTO": True, "TIER3-AUTO": True}
    h.fake.deeper_moved[1] = moved
    runner.on_start = lambda: bool(h.ev("ladder_end"))
    h.run()
    end = h.end()
    assert end["outcome"] == outcome and end["hold"] is None and not h.fake.commits
    assert h.ev("rung_verdict")[-1]["verdict"] == "NOT_REPRODUCED"
    backoff = h.service.gates._backoff.get("t2a")
    if outcome == "recovered_by_deeper_move":
        # CONVICT bookkeeping (no exoneration, no back-off); the watch starts
        # only after ladder_end is logged.
        assert backoff is None and not h.service.gates.is_exonerated("t2a", "aaaaaaaaaaaaaaaa", h.clock())
        assert len(runner.watches) == 1 and runner.start_flags == [True]
        assert h.service._watches["t2a"].ladder.mode == "group"
    else:
        assert backoff is not None  # INDETERMINATE bookkeeping
        assert runner.watches == []
        # 10-28 ship2: INDETERMINATE keeps LEAF_BACKOFF_S (10 s), not the 420 s.
        assert h.service.constants.leaf_backoff_s == 10.0
        assert backoff - h.clock() <= 10.0 + 1e-6


def test_s9_rung1_not_reproduced_backs_off_l0_for_420s() -> None:
    """10-28 ship2: rung ①'s NOT_REPRODUCED bookkeeping applies
    NOT_REPRODUCED_BACKOFF_S (420 s) to L0; other leaves stay open."""

    h = Harness(two_rungs(), {("TIER2-AUTO", "t2a"): instant(PASS), ("TIER2-AUTO", "t2b"): instant(PASS)})
    h.run()
    end = h.end()
    assert end["outcome"] == "not_reproduced" and end["rungs_run"] == 1 and not h.fake.commits
    assert h.ev("rung_verdict")[-1]["verdict"] == "NOT_REPRODUCED"
    assert h.fake.escalations == []
    now = h.clock()
    backoff = h.service.gates._backoff["t2a"]
    assert 419.0 < backoff - now <= 420.0 + 1e-6  # float clock: 2452.046 - 2032.046 > 420.0
    assert h.service.gates.is_exonerated("t2a", "aaaaaaaaaaaaaaaa", now)
    assert h.service.blocked_cheap("t2a", now + 419.0) == GATE_LEAF_BACKOFF
    assert h.service.blocked_cheap("t2a", backoff) is None
    assert h.service.blocked_cheap("t2b", now) is None


# ---------------------------------------------------------------------------
# S10 closes


def _run_tier3_exhausted(ladder: bool) -> tuple[list[tuple[str, tuple[str, ...]]], list[dict[str, Any]], Runner]:
    """Rung ① leaf EXHAUSTED with stuck connections, with and without a ladder."""

    runner = Runner(stuck("c1", "c2"))
    members = ("n1", "n2", "n3")
    single = (rung(0, "TIER2-AUTO", "n1", 3, members, url=URL_T2, timeout_ms=5000),)
    h = Harness(single, {}, runner=runner, chain0=("FINAL-EXIT", "PROXY-FALLBACK", "TIER2-AUTO", "n1"))
    state = {"n1": True, "n2": True, "n3": False}

    def fail_and_demote() -> tuple[float, ProbeClass, float]:
        state["n1"] = False  # Mihomo writes the sample before it replies
        h.fake.groups["TIER2-AUTO"] = (members, "n2", dict(state), frozenset())
        return instant(FAIL)

    h.fake.groups["TIER2-AUTO"] = (members, "n1", dict(state), frozenset())
    key = "TIER2-AUTO" if ladder else "legacy"
    h.fake.script[(key, "n1")] = fail_and_demote
    h.fake.script[(key, "n2")] = instant(FAIL)
    if not ladder:
        h.job.ladder = None
    assert h.service.start(h.job)
    assert h.service.wait_idle(10.0)
    watch = DemotionWatch(leaf="n1", t_demote_wall=time.time(), until_perf=time.perf_counter() + 60.0)
    deadline = time.monotonic() + 5.0
    while not h.fake.closes and time.monotonic() < deadline:
        h.service.on_watch_step(runner, watch, runner.stuck)
        h.service.wait_idle(2.0)
        time.sleep(0.01)
    closes = [
        {key: value for key, value in record.items() if key not in {"t", "ladder", "rung"}}
        for record in h.ev("close")
    ]
    return list(h.fake.closes), closes, runner


def test_s10_rung1_leaf_closes_exactly_as_key_off() -> None:
    ladder_closes, ladder_records, ladder_runner = _run_tier3_exhausted(True)
    legacy_closes, legacy_records, legacy_runner = _run_tier3_exhausted(False)
    assert ladder_closes == legacy_closes == [("n1", ("c1", "c2"))]
    assert [r for r in ladder_records if r["action"] == "close"] == [r for r in legacy_records if r["action"] == "close"]
    assert len(ladder_runner.watches) == len(legacy_runner.watches) == 1
    assert ladder_runner.marked == legacy_runner.marked == [("c1", "c2")]


def _escalated_commit(runner: Runner, *, witness_timeout: bool = False) -> Harness:
    script = exhausted_t2_script()
    if witness_timeout:
        script[("PROXY-FALLBACK", "TIER1-SELECT")] = instant(TIMEOUT)
    script[("PROXY-FALLBACK", "TIER3-AUTO")] = instant(PASS)
    script[("PROXY-FALLBACK", "TIER2-AUTO")] = instant(FAIL)
    h = Harness(two_rungs(), script, runner=runner)
    with_exhausted_rung1(h)
    h.fake.escalate_alive[1] = {"TIER1-SELECT": witness_timeout, "TIER2-AUTO": True, "TIER3-AUTO": True}
    # After the move: PF serves TIER3-AUTO and reads TIER2-AUTO Core-dead.
    h.fake.groups["PROXY-FALLBACK"] = (
        PF_MEMBERS,
        "TIER3-AUTO",
        {"TIER1-SELECT": False, "TIER2-AUTO": False, "TIER3-AUTO": True},
        frozenset(),
    )
    return h


def test_s10_group_rung_watch_starts_only_after_an_accepted_commit() -> None:
    runner = Runner(stuck("c9"))
    commit_seen: list[int] = []
    h = _escalated_commit(runner)
    original = h.fake.commit

    def commit(job: PostDialJudgeJob, done: Any) -> tuple[bool, str]:
        commit_seen.append(len(runner.watches))
        return original(job, done)

    h.service._hooks = PostDialJudgeHooks(**{**{f: getattr(h.service._hooks, f) for f in PostDialJudgeHooks.__dataclass_fields__}, "commit": commit})
    h.run()
    assert commit_seen == [0]  # no watch before the commit (rung ① not demoted)
    # The watch's first step (stuck ids at its start) closes on the read of
    # the committed rung.
    assert len(runner.watches) == 1
    assert h.fake.closes == [("t2a", ("c9",))]
    (close,) = [r for r in h.ev("close") if r["action"] == "close"]
    assert close["ladder"] == "D0123456789a" and close["rung"] == 2
    commit_at = h.seq(lambda r: r.get("ev") == "rung_commit")[0]
    assert h.seq(lambda r: r.get("ev") == "close")[0] > commit_at
    # The watch read the committed rung (PF), not rung ①.
    assert h.fake.reads[-1] == 1


def test_s10_group_rung_watch_keeps_all_after_a_witness_timeout() -> None:
    runner = Runner(stuck("c9"))
    h = _escalated_commit(runner, witness_timeout=True)
    h.run()
    assert [item["name"] for item in h.probes(rung=1)] == ["W0", "W1", "A_C"]
    assert h.end()["outcome"] == "convicted"
    assert len(runner.watches) == 1
    assert h.fake.closes == []
    (keep,) = h.ev("close")
    assert keep["action"] == "keep_all" and keep["rung"] == 2
    assert runner.ended == ["t2a"]


def test_s10_rejected_commit_and_other_outcomes_start_no_group_watch() -> None:
    runner = Runner(stuck("c9"))
    h = _escalated_commit(runner)
    h.fake.commit_result = (False, "native_pick_mismatch")
    h.run()
    assert h.end()["outcome"] == "commit_rejected" and h.end()["commit"] == "rejected"
    assert runner.watches == [] and h.fake.closes == []


def test_s10_group_rung1_accused_failure_is_deferred_without_a_watch() -> None:
    members = ("TIER1-X-SELECT", "TIER1-Y-SELECT")
    single = (rung(0, "TIER1-AUTO", "TIER1-X-SELECT", 3, members, url=URL_T1, timeout_ms=800),)
    runner = Runner(stuck("c1"))
    h = Harness(
        single,
        {
            ("TIER1-AUTO", "TIER1-X-SELECT"): instant(FAIL),
            ("TIER1-AUTO", "TIER1-Y-SELECT"): instant(PASS),
        },
        runner=runner,
        chain0=("FINAL-EXIT", "PROXY-FALLBACK", "TIER1-SELECT", "TIER1-AUTO", "TIER1-X-SELECT", "x1"),
        leaf="x1",
    )
    h.fake.groups["TIER1-AUTO"] = (
        members,
        "TIER1-Y-SELECT",
        {"TIER1-X-SELECT": False, "TIER1-Y-SELECT": True},
        frozenset(),
    )
    h.run()
    (deferred,) = h.ev("demotion_deferred")
    assert deferred["grp"] == "T1" and deferred["acc"] == ttok("TIER1-X-SELECT") and deferred["probe"] == "transport_fail"
    assert not h.ev("demotion") and not h.ev("demotion_not_visible")
    assert h.job.ladder.accused_failed_known == {0: True}
    # Group rung ①: legacy ``commit`` record, then the watch after the commit.
    (commit,) = h.ev("commit")
    assert commit["accepted"] is True and commit["rung"] == 1
    assert len(runner.watches) == 1 and h.service._watches["x1"].ladder.mode == "group"
    assert h.seq(lambda r: r.get("ev") == "demotion_deferred")[0] < h.seq(lambda r: r.get("ev") == "commit")[0]


# ---------------------------------------------------------------------------
# S11 per-phase queues, off-path guard


def test_s11_a_late_rung1_result_is_never_applied_to_rung2(scaled) -> None:
    issued_wall: list[float] = []

    def late_ac() -> tuple[float, ProbeClass, float]:
        issued_wall.append(time.time())
        return (0.9, PASS, 0.0)  # arrives while rung ②'s witness runs

    script = {
        ("TIER2-AUTO", "t2a"): late_ac,
        ("TIER2-AUTO", "t2b"): instant(TIMEOUT),
        ("PROXY-FALLBACK", "TIER3-AUTO"): (0.4, PASS, 0.0),
        ("PROXY-FALLBACK", "TIER2-AUTO"): instant(FAIL),
    }
    rungs = (t2_rung(timeout_ms=100), pf_rung(timeout_ms=100))
    h = Harness(rungs, script, constants=scaled)
    h.fake.escalate_alive[1] = {"TIER1-SELECT": False, "TIER2-AUTO": True, "TIER3-AUTO": True}

    class Entries(dict):
        def get(self, key, default=None):  # Mihomo wrote t2a dead after the issue
            if key != "t2a" or not issued_wall:
                return default
            sample = {"time": _rfc3339(issued_wall[0] + 0.05), "delay": 0}
            return {"name": "t2a", "alive": False, "extra": {URL_T2: {"alive": False, "history": [sample]}}}

    h.fake.leaf_entries = Entries()
    h.run()
    (readback,) = h.ev("readback")
    assert readback["accused_timeout"] is True and readback["rung"] == 1
    assert h.ev("rung_verdict")[0]["verdict"] == "FACILITY"
    rung1_results = [r for r in h.ev("probe_result")]
    assert [(r["name"], r["class"]) for r in rung1_results] == [("S_C0", "timeout")]
    assert h.end()["outcome"] == "convicted" and h.ev("rung_commit")[0]["rung"] == 2
    assert [(r["name"], r["class"]) for r in h.ev("rung_probe_result")] == [("A_C", "transport_fail")]
    assert all(item["url"] == URL_T2 for item in h.probes(rung=0))
    assert all(item["url"] == URL_PF for item in h.probes(rung=1))
    assert "t2a" not in h.service._status_failures


def test_s11_off_path_probe_is_refused_without_a_probe() -> None:
    h = Harness(two_rungs(), {})
    phase = judge_module._LadderPhase(h.rungs[0])
    action = StartProbe(
        name="S_C9",
        leaf="other",
        timeout_ms=800,
        request_timeout_s=1.45,
        url="http://elsewhere.test/",
        expected="204",
        group="TIER9-AUTO",
        admission_s=1.9,
        await_s=2.8,
    )
    h.service._ladder_issue(h.job, phase, action)
    assert h.fake.probes == [] and h.fake.checks == []
    (refused,) = h.ev("probe_refused")
    assert refused == {"ev": "probe_refused", "t": refused["t"], "ladder": "D0123456789a", "name": "S_C9", "reason": "off_path"}
    name, outcome = phase.events.get_nowait()
    assert name == "S_C9" and outcome.probe is INDET
    assert h.job.ladder.on_path("PROXY-FALLBACK") and not h.job.ladder.on_path(None)


# ---------------------------------------------------------------------------
# S12 legacy stream


def _legacy_records(with_ladder_hooks: bool) -> tuple[list[tuple[str, tuple[str, ...]]], Fake, PostDialJudgeService]:
    clock = Clock()
    fake = Fake(clock, {("legacy", "t2a"): instant(FAIL), ("legacy", "t2b"): instant(PASS)})
    fake.groups["TIER2-AUTO"] = (T2_MEMBERS, "t2a", {name: True for name in T2_MEMBERS}, frozenset())
    records: list[dict[str, Any]] = []
    service = PostDialJudgeService(
        hooks=fake.hooks(ladder_hooks=with_ladder_hooks),
        tokenizer=TOKENIZER,
        emit=lambda level, record: records.append(dict(record)),
        clock=clock,
    )
    fake.service = service
    job = PostDialJudgeJob(
        leaf="t2a",
        kind="FAST",
        tokens=("aaaaaaaaaaaaaaaa",),
        episode=1,
        test_url=URL_T2,
        expected_status="204",
        ticket_deadline=clock() + 6.0,
        runner=Runner(),
        context=None,
    )
    assert job.ladder is None
    assert service.start(job)
    seen_in_flight = service.ladder_in_flight()
    assert service.wait_idle(10.0)
    assert not seen_in_flight
    shape = [(record["ev"], tuple(sorted(record))) for record in records]
    return shape, fake, service


def test_s12_no_ladder_is_the_legacy_judgement() -> None:
    plain, plain_fake, _ = _legacy_records(False)
    hooked, hooked_fake, service = _legacy_records(True)
    assert plain == hooked
    assert [ev for ev, _keys in plain] == [
        "probe_start",
        "probe_result",
        "probe_start",
        "demotion_not_visible",
        "probe_result",
        "verdict",
        "commit",
    ]
    for _ev, keys in hooked:
        assert not {"ladder", "rung", "rverdict", "pick", "proven"} & set(keys)
    assert not {ev for ev, _keys in hooked} & _LADDER_EVS
    assert hooked_fake.checks == [] and hooked_fake.escalations == [] and hooked_fake.ends == []
    # Legacy probes carry none of the ladder fields.
    assert all(item["url"] is None and item["admission_s"] is None and item["await_s"] is None for item in hooked_fake.probes)
    assert service.metrics.get("ladders", 0) == 0


# ---------------------------------------------------------------------------
# S13 caps


def test_s13_rung_cap_stops_the_escalation() -> None:
    h = Harness(two_rungs(), exhausted_t2_script())
    with_exhausted_rung1(h)
    key = tl.rung_gate_key("PROXY-FALLBACK", "TIER2-AUTO")
    for _ in range(3):
        h.service.gates.note_rung(key, h.clock())
    h.run()
    assert h.fake.escalations == []
    end = h.end()
    assert end["outcome"] == "ladder_cap" and end["hold"] == "EXHAUSTED"


def test_s13_one_ladder_in_flight_and_global_cap_counts_rungs_not_witnesses() -> None:
    gate = threading.Event()
    script = exhausted_t2_script()
    script[("TIER2-AUTO", "t2a")] = lambda: (gate.wait(5.0), instant(FAIL))[1]
    script[("PROXY-FALLBACK", "TIER1-SELECT")] = instant(FAIL)
    script[("PROXY-FALLBACK", "TIER3-AUTO")] = instant(PASS)
    script[("PROXY-FALLBACK", "TIER2-AUTO")] = instant(FAIL)
    h = Harness(two_rungs(), script)
    with_exhausted_rung1(h)
    h.fake.escalate_alive[1] = {"TIER1-SELECT": True, "TIER2-AUTO": True, "TIER3-AUTO": True}
    assert h.service.start(h.job)
    assert h.service.ladder_in_flight()
    other = PostDialJudgeJob(
        leaf="t2b",
        kind="FAST",
        tokens=(),
        episode=1,
        test_url=URL_T2,
        expected_status="204",
        ticket_deadline=h.deadline,
        runner=None,
        context=None,
    )
    assert not h.service.start(other)
    gate.set()
    assert h.service.wait_idle(10.0)
    assert not h.service.ladder_in_flight()
    end = h.end()
    assert end["outcome"] == "convicted" and end["witnesses"] == 2
    assert len(h.service.gates._judgements) == 2  # begin(L0) + one escalated rung
    assert len(h.service.gates._checks[tl.rung_gate_key("PROXY-FALLBACK", "TIER2-AUTO")]) == 1


# ---------------------------------------------------------------------------
# S14 await bound and admission


def test_s14_every_ladder_probe_awaits_issue_plus_t_plus_2(scaled) -> None:
    script = exhausted_t2_script()
    # The witness never answers within its bound (0.1 + 0.5 s scaled).
    script[("PROXY-FALLBACK", "TIER3-AUTO")] = (1.5, PASS, 0.0)
    rungs = (t2_rung(timeout_ms=100), pf_rung(timeout_ms=100))
    h = Harness(rungs, script, constants=scaled)
    with_exhausted_rung1(h)
    h.fake.escalate_alive[1] = {"TIER1-SELECT": False, "TIER2-AUTO": True, "TIER3-AUTO": True}
    started = time.monotonic()
    assert h.service.start(h.job)
    while not h.ev("ladder_end") and time.monotonic() - started < 5.0:
        time.sleep(0.005)
    took = time.monotonic() - started
    assert h.service.wait_idle(5.0)
    end = h.end()
    assert end["outcome"] == "indeterminate" and end["hold"] is None
    assert h.ev("ladder_witness")[0]["class"] == "indeterminate"
    assert took < 1.3  # gave up at issue + 0.6 s, not at the probe's 1.5 s answer
    assert all(item["await_s"] == 0.6 and item["admission_s"] == 1.9 for item in h.fake.probes)


def test_s14_real_windows_and_legacy_constants_untouched() -> None:
    script = exhausted_t2_script()
    script[("PROXY-FALLBACK", "TIER3-AUTO")] = instant(PASS)
    script[("PROXY-FALLBACK", "TIER2-AUTO")] = instant(FAIL)
    h = Harness(two_rungs(), script)
    with_exhausted_rung1(h)
    h.fake.escalate_alive[1] = {"TIER1-SELECT": False, "TIER2-AUTO": True, "TIER3-AUTO": True}
    h.run()
    for item in h.fake.probes:
        timeout_s = item["timeout_ms"] / 1000.0
        assert item["await_s"] == pytest.approx(timeout_s + 2.0)
        assert item["admission_s"] == 1.9
    assert judge_module.PROBE_QUEUE_S == 1.5 and judge_module.PROBE_SLACK_S == 0.5
    assert len(h.fake.checks) >= 5  # A_C, S_C0, W0, A_C, commit


# ---------------------------------------------------------------------------
# S15 witness cap


def _tier1_ladder(script: dict[tuple[str, str], Any]) -> Harness:
    x_members = ("x1", "x2")
    t1_members = ("TIER1-X-SELECT", "TIER1-Y1-SELECT", "TIER1-Y2-SELECT", "TIER1-Y3-SELECT", "TIER1-Y4-SELECT")
    rungs = (
        rung(0, "TIER1-X-AUTO", "x1", 5, x_members, url=URL_T1, timeout_ms=800),
        rung(1, "TIER1-AUTO", "TIER1-X-SELECT", 3, t1_members, url=URL_T1, timeout_ms=800),
    )
    full = {("TIER1-X-AUTO", "x1"): instant(FAIL), ("TIER1-X-AUTO", "x2"): instant(FAIL)}
    full.update(script)
    h = Harness(
        rungs,
        full,
        chain0=("FINAL-EXIT", "PROXY-FALLBACK", "TIER1-SELECT", "TIER1-AUTO", "TIER1-X-SELECT", "TIER1-X-AUTO", "x1"),
    )
    h.fake.escalate_alive[1] = {name: True for name in t1_members}
    return h


def test_s15_fourth_witness_is_probed_while_f3_fits() -> None:
    script = {
        ("TIER1-AUTO", "TIER1-Y1-SELECT"): instant(FAIL),
        ("TIER1-AUTO", "TIER1-Y2-SELECT"): instant(FAIL),
        ("TIER1-AUTO", "TIER1-Y3-SELECT"): instant(FAIL),
        ("TIER1-AUTO", "TIER1-Y4-SELECT"): instant(PASS),
        ("TIER1-AUTO", "TIER1-X-SELECT"): instant(FAIL),
    }
    h = _tier1_ladder(script)
    h.run()
    assert [item["leaf"] for item in h.probes(rung=1)] == [
        "TIER1-Y1-SELECT",
        "TIER1-Y2-SELECT",
        "TIER1-Y3-SELECT",
        "TIER1-Y4-SELECT",
        "TIER1-X-SELECT",
    ]
    (commit,) = h.ev("rung_commit")
    assert commit["grp"] == "T1" and commit["pick"] == ttok("TIER1-Y4-SELECT") and commit["proven"] == 1
    assert h.end()["witnesses"] == 4 and h.end()["pf_probe"] == 0
    assert (h.ev("ladder_admit")[0]["path"]) == ["FE", "PF", "T1S", "T1", "T1XS", "T1X", "L"]


def test_s15_f3_refusal_never_reaches_the_untested_member() -> None:
    # Refresh 8 s, then 2.5 s per witness (inside the 2.8 s bound): after W2
    # 15.5 + 6.1 > 20, so Y4 is never probed and no A_C is issued.
    script = {
        ("TIER1-AUTO", "TIER1-Y1-SELECT"): instant(FAIL, advance=2.5),
        ("TIER1-AUTO", "TIER1-Y2-SELECT"): instant(FAIL, advance=2.5),
        ("TIER1-AUTO", "TIER1-Y3-SELECT"): instant(FAIL, advance=2.5),
    }
    h = _tier1_ladder(script)
    h.fake.escalate_advance[1] = 8.0
    h.run()
    assert [item["name"] for item in h.probes(rung=1)] == ["W0", "W1", "W2"]
    end = h.end()
    assert end["outcome"] == "ladder_budget_exhausted" and end["hold"] == "EXHAUSTED"
    assert h.ev("rung_verdict")[-1]["verdict"] == "EXHAUSTED_PARTIAL"


def test_s15_untested_alive_member_is_witness_incomplete_without_an_ac(monkeypatch) -> None:
    monkeypatch.setattr(judge_module, "witness_cap", lambda rung: 2)
    script = {
        ("TIER1-AUTO", "TIER1-Y1-SELECT"): instant(FAIL),
        ("TIER1-AUTO", "TIER1-Y2-SELECT"): instant(TIMEOUT),
    }
    h = _tier1_ladder(script)
    h.run()
    assert [item["name"] for item in h.probes(rung=1)] == ["W0", "W1"]
    end = h.end()
    assert end["outcome"] == "witness_incomplete" and end["hold"] == "EXHAUSTED"
    assert not h.probes(rung=1, name="A_C")


# ---------------------------------------------------------------------------
# PostDialLadder surface for the runtime hooks


def test_ladder_view_shares_state_and_reads_baseline_payload() -> None:
    rungs = two_rungs()
    ladder = PostDialLadder(rungs=rungs, authority=None, deadline=20.0, ladder_id="D1", chain0=CHAIN_T2)
    assert ladder.current is rungs[0] and "TIER2-AUTO" not in repr(ladder)
    view = ladder.view(1)
    assert view.current is rungs[1] and ladder.current is rungs[0]
    with view.lock:
        view.drop_consumed[1] = True
    assert ladder.drop_consumed == {1: True} and view.lock is ladder.lock
    assert ladder.rung_state(1) == (None, frozenset())
    # The baseline is ``(binding, snapshot, payload)`` with the STORED route
    # publication as payload: {"proxies": <GET /proxies response>,
    # "proxy_map": ...} (desktop_runtime publication; 10-30: never the bare
    # response, which hid a one-level unwrap).
    controller_response = {
        "proxies": {
            "TIER1-SELECT": {"name": "TIER1-SELECT", "type": "Selector", "alive": False},
            "TIER2-AUTO": {"name": "TIER2-AUTO", "type": "Fallback", "extra": {URL_PF: {"alive": True}}},
        }
    }
    payload = {"proxies": controller_response, "proxy_map": {}}
    ladder.baselines[1] = ("binding", "snapshot", payload)
    alive, unknown = ladder.rung_state(1)
    assert alive == {"TIER1-SELECT": False, "TIER2-AUTO": True, "TIER3-AUTO": False}
    assert unknown == frozenset({"TIER3-AUTO"})
    ladder.set_rung_state(1, alive={"TIER3-AUTO": True}, unknown=("TIER1-SELECT",))
    assert ladder.rung_state(1) == ({"TIER3-AUTO": True}, frozenset({"TIER1-SELECT"}))
    ladder.note_commit(pick="TIER3-AUTO", proven=True, sacrificial=True)
    assert (ladder.commit_pick, ladder.commit_proven, ladder.commit_sacrificial) == ("TIER3-AUTO", True, True)
    assert judge_module._outcome_text("d4_txn_busy:lease_releasing") == "d4_txn_busy_lease_releasing"
    assert PostDialJudgeJob(
        leaf="x", kind="FAST", tokens=(), episode=0, test_url="u", expected_status="204",
        ticket_deadline=1.0, runner=None, context=None,
    ).ladder is None


# ---------------------------------------------------------------------------
# 10-28 ship2 fix 2: rung 1 accused failed + S_C0 timeout -> EXHAUSTED hold


@pytest.mark.parametrize("accused", [FAIL, TIMEOUT])
def test_ship2_fix2_rung1_successor_timeout_enters_exhausted_hold(accused: ProbeClass) -> None:
    script = {("TIER2-AUTO", "t2a"): instant(accused), ("TIER2-AUTO", "t2b"): instant(TIMEOUT)}
    h = Harness(two_rungs(), script)
    h.service.successor_timeout_hold = True
    h.run()
    end = h.end()
    verdict = h.ev("rung_verdict")[0]
    if accused is TIMEOUT:
        # accused + successor timeout is FACILITY (unchanged): escalation path.
        assert verdict["verdict"] == "FACILITY"
        assert h.ev("hold_entered") == []
        return
    assert verdict["verdict"] == "INDETERMINATE" and verdict["reason"] == "successor_timeout"
    assert end["outcome"] == "exhausted_hold" and end["hold"] == "EXHAUSTED"
    assert h.fake.escalations == [] and not h.fake.commits
    held = h.ev("hold_entered")
    assert len(held) == 1 and held[0]["cause"] == "successor_timeout"
    assert h.service.gates.hold is Verdict.EXHAUSTED
    # no 10 s back-off: the hold (released by an answer) gates instead
    assert h.service.gates._backoff.get("t2a") is None
    assert h.service.blocked_cheap("t2b", h.clock()) == "exhausted_hold"
    assert h.service.metrics["exhausted_hold_entered"] == 1


def test_ship2_fix2_other_indeterminate_rung1_keeps_the_10s_backoff() -> None:
    script = {("TIER2-AUTO", "t2a"): instant(FAIL), ("TIER2-AUTO", "t2b"): instant(INDET)}
    h = Harness(two_rungs(), script)
    h.service.successor_timeout_hold = True
    h.run()
    end = h.end()
    assert end["outcome"] == "indeterminate" and end["hold"] is None
    assert h.ev("hold_entered") == []
    assert h.service.gates.hold is None
    assert h.service.gates._backoff["t2a"] - h.clock() <= 10.0


def test_ship2_fix3_strong_fast_trigger_reopens_rung1_once_inside_the_backoff() -> None:
    from types import SimpleNamespace

    h = Harness(two_rungs(), {("TIER2-AUTO", "t2a"): instant(PASS), ("TIER2-AUTO", "t2b"): instant(PASS)})
    h.service.backoff_override_on = True
    h.run()
    assert h.end()["outcome"] == "not_reproduced"
    now = h.clock()
    strong = SimpleNamespace(kind="FAST", share_failed=5, share_answered=1)
    weak = SimpleNamespace(kind="FAST", share_failed=2, share_answered=2)
    assert h.service.blocked_cheap("t2a", now, weak) == GATE_LEAF_BACKOFF
    assert h.service.blocked_cheap("t2a", now, strong) is None
    # The override judgement itself NOT_REPRODUCED again: the new 420 s
    # window has its override spent.
    h.records.clear()
    h.run()
    assert h.ev("backoff_override") and h.ev("ladder_end")[-1]["outcome"] == "not_reproduced"
    assert h.service.blocked_cheap("t2a", h.clock(), strong) == GATE_LEAF_BACKOFF


def test_ship2_fix2_key_off_keeps_the_indeterminate_10s_backoff() -> None:
    """``exhausted_hold`` off (the default): rung 1 A_C failed + S_C0 timeout
    stays INDETERMINATE with the 10 s back-off, as before."""

    script = {("TIER2-AUTO", "t2a"): instant(FAIL), ("TIER2-AUTO", "t2b"): instant(TIMEOUT)}
    h = Harness(two_rungs(), script)
    assert h.service.successor_timeout_hold is False
    h.run()
    end = h.end()
    assert end["outcome"] == "indeterminate" and end["hold"] is None
    assert h.ev("hold_entered") == [] and h.service.gates.hold is None
    assert h.service.gates._backoff["t2a"] - h.clock() <= 10.0
