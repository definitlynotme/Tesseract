"""10-28 ship2 fixes 1-3 (user decision 用户决定_V6判据与修复_20261028).

1. Pool re-export when the loaded TIER3-AUTO group is exhausted or frozen
   (judge act + tier_ladder_judge only; once per POOL_REEXPORT_INTERVAL_S).
2. EXHAUSTED hold at admission when the published group is exhausted (same
   key), and at rung 1 when the accused failed and S_C0 timed out (ladder
   service; see test_runtime_tier_ladder_judge.py).
3. A clearly failing FAST trigger passes a NOT_REPRODUCED back-off once per
   leaf per window.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

import desktop_runtime
from desktop_runtime import DesktopRuntime, ProviderSyncDisposition, _ProviderSyncOutcome
from runtime_node_judge import (
    GATE_EXHAUSTED_HOLD,
    GATE_LEAF_BACKOFF,
    GATE_LEAF_CHECK_CAP,
    JudgeGates,
    NodeJudgementPolicy,
    ProbeClass,
    Verdict,
    successor_timeout_exhausts,
)
from runtime_post_dial import DEFAULT_POST_DIAL_CONSTANTS, PostDialTokenizer, constants_with
from runtime_post_dial_judge import PostDialJudgeHooks, PostDialJudgeService
from tests.real_route_publication import captured_proxies, published_runtime

TOKENIZER = PostDialTokenizer(bytes(range(32)))


def _strong(**kw: Any) -> SimpleNamespace:
    values = {"kind": "FAST", "share_failed": 4, "share_answered": 1}
    values.update(kw)
    return SimpleNamespace(**values)


def _service(constants=DEFAULT_POST_DIAL_CONSTANTS, clock=None) -> tuple[PostDialJudgeService, list]:
    records: list[tuple[str, dict]] = []
    hooks = PostDialJudgeHooks(
        probe=lambda job, action: None,
        read_route=lambda job: None,
        read_leaf=lambda job, leaf: None,
        commit=lambda job, done: (False, "x"),
        finish=lambda job, reason: None,
        close=lambda job, leaf, ids: (),
        close_enabled=lambda: False,
    )
    service = PostDialJudgeService(
        hooks=hooks,
        tokenizer=TOKENIZER,
        emit=lambda level, record: records.append((level, dict(record))),
        constants=constants,
        clock=clock or (lambda: 1000.0),
    )
    service.backoff_override_on = True
    return service, records


# ---------------------------------------------------------------------------
# constants


def test_constants_defaults_and_replace() -> None:
    c = DEFAULT_POST_DIAL_CONSTANTS
    assert c.not_reproduced_override_min_share == 0.8
    assert c.not_reproduced_override_min_n == 4
    assert c.pool_reexport_interval_s == 120.0
    assert c.pool_reexport_max_alive == 1
    assert c.pool_reexport_min_surplus == 4
    # unchanged neighbours
    assert c.not_reproduced_backoff_s == 420.0 and c.leaf_backoff_s == 10.0
    assert c.leaf_check_cap == 3 and c.global_max == 10
    policy = NodeJudgementPolicy(post_dial_judge="act")
    assert policy.constants().pool_reexport_interval_s == 120.0
    base = constants_with(pool_reexport_interval_s=5.0, not_reproduced_override_min_n=9)
    assert policy.constants(base).pool_reexport_interval_s == 5.0
    assert policy.constants(base).not_reproduced_override_min_n == 9


# ---------------------------------------------------------------------------
# fix 3: gates


def _nr(gates: JudgeGates, leaf: str, at: float) -> None:
    gates.begin(leaf, at)
    gates.finish(leaf, Verdict.NOT_REPRODUCED, (), at + 1.0)


def test_override_passes_a_not_reproduced_backoff_once_per_window() -> None:
    gates = JudgeGates()
    _nr(gates, "n1", 100.0)  # back-off until 521
    assert gates.blocked_reason("n1", 200.0, armed=True, d4_busy=False) == GATE_LEAF_BACKOFF
    assert gates.blocked_reason("n1", 200.0, armed=True, d4_busy=False, backoff_override=True) is None
    assert gates.begin("n1", 200.0) is True
    gates.finish("n1", Verdict.CONVICT_CONTRACT, (), 201.0)
    # spent: the same window blocks even a strong trigger
    assert gates.blocked_reason("n1", 300.0, armed=True, d4_busy=False, backoff_override=True) == GATE_LEAF_BACKOFF
    # after the window everything is open again
    assert gates.blocked_reason("n1", 521.0, armed=True, d4_busy=False) is None


def test_override_not_reproduced_opens_a_window_with_the_override_spent() -> None:
    gates = JudgeGates()
    _nr(gates, "n1", 100.0)
    assert gates.begin("n1", 200.0) is True
    gates.finish("n1", Verdict.NOT_REPRODUCED, (), 201.0)  # new window to 621
    assert gates._backoff["n1"] == 621.0
    assert gates.blocked_reason("n1", 300.0, armed=True, d4_busy=False, backoff_override=True) == GATE_LEAF_BACKOFF
    # a normal (non-override) NOT_REPRODUCED later gets its own override again
    gates2 = JudgeGates()
    _nr(gates2, "n1", 0.0)
    _nr(gates2, "n1", 500.0)
    assert gates2.blocked_reason("n1", 600.0, armed=True, d4_busy=False, backoff_override=True) is None


def test_override_never_passes_an_indeterminate_backoff_or_the_caps() -> None:
    gates = JudgeGates()
    gates.begin("n1", 100.0)
    gates.finish("n1", Verdict.INDETERMINATE, (), 101.0)
    assert gates.blocked_reason("n1", 105.0, armed=True, d4_busy=False, backoff_override=True) == GATE_LEAF_BACKOFF
    gates = JudgeGates()
    for at in (0.0, 1.0):
        gates.begin("n1", at)
        gates.finish("n1", Verdict.CONVICT_CONTRACT, (), at + 0.5)
    _nr(gates, "n1", 2.0)  # third check in 600 s
    assert gates.blocked_reason("n1", 50.0, armed=True, d4_busy=False, backoff_override=True) == GATE_LEAF_CHECK_CAP
    # holds still come first
    gates = JudgeGates()
    _nr(gates, "n1", 0.0)
    gates.set_hold(Verdict.EXHAUSTED, 5.0)
    assert gates.blocked_reason("n1", 6.0, armed=True, d4_busy=False, backoff_override=True) == GATE_EXHAUSTED_HOLD


def test_abandon_clears_the_override_flag() -> None:
    gates = JudgeGates()
    _nr(gates, "n1", 0.0)
    assert gates.begin("n1", 10.0) is True
    gates.abandon("n1")
    assert gates._override_in_flight is None
    assert gates.begin("n2", 11.0) is False


def test_without_override_flag_gates_are_unchanged() -> None:
    gates = JudgeGates()
    _nr(gates, "n1", 0.0)
    assert gates.begin("n2", 2.0) is False
    gates.abandon("n2")
    for at in (5.0, 100.0, 420.9):
        assert gates.blocked_reason("n1", at, armed=True, d4_busy=False) == GATE_LEAF_BACKOFF
    assert gates.blocked_reason("n1", 421.0, armed=True, d4_busy=False) is None


# ---------------------------------------------------------------------------
# fix 3: service eligibility and wiring


@pytest.mark.parametrize(
    ("trigger", "eligible"),
    [
        (_strong(), True),                                        # 4/5 = 0.8
        (_strong(share_failed=4, share_answered=0), True),        # n = 4
        (_strong(share_failed=3, share_answered=0), False),       # n = 3
        (_strong(share_failed=3, share_answered=1), False),       # 0.75
        (_strong(kind="AGED"), False),
        (_strong(kind="FAST_SPREAD"), False),
        (_strong(share_failed=None), False),
        (_strong(share_answered=None), False),
        (None, False),
    ],
)
def test_backoff_override_eligibility(trigger: Any, eligible: bool) -> None:
    service, _ = _service()
    assert service.backoff_override_eligible(trigger) is eligible


def test_eligibility_follows_the_constants() -> None:
    service, _ = _service(constants_with(not_reproduced_override_min_n=6, not_reproduced_override_min_share=0.5))
    assert service.backoff_override_eligible(_strong()) is False
    assert service.backoff_override_eligible(_strong(share_failed=3, share_answered=3)) is True


def test_service_blocked_paths_pass_the_trigger() -> None:
    service, _ = _service()
    _nr(service.gates, "n1", 900.0)
    assert service.blocked_cheap("n1", 1000.0) == GATE_LEAF_BACKOFF
    assert service.blocked_cheap("n1", 1000.0, _strong(kind="AGED")) == GATE_LEAF_BACKOFF
    assert service.blocked_cheap("n1", 1000.0, _strong()) is None
    assert service.blocked("n1", 1000.0, armed=True, d4_busy=False, status_failure=False) == GATE_LEAF_BACKOFF
    assert service.blocked("n1", 1000.0, armed=True, d4_busy=False, status_failure=False, trigger=_strong()) is None


def test_service_start_logs_the_override_once() -> None:
    service, records = _service()
    _nr(service.gates, "n1", 900.0)
    started = threading.Event()
    service._run = lambda job: started.set()  # no real judgement
    job = SimpleNamespace(leaf="n1", kind="FAST", ladder=None)
    assert service.start(job)
    assert started.wait(2.0)
    overrides = [r for _lvl, r in records if r.get("ev") == "backoff_override"]
    assert len(overrides) == 1 and overrides[0]["rule"] == "FAST"
    assert service.metrics["backoff_override"] == 1
    service.gates.abandon("n1")
    service._worker.join(2.0)


# ---------------------------------------------------------------------------
# fix 2: pure helper and the admission hold


def test_successor_timeout_exhausts() -> None:
    T, F, P, A = ProbeClass.TIMEOUT, ProbeClass.TRANSPORT_FAIL, ProbeClass.PASS, ProbeClass.HTTP_ANSWER
    ind = Verdict.INDETERMINATE
    assert successor_timeout_exhausts(ind, {"A_C": F, "S_C0": T}) is True
    assert successor_timeout_exhausts(ind, (("A_C", "transport_fail"), ("S_C0", "timeout"))) is True
    assert successor_timeout_exhausts(ind, {"A_C": T, "S_C0": T}) is True
    assert successor_timeout_exhausts(ind, {"A_C": P, "S_C0": T}) is False
    assert successor_timeout_exhausts(ind, {"A_C": A, "S_C0": T}) is False
    assert successor_timeout_exhausts(ind, {"A_C": F, "S_C0": F}) is False
    assert successor_timeout_exhausts(ind, {"A_C": F}) is False
    assert successor_timeout_exhausts(ind, {"A_C": F, "S_C1": T}) is False
    for other in (Verdict.NOT_REPRODUCED, Verdict.CONVICT_CONTRACT, Verdict.FACILITY, Verdict.EXHAUSTED, None):
        assert successor_timeout_exhausts(other, {"A_C": F, "S_C0": T}) is False


def test_enter_exhausted_hold_blocks_until_an_answer() -> None:
    now = [1000.0]
    service, records = _service(clock=lambda: now[0])
    assert service.enter_exhausted_hold("n1", "group_exhausted") is True
    assert service.gates.hold is Verdict.EXHAUSTED
    assert service.blocked_cheap("n2", 1001.0) == GATE_EXHAUSTED_HOLD
    held = [r for _lvl, r in records if r.get("ev") == "hold_entered"]
    assert held and held[0]["cause"] == "group_exhausted" and held[0]["hold"] == "EXHAUSTED"
    # entering again while held is a no-op
    assert service.enter_exhausted_hold("n1", "group_exhausted") is False
    now[0] = 1002.0
    assert service.gates.note_answered(1002.0) is True
    assert service.blocked_cheap("n2", 1003.0) is None


def test_enter_exhausted_hold_never_while_judging() -> None:
    service, _ = _service()
    service.gates.begin("n1", 999.0)
    assert service.enter_exhausted_hold("n1", "group_exhausted") is False
    assert service.gates.hold is None


# ---------------------------------------------------------------------------
# fixes 1 and 2: desktop wiring (unbound DesktopRuntime methods on a stub)


_PUBLISHED: dict[str, tuple[Any, dict]] = {}


def _published(state: str) -> tuple[Any, dict]:
    """(snapshot, stored payload) of the REAL route publication of a live
    Mihomo 1.19.27 ``GET /proxies`` capture (10-30: never a hand-made
    payload; see tests/real_route_publication.py).  Cached per state."""

    if state not in _PUBLISHED:
        import tempfile
        from pathlib import Path

        published = published_runtime(Path(tempfile.mkdtemp(prefix="ship2-fixes-")), state)
        published.runtime.stop_event.set()
        _PUBLISHED[state] = published.runtime._runtime_post_dial_route_refs()
    return _PUBLISHED[state]


class _Stub:
    _runtime_pool_reexport_observe = DesktopRuntime._runtime_pool_reexport_observe
    _runtime_pool_reexport_worker = DesktopRuntime._runtime_pool_reexport_worker
    _runtime_post_dial_exhausted_admission = DesktopRuntime._runtime_post_dial_exhausted_admission
    # The real view over the real publication (the shape the bug was in).
    _runtime_tier3_route_view = DesktopRuntime._runtime_tier3_route_view

    def __init__(self, *, policy: NodeJudgementPolicy, state: str, healthy: int = 30,
                 phase: str = "formal_no_capture", activated: bool = True,
                 outcome: Any = None) -> None:
        self.policy = policy
        self.healthy = healthy
        self.phase = phase
        self.activated = activated
        self.now = 1000.0
        self.syncs: list[dict] = []
        self.records: list[dict] = []
        self.events: list[str] = []
        self.stop_event = threading.Event()
        self.initial_tx = False
        self.recovery_tx = False
        self.state = SimpleNamespace(judge=None)
        self.outcome = outcome or _ProviderSyncOutcome(ProviderSyncDisposition.COMMITTED)
        self.publish(state)

    def publish(self, state: str) -> None:
        """Switch to the real publication of ``state``: the hooks under test
        get the controller response (publication) or the stored wrapper
        (admission refs) exactly as production hands them over."""

        self.snapshot, self.stored = _published(state)
        self.payload = self.stored["proxies"]  # what publication hooks receive

    def _node_judgement_policy(self):
        return self.policy

    def _runtime_recovery_now(self):
        return self.now

    def _tier3_core_gate_check(self, nodes=None):
        return False, self.healthy, 3, "core_already_running"

    def _node_judgement_state(self):
        return self.state

    def _current_core_runtime_phase(self):
        return self.phase

    def _runtime_initial_activation_was_completed(self):
        return self.activated

    def _tier3_initial_activation_transaction_active(self):
        return self.initial_tx

    def _runtime_recovery_transaction_active(self):
        return self.recovery_tx

    def _sync_core_config_after_pool_update(self, **kwargs):
        self.syncs.append(kwargs)
        return self.outcome

    def _record_tier3_core_sync_event(self, event):
        self.events.append(event)

    def _log_runtime_post_dial_record(self, level, record):
        self.records.append(dict(record))

    def _runtime_post_dial_route_refs(self):
        return self.snapshot, self.stored

    def wait(self) -> None:
        thread = self.__dict__.get("_runtime_pool_reexport_thread")
        if thread is not None:
            thread.join(5.0)


ACT_LADDER = NodeJudgementPolicy(post_dial_judge="act", tier_ladder_judge=True,
                                 exhausted_pool_reexport=True, exhausted_hold=True)


def test_reexport_on_exhausted_group_uses_the_existing_sync_once_per_interval() -> None:
    stub = _Stub(policy=ACT_LADDER, state="exhausted")
    c = ACT_LADDER.constants()
    stub._runtime_pool_reexport_observe(stub.snapshot, stub.payload, ACT_LADDER)
    stub.wait()
    assert stub.syncs == [{"allow_live_rebase": True}]
    assert stub.events == ["semantic_change"]
    assert stub.records[-1]["ev"] == "pool_reexport" and stub.records[-1]["outcome"] == "committed"
    assert stub.records[-1]["exhausted"] is True and stub.records[-1]["healthy"] == 30
    assert (stub.records[-1]["alive"], stub.records[-1]["loaded"]) == (0, 3)
    # rate limit: a second publish within 120 s does nothing
    stub.now += c.pool_reexport_interval_s - 0.1
    stub._runtime_pool_reexport_observe(stub.snapshot, stub.payload, ACT_LADDER)
    stub.wait()
    assert len(stub.syncs) == 1
    stub.now += 0.1
    stub._runtime_pool_reexport_observe(stub.snapshot, stub.payload, ACT_LADDER)
    stub.wait()
    assert len(stub.syncs) == 2


def test_reexport_on_frozen_group_needs_a_clearly_larger_pool() -> None:
    stub = _Stub(policy=ACT_LADDER, state="one_alive", healthy=7)  # 7 - 3 loaded = 4 surplus
    stub._runtime_pool_reexport_observe(stub.snapshot, stub.payload, ACT_LADDER)
    stub.wait()
    assert len(stub.syncs) == 1
    stub = _Stub(policy=ACT_LADDER, state="one_alive", healthy=6)
    stub._runtime_pool_reexport_observe(stub.snapshot, stub.payload, ACT_LADDER)
    stub.wait()
    assert stub.syncs == [] and stub.records[-1]["outcome"] == "skipped_pool_not_larger"
    # alive 2 never starts a worker (no record at all)
    stub = _Stub(policy=ACT_LADDER, state="two_alive", healthy=40)
    stub._runtime_pool_reexport_observe(stub.snapshot, stub.payload, ACT_LADDER)
    stub.wait()
    assert stub.syncs == [] and stub.records == []


@pytest.mark.parametrize(
    ("setup", "outcome"),
    [
        (lambda s: setattr(s, "initial_tx", True), "skipped_initial_activation"),
        (lambda s: setattr(s, "recovery_tx", True), "skipped_recovery_active"),
        (lambda s: setattr(s, "phase", "bootstrap"), "skipped_not_online"),
        (lambda s: setattr(s, "activated", False), "skipped_not_online"),
        (lambda s: s.stop_event.set(), "skipped_stopping"),
        (lambda s: setattr(s.state, "judge", SimpleNamespace(judging=lambda: True, ladder_in_flight=lambda: False)),
         "skipped_judging"),
        (lambda s: setattr(s.state, "judge", SimpleNamespace(judging=lambda: False, ladder_in_flight=lambda: True)),
         "skipped_judging"),
    ],
)
def test_reexport_skips_while_another_owner_acts(setup, outcome) -> None:
    stub = _Stub(policy=ACT_LADDER, state="exhausted")
    setup(stub)
    stub._runtime_pool_reexport_observe(stub.snapshot, stub.payload, ACT_LADDER)
    stub.wait()
    assert stub.syncs == [] and stub.events == []
    assert stub.records[-1]["outcome"] == outcome


def test_reexport_parked_or_noop_outcome_records_no_sync_event() -> None:
    stub = _Stub(policy=ACT_LADDER, state="exhausted",
                 outcome=_ProviderSyncOutcome(ProviderSyncDisposition.NOOP, "unchanged"))
    stub._runtime_pool_reexport_observe(stub.snapshot, stub.payload, ACT_LADDER)
    stub.wait()
    assert stub.events == [] and stub.records[-1]["outcome"] == "noop"


@pytest.mark.parametrize(
    "policy",
    [
        NodeJudgementPolicy(post_dial_judge="shadow", tier_ladder_judge=True, exhausted_hold=True),
        # Keys off, opt in: exhausted_hold is True by default since 11-01.
        NodeJudgementPolicy(post_dial_judge="act", tier_ladder_judge=True, overshoot_repair=True,
                            rearm_on_stale_route=True, exhausted_hold=False),
        NodeJudgementPolicy(post_dial_judge="act", exhausted_pool_reexport=True, exhausted_hold=False),
        NodeJudgementPolicy(post_dial_judge="off", exhausted_hold=True),
        NodeJudgementPolicy.disabled(),
    ],
)
def test_fix_2_admission_is_off_without_its_own_key(policy: NodeJudgementPolicy) -> None:
    """Only ``acts and exhausted_hold``: the act / ladder keys never enable it."""

    assert not (policy.acts and policy.exhausted_hold)
    stub = _Stub(policy=policy, state="exhausted")
    judge = SimpleNamespace(calls=[], enter_exhausted_hold=lambda leaf, cause: judge.calls.append(leaf))
    assert stub._runtime_post_dial_exhausted_admission(judge, "n1") is False
    assert judge.calls == []


def test_publish_hook_is_keyed_on_act_and_ladder(monkeypatch) -> None:
    calls: list[Any] = []

    class Hook:
        _observe_published_runtime_route_view = DesktopRuntime._observe_published_runtime_route_view

        def __init__(self, policy):
            self.policy = policy

        def _route_lease_observe_snapshot(self, *a):
            pass

        def _node_judgement_arm_observe(self, *a, **k):
            pass

        def _node_judgement_policy(self):
            return self.policy

        def _runtime_ladder_observe_publish(self, *a):
            pass

        def _runtime_pool_reexport_observe(self, *a):
            calls.append(self.policy)

        def _runtime_tier3_route_view(self, *a):
            return None

    for policy, expected in (
        (ACT_LADDER, 1),
        (NodeJudgementPolicy(post_dial_judge="act", exhausted_pool_reexport=True), 1),
        (NodeJudgementPolicy(post_dial_judge="act", tier_ladder_judge=True, overshoot_repair=True,
                             rearm_on_stale_route=True, exhausted_hold=True,
                             not_reproduced_override=True, exhausted_pool_reexport=False), 0),
        (NodeJudgementPolicy(post_dial_judge="shadow", exhausted_pool_reexport=True), 0),
    ):
        calls.clear()
        Hook(policy)._observe_published_runtime_route_view(object(), captured_proxies("healthy"))
        assert len(calls) == expected


def test_exhausted_admission_sets_the_hold_only_for_a_loaded_member() -> None:
    stub = _Stub(policy=ACT_LADDER, state="exhausted")
    judge = SimpleNamespace(calls=[])
    judge.enter_exhausted_hold = lambda leaf, cause: judge.calls.append((leaf, cause)) or True
    assert stub._runtime_post_dial_exhausted_admission(judge, "n1") is True
    assert judge.calls == [("n1", "group_exhausted")]
    assert stub._runtime_post_dial_exhausted_admission(judge, "t2a") is False  # not a TIER3 member
    stub.publish("one_alive")
    assert stub._runtime_post_dial_exhausted_admission(judge, "n1") is False
    stub.publish("exhausted")
    stub.stored = None
    assert stub._runtime_post_dial_exhausted_admission(judge, "n1") is False
    assert len(judge.calls) == 1


@pytest.mark.parametrize("state", ["healthy", "two_alive", "one_alive"])
def test_exhausted_admission_never_holds_a_group_with_an_alive_member(state: str) -> None:
    """The ship2 regression (10-30): the admission reads the STORED
    publication wrapper; on the old code every member of a healthy group
    read dead there and every act trigger was gated exhausted_hold."""

    stub = _Stub(policy=ACT_LADDER, state=state)
    judge = SimpleNamespace(calls=[])
    judge.enter_exhausted_hold = lambda leaf, cause: judge.calls.append((leaf, cause)) or True
    assert set(stub.stored) == {"proxies", "proxy_map"}  # the production wrapper
    assert stub._runtime_post_dial_exhausted_admission(judge, "n1") is False
    assert judge.calls == []
    view = DesktopRuntime._runtime_tier3_route_view(stub, stub.snapshot, stub.stored)
    assert view == DesktopRuntime._runtime_tier3_route_view(stub, stub.snapshot, stub.payload)
    assert view["alive"] == {"healthy": 3, "two_alive": 2, "one_alive": 1}[state]


def test_act_start_gates_exhausted_hold_before_any_route_work() -> None:
    """``_start_runtime_post_dial_judgement_act`` checks the exhausted view
    right after the cheap gates (source code order, wiring guard)."""

    import inspect

    source = inspect.getsource(DesktopRuntime._start_runtime_post_dial_judgement_act)
    cheap = source.index("judge.blocked_cheap(leaf, now, trigger)")
    assert "else judge.blocked_cheap(leaf, now)" in source
    hold = source.index("self._runtime_post_dial_exhausted_admission(judge, leaf)")
    refs = source.index("self._runtime_post_dial_route_refs()")
    assert cheap < hold < refs
    for name in ("_runtime_post_dial_admission", "_runtime_post_dial_ladder_admission",
                 "_runtime_post_dial_stale_arm_refresh"):
        assert "_post_dial_override_kwargs(judge, trigger)" in inspect.getsource(getattr(DesktopRuntime, name)), name
    off = SimpleNamespace(backoff_override_on=False)
    on = SimpleNamespace(backoff_override_on=True)
    assert desktop_runtime._post_dial_override_kwargs(off, "t") == {}
    assert desktop_runtime._post_dial_override_kwargs(on, "t") == {"trigger": "t"}
    # the shadow would_judge path is untouched (no trigger, no hold)
    shadow = inspect.getsource(DesktopRuntime._log_runtime_post_dial_would_judge)
    assert "judge.blocked_cheap(leaf, now)" in shadow
    assert "exhausted_admission" not in shadow


def test_fix_keys_default_false_and_parse() -> None:
    # 11-01 user decision (after ship3): the three fix keys default True.
    assert NodeJudgementPolicy().exhausted_pool_reexport is True
    assert NodeJudgementPolicy().exhausted_hold is True
    assert NodeJudgementPolicy().not_reproduced_override is True
    ship_act = {"client": {"egress_recovery": {"node_judgement": {
        "post_dial_judge": "act", "tier_ladder_judge": True, "overshoot_repair": True,
        "rearm_on_stale_route": True}}}}
    p = NodeJudgementPolicy.from_config(ship_act)
    assert (p.exhausted_pool_reexport, p.exhausted_hold, p.not_reproduced_override) == (True, True, True)
    keys_off = {"client": {"egress_recovery": {"node_judgement": {
        "post_dial_judge": "act", "exhausted_pool_reexport": False, "exhausted_hold": "false",
        "not_reproduced_override": 0}}}}
    p = NodeJudgementPolicy.from_config(keys_off)
    assert (p.exhausted_pool_reexport, p.exhausted_hold, p.not_reproduced_override) == (False, False, False)
    on = {"client": {"egress_recovery": {"node_judgement": {
        "post_dial_judge": "act", "exhausted_pool_reexport": True, "exhausted_hold": "true",
        "not_reproduced_override": 1}}}}
    p = NodeJudgementPolicy.from_config(on)
    assert (p.exhausted_pool_reexport, p.exhausted_hold, p.not_reproduced_override) == (True, True, True)
    off = {"client": {"egress_recovery": {"node_judgement": {"enabled": False, "exhausted_hold": True}}}}
    assert NodeJudgementPolicy.from_config(off).exhausted_hold is False


def test_fix3_service_flag_off_is_the_old_gate() -> None:
    service, _ = _service()
    service.backoff_override_on = False
    _nr(service.gates, "n1", 900.0)
    assert service.backoff_override_eligible(_strong()) is False
    assert service.blocked_cheap("n1", 1000.0, _strong()) == GATE_LEAF_BACKOFF


def test_act_start_refreshes_the_service_fix_flags_from_the_policy() -> None:
    import inspect

    source = inspect.getsource(DesktopRuntime._start_runtime_post_dial_judgement_act)
    assert "judge.backoff_override_on = bool(fix_policy.not_reproduced_override)" in source
    assert "judge.successor_timeout_hold = bool(fix_policy.exhausted_hold)" in source
    assert source.index("successor_timeout_hold") < source.index("judge.blocked_cheap(")
