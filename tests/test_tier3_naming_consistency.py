"""Tier3 naming stays one truth for the renderer, the Core and its readers (11-02).

No Core, no network.  The real renderer and the real DesktopRuntime naming
paths only:

* the names the repair plan and the qualification probe are the names the
  real writer puts into the Core (stable and positional, with cross-tier and
  reserved-name collisions and the export limit);
* the switch is latched per runtime start, so a reload_config swap cannot
  split runtime naming from the loaded Core's naming;
* a Gateway selection saved by name under the other Tier3 naming still names
  its Gateway node when one node can carry the name (else it fails closed),
  in the renderer and in every reader of the loaded route, and the binding
  tick saves it once as that node's identity (T-GW-ID);
* a stable name never carries a literal ``%`` (Mihomo decodes a controller
  path segment twice).
"""

from __future__ import annotations

from dataclasses import replace
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from clash_formatter import (
    _stable_proxy_name,
    build_clash_proxy_list,
    build_full_clash_config,
    rendered_exit_pairs,
    resolve_gateway_member,
)
from desktop_runtime import DesktopRuntime
from gateway_selection import (
    GatewaySelection,
    clear_gateway_selection,
    gateway_identity,
    read_selection,
)
from models import Node, NodeStatus
from runtime_egress_recovery import CoreLoadedGroupState
from runtime_state import RuntimeStateStore
from subscription import render_subscription_route
from system_proxy import ProxySettings, SystemProxyManager
from tests.test_bridge_h5 import gw, node_id, other
from tests.test_bridge_h8 import exit_select, prepared, publish_route, resolved, strict_reload
from tests.test_bridge_p2_04 import runtime  # noqa: F401  (pytest fixture)
from tests.test_desktop_runtime_recovery import FakeTier3Manager, MemoryProxyBackend


def http(name, address, *, tier="tier3"):
    return Node(name=name, protocol="http", address=address, port=8080, tier=tier,
                status=NodeStatus.ALIVE)


class _RenderManager(FakeTier3Manager):
    """FakeTier3Manager plus what the real renderer and a runtime start read."""

    def __init__(self, config=None, **_kwargs):
        super().__init__()
        self.config = config if config is not None else {}
        self.client_config = self.config.setdefault("client", {})
        self.testing_config = self.config.get("testing", {})
        self.logger = logging.getLogger("tests.tier3-naming-consistency")
        self.runtime_nodes["exit"] = []
        self.on_tier3_pool_committed = None
        self.tier3_close_stage_observer = None
        self.tier3_runtime_truth_provider = None

    def active_exit_proxy_name(self):
        return ""

    def bind_runtime_generation(self, _generation, provider):
        self.tier3_runtime_truth_provider = provider

    def close(self, *, timeout_seconds: float = 3.0) -> str:
        return "STOPPED"


def _runtime(tmp_path: Path, *, verified: bool, tier3_limit: int = 20) -> DesktopRuntime:
    config = {
        "client": {
            "mihomo_runtime_config": str(tmp_path / "config" / "mihomo-runtime.yaml"),
            "mihomo_work_dir": str(tmp_path / "core-home"),
            "runtime_state_path": str(tmp_path / "runtime-state.json"),
            "system_proxy": {"enabled": True, "host": "127.0.0.1", "port": 7890},
        },
        "alerts": {"log_file": str(tmp_path / "monitor.log"), "desktop": {"enabled": False}},
        "subscription_server": {"tier3_limit": tier3_limit},
        "testing": {"mihomo_helper": {"startup_core_probe_limit": tier3_limit}},
        "tiers": {
            "tier1": {"nodes": []},
            "tier2": {"nodes": [], "subscription_urls": []},
            "tier3": {"enabled": True, "verified_provider_writes": verified},
        },
    }
    runtime = DesktopRuntime(config, project_root=tmp_path)
    for handler in list(runtime.logger.handlers):
        handler.close()
        runtime.logger.removeHandler(handler)
    runtime.logger = logging.getLogger("tests.tier3-naming-consistency.runtime")
    runtime._runtime_config_path_for_client().parent.mkdir(parents=True, exist_ok=True)
    runtime._tier3_core_provider_file_path().parent.mkdir(parents=True, exist_ok=True)
    return runtime


def _written_tier3_members(runtime):
    runtime.write_mihomo_runtime_config()
    provider = yaml.safe_load(runtime._tier3_core_provider_file_path().read_text(encoding="utf-8"))
    return [str(proxy["name"]) for proxy in provider["proxies"] if proxy["name"] != "REJECT"]


# ------------------------------------------- probe names = loaded names (F19)

@pytest.mark.parametrize("verified", [True, False], ids=["stable", "positional"])
def test_probe_names_are_the_names_the_real_writer_loads(tmp_path, verified):
    runtime = _runtime(tmp_path, verified=verified, tier3_limit=3)
    manager = _RenderManager(runtime.config)
    manager.runtime_nodes["tier2"] = [http("alpha", "t2.invalid", tier="tier2")]
    manager.runtime_nodes["tier3"] = [http("alpha", "a.invalid"), http("DIRECT", "d.invalid"),
                                      http("beta", "b.invalid"), http("beta", "e.invalid")]
    runtime.manager = manager
    loaded = _written_tier3_members(runtime)
    pairs = runtime._tier3_proxy_name_pairs()

    assert sorted(name for name, _node in pairs) == sorted(loaded)
    assert [node.name for _name, node in pairs] == ["alpha", "DIRECT", "beta"]  # export limit 3
    if verified:
        assert all("·" in name for name in loaded), loaded
    else:
        # The Sentinel "alpha" and the reserved DIRECT came first in the
        # renderer's seen set (an empty seed probed "alpha" and "DIRECT").
        assert loaded == ["alpha-2", "DIRECT-2", "beta"]


# ------------------------------------------------- per-start latch (F24)

class _KillSwitch:
    def __init__(self, **_kwargs):
        pass

    def cleanup_stale_rules(self):
        pass

    def close(self, *, timeout: float = 1.0):
        pass


def test_the_switch_is_latched_per_runtime_start(tmp_path):
    runtime = _runtime(tmp_path, verified=True)
    runtime.system_proxy = SystemProxyManager(
        runtime.config["client"]["system_proxy"],
        backend=MemoryProxyBackend(ProxySettings(enabled=False, server="", override="")),
        platform_name="windows",
    )
    runtime.runtime_state = RuntimeStateStore(tmp_path / "runtime-state.json")
    runtime.config["killswitch"] = {"enabled": False}
    runtime.recover_stale_system_proxy = lambda: False
    runtime.restore_system_proxy = lambda: False
    runtime._warn_if_killswitch_unavailable = lambda: None
    runtime._start_subscription_server = lambda: None
    runtime._monitor_loop = lambda: runtime.stop_event.wait()
    runtime.status = lambda *a, **k: {"running": bool(runtime.running),
                                       "runtime_generation": int(runtime._runtime_generation)}
    nodes = [http("alpha", "a.invalid"), http("beta", "b.invalid")]

    # Not started yet (and every DesktopRuntime.__new__ stub): the config, live.
    assert runtime._tier3_verified_provider_writes_enabled() is True
    with patch("desktop_runtime.ProxyTierManager", _RenderManager), \
            patch("desktop_runtime.KillSwitchManager", _KillSwitch):
        runtime.start(start_core=False)
        try:
            runtime.manager.runtime_nodes["tier3"] = list(nodes)
            loaded = _written_tier3_members(runtime)
            # reload_config swaps the config; the loaded Core keeps its names.
            runtime.config["tiers"]["tier3"]["verified_provider_writes"] = False
            assert runtime._tier3_verified_provider_writes_enabled() is True
            assert runtime._manager_for_mihomo_runtime_config().stable_tier3_names is True
            assert sorted(name for name, _node in runtime._tier3_proxy_name_pairs()) == sorted(loaded)
            assert all("·" in name for name in loaded)
        finally:
            runtime.stop(actor="test", reason="naming latch")
        runtime.start(start_core=False)
        try:
            runtime.manager.runtime_nodes["tier3"] = list(nodes)
            assert runtime._tier3_verified_provider_writes_enabled() is False
            assert [name for name, _node in runtime._tier3_proxy_name_pairs()] == ["alpha", "beta"]
        finally:
            runtime.stop(actor="test", reason="naming latch")


# ---------------------------------- a saved Gateway across namings (F14/F22)

def _rendered_exit(tier3, exits, saved, *, stable):
    """The real renderer: EXIT-SELECT, FINAL-EXIT, the receipt's Gateway
    decision and the proxies by name.  ``saved`` is a proxy name (the
    ``active_exit_proxy`` keyword: an old name-only selection) or a
    GatewaySelection."""
    receipt = {}
    selection = ({"active_exit_selection": saved} if isinstance(saved, GatewaySelection)
                 else {"active_exit_proxy": saved})
    document = yaml.safe_load(build_full_clash_config(
        {"tier1": [], "tier2": [], "tier3": tier3, "exit": exits},
        stable_tier3_names=stable, render_receipt=receipt, **selection))
    groups = {group["name"]: group["proxies"] for group in document["proxy-groups"]}
    proxies = {proxy["name"]: proxy for proxy in document["proxies"]}
    return groups["EXIT-SELECT"], groups["FINAL-EXIT"], receipt.get("exit_selection"), proxies


def _exit_groups(tier3, exits, saved, *, stable):
    members, final, _decision, _proxies = _rendered_exit(tier3, exits, saved, stable=stable)
    return members, final


def _bound(node, label):
    """A selection saved by node identity (label: its proxy name at pick time)."""
    return GatewaySelection("bound", label, identity=gateway_identity(node))


def test_a_selection_saved_under_the_other_tier3_naming_keeps_its_gateway():
    tier3 = [http("Free", "c.invalid")]
    gateway = http("Free", "gw.invalid", tier="exit")
    # Positional era: the Tier3 "Free" pushed the Gateway to "Free-2".
    assert _exit_groups(tier3, [gateway], "Free-2", stable=True) == (
        ["Free"], ["EXIT-SELECT", "REJECT"])
    # Stable era back to positional names (the switch turned off).
    assert _exit_groups(tier3, [gateway], "Free", stable=False) == (
        ["Free-2"], ["EXIT-SELECT", "REJECT"])


def test_a_rendered_gateway_name_is_kept_and_anything_else_fails_closed():
    """An old name-only selection is served only when one Gateway node can
    carry the name; anything else fails closed.

    T-GW-ID flips the CN-2 pin by design: "Free-2" (the second twin under
    stable names, the first under positional names) used to be kept as the
    rendered second twin.  The name never proved which twin was meant, so it
    is now [REJECT]; the saved identity is what serves the meant twin.
    """
    tier3 = [http("Free", "c.invalid")]
    first = http("Free", "gw1.invalid", tier="exit")
    second = http("Free", "gw2.invalid", tier="exit")
    members, final, decision, _proxies = _rendered_exit(
        tier3, [first, second], "Free-2", stable=True)
    assert (members, final) == (["REJECT"], ["EXIT-SELECT", "REJECT"])
    assert decision["resolution"] == "legacy_ambiguous"
    # The name-based limit this test pinned before is gone for a saved
    # identity: each twin is served under the name this render gives it.
    for node, name in ((first, "Free"), (second, "Free-2")):
        members, final, decision, proxies = _rendered_exit(
            tier3, [first, second], _bound(node, "Free-2"), stable=True)
        assert (members, final) == ([name], ["EXIT-SELECT", "REJECT"])
        assert proxies[name]["server"] == node.address
        assert decision["resolution"] == "identity"
    # Two Gateways share the base: their suffixes follow the export order, so
    # a name from the other naming never proved which one was meant.
    assert _exit_groups(tier3, [first, second], "Free-3", stable=True)[0] == ["REJECT"]
    assert _exit_groups(tier3, [first, second], "Nowhere", stable=True)[0] == ["REJECT"]
    # A Bastion member's own name never selects a Gateway.
    tier3_name = str(build_clash_proxy_list(tier3, stable_names=True)[0]["name"])
    assert _exit_groups(tier3, [first], tier3_name, stable=True)[0] == ["REJECT"]
    assert _exit_groups(tier3, [first], "Free", stable=False)[0] == ["Free-2"]


def _resolve(nodes, label, *, stable):
    """resolve_gateway_member for an old name-only selection over this
    render's exported exit pairs (as the runtime's pick path calls it); the
    full renderer's EXIT-SELECT must agree.  Returns (member, resolution)."""
    pairs = rendered_exit_pairs(nodes, stable_tier3_names=stable)
    member, decision = resolve_gateway_member(
        GatewaySelection("legacy", label), pairs, nodes,
        inventory=nodes["exit"], stable_tier3_names=stable)
    assert _exit_groups(nodes["tier3"], nodes["exit"], label, stable=stable)[0] == [
        member or "REJECT"]
    return member, decision["resolution"]


def test_resolution_pairs_rendered_exit_nodes_across_a_skipped_one():
    """resolve_exit_selection is gone: the same cases through
    resolve_gateway_member and the renderer, with the same outcomes."""
    # A VLESS node without a UUID takes a name and renders nothing.
    skipped = Node(name="Other", protocol="vless", address="x.invalid", port=1, tier="exit",
                   status=NodeStatus.ALIVE)
    target = http("Free", "gw.invalid", tier="exit")
    nodes = {"tier1": [], "tier2": [], "tier3": [http("Free", "c.invalid")],
             "exit": [skipped, target]}
    assert _resolve(nodes, "Free-2", stable=True) == ("Free", "legacy_cross_naming")
    assert _resolve(nodes, "Free", stable=False) == ("Free-2", "legacy_cross_naming")
    assert _resolve(nodes, "Free-3", stable=True) == ("", "legacy_unconfirmed")
    # A skipped node with the same base still moves the target's suffix, and
    # the old name may have meant it: the name fails closed.
    twin = Node(name="Free", protocol="vless", address="y.invalid", port=1, tier="exit",
                status=NodeStatus.ALIVE)
    nodes["exit"] = [twin, target]
    assert _resolve(nodes, "Free-3", stable=True) == ("", "legacy_ambiguous")
    assert _resolve(nodes, "Free-2", stable=False) == ("", "legacy_ambiguous")
    # The saved identity pairs the target with its rendered name across the
    # skipped twin, under either naming.
    for stable, name in ((True, "Free-2"), (False, "Free-3")):
        members, _final, _decision, proxies = _rendered_exit(
            nodes["tier3"], nodes["exit"], _bound(target, "Free-2"), stable=stable)
        assert members == [name] and proxies[name]["server"] == "gw.invalid"


def test_switch_off_resolution_never_moves_a_selection_to_another_gateway():
    # Picked while it was the only Gateway "GW"; later a Bastion member "GW"
    # and another Gateway "GW" (listed first) appear.  Before 11-02 this
    # failed closed; resolving "GW" by position would pick the other Gateway.
    tier3 = [http("GW", "c.invalid")]
    other_gateway = http("GW", "gw0.invalid", tier="exit")
    picked = http("GW", "gw1.invalid", tier="exit")
    assert _exit_groups(tier3, [other_gateway, picked], "GW", stable=False)[0] == ["REJECT"]
    # One Gateway with that base: resolved as before.
    assert _exit_groups(tier3, [picked], "GW", stable=False)[0] == ["GW-2"]


def test_loaded_route_readers_resolve_a_selection_saved_under_the_other_naming(runtime):
    """_loaded_gateway_member (replacing _loaded_gateway_proxy_name) and the
    readers built on it name the loaded member for an old name-only
    selection; a name the loaded route does not serve, an unresolved or an
    invalid selection names nothing ("", never the label)."""
    rt = prepared(runtime, tier3_limit=3)
    target = gw("Free", address="free-gw.example.invalid")
    rt.manager.runtime_nodes["tier3"] = [other("Free", "tier3", address="c.invalid")]
    rt.manager.runtime_nodes["exit"] = [target]
    rt.manager._exit_active = lambda: True
    strict_reload(rt, "Free-2")                         # saved before 11-02
    assert exit_select(rt) == ("Free",)
    assert rt._loaded_gateway_member(GatewaySelection("legacy", "Free-2")) == "Free"
    assert rt._loaded_gateway_member(GatewaySelection("legacy", "Elsewhere")) == ""
    assert rt._loaded_gateway_member(
        GatewaySelection("unresolved", "Free-2", reason="ambiguous")) == ""
    assert rt._loaded_gateway_member(
        GatewaySelection("invalid", "Free-2", reason="malformed")) == ""
    # A saved identity names its loaded member whatever the naming.
    assert rt._loaded_gateway_member(_bound(target, "Free-2")) == "Free"
    assert resolved(rt) == (node_id(rt, "exit", target), None)
    status = rt.bridge_gateway_status()
    assert (status["stale"], status["missing"], status["applied"]) == (False, False, True)
    assert status["selected"] == "Free"                 # the loaded member, not the label
    intent = rt._startup_route_intent_from_snapshot(rt._runtime_route_snapshot)
    assert intent is not None and intent.expected_gateway_key is not None

    rt.config["tiers"]["tier3"]["verified_provider_writes"] = False
    strict_reload(rt, "Free")                           # saved while names were stable
    assert exit_select(rt) == ("Free-2",)
    assert rt._loaded_gateway_member(GatewaySelection("legacy", "Free")) == "Free-2"
    assert rt._loaded_gateway_member(_bound(target, "Free")) == "Free-2"
    assert resolved(rt) == (node_id(rt, "exit", target), None)


def test_initial_activation_plans_bind_the_resolved_gateway(runtime):
    rt = prepared(runtime, tier3_limit=3)
    rt.manager.runtime_nodes["tier3"] = [other("Free", "tier3", address="c.invalid")]
    rt.manager.runtime_nodes["exit"] = [gw("Free", address="free-gw.example.invalid")]
    rt.manager._exit_active = lambda: True
    document = strict_reload(rt, "Free-2")
    leaf = next(group for group in document["proxy-groups"]
                if group["name"] == "TIER3-AUTO")["proxies"][0]
    rt._runtime_route_snapshot = replace(rt._runtime_route_snapshot, group_states=(
        CoreLoadedGroupState("FINAL-EXIT", "Selector", ("EXIT-SELECT", "REJECT"), "EXIT-SELECT"),
        CoreLoadedGroupState("EXIT-SELECT", "Selector", ("Free",), "Free"),
        CoreLoadedGroupState("PROXY-FALLBACK", "Fallback", ("TIER3-AUTO",), "TIER3-AUTO"),
        CoreLoadedGroupState("TIER3-AUTO", "Fallback", (leaf, "REJECT"), "REJECT"),
    ))
    plans = rt._runtime_initial_candidate_plans(
        rt._runtime_route_snapshot, rt._runtime_route_snapshot_payload)
    assert [(plan["ref"].proxy_name, plan["probe_ref"].proxy_name) for plan in plans] == [
        ("Free", leaf)]


# ------------------------------------------------- no literal % (F17)

def test_stable_names_never_carry_a_literal_percent():
    proxy = {"type": "http", "server": "a.invalid", "port": 1}
    name = _stable_proxy_name("50%41 %2F 100%", proxy, set())
    assert "%" not in name and name.startswith("50％41 ％2F 100％·")
    plain = _stable_proxy_name("Tokyo 01", proxy, set())
    # Only the base changes; the digest is the definition's, as before.
    assert name.rsplit("·", 1)[1] == plain.rsplit("·", 1)[1]


# ------------- an old name-only selection is bound once (F22, T-GW-ID, durable)

def _persisting(runtime):
    """A bridge harness runtime whose Manager reads the saved selection from
    the in-memory config (as the real Manager does) and whose config store
    records the binding tick's compare-and-set writes, the only write the
    tick may make: never an unconditional or name-only selection write, a
    config reload or a Core reload."""

    rt = prepared(runtime, tier3_limit=3)
    rt.logger = logging.getLogger("tests.tier3-naming-consistency.persist")
    rt.manager.runtime_nodes["exit"] = [gw("Free", address="free-gw.example.invalid")]
    rt.manager._exit_active = lambda: True
    writes = []

    def compare_and_set_exit_selection(expected_key, label, *, identity=None, unresolved=None):
        writes.append((expected_key, label, identity, unresolved))
        return True

    rt.config_store.compare_and_set_exit_selection = compare_and_set_exit_selection
    rt.config_store.set_exit_selection = lambda *a, **k: pytest.fail("selection write")
    rt.config_store.set_exit_selected_proxy = lambda *a, **k: pytest.fail("name-only write")
    rt.reload_config = lambda *a, **k: pytest.fail("config reload")
    rt.reload_core_config = lambda *a, **k: pytest.fail("Core reload")
    return rt, writes


def _reads_the_config(rt):
    rt.manager.active_exit_selection = lambda: read_selection(rt.config["tiers"]["exit"])
    rt.manager.active_exit_proxy_name = lambda: read_selection(rt.config["tiers"]["exit"]).label


def _assert_bound(rt, label, identity):
    """The in-memory exit block after a bind: 'selected' popped, the label
    and the record written together."""
    block = rt.config["tiers"]["exit"]
    assert "selected" not in block
    assert block["selected_proxy"] == label
    assert block["selected_node"] == {"identity": identity, "label": label}
    assert read_selection(block) == GatewaySelection("bound", label, identity=identity)


def _render(rt):
    """The runtime export's real render: (payload, receipt)."""
    receipt = {}
    _, payload = render_subscription_route(
        rt._manager_for_mihomo_runtime_config(), "/clash/config",
        {"tier3_limit": rt._mihomo_tier3_export_limit()}, render_receipt=receipt)
    return payload, receipt


def _load(rt, *, publish=True):
    """strict_reload (tests.test_bridge_h8) over the saved selection as it
    stands, keeping the exact main payload a provider write reuses: the real
    render with its receipt -> proxy map -> export contract -> loaded-contract
    commit, then (``publish``) the route publication."""
    payload, receipt = _render(rt)
    contract = rt._runtime_export_contract_for_payload(
        payload, rendered_proxy_map=rt._runtime_proxy_map_from_render_receipt(receipt))
    rt._record_pending_runtime_export_contract(payload, Path("isolated-runtime.yaml"),
                                               contract=contract)
    rt._mark_loaded_runtime_export_contract()
    document = yaml.safe_load(payload)
    if publish:
        publish_route(rt, document)
    return payload, document


def _render_and_load(rt, selected):
    """strict_reload, keeping the exact main payload a provider write reuses."""
    rt.config["tiers"]["exit"]["selected"] = selected
    rt.manager.active_exit_proxy_name = lambda: selected
    return _load(rt)


def test_a_resolved_selection_is_saved_once_and_outlives_the_collider(runtime):
    """The binding tick (_persist_gateway_selection_binding, replacing
    _persist_resolved_gateway_selection) saves an old name-only selection
    once, as the identity of the member the loaded Core serves for it with
    that member's name as its label, without a reload; the record keeps
    serving the Gateway after the colliding name left."""
    rt, writes = _persisting(runtime)
    gateway = rt.manager.runtime_nodes["exit"][0]
    identity = gateway_identity(gateway)
    rt.manager.runtime_nodes["tier3"] = [other("Free", "tier3", address="c.invalid")]
    payload, _document = _render_and_load(rt, "Free-2")  # saved before 11-02
    assert exit_select(rt) == ("Free",)
    _reads_the_config(rt)

    assert rt._persist_gateway_selection_binding() is True
    assert writes == [("name:Free-2", "Free", identity, None)]
    _assert_bound(rt, "Free", identity)
    assert rt._persist_gateway_selection_binding() is False  # once
    assert writes == [("name:Free-2", "Free", identity, None)]
    # The loaded Core already serves the bound node: the same payload.
    assert _render(rt)[0] == payload

    # The colliding Bastion member leaves the export: the old name alone now
    # selects nothing (EXIT-SELECT fails closed) ...
    assert _exit_groups([http("Else", "e.invalid")], [gateway], "Free-2", stable=True)[0] == [
        "REJECT"]
    # ... the saved identity keeps serving the Gateway.
    rt.manager.runtime_nodes["tier3"] = [other("Else", "tier3", address="e.invalid")]
    _load(rt)
    assert exit_select(rt) == ("Free",)
    assert resolved(rt) == (node_id(rt, "exit", gateway), None)


def test_a_served_positional_name_binds_and_unloaded_resolutions_are_never_saved(runtime):
    """The tick saves a node record, never a name alone.  T-GW-ID turns the
    switch-off case from "never saved" into a bind: the positional name the
    loaded Core serves is only the label, so the Bastion export moving that
    name later cannot move the selection.  A member the published
    EXIT-SELECT does not hold, or a pick made meanwhile, is never saved."""
    rt, writes = _persisting(runtime)
    gateway = rt.manager.runtime_nodes["exit"][0]
    identity = gateway_identity(gateway)
    rt.manager.runtime_nodes["tier3"] = [other("Free", "tier3", address="c.invalid")]
    # Switch off: a stable-era "Free" is served as "Free-2" in this render.
    rt.config["tiers"]["tier3"]["verified_provider_writes"] = False
    strict_reload(rt, "Free")
    assert exit_select(rt) == ("Free-2",)
    _reads_the_config(rt)
    assert rt._persist_gateway_selection_binding() is True
    assert writes == [("name:Free", "Free-2", identity, None)]
    _assert_bound(rt, "Free-2", identity)
    # The positional name moves with the Bastion export; the selection does not.
    rt.manager.runtime_nodes["tier3"] = []
    _load(rt)
    assert exit_select(rt) == ("Free",)
    assert resolved(rt) == (node_id(rt, "exit", gateway), None)

    # Switch on, but the loaded Core's EXIT-SELECT does not hold the member.
    rt.config["tiers"]["tier3"]["verified_provider_writes"] = True
    rt.manager.runtime_nodes["tier3"] = [other("Free", "tier3", address="c.invalid")]
    clear_gateway_selection(rt.config["tiers"]["exit"])
    document = strict_reload(rt, "Free-2", publish=False)
    for group in document["proxy-groups"]:
        if group["name"] == "EXIT-SELECT":
            group["proxies"] = ["REJECT"]
    publish_route(rt, document)
    _reads_the_config(rt)
    assert rt._persist_gateway_selection_binding() is False

    # A Gateway the user picks meanwhile wins over the save.
    strict_reload(rt, "Free-2")
    answers = iter([GatewaySelection("legacy", "Free-2"), GatewaySelection("legacy", "Picked")])
    rt.manager.active_exit_selection = lambda: next(answers)
    assert rt._persist_gateway_selection_binding() is False
    assert writes == [("name:Free", "Free-2", identity, None)]
    # Not superseded, the same published route binds: the refusals above
    # were the tick's decisions, not a broken harness.
    _reads_the_config(rt)
    assert rt._persist_gateway_selection_binding() is True
    assert writes[1:] == [("name:Free-2", "Free", identity, None)]


def test_a_tick_before_the_new_contract_is_published_does_not_skip_its_save(runtime):
    # A loaded contract is committed before its route is published (a
    # no-capture standby reload, a contract-only provider commit): a tick in
    # that window has no verdict and must not burn the once-per-contract check.
    rt, writes = _persisting(runtime)
    identity = gateway_identity(rt.manager.runtime_nodes["exit"][0])
    rt.manager.runtime_nodes["tier3"] = [other("Free", "tier3", address="c.invalid")]
    strict_reload(rt, "Free-2")                         # contract A, published
    rt.manager.runtime_nodes["tier3"] = [other("Free", "tier3", address="c.invalid"),
                                         other("Else", "tier3", address="e.invalid")]
    document = strict_reload(rt, "Free-2", publish=False)  # contract B, A's route
    _reads_the_config(rt)
    assert rt._persist_gateway_selection_binding() is False
    assert writes == []
    assert getattr(rt, "_gateway_selection_binding_signature", None) is None

    publish_route(rt, document)
    assert rt._persist_gateway_selection_binding() is True
    assert writes == [("name:Free-2", "Free", identity, None)]
    _assert_bound(rt, "Free", identity)


# ------------- the renderer's decision travels with the loaded graph (SK-OFF-1)

def test_the_renderer_decision_survives_a_provider_only_commit_after_the_collider_left(runtime):
    """The loaded decision (``selected_for``, now the selection's key) stays
    with the loaded main graph across a provider-only commit whose fresh
    render decides REJECT; the route readers and the binding tick use it."""
    rt, writes = _persisting(runtime)
    target = rt.manager.runtime_nodes["exit"][0]
    keeper = other("Else", "tier3", address="e.invalid")
    rt.manager.runtime_nodes["tier3"] = [other("Free", "tier3", address="c.invalid"), keeper]
    payload, document = _render_and_load(rt, "Free-2")   # saved before 11-02
    assert exit_select(rt) == ("Free",)

    # Before the binding tick saved the node, the Manager drops the colliding
    # Bastion member and a provider-only write commits over the unchanged
    # main graph (the fresh render alone would decide REJECT).
    rt.manager.runtime_nodes["tier3"] = [keeper]
    _fresh, receipt = _render(rt)
    assert receipt["exit_selection"] == {
        "requested": "Free-2", "key": "name:Free-2", "state": "legacy", "member": "",
        "identity": "", "resolution": "legacy_unconfirmed"}
    loaded = rt._runtime_loaded_export_contract
    contract = rt._runtime_export_contract_for_payload(
        payload, rendered_proxy_map=rt._runtime_proxy_map_from_render_receipt(receipt))
    assert contract["main_semantic_digest"] == loaded["main_semantic_digest"]
    binding = {"semantic_digest": loaded["semantic_digest"], "contract_revision": loaded["revision"]}
    assert rt._commit_tier3_provider_contract_only(contract, binding, preserve_route_authority=True)
    publish_route(rt, document)                          # the Core still serves "Free"
    _reads_the_config(rt)

    # The loaded decision (the selection's key) stays on the loaded member.
    assert rt._runtime_loaded_export_contract["proxy_map"]["Free"]["selected_for"] == "name:Free-2"
    assert rt._loaded_gateway_member(GatewaySelection("legacy", "Free-2")) == "Free"
    status = rt.bridge_gateway_status()
    assert (status["stale"], status["missing"], status["selected"]) == (False, False, "Free")
    assert resolved(rt) == (node_id(rt, "exit", target), None)
    assert rt._startup_route_intent_from_snapshot(rt._runtime_route_snapshot) is not None
    assert rt._persist_gateway_selection_binding() is True
    assert writes == [("name:Free-2", "Free", gateway_identity(target), None)]
    _assert_bound(rt, "Free", gateway_identity(target))


def test_a_decision_belongs_to_its_main_graph_only(runtime):
    """``selected_for`` is the selection's key ("name:<label>" for a
    name-only selection), recorded on the member of its own main graph."""
    rt, _writes = _persisting(runtime)
    rt.manager.runtime_nodes["tier3"] = [other("Free", "tier3", address="c.invalid")]
    _render_and_load(rt, "Free-2")
    entries = rt._runtime_loaded_export_contract["proxy_map"]
    assert {name: info.get("selected_for") for name, info in entries.items()
            if info.get("selected_for")} == {"Free": "name:Free-2"}
    # A different main graph (a full reload) carries only its own decision.
    rt.manager.runtime_nodes["tier3"] = []
    _render_and_load(rt, "Free")
    entries = rt._runtime_loaded_export_contract["proxy_map"]
    assert {name: info.get("selected_for") for name, info in entries.items()
            if info.get("selected_for")} == {"Free": "name:Free"}


# ----------------------- verified-lane state is per runtime session (CC-2)

def test_verified_lane_state_starts_fresh_at_each_runtime_start(tmp_path):
    runtime = _runtime(tmp_path, verified=True)
    runtime.system_proxy = SystemProxyManager(
        runtime.config["client"]["system_proxy"],
        backend=MemoryProxyBackend(ProxySettings(enabled=False, server="", override="")),
        platform_name="windows",
    )
    runtime.runtime_state = RuntimeStateStore(tmp_path / "runtime-state.json")
    runtime.config["killswitch"] = {"enabled": False}
    runtime.recover_stale_system_proxy = lambda: False
    runtime.restore_system_proxy = lambda: False
    runtime._warn_if_killswitch_unavailable = lambda: None
    runtime._start_subscription_server = lambda: None
    runtime._monitor_loop = lambda: runtime.stop_event.wait()
    runtime.status = lambda *a, **k: {"running": bool(runtime.running),
                                       "runtime_generation": int(runtime._runtime_generation)}
    with patch("desktop_runtime.ProxyTierManager", _RenderManager), \
            patch("desktop_runtime.KillSwitchManager", _KillSwitch):
        runtime.start(start_core=False)
        try:
            # A previous session's backoff, kick stamps and re-run signature.
            runtime._tier3_online_growth_waits = 6
            runtime._tier3_online_growth_waited_at = 123.0
            runtime._tier3_kicked_pick_since = {"K·abc123": 1.0}
            runtime._tier3_kicked_pick_retry_signature = ("K·abc123", (("K·abc123", 1.0),), True)
        finally:
            runtime.stop(actor="test", reason="per-session state")
        runtime.start(start_core=False)
        try:
            assert runtime._tier3_online_growth_waits == 0
            assert runtime._tier3_online_growth_waited_at == 0.0
            assert runtime._tier3_kicked_pick_since == {}
            assert runtime._tier3_kicked_pick_retry_signature is None
        finally:
            runtime.stop(actor="test", reason="per-session state")
