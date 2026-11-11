"""Decision D option A (11-04): READY after a verified strict Core reload.

A user graph reload (settings save, import, node edit) during an automatic
route lease bumps the user intent, so the READY carry (§8q) can never
re-stamp the old READY and the service stayed "starting".  The strict
reload's own authoritative FINAL-EXIT 204 is the proof: its success tail
republishes READY with the new intent, the same Core owner and exactly the
route and network epoch that 204 saw.  No new probe or controller request.

Driven through the real ``_reload_core_config_serialized(mode="strict")`` in
formal_capture with the strict-reload harness of
``test_tier3_strict_reload_core_names`` (real renderer, repair plan and
qualification; fake runner and controller; no Core process, no network), and
through the real ``reload_core_config`` for the basis rules.
"""

from __future__ import annotations

import pytest

from models import Node, NodeStatus
from runtime_egress_recovery import RuntimeEgressReadinessState
from runtime_reload_gate import RuntimeReloadReason
from tests.test_desktop_runtime_recovery import FakeLifecycleCoreRunner
from tests.test_tier3_strict_reload_core_names import _ExactNameController
from tests.test_tier3_strict_reload_core_names import _runtime as _strict_runtime

READY = RuntimeEgressReadinessState.READY


def _owner_fact(runner, pid=4242, generation=1):
    runner.status_payload = {**runner.status_payload, "pid": pid, "process_generation": generation}
    runner.lightweight_fact = lambda: {"running": True, "pid": pid, "process_generation": generation,
                                       "cleanup_pending": False}


def _loaded(tmp_path):
    """A formal_capture runtime with a published route and an anchored READY at intent 0."""

    runtime = _strict_runtime(tmp_path, verified=True)
    runner = FakeLifecycleCoreRunner()
    runner.start()
    _owner_fact(runner)
    runtime.core_runner = runner
    runtime._mihomo_controller_config = lambda: {"enabled": True, "host": "127.0.0.1", "port": 9090, "secret": ""}
    runtime._prime_tier3_pool_before_core_start = lambda *a, **k: None
    with runtime._core_lifecycle_lock:
        runtime._core_runtime_phase = "formal_capture"
        runtime._core_generation = 1
    runtime._runtime_initial_activation_generation = 1
    # The fake controller first: marking the export synced queues a route
    # re-read, which must never reach a real controller on 127.0.0.1:9090.
    runtime.api_client = _ExactNameController(runtime._tier3_core_provider_file_path())
    runtime._probe_urls_for_node = lambda _node: ["http://127.0.0.1:9/generate_204"]
    runtime.write_mihomo_runtime_config()
    runtime._mark_core_export_sync_success()
    runtime._last_synced_tier3_nodes = runtime._clone_runtime_nodes(
        runtime._tier3_nodes_for_core_export(runtime._mihomo_tier3_export_limit()))
    runtime.stop_core = lambda **_k: pytest.fail("the strict reload stopped the Core")
    runtime.running = True
    runtime._desired_running = True
    runtime.manager.effective_egress_ok = True
    assert runtime._refresh_runtime_route_snapshot(timeout_seconds=1.0) is not None
    ready = runtime._publish_runtime_egress_readiness(READY, validation_active=False, reason="verified")
    assert ready.route_owner == (4242, 1, 1) and ready.selector_intent_revision == 0
    return runtime, runner


def _lease_bumped_basis(runtime):
    """What reload_core_config captures for a user graph reload during an automatic lease."""

    seen = runtime._verified_reload_ready_seen()
    assert seen is not None
    runtime._runtime_selector_intent_revision += 1  # the lease branch's bump
    ready, revision, generation, owner = seen
    return (ready, revision, generation, int(runtime._runtime_selector_intent_revision), owner)


def _status(runtime):
    status = runtime.lifecycle_status()
    return bool(status["freshness"]["stale"]), status["runtime"]["startup_phase"]


def _count_requests(runtime):
    """Every controller read and every probe seam, counted."""

    controller = runtime.api_client
    counts = {"proxies": 0, "probes": 0}
    real_proxies = controller.proxies

    def proxies(*args, **kwargs):
        counts["proxies"] += 1
        return real_proxies(*args, **kwargs)

    controller.proxies = proxies
    for name in ("_probe_mihomo_mixed_port", "_probe_mihomo_mixed_port_once", "_verify_runtime_final_exit",
                 "_probe_runtime_leaf"):
        real = getattr(runtime, name, None)
        if callable(real):
            def counted(*args, _real=real, **kwargs):
                counts["probes"] += 1
                return _real(*args, **kwargs)
            setattr(runtime, name, counted)
    return controller, counts


def test_a_verified_strict_reload_republishes_ready_with_the_new_intent(tmp_path):
    runtime, _runner = _loaded(tmp_path)
    before = runtime._runtime_egress_readiness
    basis = _lease_bumped_basis(runtime)
    controller, counts = _count_requests(runtime)
    real = runtime._republish_ready_after_verified_reload
    in_tail: list[tuple[int, int, int]] = []

    def watched(tail_basis):
        start = (counts["proxies"], counts["probes"], len(controller.delay_calls))
        outcome = real(tail_basis)
        in_tail.append((counts["proxies"] - start[0], counts["probes"] - start[1],
                        len(controller.delay_calls) - start[2]))
        return outcome

    runtime._republish_ready_after_verified_reload = watched
    result = runtime._reload_core_config_serialized(mode="strict", ready_basis=basis)

    assert result.get("reloaded") is True, result
    assert runtime._runtime_reload_ready_counts == {"published": 1}
    assert in_tail == [(0, 0, 0)]  # the tail reads no controller and tests no node
    now = runtime._runtime_egress_readiness
    assert now is not before and now.state is READY and not now.validation_active
    assert now.selector_intent_revision == runtime._runtime_selector_intent_revision == 1
    assert now.route_owner == (4242, 1, 1) and now.route_physical == before.route_physical
    assert (now.lifecycle_revision, now.network_context_epoch, now.reason) == (
        before.lifecycle_revision, before.network_context_epoch, before.reason)

    assert _status(runtime) == (False, "running")
    # The reload's own re-read: the signature's user-intent element changed,
    # so the epoch moves although no traffic did.  The carry re-stamps that
    # on the unchanged physical route; it is driven on real captures in
    # tests/test_runtime_ready_carry.py (this fake controller serves no
    # FINAL-EXIT group).
    assert runtime._refresh_runtime_route_snapshot(timeout_seconds=1.0) is not None
    assert runtime._runtime_route_epoch == now.route_epoch + 1
    assert runtime._runtime_egress_readiness.route_physical == now.route_physical


def test_a_re_read_between_the_proof_and_the_tail_is_accepted_when_no_traffic_moved(tmp_path):
    """Review 11-04: a routine re-read after the 204 moves the epoch on the
    intent element alone; the tail publishes at that epoch instead of giving
    up (which would have left Q1 permanent)."""

    runtime, _runner = _loaded(tmp_path)
    basis = _lease_bumped_basis(runtime)
    epochs = []

    def reread():
        assert runtime._refresh_runtime_route_snapshot(timeout_seconds=1.0) is not None
        epochs.append(runtime._runtime_route_epoch)

    _interfere(runtime, reread)
    runtime._reload_core_config_serialized(mode="strict", ready_basis=basis)
    assert runtime._runtime_reload_ready_counts == {"published": 1}
    now = runtime._runtime_egress_readiness
    assert now.route_epoch == epochs[0] == runtime._runtime_route_epoch
    assert now.selector_intent_revision == 1
    assert _status(runtime) == (False, "running")


def test_a_physical_move_after_the_proof_refuses(tmp_path):
    runtime, _runner = _loaded(tmp_path)
    basis = _lease_bumped_basis(runtime)
    before = runtime._runtime_egress_readiness

    def moved():
        with runtime._runtime_route_snapshot_lock:
            signature = runtime._runtime_route_selection_signature
            runtime._runtime_route_selection_signature = signature + (("moved", 1),)
            runtime._runtime_route_epoch += 1
            snapshot = runtime._runtime_route_snapshot
            runtime._runtime_route_snapshot = type(snapshot)(
                process_pid=snapshot.process_pid, process_generation=snapshot.process_generation,
                core_generation=snapshot.core_generation, semantic_digest=snapshot.semantic_digest,
                route_epoch=runtime._runtime_route_epoch,
                network_context_epoch=snapshot.network_context_epoch,
                group_graph=snapshot.group_graph, group_states=snapshot.group_states,
                proxy_nodes=snapshot.proxy_nodes)

    _interfere(runtime, moved)
    runtime._reload_core_config_serialized(mode="strict", ready_basis=basis)
    assert runtime._runtime_reload_ready_counts == {"route_moved": 1}
    assert runtime._runtime_egress_readiness is before


def test_without_option_a_the_ready_keeps_the_old_intent(tmp_path):
    """The Q1 symptom: the carry's intent gate can never pass again."""

    runtime, _runner = _loaded(tmp_path)
    before = runtime._runtime_egress_readiness
    _lease_bumped_basis(runtime)
    result = runtime._reload_core_config_serialized(mode="strict")
    assert result.get("reloaded") is True, result
    assert runtime._runtime_egress_readiness is before
    assert before.selector_intent_revision != runtime._runtime_selector_intent_revision
    assert runtime._runtime_reload_ready_counts == {}


def _interfere(runtime, action):
    """Run ``action`` right after the 204 passed and was stamped (the warm-up runs there)."""

    real = runtime._warm_tier3_group_after_core_start

    def warm(*args, **kwargs):
        action()
        return real(*args, **kwargs)

    runtime._warm_tier3_group_after_core_start = warm


@pytest.mark.parametrize("case", [
    "controller_hold", "route_epoch", "network_epoch", "core_owner", "stopping",
    "activation_pending", "intent_declared", "lifecycle",
])
def test_anything_that_moved_after_the_proof_wins(tmp_path, case):
    runtime, runner = _loaded(tmp_path)
    basis = _lease_bumped_basis(runtime)
    published = []

    def act():
        if case == "controller_hold":
            published.append(runtime._publish_runtime_egress_readiness(
                RuntimeEgressReadinessState.CONTROLLER_HOLD, validation_active=False))
        elif case == "route_epoch":
            with runtime._runtime_route_snapshot_lock:
                runtime._runtime_route_epoch += 1
        elif case == "network_epoch":
            with runtime._runtime_route_snapshot_lock:
                runtime._runtime_network_context_epoch += 1
        elif case == "core_owner":
            _owner_fact(runner, generation=2)
        elif case == "stopping":
            runtime.stop_event.set()
        elif case == "activation_pending":
            runtime._tier3_egress_activation_pending = True
        elif case == "intent_declared":
            runtime._runtime_selector_intent_revision += 1  # a user selection declared meanwhile
        else:
            runtime._lifecycle_revision += 1

    _interfere(runtime, act)
    before = runtime._runtime_egress_readiness
    try:
        runtime._reload_core_config_serialized(mode="strict", ready_basis=basis)
    finally:
        runtime.stop_event.clear()
    counts = runtime._runtime_reload_ready_counts
    assert "published" not in counts, counts
    now = runtime._runtime_egress_readiness
    if case == "controller_hold":
        assert now is published[0] and now.state is RuntimeEgressReadinessState.CONTROLLER_HOLD
    else:
        assert now is before
        assert now.selector_intent_revision == 0


def _probe_on(runtime, state):
    """The 204's route-ready wait accepted the live ``state`` capture (the reload moved the route)."""

    from tests.real_route_publication import captured_proxies

    real = runtime._probe_mihomo_mixed_port

    def probe(*args, **kwargs):
        runtime._route_ready_view = captured_proxies(state)
        return real(*args, **kwargs)

    runtime._probe_mihomo_mixed_port = probe
    return captured_proxies(state)


def _physical_of(runtime, payload):
    contract = runtime._runtime_pending_export_contract or runtime._runtime_loaded_export_contract or {}
    signature = runtime._runtime_final_exit_selection_signature(payload, contract.get("proxy_map") or {})
    return tuple(item for item in signature if item[0] != "user_intent_revision")


def test_the_proof_names_the_route_the_204_ran_on_not_the_pre_reload_snapshot(tmp_path):
    """Review 11-04: the reload may change the serving path; READY must carry
    the route the 204 proved, so the reload's own re-read of it is a
    same-route move for the carry."""

    runtime, _runner = _loaded(tmp_path)
    basis = _lease_bumped_basis(runtime)
    view = _probe_on(runtime, "failover")
    pre_reload = runtime._runtime_egress_readiness.route_physical
    runtime._reload_core_config_serialized(mode="strict", ready_basis=basis)
    assert runtime._runtime_reload_ready_counts == {"published": 1}
    now = runtime._runtime_egress_readiness
    assert now.route_physical == _physical_of(runtime, view) != pre_reload


def test_a_re_read_of_the_204_route_after_the_proof_is_accepted(tmp_path):
    from tests.real_route_publication import CapturedController

    runtime, _runner = _loaded(tmp_path)
    basis = _lease_bumped_basis(runtime)
    view = _probe_on(runtime, "failover")
    epoch = runtime._runtime_route_epoch

    def reread_live_route():
        runtime.api_client = CapturedController("failover")
        assert runtime._refresh_runtime_route_snapshot(timeout_seconds=1.0) is not None

    _interfere(runtime, reread_live_route)
    runtime._reload_core_config_serialized(mode="strict", ready_basis=basis)
    assert runtime._runtime_route_epoch == epoch + 1
    assert runtime._runtime_reload_ready_counts == {"published": 1}
    now = runtime._runtime_egress_readiness
    assert now.route_epoch == epoch + 1 and now.route_physical == _physical_of(runtime, view)
    assert _status(runtime) == (False, "running")


def test_the_route_ready_wait_records_the_view_it_accepted(tmp_path, monkeypatch):
    from tests.real_route_publication import CapturedController

    runtime, _runner = _loaded(tmp_path)
    runtime.api_client = CapturedController("healthy")
    runtime._route_ready_view = None
    monkeypatch.setattr(runtime, "_mihomo_selected_routes_ready", lambda _payload: {"ready": True, "routes": {}})
    monkeypatch.setattr(runtime, "_mihomo_runtime_graph_ready", lambda *_a, **_k: {"ready": True})
    monkeypatch.setattr(runtime, "_mihomo_rule_providers_ready", lambda *_a, **_k: {"ready": True})
    from desktop_runtime import DesktopRuntime

    # The recovery harness stubs the instance's wait: run the real method.
    DesktopRuntime._wait_for_mihomo_route_ready(runtime, timeout=1.0)
    assert runtime._route_ready_view == CapturedController("healthy").proxies()


@pytest.mark.parametrize("case", ["fresh_ready", "snapshot_owner", "phase", "recovery"])
def test_the_store_guards_refuse_what_the_pre_checks_cannot_see(tmp_path, case):
    """Review 11-04: cases only the publisher's CAS (or one late check) can refuse."""

    runtime, _runner = _loaded(tmp_path)
    basis = _lease_bumped_basis(runtime)
    newer = []

    def act():
        if case == "fresh_ready":
            # Same epoch, owner and intent: only the publication revision moved.
            newer.append(runtime._publish_runtime_egress_readiness(READY, validation_active=False,
                                                                   reason="another owner"))
        elif case == "snapshot_owner":
            with runtime._runtime_route_snapshot_lock:
                snapshot = runtime._runtime_route_snapshot
                runtime._runtime_route_snapshot = type(snapshot)(
                    process_pid=snapshot.process_pid, process_generation=9,
                    core_generation=snapshot.core_generation, semantic_digest=snapshot.semantic_digest,
                    route_epoch=snapshot.route_epoch, network_context_epoch=snapshot.network_context_epoch,
                    group_graph=snapshot.group_graph, group_states=snapshot.group_states,
                    proxy_nodes=snapshot.proxy_nodes)
        elif case == "phase":
            with runtime._core_lifecycle_lock:
                runtime._core_runtime_phase = "formal_no_capture"
        else:
            runtime._runtime_egress_coordinator.core_mutation_active = lambda: True

    _interfere(runtime, act)
    before = runtime._runtime_egress_readiness
    try:
        runtime._reload_core_config_serialized(mode="strict", ready_basis=basis)
    finally:
        runtime._runtime_egress_coordinator.__dict__.pop("core_mutation_active", None)
    counts = runtime._runtime_reload_ready_counts
    assert "published" not in counts, counts
    expected = {"fresh_ready": "publication_moved", "snapshot_owner": "publication_moved",
                "phase": "phase", "recovery": "recovery"}[case]
    assert counts == {expected: 1}, counts
    if case == "fresh_ready":
        assert runtime._runtime_egress_readiness is newer[0]
    else:
        assert runtime._runtime_egress_readiness is before


def test_a_failed_verification_publishes_nothing(tmp_path):
    runtime, _runner = _loaded(tmp_path)
    basis = _lease_bumped_basis(runtime)
    before = runtime._runtime_egress_readiness

    def failed_204(*_a, **_k):
        raise RuntimeError("FINAL-EXIT 204 failed")

    runtime._verify_tier3_final_exit_for_activation = failed_204
    runtime._probe_mihomo_mixed_port = failed_204
    runtime._reload_core_config_serialized(mode="strict", ready_basis=basis)
    assert "published" not in runtime._runtime_reload_ready_counts
    assert runtime._core_reload_final_exit_proof is None
    assert runtime._runtime_egress_readiness is before or (
        runtime._runtime_egress_readiness.state is not READY)


# --- the basis rules, through the real reload_core_config ---------------------------------


def _capture_basis(runtime):
    seen = []

    def serialized(*, mode="strict", ready_basis=None):
        seen.append(ready_basis)
        return {"reloaded": True, "core": {"running": True}}

    runtime._reload_core_config_serialized = serialized
    runtime._runtime_reload_gate = None  # the gate is not under test here
    return seen


def _reload(runtime, **kwargs):
    kwargs.setdefault("mode", "strict")
    kwargs.setdefault("reason", RuntimeReloadReason.USER_GRAPH_CONFIG)
    kwargs.setdefault("user_commit_id", "u1")
    return runtime.reload_core_config(**kwargs)


def test_a_user_graph_reload_during_a_lease_hands_the_post_bump_intent_to_the_tail(tmp_path):
    runtime, _runner = _loaded(tmp_path)
    seen = _capture_basis(runtime)
    runtime._runtime_route_mutation_lease = {"kind": "fixed"}
    ready = runtime._runtime_egress_readiness
    _reload(runtime)
    assert runtime._runtime_selector_intent_revision == 1
    assert len(seen) == 1 and seen[0] is not None
    basis_ready, _revision, _generation, intent_after, owner = seen[0]
    assert basis_ready is ready and intent_after == 1 and owner == (4242, 1, 1)


@pytest.mark.parametrize("case", [
    "gateway_pick_flag", "controller_hold", "incremental_export", "declared_selection_pending",
    "lifecycle_stale", "anchored_elsewhere", "non_early_profile", "validating",
])
def test_the_reload_hands_no_basis_when_the_ready_is_not_its_to_reprove(tmp_path, case):
    runtime, runner = _loaded(tmp_path)
    seen = _capture_basis(runtime)
    kwargs = {}
    if case == "gateway_pick_flag":
        kwargs["publish_verified_ready"] = False
    elif case == "controller_hold":
        runtime._publish_runtime_egress_readiness(RuntimeEgressReadinessState.CONTROLLER_HOLD,
                                                  validation_active=False)
    elif case == "incremental_export":
        kwargs["mode"] = "incremental_export"
        runtime._last_core_export_sync_marker = None
    elif case == "declared_selection_pending":
        runtime._runtime_selector_intent_revision += 1  # a pick declared, waiting for the Core lock
    elif case == "lifecycle_stale":
        runtime._lifecycle_revision += 1
    elif case == "anchored_elsewhere":
        _owner_fact(runner, generation=2)
    elif case == "non_early_profile":
        runtime._runtime_egress_recovery_profile = "coordinated"
    else:
        runtime._publish_runtime_egress_readiness(READY, validation_active=True)
    _reload(runtime, **kwargs)
    assert seen and seen[-1] is None, case


def test_a_proof_left_by_an_earlier_call_never_reaches_this_reload(tmp_path):
    runtime, _runner = _loaded(tmp_path)
    proofs = []

    def serialized(*, mode="strict", ready_basis=None):
        proofs.append((runtime._core_reload_final_exit_proof, runtime._route_ready_view))
        return {"reloaded": True, "core": {"running": True}}

    runtime._reload_core_config_serialized = serialized
    runtime._runtime_reload_gate = None
    runtime._core_reload_final_exit_proof = (1, 0, (4242, 1, 1), ())  # stale: from another 204
    runtime._route_ready_view = {"proxies": {}}  # stale: an earlier route-ready wait's view
    _reload(runtime)
    assert proofs == [(None, None)]


# The Gateway pick's reload passes publish_verified_ready=False: asserted by the
# reload fake of tests/test_gateway_identity_runtime.py on every pick it drives.
