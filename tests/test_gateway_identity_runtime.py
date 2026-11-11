"""T-GW-ID: the saved Gateway selection names a node, through the real runtime.

A Gateway's rendered proxy name moves: same-named exits swap their ``-2``
suffixes with latency order, a namesake in an earlier tier or the Tier3 naming
switch pushes it.  The selection is saved as the node's identity with its name
as a label (gateway_selection), the renderer records which EXIT-SELECT member
it chose for that identity, and every reader of the loaded route takes that
member: the loaded contract, gateway.current, selected_node_id, the startup
intent and initial plans, the pick's Mihomo PUT and the one-time binding of an
old name-only selection.

Through the real render receipt -> proxy map -> export contract -> loaded
contract -> published route chain of DesktopRuntime (the test_bridge_h8
harness: prepared, publish_route, resolved, exit_select; the test_bridge_h5
node helpers) and the real clash_formatter renderer.  Isolated: the P2-04
synthetic runtime; no live profile, no Core, no Mihomo, no network, no HKCU or
firewall writes.  Config writes, the Core reload and Mihomo PUTs are
recorders around the real runtime methods.
"""
from __future__ import annotations

from dataclasses import replace
import logging
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
import yaml

import bridge_protocol as protocol
from bridge_node_commands import NodeCommands
from clash_formatter import build_full_clash_config
from gateway_selection import (
    GatewaySelection,
    clear_gateway_selection,
    gateway_identity,
    read_selection,
    write_bound_selection,
    write_name_only_selection,
)
from models import NodeStatus
from runtime_egress_recovery import CoreLoadedGroupState
from subscription import render_subscription_route
from tests.test_bridge_h5 import gw, node_id, other, real_order
from tests.test_bridge_h8 import (exit_select, prepared, publish_route, published, resolved,
                                  sampled, strict_reload, tick)
from tests.test_bridge_p2_04 import runtime  # noqa: F401  (pytest fixture)
from tests.test_bridge_p2_14 import FakePort, executor, run, wire_text  # noqa: F401


# --------------------------------------------------------------- harness

def selection_of(rt):
    return read_selection(rt.config["tiers"]["exit"])


def reads_the_config(rt):
    """The synthetic Manager reads the saved selection from the in-memory
    config, as ProxyTierManager.active_exit_selection does."""
    rt.manager.active_exit_selection = lambda: selection_of(rt)
    rt.manager.active_exit_proxy_name = lambda: selection_of(rt).label


def choose(rt, node, label):
    """A pick saved by this build: the node's identity, its name as label."""
    write_bound_selection(rt.config["tiers"]["exit"], label, gateway_identity(node))
    reads_the_config(rt)


def render(rt):
    """A fresh render of the runtime export view (nothing is loaded)."""
    receipt = {}
    _, payload = render_subscription_route(
        rt._manager_for_mihomo_runtime_config(), "/clash/config",
        {"tier3_limit": rt._mihomo_tier3_export_limit()}, render_receipt=receipt)
    return payload, receipt


def load(rt, *, publish=True):
    """test_bridge_h8.strict_reload of the saved selection as it is (nothing
    written to ``selected``): the real render with its receipt, receipt ->
    proxy map -> export contract -> loaded contract, then (``publish``) the
    route publication made from that loaded contract."""
    payload, receipt = render(rt)
    contract = rt._runtime_export_contract_for_payload(
        payload, rendered_proxy_map=rt._runtime_proxy_map_from_render_receipt(receipt))
    rt._record_pending_runtime_export_contract(payload, Path("isolated-runtime.yaml"),
                                               contract=contract)
    rt._mark_loaded_runtime_export_contract()
    document = yaml.safe_load(payload)
    if publish:
        publish_route(rt, document)
    return payload, document


def groups(document):
    return {group["name"]: group["proxies"] for group in document["proxy-groups"]}


def loaded_exits(rt):
    """The loaded contract's Gateway entries: proxy name -> served server."""
    return {name: info["node"].address
            for name, info in rt._runtime_loaded_export_contract["proxy_map"].items()
            if info["tier"] == "exit"}


def main_digest(rt, payload):
    """The loaded-contract digest of a main payload (what the reload gate compares)."""
    return rt._runtime_export_contract_for_payload(payload, rendered_proxy_map={})[
        "main_semantic_digest"]


def twins(runtime):
    """Two Gateways named "JP 01", real exit order: gw1 renders "JP 01" and
    gw2 "JP 01-2" until a latency change swaps them."""
    rt = real_order(prepared(runtime))
    gw1 = gw("JP 01", address="gw1.example.invalid", password="Pw-1", latency_ms=20)
    gw2 = gw("JP 01", address="gw2.example.invalid", password="Pw-2", latency_ms=40)
    rt.manager.runtime_nodes["exit"] = [gw1, gw2]
    return rt, gw1, gw2


def free_gateway(runtime, *, verified=True):
    """A Bastion member and the only Gateway share the base "Free"."""
    rt = prepared(runtime, tier3_limit=3)
    rt.config["tiers"]["tier3"]["verified_provider_writes"] = verified
    gateway = gw("Free", address="free-gw.example.invalid")
    rt.manager.runtime_nodes["tier3"] = [other("Free", "tier3", address="c.invalid")]
    rt.manager.runtime_nodes["exit"] = [gateway]
    return rt, gateway


class Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def binder(rt, *, saved=True):
    """What the bind tick writes through: a recording logger, and a config
    store whose compare-and-set records the write and answers ``saved`` (the
    file side is tests/test_config_store.py's)."""
    handler = Records()
    rt.logger = logging.Logger("tests.gateway-identity-runtime")
    rt.logger.addHandler(handler)
    writes = []

    def compare_and_set_exit_selection(expected_key, label, *, identity=None, unresolved=None):
        writes.append({"expected_key": expected_key, "label": label,
                       "identity": identity, "unresolved": unresolved})
        return saved

    rt.config_store.compare_and_set_exit_selection = compare_and_set_exit_selection
    return writes, handler.records


def errors(records):
    return [record for record in records if record.levelno >= logging.ERROR]


def picking(rt, *, during=None, running=True):
    """The real DesktopRuntime.set_exit_selection / set_exit_selected_proxy
    with recorders around it: the selector intent is declared, the config
    write lands in rt.config (as reload_config would load it), the strict
    Core reload is the harness's render -> contract -> load -> publish after
    ``during`` changed the nodes, and Mihomo PUTs are recorded."""
    rt.logger = logging.Logger("tests.gateway-identity-runtime.pick")
    rt.logger.addHandler(logging.NullHandler())
    rt._runtime_selector_intent_revision = 1
    rt._declare_runtime_selector_intent = (
        lambda reason, *, group=None: rt._runtime_selector_intent_revision)
    rt._core_transition_lock = threading.RLock()
    log = {"writes": [], "reloads": [], "puts": [], "rechecks": 0}

    def set_exit_selection(label, *, identity=None, unresolved=None):
        log["writes"].append((label, identity))
        block = rt.config["tiers"]["exit"]
        if not label:
            clear_gateway_selection(block)
        elif identity:
            write_bound_selection(block, label, identity)
        else:
            write_name_only_selection(block, label)
        return {}

    def reload_core_config(*, mode, reason, user_commit_id, publish_verified_ready=True):
        # Decision D option A: the pick ends in its own CONTROLLER_HOLD, so
        # its reload never republishes READY.
        assert publish_verified_ready is False
        log["reloads"].append(mode)
        if during is not None:
            during()
        load(rt)
        return {"reloaded": True, "running": True}

    def select_mihomo_proxy(group, name, *, revalidate=True):
        log["puts"].append((group, name))
        return {"group": group, "selected": name}

    def recheck():
        log["rechecks"] += 1

    rt.config_store.set_exit_selection = set_exit_selection
    rt.reload_config = lambda target_tier=None: None
    rt.reload_core_config = reload_core_config
    rt.select_mihomo_proxy = select_mihomo_proxy
    rt._queue_incremental_egress_recheck = recheck
    rt.core_runner = SimpleNamespace(is_running=lambda: running)
    reads_the_config(rt)
    return log


def activation(rt, exit_members, leaf):
    """The loaded route test_initial_activation_plans_bind_the_resolved_gateway
    patches in: EXIT-SELECT holding ``exit_members`` over a Bastion leaf."""
    rt._runtime_route_snapshot = replace(rt._runtime_route_snapshot, group_states=(
        CoreLoadedGroupState("FINAL-EXIT", "Selector", ("EXIT-SELECT", "REJECT"), "EXIT-SELECT"),
        CoreLoadedGroupState("EXIT-SELECT", "Selector", tuple(exit_members), exit_members[0]),
        CoreLoadedGroupState("PROXY-FALLBACK", "Fallback", ("TIER3-AUTO",), "TIER3-AUTO"),
        CoreLoadedGroupState("TIER3-AUTO", "Fallback", (leaf, "REJECT"), "REJECT"),
    ))
    return rt._runtime_route_snapshot, rt._runtime_route_snapshot_payload


class PickPort(FakePort):
    """FakePort answering one node_id pick with facts the real runtime gave."""

    def __init__(self, facts, answer, display):
        super().__init__()
        self.facts, self.answer, self.display = facts, answer, display

    def exit_proxy_name(self, node_id):
        self.calls.append(("exit_proxy_name", node_id))
        return self.facts

    def set_exit_selection(self, proxy_name, identity):
        self.calls.append(("set_exit_selection", proxy_name, identity))
        return self.answer

    def persisted_exit_selection_key(self):
        return None

    def display_proxy_name(self, raw):
        return self.display(raw)


# ------------------------------------------------- the loaded route readers

def test_loaded_member_and_node_id_follow_the_identity_across_a_swap(runtime):
    """Replaces the decoy pins of test_bridge_h8 (a saved "Paris-2" followed
    its suffix to the decoy after a reorder): the picked node keeps being
    served under whatever name the reload gives it, and selected_node_id
    stays the pick's Node.id."""
    rt, gw1, gw2 = twins(runtime)
    choose(rt, gw2, "JP 01-2")
    key = "id:" + gateway_identity(gw2)
    load(rt)
    assert exit_select(rt) == ("JP 01-2",)
    entry = rt._runtime_loaded_export_contract["proxy_map"]["JP 01-2"]
    assert entry["node"].address == gw2.address and entry["selected_for"] == key
    assert rt._loaded_gateway_member(selection_of(rt)) == "JP 01-2"
    assert resolved(rt) == (node_id(rt, "exit", gw2), None)

    gw2.latency_ms = 5                                      # the names swap
    load(rt)
    assert exit_select(rt) == ("JP 01",)
    proxy_map = rt._runtime_loaded_export_contract["proxy_map"]
    assert proxy_map["JP 01"]["node"].address == gw2.address
    assert proxy_map["JP 01"]["selected_for"] == key
    # The label is the other twin's name now; it carries no decision.
    assert proxy_map["JP 01-2"]["node"].address == gw1.address
    assert "selected_for" not in proxy_map["JP 01-2"]
    assert selection_of(rt) == GatewaySelection("bound", "JP 01-2",
                                                identity=gateway_identity(gw2))
    assert rt._loaded_gateway_member(selection_of(rt)) == "JP 01"
    assert resolved(rt) == (node_id(rt, "exit", gw2), None)
    assert node_id(rt, "exit", gw1) != node_id(rt, "exit", gw2)
    assert rt.bridge_gateway_status()["missing"] is False
    assert rt.api_client.calls == []


def test_gateway_current_selected_equals_effective_after_a_name_move(runtime):
    rt, _gw1, gw2 = twins(runtime)
    choose(rt, gw2, "JP 01-2")
    load(rt)
    state, topics, work = sampled(rt)
    tick(topics, work)
    data = published(state)
    assert data["selected"] == data["effective"] == "JP 01-2"
    assert data["selected_node_id"] == node_id(rt, "exit", gw2)

    gw2.latency_ms = 5                                      # the names swap
    load(rt)
    tick(topics, work)
    data = published(state)
    # selected is the loaded member's display name, not the saved label.
    assert data["selected"] == data["effective"] == "JP 01"
    assert data["applied"] is True and data["missing"] is False
    assert data["selected_node_id"] == node_id(rt, "exit", gw2)
    assert protocol.validate_topic_payload("gateway.current", data) == []
    assert rt.api_client.calls == []

    # No Core: the saved choice is the whole fact, shown under its label.
    rt.core_fact = lambda: {"running": False, "pid": None, "process_generation": 0,
                            "core_generation": 8, "cleanup_pending": False}
    state, topics, work = sampled(rt, running=False)
    tick(topics, work)
    assert published(state) == {"selected": "JP 01-2", "effective": None, "missing": False,
                                "applied": False, "pending": None}
    assert resolved(rt) == (None, "stopped")


def test_unmatched_identity_reports_missing_with_the_label(runtime):
    rt, gw1, gw2 = twins(runtime)
    choose(rt, gw2, "JP 01-2")
    gw2.password = "Rotated"                    # the provider reissued the credential
    load(rt)
    # The label is still rendered (the rotated node's name): never served by name.
    assert loaded_exits(rt) == {"JP 01": gw1.address, "JP 01-2": gw2.address}
    assert exit_select(rt) == ("REJECT",)
    assert rt._loaded_gateway_member(selection_of(rt)) == ""
    status = rt.bridge_gateway_status()
    assert status["missing"] is True and status["applied"] is False
    assert status["selected"] == "JP 01-2" and status["effective"] is None
    assert status["stale"] is False
    assert resolved(rt) == (None, "missing")


# ------------------------------------- provider-only commits (PC-1 carry-over)

def test_provider_only_commit_after_an_exit_swap_keeps_the_loaded_gateway_entries(runtime):
    rt, gw1, gw2 = twins(runtime)
    choose(rt, gw2, "JP 01-2")
    payload, document = load(rt)
    loaded = rt._runtime_loaded_export_contract
    gw2.latency_ms = 5                          # a swap; Core keeps the loaded main graph
    _, receipt = render(rt)
    assert receipt["exit_selection"]["member"] == "JP 01"
    fresh = rt._runtime_proxy_map_from_render_receipt(receipt)
    # PC-1: every fresh Gateway definition differs from the loaded one of its
    # name, so the fresh map alone keeps no Gateway entry at all.
    by_name = {proxy["name"]: proxy for proxy in document["proxies"]}
    assert {name for name, info in fresh.items() if info["tier"] == "exit"} == {"JP 01", "JP 01-2"}
    assert all(info["proxy"] != by_name.get(name)
               for name, info in fresh.items() if info["tier"] == "exit")
    contract = rt._runtime_export_contract_for_payload(payload, rendered_proxy_map=fresh)
    assert contract["main_semantic_digest"] == loaded["main_semantic_digest"]
    binding = {"semantic_digest": loaded["semantic_digest"],
               "contract_revision": loaded["revision"]}
    assert rt._commit_tier3_provider_contract_only(contract, binding, preserve_route_authority=True)
    publish_route(rt, document)                 # the Core still serves the loaded graph
    assert rt._runtime_loaded_export_contract["revision"] == loaded["revision"] + 1
    assert loaded_exits(rt) == {"JP 01": gw1.address, "JP 01-2": gw2.address}
    proxy_map = rt._runtime_loaded_export_contract["proxy_map"]
    assert proxy_map["JP 01-2"]["selected_for"] == "id:" + gateway_identity(gw2)
    assert "selected_for" not in proxy_map["JP 01"]
    assert exit_select(rt) == ("JP 01-2",)
    assert rt._loaded_gateway_member(selection_of(rt)) == "JP 01-2"
    status = rt.bridge_gateway_status()
    assert status["missing"] is False and status["stale"] is False
    assert resolved(rt) == (node_id(rt, "exit", gw2), None)


def test_provider_only_commit_after_a_positional_collider_left_keeps_the_gateway(runtime):
    """risk-first's failing probe scenario: positional Tier3 names, a Bastion
    "Free" pushes the Gateway to "Free-2"; the Bastion leaves and a
    provider-only commit reuses the loaded main graph."""
    rt = prepared(runtime, tier3_limit=3)
    rt.config["tiers"]["tier3"]["verified_provider_writes"] = False
    target = gw("Free", address="free-gw.example.invalid")
    keeper = other("Else", "tier3", address="e.invalid")
    rt.manager.runtime_nodes["tier3"] = [other("Free", "tier3", address="c.invalid"), keeper]
    rt.manager.runtime_nodes["exit"] = [target]
    choose(rt, target, "Free-2")
    payload, document = load(rt)
    assert exit_select(rt) == ("Free-2",)
    loaded = rt._runtime_loaded_export_contract
    rt.manager.runtime_nodes["tier3"] = [keeper]            # the collider leaves
    _, receipt = render(rt)
    assert receipt["exit_selection"]["member"] == "Free"    # a fresh render renames it
    fresh = rt._runtime_proxy_map_from_render_receipt(receipt)
    by_name = {proxy["name"]: proxy for proxy in document["proxies"]}
    assert "Free-2" not in fresh and fresh["Free"]["proxy"] != by_name["Free"]   # PC-1
    contract = rt._runtime_export_contract_for_payload(payload, rendered_proxy_map=fresh)
    binding = {"semantic_digest": loaded["semantic_digest"],
               "contract_revision": loaded["revision"]}
    assert rt._commit_tier3_provider_contract_only(contract, binding, preserve_route_authority=True)
    publish_route(rt, document)
    entry = rt._runtime_loaded_export_contract["proxy_map"]["Free-2"]
    assert entry["tier"] == "exit" and entry["node"].address == target.address
    assert entry["selected_for"] == "id:" + gateway_identity(target)
    assert rt._loaded_gateway_member(selection_of(rt)) == "Free-2"
    assert rt.bridge_gateway_status()["missing"] is False
    assert resolved(rt) == (node_id(rt, "exit", target), None)


# ------------------------------------------------ startup intent and plans

def test_startup_intent_and_initial_plans_use_the_identity_member(runtime):
    rt, _gw1, gw2 = twins(runtime)
    rt.manager._exit_active = lambda: True
    rt.manager.runtime_nodes["tier3"] = [other("Keep", "tier3", address="keep.invalid")]
    choose(rt, gw2, "JP 01-2")
    load(rt)
    gw2.latency_ms = 5                                      # the names swap
    _, document = load(rt)
    assert exit_select(rt) == ("JP 01",)
    refs = {ref.proxy_name: ref for ref in rt._runtime_route_snapshot.proxy_nodes}
    assert refs["JP 01"].identity == rt._node_identity_text(gw2)

    intent = rt._startup_route_intent_from_snapshot(rt._runtime_route_snapshot)
    assert intent is not None
    assert intent.expected_gateway_key == tuple(refs["JP 01"].key)
    assert intent.top_level_target == "EXIT-SELECT"
    assert ("EXIT-SELECT", "JP 01", "selector") in intent.actions
    leaf = groups(document)["TIER3-AUTO"][0]
    plans = rt._runtime_initial_candidate_plans(*activation(rt, ("JP 01",), leaf))
    assert [(plan["ref"].proxy_name, plan["probe_ref"].proxy_name) for plan in plans] == [
        ("JP 01", leaf)]
    assert plans[0]["ref"].key == refs["JP 01"].key

    # The picked node's credential rotates: no loaded member is the pick.
    gw2.password = "Rotated"
    _, document = load(rt)
    assert exit_select(rt) == ("REJECT",)
    assert rt._startup_route_intent_from_snapshot(rt._runtime_route_snapshot) is None
    leaf = groups(document)["TIER3-AUTO"][0]
    # Even a Core holding the rotated node's name or the label (gw1 now) is
    # no authority for the saved node.
    for held in (("JP 01",), ("JP 01-2",)):
        assert rt._runtime_initial_candidate_plans(*activation(rt, held, leaf)) == [], held


# ------------------------------- binding an old name-only selection (tick)

@pytest.mark.parametrize("verified, legacy, member", [
    (True, "Free-2", "Free"),       # saved under positional names, served under stable
    (False, "Free", "Free-2"),      # saved under stable names, served under positional
], ids=["stable", "positional"])
def test_bind_tick_binds_a_served_unique_legacy_selection_once_in_both_switch_states(
        runtime, verified, legacy, member):
    rt, gateway = free_gateway(runtime, verified=verified)
    writes, records = binder(rt)
    strict_reload(rt, legacy)                   # an older build's pick (``selected``)
    reads_the_config(rt)
    assert exit_select(rt) == (member,)
    assert selection_of(rt) == GatewaySelection("legacy", legacy)
    loaded = rt._runtime_loaded_export_contract
    before, _ = render(rt)
    assert main_digest(rt, before) == loaded["main_semantic_digest"]   # = the loaded payload

    assert rt._persist_gateway_selection_binding() is True
    identity = gateway_identity(gateway)
    assert writes == [{"expected_key": "name:" + legacy, "label": member,
                       "identity": identity, "unresolved": None}]
    block = rt.config["tiers"]["exit"]
    assert "selected" not in block and block["selected_proxy"] == member
    assert block["selected_node"] == {"identity": identity, "label": member}
    assert selection_of(rt) == GatewaySelection("bound", member, identity=identity)
    # One name-free info line (proxy names may carry secrets).
    infos = [record.getMessage() for record in records if record.levelno == logging.INFO]
    assert len(infos) == 1 and not errors(records)
    assert not any(text in message for message in infos for text in (legacy, member, identity))
    assert rt._persist_gateway_selection_binding() is False   # once
    assert len(writes) == 1
    # The bound selection renders the very payload Core loaded: no reload.
    after, _ = render(rt)
    assert after == before


@pytest.mark.parametrize("label", ["JP 01-2", "JP 01"])
def test_bind_tick_persists_unresolved_for_an_ambiguous_legacy_name(runtime, label):
    rt, gw1, gw2 = twins(runtime)
    writes, records = binder(rt)
    strict_reload(rt, label)                    # an older build's pick of a twin
    reads_the_config(rt)
    assert exit_select(rt) == ("REJECT",)
    assert rt._persist_gateway_selection_binding() is True
    assert writes == [{"expected_key": "name:" + label, "label": label,
                       "identity": None, "unresolved": "ambiguous"}]
    block = rt.config["tiers"]["exit"]
    assert "selected" not in block and block["selected_proxy"] == label
    assert block["selected_node"] == {"unresolved": "ambiguous", "label": label}
    infos = [record.getMessage() for record in records if record.levelno == logging.INFO]
    assert len(infos) == 1 and not errors(records)
    assert not any(label in message for message in infos)
    assert rt._persist_gateway_selection_binding() is False
    assert len(writes) == 1

    # gw1 leaves: the survivor gw2 is never served under the old name.
    rt.manager.runtime_nodes["exit"] = [gw2]
    _, document = load(rt)
    assert selection_of(rt) == GatewaySelection("unresolved", label, reason="ambiguous")
    assert groups(document)["EXIT-SELECT"] == ["REJECT"]
    assert rt._persist_gateway_selection_binding() is False
    assert len(writes) == 1
    status = rt.bridge_gateway_status()
    assert status["missing"] is True and status["selected"] == label
    if label == "JP 01":
        # Not vacuous: the name alone would now serve the survivor.
        name_only = yaml.safe_load(
            build_full_clash_config({"exit": [gw2]}, active_exit_proxy=label))
        assert groups(name_only)["EXIT-SELECT"] == ["JP 01"]


@pytest.mark.parametrize("case", ["unpublished", "unserved", "unsettled", "superseded",
                                  "store_refused"])
def test_bind_tick_never_writes_when_unpublished_unserved_unsettled_or_superseded(
        runtime, case):
    """A served unique legacy selection ("Free-2" -> the Gateway "Free"),
    one fresh runtime per case; each case withholds one condition."""
    rt, gateway = free_gateway(runtime)
    writes, records = binder(rt, saved=case != "store_refused")
    bound = [{"expected_key": "name:Free-2", "label": "Free",
              "identity": gateway_identity(gateway), "unresolved": None}]

    def unspent():
        return getattr(rt, "_gateway_selection_binding_signature", None) is None

    if case == "unpublished":
        # (a) The loaded contract's route is not published yet: no verdict,
        # and the once-per-contract signature is not spent.
        strict_reload(rt, "Free-2")                             # contract A, published
        rt.manager.runtime_nodes["tier3"].append(other("Else", "tier3", address="e.invalid"))
        document = strict_reload(rt, "Free-2", publish=False)   # contract B, A's route
        reads_the_config(rt)
        assert rt._persist_gateway_selection_binding() is False
        assert writes == [] and unspent()
        publish_route(rt, document)
        assert rt._persist_gateway_selection_binding() is True
        assert writes == bound
    elif case == "unserved":
        # (b) The published EXIT-SELECT does not hold the member (unique family).
        document = strict_reload(rt, "Free-2", publish=False)
        for group in document["proxy-groups"]:
            if group["name"] == "EXIT-SELECT":
                group["proxies"] = ["REJECT"]
        publish_route(rt, document)
        reads_the_config(rt)
        assert rt._persist_gateway_selection_binding() is False
        assert rt._persist_gateway_selection_binding() is False
        assert writes == []
        assert selection_of(rt) == GatewaySelection("legacy", "Free-2")
    elif case == "unsettled":
        # (c) The exit inventory is not settled (a subscription not fetched
        # yet may hold a twin): no write, no signature; once settled, it binds.
        strict_reload(rt, "Free-2")
        reads_the_config(rt)
        settled = {"value": False}
        rt.manager.gateway_legacy_exit_inventory = lambda: (
            [node.clone() for node in rt.manager.runtime_nodes["exit"]], settled["value"])
        assert rt._persist_gateway_selection_binding() is False
        assert writes == [] and unspent()
        assert selection_of(rt) == GatewaySelection("legacy", "Free-2")
        settled["value"] = True
        assert rt._persist_gateway_selection_binding() is True
        assert writes == bound
    elif case == "superseded":
        # (d) A pick made between the observation and the write wins.
        strict_reload(rt, "Free-2")
        picked = GatewaySelection("bound", "Elsewhere",
                                  identity=gateway_identity(gw("Elsewhere", address="x.invalid")))
        observed = []

        def active_exit_selection():
            observed.append(True)
            return GatewaySelection("legacy", "Free-2") if len(observed) == 1 else picked

        rt.manager.active_exit_selection = active_exit_selection
        block = dict(rt.config["tiers"]["exit"])
        assert rt._persist_gateway_selection_binding() is False
        assert len(observed) == 2               # observed, then re-read before the write
        assert writes == [] and rt.config["tiers"]["exit"] == block
    else:
        # The config file changed meanwhile (its compare-and-set refuses):
        # nothing is written in memory either.
        strict_reload(rt, "Free-2")
        reads_the_config(rt)
        block = dict(rt.config["tiers"]["exit"])
        assert rt._persist_gateway_selection_binding() is False
        assert writes == bound and rt.config["tiers"]["exit"] == block
        assert selection_of(rt) == GatewaySelection("legacy", "Free-2")
    # Every refusal is a guard, never a swallowed exception.
    assert not errors(records)


# --------------------------------------------------- the reload gate marker

def test_core_export_sync_marker_tracks_the_selection_key(runtime):
    rt, gw1, gw2 = twins(runtime)
    reads_the_config(rt)
    block = rt.config["tiers"]["exit"]
    id1, id2 = gateway_identity(gw1), gateway_identity(gw2)
    write_bound_selection(block, "JP 01", id1)
    first = rt._core_export_sync_marker()
    write_bound_selection(block, "JP 01", id2)
    second = rt._core_export_sync_marker()
    write_bound_selection(block, "JP 01-2", id1)
    relabeled = rt._core_export_sync_marker()
    write_name_only_selection(block, "JP 01")
    legacy = rt._core_export_sync_marker()
    assert dict(first)["active_exit"] == "id:" + id1
    assert dict(second)["active_exit"] == "id:" + id2      # same label, another node
    assert relabeled == first                              # a label-only change (a bind)
    assert dict(legacy)["active_exit"] == "name:JP 01"


# ------------------------------------------------------------- the picks

def test_node_id_pick_saves_label_and_identity_and_puts_the_loaded_member(runtime):
    rt, gw1, gw2 = twins(runtime)

    def reload_swaps():
        gw1.latency_ms, gw2.latency_ms = gw2.latency_ms, gw1.latency_ms

    log = picking(rt, during=reload_swaps)
    facts = rt.bridge_exit_proxy_name(node_id(rt, "exit", gw2))
    assert (facts["status"], facts["proxy_name"], facts["identity"]) == (
        "ok", "JP 01-2", gateway_identity(gw2))
    response = rt.set_exit_selection(proxy_name=facts["proxy_name"], identity=facts["identity"])
    assert log["writes"] == [("JP 01-2", gateway_identity(gw2))]
    assert log["reloads"] == ["strict"]
    # The reload swapped the names: the PUT names the loaded member, never the label.
    assert log["puts"] == [("FINAL-EXIT", "EXIT-SELECT"), ("EXIT-SELECT", "JP 01")]
    assert loaded_exits(rt) == {"JP 01": gw2.address, "JP 01-2": gw1.address}
    assert response["member"] == "JP 01" and response["applied"] is True
    assert response["selection_key"] == "id:" + gateway_identity(gw2)
    assert response["selected"] == "JP 01-2"
    assert response["selection"] == {"group": "EXIT-SELECT", "selected": "JP 01"}
    assert log["rechecks"] == 1


def test_member_unavailable_after_the_reload_is_selection_unconfirmed(runtime, executor):
    rt, _gw1, gw2 = twins(runtime)
    picked = node_id(rt, "exit", gw2)
    facts = dict(rt.bridge_exit_proxy_name(picked))        # the getter, before the pick
    assert (facts["status"], facts["proxy_name"]) == ("ok", "JP 01-2")

    def reload_kills_the_pick():
        gw2.status = NodeStatus.DEAD

    log = picking(rt, during=reload_kills_the_pick)
    answer = rt.set_exit_selection(proxy_name=facts["proxy_name"], identity=facts["identity"])
    assert exit_select(rt) == ("REJECT",)
    assert answer["applied"] is False and answer["member"] == ""
    assert answer["apply_error"] == "gateway_member_unavailable"
    assert log["puts"] == [("FINAL-EXIT", "EXIT-SELECT")]
    assert log["rechecks"] == 0

    port = PickPort(facts, answer, rt.bridge_display_proxy_name)
    handler = NodeCommands(port)
    executor.register("gateway.select", handler.gateway_select)
    job = run(executor, "gateway.select", {"node_id": picked})
    assert port.called("set_exit_selection") == [
        ("set_exit_selection", "JP 01-2", gateway_identity(gw2))]
    assert not port.called("set_exit_selected_proxy")
    assert port.called("exit_proxy_name") == [("exit_proxy_name", picked)]
    assert job["state"] == "failed" and job["error"]["code"] == "io"
    assert job["result"]["apply_error"]["reason_code"] == "selection_unconfirmed"
    assert job["result"]["applied"] is False and job["result"]["selection"] is None
    assert job["result"]["selected"] == rt.bridge_display_proxy_name("JP 01-2")
    assert job["result"]["selected"] == facts["display_name"]
    assert "Pw-2" not in wire_text(job)


def test_old_build_pick_and_clear_are_read_correctly(runtime):
    rt, _gw1, gw2 = twins(runtime)
    writes, _records = binder(rt)
    # (a) An older build re-picks twin gw1 by its name: it writes 'selected'
    # and leaves this build's record (gw2, saved under "JP 01") in place.
    choose(rt, gw2, "JP 01")
    rt.config["tiers"]["exit"]["selected"] = "JP 01"
    assert selection_of(rt) == GatewaySelection("legacy", "JP 01")
    _, document = load(rt)
    # The legacy rule over twins: REJECT, never the stale gw2 record ("JP 01-2").
    assert groups(document)["EXIT-SELECT"] == ["REJECT"]
    assert rt._persist_gateway_selection_binding() is True
    assert writes == [{"expected_key": "name:JP 01", "label": "JP 01",
                       "identity": None, "unresolved": "ambiguous"}]
    assert "selected" not in rt.config["tiers"]["exit"]
    assert selection_of(rt) == GatewaySelection("unresolved", "JP 01", reason="ambiguous")

    # (b) An older build clears: it pops 'selected' and 'selected_proxy' and
    # knows nothing of the record.
    choose(rt, gw2, "JP 01")
    block = rt.config["tiers"]["exit"]
    block.pop("selected", None)
    block.pop("selected_proxy")
    assert "selected_node" in block
    assert selection_of(rt) == GatewaySelection("none")
    _, document = load(rt, publish=False)
    assert "EXIT-SELECT" not in groups(document)
    assert groups(document)["FINAL-EXIT"] == ["PROXY-FALLBACK", "REJECT"]
    assert rt._persist_gateway_selection_binding() is False
    assert len(writes) == 1
    assert resolved(rt) == (None, "no_selection")


def test_proxy_name_pick_binds_only_when_provable(runtime):
    rt = prepared(runtime)
    target = gw("Tokyo", address="tokyo-gw.example.invalid")
    rt.manager.runtime_nodes["tier2"] = [other("Tokyo", "tier2")]
    rt.manager.runtime_nodes["exit"] = [target]
    # The Sentinel owns "Tokyo": the Gateway renders "Tokyo-2".
    assert rt._gateway_identity_for_pick_name("Tokyo") == ("Tokyo-2", gateway_identity(target))
    assert rt._gateway_identity_for_pick_name("Tokyo-2") == ("Tokyo-2", gateway_identity(target))
    log = picking(rt)
    response = rt.set_exit_selected_proxy("Tokyo")
    assert log["writes"] == [("Tokyo-2", gateway_identity(target))]
    assert log["puts"] == [("FINAL-EXIT", "EXIT-SELECT"), ("EXIT-SELECT", "Tokyo-2")]
    assert response["applied"] is True and response["member"] == "Tokyo-2"
    assert loaded_exits(rt) == {"Tokyo-2": target.address}

    # Twins: no name proves which one was meant; saved by name, fails closed.
    paris = gw("Paris", address="paris-gw.example.invalid", latency_ms=50)
    decoy = gw("Paris", address="decoy-gw.example.invalid", password="DecoyPw-1", latency_ms=10)
    rt.manager.runtime_nodes["tier2"] = []
    rt.manager.runtime_nodes["exit"] = [paris, decoy]
    assert rt._gateway_identity_for_pick_name("Paris-2") == ("Paris-2", None)
    assert rt._gateway_identity_for_pick_name("Paris") == ("Paris", None)
    log = picking(rt)
    response = rt.set_exit_selected_proxy("Paris-2")
    assert log["writes"] == [("Paris-2", None)]
    assert selection_of(rt) == GatewaySelection("legacy", "Paris-2")
    assert exit_select(rt) == ("REJECT",)
    assert log["puts"] == [("FINAL-EXIT", "EXIT-SELECT")]
    assert response["applied"] is False and response["member"] == ""
    assert response["apply_error"] == "gateway_member_unavailable"

    # A twin outside the export (DEAD) still makes the name ambiguous.
    decoy.status = NodeStatus.DEAD
    assert rt._gateway_identity_for_pick_name("Paris") == ("Paris", None)

    # No export view (the service stopped meanwhile): the name only.
    def stopped():
        raise RuntimeError("manager service is not running")

    rt._manager_for_mihomo_runtime_config = stopped
    assert rt._gateway_identity_for_pick_name("X") == ("X", None)


def test_bootstrap_contract_keeps_the_gateway_entries_and_their_decision(runtime, tmp_path):
    """Review RB-2: a contract loaded during bootstrap keeps the formal
    render's Gateway entries and their selection decision (a map replayed
    without the renderer's dialer-proxy dropped every exit, so a pick made
    in that phase reported 'member unavailable' while Core served it)."""

    rt = prepared(runtime, tier3_limit=3)
    target = gw("JP 01", address="jp.example.invalid")
    rt.manager.runtime_nodes["exit"] = [target]
    rt.manager._exit_active = lambda: True
    choose(rt, target, "JP 01")
    payload, receipt = render(rt)
    rt._runtime_pending_export_contract = rt._runtime_export_contract_for_payload(
        payload, rendered_proxy_map=rt._runtime_proxy_map_from_render_receipt(receipt))
    path = tmp_path / "runtime.yaml"
    recorded = []

    def write_formal():
        path.write_text(payload, encoding="utf-8")
        return path

    def write_payload(text, target_path):
        target_path.write_text(text, encoding="utf-8")
        return target_path

    rt.write_mihomo_runtime_config = write_formal
    rt._write_mihomo_runtime_payload = write_payload
    rt._record_pending_runtime_export_contract = lambda *args, **kwargs: recorded.append(kwargs)

    assert rt.write_mihomo_bootstrap_runtime_config() == path
    proxy_map = recorded[-1]["contract"]["proxy_map"]
    gateway = {name: info for name, info in proxy_map.items() if info["tier"] == "exit"}
    assert list(gateway) == ["JP 01"]
    assert gateway["JP 01"]["selected_for"] == "id:" + gateway_identity(target)


def test_the_audit_maps_a_served_leaf_through_the_published_map(runtime):
    """Review CMP-1: the egress audit tests the node Core serves under a leaf,
    taken from the map published with the route; a naming replay over the
    runtime list (gw1 first, DEAD) would name the twin."""

    rt = real_order(prepared(runtime))
    rt.logger = logging.getLogger("tests.gateway-identity.audit")
    gw1 = gw("JP 01", address="gw1.example.invalid", password="Pw-1", latency_ms=20,
             status=NodeStatus.DEAD)
    gw2 = gw("JP 01", address="gw2.example.invalid", password="Pw-2", latency_ms=40)
    rt.manager.runtime_nodes["exit"] = [gw1, gw2]
    choose(rt, gw2, "JP 01-2")
    _payload, document = load(rt)
    member = groups(document)["EXIT-SELECT"][0]
    replay = rt._runtime_proxy_name_map(include_runtime_unusable=True)[member]["node"].address
    assert replay == "gw1.example.invalid"          # what the replay alone would test
    assert rt._runtime_audit_leaf_info(member)["node"].address == "gw2.example.invalid"
    assert rt._runtime_audit_leaf_info("not-a-leaf") is None


def test_a_failing_bind_write_is_tried_once_per_published_contract(runtime):
    """Review RB-1: a compare-and-set that raises (an unwritable file) is
    tried once per published contract and selection, with one warning and no
    traceback, not again (and logged again) on every monitor tick."""

    rt, _gateway = free_gateway(runtime, verified=True)
    _writes, records = binder(rt)
    calls = []

    def unwritable(*args, **kwargs):
        calls.append(args)
        raise PermissionError("config.local.yaml is read-only")

    rt.config_store.compare_and_set_exit_selection = unwritable
    strict_reload(rt, "Free-2")
    reads_the_config(rt)
    assert rt._persist_gateway_selection_binding() is False
    assert rt._persist_gateway_selection_binding() is False
    assert len(calls) == 1
    warnings = [record for record in records if record.levelno == logging.WARNING]
    assert len(warnings) == 1 and warnings[0].exc_info is None
    assert not errors(records)
    assert selection_of(rt) == GatewaySelection("legacy", "Free-2")   # nothing changed
