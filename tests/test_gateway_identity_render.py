"""T-GW-ID: the Gateway (exit) selection is saved by node identity (render side).

The saved selection names a node: ``tiers.exit.selected_node`` holds the
``v1:`` digest of what chooses the exit (protocol, server, port, credentials,
transport, HTTP/SOCKS5 username, gRPC service name) and ``selected_proxy``
its proxy name at pick time as a label.  Every render picks the EXIT-SELECT
member by that identity, unaffected by latency order, status, cross-tier
namesakes or the Tier3 naming switch; an identity no exported exit carries
fails closed (EXIT-SELECT [REJECT]), never another node and never by name.
A name-only selection (an older build's pick, a name-only pick) follows the
legacy rule: it fails closed whenever the label's family over every exit
node (any status) holds two or more identities.

Isolated: the real clash_formatter renderer, models.sort_nodes_for_use, the
real gateway_selection writers and parser, subscription.render_subscription_route
and the P2-04 synthetic runtime for the export view.  No Core, no Mihomo, no
network, no live profile.
"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
import yaml

from clash_formatter import (
    build_clash_proxy_list,
    build_full_clash_config,
    rendered_exit_pairs,
    selected_gateway_nodes,
)
from gateway_selection import (
    GatewaySelection,
    clear_gateway_selection,
    gateway_identity,
    read_selection,
    write_bound_selection,
    write_name_only_selection,
    write_unresolved_selection,
)
from models import Node, NodeStatus, sort_nodes_for_use
from subscription import render_subscription_route
from tests.test_bridge_h5 import real_order
from tests.test_bridge_h8 import prepared
from tests.test_bridge_p2_04 import runtime  # noqa: F401  (pytest fixture)

# Pinned v1 material: a change to any identity field must bump the version.
GOLDEN_GATEWAY = "v1:514af0a1d5c5ec3d662709e36166d6d2271083ec2738eab496b06293b7550d3c"
GOLDEN_RESIDENTIAL = "v1:e44af3a9b3cd46488b9aafab1c3505043b6459db7160ca8291a3951123b28b83"
SELECTION_KEYS = ("selected", "selected_proxy", "selected_node")


# --------------------------------------------------------------- helpers

def node(name, address, *, tier="exit", latency=20.0, status=NodeStatus.ALIVE,
         protocol="trojan", port=443, password=None, **fields):
    return Node(name=name, protocol=protocol, address=address, port=port, tier=tier,
                password=password if password is not None else f"pw-{address}",
                status=status, latency_ms=latency, **fields)


def tiers(*, tier1=(), tier2=(), tier3=(), exits=()):
    return {"tier1": list(tier1), "tier2": list(tier2), "tier3": list(tier3), "exit": list(exits)}


def exported(nodes):
    """The runtime export view's exits: ALIVE/SLOW only, in use order."""
    return sort_nodes_for_use(
        [item for item in nodes if item.status in {NodeStatus.ALIVE, NodeStatus.SLOW}])


def bound(target, label):
    """A selection bound to ``target``, through the real writer and parser."""
    block = {}
    write_bound_selection(block, label, gateway_identity(target))
    selection = read_selection(block)
    assert (selection.state, selection.label, selection.identity) == (
        "bound", label, gateway_identity(target))
    return selection


def legacy(label):
    """A name-only selection (an older build's pick or a name-only pick)."""
    selection = read_selection({"selected_proxy": label})
    assert (selection.state, selection.label) == ("legacy", label)
    return selection


def render(tier_nodes, selection=None, *, stable=False, inventory=None):
    """The real renderer, called the way subscription._render_clash_config
    calls it (the label rides along as ``active_exit_proxy``)."""
    receipt = {}
    text = build_full_clash_config(
        tier_nodes,
        active_exit_proxy=(selection.label or None) if selection is not None else None,
        active_exit_selection=selection,
        exit_inventory=inventory,
        render_receipt=receipt,
        stable_tier3_names=stable,
    )
    document = yaml.safe_load(text)
    groups = {group["name"]: group["proxies"] for group in document["proxy-groups"]}
    proxies = {proxy["name"]: proxy for proxy in document["proxies"]}
    return SimpleNamespace(text=text, groups=groups, proxies=proxies, receipt=receipt,
                           exit_select=groups.get("EXIT-SELECT"),
                           final_exit=groups["FINAL-EXIT"],
                           decision=receipt.get("exit_selection"))


def server(result, name):
    return result.proxies[name]["server"]


def decision(selection, member, identity, resolution):
    return {"requested": selection.label, "key": selection.key, "state": selection.state,
            "member": member, "identity": identity, "resolution": resolution}


# -------------------------------------------------------------- identity

def test_identity_v1_golden_vectors_and_invariances():
    gateway = Node(name="GW", protocol="trojan", address="GW.Example.invalid", port=443,
                   password="pw", sni="sni.example.invalid")
    residential = Node(name="R", protocol="http", address="resi.example.invalid", port=8000,
                       password="p", extra={"username": "u-country-us"})
    assert gateway_identity(gateway) == GOLDEN_GATEWAY
    assert gateway_identity(residential) == GOLDEN_RESIDENTIAL

    # Names, health and bookkeeping never change which exit a node reaches.
    same_exit = {
        "renamed": replace(gateway, name="Renamed GW"),
        "dead": replace(gateway, status=NodeStatus.DEAD),
        "latency": replace(gateway, latency_ms=999),
        "upper-cased address and protocol": replace(
            gateway, address="GW.EXAMPLE.INVALID", protocol="TROJAN"),
        "outer whitespace": replace(gateway, address=" gw.example.invalid ", protocol=" trojan "),
        "port as text": replace(gateway, port="443"),
        "uri": replace(gateway, uri="trojan://pw@gw.example.invalid:443?sni=sni.example.invalid#GW"),
        "source": replace(gateway, source="https://sub.example.invalid/a"),
        "sources": replace(gateway, sources=["https://sub.example.invalid/a", "manual"]),
        "remarks": replace(gateway, remarks="hand note"),
        "clone": gateway.clone(),
    }
    for label, variant in same_exit.items():
        assert gateway_identity(variant) == GOLDEN_GATEWAY, label

    # Every credential, endpoint or transport field counts on its own.
    another_exit = {
        "password": replace(gateway, password="pw-rotated"),
        "port": replace(gateway, port=444),
        "sni": replace(gateway, sni="other-sni.example.invalid"),
        "host": replace(gateway, host="cdn.example.invalid"),
        "path": replace(gateway, path="/tunnel"),
        "address": replace(gateway, address="gw2.example.invalid"),
        "protocol": replace(gateway, protocol="vless"),
        "uuid": replace(gateway, uuid="0b6a3f3e-0000-4000-8000-000000000001"),
        "method": replace(gateway, method="aes-256-gcm"),
        "security": replace(gateway, security="tls"),
        "network": replace(gateway, network="ws"),
        "plugin": replace(gateway, plugin="obfs-local"),
        "username": replace(gateway, extra={"username": "someone"}),
    }
    identities = {label: gateway_identity(variant) for label, variant in another_exit.items()}
    for label, identity in identities.items():
        assert identity != GOLDEN_GATEWAY, label
        assert identity.startswith("v1:") and len(identity) == len(GOLDEN_GATEWAY), label
    assert len(set(identities.values())) == len(identities)


def test_username_and_grpc_service_variants_are_distinct_identities():
    # A multiplexed residential gateway routes by the username: one server,
    # one port, one password, different exits.
    us = Node(name="Resi", protocol="http", address="resi.example.invalid", port=8000,
              password="p", extra={"username": "u-country-us"})
    jp = replace(us, extra={"username": "u-country-jp"})
    assert us.dedupe_key == jp.dedupe_key
    assert gateway_identity(us) != gateway_identity(jp)

    service_a = Node(name="G", protocol="vless", address="grpc.example.invalid", port=443,
                     uuid="0b6a3f3e-0000-4000-8000-00000000000a", network="grpc",
                     security="tls", extra={"serviceName": "a"})
    service_b = replace(service_a, extra={"serviceName": "b"})
    assert service_a.dedupe_key == service_b.dedupe_key
    assert gateway_identity(service_a) != gateway_identity(service_b)

    # The gRPC service name counts only for a gRPC transport.
    ws = Node(name="W", protocol="vless", address="ws.example.invalid", port=443,
              uuid="0b6a3f3e-0000-4000-8000-00000000000a", network="ws", path="/ws",
              extra={"serviceName": "a"})
    assert gateway_identity(ws) == gateway_identity(replace(ws, extra={"serviceName": "b"}))
    assert gateway_identity(ws) == gateway_identity(replace(ws, extra={}))


# -------------------------------------------------------- saved selection

def test_read_selection_states_precedence_and_keys():
    identity = gateway_identity(node("X", "x.invalid"))
    record = {"identity": identity, "label": "X"}
    unsupported = "v2:" + "a" * 64
    cases = [
        ("empty", {}, ("none", "", "", "", "")),
        ("older-build pick", {"selected": "X"}, ("legacy", "X", "", "", "name:X")),
        ("name only", {"selected_proxy": "X"}, ("legacy", "X", "", "", "name:X")),
        ("bound", {"selected_proxy": "X", "selected_node": dict(record)},
         ("bound", "X", identity, "", "id:" + identity)),
        # An older build re-picked (it writes 'selected'): that pick wins.
        ("older-build pick over a record",
         {"selected": "Y", "selected_proxy": "X", "selected_node": dict(record)},
         ("legacy", "Y", "", "", "name:Y")),
        ("empty older-build field",
         {"selected": "", "selected_proxy": "X", "selected_node": dict(record)},
         ("bound", "X", identity, "", "id:" + identity)),
        # An older build's clear pops selected/selected_proxy only.
        ("record without its label", {"selected_node": dict(record)}, ("none", "", "", "", "")),
        # A label edited by hand leaves the record behind: the name alone.
        ("label edited", {"selected_proxy": "X edited", "selected_node": dict(record)},
         ("legacy", "X edited", "", "", "name:X edited")),
        ("unsupported version",
         {"selected_proxy": "X", "selected_node": {"identity": unsupported, "label": "X"}},
         ("invalid", "X", "", "unsupported", "invalid:X")),
        ("malformed identity",
         {"selected_proxy": "X", "selected_node": {"identity": "zzz", "label": "X"}},
         ("invalid", "X", "", "malformed", "invalid:X")),
        ("unresolved",
         {"selected_proxy": "X", "selected_node": {"unresolved": "ambiguous", "label": "X"}},
         ("unresolved", "X", "", "ambiguous", "unresolved:X")),
    ]
    for label, block, expected in cases:
        selection = read_selection(block)
        assert (selection.state, selection.label, selection.identity, selection.reason,
                selection.key) == expected, label
        assert selection.active is (expected[0] != "none"), label
    for block in (None, "X", ["X"], 7):
        assert read_selection(block) == GatewaySelection("none")


def test_selection_writers_set_and_pop_exactly():
    old_identity = gateway_identity(node("Old", "old.invalid"))
    identity = gateway_identity(node("JP 01", "jp.invalid"))
    unrelated = {"subscription_urls": ["https://sub.example.invalid/x"], "nodes": [{"uri": "x"}]}

    def full():
        return {"selected": "Old", "selected_proxy": "Old",
                "selected_node": {"identity": old_identity, "label": "Old"}, **unrelated}

    block = full()
    write_bound_selection(block, "JP 01", identity)
    assert block == {"selected_proxy": "JP 01",
                     "selected_node": {"identity": identity, "label": "JP 01"}, **unrelated}
    assert read_selection(block) == GatewaySelection("bound", "JP 01", identity=identity)

    block = full()
    write_name_only_selection(block, "X")
    assert block == {"selected_proxy": "X", **unrelated}
    assert read_selection(block) == GatewaySelection("legacy", "X")

    block = full()
    write_unresolved_selection(block, "JP 01-2")
    assert block == {"selected_proxy": "JP 01-2",
                     "selected_node": {"unresolved": "ambiguous", "label": "JP 01-2"}, **unrelated}
    assert read_selection(block) == GatewaySelection("unresolved", "JP 01-2", reason="ambiguous")
    write_unresolved_selection(block, "JP 01-2", reason="other")
    assert block["selected_node"] == {"unresolved": "other", "label": "JP 01-2"}

    for writer, args in ((write_bound_selection, ("A", identity)),
                         (write_name_only_selection, ("A",)),
                         (write_unresolved_selection, ("A",))):
        empty = {}
        writer(empty, *args)  # nothing to pop is fine
        assert "selected" not in empty and empty["selected_proxy"] == "A"

    # Every key is removed, not just the first one present.
    block = full()
    assert clear_gateway_selection(block) is True
    assert block == unrelated
    assert clear_gateway_selection(block) is False
    assert clear_gateway_selection({}) is False
    for key in SELECTION_KEYS:
        single = {key: "Old"}
        assert clear_gateway_selection(single) is True and single == {}, key


# ------------------------------------------------------- CC-1 review cases

def test_cc1b_latency_swap_serves_the_picked_gateway():
    gw1 = node("JP 01", "gw1.invalid", latency=20)
    gw2 = node("JP 01", "gw2.invalid", latency=40)
    picked = bound(gw2, "JP 01-2")  # picked while gw1 was faster: gw2 was "JP 01-2"

    before = render(tiers(exits=sort_nodes_for_use([gw1, gw2])), picked)
    assert before.exit_select == ["JP 01-2"]
    assert server(before, "JP 01-2") == "gw2.invalid"
    assert before.final_exit == ["EXIT-SELECT", "REJECT"]
    assert before.decision == decision(picked, "JP 01-2", gateway_identity(gw2), "identity")

    gw2.latency_ms = 10
    exits = sort_nodes_for_use([gw1, gw2])
    assert [item.address for item in exits] == ["gw2.invalid", "gw1.invalid"]
    after = render(tiers(exits=exits), picked)
    # The saved label now names gw1's proxy; the identity still serves gw2.
    assert server(after, "JP 01-2") == "gw1.invalid"
    assert after.exit_select == ["JP 01"]
    assert server(after, "JP 01") == "gw2.invalid"
    assert after.final_exit == ["EXIT-SELECT", "REJECT"]
    assert after.decision == {
        "requested": "JP 01-2", "key": "id:" + gateway_identity(gw2), "state": "bound",
        "member": "JP 01", "identity": gateway_identity(gw2), "resolution": "identity"}

    # The same label saved by name only cannot tell the twins apart.
    by_name = render(tiers(exits=exits), legacy("JP 01-2"))
    assert by_name.exit_select == ["REJECT"]
    assert by_name.final_exit == ["EXIT-SELECT", "REJECT"]
    assert by_name.decision["member"] == ""
    assert by_name.decision["resolution"] == "legacy_ambiguous"


def _jp_twins_one_left(left_by):
    """gw1 left the export (DEAD, or excluded by the runtime audit overlay)."""
    gw1 = node("JP 01", "gw1.invalid", latency=20,
               status=NodeStatus.DEAD if left_by == "dead" else NodeStatus.ALIVE)
    gw2 = node("JP 01", "gw2.invalid", latency=40)
    inventory = [gw1, gw2]
    exits = [gw2]
    if left_by == "dead":
        assert exported(inventory) == exits
    return gw1, gw2, inventory, exits


@pytest.mark.parametrize("left_by", ["dead", "audit-excluded"])
def test_cc1a_dead_twin_leaving_the_export_keeps_serving_the_pick(left_by):
    _gw1, gw2, inventory, exits = _jp_twins_one_left(left_by)
    picked = bound(gw2, "JP 01-2")
    result = render(tiers(exits=exits), picked, inventory=inventory)
    assert result.exit_select == ["JP 01"]  # not REJECT: the label moved, the node did not
    assert server(result, "JP 01") == "gw2.invalid"
    assert result.final_exit == ["EXIT-SELECT", "REJECT"]
    assert result.decision == decision(picked, "JP 01", gateway_identity(gw2), "identity")


@pytest.mark.parametrize("left_by", ["dead", "audit-excluded"])
def test_cc1a_dead_or_excluded_pick_fails_closed_never_its_twin(left_by):
    gw1, _gw2, inventory, exits = _jp_twins_one_left(left_by)
    picked = bound(gw1, "JP 01")
    result = render(tiers(exits=exits), picked, inventory=inventory)
    # The twin now carries the saved label, and is still never served.
    assert server(result, "JP 01") == "gw2.invalid"
    assert result.exit_select == ["REJECT"]
    assert result.final_exit == ["EXIT-SELECT", "REJECT"]
    assert result.decision["member"] == ""
    assert result.decision["resolution"] == "unmatched"

    # A name-only "JP 01": the family over every exit node holds both twins.
    by_name = render(tiers(exits=exits), legacy("JP 01"), inventory=inventory)
    assert by_name.exit_select == ["REJECT"]
    assert by_name.final_exit == ["EXIT-SELECT", "REJECT"]
    assert by_name.decision["member"] == ""
    assert by_name.decision["resolution"] == "legacy_ambiguous"


def test_cc1c_cross_tier_namesake_recovering_keeps_the_gateway():
    sentinel = node("HK", "t2.invalid", tier="tier2")
    hkgw = node("HK", "hkgw.invalid")
    picked = bound(hkgw, "HK")  # picked while the Sentinel "HK" was not exported
    alone = render(tiers(exits=[hkgw]), picked)
    assert alone.exit_select == ["HK"] and server(alone, "HK") == "hkgw.invalid"

    recovered = tiers(tier2=[sentinel], exits=[hkgw])
    result = render(recovered, picked)
    assert server(result, "HK") == "t2.invalid"  # the Sentinel took the plain name
    assert result.exit_select == ["HK-2"]
    assert server(result, "HK-2") == "hkgw.invalid"
    assert result.decision == decision(picked, "HK-2", gateway_identity(hkgw), "identity")

    by_name = render(recovered, legacy("HK"))
    assert by_name.exit_select == ["HK-2"]
    assert server(by_name, "HK-2") == "hkgw.invalid"
    assert by_name.decision["resolution"] == "legacy_base"
    assert by_name.decision["identity"] == gateway_identity(hkgw)

    # A DEAD exit twin (outside the export, inside the inventory) makes the
    # name ambiguous; the identity is unaffected.
    twin = node("HK", "hk-twin.invalid", status=NodeStatus.DEAD)
    with_twin = render(recovered, legacy("HK"), inventory=[hkgw, twin])
    assert with_twin.exit_select == ["REJECT"]
    assert with_twin.decision["resolution"] == "legacy_ambiguous"
    assert render(recovered, picked, inventory=[hkgw, twin]).exit_select == ["HK-2"]


def test_cn2_positional_era_name_is_ambiguous_after_the_switch():
    """'HK-2' saved in the positional era named g1: the positional naming's
    own answer (rendered_exit_pairs, checked against the real render).  After
    the switch to stable Tier3 names the same name is g2's, and before T-GW-ID
    the renderer served the name as it stood (g1 positional, g2 stable).  A
    name-only 'HK-2' now fails closed under both namings, because two
    Gateways share its base and the legacy twin rule does not depend on the
    naming (the rule table's positional 'GW' twins row).  Only the identity
    keeps g1, under both namings."""
    bastion = node("HK", "bastion.invalid", tier="tier3")
    g1 = node("HK", "g1.invalid", latency=10)
    g2 = node("HK", "g2.invalid", latency=30)
    nodes = tiers(tier3=[bastion], exits=sort_nodes_for_use([g1, g2]))

    namings = {}
    for stable in (False, True):
        pairs = [(item.address, name) for item, name in rendered_exit_pairs(
            nodes, stable_tier3_names=stable)]
        rendered = render(nodes, stable=stable)
        assert [(entry["proxy"]["server"], entry["proxy"]["name"])
                for entry in rendered.receipt["proxies"] if entry["tier"] == "exit"] == pairs
        namings[stable] = pairs
    assert namings[False] == [("g1.invalid", "HK-2"), ("g2.invalid", "HK-3")]  # what it meant
    assert namings[True] == [("g1.invalid", "HK"), ("g2.invalid", "HK-2")]     # after the switch

    after_switch = render(nodes, legacy("HK-2"), stable=True)
    assert server(after_switch, "HK-2") == "g2.invalid"  # a name match would serve g2
    assert after_switch.exit_select == ["REJECT"]
    assert after_switch.decision["resolution"] == "legacy_ambiguous"
    positional = render(nodes, legacy("HK-2"), stable=False)
    assert positional.exit_select == ["REJECT"]
    assert positional.decision["resolution"] == "legacy_ambiguous"

    picked = bound(g1, "HK-2")
    for stable, member in ((True, "HK"), (False, "HK-2")):
        result = render(nodes, picked, stable=stable)
        assert result.exit_select == [member], stable
        assert server(result, member) == "g1.invalid", stable
        assert result.decision == decision(picked, member, gateway_identity(g1), "identity")


def test_lsc1_identity_pick_survives_the_naming_switch():
    bastion = node("Free", "c.invalid", tier="tier3")
    f1 = node("Free", "f1.invalid", latency=10)
    f2 = node("Free", "f2.invalid", latency=30)
    nodes = tiers(tier3=[bastion], exits=sort_nodes_for_use([f1, f2]))
    picked = bound(f2, "Free-3")
    for stable, member in ((True, "Free-2"), (False, "Free-3")):
        result = render(nodes, picked, stable=stable)
        assert result.exit_select == [member], stable
        assert server(result, member) == "f2.invalid", stable
        assert result.decision["resolution"] == "identity"
    # The shared base keeps a name-only selection fail-closed.
    by_name = render(nodes, legacy("Free-3"), stable=True)
    assert by_name.exit_select == ["REJECT"]
    assert by_name.decision["resolution"] == "legacy_ambiguous"


def test_unmatched_identity_never_falls_back_to_the_name():
    rotated_from = node("US", "us.invalid", password="old")
    rotated_to = node("US", "us.invalid", password="new")
    removed = node("US", "us-old.invalid")
    replacement = node("US", "us-new.invalid")
    resi_us = node("Resi", "resi.invalid", protocol="http", port=8000, password="p",
                   extra={"username": "u-country-us"})
    resi_jp = node("Resi", "resi.invalid", protocol="http", port=8000, password="p",
                   extra={"username": "u-country-jp"})
    cases = {
        "credential rotated": (rotated_from, rotated_to),
        "node removed, namesake exported": (removed, replacement),
        "other username on the same server": (resi_us, resi_jp),
    }
    for label, (picked_node, substitute) in cases.items():
        nodes = tiers(exits=[substitute])
        picked = bound(picked_node, picked_node.name)
        result = render(nodes, picked)
        assert picked.label in result.proxies, label  # an exit with the label is exported
        assert result.exit_select == ["REJECT"], label
        assert result.final_exit == ["EXIT-SELECT", "REJECT"], label
        assert result.decision["member"] == "", label
        assert result.decision["resolution"] == "unmatched", label
        assert result.decision["key"] == "id:" + gateway_identity(picked_node), label
        # Only the identity keeps the substitute out: the label alone names it.
        assert render(nodes, legacy(picked.label)).exit_select == [picked.label], label


def test_same_identity_duplicates_serve_one_carrier_in_render_order():
    other = node("Dup", "other.invalid", latency=1)
    d1 = node("Dup", "DUP.invalid", latency=10, password="same")
    d2 = node("Dup B", "dup.invalid", latency=30, password="same")
    assert gateway_identity(d1) == gateway_identity(d2) != gateway_identity(other)
    exits = sort_nodes_for_use([d1, d2, other])
    assert [item.address for item in exits] == ["other.invalid", "DUP.invalid", "dup.invalid"]
    picked = bound(d2, "Dup B")
    result = render(tiers(exits=exits), picked)
    assert server(result, "Dup") == "other.invalid"
    assert server(result, "Dup B") == "dup.invalid"  # d2 is exported, later in render order
    assert result.exit_select == ["Dup-2"]           # the first carrier: d1
    assert server(result, "Dup-2").lower() == "dup.invalid"
    assert result.exit_select != ["Dup"]
    assert result.decision == decision(picked, "Dup-2", gateway_identity(d2), "identity")


# --------------------------------------------------- the legacy rule table

def _tokyo_alone():
    target = node("Tokyo", "tokyo-gw.invalid")
    return tiers(exits=[target]), target


def _tokyo_behind_a_sentinel():
    target = node("Tokyo", "tokyo-gw.invalid")
    return tiers(tier2=[node("Tokyo", "t2.invalid", tier="tier2")], exits=[target]), target


def _free_behind_a_bastion():
    target = node("Free", "free-gw.invalid")
    return tiers(tier3=[node("Free", "c.invalid", tier="tier3")], exits=[target]), target


def _hk_and_a_literal_hk2():
    target = node("HK", "hk.invalid")
    return tiers(exits=[target, node("HK-2", "literal.invalid")]), target


def _hk_alone():
    target = node("HK", "hk.invalid")
    return tiers(exits=[target]), target


def _gw_twins_behind_a_bastion():
    picked = node("GW", "gw1.invalid")
    other_gateway = node("GW", "gw0.invalid")
    return tiers(tier3=[node("GW", "c.invalid", tier="tier3")],
                 exits=[other_gateway, picked]), picked


def _gw_alone_behind_a_bastion():
    picked = node("GW", "gw1.invalid")
    return tiers(tier3=[node("GW", "c.invalid", tier="tier3")], exits=[picked]), picked


BASTION_OWN_NAME = "<the Bastion's stable proxy name>"

LEGACY_RULES = [
    pytest.param(_tokyo_alone, "Tokyo", False, "Tokyo", "legacy_exact", id="unique-exact"),
    pytest.param(_tokyo_behind_a_sentinel, "Tokyo", False, "Tokyo-2", "legacy_base",
                 id="cross-tier-namesake-base"),
    pytest.param(_tokyo_behind_a_sentinel, "Tokyo-2", False, "Tokyo-2", "legacy_exact",
                 id="cross-tier-namesake-exact"),
    pytest.param(_free_behind_a_bastion, "Free-2", True, "Free", "legacy_cross_naming",
                 id="positional-era-name-under-stable"),
    pytest.param(_free_behind_a_bastion, "Free", False, "Free-2", "legacy_cross_naming",
                 id="stable-era-name-under-positional"),
    pytest.param(_hk_and_a_literal_hk2, "HK-2", False, None, "legacy_ambiguous",
                 id="literal-suffix-is-ambiguous"),
    pytest.param(_hk_and_a_literal_hk2, "HK", False, "HK", "legacy_exact",
                 id="plain-name-beside-a-literal-suffix"),
    pytest.param(_hk_alone, "HK-2", False, None, "legacy_unconfirmed",
                 id="suffix-no-naming-explains"),
    pytest.param(_free_behind_a_bastion, "Nowhere", True, None, "legacy_missing", id="missing"),
    pytest.param(_free_behind_a_bastion, BASTION_OWN_NAME, True, None, "legacy_missing",
                 id="bastion-own-name"),
    pytest.param(_gw_twins_behind_a_bastion, "GW", False, None, "legacy_ambiguous",
                 id="positional-twins"),
    pytest.param(_gw_alone_behind_a_bastion, "GW", False, "GW-2", "legacy_cross_naming",
                 id="positional-single"),
]


@pytest.mark.parametrize(("build", "label", "stable", "member", "resolution"), LEGACY_RULES)
def test_legacy_rule_table(build, label, stable, member, resolution):
    nodes, target = build()
    if label == BASTION_OWN_NAME:
        label = str(build_clash_proxy_list(nodes["tier3"], stable_names=True)[0]["name"])
        assert render(nodes, stable=stable).groups["TIER3-AUTO"] == [label]
    selection = legacy(label)
    result = render(nodes, selection, stable=stable)
    assert result.final_exit == ["EXIT-SELECT", "REJECT"]
    if member is None:
        assert result.exit_select == ["REJECT"]
        assert result.decision["member"] == ""
        assert {key: result.decision[key] for key in ("requested", "key", "state", "resolution")} == {
            "requested": label, "key": "name:" + label, "state": "legacy", "resolution": resolution}
    else:
        assert result.exit_select == [member]
        assert server(result, member) == target.address
        assert result.decision == decision(selection, member, gateway_identity(target), resolution)


# ------------------------------------------------- receipts and the keyword

@pytest.mark.parametrize(("block", "state"), [
    ({}, "none"),
    ({"selected_proxy": "JP 01", "selected_node": {"unresolved": "ambiguous", "label": "JP 01"}},
     "unresolved"),
    ({"selected_proxy": "JP 01", "selected_node": {"identity": "v2:" + "b" * 64, "label": "JP 01"}},
     "invalid"),
    ({"selected_proxy": "JP 01", "selected_node": {"identity": "not-an-identity", "label": "JP 01"}},
     "invalid"),
], ids=["none", "unresolved", "invalid-unsupported", "invalid-malformed"])
def test_receipt_and_groups_for_none_unresolved_and_invalid(block, state):
    target = node("JP 01", "jp.invalid")
    selection = read_selection(block)
    assert selection.state == state
    result = render(tiers(exits=[target]), selection)
    assert server(result, "JP 01") == "jp.invalid"  # the label's node is exported
    if state == "none":
        assert "exit_selection" not in result.receipt
        assert "EXIT-SELECT" not in result.groups
        assert result.final_exit == ["PROXY-FALLBACK", "REJECT"]
        return
    assert result.exit_select == ["REJECT"]
    assert result.final_exit == ["EXIT-SELECT", "REJECT"]
    assert result.decision == {"requested": "JP 01", "key": f"{state}:JP 01", "state": state,
                               "member": "", "identity": "", "resolution": state}


def test_active_exit_proxy_keyword_is_a_legacy_selection():
    target = node("Tokyo", "tokyo-gw.invalid")
    nodes = tiers(tier2=[node("Tokyo", "t2.invalid", tier="tier2")], exits=[target])
    by_keyword_receipt, by_selection_receipt = {}, {}
    by_keyword = build_full_clash_config(nodes, active_exit_proxy="Tokyo",
                                         render_receipt=by_keyword_receipt)
    by_selection = build_full_clash_config(nodes,
                                           active_exit_selection=GatewaySelection("legacy", "Tokyo"),
                                           render_receipt=by_selection_receipt)
    assert by_keyword == by_selection
    document = yaml.safe_load(by_keyword)
    groups = {group["name"]: group["proxies"] for group in document["proxy-groups"]}
    proxies = {proxy["name"]: proxy for proxy in document["proxies"]}
    assert groups["EXIT-SELECT"] == ["Tokyo-2"]
    assert proxies["Tokyo-2"]["server"] == "tokyo-gw.invalid"
    assert by_keyword_receipt["exit_selection"] == by_selection_receipt["exit_selection"] == {
        "requested": "Tokyo", "key": "name:Tokyo", "state": "legacy", "member": "Tokyo-2",
        "identity": gateway_identity(target), "resolution": "legacy_base"}
    # A blank keyword is no selection at all.
    assert build_full_clash_config(nodes, active_exit_proxy="  ") == build_full_clash_config(nodes)


# ----------------------------- subscription server and runtime export view

def _server_and_runtime_renders(rt):
    """/clash/config from the subscription server's raw Manager (positional
    Tier3 names, every status) and from the runtime's own export view
    (stable Tier3 names, ALIVE/SLOW only), with their render receipts."""
    limits = {"tier3_limit": rt._mihomo_tier3_export_limit()}
    renders = {}
    for label, manager in (("server", rt.manager),
                           ("runtime", rt._manager_for_mihomo_runtime_config())):
        receipt = {}
        _, payload = render_subscription_route(manager, "/clash/config", limits,
                                               render_receipt=receipt)
        document = yaml.safe_load(payload)
        renders[label] = SimpleNamespace(
            groups={group["name"]: group["proxies"] for group in document["proxy-groups"]},
            proxies={proxy["name"]: proxy for proxy in document["proxies"]},
            decision=receipt.get("exit_selection"))
    return renders["server"], renders["runtime"]


def test_subscription_server_and_runtime_view_serve_the_same_node(runtime):
    rt = real_order(prepared(runtime))
    rt.config["tiers"]["tier3"]["verified_provider_writes"] = True
    f1 = node("Free", "f1.invalid", latency=10)
    f2 = node("Free", "f2.invalid", latency=30)
    gone = node("Free", "f0.invalid", latency=5, status=NodeStatus.DEAD)
    rt.manager.runtime_nodes["tier3"] = [node("Free", "c.invalid", tier="tier3")]
    rt.manager.runtime_nodes["exit"] = [f2, gone, f1]
    picked = bound(f2, "Free-2")
    rt.manager.active_exit_selection = lambda: picked

    from_server, from_runtime = _server_and_runtime_renders(rt)
    # The subscription server: positional names, every status (the DEAD node too).
    assert from_server.groups["TIER3-AUTO"] == ["Free"]
    assert "f0.invalid" in {proxy["server"] for proxy in from_server.proxies.values()}
    assert from_server.groups["EXIT-SELECT"] == ["Free-3"]
    assert from_server.proxies["Free-3"]["server"] == "f2.invalid"
    # The runtime export view: stable Tier3 names, ALIVE/SLOW only.
    (tier3_name,) = from_runtime.groups["TIER3-AUTO"]
    assert tier3_name.startswith("Free·")
    assert "f0.invalid" not in {proxy["server"] for proxy in from_runtime.proxies.values()}
    assert from_runtime.groups["EXIT-SELECT"] == ["Free-2"]
    assert from_runtime.proxies["Free-2"]["server"] == "f2.invalid"
    for rendered, member in ((from_server, "Free-3"), (from_runtime, "Free-2")):
        assert rendered.groups["FINAL-EXIT"] == ["EXIT-SELECT", "REJECT"]
        assert rendered.decision == decision(picked, member, gateway_identity(f2), "identity")


def test_subscription_server_and_runtime_view_judge_a_legacy_name_alike(runtime):
    """Both resolve with one function over every exit node: the runtime view
    passes its frozen exit inventory (DEAD exits included), the server
    exports every status."""
    rt = real_order(prepared(runtime))
    rt.config["tiers"]["tier3"]["verified_provider_writes"] = True
    target = node("Free", "free-gw.invalid")
    twin = node("Free", "twin.invalid", status=NodeStatus.DEAD)
    rt.manager.runtime_nodes["tier3"] = [node("Free", "c.invalid", tier="tier3")]
    rt.manager.runtime_nodes["exit"] = [target, twin]
    saved = legacy("Free")
    rt.manager.active_exit_selection = lambda: saved

    from_server, from_runtime = _server_and_runtime_renders(rt)
    # The runtime renders the target alone under the saved label; the DEAD
    # twin outside its export still makes the name ambiguous.
    assert from_runtime.proxies["Free"]["server"] == "free-gw.invalid"
    for rendered in (from_server, from_runtime):
        assert rendered.groups["EXIT-SELECT"] == ["REJECT"]
        assert rendered.decision["resolution"] == "legacy_ambiguous"

    rt.manager.runtime_nodes["exit"] = [target]
    from_server, from_runtime = _server_and_runtime_renders(rt)
    assert from_server.groups["EXIT-SELECT"] == ["Free-2"]
    assert from_server.proxies["Free-2"]["server"] == "free-gw.invalid"
    assert from_server.decision == decision(saved, "Free-2", gateway_identity(target),
                                            "legacy_cross_naming")
    assert from_runtime.groups["EXIT-SELECT"] == ["Free"]
    assert from_runtime.proxies["Free"]["server"] == "free-gw.invalid"
    assert from_runtime.decision == decision(saved, "Free", gateway_identity(target),
                                             "legacy_exact")


# --------------------------------------------- selected nodes without a render

def test_selected_gateway_nodes_rule():
    jp1 = node("JP 01", "jp1.invalid", latency=20)
    jp2 = node("JP 01", "jp2.invalid", latency=40)
    hk = node("HK", "hk.invalid")
    exits = [jp1, jp2, hk]

    def picked(selection, nodes=exits):
        return [item.address for item in selected_gateway_nodes(selection, nodes)]

    assert picked(bound(jp2, "JP 01-2")) == ["jp2.invalid"]  # only the picked twin
    assert picked(bound(jp1, "JP 01")) == ["jp1.invalid"]
    assert picked(legacy("HK")) == ["hk.invalid"]
    assert picked(legacy("HK-2")) == []    # a suffixed label names a position, not a node
    assert picked(legacy("JP 01")) == []   # two identities
    assert picked(legacy("JP 01-2")) == []
    assert picked(bound(node("JP 01", "gone.invalid"), "JP 01")) == []
    for block in ({},
                  {"selected_proxy": "HK", "selected_node": {"unresolved": "ambiguous", "label": "HK"}},
                  {"selected_proxy": "HK", "selected_node": {"identity": "zzz", "label": "HK"}}):
        assert picked(read_selection(block)) == [], block

    # Every carrier of the identity, whatever its name.
    hk_copy = replace(hk, name="HK copy")
    assert picked(bound(hk, "HK"), [*exits, hk_copy]) == ["hk.invalid", "hk.invalid"]
    assert [item.name for item in selected_gateway_nodes(bound(hk, "HK"), [*exits, hk_copy])] == [
        "HK", "HK copy"]
