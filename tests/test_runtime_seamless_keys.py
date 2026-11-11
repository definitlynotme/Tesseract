"""10-28 seamless keys (each default False, judge act only):

* ``post_dial_provisional_move`` - ``Judgement`` emits ``ProvisionalMove``
  when A_C has no result PROVISIONAL_MOVE_S after issue and S_C0 passed; the
  ladder service calls the ``provisional_move`` hook once (rung 1 only); the
  verdict and the action sequence are otherwise unchanged.
* ``post_dial_successor_from_history`` - a fresh alive provider history
  stands in for S_C0 PASS (``history_fresh``, ``successor_history`` hook).
* ``post_dial_fast_successor`` - S_C0_DELAY_S capped at 1.0 s.
* ``post_dial_close_frozen_streams`` - demotion-watch rule 6.
"""

from __future__ import annotations

import inspect
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import pytest

import runtime_post_dial_judge as judge_module
from runtime_node_judge import (
    CLOSE,
    Done,
    Judgement,
    NodeJudgementPolicy,
    ProbeClass,
    ProvisionalMove,
    StartProbe,
    close_record,
    select_stuck_closes,
)
from runtime_post_dial import (
    DEFAULT_POST_DIAL_CONSTANTS,
    PostDialTokenizer,
    StuckConnections,
    constants_with,
)
from runtime_post_dial_judge import history_fresh
from tests.test_runtime_post_dial import _core, _parse, replay, T0
from tests.test_runtime_tier_ladder_judge import (
    Harness,
    instant,
    t2_rung,
    pf_rung,
)

PASS, TIMEOUT, FAIL = ProbeClass.PASS, ProbeClass.TIMEOUT, ProbeClass.TRANSPORT_FAIL
NEW_KEYS = (
    "post_dial_provisional_move",
    "post_dial_fast_successor",
    "post_dial_successor_from_history",
    "post_dial_close_frozen_streams",
)


# ---------------------------------------------------------------------------
# Keys and constants


def test_new_keys_default_false_and_parse_independently() -> None:
    # 11-01 user decision (after ship3): every new key but post_dial_fast_successor
    # defaults True.
    expected = {key: key != "post_dial_fast_successor" for key in NEW_KEYS}
    default = NodeJudgementPolicy()
    assert {key: getattr(default, key) for key in NEW_KEYS} == expected
    assert {key: getattr(NodeJudgementPolicy.from_config({}), key) for key in NEW_KEYS} == expected
    for key in NEW_KEYS:
        cfg = {"client": {"egress_recovery": {"node_judgement": {key: True}}}}
        policy = NodeJudgementPolicy.from_config(cfg)
        assert getattr(policy, key) is True
        cfg = {"client": {"egress_recovery": {"node_judgement": {key: False}}}}
        assert getattr(NodeJudgementPolicy.from_config(cfg), key) is False
        assert all(getattr(policy, other) is expected[other] for other in NEW_KEYS if other != key)
    off = {"client": {"egress_recovery": {"node_judgement": {"enabled": False, **{k: True for k in NEW_KEYS}}}}}
    assert all(getattr(NodeJudgementPolicy.from_config(off), key) is False for key in NEW_KEYS)


def test_new_constants() -> None:
    c = DEFAULT_POST_DIAL_CONSTANTS
    assert (c.provisional_move_s, c.successor_history_max_age_s) == (2.5, 60.0)
    assert (c.fast_successor_s_c0_delay_s, c.stall_close_s) == (1.0, 2.0)
    # Existing constants unchanged.
    assert (c.s_c0_delay_s, c.progress_window_s, c.stream_rate_bps) == (2.0, 2.0, 65536.0)


# ---------------------------------------------------------------------------
# Judgement: ProvisionalMove


def _judgement(prov: float | None, history: bool = False) -> Judgement:
    return Judgement(
        "L",
        kind="AGED",
        tokens=("t",),
        ticket_deadline_perf=100.0 + 6.0,
        provisional_move_s=prov,
        history_candidate=history,
    )


def _drive(j: Judgement, script: list[tuple[str, Any]]) -> list[Any]:
    """Feed a scripted sequence; returns every action."""

    out: list[Any] = []
    out += j.start(100.0, 1000.0)
    for step, arg in script:
        if step == "route":
            out += j.on_route_state(("L", "S", "T"), {"L": True, "S": True, "T": True}, arg)
        elif step == "route_unavailable":
            out += j.on_route_state_unavailable(arg)
        elif step == "timer":
            out += j.on_timer(arg)
        else:
            name, value, at = step, arg[0], arg[1]
            out += j.on_probe_result(name, value, at)
    return out


def _strip(actions: list[Any]) -> list[Any]:
    return [a for a in actions if not isinstance(a, ProvisionalMove)]


SCRIPTS = {
    # S_C0 passes at 3.2 s, A_C passes late at 4.0 s -> NOT_REPRODUCED.
    "late_pass": [("route", 100.1), ("timer", 102.0), ("S_C0", (PASS, 103.2)), ("A_C", (PASS, 104.0))],
    # S_C0 passes at 3.2 s, A_C times out at 5.0 s -> CONVICT on S.
    "late_timeout": [("route", 100.1), ("timer", 102.0), ("S_C0", (PASS, 103.2)), ("A_C", (TIMEOUT, 105.0))],
    # No route read: no successor, no move.
    "no_s_c0": [("route_unavailable", 100.1), ("timer", 102.0), ("timer", 102.5), ("A_C", (TIMEOUT, 105.0))],
    # S_C0 passes before T_PROV: the timer at 2.5 s moves.
    "early_s_c0": [("route", 100.1), ("timer", 102.0), ("S_C0", (PASS, 102.2)), ("timer", 102.5), ("A_C", (PASS, 103.0))],
}


@pytest.mark.parametrize("name", sorted(SCRIPTS))
def test_provisional_move_changes_no_other_action_or_verdict(name: str) -> None:
    off = _judgement(None)
    on = _judgement(2.5)
    a_off = _drive(off, SCRIPTS[name])
    a_on = _drive(on, SCRIPTS[name])
    assert not any(isinstance(a, ProvisionalMove) for a in a_off)
    assert _strip(a_on) == a_off
    assert on.done == off.done
    moves = [a for a in a_on if isinstance(a, ProvisionalMove)]
    if name == "no_s_c0":
        assert moves == []
    else:
        assert len(moves) == 1 and moves[0].successor == "S" and moves[0].basis == "s_c0"


def test_provisional_move_never_after_an_accused_result() -> None:
    j = _judgement(2.5)
    actions = _drive(j, [("route", 100.1), ("A_C", (PASS, 101.0)), ("timer", 102.5)])
    assert not any(isinstance(a, ProvisionalMove) for a in actions)


def test_provisional_wakeup_fires_once_and_is_absent_when_off() -> None:
    off, on = _judgement(None), _judgement(2.5)
    for j in (off, on):
        j.start(100.0, 1000.0)
        j.on_route_state(("L", "S"), {"L": True, "S": True}, 100.1)
        j.on_timer(102.0)  # S_C0 starts
    assert off.next_wakeup() == on.t_evid == off.t_evid
    assert on.next_wakeup() == pytest.approx(102.5)
    on.on_timer(102.5)
    assert on.next_wakeup() == on.t_evid  # no busy loop


def test_history_candidate_asks_once_then_s_c0_still_moves() -> None:
    j = _judgement(2.5, history=True)
    actions = _drive(j, [("route", 100.1), ("timer", 102.0), ("timer", 102.5)])
    moves = [a for a in actions if isinstance(a, ProvisionalMove)]
    assert [(m.successor, m.basis) for m in moves] == [("S", "history")]
    # History was not fresh (the service did not call note_provisional_moved):
    later = j.on_probe_result("S_C0", PASS, 103.2)
    assert [(m.successor, m.basis) for m in later if isinstance(m, ProvisionalMove)] == [("S", "s_c0")]
    # Once moved, nothing more.
    j2 = _judgement(2.5, history=True)
    _drive(j2, [("route", 100.1), ("timer", 102.0), ("timer", 102.5)])
    j2.note_provisional_moved()
    assert not [a for a in j2.on_probe_result("S_C0", PASS, 103.2) if isinstance(a, ProvisionalMove)]


def _rfc(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def test_history_fresh() -> None:
    url = "http://c.test/generate_204"
    now = 1_790_000_000.0

    def entry(alive: Any, samples: list[tuple[float, int]]) -> dict[str, Any]:
        return {"extra": {url: {"alive": alive, "history": [{"time": _rfc(t), "delay": d} for t, d in samples]}}}

    assert history_fresh(entry(True, [(now - 30, 120)]), url, now_wall=now, max_age_s=60.0)
    assert not history_fresh(entry(True, [(now - 61, 120)]), url, now_wall=now, max_age_s=60.0)  # stale
    assert not history_fresh(entry(False, [(now - 5, 120)]), url, now_wall=now, max_age_s=60.0)  # dead
    assert not history_fresh(entry(True, [(now - 50, 120), (now - 5, 0)]), url, now_wall=now, max_age_s=60.0)
    assert history_fresh(entry(True, [(now - 50, 0), (now - 5, 90)]), url, now_wall=now, max_age_s=60.0)
    assert not history_fresh(entry(True, []), url, now_wall=now, max_age_s=60.0)
    assert not history_fresh({"extra": {}}, url, now_wall=now, max_age_s=60.0)
    assert not history_fresh(None, url, now_wall=now, max_age_s=60.0)


# ---------------------------------------------------------------------------
# Service (tier ladder, real worker threads, fake hooks)


def _ladder(provisional: bool, *, history: bool = False, fresh: bool = False, ac=None, s_c0=None) -> tuple[Harness, list, list]:
    # Rung 1 with T = 800 ms (S_C0 delay 0.4 s); PROVISIONAL_MOVE_S 0.6 s.
    script = {
        ("TIER2-AUTO", "t2a"): ac if ac is not None else (0.9, TIMEOUT, 0.0),
        ("TIER2-AUTO", "t2b"): s_c0 if s_c0 is not None else instant(PASS),
    }
    h = Harness((t2_rung(), pf_rung()), script, constants=constants_with(provisional_move_s=0.6))
    moves: list = []
    asked: list = []

    def move(job, successor):
        moves.append((job.ladder.index, successor))
        return True, "moved"

    def hist(job, successor):
        asked.append(successor)
        return fresh

    h.service._hooks = replace(h.service._hooks, provisional_move=move, successor_history=hist)
    h.service.provisional_move_on = provisional
    h.service.successor_history_on = history
    return h, moves, asked


def test_service_provisional_move_once_on_rung1_then_the_verdict_decides() -> None:
    h, moves, asked = _ladder(True)
    h.run()
    assert moves == [(0, "t2b")] and asked == []
    rec = h.ev("provisional_move")
    assert len(rec) == 1 and rec[0]["moved"] is True and rec[0]["basis"] == "s_c0" and rec[0]["rung"] == 1
    assert rec[0]["t"] >= rec[0]["t_start"]  # stamped after the hook returned
    assert h.job.provisional == "t2b"
    assert h.service.metrics["provisional_moves"] == 1
    # A_C timed out: CONVICT on t2b and the commit hook ran as without the key.
    assert h.ev("rung_verdict")[0]["verdict"] == "CONVICT_CONTRACT"
    assert h.fake.commits


def test_service_key_off_never_calls_the_hooks() -> None:
    h, moves, asked = _ladder(False, history=True, fresh=True)
    h.run()
    assert moves == [] and asked == [] and h.ev("provisional_move") == []
    assert h.ev("rung_verdict")[0]["verdict"] == "CONVICT_CONTRACT"


def test_service_late_pass_keeps_not_reproduced() -> None:
    h, moves, _ = _ladder(True, ac=(0.9, PASS, 0.0))
    h.run()
    assert moves == [(0, "t2b")]
    assert h.ev("rung_verdict")[0]["verdict"] == "NOT_REPRODUCED"
    assert not h.fake.commits


def test_service_history_basis_moves_before_s_c0_when_fresh() -> None:
    # S_C0 still in flight at 0.6 s (it takes 0.6 s).
    h, moves, asked = _ladder(False, history=True, fresh=True, s_c0=(0.6, PASS, 0.0))
    h.service.provisional_move_on = True
    h.run()
    assert asked == ["t2b"] and moves == [(0, "t2b")]
    assert h.ev("provisional_move")[0]["basis"] == "history"


def test_service_history_stale_falls_back_to_s_c0() -> None:
    h, moves, asked = _ladder(True, history=True, fresh=False, s_c0=(0.3, PASS, 0.0), ac=(1.2, TIMEOUT, 0.0))
    h.run()
    assert asked == ["t2b"]
    recs = h.ev("provisional_move")
    assert [(r["basis"], r["moved"], r["reason"]) for r in recs] == [
        ("history", False, "history_not_fresh"),
        ("s_c0", True, "moved"),
    ]
    assert moves == [(0, "t2b")]


def test_service_refused_by_ladder_check_does_not_move() -> None:
    h, moves, _ = _ladder(True)
    h.fake.check = lambda job: "d4_txn_busy:recovery_worker" if job.ladder is not None and h.clock() > 0 and len(h.fake.checks) > 2 else None
    h.run()
    assert moves == []


def test_fast_successor_caps_the_rung_s_c0_delay() -> None:
    h, _, _ = _ladder(False)
    rung5000 = t2_rung(timeout_ms=5000)
    assert h.service._rung_judgement_constants(rung5000, False).s_c0_delay_s == 2.0
    h.service.fast_successor_on = True
    assert h.service._rung_judgement_constants(rung5000, False).s_c0_delay_s == 1.0
    assert h.service._rung_judgement_constants(t2_rung(), False).s_c0_delay_s == 0.4
    assert h.service._judgement_constants().s_c0_delay_s == 1.0
    h.service.fast_successor_on = False
    assert h.service._judgement_constants() is h.service.constants


# ---------------------------------------------------------------------------
# Rule 6: frozen streams


def _frozen_core(on: bool):
    core = _core()
    core.close_frozen_streams = on
    mihomo = replay.FakeMihomo()
    t = T0
    specs = {
        # 2 MB/s for 2 s, then nothing (frozen).
        "frozen": dict(up=replay.step(t, 900), down=lambda x: int(min(x - t, 2.0) * 2_000_000)),
        # still growing
        "growing": dict(up=replay.step(t, 900), down=lambda x: int((x - t) * 2_000_000)),
        # small trickle, never at stream rate, then idle
        "trickle": dict(up=replay.step(t, 900), down=replay.step(t + 0.3, 6000)),
    }
    for index, (name, fns) in enumerate(specs.items()):
        mihomo.add(replay.FakeConn(name, t, 47000 + index, f"{name}.example", 443, "n1", **fns))
        core.ingest_open(_parse(replay.open_line(t, sport=47000 + index, host=f"{name}.example", leaf="n1")), now_wall=t, now_perf=t)
    for step_t in (0.1, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0):
        core.ingest_snapshot(mihomo.snapshot(t + step_t), t + step_t, t + step_t)
    late = mihomo.add(
        replay.FakeConn("after", t + 5.1, 47100, "after.example", 443, "n1",
                        up=replay.step(t + 5.1, 900), down=lambda x: int(min(max(0.0, x - t - 5.1), 0.1) * 2_000_000))
    )
    del late
    core.ingest_snapshot(mihomo.snapshot(t + 5.3), t + 5.3, t + 5.3)
    return core, t


def test_rule6_closes_only_frozen_streams_and_only_with_the_key() -> None:
    core, t = _frozen_core(True)
    stuck = core.stuck_connection_ids("n1", t_demote_wall=t + 5.05, now_wall=t + 5.3)
    assert stuck.ids == ("frozen",) and stuck.frozen == 1
    assert stuck.kept_progressing == 1  # growing
    assert stuck.kept_not_stuck == 1  # trickle
    assert stuck.kept_after_demotion == 1  # opened after the demotion
    off, _ = _frozen_core(False)
    stuck_off = off.stuck_connection_ids("n1", t_demote_wall=t + 5.05, now_wall=t + 5.3)
    assert stuck_off.ids == () and stuck_off.frozen == 0 and stuck_off.kept_not_stuck == 2


def test_rule6_close_decision_counts_frozen_separately() -> None:
    def make(ids, aged, waiting, frozen):
        return StuckConnections(ids=ids, aged=aged, waiting=waiting, kept_progressing=0, kept_not_stuck=0,
                                kept_after_demotion=0, kept_other=0, snapshot_wall=time.time(), frozen=frozen)

    kwargs = dict(enabled=True, successor_probe_pending=False, successor_timed_out=False,
                  demotion_confirmed=True, readback_age_s=0.1)
    d = select_stuck_closes(make(("a", "w", "f"), 1, 1, 1), **kwargs)
    assert d.action == CLOSE and (d.aged, d.waiting, d.frozen) == (1, 1, 1)
    rec = close_record(d, "n1", PostDialTokenizer(bytes(range(32))), now_wall=1.0)
    assert rec["frozen"] == 1
    d0 = select_stuck_closes(make(("a", "w"), 1, 1, 0), **kwargs)
    assert (d0.aged, d0.waiting, d0.frozen) == (1, 1, 0)
    assert "frozen" not in close_record(d0, "n1", PostDialTokenizer(bytes(range(32))), now_wall=1.0)
    # No move (no demotion readback): nothing is closed, frozen or not.
    no_move = select_stuck_closes(make(("f",), 0, 0, 1), **{**kwargs, "demotion_confirmed": False})
    assert no_move.ids == ()


# ---------------------------------------------------------------------------
# Wiring (source checks on the runtime)


def test_runtime_wires_the_keys_per_trigger_and_the_hooks() -> None:
    import desktop_runtime

    src = inspect.getsource(desktop_runtime)
    assert "judge.provisional_move_on = bool(fix_policy.post_dial_provisional_move)" in src
    assert "judge.successor_history_on = bool(fix_policy.post_dial_successor_from_history)" in src
    assert "judge.fast_successor_on = bool(fix_policy.post_dial_fast_successor)" in src
    assert "observer_core.close_frozen_streams = bool(fix_policy.post_dial_close_frozen_streams)" in src
    assert '("provisional_move", self._runtime_post_dial_provisional_move)' in src
    assert '("successor_history", self._runtime_post_dial_successor_history)' in src
    hist = inspect.getsource(desktop_runtime.DesktopRuntime._runtime_post_dial_successor_history) if hasattr(
        desktop_runtime, "DesktopRuntime") else ""
    if hist:
        assert "provider_proxies_one" in hist and "client.proxy(" not in hist and ".proxies(" not in hist


def _runtime_cls():
    import desktop_runtime

    for value in vars(desktop_runtime).values():
        if isinstance(value, type) and hasattr(value, "_runtime_post_dial_provisional_move"):
            return value
    raise AssertionError("runtime class not found")


class _FakeClient:
    def __init__(self, provider: Any) -> None:
        self.provider = provider
        self.calls: list[str] = []

    def provider_proxies_one(self, name, timeout=None):
        self.calls.append("provider:" + name)
        return self.provider

    def proxy(self, *a, **k):  # pragma: no cover - must not be called
        self.calls.append("proxy")
        raise AssertionError("GET /proxies/<name> is not allowed")


def test_successor_history_hook_reads_only_the_provider() -> None:
    from types import SimpleNamespace

    cls = _runtime_cls()
    url = "http://c.test/generate_204"
    now = time.time()
    provider = {"proxies": [{"name": "S", "extra": {url: {"alive": True, "history": [{"time": _rfc(now - 10), "delay": 80}]}}}]}
    client = _FakeClient(provider)
    rt = SimpleNamespace(
        _node_judgement_policy=lambda: NodeJudgementPolicy(post_dial_successor_from_history=True),
        _require_api_client=lambda: client,
        _runtime_post_dial_judge=lambda: SimpleNamespace(constants=DEFAULT_POST_DIAL_CONSTANTS),
    )
    job = SimpleNamespace(context=SimpleNamespace(provider_name="tier3-pool"), test_url=url)
    assert cls._runtime_post_dial_successor_history(rt, job, "S") is True
    assert client.calls == ["provider:tier3-pool"]
    rt._node_judgement_policy = lambda: NodeJudgementPolicy(post_dial_successor_from_history=False)
    assert cls._runtime_post_dial_successor_history(rt, job, "S") is False


def test_provisional_move_hook_refuses_without_key_or_lease_and_pins_via_the_lease_write() -> None:
    import threading
    from types import SimpleNamespace

    import runtime_tier_ladder as tl

    cls = _runtime_cls()
    rung = t2_rung()
    rung = replace(rung, group="TIER3-AUTO", kind=tl.RungKind.LEAF)
    applied: list = []
    ladder = SimpleNamespace(rungs=(rung,), index=0, current=rung, deadline=1e9, authority="A0")
    job = SimpleNamespace(ladder=ladder, context=SimpleNamespace(ticket=SimpleNamespace(deadline_monotonic=1e9)))
    state = {"policy": NodeJudgementPolicy(post_dial_provisional_move=False), "lease": True, "check": None}
    rt = SimpleNamespace(
        _node_judgement_policy=lambda: state["policy"],
        _route_lease_active=lambda: state["lease"],
        _runtime_ladder_current_rung=lambda lad: lad.current,
        _runtime_recovery_now=lambda: 0.0,
        _core_transition_lock=threading.RLock(),
        _runtime_post_dial_ladder_check=lambda j: state["check"],
        _apply_runtime_automatic_selection=lambda action, deadline_monotonic: applied.append(action) or {},
        _refresh_runtime_route_snapshot=lambda timeout_seconds: None,
        _runtime_post_dial_route_refs=lambda: ("SNAP", None),
        _runtime_post_dial_ladder_authority=lambda snap, key: ("A1", snap),
    )
    assert cls._runtime_post_dial_provisional_move(rt, job, "t2b") == (False, "key_off")
    state["policy"] = NodeJudgementPolicy(post_dial_provisional_move=True)
    state["lease"] = False
    assert cls._runtime_post_dial_provisional_move(rt, job, "t2b") == (False, "lease_off")
    state["lease"] = True
    state["check"] = "d4_txn_busy_recovery_worker"
    assert cls._runtime_post_dial_provisional_move(rt, job, "t2b") == (False, "d4_txn_busy_recovery_worker")
    state["check"] = None
    assert cls._runtime_post_dial_provisional_move(rt, job, "t2a") == (False, "not_rung1_leaf")
    assert cls._runtime_post_dial_provisional_move(rt, job, "zz") == (False, "not_rung1_leaf")
    assert applied == []
    # A settled D4 route-mutation lease owns the pick: refused, nothing written.
    rt._runtime_failure_monitor_lock = threading.RLock()
    rt._runtime_route_mutation_lease = {"mode": "fixed", "group": "TIER3-AUTO", "to": "t2c"}
    assert cls._runtime_post_dial_provisional_move(rt, job, "t2b") == (False, "d4_lease_present")
    rt._runtime_route_mutation_lease = None
    # The accused is itself pinned (fixed0 == accused): refused (the rung commit
    # must clear that pin; a provisional pin would read pin_drop_unsettled).
    ladder.current = replace(rung, fixed0="t2a")
    assert cls._runtime_post_dial_provisional_move(rt, job, "t2b") == (False, "accused_pinned")
    ladder.current = rung
    assert applied == []
    assert cls._runtime_post_dial_provisional_move(rt, job, "t2b") == (True, "moved")
    assert applied == [{"group": "TIER3-AUTO", "target": "t2b", "previous": "t2a", "mode": "fixed", "alternative_leaf": "t2b"}]
    assert ladder.authority == ("A1", "SNAP")
    # Never a dead write or a close: the hook's source names neither.
    body = inspect.getsource(cls._runtime_post_dial_provisional_move).split('"""')[-1]
    assert "delay" not in body and "close" not in body and "dead\"" not in body
