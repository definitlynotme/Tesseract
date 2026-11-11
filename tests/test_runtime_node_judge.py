from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path

import pytest
import yaml

import runtime_node_judge
from runtime_node_judge import (
    CLOSE,
    CLOSE_DEFER,
    CLOSE_DISABLED,
    CLOSE_KEEP_ALL,
    CLOSE_NEED_READBACK,
    CLOSE_NONE,
    CLOSE_NOT_DEMOTED,
    GATE_D4_BUSY,
    GATE_EXHAUSTED_HOLD,
    GATE_FACILITY_HOLD,
    GATE_GLOBAL_CAP,
    GATE_JUDGING,
    GATE_LEAF_BACKOFF,
    GATE_LEAF_CHECK_CAP,
    GATE_NOT_ARMED,
    GATE_STATUS_FAILURE,
    DEFAULT_TRIGGER_MIN_FAILED_SHARE,
    AccusedFailed,
    Done,
    Judgement,
    JudgeGates,
    LateHold,
    NeedReadback,
    NodeJudgementPolicy,
    ProbeClass,
    StartProbe,
    Verdict,
    close_record,
    contract_order,
    demotion_confirmed,
    member_alive,
    node_judgement_policy,
    probe_result_record,
    probe_start_record,
    provider_alive_map,
    readback_shows_accused_failure,
    select_stuck_closes,
    successor_cap,
    successor_list,
    verdict,
    verdict_record,
)
from runtime_post_dial import (
    DEFAULT_POST_DIAL_CONSTANTS,
    RULE_AGED,
    RULE_FAST,
    PostDialObserverCore,
    StuckConnections,
    constants_with,
    parse_rfc3339,
)


def _load_replay():
    path = Path(__file__).resolve().parent / "fixtures" / "post_dial" / "replay.py"
    spec = importlib.util.spec_from_file_location("post_dial_replay", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


replay = _load_replay()
TOKENIZER = replay.TOKENIZER
T0 = 1_790_000_000.0
# The prototype had no failed-share guard: its measured probes belong to its
# own (unguarded) first trigger, so the fixture judgements replay that one.
UNGUARDED = constants_with(trigger_min_failed_share=0.0)
MEMBERS = ("n1", "n2", "n3")
ALL_ALIVE = {"n1": True, "n2": True, "n3": True}
TEST_URL = "https://www.google.com/generate_204"


def _config(block):
    return {"client": {"egress_recovery": {"node_judgement": block}}}


def _kinds(actions) -> list[str]:
    return [type(action).__name__ for action in actions]


def _done(actions) -> Done:
    done = [action for action in actions if isinstance(action, Done)]
    assert len(done) == 1, actions
    return done[0]


def _judgement(t0: float = 100.0, **kwargs) -> Judgement:
    kwargs.setdefault("kind", "FAST")
    kwargs.setdefault("tokens", ("tok-a",))
    return Judgement("n1", ticket_deadline_perf=t0 + 6.0, **kwargs)


# ---------------------------------------------------------------------------
# Policy (P0-6)


def test_policy_defaults_match_the_design() -> None:
    policy = NodeJudgementPolicy.from_config({})

    assert policy == NodeJudgementPolicy()
    assert policy.enabled is True
    assert policy.evidence_log is True
    # 10-28 user decision: the ship1-tested package is the default (act).
    assert policy.post_dial_judge == "act"
    assert policy.act_rules == ("fast", "aged")
    assert policy.act_rule_ids == frozenset({RULE_FAST, RULE_AGED})
    assert policy.close_stuck_connections is True
    assert policy.stream_watch_seconds == 60.0
    assert policy.group_round_native_timeout is False
    assert policy.release_stale_fallback_pins is False
    assert policy.manual_check_uses_route_contract is True
    assert policy.observer_enabled is True
    assert policy.acts is True
    assert policy.constants().stream_watch_s == 60.0
    assert policy.observer_cpu_budget_pct == 2.0
    assert policy.constants().cpu_budget_pct == 2.0
    # The failed-share guard is off by default (user decision 10-01): the
    # rules alone, as when the key is absent from the observer's constants.
    assert DEFAULT_TRIGGER_MIN_FAILED_SHARE == 0.0
    assert policy.trigger_min_failed_share == 0.0
    assert policy.constants().trigger_min_failed_share == 0.0
    assert DEFAULT_POST_DIAL_CONSTANTS.trigger_min_failed_share == 0.0
    assert policy.constants().trigger_share_window_s == 10.0
    # Move-only by default (pre-registered 10-02): a conviction rebinds the
    # route only and records nothing durable about the node.
    assert policy.durable_conviction is False
    assert NodeJudgementPolicy.disabled().durable_conviction is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        ("true", True),
        ("yes", True),
        ("on", True),
        (1, True),
        ("false", False),
        ("no", False),
        ("off", False),
        (0, False),
        ("junk", False),
        (None, False),
    ],
)
def test_policy_durable_conviction_parses_leniently(value, expected) -> None:
    policy = NodeJudgementPolicy.from_config(
        _config({"post_dial_judge": "act", "durable_conviction": value})
    )
    assert policy.durable_conviction is expected
    assert policy.acts is True
    # ``enabled: false`` wins over the key, like every other flag.
    off = NodeJudgementPolicy.from_config(_config({"enabled": False, "durable_conviction": True}))
    assert off.durable_conviction is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("missing", True),  # 10-28: on by default
        (True, True),
        ("true", True),
        ("yes", True),
        (1, True),
        (False, False),
        ("off", False),
        ("garbage", True),  # unparseable: the default (on since 10-28)
        (None, True),
    ],
)
def test_policy_rearm_on_stale_route_defaults_false_and_parses(value, expected) -> None:
    block = {"post_dial_judge": "act"}
    if value != "missing":
        block["rearm_on_stale_route"] = value
    policy = NodeJudgementPolicy.from_config(_config(block))
    assert policy.rearm_on_stale_route is expected
    assert NodeJudgementPolicy().rearm_on_stale_route is True
    assert NodeJudgementPolicy.from_config({}).rearm_on_stale_route is True
    # ``enabled: false`` wins over the key, like every other flag.
    off = NodeJudgementPolicy.from_config(_config({"enabled": False, "rearm_on_stale_route": True}))
    assert off == NodeJudgementPolicy.disabled()
    assert off.rearm_on_stale_route is False


def test_the_config_example_documents_rearm_on_stale_route_as_off() -> None:
    text = (Path(__file__).resolve().parents[1] / "config.example.yaml").read_text(encoding="utf-8")
    block = text[text.index("# node_judgement:") :]
    lines = [line.strip().lstrip("#").strip() for line in block.splitlines()[:40]]
    assert "rearm_on_stale_route: true" in lines


@pytest.mark.parametrize("value", [False, "false", "no", "off", 0])
def test_master_switch_off_disables_everything(value) -> None:
    policy = NodeJudgementPolicy.from_config(
        _config({"enabled": value, "post_dial_judge": "act", "manual_check_uses_route_contract": True})
    )

    assert policy == NodeJudgementPolicy.disabled()
    assert policy.observer_enabled is False
    assert policy.acts is False
    assert policy.manual_check_uses_route_contract is False
    assert policy.close_stuck_connections is False
    assert policy.act_rules == ()
    assert policy.constants().stream_watch_s == 0.0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("act", "act"),
        ("ACT ", "act"),
        ("off", "off"),
        (False, "off"),  # YAML 1.1 bare ``off``
        # Unparseable values fall back to the default mode (act since 10-28).
        (True, "shadow"),  # YAML bare on: explicitly shadow, not the default
        ("bogus", "act"),
        (5, "act"),
        (None, "act"),
    ],
)
def test_post_dial_mode_parsing(value, expected) -> None:
    policy = NodeJudgementPolicy.from_config(_config({"post_dial_judge": value}))
    assert policy.post_dial_judge == expected
    assert policy.acts is (expected == "act")
    assert policy.observer_enabled is (expected != "off")


def test_bare_yaml_off_reads_as_off() -> None:
    config = yaml.safe_load(
        "client:\n  egress_recovery:\n    node_judgement:\n      post_dial_judge: off\n      enabled: on\n"
    )
    assert node_judgement_policy(config).post_dial_judge == "off"
    rules_off = yaml.safe_load("client:\n  egress_recovery:\n    node_judgement:\n      act_rules: off\n")
    assert node_judgement_policy(rules_off).act_rules == ()
    assert node_judgement_policy(rules_off).act_rule_ids == frozenset()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("fast", ("fast",)),
        ("fast, aged", ("fast", "aged")),
        (["FAST", "fast-spread", "bogus", "fast"], ("fast", "fast_spread")),
        (["waiting", "aged_single", "slow"], ("waiting", "aged_single", "slow")),
        ([], ()),
        ("", ()),
        # §14 rollback spellings of "no act rule".
        (False, ()),
        ("off", ()),
        ("OFF ", ()),
        ("none", ()),
        ("false", ()),
        ("0", ()),
        (["off"], ()),
        (["bogus"], ("fast", "aged")),
        (7, ("fast", "aged")),
        ({"fast": True}, ("fast", "aged")),
    ],
)
def test_act_rules_parsing(value, expected) -> None:
    assert NodeJudgementPolicy.from_config(_config({"act_rules": value})).act_rules == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("30", 30.0), (-5, 0.0), (1e9, 600.0), ("abc", 60.0), (True, 60.0), (math.nan, 60.0), (0, 0.0)],
)
def test_stream_watch_seconds_is_clamped(value, expected) -> None:
    policy = NodeJudgementPolicy.from_config(_config({"stream_watch_seconds": value}))
    assert policy.stream_watch_seconds == expected
    assert policy.constants().stream_watch_s == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (2.0, 2.0),
        ("5", 5.0),
        (0.2, 0.5),  # clamped to 0.5-20
        (100, 20.0),
        (math.inf, 20.0),
        (0, 0.0),  # 0 turns the throttle off
        (-3, 0.0),
        (False, 0.0),  # YAML 1.1 bare ``off``
        ("off", 0.0),
        ("none", 0.0),  # as for act_rules
        ("1%", 1.0),  # a trailing percent sign is the unit
        (" 0.5 % ", 0.5),
        ("0%", 0.0),
        ("%", 2.0),
        ("abc", 2.0),
        (None, 2.0),  # YAML null / empty: the default, as for every key here
        (True, 2.0),
        (math.nan, 2.0),
    ],
)
def test_observer_cpu_budget_pct_is_parsed_and_clamped(value, expected) -> None:
    policy = NodeJudgementPolicy.from_config(_config({"observer_cpu_budget_pct": value}))
    assert policy.observer_cpu_budget_pct == expected
    assert policy.constants().cpu_budget_pct == expected
    # The other constants keep their defaults.
    assert policy.constants().cpu_window_s == 10.0 and policy.constants().stream_watch_s == 60.0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.5, 0.5),
        ("0.3", 0.3),
        (1, 1.0),
        (1.5, 1.0),  # clamped to 0-1
        (50, 1.0),  # a bare number is a share: 50 is the strictest setting, not 50%
        (math.inf, 1.0),
        ("50%", 0.5),  # a trailing % is a percentage, as for observer_cpu_budget_pct
        ("30%", 0.3),
        (" 25 % ", 0.25),
        ("150%", 1.0),
        ("0%", 0.0),
        ("-10%", 0.0),
        ("%", "default"),
        ("abc%", "default"),
        (0, 0.0),  # 0 turns the guard off (the rules alone)
        (-0.2, 0.0),
        (False, 0.0),  # YAML 1.1 bare ``off``
        ("off", 0.0),
        ("none", 0.0),  # as for act_rules
        ("abc", "default"),
        (None, "default"),  # YAML null / empty: the default, as for every key here
        (True, "default"),
        (math.nan, "default"),
    ],
)
def test_trigger_min_failed_share_is_parsed_and_clamped(value, expected, monkeypatch) -> None:
    policy = NodeJudgementPolicy.from_config(_config({"trigger_min_failed_share": value}))
    want = DEFAULT_TRIGGER_MIN_FAILED_SHARE if expected == "default" else expected
    assert policy.trigger_min_failed_share == want
    assert policy.constants().trigger_min_failed_share == want
    # The window and the other constants keep their defaults.
    assert policy.constants().trigger_share_window_s == 10.0
    assert policy.constants().k_fast == 2 and policy.constants().stream_watch_s == 60.0
    # An explicit share still turns the guard on now that the default is off.
    if expected not in {"default", 0.0}:
        assert policy.trigger_min_failed_share > 0.0
    # The fallback is the default itself, not "off": another default shows through.
    monkeypatch.setattr(runtime_node_judge, "DEFAULT_TRIGGER_MIN_FAILED_SHARE", 0.42)
    again = NodeJudgementPolicy.from_config(_config({"trigger_min_failed_share": value}))
    assert again.trigger_min_failed_share == (0.42 if expected == "default" else expected)


def test_the_config_example_documents_the_failed_share_guard() -> None:
    text = (Path(__file__).resolve().parents[1] / "config.example.yaml").read_text(encoding="utf-8")
    block = text[text.index("# node_judgement:") :]
    lines = [line.strip().lstrip("#").strip() for line in block.splitlines()[:40]]
    assert "trigger_min_failed_share: 0" in lines
    assert "durable_conviction: false" in lines
    assert lines.index("durable_conviction: false") == lines.index("post_dial_judge: act") + 4


@pytest.mark.parametrize(
    "config",
    [None, "x", [], {"client": []}, {"client": {"egress_recovery": "x"}}, _config("junk"), _config(None)],
)
def test_policy_tolerates_junk_shapes(config) -> None:
    assert NodeJudgementPolicy.from_config(config) == NodeJudgementPolicy()


def test_policy_flags_parse_booleans() -> None:
    policy = NodeJudgementPolicy.from_config(
        _config(
            {
                "evidence_log": "no",
                "close_stuck_connections": 0,
                "group_round_native_timeout": "yes",
                "release_stale_fallback_pins": "true",
                "manual_check_uses_route_contract": "off",
                "durable_conviction": "yes",
            }
        )
    )
    assert policy.durable_conviction is True
    assert policy.evidence_log is False
    assert policy.close_stuck_connections is False
    assert policy.group_round_native_timeout is True
    assert policy.release_stale_fallback_pins is True
    assert policy.manual_check_uses_route_contract is False


# ---------------------------------------------------------------------------
# Verdict truth table (§5.3), including the H2 races of §1.2


P, H, X, TO, I = "pass", "http_answer", "transport_fail", "timeout", "indeterminate"


@pytest.mark.parametrize(
    ("results", "started", "cap", "successors", "expected", "successor"),
    [
        ({"A_C": P}, 0, 1, ("n2", "n3"), Verdict.NOT_REPRODUCED, None),
        ({"A_C": H}, 0, 1, ("n2", "n3"), Verdict.NOT_REPRODUCED, None),
        # H2(b): once A_C answers, successor failures never convict.
        ({"A_C": P, "S_C0": X}, 1, 1, ("n2", "n3"), Verdict.NOT_REPRODUCED, None),
        ({"A_C": X, "S_C0": P}, 1, 1, ("n2", "n3"), Verdict.CONVICT_CONTRACT, "n2"),
        ({"A_C": TO, "S_C0": P}, 1, 1, ("n2", "n3"), Verdict.CONVICT_CONTRACT, "n2"),
        # H2(a): the walk advanced on S_C0's fast failure before A_C resolved.
        ({"A_C": TO, "S_C0": X, "S_C1": P}, 2, 2, ("n2", "n3", "n4"), Verdict.CONVICT_CONTRACT, "n3"),
        ({"A_C": TO, "S_C0": TO}, 1, 1, ("n2", "n3"), Verdict.FACILITY, None),
        ({"A_C": TO, "S_C0": X, "S_C1": TO}, 2, 2, ("n2", "n3", "n4"), Verdict.FACILITY, None),
        # H2(e): a black-holed successor after a fast accused failure.
        ({"A_C": X, "S_C0": TO}, 1, 1, ("n2", "n3"), Verdict.INDETERMINATE, None),
        ({"A_C": X, "S_C0": X}, 1, 1, ("n2", "n3"), Verdict.EXHAUSTED, None),
        ({"A_C": TO, "S_C0": X}, 1, 1, ("n2", "n3"), Verdict.EXHAUSTED, None),
        ({"A_C": X, "S_C0": X}, 1, 3, ("n2",), Verdict.EXHAUSTED, None),
        ({"A_C": X}, 0, 1, (), Verdict.EXHAUSTED, None),
        ({"A_C": TO}, 0, 1, (), Verdict.EXHAUSTED, None),
        # H2(c): truncation by the ticket is INDETERMINATE, never a silent FACILITY.
        ({"A_C": X, "S_C0": X}, 1, 2, ("n2", "n3", "n4"), Verdict.INDETERMINATE, None),
        ({"A_C": TO}, 1, 1, ("n2", "n3"), Verdict.INDETERMINATE, None),
        ({"A_C": TO}, 0, 1, ("n2", "n3"), Verdict.INDETERMINATE, None),
        ({}, 1, 1, ("n2", "n3"), Verdict.INDETERMINATE, None),
        ({"A_C": I, "S_C0": P}, 1, 1, ("n2", "n3"), Verdict.INDETERMINATE, None),
        ({"A_C": X, "S_C0": I}, 1, 1, ("n2", "n3"), Verdict.INDETERMINATE, None),
        ({"A_C": X}, 0, 1, None, Verdict.INDETERMINATE, None),
    ],
)
def test_verdict_truth_table(results, started, cap, successors, expected, successor) -> None:
    result = verdict(results, started, cap, successors)
    assert result.verdict is expected, result
    assert result.successor == successor


# ---------------------------------------------------------------------------
# Scheduler (§18.6) on a fake clock


def test_pf1_fast_zombie_convicts_after_the_successor_passes() -> None:
    judgement = _judgement()
    start = judgement.start(100.0, T0)
    assert start == [StartProbe("A_C", "n1", 5000, 5.65)]
    assert judgement.on_route_state(MEMBERS, {"n1": False, "n2": True, "n3": True}, 100.010) == []

    actions = judgement.on_probe_result("A_C", ProbeClass.TRANSPORT_FAIL, 100.016)
    assert actions == [AccusedFailed("n1", ProbeClass.TRANSPORT_FAIL, late=False), StartProbe("S_C0", "n2", 5000, 5.65)]
    assert judgement.next_wakeup() == pytest.approx(105.5)

    done = _done(judgement.on_probe_result("S_C0", "pass", 101.94))
    assert done.verdict is Verdict.CONVICT_CONTRACT
    assert done.successor == "n2"
    assert done.commit_allowed is True
    assert done.tokens == ("tok-a",)
    assert judgement.probes_started == ("A_C", "S_C0")
    assert judgement.next_wakeup() is None


def test_accused_failure_before_the_route_read_starts_the_successor_on_arrival() -> None:
    judgement = _judgement()
    judgement.start(100.0, T0)
    assert _kinds(judgement.on_probe_result("A_C", "transport_fail", 100.01)) == ["AccusedFailed"]
    actions = judgement.on_route_state(MEMBERS, ALL_ALIVE, 100.02)
    assert actions == [StartProbe("S_C0", "n2", 5000, 5.65)]


def test_pf4_black_hole_with_a_five_second_accused_verdicts_inside_the_ticket() -> None:
    judgement = _judgement(kind="AGED")
    judgement.start(100.0, T0)
    judgement.on_route_state(MEMBERS, ALL_ALIVE, 100.01)
    assert judgement.next_wakeup() == pytest.approx(102.0)
    assert judgement.on_timer(101.99) == []
    assert judgement.on_timer(102.0) == [StartProbe("S_C0", "n2", 5000, 5.65)]
    assert judgement.on_probe_result("S_C0", "pass", 103.98) == []  # A_C still pending
    actions = judgement.on_probe_result("A_C", "timeout", 105.004)
    assert _kinds(actions) == ["AccusedFailed", "Done"]
    done = _done(actions)
    assert done.verdict is Verdict.CONVICT_CONTRACT and done.successor == "n2"
    assert done.commit_allowed is True
    assert judgement.timings()["A_C"] == pytest.approx(5004.0)


def test_queued_accused_is_resolved_by_the_deadline_readback() -> None:
    judgement = _judgement(kind="AGED")
    judgement.start(100.0, T0 + 0.25)
    judgement.on_route_state(MEMBERS, ALL_ALIVE, 100.01)
    judgement.on_timer(102.0)
    judgement.on_probe_result("S_C0", "pass", 103.9)
    actions = judgement.on_timer(105.5)
    assert actions == [NeedReadback("n1", T0 + 0.25)]
    assert judgement.next_wakeup() is None
    assert judgement.on_timer(105.6) == []
    actions = judgement.on_readback(True, 105.51)
    # Mihomo wrote L dead: the demotion watch and closes start now (§4.1).
    assert _kinds(actions) == ["AccusedFailed", "Done"]
    assert actions[0] == AccusedFailed("n1", ProbeClass.TIMEOUT, late=False)
    done = _done(actions)
    assert done.verdict is Verdict.CONVICT_CONTRACT and done.commit_allowed is True
    assert "accused_from_readback" in judgement.skipped
    # The queued A_C's own reply arrives later: its failure was already reported.
    assert judgement.on_probe_result("A_C", "timeout", 105.7) == []


def test_queued_accused_without_readback_is_indeterminate_but_closes_still_run() -> None:
    judgement = _judgement(kind="AGED")
    judgement.start(100.0, T0)
    judgement.on_route_state(MEMBERS, ALL_ALIVE, 100.01)
    judgement.on_timer(102.0)
    judgement.on_probe_result("S_C0", "pass", 103.9)
    judgement.on_timer(105.5)
    done = _done(judgement.on_readback(False, 105.52))
    assert done.verdict is Verdict.INDETERMINATE
    assert done.commit_allowed is False
    # The queued A_C answers after the ticket: the move happened, closes run.
    late = judgement.on_probe_result("A_C", "timeout", 106.8)
    assert late == [AccusedFailed("n1", ProbeClass.TIMEOUT, late=True)]
    decision = select_stuck_closes(
        StuckConnections(("c1", "c2"), 2, 0, 0, 0, 0, 0, T0),
        enabled=True,
        successor_probe_pending=judgement.successor_probe_pending,
        successor_timed_out=judgement.successor_timed_out,
        demotion_confirmed=True,
        readback_age_s=0.02,
    )
    assert decision.action == CLOSE and decision.ids == ("c1", "c2")


def test_h2a_successor_failure_before_the_accused_resolves_advances_the_walk() -> None:
    judgement = _judgement(kind="AGED")
    members = ("n1", "n2", "n3", "n4", "n5")
    judgement.start(100.0, T0)
    judgement.on_route_state(members, dict.fromkeys(members, True), 100.01)
    assert judgement.cap == 2
    judgement.on_timer(102.0)
    assert judgement.on_probe_result("S_C0", "transport_fail", 102.3) == [StartProbe("S_C1", "n3", 5000, 5.65)]
    assert judgement.on_probe_result("S_C1", "pass", 104.2) == []
    done = _done(judgement.on_probe_result("A_C", "timeout", 105.0))
    assert done.verdict is Verdict.CONVICT_CONTRACT and done.successor == "n3"


def test_a_successor_timeout_stops_the_walk() -> None:
    judgement = _judgement()
    members = ("n1", "n2", "n3", "n4", "n5")
    judgement.start(100.0, T0)
    judgement.on_route_state(members, dict.fromkeys(members, True), 100.01)
    judgement.on_probe_result("A_C", "transport_fail", 100.02)
    done = _done(judgement.on_probe_result("S_C0", "timeout", 105.03))
    assert done.verdict is Verdict.INDETERMINATE
    assert judgement.probes_started == ("A_C", "S_C0")
    assert judgement.successor_timed_out is True


@pytest.mark.parametrize(("members", "cap"), [(1, 1), (2, 1), (3, 1), (4, 1), (5, 2), (6, 2), (7, 3), (9, 3), (30, 3)])
def test_successor_cap_by_pool_size(members: int, cap: int) -> None:
    assert successor_cap(members) == cap


def test_pf7b_every_node_failing_fast_is_exhausted_with_two_probes() -> None:
    judgement = _judgement()
    judgement.start(100.0, T0)
    judgement.on_route_state(MEMBERS, ALL_ALIVE, 100.01)
    judgement.on_probe_result("A_C", "transport_fail", 100.02)
    done = _done(judgement.on_probe_result("S_C0", "transport_fail", 100.03))
    assert done.verdict is Verdict.EXHAUSTED
    assert judgement.probes_started == ("A_C", "S_C0")

    gates = JudgeGates()
    gates.begin("n1", 100.0)
    gates.finish("n1", done.verdict, done.tokens, 100.03)
    assert gates.blocked_reason("n2", 101.0, armed=True, d4_busy=False) == GATE_EXHAUSTED_HOLD
    assert gates.note_answered(100.0) is False  # observed before the hold
    assert gates.note_answered(130.0) is True
    assert gates.blocked_reason("n2", 131.0, armed=True, d4_busy=False) is None


def test_pf7_facility_sets_the_hold_from_the_late_successor_timeout_and_keeps_every_connection() -> None:
    judgement = _judgement(kind="AGED")
    judgement.start(100.0, T0)
    judgement.on_route_state(MEMBERS, ALL_ALIVE, 100.01)
    judgement.on_timer(102.0)
    accused = judgement.on_probe_result("A_C", "timeout", 105.004)
    assert _kinds(accused) == ["AccusedFailed"]
    stuck = StuckConnections(("c1",), 1, 0, 0, 0, 0, 0, T0)
    # A successor probe is still pending when Mihomo demotes L: defer.
    assert select_stuck_closes(
        stuck,
        enabled=True,
        successor_probe_pending=judgement.successor_probe_pending,
        successor_timed_out=judgement.successor_timed_out,
        demotion_confirmed=True,
        readback_age_s=0.01,
    ).action == CLOSE_DEFER
    done = _done(judgement.on_timer(105.5))
    assert done.verdict is Verdict.INDETERMINATE and done.reason == "successor_unresolved"
    late = judgement.on_probe_result("S_C0", "timeout", 107.0)
    assert late == [LateHold(Verdict.FACILITY)]
    assert judgement.on_probe_result("S_C0", "timeout", 107.1) == []
    assert select_stuck_closes(
        stuck,
        enabled=True,
        successor_probe_pending=judgement.successor_probe_pending,
        successor_timed_out=judgement.successor_timed_out,
        demotion_confirmed=True,
        readback_age_s=0.01,
    ).action == CLOSE_KEEP_ALL
    assert len(judgement.probes_started) == 2

    gates = JudgeGates()
    gates.begin("n1", 100.0)
    gates.finish("n1", done.verdict, done.tokens, 105.5)
    gates.set_hold(late[0].verdict, 107.0)
    assert gates.hold is Verdict.FACILITY
    assert gates.blocked_reason("n1", 120.0, armed=True, d4_busy=False) == GATE_FACILITY_HOLD
    assert gates.note_answered(126.0) is True


def test_min_probe_remaining_applies_to_successors_but_not_to_the_accused() -> None:
    late_start = Judgement("n1", kind="FAST", tokens=(), ticket_deadline_perf=100.4)
    assert _kinds(late_start.start(100.0, T0)) == ["StartProbe"]

    judgement = _judgement()
    judgement.start(100.0, T0)
    judgement.on_route_state(MEMBERS, ALL_ALIVE, 100.01)
    assert _kinds(judgement.on_probe_result("A_C", "transport_fail", 104.8)) == ["AccusedFailed"]
    assert "S_C0:ticket_remaining" in judgement.skipped
    done = _done(judgement.on_timer(105.5))
    assert done.verdict is Verdict.INDETERMINATE


def test_route_state_unavailable_never_gives_exhausted() -> None:
    judgement = _judgement()
    judgement.start(100.0, T0)
    judgement.on_probe_result("A_C", "transport_fail", 100.01)
    assert judgement.on_route_state_unavailable(100.02) == []
    done = _done(judgement.on_timer(105.5))
    assert done.verdict is Verdict.INDETERMINATE
    assert judgement.probes_started == ("A_C",)


def test_a_passing_accused_ends_the_judgement_at_once() -> None:
    judgement = _judgement()
    judgement.start(100.0, T0)
    judgement.on_route_state(MEMBERS, ALL_ALIVE, 100.01)
    done = _done(judgement.on_probe_result("A_C", "pass", 101.9))
    assert done.verdict is Verdict.NOT_REPRODUCED
    assert judgement.probes_started == ("A_C",)
    assert judgement.on_timer(102.0) == []


def test_http_answer_on_the_accused_is_reported_for_the_demotion_watch() -> None:
    judgement = _judgement()
    judgement.start(100.0, T0)
    actions = judgement.on_probe_result("A_C", "http_answer", 100.5)
    assert _kinds(actions) == ["AccusedFailed", "Done"]
    assert _done(actions).verdict is Verdict.NOT_REPRODUCED


def test_non_member_leaf_is_abandoned() -> None:
    judgement = _judgement()
    judgement.start(100.0, T0)
    done = _done(judgement.on_route_state(("n2", "n3"), ALL_ALIVE, 100.01))
    assert done.verdict is None and done.reason == "non_member"
    # §18.6 stops here: a later A_C failure starts no demotion watch and no closes.
    assert judgement.on_probe_result("A_C", "transport_fail", 100.02) == []
    assert "accused_failed:non_member" in judgement.skipped


def test_accused_failure_before_a_non_member_route_read_is_never_a_confirmed_demotion() -> None:
    judgement = _judgement()
    judgement.start(100.0, T0)
    # A fast 503 can beat the route reads (9-21 ms against 2.4-14.8 ms).
    assert _kinds(judgement.on_probe_result("A_C", "transport_fail", 100.009)) == ["AccusedFailed"]
    assert _done(judgement.on_route_state(("n2", "n3"), ALL_ALIVE, 100.012)).reason == "non_member"
    # on_accused_failed's own G2/P2 read: L is not in ``all``, so no watch and no closes.
    provider = {"proxies": [{"name": "n2", "alive": True}, {"name": "n3", "alive": True}]}
    alive = provider_alive_map(provider, TEST_URL)
    assert demotion_confirmed("n1", alive, "n2", members=("n2", "n3", "REJECT")) is False
    assert demotion_confirmed("n1", {"n1": False, "n2": True}, "n2", members=("n1", "n2", "REJECT")) is True


def test_pf10_core_dead_successor_is_probed_when_no_alive_member_exists() -> None:
    assert successor_list(("A", "B", "C"), "A", {"A": False, "B": False, "C": True}) == ("C", "B")
    judgement = Judgement("A", kind="FAST", tokens=(), ticket_deadline_perf=106.0)
    judgement.start(100.0, T0)
    judgement.on_route_state(("A", "B"), {"A": True, "B": False}, 100.01)
    actions = judgement.on_probe_result("A_C", "transport_fail", 100.02)
    assert actions[-1] == StartProbe("S_C0", "B", 5000, 5.65)
    done = _done(judgement.on_probe_result("S_C0", "pass", 101.9))
    assert done.verdict is Verdict.CONVICT_CONTRACT and done.successor == "B"


def test_commit_is_not_allowed_after_the_ticket_deadline() -> None:
    judgement = Judgement("n1", kind="AGED", tokens=(), ticket_deadline_perf=106.0)
    judgement.start(100.0, T0)
    judgement.on_route_state(MEMBERS, ALL_ALIVE, 100.01)
    judgement.on_timer(102.0)
    judgement.on_probe_result("S_C0", "pass", 104.0)
    assert _kinds(judgement.on_timer(105.5)) == ["NeedReadback"]
    # The readback itself only answered after the ticket closed.
    done = _done(judgement.on_readback(True, 106.2))
    assert done.verdict is Verdict.CONVICT_CONTRACT
    assert done.commit_allowed is False


# ---------------------------------------------------------------------------
# Gates (§3.3)


def test_gates_block_in_the_design_order() -> None:
    gates = JudgeGates()
    assert gates.blocked_reason("n1", 0.0, armed=False, d4_busy=True) == GATE_NOT_ARMED
    gates.begin("n1", 0.0)
    assert gates.blocked_reason("n2", 0.1, armed=True, d4_busy=True) == GATE_JUDGING
    gates.finish("n1", Verdict.FACILITY, (), 1.0)
    assert gates.in_flight is False
    assert gates.blocked_reason("n2", 1.1, armed=True, d4_busy=True) == GATE_FACILITY_HOLD
    gates.note_answered(2.0)
    assert gates.blocked_reason("n2", 2.1, armed=True, d4_busy=True) == GATE_D4_BUSY
    assert gates.blocked_reason("n2", 2.1, armed=True, d4_busy=False) is None
    assert (
        gates.blocked_reason("n2", 2.1, armed=True, d4_busy=False, latest_sample_status_failure=True)
        == GATE_STATUS_FAILURE
    )


def test_leaf_backoff_and_exoneration_after_not_reproduced() -> None:
    gates = JudgeGates()
    gates.begin("n1", 0.0)
    gates.finish("n1", Verdict.NOT_REPRODUCED, ("tok-a", "tok-b"), 2.0)
    # 10-28 ship2: NOT_REPRODUCED backs off NOT_REPRODUCED_BACKOFF_S (420 s), not LEAF_BACKOFF_S (10 s).
    assert gates.blocked_reason("n1", 421.9, armed=True, d4_busy=False) == GATE_LEAF_BACKOFF
    assert gates.blocked_reason("n2", 3.0, armed=True, d4_busy=False) is None
    assert gates.blocked_reason("n1", 422.0, armed=True, d4_busy=False) is None
    assert gates.is_exonerated("n1", "tok-a", 601.9) is True
    assert gates.is_exonerated("n2", "tok-a", 10.0) is False
    assert gates.exoneration_predicate(100.0)("n1", "tok-b") is True
    assert gates.is_exonerated("n1", "tok-a", 602.0) is False


def test_indeterminate_backs_off_and_convict_does_not() -> None:
    gates = JudgeGates()
    gates.begin("n1", 0.0)
    gates.finish("n1", Verdict.INDETERMINATE, ("tok",), 5.5)
    assert gates.blocked_reason("n1", 15.4, armed=True, d4_busy=False) == GATE_LEAF_BACKOFF
    gates.begin("n2", 20.0)
    gates.finish("n2", Verdict.CONVICT_CONTRACT, ("tok",), 22.0)
    assert gates.blocked_reason("n2", 22.1, armed=True, d4_busy=False) is None
    assert gates.is_exonerated("n2", "tok", 23.0) is False


def test_not_reproduced_backoff_is_420s_and_indeterminate_stays_10s() -> None:
    assert DEFAULT_POST_DIAL_CONSTANTS.not_reproduced_backoff_s == 420.0
    assert DEFAULT_POST_DIAL_CONSTANTS.leaf_backoff_s == 10.0
    gates = JudgeGates()
    gates.begin("n1", 0.0)
    gates.finish("n1", Verdict.NOT_REPRODUCED, ("tok",), 5.0)
    gates.begin("n2", 6.0)
    gates.finish("n2", Verdict.INDETERMINATE, ("tok",), 7.0)
    # n1 blocked for exactly 420 s (not longer); n2 only 10 s; n3 never.
    assert gates.blocked_reason("n1", 16.0, armed=True, d4_busy=False) == GATE_LEAF_BACKOFF
    assert gates.blocked_reason("n1", 424.999, armed=True, d4_busy=False) == GATE_LEAF_BACKOFF
    assert gates.blocked_reason("n1", 425.0, armed=True, d4_busy=False) is None
    assert gates.blocked_reason("n2", 16.999, armed=True, d4_busy=False) == GATE_LEAF_BACKOFF
    assert gates.blocked_reason("n2", 17.0, armed=True, d4_busy=False) is None
    assert gates.blocked_reason("n3", 5.1, armed=True, d4_busy=False) is None


def test_not_reproduced_backoff_follows_the_constant() -> None:
    gates = JudgeGates(constants_with(not_reproduced_backoff_s=30.0))
    gates.begin("n1", 0.0)
    gates.finish("n1", Verdict.NOT_REPRODUCED, (), 1.0)
    assert gates.blocked_reason("n1", 30.9, armed=True, d4_busy=False) == GATE_LEAF_BACKOFF
    assert gates.blocked_reason("n1", 31.0, armed=True, d4_busy=False) is None
    # A later INDETERMINATE on the same leaf re-arms the short back-off only.
    gates.begin("n1", 31.0)
    gates.finish("n1", Verdict.INDETERMINATE, (), 32.0)
    assert gates.blocked_reason("n1", 41.9, armed=True, d4_busy=False) == GATE_LEAF_BACKOFF
    assert gates.blocked_reason("n1", 42.0, armed=True, d4_busy=False) is None


def test_policy_constants_carry_the_not_reproduced_backoff() -> None:
    policy = NodeJudgementPolicy.from_config(
        {"client": {"egress_recovery": {"node_judgement": {"post_dial_judge": "act"}}}}
    )
    assert policy.constants().not_reproduced_backoff_s == 420.0
    base = constants_with(not_reproduced_backoff_s=99.0)
    assert policy.constants(base).not_reproduced_backoff_s == 99.0


def test_pf9_flapping_leaf_is_bounded_by_the_per_leaf_cap() -> None:
    gates = JudgeGates()
    for phase in range(3):
        t = phase * 120.0
        assert gates.blocked_reason("n1", t, armed=True, d4_busy=False) is None
        gates.begin("n1", t)
        gates.finish("n1", Verdict.CONVICT_CONTRACT, ("tok",), t + 2.0)
    assert gates.blocked_reason("n1", 360.0, armed=True, d4_busy=False) == GATE_LEAF_CHECK_CAP
    assert gates.blocked_reason("n2", 360.0, armed=True, d4_busy=False) is None
    assert gates.blocked_reason("n1", 600.1, armed=True, d4_busy=False) is None


def test_global_cap_is_ten_per_ten_minutes() -> None:
    gates = JudgeGates()
    for index in range(10):
        gates.begin(f"n{index}", float(index))
        gates.finish(f"n{index}", Verdict.CONVICT_CONTRACT, (), float(index) + 0.5)
    assert gates.blocked_reason("fresh", 20.0, armed=True, d4_busy=False) == GATE_GLOBAL_CAP
    assert gates.blocked_reason("fresh", 600.1, armed=True, d4_busy=False) is None


def test_abandon_releases_the_in_flight_gate_without_bookkeeping() -> None:
    gates = JudgeGates()
    gates.begin("n1", 0.0)
    gates.abandon("n1")
    assert gates.blocked_reason("n1", 0.1, armed=True, d4_busy=False) is None


# ---------------------------------------------------------------------------
# Close selection (§6 rules 3 and 6)


def _stuck(n: int) -> StuckConnections:
    return StuckConnections(tuple(f"c{i}" for i in range(n)), n - 1 if n else 0, 1 if n else 0, 3, 2, 1, 0, T0)


@pytest.mark.parametrize(
    ("kwargs", "action"),
    [
        (dict(enabled=False), CLOSE_DISABLED),
        (dict(successor_probe_pending=True), CLOSE_DEFER),
        (dict(successor_timed_out=True), CLOSE_KEEP_ALL),
        (dict(demotion_confirmed=None), CLOSE_NEED_READBACK),
        (dict(readback_age_s=1.5), CLOSE_NEED_READBACK),
        (dict(demotion_confirmed=False), CLOSE_NOT_DEMOTED),
        (dict(), CLOSE),
    ],
)
def test_close_selection_rules(kwargs, action) -> None:
    arguments = dict(
        enabled=True,
        successor_probe_pending=False,
        successor_timed_out=False,
        demotion_confirmed=True,
        readback_age_s=0.2,
    )
    arguments.update(kwargs)
    decision = select_stuck_closes(_stuck(3), **arguments)
    assert decision.action == action
    assert (decision.ids != ()) is (action == CLOSE)
    assert dict(decision.kept)["progressing"] == 3


def test_close_selection_caps_at_max_close_and_reports_none_when_nothing_is_stuck() -> None:
    decision = select_stuck_closes(
        _stuck(70),
        enabled=True,
        successor_probe_pending=False,
        successor_timed_out=False,
        demotion_confirmed=True,
        readback_age_s=0.0,
    )
    assert len(decision.ids) == DEFAULT_POST_DIAL_CONSTANTS.max_close == 64
    assert decision.aged + decision.waiting == 64
    assert select_stuck_closes(
        _stuck(0),
        enabled=True,
        successor_probe_pending=False,
        successor_timed_out=False,
        demotion_confirmed=True,
        readback_age_s=0.0,
    ).action == CLOSE_NONE


# ---------------------------------------------------------------------------
# Route helpers (§18.6)


def test_alive_rule_prefers_the_contract_entry() -> None:
    provider = {
        "name": "TIER3B-RUNTIME",
        "testUrl": "",
        "expectedStatus": "*",
        "proxies": [
            {"name": "n1", "alive": True, "extra": {TEST_URL: {"alive": False, "history": []}}},
            {"name": "n2", "alive": False, "extra": {TEST_URL: {"alive": True, "history": []}}},
            {"name": "n3", "alive": True, "extra": {}},
            {"name": "n4", "alive": False},
            {"name": "REJECT", "alive": True},
            "junk",
        ],
    }
    alive = provider_alive_map(provider, TEST_URL)
    assert alive == {"n1": False, "n2": True, "n3": True, "n4": False, "REJECT": True}
    order = contract_order(["n1", "n2", "n3", "n4", "REJECT"])
    assert order == ("n1", "n2", "n3", "n4")
    assert successor_list(order, "n2", alive) == ("n3", "n1", "n4")
    assert member_alive(None, TEST_URL) is False
    assert provider_alive_map(None, TEST_URL) == {}


def test_demotion_confirmation_and_deadline_readback() -> None:
    assert demotion_confirmed("n1", {"n1": False}, "n2") is True
    assert demotion_confirmed("n1", {"n1": False}, "n1") is False
    assert demotion_confirmed("n1", {"n1": True}, "n2") is False
    issued = 1790436956.665
    proxy = {
        "name": "n1",
        "extra": {TEST_URL: {"alive": False, "history": [{"time": "2026-09-26T11:35:56.681507+00:00", "delay": 0}]}},
    }
    newest = parse_rfc3339("2026-09-26T11:35:56.681507+00:00")
    assert readback_shows_accused_failure(proxy, TEST_URL, newest - 0.01) is True
    assert readback_shows_accused_failure(proxy, TEST_URL, newest + 0.01) is False
    # Written in the same clock tick as the A_C issue: the same time (both
    # clocks advance in Windows timer ticks), so it is not older than A_C.
    assert readback_shows_accused_failure(proxy, TEST_URL, newest) is True
    assert readback_shows_accused_failure(proxy, TEST_URL, newest + 0.001) is False
    import time

    tick = 1790670060.1716447  # time.time() inside one tick: Mihomo stamps the same instant
    local = tick - 4 * 3600
    whole = math.floor(local)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(whole))
    text = f"{stamp}.{round((local - whole) * 10_000_000):07d}-04:00"  # Mihomo's 7-digit form
    same_tick = {"extra": {TEST_URL: {"alive": False, "history": [{"time": text, "delay": 0}]}}}
    assert readback_shows_accused_failure(same_tick, TEST_URL, tick) is True
    alive_again = json.loads(json.dumps(proxy))
    alive_again["extra"][TEST_URL]["alive"] = True
    assert readback_shows_accused_failure(alive_again, TEST_URL, newest - 0.01) is False
    assert readback_shows_accused_failure({"name": "n1"}, TEST_URL, issued) is False


# ---------------------------------------------------------------------------
# Observer + judge scenarios


def test_pf6_site_down_everywhere_is_not_reproduced_and_exonerated() -> None:
    gates = JudgeGates()
    heads = 0
    triggers = 0
    for burst_at in (T0 + 1.0, T0 + 20.0, T0 + 60.0):
        mihomo = replay.FakeMihomo()
        lines = []
        for index in range(4):
            t = burst_at + index * 0.03
            mihomo.add(replay.FakeConn(f"d{burst_at}-{index}", t, 48000 + index, "down.example", 443, "n1", t_close=t + 0.01))
            lines.append((t + 0.001, replay.open_line(t, sport=48000 + index, host="down.example")))
        core = PostDialObserverCore(tokenizer=TOKENIZER, tier3_members=MEMBERS)
        now_perf = burst_at - T0
        result = replay.drive(
            core,
            lines,
            mihomo,
            t_start=burst_at - 0.1,
            t_end=burst_at + 2.0,
            is_exonerated=gates.exoneration_predicate(now_perf),
        )
        for trigger in result.triggers:
            if gates.blocked_reason(trigger.leaf, now_perf, armed=True, d4_busy=False) is not None:
                continue
            triggers += 1
            gates.begin(trigger.leaf, now_perf)
            judgement = Judgement(trigger.leaf, kind=trigger.kind, tokens=trigger.tokens, ticket_deadline_perf=now_perf + 6)
            heads += len(judgement.start(now_perf, trigger.t))
            judgement.on_route_state(MEMBERS, ALL_ALIVE, now_perf + 0.01)
            done = _done(judgement.on_probe_result("A_C", "pass", now_perf + 1.9))
            assert done.verdict is Verdict.NOT_REPRODUCED
            gates.finish(trigger.leaf, done.verdict, done.tokens, now_perf + 1.9)
    assert triggers == 1
    assert heads == 1


def _first_trigger_and_probes(rep):
    mihomo, feed = replay.vproto_mihomo(rep, t_end=rep["t_exit"])
    core = PostDialObserverCore(tokenizer=TOKENIZER, tier3_members=tuple(rep["members"]), constants=UNGUARDED)
    result = replay.drive(core, feed, mihomo, t_start=rep["t_attach"], t_end=rep["t_exit"])
    trigger = result.triggers[0]
    durations: dict[str, float] = {}
    classes: dict[str, str] = {}
    for name, _leaf, cls, ms, _ts in rep["proto"]["probe_results"]:
        # The first judgement's probes (P7fin judged twice in the prototype).
        durations.setdefault(name, ms / 1000.0)
        classes.setdefault(name, cls)
    return trigger, durations, classes


def _replay_judgement(trigger, durations, classes, succ_state, *, late=None):
    """Run §18.6 with the prototype's measured probe durations and classes.

    With ``late`` (a list), probes still pending at the verdict are then
    delivered in reply order and their actions appended to it.  A probe the
    prototype never ran (v3 starts S_C0 at +2.0 s, the prototype did not in
    every run) has no measured reply and stays pending.
    """

    t0 = 0.0
    judgement = Judgement(trigger.leaf, kind=trigger.kind, tokens=trigger.tokens, ticket_deadline_perf=t0 + 6.0)
    pending: dict[str, float] = {}
    for action in judgement.start(t0, trigger.t):
        pending[action.name] = t0 + durations.get(action.name, math.inf)
    members = ("n1", "n2", "n3")
    events = [(t0 + 0.012, "route", None)]
    done = None
    now = t0
    while done is None:
        timer = judgement.next_wakeup()
        candidates = [(t, "probe", name) for name, t in pending.items()] + events
        if timer is not None:
            candidates.append((timer, "timer", None))
        at, kind, name = min(candidates, key=lambda item: item[0])
        now = at
        if kind == "route":
            events.clear()
            actions = judgement.on_route_state(members, succ_state, now)
        elif kind == "timer":
            actions = judgement.on_timer(now)
        else:
            del pending[name]
            actions = judgement.on_probe_result(name, classes[name], now)
        for action in actions:
            if isinstance(action, StartProbe):
                pending[action.name] = now + durations.get(action.name, math.inf)
            elif isinstance(action, NeedReadback):
                actions = judgement.on_readback(False, now)
            if isinstance(action, Done):
                done = action
        for action in actions:
            if isinstance(action, Done):
                done = action
    if late is not None:
        for name, at in sorted(pending.items(), key=lambda item: item[1]):
            if at == math.inf:
                continue
            late.extend(judgement.on_probe_result(name, classes[name], at))
    return judgement, done, now


@pytest.mark.parametrize("scenario", ["P1", "P3"])
def test_vproto_trigger_to_verdict_matches_the_prototype(scenario: str) -> None:
    fixture = replay.load_fixture(f"vproto_{scenario}.json.gz")
    for rep in fixture["reps"]:
        trigger, durations, classes = _first_trigger_and_probes(rep)
        _succ, alive, _now = rep["proto"]["judge_state"][0]
        judgement, done, at = _replay_judgement(trigger, durations, classes, alive)
        expected = rep["proto"]["verdicts"][0]
        assert done.verdict.value == expected[1], rep["label"]
        assert done.successor == expected[2] == "n2", rep["label"]
        assert done.commit_allowed is True
        if scenario == "P3":
            # S_C0 now starts at +2.0 s (A_C pending), not at t0 as in v2.
            assert at == pytest.approx(durations["A_C"], abs=1e-6)
            assert at <= 5.5
        else:
            assert at <= 3.0


# v3 verdicts with the prototype's measured probe classes and durations:
# scenario -> (verdict, successor, late hold).  The prototype started S_C0 at
# t0 and walked further; v3 starts it on A_C's failure or at +2.0 s, caps the
# walk at min(3, max(1, (n - 1) // 2)) = 1 for three members, and has no
# destination probes.
V3_VERDICTS = {
    "P4": (Verdict.CONVICT_CONTRACT, "n2", None),
    # A_C and S_C0 both 504, S_C0 only after the evidence deadline: the
    # verdict is INDETERMINATE and the late S_C0 timeout sets the FACILITY hold.
    "P6": (Verdict.INDETERMINATE, None, Verdict.FACILITY),
    "P6b": (Verdict.EXHAUSTED, None, None),
    "P7": (Verdict.NOT_REPRODUCED, None, None),
    "IDLE": (Verdict.CONVICT_CONTRACT, "n2", None),
}


@pytest.mark.parametrize("scenario", sorted(V3_VERDICTS))
def test_vproto_trigger_to_v3_verdict_with_the_measured_probes(scenario: str) -> None:
    fixture = replay.load_fixture(f"vproto_{scenario}.json.gz")
    expected_verdict, expected_successor, expected_hold = V3_VERDICTS[scenario]
    judged = 0
    for rep in fixture["reps"]:
        if not rep["proto"]["triggers"]:
            continue  # P6down: every node down, no INFO line, no judgement
        trigger, durations, classes = _first_trigger_and_probes(rep)
        _succ, alive, _now = rep["proto"]["judge_state"][0]
        late: list = []
        judgement, done, at = _replay_judgement(trigger, durations, classes, alive, late=late)
        judged += 1
        assert done.verdict is expected_verdict, rep["label"]
        assert done.successor == expected_successor, rep["label"]
        assert done.commit_allowed is (expected_verdict is Verdict.CONVICT_CONTRACT)
        assert at <= 5.5
        holds = [action.verdict for action in late if isinstance(action, LateHold)]
        assert holds == ([] if expected_hold is None else [expected_hold]), rep["label"]
        # One HEAD, plus S_C0 when A_C failed or was still pending at +2.0 s.
        accused_answered = classes["A_C"] in {"pass", "http_answer"}
        heads = 1 if accused_answered and durations["A_C"] < 2.0 else 2
        assert len(judgement.probes_started) == heads, rep["label"]
    assert judged == {"P4": 10, "P6": 5, "P6b": 3, "P7": 10, "IDLE": 3}[scenario]


def test_pf6_site_down_on_every_node_costs_one_head_on_the_p7_fixture() -> None:
    fixture = replay.load_fixture("vproto_P7.json.gz")
    reps = [rep for rep in fixture["reps"] if rep["label"].startswith("P7fin")]
    assert len(reps) == 5
    for rep in reps:
        assert len(rep["proto"]["triggers"]) == 2  # the prototype judged the site twice
        _trigger, durations, classes = _first_trigger_and_probes(rep)
        gates = JudgeGates()
        clock = {"now": rep["t_attach"]}
        heads: list[StartProbe] = []

        def on_poll(t, evaluation, rep=rep, gates=gates, clock=clock, heads=heads):
            clock["now"] = t
            for trigger in evaluation.triggers:
                if gates.blocked_reason(trigger.leaf, t, armed=True, d4_busy=False) is not None:
                    continue
                gates.begin(trigger.leaf, t)
                judgement = Judgement(trigger.leaf, kind=trigger.kind, tokens=trigger.tokens, ticket_deadline_perf=t + 6.0)
                heads.extend(judgement.start(t, t))
                judgement.on_route_state(tuple(rep["members"]), ALL_ALIVE, t + 0.012)
                replied = t + durations["A_C"]
                done = _done(judgement.on_probe_result("A_C", classes["A_C"], replied))
                assert done.verdict is Verdict.NOT_REPRODUCED
                # Applied at once: the in-flight gate would block the burst until then.
                gates.finish(trigger.leaf, done.verdict, done.tokens, replied)

        mihomo, feed = replay.vproto_mihomo(rep, t_end=rep["t_exit"])
        core = PostDialObserverCore(tokenizer=TOKENIZER, tier3_members=tuple(rep["members"]), constants=UNGUARDED)
        replay.drive(
            core,
            feed,
            mihomo,
            t_start=rep["t_attach"],
            t_end=rep["t_exit"],
            is_exonerated=lambda leaf, token, gates=gates, clock=clock: gates.is_exonerated(leaf, token, clock["now"]),
            on_poll=on_poll,
        )
        # The trigger's tokens are exonerated on n1 for 10 min: the second burst is not judged.
        assert len(heads) == 1, rep["label"]


def test_vproto_black_hole_closes_only_stuck_connections_after_the_move() -> None:
    fixture = replay.load_fixture("vproto_P3.json.gz")
    for rep in fixture["reps"]:
        trigger, durations, _classes = _first_trigger_and_probes(rep)
        t_move = trigger.t + durations["A_C"]
        mihomo, feed = replay.vproto_mihomo(rep, t_end=rep["t_exit"])
        core = PostDialObserverCore(tokenizer=TOKENIZER, tier3_members=tuple(rep["members"]), constants=UNGUARDED)
        replay.drive(core, feed, mihomo, t_start=rep["t_attach"], t_end=t_move)
        stuck = core.stuck_connection_ids("n1", t_demote_wall=t_move, now_wall=t_move)
        decision = select_stuck_closes(
            stuck,
            enabled=True,
            successor_probe_pending=False,
            successor_timed_out=False,
            demotion_confirmed=True,
            readback_age_s=0.01,
        )
        assert decision.action == CLOSE, rep["label"]
        assert stuck.aged >= 2, rep["label"]
        # Selection is over the core's latest snapshot (≤ 1 s old at the move).
        assert t_move - stuck.snapshot_wall <= 1.0
        snapshot = {item["id"]: item for item in mihomo.snapshot(stuck.snapshot_wall)["connections"]}
        for conn_id in decision.ids:
            assert snapshot[conn_id]["chains"][0] == "n1"
        for conn_id in stuck.ids[: stuck.aged]:
            assert snapshot[conn_id]["download"] == 0 and snapshot[conn_id]["upload"] > 0


# ---------------------------------------------------------------------------
# Privacy of judge objects and records


def test_judge_objects_and_records_hide_leaf_names() -> None:
    leaf = "node.example.net"
    judgement = Judgement(leaf, kind="FAST", tokens=("a" * 32,), ticket_deadline_perf=106.0)
    start = judgement.start(100.0, T0)
    judgement.on_route_state((leaf, "succ.example.org"), {leaf: False, "succ.example.org": True}, 100.01)
    actions = judgement.on_probe_result("A_C", "transport_fail", 100.02)
    done = _done(judgement.on_probe_result("S_C0", "pass", 101.9))
    decision = select_stuck_closes(
        _stuck(2), enabled=True, successor_probe_pending=False, successor_timed_out=False,
        demotion_confirmed=True, readback_age_s=0.1,
    )
    records = [
        probe_start_record(start[0], TOKENIZER, now_wall=T0, queue_wait_ms=0.0),
        probe_result_record("A_C", leaf, "transport_fail", TOKENIZER, now_wall=T0, elapsed_ms=20.0),
        verdict_record(done, judgement, TOKENIZER, now_wall=T0 + 2, mode="act"),
        close_record(decision, leaf, TOKENIZER, now_wall=T0 + 2),
    ]
    blob = json.dumps(records) + repr(judgement) + repr(done) + repr(start) + repr(actions) + repr(JudgeGates())
    assert "example" not in blob
    assert records[2]["verdict"] == "CONVICT_CONTRACT"
    assert records[2]["successor"] == TOKENIZER.leaf("succ.example.org")
    assert records[2]["toks"] == ["a" * 12]
    assert verdict_record(done, judgement, TOKENIZER, now_wall=T0, mode="shadow")["ev"] == "would_verdict"


def test_verdict_record_carries_durable_right_after_commit_allowed() -> None:
    judgement = Judgement("n1", kind="FAST", tokens=("a" * 32,), ticket_deadline_perf=106.0)
    judgement.start(100.0, T0)
    judgement.on_route_state(("n1", "n2"), {"n1": False, "n2": True}, 100.01)
    judgement.on_probe_result("A_C", "transport_fail", 100.02)
    done = _done(judgement.on_probe_result("S_C0", "pass", 101.9))
    for mode in ("act", "shadow"):
        default = verdict_record(done, judgement, TOKENIZER, now_wall=T0, mode=mode)
        on = verdict_record(done, judgement, TOKENIZER, now_wall=T0, mode=mode, durable=True)
        off = verdict_record(done, judgement, TOKENIZER, now_wall=T0, mode=mode, durable=False)
        # Always present: the default is the configured default (off).
        assert default["durable"] == "off" and off["durable"] == "off" and on["durable"] == "on"
        keys = list(on)
        assert keys == [
            "ev", "t", "verdict", "reason", "kind", "leaf", "successor", "results",
            "probes", "t_ms", "toks", "commit_allowed", "durable", "skipped",
        ]
        # Nothing else changes.
        assert {k: v for k, v in on.items() if k != "durable"} == {
            k: v for k, v in off.items() if k != "durable"
        }


# ---------------------------------------------------------------------------
# Tier ladder additions (spec v1 §3.9 T1, T9, T10; §2.2 example text)


def _ladder_block(**block):
    return _config(block)


def test_t1_ladder_keys_default_false_and_parse() -> None:
    from runtime_node_judge import node_judgement_config_warnings

    # 10-28: both keys on by default; disabled() still reads both off.
    default = NodeJudgementPolicy.from_config({})
    assert default.tier_ladder_judge is True and default.overshoot_repair is True
    assert NodeJudgementPolicy().tier_ladder_judge is True and NodeJudgementPolicy().overshoot_repair is True
    disabled = NodeJudgementPolicy.disabled()
    assert disabled.tier_ladder_judge is False and disabled.overshoot_repair is False
    for value in (True, "true", "yes", "on", 1):
        policy = NodeJudgementPolicy.from_config(_ladder_block(tier_ladder_judge=value, overshoot_repair=value))
        assert policy.tier_ladder_judge is True and policy.overshoot_repair is True, value
        assert node_judgement_config_warnings(_ladder_block(tier_ladder_judge=value, overshoot_repair=value)) == ()
    for value in (False, "false", "no", "off", 0):
        policy = NodeJudgementPolicy.from_config(_ladder_block(tier_ladder_judge=value))
        assert policy.tier_ladder_judge is False, value
    for value in (None, "junk"):  # unparseable: the default (on since 10-28)
        policy = NodeJudgementPolicy.from_config(_ladder_block(tier_ladder_judge=value))
        assert policy.tier_ladder_judge is True, value
    # overshoot_repair with the ladder explicitly off reads false and is named
    # once by the warnings (alone, the ladder is now on by default).
    alone = _ladder_block(overshoot_repair=True, tier_ladder_judge=False)
    assert NodeJudgementPolicy.from_config(alone).overshoot_repair is False
    assert node_judgement_config_warnings(alone) == ("overshoot_repair_requires_tier_ladder_judge",)
    assert node_judgement_config_warnings(_ladder_block(overshoot_repair=True)) == ()
    assert node_judgement_config_warnings(_ladder_block(overshoot_repair="yes", tier_ladder_judge="no")) == (
        "overshoot_repair_requires_tier_ladder_judge",
    )
    # enabled: false wins: both false, no warning.
    off = _ladder_block(enabled=False, tier_ladder_judge=True, overshoot_repair=True)
    assert NodeJudgementPolicy.from_config(off) == NodeJudgementPolicy.disabled()
    assert node_judgement_config_warnings(off) == ()
    assert node_judgement_config_warnings(_ladder_block(enabled="off", overshoot_repair=True)) == ()
    for config in ({}, None, "x", _config(None), _ladder_block(overshoot_repair=False), _ladder_block(tier_ladder_judge=True)):
        assert node_judgement_config_warnings(config) == ()
    # Keys absent == keys true since 10-28 (dataclass equality).
    assert NodeJudgementPolicy.from_config(_ladder_block(tier_ladder_judge=True, overshoot_repair=True)) == (
        NodeJudgementPolicy.from_config(_ladder_block())
    )
    assert NodeJudgementPolicy.from_config(alone) == NodeJudgementPolicy.from_config(
        _ladder_block(tier_ladder_judge=False, overshoot_repair=False)
    )


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        # 10-28: absent keys read act / ladder on / overshoot on.
        ({}, (True, True, True)),
        ({"post_dial_judge": "act"}, (True, True, True)),
        ({"tier_ladder_judge": True, "post_dial_judge": "shadow"}, (True, False, True)),
        ({"tier_ladder_judge": True, "overshoot_repair": False}, (True, True, False)),
        ({"tier_ladder_judge": True, "overshoot_repair": True, "post_dial_judge": "shadow"}, (True, False, True)),
        ({"tier_ladder_judge": True, "overshoot_repair": True, "post_dial_judge": "act"}, (True, True, True)),
        ({"overshoot_repair": True, "tier_ladder_judge": False}, (False, True, False)),
        ({"enabled": False, "tier_ladder_judge": True, "overshoot_repair": True}, (False, False, False)),
    ],
)
def test_t1_ladder_properties_truth_table(block, expected) -> None:
    policy = NodeJudgementPolicy.from_config(_config(block))
    assert (policy.ladder_on, policy.acts, policy.overshoot_on) == expected
    assert policy.ladder_policy_key == expected
    # Disabled instances never read on, whatever the fields say.
    forced = NodeJudgementPolicy(enabled=False, tier_ladder_judge=True, overshoot_repair=True)
    assert (forced.ladder_on, forced.overshoot_on) == (False, False)


def test_the_config_example_documents_the_ladder_keys_as_off() -> None:
    raw = (Path(__file__).resolve().parents[1] / "config.example.yaml").read_bytes()
    assert raw.count(b"\r\n") == 0
    anchor = b"    #   rearm_on_stale_route: true\n"
    insert = (
        b"    #   # Judge the node that carries traffic at any tier (Tier1, Tier2 or\n"
        b"    #   # Tier3), not only Tier3. After a failed check of that node Mihomo\n"
        b"    #   # itself moves traffic to the next node in order; a group higher up\n"
        b"    #   # is tested only when every node below failed and another member of\n"
        b"    #   # that group answered first. Never writes a selection or pin; judges\n"
        b"    #   # in act mode only. While true, automatic recovery also stops\n"
        b"    #   # switching the Tier1 select groups (in every mode).\n"
        b"    #   tier_ladder_judge: true\n"
        b"    #   # Needs tier_ladder_judge. When Mihomo itself drops PROXY-FALLBACK\n"
        b"    #   # below a member whose own node still looks healthy, re-test that\n"
        b"    #   # member once so Mihomo can return to it (no write).\n"
        b"    #   overshoot_repair: true\n"
    )
    assert raw.count(anchor + insert) == 1
    assert raw.count(b"tier_ladder_judge: true") == 1 and raw.count(b"overshoot_repair: true") == 1
    # The keys are documented commented out: the example parses to the defaults.
    example = yaml.safe_load(raw.decode("utf-8"))
    policy = NodeJudgementPolicy.from_config(example)
    assert policy.tier_ladder_judge is True and policy.overshoot_repair is True
    assert policy == NodeJudgementPolicy.from_config({})
    # Uncommenting the block as documented gives the documented (true) values.
    text = raw.decode("utf-8")
    block = text[text.index("# node_judgement:") :]
    lines = [line.strip().lstrip("#").strip() for line in block.splitlines()[:60]]
    assert lines.index("tier_ladder_judge: true") == lines.index("rearm_on_stale_route: true") + 8
    assert lines.index("overshoot_repair: true") == lines.index("tier_ladder_judge: true") + 4


def _rung_800():
    from runtime_tier_ladder import Rung, RungKind

    return Rung(index=0, group="TIER2-AUTO", accused="n1", depth=3, kind=RungKind.LEAF,
                url="http://cp.cloudflare.com/generate_204", expected="204", timeout_ms=800,
                members=("n1", "n2", "n3"), fixed0="")


def test_t9_judgement_with_800ms_rung_constants() -> None:
    from runtime_tier_ladder import rung_constants, rung_ticket_s

    rung = _rung_800()
    t0 = 100.0
    judgement = Judgement(
        "n1", kind="FAST", tokens=("tok",), ticket_deadline_perf=t0 + rung_ticket_s(800, witnessed=False),
        constants=rung_constants(rung, witnessed=False),
    )
    start = judgement.start(t0, T0)
    assert start == [StartProbe("A_C", "n1", 800, 1.45)]
    assert start[0].timeout_ms == 800 and start[0].request_timeout_s == 1.45
    assert judgement.t_evid - judgement.t_ac == pytest.approx(3.2)
    assert judgement.next_wakeup() == pytest.approx(t0 + 0.4)
    assert judgement.on_route_state(("n1", "n2", "n3"), {"n2": True, "n3": True}, t0 + 0.05) == []
    assert judgement.on_timer(t0 + 0.39) == []
    assert judgement.on_timer(t0 + 0.4) == [StartProbe("S_C0", "n2", 800, 1.45)]
    # S_C0 answers inside the 800 ms window; A_C then times out: CONVICT.
    assert judgement.on_probe_result("S_C0", "pass", t0 + 0.6) == []
    done = _done(judgement.on_probe_result("A_C", "timeout", t0 + 0.85))
    assert done.verdict is Verdict.CONVICT_CONTRACT and done.successor == "n2" and done.commit_allowed
    # Legacy constants are unchanged by default.
    legacy = _judgement()
    assert legacy.start(100.0, T0) == [StartProbe("A_C", "n1", 5000, 5.65)]
    assert legacy.next_wakeup() == pytest.approx(102.0)


def test_t9_successor_cap_override() -> None:
    judgement = _judgement(successor_cap=2)
    judgement.start(100.0, T0)
    judgement.on_route_state(("n1", "n2", "n3", "n4"), {}, 100.01)
    assert judgement.cap == 2
    assert _judgement().cap == 0
    legacy = _judgement()
    legacy.start(100.0, T0)
    legacy.on_route_state(("n1", "n2", "n3", "n4"), {}, 100.01)
    assert legacy.cap == 1


@pytest.mark.parametrize("failure", ["transport_fail", "timeout"])
def test_t9_preset_successor_convicts_on_a_failing_accused(failure) -> None:
    judgement = _judgement(preset_successor="W")
    assert judgement.successors == ("W",) and judgement.cap == 1 and judgement.started_successors == 1
    start = judgement.start(100.0, T0)
    assert start == [StartProbe("A_C", "n1", 5000, 5.65)]
    # No route read is needed and the S_C0 timer never arms.
    assert judgement.next_wakeup() == pytest.approx(100.0 + 6.0 - 0.5)
    assert judgement.on_route_state(("n1", "X"), {"X": True}, 100.01) == []
    assert judgement.on_route_state_unavailable(100.02) == []
    assert judgement.on_timer(102.5) == []
    assert judgement.successors == ("W",)
    actions = judgement.on_probe_result("A_C", failure, 102.6)
    assert AccusedFailed("n1", ProbeClass(failure), late=False) in actions
    assert not any(isinstance(action, StartProbe) for action in actions)
    done = _done(actions)
    assert done.verdict is Verdict.CONVICT_CONTRACT
    assert done.successor == "W" and done.successor_index == 0 and done.commit_allowed is True
    assert dict(done.results) == {"S_C0": "pass", "A_C": failure}
    assert judgement.probes_started == ("A_C", "S_C0")
    assert judgement.successor_probe_pending is False and judgement.successor_timed_out is False


def test_t9_preset_successor_passing_accused_is_not_reproduced() -> None:
    judgement = _judgement(preset_successor="W")
    judgement.start(100.0, T0)
    done = _done(judgement.on_probe_result("A_C", "pass", 100.5))
    assert done.verdict is Verdict.NOT_REPRODUCED and done.reason == "accused_pass"


def test_t9_preset_successor_deadline_readback() -> None:
    judgement = _judgement(preset_successor="W")
    judgement.start(100.0, T0)
    actions = judgement.on_timer(105.5)
    assert actions == [NeedReadback("n1", T0)]
    done = _done(judgement.on_readback(True, 105.6))
    assert done.verdict is Verdict.CONVICT_CONTRACT and done.successor == "W"


def test_t9_contract_admission_and_await_fill_start_probe() -> None:
    judgement = _judgement(
        contract=("TIER2-AUTO", "http://cp.cloudflare.com/generate_204", "204"), admission_s=1.9, await_s=2.8,
    )
    judgement.start(100.0, T0)
    judgement.on_route_state(("n1", "n2", "n3"), {"n2": True}, 100.01)
    actions = judgement.on_probe_result("A_C", "transport_fail", 100.5)
    probes = [action for action in actions if isinstance(action, StartProbe)]
    assert len(probes) == 1
    for probe in (judgement._probe("A_C", "n1"), probes[0]):
        assert probe.url == "http://cp.cloudflare.com/generate_204"
        assert probe.expected == "204" and probe.group == "TIER2-AUTO"
        assert probe.admission_s == 1.9 and probe.await_s == 2.8
    # The extra fields never show in a repr (group names embed node names).
    assert repr(probes[0]) == "StartProbe(name='S_C0', timeout_ms=5000, request_timeout_s=5.65)"
    assert "TIER2" not in repr(probes[0]) and "cloudflare" not in repr(probes[0])
    legacy = _judgement()
    probe = legacy.start(100.0, T0)[0]
    assert (probe.url, probe.expected, probe.group, probe.admission_s, probe.await_s) == (None,) * 5
    assert repr(probe) == "StartProbe(name='A_C', timeout_ms=5000, request_timeout_s=5.65)"


def test_t10_rung_gates_count_per_key_and_globally() -> None:
    from runtime_tier_ladder import rung_gate_key

    gates = JudgeGates()
    key = rung_gate_key("TIER2-AUTO", "t2a")
    assert gates.rung_blocked(key, 100.0) is None
    for at in (100.0, 101.0, 102.0):
        gates.note_rung(key, at)
    assert gates.rung_blocked(key, 103.0) == GATE_LEAF_CHECK_CAP
    assert gates.rung_blocked(rung_gate_key("PROXY-FALLBACK", "TIER2-AUTO"), 103.0) is None
    # The rung key never collides with a leaf's own cap; the in-flight gate is untouched.
    assert gates.blocked_reason("t2a", 103.0, armed=True, d4_busy=False) is None
    assert gates.in_flight is False
    # The per-key window is 600 s.
    assert gates.rung_blocked(key, 100.0 + 600.0) is None
    # Global cap: 10 per 600 s across leaves and rungs.
    gates = JudgeGates()
    for index in range(5):
        gates.begin(f"leaf{index}", 200.0 + index)
        gates.finish(f"leaf{index}", Verdict.CONVICT_CONTRACT, (), 200.5 + index)
    for index in range(5):
        gates.note_rung(rung_gate_key("G", f"m{index}"), 210.0 + index)
    assert gates.rung_blocked(rung_gate_key("G", "other"), 220.0) == GATE_GLOBAL_CAP
    assert gates.blocked_reason("fresh", 220.0, armed=True, d4_busy=False) == GATE_GLOBAL_CAP
    assert gates.rung_blocked(rung_gate_key("G", "other"), 200.0 + 600.0 + 15.0) is None
    assert repr(JudgeGates()) == "JudgeGates(in_flight=False, hold=None, exonerated=<0>)"
