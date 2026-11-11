"""Pure route-lease rules (lease-v1): policy, due time and every condition."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import runtime_route_lease as rl
from runtime_route_lease import (
    GroupObservation,
    LeaseBusy,
    LeaseClock,
    RouteLeasePolicy,
    evaluate_route_lease,
)

URL = "https://www.gstatic.com/generate_204"
WALL0 = 1_800_000_000.0


def _early_policy(**overrides):
    """early_release is off by default since 11-01; these rule tests opt in."""

    return RouteLeasePolicy.for_tests(**{"early_release": True, **overrides})


def rfc(wall: float) -> str:
    return datetime.fromtimestamp(wall, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def member(name, alive=True, history=()):
    return {
        "name": name,
        "extra": {URL: {"alive": alive, "history": [{"time": rfc(t), "delay": d} for t, d in history]}},
    }


def provider(*members):
    return {"name": rl.TIER3_PROVIDER, "proxies": list(members) + [{"name": "REJECT"}]}


def clock(member_name="B", *, start=0.0, decision=None, group=rl.TIER3_GROUP, **kw):
    decision = start if decision is None else decision
    return LeaseClock(
        group=group,
        member=member_name,
        started_mono=start,
        started_wall=WALL0 + start,
        last_decision_mono=decision,
        last_decision_wall=WALL0 + decision,
        route_epoch=kw.pop("route_epoch", 5),
        core_generation=1,
        **kw,
    )


def obs(fixed="B", *, alive=True, members=("A", "B", "C", "REJECT"), epoch=5, generation=1, seq=1, published=0.0):
    return GroupObservation(
        group=rl.TIER3_GROUP,
        fixed=fixed,
        fixed_alive=alive if fixed else None,
        members=tuple(members),
        test_url=URL,
        route_epoch=epoch,
        core_generation=generation,
        seq=seq,
        published_mono=published,
    )


def decide(now, c, *, policy=None, observation=None, prov=None, busy=LeaseBusy(), windows=None,
           stable=110.0, incident="", lease_epoch=5, generation=1):
    return evaluate_route_lease(
        now_mono=now,
        now_wall=WALL0 + now,
        policy=policy or _early_policy(),
        clock=c,
        lease_route_epoch=lease_epoch,
        observation=observation,
        provider=prov,
        test_url=URL,
        app_windows=windows or {},
        busy=busy,
        core_generation=generation,
        stable_required=stable,
        incident_member=incident,
    )


# ---------------------------------------------------------------- policy


def test_policy_defaults_are_off_until_validation():
    # 10-28 user decision: the lease ships on by default; enabled: false is off.
    assert RouteLeasePolicy.from_config({}) == RouteLeasePolicy(enabled=True)
    assert RouteLeasePolicy.from_config(None).enabled is True
    off = RouteLeasePolicy.from_config({"client": {"egress_recovery": {"route_lease": {"enabled": False}}}})
    assert off == RouteLeasePolicy.disabled()
    on = RouteLeasePolicy.from_config({"client": {"egress_recovery": {"route_lease": {"enabled": True}}}})
    assert on == RouteLeasePolicy(enabled=True)
    assert (on.ttl_seconds, on.early_release, on.stable_seconds, on.watch_interval_seconds, on.death_carry_seconds) == (
        600.0, False, 110.0, 10.0, 60.0,
    )
    # 11-01 user decision (ship3 B-7): early_release is off by default.
    assert RouteLeasePolicy.from_config({}).early_release is False


@pytest.mark.parametrize(
    "key,value,expected",
    [
        ("ttl_seconds", 5, 60.0),
        ("ttl_seconds", 99999, 3600.0),
        ("ttl_seconds", "junk", 600.0),
        ("ttl_seconds", True, 600.0),
        ("stable_seconds", -1, 0.0),
        ("stable_seconds", 900, 600.0),
        ("watch_interval_seconds", 1, 5.0),
        ("watch_interval_seconds", 60, 30.0),
        ("death_carry_seconds", 1000, 300.0),
        ("death_carry_seconds", float("nan"), 60.0),
    ],
)
def test_policy_clamps_and_junk(key, value, expected):
    config = {"client": {"egress_recovery": {"route_lease": {"enabled": "true", key: value}}}}
    assert getattr(RouteLeasePolicy.from_config(config), key) == expected


def test_policy_is_independent_of_node_judgement_and_for_tests_bypasses_clamps():
    config = {"client": {"egress_recovery": {"node_judgement": {"enabled": False}, "route_lease": {"enabled": True}}}}
    assert RouteLeasePolicy.from_config(config).enabled is True
    assert RouteLeasePolicy.for_tests(ttl_seconds=5).ttl_seconds == 5
    assert _early_policy().witness()["rules"] == "lease-v1"


def test_lease_identifier_is_never_all_digits():
    ident = rl.lease_identifier("123")
    assert ident.startswith("L") and len(ident) == 12


# ---------------------------------------------------------------- TTL and due


def test_ttl_fires_at_due_even_without_an_alive_higher_member():
    c = clock()
    prov = provider(member("A", alive=False), member("B"), member("C"))
    assert decide(599.0, c, prov=prov).action == "none"
    decision = decide(600.0, c, prov=prov)
    assert (decision.action, decision.cause) == ("release", "ttl")


def test_due_is_never_within_120_s_of_the_last_decision():
    c = clock(start=0.0, decision=595.0)
    assert rl.due_time(c, _early_policy()) == 715.0
    assert decide(700.0, c).action == "none"
    assert decide(715.0, c).cause == "ttl"


# ---------------------------------------------------------------- early


def test_early_release_when_predicted_winner_is_stable():
    c = clock(start=0.0)
    prov = provider(
        member("A", history=[(WALL0 + 5, 0), (WALL0 + 115, 120)]),
        member("B"),
        member("C"),
    )
    decision = decide(120.0, c, prov=prov)
    assert (decision.action, decision.cause, decision.trigger) == ("release", "early", "A")
    assert decision.trigger_index == 0 and decision.pinned_index == 1
    assert decision.stable_s == pytest.approx(110.0)
    assert decision.pass_app is False


def test_fail_exactly_stable_seconds_before_the_pass_qualifies_and_less_does_not():
    c = clock(start=0.0)
    ok = provider(member("A", history=[(WALL0 + 10, 0), (WALL0 + 120, 80)]), member("B"))
    short = provider(member("A", history=[(WALL0 + 11, 0), (WALL0 + 120, 80)]), member("B"))
    assert decide(130.0, c, prov=ok).cause == "early"
    assert decide(130.0, c, prov=short).action == "none"


def test_qualifying_lower_member_behind_unstable_alive_higher_member_never_releases():
    c = clock("C", start=0.0)
    prov = provider(
        member("A", history=[(WALL0 + 100, 0), (WALL0 + 120, 90)]),  # predicted winner, unstable
        member("B", history=[(WALL0 + 1, 0), (WALL0 + 150, 90)]),  # stable, but not native's pick
        member("C"),
    )
    decision = decide(160.0, c, prov=prov)
    assert decision.action == "none"
    assert decision.predicted == "A"


def test_pass_before_lease_start_or_last_decision_does_not_count():
    prov = provider(member("A", history=[(WALL0 - 300, 0), (WALL0 + 50, 90)]), member("B"))
    assert decide(130.0, clock(start=0.0), prov=prov).cause == "early"
    assert decide(130.0, clock(start=0.0, decision=60.0), prov=prov).action == "none"
    assert decide(130.0, clock(start=60.0), prov=prov).action == "none"


def test_incident_member_span_starts_no_earlier_than_the_lease():
    prov = provider(member("A", history=[(WALL0 + 100, 90)]), member("B"))
    c = clock(start=0.0)
    # No failure in history at all: for another member the span is the
    # earliest entry; for the incident member it starts at the lease.
    assert decide(105.0, c, prov=prov, incident="A").action == "none"
    prov2 = provider(member("A", history=[(WALL0 - 50, 90), (WALL0 + 112, 90)]), member("B"))
    assert decide(120.0, c, prov=prov2, incident="").cause == "early"
    assert decide(120.0, c, prov=prov2, incident="A").cause == "early"
    prov3 = provider(member("A", history=[(WALL0 - 50, 90), (WALL0 + 100, 90)]), member("B"))
    assert decide(105.0, c, prov=prov3, incident="").cause == "early"
    assert decide(105.0, c, prov=prov3, incident="A").action == "none"


def test_backoff_doubles_the_stable_requirement_with_cap():
    policy = _early_policy()
    assert [rl.stable_requirement(policy, level) for level in range(5)] == [110.0, 220.0, 440.0, 600.0, 600.0]
    prov = provider(member("A", history=[(WALL0 + 1, 0), (WALL0 + 150, 90)]), member("B"))
    c = clock(start=0.0)
    assert decide(160.0, c, prov=prov, stable=110.0).cause == "early"
    assert decide(160.0, c, prov=prov, stable=220.0).action == "none"


def test_app_originated_pass_is_ignored_but_app_failure_counts():
    c = clock(start=0.0)
    prov = provider(member("A", history=[(WALL0 + 1, 0), (WALL0 + 130, 90)]), member("B"))
    windows = {"A": [(WALL0 + 129.9, WALL0 + 130.2)]}
    decision = decide(140.0, c, prov=prov, windows=windows)
    assert decision.action == "none"
    group_window = {"group:TIER3-AUTO": [(WALL0 + 129.0, None)]}
    assert decide(140.0, c, prov=prov, windows=group_window).action == "none"
    # An app probe's failure (inside a window) still resets the span.
    prov2 = provider(member("A", history=[(WALL0 + 100, 0), (WALL0 + 130, 90)]), member("B"))
    assert decide(140.0, c, prov=prov2, windows={"A": [(WALL0 + 99.9, WALL0 + 100.1)]}).action == "none"


def test_lower_priority_and_reject_entries_are_ignored():
    c = clock("A", start=0.0)
    prov = provider(member("A"), member("B", history=[(WALL0 + 1, 0), (WALL0 + 150, 90)]))
    assert decide(160.0, c, prov=prov).action == "none"


def test_single_entry_history_after_a_reset_waits_for_the_full_span():
    c = clock(start=0.0)
    prov = provider(member("A", history=[(WALL0 + 100, 90)]), member("B"))
    assert decide(105.0, c, prov=prov).action == "none"
    prov2 = provider(member("A", history=[(WALL0 + 100, 90), (WALL0 + 210, 90)]), member("B"))
    assert decide(215.0, c, prov=prov2).cause == "early"


def test_future_dated_entries_are_ignored():
    c = clock(start=0.0)
    prov = provider(member("A", history=[(WALL0 + 1, 0), (WALL0 + 400, 90)]), member("B"))
    assert decide(130.0, c, prov=prov).action == "none"


def test_stable_zero_is_the_literal_rule():
    c = clock(start=0.0)
    prov = provider(member("A", history=[(WALL0 + 50, 0), (WALL0 + 51, 90)]), member("B"))
    policy = _early_policy(stable_seconds=0.0)
    assert decide(60.0, c, prov=prov, policy=policy, stable=0.0).cause == "early"


def test_missing_member_or_order_mismatch_disables_early_but_not_ttl():
    c = clock("B", start=0.0)
    prov = provider(member("A", history=[(WALL0 + 1, 0), (WALL0 + 150, 90)]), member("C"))
    assert decide(160.0, c, prov=prov).skipped == "early_na_not_provider_member"
    assert decide(600.0, c, prov=prov).cause == "ttl"
    prov2 = provider(member("A", history=[(WALL0 + 1, 0), (WALL0 + 150, 90)]), member("B"))
    mismatch = obs(members=("B", "A", "REJECT"))
    assert decide(160.0, c, prov=prov2, observation=mismatch).skipped == "early_na_order_mismatch"


def test_early_release_false_gives_ttl_and_ghost_only():
    c = clock(start=0.0)
    prov = provider(member("A", history=[(WALL0 + 1, 0), (WALL0 + 150, 90)]), member("B"))
    policy = _early_policy(early_release=False)
    assert decide(160.0, c, prov=prov, policy=policy).action == "none"
    assert decide(600.0, c, prov=prov, policy=policy).cause == "ttl"
    assert decide(160.0, c, policy=policy, observation=obs(fixed="")).cause == "core_cleared"


# ---------------------------------------------------------------- dead/ghost/busy


def test_pinned_dead_releases():
    c = clock(start=0.0)
    prov = provider(member("A", alive=False), member("B", alive=False))
    assert decide(30.0, c, prov=prov).cause == "pinned_dead"
    assert decide(30.0, c, observation=obs(fixed="B", alive=False)).cause == "pinned_dead"


def test_fixed_none_is_a_ghost_end_with_purge():
    decision = decide(30.0, clock(), observation=obs(fixed=""))
    assert (decision.action, decision.cause, decision.purge) == ("end", "core_cleared", True)


def test_core_fixed_mismatch_needs_two_snapshots_an_interval_apart():
    c = clock()
    first = decide(30.0, c, observation=obs(fixed="C", seq=3))
    assert first.action == "mismatch"
    c.mismatch_seq, c.mismatch_mono = 3, 30.0
    assert decide(35.0, c, observation=obs(fixed="C", seq=4)).action == "mismatch"
    assert decide(41.0, c, observation=obs(fixed="C", seq=3)).action == "mismatch"
    decision = decide(41.0, c, observation=obs(fixed="C", seq=4))
    assert (decision.action, decision.cause, decision.purge) == ("end", "core_fixed_mismatch", False)
    busy = decide(41.0, c, observation=obs(fixed="C", seq=4), busy=LeaseBusy(hard="recovery_worker"))
    assert busy.action == "defer"


def test_stale_snapshots_are_ignored():
    c = clock()
    assert decide(30.0, c, observation=obs(fixed="", epoch=4)).action == "none"
    assert decide(30.0, c, observation=obs(fixed="", generation=2)).action == "none"
    late = clock(last_put_mono=20.0)
    assert decide(30.0, late, observation=obs(fixed="", published=10.0)).action == "none"


def test_hard_reasons_always_defer_soft_reasons_stop_at_due_plus_120():
    c = clock(start=0.0)
    hard = LeaseBusy(hard="recovery_worker")
    assert decide(5000.0, c, busy=hard).action == "defer"
    soft = LeaseBusy(soft="probe_binding")
    assert decide(700.0, c, busy=soft).action == "defer"
    assert decide(720.0, c, busy=soft).cause == "ttl"


def test_ended_clock_purges_or_verifies():
    c = clock()
    c.ended, c.purge_pending = "core_cleared", True
    assert decide(10.0, c).action == "purge"
    assert decide(10.0, c, busy=LeaseBusy(hard="core_down")).action == "defer"
    c.purge_pending, c.verify_retry = False, True
    assert decide(10.0, c).action == "verify"
    c.verify_retry = False
    assert decide(10.0, c).action == "none"


def test_orphan_uses_its_own_put_epoch():
    c = clock(route_epoch=9)
    assert decide(30.0, c, observation=obs(fixed="", epoch=8), lease_epoch=None).action == "none"
    assert decide(30.0, c, observation=obs(fixed="", epoch=9), lease_epoch=None).cause == "core_cleared"


def test_format_fields_has_no_spaces_and_dash_for_none():
    text = rl.format_lease_fields([("a", None), ("b", True), ("c", 1.25), ("d", "x y"), ("e", 3)])
    assert text == "a=- b=1 c=1.2 d=x_y e=3"
