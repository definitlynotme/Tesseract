"""Pure tier-ladder model for the post-dial judge (``tier_ladder_judge``; spec v1 §3).

With no gateway on the published FINAL-EXIT path, the judge may judge the
active leaf of PROXY-FALLBACK at any tier.  It escalates through the native
fallback *rungs* of that path, deepest first; every move is Mihomo's own (a
failed contract sample on a rung group's member makes that group move to its
next alive member in order).  This module only decides: rungs, successor and
witness order, windows and budget, rung verdicts, ladder and witness steps,
the demotion check for group-rung closes and the authority compare.  It does
no I/O and holds no state.

Nothing here runs while ``tier_ladder_judge`` is false (the default).
"""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass, fields
from enum import Enum
from typing import Any, Collection, Iterable, Mapping, Sequence

from runtime_node_judge import (
    ProbeClass,
    Verdict,
    member_alive,
    probe_class,
    successor_cap,
    successor_list,
    successor_probe_name,
    verdict,
)
from runtime_post_dial import DEFAULT_POST_DIAL_CONSTANTS, PostDialConstants, constants_with


# ---------------------------------------------------------------------------
# Constants (§3.1)

# Builtin terminals and the bootstrap proxy: never accused, never probed as a
# successor or witness, never counted as a member.
EXCLUDED_MEMBERS = frozenset(
    {"REJECT", "REJECT-DROP", "DIRECT", "PASS", "COMPATIBLE", "BOOTSTRAP-EGRESS-PROXY"}
)
LADDER_MARGIN_S = 2.0              # design §19.4 item 1 (P5 max queue wait 1526.9 ms)
LADDER_PROBE_ADMISSION_S = 1.9     # design §19.4 item 2 (ladder probes only; legacy 1.5 kept)
LADDER_CAP_S = 20.0                # design §5.3 hard cap, from admission
LADDER_REFRESH_ALLOWANCE_S = 0.75  # per escalation
LADDER_REQUEST_EXTRA_S = 0.65      # HTTP window = T + 0.65 (legacy 5.65 for 5000)
LADDER_NATIVE_TAIL_S = 0.5         # = legacy native_tail_s
LADDER_RESULT_SLACK_S = 0.1        # controller overhead incl. the expected-status readback
LADDER_END_SLACK_S = 0.5           # LS4 tolerance over the cap (emit, clock)

PROXY_FALLBACK = "PROXY-FALLBACK"
FINAL_EXIT = "FINAL-EXIT"
TIER3_AUTO = "TIER3-AUTO"

# Rounding of the derived windows: keeps 0.8 + 0.65 == 1.45 exact.
_WINDOW_DIGITS = 6
_FIT_EPSILON = 1e-9


# ---------------------------------------------------------------------------
# Views, rungs, graph helpers (§3.2)


class RungKind(str, Enum):
    LEAF = "leaf"
    GROUP = "group"


def _text(value: Any) -> str:
    return str(value or "")


@dataclass(frozen=True, slots=True)
class GroupView:
    """The selection-relevant state of one loaded group."""

    name: str
    type_name: str
    members: tuple[str, ...]
    now: str
    fixed: str
    test_url: str
    expected_status: str

    @classmethod
    def from_state(cls, state: Any) -> "GroupView | None":
        """From a ``CoreLoadedGroupState`` (duck type); None for anything else."""

        name = getattr(state, "name", None)
        if not isinstance(name, str) or not name:
            return None
        members = getattr(state, "members", ())
        if not isinstance(members, (list, tuple)):
            members = ()
        return cls(
            name=name,
            type_name=_text(getattr(state, "type_name", "")),
            members=tuple(str(member) for member in members if str(member or "").strip()),
            now=_text(getattr(state, "now", "")),
            fixed=_text(getattr(state, "fixed", "")),
            test_url=_text(getattr(state, "test_url", "")),
            expected_status=_text(getattr(state, "expected_status", "")),
        )

    @classmethod
    def from_proxy_json(cls, name: str, payload: Any) -> "GroupView | None":
        """From one ``GET /proxies`` entry, read exactly as the route snapshot
        reads it (``_runtime_group_graph_and_selection``); None for a non-group."""

        if not isinstance(payload, Mapping) or not str(name or ""):
            return None
        members = payload.get("all")
        if members is None:
            members = payload.get("proxies")
        if not isinstance(members, (list, tuple)):
            return None
        return cls(
            name=str(name),
            type_name=str(payload.get("typeName") or payload.get("type") or ""),
            members=tuple(str(member) for member in members if str(member or "").strip()),
            now=str(payload.get("now") or payload.get("selected") or ""),
            fixed=str(payload.get("fixed") or ""),
            test_url=str(payload.get("testUrl") or payload.get("test-url") or payload.get("url") or ""),
            expected_status=str(payload.get("expectedStatus") or payload.get("expected-status") or ""),
        )


@dataclass(frozen=True, slots=True)
class Rung:
    index: int                 # 0 = deepest (rung ①)
    group: str
    accused: str               # chain[depth]
    depth: int
    kind: RungKind             # LEAF iff the accused is not a group name
    url: str
    expected: str
    timeout_ms: int
    members: tuple[str, ...]   # viable members, ``all`` order
    fixed0: str                # the group's ``fixed`` in the admission snapshot ("" or a member)


ROUTE_BINDING_MISMATCH = "route_binding_mismatch"
CONTRACT_UNAVAILABLE = "contract_unavailable"
NO_RUNG = "no_rung"


@dataclass(frozen=True, slots=True)
class RungsResult:
    rungs: tuple[Rung, ...]
    reason: str | None   # None | "route_binding_mismatch" | "contract_unavailable" | "no_rung"


def real_members(members: Iterable[Any], exclusions: Collection[str] = EXCLUDED_MEMBERS) -> tuple[str, ...]:
    """``members`` in order without builtins, exclusions and empty names."""

    return tuple(
        str(name) for name in (members or ()) if str(name or "").strip() and str(name) not in exclusions
    )


def viable_members(
    group: str,
    graph: Mapping[str, Sequence[str]],
    exclusions: Collection[str] = EXCLUDED_MEMBERS,
) -> tuple[str, ...]:
    """``real_members(graph[group])`` minus member groups with no viable member
    themselves (recursive, cycle-safe: a group met again on the current path
    counts as not viable).  An empty tier renders a REJECT-only group, which
    can never be a good successor or witness."""

    memo: dict[str, bool] = {}

    def viable_group(name: str, path: frozenset[str]) -> bool:
        if name in memo:
            return memo[name]
        if name in path:
            return False
        inner = path | {name}
        result = False
        for member in real_members(graph.get(name, ()), exclusions):
            if member not in graph or viable_group(member, inner):
                result = True
                break
        memo[name] = result
        return result

    root = str(group)
    path = frozenset({root})
    return tuple(
        member
        for member in real_members(graph.get(root, ()), exclusions)
        if member not in graph or viable_group(member, path)
    )


def is_fallback(type_name: str) -> bool:
    return str(type_name or "").strip().casefold() == "fallback"


def _timeout_ms(value: Any) -> int | None:
    if type(value) is not int or value <= 0:
        return None
    return int(value)


def rungs(
    chain: Sequence[str],
    groups: Mapping[str, GroupView | None],
    loaded_timeouts: Mapping[str, int] | None,
    *,
    graph: Mapping[str, Sequence[str]],
    exclusions: Collection[str] = EXCLUDED_MEMBERS,
) -> RungsResult:
    """The native fallback rungs of ``chain`` (FINAL-EXIT first), deepest first.

    Every group on the chain below FINAL-EXIT must publish ``now`` equal to
    the next chain element, else ``route_binding_mismatch``.  A group is a
    rung iff it is a fallback with at least two viable members, one of them
    the next chain element; selects and URLTest groups never are.  Each rung
    needs its URL, expected status and loaded timeout, else
    ``contract_unavailable``; no rung at all is ``no_rung``.
    """

    path = tuple(str(name) for name in chain)
    group_names = frozenset(graph) | frozenset(name for name, view in groups.items() if view is not None)
    found: list[tuple[int, GroupView, tuple[str, ...]]] = []
    for i in range(len(path) - 2, 0, -1):
        name = path[i]
        view = groups.get(name)
        if view is None or view.now != path[i + 1]:
            return RungsResult((), ROUTE_BINDING_MISMATCH)
        if not is_fallback(view.type_name):
            continue
        members = viable_members(name, graph, exclusions)
        if len(members) < 2 or path[i + 1] not in members:
            continue
        found.append((i, view, members))
    if not found:
        return RungsResult((), NO_RUNG)
    result: list[Rung] = []
    for index, (i, view, members) in enumerate(found):
        timeout = None if loaded_timeouts is None else _timeout_ms(loaded_timeouts.get(view.name))
        if not view.test_url or not view.expected_status or timeout is None:
            return RungsResult((), CONTRACT_UNAVAILABLE)
        accused = path[i + 1]
        result.append(
            Rung(
                index=index,
                group=view.name,
                accused=accused,
                depth=i + 1,
                kind=RungKind.GROUP if accused in group_names else RungKind.LEAF,
                url=view.test_url,
                expected=view.expected_status,
                timeout_ms=timeout,
                members=members,
                fixed0=view.fixed,
            )
        )
    return RungsResult(tuple(result), None)


def _bfs_groups(graph: Mapping[str, Sequence[str]], root: str) -> list[str]:
    if root not in graph:
        return []
    seen = [root]
    queue = deque([root])
    while queue:
        name = queue.popleft()
        for member in graph.get(name, ()) or ():
            member = str(member)
            if member in graph and member not in seen:
                seen.append(member)
                queue.append(member)
    return seen


def descendant_groups(graph: Mapping[str, Sequence[str]], *, root: str = PROXY_FALLBACK) -> frozenset[str]:
    """Every group reachable from ``root`` in the loaded group graph, root included."""

    return frozenset(_bfs_groups(graph, str(root)))


def _payload_graph(proxies: Any) -> dict[str, tuple[str, ...]]:
    graph: dict[str, tuple[str, ...]] = {}
    if not isinstance(proxies, Mapping):
        return graph
    for name, entry in proxies.items():
        if isinstance(entry, Mapping) and isinstance(entry.get("all"), list):
            graph[str(name)] = tuple(str(member) for member in entry["all"] if str(member or "").strip())
    return graph


def descendant_groups_of_payload(proxies: Any, *, root: str = PROXY_FALLBACK) -> frozenset[str]:
    """``descendant_groups`` over a ``/proxies`` payload's ``proxies`` map
    (entries whose ``all`` is a list), root included: the D4 guard's input."""

    return descendant_groups(_payload_graph(proxies), root=root)


def judged_leaves(
    graph: Mapping[str, Sequence[str]],
    *,
    root: str = PROXY_FALLBACK,
    exclusions: Collection[str] = EXCLUDED_MEMBERS,
) -> frozenset[str]:
    """Non-group members reachable from ``root`` (provider members included,
    because ``graph`` is the loaded group graph), builtins excluded."""

    leaves: set[str] = set()
    for name in _bfs_groups(graph, str(root)):
        for member in real_members(graph.get(name, ()), exclusions):
            if member not in graph:
                leaves.add(member)
    return frozenset(leaves)


_GROUP_CODES = {
    "PROXY-FALLBACK": "PF",
    "TIER1-SELECT": "T1S",
    "TIER1-AUTO": "T1",
    "TIER2-AUTO": "T2",
    "TIER3-AUTO": "T3",
    "FINAL-EXIT": "FE",
}


def group_code(name: str, *, group_names: Collection[str]) -> str:
    """A short code for a chain element; per-physical Tier1 group names embed
    node names, so group names are never logged in clear."""

    text = str(name or "")
    code = _GROUP_CODES.get(text)
    if code is not None:
        return code
    if text.startswith("TIER1-") and text.endswith("-AUTO"):
        return "T1X"
    if text.startswith("TIER1-") and text.endswith("-SELECT"):
        return "T1XS"
    return "G" if text in group_names else "L"


def token_field(token: str | None) -> str:
    """``T`` + token for log fields outside gatelib's string fields (§4.11);
    ``none`` for a missing token."""

    return "none" if not token else "T" + str(token)


def rung_gate_key(group: str, accused: str) -> str:
    """The ``JudgeGates.note_rung``/``rung_blocked`` key of an escalated rung."""

    return "rung\x00" + str(group) + "\x00" + str(accused)


# ---------------------------------------------------------------------------
# Successors, witnesses, caps, expected pick (§3.3)


def rung_order(
    rung: Rung,
    alive: Mapping[str, bool],
    *,
    unknown: Collection[str] = (),
) -> tuple[str, ...]:
    """Mihomo's pick order without the accused: Core-alive members in ``all``
    order, then Core-dead ones, then members no read showed (never Core-dead)."""

    unknown_set = frozenset(str(name) for name in unknown)
    known_alive = {name: value for name, value in alive.items() if name not in unknown_set}
    order = tuple(name for name in rung.members if name not in unknown_set) + tuple(
        name for name in rung.members if name in unknown_set
    )
    return successor_list(order, rung.accused, known_alive)


def rung_successor_cap(rung: Rung, constants: PostDialConstants = DEFAULT_POST_DIAL_CONSTANTS) -> int:
    """Unwitnessed rungs (rung ①): the legacy leaf cap; a group rung probes up
    to three other members."""

    count = len(rung.members)
    if rung.kind is RungKind.LEAF:
        return successor_cap(count, constants)
    return min(int(constants.succ_cap_max), max(0, count - 1))


def witness_cap(rung: Rung) -> int:
    """Escalated rungs: every other member (bounded in practice by fit check F3)."""

    return max(0, len(rung.members) - 1)


@dataclass(frozen=True, slots=True)
class NativePick:
    pick: str
    all_dead: bool
    fixed_kept: bool


def expected_native_pick(
    members: Sequence[str],
    fixed: str,
    alive: Mapping[str, bool],
    *,
    exclusions: Collection[str] = EXCLUDED_MEMBERS,
) -> NativePick:
    """What a settled Mihomo fallback serves: an alive ``fixed``; else the
    first alive member; else member[0] (all dead)."""

    order = real_members(members, exclusions)
    pin = str(fixed or "")
    if pin and alive.get(pin):
        return NativePick(pin, False, True)
    for name in order:
        if alive.get(name):
            return NativePick(name, False, False)
    return NativePick(order[0] if order else "", True, False)


def alive_at(entries: Mapping[str, Any], url: str, names: Iterable[str]) -> dict[str, bool]:
    """``member_alive`` at ``url`` per name over ``/proxies`` entries."""

    return {str(name): member_alive(entries.get(str(name)), url) for name in names}


# ---------------------------------------------------------------------------
# Windows, tickets, budget, fit check (§3.4)


def _window(value: float) -> float:
    return round(float(value), _WINDOW_DIGITS)


def successor_delay_s(T_ms: int) -> float:
    """S_C0 start delay: min(2.0, T/2000) (0.4 s for 800 ms, 2.0 s for 5000 ms)."""

    return _window(min(2.0, float(T_ms) / 2000.0))


def evidence_s(T_ms: int, *, witnessed: bool) -> float:
    """Evidence window from A_C issue: T + 2.0 (witnessed), plus the S_C0 delay
    when a successor still has to start in the same 1.9 s queue."""

    base = float(T_ms) / 1000.0 + LADDER_MARGIN_S
    if not witnessed:
        base += successor_delay_s(T_ms)
    return _window(base)


def rung_ticket_s(T_ms: int, *, witnessed: bool) -> float:
    """Rung ticket = evidence window + native tail (3.7/9.5 unwitnessed, 3.3/7.5 witnessed)."""

    return _window(evidence_s(T_ms, witnessed=witnessed) + LADDER_NATIVE_TAIL_S)


def witness_s(T_ms: int) -> float:
    """Witness await bound from issue: T + 2.0."""

    return _window(float(T_ms) / 1000.0 + LADDER_MARGIN_S)


def probe_request_window_s(T_ms: int) -> float:
    """HTTP window after the probe starts: T + 0.65 (1.45 / 5.65)."""

    return _window(float(T_ms) / 1000.0 + LADDER_REQUEST_EXTRA_S)


def probe_await_s(T_ms: int) -> float:
    """Every ladder probe result is awaited at most issue + T + 2.0."""

    return witness_s(T_ms)


def rung_constants(
    rung: Rung,
    base: PostDialConstants = DEFAULT_POST_DIAL_CONSTANTS,
    *,
    witnessed: bool,
) -> PostDialConstants:
    """The rung's ``Judgement`` constants (others legacy, incl.
    ``min_probe_remaining_s``).  ``witnessed`` changes no constant: the ticket
    comes through ``ticket_deadline_perf`` and a witnessed rung presets S_C0."""

    del witnessed
    timeout = int(rung.timeout_ms)
    return constants_with(
        base,
        native_timeout_ms=timeout,
        probe_request_timeout_s=probe_request_window_s(timeout),
        s_c0_delay_s=successor_delay_s(timeout),
        native_tail_s=LADDER_NATIVE_TAIL_S,
    )


def escalation_need_s(next_rung: Rung) -> float:
    """Refresh allowance + witness + witnessed rung ticket (6.85 / 15.25)."""

    timeout = int(next_rung.timeout_ms)
    return _window(
        LADDER_REFRESH_ALLOWANCE_S + witness_s(timeout) + rung_ticket_s(timeout, witnessed=True)
    )


def ladder_budget_s(ladder_rungs: Sequence[Rung]) -> float:
    """min(20, rung ① unwitnessed ticket + every escalation's need)."""

    if not ladder_rungs:
        return 0.0
    total = rung_ticket_s(int(ladder_rungs[0].timeout_ms), witnessed=False)
    for rung in ladder_rungs[1:]:
        total += escalation_need_s(rung)
    return _window(min(LADDER_CAP_S, total))


def fits(now: float, deadline: float, need_s: float) -> bool:
    return float(now) + float(need_s) <= float(deadline) + _FIT_EPSILON


def witness_fit_need_s(T_ms: int) -> float:
    """F3: what a further witness at a rung of timeout T needs."""

    return _window(witness_s(T_ms) + rung_ticket_s(T_ms, witnessed=True))


def ac_after_witness_need_s(T_ms: int) -> float:
    """F4: what the A_C after a witness pass needs."""

    return rung_ticket_s(T_ms, witnessed=True)


# ---------------------------------------------------------------------------
# Verdicts and the ladder step (§3.5)


class RungVerdict(str, Enum):
    CONVICT_CONTRACT = "CONVICT_CONTRACT"
    NOT_REPRODUCED = "NOT_REPRODUCED"
    EXHAUSTED_COMPLETE = "EXHAUSTED_COMPLETE"
    EXHAUSTED_PARTIAL = "EXHAUSTED_PARTIAL"
    FACILITY = "FACILITY"
    INDETERMINATE = "INDETERMINATE"


@dataclass(frozen=True, slots=True)
class RungVerdictResult:
    verdict: RungVerdict
    reason: str
    successor: str | None
    successor_index: int | None
    base: Verdict | None


# Results that count a member as failed in this ladder at a rung's contract.
LADDER_FAILED = frozenset({ProbeClass.TRANSPORT_FAIL, ProbeClass.TIMEOUT, ProbeClass.HTTP_ANSWER})


def complete(
    rung: Rung,
    *,
    failed_in_ladder: Collection[str],
    read_alive: Mapping[str, bool] | None,
    read_unknown: Collection[str] = (),
) -> bool:
    """Every member other than the accused failed in this ladder, or the read
    showed it Core-dead (a member no read showed is never Core-dead)."""

    failed = frozenset(str(name) for name in failed_in_ladder)
    unknown = frozenset(str(name) for name in read_unknown)
    for name in rung.members:
        if name == rung.accused or name in failed:
            continue
        if read_alive is not None and name not in unknown and read_alive.get(name) is False:
            continue
        return False
    return True


def failed_successors(
    results: Mapping[str, ProbeClass | str],
    started: int,
    successors: Sequence[str] | None,
) -> frozenset[str]:
    """Members whose successor probe (``S_C<i>``) failed at the rung contract."""

    if not successors:
        return frozenset()
    failed: set[str] = set()
    for index in range(min(max(0, int(started)), len(successors))):
        if probe_class(results.get(successor_probe_name(index))) in LADDER_FAILED:
            failed.add(str(successors[index]))
    return frozenset(failed)


_BASE_TO_RUNG = {
    Verdict.CONVICT_CONTRACT: RungVerdict.CONVICT_CONTRACT,
    Verdict.NOT_REPRODUCED: RungVerdict.NOT_REPRODUCED,
    Verdict.FACILITY: RungVerdict.FACILITY,
    Verdict.INDETERMINATE: RungVerdict.INDETERMINATE,
}


def rung_verdict(
    results: Mapping[str, ProbeClass | str],
    started: int,
    cap: int,
    successors: Sequence[str] | None,
    *,
    rung: Rung,
    read_alive: Mapping[str, bool] | None,
    read_unknown: Collection[str] = (),
    failed_in_ladder: Collection[str] = (),
) -> RungVerdictResult:
    """The legacy ``verdict`` with EXHAUSTED split into COMPLETE / PARTIAL.

    Failed-in-ladder = ``failed_in_ladder`` plus this judgement's failed
    successor probes."""

    base = verdict(results, started, cap, successors)
    if base.verdict is Verdict.EXHAUSTED:
        failed = frozenset(failed_in_ladder) | failed_successors(results, started, successors)
        whole = complete(rung, failed_in_ladder=failed, read_alive=read_alive, read_unknown=read_unknown)
        value = RungVerdict.EXHAUSTED_COMPLETE if whole else RungVerdict.EXHAUSTED_PARTIAL
    else:
        value = _BASE_TO_RUNG[base.verdict]
    return RungVerdictResult(value, base.reason, base.successor, base.successor_index, base.verdict)


class EscalationCause(str, Enum):
    EXHAUSTED = "exhausted_complete"
    FACILITY = "facility"


def legacy_hold(value: RungVerdict | EscalationCause | str | None) -> Verdict | None:
    """The legacy hold a stop leaves: EXHAUSTED_* (or an EXHAUSTED cause) →
    EXHAUSTED, FACILITY → FACILITY, else None."""

    text = getattr(value, "value", value)
    if text in {
        RungVerdict.EXHAUSTED_COMPLETE.value,
        RungVerdict.EXHAUSTED_PARTIAL.value,
        EscalationCause.EXHAUSTED.value,
    }:
        return Verdict.EXHAUSTED
    if text in {RungVerdict.FACILITY.value, EscalationCause.FACILITY.value}:
        return Verdict.FACILITY
    return None


ACTION_COMMIT = "commit"
ACTION_ESCALATE = "escalate"
ACTION_STOP = "stop"
CLOSES_LEGACY = "legacy"
CLOSES_AFTER_COMMIT = "after_commit"
CLOSES_NONE = "none"

# Outcomes (§4.11).
OUTCOME_CONVICTED = "convicted"
OUTCOME_COMMIT_LATE = "commit_late"
OUTCOME_COMMIT_REJECTED = "commit_rejected"
OUTCOME_RECOVERED_BY_DEEPER_MOVE = "recovered_by_deeper_move"
OUTCOME_FACILITY_TRANSIENT = "facility_transient"
OUTCOME_NOT_REPRODUCED = "not_reproduced"
OUTCOME_NOT_REPRODUCED_UPPER = "not_reproduced_upper"
OUTCOME_MOVED_UNPROVEN = "moved_unproven"
OUTCOME_FACILITY_CONFIRMED = "facility_confirmed"
OUTCOME_ESCALATION_UNWITNESSED = "escalation_unwitnessed"
OUTCOME_WITNESS_INCOMPLETE = "witness_incomplete"
OUTCOME_EXHAUSTED_HOLD = "exhausted_hold"
OUTCOME_LADDER_BUDGET_EXHAUSTED = "ladder_budget_exhausted"
OUTCOME_LADDER_CAP = "ladder_cap"
OUTCOME_LADDER_ROUTE_MOVED = "ladder_route_moved"
OUTCOME_REFRESH_UNAVAILABLE = "refresh_unavailable"
OUTCOME_INDETERMINATE = "indeterminate"
OUTCOME_ABORTED = "aborted"


@dataclass(frozen=True, slots=True)
class LadderStep:
    action: str                 # "commit" | "escalate" | "stop"
    outcome: str | None
    hold: Verdict | None        # set at ladder stop only (JudgeGates.set_hold)
    closes: str                 # "legacy" | "after_commit" | "none"
    cause: EscalationCause | None
    bookkeeping: Verdict | None # what JudgeGates.finish(L0, ...) receives


def _stop(outcome: str, hold: Verdict | None, closes: str, bookkeeping: Verdict | None) -> LadderStep:
    return LadderStep(ACTION_STOP, outcome, hold, closes, None, bookkeeping)


def ladder_next(
    rv: RungVerdictResult,
    index: int,
    n_rungs: int,
    *,
    cause: EscalationCause | None,
    deeper_moved: bool,
    fits_escalation: bool,
    commit_allowed: bool,
    accused_kind: RungKind | str | None = None,
) -> LadderStep:
    """The §3.5 table.  ``cause`` led to rung ``index`` (None at rung ①).

    ``accused_kind`` (additive) picks the commit's closes: the unchanged
    legacy path for a rung ① leaf, after an accepted commit for a group
    accused; None infers it from the index (rung ① = leaf)."""

    value = rv.verdict
    higher = int(index) < int(n_rungs) - 1
    if value is RungVerdict.CONVICT_CONTRACT:
        if not commit_allowed:
            return _stop(OUTCOME_COMMIT_LATE, None, CLOSES_LEGACY, Verdict.CONVICT_CONTRACT)
        kind = getattr(accused_kind, "value", accused_kind)
        leaf = (kind == RungKind.LEAF.value) if kind is not None else int(index) == 0
        closes = CLOSES_LEGACY if leaf and int(index) == 0 else CLOSES_AFTER_COMMIT
        return LadderStep(ACTION_COMMIT, OUTCOME_CONVICTED, None, closes, None, Verdict.CONVICT_CONTRACT)
    if value is RungVerdict.NOT_REPRODUCED:
        if int(index) == 0:
            return _stop(OUTCOME_NOT_REPRODUCED, None, CLOSES_NONE, Verdict.NOT_REPRODUCED)
        if cause is EscalationCause.FACILITY:
            return _stop(OUTCOME_FACILITY_TRANSIENT, None, CLOSES_NONE, Verdict.INDETERMINATE)
        if cause is EscalationCause.EXHAUSTED and deeper_moved:
            return _stop(
                OUTCOME_RECOVERED_BY_DEEPER_MOVE, None, CLOSES_AFTER_COMMIT, Verdict.CONVICT_CONTRACT
            )
        return _stop(OUTCOME_NOT_REPRODUCED_UPPER, None, CLOSES_NONE, Verdict.INDETERMINATE)
    if value is RungVerdict.EXHAUSTED_COMPLETE:
        if higher and fits_escalation:
            return LadderStep(ACTION_ESCALATE, None, None, CLOSES_NONE, EscalationCause.EXHAUSTED, None)
        outcome = OUTCOME_LADDER_BUDGET_EXHAUSTED if higher else OUTCOME_EXHAUSTED_HOLD
        return _stop(outcome, Verdict.EXHAUSTED, CLOSES_LEGACY, Verdict.EXHAUSTED)
    if value is RungVerdict.EXHAUSTED_PARTIAL:
        return _stop(OUTCOME_MOVED_UNPROVEN, Verdict.EXHAUSTED, CLOSES_LEGACY, Verdict.EXHAUSTED)
    if value is RungVerdict.FACILITY:
        if higher and fits_escalation:
            return LadderStep(ACTION_ESCALATE, None, None, CLOSES_NONE, EscalationCause.FACILITY, None)
        outcome = OUTCOME_LADDER_BUDGET_EXHAUSTED if higher else OUTCOME_FACILITY_CONFIRMED
        return _stop(outcome, Verdict.FACILITY, CLOSES_LEGACY, Verdict.FACILITY)
    return _stop(OUTCOME_INDETERMINATE, None, CLOSES_LEGACY, Verdict.INDETERMINATE)


# ---------------------------------------------------------------------------
# Witness-first at every escalated rung (§3.6)

WITNESS_PROBE = "probe"
WITNESS_ISSUE_AC = "issue_ac"
WITNESS_STOP = "stop"
WITNESS_ESCALATE = "escalate"


@dataclass(frozen=True, slots=True)
class WitnessStep:
    action: str           # "probe" | "issue_ac" | "stop" | "escalate"
    target: str | None    # next witness (probe)
    outcome: str | None   # stop: facility_confirmed | escalation_unwitnessed | ladder_budget_exhausted |
                          #       exhausted_hold | witness_incomplete | indeterminate
    hold: Verdict | None
    passed: str | None    # proven witness (issue_ac)
    wasted: bool          # issue_ac refused by F4


def _witness_stop(outcome: str, hold: Verdict | None, *, wasted: bool = False) -> WitnessStep:
    return WitnessStep(WITNESS_STOP, None, outcome, hold, None, wasted)


def witness_name(index: int) -> str:
    return f"W{int(index)}"


def witness_next(
    cause: EscalationCause,
    order: Sequence[str],
    results: Mapping[str, ProbeClass | str],
    *,
    cap: int,
    fits_next: bool,
    fits_ac: bool,
    rung_is_last: bool,
    fits_escalation: bool,
    complete_if_exhausted: bool,
) -> WitnessStep:
    """The next witness step at an escalated rung.

    ``order`` is ``rung_order`` of that rung; ``results`` maps a witness
    target (member name) to its class.  Witnesses are probed one at a time in
    ``order``; ``fits_next`` is F3 for the next witness after the first,
    ``fits_ac`` is F4, ``fits_escalation`` is F2 for the rung above."""

    hold = legacy_hold(cause)
    limit = min(max(0, int(cap)), len(order))
    tested = 0
    for name in order[:limit]:
        if name not in results:
            break
        value = probe_class(results[name])
        tested += 1
        if value is ProbeClass.PASS:
            if not fits_ac:
                return _witness_stop(OUTCOME_LADDER_BUDGET_EXHAUSTED, hold, wasted=True)
            return WitnessStep(WITNESS_ISSUE_AC, None, None, None, str(name), False)
        if value is None or value is ProbeClass.INDETERMINATE:
            return _witness_stop(OUTCOME_INDETERMINATE, None)
        if value is ProbeClass.TIMEOUT and cause is EscalationCause.FACILITY:
            return _witness_stop(OUTCOME_FACILITY_CONFIRMED, Verdict.FACILITY)
    if tested < limit:
        if tested == 0 or fits_next:
            return WitnessStep(WITNESS_PROBE, str(order[tested]), None, None, None, False)
        return _witness_stop(OUTCOME_LADDER_BUDGET_EXHAUSTED, hold)
    if cause is EscalationCause.FACILITY:
        return _witness_stop(OUTCOME_ESCALATION_UNWITNESSED, Verdict.FACILITY)
    if not complete_if_exhausted:
        return _witness_stop(OUTCOME_WITNESS_INCOMPLETE, Verdict.EXHAUSTED)
    if rung_is_last:
        return _witness_stop(OUTCOME_EXHAUSTED_HOLD, Verdict.EXHAUSTED)
    if fits_escalation:
        return WitnessStep(WITNESS_ESCALATE, None, None, None, None, False)
    return _witness_stop(OUTCOME_LADDER_BUDGET_EXHAUSTED, Verdict.EXHAUSTED)


# ---------------------------------------------------------------------------
# Demotion for closes, authority (§3.7)


def rung_demoted(
    read_now: str,
    read_alive: Mapping[str, bool],
    read_unknown: Collection[str],
    rung: Rung,
    pick: str,
) -> bool:
    """The committed rung's fresh read shows the move: ``now`` is the pick,
    not the accused, and the accused read Core-dead (never when unknown)."""

    accused = rung.accused
    return (
        bool(pick)
        and read_now == pick
        and pick != accused
        and accused not in frozenset(read_unknown)
        and read_alive.get(accused) is False
    )


@dataclass(frozen=True, slots=True)
class LadderAuthority:
    pid: int
    process_generation: int
    core_generation: int
    semantic_digest: str
    network_context_epoch: int
    selector_intent_revision: int
    facility_epoch: int
    lifecycle_revision: int
    worker_token: tuple
    group_graph: tuple
    policy_key: tuple[bool, bool, bool]
    lease_obj_id: int | None
    lease_release_state: str
    lease_digest: str


_LEASE_FIELDS = frozenset({"lease_obj_id", "lease_release_state", "lease_digest"})


def authority_diff(baseline: LadderAuthority, current: LadderAuthority) -> str | None:
    """The first differing field in declaration order, as a stop reason."""

    for item in fields(LadderAuthority):
        name = item.name
        if getattr(baseline, name) != getattr(current, name):
            if name in _LEASE_FIELDS:
                return "authority_changed:lease"
            if name == "policy_key":
                return "authority_changed:policy"
            return f"authority_changed:{name}"
    return None


def lease_digest(clocks: Iterable[Any]) -> str:
    """sha256 (first 16 hex) of the sorted ``(group, member, round(started_mono,
    3), ended cause or "", purge_pending, verify_retry)`` tuples over the lease
    clocks (``LeaseClock`` duck type; its end cause is the ``ended`` field).
    Counters, skip sets and deferral spans are excluded: they change on every
    tick."""

    rows = []
    for clock in clocks:
        started = getattr(clock, "started_mono", 0.0)
        try:
            started_value = round(float(started), 3)
        except (TypeError, ValueError):
            started_value = 0.0
        ended = getattr(clock, "ended_cause", None)
        if ended is None:
            ended = getattr(clock, "ended", "")
        rows.append(
            (
                str(getattr(clock, "group", "") or ""),
                str(getattr(clock, "member", "") or ""),
                started_value,
                str(ended or ""),
                bool(getattr(clock, "purge_pending", False)),
                bool(getattr(clock, "verify_retry", False)),
            )
        )
    rows.sort()
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()[:16]


def ladder_id(incident_id: str) -> str:
    """``D`` + the first 11 hex of sha256(incident_id)."""

    return "D" + hashlib.sha256(str(incident_id).encode("utf-8")).hexdigest()[:11]
