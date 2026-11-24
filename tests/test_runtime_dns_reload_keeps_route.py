"""A Core reload that moves only the ``dns`` block keeps the verified route (11-23).

Field report 11-23 (0.1.6, a Gateway exit over the fallback layers, Windows
system proxy): about 30 s after a start the resolver watch saw a new link
fingerprint, re-qualified, and hot-reloaded a config whose only change was the
``dns`` block.  The incremental export took that for a FINAL-EXIT route
mutation: it rolled capture back, so the Windows proxy went away, and the
shipped early profile parked readiness in CONTROLLER_HOLD ("FINAL-EXIT route
changed; automatic runtime revalidation is disabled") with no verifier ever
coming.  The service read "starting" until the user restarted it, and every
source refresh failed for want of a route.

Driven through the real ``_apply_dns_resolver_plan_to_running_core`` (the
watch's call) and ``reload_core_config(mode="incremental_export",
reason=DNS_LIFECYCLE)`` on the verified-reload harness of
``test_runtime_reload_ready`` (real renderer, provider projection, export
contracts and reload gate; fake runner and controller; no Core process, no
network).  Its controller serves no Bastion FINAL-EXIT leaf, so no Tier3
evidence can carry the route: the path a Gateway or fallback-layer exit takes.
"""

from __future__ import annotations

import threading
import time

import pytest
import yaml

from dns_resolver_policy import (
    CANDIDATE_QUALIFIED,
    LINK_ANSWERS_PUBLIC,
    DnsResolverPlan,
    EncryptedCandidateResult,
    LinkResolverResult,
    ResolverQualification,
    choose_rung,
    encrypted_candidates,
    ladder_for_mode,
    render_dns_block,
)
from models import Node, NodeStatus
from runtime_egress_recovery import RuntimeEgressReadinessState
from runtime_reload_gate import RuntimeReloadReason
from tests.test_runtime_reload_ready import _loaded, _status

READY = RuntimeEgressReadinessState.READY
CANDIDATES = {candidate.url: candidate for candidate in encrypted_candidates()}


def _plan(runtime, *, resolver="192.168.43.1", url="tls://223.5.5.5"):
    """The plan a re-qualification on the 11-23 hotspot link produced."""

    qualification = ResolverQualification(
        fingerprint=f"fp-{resolver}",
        wall_time_ms=5,
        link_resolvers=(LinkResolverResult(resolver, LINK_ANSWERS_PUBLIC, 4, 7, True),),
        candidates=(EncryptedCandidateResult(CANDIDATES[url], CANDIDATE_QUALIFIED, 20),),
        inconclusive=False,
    )
    decision = choose_rung(qualification, ladder=ladder_for_mode("auto"))
    plan = DnsResolverPlan(
        decision=decision,
        qualification=qualification,
        block=render_dns_block(runtime._dns_resolver_formal_block(), decision, qualification),
        revision=2,
        applied_at_monotonic=time.monotonic(),
    )
    with runtime._dns_resolver_plan_lock:
        runtime._dns_resolver_plan = plan
    return plan


def _watch(runtime):
    """Record route invalidations, capture rollbacks, rechecks and Core loads."""

    seen = {"mutations": [], "rollbacks": [], "rechecks": 0, "hot_reloads": 0}
    real_begin = runtime._begin_core_route_mutation
    real_rollback = runtime._rollback_to_formal_no_capture
    real_recheck = runtime._queue_incremental_egress_recheck
    real_hot_reload = runtime.core_runner.hot_reload

    def begin(reason):
        seen["mutations"].append((reason, seen["hot_reloads"]))
        return real_begin(reason)

    def rollback(runner, reason, **kwargs):
        seen["rollbacks"].append(reason)
        return real_rollback(runner, reason, **kwargs)

    def recheck():
        seen["rechecks"] += 1
        return real_recheck()

    def hot_reload(**kwargs):
        seen["hot_reloads"] += 1
        return real_hot_reload(**kwargs)

    runtime._begin_core_route_mutation = begin
    runtime._rollback_to_formal_no_capture = rollback
    runtime._queue_incremental_egress_recheck = recheck
    runtime.core_runner.hot_reload = hot_reload
    return seen


def _settle():
    """Let the route re-read a Core sync queues finish (it may carry READY)."""

    for thread in list(threading.enumerate()):
        if thread.name == "runtime-route-snapshot-refresh":
            thread.join(5)
            assert not thread.is_alive()


def _loaded_dns(runtime):
    path = runtime._runtime_config_path_for_client()
    return yaml.safe_load(path.read_text(encoding="utf-8"))["dns"]


def _grow_bastion(runtime):
    runtime.manager.runtime_nodes["tier3"].append(
        Node(name="delta", protocol="http", uri="http://delta.example.invalid:8080",
             address="delta.example.invalid", port=8080, tier="tier3", status=NodeStatus.ALIVE))


@pytest.fixture
def runtime(tmp_path):
    runtime, _runner = _loaded(tmp_path)
    try:
        yield runtime
    finally:
        runtime.stop_event.set()


def test_the_network_watch_reload_keeps_ready_capture_and_the_route(runtime):
    before = runtime._runtime_egress_readiness
    plan = _plan(runtime)
    assert _loaded_dns(runtime) != dict(plan.block)
    seen = _watch(runtime)

    assert runtime._apply_dns_resolver_plan_to_running_core(plan) is True
    _settle()

    # The Core loaded the new resolvers once ...
    assert seen["hot_reloads"] == 1 and _loaded_dns(runtime) == dict(plan.block)
    # ... and nothing treated that as a route change.
    assert seen["mutations"] == [] and seen["rollbacks"] == [] and seen["rechecks"] == 0
    assert runtime._runtime_egress_readiness is before and before.state is READY
    assert runtime.manager.effective_egress_ok is True
    assert runtime._current_core_runtime_phase() == "formal_capture"
    assert runtime._revalidation_capture_active("formal_capture") is True
    assert _status(runtime) == (False, "running")
    # The same plan again is nothing to apply.
    assert runtime._apply_dns_resolver_plan_to_running_core(plan) is False
    assert seen["hot_reloads"] == 1


def test_an_escalation_or_settings_reload_of_dns_alone_keeps_the_route_too(runtime):
    before = runtime._runtime_egress_readiness
    _plan(runtime, resolver="2001:db8:43::1", url="tls://1.12.12.12")
    seen = _watch(runtime)

    result = runtime.reload_core_config(mode="incremental_export", reason=RuntimeReloadReason.DNS_LIFECYCLE)
    _settle()

    assert result.get("reloaded") is True and not result.get("unchanged"), result
    assert seen["hot_reloads"] == 1 and seen["mutations"] == [] and seen["rollbacks"] == []
    assert runtime._runtime_egress_readiness is before
    assert _status(runtime) == (False, "running")


def test_a_reload_that_also_moves_the_graph_still_revokes_the_route_first(runtime):
    _plan(runtime)
    _grow_bastion(runtime)
    seen = _watch(runtime)

    result = runtime.reload_core_config(mode="incremental_export", reason=RuntimeReloadReason.DNS_LIFECYCLE)
    _settle()

    assert result.get("reloaded") is True, result
    # Invalidated before ApplyConfig, exactly as any graph change.
    assert seen["mutations"] and seen["mutations"][0][1] == 0
    assert seen["rollbacks"]
    assert runtime._runtime_egress_readiness.state is not READY
    assert runtime.manager.effective_egress_ok is not True
    assert _status(runtime)[1] == "core_standby"


def test_a_ready_no_capture_runtime_keeps_its_route_on_a_dns_only_reload(runtime):
    runtime._runtime_traffic_capture_wanted = lambda: False
    with runtime._core_lifecycle_lock:
        runtime._core_runtime_phase = "formal_no_capture"
    runtime.write_mihomo_no_capture_runtime_config()  # the graph this Core serves
    runtime._mark_core_export_sync_success()
    _settle()
    before = runtime._runtime_egress_readiness
    plan = _plan(runtime)
    seen = _watch(runtime)
    activations = []
    runtime._queue_tier3_egress_activation = lambda: activations.append("activation")

    assert runtime._apply_dns_resolver_plan_to_running_core(plan) is True
    _settle()

    assert seen["hot_reloads"] == 1 and _loaded_dns(runtime) == dict(plan.block)
    assert seen["mutations"] == [] and activations == []
    assert runtime._runtime_egress_readiness is before
    assert runtime.manager.effective_egress_ok is True
    assert _status(runtime) == (False, "running")


def test_dns_only_is_proved_against_the_exact_loaded_contract(runtime):
    path = runtime._runtime_config_path_for_client()
    loaded_text = path.read_text(encoding="utf-8")
    _plan(runtime)
    written = runtime.write_mihomo_runtime_config()
    assert runtime._loaded_config_change_is_dns_only(loaded_text, written) is True
    # A file that is not what the Core loaded proves nothing.
    assert runtime._loaded_config_change_is_dns_only(loaded_text + "# edited\n", written) is False
    assert runtime._loaded_config_change_is_dns_only(None, written) is False
    # The provider moved as well.
    _grow_bastion(runtime)
    written = runtime.write_mihomo_runtime_config()
    assert runtime._loaded_config_change_is_dns_only(loaded_text, written) is False
