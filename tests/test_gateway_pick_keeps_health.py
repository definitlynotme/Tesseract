"""0.1.6: a Gateway pick works, running or stopped (field report 11-22, 0.1.5).

A SOCKS5 ISP Gateway, chained through Sentinel, passed 0.1.5's new Gateway
check, and still every pick failed:

* running: the pick saved the selection through a reload of the exit tier.
  That re-parsed the configured Gateway as UNKNOWN, the export (ALIVE/SLOW
  only) left it out, the strict reload loaded ``EXIT-SELECT: [REJECT]`` and
  its FINAL-EXIT 204 failed at once ("Bastion Layer verification failed
  after hot reload"). The pick tests stubbed reload_config, so none of them
  ran that reload;
* stopped: ``bridge_exit_proxy_name`` answered unknown for every id while
  nodes.list listed the Gateway with one, so the page said
  找不到这个节点，节点列表可能已经更新 on every try;
* the reload's 204 had 1.5 s for the whole chain (1.9-4.9 s on 11-22).

Real ProxyTierManager, the runtime's real export view and the real renderer
where the bug lived; recorders only around Mihomo and the settings files.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from bridge_node_commands import NodeCommands, RuntimeNodeCommandAdapter
from desktop_runtime import (
    FINAL_EXIT_PROBE_BUDGET_SECONDS,
    DesktopRuntime,
    _Tier3CoreExportManagerView,
)
from gateway_selection import gateway_identity, read_selection, write_bound_selection
from models import NodeStatus
from subscription import render_subscription_route
from tests import test_manager  # the module, not its TestCase (pytest would collect it here)
from tests.test_bridge_h5 import exportable, gw, ids
from tests.test_bridge_p2_04 import runtime  # noqa: F401  (pytest fixture)
from tests.test_gateway_identity_runtime import picking, twins

SENTINEL_URI = "ss://YWVzLTI1Ni1nY206c2VudGluZWwtcGFzcw@sentinel.example.net:8388#sentinel"
GATEWAY_URI = "socks5://isp-user:isp-pass@isp.example.net:10001#Decodo-ISP"


# --------------------------------------------------------------- harness

def checked_manager(tmp_path: Path):
    """A real Manager with one Sentinel node and one configured Gateway, both
    as their checks left them (ALIVE), nothing picked yet."""
    manager = test_manager.ManagerTests(
        "test_run_cycle_checks_unselected_gateway_candidate_so_it_can_be_selected"
    )._minimal_manager(tmp_path)
    manager.config["tiers"]["tier2"]["nodes"] = [{"name": "sentinel", "uri": SENTINEL_URI}]
    manager.config["tiers"]["exit"]["nodes"] = [{"name": "Decodo-ISP", "uri": GATEWAY_URI}]
    manager.reload_tier_state("tier2")
    manager.reload_tier_state("exit")
    for tier in ("tier2", "exit"):
        for node in manager.runtime_nodes[tier]:
            node.status, node.latency_ms = NodeStatus.ALIVE, 900
    return manager


def runtime_over(manager):
    """The DesktopRuntime methods under test around a real Manager."""
    rt = DesktopRuntime.__new__(DesktopRuntime)
    rt.manager = manager
    rt.kill_switch = None
    rt.config = manager.config
    rt.logger = logging.Logger("tests.gateway-pick-keeps-health")
    rt.logger.addHandler(logging.NullHandler())
    return rt


def loaded_groups(manager):
    """What a strict reload would load now: the runtime's export view, rendered."""
    view = _Tier3CoreExportManagerView(manager).freeze_nodes()
    _, payload = render_subscription_route(view, "/clash/config", {})
    return {group["name"]: group.get("proxies") for group in yaml.safe_load(payload)["proxy-groups"]}


def pick(manager, rt, *, keep_tier_state):
    """The pick's settings write and the reload _set_exit_selection_locked runs after it."""
    gateway = manager.runtime_nodes["exit"][0]
    write_bound_selection(manager.config["tiers"]["exit"], "Decodo-ISP", gateway_identity(gateway))
    rt._reconfigure_manager_after_config_reload(target_tier="exit", keep_tier_state=keep_tier_state)
    return gateway


# --------------------------------------------------------------- running pick

def test_a_running_pick_keeps_the_checked_gateway_and_the_reload_loads_it(tmp_path):
    manager = checked_manager(tmp_path)
    rt = runtime_over(manager)
    assert loaded_groups(manager)["FINAL-EXIT"] == ["PROXY-FALLBACK", "REJECT"]

    pick(manager, rt, keep_tier_state=True)

    assert [node.status for node in manager.runtime_effective_nodes("exit")] == [NodeStatus.ALIVE]
    assert manager._exit_active()
    groups = loaded_groups(manager)
    assert groups["FINAL-EXIT"] == ["EXIT-SELECT", "REJECT"]
    assert groups["EXIT-SELECT"] == ["Decodo-ISP"]


def test_the_old_exit_tier_reload_is_what_loaded_reject(tmp_path):
    """The 0.1.5 path, kept as the record of why the pick keeps the tier."""
    manager = checked_manager(tmp_path)
    rt = runtime_over(manager)

    pick(manager, rt, keep_tier_state=False)

    assert [node.status for node in manager.runtime_effective_nodes("exit")] == [NodeStatus.UNKNOWN]
    assert loaded_groups(manager)["EXIT-SELECT"] == ["REJECT"]


def test_the_pick_saves_through_a_reload_that_keeps_the_tier(runtime):
    rt, gw1, _gw2 = twins(runtime)
    log = picking(rt)
    answer = rt.set_exit_selection(proxy_name="JP 01", identity=gateway_identity(gw1))
    assert answer["applied"] is True
    assert log["config_reloads"] == [("exit", True)]


# --------------------------------------------------------------- stopped pick

def stopped(rt, inventory):
    """No Manager and no Core: nodes.list and the getter read this inventory;
    settings writes land in an in-memory config.local."""
    saved = {"tiers": {"exit": {}}}
    rt.manager = None
    rt.core_runner = SimpleNamespace(is_running=lambda: False)
    rt.bridge_stopped_node_inventory = lambda: {tier: [node.clone() for node in nodes]
                                               for tier, nodes in inventory.items()}
    rt.logger = logging.Logger("tests.gateway-pick-stopped")
    rt.logger.addHandler(logging.NullHandler())
    rt._runtime_selector_intent_revision = 1
    rt._declare_runtime_selector_intent = lambda reason, *, group=None: rt._runtime_selector_intent_revision
    rt._core_transition_lock = threading.RLock()

    def set_exit_selection(label, *, identity=None, unresolved=None):
        write_bound_selection(saved["tiers"]["exit"], label, identity)
        return {}

    rt.config_store.set_exit_selection = set_exit_selection
    rt.read_local_config = lambda: saved
    rt.reload_config = lambda **_kwargs: {}
    return saved


def test_a_stopped_pick_of_a_listed_gateway_is_saved_for_the_next_start(runtime):
    rt = exportable(runtime)
    gateway = gw("Decodo-ISP", protocol="socks5", address="isp.example.invalid", port=10001,
                 password="IspPw-1", extra={"username": "isp-user"},
                 status=NodeStatus.UNKNOWN, latency_ms=None)
    saved = stopped(rt, {"tier1": [], "tier2": [], "tier3": [], "exit": [gateway]})
    listed = ids(rt)[("gateway", "Decodo-ISP")]  # the id the stopped page shows

    facts = rt.bridge_exit_proxy_name(listed)
    assert facts["status"] == "ok" and facts["proxy_name"] == "Decodo-ISP"
    assert facts["identity"] == gateway_identity(gateway)

    result = NodeCommands(RuntimeNodeCommandAdapter(rt)).gateway_select({"node_id": listed}, None)
    assert result["applied"] is True and result["apply_error"] is None
    assert result["selected"] == result["selection"] == "Decodo-ISP"
    assert read_selection(saved["tiers"]["exit"]).key == "id:" + gateway_identity(gateway)


def test_a_stopped_pick_still_refuses_what_is_not_a_listed_gateway(runtime):
    rt = exportable(runtime)
    edge = gw("Edge", tier="tier2", address="edge.example.invalid")
    spaced = gw(" Spaced ", address="spaced.example.invalid")
    stopped(rt, {"tier1": [], "tier2": [edge], "tier3": [], "exit": [spaced]})
    listed = ids(rt)
    assert rt.bridge_exit_proxy_name(listed[("sentinel", "Edge")])["status"] == "not_gateway"
    spaced_id = next(row_id for (tier, name), row_id in listed.items()
                     if tier == "gateway" and name.strip() == "Spaced")
    assert rt.bridge_exit_proxy_name(spaced_id)["status"] == "unusable"
    assert rt.bridge_exit_proxy_name("0" * 24)["status"] == "unknown"


# --------------------------------------------------------------- the 204 budget

def test_final_exit_probe_budget_waits_for_a_gateway_chain():
    rt = DesktopRuntime.__new__(DesktopRuntime)
    rt._active_gateway_policy_enabled = lambda: False
    assert rt._final_exit_probe_budget_seconds(8.0) == FINAL_EXIT_PROBE_BUDGET_SECONDS == 1.5
    rt._active_gateway_policy_enabled = lambda: True
    assert rt._final_exit_probe_budget_seconds(8.0) == 8.0
    assert rt._final_exit_probe_budget_seconds(1.0) == 1.5


def test_the_reload_verification_probe_runs_under_that_budget():
    rt = DesktopRuntime.__new__(DesktopRuntime)
    rt._startup_probe_timeout_seconds = lambda: 8.0
    seen = {}

    def broker(callback, **kwargs):
        seen.update(kwargs, at=time.monotonic())
        return {"status": 204}

    rt._run_brokered_runtime_probe = broker
    for gateway, budget in ((False, 1.5), (True, 8.0)):
        rt._active_gateway_policy_enabled = lambda gateway=gateway: gateway
        rt._probe_mihomo_mixed_port(require_route_readback=False)
        assert seen["execution_deadline_monotonic"] - seen["at"] == pytest.approx(budget, abs=0.25)


# --------------------------------------------------------------- the start

def selected_manager(tmp_path, status):
    manager = checked_manager(tmp_path)
    gateway = manager.runtime_nodes["exit"][0]
    write_bound_selection(manager.config["tiers"]["exit"], "Decodo-ISP", gateway_identity(gateway))
    gateway.status = status
    return manager


@pytest.mark.parametrize("status, checks", [(NodeStatus.UNKNOWN, 1), (NodeStatus.ALIVE, 0)])
def test_a_start_checks_a_selected_gateway_the_export_would_leave_out(tmp_path, status, checks):
    manager = selected_manager(tmp_path, status)
    rt = runtime_over(manager)
    calls = []

    def check_exit(*, force_remote_refresh=False):
        calls.append(force_remote_refresh)
        for node in manager.runtime_nodes["exit"]:
            node.status = NodeStatus.ALIVE
        return list(manager.runtime_nodes["exit"])

    manager.check_exit = check_exit
    rt._check_selected_gateway_before_core_start()
    assert calls == [False] * checks
    assert loaded_groups(manager)["EXIT-SELECT"] == ["Decodo-ISP"]


def test_no_gateway_check_at_start_without_a_pick(tmp_path):
    manager = checked_manager(tmp_path)
    for node in manager.runtime_nodes["exit"]:
        node.status = NodeStatus.UNKNOWN
    rt = runtime_over(manager)
    manager.check_exit = lambda **_kwargs: pytest.fail("no Gateway is selected")
    rt._check_selected_gateway_before_core_start()


def test_the_start_path_runs_the_gateway_check_after_the_fallback_layers(tmp_path):
    manager = selected_manager(tmp_path, NodeStatus.UNKNOWN)
    rt = runtime_over(manager)
    order = []
    rt._set_tier3_startup_gate_required = lambda _required: None
    rt._clear_tier3_terminal_startup_failure = lambda: None
    rt._primary_layers_ready_for_start = lambda: order.append("fallback layers") or True
    rt._check_selected_gateway_before_core_start = lambda: order.append("gateway")
    rt._recover_tier3_pool_before_core_start()
    assert order == ["fallback layers", "gateway"]


# --------------------------------------------------------------- the reason

def test_a_non_bastion_reload_failure_names_final_exit_and_what_the_204_met():
    rt = DesktopRuntime.__new__(DesktopRuntime)
    rt._tier3_is_primary_export = lambda: False
    rt._repair_tier3_export_order_from_core_delays = lambda: False
    failure = RuntimeError("Mihomo reload FINAL-EXIT 204 verification failed: "
                           "www.gstatic.com: TimeoutError; cp.cloudflare.com: TimeoutError")
    result = rt._repair_and_retry_tier3_reload_after_verification_failure(
        None, {}, verification_error=failure)
    assert result["reloaded"] is False
    assert result["reason"] == ("FINAL-EXIT verification failed after hot reload "
                                "(www.gstatic.com: TimeoutError; cp.cloudflare.com: TimeoutError)")
    rt._tier3_is_primary_export = lambda: True
    assert rt._repair_and_retry_tier3_reload_after_verification_failure(
        None, {}, verification_error=failure)["reason"] == "Bastion Layer verification failed after hot reload"
