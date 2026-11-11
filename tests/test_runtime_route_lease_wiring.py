"""Route lease (lease-v1) wiring in DesktopRuntime, with a fake controller.

Time is injected through ``_route_lease_clock`` and ``_route_lease_tick``;
no test waits on the 10 s watch interval.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import runtime_route_lease as rl
from desktop_runtime import DesktopRuntime
from runtime_egress_recovery import (
    CoreLoadedGroupState,
    CoreLoadedRouteSnapshot,
    RuntimeNodeRef,
    RuntimeRouteBinding,
)
from runtime_route_lease import RouteLeasePolicy

URL = "https://www.gstatic.com/generate_204"
WALL0 = 1_800_000_000.0
MEMBERS = ("A", "B", "C")


def rfc(wall: float) -> str:
    return datetime.fromtimestamp(wall, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class FakeTime:
    def __init__(self) -> None:
        self.mono = 0.0

    def __call__(self) -> tuple[float, float]:
        return self.mono, WALL0 + self.mono


class FakeClient:
    """Mihomo controller: GET/DELETE on TIER3-AUTO and the provider read."""

    base_url = "http://127.0.0.1:59090"

    def __init__(self, state: dict) -> None:
        self.state = state
        self.calls: list[tuple[str, str]] = []
        self.delete_error: Exception | None = None
        self.readback_after_delete: dict | None = None
        self.provider_error: Exception | None = None

    def proxy(self, group, timeout=None):
        self.calls.append(("GET", group))
        if self.readback_after_delete is not None and ("DELETE", group) in self.calls:
            return dict(self.readback_after_delete)
        return {"fixed": self.state["fixed"], "now": self.state["now"], "all": list(MEMBERS) + ["REJECT"]}

    def unfix_proxy(self, group, timeout=None):
        self.calls.append(("DELETE", group))
        if self.delete_error is not None:
            raise self.delete_error
        self.state["fixed"] = ""
        self.state["now"] = self.state["native"]
        return {"group": group, "fixed": False}

    def provider_proxies_one(self, name, timeout=None):
        self.calls.append(("PROVIDER", name))
        if self.provider_error is not None:
            raise self.provider_error
        return self.state["provider"]

    def _forbidden(self, name):
        def call(*_args, **_kwargs):
            self.calls.append(("FORBIDDEN", name))
            raise AssertionError(f"route lease must never call {name}")
        return call

    def __getattr__(self, name):
        if name in {"proxies", "group_delay", "proxy_delay", "health_check_provider", "providers_proxies"}:
            return self._forbidden(name)
        raise AttributeError(name)

    def select_proxy_with_readback(self, group, target, **_kwargs):
        self.calls.append(("PUT", group))
        self.state["fixed"] = target
        self.state["now"] = target
        return {"commit_state": "committed", "now": target, "fixed": target}


def provider_payload(alive=("A", "B", "C"), history=None):
    history = history or {}
    proxies = []
    for name in MEMBERS:
        proxies.append(
            {
                "name": name,
                "extra": {
                    URL: {
                        "alive": name in alive,
                        "history": [{"time": rfc(WALL0 + t), "delay": d} for t, d in history.get(name, ())],
                    }
                },
            }
        )
    proxies.append({"name": "REJECT"})
    return {"name": rl.TIER3_PROVIDER, "proxies": proxies}


def build_runtime(*, enabled=True, early_release=True, extra=None):
    runtime = object.__new__(DesktopRuntime)
    runtime.stop_event = threading.Event()
    runtime.logger = logging.getLogger("test-route-lease")
    block = {"enabled": enabled, "early_release": early_release}
    block.update(extra or {})
    runtime.config = {"client": {"egress_recovery": {"route_lease": block}}}
    runtime._runtime_egress_recovery_profile = "early_single_coordinator"
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_route_snapshot_lock = threading.Lock()
    runtime._core_transition_lock = threading.RLock()
    runtime._runtime_selector_intent_revision = 1
    runtime._runtime_route_epoch = 5
    runtime._core_generation = 1
    runtime._runtime_generation = 1
    runtime._runtime_failure_recovery_bindings = set()
    runtime._runtime_failure_probe_bindings = set()
    runtime._runtime_passive_failback_requests = {}
    runtime._runtime_failure_recovery_threads = set()
    runtime._runtime_origin_diagnostic_bindings = {}
    runtime._runtime_recheck_retry_attempts = {}
    runtime._runtime_pending_recovered_payloads = {}
    runtime._runtime_incident_recovery_state = {}
    runtime._runtime_confirmed_fail_closed_snapshot_key = None
    runtime._runtime_route_mutation_lease = None
    runtime._runtime_last_verified_route = None
    runtime._route_lease_lock = threading.Lock()
    runtime._route_lease_event = threading.Event()
    runtime._route_lease_thread = None
    runtime._route_lease_thread_wanted = False
    runtime._route_lease_clocks = {}
    runtime._route_lease_obs = {}
    runtime._route_lease_backoff = {}
    runtime._route_lease_winners = {}
    runtime._route_lease_counters = {}
    runtime._route_lease_log_queue = []
    fake_time = FakeTime()
    runtime._route_lease_clock = fake_time
    state = {"fixed": "", "now": "A", "native": "A", "provider": provider_payload(), "unbound": False}
    client = FakeClient(state)
    runtime.api_client = client
    runtime._require_api_client = lambda: client
    runtime.core_runner = SimpleNamespace(is_running=lambda: True, status=lambda: {"pid": 77})
    runtime._runtime_initial_activation_was_completed = lambda: True
    runtime._tier3_initial_activation_transaction_active = lambda: False
    runtime._runtime_worker_token = lambda: (1,)
    runtime._runtime_worker_token_is_current = lambda token: token == (1,) and not runtime.stop_event.is_set()
    runtime._runtime_recovery_now = time.monotonic
    runtime._mark_runtime_route_attribution_barrier_locked = lambda: None
    arm_sites: list[str] = []
    runtime._node_judgement_arm_epoch = lambda site: arm_sites.append(site)
    runtime._bounded_core_transition = lambda *_args: nullcontext()
    transactions: list[str] = []

    def transaction(*_args, **_kwargs):
        transactions.append("txn")
        return nullcontext(state.get("txn", object()))

    runtime._runtime_egress_coordinator = SimpleNamespace(
        core_mutation_transaction=transaction,
        set_current_snapshot=lambda *_args: None,
    )
    calls: list = []

    def snapshot_now():
        group = CoreLoadedGroupState(
            name=rl.TIER3_GROUP,
            type_name="Fallback",
            members=MEMBERS + ("REJECT",),
            now=state["now"],
            fixed=state["fixed"],
            test_url=URL,
        )
        return CoreLoadedRouteSnapshot(
            process_pid=77,
            process_generation=1,
            core_generation=int(runtime._core_generation),
            semantic_digest="d",
            route_epoch=int(runtime._runtime_route_epoch),
            group_states=(group,),
        )

    def payload_now():
        entries = {name: {"name": name, "extra": {URL: {"alive": True}}} for name in MEMBERS}
        entries[rl.TIER3_GROUP] = {"now": state["now"], "fixed": state["fixed"], "all": list(MEMBERS)}
        return {"proxies": {"proxies": entries}}

    def refresh(**_kwargs):
        calls.append("refresh")
        snapshot = snapshot_now()
        runtime._runtime_route_snapshot = snapshot
        runtime._route_lease_observe_snapshot(snapshot, payload_now())
        return snapshot

    runtime._runtime_route_snapshot = None
    runtime._refresh_runtime_route_snapshot = refresh
    runtime._runtime_route_snapshot_state = lambda: (snapshot_now(), payload_now())
    node = RuntimeNodeRef("tier3", "tier3b", "id", "A")

    def bound(_snapshot, _payload, outbound, **_kwargs):
        if state["unbound"]:
            return None
        return RuntimeRouteBinding(
            process_pid=77, process_generation=1, core_generation=1, semantic_digest="d",
            route_epoch=int(runtime._runtime_route_epoch), observed_outbound=outbound,
            route_chain=(outbound, rl.TIER3_GROUP, state["now"]), node=node,
        )

    runtime._runtime_route_binding_for_outbound = bound
    runtime._verify_runtime_final_exit = lambda **_kwargs: calls.append("204") or {"status": 204}
    runtime._queue_runtime_route_snapshot_refresh = lambda: calls.append("queue_refresh")
    runtime._set_effective_egress_state = lambda ok, reason="": calls.append(("egress", ok))
    runtime._try_arm_runtime_recovery = lambda: calls.append("arm") or True
    runtime._publish_verified_runtime_source_proof = lambda **_kwargs: calls.append("proof") or True
    runtime._disarm_runtime_recovery = lambda reason="": calls.append("disarm")
    runtime.test = SimpleNamespace(
        time=fake_time, state=state, client=client, calls=calls, arm_sites=arm_sites,
        transactions=transactions, refresh=refresh,
    )
    return runtime


def pin(runtime, member="B", group=rl.TIER3_GROUP):
    """The real writer: PUT plus readback, then the note hook."""

    runtime.test.state["fixed"] = member
    runtime.test.state["now"] = member
    result = runtime._apply_runtime_automatic_selection(
        {"group": group, "target": member, "mode": "fixed"},
        deadline_monotonic=time.monotonic() + 5.0,
    )
    # Recovery reads the route back after its PUT (publishes the snapshot).
    runtime.test.refresh()
    return result


def publish_lease(runtime, member="B", *, incident="incident-1", previous="A", group=rl.TIER3_GROUP):
    lease = {
        "incident_id": incident,
        "owner_token": "owner",
        "group": group,
        "mode": "fixed",
        "from": previous,
        "to": member,
        "target_member": member,
        "previous_member": previous,
        "route_epoch": int(runtime._runtime_route_epoch),
        "selector_intent_revision": int(runtime._runtime_selector_intent_revision),
        "verified_at_monotonic": time.monotonic(),
        "release_state": "pending",
        "automatic": True,
    }
    with runtime._runtime_failure_monitor_lock:
        runtime._runtime_route_mutation_lease = lease
    return lease


def at(runtime, mono):
    runtime.test.time.mono = float(mono)
    return runtime._route_lease_tick()


def controller_calls(runtime, *kinds):
    return [call for call in runtime.test.client.calls if call[0] in kinds]


def lease_lines(caplog, kind):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(f"Runtime route lease {kind} ")]


# ---------------------------------------------------------------- clocks


def test_fixed_commit_creates_a_clock_and_renew_keeps_the_start():
    runtime = build_runtime()
    pin(runtime, "B")
    clock = runtime._route_lease_clocks[rl.TIER3_GROUP]
    assert (clock.member, clock.started_mono, clock.origin) == ("B", 0.0, "recovery")
    runtime.test.time.mono = 50.0
    pin(runtime, "B")
    assert runtime._route_lease_clocks[rl.TIER3_GROUP] is clock
    assert clock.started_mono == 0.0 and clock.last_put_mono == 50.0
    runtime.test.time.mono = 60.0
    pin(runtime, "C")
    replaced = runtime._route_lease_clocks[rl.TIER3_GROUP]
    assert replaced is not clock and replaced.member == "C" and replaced.started_mono == 60.0


def test_selector_writes_and_disabled_policy_never_create_clocks():
    runtime = build_runtime()
    runtime._route_lease_note_pin({"group": "FINAL-EXIT", "target": "X", "mode": "selector"}, {"commit_state": "committed"})
    assert runtime._route_lease_clocks == {}
    off = build_runtime(enabled=False)
    pin(off, "B")
    assert off._route_lease_clocks == {}
    assert off.test.client.calls == [("PUT", rl.TIER3_GROUP)]


def test_note_hook_does_no_io_and_starts_no_thread_under_the_monitor_lock():
    runtime = build_runtime()
    before = threading.active_count()
    with runtime._runtime_failure_monitor_lock:
        runtime._route_lease_note_pin({"group": rl.TIER3_GROUP, "target": "B", "mode": "fixed"}, {"commit_state": "committed"})
    assert runtime.test.client.calls == []
    assert threading.active_count() == before
    assert runtime._route_lease_thread_wanted is True
    assert runtime._route_lease_event.is_set()


def test_death_end_then_repin_within_carry_keeps_the_start():
    runtime = build_runtime()
    pin(runtime, "B")
    publish_lease(runtime, "B")
    at(runtime, 1.0)
    clock = runtime._route_lease_clocks[rl.TIER3_GROUP]
    runtime.test.state.update(fixed="", now="A")
    runtime.test.refresh()
    at(runtime, 100.0)
    assert clock.ended == "core_cleared"
    runtime.test.time.mono = 150.0
    pin(runtime, "B")
    assert clock.live and clock.started_mono == 0.0 and clock.last_decision_mono == 150.0
    publish_lease(runtime, "B", incident="incident-2")
    at(runtime, 151.0)
    # After the carry window a re-pin starts a fresh clock.
    runtime.test.state.update(fixed="", now="A")
    runtime.test.refresh()
    at(runtime, 160.0)
    assert clock.ended == "core_cleared"
    runtime.test.time.mono = 400.0
    pin(runtime, "B")
    assert runtime._route_lease_clocks[rl.TIER3_GROUP].started_mono == 400.0


def test_ttl_end_then_repin_starts_fresh():
    runtime = build_runtime()
    pin(runtime, "B")
    publish_lease(runtime, "B")
    at(runtime, 1.0)
    at(runtime, 601.0)
    clock = runtime._route_lease_clocks[rl.TIER3_GROUP]
    assert clock.ended == "released"
    runtime.test.time.mono = 610.0
    pin(runtime, "B")
    assert runtime._route_lease_clocks[rl.TIER3_GROUP].started_mono == 610.0


# ---------------------------------------------------------------- release


def test_ttl_release_is_readback_first_one_delete_then_verify(caplog):
    caplog.set_level(logging.INFO, logger="test-route-lease")
    runtime = build_runtime()
    pin(runtime, "B")
    lease = publish_lease(runtime, "B")
    runtime.test.client.calls.clear()
    at(runtime, 10.0)
    assert controller_calls(runtime, "GET", "DELETE") == []
    at(runtime, 600.0)
    assert controller_calls(runtime, "GET", "DELETE") == [
        ("GET", rl.TIER3_GROUP), ("DELETE", rl.TIER3_GROUP), ("GET", rl.TIER3_GROUP),
    ]
    assert "204" in runtime.test.calls
    assert runtime._runtime_route_mutation_lease is None
    assert lease["release_state"] == "released"
    assert runtime.test.arm_sites[-1] == "release_runtime_automatic_fixed_selection"
    release = lease_lines(caplog, "release")
    assert len(release) == 1 and "cause=ttl" in release[0] and "deletes=1" in release[0] and "verify=ok" in release[0]
    assert any("cause=released" in line and "by=ttl" in line for line in lease_lines(caplog, "end"))
    assert ("FORBIDDEN", "proxies") not in runtime.test.client.calls


def test_early_release_on_a_stable_non_app_pass_of_the_predicted_winner(caplog):
    caplog.set_level(logging.INFO, logger="test-route-lease")
    runtime = build_runtime()
    pin(runtime, "B")
    publish_lease(runtime, "B", previous="C")
    at(runtime, 1.0)
    runtime.test.state["provider"] = provider_payload(history={"A": [(2.0, 0), (115.0, 90)]})
    at(runtime, 116.0)
    assert ("DELETE", rl.TIER3_GROUP) in runtime.test.client.calls
    line = lease_lines(caplog, "release")[0]
    assert "cause=early" in line and "trigger_index=0" in line and "pinned_index=1" in line


def test_native_winner_other_than_the_trigger_never_gets_a_put():
    runtime = build_runtime()
    pin(runtime, "B")
    publish_lease(runtime, "B", previous="C")
    at(runtime, 1.0)
    runtime.test.state["native"] = "C"
    runtime.test.state["provider"] = provider_payload(history={"A": [(2.0, 0), (115.0, 90)]})
    runtime.test.client.calls.clear()
    at(runtime, 116.0)
    assert ("PUT", rl.TIER3_GROUP) not in runtime.test.client.calls
    assert runtime._runtime_route_mutation_lease is None


def test_unknown_readback_never_sends_a_second_delete_in_that_attempt():
    runtime = build_runtime()
    pin(runtime, "B")
    lease = publish_lease(runtime, "B")
    at(runtime, 1.0)
    runtime.test.client.readback_after_delete = {"now": "B"}  # no ``fixed`` field
    runtime.test.client.calls.clear()
    at(runtime, 600.0)
    assert controller_calls(runtime, "DELETE") == [("DELETE", rl.TIER3_GROUP)]
    assert lease["release_state"] == "readback_pending"
    assert runtime._runtime_route_mutation_lease is lease
    # Next tick reconciles readback-first: the pick is already gone.
    runtime.test.client.readback_after_delete = None
    runtime.test.client.calls.clear()
    at(runtime, 601.0)
    assert controller_calls(runtime, "DELETE") == []
    assert runtime._runtime_route_mutation_lease is None


def test_not_committed_retries_three_times_fast_then_every_60_s():
    runtime = build_runtime()
    pin(runtime, "B")
    publish_lease(runtime, "B")
    at(runtime, 1.0)
    runtime.test.client.readback_after_delete = {"fixed": "B", "now": "B"}

    def unfix(group, timeout=None):
        runtime.test.client.calls.append(("DELETE", group))
        return {}

    runtime.test.client.unfix_proxy = unfix
    for tick in (600.0, 610.0, 620.0):
        runtime.test.client.calls = [c for c in runtime.test.client.calls if c[0] != "DELETE"]
        runtime.test.client.readback_after_delete = {"fixed": "B", "now": "B"}
        at(runtime, tick)
    clock = runtime._route_lease_clocks[rl.TIER3_GROUP]
    assert clock.attempts == 3
    assert clock.next_attempt_mono == pytest.approx(680.0)
    at(runtime, 630.0)
    assert clock.attempts == 3


def test_verify_failure_still_ends_the_lease_and_retries_verification_once():
    runtime = build_runtime()
    pin(runtime, "B")
    publish_lease(runtime, "B")
    at(runtime, 1.0)

    def fail_verify(**_kwargs):
        runtime.test.calls.append("204-failed")
        raise RuntimeError("no 204")

    runtime._verify_runtime_final_exit = fail_verify
    at(runtime, 600.0)
    assert runtime._runtime_route_mutation_lease is None
    assert ("egress", None) in runtime.test.calls
    assert ("PUT", rl.TIER3_GROUP) not in runtime.test.client.calls[1:]
    clock = runtime._route_lease_clocks[rl.TIER3_GROUP]
    assert clock.verify_retry is True
    runtime._verify_runtime_final_exit = lambda **_kwargs: runtime.test.calls.append("204-retry") or {"status": 204}
    at(runtime, 610.0)
    assert "204-retry" in runtime.test.calls and ("egress", True) in runtime.test.calls
    assert clock.verify_retry is False
    runtime.test.calls.clear()
    at(runtime, 620.0)
    assert "204-retry" not in runtime.test.calls


def test_unbound_final_exit_releases_without_verify_or_egress_change():
    runtime = build_runtime()
    pin(runtime, "B")
    publish_lease(runtime, "B")
    at(runtime, 1.0)
    runtime.test.state["unbound"] = True
    at(runtime, 600.0)
    assert ("DELETE", rl.TIER3_GROUP) in runtime.test.client.calls
    assert "204" not in runtime.test.calls
    assert not [c for c in runtime.test.calls if isinstance(c, tuple) and c[0] == "egress"]
    assert runtime.test.transactions == []


def test_fail_closed_defers_lease_and_orphan_releases():
    runtime = build_runtime()
    pin(runtime, "B")
    publish_lease(runtime, "B")
    at(runtime, 1.0)
    runtime.test.state["unbound"] = True
    runtime._runtime_confirmed_fail_closed_snapshot_key = ("closed",)
    at(runtime, 600.0)
    assert controller_calls(runtime, "DELETE") == []
    assert runtime._runtime_route_mutation_lease is not None
    # An orphan is deferred while egress is fail-closed, before any read.
    orphan = build_runtime()
    pin(orphan, "B")
    orphan._runtime_confirmed_fail_closed_snapshot_key = ("closed",)
    at(orphan, 31.0)
    at(orphan, 700.0)
    assert controller_calls(orphan, "GET", "DELETE") == []


def test_recovery_binding_inserted_after_decision_defers_without_delete(caplog):
    caplog.set_level(logging.INFO, logger="test-route-lease")
    runtime = build_runtime()
    pin(runtime, "B")
    publish_lease(runtime, "B")
    at(runtime, 1.0)
    original = runtime._refresh_runtime_route_snapshot

    def refresh_then_recovery(**kwargs):
        snapshot = original(**kwargs)
        runtime._runtime_failure_recovery_bindings.add(("worker",))
        return snapshot

    runtime._refresh_runtime_route_snapshot = refresh_then_recovery
    at(runtime, 600.0)
    assert controller_calls(runtime, "DELETE") == []
    assert any("result=busy" in line for line in lease_lines(caplog, "release"))
    assert any("state=begin" in line and "reason=recovery_worker" in line for line in lease_lines(caplog, "deferred"))


@pytest.mark.parametrize(
    "setup,reason",
    [
        (lambda r: r._runtime_failure_recovery_bindings.add(("w",)), "recovery_worker"),
        (lambda r: setattr(r, "_runtime_initial_activation_was_completed", lambda: False), "not_ready"),
        (lambda r: setattr(r, "_tier3_initial_activation_transaction_active", lambda: True), "activation"),
        (lambda r: r._runtime_failure_probe_bindings.add((77, 1, 1, "d", 5, 0)), "probe_binding"),
    ],
)
def test_deferral_reasons_log_begin_and_end_with_deferred_s(caplog, setup, reason):
    caplog.set_level(logging.INFO, logger="test-route-lease")
    runtime = build_runtime()
    pin(runtime, "B")
    publish_lease(runtime, "B")
    runtime._runtime_route_snapshot = runtime._refresh_runtime_route_snapshot()
    at(runtime, 1.0)
    setup(runtime)
    at(runtime, 600.0)
    assert controller_calls(runtime, "DELETE") == []
    begin = [line for line in lease_lines(caplog, "deferred") if "state=begin" in line]
    assert begin and f"reason={reason}" in begin[0]
    runtime._runtime_failure_recovery_bindings.clear()
    runtime._runtime_failure_probe_bindings.clear()
    runtime._runtime_initial_activation_was_completed = lambda: True
    runtime._tier3_initial_activation_transaction_active = lambda: False
    at(runtime, 640.0)
    end = [line for line in lease_lines(caplog, "deferred") if "state=end" in line]
    assert end and "deferred_s=40.0" in end[0]
    assert controller_calls(runtime, "DELETE") == [("DELETE", rl.TIER3_GROUP)]


@pytest.mark.parametrize("ladder_on", [True, False])
def test_track_h_ladder_in_flight_defers_only_with_tier_ladder_judge(caplog, ladder_on):
    """Tier ladder Track H (spec_tier_ladder_v1 §5.2 L5): a ladder in flight is
    a hard deferral ``post_dial_ladder``; ``ladder_end`` wakes the watcher."""

    caplog.set_level(logging.INFO, logger="test-route-lease")
    runtime = build_runtime()
    runtime.config["client"]["egress_recovery"]["node_judgement"] = {
        "post_dial_judge": "act",
        "tier_ladder_judge": ladder_on,
    }
    in_flight = {"value": True}
    runtime._node_judgement_runtime_state = SimpleNamespace(
        judge=SimpleNamespace(ladder_in_flight=lambda: in_flight["value"]),
    )
    pin(runtime, "B")
    publish_lease(runtime, "B")
    runtime._runtime_route_snapshot = runtime._refresh_runtime_route_snapshot()
    at(runtime, 1.0)
    assert runtime._route_lease_busy(runtime._runtime_route_mutation_lease).hard == (
        "post_dial_ladder" if ladder_on else ""
    )
    at(runtime, 600.0)
    if not ladder_on:
        # Key off: byte-identical lease behaviour (the TTL release runs).
        assert controller_calls(runtime, "DELETE") == [("DELETE", rl.TIER3_GROUP)]
        assert not [line for line in lease_lines(caplog, "deferred") if "post_dial_ladder" in line]
        return
    assert controller_calls(runtime, "DELETE") == []
    begin = [line for line in lease_lines(caplog, "deferred") if "state=begin" in line]
    assert begin and "reason=post_dial_ladder" in begin[0]
    in_flight["value"] = False
    runtime._route_lease_event.clear()
    runtime._runtime_post_dial_ladder_end(SimpleNamespace(), "convicted")
    assert runtime._route_lease_event.is_set()
    at(runtime, 615.0)
    end = [line for line in lease_lines(caplog, "deferred") if "state=end" in line]
    # A hard reason defers from the first managed tick (t=1.0) on.
    assert end and "reason=post_dial_ladder deferred_s=614.0" in end[0]
    assert controller_calls(runtime, "DELETE") == [("DELETE", rl.TIER3_GROUP)]


# ---------------------------------------------------------------- ghost and purge


def test_ghost_end_on_fixed_none_makes_no_controller_call_and_bumps_nothing():
    runtime = build_runtime()
    pin(runtime, "B")
    publish_lease(runtime, "B")
    at(runtime, 1.0)
    runtime.test.state.update(fixed="", now="A")
    runtime.test.refresh()
    runtime.test.client.calls.clear()
    epoch = runtime._runtime_route_epoch
    runtime.test.client.provider_error = None
    runtime._route_lease_counters["last_read_mono"] = 30.0  # no provider read this tick
    at(runtime, 31.0)
    assert runtime.test.client.calls == []
    assert runtime._runtime_route_mutation_lease is None
    assert runtime._runtime_route_epoch == epoch
    clock = runtime._route_lease_clocks[rl.TIER3_GROUP]
    assert (clock.ended, clock.purge_pending) == ("core_cleared", True)
    # One purge DELETE on the next tick, still without an epoch bump.
    at(runtime, 41.0)
    assert controller_calls(runtime, "GET", "DELETE") == [("GET", rl.TIER3_GROUP), ("DELETE", rl.TIER3_GROUP)]
    assert runtime._runtime_route_epoch == epoch
    assert clock.purge_pending is False
    at(runtime, 51.0)
    assert len(controller_calls(runtime, "DELETE")) == 1


def test_pinned_dead_with_readback_fixed_empty_sends_no_release_delete():
    runtime = build_runtime()
    pin(runtime, "B")
    publish_lease(runtime, "B")
    at(runtime, 1.0)
    runtime.test.state["provider"] = provider_payload(alive=("A", "C"))
    runtime.test.client.calls.clear()

    def read_clears(group, timeout=None):
        runtime.test.client.calls.append(("GET", group))
        runtime.test.state.update(fixed="", now="A")  # Mihomo drops the dead pick
        return {"fixed": "", "now": "A"}

    runtime.test.client.proxy = read_clears
    epoch = runtime._runtime_route_epoch
    at(runtime, 20.0)
    assert controller_calls(runtime, "DELETE") == []
    clock = runtime._route_lease_clocks[rl.TIER3_GROUP]
    assert (clock.ended, clock.purge_pending) == ("pinned_dead", True)
    assert runtime._runtime_route_epoch == epoch
    at(runtime, 30.0)
    assert controller_calls(runtime, "DELETE") == [("DELETE", rl.TIER3_GROUP)]


def test_purge_skips_another_pick_and_gives_up_after_three_failures():
    runtime = build_runtime()
    pin(runtime, "B")
    publish_lease(runtime, "B")
    at(runtime, 1.0)
    runtime.test.state.update(fixed="", now="A")
    runtime.test.refresh()
    at(runtime, 20.0)
    runtime.test.state.update(fixed="C", now="C")
    at(runtime, 30.0)
    assert controller_calls(runtime, "DELETE") == []
    clock = runtime._route_lease_clocks[rl.TIER3_GROUP]
    assert clock.purge_pending is False

    failing = build_runtime()
    pin(failing, "B")
    publish_lease(failing, "B")
    at(failing, 1.0)
    failing.test.state.update(fixed="", now="A")
    failing.test.refresh()
    at(failing, 20.0)
    failing.test.client.proxy = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("down"))
    for tick in (30.0, 40.0, 50.0):
        at(failing, tick)
    assert rl.TIER3_GROUP not in failing._route_lease_clocks


def test_core_fixed_mismatch_ends_after_two_snapshots_without_delete():
    runtime = build_runtime()
    pin(runtime, "B")
    lease = publish_lease(runtime, "B")
    at(runtime, 1.0)
    runtime.test.state.update(fixed="C", now="C")
    runtime.test.refresh()
    runtime._route_lease_counters["last_read_mono"] = 100.0
    at(runtime, 10.0)
    assert runtime._runtime_route_mutation_lease is lease
    runtime.test.refresh()
    at(runtime, 21.0)
    assert runtime._runtime_route_mutation_lease is None
    assert controller_calls(runtime, "DELETE") == []
    assert runtime._route_lease_clocks[rl.TIER3_GROUP].ended == "core_fixed_mismatch"


# ---------------------------------------------------------------- user intent


def test_user_selection_on_tier3_drops_its_clock(caplog):
    caplog.set_level(logging.INFO, logger="test-route-lease")
    runtime = build_runtime()
    pin(runtime, "B")
    publish_lease(runtime, "B")
    at(runtime, 1.0)
    runtime._declare_runtime_selector_intent("manual Mihomo selection", group=rl.TIER3_GROUP)
    assert rl.TIER3_GROUP not in runtime._route_lease_clocks
    runtime._route_lease_flush_logs()
    assert any("cause=user_intent" in line for line in lease_lines(caplog, "end"))
    at(runtime, 700.0)
    assert controller_calls(runtime, "DELETE") == []


def test_user_selection_on_final_exit_keeps_tier3_pin_as_a_restamped_orphan():
    runtime = build_runtime()
    pin(runtime, "B")
    publish_lease(runtime, "B")
    at(runtime, 1.0)
    revision = runtime._declare_runtime_selector_intent("manual exit selection", group="FINAL-EXIT")
    clock = runtime._route_lease_clocks[rl.TIER3_GROUP]
    assert clock.intent_rev == revision and runtime._runtime_route_mutation_lease is None
    at(runtime, 10.0)
    assert clock.origin == "recovery"  # not adopted yet
    at(runtime, 45.0)
    assert clock.origin == "orphan"
    # Readback shows another member: never deleted.
    runtime.test.state.update(fixed="C", now="C")
    at(runtime, 600.0)
    assert controller_calls(runtime, "DELETE") == []
    runtime.test.state.update(fixed="B", now="B")
    runtime.test.refresh()
    clock.next_attempt_mono = 0.0
    at(runtime, 700.0)
    assert controller_calls(runtime, "DELETE") == [("DELETE", rl.TIER3_GROUP)]


def test_revalidate_false_selection_drops_that_groups_clock():
    runtime = build_runtime()
    pin(runtime, "B")
    runtime._route_lease_on_user_intent(rl.TIER3_GROUP, 1)
    assert runtime._route_lease_clocks == {}


def test_user_graph_reload_restamps_clocks():
    runtime = build_runtime()
    pin(runtime, "B")
    runtime._route_lease_restamp(9)
    assert runtime._route_lease_clocks[rl.TIER3_GROUP].intent_rev == 9


# ---------------------------------------------------------------- watcher I/O


def test_watcher_blocks_untimed_with_nothing_leased_and_reads_only_provider():
    runtime = build_runtime()
    runtime._route_lease_policy = lambda: RouteLeasePolicy.for_tests(watch_interval_seconds=0.02)
    runtime._route_lease_ensure_thread()
    thread = runtime._route_lease_thread
    try:
        time.sleep(0.25)
        assert runtime.test.client.calls == []
        assert int(runtime._route_lease_counters.get("ticks", 0) or 0) == 0
        pin(runtime, "B")
        publish_lease(runtime, "B")
        deadline = time.monotonic() + 3.0
        while int(runtime._route_lease_counters.get("reads", 0) or 0) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        kinds = {call[0] for call in runtime.test.client.calls}
        assert kinds <= {"PUT", "PROVIDER"}
        assert int(runtime._route_lease_counters.get("unleased_reads", 0) or 0) == 0
    finally:
        runtime.stop_event.set()
        runtime._route_lease_wake("stop")
        thread.join(2.0)
    assert not thread.is_alive()


def test_non_tier3_leases_make_zero_reads():
    runtime = build_runtime()
    pin(runtime, "x", group="PROXY-FALLBACK")
    publish_lease(runtime, "x", group="PROXY-FALLBACK")
    for tick in (1.0, 20.0, 40.0):
        at(runtime, tick)
    assert controller_calls(runtime, "PROVIDER") == []


def test_watcher_reads_at_most_once_per_interval():
    runtime = build_runtime()
    pin(runtime, "B")
    publish_lease(runtime, "B")
    at(runtime, 1.0)
    at(runtime, 2.0)
    at(runtime, 3.0)
    assert len(controller_calls(runtime, "PROVIDER")) == 1
    at(runtime, 12.0)
    assert len(controller_calls(runtime, "PROVIDER")) == 2


def test_idle_tick_reports_nothing_managed():
    runtime = build_runtime()
    assert at(runtime, 1.0)["managed"] == 0
    assert runtime.test.client.calls == []


# ---------------------------------------------------------------- passive failback


def leaf_pass(name="A"):
    return {
        "tier": "tier3", "pool_kind": "tier3b", "identity": name, "proxy_name": name,
        "authoritative": True, "transition": {"action": "recovered"},
    }


def live_watcher(runtime):
    runtime._route_lease_thread = SimpleNamespace(is_alive=lambda: True, route_lease_generation=1)


def test_early_leaf_recheck_pass_on_tier3_lease_only_wakes_the_watcher():
    runtime = build_runtime()
    live_watcher(runtime)
    publish_lease(runtime, "B")
    runtime._route_lease_event.clear()
    started = []
    runtime._run_runtime_passive_failback = lambda request: started.append(request)
    runtime._prewarm_runtime_switch_target = lambda *a, **k: pytest.fail("no prewarm /delay")
    runtime._on_runtime_leaf_recheck_result_early(leaf_pass())
    assert runtime._route_lease_event.is_set()
    assert started == [] and runtime._runtime_failure_recovery_threads == set()


@pytest.mark.parametrize("group,early", [("PROXY-FALLBACK", True), (rl.TIER3_GROUP, False)])
def test_other_groups_and_early_release_false_keep_the_passive_path(group, early):
    runtime = build_runtime(early_release=early)
    publish_lease(runtime, "B", group=group)
    ref = RuntimeNodeRef("tier3", "tier3b", "A", "A")
    runtime._runtime_incident_bindings = {
        "incident-1": RuntimeRouteBinding(
            process_pid=77, process_generation=1, core_generation=1, semantic_digest="d", route_epoch=5,
            observed_outbound="FINAL-EXIT", route_chain=("FINAL-EXIT", group, "B"), node=ref,
        )
    }
    runtime._runtime_passive_failback_target = lambda *_args: (ref, "A")
    started = threading.Event()
    runtime._run_runtime_passive_failback = lambda request: started.set()
    runtime._on_runtime_leaf_recheck_result_early(leaf_pass())
    assert started.wait(2.0)


# ---------------------------------------------------------------- shutdown


def test_shutdown_release_deletes_our_pin_despite_stop_event():
    runtime = build_runtime()
    pin(runtime, "B")
    runtime.stop_event.set()
    runtime.test.client.calls.clear()
    started = time.monotonic()
    assert runtime._route_lease_release_for_shutdown(3.0) == 1
    assert time.monotonic() - started < 0.5
    assert controller_calls(runtime, "GET", "DELETE") == [("GET", rl.TIER3_GROUP), ("DELETE", rl.TIER3_GROUP)]


def test_shutdown_release_skips_low_budget_and_busy_lock():
    runtime = build_runtime()
    pin(runtime, "B")
    runtime.test.client.calls.clear()
    assert runtime._route_lease_release_for_shutdown(2.3) == 0
    holder_ready = threading.Event()
    release_holder = threading.Event()

    def hold():
        with runtime._core_transition_lock:
            holder_ready.set()
            release_holder.wait(2.0)

    thread = threading.Thread(target=hold)
    thread.start()
    holder_ready.wait(1.0)
    try:
        started = time.monotonic()
        assert runtime._route_lease_release_for_shutdown(3.0) == 0
        assert time.monotonic() - started < 0.6
    finally:
        release_holder.set()
        thread.join(2.0)
    assert runtime.test.client.calls == []


def test_shutdown_never_deletes_another_pick():
    runtime = build_runtime()
    pin(runtime, "B")
    runtime.test.state.update(fixed="C")
    assert runtime._route_lease_release_for_shutdown(3.0) == 0
    assert controller_calls(runtime, "DELETE") == []


# ---------------------------------------------------------------- restart, epoch, logs


def test_core_restart_keeps_lease_and_clock_and_provider_failure_skips():
    runtime = build_runtime()
    pin(runtime, "B")
    lease = publish_lease(runtime, "B")
    at(runtime, 1.0)
    clock = runtime._route_lease_clocks[rl.TIER3_GROUP]
    runtime.core_runner = SimpleNamespace(is_running=lambda: False, status=lambda: {})
    at(runtime, 600.0)
    assert controller_calls(runtime, "DELETE") == []
    runtime._core_generation = 2
    runtime.core_runner = SimpleNamespace(is_running=lambda: True, status=lambda: {"pid": 78})
    runtime.test.client.provider_error = RuntimeError("down")
    assert runtime._route_lease_clocks[rl.TIER3_GROUP] is clock and runtime._runtime_route_mutation_lease is lease
    at(runtime, 605.0)
    assert controller_calls(runtime, "DELETE") == [("DELETE", rl.TIER3_GROUP)]


def test_only_the_delete_path_bumps_the_epoch_at_the_existing_site():
    runtime = build_runtime()
    pin(runtime, "B")
    publish_lease(runtime, "B")
    at(runtime, 1.0)
    runtime.test.arm_sites.clear()
    at(runtime, 600.0)
    assert runtime.test.arm_sites == ["release_runtime_automatic_fixed_selection"]


def test_log_lines_round_trip_and_never_look_like_reload_lines(caplog):
    caplog.set_level(logging.INFO, logger="test-route-lease")
    runtime = build_runtime()
    pin(runtime, "B")
    publish_lease(runtime, "B")
    at(runtime, 1.0)
    at(runtime, 600.0)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Runtime route lease ")]
    assert {line.split()[3] for line in lines} >= {"start", "release", "end"}
    for line in lines:
        assert "reloading mihomo runtime config" not in line
        body = line[line.index("(") + 1 : line.rindex(")")]
        pairs = [item.split("=", 1) for item in body.split(" ")]
        assert all(len(pair) == 2 and pair[0] and pair[1] for pair in pairs)
        lease = dict(pairs).get("lease")
        assert lease is None or lease == "-" or (lease.startswith("L") and not lease.isdigit())


def test_start_reset_clears_clocks():
    runtime = build_runtime()
    pin(runtime, "B")
    runtime._route_lease_reset_for_start()
    assert runtime._route_lease_clocks == {} and runtime._route_lease_counters == {}


def test_verify_retry_runs_even_though_the_failed_verify_made_egress_not_ready():
    runtime = build_runtime()
    pin(runtime, "B")
    publish_lease(runtime, "B")
    at(runtime, 1.0)

    def fail_verify(**_kwargs):
        raise RuntimeError("no 204")

    runtime._verify_runtime_final_exit = fail_verify
    at(runtime, 600.0)
    runtime._runtime_initial_activation_was_completed = lambda: False
    runtime._verify_runtime_final_exit = lambda **_kwargs: runtime.test.calls.append("204-retry") or {"status": 204}
    at(runtime, 610.0)
    assert "204-retry" in runtime.test.calls
    # A replaced Core is verified by its own admission, not by the lease.
    other = build_runtime()
    pin(other, "B")
    publish_lease(other, "B")
    at(other, 1.0)
    other._verify_runtime_final_exit = fail_verify
    at(other, 600.0)
    other._core_generation = 2
    other._verify_runtime_final_exit = lambda **_kwargs: other.test.calls.append("204-retry") or {"status": 204}
    at(other, 610.0)
    assert "204-retry" not in other.test.calls
    assert other._route_lease_clocks[rl.TIER3_GROUP].verify_retry is False


# ---------------------------------------------------------------- review fixes (2026-10-27)


def lease_id_of(line):
    return line.split("lease=")[1].split()[0].rstrip(")")


def field_of(line, key):
    return line.split(f"{key}=")[1].split()[0].rstrip(")")


def test_recovery_pin_of_another_member_ends_the_old_lease_with_its_open_deferral(caplog):
    caplog.set_level(logging.INFO, logger="test-route-lease")
    runtime = build_runtime()
    pin(runtime, "B")
    publish_lease(runtime, "B", incident="incident-1")
    at(runtime, 1.0)
    old_id = lease_id_of(lease_lines(caplog, "start")[0])
    runtime._runtime_failure_recovery_bindings.add(("worker",))
    at(runtime, 30.0)  # recovery for B's failure: hard deferral opens
    runtime.test.time.mono = 50.0
    pin(runtime, "C")  # recovery moves the pin to C
    publish_lease(runtime, "C", incident="incident-2", previous="B")
    at(runtime, 51.0)
    ends = [line for line in lease_lines(caplog, "end") if f"lease={old_id}" in line]
    assert len(ends) == 1 and "cause=superseded" in ends[0]
    assert float(field_of(ends[0], "deferred_s")) == pytest.approx(20.0)
    closed = [
        line for line in lease_lines(caplog, "deferred")
        if f"lease={old_id}" in line and "state=end" in line
    ]
    assert len(closed) == 1 and float(field_of(closed[0], "deferred_s")) == pytest.approx(20.0)
    starts = lease_lines(caplog, "start")
    assert len(starts) == 2 and lease_id_of(starts[1]) != old_id
    assert runtime._route_lease_clocks[rl.TIER3_GROUP].member == "C"


def test_end_line_counts_the_open_span_and_the_new_id_starts_its_own_deferral(caplog):
    caplog.set_level(logging.INFO, logger="test-route-lease")
    runtime = build_runtime()
    pin(runtime, "B")
    publish_lease(runtime, "B", incident="incident-1")
    at(runtime, 1.0)
    old_id = lease_id_of(lease_lines(caplog, "start")[0])
    runtime._runtime_failure_recovery_bindings.add(("worker",))
    at(runtime, 590.0)
    at(runtime, 700.0)
    publish_lease(runtime, "B", incident="incident-2")  # recovery re-leases the same member
    at(runtime, 800.0)
    end = [line for line in lease_lines(caplog, "end") if f"lease={old_id}" in line]
    assert len(end) == 1 and "cause=recovery_replaced" in end[0]
    assert float(field_of(end[0], "deferred_s")) == pytest.approx(210.0)
    new_start = lease_lines(caplog, "start")[1]
    new_id = lease_id_of(new_start)
    # The new id starts already due: due_s is 0, never negative.
    assert float(field_of(new_start, "due_s")) == 0.0
    # deferred_s is per lease id: the old id's 210 s is never inherited.
    carried = [line for line in lease_lines(caplog, "deferred") if f"lease={new_id}" in line]
    assert [field_of(line, "state") for line in carried] == ["begin"]
    assert field_of(carried[0], "reason") == "recovery_worker"
    assert float(field_of(carried[0], "deferred_s")) == 0.0
    assert not any("reason=carried" in line for line in lease_lines(caplog, "deferred"))
    runtime._runtime_failure_recovery_bindings.clear()
    at(runtime, 810.0)
    final = [
        line for line in lease_lines(caplog, "deferred")
        if f"lease={new_id}" in line and "state=end" in line
    ][-1]
    assert float(field_of(final, "deferred_s")) == pytest.approx(10.0)
    new_end = [line for line in lease_lines(caplog, "end") if f"lease={new_id}" in line]
    assert len(new_end) == 1 and float(field_of(new_end[0], "deferred_s")) == pytest.approx(10.0)


def test_superseded_closes_the_open_deferral_and_the_orphan_starts_at_zero(caplog):
    caplog.set_level(logging.INFO, logger="test-route-lease")
    runtime = build_runtime()
    pin(runtime, "B")
    publish_lease(runtime, "B", incident="incident-1")
    at(runtime, 1.0)
    old_id = lease_id_of(lease_lines(caplog, "start")[0])
    runtime._runtime_failure_recovery_bindings.add(("worker",))
    at(runtime, 100.0)
    with runtime._runtime_failure_monitor_lock:
        runtime._runtime_route_mutation_lease = None  # cleared without a release
    at(runtime, 150.0)
    end = [line for line in lease_lines(caplog, "end") if f"lease={old_id}" in line]
    assert len(end) == 1 and "cause=superseded" in end[0]
    assert float(field_of(end[0], "deferred_s")) == pytest.approx(50.0)
    closed = [
        line for line in lease_lines(caplog, "deferred")
        if f"lease={old_id}" in line and "state=end" in line
    ]
    assert len(closed) == 1 and float(field_of(closed[0], "deferred_s")) == pytest.approx(50.0)
    assert runtime._route_lease_clocks[rl.TIER3_GROUP].defer_reason == ""
    adopt = 150.0 + rl.ORPHAN_ADOPT_S + 1.0
    at(runtime, adopt)
    orphan_id = lease_id_of(lease_lines(caplog, "start")[1])
    carried = [line for line in lease_lines(caplog, "deferred") if f"lease={orphan_id}" in line]
    # The 31 s pending phase accrues nothing; the still-busy orphan opens its own span at 0.
    assert [field_of(line, "state") for line in carried] == ["begin"]
    assert float(field_of(carried[0], "deferred_s")) == 0.0
    assert not any("reason=carried" in line for line in lease_lines(caplog, "deferred"))
    runtime._runtime_failure_recovery_bindings.clear()
    at(runtime, adopt + 20.0)
    final = [
        line for line in lease_lines(caplog, "deferred")
        if f"lease={orphan_id}" in line and "state=end" in line
    ]
    assert len(final) == 1 and float(field_of(final[0], "deferred_s")) == pytest.approx(20.0)


def test_death_carried_id_never_inherits_the_dead_ids_deferral(caplog):
    caplog.set_level(logging.INFO, logger="test-route-lease")
    runtime = build_runtime()
    pin(runtime, "B")
    publish_lease(runtime, "B", incident="incident-1")
    at(runtime, 1.0)
    old_id = lease_id_of(lease_lines(caplog, "start")[0])
    runtime._runtime_failure_recovery_bindings.add(("worker",))
    at(runtime, 30.0)
    runtime._runtime_failure_recovery_bindings.clear()
    at(runtime, 60.0)  # one closed 30 s span
    runtime.test.state.update(fixed="", now="A")
    runtime.test.refresh()
    at(runtime, 100.0)
    clock = runtime._route_lease_clocks[rl.TIER3_GROUP]
    assert clock.ended == "core_cleared"
    old_end = [line for line in lease_lines(caplog, "end") if f"lease={old_id}" in line]
    assert len(old_end) == 1 and float(field_of(old_end[0], "deferred_s")) == pytest.approx(30.0)
    runtime.test.time.mono = 150.0
    pin(runtime, "B")  # death carry: the TTL start is kept
    publish_lease(runtime, "B", incident="incident-2")
    at(runtime, 151.0)
    new_id = lease_id_of(lease_lines(caplog, "start")[1])
    assert new_id != old_id and clock.started_mono == 0.0
    assert [line for line in lease_lines(caplog, "deferred") if f"lease={new_id}" in line] == []
    runtime.test.state.update(fixed="", now="A")
    runtime.test.refresh()
    at(runtime, 160.0)
    new_end = [line for line in lease_lines(caplog, "end") if f"lease={new_id}" in line]
    assert len(new_end) == 1 and float(field_of(new_end[0], "deferred_s")) == 0.0


def test_shutdown_end_line_counts_the_open_deferral(caplog):
    caplog.set_level(logging.INFO, logger="test-route-lease")
    runtime = build_runtime()
    pin(runtime, "B")
    publish_lease(runtime, "B")
    at(runtime, 1.0)
    runtime._runtime_failure_recovery_bindings.add(("worker",))
    at(runtime, 100.0)
    runtime.test.time.mono = 130.0
    assert runtime._route_lease_release_for_shutdown(10.0) == 1
    end = [line for line in lease_lines(caplog, "end") if "cause=shutdown" in line]
    assert len(end) == 1 and float(field_of(end[0], "deferred_s")) == pytest.approx(30.0)


def test_early_release_backoff_follows_the_predicted_winner():
    runtime = build_runtime()
    runtime._route_lease_backoff = {"A": 1}  # A, the predicted winner, needs 220 s
    pin(runtime, "B")
    publish_lease(runtime, "B", previous="C")  # the incident member C has no backoff
    at(runtime, 1.0)
    runtime.test.state["provider"] = provider_payload(history={"A": [(2.0, 0), (115.0, 90)]})
    at(runtime, 116.0)
    assert ("DELETE", rl.TIER3_GROUP) not in runtime.test.client.calls  # 113 s < 220 s
    runtime.test.state["provider"] = provider_payload(history={"A": [(2.0, 0), (115.0, 90), (225.0, 90)]})
    at(runtime, 226.0)
    assert ("DELETE", rl.TIER3_GROUP) in runtime.test.client.calls  # 223 s >= 220 s
    # The incident member's backoff no longer delays another winner.
    other = build_runtime()
    other._route_lease_backoff = {"C": 2}
    pin(other, "B")
    publish_lease(other, "B", previous="C")
    at(other, 1.0)
    other.test.state["provider"] = provider_payload(history={"A": [(2.0, 0), (115.0, 90)]})
    at(other, 116.0)
    assert ("DELETE", rl.TIER3_GROUP) in other.test.client.calls


def test_pure_check_takes_a_per_member_requirement():
    clock = rl.LeaseClock(
        group=rl.TIER3_GROUP, member="B", started_mono=0.0, started_wall=WALL0,
        last_decision_mono=0.0, last_decision_wall=WALL0,
    )
    provider = provider_payload(history={"A": [(2.0, 0), (115.0, 90)]})
    seen = []

    def required(member):
        seen.append(member)
        return 220.0 if member == "A" else 110.0

    check = rl.early_release_check(
        now_wall=WALL0 + 116.0, clock=clock, provider=provider, observation=None, test_url=URL,
        app_windows=None, stable_required=required, incident_member="C",
    )
    assert seen == ["A"] and not check.released and check.predicted == "A"
    check = rl.early_release_check(
        now_wall=WALL0 + 116.0, clock=clock, provider=provider, observation=None, test_url=URL,
        app_windows=None, stable_required=110.0, incident_member="C",
    )
    assert check.released


def test_leaf_pass_without_a_live_watcher_keeps_the_passive_path_and_requests_one():
    runtime = build_runtime()
    publish_lease(runtime, "B")
    ref = RuntimeNodeRef("tier3", "tier3b", "A", "A")
    runtime._runtime_incident_bindings = {
        "incident-1": RuntimeRouteBinding(
            process_pid=77, process_generation=1, core_generation=1, semantic_digest="d", route_epoch=5,
            observed_outbound="FINAL-EXIT", route_chain=("FINAL-EXIT", rl.TIER3_GROUP, "B"), node=ref,
        )
    }
    runtime._runtime_passive_failback_target = lambda *_args: (ref, "A")
    started = threading.Event()
    runtime._run_runtime_passive_failback = lambda request: started.set()
    assert runtime._route_lease_thread_wanted is False
    runtime._on_runtime_leaf_recheck_result_early(leaf_pass())
    assert started.wait(2.0)
    assert runtime._route_lease_thread_wanted is True
    # A watcher of an older runtime generation does not count either.
    stale = build_runtime()
    stale._route_lease_thread = SimpleNamespace(is_alive=lambda: True, route_lease_generation=0)
    assert stale._route_lease_takes_passive_pass({"group": rl.TIER3_GROUP}) is False
    assert stale._route_lease_thread_wanted is True


def test_any_wake_without_a_watcher_requests_one_and_ensure_thread_creates_it():
    runtime = build_runtime()
    runtime._route_lease_wake("user_intent")
    assert runtime._route_lease_thread_wanted is True
    live_watcher(runtime)
    runtime._route_lease_thread_wanted = False
    runtime._route_lease_wake("user_intent")
    assert runtime._route_lease_thread_wanted is False
    # Enabled after start (or re-enabled after a 'disabled' exit): the wanted
    # flag makes the health monitor's ensure_thread create the watcher.
    fresh = build_runtime()
    fresh._route_lease_wake("reload")
    assert fresh._route_lease_thread_wanted is True
    fresh._route_lease_ensure_thread()
    thread = fresh._route_lease_thread
    try:
        assert thread is not None and thread.is_alive() and fresh._route_lease_watcher_alive()
        assert fresh._route_lease_thread_wanted is False
    finally:
        fresh.stop_event.set()
        fresh._route_lease_wake("stop")
        thread.join(2.0)
    assert not thread.is_alive()
