"""Deferred settings-apply intent ledger (T-P2-11, B-26(1)(2)).

Pure state machine owned by one DesktopRuntime. It never calls the runtime,
Core, filesystem or clock; callers pass observed lifecycle facts in. Every
transition happens under the ledger's own leaf lock, and callers never run
runtime/Core code while holding it.

States (PROTOCOL §16 PendingApply)::

    pending -> applying -> applied | failed | cancelled
    pending -> superseded | cancelled | failed     (never applied)

* stop / not desired          -> cancelled (saved config takes effect next start)
* start failure (phase failed before running was seen) -> failed (saved config
  kept, next start); ``failed`` after running keeps waiting for recovery
* runtime generation changed   -> superseded (the newer start loaded the file)
* newer config_rev             -> superseded; the remaining Core work is carried
  by one explicit successor intent bound to the newer revision (merge rule).

Terminal records are immutable. A record in ``applying`` is finalized only by
the apply path that owns it, never by a lazy read.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import re
import threading
from typing import Any, Iterable

from runtime_reload_gate import RuntimeReloadReason


STATES = ("pending", "applying", "applied", "failed", "superseded", "cancelled")
TERMINAL = frozenset({"applied", "failed", "superseded", "cancelled"})
EFFECTS = ("recheck", "reload", "restart")
# Independent actions that need a live Core. settings_effects marks exactly
# these as ``deferred`` during startup; every other action is executed by the
# save orchestrator immediately and never enters this ledger.
CORE_DEPENDENT_ACTIONS = frozenset({"refresh_dns_policy", "latency_recheck"})
WAITING_PHASES = frozenset({"collecting", "core_standby"})
_GRAPH = RuntimeReloadReason.USER_GRAPH_CONFIG.value
_DNS = RuntimeReloadReason.DNS_LIFECYCLE.value
HISTORY = 16
_REASON = re.compile(r"[a-z][a-z0-9_]{0,63}")


@dataclass(frozen=True)
class CoreSpec:
    """The Core half of an apply plan, as produced by settings_effects.plan()."""

    effect: str
    reason: str | None = None
    mode: str | None = None
    fallback_effect: str | None = None

    @property
    def rank(self) -> int:
        # restart > strict user-graph reload > DNS incremental reload > recheck.
        # Same precedence as settings_effects.plan() for one combined save.
        if self.effect == "restart":
            return 4
        if self.effect == "reload":
            return 3 if self.reason == _GRAPH else 2
        return 1


@dataclass(frozen=True)
class ApplyFacts:
    """Observed lifecycle facts; ``None`` means "not observed by this read"."""

    generation: int
    stop_requested: bool
    phase: str | None = None
    failure_code: str | None = None
    core_running: bool | None = None
    core_generation: int | None = None


@dataclass(frozen=True)
class Intent:
    intent_id: str
    config_rev: int
    core: CoreSpec
    user_commit_id: str
    owner_generation: int
    actions: frozenset
    state: str = "pending"
    last_error: dict | None = None
    # Core generation sampled while no Core transition was in flight, after
    # the save. A later generation proves a Core launched from the saved file.
    core_floor: int | None = None
    successor: str | None = None
    covered: bool = False
    # The deferred DNS policy refresh already ran for this intent's content.
    dns_refreshed: bool = False
    # This runtime generation was observed in phase ``running`` after the save:
    # a later ``failed`` phase is a transient Core outage, not a start failure.
    seen_running: bool = False

    @property
    def effect(self) -> str:
        return self.core.effect

    def dto(self) -> dict[str, Any]:
        """PROTOCOL §16 PendingApply (closed set)."""
        return {"intent_id": self.intent_id, "config_rev": int(self.config_rev),
                "generation": int(self.owner_generation), "state": self.state,
                "effect": self.core.effect, "error": deepcopy(self.last_error)}


def apply_error(code: str, reason_code: str, message: str = "apply_failed",
                **params: Any) -> dict[str, Any]:
    """Structured, privacy-safe Error DTO; never carries raw exception text."""
    return {"code": code, "message_key": "errors.settings." + message,
            "params": dict(params), "reason_code": reason_code}


def result_reason_code(result: Any, default: str) -> str:
    """A reload result's machine reason, only when it is a plain identifier."""
    reason = result.get("reason") if isinstance(result, dict) else None
    if isinstance(reason, str) and _REASON.fullmatch(reason):
        return reason
    return default


def _error_code(value: Any, default: str) -> str:
    from bridge_protocol import ERROR_CODES
    return value if isinstance(value, str) and value in ERROR_CODES else default


def core_request_from_plan(plan: Any) -> tuple[CoreSpec | None, frozenset]:
    """Extract deferred Core work from an ApplyPlan or a bare CoreEffect.

    Only a ``deferred`` CoreEffect is Core work for this ledger; it carries the
    underlying effect in ``requested_effect``. reason/mode/fallback are kept
    unchanged so the later apply uses the same reload reason codes
    (USER_GRAPH_CONFIG / DNS_LIFECYCLE). A live restart/reload/recheck was
    already executed by the save orchestrator, so it yields no Core work here:
    passing such a plan only advances the live intent's config revision.
    """
    if plan is None:
        return None, frozenset()
    core = getattr(plan, "core", plan)
    actions: set[str] = set()
    for action in getattr(plan, "actions", ()) or ():
        if (getattr(action, "state", None) == "deferred"
                and getattr(action, "action", None) in CORE_DEPENDENT_ACTIONS):
            actions.add(action.action)
    if getattr(core, "effect", None) != "deferred":
        return None, frozenset(actions)
    effect = getattr(core, "requested_effect", None)
    if effect in (None, "none"):
        return None, frozenset(actions)
    if effect not in EFFECTS:
        raise ValueError("unsupported deferred Core effect")
    if effect == "recheck":
        actions.add("latency_recheck")
    spec = CoreSpec(effect, getattr(core, "reason", None), getattr(core, "mode", None),
                    getattr(core, "fallback_effect", None))
    return spec, frozenset(actions)


class SettingsApplyLedger:
    """At most one live (pending/applying) intent per runtime plus bounded history."""

    def __init__(self, *, history: int = HISTORY) -> None:
        self._lock = threading.Lock()
        self._counter = 0
        self._revision = 0
        self._current: Intent | None = None
        self._records: dict[str, Intent] = {}
        self._history = max(2, int(history))

    # -- internal helpers (lock held) ---------------------------------------
    def _new_id(self) -> str:
        self._counter += 1
        return f"apply-{self._counter}"

    def _store(self, intent: Intent) -> Intent:
        self._records.pop(intent.intent_id, None)  # (re)insert as the newest record
        self._records[intent.intent_id] = intent
        while len(self._records) > self._history:
            oldest = next(iter(self._records))
            if self._current is not None and oldest == self._current.intent_id:
                break
            self._records.pop(oldest)
        current = self._current
        if current is None or current.intent_id == intent.intent_id or intent.state not in TERMINAL:
            self._current = intent
        self._revision += 1
        return intent

    def _reconcile_locked(self, facts: ApplyFacts | None) -> None:
        intent = self._current
        if facts is None or intent is None or intent.state != "pending":
            return
        if int(facts.generation) != intent.owner_generation:
            self._store(replace(intent, state="superseded", last_error=apply_error(
                "superseded", "generation", "apply_superseded")))
        elif facts.phase == "failed" and not intent.seen_running:
            # Only a generation that never reached running after the save is a
            # start failure; after running, ``failed`` (Core down + last_error)
            # is a recovery window and the intent keeps waiting (review B
            # SHOULD-3 option 1; ruling requested in the T-P2-11 report).
            self._store(replace(intent, state="failed", last_error=apply_error(
                _error_code(facts.failure_code, "runtime_unavailable"), "start_failed",
                "apply_start_failed")))
        elif facts.stop_requested or facts.phase in {"stopping", "stopped"}:
            self._store(replace(intent, state="cancelled", last_error=apply_error(
                "cancelled", "stopped", "apply_cancelled")))

    @staticmethod
    def _covered(intent: Intent, facts: ApplyFacts) -> bool:
        return (intent.core_floor is not None and facts.core_generation is not None
                and int(facts.core_generation) > intent.core_floor)

    def _rebase_locked(self, intent: Intent, config_rev: int) -> Intent:
        successor = replace(intent, intent_id=self._new_id(), config_rev=int(config_rev),
                            state="pending", last_error=None, successor=None)
        self._store(replace(intent, state="superseded", successor=successor.intent_id,
                            last_error=apply_error("superseded", "config_rev", "apply_superseded",
                                                   successor_intent_id=successor.intent_id)))
        return self._store(successor)

    # -- public API ----------------------------------------------------------
    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def current(self) -> Intent | None:
        with self._lock:
            return self._current

    def get(self, intent_id: str) -> Intent | None:
        with self._lock:
            return self._records.get(intent_id)

    def needs_core_floor(self) -> bool:
        with self._lock:
            intent = self._current
            return bool(intent is not None and intent.state == "pending" and intent.core_floor is None)

    def note_core_floor(self, core_generation: int | None) -> None:
        if core_generation is None:
            return
        with self._lock:
            intent = self._current
            if intent is not None and intent.state == "pending" and intent.core_floor is None:
                self._store(replace(intent, core_floor=int(core_generation)))

    def reconcile(self, facts: ApplyFacts) -> Intent | None:
        with self._lock:
            self._reconcile_locked(facts)
            return self._current

    def request(self, core: CoreSpec | None, actions: Iterable[str] = (), *,
                config_rev: int, user_commit_id: str, facts: ApplyFacts,
                core_floor: int | None) -> Intent | None:
        """Record or merge deferred Core work; never waits for anything.

        One live intent at most: a save at the same revision merges in place, a
        newer revision supersedes the live intent with one merged successor
        (latest rev and commit ID, heavier effect, union of deferred actions).
        A late request for an older revision is recorded as superseded.
        """
        if type(config_rev) is not int or config_rev < 0:
            raise ValueError("config_rev must be a non-negative integer")
        if not isinstance(user_commit_id, str) or not user_commit_id.strip():
            raise ValueError("user_commit_id is required for an apply intent")
        actions = frozenset(actions) & CORE_DEPENDENT_ACTIONS
        generation = int(facts.generation)
        with self._lock:
            self._reconcile_locked(facts)
            live = self._current if (self._current is not None
                                     and self._current.state == "pending") else None
            if live is None:
                if core is None:
                    return None
                intent = Intent(self._new_id(), config_rev, core, user_commit_id, generation,
                                actions, core_floor=core_floor)
                self._store(intent)
            elif config_rev < live.config_rev:
                stale_core = core or live.core
                intent = Intent(self._new_id(), config_rev, stale_core, user_commit_id, generation,
                                actions, state="superseded", successor=live.intent_id,
                                last_error=apply_error("superseded", "config_rev", "apply_superseded",
                                                       successor_intent_id=live.intent_id))
                self._records[intent.intent_id] = intent
                self._revision += 1
                return intent
            else:
                merged_core = live.core if core is None or live.core.rank >= core.rank else core
                # A save without deferred Core work (e.g. a live plan whose
                # restart/reload the orchestrator already ran) only advances the
                # revision: the remaining work is still the live intent's, so its
                # floor stays. New Core work needs a Core launched after this save.
                floor = live.core_floor if core is None else core_floor
                refreshed = live.dns_refreshed and "refresh_dns_policy" not in actions
                merged = replace(live, core=merged_core, actions=live.actions | actions,
                                 user_commit_id=user_commit_id, core_floor=floor,
                                 dns_refreshed=refreshed)
                if config_rev == live.config_rev:
                    intent = self._store(merged)
                else:
                    intent = self._rebase_locked(merged, config_rev)
            self._reconcile_locked(facts)
            return self._records.get(intent.intent_id)

    def prepare(self, intent_id: str, facts: ApplyFacts, *,
                config_rev: int | None = None) -> tuple[str, Intent | None]:
        """Ready check with full lifecycle facts, before any Core lock.

        Returns ``none`` (unknown/old id), ``terminal``, ``wait`` (still
        starting) or ``ready``. A newer bridge config revision rebases the live
        intent into an explicit successor first; the old id is superseded.
        """
        with self._lock:
            intent = self._current
            if intent is None or intent.intent_id != intent_id:
                record = self._records.get(intent_id)
                return ("terminal" if record is not None and record.state in TERMINAL
                        else "none"), record
            self._reconcile_locked(facts)
            intent = self._current
            if intent.state in TERMINAL:
                return "terminal", intent
            if intent.state != "pending":
                return "wait", intent
            if type(config_rev) is int and config_rev > intent.config_rev:
                # A save that bypassed request(): its content is unknown, so the
                # DNS policy refresh is owed again (conservative).
                intent = self._rebase_locked(replace(intent, dns_refreshed=False), config_rev)
            if facts.phase != "running":
                return "wait", intent
            if not intent.seen_running:
                intent = self._store(replace(intent, seen_running=True))
            return "ready", intent

    def claim_dns_refresh(self, intent_id: str, facts: ApplyFacts) -> bool:
        """True exactly once per intent content, and only when it can matter.

        Rechecks stop/generation first and skips when the Core is down (the
        apply would wait) or a Core launched after the save already covers the
        work; the caller runs the refresh outside every lock afterwards.
        """
        with self._lock:
            intent = self._current
            if intent is None or intent.intent_id != intent_id or intent.state != "pending":
                return False
            self._reconcile_locked(facts)
            intent = self._current
            if (intent.state != "pending" or facts.stop_requested or intent.dns_refreshed
                    or "refresh_dns_policy" not in intent.actions
                    or intent.core.effect == "restart" or not facts.core_running
                    or self._covered(intent, facts)):
                return False
            self._store(replace(intent, dns_refreshed=True))
            return True

    def start(self, intent_id: str, facts: ApplyFacts) -> tuple[str, Intent | None]:
        """Final check under the Core transition lock; pending -> applying.

        Returns ``terminal``, ``wait`` (Core not running, e.g. a recovery left
        it down), ``covered`` (a Core launched after the save already loaded
        the saved file) or ``apply``.
        """
        with self._lock:
            intent = self._current
            if intent is None or intent.intent_id != intent_id or intent.state != "pending":
                record = self._records.get(intent_id)
                return ("terminal" if record is not None and record.state in TERMINAL
                        else "wait"), record
            self._reconcile_locked(facts)
            intent = self._current
            if intent.state in TERMINAL:
                return "terminal", intent
            if not facts.core_running:
                return "wait", intent
            covered = self._covered(intent, facts)
            intent = self._store(replace(intent, state="applying", covered=covered))
            return ("covered" if covered else "apply"), intent

    def finish(self, intent_id: str, state: str, error: dict | None = None) -> Intent | None:
        """applying -> applied/failed/cancelled. Terminal records never change."""
        if state not in {"applied", "failed", "cancelled"}:
            raise ValueError("invalid final apply state")
        if state != "applied" and error is None:
            raise ValueError("an unapplied intent must carry its error")
        with self._lock:
            intent = self._records.get(intent_id)
            if intent is None or intent.state != "applying":
                return intent
            return self._store(replace(intent, state=state,
                                       last_error=None if state == "applied" else deepcopy(error)))
