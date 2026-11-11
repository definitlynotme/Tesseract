"""Pure tier-ladder model (spec v1 §3.9: T2-T8, T11, T12).

T1 (policy), T9 (``Judgement`` ladder arguments) and T10 (rung gates) live in
``tests/test_runtime_node_judge.py`` next to the legacy policy and gate tests.
"""

from __future__ import annotations

import json
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import runtime_tier_ladder as L
from runtime_node_judge import ProbeClass, Verdict
from runtime_post_dial import DEFAULT_POST_DIAL_CONSTANTS
from runtime_tier_ladder import (
    EXCLUDED_MEMBERS,
    EscalationCause,
    GroupView,
    LadderAuthority,
    NativePick,
    Rung,
    RungKind,
    RungVerdict,
    RungVerdictResult,
)

ROOT = Path(__file__).resolve().parents[1]
P3_TRIALS = ROOT / ".build" / "claude-fallback" / "results" / "ladder-facts-20261026" / "p3" / "trials.json"

PROBE = "http://cp.cloudflare.com/generate_204"
T3URL = "http://www.google.com/generate_204"
FB = "Fallback"
SEL = "Selector"


# ---------------------------------------------------------------------------
# Topology fixtures in the default render shape (clash_formatter.build_clash_config)


def _group(name, type_name, members, now, *, fixed="", url=None, expected=None):
    fallback = type_name == FB
    return GroupView(
        name=name,
        type_name=type_name,
        members=tuple(members),
        now=now,
        fixed=fixed,
        test_url=(url if url is not None else (PROBE if fallback else "")),
        expected_status=(expected if expected is not None else ("204" if fallback else "")),
    )


def _topology(*, tier1="default", tier2=("t2a", "t2b"), tier3=("t3a", "t3b", "REJECT"), pf_now="TIER1-SELECT",
              t1s_now="TIER1-AUTO", t1xs_now="TIER1-X-AUTO", t1_now="TIER1-X-SELECT", t2_now="t2a",
              t3_now="t3a", x_members=("x1", "x2"), pf_members=None):
    """``(groups, graph, timeouts)`` for one render.

    tier1: "default" (two physical nodes X/Y with per-physical groups),
    "flat" (no per-physical groups), "one_variant" (X has one variant) or None.
    """

    groups: list[GroupView] = []
    timeouts: dict[str, int] = {}
    entries: list[str] = []
    if tier1 in {"default", "one_variant"}:
        xs = ("x1",) if tier1 == "one_variant" else tuple(x_members)
        for code, variants in (("X", xs), ("Y", ("y1", "y2"))):
            auto, select = f"TIER1-{code}-AUTO", f"TIER1-{code}-SELECT"
            groups.append(_group(auto, FB, variants, variants[0]))
            timeouts[auto] = 800
            groups.append(_group(select, SEL, (auto, *variants),
                                 t1xs_now if code == "X" else auto))
            entries.append(select)
    elif tier1 == "flat":
        entries = ["fa", "fb"]
    if tier1 is not None:
        groups.append(_group("TIER1-AUTO", FB, entries, t1_now if tier1 != "flat" else "fa"))
        timeouts["TIER1-AUTO"] = 800
        groups.append(_group("TIER1-SELECT", SEL, ("TIER1-AUTO", *entries), t1s_now))
    groups.append(_group("TIER2-AUTO", FB, tier2, t2_now if tier2 != ("REJECT",) else "REJECT"))
    timeouts["TIER2-AUTO"] = 800
    groups.append(_group("TIER3-AUTO", FB, tier3, t3_now, url=T3URL))
    timeouts["TIER3-AUTO"] = 5000
    if pf_members is None:
        pf_members = [*(["TIER1-SELECT"] if tier1 is not None else []), "TIER2-AUTO", "TIER3-AUTO"]
    groups.append(_group("PROXY-FALLBACK", FB, pf_members, pf_now))
    timeouts["PROXY-FALLBACK"] = 5000
    groups.append(_group("FINAL-EXIT", SEL, ("PROXY-FALLBACK", "REJECT"), "PROXY-FALLBACK"))
    groups.append(_group("GENERAL", SEL, ("FINAL-EXIT",), "FINAL-EXIT"))
    groups.append(_group("GLOBAL", SEL, ("GENERAL", "FINAL-EXIT"), "GENERAL"))
    by_name = {group.name: group for group in groups}
    graph = {group.name: group.members for group in groups}
    return by_name, graph, timeouts


def _summary(result):
    return [(r.group, r.accused, r.kind.value, r.timeout_ms, r.depth, r.index) for r in result.rungs]


# ---------------------------------------------------------------------------
# T2 rungs()


def test_t2_tier1_default_has_three_rungs_deepest_first() -> None:
    groups, graph, timeouts = _topology()
    chain = ("FINAL-EXIT", "PROXY-FALLBACK", "TIER1-SELECT", "TIER1-AUTO", "TIER1-X-SELECT", "TIER1-X-AUTO", "x1")
    result = L.rungs(chain, groups, timeouts, graph=graph)
    assert result.reason is None
    assert _summary(result) == [
        ("TIER1-X-AUTO", "x1", "leaf", 800, 6, 0),
        ("TIER1-AUTO", "TIER1-X-SELECT", "group", 800, 4, 1),
        ("PROXY-FALLBACK", "TIER1-SELECT", "group", 5000, 2, 2),
    ]
    r0 = result.rungs[0]
    assert r0.members == ("x1", "x2") and r0.url == PROBE and r0.expected == "204" and r0.fixed0 == ""
    assert result.rungs[2].members == ("TIER1-SELECT", "TIER2-AUTO", "TIER3-AUTO")


def test_t2_user_pointed_tier1_select_has_only_the_pf_rung() -> None:
    groups, graph, timeouts = _topology(t1s_now="TIER1-X-SELECT", t1xs_now="x2")
    chain = ("FINAL-EXIT", "PROXY-FALLBACK", "TIER1-SELECT", "TIER1-X-SELECT", "x2")
    result = L.rungs(chain, groups, timeouts, graph=graph)
    assert _summary(result) == [("PROXY-FALLBACK", "TIER1-SELECT", "group", 5000, 2, 0)]


def test_t2_tier1_without_per_physical_groups() -> None:
    groups, graph, timeouts = _topology(tier1="flat")
    chain = ("FINAL-EXIT", "PROXY-FALLBACK", "TIER1-SELECT", "TIER1-AUTO", "fa")
    result = L.rungs(chain, groups, timeouts, graph=graph)
    assert _summary(result) == [
        ("TIER1-AUTO", "fa", "leaf", 800, 4, 0),
        ("PROXY-FALLBACK", "TIER1-SELECT", "group", 5000, 2, 1),
    ]


def test_t2_tier1_one_variant_per_physical_skips_the_single_member_fallback() -> None:
    groups, graph, timeouts = _topology(tier1="one_variant")
    chain = ("FINAL-EXIT", "PROXY-FALLBACK", "TIER1-SELECT", "TIER1-AUTO", "TIER1-X-SELECT", "TIER1-X-AUTO", "x1")
    result = L.rungs(chain, groups, timeouts, graph=graph)
    assert _summary(result) == [
        ("TIER1-AUTO", "TIER1-X-SELECT", "group", 800, 4, 0),
        ("PROXY-FALLBACK", "TIER1-SELECT", "group", 5000, 2, 1),
    ]


def test_t2_tier2_and_tier3() -> None:
    groups, graph, timeouts = _topology(pf_now="TIER2-AUTO")
    result = L.rungs(("FINAL-EXIT", "PROXY-FALLBACK", "TIER2-AUTO", "t2a"), groups, timeouts, graph=graph)
    assert _summary(result) == [
        ("TIER2-AUTO", "t2a", "leaf", 800, 3, 0),
        ("PROXY-FALLBACK", "TIER2-AUTO", "group", 5000, 2, 1),
    ]
    groups, graph, timeouts = _topology(pf_now="TIER3-AUTO")
    result = L.rungs(("FINAL-EXIT", "PROXY-FALLBACK", "TIER3-AUTO", "t3a"), groups, timeouts, graph=graph)
    assert _summary(result) == [
        ("TIER3-AUTO", "t3a", "leaf", 5000, 3, 0),
        ("PROXY-FALLBACK", "TIER3-AUTO", "group", 5000, 2, 1),
    ]
    # REJECT is never a member: TIER3-AUTO's viable members are its two leaves.
    assert result.rungs[0].members == ("t3a", "t3b")
    assert result.rungs[0].url == T3URL


def test_t2_pf_with_one_viable_member_is_not_a_rung() -> None:
    groups, graph, timeouts = _topology(tier1=None, pf_members=["TIER3-AUTO"], pf_now="TIER3-AUTO")
    result = L.rungs(("FINAL-EXIT", "PROXY-FALLBACK", "TIER3-AUTO", "t3a"), groups, timeouts, graph=graph)
    assert _summary(result) == [("TIER3-AUTO", "t3a", "leaf", 5000, 3, 0)]
    # An empty (REJECT-only) TIER2-AUTO is not a viable PF member either.
    groups, graph, timeouts = _topology(tier1=None, tier2=("REJECT",), pf_now="TIER3-AUTO")
    assert L.viable_members("TIER2-AUTO", graph) == ()
    assert L.viable_members("PROXY-FALLBACK", graph) == ("TIER3-AUTO",)
    result = L.rungs(("FINAL-EXIT", "PROXY-FALLBACK", "TIER3-AUTO", "t3a"), groups, timeouts, graph=graph)
    assert [r.group for r in result.rungs] == ["TIER3-AUTO"]


def test_t2_selects_urltest_reject_only_and_builtins_never_count() -> None:
    groups, graph, timeouts = _topology(pf_now="TIER2-AUTO")
    # TIER2-AUTO rendered as a URLTest (or a select): never a rung.
    for type_name in ("URLTest", "Selector", "LoadBalance"):
        changed = dict(groups, **{"TIER2-AUTO": replace(groups["TIER2-AUTO"], type_name=type_name)})
        result = L.rungs(("FINAL-EXIT", "PROXY-FALLBACK", "TIER2-AUTO", "t2a"), changed, timeouts, graph=graph)
        assert [r.group for r in result.rungs] == ["PROXY-FALLBACK"]
    # Builtins are not counted: [t2a, DIRECT, REJECT, PASS] has one viable member.
    members = ("t2a", "DIRECT", "REJECT", "PASS", "COMPATIBLE", "REJECT-DROP", "BOOTSTRAP-EGRESS-PROXY")
    graph2 = dict(graph, **{"TIER2-AUTO": members})
    groups2 = dict(groups, **{"TIER2-AUTO": replace(groups["TIER2-AUTO"], members=members)})
    result = L.rungs(("FINAL-EXIT", "PROXY-FALLBACK", "TIER2-AUTO", "t2a"), groups2, timeouts, graph=graph2)
    assert [r.group for r in result.rungs] == ["PROXY-FALLBACK"]
    assert set(L.real_members(members)) == {"t2a"}
    # Case-insensitive fallback test.
    assert L.is_fallback("fallback") and L.is_fallback(" FALLBACK ") and not L.is_fallback("url-test")


def test_t2_stale_now_or_missing_state_is_a_binding_mismatch() -> None:
    groups, graph, timeouts = _topology(pf_now="TIER2-AUTO")
    chain = ("FINAL-EXIT", "PROXY-FALLBACK", "TIER2-AUTO", "t2a")
    stale = dict(groups, **{"TIER2-AUTO": replace(groups["TIER2-AUTO"], now="t2b")})
    assert L.rungs(chain, stale, timeouts, graph=graph) == L.RungsResult((), "route_binding_mismatch")
    # A select above the rungs is checked too.
    stale_pf = dict(groups, **{"PROXY-FALLBACK": replace(groups["PROXY-FALLBACK"], now="TIER3-AUTO")})
    assert L.rungs(chain, stale_pf, timeouts, graph=graph).reason == "route_binding_mismatch"
    missing = {name: view for name, view in groups.items() if name != "TIER2-AUTO"}
    assert L.rungs(chain, missing, timeouts, graph=graph).reason == "route_binding_mismatch"
    # The mismatch wins over a missing contract.
    assert L.rungs(chain, stale, None, graph=graph).reason == "route_binding_mismatch"


def test_t2_missing_contract_and_no_rung() -> None:
    groups, graph, timeouts = _topology(pf_now="TIER2-AUTO")
    chain = ("FINAL-EXIT", "PROXY-FALLBACK", "TIER2-AUTO", "t2a")
    assert L.rungs(chain, groups, None, graph=graph) == L.RungsResult((), "contract_unavailable")
    partial = {k: v for k, v in timeouts.items() if k != "PROXY-FALLBACK"}
    assert L.rungs(chain, groups, partial, graph=graph).reason == "contract_unavailable"
    for bad in (0, -5, True, "800", 800.0, None):
        odd = dict(timeouts, **{"TIER2-AUTO": bad})
        assert L.rungs(chain, groups, odd, graph=graph).reason == "contract_unavailable", bad
    no_url = dict(groups, **{"TIER2-AUTO": replace(groups["TIER2-AUTO"], test_url="")})
    assert L.rungs(chain, no_url, timeouts, graph=graph).reason == "contract_unavailable"
    no_status = dict(groups, **{"TIER2-AUTO": replace(groups["TIER2-AUTO"], expected_status="")})
    assert L.rungs(chain, no_status, timeouts, graph=graph).reason == "contract_unavailable"
    # Single-member PF on a single-member tier: nothing to escalate through.
    groups, graph, timeouts = _topology(tier1=None, tier2=("t2a",), pf_members=["TIER2-AUTO"], pf_now="TIER2-AUTO")
    assert L.rungs(chain, groups, timeouts, graph=graph) == L.RungsResult((), "no_rung")
    assert L.rungs(("FINAL-EXIT", "t2a"), groups, timeouts, graph=graph).reason == "no_rung"


def test_t2_fixed0_is_the_admission_pin() -> None:
    groups, graph, timeouts = _topology(pf_now="TIER2-AUTO", t2_now="t2b")
    groups["TIER2-AUTO"] = replace(groups["TIER2-AUTO"], fixed="t2b")
    result = L.rungs(("FINAL-EXIT", "PROXY-FALLBACK", "TIER2-AUTO", "t2b"), groups, timeouts, graph=graph)
    assert result.rungs[0].fixed0 == "t2b" and result.rungs[0].accused == "t2b"
    assert result.rungs[1].fixed0 == ""


def test_group_view_reads_states_and_proxy_json_like_the_snapshot() -> None:
    from runtime_egress_recovery import CoreLoadedGroupState

    state = CoreLoadedGroupState(name="TIER2-AUTO", type_name="Fallback", members=("a", "b"), now="a",
                                 fixed="b", test_url=PROBE, expected_status="204")
    view = GroupView.from_state(state)
    assert view == GroupView("TIER2-AUTO", "Fallback", ("a", "b"), "a", "b", PROBE, "204")
    assert GroupView.from_state(object()) is None and GroupView.from_state(None) is None
    entry = {"type": "Fallback", "all": ["a", "", "b"], "now": "a", "fixed": "", "testUrl": PROBE,
             "expectedStatus": "204"}
    assert GroupView.from_proxy_json("TIER2-AUTO", entry) == replace(view, fixed="")
    assert GroupView.from_proxy_json("x1", {"type": "Socks5", "alive": True}) is None
    assert GroupView.from_proxy_json("x", None) is None
    # Same field precedence as DesktopRuntime._runtime_group_graph_and_selection.
    from desktop_runtime import DesktopRuntime

    payload = {"proxies": {"TIER2-AUTO": dict(entry, typeName="Fallback", **{"test-url": "x"})}}
    _graph, _sel, states = DesktopRuntime._runtime_group_graph_and_selection(payload)
    assert GroupView.from_state(states[0]) == GroupView.from_proxy_json("TIER2-AUTO", payload["proxies"]["TIER2-AUTO"])


# ---------------------------------------------------------------------------
# T3 successors and caps


def _rung(members, accused, *, kind=RungKind.LEAF, timeout_ms=800, group="TIER2-AUTO", fixed0="", index=0):
    return Rung(index=index, group=group, accused=accused, depth=3, kind=kind, url=PROBE, expected="204",
                timeout_ms=timeout_ms, members=tuple(members), fixed0=fixed0)


def test_t3_rung_order_is_mihomos_pick_order_with_unknown_last() -> None:
    rung = _rung(("a", "b", "c", "d", "e"), "c")
    alive = {"a": False, "b": True, "c": False, "d": False, "e": True}
    assert L.rung_order(rung, alive) == ("b", "e", "a", "d")
    assert L.rung_order(rung, alive, unknown=("b",)) == ("e", "a", "d", "b")
    # An unknown member is never treated as alive even if a map says so.
    assert L.rung_order(rung, {"b": True, "e": True}, unknown=("e", "a")) == ("b", "d", "a", "e")
    assert "c" not in L.rung_order(rung, {})


def test_t3_caps() -> None:
    c = DEFAULT_POST_DIAL_CONSTANTS
    for n, expected in ((2, 1), (3, 1), (4, 1), (5, 2), (7, 3), (12, 3)):
        assert L.rung_successor_cap(_rung([f"m{i}" for i in range(n)], "m0"), c) == expected
    for n, expected in ((2, 1), (3, 2), (4, 3), (6, 3)):
        assert L.rung_successor_cap(_rung([f"g{i}" for i in range(n)], "g0", kind=RungKind.GROUP), c) == expected
        assert L.witness_cap(_rung([f"g{i}" for i in range(n)], "g0", kind=RungKind.GROUP)) == n - 1


# ---------------------------------------------------------------------------
# T4 expected native pick, incl. the 14 measured P3 cells


def test_t4_expected_native_pick_rules() -> None:
    members = ("a", "b", "c", "REJECT")
    assert L.expected_native_pick(members, "b", {"a": True, "b": True}) == NativePick("b", False, True)
    assert L.expected_native_pick(members, "b", {"a": False, "b": False, "c": True}) == NativePick("c", False, False)
    assert L.expected_native_pick(members, "", {"a": True}) == NativePick("a", False, False)
    assert L.expected_native_pick(members, "", {}) == NativePick("a", True, False)
    assert L.expected_native_pick(("REJECT", "DIRECT", "x"), "", {"REJECT": True, "DIRECT": True}) == NativePick("x", True, False)
    assert L.expected_native_pick((), "", {}) == NativePick("", True, False)


# (group, members, pin kind, variants) -> the measured read-2 ``now`` (P3, 3/3 each).
P3_CELLS = (
    ("TIER2-AUTO", ("t2a", "t2b", "t2c"), "mid", ("nodial", "dial", "allprov"), "t2a"),
    ("TIER2-AUTO", ("t2a", "t2b", "t2c"), "last", ("nodial", "dial", "allprov"), "t2b"),
    ("TIER3-AUTO", ("t3a", "t3b", "t3c", "REJECT"), "mid", ("nodial", "prov", "dial", "allprov"), "t3a"),
    ("TIER3-AUTO", ("t3a", "t3b", "t3c", "REJECT"), "last", ("nodial", "prov", "dial", "allprov"), "t3b"),
)


def _p3_alive(members, pin_kind):
    # mid: pin = member[1] failed its A_C, the others alive.  last: pin =
    # member[2] failed, and member[0] was failed by its own /delay first.
    real = [m for m in members if m != "REJECT"]
    pin = real[1] if pin_kind == "mid" else real[2]
    alive = {m: True for m in real}
    alive[pin] = False
    if pin_kind == "last":
        alive[real[0]] = False
    return pin, alive


def test_t4_the_14_p3_cells_equal_the_computed_pick() -> None:
    cells = [(g, m, k, v, now) for g, m, k, variants, now in P3_CELLS for v in variants]
    assert len(cells) == 14
    for group, members, kind, _variant, read2 in cells:
        pin, alive = _p3_alive(members, kind)
        # Read 2 shows fixed "" (the drop is consumed); the dead pin would not win either.
        assert L.expected_native_pick(members, "", alive).pick == read2
        assert L.expected_native_pick(members, pin, alive).pick == read2


@pytest.mark.skipif(not P3_TRIALS.exists(), reason="P3 raw trials are not in this checkout")
def test_t4_p3_raw_trials_match_the_cells() -> None:
    trials = json.loads(P3_TRIALS.read_text(encoding="utf-8"))["trials"]
    expected = {(g, k, v): now for g, _m, k, variants, now in P3_CELLS for v in variants}
    seen = set()
    for trial in trials:
        key = (trial["group"], trial["pin_kind"], trial["variant"])
        read2 = next(step["read2"] for step in trial["seq"] if "read2" in step)
        assert read2["fixed"] == "" and read2["now"] == expected[key], key
        seen.add(key)
    assert seen == set(expected) and len(trials) == 42


# ---------------------------------------------------------------------------
# T5 complete / rung_verdict


def test_t5_complete_counts_failed_in_ladder_and_core_dead_never_unknown() -> None:
    rung = _rung(("a", "b", "c", "d"), "a")
    assert L.complete(rung, failed_in_ladder=("b", "c", "d"), read_alive=None)
    assert not L.complete(rung, failed_in_ladder=("b", "c"), read_alive=None)
    assert L.complete(rung, failed_in_ladder=("b",), read_alive={"c": False, "d": False})
    assert not L.complete(rung, failed_in_ladder=("b",), read_alive={"c": False, "d": True})
    assert not L.complete(rung, failed_in_ladder=("b",), read_alive={"c": False})  # d not read
    assert not L.complete(rung, failed_in_ladder=("b",), read_alive={"c": False, "d": False}, read_unknown=("d",))
    assert L.complete(rung, failed_in_ladder=("b", "d"), read_alive={"c": False}, read_unknown=("d",))


def test_t5_rung_verdict_splits_exhausted() -> None:
    rung = _rung(("a", "b", "c", "d", "e"), "a")
    succ = ("b", "c")
    results = {"A_C": ProbeClass.TRANSPORT_FAIL, "S_C0": ProbeClass.TRANSPORT_FAIL, "S_C1": ProbeClass.TRANSPORT_FAIL}
    # cap 2 over 5 members: d and e unprobed.
    partial = L.rung_verdict(results, 2, 2, succ, rung=rung, read_alive={"d": True, "e": False})
    assert partial.verdict is RungVerdict.EXHAUSTED_PARTIAL and partial.base is Verdict.EXHAUSTED
    assert partial.reason == "successors_transport_failed"
    whole = L.rung_verdict(results, 2, 2, succ, rung=rung, read_alive={"d": False, "e": False})
    assert whole.verdict is RungVerdict.EXHAUSTED_COMPLETE
    # The failed successors count by themselves; a TIMEOUT failed in the ladder counts too.
    via_ladder = L.rung_verdict(results, 2, 2, succ, rung=rung, read_alive={"e": False}, failed_in_ladder=("d",))
    assert via_ladder.verdict is RungVerdict.EXHAUSTED_COMPLETE
    unknown = L.rung_verdict(results, 2, 2, succ, rung=rung, read_alive={"d": False, "e": False}, read_unknown=("e",))
    assert unknown.verdict is RungVerdict.EXHAUSTED_PARTIAL
    # no_other_member: complete by definition.
    lone = L.rung_verdict({"A_C": ProbeClass.TIMEOUT}, 0, 0, (), rung=_rung(("a",), "a"), read_alive=None)
    assert lone.verdict is RungVerdict.EXHAUSTED_COMPLETE and lone.reason == "no_other_member"
    assert L.failed_successors({"S_C0": "timeout", "S_C1": "http_answer", "S_C2": "pass"}, 3, ("x", "y", "z")) == {"x", "y"}


@pytest.mark.parametrize(
    ("results", "started", "successors", "expected"),
    [
        ({"A_C": "pass"}, 0, ("b",), RungVerdict.NOT_REPRODUCED),
        ({"A_C": "http_answer"}, 0, ("b",), RungVerdict.NOT_REPRODUCED),
        ({"A_C": "transport_fail", "S_C0": "pass"}, 1, ("b",), RungVerdict.CONVICT_CONTRACT),
        ({"A_C": "timeout", "S_C0": "timeout"}, 1, ("b",), RungVerdict.FACILITY),
        ({"A_C": "timeout"}, 1, ("b",), RungVerdict.INDETERMINATE),
        ({}, 0, None, RungVerdict.INDETERMINATE),
    ],
)
def test_t5_non_exhausted_verdicts_pass_through(results, started, successors, expected) -> None:
    rung = _rung(("a", "b", "c"), "a")
    result = L.rung_verdict(results, started, 1, successors, rung=rung, read_alive=None)
    assert result.verdict is expected
    if expected is RungVerdict.CONVICT_CONTRACT:
        assert result.successor == "b" and result.successor_index == 0 and result.base is Verdict.CONVICT_CONTRACT


def test_legacy_hold_mapping() -> None:
    assert L.legacy_hold(RungVerdict.EXHAUSTED_COMPLETE) is Verdict.EXHAUSTED
    assert L.legacy_hold(RungVerdict.EXHAUSTED_PARTIAL) is Verdict.EXHAUSTED
    assert L.legacy_hold(RungVerdict.FACILITY) is Verdict.FACILITY
    assert L.legacy_hold(EscalationCause.EXHAUSTED) is Verdict.EXHAUSTED
    assert L.legacy_hold(EscalationCause.FACILITY) is Verdict.FACILITY
    for other in (RungVerdict.CONVICT_CONTRACT, RungVerdict.NOT_REPRODUCED, RungVerdict.INDETERMINATE, None):
        assert L.legacy_hold(other) is None


# ---------------------------------------------------------------------------
# T6 ladder_next (§3.5 table)


def _rv(value):
    return RungVerdictResult(value, "r", None, None, None)


def _next(value, index=0, n=3, *, cause=None, deeper_moved=False, fits_escalation=True, commit_allowed=True, **kw):
    step = L.ladder_next(_rv(value), index, n, cause=cause, deeper_moved=deeper_moved,
                         fits_escalation=fits_escalation, commit_allowed=commit_allowed, **kw)
    return (step.action, step.outcome, step.hold, step.closes, step.cause, step.bookkeeping)


E, F = EscalationCause.EXHAUSTED, EscalationCause.FACILITY
V = Verdict


def test_t6_every_row_of_the_table() -> None:
    R = RungVerdict
    assert _next(R.CONVICT_CONTRACT) == ("commit", "convicted", None, "legacy", None, V.CONVICT_CONTRACT)
    assert _next(R.CONVICT_CONTRACT, 1, cause=E) == ("commit", "convicted", None, "after_commit", None, V.CONVICT_CONTRACT)
    # A group accused at rung ①: closes only after the accepted commit.
    assert _next(R.CONVICT_CONTRACT, accused_kind=RungKind.GROUP)[3] == "after_commit"
    assert _next(R.CONVICT_CONTRACT, accused_kind="leaf")[3] == "legacy"
    assert _next(R.CONVICT_CONTRACT, commit_allowed=False) == ("stop", "commit_late", None, "legacy", None, V.CONVICT_CONTRACT)
    assert _next(R.CONVICT_CONTRACT, 2, cause=F, commit_allowed=False)[1] == "commit_late"
    assert _next(R.NOT_REPRODUCED) == ("stop", "not_reproduced", None, "none", None, V.NOT_REPRODUCED)
    assert _next(R.NOT_REPRODUCED, 1, cause=E, deeper_moved=True) == (
        "stop", "recovered_by_deeper_move", None, "after_commit", None, V.CONVICT_CONTRACT)
    assert _next(R.NOT_REPRODUCED, 1, cause=F, deeper_moved=True) == (
        "stop", "facility_transient", None, "none", None, V.INDETERMINATE)
    assert _next(R.NOT_REPRODUCED, 1, cause=E) == ("stop", "not_reproduced_upper", None, "none", None, V.INDETERMINATE)
    assert _next(R.EXHAUSTED_COMPLETE, 0) == ("escalate", None, None, "none", E, None)
    assert _next(R.EXHAUSTED_COMPLETE, 0, fits_escalation=False) == (
        "stop", "ladder_budget_exhausted", V.EXHAUSTED, "legacy", None, V.EXHAUSTED)
    assert _next(R.EXHAUSTED_COMPLETE, 2, cause=E) == ("stop", "exhausted_hold", V.EXHAUSTED, "legacy", None, V.EXHAUSTED)
    assert _next(R.EXHAUSTED_COMPLETE, 0, n=1) == ("stop", "exhausted_hold", V.EXHAUSTED, "legacy", None, V.EXHAUSTED)
    for index in (0, 1, 2):
        assert _next(R.EXHAUSTED_PARTIAL, index, cause=E if index else None) == (
            "stop", "moved_unproven", V.EXHAUSTED, "legacy", None, V.EXHAUSTED)
    assert _next(R.FACILITY, 0) == ("escalate", None, None, "none", F, None)
    assert _next(R.FACILITY, 0, fits_escalation=False) == (
        "stop", "ladder_budget_exhausted", V.FACILITY, "legacy", None, V.FACILITY)
    assert _next(R.FACILITY, 2, cause=F) == ("stop", "facility_confirmed", V.FACILITY, "legacy", None, V.FACILITY)
    for index in (0, 2):
        assert _next(R.INDETERMINATE, index) == ("stop", "indeterminate", None, "legacy", None, V.INDETERMINATE)


# ---------------------------------------------------------------------------
# T7 witness_next (§3.6)


def _wn(cause, order, results, *, cap=None, fits_next=True, fits_ac=True, rung_is_last=False,
        fits_escalation=True, complete_if_exhausted=True):
    step = L.witness_next(cause, order, results, cap=len(order) if cap is None else cap, fits_next=fits_next,
                          fits_ac=fits_ac, rung_is_last=rung_is_last, fits_escalation=fits_escalation,
                          complete_if_exhausted=complete_if_exhausted)
    return (step.action, step.target, step.outcome, step.hold, step.passed, step.wasted)


def test_t7_first_witness_is_always_probed_in_order() -> None:
    assert _wn(E, ("w1", "w2"), {}, fits_next=False) == ("probe", "w1", None, None, None, False)
    assert _wn(F, ("w1", "w2"), {}) == ("probe", "w1", None, None, None, False)


def test_t7_pass_issues_the_ac_or_wastes_it_on_f4() -> None:
    assert _wn(E, ("w1", "w2"), {"w1": "transport_fail", "w2": "pass"}) == ("issue_ac", None, None, None, "w2", False)
    assert _wn(F, ("w1",), {"w1": ProbeClass.PASS}) == ("issue_ac", None, None, None, "w1", False)
    assert _wn(E, ("w1",), {"w1": "pass"}, fits_ac=False) == (
        "stop", None, "ladder_budget_exhausted", V.EXHAUSTED, None, True)
    assert _wn(F, ("w1",), {"w1": "pass"}, fits_ac=False) == (
        "stop", None, "ladder_budget_exhausted", V.FACILITY, None, True)


def test_t7_facility_timeout_confirms_exhausted_timeout_moves_on() -> None:
    assert _wn(F, ("w1", "w2"), {"w1": "timeout"}) == ("stop", None, "facility_confirmed", V.FACILITY, None, False)
    assert _wn(E, ("w1", "w2"), {"w1": "timeout"}) == ("probe", "w2", None, None, None, False)
    # transport_fail / http_answer under FACILITY: the next witness.
    assert _wn(F, ("w1", "w2"), {"w1": "transport_fail"}) == ("probe", "w2", None, None, None, False)
    assert _wn(F, ("w1", "w2"), {"w1": "http_answer"}) == ("probe", "w2", None, None, None, False)


def test_t7_all_failed_under_exhausted() -> None:
    failed = {"w1": "transport_fail", "w2": "http_answer"}
    assert _wn(E, ("w1", "w2"), failed) == ("escalate", None, None, None, None, False)
    assert _wn(E, ("w1", "w2"), failed, rung_is_last=True) == ("stop", None, "exhausted_hold", V.EXHAUSTED, None, False)
    assert _wn(E, ("w1", "w2"), failed, fits_escalation=False) == (
        "stop", None, "ladder_budget_exhausted", V.EXHAUSTED, None, False)
    # An untested Core-alive member left (not complete): no A_C at this rung.
    assert _wn(E, ("w1", "w2"), failed, complete_if_exhausted=False) == (
        "stop", None, "witness_incomplete", V.EXHAUSTED, None, False)
    assert _wn(E, ("w1", "w2", "w3"), failed, cap=2, complete_if_exhausted=False)[2] == "witness_incomplete"


def test_t7_f3_refusal_unwitnessed_and_indeterminate() -> None:
    assert _wn(E, ("w1", "w2"), {"w1": "transport_fail"}, fits_next=False) == (
        "stop", None, "ladder_budget_exhausted", V.EXHAUSTED, None, False)
    assert _wn(F, ("w1", "w2"), {"w1": "transport_fail"}, fits_next=False)[2:4] == ("ladder_budget_exhausted", V.FACILITY)
    assert _wn(F, ("w1", "w2"), {"w1": "transport_fail", "w2": "http_answer"}) == (
        "stop", None, "escalation_unwitnessed", V.FACILITY, None, False)
    assert _wn(E, ("w1", "w2"), {"w1": "indeterminate"}) == ("stop", None, "indeterminate", None, None, False)
    assert _wn(F, ("w1", "w2"), {"w1": "transport_fail", "w2": "indeterminate"})[2:4] == ("indeterminate", None)
    # Results for members outside the order are ignored.
    assert _wn(E, ("w1",), {"zz": "pass"}) == ("probe", "w1", None, None, None, False)


def test_s15_shape_five_physical_nodes_witnesses_in_order_while_f3_fits() -> None:
    order = ("Y1", "Y2", "Y3", "Y4")
    results = {"Y1": "transport_fail", "Y2": "transport_fail", "Y3": "transport_fail"}
    assert _wn(E, order, results) == ("probe", "Y4", None, None, None, False)
    assert _wn(E, order, results, fits_next=False)[2] == "ladder_budget_exhausted"
    assert _wn(E, order, dict(results, Y4="pass")) == ("issue_ac", None, None, None, "Y4", False)


# ---------------------------------------------------------------------------
# T8 windows, budget, fit


def test_t8_numbers() -> None:
    assert (L.successor_delay_s(800), L.successor_delay_s(5000)) == (0.4, 2.0)
    assert (L.evidence_s(800, witnessed=False), L.evidence_s(5000, witnessed=False)) == (3.2, 9.0)
    assert (L.evidence_s(800, witnessed=True), L.evidence_s(5000, witnessed=True)) == (2.8, 7.0)
    assert (L.rung_ticket_s(800, witnessed=False), L.rung_ticket_s(5000, witnessed=False)) == (3.7, 9.5)
    assert (L.rung_ticket_s(800, witnessed=True), L.rung_ticket_s(5000, witnessed=True)) == (3.3, 7.5)
    assert (L.witness_s(800), L.witness_s(5000)) == (2.8, 7.0)
    assert (L.probe_await_s(800), L.probe_await_s(5000)) == (2.8, 7.0)
    assert (L.probe_request_window_s(800), L.probe_request_window_s(5000)) == (1.45, 5.65)
    assert L.probe_request_window_s(5000) == DEFAULT_POST_DIAL_CONSTANTS.probe_request_timeout_s
    leaf800, grp800, grp5000 = _rung(("a", "b"), "a"), _rung(("a", "b"), "a", kind=RungKind.GROUP), _rung(
        ("a", "b"), "a", kind=RungKind.GROUP, timeout_ms=5000)
    assert (L.escalation_need_s(grp800), L.escalation_need_s(grp5000)) == (6.85, 15.25)
    assert L.ladder_budget_s([leaf800, grp5000]) == 18.95
    assert L.ladder_budget_s([leaf800, grp800, grp5000]) == 20.0
    assert L.ladder_budget_s([_rung(("a", "b"), "a", timeout_ms=5000), grp5000]) == 20.0
    assert L.ladder_budget_s([_rung(("a", "b"), "a", timeout_ms=5000)]) == 9.5
    assert L.ladder_budget_s([]) == 0.0
    assert L.witness_fit_need_s(800) == 6.1 and L.ac_after_witness_need_s(5000) == 7.5
    assert L.rung_ticket_s(5000, witnessed=False) <= L.LADDER_CAP_S  # F1


def test_t8_fits_edges() -> None:
    assert L.fits(10.0, 20.0, 10.0)
    assert not L.fits(10.0, 20.0, 10.01)
    # Tier1 3-rung: the PF escalation (need 15.25) fits at elapsed 4.75, not at 4.76.
    admission = 1000.0
    deadline = admission + L.LADDER_CAP_S
    pf = _rung(("TIER1-SELECT", "TIER2-AUTO"), "TIER1-SELECT", kind=RungKind.GROUP, timeout_ms=5000,
               group="PROXY-FALLBACK")
    assert L.fits(admission + 4.75, deadline, L.escalation_need_s(pf))
    assert not L.fits(admission + 4.76, deadline, L.escalation_need_s(pf))


def test_t8_the_800ms_window_covers_a_full_queue_wait() -> None:
    queue, tail = L.LADDER_PROBE_ADMISSION_S, L.LADDER_RESULT_SLACK_S
    for T in (800, 5000):
        assert L.evidence_s(T, witnessed=False) - L.successor_delay_s(T) >= queue + T / 1000 + tail - 1e-9
        # A_C alone (witnessed) also fits one queue wait.
        assert L.evidence_s(T, witnessed=True) >= queue + T / 1000 + tail - 1e-9


def test_rung_constants_change_only_the_contract_fields() -> None:
    base = DEFAULT_POST_DIAL_CONSTANTS
    for witnessed in (False, True):
        c = L.rung_constants(_rung(("a", "b"), "a"), base, witnessed=witnessed)
        assert (c.native_timeout_ms, c.probe_request_timeout_s, c.s_c0_delay_s, c.native_tail_s) == (800, 1.45, 0.4, 0.5)
        changed = {f.name for f in fields(base) if getattr(base, f.name) != getattr(c, f.name)}
        assert changed == {"native_timeout_ms", "probe_request_timeout_s", "s_c0_delay_s"}
        assert c.min_probe_remaining_s == 1.0
    c5 = L.rung_constants(_rung(("a", "b"), "a", timeout_ms=5000), base, witnessed=False)
    assert c5 == base


def test_constants_match_the_spec() -> None:
    assert EXCLUDED_MEMBERS == {"REJECT", "REJECT-DROP", "DIRECT", "PASS", "COMPATIBLE", "BOOTSTRAP-EGRESS-PROXY"}
    assert (L.LADDER_MARGIN_S, L.LADDER_PROBE_ADMISSION_S, L.LADDER_CAP_S, L.LADDER_REFRESH_ALLOWANCE_S,
            L.LADDER_REQUEST_EXTRA_S, L.LADDER_NATIVE_TAIL_S, L.LADDER_RESULT_SLACK_S, L.LADDER_END_SLACK_S) == (
        2.0, 1.9, 20.0, 0.75, 0.65, 0.5, 0.1, 0.5)
    assert L.LADDER_NATIVE_TAIL_S == DEFAULT_POST_DIAL_CONSTANTS.native_tail_s


# ---------------------------------------------------------------------------
# T11 graph helpers


def test_t11_descendants_and_judged_leaves() -> None:
    groups, graph, _ = _topology()
    graph = dict(graph, **{"TIER3-AUTO": ("prov-a", "prov-b", "REJECT")})
    below = L.descendant_groups(graph)
    assert below == {"PROXY-FALLBACK", "TIER1-SELECT", "TIER1-AUTO", "TIER1-X-SELECT", "TIER1-Y-SELECT",
                     "TIER1-X-AUTO", "TIER1-Y-AUTO", "TIER2-AUTO", "TIER3-AUTO"}
    assert "FINAL-EXIT" not in below and "GENERAL" not in below
    assert L.descendant_groups(graph, root="missing") == frozenset()
    leaves = L.judged_leaves(graph)
    assert leaves == {"x1", "x2", "y1", "y2", "t2a", "t2b", "prov-a", "prov-b"}
    assert not leaves & EXCLUDED_MEMBERS
    payload = {name: {"type": "Fallback", "all": list(members), "now": ""} for name, members in graph.items()}
    payload.update({"x1": {"type": "Socks5"}, "odd": {"all": "notalist"}, "junk": None})
    assert L.descendant_groups_of_payload(payload) == below
    assert L.descendant_groups_of_payload(None) == frozenset() and L.descendant_groups_of_payload([]) == frozenset()


def test_t11_viable_members_is_cycle_safe() -> None:
    graph = {"A": ("B", "x"), "B": ("A",), "C": ("C",), "D": ("REJECT", "DIRECT"), "E": ("D", "C", "y")}
    assert L.viable_members("A", graph) == ("x",)
    assert L.viable_members("B", graph) == ("A",)
    assert L.viable_members("C", graph) == ()
    assert L.viable_members("E", graph) == ("y",)
    assert L.viable_members("missing", graph) == ()


def test_t11_group_codes_and_token_field() -> None:
    names = {"PROXY-FALLBACK", "TIER1-SELECT", "TIER1-AUTO", "TIER2-AUTO", "TIER3-AUTO", "FINAL-EXIT",
             "TIER1-node.example-AUTO", "TIER1-node.example-SELECT", "EXIT-SELECT"}
    code = lambda name: L.group_code(name, group_names=names)  # noqa: E731
    assert [code(n) for n in ("PROXY-FALLBACK", "TIER1-SELECT", "TIER1-AUTO", "TIER2-AUTO", "TIER3-AUTO", "FINAL-EXIT")] == [
        "PF", "T1S", "T1", "T2", "T3", "FE"]
    assert code("TIER1-node.example-AUTO") == "T1X" and code("TIER1-node.example-SELECT") == "T1XS"
    assert code("EXIT-SELECT") == "G" and code("leaf.example.net") == "L"
    assert L.token_field("0123456789ab") == "T0123456789ab" and L.token_field(None) == "none"
    assert L.rung_gate_key("TIER2-AUTO", "t2a") == "rung\x00TIER2-AUTO\x00t2a"
    assert L.ladder_id("incident-1").startswith("D") and len(L.ladder_id("incident-1")) == 12
    assert L.alive_at({"a": {"alive": True}, "b": {"alive": True, "extra": {PROBE: {"alive": False}}}}, PROBE,
                      ("a", "b", "c")) == {"a": True, "b": False, "c": False}


# ---------------------------------------------------------------------------
# T12 demotion and authority


def test_t12_rung_demoted_truth_table() -> None:
    rung = _rung(("a", "b", "c"), "a")
    assert L.rung_demoted("b", {"a": False}, (), rung, "b")
    assert not L.rung_demoted("c", {"a": False}, (), rung, "b")      # now is not the pick
    assert not L.rung_demoted("a", {"a": False}, (), rung, "a")      # pick is the accused
    assert not L.rung_demoted("b", {"a": True}, (), rung, "b")       # accused alive
    assert not L.rung_demoted("b", {}, (), rung, "b")                # accused not read
    assert not L.rung_demoted("b", {"a": False}, ("a",), rung, "b")  # accused unknown
    assert not L.rung_demoted("", {"a": False}, (), rung, "")


def _authority(**changes):
    base = LadderAuthority(
        pid=1, process_generation=2, core_generation=3, semantic_digest="d", network_context_epoch=4,
        selector_intent_revision=5, facility_epoch=6, lifecycle_revision=7, worker_token=(1, 2),
        group_graph=(("PF", ("a",)),), policy_key=(True, True, False), lease_obj_id=None,
        lease_release_state="", lease_digest="0" * 16,
    )
    return replace(base, **changes)


def test_t12_authority_diff_names_the_first_changed_field() -> None:
    base = _authority()
    assert L.authority_diff(base, _authority()) is None
    expected = {
        "pid": 9, "process_generation": 9, "core_generation": 9, "semantic_digest": "e",
        "network_context_epoch": 9, "selector_intent_revision": 9, "facility_epoch": 9,
        "lifecycle_revision": 9, "worker_token": (9,), "group_graph": (),
    }
    for name, value in expected.items():
        assert L.authority_diff(base, _authority(**{name: value})) == f"authority_changed:{name}"
    assert L.authority_diff(base, _authority(policy_key=(False, True, False))) == "authority_changed:policy"
    for name, value in (("lease_obj_id", 42), ("lease_release_state", "releasing"), ("lease_digest", "1" * 16)):
        assert L.authority_diff(base, _authority(**{name: value})) == "authority_changed:lease"
    # Declaration order: the earliest field wins.
    assert L.authority_diff(base, _authority(facility_epoch=0, lease_digest="x", pid=0)) == "authority_changed:pid"
    assert L.authority_diff(base, _authority(policy_key=(), lease_digest="x")) == "authority_changed:policy"


def test_lease_digest_ignores_counters_and_reacts_to_clock_state() -> None:
    from runtime_route_lease import LeaseClock

    clock = LeaseClock(group="TIER2-AUTO", member="t2b", started_mono=10.0001, started_wall=1.0,
                       last_decision_mono=10.0, last_decision_wall=1.0)
    digest = L.lease_digest([clock])
    assert len(digest) == 16
    assert L.lease_digest([replace(clock, purge_attempts=5, last_decision_mono=99.0, last_put_mono=3.0)]) == digest
    assert L.lease_digest([replace(clock, started_mono=10.0004)]) == digest
    for changed in (replace(clock, ended="core_cleared"), replace(clock, purge_pending=True),
                    replace(clock, verify_retry=True), replace(clock, member="t2c"), replace(clock, started_mono=11.0)):
        assert L.lease_digest([changed]) != digest
    other = replace(clock, group="TIER1-AUTO")
    assert L.lease_digest([clock, other]) == L.lease_digest([other, clock])
    assert L.lease_digest([]) != digest
    assert L.lease_digest([SimpleNamespace(group="g", member="m", started_mono="junk")]) == L.lease_digest(
        [SimpleNamespace(group="g", member="m", started_mono=0.0)])


def test_module_has_no_io_imports() -> None:
    text = (ROOT / "runtime_tier_ladder.py").read_bytes()
    assert text.count(b"\r\n") == 0
    for banned in (b"import os", b"import socket", b"import threading", b"import time", b"open(", b"requests",
                   b"mihomo_api", b"desktop_runtime"):
        assert banned not in text, banned
