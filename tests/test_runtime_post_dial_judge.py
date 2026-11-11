"""Post-dial judge (node-judgement design v3 P2-2, P2-3; §4.1, §5, §6, §18.6-18.7).

Two layers:

- ``PostDialJudgeService`` with fake hooks and time-scaled constants (0.1x):
  the PF1/PF4/PF6/PF7/PF7b flows, the gates, holds, exoneration and the
  demotion-watch close decisions, on the real worker/probe threads.
- The ``DesktopRuntime`` glue against a fake Mihomo controller: gates on the
  real armed/D4/route state, the recovery ticket beside an open D4 ticket,
  the 2-slot broker probe, ``native_commit`` through a real route-snapshot
  refresh, and exact-id closes through the real observer thread.

Every test asserts that P2 writes nothing to Mihomo but exact-id DELETEs of
stuck connections: no PUT, pin, reload, provider write or group DELETE.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import inspect
import logging
import re
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from desktop_runtime import DesktopRuntime, _PostDialJudgeContext, _RuntimeRecoveryTicket
from mihomo_api import (
    MihomoAPIError,
    MihomoDelayStatusMismatch,
    MihomoDelayTimeout,
    MihomoDelayTransportFailed,
)
from runtime_egress_recovery import (
    BoundRuntimeFailure,
    RuntimeEgressReadiness,
    RuntimeEgressReadinessState,
    RuntimeRecoveryStatus,
)
from runtime_node_judge import Done, ProbeClass, Verdict
from runtime_post_dial import (
    DemotionWatch,
    PostDialTokenizer,
    SnapshotDigest,
    StuckConnections,
    Trigger,
    constants_with,
)
import runtime_post_dial_judge as judge_module
from runtime_post_dial_judge import (
    CLOSE_THREAD_NAME,
    JUDGE_THREAD_NAME,
    PROBE_THREAD_NAME,
    PostDialJudgeHooks,
    PostDialJudgeJob,
    PostDialJudgeService,
    ProbeOutcome,
    RouteRead,
    latest_contract_sample_wall,
    route_read_from,
)
from runtime_post_dial_observer import OBSERVER_THREAD_NAME
from runtime_probe_broker import ProbePriority


def _load_replay():
    path = Path(__file__).resolve().parent / "fixtures" / "post_dial" / "replay.py"
    spec = importlib.util.spec_from_file_location("post_dial_replay_judge", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


replay = _load_replay()

URL = "http://judge-contract.test/generate_204"
TOKENIZER = PostDialTokenizer(bytes(range(32)))
# 0.1x of §18.2: 5000 ms native timeout -> 500 ms, S_C0 at +0.2 s, 6 s ticket
# -> 0.6 s, evidence deadline 0.05 s before it, 10 s back-off -> 1 s.
SCALED = constants_with(
    native_timeout_ms=500,
    probe_request_timeout_s=0.6,
    s_c0_delay_s=0.2,
    native_tail_s=0.05,
    min_probe_remaining_s=0.1,
    leaf_backoff_s=1.0,
)
TICKET_S = 0.6
_JUDGE_THREADS = {JUDGE_THREAD_NAME, PROBE_THREAD_NAME, CLOSE_THREAD_NAME, OBSERVER_THREAD_NAME}
_HEX12 = re.compile(r"^[0-9a-f]{12}$")


@pytest.fixture(autouse=True)
def _no_judge_thread_leak():
    before = {thread for thread in threading.enumerate() if thread.name in _JUDGE_THREADS}
    yield
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        leaked = [
            thread
            for thread in threading.enumerate()
            if thread.name in _JUDGE_THREADS and thread.is_alive() and thread not in before
        ]
        if not leaked:
            break
        leaked[0].join(0.05)
    assert not leaked, [thread.name for thread in leaked]


def _wait_for(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


# ---------------------------------------------------------------------------
# Layer 1: the service with fake hooks


class _Route:
    """Mihomo's view of TIER3-AUTO for the fake hooks (S3 + S4)."""

    def __init__(self, members=("n1", "n2", "n3")) -> None:
        self.members = list(members)
        self.alive = {name: True for name in members}
        self.fixed = ""
        self.reads = 0
        self.fail = False
        self.lock = threading.Lock()

    def now(self) -> str:
        return next((name for name in self.members if self.alive[name]), self.members[0])

    def demote(self, leaf: str) -> None:
        with self.lock:
            self.alive[leaf] = False

    def revive(self, leaf: str) -> None:
        with self.lock:
            self.alive[leaf] = True

    def read(self) -> RouteRead | None:
        with self.lock:
            self.reads += 1
            if self.fail:
                return None
            return RouteRead(
                members=(*self.members, "REJECT"),
                now=self.now(),
                fixed=self.fixed,
                alive=dict(self.alive),
                read_at=time.monotonic(),
            )


class _Hooks:
    def __init__(self, route: _Route, script: dict[str, tuple[float, ProbeClass, Any]], *, close_enabled: bool = True) -> None:
        self.route = route
        self.script = script
        self.close_enabled_value = close_enabled
        self.probes: list[dict[str, Any]] = []
        self.commits: list[Any] = []
        self.finished: list[str] = []
        self.closes: list[tuple[str, tuple[str, ...]]] = []
        self.leaf_entries: dict[str, dict[str, Any]] = {}
        self.leaf_reads: list[str] = []
        self.commit_result: tuple[bool, str] = (True, "committed")
        self.moved: list[tuple[str, bool]] = []
        # What the close hook reports closed; None means every asked id.
        self.close_result: Callable[[tuple[str, ...]], Any] | None = None

    def probe(self, job: PostDialJudgeJob, action: Any) -> ProbeOutcome:
        record = {
            "name": action.name,
            "leaf": action.leaf,
            "timeout_ms": action.timeout_ms,
            "request_timeout_s": action.request_timeout_s,
            "started": time.monotonic(),
        }
        self.probes.append(record)
        delay, result, effect = self.script[action.leaf]
        time.sleep(delay)
        if effect is not None:
            # Mihomo writes the sample before it replies.
            effect()
        record["ended"] = time.monotonic()
        return ProbeOutcome(result, elapsed_ms=delay * 1000.0, queue_wait_ms=0.0)

    def read_leaf(self, job: PostDialJudgeJob, leaf: str) -> dict[str, Any] | None:
        self.leaf_reads.append(leaf)
        return self.leaf_entries.get(leaf)

    def commit(self, job: PostDialJudgeJob, done: Any) -> tuple[bool, str]:
        self.commits.append(done)
        return self.commit_result

    def finish(self, job: PostDialJudgeJob, reason: str) -> None:
        self.finished.append(reason)

    def close(self, job: PostDialJudgeJob, leaf: str, ids: tuple[str, ...]) -> Any:
        self.closes.append((leaf, tuple(ids)))
        if self.close_result is not None:
            return self.close_result(tuple(ids))
        return tuple(ids)

    def route_moved(self, job: PostDialJudgeJob) -> None:
        # (leaf, whether act had finished: the commit/finish hooks ran first)
        self.moved.append((job.leaf, bool(self.commits or self.finished)))

    def hooks(self) -> PostDialJudgeHooks:
        return PostDialJudgeHooks(
            probe=self.probe,
            read_route=lambda job: self.route.read(),
            read_leaf=self.read_leaf,
            commit=self.commit,
            finish=self.finish,
            close=self.close,
            close_enabled=lambda: self.close_enabled_value,
            route_moved=self.route_moved,
        )


class _Runner:
    """The observer runner's demotion-watch surface."""

    def __init__(self, stuck: StuckConnections | None = None) -> None:
        self.stuck = stuck
        self.watches: list[tuple[str, float]] = []
        self.ended: list[str] = []
        self.marked: list[tuple[str, ...]] = []

    def start_demotion_watch(self, leaf: str, *, t_demote_wall: float) -> DemotionWatch:
        self.watches.append((leaf, t_demote_wall))
        return DemotionWatch(leaf=leaf, t_demote_wall=t_demote_wall, until_perf=time.perf_counter() + 60.0)

    def end_demotion_watch(self, leaf: str) -> None:
        self.ended.append(leaf)

    def stuck_now(self, leaf: str, *, t_demote_wall: float, max_age_s: float) -> StuckConnections | None:
        return self.stuck

    def mark_closed(self, ids: Any) -> None:
        self.marked.append(tuple(ids))


def _stuck(*ids: str, aged: int | None = None) -> StuckConnections:
    count = len(ids) if aged is None else aged
    return StuckConnections(
        ids=tuple(ids),
        aged=count,
        waiting=len(ids) - count,
        kept_progressing=1,
        kept_not_stuck=1,
        kept_after_demotion=0,
        kept_other=0,
        snapshot_wall=time.time(),
    )


class _Records:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any]]] = []
        self.lock = threading.Lock()

    def __call__(self, level: str, record: Any) -> None:
        with self.lock:
            self.items.append((level, dict(record)))

    def events(self, name: str) -> list[dict[str, Any]]:
        with self.lock:
            return [record for _level, record in self.items if record.get("ev") == name]


def _service(hooks: _Hooks, records: _Records | None = None) -> PostDialJudgeService:
    return PostDialJudgeService(
        hooks=hooks.hooks(),
        tokenizer=TOKENIZER,
        emit=records if records is not None else (lambda _level, _record: None),
        constants=SCALED,
    )


def _job(
    runner: Any,
    *,
    leaf: str = "n1",
    ticket_s: float = TICKET_S,
    kind: str = "FAST",
    episode: int = 1,
    durable: bool = False,
) -> PostDialJudgeJob:
    return PostDialJudgeJob(
        leaf=leaf,
        kind=kind,
        tokens=("tok-a", "tok-b"),
        episode=episode,
        test_url=URL,
        expected_status="204",
        ticket_deadline=time.monotonic() + ticket_s,
        runner=runner,
        context=None,
        durable=durable,
    )


def _run(service: PostDialJudgeService, job: PostDialJudgeJob, timeout: float = 5.0) -> None:
    assert service.start(job)
    assert service.wait_idle(timeout)


def test_pf1_fast_zombie_moves_natively_and_commits_after_the_successor_passes() -> None:
    route = _Route()
    hooks = _Hooks(
        route,
        {
            "n1": (0.01, ProbeClass.TRANSPORT_FAIL, lambda: route.demote("n1")),
            "n2": (0.15, ProbeClass.PASS, None),
        },
    )
    records = _Records()
    service = _service(hooks, records)
    runner = _Runner()

    _run(service, _job(runner))

    assert [(probe["name"], probe["leaf"]) for probe in hooks.probes] == [("A_C", "n1"), ("S_C0", "n2")]
    # S_C0 starts when A_C fails, not at the +2 s (scaled +0.2 s) timer.
    assert hooks.probes[1]["started"] - hooks.probes[0]["ended"] < 0.1
    assert all(probe["timeout_ms"] == 500 and probe["request_timeout_s"] == 0.6 for probe in hooks.probes)
    assert len(hooks.commits) == 1 and hooks.commits[0].successor == "n2"
    assert hooks.finished == []
    # Mihomo's demotion was read back and watched; nothing was stuck to close.
    assert [event["ev"] for event in records.events("demotion")] == ["demotion"]
    assert [leaf for leaf, _t in runner.watches] == ["n1"]
    assert hooks.closes == []
    verdict = records.events("verdict")
    assert len(verdict) == 1 and verdict[0]["verdict"] == "CONVICT_CONTRACT"
    assert "all_loaded_dead" not in verdict[0]  # only hold verdicts carry it
    assert records.events("commit")[0]["accepted"] is True
    # route_moved once, after the commit (which refreshes the route itself).
    assert hooks.moved == [("n1", True)]
    # A conviction sets no back-off (§18.7) and no hold; the judgement is over.
    assert service.blocked_cheap("n1", time.monotonic()) is None
    assert not service.judging()


@pytest.mark.parametrize("durable", [False, True])
def test_verdict_and_commit_lines_carry_the_judgements_durable_setting(durable: bool) -> None:
    route = _Route()
    hooks = _Hooks(
        route,
        {
            "n1": (0.01, ProbeClass.TRANSPORT_FAIL, lambda: route.demote("n1")),
            "n2": (0.05, ProbeClass.PASS, None),
        },
    )
    records = _Records()
    service = _service(hooks, records)
    job = _job(_Runner(), durable=durable)
    assert job.durable is durable
    assert _job(_Runner()).durable is False  # the default is move-only

    _run(service, job)

    expected = "on" if durable else "off"
    (verdict,) = records.events("verdict")
    keys = list(verdict)
    assert keys[keys.index("commit_allowed") + 1] == "durable"
    assert verdict["durable"] == expected and verdict["commit_allowed"] is True
    (commit,) = records.events("commit")
    assert list(commit) == ["ev", "t", "leaf", "successor", "accepted", "reason", "durable"]
    assert commit["durable"] == expected and commit["reason"] == "committed"
    # The service itself behaves the same in both modes: the commit hook
    # branches on ``job.durable``.
    assert len(hooks.commits) == 1 and hooks.finished == []


@pytest.mark.parametrize("durable", [False, True])
def test_late_conviction_commit_line_carries_durable(durable: bool) -> None:
    hooks = _Hooks(_Route(), {})
    records = _Records()
    service = _service(hooks, records)
    job = _job(_Runner(), durable=durable)
    done = Done(
        verdict=Verdict.CONVICT_CONTRACT,
        reason="successor_passed",
        leaf="n1",
        successor="n2",
        successor_index=0,
        results=(("A_C", "transport_fail"), ("S_C0", "pass")),
        commit_allowed=False,
        tokens=job.tokens,
        kind=job.kind,
    )

    service._act_on(job, done)

    (commit,) = records.events("commit")
    assert commit["reason"] == "commit_late" and commit["accepted"] is False
    assert list(commit)[-2:] == ["reason", "durable"]
    assert commit["durable"] == ("on" if durable else "off")
    assert hooks.commits == [] and hooks.finished == ["commit_late"]


def test_pf4_black_hole_convicts_inside_the_ticket_and_closes_only_stuck_ids() -> None:
    route = _Route()
    hooks = _Hooks(
        route,
        {
            # A_C answers last: 504 at the (scaled) native 5 s timeout.
            "n1": (0.5, ProbeClass.TIMEOUT, lambda: route.demote("n1")),
            "n2": (0.15, ProbeClass.PASS, None),
        },
    )
    records = _Records()
    service = _service(hooks, records)
    runner = _Runner(stuck=_stuck("aged-1", "aged-2", "waiting-1", aged=2))

    _run(service, _job(runner))

    started = {probe["name"]: probe["started"] for probe in hooks.probes}
    # S_C0 started at +0.2 s while A_C was still pending.
    assert 0.15 <= started["S_C0"] - started["A_C"] <= 0.35
    assert len(hooks.commits) == 1 and hooks.commits[0].successor == "n2"
    assert hooks.commits[0].commit_allowed is True
    # Closed at the move: the successor had already passed, L is demoted.
    assert hooks.closes == [("n1", ("aged-1", "aged-2", "waiting-1"))]
    assert runner.marked == [("aged-1", "aged-2", "waiting-1")]
    close = records.events("close")
    assert close[0]["action"] == "close" and close[0]["aged"] == 2 and close[0]["waiting"] == 1


def test_black_hole_answer_after_the_ticket_loses_only_the_conviction() -> None:
    # V4 risk: the A_C reply misses the ticket.  The deadline readback finds
    # no newer sample, the verdict is INDETERMINATE, but Mihomo's own write
    # still moved the traffic: the late demotion is watched and stuck
    # connections are still closed.
    route = _Route()
    hooks = _Hooks(
        route,
        {
            "n1": (0.5, ProbeClass.TIMEOUT, lambda: route.demote("n1")),
            # Passes ~0.22 s in, well before the 0.40 s evidence deadline.
            "n2": (0.02, ProbeClass.PASS, None),
        },
    )
    hooks.leaf_entries["n1"] = {"name": "n1", "alive": True, "extra": {URL: {"alive": True, "history": []}}}
    records = _Records()
    service = _service(hooks, records)
    runner = _Runner(stuck=_stuck("aged-1"))

    _run(service, _job(runner, ticket_s=0.45))

    assert hooks.leaf_reads == ["n1"]
    assert records.events("verdict")[0]["verdict"] == "INDETERMINATE"
    assert hooks.commits == []
    assert hooks.finished == ["indeterminate"]
    demotion = records.events("demotion")
    assert len(demotion) == 1 and demotion[0]["late"] is True
    assert hooks.closes == [("n1", ("aged-1",))]
    # The late demotion (after act) still reports the move once.
    assert hooks.moved == [("n1", True)]


def test_deadline_readback_resolves_an_unanswered_accused_as_a_timeout() -> None:
    route = _Route()
    issued_after = time.time()
    hooks = _Hooks(
        route,
        {
            "n1": (0.6, ProbeClass.TIMEOUT, lambda: route.demote("n1")),
            "n2": (0.02, ProbeClass.PASS, None),
        },
    )
    # Mihomo wrote L dead (a history sample newer than the A_C issue time)
    # before the controller reply reached the judge (§5.3).
    route.demote("n1")
    hooks.leaf_entries["n1"] = {
        "name": "n1",
        "alive": False,
        "extra": {URL: {"alive": False, "history": [{"time": replay.rfc3339(issued_after + 0.3), "delay": 0}]}},
    }
    records = _Records()
    service = _service(hooks, records)

    _run(service, _job(_Runner(), ticket_s=0.45))

    assert records.events("readback")[0]["accused_timeout"] is True
    verdict = records.events("verdict")[0]
    assert verdict["verdict"] == "CONVICT_CONTRACT" and verdict["results"]["A_C"] == "timeout"
    assert len(hooks.commits) == 1
    # The real reply that arrives later does not report the demotion twice.
    assert len(records.events("demotion")) == 1


def test_pf6_site_down_everywhere_is_not_reproduced_and_exonerates_its_tokens() -> None:
    route = _Route()
    hooks = _Hooks(route, {"n1": (0.02, ProbeClass.PASS, None)})
    records = _Records()
    service = _service(hooks, records)
    runner = _Runner(stuck=_stuck("x"))

    _run(service, _job(runner))

    assert [probe["name"] for probe in hooks.probes] == ["A_C"]
    assert hooks.commits == [] and hooks.closes == [] and runner.watches == []
    assert hooks.finished == ["not_reproduced"]
    assert hooks.moved == []  # no demotion was read back
    assert service.is_exonerated("n1", "tok-a") and service.is_exonerated("n1", "tok-b")
    assert not service.is_exonerated("n2", "tok-a")
    now = time.monotonic()
    assert service.blocked_cheap("n1", now) == "leaf_backoff"
    assert service.blocked_cheap("n2", now) is None


def test_pf7_facility_keeps_every_connection_and_holds_until_a_later_answer() -> None:
    # Upstream stall: A_C and S_C0 both 504.  S_C0 resolves after the verdict
    # (INDETERMINATE at the evidence deadline), which then becomes the late
    # FACILITY hold (H1 fix 4).  A successor timed out, so nothing is closed.
    route = _Route()
    hooks = _Hooks(
        route,
        {
            "n1": (0.5, ProbeClass.TIMEOUT, lambda: route.demote("n1")),
            "n2": (0.5, ProbeClass.TIMEOUT, lambda: route.demote("n2")),
        },
    )
    records = _Records()
    service = _service(hooks, records)
    runner = _Runner(stuck=_stuck("aged-1", "aged-2"))

    _run(service, _job(runner))

    assert records.events("verdict")[0]["verdict"] == "INDETERMINATE"
    assert [event["hold"] for event in records.events("late_hold")] == ["FACILITY"]
    # §18.7 evidence: n3 was still Core-alive at the hold.
    assert records.events("late_hold")[0]["all_loaded_dead"] is False
    assert service.gates.hold is not None and service.gates.hold.value == "FACILITY"
    assert service.blocked_cheap("n3", time.monotonic()) == "facility_hold"
    assert hooks.commits == [] and hooks.closes == []
    # The watch is on; its next poll sees the successor timeout and keeps all.
    watch = DemotionWatch(leaf="n1", t_demote_wall=runner.watches[0][1], until_perf=time.perf_counter() + 60)
    service.on_watch_step(runner, watch, _stuck("aged-1", "aged-2"))
    assert hooks.closes == []
    assert runner.ended == ["n1"]
    assert records.events("close")[-1]["action"] == "keep_all"

    # Released only by an answered TIER3 user connection seen entirely after
    # the hold was set.
    hold_set = time.time()
    answered = SnapshotDigest(True, time.time(), True, 0.001, 3, 0, True, False)
    service.on_snapshot(answered, previous_poll_wall=hold_set - 5.0)
    assert service.gates.hold is not None
    # An accepted poll during the stall with no answered TIER3 user
    # connection never releases it (no facility cascade, §13).
    unanswered = SnapshotDigest(True, time.time(), True, 0.001, 3, 0, False, False)
    service.on_snapshot(unanswered, previous_poll_wall=time.time())
    assert service.gates.hold is not None
    assert records.events("hold_released") == []
    service.on_snapshot(answered, previous_poll_wall=time.time())
    assert service.gates.hold is None
    assert records.events("hold_released")[0]["hold"] == "FACILITY"


def test_pf7b_every_member_failing_fast_is_exhausted_and_holds() -> None:
    route = _Route()
    hooks = _Hooks(
        route,
        {
            "n1": (0.01, ProbeClass.TRANSPORT_FAIL, lambda: route.demote("n1")),
            "n2": (0.01, ProbeClass.TRANSPORT_FAIL, lambda: route.demote("n2")),
        },
    )
    records = _Records()
    service = _service(hooks, records)

    _run(service, _job(_Runner()))

    # Three members: the successor cap is min(3, max(1, (3 - 1) // 2)) = 1.
    assert [probe["leaf"] for probe in hooks.probes] == ["n1", "n2"]
    assert records.events("verdict")[0]["verdict"] == "EXHAUSTED"
    # §18.7 evidence for the hold: n3 is loaded and still Core-alive.
    assert records.events("verdict")[0]["all_loaded_dead"] is False
    assert hooks.commits == [] and hooks.closes == []
    assert hooks.finished == ["exhausted"]
    assert service.blocked_cheap("n3", time.monotonic()) == "exhausted_hold"


def test_exhausted_hold_logs_that_every_loaded_member_is_dead() -> None:
    route = _Route(members=("n1", "n2"))
    hooks = _Hooks(
        route,
        {
            "n1": (0.01, ProbeClass.TRANSPORT_FAIL, lambda: route.demote("n1")),
            "n2": (0.01, ProbeClass.TRANSPORT_FAIL, lambda: route.demote("n2")),
        },
    )
    records = _Records()
    service = _service(hooks, records)

    _run(service, _job(_Runner()))

    verdict = records.events("verdict")
    assert len(verdict) == 1 and verdict[0]["verdict"] == "EXHAUSTED"
    assert verdict[0]["all_loaded_dead"] is True
    assert hooks.finished == ["exhausted"]


def test_one_judgement_at_a_time_and_the_worker_keeps_the_gate_for_late_results() -> None:
    route = _Route()
    hooks = _Hooks(
        route,
        {
            "n1": (0.5, ProbeClass.TIMEOUT, lambda: route.demote("n1")),
            "n2": (0.5, ProbeClass.TIMEOUT, None),
        },
    )
    service = _service(hooks)
    assert service.start(_job(_Runner()))
    assert not service.start(_job(_Runner(), leaf="n3"))
    assert service.blocked_cheap("n3", time.monotonic()) == "judging"
    # The verdict is out at the evidence deadline, but S_C0 is still in
    # flight: its broker slot is held and no second judgement starts.
    assert _wait_for(lambda: bool(hooks.finished), timeout=2.0)
    assert service.blocked_cheap("n3", time.monotonic()) == "judging"
    assert service.wait_idle(3.0)
    assert service.blocked_cheap("n3", time.monotonic()) == "facility_hold"


def test_gated_is_logged_once_per_episode_and_reason() -> None:
    records = _Records()
    service = _service(_Hooks(_Route(), {}), records)
    trigger = SimpleNamespace(kind="FAST", leaf="n1", episode=1)
    for _ in range(5):
        service.note_gated(trigger, "not_armed")
    service.note_gated(trigger, "d4_busy")
    service.note_gated(SimpleNamespace(kind="FAST", leaf="n1", episode=2), "d4_busy")
    assert [(event["reason"]) for event in records.events("gated")] == ["not_armed", "d4_busy", "d4_busy"]
    assert all(_HEX12.match(event["leaf"]) for event in records.events("gated"))


def test_status_answer_on_the_accused_skips_its_next_check_until_mihomo_samples_again() -> None:
    route = _Route()
    hooks = _Hooks(route, {"n1": (0.01, ProbeClass.HTTP_ANSWER, lambda: route.demote("n1"))})
    records = _Records()
    service = _service(hooks, records)

    _run(service, _job(_Runner()))

    assert records.events("verdict")[0]["verdict"] == "NOT_REPRODUCED"
    recorded = service.status_failure_at("n1")
    assert recorded is not None
    stale = {"alive": False, "extra": {URL: {"alive": False, "history": [{"time": replay.rfc3339(recorded - 30), "delay": 0}]}}}
    assert service.latest_sample_is_status_failure("n1", stale, URL)
    newer = {"alive": False, "extra": {URL: {"alive": False, "history": [{"time": replay.rfc3339(recorded + 5), "delay": 0}]}}}
    assert not service.latest_sample_is_status_failure("n1", newer, URL)
    assert service.status_failure_at("n1") is None


def test_watch_steps_close_after_the_successor_passes_and_skip_while_l_is_revived() -> None:
    route = _Route()
    hooks = _Hooks(
        route,
        {
            "n1": (0.01, ProbeClass.TRANSPORT_FAIL, lambda: route.demote("n1")),
            "n2": (0.15, ProbeClass.PASS, None),
        },
    )
    records = _Records()
    service = _service(hooks, records)
    runner = _Runner(stuck=_stuck("aged-1"))
    assert service.start(_job(runner))
    # At the move the successor probe is pending: the inline close defers.
    assert _wait_for(lambda: bool(runner.watches))
    watch = DemotionWatch(leaf="n1", t_demote_wall=runner.watches[0][1], until_perf=time.perf_counter() + 60)
    service.on_watch_step(runner, watch, _stuck("aged-1"))
    assert hooks.closes == []
    assert service.wait_idle(3.0)
    # The next poll after S_C0 passed: one readback (≤1 s old), then close.
    reads = route.reads
    service.on_watch_step(runner, watch, _stuck("aged-2"))
    assert service.wait_idle(2.0)
    assert hooks.closes == [("n1", ("aged-2",))]
    assert route.reads == reads + 1
    # Another runner's watch step (an older lease) is ignored.
    service.on_watch_step(_Runner(), watch, _stuck("aged-3"))
    # Mihomo revived L (a passing tick): this batch closes nothing, but the
    # watch goes on (§18.7: a failed confirm skips only that batch).
    route.revive("n1")
    service.on_watch_step(runner, watch, _stuck("aged-4"))
    assert service.wait_idle(2.0)
    service.on_watch_step(runner, watch, _stuck("aged-4"))
    assert service.wait_idle(2.0)
    assert hooks.closes == [("n1", ("aged-2",))]
    assert runner.ended == []
    not_demoted = [
        level for level, event in records.items if event.get("ev") == "close" and event["action"] == "not_demoted"
    ]
    assert not_demoted == ["info", "debug"]  # info once per watch, then debug
    # L is demoted again inside the watch: the next step closes again.
    route.demote("n1")
    service.on_watch_step(runner, watch, _stuck("aged-5"))
    assert service.wait_idle(2.0)
    assert hooks.closes == [("n1", ("aged-2",)), ("n1", ("aged-5",))]
    assert runner.ended == []


def test_closes_off_starts_no_watch_and_closes_nothing() -> None:
    route = _Route()
    hooks = _Hooks(
        route,
        {
            "n1": (0.01, ProbeClass.TRANSPORT_FAIL, lambda: route.demote("n1")),
            "n2": (0.05, ProbeClass.PASS, None),
        },
        close_enabled=False,
    )
    runner = _Runner(stuck=_stuck("aged-1"))
    service = _service(hooks)

    _run(service, _job(runner))

    assert len(hooks.commits) == 1
    assert runner.watches == [] and hooks.closes == []


def test_only_ids_whose_close_succeeded_are_marked_and_the_rest_are_retried() -> None:
    route = _Route()
    hooks = _Hooks(
        route,
        {
            "n1": (0.01, ProbeClass.TRANSPORT_FAIL, lambda: route.demote("n1")),
            "n2": (0.05, ProbeClass.PASS, None),
        },
    )
    records = _Records()
    service = _service(hooks, records)
    runner = _Runner()
    _run(service, _job(runner))
    watch = DemotionWatch(leaf="n1", t_demote_wall=runner.watches[0][1], until_perf=time.perf_counter() + 60)

    # One DELETE of the batch failed: only the closed id is marked.
    hooks.close_result = lambda ids: ids[:1]
    service.on_watch_step(runner, watch, _stuck("aged-1", "aged-2"))
    assert service.wait_idle(2.0)
    assert runner.marked == [("aged-1",)]
    close = records.events("close")[-1]
    assert close["action"] == "close" and close["count"] == 1 and close["reason"] == "partially_closed"
    # Nothing closed (the cleanup's GET /connections failed): nothing marked,
    # the watch goes on ...
    hooks.close_result = lambda ids: ()
    service.on_watch_step(runner, watch, _stuck("aged-2"))
    assert service.wait_idle(2.0)
    assert runner.marked == [("aged-1",)]
    assert records.events("close")[-1]["action"] == "close_failed"
    assert runner.ended == []
    # ... and its next step retries the still-stuck id.
    hooks.close_result = None
    service.on_watch_step(runner, watch, _stuck("aged-2"))
    assert service.wait_idle(2.0)
    assert runner.marked == [("aged-1",), ("aged-2",)]
    assert hooks.closes == [("n1", ("aged-1", "aged-2")), ("n1", ("aged-2",)), ("n1", ("aged-2",))]


def test_route_read_marks_unread_members_unknown_never_dead() -> None:
    group = {"all": [f"m{index}" for index in range(12)], "now": "m1", "testUrl": URL}
    reads: list[str] = []

    def read_member(name: str) -> dict[str, Any]:
        reads.append(name)
        if name == "m3":
            raise MihomoAPIError("controller read timed out")
        return {"name": name, "alive": name != "m0"}

    # No provider read: the accused leaf past the cap is read first.
    route = route_read_from(group, None, test_url=URL, read_member=read_member, read_at=1.0, priority=("m10",))
    assert route is not None
    assert reads == ["m10", *[f"m{index}" for index in range(7)]]
    assert route.unknown == frozenset({"m3", "m7", "m8", "m9", "m11"})
    assert route.demoted("m10") is False  # read alive
    assert route.demoted("m0") is True  # read dead, now is m1
    # Never confirmed while its state is unknown (a failed read, past the cap).
    assert route.demoted("m3") is False and route.demoted("m9") is False
    # Unknown members are tried after every member read alive or dead.
    assert route.judge_order == (
        "m0", "m1", "m2", "m4", "m5", "m6", "m10", "m3", "m7", "m8", "m9", "m11",
    )
    assert route.order == tuple(f"m{index}" for index in range(12))
    assert route.all_loaded_dead() is False
    everything_dead = RouteRead(members=("a", "b"), now="a", fixed="", alive={"a": False}, unknown=frozenset({"b"}))
    assert everything_dead.all_loaded_dead() is None
    assert RouteRead(members=("a", "b"), now="a", fixed="", alive={"a": False, "b": False}).all_loaded_dead() is True


def test_route_read_builds_alive_from_the_provider_and_reads_prefix_members() -> None:
    provider = {
        "name": "TIER3B-RUNTIME",
        "testUrl": "",
        "expectedStatus": "*",
        "proxies": [
            {"name": "n2", "alive": True, "extra": {URL: {"alive": False}}},
            {"name": "n3", "alive": False},
        ],
    }
    group = {"all": ["p0", "n2", "n3", "REJECT"], "now": "n3", "fixed": "", "testUrl": URL}
    reads: list[str] = []

    def read_member(name: str) -> dict[str, Any]:
        reads.append(name)
        return {"name": name, "alive": True}

    route = route_read_from(group, provider, test_url=URL, read_member=read_member, read_at=1.0)
    assert route is not None
    assert route.order == ("p0", "n2", "n3")
    # The per-contract entry wins over the global alive (§18.6).
    assert route.alive == {"p0": True, "n2": False, "n3": False}
    assert reads == ["p0"]
    assert route.demoted("n2") and not route.demoted("p0")
    assert not route.demoted("stranger")
    # Another contract (a reload) is not this judgement's group.
    assert route_read_from(dict(group, testUrl="http://other/"), provider, test_url=URL, read_member=read_member, read_at=1.0) is None
    # Without the provider every member is read, up to the cap.
    reads.clear()
    many = {"all": [f"m{index}" for index in range(12)], "now": "m0", "testUrl": URL}
    route = route_read_from(many, None, test_url=URL, read_member=read_member, read_at=1.0, max_prefix_reads=8)
    assert reads == [f"m{index}" for index in range(8)]
    assert route.alive.get("m9") is None and not route.demoted("m0")


def test_latest_contract_sample_wall_reads_mihomo_history() -> None:
    now = time.time()
    proxy = {"extra": {URL: {"history": [{"time": replay.rfc3339(now - 2)}, {"time": replay.rfc3339(now - 1)}]}}}
    assert abs(latest_contract_sample_wall(proxy, URL) - (now - 1)) < 0.01
    assert latest_contract_sample_wall({"extra": {}}, URL) is None


# ---------------------------------------------------------------------------
# Layer 2: the DesktopRuntime glue against a fake Mihomo controller


PID = 5150
PROCESS_GENERATION = 3
CORE_GENERATION = 5
LEAVES = ("n1", "n2", "n3")
_FORBIDDEN = frozenset(
    {
        "select_proxy",
        "select_proxy_with_readback",
        "unfix_proxy",
        "fix_proxy",
        "reload_config",
        "patch_config",
        "update_proxy_provider",
        "close_all_connections",
        "group_delay",
        "providers_proxies",
        "provider_healthcheck",
    }
)


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self, prefix: str = "Post-dial observer") -> list[str]:
        return [record.getMessage() for record in self.records if record.getMessage().startswith(prefix)]

    def events(self, name: str) -> list[str]:
        return self.messages(f"Post-dial observer {name} ")


class _FakeController:
    """Mihomo's controller as P2 uses it: fallback semantics, no sockets.

    A failed URL test writes the leaf dead before it replies, so the group's
    ``now`` moves to the next alive member (the native move).  Every write
    other than an exact-id connection DELETE fails the test.
    """

    def __init__(self, leaves=LEAVES, *, connections: Any = None) -> None:
        self.lock = threading.RLock()
        self.leaves = list(leaves)
        self.alive = {name: True for name in leaves}
        self.samples: dict[str, list[dict[str, Any]]] = {name: [] for name in leaves}
        self.modes = {name: "ok" for name in leaves}
        self.fixed = ""
        self.calls: list[tuple[Any, ...]] = []
        self.delay_calls: list[dict[str, Any]] = []
        self.writes: list[str] = []
        self.closed: list[str] = []
        self.connections_source = connections
        self.before_reply: dict[str, Callable[[], None]] = {}

    # -- Mihomo semantics
    def sample(self, name: str, alive: bool) -> None:
        with self.lock:
            self.alive[name] = alive
            self.samples[name].append({"time": replay.rfc3339(time.time()), "delay": 42 if alive else 0})

    def _now(self) -> str:
        if self.fixed:
            if self.alive.get(self.fixed):
                return self.fixed
            self.fixed = ""  # marshalling the group clears a dead pick (Conf-1)
        return next((name for name in self.leaves if self.alive[name]), self.leaves[0])

    def _leaf(self, name: str) -> dict[str, Any]:
        alive = self.alive[name]
        history = [dict(item) for item in self.samples[name]]
        return {"name": name, "type": "Http", "alive": alive, "history": history, "extra": {URL: {"alive": alive, "history": history}}}

    def _group(self) -> dict[str, Any]:
        return {
            "name": "TIER3-AUTO",
            "type": "Fallback",
            "all": [*self.leaves, "REJECT"],
            "now": self._now(),
            "fixed": self.fixed,
            "testUrl": URL,
            "expectedStatus": "204",
        }

    # -- reads
    def proxies(self, *, timeout: float | None = None) -> dict[str, Any]:
        with self.lock:
            self.calls.append(("proxies",))
            entries = {
                "GENERAL": {"type": "Selector", "all": ["FINAL-EXIT"], "now": "FINAL-EXIT"},
                "FINAL-EXIT": {"type": "Selector", "all": ["PROXY-FALLBACK", "REJECT"], "now": "PROXY-FALLBACK"},
                "PROXY-FALLBACK": {"type": "Fallback", "all": ["TIER3-AUTO"], "now": "TIER3-AUTO", "testUrl": URL, "expectedStatus": "204"},
                "TIER3-AUTO": self._group(),
                "REJECT": {"name": "REJECT", "type": "Reject"},
                **{name: self._leaf(name) for name in self.leaves},
            }
            return {"proxies": entries}

    def proxy(self, name: str, *, timeout: float | None = None) -> dict[str, Any]:
        with self.lock:
            self.calls.append(("proxy", name))
            if name == "TIER3-AUTO":
                return self._group()
            if name in self.alive:
                return self._leaf(name)
        raise MihomoAPIError("controller HTTP 404")

    def provider_proxies_one(self, name: str, *, timeout: float | None = None) -> dict[str, Any]:
        with self.lock:
            self.calls.append(("provider", name))
            return {"name": name, "testUrl": "", "expectedStatus": "*", "proxies": [self._leaf(leaf) for leaf in self.leaves]}

    def proxy_delay(
        self,
        name: str,
        *,
        url: str,
        timeout_ms: int = 5000,
        expected_status: Any = None,
        request_timeout: float | None = None,
        connection_abort_sink: Any = None,
        verify_expected_status: bool = False,
    ) -> dict[str, Any]:
        aborted = threading.Event()
        if connection_abort_sink is not None:
            connection_abort_sink(aborted.set)
        call = {
            "name": name,
            "url": url,
            "timeout_ms": timeout_ms,
            "expected_status": expected_status,
            "request_timeout": request_timeout,
            "verify": verify_expected_status,
            "thread": threading.current_thread().name,
            "at": time.monotonic(),
        }
        with self.lock:
            self.delay_calls.append(call)
            mode = self.modes.get(name, "ok")
            hook = self.before_reply.get(name)
        if hook is not None:
            hook()
        if mode == "zombie":
            self.sample(name, False)
            raise MihomoDelayTransportFailed("controller HTTP 503 from delay test")
        if mode == "blackhole":
            aborted.wait(min(timeout_ms / 1000.0, float(request_timeout or 30.0)))
            self.sample(name, False)
            raise MihomoDelayTimeout("controller HTTP 504 Timeout")
        if mode == "sampled_blackhole":
            # The dead sample is in Mihomo's history before its 504 reply
            # reaches the judge (§5.3 deadline readback).
            self.sample(name, False)
            aborted.wait(min(timeout_ms / 1000.0, float(request_timeout or 30.0)))
            raise MihomoDelayTimeout("controller HTTP 504 Timeout")
        if mode == "answer":
            self.sample(name, False)
            raise MihomoDelayStatusMismatch("proxy URL test did not satisfy its expected status")
        self.sample(name, True)
        return {"delay": 42}

    def connections(self, *, timeout: float | None = None) -> dict[str, Any]:
        source = self.connections_source
        return source.snapshot(time.time()) if source is not None else {"connections": []}

    def close_connection(self, connection_id: str, *, timeout: float | None = None) -> dict[str, Any]:
        with self.lock:
            self.closed.append(str(connection_id))
        source = self.connections_source
        if source is not None:
            source.close(str(connection_id), time.time())
        return {}

    def __getattr__(self, name: str) -> Any:
        if name in _FORBIDDEN:
            def forbidden(*_args: Any, **_kwargs: Any) -> Any:
                self.writes.append(name)
                raise AssertionError(f"P2 must not call {name}")

            return forbidden
        raise AttributeError(name)


class _KeepAlive:
    """``MihomoControllerKeepAlive`` stand-in for the observer thread."""

    def __init__(self, controller: _FakeController) -> None:
        self.controller = controller
        self.calls = 0

    def connections(self, *, timeout_seconds: float) -> dict[str, Any]:
        self.calls += 1
        return self.controller.connections()

    def close(self) -> None:
        pass


class _FakeManager:
    effective_egress_ok = True

    def __init__(self, runtime_generation: int) -> None:
        self.runtime_generation = runtime_generation
        self.accept = True
        self.observations: list[dict[str, Any]] = []
        self.rechecks: list[tuple[Any, dict[str, Any]]] = []

    def apply_runtime_health_observation(self, observation: dict[str, Any]) -> dict[str, Any]:
        self.observations.append(dict(observation))
        return {"accepted": self.accept, "changed": self.accept, "action": "quarantined" if self.accept else "stale"}

    def enqueue_runtime_leaf_recheck(self, node: Any, **kwargs: Any) -> None:
        self.rechecks.append((node, dict(kwargs)))


# ``durable_conviction: true``: a conviction also records the durable judgement
# of L (health overlay, recheck), as before move-only became the default.
DURABLE = {"durable_conviction": True}


class _Harness:
    def __init__(
        self,
        temp_path: Path,
        *,
        flags: dict[str, Any] | None = None,
        constants: Any = None,
        connections: Any = None,
    ) -> None:
        # The keys flipped on by default on 10-28 are pinned to their legacy
        # (pre-ship) values here: this harness tests each key as an opt-in.
        node_judgement = {
            "post_dial_judge": "act",
            "rearm_on_stale_route": False,
            "tier_ladder_judge": False,
            "overshoot_repair": False,
            # 11-01: the ship2 fix and seamless keys default on; pinned off too.
            "exhausted_pool_reexport": False,
            "exhausted_hold": False,
            "not_reproduced_override": False,
            "post_dial_provisional_move": False,
            "post_dial_fast_successor": False,
            "post_dial_successor_from_history": False,
            "post_dial_close_frozen_streams": False,
            **(flags or {}),
        }
        config = {
            "client": {
                "runtime_state_path": str(temp_path / "runtime-state.json"),
                "egress_recovery": {"node_judgement": node_judgement, "route_lease": {"enabled": False}},
            },
            "alerts": {"log_file": str(temp_path / "monitor.log"), "desktop": {"enabled": False}},
        }
        runtime = DesktopRuntime(config, project_root=temp_path)
        for handler in list(runtime.logger.handlers):
            handler.close()
            runtime.logger.removeHandler(handler)
        logger = logging.getLogger(f"test.post-dial-judge.{id(runtime)}")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        self.logs = _ListHandler()
        logger.handlers[:] = [self.logs]
        runtime.logger = logger
        self.runtime = runtime
        self.controller = _FakeController(connections=connections)
        self.keepalive = _KeepAlive(self.controller)
        runtime.api_client = self.controller
        runtime.core_runner = SimpleNamespace(
            is_running=lambda: True,
            status=lambda: {"pid": PID, "process_generation": PROCESS_GENERATION},
        )
        runtime._core_generation = CORE_GENERATION
        runtime._runtime_loaded_export_contract = {
            "revision": 1,
            "semantic_digest": "post-dial-judge-test",
            "proxy_map": {
                name: {
                    "tier": "tier3",
                    "pool_kind": "tier3b",
                    "identity": f"identity-{name}",
                    "proxy": {"name": name, "type": "http"},
                }
                for name in LEAVES
            },
        }
        self.manager = _FakeManager(int(runtime._runtime_generation))
        runtime.manager = self.manager
        runtime._runtime_egress_readiness = RuntimeEgressReadiness(
            state=RuntimeEgressReadinessState.READY,
            validation_active=False,
        )
        runtime._runtime_initial_activation_generation = CORE_GENERATION
        runtime._core_runtime_phase = "formal_capture"
        runtime._runtime_post_dial_controller = lambda: self.keepalive
        if constants is not None:
            state = runtime._node_judgement_state()
            default = runtime._runtime_post_dial_judge()
            state.judge = PostDialJudgeService(
                hooks=default._hooks,
                tokenizer=state.tokenizer,
                emit=runtime._log_runtime_post_dial_record,
                constants=constants,
                clock=runtime._runtime_recovery_now,
            )
        assert runtime._refresh_runtime_route_snapshot(timeout_seconds=1.0) is not None
        assert runtime._try_arm_runtime_recovery()
        assert runtime._runtime_recovery_is_armed()
        self.published: list[Any] = []

    @property
    def judge(self) -> PostDialJudgeService:
        return self.runtime._runtime_post_dial_judge()

    def spy_readiness(self) -> None:
        runtime = self.runtime
        runtime._publish_runtime_egress_readiness = lambda *args, **kwargs: self.published.append(("publish", args, kwargs))
        runtime._set_effective_egress_state = lambda *args, **kwargs: self.published.append(("effective", args, kwargs))
        runtime._set_effective_egress_readiness_state = lambda *args, **kwargs: self.published.append(("readiness", args, kwargs))

    def binding(self) -> Any:
        snapshot, payload = self.runtime._runtime_route_snapshot_state()
        return self.runtime._runtime_route_binding_for_outbound(snapshot, payload, "FINAL-EXIT")

    def trigger(self, leaf: str = "n1", *, episode: int = 1, new_episode: bool = True, kind: str = "FAST") -> None:
        now = time.time()
        trigger = Trigger(
            kind=kind,
            leaf=leaf,
            tokens=("tok-a", "tok-b"),
            t=now,
            n_events=2,
            first_event_t=now - 0.05,
            episode=episode,
            new_episode=new_episode,
        )
        mode = self.runtime._runtime_post_dial_mode()[0]
        self.runtime._on_runtime_post_dial_trigger(
            trigger,
            mode=mode,
            tokenizer=self.runtime._node_judgement_state().tokenizer,
        )

    def settle(self, timeout: float = 8.0) -> None:
        assert self.judge.wait_idle(timeout)

    def close(self) -> None:
        self.judge.wait_idle(8.0)
        self.runtime._stop_runtime_post_dial_observer("test", join_timeout=2.0)
        self.runtime.stop_event.set()

    def judge_contexts(self) -> list[str]:
        return [incident for incident in self.runtime._runtime_recovery_tickets if incident.startswith("post-dial-")]


@pytest.fixture
def harness_factory():
    made: list[_Harness] = []
    temps: list[tempfile.TemporaryDirectory] = []

    def make(**kwargs: Any) -> _Harness:
        temp = tempfile.TemporaryDirectory()
        temps.append(temp)
        harness = _Harness(Path(temp.name), **kwargs)
        made.append(harness)
        return harness

    yield make
    for harness in made:
        harness.close()
    for temp in temps:
        temp.cleanup()


def _no_raw_names(messages: list[str]) -> None:
    assert messages
    for message in messages:
        # Leaf names only as keyed hashes; no host, URL or identity at all.
        for name in LEAVES:
            assert not re.search(rf"(?<![0-9a-z]){re.escape(name)}(?![0-9a-z])", message), message
        for text in (".test", "identity-", "http:", "127.0.0.1"):
            assert text not in message, message


def test_runtime_pf1_moves_without_a_put_and_commits_after_a_real_refresh(harness_factory) -> None:
    harness = harness_factory(flags=DURABLE)
    harness.spy_readiness()
    effects = _spy_durable_effects(harness)
    controller = harness.controller
    outcomes = _spy_recovery_outcomes(harness)
    controller.modes["n1"] = "zombie"
    before_refreshes = controller.calls.count(("proxies",))

    harness.trigger()
    harness.settle()

    # A_C on L, then S_C0 on Mihomo's own successor, both on the group's
    # exact contract with the native 5000 ms timeout and a 5.65 s window.
    assert [call["name"] for call in controller.delay_calls] == ["n1", "n2"]
    for call in controller.delay_calls:
        assert call["url"] == URL and call["timeout_ms"] == 5000
        assert call["expected_status"] == "204" and call["verify"] is True
        assert 5.4 < call["request_timeout"] <= 5.65
        # Issued by a 2-slot broker worker on the judge probe thread's behalf.
        assert call["thread"].startswith("runtime-probe-")
    # The native move: no PUT, pin, reload, provider write or group DELETE.
    assert controller.writes == [] and controller.closed == []
    assert controller.fixed == ""
    assert not any(call[0] == "providers" for call in controller.calls)
    # native_commit refreshed the published route once and bound to it.
    assert controller.calls.count(("proxies",)) == before_refreshes + 1
    binding = harness.binding()
    assert binding is not None and binding.node.proxy_name == "n2"
    assert [item["proxy_name"] for item in harness.manager.observations] == ["n1"]
    observation = harness.manager.observations[0]
    assert observation["identity"] == "identity-n1" and observation["verdict"] == "confirmed"
    assert observation["incident_id"].startswith("post-dial-")
    assert [node.proxy_name for node, _kwargs in harness.manager.rechecks] == ["n1"]
    assert effects == {"health": 1, "revoke": ["identity-n1"]}
    (incident,) = harness.judge_contexts()
    outcome = harness.runtime._runtime_recovery_outcomes[incident]
    assert outcome.status is RuntimeRecoveryStatus.RECOVERED
    assert outcome.reason == "post_dial_native_commit" and outcome.to_ref.proxy_name == "n2"
    # Never publishes readiness; recovery is re-armed on the new route.
    assert harness.published == []
    assert harness.runtime._runtime_recovery_is_armed()
    commit = harness.logs.events("commit")
    assert len(commit) == 1 and "accepted=true" in commit[0] and "reason=committed" in commit[0]
    assert "reason=committed durable=on)" in commit[0]
    assert " commit_allowed=true durable=on skipped=" in harness.logs.events("verdict")[0]
    # RECOVERED records no app selector PUT (the native move), in the call and in the terminal log.
    _assert_recovered_without_a_put(harness, outcomes, "post_dial_native_commit")
    _no_raw_names(harness.logs.messages())


def _spy_recovery_outcomes(harness: _Harness) -> list[tuple[Any, str, Any]]:
    """Record (status, reason_code, facts) of every ``_complete_runtime_recovery_outcome`` call."""

    runtime = harness.runtime
    calls: list[tuple[Any, str, Any]] = []
    complete = runtime._complete_runtime_recovery_outcome

    def spy(incident_id: str, status: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append((status, str(kwargs.get("reason_code") or ""), kwargs.get("facts")))
        return complete(incident_id, status, *args, **kwargs)

    runtime._complete_runtime_recovery_outcome = spy
    return calls


def _terminal_recovery_events(harness: _Harness, reason_code: str) -> list[dict[str, Any]]:
    """The logged terminal ``Runtime recovery phase completed`` payloads with ``reason_code``."""

    events = []
    for record in harness.logs.records:
        payload = getattr(record, "runtime_recovery_event", None)
        if (
            record.getMessage() == "Runtime recovery phase completed"
            and isinstance(payload, dict)
            and payload.get("phase") == "terminal"
            and payload.get("reason_code") == reason_code
        ):
            events.append(payload)
    return events


def _assert_recovered_without_a_put(
    harness: _Harness, calls: list[tuple[Any, str, Any]], reason_code: str
) -> None:
    recovered = [call for call in calls if call[0] is RuntimeRecoveryStatus.RECOVERED]
    assert recovered == [
        (
            RuntimeRecoveryStatus.RECOVERED,
            reason_code,
            {"put": False, "readback": True, "final_204": False},
        )
    ]
    (event,) = _terminal_recovery_events(harness, reason_code)
    assert (event["put"], event["readback"], event["final_204"], event["result"]) == (
        False,
        True,
        False,
        "recovered",
    )


def _spy_durable_effects(harness: _Harness) -> dict[str, Any]:
    """Count ``_apply_confirmed_runtime_health`` calls and Tier3 revocations."""

    runtime = harness.runtime
    effects: dict[str, Any] = {"health": 0, "revoke": []}
    health = runtime._apply_confirmed_runtime_health
    revoke = runtime._revoke_tier3_runtime_leaf_qualification

    def spy_health(*args: Any, **kwargs: Any) -> Any:
        effects["health"] += 1
        return health(*args, **kwargs)

    def spy_revoke(identity: str) -> Any:
        effects["revoke"].append(identity)
        return revoke(identity)

    runtime._apply_confirmed_runtime_health = spy_health
    runtime._revoke_tier3_runtime_leaf_qualification = spy_revoke
    return effects


def test_runtime_move_only_conviction_rebinds_the_route_and_records_nothing_durable(harness_factory) -> None:
    # durable_conviction false (the default): the same refresh, rebind and
    # ticket as the durable commit, then RECOVERED (put=False) with no health
    # observation, recheck, flap entry, revocation or incident binding.
    harness = harness_factory()
    harness.spy_readiness()
    effects = _spy_durable_effects(harness)
    outcomes = _spy_recovery_outcomes(harness)
    runtime = harness.runtime
    controller = harness.controller
    controller.modes["n1"] = "zombie"
    before_refreshes = controller.calls.count(("proxies",))
    qualified_before = dict(runtime._tier3_runtime_qualified)
    final_exit_ok_before = runtime._tier3_runtime_final_exit_ok

    harness.trigger()
    harness.settle()

    assert [call["name"] for call in controller.delay_calls] == ["n1", "n2"]
    assert controller.writes == [] and controller.closed == [] and controller.fixed == ""
    assert controller.calls.count(("proxies",)) == before_refreshes + 1
    assert harness.binding().node.proxy_name == "n2"
    assert effects == {"health": 0, "revoke": []}
    assert harness.manager.observations == [] and harness.manager.rechecks == []
    assert runtime._runtime_flap_history == {}
    (incident,) = harness.judge_contexts()
    assert incident not in runtime._runtime_incident_bindings
    assert incident not in runtime._runtime_incident_route_payloads
    assert runtime._tier3_runtime_qualified == qualified_before
    assert runtime._tier3_runtime_final_exit_ok == final_exit_ok_before
    outcome = runtime._runtime_recovery_outcomes[incident]
    assert outcome.status is RuntimeRecoveryStatus.RECOVERED
    assert outcome.reason == "post_dial_native_rebind" and outcome.to_ref.proxy_name == "n2"
    _assert_recovered_without_a_put(harness, outcomes, "post_dial_native_rebind")
    # The ticket was force-rebound with the unchanged 6 s deadline.
    ticket = runtime._runtime_recovery_tickets[incident]
    assert abs((ticket.deadline_monotonic - ticket.observed_at_monotonic) - 6.0) < 0.01
    # Never publishes readiness; recovery is re-armed on the new route.
    assert harness.published == []
    assert runtime._runtime_recovery_is_armed()
    verdict = harness.logs.events("verdict")
    assert len(verdict) == 1 and "verdict=CONVICT_CONTRACT" in verdict[0]
    assert " commit_allowed=true durable=off skipped=" in verdict[0]
    commit = harness.logs.events("commit")
    assert len(commit) == 1 and "accepted=true" in commit[0]
    assert "reason=post_dial_native_rebind durable=off)" in commit[0]
    _no_raw_names(harness.logs.messages())
    assert harness.judge.metrics["judgements"] == 1


@pytest.mark.parametrize(
    "case",
    [
        "facility_hold",
        "terminal",
        "selector",
        "worker_token",
        "core_restart",
        "stale",
        "stale_coordinator_stopped",
        "stale_coordinator_late",
    ],
)
def test_runtime_move_only_keeps_the_health_commit_guards(harness_factory, case: str) -> None:
    # The checks ``_apply_confirmed_runtime_health`` made before its effects
    # still gate the move-only RECOVERED: a failing one is a controller hold.
    # The ``stale`` cases are a route republished between the rebind and the
    # commit: in move-only mode the guard is the only thing that keeps it from
    # settling RECOVERED on a route the judge never bound.  ``selector``,
    # ``worker_token`` and ``core_restart`` happen after the re-registered
    # ticket copied its baseline (so ``authority_changed`` cannot see them):
    # only the guard's selector-revision and worker-token checks catch them.
    harness = harness_factory()
    harness.spy_readiness()
    effects = _spy_durable_effects(harness)
    runtime = harness.runtime
    controller = harness.controller
    controller.modes["n1"] = "zombie"
    guard = runtime._runtime_post_dial_rebind_guard
    coordinator = runtime._runtime_egress_coordinator

    def republished(snapshot: Any) -> Any:
        return dataclasses.replace(snapshot, route_epoch=int(snapshot.route_epoch) + 1)

    def interfere(failure: Any, snapshot: Any, **kwargs: Any) -> Any:
        if case == "facility_hold":
            # A facility hold declared after the rebind check.
            runtime._runtime_uses_early_single_coordinator = lambda: False
            runtime._runtime_facility_hold_reason = lambda: "test_facility_hold"
        elif case == "terminal":
            (incident,) = harness.judge_contexts()
            runtime._complete_runtime_recovery_outcome(
                incident,
                RuntimeRecoveryStatus.CONTROLLER_HOLD,
                reason_code="test_settled_elsewhere",
                expected_ticket=kwargs["expected_ticket"],
            )
        elif case == "selector":
            # A user pin / selector change declared after the ticket rebind.
            runtime._declare_runtime_selector_intent("test user selection")
        elif case == "worker_token":
            # The worker that owns the ticket is no longer current.
            runtime._runtime_worker_token_is_current = lambda token: False
        elif case == "core_restart":
            # A Core restart after the ticket rebind (a stale worker token).
            runtime._core_generation = int(runtime._core_generation) + 1
        elif case == "stale":
            # Another refresh republished the loaded route (first check).
            with runtime._runtime_route_snapshot_lock:
                runtime._runtime_route_snapshot = republished(runtime._runtime_route_snapshot)
        elif case == "stale_coordinator_stopped":
            # Only the first check (under the snapshot lock) sees a stopped
            # coordinator; restored before the monitor-lock check runs.
            coordinator._running = False
            try:
                return guard(failure, snapshot, **kwargs)
            finally:
                coordinator._running = True
        else:
            # The coordinator's route moves after the first check passed, while
            # the monitor lock is held (second check only).
            early = runtime._runtime_uses_early_single_coordinator

            def moved_once() -> Any:
                runtime._runtime_uses_early_single_coordinator = early
                coordinator.set_current_snapshot(republished(coordinator.current_route_snapshot()))
                return early()

            runtime._runtime_uses_early_single_coordinator = moved_once
        return guard(failure, snapshot, **kwargs)

    runtime._runtime_post_dial_rebind_guard = interfere
    harness.trigger()
    harness.settle()

    commit = harness.logs.events("commit")
    assert len(commit) == 1 and "accepted=false" in commit[0], harness.logs.messages()
    if case.startswith("stale"):
        expected_guard = "stale"
    elif case in ("selector", "worker_token", "core_restart"):
        expected_guard = "terminal"
    else:
        expected_guard = case
    assert f"reason=rebind_{expected_guard} durable=off)" in commit[0]
    outcome = _judge_outcome(harness)
    assert outcome.status is RuntimeRecoveryStatus.CONTROLLER_HOLD
    assert outcome.reason == {
        "facility_hold": "post_dial_rebind_facility_hold",
        "terminal": "test_settled_elsewhere",
        "selector": "post_dial_rebind_terminal",
        "worker_token": "post_dial_rebind_terminal",
        "core_restart": "post_dial_rebind_terminal",
    }.get(case, "post_dial_rebind_stale")
    assert effects == {"health": 0, "revoke": []}
    assert harness.manager.observations == [] and harness.manager.rechecks == []
    assert harness.published == [] and controller.writes == []
    assert not _terminal_recovery_events(harness, "post_dial_native_rebind")


def test_runtime_commit_is_rejected_when_the_core_generation_changes(harness_factory) -> None:
    harness = harness_factory()
    harness.spy_readiness()
    controller = harness.controller
    controller.modes["n1"] = "zombie"

    def core_restarted() -> None:
        harness.runtime._core_generation = CORE_GENERATION + 1

    controller.before_reply["n2"] = core_restarted
    harness.trigger()
    harness.settle()

    assert harness.manager.observations == []
    commit = harness.logs.events("commit")
    assert len(commit) == 1 and "accepted=false" in commit[0] and "reason=rebind_rejected" in commit[0]
    # Move-only (the default): the same rejection, then the same hold.
    assert "reason=rebind_rejected durable=off)" in commit[0]
    (incident,) = harness.judge_contexts()
    outcome = harness.runtime._runtime_recovery_outcomes[incident]
    assert outcome.status is RuntimeRecoveryStatus.CONTROLLER_HOLD
    assert outcome.reason == "post_dial_rebind_rejected"
    assert harness.published == []
    assert controller.writes == []


def test_runtime_user_pin_during_the_judgement_rejects_the_commit(harness_factory) -> None:
    harness = harness_factory()
    harness.spy_readiness()
    controller = harness.controller
    controller.modes["n1"] = "zombie"

    def user_pins_n3() -> None:
        harness.runtime._declare_runtime_selector_intent("test user selection")
        with controller.lock:
            controller.fixed = "n3"

    controller.before_reply["n2"] = user_pins_n3
    harness.trigger()
    harness.settle()

    assert harness.manager.observations == []
    assert "reason=rebind_rejected" in harness.logs.events("commit")[0]
    assert harness.published == []


def test_runtime_leaving_act_mode_during_the_judgement_skips_the_commit(harness_factory) -> None:
    harness = harness_factory()
    controller = harness.controller
    controller.modes["n1"] = "zombie"

    def back_to_shadow() -> None:
        harness.runtime.config["client"]["egress_recovery"]["node_judgement"]["post_dial_judge"] = "shadow"

    controller.before_reply["n2"] = back_to_shadow
    harness.trigger()
    harness.settle()

    assert harness.manager.observations == []
    assert "reason=act_mode_off" in harness.logs.events("commit")[0]
    assert controller.calls.count(("proxies",)) == 1  # no refresh for a skipped commit
    (incident,) = harness.judge_contexts()
    assert harness.runtime._runtime_recovery_outcomes[incident].reason == "post_dial_act_mode_off"


@pytest.mark.parametrize("admitted_durable", [False, True])
def test_runtime_commit_branches_on_the_durable_value_read_at_admission(
    harness_factory, admitted_durable: bool
) -> None:
    # The verdict and commit lines print ``job.durable`` (read once at
    # admission) and the gate witness (VJ-0) trusts them; the commit must
    # branch on that same value, not on a config reloaded mid-judgement.
    harness = harness_factory(flags=DURABLE if admitted_durable else None)
    harness.spy_readiness()
    effects = _spy_durable_effects(harness)
    runtime = harness.runtime
    controller = harness.controller
    controller.modes["n1"] = "zombie"

    def reload_flips_durable() -> None:
        runtime.config["client"]["egress_recovery"]["node_judgement"]["durable_conviction"] = not admitted_durable

    controller.before_reply["n2"] = reload_flips_durable
    harness.trigger()
    harness.settle()

    # The live policy did flip before the commit.
    assert bool(runtime._node_judgement_policy().durable_conviction) is (not admitted_durable)
    shown = "on" if admitted_durable else "off"
    verdict = harness.logs.events("verdict")
    assert len(verdict) == 1 and f" commit_allowed=true durable={shown} skipped=" in verdict[0]
    commit = harness.logs.events("commit")
    assert len(commit) == 1 and "accepted=true" in commit[0], harness.logs.messages()
    outcome = _judge_outcome(harness)
    assert outcome.status is RuntimeRecoveryStatus.RECOVERED
    if admitted_durable:
        assert "reason=committed durable=on)" in commit[0]
        assert effects["health"] == 1
        assert outcome.reason == "post_dial_native_commit"
    else:
        assert "reason=post_dial_native_rebind durable=off)" in commit[0]
        assert effects == {"health": 0, "revoke": []}
        assert harness.manager.observations == [] and harness.manager.rechecks == []
        assert outcome.reason == "post_dial_native_rebind"


def test_runtime_user_pin_disarms_and_the_trigger_is_gated(harness_factory) -> None:
    harness = harness_factory()
    harness.runtime._declare_runtime_selector_intent("test user selection")
    assert not harness.runtime._runtime_recovery_is_armed()

    harness.trigger()
    harness.settle()

    assert harness.controller.delay_calls == []
    gated = harness.logs.events("gated")
    assert len(gated) == 1 and "reason=not_armed" in gated[0]
    assert harness.judge_contexts() == []


def test_runtime_d4_on_the_binding_gates_the_judge_and_its_ticket_sits_beside_d4s(harness_factory) -> None:
    harness = harness_factory()
    runtime = harness.runtime
    controller = harness.controller
    controller.modes["n1"] = "zombie"
    binding = harness.binding()
    with runtime._runtime_failure_monitor_lock:
        runtime._runtime_failure_probe_bindings.add(binding.circuit_key)

    harness.trigger(episode=1)
    harness.settle()

    assert controller.delay_calls == []
    assert "reason=d4_busy" in harness.logs.events("gated")[0]

    # D4 finished its probe but its incident ticket is still open.
    with runtime._runtime_failure_monitor_lock:
        runtime._runtime_failure_probe_bindings.discard(binding.circuit_key)
    snapshot, _payload = runtime._runtime_route_snapshot_state()
    d4_failure = BoundRuntimeFailure(
        binding=binding,
        protocol="HTTPS",
        observed_outbound="FINAL-EXIT",
        destination_token="d4-destination",
        error_type="timeout",
        observed_at_monotonic=runtime._runtime_recovery_now(),
    )
    d4_ticket = runtime._ensure_runtime_recovery_ticket(d4_failure, snapshot, "d4-incident")
    assert isinstance(d4_ticket, _RuntimeRecoveryTicket)

    harness.trigger(episode=2)
    harness.settle()

    # The judge ran on its own incident; D4's ticket is untouched and open.
    assert [call["name"] for call in controller.delay_calls] == ["n1", "n2"]
    assert runtime._runtime_recovery_tickets["d4-incident"] is d4_ticket
    assert "d4-incident" not in runtime._runtime_recovery_outcomes
    assert runtime._runtime_recovery_ticket_is_current(d4_ticket, require_open=True)
    (incident,) = harness.judge_contexts()
    assert runtime._runtime_recovery_outcomes[incident].status is RuntimeRecoveryStatus.RECOVERED


def test_runtime_shadow_mode_logs_would_judge_and_makes_no_probe_close_or_ticket(harness_factory) -> None:
    harness = harness_factory(flags={"post_dial_judge": "shadow"})
    controller = harness.controller
    controller.modes["n1"] = "zombie"

    for episode_repeat in range(3):
        harness.trigger(new_episode=episode_repeat == 0)
    harness.settle()

    assert controller.delay_calls == [] and controller.closed == [] and controller.writes == []
    assert harness.judge_contexts() == []
    assert harness.judge.metrics["judgements"] == 0
    would = harness.logs.events("would_judge")
    assert len(would) == 1 and "gate=open" in would[0] and "mode=shadow" in would[0]
    assert harness.logs.events("trigger") == []
    _no_raw_names(harness.logs.messages())


def test_runtime_enabled_false_never_judges(harness_factory) -> None:
    harness = harness_factory(flags={"enabled": False})
    assert harness.runtime.__dict__.get("_runtime_post_dial_observer") is None
    harness.controller.modes["n1"] = "zombie"
    harness.runtime._on_runtime_post_dial_trigger(
        Trigger(kind="FAST", leaf="n1", tokens=("tok-a",), t=time.time(), n_events=2, first_event_t=time.time(), episode=1, new_episode=True),
        mode="act",
        tokenizer=harness.runtime._node_judgement_state().tokenizer,
    )
    harness.settle()
    assert harness.controller.delay_calls == []
    assert harness.judge_contexts() == []
    assert harness.logs.events("gated") == [] and harness.logs.events("would_judge") == []


def test_runtime_stale_published_route_is_gated_refreshed_once_then_judged(harness_factory, monkeypatch) -> None:
    # A re-check interval far longer than the test: only the refresh's new
    # publication can clear the cached route_binding_mismatch.
    monkeypatch.setattr(judge_module, "GATE_RECHECK_S", 30.0)
    harness = harness_factory(flags=DURABLE)
    controller = harness.controller
    # Mihomo moved natively (its own tick wrote n1 dead) after the last
    # refresh: user traffic now fails on n2, the published route says n1.
    controller.sample("n1", False)
    controller.modes["n2"] = "zombie"
    before = controller.calls.count(("proxies",))

    harness.trigger("n2", episode=1)
    assert "reason=route_binding_mismatch" in harness.logs.events("gated")[0]
    assert _wait_for(lambda: controller.calls.count(("proxies",)) == before + 1)
    assert _wait_for(lambda: harness.binding() is not None and harness.binding().node.proxy_name == "n2")
    assert _wait_for(lambda: harness.runtime._runtime_recovery_is_armed())
    # The refresh published a new route: the very next evaluation of the same
    # episode binds to it at once (the cached gate answer belonged to the
    # stale publication), with no second refresh.
    harness.trigger("n2", episode=1, new_episode=False)
    assert harness.judge.metrics["judgements"] == 1
    harness.settle()
    assert [call["name"] for call in controller.delay_calls] == ["n2", "n3"]
    assert [item["proxy_name"] for item in harness.manager.observations] == ["n2"]
    assert len(harness.logs.events("gated")) == 1
    # One refresh for the stale route, one in the commit; none after it.
    time.sleep(0.2)
    assert controller.calls.count(("proxies",)) == before + 2


def test_runtime_cached_gate_holds_until_the_route_is_republished(harness_factory, monkeypatch) -> None:
    monkeypatch.setattr(judge_module, "GATE_RECHECK_S", 30.0)
    harness = harness_factory()
    runtime = harness.runtime
    controller = harness.controller
    controller.modes["n1"] = "zombie"
    d4_key = ("test-d4-probe", "n1")
    with runtime._runtime_failure_monitor_lock:
        runtime._runtime_failure_probe_bindings.add(d4_key)
    harness.trigger("n1", episode=1)
    assert "reason=d4_busy" in harness.logs.events("gated")[-1]
    with runtime._runtime_failure_monitor_lock:
        runtime._runtime_failure_probe_bindings.discard(d4_key)
    # Same publication, inside the re-check interval: the cached answer holds.
    harness.trigger("n1", episode=1, new_episode=False)
    assert harness.judge.metrics["judgements"] == 0
    assert controller.delay_calls == [] and harness.judge_contexts() == []
    # A new publication invalidates it at once.
    assert runtime._refresh_runtime_route_snapshot(timeout_seconds=1.0) is not None
    harness.trigger("n1", episode=1, new_episode=False)
    assert harness.judge.metrics["judgements"] == 1
    harness.settle()
    assert [call["name"] for call in controller.delay_calls] == ["n1", "n2"]


def test_runtime_status_answer_is_not_reproduced_and_gates_the_next_check(harness_factory) -> None:
    harness = harness_factory()
    controller = harness.controller
    controller.modes["n1"] = "answer"

    harness.trigger(episode=1)
    harness.settle()
    assert [call["name"] for call in controller.delay_calls] == ["n1"]
    assert "verdict=NOT_REPRODUCED" in harness.logs.events("verdict")[0]
    # Mihomo demoted L on the status (as its own tick would); the next
    # trigger on L within the back-off is gated, and after it the status
    # sample is still L's newest, so it is skipped (§3.3 gate 8).
    harness.trigger(episode=2)
    assert "reason=leaf_backoff" in harness.logs.events("gated")[-1]
    judge = harness.judge
    with judge._lock:
        judge.gates._backoff.clear()
    harness.trigger(episode=3)
    assert "reason=status_failure_sample" in harness.logs.events("gated")[-1]
    assert len(controller.delay_calls) == 1


def test_runtime_probe_queued_behind_busy_slots_still_gets_the_full_native_window(harness_factory) -> None:
    harness = harness_factory()
    runtime = harness.runtime
    controller = harness.controller
    controller.modes["n1"] = "zombie"
    broker = runtime._runtime_probe_broker
    started = threading.Barrier(3)

    def background(context: Any) -> None:
        context.register_abort_callback(lambda: None)
        context.mark_socket_started()
        started.wait(1.0)
        time.sleep(0.6)

    holders = [
        threading.Thread(target=broker.run, args=(ProbePriority.BACKGROUND, background), kwargs={"queue_timeout_seconds": 1.0}, daemon=True)
        for _ in range(2)
    ]
    for holder in holders:
        holder.start()
    started.wait(1.0)
    harness.trigger()
    harness.settle()
    for holder in holders:
        holder.join(2.0)

    accused = controller.delay_calls[0]
    assert accused["name"] == "n1" and accused["timeout_ms"] == 5000
    assert accused["request_timeout"] > 5.4
    result = [message for message in harness.logs.events("probe_result") if "name=A_C" in message]
    wait = float(re.search(r"queue_wait_ms=([0-9.]+)", result[0]).group(1))
    assert wait >= 300.0


def _stuck_scenario(t0: float, *, successor_mode: str) -> tuple[Any, list[tuple[float, str]]]:
    """Connections on n1 before the demotion, plus a zombie burst that triggers.

    - aged:      upload, never a byte down, for 10 s        -> stuck (AGED)
    - waiting:   answered, then a request unanswered ≥2 s    -> stuck (WAITING)
    - flowing:   downloading all along                       -> kept (progressing)
    - idle:      answered keep-alive, no request outstanding -> kept (not stuck)
    - other:     stuck, but on n2                            -> kept (other leaf)
    """

    mihomo = replay.FakeMihomo()
    old = t0 - 10.0
    mihomo.add(replay.FakeConn(conn_id="aged", t_open=old, sport=41001, host="a.test", port=443, leaf="n1", up=lambda t: 600))
    mihomo.add(
        replay.FakeConn(
            conn_id="waiting",
            t_open=old,
            sport=41002,
            host="w.test",
            port=443,
            leaf="n1",
            up=lambda t: 1000 if t < t0 + 0.6 else 1600,
            down=lambda t: 5000 if t < t0 + 0.3 else 9000,
        )
    )
    mihomo.add(
        replay.FakeConn(
            conn_id="flowing",
            t_open=old,
            sport=41003,
            host="f.test",
            port=443,
            leaf="n1",
            up=lambda t: 900,
            down=lambda t: 10_000 + int(max(0.0, t - old) * 200_000),
        )
    )
    mihomo.add(replay.FakeConn(conn_id="idle", t_open=old, sport=41004, host="i.test", port=443, leaf="n1", up=lambda t: 700, down=lambda t: 4000))
    mihomo.add(replay.FakeConn(conn_id="other", t_open=old, sport=41005, host="o.test", port=443, leaf="n2", up=lambda t: 600))
    lines = []
    for index, sport in enumerate((52001, 52002)):
        t_open = t0 + 0.2 + 0.02 * index
        mihomo.add(
            replay.FakeConn(
                conn_id=f"z{sport}",
                t_open=t_open,
                sport=sport,
                host="zombie.test",
                port=443,
                leaf="n1",
                t_close=t_open + 0.05,
                up=replay.step(t_open + 0.01, 517),
            )
        )
        lines.append((t_open, replay.open_line(t_open, sport=sport, host="zombie.test", leaf="n1")))
    return mihomo, lines


def _feed(runtime: DesktopRuntime, lines: list[tuple[float, str]]) -> None:
    from core_runner import CoreOutputEvent

    for t_line, raw in sorted(lines):
        delay = t_line - time.time()
        if delay > 0:
            time.sleep(delay)
        runtime._on_core_output_event(
            CoreOutputEvent(raw_line=raw, pid=PID, process_generation=PROCESS_GENERATION, received_at_monotonic=time.monotonic())
        )


def _run_stuck_scenario(
    harness_factory,
    *,
    successor_mode: str,
    attempts: int = 3,
    flags: dict[str, Any] | None = None,
) -> _Harness:
    last = None
    for _attempt in range(attempts):
        t0 = time.time() + 0.4
        connections, lines = _stuck_scenario(t0, successor_mode=successor_mode)
        # 0.1x probe timing so a successor timeout takes 0.8 s, not 5 s.
        harness = harness_factory(
            connections=connections,
            constants=constants_with(native_timeout_ms=800, probe_request_timeout_s=0.9),
            flags=flags,
        )
        harness.controller.modes["n1"] = "zombie"
        harness.controller.modes["n2"] = successor_mode
        runner = harness.runtime.__dict__.get("_runtime_post_dial_observer")
        assert runner is not None and runner.is_alive()
        _feed(harness.runtime, lines)
        last = harness
        if _wait_for(lambda: bool(harness.logs.events("verdict")), timeout=4.0):
            return harness
    return last


def test_runtime_closes_only_stuck_connections_on_the_demoted_leaf(harness_factory) -> None:
    harness = _run_stuck_scenario(harness_factory, successor_mode="ok", flags=DURABLE)
    controller = harness.controller
    assert "verdict=CONVICT_CONTRACT" in harness.logs.events("verdict")[0], harness.logs.messages()
    # AGED at the first watch poll after S_C0 passed; WAITING once its
    # request has waited ≥2 s.
    assert _wait_for(lambda: set(controller.closed) >= {"aged", "waiting"}, timeout=6.0), harness.logs.messages()
    time.sleep(1.2)
    assert sorted(controller.closed) == ["aged", "waiting"]
    assert controller.writes == []
    closes = [message for message in harness.logs.events("close") if "action=close" in message]
    assert closes and all("reason=stuck_on_demoted_leaf" in message for message in closes)
    assert [item["proxy_name"] for item in harness.manager.observations] == ["n1"]
    _no_raw_names(harness.logs.messages())


def test_runtime_move_only_closes_the_same_stuck_connections(harness_factory) -> None:
    harness = _run_stuck_scenario(harness_factory, successor_mode="ok")
    controller = harness.controller
    verdict = harness.logs.events("verdict")[0]
    assert "verdict=CONVICT_CONTRACT" in verdict and " durable=off " in verdict, harness.logs.messages()
    assert _wait_for(lambda: set(controller.closed) >= {"aged", "waiting"}, timeout=6.0), harness.logs.messages()
    time.sleep(1.2)
    assert sorted(controller.closed) == ["aged", "waiting"]
    assert controller.writes == []
    closes = [message for message in harness.logs.events("close") if "action=close" in message]
    assert closes and all("reason=stuck_on_demoted_leaf" in message for message in closes)
    assert harness.manager.observations == [] and harness.manager.rechecks == []
    assert _wait_for(lambda: bool(harness.logs.events("commit")), timeout=4.0)
    assert "reason=post_dial_native_rebind durable=off)" in harness.logs.events("commit")[0]
    _no_raw_names(harness.logs.messages())


def test_runtime_no_close_when_the_successor_timed_out(harness_factory) -> None:
    harness = _run_stuck_scenario(harness_factory, successor_mode="blackhole")
    controller = harness.controller
    assert "verdict=INDETERMINATE" in harness.logs.events("verdict")[0], harness.logs.messages()
    assert _wait_for(lambda: any("action=keep_all" in message for message in harness.logs.events("close")), timeout=4.0), harness.logs.messages()
    time.sleep(1.2)
    assert controller.closed == [] and controller.writes == []
    assert harness.manager.observations == []


# ---------------------------------------------------------------------------
# Layer 2 at the §18.2 timing (5000 ms native timeout, +2 s S_C0, 6 s ticket):
# the black-hole and hold paths through the runtime glue


def _judge_outcome(harness: _Harness) -> Any:
    (incident,) = harness.judge_contexts()
    return harness.runtime._runtime_recovery_outcomes[incident]


def _probe_classes(harness: _Harness) -> dict[str, str]:
    classes: dict[str, str] = {}
    for message in harness.logs.events("probe_result"):
        name = re.search(r"name=(\S+)", message).group(1)
        classes[name] = re.search(r"class=(\S+)", message).group(1)
    return classes


def _short_judge_tickets(runtime: DesktopRuntime, seconds: float) -> None:
    """Register the judge's own tickets with a deadline inside the 6 s cap."""

    real = runtime._ensure_runtime_recovery_ticket

    def ensure(failure: Any, snapshot: Any, incident_id: str, **kwargs: Any) -> Any:
        if str(incident_id).startswith("post-dial-"):
            kwargs["deadline_monotonic"] = runtime._runtime_recovery_now() + seconds
        return real(failure, snapshot, incident_id, **kwargs)

    runtime._ensure_runtime_recovery_ticket = ensure


def test_runtime_pf4_black_hole_convicts_at_the_native_timeout_inside_the_ticket(harness_factory) -> None:
    harness = harness_factory(flags=DURABLE)
    harness.spy_readiness()
    controller = harness.controller
    controller.modes["n1"] = "blackhole"
    before_refreshes = controller.calls.count(("proxies",))

    harness.trigger()
    harness.settle(12.0)

    # A_C keeps its broker slot until Mihomo's own 504 at 5.0 s; S_C0 starts
    # on the +2 s timer while A_C is still pending, and passes.
    accused, successor = controller.delay_calls
    assert (accused["name"], successor["name"]) == ("n1", "n2")
    assert 1.8 <= successor["at"] - accused["at"] <= 2.6
    assert accused["timeout_ms"] == 5000 and 5.4 < accused["request_timeout"] <= 5.65
    assert _probe_classes(harness) == {"A_C": "timeout", "S_C0": "pass"}
    verdict = harness.logs.events("verdict")
    assert len(verdict) == 1 and "verdict=CONVICT_CONTRACT" in verdict[0]
    demotion = harness.logs.events("demotion")
    assert len(demotion) == 1 and "late=false" in demotion[0]
    # The commit (one real refresh, rebind, health overlay) still fits the
    # ~1 s left of the unchanged 6 s ticket after the 5.0 s answer.
    commit = harness.logs.events("commit")
    assert len(commit) == 1 and "accepted=true" in commit[0] and "reason=committed" in commit[0]
    (incident,) = harness.judge_contexts()
    ticket = harness.runtime._runtime_recovery_tickets[incident]
    assert abs((ticket.deadline_monotonic - ticket.observed_at_monotonic) - 6.0) < 0.01
    outcome = _judge_outcome(harness)
    assert outcome.status is RuntimeRecoveryStatus.RECOVERED
    assert outcome.reason == "post_dial_native_commit" and outcome.to_ref.proxy_name == "n2"
    assert controller.calls.count(("proxies",)) == before_refreshes + 1
    assert harness.binding().node.proxy_name == "n2"
    assert [item["proxy_name"] for item in harness.manager.observations] == ["n1"]
    assert [node.proxy_name for node, _kwargs in harness.manager.rechecks] == ["n1"]
    assert controller.writes == [] and controller.closed == [] and controller.fixed == ""
    assert harness.published == []
    _no_raw_names(harness.logs.messages())


def test_runtime_black_hole_answer_after_the_ticket_still_demotes_and_closes(harness_factory) -> None:
    # V4: A_C's 504 (5.0 s) misses a 4 s ticket.  S_C0 passed at +2 s, the
    # deadline readback finds no newer sample on L: INDETERMINATE, no health
    # overlay.  Mihomo's own failed sample still moved TIER3-AUTO, so the late
    # demotion is watched and the observer's stuck ids on L are closed.
    harness = None
    for _attempt in range(3):
        t0 = time.time() + 0.4
        connections, lines = _stuck_scenario(t0, successor_mode="ok")
        harness = harness_factory(connections=connections)
        _short_judge_tickets(harness.runtime, 4.0)
        harness.controller.modes["n1"] = "blackhole"
        _feed(harness.runtime, lines)
        if _wait_for(lambda: bool(harness.logs.events("verdict")), timeout=6.0):
            break
    controller = harness.controller
    assert "verdict=INDETERMINATE" in harness.logs.events("verdict")[0], harness.logs.messages()
    readback = harness.logs.events("readback")
    assert len(readback) == 1 and "accused_timeout=true" not in readback[0]
    assert _wait_for(lambda: set(controller.closed) >= {"aged", "waiting"}, timeout=6.0), harness.logs.messages()
    time.sleep(1.2)
    assert sorted(controller.closed) == ["aged", "waiting"]
    demotion = harness.logs.events("demotion")
    assert len(demotion) == 1 and "late=true" in demotion[0]
    assert harness.logs.events("commit") == []
    assert harness.manager.observations == []
    outcome = _judge_outcome(harness)
    assert outcome.status is RuntimeRecoveryStatus.CONTROLLER_HOLD
    assert outcome.reason == "post_dial_indeterminate"
    assert controller.writes == []
    _no_raw_names(harness.logs.messages())


def test_runtime_pf7_upstream_stall_holds_facility_and_closes_nothing(harness_factory) -> None:
    harness = harness_factory()
    harness.spy_readiness()
    controller = harness.controller
    controller.modes["n1"] = "blackhole"
    controller.modes["n2"] = "blackhole"

    harness.trigger()
    harness.settle(12.0)

    # A_C 504 at 5.0 s, S_C0 (from +2 s) 504 at 7.0 s: INDETERMINATE at the
    # 5.5 s evidence deadline, then the late FACILITY hold.
    assert [call["name"] for call in controller.delay_calls] == ["n1", "n2"]
    assert _probe_classes(harness) == {"A_C": "timeout", "S_C0": "timeout"}
    verdict = harness.logs.events("verdict")
    assert len(verdict) == 1 and "verdict=INDETERMINATE" in verdict[0]
    late = harness.logs.events("late_hold")
    assert len(late) == 1 and "hold=FACILITY" in late[0]
    outcome = _judge_outcome(harness)
    assert outcome.status is RuntimeRecoveryStatus.CONTROLLER_HOLD
    assert outcome.reason == "post_dial_indeterminate"
    assert controller.closed == [] and controller.writes == []
    assert harness.manager.observations == [] and harness.published == []
    # The hold gates every leaf ...
    harness.trigger("n3", episode=2)
    assert "reason=facility_hold" in harness.logs.events("gated")[-1]
    assert len(controller.delay_calls) == 2
    # ... until an answered TIER3 user connection is seen entirely after it.
    judge = harness.judge
    answered = SnapshotDigest(True, time.time(), True, 0.001, 3, 0, True, False)
    judge.on_snapshot(answered, previous_poll_wall=time.time() - 60.0)
    assert judge.gates.hold is not None
    unanswered = SnapshotDigest(True, time.time(), True, 0.001, 3, 0, False, False)
    judge.on_snapshot(unanswered, previous_poll_wall=time.time())
    assert judge.gates.hold is not None
    assert harness.logs.events("hold_released") == []
    harness.trigger("n3", episode=3)
    assert "reason=facility_hold" in harness.logs.events("gated")[-1]
    judge.on_snapshot(answered, previous_poll_wall=time.time())
    assert judge.gates.hold is None
    assert "hold=FACILITY" in harness.logs.events("hold_released")[0]


def test_runtime_pf7b_every_member_failing_fast_is_exhausted_and_holds(harness_factory) -> None:
    harness = harness_factory()
    controller = harness.controller
    controller.modes["n1"] = "zombie"
    controller.modes["n2"] = "zombie"

    harness.trigger()
    harness.settle()

    assert [call["name"] for call in controller.delay_calls] == ["n1", "n2"]
    assert _probe_classes(harness) == {"A_C": "transport_fail", "S_C0": "transport_fail"}
    assert "verdict=EXHAUSTED" in harness.logs.events("verdict")[0]
    outcome = _judge_outcome(harness)
    assert outcome.status is RuntimeRecoveryStatus.CONTROLLER_HOLD
    assert outcome.reason == "post_dial_exhausted"
    assert harness.logs.events("commit") == [] and harness.manager.observations == []
    assert controller.closed == [] and controller.writes == []
    harness.trigger("n3", episode=2)
    assert "reason=exhausted_hold" in harness.logs.events("gated")[-1]
    assert len(controller.delay_calls) == 2


# ---------------------------------------------------------------------------
# Layer 2: the stale-route refresh, the CAS baseline and the copy-free gates


def _failing_proxies(controller: _FakeController, failures: int) -> None:
    """The next ``failures`` ``GET /proxies`` reads time out."""

    real = controller.proxies
    left = {"count": int(failures)}

    def proxies(*, timeout: float | None = None) -> dict[str, Any]:
        with controller.lock:
            fail = left["count"] > 0
            if fail:
                left["count"] -= 1
                controller.calls.append(("proxies-failed",))
        if fail:
            raise TimeoutError("controller read timed out")
        return real(timeout=timeout)

    controller.proxies = proxies


def _sleep_until(deadline: float) -> None:
    time.sleep(max(0.0, deadline - time.monotonic()))


def test_runtime_failed_stale_route_refresh_is_retried_then_judged(harness_factory) -> None:
    harness = harness_factory(flags=DURABLE)
    controller = harness.controller
    # Mihomo moved natively to n2; the first refresh of the stale route fails.
    controller.sample("n1", False)
    controller.modes["n2"] = "zombie"
    _failing_proxies(controller, 1)
    before = controller.calls.count(("proxies",))

    first = time.monotonic()
    harness.trigger("n2", episode=1)
    assert "reason=route_binding_mismatch" in harness.logs.events("gated")[0]
    assert _wait_for(lambda: ("proxies-failed",) in controller.calls)
    # Inside the retry spacing: still gated, no second refresh.
    _sleep_until(first + 0.6)
    harness.trigger("n2", episode=1, new_episode=False)
    time.sleep(0.1)
    assert controller.calls.count(("proxies-failed",)) == 1
    assert controller.calls.count(("proxies",)) == before
    # After it: the retry publishes Mihomo's move and n2 is then judged.
    _sleep_until(first + 1.2)
    harness.trigger("n2", episode=1, new_episode=False)
    assert _wait_for(lambda: controller.calls.count(("proxies",)) == before + 1)
    assert _wait_for(lambda: harness.runtime._runtime_recovery_is_armed())
    _sleep_until(first + 1.8)
    harness.trigger("n2", episode=1, new_episode=False)
    harness.settle()
    assert [call["name"] for call in controller.delay_calls] == ["n2", "n3"]
    assert [item["proxy_name"] for item in harness.manager.observations] == ["n2"]


def test_runtime_stale_route_refreshes_are_bounded_per_leaf_and_snapshot(harness_factory) -> None:
    harness = harness_factory()
    controller = harness.controller
    controller.sample("n1", False)
    _failing_proxies(controller, 10)

    for attempt in range(5):
        started = time.monotonic()
        harness.trigger("n2", episode=1, new_episode=attempt == 0)
        expected = min(attempt + 1, 3)
        assert _wait_for(lambda: controller.calls.count(("proxies-failed",)) >= expected, timeout=1.0)
        _sleep_until(started + 1.1)
    time.sleep(0.2)
    assert controller.calls.count(("proxies-failed",)) == 3
    assert controller.delay_calls == [] and harness.judge_contexts() == []
    gated = harness.logs.events("gated")
    assert gated and all("reason=route_binding_mismatch" in message for message in gated)


def test_runtime_a_blocking_gate_queues_no_stale_route_refresh(harness_factory) -> None:
    harness = harness_factory()
    runtime = harness.runtime
    controller = harness.controller
    controller.sample("n1", False)
    before = controller.calls.count(("proxies",))
    d4_key = ("test-d4-probe", "n2")
    with runtime._runtime_failure_monitor_lock:
        runtime._runtime_failure_probe_bindings.add(d4_key)

    first = time.monotonic()
    harness.trigger("n2", episode=1)
    time.sleep(0.3)
    # D4 owns n2: gated before the route is looked at, nothing refreshed.
    assert "reason=d4_busy" in harness.logs.events("gated")[-1]
    assert controller.calls.count(("proxies",)) == before

    with runtime._runtime_failure_monitor_lock:
        runtime._runtime_failure_probe_bindings.discard(d4_key)
    _sleep_until(first + 0.6)
    harness.trigger("n2", episode=1, new_episode=False)
    # The stale route is now the last gate: its first refresh is queued.
    assert "reason=route_binding_mismatch" in harness.logs.events("gated")[-1]
    assert _wait_for(lambda: controller.calls.count(("proxies",)) == before + 1)


def test_runtime_selection_declared_between_the_gates_and_the_ticket_is_not_judged(harness_factory) -> None:
    harness = harness_factory()
    runtime = harness.runtime
    controller = harness.controller
    controller.modes["n1"] = "zombie"
    real = runtime._ensure_runtime_recovery_ticket

    def ensure(failure: Any, snapshot: Any, incident_id: str, **kwargs: Any) -> Any:
        runtime._declare_runtime_selector_intent("test user selection")
        return real(failure, snapshot, incident_id, **kwargs)

    runtime._ensure_runtime_recovery_ticket = ensure
    harness.trigger()
    harness.settle()

    # The ticket already carries the new revision: settled unjudged.
    assert controller.delay_calls == []
    assert "reason=authority_changed" in harness.logs.events("gated")[0]
    outcome = _judge_outcome(harness)
    assert outcome.status is RuntimeRecoveryStatus.CONTROLLER_HOLD
    assert outcome.reason == "post_dial_authority_changed"
    assert harness.manager.observations == [] and controller.writes == []


def test_runtime_selection_declared_while_the_commit_rebinds_rejects_it(harness_factory) -> None:
    harness = harness_factory()
    harness.spy_readiness()
    runtime = harness.runtime
    controller = harness.controller
    controller.modes["n1"] = "zombie"
    real = runtime._register_runtime_recovery_ticket

    def register(failure: Any, snapshot: Any, incident_id: str, **kwargs: Any) -> Any:
        if kwargs.get("force_rebind"):
            # After the rebind check, before the re-registered ticket.
            runtime._declare_runtime_selector_intent("test user selection")
        return real(failure, snapshot, incident_id, **kwargs)

    runtime._register_runtime_recovery_ticket = register
    harness.trigger()
    harness.settle()

    assert [call["name"] for call in controller.delay_calls] == ["n1", "n2"]
    commit = harness.logs.events("commit")
    assert len(commit) == 1 and "accepted=false" in commit[0] and "reason=authority_changed" in commit[0]
    assert harness.manager.observations == [] and harness.manager.rechecks == []
    outcome = _judge_outcome(harness)
    assert outcome.status is RuntimeRecoveryStatus.CONTROLLER_HOLD
    assert outcome.reason == "post_dial_authority_changed"
    assert harness.published == [] and controller.writes == []


def test_runtime_gates_read_the_published_route_without_copying_it(harness_factory) -> None:
    harness = harness_factory(flags={"post_dial_judge": "shadow"})
    runtime = harness.runtime
    d4_key = harness.binding().circuit_key
    me = threading.current_thread()
    copies: list[str] = []
    real = runtime._runtime_route_snapshot_state

    def counted() -> Any:
        if threading.current_thread() is me:
            copies.append("route_state")
        return real()

    runtime._runtime_route_snapshot_state = counted
    harness.trigger()
    assert "gate=open" in harness.logs.events("would_judge")[0]
    # Act mode, blocked after every state-reading gate ran (D4 on the binding).
    runtime.config["client"]["egress_recovery"]["node_judgement"]["post_dial_judge"] = "act"
    with runtime._runtime_failure_monitor_lock:
        runtime._runtime_failure_probe_bindings.add(d4_key)
    harness.trigger(episode=2)
    assert "reason=d4_busy" in harness.logs.events("gated")[-1]
    assert copies == []
    assert harness.controller.delay_calls == []


# ---------------------------------------------------------------------------
# Layer 2: final-review pins (route refresh after a demotion, rebind and
# native-pick refusals, probe priority, close flag, deadline readback, closes)


def test_runtime_demotion_without_a_commit_refreshes_the_route_for_the_next_trigger(harness_factory) -> None:
    harness = harness_factory(flags=DURABLE)
    controller = harness.controller
    # A status answer: NOT_REPRODUCED, but Mihomo still wrote n1 dead and moved.
    controller.modes["n1"] = "answer"
    before = controller.calls.count(("proxies",))

    harness.trigger(episode=1)
    harness.settle()

    assert "verdict=NOT_REPRODUCED" in harness.logs.events("verdict")[0]
    assert len(harness.logs.events("demotion")) == 1
    assert harness.logs.events("commit") == []
    # One refresh publishes Mihomo's move before any trigger finds it stale.
    assert _wait_for(lambda: harness.binding() is not None and harness.binding().node.proxy_name == "n2")
    assert controller.calls.count(("proxies",)) == before + 1
    assert _wait_for(lambda: harness.runtime._runtime_recovery_is_armed())
    # The next trigger, on Mihomo's successor, binds at once.
    controller.modes["n2"] = "zombie"
    harness.trigger("n2", episode=1)
    harness.settle()
    assert harness.logs.events("gated") == []
    assert [call["name"] for call in controller.delay_calls] == ["n1", "n2", "n3"]
    assert [item["proxy_name"] for item in harness.manager.observations] == ["n2"]
    assert controller.writes == []


def test_runtime_commit_refuses_a_move_while_the_accused_is_still_alive(harness_factory) -> None:
    # §18.7 native_commit: require G3.now == S and not alive(L).
    harness = harness_factory()
    harness.spy_readiness()
    controller = harness.controller
    controller.modes["n1"] = "zombie"

    def l_alive_again_but_group_on_n2() -> None:
        with controller.lock:
            controller._now = lambda: "n2"  # type: ignore[method-assign]
            controller.alive["n1"] = True

    controller.before_reply["n2"] = l_alive_again_but_group_on_n2
    harness.trigger()
    harness.settle()

    assert "verdict=CONVICT_CONTRACT" in harness.logs.events("verdict")[0]
    commit = harness.logs.events("commit")
    assert len(commit) == 1 and "accepted=false" in commit[0] and "reason=rebind_rejected" in commit[0]
    assert harness.manager.observations == [] and harness.manager.rechecks == []
    outcome = _judge_outcome(harness)
    assert outcome.status is RuntimeRecoveryStatus.CONTROLLER_HOLD
    assert outcome.reason == "post_dial_rebind_rejected"
    assert harness.published == [] and controller.writes == []


def test_runtime_commit_refuses_a_native_pick_other_than_the_proven_successor(harness_factory) -> None:
    harness = harness_factory()
    harness.spy_readiness()
    controller = harness.controller
    controller.modes["n1"] = "zombie"
    real_provider = controller.provider_proxies_one

    def stale_provider(name: str, *, timeout: float | None = None) -> dict[str, Any]:
        # A stale S3 read: n2 looks Core-dead, so S_C0 goes to n3, while
        # Mihomo itself moves TIER3-AUTO from n1 to n2.
        payload = real_provider(name, timeout=timeout)
        for entry in payload["proxies"]:
            if entry["name"] == "n2":
                entry["alive"] = False
                entry["extra"][URL] = dict(entry["extra"][URL], alive=False)
        return payload

    controller.provider_proxies_one = stale_provider  # type: ignore[method-assign]
    harness.trigger()
    harness.settle()

    assert [call["name"] for call in controller.delay_calls] == ["n1", "n3"]
    assert "verdict=CONVICT_CONTRACT" in harness.logs.events("verdict")[0]
    commit = harness.logs.events("commit")
    assert len(commit) == 1 and "accepted=false" in commit[0] and "reason=native_pick_mismatch" in commit[0]
    assert harness.manager.observations == [] and harness.manager.rechecks == []
    outcome = _judge_outcome(harness)
    assert outcome.status is RuntimeRecoveryStatus.CONTROLLER_HOLD
    assert outcome.reason == "post_dial_native_pick_mismatch"
    # Logged, not corrected: no PUT towards the proven successor.
    assert controller.writes == [] and controller.fixed == ""
    assert harness.published == []


def test_runtime_judge_probe_is_admitted_before_queued_background_work(harness_factory) -> None:
    harness = harness_factory()
    runtime = harness.runtime
    controller = harness.controller
    controller.modes["n1"] = "zombie"
    broker = runtime._runtime_probe_broker
    started = threading.Barrier(3)
    priorities: list[Any] = []
    real_run = runtime._run_brokered_runtime_probe

    def spy(*args: Any, **kwargs: Any) -> Any:
        priorities.append(kwargs.get("priority"))
        return real_run(*args, **kwargs)

    runtime._run_brokered_runtime_probe = spy

    def holder(seconds: float) -> Callable[[Any], None]:
        def probe(context: Any) -> None:
            context.register_abort_callback(lambda: None)
            context.mark_socket_started()
            started.wait(1.0)
            time.sleep(seconds)

        return probe

    # One slot frees at 0.6 s, the other only at 3 s: whoever is first in the
    # broker's (priority, sequence) order gets the 0.6 s slot.
    holders = [
        threading.Thread(
            target=broker.run,
            args=(ProbePriority.BACKGROUND, holder(seconds)),
            kwargs={"queue_timeout_seconds": 1.0},
            daemon=True,
        )
        for seconds in (0.6, 3.0)
    ]
    for thread in holders:
        thread.start()
    started.wait(1.0)
    order: list[str] = []
    controller.before_reply["n1"] = lambda: order.append("A_C")

    def queued_background(context: Any) -> None:
        order.append("background")
        context.register_abort_callback(lambda: None)
        context.mark_socket_started()

    waiter = threading.Thread(
        target=broker.run,
        args=(ProbePriority.BACKGROUND, queued_background),
        kwargs={"queue_timeout_seconds": 4.0},
        daemon=True,
    )
    waiter.start()
    assert _wait_for(lambda: broker.snapshot().queued_background >= 1)
    # Queued after the background waiter, admitted before it.
    harness.trigger()
    harness.settle()
    waiter.join(5.0)
    for thread in holders:
        thread.join(5.0)

    assert priorities and all(priority is ProbePriority.RUNTIME_MANUAL for priority in priorities)
    assert controller.delay_calls[0]["name"] == "n1"
    assert order[:2] == ["A_C", "background"], order


def test_runtime_close_flag_off_commits_but_closes_nothing_and_starts_no_watch(harness_factory) -> None:
    harness = _run_stuck_scenario(
        harness_factory,
        successor_mode="ok",
        flags={"close_stuck_connections": False, **DURABLE},
    )
    controller = harness.controller
    assert "verdict=CONVICT_CONTRACT" in harness.logs.events("verdict")[0], harness.logs.messages()
    assert _wait_for(lambda: bool(harness.logs.events("commit")), timeout=4.0)
    assert "accepted=true" in harness.logs.events("commit")[0]
    assert len(harness.logs.events("demotion")) == 1
    time.sleep(1.5)
    assert controller.closed == [] and controller.writes == []
    assert harness.judge._watches == {}
    assert harness.logs.events("close") == []
    assert [item["proxy_name"] for item in harness.manager.observations] == ["n1"]


def test_runtime_deadline_readback_turns_a_queued_accused_into_a_timeout(harness_factory) -> None:
    # A_C's 504 reaches the judge only after the evidence deadline, but
    # Mihomo's history already holds L's dead sample, newer than the A_C
    # issue: the §5.3 readback counts it as a timeout and the verdict stands.
    harness = harness_factory(flags=DURABLE)
    harness.spy_readiness()
    _short_judge_tickets(harness.runtime, 4.0)
    controller = harness.controller
    controller.modes["n1"] = "sampled_blackhole"

    harness.trigger()
    harness.settle(12.0)

    readback = harness.logs.events("readback")
    assert len(readback) == 1 and "accused_timeout=true" in readback[0]
    verdict = harness.logs.events("verdict")
    assert len(verdict) == 1 and "verdict=CONVICT_CONTRACT" in verdict[0]
    commit = harness.logs.events("commit")
    assert len(commit) == 1 and "accepted=true" in commit[0], harness.logs.messages()
    assert [item["proxy_name"] for item in harness.manager.observations] == ["n1"]
    assert controller.writes == [] and harness.published == []


class _ClosableConnections:
    """/connections for the exact-id close: two stuck ids on n1, one on n2."""

    def __init__(self) -> None:
        chain = ["TIER3-AUTO", "PROXY-FALLBACK", "FINAL-EXIT", "GENERAL"]
        self.items = [
            {"id": "a", "chains": ["n1", *chain]},
            {"id": "b", "chains": ["n1", *chain]},
            {"id": "c", "chains": ["n2", *chain]},
        ]
        self.fail_list = False

    def snapshot(self, _t: float) -> dict[str, Any]:
        if self.fail_list:
            raise TimeoutError("controller read timed out")
        return {"connections": [dict(item) for item in self.items]}

    def close(self, connection_id: str, _t: float) -> None:
        self.items = [item for item in self.items if item["id"] != connection_id]


def test_runtime_close_reports_only_the_ids_whose_delete_succeeded(harness_factory) -> None:
    connections = _ClosableConnections()
    harness = harness_factory(connections=connections)
    controller = harness.controller
    binding = harness.binding()
    assert binding is not None and binding.node.proxy_name == "n1"
    job = PostDialJudgeJob(
        leaf="n1",
        kind="FAST",
        tokens=("tok-a",),
        episode=1,
        test_url=URL,
        expected_status="204",
        ticket_deadline=time.monotonic() + 6.0,
        runner=None,
        context=SimpleNamespace(binding=binding),
    )
    real_close = controller.close_connection

    def close_connection(connection_id: str, *, timeout: float | None = None) -> dict[str, Any]:
        if connection_id == "b":
            raise MihomoAPIError("controller HTTP 500")
        return real_close(connection_id, timeout=timeout)

    controller.close_connection = close_connection  # type: ignore[method-assign]
    runtime = harness.runtime

    # "c" is on another leaf and never closed; "b"'s DELETE fails.
    assert runtime._runtime_post_dial_close(job, "n1", ("a", "b", "c")) == ("a",)
    assert controller.closed == ["a"]
    # A failed GET /connections closes nothing and says so.
    connections.fail_list = True
    assert runtime._runtime_post_dial_close(job, "n1", ("b",)) == ()
    connections.fail_list = False
    # The binding must be L's.
    assert runtime._runtime_post_dial_close(job, "n2", ("c",)) == ()
    controller.close_connection = real_close  # type: ignore[method-assign]
    assert runtime._runtime_post_dial_close(job, "n1", ("b",)) == ("b",)
    assert controller.closed == ["a", "b"] and controller.writes == []


# ---------------------------------------------------------------------------
# Layer 3: ``rearm_on_stale_route``.  An act admission gated ``not_armed``
# only because a native leaf move published a newer route epoch than the arm
# queues the existing bounded route refresh, whose worker re-arms through the
# unchanged ``_try_arm_runtime_recovery``.  Key off (the default): today's
# control flow, plus ``would_queue`` diagnostics.  Shadow never queues.

REARM = {"rearm_on_stale_route": True}


def _native_move_leaves_the_arm_stale(harness: _Harness, *, same_leaf: bool = False) -> Any:
    """Mihomo moves n1 -> n2 (or only the route epoch moves) and the passive
    path republishes the route: the arm stays installed but stale."""

    runtime = harness.runtime
    arm = runtime._runtime_recovery_arm
    if same_leaf:
        # A FINAL-EXIT selection-signature change on the same leaf.
        with runtime._runtime_route_snapshot_lock:
            runtime._runtime_route_epoch += 1
    else:
        harness.controller.sample("n1", False)
    assert runtime._refresh_runtime_route_snapshot(timeout_seconds=1.0) is not None
    assert runtime._runtime_route_epoch > arm.route_epoch
    assert runtime._runtime_recovery_arm is arm
    assert harness.binding().node.proxy_name == ("n1" if same_leaf else "n2")
    assert _arm_check(runtime) == "route_epoch"
    return arm


def _arm_check(runtime: DesktopRuntime) -> Any:
    with runtime._node_judgement_arm_capture() as trace:
        runtime._runtime_recovery_is_armed()
    return trace.get("arm_check")


def _refresh_records(harness: _Harness, count: int = 1, timeout: float = 2.0) -> list[str]:
    assert _wait_for(lambda: len(harness.logs.events("judge_refresh")) >= count, timeout)
    return harness.logs.events("judge_refresh")


def _drained(harness: _Harness) -> None:
    state = harness.runtime.__dict__.get("_node_judgement_arm_diagnostics_state")
    if state is not None:
        assert _wait_for(lambda: not state["queue"])
    time.sleep(0.05)


def _summary(harness: _Harness) -> dict[str, int]:
    harness.runtime._log_runtime_post_dial_record("info", {"ev": "summary"})
    line = harness.logs.events("summary")[-1]
    return {key: int(value) for key, value in re.findall(r"(judge_refresh_\w+)=(\d+)", line)}


def _spy_remember(harness: _Harness) -> list[Any]:
    judge = harness.judge
    real = judge.remember_gate
    remembered: list[Any] = []

    def remember(leaf: str, episode: int, now: float, reason: Any, route: Any = None) -> None:
        remembered.append(reason)
        real(leaf, episode, now, reason, route)

    judge.remember_gate = remember  # type: ignore[method-assign]
    return remembered


_COUNTERS = {
    "judge_refresh_queued_not_armed",
    "judge_refresh_queued_mismatch",
    "judge_refresh_single_flight",
    "judge_refresh_bounded_attempts",
    "judge_refresh_bounded_retry",
    "judge_refresh_would_queue",
    "judge_refresh_skipped_incident",
    "judge_refresh_skipped_activation_hold",
    "judge_refresh_source_was_ready",
    "judge_refresh_worker_started",
}


@pytest.mark.parametrize("same_leaf", [False, True])
def test_runtime_stale_arm_route_epoch_queues_one_refresh_and_rearms(harness_factory, same_leaf: bool) -> None:
    harness = harness_factory(flags=REARM)
    runtime = harness.runtime
    controller = harness.controller
    stale = _native_move_leaves_the_arm_stale(harness, same_leaf=same_leaf)
    leaf = "n1" if same_leaf else "n2"
    controller.modes[leaf] = "zombie"
    before = controller.calls.count(("proxies",))

    harness.trigger(leaf, episode=1)
    gated = harness.logs.events("gated")
    assert len(gated) == 1 and "reason=not_armed" in gated[0] and "arm_check=route_epoch" in gated[0]
    assert _wait_for(lambda: runtime._runtime_recovery_is_armed())
    assert controller.calls.count(("proxies",)) == before + 1
    arm = runtime._runtime_recovery_arm
    assert arm is not stale and arm.route_epoch == runtime._runtime_route_epoch
    assert arm.selector_intent_revision == stale.selector_intent_revision
    assert controller.writes == [] and controller.delay_calls == []
    (record,) = _refresh_records(harness)
    assert "cause=not_armed_route_epoch" in record and "disposition=queued" in record
    assert "arm_check=route_epoch" in record and "key_attempt=1" in record and "key_on=1" in record
    assert f"arm_route_epoch={stale.route_epoch} " in record

    # The next evaluation of the same episode is admitted and judged.
    harness.trigger(leaf, episode=1, new_episode=False)
    assert harness.judge.metrics["judgements"] == 1
    harness.settle()
    assert controller.writes == []
    counts = _summary(harness)
    assert set(counts) == _COUNTERS
    assert counts["judge_refresh_queued_not_armed"] == 1
    assert counts["judge_refresh_worker_started"] == 1
    assert counts["judge_refresh_would_queue"] == 0 and counts["judge_refresh_queued_mismatch"] == 0
    _no_raw_names(harness.logs.messages())


def test_runtime_stale_arm_key_off_keeps_todays_control_flow(harness_factory) -> None:
    harness = harness_factory()
    runtime = harness.runtime
    controller = harness.controller
    stale = _native_move_leaves_the_arm_stale(harness)
    remembered = _spy_remember(harness)
    before = controller.calls.count(("proxies",))

    harness.trigger("n2", episode=1)
    gated = harness.logs.events("gated")
    assert len(gated) == 1 and "reason=not_armed" in gated[0] and "arm_check=route_epoch" in gated[0]
    time.sleep(0.3)
    assert controller.calls.count(("proxies",)) == before
    assert runtime._runtime_recovery_arm is stale and not runtime._runtime_recovery_is_armed()
    assert remembered == ["not_armed"]
    assert runtime._node_judgement_state().binding_refresh_keys == {}
    (record,) = _refresh_records(harness)
    assert "disposition=would_queue" in record and "key_on=0" in record and "key_attempt=0" in record
    assert _summary(harness)["judge_refresh_would_queue"] == 1

    # A second evaluation of the episode (after the cached answer expires):
    # still nothing queued, the record is not repeated.
    time.sleep(judge_module.GATE_RECHECK_S + 0.05)
    harness.trigger("n2", episode=1, new_episode=False)
    _drained(harness)
    assert controller.calls.count(("proxies",)) == before
    assert len(harness.logs.events("judge_refresh")) == 1
    counts = _summary(harness)
    assert counts["judge_refresh_would_queue"] == 2
    assert counts["judge_refresh_queued_not_armed"] == 0 and counts["judge_refresh_worker_started"] == 0
    assert controller.delay_calls == [] and harness.judge_contexts() == []


def test_runtime_shadow_never_queues_a_stale_arm_refresh(harness_factory) -> None:
    harness = harness_factory(flags={"post_dial_judge": "shadow", **REARM})
    runtime = harness.runtime
    controller = harness.controller
    stale = _native_move_leaves_the_arm_stale(harness)
    before = controller.calls.count(("proxies",))

    harness.trigger("n2", episode=1)
    _drained(harness)
    time.sleep(0.2)

    assert "gate=not_armed" in harness.logs.events("would_judge")[0]
    assert controller.calls.count(("proxies",)) == before
    assert runtime._runtime_recovery_arm is stale
    assert runtime._node_judgement_state().binding_refresh_keys == {}
    assert harness.logs.events("judge_refresh") == []
    counts = _summary(harness)
    assert set(counts) == _COUNTERS and set(counts.values()) == {0}


def test_runtime_user_pin_disarms_and_the_trigger_is_gated_with_the_key_on(harness_factory) -> None:
    harness = harness_factory(flags=REARM)
    before = harness.controller.calls.count(("proxies",))
    harness.runtime._declare_runtime_selector_intent("test user selection")
    assert not harness.runtime._runtime_recovery_is_armed()

    harness.trigger()
    harness.settle()

    assert harness.controller.delay_calls == []
    gated = harness.logs.events("gated")
    assert len(gated) == 1 and "reason=not_armed" in gated[0]
    assert harness.judge_contexts() == []
    (record,) = _refresh_records(harness)
    assert "disposition=skipped_arm:no_arm" in record
    time.sleep(0.2)
    assert harness.controller.calls.count(("proxies",)) == before
    assert harness.runtime._runtime_recovery_arm is None


def test_runtime_user_pin_race_with_stale_epoch_does_not_rearm(harness_factory) -> None:
    harness = harness_factory(flags=REARM)
    runtime = harness.runtime
    stale = _native_move_leaves_the_arm_stale(harness)
    before = harness.controller.calls.count(("proxies",))
    # ``_declare_runtime_selector_intent`` bumps the revision before it
    # disarms: an admission in that window still sees route_epoch first.
    with runtime._runtime_failure_monitor_lock:
        runtime._runtime_selector_intent_revision += 1
    assert _arm_check(runtime) == "route_epoch"

    harness.trigger("n2", episode=1)
    (record,) = _refresh_records(harness)
    assert "disposition=skipped_arm:selector_intent" in record and "arm_check=route_epoch" in record
    time.sleep(0.2)
    assert harness.controller.calls.count(("proxies",)) == before
    assert runtime._runtime_recovery_arm is stale
    assert runtime._runtime_recovery_arm.selector_intent_revision == stale.selector_intent_revision
    assert runtime._node_judgement_state().binding_refresh_keys == {}


def test_runtime_selector_intent_refusal_checks_each_revision_on_its_own(harness_factory) -> None:
    """Either revision alone refuses: the admission's read (a newer arm installed after it) and the
    current one (a user selection after the read)."""

    harness = harness_factory(flags=REARM)
    runtime = harness.runtime
    stale = _native_move_leaves_the_arm_stale(harness)
    own = stale.selector_intent_revision
    assert runtime._runtime_selector_intent_revision == own
    assert runtime._node_judgement_stale_arm_refresh_refusal(own) is None
    # (a) the admission read an older revision; the current one equals the arm's
    assert runtime._node_judgement_stale_arm_refresh_refusal(own - 1) == "selector_intent"
    # (b) the admission read the arm's revision; a user selection bumped the current one since
    with runtime._runtime_failure_monitor_lock:
        runtime._runtime_selector_intent_revision += 1
    assert runtime._node_judgement_stale_arm_refresh_refusal(own) == "selector_intent"
    assert runtime._node_judgement_stale_arm_refresh_refusal(own + 1) == "selector_intent"
    assert runtime._runtime_recovery_arm is stale
    assert runtime._node_judgement_state().binding_refresh_keys == {}


def test_runtime_masked_mismatch_is_refreshed_and_admitted(harness_factory) -> None:
    harness = harness_factory(flags=REARM)
    runtime = harness.runtime
    controller = harness.controller
    _native_move_leaves_the_arm_stale(harness)
    # Mihomo is back on n1 (alive again) since the last publication, which
    # still names n2: the stale route is masked by not_armed.
    controller.sample("n1", True)
    controller.modes["n1"] = "zombie"
    assert harness.binding().node.proxy_name == "n2"
    before = controller.calls.count(("proxies",))

    harness.trigger("n1", episode=1)
    assert "reason=not_armed" in harness.logs.events("gated")[0]
    assert _wait_for(lambda: runtime._runtime_recovery_is_armed())
    assert harness.binding().node.proxy_name == "n1"
    assert controller.calls.count(("proxies",)) == before + 1
    (record,) = _refresh_records(harness)
    assert "cause=not_armed_route_epoch" in record and "disposition=queued" in record

    harness.trigger("n1", episode=1, new_episode=False)
    assert harness.judge.metrics["judgements"] == 1
    harness.settle()
    assert controller.writes == []


def test_runtime_both_refresh_causes_share_one_bound_per_leaf_and_snapshot(harness_factory) -> None:
    harness = harness_factory(flags=REARM)
    runtime = harness.runtime
    state = runtime._node_judgement_state()
    snapshot, _payload = runtime._runtime_route_snapshot_state()
    clock = {"t": 100.0}
    runtime._runtime_recovery_now = lambda: clock["t"]
    origins: list[Any] = []
    start = {"real": True}

    def fake_queue() -> None:
        if start["real"]:
            runtime._runtime_route_snapshot_refresh_starts += 1
            origins.append(runtime._take_post_dial_refresh_origin())

    runtime._queue_runtime_route_snapshot_refresh = fake_queue
    mismatch, stale = "route_binding_mismatch", "not_armed_route_epoch"

    def queue(at: float, cause: str, snap: Any = snapshot) -> str:
        clock["t"] = at
        return runtime._queue_post_dial_binding_refresh("n2", snap, cause=cause)

    assert queue(100.0, mismatch) == "queued"
    assert queue(100.5, stale) == "bounded_retry"
    assert queue(101.0, stale) == "queued"
    start["real"] = False
    assert queue(102.0, mismatch) == "single_flight"
    assert state.refresh_origin_pending is None
    start["real"] = True
    assert queue(103.0, stale) == "bounded_attempts"
    assert queue(109.0, mismatch) == "bounded_attempts"
    assert origins == [mismatch, stale]
    # A new route view (native move) is a new key.
    moved = dataclasses.replace(snapshot, route_epoch=int(snapshot.route_epoch) + 1)
    assert queue(109.1, stale, moved) == "queued"
    assert origins == [mismatch, stale, stale]
    with state.lock:
        counts = dict(state.refresh_counts)
    assert counts["judge_refresh_queued_mismatch"] == 1
    assert counts["judge_refresh_queued_not_armed"] == 2
    assert counts["judge_refresh_single_flight"] == 1
    assert counts["judge_refresh_bounded_retry"] == 1
    assert counts["judge_refresh_bounded_attempts"] == 2


def test_runtime_stale_arm_refreshes_are_bounded_per_leaf_and_snapshot(harness_factory) -> None:
    harness = harness_factory(flags=REARM)
    controller = harness.controller
    stale = _native_move_leaves_the_arm_stale(harness)
    _failing_proxies(controller, 10)
    starts: list[float] = []
    real = harness.runtime._refresh_runtime_route_snapshot

    def timed(*args: Any, **kwargs: Any) -> Any:
        starts.append(time.monotonic())
        return real(*args, **kwargs)

    harness.runtime._refresh_runtime_route_snapshot = timed

    for attempt in range(5):
        started = time.monotonic()
        harness.trigger("n2", episode=1, new_episode=attempt == 0)
        if attempt < 3:
            assert _wait_for(lambda: controller.calls.count(("proxies-failed",)) >= attempt + 1, timeout=1.0)
        # Inside the retry spacing (after the cached answer expired): no
        # second refresh.
        _sleep_until(started + judge_module.GATE_RECHECK_S + 0.05)
        harness.trigger("n2", episode=1, new_episode=False)
        _sleep_until(started + 1.1)
    time.sleep(0.2)
    assert controller.calls.count(("proxies-failed",)) == 3
    assert len(starts) == 3
    assert all(later - earlier >= 1.0 for earlier, later in zip(starts, starts[1:]))
    assert harness.runtime._runtime_recovery_arm is stale
    assert controller.delay_calls == [] and harness.judge_contexts() == []
    counts = _summary(harness)
    assert counts["judge_refresh_queued_not_armed"] == 3
    assert counts["judge_refresh_worker_started"] == 0
    # In-window retries after attempts 1 and 2; everything after the third.
    assert counts["judge_refresh_bounded_retry"] == 2
    assert counts["judge_refresh_bounded_attempts"] == 5
    # Every queue-call disposition is its own judge_refresh record (only would_queue and skipped_* are
    # deduplicated per episode): the f06 analyzer's key checks read each attempt from the records.
    _refresh_records(harness, 10)
    _drained(harness)
    records = harness.logs.events("judge_refresh")
    dispositions = [re.search(r"disposition=([\w:]+)", record).group(1) for record in records]
    queued = [record for record in records if "disposition=queued" in record]
    assert [re.search(r"key_attempt=(\d+)", record).group(1) for record in queued] == ["1", "2", "3"]
    assert dispositions.count("bounded_retry") == 2
    assert dispositions.count("bounded_attempts") == 5
    assert len(records) == 10


@pytest.mark.parametrize(
    "gate",
    ["d4_busy", "status_failure_sample", "judging", "leaf_backoff", "leaf_check_cap", "global_cap"],
)
def test_runtime_stale_arm_with_another_blocking_gate_queues_nothing(harness_factory, gate: str) -> None:
    harness = harness_factory(flags=REARM)
    runtime = harness.runtime
    judge = harness.judge
    gates = judge.gates
    _native_move_leaves_the_arm_stale(harness)
    before = harness.controller.calls.count(("proxies",))
    now = runtime._runtime_recovery_now()
    if gate == "d4_busy":
        with runtime._runtime_failure_monitor_lock:
            runtime._runtime_failure_probe_bindings.add(("test-d4-probe", "n2"))
    elif gate == "status_failure_sample":
        judge.latest_sample_is_status_failure = lambda *_args: True  # type: ignore[method-assign]
    else:
        # The cheap gates normally stop a trigger before the admission; the
        # admission's own residual check must hold on its own too.
        judge.blocked_cheap = lambda _leaf, _now: None  # type: ignore[method-assign]
        if gate == "judging":
            gates._in_flight = "n1"
        elif gate == "leaf_backoff":
            gates._backoff["n2"] = now + 600.0
        elif gate == "leaf_check_cap":
            gates._checks["n2"] = deque([now] * gates._c.leaf_check_cap)
        else:
            gates._judgements.extend([now] * gates._c.global_max)
    windows = (len(gates._judgements), {leaf: len(times) for leaf, times in gates._checks.items()})
    try:
        harness.trigger("n2", episode=1)
        (record,) = _refresh_records(harness)
        assert f"disposition=skipped_residual:{gate}" in record
        assert "reason=not_armed" in harness.logs.events("gated")[0]
        time.sleep(0.2)
        assert harness.controller.calls.count(("proxies",)) == before
        assert runtime._node_judgement_state().binding_refresh_keys == {}
        assert (len(gates._judgements), {leaf: len(times) for leaf, times in gates._checks.items()}) == windows
    finally:
        gates._in_flight = None
        with runtime._runtime_failure_monitor_lock:
            runtime._runtime_failure_probe_bindings.discard(("test-d4-probe", "n2"))


@pytest.mark.parametrize(
    "case",
    [
        "no_arm",
        "readiness_state",
        "validation_active",
        "lifecycle",
        "worker_token",
        "facility_epoch",
        "epoch_current",
        "override_unknown",
    ],
)
def test_runtime_other_arm_refusals_never_queue(harness_factory, case: str) -> None:
    harness = harness_factory(flags=REARM)
    runtime = harness.runtime
    expected = case
    leaf = "n2"
    if case == "no_arm":
        runtime._runtime_recovery_arm = None
        leaf = "n1"
    elif case in {"readiness_state", "validation_active"}:
        _native_move_leaves_the_arm_stale(harness)
        runtime._runtime_egress_readiness = RuntimeEgressReadiness(
            state=(
                RuntimeEgressReadinessState.SERVING
                if case == "readiness_state"
                else RuntimeEgressReadinessState.READY
            ),
            validation_active=case == "validation_active",
        )
        expected = "initial:" + case
    elif case == "lifecycle":
        _native_move_leaves_the_arm_stale(harness)
        runtime._lifecycle_revision = int(getattr(runtime, "_lifecycle_revision", 0) or 0) + 1
    elif case == "worker_token":
        _native_move_leaves_the_arm_stale(harness)
        runtime._runtime_worker_token_is_current = lambda _token: False
    elif case == "facility_epoch":
        _native_move_leaves_the_arm_stale(harness)
        runtime._runtime_uses_early_single_coordinator = lambda: False
        runtime._runtime_facility_epoch = int(getattr(runtime, "_runtime_facility_epoch", 0) or 0) + 1
    elif case == "epoch_current":
        # The arm's epoch is current; only the published snapshot key moved.
        with runtime._runtime_route_snapshot_lock:
            snapshot = runtime._runtime_route_snapshot
            runtime._runtime_route_snapshot = dataclasses.replace(
                snapshot, network_context_epoch=int(snapshot.network_context_epoch) + 1
            )
        assert _arm_check(runtime) == "snapshot_key_local"
        leaf = "n1"
    else:
        runtime._runtime_recovery_is_armed = lambda *_args, **_kwargs: False
        expected = "epoch_current"
        leaf = "n1"
    before = harness.controller.calls.count(("proxies",))

    harness.trigger(leaf, episode=1)

    assert "reason=not_armed" in harness.logs.events("gated")[0]
    (record,) = _refresh_records(harness)
    assert f"disposition=skipped_arm:{expected}" in record
    time.sleep(0.2)
    assert harness.controller.calls.count(("proxies",)) == before
    assert runtime._node_judgement_state().binding_refresh_keys == {}


@pytest.mark.parametrize(
    "route_reason",
    ["route_unavailable", "non_member", "contract_unavailable", "route_unbound"],
)
def test_runtime_stale_arm_with_unfixable_route_reason_queues_nothing(harness_factory, route_reason: str) -> None:
    harness = harness_factory(flags=REARM)
    runtime = harness.runtime
    _native_move_leaves_the_arm_stale(harness)
    runtime._runtime_post_dial_route_gate = lambda _leaf, _snapshot, _payload: (route_reason, None, None)
    before = harness.controller.calls.count(("proxies",))

    harness.trigger("n2", episode=1)

    (record,) = _refresh_records(harness)
    assert f"disposition=skipped_route:{route_reason}" in record
    time.sleep(0.2)
    assert harness.controller.calls.count(("proxies",)) == before
    assert runtime._node_judgement_state().binding_refresh_keys == {}


@pytest.mark.parametrize("incident_state", ["pending", "recovering"])
def test_runtime_open_incident_skips_without_consuming_an_attempt(harness_factory, incident_state: str) -> None:
    harness = harness_factory(flags=REARM)
    runtime = harness.runtime
    controller = harness.controller
    stale = _native_move_leaves_the_arm_stale(harness)
    with runtime._runtime_failure_monitor_lock:
        runtime._runtime_incident_recovery_state["test-incident"] = incident_state
    before = controller.calls.count(("proxies",))

    harness.trigger("n2", episode=1)
    (record,) = _refresh_records(harness)
    assert "disposition=skipped_arm:incident" in record
    time.sleep(0.2)
    assert controller.calls.count(("proxies",)) == before
    assert runtime._node_judgement_state().binding_refresh_keys == {}
    assert runtime._runtime_incident_recovery_state == {"test-incident": incident_state}
    assert runtime._runtime_recovery_arm is stale
    assert _summary(harness)["judge_refresh_skipped_incident"] == 1

    with runtime._runtime_failure_monitor_lock:
        runtime._runtime_incident_recovery_state["test-incident"] = "ready"
    harness.trigger("n2", episode=2)
    assert _wait_for(lambda: runtime._runtime_recovery_is_armed())
    records = _refresh_records(harness, 2)
    assert "disposition=queued" in records[-1] and "key_attempt=1" in records[-1]
    assert controller.calls.count(("proxies",)) == before + 1


def test_runtime_controller_hold_skips_the_stale_arm_refresh(harness_factory) -> None:
    harness = harness_factory(flags=REARM)
    runtime = harness.runtime
    _native_move_leaves_the_arm_stale(harness)
    activations: list[str] = []
    runtime._queue_tier3_egress_activation = lambda: activations.append("queued")
    runtime._tier3_standby_activation_hold_kind = "controller_hold"
    before = harness.controller.calls.count(("proxies",))
    try:
        harness.trigger("n2", episode=1)
        (record,) = _refresh_records(harness)
        assert "disposition=skipped_arm:activation_hold" in record
        time.sleep(0.2)
        assert harness.controller.calls.count(("proxies",)) == before
        assert activations == []
        assert _summary(harness)["judge_refresh_skipped_activation_hold"] == 1
    finally:
        runtime._tier3_standby_activation_hold_kind = ""


@pytest.mark.parametrize("key_on", [True, False])
def test_runtime_rearm_gap_is_not_cached_as_not_armed(harness_factory, monkeypatch, key_on: bool) -> None:
    # Only a new publication could clear a cached answer inside the test.
    monkeypatch.setattr(judge_module, "GATE_RECHECK_S", 30.0)
    harness = harness_factory(flags=REARM if key_on else None)
    runtime = harness.runtime
    controller = harness.controller
    _native_move_leaves_the_arm_stale(harness)
    controller.modes["n2"] = "zombie"
    remembered = _spy_remember(harness)
    if not key_on:
        harness.trigger("n2", episode=1)
        assert remembered == ["not_armed"]
        return
    release = threading.Event()
    real_try = runtime._try_arm_runtime_recovery

    def gated_try(*args: Any, **kwargs: Any) -> Any:
        if threading.current_thread().name == "runtime-route-snapshot-refresh":
            release.wait(3.0)
        return real_try(*args, **kwargs)

    runtime._try_arm_runtime_recovery = gated_try
    published = runtime._runtime_route_snapshot
    before = controller.calls.count(("proxies",))
    try:
        harness.trigger("n2", episode=1)
        # The worker published the refreshed route and waits before try_arm.
        assert _wait_for(lambda: runtime._runtime_route_snapshot is not published)
        assert controller.calls.count(("proxies",)) == before + 1
        assert not runtime._runtime_recovery_is_armed()
        harness.trigger("n2", episode=1, new_episode=False)
        records = _refresh_records(harness, 2)
        assert "disposition=bounded_retry" in records[-1]
        assert remembered == []
    finally:
        release.set()
    assert _wait_for(lambda: runtime._runtime_recovery_is_armed())
    time.sleep(0.1)
    harness.trigger("n2", episode=1, new_episode=False)
    assert harness.judge.metrics["judgements"] == 1
    harness.settle()


def test_runtime_fix_rearm_keeps_lease_observer_and_starts_no_round(harness_factory) -> None:
    harness = harness_factory(flags=REARM)
    runtime = harness.runtime
    lease = runtime.__dict__.get("_runtime_monitoring_lease")
    observer = runtime.__dict__.get("_runtime_post_dial_observer")
    rounds: list[Any] = []
    real_round = runtime._start_deferred_native_group_round

    def spy_round(*args: Any, **kwargs: Any) -> Any:
        rounds.append(runtime._node_judgement_state().deferred_group_round)
        return real_round(*args, **kwargs)

    runtime._start_deferred_native_group_round = spy_round
    _native_move_leaves_the_arm_stale(harness)
    harness.trigger("n2", episode=1)
    assert _wait_for(lambda: runtime._runtime_recovery_is_armed())
    time.sleep(0.1)
    assert runtime.__dict__.get("_runtime_monitoring_lease") is lease
    assert runtime.__dict__.get("_runtime_post_dial_observer") is observer
    assert all(pending is None for pending in rounds)
    assert runtime._node_judgement_state().deferred_group_round is None
    assert harness.controller.writes == [] and harness.controller.delay_calls == []


def test_runtime_fix_rearm_source_ready_side_effect_is_pinned(harness_factory) -> None:
    harness = harness_factory(flags=REARM)
    runtime = harness.runtime
    notices: list[str] = []
    harness.manager.notify_tier3_source_supply_route_changed = notices.append
    _native_move_leaves_the_arm_stale(harness)
    qualification = runtime.__dict__.get("_tier3_source_control_qualification")
    # Existing codex try_arm behaviour, which the fix re-arm reaches: the
    # stale qualification clears ``was_ready`` with no wake notice.
    runtime._source_control_final_exit_was_ready = True

    harness.trigger("n2", episode=1)
    assert _wait_for(lambda: runtime._runtime_recovery_is_armed())
    time.sleep(0.05)

    assert runtime._source_control_final_exit_was_ready is False
    assert notices == []
    assert runtime.__dict__.get("_tier3_source_control_qualification") is qualification
    (record,) = _refresh_records(harness)
    assert "disposition=queued" in record and "source_was_ready=1" in record
    assert _summary(harness)["judge_refresh_source_was_ready"] == 1


def test_runtime_raising_diagnostics_never_change_the_stale_arm_refresh(harness_factory) -> None:
    harness = harness_factory(flags=REARM)
    runtime = harness.runtime
    controller = harness.controller

    real_submit = runtime._node_judgement_arm_submit

    # Only the new ev=judge_refresh emission breaks: the F01 records' own
    # unguarded call sites (e.g. recovery_phase) are F01's, out of scope here.
    def broken(record: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(record, dict) and record.get("ev") == "judge_refresh":
            raise RuntimeError("diagnostics broke")
        return real_submit(record, *args, **kwargs)

    runtime._node_judgement_arm_submit = broken
    _native_move_leaves_the_arm_stale(harness)
    controller.modes["n2"] = "zombie"
    remembered = _spy_remember(harness)
    before = controller.calls.count(("proxies",))

    harness.trigger("n2", episode=1)
    assert "reason=not_armed" in harness.logs.events("gated")[0]
    assert _wait_for(lambda: runtime._runtime_recovery_is_armed())
    assert controller.calls.count(("proxies",)) == before + 1
    # The re-arming refresh was queued: this not_armed is not cached.
    assert remembered == []
    harness.trigger("n2", episode=1, new_episode=False)
    assert harness.judge.metrics["judgements"] == 1
    harness.settle()
    assert harness.logs.events("judge_refresh") == []
    assert _summary(harness)["judge_refresh_queued_not_armed"] == 1


def test_runtime_route_refresh_starts_count_started_workers_only(harness_factory) -> None:
    harness = harness_factory(flags=REARM)
    runtime = harness.runtime
    state = runtime._node_judgement_state()
    starts = runtime._runtime_route_snapshot_refresh_starts
    # A single-flight drop starts nothing and consumes no origin.
    with runtime._runtime_route_snapshot_lock:
        runtime._runtime_route_snapshot_refresh_running = True
    with state.lock:
        state.refresh_origin_pending = "not_armed_route_epoch"
    runtime._queue_runtime_route_snapshot_refresh()
    assert runtime._runtime_route_snapshot_refresh_starts == starts
    assert state.refresh_origin_pending == "not_armed_route_epoch"
    with runtime._runtime_route_snapshot_lock:
        runtime._runtime_route_snapshot_refresh_running = False
    with state.lock:
        state.refresh_origin_pending = None
    # A refresh that is not post-dial-originated is not a judge refresh.
    runtime._queue_runtime_route_snapshot_refresh()
    assert runtime._runtime_route_snapshot_refresh_starts == starts + 1
    assert _wait_for(lambda: not runtime._runtime_route_snapshot_refresh_running)
    assert _summary(harness)["judge_refresh_worker_started"] == 0
    # A post-dial-originated one whose refresh returned nothing is not counted.
    runtime._refresh_runtime_route_snapshot = lambda **_kwargs: None
    with state.lock:
        state.refresh_origin_pending = "route_binding_mismatch"
    runtime._queue_runtime_route_snapshot_refresh()
    assert state.refresh_origin_pending is None
    assert _wait_for(lambda: not runtime._runtime_route_snapshot_refresh_running)
    assert runtime._runtime_route_snapshot_refresh_starts == starts + 2
    assert _summary(harness)["judge_refresh_worker_started"] == 0


@pytest.mark.parametrize(
    "existing",
    [
        "test_runtime_stale_published_route_is_gated_refreshed_once_then_judged",
        "test_runtime_cached_gate_holds_until_the_route_is_republished",
        "test_runtime_failed_stale_route_refresh_is_retried_then_judged",
        "test_runtime_stale_route_refreshes_are_bounded_per_leaf_and_snapshot",
        "test_runtime_a_blocking_gate_queues_no_stale_route_refresh",
        "test_runtime_demotion_without_a_commit_refreshes_the_route_for_the_next_trigger",
        "test_runtime_user_pin_disarms_and_the_trigger_is_gated",
    ],
)
def test_existing_route_refresh_pins_hold_with_the_key_on(harness_factory, monkeypatch, existing: str) -> None:
    def factory(**kwargs: Any) -> _Harness:
        flags = {**(kwargs.pop("flags", None) or {}), **REARM}
        return harness_factory(flags=flags, **kwargs)

    test = globals()[existing]
    if "monkeypatch" in inspect.signature(test).parameters:
        test(factory, monkeypatch)
    else:
        test(factory)


# Review fixes (2026-10-06): the existing mismatch queue call's cause, counter
# and record; hold_cache for single_flight / bounded_attempts; mode off; the
# F01 envelope of ev=judge_refresh; summary counters in act; a raising kick.


@pytest.mark.parametrize("key_on", [False, True])
def test_runtime_mismatch_refresh_is_logged_and_counted_with_its_own_cause(
    harness_factory, monkeypatch, key_on: bool
) -> None:
    monkeypatch.setattr(judge_module, "GATE_RECHECK_S", 30.0)
    harness = harness_factory(flags=REARM if key_on else None)
    controller = harness.controller
    # Mihomo moved natively after the last refresh; the arm is still current.
    controller.sample("n1", False)
    controller.modes["n2"] = "zombie"
    before = controller.calls.count(("proxies",))

    harness.trigger("n2", episode=1)
    assert "reason=route_binding_mismatch" in harness.logs.events("gated")[0]
    assert _wait_for(lambda: controller.calls.count(("proxies",)) == before + 1)
    (record,) = _refresh_records(harness)
    assert "cause=route_binding_mismatch" in record and "disposition=queued" in record
    assert "key_attempt=1" in record and f"key_on={int(key_on)}" in record
    assert "arm_check=armed" in record
    assert _wait_for(lambda: harness.runtime._runtime_recovery_is_armed())
    assert _wait_for(lambda: _summary(harness)["judge_refresh_worker_started"] == 1)
    counts = _summary(harness)
    assert counts["judge_refresh_queued_mismatch"] == 1
    assert counts["judge_refresh_queued_not_armed"] == 0
    assert counts["judge_refresh_would_queue"] == 0 and counts["judge_refresh_single_flight"] == 0
    harness.trigger("n2", episode=1, new_episode=False)
    assert harness.judge.metrics["judgements"] == 1
    harness.settle()
    assert len(harness.logs.events("judge_refresh")) == 1


def test_runtime_hold_cache_covers_single_flight_but_not_exhausted_keys(harness_factory, monkeypatch) -> None:
    monkeypatch.setattr(judge_module, "GATE_RECHECK_S", 30.0)
    harness = harness_factory(flags=REARM)
    runtime = harness.runtime
    state = runtime._node_judgement_state()
    _native_move_leaves_the_arm_stale(harness)
    remembered = _spy_remember(harness)
    before = harness.controller.calls.count(("proxies",))

    # Another route refresh is running: the call is dropped (single_flight),
    # whose publication a moment later may re-arm: not cached.
    with runtime._runtime_route_snapshot_lock:
        runtime._runtime_route_snapshot_refresh_running = True
    try:
        harness.trigger("n2", episode=1)
        (record,) = _refresh_records(harness)
        assert "disposition=single_flight" in record and "key_attempt=1" in record
        assert remembered == []
    finally:
        with runtime._runtime_route_snapshot_lock:
            runtime._runtime_route_snapshot_refresh_running = False

    # An exhausted key queues nothing more: the not_armed is cached as today.
    snapshot, _payload = runtime._runtime_post_dial_route_refs()
    with state.lock:
        state.binding_refresh_keys[("n2", tuple(snapshot.snapshot_key))] = (3, 0.0)
    harness.trigger("n2", episode=2)
    records = _refresh_records(harness, 2)
    assert "disposition=bounded_attempts" in records[-1]
    assert remembered == ["not_armed"]
    time.sleep(0.2)
    assert harness.controller.calls.count(("proxies",)) == before
    counts = _summary(harness)
    assert counts["judge_refresh_single_flight"] == 1 and counts["judge_refresh_bounded_attempts"] == 1
    assert counts["judge_refresh_queued_not_armed"] == 0


@pytest.mark.parametrize(
    "flags",
    [{"post_dial_judge": "off", **REARM}, {"enabled": False, **REARM}],
    ids=["post_dial_judge_off", "enabled_false"],
)
def test_runtime_judge_off_writes_no_refresh_line_or_counter(harness_factory, flags: dict[str, Any]) -> None:
    harness = harness_factory(flags=flags)
    runtime = harness.runtime
    controller = harness.controller
    stale = _native_move_leaves_the_arm_stale(harness)
    assert runtime._runtime_post_dial_mode()[0] == "off"
    assert runtime.__dict__.get("_runtime_post_dial_observer") is None  # no summary emitter
    before = controller.calls.count(("proxies",))

    harness.trigger("n2", episode=1)
    # Even a caller that claims act mode: the policy does not act.
    runtime._on_runtime_post_dial_trigger(
        Trigger(kind="FAST", leaf="n2", tokens=("tok-a",), t=time.time(), n_events=2,
                first_event_t=time.time(), episode=2, new_episode=True),
        mode="act",
        tokenizer=runtime._node_judgement_state().tokenizer,
    )
    _drained(harness)
    time.sleep(0.2)

    assert controller.calls.count(("proxies",)) == before
    assert runtime._runtime_recovery_arm is stale
    state = runtime._node_judgement_state()
    assert state.binding_refresh_keys == {}
    with state.lock:
        assert set(state.refresh_counts) == _COUNTERS and set(state.refresh_counts.values()) == {0}
    assert harness.logs.events("judge_refresh") == [] and harness.logs.events("gated") == []
    assert harness.logs.events("summary") == []
    assert not any("judge_refresh" in record.getMessage() for record in harness.logs.records)


def test_runtime_act_summary_carries_every_refresh_counter_from_zero(harness_factory) -> None:
    harness = harness_factory()
    counts = _summary(harness)
    assert set(counts) == _COUNTERS and set(counts.values()) == {0}
    line = harness.logs.events("summary")[-1]
    assert "gated_not_armed_check_changes=0" in line


def test_runtime_judge_refresh_record_carries_the_f01_envelope(harness_factory) -> None:
    harness = harness_factory(flags=REARM)
    stale = _native_move_leaves_the_arm_stale(harness)
    harness.trigger("n2", episode=1)
    (record,) = _refresh_records(harness)
    _drained(harness)
    admission = [line for line in harness.logs.events("admission") if "reason=not_armed" in line]
    assert admission

    def field(line: str, name: str) -> str:
        match = re.search(rf"(?:\(| ){name}=([^ )]+)", line)
        assert match, (name, line)
        return match.group(1)

    for name in ("v", "mono_ns", "instance", "seq", "loss_total", "observer", "scope", "scope_seq",
                 "policy_revision", "cause", "leaf", "rule", "episode", "arm_check", "route_epoch",
                 "arm_route_epoch", "snapshot_epoch", "key_attempt", "disposition", "key_on", "source_was_ready"):
        field(record, name)
    assert field(record, "v") == field(admission[0], "v")
    assert field(record, "instance") == field(admission[0], "instance")
    assert field(record, "leaf") == field(admission[0], "leaf")
    assert field(record, "loss_total") == "0"
    assert int(field(record, "mono_ns")) > 0 and int(field(record, "seq")) > 0
    assert int(field(record, "seq")) != int(field(admission[0], "seq"))
    assert field(record, "rule") == "FAST" and field(record, "episode") == "1"
    assert int(field(record, "arm_route_epoch")) == stale.route_epoch
    assert int(field(record, "route_epoch")) > stale.route_epoch
    assert int(field(record, "snapshot_epoch")) == int(field(record, "route_epoch"))


def test_runtime_raising_kick_never_changes_the_stale_arm_refresh(harness_factory) -> None:
    harness = harness_factory(flags=REARM)
    runtime = harness.runtime
    controller = harness.controller

    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("diagnostics kick broke")

    runtime._node_judgement_arm_kick = broken
    _native_move_leaves_the_arm_stale(harness)
    controller.modes["n2"] = "zombie"
    remembered = _spy_remember(harness)
    before = controller.calls.count(("proxies",))

    harness.trigger("n2", episode=1)
    assert "reason=not_armed" in harness.logs.events("gated")[0]
    assert _wait_for(lambda: runtime._runtime_recovery_is_armed())
    assert controller.calls.count(("proxies",)) == before + 1
    assert remembered == []
    harness.trigger("n2", episode=1, new_episode=False)
    assert harness.judge.metrics["judgements"] == 1
    harness.settle()
    assert controller.writes == []
    assert _summary(harness)["judge_refresh_queued_not_armed"] == 1
