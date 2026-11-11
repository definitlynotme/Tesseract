"""Pure node-judgement policy, gates, scheduler and verdict (design v3 §3.3, §4.1, §5, §6, §18.6-18.7).

The integration layer executes every probe and controller read and feeds the
classified results back; this module only decides.  P2 scope: contract probes
on the TIER3-AUTO contract, native moves only, no destination probes, no
conviction ledger, no pins or holds on routes.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Sequence

from config_utils import coerce_bool
from runtime_post_dial import (
    DEFAULT_POST_DIAL_CONSTANTS,
    RULE_AGED,
    RULE_AGED_SINGLE,
    RULE_FAST,
    RULE_FAST_SPREAD,
    RULE_SLOW,
    RULE_WAITING,
    PostDialConstants,
    PostDialTokenizer,
    StuckConnections,
    parse_rfc3339,
)


# ---------------------------------------------------------------------------
# Policy (P0-6; §12 and §14)

POST_DIAL_MODES = ("off", "shadow", "act")
ACT_RULE_NAMES = {
    "fast": RULE_FAST,
    "aged": RULE_AGED,
    "fast_spread": RULE_FAST_SPREAD,
    "slow": RULE_SLOW,
    "waiting": RULE_WAITING,
    "aged_single": RULE_AGED_SINGLE,
}
DEFAULT_ACT_RULE_KEYS = ("fast", "aged")
# Rollback spellings for ``act_rules`` (§14): each one means "no act rule".
_ACT_RULES_OFF = frozenset({"off", "none", "false", "no", "0"})
DEFAULT_STREAM_WATCH_SECONDS = 60.0
MAX_STREAM_WATCH_SECONDS = 600.0
# The post-dial observer thread's CPU budget, percent of one core (0: off).
DEFAULT_OBSERVER_CPU_BUDGET_PCT = 2.0
MIN_OBSERVER_CPU_BUDGET_PCT = 0.5
MAX_OBSERVER_CPU_BUDGET_PCT = 20.0
# Act-trigger guard: the smallest failed share among L's recent evidence
# connections for FAST/AGED to act (0: off, the rules alone).  Off by default
# (user decision 10-01): it held zombies that die mid-browsing; the observer
# still logs its counts and the 0.5 / onset variants for the next gate run.
DEFAULT_TRIGGER_MIN_FAILED_SHARE = 0.0


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _post_dial_mode(value: Any, default: str = "act") -> str:
    # YAML 1.1 reads a bare ``off`` as False and ``on`` as True.
    if value is False:
        return "off"
    if value is True:
        return "shadow"
    text = str(value if value is not None else "").strip().lower()
    return text if text in POST_DIAL_MODES else default


def _act_rules(value: Any) -> tuple[str, ...]:
    if value is None:
        return DEFAULT_ACT_RULE_KEYS
    if value is False:
        # YAML 1.1 reads a bare ``off``/``no``/``false`` as False: a rollback.
        return ()
    if isinstance(value, str):
        items: list[Any] = [part for part in value.split(",") if part.strip()]
        if not items:
            return ()
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        if not items:
            return ()
    else:
        return DEFAULT_ACT_RULE_KEYS
    if all(item is False or str(item).strip().lower() in _ACT_RULES_OFF for item in items):
        return ()
    keys: list[str] = []
    for item in items:
        key = str(item).strip().lower().replace("-", "_")
        if key in ACT_RULE_NAMES and key not in keys:
            keys.append(key)
    return tuple(keys) if keys else DEFAULT_ACT_RULE_KEYS


def _seconds(value: Any, default: float, low: float, high: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(low, min(high, number))


def _cpu_budget_pct(value: Any) -> float:
    """``observer_cpu_budget_pct``: 0, a negative value or ``off`` (also
    ``none``/``false``/``no``, as for ``act_rules``) turns the throttle off;
    any other number, with or without a trailing ``%``, is clamped to
    0.5-20.  A missing or empty (YAML null) value is the default, as for
    every key of this block, and so is anything else."""

    if value is False or (isinstance(value, str) and value.strip().lower() in _ACT_RULES_OFF):
        return 0.0
    if isinstance(value, str) and value.strip().endswith("%"):
        value = value.strip()[:-1]
    number = _seconds(
        value,
        DEFAULT_OBSERVER_CPU_BUDGET_PCT,
        float("-inf"),
        MAX_OBSERVER_CPU_BUDGET_PCT,
    )
    if number <= 0.0:
        return 0.0
    return max(MIN_OBSERVER_CPU_BUDGET_PCT, number)


def _min_failed_share(value: Any) -> float:
    """``trigger_min_failed_share``: a share clamped to 0-1, or a percentage
    with a trailing ``%`` (``"50%"`` is 0.5); a bare number is a share, so
    ``50`` clamps to 1.0 (every recent connection must fail).  0, a negative
    value or ``off`` (also ``none``/``false``/``no``, as for ``act_rules``)
    turns the guard off.  A missing or empty (YAML null) value is the
    default (0: off), and so is anything else."""

    if value is False or (isinstance(value, str) and value.strip().lower() in _ACT_RULES_OFF):
        return 0.0
    if isinstance(value, str) and value.strip().endswith("%"):
        percent = _seconds(value.strip()[:-1], float("nan"), float("-inf"), float("inf"))
        if percent != percent:  # "%" alone or junk before it: the default
            return DEFAULT_TRIGGER_MIN_FAILED_SHARE
        return max(0.0, min(1.0, percent / 100.0))
    return _seconds(value, DEFAULT_TRIGGER_MIN_FAILED_SHARE, 0.0, 1.0)


@dataclass(frozen=True, slots=True)
class NodeJudgementPolicy:
    enabled: bool = True
    evidence_log: bool = True
    post_dial_judge: str = "act"
    act_rules: tuple[str, ...] = DEFAULT_ACT_RULE_KEYS
    close_stuck_connections: bool = True
    stream_watch_seconds: float = DEFAULT_STREAM_WATCH_SECONDS
    group_round_native_timeout: bool = False
    release_stale_fallback_pins: bool = False
    manual_check_uses_route_contract: bool = True
    # CPU budget of the post-dial observer thread (percent of one core, 0 =
    # no throttle); above it the observer stretches its poll periods.
    observer_cpu_budget_pct: float = DEFAULT_OBSERVER_CPU_BUDGET_PCT
    # FAST/AGED act only while failed / (failed + answered) among L's
    # user-origin TIER3 connections of the last 10 s is at least this
    # (failed = FAILED_FAST + AGED).  0, the default, turns the guard off.
    trigger_min_failed_share: float = DEFAULT_TRIGGER_MIN_FAILED_SHARE
    # Act mode at a CONVICT_CONTRACT: true also records the durable judgement
    # of L (``_apply_confirmed_runtime_health``: SUSPICIOUS overlay, targeted
    # recheck, flap history, Tier3 revocation).  False, the default
    # ("move-only"), only rebinds the published route and the judge's ticket
    # to Mihomo's native successor and settles it RECOVERED.
    durable_conviction: bool = False
    # Act mode only (shadow never queues).  True: when an act admission is
    # gated ``not_armed`` only because the recovery arm is from an older
    # route epoch (a native leaf move) and every other gate passes, queue the
    # existing bounded route refresh (3 per leaf + route view, >= 1 s apart),
    # whose worker re-arms through the unchanged ``_try_arm_runtime_recovery``.
    # True is the default since 10-28 (shipped after ship1); false restores
    # the pre-ship behaviour.
    rearm_on_stale_route: bool = True
    # Tier ladder (spec v1 §2.1): judge the active PROXY-FALLBACK leaf at any
    # tier and escalate through the native fallback rungs (act mode only).
    # True is the default since 10-28 (shipped after ship1); false keeps
    # every path as it was.
    tier_ladder_judge: bool = True
    # Needs ``tier_ladder_judge``: one no-write PF-contract probe of the PF
    # member Mihomo itself just left while its own subtree reads alive.
    overshoot_repair: bool = True
    # 10-28 ship2 fixes, each its own key (judge act only; the act / ladder
    # keys never enable them by themselves). True is the default since 11-01
    # (user decision after ship3); false restores the 10-28 behaviour:
    # fix 1 - re-export the Tier3 pool when TIER3-AUTO is exhausted/frozen.
    exhausted_pool_reexport: bool = True
    # fix 2 - EXHAUSTED hold at admission when the published TIER3-AUTO is
    # exhausted, and at ladder rung 1 when A_C failed and S_C0 timed out.
    exhausted_hold: bool = True
    # fix 3 - a clearly failing FAST trigger passes a NOT_REPRODUCED
    # back-off once per leaf per window.
    not_reproduced_override: bool = True
    # 10-28 seamless keys, each its own key (judge act only). True is the
    # default since 11-01 except post_dial_fast_successor (stays False):
    # move the route provisionally to an already proven successor when A_C
    # has no result after PROVISIONAL_MOVE_S (tier ladder rung 1 only).
    post_dial_provisional_move: bool = True
    # S_C0_DELAY_S = FAST_SUCCESSOR_S_C0_DELAY_S (1.0 s) instead of 2.0 s.
    post_dial_fast_successor: bool = False
    # A successor whose provider history is alive and <= 60 s old stands in
    # for S_C0 PASS for the provisional move (read-only provider GET).
    post_dial_successor_from_history: bool = True
    # Demotion-watch rule 6: also close frozen streams on the demoted leaf.
    post_dial_close_frozen_streams: bool = True

    @classmethod
    def disabled(cls) -> "NodeJudgementPolicy":
        """``enabled: false`` restores today's paths: every flag is off."""

        return cls(
            enabled=False,
            evidence_log=False,
            post_dial_judge="off",
            act_rules=(),
            close_stuck_connections=False,
            stream_watch_seconds=0.0,
            group_round_native_timeout=False,
            release_stale_fallback_pins=False,
            manual_check_uses_route_contract=False,
            durable_conviction=False,
            rearm_on_stale_route=False,
            tier_ladder_judge=False,
            overshoot_repair=False,
            exhausted_pool_reexport=False,
            exhausted_hold=False,
            not_reproduced_override=False,
            post_dial_provisional_move=False,
            post_dial_fast_successor=False,
            post_dial_successor_from_history=False,
            post_dial_close_frozen_streams=False,
        )

    @classmethod
    def from_config(cls, config: Any) -> "NodeJudgementPolicy":
        root = _mapping(config)
        client = _mapping(root.get("client"))
        recovery = _mapping(client.get("egress_recovery"))
        block = _mapping(recovery.get("node_judgement"))
        if not coerce_bool(block.get("enabled", True), True):
            return cls.disabled()
        tier_ladder_judge = coerce_bool(block.get("tier_ladder_judge", True), True)
        return cls(
            enabled=True,
            evidence_log=coerce_bool(block.get("evidence_log", True), True),
            post_dial_judge=_post_dial_mode(block.get("post_dial_judge", "act")),
            act_rules=_act_rules(block.get("act_rules")),
            close_stuck_connections=coerce_bool(block.get("close_stuck_connections", True), True),
            stream_watch_seconds=_seconds(
                block.get("stream_watch_seconds", DEFAULT_STREAM_WATCH_SECONDS),
                DEFAULT_STREAM_WATCH_SECONDS,
                0.0,
                MAX_STREAM_WATCH_SECONDS,
            ),
            group_round_native_timeout=coerce_bool(
                block.get("group_round_native_timeout", False),
                False,
            ),
            release_stale_fallback_pins=coerce_bool(
                block.get("release_stale_fallback_pins", False),
                False,
            ),
            manual_check_uses_route_contract=coerce_bool(
                block.get("manual_check_uses_route_contract", True),
                True,
            ),
            observer_cpu_budget_pct=_cpu_budget_pct(
                block.get("observer_cpu_budget_pct", DEFAULT_OBSERVER_CPU_BUDGET_PCT)
            ),
            trigger_min_failed_share=_min_failed_share(
                block.get("trigger_min_failed_share", DEFAULT_TRIGGER_MIN_FAILED_SHARE)
            ),
            durable_conviction=coerce_bool(block.get("durable_conviction", False), False),
            rearm_on_stale_route=coerce_bool(block.get("rearm_on_stale_route", True), True),
            tier_ladder_judge=tier_ladder_judge,
            overshoot_repair=coerce_bool(block.get("overshoot_repair", True), True)
            and tier_ladder_judge,
            exhausted_pool_reexport=coerce_bool(block.get("exhausted_pool_reexport", True), True),
            exhausted_hold=coerce_bool(block.get("exhausted_hold", True), True),
            not_reproduced_override=coerce_bool(block.get("not_reproduced_override", True), True),
            post_dial_provisional_move=coerce_bool(block.get("post_dial_provisional_move", True), True),
            post_dial_fast_successor=coerce_bool(block.get("post_dial_fast_successor", False), False),
            post_dial_successor_from_history=coerce_bool(
                block.get("post_dial_successor_from_history", True), True
            ),
            post_dial_close_frozen_streams=coerce_bool(
                block.get("post_dial_close_frozen_streams", True), True
            ),
        )

    @property
    def observer_enabled(self) -> bool:
        return self.enabled and self.post_dial_judge in {"shadow", "act"}

    @property
    def acts(self) -> bool:
        return self.enabled and self.post_dial_judge == "act"

    @property
    def ladder_on(self) -> bool:
        """The tier ladder is configured (it acts only when ``acts``)."""

        return self.enabled and self.tier_ladder_judge

    @property
    def overshoot_on(self) -> bool:
        return self.enabled and self.tier_ladder_judge and self.overshoot_repair

    @property
    def ladder_policy_key(self) -> tuple[bool, bool, bool]:
        """``(ladder_on, acts, overshoot_on)``: captured at a ladder admission
        and re-checked before every ladder unit."""

        return (self.ladder_on, self.acts, self.overshoot_on)

    @property
    def act_rule_ids(self) -> frozenset[str]:
        """Rule ids as the observer names them (``FAST``, ``AGED`` ...)."""

        return frozenset(ACT_RULE_NAMES[key] for key in self.act_rules)

    def constants(self, base: PostDialConstants = DEFAULT_POST_DIAL_CONSTANTS) -> PostDialConstants:
        return replace(
            base,
            stream_watch_s=float(self.stream_watch_seconds),
            cpu_budget_pct=float(self.observer_cpu_budget_pct),
            trigger_min_failed_share=float(self.trigger_min_failed_share),
        )


def node_judgement_policy(config: Any) -> NodeJudgementPolicy:
    return NodeJudgementPolicy.from_config(config)


CONFIG_WARNING_OVERSHOOT_REQUIRES_LADDER = "overshoot_repair_requires_tier_ladder_judge"


def node_judgement_config_warnings(config: Any) -> tuple[str, ...]:
    """Dependent-key warnings (never raised: the config stays valid).

    ``overshoot_repair`` set while ``tier_ladder_judge`` is not true (and the
    block is not ``enabled: false``) reads as off; this names that once."""

    root = _mapping(config)
    client = _mapping(root.get("client"))
    recovery = _mapping(client.get("egress_recovery"))
    block = _mapping(recovery.get("node_judgement"))
    if (
        coerce_bool(block.get("overshoot_repair", True), True)
        and coerce_bool(block.get("enabled", True), True)
        and not coerce_bool(block.get("tier_ladder_judge", True), True)
    ):
        return (CONFIG_WARNING_OVERSHOOT_REQUIRES_LADDER,)
    return ()


# ---------------------------------------------------------------------------
# Probe classes and verdict (§5.2, §5.3)


class ProbeClass(str, Enum):
    PASS = "pass"
    HTTP_ANSWER = "http_answer"
    TRANSPORT_FAIL = "transport_fail"
    TIMEOUT = "timeout"
    INDETERMINATE = "indeterminate"


FAILED = frozenset({ProbeClass.TRANSPORT_FAIL, ProbeClass.TIMEOUT})
ANSWERED = frozenset({ProbeClass.PASS, ProbeClass.HTTP_ANSWER})
ACCUSED = "A_C"


def successor_probe_name(index: int) -> str:
    return f"S_C{int(index)}"


def probe_class(value: ProbeClass | str | None) -> ProbeClass | None:
    if value is None or isinstance(value, ProbeClass):
        return value
    try:
        return ProbeClass(str(value))
    except ValueError:
        return ProbeClass.INDETERMINATE


class Verdict(str, Enum):
    NOT_REPRODUCED = "NOT_REPRODUCED"
    CONVICT_CONTRACT = "CONVICT_CONTRACT"
    FACILITY = "FACILITY"
    EXHAUSTED = "EXHAUSTED"
    INDETERMINATE = "INDETERMINATE"


HOLD_VERDICTS = frozenset({Verdict.FACILITY, Verdict.EXHAUSTED})


@dataclass(frozen=True, slots=True)
class VerdictResult:
    verdict: Verdict
    reason: str
    successor: str | None = field(default=None, repr=False)
    successor_index: int | None = None


def verdict(
    results: Mapping[str, ProbeClass | str],
    started: int,
    cap: int,
    successors: Sequence[str] | None,
) -> VerdictResult:
    """§5.3 over the results so far (``started`` = successor probes started, ``nxt``).

    ``successors`` None means the successor list is unknown (route read
    failed); it can then never give EXHAUSTED.
    """

    accused = probe_class(results.get(ACCUSED))
    if accused in ANSWERED:
        return VerdictResult(Verdict.NOT_REPRODUCED, f"accused_{accused.value}")
    if accused not in FAILED:
        return VerdictResult(
            Verdict.INDETERMINATE,
            "accused_unresolved" if accused is None else f"accused_{accused.value}",
        )
    names = [successor_probe_name(index) for index in range(max(0, int(started)))]
    classes = [probe_class(results.get(name)) for name in names]
    for index, value in enumerate(classes):
        if value is ProbeClass.PASS:
            successor = successors[index] if successors is not None and index < len(successors) else None
            return VerdictResult(Verdict.CONVICT_CONTRACT, "successor_passed", successor, index)
    resolved = [value for value in classes if value is not None]
    if accused is ProbeClass.TIMEOUT and resolved and resolved[-1] is ProbeClass.TIMEOUT:
        return VerdictResult(Verdict.FACILITY, "accused_and_successor_timeout")
    if successors is not None and not successors:
        return VerdictResult(Verdict.EXHAUSTED, "no_other_member")
    if (
        successors is not None
        and started > 0
        and len(resolved) == len(classes)
        and all(value is ProbeClass.TRANSPORT_FAIL for value in classes)
        and started >= min(int(cap), len(successors))
    ):
        return VerdictResult(Verdict.EXHAUSTED, "successors_transport_failed")
    if not names:
        return VerdictResult(Verdict.INDETERMINATE, "no_successor_result")
    if any(value is None for value in classes):
        return VerdictResult(Verdict.INDETERMINATE, "successor_unresolved")
    return VerdictResult(Verdict.INDETERMINATE, "successor_" + (resolved[-1].value if resolved else "none"))


def successor_timeout_exhausts(
    current: Verdict | None,
    results: Mapping[str, ProbeClass | str] | Iterable[tuple[str, str]],
) -> bool:
    """Fix 2 (10-28; key exhausted_hold, the tier ladder): an INDETERMINATE judgement whose
    accused failed and whose first successor S_C0 timed out is treated as an
    exhausted group (EXHAUSTED hold), not left to re-trigger after the 10 s
    back-off.  Any other verdict is unchanged."""

    if current is not Verdict.INDETERMINATE:
        return False
    table = dict(results.items() if isinstance(results, Mapping) else results)
    if probe_class(table.get(ACCUSED)) not in FAILED:
        return False
    return probe_class(table.get(successor_probe_name(0))) is ProbeClass.TIMEOUT


# ---------------------------------------------------------------------------
# Route helpers (§18.6 alive rule, successor list, cap; §5.4 demotion check)


def member_alive(proxy: Mapping[str, Any] | None, test_url: str) -> bool:
    """``extra[TEST_URL].alive`` if present, else the global ``alive`` (§18.6)."""

    if not isinstance(proxy, Mapping):
        return False
    extra = proxy.get("extra")
    if isinstance(extra, Mapping):
        entry = extra.get(test_url)
        if isinstance(entry, Mapping):
            return entry.get("alive") is True
    return proxy.get("alive") is True


def provider_alive_map(provider: Mapping[str, Any] | None, test_url: str) -> dict[str, bool]:
    """Alive per member from ``GET /providers/proxies/TIER3B-RUNTIME`` (S3)."""

    alive: dict[str, bool] = {}
    if not isinstance(provider, Mapping):
        return alive
    proxies = provider.get("proxies")
    if not isinstance(proxies, list):
        return alive
    for proxy in proxies:
        if isinstance(proxy, Mapping) and isinstance(proxy.get("name"), str):
            alive[proxy["name"]] = member_alive(proxy, test_url)
    return alive


def contract_order(
    members: Sequence[str],
    *,
    reject_names: Iterable[str] = ("REJECT",),
) -> tuple[str, ...]:
    rejected = frozenset(reject_names)
    return tuple(str(name) for name in members if str(name) not in rejected)


def successor_cap(member_count: int, constants: PostDialConstants = DEFAULT_POST_DIAL_CONSTANTS) -> int:
    """min(3, max(1, (n - 1) // 2)) over the group's non-Reject members."""

    return min(int(constants.succ_cap_max), max(1, (int(member_count) - 1) // 2))


def successor_list(
    order: Sequence[str],
    leaf: str,
    alive: Mapping[str, bool],
) -> tuple[str, ...]:
    """Core-alive members in ``all`` order, then Core-dead members, excluding L."""

    others = [name for name in order if name != leaf]
    return tuple(
        [name for name in others if alive.get(name, False)]
        + [name for name in others if not alive.get(name, False)]
    )


def demotion_confirmed(
    leaf: str,
    alive: Mapping[str, bool],
    group_now: str | None,
    *,
    members: Sequence[str] | None = None,
) -> bool:
    """Mihomo has demoted L: L is Core-dead and the group's ``now`` is not L.

    ``members`` is the fresh group read's ``all`` (G2).  When given, a leaf
    outside it is never confirmed: a non-member cannot be demoted, and a leaf
    missing from the provider read would otherwise read as Core-dead.
    """

    if members is not None and leaf not in contract_order(members):
        return False
    return not alive.get(leaf, False) and group_now != leaf


# The float round trip of an RFC 3339 time (7-digit fraction) back to epoch
# seconds is exact to well under this.
_READBACK_ROUNDING_S = 1e-6


def readback_shows_accused_failure(
    proxy: Mapping[str, Any] | None,
    test_url: str,
    issued_at_wall: float,
) -> bool:
    """Deadline readback (§5.3): an ``extra[testUrl]`` history entry not older
    than the A_C issue time, with ``alive`` false, counts as a timeout.

    "Not older", not "newer": on Windows, Python 3.12's ``time.time()``
    (``GetSystemTimeAsFileTime``) and Go's ``time.Now()`` both read the system
    time, which advances in timer ticks (0.5-15.6 ms).  A sample Mihomo
    writes after the A_C request, inside the tick the issue time was read
    in, carries that same time.
    """

    if not isinstance(proxy, Mapping):
        return False
    extra = proxy.get("extra")
    entry = extra.get(test_url) if isinstance(extra, Mapping) else None
    if not isinstance(entry, Mapping) or entry.get("alive") is not False:
        return False
    history = entry.get("history")
    if not isinstance(history, list):
        return False
    floor = float(issued_at_wall) - _READBACK_ROUNDING_S
    for sample in history:
        if isinstance(sample, Mapping):
            at = parse_rfc3339(sample.get("time"))
            if at is not None and at >= floor:
                return True
    return False


# ---------------------------------------------------------------------------
# Judgement scheduler (§18.6)


@dataclass(frozen=True, slots=True)
class StartProbe:
    name: str
    leaf: str = field(repr=False)
    timeout_ms: int
    request_timeout_s: float
    # Ladder probes only (spec v1 §3.8); None on every legacy probe, so the
    # legacy probe, its repr and its equality are unchanged.
    url: str | None = field(default=None, repr=False)
    expected: str | None = field(default=None, repr=False)
    group: str | None = field(default=None, repr=False)
    admission_s: float | None = field(default=None, repr=False)
    await_s: float | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class NeedReadback:
    """One S3 read for the deadline readback of an unresolved A_C."""

    leaf: str = field(repr=False)
    accused_issued_wall: float


@dataclass(frozen=True, slots=True)
class AccusedFailed:
    """A_C failed (transport, timeout or status): run ``on_accused_failed`` (§18.7).

    Emitted once per judgement: for the A_C reply, or for the deadline
    readback that resolved A_C as a timeout (then before the Done).  ``late``
    means the verdict was already out.  Never emitted once the judgement ended
    as ``non_member``.
    """

    leaf: str = field(repr=False)
    probe: ProbeClass
    late: bool


@dataclass(frozen=True, slots=True)
class LateHold:
    """Late results made the incident FACILITY/EXHAUSTED after the verdict (H1 fix 4)."""

    verdict: Verdict


@dataclass(frozen=True, slots=True)
class Done:
    verdict: Verdict | None
    reason: str
    leaf: str = field(repr=False)
    successor: str | None = field(repr=False)
    successor_index: int | None
    results: tuple[tuple[str, str], ...]
    commit_allowed: bool
    tokens: tuple[str, ...]
    kind: str


@dataclass(frozen=True, slots=True)
class ProvisionalMove:
    """Key post_dial_provisional_move (10-28): A_C has no result
    PROVISIONAL_MOVE_S after it was issued; move the route provisionally to
    ``successor``.  ``basis`` is ``s_c0`` (S_C0 passed) or ``history`` (the
    service must still find a fresh alive provider history, key
    post_dial_successor_from_history).  Never a verdict: it writes no dead and
    closes nothing; the A_C result still decides."""

    successor: str = field(repr=False)
    basis: str


JudgementAction = StartProbe | NeedReadback | AccusedFailed | LateHold | Done | ProvisionalMove


class Judgement:
    """Event-driven §18.6 loop for one incident on leaf L (perf clock)."""

    def __init__(
        self,
        leaf: str,
        *,
        kind: str,
        tokens: Iterable[str],
        ticket_deadline_perf: float,
        constants: PostDialConstants = DEFAULT_POST_DIAL_CONSTANTS,
        successor_cap: int | None = None,
        preset_successor: str | None = None,
        contract: tuple[str, str, str] | None = None,
        admission_s: float | None = None,
        await_s: float | None = None,
        provisional_move_s: float | None = None,
        history_candidate: bool = False,
    ) -> None:
        """``provisional_move_s`` (None: off, today's judgement) and
        ``history_candidate`` are the 10-28 seamless keys (``ProvisionalMove``).
        The keyword-only ladder arguments (spec v1 §3.8) default to None,
        which is today's judgement.  ``successor_cap`` overrides the leaf cap
        in ``on_route_state``; ``preset_successor`` is a passed witness that
        stands as ``S_C0 = PASS`` (no route read, no S_C0 timer);
        ``contract`` = ``(group, url, expected)`` and ``admission_s`` /
        ``await_s`` are copied into every ``StartProbe``."""

        self._c = constants
        self.leaf = leaf
        self.kind = str(kind)
        self.tokens = tuple(tokens)
        self.ticket_deadline = float(ticket_deadline_perf)
        self.t_evid = self.ticket_deadline - constants.native_tail_s
        self.t_ac: float | None = None
        self.t_ac_wall: float | None = None
        self._results: dict[str, ProbeClass] = {}
        self._started: dict[str, float] = {}
        self._resolved_at: dict[str, float] = {}
        self._successors: tuple[str, ...] | None = None
        self._route_known = False
        self._cap = 0
        self._next = 0
        self._successor_wanted = False
        self._timer_fired = False
        self._readback_requested = False
        self._accused_reported = False
        self._late_hold_reported = False
        self._done: Done | None = None
        self.skipped: list[str] = []
        self._cap_override = None if successor_cap is None else int(successor_cap)
        self._preset = None if preset_successor is None else str(preset_successor)
        self._contract = None if contract is None else tuple(str(part) for part in contract)
        self._admission_s = None if admission_s is None else float(admission_s)
        self._await_s = None if await_s is None else float(await_s)
        self._prov_s = None if provisional_move_s is None else float(provisional_move_s)
        self._prov_history = bool(history_candidate) and self._prov_s is not None
        self._prov_timer_fired = False
        self._prov_history_asked = False
        self._prov_sent = False
        if self._preset is not None:
            self._route_known = True
            self._successors = (self._preset,)
            self._cap = 1
            self._next = 1
            self._results[successor_probe_name(0)] = ProbeClass.PASS

    def __repr__(self) -> str:
        return (
            f"Judgement(kind={self.kind}, started={sorted(self._started)}, "
            f"done={None if self._done is None else self._done.verdict})"
        )

    # -- state --------------------------------------------------------------

    @property
    def done(self) -> Done | None:
        return self._done

    @property
    def results(self) -> dict[str, ProbeClass]:
        return dict(self._results)

    @property
    def successors(self) -> tuple[str, ...] | None:
        return self._successors

    @property
    def cap(self) -> int:
        return self._cap

    @property
    def started_successors(self) -> int:
        return self._next

    @property
    def probes_started(self) -> tuple[str, ...]:
        return tuple(self._started)

    @property
    def successor_probe_pending(self) -> bool:
        return any(
            name.startswith("S_C") and name not in self._results for name in self._started
        )

    @property
    def successor_timed_out(self) -> bool:
        return any(
            name.startswith("S_C") and value is ProbeClass.TIMEOUT
            for name, value in self._results.items()
        )

    def timings(self) -> dict[str, float]:
        base = self.t_ac if self.t_ac is not None else 0.0
        return {name: round((at - base) * 1000.0, 1) for name, at in self._resolved_at.items()}

    def terminal(self) -> bool:
        results = self._results
        accused = results.get(ACCUSED)
        if accused is None:
            return False
        if accused in ANSWERED:
            return True
        successors = [results.get(successor_probe_name(index)) for index in range(self._next)]
        if any(value is ProbeClass.PASS for value in successors):
            return True
        if successors and successors[-1] is ProbeClass.TIMEOUT:
            return True
        if self._route_known:
            assert self._successors is not None
            if not self._successors:
                return True
            if (
                all(value is ProbeClass.TRANSPORT_FAIL for value in successors)
                and self._next == min(self._cap, len(self._successors))
            ):
                return True
        return False

    def next_wakeup(self) -> float | None:
        if self._done is not None or self._readback_requested:
            return None
        wakeups = [self.t_evid]
        if self.t_ac is not None and self._next == 0 and not self._timer_fired:
            wakeups.append(self.t_ac + self._c.s_c0_delay_s)
        if self._prov_s is not None and self.t_ac is not None and not self._prov_timer_fired and not self._prov_sent:
            wakeups.append(self.t_ac + self._prov_s)
        return min(wakeups)

    def note_provisional_moved(self) -> None:
        """The service moved the route: no further ``ProvisionalMove``."""

        self._prov_sent = True

    def _provisional(self, now: float, *, timer: bool) -> list[JudgementAction]:
        if self._prov_s is None or self._prov_sent or self._done is not None:
            return []
        if self.t_ac is None or now < self.t_ac + self._prov_s:
            return []
        if timer:
            self._prov_timer_fired = True
        if ACCUSED in self._results or not self._route_known or not self._successors:
            return []
        if self._results.get(successor_probe_name(0)) is ProbeClass.PASS:
            self._prov_sent = True
            return [ProvisionalMove(self._successors[0], "s_c0")]
        if self._prov_history and not self._prov_history_asked and successor_probe_name(0) not in self._results:
            self._prov_history_asked = True
            return [ProvisionalMove(self._successors[0], "history")]
        return []

    # -- events -------------------------------------------------------------

    def start(self, now_perf: float, now_wall: float) -> list[JudgementAction]:
        """Issue A_C at t0 (no MIN_PROBE_REMAINING_S check for the accused)."""

        if self.t_ac is not None:
            return []
        self.t_ac = float(now_perf)
        self.t_ac_wall = float(now_wall)
        self._started[ACCUSED] = self.t_ac
        if self._preset is not None:
            name = successor_probe_name(0)
            self._started[name] = self._resolved_at[name] = self.t_ac
        return [self._probe(ACCUSED, self.leaf)]

    def on_route_state(
        self,
        order: Sequence[str],
        alive: Mapping[str, bool],
        now_perf: float,
    ) -> list[JudgementAction]:
        """S4/S3/S5 gathered: ``order`` is ``all`` minus Reject."""

        if self._done is not None or self._route_known:
            return []
        members = tuple(order)
        if self.leaf not in members:
            self._route_known = True
            self._successors = ()
            return [self._finish(None, "non_member", now_perf)]
        self._route_known = True
        if self._cap_override is None:
            self._cap = successor_cap(len(members), self._c)
        else:
            self._cap = self._cap_override
        self._successors = successor_list(members, self.leaf, alive)
        actions: list[JudgementAction] = []
        if self._successor_wanted and self._next == 0:
            self._successor_wanted = False
            actions.extend(self._start_successor(float(now_perf)))
        actions.extend(self._settle(float(now_perf)))
        return actions

    def on_route_state_unavailable(self, now_perf: float) -> list[JudgementAction]:
        """The route reads failed: no successor probes; EXHAUSTED is impossible."""

        if self._done is not None or self._route_known:
            return []
        self._successor_wanted = False
        self.skipped.append("route_state_unavailable")
        return self._settle(float(now_perf))

    def on_probe_result(
        self,
        name: str,
        value: ProbeClass | str,
        now_perf: float,
    ) -> list[JudgementAction]:
        now = float(now_perf)
        result = probe_class(value)
        if result is None or name not in self._started or name in self._results:
            return []
        self._results[name] = result
        self._resolved_at[name] = now
        actions: list[JudgementAction] = []
        if name == ACCUSED and (result in FAILED or result is ProbeClass.HTTP_ANSWER):
            actions.extend(self._report_accused(result))
        if self._done is not None:
            actions.extend(self._late_hold())
            return actions
        if name == ACCUSED and result in FAILED and self._next == 0:
            actions.extend(self._start_successor(now))
        if (
            name.startswith("S_C")
            and result is ProbeClass.TRANSPORT_FAIL
            and all(
                self._results.get(successor_probe_name(index)) is ProbeClass.TRANSPORT_FAIL
                for index in range(self._next)
            )
        ):
            actions.extend(self._start_successor(now))
        actions.extend(self._settle(now))
        if self._prov_s is not None and self._done is None and name == successor_probe_name(0):
            actions.extend(self._provisional(now, timer=False))
        return actions

    def on_timer(self, now_perf: float) -> list[JudgementAction]:
        now = float(now_perf)
        if self._done is not None:
            return []
        actions: list[JudgementAction] = []
        if (
            self.t_ac is not None
            and self._next == 0
            and not self._timer_fired
            and now >= self.t_ac + self._c.s_c0_delay_s
        ):
            # A_C still pending at t0 + S_C0_DELAY_S.
            self._timer_fired = True
            if not self.terminal():
                actions.extend(self._start_successor(now))
        actions.extend(self._settle(now))
        if self._prov_s is not None and self._done is None:
            actions.extend(self._provisional(now, timer=True))
        return actions

    def on_readback(self, accused_timed_out: bool | None, now_perf: float) -> list[JudgementAction]:
        """Result of the deadline readback; None when the read failed."""

        now = float(now_perf)
        if self._done is not None or not self._readback_requested:
            return []
        actions: list[JudgementAction] = []
        if accused_timed_out and ACCUSED not in self._results:
            self._results[ACCUSED] = ProbeClass.TIMEOUT
            self._resolved_at[ACCUSED] = now
            self.skipped.append("accused_from_readback")
            # Mihomo wrote L dead: the move happened, so the demotion watch and
            # the closes run now; the real A_C reply, when it comes, is ignored.
            actions.extend(self._report_accused(ProbeClass.TIMEOUT))
        actions.append(self._finish_verdict(now))
        return actions

    # -- internals ----------------------------------------------------------

    def _report_accused(self, result: ProbeClass) -> list[JudgementAction]:
        if self._accused_reported:
            return []
        self._accused_reported = True
        done = self._done
        if done is not None and done.reason == "non_member":
            # §18.6: a non-member judgement stops; L is not the group's to demote.
            self.skipped.append("accused_failed:non_member")
            return []
        return [AccusedFailed(self.leaf, result, late=done is not None)]

    def _probe(self, name: str, leaf: str) -> StartProbe:
        contract = self._contract
        return StartProbe(
            name=name,
            leaf=leaf,
            timeout_ms=int(self._c.native_timeout_ms),
            request_timeout_s=float(self._c.probe_request_timeout_s),
            url=None if contract is None else contract[1],
            expected=None if contract is None else contract[2],
            group=None if contract is None else contract[0],
            admission_s=self._admission_s,
            await_s=self._await_s,
        )

    def _start_successor(self, now: float) -> list[JudgementAction]:
        if not self._route_known:
            if ACCUSED in self._started and self._next == 0 and "route_state_unavailable" not in self.skipped:
                self._successor_wanted = True
            return []
        assert self._successors is not None
        if self._next >= min(self._cap, len(self._successors)):
            self.skipped.append(f"{successor_probe_name(self._next)}:cap")
            return []
        if self.t_evid - now < self._c.min_probe_remaining_s:
            self.skipped.append(f"{successor_probe_name(self._next)}:ticket_remaining")
            return []
        name = successor_probe_name(self._next)
        target = self._successors[self._next]
        self._next += 1
        self._started[name] = now
        return [self._probe(name, target)]

    def _settle(self, now: float) -> list[JudgementAction]:
        if self._done is not None:
            return []
        if self.terminal():
            return [self._finish_verdict(now)]
        if now >= self.t_evid:
            if ACCUSED not in self._results:
                if not self._readback_requested:
                    self._readback_requested = True
                    assert self.t_ac_wall is not None
                    return [NeedReadback(self.leaf, self.t_ac_wall)]
                return []
            return [self._finish_verdict(now)]
        return []

    def _finish_verdict(self, now: float) -> Done:
        result = verdict(
            self._results,
            self._next,
            self._cap,
            self._successors if self._route_known else None,
        )
        return self._finish(result, result.reason, now)

    def _finish(self, result: VerdictResult | None, reason: str, now: float) -> Done:
        self._done = Done(
            verdict=None if result is None else result.verdict,
            reason=reason,
            leaf=self.leaf,
            successor=None if result is None else result.successor,
            successor_index=None if result is None else result.successor_index,
            results=tuple((name, value.value) for name, value in self._results.items()),
            commit_allowed=bool(
                result is not None
                and result.verdict is Verdict.CONVICT_CONTRACT
                and now < self.ticket_deadline
            ),
            tokens=self.tokens,
            kind=self.kind,
        )
        return self._done

    def _late_hold(self) -> list[JudgementAction]:
        done = self._done
        if done is None or self._late_hold_reported or done.verdict in HOLD_VERDICTS:
            return []
        if done.verdict not in {Verdict.INDETERMINATE, None} or done.reason == "non_member":
            return []
        late = verdict(
            self._results,
            self._next,
            self._cap,
            self._successors if self._route_known else None,
        )
        if late.verdict in HOLD_VERDICTS:
            self._late_hold_reported = True
            return [LateHold(late.verdict)]
        return []


# ---------------------------------------------------------------------------
# Gates (§3.3) and verdict bookkeeping (§18.7 act)


GATE_NOT_ARMED = "not_armed"
GATE_JUDGING = "judging"
GATE_FACILITY_HOLD = "facility_hold"
GATE_EXHAUSTED_HOLD = "exhausted_hold"
GATE_D4_BUSY = "d4_busy"
GATE_LEAF_BACKOFF = "leaf_backoff"
GATE_LEAF_CHECK_CAP = "leaf_check_cap"
GATE_GLOBAL_CAP = "global_cap"
GATE_STATUS_FAILURE = "status_failure_sample"


class JudgeGates:
    """In-memory gates on the perf clock.  Nothing here persists."""

    _MAX_EXONERATIONS = 4096

    def __init__(self, constants: PostDialConstants = DEFAULT_POST_DIAL_CONSTANTS) -> None:
        self._c = constants
        self._in_flight: str | None = None
        self._hold: Verdict | None = None
        self._hold_since: float | None = None
        self._backoff: dict[str, float] = {}
        self._checks: dict[str, deque[float]] = {}
        self._judgements: deque[float] = deque()
        self._exonerated: dict[tuple[str, str], float] = {}
        # Fix 3 (10-28): the NOT_REPRODUCED back-off window per leaf (its end)
        # and the window whose one strong-trigger override was spent.
        self._nr_backoff: dict[str, float] = {}
        self._nr_override_used: dict[str, float] = {}
        self._override_in_flight: str | None = None

    def __repr__(self) -> str:
        return (
            f"JudgeGates(in_flight={self._in_flight is not None}, "
            f"hold={None if self._hold is None else self._hold.value}, "
            f"exonerated=<{len(self._exonerated)}>)"
        )

    @property
    def in_flight(self) -> bool:
        return self._in_flight is not None

    @property
    def hold(self) -> Verdict | None:
        return self._hold

    def blocked_reason(
        self,
        leaf: str,
        now_perf: float,
        *,
        armed: bool,
        d4_busy: bool,
        latest_sample_status_failure: bool | None = None,
        backoff_override: bool = False,
    ) -> str | None:
        """The first blocking gate in §3.3 order, or None when a judgement may start.

        ``latest_sample_status_failure`` is the caller's knowledge that L's
        latest native contract sample failed on status (gate 8).
        ``backoff_override`` (fix 3, 10-28): the trigger is a clearly failing
        FAST match; it passes a NOT_REPRODUCED back-off once per window.
        """

        now = float(now_perf)
        if not armed:
            return GATE_NOT_ARMED
        if self._in_flight is not None:
            return GATE_JUDGING
        if self._hold is Verdict.FACILITY:
            return GATE_FACILITY_HOLD
        if self._hold is Verdict.EXHAUSTED:
            return GATE_EXHAUSTED_HOLD
        if d4_busy:
            return GATE_D4_BUSY
        if now < self._backoff.get(leaf, float("-inf")) and not (
            backoff_override and self.backoff_overridable(leaf, now)
        ):
            return GATE_LEAF_BACKOFF
        if self._count(self._checks.get(leaf), now, self._c.leaf_check_window_s) >= self._c.leaf_check_cap:
            return GATE_LEAF_CHECK_CAP
        if self._count(self._judgements, now, self._c.global_window_s) >= self._c.global_max:
            return GATE_GLOBAL_CAP
        if latest_sample_status_failure:
            return GATE_STATUS_FAILURE
        return None

    def backoff_overridable(self, leaf: str, now_perf: float) -> bool:
        """L is inside a NOT_REPRODUCED back-off whose one override is unspent."""

        until = self._backoff.get(leaf)
        return bool(
            until is not None
            and float(now_perf) < until
            and self._nr_backoff.get(leaf) == until
            and self._nr_override_used.get(leaf) != until
        )

    def begin(self, leaf: str, now_perf: float) -> bool:
        """A judgement opens and A_C is issued: counts toward both caps.

        Returns True when it opens inside L's NOT_REPRODUCED back-off (only
        an override admits that): the window's override is then spent."""

        now = float(now_perf)
        override = self.backoff_overridable(leaf, now)
        if override:
            self._nr_override_used[leaf] = self._backoff[leaf]
        self._override_in_flight = leaf if override else None
        self._in_flight = leaf
        self._checks.setdefault(leaf, deque()).append(now)
        self._judgements.append(now)
        return override

    def note_rung(self, key: str, now_perf: float) -> None:
        """An escalated ladder rung opens (spec v1 §4.10): counts toward its
        own per-key cap and the global cap; the in-flight gate stays the
        ladder's rung ① leaf."""

        now = float(now_perf)
        self._checks.setdefault(key, deque()).append(now)
        self._judgements.append(now)

    def rung_blocked(self, key: str, now_perf: float) -> str | None:
        """``leaf_check_cap`` / ``global_cap`` for an escalated rung (the
        windows of ``blocked_reason``), else None."""

        now = float(now_perf)
        if self._count(self._checks.get(key), now, self._c.leaf_check_window_s) >= self._c.leaf_check_cap:
            return GATE_LEAF_CHECK_CAP
        if self._count(self._judgements, now, self._c.global_window_s) >= self._c.global_max:
            return GATE_GLOBAL_CAP
        return None

    def abandon(self, leaf: str) -> None:
        if self._in_flight == leaf:
            self._in_flight = None
        if self._override_in_flight == leaf:
            self._override_in_flight = None

    def finish(
        self,
        leaf: str,
        result: Verdict | None,
        tokens: Iterable[str],
        now_perf: float,
    ) -> None:
        """§18.7 ``act(v)`` bookkeeping; clears the in-flight gate."""

        now = float(now_perf)
        if self._in_flight == leaf:
            self._in_flight = None
        overridden = self._override_in_flight == leaf
        if overridden:
            self._override_in_flight = None
        if result in HOLD_VERDICTS:
            self.set_hold(result, now)
        elif result is Verdict.NOT_REPRODUCED:
            until = now + self._c.exonerate_s
            for token in tokens:
                self._exonerated[(leaf, str(token))] = until
            until = now + self._c.not_reproduced_backoff_s
            self._backoff[leaf] = until
            self._nr_backoff[leaf] = until
            if overridden:
                # The override's own NOT_REPRODUCED opens a window with the
                # override already spent: at most one override in a row.
                self._nr_override_used[leaf] = until
            self._prune_exonerations(now)
        elif result is Verdict.INDETERMINATE:
            self._backoff[leaf] = now + self._c.leaf_backoff_s

    def set_hold(self, result: Verdict, now_perf: float) -> None:
        if result not in HOLD_VERDICTS:
            return
        self._hold = result
        self._hold_since = float(now_perf)

    def note_answered(self, at_perf: float) -> bool:
        """An answered TIER3 user connection observed at ``at_perf`` releases the hold."""

        if self._hold is None or self._hold_since is None or float(at_perf) < self._hold_since:
            return False
        self._hold = None
        self._hold_since = None
        return True

    def is_exonerated(self, leaf: str, token: str, now_perf: float) -> bool:
        until = self._exonerated.get((leaf, token))
        if until is None:
            return False
        if float(now_perf) >= until:
            del self._exonerated[(leaf, token)]
            return False
        return True

    def exoneration_predicate(self, now_perf: float) -> Callable[[str, str], bool]:
        now = float(now_perf)
        return lambda leaf, token: self.is_exonerated(leaf, token, now)

    @staticmethod
    def _count(times: deque[float] | None, now: float, window: float) -> int:
        if not times:
            return 0
        while times and times[0] <= now - window:
            times.popleft()
        return len(times)

    def _prune_exonerations(self, now: float) -> None:
        if len(self._exonerated) <= self._MAX_EXONERATIONS:
            return
        for key in [key for key, until in self._exonerated.items() if until <= now]:
            del self._exonerated[key]
        while len(self._exonerated) > self._MAX_EXONERATIONS:
            oldest = min(self._exonerated, key=self._exonerated.__getitem__)
            del self._exonerated[oldest]


# ---------------------------------------------------------------------------
# Close selection (§6 rules 3 and 6 on top of the observer's rules 1, 2, 4, 5)


CLOSE_DISABLED = "disabled"
CLOSE_DEFER = "defer"
CLOSE_KEEP_ALL = "keep_all"
CLOSE_NONE = "none"
CLOSE_NEED_READBACK = "need_readback"
CLOSE_NOT_DEMOTED = "not_demoted"
CLOSE = "close"


@dataclass(frozen=True, slots=True)
class CloseDecision:
    action: str
    ids: tuple[str, ...]
    reason: str
    aged: int = 0
    waiting: int = 0
    kept: tuple[tuple[str, int], ...] = ()
    # Rule 6 ids among ``ids`` (key post_dial_close_frozen_streams).
    frozen: int = 0


def select_stuck_closes(
    stuck: StuckConnections,
    *,
    enabled: bool,
    successor_probe_pending: bool,
    successor_timed_out: bool,
    demotion_confirmed: bool | None,
    readback_age_s: float | None,
    constants: PostDialConstants = DEFAULT_POST_DIAL_CONSTANTS,
) -> CloseDecision:
    """§18.7 ``close_stuck``: defer on a pending successor, keep all after a
    successor timeout, and close only after a ≤1 s old demotion readback."""

    kept = (
        ("progressing", stuck.kept_progressing),
        ("not_stuck", stuck.kept_not_stuck),
        ("after_demotion", stuck.kept_after_demotion),
        ("other", stuck.kept_other),
    )
    if not enabled:
        return CloseDecision(CLOSE_DISABLED, (), "close_stuck_connections_off", kept=kept)
    if successor_probe_pending:
        return CloseDecision(CLOSE_DEFER, (), "successor_probe_pending", kept=kept)
    if successor_timed_out:
        return CloseDecision(CLOSE_KEEP_ALL, (), "successor_timeout", kept=kept)
    if not stuck.ids:
        return CloseDecision(CLOSE_NONE, (), "no_stuck_connection", kept=kept)
    if demotion_confirmed is None or readback_age_s is None or readback_age_s > 1.0:
        return CloseDecision(CLOSE_NEED_READBACK, (), "demotion_readback_required", kept=kept)
    if not demotion_confirmed:
        return CloseDecision(CLOSE_NOT_DEMOTED, (), "demotion_not_visible", kept=kept)
    ids = stuck.ids[: max(0, int(constants.max_close))]
    aged_ids = min(stuck.aged, len(ids))
    frozen = int(getattr(stuck, "frozen", 0) or 0)
    waiting_ids = min(int(stuck.waiting), len(ids) - aged_ids) if frozen else len(ids) - aged_ids
    return CloseDecision(
        CLOSE,
        ids,
        "stuck_on_demoted_leaf",
        aged=aged_ids,
        waiting=waiting_ids,
        kept=kept,
        frozen=len(ids) - aged_ids - waiting_ids,
    )


# ---------------------------------------------------------------------------
# Log-record helpers (§18.8)


def probe_start_record(
    action: StartProbe,
    tokenizer: PostDialTokenizer,
    *,
    now_wall: float,
    queue_wait_ms: float | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "ev": "probe_start",
        "t": round(float(now_wall), 3),
        "name": action.name,
        "leaf": tokenizer.leaf(action.leaf),
        "timeout_ms": action.timeout_ms,
    }
    if queue_wait_ms is not None:
        record["queue_wait_ms"] = round(float(queue_wait_ms), 1)
    return record


def probe_result_record(
    name: str,
    leaf: str,
    value: ProbeClass | str,
    tokenizer: PostDialTokenizer,
    *,
    now_wall: float,
    elapsed_ms: float | None = None,
    queue_wait_ms: float | None = None,
) -> dict[str, Any]:
    result = probe_class(value)
    record: dict[str, Any] = {
        "ev": "probe_result",
        "t": round(float(now_wall), 3),
        "name": name,
        "leaf": tokenizer.leaf(leaf),
        "class": None if result is None else result.value,
    }
    if elapsed_ms is not None:
        record["ms"] = round(float(elapsed_ms), 1)
    if queue_wait_ms is not None:
        record["queue_wait_ms"] = round(float(queue_wait_ms), 1)
    return record


def verdict_record(
    done: Done,
    judgement: Judgement,
    tokenizer: PostDialTokenizer,
    *,
    now_wall: float,
    mode: str,
    durable: bool = False,
) -> dict[str, Any]:
    """The verdict line.  ``durable`` is the judgement's
    ``durable_conviction`` setting (in shadow: the configured value), logged
    right after ``commit_allowed`` on every line."""

    return {
        "ev": "verdict" if mode == "act" else "would_verdict",
        "t": round(float(now_wall), 3),
        "verdict": None if done.verdict is None else done.verdict.value,
        "reason": done.reason,
        "kind": done.kind,
        "leaf": tokenizer.leaf(done.leaf),
        "successor": tokenizer.leaf(done.successor),
        "results": dict(done.results),
        "probes": list(judgement.probes_started),
        "t_ms": judgement.timings(),
        "toks": [token[:12] for token in done.tokens],
        "commit_allowed": done.commit_allowed,
        "durable": "on" if durable else "off",
        "skipped": list(judgement.skipped),
    }


def close_record(
    decision: CloseDecision,
    leaf: str,
    tokenizer: PostDialTokenizer,
    *,
    now_wall: float,
) -> dict[str, Any]:
    return {
        "ev": "close",
        "t": round(float(now_wall), 3),
        "leaf": tokenizer.leaf(leaf),
        "action": decision.action,
        "reason": decision.reason,
        "count": len(decision.ids),
        "aged": decision.aged,
        "waiting": decision.waiting,
        "kept": dict(decision.kept),
        # Rule 6 count, only when non-zero (so key-off records are unchanged).
        **({"frozen": int(decision.frozen)} if getattr(decision, "frozen", 0) else {}),
    }
