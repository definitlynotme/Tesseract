"""Time-limited lease for the app's own automatic fallback pins (rules lease-v1).

User decision 2026-10-26: an automatic ``fixed`` pick that the app writes on a
fallback group (TIER3-AUTO in practice) is a lease, not a permanent pin.  It
ends when its time runs out, or earlier when a higher-priority member passes
Mihomo's own health check.  A user's own selection is never a lease.

This module only decides; it does no I/O.  ``desktop_runtime`` keeps one
``LeaseClock`` per group, feeds the published route snapshot and one
side-effect-free provider read (``GET /providers/proxies/TIER3B-RUNTIME``) to
``evaluate_route_lease`` and executes the decision.

Refinements of the user's literal rule, chosen from the s2/s3/f04arm1 pin
evidence (sim3) and configurable: the higher member must be the member native
fallback would pick next, and it must have been free of failures for
``stable_seconds`` (110 s, one group health-check cycle minus jitter);
``stable_seconds: 0`` restores the literal rule.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping, Sequence

from config_utils import coerce_bool
from runtime_post_dial import parse_rfc3339


RULES_VERSION = "lease-v1"
TIER3_GROUP = "TIER3-AUTO"
TIER3_PROVIDER = "TIER3B-RUNTIME"
REJECT_NAMES = frozenset({"REJECT"})

# On by default since 10-28 (shipped with the ship1-tested judge package);
# enabled: false turns the lease off.  early_release is False by default since
# 11-01 (ship3 B-7: churn after early releases): a lease ends at its TTL.
DEFAULT_ENABLED = True
DEFAULT_TTL_SECONDS = 600.0
DEFAULT_EARLY_RELEASE = False
DEFAULT_STABLE_SECONDS = 110.0
DEFAULT_WATCH_INTERVAL_SECONDS = 10.0
DEFAULT_DEATH_CARRY_SECONDS = 60.0

TTL_RANGE = (60.0, 3600.0)
STABLE_RANGE = (0.0, 600.0)
WATCH_INTERVAL_RANGE = (5.0, 30.0)
DEATH_CARRY_RANGE = (0.0, 300.0)

# Constants (not configuration keys).
MIN_HOLD_AFTER_DECISION_S = 120.0
SOFT_DEFER_CAP_S = 120.0
RELEASE_BUDGET_S = 4.5
DELETE_BUDGET_S = 1.0
PURGE_BUDGET_S = 1.0
PURGE_MAX_ATTEMPTS = 3
SHUTDOWN_BUDGET_S = 0.4
SHUTDOWN_MIN_REMAINING_S = 2.4
SHUTDOWN_RESERVE_S = 2.0
PROVIDER_READ_TIMEOUT_S = 0.35
FAST_ATTEMPTS = 3
SLOW_RETRY_S = 60.0
ORPHAN_ADOPT_S = 30.0
IDLE_TICKS = 2
MAX_CLOCKS = 8
FUTURE_SKEW_S = 5.0
APP_WINDOW_PAD_S = 0.05
MISMATCH_CONFIRM_SNAPSHOTS = 2
BACKOFF_WINDOW_S = 300.0
BACKOFF_CAP_S = 600.0

# Ends after which a re-pin of the same member keeps the original TTL start.
DEATH_END_CAUSES = frozenset({"core_cleared", "pinned_dead"})
# Releases the lease itself decided (counted as lease-issued route changes).
LEASE_RELEASE_CAUSES = frozenset({"ttl", "early"})


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _seconds(value: Any, default: float, low: float, high: float) -> float:
    if isinstance(value, bool):
        return float(default)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if number != number or number in (float("inf"), float("-inf")):
        return float(default)
    return max(float(low), min(float(high), number))


@dataclass(frozen=True, slots=True)
class RouteLeasePolicy:
    """``client.egress_recovery.route_lease`` (read live on every tick)."""

    enabled: bool = DEFAULT_ENABLED
    ttl_seconds: float = DEFAULT_TTL_SECONDS
    early_release: bool = DEFAULT_EARLY_RELEASE
    stable_seconds: float = DEFAULT_STABLE_SECONDS
    watch_interval_seconds: float = DEFAULT_WATCH_INTERVAL_SECONDS
    death_carry_seconds: float = DEFAULT_DEATH_CARRY_SECONDS

    @classmethod
    def disabled(cls) -> "RouteLeasePolicy":
        return cls(enabled=False)

    @classmethod
    def from_config(cls, config: Any) -> "RouteLeasePolicy":
        root = _mapping(config)
        client = _mapping(root.get("client"))
        recovery = _mapping(client.get("egress_recovery"))
        block = _mapping(recovery.get("route_lease"))
        if not coerce_bool(block.get("enabled", DEFAULT_ENABLED), DEFAULT_ENABLED):
            return cls.disabled()
        return cls(
            enabled=True,
            ttl_seconds=_seconds(block.get("ttl_seconds", DEFAULT_TTL_SECONDS), DEFAULT_TTL_SECONDS, *TTL_RANGE),
            early_release=coerce_bool(block.get("early_release", DEFAULT_EARLY_RELEASE), DEFAULT_EARLY_RELEASE),
            stable_seconds=_seconds(
                block.get("stable_seconds", DEFAULT_STABLE_SECONDS), DEFAULT_STABLE_SECONDS, *STABLE_RANGE
            ),
            watch_interval_seconds=_seconds(
                block.get("watch_interval_seconds", DEFAULT_WATCH_INTERVAL_SECONDS),
                DEFAULT_WATCH_INTERVAL_SECONDS,
                *WATCH_INTERVAL_RANGE,
            ),
            death_carry_seconds=_seconds(
                block.get("death_carry_seconds", DEFAULT_DEATH_CARRY_SECONDS),
                DEFAULT_DEATH_CARRY_SECONDS,
                *DEATH_CARRY_RANGE,
            ),
        )

    @classmethod
    def for_tests(cls, **overrides: Any) -> "RouteLeasePolicy":
        """Enabled policy without clamps (unit and real-Core tests only)."""

        values: dict[str, Any] = {"enabled": True}
        values.update(overrides)
        return cls(**values)

    def witness(self) -> dict[str, Any]:
        """The exact policy a validation block ran with."""

        return {
            "rules": RULES_VERSION,
            "enabled": bool(self.enabled),
            "ttl_seconds": float(self.ttl_seconds),
            "early_release": bool(self.early_release),
            "stable_seconds": float(self.stable_seconds),
            "watch_interval_seconds": float(self.watch_interval_seconds),
            "death_carry_seconds": float(self.death_carry_seconds),
        }


def route_lease_policy(config: Any) -> RouteLeasePolicy:
    return RouteLeasePolicy.from_config(config)


@dataclass(slots=True)
class LeaseClock:
    """The TTL source of one group's automatic pin; nothing resets it.

    ``started_*`` is the first automatic PUT of this member (a renew or a
    re-pin within ``death_carry_seconds`` of a death end keeps it);
    ``last_decision_*`` is the newest clock-creating or reviving PUT.
    """

    group: str
    member: str
    started_mono: float
    started_wall: float
    last_decision_mono: float
    last_decision_wall: float
    intent_rev: int = 0
    core_generation: int = 0
    route_epoch: int = 0
    origin: str = "recovery"
    last_put_mono: float = 0.0
    inherited: bool = False
    previous_member: str = ""
    lease_id: str = ""
    # Terminal state; an ended clock is kept for death-carry and purges.
    ended: str = ""
    ended_by: str = ""
    ended_mono: float = 0.0
    purge_pending: bool = False
    purge_attempts: int = 0
    verify_retry: bool = False
    # Runtime bookkeeping (identity of the linked lease dict and counters).
    lease_obj: Any = field(default=None, repr=False, compare=False)
    announced: bool = False
    orphan_since_mono: float = 0.0
    backoff_checked: bool = False
    attempts: int = 0
    next_attempt_mono: float = 0.0
    reads: int = 0
    deferred_s: float = 0.0
    defer_reason: str = ""
    defer_started_mono: float = 0.0
    mismatch_seq: int = -1
    mismatch_mono: float = 0.0
    last_verify: str = "skipped"
    carried: bool = False
    skipped: set[str] = field(default_factory=set, repr=False, compare=False)

    @property
    def live(self) -> bool:
        return not self.ended


def lease_identifier(seed: str) -> str:
    """``L`` + 11 hex: never all digits, so log parsers keep it a string."""

    return "L" + hashlib.sha256(str(seed).encode("utf-8")).hexdigest()[:11]


def due_time(clock: LeaseClock, policy: RouteLeasePolicy) -> float:
    """TTL due time (monotonic); never within 120 s of the latest decision."""

    return max(
        float(clock.started_mono) + float(policy.ttl_seconds),
        float(clock.last_decision_mono) + MIN_HOLD_AFTER_DECISION_S,
    )


def stable_requirement(policy: RouteLeasePolicy, backoff_level: int) -> float:
    """``stable_seconds`` doubled per backoff level, capped at 600 s."""

    base = float(policy.stable_seconds)
    level = max(0, int(backoff_level or 0))
    if base <= 0.0 or level <= 0:
        return base
    return min(BACKOFF_CAP_S, base * (2 ** level))


@dataclass(frozen=True, slots=True)
class GroupObservation:
    """One group's state from the last published ``/proxies`` snapshot."""

    group: str
    fixed: str
    fixed_alive: bool | None
    members: tuple[str, ...] = ()
    test_url: str = ""
    route_epoch: int = 0
    core_generation: int = 0
    seq: int = 0
    published_mono: float = 0.0


@dataclass(frozen=True, slots=True)
class LeaseBusy:
    """Why recovery owns the route now (hard: always; soft: until due+120)."""

    hard: str = ""
    soft: str = ""


@dataclass(frozen=True, slots=True)
class LeaseDecision:
    action: str  # none | defer | end | release | purge | mismatch
    cause: str = ""
    reason: str = ""
    purge: bool = False
    trigger: str = ""
    trigger_index: int | None = None
    pinned_index: int | None = None
    predicted: str = ""
    pass_age_s: float | None = None
    stable_s: float | None = None
    pass_app: bool | None = None
    higher_alive: int = 0
    due_s: float = 0.0
    skipped: str = ""


NONE = LeaseDecision("none")


def _history(entry: Mapping[str, Any] | None, now_wall: float) -> list[tuple[float, int]]:
    """``[(time, delay)]`` oldest first; future-dated (> 5 s) samples ignored."""

    if not isinstance(entry, Mapping):
        return []
    history = entry.get("history")
    if not isinstance(history, list):
        return []
    samples: list[tuple[float, int]] = []
    ceiling = float(now_wall) + FUTURE_SKEW_S
    for sample in history:
        if not isinstance(sample, Mapping):
            continue
        at = parse_rfc3339(sample.get("time"))
        if at is None or at > ceiling:
            continue
        try:
            delay = int(sample.get("delay") or 0)
        except (TypeError, ValueError):
            delay = 0
        samples.append((float(at), delay))
    samples.sort(key=lambda item: item[0])
    return samples


def _member_entry(proxy: Mapping[str, Any] | None, test_url: str) -> Mapping[str, Any] | None:
    if not isinstance(proxy, Mapping):
        return None
    extra = proxy.get("extra")
    if isinstance(extra, Mapping):
        entry = extra.get(test_url)
        if isinstance(entry, Mapping):
            return entry
    return None


def _member_alive(proxy: Mapping[str, Any] | None, test_url: str) -> bool | None:
    entry = _member_entry(proxy, test_url)
    if entry is not None and isinstance(entry.get("alive"), bool):
        return bool(entry["alive"])
    if isinstance(proxy, Mapping) and isinstance(proxy.get("alive"), bool):
        return bool(proxy["alive"])
    return None


def provider_members(provider: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    if not isinstance(provider, Mapping):
        return []
    proxies = provider.get("proxies")
    if not isinstance(proxies, list):
        return []
    return [item for item in proxies if isinstance(item, Mapping) and isinstance(item.get("name"), str)]


def contract_members(names: Iterable[str]) -> tuple[str, ...]:
    return tuple(str(name) for name in names if str(name) not in REJECT_NAMES)


def app_windows_for(
    member: str,
    app_windows: Mapping[str, Sequence[tuple[float, float | None]]] | None,
) -> list[tuple[float, float | None]]:
    """The member's own windows plus every group and provider window.

    A ``/group/<g>/delay`` round or a provider health check tests every
    TIER3B member, so each one conservatively covers all of them.
    """

    if not isinstance(app_windows, Mapping):
        return []
    windows: list[tuple[float, float | None]] = list(app_windows.get(member, ()) or ())
    for name, values in app_windows.items():
        if str(name).startswith(("group:", "provider:")):
            windows.extend(values or ())
    return windows


def in_app_window(at: float, windows: Iterable[tuple[float, float | None]], now_wall: float) -> bool:
    for issue, finish in windows:
        end = float(now_wall) if finish is None else float(finish)
        if float(issue) - APP_WINDOW_PAD_S <= float(at) <= end + APP_WINDOW_PAD_S:
            return True
    return False


@dataclass(frozen=True, slots=True)
class EarlyCheck:
    released: bool
    skipped: str = ""
    predicted: str = ""
    predicted_index: int | None = None
    pinned_index: int | None = None
    higher_alive: int = 0
    pass_age_s: float | None = None
    stable_s: float | None = None
    pass_app: bool | None = None


def early_release_check(
    *,
    now_wall: float,
    clock: LeaseClock,
    provider: Mapping[str, Any] | None,
    observation: GroupObservation | None,
    test_url: str,
    app_windows: Mapping[str, Sequence[tuple[float, float | None]]] | None,
    stable_required: float | Callable[[str], float],
    incident_member: str = "",
) -> EarlyCheck:
    """Does the predicted native winner ahead of the pin qualify (condition 4)?

    ``stable_required`` is a number, or ``member -> seconds``: the backoff
    belongs to the predicted winner W, not to the lease's incident member.
    """

    members = provider_members(provider)
    by_name = {str(item["name"]): item for item in members}
    order = contract_members(str(item["name"]) for item in members)
    if clock.member not in order:
        return EarlyCheck(False, skipped="early_na_not_provider_member")
    if observation is not None and observation.members:
        if contract_members(observation.members) != order:
            return EarlyCheck(False, skipped="early_na_order_mismatch")
    pinned_index = order.index(clock.member)
    higher = order[:pinned_index]
    alive_higher = [name for name in higher if _member_alive(by_name.get(name), test_url) is True]
    if not alive_higher:
        return EarlyCheck(False, pinned_index=pinned_index, higher_alive=0)
    winner = alive_higher[0]
    winner_index = order.index(winner)
    samples = _history(_member_entry(by_name.get(winner), test_url), now_wall)
    base = EarlyCheck(
        False,
        predicted=winner,
        predicted_index=winner_index,
        pinned_index=pinned_index,
        higher_alive=len(alive_higher),
    )
    if not samples or samples[-1][1] <= 0:
        return base
    last_fail_index = -1
    for index, (_at, delay) in enumerate(samples):
        if delay <= 0:
            last_fail_index = index
    windows = app_windows_for(winner, app_windows)
    tail = samples[last_fail_index + 1 :]
    candidates = [at for at, delay in tail if delay > 0 and not in_app_window(at, windows, now_wall)]
    if not candidates:
        return replace(base, pass_app=True)
    pass_at = candidates[-1]
    floor = max(float(clock.started_wall), float(clock.last_decision_wall))
    if pass_at < floor:
        return replace(base, pass_age_s=max(0.0, float(now_wall) - pass_at), pass_app=False)
    stable_since = samples[last_fail_index][0] if last_fail_index >= 0 else samples[0][0]
    incident = str(incident_member or "")
    if incident and winner == incident:
        # Recovery's in-tunnel failures write no Mihomo history: the
        # incident member's span starts no earlier than the lease itself.
        stable_since = max(stable_since, float(clock.started_wall))
    span = pass_at - stable_since
    result = replace(
        base,
        pass_age_s=max(0.0, float(now_wall) - pass_at),
        stable_s=span,
        pass_app=False,
    )
    required = stable_required(winner) if callable(stable_required) else stable_required
    if span >= float(required):
        return replace(result, released=True)
    return result


def evaluate_route_lease(
    *,
    now_mono: float,
    now_wall: float,
    policy: RouteLeasePolicy,
    clock: LeaseClock,
    lease_route_epoch: int | None,
    observation: GroupObservation | None,
    provider: Mapping[str, Any] | None,
    test_url: str,
    app_windows: Mapping[str, Sequence[tuple[float, float | None]]] | None,
    busy: LeaseBusy,
    core_generation: int,
    stable_required: float | Callable[[str], float],
    incident_member: str = "",
) -> LeaseDecision:
    """One tick's decision for one managed clock; the first match wins.

    ``lease_route_epoch`` is the linked lease dict's epoch, or ``None`` for
    an orphan (a clock with no lease dict, judged by its own PUT epoch).
    """

    due = due_time(clock, policy)
    due_s = due - float(now_mono)
    if clock.ended:
        if clock.purge_pending or clock.verify_retry:
            if busy.hard:
                return LeaseDecision("defer", reason=busy.hard, due_s=due_s)
            return LeaseDecision("purge" if clock.purge_pending else "verify", due_s=due_s)
        return NONE
    # 0. Recovery owns the route: no action; the TTL keeps running.
    if busy.hard:
        return LeaseDecision("defer", reason=busy.hard, due_s=due_s)
    if busy.soft and float(now_mono) < due + SOFT_DEFER_CAP_S:
        return LeaseDecision("defer", reason=busy.soft, due_s=due_s)
    # 1. Ghost lease: Mihomo already dropped or replaced the pick.
    epoch_floor = int(clock.route_epoch if lease_route_epoch is None else lease_route_epoch)
    if (
        observation is not None
        and int(observation.route_epoch) >= epoch_floor
        and int(observation.core_generation) == int(core_generation)
        and float(observation.published_mono) >= float(clock.last_put_mono)
    ):
        if not observation.fixed:
            return LeaseDecision("end", cause="core_cleared", reason="ghost", purge=True, due_s=due_s)
        if observation.fixed != clock.member:
            confirmed = bool(
                clock.mismatch_seq >= 0
                and int(observation.seq) > int(clock.mismatch_seq)
                and float(now_mono) - float(clock.mismatch_mono) >= float(policy.watch_interval_seconds)
            )
            if confirmed:
                return LeaseDecision("end", cause="core_fixed_mismatch", reason="ghost", due_s=due_s)
            return LeaseDecision("mismatch", reason="ghost", due_s=due_s)
    # 2. The pinned member is dead.
    provider_alive: bool | None = None
    if provider is not None:
        for item in provider_members(provider):
            if str(item["name"]) == clock.member:
                provider_alive = _member_alive(item, test_url)
                break
    snapshot_dead = bool(
        observation is not None
        and observation.fixed == clock.member
        and observation.fixed_alive is False
        and int(observation.core_generation) == int(core_generation)
    )
    if provider_alive is False or snapshot_dead:
        return LeaseDecision("release", cause="pinned_dead", due_s=due_s)
    # 3. TTL.
    if float(now_mono) >= due:
        return LeaseDecision("release", cause="ttl", due_s=due_s)
    # 4. Early release: the predicted native winner passed Mihomo's own check.
    if policy.early_release and clock.group == TIER3_GROUP and provider is not None:
        check = early_release_check(
            now_wall=now_wall,
            clock=clock,
            provider=provider,
            observation=observation,
            test_url=test_url,
            app_windows=app_windows,
            stable_required=stable_required,
            incident_member=incident_member,
        )
        if check.released:
            return LeaseDecision(
                "release",
                cause="early",
                trigger=check.predicted,
                trigger_index=check.predicted_index,
                pinned_index=check.pinned_index,
                predicted=check.predicted,
                pass_age_s=check.pass_age_s,
                stable_s=check.stable_s,
                pass_app=check.pass_app,
                higher_alive=check.higher_alive,
                due_s=due_s,
            )
        return LeaseDecision(
            "none",
            skipped=check.skipped,
            pinned_index=check.pinned_index,
            predicted=check.predicted,
            higher_alive=check.higher_alive,
            due_s=due_s,
        )
    return LeaseDecision("none", due_s=due_s)


def member_alive_or_none(proxy: Mapping[str, Any] | None, test_url: str) -> bool | None:
    """``extra[test_url].alive`` (else the global flag); ``None`` if unknown."""

    return _member_alive(proxy, test_url)


class TokenName(str):
    """A member name that the log writer replaces with its route-view token."""


class LeaseDeferred(Exception):
    """A release found recovery busy after it decided: no DELETE was sent."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = str(reason)


def format_lease_fields(fields: Sequence[tuple[str, Any]]) -> str:
    """``k=v`` pairs without spaces in values; ``-`` means none."""

    parts: list[str] = []
    for key, value in fields:
        if value is None or value == "":
            text = "-"
        elif isinstance(value, bool):
            text = "1" if value else "0"
        elif isinstance(value, float):
            text = f"{value:.1f}"
        else:
            text = str(value).replace(" ", "_")
        parts.append(f"{key}={text}")
    return " ".join(parts)
