"""Post-dial judge worker (node-judgement design v3 P2-2 and P2-3).

One judgement at a time drives the pure ``runtime_node_judge.Judgement`` on
its own ``runtime-post-dial-judge`` thread (§18.6).  Every contract probe runs
on its own ``runtime-post-dial-probe`` thread, so the judge may stop *waiting*
at its evidence deadline while the probe keeps its broker slot until Mihomo
answers (§5.1).  The runtime supplies the controller reads, the probes, the
``native_commit`` and the exact-id closes through ``PostDialJudgeHooks``; this
module only schedules, gates and logs, with tokens and hashed leaves only.

P2 makes no route write: no PUT, pin, reload or provider write, and it never
DELETEs a group.  Mihomo's own failed contract sample is the move (§2).  The
closes of §6 run only on a leaf Mihomo has demoted, only for connections the
observer proved stuck, and never after a successor timed out.
"""

from __future__ import annotations

import copy
import queue
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping

from runtime_node_judge import (
    CLOSE,
    CLOSE_DEFER,
    CLOSE_DISABLED,
    CLOSE_KEEP_ALL,
    CLOSE_NEED_READBACK,
    CLOSE_NONE,
    CLOSE_NOT_DEMOTED,
    GATE_JUDGING,
    HOLD_VERDICTS,
    AccusedFailed,
    CloseDecision,
    Done,
    JudgeGates,
    Judgement,
    LateHold,
    NeedReadback,
    ProbeClass,
    ProvisionalMove,
    StartProbe,
    Verdict,
    close_record,
    contract_order,
    demotion_confirmed,
    member_alive,
    probe_class,
    probe_result_record,
    probe_start_record,
    provider_alive_map,
    readback_shows_accused_failure,
    select_stuck_closes,
    successor_timeout_exhausts,
    verdict_record,
)
from runtime_post_dial import (
    DEFAULT_POST_DIAL_CONSTANTS,
    RULE_FAST,
    DemotionWatch,
    PostDialConstants,
    PostDialTokenizer,
    SnapshotDigest,
    StuckConnections,
    gated_record,
    parse_rfc3339,
)
from runtime_route_audit import mihomo_proxies_entries
from runtime_tier_ladder import (
    ACTION_COMMIT,
    ACTION_ESCALATE,
    CLOSES_AFTER_COMMIT,
    LADDER_CAP_S,
    LADDER_FAILED,
    LADDER_PROBE_ADMISSION_S,
    OUTCOME_ABORTED,
    OUTCOME_COMMIT_LATE,
    OUTCOME_COMMIT_REJECTED,
    OUTCOME_CONVICTED,
    OUTCOME_EXHAUSTED_HOLD,
    OUTCOME_INDETERMINATE,
    OUTCOME_LADDER_BUDGET_EXHAUSTED,
    OUTCOME_LADDER_CAP,
    OUTCOME_LADDER_ROUTE_MOVED,
    OUTCOME_MOVED_UNPROVEN,
    OUTCOME_RECOVERED_BY_DEEPER_MOVE,
    OUTCOME_REFRESH_UNAVAILABLE,
    PROXY_FALLBACK,
    WITNESS_ESCALATE,
    WITNESS_ISSUE_AC,
    WITNESS_PROBE,
    EscalationCause,
    Rung,
    RungKind,
    RungVerdict,
    RungVerdictResult,
    LadderStep,
    alive_at,
    ac_after_witness_need_s,
    complete,
    escalation_need_s,
    fits,
    group_code,
    ladder_budget_s,
    ladder_next,
    legacy_hold,
    probe_await_s,
    probe_request_window_s,
    rung_constants,
    rung_demoted,
    rung_gate_key,
    rung_order,
    rung_successor_cap,
    rung_ticket_s,
    rung_verdict,
    token_field,
    witness_cap,
    witness_fit_need_s,
    witness_name,
    witness_next,
    witness_s,
)

JUDGE_THREAD_NAME = "runtime-post-dial-judge"
PROBE_THREAD_NAME = "runtime-post-dial-probe"
CLOSE_THREAD_NAME = "runtime-post-dial-close"
# Every poll re-emits an act trigger while its window still matches.  The
# gates that need controller-free runtime state (armed, D4, route binding)
# are evaluated at most this often per leaf and episode, and again as soon
# as a new route snapshot is published.
GATE_RECHECK_S = 0.5
# S5: prefix members (in ``all`` but not in the provider) are read one by
# one; usually there are none.  Beyond this many, the rest count as dead.
MAX_PREFIX_READS = 8
# A probe's result is awaited after the verdict for at most its broker
# admission window, its request window and this much dispatch slack.
PROBE_QUEUE_S = 1.5
PROBE_SLACK_S = 0.5
# An HTTP answer recorded by the judge stays L's "latest native sample"
# (§3.3 gate 8) until Mihomo's history shows a sample newer than this.
STATUS_SAMPLE_SLACK_S = 0.5
# Watch records outlive their observer watch by at most this long.
WATCH_RECORD_TTL_S = 120.0

Emit = Callable[[str, Mapping[str, Any]], None]


# ---------------------------------------------------------------------------
# Values exchanged with the runtime


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    """One contract probe as the runtime classified it (§5.2)."""

    probe: ProbeClass
    elapsed_ms: float | None = None
    queue_wait_ms: float | None = None


@dataclass(frozen=True, slots=True)
class RouteRead:
    """S4 (group), S3 (provider) and S5 (prefix members), read together."""

    members: tuple[str, ...] = field(repr=False)
    now: str = field(repr=False)
    fixed: str = field(repr=False)
    alive: Mapping[str, bool] = field(repr=False)
    read_at: float = 0.0
    # Members whose state no read showed (a failed S5 read, or past the S5
    # cap): never counted as Core-dead.
    unknown: frozenset[str] = field(default=frozenset(), repr=False)

    @property
    def order(self) -> tuple[str, ...]:
        return contract_order(self.members)

    @property
    def judge_order(self) -> tuple[str, ...]:
        """``order`` with the unknown members last, so they are tried after
        every member read Core-alive or Core-dead."""

        order = self.order
        return tuple(name for name in order if name not in self.unknown) + tuple(
            name for name in order if name in self.unknown
        )

    def demoted(self, leaf: str) -> bool:
        """§5.4 step 3: L read Core-dead, ``now`` is not L, and L is a member.

        A leaf whose state no read showed is never confirmed.
        """

        if str(leaf) in self.unknown:
            return False
        return demotion_confirmed(leaf, self.alive, self.now, members=self.members)

    def all_loaded_dead(self) -> bool | None:
        """§18.7 ``act(v)`` evidence for a hold: every loaded member read
        Core-dead.  None when no member was read alive but some were unknown."""

        order = self.order
        if any(self.alive.get(name, False) for name in order if name not in self.unknown):
            return False
        if any(name in self.unknown for name in order):
            return None
        return True


def route_read_from(
    group: Any,
    provider: Any,
    *,
    test_url: str,
    read_member: Callable[[str], Any],
    read_at: float,
    max_prefix_reads: int = MAX_PREFIX_READS,
    priority: Iterable[str] = (),
) -> RouteRead | None:
    """Build a ``RouteRead`` from ``GET /proxies/TIER3-AUTO`` and
    ``GET /providers/proxies/TIER3B-RUNTIME`` (``provider`` None when that
    read failed: every member is then read as a prefix member, up to the cap).

    ``priority`` members (the accused leaf) are read first among the prefix
    members.  A member whose read failed or fell past the cap is ``unknown``,
    never Core-dead.  None when the group read is unusable or names another
    contract.
    """

    if not isinstance(group, Mapping):
        return None
    group_url = str(group.get("testUrl") or "")
    if group_url and group_url != str(test_url):
        # The loaded contract changed (a reload): not this judgement's group.
        return None
    raw_members = group.get("all")
    if not isinstance(raw_members, (list, tuple)):
        return None
    members = tuple(str(member) for member in raw_members if str(member or "").strip())
    alive: dict[str, bool] = provider_alive_map(provider, test_url)
    first = {str(name) for name in priority}
    prefix = [name for name in contract_order(members) if name not in alive]
    prefix = [name for name in prefix if name in first] + [name for name in prefix if name not in first]
    cap = max(0, int(max_prefix_reads))
    unknown: set[str] = set(prefix[cap:])
    for name in prefix[:cap]:
        try:
            alive[name] = member_alive(read_member(name), test_url)
        except Exception:
            unknown.add(name)
    return RouteRead(
        members=members,
        now=str(group.get("now") or ""),
        fixed=str(group.get("fixed") or ""),
        alive=alive,
        read_at=float(read_at),
        unknown=frozenset(unknown),
    )


def _closed_ids(asked: tuple[str, ...], result: Any) -> tuple[str, ...]:
    """The asked ids the close hook reports closed (``True``: all of them)."""

    if result is True:
        return tuple(asked)
    if isinstance(result, (str, bytes)) or not isinstance(result, Iterable):
        return ()
    done = {str(item) for item in result}
    return tuple(item for item in asked if item in done)


def provider_member(provider: Any, name: str) -> Mapping[str, Any] | None:
    """One member entry of a ``GET /providers/proxies/{name}`` payload."""

    proxies = provider.get("proxies") if isinstance(provider, Mapping) else None
    if not isinstance(proxies, list):
        return None
    for entry in proxies:
        if isinstance(entry, Mapping) and entry.get("name") == name:
            return entry
    return None


def history_fresh(proxy: Any, test_url: str, *, now_wall: float, max_age_s: float) -> bool:
    """Key post_dial_successor_from_history (10-28): ``extra[test_url]``
    reads ``alive`` true and its newest history sample is alive (delay > 0)
    and at most ``max_age_s`` old.  False on any missing field."""

    extra = proxy.get("extra") if isinstance(proxy, Mapping) else None
    entry = extra.get(test_url) if isinstance(extra, Mapping) else None
    if not isinstance(entry, Mapping) or entry.get("alive") is not True:
        return False
    history = entry.get("history")
    if not isinstance(history, list):
        return False
    newest: tuple[float, Any] | None = None
    for sample in history:
        if not isinstance(sample, Mapping):
            continue
        at = parse_rfc3339(sample.get("time"))
        if at is not None and (newest is None or at > newest[0]):
            newest = (at, sample.get("delay"))
    if newest is None:
        return False
    try:
        delay = float(newest[1])
    except (TypeError, ValueError):
        return False
    age = float(now_wall) - newest[0]
    return delay > 0 and -1.0 <= age <= float(max_age_s)


def latest_contract_sample_wall(proxy: Any, test_url: str) -> float | None:
    """Wall time of the newest ``extra[testUrl].history`` sample, if any."""

    extra = proxy.get("extra") if isinstance(proxy, Mapping) else None
    entry = extra.get(test_url) if isinstance(extra, Mapping) else None
    history = entry.get("history") if isinstance(entry, Mapping) else None
    if not isinstance(history, list):
        return None
    times = [
        at
        for at in (
            parse_rfc3339(sample.get("time")) for sample in history if isinstance(sample, Mapping)
        )
        if at is not None
    ]
    return max(times) if times else None


@dataclass(frozen=True, slots=True)
class PostDialJudgeHooks:
    """The runtime's side of a judgement.  Every hook may raise; the judge
    treats a raising hook as its neutral result (indeterminate, unavailable,
    not committed, not closed)."""

    # Run one contract probe to completion on the calling (probe) thread.
    probe: Callable[["PostDialJudgeJob", StartProbe], ProbeOutcome]
    # S4 + S3 + S5 for the job's contract.
    read_route: Callable[["PostDialJudgeJob"], RouteRead | None]
    # The deadline readback's S3 (or S5) entry for one leaf.
    read_leaf: Callable[["PostDialJudgeJob", str], Mapping[str, Any] | None]
    # §18.7 native_commit after CONVICT_CONTRACT: (accepted, reason).  It
    # settles the job's recovery ticket itself.  With ``job.durable`` False
    # (move-only) it only rebinds the route and the ticket.
    commit: Callable[["PostDialJudgeJob", Done], tuple[bool, str]]
    # Settle the job's recovery ticket without a commit.
    finish: Callable[["PostDialJudgeJob", str], None]
    # Exact-id closes, run to completion on the calling (close) thread: the
    # ids whose DELETE succeeded.  Only those are marked closed; the watch
    # retries the rest.  (``True`` means every given id was closed.)
    close: Callable[["PostDialJudgeJob", str, tuple[str, ...]], Iterable[str] | bool]
    # ``close_stuck_connections`` and act mode.
    close_enabled: Callable[[], bool]
    # Mihomo demoted L (read back) and the judgement has acted: the runtime
    # refreshes its published route unless something already republished it
    # (the commit's own refresh), so the next trigger binds without a stale
    # ``route_binding_mismatch`` round.  Called at most once per judgement.
    route_moved: Callable[["PostDialJudgeJob"], None] | None = None
    # Tier ladder only (``job.ladder`` set; spec v1 §4.6-§4.8).  The authority
    # and D4-transaction check before every probe unit and every commit: a
    # stop reason (``authority_changed:<field>`` / ``d4_txn_busy:<sub>``) or
    # None.
    ladder_check: Callable[["PostDialJudgeJob"], str | None] | None = None
    # The escalation to rung ``k`` (0-based, k >= 1): sacrificial read, fresh
    # refresh, route compare; stores rung k's baseline and alive map in
    # ``job.ladder`` (``set_rung_state``).  A stop reason or None.
    ladder_escalate: Callable[["PostDialJudgeJob", int], str | None] | None = None
    # The ladder ended with ``outcome`` (lease wake, overshoot re-evaluation).
    ladder_end: Callable[["PostDialJudgeJob", str], None] | None = None
    # Key post_dial_provisional_move (10-28): pin the rung-1 group to the
    # successor through the route-lease write; (moved, reason).  Never
    # writes dead and closes nothing.
    provisional_move: Callable[["PostDialJudgeJob", str], tuple[bool, str]] | None = None
    # Key post_dial_successor_from_history (10-28): the successor's newest
    # provider history (read-only ``GET /providers/proxies/<provider>``) is
    # alive and at most SUCCESSOR_HISTORY_MAX_AGE_S old.
    successor_history: Callable[["PostDialJudgeJob", str], bool] | None = None


class PostDialJudgeJob:
    """One admitted judgement: the trigger's coordinates and the runtime's
    context (ticket, snapshot, binding).  Names never reach the repr."""

    __slots__ = (
        "leaf",
        "kind",
        "tokens",
        "episode",
        "test_url",
        "expected_status",
        "ticket_deadline",
        "runner",
        "context",
        "judgement",
        "settled",
        "demoted",
        "acted",
        "route_moved_sent",
        "durable",
        "ladder",
        "provisional",
    )

    def __init__(
        self,
        *,
        leaf: str,
        kind: str,
        tokens: Iterable[str],
        episode: int,
        test_url: str,
        expected_status: str,
        ticket_deadline: float,
        runner: Any,
        context: Any,
        durable: bool = False,
        ladder: "PostDialLadder | None" = None,
    ) -> None:
        self.leaf = str(leaf)
        self.kind = str(kind)
        self.tokens = tuple(str(token) for token in tokens)
        self.episode = int(episode)
        self.test_url = str(test_url)
        self.expected_status = str(expected_status)
        self.ticket_deadline = float(ticket_deadline)
        self.runner = runner
        self.context = context
        # ``durable_conviction``, read once at admission: the verdict line,
        # the commit line and the commit hook's branch all use this value.
        self.durable = bool(durable)
        self.judgement: Judgement | None = None
        self.settled = False
        # Worker-thread only: the demotion was read back, ``act`` has run,
        # and the route_moved hook was called.
        self.demoted = False
        self.acted = False
        self.route_moved_sent = False
        # Tier ladder state (spec v1 §4.7); None is today's judgement.
        self.ladder = ladder
        # The successor of an accepted provisional move (10-28), else None.
        self.provisional: str | None = None

    def __repr__(self) -> str:
        return f"PostDialJudgeJob(kind={self.kind}, episode={self.episode})"


def _graph_names(graph: Any) -> frozenset[str]:
    """Group names of a loaded group graph (a mapping, or ``(name, members)`` pairs)."""

    if isinstance(graph, Mapping):
        return frozenset(str(name) for name in graph)
    names: set[str] = set()
    try:
        for item in graph or ():
            if isinstance(item, (tuple, list)) and item:
                names.add(str(item[0]))
    except TypeError:
        pass
    return frozenset(names)


class PostDialLadder:
    """One tier ladder (spec v1 §4.7), set as ``job.ladder`` at admission
    (``tier_ladder_judge`` on, act mode).  None everywhere else, which is
    today's judgement.

    The judge worker owns ``index`` (the rung its hooks read: ``current``)
    and the per-rung result sets.  The runtime hooks write:

    - ``set_rung_state(k, alive=..., unknown=..., baseline=...)`` and
      ``deeper_moved`` from ``ladder_escalate`` (the escalation refresh);
    - ``drop_consumed[k] = True`` under ``lock`` from a rung read issued
      after ``accused_failed_known[k]`` was set;
    - ``note_commit(pick=..., proven=..., sacrificial=...)`` from the rung
      commit (logged on ``commit`` / ``rung_commit``).

    ``view(index)`` is the same ladder (shared sets and lock) read at one
    rung, for a close watch that outlives the current rung.  Names never
    reach the repr.
    """

    def __init__(
        self,
        *,
        rungs: Iterable[Rung],
        authority: Any,
        deadline: float,
        ladder_id: str,
        chain0: Iterable[str],
        admitted_at: float | None = None,
        lease: str = "none",
        group_names: Iterable[str] | None = None,
    ) -> None:
        self.rungs: tuple[Rung, ...] = tuple(rungs)
        self.authority = authority
        self.deadline = float(deadline)
        self.ladder_id = str(ladder_id)
        self.chain0: tuple[str, ...] = tuple(str(name) for name in chain0)
        self.admitted_at = None if admitted_at is None else float(admitted_at)
        self.lease = str(lease or "none")
        names = set(_graph_names(getattr(authority, "group_graph", ())))
        if group_names is not None:
            names |= {str(name) for name in group_names}
        for rung in self.rungs:
            names.add(rung.group)
            if rung.kind is RungKind.GROUP:
                names.add(rung.accused)
        self.group_names = frozenset(names)
        self.lock = threading.Lock()
        self.index = 0
        # Written by the runtime's escalation hook (rung k's refresh).
        self.baselines: dict[int, Any] = {}
        self.rung_alive: dict[int, dict[str, bool]] = {}
        self.rung_unknown: dict[int, frozenset[str]] = {}
        self.deeper_moved = False
        # §4.8 sacrificial-read bookkeeping, per rung index (True values only).
        self.drop_consumed: dict[int, bool] = {}
        self.accused_failed_known: dict[int, bool] = {}
        # Per rung index: members failed in this ladder at that rung's
        # contract, passing successors / witnesses, a successor or witness
        # timeout, the passed witness.
        self.failed: dict[int, set[str]] = {}
        self.passed: dict[int, set[str]] = {}
        self.timed_out: dict[int, bool] = {}
        self.witness: dict[int, str] = {}
        # The rung commit's facts (``note_commit``).
        self.commit_pick: str | None = None
        self.commit_proven = False
        self.commit_sacrificial = False
        # Counters for ``ladder_end``.
        self.probes = 0
        self.witnesses = 0
        self.dead_marks = 0
        self.witness_wasted = False
        self.pf_probe = False
        self.pf_probe_resolved_at: float | None = None
        self.rungs_run = 0
        self.outcome: str | None = None
        self.ended = False

    def __repr__(self) -> str:
        return (
            f"PostDialLadder(rungs={len(self.rungs)}, index={self.index}, "
            f"outcome={self.outcome})"
        )

    @property
    def current(self) -> Rung:
        return self.rungs[min(max(0, int(self.index)), len(self.rungs) - 1)]

    def on_path(self, group: str | None) -> bool:
        """The off-path guard (design §19.1): only a rung group's contract."""

        return group is not None and any(rung.group == group for rung in self.rungs)

    def set_rung_state(
        self,
        index: int,
        *,
        alive: Mapping[str, bool],
        unknown: Iterable[str] = (),
        baseline: Any = None,
    ) -> None:
        with self.lock:
            self.rung_alive[int(index)] = {str(name): bool(value) for name, value in alive.items()}
            self.rung_unknown[int(index)] = frozenset(str(name) for name in unknown)
            if baseline is not None:
                self.baselines[int(index)] = baseline

    def rung_state(self, index: int) -> tuple[dict[str, bool] | None, frozenset[str]]:
        """Rung ``index``'s alive map and unknown members from the escalation
        refresh, else from a stored ``(binding, snapshot, payload)`` baseline;
        ``(None, ())`` when neither exists."""

        with self.lock:
            alive = self.rung_alive.get(int(index))
            unknown = self.rung_unknown.get(int(index), frozenset())
            baseline = self.baselines.get(int(index))
        if alive is not None:
            return dict(alive), unknown
        payload = baseline[2] if isinstance(baseline, tuple) and len(baseline) == 3 else None
        # The baseline payload is the stored publication wrapper
        # {"proxies": <GET /proxies response>, "proxy_map": ...}: one accessor
        # (10-30; a single .get("proxies") read the response, not the members).
        entries = mihomo_proxies_entries(payload)
        if not entries:
            return None, frozenset()
        rung = self.rungs[int(index)]
        return (
            alive_at(entries, rung.url, rung.members),
            frozenset(name for name in rung.members if name not in entries),
        )

    def note_commit(self, *, pick: str | None, proven: bool, sacrificial: bool) -> None:
        with self.lock:
            self.commit_pick = None if pick is None else str(pick)
            self.commit_proven = bool(proven)
            self.commit_sacrificial = bool(sacrificial)

    def passed_at(self, index: int) -> frozenset[str]:
        with self.lock:
            return frozenset(self.passed.get(int(index), ()))

    def failed_at(self, index: int) -> frozenset[str]:
        with self.lock:
            return frozenset(self.failed.get(int(index), ()))

    def view(self, index: int) -> "PostDialLadder":
        clone = copy.copy(self)
        clone.index = int(index)
        return clone


def _outcome_text(reason: Any) -> str:
    """A hook's stop reason as a §4.11 outcome (``authority_changed:pid`` →
    ``authority_changed_pid``, ``d4_txn_busy:<sub>`` → ``d4_txn_busy_<sub>``)."""

    return str(reason).replace(":", "_")


class _LadderPhase:
    """One rung phase (witness or A_C): its own event queue and pending
    probes, so a late result of an earlier phase is never applied here."""

    __slots__ = (
        "rung",
        "judgement",
        "events",
        "pending",
        "done",
        "late",
        "refusal",
        "read",
        "witness",
        "wresults",
        "alive",
        "unknown",
    )

    def __init__(self, rung: Rung, judgement: Judgement | None = None, *, witness: bool = False) -> None:
        self.rung = rung
        self.judgement = judgement
        self.events: queue.Queue[tuple[str, ProbeOutcome]] = queue.Queue()
        self.pending: dict[str, tuple[float, str]] = {}
        self.done: Done | None = None
        self.late: Verdict | None = None
        self.refusal: str | None = None
        self.read: RouteRead | None = None
        self.witness = bool(witness)
        # Witness phase: target -> class (probe order), and the escalation
        # refresh's alive map it ordered the witnesses by.
        self.wresults: dict[str, ProbeClass] = {}
        self.alive: dict[str, bool] | None = None
        self.unknown: frozenset[str] = frozenset()


class _LadderEnd:
    """How a ladder ended, stamped at the decision (critic M11)."""

    __slots__ = ("outcome", "hold", "commit", "index", "wall", "perf", "after")

    def __init__(self) -> None:
        self.outcome = OUTCOME_ABORTED
        self.hold: Verdict | None = None
        self.commit = "none"
        self.index = 0
        self.wall: float | None = None
        self.perf: float | None = None
        # Run after ``ladder_end`` is logged (the deeper-move watch).
        self.after: Callable[[], Any] | None = None


class _LadderWatch:
    """A ladder close watch: ``leaf`` = the unchanged rung ① leaf rule
    (``RouteRead.demoted(L0)``), ``group`` = after an accepted group-rung
    commit or a deeper move (``rung_demoted`` at that rung)."""

    __slots__ = ("mode", "rung", "pick", "judgement", "timed_out", "fields")

    def __init__(
        self,
        mode: str,
        rung: Rung,
        pick: str | None,
        judgement: Judgement | None,
        timed_out: bool,
        fields: Mapping[str, Any],
    ) -> None:
        self.mode = mode
        self.rung = rung
        self.pick = pick
        self.judgement = judgement
        self.timed_out = bool(timed_out)
        self.fields = dict(fields)


class _WatchRecord:
    __slots__ = (
        "job",
        "runner",
        "t_demote_wall",
        "created_at",
        "defer_logged",
        "not_demoted_logged",
        "failed_logged",
        "ladder",
    )

    def __init__(self, job: PostDialJudgeJob, runner: Any, t_demote_wall: float, created_at: float) -> None:
        self.job = job
        self.runner = runner
        self.t_demote_wall = float(t_demote_wall)
        self.created_at = float(created_at)
        self.defer_logged = False
        self.not_demoted_logged = False
        self.failed_logged = False
        self.ladder: _LadderWatch | None = None


# ---------------------------------------------------------------------------
# The service


class PostDialJudgeService:
    """Gates, the single judge worker and the demotion-watch closes (§18.5-18.7).

    One per runtime; nothing here is persisted.  All clocks are injectable:
    ``clock`` is the runtime's recovery clock (ticket deadlines, gates and the
    judgement scheduler share it), ``wall`` is compared with Mihomo's times.
    """

    def __init__(
        self,
        *,
        hooks: PostDialJudgeHooks,
        tokenizer: PostDialTokenizer,
        emit: Emit,
        constants: PostDialConstants = DEFAULT_POST_DIAL_CONSTANTS,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._hooks = hooks
        self._tokenizer = tokenizer
        self._emit_fn = emit
        self._c = constants
        self._clock = clock
        self._wall = wall
        self._lock = threading.RLock()
        self.gates = JudgeGates(constants)
        self._worker: threading.Thread | None = None
        self._job: PostDialJudgeJob | None = None
        self._threads: set[threading.Thread] = set()
        self._watches: dict[str, _WatchRecord] = {}
        self._closing: set[str] = set()
        self._gated: dict[tuple[str, str], tuple[int, str]] = {}
        self._expensive: dict[str, tuple[float, int, str | None, Any]] = {}
        self._status_failures: dict[str, float] = {}
        self._hold_set_wall: float | None = None
        self._exonerations = False
        # The tier ladder in flight (spec v1 §5.2 L5, Track H), else None.
        self._ladder_job: PostDialJudgeJob | None = None
        self.metrics: defaultdict[str, int] = defaultdict(int)
        # 10-28 ship2 fix keys (``NodeJudgementPolicy.not_reproduced_override``
        # / ``exhausted_hold``), refreshed by the runtime at each act trigger;
        # False (the default) keeps every path as before.
        self.backoff_override_on = False
        self.successor_timeout_hold = False
        # 10-28 seamless keys (post_dial_provisional_move /
        # post_dial_successor_from_history / post_dial_fast_successor),
        # refreshed by the runtime at each act trigger; False keeps every
        # path as before.
        self.provisional_move_on = False
        self.successor_history_on = False
        self.fast_successor_on = False

    def __repr__(self) -> str:
        return (
            f"PostDialJudgeService(judging={self.judging()}, "
            f"hold={None if self.gates.hold is None else self.gates.hold.value}, "
            f"watches=<{len(self._watches)}>)"
        )

    @property
    def tokenizer(self) -> PostDialTokenizer:
        return self._tokenizer

    @property
    def constants(self) -> PostDialConstants:
        return self._c

    # -- observer-thread hooks ----------------------------------------------

    def is_exonerated(self, leaf: str, token: str) -> bool:
        """§3.3 event filter: the per-(leaf, token) exoneration of NOT_REPRODUCED."""

        if not self._exonerations:
            return False
        with self._lock:
            return self.gates.is_exonerated(leaf, token, self._clock())

    def on_snapshot(self, digest: SnapshotDigest, previous_poll_wall: float | None) -> None:
        """Release a FACILITY/EXHAUSTED hold on an answered TIER3 user connection.

        The digest covers everything since the previous accepted poll, so it
        counts only when that whole window lies after the hold was set.
        """

        if not digest.accepted or not digest.answered_tier3_user:
            return
        with self._lock:
            hold = self.gates.hold
            since = self._hold_set_wall
            if hold is None or since is None or previous_poll_wall is None or previous_poll_wall < since:
                return
            if not self.gates.note_answered(self._clock()):
                return
            self._hold_set_wall = None
        self._emit("info", {"ev": "hold_released", "t": round(self._wall(), 3), "hold": hold.value})

    def on_watch_step(self, runner: Any, watch: DemotionWatch, stuck: StuckConnections) -> None:
        """One demotion-watch poll with stuck ids on L (§18.7 ``close_stuck``).

        Runs on the observer thread: it only decides, and hands a close that
        needs the ≤1 s demotion readback to its own short-lived thread.
        """

        leaf = watch.leaf
        with self._lock:
            record = self._watches.get(leaf)
            if record is None or record.runner is not runner or leaf in self._closing:
                return
        if record.ladder is not None:
            self._ladder_watch_step(record, stuck)
            return
        pre = self._close_decision(record, stuck, route=None)
        if pre.action != CLOSE_NEED_READBACK:
            self._settle_close(record, pre)
            return
        with self._lock:
            if leaf in self._closing:
                return
            self._closing.add(leaf)
        self._spawn(CLOSE_THREAD_NAME, self._close_worker, record, stuck)

    # -- the trigger path (observer thread) ---------------------------------

    def judging(self) -> bool:
        with self._lock:
            worker = self._worker
            return bool((worker is not None and worker.is_alive()) or self.gates.in_flight)

    def backoff_override_eligible(self, trigger: Any) -> bool:
        """Fix 3 (10-28): a FAST trigger whose failed-share inputs show the
        leaf clearly failing (share >= NOT_REPRODUCED_OVERRIDE_MIN_SHARE over
        n = failed + answered >= NOT_REPRODUCED_OVERRIDE_MIN_N)."""

        if not self.backoff_override_on:
            return False
        if trigger is None or str(getattr(trigger, "kind", "") or "") != RULE_FAST:
            return False
        failed = getattr(trigger, "share_failed", None)
        answered = getattr(trigger, "share_answered", None)
        if failed is None or answered is None:
            return False
        n = int(failed) + int(answered)
        if n <= 0 or n < int(self._c.not_reproduced_override_min_n):
            return False
        return int(failed) / n >= float(self._c.not_reproduced_override_min_share)

    def blocked_cheap(self, leaf: str, now: float, trigger: Any = None) -> str | None:
        """The gates that need no runtime state: judging, holds, back-off, caps."""

        override = self.backoff_override_eligible(trigger)
        with self._lock:
            worker = self._worker
            if worker is not None and worker.is_alive():
                # A verdict is out but late probe results are still awaited:
                # the 2-socket broker bound and one-in-flight stay honest.
                return GATE_JUDGING
            return self.gates.blocked_reason(
                leaf, now, armed=True, d4_busy=False, backoff_override=override
            )

    def enter_exhausted_hold(self, leaf: str, cause: str) -> bool:
        """Fix 2 (10-28; act + exhausted_hold): the published TIER3-AUTO view is exhausted at
        admission, so no judgement opens; the EXHAUSTED hold is set (released
        by an answered TIER3 user connection, as for a verdict hold)."""

        with self._lock:
            worker = self._worker
            if (worker is not None and worker.is_alive()) or self.gates.in_flight:
                return False
            if self.gates.hold is not None:
                return False
            self.gates.set_hold(Verdict.EXHAUSTED, self._clock())
            self._hold_set_wall = self._wall()
            self.metrics["exhausted_hold_entered"] += 1
        self._emit_hold_entered(leaf, cause)
        return True

    def _emit_hold_entered(self, leaf: str, cause: str) -> None:
        self._emit(
            "info",
            {
                "ev": "hold_entered",
                "t": round(self._wall(), 3),
                "leaf": self._tokenizer.leaf(leaf),
                "hold": Verdict.EXHAUSTED.value,
                "cause": str(cause),
            },
        )

    def blocked(
        self,
        leaf: str,
        now: float,
        *,
        armed: bool,
        d4_busy: bool,
        status_failure: bool,
        trigger: Any = None,
    ) -> str | None:
        override = self.backoff_override_eligible(trigger)
        with self._lock:
            return self.gates.blocked_reason(
                leaf,
                now,
                armed=armed,
                d4_busy=d4_busy,
                latest_sample_status_failure=status_failure,
                backoff_override=override,
            )

    def recent_gate(
        self,
        leaf: str,
        episode: int,
        now: float,
        route: Any = None,
    ) -> tuple[bool, str | None]:
        """The last expensive-gate answer for this leaf and episode, if fresh.

        ``route`` is the published route snapshot object the answer was
        computed against: a new publication (for example the refresh a stale
        binding queued) invalidates it at once, so the next event in the same
        burst is judged against the refreshed route.
        """

        with self._lock:
            cached = self._expensive.get(leaf)
        if cached is None:
            return False, None
        checked_at, cached_episode, reason, cached_route = cached
        if (
            cached_episode != int(episode)
            or now - checked_at >= GATE_RECHECK_S
            or reason is None
            or cached_route is not route
        ):
            return False, None
        return True, reason

    def remember_gate(
        self,
        leaf: str,
        episode: int,
        now: float,
        reason: str | None,
        route: Any = None,
    ) -> None:
        with self._lock:
            if len(self._expensive) > 256:
                self._expensive.clear()
            self._expensive[leaf] = (float(now), int(episode), reason, route)

    def note_gated(self, trigger: Any, reason: str, *, arm_check: str | None = None) -> None:
        """Log ``gated`` once per (leaf, rule, episode, reason)."""

        key = (str(trigger.leaf), str(trigger.kind))
        value = (int(getattr(trigger, "episode", 0) or 0), str(reason))
        with self._lock:
            if reason == "not_armed" and arm_check is not None:
                checks = self.__dict__.setdefault("_gated_arm_checks", {})
                previous = checks.get(key)
                current = (value[0], str(arm_check))
                if previous is not None and previous[0] == current[0] and previous[1] != current[1]:
                    self.metrics["gated_not_armed_check_changes"] += 1
                if len(checks) >= 256 and key not in checks:
                    checks.pop(next(iter(checks)))
                checks[key] = current
            if self._gated.get(key) == value:
                return
            if len(self._gated) > 256:
                self._gated.clear()
            self._gated[key] = value
            self.metrics[f"gated_{reason}"] += 1
        self._emit("info", gated_record(trigger, self._tokenizer, reason=reason, now_wall=self._wall(), arm_check=arm_check))

    def status_failure_at(self, leaf: str) -> float | None:
        with self._lock:
            return self._status_failures.get(leaf)

    def latest_sample_is_status_failure(self, leaf: str, proxy: Any, test_url: str) -> bool:
        """§3.3 gate 8: the judge's own HTTP answer on L is still L's newest
        native contract sample.

        Mihomo's history keeps no status, so only this process knows that a
        failed sample was an answer.  ``proxy`` is L's entry in the published
        snapshot, which may predate the answer (its ``alive`` then still says
        true): only a history sample newer than the answer supersedes it.
        """

        recorded = self.status_failure_at(leaf)
        if recorded is None:
            return False
        latest = latest_contract_sample_wall(proxy, test_url)
        if latest is not None and latest > recorded + STATUS_SAMPLE_SLACK_S:
            with self._lock:
                self._status_failures.pop(leaf, None)
            return False
        return True

    def start(self, job: PostDialJudgeJob) -> bool:
        """Open the judgement (counts toward both caps) and start its worker."""

        with self._lock:
            worker = self._worker
            if (worker is not None and worker.is_alive()) or self.gates.in_flight:
                return False
            override = self.gates.begin(job.leaf, self._clock())
            if override:
                self.metrics["backoff_override"] += 1
            if job.ladder is not None:
                if job.ladder.admitted_at is None:
                    job.ladder.admitted_at = self._clock()
                self._ladder_job = job
            thread = threading.Thread(target=self._run, args=(job,), name=JUDGE_THREAD_NAME, daemon=True)
            self._worker = thread
            self._job = job
            self._threads.add(thread)
            self.metrics["judgements"] += 1
        if override:
            self._emit(
                "info",
                {
                    "ev": "backoff_override",
                    "t": round(self._wall(), 3),
                    "leaf": self._tokenizer.leaf(job.leaf),
                    "rule": str(job.kind),
                },
            )
        thread.start()
        return True

    def ladder_in_flight(self) -> bool:
        """A tier ladder is between its start and its ``ladder_end`` hook
        (spec v1 §5.2 L5: the lease watcher defers while this is true)."""

        with self._lock:
            return self._ladder_job is not None

    def wait_idle(self, timeout: float) -> bool:
        """Join the worker, probe and close threads (tests and teardown)."""

        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            with self._lock:
                threads = [thread for thread in self._threads if thread.is_alive()]
                self._threads = set(threads)
            if not threads:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return False
            threads[0].join(min(remaining, 0.05))

    # -- the judge worker ----------------------------------------------------

    def _run(self, job: PostDialJudgeJob) -> None:
        if job.ladder is not None:
            self._run_ladder(job)
            return
        judgement = Judgement(
            job.leaf,
            kind=job.kind,
            tokens=job.tokens,
            ticket_deadline_perf=job.ticket_deadline,
            constants=self._judgement_constants(),
        )
        job.judgement = judgement
        events: queue.Queue[tuple[str, ProbeOutcome]] = queue.Queue()
        pending: dict[str, tuple[float, str]] = {}
        try:
            with self._lock:
                actions = judgement.start(self._clock(), self._wall())
            self._dispatch(job, actions, events, pending)
            # S4 · S3 · S5, concurrently with A_C on its own thread.
            route = self._call(lambda: self._hooks.read_route(job), None)
            with self._lock:
                now = self._clock()
                if isinstance(route, RouteRead):
                    actions = judgement.on_route_state(route.judge_order, route.alive, now)
                else:
                    actions = judgement.on_route_state_unavailable(now)
            self._dispatch(job, actions, events, pending)
            while True:
                with self._lock:
                    done = judgement.done
                    wakeup = judgement.next_wakeup()
                if done is not None and not pending:
                    break
                now = self._clock()
                if done is not None:
                    timeout = max(stop for stop, _leaf in pending.values()) - now
                    if timeout <= 0.0:
                        break
                elif wakeup is None:
                    # Not reachable: an undecided judgement always has its
                    # evidence deadline.  Wake at the ticket end regardless.
                    timeout = max(0.0, job.ticket_deadline - now)
                else:
                    timeout = max(0.0, wakeup - now)
                try:
                    name, outcome = events.get(timeout=timeout)
                except queue.Empty:
                    if done is not None:
                        break
                    with self._lock:
                        actions = judgement.on_timer(self._clock())
                    self._dispatch(job, actions, events, pending)
                    continue
                _stop, probe_leaf = pending.pop(name, (0.0, job.leaf))
                self._emit(
                    "info",
                    probe_result_record(
                        name,
                        probe_leaf,
                        outcome.probe,
                        self._tokenizer,
                        now_wall=self._wall(),
                        elapsed_ms=outcome.elapsed_ms,
                        queue_wait_ms=outcome.queue_wait_ms,
                    ),
                )
                with self._lock:
                    if outcome.probe is ProbeClass.HTTP_ANSWER:
                        self._status_failures[probe_leaf] = self._wall()
                    actions = judgement.on_probe_result(name, outcome.probe, self._clock())
                self._dispatch(job, actions, events, pending)
        except Exception as exc:  # pragma: no cover - defensive
            self._emit("warning", {"ev": "judge_failed", "error": type(exc).__name__})
        finally:
            with self._lock:
                if self.gates.in_flight:
                    self.gates.abandon(job.leaf)
                if self._worker is threading.current_thread():
                    self._worker = None
                    self._job = None
            if not job.settled:
                job.settled = True
                self._call(lambda: self._hooks.finish(job, "judge_aborted"), None)

    def _dispatch(
        self,
        job: PostDialJudgeJob,
        actions: Iterable[Any],
        events: queue.Queue[tuple[str, ProbeOutcome]],
        pending: dict[str, tuple[float, str]],
    ) -> None:
        judgement = job.judgement
        assert judgement is not None
        # A successor probe starts before the same batch's demotion readback
        # (§4.1: S_C0 starts when A_C fails, not after the re-read).
        ordered = sorted(actions, key=lambda item: 0 if isinstance(item, StartProbe) else 1)
        for action in ordered:
            if isinstance(action, StartProbe):
                self._start_probe(job, action, events, pending)
            elif isinstance(action, NeedReadback):
                entry = self._call(lambda: self._hooks.read_leaf(job, action.leaf), None)
                timed_out = (
                    None
                    if not isinstance(entry, Mapping)
                    else readback_shows_accused_failure(entry, job.test_url, action.accused_issued_wall)
                )
                self._emit(
                    "info",
                    {
                        "ev": "readback",
                        "t": round(self._wall(), 3),
                        "leaf": self._tokenizer.leaf(action.leaf),
                        "accused_timeout": timed_out,
                    },
                )
                with self._lock:
                    more = judgement.on_readback(timed_out, self._clock())
                self._dispatch(job, more, events, pending)
            elif isinstance(action, AccusedFailed):
                self._on_accused_failed(job, action)
            elif isinstance(action, LateHold):
                with self._lock:
                    self.gates.set_hold(action.verdict, self._clock())
                    self._hold_set_wall = self._wall()
                self._emit(
                    "info",
                    {
                        "ev": "late_hold",
                        "t": round(self._wall(), 3),
                        "leaf": self._tokenizer.leaf(job.leaf),
                        "hold": action.verdict.value,
                        "all_loaded_dead": self._all_loaded_dead(job),
                    },
                )
            elif isinstance(action, Done):
                self._act(job, action)

    def _start_probe(
        self,
        job: PostDialJudgeJob,
        action: StartProbe,
        events: queue.Queue[tuple[str, ProbeOutcome]],
        pending: dict[str, tuple[float, str]],
    ) -> None:
        stop = self._clock() + PROBE_QUEUE_S + float(action.request_timeout_s) + PROBE_SLACK_S
        pending[action.name] = (stop, action.leaf)
        self._emit("debug", probe_start_record(action, self._tokenizer, now_wall=self._wall()))

        def run() -> None:
            try:
                outcome = self._hooks.probe(job, action)
            except Exception:
                outcome = ProbeOutcome(ProbeClass.INDETERMINATE)
            if not isinstance(outcome, ProbeOutcome):
                outcome = ProbeOutcome(probe_class(outcome) or ProbeClass.INDETERMINATE)
            events.put((action.name, outcome))

        with self._lock:
            self.metrics["probes"] += 1
        self._spawn(PROBE_THREAD_NAME, run)

    def _judgement_constants(self) -> PostDialConstants:
        """Key post_dial_fast_successor: S_C0_DELAY_S = FAST_SUCCESSOR_S_C0_DELAY_S."""

        if not self.fast_successor_on:
            return self._c
        return replace(self._c, s_c0_delay_s=float(self._c.fast_successor_s_c0_delay_s))

    def _rung_judgement_constants(self, rung: Rung, witnessed: bool) -> PostDialConstants:
        """``rung_constants``; key post_dial_fast_successor then caps the
        rung's S_C0_DELAY_S at FAST_SUCCESSOR_S_C0_DELAY_S."""

        constants = rung_constants(rung, self._c, witnessed=witnessed)
        if not self.fast_successor_on:
            return constants
        fast = float(self._c.fast_successor_s_c0_delay_s)
        return replace(constants, s_c0_delay_s=min(float(constants.s_c0_delay_s), fast))

    def _act(self, job: PostDialJudgeJob, done: Done) -> None:
        """§18.7 ``act(v)``."""

        judgement = job.judgement
        assert judgement is not None
        with self._lock:
            self.gates.finish(job.leaf, done.verdict, done.tokens, self._clock())
            if done.verdict is Verdict.NOT_REPRODUCED:
                self._exonerations = True
            if done.verdict in HOLD_VERDICTS:
                self._hold_set_wall = self._wall()
            self.metrics[f"verdict_{None if done.verdict is None else done.verdict.value}"] += 1
        record = verdict_record(
            done,
            judgement,
            self._tokenizer,
            now_wall=self._wall(),
            mode="act",
            durable=job.durable,
        )
        hold = done.verdict in HOLD_VERDICTS
        if not hold:
            self._emit("info", record)
        try:
            self._act_on(job, done)
        finally:
            if hold:
                # §18.7: a hold logs whether every loaded member is Core-dead,
                # read after the ticket is settled so the read never delays it.
                record["all_loaded_dead"] = self._all_loaded_dead(job)
                self._emit("info", record)
            job.acted = True
            self._maybe_route_moved(job)

    def _all_loaded_dead(self, job: PostDialJudgeJob) -> bool | None:
        """One S4 + S3 re-read at a hold: every loaded member Core-dead?"""

        route = self._call(lambda: self._hooks.read_route(job), None)
        return route.all_loaded_dead() if isinstance(route, RouteRead) else None

    def _maybe_route_moved(self, job: PostDialJudgeJob) -> None:
        """Call ``route_moved`` once, when L's demotion was read back and the
        judgement has acted (its commit, if any, is over)."""

        if not job.demoted or not job.acted or job.route_moved_sent:
            return
        job.route_moved_sent = True
        hook = self._hooks.route_moved
        if hook is not None:
            self._call(lambda: hook(job), None)

    def _act_on(self, job: PostDialJudgeJob, done: Done) -> None:
        if done.verdict is Verdict.CONVICT_CONTRACT and done.commit_allowed:
            result = self._call(lambda: self._hooks.commit(job, done), None)
            if isinstance(result, tuple) and len(result) == 2:
                accepted, reason = bool(result[0]), str(result[1])
            else:
                # The commit settles its own ticket; a raising one did not.
                accepted, reason = False, "commit_error"
                self._call(lambda: self._hooks.finish(job, reason), None)
            job.settled = True
            self._emit(
                "info",
                {
                    "ev": "commit",
                    "t": round(self._wall(), 3),
                    "leaf": self._tokenizer.leaf(job.leaf),
                    "successor": self._tokenizer.leaf(done.successor),
                    "accepted": bool(accepted),
                    "reason": str(reason),
                    "durable": "on" if job.durable else "off",
                },
            )
            return
        if done.verdict is Verdict.CONVICT_CONTRACT:
            reason = "commit_late"
            self._emit(
                "info",
                {
                    "ev": "commit",
                    "t": round(self._wall(), 3),
                    "leaf": self._tokenizer.leaf(job.leaf),
                    "successor": self._tokenizer.leaf(done.successor),
                    "accepted": False,
                    "reason": reason,
                    "durable": "on" if job.durable else "off",
                },
            )
        else:
            reason = done.reason if done.verdict is None else done.verdict.value.lower()
        job.settled = True
        self._call(lambda: self._hooks.finish(job, reason), None)

    # -- the tier ladder (spec v1 §4.7, §4.10, §4.11) -------------------------
    #
    # ``_run_ladder`` replaces ``_run`` when ``job.ladder`` is set; the legacy
    # loop above is not shared.  Per rung k: (k >= 1) cap, fit, escalation
    # hook and the witness phase; then the A_C phase (its own queue), the
    # settled rung verdict and ``ladder_next``.  Holds are set only at the
    # ladder stop; ``gates.finish(L0, ...)`` runs once, with the step's
    # bookkeeping verdict.

    def _ladder_fields(self, job: PostDialJudgeJob, index: int) -> dict[str, Any]:
        ladder = job.ladder
        assert ladder is not None
        return {"ladder": ladder.ladder_id, "rung": int(index) + 1}

    def _gcode(self, ladder: PostDialLadder, name: str) -> str:
        return group_code(name, group_names=ladder.group_names)

    def _ttok(self, name: str | None) -> str:
        return token_field(self._tokenizer.leaf(name) if name else None)

    def _ladder_elapsed(self, ladder: PostDialLadder, now: float) -> float:
        start = ladder.admitted_at if ladder.admitted_at is not None else now
        return round(max(0.0, float(now) - float(start)), 3)

    def _ladder_refusal(self, job: PostDialJudgeJob) -> str | None:
        """``ladder_check`` before a probe unit or a commit, as an outcome
        (``authority_changed_<field>``, ``d4_txn_busy_<sub>``); a raising
        check refuses (``aborted``)."""

        hook = self._hooks.ladder_check
        if hook is None:
            return None
        try:
            reason = hook(job)
        except Exception:
            return OUTCOME_ABORTED
        return _outcome_text(reason) if reason else None

    @staticmethod
    def _ladder_job_view(job: PostDialJudgeJob, index: int) -> PostDialJudgeJob:
        """``job`` with its ladder read at rung ``index`` (a close watch's reads)."""

        view = copy.copy(job)
        assert job.ladder is not None
        view.ladder = job.ladder.view(index)
        return view

    def _run_ladder(self, job: PostDialJudgeJob) -> None:
        ladder = job.ladder
        assert ladder is not None
        end = _LadderEnd()
        ac_phases: dict[int, _LadderPhase] = {}
        try:
            self._ladder_admit(job)
            cause: EscalationCause | None = None
            count = len(ladder.rungs)
            k = 0
            while True:
                rung = ladder.rungs[k]
                passed: str | None = None
                if k >= 1:
                    opened = self._ladder_open_rung(job, k, cause)
                    if opened is not None:
                        self._ladder_stop(job, end, k, *opened)
                        break
                    step, wphase = self._ladder_witness_phase(job, rung, cause)
                    self._ladder_witness_verdict(job, wphase, step)
                    if step is None:
                        self._ladder_stop(job, end, k, str(wphase.refusal), None, Verdict.INDETERMINATE)
                        break
                    if step.action == WITNESS_ESCALATE:
                        cause = EscalationCause.EXHAUSTED
                        k += 1
                        continue
                    if step.action != WITNESS_ISSUE_AC:
                        if step.wasted:
                            with ladder.lock:
                                ladder.witness_wasted = True
                        bookkeeping = step.hold if step.hold is not None else Verdict.INDETERMINATE
                        self._ladder_stop(job, end, k, str(step.outcome), step.hold, bookkeeping)
                        break
                    passed = step.passed
                    with ladder.lock:
                        if passed is not None:
                            ladder.witness[k] = passed
                else:
                    with ladder.lock:
                        ladder.index = 0
                        ladder.rungs_run = 1
                    self._ladder_rung_start(job, rung, witnessed=False)
                phase = self._ladder_ac_phase(job, rung, passed)
                ac_phases[k] = phase
                if phase.refusal is not None:
                    self._ladder_emit_verdicts(
                        job,
                        phase,
                        RungVerdictResult(RungVerdict.INDETERMINATE, phase.refusal, None, None, None),
                        legacy=False,
                    )
                    self._ladder_stop(job, end, k, phase.refusal, None, Verdict.INDETERMINATE)
                    break
                rv = self._ladder_settled_verdict(job, phase)
                self._ladder_emit_verdicts(job, phase, rv)
                now = self._clock()
                upper = ladder.rungs[k + 1] if k + 1 < count else None
                done = phase.done
                step = ladder_next(
                    rv,
                    k,
                    count,
                    cause=cause,
                    deeper_moved=bool(ladder.deeper_moved),
                    fits_escalation=upper is not None and fits(now, ladder.deadline, escalation_need_s(upper)),
                    commit_allowed=bool(done is not None and done.commit_allowed),
                    accused_kind=rung.kind,
                )
                if (
                    self.successor_timeout_hold
                    and k == 0
                    and step.outcome == OUTCOME_INDETERMINATE
                    and done is not None
                    and successor_timeout_exhausts(rv.base, done.results)
                ):
                    # Fix 2 (10-28): rung 1 accused failed and S_C0 timed
                    # out: the EXHAUSTED hold instead of a 10 s re-trigger.
                    step = LadderStep(
                        step.action, OUTCOME_EXHAUSTED_HOLD, Verdict.EXHAUSTED,
                        step.closes, None, Verdict.EXHAUSTED,
                    )
                    with self._lock:
                        self.metrics["exhausted_hold_entered"] += 1
                    self._emit_hold_entered(job.leaf, "successor_timeout")
                if step.action == ACTION_ESCALATE:
                    cause = step.cause
                    k += 1
                    continue
                if step.action == ACTION_COMMIT:
                    self._ladder_commit(job, end, phase, step)
                    break
                if step.outcome == OUTCOME_COMMIT_LATE:
                    self._ladder_commit_record(job, phase, accepted=False, reason=OUTCOME_COMMIT_LATE)
                self._ladder_stop(job, end, k, str(step.outcome), step.hold, step.bookkeeping)
                if step.outcome == OUTCOME_RECOVERED_BY_DEEPER_MOVE and step.closes == CLOSES_AFTER_COMMIT:
                    first = ac_phases.get(0)
                    end.after = lambda: self._ladder_group_watch(
                        job, 0, None, None if first is None else first.judgement
                    )
                break
        except Exception as exc:  # pragma: no cover - defensive
            self._emit("warning", {"ev": "judge_failed", "error": type(exc).__name__})
        finally:
            self._ladder_finish(job, end)

    def _ladder_decide(
        self,
        end: "_LadderEnd",
        outcome: str,
        hold: Verdict | None,
        index: int,
        *,
        commit: str = "none",
    ) -> None:
        end.outcome = str(outcome)
        end.hold = hold
        end.index = int(index)
        end.commit = commit
        end.wall = self._wall()
        end.perf = self._clock()

    def _ladder_stop(
        self,
        job: PostDialJudgeJob,
        end: "_LadderEnd",
        index: int,
        outcome: str,
        hold: Verdict | None,
        bookkeeping: Verdict | None,
    ) -> None:
        """A ladder stop without a commit: bookkeeping, the hold (once), the ticket."""

        now = self._clock()
        with self._lock:
            self.gates.finish(job.leaf, bookkeeping, job.tokens, now)
            if hold is not None and bookkeeping is not hold:
                self.gates.set_hold(hold, now)
            applied = hold if hold is not None else (bookkeeping if bookkeeping in HOLD_VERDICTS else None)
            if applied is not None:
                self._hold_set_wall = self._wall()
            if bookkeeping is Verdict.NOT_REPRODUCED:
                self._exonerations = True
        self._ladder_decide(end, outcome, applied, index)
        if not job.settled:
            job.settled = True
            self._call(lambda: self._hooks.finish(job, outcome), None)

    def _ladder_finish(self, job: PostDialJudgeJob, end: "_LadderEnd") -> None:
        ladder = job.ladder
        assert ladder is not None
        if end.wall is None:
            self._ladder_decide(end, OUTCOME_ABORTED, None, ladder.index)
        with ladder.lock:
            ladder.outcome = end.outcome
            ladder.ended = True
        with self._lock:
            if self._ladder_job is job:
                self._ladder_job = None
            self.metrics[f"ladder_{end.outcome}"] += 1
        hook = self._hooks.ladder_end
        if hook is not None:
            self._call(lambda: hook(job, end.outcome), None)
        perf = end.perf if end.perf is not None else self._clock()
        self._emit(
            "info",
            {
                "ev": "ladder_end",
                "t": round(float(end.wall if end.wall is not None else self._wall()), 3),
                "ladder": ladder.ladder_id,
                "leaf": self._tokenizer.leaf(job.leaf),
                "outcome": end.outcome,
                "rung": end.index + 1,
                "rungs_run": ladder.rungs_run,
                "elapsed_s": self._ladder_elapsed(ladder, perf),
                "probes": ladder.probes,
                "witnesses": ladder.witnesses,
                "dead_marks": ladder.dead_marks,
                "hold": None if end.hold is None else end.hold.value,
                "commit": end.commit,
                "witness_wasted": 1 if ladder.witness_wasted else 0,
                "pf_probe": 1 if ladder.pf_probe else 0,
            },
        )
        if end.after is not None:
            self._call(end.after, None)
        job.acted = True
        self._maybe_route_moved(job)
        with self._lock:
            if self.gates.in_flight:
                self.gates.abandon(job.leaf)
            if self._worker is threading.current_thread():
                self._worker = None
                self._job = None
        if not job.settled:
            job.settled = True
            self._call(lambda: self._hooks.finish(job, "judge_aborted"), None)

    def _ladder_admit(self, job: PostDialJudgeJob) -> None:
        ladder = job.ladder
        assert ladder is not None
        with self._lock:
            self.metrics["ladders"] += 1
        self._emit(
            "info",
            {
                "ev": "ladder_admit",
                "t": round(self._wall(), 3),
                "ladder": ladder.ladder_id,
                "leaf": self._tokenizer.leaf(job.leaf),
                "rule": job.kind,
                "episode": job.episode,
                "rungs": len(ladder.rungs),
                "path": [self._gcode(ladder, name) for name in ladder.chain0],
                "acc": [self._ttok(rung.accused) for rung in ladder.rungs],
                "timeouts": [int(rung.timeout_ms) for rung in ladder.rungs],
                "budget_s": ladder_budget_s(ladder.rungs),
                "cap_s": LADDER_CAP_S,
                "lease": ladder.lease,
                "mode": "act",
            },
        )

    def _ladder_rung_start(self, job: PostDialJudgeJob, rung: Rung, *, witnessed: bool) -> None:
        ladder = job.ladder
        assert ladder is not None
        now = self._clock()
        self._emit(
            "info",
            {
                "ev": "rung_start",
                "t": round(self._wall(), 3),
                "ladder": ladder.ladder_id,
                "rung": rung.index + 1,
                "depth": rung.depth,
                "grp": self._gcode(ladder, rung.group),
                "gtok": self._ttok(rung.group),
                "acc": self._ttok(rung.accused),
                "akind": rung.kind.value,
                "timeout_ms": int(rung.timeout_ms),
                "ticket_s": rung_ticket_s(int(rung.timeout_ms), witnessed=witnessed),
                "witnessed": 1 if witnessed else 0,
                "fixed0": 1 if rung.fixed0 else 0,
                "remaining_s": round(ladder.deadline - now, 3),
            },
        )

    def _ladder_open_rung(
        self,
        job: PostDialJudgeJob,
        k: int,
        cause: EscalationCause | None,
    ) -> tuple[str, Verdict | None, Verdict | None] | None:
        """Escalation to rung ``k``: cap, F2, the escalation hook (refresh),
        ``note_rung``.  A stop ``(outcome, hold, bookkeeping)`` or None."""

        ladder = job.ladder
        assert ladder is not None
        rung = ladder.rungs[k]
        hold = legacy_hold(cause)
        inherited = hold if hold is not None else Verdict.INDETERMINATE
        key = rung_gate_key(rung.group, rung.accused)
        now = self._clock()
        with self._lock:
            blocked = self.gates.rung_blocked(key, now)
        if blocked is not None:
            return OUTCOME_LADDER_CAP, hold, inherited
        if not fits(now, ladder.deadline, escalation_need_s(rung)):
            return OUTCOME_LADDER_BUDGET_EXHAUSTED, hold, inherited
        with ladder.lock:
            ladder.index = k
            ladder.deeper_moved = False
        hook = self._hooks.ladder_escalate
        reason = None if hook is None else self._call(lambda: hook(job, k), OUTCOME_REFRESH_UNAVAILABLE)
        text = _outcome_text(reason) if reason else None
        now = self._clock()
        self._emit(
            "info",
            {
                "ev": "ladder_escalate",
                "t": round(self._wall(), 3),
                "ladder": ladder.ladder_id,
                "from_rung": k,
                "to_rung": k + 1,
                "cause": None if cause is None else cause.value,
                "elapsed_s": self._ladder_elapsed(ladder, now),
                "remaining_s": round(ladder.deadline - now, 3),
                "refresh": (
                    "unavailable"
                    if text == OUTCOME_REFRESH_UNAVAILABLE
                    else "moved" if text == OUTCOME_LADDER_ROUTE_MOVED else "ok"
                ),
                "deeper_moved": 1 if ladder.deeper_moved else 0,
            },
        )
        if text is not None:
            if text == OUTCOME_REFRESH_UNAVAILABLE:
                return text, hold, inherited
            return text, None, Verdict.INDETERMINATE
        with self._lock:
            self.gates.note_rung(key, now)
        with ladder.lock:
            ladder.rungs_run = max(ladder.rungs_run, k + 1)
        self._ladder_rung_start(job, rung, witnessed=True)
        return None

    def _ladder_witness_phase(
        self,
        job: PostDialJudgeJob,
        rung: Rung,
        cause: EscalationCause | None,
    ) -> tuple[Any, _LadderPhase]:
        """§3.6: witnesses one at a time in Mihomo's order at rung k's
        contract, before any A_C there.  ``(None, phase)`` on a refusal."""

        ladder = job.ladder
        assert ladder is not None and cause is not None
        k = rung.index
        timeout = int(rung.timeout_ms)
        alive, unknown = ladder.rung_state(k)
        order = rung_order(rung, alive or {}, unknown=unknown)
        phase = _LadderPhase(rung, witness=True)
        phase.alive = alive
        phase.unknown = unknown
        results: dict[str, ProbeClass] = phase.wresults
        upper = ladder.rungs[k + 1] if k + 1 < len(ladder.rungs) else None
        issued = 0
        while True:
            now = self._clock()
            step = witness_next(
                cause,
                order,
                results,
                cap=witness_cap(rung),
                fits_next=fits(now, ladder.deadline, witness_fit_need_s(timeout)),
                fits_ac=fits(now, ladder.deadline, ac_after_witness_need_s(timeout)),
                rung_is_last=upper is None,
                fits_escalation=upper is not None and fits(now, ladder.deadline, escalation_need_s(upper)),
                complete_if_exhausted=complete(
                    rung,
                    failed_in_ladder=ladder.failed_at(k),
                    read_alive=alive,
                    read_unknown=unknown,
                ),
            )
            if step.action != WITNESS_PROBE:
                return step, phase
            target = str(step.target)
            action = StartProbe(
                name=witness_name(issued),
                leaf=target,
                timeout_ms=timeout,
                request_timeout_s=probe_request_window_s(timeout),
                url=rung.url,
                expected=rung.expected,
                group=rung.group,
                admission_s=LADDER_PROBE_ADMISSION_S,
                await_s=witness_s(timeout),
            )
            issued += 1
            self._ladder_issue(job, phase, action)
            if phase.refusal is not None:
                return None, phase
            outcome = self._ladder_await(phase, action.name, ladder.deadline)
            value = outcome.probe if outcome is not None else ProbeClass.INDETERMINATE
            results[target] = value
            self._ladder_note_result(job, phase, action.name, target, value)
            self._emit(
                "info",
                {
                    "ev": "ladder_witness",
                    "t": round(self._wall(), 3),
                    "ladder": ladder.ladder_id,
                    "rung": k + 1,
                    "name": action.name,
                    "leaf": self._tokenizer.leaf(target),
                    "class": value.value,
                    "ms": None if outcome is None or outcome.elapsed_ms is None else round(float(outcome.elapsed_ms), 1),
                    "queue_wait_ms": (
                        None
                        if outcome is None or outcome.queue_wait_ms is None
                        else round(float(outcome.queue_wait_ms), 1)
                    ),
                    "core_alive": 1 if (alive or {}).get(target) else 0,
                },
            )

    def _ladder_await(self, phase: _LadderPhase, name: str, deadline: float) -> ProbeOutcome | None:
        """One witness result from this phase's queue, until its await bound."""

        stop = min(phase.pending.get(name, (0.0, ""))[0], float(deadline))
        while True:
            timeout = stop - self._clock()
            if timeout <= 0.0:
                return None
            try:
                got, outcome = phase.events.get(timeout=timeout)
            except queue.Empty:
                return None
            phase.pending.pop(got, None)
            if got == name:
                return outcome

    def _ladder_witness_verdict(self, job: PostDialJudgeJob, phase: _LadderPhase, step: Any) -> None:
        """``rung_verdict`` for a rung the witness phase ended (stop or escalate)."""

        if step is not None and step.action == WITNESS_ISSUE_AC:
            return
        ladder = job.ladder
        assert ladder is not None
        rung = phase.rung
        if step is None:
            value, reason = RungVerdict.INDETERMINATE, str(phase.refusal)
        elif step.action == WITNESS_ESCALATE:
            value, reason = RungVerdict.EXHAUSTED_COMPLETE, "witnesses_failed"
        else:
            reason = "witness_" + str(step.outcome)
            if step.hold is Verdict.FACILITY:
                value = RungVerdict.FACILITY
            elif step.hold is Verdict.EXHAUSTED:
                whole = complete(
                    rung,
                    failed_in_ladder=ladder.failed_at(rung.index),
                    read_alive=phase.alive,
                    read_unknown=phase.unknown,
                )
                value = RungVerdict.EXHAUSTED_COMPLETE if whole else RungVerdict.EXHAUSTED_PARTIAL
            else:
                value = RungVerdict.INDETERMINATE
        names = {target: f"W{index}" for index, target in enumerate(phase.wresults)}
        self._emit(
            "info",
            {
                "ev": "rung_verdict",
                "t": round(self._wall(), 3),
                "ladder": ladder.ladder_id,
                "rung": rung.index + 1,
                "grp": self._gcode(ladder, rung.group),
                "acc": self._ttok(rung.accused),
                "verdict": value.value,
                "reason": reason,
                "successor": None,
                "results": [f"{names[target]}:{cls.value}" for target, cls in phase.wresults.items()],
                "commit_allowed": False,
                "durable": "on" if job.durable else "off",
                "skipped": [],
            },
        )

    def _ladder_ac_phase(self, job: PostDialJudgeJob, rung: Rung, passed: str | None) -> _LadderPhase:
        """Rung k's ``Judgement`` (A_C, successors) on its own queue, settled:
        after ``Done`` the pending probes are awaited to their bounds."""

        ladder = job.ladder
        assert ladder is not None
        timeout = int(rung.timeout_ms)
        witnessed = passed is not None
        now = self._clock()
        ticket_end = min(ladder.deadline, now + rung_ticket_s(timeout, witnessed=witnessed))
        judgement = Judgement(
            rung.accused,
            kind=job.kind,
            tokens=job.tokens,
            ticket_deadline_perf=ticket_end,
            constants=self._rung_judgement_constants(rung, witnessed),
            successor_cap=None if witnessed else rung_successor_cap(rung, self._c),
            preset_successor=passed,
            contract=(rung.group, rung.url, rung.expected),
            admission_s=LADDER_PROBE_ADMISSION_S,
            await_s=probe_await_s(timeout),
            provisional_move_s=(
                float(self._c.provisional_move_s)
                if self.provisional_move_on and int(rung.index) == 0 and not witnessed
                else None
            ),
            history_candidate=bool(self.successor_history_on),
        )
        phase = _LadderPhase(rung, judgement)
        job.judgement = judgement
        with self._lock:
            actions = judgement.start(self._clock(), self._wall())
        self._dispatch_ladder(job, phase, actions)
        if not witnessed and phase.refusal is None:
            # Rung read (§4.8), concurrently with A_C on its own thread.
            route = self._call(lambda: self._hooks.read_route(job), None)
            members = frozenset(rung.members)
            with self._lock:
                now = self._clock()
                if isinstance(route, RouteRead):
                    phase.read = route
                    order = tuple(name for name in route.judge_order if name in members)
                    actions = judgement.on_route_state(order, route.alive, now)
                else:
                    actions = judgement.on_route_state_unavailable(now)
            self._dispatch_ladder(job, phase, actions)
        while phase.refusal is None:
            with self._lock:
                done = judgement.done
                wakeup = judgement.next_wakeup()
            if done is not None and not phase.pending:
                break
            now = self._clock()
            if done is not None:
                timeout_s = min(max(stop for stop, _leaf in phase.pending.values()), ladder.deadline) - now
                if timeout_s <= 0.0:
                    break
            elif wakeup is None:
                timeout_s = max(0.0, ticket_end - now)
            else:
                timeout_s = max(0.0, wakeup - now)
            try:
                name, outcome = phase.events.get(timeout=timeout_s)
            except queue.Empty:
                if done is not None:
                    break
                with self._lock:
                    actions = judgement.on_timer(self._clock())
                self._dispatch_ladder(job, phase, actions)
                continue
            _stop, probe_leaf = phase.pending.pop(name, (0.0, rung.accused))
            self._ladder_result_record(job, rung, name, probe_leaf, outcome)
            self._ladder_note_result(job, phase, name, probe_leaf, outcome.probe)
            with self._lock:
                actions = judgement.on_probe_result(name, outcome.probe, self._clock())
            self._dispatch_ladder(job, phase, actions)
        return phase

    def _ladder_result_record(
        self,
        job: PostDialJudgeJob,
        rung: Rung,
        name: str,
        leaf: str,
        outcome: ProbeOutcome,
    ) -> None:
        ladder = job.ladder
        assert ladder is not None
        if rung.index == 0:
            record = probe_result_record(
                name,
                leaf,
                outcome.probe,
                self._tokenizer,
                now_wall=self._wall(),
                elapsed_ms=outcome.elapsed_ms,
                queue_wait_ms=outcome.queue_wait_ms,
            )
            record.update(self._ladder_fields(job, rung.index))
        else:
            value = probe_class(outcome.probe)
            record = {
                "ev": "rung_probe_result",
                "t": round(self._wall(), 3),
                "ladder": ladder.ladder_id,
                "rung": rung.index + 1,
                "name": name,
                "leaf": self._tokenizer.leaf(leaf),
                "class": None if value is None else value.value,
                "ms": None if outcome.elapsed_ms is None else round(float(outcome.elapsed_ms), 1),
                "queue_wait_ms": None if outcome.queue_wait_ms is None else round(float(outcome.queue_wait_ms), 1),
            }
        self._emit("info", record)

    def _ladder_note_result(
        self,
        job: PostDialJudgeJob,
        phase: _LadderPhase,
        name: str,
        target: str,
        value: ProbeClass | None,
    ) -> None:
        """The ladder's per-rung sets and counters for one probe result."""

        ladder = job.ladder
        assert ladder is not None
        index = phase.rung.index
        accused = name == "A_C"
        with ladder.lock:
            if value in LADDER_FAILED:
                ladder.dead_marks += 1
                ladder.failed.setdefault(index, set()).add(str(target))
            if value is ProbeClass.PASS and not accused:
                ladder.passed.setdefault(index, set()).add(str(target))
            if value is ProbeClass.TIMEOUT and not accused:
                ladder.timed_out[index] = True
            if phase.rung.group == PROXY_FALLBACK:
                ladder.pf_probe_resolved_at = self._clock()
        if value is ProbeClass.HTTP_ANSWER:
            with self._lock:
                self._status_failures[str(target)] = self._wall()

    def _dispatch_ladder(self, job: PostDialJudgeJob, phase: _LadderPhase, actions: Iterable[Any]) -> None:
        """``_dispatch`` for one ladder phase (critic B4): ``Done`` and
        ``LateHold`` are stored (no act, no hold), a group accused's failure
        defers its watch, every probe passes ``ladder_check`` first."""

        judgement = phase.judgement
        assert judgement is not None
        ordered = sorted(actions, key=lambda item: 0 if isinstance(item, StartProbe) else 1)
        for action in ordered:
            if isinstance(action, Done):
                phase.done = action
                continue
            if phase.refusal is not None:
                continue
            if isinstance(action, StartProbe):
                self._ladder_issue(job, phase, action)
            elif isinstance(action, NeedReadback):
                entry = self._call(lambda: self._hooks.read_leaf(job, action.leaf), None)
                timed_out = (
                    None
                    if not isinstance(entry, Mapping)
                    else readback_shows_accused_failure(entry, phase.rung.url, action.accused_issued_wall)
                )
                record = {
                    "ev": "readback",
                    "t": round(self._wall(), 3),
                    "leaf": self._tokenizer.leaf(action.leaf),
                    "accused_timeout": timed_out,
                }
                record.update(self._ladder_fields(job, phase.rung.index))
                self._emit("info", record)
                with self._lock:
                    more = judgement.on_readback(timed_out, self._clock())
                self._dispatch_ladder(job, phase, more)
            elif isinstance(action, AccusedFailed):
                self._ladder_accused_failed(job, phase, action)
            elif isinstance(action, LateHold):
                phase.late = action.verdict
            elif isinstance(action, ProvisionalMove):
                self._ladder_provisional(job, phase, action)

    def _ladder_provisional(self, job: PostDialJudgeJob, phase: _LadderPhase, action: ProvisionalMove) -> None:
        """Key post_dial_provisional_move (10-28): one provisional route move
        per judgement, rung 1 only, before A_C has a result.  The history
        basis needs a fresh alive provider history first.  ``ladder_check``
        must pass; the hook writes the route-lease pin and re-baselines the
        ladder authority.  No verdict, no dead write, no close."""

        judgement = phase.judgement
        record: dict[str, Any] = {
            "ev": "provisional_move",
            "t": round(self._wall(), 3),
            "leaf": self._tokenizer.leaf(job.leaf),
            "successor": self._tokenizer.leaf(action.successor),
            "basis": str(action.basis),
        }
        record.update(self._ladder_fields(job, phase.rung.index))
        reason: str | None = None
        if job.provisional is not None or int(phase.rung.index) != 0 or phase.witness:
            reason = "not_eligible"
        elif judgement is None or judgement.done is not None:
            reason = "decided"
        elif action.basis == "history":
            hook = self._hooks.successor_history
            fresh = (
                bool(self._call(lambda: hook(job, action.successor), False))
                if self.successor_history_on and hook is not None
                else False
            )
            if not fresh:
                reason = "history_not_fresh"
        if reason is None:
            reason = self._ladder_refusal(job)
        moved = False
        if reason is None:
            hook = self._hooks.provisional_move
            result = self._call(lambda: hook(job, action.successor), None) if hook is not None else None
            if isinstance(result, tuple) and len(result) == 2:
                moved, reason = bool(result[0]), str(result[1])
            else:
                moved, reason = False, "hook_error"
        if moved:
            job.provisional = str(action.successor)
            with self._lock:
                if judgement is not None:
                    judgement.note_provisional_moved()
                self.metrics["provisional_moves"] += 1
        else:
            with self._lock:
                self.metrics["provisional_skipped"] += 1
        record["moved"] = bool(moved)
        record["reason"] = str(reason)
        # Stamped when the hook has returned (after the history read, the
        # check, the lock wait and the write): ``t_start`` is the decision.
        record["t_start"] = record["t"]
        record["t"] = round(self._wall(), 3)
        self._emit("info", record)

    def _ladder_issue(self, job: PostDialJudgeJob, phase: _LadderPhase, action: StartProbe) -> None:
        """One ladder probe unit: the off-path guard, ``ladder_check``, start."""

        ladder = job.ladder
        assert ladder is not None
        if not ladder.on_path(action.group):
            # Design §19.1: never probe a group's contract off the ladder's
            # rungs.  The probe reads indeterminate; nothing is sent.
            await_s = 0.0 if action.await_s is None else float(action.await_s)
            phase.pending[action.name] = (self._clock() + await_s, action.leaf)
            with self._lock:
                self.metrics["ladder_probe_refused"] += 1
            self._emit(
                "info",
                {
                    "ev": "probe_refused",
                    "t": round(self._wall(), 3),
                    "ladder": ladder.ladder_id,
                    "name": action.name,
                    "reason": "off_path",
                },
            )
            phase.events.put((action.name, ProbeOutcome(ProbeClass.INDETERMINATE)))
            return
        refusal = self._ladder_refusal(job)
        if refusal is not None:
            # Recorded as never started; the phase settles INDETERMINATE.
            phase.refusal = refusal
            return
        self._start_ladder_probe(job, phase, action)

    def _start_ladder_probe(self, job: PostDialJudgeJob, phase: _LadderPhase, action: StartProbe) -> None:
        ladder = job.ladder
        assert ladder is not None
        issue = self._clock()
        if action.await_s is not None:
            await_s = float(action.await_s)
        else:
            await_s = PROBE_QUEUE_S + float(action.request_timeout_s) + PROBE_SLACK_S
        phase.pending[action.name] = (issue + await_s, action.leaf)
        with ladder.lock:
            ladder.probes += 1
            if phase.witness:
                ladder.witnesses += 1
            if action.group == PROXY_FALLBACK:
                ladder.pf_probe = True
        record = probe_start_record(action, self._tokenizer, now_wall=self._wall())
        record.update(self._ladder_fields(job, phase.rung.index))
        self._emit("debug", record)
        events = phase.events

        def run() -> None:
            try:
                outcome = self._hooks.probe(job, action)
            except Exception:
                outcome = ProbeOutcome(ProbeClass.INDETERMINATE)
            if not isinstance(outcome, ProbeOutcome):
                outcome = ProbeOutcome(probe_class(outcome) or ProbeClass.INDETERMINATE)
            # Into the queue of the phase that started it, never a later one.
            events.put((action.name, outcome))

        with self._lock:
            self.metrics["probes"] += 1
        self._spawn(PROBE_THREAD_NAME, run)

    def _ladder_settled_verdict(self, job: PostDialJudgeJob, phase: _LadderPhase) -> RungVerdictResult:
        """§3.5: the settled rung verdict, with the rung read's alive map (or
        the escalation refresh's) and this ladder's failures."""

        ladder = job.ladder
        judgement = phase.judgement
        assert ladder is not None and judgement is not None
        done = phase.done
        if done is None:
            return RungVerdictResult(RungVerdict.INDETERMINATE, "no_verdict", None, None, None)
        if done.verdict is None:
            # ``non_member``: the accused is not the group's to judge.
            return RungVerdictResult(RungVerdict.INDETERMINATE, done.reason, None, None, None)
        rung = phase.rung
        if phase.read is not None:
            read_alive: Mapping[str, bool] | None = dict(phase.read.alive)
            unknown: frozenset[str] = phase.read.unknown
        else:
            read_alive, unknown = ladder.rung_state(rung.index)
        with self._lock:
            results = judgement.results
            started = judgement.started_successors
            cap = judgement.cap
            successors = judgement.successors
        return rung_verdict(
            results,
            started,
            cap,
            successors,
            rung=rung,
            read_alive=read_alive,
            read_unknown=unknown,
            failed_in_ladder=ladder.failed_at(rung.index),
        )

    def _ladder_emit_verdicts(
        self,
        job: PostDialJudgeJob,
        phase: _LadderPhase,
        rv: RungVerdictResult,
        *,
        legacy: bool = True,
    ) -> None:
        """Rung ①'s legacy ``verdict`` (+ ``ladder rung rverdict``) and
        ``rung_verdict`` for every rung."""

        ladder = job.ladder
        judgement = phase.judgement
        assert ladder is not None and judgement is not None
        rung = phase.rung
        done = phase.done
        if legacy and rung.index == 0 and done is not None:
            with self._lock:
                self.metrics[f"verdict_{None if done.verdict is None else done.verdict.value}"] += 1
            record = verdict_record(
                done,
                judgement,
                self._tokenizer,
                now_wall=self._wall(),
                mode="act",
                durable=job.durable,
            )
            record.update(self._ladder_fields(job, rung.index))
            record["rverdict"] = rv.verdict.value
            self._emit("info", record)
        with self._lock:
            results = judgement.results
            skipped = list(judgement.skipped)
        self._emit(
            "info",
            {
                "ev": "rung_verdict",
                "t": round(self._wall(), 3),
                "ladder": ladder.ladder_id,
                "rung": rung.index + 1,
                "grp": self._gcode(ladder, rung.group),
                "acc": self._ttok(rung.accused),
                "verdict": rv.verdict.value,
                "reason": rv.reason,
                "successor": self._tokenizer.leaf(rv.successor),
                "results": [f"{name}:{value.value}" for name, value in results.items()],
                "commit_allowed": bool(done is not None and done.commit_allowed),
                "durable": "on" if job.durable else "off",
                "skipped": skipped,
            },
        )

    def _ladder_commit(self, job: PostDialJudgeJob, end: "_LadderEnd", phase: _LadderPhase, step: Any) -> None:
        """CONVICT inside the rung ticket: ``ladder_check``, bookkeeping, the
        runtime's rung commit, then (group rungs) the close watch."""

        ladder = job.ladder
        done = phase.done
        assert ladder is not None and done is not None
        rung = phase.rung
        refusal = self._ladder_refusal(job)
        if refusal is not None:
            self._ladder_stop(job, end, rung.index, refusal, None, Verdict.CONVICT_CONTRACT)
            return
        with self._lock:
            self.gates.finish(job.leaf, Verdict.CONVICT_CONTRACT, done.tokens, self._clock())
        result = self._call(lambda: self._hooks.commit(job, done), None)
        if isinstance(result, tuple) and len(result) == 2:
            accepted, reason = bool(result[0]), str(result[1])
        else:
            # The commit settles its own ticket; a raising one did not.
            accepted, reason = False, "commit_error"
            self._call(lambda: self._hooks.finish(job, reason), None)
        job.settled = True
        self._ladder_commit_record(job, phase, accepted=accepted, reason=reason)
        if accepted:
            outcome = OUTCOME_CONVICTED
        elif reason in {OUTCOME_COMMIT_LATE, OUTCOME_MOVED_UNPROVEN}:
            outcome = reason
        else:
            outcome = OUTCOME_COMMIT_REJECTED
        self._ladder_decide(end, outcome, None, rung.index, commit="accepted" if accepted else "rejected")
        if accepted and step.closes == CLOSES_AFTER_COMMIT:
            with ladder.lock:
                pick = ladder.commit_pick
            self._ladder_group_watch(job, rung.index, pick or done.successor, phase.judgement)

    def _ladder_commit_record(self, job: PostDialJudgeJob, phase: _LadderPhase, *, accepted: bool, reason: str) -> None:
        ladder = job.ladder
        done = phase.done
        assert ladder is not None
        rung = phase.rung
        with ladder.lock:
            pick = ladder.commit_pick if accepted or reason != OUTCOME_COMMIT_LATE else None
            proven = 1 if ladder.commit_proven and pick is not None else 0
            sacrificial = 1 if ladder.commit_sacrificial else 0
        successor = None if done is None else done.successor
        fixed0 = 1 if rung.fixed0 else 0
        if rung.index == 0:
            record: dict[str, Any] = {
                "ev": "commit",
                "t": round(self._wall(), 3),
                "leaf": self._tokenizer.leaf(job.leaf),
                "successor": self._tokenizer.leaf(successor),
                "accepted": bool(accepted),
                "reason": str(reason),
                "durable": "on" if job.durable else "off",
            }
            record.update(self._ladder_fields(job, rung.index))
            record.update(pick=self._ttok(pick), proven=proven, sacrificial=sacrificial, fixed0=fixed0)
        else:
            record = {
                "ev": "rung_commit",
                "t": round(self._wall(), 3),
                "ladder": ladder.ladder_id,
                "rung": rung.index + 1,
                "grp": self._gcode(ladder, rung.group),
                "acc": self._ttok(rung.accused),
                "pick": self._ttok(pick),
                "proven": proven,
                "accepted": bool(accepted),
                "reason": str(reason),
                "sacrificial": sacrificial,
                "fixed0": fixed0,
                "durable": "on" if job.durable else "off",
            }
        self._emit("info", record)

    # -- ladder demotion, watches and closes (§4.7 "Closes") -----------------

    def _ladder_accused_failed(self, job: PostDialJudgeJob, phase: _LadderPhase, action: AccusedFailed) -> None:
        ladder = job.ladder
        assert ladder is not None
        rung = phase.rung
        with ladder.lock:
            # Set before the read below: a read issued from now on consumes
            # a pinned accused's drop (§4.8).
            ladder.accused_failed_known[rung.index] = True
        if rung.index == 0 and rung.kind is RungKind.LEAF:
            self._ladder_leaf_accused_failed(job, phase, action)
            return
        # A group accused: Mihomo's move is read at the commit (§4.9); no
        # watch before an accepted commit.
        self._emit(
            "info",
            {
                "ev": "demotion_deferred",
                "t": round(self._wall(), 3),
                "ladder": ladder.ladder_id,
                "rung": rung.index + 1,
                "grp": self._gcode(ladder, rung.group),
                "acc": self._ttok(rung.accused),
                "probe": action.probe.value,
            },
        )

    def _ladder_leaf_accused_failed(self, job: PostDialJudgeJob, phase: _LadderPhase, action: AccusedFailed) -> None:
        """Rung ① leaf: today's ``_on_accused_failed`` (read, demotion, watch,
        closes), read at rung ① whatever rung the ladder reaches later."""

        view = self._ladder_job_view(job, 0)
        fields = self._ladder_fields(job, 0)
        route = self._call(lambda: self._hooks.read_route(view), None)
        t_demote_wall = self._wall()
        confirmed = isinstance(route, RouteRead) and route.demoted(job.leaf)
        record = {
            "ev": "demotion" if confirmed else "demotion_not_visible",
            "t": round(t_demote_wall, 3),
            "leaf": self._tokenizer.leaf(job.leaf),
            "probe": action.probe.value,
            "late": bool(action.late),
            "readback": "ok" if isinstance(route, RouteRead) else "unavailable",
        }
        record.update(fields)
        self._emit("info", record)
        if confirmed:
            job.demoted = True
            self._maybe_route_moved(job)
        if not confirmed or not self._call(self._hooks.close_enabled, False):
            return
        info = _LadderWatch("leaf", phase.rung, None, phase.judgement, False, fields)
        self._ladder_open_watch(job, view, info, t_demote_wall, route)

    def _ladder_group_watch(
        self,
        job: PostDialJudgeJob,
        index: int,
        pick: str | None,
        judgement: Judgement | None,
    ) -> None:
        """A group rung's watch on L0, only after an accepted commit (or a
        deeper move): ``rung_demoted`` at that rung drives it."""

        ladder = job.ladder
        assert ladder is not None
        if not self._call(self._hooks.close_enabled, False):
            return
        with self._lock:
            existing = self._watches.get(job.leaf)
            if (
                pick is None
                and existing is not None
                and existing.ladder is not None
                and existing.ladder.fields.get("ladder") == ladder.ladder_id
            ):
                # ``recovered_by_deeper_move``: rung ①'s leaf watch of this
                # ladder still runs.  After an accepted group-rung commit the
                # committed rung's watch replaces it (the observer keeps one
                # watch per leaf; rung ① exhausted, so its own rule no longer
                # shows L demoted).
                return
        with ladder.lock:
            timed_out = bool(ladder.timed_out.get(int(index)))
        rung = ladder.rungs[int(index)]
        info = _LadderWatch("group", rung, pick, judgement, timed_out, self._ladder_fields(job, index))
        self._ladder_open_watch(job, self._ladder_job_view(job, index), info, self._wall(), None)

    def _ladder_open_watch(
        self,
        job: PostDialJudgeJob,
        view: PostDialJudgeJob,
        info: _LadderWatch,
        t_demote_wall: float,
        route: RouteRead | None,
    ) -> None:
        runner = job.runner
        start_watch = getattr(runner, "start_demotion_watch", None)
        if not callable(start_watch):
            return
        if self._call(lambda: start_watch(job.leaf, t_demote_wall=t_demote_wall), None) is None:
            return
        record = _WatchRecord(view, runner, t_demote_wall, self._clock())
        record.ladder = info
        with self._lock:
            self._prune_watches()
            self._watches[job.leaf] = record
            self.metrics["watches"] += 1
        stuck_now = getattr(runner, "stuck_now", None)
        stuck = (
            self._call(lambda: stuck_now(job.leaf, t_demote_wall=t_demote_wall, max_age_s=1.0), None)
            if callable(stuck_now)
            else None
        )
        if not isinstance(stuck, StuckConnections) or not stuck.ids:
            return
        with self._lock:
            if job.leaf in self._closing:
                return
            self._closing.add(job.leaf)
        self._spawn(CLOSE_THREAD_NAME, self._ladder_close_worker, record, stuck, route)

    # -- demotion, watch and closes (§5.4 step 3, §6) ------------------------

    def _on_accused_failed(self, job: PostDialJudgeJob, action: AccusedFailed) -> None:
        route = self._call(lambda: self._hooks.read_route(job), None)
        t_demote_wall = self._wall()
        confirmed = isinstance(route, RouteRead) and route.demoted(job.leaf)
        self._emit(
            "info",
            {
                "ev": "demotion" if confirmed else "demotion_not_visible",
                "t": round(t_demote_wall, 3),
                "leaf": self._tokenizer.leaf(job.leaf),
                "probe": action.probe.value,
                "late": bool(action.late),
                "readback": "ok" if isinstance(route, RouteRead) else "unavailable",
            },
        )
        if confirmed:
            job.demoted = True
            self._maybe_route_moved(job)
        if not confirmed or not self._call(self._hooks.close_enabled, False):
            return
        runner = job.runner
        start_watch = getattr(runner, "start_demotion_watch", None)
        if not callable(start_watch):
            return
        if self._call(lambda: start_watch(job.leaf, t_demote_wall=t_demote_wall), None) is None:
            return
        record = _WatchRecord(job, runner, t_demote_wall, self._clock())
        with self._lock:
            self._prune_watches()
            self._watches[job.leaf] = record
            self.metrics["watches"] += 1
        stuck_now = getattr(runner, "stuck_now", None)
        stuck = (
            self._call(lambda: stuck_now(job.leaf, t_demote_wall=t_demote_wall, max_age_s=1.0), None)
            if callable(stuck_now)
            else None
        )
        if not isinstance(stuck, StuckConnections) or not stuck.ids:
            # The observer's next poll (woken by the watch) takes it from here.
            return
        with self._lock:
            if job.leaf in self._closing:
                return
            self._closing.add(job.leaf)
        # The close runs to completion on its own thread, never on this worker.
        self._spawn(CLOSE_THREAD_NAME, self._close_worker, record, stuck, route)

    def _close_worker(
        self,
        record: _WatchRecord,
        stuck: StuckConnections,
        route: RouteRead | None = None,
    ) -> None:
        try:
            self._consider_close(record, stuck, route=route)
        finally:
            with self._lock:
                self._closing.discard(record.job.leaf)

    def _close_decision(
        self,
        record: _WatchRecord,
        stuck: StuckConnections,
        *,
        route: RouteRead | None,
    ) -> CloseDecision:
        with self._lock:
            judgement = record.job.judgement
            pending = bool(judgement is not None and judgement.successor_probe_pending)
            timed_out = bool(judgement is not None and judgement.successor_timed_out)
        confirmed = None if route is None else route.demoted(record.job.leaf)
        age = None if route is None else max(0.0, self._clock() - route.read_at)
        return select_stuck_closes(
            stuck,
            enabled=bool(self._call(self._hooks.close_enabled, False)),
            successor_probe_pending=pending,
            successor_timed_out=timed_out,
            demotion_confirmed=confirmed,
            readback_age_s=age,
            constants=self._c,
        )

    def _consider_close(
        self,
        record: _WatchRecord,
        stuck: StuckConnections,
        *,
        route: RouteRead | None,
    ) -> None:
        decision = self._close_decision(record, stuck, route=route)
        if decision.action == CLOSE_NEED_READBACK:
            # §6 rule 3: L Core-dead and now ≠ L, read back ≤1 s before the batch.
            fresh = self._call(lambda: self._hooks.read_route(record.job), None)
            if not isinstance(fresh, RouteRead):
                self._emit(
                    "info",
                    {
                        "ev": "close",
                        "t": round(self._wall(), 3),
                        "leaf": self._tokenizer.leaf(record.job.leaf),
                        "action": "readback_unavailable",
                        "count": 0,
                    },
                )
                return
            decision = self._close_decision(record, stuck, route=fresh)
        if decision.action == CLOSE:
            result = self._call(lambda: self._hooks.close(record.job, record.job.leaf, decision.ids), ())
            closed = _closed_ids(decision.ids, result)
            mark = getattr(record.runner, "mark_closed", None)
            if closed and callable(mark):
                # Only DELETEs that succeeded: the rest stay eligible (§18.7
                # ``already_closed``) and the next watch step retries them.
                self._call(lambda: mark(closed), None)
            with self._lock:
                self.metrics["closed_ids"] += len(closed)
            if not closed:
                decision = CloseDecision(
                    "close_failed",
                    (),
                    "cleanup_unavailable",
                    kept=decision.kept,
                )
            elif len(closed) < len(decision.ids):
                aged = set(decision.ids[: decision.aged])
                aged_closed = sum(1 for item in closed if item in aged)
                decision = CloseDecision(
                    CLOSE,
                    closed,
                    "partially_closed",
                    aged=aged_closed,
                    waiting=len(closed) - aged_closed,
                    kept=decision.kept,
                )
        self._settle_close(record, decision)

    def _settle_close(self, record: _WatchRecord, decision: CloseDecision) -> None:
        leaf = record.job.leaf
        if decision.action == CLOSE_NONE:
            return
        if decision.action == CLOSE_DEFER:
            if record.defer_logged:
                return
            record.defer_logged = True
            self._emit("debug", close_record(decision, leaf, self._tokenizer, now_wall=self._wall()))
            return
        if decision.action == CLOSE_NOT_DEMOTED:
            # This batch's readback does not show L demoted (L revived, or
            # every member is dead and ``now`` fell back to L): close nothing
            # now, keep the watch, and let the next 1 Hz step re-check until
            # it expires (§18.7: a failed confirm skips only this batch).
            level = "debug" if record.not_demoted_logged else "info"
            record.not_demoted_logged = True
            self._emit(level, close_record(decision, leaf, self._tokenizer, now_wall=self._wall()))
            return
        if decision.action == "close_failed":
            # Nothing closed (the next watch step retries): info once per watch.
            level = "debug" if record.failed_logged else "info"
            record.failed_logged = True
            self._emit(level, close_record(decision, leaf, self._tokenizer, now_wall=self._wall()))
            return
        if decision.action in {CLOSE_KEEP_ALL, CLOSE_DISABLED}:
            # Nothing on this leaf will be closed in this watch any more: a
            # successor timed out (facility) or closes are off.  End the
            # watch and its 1 Hz polls.
            with self._lock:
                if self._watches.get(leaf) is record:
                    del self._watches[leaf]
            end = getattr(record.runner, "end_demotion_watch", None)
            if callable(end):
                self._call(lambda: end(leaf), None)
        self._emit("info", close_record(decision, leaf, self._tokenizer, now_wall=self._wall()))

    def _prune_watches(self) -> None:
        now = self._clock()
        for leaf in [
            leaf
            for leaf, record in self._watches.items()
            if now - record.created_at > WATCH_RECORD_TTL_S
        ]:
            del self._watches[leaf]

    # -- ladder closes: today's close rules, with the ladder's demotion check
    # and the ``ladder rung`` fields appended to every record ---------------

    def _ladder_watch_step(self, record: _WatchRecord, stuck: StuckConnections) -> None:
        leaf = record.job.leaf
        pre = self._ladder_close_decision(record, stuck, route=None)
        if pre.action != CLOSE_NEED_READBACK:
            self._ladder_settle_close(record, pre)
            return
        with self._lock:
            if leaf in self._closing:
                return
            self._closing.add(leaf)
        self._spawn(CLOSE_THREAD_NAME, self._ladder_close_worker, record, stuck)

    def _ladder_close_worker(
        self,
        record: _WatchRecord,
        stuck: StuckConnections,
        route: RouteRead | None = None,
    ) -> None:
        try:
            self._ladder_consider_close(record, stuck, route=route)
        finally:
            with self._lock:
                self._closing.discard(record.job.leaf)

    def _ladder_close_decision(
        self,
        record: _WatchRecord,
        stuck: StuckConnections,
        *,
        route: RouteRead | None,
    ) -> CloseDecision:
        info = record.ladder
        assert info is not None
        with self._lock:
            judgement = info.judgement
            pending = bool(judgement is not None and judgement.successor_probe_pending)
            timed_out = bool(judgement is not None and judgement.successor_timed_out)
        if info.mode != "leaf":
            ladder = record.job.ladder
            if ladder is not None:
                with ladder.lock:
                    timed_out = timed_out or info.timed_out or bool(ladder.timed_out.get(info.rung.index))
        if route is None:
            confirmed = None
            age = None
        else:
            if info.mode == "leaf":
                confirmed = route.demoted(record.job.leaf)
            else:
                confirmed = rung_demoted(route.now, route.alive, route.unknown, info.rung, info.pick or route.now)
            age = max(0.0, self._clock() - route.read_at)
        return select_stuck_closes(
            stuck,
            enabled=bool(self._call(self._hooks.close_enabled, False)),
            successor_probe_pending=pending,
            successor_timed_out=timed_out,
            demotion_confirmed=confirmed,
            readback_age_s=age,
            constants=self._c,
        )

    def _ladder_close_record(self, record: _WatchRecord, decision: CloseDecision) -> dict[str, Any]:
        result = close_record(decision, record.job.leaf, self._tokenizer, now_wall=self._wall())
        if record.ladder is not None:
            result.update(record.ladder.fields)
        return result

    def _ladder_consider_close(
        self,
        record: _WatchRecord,
        stuck: StuckConnections,
        *,
        route: RouteRead | None,
    ) -> None:
        decision = self._ladder_close_decision(record, stuck, route=route)
        if decision.action == CLOSE_NEED_READBACK:
            # The watch's job reads its own rung (a view), not the rung the
            # ladder has reached since.
            fresh = self._call(lambda: self._hooks.read_route(record.job), None)
            if not isinstance(fresh, RouteRead):
                unavailable = {
                    "ev": "close",
                    "t": round(self._wall(), 3),
                    "leaf": self._tokenizer.leaf(record.job.leaf),
                    "action": "readback_unavailable",
                    "count": 0,
                }
                if record.ladder is not None:
                    unavailable.update(record.ladder.fields)
                self._emit("info", unavailable)
                return
            decision = self._ladder_close_decision(record, stuck, route=fresh)
        if decision.action == CLOSE:
            result = self._call(lambda: self._hooks.close(record.job, record.job.leaf, decision.ids), ())
            closed = _closed_ids(decision.ids, result)
            mark = getattr(record.runner, "mark_closed", None)
            if closed and callable(mark):
                self._call(lambda: mark(closed), None)
            with self._lock:
                self.metrics["closed_ids"] += len(closed)
            if not closed:
                decision = CloseDecision("close_failed", (), "cleanup_unavailable", kept=decision.kept)
            elif len(closed) < len(decision.ids):
                aged = set(decision.ids[: decision.aged])
                aged_closed = sum(1 for item in closed if item in aged)
                decision = CloseDecision(
                    CLOSE,
                    closed,
                    "partially_closed",
                    aged=aged_closed,
                    waiting=len(closed) - aged_closed,
                    kept=decision.kept,
                )
        self._ladder_settle_close(record, decision)

    def _ladder_settle_close(self, record: _WatchRecord, decision: CloseDecision) -> None:
        leaf = record.job.leaf
        if decision.action == CLOSE_NONE:
            return
        if decision.action == CLOSE_DEFER:
            if record.defer_logged:
                return
            record.defer_logged = True
            self._emit("debug", self._ladder_close_record(record, decision))
            return
        if decision.action == CLOSE_NOT_DEMOTED:
            level = "debug" if record.not_demoted_logged else "info"
            record.not_demoted_logged = True
            self._emit(level, self._ladder_close_record(record, decision))
            return
        if decision.action == "close_failed":
            level = "debug" if record.failed_logged else "info"
            record.failed_logged = True
            self._emit(level, self._ladder_close_record(record, decision))
            return
        if decision.action in {CLOSE_KEEP_ALL, CLOSE_DISABLED}:
            with self._lock:
                if self._watches.get(leaf) is record:
                    del self._watches[leaf]
            end = getattr(record.runner, "end_demotion_watch", None)
            if callable(end):
                self._call(lambda: end(leaf), None)
        self._emit("info", self._ladder_close_record(record, decision))

    # -- plumbing --------------------------------------------------------------

    def _spawn(self, name: str, target: Callable[..., Any], *args: Any) -> threading.Thread:
        thread = threading.Thread(target=target, args=args, name=name, daemon=True)
        with self._lock:
            self._threads = {item for item in self._threads if item.is_alive()}
            self._threads.add(thread)
        thread.start()
        return thread

    @staticmethod
    def _call(callback: Callable[[], Any], default: Any) -> Any:
        try:
            return callback()
        except Exception:
            return default

    def _emit(self, level: str, record: Mapping[str, Any]) -> None:
        try:
            self._emit_fn(level, record)
        except Exception:
            pass
