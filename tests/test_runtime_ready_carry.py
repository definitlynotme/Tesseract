"""Decision D (11-04): READY is carried to the route epoch a same-Core move produced.

After READY the shipped early profile never re-publishes readiness, so the
first change of the serving node left ``lifecycle_status()`` stale until stop
(phase ``core_standby``: settings intents waited, the notice tracker stopped,
the tier topics went stale).  ``_maybe_carry_runtime_ready`` re-stamps the same
READY at the new epoch when the same Core owner, lifecycle, network context and
user intent still hold and Mihomo's own self-check already calls the serving
leaf alive for its group's test URL.

The user's condition: the carry must never test a node itself.  Every route
here is published by the real readback from a live 1.19.27 capture through
``CapturedController``, which raises on any call other than ``proxies`` /
``proxy``; the carry is checked to make no controller call at all.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

import desktop_runtime as desktop_runtime_module
from runtime_egress_recovery import RuntimeEgressReadiness, RuntimeEgressReadinessState
from tests.real_route_publication import published_runtime

READY = RuntimeEgressReadinessState.READY


class _Manager(SimpleNamespace):
    def __init__(self, generation):
        super().__init__(lock=threading.RLock(), runtime_generation=generation,
                         effective_egress_ok=True, effective_calls=[], notify_calls=[])

    def set_effective_egress_state(self, ok, reason=""):
        self.effective_calls.append((ok, reason))
        self.effective_egress_ok = ok

    def notify_tier3_source_supply_route_changed(self, event):
        self.notify_calls.append(event)


def _harness(tmp_path, state="healthy"):
    pub = published_runtime(tmp_path, state)
    runtime = pub.runtime
    runtime.core_runner.lightweight_fact = lambda: {
        "running": True, "pid": 4242, "process_generation": 1, "cleanup_pending": False}
    runtime.manager = _Manager(int(runtime._runtime_generation))
    runtime.running = True
    runtime._desired_running = True
    return pub


def _publish_ready(runtime):
    return runtime._publish_runtime_egress_readiness(READY, validation_active=False,
                                                     reason="verified")


def _invalidate(runtime):
    """What an app route write does (select_mihomo_proxy): drop the snapshot."""

    with runtime._runtime_route_snapshot_lock:
        runtime._runtime_route_snapshot = None
        runtime._runtime_route_snapshot_payload = None
        runtime._runtime_route_selection_signature = ()


def _status(runtime):
    status = runtime.lifecycle_status()
    return bool(status["freshness"]["stale"]), status["runtime"]["startup_phase"]


def _moved(tmp_path, state="failover"):
    """READY published on ``healthy``, then a native move to ``state``."""

    pub = _harness(tmp_path)
    runtime = pub.runtime
    ready = _publish_ready(runtime)
    assert _status(runtime) == (False, "running")
    epoch = runtime._runtime_route_epoch
    pub.publish(state)
    assert runtime._runtime_route_epoch == epoch + 1
    assert _status(runtime) == (True, "core_standby")
    return pub, ready


def _reason(runtime):
    counts = runtime._runtime_ready_carry_counts
    return set(counts)


def test_a_native_failover_is_carried_with_no_request_and_no_other_write(tmp_path):
    pub, ready = _moved(tmp_path)
    runtime = pub.runtime
    manager = runtime.manager
    calls = list(pub.controller.calls)
    report = dict(getattr(manager, "tier3_last_collection_report", {}))
    before = dict(
        effective=list(manager.effective_calls), notify=list(manager.notify_calls),
        source=getattr(runtime, "_source_control_final_exit_was_ready", None),
        ledger=runtime._tier3_health_ledger.revision, arm=runtime._runtime_recovery_arm,
        activation=runtime._runtime_initial_activation_generation,
        revision=runtime._runtime_egress_readiness_publication_revision,
    )

    assert runtime._maybe_carry_runtime_ready("t") is True

    carried = runtime._runtime_egress_readiness
    assert _status(runtime) == (False, "running")
    assert carried.state is READY and carried.route_epoch == runtime._runtime_route_epoch
    # The carry vouched for the new route: a later move back to n1 is a move.
    assert carried.route_physical != ready.route_physical
    assert carried.route_physical == tuple(
        item for item in runtime._runtime_route_selection_signature if item[0] != "user_intent_revision")
    assert (carried.lifecycle_revision, carried.network_context_epoch, carried.route_owner,
            carried.selector_intent_revision, carried.reason) == (
        ready.lifecycle_revision, ready.network_context_epoch, ready.route_owner,
        ready.selector_intent_revision, ready.reason)
    revision = runtime._runtime_egress_readiness_publication_revision
    assert revision == before["revision"] + 1 == runtime._runtime_egress_readiness_projected_revision
    # No request, no write anywhere but the readiness record.
    assert pub.controller.calls == calls
    assert manager.effective_calls == before["effective"]
    assert manager.notify_calls == before["notify"]
    assert getattr(runtime, "_source_control_final_exit_was_ready", None) == before["source"]
    assert runtime._tier3_health_ledger.revision == before["ledger"]
    assert runtime._runtime_recovery_arm is before["arm"]
    assert runtime._runtime_initial_activation_generation == before["activation"]
    assert dict(getattr(manager, "tier3_last_collection_report", {})) == report
    assert runtime._runtime_ready_carry_counts.get("carried") == 1
    # Steady state afterwards: nothing more to do.
    assert runtime._maybe_carry_runtime_ready("t") is False


def test_the_publisher_stamps_the_core_owner_and_the_user_intent(tmp_path):
    pub = _harness(tmp_path)
    runtime = pub.runtime
    runtime._runtime_selector_intent_revision = 7
    ready = _publish_ready(runtime)
    assert ready.route_owner == (4242, 1, 1)
    assert ready.selector_intent_revision == 7
    # The physical route: the selection signature without its intent element.
    signature = runtime._runtime_route_selection_signature
    assert ready.route_physical and ready.route_physical == tuple(
        item for item in signature if item[0] != "user_intent_revision")
    _invalidate(runtime)
    unknown = _publish_ready(runtime)
    assert unknown.route_owner == () and unknown.route_physical == ()
    direct = RuntimeEgressReadiness(state=READY, validation_active=False)
    assert direct.route_owner == () and direct.selector_intent_revision == -1
    assert direct.route_physical == ()


@pytest.mark.parametrize("change", ["process_generation", "pid", "core_generation"])
def test_a_ready_proved_on_another_core_owner_is_never_carried(tmp_path, change):
    """T-P1-10: an older Core's READY never counts on a new Core owner."""

    pub = _harness(tmp_path)
    runtime = pub.runtime
    _publish_ready(runtime)
    pid, generation = 4242, 1
    if change == "process_generation":
        generation = 2
    elif change == "pid":
        pid = 5151
    else:
        runtime._core_generation = 2
        runtime._runtime_initial_activation_generation = 2
    runtime.core_runner.status = lambda: {"pid": pid, "process_generation": generation}
    runtime.core_runner.lightweight_fact = lambda: {
        "running": True, "pid": pid, "process_generation": generation, "cleanup_pending": False}
    pub.publish("failover")
    assert _status(runtime)[0] is True

    assert runtime._maybe_carry_runtime_ready("t") is False
    assert "owner_changed" in _reason(runtime)
    stale, phase = _status(runtime)
    assert stale and phase != "running"


def test_a_ready_without_an_owner_anchor_is_never_carried(tmp_path):
    pub = _harness(tmp_path)
    runtime = pub.runtime
    runtime._runtime_egress_readiness = RuntimeEgressReadiness(state=READY, validation_active=False)
    pub.publish("failover")
    assert runtime._maybe_carry_runtime_ready("t") is False
    assert "owner_unknown" in _reason(runtime)
    assert _status(runtime)[0] is True


@pytest.mark.parametrize("state,validation", [
    (RuntimeEgressReadinessState.CONTROLLER_HOLD, False),
    (RuntimeEgressReadinessState.DATA_PLANE_FAILED, False),
    (RuntimeEgressReadinessState.FAIL_CLOSED, False),
    (RuntimeEgressReadinessState.SERVING, False),
    (RuntimeEgressReadinessState.ACTIVATION_HOLD, True),
    (READY, True),
])
def test_only_a_settled_ready_is_ever_carried(tmp_path, state, validation):
    pub = _harness(tmp_path)
    runtime = pub.runtime
    published = runtime._publish_runtime_egress_readiness(state, validation_active=validation)
    pub.publish("failover")
    assert runtime._maybe_carry_runtime_ready("t") is False
    assert runtime._runtime_egress_readiness is published


def _gate_cases():
    def effective_none(rt):
        rt.manager.effective_egress_ok = None
        return lambda: setattr(rt.manager, "effective_egress_ok", True)

    def effective_false(rt):
        rt.manager.effective_egress_ok = False
        return lambda: setattr(rt.manager, "effective_egress_ok", True)

    def mutation_pending(rt):
        rt._core_route_mutation_pending = True
        return lambda: setattr(rt, "_core_route_mutation_pending", False)

    def incident(state):
        def close(rt):
            rt._runtime_incident_recovery_state = {"incident-1": state}
            return lambda: setattr(rt, "_runtime_incident_recovery_state", {})
        return close

    def coordinator_lease(rt):
        rt._runtime_egress_coordinator.core_mutation_active = lambda: True
        return lambda: rt._runtime_egress_coordinator.__dict__.pop("core_mutation_active", None)

    def reload_gate(field):
        def close(rt):
            real = rt._runtime_reload_gate.snapshot

            def snapshot():
                return SimpleNamespace(**{**_gate_fields(real()), field: "busy"})
            rt._runtime_reload_gate.snapshot = snapshot
            return lambda: rt._runtime_reload_gate.__dict__.pop("snapshot", None)
        return close

    def lease_releasing(rt):
        rt._runtime_route_mutation_lease = {"release_state": "releasing"}
        return lambda: setattr(rt, "_runtime_route_mutation_lease", None)

    def passive_failback(rt):
        rt._runtime_passive_failback_requests = {"k": {"running": True}}
        return lambda: setattr(rt, "_runtime_passive_failback_requests", {})

    def controller_hold(rt):
        rt._tier3_standby_activation_hold_kind = "controller_hold"
        return lambda: setattr(rt, "_tier3_standby_activation_hold_kind", "")

    def activation_pending(rt):
        rt._tier3_egress_activation_pending = True
        return lambda: setattr(rt, "_tier3_egress_activation_pending", False)

    def activation_running(rt):
        rt._tier3_egress_activation_worker_running = True
        return lambda: setattr(rt, "_tier3_egress_activation_worker_running", False)

    def fail_closed(rt):
        rt._runtime_confirmed_fail_closed_snapshot_key = ("key",)
        return lambda: setattr(rt, "_runtime_confirmed_fail_closed_snapshot_key", None)

    def facility_hold(rt):
        rt._runtime_facility_hold_reason = lambda: "listener lost"
        return lambda: rt.__dict__.pop("_runtime_facility_hold_reason", None)

    def stopping(rt):
        rt.stop_event.set()
        return rt.stop_event.clear

    def coordinator_snapshot(rt):
        real = rt._runtime_egress_coordinator.current_route_snapshot()
        rt._runtime_egress_coordinator.set_current_snapshot(None)
        return lambda: rt._runtime_egress_coordinator.set_current_snapshot(real)

    def contract_moved(rt):
        contract = rt._runtime_loaded_export_contract
        rt._runtime_loaded_export_contract = {**contract, "semantic_digest": "another"}
        return lambda: setattr(rt, "_runtime_loaded_export_contract", contract)

    def transition_busy(rt):
        held, release = threading.Event(), threading.Event()

        def hold():
            with rt._core_transition_lock:
                held.set()
                release.wait(10)
        thread = threading.Thread(target=hold, daemon=True)
        thread.start()
        assert held.wait(5)

        def reopen():
            release.set()
            thread.join(5)
        return reopen

    return [
        ("effective_none", effective_none), ("effective_false", effective_false),
        ("mutation_pending", mutation_pending), ("incident_pending", incident("pending")),
        ("incident_recovering", incident("recovering")), ("coordinator_lease", coordinator_lease),
        ("reload_active", reload_gate("active_reload_reason")),
        ("reload_recovery", reload_gate("active_recovery_transaction_id")),
        ("lease_releasing", lease_releasing), ("passive_failback", passive_failback),
        ("controller_hold", controller_hold), ("activation_pending", activation_pending),
        ("activation_running", activation_running), ("fail_closed", fail_closed),
        ("facility_hold", facility_hold), ("stopping", stopping),
        ("coordinator_snapshot", coordinator_snapshot), ("contract_moved", contract_moved),
        ("transition_busy", transition_busy),
    ]


def _gate_fields(snapshot):
    return {name: getattr(snapshot, name) for name in (
        "revision", "loaded_semantic_digest", "active_recovery_transaction_id",
        "active_reload_reason", "pending_user_commit_id", "completed_user_commit_count")}


@pytest.mark.parametrize("name,close", _gate_cases(), ids=[case[0] for case in _gate_cases()])
def test_every_route_owner_gate_defers_the_carry_until_it_reopens(tmp_path, name, close):
    pub, _ready = _moved(tmp_path)
    runtime = pub.runtime
    reopen = close(runtime)
    before = runtime._runtime_egress_readiness
    assert runtime._maybe_carry_runtime_ready("t") is False, name
    assert runtime._runtime_egress_readiness is before
    reopen()
    assert runtime._maybe_carry_runtime_ready("t") is True, (name, runtime._runtime_ready_carry_counts)
    assert _status(runtime) == (False, "running")


@pytest.mark.parametrize("change", ["lifecycle", "network", "intent"])
def test_a_lifecycle_network_or_user_intent_change_is_never_carried(tmp_path, change):
    pub, _ready = _moved(tmp_path)
    runtime = pub.runtime
    if change == "lifecycle":
        runtime._lifecycle_revision += 1
    elif change == "network":
        runtime._runtime_network_context_epoch += 1
    else:
        runtime._runtime_selector_intent_revision += 1
    assert runtime._maybe_carry_runtime_ready("t") is False
    assert _status(runtime)[0] is True


def test_the_non_early_profile_never_carries(tmp_path):
    pub, _ready = _moved(tmp_path)
    runtime = pub.runtime
    runtime._runtime_egress_recovery_profile = "coordinated"
    assert runtime._maybe_carry_runtime_ready("t") is False
    assert runtime._runtime_ready_carry_counts == {}


@pytest.mark.parametrize("busy", ["provider_write", "judging"])
def test_test_only_activity_never_holds_the_carry(tmp_path, busy):
    pub, _ready = _moved(tmp_path)
    runtime = pub.runtime
    if busy == "provider_write":
        runtime._tier3_provider_write_busy = True
    else:
        judge = SimpleNamespace(judging=lambda: True, ladder_in_flight=lambda: True)
        state = runtime._node_judgement_state()
        object.__setattr__(state, "judge", judge) if hasattr(state, "__slots__") else setattr(state, "judge", judge)
    assert runtime._maybe_carry_runtime_ready("t") is True


def test_a_dead_leaf_is_never_carried(tmp_path):
    pub = _harness(tmp_path)
    runtime = pub.runtime
    _publish_ready(runtime)
    _invalidate(runtime)
    pub.publish("exhausted")
    calls = list(pub.controller.calls)
    assert runtime._maybe_carry_runtime_ready("t") is False
    assert {"evidence:not_alive", "evidence:tier3_view"} & _reason(runtime)
    assert pub.controller.calls == calls
    assert _status(runtime)[0] is True


@pytest.mark.parametrize("ledger,verified,expected", [
    ("alive", True, "carried"),
    (None, True, "evidence:unverdicted"),
    (None, False, "evidence:unverdicted"),
    ("alive", False, "evidence:unverdicted"),
])
def test_a_rebuilt_member_carries_only_on_its_own_or_the_ledger_verdict(tmp_path, ledger, verified,
                                                                          expected):
    """Mihomo's global alive flag (true on every rebuilt member) is never a verdict.

    READY was proved on n2 (``failover``); the move to the rebuilt n1 is a
    physical move, so it needs positive evidence.
    """

    pub = _harness(tmp_path, "failover")
    runtime = pub.runtime
    runtime._tier3_verified_provider_writes_enabled = lambda: verified
    _publish_ready(runtime)
    if ledger == "alive":
        runtime._tier3_health_ledger.observe("n1", True, time.time())
    _invalidate(runtime)
    pub.publish("rebuilt")
    carried = runtime._maybe_carry_runtime_ready("t")
    assert carried is (expected == "carried")
    assert expected in _reason(runtime), runtime._runtime_ready_carry_counts


def test_after_a_reload_republish_the_intent_only_epoch_move_is_carried(tmp_path):
    """Option A end to end on real captures: a user graph reload during a lease
    bumps the intent and republishes READY (tests/test_runtime_reload_ready.py);
    its own re-read then moves the epoch on the intent element alone, and the
    carry re-stamps it without a node test."""

    pub = _harness(tmp_path)
    runtime = pub.runtime
    _publish_ready(runtime)
    runtime._runtime_selector_intent_revision += 1  # the lease branch's bump
    republished = _publish_ready(runtime)  # what the reload's verified tail stores
    assert republished.selector_intent_revision == runtime._runtime_selector_intent_revision
    epoch = runtime._runtime_route_epoch
    calls = list(pub.controller.calls)
    pub.publish("healthy")  # the reload's re-read: same physical route
    assert runtime._runtime_route_epoch == epoch + 1
    assert _status(runtime)[0] is True
    carry_calls = list(pub.controller.calls)
    assert runtime._maybe_carry_runtime_ready("t") is True, runtime._runtime_ready_carry_counts
    assert pub.controller.calls == carry_calls and len(carry_calls) > len(calls)
    assert _status(runtime) == (False, "running")


def test_a_physically_unchanged_route_waits_only_for_negative_evidence(tmp_path):
    """Review 11-04: READY was proved on n1 and the re-read after an
    invalidation still serves n1 (rebuilt, no verdict yet): no traffic moved,
    so the missing verdict is waived.  A dead verdict is not (see
    test_a_dead_leaf_is_never_carried)."""

    pub = _harness(tmp_path)
    runtime = pub.runtime
    runtime._tier3_verified_provider_writes_enabled = lambda: True
    ready = _publish_ready(runtime)
    assert ready.route_physical
    _invalidate(runtime)
    pub.publish("rebuilt")
    assert runtime._maybe_carry_runtime_ready("t") is True, runtime._runtime_ready_carry_counts
    assert runtime._runtime_egress_readiness.route_physical == ready.route_physical
    assert _status(runtime) == (False, "running")


def test_a_publication_during_the_evidence_read_wins(tmp_path, monkeypatch):
    pub, _ready = _moved(tmp_path)
    runtime = pub.runtime
    real = runtime._runtime_ready_carry_evidence_refusal

    def racing(snapshot, payload):
        runtime._publish_runtime_egress_readiness(
            RuntimeEgressReadinessState.CONTROLLER_HOLD, validation_active=False)
        return real(snapshot, payload)

    monkeypatch.setattr(runtime, "_runtime_ready_carry_evidence_refusal", racing)
    assert runtime._maybe_carry_runtime_ready("t") is False
    # The commit's re-check (not READY any more) or its identity CAS refuses.
    assert {"transaction", "publication_moved"} & _reason(runtime)
    assert runtime._runtime_egress_readiness.state is RuntimeEgressReadinessState.CONTROLLER_HOLD


def test_a_ready_whose_projection_is_still_running_is_not_restamped(tmp_path, monkeypatch):
    pub = _harness(tmp_path)
    runtime = pub.runtime
    _publish_ready(runtime)
    seen = []
    real = runtime._source_control_final_exit_ready

    def projecting(manager):
        # The node moves while this READY's projection is still running.
        pub.publish("failover")
        seen.append(runtime._maybe_carry_runtime_ready("t"))
        return real(manager)

    monkeypatch.setattr(runtime, "_source_control_final_exit_ready", projecting)
    published = runtime._publish_runtime_egress_readiness(READY, validation_active=False,
                                                          effective_ok=True)
    assert seen == [False]
    assert "projection_pending" in _reason(runtime)
    # The publication itself completed its projection, untouched.
    assert runtime.manager.effective_calls[-1][0] is True
    assert runtime._runtime_egress_readiness is published
    assert runtime._runtime_egress_readiness_projected_revision == (
        runtime._runtime_egress_readiness_publication_revision)
    monkeypatch.setattr(runtime, "_source_control_final_exit_ready", real)
    assert runtime._maybe_carry_runtime_ready("t") is True


def test_a_route_move_between_plan_and_commit_is_never_stamped(tmp_path, monkeypatch):
    pub, _ready = _moved(tmp_path)
    runtime = pub.runtime
    real = runtime._runtime_ready_carry_commit

    def moved_first(plan):
        _invalidate(runtime)
        with runtime._runtime_route_snapshot_lock:
            runtime._runtime_route_epoch += 1
        return real(plan)

    monkeypatch.setattr(runtime, "_runtime_ready_carry_commit", moved_first)
    assert runtime._maybe_carry_runtime_ready("t") is False
    assert "route_moved" in _reason(runtime)
    assert runtime._runtime_egress_readiness.route_epoch < runtime._runtime_route_epoch


def test_a_newer_ready_published_between_plan_and_commit_wins(tmp_path, monkeypatch):
    pub, _ready = _moved(tmp_path)
    runtime = pub.runtime
    real = runtime._runtime_ready_carry_commit
    newer = []

    def republished(plan):
        newer.append(_publish_ready(runtime))
        return real(plan)

    monkeypatch.setattr(runtime, "_runtime_ready_carry_commit", republished)
    assert runtime._maybe_carry_runtime_ready("t") is False
    assert "publication_moved" in _reason(runtime)
    assert runtime._runtime_egress_readiness is newer[0]


def test_a_planner_failure_returns_false(tmp_path, monkeypatch):
    pub, _ready = _moved(tmp_path)
    runtime = pub.runtime

    def broken():
        raise RuntimeError("boom")

    monkeypatch.setattr(runtime, "_runtime_ready_carry_plan", broken)
    assert runtime._maybe_carry_runtime_ready("t") is False


class _NoLock:
    def __enter__(self):
        raise AssertionError("the steady state took a lock")

    def __exit__(self, *_exc):
        return False

    def acquire(self, *_a, **_k):
        raise AssertionError("the steady state took a lock")

    def release(self):
        pass


def test_the_steady_state_takes_no_lock_and_refusals_are_memoized(tmp_path, monkeypatch):
    pub = _harness(tmp_path)
    runtime = pub.runtime
    _publish_ready(runtime)
    saved = (runtime._runtime_route_snapshot_lock, runtime._tier3_egress_activation_lock,
             runtime._core_transition_lock)
    runtime._runtime_route_snapshot_lock = runtime._tier3_egress_activation_lock = _NoLock()
    runtime._core_transition_lock = _NoLock()
    try:
        for _ in range(3):
            assert runtime._maybe_carry_runtime_ready("coordinator_idle") is False
    finally:
        (runtime._runtime_route_snapshot_lock, runtime._tier3_egress_activation_lock,
         runtime._core_transition_lock) = saved
    # A refusal on evidence is computed once per (snapshot, ledger revision, READY).
    _invalidate(runtime)
    pub.publish("exhausted")
    calls = []
    real = runtime._runtime_ready_carry_evidence_refusal

    def counted(snapshot, payload):
        calls.append(1)
        return real(snapshot, payload)

    monkeypatch.setattr(runtime, "_runtime_ready_carry_evidence_refusal", counted)
    for _ in range(4):
        assert runtime._maybe_carry_runtime_ready("t") is False
    assert len(calls) == 1
    # The coordinator's ticks are rate-limited per (READY, epoch, snapshot).
    runtime._runtime_ready_carry_rate = {}
    plans = []
    real_plan = runtime._runtime_ready_carry_plan

    def planned():
        plans.append(1)
        return real_plan()

    monkeypatch.setattr(runtime, "_runtime_ready_carry_plan", planned)
    for _ in range(5):
        runtime._maybe_carry_runtime_ready("coordinator_idle")
    assert len(plans) == 1


class _Recorder:
    def __init__(self, name, inner, held):
        self.name, self.inner, self.held = name, inner, held

    def __enter__(self):
        self.inner.acquire()
        self.held.append(self.name)
        return self

    def __exit__(self, *_exc):
        self.held.remove(self.name)
        self.inner.release()
        return False

    def acquire(self, *args, **kwargs):
        got = self.inner.acquire(*args, **kwargs)
        if got:
            self.held.append(self.name)
        return got

    def release(self):
        self.held.remove(self.name)
        self.inner.release()


def test_the_carry_never_nests_the_route_and_activation_locks(tmp_path):
    pub, _ready = _moved(tmp_path)
    runtime = pub.runtime
    held: list[str] = []
    seen: list[tuple[str, ...]] = []

    class _Watch(_Recorder):
        def __enter__(self):
            result = super().__enter__()
            seen.append(tuple(held))
            return result

    runtime._runtime_route_snapshot_lock = _Watch("route", runtime._runtime_route_snapshot_lock, held)
    runtime._tier3_egress_activation_lock = _Watch("activation", runtime._tier3_egress_activation_lock, held)
    runtime._runtime_route_snapshot_refresh_lock = _NoLock()
    runtime._runtime_egress_readiness_projection_lock = _NoLock()
    runtime.manager.lock = _NoLock()
    assert runtime._maybe_carry_runtime_ready("t") is True
    assert seen and all(not {"route", "activation"} <= set(names) for names in seen), seen


def test_the_coordinator_idle_tick_and_the_post_event_finally_call_the_carry(tmp_path, monkeypatch):
    pub = _harness(tmp_path)
    runtime = pub.runtime
    calls = []
    monkeypatch.setattr(runtime, "_maybe_carry_runtime_ready", lambda source: calls.append(source))
    monkeypatch.setattr(runtime, "_maybe_probe_runtime_failure_guard", lambda: calls.append("guard"))
    monkeypatch.setattr(runtime, "_handle_runtime_output_event", lambda *a, **k: None)
    items = [None, ("event", None, None)]

    def next_ingress(timeout_seconds=0.25):
        if not items:
            runtime.stop_event.set()
            return None
        return items.pop(0)

    monkeypatch.setattr(runtime._runtime_egress_coordinator, "next_ingress", next_ingress)
    runtime._runtime_failure_monitor_loop()
    assert calls[:2] == ["coordinator_idle", "coordinator_event"]
    assert "guard" not in calls

    runtime.stop_event.clear()
    calls.clear()
    items[:] = [None, ("event", None, None)]
    runtime._runtime_egress_recovery_profile = "coordinated"
    runtime._runtime_failure_monitor_loop()
    assert "coordinator_idle" not in calls and "coordinator_event" not in calls
    assert calls.count("guard") >= 2


def test_the_route_refresh_worker_carries_right_after_the_rearm(tmp_path, monkeypatch):
    pub = _harness(tmp_path)
    runtime = pub.runtime
    order = []
    done = threading.Event()
    monkeypatch.setattr(runtime, "_refresh_runtime_route_snapshot", lambda **_k: object())
    monkeypatch.setattr(runtime, "_try_arm_runtime_recovery", lambda *a, **k: order.append("arm"))

    def carry(source):
        order.append(source)
        done.set()

    monkeypatch.setattr(runtime, "_maybe_carry_runtime_ready", carry)
    runtime._queue_runtime_route_snapshot_refresh()
    assert done.wait(5)
    assert order == ["arm", "route_refresh"]


def test_the_existing_route_epoch_rule_is_unchanged():
    from tests.test_runtime_lifecycle_status import test_route_epoch_advance_without_new_readiness_is_stale

    test_route_epoch_advance_without_new_readiness_is_stale()


def test_the_rate_window_constant_is_small():
    assert 0 < desktop_runtime_module._RUNTIME_READY_CARRY_RATE_SECONDS <= 0.25


# Review round (wf_2adb3643-35d): guards the first suite could lose silently.


@pytest.mark.parametrize("opened", ["mutation_pending", "incident_pending", "reload_gate"])
def test_a_transaction_opened_between_plan_and_commit_wins(tmp_path, monkeypatch, opened):
    pub, _ready = _moved(tmp_path)
    runtime = pub.runtime
    real = runtime._runtime_ready_carry_commit

    def open_then_commit(plan):
        if opened == "mutation_pending":
            runtime._core_route_mutation_pending = True
        elif opened == "incident_pending":
            runtime._runtime_incident_recovery_state = {"incident-1": "pending"}
        else:
            gate = runtime._runtime_reload_gate.snapshot()
            runtime._runtime_reload_gate.snapshot = lambda: SimpleNamespace(
                **{**_gate_fields(gate), "active_reload_reason": "busy"})
        return real(plan)

    monkeypatch.setattr(runtime, "_runtime_ready_carry_commit", open_then_commit)
    before = runtime._runtime_egress_readiness
    assert runtime._maybe_carry_runtime_ready("t") is False
    assert "transaction" in _reason(runtime)
    assert runtime._runtime_egress_readiness is before


def test_a_ledger_verdict_for_the_same_snapshot_re_evaluates_the_evidence(tmp_path):
    """The memo is keyed on the ledger revision: Phase B's verdict counts at once."""

    pub = _harness(tmp_path, "failover")  # READY on n2; n1 is a physical move
    runtime = pub.runtime
    runtime._tier3_verified_provider_writes_enabled = lambda: True
    _publish_ready(runtime)
    _invalidate(runtime)
    pub.publish("rebuilt")
    assert runtime._maybe_carry_runtime_ready("t") is False
    assert "evidence:unverdicted" in _reason(runtime)
    runtime._tier3_health_ledger.observe("n1", True, time.time())
    assert runtime._maybe_carry_runtime_ready("t") is True


def _gateway(tmp_path, state="gateway_healthy"):
    """A published Gateway route: FINAL-EXIT -> EXIT-SELECT -> gw, gw dialing through PROXY-FALLBACK."""

    pub = _harness(tmp_path, state)
    runtime = pub.runtime
    proxy_map = dict(runtime._runtime_loaded_export_contract["proxy_map"])
    proxy_map["gw"] = {"proxy_name": "gw", "tier": "exit", "pool_kind": "gateway", "identity": "identity-gw",
                       "node": None, "proxy": {"name": "gw", "type": "http", "dialer-proxy": "PROXY-FALLBACK"}}
    runtime._runtime_loaded_export_contract = {**runtime._runtime_loaded_export_contract, "proxy_map": proxy_map}
    runtime._runtime_proxy_name_map = lambda *_a, **_k: dict(proxy_map)
    pub.publish(state)
    snapshot = runtime._runtime_route_snapshot
    binding = runtime._runtime_route_binding_for_outbound(
        snapshot, runtime._runtime_route_snapshot_payload, "FINAL-EXIT")
    assert binding is not None and binding.node.pool_kind == "gateway"
    return pub


def test_a_gateway_underlay_move_is_carried_on_the_underlay_leaf_verdict(tmp_path):
    pub = _gateway(tmp_path)
    runtime = pub.runtime
    _publish_ready(runtime)
    epoch = runtime._runtime_route_epoch
    pub.publish("gateway_failover")
    assert runtime._runtime_route_epoch == epoch + 1  # the underlay is in the selection signature
    assert _status(runtime)[0] is True
    assert runtime._maybe_carry_runtime_ready("t") is True
    assert _status(runtime) == (False, "running")


def test_a_gateway_whose_underlay_leaf_is_dead_is_never_carried(tmp_path):
    pub = _gateway(tmp_path)
    runtime = pub.runtime
    _publish_ready(runtime)
    _invalidate(runtime)
    pub.publish("gateway_underlay_dead")
    assert runtime._maybe_carry_runtime_ready("t") is False
    assert "evidence:underlay_not_alive" in _reason(runtime), runtime._runtime_ready_carry_counts
    assert _status(runtime)[0] is True


def test_a_stale_pin_is_never_carried(tmp_path, monkeypatch):
    pub = _harness(tmp_path)
    runtime = pub.runtime
    _publish_ready(runtime)
    # The publication would also queue the existing stale-pin release (and the
    # carry would wait for it); keep it out to reach the evidence rule itself.
    monkeypatch.setattr(runtime, "_queue_stale_fallback_pin_release", lambda *a, **k: False)
    _invalidate(runtime)
    pub.publish("stale_pin")
    assert runtime._runtime_route_snapshot.group_state("TIER3-AUTO").fixed == "n3"
    assert runtime._maybe_carry_runtime_ready("t") is False
    assert "evidence:stale_pin" in _reason(runtime), runtime._runtime_ready_carry_counts


def test_a_call_inside_this_threads_core_transaction_never_commits(tmp_path):
    pub, _ready = _moved(tmp_path)
    runtime = pub.runtime
    with runtime._core_transition_lock:
        assert runtime._maybe_carry_runtime_ready("t") is False
    assert "reentrant" in _reason(runtime)
    assert runtime._maybe_carry_runtime_ready("t") is True


def test_a_stale_pin_release_in_flight_defers_the_carry(tmp_path):
    """Review LR-1: the release takes the Core lock without waiting and backs
    off 5 s when busy, so the carry never competes with one in flight."""

    pub, _ready = _moved(tmp_path)
    runtime = pub.runtime
    state = runtime._node_judgement_state()
    with state.lock:
        state.stale_pin_inflight.add((1, "TIER3-AUTO", "n9"))
    assert runtime._maybe_carry_runtime_ready("t") is False
    assert "stale_pin_release" in _reason(runtime)
    with state.lock:
        state.stale_pin_inflight.clear()
    assert runtime._maybe_carry_runtime_ready("t") is True


def test_a_busy_route_lock_never_holds_the_core_lock(tmp_path, monkeypatch):
    pub, _ready = _moved(tmp_path)
    runtime = pub.runtime
    real = runtime._runtime_ready_carry_commit
    held = threading.Event()
    release = threading.Event()

    def hold_route():
        with runtime._runtime_route_snapshot_lock:
            held.set()
            release.wait(10)

    def commit_while_route_busy(plan):
        thread = threading.Thread(target=hold_route, daemon=True)
        thread.start()
        assert held.wait(5)
        try:
            return real(plan)
        finally:
            release.set()
            thread.join(5)

    monkeypatch.setattr(runtime, "_runtime_ready_carry_commit", commit_while_route_busy)
    assert runtime._maybe_carry_runtime_ready("t") is False
    assert "route_busy" in _reason(runtime)
    monkeypatch.setattr(runtime, "_runtime_ready_carry_commit", real)
    assert runtime._maybe_carry_runtime_ready("t") is True


def _wrapper(tmp_path, state):
    """READY on ``healthy``, then an app invalidation and a Selector-wrapper route."""

    pub = _harness(tmp_path)
    runtime = pub.runtime
    proxy_map = dict(runtime._runtime_loaded_export_contract["proxy_map"])
    for name in ("L1", "L2"):
        proxy_map[name] = {"proxy_name": name, "tier": "tier1", "pool_kind": "tier1", "identity": f"identity-{name}",
                           "node": None, "proxy": {"name": name, "type": "http"}}
    runtime._runtime_loaded_export_contract = {**runtime._runtime_loaded_export_contract, "proxy_map": proxy_map}
    runtime._runtime_proxy_name_map = lambda *_a, **_k: dict(proxy_map)
    _publish_ready(runtime)
    _invalidate(runtime)
    pub.publish(state)
    route = runtime._runtime_route_snapshot.group_state("TIER1-AUTO")
    assert route is not None and route.now == "P1-SELECT"
    return pub


def test_a_selector_wrapper_carries_on_the_groups_own_round(tmp_path):
    """Review 11-04: TIER1-AUTO records its round on the P1-SELECT wrapper it
    tests; L1's own record (an old False) is not the group's verdict."""

    pub = _wrapper(tmp_path, "wrapper_alive_leaf_stale")
    runtime = pub.runtime
    assert runtime._maybe_carry_runtime_ready("t") is True, runtime._runtime_ready_carry_counts
    assert _status(runtime) == (False, "running")


def test_a_selector_wrapper_the_group_found_dead_is_never_carried(tmp_path):
    """The reverse: L1's old True never hides the group's failed round."""

    pub = _wrapper(tmp_path, "wrapper_dead_leaf_alive")
    runtime = pub.runtime
    assert runtime._maybe_carry_runtime_ready("t") is False
    assert "evidence:not_alive" in _reason(runtime), runtime._runtime_ready_carry_counts
    assert _status(runtime)[0] is True


def test_the_carry_sends_no_data_plane_probe_either(tmp_path, monkeypatch):
    """The user's condition beyond the controller: no mixed-port or FINAL-EXIT check."""

    pub, _ready = _moved(tmp_path)
    runtime = pub.runtime
    probes = []
    for name in ("_probe_mihomo_mixed_port_once", "_verify_runtime_final_exit", "_probe_runtime_leaf",
                 "_probe_mihomo_mixed_port_once_leased"):
        if hasattr(runtime, name):
            monkeypatch.setattr(runtime, name, lambda *a, _n=name, **k: probes.append(_n))
    calls = list(pub.controller.calls)
    assert runtime._maybe_carry_runtime_ready("t") is True
    assert probes == [] and pub.controller.calls == calls
