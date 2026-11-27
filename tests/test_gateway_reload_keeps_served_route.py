"""A reload while the served Gateway is judged DEAD keeps its route (11-25..27).

Field report 11-25..27 (0.1.7, Windows system proxy, kill switch off, a
Decodo SOCKS5 ISP Gateway dialled through JMS Sentinel nodes on a phone
hotspot): a DNS re-plan reload landed while the Gateway was briefly judged
DEAD.  Its direct probe failed and the chain probe passed slower than the
user's 2700 ms limit (a chain timeout does the same).  The runtime export
carried only ALIVE/SLOW exits, so the reload loaded ``EXIT-SELECT: [REJECT]``:
the graph moved, capture rolled back (a leak in 0.1.7, a block since 11-27),
and the admission that followed failed with "Core-loaded policy has no
admissible FINAL-EXIT startup branch".  Runtime health never reloads the
Core, so nothing loaded the Gateway again once it was ALIVE: the outage
lasted until the next unrelated reload or a restart.

The export now keeps the Gateway the loaded Core serves for the saved
selection whatever its verdict (``DesktopRuntime._served_gateway_identities``).
A Gateway the Core does not serve (a first start, another pick, a changed
node) still needs a usable verdict, and EXIT-SELECT never holds anything but
the selected node or REJECT.

Part 1: the real ProxyTierManager and its Gateway check (a failed direct
probe, then the chain prober), the runtime's real export view
(``_manager_for_mihomo_runtime_config``), the real renderer and the real
render-receipt contract.  Part 2: the real
``_apply_dns_resolver_plan_to_running_core`` on the verified-reload harness of
test_runtime_dns_reload_keeps_route (real renderer, export contracts and
reload gate; fake runner and controller; no Core process, no network).
"""

from __future__ import annotations

import logging
import threading
from unittest.mock import patch

import pytest
import yaml

import manager as manager_module
from desktop_runtime import DesktopRuntime, _Tier3CoreExportManagerView
from gateway_selection import (
    GatewaySelection,
    clear_gateway_selection,
    gateway_identity,
    write_bound_selection,
    write_name_only_selection,
)
from models import NodeStatus
from runtime_egress_recovery import RuntimeEgressReadinessState
from subscription import render_subscription_route
from tests import test_manager  # the module, not its TestCase (pytest would collect it here)
from tests.test_bridge_h5 import exportable
from tests.test_runtime_dns_reload_keeps_route import _loaded_dns, _plan, _settle, _watch
from tests.test_runtime_reload_ready import _loaded, _status

READY = RuntimeEgressReadinessState.READY
SENTINEL_URI = "ss://YWVzLTI1Ni1nY206c2VudGluZWwtcGFzcw@sentinel.example.net:8388#sentinel"
GATEWAY_URI = "socks5://isp-user:isp-pass@isp.example.net:10001#Decodo-ISP"
OTHER_URI = "socks5://isp-user-2:isp-pass-2@isp-2.example.net:10002#Other-ISP"
ROTATED_URI = "socks5://isp-user:isp-pass-rotated@isp.example.net:10001#Decodo-ISP"
USER_LIMIT_MS = 2700  # the 11-25 user's latency limit
SLOW_CHAIN = {"state": "success", "latency_ms": 4900.0, "reason": "ok"}  # 3.6-4.9 s on 11-25
CHAIN_TIMEOUT = {"state": "failed", "latency_ms": None, "reason": "timeout"}
VERDICTS = [
    pytest.param(SLOW_CHAIN, "latency_limit_exceeded", id="chain-pass-over-the-limit"),
    pytest.param(CHAIN_TIMEOUT, "gateway_unreachable", id="chain-timeout"),
]


# --------------------------------------------------------------- part 1 harness

def checked_manager(tmp_path, *uris):
    """A real Manager with one Sentinel node and these configured Gateways,
    every one as its last check left it (ALIVE), the first one picked."""

    manager = test_manager.ManagerTests("test_gateway_direct_pass_needs_no_chain_probe")._minimal_manager(tmp_path)
    manager.config["tiers"]["tier2"]["nodes"] = [{"name": "sentinel", "uri": SENTINEL_URI}]
    manager.config["tiers"]["exit"]["nodes"] = [{"name": uri.rsplit("#", 1)[1], "uri": uri} for uri in uris]
    # The user's limit; also as the Gateway's own ceiling, where a build has one.
    for key in ("primary_gateway_max_latency_ms", "gateway_max_latency_ms"):
        manager.config["testing"][key] = USER_LIMIT_MS
        manager.testing_config[key] = USER_LIMIT_MS
    manager.reload_tier_state("tier2")
    manager.reload_tier_state("exit")
    for tier in ("tier2", "exit"):
        for node in manager.runtime_nodes[tier]:
            node.status, node.latency_ms = NodeStatus.ALIVE, 900
    pick(manager, "Decodo-ISP")
    return manager


def exit_node(manager, name):
    (node,) = [node for node in manager.runtime_nodes["exit"] if node.name == name]
    return node


def pick(manager, name):
    write_bound_selection(manager.config["tiers"]["exit"], name, gateway_identity(exit_node(manager, name)))


def runtime_over(manager):
    """The DesktopRuntime around a real Manager: its export view and contracts."""

    rt = DesktopRuntime.__new__(DesktopRuntime)
    rt.manager = manager
    rt.config = manager.config
    rt.logger = logging.Logger("tests.gateway-reload-keeps-served-route")
    rt.logger.addHandler(logging.NullHandler())
    exportable(rt)
    rt._runtime_route_snapshot_lock = threading.Lock()
    rt._runtime_loaded_export_contract = None
    rt._runtime_pending_export_contract = None
    return rt


def render(rt):
    """What a reload renders now: the runtime's export view, the real renderer."""

    receipt = {}
    _, payload = render_subscription_route(
        rt._manager_for_mihomo_runtime_config(), "/clash/config", {}, render_receipt=receipt)
    return payload, receipt


def load(rt, rendered):
    """Core accepted this render: the contract the runtime commits for it."""

    payload, receipt = rendered
    rt._runtime_loaded_export_contract = rt._runtime_export_contract_for_payload(
        payload, provider_payload="", rendered_proxy_map=rt._runtime_proxy_map_from_render_receipt(receipt))
    return payload


def document(payload):
    return yaml.safe_load(payload)


def groups(payload):
    return {group["name"]: group.get("proxies") for group in document(payload)["proxy-groups"]}


def proxy_names(payload):
    return [proxy["name"] for proxy in document(payload)["proxies"]]


def judge(manager, results):
    """The Gateways' real check: every direct probe fails, the chain probe
    (fresh, no memo) answers ``results[name]``.  Returns the effective nodes."""

    def prober(node, _upstream):
        return results[node.name]

    manager.gateway_chain_prober = prober
    manager._gateway_chain_verdicts = {}
    with patch.object(manager_module, "test_nodes", test_manager.ManagerTests._direct_probe(NodeStatus.DEAD)):
        manager.check_exit()
    return {node.name: node for node in manager.runtime_effective_nodes("exit")}


def assert_fail_closed(payload, gateway):
    exit_select = groups(payload)["EXIT-SELECT"]
    assert set(exit_select) <= {gateway, "REJECT"} and len(exit_select) == 1
    assert groups(payload)["FINAL-EXIT"] == ["EXIT-SELECT", "REJECT"]


# --------------------------------------------------------------- part 1: the export

@pytest.mark.parametrize("chain, reason", VERDICTS)
def test_a_reload_while_the_served_gateway_is_judged_dead_keeps_its_route(tmp_path, chain, reason):
    manager = checked_manager(tmp_path, GATEWAY_URI)
    rt = runtime_over(manager)
    loaded = load(rt, render(rt))
    assert groups(loaded)["EXIT-SELECT"] == ["Decodo-ISP"]

    verdict = judge(manager, {"Decodo-ISP": chain})["Decodo-ISP"]
    assert (verdict.status, verdict.suspicious_reason) == (NodeStatus.DEAD, reason)

    reloaded, _receipt = render(rt)
    assert groups(reloaded)["EXIT-SELECT"] == ["Decodo-ISP"]
    assert_fail_closed(reloaded, "Decodo-ISP")
    gateway = {proxy["name"]: proxy for proxy in document(reloaded)["proxies"]}["Decodo-ISP"]
    assert gateway["dialer-proxy"] == "PROXY-FALLBACK"
    # The same graph as the loaded one: a DNS re-plan of it moves only dns.
    assert document(reloaded) == document(loaded)
    # The verdict itself stands (the node list shows it); the export carries
    # the Gateway anyway, so a start's Gateway check is not due either.
    assert manager.effective_node_usable("exit", exit_node(manager, "Decodo-ISP")) is False
    assert rt._selected_gateway_usable() is True


@pytest.mark.parametrize("chain, reason", VERDICTS)
def test_a_core_that_does_not_serve_the_gateway_still_needs_a_usable_verdict(tmp_path, chain, reason):
    """A first start: nothing is loaded, so the verdict decides, as before."""

    manager = checked_manager(tmp_path, GATEWAY_URI)
    rt = runtime_over(manager)
    judge(manager, {"Decodo-ISP": chain})

    started, _receipt = render(rt)
    assert groups(started)["EXIT-SELECT"] == ["REJECT"]
    assert_fail_closed(started, "Decodo-ISP")
    assert "Decodo-ISP" not in proxy_names(started)
    assert rt._selected_gateway_usable() is False  # the start checks it first (0.1.6)


def test_the_0_1_7_export_left_the_served_gateway_out(tmp_path):
    """The record of the field report: an export of verdicts alone drops the
    Gateway the Core serves, and a reload then loads REJECT."""

    manager = checked_manager(tmp_path, GATEWAY_URI)
    rt = runtime_over(manager)
    load(rt, render(rt))
    judge(manager, {"Decodo-ISP": SLOW_CHAIN})

    verdicts_only = _Tier3CoreExportManagerView(manager).freeze_nodes()
    _, payload = render_subscription_route(verdicts_only, "/clash/config", {})
    assert groups(payload)["EXIT-SELECT"] == ["REJECT"]
    assert groups(render(rt)[0])["EXIT-SELECT"] == ["Decodo-ISP"]


def test_the_served_gateway_comes_back_on_the_route_it_never_left(tmp_path):
    manager = checked_manager(tmp_path, GATEWAY_URI)
    rt = runtime_over(manager)
    loaded = load(rt, render(rt))
    judge(manager, {"Decodo-ISP": CHAIN_TIMEOUT})
    kept = load(rt, render(rt))  # the reload during the DEAD verdict

    judge(manager, {"Decodo-ISP": {"state": "success", "latency_ms": 1900.0, "reason": "ok"}})
    assert manager.effective_node_usable("exit", exit_node(manager, "Decodo-ISP")) is True

    recovered, _receipt = render(rt)
    assert document(recovered) == document(kept) == document(loaded)


def test_only_the_member_served_for_the_current_selection_is_kept(tmp_path):
    manager = checked_manager(tmp_path, GATEWAY_URI, OTHER_URI)
    rt = runtime_over(manager)
    load(rt, render(rt))
    judge(manager, {"Decodo-ISP": CHAIN_TIMEOUT, "Other-ISP": {"state": "success", "latency_ms": 800.0}})
    kept = load(rt, render(rt))
    assert groups(kept)["EXIT-SELECT"] == ["Decodo-ISP"]

    pick(manager, "Other-ISP")  # another Gateway: its own verdict decides
    picked, _receipt = render(rt)
    assert groups(picked)["EXIT-SELECT"] == ["Other-ISP"]
    assert "Decodo-ISP" not in proxy_names(picked)

    clear_gateway_selection(manager.config["tiers"]["exit"])  # no Gateway at all
    cleared, _receipt = render(rt)
    assert groups(cleared)["FINAL-EXIT"] == ["PROXY-FALLBACK", "REJECT"]
    assert "Decodo-ISP" not in proxy_names(cleared)


def test_a_served_gateway_whose_node_changed_fails_closed(tmp_path):
    """Rotated credentials are another node: never the old one, never by name."""

    manager = checked_manager(tmp_path, GATEWAY_URI)
    rt = runtime_over(manager)
    load(rt, render(rt))
    manager.config["tiers"]["exit"]["nodes"] = [{"name": "Decodo-ISP", "uri": ROTATED_URI}]
    manager.reload_tier_state("exit")
    for node in manager.runtime_nodes["exit"]:
        node.status, node.latency_ms = NodeStatus.ALIVE, 900

    reloaded, _receipt = render(rt)
    assert groups(reloaded)["EXIT-SELECT"] == ["REJECT"]
    assert_fail_closed(reloaded, "Decodo-ISP")


def test_an_old_name_only_selection_keeps_its_served_member_too(tmp_path):
    manager = checked_manager(tmp_path, GATEWAY_URI)
    write_name_only_selection(manager.config["tiers"]["exit"], "Decodo-ISP")
    rt = runtime_over(manager)
    loaded = load(rt, render(rt))
    assert groups(loaded)["EXIT-SELECT"] == ["Decodo-ISP"]
    judge(manager, {"Decodo-ISP": SLOW_CHAIN})

    assert document(render(rt)[0]) == document(loaded)
    pick(manager, "Decodo-ISP")  # the runtime binds it to the node it serves
    assert document(render(rt)[0]) == document(loaded)


# --------------------------------------------------------------- part 2: the 11-25 reload

def serve_gateway(runtime):
    """The harness's READY runtime, now serving its Gateway: the saved
    selection names it, the Core loaded EXIT-SELECT [gw] and READY stands."""

    manager = runtime.manager
    (gateway,) = manager.runtime_nodes["exit"]
    subscription_nodes = manager.get_nodes_for_subscription

    def export(tier_id, limit=None):
        # As ProxyTierManager: exits of every status (the runtime view filters).
        if tier_id == "exit":
            return list(manager.runtime_nodes["exit"])
        return subscription_nodes(tier_id, limit)

    manager.get_nodes_for_subscription = export
    selection = GatewaySelection("bound", gateway.name, identity=gateway_identity(gateway))
    manager.active_exit_selection = lambda: selection
    manager.active_exit_proxy_name = lambda: selection.label
    manager._exit_active = lambda: True
    runtime.write_mihomo_runtime_config()
    runtime._mark_core_export_sync_success()
    _settle()
    assert runtime._refresh_runtime_route_snapshot(timeout_seconds=1.0) is not None
    runtime._publish_runtime_egress_readiness(READY, validation_active=False, reason="verified")
    assert loaded_groups(runtime)["EXIT-SELECT"] == [gateway.name]
    return gateway


def loaded_groups(runtime):
    path = runtime._runtime_config_path_for_client()
    return groups(path.read_text(encoding="utf-8"))


@pytest.fixture
def served(tmp_path):
    runtime, _runner = _loaded(tmp_path)
    try:
        yield runtime, serve_gateway(runtime)
    finally:
        runtime.stop_event.set()


def judged_dead(gateway):
    gateway.status, gateway.latency_ms, gateway.suspicious_reason = NodeStatus.DEAD, None, "latency_limit_exceeded"


def test_the_dns_replan_during_a_dead_verdict_keeps_ready_capture_and_the_gateway(served):
    runtime, gateway = served
    before = runtime._runtime_egress_readiness
    assert before.state is READY
    judged_dead(gateway)
    plan = _plan(runtime)
    seen = _watch(runtime)

    assert runtime._apply_dns_resolver_plan_to_running_core(plan) is True
    _settle()

    assert seen["hot_reloads"] == 1 and _loaded_dns(runtime) == dict(plan.block)
    assert loaded_groups(runtime)["EXIT-SELECT"] == [gateway.name]
    assert runtime._served_gateway_identities() == {gateway_identity(gateway)}
    # No route change: READY, capture and the Windows proxy stay where they were.
    assert seen["mutations"] == [] and seen["rollbacks"] == [] and seen["rechecks"] == 0
    assert runtime._runtime_egress_readiness is before
    assert runtime._current_core_runtime_phase() == "formal_capture"
    assert runtime._revalidation_capture_active("formal_capture") is True
    assert _status(runtime) == (False, "running")


def test_the_same_reload_without_the_served_route_loaded_reject(served):
    """The 0.1.7 record: the export left the DEAD Gateway out, so the reload
    moved the graph to EXIT-SELECT [REJECT] and rolled capture back."""

    runtime, gateway = served
    runtime._served_gateway_identities = lambda: frozenset()
    judged_dead(gateway)
    plan = _plan(runtime)
    seen = _watch(runtime)

    assert runtime._apply_dns_resolver_plan_to_running_core(plan) is True
    _settle()

    assert loaded_groups(runtime)["EXIT-SELECT"] == ["REJECT"]
    assert seen["mutations"] and seen["rollbacks"]
    assert runtime._runtime_egress_readiness.state is not READY
