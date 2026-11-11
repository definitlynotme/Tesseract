"""Tier3B member health that survives provider rebuilds (11-02).

Any byte change of the TIER3B-RUNTIME provider file makes Mihomo rebuild every
member as alive-by-default with an empty ``extra``.  The rebuild's own URL test
can be skipped: it joins an in-flight check or reuses a result younger than
1 s (the provider singledo), and a dial-triggered group check swallows it the
same way.  Measured on the bundled Mihomo 1.19.27 (11-01/11-02): rebuilt
members stayed untested for 5.5-13 s and more, and an unfixed fallback then
served a revived dead first member.

This module is pure apart from controller callbacks:

* ``member_verdicts`` reads each member's verdict for the group URL from a
  side-effect-free provider payload (``GET /providers/proxies/<provider>``);
* ``Tier3HealthLedger`` keeps the last verdict per member identity across
  rebuilds;
* ``overlay_provider_payload`` fills the ledger verdict into members that have
  none, so readers applying Mihomo's ``extra[url]`` rule never count an
  untested member as alive and never see the whole group collapse right
  after a rebuild;
* ``order_tier3_members`` builds a new membership order that keeps the live
  pick first, so a rebuild cannot move it, and fills capacity from the health
  classes;
* ``verify_provider_members`` tests members explicitly through the
  provider-scoped member endpoint, which calls URLTest directly and is not
  subject to the provider singledo.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from runtime_post_dial import parse_rfc3339

ALIVE = "alive"
DEAD = "dead"
UNKNOWN = "unknown"

# A dead member is only displaced to make room once its death has settled:
# two consecutive dead verdicts, or one that is at least this old.  A member
# that flaps between ticks therefore does not churn the Core membership.
DEAD_SETTLE_SECONDS = 60.0
DEAD_SETTLE_STREAK = 2
# Mihomo and this process read the same wall clock; this only absorbs the
# ordering of the two reads around a write.
VERDICT_CLOCK_SKEW_SECONDS = 0.25
VERIFY_CONCURRENCY = 16


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    state: str
    verdict_wall: float
    dead_since_wall: float = 0.0
    dead_streak: int = 0


def member_verdicts(
    provider_payload: Mapping[str, Any] | None,
    test_url: str,
    *,
    newer_than_wall: float | None = None,
) -> dict[str, tuple[bool, float]]:
    """Per member name: ``(alive, verdict wall time)`` for ``test_url``.

    Only ``extra[test_url]`` counts.  Mihomo's global ``alive`` flag is true
    on every freshly rebuilt member and is also moved by tests with other
    URLs, so it is never a verdict for the group's contract.  A member whose
    newest sample is not newer than ``newer_than_wall`` is left out.
    """

    verdicts: dict[str, tuple[bool, float]] = {}
    if not isinstance(provider_payload, Mapping):
        return verdicts
    members = provider_payload.get("proxies")
    if not isinstance(members, list):
        return verdicts
    for member in members:
        if not isinstance(member, Mapping):
            continue
        name = str(member.get("name") or "").strip()
        extra = member.get("extra")
        entry = extra.get(test_url) if isinstance(extra, Mapping) else None
        if not name or not isinstance(entry, Mapping) or entry.get("ledger"):
            continue
        alive = entry.get("alive")
        history = entry.get("history")
        if not isinstance(alive, bool) or not isinstance(history, list) or not history:
            continue
        times = [
            stamp
            for stamp in (
                parse_rfc3339(sample.get("time"))
                for sample in history
                if isinstance(sample, Mapping)
            )
            if stamp is not None
        ]
        if not times:
            continue
        newest = max(times)
        if newer_than_wall is not None and newest <= float(newer_than_wall):
            continue
        verdicts[name] = (alive, float(newest))
    return verdicts


class Tier3HealthLedger:
    """Last contract verdict per member identity, kept across rebuilds."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, LedgerEntry] = {}
        self._revision = 0

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def observe(self, identity: str, alive: bool, verdict_wall: float) -> bool:
        """Record one verdict; older or repeated verdicts are ignored."""

        key = str(identity or "")
        if not key:
            return False
        stamp = float(verdict_wall)
        with self._lock:
            previous = self._entries.get(key)
            if previous is not None and stamp <= previous.verdict_wall:
                return False
            if alive:
                entry = LedgerEntry(ALIVE, stamp)
            elif previous is not None and previous.state == DEAD:
                entry = LedgerEntry(
                    DEAD,
                    stamp,
                    previous.dead_since_wall or stamp,
                    previous.dead_streak + 1,
                )
            else:
                entry = LedgerEntry(DEAD, stamp, stamp, 1)
            if entry == previous:
                return False
            self._entries[key] = entry
            self._revision += 1
            return True

    def observe_payload(
        self,
        provider_payload: Mapping[str, Any] | None,
        test_url: str,
        identity_of: Callable[[str], str | None],
        *,
        newer_than_wall: float | None = None,
    ) -> int:
        """Fold every verdict in a provider payload in; returns the change count."""

        changed = 0
        for name, (alive, stamp) in member_verdicts(
            provider_payload, test_url, newer_than_wall=newer_than_wall
        ).items():
            identity = identity_of(name)
            if identity and self.observe(identity, alive, stamp):
                changed += 1
        return changed

    def entry(self, identity: str) -> LedgerEntry | None:
        with self._lock:
            return self._entries.get(str(identity or ""))

    def state(self, identity: str) -> str:
        entry = self.entry(identity)
        return entry.state if entry is not None else UNKNOWN

    def dead_settled(self, identity: str, *, now_wall: float | None = None) -> bool:
        entry = self.entry(identity)
        if entry is None or entry.state != DEAD:
            return False
        if entry.dead_streak >= DEAD_SETTLE_STREAK:
            return True
        now = time.time() if now_wall is None else float(now_wall)
        return now - entry.dead_since_wall >= DEAD_SETTLE_SECONDS

    def snapshot(self) -> dict[str, LedgerEntry]:
        with self._lock:
            return dict(self._entries)

    def retain(self, identities: Iterable[str]) -> int:
        """Forget identities outside ``identities``; returns the drop count."""

        keep = {str(identity or "") for identity in identities}
        with self._lock:
            dropped = [key for key in self._entries if key not in keep]
            for key in dropped:
                del self._entries[key]
            if dropped:
                self._revision += 1
            return len(dropped)


def _rfc3339(wall: float) -> str:
    stamp = datetime.fromtimestamp(float(wall), tz=timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def overlay_provider_payload(
    provider_payload: Mapping[str, Any] | None,
    test_url: str,
    verdict_of: Callable[[str], LedgerEntry | None],
) -> Mapping[str, Any] | None:
    """Return the payload with ledger verdicts filled into untested members.

    A member that already carries ``extra[test_url]`` history keeps Mihomo's
    own verdict.  One without it gets ``{"alive": ..., "history": [last
    verdict], "ledger": <state>}`` from the ledger; with no ledger entry it
    reads ``alive: False`` with an empty history (``ledger: "unknown"``), so
    an untested member never counts as alive.  The injected sample keeps the
    ledger verdict's own time, never the read time, so a reader waiting for a
    sample newer than its probe never mistakes it for a fresh result.  REJECT
    and other non-leaf entries are left alone.
    """

    if not isinstance(provider_payload, Mapping):
        return provider_payload
    members = provider_payload.get("proxies")
    if not isinstance(members, list):
        return provider_payload
    patched: list[Any] = []
    changed = False
    for member in members:
        if not isinstance(member, Mapping):
            patched.append(member)
            continue
        name = str(member.get("name") or "").strip()
        kind = str(member.get("type") or "").strip().lower()
        extra = member.get("extra")
        entry = extra.get(test_url) if isinstance(extra, Mapping) else None
        # Mihomo creates extra[url] only when a test for that URL completes,
        # so any entry carrying a boolean alive is its own verdict.
        has_verdict = isinstance(entry, Mapping) and isinstance(entry.get("alive"), bool)
        if not name or kind in {"reject", "direct", "pass"} or has_verdict:
            patched.append(member)
            continue
        ledger = verdict_of(name)
        if ledger is None:
            synthetic: dict[str, Any] = {"alive": False, "history": [], "ledger": UNKNOWN}
        else:
            synthetic = {
                "alive": ledger.state == ALIVE,
                "history": [{"time": _rfc3339(ledger.verdict_wall), "delay": 0}],
                "ledger": ledger.state,
            }
        new_extra = dict(extra) if isinstance(extra, Mapping) else {}
        new_extra[test_url] = synthetic
        new_member = dict(member)
        new_member["extra"] = new_extra
        patched.append(new_member)
        changed = True
    if not changed:
        return provider_payload
    result = dict(provider_payload)
    result["proxies"] = patched
    return result


def order_tier3_members(
    candidates: Sequence[str],
    loaded: Sequence[str],
    *,
    pick: str | None,
    pick_alive: bool,
    state_of: Callable[[str], str],
    dead_settled: Callable[[str], bool],
    limit: int | None = None,
    always: bool = False,
) -> list[str]:
    """Order the next Core membership so a provider rebuild keeps the live pick.

    ``candidates`` are the identities the Manager pool exports (admitted, not
    kicked), in admission order; ``loaded`` is the order last loaded into the
    Core.  Rules:

    * Same membership as loaded: return the loaded order unchanged.  A health
      change alone never causes a write.  ``always`` skips this rule for
      writes that rebuild every member anyway (a full config reload).
    * Otherwise ``[pick] + ALIVE + NEW + UNKNOWN + DEAD``, each class keeping
      its previous order (NEW keeps admission order).  The pick is kept even
      when the Manager kicked it, while it still serves (it leaves at the
      first write after the pick moves).
    * Over ``limit``: drop settled-dead members first, then other dead ones,
      unknown ones, and finally the newest admissions.  The pick is never
      dropped.
    """

    seen: set[str] = set()
    candidate_order = [
        identity
        for identity in (str(item or "") for item in candidates)
        if identity and not (identity in seen or seen.add(identity))
    ]
    candidate_set = set(candidate_order)
    seen = set()
    loaded_order = [
        identity
        for identity in (str(item or "") for item in loaded)
        if identity and not (identity in seen or seen.add(identity))
    ]
    loaded_set = set(loaded_order)
    pick_id = str(pick or "")
    keep_pick = bool(pick_id) and pick_alive and (
        pick_id in candidate_set or pick_id in loaded_set
    )

    alive: list[str] = []
    unknown: list[str] = []
    dead: list[str] = []
    settled: list[str] = []
    for identity in loaded_order:
        if identity == pick_id and keep_pick:
            continue
        if identity not in candidate_set:
            continue
        state = state_of(identity)
        if state == ALIVE:
            alive.append(identity)
        elif state == DEAD:
            (settled if dead_settled(identity) else dead).append(identity)
        else:
            unknown.append(identity)
    new = [
        identity
        for identity in candidate_order
        if identity not in loaded_set and not (identity == pick_id and keep_pick)
    ]
    head = [pick_id] if keep_pick else []
    tiers = [alive, new, unknown, dead, settled]
    if limit is not None and limit > 0:
        budget = max(0, int(limit) - len(head))
        kept_tiers: list[list[str]] = []
        for tier in tiers:
            take = tier[:budget]
            kept_tiers.append(take)
            budget -= len(take)
        # Capacity favours the healthier classes; within a class, earlier
        # members (older loads, earlier admissions) are kept.
        tiers = kept_tiers
    ordered = head + [identity for tier in tiers for identity in tier]
    if not always and set(ordered) == loaded_set:
        return list(loaded_order)
    return ordered


def verification_order(
    names: Sequence[str],
    *,
    pick: str | None,
    new: Iterable[str] = (),
    successors: int = 3,
) -> list[str]:
    """Priority for post-write tests: pick, its next successors, NEW, rest."""

    ordered = [str(name) for name in names if str(name or "") and str(name) != "REJECT"]
    new_set = {str(name) for name in new}
    first: list[str] = []
    if pick and pick in ordered:
        first.append(pick)
        start = ordered.index(pick) + 1
    else:
        start = 0
    first.extend(name for name in ordered[start:start + max(0, int(successors))] if name not in first)
    rest_new = [name for name in ordered if name in new_set and name not in first]
    rest = [name for name in ordered if name not in first and name not in new_set]
    return first + rest_new + rest


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    started_wall: float
    finished_wall: float
    elapsed_ms: float
    requested: tuple[str, ...]
    verdicts: Mapping[str, tuple[bool, float]]
    untested: tuple[str, ...]
    orphaned: tuple[str, ...]
    errors: Mapping[str, str] = field(default_factory=dict)
    provider_payload: Mapping[str, Any] | None = None
    read_error: str | None = None

    @property
    def alive(self) -> tuple[str, ...]:
        return tuple(name for name in self.requested if self.verdicts.get(name, (False, 0.0))[0])

    @property
    def dead(self) -> tuple[str, ...]:
        return tuple(
            name for name in self.requested
            if name in self.verdicts and not self.verdicts[name][0]
        )

    @property
    def complete(self) -> bool:
        return not self.untested and self.read_error is None


def verify_provider_members(
    names: Sequence[str],
    *,
    test_member: Callable[[str], Any],
    read_provider: Callable[[], Mapping[str, Any]],
    test_url: str,
    started_wall: float,
    deadline_monotonic: float,
    concurrency: int = VERIFY_CONCURRENCY,
    stop_when: Callable[[Mapping[str, tuple[bool, float]]], bool] | None = None,
    read_every: float = 0.25,
) -> VerificationOutcome:
    """Test ``names`` explicitly and read their verdicts back.

    ``names`` is the priority order.  Each ``test_member`` call issues one
    provider-scoped member test.  It must return normally whenever the test
    ran, whatever its result (Mihomo answers 200 with a delay even when the
    expected status failed, 503/504 for transport failures and timeouts), and
    raise only when the controller call itself failed.  Verdicts come only
    from ``extra[test_url]`` in ``read_provider``'s payload, and only samples
    newer than the write count.  A member whose test ran but whose verdict is
    missing was rebuilt away by a later write (orphaned).  ``stop_when`` lets
    the caller stop early (for example once the pick and every member ahead
    of it have verdicts); the remaining tests still run to completion in the
    background and land on the same objects.
    """

    requested = tuple(dict.fromkeys(str(name) for name in names if str(name or "")))
    started_mono = time.monotonic()
    floor = float(started_wall) - VERDICT_CLOCK_SKEW_SECONDS
    errors: dict[str, str] = {}
    returned: set[str] = set()
    verdicts: dict[str, tuple[bool, float]] = {}
    payload: Mapping[str, Any] | None = None
    read_error: str | None = None
    if not requested:
        return VerificationOutcome(
            started_wall, time.time(), 0.0, (), {}, (), (), {}, None, None
        )

    def run(name: str) -> str:
        try:
            test_member(name)
        except Exception as exc:  # verdicts are read back; errors are diagnostics
            errors[name] = str(exc)[:200]
        return name

    def read_back(*, final: bool = False) -> None:
        nonlocal payload, read_error
        if not final and deadline_monotonic - time.monotonic() <= 0:
            return
        try:
            payload = read_provider()
            read_error = None
        except Exception as exc:
            read_error = str(exc)[:200]
            return
        verdicts.update(member_verdicts(payload, test_url, newer_than_wall=floor))

    executor = ThreadPoolExecutor(
        max_workers=max(1, min(len(requested), int(concurrency))),
        thread_name_prefix="tier3-provider-verify",
    )
    try:
        pending: set[Future[str]] = {executor.submit(run, name) for name in requested}
        last_read = 0.0
        while pending:
            remaining = deadline_monotonic - time.monotonic()
            if remaining <= 0:
                break
            done, pending = wait(pending, timeout=min(remaining, read_every), return_when=FIRST_COMPLETED)
            for future in done:
                returned.add(future.result())
            if stop_when is not None and time.monotonic() - last_read >= read_every:
                last_read = time.monotonic()
                read_back()
                if stop_when(dict(verdicts)):
                    break
        read_back(final=True)
    finally:
        executor.shutdown(wait=False, cancel_futures=False)
    tested = {name: verdicts[name] for name in requested if name in verdicts}
    untested = tuple(name for name in requested if name not in tested)
    orphaned = tuple(name for name in untested if name in returned and name not in errors)
    finished_wall = time.time()
    return VerificationOutcome(
        started_wall=float(started_wall),
        finished_wall=finished_wall,
        elapsed_ms=(time.monotonic() - started_mono) * 1000.0,
        requested=requested,
        verdicts=tested,
        untested=untested,
        orphaned=orphaned,
        errors=dict(errors),
        provider_payload=payload,
        read_error=read_error,
    )
